# Native Go2-W bounded chassis primitives

Current authorization policy (2026-10-22): this package registers 15 gRPC /
14 typed MCP capabilities. A user explicitly saying “使能底盘” or
“启用底盘运动” in Client authorizes Pilot to call the `enable` MCP primitive.
The CLI is an optional equivalent. “前进” or another action request must not
be interpreted as enable consent, and faults must not trigger automatic
reenabling or retries. Default boot stays disabled; enable sends no implicit
posture or nonzero motion.

| Contract | RPC | Result |
| --- | --- | --- |
| `robonix/primitive/chassis/enable` | `Trigger` | Explicit Client user authorization or optional CLI; internal zero-speed preparation |
| `robonix/primitive/chassis/disable` | `Trigger` | Revoke session authorization through `controller.disable()` |
| `robonix/primitive/chassis/arm` | `Trigger` | Compatibility alias for enable |
| `robonix/primitive/chassis/move` | `ExecuteMoveCommand` | JSON inside `status.data` |
| `robonix/primitive/chassis/forward` | `ExecuteMoveCommand` | Positive speed or distance magnitude mapped to positive X |
| `robonix/primitive/chassis/backward` | `ExecuteMoveCommand` | Positive speed or distance magnitude mapped to negative X |
| `robonix/primitive/chassis/left` | `ExecuteMoveCommand` | Positive lateral speed magnitude mapped to positive Y |
| `robonix/primitive/chassis/right` | `ExecuteMoveCommand` | Positive lateral speed magnitude mapped to negative Y |
| `robonix/primitive/chassis/stop` | `Stop` | Acknowledgement flag and JSON detail |
| `robonix/primitive/chassis/motion_status` | `Trigger` | JSON enablement, active request, guard and last result |
| `robonix/primitive/chassis/status` | `Trigger` | Alias for `motion_status` |
| `robonix/primitive/chassis/stand_up` | `Trigger` | Explicit Unitree StandUp targeting stand-lock mode 6 |
| `robonix/primitive/chassis/stand_down` | `Trigger` | Explicit Unitree StandDown/crouch dispatch |
| `robonix/primitive/chassis/lie_down` | `Trigger` | Alias for `stand_down` |
| `robonix/primitive/chassis/balance_stand` | `Trigger` | Explicit BalanceStand targeting mode 1 for velocity; separate from StandUp |

`move` and all four direction RPCs reuse Robonix's standard `chassis/MoveCommand` and
`chassis/ExecuteMoveCommand` IDL. Stop reuses the existing `chassis/Stop`
IDL; the local contracts bind the additional status and posture RPCs.
No global IDL is modified. The shared lifecycle Driver is provided by the SDK.
Only the legacy `arm` contract has `llm_callable=false` and no MCP tool.
Enable, movement, posture, stop, disable and status contracts have
`llm_callable=true` and MCP transports for Client/Pilot. The system `move`
contract keeps its existing callable policy. Enable may be called only for
the user's explicit enable request, not inferred from a movement request.

Model visibility does not grant motion permission. Movement and posture
requests require an existing human-enabled session and never invoke the
transport factory. Only explicit human enable may start and arm a backend;
status, stop, disable and lifecycle operations never acquire it.

## Typed MCP transport

MCP tool names are exactly the contract leaves: `enable`, `move`, `forward`, `backward`,
`left`, `right`, `stand_up`, `stand_down`, `lie_down`, `balance_stand`, `stop`,
`disable`, `status` and `motion_status`. Each uses the same existing IDL and
controller as gRPC. Enable uses the typed Trigger request following explicit
Client authorization; `arm` stays a hidden legacy gRPC entry point.

`rbnx codegen --mcp` generates `chassis_mcp`, `std_srvs_mcp` and `std_msgs_mcp`
dataclasses under `rbnx-build/codegen/robonix_mcp_types/`. Build and start
scripts add that directory to `PYTHONPATH` through `mcp_gen`. MCP movement
arguments use `{"command": {"linear_x": 0.03, "duration_sec": 0.5}}`;
direction fields, positive magnitudes and defaults follow the table below.
Trigger/status/stop requests take empty arguments. Typed responses preserve
the controller JSON in `status.data` or `message`.

