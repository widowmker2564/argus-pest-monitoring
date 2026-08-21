# Hardware + on-device

Passwords are documented separately, not in this file.

## Unitree Go2 EDU
- Sport MCU 192.168.123.161 (DDS only, no SSH, wired). **USLAM runs HERE, not on the Orin** —
  no `ps` visibility on the Orin; an MCU crash needs a full dog power-cycle.
- Jetson Orin Nano 192.168.123.18 (SSH user `unitree`, Ubuntu 20.04, ROS 2 Foxy, SSH alias
  `go2`). `unitree_ros2_example` binaries are at `bin/`, not `lib/<pkg>/` — run directly,
  `ros2 run` fails.

### Orin WiFi profiles (NetworkManager, RTL8821CU dongle on wlan0)
Managed with `nmcli`; all of them need `sudo` (the `unitree` user has no polkit session over
SSH, so bare `nmcli con up` returns "Not authorized to control networking").
- `npwireless` — NP campus **NPWirelessx**, PEAP/MSCHAPv2, no CA check, priority 100.
  **The campus IP is DHCP and it MOVES** — `10.1.125.24` (W10) became
  `10.1.122.128` by 2026-08-07. Do not hardcode it or trust it across a break; the
  pool is a /21 and the laptop usually lands in a different /21 again, so guessing
  or subnet-sweeping wastes time. Read it from the dog instead, over the wired
  link: `nmcli -t -f DEVICE,STATE,CONNECTION dev status` + `ip -br addr show wlan0`.
  Campus WiFi has **no client isolation** (verified 2026-08-07: laptop 10.1.47.8 →
  dog 10.1.122.128, ping + SSH both fine across the two /21s), so once you have the
  current address wireless SSH just works.
- `apps-jewel` — CAG's **`Apps@Jewel`** at Jewel, added 2026-07-28, priority 90.
  WPA2-Enterprise PEAP/MSCHAPv2, no CA check, identity `smart.pest@j.iot`,
  **hidden SSID** so `802-11-wireless.hidden yes` is required or association fails with
  `ssid-not-found`. Orin gets `10.38.19.10/23`, gateway `10.38.18.1`.
- `iPhone Air` — hotspot fallback.
- **The SSID is `Apps@Jewel`, with an "s".** `App@Jewel` matches nothing and the failure
  looks identical to being out of range.
- CA validation is off on both enterprise profiles on purpose: the Orin boots with a 1970
  clock, which would fail a certificate date check before `set-clock-from-http` can run.
- **`Apps@Jewel` has AP client isolation.** The Orin reaches the gateway and the internet
  fine, but laptop ↔ dog on the same /23 cannot ARP each other, so **direct SSH from the
  laptop over Jewel WiFi does not work** — see `docs/state.md` for the open item. Outbound
  is unrestricted (S3 + Rekognition TLS verified, ports 22 and 443 egress open), so the
  detection pipeline is unaffected; only interactive SSH is.
- The vendor RTL8821CU driver **cannot scan through `iw`** (`iw dev wlan0 scan` returns zero
  BSS even with wlan0 unmanaged). Use `nmcli dev wifi list` — that path works.

## USLAM navigation
Control surface is plaintext `std_msgs/String` on `/uslam/client_command` and
`/uslam/server_log`. No custom message types, no Nav2.
- **Cold boot is app-free** (verified 2026-07-03): USLAM auto-starts on the MCU at
  power-on and auto-loads the last map — `get_map_id` answers with no app touch,
  and a full 4/4 route ran from a cold boot. The old "phone app must activate SLAM
  once per power-cycle" note came from a W12 crash recovery and does NOT apply to
  normal startup. The app is only needed to record maps (and for crash diagnosis).
Sequence: `common/get_map_id` → `localization/set_initial_pose` → `localization/start` (wait
"initialization succeed!") → `navigation/start` → `navigation/set_goal_pose`.
- `navigation/start` only succeeds while localization is actively tracking (dog must be moving)
  — NUDGE before starting.
- `localization/odom` only publishes while the dog moves (standing still = no frames, normal).
- **Obstacle avoidance must be ON for odom to publish at all** (found 2026-07-09):
  toggling avoidance OFF on the remote silences `/uslam/localization/odom` completely —
  symptom is pose.py "NO ODOM within 20s" and wp_survey recording zero points even
  while the dog walks. Re-enable avoidance and the topic comes back.
- Reached signal: `navigation/state_transition/REACHED` on `/uslam/server_log`. The dog holds
  position until the next command.
- `ros2 topic pub -r N` (continuous) drops SSH — use `--once` with 1–2 resends.
- CycloneDDS Python binding segfaults intermittently — first run after `sudo reboot` is clean.
- Waypoints too near walls → planner silently reroutes; keep clearance. The gated loop must
  fail loudly.
