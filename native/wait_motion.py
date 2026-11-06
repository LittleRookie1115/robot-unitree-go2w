#!/usr/bin/env python3
"""Wait for transport readiness, without enabling or arming anything."""
import json
import argparse
import subprocess
import time
from chassis import motion_status

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--disabled", action="store_true")
parser.add_argument("--enabled", action="store_true")
parser.add_argument("--available", action="store_true")
args = parser.parse_args()
unit = ("robonix-go2-native" if args.disabled or args.available or
        subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                        "robonix-go2-native"]).returncode == 0 else "robonix-go2w-motion")
end = time.monotonic()+30
last = {}
while time.monotonic() < end:
    if subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                       unit]).returncode:
        raise SystemExit("Go2-W session exited. Inspect journalctl --user -u robonix-go2w-motion")
    try:
        last = motion_status("127.0.0.1:50051")
        guard = last.get("guard", {})
        ready = (True if args.available else
                 last.get("motion_enabled") is False if args.disabled else
                 last.get("motion_enabled") and
                 (guard.get("ready") and guard.get("session_enabled") if args.enabled else
                  guard.get("can_arm") or guard.get("ready")))
        if ready:
            print(json.dumps(last, indent=2))
            raise SystemExit(0)
    except Exception as error:
        last = {"status_error": str(error)}
    time.sleep(0.2)
raise SystemExit("Transport is not ready to arm: " + json.dumps(last))
