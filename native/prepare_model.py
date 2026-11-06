#!/usr/bin/env python3
"""Prepare the pinned official Go2-W wheel model and native Soma description."""
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
import yaml

root = Path(__file__).resolve().parent.parent
source = root / "rbnx-build/native/vendor-model/go2w"
if not (source / "source.json").is_file():
    raise SystemExit("Run python3 native/fetch_go2w_model.py first.")
output = root / "rbnx-build/native/model-go2w"
output.mkdir(parents=True, exist_ok=True)
shutil.copytree(source / "dae", output / "dae", dirs_exist_ok=True)
shutil.copy2(source / "LICENSE.unitree", output / "LICENSE.unitree")
shutil.copy2(source / "source.json", output / "source.json")
urdf = (source / "urdf/go2w_description.urdf").read_text()
urdf = urdf.replace("package://go2w_description/", "")
document = ET.fromstring(urdf)
wheel_joints = [joint for joint in document.findall("joint")
                if joint.get("type") == "continuous"]
if len(wheel_joints) != 4:
    raise SystemExit("The Go2-W model must retain its four continuous wheel joints.")
for mesh in document.findall(".//mesh"):
    if not (output / mesh.get("filename")).is_file():
        raise SystemExit("Missing official Go2-W model asset: " + mesh.get("filename"))
(output / "go2w_description.urdf").write_text(urdf)
body = yaml.safe_load((root / "soma.yaml").read_text())
body["urdf"].update(path=str(output / "go2w_description.urdf"),
                    root_link="base", model_name="unitree_go2w")
body["robot"].update(id="unitree_go2w_01", display_name="Unitree Go2-W",
                     family="wheeled_legged_robot")
# The reference Go2 footprint, mass and body dimensions are not Go2-W
# measurements. Do not publish them as hardware/navigation constraints.
for field in ("dimensions", "footprint", "mass_kg", "passable_door_width_m"):
    body["robot"].pop(field, None)
body["robot"]["exports"] = []


def clear_exports(component):
    component["exports"] = []
    for child in component.get("components", []):
        clear_exports(child)


clear_exports(body["robot"])
base = body["robot"]["components"][0]
base["urdf_link"] = "base"
parts = base["components"][0]["components"]
# Browser microphone/speaker availability is a Client property; this deployment
# has no hardware audio provider or health stream for a robot audio component.
parts[:] = [part for part in parts if part["id"] != "client_audio"]
parts.append({"id": "battery", "type": "battery", "exports": []})
base["exports"] = [{"provider_id": "go2_native", "capabilities": [
    {"path": "robonix/primitive/health/stream", "description": "Live battery and sensor freshness."},
]}]
motion_package = yaml.safe_load(
    (root / "packages/go2_chassis_motion/package_manifest.yaml").read_text())
base["exports"].append({
    "provider_id": "go2_chassis_motion",
    "capabilities": [{"path": capability["name"]}
                     for capability in motion_package["capabilities"]],
})
for part in parts:
    if part.get("urdf_link") not in {link.get("name") for link in document.findall("link")}:
        part.pop("urdf_link", None)
    contract = {
        "onboard_lidar": "robonix/primitive/lidar/lidar3d",
        "onboard_imu": "robonix/primitive/imu/imu",
        "front_camera": "robonix/primitive/camera/rgb",
        "battery": "robonix/primitive/health/stream",
    }.get(part["id"])
    if contract:
        part["exports"] = [{"provider_id": "go2_native", "capabilities": [
            {"path": contract, "description": "Native Foxy measured sensor input."},
        ]}]
for joint in wheel_joints:
    parts.append({"id": joint.find("child").get("link"), "type": "wheel",
                  "urdf_link": joint.find("child").get("link"),
                  "urdf_joint": joint.get("name"), "exports": []})
body["description"] = {
    "summary": "Go2-W native Ubuntu 20.04 / Foxy telemetry with an official static wheel model.",
    "can_do": ["Read current camera, MID-360 cloud, IMU and battery state.",
               "Provide bounded chassis primitives after explicit human enable."],
    "cannot_do": ["Build maps or navigate autonomously; use low-level joint or wheel commands."],
    "notes": [
        "Camera intrinsics and body-to-sensor transforms remain unverified.",
        "Odometry is monitored only: /dog_odom currently has an invalid zero quaternion.",
        "Headerless battery/Sport state use receipt freshness, not invented source timestamps.",
        "Original UniLiDAR clock offset is not corrected or hidden.",
        "The official Go2-W model is nominal static geometry, not measured extrinsics or live joints.",
        "Wheel/leg motion, lateral traction and posture completion still require physical acceptance.",
    ],
}
(output.parent / "soma.yaml").write_text(yaml.safe_dump(body, sort_keys=False))
print("Native Go2-W model prepared with four continuous wheel joints:", output)
