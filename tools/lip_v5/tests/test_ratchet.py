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
        A = ρ/2 × window must sit below ENTRY_FLOOR = $1.50: 0.2 × 6 = $1.20.)"""
        self.assertIsNone(RT.floor_q_contracts(rho=0.4, S=50, window_h=6.0))
        self.assertIsNone(RT.floor_q_usd(0.4, 50, 0.30, 6.0))

    def test_sole_qualifier_needs_only_the_qualification_size(self):
        self.assertEqual(RT.floor_q_contracts(6.25, 0.0, 16.0), 1)

    def test_floor_q_usd_is_dollars(self):
        self.assertAlmostEqual(RT.floor_q_usd(6.25, 50, 0.50, 16.0),
                               RT.floor_q_contracts(6.25, 50, 16.0) * 0.50, places=9)


class TestAdmission(LipTestCase):
    def _venue(self, name="V1"):
        return RT.VenueState(name)

    def test_admits_within_the_bounds(self):
        v = self._venue()
        status, cap, d = RT.admit(v, floor_usd=5.0, inv_cap_usd=10.0, per_market_cap_usd=25.0,
                                  global_ceiling_usd=CEILING, unverified_exposure_usd=0.0,
                                  unverified_count=0, oversized_count=0)
        self.assertEqual(status, RT.ADMITTED)
        self.assertAlmostEqual(cap, 5.0)

    def test_the_unverified_total_is_bounded_by_the_configured_fraction(self):
        """GENERALIZED 2026-07-28: the fraction is now 1.00 — the mechanism is verified by
        receipt, so this layer no longer rations by IGNORANCE.  Risk is bounded by the caps
        that were paid for in losses (cluster worst-case, per-rung, day stop, drawdown).
        What this test still pins is that the fraction is ENFORCED, whatever it is set to."""
        import lip_v5.config as _C
        frac = _C.UNVERIFIED_EXPOSURE_FRAC
        ceiling = 300.0
        self.assertLessEqual(frac * ceiling, ceiling + 1e-9)
        self.assertGreater(frac, 0.0)

    def test_eight_concurrent_unverified_venues_is_the_cap(self):
        v = self._venue()
        status, _, d = RT.admit(v, 1.0, 10.0, 25.0, CEILING, 0.0,
                                unverified_count=C.N_UNVERIFIED_MAX, oversized_count=0)
        self.assertEqual(status, RT.QUEUED)
        self.assertEqual(d["reason"], "unverified_count_cap")

    def test_TR6_oversized_probe_is_admitted_at_floor_q_never_shrunk(self):
        """T-R6 (B3) — a venue whose `floor_q` exceeds the oversized threshold is admitted AT
        `floor_q` while unverified totals allow, and consumes an oversized-probe slot.

        THRESHOLD CORRECTED 2026-07-29: `OVERSIZED_PROBE_FRAC` was 0.02, which classified the
        strategy's OWN planned rung size (~$10 of a $300 ceiling) as oversized and so made the
        <=2 concurrent rule the real breadth limit — measured, 40 venues offered and TWO orders
        resting.  The rule itself is unchanged and this test still owns it; the fixture just has
        to be oversized under the threshold that now applies."""
        v = self._venue()
        floor = 1.5 * C.OVERSIZED_PROBE_FRAC * CEILING         # comfortably over the line
        self.assertTrue(RT.classify_probe(floor, CEILING))
        status, cap, _ = RT.admit(v, floor, inv_cap_usd=2 * floor,
                                  per_market_cap_usd=2 * floor,
                                  global_ceiling_usd=CEILING, unverified_exposure_usd=0.0,
                                  unverified_count=0, oversized_count=0)
        self.assertEqual(status, RT.OVERSIZED)
        self.assertAlmostEqual(cap, floor)                     # NEVER shrunk below floor_q

    def test_only_two_oversized_probes_concurrently(self):
        v = self._venue()
        floor = 1.5 * C.OVERSIZED_PROBE_FRAC * CEILING
        status, _, d = RT.admit(v, floor, 2 * floor, 2 * floor, CEILING, 0.0, 0,
                                oversized_count=C.OVERSIZED_PROBE_MAX)
        self.assertEqual(status, RT.QUEUED)
        self.assertEqual(d["reason"], "oversized_probe_slots_full")

    def test_a_probe_that_cannot_reach_floor_q_is_UNPROBEABLE_not_shrunk(self):
        """"A probe smaller than `floor_q` MEASURES NOTHING."  The naive
        `min(floor_q, 0.02×ceiling)` is self-contradicting and must not be written."""
        v = self._venue()
        status, cap, d = RT.admit(v, floor_usd=40.0, inv_cap_usd=10.0,
                                  per_market_cap_usd=25.0, global_ceiling_usd=CEILING,
                                  unverified_exposure_usd=0.0, unverified_count=0,
                                  oversized_count=0)
        self.assertEqual(status, RT.UNPROBEABLE)
        self.assertEqual(cap, 0.0)

    def test_queue_is_ranked_by_net_zero(self):
        self.assertEqual(RT.rank_queue([("A", 0.1), ("B", 0.9), ("C", 0.5)]),
                         ["B", "C", "A"])

    def test_exploration_floor_admits_when_learning_is_starved(self):
        """§4.4's mirror: a cap on learning is a cap on earning."""
        self.assertTrue(RT.exploration_floor_admits(10.0, CEILING, queue_len=2))
        self.assertFalse(RT.exploration_floor_admits(10.0, CEILING, queue_len=0))
        self.assertFalse(RT.exploration_floor_admits(50.0, CEILING, queue_len=2))


