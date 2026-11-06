#include <cassert>
#include <cstdint>
#include <iostream>
#include <utility>
#include "go2_chassis/native_state_clock.hpp"
using namespace go2_chassis;

constexpr std::uint64_t epoch = 1'792'000'000'000'000'000ULL;
constexpr std::uint64_t local = 10'000'000'000ULL;

void Fill(NativeStateClock &clock) {
  for (std::uint32_t i = 0; i <= 600U; ++i) {
    const auto elapsed = static_cast<std::uint64_t>(i) * 2'000'000ULL;
    assert(clock.ObserveLow(1000U + 2U*i, epoch - 468'000'000'000ULL + elapsed,
                            local + elapsed));
    if (i % 50U == 0U) assert(clock.ObserveSport(epoch + elapsed, local + elapsed));
  }
}

int main() {
  NativeStateClock clock;
  assert(!clock.Fresh(local));
  Fill(clock);
  assert(clock.Fresh(local + 1'200'000'000ULL));
  // Watchdog takes its time before locking, then a DDS callback wins the lock.
  // Inspect the resulting newer state at the time captured under that lock.
  const auto requested = local + 1'200'000'000ULL;
  assert(clock.ObserveLow(2202U, epoch - 468'000'000'000ULL + 1'202'000'000ULL,
                          requested + 2'000'000ULL));
  assert(!clock.Fresh(requested));  // old behavior falsely rejects this receipt
  assert(clock.FreshAtInspection(requested, requested + 2'001'000ULL));
  assert(!clock.FreshAtInspection(requested, requested + 401'000'000ULL));
  assert(!clock.FreshAtInspection(requested + 3'000'000ULL, requested + 2'001'000ULL));
  assert(!clock.Fresh(local + 1'401'000'000ULL));  // both clocks must keep progressing
  assert(!clock.ObserveSport(epoch + 1'200'000'000ULL, local + 1'300'000'000ULL));
  assert(!clock.Fresh(local + 1'401'000'000ULL));  // duplicate cannot refresh
  assert(!clock.ObserveSport(epoch + 1'100'000'000ULL, local + 1'400'000'000ULL));
  assert(!clock.fault().empty());
  assert(!clock.ObserveSport(epoch + 1'500'000'000ULL, local + 1'500'000'000ULL));

  NativeStateClock frozen_tick;
  Fill(frozen_tick);
  for (int i = 1; i <= 4; ++i) {
    frozen_tick.ObserveSport(epoch + 1'200'000'000ULL + i*100'000'000ULL,
                             local + 1'200'000'000ULL + i*100'000'000ULL);
    assert(!frozen_tick.ObserveLow(2200U, epoch + i, local + 1'200'000'000ULL + i));
  }
  assert(!frozen_tick.Fresh(local + 1'600'000'000ULL));

  NativeStateClock reboot;
  Fill(reboot);
  assert(!reboot.ObserveLow(3U, epoch + 2'000'000'000ULL, local + 2'000'000'000ULL));
  assert(reboot.fault() == "lowstate_tick_regressed");

  NativeStateClock wrap;
  for (std::uint32_t i=0; i<=600U; ++i) {
    const auto elapsed = static_cast<std::uint64_t>(i)*2'000'000ULL;
    assert(wrap.ObserveLow(0xffffff00U + 2U*i, epoch+elapsed, local+elapsed));
    if (i%50U==0U) wrap.ObserveSport(epoch+elapsed, local+elapsed);
  }
  assert(wrap.Fresh(local+1'200'000'000ULL));

  for (const auto &pair : {
      std::pair<std::uint64_t,std::uint64_t>{1'000'000ULL, 500'000'000ULL},
      {500'000'000ULL, 1'000'000ULL}}) {
    NativeSourceProgress jump;
    assert(jump.Observe(epoch,local));
    assert(!jump.Observe(epoch+pair.first,local+pair.second));
    assert(!jump.fault().empty());
  }
  NativeSourceProgress missing;
  assert(!missing.Observe(0U, local));
  assert(!missing.Fresh(local+2'000'000'000ULL));
  NativeSourceProgress negative;
  assert(!negative.Observe(static_cast<std::uint64_t>(-1LL), local));
  NativeStateClock backup;
  Fill(backup);
  const auto gap = local + 1'450'000'000ULL;
  assert(!backup.PrimarySportFresh(gap));
  for (int i = 0; i <= 12; ++i) {
    const auto at = gap + static_cast<std::uint64_t>(i) * 100'000'000ULL;
    assert(backup.ObserveSportFallback(
        epoch + 2'000'000'000ULL + static_cast<std::uint64_t>(i) * 100'000'000ULL, at));
  }
  assert(backup.SportFresh(gap + 1'200'000'000ULL));
  assert(!backup.SportFresh(gap + 1'200'000'000ULL + 1'100'000'000ULL));
  assert(!backup.ObserveSportFallback(epoch + 2'000'000'000ULL, gap + 1'300'000'000ULL));
  assert(backup.fault().empty());  // a low-frequency replay must not fault the primary clock

  std::cout << "native DDS/lowstate tick qualification, replay, drift and timeout: PASS\n";
}
