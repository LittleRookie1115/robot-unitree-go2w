"""Offline tests for the loopback VLM router.

The fake upstream speaks the same OpenAI-compatible shape as the PC proxy and
Ollama. No robot, ROS graph, network interface or model is touched.
"""

import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import vlm_router


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _send_json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self):
        if self.path.endswith("/health"):
            self._send_json(200, {"ok": True})
        elif self.path.endswith("/models"):
            self._send_json(200, {"object": "list", "data": []})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append((self.path, body, dict(self.headers)))
        if self.server.sse:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            chunks = [
                b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n',
                b"data: [DONE]\n\n",
            ]
            for chunk in chunks:
                self.wfile.write(("%x\r\n" % len(chunk)).encode("ascii") + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.close_connection = True
        else:
            self._send_json(200, {"id": "x", "choices": [{"message": {"content": "OK"}}]})


class FakeUpstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, FakeUpstreamHandler)
        self.requests = []
        self.sse = False


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.upstream = FakeUpstream(("127.0.0.1", 0))
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.up_port = self.upstream.server_address[1]

        base = "http://127.0.0.1:%d/v1" % self.up_port
        health = "http://127.0.0.1:%d/health" % self.up_port
        config = json.loads(json.dumps(vlm_router.DEFAULT_CONFIG))
        config.update(
            {
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "state_file": str(Path(self.temp.name) / "state.json"),
                "log_requests": False,
                "external": {
                    "base_url": base,
                    "health_url": health,
                    "model": "gpt-6.1-sol",
                },
                "local": {
                    "base_url": base,
                    "health_url": health,
                    "model": "local-model",
                },
            }
        )
        self.router = vlm_router.Router(config)
        self.server = vlm_router.RouterServer(("127.0.0.1", 0), self.router)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        body = response.read()
        headers_out = dict(response.getheaders())
        connection.close()
        return response.status, headers_out, body

    def last_upstream_body(self):
        return json.loads(self.upstream.requests[-1][1].decode("utf-8"))

    def test_mode_defaults_to_external_and_persists_local(self):
        status, _, body = self.request("GET", "/admin/mode")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["mode"], "external")

        status, _, body = self.request("POST", "/admin/mode", {"mode": "local"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["mode"], "local")

        status, _, body = self.request("GET", "/admin/mode")
        self.assertEqual(json.loads(body)["mode"], "local")
        self.assertTrue(Path(self.router.state_file).exists())

    def test_models_advertises_logical_id_and_context(self):
        status, _, body = self.request("GET", "/v1/models")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"][0]["id"], "gpt-6.1-sol")
        self.assertEqual(payload["data"][0]["context_length"], 8192)

        status, _, body = self.request("GET", "/v1/models/gpt-6.1-sol")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], "gpt-6.1-sol")

    def test_local_rewrites_model_and_strips_optional_fields(self):
        self.request("POST", "/admin/mode", {"mode": "local"})
        payload = {
            "model": "gpt-6.1-sol",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "prompt_cache_key": "abc",
            "prompt_cache_options": {"mode": "implicit"},
            "stream_options": {"include_usage": True},
            "response_format": {"type": "json_schema", "json_schema": {"name": "x"}},
        }
        status, _, _ = self.request("POST", "/v1/chat/completions", payload)
        self.assertEqual(status, 200)
        sent = self.last_upstream_body()
        self.assertEqual(sent["model"], "local-model")
        self.assertNotIn("prompt_cache_key", sent)
        self.assertNotIn("prompt_cache_options", sent)
        self.assertNotIn("stream_options", sent)
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertEqual(sent["max_tokens"], 1024)
        last = self.router.status()["last_request"]
        self.assertEqual(last["route"], "local")
        self.assertEqual(last["model"], "local-model")

    def test_external_preserves_model_and_cache_fields(self):
        payload = {
            "model": "gpt-6.1-sol",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "prompt_cache_key": "abc",
        }
        status, _, _ = self.request("POST", "/v1/chat/completions", payload)
        self.assertEqual(status, 200)
        sent = self.last_upstream_body()
        self.assertEqual(sent["model"], "gpt-6.1-sol")
        self.assertEqual(sent["prompt_cache_key"], "abc")

    def test_stream_passthrough_keeps_sse_framing(self):
        self.upstream.sse = True
        payload = {
            "model": "gpt-6.1-sol",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        status, headers, body = self.request("POST", "/v1/chat/completions", payload)
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        text = body.decode("utf-8")
        self.assertIn('data: {"choices":[{"delta":{"content":"OK"}}]}', text)
        self.assertIn("data: [DONE]", text)

    def test_local_rejects_image_input(self):
        self.request("POST", "/admin/mode", {"mode": "local"})
        payload = {
            "model": "gpt-6.1-sol",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64,AA=="},
                        }
                    ],
                }
            ],
            "stream": False,
        }
        status, _, body = self.request("POST", "/v1/chat/completions", payload)
        self.assertEqual(status, 400)
        self.assertIn("text-only", body.decode("utf-8"))

    def test_upstream_unreachable_returns_502(self):
        self.router.config["external"]["base_url"] = "http://127.0.0.1:1/v1"
        payload = {
            "model": "gpt-6.1-sol",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        status, _, body = self.request("POST", "/v1/chat/completions", payload)
        self.assertEqual(status, 502)
        self.assertIn("unreachable", body.decode("utf-8"))

    def test_non_loopback_bind_is_rejected(self):
        with self.assertRaises(ValueError):
            vlm_router.validate_loopback("0.0.0.0")


if __name__ == "__main__":
    unittest.main()