MCP handlers run the existing synchronous gRPC handler in `asyncio.to_thread`.
Enable, movement and posture each receive an individual Event-based active context.
Cancellation clears that context while shielding the background job so the
controller can finish its own stop/disarm. Cancelling a rejected or waiting
request never unconditionally stops another controller owner. Status, stop and
disable do not acquire action contexts.

Enable MCP succeeds only with `status="enabled"`, `session_enabled=true`,
`guard.ready=true` and `guard.session_enabled=true`, and a guard reporting
neither `faulted=true` nor `guard_state="FAULT"`. Otherwise it raises
`ToolError` with the original JSON evidence, without automatically retrying.

Rejected, cancelled, faulted, unobserved and unconfirmed results raise MCP
`ToolError` with their original JSON evidence. A movement tool succeeds only
on `completed` with acknowledged stop and matching controller evidence:
nonzero motion requires both verified dispatch and observed physical motion;
a zero-only request explicitly reports both false. Posture tool success
requires `action_finished=true` and a dispatched/completed result, with no
invented StopMove acknowledgement. Read-only status succeeds without enabling;
disable may succeed with no backend and no claimed hardware stop.

## Direction RPC parameters

These are registered primitives, not CLI-only aliases. Direction requests
carry `ExecuteMoveCommand_Request(command=MoveCommand(...))` and return the
controller's result JSON unchanged in `response.status.data`.

| RPC | Accepted nonzero fields | Canonical controller command |
| --- | --- | --- |
| `forward` | Positive `linear_x`, optional `duration_sec` | `linear_x=+magnitude` |
| `backward` | Positive `linear_x`, optional `duration_sec` | `linear_x=-magnitude` |
| `forward` | Positive `forward_m` only | `forward_m=+magnitude` |
| `backward` | Positive `forward_m` only | `forward_m=-magnitude` |
| `left` | Positive `linear_y`, optional `duration_sec` | `linear_y=+magnitude` |
| `right` | Positive `linear_y`, optional `duration_sec` | `linear_y=-magnitude` |

The direction selects the sign. A negative magnitude is rejected rather than
reversing the named direction. A zero or omitted speed uses the controller's
configured limit for that direction (by default 0.10 m/s forward/backward,
0.05 m/s left/right); a zero or omitted duration uses its default, initially
0.25 seconds. Tightened controller limits also change these defaults.
An entirely empty direction request therefore requests a bounded burst at those
defaults; it never enables the session.

Distance and velocity/time forms are exclusive: combining `forward_m` with
speed or a nonzero duration is rejected. Lateral RPCs reject `forward_m`;
use `linear_y` plus duration. All other axes, yaw and angle shortcuts are
rejected by direction RPCs, as are NaN, infinity and negative duration.
Use the compatible `move` RPC for controller-supported combined planar
velocity or yaw commands.

After validation and sign conversion, every direction calls
`controller.execute(command, is_active=context.is_active)`. The controller
still enforces speed and duration limits, session authorization, exclusive
ownership, cancellation, state checks and acknowledged stop. The provider
does not clamp oversized requests or infer movement from elapsed time.
For example, a `not_observed` result with `physical_motion_observed=false`
remains a `not_observed` result. Dispatch and stop acknowledgements alone
do not prove that the physical robot moved.

## Default deployment

`native/robonix_manifest.yaml` includes this provider with `allow_motion: false`.
At startup no motion ROS worker, velocity publisher, SDK client or Sport
daemon is started. Before human enable, a movement request returns `motion_disabled`.
Stop reports that no transport exists; it does not claim a physical stop was
acknowledged.

The camera, lidar, IMU and battery provider remains independent of this
package. Human enable lazily acquires the motion runtime through the default
provider; disable revokes and releases that runtime while Core, Pilot, Client
and sensors keep running.

