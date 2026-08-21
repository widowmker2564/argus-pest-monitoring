### Deep audit — RESOLVED 2026-07-08 (recreate-level BOM for deploy.py)

_This section supersedes the in-progress notes above. It is the definitive,
recreate-level spec built from 12 per-service live-account audits (account
**366356442579** / region **us-east-1** / profile `nbk2`, read-only). Every PROD
resource below lists its exact config and the `deployer/audit/*.json` file holding
the raw spec deploy.py consumes. Nothing here is copied from `docs/aws.md` — it is
all verified against live `describe`/`get`/`list` calls._

---

## 1. PROD resource set — what deploy.py MUST recreate

### 1.1 IAM roles + policies (create FIRST — everything else references these)

All 5 Lambda execution roles share the **same trust policy**
(`iam__trust-policy-lambda.json`): Principal `Service: lambda.amazonaws.com`,
Action `sts:AssumeRole`. Create each at path `/` (drop the live console
`/service-role/` paths and random suffixes).

| Role | Attached AWS-managed | Inline / customer-managed policies | Raw files |
|---|---|---|---|
| **pest-detection-processor-role** | `AWSLambdaBasicExecutionRole` | inline `pest-detection-processor-policy` (S3 GetObject on frames-armyworm/\*, Get+Put on processed-images-armyworm/\*, DDB PutItem/GetItem/UpdateItem/Query on detections+index/\*, GetItem on cameras, `rekognition:DetectCustomLabels` \*, `ses:SendEmail/SendRawEmail/ListVerifiedEmailAddresses` \*); inline `read-system-config` (DDB GetItem on system-config) | `iam__trust-policy-lambda.json`, `iam__pest-detection-processor-role__inline__pest-detection-processor-policy.json`, `iam__pest-detection-processor-role__inline__read-system-config.json` |
| **pest-monitoring-api-role** | `AmazonSESFullAccess`, `AWSLambdaBasicExecutionRole`, `AWSBillingReadOnlyAccess` | inline `pest-monitoring-api-policy` (S3 Get/Put/List/Delete on both armyworm buckets; `dynamodb:*` on cameras+detections+index/\*; rekognition Describe/Start/StopProjectVersion \*; scheduler CRUD scoped to `schedule/default/pest-camera-*`; events PutRule/DeleteRule/PutTargets/RemoveTargets/ListTargetsByRule/DescribeRule scoped to `rule/pest-sched-*`; lambda AddPermission/RemovePermission/InvokeFunction on pest-camera-scheduler + kvs-hls-url-generator; `iam:PassRole` to the scheduler invocation role with Condition `iam:PassedToService=scheduler.amazonaws.com`; kinesisvideo Describe/List/GetDataEndpoint/GetHLSStreamingSessionURL \*); inline `pest-monitoring-ddb-full-access` (6-verb DDB on all 4 pest tables); inline `read-processed-armyworm` (S3 GetObject on processed-images-armyworm/\*) | `iam__pest-monitoring-api-role__inline__pest-monitoring-api-policy.json`, `iam__pest-monitoring-api-role__inline__pest-monitoring-ddb-full-access.json`, `iam__pest-monitoring-api-role__inline__read-processed-armyworm.json` |
| **pest-camera-scheduler-role** | `AWSLambdaBasicExecutionRole` | inline `pest-camera-scheduler-policy` (rekognition Start/Stop/DescribeProjectVersions \*; DDB GetItem/UpdateItem on cameras) | `iam__pest-camera-scheduler-role__inline__pest-camera-scheduler-policy.json` |
| **kvs-hls-handler-role** | `AWSLambdaBasicExecutionRole` | customer-managed `kvs-hls-handler-rolePolicy` (kinesisvideo GetDataEndpoint + GetHLSStreamingSessionURL \*) — recreate as a managed policy OR inline it | `iam__kvs-hls-handler-role__managed__kvs-hls-handler-rolePolicy.json` |
| **pest-model-watchdog-role** | `AWSLambdaBasicExecutionRole` (use the AWS-managed one; skip the auto-generated scoped-logs policy — identical effect) | inline `Rekognition-DB-handler` (DDB Scan/UpdateItem on cameras; rekognition DescribeProjects/DescribeProjectVersions/StopProjectVersion \*) | `iam__pest-model-watchdog-role__inline__Rekognition-DB-handler.json`, `iam__pest-model-watchdog-role__managed__AWSLambdaBasicExecutionRole-generated.json` |

**Scheduler invocation role (create ONE, stably named e.g. `pest-scheduler-invocation-role`):**
- Trust: `scheduler.amazonaws.com` `sts:AssumeRole`, Condition `StringEquals aws:SourceAccount=<ACCOUNT_ID>` (`iam__scheduler-invocation-role__trust.json`).
- Policy: `lambda:InvokeFunction` on **BOTH** `pest-model-watchdog(:*)` **AND** `pest-camera-scheduler(:*)`. The live console policy only lists watchdog (`iam__scheduler-invocation-role__managed__Amazon-EventBridge-Scheduler-Execution-Policy.json`); **widen it** because per-camera schedules invoke pest-camera-scheduler.
- Use this role's ARN as (a) the watchdog schedule `Target.RoleArn` and (b) the `pest-monitoring-api-role` `iam:PassRole` Resource.

