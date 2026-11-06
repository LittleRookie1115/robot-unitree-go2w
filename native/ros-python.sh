#!/usr/bin/env bash
set -eo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$root/native/env.sh"
set +u
source /opt/ros/foxy/setup.bash
source /home/unitree/cyclonedds_ws/install/setup.bash
if [[ -f "$GO2_NATIVE_BUILD/ros/install/setup.bash" ]]; then
  source "$GO2_NATIVE_BUILD/ros/install/setup.bash"
fi
set -u
export LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="$GO2_NATIVE_BUILD/ros-python-deps${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 "$@"
