# Go2 Demo — Full Command Sheet

Every Go2 capability, with the exact commands, for a supervisor demo.
All verified on the current setup (W13). Machine access is in `PROJECT_MANUAL.md`.

Suggested 15-minute demo order: **3 → 4 → 5 → 6 → 7** (manual nav proves the
protocol, patrol proves autonomy, then gimbal, live stream, cloud round-trip).

---

## 0. Pre-demo checklist

- Dog on the floor, area clear, **remote in hand as e-stop**.
- SLAM activated once via the phone app after power-on (connect app → localization
  mode → dog localizes on the map). All commands after that are app-free.
- Any forward motion runs **untethered** — unplug the external cable first.
- Laptop wired dongle: static `192.168.123.50/24`, gateway/DNS blank.
- Rekognition v4 model **RUNNING** if you demo detection (start it ~10 min early;
  **STOP it after the demo** — billed per running hour).

## 1. Connect

```bash
ssh unitree@192.168.123.18     # wired (dog LAN)
# wireless (use this when the dog will move) - the campus IP is DHCP and CHANGES.
# Read the current one over the wired link first, do not reuse a remembered value:
#   ssh unitree@192.168.123.18 "ip -br addr show wlan0"
ssh unitree@<current-wlan0-ip>  # e.g. 10.1.122.128 on 2026-08-07, was 10.1.125.24 in W10
# password: 123
```

## 2. Init — EVERY Orin terminal, EVERY time

```bash
source ~/setup_go2.sh          # binds CycloneDDS to the dog link; no /uslam topics without it
date                           # must show 2026 (no RTC; set-clock-from-http runs at boot)
```

## 3. USLAM manual navigation (raw protocol — the "how it works" demo)

Two plaintext `std_msgs/String` topics. Keep a second terminal listening:

```bash
# terminal A (leave running):
ros2 topic echo /uslam/server_log
```

Quoting rule: **control verbs carry inner escaped quotes; `set_goal_pose` does not.**
Never use `-r N` (continuous publish drops SSH) — always `--once`, resend 1–2× if unheard.

```bash
# 1. liveness + map check (no reply within ~8 s = MCU USLAM dead -> power-cycle the dog)
ros2 topic pub --once /uslam/client_command std_msgs/msg/String 'data: "\"common/get_map_id\""'

# 2. seed the pose (current map start pose shown; replace after any re-survey)
ros2 topic pub --once /uslam/client_command std_msgs/msg/String 'data: "\"localization/set_initial_pose/-0.086/0.142/0.033\""'

# 3. start localization — wait for "initialization succeed!" / "uslam is initialized!"
ros2 topic pub --once /uslam/client_command std_msgs/msg/String 'data: "\"localization/start\""'

# 4. NUDGE: send a small in-place rotate goal so localization is actively tracking
#    (navigation/start only succeeds while the dog is moving)
ros2 topic pub --once /uslam/client_command std_msgs/msg/String 'data: "navigation/set_goal_pose/-0.086/0.142/0.333"'

# 5. start the planner — expect "navigation/start/success"
ros2 topic pub --once /uslam/client_command std_msgs/msg/String 'data: "\"navigation/start\""'

# 6. send a real goal (meters + radians, map frame) — replace **X/Y/YAW**
ros2 topic pub --once /uslam/client_command std_msgs/msg/String 'data: "navigation/set_goal_pose/X/Y/YAW"'
```

Watch terminal A for: `state_transition/TRACKING` → `REACHED` (dog holds position).
Failure tokens: `NO_PATH` / `FAILURE` / `GOAL_CANCELLED` / `GOAL_POINT_UNREACHABLE`.

Surveyed waypoints on the ACTIVE map (`7853B2C397A44F8EB317D1C12D5B1F1C` —
TEMPORARY client-test rescan 2026-07-03; the PRIMARY validated map is
`0411...42D2`, both parameter sets in `robot/map_profiles.md`):
wp1/start `-0.086/0.142/0.033` (no capture) · wp2 `1.281/0.451/-1.784`
· wp3 `1.133/-1.108/3.014` · wp4 `-0.051/-1.185/1.643`.
Coords go stale on every map rescan — grab fresh ones with `python3 ~/go2/pose.py`
(one line per run) and update WAYPOINTS before demoing on a new map.

