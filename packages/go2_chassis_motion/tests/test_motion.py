from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

from go2_chassis_motion.controller import Limits, MotionController, MotionError, plan_command
from go2_chassis_motion.state_guard import SportWitness
from go2_chassis_motion.transport import check_live_prerequisites


class FakeTransport:
    def __init__(self, stop_ok=True, fail_after=None, ready=True, feedback=True, dispatch=True):
        self.commands = []
        self.stops = []
        self.first_command = threading.Event()
        self.stop_ok = stop_ok
        self.fail_after = fail_after
        self.ready = ready
        self.postures = []
        self.posture_holds = 0
        self.arms = 0
        self.can_arm = False
        self.movement_ready = True
        self.session_enabled = ready
        self.finishes = []
        self.dispatch_count = 0
        self.feedback_enabled = feedback
        self.dispatch_enabled = dispatch
        self.motion_observed = False
        self.closed = False

    def status(self):
        return {"ready": self.ready, "can_arm": self.can_arm,
                "session_enabled": self.session_enabled,
                "dispatch": {"move_dispatch_count": self.dispatch_count,
                             "motion_evidence_instance": "fixture-instance"},
                "motion_observation": {"motion_observed": self.motion_observed},
                "movement_ready": self.movement_ready, "reason": "guard_not_armed"}

    def enable(self):
        self.arms += 1
        self.ready = True
        self.session_enabled = True
        self.can_arm = False
        return {"session_enabled": True, "status": self.status()}

    def send(self, vx, vy, wz):
        if self.fail_after is not None and len(self.commands) >= self.fail_after:
            raise RuntimeError("independent guard lost state")
        self.commands.append((time.monotonic(), vx, vy, wz))
        if vx or vy or wz:
            self.dispatch_count += int(self.dispatch_enabled)
            self.motion_observed = self.feedback_enabled
        self.first_command.set()

    def stop(self):
        self.stops.append(time.monotonic())
        self.ready = False
        self.session_enabled = False
        return self.stop_ok

    def finish(self):
        self.stops.append(time.monotonic())
        self.finishes.append(time.monotonic())
        return self.stop_ok

    def posture(self, action):
        self.postures.append(action)
        self.first_command.set()

    def hold_posture(self):
        self.posture_holds += 1

    def finish_posture(self):
        return self.stop_ok

    def close(self):
        self.closed = True


class PlanningTest(unittest.TestCase):
    def test_velocity_and_shortcut_modes(self):
        forward = plan_command(SimpleNamespace(forward_m=0.05), Limits())
        self.assertAlmostEqual(forward.duration_s, 0.2)
        self.assertEqual(forward.vx, 0.25)
        for angle, sign in [(5, 1), (-5, -1)]:
            turn = plan_command(SimpleNamespace(rotate_deg=angle), Limits())
            self.assertAlmostEqual(turn.wz, sign * 0.50)
        velocity = plan_command(SimpleNamespace(linear_x=0.04, angular_z=-0.1), Limits())
        self.assertEqual(velocity.duration_s, 0.50)

    def test_accepts_bounded_reverse_and_both_lateral_directions(self):
        reverse = plan_command(SimpleNamespace(forward_m=-0.05), Limits())
        self.assertEqual(reverse.vx, -0.25)
        self.assertAlmostEqual(reverse.duration_s, 0.2)
        for vy in (-0.03, 0.03):
            burst = plan_command(SimpleNamespace(linear_x=-0.02, linear_y=vy), Limits())
            self.assertEqual((burst.vx, burst.vy), (-0.02, vy))

    def test_rejects_unbounded_planar_and_other_axes(self):
        for field, value in [("linear_x", -1.01), ("linear_y", 0.501),
                             ("linear_y", -0.501), ("linear_z", 0.01),
                             ("angular_x", 0.01), ("angular_y", 0.01)]:
            with self.subTest(field=field), self.assertRaises(MotionError):
                plan_command(SimpleNamespace(**{field: value}), Limits())

    def test_rejects_unbounded_duration_distance_and_speed(self):
        for fields in [{"duration_sec": -1}, {"duration_sec": 3.1}, {"forward_m": 1},
                       {"rotate_deg": 90}, {"linear_x": 1.01}, {"angular_z": 1.01}]:
            with self.subTest(fields=fields), self.assertRaises(MotionError):
                plan_command(SimpleNamespace(**fields), Limits())

    def test_rejects_nan_in_every_field(self):
        for field in ("linear_x", "linear_y", "linear_z", "angular_x", "angular_y",
                      "angular_z", "duration_sec", "forward_m", "rotate_deg"):
            with self.subTest(field=field), self.assertRaises(MotionError):
                plan_command(SimpleNamespace(**{field: float("nan")}), Limits())

    def test_limits_can_only_tighten(self):
        for limits in [dict(forward_mps=1.01), dict(yaw_rps=1.01),
                       dict(reverse_mps=1.01), dict(lateral_mps=0.51),
                       dict(posture_duration_s=4.1),
                       dict(max_duration_s=3.1), dict(forward_mps=0),
                       dict(yaw_rps=float("inf"))]:
            with self.subTest(limits=limits), self.assertRaises(MotionError):
                Limits(**limits)


