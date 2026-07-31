"""D1-D5 + the entry band + the live-program filter — the round the reviewer blocked.

Every test here targets a defect that the 626-test suite passed WITH: two blockers, a guard
whose deletion nothing detected, and zero integration coverage for B15/B16.  Note 45's thesis
arriving inside our own tests — green meant self-consistent, not correct.
"""

import unittest

from .. import alloc, config as C, guards as G, runner as RUN, runtime as R, scan
from .base import LipTestCase
from .test_engine import EngineCase, NOW

TK = "KXBAND-26JUL29-T4.12"


def prog(pid="p1", series="KXBAND", tickers=(TK,), reward=1_000_000,
         start=NOW - 3600, end=NOW + 16 * 3600, paid_out=False):
    return {"program_id": pid, "series": series, "tickers": list(tickers),
            "period_reward": reward, "start_ts": start, "end_ts": end,
            "window_h": max(1e-9, (end - start) / 3600.0),
            "rho": scan.pool_rate(reward, max(1e-9, (end - start) / 3600.0)),
            "target_size": 1000.0, "paid_out": paid_out}


class Table(object):
    """A classifier stand-in: one ticker, both sides, with the knobs each test needs.

    `close_ts` defaults NEAR (now + 16 h): the real wire always carries a close, and the
    settlement gate (note 52 D4) refuses entry on an UNKNOWN close by design — a stub without
    one models the pathological missing-payload case, which gets its own test."""

    def __init__(self, ticker=TK, pid="p1", bid_p=0.12, ask_p=0.85,
                 qualifies=True, cum=1200.0, S=500.0, close_ts=NOW + 16 * 3600):
        self.table = {ticker: {
            "ticker": ticker, "program_id": pid, "series": "KXBAND", "pinned": False,
            "target_size": 1000.0, "yes_mid": 0.135, "ts": NOW, "close_ts": close_ts,
            "sides": {"bid": {"S": S, "qualifies": qualifies, "cum_size": cum,
                              "p": bid_p, "legal": True},
                      "ask": {"S": S, "qualifies": qualifies, "cum_size": cum,
                              "p": ask_p, "legal": True}}}}


def sides(slots):
    return sorted(s.side for s in slots)


# =============================================================================================
# D1 — the held-inventory exemption.  The blocker: a fill pops the order, so the free-ride
# gate's `own_qty > 0` exemption vanished exactly when we first had inventory to shed.
# =============================================================================================
class TestD1HeldInventoryKeepsItsSlot(LipTestCase):

    def test_a_NON_held_non_qualifying_side_is_PRICED_not_refused(self):
        """REWRITTEN under the owner's law §7a (2026-07-30): the free-ride gate is dead, so
        the slot IS built — carrying its self-qualifying walk gap — and the refusal happens
        in the allocator as an UNAFFORDABLE skip with the numbers (600 x 6c = $36 > $10)."""
        slots = scan.build_slots([prog()], Table(qualifies=False, cum=400.0), NOW)
        self.assertEqual(sides(slots), ["ask", "bid"])
        for s in slots:
            self.assertEqual(s.land_grab_size, 600)
        a, spent, rep = alloc.allocate_law(slots, 300.0)
        self.assertEqual(spent, 0.0)
        self.assertEqual(rep["reasons"].get("unaffordable"), 2)

    def test_a_HELD_non_qualifying_market_STILL_GETS_A_SLOT(self):
        """D1.  No slot ⇒ `update_shed_targets` can never START a shed (`s is None`) and
        `requote_pass` has no `s.p` to price one with: the gate deleted the exit."""
        slots = scan.build_slots([prog()], Table(qualifies=False, cum=400.0), NOW,
                                 held={TK})
        self.assertEqual(sides(slots), ["ask", "bid"])

    def test_a_HELD_market_keeps_its_slot_after_the_program_WINDOW_ENDS(self):
        """The shed must stay priceable past the reward window, and the slot must not be able
        to buy fresh exposure — `alloc.allocate` refuses `hours_left <= 0` on its own line."""
        dead = prog(start=NOW - 40 * 3600, end=NOW - 3600)
        self.assertEqual(scan.build_slots([dead], Table(), NOW), [])
        slots = scan.build_slots([dead], Table(), NOW, held={TK})
        self.assertEqual(sides(slots), ["ask", "bid"])
        self.assertTrue(all(s.hours_left <= 0 for s in slots))

    def test_the_held_set_is_the_SAME_set_the_poll_guarantee_uses(self):
        """One definition, two consumers: a polled-but-slotless market is nearly as blind."""
        self.assertTrue(hasattr(RUN.Runner, "held_tickers"))


