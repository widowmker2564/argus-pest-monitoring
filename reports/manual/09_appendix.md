# Chapter 9 — Appendix: registries and reference tables

Single-source reference tables for the whole ARGUS Smart Pest Monitoring System: every AWS resource id and ARN, every device, every environment variable, the repository map, the camera registry, and the project glossary.
_As of 2026-08-11._ (Entries verified against a live account carry their own inline date. The code in `lambda/` is ground truth; where a doc lagged the code, the row says so. All repository paths in this chapter are relative to the project repository root.)

The system runs on the NP production account `506868652945`, stood up 2026-08-10/11 by the ARGUS deployer and validated end to end on 2026-08-12. Every row below describes that account unless it is explicitly marked as retired development-era history. The development account `366356442579`, where the system was built, is retired from operation; its identifiers are kept only where a historical record needs them (the model version table in 9.3.9 is the main case, because those versions were trained there and cannot be moved between accounts).

## 9.1 Role in the system

This chapter is the lookup layer of the manual. Chapters 1 to 8 explain how each part of the system works. This chapter answers a different question: "what is the exact name, id, or ARN of that thing?" Every identifier used anywhere in the manual appears here once, in a table, with its location and status.

Two audiences use it. An operator running the live stack uses it to find the resource they need to inspect (which Lambda, which table, which schedule). An engineer reproducing the stack on a fresh AWS account (Chapter 8) uses it as the checklist of what must exist when the rebuild is done, and as the map of which values are account-bound and must be replaced.

Nothing in this chapter contains a secret. AWS account ids, ARNs, bucket names, API ids, Cognito ids, IPs, and SSH usernames are non-secret identifiers and are listed in full. Passwords, access keys, and API keys are never reproduced; where one exists inside a source file, the table says "credential stored here, not reproduced".

## 9.2 Inventory

| Registry | Section | Covers |
|---|---|---|
| AWS resource registry | 9.3 | Account, S3, DynamoDB, Lambda, API Gateway, Cognito, CloudFront, Rekognition, Bedrock, KVS, EventBridge, SES, IAM — development account (9.3.1-9.3.13) and NP production account (9.3.14) |
| Device registry | 9.4 | Go2, Jetson Orin, SIYI A8 Mini, mini PC, Hikvision camera, laptop |
| Environment variables | 9.5 | Per-Lambda env vars and per-edge-service config, with meanings |
| Repository file map | 9.6 | Top-level directories of the project repository |
| Camera registry | 9.7 | The `pest-monitoring-cameras` rows on both accounts and their settings fields |
| Glossary | 9.8 | Project terms: batch_2, gate, gen-1, nbk2, and the rest |
| Conventions + change log | 9.9 | How this manual writes commands; document history stub |
| Operations | 9.10 | Console + CLI inspection procedure for every registry |
| Cross-references | 9.11 | Pointers to Chapters 1-8 |

## 9.3 AWS resource registry

### 9.3.1 Account and region

| Item | Value |
|---|---|
| **Production account id** | **`506868652945`** — the live system; everything in this chapter refers to it |
| **CLI profile / IAM user** | **`prod`** / `Student_QianRunzhe` (group `CAG_Proj` = AdministratorAccess, no permission boundary, no region lock) |
| Region | `us-east-1` |
| Retired development account | `366356442579` (profile `nbk2`, console user `cag_user`) — where the system was built; retired from operation 2026-08-10, kept read-only while the report is filed |
| Older account | `396278862184` — dormant, to be closed |

The production account is not empty: a previous student's Amplify "moobusapps" relics (13 roles, 1 deployment bucket) exist there. They are harmless; do not touch them. The retired development account was shared with unrelated NP projects (amplify chatbot, projassist, sagemaker, lights-out, spf* chatbot), and CloudShell writes were blocked there by a VPC endpoint — history only, no longer operative.

### 9.3.2 S3 buckets

| Bucket | Status | Purpose |
|---|---|---|
| `argus-frames-506868652945` | LIVE | Raw frames under `frames/<camera_id>/...`; upload here triggers the processor. Also holds training data under `training-data/`, training output under `training-output/`, and the handover snapshot under `handover/` |
| `argus-processed-506868652945` | LIVE (empty since v4.2) | Legacy annotated-image bucket. The processor stopped writing it at v4.2; kept because Lambda cold-start requires the env var and the delete path references it |
| `argus-dashboard-506868652945` | LIVE | Static website hosting for the dashboard (`web/dashboard_v4/`), public read, fronted by CloudFront `E1YADURLSAVNFA` |
| `frames-armyworm-366356442579`, `processed-images-armyworm-366356442579`, `pest-dashboard-366356442579`, `lambda-layers-366356442579` | RETIRED (dev account) | The development-era equivalents. Training data was copied out of the first of these to production on 2026-08-10; nothing writes to them now |
| `streaming-buckets` | DEAD | Gen-1 |
| `custom-labels-console-us-east-1-d1abc2aed2` | Auto-created | Made by the Rekognition Custom Labels console; do not hand-manage |
| `processed-images-moth` | Legacy (moth demo) | No account suffix — global-name collision risk if ever recreated |

S3 event notification: `argus-frames-506868652945`, event `s3:ObjectCreated:*`, prefix `frames/`, target `pest-detection-processor`. The prefix filter is load-bearing: without it every training-data upload would start a detection run.

### 9.3.3 DynamoDB tables

All pest tables: `PAY_PER_REQUEST`, TTL off, PITR off, no streams, tags `Project=armyworm-v2, Owner=Runzhe-armyworm, ManagedBy=Runzhe`.

| Table | Key schema | Status |
|---|---|---|
| `pest-monitoring-cameras` | `camera_id` (S) HASH | PROD — 3 rows (see 9.7) |
| `pest-monitoring-detections` | `image_id` (S) HASH + `detection_time` (S) RANGE; GSI `by-pest-time` (`pest_type` HASH + `detection_time` RANGE, projection ALL) | PROD. `image_id` = the exact S3 object key. Row stores `bboxes` (structured), `verifications` map, `llm_verify_model`, `llm_scan` |
| `pest-monitoring-system-config` | `config_key` (S) HASH | PROD — 1 row `detection_settings` (email_enabled, recipient_email, additional_recipients, auto_capture, capture_interval) |
| `pest-monitoring-schedule-logs` | `log_id` (S) HASH + `timestamp` (S) RANGE | PROD — runtime logs |
| `pest-monitoring-config` | `config_id` | DEAD (Gen-1, only referenced by the archived Flutter app) |
| `websocket-connections` | — | DEAD (retired WebSocket store) |

### 9.3.4 Lambda functions and layer

All PROD functions: runtime **Python 3.12**, handler `lambda_function.lambda_handler`, x86_64. Runtime and name are fixed at creation — changing either means delete + recreate, which wipes the resource policy (API Gateway invoke must be re-granted).

| Function | Mem / timeout | Role | Trigger | Source mirror |
|---|---|---|---|---|
| `pest-detection-processor` | 1024 MB / **600 s** (live read 2026-08-07/10; earlier docs say 512/60 or 1024/180 — doc lag) | `pest-detection-processor-role` | S3 ObjectCreated on `frames/` | `lambda/pest-detection-processor.py` (v6.3 — dead code stripped 2026-08-07, 3266 -> 1545 lines; pre-strip source in `lambda/archive/`) |
| `pest-monitoring-api` | 256 MB / 30 s | `pest-monitoring-api-role` | API Gateway `vzfl7s6z00` (21 routes) | `lambda/pest-monitoring-api.py` |
| `pest-camera-scheduler` | 128 MB / 60 s | `pest-camera-scheduler-role` | Invoked by the api (SCHEDULE_EXECUTOR_ARN) and by runtime `pest-sched-*` rules | `lambda/pest-camera-scheduler.py` |
| `kvs-hls-handler` | 128 MB / 30 s | `kvs-hls-handler-role` | API Gateway route `GET /video-playback` only | `deployer/audit/kvs-hls-handler_src/lambda_function.py` |
| `pest-model-watchdog` | 128 MB / 30 s | `pest-model-watchdog-role-78asdw2b` (service-role path; per the 2026-08-10 frozen baseline `migration/prod_baseline_20260810.json`) | EventBridge schedule `pest-model-watchdog-15min` (re-enabled 2026-08-05 with the v6.2 per-camera auto-close, see 9.3.10) | `lambda/pest-model-watchdog.py` (v6.2 — honours per-camera `max_runtime_min`; the older `deployer/audit/pest-model-watchdog_src/` snapshot is the pre-v6.2 global-cap version) |

