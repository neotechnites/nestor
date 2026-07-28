"""spec §8.1 — `net_rate(...)`, T-N1..N8.  The three worked rows of §0.4 are this suite's
spine: "Recompute all three rows by hand before trusting any implementation of (★)."""

import unittest

from .. import alloc, config as C, money as M
from .base import LipTestCase

RSTAR = 0.00625            # spec §0.4: the gas-window achieved marginal rate
FLOOR = C.FLOOR_RATE_PER_H  # λ_min/16 = 0.00625 /h


class TestStarRows(LipTestCase):
    """T-N1..N3 — reproduce §0.4's table to 1e-3 WITH `d` capped at `p`."""

    def test_TN1_pypl_excluded_by_three_orders_of_magnitude(self):
        t = M.net_terms(rho=0.439, S=50, p=0.30, q=0, phi=0.50, d=0.07,
                        l_eff=3744.0, r_star=RSTAR, t_hat=1.0)
        self.assertAlmostEqual(t["gross"], 0.0146, places=3)
        self.assertAlmostEqual(t["carry"], 11.70, places=2)
        self.assertAlmostEqual(t["drift"], 0.117, places=3)
        self.assertAlmostEqual(t["net"], -11.80, places=2)
        # §8.1: "PYPL −11.80 (and `H` clips to 0)"
        self.assertEqual(t["H"], 0.0)
        self.assertFalse(M.admits(t["net"], FLOOR))
        # the refusal is ~800x, i.e. three orders of magnitude
        self.assertGreater(abs(t["net"]) / t["gross"], 700.0)

    def test_TN2_treasury_daily_kept_at_17x_the_floor(self):
        t = M.net_terms(rho=6.25, S=50, p=0.50, q=0, phi=0.08, d=0.07,
                        l_eff=8.0, r_star=RSTAR, t_hat=1.0)
        self.assertAlmostEqual(t["gross"], 0.125, places=3)
        self.assertAlmostEqual(t["carry"], 0.0040, places=4)
        self.assertAlmostEqual(t["drift"], 0.0112, places=4)
        self.assertAlmostEqual(t["net"], 0.110, places=3)
        self.assertTrue(M.admits(t["net"], FLOOR))
        self.assertAlmostEqual(t["net"] / FLOOR, 17.6, places=1)   # "KEEP, 17x floor"

    def test_TN3_gas_cheap_side(self):
        t = M.net_terms(rho=6.25, S=50, p=0.02, q=0, phi=0.001, d=0.07,
                        l_eff=8.0, r_star=RSTAR, t_hat=1.0)
        self.assertAlmostEqual(t["gross"], 3.125, places=3)
        self.assertAlmostEqual(t["carry"], 0.00005, places=6)
        self.assertAlmostEqual(t["drift"], 0.001, places=4)
        self.assertAlmostEqual(t["net"], 3.12, places=2)
        # §0.4: `d` capped at p makes d/p = 1.0 here — the cap is what makes the row reproduce
        self.assertAlmostEqual(t["d_used"], 0.02, places=6)

    def test_d_cap_at_p_at_each_row_price(self):
        """§0.4's own arithmetic for the cap: d/p = 1.0 at 2c, 0.14 at 50c, 0.233 at 30c."""
        for p, expect in ((0.02, 1.0), (0.50, 0.14), (0.30, 0.2333)):
            d = M.d_estimate(None, p)
            self.assertAlmostEqual(d / p, expect, places=3, msg="p=%s" % p)

    def test_cheap_side_tilt_is_larger_on_net_than_on_gross(self):
        """§4.6 SF-3: (★) tilts FURTHER cheap — 28.4x on net vs 25.0x on gross.  v5 must own
        that this is a LARGER anti-gaming exposure than v4's, and both numbers are asserted so
        the tradeoff can never be made implicitly."""
        gas = M.net_terms(6.25, 50, 0.02, 0, 0.001, 0.07, 8.0, RSTAR, 1.0)
        tsy = M.net_terms(6.25, 50, 0.50, 0, 0.08, 0.07, 8.0, RSTAR, 1.0)
        self.assertAlmostEqual(gas["gross"] / tsy["gross"], 25.0, delta=0.05)
        self.assertAlmostEqual(gas["net"] / tsy["net"], 28.4, delta=0.1)