## Commands and limits

The robot is Go2-W. The pinned official SDK's
`example/go2w/go2w_sport_client.cpp` uses the same `go2::SportClient` and
`/lf/sportmodestate`; this source was read, never executed.
Read-only `MotionSwitcherClient::CheckMode` on this device returned `ai-w`.

With an operator beside the robot, say “使能底盘” or “启用底盘运动” in Client.
Pilot may then call enable MCP and report its result. A movement request alone
does not authorize this call. The equivalent CLI is optional:

```bash
bash native/motion.sh enable
bash native/motion.sh status
```

`motion.sh` is a compatibility shell over the primitive CLI. For enable it
first stops a running legacy `robonix-go2w-motion` service, ensures the default
`robonix-go2-native` service is started, waits for its provider with
`wait_motion.py --available`, then invokes the `enable` primitive. Starting
an already active default service does not restart Core. The shell does not
start the legacy motion service or send a posture request.

Human `enable` is the single authorization. It starts transport as needed,
performs internal zero preparation and a confirmed StopMove round trip,
and returns with the session ready. No separate `arm` is needed and no
15-second idle expiry applies. Healthy idle maintains zero-command/Ping
watchdogs. Successful movement cleanup sends StopMove and retains
enable; normal posture cleanup uses the separate posture-finish path
without StopMove. Subsequent requests use the same human authorization.
No posture, mode switch or nonzero velocity is implicit in enable.
A fault, stale state, active request timeout, owner loss or shutdown revokes
the session. Recovery requires a fresh explicit Client enable request or
optional human CLI enable; runtime health recovery never automatically
reenables it, and Pilot must not retry enable on its own.

After a fault, one fresh human enable cleans up the previous backend and
authorizes the replacement. That cleanup must not cancel the new enable
request; an explicit concurrent stop or client cancellation still cancels it.
Enable results update `last_result` so an old action failure is not retained
as if it were the latest command. Current readiness is determined by
`faulted`, `guard.ready`, `guard.can_arm` and `session_enabled`, rather than
command history alone.

Posture failure details preserve the SDK command code through the native
daemon and ROS service into the primitive response. The dispatch diagnostic
includes `posture_attempt_count`, `last_posture_action_id`,
`last_posture_code` and `last_posture_reply_received`. The last field refers
to the daemon IPC reply; it does not itself prove a firmware response.
A service rejection and a service-response timeout have separate error
messages. A rejection still stops/disarms and never retries automatically.

`bash native/chassis.sh stop` and the `stop` RPC cancel the active request and
stop while retaining healthy enable. The separate `disable` Trigger RPC
calls `controller.disable()` to revoke permission; it is not a `stop` alias.
Its success flag requires `status="disabled"` and `session_enabled=false`.
An initially absent backend may successfully remain disabled while reporting
`stop_acknowledged=false` and `hardware_stop_requested=false`: no hardware stop
has been falsely acknowledged.

To revoke motion authorization while leaving Core, Pilot, Client and sensors
running, request “禁用底盘运动” in Client. Optional CLI equivalents call the
same primitive:

```bash
bash native/motion.sh disable
bash native/chassis.sh disable
```

Only one invocation is needed; these are equivalent entry points.

Use official controls to put Go2-W into its locomotion-ready mode before
velocity testing. This device's existing Go2-W integration records StandUp
ending in mode 6 and BalanceStand returning it to mode 1. Our `stand` calls
StandUp only: it never silently calls BalanceStand or RecoveryStand.
The separate `balance_stand` primitive explicitly targets mode 1 for velocity
control and can be requested by Client/Pilot within a human-enabled session;
enable and stand_up never invoke it implicitly. StandUp's target is stand-lock
mode 6, so standing up alone does not make velocity control ready.
Velocity is accepted only in modes 1/3, with state, guard and SDK checks.
Modes 5/6 can qualify for explicit posture but cannot receive velocity.
The site's explicit allowed state modes `[1,3,5,6]` are in the native
manifest; every other mode fails closed.

