# Chapter 7 — ARGUS one-click deployer

This chapter documents the ARGUS desktop deployer: a single Windows `.exe` that stands up the complete detection stack in a customer's own AWS account, trains their model from a local YOLO folder, and can audit or destroy the whole deployment.

_As of 2026-08-11._

## 7.1 Role in the system

Everything in Chapters 2–4 was built by hand in the development/reference account `366356442579`. ARGUS is the productized replay of that work. It takes a fresh AWS account and recreates the whole cloud stack — 6 IAM roles, 4 DynamoDB tables, 3 S3 buckets, a Lambda layer, 5 Lambdas, Cognito, the HTTP API with 21 routes, CloudFront, the cost-guard schedule, an empty Rekognition Custom Labels project, and SES identities — in a single unattended run, with live progress in a desktop window. The run is fast: the measured production figure is **103 seconds for all 15 stages** (2026-08-10); CloudFront's global propagation continues for a few minutes after the run itself completes.

That figure is not a projection. On 2026-08-10 ARGUS performed the project's real account migration: a headless CLI invocation of the stage engine (`python deploy.py --profile prod --prefix argus --target-label armyworm-larva ...`) stood up the production stack in the NP production account `506868652945` — **15 of 15 stages, 103 seconds, zero errors, first attempt, no resume needed**. The KVS stage was skipped by design because `--live-view` was not passed. The resulting stack is live: dashboard `https://d1dtoxef7qmugl.cloudfront.net` (CloudFront `E1YADURLSAVNFA`), API `vzfl7s6z00`, Cognito pool `us-east-1_9selFDHpc` with client `6vebotf45bp8u46cnraddiaplv`, Rekognition project `argus-detection`, and buckets `argus-frames-506868652945` / `argus-processed-506868652945` / `argus-dashboard-506868652945`.

The system therefore now spans two accounts. `366356442579` is the development/reference account where the system was built and validated. `506868652945` is the NP production account being stood up: the ARGUS stack is deployed and verified, the training data has been copied server-side, both models have been retrained there (moth `moth-prod-20260811` F1 0.991; armyworm `v9r-prod-20260810` F1 0.613) and wired into the camera rows, and the SES sender address is verified; KVS / live view is not migrated.

ARGUS is deliberately a generic product, not an armyworm tool. Nothing armyworm-specific ships (product scope ruling, 2026-07-08). No dataset migrates. The detection target is a per-camera `target_label` string the customer chooses in the wizard. The customer brings their own labeled images and trains their own model through the in-app training pipeline. Pest monitoring at Jewel Changi is the first instance of the product, not the product itself.

The tool has three engines behind one shell. `app.py` is the desktop shell and the JS-to-Python bridge. `deploy.py` is the stage engine that creates, verifies, and destroys cloud resources. `training.py` is the pipeline that turns a local YOLO folder into a trained, wired-in Rekognition Custom Labels model. All three run from source (`python app.py`) or frozen into `dist\ARGUS.exe` by `build.ps1`.

## 7.2 Inventory

| Item | Location | Purpose |
|---|---|---|
| `app.py` | `deployer/app.py` | pywebview (Edge WebView2) desktop shell + the `Api` JS-Python bridge |
| `deploy.py` | `deployer/deploy.py` | 15-stage idempotent stack deployer; also `verify()` audit and `destroy()` teardown; runnable standalone from the CLI |
| `training.py` | `deployer/training.py` | YOLO folder -> Rekognition Custom Labels training pipeline, watch, wire-in, rollback |
| `web/index.html` | `deployer/web/index.html` | The ARGUS UI. Self-contained (inline CSS/JS, no CDNs). UI is frozen — do not restyle |
| `audit/` | `deployer/audit/` | Raw per-resource config JSONs audited from the live reference account; `deploy.py` consumes them with account/region/bucket rewrites |
| `STACK_MANIFEST.md` | `deployer/STACK_MANIFEST.md` | The recreate-level bill of materials the deployer was built from |
| `layer/fyp-pillow.zip` | `deployer/layer/fyp-pillow.zip` | Prebuilt Pillow 12.2.0 Lambda layer (manylinux2014, cp312) — required by the frozen exe |
| `legal/` | `deployer/legal/` | Terms of Use + Privacy Policy shown on the consent screen |
| `build.ps1` | `deployer/build.ps1` | PyInstaller onefile packaging script -> `dist\ARGUS.exe` |
| `build_installer.ps1` | `deployer/build_installer.ps1` | Wraps `dist\ARGUS.exe` into a Windows installer via Inno Setup 6 -> `dist\installer\ARGUS-Setup-<version>.exe` |
| `installer/argus.iss` | `deployer/installer/argus.iss` | Inno Setup script: Program Files install, shortcuts, uninstaller, silent WebView2 runtime install from the bundled bootstrapper (`installer\redist\MicrosoftEdgeWebView2Setup.exe`) |
| `requirements.txt` | `deployer/requirements.txt` | boto3, pywebview, keyring, Pillow — runtime deps for source runs; baked into the exe by `build.ps1` (pyinstaller itself is build-time only) |
| `REHEARSAL.md` | `deployer/REHEARSAL.md` | The two-round deployment rehearsal protocol |
| `README.md` | `deployer/README.md` | Component map, the one-stop rule, packaging notes |
| Design doc | `docs/deployer_training_pipeline.md` | Training pipeline spec (2026-07-16); code is ground truth where they differ |
| State file | `%LOCALAPPDATA%\ARGUS\out\deploy_state.json` (frozen) or `deployer/out/deploy_state.json` (dev) | Every generated id; drives resume, verify, destroy, training |
| WebView2 profile | `%LOCALAPPDATA%\ARGUS\webview_profile\` | Persistent browser profile for the embedded windows |
| Stored credentials | Windows Credential Manager, service name `ARGUS-deployer` | The customer's IAM access key, DPAPI-encrypted via `keyring` |

## 7.3 app.py — the desktop shell and the Api bridge

### Purpose

One window, one exe. `app.py` creates a pywebview window on the Edge WebView2 backend (`gui="edgechromium"`), loads `web/index.html`, and exposes a Python class `Api` to the page as `window.pywebview.api`. The UI calls bridge methods; long jobs run in background threads and stream events back by calling `window.evaluate_js('ARGUS.onDeployEvent({...})')`.

The `js_api=api` argument in `webview.create_window` is what makes the bridge exist. It was missing until 2026-07-15, so the shipped exe silently fell back to the UI's simulated preview mode with every button dead. Do not remove it.

### The trust boundary

Enforced in code, not just claimed:

- Card numbers and AWS passwords are typed into AWS's own pages, rendered in a separate embedded WebView2 window (`open_aws_pane`). That window has no JS bridge and no injected script. ARGUS never reads its DOM. It observes only the navigation URL (the `loaded` event) to advance the guidance rail in the main window. This is the same pattern Stripe and Plaid use for onboarding.
- The IAM access key the customer pastes is validated against STS and then stored through `keyring`, which on Windows means the Windows Credential Manager (DPAPI encryption). Never plaintext, never logged, never echoed back to the UI. The UI only ever sees the account id and ARN.
- Root account keys are refused outright (see `verify_credentials` below).

### Exposed bridge methods

Every public method on `Api` is callable from the page as `window.pywebview.api.<name>(...)`. All return JSON-serializable dicts.

**Credentials**

- `verify_credentials(access_key_id, secret_access_key)` — validates the pasted key with `sts:GetCallerIdentity`. Refusals and checks, in order:
  1. STS failure returns a friendly error (`_friendly_aws_error` maps `InvalidClientTokenId` / `SignatureDoesNotMatch` to "copy/paste slip", `AccessDenied` to "use an administrator key").
  2. Root-key refusal: if the caller ARN ends in `:root`, the key is rejected before anything is stored. Root keys cannot be permission-scoped or safely rotated; the keys screen exists to teach the IAM-user alternative.
  3. AdministratorAccess preflight (added 2026-07-15): a bare IAM user passes STS but fails every deploy stage with AccessDenied eight stages in. If the ARN contains `:user/`, the method lists the user's attached policies, and if `AdministratorAccess` is not directly attached it walks the user's groups (`list_groups_for_user` -> `list_attached_group_policies`) to catch inherited grants. Result `admin` is `True` (found), `False` (definitely missing), or `None` (key lacks IAM read — shown as a warning, not a block).
  4. On success the key pair is stored in the Credential Manager under service `ARGUS-deployer` (keys `aws_access_key_id`, `aws_secret_access_key`). If `keyring` is absent (dev only) it degrades to an in-memory store.
  Returns `{ok, account, arn, admin}`. The secret never appears in the return value.
- `has_credentials()` — true if a key pair is already stored; lets the UI mark the keys step done on relaunch.
- `forget_credentials()` — deletes both entries from the credential store.
- `account_alive()` — STS liveness probe. Distinguishes auth-dead (`InvalidClientTokenId`, `UnrecognizedClientException`, `SignatureDoesNotMatch`, `ExpiredToken` -> account closed or key revoked, the UI offers a fresh start) from network errors (must never be mistaken for a closed account).

**Embedded AWS window**

- `open_aws_pane(which)` — opens AWS's own page in the separate embedded window (reuses the window if already open). Targets: `signup` (portal.aws.amazon.com/billing/signup), `console`, `iam-keys` (the IAM **create-user** page, deliberately not `#/security_credentials` — that page is the signed-in root user's own credentials and nudges toward creating root keys, the exact thing the product forbids; fixed 2026-07-15), `ses` (verified identities), `rekognition` (Custom Labels projects), `close-account` (billing account page — account closure is root-plus-console-only; AWS provides no API for standalone accounts, so ARGUS guides and never automates it, and root never touches the product).
- `close_aws_pane()` — destroys the embedded window.