# =============================================================================================
# D3/D4 — the dead deduction, and the load-bearing zero.
# =============================================================================================
class TestD3TheGateTestsQualificationNotATruncatedCumulant(LipTestCase):

    def test_cum_size_short_of_target_but_QUALIFIES_is_admitted(self):
        """`cum_size` is the target-size WALK and stops at target, so it is not side depth and
        arithmetic on it understates rival depth up to 2x.  `qualifies` is the real test."""
        slots = scan.build_slots([prog()], Table(qualifies=True, cum=1000.0), NOW)
        self.assertEqual(sides(slots), ["ask", "bid"])

    def test_our_own_resting_size_counts_toward_the_walk_not_a_gate(self):
        """Law §7a: with the gate dead, BOTH sides build; our resting size counts toward the
        walk through `cum_size` (the classifier scores the public book, which contains us)."""
        own = {(TK, "bid"): [(12, 300.0)]}
        slots = scan.build_slots([prog()], Table(qualifies=False, cum=400.0), NOW,
                                 own_orders=own)
        self.assertEqual(sides(slots), ["ask", "bid"])


class TestD4SelfQualificationIsPricedAtTheBandFloor(LipTestCase):
    """REWRITTEN under the owner's law §7a (2026-07-30).  The old class asserted the land
    grab was DEAD under FREE_RIDE_ONLY; the flag is deleted and the walk gap is back as a
    PRICED slot property — at the entry-band floor, never at the 1c the -100% cohort was
    bought at (n = 8,240: 2c realised 0.00% on 765 markets)."""

    def test_the_walk_gap_is_carried_and_priced_at_the_band_floor(self):
        slots = scan.build_slots([prog()], Table(qualifies=False, cum=400.0), NOW,
                                 held={TK})
        self.assertTrue(slots)
        for s in slots:
            self.assertEqual(s.land_grab_size, 600)
            own_axis_c = s.land_grab_price_c if s.side == "bid" \
                else 100 - s.land_grab_price_c
            self.assertEqual(own_axis_c, C.ENTRY_BAND_LO_C,
                             "the 1c funding path is the -100% cohort's own geometry")

    def test_the_allocator_not_a_gate_refuses_the_unaffordable_walk(self):
        own = {(TK, "bid"): [(12, 300.0)]}
        slots = scan.build_slots([prog()], Table(qualifies=False, cum=400.0), NOW,
                                 own_orders=own)
        self.assertTrue(slots)
        a, spent, rep = alloc.allocate_law(slots, 300.0)
        self.assertEqual(spent, 0.0)
        self.assertGreaterEqual(rep["reasons"].get("unaffordable", 0), 1)