> **Two live name-gaps this fixes:** the brief's `pest-scheduler-invocation-role`
> does NOT exist live (the watchdog schedule uses console role
> `Amazon_EventBridge_Scheduler_LAMBDA_d585792c00`), and `pest-monitoring-api-policy`'s
> `iam:PassRole` currently points at that non-existent role name (latent bug,
> harmless today because only per-camera schedules are exercised). deploy.py must
> create ONE real, stably-named invocation role and point both consumers at it.

**Over-broad grants to optionally tighten (functional as-is):** `dynamodb:*` in
`pest-monitoring-api-policy` (could use the 6-verb list); `AmazonSESFullAccess`
(could be send-only); `AWSBillingReadOnlyAccess` (broad but read-only).
`rekognition:*`/`ses:*`/`kinesisvideo:*` on `Resource:*` are inherent to those APIs.

### 1.2 DynamoDB — 4 PROD tables (all `PAY_PER_REQUEST`, TTL off, PITR off, no streams, TableClass STANDARD, DeletionProtection false; tags `Project=armyworm-v2, Owner=Runzhe-armyworm, ManagedBy=Runzhe`)

| Table | Key schema | GSI | Seed? | Raw files |
|---|---|---|---|---|
| **pest-monitoring-cameras** | `camera_id` (S) HASH | none | **YES — 4 rows** | `dynamodb__pest-monitoring-cameras__{describe-table,continuous-backups,ttl,tags,seed-scan}.json` |
| **pest-monitoring-detections** | `image_id` (S) HASH + `detection_time` (S) RANGE | `by-pest-time`: `pest_type` HASH + `detection_time` RANGE, Projection **ALL** | EMPTY (runtime data) | `dynamodb__pest-monitoring-detections__{describe-table,continuous-backups,ttl,tags}.json` |
| **pest-monitoring-system-config** | `config_key` (S) HASH | none | **YES — 1 row** | `dynamodb__pest-monitoring-system-config__{describe-table,continuous-backups,ttl,tags,seed-scan}.json` |
| **pest-monitoring-schedule-logs** | `log_id` (S) HASH + `timestamp` (S) RANGE | none | EMPTY (runtime data) | `dynamodb__pest-monitoring-schedule-logs__{describe-table,continuous-backups,ttl,tags}.json` |

> **Note:** `ContinuousBackups=ENABLED` in the raw JSON is the always-on free 35-day
> mode, **not** PITR — PITR is DISABLED on all four. Do NOT pass
> `--stream-specification`, do NOT enable TTL/PITR, do NOT pass provisioned throughput.

**system-config seed (1 row):** `config_key='detection_settings'`,
`email_enabled(BOOL)=true`, `recipient_email(S)=<CUSTOMER_EMAIL param>` (live:
`rex2956550768@gmail.com`), `additional_recipients(L)=[]`, `auto_capture(BOOL)=true`,
`capture_interval(N)=60`. Omit live migration artifacts (`migrated_at`, `migrated_from`).

**cameras seed (4 rows)** — omit migration artifacts; seed the two `custom_model_arn`
values as **empty string `''`** in a fresh account (the live ARNs point at this
account's Rekognition IDs, which won't exist until the customer trains):

1. `manual_upload` — label 'Manual test upload', default_waypoint_id=manual_upload, target_label=Person, model_type=general, min_confidence(N)=80, stream_enabled=false, model_running=true, custom_model_arn='', kvs_stream_name=NULL, schedule={}.
2. `person_cam` — label 'Person (Hikvision test)', default_waypoint_id=zone_test, target_label=Person, model_type=general, min_confidence(N)=95, stream_enabled=false, model_running=true, custom_model_arn='', kvs_stream_name=NULL, schedule={}.
3. `moth_cam_01` — label 'Moth (fixed indoor)', default_waypoint_id=zone0_moth_fixed, target_label=Moths, model_type=custom, min_confidence(N)=80, stream_enabled=false, model_running=false, kvs_stream_name='moth-cam-stream', custom_model_arn='' (live: SmartPestProject `.../version/SmartPestProject.2026-02-15T01.13.58/1771089238911`), schedule={enabled:false, days:[], start_time:'09:00', end_time:'17:00', updated_at:'2026-05-04T06:53:40Z'}.
4. `armyworm_go2_a8mini` — label 'Armyworm (Go2 mobile)', default_waypoint_id=NULL, target_label='armyworm-larva', model_type=custom, min_confidence(N)=60, stream_enabled=false, model_running=false, tiling_enabled=true, kvs_stream_name='armyworm-cam-stream', custom_model_arn='' (live: armyworm-detection `.../version/v5-2026-07-07/1783394123547`), schedule={enabled:false, days:[], start_time:'09:00', end_time:'17:00', updated_at:'2026-05-06T05:56:08Z'}.

