"""Independent SportModeState freshness witness usable without ROS or SDK."""
import math


class SportWitness:
    def __init__(self, allowed_modes):
        self.allowed_modes = set(allowed_modes)
        self.source_stamp = 0.0
        self.received_at = 0.0
        self.valid = False
        self.fault = ""

    def observe(self, stamp, mode, error, quaternion, values, now_wall, now_mono):
        if stamp < self.source_stamp:
            self.fault = "source_timestamp_regressed"
            self.valid = False
            return
        if stamp == self.source_stamp:
            return  # duplicates never refresh the accepted receipt time
        self.source_stamp = stamp
        self.valid = (
            math.isfinite(stamp) and stamp > 0 and -0.05 <= now_wall - stamp <= 0.20
            and mode in self.allowed_modes and error == 0
            and all(math.isfinite(value) for value in list(quaternion) + list(values))
            and len(quaternion) == 4
            and abs(sum(value * value for value in quaternion) - 1.0) <= 0.1
            and not self.fault
        )
        if self.valid:
            self.received_at = now_mono

    def ready(self, now_wall, now_mono):
        return (self.valid and not self.fault and now_mono - self.received_at <= 0.20
                and -0.05 <= now_wall - self.source_stamp <= 0.20)


PRIMARY_SPORT_FRESH_S = 0.20
FALLBACK_SPORT_FRESH_S = 1.00


class _SportStream:
    """One SportModeState subscription. Primary and fallback stay independent."""

    def __init__(self):
        self.messages = self.valid_messages = 0
        self.valid = False
        self.received_at = 0.0
        self.last_message_at = None
        self.source_stamp = 0.0
        self.mode = 255
        self.last_health_rejections = []
        self.last_error_code = None
        self.last_quaternion_norm = None

    def observe(self, stamp, mode, error, quaternion, values, allowed_modes, now_mono):
        self.messages += 1
        self.last_message_at = now_mono
        self.source_stamp = stamp
        self.mode = mode
        norm = sum(value * value for value in quaternion)
        self.last_error_code = error
        self.last_quaternion_norm = norm if math.isfinite(norm) else None
        rejected = []
        if mode not in allowed_modes:
            rejected.append("mode_not_allowed")
        if error != 0:
            rejected.append("robot_error_code")
        if not all(math.isfinite(value) for value in list(quaternion) + list(values)):
            rejected.append("non_finite_state")
        if len(quaternion) != 4:
            rejected.append("quaternion_size")
        if not abs(norm - 1.0) <= 0.1:
            rejected.append("quaternion_norm")
        self.last_health_rejections = rejected
        self.valid = not rejected
        if self.valid:
            self.valid_messages += 1
            self.received_at = now_mono

    def fresh(self, now, limit):
        return (self.valid and self.valid_messages > 0
                and 0 <= now - self.received_at <= limit)


