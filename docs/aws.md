# AWS stack

> **Note on script paths.** Sections below describe work done earlier in
> the project. Some of the scripts they name were removed in the
> 2026-08-21 repository cleanup, which kept only production code and the
> current pipeline. The reasoning and the measurements stand; the paths
> are a record of how the work was done, not files you will find here.


**Account: `506868652945` (NP production, CLI profile `prod`, IAM user
`Student_QianRunzhe`). Region: `us-east-1`.** This is the live system: the stack was
deployed here by the ARGUS deployer on 2026-08-10, both detection models were trained
here, and end-to-end validation passed 2026-08-12. Every operative name, ARN and command
in this file refers to this account.

Retired: `366356442579` (profile `nbk2`, console user `cag_user`) — where the system was
built. Kept read-only while the report is filed; it appears below only inside rows marked
as history. Custom Labels model versions are account-bound and could not be moved, which
is why the production models were retrained rather than copied. Older account
`396278862184` is dormant.

## CLI quirk (important)
History (retired dev account): CloudShell writes were blocked there by a VPC endpoint, so writes had to come from the Windows CLI profile `nbk2` for
writes; CloudShell for reads only.

## Lambdas (Python 3.12)
- `pest-detection-processor` — S3-triggered. Layer **fyp-pillow:1**, **1024 MB, 600 s timeout** (read off the live function 2026-08-21; the older "fyp-pillow:2 / 512 MB / 1 min" line below was stale).
  **v6.4 (2026-08-21): PASSTHROUGH CAMERAS.** A camera row with
  `detect_enabled: false` gets its DynamoDB record written and the function
  returns — no Rekognition, no tiling, no Bedrock, no SES, no EXIF download.
  Record carries `target_detected: false`, `model_type: "none"`,
  `source: "navigation-capture"`, so passthrough frames filter apart from real
  detections in the gallery. The flag **defaults to true**, so `worm_cam`,
  `moth_cam` and `manual_upload` (none of which have the field) are untouched.
  Built for the Go2 navigation handover demo (`robot/go2_console.py` →
  camera `demo_cam`), which needs photos on the dashboard and no detection.
  **Why it is a flag inside this Lambda and not a second Lambda:** the frames
  bucket allows ONE notification filter per event type and it is already
  `frames/`; S3 rejects a second overlapping prefix rule, so the routing has to
  happen in-function. Deploy artifact: `lambda/_build/`.
  v4.2: no longer draws boxes or
  writes a processed bucket; does cloud-side tiling (1920x1080 → grid + overlap → per-tile ~4x
  upscale → per-tile detect → global-coord convert → NMS), per-camera opt-in via
  `tiling_enabled`. `put_item` is **unconditional** — clean images (zero detections) also write
  a DDB record. (Sizing was 512 MB / 1 min at v4.2; live values are on the bullet head.)
  **v4.3 (deployed 2026-07-13): application-layer non-vegetation FP suppression.**
  For custom models only, after detection, runs `DetectLabels` on the frame and drops any
  worm box ≥50% covered by a hard-object region (person/vehicle/furniture/machinery; full
  list in `SUPPRESS_LABELS`). Never suppresses on plant labels (`SUPPRESS_PROTECT`). Only
  fires when the model produced a box (no extra call on clean frames); any error is non-fatal
  (detection proceeds unfiltered). Env-tunable: `SUPPRESS_NONVEG` (default true),
  `SUPPRESS_MIN_CONF` (55), `SUPPRESS_COVERAGE` (0.5), `SUPPRESS_LABELS`, `SUPPRESS_PROTECT`.
  Added because **Rekognition CL OD cannot be trained on negatives** (see `docs/detection.md`).
  Required an IAM add: `rekognition:DetectLabels` on `pest-detection-processor-policy` (the
  role previously had only `DetectCustomLabels`). Verified pre-deploy: on cag_armyworm_103 the
  65% jeep-tire FP (94% covered by a `wheel` region) drops while the real worms in 104/105 are
  kept (`datasets/verify_suppression.py`).
  **v4.4 (2026-07-21, SUPERSEDED by v4.5 below — history only): LLM crop
  verification via Bedrock, annotate-only.** Shipped with `LLM_VERIFY_DROP` always
  false (verdict tagged, never removed a box) while an A/B experiment ran. That env
  var no longer exists in the code — kept here only so old logs/docs referencing it
  aren't a mystery.
  **v4.5 (DEPLOYED 2026-07-22): hybrid Rekognition/LLM confidence gate, replacing
  annotate-only.** Runzhe's call after reviewing the v4.4 A/B data: a target-label box
  AT/ABOVE the camera's `min_confidence` is trusted outright (Rekognition has final
  say, the LLM is not even called — no cost/latency spent on a box whose fate can't
  change). A box BELOW `min_confidence` is sent to the model; an explicit "not a
  larva" verdict DROPS it, anything else (positive verdict, or any failure —
  fail-open) keeps it. **`min_confidence` changed meaning**: it used to be "the floor
  to count as a detection at all"; it is now "the point above which Rekognition's
  word is final." **PER-CAMERA OPT-IN via a new DynamoDB field
  `llm_verify_enabled`** (boolean, mirrors `tiling_enabled`) — this Lambda is shared
  by every custom-model camera, and `moth_cam`'s target is adult moths while the
  verify prompt asks about larvae, so the gate must never run for a camera that
  hasn't explicitly opted in. Only `worm_cam` has it set true; `moth_cam` and
  `manual_upload` are completely untouched by this code. `llm_verify_enabled` is now
  in `pest-monitoring-api`'s `CAMERA_ALLOWED` (added 2026-07-22) so it CAN be toggled
  via `POST /settings`, but no dashboard UI control exists for it yet — DynamoDB/CLI
  is still the only practical way to flip it today.
  **Two real bugs found by an adversarial code review before this reached
  production (2026-07-22), both fixed and regression-tested:** (1) the Lambda used
  to decide "did verification run" from "did the call not raise an exception" —
  but every internal fail-open path (corrupt frame, all crops fail, Bedrock throttles
  the whole batch) returns normally having verified nothing, which would have
  silently let an unexamined sub-threshold box collapse the detection floor to 0 and
  count as a detection nobody ever checked. Fixed: `apply_llm_hybrid_gate` now returns
  `(labels, n_verified)` and the floor collapse only happens when `n_verified > 0`.
  (2) The DDB `labels` field (legacy, used by the dashboard's `getVerifiableBoxes`)
  used to be a verbatim dump of whatever Rekognition returned (down to its own
  ~30-50% internal gather floor) whenever the hybrid gate didn't run at all —
  `moth_cam`, `manual_upload`, or any `worm_cam` frame with no sub-threshold
  candidate. The dashboard now trusts every target-label entry in that field as a
  confirmed detection (that's the whole point of moving the gate server-side), so an
  unfiltered low-confidence entry read as a phantom detection. Fixed: `labels` now
  filters target-label entries by the same `detection_floor` `bboxes_for_db` uses.
  Env vars: `LLM_VERIFY` (global kill-switch, default true), `LLM_VERIFY_MODEL_ID`
  (`us.anthropic.claude-haiku-4-5-20251001-v1:0` — Bedrock cross-region inference
  profile id, NOT the bare foundation-model id), `LLM_VERIFY_MAX_BOXES` (5, caps
  sub-threshold candidates judged per frame), `LLM_VERIFY_PAD` (0.6), `LLM_VERIFY_
  MIN_CONTEXT_PX` (32, padding floor), `LLM_VERIFY_MAX_UPSCALE` (8.0), `LLM_VERIFY_
  LONG_EDGE` (672), `LLM_VERIFY_WORKERS` (4), `LLM_VERIFY_TIMEOUT` (12).
  Dashboard UI simplified same-turn (Runzhe: percentages and an LLM ✓/✗ badge read as
  cluttered "AI-flavored" technical text) — no confidence percentage or LLM
  verdict/reason text anywhere in the gallery card, image overlay, review list, or
  notification panel; a stale raw "Top labels" percent dump in the image modal was
  removed too. The Settings → Test upload panel is the one deliberate exception —
  it's a calibration tool, showing the actual confidence returned is the point of it.
  Requires an IAM add: `bedrock:InvokeModel` + `bedrock:Converse` on
  `pest-detection-processor-policy` (`lambda/bedrock-policy.json`) AND Bedrock model
  access enabled for that model id in us-east-1 (Runzhe submitted the use-case form
  2026-07-22; Claude Haiku 4.5 confirmed AUTHORIZED via
  `get-foundation-model-availability`). A/B proof script: `datasets/verify_llm_crop.py`
  **v4.6 (WRITTEN 2026-07-22, NOT DEPLOYED): whole-frame LLM scan on top of the
  v4.5 gate.** Runzhe's ruling: the LLM must also SEE the whole frame, not only
  re-judge Rekognition's boxes — core intent is RECOVERY of frames Rekognition
  misses entirely (light-worm FN class). One extra Haiku pass per opted-in frame
  (downscaled 1568px, 4x4 grid A1–D4, strict-JSON positive-cell list). Positive
  cells carry authority: an un-boxed positive cell is cropped, re-judged, and
  only a POSITIVE crop verdict makes it a synthetic detection (Confidence 0.0,
  `llm_scan_only`, DB `source=llm_scan`; SES says "LLM whole-frame scan" not
  "0.0%"); a sub-threshold box inside a positive cell is kept with no crop call.
  Scan SILENCE deletes nothing (2026-07-21 A/B: whole-image missed 8/14) — a
  sub-threshold box outside every cell still goes to the crop verdict, the only
  executioner. Gate now runs on zero-box frames too (recovery requires it). New
  env vars: `LLM_SCAN` (kill switch — false reverts to exact v4.5 behaviour),
  `LLM_SCAN_COLS`/`ROWS` (4/4), `LLM_SCAN_LONG_EDGE` (1568), `LLM_SCAN_MAX_TOKENS`
  (300), `LLM_SCAN_CELL_PAD` (0.15), `LLM_SCAN_MAX_RECOVER` (3, caps synthetic
  adds per frame). New DB field `llm_scan` {ran, cells, recovered}. Pre-deploy
  proof: `datasets/verify_llm_scan.py` (imports the real lambda module).
  (whole-image vs crop, same model, pre-registered KEEP/KILL thresholds — see
  `docs/detection.md`).
  **DEPLOYED CONFIG AS READ FROM AWS 2026-07-29 02:22 UTC** (read with
  `aws lambda get-function-configuration --function-name pest-detection-processor
  --profile prod --region us-east-1`). Several numbers above are historical — these
  are what is actually live:
  - `Timeout` is **600 s**, `MemorySize` **1024 MB** (re-read 2026-08-07; the
    earlier 180 s / 512 MB note is stale).
  - `LLM_VERIFY_MODEL_ID` = **`us.anthropic.claude-sonnet-4-6`**, not Haiku 4.5.
  - `LLM_VERIFY_ALL_BOXES` = **true** — the gate is now a *denoiser*: every box is
    judged, not only the sub-`min_confidence` ones described under v4.5.
  - `LLM_VERIFY_MAX_BOXES` = **120** (was documented as 5).
  - `TILE_MIN_CONFIDENCE` = **8** — tiles gather boxes down to 8 %, which is what
    feeds the denoiser its volume.
  - `LLM_SCAN` = **false**, so the v4.6 whole-frame scan is deployed but switched
    off; records carry `llm_scan {ran: false}`.
  - `LLM_MERGE` = false.
  Measured end-to-end on a live A8 frame 2026-07-29: v9 emitted **61** boxes at
  8-14 % on a clean frame, the gate judged all 61 and dropped all 61, Lambda
  duration **35.4 s** of the 180 s budget.
- `pest-monitoring-api` — ~1060 lines, 20 routes. API GW HTTP API id `vzfl7s6z00`, 21 routes.
  `CAMERA_ALLOWED` writable fields gained `tiling_enabled` (2026-07-13) so the
  dashboard "Zoom scan" toggle can persist per-camera tiling on/off.
  Cameras table now holds 4 rows: **`worm_cam`** ("Worm Cam") + **`moth_cam`**
  ("Moth Cam") + `manual_upload` (hidden fallback) + **`demo_cam`** ("Go2
  navigation demo", added 2026-08-21, `detect_enabled: false` — see the v6.4
  passthrough note above). Test/legacy
  `notile_test`/`batch2_notile`/`person_cam` deleted 2026-07-13.
  **camera_id TRUE MIGRATION executed 2026-07-14** (`armyworm_go2_a8mini`→`worm_cam`,
  `moth_cam_01`→`moth_cam`): camera rows re-keyed, 119 detection records' camera_id
  rewritten (wilbur-fyp-project legacy rows kept for provenance — dashboard maps
  their display name), Orin scripts sed-switched + kvs-controller restarted and
  verified polling `worm_cam` OK. Historical S3 keys under
  `frames/armyworm_go2_a8mini/...` untouched (records point at them; new uploads go
  to `frames/worm_cam/...`). Script: `datasets/migrate_camera_ids.py`.
  Since 2026-07-06 all routes require a Cognito JWT (authorizer `cognito-dashboard`,
  id `enxa26`) EXCEPT `GET /stream/status`, which the Orin/mini-PC kvs_controller
  polls unauthenticated. CORS AllowHeaders = content-type, authorization
  (`lambda/cors.json`).
  `DELETE /detection` (added 2026-07-07, route `glwyqo0`, JWT-gated): permanent
  delete of ALL rows for an image_id + the S3 objects they reference. Safety:
  404 without an existing record; S3 frame deletes gated to the `frames/` prefix
  in code AND in IAM (`s3:DeleteObject` scoped to `frames/*` in
  `pest-monitoring-api-policy`) — training assets under `assets/`/`datasets/`
  are unreachable both ways. S3 failures abort with 500 before rows are removed
  (retryable).
- `pest-camera-scheduler` — ~167 lines, EventBridge cron.

Lambda runtime and name are fixed at creation. Changing them means delete + recreate, which
wipes the resource policy (re-grant API GW invoke).

## S3
`argus-frames-506868652945`, `argus-processed-506868652945`.
`argus-dashboard-506868652945` — static website hosting for `web/dashboard_v4/`
(public read, added 2026-07-06; see `docs/dashboard.md` for URL + redeploy command).

## Cognito + CloudFront (dashboard auth/HTTPS, added 2026-07-06)
- User Pool `us-east-1_9selFDHpc` (`pest-dashboard-users`), app client `6vebotf45bp8u46cnraddiaplv`, email sign-in,
  admin-create-only. App client `4husu6afr835e235eu9dqp8av6` (`dashboard-web`,
  no secret, USER_PASSWORD_AUTH + refresh, 12 h tokens).
- CloudFront `E1YADURLSAVNFA` → https://d1dtoxef7qmugl.cloudfront.net
  (CachingDisabled policy; origin = the S3 website endpoint).
- User management + emergency authorizer-detach commands: `docs/dashboard.md`.

## DynamoDB
`pest-monitoring-cameras`, `pest-monitoring-detections`, `pest-monitoring-system-config`,
`pest-monitoring-schedule-logs`. Detection record PK `image_id` = the exact S3 object key.
The record stores `bboxes` (structured) + `verifications` map.

## Rekognition Custom Labels

### LIVE on production (`506868652945`)
- **armyworm — camera `worm_cam`, F1 0.613**:
  `arn:aws:rekognition:us-east-1:506868652945:project/argus-detection/version/v9r-prod-20260810/1786377372187`
  Retrained on the production account 2026-08-10 from the same recipe as the dev
  v9 retrain (Custom Labels versions are account-bound, so it could not be copied).
  Low F1 is by design: the detector is a high-recall front end and the LLM gate does
  the rejecting. Do not read 0.613 as pipeline accuracy.
- **moth — camera `moth_cam`, F1 0.991**: project `argus-moth-detection`, version
  `moth-prod-20260811`.
- Both endpoints are STOPPED between runs. EventBridge starts the detector before the
  patrol window; `pest-model-watchdog` stops it after the camera's `max_runtime_min`
  (worm_cam: 45). Two endpoints running at once bill twice.

### History — retired dev account (`366356442579`), for the record only
- armyworm v9 retrain (was live on `worm_cam` 2026-08-07 to the migration, F1 0.599):
  `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v9/version/v9-20260805-0713/1785913987295`
  Trained 2026-08-05 with 5 CAG images added to TRAIN (batch_2 101/110/111,
  batch_1 004 + bud_001 — outside the scored holdout; they are no
  longer holdout).
- armyworm v9 first cut (superseded by the retrain, endpoint STOPPED, F1 0.591):
  `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v9/version/v9-20260725-1746/1785001598671`
- armyworm v5 model ARN (was live until the v9 switch, F1 0.852, endpoint STOPPED):
  `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection/version/v5-2026-07-07/1783394123547`
- Other trained versions, all STOPPED as of 2026-07-29 (F1 in brackets): v8-20260723-1703
  [0.739], v7-5 [0.737], v7-4 [0.720], v7-3 [0.822], v7-2 [0.794], v7-1 [0.744].
- moth `SmartPestProject.2026-02-15` [0.988] — the inherited dev-account model,
  superseded by `moth-prod-20260811` above.
- armyworm v6 model ARN (**REJECTED 2026-07-15** — F1 0.839, tied v5 on the Batch 2
  holdout and was worse on batch_1; trialled live then rolled back. Kept for the
  record, endpoint stopped. See `docs/detection.md` v6 VERDICT):
  `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection/version/v6-2026-07-14/1784011732429`
- armyworm v4 model ARN (older rollback, F1 0.719):
  `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection/version/armyworm-detection.2026-05-21T12.46.19/1779338780450`
- Rollback = write the wanted ARN into `worm_cam`'s `custom_model_arn` (below).
  Beware: the ARN contains colons — a raw `aws dynamodb update-item` from
  PowerShell mangles the quoting; use boto3 (see `datasets/migrate_camera_ids.py`
  style) or a JSON file.
- The model ARN lives in ONE runtime place: `pest-monitoring-cameras` item's
  `custom_model_arn`. All three Lambdas resolve it from there at call time;
  nothing hardcodes an ARN.
- moth model: retrained on production as `argus-moth-detection` version `moth-prod-20260811` (F1 0.991). History: inherited from Wilbur's SmartPestProject (F1 0.988, label "Moths") on the retired dev account.
- Models are **account-bound** — cannot migrate the ARN; must retrain if the account changes.
  Model must be RUNNING to detect; STOP it after testing (billed per running hour).

## Bedrock (LLM crop verification, added 2026-07-21)
- Model: **`us.anthropic.claude-haiku-4-5-20251001-v1:0`** (Claude Haiku 4.5).
  **The `us.` prefix is REQUIRED and is the #1 gotcha here.** It is a cross-region
  INFERENCE PROFILE id, not a foundation-model id. Calling the bare
  `anthropic.claude-haiku-4-5-20251001-v1:0` fails with `ValidationException:
  Invocation of model ID ... with on-demand throughput isn't supported. Retry your
  request with the ID or ARN of an inference profile that contains this model.`
  (hit and fixed 2026-07-21). List valid ids with
  `aws bedrock list-inference-profiles --profile prod --region us-east-1`.
  Alternative kept in the IAM policy: `us.amazon.nova-lite-v1:0` (cheaper, needs
  harder prompt tuning). **Do NOT use `anthropic.claude-3-haiku-20240307-v1:0`** — Bedrock
  lifecycle Legacy, EOL 2026-09-10. Claude 3.5 Haiku has no image input on Bedrock.
- Called with the **Converse API** (`bedrock-runtime.converse`), raw JPEG bytes in
  `ImageSource.bytes` — do NOT pre-base64 the image, boto3 handles it. Limits: 20 images
  per message, each <= 3.75 MB and <= 8000x8000 px. The model often wraps its JSON reply
  in a ```json fence, so the parser extracts `{...}` with a regex rather than calling
  `json.loads` on the whole reply.
- Cost at FYP volume (0-5 crops per triggered frame, a few hundred frames/week) is well
  under $1/month — cost is not the deciding factor, latency and prompt reliability are.
- IAM: `lambda/bedrock-policy.json`, applied as a **separate inline policy named
  `bedrock-verify`** on `pest-detection-processor-role` (the existing
  `pest-detection-processor-policy` inline policy is untouched). It needs TWO statements:
  one on the **inference-profile ARN** (`arn:aws:bedrock:us-east-1:506868652945:
  inference-profile/us.anthropic...`) and one on the **underlying foundation-model ARNs**
  (`arn:aws:bedrock:*::foundation-model/anthropic...`) — cross-region inference routes to
  several regions, so the foundation-model ARN is region-wildcarded. Applied 2026-07-21:
  `aws iam put-role-policy --role-name pest-detection-processor-role --policy-name
  bedrock-verify --policy-document file://lambda/bedrock-policy.json --profile prod`.
- Model access was already enabled on this account (verified 2026-07-21 by a live
  `converse` probe). If a fresh account returns `AccessDeniedException`, enable it in the
  Bedrock console -> Model access -> Modify model access -> tick the model -> Save.
- Historical note: `docs/state.md` records Bedrock being **rejected** for the deployer's
  mutation path (safety/trust/cost). That ruling is about an agent WRITING changes; this is
  read-only inference in the detection path and does not contradict it.

## KVS
`armyworm-cam-stream` is live. Moth `moth-cam-stream` is parked (Hikvision cam repurposed).
Producer SDK (ARM64) compiled on the Orin; GStreamer passthrough pipeline
`rtspsrc → rtph264depay → h264parse → kvssink`.

## Gate (patrol)
Patrol script: capture + upload → get the S3 key → poll `get_item(image_id=key)` every ~2s →
record appears → point done (worm or not — clean images write a record too, so no dead-lock) →
next waypoint. Add a timeout safety net. Does NOT need the processed bucket.

## If Dr. Li tests on NP's AWS account
Rekognition models are account-bound (must retrain). Hardcoded account IDs, bucket names, and
ARNs throughout the codebase need updating.