### 1.3 S3 — 4 PROD armyworm buckets (all bucket names embed the account id — PARAMETERIZE)

> **us-east-1 quirk:** every bucket returned `LocationConstraint=null`. deploy.py
> MUST call `create-bucket` WITHOUT `--create-bucket-configuration/LocationConstraint`
> when region==us-east-1 (passing it errors). Pass it for any other region.

| Bucket (template) | Config | Raw files |
|---|---|---|
| **frames-armyworm-\<ACCT\>** | Versioning ON; SSE-S3 AES256 (BucketKey off); PAB all TRUE; no policy; **CORS** (methods GET/PUT/POST/HEAD, origins = CloudFront domain + dashboard website endpoint + localhost:5500/5501, ExposeHeaders ETag, MaxAge 3000); **event notification** → `pest-detection-processor`, `s3:ObjectCreated:*`, prefix `frames/`, id `b1b84ffd-ffbc-48ab-82a2-1e6e77118d24` | `s3__frames-armyworm__cors.json`, `s3__frames-armyworm__notification.json`, `s3__frames-bucket-notification.json` |
| **processed-images-armyworm-\<ACCT\>** | Versioning ON; SSE-S3 AES256; PAB all TRUE; no policy (private); **CORS** localhost:5500 only; no notification | `s3__processed-images-armyworm__cors.json` |
| **pest-dashboard-\<ACCT\>** | Versioning OFF; SSE-S3 AES256; **PAB all FALSE**; **public-read policy** `PublicReadDashboard` (Principal \*, s3:GetObject on `/*`); **website** Index=index.html Error=index.html (SPA fallback); no CORS. Sync 15 files from `C:\FYP\web\dashboard_v4\` (index.html, styles.css, js/{analytics,api,auth,bbox,config,costs,gallery,live,main,modal,settings,state,utils}.js) | `s3__pest-dashboard__policy.json`, `s3__pest-dashboard__website.json`, `s3__pest-dashboard__public-access-block.json` |
| **lambda-layers-\<ACCT\>** | Versioning OFF; SSE-S3 AES256 (BucketKey ON); PAB all TRUE; holds `ffmpeg-layer.zip` (~59 MB). Layer-source bucket only | (none) |

> **Ordering traps:** frames notification requires the `s3.amazonaws.com`
> AddPermission on the processor FIRST or `PutBucketNotificationConfiguration`
> fails (live has a harmless duplicate allow — emit ONE). pest-dashboard
> `PutBucketPolicy` fails unless PAB is disabled FIRST.

### 1.4 Lambda layer — fyp-pillow (BUILD, do not copy — no source zip in S3)

- LayerArn `.../layer:fyp-pillow`; live latest = **:2** (7,888,247 bytes, CodeSha256 `pbTTMOLxeIFwXFlyRW9z9Aa2PyUWgPqehyvDbuxRM+o=`). :1 is DEAD (was missing the compiled `_imaging*.so` → no bboxes drawn).
- Provides **Pillow 12.2.0, cp312, manylinux2014_x86_64**. Build: `pip install --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 --only-binary=:all: --target ./python Pillow==12.2.0`.
- **VERIFY before zip:** assert `python/PIL/_imaging*.so` and `python/pillow.libs/` exist (the exact :1 bug). Zip `python/` at archive root → `fyp-pillow-12.2.0-py312-x86_64.zip`.
- Publish: `publish-layer-version --layer-name fyp-pillow --compatible-runtimes python3.12 --compatible-architectures x86_64`. Capture returned Version (fresh account = **:1**, not :2 — do NOT hardcode :2). Attach that exact ARN to pest-detection-processor.
- Raw: `lambda__fyp-pillow_list-layer-versions.json`, `lambda__fyp-pillow_wiring.json`.

### 1.5 Lambda — 5 PROD functions

Common: `Runtime python3.12`, `Handler lambda_function.lambda_handler`,
`Architectures [x86_64]`, ephemeral 512 MB, no reserved/provisioned concurrency.
Zip must contain `lambda_function.py` at root — the 3 core mirrors in
`C:\FYP\lambda\` are named `pest-*.py` and **must be renamed to `lambda_function.py`**
inside the zip.

| Function | Mem / Timeout | Role | Layers | Env vars | Trigger | Raw files |
|---|---|---|---|---|---|---|
| **pest-detection-processor** | 512 / 60 | pest-detection-processor-role | fyp-pillow:\<N\> | `SENDER_EMAIL=<CUSTOMER_EMAIL>`, `TABLE_CAMERAS=pest-monitoring-cameras`, `TABLE_SYSTEM_CONFIG=pest-monitoring-system-config`, `S3_PROCESSED_BUCKET=processed-images-armyworm-<ACCT>`, `TABLE_DETECTIONS=pest-monitoring-detections` | S3 frames-armyworm ObjectCreated:\* prefix `frames/` | `lambda__pest-detection-processor.json` |
| **pest-monitoring-api** | 256 / 30 | pest-monitoring-api-role | none | `SCHEDULE_EXECUTOR_ARN=arn:aws:lambda:<REGION>:<ACCT>:function:pest-camera-scheduler`, `TABLE_CAMERAS`, `S3_FRAMES_BUCKET=frames-armyworm-<ACCT>`, `TABLE_SYSTEM_CONFIG`, `TABLE_SCHEDULE_LOGS=pest-monitoring-schedule-logs`, `S3_PROCESSED_BUCKET=processed-images-armyworm-<ACCT>`, `TABLE_DETECTIONS` | HTTP API (all pest routes) | `lambda__pest-monitoring-api.json`, `lambda__dynamic-camera-schedules.spec.json` |
| **pest-camera-scheduler** | 128 / 60 | pest-camera-scheduler-role | none | `TABLE_CAMERAS`, `TABLE_SCHEDULE_LOGS` | NO static trigger (invoked by api via SCHEDULE_EXECUTOR_ARN + by runtime pest-sched-\* rules) | `lambda__pest-camera-scheduler.json` |
| **kvs-hls-handler** | 128 / 30 | kvs-hls-handler-role | none | NONE (region from AWS_REGION default) | HTTP API `GET /video-playback` ONLY | `lambda__kvs-hls-handler.json`, `kvs-hls-handler_src/lambda_function.py`, `kvs-hls-handler.zip` |
| **pest-model-watchdog** | 128 / 30 | pest-model-watchdog-role | none | `TABLE_CAMERAS`, `MAX_RUNTIME_MIN=75` | EventBridge Scheduler `pest-model-watchdog-15min` | `lambda__pest-model-watchdog.json`, `scheduler__pest-model-watchdog-15min.json`, `pest-model-watchdog_src/lambda_function.py`, `pest-model-watchdog.zip` |

> **Two code-mirror gaps closed during this audit:** kvs-hls-handler and
> pest-model-watchdog live code were NOT in `C:\FYP\lambda\` (only the 3 core were).
> Both were downloaded to `deployer/audit/<name>_src/lambda_function.py`. Ideally
> copy them into `C:\FYP\lambda\` so the source of truth is one place.
> pest-model-watchdog docstring says 10min/60min but **live truth is 15min/75min**.
>
> **Dynamic per-camera schedules (runtime, NOT pre-created):** at runtime
> pest-monitoring-api creates EventBridge rules `pest-sched-{camera_id}-{action}`
> (`cron(mm hh ? * DAYS *)` UTC), adds target=pest-camera-scheduler with Input JSON,
> and adds a per-rule `lambda:AddPermission` (StatementId `sched-invoke-<rule>`,
> Principal `events.amazonaws.com`). deploy.py creates the function + role only; the
> api role's events/lambda permissions (§1.1) let it wire these live. See
> `lambda__dynamic-camera-schedules.spec.json` (api source lines 784-842).

### 1.6 Cognito — 1 pool, 1 app client (both PROD)

- **Pool** `pest-dashboard-users` (live `us-east-1_ea0aJdusl`): username attr = email, auto-verified email, MFA OFF, `AllowAdminCreateUserOnly=true` (no self sign-up), `EmailSendingAccount=COGNITO_DEFAULT` (no SES), no Lambda triggers, no hosted domain, no OAuth, account-recovery verified_email(1)/verified_phone(2), PasswordPolicy MinLength 8 / lower+numbers required. Raw: `cognito__user-pool_us-east-1_ea0aJdusl.json`.
- **App client** `dashboard-web` (live `4husu6afr835e235eu9dqp8av6`): public SPA (`--no-generate-secret`), auth flows `ALLOW_USER_PASSWORD_AUTH` + `ALLOW_REFRESH_TOKEN_AUTH`, id/access validity 12 h, refresh 30 days, token revocation ON, auth-session validity 3, no callback/logout URLs, OAuth off. Raw: `cognito__user-pool-client_dashboard-web.json`.
- **Users:** seed ZERO. Live pool is admin-create-only (2 real users, NOT dumped — customer data). Customer admin-creates their own accounts.
- **Write-back:** only `COGNITO_REGION` and `COGNITO_CLIENT_ID` land in `web/dashboard_v4/js/config.js` (the pool id is NOT referenced by the dashboard, which uses direct USER_PASSWORD_AUTH via the client id). Client id is a public identifier — safe to commit.

### 1.7 API Gateway — HTTP API v2 `pest-monitoring-api-gateway` (live `zwpcbivmsj`, PROD)

- Protocol HTTP, single auto-deploy `$default` stage, `DisableExecuteApiEndpoint=false`.
- **CORS:** AllowOrigins `*`, AllowMethods `GET,POST,DELETE,OPTIONS`, AllowHeaders `content-type,authorization`, MaxAge 3600.
- **JWT authorizer** `cognito-dashboard` (live id `enxa26`): IdentitySource `$request.header.Authorization`, Issuer `https://cognito-idp.<REGION>.amazonaws.com/<NEW_POOL_ID>`, Audience `<NEW_CLIENT_ID>`.
- **2 integrations** (both AWS_PROXY, POST, PayloadFormatVersion 2.0, TimeoutInMillis 30000, ConnectionType INTERNET): `INT_API` → pest-monitoring-api (live `t7ggvzn`); `INT_HLS` → kvs-hls-handler (live `33f69kt`).
- **21 routes** — all require the JWT authorizer EXCEPT the single open `GET /stream/status` (Authorization NONE). All target `INT_API` except `GET /video-playback` → `INT_HLS`:
  `GET /cost`, `DELETE /identities`, `DELETE /schedule`, `POST /stream/start`, `GET /presigned-url`, `POST /detection/verify`, `GET /video-playback` [INT_HLS], `GET /settings`, `GET /stream/status` [NONE], `DELETE /detection`, `POST /stream/stop`, `POST /settings`, `POST /model/stop`, `GET /identities`, `GET /schedule-logs`, `POST /model/start`, `POST /schedule`, `GET /history`, `POST /identities`, `GET /schedule`, `GET /model/status`.
