#pragma once

#include <cmath>
#include <cstdint>
#include "go2_chassis/protocol.hpp"

namespace go2_chassis {

inline constexpr const char *kNativeMotionProfile = "native-go2w-manual-v2";
inline constexpr const char *kNativeCommandTopic = "/go2/native/cmd_vel";
inline constexpr const char *kNativeStateBasis = "dds-source-lowstate-tick-v1";
// Absolute hard ceilings. The runtime limits file may only tighten these.
inline constexpr double kNativeMaxVx = 1.0;
inline constexpr double kNativeMaxVy = 0.5;
inline constexpr double kNativeMaxWz = 1.0;
inline constexpr std::uint64_t kNativeMaxMotionMs = 5'000U;
inline constexpr double kNativePostureWindowSec = 4.8;

// Unitree's SportModeState mode enum: idle/balance/pose/walk are 0..3;
// 5 (lie down), 6 (joint lock), 7 (damping), recovery and other modes must
// never receive a native velocity command. No mode-switch API is implicit.
inline bool NativeMovementMode(std::uint8_t mode) { return mode == 1U || mode == 3U; }

// Only commands published after a confirmed stop/posture completion may begin
// the next action.
// DDS source time here belongs to the local ROS command writer, not the
// robot's independently qualified state clock.
inline bool NativeCommandAfterStop(std::uint64_t source_ns, std::uint64_t stop_ns) {
  return source_ns != 0U && source_ns > stop_ns;
}

inline bool NativePostureAllowed(SportAction action) {
  return IsKnownPostureAction(static_cast<std::uint8_t>(action));
}

inline bool NativeMoveReplyAcknowledged(std::int32_t code, std::uint16_t flags) {
  return code == static_cast<std::int32_t>(ReplyCode::kOk) &&
         (flags & kReplyNativeMoveRemoteAck) != 0U;
}

inline std::uint8_t NativePostureTargetMode(SportAction action) {
  switch (action) {
    case SportAction::kStandDown: return 5U;
    case SportAction::kStandUp: return 6U;
    case SportAction::kBalanceStand: return 1U;
    default: return 255U;
  }
}

inline bool NativePostureModeReached(SportAction action, std::uint8_t mode) {
  return NativePostureAllowed(action) && mode == NativePostureTargetMode(action);
}

// A posture that is already held is a successful no-op. Do not dispatch it
// again: several Go2-W firmware paths answer a duplicate posture with a
// matched response status of -1, which would otherwise be treated as a fault.
inline bool NativePostureAlreadyReached(SportAction action, std::uint8_t mode) {
  return NativePostureModeReached(action, mode);
}

// Mode 1 is BalanceStand, not the locked StandUp mode 6. It must be allowed
// to transition to mode 6; otherwise the robot can be stuck in mode 1 where
// the firmware does not accept StandDown directly. Mode 3 is treated as
// already standing; unknown modes remain dispatchable.
inline bool NativePostureTransitionClearlyInvalid(SportAction action,
                                                  std::uint8_t mode) {
  switch (action) {
    case SportAction::kStandUp:
      return mode == 3U;
    case SportAction::kStandDown:
      return mode == 1U;
    case SportAction::kBalanceStand:
      return mode == 5U;
    default:
      return false;
  }
}

inline const char *NativePostureRecommendation(SportAction action,
                                               std::uint8_t mode) {
  if (action == SportAction::kStandUp && mode == 3U) {
    return "already_standing";
  }
  if (action == SportAction::kBalanceStand && mode == 5U) {
    return "stand_up_first";
  }
  if (action == SportAction::kStandDown && mode == 1U) {
    return "stand_up_first";
  }
  return "";
}

inline bool NativeVelocityAllowed(double vx, double vy, double wz,
                                  double max_vx, double max_vy, double max_wz,
                                  bool posture_active = false) {
  if (!std::isfinite(vx) || !std::isfinite(vy) || !std::isfinite(wz) ||
      max_vx <= 0.0 || max_vy <= 0.0 || max_wz <= 0.0 ||
      std::fabs(vx) > max_vx || std::fabs(vy) > max_vy ||
      std::fabs(wz) > max_wz) {
    return false;
  }
  return !posture_active || (vx == 0.0 && vy == 0.0 && wz == 0.0);
}

}  // namespace go2_chassis
