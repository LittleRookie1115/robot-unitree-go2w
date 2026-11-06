#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
source "$root/native/env.sh"
package="$root/packages/go2w_native"
# This package build must not run the deployment build. rbnx sets
# RBNX_PACKAGE_ROOT to this package, and the deployment build calls the
# audio bridge, which treats that variable as its own root.
unset RBNX_PACKAGE_ROOT
rbnx codegen -p "$package" --mcp
echo "go2w_native codegen complete. No ROS graph or hardware client was started."
