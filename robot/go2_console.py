#!/usr/bin/env python3
"""
=============================================================================
go2_console.py  -  One script: map profiles, waypoint survey, and the patrol
=============================================================================
Runs ON the Jetson Orin (ROS 2 Foxy, rclpy). Everything the navigation demo
needs is in this one file - reading the loaded map id, recording a route, and
driving it while photographing the capture points.

Replaces, for this workflow: get_map_id.py + pose.py + a hand-edited patrol
script. (go2_patrol_gated.py, the full detection patrol, is a separate thing
and is not touched by this.)

WHAT IT REMEMBERS
  Every map you record becomes a PROFILE in patrol_maps.json, sitting next to
  this script. A profile is:
      name        the name you give it, e.g. jewel_v2
      map_id      the USLAM map id read off the dog when it was recorded
      init_pose   where the dog must stand when a patrol on this map starts
      waypoints   the route, each with a capture flag
  Old profiles are never overwritten by a new recording, so a route survives
  until you delete it from the menu. The file is plain JSON - nothing goes to
  DynamoDB, nothing needs the cloud to read it.

THE TWO PATHS
  NEW MAP   -> record the map in the phone app first, localize on it in the
               app, park the dog on the map's start pose, then let this script
               read the map id, store the start pose, and walk the route with
               you while you drive with the remote.
  OLD MAP   -> pick the profile, the script checks the dog is on the right map
               and standing on the stored start pose, then drives the route.

PHOTOS
  Capture waypoints upload straight to
      s3://argus-frames-506868652945/frames/demo_cam/<waypoint>/<ts>.jpg
  demo_cam is a passthrough camera (detect_enabled=false), so the frame gets a
  dashboard record and NO detection runs on it - no Rekognition model to start,
  no alert emails. See docs/aws.md, processor v6.4.

PREREQUISITES - miss either and nothing here works
  * OBSTACLE AVOIDANCE MUST BE ON. With avoidance off the dog publishes no
    /uslam/localization/odom at all and every pose read says NO ODOM.
  * The map must be selected AND localized in the phone app. Localization from
    the app is the path that works; see the warning in localize_cold().

Launch:
  ssh unitree@<orin-ip>          # wireless: the campus IP is DHCP, re-read it
  tmux new -s go2
  source ~/setup_go2.sh
  python3 go2_console.py         # or: ./run_go2_console.sh
=============================================================================
"""
import argparse
import base64
import json
import math
import os
import re
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

# --- Where the map profiles live (next to this script) ---
STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "patrol_maps.json")
STORE_VERSION = 1

# --- Camera (SIYI A8 Mini on the dog network) ---
A8_IP    = "192.168.144.25"
RTSP_URL = f"rtsp://{A8_IP}:8554/main.264"

# --- AWS (production account 506868652945) ---
# demo_cam is the passthrough camera: its frames are recorded on the dashboard
# and no detection is run on them.
AWS_REGION       = "us-east-1"
CAMERA_ID        = "demo_cam"
S3_FRAMES_BUCKET = "argus-frames-506868652945"

# --- Pose sampling ---
SAMPLE_TARGET = 30      # odom frames we would like per reading
SAMPLE_WINDOW = 6.0     # seconds to collect them in
SAMPLE_MIN    = 5       # fewer than this = refuse the reading
SAMPLE_THIN   = 15      # fewer than this = accept but warn
SPREAD_WARN_M = 0.05    # position spread above this = warn (metres)
SPREAD_WARN_R = 0.10    # yaw spread above this = warn (radians)

# --- "Are we standing on the start pose?" tolerances ---
START_TOL_M   = 0.30    # metres from the stored init_pose
START_TOL_RAD = 0.35    # radians of heading error (~20 deg)

