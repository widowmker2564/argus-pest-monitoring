#!/usr/bin/env python3
"""
=============================================================================
go2_patrol_gated.py  -  Gated inspection patrol node (Go2 EDU + SIYI A8 + AWS)
=============================================================================
Runs ON the Jetson Orin (ROS 2 Foxy, rclpy). Drives the Go2 around the surveyed
route using the robot's native USLAM stack. At every waypoint it runs a
cloud-gated inspection: capture a frame -> upload to S3 -> wait for the
detection record to appear in DynamoDB -> proceed to the next point.

The gimbal stays in FOLLOW mode (it tracks the dog's heading), so by default no
gimbal aiming is done. Each waypoint may optionally carry a small "cam" override
for future per-point fine-tuning (pitch_down / yaw_offset / zoom); leave it out
for plain follow-the-head behaviour.

WHY plain ROS 2 topics (not the Unitree Python SDK):
  USLAM is exposed as plaintext std_msgs/String on two topics, so a standard
  rclpy node drives it. We deliberately avoid unitree_sdk2_python (its cyclonedds
  binding segfaults on this Orin); rclpy uses rmw_cyclonedds_cpp which is stable
  for /uslam/* topics.

USLAM protocol (reverse-engineered + verified W10-W12):
  - command  -> /uslam/client_command   (std_msgs/String)
  - feedback -> /uslam/server_log        (std_msgs/String)
  - QUOTING (copy the app's wire format exactly):
      control verbs (common/*, localization/*, navigation/start)
        -> the String data CARRIES an inner pair of double-quotes
      navigation/set_goal_pose/x/y/yaw
        -> BARE path, NO inner quotes
  - coords are x/y/yaw, METERS + RADIANS, in the map frame.

Cold-start order (verified W12 Fri, command-line only, no phone app):
  1. common/get_map_id                        -> success + map_id
  2. localization/set_initial_pose/x/y/yaw    -> success + 6-DOF echo
  3. localization/start                       -> "uslam is initialized!"
                                                 (a.k.a. "initialization succeed!")
  4. >>> the dog MUST be moving for navigation/start to return success <<<
     localization only tracks while the dog moves; navigation/start needs active
     tracking. This node nudges the dog a step (NUDGE_*) right before it, which
     replaces the manual "walk two steps with the remote" trick.
  5. navigation/start                         -> "navigation/start/success" + WAITING
  6. navigation/set_goal_pose/x/y/yaw         -> TRACKING -> REACHED -> WAITING

Per-waypoint gate signals on /uslam/server_log:
  navigation/state_transition/REACHED   -> arrived; run scan; then next point
  failure tokens: NO_PATH / GOAL_CANCELLED / FAILURE / GOAL_POINT_UNREACHABLE
  After REACHED the state machine sits at WAITING idle - the dog holds position
  until the next set_goal_pose, so there is no "pause" to implement.

IMPORTANT operational notes (hard-won W12 Fri):
  - The USLAM service lives on the dog's sport MCU (192.168.123.161), not the
    Orin. If it has crashed, get_map_id returns nothing and the ONLY recovery is
    a full power-cycle of the dog. This node fails loudly if get_map_id is silent.
  - Run UNTETHERED for any forward motion: tmux launch, unplug the external cable
    during START_COUNTDOWN_S, remote in hand as e-stop, area cleared.

Launch:
  ssh unitree@10.1.125.24            (wireless, so no cable drags when moving)
  tmux new -s patrol
  source ~/setup_go2.sh
  python3 go2_patrol_gated.py
  -> unplug the external cable during the countdown.
=============================================================================
"""
import math
import os
import sys
import time
import threading
from collections import deque
from datetime import datetime, timezone

# --- OpenCV RTSP transport must be set BEFORE importing cv2 ---
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from nav_msgs.msg import Odometry

# =============================================================================
# CONFIG
# =============================================================================

