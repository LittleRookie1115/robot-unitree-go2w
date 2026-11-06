# 笔记本本地 Client 通过 SSH 使用 Go2-W 语音

这套连接把 **Robonix Client 和麦克风/扬声器服务运行在笔记本上**，机器人运行 Atlas、音频桥、speech、Pilot 等服务。笔记本不需要 Unitree SDK、ROS、模型 API 密钥或运动后端。

机器人上的只读 `native/voice_tunnel_info.py` 必须已部署，Core、`audio_client_bridge` 和 `speech` 必须已就绪。这部分由机器人部署流程处理。

## 0. 机器狗只启动 Core，Windows 运行 Client

这是笔记本语音的推荐模式。在机器人 SSH 终端中执行：

```bash
# 从机器狗本地 Client 切换时，可先停止它
systemctl --user stop robonix-client
systemctl --user start robonix-go2-native
```

Core 仍提供 Speech 和反向音频桥 `60002`，但不会启动机器狗本地 GUI `7860`
或本地音频 `60000/60001`。这些 Client/设备端口由 Windows 自己提供。
原有 `systemctl --user start robonix-go2-native robonix-client` 仍可用于本机
Client 模式，不是笔记本模式的必需命令。

如果 Core 已在前台运行，直接复用；用户服务 inactive 不代表 Core 未运行。
不要再开一份后台 Core。需要从后台切换为前台时，先停止原 Core 用户服务：

```bash
systemctl --user stop robonix-go2-native
bash native/start.sh
```

前台和用户服务都自动加载同一个 `/home/unitree/.config/robonix/env`。
加载配置不会启动模型 API 或建立其 SSH 隧道；笔记本 Client 不需要模型密钥。
可在机器人侧先做不提交任务的检查：

```bash
bash native/check-vlm.sh
```

也支持 `--json`。这是有超时边界的认证 `GET /models`，不输出密钥、不调用
completions 或 Pilot；返回 `0` 表示模型列表返回有效 HTTP 200，且包含配置的模型名；
`2` 表示配置、认证、连接或模型列表检查失败。模型检查警告不会阻止 Core
启动传感器和停止能力。

## 1. 在笔记本安装官方 Client

准备 Python **3.11 或更新版本**、Git 和 OpenSSH 客户端。终端执行 `ssh -V` 应能显示 OpenSSH 版本。Windows 可在“可选功能”中安装 OpenSSH 客户端。

以下安装方式来自官方 `syswonder/robonix-client` 的 README；安装后的仓库内也有完整说明。

Linux / macOS：

```bash
git clone https://github.com/syswonder/robonix-client.git
cd robonix-client
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[audio]"
```

先用 `python3 --version` 确认版本。如果系统默认版本较旧，把创建环境的命令中的 `python3` 换为已安装的 `python3.11` 等。

`audio` extra 包含访问本地音频设备的 sounddevice 依赖。PortAudio 系统库按笔记本系统准备：

- **Ubuntu / Debian**：安装 `libportaudio2` 和 `portaudio19-dev`，并在有真实桌面音频会话的终端运行 Client。
- **macOS**：通过 Homebrew 安装 `portaudio`。首次使用时，允许终端/Python 访问麦克风。
- **Windows**：通常 sounddevice 自带 PortAudio DLL，无需安装 Linux/macOS 的系统库。检查系统麦克风隐私设置，允许桌面应用使用麦克风。

Linux 系统库安装：

```bash
sudo apt install libportaudio2 portaudio19-dev
```

macOS 系统库安装：

```bash
brew install portaudio
```

Windows PowerShell 的环境安装命令：

```powershell
git clone https://github.com/syswonder/robonix-client.git
cd robonix-client
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[audio]"
```

Windows 可以直接使用下面的虚拟环境完整路径启动，无需修改 PowerShell 执行策略。

## 2. 只复制一个启动脚本到笔记本

下面命令都在 **笔记本终端**执行。将 `ROBOT_IP` 替换成笔记本当前能够 SSH 连接的机器狗地址；不要照抄机器人内部网络示例地址。

在刚才的 `robonix-client` 目录里：

```bash
scp unitree@ROBOT_IP:/home/unitree/robonix_ws/robonix-unitree-go2w/native/connect_laptop.py .
```

SSH 使用非默认端口时，复制命令使用大写 `-P`：