Read the live pose (only publishes while the dog moves):

```bash
ros2 topic echo /uslam/localization/odom | grep -m1 -A11 "position:"
```

## 4. Autonomous gated patrol (the headline demo)

Full loop: navigate → capture (A8) → upload S3 → wait for the DynamoDB detection
record (cloud gate) → next waypoint → return home without capture.

**The patrol is APP-FREE and must stay that way.** The script establishes
localization itself: `set_initial_pose` seeds it with `INITIAL_POSE`, then
`localization/start`. Cold boot is app-free too — USLAM auto-starts on the MCU and
auto-loads the last map. The phone app is for recording maps and crash diagnosis
only, never as a step in running a patrol.

**The one precondition: park the dog at HOME before launching.** `INITIAL_POSE` is
the localization seed, so the dog must physically be standing there or the seed is
a lie and localization either fails or locks onto the wrong place. Home is the same
pose as `wp_return`:

    INITIAL_POSE = wp_return = (-5.013, -0.825, 1.374)   # Jewel map 1BEC7FFD...

Re-survey home and update both values together if the parking spot ever moves.

```bash
ssh unitree@192.168.123.18
tmux new -s patrol
source ~/setup_go2.sh
python3 ~/go2/go2_patrol_gated.py
```

- Countdown is `START_COUNTDOWN_S` (currently **3 s** — raise it if you need time to
  pull the cable; it was 25 s when launching remotely).
- **Do not touch the remote during the run.** A nav goal fired into manual control
  stopped localization mid-run on 2026-07-29 and the patrol aborted at bringup.
- To survive the SSH session dropping, launch detached instead of tmux:
  `setsid nohup python3 -u ~/go2/go2_patrol_gated.py > /tmp/patrol.log 2>&1 &`

What the log proves, line by line: USLAM bringup (map id → localization skipped or
seeded → nudge → navigation) → per-waypoint REACHED → `Captured WxH` →
`Uploaded s3://...` → `GATE open: record found (detected=..., boxes=N)` →
`wp_return: navigate only`.

**If waypoints fail at random, check `send_goal`'s `repeat` first.** It must be
**1**. USLAM treats every `set_goal_pose` as a new goal, so a repeat landing after
TRACKING has begun fires `GOAL_CHANGED` and kills the goal the script is waiting
on — which surfaces as `FAILURE` on a perfectly good waypoint. This was the cause
of a 1-of-5 run that became 5-of-5 the moment the default changed from 3 to 1.

**If a whole run collapses (reached 0–1 of 5, every goal NO_PATH/FAILURE), run the
full reset before retrying:**

```bash
ssh unitree@192.168.123.18 "source /opt/ros/foxy/setup.bash && source ~/cyclonedds_ws/install/setup.bash && source ~/setup_go2.sh && python3 ~/go2/uslam_reset.py"
```

It sends **all three** stop verbs — `mapping/stop`, `navigation/stop`,
`localization/stop` — which is the teardown the phone app does on a clean exit. The
patrol's own bringup only sends `localization/stop`, so residual mapping/navigation
state survives it and localization keeps locking 1.5–3.6 m away from the seed no
matter what seed you give it. On 2026-07-31 this was the difference between three
straight failed runs and two consecutive 5/5 runs with zero retries. Park the dog on
`INITIAL_POSE`, reset, then launch normally.

**Not** the fix for `TIMEOUT_ODOMETRY` + `TIMEOUT_POINTCLOUD` flooding the log —
that is the MCU pipeline wedged and only a power cycle clears it.

## 5. Gimbal — SIYI A8 Mini

- FOLLOW is the saved power-on default (set in SIYI PC Assistant; survives reboots).
  During the patrol the camera tracks the dog's heading — no commands needed.
- Status check:

```bash
python3 ~/a8/a8_status.py
```