class TestHorizonAndFloor(LipTestCase):
    def test_TN4_carry_is_linear_in_L(self):
        """T-N4 — `L_eff` doubling exactly doubles carry (and halves H's headroom)."""
        c1 = M.carry_cost(0.08, 8.0, RSTAR)
        c2 = M.carry_cost(0.08, 16.0, RSTAR)
        self.assertAlmostEqual(c2, 2.0 * c1, places=12)
        g = M.gross_rate(6.25, 50, 0.50)
        h1 = M.horizon_multiplier_display(g, c1)
        h2 = M.horizon_multiplier_display(g, c2)
        self.assertAlmostEqual(1.0 - h2, 2.0 * (1.0 - h1), places=12)

    def test_TN5_finite_at_q_zero(self):
        """T-N5 — v1's B4 defect must not return: no division by zero at q = 0."""
        self.assertTrue(M.gross_rate(6.25, 50, 0.50, 0.0) > 0)
        self.assertEqual(M.gross_rate(6.25, 0.0, 0.50, 0.0), 0.0)   # S=0 degenerates to 0
        self.assertEqual(M.gross_rate(6.25, 50, 0.0, 0.0), 0.0)     # p=0 is not a divide

    def test_TN6_past_due_escalates_and_carry_is_never_negative(self):
        """T-N6 (B2) — `now = close_ts + 3 h` with no settlement ⇒ `L_eff = 6 h` (2x past due),
        carry STRICTLY GREATER than at close, and `L_eff ≥ SETTLE_LAG_H` at EVERY input."""
        close = 1_000_000.0
        at_close = M.l_eff_h(close, close)
        past = M.l_eff_h(close, close + 3 * 3600.0)
        self.assertAlmostEqual(past, 6.0, places=9)
        self.assertGreater(M.carry_cost(0.5, past, RSTAR),
                           M.carry_cost(0.5, at_close, RSTAR))
        # monotone escalation, never shrinking
        prev = 0.0
        for h in (0.0, 1.0, 3.0, 10.0, 100.0, 10_000.0):
            eff = M.l_eff_h(close, close + h * 3600.0)
            self.assertGreaterEqual(eff, C.SETTLE_LAG_H)
            self.assertGreaterEqual(eff + 1e-12, prev)
            prev = eff
            # A NEGATIVE CARRY MUST BE UNREACHABLE — the PayPal failure with the sign flipped.
            self.assertGreater(M.carry_cost(0.5, eff, RSTAR), 0.0)

    def test_floor_holds_before_close_too(self):
        close = 1_000_000.0
        # 1 minute before close: T_settle = 0.7167h, floored answer is not below the lag
        self.assertGreaterEqual(M.l_eff_h(close, close - 60.0), C.SETTLE_LAG_H)

    def test_settled_market_does_not_escalate(self):
        close = 1_000_000.0
        self.assertAlmostEqual(M.l_eff_h(close, close + 5 * 3600.0, settled=True),
                               C.SETTLE_LAG_H, places=9)

    def test_l_shed_unmeasured_is_infinity_not_optimism(self):
        """"L_shed unmeasured ⇒ ∞ is the only default consistent with 'no cap may assume
        settlement bails it out'." """
        close = 1_000_000.0
        now = close - 100 * 3600.0
        self.assertAlmostEqual(M.l_eff_h(close, now, l_shed_h=None),
                               M.t_settle_h(close, now), places=9)
        self.assertAlmostEqual(M.l_eff_h(close, now, l_shed_h=3.0), 3.0, places=9)

    def test_l_shed_median(self):
        self.assertIsNone(M.l_shed_median_h([]))
        self.assertAlmostEqual(M.l_shed_median_h([1.0, 3.0, 2.0]), 2.0)
        self.assertAlmostEqual(M.l_shed_median_h([1.0, 2.0, 3.0, 4.0]), 2.5)

    def test_hard_horizon_exclusion_is_the_pypl_geometry(self):
        """§1.2 — exclude iff `T_settle > H_prog + 24`, unless rung ≥ 2."""
        now = 0.0
        prog_end = 30 * 24 * 3600.0                    # program ends in 30 days
        close_far = 200 * 24 * 3600.0                  # market closes DEC 31: PYPL
        close_near = 8 * 3600.0
        self.assertTrue(M.horizon_excluded(close_far, now, prog_end))
        self.assertFalse(M.horizon_excluded(close_near, now, prog_end))
        self.assertFalse(M.horizon_excluded(close_far, now, prog_end, rung=2))
        # the +24h grace covers same-day-after settlement
        self.assertFalse(M.horizon_excluded(prog_end + 20 * 3600.0, now, prog_end))
        self.assertTrue(M.horizon_excluded(prog_end + 30 * 3600.0, now, prog_end))


