from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class Go2WDeploymentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = yaml.safe_load((ROOT / "robonix_manifest.yaml").read_text(encoding="utf-8"))
        cls.lock = {}
        for line in (ROOT / "native" / "deps.lock").read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            cls.lock[key] = value.strip()

    def test_catalog_identity_and_disabled_chassis(self):
        self.assertEqual(self.manifest["name"], "go2w-foxy-native")
        self.assertEqual(self.manifest["catalog"]["name"], "robonix.robot.unitree.go2w")
        chassis = next(item for item in self.manifest["primitive"] if item["name"] == "go2w_chassis_motion")
        self.assertIs(chassis["config"]["allow_motion"], False)
        self.assertEqual(chassis["config"]["robot_variant"], "go2w")
        self.assertEqual(chassis["path"], "${ROBONIX_DEPLOY_DIR}/packages/go2w_chassis_motion")

    def test_audio_bridge_is_a_pinned_repository_dependency(self):
        audio = next(item for item in self.manifest["primitive"] if item["name"] == "audio_client_bridge")
        self.assertNotIn("path", audio)
        self.assertEqual(audio["url"], self.lock["AUDIO_CLIENT_BRIDGE_URL"])
        self.assertEqual(audio["branch"], self.lock["AUDIO_CLIENT_BRIDGE_COMMIT"])
        self.assertNotIn("..", audio["url"])

    def test_visual_manifest_matches_the_published_motion_contract(self):
        visual = next(item for item in self.manifest["service"] if item["name"] == "go2w_visual_action")
        self.assertIs(visual["config"]["motion_enabled"], True)
        self.assertEqual(visual["config"]["forward_speed_mps"], 0.25)
        self.assertEqual(visual["config"]["forward_duration_s"], 1.0)

    def test_native_manifest_is_the_root_manifest(self):
        link = ROOT / "native" / "robonix_manifest.yaml"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (ROOT / "robonix_manifest.yaml").resolve())

    def test_model_pin_matches_the_fetcher(self):
        source = (ROOT / "native" / "fetch_go2w_model.py").read_text(encoding="utf-8")
        self.assertIn(f'REVISION = "{self.lock["GO2W_MODEL_REVISION"]}"', source)

    def test_generated_egg_info_is_not_tracked(self):
        tracked = subprocess.check_output(
            ["git", "ls-files", "*.egg-info", "*.egg-info/*"],
            cwd=ROOT, text=True,
        )
        self.assertEqual(tracked, "")