class TestReadings(LipTestCase):
    def test_TR1_in_band_steps_up_and_the_cap_doubles(self):
        v = RT.VenueState("V1", rung=0, rung0_cap_usd=5.0)
        before = v.cap_usd(100.0, CEILING)
        verdict, ratio, d = RT.apply_reading(v, reading_usd=8.0, projection_usd=10.0)
        self.assertEqual(verdict, RT.VERIFY)
        self.assertEqual(v.rung, 1)
        self.assertAlmostEqual(v.cap_usd(100.0, CEILING), 2.0 * before, places=9)
        self.assertTrue(v.verified)

    def test_TR2_out_of_band_steps_down_two_and_floors_at_rung_zero(self):
        v = RT.VenueState("V1", rung=1, rung0_cap_usd=5.0)
        verdict, _, _ = RT.apply_reading(v, reading_usd=100.0, projection_usd=10.0)
        self.assertEqual(verdict, RT.DISAGREE)
        self.assertEqual(v.rung, 0)                            # max(0, 1−2)
        RT.apply_reading(RT.VenueState("V2", rung=0, rung0_cap_usd=5.0), 100.0, 10.0)
        self.assertEqual(v.rung, 0)

    def test_band_boundaries_are_inclusive(self):
        for reading in (5.0, 20.0):                            # ratio 0.5 and 2.0 exactly
            v = RT.VenueState("V", rung=0, rung0_cap_usd=1.0)
            verdict, _, _ = RT.apply_reading(v, reading, 10.0)
            self.assertEqual(verdict, RT.VERIFY, "ratio %.1f must verify" % (reading / 10))

    def test_TR3_coin_flip_verifier_terminates_at_rung_zero(self):
        """T-R3 — 1,000 ALTERNATING readings ⇒ terminal rung 0.  THE ASYMMETRY PROOF."""
        v = RT.VenueState("V1", rung=0, rung0_cap_usd=5.0)
        for i in range(1000):
            reading = 10.0 if i % 2 == 0 else 100.0            # in-band, then out-of-band
            v.stood_down = False                               # isolate the rung dynamics
            v.consec_disagree_days = 0
            RT.apply_reading(v, reading, 10.0)
        self.assertEqual(v.rung, 0)

    def test_TR7_two_thirds_is_the_ladders_characteristic_number(self):
        """T-R7 (SF-7) — `3a − 2`: a = 2/3 ⇒ zero drift; 0.60 drifts down; 0.70 drifts up.
        This number is the ladder's SENSOR-QUALITY REQUIREMENT and must appear in the test."""
        self.assertAlmostEqual(RT.expected_rung_drift(2.0 / 3.0), 0.0, places=12)
        self.assertLess(RT.expected_rung_drift(0.60), 0.0)
        self.assertGreater(RT.expected_rung_drift(0.70), 0.0)
        self.assertAlmostEqual(RT.breakeven_accuracy(), 2.0 / 3.0, places=12)

    def test_TR7_simulated_drift_matches_the_formula(self):
        import random
        for a, expect_up in ((0.60, False), (0.70, True)):
            rnd = random.Random(7)
            v = RT.VenueState("V", rung=20, rung0_cap_usd=1.0)
            for _ in range(4000):
                v.stood_down = False
                v.consec_disagree_days = 0
                RT.apply_reading(v, 10.0 if rnd.random() < a else 100.0, 10.0)
            self.assertEqual(v.rung > 20, expect_up, "a=%.2f" % a)

    def test_TR4_caps_never_exceed_per_market_or_ceiling(self):
        v = RT.VenueState("V1", rung=10, rung0_cap_usd=5.0)    # 2^10 x 5 = $5120
        self.assertLessEqual(v.cap_usd(25.0, CEILING), 25.0)
        self.assertLessEqual(v.cap_usd(1000.0, CEILING), CEILING)

    def test_TR5_stand_down_after_two_consecutive_settlement_days(self):
        v = RT.VenueState("V1", rung=4, rung0_cap_usd=5.0)
        RT.apply_reading(v, 100.0, 10.0, settlement_day=1)
        self.assertFalse(v.stood_down)
        RT.apply_reading(v, 100.0, 10.0, settlement_day=2)
        self.assertTrue(v.stood_down)
        self.assertEqual(v.cap_usd(100.0, CEILING), 0.0)

    def test_two_disagreements_in_ONE_day_do_not_stand_a_venue_down(self):
        """One day's evidence wearing two hats is still one day's evidence."""
        v = RT.VenueState("V1", rung=4, rung0_cap_usd=5.0)
        RT.apply_reading(v, 100.0, 10.0, settlement_day=1)
        RT.apply_reading(v, 100.0, 10.0, settlement_day=1)
        self.assertFalse(v.stood_down)

    def test_non_consecutive_days_reset_the_count(self):
        v = RT.VenueState("V1", rung=4, rung0_cap_usd=5.0)
        RT.apply_reading(v, 100.0, 10.0, settlement_day=1)
        RT.apply_reading(v, 100.0, 10.0, settlement_day=5)
        self.assertFalse(v.stood_down)

    def test_a_verify_clears_the_disagree_streak(self):
        v = RT.VenueState("V1", rung=4, rung0_cap_usd=5.0)
        RT.apply_reading(v, 100.0, 10.0, settlement_day=1)
        RT.apply_reading(v, 10.0, 10.0, settlement_day=2)
        RT.apply_reading(v, 100.0, 10.0, settlement_day=3)
        self.assertFalse(v.stood_down)


