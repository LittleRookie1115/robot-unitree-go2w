import unittest
from types import SimpleNamespace
from unittest import mock

import voice_tunnel_info as info


class TunnelInfoTest(unittest.TestCase):
    def setUp(self):
        self.atlas = mock.Mock()
        self.atlas.Query.return_value.providers = [
            SimpleNamespace(id=info.BRIDGE, state=info.atlas_pb2.STATE_ACTIVE, capabilities=[
                SimpleNamespace(contract_id=info.BRIDGE_INFO,
                                transport=info.atlas_pb2.TRANSPORT_GRPC)]),
            SimpleNamespace(id=info.SPEECH, state=info.atlas_pb2.STATE_ACTIVE, capabilities=[
                SimpleNamespace(contract_id="robonix/service/speech/" + name,
                                transport=info.atlas_pb2.TRANSPORT_GRPC)
                for name in ("asr_stream", "tts")]),
        ]
        self.atlas.ConnectCapability.side_effect = [
            SimpleNamespace(endpoint="http://127.0.0.1:41234", channel_id="audio"),
            SimpleNamespace(endpoint="127.0.0.1:42345", channel_id="asr"),
            SimpleNamespace(endpoint="http://localhost:42345", channel_id="tts"),
        ]
        self.bridge = mock.Mock(return_value=SimpleNamespace(
            reverse=True, endpoint="ws://127.0.0.1:60002/client"))

    def test_discovers_dynamic_grpc_and_reverse_ports_without_action_calls(self):
        result = info.discover(self.atlas, "127.0.0.1:50051", self.bridge)
        self.assertEqual(result["ports"], [41234, 42345, 50051, 60002])
        self.assertEqual(result["audio_reverse_port"], 60002)
        self.assertEqual(self.atlas.DisconnectCapability.call_count, 3)
        self.bridge.assert_called_once_with("http://127.0.0.1:41234")
        self.assertEqual({call[0][0].contract_id for call in
                          self.atlas.ConnectCapability.call_args_list},
                         {info.BRIDGE_INFO, "robonix/service/speech/asr_stream",
                          "robonix/service/speech/tts"})

    def test_disconnects_binding_if_endpoint_is_not_loopback(self):
        self.atlas.ConnectCapability.side_effect = [
            SimpleNamespace(endpoint="192.168.1.1:41234", channel_id="bad")]
        with self.assertRaisesRegex(ValueError, "loopback"):
            info.discover(self.atlas, "127.0.0.1:50051", self.bridge)
        self.atlas.DisconnectCapability.assert_called_once()
        self.bridge.assert_not_called()

    def test_missing_speech_is_reported_before_opening_channels(self):
        self.atlas.Query.return_value.providers = []
        with self.assertRaisesRegex(RuntimeError, "providers first"):
            info.discover(self.atlas, "127.0.0.1:50051", self.bridge)
        self.atlas.ConnectCapability.assert_not_called()

    def test_rejects_unexpected_or_portless_endpoints(self):
        for endpoint in ("http://example.com:1234", "http://user@localhost:1234",
                         "stdio://localhost:1234", "http://localhost"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                info.endpoint_port(endpoint)


if __name__ == "__main__":
    unittest.main()