# --- Navigation ---
NUDGE_DELTA_YAW    = 0.30   # rad rotate in place (~17 deg); position unchanged
NUDGE_SETTLE_S     = 8.0    # let the nudge execute before navigation/start
NAV_START_ATTEMPTS = 2
GETMAP_TIMEOUT_S   = 8      # no reply in this long = MCU USLAM service is down
LOCALIZE_TIMEOUT_S = 30
LOCALIZE_ATTEMPTS  = 4      # localization init is intermittent (~60% per try)
NAV_START_TIMEOUT_S = 20
NAV_REACH_TIMEOUT_S = 90    # per waypoint: max wait for REACHED
NAV_RETRY_ONCE      = True
ABORT_ON_NAV_FAIL   = False  # False = skip the bad point, finish the route
START_COUNTDOWN_S   = 15     # time to clear the area and unplug the cable
FOLLOW_SETTLE_S     = 2.0    # gimbal lags the body 1-2 s in FOLLOW
CAPTURE_WARMUP      = 30     # RTSP frames to discard before grabbing one

# Tokens the firmware prints (either form) when localization initializes.
LOC_OK_TOKENS   = ["uslam is initialized", "initialization succeed"]
LOC_FAIL_TOKENS = ["initialization failed"]
# Tokens that mean a goal will not complete.
NAV_FAIL_TOKENS = ["NO_PATH", "GOAL_CANCELLED", "FAILURE", "GOAL_POINT_UNREACHABLE"]

# Names usable as a profile name and as an S3 key segment.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


def log(msg):
    """Timestamped stdout print, flushed so tmux/ssh shows it live."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def say(msg=""):
    """Plain print for menus and prompts (no timestamp noise)."""
    print(msg, flush=True)


def ask(prompt, default=None):
    """input() that survives Ctrl+D and applies a default on a bare Enter."""
    try:
        a = input(prompt).strip()
    except EOFError:
        return default if default is not None else ""
    return a if a else (default if default is not None else "")


def ask_yes(prompt, default=True):
    """Yes/no prompt. `default` is what a bare Enter means."""
    tag = "[Y/n]" if default else "[y/N]"
    while True:
        a = ask(f"{prompt} {tag} ").lower()
        if a == "":
            return default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False


def wrap_pi(a):
    """Wrap an angle into [-pi, pi]."""
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def median(values):
    """Plain median of a list of floats."""
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def yaw_median(yaws):
    """Median of angles. Unwrap every sample relative to the first before
    taking the median, or a heading sitting near +/-pi averages to the exact
    opposite direction. Wrapped back into [-pi, pi]."""
    if not yaws:
        return 0.0
    base = yaws[0]
    return wrap_pi(median([base + wrap_pi(y - base) for y in yaws]))


def yaw_spread(yaws):
    """Peak-to-peak of the unwrapped yaw samples, in radians."""
    if not yaws:
        return 0.0
    base = yaws[0]
    unwrapped = [wrap_pi(y - base) for y in yaws]
    return max(unwrapped) - min(unwrapped)


def iso_now():
    """UTC timestamp, same format the backend uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
# Profile store (plain JSON next to this script)
# =============================================================================
def load_store():
    """Read patrol_maps.json. A missing or unreadable file yields an empty
    store rather than an exception - a first run must not need a file to exist,
    and a corrupt file must not lock the operator out of recording a new map."""
    if not os.path.isfile(STORE_PATH):
        return {"version": STORE_VERSION, "profiles": []}
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("profiles"), list):
            raise ValueError("no profiles list")
        return data
    except Exception as e:
        say(f"WARNING: {STORE_PATH} could not be read ({e}).")
        say("Starting from an empty list. The old file is left alone - move it "
            "aside yourself if you want it gone.")
        return {"version": STORE_VERSION, "profiles": []}


def save_store(store):
    """Write the store atomically: full write to a temp file in the same
    directory, then os.replace. A power cut mid-write leaves the previous file
    intact instead of a half-written one."""
    store["version"] = STORE_VERSION
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STORE_PATH)


def find_profile(store, name):
    """Return the profile with this name, or None."""
    for p in store["profiles"]:
        if p.get("name") == name:
            return p
    return None


def profiles_for_map(store, map_id):
    """Every stored profile recorded on this map id."""
    return [p for p in store["profiles"] if p.get("map_id") == map_id]