- **2 Lambda invoke perms:** pest-monitoring-api = broad SourceArn `<apiId>/*`; kvs-hls-handler = route-scoped `<apiId>/*/*/video-playback`.
- Base URL (no stage suffix, `$default`): `https://<NEW_API_ID>.execute-api.<REGION>.amazonaws.com` → write into dashboard `config.js` (`HTTP_API`).
- Raw: `apigateway__httpapi-zwpcbivmsj-{get-api,get-authorizers,get-integrations,get-routes,get-stages,lambda-invoke-permissions}.json`.

### 1.8 CloudFront — distribution `E1423RGLAXWNSI` → d1twcdquexdgj8.cloudfront.net (PROD)

- Comment 'Pest dashboard v4 frontend (S3 website origin)'. Enabled, HTTP/2, IPv6 on, PriceClass_All, DefaultRootObject index.html.
- **Origin** (1): id `s3-website-pest-dashboard`, DomainName = **S3 static-website endpoint** `<DASHBOARD_BUCKET>.s3-website-<REGION>.amazonaws.com` (NOT the REST endpoint; hyphen form for us-east-1), CustomOriginConfig `OriginProtocolPolicy=http-only` (website endpoints have no HTTPS — https → 502), TLSv1.2, no OAC/OAI.
- **Default behavior:** TargetOriginId `s3-website-pest-dashboard`, ViewerProtocolPolicy `redirect-to-https`, Allowed+Cached methods `[HEAD,GET]`, Compress true, CachePolicyId `4135ea2d-6df8-44a3-9df3-4b5a84be39ad` (AWS-managed **Managed-CachingDisabled** — same id in every account, hardcode it), no OriginRequest/Response policy, no Lambda@Edge/Functions.
- **NO CloudFront custom error responses** — SPA 404 fallback is the S3 website `ErrorDocument=index.html`, not CloudFront.
- **ViewerCertificate:** CloudFrontDefaultCertificate=true (no ACM, no aliases, no custom domain). No WAF, no logging, no tags.
- CallerReference must be fresh per run (live: `pest-dashboard-cf-2026-07-06` — CloudFront rejects duplicates). Distribution takes minutes to reach `Deployed`; capture the new Id + `*.cloudfront.net` domain and write back into dashboard config + into the frames-bucket CORS AllowedOrigins.
- Raw: `cloudfront__E1423RGLAXWNSI_distribution-config.json`, `cloudfront__E1423RGLAXWNSI_origin-s3-website-evidence.json`.

