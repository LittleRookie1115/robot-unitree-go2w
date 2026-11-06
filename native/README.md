# Native Go2-W deployment on this Orin NX

This profile runs directly on Ubuntu 20.04.5 / L4T 35.3.1 / ARM64 and ROS 2
Foxy. It uses no container. It provides camera, lidar, IMU and measured battery
state to the installed Robonix core and Client.

Authorization policy (2026-10-22): with an operator beside the robot, explicitly
say “使能底盘” or “启用底盘运动” in Client. This is human authorization for Pilot
to call the `enable` MCP primitive; terminal commands are optional equivalents.
The catalog has 15 gRPC / 14 MCP primitives. Only legacy `arm` remains hidden.
Default boot stays disabled, and enable sends no implicit posture or nonzero
motion. “前进” alone is not enable consent; faults require a fresh explicit
enable request rather than automatic reenabling or action retries.

The health view uses the full Soma paths `body/base/body/...`. Camera, lidar,
IMU and battery availability come from the native observer's freshness checks.
The four wheels remain in the physical model, but their individual health is
unknown until motor telemetry is reliably mapped to each wheel. Missing wheel
readings must not be reported as communication failures. Browser audio is not
declared as robot hardware because this deployment has no corresponding onboard audio
provider.
The native manifest now also includes the official `audio_client_bridge` in
reverse mode and the official Speech service with local CPU ASR/TTS backends
from `packages/go2w_native_speech`. These use the laptop's microphone and
speakers, without adding a fictitious onboard audio component. The bridge
listens only on loopback port 60002; `native/connect_laptop.py` discovers the
gRPC ports and tunnels them with the audio connection over SSH. See the
[laptop voice guide](../../笔记本语音连接指南.md). Hands-free and motion remain
disabled by default.
The camera reader already caps input at 5 Hz; the observer processes each
camera frame rather than applying another 200 ms sampling gate that would
drop frames when their arrival times jitter.
Preview and health observation use the bridge's validated original JPEG on
`/robonix/go2/camera/image_raw/compressed`, preserving the same header timestamp
and frame ID. The standard RGB Image capability remains on `image_raw`.
Expired telemetry is reported through the existing Soma `HEALTH_STALE` state.
Missing initial samples, invalid timestamps and explicit device faults are
not converted to healthy readings.

The default sensor deployment keeps motion disabled. There is no Sport daemon, posture API, velocity
publisher, chassis command subscription, arm service, mapping or navigation
process in this profile. Build scripts never run Unitree motion examples.

The separately registered `go2w_chassis_motion` primitive provides gRPC Move,
Stop, motion-status and explicit StandUp/StandDown interfaces with
`allow_motion: false`. Its default
startup creates no control ROS process or SDK client. See the
[motion package](../packages/go2w_chassis_motion/README.md) for bounded command
semantics, offline tests and the supervised real-runtime prerequisites.

Before a supervised physical trial, run `bash native/check-motion.sh`.
This subscription-only audit records original SportModeState timestamps and
DDS writer timestamps, network addresses/routes and the current motion RPC
status in `rbnx-build/native/motion-preflight.json`. It never enables or arms
anything. Exit status 2 means a gate is blocked. DDS transport delivery must
not be mistaken for a valid payload timestamp. On this device the payload
stamp is zero and the DDS writer clock is also offset from the host; neither
is silently rewritten. Clock-domain offset is diagnostic metadata, not a
standalone hardware fault. The network audit uses the native controller
address and selected direct route; it does not require the inherited
workstation's 192.168.123.99, remove the Internet route or alter DNS/IPv6.