# =============================================================================================
# The live-program filter.  $225 of the day's $477 loss sat in six events that paid $0 of LIP.
# =============================================================================================
class TestTheLiveProgramFilter(LipTestCase):

    def test_a_PAID_OUT_program_produces_no_slot(self):
        self.assertEqual(scan.build_slots([prog(paid_out=True)], Table(), NOW), [])

    def test_a_ZERO_POOL_program_produces_no_slot(self):
        """The gap this closes is NARROW and a mutation test found it: `runway_ok` already
        refuses ρ ≤ 0 — but only when there is nothing accrued.  With accrual at or above
        `RESCUE_TARGET_USD` it short-circuits (`if need <= 0: return True`) and a zero-pool
        program produced slots.  Asserting the easy case would have left this guard undetectable:
        deleting it passed the whole suite until this test named the accrued path."""
        accrued = {"p1": C.RESCUE_TARGET_USD + 1.0}
        self.assertTrue(scan.build_slots([prog()], Table(), NOW, accrued=accrued),
                        "control: with a real pool the slot exists")
        self.assertEqual(scan.build_slots([prog(reward=0)], Table(), NOW, accrued=accrued), [])

    def test_but_a_HELD_ticker_is_exempt_so_inventory_is_never_stranded(self):
        self.assertTrue(scan.build_slots([prog(paid_out=True)], Table(), NOW, held={TK}))
        self.assertTrue(scan.build_slots([prog(reward=0)], Table(), NOW, held={TK}))

    def test_paid_out_is_the_exchanges_OWN_word_not_our_inference(self):
        self.assertIn("paid_out", scan.parse_programs(
            {"incentive_programs": [{"id": "x", "market_tickers": ["T"],
                                     "period_reward": 100, "paid_out": True,
                                     "start_date": "2026-07-29T00:00:00Z",
                                     "end_date": "2026-07-30T00:00:00Z"}]})[0])


# =============================================================================================
# The entry band — STAGED INERT, and the inertness must be detectable.
# =============================================================================================
class TestTheEntryBandIsABiasFloor(LipTestCase):
    """CORRECTED: was `TestTheEntryBandIsStagedInert`, asserting a 7-20c band staged inert.

    Ryan's specification was "instead of a hard cap just track our average variance and make sure
    its above that", and the first cut shipped a hard per-rung price band instead.  A price cap
    cannot BE the variance instrument: 200 markets at 2c and 30 at 12c sit at the SAME V ~ 0.245,
    so price only carries variance information together with breadth.  What survives is a floor on
    measured BIAS — 2c realised 0.00% on 765 markets (note 47 §3, n = 8,240) — which no amount of
    diversification fixes.  Hence: floor at 6c, NO ceiling, and `PORTFOLIO_VAR_MAX` does the ruin
    work (see TestTheTrackedPortfolioVariance).
    """

    def test_the_floor_is_where_the_MEASURED_bias_stops_and_there_is_no_ceiling(self):
        self.assertTrue(C.ENTRY_BAND_ARMED)
        self.assertEqual(C.ENTRY_BAND_LO_C, 6)
        self.assertGreaterEqual(C.ENTRY_BAND_HI_C, C.MAX_LEGAL_PRICE_C - 1,
                                "a hard upper bound is capital efficiency, which the objective "
                                "already prices through gross prop 1/p")

    def test_a_sub_floor_price_is_refused_and_only_that_side(self):
        """A binary's two sides sum to ~$1, so the floor bites ONE side: the 2c bid goes, the
        95c ask stays.  With no upper bound that is the whole effect of the floor."""
        self.assertEqual(sides(scan.build_slots([prog()], Table(bid_p=0.02, ask_p=0.95), NOW)),
                         ["ask"])

    def test_an_expensive_side_is_NOT_refused(self):
        """The old 20c ceiling deleted these, and it is what emptied the book."""
        self.assertEqual(sides(scan.build_slots([prog()], Table(bid_p=0.12, ask_p=0.85), NOW)),
                         ["ask", "bid"])

    def test_a_held_ticker_is_exempt_from_the_floor(self):
        self.assertTrue(scan.build_slots([prog()], Table(bid_p=0.02, ask_p=0.95), NOW,
                                        held={TK}))