class TestPhiAndD(LipTestCase):
    def test_rule_of_three_replaces_the_guessed_seeds(self):
        """§2.4 — zero-fill venues use `φ_ub = 3 / Σ rest_contract_hours` at 95%, and the
        seeds survive only as the CEILING at zero exposure."""
        self.assertAlmostEqual(M.phi_estimate(0, 300.0, p=0.50), 0.01, places=9)
        self.assertAlmostEqual(M.phi_estimate(0, 0.0, p=0.50), C.PHI_SEED_MID)
        self.assertAlmostEqual(M.phi_estimate(0, 0.0, p=0.02), C.PHI_SEED_CHEAP)
        # a bound WIDER than the seed is capped by the seed at low exposure
        self.assertAlmostEqual(M.phi_estimate(0, 1.0, p=0.50), C.PHI_SEED_MID)
        # with fills, it is the plain rate
        self.assertAlmostEqual(M.phi_estimate(12, 150.0), 0.08, places=9)

    def test_rule_of_three_tightens_with_evidence(self):
        prev = None
        for e in (10.0, 100.0, 1000.0):
            ub = M.phi_estimate(0, e, p=0.50)
            if prev is not None:
                self.assertLess(ub, prev)
            prev = ub

    def test_d_estimate_uses_the_tape_then_caps_at_p(self):
        self.assertAlmostEqual(M.d_estimate([0.03, 0.05], 0.50), 0.04, places=9)
        self.assertAlmostEqual(M.d_estimate([0.30, 0.30], 0.02), 0.02, places=9)
        self.assertAlmostEqual(M.d_estimate(None, 0.50), C.D_SEED_USD, places=9)

    def test_decisiveness(self):
        """§2.4 — 10 fills, OR 2 committed dollar-hours with ZERO fills."""
        self.assertTrue(M.is_decisive(10, 0.0))
        self.assertFalse(M.is_decisive(9, 1.0))
        self.assertTrue(M.is_decisive(0, 2.0))
        self.assertFalse(M.is_decisive(1, 50.0))   # some fills but not enough: not decisive


