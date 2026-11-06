"""Offline CLI/RPC routing and compatibility-shell tests; never load native/env.sh."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import chassis_pb2
import grpc
import std_msgs_pb2
import std_srvs_pb2


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("native_chassis_cli", ROOT / "native/chassis.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class ChassisCliTest(unittest.TestCase):
    def setUp(self):
        self.result = {
            "status": "completed", "stop_acknowledged": True,
            "dispatch_verified": True, "physical_motion_observed": True,
        }
        self.stub = mock.Mock()
        self.connected = mock.Mock(return_value=contextlib.nullcontext(mock.sentinel.channel))
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(cli, "connected", self.connected))
        self.stack.enter_context(mock.patch.object(
            cli.grpc, "insecure_channel", side_effect=AssertionError("No network calls in CLI tests")))
        self.factories = {}
        names = set(cli.DIRECTION_STUBS.values()) | {
            entry[1] for entry in cli.TRIGGER_ACTIONS.values()
        } | {"RobonixPrimitiveChassisMoveStub", "RobonixPrimitiveChassisStopStub"}
        for name in names:
            factory = self.stack.enter_context(mock.patch.object(
                cli.contracts, name, return_value=self.stub))
            self.factories[name] = factory

    def invoke(self, argv, result=None, trigger_success=True):
        payload = self.result if result is None else result
        self.stub.ExecuteMoveCommand.return_value = chassis_pb2.ExecuteMoveCommand_Response(
            status=std_msgs_pb2.String(data=json.dumps(payload)))
        self.stub.Trigger.return_value = std_srvs_pb2.Trigger_Response(
            success=trigger_success, message=json.dumps(payload))
        self.stub.Stop.return_value = chassis_pb2.Stop_Response(
            success=trigger_success, message=json.dumps(payload))
        self.connected.reset_mock()
        for factory in self.factories.values():
            factory.reset_mock()
        output = io.StringIO()
        error_output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error_output):
            code = cli.main(argv)
        self.diagnostics = error_output.getvalue()
        return code, json.loads(output.getvalue())

    def test_each_direction_uses_its_registered_primitive_with_positive_speed(self):
        for direction, stub_name in cli.DIRECTION_STUBS.items():
            with self.subTest(direction=direction):
                code, result = self.invoke(
                    [direction, "--speed", ".03", "--duration", ".5"])
                self.assertEqual(code, 0)
                self.assertEqual(result, self.result)
                self.connected.assert_called_once_with(
                    "127.0.0.1:50051", "robonix/primitive/chassis/" + direction)
                self.factories[stub_name].assert_called_once_with(mock.sentinel.channel)
                self.factories["RobonixPrimitiveChassisMoveStub"].assert_not_called()
                request = self.stub.ExecuteMoveCommand.call_args.args[0]
                axis = "linear_x" if direction in ("forward", "backward") else "linear_y"
                self.assertEqual(request.command, chassis_pb2.MoveCommand(
                    **{axis: .03, "duration_sec": .5}))
                self.assertEqual(self.stub.ExecuteMoveCommand.call_args.kwargs["timeout"], 5)

    def test_forward_and_backward_distance_magnitudes_stay_positive(self):
        for direction in ("forward", "backward"):
            with self.subTest(direction=direction):
                self.invoke([direction, "--meters", ".04"])
                command = self.stub.ExecuteMoveCommand.call_args.args[0].command
                self.assertEqual(command, chassis_pb2.MoveCommand(forward_m=.04))

    def test_lateral_distance_compatibility_uses_positive_speed_and_seconds(self):
        for direction in ("left", "right"):
            with self.subTest(direction=direction):
                self.invoke([direction, "--meters", ".02"])
                command = self.stub.ExecuteMoveCommand.call_args.args[0].command
                self.assertEqual(command.linear_y, .05)
                self.assertAlmostEqual(command.duration_sec, .4)
                self.assertEqual(command.forward_m, 0.0)
                self.assertEqual(command.linear_x, 0.0)

    def test_direction_and_velocity_defaults_are_owned_by_controller(self):
        for action in ("forward", "backward", "left", "right", "velocity"):
            with self.subTest(action=action):
                self.invoke([action])
                self.assertEqual(self.stub.ExecuteMoveCommand.call_args.args[0].command,
                                 chassis_pb2.MoveCommand())

    def test_signed_velocity_and_yaw_preserve_units_on_move_rpc(self):
        self.invoke(["velocity", "--vx", "-.03", "--vy", ".02",
                     "--wz", "-.1", "--duration", ".5"])
        self.assertEqual(self.stub.ExecuteMoveCommand.call_args.args[0].command,
                         chassis_pb2.MoveCommand(
                             linear_x=-.03, linear_y=.02, angular_z=-.1, duration_sec=.5))
        self.connected.assert_called_once_with(
            "127.0.0.1:50051", "robonix/primitive/chassis/move")
        self.invoke(["turn", "--degrees", "-5"])
        self.assertEqual(self.stub.ExecuteMoveCommand.call_args.args[0].command,
                         chassis_pb2.MoveCommand(rotate_deg=-5))

    def test_invalid_and_nonfinite_arguments_fail_before_rpc(self):
        arguments = [
            ["forward", "--meters", "-.02"], ["backward", "--meters", "0"],
            ["left", "--speed", "-.01"], ["right", "--speed", "0"],
            ["forward", "--meters", ".02", "--speed", ".03"],
            ["backward", "--meters", ".02", "--duration", ".3"],
            ["left", "--meters", ".02", "--duration", ".3"],
            ["right", "--duration", "-1"],
            ["velocity", "--vx", "0.05--duration", "0.5"],
        ]
        for value in ("nan", "inf", "-inf"):
            arguments.extend([
                ["forward", "--meters", value], ["backward", "--speed", value],
                ["left", "--duration", value], ["velocity", "--vx", value],
                ["velocity", "--vy", value], ["velocity", "--wz", value],
                ["velocity", "--duration", value], ["turn", "--degrees", value],
            ])
        for argv in arguments:
            with self.subTest(argv=argv):
                self.connected.reset_mock()
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    cli.main(argv)
                self.assertEqual(error.exception.code, 2)
                self.connected.assert_not_called()

    def test_unobserved_failed_or_unverified_motion_returns_nonzero_and_original_json(self):
        results = [
            dict(self.result, status="not_observed", physical_motion_observed=False),
            dict(self.result, status="not_dispatched", dispatch_verified=False),
            dict(self.result, status="dispatched", physical_motion_observed=False),
            {"status": "completed", "stop_acknowledged": True},
            dict(self.result, stop_acknowledged=False),
            {"status": "rejected", "error": "motion_disabled"},
            {"status": "fault", "error": "guard failed"},
            {"status": "cancelled", "stop_acknowledged": True},
            {"status": "stop_unconfirmed", "stop_acknowledged": False},
        ]
        for result in results:
            with self.subTest(result=result):
                code, printed = self.invoke(["forward", "--meters", ".02"], result)
                self.assertNotEqual(code, 0)
                self.assertEqual(printed, result)

    def test_zero_velocity_completion_does_not_require_physical_motion(self):
        result = {"status": "completed", "completion": "zero_velocity_window",
                  "stop_acknowledged": True, "dispatch_verified": False,
                  "physical_motion_observed": False}
        code, printed = self.invoke(["velocity", "--duration", ".1"], result)
        self.assertEqual(code, 0)
        self.assertEqual(printed, result)
        self.assertEqual(self.diagnostics, "")

    def test_all_nonzero_command_forms_require_both_explicit_boolean_witnesses(self):
        arguments = [
            ["forward"], ["backward", "--meters", ".02"],
            ["left", "--speed", ".02"], ["right", "--meters", ".01"],
            ["velocity", "--vx", ".02"], ["velocity", "--vy", "-.02"],
            ["velocity", "--wz", ".1"], ["move", "--vx", "-.02"],
            ["turn", "--degrees", "5"], ["turn", "--degrees", "-5"],
        ]
        for argv in arguments:
            for field in ("dispatch_verified", "physical_motion_observed"):
                for value in (None, False, 1, "true"):
                    with self.subTest(argv=argv, field=field, value=value):
                        result = dict(self.result)
                        if value is None:
                            del result[field]
                        else:
                            result[field] = value
                        code, printed = self.invoke(argv, result)
                        self.assertNotEqual(code, 0)
                        self.assertEqual(printed, result)
                        diagnostic = json.loads(self.diagnostics)
                        self.assertIn(field, diagnostic["verification_error"])
                        if value is None:
                            self.assertIn("missing", diagnostic["verification_error"])

    def test_legacy_completed_reply_is_preserved_and_missing_evidence_is_explained(self):
        result = {"status": "completed", "stop_acknowledged": True}
        code, printed = self.invoke(["velocity", "--vx", ".05", "--duration", ".5"], result)
        self.assertEqual(code, 2)
        self.assertEqual(printed, result)
        diagnostic = json.loads(self.diagnostics)["verification_error"]
        self.assertIn("missing dispatch_verified", diagnostic)
        self.assertIn("missing physical_motion_observed", diagnostic)

    def test_dispatched_is_not_success_for_velocity_even_with_motion_evidence(self):
        result = dict(self.result, status="dispatched", action_finished=True)
        for argv in (["forward"], ["velocity", "--vx", ".02"], ["turn", "--degrees", "5"],
                     ["velocity"]):
            with self.subTest(argv=argv):
                code, printed = self.invoke(argv, result)
                self.assertEqual(code, 2)
                self.assertEqual(printed, result)
                self.assertIn("status must be 'completed'",
                              json.loads(self.diagnostics)["verification_error"])

    def test_authorization_and_posture_rpc_routes_and_timeouts(self):
        cases = [
            ("enable", "enable", 30, {"status": "enabled", "session_enabled": True}),
            ("arm", "arm", 30, {"status": "armed", "session_enabled": True}),
            ("disable", "disable", 15, {"status": "disabled", "session_enabled": False,
                                       "stop_acknowledged": False,
                                       "hardware_stop_requested": False}),
            ("stand", "stand_up", 8, {"status": "dispatched", "action_finished": True}),
            ("crouch", "lie_down", 8, {"status": "dispatched", "action_finished": True}),
            ("balance", "balance_stand", 8, {"status": "dispatched", "action_finished": True}),
        ]
        for action, primitive, timeout, result in cases:
            with self.subTest(action=action):
                code, printed = self.invoke([action], result)
                self.assertEqual(code, 0)
                self.assertEqual(printed, result)
                self.connected.assert_called_once_with(
                    "127.0.0.1:50051", "robonix/primitive/chassis/" + primitive)
                self.assertEqual(self.stub.Trigger.call_args.kwargs["timeout"], timeout)
                self.factories[cli.TRIGGER_ACTIONS[action][1]].assert_called_once()

    def test_authorization_and_posture_failures_keep_nonzero_exit(self):
        for argv, result, rpc_success in (
                (["enable"], {"status": "rejected", "error": "guard_not_ready"}, False),
                (["disable"], {"status": "stop_unconfirmed", "session_enabled": False}, False),
                (["disable"], {"status": "disabled", "session_enabled": True}, True),
                (["stand"], {"status": "dispatched", "action_finished": False}, True),
                (["crouch"], {"status": "cancelled", "action_finished": True}, True),
                (["balance"], {"status": "fault", "action_finished": True}, True),
                (["stand"], {"status": "dispatched", "action_finished": True}, False)):
            with self.subTest(argv=argv, result=result, rpc_success=rpc_success):
                code, printed = self.invoke(argv, result, trigger_success=rpc_success)
                self.assertNotEqual(code, 0)
                self.assertEqual(printed, result)

    def test_status_and_stop_remain_compatible(self):
        result = {"motion_enabled": False, "guard": {"ready": False}}
        with mock.patch.object(cli, "motion_status", return_value=result) as status:
            code, printed = self.invoke(["--atlas", "127.0.0.1:12345", "status"])
        self.assertEqual((code, printed), (0, result))
        status.assert_called_once_with("127.0.0.1:12345")
        code, printed = self.invoke(["stop"], {"status": "stopped", "stop_acknowledged": True})
        self.assertEqual(code, 0)
        self.connected.assert_called_once_with(
            "127.0.0.1:50051", "robonix/primitive/chassis/stop")
        self.assertEqual(self.stub.Stop.call_args.kwargs["timeout"], 5)
        code, _ = self.invoke(["stop"], {"status": "disabled", "stop_acknowledged": False},
                              trigger_success=False)
        self.assertNotEqual(code, 0)

    def test_rpc_connection_error_is_printed_as_json_and_returns_nonzero(self):
        class Unavailable(grpc.RpcError):
            def details(self):
                return "offline fake RPC"

            def code(self):
                return grpc.StatusCode.UNAVAILABLE

        self.connected.side_effect = Unavailable()
        code, printed = self.invoke(["enable"])
        self.assertNotEqual(code, 0)
        self.assertEqual(printed, {
            "status": "error", "error": "offline fake RPC", "grpc_code": "UNAVAILABLE"})


class MotionCompatibilityShellTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="go2-motion-shell-test-")
        self.addCleanup(self.directory.cleanup)
        self.temp = Path(self.directory.name)
        self.calls = self.temp / "calls.jsonl"
        fake_tool = """#!/usr/bin/python3
