#!/usr/bin/env python3
"""Subscription-only motion audit. Does not enable, arm, or publish commands."""
from datetime import datetime
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages/go2_chassis_motion"))
from go2_chassis_motion.transport import check_live_prerequisites, check_native_network


def audit_samples(records, basis="payload"):
    samples = [record for record in records if record.get("topic") == "/lf/sportmodestate"]
    blockers = []
    if not samples:
        return {"samples": 0}, ["No SportModeState samples received"]
    stamps = [record["payload_sec"] + record["payload_nsec"] * 1e-9 for record in samples]
    advances = sum(right > left for left, right in zip(stamps, stamps[1:]))
    if basis == "payload":
        if any(stamp <= 0 for stamp in stamps):
            blockers.append("SportModeState payload timestamps are zero; strict source freshness fails")
        elif any(not -0.05 <= record["wall_ns"] / 1e9 - stamp <= 0.20
                 for record, stamp in zip(samples, stamps)):
            blockers.append("SportModeState payload timestamps are stale or in the future")
        if advances == 0:
            blockers.append("SportModeState payload timestamps do not advance")
        if any(right < left for left, right in zip(stamps, stamps[1:])):
            blockers.append("SportModeState payload timestamps regress")
    elif basis == "dds-source-lowstate-tick-v1":
        summary = next((record for record in records if "native_transport_fresh" in record), {})
        if summary.get("native_transport_fresh") is not True or summary.get("native_clock_fault"):
            blockers.append("Native DDS/tick progress qualification failed: " +
                            str(summary.get("native_clock_fault", "no native evidence")))
    else:
        blockers.append("Unknown state time basis")
    if any(record["error_code"] != 0 for record in samples):
        blockers.append("SportModeState reports a nonzero error code")
    if any(not math.isfinite(record["quaternion_norm_squared"]) or
           abs(record["quaternion_norm_squared"] - 1) > 0.1 for record in samples):
        blockers.append("SportModeState orientation is invalid")
    if any(not all(math.isfinite(value) for value in record["velocity"] +
                   [record["yaw_speed"]]) for record in samples):
        blockers.append("SportModeState velocity is not finite")
    if any(any(abs(value) > 0.02 for value in record["velocity"]) or
           abs(record["yaw_speed"]) > 0.05 for record in samples):
        blockers.append("Robot is not stationary")
    dds_offsets = [(record["wall_ns"] - record["dds_source_ns"]) / 1e9
                   for record in samples if record.get("dds_source_ns", 0) > 0]
    return {
        "samples": len(samples),
        "modes": sorted({record["mode"] for record in samples}),
        "error_codes": sorted({record["error_code"] for record in samples}),
        "payload_source_stamps": stamps,
        "payload_source_advances": advances,
        "dds_local_minus_source_seconds": dds_offsets,
        "dds_writer_clock_aligned_with_host": (
            bool(dds_offsets) and all(-0.05 <= offset <= 0.20 for offset in dds_offsets)),
        "source_basis": basis,
        "native_clock_evidence": next(
            (record for record in records if "native_transport_fresh" in record), None),
        "absolute_measurement_age_verified": False,
    }, blockers


def command_json(arguments):
    result = subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=5)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware-only", action="store_true")
    args = parser.parse_args()
    probe = ROOT / "rbnx-build/native/chassis-ros/install/lib/go2_native_chassis_adapter/go2_native_state_probe"
    if not probe.is_file():
        probe = ROOT / "rbnx-build/native/chassis-ros/build/go2_native_chassis_adapter/go2_native_state_probe"
    if not probe.is_file():
        raise SystemExit("Build the read-only state probe with bash native/build-motion.sh first")
    result = subprocess.run(
        ["bash", str(ROOT / "native/ros-exec.sh"), str(probe)],
        check=True, capture_output=True, text=True, timeout=12)
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    manifest = yaml.safe_load((ROOT / "native/robonix_manifest.yaml").read_text())
    config = dict(next(item["config"] for item in manifest["primitive"]
                       if item["name"] == "go2_chassis_motion"))
    basis = config.get("state_time_basis", "payload")
    sport, blockers = audit_samples(records, basis)
    interface = config.get("network_interface", "eth10")
    robot_address = config.get("robot_address", "192.168.123.161")
    addresses = command_json(["ip", "-j", "-4", "address", "show", "dev", interface])
    routes = command_json(["ip", "-j", "route", "show", "default", "dev", interface])
    neighbors = command_json(["ip", "-j", "neigh", "show", "dev", interface])
    try:
        native_route = {"status": "passed", **check_native_network(interface, robot_address)}
    except Exception as error:
        native_route = {"status": "blocked", "reason": str(error)}
    modes = config.get("allowed_modes", sport.get("modes", []))
    if not set(sport.get("modes", [])) <= set(modes):
        blockers.append("Observed mode is outside the configured Go2-W manual mode set")
    config.update(allow_motion=True, operator_present=True, safety_ack="I_UNDERSTAND_GO2_CAN_MOVE")
    # These values are passed only to a read-only validation function. They
    # are never exported into a process or used to construct a transport.
    gate_inputs = dict(os.environ, GO2_ALLOW_MOTION="true", GO2_OPERATOR_PRESENT="true",
                       GO2_SAFETY_ACK="I_UNDERSTAND_GO2_CAN_MOVE",
                       GO2_STATE_TIME_BASIS=basis, GO2_ROBOT_VARIANT="go2w",
                       GO2_ALLOWED_MODES=",".join(str(mode) for mode in modes))
    try:
        check_live_prerequisites(config, gate_inputs)
    except Exception as error:
        blockers.insert(0, str(error))
    identity_probe = ROOT / "rbnx-build/native/chassis-sdk/install/bin/go2_native_identity_probe"
    if not identity_probe.is_file():
        identity_probe = ROOT / "rbnx-build/native/chassis-sdk/build/go2_native_identity_probe"
    identity = {}
    if identity_probe.is_file():
        try:
            identity_env = dict(os.environ)
            identity_env.pop("LD_LIBRARY_PATH", None)
            query = subprocess.run([str(identity_probe), interface], env=identity_env,
                                   capture_output=True, text=True, timeout=5)
            identity = json.loads(query.stdout)
            if query.returncode or identity.get("code") != 0 or identity.get("name") != "ai-w":
                blockers.append("Read-only motion-switcher query did not confirm Go2-W ai-w")
        except Exception as error:
            blockers.append("Go2-W identity query failed: " + str(error))
    else:
        blockers.append("Build the read-only Go2-W identity query first")
    sys.path.insert(0, str(ROOT / "native"))
    import chassis
    deployment = {"not_required": "hardware-only audit"} if args.hardware_only else {}
    if not args.hardware_only:
        try:
            deployment = chassis.motion_status("127.0.0.1:50051")
        except Exception as error:
            deployment = {"status_error": str(error)}
            blockers.append("Robonix motion status RPC is unavailable")
    report = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "read_only": True,
        "gate_status": "blocked" if blockers else "prerequisites_passed_still_disarmed",
        "blockers": blockers,
        "deployment": deployment,
        "sport": sport,
        "robot_variant": "go2w",
        "motion_switcher_identity": identity,
        "network": {"native_robot_route": native_route, "addresses": addresses,
                    "default_routes": routes, "neighbors": neighbors},
        "actions_dispatched": [],
        "note": "Passing this audit does not arm the robot or constitute physical acceptance.",
    }
    output = ROOT / ("rbnx-build/native/motion-preflight-hardware.json" if args.hardware_only
                     else "rbnx-build/native/motion-preflight.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report, indent=2, allow_nan=False))
    return 2 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
