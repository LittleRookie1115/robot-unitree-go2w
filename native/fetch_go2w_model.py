#!/usr/bin/env python3
"""Fetch only the official Go2-W model at an immutable revision, with license."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import base64
import hashlib
import json
from pathlib import Path
import urllib.request

REVISION = "5994d4faef0a9cadd3287f8de0199a67eeb2a259"
REPOSITORY = "unitreerobotics/unitree_ros"
ROOT = Path(__file__).resolve().parent.parent / "rbnx-build/native/vendor-model/go2w"


def read_json(path):
    url = f"https://api.github.com/repos/{REPOSITORY}/contents/{path}?ref={REVISION}"
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    files = []
    for directory in ("dae", "urdf"):
        rows = read_json("robots/go2w_description/" + directory)
        for row in rows:
            if row["type"] != "file" or Path(row["name"]).name != row["name"]:
                raise RuntimeError("Unexpected model tree entry")
            files.append((directory + "/" + row["name"], row))
    files.append(("LICENSE.unitree", read_json("LICENSE")))

    def fetch(entry):
        relative, metadata = entry
        destination = ROOT / relative
        expected = metadata["sha"]
        def digest(payload):
            return hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()
        if not destination.is_file() or digest(destination.read_bytes()) != expected:
            # The raw-content host is unavailable on this connection. The
            # official Git blob API addresses the exact same immutable object.
            url = f"https://api.github.com/repos/{REPOSITORY}/git/blobs/{expected}"
            with urllib.request.urlopen(url, timeout=15) as response:
                blob = json.load(response)
            if blob.get("sha") != expected or blob.get("encoding") != "base64":
                raise RuntimeError("Unexpected official Git blob response")
            payload = base64.b64decode(blob["content"])
            if digest(payload) != expected:
                raise RuntimeError("Git blob mismatch: " + relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        return {"path": relative, "git_blob": expected, "bytes": destination.stat().st_size}

    downloaded = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(fetch, entry) for entry in files]
        try:
            for future in as_completed(pending):
                record = future.result()
                downloaded.append(record)
                print("Verified " + record["path"], flush=True)
        except Exception:
            for future in pending:
                future.cancel()
            raise
    downloaded.sort(key=lambda row: row["path"])
    (ROOT / "source.json").write_text(json.dumps({
        "repository": REPOSITORY, "revision": REVISION, "license": "BSD-3-Clause",
        "robot_variant": "go2w", "files": downloaded,
        "model_geometry_physically_measured": False,
    }, indent=2) + "\n")
    print(json.dumps({"robot_variant": "go2w", "revision": REVISION, "files": len(downloaded),
                      "bytes": sum(row["bytes"] for row in downloaded), "path": str(ROOT)}))


if __name__ == "__main__":
    main()
