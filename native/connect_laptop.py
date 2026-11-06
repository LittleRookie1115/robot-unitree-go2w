#!/usr/bin/env python3
"""Run on the laptop: one OpenSSH tunnel plus a laptop-local Robonix Client."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time


REMOTE_PYTHON = "/home/unitree/robonix_ws/.tools/rbnx-python/bin/python"
DEFAULT_ROOT = "/home/unitree/robonix_ws/robot-unitree-go2-native"
AUDIO_PORTS = (60000, 60001)


class LaunchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Discovery:
    atlas_port: int
    ports: tuple[int, ...]
    audio_bridge_provider: str
    speech_provider: str
    audio_reverse_port: int = 60002


@dataclass
class Plan:
    ssh: list[str]
    client: list[str]
    forwards: list[tuple[int, int]]
    settings: dict
    local_ports: tuple[int, ...]
    audio_ports: tuple[int, int] = AUDIO_PORTS


def port_number(value):
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("端口必须是 1..65535 的整数") from None
    if isinstance(value, bool) or not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("端口必须是 1..65535 的整数")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("target", help="SSH 目标，例如 unitree@机器狗的实际地址")
    result.add_argument("--ssh-port", type=port_number, default=22)
    result.add_argument("--client-port", type=port_number, default=7860)
    result.add_argument("--audio-port", type=port_number, default=AUDIO_PORTS[0],
                        help="笔记本音频 WebSocket 端口，诊断页面使用此端口加 1")
    result.add_argument("--atlas-port", type=port_number, default=15051,
                        help="笔记本本地 Atlas 转发端口，默认 15051")
    result.add_argument("--remote-root", default=DEFAULT_ROOT)
    result.add_argument("--client-command", default="robonix-client",
                        help="Client 可执行文件的路径，作为一个参数传入，不是 shell 命令")
    result.add_argument("--print-only", action="store_true",
                        help="仅通过 SSH 查询端口并显示计划，不启动隧道和 Client")
    return result


def validate_args(args):
    if not re.fullmatch(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_\[][A-Za-z0-9_.:%\[\]-]*",
                        args.target):
        raise LaunchError("SSH 目标必须为 host 或 user@host，不接受选项、空格或 shell 表达式")
    if not args.remote_root.startswith("/") or any(
            ord(character) < 32 for character in args.remote_root):
        raise LaunchError("--remote-root 必须是机器人上的绝对路径")


def ssh_base(args, executable):
    # No shared master/socket: terminating this launcher only closes its own SSH.
    # Authentication and unknown-host confirmation stay interactive in OpenSSH.
    return [executable, "-T", "-p", str(args.ssh_port),
            "-o", "StrictHostKeyChecking=ask", "-o", "ControlMaster=no",
            "-o", "ControlPath=none", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3"]


def decode_discovery(payload):
    if not isinstance(payload, dict):
        raise LaunchError("远端端口信息必须是 JSON 对象")
    atlas = payload.get("atlas_port")
    ports = payload.get("ports")
    reverse_port = payload.get("audio_reverse_port", 60002)
    if type(atlas) is not int or not 1 <= atlas <= 65535:
        raise LaunchError("远端 atlas_port 无效")
    if not isinstance(ports, list) or not ports or any(
            type(port) is not int or not 1 <= port <= 65535 for port in ports):
        raise LaunchError("远端 ports 必须是有效整数端口列表")
    if type(reverse_port) is not int or not 1 <= reverse_port <= 65535:
        raise LaunchError("远端 audio_reverse_port 无效")
    if not {atlas, reverse_port}.issubset(ports):
        raise LaunchError(f"远端缺少 Atlas 或音频桥 {reverse_port} 端口；请检查机器人部署")
    identifiers = [payload.get("audio_bridge_provider"), payload.get("speech_provider")]
    if any(not isinstance(value, str) or
           not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_./:-]*", value) for value in identifiers):
        raise LaunchError("远端音频桥或 speech provider 标识无效")
    return Discovery(atlas, tuple(sorted(set(ports))), *identifiers, reverse_port)


def query_robot(args, ssh_executable):
    helper = str(PurePosixPath(args.remote_root) / "native" / "voice_tunnel_info.py")
    command = ssh_base(args, ssh_executable) + [
        "--", args.target, shlex.join([REMOTE_PYTHON, helper])]
    try:
        completed = subprocess.run(
            command, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            timeout=120, check=False)
    except subprocess.TimeoutExpired as error:
        raise LaunchError("SSH 端口查询超时；检查地址、网络或交互认证") from error
    if completed.returncode != 0:
        raise LaunchError(f"SSH 端口查询失败（退出码 {completed.returncode}）；请检查 Core 和只读查询脚本")
    try:
        payload = json.loads(completed.stdout)
    except (ValueError, TypeError) as error:
        raise LaunchError("voice_tunnel_info.py 未返回有效 JSON") from error
    return decode_discovery(payload)


def make_plan(args, discovery, ssh_executable, client_executable):
    if args.audio_port == 65535:
        raise LaunchError("--audio-port 最大为 65534；诊断页面还需要下一个端口")
    audio_ports = (args.audio_port, args.audio_port + 1)
    forwards = [(args.atlas_port if port == discovery.atlas_port else port, port)
                for port in discovery.ports]
    local_ports = [local for local, _remote in forwards] + [args.client_port, *audio_ports]
    if len(set(local_ports)) != len(local_ports):
        raise LaunchError("本地端口规划冲突；请调整 --atlas-port/--client-port/--audio-port")
    tunnel = ssh_base(args, ssh_executable) + ["-N", "-o", "ExitOnForwardFailure=yes"]
    for local, remote in forwards:
        tunnel += ["-L", f"127.0.0.1:{local}:127.0.0.1:{remote}"]
    tunnel += ["--", args.target]
    client = [client_executable, "--host", "127.0.0.1", "--port", str(args.client_port),
              "--robot-host", "127.0.0.1", "--atlas-port", str(args.atlas_port),
              "--mic-node-id", discovery.audio_bridge_provider,
              "--speaker-node-id", discovery.audio_bridge_provider,
              "--tts-node-id", discovery.speech_provider,
              "--audio-server-host", "127.0.0.1", "--audio-server-bind-host", "127.0.0.1",
              "--audio-server-port", str(args.audio_port), "--audio-server-ui-host", "127.0.0.1"]
    # Current Client precedence: persisted settings > browser settings > CLI.
    # A private temporary profile wins over stale browser routes, without
    # opening, changing or copying the user's existing settings file.
    settings = {
        "robotHost": "127.0.0.1", "atlasPort": args.atlas_port, "liaisonEndpoint": "",
        "micNodeId": discovery.audio_bridge_provider, "micDeviceId": "",
        "speakerNodeId": discovery.audio_bridge_provider, "speakerDeviceId": "",
        "ttsNodeId": discovery.speech_provider,
    }
    return Plan(tunnel, client, forwards, settings, tuple(sorted(local_ports)), audio_ports)


def client_environment(base, settings_path):
    environment = dict(base)
    environment["ROBONIX_CLIENT_SETTINGS"] = str(settings_path)
    environment["ROBONIX_LIAISON_ENDPOINT"] = ""
    environment["ROBONIX_CLIENT_REVERSE_AUDIO"] = "1"
    return environment


def check_free_ports(ports):
    for port in ports:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows exclusive bind
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", port))
        except OSError as error:
            raise LaunchError(
                f"笔记本端口 127.0.0.1:{port} 不可用；请自行关闭占用程序或调整端口。"
                f"系统原因：{error}。启动器不会终止已有进程。") from error
        finally:
            listener.close()


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def wait_listeners(process, ports, timeout, label, peer=None):
    deadline = time.monotonic() + timeout
    pending = set(ports)
    while pending:
        if process.poll() is not None or (peer is not None and peer.poll() is not None):
            raise LaunchError(f"{label} 在就绪前退出，请查看终端错误")
        pending = {port for port in pending if not listening(port)}
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise LaunchError(f"{label} 等待端口超时：{sorted(pending)}")
        time.sleep(0.1)


def stop_owned(process, graceful=False):
    if process is None or process.poll() is not None:
        return
    # Let Uvicorn stop the audio child it owns, including on Windows. If it
    # cannot finish, escalate only through this Popen's PID, never by port/name.
    first = (lambda: process.send_signal(
        signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)) if graceful else process.terminate
    for action, timeout in ((first, 8), (process.terminate, 3), (process.kill, 3)):
        if process.poll() is not None:
            return
        try:
            action()
            process.wait(timeout=timeout)
            return
        except ProcessLookupError:
            return
        except (OSError, subprocess.TimeoutExpired):
            continue
    print("本次子进程未能在期限内退出，请检查本终端的进程状态。", file=sys.stderr)


def run_session(plan):
    tunnel = client = None
    try:
        check_free_ports(plan.local_ports)
        tunnel = subprocess.Popen(plan.ssh, shell=False)
        wait_listeners(tunnel, [local for local, _remote in plan.forwards], 120, "SSH 隧道")
        # Password entry may have taken time: recheck before Client's audio
        # autostart, which must not take over an existing local audio listener.
        client_port = int(plan.client[plan.client.index("--port") + 1])
        check_free_ports((client_port, *plan.audio_ports))
        with tempfile.TemporaryDirectory(prefix="robonix-laptop-") as directory:
            profile = Path(directory) / "settings.yaml"
            profile.write_text(json.dumps(plan.settings, ensure_ascii=False), encoding="utf-8")
            options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                       if os.name == "nt" else {"start_new_session": True})
            client = subprocess.Popen(
                plan.client, env=client_environment(os.environ, profile), shell=False, **options)
            try:
                wait_listeners(client, [client_port], 60, "本地 Client", peer=tunnel)
                print(f"\n本地 Client 已就绪：http://127.0.0.1:{client_port}/\n"
                      f"笔记本本地音频端口：{plan.audio_ports[0]}，诊断页面：{plan.audio_ports[1]}\n"
                      "音频路由：输入/输出 audio_client_bridge，TTS speech；Ctrl+C 结束。\n"
                      "机器人 Core 重启后请重新运行启动器以更新动态端口。", flush=True)
                while True:
                    if tunnel.poll() is not None:
                        raise LaunchError("SSH 隧道已退出；关闭本次 Client 后请重新运行启动器")
                    code = client.poll()
                    if code is not None:
                        if code:
                            raise LaunchError(f"本地 Client 已退出（{code}）")
                        return 0
                    time.sleep(0.2)
            finally:
                # Stop before deleting its settings profile.
                stop_owned(client, graceful=True)
                client = None
    finally:
        stop_owned(client, graceful=True)
        stop_owned(tunnel)


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise LaunchError("请使用 Python 3.11 或更新版本")
        validate_args(arguments)
        ssh = shutil.which("ssh")
        if ssh is None:
            raise LaunchError("未找到 OpenSSH ssh；请安装 SSH 客户端并加入 PATH")
        client = shutil.which(arguments.client_command)
        if client is None and not arguments.print_only:
            raise LaunchError("未找到 robonix-client；请激活已安装 .[audio] 的环境，或指定 --client-command")
        discovery = query_robot(arguments, ssh)
        plan = make_plan(arguments, discovery, ssh, client or arguments.client_command)
        if arguments.print_only:
            print(json.dumps({"ssh": plan.ssh, "client": plan.client,
                              "forwards": plan.forwards, "settings": plan.settings},
                             indent=2, ensure_ascii=False))
            return 0
        return run_session(plan)
    except KeyboardInterrupt:
        print("\n已结束本次连接。", file=sys.stderr)
        return 130
    except (LaunchError, OSError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