# =============================================================================
# USLAM client (rclpy node) - commands, event tokens, live pose
# =============================================================================
class USLAMClient(Node):
    """Publishes commands on /uslam/client_command and waits on
    /uslam/server_log tokens. Also keeps the latest pose from
    /uslam/localization/odom and a sample buffer for the survey."""

    def __init__(self):
        super().__init__("go2_console")
        self._pub = self.create_publisher(String, CMD_TOPIC, 10)
        self.create_subscription(String, LOG_TOPIC, self._on_log, 50)
        self._lines = deque(maxlen=500)      # (monotonic_ts, text)
        self._cv = threading.Condition()

        self._lock = threading.Lock()
        self._samples = []                   # (monotonic_ts, x, y, yaw)
        self._pose = None
        self._pose_ts = 0.0
        self._map_id = None
        self.create_subscription(Odometry, "/uslam/localization/odom",
                                 self._on_odom, 20)

    # ---- subscriptions ----
    def _on_log(self, msg):
        """Buffer (timestamp, text), pick out a map_id reply, wake waiters."""
        with self._cv:
            self._lines.append((time.monotonic(), msg.data))
            self._cv.notify_all()
        if "get_map_id/map_id/" in msg.data:
            self._map_id = msg.data.split("get_map_id/map_id/")[-1].strip()

    def _on_odom(self, msg):
        """Store one pose sample. yaw uses the project convention
        2*atan2(qz, qw), wrapped to [-pi, pi]."""
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation
        yaw = wrap_pi(2.0 * math.atan2(q.z, q.w))
        with self._lock:
            self._samples.append((time.monotonic(), p.x, p.y, yaw))
            if len(self._samples) > 2000:
                del self._samples[:1000]
        self._pose = (p.x, p.y, yaw)
        self._pose_ts = time.monotonic()

    # ---- log waiting ----
    def now(self):
        """Monotonic mark to pass as `since` to wait_for_any()."""
        return time.monotonic()

    def wait_for_any(self, substrings, since, timeout):
        """Block until a server_log line arriving at/after `since` contains any
        of `substrings`. Returns the matched substring, or None on timeout."""
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
        m = String()
        m.data = data
        self._pub.publish(m)

    def send_verb(self, verb, repeat=1, gap=0.4):
        """Control verb: the wire string CARRIES inner double-quotes."""
        log(f"  -> CMD (verb)  \"{verb}\"")
        for _ in range(repeat):
            self._send_raw(f'"{verb}"')
            time.sleep(gap)

    def send_goal(self, x, y, yaw, repeat=1, gap=0.4):
        """set_goal_pose: BARE path, no inner quotes.

        repeat stays 1. USLAM treats every set_goal_pose as a NEW goal, so a
        duplicate arriving after TRACKING starts raises GOAL_CHANGED and kills
        the goal this script is waiting on."""
        path = f"navigation/set_goal_pose/{x:.6f}/{y:.6f}/{yaw:.6f}"
        log(f"  -> CMD (goal)  {path}")
        for _ in range(repeat):
            self._send_raw(path)
            time.sleep(gap)

    # ---- map id ----
    def get_map_id(self):
        """Ask which map is loaded. Returns the decoded id, or None.

        No reply at all means the USLAM service on the sport MCU
        (192.168.123.161, not the Orin) is down, and the only recovery is a
        full power-cycle of the dog."""
        self._map_id = None
        deadline = time.monotonic() + GETMAP_TIMEOUT_S
        while time.monotonic() < deadline:
            self.send_verb("common/get_map_id", gap=0.0)
            time.sleep(0.5)
            if self._map_id:
                break
        if not self._map_id:
            return None
        try:
            return base64.b64decode(self._map_id).decode("utf-8", "replace").strip()
        except Exception:
            # Some firmware builds answer with the id already decoded.
            return self._map_id

    # ---- pose ----
    def grab_pose(self):
        """Sample the pose for SAMPLE_WINDOW seconds and reduce to one reading.
        Returns (x, y, yaw, n, sx, sy, syaw) or None if odom is dead.

        Only samples arriving AFTER this call starts are used. That matters:
        driving the dog by remote can stop USLAM tracking, which freezes the
        last pose at wherever the dog USED to be, and a frozen pose read as a
        waypoint puts the whole route in the wrong place."""
        t0 = time.monotonic()
        deadline = t0 + SAMPLE_WINDOW
        while time.monotonic() < deadline:
            with self._lock:
                n = sum(1 for s in self._samples if s[0] >= t0)
            if n >= SAMPLE_TARGET:
                break
            time.sleep(0.1)
        with self._lock:
            fresh = [s for s in self._samples if s[0] >= t0]
        if len(fresh) < SAMPLE_MIN:
            return None
        xs = [s[1] for s in fresh]
        ys = [s[2] for s in fresh]
        yaws = [s[3] for s in fresh]
        return (median(xs), median(ys), yaw_median(yaws), len(fresh),
                max(xs) - min(xs), max(ys) - min(ys), yaw_spread(yaws))

    def live_pose(self, timeout=5.0, max_age=None):
        """Latest pose, or None. With max_age set, a pose older than that many
        seconds counts as absent."""
        deadline = time.monotonic() + timeout
        while self._pose is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if self._pose is None:
            return None
        if max_age is not None and (time.monotonic() - self._pose_ts) > max_age:
            return None
        return self._pose

    # ---- localization / navigation ----
    def localize_cold(self, seed):
        """Cold-start localization on `seed` = (x, y, yaw). Returns True/False.

        USE THE APP INSTEAD WHERE YOU CAN. On 2026-07-29 at Jewel this path
        returned success three times while localization was NOT matching the
        map - it was integrating odometry from an arbitrary origin, every
        surveyed coordinate came out in a frame that did not exist, and the
        whole survey had to be thrown away. The app's own relocalization is the
        route that has been proven on this stack. This function exists for the
        case where there is no app at all.

        Sequence per attempt: stop -> set_initial_pose -> start. The stop
        matters: seeding on top of a live localization fails. Init is
        intermittent (~60% per try), hence the retries."""
        for attempt in range(1, LOCALIZE_ATTEMPTS + 1):
            log("  [%d/%d] seeding (%.3f, %.3f, %.3f)"
                % ((attempt, LOCALIZE_ATTEMPTS) + tuple(seed)))
            if self.live_pose(timeout=2.0, max_age=3.0) is not None:
                t = self.now()
                self.send_verb("localization/stop", repeat=1)
                self.wait_for_any(["localization/stop/success"], since=t, timeout=6)
                time.sleep(1.0)

            t = self.now()
            self.send_verb("localization/set_initial_pose/%.6f/%.6f/%.6f" % tuple(seed))
            self.wait_for_any(["set_initial_pose/success"], since=t, timeout=6)

            t = self.now()
            self.send_verb("localization/start")
            hit = self.wait_for_any(LOC_OK_TOKENS + LOC_FAIL_TOKENS, since=t,
                                    timeout=LOCALIZE_TIMEOUT_S)
            if hit is not None and hit not in LOC_FAIL_TOKENS:
                log(f"  localization initialized (attempt {attempt}).")
                return True
            log("  attempt %d/%d did not initialize (%s)."
                % (attempt, LOCALIZE_ATTEMPTS, hit or "timed out"))
            time.sleep(2.0)
        return False

    def start_navigation(self, fallback_pose):
        """Nudge the dog, then navigation/start. Returns True on success.

        navigation/start only succeeds while localization is actively tracking,
        and tracking needs motion - that is what the nudge is for. The nudge is
        deliberately not verified: a pose-change check was tried and reported
        "did not move" on runs that then completed 5/5."""
        here = self.live_pose(timeout=5.0)
        if here is None:
            here = fallback_pose
            log("  nudging in place (no odom - using the stored start pose) ...")
        else:
            log("  nudging in place from the live pose "
                "(%.3f, %.3f, %.3f) ..." % tuple(here))
        self.send_goal(here[0], here[1], here[2] + NUDGE_DELTA_YAW, repeat=1)
        time.sleep(NUDGE_SETTLE_S)

        for k in range(1, NAV_START_ATTEMPTS + 1):
            t = self.now()
            self.send_verb("navigation/start")
            if self.wait_for_any(["navigation/start/success"], since=t,
                                 timeout=NAV_START_TIMEOUT_S):
                log("  navigation started (WAITING for goals).")
                return True
            log(f"  navigation/start attempt {k}/{NAV_START_ATTEMPTS} refused; "
                f"re-nudging.")
            spot = self.live_pose(timeout=3.0) or fallback_pose
            self.send_goal(spot[0], spot[1], spot[2] + NUDGE_DELTA_YAW * 2, repeat=1)
            time.sleep(4.0)
        log("  FAIL: navigation/start did not return success. Usually means the "
            "dog was not moving/tracking. Do NOT launch while driving the dog by "
            "remote - let the nudge do the moving.")
        return False

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
                log(f"  FAIL-LOUD: no REACHED within {NAV_REACH_TIMEOUT_S}s "
                    f"(attempt {attempt}).")
            else:
                log(f"  FAIL-LOUD: nav failure token '{hit}' (attempt {attempt}).")
            if attempt < attempts:
                log("  retrying goal once ...")
                time.sleep(1.0)
        return False


