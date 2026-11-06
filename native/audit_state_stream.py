#!/usr/bin/env python3
"""Finite subscription-only audit of the three native state checks; no command publisher."""
import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages/go2w_chassis_motion"))
from go2w_chassis_motion.state_guard import NativeWitness

import rclpy
from diagnostic_msgs.msg import DiagnosticArray
from rclpy.qos import qos_profile_sensor_data
from unitree_go.msg import LowState, SportModeState


def summarize(times):
    gaps = sorted(right-left for left, right in zip(times, times[1:]))
    def percentile(fraction):
        return gaps[min(len(gaps)-1, int((len(gaps)-1)*fraction))] if gaps else None
    return {"samples": len(times),
            "rate_hz": (len(times)-1)/(times[-1]-times[0]) if len(times)>1 else None,
            "gap_p50_sec": percentile(.5), "gap_p99_sec": percentile(.99),
            "max_gap_sec": max(gaps) if gaps else None,
            "gaps_over_200ms": sum(gap>.20 for gap in gaps)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=20)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or not 2 <= args.seconds <= 120:
        parser.error("--seconds must be in [2, 120]")
    rclpy.init(args=[])
    node = rclpy.create_node("go2_state_subscription_audit", enable_rosout=False,
                            start_parameter_services=False)
    witness = NativeWitness({1, 3, 5, 6})
    started = time.monotonic()
    stamps = {name: [] for name in ("sport", "lowstate", "adapter_diagnostics", "dds_progress")}
    payload_errors, blockers = Counter(), Counter()
    modes, errors, writers = Counter(), Counter(), set()
    unhealthy_samples = []
    diag_validity = Counter()

    def sport(message, metadata=None):
        now = time.monotonic()
        stamps["sport"].append(now)
        q = [float(value) for value in message.imu_state.quaternion]
        values = q + list(message.position) + list(message.velocity) + [float(message.yaw_speed)]
        witness.observe(message.stamp.sec+message.stamp.nanosec*1e-9, int(message.mode),
                        message.error_code, q, values, time.time(), now)
        modes[int(message.mode)] += 1
        errors[int(message.error_code)] += 1
        for rejection in witness.last_health_rejections:
            payload_errors[rejection] += 1
        gid = (metadata.get("publisher_gid") if isinstance(metadata, dict)
               else getattr(metadata, "publisher_gid", None))
        if gid is not None:
            writers.add(bytes(gid))

    def low(message):
        now = time.monotonic()
        stamps["lowstate"].append(now)
        witness.observe_tick(int(message.tick), now)

    def diagnostic(message):
        for status in message.status:
            if status.name != "go2_chassis_adapter":
                continue
            now = time.monotonic()
            stamps["adapter_diagnostics"].append(now)
            values = {item.key: item.value for item in status.values}
            source = int(values.get("sport_dds_source_ns", "0"))
            previous = witness._dds
            witness.observe_dds(source, now)
            if source > previous:
                stamps["dds_progress"].append(now)
            diag_validity[values.get("state_valid", "missing")] += 1

    subscriptions = [
        node.create_subscription(DiagnosticArray, "/diagnostics", diagnostic, 10),
        node.create_subscription(SportModeState, "/lf/sportmodestate", sport,
                                 qos_profile_sensor_data),
        node.create_subscription(LowState, "/lowstate", low, qos_profile_sensor_data),
    ]
    checked = 0
    try:
        while time.monotonic()-started < args.seconds:
            rclpy.spin_once(node, timeout_sec=.01)
            now = time.monotonic()
            if now-started < 1.5:
                continue
            checked += 1
            detail = witness.details(now)
            for blocker in detail["blockers"]:
                blockers[blocker] += 1
            if not detail["ready"] and len(unhealthy_samples)<12:
                unhealthy_samples.append({"elapsed_sec": now-started, **detail})
        report = {
            "checked_at": datetime.now().astimezone().isoformat(),
            "duration_sec": time.monotonic()-started, "subscription_only": True,
            "command_publishers_created": 0, "robot_requests": [],
            "streams": {name: summarize(values) for name, values in stamps.items()},
            "sport_payload_rejections": dict(payload_errors),
            "sport_modes": dict(modes), "sport_error_codes": dict(errors),
            "sport_writer_count": len(writers) if writers else None,
            "sport_publisher_count": node.count_publishers("/lf/sportmodestate"),
            "adapter_state_valid": dict(diag_validity),
            "post_qualification_checks": checked, "blocker_counts": dict(blockers),
            "unhealthy_samples": unhealthy_samples, "final_witness": witness.details(time.monotonic()),
        }
        print(json.dumps(report, indent=2, allow_nan=False))
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
