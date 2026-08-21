#!/usr/bin/env python3
"""
deploy.py - one-shot deployer for the AI-vision detection stack.

Stands up the COMPLETE cloud stack in a fresh AWS account, in dependency order,
idempotently (safe to re-run; existing resources are adopted, not duplicated).
Source of truth: deployer/STACK_MANIFEST.md (recreate-level BOM audited from the
live reference account) + the raw config JSONs in deployer/audit/.

Usage (bootstrap credentials must already be configured, e.g. `aws configure`):
    python deploy.py --profile customer --region us-east-1 \
        --prefix vision --target-label my-pest \
        --sender-email alerts@example.com --recipient-email me@example.com

    python deploy.py ... --only s3,lambda     # run selected stages
    python deploy.py ... --dry-run            # print the plan, change nothing

Design rules (from the product scope ruling, 2026-07-08/09):
  - Generic product: nothing armyworm-specific ships; names are parameterized.
  - Minimal set only: 5 Lambdas, 4 tables, 3 buckets, Cognito, HTTP API,
    CloudFront, 1 watchdog schedule, empty Rekognition project, SES identities.
  - NO dataset migrates. NO card data ever touches this tool.
  - Every generated id is written back to deployer/out/deploy_state.json and,
    where relevant, into the dashboard config before it is synced.
"""

import argparse
import io
import json
import mimetypes
import re
import sys
import time
import uuid
import zipfile
from pathlib import Path

import boto3
import botocore

import os

if getattr(sys, "frozen", False):
    # Packaged (PyInstaller): assets unpack to _MEIPASS; state must live in a
    # writable per-user dir (the _MEIPASS temp dir is wiped on exit).
    _BASE = Path(sys._MEIPASS)               # noqa: SLF001
    HERE = _BASE
    REPO = _BASE                             # lambda/ bundled at _BASE/lambda
    AUDIT = _BASE / "audit"
    OUT = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ARGUS" / "out"
    OUT.mkdir(parents=True, exist_ok=True)
else:
    HERE = Path(__file__).resolve().parent
    REPO = HERE.parent
    AUDIT = HERE / "audit"
    OUT = HERE / "out"

# Reference-account literals that must be rewritten into customer values.
REF_ACCOUNT = "366356442579"
REF_REGION = "us-east-1"
REF_BUCKETS = {
    "frames-armyworm-366356442579": "{prefix}-frames-{account}",
    "processed-images-armyworm-366356442579": "{prefix}-processed-{account}",
    "pest-dashboard-366356442579": "{prefix}-dashboard-{account}",
}

TABLES = {
    "cameras": "pest-monitoring-cameras",
    "detections": "pest-monitoring-detections",
    "system_config": "pest-monitoring-system-config",
    "schedule_logs": "pest-monitoring-schedule-logs",
}

LAMBDAS = {
    # name -> (source file, memory MB, timeout s, env builder key)
    # 1024 MB / 600 s matches the validated production sizing. A tiled frame
    # plus up to 120 Bedrock verify crops measures 24-54 s, so the old 60 s
    # timeout failed intermittently on real frames.
    "pest-detection-processor": (REPO / "lambda" / "pest-detection-processor.py", 1024, 600),
    "pest-monitoring-api": (REPO / "lambda" / "pest-monitoring-api.py", 256, 30),
    "pest-camera-scheduler": (REPO / "lambda" / "pest-camera-scheduler.py", 128, 60),
    "kvs-hls-handler": (AUDIT / "kvs-hls-handler_src" / "lambda_function.py", 128, 30),
    # v6.2 watchdog (repo mirror): honours per-camera max_runtime_min so a
    # scheduled morning start auto-closes ~45 min later. The old audit
    # snapshot only knew the global 75-min cap.
    "pest-model-watchdog": (REPO / "lambda" / "pest-model-watchdog.py", 128, 30),
}

# 21 routes from manifest section 1.7: (method, path, integration, auth)
ROUTES = [
    ("GET", "/cost", "api", True),
    ("GET", "/settings", "api", True),
    ("POST", "/settings", "api", True),
    ("GET", "/history", "api", True),
    ("GET", "/presigned-url", "api", True),
    ("POST", "/detection/verify", "api", True),
    ("DELETE", "/detection", "api", True),
    ("GET", "/model/status", "api", True),
    ("POST", "/model/start", "api", True),
    ("POST", "/model/stop", "api", True),
    ("GET", "/identities", "api", True),
    ("POST", "/identities", "api", True),
    ("DELETE", "/identities", "api", True),
    ("GET", "/schedule", "api", True),
    ("POST", "/schedule", "api", True),
    ("DELETE", "/schedule", "api", True),
    ("GET", "/schedule-logs", "api", True),
    ("POST", "/stream/start", "api", True),
    ("POST", "/stream/stop", "api", True),
    ("GET", "/stream/status", "api", False),  # device pollers, unauthenticated
    ("GET", "/video-playback", "hls", True),
]

CACHING_DISABLED_POLICY = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"  # AWS-managed, global id
PILLOW_VERSION = "12.2.0"


# Optional event sink so a host (the desktop app) can stream progress to a UI.
# Signature: emit(kind, payload_dict). Kinds: "log", "stage", "done", "error".
_EMIT = None


def set_emitter(fn):
    """Route structured deploy events to a host UI (the ARGUS desktop shell)."""
    global _EMIT
    _EMIT = fn


def _emit(kind, **payload):
    if _EMIT:
        try:
            _EMIT(kind, payload)
        except Exception:
            pass


def log(msg):
    print(f"[deploy] {msg}", flush=True)
    _emit("log", message=str(msg))


def die(msg):
    print(f"[deploy] FATAL: {msg}", file=sys.stderr, flush=True)
    _emit("error", message=str(msg))
    sys.exit(1)


# Human-facing stage labels for the UI (the deployment "theater").
STAGE_LABELS = {
    "iam": "Identity & permissions",
    "dynamodb": "Database",
    "s3": "Storage",
    "layer": "Image-processing library",
    "lambda": "Cloud functions",
    "s3-notification": "Event wiring",
    "cognito": "Sign-in service",
    "apigw": "Secure API",
    "cloudfront": "Dashboard & delivery",
    "writeback": "Configuration",
    "kvs": "Live video",
    "scheduler": "Cost guard",
    "rekognition": "Vision engine",
    "seed": "Initial data",
    "ses": "Alerts",
}


