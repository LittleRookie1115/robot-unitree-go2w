"""A bounded pipe to the Foxy worker; imported without creating a ROS endpoint."""
import json
import fcntl
import ipaddress
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time
import urllib.request

from .controller import Limits


def check_native_network(interface="eth10", robot_address="192.168.123.161"):
    """Check the onboard route without imposing the reference PC's IP policy.

    Other addresses, Internet routes, DNS and IPv6 on this host are unrelated
    to the selected IPv4 robot route. This function never changes them.
    It verifies reachability configuration, not DDS authentication.
    """
    if not isinstance(interface, str) or not interface or interface.startswith("-"):
        raise ValueError("a native robot network interface must be selected")
    try:
        robot = ipaddress.IPv4Address(robot_address)
    except (ValueError, ipaddress.AddressValueError) as error:
        raise ValueError("robot_address must be an IPv4 address") from error
    if (robot.is_unspecified or robot.is_multicast or robot.is_loopback or
            robot.is_reserved or robot.is_link_local or not robot.is_private):
        raise ValueError("robot_address must be a private unicast IPv4 address")
    result = subprocess.run(
        ["ip", "-j", "-4", "address", "show", "dev", interface],
        capture_output=True, text=True, check=True, timeout=3)
    links = json.loads(result.stdout)
    if len(links) != 1 or links[0].get("ifname") != interface:
        raise ValueError("the selected native robot interface is unavailable")
    link = links[0]
    flags = set(link.get("flags", []))
    if not {"UP", "LOWER_UP"} <= flags or "LOOPBACK" in flags:
        raise ValueError("the selected native robot link is not up")
    addresses = [
        ipaddress.IPv4Interface(f"{entry['local']}/{entry['prefixlen']}")
        for entry in link.get("addr_info", [])
        if entry.get("family") == "inet" and entry.get("scope") == "global"
    ]
    if any(address.ip == robot for address in addresses):
        raise ValueError("robot_address belongs to this host, not the robot controller")
    result = subprocess.run(
        ["ip", "-j", "-4", "route", "get", str(robot)],
        capture_output=True, text=True, check=True, timeout=3)
    routes = json.loads(result.stdout)
    if len(routes) != 1:
        raise ValueError("the robot IPv4 route is ambiguous or unavailable")
    route = routes[0]
    if route.get("dev") != interface or route.get("gateway") or route.get("type", "unicast") != "unicast":
        raise ValueError("the robot must have a direct route on the selected native interface")
    source = route.get("prefsrc", route.get("src"))
    matches = [address for address in addresses if str(address.ip) == source]
    if len(matches) != 1 or robot not in matches[0].network:
        raise ValueError("the robot route source must be assigned on its directly connected subnet")
    if robot in (matches[0].network.network_address, matches[0].network.broadcast_address):
        raise ValueError("robot_address must not be the subnet's network or broadcast address")
    return {"interface": interface, "robot_address": str(robot),
            "local_address": str(matches[0]), "route": route,
            "host_network_changed": False}


def check_live_prerequisites(config, environ):
    if config.get("allow_motion") is not True:
        raise ValueError("allow_motion must be explicitly true")
    if environ.get("GO2_ALLOW_MOTION", "").lower() != "true":
        raise ValueError("GO2_ALLOW_MOTION must be explicitly true")
    if config.get("operator_present") is not True or environ.get("GO2_OPERATOR_PRESENT", "").lower() != "true":
        raise ValueError("the operator-present gates are not enabled")
    ack = "I_UNDERSTAND_GO2_CAN_MOVE"
    if config.get("safety_ack") != ack or environ.get("GO2_SAFETY_ACK") != ack:
        raise ValueError("the independent safety acknowledgements are missing")
    modes = environ.get("GO2_ALLOWED_MODES", "").split(",")
    if not modes or any(not mode.strip().isdigit() or not 0 <= int(mode) <= 254 for mode in modes):
        raise ValueError("GO2_ALLOWED_MODES must list the audited modes")
    if "allowed_modes" in config and set(config["allowed_modes"]) != {int(mode) for mode in modes}:
        raise ValueError("configured native modes must match process modes")
    interface = config.get("network_interface", "eth10")
    # The ROS/SDK launchers currently bind eth10. Reject inconsistent launch
    # configuration, but do not require the reference workstation's address.
    if interface != "eth10":
        raise ValueError("network_interface must match the native ROS/SDK binding, currently eth10")
    check_native_network(interface, config.get("robot_address", "192.168.123.161"))
    basis = config.get("state_time_basis", "payload")
    if basis == "dds-source-lowstate-tick-v1":
        if (environ.get("GO2_STATE_TIME_BASIS") != basis or
                config.get("robot_variant") != "go2w" or
                environ.get("GO2_ROBOT_VARIANT") != "go2w"):
            raise ValueError("the explicit Go2-W state basis and robot variant must agree")
        # Starting transport does not arm it. Original DDS source progress and
        # LowState.tick are qualified continuously in the adapter and SDK,
        # and the worker independently checks state before every operation.
        return
    if basis != "payload":
        raise ValueError("unknown native state time basis")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:18092/status", timeout=2) as response:
        state = json.load(response)
    sport = state.get("sources", {}).get("sport", {})
    if sport.get("source_stamp", 0) <= 0:
        raise ValueError("SportModeState source timestamp is missing; motion remains blocked")
    if time.time() - sport["source_stamp"] > 0.2 or sport["source_stamp"] - time.time() > 0.05:
        raise ValueError("SportModeState source timestamp is stale or in the future")
    if sport.get("mode") not in [int(mode) for mode in modes] or sport.get("error_code") != 0:
        raise ValueError("SportModeState mode/error is not eligible")


