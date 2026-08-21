# Chapter 1 — System overview and architecture

This chapter explains what ARGUS is, the design principles behind it, the full end-to-end dataflow, and where every piece of the system physically runs.
_As of 2026-08-14 (W19). Repository file paths in this manual are relative to the project repository root. The system runs on the NP production account `506868652945`: the stack was deployed there 2026-08-10 by the ARGUS deployer, both models were retrained there, end-to-end validation passed 2026-08-12, and the handover snapshot `argus-repo-snapshot-20260813.zip` was published 2026-08-13. The development account `366356442579` is retired to read-only history — see 1.2.1._

## 1.1 Role in the system

ARGUS is a smart pest monitoring system built for Changi Airport Group (CAG). The target site is the Shiseido Forest Valley inside Jewel Changi. The pests are moths (adult) and armyworms (larvae). Both damage the indoor planting. Today CAG staff find them by walking the site and looking. ARGUS replaces that with cameras, cloud detection, and email alerts, so an operator only acts when something is found.

The system has three layers. Edge devices capture frames. The cloud decides whether a frame contains a pest. The operator sees results on a web dashboard and gets an SES email alert on a hit. No detection logic runs on the edge. No human is in the capture loop.

The project is a Ngee Ann Polytechnic (NP) final-year ECE diploma project, Apr–Aug 2026, supervised by Dr. Yan Li. The moth detection model is inherited from the predecessor, Wilbur Teo. The armyworm model, the detection pipeline upgrades, the dashboard, and the ARGUS deployer are this project's own work.

## 1.2 Inventory

Every subsystem this manual covers, with its location and its chapter. Unless a row says otherwise, cloud identifiers below are the NP production account's; the retired development account's counterparts are history and live in 1.2.1.