### 1.9 EventBridge Scheduler — 1 PROD schedule

- **pest-model-watchdog-15min** (group `default`): `rate(15 minutes)`, timezone `Asia/Singapore` (cosmetic for rate()), State ENABLED, FlexibleTimeWindow OFF, ActionAfterCompletion NONE, Target = pest-model-watchdog ARN (no Input), RetryPolicy `MaximumEventAgeInSeconds=86400 / MaximumRetryAttempts=0`, `Target.RoleArn` = the scheduler invocation role from §1.1 (do NOT reuse the live console role `Amazon_EventBridge_Scheduler_LAMBDA_d585792c00`).
- Mechanism: EventBridge Scheduler invokes Lambda by **assuming a role**, NOT via `lambda add-permission` (unlike classic EventBridge rules). ZERO classic EventBridge rules and no custom event buses exist in the account.
- Raw: `scheduler__pest-model-watchdog-15min.json`, `scheduler__list-schedules.json`.

### 1.10 Kinesis Video Streams — 2 PROD streams (live-view ingest)

- **armyworm-cam-stream** and **moth-cam-stream**: `create-stream --stream-name <name> --data-retention-in-hours 2`. AWS-managed KMS `alias/aws/kinesisvideo` (no CMK). Names carry no account id — keep literal.
- The HLS `DataEndpoint` (`b-xxxx.kinesisvideo...`) is per-account/per-stream — kvs-hls-handler resolves it at runtime via `get-data-endpoint`; never hardcode.
- Raw: `kvs__streams.json`.

