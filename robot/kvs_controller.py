#!/usr/bin/env python3
"""
KVS Stream Controller (Jetson Orin)  --  Orin / A8 / nbk2
Polls GET /stream/status?camera={CAMERA_ID} every 5s and starts/stops a
GStreamer subprocess to match the dashboard toggle. A8 -> H.264 passthrough
-> kvssink -> armyworm-cam-stream. Control path needs no AWS creds; only the
kvssink subprocess does (inherited from run_kvs_controller.sh).
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

CAMERA_ID         = os.environ.get("CAMERA_ID", "worm_cam")
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "5"))
API_BASE = os.environ.get(
    "API_BASE", "https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com").rstrip("/")

RTSP_HOST = os.environ.get("RTSP_HOST", "192.168.144.25")
RTSP_PORT = os.environ.get("RTSP_PORT", "8554")
RTSP_PATH = os.environ.get("RTSP_PATH", "/main.264")
RTSP_URL  = os.environ.get("RTSP_URL", f"rtsp://{RTSP_HOST}:{RTSP_PORT}{RTSP_PATH}")

KVS_SDK_DIR = os.environ.get(
    "KVS_SDK_DIR", os.path.expanduser("~/amazon-kinesis-video-streams-producer-sdk-cpp"))
GST_PLUGIN_PATH = os.environ.get("GST_PLUGIN_PATH", f"{KVS_SDK_DIR}/build")
LD_LIB_PATH     = os.environ.get("LD_LIBRARY_PATH", f"{KVS_SDK_DIR}/open-source/local/lib")
AWS_REGION      = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("kvs_controller")

def fetch_desired_state() -> Tuple[Optional[bool], Optional[str]]:
    """Control signal: GET /stream/status from pest-monitoring-api (plain HTTPS,
    no AWS creds). Returns (stream_enabled, kvs_stream_name), or (None, None) on
    any error - the caller then keeps the current state and retries next cycle.
    The dashboard toggle writes stream_enabled to DynamoDB; this read is how the
    Orin learns about it."""
    url = f"{API_BASE}/stream/status?camera={urllib.parse.quote(CAMERA_ID)}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("stream_enabled", False)), data.get("kvs_stream_name")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as e:
        log.error(f"API poll failed: {e}")
        return None, None

gst_proc: Optional[subprocess.Popen] = None

def build_gst_args(stream_name: str) -> list:
    """Assemble the gst-launch argv. Video path: A8 RTSP (192.168.144.25:8554)
    -> rtph264depay -> h264parse -> kvssink -> AWS KVS stream `stream_name`
    (armyworm-cam-stream) in nbk2, us-east-1. Pure passthrough, no re-encode."""
    return [
        "gst-launch-1.0",
        "rtspsrc", f"location={RTSP_URL}",
        "protocols=tcp", "latency=0", "drop-on-latency=true",
        "!", "rtph264depay",
        "!", "h264parse",
        "!", "kvssink",
        f"stream-name={stream_name}",
        "storage-size=512",
        f"aws-region={AWS_REGION}",
        "fragment-duration=2",
        "max-latency=2",
    ]

def is_gst_running() -> bool:
    """True while the GStreamer child process exists and has not exited."""
    return gst_proc is not None and gst_proc.poll() is None

def start_gst(stream_name: str):
    """Spawn the GStreamer pipeline as a child process in its own process group.
    AWS creds + SDK paths ride in via the environment (exported by
    run_kvs_controller.sh); video flows A8 -> kvssink -> KVS cloud. stdout/stderr
    are discarded - kvssink writes its own ./log/kvs.log under the SDK build dir."""
    global gst_proc
    if is_gst_running():
        log.warning("GStreamer already running, skipping start")
        return
    env = os.environ.copy()
    env["GST_PLUGIN_PATH"] = GST_PLUGIN_PATH
    env["LD_LIBRARY_PATH"] = LD_LIB_PATH
    log.info(f"Starting GStreamer -> stream={stream_name}, rtsp={RTSP_HOST}:{RTSP_PORT}{RTSP_PATH}")
    gst_proc = subprocess.Popen(
        build_gst_args(stream_name), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid)
    log.info(f"GStreamer PID={gst_proc.pid}")

def stop_gst():
    """Kill the whole GStreamer process group: SIGTERM first, escalate to
    SIGKILL after 5 s. Group-kill matters - gst-launch forks helpers."""
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
        pass
    gst_proc = None

def graceful_exit(signum, _frame):
    """systemd stop / Ctrl+C handler: tear the pipeline down before exiting so
    no orphan gst process keeps pushing to KVS."""
    log.info(f"Received signal {signum}, shutting down...")
    stop_gst()
    sys.exit(0)

def main():
    """Reconcile loop, every POLL_INTERVAL_SEC (5 s). Data flow:
    GET /stream/status?camera=armyworm_go2_a8mini -> desired on/off state;
    compare with the actual child process; start/stop the pipeline to match.
    This loop is what turns the dashboard's DynamoDB flag into real video."""
    signal.signal(signal.SIGTERM, graceful_exit)
    signal.signal(signal.SIGINT, graceful_exit)
    log.info("=== KVS Controller starting (Orin / A8) ===")
    log.info(f"camera_id={CAMERA_ID}, control={API_BASE}/stream/status")
    log.info(f"rtsp={RTSP_HOST}:{RTSP_PORT}{RTSP_PATH}")
    last_logged_state = None
    while True:
        desired, stream_name = fetch_desired_state()
        if desired is None:
            time.sleep(POLL_INTERVAL_SEC)
            continue
        running = is_gst_running()
        if gst_proc is not None and not running:
            log.warning(f"GStreamer exited unexpectedly (rc={gst_proc.returncode})")
            globals()["gst_proc"] = None
            running = False
        if desired and not running:
            if not stream_name:
                log.error(f"stream_enabled=true but kvs_stream_name empty for {CAMERA_ID}")
            else:
                start_gst(stream_name)
        elif not desired and running:
            stop_gst()
        state_tuple = (desired, running)
        if state_tuple != last_logged_state:
            log.info(f"State: desired={desired}, running={running}")
            last_logged_state = state_tuple
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