# =============================================================================
# Capture + upload
# =============================================================================
def capture_frame():
    """Grab one good frame from the A8 RTSP stream. Returns JPEG bytes or None."""
    import cv2
    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        log(f"  Capture: cannot open {RTSP_URL}. Is the A8 powered and on the "
            f"dog network?")
        return None
    frame = None
    for _ in range(CAPTURE_WARMUP):
        ok, f = cap.read()
        if ok and f is not None:
            frame = f
    cap.release()
    if frame is None:
        log("  Capture: opened the stream but decoded no frame.")
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

    The key shape is not cosmetic. The processor parses the camera and waypoint
    out of it, and the dashboard only presigns thumbnails from the frames bucket
    for keys starting with 'frames/'. Change the shape and the photo stops
    showing up on the dashboard."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    key = f"frames/{CAMERA_ID}/{waypoint_name}/{ts}.jpg"
    s3.put_object(Bucket=S3_FRAMES_BUCKET, Key=key, Body=jpeg_bytes,
                  ContentType="image/jpeg")
    log(f"  Uploaded s3://{S3_FRAMES_BUCKET}/{key}")
    return key


# =============================================================================
# Shared helpers
# =============================================================================
def report_pose(label, g):
    """Print a reading with its spread, and warn when it is thin or jumpy."""
    x, y, yaw, n, sx, sy, syaw = g
    say(f"    {label}: x={x:7.3f}  y={y:7.3f}  yaw={yaw:7.3f}   "
        f"(n={n}, spread x {sx:.3f} / y {sy:.3f} / yaw {syaw:.3f})")
    if n < SAMPLE_THIN:
        say(f"    WARNING: only {n} odom frames in {SAMPLE_WINDOW:.0f}s. Nudge "
            f"the dog with the remote and read it again.")
    if sx > SPREAD_WARN_M or sy > SPREAD_WARN_M or syaw > SPREAD_WARN_R:
        say("    WARNING: the pose moved while sampling. Let the dog settle and "
            "read it again before trusting this.")


