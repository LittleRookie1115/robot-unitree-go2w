"""Skip inherited workstation checks when the root manifest is the Go2-W launch file."""
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def skip_when_native_launch() -> None:
    manifest = yaml.safe_load((ROOT / "robonix_manifest.yaml").read_text(encoding="utf-8"))
    if manifest.get("name") == "go2w-foxy-native":
        raise unittest.SkipTest(
            "The root manifest is the Go2-W native deployment; "
            "this test checks the retired workstation launch profile."
        )