def run_plan(ctx, plan):
    """Run a list of (name, fn) stages, emitting structured events per stage.
    Returns (ok, error_message). Used by the desktop app; main() has its own loop."""
    import time as _time
    t0 = _time.time()
    # A new run invalidates any previous run's completion stamp NOW — otherwise
    # a second deployment that dies mid-run still reads as 'complete' and the
    # app resumes to the WRONG (previous) deployment's summary. Also record the
    # run's region/prefix so verify() can audit the right place later even if
    # the UI's saved config drifts.
    ctx.state.pop("deploy:completed_at", None)
    ctx.state.pop("deploy:last_stage", None)
    ctx.save("deploy:started_at", _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()))
    ctx.save("deploy:region", ctx.region)
    ctx.save("deploy:prefix", ctx.prefix)
    ctx.save("deploy:account", ctx.account)
    total = len(plan)
    for i, (name, fn) in enumerate(plan):
        _emit("stage", index=i, total=total, name=name,
              label=STAGE_LABELS.get(name, name), status="running")
        log(f"--- stage: {name} ---")
        try:
            fn(ctx)
        except (Exception, SystemExit) as e:
            _emit("stage", index=i, total=total, name=name,
                  label=STAGE_LABELS.get(name, name), status="error")
            _emit("error", message=f"{name}: {e}")
            return False, f"{name}: {e}"
        _emit("stage", index=i, total=total, name=name,
              label=STAGE_LABELS.get(name, name), status="done")
        # progress marker: lets a later launch distinguish "never ran" /
        # "died at stage N" / "completed" and offer the right resume action
        ctx.save("deploy:last_stage", name)
    ctx.save("deploy:completed_at",
             _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()))
    _emit("done",
          dashboard=f"https://{ctx.state.get('cloudfront:domain', '')}",
          api=ctx.state.get("apigw:url", ""),
          seconds=int(_time.time() - t0))
    return True, None


class Ctx:
    """Resolved deployment context: sessions, parameters, generated ids."""

    def __init__(self, args):
        self.session = boto3.Session(profile_name=args.profile, region_name=args.region)
        sts = self.session.client("sts")
        ident = sts.get_caller_identity()
        self.account = ident["Account"]
        self.region = args.region
        self.prefix = args.prefix
        self.target_label = args.target_label
        self.deployment_name = args.deployment_name
        self.sender_email = args.sender_email
        self.recipient_email = args.recipient_email or args.sender_email
        self.live_view = args.live_view
        self.dry_run = args.dry_run
        self.state_path = OUT / "deploy_state.json"
        self.state = {}
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        log(f"account={self.account} region={self.region} prefix={self.prefix} "
            f"caller={ident['Arn']}")

    @classmethod
    def from_params(cls, *, access_key_id, secret_access_key, region, prefix,
                    target_label, sender_email, recipient_email=None,
                    deployment_name="My deployment", live_view=False, dry_run=False):
        """Build a Ctx from a pasted IAM access key instead of a named profile —
        used by the desktop app (app.py). Same post-conditions as __init__."""
        self = cls.__new__(cls)
        self.session = boto3.Session(
            aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key,
            region_name=region)
        ident = self.session.client("sts").get_caller_identity()
        self.account = ident["Account"]
        self.region = region
        self.prefix = prefix
        self.target_label = target_label
        self.deployment_name = deployment_name
        self.sender_email = sender_email
        self.recipient_email = recipient_email or sender_email
        self.live_view = live_view
        self.dry_run = dry_run
        self.state_path = OUT / "deploy_state.json"
        self.state = {}
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        log(f"account={self.account} region={self.region} prefix={self.prefix} "
            f"caller={ident['Arn']}")
        return self

    # ---- naming ----
    def bucket(self, kind):
        return f"{self.prefix}-{kind}-{self.account}"

    @property
    def frames_bucket(self):
        return self.bucket("frames")

    @property
    def processed_bucket(self):
        return self.bucket("processed")

    @property
    def dashboard_bucket(self):
        return self.bucket("dashboard")

    # ---- helpers ----
    def client(self, svc):
        return self.session.client(svc)

    def save(self, key, value):
        self.state[key] = value
        OUT.mkdir(exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def rewrite(self, text):
        """Rewrite reference-account literals into this deployment's values."""
        for old, tpl in REF_BUCKETS.items():
            text = text.replace(old, tpl.format(prefix=self.prefix, account=self.account))
        text = text.replace(REF_ACCOUNT, self.account)
        # Region only in ARN-ish contexts; blanket replace is safe for our docs.
        text = text.replace(REF_REGION, self.region)
        return text

    def load_audit_json(self, name):
        """Load an audit JSON with account/region/bucket rewrites applied."""
        raw = (AUDIT / name).read_text(encoding="utf-8")
        return json.loads(self.rewrite(raw))


def wait_for(fn, desc, tries=20, delay=3):
    """Poll fn() until it returns truthy (IAM eventual consistency etc.)."""
    for _ in range(tries):
        try:
            if fn():
                return True
        except botocore.exceptions.ClientError:
            pass
        time.sleep(delay)
    die(f"timed out waiting for {desc}")


def already_exists(err):
    code = err.response["Error"]["Code"]
    return code in (
        "EntityAlreadyExists", "ResourceConflictException", "ResourceInUseException",
        "BucketAlreadyOwnedByYou", "AlreadyExistsException", "ConflictException",
        "UsernameExistsException",
    )


# =============================================================================
# Stage 1 - IAM (roles + inline policies from the audited documents)
# =============================================================================
LAMBDA_TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}

ROLE_POLICIES = {
    # role -> list of (policy-name, audit file)
    "pest-detection-processor-role": [
        ("pest-detection-processor-policy",
         "iam__pest-detection-processor-role__inline__pest-detection-processor-policy.json"),
        ("read-system-config",
         "iam__pest-detection-processor-role__inline__read-system-config.json"),
        # Without bedrock-verify the LLM gate gets AccessDenied on every crop
        # and the fail-closed denoiser rejects every detection - a fresh stack
        # deploys "working" but detects nothing. Captured from the live role
        # 2026-08-11; inference-profile ARNs are account/region-rewritten.
        ("bedrock-verify",
         "iam__pest-detection-processor-role__inline__bedrock-verify.json"),
        # v6.x writes EXIF-normalised frames back under frames/.
        ("s3-frames-write",
         "iam__pest-detection-processor-role__inline__s3-frames-write.json"),
    ],
    "pest-monitoring-api-role": [
        ("pest-monitoring-api-policy",
         "iam__pest-monitoring-api-role__inline__pest-monitoring-api-policy.json"),
        ("pest-monitoring-ddb-full-access",
         "iam__pest-monitoring-api-role__inline__pest-monitoring-ddb-full-access.json"),
        ("read-processed-armyworm",
         "iam__pest-monitoring-api-role__inline__read-processed-armyworm.json"),
    ],
    "pest-camera-scheduler-role": [
        ("pest-camera-scheduler-policy",
         "iam__pest-camera-scheduler-role__inline__pest-camera-scheduler-policy.json"),
    ],
    "kvs-hls-handler-role": [
        ("kvs-hls-policy",
         "iam__kvs-hls-handler-role__managed__kvs-hls-handler-rolePolicy.json"),
    ],
    "pest-model-watchdog-role": [
        ("rekognition-db-handler",
         "iam__pest-model-watchdog-role__inline__Rekognition-DB-handler.json"),
    ],
}

LAMBDA_ROLE_FOR = {
    "pest-detection-processor": "pest-detection-processor-role",
    "pest-monitoring-api": "pest-monitoring-api-role",
    "pest-camera-scheduler": "pest-camera-scheduler-role",
    "kvs-hls-handler": "kvs-hls-handler-role",
    "pest-model-watchdog": "pest-model-watchdog-role",
}


# AWS-managed policies the reference account attaches on top of the inline
# ones. Without these the API deploys and answers most routes, but GET
# /identities (the Alerts page) and GET /cost both return 500 - measured on a
# fresh deployment 2026-08-12. The inline documents alone are NOT the whole
# permission set; the audit only captured inline policies.
ROLE_MANAGED = {
    "pest-monitoring-api-role": [
        "arn:aws:iam::aws:policy/AmazonSESFullAccess",       # GET/POST/DELETE /identities
        "arn:aws:iam::aws:policy/AWSBillingReadOnlyAccess",  # GET /cost
    ],
}