Layer: `fyp-pillow:2` (Pillow 12.2.0, cp312, manylinux2014_x86_64, ~7.9 MB), attached to the processor only. Version `:1` is DEAD (missing the compiled `_imaging*.so`).

DEAD Lambdas (Gen-1 / Wilbur — do not recreate): `image-upload-handler`, `pest-detection-http`, `FrameExtractionControl`, `websocket-handler`, `kvs-hls-url-generator`, `ImageProcessing`, `pest-model-control`, `Scheduling`, `Extraction`, `ses-identity-manager`. Not pest: `lightsOutFunc`.

### 9.3.5 API Gateway

| Item | Value |
|---|---|
| HTTP API v2 name / id | `pest-monitoring-api-gateway` / **`vzfl7s6z00`** (retired dev: `zwpcbivmsj`) |
| Base URL | `https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com` (single `$default` auto-deploy stage, no stage suffix) |
| JWT authorizer | `cognito-dashboard`, id `enxa26`, IdentitySource `$request.header.Authorization`, Issuer = the Cognito pool, Audience = the app client id |
| Integrations | `t7ggvzn` → `pest-monitoring-api`; `33f69kt` → `kvs-hls-handler` (both AWS_PROXY, payload 2.0, 30 s) |
| CORS | AllowOrigins `*`, AllowMethods `GET,POST,DELETE,OPTIONS`, AllowHeaders `content-type,authorization`, MaxAge 3600 |
| DEAD APIs | REST `3go4jj1698` (PestDetectionAPI, already deleted); WebSocket `j4v2m5cbte` (dropped for polling in v3.7) |

Full route table — 21 routes. All require the JWT authorizer EXCEPT `GET /stream/status` (open — the Orin and mini PC poll it unauthenticated). All target `pest-monitoring-api` except `GET /video-playback`:

| Route | Handler function (in `pest-monitoring-api.py` unless noted) |
|---|---|
| `GET /settings` | read global config + camera list |
| `POST /settings` | write global config / camera fields (allow-listed, see 9.5) |
| `POST /model/start` | StartProjectVersion for a camera's model |
| `POST /model/stop` | StopProjectVersion |
| `GET /model/status` | DescribeProjectVersions state |
| `GET /presigned-url` | presigned S3 PUT for the dashboard Test upload |
| `DELETE /detection` | permanent delete of all rows for an image_id + its S3 frames (S3 deletes gated to `frames/` prefix in code AND IAM) |
| `POST /detection/verify` | write a manual verification verdict on a record |
| `GET /history` | detection records for the gallery |
| `GET /cost` | billing summary |
| `GET /identities`, `POST /identities`, `DELETE /identities` | SES recipient management |
| `GET /schedule`, `POST /schedule`, `DELETE /schedule` | per-camera capture schedules (creates runtime `pest-sched-*` EventBridge rules) |
| `GET /schedule-logs` | schedule run history |
| `POST /stream/start`, `POST /stream/stop` | flip `stream_enabled` on a camera row |
| `GET /stream/status` | **no auth** — polled by edge devices every ~5 s |
| `GET /video-playback` | → `kvs-hls-handler`: resolves the KVS HLS streaming URL |

### 9.3.6 Cognito and CloudFront

| Item | Value |
|---|---|
| Cognito User Pool | `pest-dashboard-users`, id **`us-east-1_9selFDHpc`**, app client `6vebotf45bp8u46cnraddiaplv` (`dashboard-web`), email sign-in, admin-create-only (no self sign-up), MFA off (retired dev pool: `us-east-1_ea0aJdusl`) |
| App client | `dashboard-web`, id **`4husu6afr835e235eu9dqp8av6`**, no secret, `USER_PASSWORD_AUTH` + refresh, 12 h id/access tokens, 30-day refresh |
| CloudFront distribution | **`E1YADURLSAVNFA`** → `https://d1dtoxef7qmugl.cloudfront.net` (retired dev: `E1423RGLAXWNSI` → `d1twcdquexdgj8.cloudfront.net`) |
| CloudFront config | Origin = S3 website endpoint of `argus-dashboard-506868652945` (http-only), cache policy Managed-CachingDisabled (`4135ea2d-6df8-44a3-9df3-4b5a84be39ad`, same id in every account), redirect-to-https, default CloudFront certificate |

The dashboard JS references only the region and the client id (direct USER_PASSWORD_AUTH); the pool id is used server-side by the API Gateway authorizer.

### 9.3.7 Rekognition Custom Labels — projects and versions

Hard constraint: trained Custom Labels models are **account-bound**. There is no export or cross-account copy. On a new account, retrain (Chapter 3, Chapter 8) — this is exactly what the production-account migration does (9.3.14). A model must be RUNNING to serve detections and bills per running hour — stop it after testing. The live model ARN lives in exactly ONE runtime place: the `custom_model_arn` field of the camera row in `pest-monitoring-cameras`; all Lambdas resolve it from there, nothing hardcodes an ARN.

Development-account versions (production-account versions are in 9.3.14):

