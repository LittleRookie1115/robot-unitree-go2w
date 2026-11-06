// Read-only DDS probe for Unitree high/low frequency SportModeState.
// It never constructs a SportClient and never publishes a command.
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/idl/go2/SportModeState_.hpp>

namespace {

using Sport = unitree_go::msg::dds_::SportModeState_;

struct Snapshot {
  bool seen{false};
  unsigned mode{0};
  unsigned error{0};
  double velocity[3]{0, 0, 0};
  double position[3]{0, 0, 0};
  double yaw_speed{0};
  double qnorm{0};
  long sec{0};
  unsigned long nanosec{0};
};

std::mutex g_mutex;
Snapshot g_high;
Snapshot g_low;
std::atomic<unsigned long> g_high_count{0};
std::atomic<unsigned long> g_low_count{0};

void Copy(const Sport &state, Snapshot &out) {
  out.seen = true;
  out.mode = state.mode();
  out.error = state.error_code();
  for (int i = 0; i < 3; ++i) {
    out.velocity[i] = state.velocity()[i];
    out.position[i] = state.position()[i];
  }
  out.yaw_speed = state.yaw_speed();
  out.qnorm = 0;
  for (const auto value : state.imu_state().quaternion()) {
    out.qnorm += static_cast<double>(value) * value;
  }
  out.sec = state.stamp().sec();
  out.nanosec = state.stamp().nanosec();
}

void OnHigh(const void *message) {
  if (message == nullptr) return;
  std::lock_guard<std::mutex> lock(g_mutex);
  Copy(*static_cast<const Sport *>(message), g_high);
  ++g_high_count;
}

void OnLow(const void *message) {
  if (message == nullptr) return;
  std::lock_guard<std::mutex> lock(g_mutex);
  Copy(*static_cast<const Sport *>(message), g_low);
  ++g_low_count;
}

void Print(const Snapshot &value, const char *name) {
  std::cout << "\"" << name << "\":{"
            << "\"seen\":" << (value.seen ? "true" : "false")
            << ",\"mode\":" << value.mode
            << ",\"error\":" << value.error
            << ",\"velocity\":[" << value.velocity[0] << "," << value.velocity[1]
            << "," << value.velocity[2] << "]"
            << ",\"position\":[" << value.position[0] << "," << value.position[1]
            << "," << value.position[2] << "]"
            << ",\"yaw_speed\":" << value.yaw_speed
            << ",\"qnorm\":" << value.qnorm
            << ",\"stamp\":[" << value.sec << "," << value.nanosec << "]}";
}

}  // namespace

int main(int argc, char **argv) {
  if (argc != 3) return 2;
  int duration = 0;
  try {
    duration = std::stoi(argv[2]);
  } catch (...) {
    return 2;
  }
  if (duration < 3 || duration > 600) return 2;

  unitree::robot::ChannelFactory::Instance()->Init(0, argv[1]);
  auto high = std::make_unique<unitree::robot::ChannelSubscriber<Sport>>(
      "rt/sportmodestate");
  high->InitChannel([](const void *message) { OnHigh(message); }, 16);
  auto low = std::make_unique<unitree::robot::ChannelSubscriber<Sport>>(
      "rt/lf/sportmodestate");
  low->InitChannel([](const void *message) { OnLow(message); }, 16);

  const auto start = std::chrono::steady_clock::now();
  const auto deadline = start + std::chrono::seconds(duration);
  while (std::chrono::steady_clock::now() < deadline) {
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - start).count();
    std::lock_guard<std::mutex> lock(g_mutex);
    std::cout << "{\"elapsed_ms\":" << elapsed
              << ",\"high_count\":" << g_high_count.load()
              << ",\"low_count\":" << g_low_count.load() << ",";
    Print(g_high, "high");
    std::cout << ",";
    Print(g_low, "low");
    std::cout << "}" << std::endl;
  }
  std::cout << "{\"done\":true,\"high_count\":" << g_high_count.load()
            << ",\"low_count\":" << g_low_count.load() << "}" << std::endl;
  high.reset();
  low.reset();
  return 0;
}