# --- USLAM topics (do not change) ---
CMD_TOPIC = "/uslam/client_command"
LOG_TOPIC = "/uslam/server_log"

# --- Robot pose seed (METERS + RADIANS, map frame) ---
# This is the START point of the loop (= the dog's physical pose at launch).
# PRIMARY lab-room map 04114624684C4194B7008EDB3A5642D2 (recorded W13,
# re-surveyed 2026-07-03 after app re-localization; validated 4/4 REACHED by
# tests/wp_test_2.py). Restored 2026-07-14 — the W14 SPF-showcase DEMO map is
# retired. Per-map blocks live in robot/map_profiles.md.
INITIAL_POSE = {"x": -4.970, "y": -0.657, "yaw": 1.260}

# --- Patrol route (METERS + RADIANS, map frame), in loop order ---
# JEWEL map 1BEC7FFDF97C47AC8BD751143D3FE187, surveyed 2026-07-29 by walking the
# route twice and extracting the dwell points. Round-to-round repeatability on
# the first three points was 1.8 / 0.9 / 2.4 cm, so the frame is sound.
# The start pose is INITIAL_POSE and is revisited as wp_return; capture happens
# at the three zones only. Waypoint names become the S3 key segment
# (frames/worm_cam/<name>/<ts>.jpg). Full record: robot/map_profiles.md.
# `cam` is OPTIONAL: omit it for plain FOLLOW (gimbal tracks the dog's
# head). When present it fine-tunes the gimbal at that point only:
#   pitch_down : deg downward 0..25   (gimbal is briefly switched to LOCK to apply)
#   yaw_offset : deg, + = lens right relative to current head
#   zoom       : absolute zoom (1.0 = wide)
WAYPOINTS = [
    {"name": "wp1",   "x": -4.179, "y":  0.836, "yaw":  0.669, "capture": False},
    {"name": "zone1", "x": -2.367, "y":  1.608, "yaw":  1.256},
    {"name": "zone2", "x": -1.775, "y":  0.928, "yaw": -2.519},
    # Round 1 put this point at (-2.795, 1.154, 2.460); round 2 is used here.
    {"name": "zone3", "x": -3.189, "y":  1.263, "yaw": -3.030},
    {"name": "wp_return", "x": -4.970, "y": -0.657, "yaw": 1.260, "capture": False},
]

# --- Pre-navigation nudge (makes localization 'active' so navigation/start works) ---
# Sends a tiny in-place rotate goal first so the dog moves a little, which starts
# localization tracking. If your robot is already moving / tracking you can set
# NUDGE_ENABLED = False.
NUDGE_ENABLED = True
NUDGE_DELTA_YAW = 0.30   # rad to rotate in place (~17 deg); position unchanged
NUDGE_SETTLE_S  = 8.0    # let the nudge actually execute before navigation/start
NAV_START_ATTEMPTS = 2   # navigation/start refusals are recoverable

# --- Localization bringup policy ---
# True  = trust the localization that is ALREADY running and do not touch it.
# False = original behaviour: seed set_initial_pose then localization/start.
# Re-seeding a localization that is already tracking correctly destroys it, and
# re-localizing can then fail outright - it failed three times at Jewel on
# 2026-07-29. When localization was established from the app and is healthy,
# leave it alone. Set this False for a genuine cold start with no app.
SKIP_LOCALIZATION_BRINGUP = False

# --- Network endpoints ---
A8_IP            = "192.168.144.25"
A8_CTRL_PORT     = 37260
RTSP_URL         = f"rtsp://{A8_IP}:8554/main.264"
SIYI_SDK_PATHS   = [os.path.expanduser("~/a8/siyi_sdk"), os.path.expanduser("~/a8")]

# --- AWS ---
AWS_REGION       = "us-east-1"
CAMERA_ID        = "worm_cam"   # routes the S3 key to the worm camera config (renamed from armyworm_go2_a8mini, 2026-07 migration)
S3_FRAMES_BUCKET = "argus-frames-506868652945"
DDB_DETECTIONS   = "pest-monitoring-detections"

