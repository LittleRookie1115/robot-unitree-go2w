import unittest
from go2w_chassis_motion.state_guard import NativeWitness


def populate(witness, start=10.0):
    for i in range(121):
        now = start+i*.01
        witness.observe(0, 5, 0, [1,0,0,0], [0,0,0], 1000+now, now)
        witness.observe_tick(1000+i*10, now)
        if i % 10 == 0:
            witness.observe_dds(1_000_000_000+i*10_000_000, now)


class NativeStateTest(unittest.TestCase):
    def test_qualified_tick_and_dds_progress_preserve_missing_payload_stamp(self):
        witness = NativeWitness({5})
        populate(witness)
        self.assertTrue(witness.ready(999999, 11.2))
        self.assertEqual(witness.source_stamp, 0)
        self.assertFalse(witness.ready(999999, 11.401))

    def test_replayed_tick_cannot_be_hidden_by_fresh_health_and_dds(self):
        witness = NativeWitness({5})
        populate(witness)
        for i in range(1,5):
            now = 11.2+i*.1
            witness.observe(0,5,0,[1,0,0,0],[0,0,0],1000,now)
            witness.observe_tick(2200,now)
            witness.observe_dds(2_200_000_000+i*100_000_000,now)
        self.assertFalse(witness.ready(1000,11.6))

    def test_tick_reset_and_dds_regression_latch_until_restart(self):
        for kind in ("tick","dds"):
            witness = NativeWitness({5})
            populate(witness)
            if kind == "tick":
                witness.observe_tick(3,11.21)
                witness.observe_tick(2300,11.3)
            else:
                witness.observe_dds(1_000_000_000,11.21)
                witness.observe_dds(3_000_000_000,11.3)
            self.assertTrue(witness.fault)
            self.assertFalse(witness.ready(1000,11.3))

    def test_invalid_health_is_rejected_even_with_live_clocks(self):
        for mode,error,q in [(6,0,[1,0,0,0]),(5,1,[1,0,0,0]),(5,0,[0,0,0,0]),
                             (5,0,[float("nan"),0,0,0])]:
            witness = NativeWitness({5})
            populate(witness)
            witness.observe(0,mode,error,q,[0,0,0],1000,11.2)
            self.assertFalse(witness.ready(1000,11.2))
            self.assertEqual(witness.unready_reason(11.2), "sport_payload_invalid")

    def test_low_frequency_backup_covers_a_primary_gap(self):
        witness = NativeWitness({5})
        populate(witness)
        self.assertFalse(witness.ready(1000, 11.45))
        for i in range(1, 13):
            now = 11.2 + i * 0.1
            witness.observe(0, 5, 0, [1, 0, 0, 0], [0, 0, 0], 1000, now, source="fallback")
            witness.observe_tick(2200 + i * 100, now)
            witness.observe_dds(2_200_000_000 + i * 100_000_000, now)
        self.assertTrue(witness.ready(1000, 12.4))
        self.assertEqual(witness.sport_source, "fallback")
        self.assertEqual(witness.unready_reason(13.5), "sport_state_stale")

    def test_recent_invalid_primary_is_not_hidden_by_low_frequency_backup(self):
        witness = NativeWitness({5})
        populate(witness)
        witness.observe(0, 7, 0, [1, 0, 0, 0], [0, 0, 0], 1000, 11.25)
        witness.observe(0, 5, 0, [1, 0, 0, 0], [0, 0, 0], 1000, 11.25, source="fallback")
        self.assertFalse(witness.ready(1000, 11.25))
        self.assertEqual(witness.unready_reason(11.25), "sport_payload_invalid")
