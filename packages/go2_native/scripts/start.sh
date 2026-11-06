#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
source "$root/native/env.sh"
package="$root/packages/go2_native"
# The SDK runs on Python 3.11; only the separate ROS worker imports Foxy.
export PYTHONPATH="$package:$package/rbnx-build/codegen/proto_gen:$package/rbnx-build/codegen/robonix_mcp_types"
exec "$GO2_SDK_PYTHON" -m go2_native.provider
