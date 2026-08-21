#!/usr/bin/env python3
"""
=============================================================================
KVS Stream Controller (mini PC polling daemon)  --  v2, nbk2 / API-driven
=============================================================================
Polls the backend route  GET /stream/status?camera={CAMERA_ID}  every 5s and
starts/stops a GStreamer subprocess accordingly. Pushes to the camera's
configured kvs_stream_name in AWS account 506868652945 (prod; moved from 366356442579 on 2026-08).

Changes from v1:
  - Control source: v1 did a direct DynamoDB get_item on the old `system-config`
    nested-map (`cameras.{id}.stream_enabled`). The nbk2 migration moved camera
    config into per-row `pest-monitoring-cameras`, so that read no longer works.
    v2 polls the HTTP API route `/stream/status` instead -- single source of
    truth, and the control path needs NO AWS credentials (plain HTTPS GET).
    Only the kvssink subprocess needs AWS creds, inherited from the environment.
  - Target account history: 396278862184 -> 366356442579 (nbk2) -> 506868652945 (prod, 2026-08). Credentials are
    supplied by the runner / systemd EnvironmentFile -- never hardcoded here.

CAMERA_ID drives one camera per process. To drive a second camera, run another
instance with a different CAMERA_ID (and matching RTSP_* env).

Current deployment: CAMERA_ID=moth_cam (overridden by run_kvs_controller.sh),
Hikvision 192.168.1.66 -> moth-cam-stream. The pipeline TRANSCODES (decodebin ->
x264enc) because the Hikvision RTSP is not guaranteed clean H.264 passthrough.
=============================================================================
"""
import os
import sys
import json
import time
import signal
import logging
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional, Tuple

# ============================================================
# CONFIG (edit here, or override via env vars)
# ============================================================
CAMERA_ID         = os.environ.get("CAMERA_ID", "worm_cam")
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "5"))

# Backend API -- the /stream/status route is the control signal.
API_BASE = os.environ.get(
    "API_BASE",
    "https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com",
).rstrip("/")

# RTSP source -- RTSP_PASS MUST be supplied via env (no insecure default).
RTSP_USER = os.environ.get("RTSP_USER", "admin")
RTSP_PASS = os.environ.get("RTSP_PASS", "")
RTSP_HOST = os.environ.get("RTSP_HOST", "192.168.1.66")
RTSP_PATH = os.environ.get("RTSP_PATH", "/Streaming/channels/101")
RTSP_URL  = f"rtsp://{RTSP_USER}:{RTSP_PASS}@{RTSP_HOST}:554{RTSP_PATH}"

# KVS Producer SDK build (inherited from Wilbur)
KVS_SDK_DIR     = os.environ.get(
    "KVS_SDK_DIR",
    "/home/wilburteo/amazon-kinesis-video-streams-producer-sdk-cpp",
)
GST_PLUGIN_PATH = f"{KVS_SDK_DIR}/build"
LD_LIB_PATH     = f"{KVS_SDK_DIR}/open-source/local/lib"

AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("kvs_controller")

# ============================================================
# CONTROL SIGNAL  --  GET /stream/status?camera={CAMERA_ID}
# ============================================================
def fetch_desired_state() -> Tuple[Optional[bool], Optional[str]]:
    """
    Returns (stream_enabled, kvs_stream_name) for CAMERA_ID.
    Returns (None, None) on any error -- caller keeps current state and retries.
    Plain HTTPS GET: no AWS credentials required for the control path.
    """
    url = f"{API_BASE}/stream/status?camera={urllib.parse.quote(CAMERA_ID)}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("stream_enabled", False)), data.get("kvs_stream_name")
    except (urllib.error.URLError, urllib.error.HTTPError,
            ValueError, OSError) as e:
        log.error(f"API poll failed: {e}")
        return None, None

# ============================================================
# GSTREAMER PROCESS MANAGEMENT  (unchanged from v1)
# ============================================================
gst_proc: Optional[subprocess.Popen] = None

def build_gst_args(stream_name: str) -> list:
    return [
        "gst-launch-1.0",
        "rtspsrc", f"location={RTSP_URL}",
        "latency=0", "drop-on-latency=true", "protocols=tcp",
        "!", "decodebin",
        "!", "videoconvert",
        "!", "x264enc",
        "key-int-max=45", "tune=zerolatency", "pass=cbr",
        "speed-preset=superfast", "bitrate=2000000",
        "!", "h264parse",
        "!", "kvssink",
        f"stream-name={stream_name}",
        "storage-size=512",
        f"aws-region={AWS_REGION}",
        "frame-timecodes=false",
        "fragment-duration=2",
        "max-latency=2",
    ]

def is_gst_running() -> bool:
    return gst_proc is not None and gst_proc.poll() is None

def start_gst(stream_name: str):
    global gst_proc
    if is_gst_running():
        log.warning("GStreamer already running, skipping start")
        return

    env = os.environ.copy()
    env["GST_PLUGIN_PATH"] = GST_PLUGIN_PATH
    env["LD_LIBRARY_PATH"] = LD_LIB_PATH
    # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY inherit from the environment
    # (systemd EnvironmentFile) -- must be the nbk2 credentials.

    log.info(f"Starting GStreamer -> stream={stream_name}, rtsp={RTSP_HOST}")
    gst_proc = subprocess.Popen(
        build_gst_args(stream_name),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,  # own process group, so we can kill cleanly
    )
    log.info(f"GStreamer PID={gst_proc.pid}")

def stop_gst():
    global gst_proc
    if not is_gst_running():
        gst_proc = None
        return

    pid = gst_proc.pid
    log.info(f"Stopping GStreamer PID={pid}")
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        gst_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.warning(f"PID={pid} ignored SIGTERM, escalating to SIGKILL")
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            gst_proc.wait(timeout=2)
        except Exception as e:
            log.error(f"SIGKILL failed: {e}")
    except ProcessLookupError:
        pass  # already dead
    gst_proc = None

# ============================================================
# MAIN LOOP  (unchanged from v1)
# ============================================================
def graceful_exit(signum, _frame):
    log.info(f"Received signal {signum}, shutting down...")
    stop_gst()
    sys.exit(0)

def main():
    signal.signal(signal.SIGTERM, graceful_exit)
    signal.signal(signal.SIGINT, graceful_exit)

    log.info("=== KVS Controller starting (v2, API-driven) ===")
    log.info(f"camera_id={CAMERA_ID}, poll_interval={POLL_INTERVAL_SEC}s")
    log.info(f"control={API_BASE}/stream/status")
    log.info(f"rtsp={RTSP_HOST}{RTSP_PATH}")

    last_logged_state = None

    while True:
        desired, stream_name = fetch_desired_state()

        if desired is None:
            # API poll failed -- keep current state, retry next cycle
            time.sleep(POLL_INTERVAL_SEC)
            continue

        running = is_gst_running()

        # Detect crashed subprocess
        if gst_proc is not None and not running:
            log.warning(f"GStreamer exited unexpectedly (rc={gst_proc.returncode})")
            globals()["gst_proc"] = None
            running = False

        # Reconcile desired vs actual
        if desired and not running:
            if not stream_name:
                log.error(f"stream_enabled=true but kvs_stream_name is empty for {CAMERA_ID}")
            else:
                start_gst(stream_name)
        elif not desired and running:
            stop_gst()

        # Periodic state log (only on transition)
        state_tuple = (desired, running)
        if state_tuple != last_logged_state:
            log.info(f"State: desired={desired}, running={running}")
            last_logged_state = state_tuple

        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()