#!/usr/bin/env python3
# Human-only quick motion authorization for the native Go2-W profile.
#
# Default: ensure robonix-go2-native is running, call chassis.sh enable
# (zero preparation + internal arm), then report the resulting guard state.
# --check: only read the current state; never starts or enables anything.
#
# This command never sends a posture or nonzero velocity. It is a terminal
# command, not an MCP/LLM capability; the model still cannot enable/arm.

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
CHASSIS = ROOT / "native/chassis.sh"
SERVICE = "robonix-go2-native.service"


def environment():
    env = dict(os.environ)
    env.setdefault("HOME", "/home/unitree")
    env.setdefault("PATH", "/usr/bin:/bin:/usr/local/bin")
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    return env


def run(command, timeout=40):
    try:
        proc = subprocess.run(
            command, cwd=str(ROOT), env=environment(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
        stdout = proc.stdout.decode("utf-8", "replace").strip()
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        try:
            parsed = json.loads(stdout) if stdout else {}
        except ValueError:
            parsed = {}
        return proc.returncode, stdout, stderr, parsed
    except (OSError, subprocess.TimeoutExpired) as error:
        return 124, "", type(error).__name__, {}


def service_active():
    code, stdout, _, _ = run([
        "/usr/bin/systemctl", "--user", "is-active", SERVICE,
    ], timeout=8)
    return code == 0 and stdout == "active"


def start_service():
    code, _, stderr, _ = run([
        "/usr/bin/systemctl", "--user", "start", SERVICE,
    ], timeout=75)
    if code != 0:
        print("service start failed: %s" % (stderr or code), file=sys.stderr)
        return False
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if service_active():
            return True
        time.sleep(0.5)
    print("service did not become active within 25 s", file=sys.stderr)
    return False


def status_summary(status):
    guard = status.get("guard") or {}
    return {
        "motion_enabled": status.get("motion_enabled"),
        "active": status.get("active"),
        "faulted": status.get("faulted"),
        "session_enabled": status.get("session_enabled"),
        "guard_state": guard.get("guard_state"),
        "guard_ready": guard.get("ready"),
        "guard_reason": guard.get("reason"),
        "daemon_armed": guard.get("daemon_armed"),
        "movement_ready": guard.get("movement_ready"),
        "mode": guard.get("mode"),
        "limits": status.get("limits"),
    }


def print_summary(status):
    print(json.dumps(status_summary(status), indent=2, sort_keys=True))


def enabled_state(status):
    guard = status.get("guard") or {}
    return (status.get("session_enabled") is True
            and status.get("faulted") is not True
            and guard.get("ready") is True
            and guard.get("guard_state") == "ARMED"
            and guard.get("daemon_armed") is True)


def main():
    parser = argparse.ArgumentParser(
        description="Quick human motion enable for the native Go2-W chassis.")
    parser.add_argument("--check", action="store_true",
                        help="only read state; do not start or enable anything")
    args = parser.parse_args()

    active = service_active()
    print("service: %s" % ("active" if active else "inactive"))
    if not active and args.check:
        print("service is inactive; `motion_enable` would start it first")
        return 0

    started = False
    if not active:
        print("starting %s" % SERVICE)
        if not start_service():
            return 3
        started = True
        time.sleep(2)

    deadline = time.monotonic() + (15.0 if started else 0.0)
    while True:
        code, stdout, stderr, status = run(["/bin/bash", str(CHASSIS), "status"],
                                           timeout=12)
        if status or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if not status or status.get("status") == "error":
        detail = status.get("error") or stderr or code
        print("chassis status unavailable: %s" % detail, file=sys.stderr)
        return 3
    print("current:")
    print_summary(status)

    if args.check:
        print("check only: run `motion_enable` to authorize (zero-prep + arm)")
        return 0

    if enabled_state(status):
        print("already enabled; no arm request sent")
        return 0
    if status.get("active") is True:
        print("a motion request is active; stop/disable it first", file=sys.stderr)
        return 3

    print("human authorization: enabling chassis (zero-prep + internal arm; "
          "no posture, no nonzero velocity)")
    code, stdout, stderr, result = run(["/bin/bash", str(CHASSIS), "enable"],
                                       timeout=40)
    if result:
        print("enable result:")
        print(json.dumps(result, indent=2, sort_keys=True))
    elif stderr:
        print("enable failed: %s" % stderr, file=sys.stderr)

    deadline = time.monotonic() + 6.0
    after = {}
    while True:
        code, stdout, stderr, after = run(["/bin/bash", str(CHASSIS), "status"],
                                          timeout=12)
        if enabled_state(after) or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if after:
        print("after:")
        print_summary(after)
        if enabled_state(after):
            print("motion enabled: session_enabled=true, guard=ARMED")
            return 0
        if after.get("status") == "error":
            print("post-enable status unavailable: %s" % after.get("error"),
                  file=sys.stderr)
            return 3
        guard = after.get("guard") or {}
        reason = guard.get("reason") or after.get("last_result", {}).get("error")
        print("motion not enabled: %s" % (reason or "unknown reason"),
              file=sys.stderr)
        print("if reason is sport_state_invalid_or_stale, run: "
              "bash native/check-motion.sh --hardware-only", file=sys.stderr)
        return 3
    print("post-enable status unavailable", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