**Deployment**

- `preflight(cfg)` — cheap checks before deploying: credentials on file, prefix matches `[a-z][a-z0-9-]{1,20}`, sender email present.
- `start_deploy(cfg)` — refuses if a deploy or a training run is active, runs `preflight`, then launches `_deploy_worker` in a daemon thread and returns immediately. The worker builds a `deploy.Ctx.from_params(...)` from the stored key and the wizard config, calls `deploy.set_emitter(self._push)`, then `deploy.run_plan(ctx, list(deploy.STAGES))`. Progress arrives in the UI as `stage` / `log` / `done` / `error` events.
- `deployment_status()` — reads `deploy_state.json` and reports `none` (never deployed), `partial` (started, no completion stamp — crashed or quit mid-run), or `complete` (stamp present), plus the last stage, dashboard URL, and API URL. Drives the "Resume deployment" offer and the jump to the done screen.
- `verify_deployment(cfg)` — runs `deploy.verify()` with the stored key. The state file, not the UI form, is the source of truth for region and prefix; otherwise a healthy **ap-southeast-1** stack gets audited in us-east-1 and reads as broken. Older state files without `deploy:region` recover the region from the saved API Gateway URL.
- `destroy_deployment(confirm_text)` — full teardown. Guards: refuses while a deploy runs; refuses while a training run is active (destroy would delete the Rekognition project under the trainer, and the training thread's next whole-dict state save would resurrect the archived state file); requires the typed confirmation to equal the deployment prefix exactly. Then runs `deploy.destroy(ctx)` in a thread, and afterwards re-runs `deploy.verify(ctx)` as proof of absence — success now means 0 resources remain (`destroy_verified` event with any leftovers).

**Dashboard accounts (Cognito CRUD, 2026-07-16)**

- `list_dashboard_users()` — lists users in the deployed pool.
- `create_dashboard_user(email, password)` — `admin_create_user` with `MessageAction="SUPPRESS"` followed by `admin_set_user_password(..., Permanent=True)`. The permanent password matters: the dashboard login intentionally does not handle the `FORCE_CHANGE_PASSWORD` state.
- `delete_dashboard_user(email)` — `admin_delete_user`.

All three build their session via `_mgmt_session()`, which reads the deployed region from the state file (same source-of-truth rule as verify).

**Training bridges** (all drive `training.py`; deterministic automation only — the 2026-07-16 architecture ruling: no LLM ever holds credentials or mutates cloud resources)

- `pick_training_folder()` — native folder picker (`create_file_dialog(FOLDER_DIALOG)`); the customer never types a path.
- `analyze_training_folder(path)` — local fail-fast dataset report; no AWS, no upload.
- `plan_training(report, class_id)` — train/test division and cost estimate for the cost gate.
- `start_training(params)` — claims the `_training` flag atomically under `_flag_lock` before any slow work (the STS call inside `Ctx.from_params` is slow enough for a double-click to race and launch two pipelines), then runs `training.run_training` in a thread. Refuses while a deployment runs.
- `watch_training()` — re-attaches a watcher to an in-flight run after a relaunch or crash; if training finished while the app was closed, the wire-in completes instantly.
- `training_status()` — `none` / `preparing` (local pipeline running in-process, nothing submitted to AWS yet) / `in_progress` / `needs_wire` / `trained`, plus active F1, rollback availability, and last failure. Answers from local state unless a run is pending.
- `rollback_model()` — swaps every custom camera row back to the previous model ARN.

**Lifecycle and misc**

- `on_closing()` — window-close guard. Once training is submitted (`train:pending` on disk) closing is safe; the cloud continues alone. During the local convert/upload/datasets phases a close hard-kills the run, so the first close attempt is blocked with an explanation; a second close in the same session is honored.
- `quit()` — decline-and-exit. Repeats the `on_closing` guard (window `destroy()` bypasses the closing event), closes the AWS pane, destroys the window.
- `legal_text(which)` — returns the bundled Terms of Use / Privacy Policy markdown.
- `open_external(url)` — opens the user's real browser; used only for the final dashboard URL.

### WebView2 profile persistence

`webview.start(...)` pins the WebView2 profile to `%LOCALAPPDATA%\ARGUS\webview_profile` via `storage_path`, with `private_mode=False` (required for the profile to be written at all; added 2026-07-15). Effects: the embedded AWS window keeps the customer's console session across relaunches (AWS's ~12 h session window), and the wizard's own localStorage resume state persists. The profile is always per-user under `%LOCALAPPDATA%`, never inside the project tree: it holds console session cookies, autofill and history, and in dev runs a project-tree location would ship the customer's browser profile inside any later zip or handoff of the folder.

### Known gotchas

- Without `keyring` installed the credential store degrades to in-memory (lost on exit). The packaged build always includes it (`--hidden-import keyring.backends.Windows`).
- `run_plan` already emits the `error` event itself; the deploy worker must not push it again (doing so printed every failure twice in the feed).
- The `Api` methods return error dicts, not exceptions — the UI renders `{ok: false, error}` directly.

## 7.4 deploy.py — the stage engine

### Purpose

Boto3 orchestration of 15 idempotent stages that recreate the whole stack in dependency order in a fresh account. Source of truth: `STACK_MANIFEST.md` plus the raw config JSONs in `audit/`, audited from the live reference account. Runnable standalone from the CLI or driven by `app.py`.

### Context and plumbing

- `Ctx` — the resolved deployment context: a boto3 session, the caller account (from STS), region, prefix, target label, emails, and the state dict. Built from a named AWS profile (`Ctx(args)`, CLI path) or from a pasted key (`Ctx.from_params(...)`, app path — same post-conditions).
- Naming: buckets are `{prefix}-frames-{account}`, `{prefix}-processed-{account}`, `{prefix}-dashboard-{account}`. Table, Lambda, and role names are fixed (`pest-*`) — the prefix parameterizes only the globally-namespaced resources.
- `ctx.rewrite(text)` / `ctx.load_audit_json(name)` — rewrite the reference-account literals (`366356442579`, `us-east-1`, the three reference bucket names `frames-armyworm-366356442579`, `processed-images-armyworm-366356442579`, `pest-dashboard-366356442579`) into this deployment's values before applying an audited policy document.
- `ctx.save(key, value)` — writes the whole state dict to `deploy_state.json` after every mutation. When frozen, state lives in `%LOCALAPPDATA%\ARGUS\out` (the PyInstaller `_MEIPASS` temp dir is wiped on exit); in dev it is `deployer/out/`.
- `set_emitter(fn)` / `_emit(kind, **payload)` — optional event sink so the desktop app can stream progress. Kinds: `log`, `stage`, `done`, `error`, plus `destroy`, `destroy_done`, `destroy_verified` from teardown.
- `STAGE_LABELS` — human-facing stage names for the UI theater (e.g. `iam` -> "Identity & permissions", `rekognition` -> "Vision engine").
- `run_plan(ctx, plan)` — runs a list of `(name, fn)` stages with structured events per stage. Before the first stage it pops any previous `deploy:completed_at` and `deploy:last_stage` stamps — otherwise a second deployment that dies mid-run still reads as complete and the app resumes to the wrong deployment's summary — and records `deploy:started_at`, `deploy:region`, `deploy:prefix`, `deploy:account`. After each stage it stamps `deploy:last_stage`; after all stages, `deploy:completed_at`. Returns `(ok, error_message)`.
- `wait_for(fn, desc)` — polls for IAM eventual consistency and similar; `already_exists(err)` maps the family of "already there" error codes that make every stage adoptive.

