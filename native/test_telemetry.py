import unittest

from telemetry import build_health, freshness


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.record = dict(received_monotonic=9.9, source_stamp=99.9, frame_id="livox_frame")

    def valid(self, **updates):
        record = dict(self.record, **updates)
        return freshness(record, 100, 10, 0.5, 0.05, 1.0)

    def test_rejects_delayed_source_even_if_receipt_is_fresh(self):
        self.assertEqual(self.valid(source_stamp=98), (False, "source_stale"))

    def test_rejects_zero_timestamp_instead_of_restamping(self):
        self.assertEqual(self.valid(source_stamp=0), (False, "source_stamp_missing"))

    def test_rejects_future_timestamp(self):
        self.assertEqual(self.valid(source_stamp=101), (False, "source_future"))

    def test_rejects_missing_frame_and_stale_receipt(self):
        self.assertEqual(self.valid(frame_id=""), (False, "frame_missing"))
        self.assertEqual(self.valid(received_monotonic=8), (False, "receipt_stale"))

    def test_uses_receipt_only_explicitly_for_headerless_battery(self):
        self.assertEqual(freshness(
            {"received_monotonic": 9.9}, 100, 10, 0.5, 0.05, 1.0, False),
            (True, "receipt_only"))

    def test_accepts_measured_fresh_source(self):
        self.assertEqual(self.valid(), (True, "fresh"))

    def test_invalid_odometry_orientation_is_not_navigation_ready(self):
        self.assertEqual(self.valid(quaternion=[0, 0, 0, 0]),
                         (False, "invalid_orientation"))
        self.assertEqual(self.valid(quaternion=[0, 0, 0, 1]), (True, "fresh"))

    def test_stale_battery_becomes_unknown(self):
        records = {"lowstate": dict(received_monotonic=8, voltage=32.0, battery_percent=94)}
        cfg = dict(max_source_age_s=0.5, max_source_future_s=0.05, max_receipt_age_s=1.0)
        health = build_health(records, cfg, now_wall=100, now_mono=10)
        self.assertEqual(health["voltage"], -1)
        battery = next(row for row in health["readings"] if row["name"] == "body/battery")
        self.assertEqual(battery["battery_percent"], -1)

    def test_health_preserves_measured_battery(self):
        cfg = dict(max_source_age_s=0.5, max_source_future_s=0.05, max_receipt_age_s=1.0)
        health = build_health(
            {"lowstate": dict(received_monotonic=9.9, voltage=31.9, battery_percent=94)},
            cfg, now_wall=100, now_mono=10,
        )
        self.assertEqual(health["voltage"], 31.9)
        battery = next(row for row in health["readings"] if row["name"] == "body/battery")
        self.assertEqual(battery["battery_percent"], 94)

    def test_expired_camera_reports_stale_while_retaining_original_timestamp_checks(self):
        cfg = dict(max_source_age_s=0.5, max_source_future_s=0.05, max_receipt_age_s=1.0)
        for source_stamp, stale in ((99.0, 1.0), (99.9, 0.0), (101.0, 0.0)):
            records = {"camera": dict(self.record, source_stamp=source_stamp)}
            health = build_health(records, cfg, now_wall=100, now_mono=10)
            readings = {r["name"]: r for r in health["readings"]}
            prefix = "body/front_camera"
            self.assertEqual(readings[prefix + "/stale"]["current_a"], stale)
            self.assertEqual(readings[prefix + "/online"]["current_a"],
                             float(source_stamp == 99.9))

    def test_sixteen_motor_temperatures_map_onto_the_four_wheels(self):
        cfg = dict(max_source_age_s=0.5, max_source_future_s=0.05, max_receipt_age_s=1.0)
        temperatures = list(range(16))
        health = build_health(
            {"lowstate": dict(received_monotonic=9.9, voltage=29.4,
                              battery_percent=51, motor_temperatures_c=temperatures)},
            cfg, now_wall=100, now_mono=10,
        )
        readings = {row["name"]: row for row in health["readings"]}
        for component, temp_c in (
            ("body/FL_foot", 12),
            ("body/FR_foot", 13),
            ("body/RL_foot", 14),
            ("body/RR_foot", 15),
        ):
            self.assertEqual(readings[component]["temp_c"], temp_c)
            self.assertEqual(readings[component + "/online"]["current_a"], 1)
            self.assertEqual(readings[component + "/communication_ok"]["current_a"], 1)
        self.assertNotIn("body/base/body/FR_foot/online", readings)

    def test_short_motor_list_does_not_invent_wheel_faults(self):
        cfg = dict(max_source_age_s=0.5, max_source_future_s=0.05, max_receipt_age_s=1.0)
        health = build_health(
            {"lowstate": dict(received_monotonic=9.9, voltage=29.4,
                              battery_percent=51, motor_temperatures_c=[30, 31])},
            cfg, now_wall=100, now_mono=10,
        )
        names = [row["name"] for row in health["readings"]]
        self.assertFalse(any("foot" in name for name in names))


if __name__ == "__main__":
    unittest.main()