import json
import os
from pathlib import Path
import sys
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["TEST_MOTION_CALLS"], "a") as stream:
    stream.write(json.dumps({"tool": tool, "args": args}) + "\\n")
if tool == "systemctl":
    if "is-active" in args:
        sys.exit(0 if os.environ.get("TEST_LEGACY_ACTIVE") == "1" else 3)
    sys.exit(int(os.environ.get("TEST_SYSTEMCTL_EXIT", "0")))
if tool == "motion-python":
    sys.exit(int(os.environ.get("TEST_WAIT_EXIT", "0")))
sys.exit(int(os.environ.get("TEST_CLI_EXIT", "0")))
"""
        for name in ("systemctl", "motion-python", "bash"):
            path = self.temp / name
            path.write_text(fake_tool)
            path.chmod(0o755)

    def invoke(self, argv, **overrides):
        environment = dict(os.environ, PATH=str(self.temp) + ":/usr/bin:/bin",
                           GO2_SDK_PYTHON=str(self.temp / "motion-python"),
                           TEST_MOTION_CALLS=str(self.calls))
        environment.update(overrides)
        result = subprocess.run(
            ["/bin/bash", str(ROOT / "native/motion.sh"), *argv],
            env=environment, capture_output=True, text=True, timeout=5)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return result, calls

    def test_enable_starts_default_without_restarting_core_then_invokes_rpc(self):
        result, calls = self.invoke(["enable"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            {"tool": "systemctl", "args": ["--user", "is-active", "--quiet", "robonix-go2w-motion"]},
            {"tool": "systemctl", "args": ["--user", "start", "robonix-go2-native"]},
            {"tool": "motion-python", "args": [str(ROOT / "native/wait_motion.py"), "--available"]},
            {"tool": "bash", "args": [str(ROOT / "native/chassis.sh"), "enable"]},
        ])

    def test_legacy_motion_service_is_stopped_before_default_service_start(self):
        result, calls = self.invoke(["enable"], TEST_LEGACY_ACTIVE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[1], {
            "tool": "systemctl", "args": ["--user", "stop", "robonix-go2w-motion"]})
        self.assertEqual(calls[2], {
            "tool": "systemctl", "args": ["--user", "start", "robonix-go2-native"]})
        self.assertEqual(calls[-1], {
            "tool": "bash", "args": [str(ROOT / "native/chassis.sh"), "enable"]})

    def test_disable_and_other_commands_pass_through_without_service_changes(self):
        for argv in (["disable"], ["status"], ["stop"],
                     ["left", "--speed", ".02", "--duration", ".4"], ["balance"]):
            with self.subTest(argv=argv):
                self.calls.unlink(missing_ok=True)
                result, calls = self.invoke(argv, TEST_CLI_EXIT="2")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(calls, [
                    {"tool": "bash", "args": [str(ROOT / "native/chassis.sh"), *argv]}])

    def test_unavailable_provider_prevents_enable_rpc(self):
        result, calls = self.invoke(["enable"], TEST_WAIT_EXIT="7")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(calls[-1]["tool"], "motion-python")
        self.assertFalse(any(call["tool"] == "bash" for call in calls))

    def test_failed_service_start_prevents_wait_and_enable(self):
        result, calls = self.invoke(["enable"], TEST_SYSTEMCTL_EXIT="5")
        self.assertEqual(result.returncode, 5)
        self.assertEqual([call["tool"] for call in calls], ["systemctl", "systemctl"])


if __name__ == "__main__":
    unittest.main()