### The three deploy-breaking gaps (found and fixed 2026-08-10)

A pre-migration audit of `deploy.py` against the validated production configuration found three gaps. Any fresh stack would have deployed "successfully" and then run as the untuned shell of the system, not the system Chapters 2–3 validated. All three were fixed as surgical edits before the production deploy, and the 2026-08-10 run confirmed them working on a real fresh account:

1. **Processor sizing.** `pest-detection-processor` deployed at 512 MB / 60 s while validated production runs **1024 MB / 600 s**. One tiled frame plus up to 120 LLM verify crops measures 24–54 s of runtime, so the 60 s timeout would have failed intermittently on real frames.
2. **`lambda_env` carried no detection tuning.** The processor's environment held only table and bucket names, so a fresh stack ran the retired trust-Rekognition mode on Haiku. The env builder now ships the full validated tuning: `TILE_MIN_CONFIDENCE=8`, `LLM_VERIFY_ALL_BOXES=true`, `LLM_VERIFY_MODEL_ID=us.anthropic.claude-sonnet-4-6`, `LLM_VERIFY_MAX_BOXES=120`, `LLM_VERIFY_MAX_TOKENS=300`, `LLM_VERIFY_WORKERS=3`, `LLM_VERIFY_PAD=0.6`, `POST_NMS_IOU=0.1`, `POST_NMS_CONTAIN=0.1`, `POST_MAX_BOX_AREA=0.05`, `POST_VERIFY_FLOOR=49`.
3. **The worst: seeded camera rows had no `llm_verify_enabled`.** The LLM gate is per-camera opt-in, so on a fresh deployment the entire verification layer would simply never run. The rows also seeded `min_confidence=30` — the candidate-floor trap that strangles recall before the LLM ever sees a box. Rows now seed `min_confidence=10`, `llm_verify_enabled=true`, `post_verify_floor=49`, plus `max_runtime_min=45` on the template camera.

These are product fixes, not migration patches: they make any fresh ARGUS stack reproduce the validated system, tuning included.

Two further alignment fixes landed 2026-08-11:

