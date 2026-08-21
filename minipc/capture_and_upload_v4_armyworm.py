r"""
capture_and_upload_v4_armyworm.py - Hikvision RTSP -> S3 (nbk2 account)

Migrated from capture_and_upload_v3_person_cam.py (W5, old account fyp-practice-qrz).
CURRENT TARGET (2026-08): production account 506868652945, bucket argus-frames-506868652945,
profile 'prod'. The W7 history below is kept for the record.

W7 moved the backend into the shared account nbk2 (366356442579). Four things changed:
CAMERA_ID person_cam->armyworm_go2_a8mini, bucket fyp-practice-qrz->frames-armyworm-366356442579,
AWS profile None->nbk2, config table system-config->pest-monitoring-system-config. The RTSP
password is no longer hardcoded - it is read from the RTSP_URL environment variable.

Run modes (full how-to at the bottom of this docstring):
  python capture_and_upload_v4_armyworm.py                       # loop, dashboard-controlled
  python capture_and_upload_v4_armyworm.py --ignore-config --interval 10   # loop, fixed interval
  python capture_and_upload_v4_armyworm.py --once               # one frame, then exit
  python capture_and_upload_v4_armyworm.py --image armyworm.jpg # upload a local file, no camera

  # Set RTSP only in the terminal (never hardcode the password):
  #   export RTSP_URL="rtsp://<user>:<pass>@192.168.1.66:554/Streaming/Channels/101"
  # Install deps:  pip install boto3 opencv-python   (opencv-python-headless on a headless VM)
"""

# =============================================================================
# WHERE THIS SCRIPT SITS IN THE SYSTEM  (data flow, left to right)
# =============================================================================
#
#   [THIS SCRIPT]            the PRODUCER end - turns a camera into S3 objects
#        |  put_object(key = "frames/{camera}/{waypoint}/{ts}.jpg")
#        v
#   S3  argus-frames-506868652945              <-- has an S3 "PutObject" event trigger
#        |  (S3 event fires automatically on every new frames/ object)
#        v
#   Lambda  pest-detection-processor           <-- parses camera_id from the KEY,
#        |                                          loads that camera's model + target label
#        v
#   Rekognition Custom Labels (armyworm model) <-- must be RUNNING for detections (~$1/hr)
#        |  bounding boxes + confidence
#        v
#   +---> DynamoDB  pest-monitoring-detections  (metadata: pest, conf, bbox, zone, time)
#   +---> S3  processed-images-armyworm-...      (annotated image with boxes)
#   +---> SES email alert  (only if a pest is detected above the threshold)
#        |
#        v
#   Lambda pest-monitoring-api  (HTTP API vzfl7s6z00)  <-- dashboard reads history/gallery from here
#        |
#        v
#   dashboard_v3_8.html  (Live / Gallery / Analytics / Settings)
#
# This script ONLY does the first hop (camera -> S3). Everything after the S3
# upload is triggered automatically by the cloud; this script never calls
# Rekognition, DynamoDB, or SES directly.
#
# =============================================================================
# SERVICE CONTROL - how capture is started and stopped
# =============================================================================
#
#   START/STOP (this script):
#     * Manual loop ......... run the script with no flags; press Ctrl-C to stop.
#     * Dashboard-controlled  loop mode reads the auto_capture flag from
#                             pest-monitoring-system-config every cycle. Toggle it
#                             from the dashboard Settings to start/stop capture
#                             without touching this process. (see get_capture_settings)
#     * Always-on service ... wrap in a systemd unit like kvs-controller.service on
#                             the lab VM so it survives reboot.
#     * One-shot ............ --once (single frame) / --image (no camera at all).
#
#   DOWNSTREAM SERVICE (must be ON for detections to appear):
#     * The Rekognition armyworm model is hourly-billed. Start it from the
#       dashboard model control (or it is auto-started by pest-camera-scheduler),
#       and STOP it after testing. If it is stopped, this script still uploads
#       fine but the processor's detect_custom_labels call fails and no detection
#       row / gallery image / email is produced.
# =============================================================================

