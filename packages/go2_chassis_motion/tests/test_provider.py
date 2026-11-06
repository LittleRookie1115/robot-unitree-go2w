import asyncio
import contextlib
import inspect
import json
from pathlib import Path
import threading
import tomllib
import unittest
from unittest import mock

import chassis_pb2
import chassis_mcp
from mcp.server.fastmcp.exceptions import ToolError
import std_srvs_mcp
import std_srvs_pb2
import yaml
from robonix_api import Ok
from robonix_api.lifecycle import bind_user_handler, resolve_servicer

from go2_chassis_motion import provider
from go2_chassis_motion.controller import Limits, MotionController


PACKAGE = Path(__file__).resolve().parents[1]
DIRECTIONS = (provider.forward, provider.backward, provider.left, provider.right)


def movement_request(**fields):
    return chassis_pb2.ExecuteMoveCommand_Request(command=chassis_pb2.MoveCommand(**fields))


class DisabledProviderTest(unittest.TestCase):
    def test_model_visible_actions_never_acquire_lazy_backend_without_human_enable(self):
        with mock.patch.object(provider, "NativeRuntimeTransport",
                               side_effect=AssertionError("Only human enable may start a backend")) as factory:
            self.assertIsInstance(provider.initialize({
                "allow_motion": False, "robot_variant": "go2w",
                "state_time_basis": "dds-source-lowstate-tick-v1",
                "allowed_modes": [1, 3, 5, 6],
            }), Ok)
            try:
                for action in (provider.move,) + DIRECTIONS:
                    result = action(movement_request(), mock.Mock(is_active=lambda: True))
                    self.assertEqual(json.loads(result.status.data)["error"], "motion_disabled")
                for action in (provider.stand_up, provider.stand_down,
                               provider.lie_down, provider.balance_stand):
                    result = action(std_srvs_pb2.Trigger_Request(), mock.Mock(is_active=lambda: True))
                    self.assertFalse(result.success)
                    self.assertEqual(json.loads(result.message)["error"], "motion_disabled")
                state = json.loads(provider.status(std_srvs_pb2.Trigger_Request()).message)
                self.assertFalse(state["motion_enabled"])
                self.assertIsNone(state["backend_owner"])
                self.assertFalse(provider.stop(chassis_pb2.Stop_Request()).success)
                self.assertTrue(provider.disable(std_srvs_pb2.Trigger_Request()).success)
                factory.assert_not_called()
            finally:
                provider.shutdown()
            factory.assert_not_called()

    def test_default_lifecycle_and_grpc_never_create_a_ros_transport(self):
        with mock.patch.object(provider, "RosTransport",
                               side_effect=AssertionError("No live transport may be constructed")):
            self.assertIsInstance(provider.initialize({}), Ok)
            try:
                for action in (provider.move,) + DIRECTIONS:
                    with self.subTest(action=action.__name__):
                        response = action(movement_request(), mock.Mock(is_active=lambda: True))
                        self.assertEqual(json.loads(response.status.data),
                                         {"status": "rejected", "error": "motion_disabled"})
                stopped = provider.stop(chassis_pb2.Stop_Request())
                self.assertFalse(stopped.success)
                self.assertEqual(json.loads(stopped.message)["status"], "disabled")
                for action in (provider.enable, provider.arm, provider.stand_up,
                               provider.stand_down, provider.lie_down, provider.balance_stand):
                    result = action(std_srvs_pb2.Trigger_Request(), mock.Mock(is_active=lambda: True))
                    self.assertFalse(result.success)
                    self.assertEqual(json.loads(result.message)["error"], "motion_disabled")
            finally:
                provider.shutdown()


