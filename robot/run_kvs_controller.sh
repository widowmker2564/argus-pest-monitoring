#!/bin/bash
# =============================================================================
# run_kvs_controller.sh  (Jetson Orin)
# Wrapper the systemd service execs. Sets up the KVS SDK env + AWS credentials,
# then runs the polling daemon.
#
# Credentials are read from the STANDARD ~/.aws/credentials (cag_user) -- they
# are NOT hardcoded here. Only the access key id + secret are exported to the
# environment so the kvssink subprocess can authenticate.
# =============================================================================
set -e

HOME_DIR="/home/unitree"
SDK="$HOME_DIR/amazon-kinesis-video-streams-producer-sdk-cpp"
CRED="$HOME_DIR/.aws/credentials"

# --- KVS SDK runtime paths ---
export GST_PLUGIN_PATH="$SDK/build"
export LD_LIBRARY_PATH="$SDK/open-source/local/lib"

# --- AWS env (single source of truth = ~/.aws/credentials) ---
export AWS_DEFAULT_REGION="us-east-1"
export AWS_ACCESS_KEY_ID="$(awk -F' *= *' '/aws_access_key_id/{print $2; exit}' "$CRED")"
export AWS_SECRET_ACCESS_KEY="$(awk -F' *= *' '/aws_secret_access_key/{print $2; exit}' "$CRED")"

if [ -z "$AWS_ACCESS_KEY_ID" ] || [ -z "$AWS_SECRET_ACCESS_KEY" ]; then
    echo "ERROR: could not read AWS credentials from $CRED" >&2
    exit 1
fi

# --- which camera this instance drives ---
export CAMERA_ID="worm_cam"

# kvssink writes ./log/kvs.log relative to CWD, and reads ../kvs_log_configuration
mkdir -p "$SDK/build/log"
cd "$SDK/build"

exec python3 "$HOME_DIR/go2/kvs_controller.py"