# --- Timing / policy ---
START_COUNTDOWN_S   = 3       # time to clear the area and unplug the cable
GETMAP_TIMEOUT_S    = 8       # max wait for get_map_id reply (else MCU service is down)
LOCALIZE_TIMEOUT_S  = 30
LOCALIZE_ATTEMPTS   = 4      # localization init is intermittent (~60% per try,
                             # measured 2026-07-30); retry rather than abort
NAV_START_TIMEOUT_S = 20
NAV_REACH_TIMEOUT_S = 90      # per-waypoint: max wait for REACHED before failing
NAV_RETRY_ONCE      = True    # retry a failed goal once (startup transients happen)
ABORT_ON_NAV_FAIL   = False   # False = skip the bad point and continue the patrol
GIMBAL_SETTLE_S     = 1.5     # after a LOCK override: let the gimbal stop moving
FOLLOW_SETTLE_S     = 2.0     # pure FOLLOW lags the body ~1-2 s; after the dog
                             # stops, wait this long so the gimbal catches up to
                             # the now-static heading before we capture
CAPTURE_WARMUP      = 30      # RTSP frames to discard before grabbing a good one
DDB_GATE_TIMEOUT_S  = 150      # max wait for the detection record (then fail-open)
DDB_GATE_POLL_S     = 1.5     # poll interval for the detection record

# Tokens that mean localization initialized (the firmware prints either form).
LOC_OK_TOKENS  = ["uslam is initialized", "initialization succeed"]
# ... and the one that means it did not. Without this the script waits out the
# full LOCALIZE_TIMEOUT_S on a failure it was already told about.
LOC_FAIL_TOKENS = ["initialization failed"]
# Tokens that mean a goal will not complete.
NAV_FAIL_TOKENS = ["NO_PATH", "GOAL_CANCELLED", "FAILURE", "GOAL_POINT_UNREACHABLE"]


def iso_now():
    """UTC timestamp 'YYYY-MM-DDTHH:MM:SSZ' - matches the backend's time format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    """Timestamped stdout print, flushed so tmux/ssh shows it live."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# =============================================================================