### 1.11 SES — sender/recipient email identity (PROD, sandbox)

- Path uses **SES only** — no SNS, no SQS, no SES configuration sets (all confirmed empty).
- Live account is in the **SES sandbox** (`ProductionAccessEnabled=false`, 200 msgs/24h, 1/sec). A fresh account also starts in sandbox: can only send to/from verified addresses. Exiting sandbox is a MANUAL AWS Support production-access request (~1–2 business days) deploy.py CANNOT automate.
- Live sender==recipient = one verified identity `rex2956550768@gmail.com` (processor env `SENDER_EMAIL` AND system-config `recipient_email`).
- deploy.py wizard: prompt for sender + recipient emails → `verify-email-identity` each (sends a confirmation email; pause + poll `get-identity-verification-attributes` until Success) → write sender into processor env, recipient into system-config row → warn about sandbox + print the production-access request path (`aws sesv2 put-account-details --production-access-enabled --mail-type TRANSACTIONAL ...`).
- Raw: `ses__account.json`, `ses__identities.json`, `ses__messaging-wiring.json`.

### 1.12 Rekognition Custom Labels — project skeleton only (model CANNOT be migrated)

- **HARD CONSTRAINT:** trained Custom Labels models are account-bound — no export/import/copy/cross-account API. deploy.py CANNOT ship the trained v5 model.
- **CAN automate:** `create-project --project-name armyworm-detection` (capture returned ProjectArn — numeric suffix is AWS-assigned, do NOT hardcode live `.../1779334594920`); optionally `create-dataset` TRAIN + TEST (empty or from a customer GroundTruth manifest).
- **Customer must (guided/manual):** supply labeled `armyworm-larva` object-detection images and run `train-project-version` (~3.5 h billable), then `StartProjectVersion` (MinInferenceUnits 1).
- **Write-back AFTER training:** the resulting ProjectVersionArn goes into the cameras seed `custom_model_arn` (and/or Lambda config) — treat as post-training config, not a deploy-time constant.
- Label class name constant: `armyworm-larva`. Training-output bucket = `frames-armyworm-<ACCT>`, S3KeyPrefix `training-output/v5`.
- Do NOT recreate SmartPestProject (moth, DEMO reference) or the DEAD v1 model.
- Raw: `rekognition__armyworm-detection__project-versions.json`, `rekognition__SmartPestProject__project-versions.json`, `rekognition__projects-summary.json`.

### 1.13 CloudWatch — create nothing

- Log groups `/aws/lambda/{pest-detection-processor,pest-monitoring-api,kvs-hls-handler,pest-model-watchdog}` are AUTO-created on first invoke (retention Never-Expire, no metric filters). No alarms exist. deploy.py may optionally set retention for cost, but that diverges from live. Raw: `cloudwatch__log-groups.json`.

---

## 2. Creation ORDER for deploy.py

