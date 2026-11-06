"""Download official sherpa-onnx release assets without touching host proxy settings."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import time
import urllib.request

from .settings import BUILD, MODEL_ROOT

MODELS = {
    "asr": {
        "tag": "asr-models",
        "asset": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",
        "directory": "sensevoice",
        "required": ["model.int8.onnx", "tokens.txt"],
    },
    "tts": {
        "tag": "tts-models",
        "asset": "sherpa-onnx-vits-zh-ll.tar.bz2",
        "directory": "vits-zh-ll",
        "required": ["model.onnx", "tokens.txt", "lexicon.txt"],
    },
    "kws": {
        "tag": "kws-models",
        "asset": "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2",
        "directory": "kws",
        "required": [
            "tokens.txt", "en.phone",
            "encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
            "decoder-epoch-13-avg-2-chunk-8-left-64.onnx",
            "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
        ],
    },
}


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def extract_model(archive, destination, kind):
    """Strip the bundle root; reject links/traversal and omit unused ASR float32 weights."""
    destination.mkdir(parents=True, exist_ok=True)
    needed = set(MODELS[kind]["required"])
    if kind == "asr":
        needed.add("LICENSE")
    extracted = set()
    with tarfile.open(archive, "r:bz2") as bundle:
        for member in bundle:
            source = PurePosixPath(member.name)
            if source.is_absolute() or ".." in source.parts or not source.parts:
                raise ValueError("Unsafe model archive path")
            relative = Path(*source.parts[1:])
            if not relative.parts:
                continue
            if member.issym() or member.islnk():
                raise ValueError("Model archive links are not accepted")
            if not member.isfile():
                continue
            if kind == "asr" and relative.name.endswith(".onnx") and relative.name != "model.int8.onnx":
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source_stream, target.open("wb") as output:
                shutil.copyfileobj(source_stream, output, 1024 * 1024)
            extracted.add(relative.as_posix())
            # A .tar.bz2 iterator decompresses skipped data too. Once the ASR
            # runtime and original notice are present, do not scan its huge
            # unused float32 tail.
            if kind == "asr" and needed <= extracted:
                break


def download(kind):
    model = MODELS[kind]
    destination = MODEL_ROOT / model["directory"]
    provenance = destination / "provenance.json"
    if provenance.is_file() and all((destination / name).is_file() for name in model["required"]):
        previous = json.loads(provenance.read_text())
        for name in model["required"]:
            if sha256(destination / name) != previous["files_sha256"].get(name):
                raise RuntimeError(f"Model file changed since download: {destination / name}")
        print(f"{kind}: verified existing official model", flush=True)
        return previous
    endpoint = "https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/tags/" + model["tag"]
    request = urllib.request.Request(endpoint, headers={"User-Agent": "go2-native-speech-build"})
    with urllib.request.urlopen(request, timeout=20) as response:
        release = json.load(response)
    asset = next((item for item in release["assets"] if item["name"] == model["asset"]), None)
    if asset is None:
        raise RuntimeError(f"Official asset not found: {model['asset']}")
    cache = BUILD / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / model["asset"]
    if not archive.is_file() or archive.stat().st_size != asset["size"]:
        partial = archive.with_name(archive.name + ".part")
        # The API path avoids an unreachable github.com DNS/CONNECT path. The
        # server may redirect to its official release asset CDN.
        url = asset["url"] + f"?download=1&native_speech={time.time_ns()}"
        print(f"{kind}: downloading official asset {asset['id']} ({asset['size']} bytes)", flush=True)
        subprocess.run([
            "curl", "-4", "-L", "--fail", "--show-error", "--silent",
            "--connect-timeout", "10", "--max-time", "1800",
            "--speed-limit", "1024", "--speed-time", "30",
            "--retry", "2", "--retry-delay", "2",
            "-H", "Accept: application/octet-stream",
            "-H", "User-Agent: go2-native-speech-build",
            "-o", str(partial), url,
        ], check=True)
        if partial.stat().st_size != asset["size"]:
            raise RuntimeError(f"Official asset length mismatch: {partial}")
        partial.replace(archive)
    digest = sha256(archive)
    published = asset.get("digest")
    if published and published != "sha256:" + digest:
        raise RuntimeError(f"Official asset checksum mismatch: {archive}")
    print(f"{kind}: extracting verified-length official bundle", flush=True)
    extract_model(archive, destination, kind)
    missing = [name for name in model["required"] if not (destination / name).is_file()]
    if missing:
        raise RuntimeError(f"Required files absent from model bundle: {missing}")
    result = {
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "repository": "k2-fsa/sherpa-onnx", "release": model["tag"],
        "asset_name": asset["name"], "asset_id": asset["id"],
        "source_api": asset["url"], "size_bytes": asset["size"],
        "archive_sha256": digest, "published_digest": published,
        "files_sha256": {name: sha256(destination / name) for name in model["required"]},
    }
    provenance.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=tuple(MODELS))
    parser.add_argument("--no-kws", action="store_true")
    args = parser.parse_args(argv)
    selected = [args.only] if args.only else ["asr", "tts"] + ([] if args.no_kws else ["kws"])
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = dict(zip(selected, workers.map(download, selected)))
    path = BUILD / "model-downloads.json"
    path.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({"models_ready": list(results), "report": str(path)}), flush=True)


if __name__ == "__main__":
    main()
