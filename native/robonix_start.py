#!/usr/bin/env python3
# Start, stop, or restart the native Robonix stack.
#
# Invoked as robonix_start, robonix_stop, or robonix_restart (symlinks).
# It never enables/arms the chassis and never sends posture or velocity.
# `--status` only reads the current state.

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
CHASSIS = ROOT / "native/chassis.sh"
ROUTER_UNIT = "robonix-vlm-router.service"
NATIVE_UNIT = "robonix-go2-native.service"
CLIENT_UNIT = "robonix-client.service"
LEGACY_UNIT = "robonix-go2w-motion.service"
START_UNITS = (ROUTER_UNIT, NATIVE_UNIT, CLIENT_UNIT)
STOP_UNITS = (CLIENT_UNIT, LEGACY_UNIT, NATIVE_UNIT, ROUTER_UNIT)
NAME = Path(sys.argv[0]).name


def command_name():
    if NAME.endswith("_stop"):
        return "stop"
    if NAME.endswith("_restart"):
        return "restart"
    return "start"


def environment():
    env = dict(os.environ)
    env.setdefault("HOME", "/home/unitree")
    env.setdefault("PATH", "/usr/bin:/bin:/usr/local/bin")
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    return env


def run(command, timeout=40, cwd=None):
    try:
        proc = subprocess.run(
            command, cwd=cwd, env=environment(),
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


def systemctl(action, unit, timeout=75):
    return run(["/usr/bin/systemctl", "--user", action, unit], timeout=timeout)


def is_active(unit):
    code, stdout, _, _ = run([
        "/usr/bin/systemctl", "--user", "is-active", unit,
    ], timeout=8)
    return code == 0 and stdout == "active"


def wait_active(unit, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_active(unit):
            return True
        time.sleep(0.5)
    return False


def wait_inactive(unit, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_active(unit):
            return True
        time.sleep(0.5)
    return False


def read_status(timeout=30):
    deadline = time.monotonic() + timeout
    status = {}
    while True:
        code, stdout, stderr, status = run(
            ["/bin/bash", str(CHASSIS), "status"], timeout=12, cwd=str(ROOT))
        if status and status.get("status") != "error":
            return status
        if time.monotonic() >= deadline:
            return {}
        time.sleep(0.5)


def http_code(url):
    code, stdout, _, _ = run([
        "/usr/bin/curl", "-sS", "-m", "4", "-o", "/dev/null",
        "-w", "%{http_code}", url,
    ], timeout=7)
    return stdout or "unavailable"


def http_json(url):
    code, stdout, _, _ = run([
        "/usr/bin/curl", "-sS", "-m", "4", url,
    ], timeout=7)
    try:
        return json.loads(stdout) if stdout else {}
    except ValueError:
        return {}


def print_status(status):
    guard = status.get("guard") or {}
    limits = status.get("limits") or {}
    print("chassis: motion_enabled=%s faulted=%s session_enabled=%s guard_reason=%s" % (
        status.get("motion_enabled"),
        status.get("faulted"),
        status.get("session_enabled"),
        guard.get("reason"),
    ))
    print("limits: forward=%s reverse=%s lateral=%s yaw=%s max_duration=%s" % (
        limits.get("forward_mps"),
        limits.get("reverse_mps"),
        limits.get("lateral_mps"),
        limits.get("yaw_rps"),
        limits.get("max_duration_s"),
    ))


def motion_guard(action):
    status = read_status(timeout=10)
    if not status:
        return True
    if status.get("active") is True:
        print("a motion request is active; run `bash native/chassis.sh stop` "
              "or disable it before %s" % action, file=sys.stderr)
        return False
    if status.get("motion_enabled") is True:
        print("note: motion session is enabled; %s will disarm it" % action)
    return True


def start_units():
    for unit in START_UNITS:
        if is_active(unit):
            print("%s: already active" % unit)
            continue
        print("starting %s" % unit)
        code, stdout, stderr, _ = systemctl("start", unit)
        if code != 0:
            print("start failed: %s" % (stderr or code), file=sys.stderr)
            return False
        if not wait_active(unit, 30):
            print("%s did not become active within 30 s" % unit,
                  file=sys.stderr)
            return False
    return True


def stop_units():
    ok = True
    for unit in STOP_UNITS:
        if not is_active(unit):
            print("%s: already inactive" % unit)
            continue
        print("stopping %s" % unit)
        code, stdout, stderr, _ = systemctl("stop", unit)
        if code != 0:
            print("stop failed: %s" % (stderr or code), file=sys.stderr)
            ok = False
            continue
        if not wait_inactive(unit, 20):
            print("%s did not become inactive within 20 s" % unit,
                  file=sys.stderr)
            ok = False
    return ok


def report(action):
    for unit in (ROUTER_UNIT, NATIVE_UNIT, CLIENT_UNIT, LEGACY_UNIT):
        print("%s: %s" % (unit, "active" if is_active(unit) else "inactive"))
    if action == "stop":
        print("Robonix stopped.")
        return
    status = {}
    if is_active(NATIVE_UNIT):
        status = read_status(timeout=30)
        if status:
            print_status(status)
        else:
            print("chassis status unavailable; check journalctl --user -u %s"
                  % NATIVE_UNIT, file=sys.stderr)
    else:
        print("chassis status unavailable (native inactive)", file=sys.stderr)
    router = http_json("http://127.0.0.1:16667/health")
    if router.get("ok"):
        print("vlm_router: mode=%s external=%s local=%s" % (
            router.get("mode"),
            (router.get("external") or {}).get("reachable"),
            (router.get("local") or {}).get("reachable"),
        ))
    else:
        print("vlm_router: unavailable")
    print("vlm_external: %s" % http_code("http://127.0.0.1:16668/health"))
    print("vlm_local: %s" % http_code("http://127.0.0.1:11435/v1/models"))
    print("client_http: %s" % http_code("http://127.0.0.1:7860/"))
    print("PC client: http://127.0.0.1:7860/  "
          "(telemetry: http://127.0.0.1:18092/)")
    motion_enabled = status.get("motion_enabled") is True
    if action == "restart":
        if motion_enabled:
            print("Robonix restarted. Chassis session is enabled (unchanged); "
                  "no enable/arm was performed.")
        else:
            print("Robonix restarted. Chassis remains motion_disabled; run "
                  "`motion_enable` when ready.")
    elif action == "status":
        print("status only; no service changes performed.")
    else:
        if motion_enabled:
            print("Robonix started. Chassis session is already enabled; "
                  "no enable/arm was performed.")
        else:
            print("Robonix started. Chassis remains motion_disabled; run "
                  "`motion_enable` when ready.")


def main():
    action = command_name()
    parser = argparse.ArgumentParser(
        description="%s the native Robonix stack." % action.capitalize())
    parser.add_argument("--status", action="store_true",
                        help="only report state; do not start/stop/restart")
    args = parser.parse_args()

    if args.status:
        report("status")
        return 0 if (
            is_active(ROUTER_UNIT)
            and is_active(NATIVE_UNIT)
            and is_active(CLIENT_UNIT)
        ) else 3

    if action in ("stop", "restart"):
        if not motion_guard(action):
            return 3

    if action == "stop":
        ok = stop_units()
        report("stop")
        return 0 if ok else 3

    if action == "restart":
        if not stop_units():
            report("restart")
            return 3
        time.sleep(1)
        if not start_units():
            report("restart")
            return 3
        report("restart")
        return 0

    if not start_units():
        report("start")
        return 3
    report("start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