import os
import sys
import time
import argparse
from datetime import datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# #############################################################################
# IMPORTANT INTERFACES & PARAMETERS  - the only things you normally tune
# (each can also be overridden by env var or CLI flag - see parse_args)
# #############################################################################
DEFAULTS = {
    # camera_id is the ROUTING KEY: the processor reads it from the S3 object key
    # and loads THIS camera's row from pest-monitoring-cameras (which carries the
    # model ARN + target label). Use worm_cam to hit the armyworm model.
    "camera":   "worm_cam",                       # must match a row in pest-monitoring-cameras
    # waypoint_id = the "zone" label shown on the detection. Fixed camera => fixed_cam.
    "waypoint": "fixed_cam",
    # This MUST be the bucket that has the S3->processor event trigger, or nothing fires.
    "bucket":   "argus-frames-506868652945",
    # prod = the named AWS CLI profile for the production account. "" => default chain.
    "profile":  "prod",
    "region":   "us-east-1",
    # Fallback capture interval (seconds) when system-config has none, or with --ignore-config.
    "interval": 60,
}
# The remote on/off switch + interval live in this DynamoDB row:
CONFIG_TABLE = "pest-monitoring-system-config"    # table
CONFIG_KEY   = "detection_settings"               # primary key (config_key) of the single config row
JPEG_QUALITY = 90                                 # 0-100; capture_frame encodes at this quality
MIN_INTERVAL = 5                                  # safety floor so we never poll/capture too fast
# NOTE: RTSP_URL (incl. the camera password) is intentionally NOT here - it is read
# from the environment at runtime so no credential ever lives in this file.
# #############################################################################


