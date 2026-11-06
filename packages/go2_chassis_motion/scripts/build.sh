#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
source "$root/native/env.sh"
package="$root/packages/go2_chassis_motion"
rbnx codegen -p "$package" --mcp
mcp_gen="$package/rbnx-build/codegen/robonix_mcp_types"
PYTHONPATH="$package:$package/rbnx-build/codegen/proto_gen:$mcp_gen" \
  "$GO2_SDK_PYTHON" -m unittest discover -s "$package/tests" -v
echo "Native chassis gRPC/MCP build complete. No ROS publisher or Unitree SDK client was started."
