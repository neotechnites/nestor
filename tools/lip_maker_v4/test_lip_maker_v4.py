#!/usr/bin/env python3
"""
§14 TEST PLAN — the money rules as pure functions.  NO NETWORK IN ANY TEST.

    python3 -m unittest discover tools/lip_maker_v4/

Test ids map 1:1 onto spec §14 (T1..T35).  Where a spec test's LITERAL expectation
contradicts the spec's own mathematics, the test asserts the DERIVED truth and the
divergence is stated in the test's docstring and in the implementor's report.  Three such
places exist: T1 (§14.1), T26 (§1.3's "10 of 17"), T28 (lost provenance).
"""

import json
import math
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NTFY_DISABLE", "1")   # never page a human from the unit suite

import lip_maker_v4 as M


BIG = M.Caps(inv_cap_usd=1e9, per_market_pool_mult=1e9, per_market_budget_frac=1e9)
T = "KXAAAGASD-26JUL28-4.105"


def slot(ticker, side="bid", rho=6.25, S=50.0, p=0.40, **kw):
    return M.Slot(ticker, side, rho, S, p, **kw)


def unit_progs(n=40, reward=1_000_000.0, series="KXOTHER", gas=0, gas_reward=1_000_000.0):
    """Live-program fixtures for the §0.3 unit assertion."""
    out = [{"series": series, "market_ticker": "%s-%03d" % (series, i),
            "period_reward": reward} for i in range(n)]
    out += [{"series": "KXAAAGASD", "market_ticker": "KXAAAGASD-4.%03d" % i,
             "period_reward": gas_reward} for i in range(gas)]
    return out


# =============================================================================================
# §14.1  ALLOCATE
# =============================================================================================
class T14_1_Allocate(unittest.TestCase):

    def test_T1_cheap_side_first_is_marginal_water_filling(self):
        """T1 (§14.1) — DIVERGENCE, SURFACED.

        The spec's literal expectation is "all to B, B qty ~1000, A qty 0".  That is the
        CHARTER's cheapest-first rule, which §2.1/D3 explicitly generalises to marginal-rate
        ordering, and under the spec's own §0.4 marginal rate it is false: B's marginal rate
        decays as 1/(q+S)^2, crosses A's q=0 rate at q~242, and A clears its hurdle
        (0.0082/h) comfortably at 0.0919/h.  Water-filling to equal marginal rates yields
        3.60 $/h of reward against 2.98 $/h for the all-to-B answer — 21% MORE reward on the
        same $20.  What survives literally is the thing T1 exists to prove: the 34x cheap-side
        rate ratio at q=0, and B being funded first and far deeper than A.
        """
        A = slot("A-MKT", "bid", 6.25, 50.0, 0.68)
        B = slot("B-MKT", "bid", 6.25, 50.0, 0.02)
        # the cheap-side law itself, exactly: 0.68/0.02 = 34x
        rA = M.marginal_rate(6.25, 50.0, 0, 0.68)
        rB = M.marginal_rate(6.25, 50.0, 0, 0.02)
        self.assertAlmostEqual(rB / rA, 34.0, places=9)

        # B takes 100% of any budget below its crossover with A's q=0 rate.  That crossover
        # is at q_B ~ 242, i.e. $4.84 of collateral — the number the literal T1 needed to be
        # ~$20 for "all to B" to hold, and is not.
        q_cross = math.sqrt(6.25 * 50.0 / (2 * 0.02 * rA)) - 50.0
        self.assertAlmostEqual(q_cross, 241.6, delta=1.0)
        al, _ = M.allocate([A, B], 4.80, BIG)
        self.assertEqual(al[A.key], 0)
        self.assertEqual(al[B.key], 240)

        # the full $20 case: water-filled, B far deeper in CONTRACTS, marginals equalised
        al, spent = M.allocate([A, B], 20.0, BIG)
        self.assertGreater(al[B.key], 10 * al[A.key])
        self.assertLessEqual(spent, 20.0 + 1e-9)
        mA = M.marginal_rate(6.25, 50.0, al[A.key], 0.68)
        mB = M.marginal_rate(6.25, 50.0, al[B.key], 0.02)
        self.assertLess(abs(mA - mB) / max(mA, mB), 0.25)          # within one step
        # and it beats the spec's literal answer on the objective it optimises
        wf = (M.reward_rate(6.25, al[A.key], 50.0) + M.reward_rate(6.25, al[B.key], 50.0))
        literal = M.reward_rate(6.25, 1000, 50.0)
        self.assertGreater(wf, literal)

    def test_T2_thin_S_beats_equal_price_and_stops_at_equal_marginal(self):
        """T2 — at equal p the thinner-S slot must win (§2.1/D3).  The seeded fill-cost
        hurdle is disabled (phi=0) because at phi=0.08 the S=1000 slot's q=0 rate (0.0078/h)
        never clears the 0.014/h hurdle at all — itself a sanity check on §2.2."""
        A = slot("A-MKT", "bid", 6.25, 1000.0, 0.40, phi=0.0, d=0.0)
        B = slot("B-MKT", "bid", 6.25, 50.0, 0.40, phi=0.0, d=0.0)
        # B first, until crossover
        al, _ = M.allocate([A, B], 60.0, BIG, lambda_min=0.0)
        self.assertEqual(al[A.key], 0)
        self.assertGreater(al[B.key], 0)
        # at the stopping point the equal-marginal invariant holds (p and rho are equal, so
        # it reduces to S/(q+S)^2 being equal on both slots)
        al, spent = M.allocate([A, B], 2000.0, BIG, lambda_min=0.0)
        qa, qb = al[A.key], al[B.key]
        self.assertGreater(qa, 0)
        self.assertGreater(qb, 0)
        ia = 1000.0 / (qa + 1000.0) ** 2
        ib = 50.0 / (qb + 50.0) ** 2
        self.assertLess(abs(ia - ib) / max(ia, ib), 0.15)          # one 100-contract step

    def test_T3_wall_skip(self):
        """T3 (§2.7) — rho=6.25, p=0.40, r*=0.00625 => skip if W+S > 1250.  q* < 0 means do
        not quote that slot AT ALL, and the budget flows elsewhere."""
        self.assertAlmostEqual(6.25 / (2 * 0.40 * 0.00625), 1250.0, places=6)
        self.assertLess(M.wall_indifference_size(6.25, 2000.0, 0.0, 0.40, 0.00625), 0.0)
        self.assertGreater(M.wall_indifference_size(6.25, 1000.0, 0.0, 0.40, 0.00625), 0.0)
        W = slot("W-MKT", "bid", 6.25, 0.0, 0.40, W=2000.0)
        C = slot("C-MKT", "bid", 6.25, 50.0, 0.02)
        al, spent = M.allocate([W, C], 20.0, BIG, r_star_wall=0.00625)
        self.assertEqual(al[W.key], 0)
        self.assertGreater(al[C.key], 0)
        self.assertAlmostEqual(spent, 20.0, places=6)              # budget flowed elsewhere

    def test_T4_hurdle_is_marginal_and_finite_at_q_zero(self):
        """T4 (B4) — the whole point of §2.2.  The old AVERAGE form
        fillcost_rate/(q*p) divides by zero at q=0 and returns an empty allocation."""
        # (a) computable at alloc = 0, size-independent
        h = M.hurdle(0.08, 0.07, 0.40)
        self.assertAlmostEqual(h, 0.014, places=9)
        for q in (0, 1, 10, 1000):
            self.assertAlmostEqual(M.hurdle(0.08, 0.07, 0.40), h, places=12)
        # the cheap-side law arriving a third time: 14x lower hurdle at p=$0.02
        self.assertAlmostEqual(M.hurdle(M.seed_phi(0.02), M.seed_drift(0.02), 0.02),
                               0.001, places=9)
        self.assertAlmostEqual(h / M.hurdle(M.seed_phi(0.02), M.seed_drift(0.02), 0.02),
                               14.0, places=6)
        # (b) nonzero qty, stopping where rho*S/(2p(q+S)^2) == hurdle
        s = slot("X-MKT", "bid", 6.25, 50.0, 0.40, phi=0.08, d=0.07)
        al, _ = M.allocate([s], 1000.0, BIG)
        q = al[s.key]
        self.assertGreater(q, 0)                                   # NOT an empty allocation
        step = max(1, int(round(M.STEP_FRACTION * 1000.0 / 0.40)))
        self.assertLess(M.marginal_rate(6.25, 50.0, q, 0.40), h)
        self.assertGreaterEqual(M.marginal_rate(6.25, 50.0, max(0, q - step), 0.40), h)
        q_star = math.sqrt(6.25 * 50.0 / (2 * 0.40 * h)) - 50.0    # 117.04
        self.assertAlmostEqual(q_star, 117.04, places=1)
        self.assertLessEqual(abs(q - q_star), step)
        # (c) d = $1.00 pushes the hurdle above the q=0 rate => ZERO
        s2 = slot("X-MKT", "bid", 6.25, 50.0, 0.40, phi=0.08, d=1.00)
        self.assertAlmostEqual(M.hurdle(0.08, 1.00, 0.40), 0.20, places=9)
        self.assertLess(M.marginal_rate(6.25, 50.0, 0, 0.40), 0.20)
        al2, _ = M.allocate([s2], 1000.0, BIG)
        self.assertEqual(al2[s2.key], 0)

    def test_T4b_budget_reserve_for_make_before_break(self):
        """T4b (B3, §2.4) — budget = ceiling - max_slot_collateral, so the transient double
        collateral of a make-before-break on the LARGEST slot fits inside the ceiling."""
        self.assertAlmostEqual(M.reserve_budget(300.0, 40.0), 260.0, places=9)
        s = slot("X-MKT", "bid", 6.25, 50.0, 0.40, phi=0.0, d=0.0)
        al, spent = M.allocate([s], 260.0, BIG, lambda_min=0.0)
        self.assertLessEqual(spent, 260.0 + 1e-9)
        max_slot = max(al[k] * 0.40 for k in al)
        self.assertLessEqual(spent + min(max_slot, 40.0), 300.0 + 1e-9)
        # and the reserve is never negative
        self.assertEqual(M.reserve_budget(10.0, 99.0), 0.0)

    def test_T5_budget_exact_no_lazy_underfill(self):
        """T5 — sum(qty*p) <= budget AND > budget - max_slot_price."""
        A = slot("A-MKT", "bid", 6.25, 1000.0, 0.40, phi=0.0, d=0.0)
        B = slot("B-MKT", "bid", 6.25, 50.0, 0.40, phi=0.0, d=0.0)
        al, spent = M.allocate([A, B], 2000.0, BIG, lambda_min=0.0)
        self.assertLessEqual(spent, 2000.0 + 1e-9)
        self.assertGreater(spent, 2000.0 - 0.40)

    def test_T5b_expensive_best_slot_does_not_strand_the_budget(self):
        """D-IMPL-1 — the spec pseudocode's line-10 `break` would abandon the remaining
        budget when the CURRENT best slot cannot afford one more contract, even though a
        cheaper slot still can.  That contradicts T5."""
        # hours_left is pinned large so this isolates the BUDGET mechanic: the runway guard
        # would otherwise (correctly) refuse the $0.10/h pool, whose whole 16h window cannot
        # reach the $2 entry floor at a conservative share.
        rich = slot("RICH-MKT", "bid", 100.0, 5.0, 0.99, phi=0.0, d=0.0, hours_left=999.0)
        cheap = slot("CHEAP-MKT", "bid", 0.10, 5.0, 0.01, phi=0.0, d=0.0, hours_left=999.0)
        al, spent = M.allocate([rich, cheap], 1.00, BIG, lambda_min=0.0)
        self.assertGreater(al[cheap.key], 0)
        self.assertGreater(spent, 1.00 - 0.99)

    def test_T6_caps_and_refill(self):
        """T6 (§8.1/§8.2) — no slot exceeds n_cap = floor($10/p); freed budget re-fills."""
        self.assertEqual(M.n_cap(0.40), 25)
        self.assertEqual(M.n_cap(0.02), 500)
        A = slot("A-MKT", "bid", 6.25, 50.0, 0.40)
        B = slot("B-MKT", "bid", 6.25, 50.0, 0.02)
        al, spent = M.allocate([A, B], 1000.0)
        self.assertLessEqual(al[A.key], M.n_cap(0.40))
        self.assertLessEqual(al[B.key], M.n_cap(0.02))
        # per-market cap: collateral <= min(4*pool, 0.25*budget)
        for s in (A, B):
            self.assertLessEqual(al[s.key] * s.p,
                                 M.market_cap_usd(s, 1000.0) + 1e-9)
        # freed budget re-fills: dropping A does not shrink B
        al2, _ = M.allocate([B], 1000.0)
        self.assertGreaterEqual(al2[B.key], al[B.key])

    def test_T7_determinism(self):
        """T7 — identical output over 100 runs; tie-break ticker then side."""
        slots = [slot("Z-MKT", "ask", 6.25, 50.0, 0.40),
                 slot("A-MKT", "bid", 6.25, 50.0, 0.40),
                 slot("A-MKT", "ask", 6.25, 50.0, 0.40)]
        ref = None
        for _ in range(100):
            al, spent = M.allocate(list(slots), 100.0, BIG)
            cur = (tuple(sorted((str(k), v) for k, v in al.items())), round(spent, 9))
            if ref is None:
                ref = cur
            self.assertEqual(cur, ref)

    def test_T3b_pinned_denied_p6_and_frozen_slots_are_never_funded(self):
        """ALLOCATE line 1-2 (§2.4) plus §9.4b (T32b) and §10.3-P6."""
        base = dict(rho=6.25, S=50.0, p=0.02)
        cases = [("PIN", dict(pinned=True)), ("DENY", dict(denied=True)),
                 ("ILLEGAL", dict(legal_price_exists=False)),
                 ("P6", dict(p6_ok=False)), ("FROZEN", dict(assume_filled=True))]
        for name, kw in cases:
            s = M.Slot(name, "bid", base["rho"], base["S"], base["p"], **kw)
            al, spent = M.allocate([s], 100.0, BIG)
            self.assertEqual(al[s.key], 0, name)
            self.assertEqual(spent, 0.0, name)

    def test_T0_land_grab_is_a_separate_path_from_allocate(self):
        """§6.1/§6.2/D2 — at S ~ 0 the marginal rate is 0 (share is already ~1 for any q),
        so ALLOCATE correctly assigns 0.  The qualification gate is a separate action, and
        it posts the MINIMUM size clearing the gate — the charter's "size up in empty books"
        is inverted by the spec (divergence D2)."""
        empty = M.score_side([], 1000, 0.5, "cents")
        self.assertEqual(M.t0_qualification_size(empty, 1000), 1000)
        partial = M.score_side([(1, 25.0)], 1000, 0.5, "cents")
        self.assertFalse(partial.qualifies)
        self.assertEqual(M.t0_qualification_size(partial, 1000), 975)
        s = slot("EMPTY-MKT", "bid", 6.25, 0.0, 0.01)
        al, _ = M.allocate([s], 100.0, BIG)
        self.assertEqual(al[s.key], 0)


# =============================================================================================
# §14.2  FORFEIT FLOOR / RESCUE
# =============================================================================================
class T14_2_ForfeitAndRescue(unittest.TestCase):

    def test_T8_entry_floor_boundary_is_inclusive(self):
        self.assertFalse(M.forfeit_gate(1.99))
        self.assertTrue(M.forfeit_gate(2.00))
        self.assertTrue(M.forfeit_gate(2.01))
        self.assertEqual(M.ENTRY_FLOOR_USD, 2.00)

    def test_T9_last_nights_burned_rungs_and_the_22pct_tax(self):
        """T9 (§3.1) — the measured cost of not having this gate: 22% of last night."""
        burned = [0.95, 0.33, 0.17, 0.08, 0.01]
        for x in burned:
            self.assertFalse(M.forfeit_gate(x))
        cleared = [1.80, 1.55, 1.05, 1.00]              # the rungs that DID clear $1.00
        self.assertAlmostEqual(sum(cleared), 5.40, places=9)
        total_earned = sum(cleared) + sum(burned)
        self.assertAlmostEqual(total_earned, 6.94, places=9)
        # payable WITHOUT the gate: the burned rungs are earned and then forfeited
        payable_nogate = sum(M.payable(x) for x in cleared + burned)
        self.assertAlmostEqual(payable_nogate, 5.40, places=9)
        self.assertAlmostEqual(1.0 - payable_nogate / total_earned, 0.2219, places=3)
        # WITH the gate those rungs are never entered at all, so payable == earned.
        # (The projection at entry is what the gate reads; a rung projecting its realised
        # $0.95 or less is refused.)
        entered = [x for x in cleared + burned if M.forfeit_gate(x)]
        self.assertEqual(entered, [])                   # none of them PROJECTED >= $2 ...
        # ENTRY_FLOOR is 2x the $1.00 cliff BY CONSTRUCTION (§3.1), so a projection equal
        # to 2x realised earnings admits EXACTLY the rungs that clear the cliff.
        entered = [x for x in cleared + burned if M.forfeit_gate(2.0 * x)]
        self.assertEqual(sorted(entered), sorted(cleared))
        earned_gate = sum(entered)
        payable_gate = sum(M.payable(x) for x in entered)
        self.assertAlmostEqual(payable_gate, earned_gate, places=9)
        self.assertAlmostEqual(payable_gate, 5.40, places=9)

    def test_payable_rounds_down_to_the_cent_at_the_1_dollar_cliff(self):
        self.assertEqual(M.payable(0.999), 0.0)
        self.assertEqual(M.payable(1.00), 1.00)
        self.assertEqual(M.payable(1.999), 1.99)

    def test_T10_top_up(self):
        """T10 — A=$0.60, rate=$0.05/h, h=3 => proj $0.75 < $1.10; a Delta q exists whose
        projection clears the target and beats redeploy + fill cost."""
        r = M.rescue(A=0.60, rate_now=0.05, h=3.0, rho=1.10, S=100.0, q=10, p=0.02,
                     r_star=0.00625, C=0.20, phi=0.001, d=0.02, has_other_program=True)
        self.assertAlmostEqual(0.60 + 0.05 * 3.0, 0.75, places=9)
        self.assertEqual(r.action, M.TOP_UP)
        self.assertGreater(r.delta_q, 0)
        self.assertGreaterEqual(r.proj, M.RESCUE_TARGET_USD)
        # the spec's illustrative Delta q (rate -> $0.20/h) also clears, at proj $1.20
        self.assertAlmostEqual(0.60 + 0.20 * 3.0, 1.20, places=9)
        # and the minimal Delta q the optimiser finds is no larger than that one
        q_020 = 100.0 * 0.20 / (0.55 - 0.20)
        self.assertLessEqual(r.delta_q, math.ceil(q_020) - 10 + 1)

    def test_T11_abandon_when_no_delta_q_reaches_the_target(self):
        """T11 — h=0.2: the rho/2 ceiling cannot reach $1.10, so P(recover) is 0 BY
        CONSTRUCTION and, with another live program, abandon_value > hold_value."""
        r = M.rescue(A=0.60, rate_now=0.05, h=0.2, rho=1.10, S=100.0, q=10, p=0.02,
                     r_star=0.00625, C=0.20, phi=0.001, d=0.02, p_recover=0.5,
                     has_other_program=True)
        self.assertLess(0.60 + (1.10 / 2.0) * 0.2, M.RESCUE_TARGET_USD)
        self.assertEqual(r.action, M.ABANDON)

    def test_T12_never_abandon_an_already_cleared_program(self):
        r = M.rescue(A=1.40, rate_now=0.0, h=3.0, rho=1.10, S=100.0, q=10, p=0.02,
                     r_star=0.00625, C=0.20)
        self.assertEqual(r.action, M.KEEP)

    def test_T13_three_way_single_live_program(self):
        """T13 (S7) — with ONE live program abandon_value = 0 IDENTICALLY, so HOLD wins
        unless the residual fill risk phi*q*d*h exceeds the option value.  Cancelling a
        losing rung late is justified by FILL RISK ALONE, never by redeploying capital that
        has nowhere to go."""
        common = dict(A=0.60, rate_now=0.11, h=3.0, rho=1.10, S=2000.0, q=500, p=0.02,
                      r_star=0.00625, C=10.0, p_recover=0.5, has_other_program=False,
                      max_delta_q=0)
        r = M.rescue(phi=0.001, d=0.02, **common)
        self.assertLess(r.proj, M.RESCUE_TARGET_USD)               # rescue is live
        self.assertEqual(r.abandon_value, 0.0)                     # NO redeploy benefit
        self.assertGreater(r.hold_value, 0.0)
        self.assertEqual(r.action, M.HOLD)
        self.assertEqual(r.note, "no_redeploy_benefit")
        # raise the residual fill risk above the option value -> ABANDON on fill risk alone
        r2 = M.rescue(phi=0.001, d=1.00, **common)
        self.assertEqual(r2.abandon_value, 0.0)
        self.assertLess(r2.hold_value, 0.0)
        self.assertEqual(r2.action, M.ABANDON)

    def test_T13b_multi_day_period_gate_and_checkpoints(self):
        """T13b (B2) — a 228h program accruing $0.20/day.  The gate evaluates the PERIOD
        total, and the checkpoints are WINDOW FRACTIONS: 57/114/182.4/214.32h, NOT
        T+2h/8h/13h."""
        start, end = 0.0, 228 * 3600.0
        cps = M.checkpoint_times(start, end)
        hours = [c / 3600.0 for c in cps]
        for got, want in zip(hours, [57.0, 114.0, 182.4, 214.32]):
            self.assertAlmostEqual(got, want, places=6)
        for bad in (2.0, 8.0, 13.0):
            self.assertNotIn(bad, [round(h, 6) for h in hours])
        # at the 25% checkpoint
        elapsed_h = 57.0
        rate_h = 0.20 / 24.0
        A = rate_h * elapsed_h
        h_left = 228.0 - elapsed_h
        proj = A + rate_h * h_left
        self.assertAlmostEqual(A, 0.475, places=6)
        self.assertAlmostEqual(proj, 1.90, places=6)
        # PERIOD total is what the gate sees, not the daily $0.20
        self.assertFalse(M.forfeit_gate(0.20))
        self.assertFalse(M.forfeit_gate(proj))                     # below the $2 ENTRY floor
        self.assertGreater(proj, M.RESCUE_TARGET_USD)              # but clear of $1.10
        r = M.rescue(A=A, rate_now=rate_h, h=h_left, rho=100.0 / 228.0, S=100.0, q=50,
                     p=0.02, r_star=0.00625, C=1.0)
        self.assertEqual(r.action, M.KEEP)


# =============================================================================================
# §14.3  RECYCLING
# =============================================================================================
class T14_3_Recycle(unittest.TestCase):

    def test_T14_shed_first_even_when_the_exit_inequality_holds(self):
        """T14 (§5.3) — LHS $1.07 < RHS $15.8 by 15x, and R_blocked ($1.87/h) is ~19x the
        freed-capital term ($0.10/h): inventory is expensive because it BLOCKS THE SLOT."""
        action, info = M.recycle(40, 0, 0.41, 0.40, 8.0, 0.00625, 1.87)
        self.assertAlmostEqual(info["lhs"], 1.08, places=6)         # 0.40 + 0.68 (fee up)
        self.assertAlmostEqual(info["rhs"], 15.76, places=6)
        self.assertLess(info["lhs"], info["rhs"])
        self.assertEqual(action, M.MAKER_SHED)

    def test_T15_escalate_to_taker_exit(self):
        action, info = M.recycle(40, 0, 0.41, 0.40, 1.5, 0.00625, 1.87, shed_age_s=1801)
        self.assertLess(info["lhs"], info["rhs"])
        self.assertEqual(action, M.TAKER_EXIT)
        # ... but not before the shed has had its 30 minutes
        self.assertEqual(M.recycle(40, 0, 0.41, 0.40, 1.5, 0.00625, 1.87,
                                   shed_age_s=1799)[0], M.MAKER_SHED)
        # ... and not while there is still runway and no cap breach
        self.assertEqual(M.recycle(40, 0, 0.41, 0.40, 8.0, 0.00625, 1.87,
                                   shed_age_s=3600)[0], M.MAKER_SHED)

    def test_T16_hold_when_the_exit_destroys_value(self):
        """T16 — h=0.1, 10c spread, R_blocked $0.10/h => LHS $4.68 > RHS $0.03."""
        action, info = M.recycle(70, 0, 0.40, 0.35, 0.1, 0.00625, 0.10)
        self.assertAlmostEqual(info["lhs"], 4.68, places=6)
        self.assertAlmostEqual(info["rhs"], 0.0253125, places=9)
        self.assertEqual(action, M.RECYCLE_HOLD)

    def test_T17_taker_fee_and_the_fee_exempt_maker_path(self):
        self.assertAlmostEqual(M.taker_fee_usd(40, 0.40), 0.68, places=9)   # ceil(67.2c)
        self.assertAlmostEqual(M.taker_fee_usd(1, 0.50), 0.02, places=9)    # ceil(1.75c)
        # the shed path pays nothing: the decision routes to MakerShed, which is fee-exempt
        action, _ = M.recycle(40, 0, 0.41, 0.40, 8.0, 0.00625, 1.87)
        self.assertEqual(action, M.MAKER_SHED)

    def test_T18_locked_box_is_held_not_double_exited(self):
        """T18 (§5.5) — two-sided fills are a locked box (pay ~99c, receive exactly $1.00),
        so the cap is on NET, never gross."""
        action, info = M.recycle(30, 30, 0.41, 0.40, 8.0, 0.00625, 1.87)
        self.assertEqual(action, M.RECYCLE_HOLD)
        self.assertEqual(info["net"], 0.0)

    def test_T32b_freeze_covers_recycling_too(self):
        """T32b (S1) — a quoting-only freeze is a LIVE SHORT GENERATOR: the recycler would
        fire a shed or taker exit against contracts we do not own."""
        action, info = M.recycle(40, 0, 0.41, 0.40, 8.0, 0.00625, 1.87, assume_filled=True)
        self.assertEqual(action, M.NO_ACTION)
        self.assertEqual(info["why"], "assume_filled_freeze")
        s = M.Slot("FROZEN-MKT", "bid", 6.25, 50.0, 0.02, assume_filled=True)
        al, _ = M.allocate([s], 100.0, BIG)
        self.assertEqual(al[s.key], 0)