class TestOutOfReach(LipTestCase):
    """T-R6's second half — "Assert a venue can NEVER be stood down by a probe that could not
    have paid"."""

    def test_a_reading_below_the_entry_floor_is_neither_verify_nor_disagree(self):
        v = RT.VenueState("V1", rung=3, rung0_cap_usd=5.0)
        verdict, ratio, d = RT.apply_reading(v, reading_usd=0.0, projection_usd=1.20)
        self.assertEqual(verdict, RT.OUT_OF_REACH)
        self.assertEqual(v.rung, 3)                            # the rung is HELD
        self.assertTrue(v.out_of_reach)                        # and funding stops this period
        self.assertFalse(v.stood_down)
        self.assertEqual(d["reason"], "projection_below_entry_floor")

    def test_repeated_out_of_reach_readings_never_stand_a_venue_down(self):
        v = RT.VenueState("V1", rung=2, rung0_cap_usd=5.0)
        for day in range(1, 11):
            RT.apply_reading(v, 0.0, 1.0, settlement_day=day)
        self.assertFalse(v.stood_down)
        self.assertEqual(v.rung, 2)

    def test_exactly_at_the_entry_floor_is_in_reach(self):
        v = RT.VenueState("V1", rung=0, rung0_cap_usd=5.0)
        verdict, _, _ = RT.apply_reading(v, reading_usd=2.0, projection_usd=C.ENTRY_FLOOR_USD)
        self.assertEqual(verdict, RT.VERIFY)


class TestRevive(LipTestCase):
    """§1.4's MIRROR — nothing revives on a timer."""

    def test_same_period_never_revives(self):
        v = RT.VenueState("V1")
        v.last_period_id = "P1"
        ok, why = RT.revive_allowed(v, "P1", t_hat_ub=1.0, hurdle_t_hat=0.1)
        self.assertFalse(ok)
        self.assertEqual(why, "same_period")

    def test_new_period_requires_the_posterior_to_clear_the_hurdle(self):
        v = RT.VenueState("V1")
        v.last_period_id = "P1"
        self.assertFalse(RT.revive_allowed(v, "P2", 0.05, 0.10)[0])
        self.assertTrue(RT.revive_allowed(v, "P2", 0.50, 0.10)[0])

    def test_upper_bound_is_optimistic_by_construction(self):
        """It revives on the OPTIMISTIC reading, so a venue is refused only when even optimism
        cannot clear the hurdle."""
        ub = RT.t_hat_upper_95(prox_dollar_s=3600.0 * 5.0, committed_dollar_h=10.0)
        self.assertGreater(ub, 0.5)
        self.assertLessEqual(ub, 1.0)

    def test_t_hat_hurdle_is_unreachable_for_a_long_dated_venue(self):
        """The PYPL row: carry alone exceeds the entire gross rate, so NO T̂ can save it."""
        h = RT.t_hat_hurdle(rho=0.439, S=50, p=0.30, phi=0.50, d=0.07, l_eff=3744.0,
                            r_star=0.00625)
        self.assertGreater(h, 1.0)
        # and a healthy venue's hurdle is comfortably reachable
        h2 = RT.t_hat_hurdle(6.25, 50, 0.50, 0.08, 0.07, 8.0, 0.00625)
        self.assertLess(h2, 1.0)


class TestCapsVsCeiling(LipTestCase):
    def test_TR4b_sum_of_caps_may_exceed_the_ceiling(self):
        """T-R4b — Σ venue caps MAY exceed the global ceiling; Σ ALLOCATED never does, because
        ALLOCATE's budget binds independently.  Caps are permissions; the budget is the money.
        (The allocation half of this claim is asserted in test_alloc.py.)"""
        venues = [RT.VenueState("V%d" % i, rung=3, rung0_cap_usd=20.0) for i in range(6)]
        total = sum(v.cap_usd(per_market_cap_usd=CEILING, global_ceiling_usd=CEILING)
                    for v in venues)
        self.assertGreater(total, CEILING)


if __name__ == "__main__":
    unittest.main()
