# GO2-W Mini-JEV 真机演示交接

当前视觉任务的风险恢复和 `not_observed` 语义以
`packages/go2w_visual_action/README.md` 为准。下面是 2026-10-30 的交接记录，
其中“风险后一直停止”描述的是当时核验的版本。

更新日期：2026-10-31（Asia/Shanghai）。本文记录 GO2-W 前方风险评估演示的检查结果、现场传感器读数和后续工作入口。各项数据以注明的检查日期为准。

## 现场与运行状态

| 项目 | 当前记录 |
|---|---|
| Orin | `unitree@192.168.123.18` |
| LiDAR | 10-31 直接订阅 `/unitree/slam_lidar/points`，12 秒收到 240 帧；坐标系 `livox_frame`，已保存前三帧完整数据 |
| RGB | 10-31 通过 `go2w_native` 的 `camera/snapshot` MCP 能力读取 JPEG；坐标系 `front_camera`，1920×1080，最近三次图像年龄为 0.166～0.243 秒 |
| Observer | Native Observer 随 `go2w_native` 运行，提供 `127.0.0.1:18092/status` 和 `/cloud` 用于点云诊断 |
| Odom | 10-30 `/dog_odom` 状态为 `invalid_orientation`，四元数为 `[0, 0, 0, 0]`；不能作为有效姿态依据 |
| JEV 服务 | Robonix、Mini-JEV 和 `go2w_visual_action` 已在 Orin 的 tmux 会话运行；`run_step` 持续进行 RGB 概率评估，风险出现后本次任务保持停止 |
| Client | Orin 的 Client 在 `127.0.0.1:7860` 运行；电脑已建立 SSH 本地转发，`http://127.0.0.1:7860/api/defaults` 返回成功 |
| 语音链路 | Atlas `speech` 为 `ACTIVE`，ASR、TTS 后端均已初始化；电脑音频设备服务运行中，Orin 状态为 `external=true`，健康检查通过 |
| 安全配置 | `go2w_visual_action.motion_enabled=false`，`go2w_chassis_motion.allow_motion=false`；视觉概率未提供障碍物距离或经过验证的碰撞保护 |

需要从电脑抽样统计 Observer 点云时，另开终端建立 LiDAR 状态端口转发，再运行统计脚本：

```bash
ssh -F /dev/null -N -L 18092:127.0.0.1:18092 unitree@192.168.123.18
python3 robonix-unitree-go2w/work/lidar-check/analyze_cloud.py
```

电脑上的 Client 页面通过 SSH 转发访问 `http://127.0.0.1:7860/`。转发中断后，在电脑重新运行：

```bash
ssh -F /dev/null -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -fN -L 127.0.0.1:7860:127.0.0.1:7860 unitree@192.168.123.18
```

Orin 上的当前现场操作说明为 `/home/unitree/go2w_demo/ONSITE_RUNBOOK_RGB.md`，交接文档副本为 `/home/unitree/robonix_ws/robonix-unitree-go2w/docs/GO2-W_MINI_JEV_HANDOFF_2026-10-30.md`。

## Client 音频设备服务

电脑已安装 `libportaudio2`。音频设备服务由本机 systemd 用户服务 `robonix-client-audio-device.service` 运行：WebSocket 监听 `127.0.0.1:60000`，设备网页位于 `http://127.0.0.1:60001/`。当前默认输入和输出设备编号均为 `11`，设备枚举共返回 17 项。

电脑重启或用户服务退出后，在电脑终端重新启动服务：

```bash
systemd-run --user --unit=robonix-client-audio-device.service --collect \
  --property=Restart=on-failure --property=RestartSec=2s \
  --property=WorkingDirectory=/home/xiongyingjie/Projects/go2-w-r1-robonix/robonix-client \
  /home/xiongyingjie/Projects/go2-w-r1-robonix/robonix-client/.venv/bin/robonix-client-audio-server \
  --host 127.0.0.1 --port 60000 --ui-host 127.0.0.1 --log info
```

电脑到 Orin 的音频反向 SSH 转发将 Orin 的 `127.0.0.1:60000` 和 `127.0.0.1:60001` 接到电脑本机的相同端口。转发断开后，在电脑终端重新运行：

