#pragma once

#include <chrono>
#include <cmath>
#include <cstdlib>
#include <functional>
#include <memory>
#include <mutex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/idl/go2/LowState_.hpp>
#include <unitree/idl/go2/SportModeState_.hpp>
#include "go2_chassis/native_profile.hpp"
#include "go2_chassis/native_state_clock.hpp"

namespace go2_chassis {

struct NativeSdkSnapshot {
  bool health_valid{false}, fresh_at_request{false}, fresh_at_inspection{false};
  std::uint64_t requested_ns{0}, inspected_ns{0};
  std::uint64_t sport_received_ns{0}, low_received_ns{0}, tick_received_ns{0};
  std::string clock_fault;
};

// Preserve SampleInfo's ORIGINAL DDS writer timestamp. The SDK's standard
// void* callback drops this metadata; its queue cannot prove source progress.
template <typename Message>
class NativeSampleReader final : public ::dds::sub::NoOpDataReaderListener<Message> {
 public:
  using Callback = std::function<void(const Message &, std::uint64_t)>;
  NativeSampleReader(const char *topic, Callback callback) : callback_(std::move(callback)) {
    channel_ = unitree::robot::ChannelFactory::Instance()->CreateRecvChannel<Message>(
        topic, [](const void *) {}, 0);
    auto reader = channel_->GetReader()->GetNative();
    reader.listener(this, ::dds::core::status::StatusMask::data_available());
  }
  ~NativeSampleReader() override {
    if (channel_) {
      auto reader = channel_->GetReader()->GetNative();
      reader.listener(nullptr, ::dds::core::status::StatusMask::none());
      channel_.reset();
    }
  }
 private:
  void on_data_available(::dds::sub::DataReader<Message> &reader) override {
    auto samples = reader.take();
    for (const auto &sample : samples) {
      if (!sample.info().valid()) continue;
      const auto stamp = sample.info().timestamp();
      const auto source = stamp.sec() > 0 && stamp.nanosec() < 1'000'000'000U
          ? static_cast<std::uint64_t>(stamp.sec())*1'000'000'000ULL + stamp.nanosec() : 0U;
      callback_(sample.data(), source);
    }
  }
  Callback callback_;
  unitree::robot::ChannelPtr<Message> channel_;
};

class NativeSdkState {
 public:
  NativeSdkState() {
    const char *basis = std::getenv("GO2_STATE_TIME_BASIS");
    const char *modes = std::getenv("GO2_ALLOWED_MODES");
    if (basis == nullptr || std::string(basis) != kNativeStateBasis || modes == nullptr) {
      throw std::runtime_error("native DDS state basis and audited modes are required");
    }
    std::stringstream stream(modes);
    std::string value;
    while (std::getline(stream, value, ',')) {
      std::size_t used = 0;
      const int mode = std::stoi(value, &used);
      if (used != value.size() || mode < 0 || mode > 254) {
        throw std::runtime_error("invalid native audited mode");
      }
      modes_.insert(static_cast<std::uint8_t>(mode));
    }
    if (modes_.empty()) throw std::runtime_error("native audited mode set is empty");
    sport_ = std::make_unique<NativeSampleReader<Sport>>(
        "rt/sportmodestate", [this](const Sport &state, std::uint64_t stamp) {
          OnSport(state, stamp, false);
        });
    sport_fallback_ = std::make_unique<NativeSampleReader<Sport>>(
        "rt/lf/sportmodestate", [this](const Sport &state, std::uint64_t stamp) {
          OnSport(state, stamp, true);
        });
    low_ = std::make_unique<NativeSampleReader<Low>>(
        "rt/lowstate", [this](const Low &state, std::uint64_t stamp) {
          std::lock_guard<std::mutex> lock(mutex_);
          clock_.ObserveLow(state.tick(), stamp, Now());
        });
  }
  bool Fresh(std::uint64_t now) const {
    std::lock_guard<std::mutex> lock(mutex_);
    return state_valid_ && clock_.FreshAtInspection(now, Now());
  }
  bool MovementReady() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return state_valid_ && clock_.Fresh(Now()) && NativeMovementMode(mode_);
  }
  bool PostureComplete(SportAction action, std::uint64_t requested_ns,
                       std::uint64_t action_started_ns) const {
    std::lock_guard<std::mutex> lock(mutex_);
    return state_valid_ &&
           clock_.FreshAtInspection(requested_ns, Now()) &&
           clock_.sport_received() > action_started_ns &&
           NativePostureModeReached(action, mode_);
  }
  NativeSdkSnapshot Inspect(std::uint64_t requested_ns) const {
    std::lock_guard<std::mutex> lock(mutex_);
    NativeSdkSnapshot result;
    result.requested_ns = requested_ns;
    result.inspected_ns = Now();
    result.health_valid = state_valid_;
    result.fresh_at_request = state_valid_ && clock_.Fresh(requested_ns);
    result.fresh_at_inspection = state_valid_ &&
        clock_.FreshAtInspection(requested_ns, result.inspected_ns);
    result.sport_received_ns = clock_.sport_received();
    result.low_received_ns = clock_.low_received();
    result.tick_received_ns = clock_.tick_received();
    result.clock_fault = clock_.fault();
    return result;
  }
 private:
  using Sport = unitree_go::msg::dds_::SportModeState_;
  using Low = unitree_go::msg::dds_::LowState_;
  static std::uint64_t Now() {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
  }
  void OnSport(const Sport &state, std::uint64_t stamp, bool fallback) {
    std::lock_guard<std::mutex> lock(mutex_);
    const bool accepted = fallback ? clock_.ObserveSportFallback(stamp, Now())
                                   : clock_.ObserveSport(stamp, Now());
    if (!accepted) return;
    if (fallback && clock_.PrimarySportFresh(Now())) return;
    bool valid = state.error_code() == 0U && modes_.count(state.mode()) != 0U;
    double norm = 0;
    for (const auto value : state.imu_state().quaternion()) {
      valid = valid && std::isfinite(value); norm += value*value;
    }
    valid = valid && std::fabs(norm-1.0) <= 0.1 && std::isfinite(state.yaw_speed());
    for (const auto value : state.position()) valid = valid && std::isfinite(value);
    for (const auto value : state.velocity()) valid = valid && std::isfinite(value);
    state_valid_ = valid;
    mode_ = state.mode();
  }
  mutable std::mutex mutex_;
  NativeStateClock clock_;
  std::set<std::uint8_t> modes_;
  bool state_valid_{false};
  std::uint8_t mode_{255U};
  // Destroy readers before any state that their callbacks access.
  std::unique_ptr<NativeSampleReader<Sport>> sport_;
  std::unique_ptr<NativeSampleReader<Sport>> sport_fallback_;
  std::unique_ptr<NativeSampleReader<Low>> low_;
};
}  // namespace go2_chassis