- **`localization/start` on an ALREADY-RUNNING localization fails.** Verified
  2026-07-30: three `localization/start` calls each returned
  `localization/start/success`, then `navigation/state_transition/TIMEOUT_ODOMETRY`,
  `ABNORMAL`, and finally `[Localization] initialization failed!` about 6 s later.
  Consequence before the fix: a patrol could not be re-run without a power cycle.
  **Always send `localization/stop` first** — that is what the phone app does, and
  `go2_patrol_gated.py` now does it too (bringup step 2a), which makes bringup
  idempotent from cold, running, or failed-dirty state (14.2 s).
- **RAPID REPEATED CONTROL VERBS WEDGE THE MCU. This is the single most important
  operational fact learned on 2026-07-30.** Two independent wedges, same mechanism:
  - ~46 joystick-triggered USLAM stops → **4516** `TIMEOUT_ODOMETRY`
  - six `navigation/start` messages in 26 s → TIMEOUT count went 1 → 572 → 1500 → 338
    over three minutes, i.e. the wedge forms within seconds and is visible live
  `go2_patrol_gated.py` had `send_verb(repeat=3)` and `send_goal(repeat=3)` from the
  start, added against DDS discovery loss. That is 3 messages per command, and any
  retry loop wrapped around them multiplies it. **Both defaults are now `repeat=1`**;
  DDS discovery is already established by the `get_map_id` handshake that opens
  bringup. Worst-case verbs per bringup went from 21 to 6.
  **Design rule for anything talking to USLAM: send a verb ONCE, wait for its reply
  token, and do not stack retries on top of a repeating sender.** Retry loops felt
  like robustness and were in fact the thing breaking the robot.
- **Repeated USLAM stops wedge the MCU pipeline, and only a power cycle clears it.**
  After ~46 joystick presses on 2026-07-29/30, `navigation` emitted **4516**
  `TIMEOUT_ODOMETRY` + `TIMEOUT_POINTCLOUD` events, `/uslam/localization/odom` and
  `/lio_sam_ros2/mapping/odometry` both dropped to 0 publishers, and the dog would
  only turn in place. The USLAM command channel still answered `get_map_id`
  normally, so liveness checks look fine while the robot is unusable. Seeing both
  TIMEOUT tokens together = power-cycle the dog; do not hunt for it on the Orin.
  Note `ros2 topic hz` reporting no messages on `/utlidar/*` or `/lowstate` is a QoS
  artifact (sensor topics are best-effort, `hz` subscribes reliable) — judge by
  publisher counts on the `/uslam/*` topics instead.
- **Pressing a button on the remote STOPS USLAM.** `/uslam/server_log` says it in as
  many words: `Joystick button is pressed! Uslam is stopped now!` (captured
  2026-07-30). This is not "the remote competes with navigation" — localization is
  torn down outright, so the patrol loses tracking and `navigation/start` refuses.
  It explains the 2026-07-29 14:50 abort, where a `localization/stop` appeared in the
  log with nothing having sent it. **Treat the remote as e-stop only: pressing it
  ends that patrol run.** To reposition the dog by hand, do it before launching, not
  during.
- **App-free bringup is VERIFIED on the Jewel map (2026-07-30).** With the dog parked
  at `INITIAL_POSE` and no app involvement: `set_initial_pose` → `localization/start`
  → `[Localization] initialization succeed!` in ~6 s, pose corrected 0.083 m off the
  seed, `navigation/start/success`, whole bringup 18.5 s. The app is needed for
  recording maps only.
- **`localization/start` returning success does NOT mean the dog is localized in the map**
  (learned the hard way 2026-07-29 at Jewel). Seed `set_initial_pose` with a pose the dog
  is not actually at, and USLAM will still accept it, return
  `localization/start/success`, and start publishing `/uslam/localization/odom`. What it
  is doing is integrating odometry from that seed — no map match. The pose stream looks
  perfect: smooth, continuous, **zero discontinuities over 214 s and 10 m of walking**.
  It is simply anchored in the wrong place, and the error grows with distance travelled
  (~5 m of offset after ~10 m of walking).
- **The only trustworthy signal is `/uslam/server_log`.** A real relocalization failure
  prints `[Localization] initialization failed!` roughly 5 s after start and then
  auto-sends `localization/stop`. `localization/get_status` answers
  `localization/get_status/status/0` when not localized. Watch that topic directly —
  subscribing to both `/uslam/server_log` and `/uslam/client_command` also shows what the
  phone app is sending, which is how the app's own seed pose was recovered.
- The patrol script's `LOC_OK_TOKENS` check matched and logged "localization initialized."
  on a run whose frame was ~5 m out, so **that log line is not proof of a good frame
  either.** Cross-check against a known physical spot before trusting a survey.