1. **IAM** — 5 Lambda execution roles (from §1.1 policy docs, account-parameterized) + 1 scheduler invocation role. Wait/retry for IAM eventual consistency before first attach and before any PassRole-consuming resource. Rewrite the api-role `iam:PassRole` Resource to the REAL invocation-role ARN.
2. **DynamoDB** — create the 4 tables (detections with the `by-pest-time` GSI). Do NOT seed yet if model ARNs are wanted (they stay `''` regardless).
3. **S3** — create 4 armyworm buckets (us-east-1: no LocationConstraint). Versioning/SSE/PAB per §1.3. Defer the frames notification (needs the processor + its permission first). pest-dashboard: PAB-false → website → policy.
4. **Lambda layer** — build + publish fyp-pillow, capture the versioned ARN.
5. **Lambda functions** — create all 5 (rename core mirrors to `lambda_function.py`; kvs-hls-handler + watchdog from the audit `_src`). Attach fyp-pillow:\<N\> to the processor. 2-phase env: create pest-camera-scheduler first, then set `SCHEDULE_EXECUTOR_ARN` on pest-monitoring-api.
6. **S3 notification wiring** — AddPermission (`s3.amazonaws.com`, SourceArn=frames bucket, SourceAccount) on the processor, THEN `PutBucketNotificationConfiguration` (prefix `frames/`). Emit ONE statement.
7. **Cognito** — create pool `pest-dashboard-users` → app client `dashboard-web` (idempotent: match on Name first). Capture NEW_POOL_ID + NEW_CLIENT_ID.
8. **API Gateway** — create-api → create-authorizer (Issuer/Audience from Cognito ids) → 2 integrations → 21 routes → `$default` auto-deploy stage → 2 lambda AddPermission (api-wide + route-scoped). Capture NEW_API_ID.
9. **CloudFront** — after dashboard bucket has website + public-read: create-distribution from the saved config with fresh CallerReference, parameterized website-endpoint origin, hardcoded CachingDisabled policy id. Capture new Id + domain.
10. **Config write-back** — rewrite `web/dashboard_v4/js/config.js` (`HTTP_API`=new API base URL, `COGNITO_REGION`, `COGNITO_CLIENT_ID`); update frames-bucket CORS AllowedOrigins with the new CloudFront domain + dashboard website endpoint; then `aws s3 sync C:\FYP\web\dashboard_v4\ s3://pest-dashboard-<ACCT>/`.
11. **KVS** — create armyworm-cam-stream + moth-cam-stream (retention 2h).
12. **EventBridge Scheduler** — create `pest-model-watchdog-15min` targeting the watchdog + scheduler invocation role.
13. **Rekognition** — create project `armyworm-detection` (+ optional empty datasets); THEN pause for the guided training/StartProjectVersion step; THEN write the ProjectVersionArn back into the cameras seed / config.
14. **DynamoDB seed** — write system-config (1 row) + cameras (4 rows, model ARNs `''` until step 13 resolves them).
15. **SES** — verify sender + recipient identities; write into processor env + system-config; print the production-access request instructions.

---

## 3. PARAMETERS deploy.py must template

