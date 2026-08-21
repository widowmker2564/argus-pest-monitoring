#!/bin/bash
# =============================================================================
# run_patrol_scheduler.sh  (Jetson Orin)
# Wrapper the systemd service execs. Sources the ROS 2 Foxy env
# non-interactively (the .bashrc fishros block PROMPTS foxy/noetic and would
# hang a non-interactive shell -- see docs/hardware.md), then runs the
# polling daemon. patrol_scheduler.py's own subprocess launch of
# go2_patrol_gated.py inherits this same sourced environment, so the patrol
# script's rclpy import and /uslam/* topics work without re-sourcing per run.
# =============================================================================
set -e

HOME_DIR="/home/unitree"

source /opt/ros/foxy/setup.bash
source "$HOME_DIR/cyclonedds_ws/install/setup.bash"
source "$HOME_DIR/setup_go2.sh"

exec python3 "$HOME_DIR/go2/patrol_scheduler.py"