The operator corrected the physical model to Go2-W. Read-only SDK
MotionSwitcher CheckMode returned `ai-w`. The explicit motion profile is
`native-go2w-manual-v1`, with `dds-source-lowstate-tick-v1` source-progress
qualification, unchanged original payloads and no claim of absolute
hardware measurement age. See the motion package for its limits and mode
policy. The default Core registers all fifteen chassis primitives.
Fourteen primitives, including `enable`, also enter Pilot's MCP catalog.
An explicit Client request “使能底盘” or “启用底盘运动” authorizes Pilot to
call enable; only compatibility `arm` remains hidden and gRPC-only.
Ordinary actions cannot create the backend or grant permission.
The optional equivalent `bash native/chassis.sh enable` uses the same
session authorization. Enable lazily starts the owned transport and includes
internal zero preparation/arm. There is no separate arm step or 15-second idle
expiry. Normal actions and `stop`
retain healthy enable; faults, loss of state/owner, active-request timeout
and shutdown revoke it and require a fresh human enable.
Request “禁用底盘运动” in Client, or optionally run
`bash native/chassis.sh disable`, to release transport while keeping Core, Pilot
and Client running. `motion.sh` is a compatibility wrapper; the old separate
`robonix-go2w-motion` service is no longer needed. Neither startup
nor enable dispatches posture or nonzero velocity.

Directional CLI commands call their named primitive RPCs, rather than using
CLI-only aliases for `move`. Native Move success requires the matching
firmware response; the result also reports `physical_motion_observed` from
qualified raw Sport velocity. No feedback yields `not_observed`, and no
verified dispatch yields `not_dispatched`; neither is reported as completed.
Distance and angle remain unverified. Standing, lying down and explicit
`balance` use separate posture primitives. Normal completion checks the
fresh terminal mode and does not issue StopMove; stop/disable/fault still do.
`not_observed` and `not_dispatched` are failed movement outcomes, including
through MCP. Report their evidence to the operator instead of repeating
movement automatically.

`chassis.sh status` reports transport configuration and session permission
separately: `motion_enabled: true` means the backend exists; motion requires
`guard.ready`, `guard.session_enabled`, `guard_state: ARMED` and
`daemon_armed: true`. A fault revokes the session even if the backend remains
configured. After repair/restart, explicitly request enable again in Client;
`bash native/motion.sh enable` remains an optional equivalent. Permission
never recovers automatically. `guard.adapter_reason` and
`guard.native_clock_fault` retain diagnostic context, and backend faults also
appear in the top-level `faulted` field.

The native SDK samples its inspection time while holding the same mutex as
its DDS callbacks. This avoids treating a receipt newer than the caller's
pre-lock timestamp as stale. The 200 ms freshness bound, source progression,
tick qualification and latched clock faults are unchanged. For a subscription-only
SDK audit (no SportClient and no enable), after `bash native/build-motion.sh`:

```bash
env -u LD_LIBRARY_PATH \
  GO2_STATE_TIME_BASIS=dds-source-lowstate-tick-v1 GO2_ALLOWED_MODES=1,3,5,6 \
  rbnx-build/native/chassis-sdk/install/bin/go2_sdk_state_probe eth10 120
```

After the initial qualification, `actual_unhealthy_samples` and
`guard_unhealthy_samples` should be zero. `old_timestamp_false_rejections`
counts how often the previous time-sampling order would have falsely rejected
a healthy snapshot. This audit does not validate physical motion or posture.
`native/verify_session_enable.py --idle-seconds 120` additionally checks idle
permission and zero-speed requests, but requires a session the human has
already enabled; it never enables one itself.

## Layout and prerequisites

Both the user service and foreground `native/start.sh` use
`native/start.sh → native/env.sh → native/vlm-env.sh`, which sources the same
`~/.config/robonix/env` for `VLM_BASE_URL`,
`VLM_API_KEY` and `VLM_MODEL`, and maps them to Pilot's
`ROBONIX_VLM_UPSTREAM`, `ROBONIX_VLM_API_KEY` and `ROBONIX_VLM_MODEL`
environment variables. The default manifest starts Pilot on loopback port
50071. Both the sensor and manually enabled motion sessions use this loader.
Keep the credential file outside the repository with mode 0600. Editing it
takes effect on the next restart of the actual Core owner (user service or
foreground process); it is never copied into manifests
or command arguments. Client reaches Pilot through Atlas/Liaison and does
not need the credentials in its browser settings.

