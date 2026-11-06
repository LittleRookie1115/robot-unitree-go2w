#include <chrono>
#include <array>
#include <cmath>
#include <cstdlib>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "diagnostic_msgs/msg/diagnostic_array.hpp"
#include "diagnostic_msgs/msg/diagnostic_status.hpp"
#include "geometry_msgs/msg/twist.hpp"
#include "rclcpp/rclcpp.hpp"
#include "std_srvs/srv/set_bool.hpp"
#include "std_srvs/srv/trigger.hpp"
#include "unitree_go/msg/sport_mode_state.hpp"
#include "unitree_go/msg/low_state.hpp"
#include "go2_chassis/native_profile.hpp"
#include "go2_chassis/native_motion_evidence.hpp"
#include "go2_chassis/native_state_clock.hpp"
#include "go2_chassis/safety_guard.hpp"
#include "go2_chassis/seqpacket_client.hpp"

namespace go2_chassis {
namespace {

double Steady() {
  return std::chrono::duration<double>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
}

bool EnvironmentIs(const char *key, const char *value) {
  const auto *actual = std::getenv(key);
  return actual != nullptr && std::string(actual) == value;
}

class NativeAdapter final : public rclcpp::Node {
 public:
  NativeAdapter() : Node("go2_chassis_adapter") {
    GuardConfig config;
    config.allow_motion = declare_parameter<bool>("allow_motion", false);
    config.allow_reverse = config.allow_motion;
    config.max_vx = declare_parameter<double>("max_vx", kNativeMaxVx);
    config.max_vy = declare_parameter<double>("max_vy", kNativeMaxVy);
    config.max_wz = declare_parameter<double>("max_wz", kNativeMaxWz);
    if (!(config.max_vx > 0.0 && config.max_vx <= kNativeMaxVx) ||
        !(config.max_vy > 0.0 && config.max_vy <= kNativeMaxVy) ||
        !(config.max_wz > 0.0 && config.max_wz <= kNativeMaxWz)) {
      throw std::runtime_error(
          "native velocity limits must be positive and within the hard ceiling");
    }
    max_vx_ = config.max_vx;
    max_vy_ = config.max_vy;
    max_wz_ = config.max_wz;
    config.command_timeout_sec = 0.20;
    config.state_timeout_sec = 0.20;
    state_basis_ = declare_parameter<std::string>("state_time_basis", "payload");
    if (state_basis_ != "payload" && state_basis_ != kNativeStateBasis) {
      throw std::runtime_error("unknown native state time basis");
    }
    const auto modes = declare_parameter<std::vector<std::int64_t>>("allowed_modes", {255});
    config.allowed_modes.clear();
    for (const auto mode : modes) {
      if (mode < 0 || mode > 255) {
        throw std::runtime_error("allowed_modes must contain uint8 values");
      }
      config.allowed_modes.insert(static_cast<std::uint8_t>(mode));
    }
    if (config.allow_motion) {
      if (!EnvironmentIs("GO2_ALLOW_MOTION", "true") ||
          !EnvironmentIs("GO2_OPERATOR_PRESENT", "true") ||
          !EnvironmentIs("GO2_SAFETY_ACK", "I_UNDERSTAND_GO2_CAN_MOVE") ||
          config.allowed_modes.empty() || config.allowed_modes.count(255U) != 0U) {
        throw std::runtime_error("explicit operator/motion gates and audited modes are required");
      }
      const auto *audited = std::getenv("GO2_ALLOWED_MODES");
      std::set<std::uint8_t> environment_modes;
      if (audited != nullptr) {
        std::stringstream stream(audited);
        std::string value;
        while (std::getline(stream, value, ',')) {
          std::size_t consumed = 0U;
          const int mode = std::stoi(value, &consumed);
          if (consumed != value.size() || mode < 0 || mode > 254) {
            throw std::runtime_error("invalid audited mode");
          }
          environment_modes.insert(static_cast<std::uint8_t>(mode));
        }
      }
      if (environment_modes != config.allowed_modes) {
        throw std::runtime_error("parameter modes must match independent audited process modes");
      }
      if (state_basis_ == kNativeStateBasis &&
          !EnvironmentIs("GO2_STATE_TIME_BASIS", kNativeStateBasis)) {
        throw std::runtime_error("native source clock parameter and environment must agree");
      }
    }
    enabled_ = config.allow_motion;
    allowed_modes_ = config.allowed_modes;
    guard_ = std::make_unique<SafetyGuard>(config);
    diagnostics_ = create_publisher<diagnostic_msgs::msg::DiagnosticArray>("/diagnostics", 10);
    state_sub_ = create_subscription<unitree_go::msg::SportModeState>(
        "/sportmodestate", rclcpp::SensorDataQoS().keep_last(1),
        [this](unitree_go::msg::SportModeState::SharedPtr message,
               const rclcpp::MessageInfo &info) {
          OnState(*message, info.get_rmw_message_info().source_timestamp, false);
        });
    state_fallback_sub_ = create_subscription<unitree_go::msg::SportModeState>(
        "/lf/sportmodestate", rclcpp::SensorDataQoS().keep_last(1),
        [this](unitree_go::msg::SportModeState::SharedPtr message,
               const rclcpp::MessageInfo &info) {
          OnState(*message, info.get_rmw_message_info().source_timestamp, true);
        });
    if (state_basis_ == kNativeStateBasis) {
      low_sub_ = create_subscription<unitree_go::msg::LowState>(
          "/lowstate", rclcpp::SensorDataQoS().keep_last(1),
          [this](unitree_go::msg::LowState::SharedPtr message,
                 const rclcpp::MessageInfo &info) {
            native_clock_.ObserveLow(message->tick,
                info.get_rmw_message_info().source_timestamp, SteadyNs());
            if (daemon_armed_ && !native_clock_.fault().empty()) {
              Fault(native_clock_.fault());
            }
          });
    }
    if (enabled_) {
      ipc_ = std::make_unique<SeqpacketClient>(
          declare_parameter<std::string>("sdk_socket", "/tmp/go2-native/sport.sock"));
      command_sub_ = create_subscription<geometry_msgs::msg::Twist>(
          kNativeCommandTopic, rclcpp::QoS(1).reliable(),
          [this](geometry_msgs::msg::Twist::SharedPtr message,
                 const rclcpp::MessageInfo &info) {
            const Velocity requested{message->linear.x, message->linear.y,
                                     message->angular.z};
            motion_evidence_.Received(requested, SteadyNs());
            const auto source = info.get_rmw_message_info().source_timestamp;
            if (source <= 0) {
              Fault("local command DDS source time is missing");
              return;
            }
            if (!NativeCommandAfterStop(static_cast<std::uint64_t>(source),
                                        completed_stop_ns_)) {
              ++motion_evidence_.velocity_stop_fence_rejected_count;
              return;  // queued commands from the completed action cannot resume it
            }
            if (ValidateSourceStamp(source, now().nanoseconds(), 200'000'000ULL,
                                    50'000'000ULL) != SourceStampFreshness::kFresh) {
              Fault("local command DDS source time is stale or in the future");
              return;
            }
            if (!NativeVelocityAllowed(message->linear.x, message->linear.y,
                                       message->angular.z, max_vx_, max_vy_,
                                       max_wz_, posture_active_) ||
                message->linear.z != 0.0 || message->angular.x != 0.0 ||
                message->angular.y != 0.0 ||
                ((message->linear.x != 0.0 || message->linear.y != 0.0 ||
                  message->angular.z != 0.0) && !NativeMovementMode(last_mode_))) {
              Fault("velocity request exceeded native envelope or overlapped posture");
              return;
            }
            guard_->UpdateCommand(Steady(), requested);
            motion_evidence_.Accepted(requested);
          });
      arm_ = create_service<std_srvs::srv::SetBool>(
          "/go2_chassis/arm",
          [this](std::shared_ptr<std_srvs::srv::SetBool::Request> request,
                 std::shared_ptr<std_srvs::srv::SetBool::Response> response) {
            if (request->data) {
              if (!StateFresh() || !stationary_) {
                response->success = false;
                response->message = "fresh valid stationary robot state is required";
                return;
              }
              response->success = guard_->RequestArm(Steady(), &response->message);
            } else {
              guard_->RequestDisarm(Steady(), &response->message);
              response->success = StopAndDisarm();
            }
          });
      stand_up_ = PoseService("/go2_chassis/stand_up", SportAction::kStandUp);
      stand_down_ = PoseService("/go2_chassis/stand_down", SportAction::kStandDown);
      balance_stand_ = PoseService("/go2_chassis/balance_stand", SportAction::kBalanceStand);
      finish_ = create_service<std_srvs::srv::Trigger>(
          "/go2_chassis/finish",
          [this](std::shared_ptr<Trigger::Request>,
                 std::shared_ptr<Trigger::Response> response) {
            if (!daemon_armed_ || guard_->state() != GuardState::kArmed) {
              response->success = false;
              response->message = "session is not enabled";
              Diagnostics();
              return;
            }
            completed_stop_ns_ = static_cast<std::uint64_t>(now().nanoseconds());
            guard_->RequestStop(Steady());
            posture_active_ = false;
            response->success = Exchange(CommandOp::kStop);
            if (!response->success || !StateFresh()) {
              response->success = false;
              response->message = "stop/state confirmation failed; session revoked";
              Fault(response->message);
              Diagnostics();
              return;
            }
            // Also fence commands queued while the synchronous StopMove RPC
            // was in flight, then discard the previous slew output entirely.
            completed_stop_ns_ = static_cast<std::uint64_t>(now().nanoseconds());
            guard_->RequestStop(Steady());
            output_stopped_ = true;
            response->message = "StopMove acknowledged; operator enable retained";
            // Publish the final Move evidence and new stop counter before the
            // service returns; consumers need not wait for a timer callback.
            Diagnostics();
          });
      finish_posture_ = create_service<Trigger>(
          "/go2_chassis/finish_posture",
          [this](std::shared_ptr<Trigger::Request>,
                 std::shared_ptr<Trigger::Response> response) {
            if (!daemon_armed_ || guard_->state() != GuardState::kArmed ||
                !posture_active_) {
              response->success = false;
              response->message = "no enabled native posture is active";
              Diagnostics();
              return;
            }
            if (posture_idempotent_) {
              // The target mode was already held when the request arrived.
              // Re-check it at finish: the robot may have changed mode during
              // the controller's bounded wait. A local acknowledgement is
              // only valid while the target mode still holds.
              if (!NativePostureModeReached(active_posture_action_, last_mode_)) {
                posture_active_ = false;
                posture_idempotent_ = false;
                posture_deadline_ = 0;
                response->success = false;
                response->message =
                    "posture_target_mode_changed; current_mode=" +
                    std::to_string(last_mode_) +
                    "; target_mode=" +
                    std::to_string(NativePostureTargetMode(active_posture_action_));
                Diagnostics();
                return;
              }
              // Finish is a local acknowledgement: no SDK posture or finish
              // RPC was sent, and the operator's session stays armed.
              posture_active_ = false;
              posture_idempotent_ = false;
              posture_deadline_ = 0;
              output_stopped_ = true;
              ++sdk_posture_finish_count_;
              response->success = true;
              response->message =
                  "posture already in target mode; no command or finish RPC sent";
              Diagnostics();
              return;
            }
            if (Steady() >= posture_deadline_ || !PostureTerminalReady()) {
              response->success = false;
              response->message = "posture has not reached a fresh terminal mode";
              Fault(response->message);
              Diagnostics();
              return;
            }
            response->success = Exchange(CommandOp::kFinishPosture, {},
                                         active_posture_action_);
            if (!response->success || !PostureTerminalReady()) {
              response->success = false;
              response->message = "independent SDK posture-state confirmation failed";
              Fault(response->message);
              Diagnostics();
              return;
            }
            completed_stop_ns_ = static_cast<std::uint64_t>(now().nanoseconds());
            guard_->RequestStop(Steady());
            posture_active_ = false;
            posture_deadline_ = 0;
            output_stopped_ = true;
            ++sdk_posture_finish_count_;
            response->message =
                "fresh terminal mode confirmed; no StopMove sent; operator enable retained";
            // This publishes sdk_posture_finish_count, not stop_ack_count:
            // normal posture completion intentionally sends no StopMove.
            Diagnostics();
          });
      control_timer_ = create_wall_timer(std::chrono::milliseconds(20), [this]() { Tick(); });
    }
    diagnostic_timer_ = create_wall_timer(
        std::chrono::milliseconds(100), [this]() { Diagnostics(); });
    RCLCPP_INFO(get_logger(), "Native chassis profile ready; configured=%s, armed=false",
                enabled_ ? "true" : "false");
  }

