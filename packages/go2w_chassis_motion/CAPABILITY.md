# Native Go2-W chassis primitives

Current authorization policy (2026-10-22): provider `go2w_chassis_motion`
registers 15 gRPC / 14 typed MCP capabilities in `robonix/primitive/chassis`.
A user explicitly saying “使能底盘” or “启用底盘运动” in Client grants human
authorization for Pilot to call enable MCP. Do not infer this consent from
“前进” or any other action, or retry enable automatically after a fault.
Default initialization stays disabled; enable has no implicit posture or
nonzero motion. The CLI remains an optional equivalent.
Only legacy `arm` is hidden (`llm_callable=false`, no MCP). Enable and all
other primitives are model-visible with MCP transports; the system `move`
keeps its existing callable policy.

| Capability suffix | Request / response IDL | Behavior |
| --- | --- | --- |
| `enable` | `std_srvs/Trigger` | Call `controller.enable(is_active=...)` only for explicit Client enable consent or optional human CLI |
| `disable` | `std_srvs/Trigger` | Call `controller.disable()`; revoke authorization |
| `arm` | `std_srvs/Trigger` | Compatible human enable alias |
| `forward` / `backward` | `chassis/ExecuteMoveCommand` | Positive `linear_x` speed and duration, or positive `forward_m` distance |
| `left` / `right` | `chassis/ExecuteMoveCommand` | Positive `linear_y` speed and duration |
| `move` | `chassis/ExecuteMoveCommand` | Existing bounded canonical planar command |
| `stop` | `chassis/Stop` | Stop current action while retaining healthy enable |
| `status` / `motion_status` | `std_srvs/Trigger` | Read controller, authorization and guard state |
| `stand_up` | `std_srvs/Trigger` | Explicit StandUp targeting stand-lock mode 6 |
| `stand_down` / `lie_down` | `std_srvs/Trigger` | Explicit `controller.posture("stand_down")` |
| `balance_stand` | `std_srvs/Trigger` | Explicit BalanceStand targeting mode 1 for velocity control |

MCP names match all capability suffixes except hidden legacy `arm`, including
`enable` and separate `status`/`motion_status`. Generated `chassis_mcp`/`std_srvs_mcp`/`std_msgs_mcp`
dataclasses use the same IDL as gRPC. Movement arguments contain `command`;
empty Trigger/stop requests take no fields. Responses retain controller JSON
in typed `status.data` or `message`.

The MCP bridge invokes existing gRPC handlers in background threads and keeps
the same controller and direction validation. Each enable/movement/posture owns an
Event-based context; request cancellation clears only its Event and shields
the backend job while controller cleanup finishes. Cancelling an unrelated
request never blindly stops the current owner.

Enable MCP success strictly requires `status="enabled"`, `session_enabled=true`,
`guard.ready=true`, `guard.session_enabled=true` and a guard with neither
`faulted=true` nor `guard_state="FAULT"`. Any other result is a `ToolError`
preserving the original JSON evidence; it does not trigger an automatic retry.

Failure results, including `not_observed`, `not_dispatched`, `rejected`,
`cancelled`, `fault` and `stop_unconfirmed`, are MCP tool errors with JSON
evidence. Movement success requires `completed`, acknowledged stop and both
controller witnesses true for nonzero motion (both explicitly false for a
zero-only request). Posture requires a dispatched/completed result and
`action_finished=true`; a missing StopMove acknowledgement is valid for
normal posture completion.

Direction selects the sign: forward/backward use positive/negative X,
left/right use positive/negative Y. Negative input magnitudes are rejected.
Zero or omitted velocity selects that direction's current controller speed
limit; zero or omitted duration selects the controller's default duration.
Initial defaults are 0.10 m/s longitudinal, 0.05 m/s lateral and 0.25 seconds.
The controller may tighten those values, never widen the native envelope.

`forward_m` is supported only for forward/backward, where a positive input
maps to the direction's signed canonical distance shortcut. Combining distance
with velocity or a nonzero duration is rejected. Other axes, angle/yaw fields,
nonfinite numbers and negative durations are rejected by direction handlers.
All canonical commands pass through `controller.execute(..., is_active=...)`;
the provider never bypasses controller limits, single ownership, guard checks,
cancellation or stop handling.

Movement response JSON in `status.data` passes through controller evidence,
including `dispatch_verified` and `physical_motion_observed`. A result such as
`not_observed` must not be treated as proof of movement. Distance shortcuts
are timed requests and do not establish observed physical travel.

`disable` is distinct from `stop`: its success flag requires
`status="disabled"` and `session_enabled=false`. If no backend existed,
`hardware_stop_requested=false` and `stop_acknowledged=false` remain valid
evidence; no hardware acknowledgement is invented.

Posture success requires `status` in `dispatched`/`completed` and
`action_finished=true`; cancellation or fault remains unsuccessful.
Normal posture cleanup uses the controller's posture-finish path, not a
StopMove acknowledgement. StandUp does not implicitly call BalanceStand.
StandUp targets locked standing in mode 6. Velocity control requires a separate
explicit `balance_stand` request targeting mode 1; Client/Pilot may request it
within an existing human-enabled session. Neither stand_up nor enable
automatically appends BalanceStand.

Model-visible movement and posture RPCs still require prior human enable in
the controller; an ordinary action never triggers the backend factory.
Status, stop and disable never create or arm a backend. No lifecycle,
health check or direction handler grants or restores authorization.
Determine current readiness from `faulted`, `guard.ready`, `guard.can_arm` and
`session_enabled`. `last_result` records the most recent command, including
human enable and its rejection/cancellation; a historical failure is not proof
of a current hardware fault. If `faulted=false`, `guard.can_arm=true` and
`session_enabled=false`, explain that an explicit Client enable request is
required. Once the user says “使能底盘” or “启用底盘运动”, Pilot may call enable
MCP; that fresh request may rebuild a faulted native backend. Never infer
consent from movement, retry faults automatically, or use shell tools to
bypass this request boundary. A human may still choose the equivalent CLI.
Native posture failures preserve the SDK command result in
`dispatch.last_posture_code` and the ROS service detail in the error message.
`last_posture_reply_received` describes the local SDK daemon's IPC reply, not
independent proof of a firmware response; use the SDK audit log for the latter.
This interface documentation does not assert physical acceptance of posture,
lateral traction or travel distance on this robot.

The local CLI uses each direction's own primitive. Forward/backward accept
positive `--meters`, or positive `--speed` (m/s) with `--duration` (s).
Left/right primarily use positive `--speed` with duration; their legacy
positive `--meters` shortcut requests 0.05 m/s for `meters/0.05` seconds
and never claims measured distance. Empty direction requests use the
controller defaults. Contradictory options, negative magnitudes and nonfinite
values are rejected before RPC dispatch.

CLI `stand`, `crouch` and `balance` call `stand_up`, `lie_down` and
`balance_stand` respectively. `disable` calls its own revocation primitive.
Enable/arm wait up to 30 seconds, disable 15 seconds and posture 8 seconds.
The CLI preserves result JSON and exits nonzero for `not_observed`,
`not_dispatched`, rejection, cancellation, fault or unconfirmed stop.
Nonzero movement requires verified dispatch, observed physical motion and
an acknowledged stop for exit code 0.

`native/motion.sh enable` ensures the default service is running and waits for
provider availability before invoking the enable RPC. It stops a running
legacy motion service first, and does not restart an active Core or start
the old motion service. `motion.sh disable` invokes primitive disable while
Core, Pilot and Client stay running. Other arguments pass through to the CLI.