Loading this file configures the API; it does not start the API process or
create a model SSH tunnel. A refused connection, including localhost:16667,
usually means that listener is absent or its model tunnel has closed.
HTTP 401 is authentication failure; HTTP 404 calls for checking the API path.
The current banner's `vlm=?@?` defers the environment-backed summary and does
not prove that the environment was omitted. Do not blindly start the optional
model router: port 16667 may already belong to a different API.

Run this bounded, authenticated models-list check on the robot:

```bash
bash native/check-vlm.sh
bash native/check-vlm.sh --json
```

It reports configuration presence without printing keys and reads `/models`;
it makes no completions request or Pilot task. Exit 0 means valid HTTP 200
with the configured model in the list; exit 2 means configuration,
authentication, connectivity or model-list verification failed.
`native/start.sh` checks after acquiring its deployment lock and before boot.
A warning still permits sensors and stop capabilities to start.

This checkout belongs beside the existing Robonix installation:

```text
robonix_ws/
  env.sh                      installed Robonix toolchain environment
  .tools/rbnx-python/          Python 3.11 + Robonix SDK
  robonix/                    Robonix source registered with rbnx setup
  robonix-client/              installed Client with its separate Python
  robonix-unitree-go2w/        this repository
```

Native dependencies already present on this device are `/opt/ros/foxy`,
`/home/unitree/cyclonedds_ws/install/setup.bash`, colcon, CMake, GCC, libjpeg,
and the system Python 3.8 with rclpy, numpy, cv2 and PyYAML. The Foxy message
packages are built from the pinned `third_party/unitree_ros2` submodule.
The camera SDK is built from the pinned `third_party/unitree_sdk2` submodule.
No existing autonomy workspace's Python messages are required at runtime.

The SDK provider runs on Python 3.11 and owns Atlas registration, gRPC
lifecycle/health and the standard camera-snapshot MCP tool. Its separate
Python 3.8 observer imports Foxy's rclpy and reads sensors. They communicate
only through a loopback HTTP endpoint at `127.0.0.1:18092`.

The camera reader uses its private Unitree DDS libraries in a separate
process; the ROS camera bridge never loads those SDK libraries. The bridge
publishes libjpeg's already-decoded BGR bytes directly as `sensor_msgs/Image`.
It does not link cv_bridge/OpenCV, avoiding this host's mixed OpenCV 4.2/4.5
ABI. The inherited strict JPEG, framing and freshness guards remain active.

## Build and validate

From the repository root:

```bash
git submodule update --init --recursive
python3 native/fetch_go2w_model.py
bash build.sh
bash native/validate.sh
bash packages/go2_sensors/tests/run_offline_tests.sh
```

All build outputs, generated bindings, model copies and local acceptance
reports live under ignored `rbnx-build/` directories. The build never installs
system packages, changes a network interface, or starts a hardware client.
Original model meshes and `LICENSE.unitree` are retained.

## Start and stop

For a laptop/Windows Client, the recommended robot-side mode runs only Core:

```bash
# Optional when switching away from the robot's local Client
systemctl --user stop robonix-client
systemctl --user start robonix-go2-native
```

Run `connect_laptop.py` on Windows using the installed Client/Conda executable;
see the [laptop voice guide](../../笔记本语音连接指南.md). This mode does not start
the robot's GUI on 7860 or local device audio on 60000/60001. Core still runs
Speech and the reverse audio bridge on 60002; laptop audio stays on the laptop.

For the existing robot-local Client mode, keep the combined startup:

```bash
bash native/install-service.sh
systemctl --user start robonix-go2-native robonix-client
systemctl --user status robonix-go2-native robonix-client
```

It conflicts with the earlier `robonix-core` static-model service because both
need the same Atlas/system ports. Starting the native service stops that
earlier service. Auto-start is not enabled.

An already running foreground Core may own the ports while the user unit is
inactive; reuse it rather than starting another instance. To switch from a
user service to foreground, stop the Core unit first and then run:

```bash
systemctl --user stop robonix-go2-native
bash native/start.sh
```

Stop the service with `systemctl --user stop robonix-go2-native`, or stop a
foreground deployment with Ctrl-C / `bash stop.sh`.
Both entry points load the same VLM environment file. Exit the foreground
instance before returning to systemd; laptop mode needs no robot-local Client.

To return to the earlier static-model setup:

```bash
systemctl --user stop robonix-go2-native
systemctl --user start robonix-core
```

The native profile never changes the existing `eth10` addresses or routes.
CycloneDDS is explicitly bound to that measured interface. The existing
Unitree SLAM/sensor processes continue to own their source topics.
The native camera alone owns its namespaced camera outputs.

## View and inspect

- Client: `http://127.0.0.1:7860/` on the machine running Client. In laptop mode
  this is Windows, and no robot-side 7860 listener is expected. Its Perception page obtains camera snapshots
  from the native provider through Atlas + MCP.
- Native telemetry: `http://127.0.0.1:18092/`. Displays camera, sampled point
  cloud, source freshness, battery and the observed odometry defect.
- JSON metadata: `http://127.0.0.1:18092/status`.
- Logs: `native/rbnx-boot/logs/` and
  `packages/go2w_native/rbnx-build/data/`.

For an already running robot-local Client, another computer may forward the
two UI ports through the existing SSH connection:

```bash
ssh -N -L 7860:127.0.0.1:7860 -L 18092:127.0.0.1:18092 unitree@<GO2_SSH_ADDRESS>
```

Laptop Client mode instead uses the helper to discover/tunnel Core and reverse
audio ports, with its own local GUI and audio services.

For an installation/stream/health check:

```bash
source ../env.sh
python native/verify.py
rbnx caps -v
```

The verification report contains metadata, not camera images or cloud payloads.
The runtime also keeps camera/cloud previews only in bounded memory.

## Measured sources and limits

Measured on this device on 2026-10-16:

| Data | Input/output | Treatment |
| --- | --- | --- |
| MID-360 cloud | `/unitree/slam_lidar/points`, frame `livox_frame` | Original timestamp retained; standard lidar capability |
| IMU | `/dog_imu_raw`, frame `dog_imu_link` | Original timestamp retained; standard IMU capability |
| Battery/motor inventory | `/lowstate` | Headerless; receipt freshness explicitly reported |
| Sport state | `/lf/sportmodestate` | Timestamp is zero; receipt-only diagnostics, no control |
| Odometry observation | `/dog_odom`, `odom -> robot_center` | Orientation is all zero; rejected as navigation odometry, no capability declared |
| RGB camera | `/robonix/go2/camera/image_raw`, frame `front_camera` | SDK image acquisition only; uncalibrated |

The raw `/utlidar` inputs previously had a substantial source-clock offset;
this profile does not modify those stamps or disable freshness checks.
Sensor data must have both a recent receipt and a valid source timestamp/frame.
Battery/Sport messages are explicitly labelled `receipt_only`; that does not
prove the age of their original hardware measurements.

Battery voltage/percentage are measured. Charging status and remaining time
are not established; the wire format's charging default must not be treated
as a measured charging-state conclusion.

No base-to-sensor transform or camera calibration has been verified here.
The static model now comes from official Unitree Go2-W assets, pinned at
5994d4faef0a9cadd3287f8de0199a67eeb2a259, with BSD-3-Clause notice and
four continuous wheel joints. It is nominal geometry, not a measured
footprint or live joint configuration. The reference Go2 mass, footprint
and body dimensions are omitted from the native Soma description. This profile does not
publish a replacement TF tree. No map/TF alignment, navigation, real-time scene
understanding, voice or motion acceptance is claimed. Future navigation work
must first establish valid odometry, measured transforms and a qualified map.

The original root Humble scripts can still be selected with
`GO2_DEPLOY_PROFILE=humble-upstream`, but they retain their upstream workstation
requirements and are not part of this native acceptance.