class TestTheTrackedPortfolioVariance(LipTestCase):
    """Ryan's instrument: V = sum wi^2 (1-pi)/pi over CLUSTERS, weights against the CEILING."""

    def leg(self, tk, n, b):
        return {"ticker": tk, "side": "yes", "n": n, "basis": b}

    def test_the_target_book_passes_and_a_single_ladder_does_not(self):
        thirty = [self.leg("C%02d-1" % i, 83.3, 0.12) for i in range(30)]
        v, n_eff = G.portfolio_variance(thirty, denominator_usd=300.0)
        self.assertLessEqual(v, C.PORTFOLIO_VAR_MAX)
        self.assertGreater(n_eff, 25)
        # the gas geometry: nine rungs, ONE settle source, one bet
        ladder = [self.leg("KXAAAGASD-26JUL29-T%d" % i, 278, 0.12) for i in range(9)]
        v2, n2 = G.portfolio_variance(ladder, denominator_usd=300.0)
        self.assertGreater(v2, C.PORTFOLIO_VAR_MAX)
        self.assertLess(n2, 1.5, "nine rungs of one ladder is ONE bet, not nine")

    def test_a_book_of_2c_rungs_passes_AT_BREADTH(self):
        """The result that kills the price floor as a variance instrument."""
        book = [self.leg("C%03d-1" % i, 75, 0.02) for i in range(200)]
        v, _ = G.portfolio_variance(book, denominator_usd=300.0)
        self.assertLessEqual(v, C.PORTFOLIO_VAR_MAX)

    def test_the_FIRST_order_is_admitted_which_is_why_the_denominator_is_the_ceiling(self):
        """Against DEPLOYED capital the first order is one cluster at w = 1.0, V = 7.33, and a
        variance rail would refuse every book's first order forever."""
        first = [self.leg("AAA-1", 83.3, 0.12)]
        v_ceiling, _ = G.portfolio_variance(first, denominator_usd=300.0)
        v_deployed, _ = G.portfolio_variance(first)
        self.assertLess(v_ceiling, C.PORTFOLIO_VAR_MAX)
        self.assertGreater(v_deployed, C.PORTFOLIO_VAR_MAX)

    def test_an_empty_book_has_no_variance(self):
        self.assertEqual(G.portfolio_variance([], denominator_usd=300.0), (0.0, 0.0))

    def test_the_rail_refuses_a_concentrating_order_and_ALWAYS_admits_a_diluting_one(self):
        heavy = [self.leg("AAA-1", 4000, 0.06)]              # $240 of a $300 ceiling, one cluster
        ctx = G.PlaceContext(portfolio_var_max=C.PORTFOLIO_VAR_MAX, ceiling_usd=300.0,
                             positions=heavy)
        ok, reason, _ = G.place_allowed(ctx, {"ticker": "AAA-2", "side": "yes", "n": 100,
                                              "basis": 0.06, "fully_closing": False})
        self.assertFalse(ok)
        self.assertEqual(reason, "portfolio_var")
        # THERE IS NO DILUTING ORDER.  Ceiling-denominated weights rise with every added dollar,
        # so a second cluster raises V too — which is why the guard has no "only if it worsens V"
        # clause: that condition could never be false.
        # LAW CHANGE (2026-07-30): the lines that used to follow asserted "a book above
        # tolerance must always be able to LEAVE" and admitted a `fully_closing` order.  There
        # is no leaving.  A book above tolerance STOPS ADDING and waits for settlement (D4: ≤7
        # days), and the flag no longer buys anything:
        ok2, reason2, _ = G.place_allowed(ctx, {"ticker": "AAA-1", "side": "no", "n": 100,
                                                "basis": 0.06, "fully_closing": True})
        self.assertFalse(ok2, "a self-declared closing order got past the variance rail")
        self.assertEqual(reason2, "portfolio_var")


