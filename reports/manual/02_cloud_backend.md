# Chapter 2 — AWS cloud backend

This chapter documents every production cloud resource of ARGUS: S3, Lambda, DynamoDB, API Gateway, Cognito, CloudFront, Kinesis Video Streams, EventBridge, SES, IAM, and the Bedrock and Rekognition touch points, with the configuration and code needed to operate and reproduce each one.

_As of 2026-08-14._ (Facts with a different date are marked with that date. The live camera-row values quoted in 2.4/2.5 were read from the production DynamoDB table on 2026-08-14. `migration/prod_baseline_20260810.json` is the deploy-day freeze; it predates the 2026-08-13 floor refit and still shows floor 49. File paths in this chapter are relative to the project repository root.)

**The system runs on the NP production account.** ARGUS was built and validated on the development account **366356442579** (CLI profile `nbk2`, now retired to history/evidence duty). On 2026-08-10 the ARGUS deployer stood the full stack up on the NP production account **506868652945** (IAM user `Student_QianRunzhe`, CLI profile `prod`): all 15 stages in 103 seconds. Both models were then retrained there (`moth-prod-20260811` F1 0.991, trained 2026-08-11; armyworm `v9r-prod-20260810` F1 0.613, trained 2026-08-12, ARN wired into the camera rows), the end-to-end validation run passed 2026-08-12, and the handover snapshot `argus-repo-snapshot-20260813.zip` was published to `s3://argus-frames-506868652945/handover/` on 2026-08-13. This chapter documents the production account as the operative system; development-account identifiers are kept as marked history (2.2.1). KVS/live view is the one part not stood up on production (2.14).

## 2.1 Role in the system

The cloud backend is the middle of the end-to-end chain. Upstream, a camera (the SIYI A8 Mini on the Unitree Go2 during demos, fixed cameras in the production concept, or the dashboard's Test upload panel) puts a JPEG frame into an S3 bucket under `frames/{camera_id}/{waypoint_id}/{filename}`. Downstream, the vanilla-JS dashboard reads results over an authenticated HTTP API and draws bounding boxes on the original frame.

Everything between those two points is serverless and event-driven. An S3 event notification invokes the `pest-detection-processor` Lambda for every new frame. That Lambda runs AWS Rekognition Custom Labels detection (tiled or whole-frame per camera), filters false positives with a Rekognition DetectLabels pass, adjudicates candidate boxes with a Bedrock multimodal model, writes one DynamoDB record per frame (clean frames included), and sends an SES alert when a pest is found. A second Lambda, `pest-monitoring-api`, serves all dashboard routes behind an API Gateway HTTP API with a Cognito JWT authorizer. Three small Lambdas handle scheduled model start/stop, KVS HLS playback URLs, and a cost watchdog that stops any Rekognition endpoint left running.

The design principle that shapes the whole backend: the DynamoDB detection record is the contract. The Go2 patrol gate, the dashboard gallery, and the model test scripts all poll or query the same `pest-monitoring-detections` table keyed by the exact S3 object key. Nothing depends on annotated images being generated (none are, since processor v4.1); the dashboard draws boxes client-side from stored coordinates.

Production account: **506868652945**, region **us-east-1**, CLI profile `prod` (IAM user `Student_QianRunzhe`, AdministratorAccess via group `CAG_Proj`, no permission boundary). Every command in this chapter targets this account unless it is explicitly marked as history. The production account is not empty — a previous student's Amplify "moobusapps" relics (13 roles, one deployment bucket) exist there; leave them alone. The retired development account (history and evidence only): **366356442579**, CLI profile `nbk2`. It is shared with unrelated projects; only the resources mapped in 2.2.1 belonged to ARGUS. Its one CLI quirk, kept for anyone auditing the old evidence: CloudShell writes are blocked by a VPC endpoint there — use a local CLI with `--profile nbk2` for writes, CloudShell for reads only.

## 2.2 Inventory

| Component | Location / identifier | Purpose |
|---|---|---|
| Frames bucket | `argus-frames-506868652945` | Frame ingest; S3 event triggers the processor; also holds the migrated training sets under `training-data/` and the handover snapshot under `handover/` |
| Processed bucket | `argus-processed-506868652945` | Legacy annotated-image store; empty since processor v4.1, kept because the API's `S3_PROCESSED_BUCKET` env var and the delete path reference it |
| Dashboard bucket | `argus-dashboard-506868652945` | Static website hosting for `web/dashboard_v4/` (public read) |
| Layer bucket | `lambda-layers-366356442579` (dev account only, history) | Held the unused Gen-1 ffmpeg layer zip only; no production counterpart exists or is needed |
| `pest-detection-processor` | Mirror `lambda/pest-detection-processor.py` | S3-triggered detection pipeline (tiling, FP suppression, LLM denoiser gate, post-gate cleanup, DDB write, SES) |
| `pest-monitoring-api` | Mirror `lambda/pest-monitoring-api.py` | All dashboard HTTP routes; DynamoDB read/write, model start/stop, schedules, identities, cost, delete |
| `pest-camera-scheduler` | Mirror `lambda/pest-camera-scheduler.py` | Executes scheduled Rekognition model start/stop from EventBridge rules |
| `kvs-hls-handler` | Mirror `deployer/audit/kvs-hls-handler_src/lambda_function.py` | Generates KVS HLS playback URLs for the dashboard Live tab |
| `pest-model-watchdog` | Mirror `lambda/pest-model-watchdog.py` (v6.2, per-camera limit; the pre-v6.2 audit copy is `deployer/audit/pest-model-watchdog_src/lambda_function.py`) | Auto-stops Rekognition endpoints after a runtime limit |
| Lambda layer `fyp-pillow` | `arn:aws:lambda:us-east-1:506868652945:layer:fyp-pillow:1` | Pillow 12.2.0 for the processor's tiling and cropping |
| DynamoDB `pest-monitoring-cameras` | table, PK `camera_id` | Per-camera config incl. model ARN, `tiling_enabled`, `llm_verify_enabled` |
| DynamoDB `pest-monitoring-detections` | table, PK `image_id` + SK `detection_time`, GSI `by-pest-time` | One record per processed frame |
| DynamoDB `pest-monitoring-system-config` | table, PK `config_key` | Single-row global settings (email recipients, capture flags) |
| DynamoDB `pest-monitoring-schedule-logs` | table, PK `log_id` + SK `timestamp` | Audit rows from the scheduler Lambda |
| API Gateway HTTP API | `vzfl7s6z00` (`pest-monitoring-api-gateway`) | 21 routes, Cognito JWT authorizer, one open route |
| Cognito | pool `us-east-1_9selFDHpc`, client `6vebotf45bp8u46cnraddiaplv` | Dashboard login, admin-create-only |
| CloudFront | `E1YADURLSAVNFA` → `https://d1dtoxef7qmugl.cloudfront.net` | HTTPS front for the dashboard bucket, CachingDisabled |
| KVS | `armyworm-cam-stream`, `moth-cam-stream` (dev account only; not created on production — 2.14) | Live-view ingest from the Jetson Orin GStreamer producer |
| EventBridge Scheduler | `pest-model-watchdog-15min` + runtime `pest-sched-*` rules | Watchdog cadence; per-camera model schedules |
| SES | identity `rex2956550768@gmail.com` (sandbox) | Detection alert email, sender and default recipient |
| IAM | 5 execution roles + scheduler invocation role | Per-function policies (2.17) |
| Policy files | `lambda/cors.json`, `lambda/ddb-policy.json`, `lambda/bedrock-policy.json` | Source JSON applied to API GW CORS and the processor/api roles |
| Rekognition Custom Labels | projects `argus-detection` (armyworm), `argus-moth-detection` (moth) | The detector models; ARNs stored per camera in DynamoDB, never hardcoded |

### 2.2.1 Development account (366356442579) identifiers — retired 2026-08-10

ARGUS was built and validated on the development account, then stood up on production by the ARGUS deployer (`deployer/deploy.py`, run 2026-08-10 with `--prefix argus`). Table/Lambda/role/layer names are unchanged across accounts, and so are the DynamoDB table names and KVS stream names; the account-suffixed and generated identifiers differ. The development column below is history — use it only to read old evidence, never in a "how to do it now" command:

| Resource | Development (366356442579) | Production (506868652945) |
|---|---|---|
| Frames bucket | `frames-armyworm-366356442579` | `argus-frames-506868652945` |
| Processed bucket | `processed-images-armyworm-366356442579` | `argus-processed-506868652945` |
| Dashboard bucket | `pest-dashboard-366356442579` | `argus-dashboard-506868652945` |
| API Gateway HTTP API | `zwpcbivmsj` | `vzfl7s6z00` (`https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com`) |
| Cognito pool / client | `us-east-1_ea0aJdusl` / `4husu6afr835e235eu9dqp8av6` | `us-east-1_9selFDHpc` / `6vebotf45bp8u46cnraddiaplv` |
| CloudFront | `E1423RGLAXWNSI` → `https://d1twcdquexdgj8.cloudfront.net` | `E1YADURLSAVNFA` → `https://d1dtoxef7qmugl.cloudfront.net` |
| Rekognition projects | `armyworm-detection-v9`, `SmartPestProject` | `argus-detection` (armyworm; version `v9r-prod-20260810`, trained 2026-08-12, F1 0.613), `argus-moth-detection` (version `moth-prod-20260811`, trained 2026-08-11, F1 0.991) |
| fyp-pillow layer | `...layer:fyp-pillow:2` | `...layer:fyp-pillow:1` (fresh publish) |
| KVS streams | `armyworm-cam-stream`, `moth-cam-stream` | none (live view not migrated) |

Device-side consequence of the rename: the Orin / mini-PC upload target and API base had to move to `argus-frames-506868652945` and the `vzfl7s6z00` API. The repo mirrors (`robot/go2_patrol_gated.py`, `robot/capture_4k_hdmi.py`, `robot/kvs_controller.py`, `minipc/kvs_controller.py`, `minipc/capture_and_upload_v4_armyworm.py`) were repointed 2026-08-13; syncing them onto the devices and swapping the on-device AWS credentials (both still carry old-account keys) is pending the hardware's return.

## 2.3 S3 buckets

### 2.3.1 argus-frames-506868652945 (the ingest bucket)

Purpose. Every frame enters the system as an object under `frames/{camera_id}/{waypoint_id}/{filename}`. The bucket also stores non-frame material under other prefixes — the migrated training sets under `training-data/v9/` and `training-data/moth/`, and the handover snapshot under `handover/` — which is why the delete path in the API is prefix-gated (2.6).

Configuration (created by the ARGUS deployer to the same shape as the audited development bucket; the dev-account snapshot `deployer/audit/s3__frames-armyworm__*.json` is the template):
- Versioning ON, SSE-S3 AES256, all four Block Public Access flags TRUE, no bucket policy.
- CORS (needed because the dashboard PUTs test uploads and GETs frames with presigned URLs directly from the browser): AllowedMethods GET, PUT, POST, HEAD; AllowedHeaders `*`; ExposeHeaders ETag; MaxAge 3000; AllowedOrigins:
  - `https://d1dtoxef7qmugl.cloudfront.net`
  - `http://argus-dashboard-506868652945.s3-website-us-east-1.amazonaws.com`
  - `http://localhost:5500`, `http://127.0.0.1:5500`, `http://localhost:5501`, `http://127.0.0.1:5501`
- Event notification: `s3:ObjectCreated:*`, key prefix filter `frames/`, target Lambda `pest-detection-processor`. The prefix filter keeps training-data and handover uploads from triggering detection (publishing the 188.9 MiB snapshot to `handover/` fired zero invocations). Note that the processor's EXIF write-back (2.5.1 step 5) rewrites the same `frames/` key, so a rotated upload re-triggers the Lambda once; the second pass finds an already-upright image, does not rewrite again, and the loop terminates after one extra invocation.

Note on `lambda/cors.json`: that file is the API Gateway CORS document (AllowHeaders content-type, authorization), not this bucket's CORS. The bucket CORS lives in the audit JSON above.

Inspect:
- Console: S3 → Buckets → `argus-frames-506868652945` → Permissions tab (CORS) and Properties tab → Event notifications.
- CLI: `aws s3api get-bucket-cors --bucket argus-frames-506868652945 --profile prod` and `aws s3api get-bucket-notification-configuration --bucket argus-frames-506868652945 --profile prod`

Recreate (fresh account, after the processor Lambda exists):
1. `aws s3api create-bucket --bucket **frames-<prefix>-<ACCOUNT_ID>** --profile **PROFILE**` (in us-east-1, do NOT pass `--create-bucket-configuration`; in any other region you must).
2. Grant S3 permission to invoke the processor BEFORE wiring the notification, or the put fails:
   `aws lambda add-permission --function-name pest-detection-processor --statement-id s3-invoke --action lambda:InvokeFunction --principal s3.amazonaws.com --source-arn arn:aws:s3:::**frames-<prefix>-<ACCOUNT_ID>** --source-account **ACCOUNT_ID** --profile **PROFILE**`
3. `aws s3api put-bucket-notification-configuration --bucket **frames-<prefix>-<ACCOUNT_ID>** --notification-configuration file://**path-to-notification.json** --profile **PROFILE**` (template: `deployer/audit/s3__frames-armyworm__notification.json`).
4. `aws s3api put-bucket-cors --bucket **frames-<prefix>-<ACCOUNT_ID>** --cors-configuration file://**path-to-cors.json** --profile **PROFILE**` (update the origins to the new CloudFront domain and website endpoint).
Console path for the notification: bucket → Properties → Event notifications → Create event notification → prefix `frames/`, event type All object create events, destination Lambda.

Gotcha. The retired development account carried two duplicate S3 invoke statements on the processor (one console-added, one CLI-added). Harmless, but emit only one when recreating; the deployer emits one.

### 2.3.2 argus-processed-506868652945

Purpose. On the development account, Gen-1 and processor v4.0 wrote annotated images to the processed bucket; since v4.1 the processor writes nothing to it, and the production bucket has been empty from day one. The bucket must still exist because `pest-monitoring-api` requires the `S3_PROCESSED_BUCKET` env var at cold start (`os.environ["S3_PROCESSED_BUCKET"]`, a hard KeyError if absent), and `DELETE /detection` deletes legacy `processed_image_key` objects from it. The processor does NOT read this env var anywhere in its code; it is merely set on the live function config and ignored. (`deployer/STACK_MANIFEST.md` item 6 overstates this as a processor cold-start requirement; the code contradicts it. NOTE: doc lag there, not here.)

Configuration: Versioning ON, SSE-S3, PAB all TRUE, private, CORS localhost:5500 only, no notification.

### 2.3.3 argus-dashboard-506868652945 (static website)

Purpose. Hosts the built dashboard files (index.html, styles.css, `js/*.js`). Served publicly two ways: the raw website endpoint over HTTP, and CloudFront `E1YADURLSAVNFA` over HTTPS (the URL that is handed out).

Configuration:
- Versioning OFF, SSE-S3, Block Public Access all FALSE.
- Bucket policy `PublicReadDashboard`: Principal `*`, `s3:GetObject` on `arn:aws:s3:::argus-dashboard-506868652945/*`.
- Static website hosting: Index document `index.html`, Error document `index.html` (this is the SPA fallback; CloudFront has no custom error responses).

Redeploy after any dashboard edit (the `--cache-control` flag is required; without it browsers heuristically cache the JS and users keep the old build):

`aws s3 sync web/dashboard_v4 s3://argus-dashboard-506868652945 --delete --cache-control "no-cache, must-revalidate" --exclude "*.md" --exclude ".claude/*" --profile prod`

The repo copy of `web/dashboard_v4/js/config.js` keeps the development reference values; the deployer rewrites `HTTP_API` / `COGNITO_CLIENT_ID` in memory at upload time. If you sync by hand, first set them to the production values (`https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com`, `6vebotf45bp8u46cnraddiaplv`) in your working copy.

Because CloudFront runs the CachingDisabled policy, this sync IS the full redeploy. No CloudFront invalidation step exists or is needed.

Recreate order trap: `put-public-access-block` (all false) must come BEFORE `put-bucket-policy`, or the policy call is rejected. Then `put-bucket-website`, then sync.
- Console: S3 → bucket → Permissions → Block public access → Edit; Permissions → Bucket policy; Properties → Static website hosting.
- CLI: `aws s3api put-public-access-block --bucket **BUCKET** --public-access-block-configuration BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false --profile **PROFILE**`, then `aws s3api put-bucket-policy --bucket **BUCKET** --policy file://**policy.json**`, then `aws s3api put-bucket-website --bucket **BUCKET** --website-configuration '{"IndexDocument":{"Suffix":"index.html"},"ErrorDocument":{"Key":"index.html"}}'`.

### 2.3.4 lambda-layers-366356442579 (dev account only, history)

Held only the Gen-1 `ffmpeg-layer.zip` (~59 MB), which no current Lambda uses. The `fyp-pillow` layer (7.9 MB) publishes directly without S3 staging. The production account has no layer bucket and does not need one.

## 2.4 DynamoDB tables

All four production tables: billing PAY_PER_REQUEST, TTL off, PITR off, no streams, TableClass STANDARD, DeletionProtection false. Tags: `Project=armyworm-v2`, `Owner=Runzhe-armyworm`, `ManagedBy=Runzhe`.

Inspect any table:
- Console: DynamoDB → Tables → table name → Overview / Indexes / Explore table items.
- CLI: `aws dynamodb describe-table --table-name **TABLE** --profile prod --region us-east-1`

### 2.4.1 pest-monitoring-cameras

PK `camera_id` (S). No GSI. One row per camera; this table is the single runtime home of the Rekognition model ARN and of the per-camera feature flags. All three detection-path Lambdas resolve `custom_model_arn` from here at call time; nothing hardcodes an ARN.

Live rows: 3, after the 2026-07-14 camera_id migration (`armyworm_go2_a8mini`→`worm_cam`, `moth_cam_01`→`moth_cam`; test row `person_cam` deleted 2026-07-13).

Live item (`worm_cam`, read from the production table 2026-08-14):

```json
{
  "camera_id": "worm_cam",
  "label": "Worm Cam",
  "target_label": "armyworm-larva",
  "model_type": "custom",
  "custom_model_arn": "arn:aws:rekognition:us-east-1:506868652945:project/argus-detection/version/v9r-prod-20260810/1786377372187",
  "min_confidence": 10,
  "post_verify_floor": 33,
  "max_runtime_min": 45,
  "model_running": false,
  "tiling_enabled": true,
  "llm_verify_enabled": true,
  "stream_enabled": false,
  "kvs_stream_name": "armyworm-cam-stream",
  "default_waypoint_id": null,
  "schedule": {"enabled": false, "days": [], "start_time": "05:40", "updated_at": "..."}
}
```

Three of these fields form the tuned detection ladder: `min_confidence` 10 is the CANDIDATE floor before the LLM gate (raising it strangles recall — the exact trap found 2026-08-05), `post_verify_floor` is the display/denoise floor applied AFTER the LLM verdict (per-camera override of the processor env `POST_VERIFY_FLOOR`), and `max_runtime_min` 45 is the per-camera watchdog auto-stop window (v6.2). The floor's history matters when reading old runs: 49 was decided and flipped live 2026-08-10 on the development account, and the deploy-day baseline `migration/prod_baseline_20260810.json` still shows 49; after the production model retrain, the floor was refitted per-build to **33** on 2026-08-13 (camera row AND Lambda env together) so a live Test upload draws the same boxes the curated gallery shows. **The current live value is 33.** The floor is a property of a model build, not a constant — refit it whenever the detector is retrained. The schedule map no longer carries `end_time`: since API v6.2 scheduling is start-only and the watchdog closes the run (2.6, 2.9).

`moth_cam` mirrors this with `target_label` "Moths", `model_type` custom, the production moth ARN (`arn:aws:rekognition:us-east-1:506868652945:project/argus-moth-detection/version/moth-prod-20260811/1786412382515`, seeded by `migration/migrate_moth.py`), `kvs_stream_name` "moth-cam-stream", `max_runtime_min` 45, and `llm_verify_enabled` FALSE on purpose — the moth model is accurate enough on its clean trap background that verification tokens buy nothing, and the verify prompt asks about larvae while this camera's target is adult moths. `manual_upload` is the hidden fallback row used whenever an S3 key does not parse to a known camera; it also backs the dashboard Test upload. The deployer seeds it as armyworm-larva/custom (the development account's row was Person/general/80) — behaviour is identical because its `custom_model_arn` is empty, which routes the processor down the generic path.

