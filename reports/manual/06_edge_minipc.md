# Chapter 6 — Edge platform: mini PC (moth camera)

This chapter covers the mini PC that drives the fixed Hikvision moth camera: its network topology, the Kinesis Video Streams producer daemon, the still-frame capture script, the systemd units, the reverse SSH tunnel that makes the machine reachable, and the operating procedures.

_As of 2026-08-15. Live config values verified against the production DynamoDB rows 2026-08-14._

## 6.1 Role in the system

The production concept for ARGUS is fixed cameras at each waypoint. The Unitree Go2 is a demo testbed; the mini PC is the fixed-camera edge node. It sits in the lab, wired to a Hikvision IP camera (192.168.1.66) that watches for adult moths. Its job is to turn that camera into two cloud inputs: a live H.264 video stream into Kinesis Video Streams (`moth-cam-stream`), and, on demand, still JPEG frames into S3 that trigger the detection pipeline.

The machine is a Windows 11 host running an Ubuntu 22.04 VM under VMware. Everything project-related runs inside the VM as user `wilburteo` (the account name is inherited from the predecessor, Wilbur Teo). The VM's uplink is NAT'd, so no machine anywhere can open a connection into it. All remote administration goes through a self-healing reverse SSH tunnel that the VM keeps open to the Jetson Orin on the Go2.

Control is cloud-driven. The streaming daemon never decides anything locally: it polls one HTTP API route (`GET /stream/status`) every 5 seconds and starts or stops the GStreamer producer to match what the dashboard says. Toggling "Stream on" for the Moth Cam card on the dashboard is the only control action an operator needs.

**Account note (updated 2026-08-13).** The system runs on the NP production account `506868652945` (CLI profile `prod`). The full ARGUS stack deployed there 2026-08-10 (dashboard `https://d1dtoxef7qmugl.cloudfront.net`, API `https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com`), both models were retrained there (moth `moth-prod-20260811` on 2026-08-11, armyworm `v9r-prod-20260810` on 2026-08-12, Chapter 3), and the handover snapshot `argus-repo-snapshot-20260813.zip` was published 2026-08-13. The development account `366356442579` (CLI profile `nbk2`) is where this node was built and validated; it appears in this chapter only as history. The repo copies of the mini PC scripts were repointed to production 2026-08-13. Two caveats specific to this node. First, the VM itself has not had its sync pass yet: the script copies and AWS keys deployed on the machine still carry development-account values until the hardware returns (the pending pass is listed in Section 6.10). Second, the KVS live-view path has no production counterpart yet: the production deploy ran without `--live-view`, so the account has zero video streams. Stream names are unchanged across accounts — the production `moth_cam` row carries `kvs_stream_name = moth-cam-stream` with `stream_enabled = false` — so the daemon polls the production API and correctly starts nothing. The still-frame path (Section 6.7) works on production as-is.

## 6.2 Inventory

| Item | Location | Purpose |
|---|---|---|
| `kvs_controller.py` | repo `minipc/`; deployed `/home/wilburteo/kvs_controller.py` on the VM | Polling daemon: `/stream/status` → start/stop GStreamer → `moth-cam-stream` |
| `run_kvs_controller.sh` | repo `minipc/`; deployed `/home/wilburteo/run_kvs_controller.sh` | Wrapper the service execs; exports credentials, RTSP settings, and `CAMERA_ID=moth_cam` |
| `kvs-controller.service` | repo `minipc/`; deployed `/etc/systemd/system/kvs-controller.service` | systemd unit; `User=wilburteo`, `Restart=always` |
| `capture_and_upload_v4_armyworm.py` | repo `minipc/` | Still-frame path: RTSP (or local file) → JPEG → S3 → detection pipeline |
| `capture_and_upload_v3_person_cam.py` | repo `minipc/` | Historical predecessor (W5, old account). Kept for history only. Never run. |
| `reverse-tunnel-fyp.service` | VM only, `/etc/systemd/system/` (no repo copy) | Self-healing reverse SSH tunnel VM → Orin port 2222 |
| Hikvision moth camera | 192.168.1.66, RTSP path `/Streaming/channels/101` | The fixed moth camera; RTSP source for both scripts |
| Ubuntu 22.04 VM | VMware on the Win11 host; NAT IP 192.168.189.130 | Runs everything above |
| `moth-cam-stream` | stream name on the `moth_cam` row; same name on both accounts | The video stream the daemon produces into. Streaming was built and HLS-validated on the development account; the production account 506868652945 has no KVS streams yet (deploy ran without `--live-view`) |
| `moth_cam` row | DynamoDB `pest-monitoring-cameras` (partition key attribute `camera_id`) | Per-camera config: `stream_enabled`, `kvs_stream_name`, model settings |
| KVS Producer SDK build | `/home/wilburteo/amazon-kinesis-video-streams-producer-sdk-cpp` | C++ SDK build that provides the `kvssink` GStreamer element |

## 6.3 Topology and networking

Layout:

```
Hikvision 192.168.1.66 ──RTSP──> Ubuntu 22.04 VM (wilburteo)
                                   ens33 NAT   192.168.189.130  ──> internet (egress only)
                                   ens37 bridged 192.168.123.99/24 (dog wired net, only when plugged)
                                 VMware NAT on Win11 host (campus IP, e.g. 10.1.67.21)
```

Facts that matter:

- The VM has two NICs. `ens33` is VMware NAT (192.168.189.x subnet, VM at 192.168.189.130). `ens37` is bridged onto the Go2 wired network as 192.168.123.99/24 and only works when the dog-net cable is plugged in.
- Because `ens33` is NAT, the VM's outbound traffic appears to the campus network as the Win11 host's IP (for example 10.1.67.21). **No inbound path to the VM exists from anywhere.** This is why the reverse tunnel (Section 6.8) exists.
- The Hikvision camera is reached by RTSP at `rtsp://admin:<password>@192.168.1.66:554/Streaming/channels/101`. The password is never written into any active repo file; it is supplied by environment variable on the VM only. The retired v3 script is the historical exception — it hardcodes the credential inline (Section 6.7).
- Streaming to AWS needs only outbound HTTPS, so KVS production works fine through the NAT with no tunnel and no Orin. The tunnel is for administration only.

## 6.4 `kvs_controller.py` — the streaming daemon

### Purpose

A single-file Python 3 daemon (v2, "API-driven"). It polls the backend for the desired streaming state of one camera and reconciles a GStreamer child process to match. One process drives one camera; a second camera means a second instance with a different `CAMERA_ID`. The Orin runs its own copy of the same file for the Go2's SIYI A8 Mini (`worm_cam`, Chapter 5); this chapter's instance drives `moth_cam`. (Note: as of 2026-07-29 the Orin instance's RTSP source no longer exists — the A8 was moved to an HDMI-USB capture card and `worm_cam` streaming is broken; see Chapter 5. The service still polls `/stream/status`, so it looks healthy. This chapter's `moth_cam` instance is unaffected.)

### The control contract: `GET /stream/status`

Every `POLL_INTERVAL_SEC` (5 s) the daemon calls:

```
GET https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com/stream/status?camera=moth_cam
```

The response carries `stream_enabled` (bool) and `kvs_stream_name` (string), read from the camera's row in DynamoDB `pest-monitoring-cameras` by the `pest-monitoring-api` Lambda. The dashboard's "Stream on" toggle writes `stream_enabled`; the daemon picks the change up within one poll cycle (~5 s).

This is deliberately **the one unauthenticated route in the whole API**. Every other route requires a Cognito JWT — a design set 2026-07-06 on the development account and reproduced exactly on the production API (21 routes, JWT on all but this one; authorizer name `cognito-dashboard`). `GET /stream/status` stays open so the edge daemons can poll it with a plain HTTPS GET and zero AWS credentials. The route is read-only and returns nothing sensitive. Consequence: the control path needs no credentials at all; only the `kvssink` child process needs AWS keys, inherited from the environment.

This is a v2 design change. v1 did a direct DynamoDB `get_item` on the old `system-config` nested map (`cameras.{id}.stream_enabled`). The W7-era move onto the shared development account (`nbk2`) put camera config into per-row `pest-monitoring-cameras`, so that read stopped working; v2 switched the control source to the HTTP route. The route survives the 2026-08 production migration unchanged: table names and the response shape are identical on account 506868652945.

**Production status (2026-08-13).** The code default `API_BASE` in `minipc/kvs_controller.py` has pointed at the production API since the 2026-08-13 repoint. The copy deployed on the VM predates that repoint and still polls the development API until the pending device sync pass (Section 6.10); the polling contract is identical on both, so the sync is a file copy plus a credential swap. One production reality to keep in mind: the account has no KVS streams yet, and the `moth_cam` row reads `stream_enabled=false`, so the daemon idles. If the dashboard toggle is flipped on before a `moth-cam-stream` stream is created there, the daemon will start the pipeline, `kvssink` will fail against the missing stream, and the crash-detect in `main()` will relaunch it every cycle — visible restart churn in `journalctl`. Create the stream first (Section 6.10, reproduction step 1).

### Key functions

**`fetch_desired_state()` — read the control signal.** Purpose: ask the backend whether this camera should be streaming right now. How it works: builds `{API_BASE}/stream/status?camera={CAMERA_ID}` (the camera id is URL-quoted), sends a plain HTTPS GET with an `Accept: application/json` header and a **10 s timeout**, parses the JSON body, and returns `(bool(stream_enabled), kvs_stream_name)`. A missing `stream_enabled` field reads as `False`. Failure behaviour: any `URLError`, `HTTPError`, JSON `ValueError`, or `OSError` is caught, logged as `API poll failed`, and the function returns `(None, None)`. The main loop treats `(None, None)` as "no information" — it keeps the current state and retries next cycle. A flaky network can therefore never stop a running stream, and never start one either.

**`build_gst_args(stream_name)` — assemble the pipeline command.** Purpose: produce the full `gst-launch-1.0` argument list (a Python list, no shell). The pipeline **transcodes**: `rtspsrc` (location = the assembled RTSP URL, `latency=0`, `drop-on-latency=true`, `protocols=tcp`) → `decodebin` → `videoconvert` → `x264enc` (`key-int-max=45` = one keyframe every 45 frames, `tune=zerolatency`, `pass=cbr`, `speed-preset=superfast`, `bitrate=2000000`) → `h264parse` → `kvssink` (`stream-name={stream_name}`, `storage-size=512`, `aws-region` from `AWS_DEFAULT_REGION`, `frame-timecodes=false`, `fragment-duration=2`, `max-latency=2`). Transcoding exists because the Hikvision RTSP output is not guaranteed to be clean H.264 that `kvssink` can pass through. Contrast with the Orin's A8 pipeline, which is passthrough (`rtspsrc → rtph264depay → h264parse → kvssink`, Chapter 5).

**`is_gst_running()` — child alive check.** True when `gst_proc` is set and `gst_proc.poll()` returns `None` (the process has not exited). This one-liner is the "actual state" side of every reconcile decision.