  ~NativeAdapter() override {
    if (enabled_ && daemon_armed_) {
      (void)StopAndDisarm();
    }
  }

 private:
  using Trigger = std_srvs::srv::Trigger;

  static std::uint64_t SteadyNs() {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
  }

  bool Exchange(CommandOp operation, const Velocity &velocity = {},
                SportAction action = SportAction::kHello) {
    if (operation == CommandOp::kSportAction) {
      ++posture_attempt_count_;
      last_posture_action_id_ = static_cast<std::uint8_t>(action);
      last_posture_code_ = static_cast<std::int32_t>(ReplyCode::kInternalError);
      last_posture_reply_received_ = false;
    }
    if (!ipc_) {
      return false;
    }
    std::int32_t code = static_cast<std::int32_t>(ReplyCode::kInternalError);
    std::string error;
    if (operation == CommandOp::kMove) {
      motion_evidence_.MoveAttempt(velocity, SteadyNs());
    }
    bool ok = ipc_->Exchange(operation, velocity, &code, &error, action);
    if (operation == CommandOp::kMove) {
      if (ok && !NativeMoveReplyAcknowledged(code, ipc_->last_reply_flags())) {
        ok = false;
        code = static_cast<std::int32_t>(ReplyCode::kInternalError);
        error = "SDK daemon did not attest a matching native Move response";
      }
      motion_evidence_.MoveReply(ok, ipc_->last_reply_received(), code, SteadyNs());
    } else if (operation == CommandOp::kStop && ok) {
      ++stop_ack_count_;
    } else if (operation == CommandOp::kSportAction) {
      // Stop/disarm cleanup must not overwrite the original posture result.
      // This flag denotes a valid daemon IPC reply, not remote acceptance.
      last_posture_code_ = code;
      last_posture_reply_received_ = ipc_->last_reply_received();
    }
    if (!ok) {
      RCLCPP_ERROR(get_logger(), "Native SDK request failed: %s", error.c_str());
    }
    return ok;
  }