- View the raw camera (from the Orin, or any machine on the a8 subnet):
  RTSP `rtsp://192.168.144.25:8554/main.264` (1080p; open in VLC).
- Per-waypoint overrides (LOCK + `pitch_down` 0–25° / `yaw_offset` / `zoom`) are
  built into the patrol script's `WAYPOINTS` via the optional `"cam"` key — show the
  code block if asked. Pitch convention on this unit: positive = lens DOWN.

## 6. KVS live streaming (A8 → AWS → dashboard)

Runs as a boot service on the Orin — nothing to start by hand.

```bash
# prove the service is alive (on the Orin):
systemctl status kvs-controller.service
journalctl -u kvs-controller -n 30 --no-pager
```

- Dashboard → **Live stream** tab → Armyworm card → toggle **Stream on** → HLS
  playback appears (producer picks up the toggle within ~5 s, fragments in ~10 s).
- Moth card = fixed Hikvision cam via the mini-PC VM (same mechanism, transcode path).
- CLI proof that fragments land (run on the laptop, CLI v2):

```bash
aws kinesisvideo list-fragments --stream-name armyworm-cam-stream --profile nbk2 --region us-east-1 --max-results 5 2>$null || aws kinesis-video-archived-media list-fragments --stream-name armyworm-cam-stream --profile nbk2 --region us-east-1
```

## 7. Cloud detection round-trip (works without the robot too)

1. Start the model — dashboard: **Settings → Cameras → Armyworm → Start** (5–10 min
   to RUNNING). CLI equivalent:

```bash
aws rekognition start-project-version --project-version-arn "arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v9/version/v9-20260725-1746/1785001598671" --min-inference-units 1 --profile nbk2 --region us-east-1
```

**The ARN above was the v4 model until 2026-07-30** — stale by four model generations.
The live model is whatever `custom_model_arn` says on the `worm_cam` row in
`pest-monitoring-cameras`; read it there rather than trusting a pasted ARN:

```bash
aws dynamodb get-item --table-name pest-monitoring-cameras --key '{"camera_id":{"S":"worm_cam"}}' --profile nbk2 --region us-east-1 --query "Item.custom_model_arn.S"
```

2. Dashboard → **Settings → Test upload** → drop an armyworm photo → Run detection
   → boxes render in ~5–15 s (S3 → pest-detection-processor v4.2 with cloud tiling
   → DynamoDB → dashboard canvas).
3. **Gallery** tab: every patrol capture with its boxes; X = flag false positive
   (persists to DynamoDB `verifications`, survives reload/redeploy).
4. **Analytics** tab: per-zone / per-day counts driven by the same records.
5. Stop the model after the demo — dashboard **Stop** button, or:

```bash
aws rekognition stop-project-version --project-version-arn "arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v9/version/v9-20260725-1746/1785001598671" --profile nbk2 --region us-east-1
```

Dashboard serving: VS Code Live Server on `C:\FYP\` → open
`web/dashboard_v4/index.html`, or `python -m http.server 5501 --directory web/dashboard_v4`.

## 8. Utilities

```bash
python3 ~/go2/get_map_id.py      # quick map/liveness check
python3 ~/go2/nav_probe.py       # minimal nav smoke test (nudge + short hop)
```

## 9. Recovery cheat sheet

| Symptom | Fix |
|---|---|
| `get_map_id` silent | MCU USLAM dead → **power-cycle the whole dog**, re-activate SLAM in the app |
| CycloneDDS Python segfault | `sudo reboot` the Orin — first run after reboot is clean |
| SSH drops during topic pub | You used `-r N` — use `--once` with 1–2 resends |
| `navigation/start` no success | Dog wasn't moving — repeat the NUDGE, then retry |
| No `/uslam` topics visible | Forgot `source ~/setup_go2.sh` in this terminal |
| A8 stream dead | Check `a8-link` NIC on the Orin: `nmcli con show a8-link`; RTSP caps at 1080p |
| Editing files on the Orin | `nano` installed (2026-07-03); `vi` / heredoc / `scp` also work |