class EnableFactoryWiringTest(unittest.TestCase):
    def test_full_native_config_lazily_creates_backend_only_on_human_enable_and_closes_on_disable(self):
        backend = mock.Mock()
        backend.status.return_value = {"ready": False, "can_arm": True, "faulted": False}
        backend.enable.return_value = {"status": {"ready": True, "session_enabled": True}}
        backend.stop.return_value = True
        config = {
            "allow_motion": False, "robot_variant": "go2w",
            "state_time_basis": "dds-source-lowstate-tick-v1",
            "allowed_modes": [1, 3, 5, 6],
        }
        with mock.patch.object(provider, "NativeRuntimeTransport", return_value=backend) as factory, \
                mock.patch.object(provider, "RosTransport",
                                  side_effect=AssertionError("Legacy transport must not be created")):
            self.assertIsInstance(provider.initialize(config), Ok)
            try:
                factory.assert_not_called()
                self.assertFalse(provider.controller.status()["motion_enabled"])
                context = mock.Mock(is_active=lambda: True)
                response = provider.enable(std_srvs_pb2.Trigger_Request(), context)
                self.assertTrue(response.success)
                self.assertEqual(json.loads(response.message)["status"], "enabled")
                factory.assert_called_once()
                self.assertEqual(factory.call_args.args[0], provider.ROOT)
                self.assertEqual(factory.call_args.args[1], config)
                backend.enable.assert_called_once_with()
                backend.send.assert_not_called()
                backend.posture.assert_not_called()
                disabled = provider.disable(std_srvs_pb2.Trigger_Request())
                self.assertTrue(disabled.success)
                self.assertFalse(json.loads(disabled.message)["session_enabled"])
                backend.close.assert_called_once_with()
                self.assertIsNone(provider.controller.status()["backend_owner"])
            finally:
                provider.shutdown()
            factory.assert_called_once()
            backend.send.assert_not_called()
            backend.posture.assert_not_called()


class RegistrationTest(unittest.TestCase):
    def test_manifest_contracts_match_registered_grpc_handlers(self):
        manifest = yaml.safe_load((PACKAGE / "package_manifest.yaml").read_text())
        entries = {entry["name"]: entry for entry in manifest["capabilities"]}
        registrations = provider.provider._grpc_handlers
        registered = {contract: handler for contract, handler, _ in registrations}
        expected = {"enable", "disable", "arm", "move", "forward", "backward",
                    "left", "right", "stop", "motion_status", "status",
                    "stand_up", "stand_down", "lie_down", "balance_stand"}
        self.assertEqual(set(registered),
                         {"robonix/primitive/chassis/" + name for name in expected})
        self.assertEqual(len(registrations), len(registered))
        self.assertEqual(set(entries), set(registered))
        mcp_handlers = provider.provider._mcp_handlers
        self.assertEqual(len(mcp_handlers), 14)
        mcp_contracts = {handler._robonix_contract_id for handler in mcp_handlers}
        self.assertEqual(mcp_contracts, set(registered) - {
            "robonix/primitive/chassis/arm"})
        self.assertEqual(set(provider.MCP_HANDLERS), expected - {"arm"})
        self.assertEqual({tool.name for tool in provider.provider._mcp_app._tool_manager.list_tools()},
                         expected - {"arm"})
        for handler in mcp_handlers:
            self.assertTrue(inspect.iscoroutinefunction(handler))
            self.assertTrue(callable(handler._robonix_input_cls.json_schema))
            self.assertTrue(callable(handler._robonix_output_cls.json_schema))
        for contract_id, entry in entries.items():
            if "path" not in entry:  # move retains the system contract and policy.
                continue
            with self.subTest(contract=contract_id):
                contract = tomllib.loads((PACKAGE / entry["path"]).read_text())
                self.assertEqual(contract["contract"]["id"], contract_id)
                self.assertEqual(contract["mode"]["type"], "rpc")
                expected_visibility = contract_id.rsplit("/", 1)[1] != "arm"
                self.assertIs(contract["contract"]["llm_callable"], expected_visibility)

    def test_registered_handlers_bind_to_generated_services_without_hardware(self):
        import robonix_contracts_pb2_grpc

        for contract_id, handler, _ in provider.provider._grpc_handlers:
            with self.subTest(contract=contract_id):
                resolved = resolve_servicer(contract_id, robonix_contracts_pb2_grpc)
                self.assertIsNotNone(resolved, "Regenerate package gRPC stubs before testing")
                servicer, method_name, _, _ = resolved
                bound = bind_user_handler(servicer, method_name, handler)()
                self.assertTrue(callable(getattr(bound, method_name)))


