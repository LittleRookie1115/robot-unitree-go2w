#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for script in "$root"/native/*.sh "$root/packages/go2_native/scripts/start.sh"; do
  bash -n "$script"
done
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s "$root/native" -p 'test_*.py' -v
temporary="$(mktemp -d)"
trap 'rm -rf -- "$temporary"' EXIT
g++ -std=c++17 -Wall -Wextra -Wconversion -Werror -pthread \
  -I"$root/packages/go2_sensors/include" \
  "$root/packages/go2_sensors/tests/test_camera_ipc.cpp" \
  -o "$temporary/camera-ipc"
"$temporary/camera-ipc"
echo "Native offline validation passed; no ROS graph or hardware connection was opened."