  bool StopAndDisarm() {
    posture_active_ = false;
    posture_idempotent_ = false;
    if (!ipc_) return false;
    if (!daemon_armed_ && !stop_unconfirmed_) {
      return true;
    }
    const bool stopped = daemon_armed_ ? Exchange(CommandOp::kStop) : true;
    const bool disarmed = Exchange(CommandOp::kDisarm);
    stop_unconfirmed_ = !(stopped && disarmed);
    daemon_armed_ = false;
    output_stopped_ = true;
    ipc_->Close();
    return stopped && disarmed;
  }

  void Fault(const std::string &reason) {
    // Preserve the first failure while still retrying an unconfirmed stop.
    if (guard_->state() != GuardState::kFault) guard_->ForceFault(reason);
    (void)StopAndDisarm();
  }

  bool StateFresh() const {
    if (state_basis_ == kNativeStateBasis) {
      const double receipt_limit =
          native_clock_.PrimarySportFresh(SteadyNs()) ? 0.20 : 1.0;
      return state_valid_ && Steady() - state_received_ <= receipt_limit &&
             native_clock_.Fresh(SteadyNs());
    }
    return state_valid_ && Steady() - state_received_ <= 0.20 &&
           ValidateSourceStamp(last_stamp_ns_, now().nanoseconds(), 200'000'000ULL,
                               50'000'000ULL) == SourceStampFreshness::kFresh;
  }