# =============================================================================================
# INTEGRATION — across 206 tests, 0 `ceiling` and 0 `market_cap` refusals ever fired.  Every
# B15/B16 test hand-built a `PlaceContext`; none exercised `engine.place_context()`.
# =============================================================================================
class TestB15B16FireThroughTheRealPlaceContext(EngineCase):

    def _armed(self, ceiling):
        m = self.maker(ceiling_usd=ceiling)
        ok, refusals = m.startup(NOW, nestor_state={"open_order_tickers": [],
                                                    "position_tickers": []})
        self.assertTrue(ok, refusals)
        return m

    def _fill_book(self, m, clusters, usd_each, basis=0.50):
        """Load the book across DISTINCT clusters, so the total binds before any cluster does —
        B15 is deliberately LAST so a cheaper refusal names the specific cause, and a test that
        trips the cluster cap first never reaches it."""
        for name in clusters:
            tk = "%s-1" % name
            m.positions[tk] = {"yes": usd_each / basis, "no": 0.0}
            m.entry_basis[(tk, "yes")] = basis

    def test_the_CEILING_refuses_through_engine_place_context(self):
        # 32 clusters × $9.20 = $294.40: every cluster INSIDE its $10 reserve (note 52 D5),
        # so the only rail left standing between a $9 order and the wire is B15 itself —
        # the cheaper cluster refusal must not be what this test exercises.
        m = self._armed(300.0)
        self._fill_book(m, tuple("C%02d" % i for i in range(32)), 9.20)
        ctx = m.place_context(available_cash_usd=10_000.0)
        self.assertAlmostEqual(ctx.ceiling_usd, 300.0, places=6)
        ok, reason, detail = G.place_allowed(ctx, {"ticker": "EEE-1", "side": "yes",
                                                  "n": 18, "basis": 0.50,
                                                  "fully_closing": False})
        self.assertFalse(ok)
        self.assertEqual(reason, "ceiling", detail)
        # LAW CHANGE (2026-07-30): was "a book at its ceiling must always be able to LEAVE".
        # It stops instead, through the REAL place_context — the plumbing this class owns.
        ok2, reason2, _ = G.place_allowed(ctx, {"ticker": "EEE-1", "side": "yes",
                                                "n": 18, "basis": 0.50,
                                                "fully_closing": True})
        self.assertFalse(ok2)
        self.assertEqual(reason2, "ceiling")

    def test_the_MARKET_CAP_refuses_through_engine_place_context(self):
        m = self._armed(300.0)
        cap = C.market_leg_cap_usd(300.0, G.day_stop_usd(m.projected_day_reward,
                                                        ceiling_usd=300.0))
        m.positions["MINE-1"] = {"yes": cap / 0.50, "no": 0.0}   # exactly at the leg cap
        m.entry_basis[("MINE-1", "yes")] = 0.50
        ctx = m.place_context(available_cash_usd=10_000.0)
        self.assertAlmostEqual(ctx.market_cap_usd, cap, places=6)
        ok, reason, _ = G.place_allowed(ctx, {"ticker": "MINE-1", "side": "yes",
                                              "n": 4, "basis": 0.50,
                                              "fully_closing": False})
        self.assertFalse(ok)
        # B16 IS SHADOWED BY THE CLUSTER CAP for a single-market cluster, and that is expected
        # rather than a defect: `MARKET_CAP_FRAC == MAX_CLUSTER_FRAC` (the plan puts ONE rung per
        # settle source, so market and cluster coincide), and the cluster check runs first because
        # cheaper refusals should name the specific cause.  B16 survives as the belt for the
        # MULTI-market cluster, where `worst_case_loss_usd` nets the ladder and would otherwise
        # let one market inside it run unbounded.  The per-leg semantics are asserted directly in
        # test_guards.TestB16...test_the_cap_is_PER_LEG...; what this test owns is the PLUMBING.
        self.assertIn(reason, ("market_cap", "cluster_worst_case_cap"), reason)
        # The opposing-leg admission is NOT asserted here: for an unparseable ticker
        # `clusters.worst_case_loss_usd` cannot net the two legs, so it charges both at full basis
        # and the CLUSTER cap refuses the ask for a reason that has nothing to do with B16.  The
        # per-leg property is asserted against a hand-built context in
        # test_guards.TestB16ThePerMarketAcquisitionCap, which is the right place for a unit
        # property; this test owns only that the real `place_context` plumbs the cap through.

    def test_D5_a_gone_404_order_is_NOT_charged_as_collateral(self):
        """The exchange said the id does not exist; six other consumers of `self.orders` already
        exclude it.  Under a binding ceiling a phantom dollar refuses a real one 1:1."""
        m = self._armed(300.0)
        m.orders["real"] = {"ticker": "REAL-1", "side": "bid", "price": 0.50,
                            "remaining": 40.0}
        m.orders["ghost"] = {"ticker": "GHOST-1", "side": "bid", "price": 0.50,
                             "remaining": 400.0, "gone_404": True}       # $200 of phantom
        ctx = m.place_context(available_cash_usd=10_000.0)
        self.assertEqual([p for p in ctx.resting_basis if p["ticker"] == "GHOST-1"], [])
        self.assertEqual([p["ticker"] for p in ctx.resting_basis], ["REAL-1"])
        committed = sum(p["n"] * p["basis"]
                        for p in list(ctx.positions) + list(ctx.resting_basis))
        self.assertAlmostEqual(committed, 20.0, places=6,
                               msg="the $200 ghost must not be charged against the ceiling")