class NativeWitness:
    """Raw health, lowstate tick and original DDS-progress diagnostics.

    High-frequency ``/sportmodestate`` is the primary stream and keeps the
    200 ms gate. ``/lf/sportmodestate`` is only a backup after that primary
    receipt has stopped, and its own period is allowed up to one second.
    This proves stream continuity for bounded manual control. It never
    creates a hardware timestamp or claims an absolute measurement age.
    The C++ adapter independently validates DDS elapsed-time consistency.
    """
    def __init__(self, allowed_modes):
        self.allowed_modes = set(allowed_modes)
        self.primary = _SportStream()
        self.fallback = _SportStream()
        self.sport_source = "none"
        self.source_stamp = 0.0
        self.received_at = 0.0
        self.valid = False
        self.fault = ""
        self.mode = 255
        self._tick = None
        self._tick_first = self._tick_at = 0.0
        self._tick_elapsed = 0
        self._tick_count = 0
        self._dds = 0
        self._dds_first = self._dds_at = 0.0
        self._dds_count = 0
        self.sport_messages = self.sport_valid_messages = 0
        self.tick_messages = self.dds_diagnostics = 0
        self._sport_message_at = self._tick_message_at = self._dds_message_at = None
        self.last_health_rejections = []
        self.last_error_code = None
        self.last_quaternion_norm = None

    def observe(self, stamp, mode, error, quaternion, values, now_wall, now_mono,
                source="primary"):
        del now_wall  # payload time is not a liveness clock on this robot
        stream = self.fallback if source == "fallback" else self.primary
        stream.observe(stamp, mode, error, quaternion, values, self.allowed_modes, now_mono)
        self._select(now_mono)

    def _recent_invalid_primary(self, now):
        seen = self.primary.last_message_at
        return (seen is not None and not self.primary.valid
                and 0 <= now - seen <= PRIMARY_SPORT_FRESH_S)

    def _select(self, now):
        self.sport_messages = self.primary.messages + self.fallback.messages
        self.sport_valid_messages = (
            self.primary.valid_messages + self.fallback.valid_messages)
        stamps = [item for item in (self.primary.last_message_at,
                                    self.fallback.last_message_at) if item is not None]
        self._sport_message_at = max(stamps) if stamps else None
        if self.primary.fresh(now, PRIMARY_SPORT_FRESH_S):
            chosen, name = self.primary, "primary"
        elif (not self._recent_invalid_primary(now)
              and self.fallback.fresh(now, FALLBACK_SPORT_FRESH_S)):
            chosen, name = self.fallback, "fallback"
        else:
            self.sport_source = "none"
            self.valid = False
            newest = self.primary if (
                (self.primary.last_message_at or -1) >= (self.fallback.last_message_at or -1)
            ) else self.fallback
            self.mode = newest.mode
            self.last_health_rejections = list(newest.last_health_rejections)
            self.last_error_code = newest.last_error_code
            self.last_quaternion_norm = newest.last_quaternion_norm
            return
        self.sport_source = name
        self.valid = True
        self.received_at = chosen.received_at
        self.source_stamp = chosen.source_stamp
        self.mode = chosen.mode
        self.last_health_rejections = list(chosen.last_health_rejections)
        self.last_error_code = chosen.last_error_code
        self.last_quaternion_norm = chosen.last_quaternion_norm

    def sport_fresh(self, now):
        self._select(now)
        return self.sport_source != "none"

    def _dds_limit(self, now):
        # The diagnostic source timestamp follows whichever sport stream is
        # currently allowed to prove liveness. A repeated low-frequency
        # timestamp is not a dead stream while that backup is still inside
        # its one-second period.
        return PRIMARY_SPORT_FRESH_S if self.primary.fresh(now, PRIMARY_SPORT_FRESH_S) else FALLBACK_SPORT_FRESH_S

    def observe_tick(self, tick, now):
        self.tick_messages += 1
        self._tick_message_at = now
        if isinstance(tick, bool) or not isinstance(tick, int) or not 0 <= tick <= 0xffffffff:
            self.fault = "invalid_lowstate_tick"
            return
        if self._tick is None:
            self._tick, self._tick_first, self._tick_at = tick, now, now
            self._tick_count = 1
            return
        delta = (tick-self._tick) & 0xffffffff
        if delta == 0:
            return
        if delta >= 0x80000000 or now < self._tick_at:
            self.fault = "lowstate_tick_regressed"
            return
        self._tick_elapsed += delta
        difference = (now-self._tick_first)-self._tick_elapsed/1000
        if not -0.05 <= difference <= 0.20:
            self.fault = "lowstate_tick_rate_or_clock_jump"
            return
        self._tick, self._tick_at = tick, now
        self._tick_count += 1

    def observe_dds(self, source_ns, now):
        self.dds_diagnostics += 1
        self._dds_message_at = now
        if source_ns <= 0:
            return
        if source_ns < self._dds:
            self.fault = "dds_source_regressed"
            return
        if source_ns == self._dds:
            return
        if self._dds == 0:
            self._dds_first = now
        self._dds, self._dds_at = source_ns, now
        self._dds_count += 1

    def ready(self, now_wall, now_mono):
        del now_wall
        return (
            self.sport_fresh(now_mono) and not self.fault
            and self._tick_count >= 8 and now_mono-self._tick_first >= 1.0
            and 0 <= now_mono-self._tick_at <= 0.20
            and self._dds_count >= 8 and now_mono-self._dds_first >= 1.0
            and 0 <= now_mono-self._dds_at <= self._dds_limit(now_mono)
        )

    def unready_reason(self, now_mono):
        """Name the failed gate instead of collapsing every miss into one string."""
        if self.fault:
            return self.fault
        if not self.sport_fresh(now_mono):
            return ("sport_payload_invalid" if self._recent_invalid_primary(now_mono)
                    else "sport_state_stale")
        if (self._tick_count < 8 or now_mono-self._tick_first < 1.0
                or not 0 <= now_mono-self._tick_at <= 0.20):
            return "lowstate_tick_stale"
        if (self._dds_count < 8 or now_mono-self._dds_first < 1.0
                or not 0 <= now_mono-self._dds_at <= self._dds_limit(now_mono)):
            return "dds_progress_stale"
        return "sport_state_invalid_or_stale"

    def details(self, now_mono):
        """Explain the unchanged guard using raw receipts and accepted progress separately."""
        self._select(now_mono)
        limit = self._dds_limit(now_mono)
        blockers = []
        if self.fault:
            blockers.append(self.fault)
        if not self.sport_fresh(now_mono):
            blockers.append("sport_payload_invalid" if self._recent_invalid_primary(now_mono)
                            else "sport_valid_receipt_stale")
        if self._tick_count < 8 or now_mono-self._tick_first < 1.0:
            blockers.append("lowstate_tick_not_qualified")
        if not 0 <= now_mono-self._tick_at <= .20:
            blockers.append("lowstate_tick_progress_stale")
        if self._dds_count < 8 or now_mono-self._dds_first < 1.0:
            blockers.append("dds_progress_not_qualified")
        if not 0 <= now_mono-self._dds_at <= limit:
            blockers.append("dds_diagnostic_progress_stale")

        def age(value):
            return None if value is None else now_mono-value

        return {
            "ready": self.ready(0, now_mono), "blockers": blockers,
            "freshness_limit_sec": limit, "sport_source": self.sport_source,
            "clock_fault": self.fault,
            "sport_messages": self.sport_messages,
            "sport_valid_messages": self.sport_valid_messages,
            "sport_message_age_sec": age(self._sport_message_at),
            "sport_valid_age_sec": age(self.received_at) if self.sport_valid_messages else None,
            "last_health_rejections": list(self.last_health_rejections),
            "mode": self.mode, "error_code": self.last_error_code,
            "quaternion_norm_squared": self.last_quaternion_norm,
            "lowstate_messages": self.tick_messages, "tick_progress_samples": self._tick_count,
            "lowstate_message_age_sec": age(self._tick_message_at),
            "tick_progress_age_sec": age(self._tick_at) if self._tick_count else None,
            "dds_diagnostic_messages": self.dds_diagnostics,
            "dds_progress_samples": self._dds_count,
            "dds_message_age_sec": age(self._dds_message_at),
            "dds_progress_age_sec": age(self._dds_at) if self._dds_count else None,
        }


def effective_session_enabled(session_enabled, diagnostic):
    # The controller's permission is only usable while the independent SDK
    # daemon still reports armed. This lets Pilot ask for a fresh human enable
    # instead of sending a command that can only fail with -5 (kNotArmed).
    return bool(session_enabled and diagnostic.get("daemon_armed") == "true")