  bool PostureTerminalReady() const {
    return posture_active_ && StateFresh() &&
           native_clock_.sport_received() > posture_started_ns_ &&
           NativePostureModeReached(active_posture_action_, last_mode_);
  }

  void OnState(const unitree_go::msg::SportModeState &message, std::uint64_t dds_source,
               bool fallback) {
    const auto sec = message.stamp.sec;
    const auto nsec = message.stamp.nanosec;
    const std::uint64_t stamp = sec > 0 && nsec < 1'000'000'000U
        ? static_cast<std::uint64_t>(sec) * 1'000'000'000ULL + nsec : 0U;
    if (state_basis_ == kNativeStateBasis) {
      const bool accepted = fallback
          ? native_clock_.ObserveSportFallback(dds_source, SteadyNs())
          : native_clock_.ObserveSport(dds_source, SteadyNs());
      if (!accepted) {
        if (!fallback && !native_clock_.fault().empty()) {
          state_valid_ = false;
          guard_->UpdateRobotState(Steady(), message.mode, false);
          if (daemon_armed_) Fault(native_clock_.fault());
        }
        return;
      }
      // A live high-frequency sample owns health. The low-frequency copy must
      // not overwrite it or hide a rejected primary payload.
      if (fallback && native_clock_.PrimarySportFresh(SteadyNs())) return;
    } else {
    if (ValidateSourceStamp(stamp, now().nanoseconds(), 200'000'000ULL,
                            50'000'000ULL) != SourceStampFreshness::kFresh) {
      state_valid_ = false;
      if (daemon_armed_) Fault("source timestamp is invalid or stale");
      return;
    }
    if (enabled_ && stamp < stamp_tracker_.last_source_stamp_ns()) {
      state_valid_ = false;
      Fault("source timestamp regressed");
      return;
    }
    if (!stamp_tracker_.Accept(stamp, enabled_)) {
      return;  // rejected timestamps do not extend state liveness
    }
    }
    double qnorm = 0.0;
    bool valid = message.error_code == 0U && allowed_modes_.count(message.mode) != 0U;
    for (const auto value : message.imu_state.quaternion) {
      valid = valid && std::isfinite(value);
      qnorm += static_cast<double>(value) * value;
    }
    valid = valid && std::fabs(qnorm - 1.0) <= 0.1 && std::isfinite(message.yaw_speed);
    stationary_ = std::fabs(message.yaw_speed) <= 0.05;
    double jump_squared = 0.0;
    for (std::size_t i = 0; i < 3U; ++i) {
      valid = valid && std::isfinite(message.position[i]) &&
              std::isfinite(message.velocity[i]);
      stationary_ = stationary_ && std::fabs(message.velocity[i]) <= 0.02;
      const double delta = message.position[i] - previous_position_[i];
      jump_squared += delta * delta;
    }
    valid = valid && (!has_position_ || jump_squared <= 1.0);
    if (valid) {
      for (std::size_t i = 0; i < 3U; ++i) previous_position_[i] = message.position[i];
      has_position_ = true;
    }
    state_valid_ = valid;
    last_mode_ = message.mode;
    last_stamp_ns_ = stamp;
    state_received_ = Steady();
    guard_->UpdateRobotState(state_received_, message.mode, valid && StateFresh());
    if (!valid && daemon_armed_) Fault("invalid robot health or orientation");
  }

