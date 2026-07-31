"""THE CAPITAL DIALS (v6 stage 2) — N >= z^2 p(1-p)/(d-p)^2, A = C/N, and the floor-cap
coupling, each pinned to the note it comes from.

The worked numbers in these tests ARE the deploy numbers: at C = 600 and the measured
reference mix the rail is $21.43, which is note 55's own "~$20 seats".
"""

from .. import config as C, dials as D, marginal as MQ
from .. import alloc
from .base import LipTestCase


def slot(ticker="KXD-26JUL31-T1", side="bid", rho=1.0, S=100.0, p=0.20, phi=0.0,
         hours_left=24.0, accrued=0.0, **kw):
    return alloc.Slot(ticker, side, rho=rho, S=S, p=p, phi=phi, hours_left=hours_left,
                      accrued=accrued, target_size=1000, cum_size=2000.0,
                      land_grab_price_c=C.ENTRY_BAND_LO_C, **kw)


class TestTheRuinFormula(LipTestCase):
    def test_the_formula_is_the_notes_arithmetic(self):
        # note 55 derivation (5): N >= z^2 p(1-p)/(d-p)^2 at z = 2, d = 0.20
        self.assertAlmostEqual(D.n_required(0.09), 4.0 * 0.09 * 0.91 / (0.11 ** 2), places=9)
        self.assertAlmostEqual(D.n_required(0.09), 27.0743801652893, places=9)

    def test_N_rises_without_bound_as_p_approaches_d_and_says_dont_play_past_it(self):
        self.assertGreater(D.n_required(0.19), D.n_required(0.15))
        self.assertEqual(D.n_required(C.RUIN_D), float("inf"))
        self.assertEqual(D.n_required(0.5), float("inf"))

    def test_N_is_capital_independent_and_the_rail_scales_linearly(self):
        rows = [("KXA", 10.0, 0.197, 0.0, 24.0)]
        d3 = D.derive(300.0, rows)
        d6 = D.derive(600.0, rows)
        self.assertEqual(d3.n_clusters, d6.n_clusters)
        self.assertAlmostEqual(d6.rail_usd, 2.0 * d3.rail_usd, places=9)

    def test_the_deploy_numbers_at_C_600(self):
        """THE WORKED EXAMPLE.  Reference mix (19.7c) ⇒ p = 0.09 ⇒ n_required = 27.07, which
        the FLOOR (G1) rounds up to the note's own N = 30 ⇒ A = $20.00 — note 55's "run 30"
        and "~$20 seats at $600", exactly."""
        d = D.derive(600.0, [("KXA", 20.0, C.RUIN_P_REF_PRICE, 0.0, 24.0)])
        self.assertAlmostEqual(d.p, C.RUIN_P_BASE, places=6)
        self.assertAlmostEqual(d.n_required, 27.0743801652893, places=9)
        self.assertEqual(d.n_clusters, C.N_TARGET_CLUSTERS)
        self.assertAlmostEqual(d.rail_usd, 20.0, places=6)

    def test_the_notes_THREE_PRINTED_PAIRS_reproduce(self):
        """note 55 prints ($10 at $300), (~$20 at $600) and ($66 at $2k) — all C/30.  A build
        whose rail does not reproduce them at the measured mix has drifted from the note."""
        for cap, seat in ((300.0, 10.0), (600.0, 20.0), (2000.0, 66.67)):
            d = D.derive(cap, [("KXA", 20.0, C.RUIN_P_REF_PRICE, 0.0, 24.0)])
            self.assertAlmostEqual(d.rail_usd, seat, delta=0.01,
                                   msg="C=%s wanted $%s got $%.2f" % (cap, seat, d.rail_usd))

    def test_a_RICH_mix_may_NOT_widen_the_rail_below_the_floor(self):
        """G1, THE BLOCKING FINDING.  Without the floor a 45c funded mix gives p = 0.0617,
        n_required = 12.12, N = 13 and a rail of $46.15 — three times the note's seat, reached
        by the book having been expensive for an afternoon.  "N is a diversification FLOOR,
        not a target" (note 55), so the coupling is ONE-DIRECTIONAL."""
        rich = D.derive(600.0, [("KXA", 20.0, 0.45, 0.0, 24.0)])
        self.assertLess(rich.n_required, C.N_TARGET_CLUSTERS)
        self.assertAlmostEqual(rich.n_required, 12.1189, places=3)
        self.assertEqual(rich.n_clusters, C.N_TARGET_CLUSTERS)
        self.assertAlmostEqual(rich.rail_usd, 20.0, places=6)

    def test_the_floor_is_the_SEED_so_a_derivation_is_never_looser_than_no_derivation(self):
        seed = D.seed_dials(600.0)
        for px in (0.45, 0.90, 0.197, 0.05):
            d = D.derive(600.0, [("KXA", 20.0, px, 0.0, 24.0)])
            self.assertLessEqual(d.rail_usd, seed.rail_usd + 1e-9,
                                 "a measured mix widened the rail past the unmeasured seed "
                                 "at %sc" % (px * 100))


