"""spec §8.3 — `ratchet(state, reading, model)`, T-R1..R7.

T-R3 and T-R7 together are the ASYMMETRY PROOF: with up-1/down-2 the expected drift per
reading is `3a − 2`, so a = 2/3 is the ladder's characteristic number and a coin-flip verifier
must terminate at rung 0.
"""

import unittest

from .. import config as C, ratchet as RT
from .base import LipTestCase

CEILING = 300.0


class TestFloorQ(LipTestCase):
    def test_floor_q_solves_the_share_equation(self):
        """`q ≥ F·S/(A − F)` with `A = (ρ/2)·window_h`."""
        rho, S, wh = 6.25, 50.0, 16.0
        A = (rho / 2) * wh                                    # $50 of side pool
        expect = int(-(-C.ENTRY_FLOOR_USD * S // (A - C.ENTRY_FLOOR_USD)))
        self.assertEqual(RT.floor_q_contracts(rho, S, wh), expect)
        # sanity: that q really does clear the floor
        q = RT.floor_q_contracts(rho, S, wh)
        self.assertGreaterEqual((q / (q + S)) * (rho / 2) * wh, C.ENTRY_FLOOR_USD - 1e-9)

    def test_a_venue_whose_whole_pool_is_below_the_floor_is_None(self):
        """No q clears it — funding it is the PayPal error in miniature.  (Side pool
        A = ρ/2 × window must sit below ENTRY_FLOOR = $1.00 since the owner set the
        target to the forfeit floor exactly: 0.1 × 6 = $0.60.)"""
        self.assertIsNone(RT.floor_q_contracts(rho=0.2, S=50, window_h=6.0))
        self.assertIsNone(RT.floor_q_usd(0.2, 50, 0.30, 6.0))

    def test_sole_qualifier_needs_only_the_qualification_size(self):
        self.assertEqual(RT.floor_q_contracts(6.25, 0.0, 16.0), 1)

    def test_floor_q_usd_is_dollars(self):
        self.assertAlmostEqual(RT.floor_q_usd(6.25, 50, 0.50, 16.0),
                               RT.floor_q_contracts(6.25, 50, 16.0) * 0.50, places=9)


class TestOutOfReachIsStillNotADisagreement(LipTestCase):
    """The one rule from the ratchet that had to survive its ladder: a venue must never be
    punished for a question it was never able to answer.  `classify_reading` now feeds the
    MEASURED DENY (engine.venue_reading) instead of a rung, and OUT_OF_REACH still means
    "the projection never cleared the entry floor", which counts no disagreement day."""

    def test_a_reading_below_the_entry_floor_is_neither_verify_nor_disagree(self):
        verdict, ratio = RT.classify_reading(0.0, projection_usd=C.ENTRY_FLOOR_USD - 0.30)
        self.assertEqual(verdict, RT.OUT_OF_REACH)
        self.assertIsNone(ratio)

    def test_exactly_at_the_entry_floor_is_in_reach(self):
        verdict, _ = RT.classify_reading(2.0, projection_usd=C.ENTRY_FLOOR_USD)
        self.assertNotEqual(verdict, RT.OUT_OF_REACH)

    def test_the_band_still_decides_verify_from_disagree(self):
        lo, hi = C.VERIFY_BAND
        self.assertEqual(RT.classify_reading(lo * 4.0, 4.0)[0], RT.VERIFY)
        self.assertEqual(RT.classify_reading(hi * 4.0, 4.0)[0], RT.VERIFY)
        self.assertEqual(RT.classify_reading(lo * 4.0 - 0.01, 4.0)[0], RT.DISAGREE)


class TestThePermissionMachineIsGone(LipTestCase):
    """Stage 1: the ladder a venue climbed by being probed is deleted, not disconnected.  A
    dormant permission machine is one import away from being consulted again."""

    def test_no_ladder_survives_in_the_module(self):
        for gone in ("VenueState", "admit", "rung0_cap", "classify_probe", "apply_reading",
                     "exploration_floor_admits", "rank_queue", "revive_allowed",
                     "expected_rung_drift", "breakeven_accuracy"):
            self.assertFalse(hasattr(RT, gone), gone)


if __name__ == "__main__":
    unittest.main()
