# Go2-W 画面风险判断服务

`go2w_visual_action` 通过 Robonix Client 提供 `evaluate`、`run_step`、`status` 和 `cancel`。服务经 `go2w_native` 的 `camera/snapshot` MCP 能力读取 RGB JPEG 图像，使用 Mini-JEV 的三项概率进行判断。

`evaluate` 获取一帧相机图像，向 Mini-JEV 请求 `person`、`prohibition` 和 `obstacle` 概率，不调用运动接口。三个概率写入响应及 `result_json`。`run_step` 持续获取图像并判断，直到 `cancel`；每轮最多请求一次有界前进，动作结束后重新获取图像。`status` 返回最新结果，`cancel` 取消任务；存在前进请求时会调用底盘 `StopMove`。

## 调用方式

- 只检查当前画面：调用一次 `evaluate`，将 `request_id` 设为空字符串。服务会自动生成请求标识并直接返回结果。
- 开始持续演示：调用一次 `run_step`，将 `request_id` 设为空字符串。服务持续读取新图像并完成风险判断，运动配置和底盘会话满足条件时每轮执行一次有界前进。
- `run_step` 返回 `run_id` 后，需要查看最终结果时，将该值原样传给 `status`。需要中止当前任务时，将同一个值传给 `cancel`。不要自行编造 `run_id`，也不要在启动任务前调用 `status`。

软件包自身的缺省是 `motion_enabled: false`。当前根部署清单把它设为 `true`，前进速度 `0.25 m/s`，持续时间 `1` 秒。底盘清单仍是 `allow_motion: false`，所以开机不会自动走。操作员明确使能底盘会话后，`run_step` 才可以请求前进。

`evaluation_interval_s` 默认为 `1.0` 秒，控制两轮开始时间的最小间隔。推理和运动耗时超过该间隔时，下一轮在当前轮结束后开始。前进期间使用该轮开始时的图像结果；根清单里每次动作是 `1` 秒，动作结束后重新观察。

激光没有可用距离时只跳过这一轮。人和禁行标志会暂停前进：禁行概率连续 3 轮平均值低于 `0.35` 后可以恢复，人离开且画面重新满足条件后也可以恢复。模型报告的障碍，以及激光距离进入停止距离，会锁住本次任务，直到取消。底盘返回 `not_observed` 或 `not_dispatched` 时任务失败；已经确认的 `StopMove` 不能当作观察到了运动，服务不会因此自动再请求前进。

## 启动 Mini-JEV

Orin 的 Native 环境将 `GO2W_JEV_MODEL_URL` 缺省设为 `http://127.0.0.1:8765`。演示模型需先运行并返回 `/health` 与 `/answer`。模型服务使用 `go2w_demo` 中的模型、runtime 和 LoRA 文件。

在 Orin 上启动本机模型服务：

```bash
cd /home/unitree/go2w_demo
tmux new-session -d -s go2w-jev 'cd /home/unitree/go2w_demo && .venv/bin/python laptop/start_model_server.py --host 127.0.0.1 --port 8765'
tmux ls
curl -fsS http://127.0.0.1:8765/health
```

电脑的 GPU 服务用于 Orin 单轮推理耗时超过 1.2 秒时。电脑与 Orin 需要互通 TCP 8765。在电脑 `go2w_demo` 目录启动：

```bash
tmux new-session -d -s go2w-jev 'python laptop/start_model_server.py --host 0.0.0.0 --port 8765'
tmux ls
curl -fsS http://127.0.0.1:8765/health
```

使用电脑服务时，在 Orin 的用户环境文件 `~/.config/robonix/env` 设置 `GO2W_JEV_MODEL_URL=http://<电脑地址>:8765`。重新加载 Native 环境后，部署清单会将该地址传给服务。

## 前进判定

单次前进需要同时满足以下条件：

- `person`、`prohibition` 与 `obstacle` 概率都低于 `clear_probability_limit`，默认值为 `0.35`；
- RGB frame 为 `front_camera`，相机源时间戳通过新鲜度检查；
- `motion_enabled` 设为 `true`；
- 操作员已通过 Client 明确启用底盘会话，且底盘状态健康。服务不会调用底盘 `enable` primitive。

软件包缺省速度为 `0.1 m/s`，持续 `0.9 s`；字段上限为 `0.25 m/s` 和 `1 s`。根清单当前使用上限值：`0.25 m/s`、`1 s`。图像时间戳过期、frame 异常或模型结果无效时，服务停止任务并返回原因。视觉概率不能提供经过验证的障碍物距离或碰撞保护。

`evaluate` 只返回判断，不请求运动。`run_step` 在 `motion_enabled` 为 false，或底盘会话未使能时，也不会发出前进请求。Mini-JEV 权重和 LoRA 不在本仓库内，需要单独准备模型服务。

## Client 能力

- `robonix/service/go2w_visual_action/evaluate`
- `robonix/service/go2w_visual_action/run_step`
- `robonix/service/go2w_visual_action/status`
- `robonix/service/go2w_visual_action/cancel`

`evaluate` 和 `run_step` 接收 `request_id`；可传空字符串，由服务自动生成请求标识。`run_step` 返回服务生成的 `run_id`，仅将它传给 `status` 或 `cancel` 查询或取消本次任务。`STOP` 结果会附带拒绝前进请求的原因。