Use `bash native/chassis.sh ...` from the repository root:

```bash
bash native/chassis.sh status
bash native/chassis.sh stop
```

Examples of bounded movement requests (the current disabled deployment rejects
them without sending anything to the robot):

```bash
bash native/chassis.sh forward --meters 0.05
bash native/chassis.sh backward --meters 0.05
bash native/chassis.sh left --meters 0.02
bash native/chassis.sh right --meters 0.02
bash native/chassis.sh forward --speed 0.04 --duration 0.5
bash native/chassis.sh backward --speed 0.04 --duration 0.5
bash native/chassis.sh left --speed 0.02 --duration 0.5
bash native/chassis.sh right --speed 0.02 --duration 0.5
bash native/chassis.sh turn --degrees 5
bash native/chassis.sh turn --degrees -5
bash native/chassis.sh velocity --vx -0.05 --vy 0.02 --wz 0 --duration 0.5
bash native/chassis.sh crouch
bash native/chassis.sh stand
bash native/chassis.sh balance
```

Each CLI direction calls its own primitive, rather than routing through
`move`. Forward/backward `--meters` send a positive `forward_m`; their
primitive applies the sign. Legacy lateral `--meters` converts positive
distance to positive `linear_y=0.05` m/s and `duration_sec=meters/0.05`;
the selected lateral primitive applies the sign. This is a timed estimate,
not distance feedback. A tightened speed limit or excessive computed duration
is still rejected by the controller. Prefer lateral `--speed` plus `--duration`
to choose the requested speed explicitly.

`--meters` and `--speed` are mutually exclusive. Nonzero `--duration` cannot
be combined with `--meters`. All magnitudes must be positive and finite;
velocity/yaw fields may be signed but must be finite. Empty direction commands
leave speed and duration at zero on the wire so the provider uses current
controller defaults. Velocity duration also defaults to zero on the wire.

`stand` calls `stand_up`; `crouch` calls `lie_down`; `balance` calls the
separate `balance_stand` primitive. Their explicit primitive names are accepted
as CLI aliases. No stand or enable command appends BalanceStand automatically.
RPC timeouts are 30 seconds for enable/arm, 15 seconds for disable,
8 seconds for posture, and 5 seconds for movement/status/stop.

The CLI prints the response JSON unchanged in meaning and returns exit code 2
for rejection, fault, cancellation, unconfirmed stop, `not_observed` or
`not_dispatched`. Nonzero movement also requires both
`dispatch_verified=true` and `physical_motion_observed=true`, plus an
acknowledged stop, for exit code 0. A completed zero-velocity window does not
claim physical movement. Posture success follows the separate
`action_finished` contract, without inventing a StopMove acknowledgement.
RPC transport errors are printed as an error JSON and return exit code 2.

The explicit `native-go2w-manual-v2` profile, when no limits file is present,
accepts forward and reverse at 0.25 m/s, lateral movement at 0.10 m/s, yaw at
0.50 rad/s, and a movement request of at most 2.0 seconds. A zero
`duration_sec` uses the 0.50-second default. The Python hard ceilings are
1.0 m/s forward and reverse, 0.5 m/s lateral, 1.0 rad/s yaw and 3.0 seconds.
The C++ ceilings are 1.0 m/s X, 0.5 m/s Y, 1.0 rad/s yaw and 5000 ms. A limits
file may tighten these, not widen them. See `docs/SAFETY.md`. Vertical, roll
and pitch velocity requests are rejected.
The older commissioning and workstation profiles keep their forward-only limits;
only the separate native profile enables negative X and lateral SDK commands.
Commands use body-frame planar velocity: positive X is forward and positive
Z yaw is counterclockwise by the ROS convention. Physical acceptance of this
exact deployment is still pending.

The standard command modes follow `forward_m`, then `rotate_deg`, then direct
velocity priority. Distance and angle shortcuts convert to **timed bursts**.
There is no validated odometry feedback here: results explicitly set
`distance_verified=false` and `angle_verified=false`. They do not claim the
requested physical distance or angle was reached. A large distance/angle that
would need more than one second is rejected, not silently truncated.