class RosTransport:
    backend_owner = "external"

    def __init__(self, root, config, environ=None, limits=None):
        self._lock = threading.Lock()
        self._sequence = 0
        self._buffer = b""
        self.limits = limits or Limits()
        max_vx = max(self.limits.forward_mps, self.limits.reverse_mps)
        environment = os.environ if environ is None else environ
        check_live_prerequisites(config, environment)
        worker = root / "packages/go2w_chassis_motion/go2w_chassis_motion/ros_worker.py"
        self.process = subprocess.Popen(
            ["bash", str(root / "native/ros-python.sh"), str(worker),
             "--allowed-modes", environment["GO2_ALLOWED_MODES"], "--allow-motion",
             "--state-time-basis", config.get("state_time_basis", "payload"),
             "--operator-present", "--safety-ack", "I_UNDERSTAND_GO2_CAN_MOVE",
             "--max-vx", str(max_vx),
             "--max-vy", str(self.limits.lateral_mps),
             "--max-wz", str(self.limits.yaw_rps)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            start_new_session=True, bufsize=0,
        )
        self._selector = selectors.DefaultSelector()
        self._selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.status()
        except Exception:
            self.close()
            raise

    def _request(self, operation, timeout=0.25, **values):
        with self._lock:
            if self.process.poll() is not None:
                raise RuntimeError("the guarded ROS worker has exited")
            self._sequence += 1
            request = dict(id=self._sequence, operation=operation, **values)
            self.process.stdin.write((json.dumps(request, allow_nan=False) + "\n").encode())
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    reply = json.loads(line)
                    if reply.get("id") != self._sequence:
                        raise RuntimeError("the ROS worker replied to an unexpected request")
                    if not reply.get("ok"):
                        raise RuntimeError(reply.get("error", "guarded command rejected"))
                    return reply
                events = self._selector.select(max(0.0, deadline - time.monotonic()))
                if events:
                    data = os.read(self.process.stdout.fileno(), 65536)
                    if not data:
                        raise RuntimeError("the ROS worker pipe closed")
                    self._buffer += data
                    if len(self._buffer) > 65536:
                        raise RuntimeError("the ROS worker response exceeded its size limit")
            raise TimeoutError("the ROS worker response deadline expired")

    def status(self):
        return self._request("status", timeout=3)["status"]

    def send(self, vx, vy, wz):
        return self._request("move", vx=vx, vy=vy, wz=wz)

    def arm(self):
        return self._request("arm", timeout=5)

    def enable(self):
        return self._request("enable", timeout=5)

    def finish(self):
        return self._request("finish", timeout=2)["stop_acknowledged"]

    def finish_posture(self):
        return self._request("finish_posture", timeout=2)["action_finished"]

    def posture(self, action):
        response = self._request("posture", timeout=0.5, action=action)
        if not response.get("dispatch_acknowledged"):
            raise RuntimeError("posture dispatch was not acknowledged")

    def hold_posture(self):
        self._request("posture_hold")

    def stop(self):
        return self._request("stop", timeout=2)["stop_acknowledged"]

    def close(self):
        if getattr(self, "process", None) is not None and self.process.poll() is None:
            self.process.stdin.close()  # EOF independently requests stop/disarm in the worker.
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=2)
        if hasattr(self, "_selector"):
            self._selector.close()