  rclcpp::Service<Trigger>::SharedPtr PoseService(const char *name, SportAction action) {
    return create_service<Trigger>(
        name, [this, action](std::shared_ptr<Trigger::Request>,
                            std::shared_ptr<Trigger::Response> response) {
          if (!NativePostureAllowed(action) || posture_active_ || !StateFresh() ||
              !stationary_ || !output_stopped_ || !daemon_armed_ ||
              guard_->state() != GuardState::kArmed) {
            response->success = false;
            response->message = "posture requires an armed, healthy, stationary, idle native controller";
            Diagnostics();
            return;
          }
          if (NativePostureAlreadyReached(action, last_mode_)) {
            // Duplicate posture: report success without a second firmware
            // call. Mark the posture active so the normal finish path can
            // acknowledge it without sending a finish RPC to the daemon.
            posture_active_ = true;
            posture_idempotent_ = true;
            active_posture_action_ = action;
            posture_started_ns_ = SteadyNs();
            // The idempotent finish is local and may follow the controller's
            // full posture window; keep a wider deadline than a real dispatch.
            posture_deadline_ = Steady() + kNativePostureWindowSec + 2.0;
            response->success = true;
            response->message = "posture_already_in_target_mode; current_mode=" +
                                std::to_string(last_mode_) +
                                "; target_mode=" +
                                std::to_string(NativePostureTargetMode(action));
            Diagnostics();
            return;
          }
          if (NativePostureTransitionClearlyInvalid(action, last_mode_)) {
            // Firmware-state rejection, not a transport fault. Keep the
            // operator's session authorized and report the current mode.
            response->success = false;
            response->message =
                "posture_state_rejected: transition_not_applicable"
                "; current_mode=" + std::to_string(last_mode_) +
                "; target_mode=" +
                std::to_string(NativePostureTargetMode(action)) +
                "; recommendation=" +
                NativePostureRecommendation(action, last_mode_);
            Diagnostics();
            return;
          }
          response->success = Exchange(CommandOp::kSportAction, {}, action);
          if (!response->success) {
            response->message =
                "SDK posture command failed: action_id=" +
                std::to_string(last_posture_action_id_) +
                ", code=" + std::to_string(last_posture_code_) +
                ", ipc_reply_received=" +
                (last_posture_reply_received_ ? "true" : "false") +
                ", current_mode=" + std::to_string(last_mode_) +
                ", target_mode=" +
                std::to_string(NativePostureTargetMode(action));
            Fault(response->message);
            Diagnostics();
            return;
          }
          posture_active_ = true;
          active_posture_action_ = action;
          posture_started_ns_ = SteadyNs();
          posture_deadline_ = Steady() + kNativePostureWindowSec;
          ++sdk_posture_dispatch_count_;
          response->message = "SDK dispatch acknowledged; physical completion is unverified";
          Diagnostics();
        });
  }