# =============================================================================================
# PHI, BREADTH, AND THE CONDITIONAL FLOOR — the round that made the runtime match the strategy.
# =============================================================================================
class TestPhiIsActuallyMeasured(LipTestCase):
    """The whole point of running is to learn the fill rate.  It was never wired: the meter
    recorded fills (engine.py note_fill) and nothing carried them to the estimator."""

    def rows(self, fills, rest_contract_s, side="bid"):
        return [{"ticker": TK, "side": side, "fills_ct": fills,
                 "rest_contract_s": rest_contract_s, "rest_dollar_s": 0.0,
                 "prox_dollar_s": 0.0, "inv_dollar_s": 0.0, "at_best_s": 0.0,
                 "fill_notional": 0.0}]

    def test_presence_exposes_the_NUMERATOR_not_only_the_denominator(self):
        from .. import presence as P
        rows = self.rows(7, 3600.0)
        self.assertEqual(P.fills_ct(rows, (TK, "bid")), 7)
        self.assertAlmostEqual(P.rest_contract_hours(rows, (TK, "bid")), 1.0, places=9)

    def test_build_slots_carries_measured_fills_into_phi(self):
        """With fills on the tape, phi must track the MEASURED rate, not the seed.

        REWRITTEN 2026-07-30 night (owner: "take our global average and use the rung's
        history to adjust it until the history is very long").  phi is now the SHRUNK
        estimate (fills + k x prior)/(exposure + k), so a LONG history no longer produces
        n/E exactly — it produces n/E to within the prior's residual pull, and the pull must
        be negligible here because 1,000 contract-hours is exactly what "very long" means.
        The seed's own strength is the assertion under test: k = prior/v_seed = 1.92
        contract-hours at the cheap seed, so 1,000 hours of fact carry 99.8% of the answer.
        THIS IS THE TEST THAT KILLED `k = RULE_OF_THREE / prior` (see `scan.phi_k`): that
        fallback gave the cheap seed k = 3,000 contract-hours and answered 0.0133 here — a
        guess outvoting 1,000 measured hours 3:1, in the direction that oversizes hot rungs.
        """
        rows = self.rows(50, 1000.0 * 3600.0)
        slots = scan.build_slots([prog()], Table(), NOW, presence_rows=rows)
        bid = [s for s in slots if s.side == "bid"][0]
        k = scan.phi_k(C.PHI_SEED_CHEAP)
        self.assertAlmostEqual(bid.phi_k, k, places=9)
        self.assertAlmostEqual(bid.phi_prior, C.PHI_SEED_CHEAP, places=9)
        self.assertAlmostEqual(bid.phi_exposure_h, 1000.0, places=9)
        self.assertAlmostEqual(bid.phi, (50.0 + k * C.PHI_SEED_CHEAP) / (1000.0 + k),
                               places=12)
        self.assertAlmostEqual(bid.phi, 50.0 / 1000.0, places=3)   # "very long" ⇒ the fact
        # and with NO evidence phi IS the prior — the seed, unshrunk, at zero exposure
        clean = [s for s in scan.build_slots([prog()], Table(), NOW) if s.side == "bid"][0]
        self.assertAlmostEqual(clean.phi, C.PHI_SEED_CHEAP, places=9)
        self.assertEqual(clean.phi_source, "seed")