One request owns the controller at a time. A second move is rejected. Stop,
gRPC cancellation, driver deactivation and shutdown cancel the active request.
The controller sends at 20 Hz; every attempted burst ends in a guarded stop
request. An unavailable stop acknowledgement returns `stop_unconfirmed` and
latches the controller instead of returning success.

## Real transport boundary

The real path is:

```text
gRPC move
  -> bounded Python 3.11 controller
  -> separate Foxy Python 3.8 motion worker
  -> /go2/native/cmd_vel
  -> native guarded C++ chassis adapter
  -> existing private IPC + SDK daemon
  -> Unitree SportClient Move/StopMove
```

The new native adapter reuses the inherited SafetyGuard, checksummed same-UID
IPC client, SDK acknowledgement validation, interface ownership and independent
watchdogs. The old adapter remains available for its existing profiles.
Lifecycle initialization and status never launch or arm a Sport daemon.
The default provider starts its guarded runtime only on human enable;
the controller's transport factory owns startup and shutdown. Existing
state, ownership and SDK checks still apply. Build-only native
artifacts are available with:

```bash
bash native/build-motion.sh
```

This compiles both adapters for Foxy, builds the SDK daemon with vendor examples
disabled, and runs only fake-client C++ tests. It executes no hardware binary.
The native SDK profile has fixed envelope arguments of 0.10 / 0.05 / 0.20 and
a 5000 ms whole-action deadline; it starts disarmed and retains its 300 ms
independent watchdog.

Enabling the bridge requires explicit Driver config **and independent process
environment** acknowledgements for motion and operator presence, the exact
acknowledgement phrase and Go2-W mode policy. Network configuration uses
the native interface and robot controller address, currently eth10 and
192.168.123.161 in `native/robonix_manifest.yaml`. The check measures the
direct route and its assigned local source, currently 192.168.123.18.
The reference workstation's .99 address, DNS/IPv6 restrictions and lack of an
Internet route are not requirements for this onboard profile. Existing host
addresses and routes are preserved. The route check does not authenticate DDS.
The explicit `dds-source-lowstate-tick-v1` basis preserves the raw zero
SportModeState payload stamp. It validates original DDS writer timestamps
against elapsed monotonic time in each source's own epoch, plus the
independently advancing millisecond LowState.tick counter. Source/receipt
progress qualifies for at least one second before arming. Duplicates never
refresh liveness; regression, reboot, clock jumps, excessive accumulated
delay and 200 ms stream loss reject control. Absolute hardware measurement
age remains **unverified** and is reported as such. This manual-control
contract is not navigation odometry, UTC synchronization or a restamped
sensor stream. The older payload-time path and upstream profiles are unchanged.

The worker independently requires advancing DDS evidence and LowState.tick,
current raw health with finite valid orientation, an eligible mode and zero
error code, fresh adapter diagnostics,
an explicitly ARMED guard, an armed SDK daemon and one velocity-channel owner.
Only human `enable` (or its compatibility `arm` alias) can call SetBool(true).
Default boot, lifecycle, status, Move and posture never grant permission.
Fault/shutdown immediately records local disarm even if delayed diagnostics
still say ARMED. Normal finish has a separate acknowledged StopMove service;
it fences out queued commands published before the stop, resets the native
action deadline and retains the session for a new request.

Each velocity packet renews a 150 ms worker lease. Worker lease expiry, state
loss or a malformed active command immediately publishes zero and requests
stop/disarm, then latches a fault. The inherited adapter and SDK watchdogs
remain independent. EOF/disconnect also requests stop/disarm.

Normal finish/stop uses `/go2_chassis/finish` and retains permission. Fault
and shutdown use `SetBool(data=false)` for full stop/disarm. An acknowledgement
is not proof of observed physical motion completion. Failed StopMove
confirmation latches the controller and revokes permission.

## Explicit crouch, stand and balance stand

