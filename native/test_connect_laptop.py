"""Offline launcher tests: no real SSH, Client, network, microphone or robot."""
import contextlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import unittest
from unittest import mock

import connect_laptop as launcher


INFO = {"atlas_port": 50051, "ports": [50051, 53000, 53001, 60002, 18092],
        "audio_bridge_provider": "audio_client_bridge", "speech_provider": "speech"}


def arguments(*extra):
    return launcher.parser().parse_args(["unitree@robot.local", *extra])


def plan():
    return launcher.make_plan(arguments(), launcher.decode_discovery(INFO),
                              "/fake/ssh", "/fake/robonix-client")


class LaptopLauncherTest(unittest.TestCase):
    def test_custom_audio_ports_are_local_and_passed_to_client(self):
        result = launcher.make_plan(
            arguments("--client-port", "7861", "--audio-port", "16000"),
            launcher.decode_discovery(INFO), "ssh", "robonix-client")
        self.assertEqual(result.audio_ports, (16000, 16001))
        self.assertEqual(result.client[result.client.index("--audio-server-port") + 1], "16000")
        self.assertTrue({7861, 16000, 16001}.issubset(result.local_ports))
        self.assertFalse(set(launcher.AUDIO_PORTS).intersection(result.local_ports))
        self.assertFalse(any(port in (16000, 16001) for port, _remote in result.forwards))
        self.assertIn((60002, 60002), result.forwards)

    def test_audio_ports_cannot_overlap_forwarded_ports_or_overflow(self):
        for port in ("65535", "52999", "53000", "60001", "18091"):
            with self.subTest(port=port), self.assertRaises(launcher.LaunchError):
                launcher.make_plan(arguments("--audio-port", port),
                                   launcher.decode_discovery(INFO), "ssh", "robonix-client")

    def test_custom_audio_ports_are_rechecked_before_starting_client(self):
        result = launcher.make_plan(
            arguments("--client-port", "7861", "--audio-port", "16000"),
            launcher.decode_discovery(INFO), "ssh", "robonix-client")
        tunnel = mock.Mock()
        tunnel.poll.return_value = None
        client = mock.Mock()
        client.poll.return_value = 0
        with mock.patch.object(launcher, "check_free_ports") as free, \
                mock.patch.object(launcher, "wait_listeners"), \
                mock.patch.object(launcher.subprocess, "Popen", side_effect=[tunnel, client]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.run_session(result), 0)
        self.assertEqual(free.call_args_list, [
            mock.call(result.local_ports), mock.call((7861, 16000, 16001))])
        tunnel.terminate.assert_called_once_with()

    def test_all_discovered_ports_forward_loopback_and_only_atlas_is_remapped(self):
        discovery = launcher.decode_discovery(dict(INFO, ports=INFO["ports"] + [53000]))
        result = launcher.make_plan(arguments(), discovery, "ssh", "robonix-client")
        self.assertEqual(result.forwards, [
            (18092, 18092), (15051, 50051), (53000, 53000), (53001, 53001), (60002, 60002)])
        self.assertEqual(result.ssh.count("-N"), 1)
        self.assertIn("ExitOnForwardFailure=yes", result.ssh)
        self.assertIn("StrictHostKeyChecking=ask", result.ssh)
        for local, remote in result.forwards:
            self.assertIn(f"127.0.0.1:{local}:127.0.0.1:{remote}", result.ssh)
        self.assertEqual(result.ssh[-2:], ["--", "unitree@robot.local"])
        self.assertIn(7860, result.local_ports)
        self.assertTrue(set(launcher.AUDIO_PORTS).issubset(result.local_ports))
        optional = launcher.decode_discovery(dict(INFO, ports=[50051, 60002]))
        self.assertNotIn(18092, optional.ports)
        self.assertEqual(optional.audio_reverse_port, 60002)
        discovered = launcher.decode_discovery(dict(
            INFO, audio_reverse_port=61002, ports=[50051, 53000, 61002]))
        self.assertEqual(discovered.audio_reverse_port, 61002)
        alternate = launcher.make_plan(arguments(), discovered, "ssh", "robonix-client")
        self.assertIn((61002, 61002), alternate.forwards)
        self.assertNotIn((60002, 60002), alternate.forwards)

    def test_invalid_discovery_and_conflicting_local_ports_are_rejected(self):
        for update in ({"atlas_port": True}, {"ports": [True, 60002]},
                       {"ports": [50051]}, {"ports": [50051, 60002, 65536]},
                       {"audio_bridge_provider": "--bad"}, {"speech_provider": ""},
                       {"audio_reverse_port": True}, {"audio_reverse_port": 0},
                       {"audio_reverse_port": 61002}):
            with self.subTest(update=update):
                with self.assertRaises(launcher.LaunchError):
                    launcher.decode_discovery(dict(INFO, **update))
        for extra in (("--atlas-port", "60002"), ("--client-port", "15051"),
                      ("--client-port", "60000")):
            with self.subTest(extra=extra):
                with self.assertRaises(launcher.LaunchError):
                    launcher.make_plan(arguments(*extra), launcher.decode_discovery(INFO),
                                       "ssh", "robonix-client")

    def test_target_port_validation_and_remote_shell_quoting_with_one_query(self):
        for target in ("-oProxyCommand=evil", "unitree@host;evil", "host\nother", "a b"):
            args = arguments()
            args.target = target
            with self.subTest(target=target), self.assertRaises(launcher.LaunchError):
                launcher.validate_args(args)
        for value in ("0", "65536", "abc"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                arguments("--ssh-port", value)
        args = arguments("--ssh-port", "2222", "--remote-root", "/tmp/a b;$(echo bad)")
        launcher.validate_args(args)
        completed = subprocess.CompletedProcess([], 0, json.dumps(INFO))
        with mock.patch.object(launcher.subprocess, "run", return_value=completed) as run:
            self.assertEqual(launcher.query_robot(args, "/fake/ssh").atlas_port, 50051)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/fake/ssh")
        self.assertIn("2222", command)
        self.assertEqual(shlex.split(command[-1]), [
            launcher.REMOTE_PYTHON, "/tmp/a b;$(echo bad)/native/voice_tunnel_info.py"])
        self.assertEqual(command[-3:-1], ["--", args.target])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_cli_and_private_settings_override_stale_routes_without_reading_user_file(self):
        result = plan()
        argv = result.client
        for flag, value in (
            ("--robot-host", "127.0.0.1"), ("--atlas-port", "15051"),
            ("--mic-node-id", "audio_client_bridge"),
            ("--speaker-node-id", "audio_client_bridge"), ("--tts-node-id", "speech"),
            ("--audio-server-bind-host", "127.0.0.1"),
            ("--audio-server-ui-host", "127.0.0.1"),
        ):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        base = {"ROBONIX_CLIENT_SETTINGS": "/do-not-read/existing.yaml",
                "ROBONIX_LIAISON_ENDPOINT": "other-host:1234",
                "ROBONIX_CLIENT_REVERSE_AUDIO": "0", "UNRELATED": "keep"}
        original = dict(base)
        environment = launcher.client_environment(base, "/temporary/settings.yaml")
        self.assertEqual(base, original)
        self.assertEqual(environment["ROBONIX_CLIENT_SETTINGS"], "/temporary/settings.yaml")
        self.assertEqual(environment["ROBONIX_LIAISON_ENDPOINT"], "")
        self.assertEqual(environment["ROBONIX_CLIENT_REVERSE_AUDIO"], "1")
        self.assertEqual(environment["UNRELATED"], "keep")
        self.assertEqual(result.settings["robotHost"], "127.0.0.1")
        self.assertEqual(result.settings["atlasPort"], 15051)
        self.assertEqual(result.settings["liaisonEndpoint"], "")
        self.assertEqual(result.settings["micDeviceId"], "")
        self.assertEqual(result.settings["speakerDeviceId"], "")

    def test_bind_collision_closes_probe_and_never_starts_or_kills_processes(self):
        sock = mock.Mock()
        sock.bind.side_effect = OSError("address in use")
        with mock.patch.object(launcher.socket, "socket", return_value=sock), \
                mock.patch.object(launcher.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(launcher.LaunchError, "60000"):
                launcher.check_free_ports([60000])
        sock.bind.assert_called_once_with(("127.0.0.1", 60000))
        sock.close.assert_called_once_with()
        popen.assert_not_called()

    def test_tunnel_failure_stops_owned_client_before_deleting_its_profile(self):
        tunnel, client = mock.Mock(), mock.Mock()
        tunnel.poll.return_value = 1
        client.poll.return_value = None
        observed = {}

        def start(command, **options):
            self.assertIs(options["shell"], False)
            if command == result.ssh:
                return tunnel
            self.assertEqual(command, result.client)
            profile = Path(options["env"]["ROBONIX_CLIENT_SETTINGS"])
            observed["profile"] = profile
            self.assertEqual(json.loads(profile.read_text()), result.settings)
            return client

        result = plan()
        with mock.patch.object(launcher, "check_free_ports") as free, \
                mock.patch.object(launcher, "wait_listeners") as ready, \
                mock.patch.object(launcher.subprocess, "Popen", side_effect=start), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(launcher.LaunchError, "SSH"):
                launcher.run_session(result)
        self.assertEqual(free.call_args_list, [
            mock.call(result.local_ports), mock.call((7860, 60000, 60001))])
        self.assertEqual(ready.call_args_list[0].args[0], tunnel)
        self.assertEqual(ready.call_args_list[1].args[0], client)
        client.send_signal.assert_called_once()
        client.wait.assert_called_once_with(timeout=8)
        self.assertFalse(observed["profile"].exists())
        tunnel.terminate.assert_not_called()  # it had already exited

    def test_ctrl_c_during_tunnel_startup_cleans_only_owned_ssh(self):
        tunnel = mock.Mock()
        tunnel.poll.return_value = None
        with mock.patch.object(launcher, "check_free_ports"), \
                mock.patch.object(launcher, "wait_listeners", side_effect=KeyboardInterrupt), \
                mock.patch.object(launcher.subprocess, "Popen", return_value=tunnel) as popen:
            with self.assertRaises(KeyboardInterrupt):
                launcher.run_session(plan())
        popen.assert_called_once()
        tunnel.terminate.assert_called_once_with()
        tunnel.wait.assert_called_once_with(timeout=8)

    def test_print_only_queries_once_and_never_starts_children(self):
        with mock.patch.object(launcher.shutil, "which", side_effect=["/fake/ssh", None]), \
                mock.patch.object(launcher, "query_robot",
                                  return_value=launcher.decode_discovery(INFO)) as query, \
                mock.patch.object(launcher.subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(launcher.main(["unitree@robot.local", "--print-only"]), 0)
        query.assert_called_once()
        popen.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertIn([15051, 50051], result["forwards"])
        self.assertNotIn("env", result)

    def test_owned_cleanup_escalates_after_graceful_timeout(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("owned-client", 8), 0]
        launcher.stop_owned(process, graceful=True)
        process.send_signal.assert_called_once()
        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
