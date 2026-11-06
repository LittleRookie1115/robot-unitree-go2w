#!/usr/bin/env python3
"""Bounded live audit of this deployment's ROS endpoints; no control requests."""
import json
from pathlib import Path
import time

import rclpy

rclpy.init()
node = rclpy.create_node("go2w_native_endpoint_audit", enable_rosout=False,
                        start_parameter_services=False)
deadline = time.monotonic() + 3
while time.monotonic() < deadline:
    rclpy.spin_once(node, timeout_sec=0.1)
report = {}
for name, namespace in node.get_node_names_and_namespaces():
    assert name != "go2w_native_motion_bridge", "Default native deployment opened a motion ROS node"
    if name not in ("go2w_native_sensor_observer", "go2_camera_bridge"):
        continue
    publishers = node.get_publisher_names_and_types_by_node(name, namespace)
    subscriptions = node.get_subscriber_names_and_types_by_node(name, namespace)
    services = node.get_service_names_and_types_by_node(name, namespace)
    report[name] = {
        "publishers": [topic for topic, _ in publishers],
        "subscriptions": [topic for topic, _ in subscriptions],
        "services": [topic for topic, _ in services],
    }
    endpoints = report[name]["publishers"] + report[name]["subscriptions"] + report[name]["services"]
    assert not any("cmd_vel" in topic or "lowcmd" in topic or "/api/sport" in topic
                   or "arm_motion" in topic for topic in endpoints), endpoints
node.destroy_node()
rclpy.shutdown()
assert set(report) == {"go2w_native_sensor_observer", "go2_camera_bridge"}, report
destination = Path(__file__).resolve().parent.parent / "rbnx-build/native/ros-endpoints.json"
destination.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