def no_odom_help():
    """The three things that cause NO ODOM, in the order worth checking."""
    say("    NO ODOM - no pose is being published right now. Check, in order:")
    say("      1. obstacle avoidance is ON (with it off, odom is silent),")
    say("      2. the map is selected AND localized in the app,")
    say("      3. the dog is awake - nudge it with the remote, then retry.")


def read_map_id(node):
    """Read the loaded map id, printing the standard failure advice."""
    say("  reading the loaded map id ...")
    map_id = node.get_map_id()
    if not map_id:
        say("  NO REPLY to get_map_id. The USLAM service runs on the dog's sport")
        say("  MCU (192.168.123.161), not the Orin. No reply means it has")
        say("  crashed, and the only recovery is a FULL POWER-CYCLE of the dog.")
        return None
    say(f"  map loaded: {map_id}")
    return map_id


def describe(p):
    """One-line summary of a profile for the menu."""
    n_cap = sum(1 for w in p.get("waypoints", []) if w.get("capture"))
    return ("%-16s map %-34s %2d waypoint(s), %d photo   %s"
            % (p.get("name", "?"), p.get("map_id", "?"),
               len(p.get("waypoints", [])), n_cap, p.get("created", "")))


# =============================================================================
# Flow 1 - record a new map profile
# =============================================================================
def record_profile(node, store):
    """Survey a new route and save it as a profile. Returns the profile or None."""
    say("")
    say("=" * 70)
    say("RECORD A NEW MAP")
    say("=" * 70)
    say("Before going on, in the phone app:")
    say("  1. the map is RECORDED and SAVED,")
    say("  2. that map is SELECTED and you have localized on it,")
    say("  3. obstacle avoidance is ON.")
    say("And physically: the dog is parked on the pose you want every patrol on")
    say("this map to start from.")
    if not ask_yes("All of that done?", default=False):
        say("  nothing recorded.")
        return None

    map_id = read_map_id(node)
    if not map_id:
        return None

    existing = profiles_for_map(store, map_id)
    if existing:
        say(f"  NOTE: {len(existing)} profile(s) already use this map id: "
            f"{', '.join(p['name'] for p in existing)}")
        if not ask_yes("  Record another route on the same map?", default=True):
            return None

    # --- name ---
    while True:
        name = ask("  name for this map profile (e.g. jewel_v2): ")
        if not NAME_RE.match(name):
            say("    lowercase letters, digits, '_' or '-', max 31 chars.")
            continue
        if find_profile(store, name):
            say(f"    '{name}' already exists. Pick another name, or delete it "
                f"from the menu first.")
            continue
        break

    # --- localization ---
    say("")
    if not ask_yes("  Did you localize on this map IN THE APP just now?",
                   default=True):
        say("    Cold-starting localization from the script instead.")
        say("    WARNING: this path has silently produced a wrong coordinate")
        say("    frame before (Jewel, 2026-07-29) - localization reported")
        say("    success while it was not actually matching the map, and every")
        say("    surveyed point was void. If the app is available, use it.")
        if not ask_yes("    Continue with a cold start?", default=False):
            return None
        seed = (0.0, 0.0, 0.0)
        say("    Seeding (0, 0, 0): correct only when the dog is standing at the")
        say("    spot where the mapping run started.")
        if not node.localize_cold(seed):
            say("    localization did not initialize. Do it in the app and "
                "start again.")
            return None
    else:
        say("    trusting the localization the app already started.")

    # --- start pose ---
    say("")
    say("  Reading the START POSE (where the dog stands now). Every patrol on")
    say("  this map must begin from this spot.")
    g = node.grab_pose()
    if g is None:
        no_odom_help()
        return None
    report_pose("start pose", g)
    if not ask_yes("  Store this as the start pose?", default=True):
        say("  nothing recorded.")
        return None
    init_pose = {"x": round(g[0], 3), "y": round(g[1], 3), "yaw": round(g[2], 3)}

    # --- waypoint loop ---
    say("")
    say("  WAYPOINTS. Drive the dog to a point with the remote, then press")
    say("  Enter here to store it.")
    say("    Enter    store the spot the dog is standing on")
    say("    d        delete the last stored waypoint")
    say("    l        list what is stored so far")
    say("    done     finish and save this profile")
    say("    abort    throw the whole thing away")
    waypoints = []
    while True:
        cmd = ask(f"  [{len(waypoints)} stored] Enter=store, d, l, done, abort > ").lower()

        if cmd == "abort":
            if ask_yes("  Discard this profile?", default=False):
                say("  nothing recorded.")
                return None
            continue

        if cmd == "l":
            if not waypoints:
                say("    (nothing yet)")
            for k, w in enumerate(waypoints, 1):
                say("    %2d. %-14s x=%7.3f y=%7.3f yaw=%7.3f  photo=%s"
                    % (k, w["name"], w["x"], w["y"], w["yaw"],
                       "yes" if w["capture"] else "no"))
            continue

        if cmd == "d":
            if waypoints:
                say(f"    dropped '{waypoints.pop()['name']}'.")
            else:
                say("    nothing to drop.")
            continue

        if cmd == "done":
            if not waypoints:
                say("    no waypoints stored - nothing to save. Store at least "
                    "one, or 'abort'.")
                continue
            break

        if cmd != "":
            say("    Enter, d, l, done or abort.")
            continue

        # --- bare Enter: store the current spot ---
        g = node.grab_pose()
        if g is None:
            no_odom_help()
            continue
        report_pose("here", g)

        default_name = f"wp{len(waypoints) + 1}"
        while True:
            wname = ask(f"    name for this waypoint [{default_name}]: ",
                        default=default_name)
            if not NAME_RE.match(wname):
                say("      lowercase letters, digits, '_' or '-', max 31 chars. "
                    "This name becomes the S3 folder and the zone on the "
                    "dashboard.")
                continue
            if any(w["name"] == wname for w in waypoints):
                say(f"      '{wname}' is already used in this route.")
                continue
            break

        capture = ask_yes(f"    take a photo at '{wname}'?", default=True)
        waypoints.append({"name": wname,
                          "x": round(g[0], 3), "y": round(g[1], 3),
                          "yaw": round(g[2], 3), "capture": capture})
        say(f"    stored '{wname}' ({len(waypoints)} so far).")

    profile = {
        "name": name,
        "map_id": map_id,
        "created": iso_now(),
        "init_pose": init_pose,
        "waypoints": waypoints,
    }
    store["profiles"].append(profile)
    save_store(store)
    say("")
    say(f"  SAVED to {STORE_PATH}")
    say(f"  {describe(profile)}")
    say("  Run the script again and pick this profile to drive the route.")
    return profile