class TestTheCalibrationDegrade(LipTestCase):
    """note 55 §4: the second factor is "READ OFF THE BOARD (the price), degraded by the
    measured calibration gap"."""

    def test_p_against_is_one_minus_the_DEGRADED_win_rate(self):
        # 20c posted, g = 0.3508 ⇒ realised 0.1298 ⇒ p_against = 0.8702
        self.assertAlmostEqual(D.p_against(0.20), 1.0 - 0.20 * (1.0 - 0.3508), places=9)

    def test_the_degrade_is_what_makes_cheap_rungs_riskier(self):
        self.assertGreater(D.p_against(0.02), D.p_against(0.20))
        self.assertGreater(D.p_against(0.20), D.p_against(0.60))


class TestTheFloorCapCoupling(LipTestCase):
    """note 55: "floor↓ ⇒ funded-mix p↑ ⇒ N↑ ⇒ A↓ — compute the cap from the ACTUAL funded
    mix's p."  This is the whole coupling, in one assertion chain."""

    def test_a_cheaper_funded_mix_buys_a_SMALLER_rail(self):
        """The coupling, in the direction it runs: cheaper mix ⇒ higher p ⇒ higher N ⇒ smaller
        rail.  Upward from the floor only (G1) — the rich end is pinned by
        `test_a_RICH_mix_may_NOT_widen_the_rail_below_the_floor`."""
        rich = D.derive(600.0, [("KXA", 20.0, 0.40, 0.0, 24.0)])
        mid = D.derive(600.0, [("KXA", 20.0, 0.197, 0.0, 24.0)])
        cheap = D.derive(600.0, [("KXA", 20.0, 0.05, 0.0, 24.0)])
        cheapest = D.derive(600.0, [("KXA", 20.0, 0.01, 0.0, 24.0)])
        self.assertLess(rich.p, mid.p)
        self.assertLess(mid.p, cheap.p)
        self.assertLessEqual(mid.n_clusters, cheap.n_clusters)
        self.assertLess(cheap.n_clusters, cheapest.n_clusters)
        self.assertGreaterEqual(mid.rail_usd, cheap.rail_usd)
        self.assertGreater(cheap.rail_usd, cheapest.rail_usd)
        # the deploy-relevant magnitudes, so a drift in the table shows up here
        self.assertAlmostEqual(mid.rail_usd, 20.0, places=6)
        self.assertAlmostEqual(cheap.rail_usd, 600.0 / 37.0, places=6)
        self.assertAlmostEqual(cheapest.rail_usd, 600.0 / 40.0, places=6)

    def test_the_wipe_unit_is_the_CLUSTER_not_the_market(self):
        """Two markets of one cluster are ONE bet, so they pool into one p, not two."""
        one_cluster = [("KXA", 10.0, 0.05, 0.0, 24.0), ("KXA", 10.0, 0.40, 0.0, 24.0)]
        p, _pf, _pa, n, px = D.mix_p(one_cluster)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(px, 0.225, places=9)
        self.assertAlmostEqual(p, D.p_from_mix(0.225), places=9)


class TestTheEmptyMixCannotWidenTheRail(LipTestCase):
    """THE FAILURE DIRECTION THAT MATTERS.  p = 0 makes the formula return N = 0 ⇒ N = 1 ⇒
    A = C: the WHOLE STACK on one settle source, produced by having measured nothing.  An
    empty mix must keep the seed."""

    def test_an_empty_mix_holds_the_seed_rail(self):
        d = D.derive(600.0, [])
        self.assertEqual(d.n_clusters, C.N_TARGET_CLUSTERS)
        self.assertAlmostEqual(d.rail_usd, 600.0 / C.N_TARGET_CLUSTERS, places=9)
        self.assertTrue(self.logs_of("dials_no_mix"))

    def test_the_seed_is_v5s_own_derived_N(self):
        d = D.seed_dials(600.0)
        self.assertEqual(d.n_clusters, C.N_TARGET_CLUSTERS)