- Symptom chain to recognise: every goal returns NO_PATH/FAILURE regardless of whether the
  target is in open space, and the dog shuffles and spins without departing (5.6 m of path
  for 0.07 m of net displacement, 358° of yaw). Do not chase waypoint clearance for this —
  clearance measured in a bad frame proves nothing. Fix the localization first.
- Active map: `7853B2C397A44F8EB317D1C12D5B1F1C` — **TEMPORARY** client-test rescan
  (2026-07-03 afternoon). The PRIMARY map remains `04114624684C4194B7008EDB3A5642D2`
  (validated 4/4); switch back after the test. Both parameter sets (INITIAL_POSE +
  WAYPOINTS per map) live in `robot/map_profiles.md`. Recorded maps persist on the
  dog. Survey tool: `~/go2/pose.py` (one-shot x/y/yaw grab). Patrol script
  `go2_patrol_gated.py` at `~/go2/`. Waypoints with `"capture": False` navigate only
  (no scan/S3/gate); wp1 and wp_return carry it.
  Stale localization symptom: EVERY goal instantly NO_PATH/FAILURE, even a
  zero-distance one — re-localize in the app, do not re-record the map.
- Any forward-motion route must be **untethered** — a connected cable can drag hardware off
  the table.

### Patrol scheduler (added 2026-08-20) — auto-launch on the frontend schedule
Before this, the dashboard's Schedule panel only drove `pest-camera-scheduler`
(Rekognition model start/stop) — nothing turned the schedule into an actual Go2
patrol launch. `robot/patrol_scheduler.py` closes that gap: a systemd daemon on
the Orin that polls `GET /schedule?camera=worm_cam` (the same row the dashboard
already writes) every 30s and launches `go2_patrol_gated.py` when the scheduled
SGT time hits, mirroring `kvs_controller.py`'s "poll the API, no AWS creds
needed for the control path" pattern.
- Deploy: `robot/patrol_scheduler.py` + `robot/run_patrol_scheduler.sh` →
  `~/go2/` and `~/` on the Orin; `robot/patrol-scheduler.service` →
  `/etc/systemd/system/`, then `sudo systemctl enable --now patrol-scheduler`.
  Starts on boot, `Restart=always` (same as `kvs-controller.service`).
- `run_patrol_scheduler.sh` sources the ROS env non-interactively
  (`/opt/ros/foxy/setup.bash` → `~/cyclonedds_ws/install/setup.bash` →
  `~/setup_go2.sh`) before exec'ing the daemon, because the `.bashrc` fishros
  block prompts foxy/noetic and would hang under systemd.
- **Safety gate — do not remove.** `go2_patrol_gated.py` needs a human present:
  remote in hand as e-stop, area cleared, external cable unplugged before
  motion. A scheduled trigger can't satisfy any of that on its own, so the
  daemon refuses to launch unless `~/go2/.patrol_armed` was touched within the
  last `PATROL_ARM_MAX_AGE_MIN` (default 60) minutes — a human must physically
  do the pre-flight check and then `touch ~/go2/.patrol_armed` shortly before
  the scheduled time, or the run is skipped (logged, not forced through).
  `PATROL_REQUIRE_ARM=0` disables the gate; do not set that for any run at a
  real venue.
- Per-run output goes to `~/go2/patrol_logs/patrol_<UTC-timestamp>.log` (nobody
  is watching a tmux session for a scheduled run).
- Dedup state (`~/go2/.patrol_scheduler_state.json`) fires at most once per SGT
  calendar date, so a 30s poll interval can't double-launch inside the matching
  minute, and a service restart mid-day doesn't refire.

## SIYI A8 Mini gimbal
IP 192.168.144.25, RTSP `rtsp://192.168.144.25:8554/main.264` (RTSP caps at 1080p; 4K is
SD-card / HDMI only, not RTSP). UDP control port 37260, fw 0.2.8.
- siyi_sdk (mzahana): add `~/a8/siyi_sdk` to `sys.path`, then `from siyi_sdk import SIYISDK`
  (flat layout, not package-style).
- FOLLOW mode: set in SIYI PC Assistant (work mode → Follow → save). Persists across reboots —
  no need to call `requestFollowMode()` in the SDK. Patrol waits `FOLLOW_SETTLE_S = 2.0` at
  each waypoint (gimbal lags the dog ~1–2s).
- Control mapping (fw 0.2.8, mount=2, unfixable): LOCK mode required for absolute angles; yaw
  1:1 (+ = lens right); pitch INVERTED (+ = lens DOWN, clamped [−90, +25], cmd 0 = level-
  forward). `getAttitude()` pitch reads wrapped — not for control. `[Errno 9] Bad file
  descriptor` on disconnect is benign.
