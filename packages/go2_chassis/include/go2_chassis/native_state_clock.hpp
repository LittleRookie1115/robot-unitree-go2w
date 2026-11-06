#pragma once

#include <cstdint>
#include <limits>
#include <string>

namespace go2_chassis {

// This is transport/source-progress evidence for bounded manual commands.
// It is NOT a replacement measurement timestamp, UTC synchronization, or
// proof of absolute hardware measurement age. No message is restamped.
// Each DDS source clock is compared with monotonic elapsed time in its own
// epoch. The initial epoch is immutable for this process lifetime.
class NativeSourceProgress {
 public:
  bool Observe(std::uint64_t source_ns, std::uint64_t received_ns) {
    if (!fault_.empty()) return false;
    if (source_ns == 0U || source_ns > static_cast<std::uint64_t>(
            std::numeric_limits<std::int64_t>::max()) || received_ns == 0U) {
      return Fail("missing_or_invalid_source_clock");
    }
    if (count_ == 0U) {
      first_source_ = last_source_ = source_ns;
      first_received_ = last_received_ = received_ns;
      count_ = 1U;
      return true;
    }
    if (received_ns < last_received_) return Fail("monotonic_clock_regressed");
    if (source_ns < last_source_) return Fail("source_clock_regressed");
    if (source_ns == last_source_) return false;  // replay never renews liveness
    const auto source_elapsed = source_ns - first_source_;
    const auto local_elapsed = received_ns - first_received_;
    if (local_elapsed > source_elapsed &&
        local_elapsed - source_elapsed > 200'000'000ULL) {
      return Fail("source_progress_delayed");
    }
    if (source_elapsed > local_elapsed &&
        source_elapsed - local_elapsed > 50'000'000ULL) {
      return Fail("source_clock_jump_or_catchup");
    }
    last_source_ = source_ns;
    last_received_ = received_ns;
    ++count_;
    return true;
  }

  bool Fresh(std::uint64_t now_ns, std::uint64_t max_age_ns = 200'000'000ULL) const {
    return fault_.empty() && count_ >= 8U && now_ns >= last_received_ &&
           now_ns - last_received_ <= max_age_ns &&
           last_received_ - first_received_ >= 1'000'000'000ULL;
  }

  const std::string &fault() const { return fault_; }
  std::uint64_t source() const { return last_source_; }
  std::uint64_t count() const { return count_; }
  std::uint64_t received() const { return last_received_; }

 private:
  bool Fail(const char *reason) { fault_ = reason; return false; }
  std::uint64_t first_source_{0U}, last_source_{0U};
  std::uint64_t first_received_{0U}, last_received_{0U}, count_{0U};
  std::string fault_;
};

class NativeStateClock {
 public:
  bool ObserveSport(std::uint64_t dds_source_ns, std::uint64_t received_ns) {
    return sport_.Observe(dds_source_ns, received_ns);
  }

  // Low-frequency SportModeState is an independent backup. Its timestamp must
  // not regress or refresh the high-frequency clock.
  bool ObserveSportFallback(std::uint64_t dds_source_ns, std::uint64_t received_ns) {
    return sport_fallback_.Observe(dds_source_ns, received_ns);
  }

  bool PrimarySportFresh(std::uint64_t now_ns) const {
    return sport_.fault().empty() && sport_.Fresh(now_ns);
  }

  bool ObserveLow(std::uint32_t tick, std::uint64_t dds_source_ns,
                  std::uint64_t received_ns) {
    if (!counter_fault_.empty()) return false;
    if (!has_tick_) {
      last_tick_ = tick;
      extended_tick_ = static_cast<std::uint64_t>(tick) + 1U;
      has_tick_ = true;
    } else {
      const std::uint32_t delta = tick - last_tick_;
      if (delta == 0U) return false;
      if (delta >= 0x80000000U) {
        counter_fault_ = "lowstate_tick_regressed";
        return false;
      }
      extended_tick_ += delta;  // uint32 wrap is accepted only as forward progress
      last_tick_ = tick;
    }
    // This device's tick advances in milliseconds; the read-only probe
    // qualifies that rate independently against the monotonic clock.
    const bool counter = tick_clock_.Observe(extended_tick_ * 1'000'000ULL, received_ns);
    const bool transport = low_.Observe(dds_source_ns, received_ns);
    return counter && transport;
  }

  bool Fresh(std::uint64_t now_ns) const {
    return fault().empty() && SportFresh(now_ns) && low_.Fresh(now_ns) &&
           tick_clock_.Fresh(now_ns);
  }

  // High-frequency sport state stays on the 200 ms gate. The low-frequency
  // topic carries the same evidence only after that primary receipt is gone,
  // and only inside its own one-second period.
  bool SportFresh(std::uint64_t now_ns) const {
    if (!sport_.fault().empty()) return false;
    if (sport_.Fresh(now_ns)) return true;
    return sport_fallback_.fault().empty() &&
           sport_fallback_.Fresh(now_ns, 1'000'000'000ULL);
  }

  // Both the inspection time and the clock state must be sampled under the
  // subscriber's mutex. A caller's earlier timestamp can precede a callback
  // that acquired that mutex first; such a new receipt is not stale.
  bool FreshAtInspection(std::uint64_t requested_ns,
                         std::uint64_t inspected_ns) const {
    return requested_ns <= inspected_ns && Fresh(inspected_ns);
  }

  std::string fault() const {
    if (!sport_.fault().empty()) return "sport:" + sport_.fault();
    if (!low_.fault().empty()) return "lowstate_dds:" + low_.fault();
    if (!tick_clock_.fault().empty()) return "lowstate_tick:" + tick_clock_.fault();
    return counter_fault_;
  }
  std::uint64_t sport_source() const {
    return sport_.source() > sport_fallback_.source() ? sport_.source()
                                                     : sport_fallback_.source();
  }
  std::uint64_t sport_samples() const { return sport_.count(); }
  std::uint32_t tick() const { return last_tick_; }
  std::uint64_t sport_received() const {
    return sport_.received() > sport_fallback_.received() ? sport_.received()
                                                         : sport_fallback_.received();
  }
  std::uint64_t low_received() const { return low_.received(); }
  std::uint64_t tick_received() const { return tick_clock_.received(); }

 private:
  NativeSourceProgress sport_, sport_fallback_, low_, tick_clock_;
  bool has_tick_{false};
  std::uint32_t last_tick_{0U};
  std::uint64_t extended_tick_{0U};
  std::string counter_fault_;
};

}  // namespace go2_chassis