| Parameter | Live value | Where it appears |
|---|---|---|
| `ACCOUNT_ID` | 366356442579 | every ARN; embedded in bucket names `frames-armyworm-<ACCT>`, `processed-images-armyworm-<ACCT>`, `pest-dashboard-<ACCT>`, `lambda-layers-<ACCT>`; role trust `aws:SourceAccount`; S3 SourceAccount; DDB TableArns; seed model ARNs |
| `REGION` | us-east-1 | every ARN; S3 website endpoint form `s3-website-<REGION>`; Cognito authorizer Issuer; SES verification region; KVS/scheduler ARNs |
| `NEW_POOL_ID` / `NEW_CLIENT_ID` | us-east-1_ea0aJdusl / 4husu6afr835e235eu9dqp8av6 | Cognito-generated → feed API authorizer Issuer/Audience + config.js (client id only) |
| `NEW_API_ID` | zwpcbivmsj | server-assigned → dashboard `HTTP_API` base URL + both lambda invoke SourceArns |
| Server-assigned ids (capture, never reuse) | Authorizer `enxa26`, integrations `t7ggvzn`/`33f69kt`, all RouteIds, DeploymentId `fa82rj`, CloudFront Id `E1423RGLAXWNSI`+domain `d1twcdquexdgj8`, Rekognition project/version/dataset numeric suffixes, layer version number | recreated fresh each run |
| `SENDER_EMAIL` | rex2956550768@gmail.com | processor env; must be a wizard input (customer's SES-verified sender) |
| `RECIPIENT_EMAIL` | rex2956550768@gmail.com | system-config `recipient_email`; wizard input |
| Rekognition ProjectVersionArn(s) | armyworm `v5-2026-07-07/1783394123547`; moth `.../1771089238911` | post-training write-back into cameras seed / Lambda config — resolve at runtime, never hardcode |
| CloudFront `CallerReference` | pest-dashboard-cf-2026-07-06 | must be unique per create-distribution run |
| Layer version `:N` | fyp-pillow:2 | read from publish response (fresh account = :1) |
| Tag values (optional genericize) | Project=armyworm-v2, Owner=Runzhe-armyworm, ManagedBy=Runzhe | DDB tags |
| Hardcode (account-independent) | Managed-CachingDisabled id `4135ea2d-6df8-44a3-9df3-4b5a84be39ad`; CloudFront origin logical id `s3-website-pest-dashboard`; layer name `fyp-pillow`; table names; KVS stream names; label class `armyworm-larva` | — |

---

## 4. DEAD / NOT_PEST — do NOT recreate (listed for a complete picture)

**Lambda (11):** Gen-1/Wilbur — `image-upload-handler`, `pest-detection-http`,
`FrameExtractionControl`, `websocket-handler`, `kvs-hls-url-generator` (py3.11),
`ImageProcessing`, `pest-model-control`, `Scheduling`, `Extraction`,
`ses-identity-manager`. NOT_PEST — `lightsOutFunc` (py3.14, lights-out project).
_(`pest-model-control`/`Extraction` are still wired to ENABLED Gen-1 schedules but belong to the legacy pipeline.)_

**API Gateway:** REST `3go4jj1698` (PestDetectionAPI — **already gone**,
NotFoundException); WebSocket `j4v2m5cbte` (PestMonitoringWebSocket — dropped for
polling in v3.7).

**DynamoDB:** `pest-monitoring-config` (Gen-1 config, PK `config_id`, only in archived
Flutter — resolves the old `[?]`); `websocket-connections` (retired WebSocket store).

**S3:** `streaming-buckets` (Gen-1, wired to ImageProcessing); `custom-labels-console-us-east-1-d1abc2aed2`
(Rekognition-managed — auto-created with its own random suffix by the Custom Labels
console when the project/dataset is created; NOT hand-created, training images go in
via the create-dataset flow); `processed-images-moth` (moth pipeline, PROD-but-moth —
see Open Decisions; note NO account suffix → global-name collision risk, rename to
`processed-images-moth-<ACCT>` if kept).

**EventBridge Scheduler (Gen-1):** `model-start-schedule` (cron 05:53),
`model-stop-schedule` (cron 06:01), `frame-extraction-schedule` (cron 06:00) — all
still ENABLED and firing daily against dead Lambdas (**wasted spend — recommend
disabling in the source account**); `ExtractFrameEveryMinute` (DISABLED). Shared Gen-1
scheduler role `Amazon_EventBridge_Scheduler_LAMBDA_4c3f0b5d17`.

**KVS:** `FYP-PROJECT` (Gen-1, 168h retention).

**SES identities:** `teowilbur@gmail.com` (Wilbur, DEAD), `neobkee@gmail.com` (NOT_PEST).

**Rekognition:** SmartPestProject (moth, DEMO reference), armyworm v1 (DEAD, F1 0.719).

**Empty/absent (nothing to create):** SNS, SQS, SES config sets, Secrets Manager, SSM
Parameter Store, Amplify (us-east-1), WAFv2 (both scopes), ACM, Route53, classic
EventBridge rules/custom buses, KMS CMKs.

**EC2 `MonitoringSystem` (i-0750c917a1f038d4d, running t3.micro):** UNKNOWN — likely
the older/alternate "EC2 + nginx" dashboard host, but the live dashboard is served from
S3 + CloudFront. Confirm before deciding whether deploy.py provisions any EC2 host (see
Open Decisions).

**ffmpeg-for-frames layer + `lambda-layers-<ACCT>/ffmpeg-layer.zip` (~59 MB):** the
zip must ship with the deployer if the ffmpeg layer is in scope. The layer is NOT
attached to any of the 5 Gen-2 core Lambdas — resolve its PROD/DEAD status (Gen-1
frame-extraction vs kvs-hls live-view) before recreating.

---

## 5. OPEN DECISIONS (block or scope deploy.py)

1. **Scheduled auto-capture in scope for Gen-2?** The Gen-1 daily cycle (model-start
   05:53 → frame-extraction 06:00 → model-stop 06:01 SGT, 7 days) is still ENABLED on
   dead Lambdas. If YES, deploy.py rebuilds ONE auto-capture schedule (or start/stop
   pair) on **pest-camera-scheduler**, porting the cron windows + the frame-extraction
   Input payload (`rate_value:30, rate_unit:minutes, start_time/end_time/days`), with a
   fresh scheduler role scoped to pest-camera-scheduler. If NO, drop the whole set. The
   Gen-2 watchdog already covers model-stop, so the Gen-1 stop schedule is redundant.
2. **Is the moth pipeline in customer scope, or armyworm-only?** Decides whether
   deploy.py provisions `processed-images-moth-<ACCT>` + a moth Rekognition project +
   the moth-cam-stream+moth_cam_01 seed row, or ships armyworm-only.
3. **Dashboard image fetch path:** does the deployed dashboard fetch annotated images
   directly from processed-images-armyworm (needs CloudFront/website CORS origins added
   — currently localhost-only) or exclusively via the API/presigned URLs (current
   localhost-only CORS is fine)?
4. **EC2 `MonitoringSystem` — still serving, or fully superseded by S3+CloudFront?**
   Determines whether deploy.py provisions an EC2 host at all.
5. **Cognito hardening:** live client leaves `PreventUserExistenceErrors` default
   (LEGACY). Faithful clone keeps default; deploy.py could optionally set ENABLED.
6. **New-model default state:** should deploy.py auto-start the freshly trained
   Rekognition model, or leave it STOPPED (the watchdog stops it when idle; cost)?
7. **Runtime scheduler role at runtime:** confirm against pest-monitoring-api source
   how per-camera schedules currently obtain a valid `Target.RoleArn` (the named
   `pest-scheduler-invocation-role` does not exist live), so deploy.py locks in the
   right role name before wiring PassRole.
8. **kvs-hls-url-generator (role `kvs-hls-url-generator-role-013j8khq`):** api-role
   explicitly grants InvokeFunction on it — confirm whether it is Gen-2 live-view PROD
   (and should be in the deployer) or Gen-1 DEAD before finalizing the api-role policy.