class NativeRuntimeTransport(RosTransport):
    """Own the disarmed SDK/adapter lazily, only after a human enable RPC."""
    backend_owner = "primitive"

    def __init__(self, root, config, environ=None, limits=None):
        self._children = []
        self._owner = None
        self._log_offsets = {}
        self.limits = limits or Limits()
        max_vx = max(self.limits.forward_mps, self.limits.reverse_mps)
        environment = dict(os.environ if environ is None else environ)
        settings = dict(config, allow_motion=True, operator_present=True,
                        safety_ack="I_UNDERSTAND_GO2_CAN_MOVE")
        modes = ",".join(str(value) for value in settings["allowed_modes"])
        environment.update(
            GO2_ALLOW_MOTION="true", GO2_OPERATOR_PRESENT="true",
            GO2_SAFETY_ACK=settings["safety_ack"], GO2_ALLOWED_MODES=modes,
            GO2_STATE_TIME_BASIS=settings["state_time_basis"], GO2_ROBOT_VARIANT="go2w")
        check_live_prerequisites(settings, environment)
        build = root / "rbnx-build/native"
        sdk = build / "chassis-sdk/install/bin/go2_sport_daemon"
        adapter = (build / "chassis-ros/install/lib/go2_native_chassis_adapter/"
                   "go2_native_chassis_adapter_node")
        if not all(path.is_file() and os.access(path, os.X_OK) for path in (sdk, adapter)):
            raise RuntimeError("Build the native chassis backend with native/build-motion.sh first")
        runtime = build / "primitive-chassis"
        runtime.mkdir(parents=True, exist_ok=True)
        socket = Path(f"/run/user/{os.getuid()}/go2w-native/sport.sock")
        socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._owner = (runtime / "owner.lock").open("a")
            fcntl.flock(self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            sdk_env = dict(environment, LD_LIBRARY_PATH=str(build / "chassis-sdk/install/lib"))
            self._spawn(runtime / "sdk.log", [
                str(sdk), "--socket", str(socket), "--allow-motion",
                "--interface", settings.get("network_interface", "eth10"),
                "--motion-ack", "GO2_PHYSICAL_MOTION_APPROVED",
                "--motion-profile", "native-go2w-manual-v2", "--watchdog-ms", "300",
                "--max-vx", str(max_vx),
                "--max-vy", str(self.limits.lateral_mps),
                "--max-wz", str(self.limits.yaw_rps),
                "--max-motion-ms", "5000"], sdk_env, root)
            deadline = time.monotonic() + 6
            while not socket.is_socket():
                if self._children[0].poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("native SDK listener did not start")
                time.sleep(.02)
            gates = [f"{name}={environment[name]}" for name in (
                "GO2_ALLOW_MOTION", "GO2_OPERATOR_PRESENT", "GO2_SAFETY_ACK",
                "GO2_ALLOWED_MODES", "GO2_STATE_TIME_BASIS", "GO2_ROBOT_VARIANT")]
            self._spawn(runtime / "adapter.log", [
                "bash", str(root / "native/ros-exec.sh"), "env", *gates, str(adapter),
                "--ros-args", "-p", "allow_motion:=true",
                "-p", "allowed_modes:=" + json.dumps(settings["allowed_modes"]),
                "-p", "sdk_socket:=" + str(socket),
                "-p", "state_time_basis:=" + settings["state_time_basis"],
                "-p", "max_vx:=" + str(max_vx),
                "-p", "max_vy:=" + str(self.limits.lateral_mps),
                "-p", "max_wz:=" + str(self.limits.yaw_rps)],
                environment, root)
            (runtime / "session.json").write_text(json.dumps({
                "robot_variant": "go2w", "profile": "native-go2w-manual-v2",
                "armed_at_start": False, "postures_at_start": [],
                "pids": {"sdk": self._children[0].pid, "adapter": self._children[1].pid},
                "log_start_bytes": self._log_offsets,
            }, indent=2)+"\n")
            super().__init__(root, settings, environment, limits=self.limits)
            deadline = time.monotonic() + 6
            while True:
                status = self.status()
                if any(child.poll() is not None for child in self._children):
                    raise RuntimeError("a native chassis backend exited during startup")
                if status.get("can_arm") or status.get("ready"):
                    break
                if status.get("faulted") or time.monotonic() >= deadline:
                    raise RuntimeError(status.get("reason") or "native state qualification failed")
                time.sleep(.05)
        except BaseException:
            self.close()
            raise

    def _spawn(self, path, command, environment, cwd):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "ab") as log:
            self._log_offsets[path.stem] = os.fstat(log.fileno()).st_size
            self._children.append(subprocess.Popen(
                command, env=environment, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True))

    def close(self):
        try:
            super().close()
        finally:
            for child in reversed(getattr(self, "_children", [])):
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(child.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        try:
                            child.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(child.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            child.wait(timeout=2)
            self._children = []
            if getattr(self, "_owner", None) is not None:
                self._owner.close()
                self._owner = None
