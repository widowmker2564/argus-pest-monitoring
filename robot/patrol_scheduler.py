#!/usr/bin/env python3
"""
=============================================================================
Patrol Scheduler (Orin polling daemon)  --  v1
=============================================================================
Runs ON the Jetson Orin as a systemd service (mirrors minipc/kvs_controller.py
and robot/kvs_controller.py's pattern). Polls the backend route
GET /schedule?camera=worm_cam every POLL_INTERVAL_SEC and, when the scheduled
time arrives, launches go2_patrol_gated.py.

This closes the gap that the dashboard's schedule only ever drove
pest-camera-scheduler (Rekognition model start/stop) -- nothing previously
turned the schedule into an actual Go2 patrol launch. The dashboard write path
is unchanged: this daemon reads the SAME /schedule row the frontend already
writes (POST /schedule -> pest-monitoring-cameras.worm_cam.schedule), no new
API route needed.

Control path needs NO AWS credentials (plain HTTPS GET), same reasoning as
kvs_controller.py. go2_patrol_gated.py's own AWS calls pick up credentials
from ~/.aws/credentials via boto3's default chain (unchanged).

SAFETY -- do not remove this gate:
  go2_patrol_gated.py requires a human on site: remote in hand as e-stop, area
  cleared, external power cable unplugged before forward motion (see its own
  docstring). A scheduled trigger with nobody present cannot satisfy any of
  that, so this daemon refuses to launch unless a human has recently confirmed
  those checks by touching ARM_FILE:
      touch ~/go2/.patrol_armed
  The touch must be within ARM_MAX_AGE_MIN of the scheduled time -- it is a
  "I am here right now and the area is clear" signal, not a one-time setting.
  A schedule that matches with no fresh arm file is logged and skipped, not
  forced through.

Launch:
  Deployed as systemd service `patrol-scheduler` via run_patrol_scheduler.sh
  (sources the Foxy ROS env non-interactively, then execs this). See
  robot/patrol-scheduler.service and docs/hardware.md.
=============================================================================
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

# ============================================================
# CONFIG (edit here, or override via env vars)
# ============================================================
CAMERA_ID         = os.environ.get("CAMERA_ID", "worm_cam")
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "30"))

# Backend API -- same /schedule row the dashboard's Schedule panel writes to.
API_BASE = os.environ.get(
    "API_BASE",
    "https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com",
).rstrip("/")

GO2_DIR       = os.path.expanduser("~/go2")
PATROL_SCRIPT = os.path.join(GO2_DIR, "go2_patrol_gated.py")
LOG_DIR       = os.path.join(GO2_DIR, "patrol_logs")
STATE_FILE    = os.path.join(GO2_DIR, ".patrol_scheduler_state.json")

# --- Safety arm gate (see module docstring) ---
ARM_FILE       = os.environ.get("PATROL_ARM_FILE", os.path.join(GO2_DIR, ".patrol_armed"))
ARM_MAX_AGE_MIN = int(os.environ.get("PATROL_ARM_MAX_AGE_MIN", "60"))
REQUIRE_ARM    = os.environ.get("PATROL_REQUIRE_ARM", "1") != "0"

# Dashboard schedule times are Singapore local (matches _cron_expression in
# pest-monitoring-api.py). Fixed UTC+8, no DST -- do not rely on the Orin's
# system timezone, compute SGT from UTC instead.
SGT_OFFSET = timedelta(hours=8)
DAY_CODES  = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("patrol_scheduler")


# ============================================================
# STATE (dedupe: fire at most once per SGT calendar date)
# ============================================================
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ============================================================
# CONTROL SIGNAL  --  GET /schedule?camera={CAMERA_ID}
# ============================================================
def fetch_schedule() -> Optional[dict]:
    """Returns {"enabled": bool, "start_time": "HH:MM", "days": [...]}, or
    None on any error -- caller just retries next cycle."""
    url = f"{API_BASE}/schedule?camera={urllib.parse.quote(CAMERA_ID)}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("schedule") or {}
    except (urllib.error.URLError, urllib.error.HTTPError,
            ValueError, OSError) as e:
        log.error(f"API poll failed: {e}")
        return None


def now_sgt() -> datetime:
    return datetime.now(timezone.utc) + SGT_OFFSET


def schedule_matches_now(schedule: dict, sgt_now: datetime) -> bool:
    if not schedule.get("enabled"):
        return False
    start_time = schedule.get("start_time")
    if not start_time:
        return False
    try:
        hh, mm = (int(p) for p in start_time.split(":"))
    except (ValueError, AttributeError):
        log.error(f"Bad start_time in schedule: {start_time!r}")
        return False
    days = schedule.get("days") or []
    if days:
        today_code = DAY_CODES[sgt_now.weekday()]
        if today_code not in days:
            return False
    return sgt_now.hour == hh and sgt_now.minute == mm


# ============================================================
# SAFETY ARM GATE
# ============================================================
def is_armed() -> bool:
    if not REQUIRE_ARM:
        return True
    try:
        mtime = os.path.getmtime(ARM_FILE)
    except OSError:
        return False
    age_min = (time.time() - mtime) / 60.0
    return age_min <= ARM_MAX_AGE_MIN


# ============================================================
# PATROL PROCESS MANAGEMENT
# ============================================================
patrol_proc: Optional[subprocess.Popen] = None
patrol_logfile = None


def is_patrol_running() -> bool:
    return patrol_proc is not None and patrol_proc.poll() is None


def launch_patrol():
    global patrol_proc, patrol_logfile
    if is_patrol_running():
        log.warning("Patrol already running, skipping launch")
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_path = os.path.join(LOG_DIR, f"patrol_{ts}.log")
    patrol_logfile = open(log_path, "wb", buffering=0)

    log.info(f"Scheduled trigger fired -- launching {PATROL_SCRIPT}")
    log.info(f"  patrol output -> {log_path}")
    patrol_proc = subprocess.Popen(
        [sys.executable, PATROL_SCRIPT],
        cwd=GO2_DIR,
        env=os.environ.copy(),   # ROS env (sourced by the wrapper) + AWS creds chain
        stdout=patrol_logfile,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,    # own process group, so we can kill cleanly
    )
    log.info(f"Patrol PID={patrol_proc.pid}")


def stop_patrol():
    global patrol_proc, patrol_logfile
    if patrol_proc is not None:
        try:
            os.killpg(os.getpgid(patrol_proc.pid), signal.SIGTERM)
            patrol_proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            try:
                os.killpg(os.getpgid(patrol_proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        patrol_proc = None
    if patrol_logfile is not None:
        patrol_logfile.close()
        patrol_logfile = None


# ============================================================
# MAIN LOOP
# ============================================================
def graceful_exit(signum, _frame):
    log.info(f"Received signal {signum}, shutting down (leaving any running patrol alone)...")
    if patrol_logfile is not None:
        patrol_logfile.close()
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, graceful_exit)
    signal.signal(signal.SIGINT, graceful_exit)

    log.info("=== Patrol Scheduler starting ===")
    log.info(f"camera_id={CAMERA_ID}, poll_interval={POLL_INTERVAL_SEC}s")
    log.info(f"control={API_BASE}/schedule?camera={CAMERA_ID}")
    log.info(f"patrol_script={PATROL_SCRIPT}")
    log.info(f"require_arm={REQUIRE_ARM}, arm_file={ARM_FILE}, arm_max_age={ARM_MAX_AGE_MIN}min")

    state = load_state()

    while True:
        schedule = fetch_schedule()

        # Reap a finished patrol so is_patrol_running() reflects reality.
        if patrol_proc is not None and patrol_proc.poll() is not None:
            log.info(f"Patrol exited (rc={patrol_proc.returncode})")
            stop_patrol()

        if schedule is not None:
            sgt = now_sgt()
            today = sgt.strftime("%Y-%m-%d")

            if schedule_matches_now(schedule, sgt) and state.get("last_fired_date") != today:
                if is_patrol_running():
                    log.warning("Schedule matched but a patrol is already running -- skipping.")
                elif not is_armed():
                    if state.get("last_warned_date") != today:
                        log.warning(
                            "Schedule matched (%s SGT) but NOT ARMED -- skipping. "
                            "A human must confirm area clear / cable unplugged / remote "
                            "in hand, then run: touch %s" % (schedule.get("start_time"), ARM_FILE)
                        )
                        state["last_warned_date"] = today
                        save_state(state)
                else:
                    launch_patrol()
                    state["last_fired_date"] = today
                    save_state(state)

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