- A8 XH power cable: GND wire was resoldered — needs strain relief (service loop) before
  sustained untethered operation.

### A8 via mini-HDMI + USB capture card (added 2026-07-29, verified on the Orin)
Runzhe moved the A8 off ethernet onto **mini-HDMI → USB capture card → Orin**. State as
found: `192.168.144.25` no longer answers, `eth0` (the USB ASIX) is DOWN, and the capture
card enumerates fine.
- Card is a **MACROSILICON MS2109** (`345f:2109`), driver `uvcvideo`, nodes `/dev/video0`
  and `/dev/video1`. Enumerated as USB **high speed = USB 2.0**.
- **It maxes out at 1920x1080** — formats MJPG and YUYV, largest frame size 1920x1080 in
  both. There is no 4K, no 1440p. So this path does **not** beat the RTSP 1080p cap; if
  4K was the reason for the change, this card cannot deliver it. USB 2.0 bandwidth also
  means 1080p is MJPG-only in practice (YUYV 1080p would be a few fps).
- `v4l2-ctl`, `ffmpeg` and `ffprobe` are all **not installed** on the Orin. Format
  enumeration was done with a raw V4L2 ioctl from `python3`.
- **`a8-link` is bound to the wrong dongle now.** The profile is MAC-locked to
  `6C:1F:F7:21:52:73`, but the ASIX adapter currently plugged into the Orin is an
  **AX88179B with MAC `6C:1F:F7:28:CD:0C`** — a different physical unit (the laptop dock
  has an ASIX too; they look identical). If the A8 ever goes back on ethernet, `a8-link`
  will not activate on this adapter. Fix = re-bind the profile MAC, or find the original
  dongle.

## Mini PC (KVS)
Win11 host, Ubuntu 22.04 VM (account `wilburteo`). NICs ens33 NAT + ens37 bridged go2-link
192.168.123.99/24. `kvs_controller.py` daemon → `run_kvs_controller.sh` → systemd
`kvs-controller.service` (User=wilburteo, Restart=always), polls `/stream/status`
every 5s. Hikvision moth cam 192.168.1.66, DDB row `moth_cam` (migrated 2026-07-14),
stream `moth-cam-stream`, `stream_enabled=true`.
**VM access recipe (AUTOMATIC since 2026-07-14):** the VM is NAT'd (ens33
192.168.189.x, egress appears as the Win11 host's campus IP, e.g. 10.1.67.21) — NO
inbound path exists from anywhere; ens37 (.99) only works when the wired dog-net
link is plugged. Remote path = `reverse-tunnel-fyp.service` on the VM (systemd,
enabled, self-healing: Restart=15s + ConnectTimeout=10 + ServerAlive 30x3,
StartLimitIntervalSec=0) keeps `-R 2222:localhost:22` up to the Orin whenever both
machines are on. From the laptop: SSH to the Orin (10.1.125.24 wifi, or .18
dog-net), then `ssh -i ~/.ssh/id_ed25519 -p 2222 wilburteo@localhost`. Key auth
BOTH ways (no passwords): Orin's id_ed25519 in the VM's authorized_keys, VM's key
in the Orin's. Adversarially verified: tunnel killed → self-healed in <45 s.
Known dependency: this path needs the Orin up; if the tunnel is up but connects
hang ("banner exchange" timeouts), kill the stale sshd pids on the Orin
(`sudo ss -tnp | grep <host-ip>` → kill) and the service rebinds within 15 s.
**Gotcha:** `CAMERA_ID` for the service comes from the systemd unit
`Environment=` line AND a line-2 `export CAMERA_ID=moth_cam` in the run script
(both now say moth_cam; the script export wins if they ever differ). Service
restart without sudo: `kill <pid>` (runs as wilburteo; Restart=always respawns).
VM home audited + tidied 2026-07-14: legacy stream scripts (`run_stream.sh`,
`runzhe_stream.sh`), pre-W15 baks and the pubkey scratch file deleted (Runzhe-
confirmed list); the Go2 ROS2 workspaces (unitree_ros2, go2_*, ~640M — the old
drive-the-dog-from-the-VM era) are KEPT intact per Runzhe. `.bak_premigration`
files kept as migration rollback.
True direct-from-laptop access (optional, GUI on the Win11 host): VMware Virtual
Network Editor → VMnet8 NAT Settings → forward host port 2222 → 192.168.189.130:22.

## Laptop
Win11 ZenBook Air (username has a space: "Zenbook Air"). Project root `C:\FYP\`. USB-C dock with
ASIX USB Ethernet adapter for the dog network: manual static 192.168.123.50/24 (gateway/DNS
blank). Reset to DHCP for normal networking.