- The processor role gained two inline policies captured from the live reference role: `bedrock-verify` (`bedrock:InvokeModel` + `bedrock:Converse` on the account's inference profiles and underlying foundation models — without it every crop verdict gets AccessDenied, the processor's total-gate-failure guard trips, and the stack FAILS OPEN to plain `min_confidence` thresholding at the candidate floor, flooding the dashboard with unverified boxes that look like confident detections; exactly this happened in production 2026-08-12, 321 boxes across 13 images — see Chapter 2 §2.5.6 and 9.3.14) and `s3-frames-write` (`s3:PutObject` on `frames/*` for the processor's EXIF-normalised frame write-back). Audit JSONs: `deployer/audit/iam__pest-detection-processor-role__inline__bedrock-verify.json` and `...__s3-frames-write.json`.
- The watchdog now deploys from the repo mirror `lambda/pest-model-watchdog.py` (v6.2, which honours per-camera `max_runtime_min`) instead of the pre-v6.2 `audit/pest-model-watchdog_src/` snapshot; `MAX_RUNTIME_MIN=75` stays in its env as the global fallback.

Timing caveat on the 2026-08-10 production run: it PREDATED these role-policy fixes. The production processor role came up without `bedrock-verify` and `s3-frames-write` (and without `rekognition:DetectLabels`) and had to be patched separately on 2026-08-12 by `migration/fix_processor_iam.py`, after the first validation run exposed the fail-open flood described above (9.3.14). Future deployments get the policies from the deployer's role build; but validate ANY deployment by result — push a known image through the pipeline and check the detections — never by green stages alone.

The packaged artifacts were rebuilt the same day (2026-08-11) so the frozen build carries all of the above: `dist\ARGUS.exe` (57.4 MB) and `dist\installer\ARGUS-Setup-1.0.0.exe` both ship the Bedrock policy JSONs, the v6.2 watchdog mirror, and the v6.3 Lambda sources. An exe built before 2026-08-11 deploys the untuned stack — do not distribute stale builds.

### The 15 stages, in order

Every stage is idempotent: existing resources are adopted (matched by name), never duplicated. Safe to re-run.

| # | Stage | UI label | Creates / does | Adopt behavior |
|---|---|---|---|---|
| 1 | `iam` | Identity & permissions | 5 Lambda execution roles (`pest-detection-processor-role`, `pest-monitoring-api-role`, `pest-camera-scheduler-role`, `kvs-hls-handler-role`, `pest-model-watchdog-role`) with `AWSLambdaBasicExecutionRole` attached and inline policies loaded from the rewritten audit JSONs (the processor role's set includes `bedrock-verify` — required by the LLM verify gate — and `s3-frames-write`; both captured from the live reference role 2026-08-11, see the fix list above); plus `pest-scheduler-invocation-role` (trusted by `scheduler.amazonaws.com`, scoped `lambda:InvokeFunction` on the watchdog and camera-scheduler). Creating this stably named role also resolves the reference account's latent bug where the api-role's `iam:PassRole` pointed at a role that did not exist live | create_role tolerated on exists; policies re-put every run |
| 2 | `dynamodb` | Database | 4 tables, all PAY_PER_REQUEST: `pest-monitoring-cameras` (HASH `camera_id`), `pest-monitoring-detections` (HASH `image_id` + RANGE `detection_time`, GSI `by-pest-time` on `pest_type`+`detection_time`, projection ALL), `pest-monitoring-system-config` (HASH `config_key`), `pest-monitoring-schedule-logs` (HASH `log_id` + RANGE `timestamp`). Waits for `table_exists` on all | create tolerated; existing tables adopted |
| 3 | `s3` | Storage | 3 buckets with SSE-S3 (AES256). Frames + processed: versioning on, public access block all-true. Dashboard: public static website — public access block turned OFF first, then the public-read policy, then website config (index and error both `index.html`, the SPA fallback). The ordering is a trap: `PutBucketPolicy` fails while the block is on. Skips `LocationConstraint` in us-east-1 (passing it errors) | `BucketAlreadyOwnedByYou` adopted |
| 4 | `layer` | Image-processing library | Publishes Lambda layer `fyp-pillow` (Pillow 12.2.0, cp312, manylinux2014_x86_64). Prefers the prebuilt `layer/fyp-pillow.zip` bundled beside the app; a frozen exe has no pip toolchain, so if the zip is missing it dies with a pointer to `build.ps1`. In dev it pip-builds the layer and asserts `PIL/_imaging*.so` exists — the exact defect that broke layer v1 in the reference account (no compiled binaries, no boxes drawn) | publishes a new version each run (versions are cheap; destroy deletes all) |
| 5 | `lambda` | Cloud functions | The 5 functions, python3.12, handler `lambda_function.lambda_handler`: `pest-camera-scheduler` first (the api's env references its ARN), then `pest-detection-processor` (1024 MB/600 s — the validated production sizing — and the only one with the Pillow layer; its env ships the full detection tuning listed in the fix section above), `pest-monitoring-api` (256/30), `kvs-hls-handler` (128/30), `pest-model-watchdog` (128/30, `MAX_RUNTIME_MIN=75` global fallback; the v6.2 code honours per-camera `max_runtime_min`). Source zips are built in-memory; the kvs-hls-handler source comes from `audit/kvs-hls-handler_src/`, the watchdog from the repo mirror `lambda/pest-model-watchdog.py`. Retries create on `InvalidParameterValueException` (IAM role not yet propagated) | on exists: `update_function_code` + `update_function_configuration` |
| 6 | `s3-notification` | Event wiring | `lambda:AddPermission` for `s3.amazonaws.com` on the processor FIRST, then the frames-bucket notification: `s3:ObjectCreated:*`, prefix `frames/`, target `pest-detection-processor`. Order matters — the notification put fails without the permission | permission add tolerated on exists; notification is a whole-config put |
| 7 | `cognito` | Sign-in service | Pool `pest-dashboard-users` (email as username, auto-verified email, admin-create-only, password minimum 8 with lowercase+number) and SPA client `dashboard-web` (no secret, `ALLOW_USER_PASSWORD_AUTH` + refresh, 12 h tokens / 30 d refresh). Zero users seeded — the customer creates accounts from the done screen | matched by name via list, adopted |
| 8 | `apigw` | Secure API | HTTP API `pest-monitoring-api-gateway` with CORS (`*` origins, GET/POST/DELETE/OPTIONS); JWT authorizer `cognito-dashboard` bound to the pool/client; 2 AWS_PROXY integrations (api, hls); the 21 routes — JWT on all except `GET /stream/status` (device pollers, unauthenticated); all routes target the api integration except `GET /video-playback`, which targets the HLS integration (still JWT-protected); `$default` auto-deploy stage; 2 invoke permissions (api-wide for `pest-monitoring-api`, route-scoped `/*/*/video-playback` for `kvs-hls-handler`) | api/authorizer/integrations matched by name/URI; existing routes go through `update_route` — which requires `ApiId` and only `RouteKey` stripped from the kwargs (stripping both made every resume fail on the first pre-existing route; fixed 2026-07-16) |
| 9 | `cloudfront` | Dashboard & delivery | Distribution in front of the dashboard bucket's **website endpoint** (custom origin, `http-only` — website endpoints have no HTTPS), viewer redirect-to-https, AWS-managed CachingDisabled policy `4135ea2d-6df8-44a3-9df3-4b5a84be39ad` (same id in every account), compress on, fresh UUID caller reference. Takes minutes to reach Deployed | matched by the distribution comment (`{prefix} dashboard frontend (S3 website origin)`), adopted |
| 10 | `writeback` | Configuration | Templates `web/dashboard_v4/js/config.js` in memory (`HTTP_API`, `COGNITO_REGION`, `COGNITO_CLIENT_ID`), uploads every dashboard file to the dashboard bucket with correct content types (`.js` forced to `text/javascript`), then sets the frames-bucket CORS to the website endpoint + the CloudFront domain | full re-sync every run |
| 11 | `kvs` | Live video | Optional. Only if the wizard enabled live view: a Kinesis Video Streams stream `{prefix}-cam-stream`, 2 h retention | create tolerated on exists; skipped entirely when live view is off |
| 12 | `scheduler` | Cost guard | EventBridge Scheduler schedule `pest-model-watchdog-15min`, `rate(15 minutes)`, ENABLED, target = watchdog Lambda via `pest-scheduler-invocation-role`, zero retries | create tolerated on exists |
| 13 | `rekognition` | Vision engine | Empty Custom Labels project `{prefix}-detection`. No datasets, no model — trained Custom Labels models are account-bound (no export/copy API), so the customer trains later with their own images | matched by project name, adopted |
| 14 | `seed` | Initial data | System-config row `detection_settings` (email enabled, recipient, auto-capture, 60 s interval) and 2 camera rows: `manual_upload` (required by the dashboard Test-upload feature) and `camera-1` (the template camera: the wizard's deployment name as label, `target_label` from the wizard, `tiling_enabled=True`, `max_runtime_min=45` — bounds an unattended model start via the watchdog). Both rows seed the validated per-camera detection config: `model_type=custom`, `min_confidence=10` (the candidate floor feeding the LLM gate, NOT the display threshold — setting it high silently strangles recall before the LLM sees a box), `llm_verify_enabled=True` (the gate is per-camera opt-in; left unset it never runs), `post_verify_floor=49` (the user-facing threshold the dashboard edits), empty `custom_model_arn` — filled by training. The tiling default is deliberate: the earlier whole-image-only ruling was superseded in late July 2026 (Rekognition finds candidates on tiles, the LLM verifier filters them — see Chapter 2) | put_item overwrites — re-running resets the seeds |
| 15 | `ses` | Alerts | `verify_email_identity` for sender and recipient (each gets a confirmation email that must be clicked). Then checks `sesv2 get_account`: if the account is in the SES sandbox it logs the warning — alert emails only reach verified addresses until production access is granted (manual AWS request, ~1–2 business days) | verification re-send is harmless |

### State file, resume, and stamps

`deploy_state.json` accumulates every generated id: `role:*` ARNs, `bucket:*` names, `layer:fyp-pillow`, `lambda:*` ARNs, `cognito:pool`/`client`, `apigw:id`/`url`, `cloudfront:id`/`domain`, `kvs:stream`, `rekognition:project`, plus the `deploy:*` run stamps and every `train:*` key from Chapter 7.5's pipeline.

Resume semantics:

- `deploy:last_stage` lets a later launch distinguish "never ran" / "died at stage N" / "completed" and offer the right action.
- The completion stamp `deploy:completed_at` means the whole stack ran. A `--only` subset run never writes it — the CLI logs "completion stamp not written" instead. The app's resume logic trusts the stamp, so a subset must not forge it.
- Stale-stamp invalidation: both `main()` and `run_plan()` pop `deploy:completed_at` and `deploy:last_stage` at the start of every run, so a re-deploy that dies mid-run cannot masquerade as the previous complete run.
- Because every stage adopts existing resources, "resume" is simply "run the plan again" — completed stages fly through as adoptions.

CLI flags: `--only s3,lambda` runs selected stages; `--dry-run` prints the plan and changes nothing; `--verify` audits instead of deploying.

### verify(ctx) — the live audit

Proves every resource recorded in the state file exists in the account right now. Read-only, costs nothing, safe on a partial deploy (missing resources come back `ok=False`). For each state key it makes the matching describe/head call: `iam.get_role`, `s3.head_bucket`, `lambda.get_function` (detail = function state), `get_layer_version_by_arn`, `describe_user_pool`/`_client`, `apigw.get_api`, `cloudfront.get_distribution` (detail = `Deployed` vs `InProgress`), `kinesisvideo.describe_stream`, Rekognition project lookup. It also checks resources the stages never wrote into state: the 4 DynamoDB tables and the `pest-model-watchdog-15min` schedule. Returns `[{name, ok, detail}]`; the CLI prints `OK`/`MISS` per line and "deployment is INCOMPLETE" if anything is missing. In the app this is the "System check" behind `verify_deployment`, and it doubles as the post-destroy proof of absence.

### destroy(ctx) — teardown

Tears the whole deployment down in reverse dependency order, driven by the state file plus the fixed naming. Every step is best-effort: a missing resource is reported "(already gone)" and counted as done, never fatal, so a partial stack tears down cleanly too. Not-found error codes (`ResourceNotFoundException`, `NoSuchEntity`, `NotFoundException`, `NoSuchBucket`, `NoSuchDistribution`, `404`, `ValidationException`) are benign; anything else marks the step failed and continues.

Safety rails:

- Account-match guard: refuses to run if `deploy:account` in the state file differs from the account the stored key resolves to — you cannot destroy account B's stack with account A's state file. One gap: `deploy:account` is written by `run_plan` (the app path) but not by the CLI's `main()` loop, and the guard skips silently when the key is absent. A CLI-deployed state file therefore does not get this protection; the typed-confirmation and best-effort semantics still apply.
- Typed confirmation: the app requires the deployment prefix typed exactly (`destroy_deployment`).
- Never under training: the app refuses while `_training` is set.

Order: (1) the watchdog schedule; (2) Rekognition — stop RUNNING project versions, wait out STOPPING, delete versions, delete the project; (3) the KVS stream; (4) CloudFront — disable, wait for Deployed (the slow one, minutes), delete; (5) API Gateway; (6) the Cognito pool (clients and users die with it); (7) the 5 Lambdas; (8) every `fyp-pillow` layer version; (9) the 3 buckets (empty each via paginated `delete_objects`, then delete); (10) the 4 tables; (11) the 6 IAM roles (inline policies deleted and managed policies detached first); (12) SES identities. Finally the state file is renamed to `destroyed_{timestamp}.json` — the deployment no longer exists, but the record of what it was is archived.

After the engine finishes, the app re-runs `verify()` on the still-in-memory state as proof of absence: the pass condition is "0/N remain". Leftovers are listed in the `destroy_verified` event. Only then does the UI treat the deployment as gone.

Account closure is not part of destroy: closing an AWS account is root-plus-console-only (no API for standalone accounts). ARGUS opens the billing page in the embedded AWS window and guides; root credentials never touch the product.

### 7.4.9 Function reference: every function in deploy.py

The stage list tells you the order. This table tells you what each function is responsible for and the rule inside it, so the source can be read without tracing every call.

| Function | Job | Rule or algorithm inside |
|---|---|---|
| `set_emitter` / `_emit` | Send structured progress to whatever is watching | The desktop app registers a callback; headless runs get plain log lines. Every stage reports through the same channel, so the UI and the CLI never diverge |
| `log` / `die` | The only two output paths | `die` raises after emitting, so a stage cannot half-fail silently |
| `run_plan` | Drive a list of `(name, fn)` stages | Runs stages in order, emits a start and an end event per stage, and returns `(ok, error_message)`. The desktop app uses it; `main()` keeps its own loop so the CLI can support `--only` |
| `wait_for` | Absorb eventual consistency | Polls a predicate with retries and swallows `ClientError` between attempts. Needed because a role is not immediately usable by Lambda after `create_role` |
| `already_exists` | Decide whether a failure is really a failure | A whitelist of the "already there" error codes the various services return (`EntityAlreadyExists`, `ResourceConflictException`, `BucketAlreadyOwnedByYou`, and four more). This one function is what makes every stage idempotent |
| `stage_iam` | Create the six roles | For each role: `create_role`, adopt on `already_exists`, attach the basic execution policy, attach any managed policies, then put every inline policy from the audited JSON with the account and region rewritten to the target account |
| `stage_dynamodb` | Create the four tables | Declares each table's key schema inline, all pay-per-request, including the `by-pest-time` index on the detections table; adopts an existing table by name |
| `create_bucket` / `stage_s3` | Create and configure the three buckets | `create_bucket` handles the us-east-1 special case, where a `LocationConstraint` must NOT be sent. `stage_s3` then applies SSE-S3 encryption, versioning, and the public-access settings each bucket needs — the dashboard bucket has its block cleared so the public-read website policy is accepted |
| `stage_layer` | Publish the Pillow layer | Prefers a prebuilt `fyp-pillow.zip` shipped with the app, because a packaged executable has no pip toolchain. From source it builds the layer with the manylinux target flags and asserts the compiled `_imaging` object is present before publishing |
| `zip_lambda` | Package one function in memory | Writes the source into a zip **renamed to `lambda_function.py`**, because that is the handler name every function is deployed with. No temporary files touch disk |
| `lambda_env` | Supply each function's environment | A per-function dictionary. The processor branch carries the validated detection tuning, so a fresh account comes up in the measured two-stage configuration rather than on code defaults |
| `stage_lambda` | Create the five functions | Deploys in a fixed order that satisfies the cross-references: the scheduler first, because the API needs its ARN. Sets memory, timeout and layer per function; updates code and configuration when the function already exists |
| `stage_s3_notification` | Wire the trigger | Permission first, notification second — the reverse order fails. The notification carries the `frames/` prefix filter that keeps training uploads from starting detection runs |
| `stage_cognito` | Create the pool and app client | Finds an existing pool by name before creating one, so a re-run does not orphan users. The client is a public single-page client with no secret |
| `stage_apigw` | Build the HTTP API | Adopts an existing API by name, creates the JWT authorizer bound to the Cognito pool and client, creates the two Lambda integrations, then creates all 21 routes from the route table — authorization is a field in that table, which is why the one unauthenticated route is a single visible line |
| `stage_cloudfront` | Put HTTPS in front of the dashboard | Identifies its own distribution by a generated comment string, since CloudFront has no name field. Uses the managed CachingDisabled policy so a plain sync is a full redeploy |
| `stage_writeback` | Make the deployment self-configuring | Templates the API URL, Cognito region and client id into `config.js` in memory, uploads every dashboard file with the right content type, then adds the CloudFront domain and website endpoint to the frames-bucket CORS list. Without that last step the Test upload panel fails on its presigned PUT |
| `stage_kvs` | The optional live-view stream | Returns immediately unless `--live-view` was passed. This is why the production run completed 15 of 15 stages with KVS skipped by design |
| `stage_scheduler` | Install the cost guard | Creates the EventBridge schedule `pest-model-watchdog-15min` at `rate(15 minutes)`, targeting the watchdog through the scheduler invocation role, with retries disabled |
| `stage_rekognition` | Create an empty Custom Labels project | Adopts by project name if it exists. Deliberately empty: models are account-bound and cannot be shipped, so the customer trains their own |
| `stage_seed` | Write the starting configuration | Puts the global config row and the two camera rows, carrying the validated per-camera detection settings. This is what makes the stack usable the moment it finishes |
| `stage_ses` | Start email verification | Sends a verification email to the sender and recipient addresses, then checks `ProductionAccessEnabled` and warns when the account is still in the SES sandbox — the state the production account is in today |
| `destroy` | Tear the whole deployment down | Reverse dependency order, driven by `deploy_state.json`. Every step is best-effort so a missing resource is reported and skipped rather than aborting the teardown. The state file is renamed rather than deleted, so the record of what existed survives |
| `verify` | Prove the deployment is real | Read-only post-deployment audit: for every resource recorded in the state file, ask the account whether it exists right now, and return per-resource evidence. Safe on a partial deploy, where missing resources simply come back not-ok. It is also what proves a destroy actually removed everything |
| `main` | The command-line entry point | Parses the arguments, builds the context, and runs the stages, honouring `--only` for a subset and the resume stamp for a restart after a failure |

## 7.5 training.py — the in-app training pipeline

### Purpose

Takes the customer's local YOLO-labeled image folder and produces a trained, wired-in Rekognition Custom Labels model: analyze -> validate -> convert (YOLO -> Ground Truth manifest) -> upload to S3 -> create/append Rekognition datasets -> train -> watch -> write `custom_model_arn` onto every custom camera row. The converter math is the production-proven `datasets/convert_to_manifest.py` logic (models v3–v6 all trained through it), ported here. Design doc: `docs/deployer_training_pipeline.md`.

### Folder analysis and validation (`analyze`, `_find_splits`, `_class_names`, `parse_yolo_line`)

`_find_splits` auto-detects three layouts: `yolov8` (`root/images/{split}/` + `root/labels/{split}/`), `flat` (`root/images/` + `root/labels/`, single split), and `roboflow` (`root/{split}/images/` + `root/{split}/labels/`). `_class_names` best-effort parses `data.yaml` without a YAML dependency (inline list, block list, and id-map forms). `parse_yolo_line` accepts detection lines (5 numbers) and segmentation lines (1 + 2N numbers, reduced to the bounding box).

`analyze(folder)` is local, fast, and fail-fast: no AWS calls, no image decoding (label text only). It reports layout, per-split counts, per-class stats with per-split image counts, warnings, and hard blockers:

- Blocker: no readable YOLO boxes anywhere.
- Blocker: pixel-coordinate labels. YOLO coords are normalized [0,1]; any value above 1.5 (tolerating rounding slop) means pixel units. If more than half the boxes are out of range, the whole set is blocked with "re-export with normalized coordinates" — catching it here instead of letting them clamp to zero-size boxes deep in convert. A minority is only a warning (those boxes get dropped).
- Warnings: orphan label files (no matching image), unlabeled images, zero-box labeled images, malformed lines. Zero-box images are excluded up front and reported, never uploaded — Rekognition Custom Labels object detection ignores them (the v6 negatives lesson).

`plan_for_class(report, class_id)` is a pure function feeding the cost gate: if the folder has an explicit `test` split with at least 10 images, it is used; otherwise a test set of `max(10, 15%)` is carved from the pool. Blocked if fewer than 10 train or 10 test images survive. Cost estimate: `est_hours = max(1.0, round(0.8 + train_n/400, 1))` at ~US$4 per training hour (reference run: 1,148 images -> 3.5 h billable). Deliberately uncapped: a 10k-image set really does train for a day and the gate must say so.

NOTE: doc lag — `docs/deployer_training_pipeline.md` says "~$1/h" and "poll every 5 min"; the code bills the estimate at US$4/h and `watch_and_wire` polls every 60 seconds.

### Convert and stage (`_convert`)

Walks every (image, label) pair, keeps boxes of the chosen class, and produces the upload list plus per-split Ground Truth manifest entries. Key behaviors:

- Dedupe by stem: `_existing_stems` lists every dataset entry already in the project (excluding the current version's own prefix, so a retry replaces its own earlier upload instead of being deduped against it) and `_convert` skips those stems. Re-picking an old folder cannot duplicate entries or leak former TRAIN images into TEST. If every image is excluded, the run fails with "pick a folder of NEW photos".
- Deterministic split assignment mirroring `plan_for_class`: explicit `test` split honored if >= 10, otherwise an evenly spaced carve from the sorted pool.
- Images that need work — any dimension over 4096 px (Custom Labels rejects them) or a non-JPEG/PNG format — are re-encoded into a staging dir as JPEG quality 90, with oversize images LANCZOS-downscaled to 4096 max edge. Everything else uploads untouched. Box pixel coordinates are computed from the final saved dimensions, and since YOLO boxes are normalized, the downscale is lossless for labels.
- Degenerate boxes (under 1 px after rounding) are dropped; images left with no annotations are dropped; unreadable images are logged and skipped. After conversion, both splits must still have >= 10 images or the run fails with a pointer to the warnings.

Manifest entries are Ground Truth object-detection format: `source-ref` to the S3 key, `bounding-box` with image size and pixel annotations, `class-map {"0": class_name}`, `human-annotated: yes`, job name `argus-training`. The class name is sanitized to `[A-Za-z0-9_.\- ]`, max 64 chars.

### Upload and bucket grant (`_grant_rekognition_access`, `_upload`)

Before uploading, the frames bucket gets a policy so the Rekognition service principal can read `training-data/*` and write evaluation output to `training-output/*` (mirroring what the Custom Labels console generates). Idempotent: the three ARGUS Sids (`ArgusRekognitionBucketRead`, `ArgusRekognitionObjectRead`, `ArgusRekognitionOutputWrite`) are replaced in place, all other statements preserved. Images upload to `training-data/v{N}/{split}/images/`, manifests to `training-data/manifests/v{N}/{train,test}.manifest`, with progress events every 20 files.

### Datasets (`_load_datasets`, `_wait_dataset`)

If the project has no TRAIN/TEST dataset yet, each is created from its manifest (`create_dataset` with the Ground Truth S3 source). On iteration, entries are appended with `update_dataset_entries`, chunked under 4 MB per call (the API cap is 5 MB). `_wait_dataset` polls to the terminal status with a freshness guard: it ignores a terminal status recorded before our own submission (`after_ts`), because a multi-chunk append could otherwise read the previous chunk's `UPDATE_COMPLETE` and fire the next chunk into a dataset still ingesting. Dataset `ErrorEntries` are reported but non-fatal — the engine skips those images at training.

One engineering lesson from the 2026-08-10 account migration belongs here for anyone moving a Custom Labels project between accounts: after any labelling done in the Rekognition console, the **dataset — not the S3 manifest it was created from — is the source of truth**. Console edits live only inside the dataset. Export the live entries with `ListDatasetEntries` before rebuilding a dataset elsewhere; rebuilding from the original manifest silently drops every console edit.

### Train, watch, wire (`_next_version_number`, `_start_version`, `watch_and_wire`, `_wire_in`)

`train:next_n` is persisted at run start, not on success: a retry after any failure re-uses the same `n`, so the same images land on the same S3 keys and replace their dataset entries instead of duplicating (`update_dataset_entries` matches on source-ref). The `train:attempt` stamp lets a relaunched app say "an earlier attempt was interrupted" if the process is hard-killed before submit.

`_start_version` calls `create_project_version` (version name `v{n}-{UTCdate-time}`, output to `training-output/v{n}` in the frames bucket). A `LimitExceededException` becomes a clear message: delete old versions in the Rekognition console, then train again — the pipeline never deletes anything itself. On submit it swaps the local attempt stamp for the durable `train:pending` record; from here the cloud continues without the app.

`watch_and_wire` polls `describe_project_versions` every 60 s. Transient network/API errors are tolerated up to 30 consecutive failures (~30 min) before giving up with "training itself continues in the cloud — re-attach from the Train screen"; `train:pending` survives the give-up. Status handling is deliberate: `TRAINING_COMPLETED`, `STARTING`, `RUNNING`, `STOPPING`, `STOPPED` all mean training succeeded (a model that completed while the app was closed may already have been started or stopped from the dashboard); `TRAINING_FAILED`, `FAILED`, `DELETING` are dead (recorded in `train:last_failure`).

`_wire_in` is the critical step (as the `training.py` docstring puts it, a trained but unwired model is useless): it scans `pest-monitoring-cameras` and sets `custom_model_arn` to the new version ARN on every row with `model_type == "custom"`, via boto3 only — model ARNs contain colons that shell quoting mangles (see `docs/aws.md`). It records the version under `train:v{n}` (ARN, F1, billable seconds), saves the previous ARN as `train:rollback_arn`, sets `train:active_arn`/`train:active_f1`, and bumps `train:next_n`. If no camera row is custom, it warns loudly instead of pretending success.

`rollback(ctx)` codifies the v6-to-v5 hand-rollback of 2026-07-15: one click swaps every custom camera row back to `train:rollback_arn`, recovers that model's F1 from its version record, and makes the swap reversible (current becomes the new rollback target).

`status(ctx)` gives the UI a one-call summary, making a single describe call only when a run is pending. A pending run that vanished from the project (deleted in the console) is cleared to `train:last_failure` status `MISSING` so the UI cannot wedge.

### Deliberate defaults (settled 2026-07-17)

- **No auto-start.** The model is left STOPPED after training. The done panel says so: start detection from the dashboard, inference costs ~US$4/h, and the watchdog stops forgotten models.
- **No auto-delete.** Old project versions stay until the customer deletes them in the console; on the version limit the pipeline stops with a clear message.
- **No training-data prefix deletion.** Iteration appends to the same Rekognition datasets, so every past `v{N}` prefix stays referenced by dataset entries — deleting one would break the next retrain. Storage cost is cents; correctness wins.

## 7.6 The UI — screens map

`web/index.html` is self-contained (inline CSS and JS, WebGL + Canvas2D hero, no CDNs). It doubles as a browser preview with simulated actions when `window.pywebview.api` is absent, and the real app when it is present. The UI is frozen by ruling (2026-07-21) — do not restyle it.

| Screen id | Name | What happens there |
|---|---|---|
| `s-welcome` | Welcome | Product intro, start |
| `s-consent` | Consent | Terms of Use + Privacy Policy (`legal_text`), accept or decline-and-exit (`quit`) |
| `s-account` | AWS account | Guidance rail + `open_aws_pane('signup'/'console')` for creating or signing into the AWS account; card and passwords go into AWS's embedded pages only |
| `s-keys` | Access keys | `open_aws_pane('iam-keys')` walks the customer through creating the `deployer` IAM user; pasted key goes through `verify_credentials` (root refusal, admin preflight) |
| `s-config` | Configuration | Deployment id (prefix), deployment name, target label, region, sender/recipient email, live view toggle |
| `s-review` | Review | The plan summary; hold-to-deploy |
| `s-theater` | Deployment theater | The 15 stages streaming live (`stage`/`log` events); camera-aperture progress motif ending at f/1.4 |
| `s-done` | Done | Dashboard URL (opens via `open_external`), API URL, system check (`verify_deployment`), SES/Rekognition console shortcuts, plus two embedded panels: **Dashboard accounts** (the pool starts with zero users — nobody can sign in until the first is created here; `list/create/delete_dashboard_user`) and the **Danger zone** (Delete deployment: typed-prefix confirmation, streaming destroy log, post-destroy verify) |
| `s-train` | Train your model | Folder pick, analysis report, class choice, cost gate with typed TRAIN, phase rail (validate/convert/upload/datasets/train/wire), resume bar that re-attaches to an in-flight run on relaunch, F1 result, rollback |

## 7.7 build.ps1 — packaging

`build.ps1` packages everything into `dist\ARGUS.exe`. Run it from `deployer/`:

```
powershell -ExecutionPolicy Bypass -File build.ps1
```

What it does, in order:

1. Installs/upgrades build deps: pyinstaller, boto3, pywebview, keyring, Pillow.
2. Prebuilds the Pillow Lambda layer zip once if `layer\fyp-pillow.zip` is missing: `pip install --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 --only-binary=:all: Pillow==12.2.0`, asserts `PIL\_imaging*.so` exists (the layer-v1 defect), zips it. A frozen exe cannot pip-build (no toolchain, and `sys.executable` is the exe itself), so the zip must ship inside the bundle; `stage_layer` publishes it when present.
3. Runs PyInstaller: `python -m PyInstaller app.py --onefile --noconsole --name ARGUS` with the liquid-glass icon (`assets\argus_B.ico`, Runzhe's pick 2026-07-21) and a version resource, plus `--hidden-import keyring.backends.Windows` and `--collect-data botocore --collect-data boto3`.

Hard-won build facts, all encoded in the script:

- **`python -m PyInstaller`, not the bare `pyinstaller` exe** — the exe lives in a Scripts dir that is not reliably on PATH (failed 2026-07-17); the module form always works.
- **Pure ASCII requirement**: the script must stay pure ASCII. Windows PowerShell 5.1 reads BOM-less files as ANSI; a UTF-8 em-dash decodes into a smart quote and breaks the parser (bit the project 2026-07-17).
- **The writeback-bundle bug and fix**: `stage_writeback` reads the dashboard from `REPO / "web" / "dashboard_v4"`, and in a frozen exe `REPO` is the PyInstaller `_MEIPASS` unpack dir. The exe therefore must bundle the repo's dashboard: `--add-data "..\web\dashboard_v4;web\dashboard_v4"`. Without that entry the writeback stage has nothing to upload and the deployment dies at stage 10. Similarly the app's own UI packs as `"web;web"` (not `"web;."`) because the code expects it at `_MEIPASS\web`. Windows' `--add-data` separator is `;`.
- **Assets bundled**: `audit;audit` (the policy JSONs and two Lambda sources), `legal;legal`, `layer;layer` (the prebuilt Pillow zip), `..\lambda;lambda` (the three core Lambda sources).
- **WebView2 runtime**: handled by the installer (next section). The `MicrosoftEdgeWebView2Setup.exe` evergreen bootstrapper (~2 MB) is bundled at `installer\redist\` and auto-invoked by the installer on machines without the runtime. Win11 and current Win10 preinstall it. Only when shipping the bare onefile `dist\ARGUS.exe` (no installer) does the old rule apply: ship the bootstrapper next to the exe as a fallback.

### build_installer.ps1 — the Windows installer

`build_installer.ps1` wraps the finished `dist\ARGUS.exe` into a proper Windows installer. Run it from `deployer/` after `build.ps1`:

```
powershell -ExecutionPolicy Bypass -File build_installer.ps1
```

Prereqs: `dist\ARGUS.exe` exists, Inno Setup 6 installed (`winget install JRSoftware.InnoSetup`), and the WebView2 evergreen bootstrapper present at `installer\redist\MicrosoftEdgeWebView2Setup.exe`. The script locates `ISCC.exe` and compiles `installer\argus.iss`; output is `dist\installer\ARGUS-Setup-<version>.exe`.

What the installed app gets, per `argus.iss`:

- `Program Files\ARGUS\ARGUS.exe`, a desktop shortcut (checked by default — Runzhe's spec), a Start Menu entry, and an uninstaller.
- **WebView2 auto-install**: the bundled bootstrapper is staged to `{tmp}` and run `/silent /install` before app launch, gated by a registry check (`WebView2Installed` reads the evergreen runtime's `pv` value under HKLM WOW6432Node / HKCU). Machines that already have the runtime skip it entirely.
- License page = the bundled Terms of Use; uninstall also removes the app's working data under `%LOCALAPPDATA%\ARGUS`.
- The `AppId` GUID must never change after first release — it is how upgrades find the install.

The script is pure ASCII for the same PowerShell 5.1 encoding reason as `build.ps1`.

NOTE: `app.py`'s docstring says "Package: see build_exe.md"; that file does not exist — `build.ps1`, `build_installer.ps1`, and `README.md` are the packaging references.

## 7.8 The two-round rehearsal plan

`deployer/REHEARSAL.md` defines the acceptance protocol. Round 1 shakes out bugs cheaply; round 2 is the customer-realism dress rehearsal. Do not skip round 1 — burning the fresh-account experience on a packaging bug wastes it.

**Round 1 — cheap shakeout (dev machine, old account).** Target: the dormant old account `396278862184` (queued for closure; its Gen-1 residue usefully exercises the adopt-existing paths). Run from source (`python app.py --debug`), paste an IAM key for that account, run the full wizard, watch all 15 stages. PASS = the theater completes, the done screen shows a real CloudFront URL, and `deploy_state.json` holds every id. Then the training pipeline: pick a small YOLO folder (~30 images), confirm the analysis report and cost gate, type TRAIN, let it run to TRAINING_COMPLETED (~1 h, ~US$4), confirm F1 appears and `custom_model_arn` landed on both camera rows, and close/reopen the app mid-watch once to prove re-attach. "Exists, adopting" logs from residue are acceptable noise. Bugs found here get fixed before round 2.

**Round 2 — full dress rehearsal.** Realism on all four axes: build the real `dist\ARGUS.exe`; run it on a clean machine (the mini PC's Win11 host — no Python, no AWS CLI, no cached credentials); create a fresh AWS account through the app's embedded signup (email verification, card typed into AWS's embedded page, phone code, Basic plan — the only true test of the one-stop UX); IAM key via the embedded console flow, confirmed to land in the Windows Credential Manager. Then a real deploy, and the day-1 customer smoke test: dashboard over HTTPS with login, first Cognito user, test upload appears in the gallery, train screen renders, SES verification email confirmed, watchdog schedule ENABLED. Cost check: 24 h idle should bill ~$0. The account is kept afterwards as the demo/handoff account for Dr. Li, or closed (one console action).

**What actually happened (2026-08-10): the production deployment became the acceptance run.** The account migration to the NP production account `506868652945` merged into the rehearsal plan. The stage engine ran headless from the CLI — `python deploy.py --profile prod --prefix argus --target-label armyworm-larva --sender-email ... --deployment-name "Jewel Forest Valley"` — against the brand-new account and completed **all 15 stages in 103 seconds with zero errors**, first attempt, no resume needed. The KVS stage was skipped by design (`--live-view` not passed; live view is not migrated). Post-deploy verification confirmed the three 2026-08-10 gap fixes landed on a real fresh account: processor at 1024 MB / 600 s with the Pillow layer attached; env carrying Sonnet 4.6, `LLM_VERIFY_ALL_BOXES=true`, tile floor 8, `POST_VERIFY_FLOOR=49`, area cap 0.05; both camera rows reading `llm_verify_enabled=true, min_confidence=10, post_verify_floor=49`. The dashboard, API, Cognito pool, and Rekognition project ids it produced are listed in section 7.1.

This run covers the **account axis** of Round 2 — fresh account, no adoption residue, real CloudFront/Cognito/SES from zero. It exercised `deploy.py`, not the frozen exe: the **clean-machine axis** (packaged `dist\ARGUS.exe` on a no-Python machine, embedded signup and key flow, Credential Manager storage) remains untested and still deserves Round 2's dress-rehearsal treatment if the product is taken further.

**Success criteria:** zero terminal windows and zero external-browser visits during the whole flow; every user-typed secret went either into AWS's own embedded page (card, passwords) or into the OS credential store (IAM key), nothing in plaintext; the done screen's URL serves the dashboard over HTTPS with login; the state file contains 6 role ARNs, 3 buckets, the layer ARN, 5 Lambdas, pool+client ids, API id/url, CloudFront id/domain, and the project ARN.

NOTE: doc lag — `REHEARSAL.md` lists "4 tables" among the state-file contents. `deploy.py` never writes table keys into `deploy_state.json`; the tables have fixed names and `verify()` checks them directly. The state-file checklist above is the code-accurate one.

## 7.9 Security doctrine summary

- **Card data never touches the product** (PCI-DSS). It is typed into AWS's own embedded pages; ARGUS observes navigation URLs only, never the DOM, never any field.
- **Root keys are refused** before storage, with the IAM-user path taught instead. Root is used exactly once in the customer's life with this product — account creation and (if ever) account closure — both inside AWS's own pages.
- **The IAM key lives in the OS credential store** (Windows Credential Manager via `keyring`, DPAPI). Never plaintext on disk, never logged, never returned to the UI.
- **Permission preflight over late failure**: the AdministratorAccess check (direct and group-inherited) names the fix at the keys screen instead of letting a deployment die 8 stages in.
- **Destroy is guarded three ways**: typed prefix confirmation, account-match between state file and credentials, and a refusal while training is live. Its result is proven by a live audit, not trusted.
- **Deterministic automation only**: no LLM ever holds credentials or mutates cloud resources (architecture ruling, 2026-07-16).
- **No secrets in the repo**: the deployer tree contains identifiers only (account ids, ARNs, bucket names). One known exception elsewhere in the project: `datasets/archive/experiments/pre_v3_abandoned/download.py` contains an inline Roboflow API key — credential stored there, not reproduced here. Treat that file as sensitive; do not publish it.
- **The WebView2 profile stays in `%LOCALAPPDATA%`**, never the project tree, so console session cookies can never ride along in a zip or handoff.

## 7.10 Operations / reproduction

### 7.10.1 Run the deployer from source

From the repository root:

```
cd deployer
pip install -r requirements.txt
python app.py --debug
```

`--debug` opens WebView2 devtools. Without pywebview, serving `web/` from any static server gives the simulated preview (all actions fake).

### 7.10.2 Deploy a stack from the CLI (no app)

Bootstrap credentials must already be configured (`aws configure --profile **customer**`).

```
python deploy.py --profile **customer** --region **us-east-1** ^
    --prefix **vision** --target-label **my-pest** ^
    --sender-email **alerts@example.com** --recipient-email **me@example.com**
```

Useful variants: `--dry-run` (print the plan, change nothing), `--only **s3,lambda**` (subset; never stamps the run complete), `--live-view` (adds the KVS stage's stream).

The 2026-08-10 production deployment is a worked example of exactly this path: `--profile prod --prefix argus --target-label armyworm-larva` plus the emails and deployment name, 15/15 stages in 103 seconds on account `506868652945`, KVS skipped because `--live-view` was omitted.

### 7.10.3 Verify a deployment

App: done screen -> System check. CLI:

```
python deploy.py --profile **customer** --region **us-east-1** --prefix **vision** ^
    --target-label x --sender-email x@x --verify
```

Expect `verify: N/N present`. Any `MISS` line names the missing resource.

Console spot checks (region **us-east-1** unless you deployed elsewhere):

- Lambda: console -> Lambda -> Functions -> expect the 5 `pest-*` functions. CLI: `aws lambda list-functions --profile **customer** --query "Functions[].FunctionName"`
- Watchdog: console -> Amazon EventBridge -> Scheduler -> Schedules -> `pest-model-watchdog-15min` ENABLED. CLI: `aws scheduler get-schedule --name pest-model-watchdog-15min --profile **customer**`
- CloudFront: console -> CloudFront -> Distributions -> status Deployed. CLI: `aws cloudfront get-distribution --id **<cloudfront:id from deploy_state.json>** --profile **customer**`

### 7.10.4 Inspect the state file

Frozen exe: `%LOCALAPPDATA%\ARGUS\out\deploy_state.json`. Dev: `deployer/out/deploy_state.json`. It should contain 6 `role:*`, 3 `bucket:*`, `layer:fyp-pillow`, 5 `lambda:*`, `cognito:pool`/`client`, `apigw:id`/`url`, `cloudfront:id`/`domain`, `rekognition:project`, and the `deploy:*` stamps.

### 7.10.5 Confirm the stored credential

Console path (Windows): Control Panel -> Credential Manager -> Windows Credentials -> look for entries under `ARGUS-deployer`. PowerShell check that the entry exists (does not reveal the secret):

```
cmdkey /list | Select-String ARGUS
```

### 7.10.6 Create the first dashboard sign-in account

App: done screen -> Dashboard accounts -> add email + password. Console: Amazon Cognito -> User pools -> `pest-dashboard-users` -> Users -> Create user. CLI:

```
aws cognito-idp admin-create-user --user-pool-id **<cognito:pool>** --username **user@example.com** --user-attributes Name=email,Value=**user@example.com** Name=email_verified,Value=true --message-action SUPPRESS --profile **customer**
aws cognito-idp admin-set-user-password --user-pool-id **<cognito:pool>** --username **user@example.com** --password "**<password>**" --permanent --profile **customer**
```

The permanent flag matters: the dashboard login does not handle `FORCE_CHANGE_PASSWORD`.

### 7.10.7 Confirm SES identities

Each address gets a verification email that must be clicked. Console: Amazon SES -> Verified identities -> status Verified. CLI:

```
aws ses get-identity-verification-attributes --identities **alerts@example.com** --profile **customer**
```

While the account is in the SES sandbox, alerts only reach verified addresses. Production access is a manual AWS request (~1–2 business days).

### 7.10.8 Train a model

App: done screen -> Train your model -> pick the YOLO folder -> review the analysis -> choose the class -> pass the cost gate (typed TRAIN). Training runs 1–4 h in AWS; the app can be closed after submit and re-attached later (the Train screen's resume bar, or `watch_training`). Watch progress in the console: Amazon Rekognition -> Custom Labels -> Projects -> `**<prefix>**-detection` -> the training version. CLI:

```
aws rekognition describe-project-versions --project-arn **<rekognition:project>** --profile **customer** --region **us-east-1**
```

On completion the app wires `custom_model_arn` onto every `model_type=custom` camera row automatically. Verify:

```
aws dynamodb get-item --table-name pest-monitoring-cameras --key "{\"camera_id\":{\"S\":\"camera-1\"}}" --profile **customer** --region **us-east-1**
```

The model is left STOPPED by design. Start it from the dashboard when needed; the watchdog stops it after the camera row's `max_runtime_min` (seeded 45). The env `MAX_RUNTIME_MIN=75` is only the global fallback for rows without the field.

### 7.10.9 Destroy a deployment

App: done screen -> Danger zone -> Delete deployment -> type the deployment prefix exactly -> watch the destroy log stream -> the final line must read 0 resources remaining. The state file is archived as `destroyed_{timestamp}.json` in the same `out/` folder. Account closure afterwards (optional) is guided through the embedded AWS window: Billing -> Account -> Close account, as root, in AWS's own page.

### 7.10.10 Rebuild the exe

From the repository root:

```
cd deployer
powershell -ExecutionPolicy Bypass -File build.ps1
powershell -ExecutionPolicy Bypass -File build_installer.ps1
```

Output: `dist\ARGUS.exe` (onefile) and `dist\installer\ARGUS-Setup-<version>.exe` (installer; auto-installs the WebView2 runtime if missing). If shipping only the bare exe, put `MicrosoftEdgeWebView2Setup.exe` next to it for machines without the runtime.

## 7.11 Cross-references

- Chapter 1 — where the deployer sits in the overall system, and the production-vs-testbed framing.
- Chapter 2 — the cloud stack the deployer recreates (Lambdas, tables, buckets, API Gateway, Cognito, CloudFront, SES, the watchdog); `STACK_MANIFEST.md` is the deployer-side mirror of that chapter's live-account facts.
- Chapter 3 — detection models: what the training pipeline produces, F1 history, Rekognition Custom Labels limits (4096 px, zero-box images, account-bound models).
- Chapter 4 — the dashboard that stage 10 (`writeback`) templates and syncs; its Cognito auth is what the done screen's account panel manages.
- Chapter 8 — reproducing the whole system on a fresh AWS account end to end; ARGUS is the fast path for that chapter, and the 2026-08-10 production deployment to `506868652945` is its proof. Chapter 8 also lists the account ids in play (development/reference `366356442579`, NP production `506868652945`, old `396278862184`).