**`start_gst(stream_name)` — launch the producer.** Steps: (1) if a pipeline is already running, log a warning and return — the start is idempotent; (2) copy the daemon's environment and add `GST_PLUGIN_PATH = <sdk>/build` and `LD_LIBRARY_PATH = <sdk>/open-source/local/lib` so GStreamer can find `kvssink`; (3) `subprocess.Popen` with stdout/stderr sent to `/dev/null` and `preexec_fn=os.setsid`, which puts the pipeline in its own process group — the property `stop_gst()` relies on; (4) log the child PID. AWS keys are never passed explicitly; `kvssink` inherits them from the environment the run script exported.

**`stop_gst()` — tear the pipeline down completely.** Steps: if nothing is running, just clear the stale handle and return. Otherwise send `SIGTERM` to the **whole process group** (`os.killpg`), wait up to 5 s; on timeout escalate to `SIGKILL` on the group and wait 2 s more; a `ProcessLookupError` (already dead) is silently ignored. Always ends by setting `gst_proc = None`. The group kill matters because `gst-launch-1.0` spawns children; killing only the parent leaks the RTSP connection to the camera.

**`graceful_exit(signum, _frame)` — clean shutdown.** Registered for SIGTERM and SIGINT in `main()`. Logs the signal, calls `stop_gst()`, exits 0. This is what makes systemd restarts clean: the pipeline dies before the daemon does, so no orphan holds the camera or the stream.

**`main()` — the 5-second reconcile loop.** Purpose: keep the actual GStreamer state equal to the desired state from the API, forever. Each cycle:

1. Poll: `fetch_desired_state()`.
2. **Hold state on a failed poll**: if the poll returned `None`, sleep `POLL_INTERVAL_SEC` and `continue`. Nothing is started or stopped on missing information — a dead backend freezes the current state rather than flapping it.
3. Crash detection: if `gst_proc` is set but the child has exited, log `GStreamer exited unexpectedly` with the return code and clear the handle. The desired state is still true, so step 4 restarts it — automatic recovery one cycle (~5 s) after any GStreamer crash.
4. Reconcile desired vs actual:

```python
        if desired and not running:
            if not stream_name:
                log.error(f"stream_enabled=true but kvs_stream_name is empty for {CAMERA_ID}")
            else:
                start_gst(stream_name)
        elif not desired and running:
            stop_gst()
```

   The empty-name error repeats every cycle until the camera row is fixed; nothing starts. The two remaining combinations (already matching) need no action.
5. Log only on transitions: the `(desired, running)` tuple is compared with the last logged tuple, so a stable state produces silence, not one log line per 5 s.
6. Sleep `POLL_INTERVAL_SEC` (5 s) and repeat. There is no exit path except a signal.

### Configuration (all overridable by environment variable)

| Variable | Code default | Deployed value | Meaning |
|---|---|---|---|
| `CAMERA_ID` | `worm_cam` | `moth_cam` (exported by the run script) | Which camera row this instance polls |
| `POLL_INTERVAL_SEC` | `5` | 5 | Poll period |
| `API_BASE` | `https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com` (production, since the 2026-08-13 repoint) | VM copy predates the repoint; it polls the development API until the sync pass (Section 6.10) | Backend HTTP API |
| `RTSP_USER` | `admin` | `admin` | Hikvision user |
| `RTSP_PASS` | `""` (no insecure default) | set on VM only | Hikvision password. Credential stored on the VM, not reproduced. |
| `RTSP_HOST` | `192.168.1.66` | same | Camera IP |
| `RTSP_PATH` | `/Streaming/channels/101` | same | RTSP channel path |
| `KVS_SDK_DIR` | `/home/wilburteo/amazon-kinesis-video-streams-producer-sdk-cpp` | same | Producer SDK build root (`GST_PLUGIN_PATH` = `<sdk>/build`, `LD_LIBRARY_PATH` = `<sdk>/open-source/local/lib`) |
| `AWS_DEFAULT_REGION` | `us-east-1` | same | Region passed to `kvssink` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | none | set on VM only | Producer credentials, consumed by `kvssink` only. Still the development-account keys until the sync pass swaps in a production-account key (Section 6.10). Credential stored on the VM, not reproduced. |

Note the code default `CAMERA_ID` is `worm_cam` because the same file serves the Orin instance. The mini PC identity comes entirely from the run script export (Section 6.5).

### Data in / data out

- In: RTSP video from 192.168.1.66; JSON control state from `GET /stream/status`.
- Out: H.264 fragments into Kinesis Video Streams `moth-cam-stream`, us-east-1, on whichever account the VM's exported keys belong to. The HLS-validated runs happened on the development account; after the sync pass this is the production account 506868652945, which needs the stream created there first (Section 6.10). The dashboard plays this back over HLS (Chapter 4).

### Gotchas and failure modes

- API unreachable → the daemon logs `API poll failed` and holds its current state. A dead backend does not stop a running stream.
- `stream_enabled=true` with an empty `kvs_stream_name` → logged as an error every cycle, nothing starts. Fix the camera row.
- GStreamer crash → detected next cycle and restarted automatically (desired state still true).
- The camera password appears inside the GStreamer command line (`rtsp://user:pass@...`), so it is visible in `ps` output on the VM. Known and accepted on this single-user machine.
- NOTE: doc lag — the KVS section of `docs/aws.md` still says `moth-cam-stream` is "parked (Hikvision cam repurposed)". `docs/hardware.md` (updated later) and the 2026-07-14 migration record say the controller is deployed, the `moth_cam` row has `stream_enabled=true`, and HLS playback was verified on the dashboard. Follow hardware.md and the code: the controller is in active use, and `docs/state.md` records HLS playback verified on the dashboard through this controller (transcode pipeline). The aws.md line is stale doc lag. Physical cable status on any given day can only be confirmed on the machine itself.

## 6.5 `run_kvs_controller.sh` — the wrapper script

