"""Client enable tests through FastMCP's real tool manager, without ROS/SDK."""
import asyncio
import contextlib
import copy
import json
import threading
import unittest
from unittest import mock

from mcp.server.fastmcp.exceptions import ToolError
from robonix_api import Ok

from go2w_chassis_motion import controller as controller_module
from go2w_chassis_motion import provider
from go2w_chassis_motion.controller import Limits, MotionController


NATIVE_CONFIG = {
    "allow_motion": False,
    "robot_variant": "go2w",
    "state_time_basis": "dds-source-lowstate-tick-v1",
    "allowed_modes": [1, 3, 5, 6],
    "network_interface": "eth10",
    "robot_address": "192.168.123.161",
}


class ClientEnableMcpTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.release_events = []
        self.client_tasks = []
        # initialize() closes the previous controller. Give it a disposable
        # controller and restore the caller's globals only after jobs drain.
        self.patches.enter_context(mock.patch.object(provider, "controller", MotionController()))
        self.patches.enter_context(mock.patch.object(provider, "transport", None))
        self.patches.enter_context(mock.patch.object(provider, "_mcp_jobs", set()))

        def offline_limits(path=None, fallback=None):
            return fallback if fallback is not None else Limits()

        self.patches.enter_context(mock.patch.object(
            provider, "load_limits", side_effect=offline_limits))
        self.patches.enter_context(mock.patch.object(
            controller_module, "load_limits", side_effect=offline_limits))

        self.guard = {
            "ready": False, "session_enabled": False, "can_arm": True,
            "faulted": False, "guard_state": "DISARMED", "daemon_armed": False,
        }
        self.backend = mock.Mock(spec=[
            "status", "enable", "stop", "close", "finish", "send", "posture",
            "backend_owner", "limits",
        ])
        self.backend.backend_owner = "primitive"
        self.backend.limits = Limits()
        self.backend.status.side_effect = lambda: dict(self.guard)
        self.backend.enable.side_effect = self.enable_backend
        self.backend.stop.side_effect = self.revoke_backend
        self.backend.close.side_effect = self.close_backend
        self.backend.finish.return_value = True
        self.factory = mock.Mock(return_value=self.backend)

        def offline_factory(root, config, _environment, *, limits):
            # Do not inspect or retain the environment in mock call records.
            return self.factory(root, config, limits=limits)

        self.patches.enter_context(mock.patch.object(
            provider, "NativeRuntimeTransport", new=offline_factory))
        self.legacy_factory = self.patches.enter_context(mock.patch.object(
            provider, "RosTransport",
            side_effect=AssertionError("Offline Client enable must use the native factory")))
        self.assertIsInstance(provider.initialize(copy.deepcopy(NATIVE_CONFIG)), Ok)
        self.manager = provider.provider._mcp_app._tool_manager

    def enable_backend(self):
        self.guard.update(ready=True, session_enabled=True, can_arm=False,
                          guard_state="ARMED", daemon_armed=True)
        return {"status": dict(self.guard)}

    def revoke_backend(self):
        self.guard.update(ready=False, session_enabled=False, can_arm=True,
                          guard_state="DISARMED", daemon_armed=False)
        return True

    def close_backend(self):
        self.revoke_backend()

    async def call(self, leaf):
        return await self.manager.call_tool(leaf, {}, convert_result=False)

    async def drain_jobs(self):
        while provider._mcp_jobs:
            await asyncio.wait_for(asyncio.gather(
                *tuple(provider._mcp_jobs), return_exceptions=True), timeout=3)
            await asyncio.sleep(0)  # let the provider's completion callbacks run

    async def asyncTearDown(self):
        # Release every blocked thread even when a test assertion fails.
        for event in self.release_events:
            event.set()
        for task in self.client_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.client_tasks, return_exceptions=True)
        try:
            await self.drain_jobs()
        finally:
            await asyncio.to_thread(provider.shutdown)
        self.assertFalse(provider._mcp_jobs)
        self.backend.send.assert_not_called()
        self.backend.posture.assert_not_called()
        self.assertEqual(self.legacy_factory.call_count, 0)

    async def assert_enable_tool_error(self, result):
        with mock.patch.object(provider.controller, "enable", return_value=result) as enable:
            with self.assertRaises(ToolError) as raised:
                await self.call("enable")
            self.assertIn(json.dumps(result), str(raised.exception))
            enable.assert_called_once()
            self.assertTrue(enable.call_args.kwargs["is_active"]())
        self.factory.assert_not_called()

    async def test_client_enable_is_lazy_idempotent_and_disable_releases_backend(self):
        self.factory.assert_not_called()
        self.assertFalse(provider.controller.status()["motion_enabled"])

        first = await self.call("enable")
        self.assertIs(first["success"], True)
        enabled = json.loads(first["message"])
        self.assertEqual(enabled["status"], "enabled")
        self.assertIs(enabled["session_enabled"], True)
        self.assertIs(enabled["guard"]["ready"], True)
        self.assertIs(enabled["guard"]["session_enabled"], True)
        self.factory.assert_called_once_with(
            provider.ROOT, NATIVE_CONFIG, limits=provider.controller.limits)
        self.backend.enable.assert_called_once_with()
        self.backend.send.assert_not_called()
        self.backend.posture.assert_not_called()

        repeated = await self.call("enable")
        self.assertIs(repeated["success"], True)
        self.assertEqual(json.loads(repeated["message"])["guard"], dict(self.guard))
        self.factory.assert_called_once()
        self.backend.enable.assert_called_once_with()
        self.backend.stop.assert_not_called()

        disabled = await self.call("disable")
        self.assertIs(disabled["success"], True)
        released = json.loads(disabled["message"])
        self.assertEqual(released["status"], "disabled")
        self.assertIs(released["session_enabled"], False)
        self.backend.stop.assert_called_once_with()
        self.backend.close.assert_called_once_with()
        self.assertIsNone(provider.controller.transport)
        self.assertIsNone(provider.controller.status()["backend_owner"])

    async def test_failed_rejected_cancelled_and_unconfirmed_results_are_tool_errors(self):
        for status in ("failed", "fault", "rejected", "cancelled", "stop_unconfirmed"):
            with self.subTest(status=status):
                # Even optimistic flags must not turn a non-enabled result green.
                await self.assert_enable_tool_error({
                    "status": status, "error": "offline failure",
                    "session_enabled": True,
                    "guard": {"ready": True, "session_enabled": True},
                })

    async def test_enabled_status_requires_explicit_true_session_and_guard_flags(self):
        valid = {"status": "enabled", "session_enabled": True,
                 "guard": {"ready": True, "session_enabled": True}}
        missing = object()
        for path in (("session_enabled",), ("guard", "ready"),
                     ("guard", "session_enabled")):
            for invalid in (False, 1, missing):
                with self.subTest(path=path, invalid="missing" if invalid is missing else invalid):
                    result = copy.deepcopy(valid)
                    target = result if len(path) == 1 else result["guard"]
                    if invalid is missing:
                        target.pop(path[-1])
                    else:
                        target[path[-1]] = invalid
                    await self.assert_enable_tool_error(result)
        for guard in (None, []):
            with self.subTest(guard=guard):
                await self.assert_enable_tool_error(dict(valid, guard=guard))
        await self.assert_enable_tool_error(dict(
            valid, guard={"ready": True, "session_enabled": True, "faulted": True}))
        await self.assert_enable_tool_error(dict(
            valid, guard={"ready": True, "session_enabled": True, "guard_state": "FAULT"}))

    async def test_backend_enable_failure_is_a_tool_error_and_revokes(self):
        self.backend.enable.side_effect = RuntimeError("offline enable RPC failed")
        with self.assertRaises(ToolError) as raised:
            await self.call("enable")
        result = provider.controller.status()["last_result"]
        self.assertEqual(result["status"], "fault")
        self.assertIn("offline enable RPC failed", result["error"])
        self.assertIn(json.dumps(result), str(raised.exception))
        self.factory.assert_called_once()
        self.backend.enable.assert_called_once_with()
        self.backend.stop.assert_called_once_with()
        self.backend.finish.assert_not_called()
        self.assertFalse(provider.controller.status()["session_enabled"])

    async def check_cancelled_enable(self, phase):
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        captured = {}
        real_enable = provider.controller.enable

        def capture_context(*, is_active):
            captured["is_active"] = is_active
            return real_enable(is_active=is_active)

        def blocked_factory(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("offline factory was not released")
            return self.backend

        def blocked_enable():
            entered.set()
            if not release.wait(3):
                raise AssertionError("offline enable was not released")
            return self.enable_backend()

        if phase == "factory":
            self.factory.side_effect = blocked_factory
        else:
            self.backend.enable.side_effect = blocked_enable
        with mock.patch.object(provider.controller, "enable", side_effect=capture_context):
            task = asyncio.create_task(self.call("enable"))
            self.client_tasks.append(task)
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertTrue(captured["is_active"]())
            jobs = tuple(provider._mcp_jobs)
            self.assertEqual(len(jobs), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(captured["is_active"]())
            self.assertFalse(jobs[0].cancelled())
            self.assertFalse(jobs[0].done())
            self.backend.stop.assert_not_called()
            release.set()
            await self.drain_jobs()

        self.factory.assert_called_once()
        if phase == "factory":
            self.backend.enable.assert_not_called()
        else:
            self.backend.enable.assert_called_once_with()
        self.backend.stop.assert_called_once_with()
        self.backend.finish.assert_not_called()
        state = provider.controller.status()
        self.assertFalse(state["active"])
        self.assertFalse(state["session_enabled"])
        self.assertEqual(state["last_result"]["status"], "cancelled")
        self.assertIs(state["last_result"]["stop_acknowledged"], True)

    async def test_cancellation_during_factory_invalidates_context_and_revokes(self):
        await self.check_cancelled_enable("factory")

    async def test_cancellation_during_enable_invalidates_context_and_revokes(self):
        await self.check_cancelled_enable("enable")


if __name__ == "__main__":
    unittest.main()