```bash
ssh -F /dev/null -o BatchMode=yes -o ConnectTimeout=10 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -fN \
  -R 127.0.0.1:60000:127.0.0.1:60000 \
  -R 127.0.0.1:60001:127.0.0.1:60001 \
  unitree@192.168.123.18
```

Client 保持 `ROBONIX_CLIENT_REVERSE_AUDIO=0`，Orin 不启动本地音频服务进程；Orin 的 `/api/audio-server/status` 返回 `running=false`、`external=true`，表示电脑服务已可访问。`/api/audio-server/health?host=127.0.0.1&port=60000` 返回 `ok=true`，输入与输出设备编号均为 `11`。电脑 WebSocket `/health` 返回 `reachable=true`，设备网页返回 HTTP 200。本次检查只枚举设备并查询健康状态，没有执行麦克风采集或扬声器播放。

点云抽样脚本每次读取 8 轮 `/status` 与 `/cloud`。Observer 的 `/cloud` 每帧从约 10,000 个原始点中抽取约 800 个点；数值统计不包含颜色信息。

## 蓝色方块与点云核对

按现场描述，蓝色小方块位于狗头前方约 1 米处。随后按要求沿狗头朝向向外移动 20 厘米，再沿原线朝机器人移回 20 厘米。机器人始终静止。

| 采样位置 | 采样点 | `-X` 区域中位坐标 `(x, y, z)` | `-X` 区域范围 |
|---|---:|---|---|
| 向外移动后 | 8 轮，共 6,744 点 | `(-1.290, -0.019, 0.273) m` | `x=[-1.397,-1.061]`、`y=[-0.447,0.449]`、`z=[-0.090,0.721] m` |
| 移回后 | 8 轮，共 6,712 点 | `(-1.3005, -0.0435, 0.363) m` | `x=[-1.400,-1.116]`、`y=[-0.449,0.427]`、`z=[-0.062,0.778] m` |

移回后 `-X` 区域的汇总中位数没有显示预期约 20 厘米的 X 坐标变化：X 坐标实际变化约为 -1 厘米，两个区域的点范围高度重叠。统计点集没有按物体分组，无法确认其中哪些回波来自方块。此前凳子现场的采样不能用作蓝色方块移动前的基线。

当前 Observer 配置将正 X 作为前向，走廊为 `x=[0,3] m`、`|y|≤0.45 m`、`z=[-0.5,0.8] m`。向外移动后的采样在 `+X` 的 `0.6–1.4 m` 区间为 0 点，移回后的 `-X` 区域有 62 点。抽样结果没有跟随方块移动，不能据此确认坐标变换；后续 B、C、D 位置的完整点云核验见下文。

## JEV 判断和运动门槛

本机 `go2w_visual_action` 使用 RGB 图像向 Mini-JEV 查询 `person`、`prohibition` 和 `obstacle` 三项概率。障碍物问题检查物体是否挡住机器人前方区域，三项概率均写入 `evaluate` 响应的 `result_json`。

- 三项概率均低于 `0.35` 时，视觉判断 `forward_allowed` 为 `true`。`go2w_visual_action` 不读取 LiDAR 数据。
- `evaluate` 读取一帧 RGB 图像并返回三项概率，不发送运动指令。
- `run_step` 持续评估直到调用 `cancel`。`evaluation_interval_s=1.0` 控制相邻两轮开始时间的最小间隔；推理或动作超过一秒时，下一轮在当前轮结束后开始。
- 任一风险概率达到阈值后，任务记录 `blocked_reason`，本次任务持续保持停止并继续观察；后续图像变化不会恢复本次任务的前进。
- 每次前进还要求 `motion_enabled=true`、相机数据新鲜、运控会话由操作员启用且健康。前进结束后重新拍摄和判断；动作期间不会进行下一轮推理。
- 默认速度为 `0.1 m/s`、时长为 `0.9 s`；配置上限为 `0.25 m/s` 和 `1 s`。
- Native 配置继续保持 `motion_enabled=false` 和底盘 `allow_motion=false`。视觉模型没有经过现场障碍物距离和碰撞保护验收。

### 2026-10-31 Orin 实时评估

`go2w_visual_action` 已同步到 Orin，Robonix 与 Mini-JEV 分别运行在 tmux 会话 `go2w-robonix` 和 `go2w-jev`。模型健康接口返回 `loaded: true`。

