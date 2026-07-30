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
        """§1.2 — exclude iff `T_settle > H_prog + grace`, unless rung ≥ 2.  The grace is
        now SETTLE_HORIZON_H (note 52 D4): the settlement gate at slot-build is the entry
        guard, and this exclusion survives as the backstop BEHIND it at the same boundary —
        so a market settling a week past its program is excluded, one settling a day past
        it is priced by (★)'s carry term instead of amputated."""
        now = 0.0
        prog_end = 30 * 24 * 3600.0                    # program ends in 30 days
        close_far = 200 * 24 * 3600.0                  # market closes DEC 31: PYPL
        close_near = 8 * 3600.0
        self.assertTrue(M.horizon_excluded(close_far, now, prog_end))
        self.assertFalse(M.horizon_excluded(close_near, now, prog_end))
        self.assertFalse(M.horizon_excluded(close_far, now, prog_end, rung=2))
        # inside the grace: settling a day after the program is a carry question, not a ban
        self.assertFalse(M.horizon_excluded(prog_end + 30 * 3600.0, now, prog_end))
        # past it: the PYPL geometry, excluded
        self.assertTrue(M.horizon_excluded(prog_end + C.SETTLE_HORIZON_H * 3600.0
                                           + 30 * 3600.0, now, prog_end))


class TestPhiAndD(LipTestCase):
    def test_rule_of_three_replaces_the_guessed_seeds(self):
        """§2.4 — zero-fill venues use `φ_ub = 3 / Σ rest_contract_hours` at 95%, and the
        seeds survive only as the CEILING at zero exposure."""
        # THE SEED IS THE CEILING AT ALL EXPOSURES, not only at zero (spec §2.4), so with
        # ONE seed a zero-fill venue sits at PHI_SEED_CHEAP until 3/E drops below it — i.e.
        # "assume a resting order is rarely eaten until one actually is".  That is the
        # bootstrap escape: without the cap a venue at E = 1 would read phi = 3.0 and be
        # refused, so it could never accumulate the exposure that would lower it.
        self.assertAlmostEqual(M.phi_estimate(0, 300.0, p=0.50), C.PHI_SEED_CHEAP, places=9)
        self.assertAlmostEqual(M.phi_estimate(0, 6000.0, p=0.50), 0.0005, places=9)
        # ONE SEED, NO PRICE STEP (2026-07-29).  The step was a bootstrap deadlock: at 12c
        # the 0.08 seed refused the venue AND capped phi_ub, so E never left 0 and the
        # Rule of Three — the actual estimator — could never run.  See money.seed_phi.
        self.assertAlmostEqual(M.phi_estimate(0, 0.0, p=0.50), C.PHI_SEED_CHEAP)
        self.assertAlmostEqual(M.phi_estimate(0, 0.0, p=0.02), C.PHI_SEED_CHEAP)
        self.assertAlmostEqual(M.seed_phi(0.50), M.seed_phi(0.02),
                               "the seed must not step on price")
        # a bound WIDER than the seed is still capped by the seed at low exposure
        self.assertAlmostEqual(M.phi_estimate(0, 1.0, p=0.50), C.PHI_SEED_CHEAP)
        # with fills, it is the plain rate
        self.assertAlmostEqual(M.phi_estimate(12, 150.0), 0.08, places=9)

    def test_rule_of_three_tightens_with_evidence(self):
        prev = None
        for e in (6e3, 6e4, 6e5):          # past the seed cap, where 3/E is the binding bound
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
        # §1.3's claim, isolated to the four steps it was made about
        self.assertAlmostEqual(residuals[0] / residuals[4], 16.0, places=6)
        self.assertLessEqual(res.iters, C.RSTAR_MAX_ITERS)

    def test_TN7_nine_iterations_DO_converge_at_the_5pct_rule(self):
        """D3, RESOLVED: `RSTAR_MAX_ITERS = 9` is the smallest value making §1.3's two
        statements true at once — it covers the 16x seed error §1.3 names, at the 5% tolerance
        §1.3 sets.  `k ≥ log2(e/0.05)`: 5 steps for a 2x seed, 9 for a 16x one."""
        true_r = 0.10
        self.assertEqual(C.RSTAR_MAX_ITERS, 9)
        for seed_mult, expect_iters in ((2.0, 5), (16.0, 9)):
            res = M.solve_rstar(lambda r: ("A", true_r), true_r * seed_mult)
            self.assertTrue(res.converged, "seed x%g did not converge" % seed_mult)
            self.assertEqual(res.iters, expect_iters, "seed x%g" % seed_mult)
            self.assertLess(abs(res.r_star - true_r) / true_r, 0.05)

    def test_TN7_at_four_iterations_it_could_never_have_converged(self):
        """The defect itself, pinned: at the spec's original 4 the stop rule cannot trip for
        any meaningful seed error, so `rstar_no_converge` fired every cycle."""
        true_r = 0.10
        for seed_mult in (2.0, 16.0):
            res = M.solve_rstar(lambda r: ("A", true_r), true_r * seed_mult, max_iters=4)
            self.assertFalse(res.converged)

    def test_TN7_the_fallback_errs_high_ONLY_when_the_seed_was_high(self):
        """CORRECTED (the reviewer verified this): `max(trace)` does NOT err high in both
        regimes.  It errs high relative to the SEED, which is not the same as relative to the
        TRUTH — and the low-seed case lands BELOW the true fixed point, pricing carry too
        cheaply, which is the PayPal direction.  §1.4's unverified-exposure cap, not this
        tie-break, is the cold-start guard."""
        true_r = 0.10
        high = M.solve_rstar(lambda r: ("A", true_r), 1.60, max_iters=4)
        self.assertEqual(high.r_star, max(high.trace))
        self.assertGreater(high.r_star, true_r)             # above the truth: conservative

        low = M.solve_rstar(lambda r: ("A", true_r), C.FLOOR_RATE_PER_H, max_iters=4)
        self.assertEqual(low.r_star, max(low.trace))
        self.assertLess(low.r_star, true_r)                 # BELOW the truth: NOT conservative

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
        a, spent, _ = alloc.allocate([s], 100.0, RSTAR,
                                     caps=alloc.Caps(inv_cap_usd=10.0))
        # explicit $10 container: the live lot container ($2.50) cannot hold an 800-lot
        # grab, and this test is about the QUALIFICATION mechanism, not the live constant
        # (under FREE_RIDE_ONLY the grab path is dead in production anyway).
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