| Version | F1 | Status | ARN |
|---|---|---|---|
| **armyworm v9r-prod — LIVE on `worm_cam`, trained on the production account 2026-08-10** | 0.613 | STOPPED between windows (watchdog auto-close, `max_runtime_min` 45) | `arn:aws:rekognition:us-east-1:506868652945:project/argus-detection/version/v9r-prod-20260810/1786377372187` |
| **moth-prod — LIVE on `moth_cam`, trained on the production account 2026-08-11** | 0.991 | STOPPED between windows | project `argus-moth-detection`, version `moth-prod-20260811` (account `506868652945`) |
| _Rows below are development-account history._ Custom Labels versions are account-bound and cannot be moved, so the production models above were retrained from the same recipes. | | | |
| armyworm v9 retrain "v9r" (dev, live on `worm_cam` 2026-08-07 to the migration) | 0.599 | STOPPED | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v9/version/v9-20260805-0713/1785913987295` |
| armyworm v9 first cut (live 2026-07-29 to 2026-08-07) | 0.591 | STOPPED | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v9/version/v9-20260725-1746/1785001598671` |
| armyworm v8 | 0.739 | STOPPED | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v8/version/v8-20260723-1703/1784826241523` |
| armyworm v7.5 | 0.737 | STOPPED | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v7-5/version/v7-5-20260723-0317/1784776673711` |
| armyworm v7.4 | 0.720 | STOPPED | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v7-4/version/v7-4-20260723-0259/1784775551708` |
| armyworm v7.3 | 0.822 | STOPPED | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v7-3/version/v7-3-20260722-0610/1784700642273` |
| armyworm v7.2 | 0.794 | STOPPED | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v7-2/version/v7-2-20260721-0301/1784602920896` |
| armyworm v7.1 | 0.744 | STOPPED | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v7/version/v7-1-20260720-0604/1784527489460` |
| armyworm v6 (REJECTED 2026-07-15) | 0.839 | STOPPED | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection/version/v6-2026-07-14/1784011732429` |
| armyworm v5 (live until the v9 switch) | 0.852 | STOPPED | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection/version/v5-2026-07-07/1783394123547` |
| armyworm v4 (oldest kept rollback) | 0.719 | STOPPED | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection/version/armyworm-detection.2026-05-21T12.46.19/1779338780450` |
| moth (Wilbur's, label `Moths`) | 0.988 | STOPPED (verified 2026-08-07 audit — `moth_cam` cannot detect until this endpoint is started) | `arn:aws:rekognition:us-east-1:366356442579:project/SmartPestProject/version/SmartPestProject.2026-02-15T01.13.58/1771089238911` |

Reading note: the v9-family low F1 is by design — the detector runs as a high-recall front end that gathers candidate boxes down to very low confidence, and the LLM gate does the rejecting. Do not read 0.599 as pipeline accuracy. Endpoint policy since v6.2: nothing stays RUNNING permanently — the watchdog stops an endpoint `max_runtime_min` minutes (45 for `worm_cam`) after it comes up, and any concurrently RUNNING endpoints bill concurrently.

Rollback = write the wanted ARN into `worm_cam.custom_model_arn`. Gotcha: the ARN contains colons — a raw `aws dynamodb update-item` from PowerShell mangles the quoting; use boto3 or a JSON file.

### 9.3.8 Bedrock (LLM verification)

| Item | Value |
|---|---|
| Live verifier model id | `us.anthropic.claude-sonnet-4-6` (Claude Sonnet 4.6; live since the v9-era denoiser config — earlier docs say Haiku 4.5, doc lag) |
| Code default (if env unset) | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| Routable aliases (v4.9, S3 key `__llm-<alias>`) | `sonnet46`, `haiku45`, `novapro` (`us.amazon.nova-pro-v1:0`), `llama4` (`us.meta.llama4-maverick-17b-instruct-v1:0`), `pixtral` (`us.mistral.pixtral-large-2502-v1:0`) |
| API | `bedrock-runtime` Converse, raw JPEG bytes (no pre-base64) |
| IAM | Inline policy `bedrock-verify` on `pest-detection-processor-role`: one statement on the inference-profile ARN, one on the region-wildcarded foundation-model ARNs. **Doc lag CLOSED 2026-08-11:** the live policy (covering the Sonnet 4.6 verifier) was dumped from the account into `deployer/audit/iam__pest-detection-processor-role__inline__bedrock-verify.json` (alongside `...__inline__s3-frames-write.json`) and wired into the deployer's ROLE_POLICIES, so a fresh deployment now grants the verifier correctly. The older `lambda/bedrock-policy.json` only grants Haiku 4.5 + Nova Lite — do not rebuild from it |

The #1 gotcha: the `us.` prefix is REQUIRED — it is a cross-region inference-profile id, not a foundation-model id. The bare id fails with "on-demand throughput isn't supported". Claude Sonnet 5 / Opus 5 remain AccessDenied on this account (AWS-side entitlement, not IAM) and are off the critical path.

### 9.3.9 Kinesis Video Streams

| Stream | Status | Purpose |
|---|---|---|
| `armyworm-cam-stream` | PROD | Live view of the A8 feed (GStreamer `rtspsrc → rtph264depay → h264parse → kvssink` on the Orin) |
| `moth-cam-stream` | Parked | Moth Hikvision live view via the mini PC VM |
| `FYP-PROJECT` | DEAD | Gen-1, 168 h retention |

Retention 2 h on the PROD streams, AWS-managed KMS. The HLS `DataEndpoint` is per-account/per-stream — `kvs-hls-handler` resolves it at runtime via `get-data-endpoint`; never hardcode it.

### 9.3.10 EventBridge schedules

EventBridge **Scheduler** schedules (group `default`; Scheduler invokes Lambda by assuming a role, not via `lambda add-permission`):

| Schedule | Expression | State | Target |
|---|---|---|---|
| `pest-model-watchdog-15min` | `rate(15 minutes)`, tz Asia/Singapore | **ENABLED** (disabled 2026-07-28 during testing, re-enabled with the 2026-08-05 v6.2 closeout: the watchdog now honours per-camera `max_runtime_min` — `worm_cam` 45 — with env `MAX_RUNTIME_MIN=75` as the global fallback; this is the auto-close half of the unattended morning run) | `pest-model-watchdog` |
| `model-start-schedule` | daily 05:53 SGT | **DELETED 2026-08-05** (Gen-1; the legacy start/stop chain was broken — fixed-time stop killed the model while still STARTING) | Gen-1 `pest-model-control` |
| `model-stop-schedule` | daily 06:01 SGT | **DELETED 2026-08-05** | Gen-1 `pest-model-control` |
| `frame-extraction-schedule` | daily 06:00 SGT | **DELETED 2026-08-05** | Gen-1 `Extraction` |
| `ExtractFrameEveryMinute` | every minute | **DELETED 2026-08-05** | Gen-1 |

Only the watchdog schedule remains on the development account. Since v6.2, per-camera capture scheduling is START-only: the dashboard schedule sets a start time, the API creates one classic rule, and the watchdog closes the endpoint after `max_runtime_min` — no stop time exists anywhere.

Classic EventBridge **rules** are created at runtime by `pest-monitoring-api` for per-camera capture schedules: name pattern `pest-sched-{camera_id}-{action}`, expression `cron(mm hh ? * DAYS *)` in UTC (the API converts SGT to UTC with day-shift), target `pest-camera-scheduler`, plus a per-rule `lambda:AddPermission` (StatementId `sched-invoke-<rule>`). None are pre-created, and the API deletes the rule when a schedule is disabled — `worm_cam`'s schedule is currently saved as 05:40 SGT daily but disabled (2026-08-05), so no `pest-sched-*` rule exists live.

Re-enabling a disabled schedule needs the FULL existing definition, not just the state: `aws scheduler update-schedule --name` **<schedule-name>** `--state ENABLED ...` with the whole schedule body.

### 9.3.11 SES

| Identity | Status |
|---|---|
| `rex2956550768@gmail.com` | PROD — both sender (`SENDER_EMAIL` on the processor) and recipient (`recipient_email` in system-config) |
| `teowilbur@gmail.com` | DEAD (predecessor) |
| `neobkee@gmail.com` | Not pest |

The account is in the SES **sandbox** (200 msgs/24 h, verified addresses only). Production access is a manual AWS Support request. No SNS, no SQS, no configuration sets.

### 9.3.12 IAM roles

| Role | Used by | Notable policies |
|---|---|---|
| `pest-detection-processor-role` | processor | inline `pest-detection-processor-policy` (S3 frames/processed, DDB detections/cameras, `rekognition:DetectCustomLabels` + `DetectLabels`, SES send); inline `read-system-config`; inline `bedrock-verify` (`bedrock:InvokeModel` + `bedrock:Converse`) |
| `pest-monitoring-api-role` | api | inline `pest-monitoring-api-policy` (S3 both buckets incl. `s3:DeleteObject` scoped to `frames/*`; DDB; Rekognition start/stop/describe; scheduler + events CRUD scoped to `pest-camera-*` / `pest-sched-*`; `iam:PassRole` to the scheduler invocation role; kinesisvideo read); inline `pest-monitoring-ddb-full-access`; inline `read-processed-armyworm`; managed `AmazonSESFullAccess`, `AWSBillingReadOnlyAccess`, `AWSLambdaBasicExecutionRole` |
| `pest-camera-scheduler-role` | scheduler Lambda | inline `pest-camera-scheduler-policy` |
| `kvs-hls-handler-role` | kvs-hls-handler | customer-managed `kvs-hls-handler-rolePolicy` (KVS GetDataEndpoint + GetHLSStreamingSessionURL) |
| `pest-model-watchdog-role` | watchdog | inline `Rekognition-DB-handler` |
| `Amazon_EventBridge_Scheduler_LAMBDA_d585792c00` | watchdog schedule target role | console-generated (the stably-named `pest-scheduler-invocation-role` in the deployer spec does NOT exist live) |
| `Amazon_EventBridge_Scheduler_LAMBDA_4c3f0b5d17` | Gen-1 schedules | legacy |
| `kvs-hls-url-generator-role-013j8khq` | Gen-1 | legacy |

### 9.3.13 Other

- EC2 instance `MonitoringSystem` (`i-0750c917a1f038d4d`, t3.micro, running): believed to be the older EC2+nginx dashboard host, superseded by S3 + CloudFront. OPEN ITEM: confirm it no longer serves anything, then stop or terminate it; it bills while running.
- CloudWatch: log groups `/aws/lambda/<function>` are auto-created, retention Never-Expire, no alarms, no metric filters.
- Empty on this account: SNS, SQS, Secrets Manager, SSM Parameter Store, ACM, Route53, WAF, KMS CMKs, custom event buses.

### 9.3.14 NP production account `506868652945` (migration in progress)

Stood up by the ARGUS deployer, 2026-08-10: full 15-stage headless deploy, 103 seconds, zero errors (`python deploy.py --profile prod --prefix argus ...`). This run doubles as the deployer's Round-2 rehearsal on a real fresh account. Post-deploy verification passed: processor 1024 MB / 600 s with the Pillow layer, env carries Sonnet 4.6 + `LLM_VERIFY_ALL_BOXES=true` + tile floor 8 + post-verify floor 49 + area cap 0.05; seeded camera rows read `llm_verify_enabled=true, min_confidence=10, post_verify_floor=49`. All CLI commands against this account use `--profile prod`.

| Item | Value |
|---|---|
| Account / IAM / profile | `506868652945` / `Student_QianRunzhe` (group `CAG_Proj` = AdministratorAccess) / `prod` |
| S3 buckets | `argus-frames-506868652945` (frames + `training-data/`), `argus-processed-506868652945`, `argus-dashboard-506868652945` |
| DynamoDB | same 4 `pest-monitoring-*` table names as the development account (the `argus` prefix renames buckets and Rekognition projects, not tables) |
| Lambdas | the same 5 functions, deployed from the repo `lambda/` sources (processor v6.3, watchdog v6.2) |
| API Gateway | HTTP API id **`vzfl7s6z00`** — `https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com`, 21 routes, JWT on all but `GET /stream/status` |
| Cognito | pool **`us-east-1_9selFDHpc`**, app client **`6vebotf45bp8u46cnraddiaplv`**; sign-in user created 2026-08-11 (admin-create-only, CONFIRMED) |
| CloudFront | **`E1YADURLSAVNFA`** → `https://d1dtoxef7qmugl.cloudfront.net` (Deployed; serves the ARGUS dashboard, verified 2026-08-11) |
| Rekognition project (armyworm) | `arn:aws:rekognition:us-east-1:506868652945:project/argus-detection/1786376502421` |
| Armyworm model version | **`v9r-prod-20260810`** — `arn:aws:rekognition:us-east-1:506868652945:project/argus-detection/version/v9r-prod-20260810/1786377372187`. Retrain of the v9 retrain recipe (added data augmentation: flips, rotations, exposure jitter). Datasets CREATE_COMPLETE: TRAIN 32,986 labelled, TEST 3,653 labelled. Submitted 2026-08-10 23:56; TRAINING_COMPLETED 2026-08-12, console F1 0.613 (the development account's equivalent version reads 0.599 — same data and recipe, so treat them as equivalent; a poller, `migration/poll_training.py`, watched the run). The version ARN is written into `worm_cam` (verified by live scan 2026-08-12; `manual_upload` keeps an empty ARN and takes the generic path). Validation against the development account's threshold-study zones is in flight — see the IAM-gap note below the table |
| Rekognition project (moth) | `argus-moth-detection` — version **`moth-prod-20260811`**, `arn:aws:rekognition:us-east-1:506868652945:project/argus-moth-detection/version/moth-prod-20260811/1786412382515`. **TRAINED 2026-08-11, F1 = 0.991** (statistically the same as the development-account moth model's 0.988 — same data). ARN already written into `moth_cam.custom_model_arn`; endpoint not yet started. Rebuilt from the surviving labelled data (116 TRAIN + 29 TEST images) because trained models are account-bound |
| EventBridge | watchdog schedule `rate(15 minutes)` created by the deployer; no `pest-sched-*` rules (worm schedule saved 05:40 daily, disabled) |
| SES | `rex2956550768@gmail.com` VERIFIED 2026-08-11 (sender + recipient). Account is in the SES **sandbox**, same as the development account |
| KVS / live view | **NOT migrated** — deploy ran without `--live-view`; no streams exist, `worm_cam.kvs_stream_name` is empty. The dashboard Live tab and the devices' `kvs_controller` cannot work until streams are created (open decision) |
| Cameras | `worm_cam` (deployer-seeded as `camera-1`, re-keyed to the reference id 2026-08-11), `manual_upload`, `moth_cam` (seeded by `migration/migrate_moth.py`). Full rows in 9.7 |
| Migrated records | 40 detection records: the complete 36-record Jewel on-site set (frames included, S3 keys byte-identical, `model_arn` left pointing at the development-account model as provenance) + 4 requested gallery captures |
| Bedrock | Sonnet 4.6 and Haiku 4.5 both authorized (verified 2026-08-10); no use-case form wait |
| Pre-existing relics | a previous student's Amplify "moobusapps" leftovers (13 roles, 1 deployment bucket) — harmless, do not touch |

IAM gap found by the first validation run (2026-08-12): the deployer had created `pest-detection-processor-role` WITHOUT the `bedrock-verify` and `s3-frames-write` inline policies (so no `bedrock:InvokeModel`/`Converse`, no `rekognition:DetectLabels`, no `s3:PutObject` on `frames/*`). The failure is silent and looks like success — every Bedrock call raises, the gate reports zero verdicts, the processor fails open to plain `min_confidence` (10), and frames come back covered in hundreds of unverified boxes. Fixed on the account by copying the development account's known-good policies (`migration/fix_processor_iam.py`); the role now matches the development account action-for-action, and `deployer/deploy.py` ROLE_POLICIES ships both policies so future deployments are correct (9.3.8). Lesson for any redeployment: validate by RESULT (push a known image, count surviving boxes), never by "all stages went green".

Training-data migration: `migration/copy_training_data.py` copied 36,641 objects / 4.27 GB server-side into `argus-frames-506868652945/training-data/v9/` (counts verified, manifest `source-ref`s repointed). The copy uses a temporary prefix-scoped read grant on the old bucket; run the scripts' `--revoke` mode after model sign-off (three grants are outstanding: training data, Jewel frames, moth console bucket).

Engineering lesson from this migration, generalised: **after any Rekognition console labelling, the S3 manifest is stale — the dataset is the source of truth.** Export the live entries with `ListDatasetEntries`, repoint every `source-ref`, upload as new manifests, then create the datasets (`migration/train_v9r_on_prod.py` and `migration/migrate_moth.py` both do this). Building from the copied S3 manifest alone silently trains a different model.

Remaining before the development account can be retired: the armyworm endpoint validation run (same holdout images, same detections — the ARN writeback is done; the 2026-08-12 first attempt was invalidated by the IAM gap above and must be repeated), restore `worm_cam.max_runtime_min` from the temporary 240 to 45 after sign-off, repoint the Orin / mini PC (bucket → `argus-frames-506868652945`, API base → the new API GW URL; camera ids unchanged), revoke the three cross-account read grants. The development account stays untouched until the new one is proven end to end, and holds the threshold-study evidence the final report cites.

## 9.4 Device registry

No passwords in this table. SSH is key-auth where stated; remaining passwords are held by Runzhe outside the repo.

| Host | IP / network | OS | User | Services / notes |
|---|---|---|---|---|
| Unitree Go2 EDU — Sport MCU | `192.168.123.161` (wired dog net) | vendor firmware | none (DDS only, no SSH) | **USLAM runs here**, auto-starts at power-on, auto-loads the last map. Control surface: plaintext `std_msgs/String` on `/uslam/client_command` + `/uslam/server_log`. An MCU wedge needs a full power cycle |
| Jetson Orin Nano (on the Go2) | `192.168.123.18` dog net; WiFi `10.1.125.24` at NP (`npwireless`), `10.38.19.10/23` gw `10.38.18.1` at Jewel (`apps-jewel`, hidden SSID `Apps@Jewel`, client isolation — no inbound SSH over Jewel WiFi) | Ubuntu 20.04, ROS 2 Foxy | `unitree` (SSH alias `go2`) | Patrol `~/go2/go2_patrol_gated.py`, survey `~/go2/pose.py`, `~/setup_go2.sh` (binds CycloneDDS), SIYI SDK at `~/a8/siyi_sdk`, KVS producer SDK (ARM64). AWS CLI is v1. `eth0` = `a8-link` (`192.168.144.30`, MAC-bound to ASIX AX88179B `6C:1F:F7:28:CD:0C`), `eth1` = dog link |
| SIYI A8 Mini gimbal camera | `192.168.144.25`; RTSP `rtsp://192.168.144.25:8554/main.264` (1080p cap); UDP control port `37260` | fw 0.2.8 | — | FOLLOW mode set in SIYI PC Assistant, persists. Pitch is INVERTED (+ = lens down, clamp [-90, +25]); LOCK mode required for absolute angles. Back on ethernet since 2026-07-29; the MS2109 USB capture card detour is retired (card maxes at 1080p, no 4K) |
| Mini PC — Win11 host | campus IP (e.g. `10.1.67.21`) | Windows 11 | — | Runs VMware hosting the VM below |
| Mini PC — Ubuntu VM | ens33 NAT `192.168.189.x` (no inbound); ens37 bridged `192.168.123.99/24` (dog net, only when wired) | Ubuntu 22.04 | `wilburteo` | systemd `kvs-controller.service` (`kvs_controller.py` via `run_kvs_controller.sh`, Restart=always, polls `GET /stream/status` every 5 s); systemd `reverse-tunnel-fyp.service` (self-healing `-R 2222:localhost:22` to the Orin; from the laptop: SSH to the Orin, then `ssh -p 2222 wilburteo@localhost`, key auth both ways) |
| Hikvision moth camera | `192.168.1.66` | — | — | Feeds `moth-cam-stream` (parked). RTSP credential stored on the mini PC, not reproduced |
| Laptop (ZenBook Air) | dock ASIX adapter static `192.168.123.50/24` for the dog net | Windows 11 | `Zenbook Air` (note the space) | Holds the working clone of the project repository; CLI profiles `nbk2` (dev) + `prod` (NP production), Posh-SSH for remote ops |

## 9.5 Environment-variable tables

Names and meanings only. No values are secret; email addresses and ids are shown, credentials never appear in env vars in this stack (AWS access on devices comes from per-device AWS CLI profiles — credential stored on each device, not reproduced).

### `pest-detection-processor` (code defaults in brackets; live overrides noted)

Core:

| Var | Meaning |
|---|---|
| `SENDER_EMAIL` | **required** — SES-verified alert sender (live `rex2956550768@gmail.com`) |
| `TABLE_DETECTIONS` / `TABLE_CAMERAS` / `TABLE_SYSTEM_CONFIG` | DynamoDB table names [defaults = the real names] |
| `S3_PROCESSED_BUCKET` | set on the live function but **not read by current code** (v4.2 stopped writing processed images) — NOTE: doc lag in older docs that call it required |

Tiling (cloud-side zoom scan, per-camera opt-in via `tiling_enabled`):

| Var [default] | Meaning |
|---|---|
| `TILING_ENABLED` [true] | global kill switch |
| `TILE_COLS` / `TILE_ROWS` [4 / 4] | grid |
| `TILE_OVERLAP` [0.15] | overlap fraction per tile |
| `TILE_UPSCALE_LONG_EDGE` [1920] | per-tile upscale target |
| `TILE_INCLUDE_FULL_FRAME` [true] | also detect on the whole frame |
| `TILE_MIN_CONFIDENCE` [30; **live 8** per the 2026-08-10 frozen baseline] | per-tile gather floor — a box under this never exists downstream (the first of the two stacked floors; the camera row's `min_confidence` is the second) |
| `TILE_NMS_IOU` [0.5], `TILE_MAX_WORKERS` [4] | NMS + parallelism |

Non-vegetation false-positive suppression (v4.3):

| Var [default] | Meaning |
|---|---|
| `SUPPRESS_NONVEG` [true] | drop worm boxes ≥ coverage inside hard-object `DetectLabels` regions |
| `SUPPRESS_MIN_CONF` [55], `SUPPRESS_COVERAGE` [0.5] | DetectLabels floor; coverage fraction |
| `SUPPRESS_LABELS` / `SUPPRESS_PROTECT` | hard-object list / never-suppress plant list |

LLM verification gate (v4.5+) and its extensions:

| Var [default] | Meaning |
|---|---|
| `LLM_VERIFY` [true] | global kill switch for the gate |
| `LLM_VERIFY_MODEL_ID` [Haiku 4.5 profile id; **live `us.anthropic.claude-sonnet-4-6`**] | Bedrock inference-profile id (`us.` prefix required) |
| `LLM_VERIFY_ALL_BOXES` [false; **live true**] | judge every box (denoiser mode), not only sub-`min_confidence` ones |
| `LLM_VERIFY_MAX_BOXES` [5; **live 120**] | cap on clusters judged per frame |
| `LLM_VERIFY_PAD` [0.6], `LLM_VERIFY_LONG_EDGE` [672], `LLM_VERIFY_MIN_CONTEXT_PX` [32], `LLM_VERIFY_MAX_UPSCALE` [8.0] | crop recipe (measured well-tuned — do not widen) |
| `LLM_VERIFY_WORKERS` [4; **live 3**], `LLM_VERIFY_TIMEOUT` [12], `LLM_VERIFY_MAX_TOKENS` [100; **live 300**] | parallelism / per-call limits |
| `LLM_VERIFY_TEMPERATURE` [unset] | keep UNSET — a "0" here 400s on newer models |
| `LLM_MERGE` [false; live false] | cluster-merge gate (v4.8) — measured harmful; code path DELETED in v6.3 |
| `LLM_SCAN` [live false] | v4.6 whole-frame recovery scan — code path DELETED in v6.3 |
| `LLM_FIRST` [false] | v5.1 LLM-first mode — measured worse; code path DELETED in v6.3 |
| `LLM_LEAD` [false; **live false**] | v5.2 Sonnet-leads experiment — retired; code path DELETED in v6.3 |
| `POST_NMS_IOU` [0; **live 0.1**] | v5.0 post-gate NMS (0 = off) |
| `POST_NMS_CONTAIN` [0; **live 0.1**] | post-gate containment-NMS (drop a box mostly contained in a kept box) |
| `POST_MAX_BOX_AREA` [0; **live 0.05**] | the 5% area cap — drop boxes covering more than this fraction of the frame (too big to be a larva) |
| `POST_VERIFY_FLOOR` [0; **live 49** — flipped 34→49 on 2026-08-10, the delivered gate] | confidence floor applied AFTER the LLM verdict; the camera row's `post_verify_floor` overrides it per camera |

v6.3 (2026-08-07) deleted the scan / merge / LLM-first / LLM-lead code paths and their `LLM_SCAN_* / LLM_MERGE_* / LLM_FIRST_* / LLM_LEAD_*` geometry knobs. The development-account function still carries some of those vars (set to false) — they are ignored. The production-account function omits them; behaviour is identical. The authoritative dev-account env read as of the migration is `migration/prod_baseline_20260810.json`.

Stateless S3-key overrides (not env vars, but part of the same contract): a key segment `__conf<N>` overrides the camera's `min_confidence` for that one run; `__llm-<alias>` picks the verifier model for that one run (aliases in 9.3.8).

### `pest-monitoring-api`

| Var | Meaning |
|---|---|
| `S3_FRAMES_BUCKET` | **required** — `frames-armyworm-366356442579` |
| `S3_PROCESSED_BUCKET` | **required** — legacy bucket (cold-start check + delete path) |
| `TABLE_CAMERAS` / `TABLE_DETECTIONS` / `TABLE_SYSTEM_CONFIG` / `TABLE_SCHEDULE_LOGS` | table names |
| `SCHEDULE_EXECUTOR_ARN` | ARN of `pest-camera-scheduler` (filled after that function exists — 2-phase deploy) |

Writable-field allow-lists in this Lambda (code constants, not env): `GLOBAL_ALLOWED` = email_enabled, recipient_email, additional_recipients, auto_capture, capture_interval; `CAMERA_ALLOWED` = label, target_label, model_type, custom_model_arn, min_confidence, model_running, default_waypoint_id, kvs_stream_name, stream_enabled, tiling_enabled, llm_verify_enabled.

### `pest-camera-scheduler`

| Var | Meaning |
|---|---|
| `TABLE_CAMERAS`, `TABLE_SCHEDULE_LOGS` | table names (defaults = real names) |

### `kvs-hls-handler`

No env vars. Region comes from the Lambda's `AWS_REGION` default.

### `pest-model-watchdog`

| Var | Meaning |
|---|---|
| `TABLE_CAMERAS` | table name |
| `MAX_RUNTIME_MIN` | live 75 — the GLOBAL FALLBACK: stop a RUNNING Rekognition endpoint after this many minutes. Since watchdog v6.2 a per-camera `max_runtime_min` field wins where set (`worm_cam` 45). The watchdog schedule was re-ENABLED 2026-08-05 with the v6.2 closeout (see 9.3.10) |

### Edge services

| Service (host) | Config item | Meaning |
|---|---|---|
| `kvs-controller.service` (mini PC VM) | `CAMERA_ID` | which camera row to poll — set BOTH in the systemd unit `Environment=` line AND as `export CAMERA_ID=moth_cam` on line 2 of `run_kvs_controller.sh`; the script export wins if they differ |
| `reverse-tunnel-fyp.service` (VM) | tunnel spec | `-R 2222:localhost:22` to the Orin, key auth, Restart=15 s self-healing |
| `go2_patrol_gated.py` (Orin `~/go2/`) | `INITIAL_POSE`, `WAYPOINTS` | per-map parameter sets live in `robot/map_profiles.md`; waypoints with `"capture": False` navigate only |
| `go2_patrol_gated.py` | `FOLLOW_SETTLE_S = 2.0` | gimbal settle wait per waypoint |
| `go2_patrol_gated.py` | `send_verb(repeat=1)` / `send_goal(repeat=1)` | MUST stay 1 — repeated USLAM verbs wedge the MCU (Chapter 5) |
| `setup_go2.sh` (Orin) | CycloneDDS binding | binds DDS to the dog link by IP `192.168.123.18`; source it in every Orin terminal |
| Capture script (Orin) | camera id `worm_cam` | uploads to `frames/worm_cam/...` in the frames bucket |

## 9.6 Repository file map

All paths are relative to the project repository root.

| Path | Contents |
|---|---|
| `CLAUDE.md` | behavioral brief for the AI assistant |
| `docs/` | `state.md` (living state + roadmap — start here), `aws.md`, `hardware.md`, `detection.md`, `dashboard.md`, `go2_demo_commands.md`, `project_timeline.md`, `model_ladder.md`, `deployer_training_pipeline.md`; `docs/history/` = frozen weekly records + retired `code.md` |
| `lambda/` | deployed source of the 3 core Lambdas (`pest-detection-processor.py`, `pest-monitoring-api.py`, `pest-camera-scheduler.py`) + `cors.json`, `ddb-policy.json`, `bedrock-policy.json`. The other two live Lambdas are mirrored under `deployer/audit/*_src/` |
| `robot/` | mirror of the Orin `~/go2/` set + `setup_go2.sh`, `pose.py`, `map_profiles.md`; `tests/` = nav validation harness + run logs |
| `minipc/` | mini-PC stack: `kvs_controller.py`, `run_kvs_controller.sh`, `kvs-controller.service`, `capture_and_upload_v4_armyworm.py`, `capture_and_upload_v3_person_cam.py` |
| `web/` | `dashboard_v4/` (current dashboard) + `_archive/` (holds `dashboard_v3_9.html`, the fallback, alongside v3_4-v3_8; unrelated to the top-level `archive/`) |
| `deployer/` | ARGUS one-click deployer: `deploy.py`, `STACK_MANIFEST.md` (authoritative BOM), `audit/` (raw live-account specs, incl. the live `bedrock-verify` + `s3-frames-write` role policies dumped 2026-08-11), `web/` (frozen deployer UI), `REHEARSAL.md` |
| `migration/` | account-migration toolkit (dev `366356442579` → NP production `506868652945`): `prod_baseline_20260810.json` (frozen dev-account config baseline — Lambdas, cameras, system-config, schedules; no secrets), `copy_training_data.py` (server-side S3 copy + manifest repoint + `--revoke`), `train_v9r_on_prod.py` (dataset export via `ListDatasetEntries` + retrain on the new account), `migrate_moth.py` (moth data + project + `moth_cam` row rebuild), `migrate_jewel_records.py` + `migrate_v9r49_picks.py` (detection-record migration), `diff_accounts.py` (old-vs-new account diff), `poll_training.py` (training-status poller), `fix_processor_iam.py` (prod processor-role policy repair, 2026-08-12), plus survey/verify helpers |
| `datasets/` | training data pipeline, restructured into `live` / `staging` / `sources` / `holdout` / `archive` (+ `current/` in transition); `holdout/cag/` = the sacred CAG holdout. Full map: `datasets/README.md`. **Caution: `datasets/archive/experiments/pre_v3_abandoned/download.py` contains a live Roboflow API key — credential stored here, not reproduced; do not publish this path** |
| `reports/` | deliverables: `proposal/`, `interim/`, `weekly/` (W1+ docx), `presentation/`, `manual/` (this manual), `final/` (final report: `REPORT_PLAN.md`, `figures/` + `build_figures.py`, `draft/` = full chapter drafts ch00-ch10 + appendix, 2026-08-11) |
| `reference/wilbur/` | predecessor material (final report, decks, old Flutter dashboard). Reference only. **Caution: Wilbur's final-report docx has an AWS secret key in cleartext in its appendix — credential stored here, not reproduced; never publish unscrubbed** |
| `context/claude-chat/` | immutable archives of prior Claude chat accounts (historical evidence, not current state) |
| `archive/` | ALL other historical material (chat imports, snapshots, old lambda snapshot). Nothing load-bearing |

## 9.7 Camera registry

### Development account `366356442579`

Three rows in `pest-monitoring-cameras` (test/legacy rows `notile_test`, `batch2_notile`, `person_cam` deleted 2026-07-13; ids migrated 2026-07-14: `armyworm_go2_a8mini` → `worm_cam`, `moth_cam_01` → `moth_cam`; historical S3 keys under `frames/armyworm_go2_a8mini/...` untouched, records still point at them). Live values below verified against the frozen 2026-08-10 baseline `migration/prod_baseline_20260810.json` plus the 2026-08-10 floor flip.

| Field | `worm_cam` | `moth_cam` | `manual_upload` |
|---|---|---|---|
| Display label | Worm Cam | Moth Cam | Manual test upload (hidden fallback) |
| Device | SIYI A8 Mini on the Go2, captured by the Orin | Hikvision `192.168.1.66` via the mini PC VM | dashboard Test upload panel |
| `target_label` | `armyworm-larva` | `Moths` | `Person` |
| `model_type` | custom | custom | general (plain DetectLabels) |
| `custom_model_arn` | armyworm **v9 retrain `v9-20260805-0713`** ARN (9.3.7) | moth SmartPestProject ARN (9.3.7) | empty |
| `min_confidence` | **10** (lowered from 75 — the candidate floor; the LLM gate and `post_verify_floor` do the rejecting) | 80 | 80 |
| `post_verify_floor` | **49** (34→49, decided + flipped live 2026-08-10 — the delivered gate) | — | — |
| `max_runtime_min` | **45** (restored from a temporary 240 on 2026-08-10; watchdog v6.2 honours it) | — | — |
| `tiling_enabled` | **true** | false | false |
| `llm_verify_enabled` | **true** (only camera opted in — the verify prompt asks about larvae, so it must never run for moth_cam) | false / unset | false / unset |
| `kvs_stream_name` | `armyworm-cam-stream` | `moth-cam-stream` | none |
| `stream_enabled` | false (patrol uploads stills; stream on demand) | false | false |
| `schedule` | 05:40 SGT daily, **disabled** | 09:00-17:00, disabled | — |

### NP production account `506868652945`

Three rows, seeded 2026-08-10/11 (the worm camera + `manual_upload` by the ARGUS deployer, `moth_cam` by `migration/migrate_moth.py`). The deployer seeded the worm camera as `camera-1`; the row was re-keyed to **`worm_cam`** in the 2026-08-11 reconciliation, so device upload scripts keep their camera id.

| Field | `worm_cam` | `moth_cam` | `manual_upload` |
|---|---|---|---|
| Display label | Worm Cam (deployer wrote the deployment name into it; restored to "Worm Cam" 2026-08-11 — the dashboard shows the label, so the label IS functionality) | Moth Cam | Manual test upload |
| `target_label` | `armyworm-larva` | `Moths` | `armyworm-larva` (differs from the dev row's Person/general by design; behaviour is generic while `custom_model_arn` is empty) |
| `model_type` | custom | custom | custom |
| `custom_model_arn` | **`v9r-prod-20260810`** ARN (9.3.14), written after training completed | **`moth-prod-20260811`** ARN (9.3.14), written 2026-08-11 | empty |
| `min_confidence` | **10** | 80 | **10** |
| `post_verify_floor` | **49** | — | **49** |
| `max_runtime_min` | seeded **45**; live read 2026-08-12 shows **240** (temporarily raised for the post-training validation window — restore to 45 after sign-off, or the watchdog lets a $4/hr endpoint run 4 hours) | 45 | — (not seeded; global watchdog fallback 75 applies) |
| `tiling_enabled` | **true** | false | — |
| `llm_verify_enabled` | **true** | **false — on purpose** (the verify prompt asks about larvae; this camera's target is adult moths) | **true** |
| `kvs_stream_name` | empty (KVS not migrated) | `moth-cam-stream` (stream does not exist on this account yet) | none |
| `schedule` | 05:40 SGT daily, **disabled** (deployer default 09:00-17:00 corrected 2026-08-11 to match the development account's intent) | 09:00-17:00, disabled | — |

Field semantics (both accounts): `min_confidence` is NOT a plain detection floor any more — since v4.5 it is "the point above which Rekognition's word is final"; below it the LLM gate decides (and in live denoiser mode every box is judged). `post_verify_floor` is the confidence floor applied AFTER the LLM verdict — the delivered 49 gate. `max_runtime_min` is how long the watchdog lets that camera's Rekognition endpoint run. `tiling_enabled` and `llm_verify_enabled` are per-camera opt-ins on this shared processor. `llm_verify_enabled` is in the API's `CAMERA_ALLOWED` so it CAN be flipped via `POST /settings`, but no dashboard control exists — DynamoDB/CLI is the practical toggle.

## 9.8 Glossary of project terms

| Term | Meaning |
|---|---|
| **ARGUS** | Project name of the deliverable system: the v5.2 vanilla-JS dashboard (cloud-deployed with Cognito) + the one-click deployer. The dashboard IS the final deliverable (settled 2026-07-15; the old "Flutter Web target" claim was a miscapture — do not resurrect) |
| **batch_1** | 13 CAG images (23 worms), hand-held close-ups. Used for internal spot checks only — evaluation numbers in outward-facing documents are quoted solely from never-trained images: batch_2 102-109, Jewel_1/2, and the 4 field-realistic photos |
| **batch_2** | The sacred CAG holdout: originally 7 images, grown to 10-11 labelled images / 11 worms (110/111 added 2026-07-28). **Never train on it.** The only fair evaluation set; contains NO true negatives, so image-level FP rate is not measurable on it |
| **gate (patrol gate)** | The Orin patrol's per-waypoint wait: capture + upload → poll DynamoDB `get_item(image_id=key)` every ~2 s → record appears (clean frames also write a record, so no deadlock) → next waypoint |
| **gate (LLM gate)** | The Bedrock verification step in the processor: Rekognition proposes boxes, the LLM judges which survive. In the live denoiser mode survival is fail-closed per box (rejected AND un-judged boxes are dropped); the fail-open path exists only for total gate failure (zero verdicts returned, infrastructure down), when all boxes are kept and the caller falls back to plain thresholding |
| **denoiser mode** | `LLM_VERIFY_ALL_BOXES=true`: Rekognition runs as a high-recall front end (low tile floor), the LLM rejects the noise. Explains v9's deliberate low F1 |
| **gen-1 / legacy** | Wilbur's original stack (Gen-1 Lambdas, REST + WebSocket APIs, `FYP-PROJECT` stream, EC2 dashboard). Dead; listed in 9.3 so nobody recreates it |
| **prod** | The AWS CLI profile name for the production account `506868652945`. Every operative command in this manual uses it |
| **nbk2** | The CLI profile of the retired development account `366356442579`. It appears only in historical notes; nothing in the live system uses it |
| **whole-image protocol** | The retired "never tile, judge the whole frame" evaluation rule. SUPERSEDED: `tiling_enabled=true` on worm_cam (2026-07-27/28); Rekognition finds, the LLM verifies |
| **tiling / zoom scan** | Cloud-side split of the frame into an overlapping grid, per-tile upscale + detect, global-coordinate NMS. Dashboard name: "Zoom scan" |
| **two stacked floors** | `TILE_MIN_CONFIDENCE` (inside tiling, before NMS) and the camera row's `min_confidence` (after tiling). Lowering only the second cannot recover what the first already discarded |
| **`__confN` / `__llm-<alias>`** | Stateless S3-key overrides: per-upload `min_confidence` / per-upload verifier model. Lets N experiment arms run concurrently with no Lambda config swaps |
| **Zone** | Dashboard grouping field (waypoint id). Experiment arms write to Zones named after themselves, so the gallery labels the arm |
| **holdout** | Images deliberately excluded from all training, used only for evaluation (`datasets/holdout/cag/`) |
| **manual_upload** | Hidden camera row backing the dashboard Settings → Test upload panel — the way live detection is demonstrated, since there are no worms at Jewel |
| **USLAM** | Unitree's SLAM/nav stack. Runs on the Go2 Sport MCU (not the Orin), driven by plaintext string commands. See Chapter 5 for its failure modes (wedge, stale localization) |
| **wedge (MCU wedge)** | USLAM failure state from rapid repeated control verbs: thousands of `TIMEOUT_ODOMETRY` events, odom publishers drop to 0, only a power cycle clears it. Send every verb once |
| **CL** | Rekognition Custom Labels. Cannot be trained on negatives; models are account-bound |
| **inference profile** | The `us.`-prefixed Bedrock model id required for cross-region on-demand invocation. The bare foundation-model id fails |
| **NOMERGE** | Experiment arms with `LLM_MERGE=false` (per-box judging). The merge gate was measured to destroy detections; live config keeps it off |
| **LLM_LEAD** | v5.2 experiment: Sonnet 4.6 swept native-resolution tiles and led detection, with Rekognition refining box geometry. RETIRED — code path deleted in v6.3; the delivered architecture is the reverse (Rekognition finds, the LLM verifies) |
| **clean image** | A frame with zero surviving boxes. Clean frames still write a DynamoDB record (unconditional `put_item`) — the patrol gate depends on this |
| **sandbox (SES)** | SES restricted mode: send only to/from verified identities |
| **Gen-2** | The current 5-Lambda stack described in this manual, as opposed to gen-1 |

## 9.9 Document conventions and change log

Conventions used throughout this manual:

- English only, plain and short. Real proper nouns are kept exactly: SIYI A8 Mini, Unitree Go2, Jetson Orin, AWS Rekognition Custom Labels, USLAM, ARGUS, CAG, Jewel Changi.
- Every AWS procedure gives BOTH the console click-path and the CLI command. In commands, **bold** marks a value the reader must fill in themselves; plain code is a literal value of this deployment.
- The code is ground truth. `docs/*.md` provide context and sometimes lag; rows above marked "doc lag" record known disagreements, resolved in the code's favor.
- "OPEN ITEM" marks a fact this chapter could not resolve from the repository alone; each has a one-command check listed in 9.10.
- Secrets never appear. "Credential stored here, not reproduced" marks a known secret location.

Change log (append one row per revision):

| Date | Version | Author | Change |
|---|---|---|---|
| 2026-07-22 | 1.0 | Runzhe (with Claude) | First complete appendix: all registries, glossary, conventions |
| 2026-08-11 | 1.1 | Runzhe (with Claude) | Dual-account update: NP production registry (9.3.14) and prod camera rows (9.7); dev rows refreshed to the v9-retrain / floor-49 / v6.3 state; `migration/` and `reports/final/draft/` added to the file map; all repository paths made repo-relative |
| | | | |

## 9.10 Operations / reproduction

Concrete inspection procedures for every registry above. The commands are written against the live production account `506868652945` with CLI profile `prod`. On a stack reproduced elsewhere, replace `--profile prod` with **your own profile**, `506868652945` with **your account id**, and the `argus-*` bucket names with the ones your deployment prefix produced; DynamoDB table names and Lambda function names are the same on every deployment.

**Lambda inventory and live config.** Console: Lambda → Functions → filter `pest`. CLI:

    aws lambda list-functions --profile prod --region us-east-1 --query "Functions[?starts_with(FunctionName,'pest')].[FunctionName,Runtime,MemorySize,Timeout]" --output table
    aws lambda get-function-configuration --function-name pest-detection-processor --profile prod --region us-east-1

The second command is the authoritative read of the processor's live env vars (this is how the live values in 9.5 were captured).

**API Gateway routes and authorizer.** Console: API Gateway → HTTP APIs → `pest-monitoring-api-gateway` (`vzfl7s6z00`) → Routes / Authorization. CLI:

    aws apigatewayv2 get-routes --api-id vzfl7s6z00 --profile prod --region us-east-1 --output table
    aws apigatewayv2 get-authorizers --api-id vzfl7s6z00 --profile prod --region us-east-1

**DynamoDB tables and camera rows.** Console: DynamoDB → Tables → `pest-monitoring-cameras` → Explore table items. CLI:

    aws dynamodb list-tables --profile prod --region us-east-1
    aws dynamodb scan --table-name pest-monitoring-cameras --profile prod --region us-east-1

The scan is the definitive live read of the camera rows in 9.7: it shows `custom_model_arn`, `min_confidence`, `tiling_enabled`, `llm_verify_enabled` per camera.

**Rekognition versions and full ARNs.** Console: Rekognition → Use custom labels → Projects → pick the project → Models. CLI (this is how the full ARN suffixes in 9.3.7 are verified):

    aws rekognition describe-projects --profile prod --region us-east-1 --query "ProjectDescriptions[].ProjectArn"
    aws rekognition describe-project-versions --project-arn **<project ARN from the previous command>** --profile prod --region us-east-1 --query "ProjectVersionDescriptions[].[ProjectVersionArn,Status,EvaluationResult.F1Score]"

**Endpoint state before/after testing.** Same `describe-project-versions` call; `Status` RUNNING means it is billing. Stop with:

    aws rekognition stop-project-version --project-version-arn "**<version ARN>**" --profile prod --region us-east-1

**EventBridge schedules.** Console: Amazon EventBridge → Scheduler → Schedules. CLI:

    aws scheduler list-schedules --profile prod --region us-east-1 --output table
    aws scheduler get-schedule --name pest-model-watchdog-15min --profile prod --region us-east-1

Runtime per-camera rules: Console: EventBridge → Rules (filter `pest-sched-`). CLI: `aws events list-rules --name-prefix pest-sched- --profile prod --region us-east-1`.

**Cognito.** Console: Cognito → User pools → `pest-dashboard-users`. CLI:

    aws cognito-idp describe-user-pool --user-pool-id us-east-1_9selFDHpc --profile prod --region us-east-1
    aws cognito-idp describe-user-pool-client --user-pool-id us-east-1_9selFDHpc --client-id 6vebotf45bp8u46cnraddiaplv --profile prod --region us-east-1

**CloudFront.** Console: CloudFront → Distributions → `E1YADURLSAVNFA`. CLI: `aws cloudfront get-distribution --id E1YADURLSAVNFA --profile prod`.

**S3 buckets, CORS, notification.** Console: S3 → pick the bucket → Permissions / Properties. CLI:

    aws s3api get-bucket-notification-configuration --bucket argus-frames-506868652945 --profile prod
    aws s3api get-bucket-cors --bucket argus-frames-506868652945 --profile prod

**KVS.** Console: Kinesis Video Streams → Video streams. CLI: `aws kinesisvideo list-streams --profile prod --region us-east-1`.

**SES.** Console: Amazon SES → Identities. CLI: `aws ses list-identities --profile prod --region us-east-1` and `aws sesv2 get-account --profile prod --region us-east-1` (shows sandbox state).

**IAM roles.** Console: IAM → Roles → filter `pest`. CLI: `aws iam list-role-policies --role-name pest-detection-processor-role --profile prod` then `aws iam get-role-policy --role-name pest-detection-processor-role --policy-name **<policy name>** --profile prod`.

**Bedrock model access.** Console: Bedrock → Model access. CLI: `aws bedrock list-inference-profiles --profile prod --region us-east-1` (the valid `us.` ids) and `aws bedrock get-foundation-model-availability --model-id **<model id>** --profile prod --region us-east-1`.

**Reproduction.** This chapter is the target-state checklist; the build procedure itself is Chapter 8 (manual) and Chapter 7 (the ARGUS deployer, driven by `deployer/STACK_MANIFEST.md` — the authoritative creation order, parameter table, and per-resource raw specs under `deployer/audit/`). On a fresh account, everything in 9.3 marked PROD must exist; everything marked DEAD/legacy must not be recreated; Rekognition models must be retrained (account-bound); and every value in the STACK_MANIFEST parameter table (`ACCOUNT_ID`, `REGION`, pool/client/API ids, emails, ARNs) must be replaced with the new account's values. This is no longer theoretical: the 2026-08-10 deploy onto the NP production account (9.3.14) executed exactly this procedure — 15 stages, 103 seconds — and its section is what a correct fresh deployment looks like.

## 9.11 Cross-references

- Chapter 1 — where each registry entry sits in the end-to-end chain.
- Chapter 2 — the AWS cloud backend: the Lambdas, tables, buckets, and API of 9.3 in working detail.
- Chapter 3 — the model versions of 9.3.7: what each was trained on and why v9 + LLM gate is the live pair.
- Chapter 4 — the ARGUS dashboard: consumer of the API routes (9.3.5) and Cognito/CloudFront ids (9.3.6).
- Chapter 5 — the Go2 + Orin devices of 9.4 and the patrol constants of 9.5.
- Chapter 6 — the mini PC devices and services of 9.4/9.5.
- Chapter 7 — the ARGUS deployer that recreates 9.3 from `deployer/STACK_MANIFEST.md`.
- Chapter 8 — the from-scratch reproduction runbook that 9.10 points into.
