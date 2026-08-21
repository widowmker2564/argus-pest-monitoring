# Code walkthrough prep — Dr. Yan Li, Friday 21 Aug 2026

Everything below was read out of the live code this week; every `file:line`
was verified to exist, and every Q&A answer survived an adversarial check
against the source. Present subsystems in the order listed.

**Open with one sentence:** "One Lambda is the whole pipeline — I suggest we
start there, and I can jump anywhere you want afterwards."

---

## Live values — read off AWS on 2026-08-20. Trust this page over any file.

| Setting | Live value | Note |
|---|---|---|
| POST_VERIFY_FLOOR | **33** | display floor. NOT 49 - see below |
| TILE_MIN_CONFIDENCE | **8** | how low a tile candidate can be and still be judged |
| min_confidence (worm_cam row) | **10** | floor handed to Rekognition itself |
| LLM_VERIFY_MODEL_ID | **us.anthropic.claude-sonnet-4-6** | the us. prefix is required |
| LLM_VERIFY_ALL_BOXES | **true** | denoiser mode: every box is judged |
| LLM_VERIFY_MAX_BOXES | **120** | boxes past this are dropped unjudged |
| LLM_VERIFY_MAX_TOKENS | **300** |  |
| LLM_VERIFY_WORKERS | **3** | parallel Bedrock calls |
| LLM_VERIFY_PAD | **0.6** | crop padding around each box |
| POST_NMS_IOU / POST_NMS_CONTAIN | **0.1 / 0.1** | overlap and containment cleanup |
| POST_MAX_BOX_AREA | **0.05** | area cap, 5% of the frame |
| max_runtime_min (worm_cam row) | **45** | watchdog stops the endpoint |
| Lambda size | **1024 MB / 600 s** | docstring still says 512/180 - stale |
| Live model | **v9r-prod-20260810** | project argus-detection, acct 506868652945 |

**The floor question is the trap.** The report threshold study settled on 49.
That was the dev account. After the model was retrained on production, 49 was
throwing away real worms, so it was re-tuned to 33 on 2026-08-13. Live Lambda
and the worm_cam row both read 33. If she asks why the report says 49, that is
the answer - the study is correct for the build it was run on.

**Fixed on 2026-08-20, say so if it comes up:** the deployer was still
shipping `POST_VERIFY_FLOOR: "49"`, so re-running it would have quietly put
the discarded floor back and lost detections. Now 33, with the reason in a
comment beside it (`deployer/deploy.py:654`).

**Not a conflict, do not concede it:** three runtime numbers exist because
they do three jobs. Per-camera `max_runtime_min` (45) wins; the watchdog env
`MAX_RUNTIME_MIN` (75) is the fallback for cameras that set nothing; 60 is the
code default used only if the env var is absent, and it is not
(`lambda/pest-model-watchdog.py:127`).

**Known open, own it before she finds it:** LLM_VERIFY_MAX_TOKENS was raised
to 300 but LLM_VERIFY_TIMEOUT is still the 12 s code default, against the
processor's own comment saying to raise them together.

---

## 1. processor (lambda/pest-detection-processor.py, v6.3)

*The Lambda that turns one uploaded frame into one detection record: Rekognition finds candidate boxes (tiled for recall), an LLM on Bedrock verifies each crop (precision), a cleanup pass dedupes and floors what survives, and the result is written to DynamoDB and optionally emailed.*

**1) lambda_handler entry + parse_s3_key**  `lambda/pest-detection-processor.py:1324-1336`

> Entry is an S3 PutObject event on the frames bucket. I URL-decode the key, skip non-images, and parse it as frames/{camera_id}/{waypoint_id}/{filename}. parse_s3_key at line 371 strips anything after a double underscore from the waypoint segment, so override suffixes in the key never create phantom waypoints. Any key that does not match falls back to the manual_upload camera instead of failing - the function degrades, it never dies on a weird key.
>
> *Why:* The dashboard Test upload and the robot both write into the same bucket; one parser handles both.

**2) get_camera_config (line 395) + system config**  `lambda/pest-detection-processor.py:1338-1348`

> Config is per-camera in DynamoDB, with a three-step fallback: the requested camera row, then the manual_upload row, then hardcoded defaults at line 417 if even that read fails. So a DynamoDB hiccup degrades detection rather than killing it. The live worm_cam row has min_confidence 10, tiling_enabled true, llm_verify_enabled true, and post_verify_floor 33.
>
> *Why:* One shared Lambda serves worm_cam, moth_cam, and manual uploads; everything camera-specific lives in the table, not the code.

**3) Per-request overrides: __confN and __llm-alias**  `lambda/pest-detection-processor.py:1350-1364`

> Two stateless overrides ride in the S3 key itself. __conf<N>, N between 10 and 100, overrides min_confidence for this run only - that is the Test upload confidence slider. __llm-<alias> picks the verification model for this run; set_active_llm_model at line 216 resolves key alias, then camera row, then env default, and is called on every record because Lambda reuses warm containers - a stale value would attribute one model's results to another. Only aliases in a whitelist at line 201 are honoured; a raw model id can never be injected through the key.
>
> *Why:* Stateless overrides let two A/B arms run side by side on the same images with zero config swaps and no DDB writes fighting each other. The alias whitelist exists because a bad model id does not fail loudly - it fails as AccessDenied on every crop (line 227-231, the 492-call incident).

**4) EXIF orientation normalisation (step 2b)**  `lambda/pest-detection-processor.py:1369-1400`

> Phone photos store landscape pixels plus an EXIF rotate tag. Browsers apply the tag; PIL and my coordinate maths do not - so on two iPhone uploads, 4032x3024 with Orientation 6, every box drew in the wrong place. Fix: exif_transpose once at ingest, and if the size changed, write the upright JPEG back to the same S3 key at quality 95. After that Rekognition, the crops, and the dashboard all agree on one pixel grid. Untagged images are untouched, and the whole block is non-fatal.
>
> *Why:* Fix the pixels once at the source instead of teaching four consumers about EXIF.

**5) Detection branch: run_tiled_detection (line 741)**  `lambda/pest-detection-processor.py:1402-1431`

> For worm_cam the frame is sliced into a 4x4 grid with 15 percent overlap per side - compute_tile_regions at line 550 - each tile Lanczos-upscaled to a 1920 long edge, plus one full-frame pass: 17 detect_custom_labels calls in a 4-worker thread pool. Overlap guarantees a worm straddling a tile boundary lands whole in at least one tile; the duplicates that overlap creates are removed by greedy per-class NMS at IoU 0.5, line 720. The per-tile gather floor is env TILE_MIN_CONFIDENCE - code default 30, live 8, because the LLM gate downstream wants volume. Output is shaped exactly like a Rekognition CustomLabels response, so nothing downstream knows tiling happened. Any tiling error falls back to a single whole-frame call at line 1420.
>
> *Why:* Small targets: a worm can be under 1 percent of a wide frame. Upscaled tiles give Rekognition roughly the 4x zoom we validated in week 6.

**6) detect_whole_frame + _full_frame_bytes (oversized-frame guard)**  `lambda/pest-detection-processor.py:609-635`

> Rekognition rejects an oversized image with ImageTooLargeException. Our Jewel captures are 5712x3213, so the full-frame pass failed on every run of exactly the two demo images - silently. Now on that exception I download the frame and shrink it: cap the long edge at 4000 pixels, then halve until the JPEG is under 4 MB, line 583. Box coordinates are normalised zero-to-one, so shrinking moves nothing.
>
> *Why:* Before this fix the exception could reach the outer handler and the frame vanished from the dashboard with no record at all.

**7) suppress_nonveg (v4.3, functions at 789 and 835)**  `lambda/pest-detection-processor.py:1433-1448`

> Rekognition Custom Labels object detection cannot be trained on negatives - we proved that on 2026-07-13. So false positives on hard objects are filtered at the application layer: one DetectLabels call at MinConfidence 55 finds people, vehicles, furniture, machinery, and any worm box that is at least 50 percent covered by such a region is dropped. A protect list at line 182 means plant labels never suppress - worms live on plants. It only runs when a custom box actually exists, so clean frames never pay for the extra call, and any error keeps all labels.
>
> *Why:* The one filter the model itself cannot learn.

**8) apply_llm_verify_gate call site + denoiser rule (function at 1090)**  `lambda/pest-detection-processor.py:1450-1475`

> The LLM gate is per-camera opt-in via llm_verify_enabled - not ceremony: this Lambda is shared, and moth_cam detects adult moths while my prompt asks about larvae, so a global gate would silently reject every moth. Live mode is LLM_VERIFY_ALL_BOXES=true, the v4.7 denoiser: every target box is crop-judged and only a positive verdict lets it survive - line 1162 drops rejected boxes AND un-judged boxes, fail-closed. The evidence for flipping the old authority rule: with tiling on, recall is near-total but leaf and shadow noise comes back at high confidence too, so trusting high confidence lets noise through. Note line 1473: hybrid_gate_ran is n_verified greater than zero, never just 'the call did not raise' - every internal failure path returns normally having verified nothing, a real bug we caught in review on 2026-07-22.
>
> *Why:* Rekognition finds, the LLM verifies. Each model does the one task it is measurably good at.

**9) TOTAL GATE FAILURE exception (the one fail-open in denoiser mode)**  `lambda/pest-detection-processor.py:1146-1161`

> One deliberate exception to fail-closed: candidates existed but zero verdicts came back. That is infrastructure down - no Bedrock permission, wrong model id, region-wide throttle - not evidence about the frame. Fail-closed there would silently delete every box on every frame and report a clean garden. That exact thing happened on 2026-07-26: the model id was switched to Sonnet 4.6 before the Lambda role had permission for it - 492 AccessDenied calls, 44 out of 44 images reporting zero detections. So on total failure nothing is dropped, the log shouts, and the system falls back to plain min_confidence thresholding. A monitoring system going blind must be loud.
>
> *Why:* Distinguish 'the model said no' from 'the model was unreachable' - they must have opposite outcomes.

**10) verify_one_crop + crop_box_bytes (894) + LLM_VERIFY_PROMPT (315)**  `lambda/pest-detection-processor.py:998-1044`

> Each candidate is cropped with 60 percent padding, floored at 32 pixels of context so a tiny box still shows surroundings, upscaled toward a 672 long edge but capped at 8x - we found a 6x4-pixel box being blown up 60x into pure interpolation noise. One Bedrock Converse call per crop: live model is Sonnet 4.6 through the us. cross-region inference profile, 12-second timeout, 300 max tokens live, up to 120 boxes per frame in a 3-worker pool. The prompt at line 315 is deliberately neutral - it opens 'What is in this photo?' and says a larva is uncommon. We measured why: the old primed opening that said a detector had flagged a possible larva killed only 2 of 28 false boxes, the neutral wording killed 5, and 5 false verdicts literally cited my hand-drawn ink circles as their evidence. Verdict parsing at line 931 is a balanced-brace scan, and anything unparseable returns None, never a guess.
>
> *Why:* The crop-vs-whole-frame A/B on 2026-07-21: crops caught 13 of 14 larvae, the whole image only 6 of 14. Never ask a multimodal model to find a sub-1-percent target in a wide frame.

**11) apply_post_gate_cleanup (function at 1218)**  `lambda/pest-detection-processor.py:1477-1487`

> The gate survivors get four cheap rules, all env or DDB configured, all default off in code. Area cap, live 5 percent of frame: the largest of my 34 hand-labelled worms covers 4.39 percent, so 10 percent-plus boxes cannot be real - this caught a 43-percent-of-frame box the verifier had confidently called a caterpillar. Then the display floor, live 33, per-camera from DDB so the dashboard threshold knob edits this and not min_confidence. Then NMS at IoU 0.1 plus a containment test at 0.1 - IoU divides by the union, so a small box wholly inside a big one scored 0.022 and survived; containment asks how much of the smaller box is covered and calls that 1.0. Only runs when the gate verified something, line 1481, otherwise the floor would just be a weaker copy of min_confidence.
>
> *Why:* The floor is applied AFTER the LLM verdict on purpose: among LLM-accepted boxes, ones on a real worm have median 27 percent Rekognition confidence, ones on nothing have median 15.5 - that separation does not exist as an input floor, which is what every earlier floor sweep showed.

**12) detection_floor decision + bbox extraction**  `lambda/pest-detection-processor.py:1489-1515`

> Line 1497: if the gate actually verified something, detection_floor collapses to zero - a sub-threshold box that survived an explicit LLM check is a legitimate detection, and re-applying min_confidence would delete it again. If nothing was verified, plain min_confidence applies. The same floor then feeds extract_bounding_boxes at line 447, and boxes_to_db_format at line 495 stringifies the normalized coordinates - DynamoDB's Number type rejects the high-precision floats Rekognition emits - and attaches verified_by_llm and llm_reason per box for the dashboard.
>
> *Why:* One floor value used consistently for the flag, the boxes, and the legacy labels field, so the record can never contradict itself.

**13) DynamoDB write + SES alert**  `lambda/pest-detection-processor.py:1517-1612`

> put_item at line 1577 is unconditional - clean frames also write a record. The Go2 patrol completion gate polls this table by S3 key, so a clean waypoint must still produce a row or the patrol would deadlock. Line 1534 filters the legacy labels field by the same detection_floor as the boxes, because the dashboard trusts every target-label entry there as confirmed - unfiltered, a raw 30-percent Rekognition label would show as a detection on a frame the record itself says is clean. Every row also records llm_verify_model, so A/B runs stay readable months later without CloudWatch. SES fires only when target_detected, email_enabled, and recipients all hold; a send failure is logged and non-fatal, since an unverified sandbox recipient must not kill the detection record.
>
> *Why:* The record is the product; email is best-effort on top.

**Looks wrong, is deliberate:**
- Line 1114 returns 4 values (labels, 0, 0, None) while the function's normal return at 1197 and the caller's unpack at 1471 use 2. Defence, honestly: it is a leftover arity from the v6.3 dead-code strip. If PIL cannot open the frame, the unpack raises ValueError, the try/except at 1474 catches it, and all labels are kept - so the intended fail-open behaviour still holds, just via the outer catch. Say: 'known cleanup item, behaviour is correct, I will fix the arity.'
- worm_cam min_confidence is 10, which looks absurdly permissive. It is the CANDIDATE floor in front of the LLM gate, not the display floor. In denoiser mode every candidate gets judged, so a low floor buys recall; the number the user sees is post_verify_floor 33. Raising min_confidence is the exact trap found 2026-08-05 - it strangles the candidate stream before the LLM can rescue real worms.
- Line 1497 sets detection_floor to 0 when the gate ran - it looks like all thresholding is removed. Deliberate: a box that survived an explicit LLM verdict no longer needs a confidence check, and re-applying min_confidence would delete it. The guard is that hybrid_gate_ran means n_verified > 0 (line 1473), never 'the call did not raise' - the 2026-07-22 review bug where all-fail-open runs collapsed the floor with nothing verified.
- The gate is fail-OPEN per box in default hybrid mode but fail-CLOSED per box in live denoiser mode (line 1162 drops even un-judged boxes). Not a contradiction: in hybrid mode the LLM is a safety net under Rekognition's authority, so absence of a verdict must not delete; in denoiser mode every box was MEANT to be judged, so an un-judged box passing through would be exactly the high-confidence tiling noise the mode exists to remove - three HIGH findings in the 2026-07-23 adversarial review forced this.
- The total-gate-failure branch (line 1146) keeps everything when zero verdicts come back - it looks like it defeats the denoiser. Defence: candidates-but-zero-verdicts is infrastructure down, not evidence about the frame. The 2026-07-26 incident is the proof: Sonnet 4.6 set before IAM allowed it, 492 AccessDenied calls, 44/44 images falsely clean. Fall back to plain thresholding and shout in the log.
- The EXIF block writes back to the same S3 key (line 1390), which re-fires the S3 trigger - looks like an infinite loop. It terminates: the second invocation sees an already-upright image, exif_transpose returns the same size, no put_object happens, no third trigger. Cost is one extra invocation per rotated phone upload, accepted for having every consumer agree on one pixel grid.
- detect_custom_labels is called with MinConfidence hardcoded to 30 on the non-tiled path (lines 622, 634) and TILE_MIN_CONFIDENCE live 8 on tiles - both far below any real threshold. Deliberate: gather low, filter later, so the record keeps full label data and the LLM gate gets its candidate volume. The code-default 30 vs live 8 difference is env config, not a code change.
- LLM_VERIFY_PROMPT_PRIMED_RETIRED at line 313 is a string tombstone, not dead code by accident. The retired prompt was a measured failure (primed wording killed 2/28 false boxes vs 5/28 neutral) and the comment above line 315 exists so nobody reintroduces a preamble asserting a larva may be present.
- Bbox coordinates are stored as strings in DynamoDB (line 495-527) - looks lazy. DDB's Number type rejects the high-precision floats Rekognition emits, and the Wilbur-era migration data already used strings; the dashboard multiplies them out client-side.
- LLM_VERIFY_MAX_TOKENS default 100 with the long comment at line 271: on thinking-always-on models the whole budget goes to reasoning, the JSON never arrives, and every verdict silently fails open. Live is 300 for Sonnet 4.6, and verify_one_crop logs the specific stopReason=max_tokens symptom at line 1035. Temperature is only sent when explicitly configured because sampling params are a 400 on newer Claude models (line 278).
- get_bedrock_client (line 866) is lazy AND lock-guarded - looks over-engineered. Crop verdicts run in a thread pool; unlocked concurrent client construction on boto3's default session can raise inside a worker, and since verify_one_crop catches everything, that race would silently score boxes as unverified. The adaptive retry mode (line 888) is client-side rate limiting because a big sweep can fire over a hundred Bedrock calls per frame.
- If quoting the display floor: it is 33 live (refit 2026-08-13 for the retrained production model), history 34 to 49 on 2026-08-10, 49 to 33 after the prod retrain. The frozen deploy baseline still says 49 - do not mix the two eras.