# =============================================================================
# Flow 2 - run the patrol on a stored profile
# =============================================================================
def check_at_start(node, profile):
    """Confirm the dog is standing on the profile's stored start pose.
    Returns True to go ahead."""
    ip = profile["init_pose"]
    say("")
    say("  Checking the dog is on this map's start pose "
        f"({ip['x']:.3f}, {ip['y']:.3f}, {ip['yaw']:.3f}) ...")
    g = node.grab_pose()
    if g is None:
        no_odom_help()
        return False
    report_pose("dog is at", g)
    dx, dy = g[0] - ip["x"], g[1] - ip["y"]
    dist = math.hypot(dx, dy)
    dyaw = abs(wrap_pi(g[2] - ip["yaw"]))
    say(f"    off by {dist:.3f} m and {dyaw:.3f} rad "
        f"({math.degrees(dyaw):.1f} deg).")
    if dist <= START_TOL_M and dyaw <= START_TOL_RAD:
        say("    on the start pose.")
        return True
    say(f"    NOT on the start pose (tolerance {START_TOL_M} m / "
        f"{START_TOL_RAD} rad).")
    say("    Drive the dog back to where the route starts and try again.")
    say("    If the offset is large and the dog LOOKS right, localization is")
    say("    probably tracking in the wrong place - re-localize in the app.")
    return ask_yes("    Run anyway?", default=False)


