// Subscription-only audit. In particular, never constructs a SportClient.
#include <chrono>
#include <cmath>
#include <iostream>
#include <memory>
#include <string>

#include "rclcpp/rclcpp.hpp"
#include "unitree_go/msg/sport_mode_state.hpp"
#include "unitree_go/msg/low_state.hpp"
#include "go2_chassis/native_state_clock.hpp"

int main(int argc, char **argv) {
  rclcpp::init(argc, argv);
  auto node = std::make_shared<rclcpp::Node>("go2_native_state_probe");
  go2_chassis::NativeStateClock clock;
  const auto steady = []() {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
  };
  std::size_t primary = 0, low_frequency = 0, low_samples = 0;
  std::uint32_t first_tick = 0;
  const auto observe = [&node, &clock, &steady](const char *topic, std::size_t &count) {
    return node->create_subscription<unitree_go::msg::SportModeState>(
        topic, rclcpp::SensorDataQoS().keep_last(1),
        [&node, &clock, &steady, topic, &count](unitree_go::msg::SportModeState::SharedPtr state,
                              const rclcpp::MessageInfo &info) {
          const auto &metadata = info.get_rmw_message_info();
          if (std::string(topic) == "/lf/sportmodestate") {
            clock.ObserveSport(metadata.source_timestamp, steady());
          }
          if (++count > 5) return;
          double qnorm = 0;
          for (const auto value : state->imu_state.quaternion) qnorm += value * value;
          std::cout << "{\"topic\":\"" << topic << "\",\"wall_ns\":"
                    << node->now().nanoseconds() << ",\"dds_source_ns\":"
                    << metadata.source_timestamp << ",\"dds_received_ns\":"
                    << metadata.received_timestamp << ",\"payload_sec\":"
                    << state->stamp.sec << ",\"payload_nsec\":"
                    << state->stamp.nanosec << ",\"mode\":"
                    << static_cast<unsigned>(state->mode)
                    << ",\"error_code\":" << state->error_code
                    << ",\"quaternion_norm_squared\":" << qnorm
                    << ",\"velocity\":[" << state->velocity[0] << ","
                    << state->velocity[1] << "," << state->velocity[2]
                    << "],\"yaw_speed\":" << state->yaw_speed << "}\n";
        });
  };
  auto primary_sub = observe("/sportmodestate", primary);
  auto low_sub = observe("/lf/sportmodestate", low_frequency);
  auto lowstate_sub = node->create_subscription<unitree_go::msg::LowState>(
      "/lowstate", rclcpp::SensorDataQoS().keep_last(1),
      [&](unitree_go::msg::LowState::SharedPtr state, const rclcpp::MessageInfo &info) {
        if (low_samples++ == 0) first_tick = state->tick;
        clock.ObserveLow(state->tick, info.get_rmw_message_info().source_timestamp, steady());
      });
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(4);
  rclcpp::WallRate rate(200);
  while (rclcpp::ok() && std::chrono::steady_clock::now() < deadline) {
    rclcpp::spin_some(node);
    rate.sleep();
  }
  std::cout << "{\"primary_samples\":" << primary
            << ",\"low_frequency_samples\":" << low_frequency
            << ",\"lowstate_samples\":" << low_samples
            << ",\"first_lowstate_tick\":" << first_tick
            << ",\"last_lowstate_tick\":" << clock.tick()
            << ",\"native_transport_fresh\":" << (clock.Fresh(steady()) ? "true" : "false")
            << ",\"native_clock_fault\":\"" << clock.fault()
            << "\",\"absolute_measurement_age_verified\":false}\n";
  rclcpp::shutdown();
}
