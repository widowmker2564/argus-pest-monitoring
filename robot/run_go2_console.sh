#!/bin/bash
# =============================================================================
# run_go2_console.sh  (Jetson Orin)
# One command to start the patrol console. Sources the ROS 2 Foxy environment
# non-interactively (the .bashrc fishros block PROMPTS foxy/noetic and would
# hang a non-interactive shell -- see docs/hardware.md), then hands over to
# go2_console.py.
#
# Usage:  ./run_go2_console.sh            normal run
#         ./run_go2_console.sh --no-upload   rehearse a route, send nothing to S3
#
# Run it inside tmux. The dog moves during a patrol, so launch it untethered
# with the remote in hand as an e-stop.
# =============================================================================
set -e

HOME_DIR="/home/unitree"

source /opt/ros/foxy/setup.bash
source "$HOME_DIR/cyclonedds_ws/install/setup.bash"
source "$HOME_DIR/setup_go2.sh"

# -u keeps the interactive prompts unbuffered over ssh.
exec python3 -u "$HOME_DIR/go2/go2_console.py" "$@"