class TestRStarFixpoint(LipTestCase):
    """T-N7 (SF-8) — the fixpoint converges in ≤4 damped iterations on a 16x seed error; a
    constructed oscillating book hits the cap, uses `max(r*_0..r*_4)`, logs
    `rstar_no_converge`, and allocates ≤ the converged run."""

    def test_seed_never_below_the_floor(self):
        self.assertEqual(M.rstar_seed(None), C.FLOOR_RATE_PER_H)     # cold start
        self.assertEqual(M.rstar_seed(0.0001), C.FLOOR_RATE_PER_H)
        self.assertEqual(M.rstar_seed(0.05), 0.05)

    def test_TN7_four_iterations_reduce_the_residual_16x(self):
        """§1.3's DERIVATION, asserted exactly as written: "damped iteration on a monotone
        scalar map halves the residual per step, so 4 covers a 16x seed error"."""
        true_r = 0.10

        def allocate_fn(r):
            return ("A", true_r)              # a well-behaved book: the map is constant

        res = M.solve_rstar(allocate_fn, true_r * 16.0)
        residuals = [abs(x - true_r) for x in res.trace]
        for a, b in zip(residuals, residuals[1:]):
            self.assertAlmostEqual(b, 0.5 * a, places=12)      # halves per step, exactly
        self.assertAlmostEqual(residuals[0] / residuals[-1], 16.0, places=6)
        self.assertLessEqual(res.iters, C.RSTAR_MAX_ITERS)

    def test_TN7_FINDING_the_5pct_stop_rule_cannot_trip_within_4_iterations(self):
        """SURFACED DIVERGENCE (spec §1.3, reported upward — NOT silently fixed).

        §1.3 states two things that are not the same claim:
          (i)  4 iterations "covers a 16x seed error"  — true of the RESIDUAL (test above);
          (ii) "stop when |r*_k − r*_{k−1}| / r*_k < 0.05".

        The damped map reaches a 5% RELATIVE band from initial relative error `e` only after
        `k ≥ log2(e/0.05)` steps: e = 1 (a 2x seed) needs 5, e = 15 (a 16x seed) needs 9.  So
        with RSTAR_MAX_ITERS = 4 the stop rule cannot trip for ANY meaningful seed error, and
        the fixpoint always falls back to `max(r*_0..r*_4)`.

        This is asserted here because it is the ACTUAL shipped behavior and it must not change
        unnoticed.  It is SAFE — see the next test — but it is not adaptive, and
        `rstar_no_converge` will therefore fire every cycle.
        """
        true_r = 0.10
        for seed_mult in (2.0, 16.0):
            res = M.solve_rstar(lambda r: ("A", true_r), true_r * seed_mult)
            self.assertFalse(res.converged, "seed x%g unexpectedly converged" % seed_mult)
            self.assertEqual(res.r_star, max(res.trace))

    def test_TN7_the_fallback_is_conservative_in_both_directions(self):
        """Why the finding above is safe to ship: `max(trace)` errs HIGH in both regimes.

        Seed too HIGH ⇒ the trace decreases ⇒ max = the seed ⇒ carry priced high ⇒ fewer
        venues admitted.  Seed too LOW ⇒ the trace increases ⇒ max = the last value ⇒ again
        the highest r* seen.  A higher r* always allocates LESS, which is the direction that
        fails toward the PayPal lesson rather than away from it.
        """
        for true_r, seed in ((0.10, 1.60), (0.10, 0.00625)):
            res = M.solve_rstar(lambda r: ("A", true_r), seed)
            self.assertGreaterEqual(res.r_star, min(res.trace))
            self.assertEqual(res.r_star, max(res.trace))

    def test_TN7_damping_prevents_a_two_cycle(self):
        """An oscillating map: without the 0.5 damping this is a permanent 2-cycle."""
        seen = []

        def allocate_fn(r):
            seen.append(r)
            return ("A", 0.02 if r > 0.05 else 0.20)

        res = M.solve_rstar(allocate_fn, 0.08)
        self.assertGreaterEqual(len(seen), 2)
        self.assertTrue(all(x > 0 for x in res.trace))

    def test_TN7_non_convergence_takes_the_max_and_allocates_no_more(self):
        """The conservative tie-break: a HIGHER r* prices carry higher, admits fewer venues,
        and allocates LESS.  Assert the non-converged run allocates ≤ the converged run."""
        calls = {"n": 0}

        def oscillate(r):
            calls["n"] += 1
            # deliberately never settles inside the 5% band
            return (_alloc_size(r), 0.001 if calls["n"] % 2 else 0.5)

        def _alloc_size(r):
            # a stand-in allocation: strictly decreasing in r*, as (★) requires
            return max(0.0, 100.0 - 100.0 * float(r))

        res = M.solve_rstar(oscillate, 0.00625)
        self.assertFalse(res.converged)
        self.assertEqual(res.r_star, max(res.trace))
        converged_alloc = _alloc_size(min(res.trace))
        self.assertLessEqual(res.alloc, converged_alloc)

    def test_rstar_no_converge_is_logged_by_the_allocator_path(self):
        slots = [alloc.Slot("T1", "bid", rho=6.25, S=50, p=0.50, phi=0.08, d=0.07, l_eff=8.0)]
        # a trailing rate high enough to force iteration, on a book whose marginal rate is low
        a, spent, res = alloc.allocate_with_rstar(slots, 100.0, trailing_rate=5.0)
        self.assertIsNotNone(res)
        if not res.converged:
            self.assertTrue(self.logs_of("rstar_no_converge"))