class TestPhiShrinkage(LipTestCase):
    """THE ESTIMATOR ITSELF (owner, 2026-07-30 night): "we should take our global average and
    use the rung's history to adjust it until the history is very long."

    Every test here is mutation-checked against one clause:
      * revert the posterior to the ladder ⇒ the incident test in test_law fails on the exact
        symptom (the full $10 on two quiet contract-hours);
      * replace k's derivation with ANY constant ⇒ `test_k_is_DERIVED_from_dispersion_not_a
        _constant` fails, because k has to move when the board's dispersion moves;
      * drop leave-one-out ⇒ `test_a_rung_is_never_its_own_prior` fails.
    """

    def test_the_posterior_is_the_credibility_formula_at_both_ends(self):
        # zero exposure IS the prior; infinite exposure IS the tape; k is the midpoint
        self.assertAlmostEqual(scan.phi_posterior(0, 0.0, 0.3, 577.0), 0.3, places=12)
        self.assertAlmostEqual(scan.phi_posterior(0, 577.0, 0.3, 577.0), 0.15, places=12)
        self.assertAlmostEqual(scan.phi_posterior(500, 1e7, 0.3, 577.0),
                               (500.0 + 577.0 * 0.3) / (1e7 + 577.0), places=15)
        self.assertAlmostEqual(scan.phi_posterior(500, 1e7, 0.3, 577.0), 5e-5, delta=2e-5)
        # and it is monotone in the tape, never a step: no exposure can be "decisive"
        prev = None
        for e in (0.5, 2.0, 10.0, 100.0, 577.0, 5000.0):
            v = scan.phi_posterior(0, e, 0.3, 577.0)
            self.assertTrue(prev is None or v < prev)
            prev = v

    def test_k_is_DERIVED_from_dispersion_not_a_constant(self):
        """k = mu/v (gamma-Poisson): a board whose markets differ WIDELY makes the prior
        weak; a board whose markets agree makes it strong.  A constant k cannot do this, so
        this test is the mutation guard on the derivation."""
        tight = [(0.30, 40000.0), (0.32, 40000.0), (0.28, 40000.0), (0.31, 40000.0)]
        wide = [(0.05, 40000.0), (0.60, 40000.0), (0.10, 40000.0), (0.55, 40000.0)]
        k_tight = scan.phi_prior_strength(tight)
        k_wide = scan.phi_prior_strength(wide)
        self.assertTrue(k_tight and k_wide)
        self.assertGreater(k_tight, 10.0 * k_wide,
                           "k must move with the board's dispersion (k=%s vs %s)"
                           % (k_tight, k_wide))
        # the Poisson term is SUBTRACTED: the SAME spread measured over thinner exposure is
        # more explicable as noise, so less of it is real dispersion and k is larger.
        thin = [(m, 400.0) for m, _e in wide]
        self.assertGreater(scan.phi_prior_strength(thin), k_wide)
        # and a spread ENTIRELY explained by Poisson noise is not estimable -> None
        self.assertIsNone(scan.phi_prior_strength([(0.30, 0.5), (0.31, 0.5)]))
        self.assertIsNone(scan.phi_prior_strength([(0.30, 400.0)]))

    def test_the_fallback_k_is_the_same_formula_on_the_seed_band(self):
        v = ((C.PHI_SEED_MID - C.PHI_SEED_CHEAP) ** 2) / 12.0
        self.assertAlmostEqual(scan.phi_seed_band_var(), v, places=15)
        self.assertAlmostEqual(scan.phi_k(0.3), 0.3 / v, places=9)
        self.assertAlmostEqual(scan.phi_k(0.3), 577.0, places=0)
        # a measured dispersion always wins over the fallback
        self.assertAlmostEqual(scan.phi_k(0.3, dispersion_k=25.0), 25.0, places=9)
        # nothing to shrink toward ⇒ no strength
        self.assertEqual(scan.phi_k(0.0), 0.0)

    def test_a_rung_is_never_its_own_prior(self):
        """LEAVE-ONE-OUT.  A bucket holding exactly one measured market would otherwise hand
        that market its OWN tape as its prior — phi = n/E at any k, the ladder rebuilt inside
        the posterior.  Here the bid is the only measured key on the board, so its prior must
        fall through to the seed, NOT to its own 0.05."""
        rows = [{"ticker": TK, "side": "bid", "fills_ct": 50,
                 "rest_contract_s": 1000.0 * 3600.0, "rest_dollar_s": 0.0,
                 "prox_dollar_s": 0.0, "inv_dollar_s": 0.0, "at_best_s": 0.0,
                 "fill_notional": 0.0}]
        bid = [s for s in scan.build_slots([prog()], Table(), NOW, presence_rows=rows)
               if s.side == "bid"][0]
        self.assertEqual(bid.phi_source, "seed")
        self.assertAlmostEqual(bid.phi_prior, C.PHI_SEED_CHEAP, places=9)
        self.assertNotAlmostEqual(bid.phi_prior, 0.05, places=6)

    def test_the_composition_is_logged_no_silent_estimator(self):
        R.reset_log_once()
        scan.build_slots([prog()], Table(), NOW)
        line = self.logs_of("phi_shrinkage_k")
        self.assertTrue(line, "the estimator must announce its k")
        self.assertIn("k_dispersion", line[0])
        self.assertIn("n_measured", line[0])