# =============================================================================================
# §14.4  AT-BEST / COVERAGE / REQUOTE TRIGGERS
# =============================================================================================
class T14_4_Requote(unittest.TestCase):

    def test_T19_at_best_and_trigger_a(self):
        self.assertTrue(M.at_best(40, 40))
        self.assertFalse(M.at_best(40, 41))
        trig = M.requote_triggers(40, 41, 10, 10, 50.0, 50.0, True, True, 5.0, 0.0)
        self.assertIn(M.TRIG_OFF_BEST, trig)

    def test_T20_minimum_resting_life_and_the_trigger_a_override(self):
        """T20 (§4.4/P1) — at best, 10s old, S unchanged => NO requote.  At best, 10s old,
        best moved => requote: a genuine price improvement is not a dodge."""
        self.assertEqual(M.requote_triggers(40, 40, 10, 10, 50.0, 50.0, True, True,
                                            10.0, 0.0), [])
        trig = M.requote_triggers(40, 41, 10, 10, 50.0, 50.0, True, True, 10.0, 0.0)
        self.assertEqual(trig, [M.TRIG_OFF_BEST])
        # the safety resync is suppressed inside the minimum resting life ...
        self.assertEqual(M.requote_triggers(40, 40, 10, 10, 50.0, 50.0, True, True,
                                            10.0, 999.0), [])
        # ... and fires once the order is older than it
        self.assertIn(M.TRIG_RESYNC,
                      M.requote_triggers(40, 40, 10, 10, 50.0, 50.0, True, True,
                                         31.0, 999.0))

    def test_T21_coverage_metering_matches_v3s_measured_2pct_loss(self):
        """T21 — a 1.2s gap per 60s cancel-first cycle meters 98.0%; make-before-break
        meters 100.0%.  The model validating itself against v3's MEASURED 2% loss."""
        self.assertAlmostEqual(M.coverage_from_cycle(3600, 60, 1.2), 0.980, places=9)
        self.assertAlmostEqual(M.coverage_from_cycle(3600, 60, 0.0), 1.000, places=9)
        self.assertAlmostEqual(M.coverage(57600 * 0.95, 57600), 0.95, places=9)
        # §4.2's cancel-first optimum and its flatness
        self.assertAlmostEqual(M.cancel_first_optimum_s(), 46.0, delta=0.5)
        self.assertEqual(M.CANCEL_FIRST_PERIOD_S, 46)
        self.assertAlmostEqual(M.cancel_first_efficiency(46), 0.949, places=3)
        self.assertAlmostEqual(M.cancel_first_efficiency(60), 0.948, places=3)
        self.assertAlmostEqual(M.cancel_first_efficiency(120), 0.927, places=3)

    def test_T22_refill_trigger_threshold(self):
        """T22 (§4.3b) — remaining < 50% of target q tops up."""
        self.assertIn(M.TRIG_REFILL,
                      M.requote_triggers(40, 40, 40, 100, 50.0, 50.0, True, True,
                                         60.0, 0.0))
        self.assertNotIn(M.TRIG_REFILL,
                         M.requote_triggers(40, 40, 60, 100, 50.0, 50.0, True, True,
                                            60.0, 0.0))

    def test_triggers_c_and_d(self):
        self.assertIn(M.TRIG_S_MOVED,
                      M.requote_triggers(40, 40, 100, 100, 130.0, 100.0, True, True,
                                         60.0, 0.0))
        self.assertNotIn(M.TRIG_S_MOVED,
                         M.requote_triggers(40, 40, 100, 100, 120.0, 100.0, True, True,
                                            60.0, 0.0))
        self.assertIn(M.TRIG_QUALIFIES,
                      M.requote_triggers(40, 40, 100, 100, 100.0, 100.0, False, True,
                                         5.0, 0.0))

    def test_improve_vs_join_is_evaluated_per_slot(self):
        """§2.6/N5 — no price-band shortcut; the inequality prices the extra cent at every p."""
        self.assertTrue(M.should_improve(rho=6.25, q=100, S=50.0, r_star=0.00625))
        self.assertFalse(M.should_improve(rho=0.01, q=100, S=50.0, r_star=0.50))
        self.assertFalse(M.should_improve(rho=6.25, q=100, S=0.0, r_star=0.00625))


# =============================================================================================
# §14.5  score_side — the CFTC algorithm
# =============================================================================================
# Reconstructed 2026-07-27 16:35Z gas fixtures.  PROVENANCE NOTE: the raw book snapshot
# (scratchpad/micro/books.jsonl) did not survive; these ladders are reconstructed to satisfy
# every published invariant of verify-lip-gas §3b simultaneously — reference price, top-level
# size, TOTAL resting size, and the measured per-side qualifying-set score.  They are
# therefore a faithful regression fixture for the scorer and nothing more.
GAS_4100_YES = [(68, 35), (67, 40), (66, 16), (65, 8), (64, 8), (40, 2379), (1, 1003)]
GAS_4100_NO = [(31, 0.16), (30, 8.08), (29, 4.0), (3, 1200)]


class T14_5_ScoreSide(unittest.TestCase):

    def test_T23_measured_reference_scores(self):
        """T23 — 4.100 yes side (ref 68c, top 35, 3,489 resting) => S = 60.5 +- 0.1;
        no side (ref 31c) => S = 5.2.  Mismatch means the scorer is wrong."""
        self.assertEqual(sum(s for _, s in GAS_4100_YES), 3489)
        y = M.score_side(GAS_4100_YES, 1000, 0.5, "cents")
        self.assertEqual(y.ref_c, 68)
        self.assertEqual(y.top_size, 35)
        self.assertTrue(y.qualifies)
        self.assertAlmostEqual(y.S, 60.5, delta=0.1)
        n = M.score_side(GAS_4100_NO, 1000, 0.5, "cents")
        self.assertEqual(n.ref_c, 31)
        self.assertTrue(n.qualifies)
        self.assertAlmostEqual(n.S, 5.2, delta=0.1)
        # DF = 0.5 annihilates everything more than ~6 ticks out: the 1c wall of 1,003 lots
        # is 67 ticks away and contributes 0.5^67 ~ 0.
        self.assertLess(1003 * 0.5 ** 67, 1e-15)

    def test_T24_our_share_at_best(self):
        """T24 — 100 lots at 68c => 62.3% of that entire side; 100 at 31c => 95.0%."""
        y = M.score_side(GAS_4100_YES, 1000, 0.5, "cents")
        n = M.score_side(GAS_4100_NO, 1000, 0.5, "cents")
        self.assertAlmostEqual(M.our_share(100, y.S), 0.623, delta=0.002)
        self.assertAlmostEqual(M.our_share(100, n.S), 0.950, delta=0.002)

    def test_T25_target_size_failure_clears_the_qualifying_set(self):
        """T25 — if bids run out before Target Size, the qualifying set is CLEARED, not
        partial.  A partial reading would let us "score" on a side nobody is paid on."""
        sc = M.score_side([(40, 100), (39, 200)], 1000, 0.5, "cents")
        self.assertFalse(sc.qualifies)
        self.assertEqual(sc.S, 0.0)
        self.assertEqual(sc.cum_size, 300.0)
        self.assertEqual(sc.reason, "target_size_not_reached")
        # exactly at target => qualifies
        self.assertTrue(M.score_side([(40, 1000)], 1000, 0.5, "cents").qualifies)

    def test_T26_pinned_classification_over_all_17_gas_rungs(self):
        """T26 (§1.3) — DIVERGENCE, SURFACED.  §1.3 says "10 of 17 gas rungs are pinned".
        The measured table in verify-lip-gas §3a gives 10 NON-QUALIFYING rungs, of which
        exactly 8 are pinned (2 at the 99c yes-bid cap, 6 at the 1c yes-ask floor) and 2
        (4.075, 4.085) are REVIVABLE — a legal NO bid at 1c rests on both.  §1.3's "10" is
        the non-qualifying count; the pinned count is 8.  Conflating them would throw away
        the two highest-return slots on the board (§1.4/§4-revival, ~$98/window for $20)."""
        rungs = [
            ("4.070", 99, None, "pinned"), ("4.075", 98, None, "revivable"),
            ("4.080", 99, None, "pinned"), ("4.085", 98, 99, "revivable"),
            ("4.090", 97, 98, "qualifying"), ("4.095", 91, 93, "qualifying"),
            ("4.100", 68, 69, "qualifying"), ("4.105", 40, 41, "qualifying"),
            ("4.110", 30, 36, "qualifying"), ("4.115", 4, 6, "qualifying"),
            ("4.120", 1, 3, "qualifying"),
            ("4.125", None, 1, "pinned"), ("4.130", None, 1, "pinned"),
            ("4.135", None, 1, "pinned"), ("4.140", None, 1, "pinned"),
            ("4.145", None, 1, "pinned"), ("4.150", None, 1, "pinned"),
        ]
        self.assertEqual(len(rungs), 17)
        pinned = [r for r in rungs if M.is_pinned(r[1], r[2])]
        self.assertEqual(sorted(r[0] for r in pinned),
                         sorted(r[0] for r in rungs if r[3] == "pinned"))
        self.assertEqual(len(pinned), 8)
        for name, yb, ya, want in rungs:
            self.assertEqual(M.is_pinned(yb, ya), want == "pinned", name)
        # all 7 qualifying rungs classify not-pinned
        self.assertEqual(len([r for r in rungs if r[3] == "qualifying"]), 7)
        # and the 2 revivable ones are not pinned but do not qualify on both sides
        for name, yb, ya, want in rungs:
            if want == "revivable":
                self.assertFalse(M.is_pinned(yb, ya), name)
                self.assertTrue(M.is_revivable(yb, ya, True, False), name)
        # a best AT the price cap has no Reference Price at all (the filing's proviso)
        self.assertEqual(M.score_side([(99, 5000)], 1000, 0.5, "cents").reason, "ref_at_cap")

    def test_T27_Q5_cents_vs_levels_and_which_one_allocate_consumes(self):
        """T27 (§1.5) — gapped book (best 40c, next level 34c): S_cents uses 0.5^6,
        S_levels uses 0.5^1.  S_levels >= S_cents ALWAYS, so it is the conservative ENTRY
        input; S_cents is the reconciliation model."""
        book = [(40, 100), (34, 900), (33, 500)]
        sc = M.score_side(book, 1000, 0.5, "cents")
        sl = M.score_side(book, 1000, 0.5, "levels")
        self.assertAlmostEqual(sc.S, 100 + 900 * 0.5 ** 6, places=9)
        self.assertAlmostEqual(sl.S, 100 + 900 * 0.5 ** 1, places=9)
        self.assertGreater(sl.S, sc.S)
        self.assertEqual(M.S_MODE_ENTRY, "levels")
        self.assertEqual(M.S_MODE_RECON, "cents")
        # ALLOCATE consumes the conservative reading: more rival score => less of our own
        a_c, _ = M.allocate([M.Slot("G", "bid", 6.25, sc.S, 0.40, phi=0.0, d=0.0)],
                            1000.0, BIG, lambda_min=0.0)
        a_l, _ = M.allocate([M.Slot("G", "bid", 6.25, sl.S, 0.40, phi=0.0, d=0.0)],
                            1000.0, BIG, lambda_min=0.0)
        self.assertGreaterEqual(M.our_share(a_c[("G", "bid")], sc.S),
                                M.our_share(a_l[("G", "bid")], sl.S))
        # at distance 0 and 1 -- the only places we ever quote -- the readings are identical
        flat = [(40, 600), (39, 600)]
        self.assertAlmostEqual(M.score_side(flat, 1000, 0.5, "cents").S,
                               M.score_side(flat, 1000, 0.5, "levels").S, places=12)

    def test_T28_size_ladder_change_detector(self):
        """T28 (N4) — a CHANGE DETECTOR on frozen input, never a correctness proof.

        PROVENANCE LOST: the spec's five numbers ($20.14/$105.16/$311.42/$432.43/$652.03)
        came from probe_sim2.py on the full 7-rung 2026-07-27 16:35Z snapshot, which is not
        on disk.  Re-baselined here onto the 3 rungs whose per-side qualifying scores ARE
        published (verify-lip-gas §3b: 4.090, 4.100, 4.105), which is a real frozen
        regression rather than a fixture fitted to its own answer.  Delete or re-baseline the
        moment a real payout is reconciled.
        """
        S = {"4.090": (155.6, 1041.0), "4.100": (60.5, 5.2), "4.105": (24.9, 73.2)}
        baseline = {1: 11.8492, 10: 63.8183, 100: 171.5255, 300: 221.2273, 1000: 260.0292}
        prev = -1.0
        for q in (1, 10, 100, 300, 1000):
            rew = sum(50.0 * M.our_share(q, s) for pair in S.values() for s in pair)
            self.assertAlmostEqual(rew, baseline[q], delta=0.01 * baseline[q])   # within 1%
            self.assertGreater(rew, prev)                       # monotone in size
            prev = rew
        # bounded by half the pool per slot (SnapshotScore <= 2.0 total, §0.2)
        self.assertLess(baseline[1000], 6 * 50.0)
        # concave: the reward bought by each ADDITIONAL contract strictly decreases, which
        # is the whole reason "the capital-efficient point is 10-30 lots, not 100"
        m1 = (baseline[10] - baseline[1]) / 9.0
        m2 = (baseline[100] - baseline[10]) / 90.0
        m3 = (baseline[1000] - baseline[100]) / 900.0
        self.assertGreater(m1, m2)
        self.assertGreater(m2, m3)

    def test_T28b_unit_assertion(self):
        """T28b (B1, §0.3/§15.0) — period_reward is in units of $1e-4.  A wrong unit is a
        10x or 10,000x sizing error and the startup assertion is the only thing standing
        between the model and it."""
        self.assertAlmostEqual(M.pool_usd(1_000_000), 100.00, places=9)
        self.assertAlmostEqual(M.pool_usd(10_000), 1.00, places=9)
        self.assertTrue(M.unit_assertion_ok(1_000_000))
        for wrong in (100_000, 10_000_000, 1_000, 999_000):
            self.assertFalse(M.unit_assertion_ok(wrong), wrong)
        # rho is per-hour over the program's OWN window (§0.5), never "per day"
        self.assertAlmostEqual(M.pool_rate(1_000_000, 16.0), 6.25, places=9)
        self.assertAlmostEqual(M.pool_rate(1_000_000, 228.0), 100.0 / 228.0, places=9)
        self.assertEqual(M.pool_rate(1_000_000, 0.0), 0.0)
        self.assertAlmostEqual(M.window_hours(0, 228 * 3600), 228.0, places=9)


# =============================================================================================
# §14.6  LEDGER REPLAY / RESTART
# =============================================================================================
def rec_place(oid, ticker=T, side="bid", price=0.40, size=10, fill=0, rem=None, ts=1000.0,
              coid=None, seq=1):
    return {"k": "place_resp", "t": ts, "order_id": oid,
            "coid": coid or M.make_coid(ticker, side, seq), "ticker": ticker, "side": side,
            "price": price, "size": size, "fill_count": fill,
            "remaining_count": (size - fill) if rem is None else rem, "seq": seq}


def rec_cancel(oid, http=200, reduced_by=None, ticker=T, ts=1100.0):
    return {"k": "cancel_resp", "t": ts, "order_id": oid, "ticker": ticker, "http": http,
            "reduced_by": reduced_by}


def rec_fill_obs(oid, count, ticker=T, side="bid", price_c=40, ts=1120.0):
    return {"k": "fill_obs", "t": ts, "order_id": oid, "ticker": ticker, "side": side,
            "count": count, "price_c": price_c, "src": "fills_api"}