  void Tick() {
    if (!enabled_) return;
    // Synchronous SDK/evidence waits can delay this timer. Use elapsed steady
    // time for slew; SafetyGuard still clamps each step to at most 0.10 s.
    const auto decision = guard_->Tick(Steady(), 0.0);
    if (decision.state == GuardState::kFault ||
        (daemon_armed_ && !StateFresh())) {
      Fault("state/command watchdog or safety guard fault");
      return;
    }
    if (decision.state != GuardState::kArmed) return;
    if (!daemon_armed_) {
      if (!Exchange(CommandOp::kArm)) {
        Fault("SDK arm acknowledgement failed");
        return;
      }
      daemon_armed_ = true;
      return;  // revalidate state before the first following command
    }
    if (posture_active_) {
      if (Steady() >= posture_deadline_) {
        Fault("posture window expired before explicit stop");
      } else if (!Exchange(CommandOp::kPing)) {
        Fault("posture heartbeat failed");
      }
      return;
    }
    if (decision.action == GuardAction::kMove) {
      if (!NativeVelocityAllowed(decision.velocity.vx, decision.velocity.vy,
                                 decision.velocity.wz, max_vx_, max_vy_,
                                 max_wz_) ||
          !Exchange(CommandOp::kMove, decision.velocity)) {
        Fault("native velocity dispatch failed");
        return;
      }
      output_stopped_ = false;
    } else if (!output_stopped_) {
      if (!Exchange(CommandOp::kStop)) Fault("zero-velocity stop was not acknowledged");
      output_stopped_ = true;
    } else if (!Exchange(CommandOp::kPing)) {
      Fault("SDK heartbeat failed");
    }
  }