class DirectionProviderTest(unittest.TestCase):
    def setUp(self):
        self.backend = mock.Mock(limits=Limits())
        self.result = {"status": "not_observed", "physical_motion_observed": False,
                       "dispatch_verified": True, "stop_acknowledged": True}
        self.backend.execute.return_value = self.result
        self.context = mock.Mock(is_active=lambda: True)
        patcher = mock.patch.object(provider, "controller", self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_positive_velocity_magnitudes_map_to_direction_signs(self):
        for action, axis, sign in ((provider.forward, "linear_x", 1),
                                   (provider.backward, "linear_x", -1),
                                   (provider.left, "linear_y", 1),
                                   (provider.right, "linear_y", -1)):
            with self.subTest(action=action.__name__):
                self.backend.reset_mock()
                response = action(movement_request(**{axis: .03, "duration_sec": .4}), self.context)
                command = self.backend.execute.call_args.args[0]
                self.assertEqual(command, chassis_pb2.MoveCommand(
                    **{axis: sign * .03, "duration_sec": .4}))
                self.assertIs(self.backend.execute.call_args.kwargs["is_active"],
                              self.context.is_active)
                self.assertEqual(json.loads(response.status.data), self.result)

    def test_forward_distance_uses_positive_magnitude_for_both_directions(self):
        for action, sign in ((provider.forward, 1), (provider.backward, -1)):
            with self.subTest(action=action.__name__):
                action(movement_request(forward_m=.04), self.context)
                self.assertEqual(self.backend.execute.call_args.args[0],
                                 chassis_pb2.MoveCommand(forward_m=sign * .04))

    def test_zero_or_omitted_fields_use_current_controller_limits(self):
        self.backend.limits = Limits(forward_mps=.04, reverse_mps=.03,
                                     lateral_mps=.02, default_duration_s=.12)
        for action, axis, speed in ((provider.forward, "linear_x", .04),
                                    (provider.backward, "linear_x", -.03),
                                    (provider.left, "linear_y", .02),
                                    (provider.right, "linear_y", -.02)):
            with self.subTest(action=action.__name__):
                action(movement_request(), self.context)
                command = self.backend.execute.call_args.args[0]
                self.assertEqual(command, chassis_pb2.MoveCommand(
                    **{axis: speed, "duration_sec": .12}))

    def test_contradictory_negative_and_unsupported_fields_never_reach_controller(self):
        for action in DIRECTIONS:
            axis = "linear_x" if action in (provider.forward, provider.backward) else "linear_y"
            other_axis = "linear_y" if axis == "linear_x" else "linear_x"
            invalid = [{axis: -.01}, {"duration_sec": -.01}, {other_axis: .01},
                       {"forward_m": -.01}, {"linear_z": .01}, {"angular_x": .01},
                       {"angular_y": .01}, {"angular_z": .01}, {"rotate_deg": 1}]
            if axis == "linear_x":
                invalid.extend([{"forward_m": .02, axis: .01},
                                {"forward_m": .02, "duration_sec": .2}])
            else:
                invalid.append({"forward_m": .02})
            for fields in invalid:
                with self.subTest(action=action.__name__, fields=fields):
                    self.backend.reset_mock()
                    result = json.loads(action(movement_request(**fields), self.context).status.data)
                    self.assertEqual(result["status"], "rejected")
                    self.assertTrue(result["error"])
                    self.backend.execute.assert_not_called()

    def test_nonfinite_values_in_every_field_never_reach_controller(self):
        for action in DIRECTIONS:
            for field in chassis_pb2.MoveCommand.DESCRIPTOR.fields:
                for invalid in (float("nan"), float("inf"), float("-inf")):
                    with self.subTest(action=action.__name__, field=field.name, value=invalid):
                        self.backend.reset_mock()
                        result = json.loads(action(
                            movement_request(**{field.name: invalid}), self.context).status.data)
                        self.assertEqual(result["status"], "rejected")
                        self.backend.execute.assert_not_called()

    def test_result_evidence_is_not_reinterpreted_by_provider(self):
        for status, observed in (("not_observed", False), ("dispatched", True),
                                 ("cancelled", False), ("fault", False)):
            result = dict(self.result, status=status, physical_motion_observed=observed)
            self.backend.execute.return_value = result
            for action in (provider.move,) + DIRECTIONS:
                with self.subTest(status=status, action=action.__name__):
                    response = action(movement_request(), self.context)
                    self.assertEqual(json.loads(response.status.data), result)

    def test_controller_rejects_requests_above_its_limits_before_any_send(self):
        transport = mock.Mock()
        transport.send.side_effect = AssertionError("Invalid requests must never dispatch")
        controller = MotionController(transport=transport)
        with mock.patch.object(provider, "controller", controller):
            for action, fields in ((provider.forward, {"linear_x": .251}),
                                   (provider.backward, {"linear_x": .251}),
                                   (provider.left, {"linear_y": .101}),
                                   (provider.right, {"linear_y": .101}),
                                   (provider.forward, {"duration_sec": 2.01}),
                                   (provider.backward, {"forward_m": .501})):
                with self.subTest(action=action.__name__, fields=fields):
                    result = json.loads(action(movement_request(**fields), self.context).status.data)
                    self.assertEqual(result["status"], "rejected")
            transport.send.assert_not_called()
            transport.posture.assert_not_called()


class AuthorizationProviderTest(unittest.TestCase):
    def test_enable_delegates_cancellation_and_preserves_controller_response(self):
        backend = mock.Mock()
        result = {"status": "enabled", "session_enabled": True,
                  "guard": {"ready": True, "session_enabled": True}}
        backend.enable.return_value = result
        context = mock.Mock(is_active=lambda: True)
        with mock.patch.object(provider, "controller", backend):
            response = provider.enable(std_srvs_pb2.Trigger_Request(), context)
        backend.enable.assert_called_once_with(is_active=context.is_active)
        backend.execute.assert_not_called()
        self.assertTrue(response.success)
        self.assertEqual(json.loads(response.message), result)

    def test_disable_calls_explicit_revoke_instead_of_stop(self):
        backend = mock.Mock()
        result = {"status": "disabled", "session_enabled": False, "stop_acknowledged": True}
        backend.disable.return_value = result
        with mock.patch.object(provider, "controller", backend):
            response = provider.disable(std_srvs_pb2.Trigger_Request())
        backend.disable.assert_called_once_with()
        backend.stop.assert_not_called()
        backend.enable.assert_not_called()
        self.assertTrue(response.success)
        self.assertEqual(json.loads(response.message), result)

    def test_disable_without_backend_does_not_invent_hardware_stop_ack(self):
        backend = mock.Mock()
        result = {"status": "disabled", "session_enabled": False,
                  "stop_acknowledged": False, "hardware_stop_requested": False}
        backend.disable.return_value = result
        with mock.patch.object(provider, "controller", backend):
            response = provider.disable(std_srvs_pb2.Trigger_Request())
        self.assertTrue(response.success)
        self.assertEqual(json.loads(response.message), result)

    def test_disable_does_not_report_success_for_unconfirmed_revocation(self):
        for result in ({"status": "stop_unconfirmed", "session_enabled": False},
                       {"status": "disabled", "session_enabled": True},
                       {"status": "fault", "error": "backend lost"}):
            with self.subTest(result=result):
                backend = mock.Mock()
                backend.disable.return_value = result
                with mock.patch.object(provider, "controller", backend):
                    response = provider.disable(std_srvs_pb2.Trigger_Request())
                self.assertFalse(response.success)
                self.assertEqual(json.loads(response.message), result)

    def test_posture_aliases_delegate_canonical_actions_without_inventing_completion(self):
        context = mock.Mock(is_active=lambda: True)
        backend = mock.Mock()
        result = {"status": "dispatched", "action_finished": True,
                  "physical_completion_verified": False}
        backend.posture.return_value = result
        with mock.patch.object(provider, "controller", backend):
            for action, canonical in ((provider.stand_up, "stand_up"),
                                      (provider.stand_down, "stand_down"),
                                      (provider.lie_down, "stand_down"),
                                      (provider.balance_stand, "balance_stand")):
                with self.subTest(action=action.__name__):
                    response = action(std_srvs_pb2.Trigger_Request(), context)
                    backend.posture.assert_called_with(canonical, is_active=context.is_active)
                    self.assertTrue(response.success)
                    self.assertEqual(json.loads(response.message), result)

    def test_posture_success_requires_action_finished_and_nonfault_status(self):
        backend = mock.Mock()
        context = mock.Mock(is_active=lambda: True)
        cases = (({"status": "dispatched", "action_finished": True}, True),
                 ({"status": "completed", "action_finished": True}, True),
                 ({"status": "dispatched", "stop_acknowledged": True}, False),
                 ({"status": "dispatched", "action_finished": False}, False),
                 ({"status": "cancelled", "action_finished": True}, False),
                 ({"status": "fault", "action_finished": True}, False),
                 ({"status": "stop_unconfirmed", "action_finished": True}, False))
        with mock.patch.object(provider, "controller", backend):
            for result, success in cases:
                with self.subTest(result=result):
                    backend.posture.return_value = result
                    response = provider.stand_up(std_srvs_pb2.Trigger_Request(), context)
                    self.assertEqual(response.success, success)
                    self.assertEqual(json.loads(response.message), result)


class McpProviderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = mock.Mock(limits=Limits())
        self.backend.execute.return_value = {
            "status": "completed", "stop_acknowledged": True,
            "dispatch_verified": True, "physical_motion_observed": True,
        }
        self.backend.posture.return_value = {
            "status": "dispatched", "action_finished": True,
            "stop_acknowledged": False, "physical_completion_verified": False,
        }
        self.backend.status.return_value = {
            "active": False, "motion_enabled": True, "backend_owner": "primitive",
        }
        patcher = mock.patch.object(provider, "controller", self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request_for(self, leaf, **fields):
        handler = provider.MCP_HANDLERS[leaf]
        data = {"command": fields} if "command" in handler._robonix_input_cls.json_schema().get(
            "properties", {}) else {}
        return handler._robonix_input_cls.from_dict(data)

    async def test_action_mcp_tools_without_permission_never_create_backend_or_move(self):
        with mock.patch.object(provider, "NativeRuntimeTransport",
                               side_effect=AssertionError("MCP must not create a backend")) as factory:
            self.assertIsInstance(provider.initialize({
                "allow_motion": False, "robot_variant": "go2w",
                "state_time_basis": "dds-source-lowstate-tick-v1",
                "allowed_modes": [1, 3, 5, 6],
            }), Ok)
            try:
                for leaf, handler in provider.MCP_HANDLERS.items():
                    if leaf == "enable":  # Only an explicit enable may acquire a backend.
                        continue
                    with self.subTest(tool=leaf):
                        request = self.request_for(leaf)
                        if leaf in ("status", "motion_status", "disable"):
                            response = await handler(request)
                            self.assertIsInstance(response, std_srvs_mcp.Trigger_Response)
                            if leaf == "disable":
                                result = json.loads(response.message)
                                self.assertFalse(result["session_enabled"])
                                self.assertFalse(result["stop_acknowledged"])
                                self.assertFalse(result["hardware_stop_requested"])
                        else:
                            with self.assertRaises(ToolError) as raised:
                                await handler(request)
                            result = json.loads(str(raised.exception))
                            self.assertIn(result["status"], ("rejected", "disabled"))
                        factory.assert_not_called()
                self.assertFalse(provider.controller.status()["motion_enabled"])
            finally:
                provider.shutdown()
            factory.assert_not_called()

    async def test_direction_mcp_reuses_sign_validation_and_typed_response(self):
        for leaf, axis, sign in (("forward", "linear_x", 1), ("backward", "linear_x", -1),
                                 ("left", "linear_y", 1), ("right", "linear_y", -1)):
            with self.subTest(tool=leaf):
                request = self.request_for(leaf, **{axis: .03, "duration_sec": .4})
                response = await provider.MCP_HANDLERS[leaf](request)
                self.assertIsInstance(response, chassis_mcp.ExecuteMoveCommand_Response)
                self.assertEqual(json.loads(response.status.data), self.backend.execute.return_value)
                command = self.backend.execute.call_args.args[0]
                self.assertEqual(getattr(command, axis), sign * .03)
                self.assertEqual(command.duration_sec, .4)
                self.assertTrue(self.backend.execute.call_args.kwargs["is_active"]())
                self.backend.reset_mock()
                with self.assertRaises(ToolError) as raised:
                    await provider.MCP_HANDLERS[leaf](self.request_for(leaf, **{axis: -.03}))
                self.assertEqual(json.loads(str(raised.exception))["status"], "rejected")
                self.backend.execute.assert_not_called()

    async def test_fastmcp_shim_is_callable_offline_and_preserves_structured_output(self):
        manager = provider.provider._mcp_app._tool_manager
        wire = await manager.call_tool("backward", {
            "command": {"forward_m": .02}}, convert_result=False)
        self.assertEqual(json.loads(wire["status"]["data"]), self.backend.execute.return_value)
        self.assertEqual(self.backend.execute.call_args.args[0].forward_m, -.02)

    async def test_fastmcp_execution_reports_unobserved_motion_as_tool_error(self):
        result = {"status": "not_observed", "dispatch_verified": True,
                  "physical_motion_observed": False, "stop_acknowledged": True}
        self.backend.execute.return_value = result
        with self.assertRaises(ToolError) as raised:
            await provider.provider._mcp_app._tool_manager.call_tool(
                "forward", {"command": {"linear_x": .02}}, convert_result=False)
        self.assertIn(json.dumps(result), str(raised.exception))

    async def test_unobserved_and_unverified_motion_are_tool_errors_with_original_evidence(self):
        results = [
            {"status": "not_observed", "dispatch_verified": True, "physical_motion_observed": False},
            {"status": "not_dispatched", "dispatch_verified": False, "physical_motion_observed": False},
            {"status": "rejected", "error": "motion_disabled"},
            {"status": "fault", "error": "guard lost"},
            {"status": "cancelled", "stop_acknowledged": True},
            {"status": "stop_unconfirmed", "stop_acknowledged": False},
            {"status": "completed", "stop_acknowledged": True},
            dict(self.backend.execute.return_value, dispatch_verified=1),
            dict(self.backend.execute.return_value, physical_motion_observed="true"),
            dict(self.backend.execute.return_value, status="dispatched"),
        ]
        for result in results:
            with self.subTest(result=result):
                self.backend.execute.return_value = result
                with self.assertRaises(ToolError) as raised:
                    await provider.MCP_HANDLERS["move"](self.request_for("move", linear_x=.02))
                self.assertEqual(json.loads(str(raised.exception)), result)
        self.backend.stop.assert_not_called()

    async def test_zero_velocity_needs_explicit_zero_evidence(self):
        result = {"status": "completed", "stop_acknowledged": True,
                  "dispatch_verified": False, "physical_motion_observed": False}
        self.backend.execute.return_value = result
        response = await provider.MCP_HANDLERS["move"](self.request_for("move", duration_sec=.1))
        self.assertEqual(json.loads(response.status.data), result)
        self.backend.execute.return_value = dict(result, physical_motion_observed=True)
        with self.assertRaises(ToolError):
            await provider.MCP_HANDLERS["move"](self.request_for("move", duration_sec=.1))

    async def test_posture_requires_action_finished_and_no_stop_ack(self):
        for leaf in ("stand_up", "stand_down", "lie_down", "balance_stand"):
            response = await provider.MCP_HANDLERS[leaf](self.request_for(leaf))
            self.assertIsInstance(response, std_srvs_mcp.Trigger_Response)
            self.assertTrue(response.success)
            self.assertFalse(json.loads(response.message)["stop_acknowledged"])
            self.assertTrue(self.backend.posture.call_args.kwargs["is_active"]())
        for result in ({"status": "dispatched", "action_finished": False},
                       {"status": "fault", "action_finished": True},
                       {"status": "cancelled", "action_finished": True}):
            with self.subTest(result=result):
                self.backend.posture.return_value = result
                with self.assertRaises(ToolError) as raised:
                    await provider.MCP_HANDLERS["stand_up"](self.request_for("stand_up"))
                self.assertEqual(json.loads(str(raised.exception)), result)
        self.backend.stop.assert_not_called()

    async def test_status_stop_disable_delegate_without_owning_action_context(self):
        self.backend.stop.return_value = {"status": "stopped", "stop_acknowledged": True}
        self.backend.disable.return_value = {
            "status": "disabled", "session_enabled": False,
            "stop_acknowledged": False, "hardware_stop_requested": False,
        }
        for leaf in ("status", "motion_status", "stop", "disable"):
            response = await provider.MCP_HANDLERS[leaf](self.request_for(leaf))
            self.assertTrue(response.success)
        self.assertEqual(self.backend.status.call_args_list, [mock.call(), mock.call()])
        self.backend.stop.assert_called_once_with(clear_fault=True)
        self.backend.disable.assert_called_once_with()

    async def test_cancelling_shielded_busy_request_only_invalidates_its_own_context(self):
        entered, release = threading.Event(), threading.Event()
        captured = {}

        def busy(command, is_active):
            captured["is_active"] = is_active
            entered.set()
            if not release.wait(2):
                raise AssertionError("offline worker was not released")
            return {"status": "rejected", "error": "another_move_is_active"}

        self.backend.execute.side_effect = busy
        task = asyncio.create_task(provider.MCP_HANDLERS["move"](
            self.request_for("move", linear_x=.02, duration_sec=.5)))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            jobs = tuple(provider._mcp_jobs)
            self.assertEqual(len(jobs), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(captured["is_active"]())
            self.assertFalse(jobs[0].cancelled())
            self.assertFalse(jobs[0].done())
            self.backend.stop.assert_not_called()
            self.backend.disable.assert_not_called()
            release.set()
            await asyncio.wait_for(asyncio.gather(*jobs), 1)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def test_cancelled_move_controller_observes_inactive_context_and_revokes_its_backend(self):
        sent = threading.Event()
        transport = mock.Mock()
        transport.status.return_value = {"ready": True, "session_enabled": True, "movement_ready": True}
        transport.send.side_effect = lambda *velocity: sent.set()
        transport.stop.return_value = True
        provider.controller = MotionController(transport=transport)
        task = asyncio.create_task(provider.MCP_HANDLERS["move"](
            self.request_for("move", linear_x=.02, duration_sec=.5)))
        try:
            self.assertTrue(await asyncio.to_thread(sent.wait, 1))
            jobs = tuple(provider._mcp_jobs)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(asyncio.gather(*jobs), 1)
            transport.stop.assert_called()
            transport.finish.assert_not_called()
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


if __name__ == "__main__":
    unittest.main()
