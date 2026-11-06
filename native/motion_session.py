#!/usr/bin/env python3
"""Go2-W transport supervisor. Only the human enable RPC opens motion permission."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import yaml

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "rbnx-build/native"
sys.path.insert(0, str(ROOT / "packages/go2w_chassis_motion"))
from go2w_chassis_motion.controller import load_limits


def live_manifest():
    manifest = yaml.safe_load((ROOT / "native/robonix_manifest.yaml").read_text())
    primitive = next(item for item in manifest["primitive"] if item["name"] == "go2w_chassis_motion")
    config = primitive["config"]
    if config.get("robot_variant") != "go2w":
        raise ValueError("the native session requires its configured Go2-W variant")
    config.update(allow_motion=True, operator_present=True,
                  safety_ack="I_UNDERSTAND_GO2_CAN_MOVE")
    return manifest, config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-present", action="store_true", required=True)
    parser.parse_args()
    # Only read-only hardware queries run before any motion-capable process.
    subprocess.run([os.environ["GO2_SDK_PYTHON"], str(ROOT / "native/check_motion.py"),
                    "--hardware-only"], check=True)
    manifest, config = live_manifest()
    limits = load_limits()
    max_vx = max(limits.forward_mps, limits.reverse_mps)
    live = BUILD / "live"
    live.mkdir(parents=True, exist_ok=True)
    manifest_path = live / "robonix_manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    environment = dict(os.environ, GO2_ALLOW_MOTION="true", GO2_OPERATOR_PRESENT="true",
                       GO2_SAFETY_ACK="I_UNDERSTAND_GO2_CAN_MOVE",
                       GO2_ALLOWED_MODES=",".join(str(mode) for mode in config["allowed_modes"]),
                       GO2_STATE_TIME_BASIS=config["state_time_basis"], GO2_ROBOT_VARIANT="go2w")
    socket = Path(f"/run/user/{os.getuid()}/go2w-native/sport.sock")
    socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stopping = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    children = []
    logs = []
    log_offsets = {}

    def start(name, command, child_env):
        log = (live / (name + ".log")).open("a")
        log_offsets[name] = log.tell()
        logs.append(log)
        process = subprocess.Popen(command, cwd=live, env=child_env,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        children.append((name, process))
        return process

    try:
        sdk_env = dict(environment, LD_LIBRARY_PATH=str(BUILD / "chassis-sdk/install/lib"))
        sdk = start("sdk", [
            str(BUILD / "chassis-sdk/install/bin/go2_sport_daemon"),
            "--socket", str(socket), "--allow-motion", "--interface", config["network_interface"],
            "--motion-ack", "GO2_PHYSICAL_MOTION_APPROVED",
            "--motion-profile", "native-go2w-manual-v2", "--watchdog-ms", "300",
            "--max-vx", str(max_vx),
            "--max-vy", str(limits.lateral_mps),
            "--max-wz", str(limits.yaw_rps),
            "--max-motion-ms", "5000"], sdk_env)
        end = time.monotonic()+5
        while not socket.is_socket():
            if sdk.poll() is not None or time.monotonic() >= end or stopping.is_set():
                raise RuntimeError("native SDK listener did not start")
            stopping.wait(0.05)
        gates = [f"{key}={environment[key]}" for key in (
            "GO2_ALLOW_MOTION", "GO2_OPERATOR_PRESENT", "GO2_SAFETY_ACK",
            "GO2_ALLOWED_MODES", "GO2_STATE_TIME_BASIS", "GO2_ROBOT_VARIANT")]
        start("adapter", [
            "bash", str(ROOT / "native/ros-exec.sh"), "env", *gates,
            str(BUILD / "chassis-ros/install/lib/go2w_native_chassis_adapter/go2w_native_chassis_adapter_node"),
            "--ros-args", "-p", "allow_motion:=true",
            "-p", "allowed_modes:=" + json.dumps(config["allowed_modes"]),
            "-p", "sdk_socket:=" + str(socket),
            "-p", "state_time_basis:=" + config["state_time_basis"],
            "-p", "max_vx:=" + str(max_vx),
            "-p", "max_vy:=" + str(limits.lateral_mps),
            "-p", "max_wz:=" + str(limits.yaw_rps)],
            environment)
        start("robonix", ["rbnx", "boot", "--no-update-check", "-f", str(manifest_path)],
              environment)
        (live / "session.json").write_text(json.dumps({
            "robot_variant": "go2w", "profile": "native-go2w-manual-v2",
            "armed_at_start": False, "postures_at_start": [],
            "pids": {name: process.pid for name, process in children},
            "log_start_bytes": log_offsets,
        }, indent=2) + "\n")
        print("Go2-W transport started DISARMED. Waiting for human enable; no posture was sent.",
              flush=True)
        while not stopping.wait(0.2):
            for name, process in children:
                if process.poll() is not None:
                    raise RuntimeError(f"{name} exited with {process.returncode}; stopping session")
    finally:
        # Provider/worker requests stop first, then adapter, then SDK owner.
        for name, process in reversed(children):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=8 if name == "robonix" else 3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