def stage_iam(ctx):
    iam = ctx.client("iam")
    basic = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

    for role, policies in ROLE_POLICIES.items():
        try:
            iam.create_role(RoleName=role,
                            AssumeRolePolicyDocument=json.dumps(LAMBDA_TRUST))
            log(f"iam: created role {role}")
        except botocore.exceptions.ClientError as e:
            if not already_exists(e):
                raise
            log(f"iam: role {role} exists, adopting")
        iam.attach_role_policy(RoleName=role, PolicyArn=basic)
        for managed in ROLE_MANAGED.get(role, []):
            iam.attach_role_policy(RoleName=role, PolicyArn=managed)
            log(f"iam: {role} + {managed.split('/')[-1]}")
        for pname, fname in policies:
            doc = ctx.load_audit_json(fname)
            # Fix the audited latent bug: PassRole must point at OUR role name.
            body = json.dumps(doc).replace(
                "pest-scheduler-invocation-role", "pest-scheduler-invocation-role")
            iam.put_role_policy(RoleName=role, PolicyName=pname, PolicyDocument=body)
        ctx.save(f"role:{role}", f"arn:aws:iam::{ctx.account}:role/{role}")

    # Scheduler invocation role (EventBridge Scheduler assumes it to invoke Lambdas)
    sched_trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "scheduler.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": ctx.account}},
        }],
    }
    sched_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": "lambda:InvokeFunction",
            "Resource": [
                f"arn:aws:lambda:{ctx.region}:{ctx.account}:function:pest-model-watchdog",
                f"arn:aws:lambda:{ctx.region}:{ctx.account}:function:pest-model-watchdog:*",
                f"arn:aws:lambda:{ctx.region}:{ctx.account}:function:pest-camera-scheduler",
                f"arn:aws:lambda:{ctx.region}:{ctx.account}:function:pest-camera-scheduler:*",
            ],
        }],
    }
    role = "pest-scheduler-invocation-role"
    try:
        iam.create_role(RoleName=role, AssumeRolePolicyDocument=json.dumps(sched_trust))
        log(f"iam: created role {role}")
    except botocore.exceptions.ClientError as e:
        if not already_exists(e):
            raise
    iam.put_role_policy(RoleName=role, PolicyName="invoke-scheduled-lambdas",
                        PolicyDocument=json.dumps(sched_policy))
    ctx.save(f"role:{role}", f"arn:aws:iam::{ctx.account}:role/{role}")
    log("iam: done (6 roles)")


# =============================================================================
# Stage 2 - DynamoDB (4 tables; detections carries the by-pest-time GSI)
# =============================================================================
def stage_dynamodb(ctx):
    ddb = ctx.client("dynamodb")
    specs = {
        TABLES["cameras"]: dict(
            KeySchema=[{"AttributeName": "camera_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "camera_id", "AttributeType": "S"}],
        ),
        TABLES["detections"]: dict(
            KeySchema=[{"AttributeName": "image_id", "KeyType": "HASH"},
                       {"AttributeName": "detection_time", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "image_id", "AttributeType": "S"},
                {"AttributeName": "detection_time", "AttributeType": "S"},
                {"AttributeName": "pest_type", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "by-pest-time",
                "KeySchema": [{"AttributeName": "pest_type", "KeyType": "HASH"},
                              {"AttributeName": "detection_time", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
        ),
        TABLES["system_config"]: dict(
            KeySchema=[{"AttributeName": "config_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_key", "AttributeType": "S"}],
        ),
        TABLES["schedule_logs"]: dict(
            KeySchema=[{"AttributeName": "log_id", "KeyType": "HASH"},
                       {"AttributeName": "timestamp", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "log_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
        ),
    }
    for name, spec in specs.items():
        try:
            ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", **spec)
            log(f"ddb: creating {name}")
        except botocore.exceptions.ClientError as e:
            if not already_exists(e):
                raise
            log(f"ddb: {name} exists, adopting")
    for name in specs:
        ddb.get_waiter("table_exists").wait(TableName=name)
    log("ddb: done (4 tables)")


# =============================================================================
# Stage 3 - S3 (3 buckets; dashboard = public website; frames CORS added later
#              once the CloudFront domain exists)
# =============================================================================
def create_bucket(ctx, s3, name):
    kwargs = {"Bucket": name}
    if ctx.region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": ctx.region}
    try:
        s3.create_bucket(**kwargs)
        log(f"s3: created {name}")
    except botocore.exceptions.ClientError as e:
        if not already_exists(e):
            raise
        log(f"s3: {name} exists, adopting")


def stage_s3(ctx):
    s3 = ctx.client("s3")
    for b in (ctx.frames_bucket, ctx.processed_bucket, ctx.dashboard_bucket):
        create_bucket(ctx, s3, b)
        s3.put_bucket_encryption(Bucket=b, ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault":
                       {"SSEAlgorithm": "AES256"}}]})

    # frames + processed stay private with versioning (matches the reference)
    for b in (ctx.frames_bucket, ctx.processed_bucket):
        s3.put_bucket_versioning(Bucket=b,
                                 VersioningConfiguration={"Status": "Enabled"})
        s3.put_public_access_block(Bucket=b, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True})

    # dashboard: public static website (PAB off FIRST, then policy — ordering trap)
    db = ctx.dashboard_bucket
    s3.put_public_access_block(Bucket=db, PublicAccessBlockConfiguration={
        "BlockPublicAcls": False, "IgnorePublicAcls": False,
        "BlockPublicPolicy": False, "RestrictPublicBuckets": False})
    s3.put_bucket_policy(Bucket=db, Policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Sid": "PublicReadDashboard", "Effect": "Allow",
                       "Principal": "*", "Action": "s3:GetObject",
                       "Resource": f"arn:aws:s3:::{db}/*"}]}))
    s3.put_bucket_website(Bucket=db, WebsiteConfiguration={
        "IndexDocument": {"Suffix": "index.html"},
        "ErrorDocument": {"Key": "index.html"}})
    ctx.save("bucket:frames", ctx.frames_bucket)
    ctx.save("bucket:processed", ctx.processed_bucket)
    ctx.save("bucket:dashboard", ctx.dashboard_bucket)
    log("s3: done (3 buckets)")