# USLAM client (rclpy node)
# =============================================================================
class USLAMClient(Node):
    """Publishes commands and waits on /uslam/server_log event tokens."""

    def __init__(self):
        """Set up the two USLAM endpoints: publisher -> /uslam/client_command,
        subscriber <- /uslam/server_log (lines buffered for wait_for_any)."""
        super().__init__("go2_patrol_gated")
        self._pub = self.create_publisher(String, CMD_TOPIC, 10)
        self._sub = self.create_subscription(String, LOG_TOPIC, self._on_log, 50)
        self._lines = deque(maxlen=500)          # (monotonic_ts, text)
        self._cv = threading.Condition()
        # Live pose, used to aim the nudge at a REAL rotation (see bringup step 4).
        self._pose = None
        self._pose_ts = 0.0
        self._fallback_seed = None
        self._odom_sub = self.create_subscription(
            Odometry, "/uslam/localization/odom", self._on_odom, 10)

    def _on_log(self, msg):
        """server_log listener: buffer (timestamp, text) and wake wait_for_any()."""
        with self._cv:
            self._lines.append((time.monotonic(), msg.data))
            self._cv.notify_all()

    def _on_odom(self, msg):
        """Keep the latest pose. yaw uses the project convention 2*atan2(qz,qw)."""
        pos = msg.pose.pose.position
        q = msg.pose.pose.orientation
        yaw = 2.0 * math.atan2(q.z, q.w)
        yaw = (yaw + math.pi) % (2.0 * math.pi) - math.pi
        self._pose = (pos.x, pos.y, yaw)
        self._pose_ts = time.monotonic()

    def live_pose(self, timeout=5.0, max_age=None):
        """Wait briefly for an odom frame and return (x, y, yaw), or None.

        Odom only flows while localization is running, so this returns None
        before bringup has started it. With max_age set, a pose older than that
        many seconds is treated as absent - driving the dog by remote stops USLAM,
        which freezes the last pose at wherever the dog USED to be, and seeding
        from that is worse than not seeding from it at all."""
        deadline = time.monotonic() + timeout
        while self._pose is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if self._pose is None:
            return None
        if max_age is not None and (time.monotonic() - self._pose_ts) > max_age:
            return None
        return self._pose

    def now(self):
        """Monotonic timestamp; callers pass it as the 'since' mark to wait_for_any()."""
        return time.monotonic()

    def wait_for_any(self, substrings, since, timeout):
        """Block until a server_log line arriving at/after `since` contains any of
        `substrings`. Returns the matched substring, or None on timeout."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for ts, text in self._lines:
                    if ts >= since:
                        for sub in substrings:
                            if sub in text:
                                return sub
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(timeout=remaining)

    # ---- command send (quoting rules) ----
    def _send_raw(self, data):
        """Publish one raw String onto /uslam/client_command (quoting done by callers)."""
        m = String()
        m.data = data
        self._pub.publish(m)

    def send_verb(self, verb, repeat=1, gap=0.4):
        """Control verb: wire string carries inner double-quotes. Sent a few times
        because a single publish can be missed on first DDS discovery."""
        log(f"  -> CMD (verb)  \"{verb}\"")
        for _ in range(repeat):
            self._send_raw(f'"{verb}"')
            time.sleep(gap)

    def send_goal(self, x, y, yaw, repeat=1, gap=0.4):
        """set_goal_pose: BARE path, no inner quotes."""
        path = f"navigation/set_goal_pose/{x:.6f}/{y:.6f}/{yaw:.6f}"
        log(f"  -> CMD (goal)  {path}")
        for _ in range(repeat):
            self._send_raw(path)
            time.sleep(gap)

    # ---- high-level ----
    def bringup(self):
        """Cold-start localization + navigation (no app). Returns True on success.
        Data flow: verbs/goals out on /uslam/client_command; success and failure
        tokens read back from /uslam/server_log via wait_for_any()."""
        log("USLAM bringup ...")

        # 1. get_map_id - also our liveness check for the MCU USLAM service.
        t = self.now()
        self.send_verb("common/get_map_id")
        hit = self.wait_for_any(["common/get_map_id/success", "get_map_id/map_id"],
                                since=t, timeout=GETMAP_TIMEOUT_S)
        if not hit:
            log("  FATAL: no reply to get_map_id. The dog's USLAM service is most "
                "likely DOWN (it runs on the sport MCU, not the Orin). "
                "Recovery = POWER-CYCLE the whole dog, then re-run. Aborting.")
            return False
        recent = [txt for ts, txt in list(self._lines) if ts >= t and "map_id" in txt]
        if recent:
            log(f"  map loaded: {recent[-1][:90]}")

        # 2-3. Localization seed + start, unless we were told to leave a working
        # localization alone (see SKIP_LOCALIZATION_BRINGUP).
        ip = INITIAL_POSE
        if SKIP_LOCALIZATION_BRINGUP:
            log("  SKIPPING localization bringup - the localization already "
                "running is trusted (SKIP_LOCALIZATION_BRINGUP=True).")
        else:
            # 2a. Stop any localization that is already running. Seeding and
            # starting on top of a live localization FAILS - verified
            # 2026-07-30: three localization/start calls all returned success,
            # then "[Localization] initialization failed!" 6 s later. That made
            # a second patrol run impossible without a power cycle. The phone
            # app always sends stop first, so mirror it. Harmless when nothing
            # is running.
            # Localization init is intermittent, so retry the whole
            # stop -> seed -> start sequence. Identical seed, identical dog
            # position: failed 12:56, succeeded 13:04 on 2026-07-30.
            initialized = False
            for attempt in range(1, LOCALIZE_ATTEMPTS + 1):
                # Prefer a FRESH live pose as the seed. INITIAL_POSE is only
                # right if the dog is parked on it. Read it BEFORE the stop,
                # because stopping localization ends the odom feed. A stale
                # pose is refused: driving the dog by remote stops USLAM and
                # freezes the pose at where it used to be.
                seed = self.live_pose(timeout=3.0, max_age=3.0)
                if seed is None:
                    self._fallback_seed = (ip["x"], ip["y"], ip["yaw"])
                    seed = self._fallback_seed
                    log("  [%d/%d] seed: INITIAL_POSE (%.3f, %.3f, %.3f) - no "
                        "fresh odom, so the dog must be parked on it."
                        % ((attempt, LOCALIZE_ATTEMPTS) + seed))
                else:
                    log("  [%d/%d] seed: live pose (%.3f, %.3f, %.3f)"
                        % ((attempt, LOCALIZE_ATTEMPTS) + seed))

                # Only stop when something is actually running. Fresh odom is the
                # proxy: no odom means localization is already down, and sending a
                # stop anyway just adds churn. repeat=1 for the same reason -
                # send_verb defaults to 3, which tripled every stop.
                if seed is not self._fallback_seed:
                    t = self.now()
                    self.send_verb("localization/stop", repeat=1)
                    self.wait_for_any(["localization/stop/success"], since=t, timeout=6)
                    time.sleep(1.0)
                else:
                    log("  no live localization to stop - skipping the stop.")

                t = self.now()
                self.send_verb("localization/set_initial_pose/%.6f/%.6f/%.6f" % seed)
                self.wait_for_any(["set_initial_pose/success"], since=t, timeout=6)

                t = self.now()
                self.send_verb("localization/start")
                hit = self.wait_for_any(LOC_OK_TOKENS + LOC_FAIL_TOKENS, since=t,
                                        timeout=LOCALIZE_TIMEOUT_S)
                if hit is not None and hit not in LOC_FAIL_TOKENS:
                    log("  localization initialized (attempt %d)." % attempt)
                    initialized = True
                    break
                log("  attempt %d/%d did not initialize (%s)."
                    % (attempt, LOCALIZE_ATTEMPTS, hit or "timed out"))
                time.sleep(2.0)

            if not initialized:
                log("  FAIL: localization did not initialize in %d attempts. "
                    "Park the dog on INITIAL_POSE, or power-cycle the dog if "
                    "navigation is also reporting TIMEOUT_ODOMETRY."
                    % LOCALIZE_ATTEMPTS)
                return False

        # 4. Nudge, then navigation/start.
        # navigation/start needs localization to be actively tracking, which needs
        # motion - hence the nudge. It is deliberately NOT verified: a pose-change
        # check was tried on 2026-07-30 and reported "did not move" three times per
        # run while navigation/start then succeeded first try and the route
        # completed 5/5, twice. It cost 37 s per run and never changed an outcome.
        # If navigation/start does refuse, its own retry re-nudges harder.
        if NUDGE_ENABLED:
            here = self.live_pose(timeout=5.0)
            if here is None:
                here = (ip["x"], ip["y"], ip["yaw"])
                log("  nudging in place (no odom - using INITIAL_POSE) ...")
            else:
                log("  nudging in place from the live pose "
                    "(%.3f, %.3f, %.3f) ..." % tuple(here))
            self.send_goal(here[0], here[1], here[2] + NUDGE_DELTA_YAW, repeat=1)
            time.sleep(NUDGE_SETTLE_S)
        # 5. navigation/start
        hit = None
        for k in range(1, NAV_START_ATTEMPTS + 1):
            t = self.now()
            self.send_verb("navigation/start")
            hit = self.wait_for_any(["navigation/start/success"], since=t,
                                    timeout=NAV_START_TIMEOUT_S)
            if hit:
                break
            log("  navigation/start attempt %d/%d refused; re-nudging."
                % (k, NAV_START_ATTEMPTS))
            spot = self.live_pose(timeout=3.0) or (ip["x"], ip["y"], ip["yaw"])
            self.send_goal(spot[0], spot[1], spot[2] + NUDGE_DELTA_YAW * 2, repeat=1)
            time.sleep(4.0)
        if not hit:
            log("  FAIL: navigation/start did not return success. Usually means the "
                "dog was not moving/tracking. Try increasing NUDGE_DELTA_YAW or move "
                "the dog manually, then re-run.")
            return False
        log("  navigation started (WAITING for goals).")
        return True

    def goto(self, x, y, yaw):
        """Send one goal and wait for REACHED. Returns True if reached."""
        attempts = 2 if NAV_RETRY_ONCE else 1
        for attempt in range(1, attempts + 1):
            t = self.now()
            self.send_goal(x, y, yaw)
            hit = self.wait_for_any(["state_transition/REACHED"] + NAV_FAIL_TOKENS,
                                    since=t, timeout=NAV_REACH_TIMEOUT_S)
            if hit and "REACHED" in hit:
                log("  REACHED.")
                return True
            if hit is None:
                log(f"  FAIL-LOUD: no REACHED within {NAV_REACH_TIMEOUT_S}s (attempt {attempt}).")
            else:
                log(f"  FAIL-LOUD: nav failure token '{hit}' (attempt {attempt}).")
            if attempt < attempts:
                log("  retrying goal once ...")
                time.sleep(1.0)
        return False


# =============================================================================
# Gimbal (SIYI A8 via siyi_sdk) - FOLLOW by default; optional per-point override.
# Fail-soft: any gimbal hiccup only logs, never stops the patrol.
# =============================================================================
class Gimbal:
    def __init__(self):
        """Prepare sys.path for the flat-layout siyi_sdk; no connection yet (cam=None)."""
        self.cam = None
        for p in SIYI_SDK_PATHS:
            if p not in sys.path:
                sys.path.append(p)

    def connect(self):
        """Open the UDP control link to the A8 (192.168.144.25:37260) and set
        FOLLOW. Returns False and disables gimbal control on ANY failure - the
        patrol runs fine without a gimbal (fail-soft)."""
        # This siyi_sdk uses flat imports internally (`from siyi_message import *`),
        # so the package dir itself must be on sys.path and the class is imported
        # as `from siyi_sdk import SIYISDK`. Confirmed working on this Orin.
        SIYISDK = None
        try:
            from siyi_sdk import SIYISDK          # ~/a8/siyi_sdk on path (flat)
        except Exception:
            try:
                from siyi_sdk.siyi_sdk import SIYISDK  # package-style fallback
            except Exception as e:
                log(f"  Gimbal: cannot import siyi_sdk ({e}); running without gimbal control.")
                return False
        try:
            self.cam = SIYISDK(server_ip=A8_IP, port=A8_CTRL_PORT)
            if not self.cam.connect():
                log("  Gimbal: connect() failed; running without gimbal control.")
                self.cam = None
                return False
            self._set_follow()
            log("  Gimbal connected (FOLLOW mode - tracks the dog's heading).")
            return True
        except Exception as e:
            log(f"  Gimbal: connect error ({e}); running without gimbal control.")
            self.cam = None
            return False

    def _set_follow(self):
        """Send requestFollowMode over the A8 UDP link (gimbal yaw tracks the body)."""
        # FOLLOW = gimbal yaw follows the body; this is the default the user wants.
        # Confirmed method on this Orin's siyi_sdk build.
        try:
            self.cam.requestFollowMode()
        except Exception as e:
            log(f"  Gimbal: requestFollowMode failed ({e}).")

    def apply_override(self, cam):
        """Optional per-waypoint fine-tune. Switches to LOCK to apply absolute
        angles, then it is the caller's job to leave it; we restore FOLLOW after
        the capture via restore_follow(). No-op if cam is None/empty."""
        if self.cam is None or not cam:
            return False
        # LOCK is required for precise absolute angle commands.
        try:
            self.cam.requestLockMode()
        except Exception as e:
            log(f"  Gimbal: requestLockMode failed ({e}).")
        yaw_off   = float(cam.get("yaw_offset", 0.0))
        pitch_dn  = max(0.0, min(25.0, float(cam.get("pitch_down", 0.0))))
        zoom      = cam.get("zoom", None)
        try:
            # requestSetAngles(yaw, pitch); pitch positive = down (inverted mount).
            self.cam.requestSetAngles(yaw_off, pitch_dn)
        except Exception as e:
            log(f"  Gimbal: setAngles failed ({e}).")
        if zoom is not None:
            self._set_zoom(float(zoom))
        return True

    def restore_follow(self):
        """Back to FOLLOW after a LOCK override (called right after the capture)."""
        if self.cam is None:
            return
        self._set_follow()

    def _set_zoom(self, zoom):
        """Send absolute zoom (1.0 = wide) over the A8 UDP link; log-only on failure."""
        # Confirmed method on this Orin's siyi_sdk build.
        try:
            self.cam.requestAbsoluteZoom(zoom)
        except Exception as e:
            log(f"  Gimbal: requestAbsoluteZoom({zoom}) failed ({e}).")

    def close(self):
        """Disconnect the A8 UDP link at patrol end (one benign Errno 9 is normal)."""
        if self.cam is not None:
            try:
                self.cam.disconnect()   # one benign "[Errno 9] Bad file descriptor" is normal
            except Exception:
                pass
            self.cam = None


# =============================================================================
# Capture (cv2 RTSP) + S3 upload + DDB gate
# =============================================================================
def capture_frame():
    """Grab one good frame from the A8 RTSP stream. Returns JPEG bytes or None."""
    import cv2
    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        log("  Capture: cannot open RTSP stream.")
        return None
    frame = None
    for _ in range(CAPTURE_WARMUP):
        ok, f = cap.read()
        if ok and f is not None:
            frame = f
    cap.release()
    if frame is None:
        log("  Capture: opened stream but decoded no frame.")
        return None
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        log("  Capture: JPEG encode failed.")
        return None
    h, w = frame.shape[:2]
    log(f"  Captured {w}x{h}.")
    return buf.tobytes()


def upload_frame(s3, waypoint_name, jpeg_bytes):
    """Upload to frames/{CAMERA_ID}/{waypoint}/{ts}.jpg. Returns the S3 key.
    The camera segment routes the object to the armyworm camera config, which is
    where cloud-side tiling (processor v4.2) is applied if tiling_enabled."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    key = f"frames/{CAMERA_ID}/{waypoint_name}/{ts}.jpg"
    s3.put_object(Bucket=S3_FRAMES_BUCKET, Key=key, Body=jpeg_bytes,
                  ContentType="image/jpeg")
    log(f"  Uploaded s3://{S3_FRAMES_BUCKET}/{key}")
    return key