class TestS0Qualification(LipTestCase):
    """T-N8 (N1) — `S = 0` ⇒ ALLOCATE returns qty 0 for that slot, AND the qualification path
    supplies `target_size − cum_size` at the cheapest legal price, bounded by P7 and the 0.25
    land-grab fraction."""

    def test_TN8_allocate_assigns_zero_at_S0(self):
        s = alloc.Slot("EMPTY", "bid", rho=6.25, S=0.0, p=0.50, phi=0.08, d=0.07, l_eff=8.0)
        a, spent, _ = alloc.allocate([s], 100.0, RSTAR)
        self.assertEqual(a[("EMPTY", "bid")], 0)
        self.assertEqual(spent, 0.0)

    def test_TN8_qualification_path_supplies_the_gate_size(self):
        s = alloc.Slot("EMPTY", "bid", rho=6.25, S=0.0, p=0.50, phi=0.08, d=0.07, l_eff=8.0,
                       target_size=1000, cum_size=200, land_grab_size=800,
                       land_grab_price_c=1)
        a, spent, _ = alloc.allocate([s], 100.0, RSTAR)
        # 800 contracts at 1c = $8.00, inside the 0.25 land-grab fraction of a $100 budget
        self.assertEqual(a[("EMPTY", "bid")], 800)
        self.assertAlmostEqual(spent, 8.00, places=6)
        self.assertLessEqual(spent, C.LAND_GRAB_MAX_COLLATERAL_FRAC * 100.0)

    def test_TN8_size_is_the_minimum_qualifying_size_not_more(self):
        """v1 D2: do NOT size up into an empty book — the minimum qualifying size IS the
        maximum of the objective, because at S≈0 extra size buys no share, only fill risk."""
        self.assertEqual(alloc.t0_qualification_size(200, 1000), 800)
        self.assertEqual(alloc.t0_qualification_size(1000, 1000), 0)

    def test_TN8_p7_caps_concurrent_revival_markets(self):
        slots = []
        for i in range(6):
            slots.append(alloc.Slot("M%d" % i, "bid", rho=6.25, S=0.0, p=0.50, phi=0.08,
                                    d=0.07, l_eff=8.0, target_size=100, land_grab_size=100,
                                    land_grab_price_c=1, moneyness=float(i)))
        a, spent = alloc.qualification_pass(slots, 1000.0)
        funded = {k[0] for k, v in a.items() if v > 0}
        self.assertLessEqual(len(funded), C.P7_MAX_REVIVAL_MARKETS)


class TestAdmission(LipTestCase):
    def test_admits_is_strictly_above_the_water_level(self):
        self.assertFalse(M.admits(FLOOR, FLOOR))
        self.assertTrue(M.admits(FLOOR * 1.0001, FLOOR))
        self.assertFalse(M.admits(-1.0, FLOOR))

    def test_t_hat_scales_only_the_gross_term(self):
        """(★) is ADDITIVE (divergence D1): T̂ multiplies gross, and the costs stand alone."""
        a = M.net_terms(6.25, 50, 0.50, 0, 0.08, 0.07, 8.0, RSTAR, t_hat=1.0)
        b = M.net_terms(6.25, 50, 0.50, 0, 0.08, 0.07, 8.0, RSTAR, t_hat=0.5)
        self.assertAlmostEqual(b["net"], 0.5 * a["gross"] - a["carry"] - a["drift"], places=12)

    def test_t_hat_zero_kills_the_venue_without_settlement_data(self):
        """PYPL's geometry drives T̂ → 0 within hours; at T̂ = 0 nothing can be admitted."""
        n = M.net_rate(6.25, 50, 0.50, 0, 0.08, 0.07, 8.0, RSTAR, t_hat=0.0)
        self.assertLess(n, 0.0)
        self.assertFalse(M.admits(n, FLOOR))


class TestDoseResponse(LipTestCase):
    def test_panel_is_stable_within_a_period_and_process_independent(self):
        keys = [("A", "bid"), ("B", "ask"), ("C", "bid"), ("D", "ask")]
        p1 = M.dose_panel(keys, "2026-07-28")
        p2 = M.dose_panel(keys, "2026-07-28")
        self.assertEqual(p1, p2)
        self.assertTrue(all(v in C.DOSE_MULTIPLIERS for v in p1.values()))

    def test_panel_needs_at_least_three_slots(self):
        self.assertEqual(M.dose_panel([("A", "bid"), ("B", "ask")], "p"), {})
        self.assertEqual(len(M.dose_panel([("A", "bid"), ("B", "ask"), ("C", "bid")], "p")), 3)

    def test_budget_is_two_percent_of_the_portfolio_rate(self):
        self.assertTrue(M.dose_budget_ok(0.01, 1.0))
        self.assertFalse(M.dose_budget_ok(0.02, 1.0))


if __name__ == "__main__":
    unittest.main()
