#!/usr/bin/env bash
# Fetch the pinned ROS Foxy sensor_msgs_py and install it for the system interpreter.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$root/native/deps.lock"
source="$root/rbnx-build/native/src/common_interfaces"
mkdir -p "$(dirname "$source")"
if [[ -d "$source/.git" ]]; then
  actual="$(git -C "$source" rev-parse HEAD)"
  [[ "$actual" == "$SENSOR_MSGS_PY_COMMIT" ]] || {
    echo "sensor_msgs_py source is $actual, expected $SENSOR_MSGS_PY_COMMIT." >&2
    echo "Remove $source and run this fetch again." >&2
    exit 2
  }
else
  rm -rf "$source"
  mkdir -p "$source"
  git -C "$source" init
  git -C "$source" remote add origin "$SENSOR_MSGS_PY_REPOSITORY"
  git -C "$source" fetch --depth 1 origin "$SENSOR_MSGS_PY_COMMIT"
  git -C "$source" checkout --detach FETCH_HEAD
fi
[[ -f "$source/sensor_msgs_py/setup.py" ]] || {
  echo "Pinned common_interfaces checkout has no sensor_msgs_py/setup.py." >&2
  exit 2
}
SENSOR_MSGS_PY_SOURCE="$source/sensor_msgs_py" bash "$root/native/install-ros-deps.sh"
echo "Installed sensor_msgs_py from $SENSOR_MSGS_PY_COMMIT"