  void Diagnostics() {
    diagnostic_msgs::msg::DiagnosticArray message;
    message.header.stamp = now();
    diagnostic_msgs::msg::DiagnosticStatus status;
    status.name = "go2_chassis_adapter";
    status.hardware_id = "unitree_go2";
    status.level = guard_->state() == GuardState::kFault ? 2 : (daemon_armed_ ? 0 : 1);
    status.message = guard_->reason();
    const auto add = [&status](const std::string &key, const std::string &value) {
      diagnostic_msgs::msg::KeyValue item;
      item.key = key; item.value = value; status.values.push_back(item);
    };
    add("motion_profile", kNativeMotionProfile);
    add("allow_motion", enabled_ ? "true" : "false");
    add("guard_state", GuardStateName(guard_->state()));
    add("daemon_armed", daemon_armed_ ? "true" : "false");
    add("state_valid", StateFresh() ? "true" : "false");
    add("source_stamp_status", StateFresh() ? "fresh" : "invalid_or_stale");
    add("state_time_basis", state_basis_);
    add("sport_dds_source_ns", std::to_string(native_clock_.sport_source()));
    add("payload_source_stamp_ns", std::to_string(last_stamp_ns_));
    add("lowstate_tick", std::to_string(native_clock_.tick()));
    add("native_clock_fault", native_clock_.fault());
    add("absolute_measurement_age_verified", "false");
    add("movement_mode_ready", NativeMovementMode(last_mode_) ? "true" : "false");
    add("mode", std::to_string(last_mode_));
    add("max_vx", std::to_string(max_vx_));
    add("max_vy", std::to_string(max_vy_));
    add("max_wz", std::to_string(max_wz_));
    add("posture_active", posture_active_ ? "true" : "false");
    add("posture_idempotent", posture_idempotent_ ? "true" : "false");
    add("posture_action_id", std::to_string(static_cast<std::uint8_t>(active_posture_action_)));
    add("posture_target_mode", std::to_string(NativePostureTargetMode(active_posture_action_)));
    add("posture_terminal_ready", PostureTerminalReady() ? "true" : "false");
    add("sdk_posture_dispatch_count", std::to_string(sdk_posture_dispatch_count_));
    add("sdk_posture_finish_count", std::to_string(sdk_posture_finish_count_));
    add("posture_attempt_count", std::to_string(posture_attempt_count_));
    add("last_posture_action_id", std::to_string(last_posture_action_id_));
    add("last_posture_code", std::to_string(last_posture_code_));
    add("last_posture_reply_received", last_posture_reply_received_ ? "true" : "false");
    add("motion_evidence_instance", motion_evidence_instance_);
    add("sdk_move_acknowledgement_basis", "matching_remote_response_status_zero");
    add("sdk_move_reply_basis", "daemon_ipc");
    add("sdk_move_requires_remote_ack", "true");
    add("physical_motion_verified", "false");
    add("move_attempt_count", std::to_string(motion_evidence_.sdk_move_attempt_count));
    add("move_dispatch_count", std::to_string(motion_evidence_.sdk_move_dispatch_count));
    add("move_failure_count", std::to_string(motion_evidence_.sdk_move_failure_count));
    add("last_move_code", std::to_string(motion_evidence_.last_sdk_move_reply_code));
    add("last_move_reply_received", motion_evidence_.last_sdk_move_reply_received ? "true" : "false");
    add("last_move_remote_response_verified", motion_evidence_.last_sdk_move_remote_response_verified ? "true" : "false");
    add("last_move_vx", std::to_string(motion_evidence_.last_sdk_move.vx));
    add("last_move_vy", std::to_string(motion_evidence_.last_sdk_move.vy));
    add("last_move_wz", std::to_string(motion_evidence_.last_sdk_move.wz));
    add("last_move_request_monotonic_ns", std::to_string(motion_evidence_.last_sdk_move_request_monotonic_ns));
    add("last_move_dispatch_monotonic_ns", std::to_string(motion_evidence_.last_sdk_move_dispatch_monotonic_ns));
    add("stop_ack_count", std::to_string(stop_ack_count_));
    add("velocity_received_count", std::to_string(motion_evidence_.velocity_received_count));
    add("velocity_nonzero_received_count", std::to_string(motion_evidence_.velocity_nonzero_received_count));
    add("velocity_accepted_count", std::to_string(motion_evidence_.velocity_accepted_count));
    add("velocity_nonzero_accepted_count", std::to_string(motion_evidence_.velocity_nonzero_accepted_count));
    add("velocity_stop_fence_rejected_count", std::to_string(motion_evidence_.velocity_stop_fence_rejected_count));
    add("last_velocity_received_vx", std::to_string(motion_evidence_.last_velocity_received.vx));
    add("last_velocity_received_vy", std::to_string(motion_evidence_.last_velocity_received.vy));
    add("last_velocity_received_wz", std::to_string(motion_evidence_.last_velocity_received.wz));
    add("last_velocity_received_monotonic_ns", std::to_string(motion_evidence_.last_velocity_received_monotonic_ns));
    add("sdk_move_attempt_count", std::to_string(motion_evidence_.sdk_move_attempt_count));
    add("sdk_move_dispatch_count", std::to_string(motion_evidence_.sdk_move_dispatch_count));
    add("sdk_move_failure_count", std::to_string(motion_evidence_.sdk_move_failure_count));
    add("last_sdk_move_vx", std::to_string(motion_evidence_.last_sdk_move.vx));
    add("last_sdk_move_vy", std::to_string(motion_evidence_.last_sdk_move.vy));
    add("last_sdk_move_wz", std::to_string(motion_evidence_.last_sdk_move.wz));
    add("last_sdk_move_request_monotonic_ns", std::to_string(motion_evidence_.last_sdk_move_request_monotonic_ns));
    add("last_sdk_move_dispatch_monotonic_ns", std::to_string(motion_evidence_.last_sdk_move_dispatch_monotonic_ns));
    add("last_sdk_move_reply_code", std::to_string(motion_evidence_.last_sdk_move_reply_code));
    add("last_sdk_move_reply_received", motion_evidence_.last_sdk_move_reply_received ? "true" : "false");
    add("last_sdk_move_remote_response_verified", motion_evidence_.last_sdk_move_remote_response_verified ? "true" : "false");
    message.status.push_back(status);
    diagnostics_->publish(message);
  }

