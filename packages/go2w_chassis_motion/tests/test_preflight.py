"""The audit must never mistake live DDS delivery for valid payload time."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "native_motion_preflight", Path(__file__).resolve().parents[3] / "native/check_motion.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def sample(wall=100.0, stamp=99.98, dds=99.98):
    return {"topic": "/lf/sportmodestate", "wall_ns": int(wall * 1e9),
            "payload_sec": int(stamp), "payload_nsec": int((stamp % 1) * 1e9),
            "dds_source_ns": int(dds * 1e9), "mode": 5, "error_code": 0,
            "quaternion_norm_squared": 1.0, "velocity": [0.0, 0.0, 0.0],
            "yaw_speed": 0.0}


class PreflightTest(unittest.TestCase):
    def test_live_dds_timestamps_do_not_hide_zero_payload_timestamps(self):
        _, blockers = preflight.audit_samples([
            sample(stamp=0), sample(wall=100.1, stamp=0, dds=100.08)])
        self.assertTrue(any("are zero" in item for item in blockers))
        self.assertTrue(any("do not advance" in item for item in blockers))

    def test_writer_clock_offset_is_reported_without_restamping(self):
        records = [sample(dds=20), sample(wall=100.1, stamp=100.08, dds=20.1)]
        report, blockers = preflight.audit_samples(records)
        self.assertEqual(records[0]["dds_source_ns"], 20_000_000_000)
        self.assertEqual(report["dds_local_minus_source_seconds"], [80.0, 80.0])
        self.assertFalse(report["dds_writer_clock_aligned_with_host"])
        self.assertEqual(blockers, [])  # The selected payload clock is valid.

    def test_fresh_advancing_stationary_state_has_no_state_blocker(self):
        _, blockers = preflight.audit_samples([
            sample(), sample(wall=100.1, stamp=100.08, dds=100.08)])
        self.assertEqual(blockers, [])

    def test_no_samples_stale_or_regressing_state_does_not_pass(self):
        for records in [[], [sample(stamp=90)],
                        [sample(stamp=99.98), sample(wall=100.1, stamp=99.97)]]:
            with self.subTest(records=records):
                self.assertTrue(preflight.audit_samples(records)[1])

    def test_native_basis_requires_qualified_dds_and_tick_not_restamped_payload(self):
        records = [sample(stamp=0,dds=20),
                   sample(wall=100.1,stamp=0,dds=20.1)]
        basis = "dds-source-lowstate-tick-v1"
        self.assertTrue(preflight.audit_samples(records,basis)[1])
        records.append({"native_transport_fresh":True,"native_clock_fault":""})
        report,blockers=preflight.audit_samples(records,basis)
        self.assertEqual(blockers,[])
        self.assertEqual(report["payload_source_stamps"],[0,0])
        self.assertFalse(report["absolute_measurement_age_verified"])
        records[-1]["native_clock_fault"]="lowstate_tick_regressed"
        self.assertTrue(preflight.audit_samples(records,basis)[1])