def run_patrol(node, profile, upload=True):
    """Drive the stored route. Photographs and uploads the capture waypoints."""
    waypoints = profile.get("waypoints", [])
    if not waypoints:
        say("  this profile has no waypoints.")
        return

    s3 = None
    if upload:
        # Credentials come from the Orin's environment / ~/.aws.
        import boto3
        s3 = boto3.client("s3", region_name=AWS_REGION)

    # The map the dog has loaded must be the map this route was surveyed on.
    # Coordinates from one map mean nothing on another - they are not "close",
    # they are meaningless, and the dog will drive at them anyway.
    live_map = read_map_id(node)
    if live_map is None:
        return
    if live_map != profile["map_id"]:
        say("  MAP MISMATCH.")
        say(f"    profile '{profile['name']}' was surveyed on: {profile['map_id']}")
        say(f"    the dog currently has loaded:               {live_map}")
        say("    Select the right map in the app and localize on it.")
        if not ask_yes("    Run anyway (the route will be wrong)?", default=False):
            return

    if not check_at_start(node, profile):
        return

    n_cap = sum(1 for w in waypoints if w.get("capture"))
    say("")
    log("=" * 62)
    log(f"PATROL: {profile['name']}")
    log(f"  waypoints : {len(waypoints)} ({n_cap} with a photo)")
    log(f"  upload    : {'OFF (--no-upload)' if not upload else S3_FRAMES_BUCKET}")
    if upload:
        log(f"  camera    : {CAMERA_ID} (passthrough - photos are recorded, "
            f"nothing is detected)")
    log(f"  UNPLUG THE EXTERNAL CABLE during the {START_COUNTDOWN_S}s countdown.")
    log("  Remote in hand as an e-stop. Area clear.")
    log("=" * 62)
    for s in range(START_COUNTDOWN_S, 0, -1):
        log(f"  starting in {s:2d}s ...")
        time.sleep(1.0)

    ip = profile["init_pose"]
    if not node.start_navigation((ip["x"], ip["y"], ip["yaw"])):
        log("NAVIGATION DID NOT START - aborting.")
        return

    reached, failed, photos, uploaded = 0, 0, 0, []
    for i, wp in enumerate(waypoints, 1):
        log("-" * 62)
        log(f"[{i}/{len(waypoints)}] -> {wp['name']}  "
            f"({wp['x']:.3f}, {wp['y']:.3f}, {wp['yaw']:.3f})")
        if not node.goto(wp["x"], wp["y"], wp["yaw"]):
            failed += 1
            if ABORT_ON_NAV_FAIL:
                log("  ABORT_ON_NAV_FAIL set - stopping.")
                break
            log("  skipping this waypoint, continuing.")
            continue
        reached += 1

        if not wp.get("capture"):
            log("  transit point - no photo.")
            continue

        # The gimbal is in FOLLOW and lags the body by 1-2 s after the dog's
        # final in-place rotation. Let it catch up before capturing.
        time.sleep(FOLLOW_SETTLE_S)
        jpeg = capture_frame()
        if jpeg is None:
            log("  no frame at this point - carrying on.")
            continue
        photos += 1
        if not upload:
            log("  --no-upload: frame discarded.")
            continue
        try:
            uploaded.append(upload_frame(s3, wp["name"], jpeg))
        except Exception as e:
            log(f"  S3 upload FAILED ({e}) - carrying on.")

    log("=" * 62)
    log(f"Patrol complete. reached={reached} failed={failed} of "
        f"{len(waypoints)}; {photos} photo(s), {len(uploaded)} uploaded.")
    if uploaded:
        log(f"Dashboard -> Gallery, filter camera={CAMERA_ID}. Records appear a "
            f"few seconds after the upload.")
    log("=" * 62)


