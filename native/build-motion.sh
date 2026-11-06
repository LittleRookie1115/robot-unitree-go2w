#!/usr/bin/env bash
# Compile the inherited guard and SDK transport without starting either one.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
[[ -f "$GO2_NATIVE_BUILD/ros/install/setup.bash" ]] || {
  echo "Build the native ROS message overlay first with bash build.sh." >&2; exit 2;
}
bash "$GO2_NATIVE_ROOT/packages/go2_chassis_motion/scripts/build.sh"
set +u
source /opt/ros/foxy/setup.bash
source /home/unitree/cyclonedds_ws/install/setup.bash
source "$GO2_NATIVE_BUILD/ros/install/setup.bash"
set -u
/usr/bin/colcon --log-base "$GO2_NATIVE_BUILD/chassis-ros/log" build \
  --base-paths \
    "$GO2_NATIVE_ROOT/packages/go2_chassis/ros2_ws/src/go2_chassis_adapter" \
    "$GO2_NATIVE_ROOT/packages/go2_chassis/ros2_ws/src/go2_native_chassis_adapter" \
  --build-base "$GO2_NATIVE_BUILD/chassis-ros/build" \
  --install-base "$GO2_NATIVE_BUILD/chassis-ros/install" \
  --merge-install --cmake-args -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release \
    -DPYTHON_EXECUTABLE=/usr/bin/python3
cmake -S "$GO2_NATIVE_ROOT/packages/go2_chassis/sdk_daemon" \
  -B "$GO2_NATIVE_BUILD/chassis-sdk/build" \
  -DUNITREE_SDK2_DIR="$GO2_NATIVE_ROOT/third_party/unitree_sdk2" \
  -DBUILD_EXAMPLES=OFF \
  -DCMAKE_INSTALL_PREFIX="$GO2_NATIVE_BUILD/chassis-sdk/install"
cmake --build "$GO2_NATIVE_BUILD/chassis-sdk/build" --parallel 2 \
  --target go2_sport_daemon go2_native_identity_probe go2_sdk_state_probe
cmake --install "$GO2_NATIVE_BUILD/chassis-sdk/build"
cmake -S "$GO2_NATIVE_ROOT/packages/go2_chassis" -B "$GO2_NATIVE_BUILD/chassis-tests" \
  -DGO2_CHASSIS_BUILD_OFFLINE_TESTS=ON
cmake --build "$GO2_NATIVE_BUILD/chassis-tests" --parallel 2
(
  cd "$GO2_NATIVE_BUILD/chassis-tests"
  ctest --output-on-failure
)
echo "Foxy guarded chassis artifacts compiled. No real motion backend was executed."
