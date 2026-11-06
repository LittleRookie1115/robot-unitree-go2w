// Subscription-only SDK freshness audit. Never constructs a SportClient.
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <thread>
#include <unitree/common/json/jsonize.hpp>
#include "go2_chassis/native_sdk_state.hpp"

static std::uint64_t Now() {
  return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count());
}

int main(int argc, char **argv) {
  if (argc != 3) return 2;
  try {
    const int duration = std::stoi(argv[2]);
    if (duration < 3 || duration > 180) return 2;
    unitree::robot::ChannelFactory::Instance()->Init(0, argv[1]);
    go2_chassis::NativeSdkState state;
    const auto start = Now();
    const auto end = start + static_cast<std::uint64_t>(duration)*1'000'000'000ULL;
    std::uint64_t samples=0, caller_only_stale=0, actual_stale=0, guard_stale=0;
    std::string first_clock_fault;
    while (Now() < end) {
      const auto requested = Now();
      const auto observation = state.Inspect(requested);
      const bool guard_fresh = state.Fresh(requested);
      if (requested-start > 1'500'000'000ULL) {
        ++samples;
        if (!observation.fresh_at_request && observation.fresh_at_inspection) {
          ++caller_only_stale;
          if (caller_only_stale <= 5) {
            std::cout << "{\"event\":\"old_snapshot_rejected_new_receipt\","
                      << "\"requested_ns\":" << requested
                      << ",\"inspected_ns\":" << observation.inspected_ns
                      << ",\"sport_received_ns\":" << observation.sport_received_ns
                      << ",\"low_received_ns\":" << observation.low_received_ns
                      << ",\"tick_received_ns\":" << observation.tick_received_ns
                      << "}" << std::endl;
          }
        }
        if (!observation.fresh_at_inspection) ++actual_stale;
        if (!guard_fresh) ++guard_stale;
        if (first_clock_fault.empty() && !observation.clock_fault.empty()) {
          first_clock_fault = observation.clock_fault;
        }
      }
      std::this_thread::sleep_for(std::chrono::microseconds(500));
    }
    unitree::common::JsonMap report;
    report["read_only"] = true;
    report["duration_sec"] = duration;
    report["samples"] = samples;
    report["old_timestamp_false_rejections"] = caller_only_stale;
    report["actual_unhealthy_samples"] = actual_stale;
    report["guard_unhealthy_samples"] = guard_stale;
    report["first_clock_fault"] = first_clock_fault;
    report["absolute_measurement_age_verified"] = false;
    std::cout << unitree::common::ToJsonString(report) << std::endl;
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << std::endl;
    return 2;
  }
}