| Component | Location | Purpose |
|---|---|---|
| AWS accounts | NP production `506868652945` (IAM user `Student_QianRunzhe`, CLI profile `prod`) — the operative account; retired dev `366356442579` (CLI profile `nbk2`, read-only history); both region `us-east-1` | Host the cloud stack (see 1.2.1 for the split) |
| `pest-detection-processor` Lambda | `lambda/pest-detection-processor.py` (mirror of the live function; entry point `lambda_handler(event, context)`) | S3-triggered detection pipeline v6.3: tiled Rekognition finder + hard-object FP suppression + Sonnet 4.6 judge-every-box gate + post-gate cleanup (see 1.5) |
| `pest-monitoring-api` Lambda | `lambda/pest-monitoring-api.py` | 21-route HTTP API behind API Gateway `vzfl7s6z00` (the retired dev account's gateway was `zwpcbivmsj`) |
| `pest-camera-scheduler` Lambda | `lambda/pest-camera-scheduler.py` | Runs per-camera capture/model schedules |
| `kvs-hls-handler`, `pest-model-watchdog` Lambdas | live account; sources mirrored in `deployer/audit/*_src/` and `lambda/pest-model-watchdog.py` (the v6.2 per-camera build the deployer ships) | HLS playback URL minting; auto-stop of running Rekognition endpoints (per-camera `max_runtime_min`, 15-minute EventBridge schedule; see 1.3) |
| S3 buckets | `argus-frames-506868652945`, `argus-processed-506868652945`, `argus-dashboard-506868652945` (retired dev: `frames-armyworm-366356442579`, `processed-images-armyworm-366356442579`, `pest-dashboard-366356442579`) | Frame ingest, processed images, dashboard hosting |
| DynamoDB tables | `pest-monitoring-cameras`, `-detections`, `-system-config`, `-schedule-logs` (same names on both accounts) | Camera config, detection records, global settings, schedule logs |
| Rekognition Custom Labels | production: project `argus-detection` (armyworm, `v9r-prod-20260810` trained, F1 0.613), project `argus-moth-detection` (`moth-prod-20260811` trained, F1 0.991). Retired dev history: `armyworm-detection-v9` (v9 retrain `v9-20260805-0713`), `armyworm-detection` (v5/v4), `SmartPestProject` (Wilbur's moth model) | The detection models |
| Bedrock | inference profile `us.anthropic.claude-sonnet-4-6` (authorized on both accounts; production also has Haiku 4.5) | LLM verification stage (Claude Sonnet 4.6; the v4.5-era Haiku 4.5 profile is history) |
| Dashboard (web) | `web/dashboard_v4/` → https://d1dtoxef7qmugl.cloudfront.net (production); the retired dev copy at https://d1twcdquexdgj8.cloudfront.net holds history only | Operator UI: live view, gallery, analytics, settings |
| Cognito | production: pool `us-east-1_9selFDHpc`, app client `6vebotf45bp8u46cnraddiaplv` (retired dev: pool `us-east-1_ea0aJdusl`, client `4husu6afr835e235eu9dqp8av6`) | Dashboard login (admin-created accounts only) |
| CloudFront | production `E1YADURLSAVNFA` (retired dev: `E1423RGLAXWNSI`) | HTTPS in front of the dashboard S3 website |
| Kinesis Video Streams | `armyworm-cam-stream`, `moth-cam-stream` — the names are the same on both accounts, but the stream resources exist only on the retired dev account; production has none (deployed without `--live-view`; see 1.6) | Live video rail (parallel to detection) |
| Unitree Go2 EDU + Jetson Orin Nano | dog network `192.168.123.x`; Orin at `192.168.123.18` | Demo/testbed patrol platform (NOT production) |
| SIYI A8 Mini gimbal camera | mini-HDMI -> MS2109 USB capture card on the Orin (since 2026-07-29; the old ethernet address `192.168.144.25` and its RTSP URL no longer answer) | Camera on the Go2 rig (`worm_cam`) |
| Mini PC (Win11 + Ubuntu 22.04 VM) | VM user `wilburteo`; bridged NIC `192.168.123.99` | Runs `kvs_controller.py` for the fixed Hikvision moth camera (`192.168.1.66`) |
| ARGUS deployer | `deployer/` (`deploy.py`, `app.py`, built exe) | One-click recreation of the whole stack in a fresh AWS account (executed for real on the production account 2026-08-10) |
| Account migration tooling | `migration/` (`copy_training_data.py`, `train_v9r_on_prod.py`, `migrate_moth.py`, `migrate_jewel_records.py`, `diff_accounts.py`; frozen config baseline `prod_baseline_20260810.json`) | The scripts that stood up and verified the production account |
| Docs | `docs/` (`state.md`, `aws.md`, `hardware.md`, `detection.md`, `dashboard.md`) | Living project documentation (context; code is ground truth) |

### 1.2.1 The two accounts — development/reference vs NP production

Until 2026-08-09 everything lived in one account. On 2026-08-10 the project received the NP production account, the ARGUS deployer stood the full stack up there the same day, and the migration is COMPLETE: both models retrained on production (armyworm 2026-08-12, moth 2026-08-11), end-to-end validation passed 2026-08-12, handover snapshot `argus-repo-snapshot-20260813.zip` published to `s3://argus-frames-506868652945/handover/` 2026-08-13. Where the two accounts stand:

**`366356442579` — retired development/reference (CLI profile `nbk2`).** The account the system was built and validated in. Every development-era measurement in this manual was taken here unless stated otherwise. It is now history, read-only by policy: nothing operative runs there, no new writes. It still holds the dev-era dashboard at https://d1twcdquexdgj8.cloudfront.net and all development detection history, including the threshold-study evidence zones the final report cites — the account is kept until the final report is filed because that evidence lives nowhere else. Every "how to do it now" instruction in this manual points at production.

**`506868652945` — NP production (IAM user `Student_QianRunzhe`, CLI profile `prod`).** Stood up 2026-08-10 by the ARGUS deployer itself: the full 15-stage deploy ran headless in 103 seconds with zero errors, which doubles as the deployer's first real-account validation (Chapter 7). This is the operative account. Live as of 2026-08-14:

- Dashboard https://d1dtoxef7qmugl.cloudfront.net (CloudFront `E1YADURLSAVNFA`, Deployed and serving; a Cognito sign-in user exists and is CONFIRMED).
- API `https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com` — 21 routes, JWT on all but `GET /stream/status`. Cognito pool `us-east-1_9selFDHpc`, app client `6vebotf45bp8u46cnraddiaplv`.
- Buckets `argus-frames-506868652945`, `argus-processed-506868652945`, `argus-dashboard-506868652945`; the four `pest-monitoring-*` tables; the five Lambdas under their usual names (the processor at the validated 1024 MB / 600 s sizing with the full v6.3 tuning env); the fyp-pillow layer; the watchdog schedule ENABLED (rate 15 min).
- Rekognition project `argus-detection`: models are account-bound and cannot be exported, so the armyworm model had to be RETRAINED — the largest migration work item. The training data (36,641 objects / 4.27 GB) was copied server-side with every manifest `source-ref` repointed at the new bucket, and version `v9r-prod-20260810` was submitted for training 2026-08-10. Training completed: Rekognition test-split F1 0.613, and the version ARN is wired into the production camera rows (verified live 2026-08-12; endpoint RUNNING at that read for the validation window).
- Rekognition project `argus-moth-detection`: `moth-prod-20260811` TRAINED 2026-08-11, F1 0.991 (the inherited SmartPestProject scored 0.988 on the same data — treat them as equivalent). The moth capability is fully reconstructed on the new account from data alone: model, camera row, and training set.
- SES: the alert address is verified; the account is still in the SES sandbox.
- Bedrock: Sonnet 4.6 and Haiku 4.5 were already authorized — no use-case-form wait.
- 40 detection records migrated: the complete 36-record Jewel on-site set (the irreplaceable Go2-at-Jewel patrol evidence, frames included, S3 keys byte-identical) plus 4 requested gallery records. Migrated records keep the old account's model ARN on purpose — it is provenance, not configuration.

Deliberately NOT migrated: KVS / live view (the deploy ran without `--live-view`, so no streams exist on the new account and the Live tab has nothing to play — see 1.6), the Wilbur-era legacy Lambdas/tables/APIs, and the bulk detection history (the post-training validation re-push recreates that class of evidence natively, with the new model's ARN).

Naming differs by design: the deployer parameterizes everything, so production buckets carry the `argus-` prefix and the deployer seeds the detection camera row as `camera-1` (with `manual_upload`; the moth migration added `moth_cam`). During the 2026-08-11 reconciliation the detection row was aligned with the old account's `worm_cam` configuration (label "Worm Cam", 05:40 schedule kept disabled). Verified live 2026-08-12: the production `pest-monitoring-cameras` rows are `worm_cam`, `moth_cam`, and `manual_upload` — device upload scripts keep their camera ids at cutover.

Closed since: the holdout validation re-push PASSED 2026-08-12 15:22 (26 images through the live production chain; the first same-day attempt had been invalidated by the processor-role IAM gap, see 9.3.14), and `worm_cam.max_runtime_min` is back at 45 (restored 2026-08-12 after a temporary 240). The edge scripts' repo mirrors were repointed at production 2026-08-13 (upload bucket `argus-frames-506868652945`, API base `vzfl7s6z00`; camera ids and table names unchanged). Still open: sync those mirrors onto the Orin and the mini PC VM and swap the on-device AWS credentials to a production-account key when the hardware returns, and revoke the three temporary cross-account S3 read grants on the old bucket (each migration script has a `--revoke` flag).

## 1.3 Design principles

**Edge captures, cloud decides, operator acts.** Edge devices only take pictures and upload them. All detection intelligence lives in one Lambda in the cloud. The operator's only jobs are to review the dashboard and respond to alerts. This keeps every edge device dumb, cheap, and replaceable.

**Serverless.** There is no server to patch or keep alive. The stack is S3 + Lambda + DynamoDB + API Gateway + Rekognition + Bedrock + SES + Cognito + CloudFront + Kinesis Video Streams. Everything is pay-per-use. The one billed-per-hour resource is a running Rekognition Custom Labels endpoint (about $4/hr). Endpoint lifecycle is automated again since the 2026-08-05 scheduling rework: the `pest-model-watchdog` Lambda (schedule `pest-model-watchdog-15min`, re-enabled after a 2026-07-28..08-05 pause during which stops were manual) honours a per-camera `max_runtime_min` field (`worm_cam` = 45), so a scheduled morning start runs one detection round and the endpoint is stopped again within 45–60 minutes. No fixed stop time exists anywhere; the legacy `model-start-schedule` / `model-stop-schedule` entries were deleted.

**One region; one operative account.** Everything runs in `us-east-1`. Historically the whole stack lived in the development account (`366356442579`, CLI profile `nbk2`); on 2026-08-10 the ARGUS deployer reproduced it in the NP production account (`506868652945`, CLI profile `prod`), which is the operative account since — see 1.2.1. Within each account there is no cross-account wiring and no cross-region resource (the migration's temporary cross-account S3 read grants are scoped, documented, and revoked at sign-off). The one deliberate exception: the Bedrock model id is a cross-region inference profile (`us.` prefix), which Bedrock itself routes; the caller still only talks to `us-east-1`.

**Config in DynamoDB, not in code.** Per-camera behavior (target label, model ARN, confidence threshold, tiling on/off, LLM verify on/off, schedule) is a row in `pest-monitoring-cameras`. The Lambdas read it at call time. Changing the live model is a one-field write, not a redeploy.

**The detection target is a parameter.** Nothing in the Lambdas or dashboard hardcodes "armyworm". A camera row's `target_label` decides what counts as a detection. This is what makes the ARGUS deployer able to ship a generic product (Chapter 7).

**S3 CLI note:** CloudShell writes are blocked in the dev/reference account by a VPC endpoint. Use a local CLI with profile `prod` for any write there; CloudShell is reads only.

## 1.4 Production vision vs the testbed — read this first

This distinction is a hard project truth. Getting it wrong misrepresents the whole design.

**Production vision: fixed cameras.** The deployed product is a set of fixed cameras, one at each monitoring waypoint in the Shiseido Forest Valley, each pointed close-range at vegetation. Fixed close-range capture is not just cheaper — it is what the detection model is good at. The armyworm model's in-domain recall on close-range vegetation is near-total, while wide indoor scenes are its known weakness (Chapter 3). Deployment geometry is a detection lever, not just a logistics choice.

**The Unitree Go2 + SIYI A8 Mini rig is a demo and testbed only.** The quadruped patrols between waypoints, points the gimbal camera, captures, and uploads — a mobile stand-in for cameras that are not installed yet. It exists to prove the pipeline end-to-end and to demo it. It is not part of the production design, and no argument in this manual should be read as "the robot is the product." The testbed did its job: autonomous patrol was declared complete on 2026-07-30, after three consecutive 5/5 waypoint runs at Jewel, app-free (route and settings in `robot/map_profiles.md`; Chapter 5).

The cloud cannot tell the difference, by design. A frame from the Go2's camera and a frame from a future fixed camera land in the same S3 prefix, hit the same Lambda, and follow the same rules. Swapping the testbed for fixed cameras changes nothing in the cloud.

## 1.5 End-to-end dataflow (the detection rail)

The full chain, camera to operator:

```
camera frame (JPEG)
   -> S3  argus-frames-506868652945  key: frames/<camera_id>/<...>.jpg
      (retired dev account: frames-armyworm-366356442579)
   -> S3 ObjectCreated event (prefix frames/)
   -> Lambda pest-detection-processor (v6.3)
        1. read camera row from pest-monitoring-cameras (model ARN, target_label,
           min_confidence = the CANDIDATE floor (10), tiling_enabled,
           llm_verify_enabled, post_verify_floor = the display floor (33))
        2. Rekognition DetectCustomLabels (worm_cam runs v9r-prod-20260810,
           a high-recall front end)
           (per-camera tiling: grid + overlap + per-tile upscale + NMS;
            ON for worm_cam — tiles gather candidates down to
            TILE_MIN_CONFIDENCE=8; the W15 whole-image ruling is SUPERSEDED)
        3. hard-object FP suppression (v4.3): a DetectLabels pass finds
           person/vehicle/furniture/machinery regions; any target box mostly
           covered by one is dropped; boxes on plants are never touched
        4. LLM denoiser gate (per-camera opt-in): Bedrock Claude Sonnet 4.6
           judges EVERY candidate box as a padded, upscaled crop
           (LLM_VERIFY_ALL_BOXES=true, up to LLM_VERIFY_MAX_BOXES=120);
           the verify prompt describes the larva (elongated, clearly
           segmented body, typically yellow-and-black striped)
        5. post-gate cleanup: NMS + containment dedupe, area cap (a box
           covering more than 5% of the frame is dropped), and the
           post_verify_floor (33)
        6. write ONE DynamoDB record to pest-monitoring-detections
           (unconditional — clean frames write a record too)
        7. on a confirmed detection: SES email alert to the configured recipient
   -> dashboard (reads records via pest-monitoring-api, draws boxes client-side)
   -> operator
```

Key properties of this rail:

- **The frame upload is the API.** An edge device needs exactly one AWS permission: put an object into the frames bucket. Everything after that is event-driven.
- **`put_item` is unconditional.** A clean frame (zero detections) still writes a record. The Go2 patrol gate depends on this: the patrol script uploads, then `wait_for_detection()` polls a DynamoDB `query` on `image_id` (every 1.5 s, 150 s budget, fail-open on timeout) until the record appears, then moves to the next waypoint. The gate condition is record EXISTENCE, not detection: no detection is not a timeout; it is a normal record.
- **The detection record is the single source of truth.** Primary key `image_id` is the exact S3 object key. The record stores structured `bboxes` plus a `verifications` map (operator TP/FP verdicts). The processor no longer bakes boxes into images; the dashboard draws boxes on a canvas overlay from the stored coordinates.
- **Who finds and who judges is settled: Rekognition finds, Sonnet 4.6 judges.** The detector is deliberately a high-recall front end — its console F1 (0.613 for the production `v9r-prod-20260810`; the dev v9r scored 0.599) is not pipeline accuracy, because precision comes from the gate and the post-gate cleanup, not from the detector. A whole-frame LLM pass as the finder was A/B-tested and rejected (Bedrock's input-resolution ceiling makes it miss most worms; Chapter 3), and the v5.x experiments that let the LLM lead via its own tile sweep were retired as well: the v6.3 dead-code strip (deployed 2026-08-07, 3266 -> 1545 lines) deleted all of those code paths, leaving exactly the live chain — tiled finder -> v4.3 suppression -> judge-every-box gate -> post-gate cleanup — with the live path byte-identical to v6.2's.
- **Two floors, two meanings.** `min_confidence` (10) is the candidate floor feeding the gate; raising it silently strangles recall before the LLM ever sees a box. The user-facing threshold is the per-camera `post_verify_floor`, applied after verification — it is the field the dashboard's threshold control edits. The live value is **33** (verified against the production row 2026-08-14). History: 49 was decided 2026-08-10 after a measured 34-vs-49 threshold study, then refitted per-build to 33 on 2026-08-13 so a live Test upload draws the same boxes the curated gallery shows. The two knobs cannot be confused since the 2026-08-05 rework.
- **Stage order is fixed:** finder → v4.3 suppression → judge → post-gate cleanup. Each stage only ever removes or confirms what the previous stage produced.

NOTE: version pinning. This chapter matches processor v6.3, deployed 2026-08-07. The repo mirror is `lambda/pest-detection-processor.py`; the pre-strip v6.2 source is archived at `lambda/archive/pest-detection-processor_v6.2_full.py`. The same v6.3 source ships in the ARGUS deployer and runs on the production account. Chapter 2 documents the processor version history in full.

## 1.6 The parallel live rail (Kinesis Video Streams)

Detection is asynchronous. The live rail exists so the operator can also watch the cameras in real time. It is fully parallel to the detection rail and shares nothing with it except the cameras.

```
camera RTSP feed
   -> GStreamer pipeline (rtspsrc -> rtph264depay -> h264parse -> kvssink)
      running on the edge device (Orin for worm_cam, mini PC VM for moth_cam)
   -> Kinesis Video Streams (armyworm-cam-stream / moth-cam-stream, 2 h retention)
   -> Lambda kvs-hls-handler (GET /video-playback) mints an HLS session URL
   -> dashboard Live tab plays the HLS stream
```

Streaming is on-demand, controlled from the dashboard. The edge daemons (`kvs_controller.py` on both the Orin and the mini PC VM) poll `GET /stream/status` every 5 seconds — this is the single unauthenticated API route — and start or stop their GStreamer pipeline to match the `stream_enabled` flag on their camera row. `armyworm-cam-stream` is BROKEN as of 2026-07-29: the SIYI A8 was moved off ethernet onto the MS2109 USB capture card, so the RTSP source the GStreamer pipeline expects no longer exists. `kvs-controller.service` on the Orin still runs, but streaming will fail the moment it is enabled. On `moth-cam-stream` the sources split across layers: the mini PC's `kvs-controller.service` is healthy and polls with `stream_enabled=true` on the `moth_cam` row, and HLS playback was verified on the dashboard at some earlier point, but `docs/aws.md` (newer) records the stream as parked with the Hikvision camera repurposed. Treat the moth stream as parked; closing this definitively needs a live check (the row's `stream_enabled` flag plus KVS PutMedia activity).

On the NP production account the live rail does not exist: the 2026-08-10 deploy deliberately ran without `--live-view`, so no KVS stream resources were created. The production `worm_cam` row does carry `kvs_stream_name` `armyworm-cam-stream` with `stream_enabled` false (verified live 2026-08-14) — the name matches the old account's, but there is no stream resource behind it. Nothing in production used streaming anyway (`stream_enabled` was already false on the old account), but the dashboard Live tab and the devices' `kvs_controller` have nothing to talk to until streams are created — an open decision, driven by whether the demo needs live view.

## 1.7 Deployment topology — what runs where

**Cloud — NP production (AWS `506868652945`, `us-east-1`) — the operative stack.** The ARGUS-deployed implementation of the design: the five Lambdas under their usual names (processor v6.3 at 1024 MB / 600 s), the four tables, the three `argus-*` buckets, API Gateway `vzfl7s6z00`, Cognito `us-east-1_9selFDHpc`, CloudFront `E1YADURLSAVNFA`, the fyp-pillow layer, Rekognition projects `argus-detection` + `argus-moth-detection`, and the watchdog schedule (rate 15 min, ENABLED; the watchdog is the v6.2 per-camera build, which reads `max_runtime_min` per camera — shipped by the deployer). No KVS (see 1.6). SES is in sandbox mode. Chapter 2 covers all of it.

**Cloud — retired dev/reference (AWS `366356442579`, `us-east-1`) — history only.** The same design, where it was built: five Gen-2 Lambdas, the four tables, the three old-name buckets, the dev Rekognition projects, Bedrock, SES, Cognito, CloudFront, API Gateway `zwpcbivmsj`, both KVS streams, and the EventBridge Scheduler entries `pest-model-watchdog-15min` and the runtime `pest-sched-*` rules (the 05:40 SGT morning-start rule exists only while the camera row's schedule is enabled; it was left disabled). The legacy `model-start-schedule` / `model-stop-schedule` / `frame-extraction-schedule` entries were deleted 2026-08-05. Kept read-only for the evidence it holds (1.2.1).

**Jetson Orin Nano (on the Go2, `192.168.123.18`, SSH user `unitree`).** Ubuntu 20.04, ROS 2 Foxy. Runs the patrol script (repo mirror `robot/go2_patrol_gated.py`, deployed on the Orin under `~/go2/`; its `main()` walks the waypoint list — per scan waypoint: USLAM goal, `capture_frame()`, `upload_frame()` to `frames/worm_cam/<waypoint>/<ts>.jpg`, then `wait_for_detection()` polls the detections table every 1.5 s for up to 150 s until the processor's unconditional record appears, fail-open on timeout — the repointed mirror already uploads to `argus-frames-506868652945`), the SIYI A8 Mini capture/control code (siyi_sdk over UDP port 37260 for gimbal control; frames via the mini-HDMI -> MS2109 USB capture card since 2026-07-29 — the old RTSP path is gone), the S3 uploader, the compiled ARM64 KVS producer, and its `kvs_controller`. It does NOT run USLAM — navigation and SLAM run on the Go2's sport MCU (`192.168.123.161`, DDS only, no SSH). The Orin talks to USLAM over plaintext `std_msgs/String` topics `/uslam/client_command` and `/uslam/server_log`. Chapter 5.

**Mini PC (fixed moth camera site).** Windows 11 host running an Ubuntu 22.04 VM (account `wilburteo`). The VM runs `kvs-controller.service` (systemd, `Restart=always`), which polls `/stream/status` and pipes the Hikvision camera at `192.168.1.66` into `moth-cam-stream`. Remote access is a self-healing reverse SSH tunnel from the VM to the Orin (`reverse-tunnel-fyp.service`, `-R 2222:localhost:22`), key-auth both ways. This box is the closest thing in the current build to the production fixed-camera node. Chapter 6.

**Browser (operator).** The dashboard is static files — `index.html`, `styles.css`, 13 ES modules — served from S3 bucket `argus-dashboard-506868652945` behind CloudFront `E1YADURLSAVNFA` at https://d1dtoxef7qmugl.cloudfront.net, its `js/config.js` templated at deploy time to the production API and Cognito client (the retired dev copy: `pest-dashboard-366356442579` behind `E1423RGLAXWNSI` at https://d1twcdquexdgj8.cloudfront.net). Login is Cognito (email sign-in, admin-created accounts, 12 h tokens). All API calls carry a Bearer JWT; enforcement lives in API Gateway, not in the JS. Chapter 4.

**ARGUS deployer (any Windows machine).** A built exe (`deployer/`, built from `deploy.py` / `app.py` via PyInstaller, spec `ARGUS.spec`) that recreates the whole cloud stack in a fresh AWS account from the audited bill of materials in `deployer/STACK_MANIFEST.md`. Entry point: `main()` in `deployer/deploy.py`, which runs the module-level `STAGES` list — 15 `(name, stage_fn)` pairs in dependency order, `stage_iam` through `stage_ses` — via `run_plan()`, emitting a structured event per stage and stamping `deploy:last_stage` into saved state so a died run can report where it stopped and resume; `--only s3,lambda` runs a named subset. `app.py` is the WebView UI wrapper around the same plan. It ships a generic, detection-target-agnostic product: no dataset, no trained model (Rekognition Custom Labels models are account-bound and cannot be exported), parameterized names, and a guided training step for the customer's own data. No longer only a rehearsal artifact: the 2026-08-10 NP-production-account deploy was executed by this deployer (15 stages, 103 s, zero errors). Chapter 7. Rehearsal procedure: `deployer/REHEARSAL.md`.

**Development laptop.** A Windows 11 machine holding the working copy of the project repository. Joins the dog network via a USB Ethernet adapter at static `192.168.123.50/24`. All AWS writes go through CLI profiles `prod` (NP production, the operative one) and `nbk2` (retired dev/reference) from here.

## 1.8 The two cameras and the two models

The `pest-monitoring-cameras` table holds three rows: `worm_cam`, `moth_cam`, and `manual_upload` (a hidden fallback that backs the dashboard's Test-upload feature). The ids are the same on both accounts since the 2026-08-11 reconciliation (see 1.2.1). The two real cameras, production values (checked against the live `worm_cam` row 2026-08-14):

| | `worm_cam` | `moth_cam` |
|---|---|---|
| Physical camera | SIYI A8 Mini on the Go2 (testbed) | Hikvision fixed camera at `192.168.1.66` |
| Edge host | Jetson Orin Nano | Mini PC Ubuntu VM |
| Target label | `armyworm-larva` | `Moths` |
| Model | `v9r-prod-20260810` in project `argus-detection` (own work; retrained on production 2026-08-10; the dev-account v9 retrain is its direct ancestor, same data and recipe) | `moth-prod-20260811` in project `argus-moth-detection` (rebuilt on production from the data of Wilbur Teo's inherited SmartPestProject) |
| Model F1 | 0.613 — low by design: a high-recall front end feeding the LLM gate (the dev v9r scored 0.599 on the same recipe; v5's 0.852 is history; see the caveat below) | 0.991 (the inherited SmartPestProject scored 0.988 on the same data — treat them as equivalent) |
| Candidate floor / display floor | `min_confidence` 10 / `post_verify_floor` 33 (49 decided 2026-08-10, refitted per-build to 33 on 2026-08-13) | `min_confidence` only (no gate) |
| KVS stream | `kvs_stream_name` = `armyworm-cam-stream`, `stream_enabled` false; no stream resource on production, and the dev-era stream is BROKEN as of 2026-07-29 (see 1.6) | `moth-cam-stream` (name only on production; see 1.6) |
| Tiling | ON (`tiling_enabled=true`; the W15 whole-image ruling is superseded) | off |
| LLM crop-verify | ON (`llm_verify_enabled=true`) | OFF — the verify prompt asks about larvae; running it on adult moths would misjudge every box |
| Endpoint auto-stop | `max_runtime_min` 45 (watchdog-enforced) | — |

Model ARNs (both resolve at call time from the camera row's `custom_model_arn` field — nothing hardcodes an ARN). Operative, on production:

- armyworm (live; wired into `worm_cam` + `manual_upload`, verified 2026-08-12): `arn:aws:rekognition:us-east-1:506868652945:project/argus-detection/version/v9r-prod-20260810/1786377372187`
- moth (live; wired into `moth_cam`): `arn:aws:rekognition:us-east-1:506868652945:project/argus-moth-detection/version/moth-prod-20260811/1786412382515`

Development-era history, all on the retired dev account (kept for provenance — the migrated Jewel detection records deliberately keep these ARNs, because the ARN records which model produced that detection):

- armyworm v9 retrain (the dev-era final; live on the dev account 2026-08-07..08-10; a 2026-08-05 retrain of v9 with added data augmentation — flips, rotations, exposure jitter; note the separate project name): `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v9/version/v9-20260805-0713/1785913987295`
- armyworm v9 first cut (superseded by the retrain; endpoint STOPPED): `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v9/version/v9-20260725-1746/1785001598671`
- armyworm v5 (old rollback; endpoint STOPPED): `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection/version/v5-2026-07-07/1783394123547`
- armyworm v4 (older history): `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection/version/armyworm-detection.2026-05-21T12.46.19/1779338780450`
- moth: Wilbur's `SmartPestProject`, native in the dev/reference account (its data, not the model, seeded the production rebuild — Rekognition models are account-bound).

Honest caveat on v5's 0.852: that F1 was measured on the purchased-domain test set and did not transfer to the CAG holdout images (v5 ties v4 there at the operational threshold). The v9 family's 0.599–0.613 is not a regression — it is deliberate: v9 is tuned for recall and the Sonnet 4.6 gate removes the false positives. The full model history, the holdout methodology, and why domain — not model capability — is the binding constraint are in Chapter 3.

Rollback between armyworm versions is one field write. Beware: the ARN contains colons, and a raw `aws dynamodb update-item` from PowerShell mangles the quoting — use boto3 or a JSON parameter file (Chapter 3 has the procedure).

## 1.9 Component map — where everything is documented

| Subsystem | Chapter |
|---|---|
| Cloud stack: Lambdas, S3, DynamoDB, API Gateway, SES, Bedrock, KVS, IAM, watchdog | Chapter 2 |
| Detection models: armyworm v1–v9 history, moth model, holdout methodology, Rekognition CL limits | Chapter 3 |
| Dashboard: modules, auth, deploy, gallery/analytics/live tabs | Chapter 4 |
| Go2 + Jetson Orin: USLAM navigation, patrol script, SIYI A8 Mini control, capture-upload gate | Chapter 5 |
| Mini PC: VM, kvs-controller service, reverse tunnel, Hikvision camera | Chapter 6 |
| ARGUS deployer: exe, deploy.py, stack manifest, rehearsal | Chapter 7 |
| Reproduction on a fresh AWS account end to end (executed for real on NP's `506868652945`, 2026-08-10/11) | Chapter 8 |
| Registries: every ID, ARN, IP, endpoint, and credential location in one place | Chapter 9 |

## 1.10 Operations / reproduction

The procedures below verify that you are looking at the right account and that the top-level stack is alive. Deep operations live in the per-subsystem chapters.

### 1.10.1 Verify the account and region

There are now two accounts (1.2.1) — first make sure you are in the one you intend.

Console: sign in, open the account menu (top right) and confirm the account ID reads `5068-6865-2945` (NP production, the operative account) or `3663-5644-2579` (retired dev/reference); confirm the region selector (top right) reads **N. Virginia (us-east-1)**.

CLI:

```
aws sts get-caller-identity --profile **prod**
aws sts get-caller-identity --profile **nbk2**
```

Expect `"Account": "506868652945"` for `prod` and `"Account": "366356442579"` for `nbk2`. If you are reproducing on your own account (Chapter 8), substitute your own profile name and expect your own account ID everywhere this manual shows either number.

Note: in the dev/reference account, CloudShell cannot write (VPC endpoint block). Do all writes from a local CLI with the `nbk2` profile.

### 1.10.2 Confirm the core Lambdas exist

Console: **Lambda → Functions** (region us-east-1). You should see, among others: `pest-detection-processor`, `pest-monitoring-api`, `pest-camera-scheduler`, `kvs-hls-handler`, `pest-model-watchdog`. The production account carries exactly those five, plus a previous student's unrelated Amplify relics — leave those alone. The retired dev/reference account also carries ~11 legacy Gen-1 functions — do not delete anything based on this list; Chapter 9 classifies every function.

CLI:

```
aws lambda list-functions --profile **prod** --region us-east-1 --query "Functions[].FunctionName" --output table
```

### 1.10.3 Confirm the camera configuration

Console: **DynamoDB → Tables → pest-monitoring-cameras → Explore table items**. Expect three rows: `worm_cam`, `moth_cam`, `manual_upload` (same trio on both accounts since the 2026-08-11 reconciliation; see 1.2.1). Check `custom_model_arn`, `target_label` (`armyworm-larva`), `min_confidence` (candidate floor, 10), `post_verify_floor` (display floor, 33), `tiling_enabled` (true), `llm_verify_enabled` (true), `max_runtime_min` (45) on `worm_cam`.

CLI:

```
aws dynamodb scan --table-name pest-monitoring-cameras --profile **prod** --region us-east-1
```

### 1.10.4 Confirm the ingest trigger

Console: **S3 → argus-frames-506868652945 → Properties → Event notifications** (retired dev: `frames-armyworm-366356442579`). Expect one notification: `s3:ObjectCreated:*`, prefix `frames/`, destination `pest-detection-processor`.

CLI:

```
aws s3api get-bucket-notification-configuration --bucket argus-frames-506868652945 --profile **prod**
```

### 1.10.5 Smoke-test the whole detection rail

The cheapest full-rail test needs no robot and no camera: the dashboard **Settings → Test upload** panel uploads an image via a presigned URL into `frames/manual_upload/...`, which fires the real pipeline end to end. A test image with a known pest should produce a gallery card with boxes within ~10–30 seconds (longer if the Rekognition endpoint has to be running — check **Rekognition → Custom Labels → Projects → argus-detection** or `aws rekognition describe-project-versions` first; a stopped model means zero custom detections. Since 2026-08-05 the watchdog auto-stops the endpoint `max_runtime_min` (45) minutes after it comes up, so a stopped endpoint is the NORMAL resting state, not a fault).

Equivalent CLI upload:

```
aws s3 cp **path/to/test.jpg** s3://argus-frames-506868652945/frames/manual_upload/**test-name**.jpg --profile **prod**
```

Then check the record:

```
aws dynamodb get-item --table-name pest-monitoring-detections --key "{\"image_id\":{\"S\":\"frames/manual_upload/**test-name**.jpg\"},\"detection_time\":{\"S\":\"**<from a query>**\"}}" --profile **prod** --region us-east-1
```

(The table's sort key is `detection_time`; if you do not know it, `query` on `image_id` instead of `get-item`.)

### 1.10.6 Open the dashboard

URL to hand out: **https://d1dtoxef7qmugl.cloudfront.net** (NP production — the one to give people) or **https://d1twcdquexdgj8.cloudfront.net** (retired dev/reference, history only). Login accounts are Cognito admin-created only, per account and pool (creation commands in Chapter 4). The raw S3 website URL also works but is HTTP-only — never hand it out; passwords must not travel over plain HTTP.

### 1.10.7 Reproducing on a fresh account

Two supported paths, both in later chapters:

- **ARGUS deployer (Chapter 7):** the built exe recreates the whole parameterized stack from `deployer/STACK_MANIFEST.md`. This path is proven: it built the NP production account for real on 2026-08-10 (15 stages, 103 s).
- **Manual (Chapter 8):** the step-by-step console + CLI build, in the dependency order IAM → DynamoDB → S3 → Lambda layer → Lambdas → S3 notification → Cognito → API Gateway → CloudFront → config write-back → KVS → Scheduler → Rekognition → seeds → SES.

Two hard constraints to know before starting: Rekognition Custom Labels models are account-bound — they cannot be exported or copied, so a fresh account must retrain from labeled images. And a fresh account's SES starts in sandbox mode — it can only send to verified addresses until a manual production-access request is approved.

One expensive lesson from the real migration, learned before it could waste a multi-hour training run: **after any labelling done in the Rekognition console, the S3 manifest file is stale — the console dataset is the source of truth.** Rebuilding a dataset on another account from the copied manifest silently loses console-added boxes. Export the live entries with `ListDatasetEntries`, repoint every `source-ref` at the new bucket, upload that as the new manifest, and only then create the datasets (scripted in `migration/train_v9r_on_prod.py`).

### 1.10.8 Security note for anyone touching the repo

No file in the repo or this manual may contain a secret: no AWS secret access keys, no passwords, no tokens. Non-secret identifiers (account ID, ARNs, bucket names, API IDs, IPs) are fine and are used freely above. One known exception exists as a caution: `datasets/archive/experiments/pre_v3_abandoned/download.py` contains an inline Roboflow API key from an abandoned experiment — credential stored there, not reproduced here. Do not copy that file anywhere public. Device and RTSP passwords are documented separately, outside the repo (see `docs/hardware.md` header).

## 1.11 Cross-references

- Chapter 2 — every cloud resource in 1.5–1.7, at recreate-level detail, including the processor version history (v4.0 → v6.3).
- Chapter 3 — both models, the v1–v9 training history, the holdout evaluation discipline, and the tiling history (the W15 whole-image ruling and its supersession).
- Chapter 4 — the dashboard modules, Cognito user management, redeploy command, emergency auth rollback.
- Chapter 5 — Go2/Orin: USLAM command sequence, patrol gate loop, SIYI A8 Mini control mapping and its firmware quirks.
- Chapter 6 — mini PC VM, `kvs-controller.service`, the reverse-tunnel access path.
- Chapter 7 — the ARGUS deployer and the product-scope ruling (generic, no dataset ships).
- Chapter 8 — full reproduction runbook for a fresh account (executed for real on the NP production account, 2026-08-10/11).
- Chapter 9 — registries: the complete ID/ARN/IP/endpoint tables, and the PROD/DEMO/DEAD classification of every live resource.