  bool enabled_{false}, daemon_armed_{false}, output_stopped_{true};
  double max_vx_{kNativeMaxVx}, max_vy_{kNativeMaxVy}, max_wz_{kNativeMaxWz};
  bool state_valid_{false}, stationary_{false}, posture_active_{false};
  bool posture_idempotent_{false};
  bool has_position_{false};
  bool stop_unconfirmed_{false};
  NativeMotionEvidence motion_evidence_;
  const std::string motion_evidence_instance_{std::to_string(SteadyNs())};
  SportAction active_posture_action_{static_cast<SportAction>(0)};
  std::uint64_t posture_started_ns_{0U};
  std::uint64_t sdk_posture_dispatch_count_{0U}, sdk_posture_finish_count_{0U};
  std::uint64_t posture_attempt_count_{0U};
  std::uint8_t last_posture_action_id_{0U};
  std::int32_t last_posture_code_{
      static_cast<std::int32_t>(ReplyCode::kInternalError)};
  bool last_posture_reply_received_{false};
  std::uint64_t stop_ack_count_{0U};
  std::string state_basis_;
  std::uint8_t last_mode_{255U};
  NativeStateClock native_clock_;
  std::array<double,3> previous_position_{{0,0,0}};
  double state_received_{0.0}, posture_deadline_{0.0};
  SourceStampTracker stamp_tracker_;
  std::uint64_t last_stamp_ns_{0U};
  std::uint64_t completed_stop_ns_{0U};
  std::set<std::uint8_t> allowed_modes_;
  std::unique_ptr<SafetyGuard> guard_;
  std::unique_ptr<SeqpacketClient> ipc_;
  rclcpp::Publisher<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diagnostics_;
  rclcpp::Subscription<unitree_go::msg::SportModeState>::SharedPtr state_sub_;
  rclcpp::Subscription<unitree_go::msg::SportModeState>::SharedPtr state_fallback_sub_;
  rclcpp::Subscription<unitree_go::msg::LowState>::SharedPtr low_sub_;
  rclcpp::Subscription<geometry_msgs::msg::Twist>::SharedPtr command_sub_;
  rclcpp::Service<std_srvs::srv::SetBool>::SharedPtr arm_;
  rclcpp::Service<Trigger>::SharedPtr stand_up_, stand_down_, balance_stand_, finish_,
      finish_posture_;
  rclcpp::TimerBase::SharedPtr control_timer_, diagnostic_timer_;
};
}  // namespace
}  // namespace go2_chassis

int main(int argc, char **argv) {
  rclcpp::init(argc, argv);
  try {
    rclcpp::spin(std::make_shared<go2_chassis::NativeAdapter>());
  } catch (const std::exception &error) {
    RCLCPP_ERROR(rclcpp::get_logger("go2_native_chassis_adapter"), "%s", error.what());
    rclcpp::shutdown();
    return 1;
  }
  rclcpp::shutdown();
  return 0;
}