NOTE: doc lag. `deployer/audit/dynamodb__pest-monitoring-cameras__seed-scan.json` is a 2026-07-08 development-account snapshot with 4 rows under the old camera ids and the v5 model ARN. The 3-row layout above has been current since the 2026-07-14 camera_id migration.

Seeding history worth keeping: the ARGUS deployer seeds camera rows with the tuned values (`min_confidence` 10, `llm_verify_enabled` true, `post_verify_floor` 49 as of deploy day — see the floor history above; `max_runtime_min` 45). That seeding is a 2026-08-10 fix: an earlier deployer seeded `min_confidence` 30 and no `llm_verify_enabled` at all, which would have silently disabled the whole LLM layer on a fresh stack. Two post-deploy row repairs on 2026-08-11: `worm_cam.label` restored to "Worm Cam" (the deployer had written the `--deployment-name` into the label, and since the dashboard shows the label, not the camera_id, the row looked missing), and `worm_cam.schedule` restored to the 05:40-daily-disabled setting so re-enabling it does the right thing. `worm_cam.kvs_stream_name` carries the same stream name as on the development account, but no KVS stream actually exists on production (2.14); `stream_enabled` is false, so nothing reads it.

Gotcha. Writing a model ARN with `aws dynamodb update-item` from PowerShell mangles the quoting (the ARN contains colons). Use boto3 or a JSON file (`datasets/migrate_camera_ids.py` shows the pattern).

### 2.4.2 pest-monitoring-detections

PK `image_id` (S) = the exact S3 object key. SK `detection_time` (S), ISO 8601 with Z suffix. GSI `by-pest-time`: `pest_type` HASH + `detection_time` RANGE, projection ALL. The production table holds the curated handover gallery (64 records after the 2026-08-13 curation, 28 with a detection) plus the 36 migrated Jewel on-site records; the development table (~907 rows at retirement) keeps the threshold-study evidence.

One record per processed frame, written unconditionally (clean frames too, so the patrol gate never dead-locks). Example item — a 2026-07-29 development-account record, kept because it shows every field the schema has carried; production rows differ as noted below. Shaped by the processor's `db_item` (2.5.1 step 12):

```json
{
  "image_id": "frames/worm_cam/wp3/20260729_101502.jpg",
  "detection_time": "2026-07-29T02:15:07Z",
  "pest_type": "armyworm-larva",
  "source": "live-detection",
  "created_at": "2026-07-29T02:15:07Z",
  "bucket": "frames-armyworm-366356442579",
  "original_image_key": "frames/worm_cam/wp3/20260729_101502.jpg",
  "camera_id": "worm_cam",
  "waypoint_id": "wp3",
  "target_label": "armyworm-larva",
  "target_detected": true,
  "target_confidence": "81.6",
  "min_confidence_used": 60,
  "label_count": 2,
  "labels": "[{\"name\": \"armyworm-larva\", \"confidence\": 81.6}]",
  "bboxes": [
    {"label": "armyworm-larva", "confidence": "81.6",
     "top": "0.4412", "left": "0.6120", "width": "0.0231", "height": "0.0184",
     "verified_by_llm": "true", "llm_reason": "elongated segmented larva with spots"}
  ],
  "verifications": {},
  "model_type": "custom",
  "model_arn": "arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v9/version/v9-20260725-1746/1785001598671",
  "llm_verify_model": "us.anthropic.claude-sonnet-4-6",
  "llm_scan": {"ran": false, "cells": "", "recovered": 0}
}
```

Field notes. Rows written by processor v6.3+ (2026-08-07) no longer carry the `llm_scan` map or `source=llm_scan` recovery boxes — those belong to the removed whole-frame scan (2.5.6) and survive only on historical rows. Production rows carry `bucket` `argus-frames-506868652945` and the production model ARN; the 36 migrated Jewel records deliberately keep the development account's `model_arn` (provenance — it records which model produced that detection) plus `migrated_from`/`migrated_at` stamps. `bboxes` is only populated when `target_detected` is true; a clean frame writes the record with an empty list. Bbox coordinates are stringified normalized floats (DynamoDB's Number type rejects the high-precision floats Rekognition emits; strings also match the Wilbur-era migrated data), confidence is formatted to one decimal, and `llm_reason` is truncated to 120 characters by `boxes_to_db_format`. `verifications` is a map filled by dashboard TP/FP clicks, keyed by bbox index as a string. `labels` is a legacy JSON string the dashboard still reads; since v4.5 its target-label entries are filtered by the same detection floor as `bboxes` (a real bug fix, 2.5.6), while non-target entries pass through unfiltered as context. Duplicate rows per `image_id` are possible (at-least-once S3 events write a new row with a different `detection_time`); `DELETE /detection` removes all of them. Old migrated rows use space-separated timestamps; `/history` handles both formats.

### 2.4.3 pest-monitoring-system-config

PK `config_key` (S). Single live row `detection_settings`:

```json
{
  "config_key": "detection_settings",
  "email_enabled": true,
  "recipient_email": "rex2956550768@gmail.com",
  "additional_recipients": [],
  "auto_capture": true,
  "capture_interval": 60
}
```

Read by the processor for alert recipients and by the API for `/settings` and `/identities`.

### 2.4.4 pest-monitoring-schedule-logs

PK `log_id` (S, uuid4) + SK `timestamp` (S, ISO Z). Audit rows written by `pest-camera-scheduler`:

```json
{
  "log_id": "0f6f8b8e-...",
  "timestamp": "2026-07-20T01:00:03Z",
  "camera_id": "worm_cam",
  "action": "start",
  "status": "success",
  "message": "Model start initiated for worm_cam",
  "schedule_id": "b71b...",
  "trigger_time": "09:00"
}
```

### 2.4.5 Dead tables (do not recreate)

`pest-monitoring-config` (Gen-1 config, PK `config_id`, only referenced by the archived Flutter app) and `websocket-connections` (the WebSocket push store retired when the dashboard moved to polling in v3.7).

Recreate the four production tables. Detections (the only one with a GSI):

`aws dynamodb create-table --table-name pest-monitoring-detections --attribute-definitions AttributeName=image_id,AttributeType=S AttributeName=detection_time,AttributeType=S AttributeName=pest_type,AttributeType=S --key-schema AttributeName=image_id,KeyType=HASH AttributeName=detection_time,KeyType=RANGE --billing-mode PAY_PER_REQUEST --global-secondary-indexes '[{"IndexName":"by-pest-time","KeySchema":[{"AttributeName":"pest_type","KeyType":"HASH"},{"AttributeName":"detection_time","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}]' --profile **PROFILE** --region **REGION**`

The other three are plain creates with their key schemas from 2.4.1/2.4.3/2.4.4 and no GSI. Console: DynamoDB → Create table → set keys → Customize settings → On-demand capacity.

## 2.5 Lambda: pest-detection-processor (the detection pipeline)