def wait_for_detection(ddb_table, image_key):
    """Poll the detections table for the record the processor writes for this key.
    The processor's put_item is UNCONDITIONAL (clean frames write a record too), so
    this gate cannot dead-lock on a clean waypoint. Fail-open on timeout."""
    from boto3.dynamodb.conditions import Key
    deadline = time.monotonic() + DDB_GATE_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            resp = ddb_table.query(
                KeyConditionExpression=Key("image_id").eq(image_key),
                Limit=1,
            )
            items = resp.get("Items", [])
            if items:
                it = items[0]
                detected = it.get("target_detected", False)
                conf = it.get("target_confidence", "0")
                nboxes = len(it.get("bboxes", []) or [])
                log(f"  GATE open: record found "
                    f"(detected={detected}, conf={conf}, boxes={nboxes}).")
                return True
        except Exception as e:
            log(f"  GATE: query error ({e}); retrying.")
        time.sleep(DDB_GATE_POLL_S)
    log(f"  GATE timeout after {DDB_GATE_TIMEOUT_S}s - proceeding (fail-open).")
    return False


# =============================================================================
# Main
# =============================================================================
def main():
    """Patrol orchestrator. Data flow per scan waypoint:
      USLAM goal -> /uslam/client_command ... REACHED <- /uslam/server_log
      -> A8 RTSP frame (capture_frame) -> JPEG bytes in memory
      -> S3 put_object to argus-frames-506868652945 (upload_frame)
      -> S3 event fires pest-detection-processor -> DynamoDB detection record
      -> wait_for_detection polls that record (the cloud gate) -> next waypoint.
    Return/transit points ("capture": False) do only the USLAM leg."""
    if not WAYPOINTS:
        log("No WAYPOINTS configured. Exiting.")
        return

    # AWS clients (creds from the Orin's environment / ~/.aws, as cag_user)
    import boto3
    s3 = boto3.client("s3", region_name=AWS_REGION)
    ddb_table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(DDB_DETECTIONS)

    # Gimbal: open ONE connection for the whole patrol (FOLLOW mode).
    gimbal = Gimbal()
    gimbal.connect()

    rclpy.init()
    node = USLAMClient()
    spin_thread = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin_thread.start()

    try:
        log("=" * 62)
        log("Go2 GATED inspection patrol")
        log(f"  waypoints: {len(WAYPOINTS)}   remote in hand as e-stop")
        log(f"  UNPLUG THE EXTERNAL CABLE during the {START_COUNTDOWN_S}s countdown.")
        log("=" * 62)
        for s in range(START_COUNTDOWN_S, 0, -1):
            log(f"  starting in {s:2d}s ...")
            time.sleep(1.0)

        if not node.bringup():
            log("BRINGUP FAILED - aborting patrol.")
            return

        reached, failed = 0, 0
        for i, wp in enumerate(WAYPOINTS, 1):
            name = wp.get("name", f"wp{i}")
            log("-" * 62)
            log(f"[{i}/{len(WAYPOINTS)}] -> {name}  "
                f"({wp['x']:.3f}, {wp['y']:.3f}, {wp['yaw']:.3f})")

            if not node.goto(wp["x"], wp["y"], wp["yaw"]):
                failed += 1
                if ABORT_ON_NAV_FAIL:
                    log("  ABORT_ON_NAV_FAIL set - stopping patrol.")
                    break
                log("  skipping this waypoint, continuing patrol.")
                continue
            reached += 1

            # Return/transit points carry "capture": False -> navigate only, no scan.
            if not wp.get("capture", True):
                log("  return point - navigate only, no capture/upload.")
                continue

            # ---- scan hook (dog holds at WAITING idle while we do this) ----
            cam = wp.get("cam")
            applied = gimbal.apply_override(cam) if cam else False
            # Let the gimbal settle on the final heading before capturing:
            #   override point -> it was driven to an absolute angle (LOCK)
            #   bare point     -> pure FOLLOW is still catching up after the dog's
            #                     final in-place rotation to the goal yaw (~1-2 s lag)
            time.sleep(GIMBAL_SETTLE_S if applied else FOLLOW_SETTLE_S)
            jpeg = capture_frame()
            if applied:
                gimbal.restore_follow()       # back to follow-the-head for travel
            if jpeg is None:
                log("  scan: no frame - skipping gate for this point.")
                continue
            try:
                key = upload_frame(s3, name, jpeg)
            except Exception as e:
                log(f"  scan: S3 upload failed ({e}) - skipping gate.")
                continue
            wait_for_detection(ddb_table, key)
            # REACHED left the state machine at WAITING idle; the next goto() drives on.

        log("=" * 62)
        log(f"Patrol complete. reached={reached} failed={failed} "
            f"of {len(WAYPOINTS)} waypoints.")
        log("=" * 62)

    except KeyboardInterrupt:
        log("Interrupted by user (Ctrl+C).")
    finally:
        gimbal.close()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()