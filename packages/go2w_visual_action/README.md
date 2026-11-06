# Go2-W 画面风险判断服务

`go2w_visual_action` 通过 Robonix Client 提供 `evaluate`、`run_step`、`status` 和 `cancel`。服务经 `go2_native` 的 `camera/snapshot` MCP 能力读取 RGB JPEG 图像，使用 Mini-JEV 的三项概率进行判断。

`evaluate` 获取一帧相机图像，向 Mini-JEV 请求 `person`、`prohibition` 和 `obstacle` 概率，不调用运动接口。三个概率写入响应及 `result_json`。`run_step` 持续获取图像并判断，直到 `cancel`；每轮最多请求一次有界前进，动作结束后重新获取图像。`status` 返回最新结果，`cancel` 取消任务；存在前进请求时会调用底盘 `StopMove`。

## 调用方式

- 只检查当前画面：调用一次 `evaluate`，将 `request_id` 设为空字符串。服务会自动生成请求标识并直接返回结果。
- 开始持续演示：调用一次 `run_step`，将 `request_id` 设为空字符串。服务持续读取新图像并完成风险判断，运动配置和底盘会话满足条件时每轮执行一次有界前进。
- `run_step` 返回 `run_id` 后，需要查看最终结果时，将该值原样传给 `status`。需要中止当前任务时，将同一个值传给 `cancel`。不要自行编造 `run_id`，也不要在启动任务前调用 `status`。

当前 `motion_enabled` 为 `false`，所以 `run_step` 只会完成风险判断并保持停止。

`evaluation_interval_s` 默认为 `1.0` 秒，控制两轮开始时间的最小间隔。推理和运动耗时超过该间隔时，下一轮在当前轮结束后开始。前进期间使用该轮开始时的图像结果；每次动作最长 `1` 秒，动作结束后重新观察。一旦任一风险概率达到阈值，本次任务保持停止并继续观察，直到取消；后续画面变化不会使本次任务恢复运动。

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

单次前进默认速度为 `0.1 m/s`，持续 `0.9 s`；速度上限为 `0.25 m/s`，持续时间上限为 `1 s`。图像时间戳过期、frame 异常或模型结果无效时，服务停止任务并返回原因。视觉概率不能提供经过验证的障碍物距离或碰撞保护。

当前 Native 配置保持 `motion_enabled: false`。因此 `evaluate` 可用于观察判断结果；`run_step` 不会发出前进请求。

## Client 能力

- `robonix/service/go2w_visual_action/evaluate`
- `robonix/service/go2w_visual_action/run_step`
- `robonix/service/go2w_visual_action/status`
- `robonix/service/go2w_visual_action/cancel`

`evaluate` 和 `run_step` 接收 `request_id`；可传空字符串，由服务自动生成请求标识。`run_step` 返回服务生成的 `run_id`，仅将它传给 `status` 或 `cancel` 查询或取消本次任务。`STOP` 结果会附带拒绝前进请求的原因。
