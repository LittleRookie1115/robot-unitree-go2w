#!/usr/bin/env python3
"""Read-only fast path for an idempotent human enable command."""
import json
from chassis import motion_status

try:
    status = motion_status("127.0.0.1:50051")
except Exception:
    raise SystemExit(1)
guard = status.get("guard", {})
if status.get("motion_enabled") and not status.get("faulted") and guard.get("session_enabled") and guard.get("ready"):
    print(json.dumps(status, indent=2))
else:
    raise SystemExit(1)