# =============================================================================
# Stage 4 - Lambda layer (Pillow built locally via pip, published directly)
# =============================================================================
def stage_layer(ctx):
    import subprocess
    lam = ctx.client("lambda")

    # A frozen .exe has no pip/build toolchain and sys.executable is the exe, so
    # prefer a PREBUILT layer zip bundled beside the app. `build.ps1` produces
    # `layer/fyp-pillow.zip` once; it ships as an --add-data asset.
    prebuilt = None
    for cand in (HERE / "layer" / "fyp-pillow.zip", REPO / "layer" / "fyp-pillow.zip"):
        if cand.exists():
            prebuilt = cand
            break

    if prebuilt:
        log(f"layer: publishing prebuilt {prebuilt.name}")
        zip_bytes = prebuilt.read_bytes()
    else:
        if getattr(sys, "frozen", False):
            die("layer: no prebuilt fyp-pillow.zip bundled — the packaged build "
                "must include layer/fyp-pillow.zip (see build.ps1). Cannot pip-build "
                "inside a frozen exe.")
        build = OUT / "layer-build"
        pydir = build / "python"
        if not (pydir / "PIL").exists():
            pydir.mkdir(parents=True, exist_ok=True)
            log(f"layer: pip-installing Pillow {PILLOW_VERSION} (manylinux, py312)")
            subprocess.run([
                sys.executable, "-m", "pip", "install",
                "--platform", "manylinux2014_x86_64",
                "--implementation", "cp", "--python-version", "3.12",
                "--only-binary=:all:", "--target", str(pydir),
                f"Pillow=={PILLOW_VERSION}",
            ], check=True)
        # The exact defect that broke layer v1 in the reference account:
        if not list(pydir.glob("PIL/_imaging*.so")):
            die("layer build is missing PIL/_imaging*.so (compiled binaries absent)")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(build.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(build))
        zip_bytes = buf.getvalue()

    resp = lam.publish_layer_version(
        LayerName="fyp-pillow",
        Content={"ZipFile": zip_bytes},
        CompatibleRuntimes=["python3.12"],
        CompatibleArchitectures=["x86_64"],
        Description=f"Pillow {PILLOW_VERSION} for the detection runtime",
    )
    ctx.save("layer:fyp-pillow", resp["LayerVersionArn"])
    log(f"layer: published {resp['LayerVersionArn']}")


# =============================================================================
# Stage 5 - Lambda functions (5)
# =============================================================================
def lambda_env(ctx, name):
    common_tables = {
        "TABLE_CAMERAS": TABLES["cameras"],
        "TABLE_SYSTEM_CONFIG": TABLES["system_config"],
        "TABLE_DETECTIONS": TABLES["detections"],
    }
    if name == "pest-detection-processor":
        # The detection tuning below is the validated production configuration,
        # not code defaults. Without it a fresh stack comes up in the old
        # trust-Rekognition-above-min_confidence mode on Haiku, which is the
        # architecture this project measured and replaced.
        return {**common_tables,
                "SENDER_EMAIL": ctx.sender_email,
                "S3_PROCESSED_BUCKET": ctx.processed_bucket,
                # Detector runs as a high-recall frontend; the LLM does the
                # rejecting, so tiles gather candidates down to 8%.
                "TILE_MIN_CONFIDENCE": "8",
                # Denoiser mode: every box is judged, no confidence exemption.
                "LLM_VERIFY_ALL_BOXES": "true",
                "LLM_VERIFY_MODEL_ID": "us.anthropic.claude-sonnet-4-6",
                "LLM_VERIFY_MAX_BOXES": "120",
                "LLM_VERIFY_MAX_TOKENS": "300",
                "LLM_VERIFY_WORKERS": "3",
                "LLM_VERIFY_PAD": "0.6",
                # Post-gate cleanup: NMS by overlap AND by containment (a small
                # box wholly inside a big one scores ~0 IoU and would survive),
                # plus a size sanity cap and the display floor.
                "POST_NMS_IOU": "0.1",
                "POST_NMS_CONTAIN": "0.1",
                "POST_MAX_BOX_AREA": "0.05",
                # 33, not 49. 49 came from the threshold study on the dev
                # account; after the model was retrained on production it
                # was dropping real worms and was re-tuned down on
                # 2026-08-13. Live Lambda and the worm_cam row both read
                # 33 - raising this again silently loses detections.
                "POST_VERIFY_FLOOR": "33"}
    if name == "pest-monitoring-api":
        return {**common_tables,
                "TABLE_SCHEDULE_LOGS": TABLES["schedule_logs"],
                "S3_FRAMES_BUCKET": ctx.frames_bucket,
                "S3_PROCESSED_BUCKET": ctx.processed_bucket,
                "SCHEDULE_EXECUTOR_ARN":
                    f"arn:aws:lambda:{ctx.region}:{ctx.account}:function:pest-camera-scheduler"}
    if name == "pest-camera-scheduler":
        return {"TABLE_CAMERAS": TABLES["cameras"],
                "TABLE_SCHEDULE_LOGS": TABLES["schedule_logs"]}
    if name == "pest-model-watchdog":
        return {"TABLE_CAMERAS": TABLES["cameras"], "MAX_RUNTIME_MIN": "75"}
    return {}


def zip_lambda(src_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("lambda_function.py", src_path.read_text(encoding="utf-8"))
    return buf.getvalue()


def stage_lambda(ctx):
    lam = ctx.client("lambda")
    layer_arn = ctx.state.get("layer:fyp-pillow")
    if not layer_arn:
        die("layer stage must run before lambda stage")

    # pest-camera-scheduler FIRST (the api env references its ARN)
    order = ["pest-camera-scheduler", "pest-detection-processor",
             "pest-monitoring-api", "kvs-hls-handler", "pest-model-watchdog"]
    for name in order:
        src, mem, timeout = LAMBDAS[name]
        if not src.exists():
            die(f"lambda source missing: {src}")
        role_arn = ctx.state[f"role:{LAMBDA_ROLE_FOR[name]}"]
        code = zip_lambda(src)
        kwargs = dict(
            FunctionName=name, Runtime="python3.12",
            Handler="lambda_function.lambda_handler",
            Role=role_arn, MemorySize=mem, Timeout=timeout,
            Environment={"Variables": lambda_env(ctx, name)},
            Architectures=["x86_64"],
        )
        if name == "pest-detection-processor":
            kwargs["Layers"] = [layer_arn]

        def try_create():
            try:
                lam.create_function(Code={"ZipFile": code}, **kwargs)
                return True
            except botocore.exceptions.ClientError as e:
                if already_exists(e):
                    lam.update_function_code(FunctionName=name, ZipFile=code)
                    lam.get_waiter("function_updated").wait(FunctionName=name)
                    cfg = {k: v for k, v in kwargs.items() if k != "Architectures"}
                    lam.update_function_configuration(**cfg)
                    return True
                if e.response["Error"]["Code"] == "InvalidParameterValueException":
                    return False  # IAM role not yet propagated - retry
                raise

        wait_for(try_create, f"lambda {name} creation (IAM propagation)")
        lam.get_waiter("function_active_v2").wait(FunctionName=name)
        ctx.save(f"lambda:{name}",
                 f"arn:aws:lambda:{ctx.region}:{ctx.account}:function:{name}")
        log(f"lambda: {name} ready")
    log("lambda: done (5 functions)")


# =============================================================================
# Stage 6 - S3 -> processor notification (permission FIRST, then notification)
# =============================================================================
def stage_s3_notification(ctx):
    lam, s3 = ctx.client("lambda"), ctx.client("s3")
    fn_arn = ctx.state["lambda:pest-detection-processor"]
    try:
        lam.add_permission(
            FunctionName="pest-detection-processor",
            StatementId="s3-frames-invoke",
            Action="lambda:InvokeFunction",
            Principal="s3.amazonaws.com",
            SourceArn=f"arn:aws:s3:::{ctx.frames_bucket}",
            SourceAccount=ctx.account,
        )
    except botocore.exceptions.ClientError as e:
        if not already_exists(e):
            raise
    s3.put_bucket_notification_configuration(
        Bucket=ctx.frames_bucket,
        NotificationConfiguration={"LambdaFunctionConfigurations": [{
            "Id": "frames-to-processor",
            "LambdaFunctionArn": fn_arn,
            "Events": ["s3:ObjectCreated:*"],
            "Filter": {"Key": {"FilterRules": [
                {"Name": "Prefix", "Value": "frames/"}]}},
        }]})
    log("s3-notification: frames/ -> pest-detection-processor wired")


# =============================================================================
# Stage 7 - Cognito (pool + SPA client; zero users seeded)
# =============================================================================
def stage_cognito(ctx):
    cog = ctx.client("cognito-idp")
    pool_id = None
    pools = cog.list_user_pools(MaxResults=60)["UserPools"]
    for p in pools:
        if p["Name"] == "pest-dashboard-users":
            pool_id = p["Id"]
            log(f"cognito: pool exists ({pool_id}), adopting")
    if not pool_id:
        pool_id = cog.create_user_pool(
            PoolName="pest-dashboard-users",
            UsernameAttributes=["email"],
            AutoVerifiedAttributes=["email"],
            AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
            Policies={"PasswordPolicy": {
                "MinimumLength": 8, "RequireUppercase": False,
                "RequireLowercase": True, "RequireNumbers": True,
                "RequireSymbols": False}},
        )["UserPool"]["Id"]
        log(f"cognito: pool created ({pool_id})")

    client_id = None
    clients = cog.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)
    for c in clients["UserPoolClients"]:
        if c["ClientName"] == "dashboard-web":
            client_id = c["ClientId"]
            log(f"cognito: client exists ({client_id}), adopting")
    if not client_id:
        client_id = cog.create_user_pool_client(
            UserPoolId=pool_id, ClientName="dashboard-web",
            GenerateSecret=False,
            ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
            IdTokenValidity=12, AccessTokenValidity=12, RefreshTokenValidity=30,
            TokenValidityUnits={"IdToken": "hours", "AccessToken": "hours",
                                "RefreshToken": "days"},
        )["UserPoolClient"]["ClientId"]
        log(f"cognito: client created ({client_id})")
    ctx.save("cognito:pool", pool_id)
    ctx.save("cognito:client", client_id)