The systemd service execs this script, not the Python file. It exists to inject environment that must never live in a committed file:

1. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_DEFAULT_REGION` — the producer keys (still development-account keys on the VM; the Section 6.10 sync pass replaces them with a production-account key). The repo copy carries `<SET_ON_VM>` placeholders; the real values exist only on the VM. Credential stored there, not reproduced. The keys live in the VM copy of the script unless an EnvironmentFile was added later: the deployed unit as captured in the repo has no `EnvironmentFile=` directive, and the script header's EnvironmentFile mention is a recommendation, not a record. Confirm with `systemctl cat kvs-controller` on the VM.
2. `export CAMERA_ID="moth_cam"` — the camera identity override. This line is load-bearing; see the story below.
3. `RTSP_USER` / `RTSP_PASS` / `RTSP_HOST` / `RTSP_PATH` — the Hikvision source. Password placeholder in the repo, real value on the VM only.
4. `exec python3 /home/wilburteo/kvs_controller.py` — replaces the shell, so systemd supervises the Python process directly.

### The `CAMERA_ID` override story (2026-07-14 migration)

During the camera-id true migration (`moth_cam_01` → `moth_cam`), the home-directory files on the VM were switched by `sed`. But the deployed systemd unit carried its own `Environment=CAMERA_ID=moth_cam_01` line, which `sed` on home files cannot touch and which needs sudo to edit. The no-sudo fix: add `export CAMERA_ID=moth_cam` to `run_kvs_controller.sh` before the final `exec` line (hardware.md records it at line 2 on the VM copy; the repo copy has it at line 17, after the AWS exports — the position does not matter, only that it runs before the `exec`). A shell `export` executed inside `ExecStart` overrides whatever the unit's `Environment=` line set, because the script runs after systemd builds the environment. The service runs as `wilburteo` with `Restart=always`, so a plain `kill <pid>` (no sudo) respawned it with the new export. Verified by reading `/proc/<new-pid>/environ`: `CAMERA_ID=moth_cam`.

The cosmetic residual (the unit file still saying `moth_cam_01`) was cleaned up the same day with sudo:

```bash
sudo sed -i 's/moth_cam_01/moth_cam/' /etc/systemd/system/kvs-controller.service && sudo systemctl daemon-reload
```

Both sources now say `moth_cam`; the script export still wins if they ever differ again. Keep the export line — it makes the camera identity independent of the unit file.

## 6.6 `kvs-controller.service` — the systemd unit

Deployed at `/etc/systemd/system/kvs-controller.service`, enabled, so the stream controller survives VM reboots unattended.

```ini
[Unit]
Description=KVS Controller (mini PC) - polls /stream/status and pushes Hikvision RTSP to KVS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=wilburteo
ExecStart=/home/wilburteo/run_kvs_controller.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- `User=wilburteo` — runs unprivileged. This is what makes the no-sudo restart trick work: the process owner can `kill` it, and `Restart=always` respawns it within 5 s with any script changes applied.
- `Restart=always`, `RestartSec=5` — crashes and kills alike come back automatically.
- The repo copy above has no `Environment=` line. The deployed VM unit historically carried `Environment=CAMERA_ID=moth_cam_01` (now corrected to `moth_cam`, see Section 6.5). If you redeploy from the repo copy, the `Environment=` line simply does not exist and the script export is the only source — that is fine and simpler.

## 6.7 `capture_and_upload_v4_armyworm.py` — the still-frame path

### Purpose and current role

This is the rescued still-capture script: it grabs single JPEG frames from an RTSP camera (or takes a local image file) and puts them into S3 under the exact key shape that triggers the detection pipeline. It is the first hop only — camera → S3. Everything downstream (the `pest-detection-processor` Lambda, AWS Rekognition Custom Labels, DynamoDB `pest-monitoring-detections`, the processed-images bucket, SES alerts, the dashboard gallery) fires automatically off the S3 `PutObject` event. The script never calls Rekognition, DynamoDB (except the on/off config read), or SES.

Current role: an on-demand tool, not a service. There is no systemd unit for it. It is used for manual captures, smoke tests of the cloud chain (`--once`), and pushing known images through the live pipeline without any camera at all (`--image` — this is how holdout and test images get onto the live dashboard). The lineage name says "armyworm" (it was migrated in W7 from the W5 `person_cam` version for the armyworm camera), but it is camera-agnostic: `--camera moth_cam` routes a frame to the moth model, `--camera worm_cam` to the armyworm model.

NOTE: the docstring keeps the W7 migration story as dated development-era history (`armyworm_go2_a8mini`, the old dev-account bucket, `dashboard_v3_8.html` in the flow diagram). A `CURRENT TARGET (2026-08)` note at the top of the docstring and the `DEFAULTS` block carry the truth: production account 506868652945, bucket `argus-frames-506868652945`, profile `prod`, default camera `worm_cam` (post-2026-07-14 migration), and the current dashboard is the v5.2 ARGUS build.

**Production-account repoint (done in the repo 2026-08-13).** The repo copy targets production by default: bucket `argus-frames-506868652945`, CLI profile `prod`, camera ids unchanged (`worm_cam` / `moth_cam`, aligned 2026-08-11). Any copy still sitting on a device predates the repoint and keeps the development-era defaults until the sync pass (Section 6.10). Every value is also settable without editing the file (CLI flags `--bucket` / `--profile` / `--camera`, or the matching environment variables). The moth model the production `moth_cam` row points at is `moth-prod-20260811` in Rekognition project `argus-moth-detection`, retrained 2026-08-11 on the production account from the predecessor's recovered labelled dataset (Chapter 3 covers the rebuild). One deliberate difference from `worm_cam`: the production `moth_cam` row is seeded with `llm_verify_enabled=false` (`migration/migrate_moth.py`), because the LLM verify prompt describes armyworm larvae and this camera's target is adult moths — moth uploads take the plain Rekognition path with no LLM gate. Do not "fix" that flag. As everywhere, that model's endpoint must be RUNNING before an upload produces a detection.