class T14_6_LedgerReplay(unittest.TestCase):

    def test_T29_filled_invariant(self):
        """T29 (§9.2) — filled = fill_count + (remaining_count - reduced_by).

        NOTE on "collateral 0": after the cancel the order rests nothing, so the RESTING
        collateral is 0 — but §9.3 also counts positions, and 5 filled contracts at 40c are
        $2.00 of position collateral.  Both readings are asserted so the ambiguity is
        visible rather than resolved by silence."""
        st = M.ledger_replay([rec_place("O1", fill=2, rem=8),
                              rec_cancel("O1", 200, "5.00")])
        self.assertEqual(st.filled(T, "bid"), 5.0)
        self.assertEqual(st.resting_collateral, 0.0)
        self.assertAlmostEqual(st.position_collateral, 5 * 0.40, places=9)
        self.assertAlmostEqual(st.collateral, 2.00, places=9)
        self.assertEqual(st.positions[T]["yes"], 5.0)

    def test_T30_404_then_fills_show_the_full_size(self):
        st = M.ledger_replay([rec_place("O1"), rec_cancel("O1", 404),
                              rec_fill_obs("O1", 10)])
        self.assertEqual(st.filled(T, "bid"), 10.0)
        self.assertEqual(st.net_position(T), 10.0)
        self.assertAlmostEqual(st.collateral, 4.00, places=9)
        self.assertEqual(st.unknown_orders, [])

    def test_T31_two_no_fill_reads_36s_apart_before_concluding_expired(self):
        """T31 (S2) — a SINGLE no-fills read must NOT conclude "expired".  Assert the second
        query is issued, at +36s (3x the ~12s worst observed index lag)."""
        o = M.OrderState("O1", "c", T, "bid", 0.40, 10, 0.0, 10.0)
        verdict, filled, requery_at = M.disambiguate_404(o, M.FillsRead(True, 0.0),
                                                         now=1000.0)
        self.assertEqual(verdict, M.R404_NEED_REQUERY)
        self.assertIsNone(filled)
        self.assertEqual(requery_at, 1036.0)
        self.assertEqual(M.FILLS_REQUERY_DELAY_S, 36)
        verdict, filled, _ = M.disambiguate_404(o, M.FillsRead(True, 0.0),
                                                M.FillsRead(True, 0.0), now=1036.0)
        self.assertEqual(verdict, M.R404_EXPIRED)
        self.assertEqual(filled, 0.0)
        st = M.ledger_replay([rec_place("O1"), rec_cancel("O1", 404),
                              {"k": "expired", "t": 1140.0, "order_id": "O1", "ticker": T}])
        self.assertEqual(st.filled(T, "bid"), 0.0)
        self.assertEqual(st.net_position(T), 0.0)
        self.assertEqual(st.collateral, 0.0)

    def test_T31b_the_case_a_single_read_would_have_booked_as_zero(self):
        o = M.OrderState("O1", "c", T, "bid", 0.40, 10, 0.0, 10.0)
        verdict, filled, _ = M.disambiguate_404(o, M.FillsRead(True, 0.0),
                                                M.FillsRead(True, 10.0), now=1036.0)
        self.assertEqual(verdict, M.R404_FILLED)
        self.assertEqual(filled, 10.0)
        st = M.ledger_replay([rec_place("O1"), rec_cancel("O1", 404),
                              rec_fill_obs("O1", 10, ts=1136.0)])
        self.assertEqual(st.filled(T, "bid"), 10.0)

    def test_T32_query_error_assumes_fully_filled_and_freezes_the_market(self):
        """T32 (§9.4a) — conservative on inventory, and it must NEVER be resolved by
        booking zero."""
        o = M.OrderState("O1", "c", T, "bid", 0.40, 10, 0.0, 10.0)
        verdict, filled, _ = M.disambiguate_404(o, M.FillsRead(False))
        self.assertEqual(verdict, M.R404_ASSUME_FILLED)
        self.assertEqual(filled, 10.0)
        # error on the SECOND read too
        v2, f2, _ = M.disambiguate_404(o, M.FillsRead(True, 0.0), M.FillsRead(False))
        self.assertEqual((v2, f2), (M.R404_ASSUME_FILLED, 10.0))
        # two nonzero reads that DISAGREE
        v3, f3, _ = M.disambiguate_404(o, M.FillsRead(True, 4.0), M.FillsRead(True, 7.0))
        self.assertEqual((v3, f3), (M.R404_ASSUME_FILLED, 10.0))
        st = M.ledger_replay([rec_place("O1"), rec_cancel("O1", 404),
                              {"k": "assume_filled", "t": 1150.0, "order_id": "O1",
                               "ticker": T, "side": "bid"}])
        self.assertEqual(st.filled(T, "bid"), 10.0)
        self.assertIn(T, st.assume_filled)
        self.assertIn(T, st.poisoned)

    def test_T32b_freeze_survives_restart_and_clears_only_on_an_operator_record(self):
        recs = [rec_place("O1"), rec_cancel("O1", 404),
                {"k": "assume_filled", "t": 1150.0, "order_id": "O1", "ticker": T,
                 "side": "bid"}]
        self.assertIn(T, M.ledger_replay(recs).assume_filled)
        recs2 = recs + [{"k": "assume_filled_clear", "t": 9999.0, "ticker": T,
                         "operator": "ryan"}]
        st = M.ledger_replay(recs2)
        self.assertNotIn(T, st.assume_filled)
        self.assertNotIn(T, st.poisoned)

    def test_T32c_crash_gap_sweep(self):
        """T32c (S3) — fills that occurred while the process was dead belong to no specific
        UNKNOWN order.  ONE time-windowed query over [last_ledger_ts - 60s, now]."""
        lo, hi = M.crash_gap_window(5000.0, now=5300.0)
        self.assertEqual((lo, hi), (4940.0, 5300.0))
        recs = [rec_place("O1", ts=5000.0), rec_cancel("O1", 200, "10.00", ts=5000.0),
                {"k": "fill_obs", "t": 5290.0, "order_id": None, "ticker": T,
                 "side": "bid", "count": 7, "price_c": 40, "src": "fills_api",
                 "why": "crash_gap"}]
        st = M.ledger_replay(recs)
        self.assertEqual(st.filled(T, "bid"), 7.0)
        self.assertEqual(st.net_position(T), 7.0)
        self.assertAlmostEqual(st.collateral, 7 * 0.40, places=9)

    def test_T33_410_is_an_anomaly_that_stops_posting_that_market(self):
        """T33 — v1's exact loss: DELETE /portfolio/orders/{id} 410s and v1 logged it as
        success, leaving phantom stacks.  The order MAY BE LIVE."""
        st = M.ledger_replay([rec_place("O1"), rec_cancel("O1", 410)])
        self.assertIn(T, st.poisoned)
        self.assertEqual(st.unknown_orders, ["O1"])
        self.assertEqual(st.consec_cancel_anomalies, 1)
        self.assertEqual(M.ORDERS_PATH, "/portfolio/events/orders")
        # three in a row trips the error budget (§8.5)
        recs = []
        for i in range(3):
            recs.append(rec_place("O%d" % i, ts=1000.0 + i))
            recs.append(rec_cancel("O%d" % i, 410, ts=1050.0 + i))
        self.assertIn("consecutive_cancel_anomalies=3",
                      M.ledger_replay(recs).budget_tripped(now=1100.0))
        # a clean cancel resets the counter
        recs.append(rec_place("OK", ts=1200.0))
        recs.append(rec_cancel("OK", 200, "10.00", ts=1250.0))
        self.assertEqual(M.ledger_replay(recs).consec_cancel_anomalies, 0)

    def test_T34_restart_parity_over_a_four_hour_synthetic_tape(self):
        """T34 — THE TEST v3 WOULD HAVE FAILED.  A 4h tape of places, partial fills,
        cancels, a 404-with-fills and a crash-gap fill; the replayed filled_cum, positions
        and COLLATERAL must equal the independently tracked live values exactly.

        The "live" side below is deliberately naive arithmetic, not a second call into
        ledger_replay, so the two are genuinely independent bookkeepers.
        """
        recs = []
        live_filled = {}
        live_pos = {}
        live_cost = 0.0
        live_resting = 0.0
        t = 0.0
        oid = 0
        prices = {"bid": 0.40, "ask": 0.75}
        for cyc in range(48):                       # 48 cycles x 5 min = 4 hours
            t = cyc * 300.0
            for side in ("bid", "ask"):
                oid += 1
                o = "O%03d" % oid
                px = prices[side]
                size = 10
                imm = 2 if cyc % 7 == 0 else 0      # immediate partial fill on the 201
                recs.append(rec_place(o, T, side, px, size, imm, size - imm, t, seq=oid))
                live_filled[(T, side)] = live_filled.get((T, side), 0.0) + imm
                live_pos[side] = live_pos.get(side, 0.0) + imm
                live_cost += imm * M.unit_collateral(side, px)
                live_resting += (size - imm) * M.unit_collateral(side, px)
                if cyc % 11 == 3:                   # 404 that the fills API resolves
                    recs.append(rec_cancel(o, 404, ticker=T, ts=t + 60.0))
                    recs.append(rec_fill_obs(o, size - imm, T, side,
                                             int(px * 100), t + 96.0))
                    live_filled[(T, side)] += (size - imm)
                    live_pos[side] += (size - imm)
                    live_cost += (size - imm) * M.unit_collateral(side, px)
                    live_resting -= (size - imm) * M.unit_collateral(side, px)
                else:
                    reduced = 10 - imm - (3 if cyc % 5 == 0 else 0)
                    recs.append(rec_cancel(o, 200, "%.2f" % reduced, T, t + 60.0))
                    learned = (size - imm) - reduced
                    live_filled[(T, side)] += learned
                    live_pos[side] += learned
                    live_cost += learned * M.unit_collateral(side, px)
                    live_resting -= (size - imm) * M.unit_collateral(side, px)
        # a crash-gap fill nobody attributed to an order
        recs.append({"k": "fill_obs", "t": t + 200.0, "order_id": None, "ticker": T,
                     "side": "bid", "count": 4, "price_c": 40, "src": "fills_api"})
        live_filled[(T, "bid")] += 4
        live_pos["bid"] = live_pos.get("bid", 0.0) + 4
        live_cost += 4 * M.unit_collateral("bid", 0.40)

        st = M.ledger_replay(recs)
        self.assertGreater(len(recs), 200)
        for side in ("bid", "ask"):
            self.assertAlmostEqual(st.filled(T, side), live_filled[(T, side)], places=6,
                                   msg=side)
        self.assertAlmostEqual(st.positions[T]["yes"], live_pos["bid"], places=6)
        self.assertAlmostEqual(st.positions[T]["no"], live_pos["ask"], places=6)
        self.assertAlmostEqual(st.position_collateral, live_cost, places=6)
        self.assertAlmostEqual(st.resting_collateral, live_resting, places=6)
        self.assertAlmostEqual(st.collateral, live_cost + live_resting, places=6)
        self.assertGreater(st.collateral, 0.0)      # v3 reconstructed ZERO here
        self.assertEqual(st.unknown_orders, [])

    def test_T34b_restart_parity_through_a_real_file_round_trip(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "v4_ledger.jsonl")
            recs = [rec_place("O1", fill=2, rem=8), rec_cancel("O1", 200, "5.00"),
                    rec_place("O2", side="ask", price=0.75), rec_cancel("O2", 404),
                    rec_fill_obs("O2", 10, T, "ask", 75)]
            with open(path, "w") as fh:
                for r in recs:
                    fh.write(json.dumps(r) + "\n")
            a = M.ledger_replay(recs)
            b = M.replay_ledger_file(path)
            self.assertEqual(a.filled_cum, b.filled_cum)
            self.assertAlmostEqual(a.collateral, b.collateral, places=9)
            self.assertEqual(a.positions, b.positions)
            self.assertEqual(M.replay_ledger_file(os.path.join(tmp, "nope")).orders, {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_T35_coid_prefix_is_stable_across_restarts(self):
        """T35 (§9.5) — orders placed by run N must be matched by run N+1's prefix sweep.
        A run-id in the prefix would make §9.4 step 4 blind to the previous process's
        orders, which is EXACTLY v3's loss."""
        run_n = [M.make_coid(T, "bid", i) for i in (1, 2, 3)]
        run_n1 = [M.make_coid(T, "ask", i) for i in (4, 5)]
        for c in run_n + run_n1:
            self.assertTrue(M.owns_coid(c), c)
            self.assertNotIn(".", c)                      # R167
            self.assertTrue(c.startswith("v4-"))
        # deterministic: no uuid, no timestamp, no run-id
        self.assertEqual(M.make_coid(T, "bid", 7), M.make_coid(T, "bid", 7))
        self.assertNotEqual(M.make_coid(T, "bid", 7), M.make_coid(T, "bid", 8))
        self.assertNotEqual(M.make_coid(T, "bid", 7), M.make_coid(T, "ask", 7))
        # the sanitiser is applied AT COID CONSTRUCTION, not at the wire
        self.assertEqual(M.make_coid("A.B.C", "bid", 1), "v4-lipm-A_B_C-y-1")
        self.assertFalse(M.owns_coid("nestor-abc"))
        self.assertFalse(M.owns_coid(None))
        # the sequence counter is recovered from the ledger, so coids never repeat (R161:
        # coid dedupe OUTLIVES the order, and a reused coid 409s forever)
        st = M.ledger_replay([rec_place("O1", seq=41), rec_place("O2", seq=42)])
        self.assertEqual(st.coid_seq, 42)

    def test_2xx_without_order_id_poisons_the_market(self):
        """§8.5 — we cannot cancel what we cannot name."""
        st = M.ledger_replay([{"k": "place_resp", "t": 1.0, "ticker": T, "side": "bid",
                               "price": 0.40, "size": 10, "coid": "v4-x"}])
        self.assertIn(T, st.poisoned)
        self.assertEqual(len(st.orders), 0)

    def test_snapshot_records_are_advisory_only(self):
        """§9.1 — a snapshot must never be able to move reconstructed state."""
        base = [rec_place("O1"), rec_cancel("O1", 200, "10.00")]
        a = M.ledger_replay(base)
        b = M.ledger_replay(base + [{"k": "snapshot", "t": 2000.0,
                                     "positions": {T: {"yes": 999.0}},
                                     "collateral_usd": 999.0, "orders": {}}])
        self.assertEqual(a.filled_cum, b.filled_cum)
        self.assertEqual(a.collateral, b.collateral)
        self.assertEqual(a.positions, b.positions)


# =============================================================================================
# RISK CAPS, ANTI-GAMING TELEMETRY, STAND-DOWN, ORDER BODY
# =============================================================================================
class RiskAndPolicy(unittest.TestCase):

    def test_day_stop(self):
        """§8.4 — -max($20, 0.35 * projected), capped at -$150."""
        self.assertAlmostEqual(M.day_stop_usd(0.0), 20.0, places=9)
        self.assertAlmostEqual(M.day_stop_usd(100.0), 35.0, places=9)
        self.assertAlmostEqual(M.day_stop_usd(10000.0), 150.0, places=9)

    def test_first_run_ceiling_and_allowlist_defaults(self):
        self.assertGreater(M.MAX_TOTAL_COLLATERAL_USD, 0.0)
        self.assertEqual(M.EVENT_ALLOWLIST, [])          # OFF: the scanner ranks everything
        self.assertIn("KXRAIN", M.DENY_SERIES)

    def test_post_size_and_refill_cap_are_decoupled(self):
        """§8.7 — the v3 lesson.  refill_cap = 4 * n_cap, independent of q_target."""
        self.assertEqual(M.refill_cap(0.40), 4 * 25)
        self.assertEqual(M.refill_cap(0.02), 4 * 500)

    def test_P3_portfolio_two_sidedness(self):
        """§10.3-P3 (S4) — measured at the PORTFOLIO level.  Per-slot pairing directly
        contradicts ALLOCATE, which by construction funds one side of a rung (T1)."""
        resting = [{"ticker": "M1", "side": "bid", "collateral": 10.0},
                   {"ticker": "M1", "side": "ask", "collateral": 10.0},
                   {"ticker": "M2", "side": "bid", "collateral": 5.0},
                   {"ticker": "M3", "side": "bid", "collateral": 5.0}]
        c_pct, m_pct = M.two_sided_metrics(resting)
        self.assertAlmostEqual(c_pct, 20.0 / 30.0, places=9)
        self.assertAlmostEqual(m_pct, 1.0 / 3.0, places=9)
        self.assertGreaterEqual(c_pct, M.P3_TWO_SIDED_COLLATERAL_MIN)
        self.assertGreaterEqual(m_pct, M.P3_TWO_SIDED_MARKET_MIN)
        # pinned / shedding markets are excluded from the ratio
        c2, m2 = M.two_sided_metrics(resting + [{"ticker": "M4", "side": "bid",
                                                 "collateral": 100.0, "excluded": True}])
        self.assertAlmostEqual(c2, c_pct, places=9)

    def test_P4_fill_honor_and_P5_cheap_side_telemetry(self):
        self.assertAlmostEqual(M.fill_honor_ratio(95, 5), 0.95, places=9)
        self.assertLess(M.fill_honor_ratio(80, 20), M.P4_FILL_HONOR_FLOOR)
        # P5 is TELEMETRY, never a block: a 95%-cheap posture is the DERIVED answer (D5)
        pct = M.cheap_side_score_pct([(0.02, 95.0), (0.68, 5.0)])
        self.assertAlmostEqual(pct, 0.95, places=9)
        self.assertLessEqual(pct, M.P5_CHEAP_SIDE_ALERT)

    def test_P6_both_directions(self):
        """§10.3-P6 — the rule that actually settles Q1, applied BOTH ways."""
        self.assertFalse(M.p6_pre_entry_ok(0))
        self.assertTrue(M.p6_pre_entry_ok(1))
        self.assertTrue(M.p6_prune([0, 0, 0, 0, 0]))
        self.assertFalse(M.p6_prune([0, 0, 1, 0, 0]))
        self.assertFalse(M.p6_prune([0, 0, 0, 0]))       # not enough days yet
        self.assertEqual(M.P6_LOOKBACK_DAYS, 5)

    def test_P7_revival_caps(self):
        """§10.3-P7 — <=3 concurrent revival markets, and never >90% of a qualifying side
        for more than 5 consecutive days on the same market."""
        cands = [("M1", 10.0), ("M2", 9.0), ("M3", 8.0), ("M4", 7.0), ("M5", 7.0)]
        allowed = M.p7_revival_allowed(cands)
        self.assertEqual(allowed, {"M1", "M2", "M3"})
        self.assertEqual(len(allowed), M.P7_MAX_REVIVAL_MARKETS)
        # deterministic on ties
        self.assertEqual(M.p7_revival_allowed([("B", 1.0), ("A", 1.0)], 1), {"A"})
        self.assertFalse(M.p7_side_share_breach([0.95] * 5))
        self.assertTrue(M.p7_side_share_breach([0.95] * 6))
        self.assertFalse(M.p7_side_share_breach([0.95, 0.95, 0.5, 0.95, 0.95, 0.95]))

    def test_standdown_triggers_are_independent(self):
        """§12.3 (S6) — (a) bad ratio and (b) NO DATA.  A silent reconciliation loop is
        worse than a bad one: it looks identical to a good day while capital scales."""
        self.assertTrue(M.standdown_ratio_breach([1.0, 0.3, 0.4]))
        self.assertFalse(M.standdown_ratio_breach([0.3, 0.6]))     # 0.6 is inside 2x
        self.assertFalse(M.standdown_ratio_breach([0.3]))
        self.assertTrue(M.standdown_ratio_breach([5.0, 3.0]))      # bad in EITHER direction
        self.assertTrue(M.standdown_nodata_breach([3, 0, 0]))
        self.assertFalse(M.standdown_nodata_breach([0, 1]))

    def test_disclosure_ladder(self):
        """§10.4 — we are below notice at every rung of the planned ladder."""
        self.assertEqual(M.disclosure_state(45.0, 5.0)[0], "NO_DISCLOSE")
        self.assertEqual(M.disclosure_state(5000.0, 100.0)[0], "MONITOR")
        self.assertEqual(M.disclosure_state(20000.0, 100.0)[0], "DISCLOSE_BEFORE_DEPLOY")
        self.assertEqual(M.disclosure_state(45.0, 5000.0)[0], "DISCLOSE_NOW")

    def test_order_body_matches_the_v3_proven_contract(self):
        """§4.7 — the body that actually rests on V2."""
        b = M.order_body(T, "ask", 0.7600, 1785000000, "v4-lipm-x-n-1", 6)
        self.assertEqual(b["ticker"], T)
        self.assertEqual(b["side"], "ask")
        self.assertEqual(b["count"], "6.00")
        self.assertEqual(b["price"], "0.7600")
        self.assertEqual(b["time_in_force"], "good_till_canceled")
        self.assertEqual(b["expiration_ts"], 1785000000)
        self.assertEqual(b["self_trade_prevention_type"], "taker_at_cross")
        self.assertNotIn(".", b["client_order_id"])
        self.assertEqual(M.price_str(0.0499999), "0.0500")

    def test_unit_collateral_ask_is_the_no_price(self):
        self.assertAlmostEqual(M.unit_collateral("bid", 0.74), 0.74, places=9)
        self.assertAlmostEqual(M.unit_collateral("ask", 0.75), 0.25, places=9)

    def test_book_parsing_is_v3_proven(self):
        book = {"orderbook": {"orderbook_fp": {
            "yes_dollars": [["0.7000", "50"], ["0.7400", "12"], ["0.7200", "30"]],
            "no_dollars": [["0.2000", "40"], ["0.2400", "9"]]}}}
        self.assertEqual(M.best_from_book(book), (74, 76))
        yes, no = M.book_levels(book)
        self.assertEqual(sorted(yes), [(70, 50.0), (72, 30.0), (74, 12.0)])
        self.assertEqual(M.best_from_book({}), (None, None))
        self.assertEqual(M.book_levels({}), ([], []))

    def test_the_signed_path_excludes_the_query_string(self):
        """R166 — the one auth trap.  Asserted structurally, with no network and no key."""
        class FakeKey(object):
            def __init__(self):
                self.msg = None

            def sign(self, msg, *a, **k):
                self.msg = msg
                return b"sig"

        fk = FakeKey()
        M.Auth("kid", fk).headers("GET", "/portfolio/fills?order_id=abc&limit=200")
        self.assertTrue(fk.msg.endswith(b"GET/trade-api/v2/portfolio/fills"))
        self.assertNotIn(b"?", fk.msg)


# =============================================================================================
# ADVERSARIAL REVIEW REGRESSIONS (B1, S1-S4) — each asserts the FIXED behaviour, on the
# reviewer's own live-measured shape where one exists.
# =============================================================================================
RHO_GAS = M.pool_rate(1000000, M.window_hours(1785585600.0, 1785643140.0))
# LIVE-MEASURED 2026-07-28T02:11Z: 4.070-4.085 pinned (yes_bid=99); 4.130-4.150 pinned
# (yes_ask=1).  Qualifying-side scores from verify-lip-gas §3b where published.
GAS_PINNED_NOW = {"4.070", "4.075", "4.080", "4.085",
                  "4.130", "4.135", "4.140", "4.145", "4.150"}
GAS_SIDES = {"4.090": [(155.6, 0.96), (1041.0, 0.02)],
             "4.095": [(120.0, 0.91), (300.0, 0.09)],
             "4.100": [(60.5, 0.68), (5.2, 0.31)],
             "4.105": [(24.9, 0.40), (73.2, 0.59)],
             "4.110": [(18.0, 0.30), (40.0, 0.64)],
             "4.115": [(9.0, 0.04), (600.0, 0.94)],
             "4.120": [(6.0, 0.01), (900.0, 0.97)],
             "4.125": [(4.0, 0.01), (1500.0, 0.98)]}
GAS_RUNGS = ["4.070", "4.075", "4.080", "4.085", "4.090", "4.095", "4.100", "4.105",
             "4.110", "4.115", "4.120", "4.125", "4.130", "4.135", "4.140", "4.145",
             "4.150"]


def gas_classified():
    out = {}
    for r in GAS_RUNGS:
        sides = [{"S": S, "p": p, "qualifies": True, "legal": r not in GAS_PINNED_NOW}
                 for S, p in GAS_SIDES.get(r, [(1.0, 0.99)])]
        out["KXAAAGASD-26JUL28-%s" % r] = {
            "rho": RHO_GAS, "pinned": r in GAS_PINNED_NOW, "denied": False, "sides": sides}
    return out


class B1_ClassifyThenClamp(unittest.TestCase):

    def test_rho_alone_cannot_rank_inside_one_event(self):
        """B1 root cause — every rung of one gas daily carries the identical period_reward
        and the identical window, so rho is CONSTANT across all 17 and the ticker tie-break
        decides the §4.6 clamp entirely."""
        rhos = {M.pool_rate(1000000, M.window_hours(1785585600.0, 1785643140.0))
                for _ in GAS_RUNGS}
        self.assertEqual(len(rhos), 1)
        # ... and ticker-ascending on a gas daily is deep-ITM-first, i.e. pinned-first
        by_ticker = sorted(GAS_RUNGS)[:M.MAX_REST_MARKETS]
        self.assertEqual(len([r for r in by_ticker if r in GAS_PINNED_NOW]), 4)

    def test_clamp_never_spends_a_rest_slot_on_a_pinned_market(self):
        """B1 fix — classify THEN clamp.  No snapshot on a pinned market can EVER be
        included, so no REST slot may be spent on one."""
        picked = [t.rsplit("-", 1)[1] for t in M.market_poll_rank(gas_classified())]
        self.assertEqual(len(picked), M.MAX_REST_MARKETS)
        self.assertEqual([r for r in picked if r in GAS_PINNED_NOW], [])
        # the reviewer's live selection is exactly what must NOT happen any more
        self.assertNotEqual(picked, ["4.070", "4.075", "4.080", "4.085", "4.090", "4.095"])

    def test_clamp_selects_the_slots_the_allocator_would_fund(self):
        """B1 fix — the clamp ranks by the ALLOCATOR'S OWN first-dollar rate, so the three
        best slots on the board can no longer be starved of a poll."""
        picked = [t.rsplit("-", 1)[1] for t in M.market_poll_rank(gas_classified())]
        self.assertFalse({"4.100", "4.105", "4.110"}.isdisjoint(picked))
        # ranking is by value, descending, and deterministic
        vals = [M.market_rank_value(gas_classified()["KXAAAGASD-26JUL28-%s" % r])
                for r in picked]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertEqual(M.market_poll_rank(gas_classified()),
                         M.market_poll_rank(gas_classified()))

    def test_first_dollar_rate_is_the_allocator_rate_and_prices_revivals(self):
        """§0.4 at q=0, and §1.4: a legal-but-unqualified side has S=0, where the marginal
        form is degenerate — it must rank HIGH (the revival trade), never as zero."""
        self.assertAlmostEqual(M.slot_first_dollar_rate(6.25, 50.0, 0.40),
                               M.marginal_rate(6.25, 50.0, 0, 0.40), places=12)
        rev = M.slot_first_dollar_rate(6.25, 0.0, 0.99, qualifies=False, target_size=1000.0)
        self.assertGreater(rev, 0.0)
        self.assertAlmostEqual(rev, M.marginal_rate(6.25, 1000.0, 0, 0.01), places=12)
        # pinned / illegal is worth exactly zero, not "a little"
        self.assertEqual(M.slot_first_dollar_rate(6.25, 50.0, 0.40, legal=False), 0.0)
        self.assertEqual(M.market_rank_value({"rho": 6.25, "pinned": True,
                                              "sides": [{"S": 5.0, "p": 0.02}]}), 0.0)
        self.assertEqual(M.market_poll_rank({"P": {"rho": 6.25, "pinned": True,
                                                   "sides": [{"S": 5.0, "p": 0.02}]}}), [])


class S1_DayStop(unittest.TestCase):

    def test_mark_to_market_and_the_breach_predicate(self):
        """§8.4 — YES marks at the yes mid, NO at (1 - yes mid); cost from the ledger."""
        pos = {"T": {"yes": 10.0, "no": 0.0}}
        cost = {"T": 4.00}                                   # 10 YES bought at 40c
        self.assertAlmostEqual(M.mark_to_market_pnl(pos, cost, {"T": 0.40}), 0.0, places=9)
        self.assertAlmostEqual(M.mark_to_market_pnl(pos, cost, {"T": 0.30}), -1.0, places=9)
        self.assertAlmostEqual(M.mark_to_market_pnl(pos, cost, {"T": 0.30}, 0.68),
                               -1.68, places=9)
        no = {"T": {"yes": 0.0, "no": 10.0}}
        self.assertAlmostEqual(M.mark_to_market_pnl(no, {"T": 6.00}, {"T": 0.40}), 0.0,
                               places=9)
        # NEW-2: a market with no two-sided mid cannot be marked, so it marks AT COST and
        # contributes exactly zero -- NOT minus its whole cost (that read pinned-rung
        # inventory as a total loss and tripped the day stop on the gas books).
        self.assertAlmostEqual(M.mark_to_market_pnl(pos, cost, {}), 0.0, places=9)

    def test_breach_thresholds(self):
        """§8.4 — -max($20, 0.35*projected), capped at -$150."""
        self.assertFalse(M.day_stop_breached(-19.99, 0.0))
        self.assertTrue(M.day_stop_breached(-20.00, 0.0))
        self.assertFalse(M.day_stop_breached(-34.99, 100.0))
        self.assertTrue(M.day_stop_breached(-35.00, 100.0))
        self.assertFalse(M.day_stop_breached(-149.0, 10000.0))
        self.assertTrue(M.day_stop_breached(-150.0, 10000.0))
        self.assertFalse(M.day_stop_breached(500.0, 100.0))      # profit never breaches

    def test_a_breached_stop_actually_stops_posting(self):
        """S1 — the fix is not the predicate, it is the CALL SITE.  A halted maker places
        nothing, on every path into placement, and consumes no coid sequence."""
        m = M.Maker(None, M.LedgerState(), [])
        m.halted = True
        self.assertIsNone(m.place("T", "bid", 40, 10, 1785000000))
        self.assertEqual(m.st.coid_seq, 0)                  # no seq burned, no wire call
        self.assertEqual(m.st.orders, {})

    def test_the_day_stop_is_wired_into_the_cycle(self):
        """S1 — a breach cancels all, flattens, alerts and halts.  Runs with no network:
        there are no live orders to cancel and no positions to flatten."""
        tmp = tempfile.mkdtemp()
        old_dir, old_led = M.DATA_DIR, M.LEDGER_PATH
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            st = M.LedgerState()
            st.positions = {"T": {"yes": 100.0, "no": 0.0}}
            st.position_cost = {"T": 40.00}
            m = M.Maker(None, st, [])
            m.scores = {"T": {"yes_bid_c": 5, "yes_ask_c": 7}}   # marked down to ~6c
            self.assertTrue(m.check_day_stop([], {}, 1000.0))
            self.assertTrue(m.halted)
            self.assertTrue(m.stopping)
            self.assertLess(m.day_pnl, -20.0)
            self.assertIsNone(m.place("T", "bid", 40, 10, 1785000000))
        finally:
            M.DATA_DIR, M.LEDGER_PATH = old_dir, old_led
            shutil.rmtree(tmp, ignore_errors=True)


class S2_FillIdempotency(unittest.TestCase):

    def test_the_same_api_fill_is_booked_once_however_many_restarts(self):
        """S2 — §9.4 step 4 re-reads [last_ts-60s, now] BY DESIGN, so a crash loop
        re-observes the same fills.  Without an idempotency key replay double-books them."""
        base = [rec_place("O1")]
        gap = {"k": "fill_obs", "t": 1010.0, "order_id": None, "fill_id": "trade-777",
               "ticker": T, "side": "bid", "count": 10, "price_c": 40,
               "src": "fills_api", "why": "crash_gap"}
        one = M.ledger_replay(base + [gap])
        self.assertAlmostEqual(one.filled(T, "bid"), 10.0)
        for n_restarts in (2, 3, 5):
            recs = base + [dict(gap, t=1010.0 + 25 * i) for i in range(n_restarts)]
            st = M.ledger_replay(recs)
            self.assertAlmostEqual(st.filled(T, "bid"), 10.0, msg=n_restarts)
            self.assertAlmostEqual(st.net_position(T), 10.0)
            # $4.00 of position + the $4.00 still resting on O1 (never cancelled here)
            self.assertAlmostEqual(st.collateral, 4.00 + 4.00, places=6)
            self.assertAlmostEqual(st.position_collateral, 4.00, places=6)
        # ... and the re-query window genuinely does overlap, which is why this is needed
        lo, hi = M.crash_gap_window(1010.0, now=1035.0)
        self.assertLess(lo, 1010.0)

    def test_distinct_fills_are_still_counted_separately(self):
        """The dedup must not collapse two genuinely different fills."""
        base = [rec_place("O1", size=20, rem=20)]
        a = {"k": "fill_obs", "t": 1010.0, "order_id": None, "fill_id": "trade-1",
             "ticker": T, "side": "bid", "count": 10, "price_c": 40, "src": "fills_api",
             "why": "crash_gap"}
        b = dict(a, fill_id="trade-2", t=1011.0)
        self.assertAlmostEqual(M.ledger_replay(base + [a, b]).filled(T, "bid"), 20.0)

    def test_pre_fix_unkeyed_crash_gap_rows_are_deduped_by_content_and_counted(self):
        """A row written by a pre-fix binary carries no key.  Its identity is well defined
        for this class alone (an overlapping re-read of the same window), and the fallback
        is COUNTED so the operator can see it was used."""
        base = [rec_place("O1")]
        gap = {"k": "fill_obs", "t": 1010.0, "order_id": None, "ticker": T, "side": "bid",
               "count": 10, "price_c": 40, "src": "fills_api", "why": "crash_gap"}
        st = M.ledger_replay(base + [gap, dict(gap, t=1035.0)])
        self.assertAlmostEqual(st.filled(T, "bid"), 10.0)
        self.assertEqual(st.unkeyed_fill_rows, 2)

    def test_N2_duplicate_place_resp_creates_one_order(self):
        """N2 — a retried write or a copied ledger must not create a second order or
        double-credit its immediate fill_count."""
        r = rec_place("O1", fill=3, rem=7)
        st = M.ledger_replay([r, dict(r, t=1001.0)])
        self.assertEqual(len(st.orders), 1)
        self.assertAlmostEqual(st.filled(T, "bid"), 3.0)
        self.assertAlmostEqual(st.collateral, 3 * 0.40 + 7 * 0.40, places=9)


class S3_AccrualSurvivesRestart(unittest.TestCase):

    def test_the_reviewers_scenario_restart_preserves_KEEP(self):
        """S3 — reproduced by review: live A=$0.95 KEEP, post-restart A=0 ABANDON, which
        forfeits real accrued score to a bookkeeping gap."""
        rho, S, q, p = 6.25, 60.0, 100, 0.40
        r_star = M.LAMBDA_MIN / M.LAMBDA_MIN_WINDOW_HOURS
        C, h = q * p, 0.30
        rate_now = M.reward_rate(rho, q, S)
        live = M.rescue(0.95, rate_now, h, rho, S, q, p, r_star, C, has_other_program=True)
        self.assertEqual(live.action, M.KEEP)
        # WITHOUT persistence the restarted process abandons ...
        lost = M.rescue(0.0, rate_now, h, rho, S, q, p, r_star, C, has_other_program=True)
        self.assertEqual(lost.action, M.ABANDON)
        # ... WITH it, the ledger hands A back and the decision is unchanged
        st = M.ledger_replay([{"k": "accrual", "t": 5000.0, "program_id": "P1",
                               "accrued_usd": 0.95, "checkpoints_done": [0.25, 0.50]}])
        self.assertAlmostEqual(st.accrued["P1"], 0.95, places=9)
        restarted = M.rescue(st.accrued["P1"], rate_now, h, rho, S, q, p, r_star, C,
                             has_other_program=True)
        self.assertEqual(restarted.action, M.KEEP)
        self.assertEqual(restarted.action, live.action)

    def test_checkpoints_do_not_refire_after_a_restart(self):
        st = M.ledger_replay([{"k": "accrual", "t": 5000.0, "program_id": "P1",
                               "accrued_usd": 0.95, "checkpoints_done": [0.25, 0.50]}])
        self.assertEqual(st.checkpoints_done["P1"], {0.25, 0.50})
        m = M.Maker(None, st, [])
        self.assertEqual(m.accrued["P1"], 0.95)
        self.assertEqual(m.checkpoints_done["P1"], {0.25, 0.50})
        self.assertIn(0.25, m.checkpoints_done["P1"])         # would refire without this
        self.assertNotIn(0.80, m.checkpoints_done["P1"])      # and 80% still must fire
        # the LAST accrual row wins (it is a checkpoint of the integral, not a delta)
        st2 = M.ledger_replay([
            {"k": "accrual", "t": 5000.0, "program_id": "P1", "accrued_usd": 0.95,
             "checkpoints_done": [0.25]},
            {"k": "accrual", "t": 5060.0, "program_id": "P1", "accrued_usd": 1.40,
             "checkpoints_done": [0.25, 0.50]}])
        self.assertAlmostEqual(st2.accrued["P1"], 1.40, places=9)
        self.assertEqual(st2.checkpoints_done["P1"], {0.25, 0.50})

    def test_accrual_rows_are_authoritative_but_snapshots_are_still_advisory(self):
        """§9.1's invariant is preserved: an ADVISORY snapshot still cannot move state.
        `accrual` is a separate, authoritative record kind precisely so that stays true."""
        base = [rec_place("O1"), rec_cancel("O1", 200, "10.00")]
        a = M.ledger_replay(base)
        b = M.ledger_replay(base + [{"k": "snapshot", "t": 2000.0,
                                     "positions": {T: {"yes": 999.0}},
                                     "collateral_usd": 999.0, "orders": {}}])
        self.assertEqual(a.positions, b.positions)
        self.assertEqual(a.collateral, b.collateral)
        self.assertEqual(b.accrued, {})


class S4_TakerExitIsSuppressedAtThisRung(unittest.TestCase):

    def test_the_decision_is_still_computed_and_priced(self):
        """S4 — the §5.2 inequality and the §5.4 escalation are unchanged; only the ORDER
        is withheld, and the value forgone is computable so the choice stays measured."""
        action, info = M.recycle(40, 0, 0.41, 0.40, 1.5, 0.00625, 1.87, shed_age_s=1801)
        self.assertEqual(action, M.TAKER_EXIT)
        self.assertGreater(info["rhs"] - info["lhs"], 0.0)

    def test_the_gate_is_off_at_the_first_run_ceiling_and_is_a_ladder_decision(self):
        self.assertFalse(M.TAKER_EXIT_ENABLED)
        self.assertLess(M.MAX_TOTAL_COLLATERAL_USD, M.TAKER_EXIT_REQUIRED_ABOVE_USD)
        # the bound the derivation rests on: stranded inventory is capped per slot at $10
        self.assertAlmostEqual(M.n_cap(0.40) * 0.40, 10.0, places=9)
        self.assertLessEqual(M.n_cap(0.02) * 0.02, 10.0 + 1e-9)


# =============================================================================================
# VERIFICATION-ROUND REGRESSIONS (NEW-1 .. NEW-5)
# =============================================================================================
class NEW1_RevivalsDoNotBurnARestSlot(unittest.TestCase):

    def test_a_market_allocate_will_never_fund_does_not_take_the_slot(self):
        """NEW-1 — market_rank_value promoted revival markets whose sides ALLOCATE then
        skips (S=0 => marginal rate 0 => $0 forever), because §6.2's T0 qualification path
        has zero call sites.  A REVIVE market took a top-6 REST slot and earned nothing."""
        revive = {"rho": 6.25, "pinned": False, "denied": False,
                  "sides": [{"S": 0.0, "p": 0.99, "qualifies": False, "legal": True,
                             "target_size": 1000.0}]}
        good = {"rho": 6.25, "pinned": False, "denied": False,
                "sides": [{"S": 60.5, "p": 0.68, "qualifies": True, "legal": True}]}
        # the revival PROXY really does out-rank the good market -- that was the defect
        self.assertGreater(M.market_rank_value(revive, count_unqualified=True),
                           M.market_rank_value(good))
        # ... and ALLOCATE funds the revival exactly $0, which is why the slot was wasted
        rev_slot = M.Slot("REVIVE", "bid", 6.25, 0.0, 0.99)
        al, spent = M.allocate([rev_slot], 45.0, BIG)
        self.assertEqual(al[rev_slot.key], 0)
        self.assertEqual(spent, 0.0)
        # FIX: while the T0 path is unwired the clamp ignores unqualified sides, so with one
        # REST slot the GOOD market takes it.
        self.assertFalse(M.T0_QUALIFICATION_WIRED)
        self.assertEqual(M.market_rank_value(revive), 0.0)
        self.assertEqual(M.market_poll_rank({"REVIVE": revive, "GOOD": good}, 1), ["GOOD"])
        self.assertEqual(M.market_poll_rank({"REVIVE": revive, "GOOD": good}), ["GOOD"])

    def test_the_revival_arithmetic_itself_is_untouched_and_returns_when_wired(self):
        """The gate is on USING the value, not on the maths -- §1.4 stays correct so that
        wiring the T0 call site is a one-flag change with a test already covering it."""
        revive = {"rho": 6.25, "pinned": False, "denied": False,
                  "sides": [{"S": 0.0, "p": 0.99, "qualifies": False, "legal": True,
                             "target_size": 1000.0}]}
        self.assertAlmostEqual(M.market_rank_value(revive, count_unqualified=True),
                               M.marginal_rate(6.25, 1000.0, 0, 0.01), places=12)
        self.assertGreater(M.t0_qualification_size(M.score_side([], 1000, 0.5), 1000), 0)

    def test_pinned_exclusion_is_unaffected(self):
        pinned = {"rho": 6.25, "pinned": True, "denied": False,
                  "sides": [{"S": 5.0, "p": 0.02, "qualifies": True, "legal": False}]}
        self.assertEqual(M.market_rank_value(pinned, count_unqualified=True), 0.0)


class NEW2_UnpricedPositionsDoNotTripTheDayStop(unittest.TestCase):

    def test_pinned_rung_inventory_does_not_move_pnl(self):
        """NEW-2 — a pinned rung is ONE-SIDED BY DEFINITION (§1.3), so it has no mid.
        Subtracting its full cost printed two $10 slots as -$20 = the §8.4 floor, and the
        stop cancelled everything mid-window on exactly the gas books."""
        pos = {"PIN1": {"yes": 1000.0, "no": 0.0}, "PIN2": {"yes": 1000.0, "no": 0.0}}
        cost = {"PIN1": 10.0, "PIN2": 10.0}
        self.assertAlmostEqual(M.mark_to_market_pnl(pos, cost, {}), 0.0, places=9)
        self.assertFalse(M.day_stop_breached(M.mark_to_market_pnl(pos, cost, {}), 0.0))
        # the old behaviour, stated so the regression is unmistakable
        self.assertAlmostEqual(-sum(cost.values()), -20.0, places=9)
        self.assertTrue(M.day_stop_breached(-20.0, 0.0))
        # and it is surfaced, never silent
        self.assertEqual(M.unpriced_positions(pos, {}), ["PIN1", "PIN2"])
        self.assertEqual(M.unpriced_positions(pos, {"PIN1": 0.5}), ["PIN2"])
        self.assertEqual(M.unpriced_positions({"F": {"yes": 0.0, "no": 0.0}}, {}), [])

    def test_a_priced_position_still_marks_and_still_stops(self):
        """The fix must not blunt the stop where a mark DOES exist."""
        pos = {"T": {"yes": 1000.0, "no": 0.0}}
        cost = {"T": 40.0}
        self.assertAlmostEqual(M.mark_to_market_pnl(pos, cost, {"T": 0.02}), -20.0,
                               places=9)
        self.assertTrue(M.day_stop_breached(M.mark_to_market_pnl(pos, cost, {"T": 0.02}),
                                            0.0))

    def test_mixed_book_marks_only_what_it_can_see(self):
        pos = {"SEEN": {"yes": 100.0, "no": 0.0}, "BLIND": {"yes": 100.0, "no": 0.0}}
        cost = {"SEEN": 40.0, "BLIND": 40.0}
        self.assertAlmostEqual(M.mark_to_market_pnl(pos, cost, {"SEEN": 0.30}), -10.0,
                               places=9)


class NEW3_FillKeyDoesNotCollide(unittest.TestCase):

    def test_two_distinct_equal_size_fills_both_book(self):
        """NEW-3 — the exchange fills a 10-lot order as 5+5 routinely.  The first synthetic
        key collided on (order_id, ticker, side, count, time), so replay DROPPED the second
        fill: under-counted inventory, the §9.4b naked-short direction."""
        f = {"order_id": "O1", "ticker": T, "side": "yes", "count": 5, "yes_price": 40,
             "created_time": "2026-07-28T02:00:00Z"}
        k0, k1 = M.Maker.fill_key(f, 0), M.Maker.fill_key(dict(f), 1)
        self.assertNotEqual(k0, k1)
        rows = [{"k": "fill_obs", "t": 1010.0, "order_id": None, "fill_id": k0,
                 "ticker": T, "side": "bid", "count": 5, "price_c": 40,
                 "src": "fills_api", "why": "crash_gap"},
                {"k": "fill_obs", "t": 1010.0, "order_id": None, "fill_id": k1,
                 "ticker": T, "side": "bid", "count": 5, "price_c": 40,
                 "src": "fills_api", "why": "crash_gap"}]
        st = M.ledger_replay(rows)
        self.assertAlmostEqual(st.filled(T, "bid"), 10.0)      # was 5.0 -- under-counted
        self.assertAlmostEqual(st.net_position(T), 10.0)

    def test_price_is_part_of_the_key(self):
        base = {"order_id": "O1", "ticker": T, "side": "yes", "count": 5,
                "created_time": "2026-07-28T02:00:00Z"}
        self.assertNotEqual(M.Maker.fill_key(dict(base, yes_price=40), 0),
                            M.Maker.fill_key(dict(base, yes_price=41), 0))

    def test_a_real_trade_id_still_wins_and_is_order_independent(self):
        """The synthetic form is a FALLBACK.  A real trade_id is stable under reordering,
        which is what keeps the crash-loop dedup correct in the normal case."""
        f = {"trade_id": "abc-123", "order_id": "O1", "count": 5}
        self.assertEqual(M.Maker.fill_key(f, 0), "abc-123")
        self.assertEqual(M.Maker.fill_key(f, 7), "abc-123")
        self.assertEqual(M.Maker.fill_key({"fill_id": "z"}, 0), "z")
        # dedup still holds on the real key across any number of restarts
        row = {"k": "fill_obs", "t": 1010.0, "order_id": None, "fill_id": "abc-123",
               "ticker": T, "side": "bid", "count": 5, "price_c": 40,
               "src": "fills_api", "why": "crash_gap"}
        st = M.ledger_replay([row, dict(row, t=1035.0), dict(row, t=1060.0)])
        self.assertAlmostEqual(st.filled(T, "bid"), 5.0)


class NEW4_RankTableIsRefreshedByTheOneHzLoop(unittest.TestCase):

    BOOK_OK = {"orderbook": {"orderbook_fp": {
        "yes_dollars": [["0.4000", "600"], ["0.3900", "600"]],
        "no_dollars": [["0.5900", "600"], ["0.5800", "600"]]}}}
    BOOK_PINNED = {"orderbook": {"orderbook_fp": {
        "yes_dollars": [["0.9900", "5000"]], "no_dollars": []}}}

    def prog(self):
        return {"program_id": "P1", "market_ticker": "M1", "series": "KX",
                "period_reward": 1000000.0, "target_size_fp": 1000.0,
                "discount_factor_bps": 5000.0, "start_ts": 0.0, "end_ts": 16 * 3600.0,
                "paid_out": False}

    def test_classify_market_returns_slots_and_updates_the_rank_table(self):
        """NEW-4 — the 1 Hz loop updated books/scores but never `classified`, so the rank
        table went up to CLASSIFY_REFRESH_S (900s) stale for markets polled every second."""
        m = M.Maker(None, M.LedgerState(), [])
        slots, info = m.classify_market(self.prog(), self.BOOK_OK, 100.0)
        self.assertEqual(len(slots), 2)
        self.assertIn("M1", m.classified)
        self.assertIn("M1", m.scores)
        self.assertIn("M1", m.books)
        self.assertFalse(m.classified["M1"]["pinned"])
        self.assertGreater(M.market_rank_value(m.classified["M1"]), 0.0)
        self.assertEqual(M.market_poll_rank(m.classified), ["M1"])

    def test_a_rung_that_flips_pinned_loses_its_slot_on_the_next_poll(self):
        m = M.Maker(None, M.LedgerState(), [])
        m.classify_market(self.prog(), self.BOOK_OK, 100.0)
        self.assertEqual(M.market_poll_rank(m.classified), ["M1"])
        m.classify_market(self.prog(), self.BOOK_PINNED, 101.0)   # one second later
        self.assertTrue(m.classified["M1"]["pinned"])
        self.assertEqual(M.market_poll_rank(m.classified), [])    # slot released at once
        self.assertEqual(m.classified["M1"]["ts"], 101.0)

    def test_denied_and_frozen_markets_are_carried_into_the_table(self):
        st = M.LedgerState()
        st.poisoned.add("M1")
        m = M.Maker(None, st, [])
        m.classify_market(self.prog(), self.BOOK_OK, 100.0, denied=True)
        self.assertTrue(m.classified["M1"]["denied"])
        self.assertEqual(M.market_poll_rank(m.classified), [])


class NEW5_TakerExitIsCoupledToTheCeiling(unittest.TestCase):

    def _assert_with(self, ceiling, enabled, programs):
        tmp = tempfile.mkdtemp()
        old = (M.DATA_DIR, M.LEDGER_PATH, M.MAX_TOTAL_COLLATERAL_USD, M.TAKER_EXIT_ENABLED)
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            M.MAX_TOTAL_COLLATERAL_USD = ceiling
            M.TAKER_EXIT_ENABLED = enabled
            ok, results = M.startup_assertions(None, "n/a", programs=programs)
            return ok, {n: (g, d) for n, g, d in results}
        finally:
            (M.DATA_DIR, M.LEDGER_PATH, M.MAX_TOTAL_COLLATERAL_USD,
             M.TAKER_EXIT_ENABLED) = old
            shutil.rmtree(tmp, ignore_errors=True)

    GOOD_PROGS = unit_progs(40, gas=17)

    def test_first_run_ceiling_passes_with_the_taker_exit_off(self):
        ok, r = self._assert_with(45.0, False, self.GOOD_PROGS)
        self.assertTrue(r["taker_exit_decision_matches_ceiling"][0])
        self.assertTrue(ok)

    def test_the_300_dollar_rung_REFUSES_TO_RUN_until_the_decision_is_made(self):
        """NEW-5 — nothing coupled TAKER_EXIT_ENABLED to the ceiling, so a ceiling bump
        would silently inherit a decision derived for a $45 sleeve.  Refuse, do NOT
        auto-enable: crossing the spread is a human decision, taken AT the rung."""
        ok, r = self._assert_with(300.0, False, self.GOOD_PROGS)
        self.assertFalse(r["taker_exit_decision_matches_ceiling"][0])
        self.assertFalse(ok)
        self.assertIn("300", r["taker_exit_decision_matches_ceiling"][1])

    def test_the_300_dollar_rung_runs_once_the_decision_is_explicit(self):
        ok, r = self._assert_with(300.0, True, self.GOOD_PROGS)
        self.assertTrue(r["taker_exit_decision_matches_ceiling"][0])
        self.assertTrue(ok)

    def test_the_threshold_is_the_next_ladder_rung(self):
        self.assertEqual(M.TAKER_EXIT_REQUIRED_ABOVE_USD, 300.0)
        self.assertLess(M.MAX_TOTAL_COLLATERAL_USD, M.TAKER_EXIT_REQUIRED_ABOVE_USD)


# =============================================================================================
# LIVE-RUN REGRESSIONS (FIX-A, FIX-B) — observed in v4's first hour at a saturated $45 ceiling
# =============================================================================================
class FIXA_ClosingOrdersDoNotChargeCollateral(unittest.TestCase):

    def test_the_live_deadlock_shed_can_post_at_a_saturated_ceiling(self):
        """FIX-A — observed live: 19.95 NO held on KXDXYDUD, recycler logging
        MakerShed/shed_preferred every second, and the shed order skipped on
        `collateral_ceiling` every second.  The shed could never post, so the inventory
        locked until settlement — the §5.3 "inventory BLOCKS THE SLOT" failure arriving
        through our own risk control."""
        st = M.LedgerState()
        st.positions = {"D": {"yes": 0.0, "no": 19.95}}     # long NO
        st.position_cost = {"D": M.MAX_TOTAL_COLLATERAL_USD - 2.13}   # ceiling saturated
        m = M.Maker(None, st, [])
        self.assertGreater(st.collateral, M.MAX_TOTAL_COLLATERAL_USD - 3.0)
        # shedding a NO position means BUYING YES -> a bid, closing up to 19.95
        self.assertEqual(M.shed_slot("ask"), "bid")
        self.assertAlmostEqual(M.closing_qty("bid", 19, -19.95), 19.0, places=9)
        self.assertAlmostEqual(M.order_collateral_usd("bid", 0.42, 19, -19.95), 0.0,
                               places=9)
        # ... so it is NOT skipped any more.  (do_post is stubbed; no network.)
        posted = {}
        m.do_post = lambda body: (posted.setdefault("body", body),
                                  (201, {"order_id": "O1", "fill_count": "0.00",
                                         "remaining_count": body["count"]}))[1]
        old_dry = M.DRY
        tmp = tempfile.mkdtemp()
        old = (M.DATA_DIR, M.LEDGER_PATH)
        try:
            M.DRY = False
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            o = m.place("D", "bid", 42, 19, 1785000000)
            self.assertIsNotNone(o)                          # was None before FIX-A
            self.assertIsNone(m.last_place_skip)
        finally:
            M.DRY = old_dry
            M.DATA_DIR, M.LEDGER_PATH = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_partial_case_charges_only_the_opening_tail(self):
        """FIX-A — the case that bites: a 25-lot ask against 19.95 held is 19.95 closing
        plus 5.05 opening, and only the 5.05 tail may be charged."""
        self.assertAlmostEqual(M.closing_qty("ask", 25, 19.95), 19.95, places=9)
        charged = M.order_collateral_usd("ask", 0.42, 25, 19.95)
        self.assertAlmostEqual(charged, 5.05 * M.unit_collateral("ask", 0.42), places=9)
        self.assertAlmostEqual(charged, 5.05 * 0.58, places=9)
        gross = 25 * M.unit_collateral("ask", 0.42)
        self.assertLess(charged, gross)
        self.assertAlmostEqual(gross - charged, 19.95 * 0.58, places=9)

    def test_a_non_closing_order_still_charges_in_full(self):
        """The netting must not become a hole in the §8.3 ceiling."""
        self.assertAlmostEqual(M.order_collateral_usd("bid", 0.42, 25, 0.0),
                               25 * 0.42, places=9)
        # an order on the SAME side as the position OPENS more of it
        self.assertEqual(M.closing_qty("bid", 25, 19.95), 0.0)
        self.assertAlmostEqual(M.order_collateral_usd("bid", 0.42, 25, 19.95),
                               25 * 0.42, places=9)
        self.assertEqual(M.closing_qty("ask", 25, -19.95), 0.0)
        self.assertAlmostEqual(M.order_collateral_usd("ask", 0.42, 25, -19.95),
                               25 * 0.58, places=9)
        # and a flat book charges everything, both sides
        for side, px in (("bid", 0.40), ("ask", 0.40)):
            self.assertAlmostEqual(M.order_collateral_usd(side, px, 10, 0.0),
                                   10 * M.unit_collateral(side, px), places=9)

    def test_a_new_post_at_a_saturated_ceiling_still_skips(self):
        st = M.LedgerState()
        st.positions = {"D": {"yes": 0.0, "no": 19.95}}
        st.position_cost = {"D": M.MAX_TOTAL_COLLATERAL_USD - 2.13}
        m = M.Maker(None, st, [])
        tmp = tempfile.mkdtemp()
        old = (M.DATA_DIR, M.LEDGER_PATH)
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            self.assertIsNone(m.place("OTHER", "bid", 40, 25, 1785000000))
            self.assertEqual(m.last_place_skip, "collateral_ceiling")
        finally:
            M.DATA_DIR, M.LEDGER_PATH = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_resting_collateral_nets_the_same_way_placement_does(self):
        """If placement nets a closing order to $0 but the resting view then charges it in
        full, the ceiling re-seals one tick later and the deadlock returns."""
        recs = [rec_place("O1", T, "bid", 0.40, 20, fill=20, rem=0),      # 20 YES held
                rec_place("O2", T, "ask", 0.42, 20)]                      # closing ask
        st = M.ledger_replay(recs)
        self.assertAlmostEqual(st.net_position(T), 20.0, places=9)
        self.assertAlmostEqual(st.resting_collateral, 0.0, places=9)      # fully closing
        self.assertAlmostEqual(st.position_collateral, 20 * 0.40, places=9)
        # a partially-closing rest charges only its tail
        recs2 = [rec_place("O1", T, "bid", 0.40, 20, fill=20, rem=0),
                 rec_place("O2", T, "ask", 0.42, 25)]
        st2 = M.ledger_replay(recs2)
        self.assertAlmostEqual(st2.resting_collateral, 5 * 0.58, places=9)
        # and a non-closing rest is untouched
        recs3 = [rec_place("O1", T, "bid", 0.40, 20, fill=20, rem=0),
                 rec_place("O2", T, "bid", 0.40, 10)]
        self.assertAlmostEqual(M.ledger_replay(recs3).resting_collateral, 10 * 0.40,
                               places=9)

    def test_closing_capacity_is_shared_deterministically_across_resting_orders(self):
        """Two closing asks against one position must not BOTH net to zero."""
        recs = [rec_place("O1", T, "bid", 0.40, 20, fill=20, rem=0),
                rec_place("O2", T, "ask", 0.42, 15),
                rec_place("O3", T, "ask", 0.42, 15)]
        st = M.ledger_replay(recs)
        # 20 of the 30 resting asks close; the other 10 open at the NO price
        self.assertAlmostEqual(st.resting_collateral, 10 * 0.58, places=9)
        self.assertEqual(st.resting_collateral, M.ledger_replay(recs).resting_collateral)


class FIXB_CeilingBlockedRequotesDegradeInsteadOfFreezing(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # EVERY writable path is redirected, not just the ledger.  RECON_PATH and
        # OPERATOR_PATH are module constants frozen at import from DATA_DIR, so patching
        # DATA_DIR alone leaves them pointing at the OPERATOR'S REAL FILES -- the same class
        # of leak as the ntfy incident, and one that would have appended test rows to a live
        # reconciliation ledger on the VPS.
        self.old = (M.DATA_DIR, M.LEDGER_PATH, M.RECON_PATH, M.OPERATOR_PATH, M.SEQ_PATH,
                    M.DRY)
        M.DATA_DIR = os.path.join(self.tmp, "lip")
        M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
        M.RECON_PATH = os.path.join(M.DATA_DIR, "recon.jsonl")
        M.OPERATOR_PATH = os.path.join(M.DATA_DIR, "pools_operator.jsonl")
        M.SEQ_PATH = os.path.join(M.DATA_DIR, "v4_coid_seq")
        M.DRY = False

    def tearDown(self):
        (M.DATA_DIR, M.LEDGER_PATH, M.RECON_PATH, M.OPERATOR_PATH, M.SEQ_PATH,
         M.DRY) = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _saturated_maker(self, cost=None, headroom=4.30):
        """`headroom` is dollars left under the CEILING before this slot's own resting
        order — derived from the live constant so a ceiling change can never silently
        un-saturate these fixtures (it did: the 45->65 hot edit broke eight of them)."""
        if cost is None:
            cost = M.MAX_TOTAL_COLLATERAL_USD - headroom
        # `cost` is everything OTHER than this slot's own resting order.  At 40.70 plus a
        # $4.00 resting bid the book is saturated at $44.70 of $45: the make-before-break
        # OVERLAP (+$4.10) does not fit, but the cancel-first repost does — which is the
        # whole point of FIX-B.
        st = M.LedgerState()
        st.positions = {"T": {"yes": 0.0, "no": 0.0}}
        st.position_cost = {"T": cost}
        m = M.Maker(None, st, [])
        self.calls = []
        m.do_cancel = lambda oid: (self.calls.append(("cancel", oid)),
                                   (200, {"reduced_by": "10.00"}))[1]

        def post(body):
            self.calls.append(("post", body["price"], body["count"]))
            return (201, {"order_id": "N%d" % len(self.calls),
                          "fill_count": "0.00", "remaining_count": body["count"]})
        m.do_post = post
        return m

    def test_a_moved_book_still_gets_a_recentred_quote_via_cancel_first(self):
        """FIX-B — the live freeze: skip_post on collateral_ceiling treated a re-centre like
        a new post, so the quote sat off-best and decayed at 0.5^ticks."""
        m = self._saturated_maker()
        resting = M.OrderState("OLD", "v4-c", "T", "bid", 0.40, 10, 0.0, 10.0)
        m.st.orders["OLD"] = resting
        m.live_by_slot[("T", "bid")] = resting
        # the overlap does not fit: 10 more at 41c on top of a saturated ceiling
        self.assertGreater(m.st.collateral + 10 * 0.41, M.MAX_TOTAL_COLLATERAL_USD)
        new = m.requote("T", "bid", 41, 10, 1785000000)
        self.assertIsNotNone(new)                       # was None before FIX-B
        self.assertEqual(new.price, 0.41)               # and it is re-centred
        # Cancel-first: the cancel precedes the post.  The make leg never reaches the wire
        # at all -- the ceiling check declines it locally -- so no request is wasted.
        self.assertEqual([c[0] for c in self.calls], ["cancel", "post"])
        self.assertEqual(m.live_by_slot[("T", "bid")].order_id, new.order_id)
        # a ceiling block must NOT latch the slot into cancel-first: it is transient
        self.assertNotIn(("T", "bid"), m.mbb_degraded)

    def test_the_ceiling_case_is_logged_distinctly_from_a_balance_reject(self):
        m = self._saturated_maker()
        resting = M.OrderState("OLD", "v4-c", "T", "bid", 0.40, 10, 0.0, 10.0)
        m.st.orders["OLD"] = resting
        m.live_by_slot[("T", "bid")] = resting
        m.requote("T", "bid", 41, 10, 1785000000)
        events = [json.loads(l)["event"]
                  for l in open(M.LEDGER_PATH)] if os.path.exists(M.LEDGER_PATH) else []
        self.assertIn("mbb_degraded_ceiling", events)
        self.assertNotIn("mbb_degraded", events)

    def test_an_upsized_requote_is_not_collateral_neutral_and_still_degrades(self):
        """The neutrality argument only holds for equal-or-smaller size."""
        m = self._saturated_maker()
        resting = M.OrderState("OLD", "v4-c", "T", "bid", 0.40, 10, 0.0, 10.0)
        m.st.orders["OLD"] = resting
        m.live_by_slot[("T", "bid")] = resting
        m.requote("T", "bid", 41, 40, 1785000000)
        self.assertIn(("T", "bid"), m.mbb_degraded)
        events = [json.loads(l)["event"] for l in open(M.LEDGER_PATH)]
        self.assertIn("mbb_degraded", events)

    def test_a_genuinely_new_post_at_saturation_still_skips(self):
        """No resting order means no overlap, nothing to cancel and nothing to degrade —
        the ceiling must simply hold."""
        m = self._saturated_maker(headroom=0.40)  # nothing resting, no headroom at all
        self.assertIsNone(m.requote("T", "bid", 41, 10, 1785000000))
        self.assertEqual(self.calls, [])
        self.assertNotIn(("T", "bid"), m.mbb_degraded)
        self.assertEqual(m.last_place_skip, "collateral_ceiling")

    def test_make_before_break_is_still_the_default_when_headroom_exists(self):
        """§4.1 must be untouched on an unsaturated book: post, confirm, THEN cancel."""
        st = M.LedgerState()
        m = M.Maker(None, st, [])
        self.calls = []
        m.do_cancel = lambda oid: (self.calls.append(("cancel", oid)),
                                   (200, {"reduced_by": "10.00"}))[1]
        m.do_post = lambda body: (self.calls.append(("post", body["count"])),
                                  (201, {"order_id": "N1", "fill_count": "0.00",
                                         "remaining_count": body["count"]}))[1]
        resting = M.OrderState("OLD", "v4-c", "T", "bid", 0.40, 10, 0.0, 10.0)
        m.st.orders["OLD"] = resting
        m.live_by_slot[("T", "bid")] = resting
        new = m.requote("T", "bid", 41, 10, 1785000000)
        self.assertIsNotNone(new)
        self.assertEqual([c[0] for c in self.calls], ["post", "cancel"])   # make, THEN break
        self.assertNotIn(("T", "bid"), m.mbb_degraded)


class FIXA1_ClosingRoomIsConsumedByRestingOrders(unittest.TestCase):
    """FIX-A-1 — place() netted against the RAW net position, ignoring closing capacity
    that resting orders had already taken."""

    def _state(self, net_yes=20.0, cost=8.0):
        st = M.LedgerState()
        st.positions = {"T": {"yes": max(0.0, net_yes), "no": max(0.0, -net_yes)}}
        st.position_cost = {"T": cost}
        return st

    def test_the_verifiers_measured_walkthrough(self):
        """20 YES held + one 20-lot closing ask resting ($0, correct).  A SECOND 20-lot ask
        also priced at $0 and the ceiling approved it; once both rested,
        resting_collateral jumped by an amount the check had priced at zero."""
        st = self._state()
        # first closer: correctly free, and it consumes the whole room
        self.assertAlmostEqual(st.closing_room("T", "ask"), 20.0, places=9)
        self.assertAlmostEqual(
            M.order_collateral_usd("ask", 0.42, 20, room=st.closing_room("T", "ask")),
            0.0, places=9)
        st.orders["O1"] = M.OrderState("O1", "c", "T", "ask", 0.42, 20, 0.0, 20.0)
        self.assertAlmostEqual(st.resting_collateral, 0.0, places=9)
        # SECOND closer: the room is gone, so it prices its whole size as opening
        self.assertAlmostEqual(st.closing_room("T", "ask"), 0.0, places=9)
        second = M.order_collateral_usd("ask", 0.42, 20, room=st.closing_room("T", "ask"))
        self.assertAlmostEqual(second, 20 * 0.58, places=9)
        self.assertGreater(second, 0.0)                     # was $0.00 -- the bug
        # and that is EXACTLY what it costs once it rests: check and view now agree
        before = st.resting_collateral
        st.orders["O2"] = M.OrderState("O2", "c", "T", "ask", 0.42, 20, 0.0, 20.0)
        self.assertAlmostEqual(st.resting_collateral - before, second, places=9)
        # the raw-net reading, preserved so the regression is unmistakable
        self.assertAlmostEqual(M.order_collateral_usd("ask", 0.42, 20, 20.0), 0.0, places=9)

    def test_the_check_and_the_resting_view_agree_for_every_shape(self):
        """The invariant that closes this class: what place() charges for an order is
        EXACTLY what resting_collateral charges once that order rests."""
        for net in (20.0, -20.0, 0.0, 5.0):
            for side in ("bid", "ask"):
                for size in (1, 5, 20, 25, 40):
                    st = self._state(net_yes=net)
                    st.orders["A"] = M.OrderState("A", "c", "T", side, 0.42, 7, 0.0, 7.0)
                    before = st.resting_collateral
                    quoted = M.order_collateral_usd(
                        side, 0.42, size, room=st.closing_room("T", side))
                    st.orders["B"] = M.OrderState("B", "c", "T", side, 0.42, size, 0.0,
                                                  float(size))
                    self.assertAlmostEqual(st.resting_collateral - before, quoted,
                                           places=9, msg=(net, side, size))

    def test_make_before_break_overlap_is_charged_and_can_degrade(self):
        """The shape that made this reachable: MBB puts two orders on one side, and while
        closing orders priced at $0 the FIX-B ceiling guard never fired for them -- so the
        ceiling was BREACHED rather than degraded.  Now the overlap is priced, the guard
        fires, and the requote takes the collateral-neutral cancel-first path."""
        tmp = tempfile.mkdtemp()
        old = (M.DATA_DIR, M.LEDGER_PATH, M.DRY)
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            M.DRY = False
            st = self._state(net_yes=20.0, cost=M.MAX_TOTAL_COLLATERAL_USD - 5.00)
            resting = M.OrderState("OLD", "v4-c", "T", "ask", 0.42, 20, 0.0, 20.0)
            st.orders["OLD"] = resting
            m = M.Maker(None, st, [])
            m.live_by_slot[("T", "ask")] = resting
            calls = []
            m.do_cancel = lambda oid: (calls.append(("cancel", oid)),
                                       (200, {"reduced_by": "20.00"}))[1]
            m.do_post = lambda body: (calls.append(("post", body["count"])),
                                      (201, {"order_id": "N1", "fill_count": "0.00",
                                             "remaining_count": body["count"]}))[1]
            self.assertAlmostEqual(st.resting_collateral, 0.0, places=9)   # closer is free
            new = m.requote("T", "ask", 43, 20, 1785000000)
            # the overlap is now priced at its full tail ($11.60) and does not fit under $45
            self.assertEqual([c[0] for c in calls], ["cancel", "post"])
            self.assertIsNotNone(new)
            events = [json.loads(l)["event"] for l in open(M.LEDGER_PATH)]
            self.assertIn("mbb_degraded_ceiling", events)   # degraded, NOT breached
            self.assertNotIn(("T", "ask"), m.mbb_degraded)
            self.assertLessEqual(st.collateral, M.MAX_TOTAL_COLLATERAL_USD + 1e-9)
        finally:
            M.DATA_DIR, M.LEDGER_PATH, M.DRY = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_room_is_shared_across_sides_and_survives_replay(self):
        st = self._state(net_yes=20.0)
        st.orders["O1"] = M.OrderState("O1", "c", "T", "ask", 0.42, 12, 0.0, 12.0)
        self.assertAlmostEqual(st.closing_room("T", "ask"), 8.0, places=9)
        self.assertAlmostEqual(st.closing_room("T", "bid"), 0.0, places=9)  # long YES
        self.assertAlmostEqual(
            M.order_collateral_usd("ask", 0.42, 20, room=st.closing_room("T", "ask")),
            12 * 0.58, places=9)
        # allocate_closing_room is deterministic, so a replay reproduces the split exactly
        orders = list(st.orders.values())
        self.assertEqual(M.allocate_closing_room(orders, 20.0),
                         M.allocate_closing_room(list(reversed(orders)), 20.0))


class RunwayGuardAndProgramEndRelease(unittest.TestCase):
    """LIVE DEFECT: v4 allocated 735 lots on gas 4.120 and 50 on 4.110 with under 25 minutes
    left in that program's window.  ALLOCATE optimises a RATE and is blind to how long that
    rate can be earned."""

    def test_min_runway_is_derived_from_the_entry_floor_not_hardcoded(self):
        # h >= ENTRY_FLOOR / (share * rho/2);  at rho=6.25, floor=$2, share=0.5 -> 1.28h
        self.assertAlmostEqual(M.min_runway_h(6.25), 1.28, places=6)
        self.assertAlmostEqual(
            M.min_runway_h(6.25),
            M.ENTRY_FLOOR_USD / (M.ENTRY_SHARE_ASSUMPTION * 6.25 / 2.0), places=12)
        self.assertEqual(M.ENTRY_SHARE_ASSUMPTION, 0.5)      # conservative, NOT 1.0
        # it scales with the pool: a fatter program needs less runway, a thinner one more
        self.assertLess(M.min_runway_h(62.5), M.min_runway_h(6.25))
        self.assertGreater(M.min_runway_h(0.625), M.min_runway_h(6.25))
        self.assertEqual(M.min_runway_h(0.0), float("inf"))
        # assuming the sole-qualifier share of 1.0 is exactly the optimism that produced the
        # live late entries -- it would have halved the required runway
        self.assertAlmostEqual(M.min_runway_h(6.25, share=1.0), 0.64, places=6)

    def test_entry_is_refused_under_min_runway(self):
        """The live shape: 25 minutes left on a gas rung, nothing accrued."""
        self.assertFalse(M.runway_ok(6.25, 25.0 / 60.0, 0.0))
        s = M.Slot("KXAAAGASD-26JUL28-4.120", "bid", 6.25, 50.0, 0.02,
                   hours_left=25.0 / 60.0)
        al, spent = M.allocate([s], 45.0, BIG)
        self.assertEqual(al[s.key], 0)                        # was 735 lots live
        self.assertEqual(spent, 0.0)
        # and the budget is genuinely released, not merely withheld from this slot
        good = M.Slot("GOOD", "bid", 6.25, 50.0, 0.02, hours_left=8.0)
        al2, spent2 = M.allocate([s, good], 45.0, BIG)
        self.assertEqual(al2[s.key], 0)
        self.assertGreater(al2[good.key], 0)
        self.assertGreater(spent2, 0.0)

    def test_entry_is_allowed_with_runway(self):
        self.assertTrue(M.runway_ok(6.25, 1.28, 0.0))
        self.assertTrue(M.runway_ok(6.25, 8.0, 0.0))
        s = M.Slot("T", "bid", 6.25, 50.0, 0.02, hours_left=2.0)
        self.assertGreater(M.allocate([s], 45.0, BIG)[0][s.key], 0)

    def test_rescue_top_up_is_still_allowed_above_the_accrued_threshold(self):
        """§3.6 -- accrued score is not sunk, it is CONDITIONAL on clearing $1.00, which is
        why topping up a nearly-paid program late beats redeploy.  The guard must not
        override the rescue path."""
        self.assertFalse(M.runway_ok(6.25, 0.3, 0.0))
        self.assertTrue(M.runway_ok(6.25, 0.3, M.RESCUE_TARGET_USD))
        self.assertTrue(M.runway_ok(6.25, 0.01, 5.00))
        self.assertFalse(M.runway_ok(6.25, 0.3, M.RESCUE_TARGET_USD - 0.01))
        late = M.Slot("T", "bid", 6.25, 50.0, 0.02, hours_left=0.3,
                      accrued=M.RESCUE_TARGET_USD)
        self.assertGreater(M.allocate([late], 45.0, BIG)[0][late.key], 0)
        cold = M.Slot("T", "bid", 6.25, 50.0, 0.02, hours_left=0.3, accrued=0.0)
        self.assertEqual(M.allocate([cold], 45.0, BIG)[0][cold.key], 0)

    def test_slots_from_market_carries_the_real_remaining_window(self):
        prog = {"program_id": "P1", "market_ticker": "T", "series": "KX",
                "period_reward": 1000000.0, "target_size_fp": 10.0,
                "discount_factor_bps": 5000.0, "start_ts": 0.0, "end_ts": 16 * 3600.0,
                "paid_out": False}
        # thin enough that the §2.2 hurdle is cleared, so the only thing under test here is
        # the runway
        book = {"orderbook": {"orderbook_fp": {
            "yes_dollars": [["0.4000", "100"], ["0.3900", "100"]],
            "no_dollars": [["0.5900", "100"], ["0.5800", "100"]]}}}
        late, _ = M.slots_from_market(prog, book, 16 * 3600.0 - 1500.0)   # 25 min left
        for s in late:
            self.assertAlmostEqual(s.hours_left, 1500.0 / 3600.0, places=6)
        self.assertEqual(sum(M.allocate(late, 45.0, BIG)[0].values()), 0)
        early, _ = M.slots_from_market(prog, book, 0.0)
        self.assertAlmostEqual(early[0].hours_left, 16.0, places=6)
        self.assertGreater(sum(M.allocate(early, 45.0, BIG)[0].values()), 0)

    # ---- program-end release ---------------------------------------------------------
    def _prog(self, pid="P1", ticker="T", end=1000.0):
        return {"program_id": pid, "market_ticker": ticker, "series": "KX",
                "period_reward": 1000000.0, "target_size_fp": 1000.0,
                "discount_factor_bps": 5000.0, "start_ts": 0.0, "end_ts": end,
                "paid_out": False}

    def test_ended_programs_leave_the_allocation(self):
        """Confirming the allocator already drops them: cycle() filters on end_ts > now, so
        no slots are built and nothing is allocated."""
        prog = self._prog(end=1000.0)
        m = M.Maker(None, M.LedgerState(), [prog])
        live = [p for p in m.programs.values() if p["end_ts"] > 1001.0]
        self.assertEqual(live, [])

    def test_window_end_cancels_non_closing_orders_but_keeps_closing_ones(self):
        """Inventory OUTLIVES the program that produced it -- a shed unwinding a position
        must survive the window end, or the inventory strands until settlement."""
        tmp = tempfile.mkdtemp()
        old = (M.DATA_DIR, M.LEDGER_PATH, M.DRY)
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            M.DRY = False
            st = M.LedgerState()
            st.positions = {"T": {"yes": 20.0, "no": 0.0}}       # long YES
            st.position_cost = {"T": 8.0}
            quote = M.OrderState("A_QUOTE", "v4-c", "T", "bid", 0.40, 10, 0.0, 10.0)
            shed = M.OrderState("B_SHED", "v4-c", "T", "ask", 0.42, 20, 0.0, 20.0)
            st.orders["A_QUOTE"] = quote
            st.orders["B_SHED"] = shed
            m = M.Maker(None, st, [self._prog(end=1000.0)])
            m.live_by_slot[("T", "bid")] = quote
            m.live_by_slot[("T", "ask")] = shed
            m.classified["T"] = {"rho": 6.25, "pinned": False, "denied": False,
                                 "sides": [{"S": 50.0, "p": 0.40, "qualifies": True}]}
            cancelled = []
            m.do_cancel = lambda oid: (cancelled.append(oid),
                                       (200, {"reduced_by": "10.00"}))[1]
            m.release_out_of_window(1001.0)
            self.assertEqual(cancelled, ["A_QUOTE"])            # the earning quote goes
            self.assertEqual(shed.state, M.ST_LIVE)             # the shed stays
            self.assertNotIn(("T", "bid"), m.live_by_slot)
            self.assertIn(("T", "ask"), m.live_by_slot)
            self.assertNotIn("T", m.classified)                 # leaves the poll ranking
            # idempotent: a second pass does nothing
            m.release_out_of_window(1002.0)
            self.assertEqual(cancelled, ["A_QUOTE"])
        finally:
            M.DATA_DIR, M.LEDGER_PATH, M.DRY = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_release_does_not_fire_while_another_program_on_that_market_is_live(self):
        tmp = tempfile.mkdtemp()
        old = (M.DATA_DIR, M.LEDGER_PATH, M.DRY)
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            M.DRY = False
            st = M.LedgerState()
            quote = M.OrderState("A", "v4-c", "T", "bid", 0.40, 10, 0.0, 10.0)
            st.orders["A"] = quote
            m = M.Maker(None, st, [self._prog("P1", "T", 1000.0),
                                   self._prog("P2", "T", 9e9)])
            m.live_by_slot[("T", "bid")] = quote
            cancelled = []
            m.do_cancel = lambda oid: (cancelled.append(oid), (200, {"reduced_by": "0"}))[1]
            m.release_out_of_window(1001.0)
            self.assertEqual(cancelled, [])
            self.assertIn(("T", "bid"), m.live_by_slot)
        finally:
            M.DATA_DIR, M.LEDGER_PATH, M.DRY = old
            shutil.rmtree(tmp, ignore_errors=True)


class C1_NetInventoryDollarCapIsRestingAware(unittest.TestCase):
    """C1/C8/C9 -- the DXY root cause.  place()'s old check bounded `net + size` in CONTRACTS
    against the CURRENT price and was blind to orders already RESTING on the same side.
    Make-before-break puts two orders on one side by construction, so both passed
    independently and both filled: 2x the cap, reproduced at 58 modelled vs 59 observed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = (M.DATA_DIR, M.LEDGER_PATH, M.DRY)
        M.DATA_DIR = os.path.join(self.tmp, "lip")
        M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
        M.DRY = False

    def tearDown(self):
        M.DATA_DIR, M.LEDGER_PATH, M.DRY = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _maker(self):
        m = M.Maker(None, M.LedgerState(), [])
        self.n = 0

        def post(body):
            self.n += 1
            return (201, {"order_id": "N%d" % self.n, "fill_count": "0.00",
                          "remaining_count": body["count"]})
        m.do_post = post
        m.do_cancel = lambda oid: (200, {"reduced_by": "0.00"})
        return m

    def _fill(self, m, order):
        """The order fills in full, exactly as a cancel returning reduced_by 0 reports."""
        order.reduced_by = 0.0
        order.state = M.ST_CLOSED
        m.st._credit_fill(order, order.remaining_count)

    def test_COMPOSITION_mbb_overlap_both_fill_then_refill_hits_the_cap(self):
        """The full sequence, not a pure function: post, MBB-overlap post, both fill, then
        try to refill against the cap."""
        m = self._maker()
        cap_contracts = int(M.INV_CAP_USD / 0.34)              # 29 at a $0.34 basis
        # 1. first order rests
        o1 = m.place("DXY", "bid", 34, cap_contracts, 1785000000)
        self.assertIsNotNone(o1)
        # 2. MAKE-BEFORE-BREAK overlap: a second order on the SAME side, o1 still resting.
        #    The old check passed this because it only looked at `net + size` with net = 0.
        o2 = m.place("DXY", "bid", 34, cap_contracts, 1785000000)
        self.assertIsNone(o2, "resting-blind cap let the MBB overlap through -> 2x the cap")
        self.assertEqual(m.last_place_skip, "net_inventory_cap")
        # 3. o1 fills in full; exposure is now real inventory rather than resting risk
        self._fill(m, o1)
        self.assertAlmostEqual(m.st.net_position("DXY"), cap_contracts, places=6)
        self.assertAlmostEqual(m.st.entry_basis("DXY", "yes"), 0.34, places=6)
        # 4. refill attempt on the same side is refused: we are already at the dollar cap
        o3 = m.place("DXY", "bid", 34, 5, 1785000000)
        self.assertIsNone(o3)
        self.assertEqual(m.last_place_skip, "net_inventory_cap")
        # and the invariant holds on the realised book
        self.assertLessEqual(m.st.net_exposure_usd("DXY", "bid", 0), M.INV_CAP_USD + 1e-9)
        self.assertLessEqual(abs(m.st.net_position("DXY")) * 0.34, M.INV_CAP_USD + 1e-9)

    def test_COMPOSITION_two_resting_orders_can_never_jointly_breach(self):
        """The invariant, swept: whatever sequence of same-side posts is ACCEPTED, the total
        that could fill never exceeds the cap."""
        for size in (5, 10, 15, 29):
            m = self._maker()
            accepted = []
            for _ in range(6):
                o = m.place("DXY", "bid", 34, size, 1785000000)
                if o is None:
                    break
                accepted.append(o)
            worst = sum(o.remaining_count for o in accepted) * 0.34
            self.assertLessEqual(worst, M.INV_CAP_USD + 1e-9, msg=size)

    def test_C9_the_cap_is_dollars_against_entry_basis_not_count_at_current_price(self):
        """A contract-count cap of floor($10/p) re-permits at p=$0.20 against inventory
        bought at $0.34 -- which is how a $10 cap silently becomes a $17 one."""
        m = self._maker()
        o1 = m.place("DXY", "bid", 34, 29, 1785000000)
        self._fill(m, o1)
        self.assertEqual(M.n_cap(0.20), 50)                    # the old count cap re-permits
        self.assertGreater(50, 29)
        o2 = m.place("DXY", "bid", 20, 10, 1785000000)         # price fell to 20c
        self.assertIsNone(o2, "count-at-current-price cap would have re-permitted here")
        self.assertAlmostEqual(m.st.entry_basis("DXY", "yes"), 0.34, places=6)

    def test_C8_a_shed_can_never_flip_the_position_sign(self):
        """A 40-lot shed against 20 held would take -20 to +20: not a shed, a fresh opposite
        position wearing a shed's name."""
        m = self._maker()
        m.st.positions["DXY"] = {"yes": 0.0, "no": 20.0}       # net -20
        m.st.position_cost["DXY"] = 20 * 0.34
        m.st.position_cost_leg["DXY"] = {"yes": 0.0, "no": 20 * 0.34}
        self.assertAlmostEqual(m.st.net_position("DXY"), -20.0, places=6)
        m.shed_target[("DXY", "bid")] = 40                     # oversized shed intent
        shed_q = int(min(m.shed_target[("DXY", "bid")],
                         abs(m.st.net_position("DXY"))))
        self.assertEqual(shed_q, 20)                           # clamped at |net|
        self.assertEqual(m.st.net_position("DXY") + shed_q, 0.0)   # cannot cross zero
        # AND -- the reason C8 is not redundant with C1 -- the DOLLAR cap does NOT catch
        # this on its own: ending at +20 at 34c is $6.80 of exposure, genuinely under the
        # $10 cap.  The cap is sign-agnostic by construction, so only the clamp prevents an
        # oversized shed from silently reversing the position.
        self.assertLess(m.st.net_exposure_usd("DXY", "bid", 40, 0.34), M.INV_CAP_USD)
        self.assertAlmostEqual(m.st.net_exposure_usd("DXY", "bid", 40, 0.34), 6.80,
                               places=6)
        # the clamped shed lands exactly flat, which is what a shed is for
        self.assertAlmostEqual(m.st.net_exposure_usd("DXY", "bid", shed_q, 0.34), 0.0,
                               places=6)

    def test_closing_orders_are_still_permitted_at_the_cap(self):
        """The cap must not re-create the FIX-A deadlock: an order that REDUCES |net| is
        never refused by it."""
        m = self._maker()
        m.st.positions["DXY"] = {"yes": 29.0, "no": 0.0}
        m.st.position_cost["DXY"] = 29 * 0.34
        m.st.position_cost_leg["DXY"] = {"yes": 29 * 0.34, "no": 0.0}
        self.assertGreaterEqual(m.st.net_exposure_usd("DXY", "bid", 0), M.INV_CAP_USD - 0.2)
        o = m.place("DXY", "ask", 36, 29, 1785000000)          # fully closing
        self.assertIsNotNone(o)
        self.assertNotEqual(m.last_place_skip, "net_inventory_cap")


class C2_SettlementRelease(unittest.TestCase):
    """C2 -- positions and position_cost had NO writer that decremented.  The ledger
    accumulated forever, replay faithfully rebuilt every ghost (a synthetic 16h tape
    reconstructed $3,612 of position_cost) and the §8.3 ceiling self-sealed on window 2."""

    def test_settle_releases_collateral_and_ceiling_room(self):
        st = M.LedgerState()
        st.positions = {"T": {"yes": 20.0, "no": 0.0}}
        st.position_cost = {"T": 8.0}
        st.position_cost_leg = {"T": {"yes": 8.0, "no": 0.0}}
        self.assertAlmostEqual(st.collateral, 8.0, places=9)
        ry, rn, cost, pnl = M.settlement_release(st.positions["T"], 8.0, "yes")
        self.assertEqual((ry, rn), (20.0, 0.0))
        self.assertAlmostEqual(cost, 8.0, places=9)
        self.assertAlmostEqual(pnl, 12.0, places=9)            # 20 x $1.00 - $8.00
        recs = [{"k": "settlement", "t": 9000.0, "ticker": "T", "result": "yes",
                 "released_yes": 20.0, "released_no": 0.0, "cost_released": 8.0,
                 "realized_pnl": 12.0}]
        st2 = M.ledger_replay([rec_place("O1", "T", "bid", 0.40, 20, fill=20, rem=0)]
                              + recs)
        self.assertAlmostEqual(st2.collateral, 0.0, places=9)   # ceiling room returned
        self.assertEqual(st2.positions["T"], {"yes": 0.0, "no": 0.0})
        self.assertAlmostEqual(st2.realized_pnl, 12.0, places=9)

    def test_replay_across_a_settlement_row_reconstructs_zero(self):
        """The ghost-accumulation failure, end to end: two windows of fills with a
        settlement between them must not carry window 1 into window 2."""
        recs = []
        for i in range(20):
            recs.append(rec_place("A%02d" % i, "T", "bid", 0.40, 10, fill=10, rem=0,
                                  ts=100.0 + i))
        mid = M.ledger_replay(recs)
        self.assertAlmostEqual(mid.position_cost["T"], 200 * 0.40, places=9)
        recs.append({"k": "settlement", "t": 500.0, "ticker": "T", "result": "no",
                     "released_yes": 200.0, "released_no": 0.0, "cost_released": 80.0,
                     "realized_pnl": -80.0})
        after = M.ledger_replay(recs)
        self.assertAlmostEqual(after.position_cost["T"], 0.0, places=9)
        self.assertAlmostEqual(after.collateral, 0.0, places=9)
        self.assertAlmostEqual(after.realized_pnl, -80.0, places=9)
        # window 2 starts from a clean book
        recs.append(rec_place("B1", "T", "bid", 0.40, 10, fill=10, rem=0, ts=600.0))
        w2 = M.ledger_replay(recs)
        self.assertAlmostEqual(w2.position_cost["T"], 10 * 0.40, places=9)

    def test_a_double_settlement_row_is_a_no_op(self):
        base = [rec_place("O1", "T", "bid", 0.40, 20, fill=20, rem=0)]
        row = {"k": "settlement", "t": 9000.0, "ticker": "T", "result": "yes",
               "released_yes": 20.0, "released_no": 0.0, "cost_released": 8.0,
               "realized_pnl": 12.0}
        once = M.ledger_replay(base + [row])
        twice = M.ledger_replay(base + [row, dict(row, t=9100.0)])
        self.assertEqual(once.positions, twice.positions)
        self.assertAlmostEqual(once.collateral, twice.collateral, places=9)
        self.assertAlmostEqual(once.realized_pnl, twice.realized_pnl, places=9)
        self.assertAlmostEqual(twice.realized_pnl, 12.0, places=9)   # counted ONCE

    def test_release_without_an_exchange_result_is_impossible_by_construction(self):
        """The wrong direction -- releasing a position the exchange has not resolved -- has
        no code path: `settlement_release` releases nothing without a yes/no result, and
        `is_settleable` demands BOTH a result and a settleable status."""
        pos = {"yes": 20.0, "no": 0.0}
        for bad in ("", None, "unknown", "void", "YES!", "maybe"):
            self.assertEqual(M.settlement_release(pos, 8.0, bad), (0.0, 0.0, 0.0, 0.0))
        self.assertEqual(M.is_settleable({"market": {"result": "", "status": "settled"}}),
                         (False, None))
        self.assertEqual(M.is_settleable({"market": {"result": "yes", "status": "active"}}),
                         (False, None))
        self.assertEqual(M.is_settleable({"market": {"status": "settled"}}), (False, None))
        self.assertEqual(M.is_settleable({}), (False, None))
        self.assertEqual(M.is_settleable({"market": {"result": "yes",
                                                     "status": "settled"}}), (True, "yes"))

    def test_the_sweep_is_idempotent_and_reads_market_truth_only(self):
        """§8.6 doctrine: the PUBLIC market endpoint, never the portfolio positions index."""
        tmp = tempfile.mkdtemp()
        old = (M.DATA_DIR, M.LEDGER_PATH, M.DRY, M.public_get)
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            M.DRY = False
            calls = []

            def fake_get(path, params=None):
                calls.append(path)
                return 200, {"market": {"result": "yes", "status": "settled"}}
            M.public_get = fake_get
            st = M.LedgerState()
            st.positions = {"T": {"yes": 20.0, "no": 0.0}}
            st.position_cost = {"T": 8.0}
            st.position_cost_leg = {"T": {"yes": 8.0, "no": 0.0}}
            m = M.Maker(None, st, [])
            m.sweep_settlements(1000.0)
            self.assertEqual(calls, ["/markets/T"])            # market truth only
            self.assertEqual(st.positions["T"], {"yes": 0.0, "no": 0.0})
            self.assertAlmostEqual(st.realized_pnl, 12.0, places=9)
            self.assertAlmostEqual(st.collateral, 0.0, places=9)
            m.sweep_settlements(2000.0)                        # nothing held: no re-read
            self.assertEqual(calls, ["/markets/T"])
            self.assertAlmostEqual(st.realized_pnl, 12.0, places=9)
            events = [json.loads(l)["event"] for l in open(M.LEDGER_PATH)]
            self.assertEqual(events.count("settlement"), 1)
        finally:
            M.DATA_DIR, M.LEDGER_PATH, M.DRY, M.public_get = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_an_unsettled_market_is_left_alone(self):
        tmp = tempfile.mkdtemp()
        old = (M.DATA_DIR, M.LEDGER_PATH, M.DRY, M.public_get)
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            M.DRY = False
            M.public_get = lambda path, params=None: (
                200, {"market": {"result": "", "status": "active"}})
            st = M.LedgerState()
            st.positions = {"T": {"yes": 20.0, "no": 0.0}}
            st.position_cost = {"T": 8.0}
            st.position_cost_leg = {"T": {"yes": 8.0, "no": 0.0}}
            M.Maker(None, st, []).sweep_settlements(1000.0)
            self.assertEqual(st.positions["T"], {"yes": 20.0, "no": 0.0})
            self.assertAlmostEqual(st.position_cost["T"], 8.0, places=9)
            self.assertEqual(st.realized_pnl, 0.0)
        finally:
            M.DATA_DIR, M.LEDGER_PATH, M.DRY, M.public_get = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_realized_pnl_feeds_the_day_stop(self):
        st = M.LedgerState()
        st.realized_pnl = -30.0
        m = M.Maker(None, st, [])
        m.scores = {}
        tmp = tempfile.mkdtemp()
        old = (M.DATA_DIR, M.LEDGER_PATH)
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            self.assertTrue(m.check_day_stop([], {}, 1000.0))
            self.assertAlmostEqual(m.day_pnl, -30.0, places=9)
        finally:
            M.DATA_DIR, M.LEDGER_PATH = old
            shutil.rmtree(tmp, ignore_errors=True)


class _RunnerCase(unittest.TestCase):
    """Shared harness: a Maker with no network and a real ledger file.

    EVERY writable path is redirected, not just the ledger.  RECON_PATH and OPERATOR_PATH
    are module constants frozen at import from DATA_DIR, so patching DATA_DIR alone leaves
    them pointing at the OPERATOR'S REAL FILES — the same class of leak as the ntfy
    incident, and one that would have appended test rows to a live reconciliation ledger on
    the VPS.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = (M.DATA_DIR, M.LEDGER_PATH, M.RECON_PATH, M.OPERATOR_PATH, M.SEQ_PATH,
                    M.DRY)
        M.DATA_DIR = os.path.join(self.tmp, "lip")
        M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
        M.RECON_PATH = os.path.join(M.DATA_DIR, "recon.jsonl")
        M.OPERATOR_PATH = os.path.join(M.DATA_DIR, "pools_operator.jsonl")
        M.SEQ_PATH = os.path.join(M.DATA_DIR, "v4_coid_seq")
        M.DRY = False
        os.makedirs(M.DATA_DIR, exist_ok=True)

    def tearDown(self):
        (M.DATA_DIR, M.LEDGER_PATH, M.RECON_PATH, M.OPERATOR_PATH, M.SEQ_PATH,
         M.DRY) = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def events(self):
        if not os.path.exists(M.LEDGER_PATH):
            return []
        return [json.loads(l) for l in open(M.LEDGER_PATH)]


class B2_PhantomOrderRisk(_RunnerCase):

    def test_a_timed_out_place_poisons_the_market_and_records_the_coid(self):
        """B2 — status 0 is a TRANSPORT failure, not a rejection: the exchange may have
        accepted the order and we simply never learned its order_id.  Such an order is
        invisible to resting_collateral, to cancel_all and to the restart sweep."""
        m = M.Maker(None, M.LedgerState(), [])
        m.do_post = lambda body: (0, {"_transport_error": "timeout"})
        self.assertIsNone(m.place("T", "bid", 40, 10, 1785000000))
        self.assertIn("T", m.st.poisoned)
        self.assertIn("T", m.st.phantom_risk)
        rows = [e for e in self.events() if e["event"] == "phantom_risk"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["coid"].startswith("v4-"))
        self.assertEqual(rows[0]["size"], 10)
        # no further posts to that market
        self.assertIsNone(m.place("T", "bid", 40, 10, 1785000000))
        # ... and the risk is NOT assumed away: it survives replay
        st = M.ledger_replay(self.events())
        self.assertIn("T", st.phantom_risk)
        self.assertIn("T", st.poisoned)

    def test_an_ordinary_rejection_is_not_phantom(self):
        m = M.Maker(None, M.LedgerState(), [])
        m.do_post = lambda body: (400, {"error": "invalid_parameters"})
        self.assertIsNone(m.place("T", "bid", 40, 10, 1785000000))
        self.assertNotIn("T", m.st.phantom_risk)
        self.assertEqual([e for e in self.events() if e["event"] == "phantom_risk"], [])


class B3_CrashGapFillsAreAppliedAndCorrectlySigned(_RunnerCase):
    """B3 — crash-gap fills were logged but never applied to running state, AND the replay
    mapping was sign-INVERTED (side came raw from the fills payload as yes|no while replay
    tested for "bid"), with `action` dropped entirely."""

    def _maker_with_fills(self, fills):
        st = M.LedgerState()
        st.last_ts = 1000.0
        m = M.Maker(object(), st, [])
        m.do_fills = lambda **kw: (M.FillsRead(True, sum(
            M.num(f.get("count"), 0.0) for f in fills)), fills)
        return m

    def test_A4_crash_gap_buy_25_yes_at_30c(self):
        """The audit's A4: a buy of 25 YES at 30c must book as yes:25 costing $7.50 -- it
        was booking as no:25 at 70c, i.e. net -25."""
        self.assertEqual(M.normalize_fill("yes", "buy"), ("bid", 1.0))
        m = self._maker_with_fills([{"trade_id": "t1", "ticker": "DXY", "side": "yes",
                                     "action": "buy", "count": 25, "yes_price": 30}])
        m.restart_recovery()
        self.assertEqual(m.st.positions["DXY"], {"yes": 25.0, "no": 0.0})
        self.assertAlmostEqual(m.st.position_cost["DXY"], 7.50, places=9)
        self.assertAlmostEqual(m.st.net_position("DXY"), 25.0, places=9)
        # and the replay of what it wrote is IDENTICAL to the live state
        st = M.ledger_replay(self.events())
        self.assertEqual(st.positions["DXY"], m.st.positions["DXY"])
        self.assertAlmostEqual(st.position_cost["DXY"], m.st.position_cost["DXY"],
                               places=9)
        self.assertAlmostEqual(st.net_position("DXY"), 25.0, places=9)

    def test_A4_a_manual_SELL_of_held_inventory_decreases_the_position(self):
        """The live case: the operator is flattening DXY by hand while v4 is down.  A
        `action=sell` fill must DECREASE the position -- dropping `action` booked it as
        MORE inventory, which the recycler would then shed against contracts we no longer
        hold."""
        self.assertEqual(M.normalize_fill("yes", "sell"), ("bid", -1.0))
        st = M.LedgerState()
        st.last_ts = 1000.0
        st.apply_fill("DXY", "bid", 25, 0.30, 1.0)          # 25 YES already held
        m = M.Maker(object(), st, [])
        m.do_fills = lambda **kw: (M.FillsRead(True, 10.0), [
            {"trade_id": "t2", "ticker": "DXY", "side": "yes", "action": "sell",
             "count": 10, "yes_price": 32}])
        m.restart_recovery()
        self.assertAlmostEqual(m.st.positions["DXY"]["yes"], 15.0, places=9)
        self.assertAlmostEqual(m.st.net_position("DXY"), 15.0, places=9)
        self.assertLess(m.st.position_cost["DXY"], 7.50)
        rows = [e for e in self.events()
                if e["event"] == "fill_obs" and e.get("why") == "crash_gap"]
        self.assertEqual(rows[0]["side"], "bid")            # normalised, not raw "yes"
        self.assertEqual(rows[0]["action"], "sell")
        self.assertEqual(rows[0]["sign"], -1.0)

    def test_a_full_manual_flatten_leaves_no_phantom_inventory(self):
        st = M.LedgerState()
        st.last_ts = 1000.0
        st.apply_fill("DXY", "bid", 25, 0.30, 1.0)
        m = M.Maker(object(), st, [])
        m.do_fills = lambda **kw: (M.FillsRead(True, 25.0), [
            {"trade_id": "t3", "ticker": "DXY", "side": "yes", "action": "sell",
             "count": 25, "yes_price": 32}])
        m.restart_recovery()
        self.assertAlmostEqual(m.st.net_position("DXY"), 0.0, places=9)
        self.assertAlmostEqual(m.st.position_cost["DXY"], 0.0, places=9)
        # a disposal can never take the position negative
        st2 = M.LedgerState()
        st2.apply_fill("T", "bid", 5, 0.30, 1.0)
        st2.apply_fill("T", "bid", 50, 0.30, -1.0)
        self.assertAlmostEqual(st2.positions["T"]["yes"], 0.0, places=9)

    def test_a_no_side_buy_books_on_the_no_leg(self):
        self.assertEqual(M.normalize_fill("no", "buy"), ("ask", 1.0))
        st = M.LedgerState()
        st.apply_fill("T", "ask", 25, 0.30, 1.0)            # yes_price 30 => NO at 70c
        self.assertEqual(st.positions["T"], {"yes": 0.0, "no": 25.0})
        self.assertAlmostEqual(st.position_cost["T"], 25 * 0.70, places=9)
        self.assertAlmostEqual(st.net_position("T"), -25.0, places=9)


class B4_404IsResolvedExactlyOnce(_RunnerCase):

    def test_A3_restart_with_one_unresolved_404_books_10_not_20(self):
        """B4 — restart runs two passes that can both reach the same order: the UNKNOWN loop
        cancels it, gets a 404 and resolves; then the unresolved_404 loop resolves it AGAIN
        and credits the same contracts twice.  Doubled inventory drives an oversized shed
        against contracts we never held: a real naked short from pure bookkeeping."""
        recs = [rec_place("O1", T, "bid", 0.40, 10), rec_cancel("O1", 404)]
        st = M.ledger_replay(recs)
        self.assertEqual(st.unresolved_404, ["O1"])
        m = M.Maker(object(), st, [])
        m.do_cancel = lambda oid: (404, {"error": "not_found"})
        m.do_fills = lambda **kw: (M.FillsRead(True, 10.0),
                                   [{"trade_id": "f1", "count": 10}])
        m.restart_recovery()
        self.assertAlmostEqual(m.st.filled(T, "bid"), 10.0)      # was 20.0
        self.assertAlmostEqual(m.st.net_position(T), 10.0)
        self.assertAlmostEqual(m.st.position_cost[T], 4.00, places=9)
        self.assertEqual(m.st.unresolved_404, [])
        # live state and a fresh replay of the same ledger agree
        replayed = M.ledger_replay(recs + self.events())
        self.assertAlmostEqual(replayed.net_position(T), m.st.net_position(T), places=9)
        skipped = [e for e in self.events() if e["event"] == "resolve_404_skipped"]
        self.assertEqual(len(skipped), 1)

    def test_resolving_an_already_resolved_order_is_a_no_op(self):
        m = M.Maker(object(), M.LedgerState(), [])
        o = M.OrderState("O1", "v4-c", T, "bid", 0.40, 10, 0.0, 10.0)
        o.reduced_by = 0.0
        o.extra_fills = 10.0
        m.st.orders["O1"] = o
        m.do_fills = lambda **kw: (M.FillsRead(True, 10.0), [{"trade_id": "x", "count": 10}])
        m.resolve_404(o)
        self.assertEqual(o.extra_fills, 10.0)
        self.assertEqual(m.st.filled(T, "bid"), 0.0)


class D1_ShutdownDoesNotBlockOnRequeries(_RunnerCase):

    def test_cancel_all_with_six_pending_404s_completes_fast(self):
        """D1 — the §9.4a re-query is a BLOCKING 36s sleep.  Six pending 404s in a bulk
        cancel is 216s against TimeoutStopSec, so systemd SIGKILLs us mid-shutdown, leaving
        stranded orders AND unresolved 404s -- the B4 amplifier."""
        m = M.Maker(object(), M.LedgerState(), [])
        for i in range(6):
            o = M.OrderState("O%d" % i, "v4-c", T, "bid", 0.40, 10, 0.0, 10.0)
            m.st.orders[o.order_id] = o
        m.do_cancel = lambda oid: (404, {"error": "not_found"})
        m.do_fills = lambda **kw: (M.FillsRead(True, 0.0), [])
        t0 = time.time()
        m.cancel_all("shutdown")
        elapsed = time.time() - t0
        self.assertLess(elapsed, 5.0)
        self.assertGreaterEqual(M.FILLS_REQUERY_DELAY_S * 6, 216)   # what it would have been
        deferred = [e for e in self.events() if e["event"] == "resolve_404_deferred"]
        self.assertEqual(len(deferred), 6)
        # the 404s are on the ledger, so replay hands them to restart_recovery
        tape = [rec_place("O%d" % i, T, "bid", 0.40, 10, ts=100.0 + i) for i in range(6)]
        self.assertEqual(sorted(M.ledger_replay(tape + self.events()).unresolved_404),
                         ["O%d" % i for i in range(6)])
        self.assertEqual(m.defer_404, False)                # flag restored

    def test_the_live_loop_single_cancel_still_requeries(self):
        m = M.Maker(object(), M.LedgerState(), [])
        o = M.OrderState("O1", "v4-c", T, "bid", 0.40, 10, 0.0, 10.0)
        m.st.orders["O1"] = o
        m.do_cancel = lambda oid: (404, {"error": "not_found"})
        m.do_fills = lambda **kw: (M.FillsRead(True, 10.0), [{"trade_id": "f", "count": 10}])
        m.cancel(o)
        self.assertAlmostEqual(m.st.filled(T, "bid"), 10.0)   # resolved inline, not deferred
        self.assertEqual([e for e in self.events()
                          if e["event"] == "resolve_404_deferred"], [])

    def test_the_unit_file_allows_room_for_shutdown(self):
        unit = open(os.path.join(os.path.dirname(os.path.abspath(M.__file__)),
                                 "lip-maker-v4.service")).read()
        self.assertIn("TimeoutStopSec=180", unit)


class C3_ProjectionScalesWithRemainingWindow(unittest.TestCase):

    def test_A8_228h_program_with_2h_left_is_refused(self):
        """C3 — the projection multiplied the FULL-period pool by the CURRENT share with no
        hours_left scaling, so a 228h program with 2h left projected as if it had all 228.
        A gate that mis-grades permissively is worse than no gate: it launders a bad entry
        as a checked one."""
        rho = 100.0 / 228.0
        late = M.Slot("T", "bid", rho, 1.0, 0.02, program_id="P1", window_h=228.0,
                      pool=100.0, hours_left=2.0)
        alloc = {late.key: 100}
        proj = M.projected_period_payout([late], alloc)
        reachable = M.our_share(100, 1.0) * (rho / 2.0) * 2.0
        self.assertAlmostEqual(proj, reachable, places=9)
        self.assertLess(proj, 0.50)                          # ~$0.43, not ~$25
        self.assertFalse(M.forfeit_gate(proj))
        # the old, unscaled form is what waved it through
        self.assertGreater((100.0 / 2.0) * M.our_share(100, 1.0), 25.0)

    def test_a_full_window_is_unchanged(self):
        """share*(rho/2)*window_h == share*(pool/2), so nothing moves for a fresh program."""
        s = M.Slot("T", "bid", 6.25, 50.0, 0.40, pool=100.0, window_h=16.0)
        alloc = {s.key: 100}
        self.assertAlmostEqual(M.projected_period_payout([s], alloc),
                               (100.0 / 2.0) * M.our_share(100, 50.0), places=9)

    def test_accrued_is_added_and_not_scaled(self):
        """§3.6 -- accrued score is already banked; only the un-accrued portion scales."""
        rho = 100.0 / 228.0
        s = M.Slot("T", "bid", rho, 1.0, 0.02, pool=100.0, window_h=228.0,
                   hours_left=2.0, accrued=1.20)
        proj = M.projected_period_payout([s], {s.key: 100})
        self.assertAlmostEqual(proj, 1.20 + M.our_share(100, 1.0) * (rho / 2.0) * 2.0,
                               places=9)
        self.assertGreater(proj, M.RESCUE_TARGET_USD)        # worth rescuing
        self.assertFalse(M.forfeit_gate(proj))               # but not worth ENTERING

    def test_the_gate_drops_a_program_it_can_no_longer_reach(self):
        """Two independent layers now refuse a dying program, and it is worth knowing which
        fires: the RUNWAY guard rejects the un-accrued case at entry (18.2h needed, 2h
        left), so the scaled projection never even gets a chance.  The gate is what catches
        the case runway lets through -- one already accrued past RESCUE_TARGET."""
        rho = 100.0 / 228.0
        late = M.Slot("T", "bid", rho, 1.0, 0.02, program_id="P1", window_h=228.0,
                      pool=100.0, hours_left=2.0, phi=0.0, d=0.0)
        self.assertFalse(M.runway_ok(rho, 2.0, 0.0))
        alloc, spent, dropped = M.allocate_with_forfeit_gate([late], 45.0, BIG,
                                                             lambda_min=0.0)
        self.assertEqual(alloc[late.key], 0)                 # no capital, either way
        self.assertEqual(spent, 0.0)
        # now the case runway ALLOWS through: accrued past RESCUE_TARGET, still unreachable
        accrued = M.Slot("T", "bid", rho, 1.0, 0.02, program_id="P1", window_h=228.0,
                         pool=100.0, hours_left=2.0, phi=0.0, d=0.0, accrued=1.20)
        self.assertTrue(M.runway_ok(rho, 2.0, 1.20))
        proj = M.projected_period_payout([accrued], {accrued.key: 100})
        self.assertLess(proj, M.ENTRY_FLOOR_USD)
        self.assertFalse(M.forfeit_gate(proj))


class C4_ShedRetriesAloneWhenTheCombinedOrderIsRejected(_RunnerCase):

    def test_A7_shed_posts_at_29_when_the_combined_size_fails_the_cap(self):
        """C4 -- q = max(alloc, shed) is submitted as ONE order.  If the allocator wants more
        than the shed size, the combined order fails the C1 cap, place() returns None, and
        the shed inside it vanishes: the inventory locks.  A closing-only order always
        passes by netting, so it must be retried alone."""
        st = M.LedgerState()
        st.positions = {"DXY": {"yes": 29.0, "no": 0.0}}
        st.position_cost = {"DXY": 29 * 0.34}
        st.position_cost_leg = {"DXY": {"yes": 29 * 0.34, "no": 0.0}}
        m = M.Maker(None, st, [])
        posted = []

        def post(body):
            posted.append(int(float(body["count"])))
            return (201, {"order_id": "N%d" % len(posted), "fill_count": "0.00",
                          "remaining_count": body["count"]})
        m.do_post = post
        shed_q = 29
        combined = 60                                   # allocator wants far more
        # the combined order is refused by the C1 cap ...
        self.assertIsNone(m.place("DXY", "ask", 36, combined, 1785000000))
        self.assertEqual(m.last_place_skip, "net_inventory_cap")
        self.assertEqual(posted, [])
        # ... and the closing-only retry succeeds, because it nets to zero exposure
        room_before = m.st.closing_room("DXY", "ask")
        self.assertAlmostEqual(room_before, 29.0, places=9)
        self.assertAlmostEqual(
            M.order_collateral_usd("ask", 0.36, shed_q, room=room_before), 0.0, places=9)
        o = m.place("DXY", "ask", 36, shed_q, 1785000000)
        self.assertIsNotNone(o)
        self.assertEqual(posted, [29])
        # and once it rests it has consumed the room, so a SECOND shed cannot free-ride
        self.assertAlmostEqual(m.st.closing_room("DXY", "ask"), 0.0, places=9)

    def test_the_retry_is_wired_and_logged(self):
        src = open(M.__file__.replace(".pyc", ".py")).read()
        self.assertIn("shed_retry_after_combined_reject", src)
        self.assertIn("if placed is None and shed_q > 0 and q > shed_q:", src)


class UnitAssertionIsAboutTheUnitNotAboutGas(unittest.TestCase):
    """§0.3 -- the assertion verifies PERIOD_REWARD_UNIT_USD against known truth.  Pinning it
    to one series made the process unstartable between that series' windows: measured
    04:04Z, 771 programs live, 585 reading exactly $100.00, and v4 refused to start because
    the gas daily had closed at 03:59Z and the next day had not listed."""

    def test_the_modal_pool_carries_the_assertion_without_gas(self):
        ok, d = M.unit_assertion_check(unit_progs(40, gas=0))
        self.assertTrue(ok)
        self.assertEqual(d["n_at_expect"], 40)
        self.assertEqual(d["series_live"], 0)
        self.assertIsNone(d["series_ok"])              # skipped, not failed
        self.assertEqual(len(d["samples"]), 3)
        self.assertEqual(d["min_required"], 30)
        self.assertEqual(M.UNIT_ASSERT_MIN_MATCHES, 30)

    def test_gas_when_live_is_an_additional_belt(self):
        ok, d = M.unit_assertion_check(unit_progs(40, gas=17))
        self.assertTrue(ok)
        self.assertTrue(d["series_ok"])
        self.assertEqual(d["series_live"], 17)
        self.assertEqual(d["n_at_expect"], 57)

    def test_gas_live_but_reading_wrong_fails_even_with_the_modal_pool_intact(self):
        """The belt has teeth: if the original anchor is live and disagrees, refuse."""
        ok, d = M.unit_assertion_check(unit_progs(40, gas=17, gas_reward=100_000.0))
        self.assertFalse(ok)
        self.assertFalse(d["series_ok"])
        self.assertEqual(d["n_at_expect"], 40)         # the modal pool alone would pass

    def test_a_unit_error_collapses_the_count_to_zero_and_refuses(self):
        """A unit error is not subtle: at 1e-3 every program reads $1,000 and at 1e-5 every
        one reads $10.00, so the matching count goes to ZERO rather than degrading."""
        for wrong in (100_000.0, 10_000_000.0, 1_000.0, 100_000_000.0):
            ok, d = M.unit_assertion_check(unit_progs(500, reward=wrong))
            self.assertFalse(ok, wrong)
            self.assertEqual(d["n_at_expect"], 0, wrong)
        # and the real 100x-off cases specifically
        ok, _ = M.unit_assertion_check(unit_progs(771, reward=1_000_000.0 * 100))
        self.assertFalse(ok)
        ok, _ = M.unit_assertion_check(unit_progs(771, reward=1_000_000.0 / 100))
        self.assertFalse(ok)

    def test_too_few_matches_refuses(self):
        self.assertFalse(M.unit_assertion_check(unit_progs(29))[0])
        self.assertTrue(M.unit_assertion_check(unit_progs(30))[0])
        self.assertFalse(M.unit_assertion_check([])[0])
        # 20x margin against program-mix drift: today's 585 is far above the floor
        self.assertGreaterEqual(585 / M.UNIT_ASSERT_MIN_MATCHES, 19.0)

    def test_the_refusal_reaches_startup(self):
        tmp = tempfile.mkdtemp()
        old = (M.DATA_DIR, M.LEDGER_PATH)
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            ok, results = M.startup_assertions(None, "n/a",
                                               programs=unit_progs(40, reward=100_000.0))
            unit = [r for r in results if r[0].startswith("unit_")][0]
            self.assertFalse(unit[1])
            self.assertFalse(ok)
            ok2, results2 = M.startup_assertions(None, "n/a", programs=unit_progs(40))
            unit2 = [r for r in results2 if r[0].startswith("unit_")][0]
            self.assertTrue(unit2[1])
            self.assertIn("40/40", unit2[2])
        finally:
            M.DATA_DIR, M.LEDGER_PATH = old
            shutil.rmtree(tmp, ignore_errors=True)


class WindowStartGuardAndPrepositioning(_RunnerCase):
    """LIVE DEFECT: the allocator checked the window END and never the START, so v4 entered
    three WNBA-mention slots whose programs open 10.5h later (15:00Z), locking ~$11 of a
    BINDING ceiling while live-window PYPL posts were being refused on `collateral_ceiling`
    in the same second.  Under a binding ceiling every non-earning dollar displaces an
    earning dollar 1:1."""

    def test_a_program_that_opens_in_10_hours_is_excluded(self):
        far = M.Slot("KXWNBAMENTION-26JUL29", "bid", 6.25, 50.0, 0.02, hours_to_start=10.5)
        self.assertFalse(M.preposition_ok(10.5))
        al, spent = M.allocate([far], 45.0, BIG)
        self.assertEqual(al[far.key], 0)
        self.assertEqual(spent, 0.0)

    def test_the_displaced_earning_slot_gets_the_capital_instead(self):
        """The 1:1 displacement, made concrete: with the ceiling binding, the pre-start slot
        was taking the dollars the live-window slot was then refused."""
        far = M.Slot("WNBA", "bid", 6.25, 50.0, 0.02, hours_to_start=10.5)
        live = M.Slot("PYPL", "bid", 6.25, 50.0, 0.02, hours_to_start=0.0)
        al, spent = M.allocate([far, live], 11.0, M.Caps())
        self.assertEqual(al[far.key], 0)
        self.assertGreater(al[live.key], 0)
        self.assertGreater(spent, 0.0)

    def test_a_program_that_opens_in_10_minutes_is_allowed_under_the_lead(self):
        """§6.1/6.2's land-grab is real -- the 6am gas open still gets first-mover quotes 15
        minutes early, just never 10 hours early."""
        self.assertEqual(M.PREPOSITION_LEAD_H, 0.25)
        self.assertTrue(M.preposition_ok(10.0 / 60.0))
        soon = M.Slot("KXAAAGASD", "bid", 6.25, 50.0, 0.02, hours_to_start=10.0 / 60.0)
        self.assertGreater(M.allocate([soon], 45.0, BIG)[0][soon.key], 0)

    def test_the_boundary_is_exactly_the_lead(self):
        self.assertTrue(M.preposition_ok(0.25))
        self.assertFalse(M.preposition_ok(0.2501))
        self.assertTrue(M.preposition_ok(0.0))
        at_lead = M.Slot("T", "bid", 6.25, 50.0, 0.02, hours_to_start=0.25)
        past = M.Slot("T2", "bid", 6.25, 50.0, 0.02, hours_to_start=0.26)
        self.assertGreater(M.allocate([at_lead], 45.0, BIG)[0][at_lead.key], 0)
        self.assertEqual(M.allocate([past], 45.0, BIG)[0][past.key], 0)

    def test_the_transition_at_start_ts_admits_the_slot(self):
        """The same program, one poll before and one poll after its own start_ts."""
        prog = {"program_id": "P1", "market_ticker": "T", "series": "KX",
                "period_reward": 1000000.0, "target_size_fp": 10.0,
                "discount_factor_bps": 5000.0, "start_ts": 10000.0,
                "end_ts": 10000.0 + 16 * 3600.0, "paid_out": False}
        book = {"orderbook": {"orderbook_fp": {
            "yes_dollars": [["0.4000", "100"], ["0.3900", "100"]],
            "no_dollars": [["0.5900", "100"], ["0.5800", "100"]]}}}
        early, _ = M.slots_from_market(prog, book, 10000.0 - 10 * 3600.0)   # 10h before
        self.assertAlmostEqual(early[0].hours_to_start, 10.0, places=6)
        self.assertEqual(sum(M.allocate(early, 45.0, BIG)[0].values()), 0)
        inside, _ = M.slots_from_market(prog, book, 10000.0 - 600.0)        # 10 min before
        self.assertAlmostEqual(inside[0].hours_to_start, 10.0 / 60.0, places=6)
        self.assertGreater(sum(M.allocate(inside, 45.0, BIG)[0].values()), 0)
        started, _ = M.slots_from_market(prog, book, 10000.0)               # exactly at open
        self.assertEqual(started[0].hours_to_start, 0.0)
        self.assertGreater(sum(M.allocate(started, 45.0, BIG)[0].values()), 0)

    # ---- release ---------------------------------------------------------------------
    def _prog2(self, pid, ticker, start, end):
        return {"program_id": pid, "market_ticker": ticker, "series": "KX",
                "period_reward": 1000000.0, "target_size_fp": 1000.0,
                "discount_factor_bps": 5000.0, "start_ts": start, "end_ts": end,
                "paid_out": False}

    def test_a_pre_start_market_has_its_non_closing_orders_released(self):
        st = M.LedgerState()
        st.positions = {"T": {"yes": 20.0, "no": 0.0}}
        st.position_cost = {"T": 8.0}
        st.position_cost_leg = {"T": {"yes": 8.0, "no": 0.0}}
        quote = M.OrderState("A_QUOTE", "v4-c", "T", "bid", 0.40, 10, 0.0, 10.0)
        shed = M.OrderState("B_SHED", "v4-c", "T", "ask", 0.42, 20, 0.0, 20.0)
        st.orders["A_QUOTE"] = quote
        st.orders["B_SHED"] = shed
        m = M.Maker(None, st, [self._prog2("P1", "T", 100000.0, 200000.0)])   # far future
        m.live_by_slot[("T", "bid")] = quote
        m.live_by_slot[("T", "ask")] = shed
        m.classified["T"] = {"rho": 6.25, "pinned": False, "denied": False, "sides": []}
        cancelled = []
        m.do_cancel = lambda oid: (cancelled.append(oid), (200, {"reduced_by": "10.00"}))[1]
        m.release_out_of_window(1000.0)
        self.assertEqual(cancelled, ["A_QUOTE"])          # the pre-start quote goes
        self.assertEqual(shed.state, M.ST_LIVE)           # inventory still outlives it
        self.assertNotIn("T", m.classified)
        rows = [e for e in self.events() if e["event"] == "out_of_window_release"]
        self.assertEqual(rows[0]["reason"], "pre_start")

    def test_release_re_arms_when_the_program_actually_starts(self):
        """A pre-start release must not be permanent -- the program WILL start, unlike an
        ended one, and the market has to be re-enterable when it does."""
        st = M.LedgerState()
        m = M.Maker(None, st, [self._prog2("P1", "T", 5000.0, 90000.0)])
        m.do_cancel = lambda oid: (200, {"reduced_by": "0.00"})
        m.release_out_of_window(1000.0)                   # 4000s early: released
        self.assertIn("P1", m.released)
        m.release_out_of_window(5000.0)                   # now open: re-armed
        self.assertNotIn("P1", m.released)

    def test_an_ended_program_still_releases(self):
        st = M.LedgerState()
        quote = M.OrderState("A", "v4-c", "T", "bid", 0.40, 10, 0.0, 10.0)
        st.orders["A"] = quote
        m = M.Maker(None, st, [self._prog2("P1", "T", 0.0, 1000.0)])
        m.live_by_slot[("T", "bid")] = quote
        cancelled = []
        m.do_cancel = lambda oid: (cancelled.append(oid), (200, {"reduced_by": "10.00"}))[1]
        m.release_out_of_window(1001.0)
        self.assertEqual(cancelled, ["A"])
        rows = [e for e in self.events() if e["event"] == "out_of_window_release"]
        self.assertEqual(rows[0]["reason"], "ended")

    def test_an_earning_program_on_the_same_market_keeps_the_quotes(self):
        st = M.LedgerState()
        quote = M.OrderState("A", "v4-c", "T", "bid", 0.40, 10, 0.0, 10.0)
        st.orders["A"] = quote
        m = M.Maker(None, st, [self._prog2("P1", "T", 100000.0, 200000.0),   # pre-start
                               self._prog2("P2", "T", 0.0, 90000.0)])        # earning now
        m.live_by_slot[("T", "bid")] = quote
        cancelled = []
        m.do_cancel = lambda oid: (cancelled.append(oid), (200, {"reduced_by": "0"}))[1]
        m.release_out_of_window(1000.0)
        self.assertEqual(cancelled, [])
        self.assertIn(("T", "bid"), m.live_by_slot)

    def test_in_window_combines_both_ends(self):
        self.assertTrue(M.in_window(0.0, 5.0))
        self.assertTrue(M.in_window(0.2, 5.0))
        self.assertFalse(M.in_window(10.5, 5.0))          # not started
        self.assertFalse(M.in_window(0.0, 0.0))           # ended


class Phase1_DayStopActuallyFlattens(_RunnerCase):
    """C3 / cold-audit C2 -- the day stop set `halted` BEFORE flattening, so place() refused
    every shed and "flatten" was a log line.  The stop cancelled everything and then held the
    inventory to settlement: the opposite of §8.4's cancel-all -> flatten -> alert -> exit."""

    def _maker(self, net_yes=20.0):
        st = M.LedgerState()
        st.positions = {"T": {"yes": max(0.0, net_yes), "no": max(0.0, -net_yes)}}
        st.position_cost = {"T": abs(net_yes) * 0.40}
        st.position_cost_leg = {"T": {"yes": max(0.0, net_yes) * 0.40,
                                      "no": max(0.0, -net_yes) * 0.40}}
        m = M.Maker(None, st, [])
        m.scores = {"T": {"yes_bid_c": 40, "yes_ask_c": 42}}
        self.posted = []
        m.do_post = lambda b: (self.posted.append((b["side"], b["count"], b["price"])),
                               (201, {"order_id": "N%d" % len(self.posted),
                                      "fill_count": "0.00",
                                      "remaining_count": b["count"]}))[1]
        m.do_cancel = lambda oid: (200, {"reduced_by": "0.00"})
        return m

    def test_a_closing_order_survives_the_halt_gate(self):
        m = self._maker()
        m.halted = True
        self.assertTrue(m._closing_exempt("T", "ask", 20))
        self.assertIsNotNone(m.place("T", "ask", 42, 20, 1785000000))
        # ... but an OPENING order still does not
        self.assertFalse(m._closing_exempt("T", "bid", 20))
        self.assertIsNone(m.place("T", "bid", 40, 20, 1785000000))
        self.assertEqual(m.last_place_skip, "halted")

    def test_the_day_stop_posts_sheds_before_halting(self):
        m = self._maker()
        m.st.realized_pnl = -100.0                    # force the breach
        self.assertTrue(m.check_day_stop([], {}, 1000.0))
        self.assertTrue(m.halted)
        self.assertEqual([p[0] for p in self.posted], ["ask"])   # a shed WAS posted
        self.assertEqual(self.posted[0][1], "20.00")             # sized at exactly abs(net)
        ev = {e["event"] for e in self.events()}
        self.assertIn("flatten_shed_posted", ev)
        self.assertIn("flatten_summary", ev)

    def test_the_shed_is_sized_at_abs_net_and_never_reverses(self):
        m = self._maker(net_yes=-15.0)               # long NO
        posted, residual, usd = m.flatten(1000.0, "test")
        self.assertEqual(posted, 1)
        self.assertEqual(self.posted[0][0], "bid")   # shedding NO means buying YES
        self.assertEqual(self.posted[0][1], "15.00")
        self.assertEqual(residual, 0.0)

    def test_residual_inventory_is_reported_honestly(self):
        """With TAKER_EXIT_ENABLED False there is no second mechanism, so the process can
        exit still holding inventory -- and must say so in those words."""
        m = self._maker()
        m.scores = {}                                 # no book: the shed cannot be priced
        posted, residual, usd = m.flatten(1000.0, "day_stop")
        self.assertEqual(posted, 0)
        self.assertAlmostEqual(residual, 20.0, places=6)
        self.assertAlmostEqual(usd, 8.0, places=6)
        summary = [e for e in self.events() if e["event"] == "flatten_summary"][0]
        self.assertIn("exited holding 20 contracts, $8.00", summary["note"])
        self.assertFalse(summary["taker_exit_enabled"])

    def test_a_frozen_market_is_never_flattened(self):
        """§9.4b/§5.6 -- acting on unverified inventory is how a bookkeeping ambiguity
        becomes a real naked short."""
        m = self._maker()
        m.st.assume_filled.add("T")
        posted, residual, usd = m.flatten(1000.0, "day_stop")
        self.assertEqual(posted, 0)
        self.assertEqual(self.posted, [])
        self.assertAlmostEqual(residual, 20.0, places=6)
        why = [e for e in self.events() if e["event"] == "flatten_skipped"][0]["why"]
        self.assertEqual(why, "assume_filled_freeze")


class Phase1_TakerExitPathIsReadyForEitherAnswer(_RunnerCase):
    """The DECISION stays Ryan's at 5:35; the CODE must be correct for either answer."""

    def _maker(self, net_yes=20.0, yb=40, ya=42):
        st = M.LedgerState()
        st.positions = {"T": {"yes": max(0.0, net_yes), "no": max(0.0, -net_yes)}}
        st.position_cost = {"T": abs(net_yes) * 0.40}
        st.position_cost_leg = {"T": {"yes": max(0.0, net_yes) * 0.40,
                                      "no": max(0.0, -net_yes) * 0.40}}
        m = M.Maker(None, st, [])
        m.scores = {"T": {"yes_bid_c": yb, "yes_ask_c": ya}}
        self.posted = []
        m.do_post = lambda b: (self.posted.append(b),
                               (201, {"order_id": "N1", "fill_count": b["count"],
                                      "remaining_count": "0.00"}))[1]
        m.do_cancel = lambda oid: (200, {"reduced_by": "0.00"})
        return m

    def test_it_is_a_no_op_while_the_flag_is_off(self):
        m = self._maker()
        m.shed_since["T"] = 0.0
        self.assertFalse(M.TAKER_EXIT_ENABLED)
        self.assertIsNone(m.taker_exit("T", 99999.0))
        self.assertEqual(self.posted, [])

    def _enabled(self, fn):
        old = M.TAKER_EXIT_ENABLED
        M.TAKER_EXIT_ENABLED = True
        try:
            return fn()
        finally:
            M.TAKER_EXIT_ENABLED = old

    def test_when_enabled_it_crosses_by_a_bounded_limit_and_is_sized_at_abs_net(self):
        m = self._maker(net_yes=20.0, yb=40, ya=42)
        m.shed_since["T"] = 0.0                       # shed has had its 30 minutes
        o = self._enabled(lambda: m.taker_exit("T", 99999.0))
        self.assertIsNotNone(o)
        self.assertEqual(len(self.posted), 1)
        b = self.posted[0]
        self.assertEqual(b["side"], "ask")            # shedding YES
        self.assertEqual(b["count"], "20.00")         # bounded by abs(net): never reverses
        # crosses DOWN through the bid by at most the slippage cap, never further
        self.assertAlmostEqual(float(b["price"]), (40 - M.TAKER_EXIT_MAX_SLIPPAGE_C) / 100.0,
                               places=6)
        self.assertEqual(M.TAKER_EXIT_MAX_SLIPPAGE_C, 3)

    def test_it_waits_the_full_thirty_shed_minutes(self):
        """§5.4(ii) -- escalation only after the maker shed has had its chance."""
        m = self._maker()
        m.shed_since["T"] = 99999.0 - (M.SHED_PATIENCE_S - 1)
        self.assertIsNone(self._enabled(lambda: m.taker_exit("T", 99999.0)))
        m.shed_since["T"] = 99999.0 - M.SHED_PATIENCE_S
        self.assertIsNotNone(self._enabled(lambda: m.taker_exit("T", 99999.0)))

    def test_it_aborts_on_a_crossed_book_per_8_8(self):
        m = self._maker(yb=50, ya=48)                 # our_bid >= our_ask
        m.shed_since["T"] = 0.0
        self.assertIsNone(self._enabled(lambda: m.taker_exit("T", 99999.0)))
        self.assertIn("T", m.st.poisoned)
        ev = [e["event"] for e in self.events()]
        self.assertIn("taker_exit_abort", ev)

    def test_it_never_touches_a_frozen_market_or_a_flat_one(self):
        m = self._maker()
        m.shed_since["T"] = 0.0
        m.st.assume_filled.add("T")
        self.assertIsNone(self._enabled(lambda: m.taker_exit("T", 99999.0)))
        m2 = self._maker(net_yes=0.0)
        m2.shed_since["T"] = 0.0
        self.assertIsNone(self._enabled(lambda: m2.taker_exit("T", 99999.0)))

    def test_the_limit_stays_inside_the_legal_tick_range(self):
        m = self._maker(net_yes=20.0, yb=2, ya=4)
        m.shed_since["T"] = 0.0
        self._enabled(lambda: m.taker_exit("T", 99999.0))
        self.assertGreaterEqual(float(self.posted[0]["price"]) * 100,
                                M.MIN_LEGAL_PRICE_C)

    def test_the_300_flip_is_one_coordinated_change(self):
        """The morning commit flips ceiling AND flag together; the startup assertion is what
        makes forgetting either one impossible."""
        self.assertFalse(M.TAKER_EXIT_ENABLED)
        self.assertLess(M.MAX_TOTAL_COLLATERAL_USD, M.TAKER_EXIT_REQUIRED_ABOVE_USD)
        ok, _ = M.unit_assertion_check(unit_progs(40))
        self.assertTrue(ok)


class Phase1_ReleaseAndHousekeeping(_RunnerCase):

    def test_C5_scores_are_pruned_on_release_so_marks_cannot_freeze(self):
        st = M.LedgerState()
        m = M.Maker(None, st, [{"program_id": "P1", "market_ticker": "T", "series": "KX",
                                "period_reward": 1e6, "target_size_fp": 1000.0,
                                "discount_factor_bps": 5000.0, "start_ts": 0.0,
                                "end_ts": 1000.0, "paid_out": False}])
        m.scores["T"] = {"yes_bid_c": 40, "yes_ask_c": 42}
        m.classified["T"] = {"rho": 6.25, "pinned": False, "denied": False, "sides": []}
        m.do_cancel = lambda oid: (200, {"reduced_by": "0"})
        m.release_out_of_window(1001.0)
        self.assertNotIn("T", m.scores)
        self.assertNotIn("T", m.classified)
        # NEW-2 then marks the position AT COST rather than off a book we stopped watching
        self.assertEqual(M.unpriced_positions({"T": {"yes": 5.0, "no": 0.0}}, {}), ["T"])

    def test_C7_a_partially_closing_order_is_trimmed_to_its_closing_size(self):
        st = M.LedgerState()
        st.positions = {"T": {"yes": 20.0, "no": 0.0}}
        st.position_cost = {"T": 8.0}
        st.position_cost_leg = {"T": {"yes": 8.0, "no": 0.0}}
        big = M.OrderState("A", "v4-c", "T", "ask", 0.42, 30, 0.0, 30.0)   # 20 close/10 open
        st.orders["A"] = big
        m = M.Maker(None, st, [{"program_id": "P1", "market_ticker": "T", "series": "KX",
                                "period_reward": 1e6, "target_size_fp": 1000.0,
                                "discount_factor_bps": 5000.0, "start_ts": 0.0,
                                "end_ts": 1000.0, "paid_out": False}])
        m.live_by_slot[("T", "ask")] = big
        posted = []
        m.do_post = lambda b: (posted.append(b["count"]),
                               (201, {"order_id": "N1", "fill_count": "0.00",
                                      "remaining_count": b["count"]}))[1]
        m.do_cancel = lambda oid: (200, {"reduced_by": "30.00"})
        m.release_out_of_window(1001.0)
        self.assertEqual(posted, ["20.00"])          # reposted at exactly the closing size
        row = [e for e in self.events() if e["event"] == "out_of_window_release"][0]
        self.assertEqual(row["trimmed"][0]["closing"], 20)

    def test_C6_a_kept_shed_keeps_requoting_after_the_window_ends(self):
        st = M.LedgerState()
        st.positions = {"T": {"yes": 20.0, "no": 0.0}}
        st.position_cost = {"T": 8.0}
        st.position_cost_leg = {"T": {"yes": 8.0, "no": 0.0}}
        m = M.Maker(None, st, [])
        m.flatten_only.add("T")
        old_get = M.public_get
        posted = []
        try:
            M.public_get = lambda path, params=None: (200, {"orderbook": {
                "orderbook_fp": {"yes_dollars": [["0.4100", "50"]],
                                 "no_dollars": [["0.5700", "50"]]}}})
            m.do_post = lambda b: (posted.append((b["side"], b["price"], b["count"])),
                                   (201, {"order_id": "N1", "fill_count": "0.00",
                                          "remaining_count": b["count"]}))[1]
            m.do_cancel = lambda oid: (200, {"reduced_by": "0.00"})
            m.requote_orphan_sheds(1000.0)
            self.assertEqual(posted, [("ask", "0.4300", "20.00")])   # joined the NO best
            m.requote_orphan_sheds(1001.0)                # throttled to SAFETY_RESYNC_S
            self.assertEqual(len(posted), 1)
        finally:
            M.public_get = old_get
        # once flat it stops chasing
        m.st.positions["T"] = {"yes": 0.0, "no": 0.0}
        m.requote_orphan_sheds(1000.0 + M.SAFETY_RESYNC_S + 1)
        self.assertNotIn("T", m.flatten_only)

    def test_C10_terminal_orders_are_pruned_and_live_views_are_unaffected(self):
        st = M.LedgerState()
        for i in range(M.ORDER_RETENTION + 50):
            o = M.OrderState("O%04d" % i, "c", "T", "bid", 0.40, 10, 0.0, 10.0)
            o.reduced_by = 10.0
            o.state = M.ST_CLOSED
            st.orders[o.order_id] = o
        live = M.OrderState("ZLIVE", "c", "T", "bid", 0.40, 10, 0.0, 10.0)
        st.orders["ZLIVE"] = live
        before = st.resting_collateral
        dropped = st.prune_terminal_orders()
        self.assertEqual(dropped, 50)
        self.assertEqual(len(st.orders), M.ORDER_RETENTION + 1)
        self.assertIn("ZLIVE", st.orders)             # a RESTING order is never pruned
        self.assertAlmostEqual(st.resting_collateral, before, places=9)
        self.assertEqual(st.prune_terminal_orders(), 0)   # idempotent

    def test_D5_the_plan_is_feasible_against_held_positions(self):
        """Planning against the raw ceiling produced an infeasible plan that place() then
        rationed first-come, so what reached the book was not what ALLOCATE computed."""
        held = M.MAX_TOTAL_COLLATERAL_USD * 0.5
        budget = max(0.0, M.MAX_TOTAL_COLLATERAL_USD - held)
        self.assertAlmostEqual(budget, M.MAX_TOTAL_COLLATERAL_USD - held, places=9)
        s = M.Slot("T", "bid", 6.25, 50.0, 0.02, phi=0.0, d=0.0)
        al, spent = M.allocate([s], budget, BIG, lambda_min=0.0)
        self.assertLessEqual(spent + held, M.MAX_TOTAL_COLLATERAL_USD + 1e-9)

    def test_D7_a_stuck_unknown_order_is_retried_then_booked_conservatively(self):
        st = M.LedgerState()
        o = M.OrderState("O1", "v4-c", "T", "bid", 0.40, 10, 0.0, 10.0)
        o.state = M.ST_UNKNOWN
        st.orders["O1"] = o
        m = M.Maker(None, st, [])
        m.do_cancel = lambda oid: (410, {"error": "gone"})
        m.do_fills = lambda **kw: (M.FillsRead(True, 0.0), [])
        t = 1000.0
        for i in range(M.UNKNOWN_MAX_RETRIES):
            m.sweep_unknown_orders(t)
            t += M.UNKNOWN_RETRY_S
        self.assertEqual(o.state, M.ST_CLOSED)
        self.assertAlmostEqual(m.st.filled("T", "bid"), 10.0)   # conservative, never zero
        self.assertIn("T", m.st.poisoned)
        self.assertIn("unknown_order_expired", [e["event"] for e in self.events()])

    def test_D7_does_not_fire_before_the_retry_cadence(self):
        st = M.LedgerState()
        o = M.OrderState("O1", "v4-c", "T", "bid", 0.40, 10, 0.0, 10.0)
        o.state = M.ST_UNKNOWN
        st.orders["O1"] = o
        m = M.Maker(None, st, [])
        calls = []
        m.do_cancel = lambda oid: (calls.append(oid), (410, {}))[1]
        m.sweep_unknown_orders(1000.0)
        m.sweep_unknown_orders(1000.0 + M.UNKNOWN_RETRY_S - 1)
        self.assertEqual(len(calls), 1)

    def test_D8_remaining_count_is_refreshed_from_the_cancel_response(self):
        """Believing the stale placement number makes filled = remaining - reduced_by
        overstate the fill."""
        st = M.LedgerState()
        o = M.OrderState("O1", "v4-c", "T", "bid", 0.40, 10, 0.0, 10.0)
        st.orders["O1"] = o
        m = M.Maker(None, st, [])
        m.do_cancel = lambda oid: (200, {"reduced_by": "4.00", "remaining_count": "6.00"})
        m.cancel(o)
        self.assertAlmostEqual(o.remaining_count, 6.0, places=9)
        self.assertAlmostEqual(o.filled, 2.0, places=9)          # 6 - 4, not 10 - 4
        self.assertIn("remaining_count_refreshed", [e["event"] for e in self.events()])

    def test_D6_a_settled_market_is_not_marked_at_all(self):
        """C2 zeroes the position, so there is nothing left to mis-mark."""
        recs = [rec_place("O1", "T", "bid", 0.40, 20, fill=20, rem=0),
                {"k": "settlement", "t": 9000.0, "ticker": "T", "result": "yes",
                 "released_yes": 20.0, "released_no": 0.0, "cost_released": 8.0,
                 "realized_pnl": 12.0}]
        st = M.ledger_replay(recs)
        self.assertEqual(M.unpriced_positions(st.positions, {}), [])
        self.assertAlmostEqual(M.mark_to_market_pnl(st.positions, st.position_cost, {}),
                               0.0, places=9)


class Phase1_NtfyNeverPagesFromTests(unittest.TestCase):

    def test_the_env_guard_blocks_the_send(self):
        """The unit suite fired a REAL push to a phone tonight (a fixture ticker tripping
        the day stop).  A test that can reach a human is a test nobody will run."""
        self.assertTrue(os.environ.get("NTFY_DISABLE"))
        sent = []
        old = M._SESSION
        try:
            M._SESSION = type("S", (), {"post": lambda self, *a, **k: sent.append(a)})()
            M.ntfy("title", "some message about REALTICKER-26JUL28")
            self.assertEqual(sent, [])
        finally:
            M._SESSION = old

    def test_the_fixture_ticker_guard_is_independent_of_the_env_guard(self):
        sent = []
        old_env = os.environ.pop("NTFY_DISABLE", None)
        old_dry, old_sess = M.DRY, M._SESSION
        try:
            M.DRY = False
            M._SESSION = type("S", (), {"post": lambda self, *a, **k: sent.append(a)})()
            M.ntfy("LIP v4 cancel anomaly", "T http=410 - order may be LIVE")
            self.assertEqual(sent, [])                # fixture ticker: suppressed
            M.ntfy("real", "KXAAAGASD-26JUL28-4.100 http=410")
            self.assertEqual(len(sent), 1)            # a real ticker still pages
        finally:
            M.DRY, M._SESSION = old_dry, old_sess
            if old_env is not None:
                os.environ["NTFY_DISABLE"] = old_env


class InventorySlotGuarantee(_RunnerCase):
    """LIVE DEFECT (Ryan's find): the §4.6 six-market clamp de-polled markets where we HELD
    INVENTORY.  Fills are learned from cancel `reduced_by`, so a de-polled market is never
    requoted, never cancelled, its fills are never learned, and no shed is ever posted --
    the position goes invisible to our own books and strands until settlement.  Live
    evidence: the exchange showed PYPL and UST10AD-T4.65 positions while v4's snapshot
    showed no PYPL at all and no shed for either."""

    def _prog(self, tk, rho_reward=1_000_000.0):
        return {"program_id": "P-" + tk, "market_ticker": tk, "series": "KX",
                "period_reward": rho_reward, "target_size_fp": 1000.0,
                "discount_factor_bps": 5000.0, "start_ts": 0.0,
                "end_ts": 9e9, "paid_out": False}

    def _maker(self, tickers, inventory=None):
        st = M.LedgerState()
        for tk, n in (inventory or {}).items():
            st.positions[tk] = {"yes": max(0.0, n), "no": max(0.0, -n)}
            st.position_cost[tk] = abs(n) * 0.40
            st.position_cost_leg[tk] = {"yes": max(0.0, n) * 0.40,
                                        "no": max(0.0, -n) * 0.40}
        m = M.Maker(None, st, [self._prog(t) for t in tickers])
        # every market classified as usable, ranked by descending value via S
        for i, tk in enumerate(tickers):
            m.classified[tk] = {"rho": 6.25, "pinned": False, "denied": False,
                                "sides": [{"S": 10.0 + i, "p": 0.40, "qualifies": True}]}
        return m

    def test_a_market_with_inventory_is_always_polled_even_when_it_ranks_last(self):
        tickers = ["M%d" % i for i in range(10)]
        # M9 ranks LAST (largest S => lowest first-dollar rate) but holds inventory
        m = self._maker(tickers, inventory={"M9": 25.0})
        by = {t: self._prog(t) for t in tickers}
        chosen, shed_only = m.poll_set(by, 1000.0)
        self.assertIn("M9", chosen)                       # was silently dropped
        self.assertIn("M9", shed_only)                    # ... as a SHED-ONLY slot
        self.assertLessEqual(len(chosen), M.MAX_REST_MARKETS)
        # and it did not eat the whole breadth
        self.assertGreaterEqual(len(chosen) - len(shed_only), M.MIN_RANK_POLL_SLOTS)

    def test_a_shed_only_market_gets_zero_opening_allocation(self):
        """It is polled so fills are learned and the closing order tracks the book -- not so
        we can put fresh capital into a market that did not earn a slot."""
        s = M.Slot("M9", "bid", 6.25, 50.0, 0.02, denied=True)
        al, spent = M.allocate([s], 45.0, BIG)
        self.assertEqual(al[s.key], 0)
        self.assertEqual(spent, 0.0)

    def test_a_non_terminal_order_also_pins_the_market(self):
        """An unresolved order can still fill, and a fill we never learn is a position we
        never shed."""
        tickers = ["M%d" % i for i in range(10)]
        m = self._maker(tickers)
        o = M.OrderState("O1", "v4-c", "M9", "bid", 0.40, 10, 0.0, 10.0)
        m.st.orders["O1"] = o
        self.assertIn("M9", m.inventory_markets())
        chosen, _ = m.poll_set({t: self._prog(t) for t in tickers}, 1000.0)
        self.assertIn("M9", chosen)
        # an UNKNOWN order counts too
        o.remaining_count = 0.0
        o.state = M.ST_UNKNOWN
        self.assertIn("M9", m.inventory_markets())
        # a fully terminal one does not
        o.state = M.ST_CLOSED
        o.reduced_by = 10.0
        self.assertNotIn("M9", m.inventory_markets())

    def test_inventory_can_never_consume_all_of_breadth(self):
        tickers = ["M%d" % i for i in range(10)]
        inv = {t: 25.0 for t in tickers}               # every market holds inventory
        m = self._maker(tickers, inventory=inv)
        by = {t: self._prog(t) for t in tickers}
        chosen, shed_only = m.poll_set(by, 1000.0)
        self.assertLessEqual(len(chosen), M.MAX_REST_MARKETS)
        rank_slots = len([t for t in chosen if t not in shed_only])
        self.assertGreaterEqual(rank_slots, M.MIN_RANK_POLL_SLOTS)
        self.assertEqual(M.MIN_RANK_POLL_SLOTS, 2)

    def test_overflow_inventory_is_tracked_on_the_slow_cadence_never_dropped(self):
        tickers = ["M%d" % i for i in range(10)]
        inv = {t: 25.0 for t in tickers}
        m = self._maker(tickers, inventory=inv)
        by = {t: self._prog(t) for t in tickers}
        chosen, _ = m.poll_set(by, 1000.0)
        overflow = [t for t in tickers if t not in chosen]
        self.assertTrue(overflow)
        for t in overflow:
            self.assertIn(t, m.flatten_only)           # picked up by the orphan requoter
        ev = [e["event"] for e in self.events()]
        self.assertIn("inventory_slot_overflow", ev)

    def test_biggest_exposure_is_prioritised_deterministically(self):
        tickers = ["M%d" % i for i in range(10)]
        m = self._maker(tickers, inventory={"M1": 5.0, "M2": 50.0, "M3": 20.0})
        by = {t: self._prog(t) for t in tickers}
        a, _ = m.poll_set(by, 1000.0)
        b, _ = m.poll_set(by, 1000.0)
        self.assertEqual(a, b)                          # deterministic
        self.assertLess(a.index("M2"), a.index("M3"))   # 50 before 20
        self.assertLess(a.index("M3"), a.index("M1"))   # 20 before 5

    def test_a_market_with_no_live_program_but_inventory_goes_to_flatten_only(self):
        m = self._maker(["M0", "M1"], inventory={"GONE": 20.0})
        m.poll_set({"M0": self._prog("M0"), "M1": self._prog("M1")}, 1000.0)
        self.assertIn("GONE", m.flatten_only)

    def test_when_the_inventory_settles_the_slot_returns_to_rank(self):
        tickers = ["M%d" % i for i in range(10)]
        m = self._maker(tickers, inventory={"M9": 25.0})
        by = {t: self._prog(t) for t in tickers}
        chosen, shed_only = m.poll_set(by, 1000.0)
        self.assertIn("M9", shed_only)
        rank_before = len([t for t in chosen if t not in shed_only])
        # settlement (C2) zeroes the position ...
        m.st.positions["M9"] = {"yes": 0.0, "no": 0.0}
        m.st.position_cost["M9"] = 0.0
        chosen2, shed_only2 = m.poll_set(by, 2000.0)
        self.assertEqual(shed_only2, set())            # ... the shed-only slot is released
        self.assertNotIn("M9", m.inventory_markets())
        self.assertGreater(len([t for t in chosen2 if t not in shed_only2]), rank_before)

    def test_the_guarantee_and_C6_are_one_mechanism(self):
        """An ended-program shed and a de-polled live-program shed are the same problem:
        inventory nobody is watching.  Both land in flatten_only / the inventory set."""
        m = self._maker(["M0", "M1"], inventory={"GONE": 20.0})
        m.poll_set({"M0": self._prog("M0"), "M1": self._prog("M1")}, 1000.0)
        self.assertIn("GONE", m.flatten_only)
        self.assertIn("GONE", m.inventory_markets())


class Phase2_ReconPoller(_RunnerCase):
    """U7 -- the §12 pure functions existed and were tested, but NOTHING CALLED THEM: no
    paid_out poll, no recon rows, no stand-down.  Reconciliation had to be run by hand, and
    §12.3's whole point is that a loop which has silently stopped looks identical to a good
    day from the inside."""

    def _prog(self, pid="P1", tk="KXA-26JUL28-1", reward=1_000_000.0):
        return {"program_id": pid, "market_ticker": tk, "series": "KXA",
                "period_reward": reward, "start_ts": 0.0, "end_ts": 16 * 3600.0,
                "paid_out": True}

    # ---- pure layer -----------------------------------------------------------------
    def test_credits_by_program_ignores_unknown_kinds_with_a_warn(self):
        """§7.2 -- the operator leg is hand-appended, so it WILL contain a typo one day, and
        the trading path must never block on it."""
        recs = [{"kind": "credit", "program_id": "P1", "paid_usd": 5.40},
                {"kind": "competition", "event_ticker": "X", "tag": "Low"},
                {"kind": "wat", "program_id": "P2", "paid_usd": 99.0},
                {"kind": "credit", "paid_usd": 1.0}]                 # no program_id
        credits, unknown = M.credits_by_program(recs)
        self.assertEqual(credits, {"P1": 5.40})
        self.assertEqual(unknown, ["wat"])

    def test_read_jsonl_survives_a_malformed_line(self):
        path = os.path.join(self.tmp, "op.jsonl")
        with open(path, "w") as fh:
            fh.write('{"kind":"credit","program_id":"P1","paid_usd":5.4}\n')
            fh.write('this is not json\n')
            fh.write('{"kind":"credit","program_id":"P2","paid_usd":1.0}\n')
        self.assertEqual(len(M.read_jsonl(path)), 2)
        self.assertEqual(M.read_jsonl(os.path.join(self.tmp, "nope")), [])

    def test_only_credited_rows_count_as_reconciled(self):
        """A model number with no credit is not a data point, it is a MISSING one."""
        rows = [M.recon_program_row(self._prog("P1"), 5.0, 10.0, 12.0, 0.95, 3),
                M.recon_program_row(self._prog("P2", "KXA-26JUL28-2"), 4.0, 8.0, 9.0,
                                    0.9, 1)]
        days = M.daily_reconcile(rows, {"P1": 5.4})
        d = days[rows[0]["settle_date"]]
        self.assertEqual(d["n_rows"], 2)
        self.assertEqual(d["n_reconciled"], 1)
        self.assertAlmostEqual(d["paid"], 5.4, places=9)
        self.assertAlmostEqual(d["model"], 5.0, places=9)
        self.assertAlmostEqual(d["ratio"], 5.4 / 5.0, places=9)

    def test_standdown_ratio_trigger(self):
        days = {"2026-07-26": {"paid": 1.0, "model": 5.0, "ratio": 0.2, "n_reconciled": 1,
                               "n_rows": 1},
                "2026-07-27": {"paid": 1.0, "model": 6.0, "ratio": 1.0 / 6.0,
                               "n_reconciled": 1, "n_rows": 1}}
        breached, why = M.standdown_check(days)
        self.assertTrue(breached)
        self.assertIn("ratio", why)

    def test_standdown_nodata_trigger_is_independent(self):
        days = {"2026-07-26": {"paid": 0.0, "model": 0.0, "ratio": None,
                               "n_reconciled": 0, "n_rows": 3},
                "2026-07-27": {"paid": 0.0, "model": 0.0, "ratio": None,
                               "n_reconciled": 0, "n_rows": 2}}
        breached, why = M.standdown_check(days)
        self.assertTrue(breached)
        self.assertIn("zero reconcilable rows", why)

    def test_a_healthy_book_does_not_stand_down(self):
        days = {"2026-07-26": {"paid": 5.0, "model": 5.4, "ratio": 5.0 / 5.4,
                               "n_reconciled": 2, "n_rows": 2},
                "2026-07-27": {"paid": 6.0, "model": 5.0, "ratio": 1.2,
                               "n_reconciled": 2, "n_rows": 2}}
        self.assertEqual(M.standdown_check(days), (False, None))

    def test_credit_overdue_only_after_24h_and_only_when_paid_out(self):
        row = M.recon_program_row(self._prog("P1"), 5.0, 1.0, 1.0, 0.9, 0, now=1000.0)
        row["paid_out_ts"] = 1000.0
        self.assertFalse(M.credit_overdue(row, {}, now=1000.0 + 3600))
        self.assertTrue(M.credit_overdue(row, {}, now=1000.0 + 86400))
        self.assertFalse(M.credit_overdue(row, {"P1": 5.4}, now=1000.0 + 86400))
        row["paid_out_flag"] = False
        self.assertFalse(M.credit_overdue(row, {}, now=1000.0 + 86400))

    # ---- wired layer ----------------------------------------------------------------
    def _maker(self, raw_programs):
        st = M.LedgerState()
        m = M.Maker(None, st, [])
        m.accrued = {"P1": 5.0}
        m.collateral_avg = {"P1": 10.0}
        m.collateral_peak = {"P1": 12.0}
        m.fills_ct = {"P1": 3}
        self._raw = raw_programs
        return m

    def test_the_poller_writes_a_recon_row_when_paid_out_flips(self):
        m = self._maker([{"id": "P1", "market_ticker": "KXA-26JUL28-1",
                          "period_reward": 1_000_000.0, "paid_out": True,
                          "start_date": "2026-07-27T12:00:00Z",
                          "end_date": "2026-07-28T03:59:00Z"}])
        old = M.scan_programs_raw
        try:
            M.scan_programs_raw = lambda: self._raw
            m.poll_paid_out(99999.0)
        finally:
            M.scan_programs_raw = old
        rows = M.read_jsonl(M.RECON_PATH)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["program_id"], "P1")
        self.assertAlmostEqual(rows[0]["pool_usd"], 100.0, places=6)
        self.assertAlmostEqual(rows[0]["model_usd"], 5.0, places=6)
        self.assertTrue(rows[0]["paid_out_flag"])
        self.assertIn("credit_pending", [e["event"] for e in self.events()])

    def test_the_row_is_written_once_and_the_poll_is_throttled(self):
        m = self._maker([{"id": "P1", "market_ticker": "KXA-26JUL28-1",
                          "period_reward": 1_000_000.0, "paid_out": True,
                          "start_date": "2026-07-27T12:00:00Z",
                          "end_date": "2026-07-28T03:59:00Z"}])
        calls = []
        old = M.scan_programs_raw
        try:
            M.scan_programs_raw = lambda: (calls.append(1), self._raw)[1]
            m.poll_paid_out(99999.0)
            m.poll_paid_out(99999.0 + 10)                    # inside PAID_OUT_POLL_S
            self.assertEqual(len(calls), 1)
            m.poll_paid_out(99999.0 + M.PAID_OUT_POLL_S + 1)  # due again, but P1 is written
            self.assertEqual(len(calls), 1)
        finally:
            M.scan_programs_raw = old
        self.assertEqual(len(M.read_jsonl(M.RECON_PATH)), 1)

    def test_an_unflipped_program_writes_nothing(self):
        m = self._maker([{"id": "P1", "market_ticker": "KXA-26JUL28-1",
                          "period_reward": 1_000_000.0, "paid_out": False,
                          "start_date": "2026-07-27T12:00:00Z",
                          "end_date": "2026-07-28T03:59:00Z"}])
        old = M.scan_programs_raw
        try:
            M.scan_programs_raw = lambda: self._raw
            m.poll_paid_out(99999.0)
        finally:
            M.scan_programs_raw = old
        self.assertEqual(M.read_jsonl(M.RECON_PATH), [])

    def test_a_standdown_breach_halts_and_flattens(self):
        """§12.3 -- HALT DEPLOYMENT.  Not a warning: capital stops scaling until a human
        re-derives against the captured book tape."""
        os.makedirs(M.DATA_DIR, exist_ok=True)
        with open(M.RECON_PATH, "w") as fh:
            for i, day in enumerate(("2026-07-26", "2026-07-27")):
                row = M.recon_program_row(self._prog("P%d" % i, "KXA-%d" % i), 5.0,
                                          1.0, 1.0, 0.9, 0)
                row["settle_date"] = day
                fh.write(json.dumps(row) + "\n")
        with open(M.OPERATOR_PATH, "w") as fh:
            fh.write(json.dumps({"kind": "credit", "program_id": "P0",
                                 "paid_usd": 0.5}) + "\n")
            fh.write(json.dumps({"kind": "credit", "program_id": "P1",
                                 "paid_usd": 0.5}) + "\n")
        m = M.Maker(None, M.LedgerState(), [])
        m.do_cancel = lambda oid: (200, {"reduced_by": "0.00"})
        old = M.scan_programs_raw
        try:
            M.scan_programs_raw = lambda: []
            m.poll_paid_out(99999.0)
        finally:
            M.scan_programs_raw = old
        ev = [e["event"] for e in self.events()]
        self.assertIn("standdown", ev)
        self.assertTrue(m.halted)
        self.assertTrue(m.stopping)

    def test_the_overdue_credit_alert_fires_once(self):
        os.makedirs(M.DATA_DIR, exist_ok=True)
        row = M.recon_program_row(self._prog("P1"), 5.0, 1.0, 1.0, 0.9, 0, now=1.0)
        row["paid_out_ts"] = 1.0
        with open(M.RECON_PATH, "w") as fh:
            fh.write(json.dumps(row) + "\n")
        m = M.Maker(None, M.LedgerState(), [])
        old = M.scan_programs_raw
        try:
            M.scan_programs_raw = lambda: []
            m.poll_paid_out(1.0 + 2 * 86400)
            m.poll_paid_out(1.0 + 4 * 86400)
        finally:
            M.scan_programs_raw = old
        overdue = [e for e in self.events() if e["event"] == "credit_overdue"]
        self.assertEqual(len(overdue), 1)          # once, not every poll

    def test_coverage_pct_is_per_program(self):
        m = M.Maker(None, M.LedgerState(), [])
        m.slot_program = {("T", "bid"): "P1", ("U", "bid"): "P2"}
        m.at_best_s = {("T", "bid"): 95.0, ("U", "bid"): 10.0}
        m.rest_s = {("T", "bid"): 100.0, ("U", "bid"): 100.0}
        self.assertAlmostEqual(m.coverage_pct("P1"), 0.95, places=9)
        self.assertAlmostEqual(m.coverage_pct("P2"), 0.10, places=9)
        self.assertEqual(m.coverage_pct("NOPE"), 0.0)


class StartupAssertions(unittest.TestCase):

    def test_unit_assertion_refuses_a_wrong_unit(self):
        """§0.3 — refuse to run.  Exercised through the pure predicate; the network leg is
        the same predicate applied to the scanner's output."""
        self.assertTrue(M.unit_assertion_check(unit_progs(40))[0])
        self.assertFalse(M.unit_assertion_check(unit_progs(40, reward=100_000.0))[0])
        self.assertTrue(M.unit_assertion_ok(1_000_000))       # the per-program predicate
        self.assertFalse(M.unit_assertion_ok(100_000))
        self.assertTrue(M.REFUSE_ON_UNIT_MISMATCH)

    def test_data_dir_and_ledger_assertions_run_without_network(self):
        tmp = tempfile.mkdtemp()
        old_dir, old_led = M.DATA_DIR, M.LEDGER_PATH
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            ok, results = M.startup_assertions(None, "no key (dry)",
                                               programs=unit_progs(40, gas=17))
            names = {n: (g, d) for n, g, d in results}
            self.assertTrue(names["data_dir_writable"][0])
            self.assertTrue(names["ledger_replay_clean"][0])
            self.assertTrue([v for k, v in names.items() if k.startswith("unit_")][0][0])
            self.assertTrue(ok or not M.DRY)
        finally:
            M.DATA_DIR, M.LEDGER_PATH = old_dir, old_led
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_live_unit_program_refuses(self):
        tmp = tempfile.mkdtemp()
        old_dir, old_led = M.DATA_DIR, M.LEDGER_PATH
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            ok, results = M.startup_assertions(None, "n/a", programs=[])
            unit = [r for r in results if r[0].startswith("unit_")][0]
            self.assertFalse(unit[1])
            self.assertFalse(ok)
        finally:
            M.DATA_DIR, M.LEDGER_PATH = old_dir, old_led
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
