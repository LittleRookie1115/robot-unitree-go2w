#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "[validate] OFFLINE ONLY - no ROS graph, network, or motion command is used"

"$DEPLOY_DIR/scripts/verify_submodule_pins.sh"

find "$DEPLOY_DIR" -type f -name '*.sh' \
  -not -path '*/rbnx-build/*' \
  -not -path '*/third_party/*' -print0 \
  | xargs -0 -n1 bash -n

DEPLOY_DIR="$DEPLOY_DIR" python3 - <<'PY'
from pathlib import Path
import os
import xml.etree.ElementTree as ET
import yaml

root = Path(os.environ["DEPLOY_DIR"])
excluded_yaml_trees = {"rbnx-build", "third_party", "logs"}
for path in sorted(root.rglob("*.yaml")):
    if excluded_yaml_trees.isdisjoint(path.parts):
        yaml.safe_load(path.read_text(encoding="utf-8"))
for path in sorted(root.rglob("*.yml")):
    if excluded_yaml_trees.isdisjoint(path.parts):
        yaml.safe_load(path.read_text(encoding="utf-8"))
ET.parse(root / "config" / "navigate.xml")
PY

python3 -m unittest discover \
  -s "$DEPLOY_DIR/packages/go2_chassis/tests" -p 'test_*.py'

command -v g++ >/dev/null 2>&1 || {
  echo "[validate] missing offline test compiler: g++" >&2
  exit 1
}
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TEMP_DIR"' EXIT
g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror \
  -I"$DEPLOY_DIR/packages/go2_chassis/include" \
  "$DEPLOY_DIR/packages/go2_chassis/tests/protocol_guard_test.cpp" \
  -o "$TEMP_DIR/protocol_guard_test"
"$TEMP_DIR/protocol_guard_test"

bash "$DEPLOY_DIR/packages/go2_sensors/tests/run_offline_tests.sh"
python3 -m unittest discover \
  -s "$DEPLOY_DIR/deploy/jetson-d435i-readonly" -p 'test_*.py'
python3 -m unittest discover -s "$DEPLOY_DIR/tests" -p 'test_*.py'
(
  cd "$DEPLOY_DIR/packages/go2w_chassis_motion/tests"
  PYTHONPATH="$DEPLOY_DIR/packages/go2w_chassis_motion${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m unittest \
      test_motion test_native_network test_native_state \
      test_runtime test_worker test_preflight
)
PYTHONPATH="$DEPLOY_DIR/packages/go2w_visual_action${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m unittest discover \
    -s "$DEPLOY_DIR/packages/go2w_visual_action/tests" -p 'test_*.py'
g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror \
  -I"$DEPLOY_DIR/packages/go2_chassis/include" \
  "$DEPLOY_DIR/packages/go2_chassis/tests/native_state_clock_test.cpp" \
  -o "$TEMP_DIR/native_state_clock_test"
"$TEMP_DIR/native_state_clock_test"
g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -pthread \
  -I"$DEPLOY_DIR/packages/go2_chassis/include" \
  "$DEPLOY_DIR/packages/go2_chassis/tests/native_profile_test.cpp" \
  -o "$TEMP_DIR/native_profile_test"
"$TEMP_DIR/native_profile_test"

python3 -m unittest discover \
  -s "$DEPLOY_DIR/sim/onboarding/tests" -p 'test_*.py'

echo "[validate] PASS"
