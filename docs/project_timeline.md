# Project development timeline

This timeline records completed work and validated lessons. A planned item is not
listed as completed. Dates are Singapore time unless stated otherwise.

## April 2026 — Foundation and first working prototype

### W1, 6–10 April — Inheritance audit and cloud proof of concept

- Reviewed Wilbur Teo's moth-monitoring work, reports, deployment assets, and
  AWS architecture.
- Defined the project split: retain the existing moth path and add an independent
  armyworm-detection path.
- Built a first end-to-end prototype from Hikvision RTSP capture through S3,
  Lambda, Rekognition Custom Labels, DynamoDB, API Gateway, WebSocket updates,
  and a dashboard.
- Trained and tested an early armyworm Rekognition model on a small dataset.

### W2–W4, 13–30 April — Project definition and early integration

- Finalised the proposal and corrected technical claims and system diagrams.
- Refined the armyworm detection, bounding-box drawing, thresholding, dashboard,
  and alerting paths.
- Defined the initial Go2 + SIYI A8 Mini integration direction while preserving
  the inherited moth system.

## May 2026 — System hardening and AWS migration

### W5–W6, 4–15 May — Dashboard and model iteration

- Iterated the dashboard and repaired multiple backend and frontend defects.
- Evaluated the v3 armyworm model (purchased-test F1 0.719 at that stage) and
  deployed it through the camera configuration.
- Ran crop/zoom experiments on CAG images. The result was a practical A8 target
  range of 2–4×; 6× did not improve recall.
- Established Go2/Ubuntu VM networking and ROS 2 communication.

### W7–W8, 18–29 May — Move from practice account to the lab account

- Migrated the working pipeline from the personal practice AWS account to the
  shared nbk2 AWS account.
- Wired the inherited moth model and armyworm model into the new environment.
- Brought up Kinesis Video Streams for live video and fixed the test-upload to
  detection to email route.
- Resolved the real integration failures in S3 event matching, Pillow Lambda
  packaging, Lambda IAM, processed-frame URL routing, and S3 CORS.

## June 2026 — Robot, camera, and interim-review integration

### W9–W10, 2–12 June — Untethered Go2 and SIYI A8 bring-up

- Diagnosed the Go2 wireless topology and established reliable wireless access
  to the Jetson Orin.
- Confirmed the Go2 native USLAM stack and used it instead of building a separate
  navigation stack.
- Connected the SIYI A8 Mini to the Orin through its own network link, created a
  persistent network profile, and verified status, capture, pan/tilt, zoom, and
  S3 upload.
- Reworked the interim presentation, report, architecture diagram, and lab demo.

### W12, 22–26 June — Programmatic patrol and end-to-end field path

- Repaired changing network-interface assignment after the A8 adapter was added.
- Implemented programmatic USLAM waypoint scanning and surveyed valid waypoints.
- Built the integrated patrol flow: navigate, wait for gimbal settle, capture,
  upload, detect, and show the result.
- Achieved the first full inspection run in the lab. Waypoint validity was learned
  to require a navigation probe, not only a physically reachable-looking pose.

### W13, 29 June–2 July — Live video and modular dashboard

- Fixed SIYI Follow mode by saving Follow as the A8 power-on default and added a
  capture settle delay.
- Built the ARM64 KVS producer on the Orin, changed the A8 stream to H.264, and
  brought live streaming back through the cloud stack.
- Reconfigured the moth-camera stream and began splitting the monolithic dashboard
  into maintainable JavaScript modules.

## July 2026 — Productisation, evaluation correction, and next model direction

### W14, 6–12 July — Cloud dashboard and showcase readiness

- Deployed the dashboard through S3 and CloudFront with Cognito authentication
  and JWT-protected APIs.
- Added Gallery deletion with DynamoDB and S3 cleanup.
- Recovered the v5 training path and retrained the armyworm model from an earlier
  F1 of about 0.72 to 0.852 on the purchased test split.
- Prepared the Go2 showcase route and fixed a waypoint dead zone.
- Reskinned the dashboard and started the ARGUS deployment application.

### W15, 13–20 July — Evidence-led model correction and deployment product

- Simplified the product to Worm Cam and Moth Cam, migrated real cloud/device
  camera IDs to `worm_cam` and `moth_cam`, and aligned dashboard timestamps to
  Singapore time.
- Established that whole-image detection is the deployment mode. Tiling/cropping
  damages Rekognition's shadow filtering and increases foliage/shadow false
  positives.
- Corrected an evaluation bug where the DynamoDB query selected the oldest rather
  than newest detection record. Earlier v5/v6 comparisons that used that query
  are not valid evidence.
- Evaluated v6 and rolled it back. Its close-up data contained wrong species,
  blur, and unsuitable scale, so it regressed against v5.
- Audited the purchased dataset and found that its test split is dominated by
  wrong-species dark caterpillars. The v5 F1 0.852 must not be presented as
  deployment accuracy.
- Completed the first clean-account ARGUS deployment verification (15/15 stages),
  including teardown/account lifecycle work. The training screen exists in source
  but still needs its final executable rebuild and rehearsal.
- Set the v7 direction with Dr. Yan Li: curate real light-coloured larvae at a
  field-like scale, remove unsuitable generic dark-caterpillar data, then test
  augmentation only after the clean dataset is validated. No v7 training has run.

## Current position — 20 July 2026

- v5 is the active armyworm model. v6 is rejected.
- The live detection path works from camera frame to dashboard record.
- Go2 is a demonstration/test platform. Production deployment remains fixed
  cameras at waypoints.
- The immediate model task is v7 data curation and validation, not another
  unreviewed training run.

## Evidence base

- Official Claude Chat exports archived under `context/claude-chat/`.
- Claude Code records under `.claude/projects/C--FYP/`.
- The full dated record is `docs/state.md` (newest entries first).
- Current operational facts in `docs/state.md`, `docs/aws.md`, `docs/hardware.md`,
  and `docs/detection.md`.
