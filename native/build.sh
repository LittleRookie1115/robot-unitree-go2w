#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
# A deployment build is not one Robonix package. Nested package scripts must
# resolve their own directories instead of inheriting this process's package.
unset RBNX_PACKAGE_ROOT
mark_built() {
  mkdir -p "$1/rbnx-build"
  : > "$1/rbnx-build/.rbnx-built"
}
[[ "$(uname -m)" == aarch64 && -f /opt/ros/foxy/setup.bash ]] || {
  echo "This profile requires ARM64 and /opt/ros/foxy." >&2; exit 2;
}
mkdir -p "$GO2_NATIVE_BUILD"
"$GO2_SDK_PYTHON" -c 'import robonix_api, grpc_tools.protoc'
bash "$GO2_NATIVE_ROOT/packages/go2w_native/scripts/build.sh"
mark_built "$GO2_NATIVE_ROOT/packages/go2w_native"
bash "$GO2_NATIVE_ROOT/packages/go2w_chassis_motion/scripts/build.sh"
mark_built "$GO2_NATIVE_ROOT/packages/go2w_chassis_motion"
bash "$GO2_NATIVE_ROOT/packages/go2w_visual_action/scripts/build.sh"
mark_built "$GO2_NATIVE_ROOT/packages/go2w_visual_action"
audio_bridge="$(bash "$GO2_NATIVE_ROOT/native/fetch-audio-bridge.sh")"
AUDIO_CLIENT_BRIDGE_PYTHON="$GO2_SDK_PYTHON" bash "$audio_bridge/scripts/build.sh"
mark_built "$audio_bridge"
bash "$GO2_NATIVE_ROOT/native/build-voice.sh"
mark_built "$GO2_NATIVE_ROOT/packages/go2w_native_speech"
set +u
source /opt/ros/foxy/setup.bash
cyclonedds_setup="${CYCLONEDDS_SETUP:-/home/unitree/cyclonedds_ws/install/setup.bash}"
[[ -f "$cyclonedds_setup" ]] || {
  echo "Missing CycloneDDS overlay: $cyclonedds_setup" >&2
  echo "Install it or set CYCLONEDDS_SETUP to its setup.bash." >&2
  exit 2
}
source "$cyclonedds_setup"
set -u
bash "$GO2_NATIVE_ROOT/native/fetch-sensor-msgs-py.sh"
/usr/bin/colcon --log-base "$GO2_NATIVE_BUILD/ros/log" build \
  --base-paths \
    "$GO2_NATIVE_ROOT/third_party/unitree_ros2/cyclonedds_ws/src/unitree/unitree_go" \
    "$GO2_NATIVE_ROOT/third_party/unitree_ros2/cyclonedds_ws/src/unitree/unitree_api" \
    "$GO2_NATIVE_ROOT/packages/go2_sensors" \
  --build-base "$GO2_NATIVE_BUILD/ros/build" \
  --install-base "$GO2_NATIVE_BUILD/ros/install" \
  --merge-install --parallel-workers 2 \
  --cmake-args -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release \
    -DPython3_EXECUTABLE=/usr/bin/python3 -DPYTHON_EXECUTABLE=/usr/bin/python3
cmake -S "$GO2_NATIVE_ROOT/packages/go2_sensors/camera_daemon" \
  -B "$GO2_NATIVE_BUILD/camera/build" \
  -DUNITREE_SDK2_ROOT="$GO2_NATIVE_ROOT/third_party/unitree_sdk2" \
  -DBUILD_EXAMPLES=OFF -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$GO2_NATIVE_BUILD/camera/install"
cmake --build "$GO2_NATIVE_BUILD/camera/build" --parallel 2 --target go2_camera_daemon
cmake --install "$GO2_NATIVE_BUILD/camera/build" --component go2_camera_runtime
"$GO2_SDK_PYTHON" "$GO2_NATIVE_ROOT/native/fetch_go2w_model.py"
"$GO2_SDK_PYTHON" "$GO2_NATIVE_ROOT/native/prepare_model.py"
echo "Native Foxy build complete. No hardware client was started."