# =============================================================================
# Menu
# =============================================================================
def menu(node, store, upload):
    """Top-level loop: pick a saved map, record a new one, or delete one."""
    while True:
        profiles = store["profiles"]
        say("")
        say("=" * 70)
        say("GO2 PATROL CONSOLE")
        say("=" * 70)
        if profiles:
            say("Saved maps:")
            for k, p in enumerate(profiles, 1):
                say(f"  {k}. {describe(p)}")
        else:
            say("No maps saved yet.")
        say("")
        say("  <number>  run the patrol on that map")
        say("  n         record a NEW map (survey a route)")
        say("  d <num>   delete a saved map")
        say("  q         quit")
        cmd = ask("> ").lower()

        if cmd in ("q", "quit", "exit"):
            return

        if cmd == "n":
            record_profile(node, store)
            continue

        if cmd.startswith("d"):
            arg = cmd[1:].strip()
            if not arg.isdigit() or not (1 <= int(arg) <= len(profiles)):
                say("  usage: d <number from the list>")
                continue
            victim = profiles[int(arg) - 1]
            say(f"  {describe(victim)}")
            if ask_yes(f"  Delete '{victim['name']}' permanently?", default=False):
                profiles.remove(victim)
                save_store(store)
                say("  deleted.")
            continue

        if cmd.isdigit() and 1 <= int(cmd) <= len(profiles):
            run_patrol(node, profiles[int(cmd) - 1], upload=upload)
            continue

        if cmd:
            say("  not a valid choice.")


def main():
    ap = argparse.ArgumentParser(
        description="Go2 map profiles, waypoint survey and patrol, in one script")
    ap.add_argument("--no-upload", action="store_true",
                    help="patrol and photograph, but send nothing to S3 "
                         "(use this to rehearse a route)")
    args = ap.parse_args()

    store = load_store()
    rclpy.init()
    node = USLAMClient()
    threading.Thread(target=rclpy.spin, args=(node,), daemon=True).start()
    try:
        menu(node, store, upload=not args.no_upload)
    except KeyboardInterrupt:
        say("\ninterrupted.")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