class ControllerTest(unittest.TestCase):
    def test_human_enable_recovers_fault_once_without_cancelling_itself(self):
        old = FakeTransport(fail_after=0)
        replacement = FakeTransport(ready=False)
        replacement.can_arm = True
        factory = mock.Mock(return_value=replacement)
        controller = MotionController(old, transport_factory=factory)
        self.assertEqual(controller.execute(
            SimpleNamespace(linear_x=.02, duration_sec=.01))["status"], "fault")
        self.assertTrue(controller.status()["faulted"])
        self.assertEqual(controller.enable()["status"], "enabled")
        self.assertTrue(old.closed)
        self.assertTrue(old.stops)
        self.assertEqual(replacement.arms, 1)
        self.assertFalse(replacement.stops)
        self.assertFalse(replacement.commands or replacement.postures)
        self.assertEqual(controller.status()["last_result"]["status"], "enabled")
        self.assertTrue(controller.status()["session_enabled"])
        self.assertFalse(controller.status()["faulted"])
        factory.assert_called_once()

    def test_explicit_stop_during_recovery_still_cancels_new_enable(self):
        old = FakeTransport(ready=False)
        replacement = FakeTransport(ready=False)
        replacement.can_arm = True
        created, release = threading.Event(), threading.Event()

        def factory():
            created.set()
            if not release.wait(timeout=2):
                raise RuntimeError("fixture recovery wait expired")
            return replacement

        controller = MotionController(old, transport_factory=factory)
        controller._faulted = True
        results = []
        thread = threading.Thread(target=lambda: results.append(controller.enable()))
        thread.start()
        try:
            self.assertTrue(created.wait(timeout=1))
            controller.stop()
        finally:
            release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertEqual(replacement.arms, 0)
        self.assertTrue(replacement.stops)
        self.assertFalse(replacement.commands or replacement.postures)
        self.assertEqual(controller.status()["last_result"]["status"], "cancelled")
        self.assertFalse(controller.status()["session_enabled"])

    def test_lazy_backend_is_only_created_on_enable_and_disable_releases_it(self):
        backend = FakeTransport(ready=False)
        backend.can_arm = True
        factory = mock.Mock(return_value=backend)
        controller = MotionController(transport_factory=factory)
        self.assertFalse(controller.status()["motion_enabled"])
        self.assertEqual(controller.execute(SimpleNamespace(linear_x=.02))["status"], "rejected")
        factory.assert_not_called()
        self.assertEqual(controller.enable()["status"], "enabled")
        factory.assert_called_once()
        result = controller.disable()
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["session_enabled"])
        self.assertTrue(backend.closed)
        self.assertFalse(controller.status()["motion_enabled"])

    def test_wire_dispatch_without_physical_feedback_is_not_completed(self):
        controller = MotionController(FakeTransport(feedback=False))
        result = controller.execute(SimpleNamespace(linear_x=.02, duration_sec=.01))
        self.assertEqual(result["status"], "not_observed")
        self.assertTrue(result["dispatch_verified"])
        self.assertFalse(result["physical_motion_observed"])

    def test_local_publishing_without_remote_dispatch_evidence_is_not_completed(self):
        controller = MotionController(FakeTransport(dispatch=False, feedback=False))
        result = controller.execute(SimpleNamespace(linear_x=.02, duration_sec=.01))
        self.assertEqual(result["status"], "not_dispatched")
        self.assertFalse(result["dispatch_verified"])

    def test_disable_is_idempotent_without_inventing_a_hardware_stop(self):
        result = MotionController().disable()
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["hardware_stop_requested"])
        self.assertFalse(result["stop_acknowledged"])

    def test_disable_cancels_active_request_then_closes_the_backend(self):
        backend = FakeTransport()
        controller = MotionController(backend)
        results = []
        thread = threading.Thread(target=lambda: results.append(controller.execute(
            SimpleNamespace(linear_x=.05, duration_sec=.5))))
        thread.start()
        self.assertTrue(backend.first_command.wait(timeout=1))
        self.assertEqual(controller.disable()["status"], "disabled")
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertTrue(backend.closed)
        self.assertFalse(controller.status()["session_enabled"])

    def test_idle_backend_fault_is_reported_without_enabling_or_dispatching(self):
        transport = FakeTransport(ready=False)
        transport.status = lambda: {
            "ready": False, "faulted": True,
            "reason": "adapter_fault: SDK heartbeat failed",
            "session_enabled": False,
        }
        controller = MotionController(transport)
        status = controller.status()
        self.assertTrue(status["faulted"])
        self.assertEqual(status["guard"]["reason"], "adapter_fault: SDK heartbeat failed")
        self.assertFalse(transport.commands or transport.postures or transport.arms)

    def test_one_enable_allows_repeated_bursts_and_public_stop_retains_permission(self):
        transport = FakeTransport(ready=False)
        transport.can_arm = True
        controller = MotionController(transport)
        self.assertEqual(controller.enable()["status"], "enabled")
        for _ in range(2):
            result = controller.execute(SimpleNamespace(linear_x=.02,duration_sec=.02))
            self.assertEqual(result["status"], "completed")
            self.assertTrue(transport.session_enabled)
        self.assertTrue(controller.stop()["stop_acknowledged"])
        self.assertTrue(transport.session_enabled)
        self.assertEqual(controller.enable()["status"], "enabled")  # idempotent
        self.assertEqual(transport.arms, 1)
        controller.close()
        self.assertFalse(transport.session_enabled)

    def test_arming_is_explicit_and_never_emits_a_velocity_or_posture(self):
        transport = FakeTransport(ready=False)
        transport.can_arm = True
        controller = MotionController(transport)
        self.assertEqual(transport.arms, 0)
        self.assertEqual(controller.arm()["status"], "armed")
        self.assertEqual(transport.arms, 1)
        self.assertEqual(transport.commands, [])
        self.assertEqual(transport.postures, [])

    def test_cancelled_arm_stops_and_default_arm_is_disabled(self):
        self.assertEqual(MotionController().arm()["error"], "motion_disabled")
        transport = FakeTransport(ready=False)
        transport.can_arm = True
        result = MotionController(transport).arm(is_active=lambda: False)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["stop_acknowledged"])

    def test_pose_ready_mode_cannot_emit_velocity(self):
        transport = FakeTransport()
        transport.movement_ready = False
        result = MotionController(transport).execute(
            SimpleNamespace(linear_x=.02, duration_sec=.2))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(transport.commands, [])

    def test_postures_dispatch_only_explicitly_and_do_not_claim_completion(self):
        for action in ("stand_up", "stand_down"):
            transport = FakeTransport()
            controller = MotionController(transport, Limits(posture_duration_s=0.08))
            self.assertEqual(transport.postures, [])
            result = controller.posture(action)
            self.assertEqual(result["status"], "dispatched")
            self.assertFalse(result["physical_completion_verified"])
            self.assertEqual(transport.postures, [action])
            self.assertGreater(transport.posture_holds, 0)
            self.assertEqual(transport.commands, [])
            controller.close()
            self.assertEqual(transport.postures, [action])

    def test_move_and_posture_share_exclusive_ownership_and_cancel(self):
        transport = FakeTransport()
        controller = MotionController(transport, Limits(posture_duration_s=0.5))
        results = []
        thread = threading.Thread(target=lambda: results.append(controller.posture("stand_down")))
        thread.start()
        self.assertTrue(transport.first_command.wait(timeout=1))
        self.assertEqual(controller.execute(SimpleNamespace())["status"], "rejected")
        controller.stop()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertEqual(transport.commands, [])

    def test_disabled_controller_rejects_both_postures_without_transport(self):
        controller = MotionController()
        for action in ("stand_up", "stand_down"):
            self.assertEqual(controller.posture(action)["error"], "motion_disabled")

    def test_disabled_controller_never_constructs_or_calls_a_transport(self):
        controller = MotionController()
        self.assertEqual(controller.execute(SimpleNamespace(linear_x=0.1))["error"],
                         "motion_disabled")
        self.assertFalse(controller.stop()["stop_acknowledged"])
        self.assertFalse(controller.status()["motion_enabled"])

    def test_burst_always_stops_and_does_not_claim_measured_distance(self):
        transport = FakeTransport()
        controller = MotionController(transport)
        result = controller.execute(SimpleNamespace(linear_x=0.04, duration_sec=0.08))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["stop_acknowledged"])
        self.assertFalse(result["distance_verified"])
        self.assertGreaterEqual(len(transport.commands), 1)
        self.assertEqual(len(transport.stops), 1)

    def test_invalid_request_does_not_send_or_stop_another_controller(self):
        transport = FakeTransport()
        result = MotionController(transport).execute(SimpleNamespace(linear_x=1))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(transport.commands, [])
        self.assertEqual(transport.stops, [])

    def test_not_armed_does_not_send_a_command(self):
        transport = FakeTransport(ready=False)
        result = MotionController(transport).execute(SimpleNamespace(linear_x=0.05))
        self.assertEqual(result["error"], "guard_not_armed")
        self.assertEqual(transport.commands, [])

    def test_guard_failure_requests_stop_and_latches_the_controller(self):
        transport = FakeTransport(fail_after=1)
        controller = MotionController(transport)
        result = controller.execute(SimpleNamespace(linear_x=0.05, duration_sec=0.2))
        self.assertEqual(result["status"], "fault")
        self.assertTrue(result["stop_acknowledged"])
        self.assertTrue(controller.status()["faulted"])
        self.assertEqual(controller.execute(SimpleNamespace())["status"], "rejected")

    def test_stop_ack_failure_is_never_reported_as_success(self):
        transport = FakeTransport(stop_ok=False)
        result = MotionController(transport).execute(
            SimpleNamespace(linear_x=0.05, duration_sec=0.01))
        self.assertEqual(result["status"], "stop_unconfirmed")
        self.assertFalse(result["stop_acknowledged"])

    def test_guard_loss_at_the_end_is_not_reported_as_completed(self):
        transport = FakeTransport()
        with mock.patch.object(transport, "status", side_effect=[
                {"ready": True}, {"ready": False, "reason": "source_stale"}]):
            result = MotionController(transport).execute(
                SimpleNamespace(linear_x=0.05, duration_sec=0.01))
        self.assertEqual(result["status"], "fault")
        self.assertEqual(result["error"], "source_stale")
        self.assertTrue(result["stop_acknowledged"])

    def test_cancelled_grpc_request_stops_after_its_first_command(self):
        transport = FakeTransport()
        result = MotionController(transport).execute(
            SimpleNamespace(linear_x=0.05, duration_sec=0.2),
            is_active=lambda: not transport.first_command.is_set(),
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(transport.commands), 1)
        self.assertTrue(transport.stops)
        self.assertFalse(transport.session_enabled)

    def test_disconnect_during_terminal_status_also_revokes_permission(self):
        transport = FakeTransport()
        active = [True]
        calls = [0]
        def observed_status():
            calls[0] += 1
            if calls[0] == 2:  # disconnect arrives after the final velocity packet
                active[0] = False
            return {"ready": True, "session_enabled": True}
        with mock.patch.object(transport, "status", side_effect=observed_status):
            result = MotionController(transport).execute(
                SimpleNamespace(linear_x=.02,duration_sec=.01),
                is_active=lambda: active[0])
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(transport.session_enabled)

    def test_explicit_stop_cancels_without_later_velocity_commands(self):
        transport = FakeTransport()
        controller = MotionController(transport)
        results = []
        thread = threading.Thread(target=lambda: results.append(
            controller.execute(SimpleNamespace(linear_x=0.05, duration_sec=0.5))))
        thread.start()
        self.assertTrue(transport.first_command.wait(timeout=1))
        controller.stop()
        stopped_at = time.monotonic()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertTrue(all(command[0] <= stopped_at for command in transport.commands))

    def test_second_request_cannot_take_over_an_active_move(self):
        transport = FakeTransport()
        controller = MotionController(transport)
        thread = threading.Thread(target=lambda: controller.execute(
            SimpleNamespace(linear_x=0.05, duration_sec=0.5)))
        thread.start()
        self.assertTrue(transport.first_command.wait(timeout=1))
        self.assertEqual(controller.execute(SimpleNamespace())["error"],
                         "another_move_is_active")
        controller.close()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(controller.execute(SimpleNamespace())["status"], "rejected")