模型在运行设备上合并 LoRA 权重，并检查合并结果及输出概率均为有限数值。使用宽度 `320` 的演示图片，有人图片和走廊图片的 `person` 概率分别为 `0.953` 和 `0.057`；禁行标志和欢迎标志图片的 `prohibition` 概率分别为 `0.948` 和 `0.006`。宽度 `640` 的有人与走廊图片对应 `person` 概率 `0.946` 和 `0.017`，推理耗时约 `2.3～2.4 s`；当前保持宽度 `320`。

通过实际 MCP 接口调用 `evaluate`，返回 `person=0.020`、`prohibition=0.011`、`obstacle=0.044` 和 `forward_allowed=true`。随后使用 `run_step` 完成三轮真实画面评估并调用 `cancel`，最终状态为 `CANCELLED`，`motion_requested=false`。使用 Mini-JEV 对禁行标志与走廊图片的真实输出核验停止保持逻辑：风险出现后再输入通畅画面，`blocked_reason` 仍保持不变。本轮没有请求底盘运动。核验记录位于 Orin 的 `work/visual-action-check/mcp-result.log` 和 `latch-result.log`。

## 2026-10-31 数据来源与坐标核验

现场采用 RGB、JEV 和 LiDAR 路线，D435i 不作为必要输入。本次仅创建传感器订阅并保存数据，没有调用运动、姿态或启用接口。

采集时间为 UTC `2026-10-31 05:03:53` 起的 12 秒，即北京时间约 13:03:53。每个有数据的点云话题保存前三帧完整 `PointCloud2` CDR，另外保存静态 TF 和 JSON 检查报告。

| 话题 | 12 秒收到帧数 | 原始 `frame_id` | 前三帧记录 |
|---|---:|---|---|
| `/unitree/slam_lidar/points` | 240 | `livox_frame` | 每帧 10,013～10,080 点；源时间与接收时间相差 0.052～0.086 秒 |
| `/utlidar/cloud` | 185 | `utlidar_lidar` | 每帧 3,986～4,043 点；源时间与接收时间相差约 116.25 秒，需核对两个时钟的关系 |
| `/utlidar/cloud_base` | 0 | 未取得 | 发现发布者，采样期间没有收到消息 |
| `/utlidar/cloud_deskewed` | 0 | 未取得 | 发现发布者，采样期间没有收到消息 |
| `/unitree/slam_mapping/points_synced` | 100 | `map` | 发布节点为 `mapping_cloud_restamp`；重新写入的时间戳不能证明原始传感器采集时间 |

本次收到的 TF 包含 `map -> base_footprint` 和 D435i 内部坐标关系，没有收到连接 `livox_frame`、`utlidar_lidar` 与机器人机体的坐标关系。当前数据支持继续使用 MID-360 原始点云开展几何核验；尚无证据允许把 `/utlidar/cloud_base` 直接作为有效机体坐标点云。

本机证据目录：`robonix-unitree-go2w/work/lidar-check/captures/20261031T050353Z/`，其中 `report.json` 包含原始字段、发布者、时间戳和 TF 数值。Orin 副本位于 `/home/unitree/robonix_ws/robonix-unitree-go2w/work/lidar-check/captures/20261031T050353Z/`。这些文件位于工作目录，跨机器交接时需要同时复制证据目录。

采集脚本为 `robonix-unitree-go2w/work/lidar-check/capture_lidar_frames.py`。远端执行需要加载 `/opt/ros/foxy/setup.bash`、`/home/unitree/cyclonedds_ws/install/setup.bash`，并使用 `/usr/local/lib` 中的动态库。仅加载系统 ROS 环境的查询出现过启动异常；按服务器已有环境加载后，12 秒真实采样成功完成。

## 2026-10-31 地面方向与安装参数检查

本轮同步订阅 3 秒 MID-360 点云、LiDAR IMU 和机体 IMU，共收到 59 帧点云、601 条 LiDAR IMU、1,485 条机体 IMU，保存前三帧完整点云和 IMU 数值记录。采样约对应北京时间 13:18:22～13:18:25，没有调用运动、姿态或启用接口。

