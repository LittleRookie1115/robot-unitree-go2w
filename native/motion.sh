#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
motion_python="${GO2_SDK_PYTHON:-$(dirname "$root")/.tools/rbnx-python/bin/python}"
case "${1:-}" in
  enable)
    shift
    # Migrate an old exclusive motion service before using the default provider.
    if systemctl --user is-active --quiet robonix-go2w-motion; then
      systemctl --user stop robonix-go2w-motion
    fi
    systemctl --user start robonix-go2-native
    "$motion_python" "$root/native/wait_motion.py" --available
    exec bash "$root/native/chassis.sh" enable "$@"
    ;;
  "")
    echo "Usage: bash native/motion.sh enable|disable|status|stop|<chassis command>" >&2
    exit 2
    ;;
  *)
    # disable revokes through its primitive; Core/Pilot/Client stay running.
    exec bash "$root/native/chassis.sh" "$@"
    ;;
esac
