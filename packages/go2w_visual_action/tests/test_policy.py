import unittest

from go2w_visual_action.policy import (
    PERSON_REASON,
    SIGN_REASON,
    confirmed_forward,
    latch_risk,
    track_sign,
)


def decision(reason, allowed=False, prohibition=0.0):
    return {
        "forward_allowed": allowed,
        "reason": reason,
        "prohibition_probability": prohibition,
    }


class PolicyTest(unittest.TestCase):
    def test_obstacle_and_lidar_block_stay_latched(self):
        for reason in ("obstacle_or_uncertain", "lidar_blocked"):
            self.assertEqual(latch_risk(None, decision(reason)), reason)
            self.assertEqual(latch_risk(reason, decision("clear", allowed=True)), reason)

    def test_person_sign_and_missing_lidar_do_not_latch(self):
        for reason in (PERSON_REASON, SIGN_REASON, "lidar_unavailable", "lidar_stale", "lidar_no_return"):
            self.assertIsNone(latch_risk(None, decision(reason)))

    def test_sign_can_clear_after_three_low_readings(self):
        recent = None
        recent = track_sign(recent, decision(SIGN_REASON, prohibition=0.50))
        self.assertEqual(recent, [0.50])
        recent = track_sign(recent, decision(SIGN_REASON, prohibition=0.20))
        recent = track_sign(recent, decision(SIGN_REASON, prohibition=0.20))
        self.assertIsNone(recent)

    def test_not_observed_is_not_a_completed_motion(self):
        self.assertFalse(confirmed_forward({
            "status": "not_observed",
            "stop_acknowledged": True,
            "physical_motion_observed": False,
        }))
        self.assertFalse(confirmed_forward({
            "status": "completed",
            "stop_acknowledged": True,
            "physical_motion_observed": False,
        }))
        self.assertTrue(confirmed_forward({
            "status": "completed",
            "stop_acknowledged": True,
            "physical_motion_observed": True,
        }))
