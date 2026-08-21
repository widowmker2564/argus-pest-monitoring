#!/bin/bash
# =============================================================================
# run_kvs_controller.sh  (mini PC VM, /home/wilburteo)
# Wrapper the systemd service execs. Exports the AWS + RTSP creds into the env,
# then runs the polling daemon.
#
# SECRET VALUES ARE PLACEHOLDERS in this repo copy. Fill them in on the VM only
# (here, or preferably a systemd EnvironmentFile) and NEVER commit real secrets.
#   - AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY : the nbk2 cag_user keys
#   - RTSP_PASS : the Hikvision 192.168.1.66 password
# =============================================================================
export AWS_ACCESS_KEY_ID="<SET_ON_VM>"
export AWS_SECRET_ACCESS_KEY="<SET_ON_VM>"
export AWS_DEFAULT_REGION="us-east-1"

# --- which camera this instance drives (current deployment = moth cam) ---
export CAMERA_ID="moth_cam"

# --- Hikvision .66 RTSP source ---
export RTSP_USER="admin"
export RTSP_PASS="<SET_ON_VM>"
export RTSP_HOST="192.168.1.66"
export RTSP_PATH="/Streaming/channels/101"

exec python3 /home/wilburteo/kvs_controller.py