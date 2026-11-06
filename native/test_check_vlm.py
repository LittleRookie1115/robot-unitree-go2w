import contextlib
import io
import json
import socket
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from unittest import mock

import check_vlm


CONFIG = {
    "VLM_BASE_URL": "http://localhost:16667/v1",
    "VLM_API_KEY": "fixture-private-key-do-not-print",
    "VLM_MODEL": "fixture-model",
}


class ModelCheckTest(unittest.TestCase):
    def opener(self, body):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = body
        opener = mock.Mock()
        opener.open.return_value = response
        return opener

    def test_loaded_configuration_and_authenticated_models_check(self):
        opener = self.opener(b'{"data":[{"id":"fixture-model"}]}')
        result = check_vlm.probe(CONFIG, opener=opener)
        self.assertTrue(result["configuration_loaded"])
        self.assertTrue(result["models_check_passed"])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:16667/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + CONFIG["VLM_API_KEY"])
        self.assertNotIn(CONFIG["VLM_API_KEY"], json.dumps(result))

    def test_refused_connection_is_not_reported_as_missing_environment(self):
        opener = mock.Mock()
        opener.open.side_effect = URLError(ConnectionRefusedError(111, "refused"))
        result = check_vlm.probe(CONFIG, opener=opener)
        self.assertTrue(result["configuration_loaded"])
        self.assertFalse(result["api_reachable"])
        self.assertEqual(result["reason"], "connection_refused")
        self.assertEqual(result["missing_variables"], [])

    def test_http_errors_and_bad_metadata_do_not_leak_response_details(self):
        for status, reason in ((401, "authentication_failed"), (403, "authentication_failed"),
                               (404, "models_endpoint_unavailable")):
            with self.subTest(status=status):
                opener = mock.Mock()
                opener.open.side_effect = HTTPError(CONFIG["VLM_BASE_URL"], status,
                                                    CONFIG["VLM_API_KEY"], {}, None)
                result = check_vlm.probe(CONFIG, opener=opener)
                self.assertTrue(result["api_reachable"])
                self.assertEqual(result["reason"], reason)
                self.assertNotIn(CONFIG["VLM_API_KEY"], json.dumps(result))
        for body in (b"<html>login</html>", b'{"data":[]}', b'{"error":"private"}'):
            result = check_vlm.probe(CONFIG, opener=self.opener(body))
            self.assertFalse(result["models_check_passed"])

    def test_missing_configuration_never_sends_a_request(self):
        opener = mock.Mock()
        result = check_vlm.probe({}, opener=opener)
        self.assertFalse(result["configuration_loaded"])
        self.assertIn("VLM_API_KEY", result["missing_variables"])
        opener.open.assert_not_called()

    def test_connection_timeout_and_aliases_are_classified_without_secrets(self):
        aliases = {"ROBONIX_VLM_UPSTREAM": CONFIG["VLM_BASE_URL"],
                   "ROBONIX_VLM_API_KEY": CONFIG["VLM_API_KEY"],
                   "ROBONIX_VLM_MODEL": CONFIG["VLM_MODEL"]}
        opener = mock.Mock()
        opener.open.side_effect = URLError(socket.timeout("private debug detail"))
        result = check_vlm.probe(aliases, opener=opener)
        self.assertTrue(result["configuration_loaded"])
        self.assertEqual(result["reason"], "connection_timeout")
        self.assertNotIn("private debug detail", json.dumps(result))

    def test_cli_json_reports_configuration_without_credentials(self):
        output = io.StringIO()
        with mock.patch.dict(check_vlm.os.environ, CONFIG, clear=True), \
                mock.patch.object(check_vlm, "build_opener",
                                  return_value=self.opener(b'{"data":[{"id":"fixture-model"}]}')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(check_vlm.main(["--json"]), 0)
        self.assertTrue(json.loads(output.getvalue())["models_check_passed"])
        self.assertNotIn(CONFIG["VLM_API_KEY"], output.getvalue())

    def test_shell_wrapper_rejects_incomplete_file_without_secret_output(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".config/robonix/env"
            config.parent.mkdir(parents=True)
            config.write_text("VLM_BASE_URL='http://localhost:1/v1'\n"
                              "VLM_API_KEY='fixture-private-key-do-not-print'\n")
            result = subprocess.run(
                ["bash", str(Path(__file__).with_name("check-vlm.sh")), "--json"],
                env=dict(os.environ, HOME=directory),
                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("VLM_MODEL", result.stderr)
        self.assertNotIn(CONFIG["VLM_API_KEY"], result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