`crouch` calls only `StandDown`; it does not call `Sit`, `Damp` or recovery.
`stand` calls only `StandUp`. The explicit `balance_stand` primitive calls only
`BalanceStand`. Their ROS Trigger services are created only in
the motion-enabled native adapter and are absent in its passive mode.
They require a fresh, healthy, stationary robot, an armed idle controller and
the same single SDK owner. The native posture path is limited to StandUp,
StandDown and BalanceStand; it cannot dispatch dance or
other inherited animations.

Moving and posture requests share one exclusive owner. A posture is dispatched
once, followed by a bounded heartbeat window (StandUp up to 4 seconds,
StandDown up to 3 seconds). Normal posture cleanup uses `finish_posture`,
retaining healthy enable without sending StopMove.
The adapter caps this window at 4.8 seconds; the worker pipe lease and SDK
watchdog remain independent. No lifecycle, startup, shutdown, health check or
timer dispatches posture. Timers only maintain zero-command/Ping freshness or
request stop. A second posture or nonzero velocity cannot overlap it.

The Trigger success flag requires `status` in `dispatched`/`completed`
and `action_finished=true`. Cancellation and fault never report success.
Normal posture cleanup does not require or invent `stop_acknowledged`.
The full controller evidence remains in `message`; RPC success alone does
not prove that the physical body reached the requested posture.
Physical acceptance remains pending with the operator.
No sit, recovery, low-level motor, mapping or navigation capability is added.

## Validation

```bash
bash packages/go2w_chassis_motion/scripts/build.sh
bash native/build-motion.sh
```

These commands generate gRPC stubs and typed MCP dataclasses, and run in-memory controller/state
tests plus the inherited C++ guard/protocol tests. Subprocess tests replace all
ROS modules before running the production worker and check signed velocity,
explicit posture dispatch, exclusivity, command-lease expiry, disconnect and
malformed-command stops. C++ posture tests use fake SDK clients only. These
tests create no real ROS control publisher and call no real posture API.

Provider tests verify all fifteen gRPC and fourteen MCP registrations, typed
request/response wiring, shared direction validation, tool errors and context
cancellation. A positive mock wiring test confirms that full native
configuration does not create a backend until explicit human enable, and that
disable closes it. FastMCP tool calls in these tests stay in memory.

`tests/test_chassis_cli.py` additionally uses fake RPCs to check independent
direction routing, units, finite positive arguments, defaults, timeouts,
evidence-dependent exit status and preserved response JSON. Its shell tests
replace systemctl, the readiness Python and the final CLI shell with temporary
executables, so they neither touch services nor load `native/env.sh`.

For the real gRPC registration check, use the installed SDK Python:

```bash
source native/env.sh
"$GO2_SDK_PYTHON" native/verify_motion.py
```

That check first requires `motion_enabled=false` and `backend_owner=null`.
Enable, arm and all postures are checked only through Atlas discovery and
generated stub existence; none of those RPCs are invoked. Movement rejection
checks use deliberately invalid commands (negative direction magnitudes or
an oversized generic velocity), so a concurrent human enable cannot make
them execute. Stop must not claim a hardware acknowledgement when disabled.
Its report is `rbnx-build/native/motion-disabled-acceptance.json`.

`bash native/check-motion.sh` is subscription/read-only RPC only. It records
the Go2-W identity, original clocks, tick qualification and selected route.
The sensor telemetry page is a preview and explicitly directs operators to
`chassis status` for the motion state.
Primitive-owned session logs are under `rbnx-build/native/primitive-chassis/`;
legacy external sessions use `rbnx-build/native/live/`. Enabled verification
scripts require prior human enable and select logs using `backend_owner`.
They compare Move attempts, verified Move dispatches and posture dispatches
before and after the check, preserving the backend evidence instance.
Current-interval SDK logs also cover verified Move replies and BalanceStand;
an old one-way log string alone is insufficient. No raw camera/cloud or
credentials are written there. Successful dispatch/stop tests alone do not
constitute physical acceptance of wheel traction, lateral motion or posture.
