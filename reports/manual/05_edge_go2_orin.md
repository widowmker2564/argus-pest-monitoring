# Chapter 5 — Edge platform: Unitree Go2 + Jetson Orin

This chapter covers the mobile edge platform: the Unitree Go2 quadruped, the Jetson Orin mounted on its back, the SIYI A8 Mini gimbal camera, USLAM navigation and its map profiles, the gated patrol script, the patrol scheduler, the live-stream producer, and the full operations runbook.
_As of 2026-08-20._

## 5.1 Role in the system

The Go2 is the demo and testbed carrier for the ARGUS armyworm camera. It walks a surveyed route, stops at waypoints, and lets the SIYI A8 Mini capture a frame at each stop. The Jetson Orin on its back runs everything project-side: the patrol node, the frame capture, the S3 upload, the DynamoDB detection gate, and the Kinesis Video Streams producer for the dashboard's live view. The dog's own sport MCU runs USLAM (the localization and navigation service); the Orin only talks to it over ROS 2 topics.

In the end-to-end chain, this platform is the front end of the detection pipeline: A8 frame -> S3 (`argus-frames-506868652945`) -> the `pest-detection-processor` Lambda -> AWS Rekognition Custom Labels + the Bedrock LLM verifier -> DynamoDB (`pest-monitoring-detections`) -> dashboard. The patrol script waits at each capture waypoint until the DynamoDB record for its uploaded frame exists, so the robot's motion is gated on the cloud pipeline having actually processed the frame.

Project framing to keep straight: the Go2 is a demo/testbed only. The production design for CAG at Jewel Changi is fixed cameras at each waypoint, no robot deployment. This chapter documents a finished milestone: on 2026-07-30 the Go2 ran three consecutive 5/5 autonomous patrols on the real Jewel site map, app-free, from both cold boot and back-to-back starts, best run 150 s with zero retries, with the full capture -> S3 -> Rekognition -> LLM -> DynamoDB -> dashboard chain live.

**Account note (updated 2026-08-13).** Everything in this chapter was built and validated on the development account `366356442579`; the system now runs on the NP production account `506868652945`. The ARGUS deployer stood the full cloud stack up there on 2026-08-10 (all 15 stages, 103 s; Chapter 7), both Rekognition models were retrained on it, and the handover snapshot `argus-repo-snapshot-20260813.zip` was published to it on 2026-08-13. Production names: frames bucket `argus-frames-506868652945`, API `https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com`. The deployer originally seeded the armyworm camera row as `camera-1`, but it was re-keyed to `worm_cam` on 2026-08-11, so the Orin's `CAMERA_ID` needs NO change. The repo mirrors of the edge scripts were repointed to these production values on 2026-08-13: `S3_FRAMES_BUCKET` in `go2_patrol_gated.py` and `capture_4k_hdmi.py`, and the `API_BASE` default in `kvs_controller.py`. One device pass is still open — the hardware was away when the repoint was made, so the mirrors must be synced onto the Orin and the on-device `~/.aws/credentials` swapped from the dev-account `cag_user` key to a production-account key; 5.12.6 has the checklist. KVS / live view was skipped at deploy, so a stream must still be created on the production account before the live-stream producer of 5.10 has a target there.

## 5.2 Inventory

| Item | Location | Purpose |
|---|---|---|
| Unitree Go2 EDU | physical; sport MCU at `192.168.123.161` | Quadruped. USLAM runs on its MCU (DDS only, no SSH). |
| Jetson Orin Nano | on the Go2's back; `192.168.123.18` (dog net); SSH user `unitree` | Runs patrol, capture, upload, gate, KVS producer. Ubuntu 20.04, ROS 2 Foxy. |
| SIYI A8 Mini gimbal camera | on the Go2; `192.168.144.25`, UDP control 37260 | Frame source (RTSP 1080p) + live-stream source. |
| Mount plate | `robot/Karlz+Livox+Mount+plate.stl` | Printable mounting plate for the payload stack. |
| `robot/setup_go2.sh` | mirrors Orin `~/setup_go2.sh` | Per-terminal DDS environment (CycloneDDS bound to the dog link by IP). |
| `robot/go2_patrol_gated.py` | mirrors Orin `~/go2/go2_patrol_gated.py` | The gated patrol node (bringup + waypoints + capture + S3 + DDB gate). |
| `robot/pose.py` | mirrors Orin `~/go2/pose.py` | One-shot pose grab for waypoint surveying. |
| `robot/get_map_id.py` | mirrors Orin copy | Read-only: print the currently loaded USLAM map id. |
| `robot/nav_probe.py` | mirrors Orin copy | Single-goal planner probe (is the map plannable at all). |
| `robot/map_profiles.md` | repo only | Per-map parameter sets (INITIAL_POSE + WAYPOINTS) for every recorded map. |
| `robot/tests/wp_test_1.py` | + `.log` | Nav-only route validation, round 1 (superseded by round 2). |
| `robot/tests/wp_test_2.py` | + `.log` | Nav-only route validation on the lab PRIMARY map; the 4/4 proof. |
| `robot/tests/wp_test_3.py` | repo + Orin | Nav-only route validation on the lab DEMO map. |
| `robot/tests/wp2_probe.py` | repo + Orin | Planner probe: find a plannable cell for a refused waypoint. |
| `robot/tests/wp_survey.py` | repo + Orin | Hands-free waypoint survey on a new map (odom motion/silence detector). |
| `robot/tools/pose_logger.py` | Orin `/tmp/pose_logger.py` during surveys | Continuous pose logger to `/tmp/pose_log.txt`; used for the Jewel walk-the-route survey. |
| `robot/tools/localize_only.py` | repo + Orin | Localize without nudging, report the settled pose (for surveying a parking spot). |
| `robot/tools/uslam_listen.py` | repo + Orin | Raw listener on both `/uslam/server_log` and `/uslam/client_command`, each line annotated with the live pose, logging to `/tmp/uslam_listen.log`. The client_command tap is how the phone app's own seed pose was recovered. |
| `robot/tools/bringup_test.py` | repo + Orin | Bringup-sequence-only test (stop -> seed -> start), no route. |
| `robot/kvs_controller.py` | mirrors Orin `~/go2/kvs_controller.py` | Polls the dashboard stream toggle; starts/stops the KVS pipeline. |
| `robot/run_kvs_controller.sh` | mirrors Orin `~/run_kvs_controller.sh` | Env + credentials wrapper the systemd service execs. |
| `robot/kvs-controller.service` | mirrors Orin systemd unit | Boot-persistent service wrapping the two files above. |
| `robot/patrol_scheduler.py` | mirrors Orin `~/go2/patrol_scheduler.py` | Polls the dashboard's `/schedule` row; launches `go2_patrol_gated.py` at the scheduled time. |
| `robot/run_patrol_scheduler.sh` | mirrors Orin `~/run_patrol_scheduler.sh` | Non-interactive ROS-env wrapper the systemd service execs. |
| `robot/patrol-scheduler.service` | mirrors Orin systemd unit | Boot-persistent service wrapping the two files above. |
| `robot/capture_4k_hdmi.py` | repo + Orin | Experimental 4K capture over HDMI + UVC card (see 5.3; the fitted card capped at 1080p). |
| `robot/_archive/2026-07-29/`, `robot/_archive/2026-07-30/` | repo | Field run logs: the failed first Jewel run, the 5/5 clean runs, Orin script backups. |
| KVS stream `armyworm-cam-stream` | AWS, us-east-1 | Live H.264 stream from the A8 to the dashboard. The stream name is unchanged across accounts, but the 2026-08-10 production deploy skipped KVS — create the stream on `506868652945` before enabling live view there (5.12.6). |
| S3 bucket `argus-frames-506868652945` | AWS, us-east-1, production account | Frame drop point; S3 event triggers the processor Lambda. The dev account used `frames-armyworm-366356442579` (history). |
| DynamoDB table `pest-monitoring-detections` | AWS, us-east-1 (same table name on both accounts) | Detection records; the patrol gate polls it. |

## 5.3 Hardware and wiring

**Unitree Go2 EDU.** Two computers matter:

- **Sport MCU `192.168.123.161`** — wired, DDS only, no SSH. USLAM (mapping, localization, navigation) runs HERE, not on the Orin. There is no `ps` visibility into it from the Orin. If the USLAM service crashes or wedges, the only recovery is a full power-cycle of the dog.
- **Jetson Orin Nano `192.168.123.18`** — SSH user `unitree`, SSH alias `go2`, Ubuntu 20.04, ROS 2 Foxy. Note: the `unitree_ros2_example` binaries sit in `bin/`, not `lib/<pkg>/` — run them directly; `ros2 run` fails.

**Networks.** Three distinct links touch the Orin:

- **Dog net `192.168.123.0/24`** (wired): MCU `.161`, Orin `.18`, mini PC bridged NIC `.99`, laptop `.50` (ASIX USB Ethernet adapter on the dock, manual static, gateway/DNS blank; reset to DHCP for normal networking).
- **A8 net `192.168.144.0/24`** (wired, second USB Ethernet dongle): A8 at `.25`, Orin at `.30` via the NetworkManager profile `a8-link` (autoconnect). `a8-link` is MAC-locked; on 2026-07-29 it had to be re-bound from a missing dongle (`6C:1F:F7:21:52:73`) to the AX88179B actually fitted (`6C:1F:F7:28:CD:0C`). If the A8 link ever dies after a hardware shuffle, check this MAC bind first. A second gotcha found the same day: an orphan profile `Wired connection 2` was grabbing `eth0` and starving `a8-link`; it is now set to `autoconnect no`.
- **WiFi (RTL8821CU dongle, `wlan0`)**: `npwireless` (NP campus, priority 100), `apps-jewel` (CAG's `Apps@Jewel` at Jewel, priority 90, hidden SSID — `802-11-wireless.hidden yes` is required), `iPhone Air` hotspot fallback. All `nmcli` operations need `sudo` over SSH. The SSID is `Apps@Jewel` with an "s"; `App@Jewel` fails identically to being out of range. CA validation is off on both enterprise profiles on purpose: the Orin boots with a 1970 clock. `Apps@Jewel` has AP client isolation — the Orin gets full internet (S3 + Rekognition verified) but the laptop cannot SSH to it over that SSID; see 5.12.4.

**SIYI A8 Mini power.** The A8 is powered over an XH connector cable. The GND wire of that cable was resoldered after unrelieved tension snapped it. Before any sustained untethered operation the cable needs strain relief (a service loop) — a standing OPEN roadmap item, still not done as of 2026-08-11. The supply rail the XH cable draws from is not recorded in any repo source; confirm on the hardware before rewiring anything.

**SIYI A8 Mini video paths.** RTSP at `rtsp://192.168.144.25:8554/main.264` caps at 1080p; 4K exists only on the SD card and the HDMI port. A 4K-over-HDMI detour was tried on 2026-07-29: the A8 was rewired to mini-HDMI -> USB capture card -> Orin. The fitted card turned out to be a MACROSILICON MS2109 (USB 2.0) that maxes out at 1920x1080 in both MJPG and YUYV, so the path gained nothing over RTSP, and it also killed gimbal UDP control (port 37260 rides the ethernet link HDMI does not carry). The A8 was reverted to ethernet the same day and the full chain re-verified. `robot/capture_4k_hdmi.py` remains as the experiment harness; it forces the MJPG fourcc before setting resolution and verifies the actual delivered frame size so a 1080p frame can never be mislabelled as 4K. No genuine 4K frame was ever captured through it: the only card ever fitted was the 1080p-max MS2109, the script's guard refuses to upload non-3840x2160 frames, and the `test_4k` S3 zone was never populated. The card was unplugged in the same-day revert to ethernet.

Function-level detail of `robot/capture_4k_hdmi.py` (no ROS involved; constants: `WARMUP = 8` discard frames, `JPEG_Q = 92`, `CAMERA_ID = worm_cam`, bucket `argus-frames-506868652945` since the 2026-08-13 repoint):

- `open_dev(dev_index, w, h)` — opens `/dev/videoN` via V4L2 and sets the MJPG fourcc BEFORE the resolution. Order matters: without MJPG most cheap UVC cards silently cap at 1080p (USB bandwidth cannot carry raw YUY2 at 4K) and OpenCV keeps the old size without any error. Returns the capture handle or None.
- `grab(dev_index, w, h)` — reads `WARMUP` frames keeping the last (exposure and AWB settle), JPEG-encodes at quality 92, and returns `(jpeg_bytes, actual_w, actual_h)`. The size is read back from the delivered frame, not the requested mode — that read-back is what the mislabel guard keys on. `(None, 0, 0)` on open, decode, or encode failure.
- `upload(zone, jpeg_bytes)` — `put_object` to `frames/worm_cam/<zone>/<UTC ts>.jpg`, the same key scheme as the patrol, so a test frame flows through the normal S3 -> processor -> DynamoDB -> dashboard chain and lands in the named dashboard zone.
- `probe()` — lists `/dev/video*` nodes, prints the `v4l2-ctl --list-formats-ext` command to enumerate modes (look for MJPG 3840x2160), and open-tests each node. Capture cards usually expose two nodes; only the first reads frames.
- `main()` — `--shot` grabs at 3840x2160 and REFUSES to upload unless the delivered size is exactly 3840x2160 (exit code 2): this is the guard that makes a 1080p frame impossible to mislabel as `test_4k`. `--ab` then grabs a 1080p arm of the same scene into `test_1080p`; that arm uploads even on a size mismatch, labelled with its true size, because the 1080p side is the control, not the claim. Exit code 1 = no frame decoded at all.

**Untethered rule.** Any route with forward motion must run untethered — a connected cable can drag hardware off a table or off the dog. The patrol script's launch countdown exists to give the operator time to unplug. The remote stays in the operator's hand as the e-stop, with the critical caveat in 5.5: pressing any button on it stops USLAM outright and ends that patrol run.

## 5.4 DDS environment: setup_go2.sh

`robot/setup_go2.sh` (Orin: `~/setup_go2.sh`) is a 4-line environment file sourced in every terminal that touches ROS:

- `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp` — the patrol deliberately uses plain rclpy over CycloneDDS. The Unitree Python SDK (`unitree_sdk2_python`) is avoided: its cyclonedds Python binding segfaults on this Orin.
- `CYCLONEDDS_URI` pins `NetworkInterfaceAddress` to `192.168.123.18` — the dog link is selected by IP, so eth0/eth1 interface renames do not matter.

Sourcing rules (these bite hard over SSH, see 5.12.4): interactive terminals answer `1` (foxy) at the .bashrc fishros prompt, then `source ~/setup_go2.sh`. Non-interactive shells (Posh-SSH exec channels, `bash -c`) get NO ROS from .bashrc at all — they need the explicit triple source:

```
source /opt/ros/foxy/setup.bash && source ~/cyclonedds_ws/install/setup.bash && source ~/setup_go2.sh
```

## 5.5 USLAM navigation

USLAM is Unitree's on-dog SLAM/nav service. Its entire control surface is plaintext `std_msgs/String` on two topics — no Nav2, no custom message types:

- commands out: `/uslam/client_command`
- feedback in: `/uslam/server_log`

**Wire format (reverse-engineered from the app, verified W10–W12).** Two quoting rules, not interchangeable:

- Control verbs (`common/*`, `localization/*`, `navigation/start`): the String data CARRIES an inner pair of double quotes — the message data is `"common/get_map_id"` including the quotes.
- Goals (`navigation/set_goal_pose/x/y/yaw`): BARE path, no inner quotes.

Coordinates are x/y/yaw in meters and radians, in the map frame. Project yaw convention: `yaw = 2*atan2(qz, qw)`, wrapped to [-pi, pi].

**Bringup sequence (app-free, as implemented in the patrol — see 5.7 for the code):**

1. `common/get_map_id` -> success + map id. Doubles as the MCU liveness check: silence means the USLAM service is down, and the only fix is a full dog power-cycle. It also establishes DDS discovery for everything that follows.
2. `localization/stop` first, IF a localization is already running. Seeding and starting on top of a live localization FAILS: verified 2026-07-30, three `localization/start` calls each returned success and then `[Localization] initialization failed!` about 6 s later. The phone app always sends stop first; the patrol now mirrors it, which makes bringup idempotent from cold, running, or failed-dirty state (14.2 s measured).
3. `localization/set_initial_pose/x/y/yaw` -> success + 6-DOF echo.
4. `localization/start` -> wait for "uslam is initialized!" / "initialization succeed!" on `/uslam/server_log`.
5. The dog MUST be moving for step 6 to succeed — localization only tracks while the dog moves. The patrol sends a small in-place rotate goal (the "nudge") to start tracking.
6. `navigation/start` -> "navigation/start/success", state WAITING.
7. `navigation/set_goal_pose/x/y/yaw` -> TRACKING -> `navigation/state_transition/REACHED` -> WAITING. After REACHED the dog holds position until the next goal; no pause logic is needed.

Failure tokens on `/uslam/server_log`: `NO_PATH`, `GOAL_CANCELLED`, `FAILURE`, `GOAL_POINT_UNREACHABLE`.

**Map lifecycle.** USLAM auto-starts on the MCU at power-on and auto-loads the LAST map. A normal cold boot needs no phone app at all — app-free bringup is verified on the Jewel map (2026-07-30: seed -> `localization/start` -> success in ~6 s, pose corrected 0.083 m off the seed, whole bringup 18.5 s). The app is needed only to: record a new map, select and localize on a DIFFERENT map than the last-loaded one, and diagnose crashes. Recorded maps persist on the dog; recording a new one does not delete old ones. One field exception (2026-07-09): on a freshly recorded map the CLI cold-init did not take — localization on a brand-new map needed the app once.

**Hard-won operational facts. These are the reasons the patrol works; read them before touching anything that talks to USLAM.**

- **RAPID REPEATED CONTROL VERBS WEDGE THE MCU** (the single most important operational fact, learned 2026-07-30). Two independent wedges, same mechanism: ~46 joystick-triggered USLAM stops produced 4516 `TIMEOUT_ODOMETRY` events; six `navigation/start` messages in 26 s drove the TIMEOUT count from 1 to over 1500 within minutes. Design rule: **send a verb ONCE, wait for its reply token, and never stack retry loops on top of a repeating sender.** The patrol's `send_verb` and `send_goal` defaults are now `repeat=1` (they were 3, added against DDS discovery loss; discovery is already established by the `get_map_id` handshake). A wedged MCU still answers `get_map_id` normally while the robot is unusable — liveness checks look fine. Seeing `TIMEOUT_ODOMETRY` and `TIMEOUT_POINTCLOUD` together = power-cycle the dog; do not hunt for it on the Orin. (`ros2 topic hz` showing nothing on `/utlidar/*` or `/lowstate` is a QoS artifact — sensor topics are best-effort, `hz` subscribes reliable; judge by publisher counts on `/uslam/*` topics.)
- **Pressing a button on the remote STOPS USLAM.** `/uslam/server_log` prints `Joystick button is pressed! Uslam is stopped now!`. Localization is torn down outright; the patrol loses tracking and `navigation/start` refuses. Treat the remote as e-stop only: pressing it ends that patrol run. Reposition the dog by hand BEFORE launching, never during.
- **`localization/start` returning success does NOT mean the dog is localized in the map.** Seed `set_initial_pose` with a pose the dog is not actually at, and USLAM still accepts it, returns success, and starts publishing `/uslam/localization/odom` — but it is integrating odometry from that seed, with no map match. The pose stream looks perfect (smooth, zero discontinuities over 214 s at Jewel) while being anchored in the wrong place; the error grows with distance (~5 m offset after ~10 m of walking). A whole waypoint survey done in such a frame is void — this is exactly what killed the first Jewel map on 2026-07-29.
- **The only trustworthy signal is `/uslam/server_log`.** A real relocalization failure prints `[Localization] initialization failed!` ~5 s after start and auto-sends `localization/stop`; `localization/get_status` answers `status/0` when not localized. A genuinely successful relocalization shows a visible correction between seed and converged pose (0.44 m on the 2026-07-29 success) — that correction is the signature of a real scan match. The patrol's own "localization initialized." log line once matched on a run whose frame was ~5 m out, so it is not proof either; cross-check the reported pose against a known physical spot before trusting a survey.
- **Localization init is intermittent, ~50-60% per attempt, cause never identified.** Identical seed, identical dog position: failed at 12:56, succeeded at 13:04 on 2026-07-30. The patrol retries the whole stop -> seed -> start sequence up to `LOCALIZE_ATTEMPTS = 4` (all-four-fail probability ~6%).
- **Mislocalization signature:** EVERY goal fails instantly with NO_PATH/FAILURE — even a zero-distance goal — and the dog shuffles and spins without departing (measured: 5.6 m of cumulative path for 0.07 m of net displacement and 358 degrees of yaw). Do not chase waypoint clearance for this; clearance measured in a bad frame proves nothing. Recovery = re-localize in the app, confirm via the server_log success token, then re-survey the waypoints in the same session. Do NOT re-record the map.
- `/uslam/localization/odom` only publishes while the dog moves. Standing still = no frames. Normal, not a fault.
- **Obstacle avoidance must be ON or odom is silent entirely** (found 2026-07-09). Avoidance OFF on the remote kills `/uslam/localization/odom`. Symptoms: `pose.py` reports "NO ODOM within 20s"; `wp_survey.py` records zero points while the dog visibly walks. Re-enable avoidance and the topic returns.
- Waypoints too near walls: the planner silently reroutes or refuses. Keep clearance; the patrol fails loudly instead of guessing. Clearance ANALYSIS is not possible on this stack: `/uslam/cloud_map` never publishes and `/uslam/localization/cloud_world` carries live lidar returns (bystanders read as obstacles). Judge waypoints by whether a patrol reaches them, not by computed clearance.
- USLAM treats every `set_goal_pose` as a new goal. A repeat arriving after TRACKING starts raises `GOAL_CHANGED` and kills the goal the script is waiting on — this is why `send_goal` must stay at `repeat=1` (with `repeat=3` waypoints failed at random).
- `ros2 topic pub -r N` (continuous publish) drops the SSH session — always `--once` with 1–2 resends.
- The CycloneDDS Python binding segfaults intermittently; the first run after `sudo reboot` is clean.

## 5.6 Map profiles: map_profiles.md

`robot/map_profiles.md` holds the full parameter set for every recorded map: an `INITIAL_POSE` dict and a `WAYPOINTS` list, ready to paste into `go2_patrol_gated.py`. Switching maps is a three-step procedure:

1. Select the map in the app once (localize on it).
2. Paste that map's block into `go2_patrol_gated.py` (INITIAL_POSE + WAYPOINTS).
3. Push the script to the Orin.

Nothing else changes, because the patrol script reads the live map id from `get_map_id` at bringup — there is no hardcoded map id anywhere in the code.

**The maps, newest first:**

- **JEWEL SITE v2 — `1BEC7FFDF97C47AC8BD751143D3FE187` (CURRENT, the zone of record).** Re-mapped by Runzhe on site 2026-07-29 after the first Jewel map proved unusable. Surveyed by walking the route twice with the continuous pose logger and extracting the dwell points (round-to-round repeatability 0.9–2.4 cm; a 14 m loop closed to 0.17 m). Validated by three consecutive 5/5 autonomous patrols on 2026-07-30. Route: `wp1 -> zone1 -> zone2 -> zone3 -> wp_return`, capture at the three zones only; `wp1` and `wp_return` carry `"capture": False`.
- **RETIRED — first Jewel map `F0E056FC045649B7BE3BDFF92FC54363`** (2026-07-29, unusable). Every surveyed coordinate on it is void: the survey ran in an odometry frame that never matched the map (see 5.5, the localization-success trap). Kept in `map_profiles.md` as the documented failure case, including the retracted clearance analysis.
- **PRIMARY — lab room `04114624684C4194B7008EDB3A5642D2`.** Recorded W13, re-surveyed 2026-07-03, validated 4/4 REACHED (~50 s loop) by `tests/wp_test_2.py`. Historical: the W15 roadmap item "switch back to the PRIMARY map" became obsolete on 2026-07-29 when the Jewel map became the zone of record.
- **DEMO — showcase route `DACB71661B5A48D48C6631AB2480C611`.** Recorded 2026-07-09 for the W14 NP Robotics Centre SPF showcase. Capture at wp1 in place; ends at wp3, no return leg. wp2 was re-picked after the original spot proved to be a planner dead zone (see `wp2_probe.py`, 5.9). Also historical.
- **RETIRED / dead:** client-test rescan `7853B2C397A44F8EB317D1C12D5B1F1C` (discarded 2026-07-09), `7E7AABAE...` (W12, unplannable), `DA230BE6...` (old room). Do not use.

**Two settings the Jewel route depends on** (recorded in `map_profiles.md`, present in the code): `send_goal` default `repeat=1` (see 5.5), and do not launch while driving the dog by remote — a nav goal fired into manual control stopped localization mid-run at Jewel and aborted the patrol at bringup.

NOTE: doc lag — `map_profiles.md`'s Jewel v2 block says `SKIP_LOCALIZATION_BRINGUP = True` is required and lists `INITIAL_POSE = (-5.001, -0.817, 1.249)` / `wp_return = (-5.013, -0.825, 1.374)`. The deployed code (synced from the Orin 2026-07-30, after the stop-first bringup fix) has `SKIP_LOCALIZATION_BRINGUP = False` — app-free bringup is now the verified default — and carries `INITIAL_POSE = wp_return = (-4.970, -0.657, 1.260)`. The code is what ran the three 5/5 validation patrols; follow the code. NOTE: doc lag — `docs/hardware.md` still names the retired `7853B2...` rescan as the active map; the live map is read at bringup anyway.

## 5.7 go2_patrol_gated.py — the gated patrol node

**Purpose.** One rclpy node that drives the whole demo loop: USLAM bringup, waypoint navigation, gimbal settle, frame capture, S3 upload, DynamoDB detection gate, next waypoint. Runs on the Orin (`~/go2/go2_patrol_gated.py`) under tmux or plain SSH.

**Configuration (constants at the top of the file, values as deployed):**

| Constant | Value | Meaning |
|---|---|---|
| `CMD_TOPIC` / `LOG_TOPIC` | `/uslam/client_command` / `/uslam/server_log` | USLAM control surface. Do not change. |
| `INITIAL_POSE` | `{x: -4.970, y: -0.657, yaw: 1.260}` | Jewel-map fallback localization seed = the dog's physical parking spot at launch (= `wp_return`). |
| `WAYPOINTS` | wp1, zone1, zone2, zone3, wp_return | The Jewel route in loop order. `"capture": False` on wp1 and wp_return = navigate only. Waypoint names become the S3 key segment, so zone names stay traceable in the dashboard. Optional per-point `cam` dict (see below). |
| `NUDGE_ENABLED` / `NUDGE_DELTA_YAW` / `NUDGE_SETTLE_S` | True / 0.30 rad / 8.0 s | Pre-navigation in-place rotate so localization starts tracking. |
| `NAV_START_ATTEMPTS` | 2 | `navigation/start` refusals are recoverable; each retry re-nudges harder (2x delta). |
| `SKIP_LOCALIZATION_BRINGUP` | False | False = full app-free bringup (stop -> seed -> start). True = trust an app-established localization and leave it alone. |
| `A8_IP` / `A8_CTRL_PORT` / `RTSP_URL` | `192.168.144.25` / 37260 / `rtsp://192.168.144.25:8554/main.264` | Gimbal endpoints. |
| `AWS_REGION` | `us-east-1` | |
| `CAMERA_ID` | `worm_cam` | Routes the S3 key to the worm camera config (renamed from `armyworm_go2_a8mini` in the 2026-07 camera-id rename). The production account `506868652945` row is also `worm_cam` (deployer-seeded as `camera-1`, re-keyed 2026-08-11), so this constant needed no change in the production repoint (5.12.6). |
| `S3_FRAMES_BUCKET` | `argus-frames-506868652945` | Production frames bucket (repointed 2026-08-13 from the dev-account `frames-armyworm-366356442579`). |
| `DDB_DETECTIONS` | `pest-monitoring-detections` | |
| `START_COUNTDOWN_S` | 3 | Time to clear the area and unplug the cable. |
| `GETMAP_TIMEOUT_S` | 8 | Silence beyond this = MCU USLAM down, abort. |
| `LOCALIZE_TIMEOUT_S` / `LOCALIZE_ATTEMPTS` | 30 / 4 | Localization init is intermittent (~50-60% per try); retry the whole sequence rather than abort. |
| `NAV_START_TIMEOUT_S` | 20 | Wait for `navigation/start/success`. |
| `NAV_REACH_TIMEOUT_S` | 90 | Per-waypoint max wait for REACHED. |
| `NAV_RETRY_ONCE` / `ABORT_ON_NAV_FAIL` | True / False | Retry a failed goal once; on final failure skip the point and continue (set True to abort instead). |
| `GIMBAL_SETTLE_S` | 1.5 | Settle after a LOCK override drove the gimbal to an absolute angle. |
| `FOLLOW_SETTLE_S` | 2.0 | FOLLOW mode lags the body ~1–2 s; wait this long after the dog stops before capturing. |
| `CAPTURE_WARMUP` | 30 | RTSP frames discarded before keeping one. |
| `DDB_GATE_TIMEOUT_S` / `DDB_GATE_POLL_S` | 150 / 1.5 | Detection-gate poll budget and interval; fail-open on timeout. The 150 s was originally sized under the processor Lambda's then-180 s timeout; the processor now runs at 600 s (1024 MB), so on a pathological frame the gate fails open before the Lambda finishes — acceptable, since the measured v6.3 envelope (24–54 s per tiled frame with full LLM verification) sits well inside the budget. |
| `LOC_OK_TOKENS` / `LOC_FAIL_TOKENS` | init-succeed strings / "initialization failed" | Without the fail tokens the script would wait out the full timeout on a failure it was already told about. |
| `NAV_FAIL_TOKENS` | NO_PATH, GOAL_CANCELLED, FAILURE, GOAL_POINT_UNREACHABLE | Goal-fatal tokens. |

AWS credentials are NOT in the file — boto3 reads the Orin's environment / `~/.aws` (the `cag_user` IAM user). Credential stored there, not reproduced here. Open item (5.12.6): the key on the device is still the dev-account `cag_user` key, which cannot write the production bucket — the repointed script only works end to end after the pending on-device credential swap.

NOTE: doc lag inside the code — the comment block above `INITIAL_POSE` still describes the PRIMARY lab-room map, but the values below it are the Jewel v2 set. The values are current; the comment is stale.

**Class `USLAMClient(Node)`** — the USLAM protocol driver:

- `_on_log(msg)` — subscriber callback; buffers each `/uslam/server_log` line as `(monotonic_ts, text)` in a 500-line deque and notifies waiters.
- `_on_odom(msg)` — subscriber on `/uslam/localization/odom`; keeps the latest pose (project yaw convention) plus its timestamp. This is the live pose feed the bringup seeds and nudges from.
- `live_pose(timeout=5.0, max_age=None)` — polls the stored odom pose every 0.1 s until one exists or `timeout` runs out; returns `(x, y, yaw)` or None. With `max_age` set, a pose older than that many seconds is refused (returns None): driving the dog by remote stops USLAM and freezes the last pose at where the dog USED to be, and seeding from that is worse than not seeding at all. Odom only flows while localization runs, so before bringup this correctly returns None.
- `now()` — monotonic timestamp; callers pass it as the `since` mark so `wait_for_any` only matches lines that arrived after the command went out.
- `wait_for_any(substrings, since, timeout)` — blocks until a buffered line at/after `since` contains any of the substrings; returns the matched substring or None on timeout. This is the entire event mechanism. Algorithm: on every wake it rescans the full 500-line deque and matches only lines stamped at/after `since` (the monotonic mark taken BEFORE the command went out, so a stale reply from an earlier command can never satisfy a new wait); the condition-variable wait uses the remaining time, so the deadline is exact and a matching line wakes it immediately.
- `_send_raw(data)` — the single wire writer: wraps the string in a `std_msgs/String` and publishes it once on `/uslam/client_command`. All quoting is decided by the two callers below; `_send_raw` adds nothing.
- `send_verb(verb, repeat=1, gap=0.4)` — publishes a control verb WITH the inner double quotes, `repeat` times with a 0.4 s gap. Default repeat is 1 (see 5.5, the MCU wedge rule; it was 3 until 2026-07-30).
- `send_goal(x, y, yaw, repeat=1, gap=0.4)` — publishes `navigation/set_goal_pose/x/y/yaw` as a BARE path. Repeat 1 for the same reason plus `GOAL_CHANGED`.

The two senders are the whole wire protocol in miniature — the quoting split and the repeat=1 rule are both visible in the code:

```python
def send_verb(self, verb, repeat=1, gap=0.4):
    log(f"  -> CMD (verb)  \"{verb}\"")
    for _ in range(repeat):
        self._send_raw(f'"{verb}"')        # verb rides WITH inner quotes
        time.sleep(gap)

def send_goal(self, x, y, yaw, repeat=1, gap=0.4):
    path = f"navigation/set_goal_pose/{x:.6f}/{y:.6f}/{yaw:.6f}"
    log(f"  -> CMD (goal)  {path}")
    for _ in range(repeat):
        self._send_raw(path)               # goal is a BARE path, no quotes
        time.sleep(gap)
```

The `repeat` loop is the 2026-07-30 fix frozen into the defaults. Repeat was 3 — a hedge against a missed first publish before DDS discovery settles — and that hedge is exactly what wedged the MCU (each duplicate verb counts against the wedge budget) and randomised waypoints (a duplicate `set_goal_pose` arriving after TRACKING raises `GOAL_CHANGED` and kills the goal in flight). Discovery is already established by the `get_map_id` handshake at bringup, so one publish is enough; callers keep the `repeat` parameter only so a future caller can raise it deliberately, never by default. (One doc lag inside the code: `send_verb`'s docstring still carries the "sent a few times" DDS-discovery rationale from the repeat=3 era. The shipped default is 1; the docstring is stale.)
- `bringup()` — the sequence of 5.5 as code:
  1. `get_map_id` with the 8 s liveness timeout — on silence it logs "POWER-CYCLE the whole dog" and returns False. It logs the map id line it got back (the live map id read — no hardcode) and establishes DDS discovery.
  2. Unless `SKIP_LOCALIZATION_BRINGUP`, loop up to `LOCALIZE_ATTEMPTS` times: pick a seed — a FRESH live pose (`max_age=3.0`) if odom is flowing, else `INITIAL_POSE` (in which case the dog must be parked on it); if the seed was live, send `localization/stop` and wait for its success (fresh odom is the proxy for "something is running"; no odom = nothing to stop, skip the churn); `set_initial_pose` with the seed; `localization/start`; wait on both OK and FAIL tokens. Break on success; otherwise sleep 2 s and retry the whole stop -> seed -> start sequence.
  3. If `NUDGE_ENABLED`: send one goal at the current (or seed) position with yaw + 0.30 rad, sleep `NUDGE_SETTLE_S`. The nudge is deliberately NOT verified — a pose-change check was tried on 2026-07-30, reported "did not move" three times per run while `navigation/start` then succeeded first try, and cost 37 s per run without ever changing an outcome.
  4. `navigation/start`, up to `NAV_START_ATTEMPTS` tries; each refusal re-nudges with double delta before retrying. Any failed stage returns False and the patrol aborts.
- `goto(x, y, yaw)` — send one goal, wait up to 90 s for `state_transition/REACHED` or a failure token; on failure or timeout, log FAIL-LOUD and retry once (`NAV_RETRY_ONCE`); returns True only on REACHED. The REACHED token and the four `NAV_FAIL_TOKENS` sit in ONE `wait_for_any` call, so a `NO_PATH` returns immediately instead of burning the 90 s budget; the retry sleeps 1 s and re-sends the identical goal (a second `send_goal` call, still repeat=1 — this is a new goal after the old one died, not a duplicate into a live one).

**Class `Gimbal`** — SIYI A8 control via the mzahana `siyi_sdk`, entirely fail-soft: any gimbal error only logs, the patrol never stops for it.

- `__init__` appends `~/a8/siyi_sdk` and `~/a8` to `sys.path` (this sdk build is flat-layout: `from siyi_sdk import SIYISDK`, not package-style).
- `connect()` — opens the UDP link to `192.168.144.25:37260` and calls `_set_follow()`. On ANY failure it returns False and the patrol runs without gimbal control.
- `_set_follow()` — `requestFollowMode()`: gimbal yaw tracks the dog's body. FOLLOW is also the saved power-on default (5.11), so this call is defensive, not load-bearing.
- `apply_override(cam)` — the per-waypoint camera override interface. A waypoint may carry `"cam": {"pitch_down": deg 0..25, "yaw_offset": deg (+ = lens right), "zoom": absolute (1.0 = wide)}`. Absolute angles need LOCK mode, so this calls `requestLockMode()`, then `requestSetAngles(yaw_off, pitch_dn)` (pitch positive = down on this inverted mount), then `requestAbsoluteZoom` if zoom is given. No deployed waypoint currently carries `cam`; the interface exists for future fine-tuning.
- `restore_follow()` — back to FOLLOW right after the capture at an override point.
- `close()` — disconnect at patrol end; one benign `[Errno 9] Bad file descriptor` is normal.

**Capture / upload / gate functions:**

- `capture_frame()` — opens the A8 RTSP stream with OpenCV over TCP (`OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp` is set before `import cv2`), reads `CAPTURE_WARMUP = 30` frames keeping the LAST good one (draining the decoder's stale buffer so the kept frame is current), JPEG-encodes at quality 92, returns bytes or None. Frames are 1920x1080 (the RTSP cap). Three failure exits, each one log line and None: stream will not open, stream opened but zero frames decoded, JPEG encode failed — the caller then skips upload and gate for that point and the patrol continues.
- `upload_frame(s3, waypoint_name, jpeg_bytes)` — `put_object` to `frames/{CAMERA_ID}/{waypoint}/{UTC timestamp}.jpg` in `argus-frames-506868652945`. The `worm_cam` key segment routes the frame to the armyworm camera config cloud-side, where tiling and the LLM gate apply. Returns the S3 key. It does not catch its own exceptions — the caller in `main()` wraps it and skips the gate on failure.
- `wait_for_detection(ddb_table, image_key)` — polls `pest-monitoring-detections` with a Key query on `image_id == the S3 key` (`Limit=1`), every 1.5 s for up to 150 s. **The processor's `put_item` is UNCONDITIONAL — clean frames (zero detections) also write a record — so the gate cannot dead-lock on a clean waypoint.** When the record appears it logs `detected`, `target_confidence`, and box count, and opens the gate. A query exception inside the loop only logs and retries on the next poll — a transient DynamoDB or network error cannot abort the patrol. On timeout it proceeds anyway (fail-open) with a loud log line. Measured end-to-end on site: 10–47 s per frame depending on the detection config (13/13/10 s gates on the 2026-07-29 5/5 run; 19/20/19 s on the 2026-07-30 zero-retry run). The current cloud side — processor v6.3 (dead code stripped 2026-08-07) with the v9-family model, tiling, the Sonnet 4.6 verify gate, display floor 33 (`post_verify_floor`; the 2026-08-10 decision was 49, refitted per-build to 33 on 2026-08-13) — measures 24–54 s per tiled frame, still inside the 150 s gate budget.

**`main()` — the loop.** Build boto3 S3 + DynamoDB clients; `gimbal.connect()` once for the whole patrol; init rclpy and spin `USLAMClient` in a daemon thread. Print the safety banner and the countdown (this is when the operator unplugs the cable). `bringup()` or abort. Then for each waypoint in order: `goto()`; on failure count it and (since `ABORT_ON_NAV_FAIL=False`) skip to the next point. On REACHED: if `"capture": False`, continue (navigate-only). Otherwise apply the `cam` override if present; sleep `GIMBAL_SETTLE_S` (override applied) or `FOLLOW_SETTLE_S` (pure FOLLOW catching up after the dog's final in-place rotation); `capture_frame()`; restore FOLLOW if an override was applied; upload; `wait_for_detection()`. Every scan step fails soft: a None frame or an S3 upload exception logs, skips the gate for that point, and continues the route — only bringup failure aborts the patrol. Finish with a reached/failed summary; the `finally` block closes the gimbal and shuts rclpy down even on Ctrl+C.

**Data in/out per scan waypoint:** USLAM goal out on `/uslam/client_command`; REACHED in on `/uslam/server_log`; one JPEG in memory from the A8 RTSP; one S3 object out; one DynamoDB record polled in. Nothing is stored on the Orin.

**Validation record (2026-07-30, Jewel).** Three consecutive 5/5 runs, app-free, cold boot and back-to-back both proven; best run 150 s with zero retries (`robot/_archive/2026-07-30/patrol_run4_clean_zero_retries.log`). Judged by what the demo needs — the three zone captures — earlier runs that day, before the repeat=1 fix, scored 3/3, 3/3, 2/3; the failures cluster on `wp1`, the first goal after bringup (navigation tracking not fully warmed), and cost nothing because `wp1` captures nothing. One measured caveat: at the moment REACHED fires the believed pose is within 0.013–0.041 m of the goal, yet the dog physically parks short and the pose re-converges over the next minutes to 0.17–0.46 m off — localization carries error during the run and the planner declares arrival on the wrong estimate. Re-recording `wp_return` would not fix this; the dog would stop short of the new target too.

## 5.8 pose.py — one-shot pose grab

`robot/pose.py` (Orin: `~/go2/pose.py`) is the basic survey tool. Run once at a spot; it subscribes to `/uslam/localization/odom`, keeps the FIRST frame, prints exactly one line and exits:

```
x=  0.143  y= -0.009  yaw= -1.569
```

- Class `OneShot(Node)`: `_cb(m)` converts the quaternion to yaw with the project convention `2*atan2(qz, qw)` wrapped to [-pi, pi], stores the first result, and wakes main via a threading.Event.
- 20 s timeout. Because odom only publishes while the dog moves, "NO ODOM within 20s" usually just means the dog is standing still — nudge it with the remote and re-run. It is also the avoidance-OFF symptom (5.5). Exit code 0 on success, 1 on timeout.

Three levels of survey tooling exist, in the order they were built:

1. `pose.py` — one shot per point (nudge, run, read).
2. `tests/wp_survey.py` — hands-free: watches the odom stream for motion-then-silence (stand still ~5 s at each spot), records the last pose before each silence, and prints a paste-ready WAYPOINTS block. Sanity caps: minimum 0.25 m separation, max 12 points.
3. `tools/pose_logger.py` — continuous logger to `/tmp/pose_log.txt`; the Jewel v2 route was surveyed by walking the route twice with this running and extracting the dwell points afterwards. `tools/localize_only.py` complements it for parking spots: it does the localization half only (stop -> seed -> start, importing the patrol's own machinery) without the heading-destroying nudge, then reports the settled pose.

## 5.9 The tests harness (robot/tests/)

These scripts validate navigation WITHOUT touching `go2_patrol_gated.py` — pure USLAM, no gimbal, no S3/DDB. `wp_test_1` and `wp_test_2` keep their run logs next to them (the `.log` files are the data product); `wp_test_3` and `wp2_probe` have no repo logs. They predate the 2026-07-30 `repeat=1` rule, so their `send_verb`/`send_goal` defaults are still 2–3; run them as-is for lab regression only, and copy the patrol's repeat=1 discipline into anything new.

**`wp_test_1.py` — round 1.** Same wire protocol as the patrol, three safety gates before any traverse: (1) `get_map_id` silent -> abort (MCU down); (2) localization does not init -> abort; (3) zero-distance sanity goal (= the seed pose itself) rejected -> abort (dog not at start / bad localization). Then the route, one retry per point. Round 1 aborted in the field: an app re-calibration session between runs reset the navigation state, so the one-shot nudge no longer activated tracking and `navigation/start` timed out.

**`wp_test_2.py` — round 2, the lab 4/4 proof.** Replaces the one-shot nudge with a PATIENT LOOP: for up to `TRACKING_WINDOW_S = 240` it alternates nudge goals and `navigation/start` attempts while the operator wiggles the dog with the remote (the script prints ">>> WIGGLE THE DOG WITH THE REMOTE NOW <<<"). Real motion -> localization tracks -> `navigation/start` succeeds -> break. Then the zero-distance sanity gate, then the route. Result on the PRIMARY lab map: 4/4 REACHED in ~50 s from a cold boot, zero app involvement (2026-07-03). Every abort path prints a machine-greppable verdict line (`### TEST COMPLETE ### verdict=...`). Note the operator-wiggle technique is now superseded for patrols by the scripted nudge, and post-2026-07-30 the wiggle needs care: only remote BUTTON presses are proven to stop USLAM (the server log line is "Joystick button is pressed! Uslam is stopped now!"); stick-only driving was used successfully as the wiggle on 2026-07-03 without stopping USLAM, but no stick-only test exists after the 2026-07-30 finding. If wiggling, use sticks only, touch no buttons, and treat even that with caution.

**`wp_test_3.py` — same skeleton for the lab DEMO map.** Route sanity -> wp2 -> wp3, ending at wp3 (no return leg). The zero-distance sanity goal doubles as a rehearsal of that route's wp1 capture-in-place behaviour.

**`wp2_probe.py` — the planner-probe methodology.** Field lesson 2026-07-09, worth stating as a rule: **"the dog can stand there" does not mean "the planner will go there."** During the DEMO-map patrol, wp1 and wp3 REACHED but wp2 at (1.143, -0.176) was refused INSTANTLY with `FAILURE` twice. Instant refusal with everything else passing (zero-distance sanity OK, other goals OK) means the goal CELL is bad — edge of free space / too near an obstacle on a quickly-recorded map — NOT mislocalization (whose signature is EVERY goal failing, 5.5). The probe sends candidate goals and stops at the first one the planner accepts; that candidate becomes the new waypoint. Round 1 probed 5 candidates around the original spot — all refused (a genuine dead zone). Round 2 probed a physically different spot ~2.5 m away, which REACHED and was committed to the patrol, `map_profiles.md`, and `wp_test_3.py`. If no candidate is accepted: pick a physically different spot, or re-record the map slowly with full coverage of that area. Consequence for all future routes: validate every new waypoint with a probe goal, not just with `pose.py`.

**`nav_probe.py` — is the map plannable at all.** One short goal into floor the dog just drove across, from a verified-accurate localization. REACHED = the planner works, the waypoints were just in bad cells (fix coords). NO_PATH = the map itself is unplannable (re-record slowly, full coverage, loop closure). Its header carries the landmine warning: a stale SEED pose makes every goal NO_PATH and the verdict meaningless — always re-read odom first.

## 5.10 kvs-controller.service — the A8 live-stream producer

**Purpose.** Push the A8's H.264 RTSP feed to the Kinesis Video Streams stream `armyworm-cam-stream` whenever the dashboard's live-view toggle is on. Pure passthrough — no re-encode. Boot-persistent: the systemd service plus the `a8-link` autoconnect profile mean the stream capability survives an unattended Orin reboot with no operator action (verified; it also ran throughout the 2026-07-30 Jewel visit).

Three files:

- **`kvs-controller.service`** (systemd unit): `User=unitree`, `WorkingDirectory=/home/unitree/amazon-kinesis-video-streams-producer-sdk-cpp/build`, `ExecStart=/home/unitree/run_kvs_controller.sh`, `Restart=always` / `RestartSec=5`, `WantedBy=multi-user.target`.
- **`run_kvs_controller.sh`** (wrapper): exports `GST_PLUGIN_PATH` and `LD_LIBRARY_PATH` for the KVS producer SDK build; reads `aws_access_key_id` / `aws_secret_access_key` out of the standard `~/.aws/credentials` (the `cag_user` profile) with awk and exports them so the `kvssink` GStreamer element can authenticate — credentials stored there, not reproduced anywhere in the script or this manual; exports `CAMERA_ID="worm_cam"`; `cd`s to the SDK build dir (kvssink writes `./log/kvs.log` relative to CWD) and execs the Python daemon.
- **`kvs_controller.py`** (daemon): a reconcile loop.
  - `fetch_desired_state()` — `GET {API_BASE}/stream/status?camera=worm_cam` against the `pest-monitoring-api` API Gateway, once per reconcile cycle (`POLL_INTERVAL_SEC = 5`), with a 10 s HTTP timeout on each request. The code's default `API_BASE` is the production API `https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com` (repointed 2026-08-13; the dev account's `zwpcbivmsj` API is retired history). Plain HTTPS, no AWS credentials — this route is deliberately exempt from the dashboard's Cognito JWT authorizer so device pollers stay untouched by auth. Returns `(stream_enabled, kvs_stream_name)` parsed from the JSON body, or `(None, None)` on ANY error (URL, HTTP, JSON decode, socket — all caught), in which case the loop keeps its current state and retries next cycle.
  - `build_gst_args(stream_name)` — `gst-launch-1.0 rtspsrc location=rtsp://192.168.144.25:8554/main.264 protocols=tcp latency=0 drop-on-latency=true ! rtph264depay ! h264parse ! kvssink stream-name=<name> storage-size=512 aws-region=us-east-1 fragment-duration=2 max-latency=2`.
  - `is_gst_running()` — the one liveness check: True while the child-process handle exists and `poll()` reports it has not exited.
  - `start_gst(stream_name)` — refuses a double-start (`is_gst_running()` guard), then spawns `gst-launch-1.0` in its OWN process group (`preexec_fn=os.setsid`) with `GST_PLUGIN_PATH` / `LD_LIBRARY_PATH` injected into the child's environment. stdout/stderr are discarded on purpose — kvssink writes its own `./log/kvs.log` under the SDK build dir.
  - `stop_gst()` — kills the whole process group: SIGTERM first, wait up to 5 s, escalate to SIGKILL and wait 2 s more (gst-launch forks helpers, so group-kill matters; a process already gone is treated as stopped). Always clears the child handle.
  - `main()` — the reconcile loop, every 5 s: fetch desired state; a failed fetch just sleeps and retries, changing nothing. If the child died on its own, log its return code and clear the handle. Then reconcile: desired ON and not running -> `start_gst`, UNLESS `kvs_stream_name` came back empty — then it logs an error and starts nothing (the guard for a camera row with the toggle on but no stream name). Desired OFF and running -> `stop_gst`. The `(desired, running)` pair is logged only when it changes, so a healthy idle loop is silent. This loop is what turns the dashboard's DynamoDB `stream_enabled` flag into real video. `graceful_exit` (the SIGTERM/SIGINT handler) tears the pipeline down on systemd stop / Ctrl+C so no orphan keeps pushing to KVS.
  - NOTE: doc lag inside the file — `main()`'s docstring still says `camera=armyworm_go2_a8mini` (the code default and the exported env are `worm_cam`, the 2026-07 camera-id migration), and the module header plus the `build_gst_args` docstring still name the retired dev CLI profile `nbk2`. The operative values are the code's: `worm_cam` and the production `API_BASE`. The code wins.

The environment can override everything (`CAMERA_ID`, `API_BASE`, `RTSP_*`, `KVS_SDK_DIR`); the same daemon source drives the mini PC's `moth-cam-stream` instance with different env (Chapter 6).

Operational rules: the KVS pair is driven by the systemd service — do not run `run_kvs_controller.sh` by hand; restart with `sudo systemctl restart kvs-controller`. Live streaming and patrol capture were confirmed non-contending on the A8 RTSP (both can read the stream at once). One failure signature worth knowing: because the daemon only polls `/stream/status`, it stays `active (running)` even when the RTSP source is unreachable — the fault only surfaces when the dashboard actually enables the stream. This is exactly what happened during the 2026-07-29 HDMI detour.

Production status (2026-08-14): the daemon's default `API_BASE` targets the production API (repo mirror repointed 2026-08-13), and the production `worm_cam` row reads `kvs_stream_name = armyworm-cam-stream`, `stream_enabled = false` (verified against the live row 2026-08-14). The missing piece is the stream itself: the 2026-08-10 deploy ran with live view off, so NO KVS streams exist on the production account — create `armyworm-cam-stream` there (5.12.6) before enabling the dashboard toggle, or the daemon will start a pipeline with nothing to write to. The on-device unit also still needs the 5.12.6 device pass (mirror sync + credential swap; `kvs-controller.service` may carry its own `Environment=` overrides for `API_BASE` — check both units during the pass). Whether the demo needs live view on the production account is an open call.

## 5.11 Gimbal control map (SIYI A8 Mini)

The control mapping on this unit (mount=2, unfixable in firmware) — needed by anyone extending the `cam` override interface:

- **FOLLOW mode** — gimbal yaw tracks the dog's body. This is the wanted default. It is saved as the POWER-ON default in SIYI PC Assistant (work mode -> Follow -> save) and persists across reboots, so the SDK never needs `requestFollowMode()` at startup (the patrol still calls it defensively on connect, which is harmless).
- **The LOCK boot bug and its fix.** Root cause of an early field failure: the A8 was booting in LOCK mode, so the gimbal did not track the dog's heading and captures pointed the wrong way. Fix = save FOLLOW as the power-on default in SIYI PC Assistant, as above. Fixed and closed; if captures ever point wrong again after a gimbal swap or firmware flash, re-check this setting first.
- **LOCK mode** is required for absolute angle commands (`requestSetAngles`). Yaw is 1:1 (+ = lens right). **Pitch is INVERTED: + = lens DOWN**, clamped to [-90, +25], command 0 = level-forward.
- `getAttitude()` pitch reads back WRAPPED — do not use it for control.
- `[Errno 9] Bad file descriptor` on disconnect is benign.
- `FOLLOW_SETTLE_S = 2.0` in the patrol exists because FOLLOW lags the body by ~1–2 s; capture without the wait gets a mid-swing frame.
- Firmware note: `docs/hardware.md` records fw 0.2.8 for the control-mapping findings; the 2026-07-29 `a8_status.py` readout after the ethernet revert reported fw 09030073. No repo source reconciles the two ids and no flash is recorded; the control mapping has not been re-characterized against the second number. Settle it by re-reading the unit (SIYI PC Assistant or `a8_status.py`) before extending the `cam` override interface.

## 5.12 Operations and reproduction

### 5.12.1 Cold boot sequence

1. Power on the Go2. USLAM auto-starts on the sport MCU and auto-loads the last map (currently Jewel v2). No app needed for a normal boot.
2. The Orin boots with it. `kvs-controller.service` starts on its own; `a8-link` autoconnects to the gimbal; the WiFi profile with the highest priority that is in range associates on its own (verified for `Apps@Jewel` after a power cycle, including the 1970 -> real clock correction). Nothing to do.
3. If the loaded map is NOT the one you intend to patrol: open the Unitree app once, select the map, localize on it. This is per-map-switch, not per-boot.
4. Confirm obstacle avoidance is ON on the remote (odom is silent otherwise, 5.5).
5. First ROS run after a reboot is the clean one (the CycloneDDS binding segfaults intermittently otherwise). If a script segfaults at import, `sudo reboot` and run again.

### 5.12.2 Running a patrol

Standing procedure, exactly as validated on 2026-07-30:

1. Park the dog physically on `INITIAL_POSE` = `wp_return` = `(-4.970, -0.657, 1.260)`. Do any manual repositioning NOW — never mid-run.
2. SSH in and launch (tmux keeps the run alive if the SSH session drops):

```
ssh unitree@<orin-ip>
tmux new -s patrol
# interactive shell: answer 1 (foxy) at the fishros prompt, then:
source ~/setup_go2.sh
python3 ~/go2/go2_patrol_gated.py
```

3. During the countdown: unplug the external cable (untethered rule), clear the area, keep the remote in hand as the e-stop — and remember that pressing ANY button on it stops USLAM and ends the run (5.5). Detach tmux with `Ctrl+b d`.
4. Watch the log: bringup (map id -> localization attempt N -> nudge -> navigation started), then per waypoint REACHED -> Captured -> Uploaded -> GATE open.

Which `<orin-ip>` works depends on the site: at NP, the campus WiFi address; at Jewel, `Apps@Jewel` has AP client isolation, so the laptop cannot reach the Orin over that SSID — use the wired `192.168.123.18` link for setup (unplug before motion) or the `iPhone Air` hotspot profile for untethered supervision.

This manual launch is still how every patrol run to date has been started. 5.13 covers the systemd daemon that automates the launch step itself (not the physical safety steps, which stay manual) against the dashboard's existing Schedule panel.

Before a run where you want real detections: start the Rekognition Custom Labels model (the processor writes clean records when the model is stopped — the gate still opens, but with no boxes). On the production account you do not need to remember the stop: the `pest-model-watchdog` Lambda (the v6.2 per-camera build, on a 15-minute EventBridge schedule) auto-stops the endpoint once it has run past the camera row's `max_runtime_min` — 45 minutes on `worm_cam` — so start the model shortly before the patrol and expect it to be stopped for you afterwards. Model start/stop procedures are in Chapter 3; the detection configuration the gate waits on is in Chapter 2.

### 5.12.3 Surveying and validating a new route

1. Record the map with the app (drive slowly, full coverage, loop closure). Keep clearance from walls and low planting.
2. Localize on the new map and CONFIRM it against `/uslam/server_log` — a success token plus a visible seed-to-converged correction (5.5). Do not trust `localization/start/success` alone: a survey taken in an unmatched frame is void, which is how the first Jewel map was lost.
3. Survey the points: walk the route with `tools/pose_logger.py` running and extract dwell points (best), or stand at each spot for `tests/wp_survey.py`, or take `pose.py` one-shots. Cross-check at least one point against a known physical spot.
4. Add a new map block (INITIAL_POSE + WAYPOINTS) to `robot/map_profiles.md`.
5. Validate nav-only first (copy the `wp_test_3.py` skeleton with the new coords). Any instantly-refused point: probe alternatives with the `wp2_probe.py` pattern — a probe goal, not `pose.py`, is what proves a waypoint (5.9).
6. Only then paste the block into `go2_patrol_gated.py` and push to the Orin.

### 5.12.4 SSH conventions (from the laptop)

- Remote ops use **Posh-SSH** (PowerShell module, password auth) from the Win11 laptop.
- Non-interactive exec channels get no ROS from .bashrc (the fishros block prompts and bails). Prefix every ROS command with the triple source: `source /opt/ros/foxy/setup.bash && source ~/cyclonedds_ws/install/setup.bash && source ~/setup_go2.sh && <command>`.
- Long jobs: launch detached — `nohup python3 -u ~/go2/tests/wp_test_2.py > ~/go2/tests/wp_test_2.log 2>&1 &` — then poll the log file.
- Killing a detached job from an exec channel: `pkill -f` must use a NON-self-matching pattern, e.g. `pkill -f "[w]p_test"` — a plain pattern matches the pkill command line itself.
- `ros2 topic pub` with `-r N` (continuous) drops the SSH session — use `--once` and resend 1–2 times. Post-2026-07-30 caveat: resends of control verbs count against the MCU wedge budget (5.5) — resend sparingly and only when the reply token did not come back.
- Writing files to the Orin over SSH: `nano` is installed; heredoc `cat > file << 'EOF' ... EOF` works for scripted writes.
- The AWS CLI on the Orin is v1 (parameter format differs from v2 on the laptop, notably for `kinesisvideo-archived-media`).

### 5.12.5 Failure triage

| Symptom | Diagnosis | Recovery |
|---|---|---|
| `get_map_id` silent (patrol logs FATAL) | USLAM service crashed on the MCU | Full power-cycle of the dog. Nothing on the Orin can restart it. |
| `TIMEOUT_ODOMETRY` + `TIMEOUT_POINTCLOUD` flooding `/uslam/server_log`; dog only turns in place; `get_map_id` still answers | MCU wedged by rapid repeated control verbs / stops | Power-cycle the dog. Then find and remove whatever was spamming verbs. |
| EVERY goal instantly NO_PATH/FAILURE, even zero-distance; dog shuffles without departing | Mislocalization (frame never matched the map, or localization went stale) | App re-localize, confirm via server_log token + seed correction, re-survey same session. Do NOT re-record the map. |
| ONE goal instantly FAILURE, others fine | Bad goal cell (dead zone) | Probe alternatives (`wp2_probe.py` pattern); move the waypoint. |
| `localization/start` succeeds then `initialization failed!` ~6 s later, repeatedly | Started on top of an already-running localization | Send `localization/stop` first (the patrol does this since 2026-07-30). |
| Localization init fails intermittently with a good seed | Known ~50-60% per-attempt flake, cause unidentified | Let `LOCALIZE_ATTEMPTS = 4` retry; park the dog on `INITIAL_POSE` if odom is not flowing. |
| Patrol aborts at bringup right after the dog was driven by remote | A button press stopped USLAM / the frozen stale pose was refused as seed | Re-launch; position the dog by hand only BEFORE launching. |
| `pose.py` "NO ODOM"; survey records nothing while the dog walks | Obstacle avoidance toggled OFF | Re-enable avoidance on the remote. |
| `navigation/start` never succeeds | Dog not moving, so localization not tracking | Let the retry re-nudge (it doubles the delta); or raise `NUDGE_DELTA_YAW`. |
| Python segfault at CycloneDDS import | Intermittent binding fault | `sudo reboot`; first run after reboot is clean. |
| Captures point the wrong way | Gimbal booted in LOCK | Re-save FOLLOW as power-on default in SIYI PC Assistant (5.11). |
| Gimbal or RTSP unreachable at `192.168.144.25` | `a8-link` not up — MAC bind mismatch or a rogue profile holding the NIC | `nmcli con show`; re-bind `a8-link` to the fitted dongle's MAC; keep `Wired connection 2` autoconnect off (5.3). |
| No live stream on dashboard, service "active (running)" | Controller only polls; source or toggle is the fault | Check `GET /stream/status?camera=worm_cam` returns `stream_enabled=true`; ping `192.168.144.25`; `systemctl status kvs-controller` last. |

### 5.12.6 Reproducing the AWS side on a new account

This is no longer hypothetical: the procedure was executed for real on 2026-08-10/11, when the ARGUS deployer (Chapter 7) stood up the full cloud stack on the NP production account `506868652945` in 103 s (all 15 stages). That deploy fixes the names the devices must use at cutover:

- Frames bucket: `argus-frames-506868652945` (ARGUS naming, not the `frames-armyworm-<account-id>` pattern of the dev account).
- Armyworm camera id: `worm_cam`, unchanged — the deployer seeded the row as `camera-1` but it was aligned to the reference id on 2026-08-11 (plus `manual_upload`; a `moth_cam` row was seeded separately for the moth rebuild). The Orin's `CAMERA_ID` stays `worm_cam`.
- API Gateway: `https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com`; dashboard `https://d1dtoxef7qmugl.cloudfront.net`.
- KVS: skipped (live view off) — no streams exist on the production account, so the `kvs-controller` service has nothing to drive there yet.

**Where the cutover stands (2026-08-14).** The cloud side is done: the stack runs on production, both models are retrained there (armyworm `v9r-prod-20260810`, moth `moth-prod-20260811`), and the repo mirrors of the edge scripts were repointed to the production values on 2026-08-13. The DEVICE side is not: the hardware was away when the repoint was made, so the Orin and mini PC still run the old script copies with dev-account `cag_user` keys, and still upload to the dev account until the pending device pass (sync the mirrors onto the devices, swap `~/.aws/credentials` to a production-account key). The generic procedure below remains the reference for any future fresh account (and for creating the pieces the deployer skipped, like the KVS stream and the device IAM user).

The edge scripts touch four AWS resources. On a fresh account, create/verify each (region `us-east-1` throughout; full backend creation order is in Chapter 2, and the ARGUS deployer of Chapter 7 automates most of it):

- **Frames bucket.** Console: S3 -> Buckets -> Create bucket -> name it (the pattern used here is `frames-armyworm-<account-id>`). CLI:
  `aws s3 mb s3://**your-frames-bucket** --region **us-east-1**`
  Verify the patrol's uploads land — Console: S3 -> **your-frames-bucket** -> `frames/worm_cam/`. CLI:
  `aws s3 ls s3://**your-frames-bucket**/frames/worm_cam/ --recursive --region **us-east-1**`
- **Detections table.** Console: DynamoDB -> Tables -> `pest-monitoring-detections` -> Explore table items -> Query, partition key `image_id` = the S3 key the patrol logged. CLI:
  `aws dynamodb query --table-name **pest-monitoring-detections** --key-condition-expression "image_id = :k" --expression-attribute-values "{\":k\":{\"S\":\"**frames/worm_cam/zone1/<timestamp>.jpg**\"}}" --region **us-east-1**`
- **KVS stream.** Console: Kinesis Video Streams -> Video streams -> Create video stream -> name `armyworm-cam-stream`. CLI:
  `aws kinesisvideo create-stream --stream-name **armyworm-cam-stream** --data-retention-in-hours **24** --region **us-east-1**`
- **Device IAM user.** Console: IAM -> Users -> Create user (the pattern here is `cag_user`) -> attach a policy allowing `s3:PutObject` on the frames bucket, `dynamodb:Query` on the detections table, and the KVS producer actions (`kinesisvideo:DescribeStream`, `kinesisvideo:GetDataEndpoint`, `kinesisvideo:PutMedia`) -> create an access key. Then on the Orin run `aws configure` as user `unitree` and enter the key pair there — the key lands only in `~/.aws/credentials` on the device, never in any script or repo file. CLI (policy attach):
  `aws iam put-user-policy --user-name **cag_user** --policy-name **edge-device-policy** --policy-document file://**policy.json**`

Then edit the constants at the top of `go2_patrol_gated.py` (`S3_FRAMES_BUCKET`, `CAMERA_ID`, table name if changed) and the `API_BASE` / `CAMERA_ID` env for `kvs_controller.py` to point at the new account, push both to the Orin, and `sudo systemctl enable --now kvs-controller`. For the 2026-08 production cutover specifically, the repo-side edits are ALREADY DONE (2026-08-13): `S3_FRAMES_BUCKET` = `argus-frames-506868652945` in `robot/go2_patrol_gated.py` and `robot/capture_4k_hdmi.py`, `API_BASE` default = `https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com` in `robot/kvs_controller.py`, and `CAMERA_ID` unchanged at `worm_cam` (the production row was re-keyed to the reference id 2026-08-11). Both production models are trained. What remains is the device pass: sync the mirrors onto the Orin (`~/go2/`), swap the on-device `~/.aws/credentials` to a production-account key, check `kvs-controller.service` for on-device `Environment=` overrides, and hold the kvs-controller start until a KVS stream exists on that account.

Security note for reproducers: never commit or paste AWS secret keys, device passwords, or tokens into any project file — the device `~/.aws/credentials` file is their only home on the edge. One historical caution: a Roboflow API key sits inside `datasets/archive/experiments/pre_v3_abandoned/download.py` in the repo (credential stored there, not reproduced); treat that folder as sensitive and do not publish it.

## 5.13 patrol-scheduler.service — auto-launch on the dashboard schedule

**Purpose.** Close a gap between the dashboard and the robot: the Schedule panel's `/schedule` row (Chapter 4) previously only drove `pest-camera-scheduler` (Rekognition model start/stop, Chapter 3) — nothing turned that same schedule into an actual patrol launch, so `go2_patrol_gated.py` stayed purely manual (5.12.2), unlike the KVS pair's systemd-driven poll loop (5.10). `patrol_scheduler.py` is the missing listener, built as a second daemon on the same pattern as `kvs_controller.py`.

Three files, same shape as the KVS trio in 5.10:

- **`patrol-scheduler.service`** (systemd unit): `User=unitree`, `WorkingDirectory=/home/unitree/go2`, `ExecStart=/home/unitree/run_patrol_scheduler.sh`, `Restart=always` / `RestartSec=5`, `WantedBy=multi-user.target`.
- **`run_patrol_scheduler.sh`** (wrapper): sources the ROS 2 Foxy environment non-interactively — `/opt/ros/foxy/setup.bash` -> `~/cyclonedds_ws/install/setup.bash` -> `~/setup_go2.sh` — then execs the daemon. This triple-source is required because `.bashrc`'s fishros block prompts foxy/noetic interactively (5.12.4); under systemd that prompt would just hang forever with no ROS environment loaded. The daemon's subprocess launch of `go2_patrol_gated.py` inherits this same sourced environment, so the patrol script's `rclpy` import and `/uslam/*` topics work without re-sourcing per run.
- **`patrol_scheduler.py`** (daemon): a reconcile loop, structurally close to `kvs_controller.py`.
  - `fetch_schedule()` — `GET {API_BASE}/schedule?camera=worm_cam` against `pest-monitoring-api`, once per `POLL_INTERVAL_SEC = 30`. Same reasoning as the KVS poller: plain HTTPS, no AWS credentials on the control path. Returns `{"enabled", "start_time", "days"}` or `None` on any error, in which case the loop just retries next cycle.
  - `schedule_matches_now()` — converts the dashboard's Singapore-local `start_time` against the current time computed as UTC+8 (fixed offset, no DST), the same conversion `_cron_expression` already does server-side for the EventBridge rule (Chapter 2); the daemon deliberately does not trust the Orin's own system timezone. An empty `days` list means every day, matching the API's own default.
  - **Safety arm gate — the reason this is not a plain cron job.** `go2_patrol_gated.py` needs a human physically present: remote in hand as e-stop, area cleared, external cable unplugged before motion (5.12.2). A scheduled trigger cannot satisfy any of that by itself, so the daemon only launches if `~/go2/.patrol_armed` was touched within the last `PATROL_ARM_MAX_AGE_MIN` (default 60) minutes — the human does the pre-flight check on site and then `touch`es that file shortly before the scheduled time. A schedule match with a stale or missing arm file is logged once per day and skipped, not forced through. `PATROL_REQUIRE_ARM=0` disables the gate; this is not recommended for any run at a real venue.
  - `launch_patrol()` — refuses a double-launch if a patrol subprocess handle is already alive, then spawns `go2_patrol_gated.py` in its own process group (`preexec_fn=os.setsid`, matching `start_gst()`'s pattern in 5.10), with output to a fresh timestamped file under `~/go2/patrol_logs/` — a scheduled run has nobody watching a tmux session, so the log file is the only record.
  - Dedup state (`~/go2/.patrol_scheduler_state.json`, `last_fired_date`) fires at most once per SGT calendar date, so the 30 s poll can't double-launch inside the matching minute, and a service restart mid-day does not refire a schedule that already ran.

Deploy: copy `patrol_scheduler.py` + `run_patrol_scheduler.sh` to `~/go2/` and `~/` on the Orin, `patrol-scheduler.service` to `/etc/systemd/system/`, then `sudo systemctl daemon-reload && sudo systemctl enable --now patrol-scheduler`.

**Status (2026-08-20): written and reviewed against the codebase, not yet deployed or dry-run on the physical Orin.** Before relying on it for anything unattended, run an on-site dry run: set a near-future schedule on the dashboard, `touch ~/go2/.patrol_armed` after the physical pre-flight check, and confirm the service actually launches the patrol and writes a log.

## 5.14 Cross-references

- **Chapter 1** — where this edge platform sits in the whole ARGUS architecture, and the fixed-cameras production design the Go2 stands in for.
- **Chapter 2** — the cloud half the gate depends on: `pest-detection-processor` (v6.3), its unconditional `put_item`, the S3 event wiring, tiling and the Bedrock LLM verification gate, API Gateway `vzfl7s6z00` on the production account (the dev account's `zwpcbivmsj` is retired history), the JWT-exempt `GET /stream/status` route, and the SGT->UTC cron conversion `patrol_scheduler.py` mirrors client-side.
- **Chapter 3** — Rekognition Custom Labels model versions, start/stop procedures and cost, and the holdout methodology behind the detection claims.
- **Chapter 4** — the dashboard: the Schedule panel that now drives both the model watchdog and (5.13) the patrol launch, the live-view toggle that `kvs_controller.py` obeys, the Gallery that renders the patrol's zone frames, the Test upload panel used for live demos.
- **Chapter 6** — the mini PC's parallel `moth-cam-stream` producer (same `kvs_controller.py` source, different env) and the Hikvision moth camera.
- **Chapter 7** — the ARGUS deployer, which reproduces the cloud resources of 5.12.6 on a fresh account (executed for real on `506868652945`, 2026-08-10).
- **Chapter 8** — the end-to-end reproduction runbook that sequences this chapter's procedures with the cloud-side ones.
