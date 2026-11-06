#!/usr/bin/env python3
"""Live native acceptance: metadata only, no control calls or payload files."""
import asyncio
import base64
from datetime import datetime
import io
import json
from pathlib import Path
import urllib.error
import urllib.request

from PIL import Image
import websockets

ROOT = Path(__file__).resolve().parent.parent
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read(url):
    with opener.open(url, timeout=8) as response:
        return json.load(response)


async def client_health():
    async with websockets.connect("ws://127.0.0.1:7860/ws/vitals", proxy=None) as ws:
        await ws.send(json.dumps({"settings": {"robotHost": "127.0.0.1", "atlasPort": 50051}}))
        async with asyncio.timeout(15):
            while True:
                event = json.loads(await ws.recv())
                if event.get("type") == "hardware" and event.get("data", {}).get("power"):
                    return event["data"]


state = read("http://127.0.0.1:18092/status")
assert state["motion_enabled"] is False
for key in ("lidar", "imu", "camera", "lowstate"):
    assert state["sources"][key]["healthy"], (key, state["sources"][key])
assert state["sources"]["odom"]["freshness"] == "invalid_orientation"
system = read("http://127.0.0.1:7860/api/system")
providers = {entry["id"]: entry["state"] for entry in system["providers"]}
assert providers.get("go2_native") == "ACTIVE", providers
assert all(value == "ACTIVE" for value in providers.values()), providers
native = next(entry for entry in system["providers"] if entry["id"] == "go2_native")
capabilities = [cap["contractId"] for cap in native["capabilities"]]
assert "robonix/primitive/chassis/odom" not in capabilities
assert not any("twist" in name or "/move" in name for name in capabilities)
image_response = None
for attempt in range(3):
    image_response = read("http://127.0.0.1:7860/api/perception/camera")
    if image_response.get("ok"):
        break
assert image_response and image_response.get("ok"), image_response
payload = image_response["image"]
image = Image.open(io.BytesIO(base64.b64decode(payload["data"], validate=True)))
image.verify()
hardware = asyncio.run(client_health())
assert 0 <= hardware["power"]["socPercent"] <= 100, hardware["power"]
assert hardware["power"]["voltage"] > 0, hardware["power"]
report = {
    "checked_at": datetime.now().astimezone().isoformat(),
    "deployment": "native Ubuntu 20.04 / Foxy / ARM64; no container",
    "providers": providers,
    "registered_capabilities": capabilities,
    "camera": {"width": payload["width"], "height": payload["height"],
               "jpeg_decoded": True, "client_transport": "Atlas + MCP"},
    "battery": hardware["power"],
    "sources": state["sources"],
    "motion_enabled": False,
    "raw_payloads_saved": False,
    "passed": True,
}
(ROOT / "rbnx-build/native/acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
