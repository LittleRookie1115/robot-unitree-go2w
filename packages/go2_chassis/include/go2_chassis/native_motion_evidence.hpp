#pragma once

#include <cstdint>

#include "go2_chassis/protocol.hpp"
#include "go2_chassis/safety_guard.hpp"

namespace go2_chassis {

// Process-local, monotonic counters. These describe the command path; they
// never claim measured displacement. A native successful Move reply is backed
// by the SDK's matching request AND response/status witness. Ping and Stop must
// not call these methods or contribute to the Move counters.
struct NativeMotionEvidence {
  static bool Nonzero(const Velocity &velocity) {
    return velocity.vx != 0.0 || velocity.vy != 0.0 || velocity.wz != 0.0;
  }

  void Received(const Velocity &velocity, std::uint64_t received_ns) {
    ++velocity_received_count;
    if (Nonzero(velocity)) ++velocity_nonzero_received_count;
    last_velocity_received = velocity;
    last_velocity_received_monotonic_ns = received_ns;
  }

  void Accepted(const Velocity &velocity) {
    ++velocity_accepted_count;
    if (Nonzero(velocity)) ++velocity_nonzero_accepted_count;
  }

  void MoveAttempt(const Velocity &velocity, std::uint64_t requested_ns) {
    ++sdk_move_attempt_count;
    last_sdk_move = velocity;
    last_sdk_move_request_monotonic_ns = requested_ns;
    last_sdk_move_reply_received = false;
    last_sdk_move_reply_code =
        static_cast<std::int32_t>(ReplyCode::kInternalError);
    last_sdk_move_remote_response_verified = false;
  }

  void MoveReply(bool ok, bool reply_received, std::int32_t code,
                 std::uint64_t completed_ns) {
    last_sdk_move_reply_received = reply_received;
    last_sdk_move_reply_code = code;
    last_sdk_move_remote_response_verified = ok && reply_received && code == 0;
    if (last_sdk_move_remote_response_verified && Nonzero(last_sdk_move)) {
      ++sdk_move_dispatch_count;
      last_sdk_move_dispatch_monotonic_ns = completed_ns;
    } else {
      ++sdk_move_failure_count;
    }
  }

  std::uint64_t velocity_received_count{0};
  std::uint64_t velocity_nonzero_received_count{0};
  std::uint64_t velocity_accepted_count{0};
  std::uint64_t velocity_nonzero_accepted_count{0};
  std::uint64_t velocity_stop_fence_rejected_count{0};
  Velocity last_velocity_received{};
  std::uint64_t last_velocity_received_monotonic_ns{0};
  std::uint64_t sdk_move_attempt_count{0};
  std::uint64_t sdk_move_dispatch_count{0};
  std::uint64_t sdk_move_failure_count{0};
  Velocity last_sdk_move{};
  std::uint64_t last_sdk_move_request_monotonic_ns{0};
  std::uint64_t last_sdk_move_dispatch_monotonic_ns{0};
  std::int32_t last_sdk_move_reply_code{
      static_cast<std::int32_t>(ReplyCode::kInternalError)};
  bool last_sdk_move_reply_received{false};
  bool last_sdk_move_remote_response_verified{false};
};

}  // namespace go2_chassis