完整 CDR 使用 Rosbags 0.11.9 解析，PointCloud2 使用 pypcd4 1.5.1 转为数值数组，逐帧确认点数与采集元数据一致。平面拟合采用 Open3D CPU 0.19.0，仅进行数值计算。分析区域是距传感器 0.3～8 米的完整点集，平面距离阈值为 0.025 米，RANSAC 每次 2,000 轮、固定随机种子 42，最多提取六个平面。

LiDAR IMU 的平均加速度归一化向量为 `[-0.24204, 0.01296, 0.97018]`，最大角速度模长约 `0.014 rad/s`。以下候选平面的法线与该加速度方向夹角小于 3°，且传感器到平面的有向距离在 0.2～1.5 米范围。这些条件用于筛选地面候选，不构成完整机体标定。

| 原始帧 | 平面内点数 | 传感器到候选地面距离 | 法线与 Z 轴夹角 | 法线与加速度参考夹角 | 拟合 RMS |
|---|---:|---:|---:|---:|---:|
| `unitree__slam_lidar__points_0.cdr` | 1,139 | 0.56294 m | 13.595° | 0.580° | 0.00501 m |
| `unitree__slam_lidar__points_2.cdr` | 1,128 | 0.56063 m | 13.550° | 0.607° | 0.00631 m |

第一帧候选地面方程约为 `-0.23498*x + 0.00595*y + 0.97198*z + 0.56294 = 0`。第二帧未提取到满足条件的候选地面，保留这一结果。两帧地面候选和平均加速度方向共同支持：当前传感器 Z 轴与朝上参考约有 14° 夹角，候选地面位于传感器下方约 0.56 米。该数值包含当前机体姿态的影响，不能直接认定为固定安装角。

LiDAR IMU 的 `header.frame_id` 为空，因此没有自动建立它与 `livox_frame` 的 TF。地面法线与加速度的数值关系用于交叉核对；机器人前向、横向、完整安装旋转和机体平移仍未确认。传感器 Z 值会受到倾角和目标位置影响，检测高度需要按确认后的坐标关系计算。

实际驱动程序位于 `/unitree/module/unitree_slam/bin/mid360_driver`。已检查同目录下的驱动、建图和定位参数：

- `config/mid360/user_config.yaml` 声明点云坐标系为 `livox_frame`、频率为 20 Hz。
- `config/mid360/mid360_config.json` 的 `extrinsic_parameter` 中旋转和平移均为零，没有记录现场测得的机体安装姿态。
- `config/pl_mapping/mid360.yaml` 的 `Go2_W.pose_imu_lidar` 为 `[1,0,0,0,-0.011,-0.02329,0.04412]`。
- `config/pl_relocation/mid360.yaml` 的同名参数为 `[1,0,0,0,0.187,0,0.0803]`。配置没有把它们声明为经过现场验证的机器人机体外参。
- 当前导航程序 `unitree_slam_client.py` 将官方里程计的 X/Y 和 yaw 直接用于平面机体位姿，没有提供完整的 LiDAR 到机体安装变换。

同步采样证据和分析结果位于 `robonix-unitree-go2w/work/lidar-check/captures/gravity-check-20261031/`，其中 `imu_report.json` 保存 IMU 数值，`report.json` 保存原始帧信息，`plane_analysis.json` 保存各帧 SHA256、全部候选平面及拟合指标。初次完整点云的独立拟合结果保存在 `captures/20261031T050353Z/plane_analysis.json`。官方配置副本位于 `robonix-unitree-go2w/work/lidar-check/calibration-config-20261031/`。

诊断脚本为 `work/lidar-check/capture_imu.py` 和 `work/lidar-check/analyze_planes.py`，相对于 `robonix-unitree-go2w/`。分析依赖安装在 `work/lidar-check/.venv/`，机器人运行环境没有安装新依赖。本轮完成地面候选与现有安装参数检查；完整机体坐标关系继续等待已知位置的现场目标验证。

## 前向与横向核验：30 厘米方块位置 A、B、C、D 已采集

现场目标为蓝色方块，操作员报告长宽高均约 0.30 米，并要求直接采样。初始水平距离没有实测，采集备注明确记录距离未知。本轮没有依据此前提出的 1.5 米放置要求填写距离。

