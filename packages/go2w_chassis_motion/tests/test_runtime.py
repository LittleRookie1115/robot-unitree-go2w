import fcntl
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from go2w_chassis_motion.transport import NativeRuntimeTransport, RosTransport


class RuntimeOwnershipTest(unittest.TestCase):
    def exercise(self, fail_worker=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "rbnx-build/native/primitive-chassis"
            runtime.mkdir(parents=True)
            for relative in (
                "chassis-sdk/install/bin/go2_sport_daemon",
                "chassis-ros/install/lib/go2_native_chassis_adapter/go2_native_chassis_adapter_node",
            ):
                artifact = root / "rbnx-build/native" / relative
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.touch(mode=0o700)
            children = []
            for pid in (20_000_001, 20_000_002):
                child = mock.Mock(pid=pid)
                child.poll.return_value = None
                def wait(timeout, child=child):
                    child.poll.return_value = 0
                    return 0
                child.wait.side_effect = wait
                children.append(child)
            config = dict(allowed_modes=[1, 3, 5, 6], robot_variant="go2w",
                          state_time_basis="dds-source-lowstate-tick-v1")
            # No real SDK/ROS process, network query or /run mutation is allowed.
            with (mock.patch("go2w_chassis_motion.transport.check_live_prerequisites"),
                  mock.patch("go2w_chassis_motion.transport.subprocess.Popen",
                             side_effect=children) as spawn,
                  mock.patch("go2w_chassis_motion.transport.os.killpg") as terminate,
                  mock.patch.object(Path, "mkdir"),
                  mock.patch.object(Path, "is_socket", return_value=True),
                  mock.patch.object(RosTransport, "__init__",
                                    side_effect=RuntimeError("fixture worker failure")
                                    if fail_worker else None),
                  mock.patch.object(RosTransport, "status", return_value={"can_arm": True})):
                if fail_worker:
                    with self.assertRaisesRegex(RuntimeError, "fixture worker failure"):
                        NativeRuntimeTransport(root, config, {})
                else:
                    backend = NativeRuntimeTransport(root, config, {})
                    self.assertEqual(spawn.call_count, 2)
                    terminate.assert_not_called()
                    backend.close()
                    backend.close()
                self.assertEqual(terminate.call_args_list, [
                    mock.call(children[1].pid, signal.SIGINT),
                    mock.call(children[0].pid, signal.SIGINT),
                ])
                self.assertTrue(all(child.poll() == 0 for child in children))
                with (runtime / "owner.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_disable_reaps_only_owned_children_and_releases_lock(self):
        self.exercise()

    def test_partial_startup_failure_reaps_children_and_releases_lock(self):
        self.exercise(fail_worker=True)