### Run modes

```bash
python capture_and_upload_v4_armyworm.py                          # loop, dashboard-controlled
python capture_and_upload_v4_armyworm.py --ignore-config --interval 10   # loop, fixed interval
python capture_and_upload_v4_armyworm.py --once                   # one frame, then exit
python capture_and_upload_v4_armyworm.py --image some.jpg         # upload a local file, no camera
```

Camera modes need `RTSP_URL` exported in the terminal first (`rtsp://<user>:<pass>@192.168.1.66:554/Streaming/Channels/101` — password from the VM notes, never hardcoded). `--image` mode needs no camera and no OpenCV.

### Key functions

**`make_session(profile, region)` — pick the AWS identity.** Purpose: build the one boto3 Session every AWS call in the script uses (S3, and DynamoDB in loop mode). How it works: a non-empty profile name gives `boto3.Session(profile_name=..., region_name=...)`; an empty or `None` profile falls back to the default credential chain (environment variables, then shared config). Defaults since the 2026-08-13 repoint: profile `prod`, region `us-east-1`. This is the single place where "which account" is decided.

**`get_capture_settings(config_t, fallback_interval)` — the remote on/off switch.** Purpose: let the dashboard start and stop loop-mode capture without touching the process. How it works: one `get_item` on the `detection_settings` row of DynamoDB `pest-monitoring-system-config` (a missing item is tolerated and reads as an empty dict), then returns `(bool(auto_capture), interval)` where the interval is `capture_interval` from the row (or the fallback) clamped to at least `MIN_INTERVAL` (5 s), so a bad config value can never make the loop hammer the camera. Data flow: dashboard Settings page → `pest-monitoring-api` → this row → this function. Failure behaviour: any `BotoCoreError`, `ClientError`, or `ValueError` logs `[Config] read failed` and returns `(False, fallback_interval)` — a broken config read idles the loop rather than capturing blind.

**`capture_frame(rtsp_url, attempts=3)` — one fresh JPEG from the camera.** Purpose: turn the RTSP stream into a single current frame as JPEG bytes. How it works: `cv2` is imported lazily inside the function, so `--image` mode runs on a machine with no OpenCV; a failed import logs an install hint and returns `None`. Then up to 3 attempts. Each attempt: open the stream with `cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)`; a failed open releases the handle, sleeps 1 s, and retries. On a good open it calls `cap.read()` twice and throws the results away — RTSP delivers buffered stale frames first, and this flush makes the next read current — then reads the real frame and releases the capture. A failed grab also sleeps 1 s and retries. A good frame is JPEG-encoded at quality `JPEG_QUALITY` (90). Returns the encoded bytes, or `None` once all attempts are spent; the caller skips that cycle and nothing is uploaded.

**`load_local_image(path)` — file input for `--image` mode.** Purpose: exercise the whole cloud chain with a known image and no camera. How it works: maps the file extension to a content type (`.jpg`/`.jpeg` → `image/jpeg`, `.png` → `image/png`); any other extension is rejected with a log line, because the processor only accepts these. A missing file is rejected too. Returns `(bytes, content_type)` on success, `(None, None)` on rejection; `main()` exits with code 1 on the latter.

**`upload_frame(s3, bucket, camera, waypoint, body, content_type, basename)` — the trigger point.** Purpose: put the image into S3 under the exact key shape the processor expects; this single `put_object` starts the entire downstream pipeline. How the key is built:

```python
ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
ext = ".png" if content_type == "image/png" else ".jpg"
filename = f"{ts}{ext}" if not basename else f"{ts}_{basename}"
key = f"frames/{camera}/{waypoint}/{filename}"
```

Microseconds in the timestamp guarantee uniqueness even for rapid `--once` runs; in `--image` mode the original basename rides along after the timestamp. One `s3.put_object` with the matching `ContentType` writes it (default bucket `argus-frames-506868652945`) and returns the key. No retry inside: a `BotoCoreError`/`ClientError` propagates to the caller — loop mode logs it and carries on; `--once`/`--image` exit 1.

**`parse_args()` — runtime config resolution.** Purpose: decide every setting once, at start. Precedence for each: CLI flag > environment variable (`CAMERA_ID`, `WAYPOINT_ID`, `S3_BUCKET`, `AWS_PROFILE`, `AWS_REGION`, `CAPTURE_INTERVAL`) > `DEFAULTS`. Mode flags: `--once`, `--image PATH`, `--ignore-config`.

**`main()` — mode dispatch and the capture loop.** Purpose: pick a mode, then run it. It builds the session and S3 client once, then dispatches. `--image`: load the file, upload once, exit (1 on any failure). Camera modes require `RTSP_URL` in the environment; if it is missing the script exits 1 with an export hint instead of limping on. `--once`: capture one frame, upload, exit. Otherwise loop mode, where the DynamoDB table handle is created (it is needed nowhere else). Each loop cycle: (1) with `--ignore-config` use the fixed interval, floored at 5 s; otherwise call `get_capture_settings` — if `auto_capture` is false, log `[Idle]`, sleep out the interval, and skip the cycle; (2) `capture_frame` — a `None` result logs `[Skip]` and sleeps out the interval; (3) `upload_frame` — an upload failure is logged but never breaks the loop; (4) sleep the interval and repeat. There is no exit path in loop mode except Ctrl-C, which the `KeyboardInterrupt` handler at the bottom of the file turns into a clean `[Stop]` message. (`log(msg)` is the only helper: a one-line timestamped, flushed print.)