位置 A 约在北京时间 13:35:17～13:35:20 采样，保存 61 帧完整 MID-360 点云，共 615,072 个原始点，源时间与接收时间相差 0.041～0.070 秒。证据目录为 `robonix-unitree-go2w/work/lidar-check/captures/20261031T053517Z/`，远端保留相同目录名的副本。

本轮直接解析每帧完整 CDR，并逐帧确认点数与元数据一致。合并点云重新拟合的地面候选方程约为 `-0.23541*x + 0.00596*y + 0.97188*z + 0.56428 = 0`，使用此前 IMU 数值作为候选方向参照。按地面相对高度和空间聚类检查小物体后，尺寸筛选仍得到多个点群，当前不能确认哪个点群对应蓝色方块，也没有得到经过现场验证的方块距离。LiDAR 数据不含颜色信息。

分析脚本为 `robonix-unitree-go2w/work/lidar-check/analyze_cube.py`，数值结果保存为证据目录中的 `cube_analysis.json`，包含每帧 SHA256、地面模型、聚类范围和逐帧匹配点数。分析坐标仅根据地面候选调整高度方向，尚未等同于机器人机体坐标。

操作员报告沿狗头朝向、远离机器人移动方块 0.5 米后，位置 B 于北京时间约 13:42:28～13:42:31 采集，保存 60 帧、604,512 个原始点；源时间与接收时间相差约 0.046～0.061 秒。证据目录为 `robonix-unitree-go2w/work/lidar-check/captures/20261031T054228Z/`。地面候选方程为 `-0.23555*x + 0.00596*y + 0.97184*z + 0.56456 = 0`。

对 A、B 的完整点云采用 0.025 米体素和 0.06 米邻近距离进行差异检查，在 B 发现一组新增候选点，中位坐标约为 `livox_frame: [1.923,-0.066,0.077] m`，包含 140 个体素点。B 的独立聚类分析在 60 帧中的 42 帧各找到至少 5 个邻近原始点；按地面候选调整高度后的水平距离中位数约为 1.881 米。位置 A 没有提取到可对应的消失点群，因此不能仅据此确认该点群属于蓝色方块，也不能计算方块的实际移动向量。

操作员报告将方块向机器人左侧移动约 0.5 米，前后位置和朝向也有少量变化。位置 C 约在北京时间 13:45:07 采样，保存 61 帧、613,152 个原始点；源时间与接收时间相差约 0.047～0.063 秒。证据目录为 `robonix-unitree-go2w/work/lidar-check/captures/20261031T054507Z/`。

比较 B、C 点云，B 的候选点群消失，同时 C 出现新点群：中位坐标从 `[1.923,-0.066,0.077] m` 变为 `[1.960,0.540,0.061] m`，变化约 `[+0.037,+0.606,-0.016] m`。这一变化与现场报告的左移一致，支持将点群对应至方块，机器人左侧对应 `livox_frame` 的正 Y 方向。两组坐标是点群中位数，受到方块朝向、前后位置和可见表面变化影响，不能作为精确位移。位置 A 没有提取到对应点群，前向轴及完整机体外参仍需核验。

操作员报告从位置 C 沿狗头方向前移方块约 0.5 米。位置 D 于北京时间约 13:47:00 采样，保存 61 帧、613,344 个原始点，源时间与接收时间相差约 0.045～0.080 秒；证据目录为 `robonix-unitree-go2w/work/lidar-check/captures/20261031T054700Z/`。

比较 C、D 点云，C 的候选点群消失，D 出现新点群：中位坐标由 `[1.9595,0.5404,0.0617] m` 变为 `[2.6797,0.5100,0.2134] m`，变化约 `[+0.720,-0.030,+0.152] m`。现场移动方向与正 X 一致，结合左移检查，支持 `livox_frame` 正 X 对应前方、正 Y 对应左侧。点群中位数受可见表面和目标朝向影响，不能作为精确位移或机体平移外参；Z 坐标变化也受到已测地面倾角影响。

结果保存在各次采集目录的 `cube_analysis.json` 和 `position_diff.json`；比较脚本为 `robonix-unitree-go2w/work/lidar-check/compare_cube.py`。当前已核验主要方向，完整安装外参、地面相对高度和走廊覆盖仍需确认。本轮没有调用控制接口，`motion_enabled` 保持 `false`。