Purpose. Turns one uploaded frame into one detection record and, when warranted, one alert email. This is the largest and most-evolved piece of the backend (~1,630 lines after the v6.3 dead-code strip; ~3,270 before it). Python 3.12, **1024 MB / 600 s** live (the 512 MB / 180 s recommendation in the file header is the minimum, not the deployment), layer `fyp-pillow:1` (production; the dev account's live layer was `:2` — 2.10), role `pest-detection-processor-role`, handler `lambda_function.lambda_handler`. Trigger: the frames-bucket S3 notification (2.3.1).

Version status (updated 2026-08-11):
- The mirror `lambda/pest-detection-processor.py` is **v6.3** and matches the deployed code: the v6.3 dead-code strip was deployed 2026-08-07 (Runzhe's pre-migration order). Removed: the v4.6 whole-frame scan, v4.8 cluster merge, v5.1 LLM-FIRST, v5.2 LLM-LEAD, v5.6 LLM-PLAIN, v5.7 LLM-AGENT, v6.1 picture-in-picture composite, and the `__rek-` detector override — ~1.6k lines of experiment paths production never ran. The live path is unchanged: tiling → v4.3 suppression → LLM denoiser gate (`LLM_VERIFY_ALL_BOXES=true`) → post-gate cleanup. The `__confN` and `__llm-` key overrides stay (the dashboard Test upload uses both). Full pre-strip source: `lambda/archive/pest-detection-processor_v6.2_full.py`. The earlier open item about proving mirror-vs-deployed byte equality is closed by this deploy. Two oversized-frame fixes were added and deployed 2026-08-12 (`detect_whole_frame` + `_full_frame_bytes`, documented in 2.5.3); the mirror carries both.
- Live configuration: Timeout **600 s**, MemorySize **1024 MB**, `LLM_VERIFY_MODEL_ID=us.anthropic.claude-sonnet-4-6`, `LLM_VERIFY_ALL_BOXES=true` (denoiser mode: every box judged), `LLM_VERIFY_MAX_BOXES=120`, `LLM_VERIFY_MAX_TOKENS=300`, `LLM_VERIFY_WORKERS=3`, `LLM_VERIFY_PAD=0.6`, `TILE_MIN_CONFIDENCE=8`, `POST_NMS_IOU=0.1`, `POST_NMS_CONTAIN=0.1`, `POST_MAX_BOX_AREA=0.05`, `POST_VERIFY_FLOOR=33` (floor history: 34→49 on 2026-08-10, refitted 49→**33** on 2026-08-13 for the retrained production model; the per-camera `post_verify_floor` DDB field overrides it and reads 33 too — verified against the live row 2026-08-14; the frozen deploy-day baseline `migration/prod_baseline_20260810.json` still shows 49). The retired dev account also still carries dead `LLM_SCAN`/`LLM_MERGE`/`LLM_LEAD`/`LLM_FIRST`/`LLM_AGENT`/`LLM_PLAIN`/`LLM_VERIFY_COMPOSITE` env vars whose code paths v6.3 deleted; the production account omits them (harmless either way).
- `deployer/audit/lambda__pest-detection-processor.json` is a 2026-07-08 snapshot (Timeout 60, no LLM env vars). Stale on those fields; still correct for role, layer, trigger, and the five base env vars. The frozen baseline above supersedes it for configuration.
- The `bedrock-verify` inline policy is now dumped: `deployer/audit/iam__pest-detection-processor-role__inline__bedrock-verify.json`. The live policy grants `bedrock:InvokeModel` + `bedrock:Converse` on wildcarded inference-profile families (`us.anthropic.*`, `global.anthropic.*`, `us.amazon.*`, `us.meta.*`, `us.mistral.*`) plus the matching region-wildcarded foundation-model ARNs, so Sonnet 4.6 is covered. `lambda/bedrock-policy.json` (Haiku 4.5 + Nova Lite only, no Sonnet 4.6) is the older, narrower source file — recreating the role from it while the env points at Sonnet 4.6 reproduces the total-gate-failure mode in 2.5.6/2.5.9. Use the audit dump.
- A second, previously undocumented inline policy is also dumped: `deployer/audit/iam__pest-detection-processor-role__inline__s3-frames-write.json` — `s3:PutObject` scoped to `frames/*` on the frames bucket. It exists for exactly one code path: the v4.8.3 EXIF orientation write-back (2.5.1 step 5). Without it, rotated phone uploads keep their EXIF tag and every box draws in the wrong place.

Base environment variables (the LLM/tiling tunables are documented inline below): `SENDER_EMAIL` (required — the code's only hard `os.environ[]` read, live `rex2956550768@gmail.com`), `TABLE_DETECTIONS`, `TABLE_CAMERAS`, `TABLE_SYSTEM_CONFIG`, `S3_PROCESSED_BUCKET` (config-only: set on the live function but never read by the processor code).

### 2.5.1 Handler flow, end to end

`lambda_handler(event, context)` in plain steps:

1. Parse the S3 event record: bucket + URL-decoded object key. Non-image extensions (anything not .jpg/.jpeg/.png) return 200 "Skipped".
2. `parse_s3_key(object_key)`: splits the key on `/` and accepts it as standard only when there are at least 4 segments and the first is literally `frames`; then camera_id = segment 2, waypoint_id = segment 3 with everything from the first `__` stripped (so per-request overrides can ride in the key, step 4), filename = the remaining segments rejoined. Any other shape logs the key and returns the `manual_upload` fallback pair — the function never fails, it degrades.
3. `get_camera_config(camera_id)` reads the camera row with a three-step fallback chain: the requested id, then the `manual_upload` row, then hardcoded defaults (`Person` / `general` / `min_confidence` 80) if even that read fails — a DynamoDB outage degrades detection rather than killing it. `get_system_config()` reads the global row (empty dict on any error); `collect_recipients()` merges `recipient_email` + `additional_recipients` into a deduped, order-preserving list (a comma-separated string is tolerated and split).
4. Per-request overrides parsed from the key, both stateless (no DDB writes, so parallel A/B runs never fight over config):
   - `__conf<N>` (N 10-100) overrides `min_confidence` for this run only. Used by the dashboard Test upload confidence slider.
   - `__llm-<alias>` (v4.9) picks the Bedrock verification model for this run from `LLM_MODEL_ALIASES` (v6.3 keeps two: `sonnet46`, `haiku45`). `set_active_llm_model()` is called on EVERY record and resets a one-slot holder, because Lambda reuses warm containers and a stale model id would silently attribute one model's results to another. Resolution order (v6.3), most specific first: the `__llm-` key alias, then the camera row's `llm_model_id` (the dashboard's per-camera model picker — stored as an ALIAS, not a raw model id, so a typo can never reach Bedrock as an unknown id), then the `LLM_VERIFY_MODEL_ID` env default. Only known aliases are honoured at any level: an unknown model id does not fail loudly at the call site, it fails as AccessDenied on EVERY crop, which the gate reads as "no verdict".
5. EXIF orientation normalisation (v4.8.3, 2026-07-28): download the frame, `ImageOps.exif_transpose`, and if the image was rotated, write the upright pixels BACK to the same S3 key. Reason: browsers honour the EXIF Orientation tag, PIL and the coordinate maths here do not, so phone uploads had every box drawn in the wrong place. Baking the rotation in once makes Rekognition, the crops, and the dashboard agree. Non-fatal on any error.
6. Detection (2.5.2 / 2.5.3): tiled or single `detect_custom_labels` for custom cameras, `detect_labels` for general ones.
7. v4.3 hard-object suppression (2.5.5).
8. LLM gate (2.5.6): `apply_llm_verify_gate` — since v6.3 the one and only gate (the scan/merge/lead/first experiment dispatches are deleted). Only runs for custom-model cameras with `llm_verify_enabled: true` in DynamoDB and `LLM_VERIFY=true` globally. `hybrid_gate_ran = n_verified > 0`, never "the call did not raise" (a 2026-07-22 review bug: every internal fail-open path returns normally having verified nothing).
9. Post-gate cleanup (v5.0, extended v5.9/v6.2): `apply_post_gate_cleanup` over gate survivors, only when the gate verified something. Four knobs, all live-configured: `POST_MAX_BOX_AREA` (live 0.05 — a box over 5% of the frame cannot be a larva whatever the verifier said; grounded in the hand-labelled maximum of 4.39%, and the guard against a frame-spanning box the verifier confidently called a caterpillar), `POST_NMS_IOU` (live 0.1) plus `POST_NMS_CONTAIN` (live 0.1 — IoU divides by the union, so a small box wholly inside a big one scores near zero and survives IoU-NMS; the containment test asks how much of the SMALLER box is covered), and the post-verify confidence floor: env `POST_VERIFY_FLOOR` overridden per camera by the DDB field `post_verify_floor` (v6.2; both live 49). All default 0 = off in code.
10. Decide `target_detected`: `detection_floor = 0 if hybrid_gate_ran else min_confidence`. A gate-approved sub-threshold box is a legitimate detection; re-applying `min_confidence` would delete it.
11. `extract_bounding_boxes` + `boxes_to_db_format` build the `bboxes` list (2.5.8).
12. Unconditional `put_item` to `pest-monitoring-detections`. Clean frames write a record; the Go2 patrol completion gate polls `get_item` by S3 key and depends on it.
13. SES alert if `target_detected` and email enabled and recipients exist (2.5.10).

### 2.5.2 Camera config lookup and the detection branch

`is_custom = model_type == "custom" and custom_model_arn set and not REPLACE_*`. Custom cameras with `tiling_enabled: true` on the row (AND global env `TILING_ENABLED`, default true) go through `run_tiled_detection`. Custom without tiling do a single `detect_custom_labels` on the S3 object with `MinConfidence=30` (deliberately low; filtering happens later so the record keeps full label data). General cameras call `detect_labels` (`MaxLabels=50, MinConfidence=50`). Any tiling failure falls back to the single S3Object call, so tiling can never break detection.

### 2.5.3 The tiling path (v4.2)

Functions: `get_tiling_config`, `compute_tile_regions`, `_crop_upscale_bytes`, `_detect_custom_on_bytes`, `_tile_label_to_global`, `nms`, `run_tiled_detection`.

Geometry. `compute_tile_regions(w, h, cols, rows, overlap)` slices the frame into a `TILE_COLS x TILE_ROWS` grid (default 4x4), expands every tile by `TILE_OVERLAP` (0.15) of the base tile on each side, and clamps to the frame. Overlap guarantees a target straddling a boundary lands whole in at least one tile. With `TILE_INCLUDE_FULL_FRAME=true` a full-frame pass is prepended: 16 tiles + 1 = the 17-call geometry ("quadrant/17-tile" in project shorthand; cols/rows are env-tunable, so a 2x2 quadrant grid is the same code with different numbers).

Each tile is cropped and Lanczos-upscaled until its long edge reaches `TILE_UPSCALE_LONG_EDGE` (1920; roughly the W6-validated ~4x zoom for a 1080p frame), JPEG-encoded, and sent to `detect_custom_labels` as raw bytes with `MinConfidence=TILE_MIN_CONFIDENCE` (code default 30; LIVE VALUE 8 since the denoiser era, which is what feeds the LLM gate its volume). `_detect_custom_on_bytes` retries throttles three times with backoff. `_tile_label_to_global` converts each tile-normalized box back to global normalized coordinates. `nms` greedy-deduplicates per class at `TILE_NMS_IOU` 0.5, keeping the highest-confidence box per overlap cluster (overlapping tiles produce duplicates by construction). Tiles run in a ThreadPoolExecutor (`TILE_MAX_WORKERS` 4). The output list is shaped exactly like Rekognition CustomLabels, so nothing downstream knows tiling happened.

History note. Tiling was ruled OFF in W15 ("whole-image is the deployment mode") and turned back ON for `worm_cam` by Runzhe's own call 2026-07-27/28 when the architecture flipped to "Rekognition finds (high recall), the LLM verifies (precision)". `docs/detection.md`'s "tiling is OFF in production" paragraph is the W15-era record. NOTE: doc lag.

### 2.5.4 Rekognition calls

Three Rekognition APIs are used: `detect_custom_labels` (the armyworm/moth models; per tile as bytes, or once per frame as an S3Object reference), `detect_labels` (twice over: as the general-camera detector, and as the v4.3 suppression pass), and, from the other Lambdas, `start/stop_project_version` + `describe_projects/describe_project_versions`. The endpoint must be RUNNING or `detect_custom_labels` fails; see 2.18.

**Where the detection JSON goes.** This is the most common question about the pipeline, so it is worth stating exactly. Rekognition does not write a result file anywhere, and nothing is "sent" to the processor. `detect_custom_labels` is a **synchronous HTTPS request** made by the Lambda through boto3. The Lambda puts the image in the request — either as raw bytes (`Image={"Bytes": ...}`, used for every tile) or as a pointer (`Image={"S3Object": {"Bucket": ..., "Name": ...}}`, used for the single whole-frame call) — and the JSON result comes back **in the response of that same call**, parsed by boto3 into a Python dictionary:

```python
resp = rekognition.detect_custom_labels(
    ProjectVersionArn=model_arn,
    Image={"Bytes": tile_bytes},
    MinConfidence=TILE_MIN_CONFIDENCE,
)
labels = resp["CustomLabels"]     # list of dicts, in memory, nothing on disk
```

Each entry in `resp["CustomLabels"]` is `{"Name": <label>, "Confidence": <0-100>, "Geometry": {"BoundingBox": {"Left","Top","Width","Height"}}}`, with the box normalised 0-1 against the image that was sent. For a tiled frame that means 17 separate responses, each with coordinates relative to its own tile; `_tile_label_to_global` converts them to whole-frame coordinates before anything else touches them.

From that point the result exists **only as a Python object inside the running Lambda**. It is passed by reference through `suppress_nonveg`, `apply_llm_verify_gate` and `apply_post_gate_cleanup`, each of which filters the list in memory. Nothing is persisted until the very end, when `detections_table.put_item` writes the surviving boxes into the DynamoDB record. There is no intermediate S3 object, no queue, no file, and no way to inspect the raw model output after the fact except in the CloudWatch log lines the processor prints as it goes. If a raw copy is ever needed for debugging, it has to be added deliberately, for example by logging `json.dumps(resp)` before filtering.

The same holds for `detect_labels`: its response is consumed in-process by `get_suppression_regions` and discarded.

### 2.5.5 v4.3 DetectLabels hard-object suppression

Problem it solves: Rekognition Custom Labels object detection cannot be trained on negative images (proven empirically and against AWS documentation, 2026-07-13; see `docs/detection.md`). The model's learned prior is "plant close-up means armyworm"; out of domain it boxed people, a robot arm, and a jeep wheel at 90%+. So non-vegetation false positives are filtered at the application layer.

Functions: `get_suppression_regions`, `_covered_fraction`, `suppress_nonveg`.

Algorithm:
1. Runs only for custom models, only when the model actually produced at least one box (clean frames never pay the extra call), and only when `SUPPRESS_NONVEG=true` (default).
2. `get_suppression_regions` runs `detect_labels` on the frame (`MaxLabels=50`, floor `SUPPRESS_MIN_CONF` 55) and collects instance bounding boxes whose label is in `SUPPRESS_LABELS` (person, vehicle, wheel, tire, machinery, furniture, building, wall, and ~40 more) and NOT in `SUPPRESS_PROTECT`.
3. The plant-label exemption: `SUPPRESS_PROTECT` (plant, leaf, flower, tree, vegetation, grass, foliage, soil, ground, ...) can never generate a suppression region. Worms live on plants; a DetectLabels "Plant" box overlapping a worm proves nothing.
4. Coverage test: `suppress_nonveg` drops a custom box when `_covered_fraction` — the fraction of the WORM BOX's own area lying inside a hard-object region, not IoU — reaches `SUPPRESS_COVERAGE` (0.5). Boxes without geometry pass through untouched.
5. Non-fatal at every level: a DetectLabels error returns zero regions and detection proceeds unfiltered.

All knobs env-tunable: `SUPPRESS_NONVEG`, `SUPPRESS_MIN_CONF`, `SUPPRESS_COVERAGE`, `SUPPRESS_LABELS`, `SUPPRESS_PROTECT`. Required an IAM add of `rekognition:DetectLabels` to `pest-detection-processor-policy` (the role previously had only `DetectCustomLabels`). Verified pre-deploy: the 65% jeep-tire FP on cag_armyworm_103 (94% covered by a `wheel` region) drops while the real worms in 104/105 stay (`datasets/verify_suppression.py`).

### 2.5.6 The Bedrock LLM verification gate (v4.4 → v4.7)

Shared plumbing:
- `get_bedrock_client()` builds the `bedrock-runtime` client lazily under a lock (crop verdicts run in a thread pool; unlocked concurrent construction on boto3's default session can raise inside a worker, which fail-open would silently score as "unverified, keep"). Client config: read timeout `LLM_VERIFY_TIMEOUT` (12 s), 5 retries in adaptive mode (client-side rate limiting; a large sweep can fire 144 calls per frame).
- `crop_box_bytes(pil_img, bbox, pad, long_edge)`: crops a normalized box with `LLM_VERIFY_PAD` (0.6) padding as a fraction of the box's own size, floored at `LLM_VERIFY_MIN_CONTEXT_PX` (32 px) so a tiny box still gets real surrounding context, then upscales toward `LLM_VERIFY_LONG_EDGE` (672) capped at `LLM_VERIFY_MAX_UPSCALE` (8x) so a 6x4 px box is not blown into pure interpolation noise (a real 2026-07-21 finding).
- `verify_one_crop(crop_bytes)`: one Bedrock Converse call, raw JPEG bytes + `LLM_VERIFY_PROMPT`, `maxTokens` `LLM_VERIFY_MAX_TOKENS` (code default 100; live 300 for Sonnet 4.6, see the thinking-model gotcha in 2.5.9). Temperature is only sent when `LLM_VERIFY_TEMPERATURE` is explicitly set (sampling parameters are a 400 on newer Claude models). Refusal/guardrail stop reasons return None (a safety decline is not a verdict about the crop). The reply text is taken from the first text block, skipping the reasoning blocks that thinking-always-on models emit.
- `_extract_json_object` + `parse_llm_verdict`: balanced-brace scan honouring string quoting (a greedy regex broke on any brace after the object), then strict parsing of `{"is_larva": bool, "reason": str}` with tolerant bool coercion. Anything unparseable returns None = "unverified", which KEEPS the box.
- The prompt (`LLM_VERIFY_PROMPT`) is the v5.9 NEUTRAL rewrite (2026-08-04, wording refined 2026-08-07): it opens with "What is in this photo?", states that most such crops contain only leaves/stems/wood/soil/shadows, describes the target as an elongated soft body with clear segmentation "typically carrying yellow-and-black stripes" (Runzhe's wording), lists the common non-larva look-alikes, and demands strict JSON with true ONLY if a larva is genuinely present at the crop centre. Two earlier prompt generations are retired for measured reasons: the recall-biased ending ("if unsure, answer true") let wood planks and dried leaves survive as confirmed detections, and the primed opening ("an automated detector flagged this region as possibly containing a larva") was a leading question — of 28 surviving false boxes the primed prompt killed 2, the neutral wording killed 5, and 5 false verdicts literally cited hand-drawn ink marks as their evidence. Do not reintroduce a preamble that asserts a larva may be present.

The v4.5 hybrid gate (deployed 2026-07-22; still the code's default behaviour when `LLM_VERIFY_ALL_BOXES=false`):
- Only target-label boxes BELOW the camera's `min_confidence` are adjudicated. A box at/above it is trusted outright and never sent to Bedrock — its fate cannot change, so verifying it would only spend money and latency. This changed the meaning of `min_confidence` from "detection floor" to "the point above which Rekognition's word is final".
- An explicit "not a larva" verdict DROPS the box; a positive verdict or ANY failure keeps it. Fail-open at every level: missing Bedrock permission, model error, malformed reply, throttle — none of them ever deletes a box.
- Candidates are sorted by confidence and capped at `LLM_VERIFY_MAX_BOXES` (`_crop_verdicts`), judged in parallel (`LLM_VERIFY_WORKERS`, code default 4, live 3).
- Per-camera opt-in via the DynamoDB flag `llm_verify_enabled` (mirrors `tiling_enabled`). Not ceremony: the Lambda is shared by every custom camera, and `moth_cam`'s target is adult moths while the prompt asks about larvae. Only `worm_cam` opts in. Global kill switch `LLM_VERIFY` (default true).
- Results land per box as `verified_by_llm` ("true"/"false") and `llm_reason` (written by `boxes_to_db_format` from the internal `_llm` field), plus `llm_verify_model` on the row.
- Two pre-production bugs found by adversarial review 2026-07-22, both fixed and regression-tested: (1) gate success used to be judged by "did the call not raise", which let all-fail-open runs collapse the detection floor to 0 with nothing verified; the gate now returns `n_verified` and the floor only collapses when `n_verified > 0`. (2) The legacy `labels` field dumped raw Rekognition output down to its ~30-50% internal gather floor whenever the gate did not run, and the dashboard trusts target-label entries there as confirmed detections; it is now filtered by the same detection floor as `bboxes`.

The v4.6 whole-frame scan is HISTORY as of v6.3: it ran one extra Bedrock pass over the downscaled frame asking for positive grid cells and could recover a synthetic box from an un-boxed positive cell (fail-closed, crop-confirmed). It was never enabled in production (`LLM_SCAN=false` throughout) and the v6.3 strip deleted the code; old detection records still carry its `llm_scan {ran: false}` observability map, which is why the field exists. The key measurement that killed it stands: whole-frame LLM vision missed 8/14 larvae that a zoomed crop caught (2026-07-21; a methodology A/B of the verifier on internal images — not a detection-recall statistic; see Chapter 3) — a whole-frame model's silence is not evidence.

The v4.7 denoiser mode (`LLM_VERIFY_ALL_BOXES=true`, THE LIVE MODE since 2026-07-29): the min_confidence exemption is removed; EVERY target box is crop-judged and the model may delete even a high-confidence box. Rationale: with tiling on, Rekognition recall is near-total but the noise boxes (leaf/shadow/soil/flower) also come back at high confidence, so trusting high confidence lets that noise through. Survival is fail-CLOSED per box: a target box survives only with a positive verdict; rejected boxes AND un-judged boxes (over the cap, crop failure, throttle) are both dropped. The one exception is TOTAL GATE FAILURE — candidates existed, zero verdicts came back. That is infrastructure down, not evidence about the frame, so everything is kept, the log shouts, and the caller falls back to plain min_confidence thresholding. The guard exists because the real thing happened: the model id was switched to Sonnet 4.6 before the role had Bedrock permission for it, producing 492 AccessDenied calls and 44/44 images in that test batch reporting zero detections. Pair the mode with a raised `LLM_VERIFY_MAX_BOXES` (live 120) so tiling's volume is actually covered, or unchecked boxes die fail-closed. Measured live 2026-07-29: a clean SIYI A8 frame produced 61 boxes at 8-14%, the gate judged all 61 and dropped all 61, Lambda duration 35.4 s (the timeout budget was 180 s that day, 600 s since; a tiled frame plus up to 120 crops measures 24-54 s).

### 2.5.7 Retired experiment gates (removed in v6.3)

Between v4.8 and v6.2 the mirror accumulated experiment modes that production never enabled: v4.8 cluster merge (`LLM_MERGE` — union-find over fragment boxes, one union-crop verdict per cluster), v5.1 LLM-FIRST (the model finds first, Rekognition matched against it), v5.2 LLM-LEAD (Sonnet sweeps an 8x8 native-resolution tile grid and Rekognition may only sharpen geometry), v5.6 LLM-PLAIN, v5.7 LLM-AGENT, the v6.1 picture-in-picture composite, and the `__rek-` per-request detector override. The v6.3 dead-code strip (2026-08-07) deleted them all; the corresponding env vars are inert if still present on a function config. Two things to know remain:
- The full pre-strip source is archived at `lambda/archive/pest-detection-processor_v6.2_full.py`; the experiments' measured results live in `docs/detection.md`. Do not re-deploy the archive.
- v5.0 post-gate cleanup is the one survivor — it graduated into the live path and is documented in 2.5.1 step 9. Its floor is deliberately applied AFTER the LLM verdict (the same number as an input floor does nothing useful).

### 2.5.8 Output shaping

`extract_bounding_boxes(labels, target_label, floor, model_type)` filters by exact case-insensitive name match and the passed floor, and normalizes both custom-label geometry and general-label Instances into `{Left, Top, Width, Height, Confidence, Name}` dicts (a general label above threshold with no instance becomes a `no_box` entry, which the DB formatter skips). `boxes_to_db_format` stringifies coordinates and attaches `verified_by_llm` / `llm_reason` / `source=llm_scan` when present. The dashboard reads bbox fields by name and ignores unknown keys, so new fields never require a frontend change.

### 2.5.9 Known gotchas and failure modes

- The Bedrock model id MUST be the cross-region inference profile (`us.` prefix), never the bare foundation-model id. The bare id fails with "Invocation of model ID ... with on-demand throughput isn't supported. Retry your request with the ID or ARN of an inference profile". List valid ids: `aws bedrock list-inference-profiles --profile prod --region us-east-1`.
- Pointing `LLM_VERIFY_MODEL_ID` at a thinking-always-on model without raising `LLM_VERIFY_MAX_TOKENS` makes every verdict silently fail open: the 100-token budget is consumed by reasoning and the JSON never arrives. The code logs the specific symptom (`stopReason=max_tokens` with no parsable verdict).
- Switching the model id before adding IAM for it = total gate failure (2.5.6). Loud in logs, invisible on the dashboard except as suspicious cleanliness.
- Bedrock Converse limits: 20 images per message, each <= 3.75 MB and <= 8000x8000 px; do not pre-base64 image bytes, boto3 handles it.
- `put_item` is unconditional. If no record appears for an upload within ~30 s, the Lambda itself failed; check CloudWatch `/aws/lambda/pest-detection-processor`.
- Changing a Lambda's runtime or name requires delete + recreate, which wipes the resource policy; re-grant the S3 invoke permission (and for the API, the API Gateway permission) after.
- The fyp-pillow layer must contain the compiled `PIL/_imaging*.so`; layer version :1 lacked it and every PIL import failed (no tiling, no crops).

### 2.5.10 SES alerting from the processor

Subject `[FYP Alert] {target_label} detected at {waypoint_id} ({confidence})`. Body lists pest, confidence (or "LLM whole-frame scan" for recovery-only), camera, zone, model type, the `s3://` image path, timestamp, a recovery note when applicable, and all labels. Send failures are logged and non-fatal (the classic cause: an unverified recipient in SES sandbox mode, 2.16).

Inspect / deploy:
- Console: Lambda → Functions → `pest-detection-processor` → Configuration (env vars, layer, trigger) / Monitor → Logs.
- CLI inspect: `aws lambda get-function-configuration --function-name pest-detection-processor --profile prod --region us-east-1`
- CLI deploy code (the mirror must be renamed inside the zip): zip `pest-detection-processor.py` as `lambda_function.py`, then `aws lambda update-function-code --function-name pest-detection-processor --zip-file fileb://**path-to-zip** --profile prod`
- CLI recreate: `aws lambda create-function --function-name pest-detection-processor --runtime python3.12 --handler lambda_function.lambda_handler --role arn:aws:iam::**ACCOUNT_ID**:role/pest-detection-processor-role --memory-size 1024 --timeout 600 --layers **fyp-pillow-layer-arn** --zip-file fileb://**path-to-zip** --environment "Variables={SENDER_EMAIL=**SENDER_EMAIL**,TABLE_DETECTIONS=pest-monitoring-detections,TABLE_CAMERAS=pest-monitoring-cameras,TABLE_SYSTEM_CONFIG=pest-monitoring-system-config,S3_PROCESSED_BUCKET=**PROCESSED_BUCKET**}" --profile **PROFILE**` — then add the detection-tuning env vars from the frozen baseline (Sonnet 4.6, `LLM_VERIFY_ALL_BOXES=true`, `LLM_VERIFY_MAX_BOXES=120`, `LLM_VERIFY_MAX_TOKENS=300`, `TILE_MIN_CONFIDENCE=8`, `POST_NMS_IOU=0.1`, `POST_NMS_CONTAIN=0.1`, `POST_MAX_BOX_AREA=0.05`, `POST_VERIFY_FLOOR=49`). Sizing matters: one tiled frame plus up to 120 verify crops measures 24-54 s, so a 60 s timeout would fail intermittently on real frames (a real ARGUS deployer bug, fixed 2026-08-10). The ARGUS deployer (`deployer/deploy.py`) now sets all of this automatically.

### 2.5.11 Function reference: every function in the processor

The sections above follow the frame through the pipeline. This table is the other view: each function in `lambda/pest-detection-processor.py`, what it is responsible for, and the actual rule or algorithm inside it. Read it when you are looking at the source and need to know why a function does what it does. Order follows the file.

| Function | Job | Rule or algorithm inside |
|---|---|---|
| `active_llm_model` / `set_active_llm_model` | Decide which Bedrock model verifies THIS record | Most-specific-wins resolution: an `__llm-<alias>` segment in the S3 key beats the camera row's `llm_model_id`, which beats the `LLM_VERIFY_MODEL_ID` env default. The alias is looked up in a table of allowed ids, so a bad alias cannot inject an arbitrary model id. Stored in a module-level global for the life of one invocation |
| `iso_now` | One timestamp format everywhere | UTC, ISO 8601, `Z` suffix. Every record and log line uses it, so string comparison sorts chronologically and the `/history` date filter can be a plain string range |
| `parse_s3_key` | Turn the object key into (camera, waypoint, filename) | Splits `frames/{camera}/{waypoint}/{file}`; strips a `__suffix` from the waypoint segment so per-request overrides (`__conf50`, `__llm-haiku`) do not create phantom waypoints. Anything that does not match falls back to the `manual_upload` camera rather than failing |
| `get_camera_config` | Fetch the per-camera row | Two DynamoDB `get_item` calls: the requested id, then `manual_upload` as fallback. Read failures are caught, so a table hiccup degrades to the fallback instead of killing the invocation |
| `get_system_config` | Fetch the single global row | One `get_item` on the config key; returns `{}` on failure so callers can use `.get()` defaults |
| `collect_recipients` | Build the alert address list | Merges `recipient_email` with `additional_recipients`, de-duplicates while preserving order |
| `extract_bounding_boxes` | Turn the raw Rekognition reply into the internal box list | Filters on two conditions at once: label name matches the camera's `target_label` (case-insensitive) AND confidence is at or above the threshold. Carries `no_box` for labels with no geometry and `_llm` for the verdict attached later |
| `boxes_to_db_format` | Shape boxes for DynamoDB | Stringifies every coordinate. DynamoDB's Number type rejects the high-precision floats Rekognition returns, and the dashboard parses them back to float anyway |
| `get_tiling_config` | Decide whether this frame is tiled, and how | Per-camera opt-in (`tiling_enabled`) gated by a global env switch; grid size, overlap, upscale and gather floor all come from env vars so tuning never needs a code deploy |
| `compute_tile_regions` | Produce the tile rectangles | Divides the frame into a `cols x rows` grid, expands every tile by `overlap` of a base cell on all four sides, then clamps to the frame edges. The overlap is what stops a worm sitting on a seam from being cut in half; the clamp is why edge tiles are smaller than interior ones |
| `_encode_jpeg` / `_full_frame_bytes` | Produce JPEG bytes Rekognition will accept | Encodes at a set quality, then shrinks in a loop until the payload is under the service limit |
| `detect_whole_frame` | The single full-frame detection call | Prefers the `S3Object` form (no download). On `ImageTooLargeException` it falls back to downloading, shrinking and sending bytes, so an oversized phone photo degrades instead of failing the frame |
| `_crop_upscale_bytes` | Prepare one tile for the detector | Crops the region, then Lanczos-upscales until the long edge reaches the configured target. This is the zoom that gives a small target more pixels to be found in |
| `_detect_custom_on_bytes` | Call the detector on raw bytes | Wraps `detect_custom_labels` with retry and backoff on throttling, so a burst of 17 tile calls does not lose a tile to a transient limit |
| `_tile_label_to_global` | Put a tile hit back on the frame | Rescales the tile-normalised box by the tile's pixel origin and size, divided by the frame size. `region=None` means the full-frame pass, whose geometry is already global. Returns `None` for a label with no usable geometry |
| `_iou` | Overlap measure for the tile merge | Intersection area divided by union area of two boxes; zero when they do not touch, one when identical |
| `nms` | Merge duplicates across tiles | Greedy per-class non-maximum suppression: sort by confidence descending, walk the list, keep a box only if its IoU against every already-kept box of the same class is below the threshold. Greedy means each box is compared with what survived, never with the original list |
| `run_tiled_detection` | Orchestrate the whole tiled pass | Builds the regions, runs the tile calls in a small thread pool plus one full-frame pass, converts every hit to global coordinates, then de-duplicates with `nms`. Returns a list shaped exactly like a plain Rekognition reply, so nothing downstream knows tiling happened |
| `get_suppression_regions` | Find hard non-vegetation objects | One `detect_labels` call; keeps instance boxes whose label is in the suppression list and not in the protect list. Plant labels can never suppress, because worms live on plants. Returns `[]` on any error so detection is never blocked by this call |
| `_covered_fraction` | How much of a box sits inside a region | Intersection area divided by the BOX's own area, not by the union. The question here is "is this box on top of that object", not "are these the same object" |
| `suppress_nonveg` | Drop boxes sitting on hard objects | Drops a target box when `_covered_fraction` against any suppression region reaches `SUPPRESS_COVERAGE` (0.5). Boxes with no geometry pass through untouched |
| `get_bedrock_client` | Build the Bedrock client once | Lazy construction behind a lock. Verification runs in a thread pool, and without the lock the first verified frame after a cold start could build the client from several threads at once |
| `crop_box_bytes` | Cut the crop the verifier will see | Pads the box by a fraction of its OWN size with a pixel floor, clamps the padded rectangle to the frame, crops, then Lanczos-upscales to a target long edge with a hard cap on the scale factor. The pad gives context to compare against; the cap stops a six-pixel box being blown up into pure interpolation noise |
| `_extract_json_object` | Find the JSON in a model reply | Scans for the first balanced `{...}`, tracking string quoting so a brace inside a quoted value does not end the scan early. Replaced an earlier greedy regex that broke on exactly that case |
| `parse_llm_verdict` | Turn the reply into a verdict | Extracts `{"is_larva": bool, "reason": str}`. Returns `None` when the reply cannot be trusted, which the caller treats as "no verdict" rather than as a negative |
| `verify_one_crop` | Ask Bedrock about one crop | One Converse call carrying an image block and the prompt text. Sampling parameters are omitted unless explicitly configured, because newer Claude models reject them. A safety refusal (`stopReason` of `refusal` or `guardrail_intervened`) is treated as no verdict, not as a rejection |
| `_crop_verdicts` | Judge every candidate | Sorts candidates by confidence, caps the count at `LLM_VERIFY_MAX_BOXES`, then crops and judges them through a small thread pool. Returns `(label, verdict-or-None)` pairs |
| `apply_llm_verify_gate` | Decide what survives verification | In the delivered denoiser mode every target box needs a positive verdict to live; rejected and un-judged boxes are dropped. One deliberate exception first: if candidates existed but ZERO verdicts came back, the model is unreachable, so nothing is dropped, the log shouts, and the Lambda falls back to a plain confidence floor |
| `apply_post_gate_cleanup` | Final tidy of what the gate passed | Runs four rules in a fixed order: area cap, display floor, then a pairwise pass that drops a box clashing with any already-kept box by either IoU or containment. Only boxes matching the target label are touched. A per-camera `post_verify_floor` overrides the env default, which is how the dashboard threshold control works without a deploy |
| `_bbox_contain` | The overlap IoU cannot see | Intersection divided by the area of the SMALLER box. Two tiles marking one worm can share almost no union, so IoU calls it 0.02 while this calls it 1.0 |
| `_bbox_iou` | IoU for the cleanup pass | Same measure as `_iou`, on Rekognition-style `{Left, Top, Width, Height}` boxes |
| `lambda_handler` | The whole flow | Parses the event, reads config, fixes orientation, detects, suppresses, verifies, cleans up, writes one DynamoDB record unconditionally, and sends SES mail only on a confirmed detection. Every stage is wrapped so a non-fatal failure degrades that stage rather than losing the frame |

## 2.6 Lambda: pest-monitoring-api (the dashboard API)

Purpose. A single Lambda serving every dashboard route through API Gateway `vzfl7s6z00`. Python 3.12, 256 MB, 30 s, role `pest-monitoring-api-role`, no layers. ~1,095 lines (mirror `lambda/pest-monitoring-api.py`), merged from Gen-1 `pest-control-api` v3.6 + `pest-history-query` v3. WebSocket broadcast was removed; the dashboard polls.

Env vars: `S3_FRAMES_BUCKET` (required), `S3_PROCESSED_BUCKET` (required), `SCHEDULE_EXECUTOR_ARN` (the pest-camera-scheduler function ARN; two-phase deploy, set after that function exists), plus the four table names.

Router. `lambda_handler` reads method + path from the HTTP API v2 `requestContext.http` payload and dispatches. `OPTIONS *` returns CORS headers unconditionally. Every response goes through `cors_response` (Allow-Origin `*`, Allow-Headers Content-Type, Authorization) with a Decimal-safe JSON encoder (`_json_default`).

Write protection. Two allowlists guard DynamoDB writes against arbitrary key injection. `GLOBAL_ALLOWED`: email_enabled, recipient_email, additional_recipients, auto_capture, capture_interval. `CAMERA_ALLOWED`: label, target_label, model_type, custom_model_arn, min_confidence, model_running, default_waypoint_id, kvs_stream_name, stream_enabled, tiling_enabled, llm_verify_enabled, llm_model_id, post_verify_floor, max_runtime_min. Additions in order: `tiling_enabled` 2026-07-13 (the dashboard "Zoom scan" toggle), `llm_verify_enabled` 2026-07-22, then the v6.2/v6.3 trio — `post_verify_floor` (the dashboard threshold control edits THIS, the display/denoise floor after the LLM check, never `min_confidence`, the candidate floor before it), `max_runtime_min` (per-camera watchdog window), and `llm_model_id` (per-camera verification-model alias; a Test upload's `__llm-` key still wins for that one run).

Routes and their data access:

| Route | Handler | What it touches |
|---|---|---|
| GET /settings | `handle_get_settings` | Reads the system-config row + a full cameras scan (`list_cameras_as_map`, paginated); returns the old nested-map shape `{...global, cameras: {id: {...}}}` for dashboard compatibility |
| POST /settings | `handle_post_settings` | Allowlisted `update_item` on one camera row (`{camera_id, fields}`) and/or the global row |
| POST /model/start | `handle_model_start` | Resolves `custom_model_arn` from the camera row, `rekognition.start_project_version` (1 inference unit), sets `model_running=true` |
| POST /model/stop | `handle_model_stop` | `stop_project_version`, sets `model_running=false` |
| GET /model/status | `handle_model_status` | Resolves the PROJECT ARN by prefix-matching `describe_projects` (the project id is not derivable from the version ARN by string-splitting), then `describe_project_versions` for the real RUNNING/STOPPED status, per camera or for all |
| GET /presigned-url | `handle_presigned_url` | `method=PUT`: presigned POST to the frames bucket (1 B - 25 MB content-length condition), used by Test upload. `method=GET`: bucket chosen by key shape — keys starting `frames/` from the frames bucket, prefix-less keys from the processed bucket (legacy annotated images) |
| POST /detection/verify | `handle_verify_detection` | Resolves the composite key by querying the first row for the image_id; per-bbox verdicts into the `verifications` map (`SET verifications.#idx = "TP"/"FP"`, `REMOVE` to clear, with an `if_not_exists` init) or the legacy image-level `verified` bool |
| DELETE /detection | `handle_delete_detection` | Permanent delete; detailed below |
| GET /history | `handle_get_history` | Filtered `scan` of detections (camera, zone, detected, pest_type, source, date_from/date_to, model substring), paginated with a 10,000-scanned cap, sorted desc by detection_time, limit max 500. The date filter is a plain string range, which works for BOTH the old space-separated and new ISO timestamp formats |
| GET /cost | `handle_cost` | Cost Explorer `get_cost_and_usage` grouped by SERVICE (daily or monthly), plus a month-to-date total; touches no project tables |
| GET /identities | `handle_get_identities` | Merges system-config recipients, then SES `get_identity_verification_attributes` for each |
| POST /identities | `handle_post_identity` | `ses.verify_email_identity` (sends the confirmation email) + appends to `additional_recipients` |
| DELETE /identities | `handle_delete_identity` | `ses.delete_identity` (non-fatal) + removes from the list; refuses to remove the primary recipient |
| GET /schedule | `handle_get_schedule` | Reads the `schedule` map from camera rows |
| POST /schedule | `handle_post_schedule` | Creates/removes the START rule (v6.2, below) + stores the schedule map on the camera row; deletes any legacy stop rule on every save |
| DELETE /schedule | `handle_delete_schedule` | Deletes both rules, sets `schedule={enabled: false}` |
| GET /schedule-logs | `handle_get_schedule_logs` | Scan of schedule-logs, optional camera filter, newest first, limit max 500 |
| POST /stream/start, /stream/stop | `handle_stream_start/stop` | Toggle `stream_enabled` on the camera row. Never touches KVS; the Orin-side kvs_controller polls the flag and starts/stops the GStreamer producer |
| GET /stream/status | `handle_stream_status` | Read-only stream flags per camera. THE one unauthenticated route (2.11); the Orin/mini-PC controller polls it without a JWT |

DELETE /detection (v4.3 of the API, added 2026-07-07, route id `glwyqo0`, JWT-gated) in detail. `DELETE /detection?image_id=<key>`:
1. Query ALL rows for the image_id (duplicates possible). Zero rows = 404 and NOTHING is touched — the endpoint can never become an arbitrary S3 key deleter.
2. Delete S3 objects FIRST. Frame keys are deleted only under the `frames/` prefix, gated in code AND in IAM (`s3:DeleteObject` in `pest-monitoring-api-policy` is scoped to `frames/*`), so training assets under `assets/` and `datasets/` are unreachable both ways. Legacy `processed_image_key` objects are deleted from the processed bucket.
3. Any S3 failure aborts with 500 BEFORE rows are removed. The record is the only index to the object, so it must outlive it. The call is safely retryable (`delete_object` on a gone key is a no-op).
4. Then delete every DynamoDB row.

Dynamic schedules — START-ONLY since v6.2 (Runzhe's call). `handle_post_schedule` creates ONE classic EventBridge rule per camera, `pest-sched-{camera_id}-start`, cron built by `_cron_expression` (`cron(mm hh ? * MON,TUE.. *)`, UTC), targeting `SCHEDULE_EXECUTOR_ARN` with an Input payload `{camera_id, action, schedule_id, trigger_time}` and a per-rule `lambda:AddPermission` (StatementId `sched-invoke-<rule>`, Principal `events.amazonaws.com`, SourceArn built from the account id that `_account_id_from_context` extracts from the invocation context). No stop rule exists any more: the model starts at the chosen time and the watchdog closes it after the camera's `max_runtime_min` (2.9). Every save deletes any legacy `-stop` rule so old pairs die off; `_delete_scheduled_rule` removes targets, rule, and permission, each best-effort. The schedule map on the camera row accordingly has no `end_time` field.

Gotchas:
- `/history` and `/schedule-logs` are table scans. Acceptable at project volume (hundreds of rows), not at scale; the `by-pest-time` GSI exists but these handlers do not use it.
- GET /settings returns every camera field to any authenticated user, including the model ARN. Non-secret, but know it is exposed.
- `/cost` needs `AWSBillingReadOnlyAccess` on the role; without it the route returns 500.
- Schedule times are entered in Singapore local time and converted to UTC **by the API, not by the caller**. `_cron_expression` subtracts a fixed 8 hours (SGT has no daylight saving) and, when that crosses midnight, shifts every day in the list back by one — Monday 05:40 SGT becomes `cron(40 21 ? * SUN *)`. Do not pre-convert on the dashboard side or the schedule will fire eight hours early. An earlier version passed the SGT time straight through and every schedule fired eight hours late; that is the bug this conversion exists to fix.

**Function reference: the helpers behind the routes.** The route table above names the handler for each path. These are the shared functions those handlers call.

| Function | Job | Rule or algorithm inside |
|---|---|---|
| `cors_response` | The single exit point for every response | Wraps status and body with the CORS headers and serialises with `_json_default`. Nothing returns a raw dict, so no route can accidentally omit CORS |
| `_json_default` | Make DynamoDB data JSON-safe | Converts `Decimal` to `int` when it has no fractional part and to `float` otherwise. DynamoDB returns every number as `Decimal`, which `json.dumps` refuses |
| `_account_id_from_context` | Learn our own account id at run time | Splits it out of the invocation context's function ARN, so the EventBridge `SourceArn` is built for whatever account the stack was deployed into. Nothing is hard-coded |
| `get_camera` / `list_cameras_as_map` | Read camera rows | `get_item` for one; a paginated `scan` for all, reshaped into `{camera_id: {...}}` because the dashboard was written against that older nested shape |
| `update_system_config` / `update_camera` | Write config safely | Build an `UpdateExpression` from only the keys present in the allowlist, with `ExpressionAttributeNames` for every field so a reserved word cannot break the call. A key outside the allowlist is silently ignored, which is what stops arbitrary attribute injection |
| `_camera_arn` | Resolve a camera's model ARN | Reads the row and raises on an unknown camera, so the model start/stop routes fail loudly instead of calling Rekognition with an empty ARN |
| `_rule_name` | Name the EventBridge rule | `pest-sched-{camera_id}-{action}`, one deterministic name per camera, so a save can find and replace its own rule without storing an id |
| `_cron_expression` | Build the UTC cron from a local time | Subtracts a fixed 8 hours for SGT and shifts the day list back one when that crosses midnight (see the gotcha above) |
| `_put_scheduled_rule` | Create the rule and let EventBridge invoke the target | Puts the rule, sets the scheduler Lambda as target with a JSON input payload, then adds a per-rule `lambda:AddPermission` whose `SourceArn` is built from the account id discovered at run time. Refuses to run if `SCHEDULE_EXECUTOR_ARN` is unset, because a rule with no target is worse than no rule |
| `_delete_scheduled_rule` | Remove a rule completely | Removes targets, then the rule, then the permission, each wrapped so a missing piece does not abort the rest. This is why a half-created rule can always be cleaned up by saving again |

Inspect: Console Lambda → `pest-monitoring-api`; CLI `aws lambda get-function-configuration --function-name pest-monitoring-api --profile prod`.

## 2.7 Lambda: pest-camera-scheduler

Purpose. The executor behind the dynamic `pest-sched-*` rules. ~167 lines. Python 3.12, 128 MB, 60 s, role `pest-camera-scheduler-role`, no static trigger of its own (invoked by the rules and, in principle, directly via `SCHEDULE_EXECUTOR_ARN`).

Handler flow: validate `camera_id` and `action` ("start"/"stop") from the event payload → `get_camera` → resolve `custom_model_arn` (missing or placeholder = logged failure row + 400) → `rekognition.start_project_version(MinInferenceUnits=1)` or `stop_project_version` → `update_camera` sets `model_running` → `write_schedule_log` writes the audit row (status success/failure, message, schedule_id, trigger_time). `ResourceInUseException` (already in the target state, e.g. a start rule firing on a running model) is treated as a soft success so schedules do not spam failure rows.

Env vars: `TABLE_CAMERAS`, `TABLE_SCHEDULE_LOGS`.

Gotcha: `SCHEDULE_EXECUTOR_ARN` on `pest-monitoring-api` must point at this function or POST /schedule fails with "deploy pest-camera-scheduler first".

## 2.8 Lambda: kvs-hls-handler

Purpose. Turns a KVS stream name into a playable HLS URL for the dashboard Live tab. Kept separate from `pest-monitoring-api` on purpose: its IAM surface is only two kinesisvideo read actions (no DynamoDB, S3, or SES), and KVS is a separable phase. Python 3.12, 128 MB, 30 s, role `kvs-hls-handler-role`, NO env vars. Source mirror: `deployer/audit/kvs-hls-handler_src/lambda_function.py` (rescued during the 2026-07-08 audit; it was never in `lambda/`).

Route: `GET /video-playback?stream=<kvs_stream_name>` on API `zwpcbivmsj`, integration `33f69kt`, JWT-gated.

Handler flow: `get_hls_url(stream)` calls `kinesisvideo.get_data_endpoint(APIName="GET_HLS_STREAMING_SESSION_URL")` — the endpoint is per-account and per-stream, always resolved at runtime, never hardcoded — then builds a `kinesis-video-archived-media` client against that endpoint and calls `get_hls_streaming_session_url(PlaybackMode="LIVE", Expires=3600)`. Errors return 503 with a hint: the stream does not exist in this account, or it exists with no live fragments (the producer is not pushing). The /stream/start|stop|status control routes stay in `pest-monitoring-api`; they only toggle a DynamoDB flag and never touch KVS.

## 2.9 Lambda: pest-model-watchdog

Purpose. Cost guard. A running Rekognition Custom Labels endpoint bills per hour; this function guarantees no model runs forever, no matter HOW it was started (dashboard, schedule, console, or CLI). Python 3.12, 128 MB, 30 s. Role: `pest-model-watchdog-role` on the production account, created by the ARGUS deployer (2.17). Historical note: the retired development account carried a console-generated variant, `pest-model-watchdog-role-78asdw2b` under a `/service-role/` path; the deployer deliberately drops the suffix and path so every fresh deployment gets the plain name. Source mirror: `lambda/pest-model-watchdog.py` (v6.2; the pre-v6.2 audit rescue at `deployer/audit/pest-model-watchdog_src/lambda_function.py` lacks the per-camera limit).

Trigger: EventBridge Scheduler `pest-model-watchdog-15min`, `rate(15 minutes)` (2.15; the ARGUS deployer creates the same schedule on the production account). Env: `TABLE_CAMERAS`, `MAX_RUNTIME_MIN=75` live — but since v6.2 that env value is only the FALLBACK: the per-camera DDB field `max_runtime_min` wins when present, and `worm_cam` carries **45** (set 2026-08-10, restored from a temporary 240 used for the 08-07 test window). The dashboard writes `max_runtime_min` through the settings API, and it is what makes start-only scheduling (2.6) safe: a scheduled morning run closes itself ~45 minutes after start.

NOTE: doc lag. The function docstring says "every 10 minutes" and a 60-minute default; live truth is a 15-minute schedule with the per-camera/env limits above. Worst case a model runs its limit + 15 minutes before being stopped.

Handler flow, per camera row with a real `custom_model_arn`:
1. `project_arn_for_version` resolves the project ARN by listing `describe_projects` and prefix-matching on `project/<name>/`, because the project id in the project ARN is not present in the version ARN (same trick as the API's /model/status).
2. `model_status` reads the authoritative status from `describe_project_versions`.
3. Not RUNNING → remove any stale `model_started_at` stamp so the next start measures fresh.
4. RUNNING with no stamp → self-stamp `model_started_at = now` (first sighting). Self-stamping is what makes the watchdog start-method-agnostic.
5. RUNNING past the limit → `stop_project_version`, set `model_running=false`, remove the stamp.

Handler flow addendum (v6.2): step 5's limit is `int(cam.get("max_runtime_min", MAX_RUNTIME_MIN))` with a try/except fallback to the env value on a malformed field.

Operational consequence to remember during long test sessions: the watchdog WILL stop your endpoint mid-run after the camera's `max_runtime_min` (worm_cam: ~45 minutes). This is working as designed, not a fault — the armyworm endpoint was auto-stopped exactly this way after the 2026-08-07 test window. For a longer session, raise the camera's `max_runtime_min` for the day (POST /settings) or temporarily disable the schedule (2.15), and put it back after. Do not reflexively stop endpoints yourself between test runs; check state first (`GET /model/status` or the CLI in 2.18).

## 2.10 Lambda layer: fyp-pillow

Provides Pillow 12.2.0 (cp312, manylinux2014_x86_64) to the processor. Live ARN `arn:aws:lambda:us-east-1:366356442579:layer:fyp-pillow:2` (7,888,247 bytes). Version :1 is dead: it was built without the compiled `_imaging*.so`, so every PIL import failed and no boxes could be tiled or cropped.

Rebuild for a fresh account:
1. `pip install --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 --only-binary=:all: --target ./python Pillow==12.2.0`
2. VERIFY `python/PIL/_imaging*.so` and `python/pillow.libs/` exist (the exact :1 bug), then zip the `python/` folder at archive root.
3. `aws lambda publish-layer-version --layer-name fyp-pillow --zip-file fileb://**layer-zip** --compatible-runtimes python3.12 --compatible-architectures x86_64 --profile **PROFILE**` — capture the returned version number (a fresh account gets :1; do not hardcode :2) and attach it: `aws lambda update-function-configuration --function-name pest-detection-processor --layers **new-layer-arn** --profile **PROFILE**`
Console: Lambda → Layers → Create layer; then function → Code → Layers → Add a layer.

## 2.11 API Gateway: HTTP API zwpcbivmsj

`pest-monitoring-api-gateway`, HTTP API v2, single auto-deploy `$default` stage, base URL `https://zwpcbivmsj.execute-api.us-east-1.amazonaws.com` (no stage suffix). The production-account counterpart is `vzfl7s6z00` (`https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com`), created by the ARGUS deployer 2026-08-10 with the same 21-route/JWT layout; the route ids below are the dev account's. CORS at the API level, from `lambda/cors.json`: AllowOrigins `*`, AllowMethods GET, POST, DELETE, OPTIONS, AllowHeaders content-type, authorization, MaxAge 3600. The `authorization` entry is what lets the browser send the Cognito JWT.

JWT authorizer `cognito-dashboard`, id `enxa26`: IdentitySource `$request.header.Authorization`, Issuer `https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ea0aJdusl`, Audience `4husu6afr835e235eu9dqp8av6`. Applied to every route since 2026-07-06 EXCEPT `GET /stream/status` (AuthorizationType NONE), which the Orin/mini-PC kvs_controller polls unauthenticated. That route returns only stream flags and writes nothing.

Two integrations, both AWS_PROXY POST, payload format 2.0, 30 s timeout: `t7ggvzn` → `pest-monitoring-api` (20 routes) and `33f69kt` → `kvs-hls-handler` (`GET /video-playback` only).

All 21 routes with live route ids: GET /cost `1xu5zhs`; DELETE /identities `1zwy2vl`; DELETE /schedule `2dlsspt`; POST /stream/start `33iv13c`; GET /presigned-url `58gsnz6`; POST /detection/verify `7wgkuzm`; GET /video-playback `ao22ksu` [→ 33f69kt]; GET /settings `b3kl8si`; GET /stream/status `gcc7355` [NO AUTH]; DELETE /detection `glwyqo0`; POST /stream/stop `i8pcllp`; POST /settings `jedpy5t`; POST /model/stop `krzufjf`; GET /identities `nqy131o`; GET /schedule-logs `odwxz00`; POST /model/start `t4w6czh`; POST /schedule `tos63cu`; GET /history `ttif38k`; POST /identities `w0ivphi`; GET /schedule `wqh3mvj`; GET /model/status `zbh9lli`.

Lambda invoke permissions: `pest-monitoring-api` has a broad SourceArn `arn:aws:execute-api:us-east-1:366356442579:zwpcbivmsj/*`; `kvs-hls-handler` is route-scoped to `.../zwpcbivmsj/*/*/video-playback`.

Inspect:
- Console: API Gateway → APIs → pest-monitoring-api-gateway → Routes / Authorization / Integrations / CORS.
- CLI: `aws apigatewayv2 get-routes --api-id vzfl7s6z00 --profile prod` (also `get-authorizers`, `get-integrations`, `get-api`).

Recreate: `aws apigatewayv2 create-api --name **NAME** --protocol-type HTTP --cors-configuration file://lambda/cors.json --profile **PROFILE**` → `create-authorizer` (type JWT, issuer/audience from the NEW Cognito ids) → two `create-integration` → 21 `create-route` calls (attach the authorizer to all but GET /stream/status) → `create-stage --stage-name '$default' --auto-deploy` → the two `aws lambda add-permission` calls with SourceArns containing the **new API id**. Then write the new base URL into `web/dashboard_v4/js/config.js`.

Emergency authorizer detach (if Cognito ever locks everyone out): `aws apigatewayv2 update-route --api-id vzfl7s6z00 --route-id **ROUTE_ID** --authorization-type NONE --profile prod` per route; the prepared commands live in `docs/dashboard.md`.

Dead APIs, do not recreate: REST `3go4jj1698` (PestDetectionAPI, already deleted) and WebSocket `j4v2m5cbte` (retired when the dashboard moved to polling).

## 2.12 Cognito

User pool `pest-dashboard-users`, id `us-east-1_ea0aJdusl` (production account: pool `us-east-1_9selFDHpc`, client `6vebotf45bp8u46cnraddiaplv`, same admin-create-only design; first production sign-in user created and CONFIRMED 2026-08-11). Email as the username attribute, auto-verified email, MFA off, **AllowAdminCreateUserOnly=true** (nobody can self-register; every account is created by the operator), Cognito-default email sending (no SES dependency for auth mails), no hosted UI, no OAuth, no domain, no Lambda triggers, password policy minimum length 8 with lowercase + numbers required, account recovery via verified email.

App client `dashboard-web`, id `4husu6afr835e235eu9dqp8av6`: public SPA client with NO secret, auth flows `ALLOW_USER_PASSWORD_AUTH` + `ALLOW_REFRESH_TOKEN_AUTH`, id/access token validity 12 h, refresh 30 days, token revocation on. The dashboard signs in with direct USER_PASSWORD_AUTH against the client id; the pool id is not referenced by the frontend. The client id is a public identifier and safe to commit.

User management (admin-create-only means these are the only ways users exist):
- Console: Amazon Cognito → User pools → pest-dashboard-users → Users → Create user.
- CLI create: `aws cognito-idp admin-create-user --user-pool-id us-east-1_9selFDHpc --username **EMAIL** --temporary-password **TEMP_PASSWORD** --profile prod`
- CLI set permanent password: `aws cognito-idp admin-set-user-password --user-pool-id us-east-1_9selFDHpc --username **EMAIL** --password **NEW_PASSWORD** --permanent --profile prod`
- CLI delete: `aws cognito-idp admin-delete-user --user-pool-id us-east-1_9selFDHpc --username **EMAIL** --profile prod`

Recreate: `aws cognito-idp create-user-pool --pool-name pest-dashboard-users --username-attributes email --auto-verified-attributes email --admin-create-user-config AllowAdminCreateUserOnly=true --policies "PasswordPolicy={MinimumLength=8,RequireLowercase=true,RequireNumbers=true,RequireUppercase=false,RequireSymbols=false}" --profile **PROFILE**`, then `aws cognito-idp create-user-pool-client --user-pool-id **NEW_POOL_ID** --client-name dashboard-web --no-generate-secret --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH --token-validity-units "IdToken=hours,AccessToken=hours,RefreshToken=days" --id-token-validity 12 --access-token-validity 12 --refresh-token-validity 30 --profile **PROFILE**`. Feed the new ids into the API Gateway authorizer and `config.js` (client id + region only). Seed zero users; the operator admin-creates their own. Console: Amazon Cognito → Create user pool.

## 2.13 CloudFront: E1423RGLAXWNSI

Distribution `E1423RGLAXWNSI` → `https://d1twcdquexdgj8.cloudfront.net`. This is the development-account dashboard URL. The production distribution is `E1YADURLSAVNFA` → **`https://d1dtoxef7qmugl.cloudfront.net`** (Deployed and serving the ARGUS dashboard since 2026-08-11; same design as below with the `argus-dashboard-506868652945` website origin).

Configuration:
- One origin, id `s3-website-pest-dashboard`: the S3 STATIC WEBSITE endpoint `pest-dashboard-366356442579.s3-website-us-east-1.amazonaws.com` with CustomOriginConfig `OriginProtocolPolicy=http-only`. Both choices are forced: only the website endpoint honours the index/error documents (the SPA fallback), and website endpoints have no HTTPS, so an https origin policy returns 502. No OAC/OAI (the bucket is public-read).
- Default behavior: ViewerProtocolPolicy redirect-to-https, methods HEAD+GET only, Compress on, CachePolicyId `4135ea2d-6df8-44a3-9df3-4b5a84be39ad` = the AWS-managed **Managed-CachingDisabled** policy (the id is the same in every account; hardcode it).
- CachingDisabled rationale: every request passes through to S3, so a plain `aws s3 sync` is a complete redeploy with no invalidation step and no cache-propagation wait. At this project's traffic the lost caching is irrelevant; the removed operational failure mode (users seeing a stale build after a deploy) is not. Browser-side caching is separately defeated by the `--cache-control "no-cache, must-revalidate"` flag on the sync (2.3.3).
- DefaultRootObject index.html, default CloudFront certificate, no aliases, no WAF, no logging, no custom error responses (the S3 website ErrorDocument does the SPA fallback).

Inspect: Console CloudFront → Distributions → E1YADURLSAVNFA; CLI `aws cloudfront get-distribution-config --id E1YADURLSAVNFA --profile prod`.

Recreate: `aws cloudfront create-distribution --distribution-config file://**config.json** --profile **PROFILE**` using `deployer/audit/cloudfront__E1YADURLSAVNFA_distribution-config.json` as the template, with a FRESH CallerReference (CloudFront rejects duplicates; live was `pest-dashboard-cf-2026-07-06`) and the new website-endpoint origin. Deployment takes minutes to reach Deployed; capture the new id + `*.cloudfront.net` domain and add the domain to the frames-bucket CORS origins and to wherever the dashboard URL is published.

## 2.14 Kinesis Video Streams

KVS exists only on the development account: the 2026-08-10 production deploy ran without `--live-view`, so the production account has NO streams, `worm_cam.kvs_stream_name` is empty there, and the dashboard Live tab and the devices' kvs_controller cannot work against production until streams are created (whether the demo needs them is the project owner's call). Two dev-account streams, both `DataRetentionInHours=2`, AWS-managed KMS key `alias/aws/kinesisvideo` (no CMK):
- `armyworm-cam-stream` (ARN `arn:aws:kinesisvideo:us-east-1:366356442579:stream/armyworm-cam-stream/1779436101323`) — live; fed by the KVS Producer SDK compiled for ARM64 on the Jetson Orin, GStreamer passthrough pipeline `rtspsrc → rtph264depay → h264parse → kvssink` off the SIYI A8 Mini RTSP feed.
- `moth-cam-stream` — parked (the Hikvision camera was repurposed).
- `FYP-PROJECT` is Wilbur's Gen-1 stream (168 h retention): dead, do not recreate.

Control flow: dashboard → POST /stream/start toggles `stream_enabled` in DynamoDB → the Orin kvs_controller polls `GET /stream/status` (the unauthenticated route) and starts/stops the producer → the dashboard Live tab calls `GET /video-playback` → `kvs-hls-handler` mints an HLS URL.

Inspect: Console Kinesis Video Streams → Streams; CLI `aws kinesisvideo list-streams --profile prod` and `aws kinesisvideo describe-stream --stream-name armyworm-cam-stream --profile prod`.
Recreate: `aws kinesisvideo create-stream --stream-name **STREAM_NAME** --data-retention-in-hours 2 --profile **PROFILE**`. Stream names carry no account id. The HLS data endpoint is per-account per-stream and is always resolved at runtime; never write it into config.

## 2.15 EventBridge schedules

Two mechanisms coexist; do not confuse them.

1. EventBridge SCHEDULER (the newer service) — one production schedule, `pest-model-watchdog-15min` (group `default`): `rate(15 minutes)`, timezone Asia/Singapore (cosmetic for rate()), State ENABLED, FlexibleTimeWindow OFF, MaximumRetryAttempts 0, target = the watchdog Lambda ARN with no Input. Scheduler invokes Lambda by ASSUMING a role (live: the console-generated `Amazon_EventBridge_Scheduler_LAMBDA_d585792c00` with `lambda:InvokeFunction` on the watchdog), not via a Lambda resource policy.
   - Inspect: Console → Amazon EventBridge → Scheduler → Schedules; CLI `aws scheduler get-schedule --name pest-model-watchdog-15min --profile prod`.
   - Disable temporarily (long test session): Console → the schedule → Disable, or `aws scheduler update-schedule --name pest-model-watchdog-15min --schedule-expression "rate(15 minutes)" --flexible-time-window Mode=OFF --target file://**target.json** --state DISABLED --profile prod` (update-schedule requires re-passing the full target; read it with get-schedule first).
   - Recreate: create one stably named invocation role (trust `scheduler.amazonaws.com` with condition `aws:SourceAccount` = **ACCOUNT_ID**; policy `lambda:InvokeFunction` on BOTH the watchdog and pest-camera-scheduler), then `aws scheduler create-schedule --name pest-model-watchdog-15min --schedule-expression "rate(15 minutes)" --flexible-time-window Mode=OFF --target '{"Arn":"**WATCHDOG_ARN**","RoleArn":"**INVOCATION_ROLE_ARN**","RetryPolicy":{"MaximumEventAgeInSeconds":86400,"MaximumRetryAttempts":0}}' --profile **PROFILE**`
2. Classic EventBridge RULES — created at RUNTIME by `pest-monitoring-api` for per-camera model schedules: `pest-sched-{camera_id}-start/stop`, cron in UTC, target pest-camera-scheduler with an Input payload, invoke permission granted per rule on the function's resource policy (2.6). Nothing to pre-create beyond the api role's events/lambda permissions.

Gen-1 leftovers in the live account (`model-start-schedule`, `model-stop-schedule`, `frame-extraction-schedule`, all still ENABLED and firing daily against dead Lambdas = wasted spend; `ExtractFrameEveryMinute` DISABLED): do not recreate; disabling them in the source account is recommended.

## 2.16 SES

The alert channel. BOTH accounts are in the SES SANDBOX (`ProductionAccessEnabled=false`, 200 messages/24 h, 1/sec): they can only send FROM and TO verified identities. That is fine here because sender and default recipient are the same verified address, `rex2956550768@gmail.com` (both the processor's `SENDER_EMAIL` env var and system-config `recipient_email`). The address was verified on the production account 2026-08-11 (until the verification link is clicked, `SendingEnabled=false` and no alert mail goes anywhere — the exact state the production account sat in for a day). No SNS, no SQS, no configuration sets. Legacy identities `teowilbur@gmail.com` (Wilbur, dead) and `neobkee@gmail.com` (unrelated) also exist on the development account; ignore them.

Adding a recipient: dashboard → POST /identities, or Console SES → Identities → Create identity → Email address, or CLI `aws ses verify-email-identity --email-address **EMAIL** --profile prod`. The person must click the verification link, or sandbox SES silently refuses delivery; the processor logs "recipient may be unverified" on send failure.

Fresh account: verify sender + recipient (`verify-email-identity` each, poll `aws ses get-identity-verification-attributes --identities **EMAIL** --profile **PROFILE**` until Success), set the processor env var and the system-config row. Leaving the sandbox is a manual production-access request to AWS Support (about 1-2 business days); at project volume the sandbox is sufficient.

## 2.17 IAM roles and policies

All five Lambda execution roles share the same trust policy (Principal `lambda.amazonaws.com`, `sts:AssumeRole`; `deployer/audit/iam__trust-policy-lambda.json`) and all carry `AWSLambdaBasicExecutionRole` for CloudWatch logs. Raw policy JSONs live in `deployer/audit/iam__*.json`; the checked-in sources for two of them are `lambda/ddb-policy.json` and `lambda/bedrock-policy.json`.

Staleness caveat on the audit JSONs: most are 2026-07-08 snapshots. One known gap remains: `iam__pest-detection-processor-role__inline__pest-detection-processor-policy.json` contains only `rekognition:DetectCustomLabels`; the `rekognition:DetectLabels` action was added 2026-07-13 for v4.3 suppression. Two later processor-role policies ARE now dumped fresh (captured from the live development-account role 2026-08-11): `iam__pest-detection-processor-role__inline__bedrock-verify.json` (wildcarded inference-profile families including `us.anthropic.*`, so Sonnet 4.6 is covered — use this, not the narrower `lambda/bedrock-policy.json`) and `iam__pest-detection-processor-role__inline__s3-frames-write.json` (`s3:PutObject` on `frames/*` for the EXIF write-back). The same 2026-08-11 pass wired both into the ARGUS deployer's role build (account/region rewritten at deploy). Note the production role's provenance: it was deployed 2026-08-10, BEFORE that deployer fix, so it came up without `bedrock-verify`, `s3-frames-write`, or `rekognition:DetectLabels`; it was patched 2026-08-12 by `migration/fix_processor_iam.py` after the first validation run exposed the gap (see 9.3.14), and now matches the development role action-for-action. Recreating the processor role verbatim from the 2026-07-08 JSON alone still breaks v4.3 suppression; add DetectLabels and the two dumped policies. The table below describes the LIVE state.

| Role | Managed policies | Inline / customer-managed policies (content) |
|---|---|---|
| pest-detection-processor-role | AWSLambdaBasicExecutionRole | `pest-detection-processor-policy`: s3:GetObject on frames/\*, s3:Get+PutObject on processed/\*, DDB PutItem/GetItem/UpdateItem/Query on detections + index, GetItem on cameras, `rekognition:DetectCustomLabels` + `rekognition:DetectLabels` (v4.3 add, 2026-07-13; absent from the 2026-07-08 audit JSON) on `*`, ses:SendEmail/SendRawEmail (the live policy also grants ses:ListVerifiedEmailAddresses). `read-system-config`: DDB GetItem on system-config. `bedrock-verify` (dump: `deployer/audit/iam__pest-detection-processor-role__inline__bedrock-verify.json`): bedrock:InvokeModel + bedrock:Converse on wildcarded inference-profile families (`us.anthropic.*`, `global.anthropic.*`, `us.amazon.*`, `us.meta.*`, `us.mistral.*`) AND on the region-wildcarded foundation-model ARNs (`arn:aws:bedrock:*::foundation-model/...` — cross-region inference routes to several regions, so the foundation-model statement must be region-wildcarded). `s3-frames-write` (dump: `deployer/audit/iam__pest-detection-processor-role__inline__s3-frames-write.json`): s3:PutObject on frames/\* for the v4.8.3 EXIF write-back |
| pest-monitoring-api-role | AmazonSESFullAccess, AWSLambdaBasicExecutionRole, AWSBillingReadOnlyAccess | `pest-monitoring-api-policy`: S3 Get/Put/List/Delete on both armyworm buckets (DeleteObject scoped to `frames/*`); `dynamodb:*` on cameras + detections + index; rekognition Describe/Start/StopProjectVersion; events PutRule/DeleteRule/PutTargets/RemoveTargets/ListTargetsByRule/DescribeRule scoped to `rule/pest-sched-*`; scheduler CRUD scoped to `schedule/default/pest-camera-*`; lambda Add/RemovePermission/InvokeFunction on pest-camera-scheduler; iam:PassRole conditioned to `scheduler.amazonaws.com`; kinesisvideo read. `pest-monitoring-ddb-full-access` (mirror `lambda/ddb-policy.json`): six DDB verbs on all four pest tables + the detections index. `read-processed-armyworm`: s3:GetObject on processed/\* |
| pest-camera-scheduler-role | AWSLambdaBasicExecutionRole | `pest-camera-scheduler-policy`: rekognition Start/Stop/DescribeProjectVersions; DDB GetItem/UpdateItem on cameras |
| kvs-hls-handler-role | AWSLambdaBasicExecutionRole | customer-managed `kvs-hls-handler-rolePolicy`: kinesisvideo:GetDataEndpoint + GetHLSStreamingSessionURL only |
| pest-model-watchdog-role | AWSLambdaBasicExecutionRole (generated scoped-logs variant live) | `Rekognition-DB-handler`: DDB Scan/UpdateItem on cameras; rekognition DescribeProjects/DescribeProjectVersions/StopProjectVersion |

Plus the EventBridge Scheduler invocation role (2.15). Known deviations, recorded in `deployer/STACK_MANIFEST.md`: the api-policy's `iam:PassRole` points at a role name that does not exist live (latent and harmless — per-camera schedules use classic rules with the Lambda resource policy, not PassRole); the api-policy still grants InvokeFunction on the dead Gen-1 `kvs-hls-url-generator` (drop on recreate); `dynamodb:*` and `AmazonSESFullAccess` are broader than needed but functional.

Apply an inline policy (the pattern used for every one of these):
- Console: IAM → Roles → role name → Add permissions → Create inline policy → JSON tab → paste → name → Create.
- CLI: `aws iam put-role-policy --role-name pest-detection-processor-role --policy-name bedrock-verify --policy-document file://lambda/bedrock-policy.json --profile prod`

Bedrock model access is separate from IAM: the model must ALSO be enabled under Bedrock → Model access in the console for the region (Modify model access → tick the model → Save), or every call is AccessDeniedException even with perfect IAM. Claude Haiku 4.5 was confirmed AUTHORIZED on the development account 2026-07-22 after the Anthropic use-case form (Sonnet 4.6 followed). On the production account both Sonnet 4.6 and Haiku 4.5 were found already AUTHORIZED at the 2026-08-10 audit — no use-case-form wait. Budget for that approval lag on any other fresh account; the LLM gate is the pipeline's spine.

## 2.18 Rekognition Custom Labels (backend view)

The models themselves are the subject of the detection chapter; the backend facts:
- Every camera row stores exactly one `custom_model_arn`; all Lambdas resolve it at call time. Model switch or rollback = one DynamoDB write of the ARN (2.4.1 quoting gotcha).
- The endpoint must be RUNNING to serve `detect_custom_labels`. Start/stop via the dashboard, the schedule pair, or CLI: `aws rekognition start-project-version --project-version-arn **MODEL_ARN** --min-inference-units 1 --profile prod` / `aws rekognition stop-project-version --project-version-arn **MODEL_ARN** --profile prod`. Status: `aws rekognition describe-project-versions --project-arn **PROJECT_ARN** --profile prod`. Console: Rekognition → Custom Labels → Projects.
- Running endpoints bill per hour, and TWO can bill at once (armyworm + moth). The watchdog (2.9) is the safety net; the armyworm endpoint being STOPPED outside test windows is the watchdog working, not a fault.
- Live on `worm_cam` (dev account) since 2026-08-07: the v9 retrain `v9-20260805-0713`, `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v9/version/v9-20260805-0713/1785913987295`, F1 0.599. The v9 family's training recipe adds data augmentation (flips, rotations, exposure jitter — the documented 13x build). The low F1 is by design in the current architecture: the detector is a high-recall front end gathering boxes down to `TILE_MIN_CONFIDENCE=8`, and the LLM denoiser plus post-gate cleanup do the rejecting. Do not read 0.599 as pipeline accuracy. Fallback ARNs (v5 F1 0.852 etc.) are listed in `docs/aws.md`.
- Models are ACCOUNT-BOUND. No export, import, copy, or cross-account API exists — a fresh account must create a project, load labeled data, and `train-project-version` (hours, billable), then write the resulting version ARN into the camera row. This was executed for real on the production account (2026-08-10/11): the training set (36,641 objects, 4.27 GB under `training-data/v9/`) was copied server-side by `migration/copy_training_data.py` via a temporary prefix-scoped cross-account bucket policy (revoke with `--revoke` after sign-off), every manifest `source-ref` repointed at `argus-frames-506868652945`, and armyworm version `v9r-prod-20260810` submitted in project `argus-detection`. The moth detector was fully rebuilt from data alone (`migration/migrate_moth.py`): Wilbur's SmartPestProject model could not move, but its 116 TRAIN + 29 TEST labelled images survived in the old account's Rekognition console bucket; the rebuild `moth-prod-20260811` in project `argus-moth-detection` trained to F1 0.991 (original: 0.988 — same data, treat as equivalent).
- THE MANIFEST TRAP (found 2026-08-10; generic lesson worth the box it is printed in): boxes drawn or corrected in the Rekognition console live in the project's DATASET, not in the S3 manifest the dataset was originally created from. After any console labelling, the manifest is stale; rebuilding a dataset from it silently loses the console work. Export the labelled truth with `ListDatasetEntries` (scripted in `migration/train_v9r_on_prod.py`), repoint the `source-ref`s, and create the new dataset from that export. **The dataset — not the S3 manifest — is the source of truth.**

## 2.19 Operations / reproduction

Daily operations quick list:
- Watch the pipeline: CloudWatch → Log groups → `/aws/lambda/pest-detection-processor` (also `pest-monitoring-api`, `pest-model-watchdog`, `kvs-hls-handler`). CLI: `aws logs tail /aws/lambda/pest-detection-processor --follow --profile prod`. Log groups auto-create on first invoke; retention is Never-Expire; no alarms exist.
- Check endpoint state before touching it: dashboard `GET /model/status`, or the describe CLI in 2.18.
- Push a test image through the full pipeline: dashboard Settings → Test upload. This is also how live detection is demonstrated (there are no worms at Jewel to stage). The upload key can carry the `__conf<N>` and `__llm-<alias>` overrides (2.5.1).
- Toggle the LLM gate per camera: flip `llm_verify_enabled` via boto3, or `POST /settings` with body `{"camera_id": "worm_cam", "fields": {"llm_verify_enabled": true}}`. The dashboard's threshold control edits `post_verify_floor` (never `min_confidence`), and its model picker edits `llm_model_id` (2.6).
- Redeploy the dashboard: the s3 sync command in 2.3.3. Redeploy a Lambda: the update-function-code command in 2.5.10.
- After a training run, push the holdout through the live pipeline so results appear on the dashboard (project standing order), and check which endpoints are RUNNING before switching models.

Full reproduction on a fresh account (condensed; `deployer/STACK_MANIFEST.md` is the recreate-level bill of materials and the ARGUS deployer `deployer/deploy.py` automates this order — proven for real 2026-08-10 on account 506868652945: all 15 stages in 103 seconds, post-deploy verification passed, including the tuned processor sizing/env and camera-row seeds):
1. IAM: five execution roles + one scheduler invocation role from the audit policy JSONs, account id and bucket names parameterized. Wait out IAM eventual consistency before attaching anything to them.
2. DynamoDB: four tables (detections with the `by-pest-time` GSI).
3. S3: the buckets (us-east-1: no LocationConstraint). Dashboard bucket order: PAB false → website → policy. Defer the frames notification.
4. Lambda layer: build + publish fyp-pillow (2.10); capture the version.
5. Lambda functions: all five (rename mirrors to `lambda_function.py` inside the zips; kvs-hls-handler comes from `deployer/audit/kvs-hls-handler_src/`; the watchdog MUST come from the repo mirror `lambda/pest-model-watchdog.py` — the audit `*_src` copy is pre-v6.2 and lacks the per-camera `max_runtime_min`, a real deployer bug fixed 2026-08-11). Attach fyp-pillow to the processor. Two-phase env: create pest-camera-scheduler first, then set `SCHEDULE_EXECUTOR_ARN` on the api.
6. Frames-bucket notification: `lambda add-permission` FIRST, then `put-bucket-notification-configuration` with prefix `frames/` (2.3.1).
7. Cognito: pool, then client; capture both ids (2.12).
8. API Gateway: api → authorizer → two integrations → 21 routes → `$default` stage → two invoke permissions (2.11).
9. CloudFront: after the dashboard bucket is public and website-enabled (2.13).
10. Config write-back: new API base URL + Cognito region + client id into `web/dashboard_v4/js/config.js`; new CloudFront domain into the frames CORS; sync the dashboard.
11. KVS streams (2.14) and the EventBridge Scheduler watchdog schedule (2.15).
12. Rekognition: create the project, train, write the version ARN into the camera row (2.18).
13. Seed DynamoDB: the system-config row and the camera rows (model ARNs empty until step 12 resolves them).
14. SES: verify sender + recipient (2.16).

Production migration status (2026-08-11) — what remains before the production account fully replaces the development one:
- Armyworm model `v9r-prod-20260810`: training DONE 2026-08-12 (F1 0.613) and the version ARN is wired into the camera rows. Remaining: repeat the holdout validation re-push through the live pipeline and compare against the development account's equivalent dashboard zones — the first 2026-08-12 attempt was invalidated by the processor-role IAM gap (see 9.3.14) and must be run again. The migration is only proven when the same images give the same detections. At sign-off, restore `worm_cam.max_runtime_min` from the temporary 240 back to 45.
- Repoint the edge devices (Orin / mini-PC): upload bucket → `argus-frames-506868652945`, API base → the new API Gateway URL, and the production camera id.
- Revoke the temporary cross-account S3 read grants (`migration/copy_training_data.py --revoke`, `migration/migrate_moth.py --revoke`, and the Jewel-frames grant) after model sign-off.
- KVS/live view: not migrated; create streams only if the demo needs the Live tab (2.14).
- The development account stays untouched until the production one is proven end to end; its dashboard zones also hold the threshold-study evidence the final report cites. Historical note for the handover: 36 Jewel on-site records (frames + rows, zones zone1/zone2/zone3/field_worm, 2026-07-29..31) were migrated to production by `migration/migrate_jewel_records.py` with S3 keys byte-identical and `model_arn` left pointing at the old account's model on purpose — it is provenance, and rewriting it would falsify history.

Security cautions for reproducers:
- No AWS secret access keys, passwords, or tokens appear in the committed cloud files or in this manual, and none may ever be added. Credentials belong in the CLI profile (`aws configure --profile **PROFILE_NAME**`), never in code, docs, or chat.
- One known exception exists elsewhere in the repo: an old script at `datasets/archive/experiments/pre_v3_abandoned/download.py` contains an inline Roboflow API key (credential stored there, not reproduced here). Do not copy that pattern, and do not redistribute that file.
- Non-secret identifiers (account id, ARNs, bucket names, API/CloudFront/Cognito ids, IPs) are intentionally printed throughout this chapter; they are safe to share.

## 2.20 Cross-references

- Chapter 1 — system overview: where this backend sits in the full capture-to-dashboard chain.
- Chapter 4 (dashboard frontend) — the frontend that consumes `pest-monitoring-api`: module layout of `web/dashboard_v4/`, Cognito login flow, the Test upload panel, gallery delete.
- Chapter 3 (models and training) — Rekognition Custom Labels training history (v1-v9), the negatives limitation, and the evidence base behind tiling and the LLM gates (`docs/detection.md`).
- Chapter 5 (edge: Go2/Orin) — Jetson Orin, SIYI A8 Mini, the KVS producer, the kvs_controller polling loop, and the patrol completion gate that polls `pest-monitoring-detections`. (The mini-PC moth node is Chapter 6.)
- Chapter 7 (deployer, ARGUS) — `deployer/STACK_MANIFEST.md`, `deploy.py`, and the rehearsal plan that automates section 2.19 end to end.
