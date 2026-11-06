#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$root/native/env.sh"
source_dir="${SENSOR_MSGS_PY_SOURCE:?set SENSOR_MSGS_PY_SOURCE to the ROS Foxy sensor_msgs_py source directory}"
install_dir="$GO2_NATIVE_BUILD/ros-python-deps"
work_dir="$GO2_NATIVE_BUILD/ros-deps/temp"
mkdir -p "$install_dir" "$work_dir"
TMPDIR="$work_dir" /usr/bin/python3 -m pip install --upgrade --no-cache-dir --no-deps \
  --no-build-isolation --target "$install_dir" "$source_dir"