## 2026-10-31 走廊离线核验

使用位置 B、C、D 保存的完整点云，按本机 `native/ros_worker.py` 的条件逐帧统计：`x=[0,3] m`、`|y|≤0.45 m`、`z=[-0.5,0.8] m`，前方距离采用 `hypot(x,y)`。方块候选范围取位置差异点群的坐标范围，每个方向增加 0.02 米；这个范围可能包含背景点，不能直接当作方块识别率。

| 位置 | 帧数 | 候选范围中有回波的帧数 | 候选范围与当前走廊同时有回波的帧数 | 走廊包含原点的帧数 |
|---|---:|---:|---:|---:|
| B | 60 | 45 | 45 | 60 |
| C | 61 | 61 | 0 | 61 |
| D | 61 | 61 | 61 | 61 |

位置 C 的目标候选区域在走廊左侧边缘附近，采样没有找到同时满足候选范围与当前走廊的点。是否需要该位置参与停车判断，取决于机器人实际宽度、雷达安装位置和安全余量；现有点群范围不能代替这些现场测量。位置 D 的候选范围内可能包含背景回波，表格中的帧数只表示几何范围统计。

全部 182 帧的原始点云都包含 `[0,0,0]` 点，每帧有 779～1,680 个，强度均为零。本机 Observer 现在从有效点、预览和走廊统计中排除三轴均为零的点，其他非零近距离点继续参与判断。有效点数不足时服务返回 `LIDAR_INVALID`；走廊没有有效回波时也返回 `LIDAR_INVALID`，不请求前进。

对 B、C、D 的完整点云检查当前走廊中的最近非零点：182 帧中有 141 帧的最近水平距离小于 0.2 米，分别为 B 的 35 帧、C 的 54 帧和 D 的 52 帧；其中 133 帧的最近点强度为零。这些最近点位于 `livox_frame` 的 X 约 `0.108～0.168 m`、Y 约 `-0.037～0.032 m`、Z 约 `-0.011～0 m`。方块位置发生变化时这类点仍多次出现，说明仅凭 `0.13 m` 不能确认狗头前方存在障碍物。当前统计使用到雷达坐标原点的 `hypot(x,y)`，完整安装外参与机体边界尚未确认；强度为零的含义也需要结合驱动数据确认。诊断脚本为 `work/lidar-check/inspect_near_returns.py`，尚未据此过滤非零点。

使用真实 B、C、D 点云共 182 帧回放本机计算，并与独立的 NumPy 计算逐帧核对：全部帧的有效点数不少于 8,370；15 帧走廊没有有效回波，均验证返回 `LIDAR_INVALID`；141 帧最近距离不超过 1.5 米，26 帧超过 1.5 米。这些结果仅覆盖已保存的现场数据和当前走廊；近距离标定目标、传感器异常和实际走廊覆盖仍需现场只读验收。

实现位于 `native/lidar_guard.py`、`native/ros_worker.py` 和 `packages/go2w_visual_action/go2w_visual_action/lidar.py`；回放脚本为 `work/lidar-check/verify_zero_filter.py`，逐帧文件 SHA256 和结果保存在 `work/lidar-check/captures/zero-filter-verification-20261031.json`。代码已经同步至 Orin。

Orin 当前导航配置中的 `base_footprint` 矩形使用 X 范围 `[-0.34,0.34] m`、Y 范围 `[-0.22,0.22] m`。这是软件配置，尚无证据证明它是当前机体的实测尺寸，也不能据此确定 LiDAR 中心到机体的平移。

使用位置 C 的地面候选平面，按距离平面不超过 0.02 米筛选地面候选点；在 B、C、D 的 182 帧中，当前走廊距离传感器 0.05～1.5 米的区域没有出现此类点。该结果适用于已采集的现场数据和筛选条件。

诊断脚本为 `robonix-unitree-go2w/work/lidar-check/audit_guard.py`，逐帧记录保存在 `robonix-unitree-go2w/work/lidar-check/captures/guard-audit-20261031.json`。完整外参和走廊覆盖尚未确认，两个运动相关标记保持 `false`。

## 2026-10-31 宇树参数与现有配置