**If she opens these, say "archived / not the live path":**
- The LLM_VERIFY_ALL_BOXES=false branch (lines 1126-1127 and 1191-1193) is the v4.5 hybrid-authority mode - still the CODE default but not what production runs. Live is denoiser mode (env true since 2026-07-29). If asked why the default is false: a fresh deploy without env vars gets the safer fail-open behaviour.
- Env vars LLM_SCAN, LLM_MERGE, LLM_LEAD, LLM_FIRST, LLM_AGENT, LLM_PLAIN, LLM_VERIFY_COMPOSITE still sit on the retired dev account's function config - their code paths were deleted in v6.3, they are inert. The production account omits them.
- Old detection records carry an llm_scan {ran: false} map - written by the deleted v4.6 whole-frame scan's observability code. The field is historical data, not a live feature.
- deployer/audit/lambda__pest-detection-processor.json is a 2026-07-08 snapshot (Timeout 60, no LLM env vars) - stale for config; the frozen production configuration baseline supersedes it. Live sizing is 1024 MB / 600 s (one tiled frame plus up to 120 crops measures 24-54 s; the file header's 512 MB / 180 s is a minimum, not the deployment).
- docs/detection.md still contains a W15-era paragraph saying tiling is OFF in production - doc lag. Tiling was turned back on for worm_cam 2026-07-27/28 when the architecture flipped to 'Rekognition finds, the LLM verifies'; the live camera row has tiling_enabled true.
- lambda/bedrock-policy.json (Haiku 4.5 + Nova Lite only) is the older, narrower IAM source file - recreating the role from it while the env points at Sonnet 4.6 reproduces the 492-AccessDenied total-gate-failure. The live policy is the audit dump iam__pest-detection-processor-role__inline__bedrock-verify.json.
- The hardcoded Person/general/80 fallback config at line 417 and the no_box path at line 488 belong to the generic detect_labels route - manual_upload's fallback lane, not the worm pipeline. worm_cam never touches them.

---

## 2. api

*Three Lambdas that form the control plane: pest-monitoring-api serves the dashboard's 21 HTTP routes, pest-camera-scheduler starts the Rekognition model on an EventBridge cron before the patrol window, and pest-model-watchdog stops any running model after its per-camera time limit so it never burns money overnight.*

**1) File header — route table**  `lambda/pest-monitoring-api.py:22-47`

> This one Lambda serves all the dashboard routes: settings, model start/stop/status, presigned URLs, detection verify and delete, history, cost, SES identities, schedules, schedule logs, and stream flags. It runs Python 3.12, 256 MB, 30 second timeout. It's a merge of two older Lambdas — pest-control-api v3.6 and pest-history-query — so the dashboard talks to exactly one function.
>
> *Why:* One function, one deploy, one log group. The old split forced two deploys for one dashboard change.

**2) lambda_handler — the router**  `lambda/pest-monitoring-api.py:1010-1016`

> Entry point. I pull method and path from the API Gateway HTTP API v2 payload and dispatch with plain if-statements. OPTIONS returns 200 immediately for CORS preflight. Anything unmatched falls to a 404 at line 1093, and the whole router sits in one try/except so an unexpected error comes back as a JSON 500 instead of an API Gateway generic error.
>
> *Why:* Twenty-one routes doesn't justify a framework; a flat if-chain is readable and has zero dependencies.

**3) cors_response — and the auth boundary**  `lambda/pest-monitoring-api.py:117-127`

> You'll notice there is no authentication code anywhere in this file. That's deliberate: the Cognito JWT authorizer lives on API Gateway itself, in front of the Lambda. Every route requires a valid JWT except one — GET /stream/status, which the Orin and mini PC controllers poll without a token. That route is read-only and returns only stream on/off flags, it writes nothing.
>
> *Why:* Enforcing auth at the gateway means an unauthenticated request never even invokes the Lambda, and the Lambda code stays simple.

**4) GLOBAL_ALLOWED / CAMERA_ALLOWED allow-lists**  `lambda/pest-monitoring-api.py:90-108`

> These two sets are the write protection for DynamoDB. POST /settings only accepts field names that appear here — anything else is silently dropped, so a client can never inject arbitrary keys into a camera row. Three fields matter most: post_verify_floor is the display threshold the dashboard edits, max_runtime_min is the per-camera watchdog window, and llm_model_id picks the verification model per camera.
>
> *Why:* The DDB update expressions are built from client-supplied keys, so without an allow-list a malicious body could write any attribute it liked.

**5) handle_post_settings**  `lambda/pest-monitoring-api.py:243-276`

> Two write paths. If the body has camera_id plus fields, I update one camera row, filtering through CAMERA_ALLOWED and coercing min_confidence to int. Otherwise I take global fields — email settings, auto_capture, capture_interval — filter through GLOBAL_ALLOWED, and write the single system-config row. The response echoes back exactly which fields were accepted.
>
> *Why:* Echoing accepted fields lets the dashboard detect a dropped field instead of silently believing a write happened.

**6) handle_model_start / handle_model_stop**  `lambda/pest-monitoring-api.py:292-311`

> Start resolves the camera's custom_model_arn, calls Rekognition start_project_version with MinInferenceUnits equal to 1 — one inference unit, the minimum, because a running endpoint bills about a dollar an hour — and optimistically sets model_running to true in DynamoDB. Stop is the mirror image. Note the ARN guard: if the ARN is empty or still says REPLACE_, we refuse with a clear error rather than crashing inside boto3.
>
> *Why:* The model_running flag is advisory for the UI; the watchdog treats Rekognition's own status as the truth and reconciles the flag.

**7) handle_presigned_url**  `lambda/pest-monitoring-api.py:368-397`

> For uploads I use generate_presigned_post, not a presigned PUT, because POST lets me attach a condition: content-length-range 1 byte to 25 MB. So the browser can upload a test image directly to S3 but cannot dump a gigabyte file on us. Both directions expire in 3600 seconds. For downloads the bucket is picked by key shape: keys starting with frames/ come from the frames bucket, everything else is an annotated image from the processed bucket.
>
> *Why:* The size cap is enforced by S3 itself at upload time, not by any code I have to trust the client to run.

**8) handle_verify_detection — the verifications map**  `lambda/pest-monitoring-api.py:403-465`

> This is the human-in-the-loop write path. The table key is composite — image_id plus detection_time — but the dashboard only knows image_id, so I query with Limit 1 to recover the full key first. Verdicts are per bounding box: the row carries a verifications map keyed by bbox index, values TP or FP. The write is two update_item calls: the first creates an empty map with if_not_exists, the second sets verifications.#k. Sending verdict null removes that one entry. Lines 467 to 482 keep the old image-level true/false path alive for legacy callers.
>
> *Why:* DynamoDB cannot SET a nested map member if the parent map doesn't exist yet, so the two-step write is required, not redundant.

**9) handle_delete_detection — permanent delete with safety gates**  `lambda/pest-monitoring-api.py:492-556`

> Gallery delete. Three gates so this can never become an arbitrary S3 deleter: first, if no detection record exists for the image_id it's a 404 and nothing is touched. Second, frame objects are only deleted under the frames/ prefix — the same bucket holds training assets under assets/ and datasets/, and those are out of reach. Third, order: S3 objects are deleted first, DynamoDB rows last. If an S3 delete fails I return 500 with the rows intact, because the record is the only index to the object — and the whole call is safely retryable since deleting a gone key is a no-op.
>
> *Why:* The row must outlive the object it points at, never the other way round; otherwise a half-failure orphans S3 objects we can no longer find.

**10) handle_get_history**  `lambda/pest-monitoring-api.py:563-634`

> Filtered history query: camera, zone, detected true/false, pest_type, source, and a date range. Limit defaults to 100, capped at 500, and the scan loop stops after 10,000 scanned items as a runaway guard. The date filter at lines 594 to 606 is the interesting bit: migrated old records use space-separated timestamps and new ones use ISO 8601 with a T, but a plain string range comparison sorts both correctly, so one filter handles both formats without any parsing.
>
> *Why:* String comparison works because both formats start YYYY-MM-DD; that saved a data migration of every historical row.

**11) _cron_expression — SGT to UTC**  `lambda/pest-monitoring-api.py:801-825`

> Dashboard times are Singapore local but EventBridge crons are UTC. Singapore is UTC+8 with no daylight saving, so the conversion is a fixed minus 8 hours — and when that crosses midnight the day-of-week list shifts back one day, so Monday 05:40 SGT correctly becomes Sunday 21:40 UTC. The first version of this code passed SGT straight through and every schedule fired 8 hours late; this function is the fix.
>
> *Why:* Measured bug, measured fix: schedules fired 8 hours late until the -8 conversion and the midnight day-shift were added.

**12) handle_post_schedule — start-only scheduling (v6.2)**  `lambda/pest-monitoring-api.py:886-920`

> Since v6.2 a schedule is start-only. Saving creates one EventBridge rule, pest-sched-{camera}-start, targeting the scheduler Lambda with a JSON payload, plus a per-rule invoke permission whose SourceArn uses the account id extracted from the invocation context. There is no stop rule any more — the watchdog closes the run after the camera's max_runtime_min. Line 904 deletes any legacy stop rule on every save, so old start/stop pairs die off naturally.
>
> *Why:* A stop rule can be missed if the start slips; a watchdog that measures actual runtime cannot.

**13) Scheduler: lambda_handler validation**  `lambda/pest-camera-scheduler.py:102-134`

> This Lambda is only invoked by the pest-sched-* EventBridge rules; the payload carries camera_id, action, schedule_id and trigger_time. It validates the action is start or stop, loads the camera row, and refuses if the model ARN is missing or a REPLACE_ placeholder. Every outcome — including every failure — is written to the pest-monitoring-schedule-logs table, so I can audit whether the 05:40 start actually fired.
>
> *Why:* The audit table exists because a schedule that silently fails looks identical to a schedule that never fired.

**14) Scheduler: execute against Rekognition**  `lambda/pest-camera-scheduler.py:136-151`

> Start calls start_project_version with MinInferenceUnits 1, stop calls stop_project_version, and in both cases it updates the camera's model_running flag and writes a success log row. It's 128 MB and a 60 second timeout — the Rekognition call returns quickly; the model itself warms up asynchronously over the next 10 to 15 minutes.
>
> *Why:* The API call is fire-and-forget, so a small memory size and short timeout are enough; the schedule fires early precisely to absorb the warm-up before the patrol window.

**15) Scheduler: ResourceInUseException soft success**  `lambda/pest-camera-scheduler.py:152-159`

> If Rekognition throws ResourceInUseException — say the rule fires while I already started the model manually — that means the model is already in the target state. I log it as success with a soft_success flag, not as a failure. The desired end state was reached; who reached it doesn't matter.
>
> *Why:* Treating idempotent re-starts as failures would fill the audit log with false alarms.

**16) Watchdog: purpose and env**  `lambda/pest-model-watchdog.py:29-39`

> This is the cost control. A running Rekognition Custom Labels endpoint bills about a dollar an hour whether or not it's used, and two endpoints can run at once, so a forgotten model over a weekend is real money. EventBridge Scheduler invokes this every 15 minutes — the schedule is named pest-model-watchdog-15min. The env fallback limit MAX_RUNTIME_MIN is set to 75 live, but per-camera values override it.
>
> *Why:* The scheduled start plus this watchdog together replace the old start/stop rule pairs; the stop side had to be unmissable.

**17) Watchdog: project_arn_for_version**  `lambda/pest-model-watchdog.py:49-69`

> Small AWS quirk this handles: to describe a model version you need the project ARN, but the project ARN ends in a project id that is not present anywhere in the version ARN. So you cannot derive it by string-splitting — I have to call describe_projects and match on the project-name prefix. That's the only reason this helper exists.
>
> *Why:* Rekognition's ARN scheme forces the lookup; the docstring at lines 50-57 records the quirk so nobody 'simplifies' it back into a broken string-split.

**18) Watchdog: self-stamping**  `lambda/pest-model-watchdog.py:87-120`

> Every pass it scans the cameras table and asks Rekognition for each model's actual status — the DynamoDB flag is never trusted. If a model is RUNNING with no recorded start time, the watchdog stamps model_started_at itself, right then. If a model is not running, any stale stamp is removed so the next start measures fresh. Self-stamping is the key property: it bounds the runtime no matter how the model was started — dashboard, schedule, or someone at the AWS console.
>
> *Why:* A watchdog that depends on the starter setting a timestamp fails exactly when someone bypasses the normal path — which is the case it exists for.

**19) Watchdog: per-camera limit and stop**  `lambda/pest-model-watchdog.py:122-143`

> The limit is int of the camera's max_runtime_min field, falling back to the env value of 75 if the field is missing or malformed. worm_cam carries 45, so a scheduled morning run closes itself about 45 minutes after start. When elapsed minutes reach the limit it calls stop_project_version, sets model_running false, and removes the stamp. Worst case, a model runs its limit plus one 15-minute poll interval before being stopped.
>
> *Why:* Per-camera limits landed in v6.2 so the moth and armyworm endpoints can have different windows without redeploying the Lambda.

**Looks wrong, is deliberate:**
- No auth code anywhere in pest-monitoring-api.py — deliberate. The Cognito JWT authorizer is attached at API Gateway, in front of the Lambda; every route is gated except GET /stream/status, which the Orin/mini-PC controller polls tokenless and which is read-only. Defence: unauthenticated requests never invoke the Lambda at all.
- Access-Control-Allow-Origin is '*' (pest-monitoring-api.py:122) — looks lax, but security is the JWT, not the origin header. CORS only governs what a browser will let a page read; the gateway rejects anything without a valid token regardless of origin.
- Watchdog docstring says 'every 10 minutes' and a 60-minute default (pest-model-watchdog.py:6,21) — that is doc lag, admit it upfront. Live truth: the EventBridge schedule is pest-model-watchdog-15min at rate(15 minutes), the env MAX_RUNTIME_MIN is 75, and per-camera max_runtime_min (worm_cam = 45) wins. The code itself is cadence-agnostic so behaviour is correct either way.
- Two confidence thresholds exist and the dashboard edits only one. min_confidence (coerced to int at api:254-255) is the CANDIDATE floor before LLM verification and stays low at 10 — raising it strangles recall, the exact trap found 2026-08-05. post_verify_floor is the display/denoise floor after the LLM verdict; the dashboard threshold control edits THIS. The floor is refit per model build.
- The verifications write is two update_item calls (api:451-461) — looks redundant but is required: DynamoDB cannot SET verifications.#k if the verifications map does not exist, so the first call creates an empty map with if_not_exists.
- Clearing a verdict uses REMOVE with ConditionExpression attribute_exists and swallows ConditionalCheckFailedException (api:438-448) — clearing a verdict that was never set is a legitimate no-op, not an error.
- model_running in DynamoDB is written optimistically on start/stop (api:296,307) and can be stale — deliberate. It is only a UI hint; the watchdog asks Rekognition for the authoritative status every pass and reconciles the flag.
- GET /history is a full table scan, not a Query (api:617) — deliberate for a small table with ad-hoc filters across six different attributes; a GSI per filter would be overkill. The scan is capped at 10,000 scanned items (api:624) as a runaway guard.
- _delete_scheduled_rule swallows every exception (api:861-870) — idempotent teardown of a rule that may not exist; a delete of a missing rule must not fail the schedule save.
- The scheduler treats ResourceInUseException as SUCCESS (scheduler:152-159) — starting a model that is already running achieved the desired state; logging it as failure would create false alarms in the audit table.
- The scheduler still accepts action 'stop' (scheduler:143-145) even though the API stopped creating stop rules in v6.2 — kept on purpose so any legacy stop rule that still exists executes cleanly, and so the function can be invoked directly to stop a model.
- DELETE /detection deletes S3 objects BEFORE DynamoDB rows (api:534-554) — the row is the only index to the object, so it must outlive it; an S3 failure returns 500 with rows intact and the call is safely retryable.
- hh -= 8 at api:814 is a magic number with a story: Singapore is UTC+8 with no DST, so the SGT-to-UTC conversion is a constant; the original code skipped it and every schedule fired 8 hours late. The day-of-week shift below it handles crossing midnight.