```bash
scp -P 2222 unitree@ROBOT_IP:/home/unitree/robonix_ws/robonix-unitree-go2w/native/connect_laptop.py .
```

这个脚本只依赖 Python 标准库，复制单文件即可。测试文件不用复制。

单独复制后，脚本无法从自己的位置知道机器人仓库在哪。启动时必须传入机器人上的目录：

```text
--remote-root /home/unitree/robonix_ws/robonix-unitree-go2w
```

机器人上的查询解释器默认是 `/home/unitree/robonix_ws/.tools/rbnx-python/bin/python`。其他工作区用 `--remote-python` 或环境变量 `GO2W_REMOTE_PYTHON` 指定。

## 3. 启动连接

Linux / macOS 激活上面的虚拟环境后：

```bash
python connect_laptop.py unitree@ROBOT_IP --remote-root /home/unitree/robonix_ws/robonix-unitree-go2w
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe .\connect_laptop.py unitree@ROBOT_IP --remote-root /home/unitree/robonix_ws/robonix-unitree-go2w --client-command .\.venv\Scripts\robonix-client.exe
```

使用已经安装好 Client 的 Windows Conda 环境时，保留同样的启动器用法：

```powershell
python ".\connect_laptop.py" unitree@ROBOT_IP --remote-root /home/unitree/robonix_ws/robonix-unitree-go2w --client-command "$env:CONDA_PREFIX\Scripts\robonix-client.exe"
```

如果旧转发占用本地音频端口，可加 `--audio-port 16000`；页面端口被占用时可加
`--client-port 7861`，完整兼容示例保留在第 6 节。

启动器先通过一次只读 SSH 调用查询机器人端口，再创建 **一个带多个 `-L` 的 SSH 隧道进程**，等待转发监听就绪，最后启动笔记本本地 Client。

SSH 使用正常交互认证，可能分别为查询和隧道连接询问密码；脚本不保存密码。首次连接时按 OpenSSH 提示核对主机指纹，确认后再继续。脚本不会自动接受未知主机。

终端显示就绪后，打开：

```text
http://127.0.0.1:7860/
```

默认端口安排：

这里的 `7860` 是 Windows/笔记本本地页面，不依赖机器人是否监听 `7860`。

| 笔记本端口 | 用途 |
|---|---|
| 7860 | 本地 Client 页面 |
| 15051 → 机器人 50051 | Atlas |
| 发现的其他 gRPC 端口，同号转发 | Liaison、音频桥、speech 等能力接口 |
| 查询返回的 `audio_reverse_port`，同号转发（当前为 60002） | 客户端主动连接的双向音频桥 WebSocket |
| 18092 → 机器人 18092（若已发布） | 原生遥测和资源 |
| 60000、60001（可用 `--audio-port` 改动） | 笔记本本地音频 WebSocket 和诊断页面，不转发到机器人 |

所有转发和本地页面/音频监听均绑定 `127.0.0.1`。机器人服务继续保持回环监听。

## 4. 在页面检查路由并由你验证音频

在设置/连接区确认：

- Robot Host：`127.0.0.1`
- Atlas Port：`15051`
- Liaison Endpoint：留空，由 Atlas 发现

打开 **Audio / 音频**：

1. 输入 provider 选择 `audio_client_bridge`，选择笔记本的麦克风。
2. 输出 provider 同样选择 `audio_client_bridge`，选择笔记本的扬声器或耳机。
3. TTS provider 选择 `speech`。
4. 点击 **Apply Route / 应用路由**。
5. 点击 **Test microphone / 测试麦克风**，由你对麦克风说话并检查输入反馈。
6. 点击 **Test speaker / 测试扬声器**，由你确认声音确实从笔记本所选设备播放。

上述测试成功后，再进行一次只读语音试用：

1. 按 **F2** 开始录音，说“只告诉我当前电池电量，不执行动作”。
2. 说完后**再次按 F2 结束录音并发送**；也可以等待页面设置的录音上限自动结束。
3. 录音流结束后，等待最终识别文本及回答。当前 ASR 使用机器人本地的 **SenseVoice CPU**，不是逐字实时识别；说话时暂时没有文字不代表故障。当前流程也不靠自动静音 VAD 结束录音。