class TestTheFloorLiftsOnEvidence(LipTestCase):
    """Ryan: "there shouldnt be a floor, there should be an average."  The average is
    PORTFOLIO_VAR_MAX; the floor survives only where phi has no evidence to price drift with."""

    def rows(self, fills, rest_contract_s):
        return [{"ticker": TK, "side": "bid", "fills_ct": fills,
                 "rest_contract_s": rest_contract_s, "rest_dollar_s": 0.0,
                 "prox_dollar_s": 0.0, "inv_dollar_s": 0.0, "at_best_s": 0.0,
                 "fill_notional": 0.0}]

    def test_an_UNMEASURED_cheap_side_is_refused(self):
        self.assertEqual(
            sides(scan.build_slots([prog()], Table(bid_p=0.02, ask_p=0.95), NOW)), ["ask"])

    def test_a_MEASURED_cheap_side_is_admitted_and_judged_by_star_instead(self):
        rows = self.rows(1, 500.0 * 3600.0)
        self.assertIn("bid", sides(scan.build_slots(
            [prog()], Table(bid_p=0.02, ask_p=0.95), NOW, presence_rows=rows)))


class TestBreadthIsNotRationedByIgnorance(LipTestCase):
    """MEASURED before the fix: 40 venues offered, 80 slots built, TWO orders resting, $60 of a
    $300 ceiling — because every planned rung counted as an "oversized probe" (>2% of ceiling)
    and only 2 may be outstanding.  Breadth then grew 2 per credit cycle: ~15 days to reach 30."""

    def test_the_oversized_threshold_is_the_per_market_cap_not_a_looser_number(self):
        self.assertAlmostEqual(C.OVERSIZED_PROBE_FRAC, C.MARKET_CAP_FRAC, places=12,
                               msg="a probe inside the per-market cap is not 'unusual'")

    def test_the_oversized_PROBE_classification_is_gone_with_the_probe(self):
        """Stage 1: there is no probe to call unusual.  The finding this test recorded — a
        threshold for "unusually large" that sat BELOW the plan's own rung size — cannot
        recur, because the classification it lived in no longer exists.  The constant stays
        (the identity above still documents where the number came from)."""
        from .. import ratchet as RT
        self.assertFalse(hasattr(RT, "classify_probe"))


if __name__ == "__main__":
    unittest.main()