# =============================================================================
# Stage 8 - API Gateway (HTTP API + JWT authorizer + 2 integrations + 21 routes)
# =============================================================================
def stage_apigw(ctx):
    gw = ctx.client("apigatewayv2")
    lam = ctx.client("lambda")

    api_id = None
    for a in gw.get_apis(MaxResults="100")["Items"]:
        if a["Name"] == "pest-monitoring-api-gateway":
            api_id = a["ApiId"]
            log(f"apigw: api exists ({api_id}), adopting")
    if not api_id:
        api_id = gw.create_api(
            Name="pest-monitoring-api-gateway", ProtocolType="HTTP",
            CorsConfiguration={
                "AllowOrigins": ["*"],
                "AllowMethods": ["GET", "POST", "DELETE", "OPTIONS"],
                "AllowHeaders": ["content-type", "authorization"],
                "MaxAge": 3600},
        )["ApiId"]
        log(f"apigw: api created ({api_id})")

    # JWT authorizer bound to the Cognito pool/client
    auth_id = None
    for a in gw.get_authorizers(ApiId=api_id, MaxResults="50")["Items"]:
        if a["Name"] == "cognito-dashboard":
            auth_id = a["AuthorizerId"]
    if not auth_id:
        auth_id = gw.create_authorizer(
            ApiId=api_id, Name="cognito-dashboard", AuthorizerType="JWT",
            IdentitySource=["$request.header.Authorization"],
            JwtConfiguration={
                "Audience": [ctx.state["cognito:client"]],
                "Issuer": f"https://cognito-idp.{ctx.region}.amazonaws.com/"
                          f"{ctx.state['cognito:pool']}"},
        )["AuthorizerId"]

    def integration(fn_name):
        uri = ctx.state[f"lambda:{fn_name}"]
        for i in gw.get_integrations(ApiId=api_id, MaxResults="50")["Items"]:
            if i.get("IntegrationUri") == uri:
                return i["IntegrationId"]
        return gw.create_integration(
            ApiId=api_id, IntegrationType="AWS_PROXY", IntegrationUri=uri,
            IntegrationMethod="POST", PayloadFormatVersion="2.0",
        )["IntegrationId"]

    int_api = integration("pest-monitoring-api")
    int_hls = integration("kvs-hls-handler")

    existing = {r["RouteKey"]: r for r in
                gw.get_routes(ApiId=api_id, MaxResults="100")["Items"]}
    for method, path, target, needs_auth in ROUTES:
        key = f"{method} {path}"
        tgt = f"integrations/{int_api if target == 'api' else int_hls}"
        kwargs = dict(ApiId=api_id, RouteKey=key, Target=tgt)
        if needs_auth:
            kwargs.update(AuthorizationType="JWT", AuthorizerId=auth_id)
        else:
            kwargs.update(AuthorizationType="NONE")
        if key in existing:
            # update_route REQUIRES ApiId — only RouteKey must be stripped.
            # (Stripping ApiId too made every RESUME fail with "Missing required
            # parameter: ApiId" on the first pre-existing route; the create-only
            # first run never exercised this branch. Fixed 2026-07-16.)
            gw.update_route(RouteId=existing[key]["RouteId"], **{
                k: v for k, v in kwargs.items() if k != "RouteKey"})
        else:
            gw.create_route(**kwargs)
    log(f"apigw: {len(ROUTES)} routes in place (JWT on all but GET /stream/status)")

    # $default auto-deploy stage
    try:
        gw.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True)
    except botocore.exceptions.ClientError as e:
        if not already_exists(e):
            raise

    # invoke permissions: api-wide for the api fn, route-scoped for hls
    perms = [
        ("pest-monitoring-api", "apigw-invoke",
         f"arn:aws:execute-api:{ctx.region}:{ctx.account}:{api_id}/*"),
        ("kvs-hls-handler", "apigw-invoke-videoplayback",
         f"arn:aws:execute-api:{ctx.region}:{ctx.account}:{api_id}/*/*/video-playback"),
    ]
    for fn, sid, src in perms:
        try:
            lam.add_permission(FunctionName=fn, StatementId=sid,
                               Action="lambda:InvokeFunction",
                               Principal="apigateway.amazonaws.com", SourceArn=src)
        except botocore.exceptions.ClientError as e:
            if not already_exists(e):
                raise

    ctx.save("apigw:id", api_id)
    ctx.save("apigw:url", f"https://{api_id}.execute-api.{ctx.region}.amazonaws.com")
    log(f"apigw: done -> {ctx.state['apigw:url']}")