### Constants

`DEFAULTS`: camera `worm_cam`, waypoint `fixed_cam`, bucket `argus-frames-506868652945`, profile `prod`, region `us-east-1`, interval 60. (The development-era values — bucket `frames-armyworm-366356442579`, profile `nbk2` — survive only in the docstring's W7 history block.) `CONFIG_TABLE=pest-monitoring-system-config`, `CONFIG_KEY=detection_settings`, `JPEG_QUALITY=90`, `MIN_INTERVAL=5`.

### Gotchas and failure modes

- The S3 key shape is a contract. The processor's `parse_s3_key` requires the first segment to be `frames` and at least 4 segments; the `{camera}` segment picks the model. A wrong key either fires nothing or falls back to the `manual_upload` camera and runs the wrong model.
- The Rekognition Custom Labels model for the target camera must be RUNNING (hourly-billed). If it is stopped, uploads still succeed but the processor's `detect_custom_labels` call fails: no detection row, no gallery image, no email.
- SES on the production account is in sandbox mode: alert emails reach verified addresses only. A detection that lands on the dashboard but sends no email to a new recipient is this, not a pipeline fault.
- The bucket must be the one carrying the S3 → processor event trigger (`argus-frames-506868652945` on production). Uploading anywhere else does nothing.
- This script always uploads the whole, uncropped frame. Do not add cropping or resizing here. Whether the processor (v6.3) then tiles the frame is a per-camera cloud setting (`tiling_enabled` on the camera row, Chapter 2) — true on `worm_cam` (verified against the live production row 2026-08-14) and seeded false on `moth_cam` (`migration/migrate_moth.py`). Both states are deliberate; do not "fix" either from this end.

### `capture_and_upload_v3_person_cam.py` — history only

The W5 predecessor. It points at the old practice account's bucket `fyp-practice-qrz` and the old `system-config` table, uses camera id `person_cam` (deleted from the product 2026-07-13), and has Chinese comments. It also hardcodes the Hikvision RTSP credential inline in its `RTSP_URL` constant — credential stored there, not reproduced; this is exactly the practice v4 was written to end. Keep the file as history. Never run it and never copy its pattern.

## 6.8 `reverse-tunnel-fyp.service` — the self-healing reverse tunnel

### Purpose

The VM is NAT'd and unreachable inbound (Section 6.3). This unit makes the VM keep an outbound SSH connection to the Jetson Orin with a reverse forward: `-R 2222:localhost:22`. While it is up, anyone on the Orin can reach the VM's sshd at `localhost:2222`. Installed and adversarially verified 2026-07-14: all tunnel connections were killed on the Orin, the port went down within 5 s, and the tunnel self-healed by T+45 s.

### Unit facts (from the deployed VM unit; there is no repo copy)

- systemd service, `User=wilburteo`, enabled at boot.
- `Restart=always` with `RestartSec=15` — any exit retries every 15 s, forever.
- SSH options: `ConnectTimeout=10`, `ServerAliveInterval 30` / `ServerAliveCountMax 3` (a dead link is declared after ~90 s of silence and the client exits, triggering a restart).
- `StartLimitIntervalSec=0` — disables systemd's start-rate limiter.

The exact `ExecStart` line was never captured into the repo; the flags above are recorded in docs/hardware.md and docs/state.md, and the reconstruction in Section 6.10 step 7 is derived from them. Capture the real unit with `systemctl cat reverse-tunnel-fyp` next time the VM is reachable.

### Why the two hardening flags exist

The first build of this unit had neither `ConnectTimeout` nor `StartLimitIntervalSec=0`. During a dog-wifi flap (the Orin's network dropping and returning), the ssh client hung inside the TCP connect. The unit showed `active (running)` while doing nothing, indefinitely — a silent failure that looked healthy.

- `ConnectTimeout=10` bounds the connect attempt: a hung connect now fails in 10 s instead of hanging forever, so the exit/restart cycle actually happens.
- `StartLimitIntervalSec=0` removes systemd's default start-rate limit. Without it, a fast fail loop (network down for a while → many restarts) trips the limiter and systemd permanently gives up on the unit. With it set to 0, the unit retries every 15 s forever, and comes back on its own the moment both machines are up.

### Two-way key auth

Both directions are key-only, no passwords in the path:

- The VM's ed25519 public key is in the Orin's `/home/unitree/.ssh/authorized_keys` — this lets the tunnel's outbound ssh log in to the Orin unattended.
- The Orin's `~/.ssh/id_ed25519` public key is in the VM's `/home/wilburteo/.ssh/authorized_keys` — this lets an operator on the Orin hop into the VM through the tunnel.

The VM sudo password was used transiently during the 2026-07-14 install and is not recorded anywhere.

### Known dependency (accepted)

The remote path requires the Orin to be up. Orin off → no tunnel → no remote access to the VM. This is accepted: the KVS streaming path does not depend on the Orin at all (it is direct VM → AWS over NAT), so a down Orin costs administration convenience only, never the moth stream. The escape hatch is the VMware NAT port-forward (Section 6.9), which needs one-time GUI setup on the Win11 host.

## 6.9 Access recipe: laptop → Orin → tunnel → VM

Standard remote path (automatic since 2026-07-14, nothing to start manually):

1. SSH from the laptop to the Orin:
   - Campus wifi: `ssh unitree@10.1.125.24`
   - Dog wired net (laptop static 192.168.123.50): `ssh unitree@192.168.123.18` (alias `go2`)
2. From the Orin, hop through the tunnel into the VM:

```bash
ssh -i ~/.ssh/id_ed25519 -p 2222 wilburteo@localhost
```

Failure modes on this path:

- Tunnel port not listening on the Orin → the VM or its service is down, or the tunnel is mid-heal. Wait 45 s and retry; then check from the VM side if you have physical access (`systemctl status reverse-tunnel-fyp`).
- Port listens but the connect hangs with "banner exchange" timeouts → stale sshd processes on the Orin are holding the forwarded port. On the Orin: `sudo ss -tnp | grep `**`<mini-pc-host-ip>`**, kill the stale pids, and the service rebinds within 15 s.

Optional direct-from-laptop path (no Orin needed; one-time GUI setup on the Win11 host):

1. On the Win11 host: VMware Workstation → Edit → Virtual Network Editor → select VMnet8 → NAT Settings → Add a port forward: host port `2222` → `192.168.189.130`, guest port `22`.
2. Then from the laptop: `ssh -p 2222 wilburteo@`**`<win11-host-campus-ip>`** (e.g. 10.1.67.21; the host IP is DHCP-assigned, check it on the host with `ipconfig`).

## 6.10 Operations / reproduction

### Day-to-day operations (on the VM, as `wilburteo`)

Status and logs:

```bash
systemctl status kvs-controller
journalctl -u kvs-controller -f          # live log; state transitions + GStreamer PIDs
systemctl status reverse-tunnel-fyp
journalctl -u reverse-tunnel-fyp -n 50
```

Restart the controller without sudo (the standard move after editing the run script):

```bash
pgrep -af kvs_controller.py     # find the PID
kill <pid>                      # Restart=always respawns it in ~5 s with changes applied
```

With sudo (equivalent): `sudo systemctl restart kvs-controller`.

Confirm the running identity after any restart:

```bash
tr '\0' '\n' < /proc/$(pgrep -f kvs_controller.py)/environ | grep CAMERA_ID   # expect moth_cam
```

### Start / stop the stream (the normal control path)

- Console/dashboard: ARGUS dashboard → Live stream tab → Moth Cam card → toggle Stream on/off. The daemon reacts within ~5 s; HLS fragments appear in ~10 s.
- CLI (writes the same DynamoDB field the API serves — note the project quirk: DynamoDB CLI JSON quoting mangles under PowerShell; run this from bash, or use boto3). The table's partition key attribute is `camera_id` (confirmed in `datasets/archive/experiments/one_off_scripts/migrate_camera_ids.py`):

```bash
aws dynamodb update-item --table-name pest-monitoring-cameras \
  --key '{"camera_id": {"S": "moth_cam"}}' \
  --update-expression "SET stream_enabled = :v" \
  --expression-attribute-values '{":v": {"BOOL": true}}' \
  --profile prod --region us-east-1
```

### Verify the stream is landing in AWS

These checks were validated end-to-end on the development account; on production they only work after the `moth-cam-stream` stream has been created there (reproduction step 1 — the account currently has zero video streams).

- Console: AWS Console → Kinesis Video Streams → Video streams → `moth-cam-stream` → Media playback (live video should render).
- CLI:

```bash
aws kinesisvideo describe-stream --stream-name moth-cam-stream --profile prod --region us-east-1
EP=$(aws kinesisvideo get-data-endpoint --stream-name moth-cam-stream \
  --api-name LIST_FRAGMENTS --profile prod --region us-east-1 --query DataEndpoint --output text)
aws kinesis-video-archived-media list-fragments --stream-name moth-cam-stream \
  --endpoint-url "$EP" --max-results 5 --profile prod --region us-east-1
```

Fresh fragment timestamps prove production is live.

### Verify the control route

- Console: AWS Console → API Gateway → APIs → the HTTP API with id `vzfl7s6z00` → Routes → `GET /stream/status` → confirm no authorizer is attached (every other route shows `cognito-dashboard`).
- CLI:

```bash
aws apigatewayv2 get-routes --api-id vzfl7s6z00 --profile prod --region us-east-1 \
  --query "Items[?RouteKey=='GET /stream/status']"
curl "https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com/stream/status?camera=moth_cam"
```

The `curl` works from anywhere with no token — that is the design.

### What breaks when the Orin is down

- Lost: remote SSH access to the VM (the tunnel has no endpoint). Accepted limitation.
- NOT lost: the moth stream. The controller polls the API and pushes to Kinesis Video Streams directly over the NAT uplink. Detection uploads via the v4 script also work.
- Recovery is automatic: when the Orin comes back, the tunnel unit's 15 s retry loop reconnects with no action on either machine.

### On-device production sync pass (status 2026-08-14: pending hardware return)

The cloud side is done: the ARGUS stack has run on the NP production account 506868652945 since 2026-08-10, both models are trained there, and the repo copies of the mini PC scripts were repointed to production 2026-08-13 (bucket `argus-frames-506868652945`, profile `prod`, API `vzfl7s6z00`). What remains is a device-local file-and-key sync on the VM, to run when the hardware returns:

1. **Copy the repointed scripts** from `minipc/` over the VM's home-directory copies (`kvs_controller.py`, `run_kvs_controller.sh`, `capture_and_upload_v4_armyworm.py` if a copy lives there). Camera ids are unchanged (`worm_cam` / `moth_cam`); the S3-key contract and everything downstream are identical on production — the three ARGUS deploy fixes of 2026-08-10 mean the fresh stack seeds the same tuned detection config the development account ran.
2. **Swap the credentials.** The VM still carries development-account keys. Replace them with a production-account key (Section 6.10 reproduction step 2 gives the minimal policy: `s3:PutObject` on the frames bucket, plus the KVS producer permissions if streaming is revived).
3. **Check the unit environment.** The deployed `kvs-controller.service` may carry its own `Environment=` lines that would override the script's exports (the `CAMERA_ID` story in Section 6.5 shows the failure shape). Run `systemctl cat kvs-controller` on the VM during the pass and clean any old-API or old-account value found there.

The streaming leg stays parked until KVS exists on the production account: the deploy ran without `--live-view`, so the account has zero video streams, even though the `moth_cam` row is already seeded with `kvs_stream_name = moth-cam-stream` and `stream_enabled = false` (`migration/migrate_moth.py`). That is a deliberate open decision (does the demo need live view?), not an omission. The daemon is safe to sync now: it will poll the production API, read `stream_enabled=false`, and start nothing. Create the stream (reproduction step 1) before anyone flips the dashboard toggle.

### Camera-id migration residuals (state as of 2026-07-22)

- The `moth_cam_01` → `moth_cam` migration is complete and verified on all three legs (Orin, cloud, mini PC VM).
- The VM unit's `Environment=` line and the run-script export both say `moth_cam`. The export is the authoritative one if they ever diverge.
- `.bak_premigration` files in `/home/wilburteo` are the rollback copies of the sed-switched files. They predate the 2026-07-30 "backups local only" rule (no .bak/scratch files on production devices). Runzhe's call: keep them as a migration-rollback exception, or archive them locally and delete them from the VM.
- Historical S3 keys under old camera-id prefixes were left untouched on purpose; detection records point at them.
- The Go2 ROS 2 workspaces in the VM home (`unitree_ros2`, `go2_*`, ~640 MB, from the old drive-the-dog-from-the-VM era) are kept intact deliberately. Do not tidy them away.

### Reproducing this node on a fresh AWS account

Prerequisites on the cloud side (Chapter 2): the `pest-monitoring-cameras` table with a `moth_cam` row, the `pest-monitoring-api` Lambda + HTTP API with the unauthenticated `GET /stream/status` route, and an IAM user whose keys the producer will use.

1. Create the video stream.
   - Console: AWS Console → Kinesis Video Streams → Video streams → Create → name `moth-cam-stream`, data retention as desired (the project uses the default).
   - CLI: `aws kinesisvideo create-stream --stream-name moth-cam-stream --data-retention-in-hours 24 --profile `**`<your-profile>`**` --region `**`<your-region>`**
2. Give the producer credentials. Create an IAM user (Console: IAM → Users → Create; CLI: `aws iam create-user --user-name `**`<producer-user>`**) with a policy allowing `kinesisvideo:PutMedia`, `kinesisvideo:GetDataEndpoint`, `kinesisvideo:DescribeStream` on the stream ARN, plus `s3:PutObject` on the frames bucket if the v4 script will run here. Generate an access key and place it ONLY in the VM's `run_kvs_controller.sh` (or a root-owned systemd EnvironmentFile). Never commit it.
3. Set the camera row: in `pest-monitoring-cameras`, the `moth_cam` item needs `kvs_stream_name = moth-cam-stream` and `stream_enabled` present (start `false`).
4. Build the KVS Producer SDK on the VM: clone `amazon-kinesis-video-streams-producer-sdk-cpp` into `/home/`**`<user>`**`/`, build per the SDK's README with GStreamer plugin support, so that `<sdk>/build` contains `libgstkvssink.so`. Install GStreamer (`gstreamer1.0-tools`, `gstreamer1.0-plugins-base/good/bad`, `gstreamer1.0-libav`) and `x264` support.
5. Deploy the three files from the repo's `minipc/` directory to the VM home, fill the `<SET_ON_VM>` placeholders in the script, adjust `API_BASE` to **your API id**, and fix `KVS_SDK_DIR` if the username differs.
6. Install the unit: copy `kvs-controller.service` to `/etc/systemd/system/` (fix `User=` and paths for your username), then `sudo systemctl daemon-reload && sudo systemctl enable --now kvs-controller`.
7. Rebuild the tunnel for your topology (or skip it and use the VMware NAT port-forward): a systemd unit running `ssh -N -R 2222:localhost:22 -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `**`<user>@<jump-host>`** with `Restart=always`, `RestartSec=15`, `StartLimitIntervalSec=0`, and key auth pre-installed in both directions. Do not omit `ConnectTimeout` or `StartLimitIntervalSec=0`; Section 6.8 explains the hang they prevent.
8. Test end to end: toggle `stream_enabled` on, wait 10 s, run the fragment check from this section.

## 6.11 Cross-references

- Chapter 2 (Cloud backend) — the `pest-monitoring-api` Lambda, the `/stream/status` route and its Cognito exception, DynamoDB tables, the per-camera `tiling_enabled` setting, and the S3 → `pest-detection-processor` trigger that the v4 script feeds.
- Chapter 3 (Models and training) — what the moth and armyworm Rekognition Custom Labels models are and why they must be RUNNING for detections; also the production-account moth rebuild (`moth-prod-20260811`, project `argus-moth-detection`, retrained 2026-08-11 from the predecessor's recovered labelled dataset).
- Chapter 4 (Dashboard frontend) — the Live stream tab (HLS playback, Stream on/off toggle) and the Settings page that drives `auto_capture` for the v4 loop mode.
- Chapter 5 (Edge platform: Go2 / Jetson Orin) — the twin `kvs_controller.py` instance for `worm_cam` (passthrough pipeline, no transcode; broken since the 2026-07-29 move to an HDMI-USB capture card, see Chapter 5) and the Orin end of the reverse tunnel.
- Chapter 8 (Reproduction runbook) — the full-system rebuild order; the steps in Section 6.10 slot in after the cloud backend exists.