class TestTheFixpoint(LipTestCase):
    def _board(self, n=12, p=0.20, rho=2.0):
        return [slot("KXF%02d-26JUL31-T1" % i, rho=rho, S=200.0, p=p) for i in range(n)]

    def _alloc_fn(self):
        def f(sl, budget, rail, **kw):
            return MQ.allocate_marginal(sl, budget, cluster_cap_usd=rail,
                                        per_market_cap_usd=rail, **kw)
        return f

    def test_it_converges_and_reports_its_inputs(self):
        d = D.derive_from_slots(600.0, self._board(), self._alloc_fn())
        self.assertTrue(d.feasible)
        self.assertGreater(d.n_clusters, 1)
        self.assertGreater(d.rail_usd, 0.0)
        it = self.logs_of("dials_iterate")
        self.assertTrue(it, "the fixpoint must show its trajectory")
        self.assertIn("p_fill_implied", it[0],
                      "the assumption behind p must be readable off the log")

    def test_a_rail_that_starves_the_board_is_HELD_not_widened(self):
        """The oscillation door: the tighter rail funds nothing, the empty mix reads p = 0,
        and a naive fixpoint would widen the rail straight back.  It must hold instead."""
        expensive = [slot("KXBIG-26JUL31-T1", rho=0.5, S=5000.0, p=0.20)]
        d = D.derive_from_slots(60.0, expensive, self._alloc_fn())
        self.assertLessEqual(d.rail_usd, 60.0 / C.N_TARGET_CLUSTERS + 1e-9)

    def test_the_emergent_floor_is_REPORTED(self):
        """note 54 step 3's floor dial is the queue's lambda, so the floor is READ, not set."""
        board = [slot("KXA-26JUL31-T1", rho=2.0, S=200.0, p=0.20),
                 slot("KXB-26JUL31-T1", rho=2.0, S=200.0, p=0.06)]
        D.derive_from_slots(600.0, board, self._alloc_fn())
        it = self.logs_of("dials_iterate")
        self.assertTrue(it and it[-1]["floor_c"] is not None)
        self.assertLessEqual(it[-1]["floor_c"], 20)


class TestTheAlwaysFilledInstrument(LipTestCase):
    """note 55 §4 reading B, kept as an INSTRUMENT: it answers "don't play", which is its
    honest content at our own funded prices, and that is exactly why it is not the default."""

    def test_reading_B_refuses_to_play_at_the_funded_mix(self):
        old = C.RUIN_ALWAYS_FILLED
        try:
            C.RUIN_ALWAYS_FILLED = True
            d = D.derive(600.0, [("KXA", 20.0, C.RUIN_P_REF_PRICE, 0.0, 24.0)])
            self.assertFalse(d.feasible)
            self.assertEqual(d.rail_usd, 0.0)
            self.assertGreater(d.p, C.RUIN_D)
            self.assertTrue(any(a[0] == "dials_dont_play" for a in self.alerts),
                            "refusing to play must PAGE: %s" % self.alerts)
        finally:
            C.RUIN_ALWAYS_FILLED = old

    def test_the_default_reading_plays(self):
        self.assertFalse(C.RUIN_ALWAYS_FILLED)
        self.assertTrue(D.derive(600.0,
                                 [("KXA", 20.0, C.RUIN_P_REF_PRICE, 0.0, 24.0)]).feasible)


class TestTheTapeSocket(LipTestCase):
    """note 54 step 1: "Re-measure p from cluster-days tape ... before scaling"."""

    def test_a_thin_tape_cannot_move_the_prior(self):
        # 2 wipes in 10 cluster-days is 20%, but 10 days against an 819-day prior moves p by
        # under a point.
        p = D.p_from_tape(2, 10)
        self.assertLess(abs(p - C.RUIN_P_BASE), 0.005)

    def test_a_thick_tape_does_move_it(self):
        p = D.p_from_tape(400, 2000)
        self.assertGreater(p, 0.14)

    def test_the_priors_strength_is_derived_from_its_stated_precision(self):
        # "8-10%" is +-1pp around 0.09 ⇒ n = p(1-p)/se^2
        self.assertAlmostEqual(C.RUIN_P_PRIOR_DAYS,
                               round(0.09 * 0.91 / (0.01 ** 2)), delta=1.0)