def log(msg):
    """Timestamped stdout line. (Single logging helper used everywhere.)"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# =============================================================================
# MODULE: make_session  -  IDENTITY / AWS CONNECTION
# Builds the boto3 session that every AWS call below uses (S3 + DynamoDB).
# IN : profile name, region     OUT: a boto3.Session
# This is where "which AWS account / which credentials" is decided.
# =============================================================================
def make_session(profile, region):
    """Build a session for the nbk2 profile. Empty/None profile -> default credential chain."""
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


# =============================================================================
# MODULE: get_capture_settings  -  REMOTE ON/OFF SWITCH + INTERVAL
# Reads the dashboard-controlled flags from pest-monitoring-system-config.
# IN : DynamoDB table handle, fallback interval
# OUT: (enabled: bool, interval: int)
# DATA FLOW: dashboard Settings -> pest-monitoring-api -> this DynamoDB row -> here.
# This is how the dashboard starts/stops capture without killing this process.
# =============================================================================
def get_capture_settings(config_t, fallback_interval):
    """Read auto_capture + capture_interval from system-config (dashboard-controlled)."""
    try:
        r = config_t.get_item(Key={"config_key": CONFIG_KEY})
        item = r.get("Item", {}) or {}
        enabled  = bool(item.get("auto_capture", False))
        interval = int(item.get("capture_interval", fallback_interval))
        return enabled, max(MIN_INTERVAL, interval)
    except (BotoCoreError, ClientError, ValueError) as e:
        log(f"[Config] read failed: {e}; using defaults (idle)")
        return False, fallback_interval


# =============================================================================
# MODULE: capture_frame  -  SENSOR READ (camera -> JPEG bytes)
# Opens the RTSP stream, flushes stale buffered frames, grabs one fresh frame,
# and JPEG-encodes it. Retries a few times because RTSP open can be flaky.
# IN : rtsp_url (from env)       OUT: JPEG bytes, or None on failure
# cv2 is imported lazily so --image mode runs without opencv installed.
# =============================================================================
def capture_frame(rtsp_url, attempts=3):
    """Grab a single JPEG from the RTSP stream. Returns bytes or None."""
    try:
        import cv2
    except ImportError:
        log("[Capture] opencv not installed. Run: pip install opencv-python "
            "(or use --image to test without the camera)")
        return None

    for attempt in range(1, attempts + 1):
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            log(f"[Capture] cannot open RTSP (attempt {attempt}/{attempts})")
            cap.release()
            time.sleep(1)
            continue
        # Flush a couple of stale buffered frames, then grab a fresh one.
        for _ in range(2):
            cap.read()
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            log(f"[Capture] frame grab failed (attempt {attempt}/{attempts})")
            time.sleep(1)
            continue
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if ok:
            return buf.tobytes()
        log(f"[Capture] JPEG encode failed (attempt {attempt}/{attempts})")
    return None


# =============================================================================
# MODULE: load_local_image  -  ALTERNATE INPUT (file -> bytes), used by --image
# Lets you exercise the whole cloud chain with a known image, no camera needed.
# IN : local file path           OUT: (bytes, content_type) or (None, None)
# =============================================================================
def load_local_image(path):
    """Read a local jpg/jpeg/png for --image mode."""
    ext = os.path.splitext(path)[1].lower()
    ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}.get(ext)
    if not ctype:
        log(f"[Image] unsupported extension '{ext}'. Use .jpg/.jpeg/.png "
            "(the processor only accepts these).")
        return None, None
    if not os.path.isfile(path):
        log(f"[Image] file not found: {path}")
        return None, None
    with open(path, "rb") as f:
        return f.read(), ctype


# =============================================================================
# MODULE: upload_frame  -  HAND-OFF TO THE CLOUD (the trigger point)
# Puts the image into S3 under the EXACT key shape the processor expects:
#     frames/{camera}/{waypoint}/{timestamp}.jpg
# The processor's parse_s3_key requires first segment "frames" and >= 4 segments;
# it then uses {camera} to pick the model. Get this key wrong and either nothing
# fires or it falls back to the manual_upload camera (wrong model). This single
# put_object is what starts the entire downstream pipeline.
# IN : s3 client, bucket, camera, waypoint, image bytes, content_type
# OUT: the S3 key that was written
# =============================================================================
def upload_frame(s3, bucket, camera, waypoint, body, content_type="image/jpeg", basename=None):
    """Upload under frames/{camera}/{waypoint}/{file} so the processor routes it correctly."""
    # Microseconds in the name guarantee uniqueness even for rapid --once runs.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    ext = ".png" if content_type == "image/png" else ".jpg"
    filename = f"{ts}{ext}" if not basename else f"{ts}_{basename}"
    key = f"frames/{camera}/{waypoint}/{filename}"
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    return key


# =============================================================================
# MODULE: parse_args  -  RUNTIME CONFIG RESOLUTION
# Precedence for every setting: CLI flag  >  environment variable  >  DEFAULTS.
# This is the other place (besides the DEFAULTS block) where parameters come in.
# =============================================================================
def parse_args():
    p = argparse.ArgumentParser(description="Capture Hikvision RTSP frames and upload to nbk2 S3.")
    p.add_argument("--camera",   default=os.environ.get("CAMERA_ID",   DEFAULTS["camera"]))
    p.add_argument("--waypoint", default=os.environ.get("WAYPOINT_ID", DEFAULTS["waypoint"]))
    p.add_argument("--bucket",   default=os.environ.get("S3_BUCKET",   DEFAULTS["bucket"]))
    p.add_argument("--profile",  default=os.environ.get("AWS_PROFILE", DEFAULTS["profile"]))
    p.add_argument("--region",   default=os.environ.get("AWS_REGION",  DEFAULTS["region"]))
    p.add_argument("--interval", type=int,
                   default=int(os.environ.get("CAPTURE_INTERVAL", DEFAULTS["interval"])),
                   help="Seconds between captures with --ignore-config, or fallback if config has none.")
    p.add_argument("--once", action="store_true",
                   help="Capture exactly one frame and exit (demo 'capture now' / smoke test).")
    p.add_argument("--image", metavar="PATH",
                   help="Upload a local jpg/png instead of the camera (test the chain without RTSP).")
    p.add_argument("--ignore-config", action="store_true",
                   help="Loop on --interval regardless of the dashboard auto_capture flag.")
    return p.parse_args()


# =============================================================================
# MODULE: main  -  ORCHESTRATOR (picks a mode, then runs it)
# Dispatch:  --image  -> upload one local file and exit
#            --once   -> capture one camera frame and exit
#            (else)   -> loop:  [poll on/off] -> [capture] -> [upload] -> [sleep]
# The loop is the long-running "service"; the on/off + interval come from
# get_capture_settings unless --ignore-config is set.
# =============================================================================
def main():
    args = parse_args()
    # ---- set up the AWS connection once, reused for every upload ----
    session = make_session(args.profile, args.region)
    s3 = session.client("s3")

    log(f"[Start] camera={args.camera} waypoint={args.waypoint} "
        f"bucket={args.bucket} profile={args.profile or '(default)'}")

    # ---- MODE 1: --image  (no camera; test the full chain with a local file) ----
    if args.image:
        body, ctype = load_local_image(args.image)
        if body is None:
            sys.exit(1)
        try:
            key = upload_frame(s3, args.bucket, args.camera, args.waypoint, body,
                               content_type=ctype, basename=os.path.basename(args.image))
            log(f"[Upload] s3://{args.bucket}/{key} ({len(body)//1024} KB) - chain triggered")
        except (BotoCoreError, ClientError) as e:
            log(f"[Upload] FAILED: {e}")
            sys.exit(1)
        return

    # ---- From here on we need the camera, so RTSP_URL must be set in the env ----
    rtsp_url = os.environ.get("RTSP_URL")
    if not rtsp_url:
        log("[Fatal] RTSP_URL not set. Export it first (do NOT hardcode the password):")
        log('        export RTSP_URL="rtsp://<user>:<pass>@192.168.1.66:554/Streaming/Channels/101"')
        log("        (or use --image PATH to test without the camera)")
        sys.exit(1)

    # ---- MODE 2: --once  (single capture, then exit; demo 'capture now') ----
    if args.once:
        jpeg = capture_frame(rtsp_url)
        if jpeg is None:
            log("[Fatal] capture failed")
            sys.exit(1)
        try:
            key = upload_frame(s3, args.bucket, args.camera, args.waypoint, jpeg)
            log(f"[Upload] s3://{args.bucket}/{key} ({len(jpeg)//1024} KB) - chain triggered")
        except (BotoCoreError, ClientError) as e:
            log(f"[Upload] FAILED: {e}")
            sys.exit(1)
        return

    # ---- MODE 3: loop  (the long-running capture "service") ----
    # DynamoDB handle is only needed in loop mode (to read the on/off switch).
    config_t = session.resource("dynamodb").Table(CONFIG_TABLE)
    if args.ignore_config:
        log(f"[Loop] --ignore-config: fixed interval {args.interval}s")
    else:
        log("[Loop] polling system-config for auto_capture + capture_interval")

    while True:
        # --- 1. decide whether to capture this cycle, and how long to wait ---
        if args.ignore_config:
            interval = max(MIN_INTERVAL, args.interval)
        else:
            enabled, interval = get_capture_settings(config_t, args.interval)
            if not enabled:                       # dashboard switch is OFF -> idle
                log(f"[Idle] auto_capture=false, sleeping {interval}s")
                time.sleep(interval)
                continue

        # --- 2. read one frame from the camera ---
        jpeg = capture_frame(rtsp_url)
        if jpeg is None:
            log(f"[Skip] no frame this cycle, retry in {interval}s")
            time.sleep(interval)
            continue

        # --- 3. hand it to S3 (this is what triggers the cloud pipeline) ---
        try:
            key = upload_frame(s3, args.bucket, args.camera, args.waypoint, jpeg)
            log(f"[Upload] s3://{args.bucket}/{key} ({len(jpeg)//1024} KB)")
        except (BotoCoreError, ClientError) as e:
            log(f"[Upload] failed: {e}")

        # --- 4. wait, then repeat ---
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:                      # Ctrl-C is the normal way to stop loop mode
        print("\n[Stop] interrupted by user")