部分笔记本需要按 `Fn+F2`，也可使用页面的录音/结束录音按钮。当前 TTS 使用机器人本地的中文 VITS。免提模式默认关闭，已部署 KWS 模型不表示自动开启唤醒。

本地麦克风权限、选中的物理设备和实际播放效果必须由你现场确认；启动器只检查进程和 TCP 监听就绪。

## 5. 设置优先级和退出

当前 Client 的优先级是：**持久化设置覆盖浏览器设置，浏览器设置覆盖 CLI 默认值**。因此仅传 `--robot-host` 并不能清除之前保存的机器人或音频路由。

启动器为本次连接创建临时专属设置文件，并用 `ROBONIX_CLIENT_SETTINGS` 指向它。它预设 Atlas、音频 provider 和空 Liaison Endpoint，避免旧路由覆盖本次 SSH 配置；不读取或改写原有 Client 设置文件。设备选择在本次连接中保存，结束后临时文件删除，下次可重新选择。

保持这个启动终端开启。按 **Ctrl+C** 结束本次 Client 和 SSH；Client 正常退出时会清理自己启动的本地音频服务。启动器不按端口或进程名称批量终止其他程序。

## 6. 常见情况

- **为什么转发机器人上的 7860 页面不能直接用笔记本麦克风？** 那样 Client 后端仍运行在机器人上，音频设备访问也发生在那里。本方案让 Client 后端和设备服务一起运行在笔记本上。
- **为什么只转发 Atlas 不够？** Atlas 返回实际能力服务地址，Client 随后还要连接动态 gRPC 端口和音频 WebSocket。启动器查询当前端口并全部转发。
- **Core 重启后连接异常**：退出启动器，再重新执行。动态服务端口可能改变，旧隧道不会自动更新。
- **机器狗没有 7860 监听**：推荐的仅 Core 模式本来就不启动机器人 Client；运行 Windows helper 后打开笔记本自己的页面。
- **用户服务 inactive，但机器人端口正常**：可能正在运行前台 Core；复用现有实例，不要重复启动。
- **模型连接 localhost:16667 被拒绝**：这是 Pilot 在机器人侧看到的模型 API 地址，应检查该 listener 或单独的模型 SSH 隧道。Client 的音频隧道不会替你启动模型 API。运行机器人侧 `check-vlm.sh`，不要直接启动 router 抢占端口。
- **HTTP 401 / 404 或 `vlm=?@?`**：401 是认证问题，404 应检查 API 路径；banner 的 `vlm=?@?` 是未展开环境配置的占位摘要，不是 env 未加载的证据。前台与用户服务读取同一 env 文件，以诊断结果为准。
- **端口被占用**：启动器会报出具体本地端口并退出，不会杀进程。自行关闭占用程序。页面端口可以改为 `--client-port 7861`；本地 Atlas 可以改为 `--atlas-port 15052`。发现的其他服务端口必须同号转发，不能随意改号。
- **音频端口 60000/60001 被占用**：占用者也可能是旧 SSH 转发，它同样需要监听笔记本的本地端口。可以正常关闭旧连接/程序，或用 `--audio-port 16000` 让本地音频使用 16000、诊断页面使用 16001。这个选项不改机器人音频桥的端口；后者仍由 Atlas 发现并转发。
- **Client 命令找不到**：激活安装了音频 extra 的环境，或使用 `--client-command` 指定 Client 可执行文件的完整路径。这个选项接收一个可执行文件路径，不接收整段 shell 命令。
- **想先看转发计划**：加 `--print-only`。它仍会通过 SSH 查询一次机器人信息，但不启动隧道和 Client。
- **SSH 非默认端口**：启动命令加 `--ssh-port 2222`。

已经保留旧网页/音频转发时，可重新复制更新后的启动器，在 Windows Conda 环境中执行：

```powershell
scp unitree@ROBOT_IP:/home/unitree/robonix_ws/robonix-unitree-go2w/native/connect_laptop.py .
python ".\connect_laptop.py" unitree@ROBOT_IP --remote-root /home/unitree/robonix_ws/robonix-unitree-go2w --client-port 7861 --audio-port 16000 --client-command "$env:CONDA_PREFIX\Scripts\robonix-client.exe"
```

打开 `http://127.0.0.1:7861/`。如果旧连接还占用了查询返回的其他转发端口，
需要关闭旧端口转发，让启动器统一管理本次隧道；机器人服务可以继续运行。
