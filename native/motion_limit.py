#!/usr/bin/env python3
# Set runtime motion limits for the native Go2-W manual profile.
#
# Commands:
#   forward_limit 1.00
#   reverse_limit 0.25
#   lateral_limit 0.10
#   yaw_limit 0.50
#   duration_limit 2.0
#   motion_limits
#   motion_limits_reset
#
# Limits are stored in ~/.config/robonix/motion_limits.json and are applied on
# the next human enable. If an idle motion backend exists, it is disabled first
# so the next enable rebuilds it with the new values. Active motion is refused.

from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages/go2_chassis_motion"))

from go2_chassis_motion.controller import (  # noqa: E402
    DEFAULT_LIMITS,
    HARD_LIMITS,
    LIMITS_PATH,
    Limits,
    MotionError,
    load_limits,
)

COMMANDS = {
    "forward_limit": "forward_mps",
    "reverse_limit": "reverse_mps",
    "lateral_limit": "lateral_mps",
    "yaw_limit": "yaw_rps",
    "duration_limit": "max_duration_s",
}


def print_limits(limits):
    print(json.dumps(limits, indent=2, sort_keys=True))


def current():
    try:
        return load_limits()
    except MotionError as error:
        print("invalid motion limits: %s" % error, file=sys.stderr)
        raise SystemExit(2)


def write_limits(values):
    try:
        limits = Limits(**values)
    except (MotionError, TypeError) as error:
        print("invalid motion limits: %s" % error, file=sys.stderr)
        raise SystemExit(2)
    target = Path(LIMITS_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    temporary = target.with_name(target.name + ".tmp")
    payload = {
        "forward_mps": limits.forward_mps,
        "reverse_mps": limits.reverse_mps,
        "lateral_mps": limits.lateral_mps,
        "yaw_rps": limits.yaw_rps,
        "default_duration_s": limits.default_duration_s,
        "max_duration_s": limits.max_duration_s,
        "period_s": limits.period_s,
        "posture_duration_s": limits.posture_duration_s,
    }
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    return limits


def chassis_status():
    environment = dict(os.environ)
    environment.setdefault("HOME", "/home/unitree")
    try:
        proc = subprocess.run(
            ["/bin/bash", str(ROOT / "native/chassis.sh"), "status"],
            cwd=str(ROOT), env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10, check=False,
        )
        return json.loads(proc.stdout.decode("utf-8", "replace").strip() or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def prepare_backend():
    status = chassis_status()
    if status is None:
        print("note: chassis status unavailable; the new limits apply on next enable")
        return
    if status.get("active") is True:
        print("refusing: a motion request is active; stop/disable it first", file=sys.stderr)
        raise SystemExit(3)
    if status.get("motion_enabled") is True:
        environment = dict(os.environ)
        environment.setdefault("HOME", "/home/unitree")
        print("disabling the idle motion backend so the next enable uses the new limits")
        proc = subprocess.run(
            ["/bin/bash", str(ROOT / "native/chassis.sh"), "disable"],
            cwd=str(ROOT), env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, check=False,
        )
        if proc.returncode != 0:
            print("disable failed: %s" % proc.stderr.decode("utf-8", "replace")[:500],
                  file=sys.stderr)
            raise SystemExit(3)


def value_for(key, text):
    try:
        value = float(text)
    except ValueError:
        print("%s must be a number" % key, file=sys.stderr)
        raise SystemExit(2)
    if not math.isfinite(value) or value <= 0:
        print("%s must be finite and positive" % key, file=sys.stderr)
        raise SystemExit(2)
    ceiling = HARD_LIMITS[key]
    if value > ceiling:
        print("%s must not exceed hard ceiling %s" % (key, ceiling), file=sys.stderr)
        raise SystemExit(2)
    return value


def main():
    name = Path(sys.argv[0]).name
    arguments = sys.argv[1:]
    if name in ("motion_limit.py", "motion_limit"):
        if not arguments:
            print("usage: motion_limit {forward|reverse|lateral|yaw|duration}_limit VALUE",
                  file=sys.stderr)
            return 2
        name = arguments.pop(0)

    if name == "motion_limits":
        print_limits(asdict(current()))
        return 0
    if name == "motion_limits_reset":
        prepare_backend()
        limits = write_limits(DEFAULT_LIMITS)
        print("reset to defaults")
        print_limits(asdict(limits))
        print("run: bash native/chassis.sh enable")
        return 0
    key = COMMANDS.get(name)
    if key is None:
        print("unknown command: %s" % name, file=sys.stderr)
        return 2
    if len(arguments) != 1:
        print("%s requires exactly one value" % name, file=sys.stderr)
        return 2

    old = asdict(current())
    values = dict(old)
    values[key] = value_for(key, arguments[0])
    if key == "max_duration_s" and values["default_duration_s"] > values[key]:
        values["default_duration_s"] = values[key]
    limits = write_limits(values)
    prepare_backend()
    print("%s: %s -> %s" % (key, old[key], getattr(limits, key)))
    print("saved: %s" % LIMITS_PATH)
    print_limits(asdict(limits))
    print("run: bash native/chassis.sh enable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
