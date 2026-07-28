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
import unittest

import lip_maker_v4 as M


BIG = M.Caps(inv_cap_usd=1e9, per_market_pool_mult=1e9, per_market_budget_frac=1e9)
T = "KXAAAGASD-26JUL28-4.105"


def slot(ticker, side="bid", rho=6.25, S=50.0, p=0.40, **kw):
    return M.Slot(ticker, side, rho, S, p, **kw)


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
        rich = slot("RICH-MKT", "bid", 100.0, 5.0, 0.99, phi=0.0, d=0.0)
        cheap = slot("CHEAP-MKT", "bid", 0.10, 5.0, 0.01, phi=0.0, d=0.0)
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
        self.assertEqual(M.MAX_TOTAL_COLLATERAL_USD, 45.0)
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
        # a market with no mid available cannot be marked and must not be guessed at
        self.assertAlmostEqual(M.mark_to_market_pnl(pos, cost, {}), -4.00, places=9)

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
        self.assertEqual(M.MAX_TOTAL_COLLATERAL_USD, 45.0)
        # the bound the derivation rests on: stranded inventory is capped per slot at $10
        self.assertAlmostEqual(M.n_cap(0.40) * 0.40, 10.0, places=9)
        self.assertLessEqual(M.n_cap(0.02) * 0.02, 10.0 + 1e-9)


class StartupAssertions(unittest.TestCase):

    def test_unit_assertion_refuses_a_wrong_unit(self):
        """§0.3 — refuse to run.  Exercised through the pure predicate; the network leg is
        the same predicate applied to the scanner's output."""
        good = [{"series": "KXAAAGASD", "period_reward": 1_000_000.0}]
        bad = [{"series": "KXAAAGASD", "period_reward": 100_000.0}]
        self.assertTrue(any(M.unit_assertion_ok(p["period_reward"]) for p in good))
        self.assertFalse(any(M.unit_assertion_ok(p["period_reward"]) for p in bad))
        self.assertTrue(M.REFUSE_ON_UNIT_MISMATCH)

    def test_data_dir_and_ledger_assertions_run_without_network(self):
        tmp = tempfile.mkdtemp()
        old_dir, old_led = M.DATA_DIR, M.LEDGER_PATH
        try:
            M.DATA_DIR = os.path.join(tmp, "lip")
            M.LEDGER_PATH = os.path.join(M.DATA_DIR, "v4_ledger.jsonl")
            ok, results = M.startup_assertions(None, "no key (dry)",
                                               programs=[{"series": "KXAAAGASD",
                                                          "period_reward": 1_000_000.0}])
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