**If she opens these, say "archived / not the live path":**
- lambda/pest-detection-processor.py — live code, but a DIFFERENT subsystem (the S3-triggered detection pipeline). If Dr. Li opens it: 'that is the processor, covered separately; today's three files are the API, scheduler, and watchdog.'
- deployer/audit/pest-model-watchdog_src/ and pest-model-watchdog.zip — pre-v6.2 audit snapshots that LACK the per-camera max_runtime_min. The live source of truth is lambda/pest-model-watchdog.py (this mismatch was a real deployer bug, fixed 2026-08-11).
- archive/old_lambda_snapshot/ — the old-account pest-control-api v3.6 and pest-history-query v3; both were merged into pest-monitoring-api.py v4, nothing there is live.
- pest-sched-*-stop EventBridge rules — retired in v6.2 (start-only scheduling). The stop-deletion code at api:904 and 929 exists only to clean up legacy pairs, not because stop rules are still created.
- WebSocket broadcast — removed in the v4.0 merge (api header line 18); the dashboard polls now. Any broadcast code found in old snapshots is dead.
- /stream/* routes (api:963-1004) — placeholder toggles on the stream_enabled flag only; actual KVS producing happens on the mini PC/Orin, and HLS playback is the separate kvs-hls-handler Lambda.

---

## 3. dashboard (web/dashboard_v4 — ARGUS v5.x, vanilla-JS ES modules)

*A framework-free static web dashboard (13 ES modules + one index.html) that CAG operators use to watch live streams, review detections with per-box verification, read analytics, and control cameras/models — every backend call funnelled through one authenticated API client.*

**1) index.html — shell and single module entry**  `index.html:113-119`

> The whole app is one HTML file plus 13 ES modules. There is no framework and no build step. Two CDN globals load first — Chart.js 4.4.1 and hls.js 1.4.12 — then line 119 loads js/main.js as the single module entry. Deploying is literally one aws s3 sync; any static server can host it.
>
> *Why:* No build step was a handover decision: the next student can open the files in a text editor and the deploy is reproducible with zero toolchain.

**2) init() and startApp() — boot sequence**  `main.js:167-191`

> Boot is five steps: paint the tab bar, open the IndexedDB image cache early so the first gallery render is instant, wire the login form, and if a session exists run startApp, otherwise show the login overlay. startApp fetches /settings, /model/status and /stream/status, then renders the default tab. If settings fail I still boot with empty cameras rather than a blank page.

**3) window bridge — Object.assign(window, {...})**  `main.js:137-162`

> The UI is rendered as HTML strings with inline onclick handlers. Inline handlers resolve names on window, and module scope is not window scope, so this block exposes exactly the inline-handler surface and nothing else. Everything not listed here stays module-private. If I add a new inline onclick and forget to add it here, the click throws 'X is not defined' — that is the contract.
>
> *Why:* It keeps the discipline of modules (private scope) while still using simple string-template rendering with no framework.

**4) switchTab() — router with teardown**  `main.js:103-130`

> Tab switching is a plain function, not a router library. The important part is teardown: leaving Live destroys the HLS player, leaving Analytics destroys the three Chart.js instances, leaving Settings stops the model-status polling timer. Without this, charts leak canvases and the poller keeps hitting the API in the background.

**5) CONFIG — the one per-environment file**  `config.js:6-20`

> This is the only file that changes between hosts. API base URL, Cognito region and client id, the 5000 ms model poll interval, UTC+8 for Singapore time, and a 400 MB IndexedDB cache ceiling — bumped from 200 MB when gallery pagination went to 500-record fetches. The ARGUS deployer templates these values at deploy time; the client id is a public identifier, not a secret.

**6) api._fetch — the single backend funnel**  `api.js:9-23`

> Every backend call in the app goes through this one method — there is no other fetch to our API anywhere. It attaches the Cognito ID token as a Bearer header, parses the body defensively, and on any 401 it reopens the login overlay and throws. So auth handling is written once, and the 21 endpoint wrappers below it are one-liners.
>
> *Why:* One funnel means one place to change the base URL, the auth header, and the error contract.

**7) getIdToken() — token refresh**  `auth.js:86-92`

> Tokens sit in localStorage and I refresh the ID token when it is within 5 minutes of expiry, using the stored refresh token. There is no Cognito SDK — it is raw fetch against the IDP endpoint, because the app is bundler-free. The login screen is UX only; the real enforcement boundary is the JWT authorizer on API Gateway.

**8) getVerifiableBoxes() — the canonical box list**  `bbox.js:24-33`

> This extracts the target-label detections from a record and sorts them by confidence descending, so box index 0 is always the highest-confidence one. That index is the shared key: the backend verify call, the review rows, the overlay, and analytics all agree on it. Note lines 18-23: since v4.5 there is deliberately no client-side threshold re-filter — the processor's server-side gate already decided which boxes survive.

**9) renderOverlayBoxes() — normalised coords to percentage divs**  `bbox.js:69-83`

> The processor stores box geometry normalised 0-1. I multiply by 100 and position plain divs with percentage left/top/width/height inside an overlay that exactly covers the image. Because everything is in percent, the boxes stay glued to the image at any window size and any zoom level with zero recalculation. A box flagged false-positive simply is not drawn.
>
> *Why:* Percentages make the overlay resolution-independent — no canvas redraw on resize, no coordinate math on zoom.

**10) verifyClick() — per-box TP/FP with optimistic update**  `bbox.js:153-186`

> Each box can be marked true-positive or false-positive individually; clicking the same verdict again clears it. The update is optimistic: I snapshot the previous verdicts, update the UI immediately, then persist via POST /detection/verify — and if the backend fails I roll back to the snapshot and toast the error. So the reviewer never waits on the network, but the screen never lies about saved state.

**11) attachImageZoom() — the --z zoom-chrome trick**  `bbox.js:243-257`

> Zoom range is 1x to 6x, wheel steps are 0.85 down and 1.18 up, double-click toggles 2.5x. The overlay gets the identical CSS transform as the image so boxes track perfectly. The subtle bit is line 255: the same transform also magnified the box borders — a 2.4 px border became 14 px at 6x and the flag button covered a small worm. So I publish the live scale as a CSS variable --z, and the stylesheet divides every border, font and padding by it. The chrome stays constant on screen while the worm keeps growing.
>
> *Why:* We hit this in real review at high zoom: the decoration swallowed the target. One CSS variable fixed it without touching geometry.

**12) paintCardBoxes() — boxes on object-fit:cover thumbnails**  `bbox.js:193-219`

> Gallery thumbnails use object-fit:cover on a 4:3 box, so the frame is scaled to fill and centre-cropped. I compute that cover rect — scale is max of the two ratios, line 208 — size the overlay to it, and let the card's overflow:hidden clip the excess. Then the same percentage boxes work on thumbnails too, and the card always matches the modal.

**13) AI filter threshold input — post_verify_floor**  `settings.js:569-576`

> This number input is bound to the camera's post_verify_floor — the confidence floor applied after the LLM verification step, which is the one user-facing threshold. The fallback value 33 is the current live production floor. The low Rekognition candidate floor, min_confidence 10, is deliberately not exposed here — operators tune the post-verify floor only.
>
> *Why:* We learned the floor is a per-build fit, not a constant: the previous build ran at 49, the retrained model scores the same images lower, so it was refitted to 33 on 2026-08-13.

**14) debouncedSaveCamera() + saveCameraSettings() — auto-save with verify-by-refetch**  `settings.js:727-788`

> Edits auto-save after 500 ms of quiet, one debounce timer per camera so two cameras cannot cancel each other. The save then re-fetches GET /settings and compares every field it sent against what actually came back — numeric compare for the floor, string for the rest. Full match flashes the ok indicator for 2 seconds; any mismatch names the field in an error toast. I never trust that a POST worked; I verify it did.

**15) submitUpload() — the test-upload key contract**  `settings.js:320-333`

> The Test upload panel is how we demo live detection, since there are no worms at Jewel to stage. The S3 key encodes the run config in the waypoint segment: manual_test__conf10__llm-sonnet46. The processor Lambda parses that suffix and overrides the candidate floor to 10 and the verifier model for this one run only — no DynamoDB mutation, no race, no cleanup. Then waitForDetection polls /history every 3 seconds up to 60 seconds for the result.

**16) startModelPolling() — 5 s status poll with surgical DOM patching**  `settings.js:863-895`

> While the Cameras sub-tab is open I poll /model/status every 5000 ms — Rekognition endpoints take 5 to 10 minutes to start, so the operator needs live feedback. On a state change I do a surgical patch of only the status badge, buttons and hint; I never re-render the whole card, so a value the user is mid-typing is never clobbered. The poller only runs when custom-model cameras exist, and switchTab kills it on leaving Settings.

**17) zoneBuckets7d() / dailyBuckets() — count boxes, not photos**  `analytics.js:91-120`

> Every analytics number counts bounding boxes, not photos. Before v3.6.2 a photo with 5 worms contributed 1 to its zone; now it contributes 5, which is what CAG actually asks — 'where are the pests', not 'where are the photos'. Both bucket functions go through getCountedBoxes, so a box a human flagged as false-positive drops out of every chart consistently.

**18) FP-rate stat — the opt-out counting model**  `analytics.js:138-153`

> Verification is opt-out: a detection counts unless a human marked it false-positive, so unreviewed and confirmed both count. The 'Flagged FP' stat's denominator is all detections in the last 7 days, not just reviewed ones — CAG only has to flag mistakes, not confirm every true detection. That is the entire review workload we are asking of them.

**Looks wrong, is deliberate:**
- bbox.js does NO client-side confidence filtering (bbox.js:18-23, 49-50) — looks like a missing filter. Defence: since v4.5 the processor's hybrid gate is authoritative — Rekognition proposes at floor 10, the LLM verifies, post_verify_floor decides what is written. Any box present in the record is already confirmed; re-filtering by the camera's current threshold would wrongly hide legitimate low-confidence detections the LLM confirmed, and raising the threshold later would retroactively hide old confirmed records.
- The '?? 33' fallback in settings.js:572 looks like a magic number. Defence: 33 IS the current live production post_verify_floor. The floor is a per-build fit — the previous build ran at 49; the retrained model scores the same images lower, so it was refitted to 33 on 2026-08-13 (camera row + Lambda env together).
- costs.js is a whole module for a tab that does not exist in TABS (main.js:39-40). Defence: deliberate dormancy, not dead code by accident — the shared IAM user has no ce:GetCostAndUsage permission (needs account-root billing access). The functions are kept verbatim and still exposed on the window bridge (main.js:161), so re-enabling is one line in TABS.
- conf10 is hard-coded in the upload key (settings.js:332). Defence: it pins the Rekognition candidate floor at 10, the validated production value, for that one run; the processor parses the waypoint suffix so there is no DynamoDB mutation, no race, no cleanup. The user-facing threshold is post_verify_floor, applied after verification.
- The comment at settings.js:324 names the Lambda 'image-detection-handler' but the deployed function is pest-detection-processor. Defence: comment lag only; the behaviour described (suffix parsing, per-run override) is exactly what the deployed processor does. Say it before the professor spots it.
- submitUpload keeps a legacy presigned-PUT branch (settings.js:355-361) alongside the modern POST+FormData path. Defence: backward compatibility from the v3.4.4 backend change; the POST path (AWS-recommended for browser uploads) is what runs in production.
- Cognito client id is committed in config.js:12. Defence: an app client id is a public identifier, not a secret — the JWT authorizer on API Gateway is the enforcement boundary. The file must carry live values because a hand-run 'aws s3 sync' ships it as-is.
- Tokens in localStorage and a login overlay that is 'just CSS' (auth.js header comment, lines 4-7). Defence: the browser login is UX only by design; every API route except GET /stream/status is enforced server-side by the API Gateway JWT authorizer. Hiding the UI is not the security model.
- bbox.js has runtime-circular imports with gallery.js and modal.js (bbox.js:5-6). Defence: safe because all cross-calls happen inside functions, never at module evaluation time — ES modules handle this; it is documented at the top of the file.
- Two different box sources: getVerifiableBoxes reads it.labels, getDrawableBoxes reads it.bboxes (bbox.js:35-43). Defence: labels is the canonical verify/analytics list, bboxes carries drawing geometry; both use the same confidence-desc sort so indices line up with verifyMap. The comment documents the one divergence case.
- The FP-rate denominator includes unreviewed detections (analytics.js:139-141) — looks like it dilutes the rate. Defence: intentional opt-out model — a detection counts unless flagged; CAG's only job is flagging false positives, so 'share of all detections flagged FP' is the honest operational number.
- 'state' itself is exposed on window (main.js:139). Defence: one inline onchange in the upload sub-tab writes state.uploadCam directly; exposing the shared state object was cheaper than a setter for a single field, and it is listed explicitly in the bridge with a comment.
- The v5.5 comment at analytics.js:334-336 describes a ReferenceError ('sub' read without being declared) — it reads like a live bug but is the record of a FIXED one: the undeclared read used to abort loadAnalytics so Camera health and By camera never rendered. The declaration on line 336 is the fix.

**If she opens these, say "archived / not the live path":**
- web/_archive/dashboard_v3_4.html through dashboard_v3_9.html — single-file pre-module predecessors. dashboard_v3_9.html is kept untouched as the emergency fallback (it is the file the v4.0 module split came from; every module header says 'Split from dashboard_v3_9.html'). The live path is web/dashboard_v4/.
- web/_archive/dashboard_v4_v42_pre-argus_2026-07-10/ — full copy of the dashboard before the ARGUS liquid-glass reskin. Historical backup only.
- web/dashboard_v4/js/costs.js — dormant Costs tab, not routed (removed from TABS in main.js:39-40 because the IAM user lacks Cost Explorer access). Kept deliberately so it can be re-enabled by one line.
- WebSocket code — there is none. Comments in main.js (44, 168) and settings.js (383) reference 'WebSocket removed v3.7'; everything live-ish (notifications, upload results, model status) is polling. If asked where the WS client is: it was removed, polling replaced it.
- docs/dashboard.md mentions a js/cameras.js — no such file exists. Camera settings cards live in js/settings.js (Cameras sub-tab); live-view camera cards live in js/live.js.
- reports/manual/04_dashboard_frontend.md section 4.4 shows development-account config values (API zwpcbivmsj, client 4husu6afr835e235eu9dqp8av6) as the repo copy — the actual js/config.js now carries the production values (vzfl7s6z00, 6vebotf45bp8u46cnraddiaplv). Trust the code; the manual table lags one migration.
- The 'min confidence override' input in Test upload — gone. Older docs/comments reference a user-facing min-confidence override; the current per-run choice is only the verification model (sonnet46/haiku45), with conf10 fixed in the key.

---

## 4. robot (Go2 patrol + capture + live stream)

*A single rclpy node on the Jetson Orin that drives the Go2 around a surveyed route over plain ROS 2 topics, and at each zone captures a SIYI A8 frame, uploads it to S3, and waits for the cloud detection record in DynamoDB before moving on.*

**1) Header: why plain ROS 2 topics, no vendor SDK**  `robot/go2_patrol_gated.py:16-30`

> I deliberately do not use unitree_sdk2_python. Its cyclonedds binding segfaults on this Orin. USLAM's whole control surface is plaintext std_msgs/String on two topics, /uslam/client_command out and /uslam/server_log back, so a standard rclpy node is enough. I reverse-engineered the wire format from the phone app: control verbs carry an inner pair of double quotes, goal poses are bare paths with no quotes. Those two quoting rules are not interchangeable.
>
> *Why:* Stability over convenience: rclpy on rmw_cyclonedds_cpp is stable for /uslam/* while the vendor Python SDK crashes on this hardware.

**2) main() - the orchestrator**  `robot/go2_patrol_gated.py:601-654`

> The flow per scan waypoint is: USLAM goal out, REACHED back, gimbal settle, capture one RTSP frame, put_object to S3, then poll DynamoDB until the processor's record exists, then next goal. I spin the rclpy node in a daemon thread and run the route in the main thread. A 3-second countdown at start is the window to unplug the tether cable.
>
> *Why:* The robot's motion is gated on the cloud actually processing each frame - that is the 'gated' in the filename.

**3) send_verb / send_goal - the verb protocol, repeat=1**  `robot/go2_patrol_gated.py:265-279`

> This is the most important fix in the file. Both senders default to repeat=1: one publish, then wait for the reply token on server_log. They used to default to 3 as a hedge against DDS discovery loss. We measured what duplicates do: about 46 repeated stops produced 4516 TIMEOUT_ODOMETRY events, and six navigation/start messages in 26 seconds drove the timeout count from 1 to over 1500 - the MCU wedges and only a power cycle recovers it. Duplicated goals were also killing waypoints, because a repeat set_goal_pose arriving after TRACKING raises GOAL_CHANGED. After the repeat=1 change on 2026-07-30 we ran three consecutive 5/5 patrols with zero retries.
>
> *Why:* Discovery is already established by the get_map_id handshake at bringup, so one publish is enough. Send once, wait for the token, never stack retry loops on a repeating sender.

**4) wait_for_any - the entire event mechanism**  `robot/go2_patrol_gated.py:242-256`

> All feedback comes through this one function. The subscriber buffers every server_log line with a monotonic timestamp in a 500-line deque. Callers take a timestamp with now() before sending a command and pass it as 'since', so a stale reply from an earlier command can never satisfy a new wait. It blocks on a condition variable with the exact remaining time, and returns the matched substring or None on timeout.
>
> *Why:* One primitive replaces a state machine: every stage of bringup and navigation is just send-then-wait_for_any on the right tokens.

**5) bringup step 1: get_map_id as the liveness check**  `robot/go2_patrol_gated.py:288-300`

> get_map_id doubles as the MCU health check. USLAM runs on the dog's sport MCU at 192.168.123.161, not on the Orin, so I have no process visibility into it. If there is no reply within 8 seconds, the service is down and the only recovery is power-cycling the whole dog, so I fail loudly and abort. This exchange also warms up DDS discovery for everything after it.

**6) Localization seeding loop: stop, seed, start, retry x4**  `robot/go2_patrol_gated.py:319-363`

> Localization init is intermittent - roughly 60 percent per try, measured 2026-07-30: identical seed, identical position, failed at 12:56 and succeeded at 13:04. So I retry the whole stop-seed-start sequence up to 4 times instead of aborting. For the seed I prefer a fresh live odom pose, and I refuse one older than 3 seconds - driving the dog by remote stops USLAM and freezes the pose where the dog used to be. If there is no fresh odom I fall back to INITIAL_POSE, which requires the dog to be parked on it. The stop-first order matters: seeding on top of a live localization fails - on 2026-07-30 three start calls each returned success and then printed 'initialization failed' 6 seconds later. The phone app always sends stop first, so I mirror it. And I wait on failure tokens as well as success tokens, so a known failure returns immediately instead of burning the full 30-second timeout.

**7) The nudge, then navigation/start with re-nudge retries**  `robot/go2_patrol_gated.py:379-408`

> navigation/start only succeeds while localization is actively tracking, and localization only tracks while the dog moves. So right before it I send a tiny in-place rotate goal - 0.30 radians, about 17 degrees - and wait 8 seconds. That replaces the old manual trick of walking the dog two steps with the remote. If navigation/start still refuses I retry once, re-nudging with double the rotation. The nudge is deliberately not verified - I will defend that in the gotchas.
>
> *Why:* Measured: a pose-change verification cost 37 seconds per run, reported 'did not move' three times per run, and never changed an outcome.

**8) goto() - one goal, one retry, fail loud**  `robot/go2_patrol_gated.py:411-429`

> Each waypoint is one goal and a single wait_for_any on both the REACHED token and the four failure tokens - NO_PATH, GOAL_CANCELLED, FAILURE, GOAL_POINT_UNREACHABLE - with a 90-second budget. Putting the failure tokens in the same wait means a NO_PATH returns immediately instead of wasting the 90 seconds. On failure I retry the goal once. That resend is still repeat=1 and is safe: the old goal is already dead, so it is a new goal, not a duplicate into a live one. After REACHED the state machine sits at WAITING and the dog holds position, so there is no pause logic to write.

**9) Scan hook: settle timing, then capture**  `robot/go2_patrol_gated.py:657-672`

> wp1 and wp_return carry capture: False, so they navigate only. At the three zones I wait before capturing: 2.0 seconds for a plain FOLLOW point, because the gimbal lags the body by 1 to 2 seconds after the dog's final in-place rotation, or 1.5 seconds if a per-point LOCK override was applied. Capture without the wait gets a mid-swing frame. The gimbal class is entirely fail-soft - any gimbal error only logs and the patrol continues.

**10) capture_frame + upload_frame: A8 RTSP to S3**  `robot/go2_patrol_gated.py:533-567`

> I open the A8's RTSP stream at rtsp://192.168.144.25:8554/main.264 over TCP - the transport option is set in the environment before cv2 is even imported, line 74. I read 30 frames and keep the last good one; that drains the decoder's stale buffer so the kept frame is current. JPEG quality 92, frames are 1080p, which is the A8 RTSP hardware cap. The key is frames/worm_cam/<waypoint>/<UTC timestamp>.jpg into argus-frames-506868652945 - the worm_cam segment routes it to the right camera config cloud-side, and the waypoint name makes each zone traceable on the dashboard. The S3 event triggers the processor Lambda; nothing is stored on the Orin.

**11) wait_for_detection - the DDB gate**  `robot/go2_patrol_gated.py:570-595`

> This is the cloud gate. I query pest-monitoring-detections by image_id equals the exact S3 key, Limit 1, every 1.5 seconds, for up to 150 seconds. The gate opens on record EXISTENCE, not on a positive detection - the processor's put_item is unconditional, clean frames write a record too, so the gate cannot deadlock on a waypoint with nothing in it. A query exception just logs and retries next poll, so a transient network error cannot abort the patrol. On timeout it fails open and moves on. Measured on site: 10 to 47 seconds per frame; the current v6.3 processor with tiling and LLM verification runs 24 to 54 seconds, well inside the 150-second budget.


**12) kvs_controller: the control signal**  `robot/kvs_controller.py:42-56`

> This daemon polls GET /stream/status?camera=worm_cam on the pest-monitoring-api every 5 seconds with a 10-second HTTP timeout. Plain HTTPS, no AWS credentials on the control path - that route is deliberately exempt from the dashboard's Cognito authorizer so device pollers are untouched by auth. Any error returns (None, None) and the loop just keeps its current state and retries.

**13) The reconcile loop**  `robot/kvs_controller.py:131-163`

> It is a reconcile loop, not a command handler: compare desired state from the API with whether the GStreamer child is actually alive, and start or stop to match. If the child died on its own I log its return code and clear the handle. If the toggle is on but the row has no kvs_stream_name, I log an error and start nothing. State is logged only when it changes, so a healthy idle loop is silent. This is what turns the dashboard's DynamoDB stream_enabled flag into real video.

**14) The pipeline and the group-kill**  `robot/kvs_controller.py:60-122`

> The pipeline is pure passthrough, no re-encode: rtspsrc from the A8 over TCP, rtph264depay, h264parse, kvssink with fragment-duration 2. I spawn it in its own process group with os.setsid, because gst-launch forks helpers - on stop I SIGTERM the whole group, wait 5 seconds, then escalate to SIGKILL and wait 2 more. The SIGTERM/SIGINT handler tears the pipeline down too, so a systemd stop never leaves an orphan pushing to KVS.

**Looks wrong, is deliberate:**
- The gate FAILS OPEN on timeout (go2_patrol_gated.py:594). Defence: the gate exists to pace the robot against the cloud, not to police it. The processor writes a record unconditionally even for clean frames, so a timeout means a pipeline fault, and a stuck robot in a public planting is worse than a missed record. The failure is logged loudly and the frame is still in S3 for later processing.
- Two comments in the patrol still describe the old repeat=3 behaviour: the send_verb docstring says 'Sent a few times' (line 266-267) and the bringup comment says 'send_verb defaults to 3, which tripled every stop' (line 339-340). Defence: known doc lag, flagged in the technical manual (5.7). The signature defaults are 1, that is what shipped, and that is what ran the three 5/5 validation patrols. Also the tense of line 340 is historical - it explains WHY repeat=1 is passed there.
- The comment block above INITIAL_POSE (lines 92-94) describes the PRIMARY lab-room map, but the values below and the WAYPOINTS are the Jewel v2 set (map 1BEC7FFD..., surveyed 2026-07-29). Defence: values are current, comment is stale; the manual flags this exact lag. The map id itself is never hardcoded - bringup reads it live from get_map_id.
- The nudge is deliberately NOT verified (lines 372-378). Looks sloppy - send a motion command and just sleep 8 seconds. Defence: a pose-change verification was tried on 2026-07-30; it reported 'did not move' three times per run while navigation/start then succeeded first try and the route completed 5/5 twice. It cost 37 seconds per run and never changed an outcome. If navigation/start does refuse, its retry re-nudges with double the delta anyway.
- The Gimbal class swallows every exception (lines 436-527). Defence: fail-soft by design - a patrol without gimbal control is still a valid patrol, because FOLLOW is saved as the gimbal's power-on default in SIYI PC Assistant, so the camera tracks the dog's heading even with zero SDK control. The requestFollowMode on connect is defensive, not load-bearing.
- `if seed is not self._fallback_seed` (line 341) uses identity, not equality. Defence: intentional sentinel test. _fallback_seed is set only when there was no fresh odom, and the identity check asks 'did we fall back?' - fresh odom is the proxy for 'localization is running, so there is something to stop'. No odom means nothing to stop, and sending a stop anyway just adds verb churn against the MCU wedge budget.
- ABORT_ON_NAV_FAIL = False (line 157): a failed waypoint is skipped, not fatal. Defence: deliberate demo policy - one bad goal cell should not kill a five-point patrol. Failures are counted and reported in the end summary, and goto already failed loudly with the token that killed it.
- CAPTURE_WARMUP = 30 reads and throws away frames, keeping only the last (lines 541-544). Looks wasteful. Defence: it drains the RTSP decoder's stale buffer so the kept frame shows the scene NOW, after the settle wait, not two seconds ago mid-turn.
- DDB_GATE_TIMEOUT_S = 150 is shorter than the processor Lambda's 600-second timeout, so on a pathological frame the gate opens before the Lambda finishes. Defence: acknowledged and accepted - the budget was sized under the old 180-second Lambda, and the measured v6.3 envelope is 24-54 seconds per tiled frame, so the 150-second budget has roughly 3x headroom over the real worst case.
- kvs_controller.py discards the GStreamer child's stdout and stderr (line 97). Defence: kvssink writes its own log to ./log/kvs.log under the SDK build directory (which is why the systemd unit sets that as WorkingDirectory); duplicating it into the journal is noise.
- kvs_controller.py has stale strings: the main() docstring says camera=armyworm_go2_a8mini (line 134) and the header mentions the retired dev profile nbk2 (line 3). Defence: doc lag from the 2026-07 camera-id migration; the operative default is CAMERA_ID='worm_cam' (line 21) and the production API_BASE (line 24). The manual (5.10) explicitly flags this and says the code wins.
- SKIP_LOCALIZATION_BRINGUP = False (line 134) sits right under a comment warning that re-seeding a live localization destroys it. Defence: the stop-first fix (line 308-318) made cold app-free bringup safe, so False is the validated default; the True path is kept for the case where localization was established from the app and should not be touched.

**If she opens these, say "archived / not the live path":**
- robot/_archive/2026-07-29/ and robot/_archive/2026-07-30/ - field run logs from the failed first Jewel run and the three 5/5 clean runs, plus Orin script backups. Evidence, not live code. If asked: 'that is the archived validation record; the live script is robot/go2_patrol_gated.py'.
- robot/map_profiles.md Jewel v2 block has doc lag: it says SKIP_LOCALIZATION_BRINGUP=True is required and lists an older INITIAL_POSE. The deployed code (False, pose -4.970/-0.657/1.260) is what ran the three 5/5 patrols - the code is authoritative, and the manual (5.6) records this lag explicitly.
- The per-waypoint 'cam' override interface in the patrol (Gimbal.apply_override, lines 483-504) is dormant - no deployed waypoint carries a cam dict. It exists for future per-point fine-tuning; every live capture is plain FOLLOW.
- Old dev-account values in logs and history (bucket frames-armyworm-366356442579, API zwpcbivmsj) - the repo mirrors were repointed to the production account 506868652945 on 2026-08-13 (bucket argus-frames-506868652945, API vzfl7s6z00). Anything showing the old names is history, not the live target.

---

## 5. minipc (fixed Hikvision camera node: still-frame capture + KVS streaming daemon)

*The production-shape edge node: a fixed Hikvision IP camera whose frames land in S3 under the exact same key format as the robot's, so the cloud pipeline cannot tell a fixed camera from the Go2 — the proof the system is camera-agnostic.*

**1) System-position comment (data flow diagram)**  `minipc/capture_and_upload_v4_armyworm.py:24-53`

> This script only does the first hop: camera to S3. One put_object under frames/{camera}/{waypoint}/{timestamp}.jpg, and the S3 PutObject event triggers everything downstream — the processor Lambda, Rekognition, DynamoDB, SES, the dashboard. The script never calls Rekognition or SES directly. That separation is the whole architecture: the edge is dumb, the cloud is smart.
>
> *Why:* Any device that can write one S3 object in the right key shape is a valid camera node. That is the camera-agnostic claim in one design decision.

**2) DEFAULTS block + constants**  `minipc/capture_and_upload_v4_armyworm.py:89-110`

> Everything tunable sits in one block: camera worm_cam, waypoint fixed_cam, bucket argus-frames-506868652945, profile prod, region us-east-1, fallback interval 60 seconds. Below it: JPEG quality 90, and MIN_INTERVAL 5 seconds as a safety floor so no config value can make the loop hammer the camera. The RTSP URL is deliberately not here — it carries the camera password, so it comes from the environment only.
>
> *Why:* camera_id is the routing key: the processor reads it from the S3 key and loads that camera's model ARN from pest-monitoring-cameras. Change one default and the same script drives the moth camera instead.

**3) main() — mode dispatch**  `minipc/capture_and_upload_v4_armyworm.py:266-313`

> Three modes. --image uploads a local file with no camera and no OpenCV — that is how I push holdout images through the live pipeline. --once grabs one frame and exits, my smoke test. No flag means loop mode, the long-running service. If RTSP_URL is missing in a camera mode, the script exits 1 with an export hint instead of limping on.
>
> *Why:* --image mode means the entire cloud chain can be exercised and demoed with a known image, which matters because there are no worms at the live site.

**4) get_capture_settings() — remote on/off switch**  `minipc/capture_and_upload_v4_armyworm.py:140-150`

> Loop mode polls one DynamoDB row, detection_settings in pest-monitoring-system-config, every cycle. The dashboard Settings page writes auto_capture and capture_interval into that row, so an operator can start and stop capture without touching this process. The interval is clamped to at least 5 seconds. If the read fails, it returns disabled — a broken config read idles the loop, it never captures blind.
>
> *Why:* Cloud-driven control with no inbound connection to the VM needed. The VM is NAT'd; it can only poll out.

**5) capture_frame() — RTSP to JPEG**  `minipc/capture_and_upload_v4_armyworm.py:160-189`

> Up to 3 attempts, 1 second between them, because RTSP open is flaky. On a good open I call cap.read() twice and throw both frames away — RTSP hands you stale buffered frames first, so the flush makes the third read actually current. The frame is JPEG-encoded at quality 90. cv2 is imported lazily inside the function so --image mode runs on machines without OpenCV.
>
> *Why:* We saw stale frames on RTSP grabs, so the code discards 2 buffered frames before the real read.

**6) upload_frame() — the key contract**  `minipc/capture_and_upload_v4_armyworm.py:223-231`

> This is the contract line: frames/{camera}/{waypoint}/{timestamp}.jpg. The processor's parse_s3_key requires the first segment to be 'frames' and at least 4 segments, then uses the camera segment to pick the model. The timestamp includes microseconds so rapid --once runs never collide. The robot's uploader writes the identical shape, so the cloud cannot tell this camera from the Go2.
>
> *Why:* One key format is the entire edge-to-cloud interface. Get it wrong and either nothing fires or it falls back to the manual_upload camera and runs the wrong model.

**7) main() loop mode — the capture cycle**  `minipc/capture_and_upload_v4_armyworm.py:319-345`

> Each cycle: read the on/off switch, capture, upload, sleep the interval. Every failure is non-fatal — a failed capture logs [Skip] and waits, a failed upload logs and carries on. The only exit is Ctrl-C, which the handler at the bottom turns into a clean [Stop]. With --ignore-config it loops on a fixed interval, still floored at 5 seconds.
>
> *Why:* In a periodic capture loop the next frame is only seconds away, so retrying a failed upload buys nothing. Log and move on.

**8) kvs_controller.py — config block**  `minipc/kvs_controller.py:43-57`

> Second file: the live-video daemon. It polls the backend every 5 seconds and reconciles a GStreamer child process to match what the dashboard says. Every setting is an env var: the RTSP password has an empty default, never a hardcoded one. One process drives one camera; on this machine the run script exports CAMERA_ID=moth_cam.
>
> *Why:* The same file runs on the Jetson Orin for worm_cam, so identity comes from the environment, not the code.

**9) fetch_desired_state() — the control signal**  `minipc/kvs_controller.py:82-97`

> One plain HTTPS GET to /stream/status?camera=moth_cam with a 10-second timeout. It is the one unauthenticated route in the whole API — every other route needs a Cognito JWT. It is read-only and returns nothing sensitive, so the edge daemon needs zero AWS credentials for control; only the kvssink child needs keys. On any error it returns (None, None), which the loop treats as 'no information'.
>
> *Why:* v1 read DynamoDB directly and needed AWS credentials on the control path. v2 moved control to the HTTP API: single source of truth, no keys.

**10) main() — the 5-second reconcile loop**  `minipc/kvs_controller.py:190-221`

> Classic desired-versus-actual reconcile. On a failed poll it holds current state — a dead backend can never stop a running stream and never start one. If the GStreamer child died, that is detected here by poll() and, since desired is still true, it restarts within one 5-second cycle. State is only logged on transitions, so a stable stream produces silence, not a line every 5 seconds.
>
> *Why:* Missing information must not flap the stream. Freeze on unknown, act only on a real answer.

**11) build_gst_args() — the transcode pipeline**  `minipc/kvs_controller.py:104-122`

> The pipeline transcodes: rtspsrc over TCP into decodebin, videoconvert, x264enc with a keyframe every 45 frames and zerolatency tuning, then h264parse into kvssink with 2-second fragments. We transcode because the Hikvision RTSP output is not guaranteed clean H.264 passthrough. The Orin's SIYI A8 pipeline is passthrough; this one cannot be. It is a Python list, no shell, so the URL is never shell-parsed.
>
> *Why:* Measured decision: passthrough into kvssink was unreliable from this camera, so the code re-encodes. HLS playback on the dashboard validated the transcode path end to end.

**12) start_gst() / stop_gst() — process lifecycle**  `minipc/kvs_controller.py:140-169`

> Start uses preexec_fn=os.setsid so the pipeline gets its own process group. Stop then kills the whole group: SIGTERM, wait 5 seconds, escalate to SIGKILL, wait 2 more. That matters because gst-launch spawns children — killing only the parent leaks the RTSP connection to the camera. SIGTERM and SIGINT are wired to the same teardown, so systemd restarts are clean with no orphans.
>
> *Why:* We saw leaked RTSP connections when only the parent died, so the code kills the process group, not the PID.

**Looks wrong, is deliberate:**
- capture_frame throws away two frames before the real read (v4 lines 177-178). Looks wasteful; it is a deliberate flush — RTSP delivers stale buffered frames first, and without the flush the 'current' upload is seconds old.
- get_capture_settings returns (False, fallback) on any error (v4 lines 148-150). It fails CLOSED, not open: a broken config read idles the loop instead of capturing with no dashboard control. Defence: the dashboard switch is the authority; no answer means do not capture.
- fetch_desired_state in kvs_controller does the opposite — on error it returns (None, None) and the loop HOLDS current state (lines 193-196). Looks like a fail-open branch; it is deliberate: 'no information' must not stop a running stream or start a stopped one. The two error policies differ because the cost of a wrong action differs.
- The v4 docstring still narrates the W7 nbk2-account migration and the flow diagram says dashboard_v3_8.html (line 48). That is a dated history block kept on purpose; the CURRENT TARGET note at line 5 and the DEFAULTS block (prod account 506868652945, bucket argus-frames-506868652945) carry the truth. Same for the 'nbk2 credentials' comment at kvs_controller line 137 — account history, superseded by the prod note in its header.
- The filename says 'armyworm' but the script is camera-agnostic. The name is lineage (W7 migration from the person_cam version); --camera moth_cam routes the same frame to the moth model. The routing lives in the S3 key, not the script.
- kvs_controller's default CAMERA_ID is worm_cam (line 43) even though this machine drives moth_cam. Deliberate: the identical file runs on the Orin; this node's identity is the export in run_kvs_controller.sh, which overrides the default.
- GStreamer stdout/stderr go to /dev/null (kvs_controller lines 143-144). Looks like hiding errors; deliberate — gst-launch spews continuously and would flood the journal. Crash detection uses the process return code (line 202), and a persistent failure shows up as visible restart churn in journalctl.
- upload_frame has no retry (v4 lines 223-231). Deliberate: in loop mode the next cycle produces a fresher frame anyway, so a retry would upload a stale one; --once and --image exit 1 so a script caller sees the failure.
- The RTSP password appears in the GStreamer command line, visible in ps on the VM. Known and accepted on this single-user lab machine; the win is that no credential is in any committed file (v1/v3 hardcoded it — v4 and v2 ended that).
- No systemd unit exists for the v4 capture script. Not an omission: it is an on-demand tool (manual captures, --once smoke tests, --image pushes), not a service. The always-on service on this box is kvs-controller.
- On the production account the moth_cam row reads stream_enabled=false and the account has zero KVS streams yet (deploy ran without --live-view). So the daemon polling production and starting nothing is correct behavior, not a fault. If the dashboard toggle were flipped before the stream is created, kvssink would fail and the crash-detect would relaunch it every 5 seconds — create moth-cam-stream first.

**If she opens these, say "archived / not the live path":**
- The dashboard_v3_8.html label in the v4 flow comment (line 48) — historical; the live dashboard is the v5.2 ARGUS build, cloud-deployed with Cognito.
- The nbk2 / 366356442579 account references throughout both docstrings — development-account history. The live target is production account 506868652945, profile prod, since the 2026-08-13 repo repoint. (The copies on the VM itself still carry dev-account values pending the hardware-return sync pass — that is a tracked task in the manual chapter 6.10, not drift.)
- The Orin's twin kvs_controller instance for worm_cam — if the reviewer asks about it: its RTSP source no longer exists since the A8 moved to an HDMI-USB capture card (2026-07-29), so worm_cam streaming is parked. This machine's moth_cam instance is unaffected; that story belongs to the Go2/Orin chapter, not this one.
- run_kvs_controller.sh and kvs-controller.service repo copies carry <SET_ON_VM> placeholders for credentials — that is by design (real values live only on the VM), not missing configuration.

---

## 6. deployer (ARGUS)

*ARGUS is a desktop one-click deployer: app.py is the pywebview shell, deploy.py stands up the whole cloud stack in 15 idempotent boto3 stages (and can audit or destroy it), and training.py turns a local YOLO folder into a trained, wired-in Rekognition Custom Labels model.*

**1) main() — the shell**  `deployer/app.py:701-731`

> The entry point is one pywebview window on Edge WebView2 loading web/index.html. The js_api argument on line 710 is what creates window.pywebview.api in the page — it was missing until 15 July, so the shipped exe silently ran the simulated preview with every button dead. storage_path pins the WebView2 profile to %LOCALAPPDATA%\ARGUS so the embedded AWS console session survives relaunches, and private_mode=False is required or the profile is never written.
>
> *Why:* One window, one exe. The profile lives per-user, never in the project tree, so console cookies can never ride along in a zip of the repo.

**2) Api.verify_credentials — root refusal + admin preflight**  `deployer/app.py:85-134`

> The pasted key is validated with sts:GetCallerIdentity. If the ARN ends in ':root' it is refused outright before anything is stored — root keys can't be permission-scoped or rotated safely. Then a preflight: a bare IAM user passes STS but dies with AccessDenied eight stages into a deploy, so if the ARN contains ':user/' I check for AdministratorAccess directly and through the user's groups. On success the key goes into Windows Credential Manager via keyring, DPAPI-encrypted; the secret is never logged and never returned to the UI.
>
> *Why:* Fail at the keys screen with a named fix, not mid-deploy with a cryptic AccessDenied.

**3) open_aws_pane — the trust boundary**  `deployer/app.py:153-194`

> Card numbers and AWS passwords are typed into AWS's own pages in a separate embedded window with no JS bridge. I never read its DOM — line 190 shows the only thing I observe is the navigation URL, to advance the guidance rail. Also note line 165: the keys target is the IAM create-user page, deliberately not #/security_credentials, because that page nudges a signed-in root user toward creating root keys — the exact thing we refuse.
>
> *Why:* PCI-style boundary: sensitive input goes to AWS directly, ARGUS only watches where the browser navigated.

**4) Ctx — the deployment context**  `deployer/deploy.py:210-259`

> Ctx resolves everything a stage needs: a boto3 session, the account from STS, region, prefix, emails, and the state dict loaded from deploy_state.json. There are two constructors with identical post-conditions: the CLI path uses a named profile, the app path (from_params) uses the pasted key. Buckets are named {prefix}-{kind}-{account}; everything else is fixed pest-* names, so the prefix only parameterizes the globally-namespaced resources.
>
> *Why:* Single-sourcing: training.py reuses the same Ctx, so naming and state never fork.

**5) wait_for + already_exists — what makes the stages idempotent**  `deployer/deploy.py:301-319`

> wait_for polls a predicate 20 times at 3-second intervals, swallowing ClientError between tries — that absorbs IAM eventual consistency. already_exists is a whitelist of seven 'already there' error codes across services. This one function is why every stage adopts existing resources instead of failing: resume is literally just 'run the plan again' and completed stages fly through as adoptions.
>
> *Why:* Idempotency by adoption, not by tracking — the account itself is the source of truth.

**6) run_plan — the stage driver and the stamp rules**  `deployer/deploy.py:168-207`

> run_plan drives the (name, fn) stage list and emits a structured event per stage for the UI. Lines 178-180: before the first stage it pops the previous run's completed_at and last_stage stamps — otherwise a re-deploy that dies mid-run still reads as complete and the app resumes to the wrong deployment's summary. After each stage it stamps last_stage, so a later launch can tell 'never ran' from 'died at stage N' from 'completed'. The real production run through this engine: all 15 stages in 103 seconds on the fresh NP account, 10 August, zero errors.
>
> *Why:* The stamps are the resume protocol; the UI trusts them, so their write rules are strict.

**7) ROLE_MANAGED — the measured missing permissions**  `deployer/deploy.py:387-392`

> The audit only captured inline policies, and the inline documents alone are not the whole permission set. On a fresh deployment we measured GET /identities and GET /cost both returning 500 — 12 August — so the api role now also attaches AmazonSESFullAccess and AWSBillingReadOnlyAccess on top of the audited inline set.
>
> *Why:* We measured the 500s on a real fresh account, so the code attaches what the audit missed.

**8) stage_s3 — the public-access-block ordering trap**  `deployer/deploy.py:524-556`

> Three buckets, all SSE-S3. Frames and processed stay private with versioning and the full public-access block. The dashboard bucket is a public static website, and the order on lines 542-549 matters: the public-access block must be turned OFF first, then the public-read policy — PutBucketPolicy fails while the block is on. Also create_bucket skips LocationConstraint in us-east-1 because passing it there errors.
>
> *Why:* AWS rejects a public policy on a blocked bucket; the reverse order is a hard failure, not a race.

**9) LAMBDAS table — the measured processor sizing**  `deployer/deploy.py:74-87`

> Five functions. The processor is 1024 MB and 600 seconds because we measured one tiled frame plus up to 120 Bedrock verify crops at 24 to 54 seconds — the old 60-second timeout failed intermittently on real frames. The others are small: api 256/30, scheduler 128/60, hls and watchdog 128/30. The watchdog deploys from the repo mirror, which is the v6.2 code that honours per-camera max_runtime_min; the audit snapshot only knew the global 75-minute cap.
>
> *Why:* We measured 24-54 s per real frame, so the code ships 600 s, not the default 60.

**10) lambda_env — the validated detection tuning**  `deployer/deploy.py:619-649`

> The processor's environment is the validated production configuration, not code defaults: tile candidate floor 8, verify every box with Sonnet 4.6, max 120 boxes, 300 tokens, 3 workers, crop pad 0.6, then post-gate NMS at IoU 0.1 and containment 0.1, a 5 percent area cap, and display floor 49. Without this block a fresh stack comes up in the retired trust-Rekognition mode on Haiku — it deploys 'working' but is not the system we evaluated.
>
> *Why:* Found in the pre-migration audit on 10 August: a green deploy that silently runs the wrong architecture.

**11) stage_lambda — creation order and the IAM race**  `deployer/deploy.py:672-717`

> Deploy order is fixed: pest-camera-scheduler first because the api's environment references its ARN. Each function either creates, or on 'already exists' updates code then configuration. The subtle bit is line 708: a fresh role isn't immediately assumable, so create_function throws InvalidParameterValueException — we treat that as 'retry', wrapped in wait_for's 20-by-3-second poll.
>
> *Why:* IAM propagation is eventually consistent; retrying create is the documented AWS pattern.

**12) stage_s3_notification — permission before notification**  `deployer/deploy.py:723-747`

> This is the classic S3-to-Lambda ordering trap: lambda add_permission for s3.amazonaws.com goes FIRST, then put_bucket_notification_configuration — the notification put fails validation without the permission. The trigger is scoped to ObjectCreated under the frames/ prefix, which is also what keeps training-data uploads to the same bucket from starting detection runs.
>
> *Why:* S3 validates it can invoke the target at notification-put time, so the reverse order is a hard fail.

**13) stage_apigw — 21 routes, one deliberate unauthenticated route**  `deployer/deploy.py:797-891`

> HTTP API, JWT authorizer bound to the Cognito pool and client, two AWS_PROXY integrations, and the 21 routes from the table at the top of the file. Auth is a column in that table, so the single unauthenticated route — GET /stream/status, for device pollers — is one visible line, line 110. On resume, existing routes go through update_route, and line 856's comment records the fix: update_route requires ApiId and only RouteKey stripped; stripping both made every resume fail on the first pre-existing route until 16 July.
>
> *Why:* Declaring auth per-route in data makes the one exception auditable at a glance.

**14) stage_seed — the two fields that decide whether detection works**  `deployer/deploy.py:1040-1092`

> Seeds the config row and two camera rows: manual_upload for the dashboard test panel and camera-1 as the template. The comment on lines 1052-1059 is the heart of it: llm_verify_enabled is per-camera opt-in, so left unset the verification gate never runs; and min_confidence 10 is the CANDIDATE floor feeding the gate, not the display threshold — the user-facing threshold is post_verify_floor 49. camera-1 also gets max_runtime_min 45 so the watchdog auto-stops an unattended model start.
>
> *Why:* The worst pre-migration gap: a fresh stack where the entire LLM verification layer silently never ran.

**15) destroy — reverse-dependency teardown**  `deployer/deploy.py:1127-1295`

> Teardown runs in reverse dependency order: schedule, Rekognition — stop RUNNING versions, wait out STOPPING up to 30 polls of 10 seconds, delete versions, delete project — KVS, CloudFront, API Gateway, Cognito, the 5 Lambdas, every layer version, the 3 buckets emptied page by page, the 4 tables, the 6 roles with inline policies deleted and managed detached first, then SES. Every step is best-effort: not-found errors count as 'already gone', so a partial stack tears down cleanly. Two rails: line 1138 refuses if the state file's account differs from the credentials' account, and at the end the state file is renamed destroyed_{timestamp}.json — archived, not deleted.
>
> *Why:* A teardown that aborts halfway leaves the worst of both worlds; best-effort plus a final audit is safer than fail-fast here.

**16) the CloudFront disable-wait-delete dance inside destroy**  `deployer/deploy.py:1199-1214`

> CloudFront cannot be deleted while enabled. So: fetch the config, flip Enabled to false, update with the ETag, then wait for the distribution_deployed waiter — 30-second delay, up to 40 attempts, so up to 20 minutes — then re-fetch for a fresh ETag and delete. The re-fetch matters: the update changed the ETag and delete requires the current one.
>
> *Why:* This is an AWS-imposed three-step; skipping the wait gets DistributionNotDisabled.

**17) verify — the self-audit, and 0-of-N as proof of absence**  `deployer/deploy.py:1298-1363`

> verify is read-only: for every id in the state file it makes the matching describe or head call and returns per-resource ok/detail. It also checks the four tables and the watchdog schedule, which the stages never wrote into state. It's used twice: after deploy, N-of-N present proves the deployment is real; and after destroy the app re-runs the same audit on the still-in-memory state — app.py lines 442-446 — where success now means 0 of N remain, with leftovers named.
>
> *Why:* Destroy's own step results are best-effort claims; proof of absence comes from the same audit that proved presence.

**18) destroy_deployment — the three guards**  `deployer/app.py:401-453`

> Teardown from the UI has three guards: it refuses while a deploy is running, refuses while training is active — destroy would delete the Rekognition project under the trainer, and the training thread's next whole-dict save would resurrect the archived state file — and requires the deployment prefix typed exactly. Only then does deploy.destroy run in a thread, followed by the verify-as-proof-of-absence pass.
>
> *Why:* Irreversible action, so confirmation is typed, not clicked, and concurrent mutators are locked out.

**19) start_training — the atomic flag claim**  `deployer/app.py:519-562`

> Lines 524-530: the _training flag is claimed under a lock BEFORE any slow work, because the STS call inside Ctx.from_params takes long enough for a double-click to race the old check-then-set and launch two whole pipelines. Every early-return path resets the flag. Deployment and training are mutually exclusive by these two flags.
>
> *Why:* Classic TOCTOU on a UI button; the lock plus claim-first closes it.

**20) analyze — local fail-fast validation**  `deployer/training.py:219-318`

> analyze is local and fast: no AWS, no image decoding, label text only. It auto-detects three layouts — yolov8, flat, roboflow — and reports per-class counts, warnings, and hard blockers. The key blocker is lines 264-269 and 293-299: YOLO coordinates are normalized 0 to 1, so any value over 1.5 means pixel-unit labels; if more than half the boxes are out of range the whole set is blocked with 're-export with normalized coordinates', instead of letting them clamp to zero-size boxes deep in convert. Zero-box images are excluded and reported — Rekognition object detection ignores them, our v6 negatives lesson.
>
> *Why:* Catch bad exports in seconds at the picker, not an hour later mid-upload.

**21) plan_for_class — the cost gate math**  `deployer/training.py:321-352`

> Pure function feeding the cost gate. If the folder has an explicit test split of at least 10 images it's honored; otherwise a test set of max(10, 15 percent) is carved. Blocked below 10 train or 10 test images — Rekognition's minimum. Cost estimate: est_hours = max(1.0, 0.8 + train_n/400) at 4 US dollars per training hour — calibrated on our reference run, 1,148 images to 3.5 billable hours. Deliberately uncapped: a 10,000-image set really does train for a day and the gate must say so.
>
> *Why:* We measured 1,148 images at 3.5 h, so the estimator is anchored to that, not a guess.

**22) _convert — YOLO to Ground Truth, dedupe, downscale**  `deployer/training.py:368-512`

> Walks every image-label pair keeping only the chosen class. Three behaviors matter. Dedupe by stem against everything already in the project's datasets, excluding this version's own prefix so a retry replaces its own upload — re-picking an old folder can't duplicate entries or leak former train images into test. Split assignment mirrors the plan deterministically. And any image over 4096 pixels on an edge — Custom Labels rejects those — is LANCZOS-downscaled and re-encoded JPEG quality 90; box pixels are computed from the FINAL saved dimensions, and since YOLO boxes are normalized the downscale is lossless for labels. Boxes under 1 pixel are dropped, and both splits must still have 10-plus images or it fails with a pointer to the warnings.
>
> *Why:* The converter math is the production-proven convert_to_manifest.py logic — models v3 through v6 all trained through it.

**23) _load_datasets + _wait_dataset — chunked appends with a freshness guard**  `deployer/training.py:606-644`

> First run creates TRAIN and TEST datasets from the manifests; iteration appends with update_dataset_entries, chunked at 4 MB per call under the API's 5 MB cap. The freshness guard in _wait_dataset, line 583, is the subtle bug it prevents: a multi-chunk append could read the PREVIOUS chunk's UPDATE_COMPLETE and fire the next chunk into a dataset still ingesting — so a terminal status recorded before our own submission timestamp is ignored. Dataset error entries are reported but non-fatal; the engine just skips those images.
>
> *Why:* The dataset status is shared mutable state; without the timestamp check, success from the last write reads as success for this one.

**24) run_training — the orchestrator, and why n persists at start**  `deployer/training.py:912-985`

> The pipeline is validate, convert, upload, datasets, train, then watch and wire. Line 947: the version number n is persisted at RUN START, not on success — a retry after any failure re-uses the same n, so the same images land on the same S3 keys and REPLACE their dataset entries instead of duplicating, because update_dataset_entries matches on source-ref. A pre-submit failure clears the attempt stamp and records an honest 'INCOMPLETE' failure; after submit the durable train:pending record means the cloud continues without the app.
>
> *Why:* Retry-safety comes from key stability, so the counter must survive the failure it exists to absorb.

**25) watch_and_wire — the tolerant poll loop**  `deployer/training.py:720-779`

> Polls describe_project_versions every 60 seconds for a run that takes 1 to 4 hours. Transient errors are tolerated up to 30 consecutive failures — about 30 minutes — before giving up with 'training continues in the cloud, re-attach from the Train screen'; train:pending survives the give-up. Status handling is deliberate: TRAINING_COMPLETED, STARTING, RUNNING, STOPPING and STOPPED all mean training SUCCEEDED — a model that finished while the app was closed may already have been started or stopped from the dashboard. Only TRAINING_FAILED, FAILED and DELETING are dead.
>
> *Why:* One network blip in a 4-hour watch must never be reported as a training failure.

**26) _wire_in — the step that makes the model real**  `deployer/training.py:782-821`

> A trained but unwired model is useless, so this scans the cameras table and sets custom_model_arn on every row with model_type custom — boto3 only, because model ARNs contain colons that shell quoting mangles. It records the version's ARN, F1 and billable seconds under train:v{n}, saves the previous ARN as the rollback target, and bumps next_n. If NO camera row is custom it warns loudly instead of pretending success. rollback() right below is the same scan pointed at the previous ARN — it codifies our v6-to-v5 hand-rollback of 15 July, and the swap is reversible.
>
> *Why:* The dashboard reads the camera row, not the project, so wiring is the actual deliverable of training.

**Looks wrong, is deliberate:**
- deploy.py:415-417 — a .replace() with identical old and new strings, which looks like a copy-paste bug. Defence: the audited reference account had a latent bug where the api role's iam:PassRole pointed at a scheduler-invocation role that did not exist; the REAL fix is that stage_iam creates a role with exactly that stable name (line 443), which makes the audited document correct as-is. The replace became a no-op once the names converged and was left as a marker with the comment. If pressed: 'the fix is the role creation below, the replace is vestigial and provably harmless.'
- deploy.py:129-134 and app.py:75-82 — _emit and _push swallow every exception silently. Deliberate fail-open: a UI callback crash must never kill a deploy stage mid-mutation. The deploy's own failure path is die()/exceptions, which are separate.
- deploy.py:605-613 — stage_layer publishes a NEW layer version on every run instead of adopting, unlike every other stage. Deliberate: layer versions are immutable and cost nothing, adoption would require content-hash comparison, and destroy deletes ALL versions (deploy.py:1237-1242).
- deploy.py:1069-1091 — stage_seed's put_item overwrites on every re-run, so a resume resets camera settings a customer may have edited. Accepted: the seeds ARE the validated baseline, resume is a recovery action, and the manual documents 're-running resets the seeds'.
- deploy.py:1064 — min_confidence seeded at 10 looks dangerously low for a detection threshold. It is not the display threshold: it is the candidate floor feeding the LLM verify gate (the LLM does the rejecting); the user-facing threshold is post_verify_floor 49. Setting it high silently strangles recall before the LLM ever sees a box — that exact trap (seeded 30) was one of the three deploy-breaking gaps fixed 2026-08-10.
- deploy.py:1152-1154 — destroy treats ValidationException as a benign 'already gone' code, which looks like swallowing real errors. Defence: some services return ValidationException for operations on ids that no longer resolve; in a best-effort teardown a false 'gone' is caught anyway by the post-destroy verify pass, which is the real arbiter (0-of-N).
- deploy.py:1138-1141 vs main() at 1410-1414 — the destroy account-match guard reads deploy:account, but the CLI's main() loop never writes it (only run_plan does). So a CLI-deployed state file skips this guard silently. Known, documented gap; the typed-prefix confirmation and best-effort semantics still apply, and the production deploy was done via CLI knowingly.
- app.py:640-660 — on_closing blocks the FIRST close during un-submitted training but honors the SECOND. Looks like a broken guard; it is a deliberate two-strike design: the user has been told closing cancels the upload, and an app that refuses to close is worse. Once train:pending is on disk, closing is always safe — the cloud continues alone.
- app.py:289-313 — verify_deployment ignores the region/prefix the UI passes in and trusts the state file. Deliberate source-of-truth rule: the UI's saved config can drift, and a healthy ap-southeast-1 stack audited in us-east-1 reads as completely broken. Older state files recover the region from the saved API Gateway URL.
- training.py:663-692 — _existing_stems returns whatever it gathered on a listing failure instead of raising. Deliberate fail-open on the dedupe: the worst case is a duplicate entry that update_dataset_entries collapses by source-ref anyway, while aborting a paid training run over a transient list call would be worse.
- training.py:78-80 — STOPPED and RUNNING both counted as 'training succeeded' looks like conflated states. Deliberate: those are ENDPOINT lifecycle states after training completed; a model that finished overnight may already have been started or stopped from the dashboard, and treating STOPPED as failure would mis-handle exactly the close-the-app-and-come-back flow we support.
- deploy.py:868-871 — create_stage for $default is wrapped in try/except-already-exists with no adoption lookup, unlike other stages. The stage name is fixed and there is nothing to adopt or update on it: AutoDeploy is its only property. Conflict simply means it is already right.

**If she opens these, say "archived / not the live path":**
- deployer/audit/pest-model-watchdog_src/ and audit/pest-model-watchdog.zip — the pre-v6.2 watchdog snapshot from the reference-account audit. NOT deployed: deploy.py:86 sources the watchdog from the repo mirror lambda/pest-model-watchdog.py (v6.2, honours per-camera max_runtime_min). Say: 'that is the audit snapshot; the live source is the repo mirror.'
- deployer/audit/*.json generally — read-only evidence captured from the live reference account 366356442579 (including files literally named DEAD-apis-evidence). Only the iam__*__inline__*.json policy documents and audit/kvs-hls-handler_src/ are consumed by deploy.py; the rest is provenance for STACK_MANIFEST.md, not code.
- app.py:22 docstring says 'Package: see build_exe.md' — that file does not exist. The real packaging references are deployer/build.ps1, build_installer.ps1, and README.md. Known doc-lag, also flagged in the manual (07_deployer.md).
- docs/deployer_training_pipeline.md — the 2026-07-16 design spec. It says ~US$1/hour and poll-every-5-minutes; the code bills at US$4/hour (training.py:350) and polls every 60 seconds (training.py:720). Code is ground truth where they differ — the spec is history.
- deployer/out/destroyed_*.json (when present) — archives created by destroy renaming the state file (deploy.py:1285-1290). Records of dead deployments, never read by any code path; the live file is deploy_state.json only.
- deployer/build/, deployer/dist/, ARGUS.spec, __pycache__/ — PyInstaller build artifacts and the generated spec. Regenerated by build.ps1; nothing hand-maintained in there.
- web/index.html's simulated preview mode — when window.pywebview.api is absent (e.g. opened in a plain browser) every action is fake by design. Not dead code, but a reviewer clicking around a browser preview is not exercising any of the three Python engines.
- REHEARSAL.md lists '4 tables' in the state-file checklist — deploy.py never writes table keys into deploy_state.json; the tables have fixed names and verify() checks them directly (deploy.py:1354-1357). Doc-lag, already noted in the manual.

---

## The numbers (do not misquote your own code)

| Constant | Value | Where | Means |
|---|---|---|---|
| MODEL_POLL_INTERVAL_MS | **5000** | `C:/FYP/web/dashboard_v4/js/config.js:15` | dashboard model-status poll period while custom models exist |
| SG_OFFSET_HOURS | **8** | `C:/FYP/web/dashboard_v4/js/config.js:14` | Singapore UTC+8 offset for display times |
| CACHE_MAX_BYTES | **400*1024*1024 (400 MB)** | `C:/FYP/web/dashboard_v4/js/config.js:20` | IndexedDB image cache cap (bumped 200->400 for 500-record gallery pages) |
| history fetch limit (ops stats) | **500** | `C:/FYP/web/dashboard_v4/js/settings.js:31` | detections pulled to compute per-camera last-detection/today-count |
| schedule-logs fetch count | **30** | `C:/FYP/web/dashboard_v4/js/settings.js:94` | log rows the Settings tab requests |
| Sonnet 4.6 UI price | **"$20 / 1M tokens"** | `C:/FYP/web/dashboard_v4/js/settings.js:160 (repeated :584)` | price baked into model-picker option text |
| Haiku 4.5 UI price | **"$5 / 1M tokens"** | `C:/FYP/web/dashboard_v4/js/settings.js:161 (repeated :585)` | price baked into model-picker option text |
| daily-cost estimate text | **"3 photos/day ≈ $5.40 Sonnet · $1.35 Haiku"** | `C:/FYP/web/dashboard_v4/js/settings.js:163` | hardcoded cost projection under the model picker |
| upload size cap | **20 MB (20*1024*1024)** | `C:/FYP/web/dashboard_v4/js/settings.js:251` | Test-upload file size rejection limit |
| Test-upload waypoint override | **manual_test__conf10__llm-<alias>** | `C:/FYP/web/dashboard_v4/js/settings.js:332` | forces candidate floor min_confidence=10 for dashboard test runs |
| waitForDetection timeout | **60000 ms** | `C:/FYP/web/dashboard_v4/js/settings.js:370,385` | max wait for the detection record after a test upload |
| waitForDetection pollInterval | **3000 ms** | `C:/FYP/web/dashboard_v4/js/settings.js:388` | /history poll cadence while waiting for a test-upload result |
| AI filter threshold UI fallback | **33 (cam.post_verify_floor ?? 33)** | `C:/FYP/web/dashboard_v4/js/settings.js:572` | default shown in the per-camera threshold knob when DDB has no value |
| endpoint cost UI text | **"$4/hr"** | `C:/FYP/web/dashboard_v4/js/settings.js:607` | Rekognition endpoint hourly cost quoted in the schedule empty-state |
| schedule default start_time | **'05:40'** | `C:/FYP/web/dashboard_v4/js/settings.js:641` | prefilled camera schedule start time |
| TILE_COLS / TILE_ROWS | **4 / 4** | `C:/FYP/lambda/pest-detection-processor.py:156-157` | tiling grid size (default) |
| TILE_OVERLAP | **0.15** | `C:/FYP/lambda/pest-detection-processor.py:158` | tile overlap as fraction of base tile |
| TILE_UPSCALE_LONG_EDGE | **1920** | `C:/FYP/lambda/pest-detection-processor.py:159` | per-tile upscale target (zoom knob) |
| TILE_MIN_CONFIDENCE (code default) | **30** | `C:/FYP/lambda/pest-detection-processor.py:161` | per-tile candidate gather floor |
| TILE_NMS_IOU | **0.5** | `C:/FYP/lambda/pest-detection-processor.py:162` | IoU threshold for de-duplicating tile detections |
| TILE_MAX_WORKERS | **4** | `C:/FYP/lambda/pest-detection-processor.py:163` | parallel tile-detect threads |
| SUPPRESS_MIN_CONF | **55** | `C:/FYP/lambda/pest-detection-processor.py:171` | DetectLabels floor for non-vegetation FP suppression regions |
| SUPPRESS_COVERAGE | **0.5** | `C:/FYP/lambda/pest-detection-processor.py:172` | fraction of worm box inside a hard-object region to suppress |
| LLM_VERIFY_MODEL_ID (code default) | **us.anthropic.claude-haiku-4-5-20251001-v1:0** | `C:/FYP/lambda/pest-detection-processor.py:193-194` | default Bedrock verify model |
| LLM_VERIFY_MAX_BOXES (code default) | **5** | `C:/FYP/lambda/pest-detection-processor.py:260` | per-frame cap on boxes sent to the LLM (cost/latency) |
| LLM_VERIFY_PAD | **0.6** | `C:/FYP/lambda/pest-detection-processor.py:261` | crop padding as fraction of box size |
| LLM_VERIFY_LONG_EDGE | **672** | `C:/FYP/lambda/pest-detection-processor.py:262` | crop upscale target long edge for the verify crop |
| LLM_VERIFY_MIN_CONTEXT_PX | **32** | `C:/FYP/lambda/pest-detection-processor.py:267` | padding floor in px so tiny boxes keep real context |
| LLM_VERIFY_MAX_UPSCALE | **8.0** | `C:/FYP/lambda/pest-detection-processor.py:268` | upscale cap so tiny crops aren't pure interpolation |
| LLM_VERIFY_WORKERS (code default) | **4** | `C:/FYP/lambda/pest-detection-processor.py:269` | parallel Bedrock verify threads |
| LLM_VERIFY_TIMEOUT | **12 s** | `C:/FYP/lambda/pest-detection-processor.py:270` | per-Bedrock-call read timeout (also used at :881) |
| LLM_VERIFY_MAX_TOKENS (code default) | **100** | `C:/FYP/lambda/pest-detection-processor.py:277` | output token cap per verdict |
| LLM_VERIFY_ALL_BOXES (code default) | **false** | `C:/FYP/lambda/pest-detection-processor.py:293` | denoiser mode off by default (authority rule stands) |
| POST_NMS_IOU (code default) | **0 (off); comment: measured best 0.3** | `C:/FYP/lambda/pest-detection-processor.py:337 (comment :336)` | post-gate NMS IoU threshold |
| POST_VERIFY_FLOOR (code default) | **0 (off); comment: measured best 15** | `C:/FYP/lambda/pest-detection-processor.py:338 (comment :336)` | display floor applied AFTER the LLM verdict |
| POST_MAX_BOX_AREA (code default) | **0 (off)** | `C:/FYP/lambda/pest-detection-processor.py:345` | box-area sanity cap as frame fraction; largest real worm 4.39% |
| POST_NMS_CONTAIN (code default) | **0 (off)** | `C:/FYP/lambda/pest-detection-processor.py:353` | containment suppression threshold (small box inside big) |
| fallback camera min_confidence | **80** | `C:/FYP/lambda/pest-detection-processor.py:420 and :1344` | last-resort default when camera row missing / field absent |
| _encode_jpeg quality | **90** | `C:/FYP/lambda/pest-detection-processor.py:573` | JPEG quality for tiles and crops |
| EXIF rewrite quality | **95** | `C:/FYP/lambda/pest-detection-processor.py:1388` | JPEG quality when baking EXIF rotation back to S3 |
| FULL_FRAME_MAX_EDGE | **4000** | `C:/FYP/lambda/pest-detection-processor.py:583` | downscale ceiling before Rekognition full-frame pass |
| FULL_FRAME_MAX_BYTES | **4*1024*1024 (4 MB)** | `C:/FYP/lambda/pest-detection-processor.py:584-585` | byte cap for the full-frame pass; halves until under (min edge 640, :600) |
| detect_whole_frame MinConfidence | **30 (hardcoded)** | `C:/FYP/lambda/pest-detection-processor.py:622 and :634` | candidate floor on the NON-tiled fallback path |
| Rekognition throttle retry | **3 attempts, 0.5*(n+1)s backoff** | `C:/FYP/lambda/pest-detection-processor.py:658,670` | per-tile detect_custom_labels retry policy |
| DetectLabels MaxLabels | **50 @ MinConfidence=SUPPRESS_MIN_CONF** | `C:/FYP/lambda/pest-detection-processor.py:797` | suppression-region label scan size |
| Bedrock client config | **connect_timeout=5, retries max_attempts=5 adaptive** | `C:/FYP/lambda/pest-detection-processor.py:882,888-889` | Bedrock runtime client resilience settings |
| __confN override range | **10 <= N <= 100** | `C:/FYP/lambda/pest-detection-processor.py:1358` | accepted per-run min_confidence override window |
| processor sizing (doc) | **512 MB / 180 s (docstring)** | `C:/FYP/lambda/pest-detection-processor.py:116,119` | documented recommended Lambda sizing |
| MAX_RUNTIME_MIN (watchdog code default) | **60** | `C:/FYP/lambda/pest-model-watchdog.py:33` | global auto-stop window for a running Rekognition model |
| watchdog cadence + price note | **every 10 min; "~$1/hr"** | `C:/FYP/lambda/pest-model-watchdog.py:6,17` | EventBridge poll interval and quoted endpoint cost |
| per-camera max_runtime_min override | **cam.max_runtime_min, else MAX_RUNTIME_MIN** | `C:/FYP/lambda/pest-model-watchdog.py:127` | dashboard-set per-camera runtime cap (comment cites ~45 min use) |
| MinInferenceUnits | **1** | `C:/FYP/lambda/pest-camera-scheduler.py:139 (also pest-monitoring-api.py:295)` | Rekognition endpoint capacity on start |
| presigned URL expiry | **3600 s** | `C:/FYP/lambda/pest-monitoring-api.py:380,393` | S3 presigned POST/GET lifetime |
| /history limit | **default 100, clamp 1..500** | `C:/FYP/lambda/pest-monitoring-api.py:582` | history query page size |
| /cost defaults | **days=30; DAILY if <=60 else MONTHLY** | `C:/FYP/lambda/pest-monitoring-api.py:645,650` | Cost Explorer window and granularity |
| schedule SG->UTC shift | **hh -= 8** | `C:/FYP/lambda/pest-monitoring-api.py:814` | schedule time conversion (mirror of SG_OFFSET_HOURS) |
| /schedule-logs limit | **default 50, max 500** | `C:/FYP/lambda/pest-monitoring-api.py:941` | schedule-log query cap |
| v4 capture interval fallback | **60 s** | `C:/FYP/minipc/capture_and_upload_v4_armyworm.py:102` | loop interval when system-config has none |
| v4 JPEG_QUALITY | **90** | `C:/FYP/minipc/capture_and_upload_v4_armyworm.py:107` | frame encode quality |
| v4 MIN_INTERVAL | **5 s** | `C:/FYP/minipc/capture_and_upload_v4_armyworm.py:108 (applied :147,:322)` | safety floor on poll/capture rate |
| v4 RTSP capture attempts | **3 (flush 2 frames, 1 s sleep between)** | `C:/FYP/minipc/capture_and_upload_v4_armyworm.py:160,174,177` | RTSP open/grab retry policy |
| POLL_INTERVAL_SEC | **5** | `C:/FYP/minipc/kvs_controller.py:44` | /stream/status control poll cadence |
| API poll HTTP timeout | **10 s** | `C:/FYP/minipc/kvs_controller.py:91` | urlopen timeout on the status poll |
| x264 encoder settings | **key-int-max=45, bitrate=2000000, zerolatency/cbr/superfast** | `C:/FYP/minipc/kvs_controller.py:112-113` | KVS pipeline encode parameters |
| kvssink settings | **storage-size=512, fragment-duration=2, max-latency=2** | `C:/FYP/minipc/kvs_controller.py:117,120,121` | KVS sink buffer/fragment tuning |
| GStreamer stop waits | **SIGTERM wait 5 s, SIGKILL wait 2 s** | `C:/FYP/minipc/kvs_controller.py:159,164` | process shutdown escalation |
| NUDGE_DELTA_YAW / NUDGE_SETTLE_S | **0.30 rad / 8.0 s** | `C:/FYP/robot/go2_patrol_gated.py:123-124` | pre-nav in-place rotate to wake localization |
| NAV_START_ATTEMPTS | **2** | `C:/FYP/robot/go2_patrol_gated.py:125` | navigation/start retry count |
| START_COUNTDOWN_S | **3** | `C:/FYP/robot/go2_patrol_gated.py:149` | clear-area / unplug-cable countdown |
| GETMAP_TIMEOUT_S | **8** | `C:/FYP/robot/go2_patrol_gated.py:150` | max wait for get_map_id reply |
| LOCALIZE_TIMEOUT_S / LOCALIZE_ATTEMPTS | **30 s / 4** | `C:/FYP/robot/go2_patrol_gated.py:151-152` | localization init wait and retries (~60% success per try) |
| NAV_START_TIMEOUT_S | **20** | `C:/FYP/robot/go2_patrol_gated.py:154` | max wait for navigation/start ack |
| NAV_REACH_TIMEOUT_S | **90** | `C:/FYP/robot/go2_patrol_gated.py:155` | per-waypoint max wait for REACHED |
| GIMBAL_SETTLE_S / FOLLOW_SETTLE_S | **1.5 / 2.0 s** | `C:/FYP/robot/go2_patrol_gated.py:158-159` | gimbal settle delays before capture |
| CAPTURE_WARMUP | **30 frames** | `C:/FYP/robot/go2_patrol_gated.py:162` | RTSP frames discarded before the kept frame |
| DDB_GATE_TIMEOUT_S / DDB_GATE_POLL_S | **150 s / 1.5 s** | `C:/FYP/robot/go2_patrol_gated.py:163-164` | robot's wait for the detection record (fail-open) |
| Lambda sizing table | **processor 1024 MB/600 s; api 256/30; scheduler 128/60; kvs-hls 128/30; watchdog 128/30** | `C:/FYP/deployer/deploy.py:79-86` | deployed memory/timeout per Lambda (validated prod sizing) |
| PILLOW_VERSION | **12.2.0** | `C:/FYP/deployer/deploy.py:115` | pinned PIL layer version |
| wait_for polling | **tries=20, delay=3 s (60 s budget)** | `C:/FYP/deployer/deploy.py:301` | IAM eventual-consistency poll |
| prod env: TILE_MIN_CONFIDENCE | **"8"** | `C:/FYP/deployer/deploy.py:635` | deployed candidate gather floor (high-recall frontend) |
| prod env: LLM_VERIFY_ALL_BOXES | **"true"** | `C:/FYP/deployer/deploy.py:636` | deployed denoiser mode ON |
| prod env: LLM_VERIFY_MODEL_ID | **us.anthropic.claude-sonnet-4-6** | `C:/FYP/deployer/deploy.py:638` | deployed verify model |
| prod env: LLM_VERIFY_MAX_BOXES / MAX_TOKENS / WORKERS / PAD | **120 / 300 / 3 / 0.6** | `C:/FYP/deployer/deploy.py:639-642` | deployed verify-gate tuning |
| prod env: POST_NMS_IOU / POST_NMS_CONTAIN / POST_MAX_BOX_AREA / POST_VERIFY_FLOOR | **0.1 / 0.1 / 0.05 / 49** | `C:/FYP/deployer/deploy.py:646-649` | deployed post-gate cleanup (5% area cap, floor 49 = live decision 2026-08-10) |
| prod env: MAX_RUNTIME_MIN | **"75"** | `C:/FYP/deployer/deploy.py:661` | deployed watchdog global runtime cap |

**Same concept, different values — know these before she finds them:**
- POST_VERIFY_FLOOR (the display threshold) has FOUR values: code default 0/off (processor:338), code comment 'measured best 15' (processor:336), dashboard UI fallback 33 (settings.js:572 `cam.post_verify_floor ?? 33`), deployer prod env '49' (deploy.py:649). 49 is the decided live value; the UI's 33 fallback silently misrepresents a camera whose DDB row lacks the field.
- TILE_MIN_CONFIDENCE: code default 30 (processor:161) vs deployer '8' (deploy.py:635). A stack run from code defaults gathers candidates at 30 and starves the LLM denoiser that prod tuned for an 8-floor stream.
- Whole-frame fallback floor is HARDCODED 30 (processor:622 and :634, detect_whole_frame) while the tiled path uses TILE_MIN_CONFIDENCE (8 in prod). When tiling errors and the code 'falls back to a single call' (handler ~:1419), the candidate floor silently jumps 8->30 on exactly the frames that already had a problem.
- LLM_VERIFY_MODEL_ID: code default Haiku 4.5 (processor:193) vs deployer Sonnet 4.6 (deploy.py:638) vs dashboard default alias 'sonnet46' (settings.js:160,584). Code-default deployment runs the architecture on the cheaper model the project measured and moved past.
- LLM_VERIFY_MAX_BOXES: 5 in code (processor:260) vs 120 in deployer (deploy.py:639). In denoiser mode, boxes over the cap are DROPPED unjudged (processor:1167-1188), so the code default of 5 would delete nearly every tiled candidate.
- LLM_VERIFY_MAX_TOKENS raised 100->300 in deployer (deploy.py:640) but LLM_VERIFY_TIMEOUT stays at the code default 12 s (processor:270) - the code's own comment (processor:276) says to raise the timeout alongside the token cap for thinking models.
- LLM_VERIFY_WORKERS: 4 in code (processor:269) vs 3 in deployer (deploy.py:641) - minor, but a reviewer will ask which number the latency claims were measured at.
- LLM_VERIFY_ALL_BOXES: code default false (processor:293) vs deployer 'true' (deploy.py:636) - default-config behaviour is the retired v4.5 authority mode, not the shipped denoiser architecture.
- POST_NMS_IOU: comment says measured best 0.3 (processor:336) but deployer ships 0.1 (deploy.py:646); code default is 0/off (processor:337). Three different numbers for one knob.
- MAX_RUNTIME_MIN: watchdog code default 60 (pest-model-watchdog.py:33 and docstring :21) vs deployer '75' (deploy.py:661); deploy.py's own comment (:84) and the watchdog comment (:124) both talk about a ~45-min per-camera window. 45/60/75 all appear for the same concept.
- Processor Lambda sizing: docstring says '512 MB recommended, 180 s timeout' (processor:116,119) vs deployer's validated 1024 MB / 600 s (deploy.py:75-79). The in-file doc is stale against the deployed truth.
- Rekognition endpoint hourly price: watchdog docstring '~$1/hr' (pest-model-watchdog.py:6) vs dashboard '$4/hr' (settings.js:607). Same endpoint, 4x apart in two user-facing places.
- Detection-record wait budget: dashboard polls every 3 s up to 60 s (settings.js:385-391, UI text 'typically 5-15 s') vs robot gate polls every 1.5 s up to 150 s (go2_patrol_gated.py:163-164), while deploy.py:77 states a tiled+verified frame measures 24-54 s. The dashboard's 60 s budget is tight against its own measured upper bound; the two consumers disagree on how long the same pipeline is allowed to take.
- Bedrock retry comment cites 'a 12x12 sweep fires 144 calls per frame' (processor:883-884) but the tiling grid is 4x4 (processor:156-157) - stale comment from an experiment that no longer exists in v6.3.
- JPEG quality: capture and tiles/crops use 90 (minipc v4:107, minipc v3:31, processor:573) but the EXIF-normalisation rewrite re-encodes the frame at 95 (processor:1388) - two quality levels touching the same frame bytes.
- Config-poll interval floor: minipc capture clamps to MIN_INTERVAL=5 s (v4:108) and v3 clamps inline to max(5,...) (v3:48) - same rule, one named constant and one magic number across the two generations of the script.

---

## Likely questions (25, answers verified against code)

### Lambda processor — fail-open

**Q: Your LLM gate is documented as 'fails OPEN at every level'. Why would a detection system keep a box nobody verified?**

That "fails open at every level" line is from the old v4.4/v4.5 write-up, before the denoiser mode; the live processor runs the opposite. On worm_cam — the only camera with llm_verify_enabled=true, since the gate is per-camera opt-in (pest-detection-processor.py:1466) — LLM_VERIFY_ALL_BOXES is true, so every target box is judged and a box survives only on an explicit positive verdict: rejected boxes and un-judged boxes, whether from the 120-box cap, a crop failure or a Bedrock throttle, are both dropped, fail-closed (:1162-1190). That is deliberate, because tiling gathers down to 8% and emits high-confidence noise that would otherwise ride straight through to the dashboard. The one place it still fails open is total gate failure — candidates existed and zero verdicts came back — which means the model is unreachable, not that the frame is clean, so the code keeps the boxes, falls back to plain min_confidence thresholding and logs loudly (:1146-1161). That branch was added on 2026-07-26 after a bad model id produced 492 AccessDenied calls and 44 of 44 images reporting zero detections (:229) — a monitoring system going blind must not quietly report a pest-free garden.


### Lambda processor — Bedrock timeout

**Q: What happens if Bedrock times out mid-frame?**

Each Bedrock call gets a 12 second read timeout, 5 second connect timeout, and 5 retry attempts in adaptive mode, so a slow call is retried rather than fatal (pest-detection-processor.py:874-891). If it still fails, verify_one_crop returns None (:1043-1044), and in denoiser mode that one box is dropped as un-judged, fail-closed (:1162-1190); only if every candidate comes back None does the total-gate-failure path fail open to plain min_confidence thresholding (:1146-1161). I should be straight about the limit: 12 seconds is per attempt, and there is no per-frame deadline check in the code, so the only hard bound is the Lambda's 600 s / 1024 MB. If the invocation hits that, the put_item at :1577 never runs and the frame gets no record at all. That has happened once, on a bulk backfill of 9 frames at about 129 crops each, which throttled Bedrock and timed out every Lambda on the then 180 second budget, which is why bulk uploads are now documented as serial.


### Lambda processor — tiling

**Q: Why 15% tile overlap and not 10%?**

Honest answer: 15% is an engineering margin, not the winner of a 15-versus-10 A/B — no overlap sweep exists anywhere in my logs. All it has to do is guarantee a target straddling a tile boundary still lands wholly inside the neighbouring tile; that is the docstring at pest-detection-processor.py:550-554, and on a 4x4 grid the margin is 15% of a quarter-frame, so roughly 3.75% of the frame width on every side — far larger than the small distant worms tiling exists to recover, and large targets are covered anyway by the separate full-frame pass (:160). Extra overlap costs no extra Rekognition calls — still 16 tiles plus the full frame, 17 passes (:156-160) — but it is not free: it creates duplicate boxes, and plain IoU NMS does not catch them all. Measured on live output, all five overlapping pairs escaped IoU-NMS even at 0.3, the worst at IoU 0.022 with the small box sitting 100% inside the large one, which is why the post-gate pass adds a containment test, running live at POST_NMS_IOU=0.1 and POST_NMS_CONTAIN=0.1 (:1262-1272; both default to 0 in code and are set by env). TILE_OVERLAP is an env var (:158), so the value can be re-swept without a code deploy.


### Lambda processor — threshold mismatch

**Q: Why is the live confidence floor 33 when the report's threshold study decided 49?**

Two accounts, two trainings of the model. 49 was decided on the old dev account off a measured ladder — Jewel noise boxes sit at 40 to 48 percent and the new worm hits sit at 34.9 to 39.4, so we took precision (docs/detection.md:55-62). Custom Labels models are account-bound, so moving to the NP production account meant a full retrain, and the new weights score the same photos differently — image 104 dropped from 77.2 percent to 14.3 (docs/state.md:665). At 49 that retrained model was throwing away real worms, so on 2026-08-13 we set the live floor to 33 on the camera row and the Lambda env, which is also the floor the curated gallery was produced at, so a live Test upload during the demo draws the same boxes as the stored records (docs/state.md:727-730). To be straight about it: the sweep on the new account recommended 30, and 33 was picked for gallery consistency; the report quotes only the old-account validated runs for the threshold study and states 33 as the live production value.


### Lambda processor — two thresholds

**Q: There are two confidence knobs, min_confidence=10 and post_verify_floor. Why two, and why is min_confidence so low?**

They sit on opposite sides of the LLM gate: min_confidence is the candidate floor a Rekognition box must clear to be sent for verification, and post_verify_floor is the display floor applied to the boxes the gate accepted — the docstring spells it out at C:\FYP\lambda\pest-detection-processor.py:1218-1228, and the per-camera field is whitelisted in pest-monitoring-api.py:104-106. min_confidence has to be low, it is 10 on worm_cam, because tiled boxes that land on real worms come back at 10-17% Rekognition confidence (batch_2 worms scored 17.6, 15.3, 10.2), so the gate is what rejects junk, not the number. The split came out of a real bug: the dashboard threshold knob used to write into min_confidence, so a "35" silently strangled the candidate stream before the LLM ever saw it (C:\FYP\docs\state.md:1716-1723). The floor works after the verdict and not before because among LLM-accepted boxes, true worms have median 27% confidence and junk 15.5% — that separation only exists post-gate (pest-detection-processor.py:1213-1217). Two caveats: the live display floor on worm_cam is 33, not the 49 in the report's threshold study, and there is actually a third floor upstream, TILE_MIN_CONFIDENCE=8 inside tiling, which min_confidence cannot recover a box from.


### Lambda processor — concurrency

**Q: What breaks if two images upload at once?**

Structurally nothing breaks. Each S3 PutObject is its own Lambda invocation, the per-record model holder is reset on every record so a warm container never carries the last image's model over (pest-detection-processor.py:205-208 and :1364), and the detections table uses a composite key of image_id plus detection_time, so a duplicate S3 event adds a row instead of overwriting one, and delete removes every row for that image_id (pest-monitoring-api.py:492-524). The real limit is Bedrock account throughput, and I hit it: 9 frames uploaded at once at about 129 crops each throttled the account and every Lambda timed out at the then 180 second budget (docs/state.md:1754-1759). Since then the Bedrock client runs adaptive rate limiting with 5 attempts (:877-890), per-tile Rekognition calls retry 3 times on throttle (:655-674), un-judged boxes are dropped rather than passed through, the Lambda is sized 1024 MB and 600 seconds, and bulk backfills are a documented serial upload.


### Architecture — LLM role

**Q: Why does the LLM only verify crops? Why not let it find the worms and skip Rekognition hosting costs?**

We measured it twice and letting the LLM find things fails. In the 2026-07-21 A/B on the same image set, Haiku judging crops got 13 of 14, Haiku on the whole frame got 6 of 14 and missed an obvious striped larva on bare concrete — that is an input-resolution limit, not model quality, because Bedrock's standard tier downscales to 1568px and a worm under about 0.5% of frame area survives as two or three visual patches (docs/detection.md:411-434). The controlled ladder repeated it with a stronger model: Sonnet 4.6 alone on whole frames found 5 of 33 worms, while the tiled Rekognition front end surfaced all 33 of 33 — but at roughly 110 false boxes at confidence 50, and cleaning that up is exactly what the LLM stage is for (detection.md:116-121). On cost, the Custom Labels endpoint is about $4 an hour, but the watchdog Lambda stops it automatically after the run window, so it is a bounded per-patrol cost rather than a standing one. So the split is measured, not aesthetic: Rekognition finds, the LLM verifies zoomed crops, and that is written into the pipeline at lambda/pest-detection-processor.py:75-77.


### Models — negatives limitation

**Q: Why is false-positive suppression bolted on in the Lambda instead of trained into the model?**

Rekognition Custom Labels object detection simply cannot train on negatives, and I proved that three ways on 2026-07-13: I appended 410 empty-annotation entries to the TRAIN dataset, they registered as is_labeled:false and LabeledEntries stayed at 1160, the AWS object-detection manifest spec requires a box per annotation, and AWS's own guidance says remove true-negative images (docs/detection.md:333-350). With zero negatives the model's learned prior is "plant close-up means armyworm", so suppression has to live in the application layer: a DetectLabels pass finds hard-object regions and I drop any custom box that is at least 50% covered by one, with plant, leaf, flower and similar labels protected so a worm sitting on a leaf is never touched (lambda/pest-detection-processor.py:170-184, the function at :835-857, called at :1438). The proof for that is one control case, not a fleet statistic: the 65% jeep-wheel box on cag_armyworm_103 was 94% inside a "wheel" region and got dropped, while the real worms in 104 and 105 were kept. What geometry cannot fix is plant-on-plant false positives, and I should be honest that the Sonnet 4.6 gate reduces those rather than removing them - on the four field-realistic images at the 49 floor, 9 surviving boxes were 4 true and 5 false, with the false ones at 62-84%, above the real worm in three of the four (docs/detection.md:82-88).


### Lambda processor — clean-frame records

**Q: Why does every frame write a DynamoDB record even when nothing is detected?**

The put_item at pest-detection-processor.py:1577 sits deliberately outside the target_detected check, and the comment right above it at line 1518 says why. The Go2 patrol gate polls DynamoDB by the S3 key after every upload (go2_patrol_gated.py:570), so if clean frames wrote nothing the robot would sit at each pest-free waypoint for the full 150-second timeout before failing open and walking on — writing every frame keeps the patrol at normal speed. The clean rows are also visible on the dashboard: the gallery has a "Clear only" filter backed by the API's target_detected filter. To be clear, the analytics page does not use them — it only counts rows where target_detected is true, at analytics.js:298.


### Lambda processor — EXIF rewrite

**Q: The EXIF fix rewrites the S3 object under the same key. Doesn't that retrigger this very Lambda — an infinite loop?**

Yes, it retriggers — the bucket notification is s3:ObjectCreated:* on the frames/ prefix (deployer/deploy.py:743), so the write-back fires the Lambda again. It stops after exactly one extra pass because the rewrite only happens when exif_transpose changes the image size (lambda/pest-detection-processor.py:1386), and the JPEG is written back with the rotation baked into the pixels and no orientation tag, so pass two measures the same size and does not write. What I understated in the draft is the cost: that second pass is a full re-run, so it pays for Rekognition and the Bedrock verify again, the put_item is unconditional so it writes a second DynamoDB row under a new sort key (line 1577) that shows as a second gallery card, and on a positive frame it sends a second SES alert (line 1598). We live with it because duplicate rows are already expected from at-least-once S3 events and the delete endpoint removes every row for an image_id (lambda/pest-monitoring-api.py:496). One real limit: only orientations that swap width and height are fixed — a 180-degree tag leaves the size unchanged, so that frame is never rewritten.


### API — history scan

**Q: GET /history does a full table scan with filter expressions. Why no GSI, and when does this fall over?**

The production detections table holds 64 records right now, so a scan is genuinely cheaper and simpler than an index at this size. /history builds one DynamoDB FilterExpression from up to seven filters, pages the scan and stops once ScannedCount passes 10,000, then sorts by detection_time and cuts to at most 500 items — that loop is pest-monitoring-api.py:615-630, and the limit clamp (default 100, max 500) is at :582. The table actually does carry a by-pest-time GSI, pest_type hash plus detection_time range, created at deployer/deploy.py:474-478, and the processor writes pest_type on every record (pest-detection-processor.py:1547), but no Lambda ever queries it, because one index cannot serve arbitrary combinations of camera, zone, pest_type, source, detected and a date range. It falls over in the tens of thousands: the 10,000 cap starts silently truncating results, and Analytics already asks for only 500 rows (dashboard_v4/js/analytics.js:69); the fix is a Query on that GSI by pest_type with a detection_time range and the rest left as a filter, contained inside handle_get_history.


### Security — unauthenticated route

**Q: Every route requires a Cognito JWT except GET /stream/status. Why is one route open?**

That route is the control signal for the field devices: the Orin and the mini PC both run kvs_controller.py, which does a plain HTTPS GET every 5 seconds to decide whether to start the GStreamer push (minipc/kvs_controller.py:88 — "no AWS credentials required for the control path"). Putting Cognito logins and a token-refresh loop on two headless boxes was more moving parts than the exposure was worth, so it is the one AuthorizationType NONE entry in the deployer's route table — deployer/deploy.py:110, the other 20 routes are JWT, including POST /stream/start and /stream/stop. What it returns is only a boolean stream_enabled, the kvs_stream_name and a status string (lambda/pest-monitoring-api.py:985) — no image data and no write path. One honest caveat: on the production account the deploy ran without --live-view, so no KVS streams exist, kvs_stream_name is empty and that route currently answers NOT_STREAMABLE. Re-gating it is one flag in deploy.py, or an update-route with authorizer enxa26 on the live API.


### Security — destructive endpoint

**Q: DELETE /detection deletes S3 objects. What stops it becoming an arbitrary S3 deleter?**

Three gates, all in handle_delete_detection at lambda/pest-monitoring-api.py:492-556. First it queries DynamoDB for that image_id and returns 404 with nothing touched if no record exists, so you cannot hand it a key you invented. Second, a frame is only deleted if the key starts with "frames/" (line 536), and IAM backs that up: the inline policy pest-monitoring-api-policy has a statement S3DeleteCapturesOnly allowing s3:DeleteObject only on arn:aws:s3:::argus-frames-506868652945/frames/*, so the training-data/ and training-output/ prefixes in the same bucket are unreachable even if the code check were bypassed. Third, the S3 deletes run before the DynamoDB rows, so an S3 failure returns 500 with the records intact and the call is safe to retry. The route is JWT-gated on production (route 0ah3h33, authorizer yjmys4); the E2E test — no token 401, training-asset key 404 with the object intact — was 2026-07-07 on the old account, and the emergency authorizer-detach runbook at docs/dashboard.md:226 still excludes the old route id glwyqo0, so I need to update that to 0ah3h33.


### Models — F1

**Q: The live armyworm model's F1 is 0.613. Why deploy a model that weak?**

F1 measures the detector on its own, and since v6.0 the detector is deliberately a high-recall front end - it tiles the frame and gathers candidates down to 8% confidence (TILE_MIN_CONFIDENCE=8, docs/aws.md:121), then Sonnet 4.6 judges every box and post-gate cleanup removes the rest, so precision comes from the gate, not the detector (docs/aws.md:179-184). The controlled ladder over one frozen 22-image, 33-worm set shows the split: tiled candidates alone reach 33/33 worms with about 13x noise, Sonnet alone finds only 5/33, and the full pipeline lands 24/33 with 15 false boxes, roughly 0.7 boxes per frame (docs/detection.md:116-131). One caveat I should state up front: that ladder was run on the dev account's build at display floor 49, while production runs a separate retrain, v9r-prod-20260810, whose display floor was refitted to 33 because 49 was throwing away real worms on the new build (docs/state.md:150-155) - so the ladder is the design evidence, not a production benchmark. The moth model, which is a conventional single-stage detector with no gate behind it, sits at F1 0.991, so the low armyworm number is a consequence of the role I gave the detector, not a quality ceiling.


### Dashboard — no framework

**Q: Why is the frontend vanilla JavaScript with no framework?**

It was settled on 2026-07-15, and it holds up technically: the dashboard is 13 ES modules with real import/export scoping, and the only thing put on the global object is the inline-handler surface — that is the single Object.assign(window, {...}) at web/dashboard_v4/js/main.js:137, everything else stays module-private. There is no package.json and no node_modules, so it ships as static files to S3 behind CloudFront and a redeploy is one s3 sync with the no-cache flag (docs/dashboard.md:212-216) — that caching bite was the one real cost we hit. Even sign-in avoids an SDK: js/auth.js does a raw InitiateAuth fetch to the Cognito endpoint with auto-refresh five minutes before expiry, and enforcement lives in API Gateway, not in the JS. To be straight about it, it is not zero-dependency — index.html:114-115 still pulls Chart.js 4.4.1 and hls.js 1.4.12 off jsDelivr — but for four tabs against one API, a framework would add a toolchain without adding capability.


### Dashboard — box rendering

**Q: Why does the dashboard draw bounding boxes itself instead of the backend producing annotated images?**

Since processor v4.1 the Lambda stops after writing the record, no annotated image is produced (lambda/pest-detection-processor.py:9 and the comment at :1506). The record carries normalized 0-1 coordinates stored as strings, because boto3's DynamoDB Number type rejects the high-precision floats Rekognition returns (boxes_to_db_format, lambda/pest-detection-processor.py:495-528). The dashboard then draws each box as an absolutely positioned div whose left/top/width/height are just those fractions times 100 in percent, so it needs no pixel maths at all and the boxes track the image at any size (web/dashboard_v4/js/bbox.js:74-84); the only place I compute pixels is the gallery thumbnail, where object-fit:cover crops the frame (bbox.js:203-212). Doing it client-side buys two things: each box is clickable for a TP/FP verdict that persists into the record's verifications map, and under zoom the live scale is published as a CSS variable --z so borders and the flag button stay the same on-screen size up to 6x (bbox.js:227 and :252-255, styles.css:744-786). It removes the annotated-image write entirely, though the read-side fallback to the old processed image is still there for pre-v4.0 records (web/dashboard_v4/js/modal.js:63-70) — I would not claim a specific storage saving beyond "one image per frame instead of two".


### Dashboard — auth boundary

**Q: If I open the dashboard and bypass your login overlay in devtools, what do I get?**

You get the page shell and nothing useful, because the gate is API Gateway, not the JavaScript. In the deployer's route table 20 of the 21 routes are created with AuthorizationType JWT bound to the Cognito authorizer, and the single exception is GET /stream/status, created with AuthorizationType NONE because the Orin and mini-PC kvs_controller poll it — that's deployer/deploy.py:110 for the route list and :852 for where the authorizer gets attached. So with the login overlay deleted in devtools, every data call — /history, /presigned-url, /settings, DELETE /detection — comes back 401; the one thing an anonymous caller can read is that status route, which returns camera ids, KVS stream names and an enabled flag, no images and no detections. The pool is admin-create-only with 12-hour ID tokens and a 30-day refresh (deploy.py:766 and :785), and the frames and processed buckets have public access fully blocked, so the only public bucket is the static dashboard site itself. One honest caveat: the tokenless-401 smoke test written up in docs/dashboard.md:195-199 was run on 2026-07-06 on the old AWS account; the stack now live on 506868652945 was redeployed on 2026-08-10 and its route auth was confirmed at deploy time, so I'll re-run that one no-token curl before the demo.


### Robot — gate timeout

**Q: The patrol's cloud gate is fail-open on timeout, and a failed waypoint is just skipped. Why is the robot allowed to shrug off failures?**

It is deliberate, and the three failure classes are handled differently. The DynamoDB gate waits up to 150 seconds for the detection record and then proceeds fail-open (go2_patrol_gated.py:163 and the log line at :594), because a late record still lands in the dashboard, so blocking the dog in the middle of Jewel buys nothing. A navigation failure gets one retry and then the waypoint is skipped, because ABORT_ON_NAV_FAIL is False (:156-157, skip branch at :648-654) — finishing 4 of 5 points beats aborting a field demo. The gimbal is fail-soft too: a camera-control hiccup only logs and the patrol continues (:434); and if the frame capture or the S3 upload fails, that point just has no image and the gate is skipped. None of this is safety — safety is that we launch untethered with the remote in hand as e-stop (:54-56). In practice we got three consecutive 5/5 runs at Jewel.


### Robot — nudge

**Q: You send the robot a 'nudge' rotation before navigation/start and never verify it executed. Why?**

navigation/start only returns success while localization is actively tracking, and USLAM only tracks while the dog is moving, so the nudge is a small in-place rotate goal of 0.30 rad, about 17 degrees, with an 8 second settle before navigation/start (go2_patrol_gated.py:37-40 and :118-124). I did verify it at one point and removed the check on evidence: on 2026-07-30 the pose-change check logged "did not move the dog" three times per run on runs 2 and 3, burned 37 seconds each run, and navigation/start then succeeded on the first attempt with the route completing 5 of 5 waypoints both times, so it never changed an outcome. That is written into the code at :372-378 and recorded in docs/state.md:3857-3861. The failure case is still handled: NAV_START_ATTEMPTS is 2, so if navigation/start refuses, it re-nudges once at double the yaw and retries, and if that also fails the node aborts with a message telling me to increase NUDGE_DELTA_YAW or walk the dog manually (:391-406).


### Deployer — reproducing on NP's account

**Q: If I deploy this on the school's AWS account tomorrow, what exactly runs?**

One command - python deploy.py --profile prod --region us-east-1 --prefix argus --target-label armyworm-larva --sender-email ... - runs 15 idempotent stages in dependency order, from IAM through to SES, and every id it creates is written to deploy_state.json; the list is at deployer/deploy.py:1108-1124, and --dry-run or --only lets you run part of it. That is exactly how the NP account 506868652945 was stood up on 2026-08-10, 15 stages in 103 seconds, end-to-end validated on 2026-08-12 (docs/aws.md:3-7). But the first run was not clean: the processor role came up with no bedrock:InvokeModel, so the LLM gate failed open silently and one validation batch drew 321 boxes instead of 26, and the API role was missing AmazonSESFullAccess and AWSBillingReadOnlyAccess. Both are now fixed in the deployer itself - deploy.py:334 ROLE_POLICIES and deploy.py:387 ROLE_MANAGED - so the lesson I carry is that you validate a deployment by its results, not by all the stages going green. Two things the deployer still cannot do for you: the Custom Labels model is account-bound so it has to be retrained locally through deployer/training.py, and the Cost page stays broken on an NP account because an Organization SCP blocks Cost Explorer.


### Deployer — fresh-account findings

**Q: What did deploying to a genuinely fresh account catch that code review didn't?**

Two things. First, my IAM audit had only exported inline policies, so the first genuinely clean deploy answered most routes but returned 500 on GET /identities and GET /cost — the reference account also had two AWS-managed policies attached on top, AmazonSESFullAccess and AWSBillingReadOnlyAccess; I added a ROLE_MANAGED map at deployer/deploy.py:387 so a deployment attaches them automatically, and /identities then returned 200 with the verified sender. Second, /cost stays broken on the NP account and cannot be fixed from inside it: even the AdministratorAccess user gets AccessDeniedException on ce:GetCostAndUsage because an NP Organization SCP blocks Cost Explorer on member accounts — that whole finding is written up at docs/state.md:733-746. That one is an environment constraint, not a code defect, so the Cost page just will not work on the school account. SES is in sandbox too, but to be accurate that is not a fresh-account discovery — both the old and new accounts are sandboxed, so recipients have to be verified individually either way.


### Security — credentials and IAM

**Q: Where do credentials live, and what is the blast radius if a device is stolen?**

Nothing secret is committed and the handover archive ships zero credentials — README_HANDOVER.md line 54 says so explicitly. On the devices it is not a role, it is a long-lived IAM user access key sitting in ~/.aws, which you can see at robot/go2_patrol_gated.py:613, and right now both the Orin and the mini PC still carry the old cag_user key for the retired account 366356442579; the swap to a production key is still pending until the hardware comes back, docs/state.md lines 1479 to 1484. So the blast radius of a stolen device today is the dead account, not production, and once I swap it the prod key is scoped to just s3:PutObject on frames/*, read on the detections table, and KVS producer, so the worst case is someone pushing junk frames into one prefix. On the laptop side the deployer never writes a key to a file: it validates the pasted key against STS and stores it encrypted in Windows Credential Manager through keyring, deployer/app.py:128-130, and card and password entry only happens inside AWS's own script-free embedded window. Lambda roles are per-function with scoped inline policies — the API's s3:DeleteObject is limited to frames/* in docs/aws.md:149, and the AdministratorAccess on Student_QianRunzhe is the NP bootstrap for deployment, the running system never uses it.


### Cost

**Q: What does this cost to run, and what stops a runaway bill?**

The dominant cost is hosting the Rekognition Custom Labels endpoint, about US$4 per inference-unit hour, so both endpoints stay STOPPED between runs and nothing auto-starts one after training — the deployer only writes the model ARN onto the camera rows, it never calls StartProjectVersion. The guard is pest-model-watchdog on an EventBridge schedule of rate(15 minutes): it reads the model's real status from Rekognition, stamps the first RUNNING sighting itself, and stops the endpoint once the camera's own max_runtime_min is passed — 45 minutes on worm_cam — so the worst case is 45 plus one 15-minute poll, about an hour, no matter whether the start came from the dashboard, the schedule, or a console click (lambda/pest-model-watchdog.py:122-134, deployer/deploy.py:1008-1009). Verification cost is measured, not estimated: about 129 judged crops per triggered frame at roughly 694 tokens each is about 90k tokens per frame, which at 3 photos a day is US$5.40/day on Sonnet 4.6 and US$1.35/day on Haiku 4.5, and the verifier model is selectable per camera (docs/state.md:1743-1745, lambda/pest-detection-processor.py:215-224). Training bills about US$4 per training hour — the 1,148-image reference run took 3.5 hours — and the deployer cost-gates in the UI before start_training is ever called (deployer/training.py:30-31).


### Handover — retraining

**Q: How do I retrain the model?**

Two paths. Inside the ARGUS deployer, training.py takes a local YOLO-labeled folder end to end — validate, convert to a Ground Truth manifest, upload to S3, create or append the Rekognition datasets, train, watch, then write custom_model_arn onto every model_type=custom camera row (deployer/training.py:782-800); it excludes zero-box images because Custom Labels ignores negatives, downscales anything over 4096 px and rescales the boxes from the normalized coords, and writes the ARN through boto3 only because the colons in an ARN get mangled by PowerShell quoting. To be straight about it, that screen has only been proven on a synthetic local test — the two models actually running on the production account, moth-prod-20260811 and v9r-prod-20260810, were trained by the manual CLI recipe in reports/manual/03_models_training.md section 3.14, driven by the account-migration tooling. The lesson that cost me time there: after any labelling in the console the S3 manifest is stale, so you export the live entries with ListDatasetEntries — the dataset is the source of truth, not the manifest. Either way the new ARN lands in exactly one runtime place, the camera row's custom_model_arn, and nothing hardcodes it (docs/aws.md:215-217). Models are account-bound, so a new account always means a retrain, and there is deliberately no auto-retrain loop — wrong detections get labeled by hand and go into the next supervised run.


### Handover — continuity

**Q: A junior inherits this next year. Where do they start, and what will they get wrong first?**

Start at README_HANDOVER.md at the repo root, then the technical manual: Chapter 1 for architecture, Chapter 8 to stand the whole system up from scratch, Chapter 9 for the resource registry; docs/state.md is the living state file, but the code is ground truth. What sits in the production account at s3://argus-frames-506868652945/handover/ is the 13 August package - the 481-file repo snapshot zip plus the 246-page v1.1 manual; the newer 278-page English build is reports/final/ARGUS_Technical_Manual_v1.1.pdf and still needs re-uploading, and there is also a private handover repo built and waiting on a GitHub push. The predictable first mistakes are documented ones: judging the armyworm model by its F1 of 0.599 when the detector is deliberately high-recall and Sonnet 4.6 does the precision work (docs/detection.md:4-9); editing min_confidence, which is 10 and feeds the LLM, when they mean the display floor post_verify_floor, which is 33 - that exact mix-up once silently strangled recall (lambda/pest-detection-processor.py:1222-1226); calling Bedrock with a bare foundation-model id instead of the us. inference-profile id (docs/aws.md:223-224); and forgetting Rekognition models are account-bound, so a new account means a retrain, not an ARN copy. On endpoints, be honest about the watchdog: it is a safety net, not an excuse - worm_cam's max_runtime_min is 45 and it polls every 15 minutes, so a forgotten endpoint still bills about an hour before it closes itself.
