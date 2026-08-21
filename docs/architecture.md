# Architecture — the request path, and who owns each hop

A short orientation map. The exhaustive version, including every AWS resource
and identifier, is
[`reports/manual/01_system_architecture.md`](../reports/manual/01_system_architecture.md).
Why it is shaped this way is in [`DECISIONS.md`](DECISIONS.md).

Three layers: **edge captures, cloud decides, operator sees.** No detection
logic runs on the edge. No human is in the capture loop.

---

## The detection path, end to end

```
  camera                                                        operator
    |                                                               ^
    | 1. capture + upload                                           | 8. view / flag
    v                                                               |
  S3  frames bucket  --2. ObjectCreated event-->  Lambda            |
                                                    |               |
                     3. tile + Rekognition detect   |               |
                     4. hard-object FP suppression  |               |
                     5. Bedrock judges every box    |               |
                     6. NMS / containment / area cap|               |
                                                    v               |
                                            DynamoDB detections     |
                                                    |               |
                                7. HTTP API (JWT) --+--> dashboard -+
                                                    |
                                                    +--> SES alert email
```

| Hop | Owner | Notes |
|---|---|---|
| 1. Capture | `minipc/capture_and_upload_v4_armyworm.py` (fixed camera) or `robot/go2_patrol_gated.py` (Go2 patrol) | Both write to the same S3 prefix, so the cloud side is identical either way. |
| 2. Trigger | S3 `ObjectCreated` notification | Fires on **any** put, including a copy. Copying a frame in re-runs detection. |
| 3–6. Pipeline | `lambda/pest-detection-processor.py` | The whole pipeline is this one function. Start here. |
| 7. Read path | `lambda/pest-monitoring-api.py` | 21 routes behind API Gateway; JWT authorizer on all but `GET /stream/status`. |
| 8. UI | `web/dashboard_v4/` | 13 ES modules, no bundler. `api.js` attaches the Cognito ID token. |

Supporting functions: `pest-camera-scheduler` (per-camera capture and model
schedules), `pest-model-watchdog` (stops a Rekognition endpoint left running
past the camera row's `max_runtime_min`), `kvs-hls-handler` (live-view playback
URLs).

**Five Lambdas, five files.** `deployer/deploy.py` creates exactly five
functions and `lambda/` mirrors all five, one file per function. If you add a
Lambda to the deployer, add its mirror here too — the two lists are meant to
match, and a missing mirror is the kind of gap that hides until someone needs
to read the code. `kvs-hls-handler` is deliberately separate from
`pest-monitoring-api`: it holds a narrow IAM surface (two `kinesisvideo` read
actions, no DynamoDB, S3 or SES).

---

## Inside the Lambda

The order matters, and each stage exists because of a specific failure:

1. **Tile** — the frame is split into an overlapping grid and each tile is
   upscaled before detection. Small larvae at patrol distance are a handful of
   pixels in a whole frame.
2. **Detect** — Rekognition Custom Labels, run as a high-recall front end with
   candidates gathered down to `TILE_MIN_CONFIDENCE = 8`, then converted back
   to global coordinates and merged with NMS.
3. **Suppress** — `DetectLabels` runs on the frame and any worm box more than
   half covered by a hard object (person, vehicle, furniture, machinery) is
   dropped. Plant labels never suppress. Errors here are non-fatal.
4. **Verify** — every surviving box is cropped with padding and judged by
   Claude Sonnet on Bedrock. This is the precision stage. It **fails open**: a
   Bedrock error lets the box through rather than dropping it.
5. **Clean up** — NMS, containment removal, and an area cap
   (`POST_MAX_BOX_AREA = 0.05`) before the display floor is applied.
6. **Write** — `put_item` is unconditional. A clean frame with zero detections
   still writes a record, so the gallery shows what was actually looked at.

Because stage 4 supplies precision, the detector's stand-alone F1 does not
describe pipeline behaviour. See [`detection.md`](detection.md).

---

## Where things run

| | |
|---|---|
| Cloud | AWS `us-east-1`. Lambdas, S3, DynamoDB, Rekognition Custom Labels, Bedrock, API Gateway, Cognito, CloudFront, SES. |
| Fixed camera | Hikvision over RTSP → mini PC (Windows 11 host, Ubuntu VM) → S3. Also runs the Kinesis Video Streams controller. |
| Robot | Unitree Go2 EDU carrying a Jetson Orin Nano, with a SIYI A8 Mini gimbal over HDMI into a USB capture card. Demo and testbed only. |
| Operator | Browser, over CloudFront, signed in with Cognito. |

---

## Two things to know before you change anything

**Both capture paths are interchangeable to the cloud.** They differ only in
what moves the camera. Anything you change in the pipeline affects both.

**The camera row in DynamoDB is the configuration surface.** Tiling,
verification, thresholds, schedule and runtime cap are all per-camera fields,
not code constants. A pipeline that appears not to be verifying is usually a
camera row missing `llm_verify_enabled`, not a broken function.
