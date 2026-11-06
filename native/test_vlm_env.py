import subprocess
import tempfile
import unittest
from pathlib import Path


LOADER = Path(__file__).with_name("vlm-env.sh")


class VlmEnvironmentTest(unittest.TestCase):
    def run_loader(self, content, *, trace=False):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "model-config"
            config.write_text(content)
            script = '''
source "$1"
load_robonix_vlm_env "$2"
[[ "$-" != *a* ]]
python3 - <<'PY'
import os
assert os.environ["ROBONIX_VLM_UPSTREAM"] == os.environ["VLM_BASE_URL"]
assert os.environ["ROBONIX_VLM_API_KEY"] == os.environ["VLM_API_KEY"]
assert os.environ["ROBONIX_VLM_MODEL"] == os.environ["VLM_MODEL"]
PY
'''
            return subprocess.run(
                ["bash", "-eux" if trace else "-eu", "-c", script,
                 "vlm-env-test", str(LOADER), str(config)],
                capture_output=True, text=True, timeout=5,
            )

    def test_exports_aliases_without_exposing_credentials_to_shell_trace(self):
        sentinel = "fixture-secret-with-spaces only"
        result = self.run_loader(
            "export VLM_BASE_URL='https://example.invalid/v1'\n"
            f"VLM_API_KEY='{sentinel}'\n"
            "export VLM_MODEL='fixture-model'\n", trace=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(sentinel, result.stdout + result.stderr)

    def test_incomplete_configuration_fails_with_variable_name_only(self):
        result = self.run_loader("VLM_BASE_URL='https://example.invalid/v1'\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VLM_API_KEY", result.stderr)
        self.assertNotIn("https://example.invalid/v1", result.stderr)