class StateWitnessTest(unittest.TestCase):
    def observe(self, witness, stamp=99.95, mode=5, error=0, quaternion=(1, 0, 0, 0),
                now_wall=100, now_mono=10):
        witness.observe(stamp, mode, error, quaternion, [0, 0, 0],
                        now_wall, now_mono)

    def test_accepts_valid_state_but_expires_without_new_source_samples(self):
        witness = SportWitness({5})
        self.observe(witness)
        self.assertTrue(witness.ready(100, 10))
        self.assertFalse(witness.ready(100.3, 10.3))

    def test_zero_old_future_bad_mode_and_bad_quaternion_fail_closed(self):
        for args in [dict(stamp=0), dict(stamp=98), dict(stamp=101), dict(mode=255),
                     dict(error=100), dict(quaternion=(0, 0, 0, 0))]:
            witness = SportWitness({5})
            self.observe(witness, **args)
            self.assertFalse(witness.ready(100, 10), args)

    def test_replayed_timestamp_cannot_refresh_liveness(self):
        witness = SportWitness({5})
        self.observe(witness)
        self.observe(witness, now_wall=100.3, now_mono=10.3)
        self.assertFalse(witness.ready(100.3, 10.3))
        self.assertEqual(witness.received_at, 10)

    def test_regression_latches_even_if_later_packets_are_fresh(self):
        witness = SportWitness({5})
        self.observe(witness)
        self.observe(witness, stamp=99.9)
        self.observe(witness, stamp=100.0)
        self.assertFalse(witness.ready(100, 10))
        self.assertEqual(witness.fault, "source_timestamp_regressed")


class EnablementTest(unittest.TestCase):
    def test_default_or_missing_operator_gates_do_not_touch_network_or_ros(self):
        for config, environ in [({}, {}), ({"allow_motion": True}, {}),
                                ({"allow_motion": True}, {"GO2_ALLOW_MOTION": "true"})]:
            with mock.patch("go2_chassis_motion.transport.subprocess.run",
                            side_effect=AssertionError("No network process should run")):
                with self.assertRaises(ValueError):
                    check_live_prerequisites(config, environ)


if __name__ == "__main__":
    unittest.main()