# =============================================================================
# Stage 9 - CloudFront (HTTPS in front of the dashboard website endpoint)
# =============================================================================
def stage_cloudfront(ctx):
    cf = ctx.client("cloudfront")
    comment = f"{ctx.prefix} dashboard frontend (S3 website origin)"
    for d in cf.list_distributions().get("DistributionList", {}).get("Items", []) or []:
        if d.get("Comment") == comment:
            ctx.save("cloudfront:id", d["Id"])
            ctx.save("cloudfront:domain", d["DomainName"])
            log(f"cloudfront: exists ({d['Id']}), adopting")
            return
    website_endpoint = (f"{ctx.dashboard_bucket}.s3-website-{ctx.region}.amazonaws.com"
                        if ctx.region != "us-east-1" else
                        f"{ctx.dashboard_bucket}.s3-website-us-east-1.amazonaws.com")
    resp = cf.create_distribution(DistributionConfig={
        "CallerReference": f"{ctx.prefix}-dashboard-{uuid.uuid4()}",
        "Comment": comment,
        "Enabled": True,
        "DefaultRootObject": "index.html",
        "Origins": {"Quantity": 1, "Items": [{
            "Id": "s3-website-dashboard",
            "DomainName": website_endpoint,
            "CustomOriginConfig": {
                "HTTPPort": 80, "HTTPSPort": 443,
                "OriginProtocolPolicy": "http-only",
                "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]}},
        }]},
        "DefaultCacheBehavior": {
            "TargetOriginId": "s3-website-dashboard",
            "ViewerProtocolPolicy": "redirect-to-https",
            "CachePolicyId": CACHING_DISABLED_POLICY,
            "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                               "CachedMethods": {"Quantity": 2,
                                                 "Items": ["GET", "HEAD"]}},
            "Compress": True},
    })
    ctx.save("cloudfront:id", resp["Distribution"]["Id"])
    ctx.save("cloudfront:domain", resp["Distribution"]["DomainName"])
    log(f"cloudfront: created {resp['Distribution']['Id']} "
        f"-> https://{resp['Distribution']['DomainName']} (deploying, takes minutes)")