[宇树 Go2-W 官方产品页面](https://www.unitree.com/cn/go2-w/)列出的站立尺寸为 `70 × 43 × 50 cm`。按长、宽、高顺序理解，标称宽度为 0.43 米；官网页面没有标注雷达到狗头前缘的距离。当前导航软件使用的标称宽度为 0.44 米，两个数值可作为检查参考，现场附件和姿态需要单独考虑。

机器人上的 `/unitree/module/unitree_slam/config/gridmap_config/config.yaml` 含有 `Go2_W` 配置：`T_Dog2lidar: [0,0,0,0,0.226,0]`，注释标明顺序为 `xyz rpy`；`0.226 rad` 约为 12.95°。此前点云与 IMU 计算得到 Z 轴和朝上参考约 13.55～13.60°，二者数值接近。该配置的平移项为零，文件没有说明该原点与机身前缘、中线的实际关系，不能直接得到雷达到狗头前缘的水平距离。

同一 `Go2_W` 配置的 `lidar_height` 为 0.4 米；本次点云的地面候选到传感器距离约 0.56 米。配置值的参考条件和现场姿态仍需确认。官方 Go2-W 标称 URDF 有 `radar_joint`，但当前检查没有确定它对应现场的 MID-360，因此尚未将该关节用于 MID-360 坐标变换。

若按官网尺寸将 0.43 米视作站立宽度，并假设雷达位于机体中线上，当前走廊半宽 0.45 米相对标称半宽 0.215 米，每侧多出约 0.235 米。位置 C 的差异点群最小 Y 坐标约为 0.467 米；该回波与标称半宽的坐标差约 0.252 米。这些数值依赖居中假设，方块实体边缘和现场机体完整包络仍需用数据核验。

官方设备配置副本保存在 `robonix-unitree-go2w/work/lidar-check/calibration-config-20261031/gridmap_config.yaml`。官网尺寸和设备现有配置可用于离线核验，现场机体边界仍需测量。

## 2026-10-31 Orin Observer 与服务接口

Orin 的 `native/ros_worker.py`、`native/lidar_guard.py`、`native/config.yaml` 和 `native/ros-python.sh` 已配置 Observer 所需的点云统计与依赖路径。ROS Foxy 官方 `sensor_msgs_py` 2.0.5 安装在 `rbnx-build/native/ros-python-deps`，源码来自 `ros2/common_interfaces` 的 `foxy` 分支，提交为 `6356bc82f3a034f5d2c61c6760df0007a6cadfb0`。重新安装时，在 Orin 执行：

```bash
bash native/fetch-sensor-msgs-py.sh
```

Native Observer 随 `go2w_native` 持续运行，提供点云状态和走廊统计用于独立诊断。完整外参和走廊覆盖尚未核验。此前 15 次 Observer 检查结果保存在 `robonix-unitree-go2w/work/lidar-check/orin-stage/verification-18092.json`。

`packages/go2w_visual_action` 已同步至 Orin，使用 Python 3.11 独立环境安装依赖，并通过 Robonix `rbnx build` 正式构建，生成接口文件。模型服务当前在 tmux 会话 `go2w-jev` 运行，地址为 `127.0.0.1:8765`，健康接口报告模型已加载。模型主体及 LoRA 文件在电脑和 Orin 的 SHA256 一致；`MANIFEST.json` 保存交付包摘要，`verify_manifest.py` 在电脑列出五个差异文件，在 Orin 列出四个差异文件，均为运行代码或配置。

服务通过 Atlas 的 `camera/snapshot` MCP 能力读取 RGB JPEG。`go2w_native` 登记 `camera/rgb` 和 `lidar/lidar3d` ROS2 能力。服务构建期间已在 Orin 注册 Robonix 源码目录。

Robonix、`go2w_visual_action`、Mini-JEV 与 Client 当前分别在 tmux 会话 `go2w-robonix`、`go2w-jev`、`go2w-client` 中运行。电脑已建立 `127.0.0.1:7860` 到 Orin Client 的 SSH 转发，并通过该地址读取 `/api/defaults`。

语音链路检查显示 Atlas 中共有 10 个活动 provider，`speech`、ASR 和 TTS 能力均可用；`speech.log` 记录 ASR、ASR stream、TTS 后端状态为 `OK`，`audio_client_bridge` 也处于 `ACTIVE`。电脑的 `robonix-client-audio-device.service` 处于 `active`，WebSocket 健康检查返回 `ok=true`，设备网页返回 HTTP 200。电脑到 Orin 的音频反向 SSH 转发已建立；Orin 的 Client 状态返回 `external=true`，远端健康检查返回 `ok=true`，输入和输出设备编号均为 `11`。Client 继续使用 `ROBONIX_CLIENT_REVERSE_AUDIO=0`，由电脑提供音频设备服务。本次没有执行麦克风采集或扬声器播放。

最近一次周期评估中的模型推理耗时为 `0.835` 秒。Native 配置的 `motion_enabled` 和底盘 `allow_motion` 均为 `false`；本次评估没有调用运动接口。

### 2026-10-31 llm-default API

电脑将 gateway API key 保存在用户目录的 `~/.config/robonix-go2w/gateway_api_key`，目录权限为 `700`，文件权限为 `600`。电脑端 `robonix-go2w-vlm-proxy.service` 与 `robonix-go2w-vlm-tunnel.service` 已启用并处于 `active`；代理启动时从该文件读取密钥，不需要每次重新配置。代理健康接口显示 `provider=gateway`、`model=llm-default`，SSH 反向隧道将电脑 `127.0.0.1:16668` 转发至 Orin 的同一地址。

电脑代理的 `/v1/models` 请求返回 HTTP `200`，gateway 返回的模型数组为空。随后对 `/v1/chat/completions` 发送一次 `max_tokens=1` 的真实短请求，使用 `llm-default` 返回 HTTP `200`。Orin 的 `robonix-vlm-router.service` 已启动，`native/check-vlm.sh --json` 返回 `models_check_passed`，目标为 `localhost:16667`。此次核验没有调用机器人运动接口。

## 代码与配置入口

| 路径 | 内容 |
|---|---|
| `robonix-unitree-go2w/packages/go2w_visual_action/go2w_visual_action/robot.py` | 通过 Atlas MCP 获取 RGB 快照，调用 JEV 并判断三项概率 |
| `robonix-unitree-go2w/packages/go2w_visual_action/go2w_visual_action/runtime.py` | `evaluate`、`run_step` 生命周期与运动门槛 |
| `robonix-unitree-go2w/packages/go2w_visual_action/go2w_visual_action/settings.py` | 评估间隔、运动开关、速度与时长设置 |
| `robonix-unitree-go2w/native/ros_worker.py` | 原生传感器 Observer；正 X 走廊统计及点云抽样 |
| `robonix-unitree-go2w/native/config.yaml` | Observer 当前 LiDAR 走廊，`allow_motion: false` |
| `robonix-unitree-go2w/native/robonix_manifest.yaml` | 本机部署清单；视觉服务周期为一秒，运动标记保持 `false` |
| `robonix-unitree-go2w/work/lidar-check/analyze_cloud.py` | 现场只读点云统计脚本 |

## 后续处理

1. 已取得前方向正 X、左方向正 Y 的现场点云依据。继续确认完整安装外参和地面相对高度，核验走廊覆盖。
2. 确认坐标后，验证空通道与已知距离的大板障碍物，排除地面和机器人自身，再检查 Observer 走廊设置。
3. 根据运控就绪条件判断是否需要里程计。若需要姿态数据，核验 `/slam/odom` 的时间、连续性和有效性；`/dog_odom` 的全零四元数不能作为姿态依据。
4. Mini-JEV、Robonix 和 Client 已在 Orin 的 tmux 会话运行；Client 已通过 SSH 转发到电脑 `http://127.0.0.1:7860/`。最近两次实时推理为 0.943 秒和 0.858 秒；继续观察后续耗时，连续超过 1.2 秒时切换电脑的 5070 Ti。
5. 现场操作员持遥控器，完成明确的运控会话启用和前述检查后，执行有限前进验收；默认 0.1 m/s、0.9 秒，速度上限 0.25 m/s。
6. 当前继续保持 `motion_enabled=false` 和底盘 `allow_motion=false`。
7. 继续使用现场已知画面核对三项 `noul` 概率，特别是实际前进路线上的障碍物。
