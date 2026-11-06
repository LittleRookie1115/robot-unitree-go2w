# Repository safety rules

Current authorization policy (2026-10-22): this overrides older terminal-only,
gRPC-only or non-model-callable restrictions on `enable` below. A user explicitly
saying “使能底盘” or “启用底盘运动” in Client grants human authorization for Pilot
to call the typed `enable` MCP tool (`llm_callable=true`). The CLI is an optional
equivalent, not a prerequisite. “前进” or another movement request does not
authorize enabling; Pilot must not infer permission or automatically retry
enable after a fault. Recovery needs a fresh explicit user enable request.
Default boot stays disabled. Enable performs preparation only, with no implicit
posture, mode switch or nonzero motion. The catalog has 15 gRPC / 14 MCP
primitives; only legacy `arm` stays hidden (`llm_callable=false`, no MCP).

Primitive lifecycle update on 2026-10-17: default Core now registers all chassis
RPCs with no control subprocess at startup. A human primitive `enable` lazily
creates the owned SDK/adapter/worker and performs internal arm; `disable`
revokes and releases them while Core/Pilot/Client stay running. `motion.sh`
is a compatibility wrapper over these RPCs. Native Move requires a verified
response-bearing firmware reply; "completed" additionally requires observed
raw Sport velocity feedback. Missing feedback is `not_observed`, not proof
of displacement. Explicit `balance_stand` prepares mode 1 from stand-lock;
it must never run automatically. Normal posture completion releases its
bounded action only after both state witnesses see the fresh terminal mode,
without StopMove. Cancellation/fault/shutdown retain StopMove/disarm.
Action/status/stop/disable primitives also have MCP transports for Pilot.
The initial gRPC-only restriction on enable is superseded by the current
Client authorization policy above. Ordinary movement/posture requests never
construct a backend or grant permission; before explicit human enable they
must be rejected.

Operator session policy introduced on 2026-10-17 overrides the old per-action
arm flow; its authorization entry points are updated by the policy above.
An explicit human enable through Client or the optional CLI grants the session.
It includes the internal zero preparation/arm handshake. No separate arm is
required, no 15-second idle revocation applies, and successful action completion
or explicit StopMove retains a healthy enable. `disable`, fault, active-command
timeout, lost state/ownership or shutdown revokes permission. Fault recovery
requires a fresh human enable and must never happen automatically.
Default boot remains motion-disabled. Tests never issue nonzero real commands
or posture. Pilot may relay an explicit Client enable request, but must never
enable autonomously or infer authorization from an action request.

The operator corrected the hardware identity: the local robot is **Go2-W**,
not the upstream reference Go2. Keep the repository/path for continuity.
Use the official pinned SDK's `example/go2w/go2w_sport_client.cpp` as a
READ-ONLY interface reference: it shares go2::SportClient, but that does not
validate this firmware's posture behavior or lateral locomotion. Never run
the example, which can issue a posture on its first input. Do not advertise
the old 12-leg-joint Go2 URDF as a validated Go2-W model. Native motion uses
the separate `native-go2w-manual-v1` profile.

Native Go2-W status on 2026-10-16: read-only CheckMode confirmed `ai-w`;
DDS/tick qualification, explicit zero-speed arm/StopMove, 15-second idle
disarm, default-disabled RPC rejection, and Client's four-wheel model path
were verified. No nonzero Move or posture API was dispatched for acceptance.
Do not describe wheel traction, lateral motion or posture completion as
physically accepted. The explicit manual state basis preserves raw zero
payload stamps and reports absolute measurement age as unverified; see the
tested `native_state_clock.hpp` contract and native motion README.
The old 15-second/per-action arm behavior verified on 2026-10-16 is superseded
by the operator policy above. `native/motion.sh enable` now starts and enables
the guarded session, while `motion.sh disable` restores the sensor service.
Default manifests remain motion-disabled and no motion service autostarts.

This is a local native Ubuntu 20.04 / ROS 2 Foxy / ARM64 fork. The default
root scripts select `native/`; read `native/README.md` before running them.
The inherited supervised Humble results below belong to the reference
deployment. They are not motion acceptance for this local Foxy profile.
This device's `/dog_odom` has an invalid zero quaternion; never advertise it
as navigation odometry or replace it with an invented orientation.
The operator requested native reverse/lateral motion and explicit StandUp /
StandDown interfaces. These are implemented only behind the disabled-by-default
native motion profile. This request does not authorize real action tests
without the operator present. Posture tests use fake clients only.
The operator clarified that the upstream repository is a reference, not this
host's deployment specification. In the native profile, measure the directly
connected robot route and selected source address; do not impose the reference
PC's 192.168.123.99, no-default-route, DNS or IPv6 settings on this onboard NX.
Native manual primitives do not depend on the upstream Nav2/map acceptance
stages. Keep the motion default off, explicit session authorization, bounded commands,
single owner, fresh-state validation and independent stop/watchdog paths.
The explicit human enable includes internal arm; it is not an extra user step.

This repository integrates Robonix with a physical Unitree Go2. Safety takes
precedence over convenience.

1. Motion is disabled by default. Never change a public/default configuration
   to arm motion automatically.
2. Never call StandUp, StandDown, Sit, RecoveryStand, Damp, BalanceStand or any
   other posture API from boot, shutdown, tests, or health checks.
3. Never use or expose Unitree low-level motor control or `/lowcmd`.
4. Never run Unitree motion examples, including `sport_mode_ctrl`,
   `go2_sport_client`, `go2_stand_example`, and `low_level_ctrl`.
5. `/cmd_vel` may only be consumed by the guarded chassis adapter. It must not
   be published by setup, audit, test, or read-only scripts.
6. Enabling motion requires the applicable checks in `docs/SAFETY.md`.
   Default deployment startup never arms; an explicit Client enable request
   or equivalent optional CLI authorizes the internal preparation and arm.
7. Every command path has an independent watchdog and must request StopMove on
   stale input, fault, disarm, deactivate, or shutdown.
8. Sensor/network diagnostics are read-only. All `ros2 topic echo` operations
   require a finite timeout.
9. Do not change host networking, install packages with sudo/apt, or connect to
   a physical robot without the operator's explicit approval for that step.
10. Do not store passwords, tokens, API keys, SSH credentials, subscription
    URLs, device handover PDFs, maps containing sensitive data, or `.env`.
11. Preserve the Unitree BSD-3-Clause notice for the model in
    `packages/go2_description/`.
12. Hardware-dependent dimensions, frame transforms, QoS, topic names and
    modes must be measured and recorded before supervised motion acceptance.

## Current validated phase (2026-07-31)

Supervised wireless motion, StopMove, human takeover, generation-1
Mapping/localization, and real Client Chinese-voice long-distance outbound and
autonomous-return Nav2 runs have succeeded. The operator retained video of the
successful physical round trip and accepted the small final placement
deviation as non-blocking. A fresh conversation may resume the existing
persistent stack without repeating a no-motion-only or centimeter-scale stage,
and it must not create new permit bundles or approval artifacts merely because
the conversation is new.

The local and global Nav2 costmaps consume the live MID-360-derived 2D scan and
have obstacle and inflation layers enabled. This proves that the laser obstacle
chain was active during physical navigation; it does not claim a dedicated
placed-obstacle avoidance trial. D435i remains a read-only preview source and
is not a Nav2 costmap input.

The implementation is not restricted to upstream-only code. The local dirty
integration and the supplied same-Go2 ROS1 archive are valid adaptation
references. Keep the existing disarmed-at-start behavior and the working
stop/cancel/watchdog/single-owner path; avoid adding new hard gates unless they
address a reproduced failure.