# =============================================================================
# Stage 10 - config write-back + dashboard sync + frames CORS
# =============================================================================
def stage_writeback(ctx):
    s3 = ctx.client("s3")
    web = REPO / "web" / "dashboard_v4"
    cfg = (web / "js" / "config.js").read_text(encoding="utf-8")
    cfg = re.sub(r"HTTP_API:\s*'[^']*'", f"HTTP_API: '{ctx.state['apigw:url']}'", cfg)
    cfg = re.sub(r"COGNITO_REGION:\s*'[^']*'",
                 f"COGNITO_REGION: '{ctx.region}'", cfg)
    cfg = re.sub(r"COGNITO_CLIENT_ID:\s*'[^']*'",
                 f"COGNITO_CLIENT_ID: '{ctx.state['cognito:client']}'", cfg)

    uploaded = 0
    for f in sorted(web.rglob("*")):
        if not f.is_file():
            continue
        key = f.relative_to(web).as_posix()
        body = cfg.encode() if key == "js/config.js" else f.read_bytes()
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if f.suffix == ".js":
            ctype = "text/javascript"
        s3.put_object(Bucket=ctx.dashboard_bucket, Key=key, Body=body,
                      ContentType=ctype)
        uploaded += 1
    log(f"writeback: dashboard synced ({uploaded} files, config.js templated)")

    cf_domain = ctx.state.get("cloudfront:domain", "")
    website = (f"http://{ctx.dashboard_bucket}.s3-website-{ctx.region}"
               f".amazonaws.com")
    origins = [website] + ([f"https://{cf_domain}"] if cf_domain else [])
    s3.put_bucket_cors(Bucket=ctx.frames_bucket, CORSConfiguration={
        "CORSRules": [{
            "AllowedHeaders": ["*"],
            "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
            "AllowedOrigins": origins,
            "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 3000}]})
    log(f"writeback: frames CORS -> {origins}")


# =============================================================================
# Stage 11 - KVS (optional live-view streams)
# =============================================================================
def stage_kvs(ctx):
    if not ctx.live_view:
        log("kvs: live view disabled, skipping")
        return
    kvs = ctx.client("kinesisvideo")
    name = f"{ctx.prefix}-cam-stream"
    try:
        kvs.create_stream(StreamName=name, DataRetentionInHours=2)
        log(f"kvs: created stream {name}")
    except botocore.exceptions.ClientError as e:
        if not already_exists(e):
            raise
        log(f"kvs: stream {name} exists, adopting")
    ctx.save("kvs:stream", name)


# =============================================================================
# Stage 12 - EventBridge Scheduler (cost-guard watchdog every 15 min)
# =============================================================================
def stage_scheduler(ctx):
    sch = ctx.client("scheduler")
    try:
        sch.create_schedule(
            Name="pest-model-watchdog-15min",
            ScheduleExpression="rate(15 minutes)",
            FlexibleTimeWindow={"Mode": "OFF"},
            State="ENABLED",
            Target={
                "Arn": ctx.state["lambda:pest-model-watchdog"],
                "RoleArn": ctx.state["role:pest-scheduler-invocation-role"],
                "RetryPolicy": {"MaximumEventAgeInSeconds": 86400,
                                "MaximumRetryAttempts": 0}},
        )
        log("scheduler: watchdog schedule created (rate 15m)")
    except botocore.exceptions.ClientError as e:
        if not already_exists(e):
            raise
        log("scheduler: watchdog schedule exists, adopting")


# =============================================================================
# Stage 13 - Rekognition project skeleton (customer trains later)
# =============================================================================
def stage_rekognition(ctx):
    rek = ctx.client("rekognition")
    name = f"{ctx.prefix}-detection"
    for p in rek.describe_projects()["ProjectDescriptions"]:
        if p["ProjectArn"].split("/")[1] == name:
            ctx.save("rekognition:project", p["ProjectArn"])
            log(f"rekognition: project exists, adopting")
            return
    arn = rek.create_project(ProjectName=name)["ProjectArn"]
    ctx.save("rekognition:project", arn)
    log(f"rekognition: project created ({arn}) — training happens later, "
        f"with the customer's own labeled images")


# =============================================================================
# Stage 14 - Seeds (system config + 2 camera rows) and SES identities
# =============================================================================
def stage_seed(ctx):
    ddb = ctx.session.resource("dynamodb")
    ddb.Table(TABLES["system_config"]).put_item(Item={
        "config_key": "detection_settings",
        "email_enabled": True,
        "recipient_email": ctx.recipient_email,
        "additional_recipients": [],
        "auto_capture": True,
        "capture_interval": 60,
    })
    cameras = ddb.Table(TABLES["cameras"])
    # Two per-camera fields decide whether the detection pipeline actually
    # works, and both are opt-in, so a seeded row must set them explicitly:
    #   llm_verify_enabled - the LLM gate is per-camera by design (one Lambda
    #     serves every camera and a wrong-domain prompt must never run on a
    #     camera that did not ask for it). Left unset, the gate NEVER runs.
    #   min_confidence 10 - this is the CANDIDATE floor feeding the gate, not
    #     the display threshold. Setting it high silently strangles recall
    #     before the LLM ever sees a box. The user-facing threshold is
    #     post_verify_floor, which the dashboard edits.
    common = {
        "target_label": ctx.target_label,
        "model_type": "custom",
        "min_confidence": 10,
        "llm_verify_enabled": True,
        "post_verify_floor": 49,
        "model_running": False,
        "custom_model_arn": "",   # filled after the customer trains
    }
    cameras.put_item(Item={
        **common,
        "camera_id": "manual_upload",
        "label": "Manual test upload",
        "default_waypoint_id": "manual_upload",
        "stream_enabled": False,
        "kvs_stream_name": None,
        "schedule": {},
    })
    cameras.put_item(Item={
        **common,
        "camera_id": "camera-1",
        "label": ctx.deployment_name,
        "default_waypoint_id": None,
        "tiling_enabled": True,
        # Bounds an unattended model start: the watchdog stops the endpoint
        # this many minutes after it first sees it RUNNING.
        "max_runtime_min": 45,
        "stream_enabled": bool(ctx.live_view),
        "kvs_stream_name": ctx.state.get("kvs:stream"),
        "schedule": {"enabled": False, "days": [], "start_time": "09:00",
                     "end_time": "17:00"},
    })
    log("seed: system config + 2 camera rows written")


def stage_ses(ctx):
    ses = ctx.client("ses")
    for email in {ctx.sender_email, ctx.recipient_email}:
        ses.verify_email_identity(EmailAddress=email)
        log(f"ses: verification email sent to {email} (must be confirmed)")
    acct = ctx.client("sesv2").get_account()
    if not acct.get("ProductionAccessEnabled", False):
        log("ses: NOTE — this account is in the SES SANDBOX. Alert emails only "
            "reach verified addresses until production access is granted "
            "(manual AWS request, ~1-2 business days).")


# =============================================================================
STAGES = [
    ("iam", stage_iam),
    ("dynamodb", stage_dynamodb),
    ("s3", stage_s3),
    ("layer", stage_layer),
    ("lambda", stage_lambda),
    ("s3-notification", stage_s3_notification),
    ("cognito", stage_cognito),
    ("apigw", stage_apigw),
    ("cloudfront", stage_cloudfront),
    ("writeback", stage_writeback),
    ("kvs", stage_kvs),
    ("scheduler", stage_scheduler),
    ("rekognition", stage_rekognition),
    ("seed", stage_seed),
    ("ses", stage_ses),
]


def destroy(ctx):
    """Tear the WHOLE deployment down, reverse dependency order. Driven by
    deploy_state.json + fixed naming; every step is best-effort (a missing
    resource is reported and skipped, never fatal) so a partial stack tears
    down cleanly too. Emits ('destroy', {line, ok}) events per step and a final
    ('destroy_done', {...}).

    SAFETY: refuses to run if the state file was written for a DIFFERENT
    account than the credentials point at."""
    import time as _time

    if ctx.state.get("deploy:account") and ctx.state["deploy:account"] != ctx.account:
        raise RuntimeError(
            f"state file belongs to account {ctx.state['deploy:account']} but the "
            f"stored key is for {ctx.account} — refusing to destroy")

    done, failed = [], []

    def step(label, fn):
        try:
            fn()
            done.append(label)
            _emit("destroy", line=f"DELETE  {label}", ok=True)
            log(f"destroy: {label} OK")
        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            benign = code in ("ResourceNotFoundException", "NoSuchEntity", "NotFoundException",
                              "NoSuchBucket", "NoSuchDistribution", "404", "ValidationException")
            (done if benign else failed).append(label)
            _emit("destroy", line=f"DELETE  {label}"
                  + ("  (already gone)" if benign else f"  FAILED: {str(e)[:90]}"),
                  ok=benign)
            log(f"destroy: {label} {'already gone' if benign else 'FAILED: ' + str(e)[:200]}")

    # 1. cost guard schedule
    sch = ctx.client("scheduler")
    step("schedule pest-model-watchdog-15min",
         lambda: sch.delete_schedule(Name="pest-model-watchdog-15min"))

    # 2. rekognition (stop running versions, delete versions, delete project)
    def _rek():
        rek = ctx.client("rekognition")
        arn = ctx.state.get("rekognition:project")
        if not arn:
            return
        name = arn.split("/")[1]
        vers = rek.describe_project_versions(ProjectArn=arn)["ProjectVersionDescriptions"]
        for v in vers:
            if v["Status"] == "RUNNING":
                rek.stop_project_version(ProjectVersionArn=v["ProjectVersionArn"])
        for _ in range(30):
            vers = rek.describe_project_versions(ProjectArn=arn)["ProjectVersionDescriptions"]
            if not any(v["Status"] in ("RUNNING", "STOPPING") for v in vers):
                break
            _time.sleep(10)
        for v in vers:
            rek.delete_project_version(ProjectVersionArn=v["ProjectVersionArn"])
        rek.delete_project(ProjectArn=arn)
    step("rekognition project", _rek)

    # 3. KVS stream
    def _kvs():
        kvs = ctx.client("kinesisvideo")
        name = ctx.state.get("kvs:stream")
        if not name:
            return
        info = kvs.describe_stream(StreamName=name)["StreamInfo"]
        kvs.delete_stream(StreamARN=info["StreamARN"],
                          CurrentVersion=info["Version"])
    step("kvs stream", _kvs)

    # 4. CloudFront (disable -> wait Deployed -> delete; the slow one)
    def _cf():
        cf = ctx.client("cloudfront")
        did = ctx.state.get("cloudfront:id")
        if not did:
            return
        conf = cf.get_distribution_config(Id=did)
        if conf["DistributionConfig"]["Enabled"]:
            conf["DistributionConfig"]["Enabled"] = False
            cf.update_distribution(Id=did, IfMatch=conf["ETag"],
                                   DistributionConfig=conf["DistributionConfig"])
            _emit("destroy", line="        cloudfront disabling — takes a few minutes",
                  ok=True)
        cf.get_waiter("distribution_deployed").wait(
            Id=did, WaiterConfig={"Delay": 30, "MaxAttempts": 40})
        conf = cf.get_distribution_config(Id=did)
        cf.delete_distribution(Id=did, IfMatch=conf["ETag"])
    step("cloudfront distribution", _cf)

    # 5. API gateway
    def _api():
        if ctx.state.get("apigw:id"):
            ctx.client("apigatewayv2").delete_api(ApiId=ctx.state["apigw:id"])
    step("api gateway", _api)

    # 6. cognito pool (clients + users die with it)
    def _cog():
        if ctx.state.get("cognito:pool"):
            ctx.client("cognito-idp").delete_user_pool(
                UserPoolId=ctx.state["cognito:pool"])
    step("cognito user pool", _cog)

    # 7. lambdas
    lam = ctx.client("lambda")
    for name in LAMBDAS:
        step(f"lambda {name}",
             lambda n=name: lam.delete_function(FunctionName=n))

    # 8. layer versions
    def _layer():
        vers = lam.list_layer_versions(LayerName="fyp-pillow")["LayerVersions"]
        for v in vers:
            lam.delete_layer_version(LayerName="fyp-pillow",
                                     Version=v["Version"])
    step("lambda layer fyp-pillow", _layer)

    # 9. buckets (empty, then delete)
    s3 = ctx.client("s3")
    for kind in ("frames", "processed", "dashboard"):
        bucket = ctx.bucket(kind)

        def _bkt(b=bucket):
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=b):
                objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if objs:
                    s3.delete_objects(Bucket=b, Delete={"Objects": objs})
            s3.delete_bucket(Bucket=b)
        step(f"bucket {bucket}", _bkt)

    # 10. dynamodb tables
    ddb = ctx.client("dynamodb")
    for tname in TABLES.values():
        step(f"table {tname}",
             lambda t=tname: ddb.delete_table(TableName=t))

    # 11. IAM roles (inline + attached policies first)
    iam = ctx.client("iam")
    roles = [k.split(":", 1)[1] for k in ctx.state if k.startswith("role:")]

    def _role(r):
        for p in iam.list_role_policies(RoleName=r)["PolicyNames"]:
            iam.delete_role_policy(RoleName=r, PolicyName=p)
        for p in iam.list_attached_role_policies(RoleName=r)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=r, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=r)
    for r in roles:
        step(f"role {r}", lambda rr=r: _role(rr))

    # 12. SES identities (best-effort)
    def _ses():
        ses = ctx.client("ses")
        for email in {ctx.sender_email, ctx.recipient_email}:
            if email and "@" in email:
                ses.delete_identity(Identity=email)
    step("ses identities", _ses)

    # 13. archive the state file — the deployment no longer exists
    try:
        stamp = _time.strftime("%Y%m%d_%H%M%S", _time.gmtime())
        ctx.state_path.rename(ctx.state_path.with_name(f"destroyed_{stamp}.json"))
    except Exception:
        pass

    _emit("destroy_done", deleted=len(done), failed=len(failed),
          failures=failed[:10])
    log(f"destroy: {len(done)} deleted, {len(failed)} failed")
    return {"deleted": len(done), "failed": failed}


def verify(ctx):
    """Live post-deployment audit: prove every resource recorded in
    deploy_state.json actually exists in the account RIGHT NOW. Answers
    "did it really deploy, completely?" with per-resource evidence instead
    of trust. Returns a list of {"name", "ok", "detail"} dicts; read-only,
    costs nothing, safe to run any time (including on a partial deploy —
    missing resources simply come back ok=False)."""
    checks = []

    def add(name, fn):
        try:
            detail = fn()
            checks.append({"name": name, "ok": True, "detail": str(detail or "")[:120]})
        except Exception as e:
            msg = getattr(e, "response", {}).get("Error", {}).get("Code", "") or str(e)
            checks.append({"name": name, "ok": False, "detail": str(msg)[:200]})

    iam = ctx.client("iam"); s3 = ctx.client("s3"); lam = ctx.client("lambda")
    ddb = ctx.client("dynamodb"); cog = ctx.client("cognito-idp")
    apigw = ctx.client("apigatewayv2"); cf = ctx.client("cloudfront")
    kvs = ctx.client("kinesisvideo"); rek = ctx.client("rekognition")
    sch = ctx.client("scheduler")

    for key, val in sorted(ctx.state.items()):
        if key.startswith("role:"):
            add(key, lambda v=val: iam.get_role(RoleName=v.split("/")[-1])["Role"]["Arn"])
        elif key.startswith("bucket:"):
            add(key, lambda v=val: (s3.head_bucket(Bucket=v), v)[1])
        elif key.startswith("lambda:"):
            add(key, lambda v=val: lam.get_function(FunctionName=v)
                ["Configuration"]["State"])
        elif key.startswith("layer:"):
            add(key, lambda v=val: lam.get_layer_version_by_arn(Arn=v)["LayerVersionArn"])
        elif key == "cognito:pool":
            add(key, lambda v=val: cog.describe_user_pool(UserPoolId=v)["UserPool"]["Id"])
        elif key == "cognito:client":
            add(key, lambda v=val: cog.describe_user_pool_client(
                UserPoolId=ctx.state["cognito:pool"], ClientId=v)
                ["UserPoolClient"]["ClientId"])
        elif key == "apigw:id":
            add(key, lambda v=val: apigw.get_api(ApiId=v)["ApiId"])
        elif key == "cloudfront:id":
            # detail = distribution status: "Deployed" is fully live;
            # "InProgress" means the edge rollout is still propagating.
            add(key, lambda v=val: cf.get_distribution(Id=v)["Distribution"]["Status"])
        elif key == "kvs:stream":
            add(key, lambda v=val: kvs.describe_stream(StreamName=v)
                ["StreamInfo"]["Status"])
        elif key == "rekognition:project":
            def _rek(v=val):
                for p in rek.describe_projects()["ProjectDescriptions"]:
                    if p["ProjectArn"] == v:
                        return p["Status"]
                raise RuntimeError("project not found")
            add(key, _rek)

    # Resources the stages create but never wrote into state:
    for tname in TABLES.values():
        add(f"table:{tname}", lambda v=tname: ddb.describe_table(TableName=v)
            ["Table"]["TableStatus"])
    add("schedule:pest-model-watchdog-15min",
        lambda: sch.get_schedule(Name="pest-model-watchdog-15min")["State"])

    ok_n = sum(1 for c in checks if c["ok"])
    log(f"verify: {ok_n}/{len(checks)} resources present")
    return checks


def main():
    ap = argparse.ArgumentParser(description="One-shot stack deployer")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--prefix", default="vision",
                    help="resource name prefix (lowercase, no dots)")
    ap.add_argument("--deployment-name", default="My deployment")
    ap.add_argument("--target-label", required=True,
                    help="the object class the customer will train, e.g. 'my-pest'")
    ap.add_argument("--sender-email", required=True)
    ap.add_argument("--recipient-email", default=None)
    ap.add_argument("--live-view", action="store_true")
    ap.add_argument("--only", default=None,
                    help="comma-separated stage names to run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="audit an existing deployment (read-only) instead of deploying")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z][a-z0-9-]{1,20}", args.prefix):
        die("--prefix must be lowercase alphanumeric/hyphens, 2-21 chars")

    ctx = Ctx(args)

    if args.verify:
        checks = verify(ctx)
        bad = [c for c in checks if not c["ok"]]
        for c in checks:
            mark = "OK " if c["ok"] else "MISS"
            log(f"  [{mark}] {c['name']:<44} {c['detail']}")
        log(f"verify: {len(checks) - len(bad)}/{len(checks)} present"
            + ("" if not bad else " — deployment is INCOMPLETE"))
        return

    selected = [s.strip() for s in args.only.split(",")] if args.only else None
    plan = [(n, f) for n, f in STAGES if not selected or n in selected]
    if not plan:
        die(f"--only matched no stages: {args.only}")

    log(f"plan: {' -> '.join(n for n, _ in plan)}")
    if ctx.dry_run:
        log("dry-run: no changes made")
        return

    ctx.state.pop("deploy:completed_at", None)
    ctx.state.pop("deploy:last_stage", None)
    ctx.save("deploy:started_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    ctx.save("deploy:region", ctx.region)
    ctx.save("deploy:prefix", ctx.prefix)
    t0 = time.time()
    for name, fn in plan:
        log(f"--- stage: {name} ---")
        fn(ctx)
        ctx.save("deploy:last_stage", name)
    # The completion stamp means THE WHOLE STACK ran — a --only subset must
    # never mark the deployment complete (the app's resume logic trusts it).
    if [n for n, _ in plan] == [n for n, _ in STAGES]:
        ctx.save("deploy:completed_at",
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    else:
        log(f"subset run ({len(plan)} stage(s)) — completion stamp not written")
    log(f"ALL DONE in {time.time() - t0:.0f}s")
    log(f"dashboard: https://{ctx.state.get('cloudfront:domain', '(pending)')}")
    log(f"api:       {ctx.state.get('apigw:url')}")
    log("next steps: confirm the SES verification email; create dashboard "
        "sign-in accounts (Cognito admin-create); label images and train "
        "the first model, then set custom_model_arn on the camera rows.")


if __name__ == "__main__":
    main()
