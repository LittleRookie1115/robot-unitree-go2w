import json
import os
from pathlib import Path
import selectors
import subprocess
import time
import unittest


class WorkerDiagnosticTest(unittest.TestCase):
    def test_backend_fault_and_original_reason_are_reported_without_a_command(self):
        result = subprocess.run(
            ["/usr/bin/python3", str(Path(__file__).with_name("fake_ros_worker.py"))],
            input=json.dumps({"id": 1, "operation": "status"}) + "\n",
            capture_output=True, text=True, timeout=3,
            env=dict(os.environ, GO2_TEST_START_DISARMED="1", GO2_TEST_GUARD_FAULT="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout.splitlines()[0])["status"]
        self.assertTrue(status["faulted"])
        self.assertFalse(status["ready"])
        self.assertEqual(status["adapter_reason"], "SDK heartbeat failed")
        events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
        self.assertFalse(any(e["event"] in ("arm", "posture") for e in events))


class WorkerProcessTest(unittest.TestCase):
    def setUp(self):
        self.process = subprocess.Popen(
            ["/usr/bin/python3", str(Path(__file__).with_name("fake_ros_worker.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            env=dict(os.environ, GO2_TEST_START_DISARMED="1"),
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.sequence = 0
        # The worker starts its independent spin thread before reading requests.
        self.assertFalse(self.request("status")["status"]["ready"])
        end = time.monotonic()+2
        while not self.request("status")["status"]["can_arm"]:
            self.assertLess(time.monotonic(), end)
            time.sleep(.01)
        self.assertTrue(self.request("enable")["session_enabled"])

    def test_one_enable_allows_multiple_actions_and_stops_without_rearming(self):
        for _ in range(2):
            self.assertTrue(self.request("move",vx=.02,vy=0,wz=0)["ok"])
            self.assertTrue(self.request("finish")["stop_acknowledged"])
            status=self.request("status")["status"]
            self.assertTrue(status["ready"] and status["session_enabled"])
            self.assertIsNone(status["idle_timeout_sec"])
            time.sleep(.2)
        self.assertTrue(self.request("enable")["session_enabled"])
        events=self.finish()
        self.assertEqual(len([e for e in events if e["event"]=="arm"]),1)
        self.assertEqual(len([e for e in events if e["event"]=="finish"]),2)

    def test_posture_completion_preserves_enable_without_velocity_stop(self):
        self.assertTrue(self.request("posture", action="stand_up")["ok"])
        self.assertTrue(self.request("finish_posture")["action_finished"])
        self.assertTrue(self.request("status")["status"]["session_enabled"])
        events = self.finish()
        self.assertEqual(len([e for e in events if e["event"] == "finish_posture"]), 1)
        self.assertFalse(any(e["event"] == "finish" for e in events))

    def test_sdk_dispatch_evidence_does_not_invent_physical_movement(self):
        self.assertTrue(self.request("move", vx=.05, vy=0, wz=0)["ok"])
        time.sleep(.05)
        self.assertTrue(self.request("finish")["stop_acknowledged"])
        status = self.request("status")["status"]
        self.assertGreater(status["dispatch"]["move_dispatch_count"], 0)
        self.assertGreater(status["motion_observation"]["samples"], 0)
        self.assertFalse(status["motion_observation"]["motion_observed"])

    def test_posture_rejection_preserves_response_detail_and_revokes_permission(self):
        self.process.stdin.close()
        self.process.wait(timeout=3)
        self.process.stdout.close()
        self.process.stderr.close()
        self.selector.close()
        self.process = subprocess.Popen(
            ["/usr/bin/python3", str(Path(__file__).with_name("fake_ros_worker.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            env=dict(os.environ, GO2_TEST_START_DISARMED="1", GO2_TEST_POSTURE_CODE="-1"))
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        end = time.monotonic()+2
        while not self.request("status")["status"]["can_arm"]:
            self.assertLess(time.monotonic(), end)
            time.sleep(.01)
        self.assertTrue(self.request("enable")["session_enabled"])
        failed = self.request("posture", action="stand_down")
        self.assertFalse(failed["ok"])
        self.assertIn("SDK command failed with code -1", failed["error"])
        self.assertNotIn("not acknowledged", failed["error"])
        time.sleep(.03)
        status = self.request("status")["status"]
        self.assertFalse(status["session_enabled"])
        self.assertTrue(status["faulted"])
        self.assertEqual(status["dispatch"]["last_posture_code"], -1)
        self.assertTrue(status["dispatch"]["last_posture_reply_received"])
        events = self.finish()
        self.assertEqual(len([e for e in events if e["event"] == "posture"]), 1)
        self.assertFalse(any(e["event"] == "publish" and
                             (e["vx"] or e["vy"] or e["wz"]) for e in events))

    def request(self, operation, **values):
        self.sequence += 1
        self.process.stdin.write(json.dumps(
            {"id": self.sequence, "operation": operation, **values}) + "\n")
        self.process.stdin.flush()
        self.assertTrue(self.selector.select(timeout=2), "Worker reply timed out")
        reply = json.loads(self.process.stdout.readline())
        self.assertEqual(reply["id"], self.sequence)
        return reply

    def finish(self):
        self.process.stdin.close()
        self.process.wait(timeout=3)
        errors = self.process.stderr.read()
        self.assertEqual(self.process.returncode, 0, errors)
        return [json.loads(line) for line in errors.splitlines() if line.startswith("{")]

    def tearDown(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not pipe.closed:
                pipe.close()
        self.selector.close()

    def test_pipe_silence_expires_lease_stops_and_refuses_more_movement(self):
        self.assertTrue(self.request("move", vx=0.05, vy=0, wz=0)["ok"])
        time.sleep(0.25)
        status = self.request("status")["status"]
        self.assertFalse(status["ready"])
        self.assertEqual(status["reason"], "command_lease_expired")
        self.assertFalse(self.request("move", vx=0.05, vy=0, wz=0)["ok"])
        events = self.finish()
        moving = [e for e in events if e["event"] == "publish" and e["vx"] != 0]
        self.assertEqual(len(moving), 1)
        self.assertTrue(any(e["event"] == "disarm" and e["data"] is False for e in events))

    def test_disconnect_while_active_requests_zero_and_disarm(self):
        self.assertTrue(self.request("move", vx=0.05, vy=0, wz=0)["ok"])
        events = self.finish()
        published = [e for e in events if e["event"] == "publish"]
        self.assertEqual(published[-1]["vx"], 0)
        self.assertEqual(published[-1]["wz"], 0)
        self.assertTrue(any(e["event"] == "disarm" for e in events))

    def test_malformed_active_command_faults_and_stops(self):
        self.assertTrue(self.request("move", vx=0.05, vy=0, wz=0)["ok"])
        self.assertFalse(self.request("move", vx=1, vy=0, wz=0)["ok"])
        self.assertFalse(self.request("status")["status"]["ready"])
        events = self.finish()
        self.assertEqual(len([e for e in events if e["event"] == "publish" and e["vx"] != 0]), 1)
        self.assertTrue(any(e["event"] == "disarm" for e in events))

    def test_signed_velocity_is_preserved_to_the_guarded_adapter(self):
        self.assertTrue(self.request("move", vx=-0.03, vy=0.02, wz=-0.1)["ok"])
        self.request("stop")
        events = self.finish()
        self.assertTrue(any(e["event"] == "publish" and
                            (e["vx"], e["vy"], e["wz"]) == (-0.03, 0.02, -0.1)
                            for e in events))

    def test_startup_and_shutdown_never_dispatch_a_posture(self):
        events = self.finish()
        self.assertFalse(any(e["event"] == "posture" for e in events))
        self.assertEqual(len([e for e in events if e["event"] == "arm"]),1)

    def test_process_start_alone_does_not_enable_or_dispatch(self):
        result=subprocess.run(
            ["/usr/bin/python3",str(Path(__file__).with_name("fake_ros_worker.py"))],
            input=json.dumps({"id":1,"operation":"status"})+"\n",
            capture_output=True,text=True,timeout=3,
            env=dict(os.environ,GO2_TEST_START_DISARMED="1"))
        self.assertEqual(result.returncode,0,result.stderr)
        reply=json.loads(result.stdout.splitlines()[0])
        self.assertFalse(reply["status"]["session_enabled"])
        events=[json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
        self.assertFalse(any(e["event"] in ("arm","posture") for e in events))

    def test_posture_dispatch_is_explicit_and_cannot_overlap_velocity(self):
        self.assertTrue(self.request("posture", action="stand_down")["dispatch_acknowledged"])
        self.assertTrue(self.request("posture_hold")["ok"])
        self.assertFalse(self.request("move", vx=0.01, vy=0, wz=0)["ok"])
        events = self.finish()
        self.assertEqual([e["service"] for e in events if e["event"] == "posture"],
                         ["/go2_chassis/stand_down"])
        self.assertFalse(any(e["event"] == "publish" and
                             (e["vx"] or e["vy"] or e["wz"]) for e in events))

    def test_stand_up_uses_its_own_explicit_service(self):
        self.assertTrue(self.request("posture", action="stand_up")["dispatch_acknowledged"])
        self.request("stop")
        events = self.finish()
        self.assertEqual([e["service"] for e in events if e["event"] == "posture"],
                         ["/go2_chassis/stand_up"])

    def test_posture_pipe_lease_expires_independently(self):
        self.assertTrue(self.request("posture", action="stand_up")["ok"])
        time.sleep(0.25)
        self.assertEqual(self.request("status")["status"]["reason"], "command_lease_expired")
        events = self.finish()
        self.assertTrue(any(e["event"] == "disarm" for e in events))


class ExplicitNativeArmTest(WorkerProcessTest):
    def setUp(self):
        self.process = subprocess.Popen(
            ["/usr/bin/python3", str(Path(__file__).with_name("fake_ros_worker.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=dict(os.environ, GO2_TEST_NATIVE="1",
                                         GO2_TEST_START_DISARMED="1"))
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.sequence = 0
        end = time.monotonic()+3
        while not self.request("status")["status"]["can_arm"]:
            self.assertLess(time.monotonic(), end)
            time.sleep(.02)
        self.assertTrue(self.request("enable")["session_enabled"])

    def test_explicit_arm_only_publishes_zero_and_stop_disarms(self):
        self.assertTrue(self.request("status")["status"]["ready"])
        self.assertTrue(self.request("stop")["stop_acknowledged"])
        self.assertFalse(self.request("status")["status"]["ready"])
        events = self.finish()
        self.assertEqual(len([e for e in events if e["event"] == "arm"]),1)
        self.assertFalse(any(e["event"] == "posture" for e in events))
        self.assertFalse(any(e["event"] == "publish" and
                             (e["vx"] or e["vy"] or e["wz"]) for e in events))

    def test_startup_and_shutdown_never_dispatch_a_posture(self):
        # This subclass explicitly armed in setUp, but still never dispatched
        # a posture. The parent class separately proves startup never arms.
        events = self.finish()
        self.assertFalse(any(e["event"] == "posture" for e in events))
