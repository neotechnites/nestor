"""NOTE 52 — the presence-reserve strategy round (settled with Ryan 2026-07-29 night).

D4  the settlement gate (market close, not program window, decides entry)
D5  one rung per cluster; cluster reserve = ceiling/N
D6  the lot container; replenish re-posts the SAME lot; "fewer rungs, never smaller lots"
D11 the variance instrument lives in the PLAN, not only the rail
D12 a funded rung is never shrunk, zeroed, or evicted mid-period by re-planning

Every guard here is MUTATION-CHECKED by construction of the assertions: each test funds or
refuses through the assembled path, so deleting the guard flips the observable, not a mock.
"""

import unittest

from .. import alloc, config as C, scan
from .base import LipTestCase
from .test_d2round import Table, prog, sides
from .test_engine import NOW

RSTAR = 0.00625


def _slot(tk, cluster_series=None, p=0.12, S=100.0, rho=6.25, **kw):
    kw.setdefault("phi", 0.001)
    kw.setdefault("d", 0.0)
    kw.setdefault("l_eff", 8.0)
    kw.setdefault("hours_left", 16.0)
    kw.setdefault("window_h", 16.0)
    return alloc.Slot(tk, "bid", rho=rho, S=S, p=p,
                      venue=cluster_series or tk.split("-")[0], **kw)


class TestConfigIdentities(LipTestCase):
    """The cap stack is ONE derivation; these identities are what stops its three constants
    drifting apart silently (the B16 'replaces it' lesson, applied in advance)."""

    def test_lot_times_reserve_is_the_cluster_reserve(self):
        """lot = reserve/2: at least ONE re-post for the largest admissible lot; refills per
        rung are EMERGENT (reserve/lot − 1), not fixed — the measured board's median
        cost-to-clear ($3.68) must fit or the book starves (measured: it did)."""
        self.assertAlmostEqual(C.SLOT_LOT_CAP_USD * 2.0,
                               300.0 / C.N_TARGET_CLUSTERS, places=9)

    def test_the_entry_floor_IS_the_credit_target(self):
        self.assertAlmostEqual(C.ENTRY_FLOOR_USD,
                               C.CREDIT_TARGET_USD * C.CREDIT_TARGET_MARGIN, places=9)

    def test_the_day_stop_bound_holds_transitively(self):
        """ceiling/N ≤ 0.5 × day_stop_floor(= 0.2×ceiling) ⇔ N ≥ 10."""
        self.assertGreaterEqual(C.N_TARGET_CLUSTERS, 10)

    def test_the_inv_cap_is_the_lot_container(self):
        self.assertAlmostEqual(C.INV_CAP_USD, C.SLOT_LOT_CAP_USD, places=9)
        self.assertAlmostEqual(C.slot_cap_usd(9999.0), C.SLOT_LOT_CAP_USD, places=9)

    def test_the_horizon_grace_is_the_settlement_horizon(self):
        self.assertAlmostEqual(C.HORIZON_GRACE_H, C.SETTLE_HORIZON_H, places=9)


class TestD4SettlementGate(LipTestCase):
    def test_a_far_settling_market_is_refused_at_entry(self):
        t = Table(close_ts=NOW + (C.SETTLE_HORIZON_H + 48) * 3600)
        self.assertEqual(scan.build_slots([prog()], t, NOW), [])
        self.assertTrue(self.logs_of("settle_horizon_refused"))

    def test_an_unknown_close_REFUSES_entry(self):
        """The prog-end fallback makes a 2032 market wearing a 5-day program look NEAR —
        exactly the wrong direction for this gate, so unknown refuses."""
        t = Table(close_ts=None)
        self.assertEqual(scan.build_slots([prog()], t, NOW), [])
        self.assertTrue(self.logs_of("settle_close_unknown"))

    def test_a_near_settling_market_is_admitted(self):
        t = Table(close_ts=NOW + 24 * 3600)
        self.assertEqual(sides(scan.build_slots([prog()], t, NOW)), ["ask", "bid"])

    def test_held_is_exempt_from_both_refusals(self):
        """D1: a market we are inside is not asking an entry question — the shed and the
        requote must keep their slot whatever the close says."""
        from .test_d2round import TK
        for t in (Table(close_ts=None),
                  Table(close_ts=NOW + (C.SETTLE_HORIZON_H + 48) * 3600)):
            slots = scan.build_slots([prog()], t, NOW, held={TK})
            self.assertEqual(sides(slots), ["ask", "bid"], t.table[TK]["close_ts"])

    def test_candidates_stop_paying_classify_budget_for_known_far_closes(self):
        cl = scan.Classifier()
        p = prog()
        cl.close_ts[p["tickers"][0]] = NOW + (C.SETTLE_HORIZON_H + 48) * 3600
        self.assertEqual(cl.candidates([p], NOW), [])
        cl2 = scan.Classifier()                       # unknown close stays IN (to be learned)
        self.assertEqual(len(cl2.candidates([p], NOW)), 1)


class TestD5OneRungPerCluster(LipTestCase):
    """── D5′, 2026-07-30 (Ryan: "why shouldn\'t we make that not a requirement"). ──────────
    THE CLUSTER BOUND IS DOLLARS, NOT A RUNG COUNT.  These tests encoded the count; they now
    encode the dollars, deliberately.

    D5\'s argument was that a second rung spends the first rung\'s refill reserve — true, and
    the reserve is what `cluster_cap_usd` already enforces, on the same dollars, in the plan
    AND at the rail.  The count added nothing to the worst case (one $10 rung and four $2.50
    rungs lose the same $10 if the settle source goes against us) and cost the book a great
    deal: MEASURED, `cluster_owned` refused 270 candidates per cycle and ALL 76 pass-2
    candidates while ~$234 sat idle.  The correlation evidence is what makes the dollar bound
    sufficient — treasury tenors\' daily settle directions agreed 9/9 across pairs over 13
    settled days, so a cluster really is ONE BET, and bounding the bet in dollars is the
    honest control."""

    def test_several_rungs_of_one_cluster_are_fundable_up_to_its_DOLLARS(self):
        ss = [_slot("KXG-1-T1"), _slot("KXG-1-T2")]      # same series ⇒ one cluster
        a, spent, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 2)
        self.assertLessEqual(spent, 10.0 + 1e-9)         # the bound that survived

    def test_the_cluster_DOLLAR_cap_is_what_stops_the_third_rung(self):
        """Four rungs, one cluster, a cap that fits two lots: the cap binds, not a count."""
        ss = [_slot("KXG-1-T%d" % i) for i in range(1, 5)]
        lot = C.SLOT_LOT_CAP_USD
        a, spent, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=2 * lot)
        self.assertLessEqual(spent, 2 * lot + 1e-9)
        self.assertGreater(sum(1 for q in a.values() if q > 0), 1)

    def test_two_clusters_get_their_own_dollars_each(self):
        ss = [_slot("KXG-1-T1"), _slot("KXH-1-T1")]
        a, spent, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 2)

    def test_money_already_in_a_cluster_no_longer_EXCLUDES_its_siblings(self):
        """WAS `test_money_already_in_a_cluster_owns_it` ("the plan may grow THAT rung and no
        other").  Ownership without ACCRUAL is now just bookkeeping: the sibling is fundable
        out of whatever dollars the cluster has left.  Accrual seniority is a separate rule
        and still bites — see TestOwnerDisplacement."""
        ss = [_slot("KXG-1-T1"), _slot("KXG-1-T2")]
        a, spent, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0,
                                     resting={ss[1].key: 5.0})
        self.assertGreater(a[ss[0].key], 0, "the sibling is bounded by dollars, not banned")
        self.assertGreater(a[ss[1].key], 0)
        self.assertLessEqual(spent + 5.0 * ss[1].p, 10.0 + 1e-9)

    def test_a_zeroed_rung_frees_its_cluster(self):
        """A rung the cliff pass drops (cannot clear) hands the cluster to the next
        candidate rather than squatting on it."""
        hopeless = _slot("KXG-1-T1", rho=0.05, S=5000.0)  # side pool $0.40: unclearable
        good = _slot("KXG-1-T2")
        a, _, _ = alloc.allocate([hopeless, good], 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertEqual(a[hopeless.key], 0)
        self.assertGreater(a[good.key], 0)


class TestD6LotSemantics(LipTestCase):
    def test_the_lot_container_bounds_the_resting_order(self):
        s = _slot("KXG-1-T1", p=0.10)
        a, spent, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertLessEqual(a[s.key] * s.p, C.SLOT_LOT_CAP_USD + 1e-9)

    def test_the_replenish_reposts_the_SAME_lot_not_the_difference(self):
        """v1 §8.1's NET cap killed presence on the first fill (held ate the room).  The
        reserve semantics: the lot re-posts whole; cumulative acquisition is the cluster
        reserve's job."""
        s = _slot("KXG-1-T1", p=0.10)
        a0, _, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0)
        a1, _, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0,
                                  held={s.key: float(a0[s.key])})
        self.assertEqual(a1[s.key], a0[s.key], "the SAME lot must re-post after a fill")

    def test_the_reserve_ends_the_replenish_after_its_refills(self):
        """(1 + refills) lots of cumulative acquisition = the reserve; the next re-post is
        refused at the cluster term, cleanly, and the period ends for that rung."""
        s = _slot("KXG-1-T1", p=0.10)
        lot = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0)[0][s.key]
        held_full = float(lot * (1 + C.RUNG_REFILLS))
        a, _, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 held={s.key: held_full})
        self.assertLessEqual(a[s.key], max(0, int((10.0 - held_full * 0.10) / 0.10)))


class TestD11PlanSideVariance(LipTestCase):
    def _cheap(self, i):
        # 2c rungs across DISTINCT clusters: individually harmless, jointly the ruin book
        return _slot("KXC%02d-1-T1" % i, p=0.02, S=100.0)

    def test_a_cheap_book_is_stopped_by_the_plan_not_the_rail(self):
        ss = [self._cheap(i) for i in range(40)]
        a, spent, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0,
                                     ceiling_usd=300.0)
        funded = [k for k, q in a.items() if q > 0]
        # charged at the cluster RESERVE ($10, what a funded cluster can become), 2c carries
        # (10/300)^2 x 49 = 0.0544 of V per cluster -> the tolerance holds ~4, never 40
        self.assertLess(len(funded), 8, "the plan admitted a 2c book: no variance "
                                        "instrument in the planner")
        self.assertGreater(len(funded), 0, "the instrument must steer, not shut the book")
        v = sum((10.0 / 300.0) ** 2 * (1 - 0.02) / 0.02 for _ in funded)
        self.assertLessEqual(v, C.PORTFOLIO_VAR_MAX + 0.06)

    def test_without_a_ceiling_the_test_is_off_pure_test_compat(self):
        ss = [self._cheap(i) for i in range(40)]
        a, _, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 40)

    def test_a_mid_priced_book_is_not_blocked(self):
        ss = [_slot("KXM%02d-1-T1" % i, p=0.15) for i in range(30)]
        a, _, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0,
                                 ceiling_usd=300.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 30)

    def test_the_steering_a_dearer_rung_passes_where_a_cheap_one_was_blocked(self):
        """The whole point of plan-side: skipped ≠ refused-forever — the book's AVERAGE is
        steered by admitting the dearer candidate once cheap has eaten the tolerance."""
        cheap = [self._cheap(i) for i in range(40)]
        dear = _slot("KXDEAR-1-T1", p=0.40, S=100.0)
        a, _, _ = alloc.allocate(cheap + [dear], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 ceiling_usd=300.0)
        self.assertGreater(a[dear.key], 0, "the dear rung must pass the variance test even "
                                           "with the cheap tolerance consumed")


class TestD12PeriodLock(LipTestCase):
    def test_a_funded_rung_is_not_zeroed_by_the_cliff_pass(self):
        """Un-funded, this rung cannot clear the floor and is dropped; funded (money resting),
        it holds — zeroing cancels the order and forfeits the whole $1.00 for a fraction."""
        s = _slot("KXG-1-T1", p=0.10, S=3000.0, rho=0.60)  # floor needs > container: sub-cliff
        a0, _, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertEqual(a0[s.key], 0, "control: unfunded, the cliff pass drops it")
        a1, _, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0,
                                  resting={s.key: 20.0})
        self.assertGreaterEqual(a1[s.key], 20, "funded, the rung must hold its size (D12)")

    def test_a_funded_program_is_not_dropped_by_the_entry_floor(self):
        s = _slot("KXG-1-T1", p=0.10, S=3000.0, rho=0.60, program_id="PL")
        a, _, _, dropped = alloc.allocate_with_forfeit_gate(
            [s], 300.0, RSTAR, cluster_cap_usd=10.0, resting={s.key: 20.0})
        self.assertNotIn("PL", dropped)
        self.assertGreaterEqual(a[s.key], 20)

    def test_an_unmeasured_p_recover_cannot_evict_a_funded_rung(self):
        """The churn engine, pinned: rivals deepen the book, the floor recedes, rescue's
        p_recover defaults to 0 and ABANDON evicts a funded rung mid-period.  While the
        cliff is REACHABLE, the funded rung holds (note 49 R1: no number enters a decision
        naked)."""
        s = _slot("KXG-1-T1", p=0.10, S=3000.0, rho=1.0, program_id="PL", accrued=0.30)
        a, _, _, dropped = alloc.allocate_with_forfeit_gate(
            [s], 300.0, RSTAR, cluster_cap_usd=10.0, resting={s.key: 20.0})
        self.assertNotIn("PL", dropped)
        self.assertTrue(self.logs_of("cliff_hold_funded") or a[s.key] >= 20)

    def test_dead_accrual_still_abandons_funded_or_not(self):
        """The mirror: a cliff UNREACHABLE at the ρ/2 ceiling is a COMPUTED zero, not a
        defaulted one — the abandon stands even for a funded rung."""
        s = _slot("KXG-1-T1", p=0.10, S=3000.0, rho=0.05, hours_left=1.0,
                  program_id="PL", accrued=0.30)          # ceiling: 0.30+0.025 < $1.10
        a, _, _, dropped = alloc.allocate_with_forfeit_gate(
            [s], 300.0, RSTAR, cluster_cap_usd=10.0, resting={s.key: 20.0})
        self.assertIn("PL", dropped)


if __name__ == "__main__":
    unittest.main()


class TestRealWireFills(LipTestCase):
    """The 2026-07-30 wire dialect, tested against the CAPTURED payload verbatim
    (captured_fills_20260730.json, a real maker fill on the note-52 deploy's first order).
    The old parser read `count` (gone) and booked every real fill as ZERO contracts —
    inventory invisible to the caps, the shed, the variance rail AND the cash feed, which
    under-declared and halted nestor's divergence breaker."""

    ROW = {"action": "sell", "book_side": "ask", "count_fp": "2.97",
           "created_time": "2026-07-30T04:39:40.458649Z", "fee_cost": "0.000000",
           "fill_id": "9a499d1c-c837-67c7-7c55-67fe7c848451",
           "is_taker": False, "market_ticker": "KXUST10AD-26JUL30-T4.73",
           "no_price_dollars": "0.8700",
           "order_id": "849c8d28-6072-4fdd-8985-affa0f5ca7a1",
           "outcome_side": "no", "side": "no", "subaccount_number": 0,
           "ticker": "KXUST10AD-26JUL30-T4.73",
           "trade_id": "9a499d1c-c837-67c7-7c55-67fe7c848451",
           "ts": 1785386380, "yes_price_dollars": "0.1300"}

    def _maker(self):
        from .test_engine import EngineCase
        class T(EngineCase):
            def runTest(self):
                pass
        t = T()
        t.setUp()
        self.addCleanup(t.doCleanups)
        return t.maker()

    def test_the_captured_row_books_fractional_contracts_at_the_dollar_price(self):
        m = self._maker()
        self.assertTrue(m.book_fill_row(dict(self.ROW), NOW))
        pos = m.positions["KXUST10AD-26JUL30-T4.73"]
        self.assertAlmostEqual(pos["no"], 2.97, places=6)   # ask-shaped: acquires NO
        self.assertAlmostEqual(m.position_cost["KXUST10AD-26JUL30-T4.73"],
                               2.97 * 0.87, places=6)       # collateral at 1−0.13
        self.assertAlmostEqual(m.cash.fees_paid, 0.0, places=9)

    def test_a_charged_fee_is_booked_from_fee_cost(self):
        m = self._maker()
        row = dict(self.ROW, fee_cost="0.160000", trade_id="fee-1", fill_id="fee-1")
        self.assertTrue(m.book_fill_row(row, NOW))
        self.assertAlmostEqual(m.cash.fees_paid, 0.16, places=9)

    def test_a_zero_count_row_is_a_non_event_and_does_not_consume_its_id(self):
        """The heal path depends on this: the ids the broken parser saw at count 0 must
        stay bookable when the same fills are re-read with true counts."""
        m = self._maker()
        z = dict(self.ROW, count_fp="0.00")
        self.assertFalse(m.book_fill_row(z, NOW))
        self.assertTrue(m.book_fill_row(dict(self.ROW), NOW),
                        "the true reading of the same fill_id must still book")

    def test_dedupe_still_holds_on_the_true_row(self):
        m = self._maker()
        self.assertTrue(m.book_fill_row(dict(self.ROW), NOW))
        self.assertFalse(m.book_fill_row(dict(self.ROW), NOW))

    def test_book_side_attributes_an_orderless_row(self):
        """Crash-gap case: no order in our books; `book_side` names our side directly."""
        m = self._maker()
        row = dict(self.ROW, order_id="unknown-1", trade_id="bs-1", fill_id="bs-1")
        self.assertTrue(m.book_fill_row(row, NOW))
        self.assertAlmostEqual(
            m.positions["KXUST10AD-26JUL30-T4.73"]["no"], 2.97, places=6)


class TestPostFillCooldown(LipTestCase):
    """Two burst-halts in one night (TRUMPSAY 23:47, APRPOTUS 00:54): the replenish
    re-posted the instant a fill booked, straight back into the flow that ate it, and B14
    halted the WHOLE book both times.  The cooldown lets the flow pass; exits are exempt."""

    def _armed(self):
        from .test_engine import EngineCase
        from .test_runner import NOW as RNOW, program_body
        from .. import exchange as X, runner as RUN

        class T(EngineCase):
            def runTest(self):
                pass
        t = T()
        t.setUp()
        self.addCleanup(t.doCleanups)
        tk = "KXAAAGASD-26JUL29-T4.12"
        ex = X.FakeExchange(balance_cents=1_000_000, now=RNOW)
        ex.books[tk] = {"orderbook": {"orderbook_fp": {
            "yes_dollars": [["0.06", "1200"]], "no_dollars": [["0.93", "1200"]]}}}
        ex._programs = program_body(tickers=(tk,))
        ex.programs = lambda cursor=None: (200, ex._programs)
        ex.market_closes[tk] = RNOW + 16 * 3600
        m = t.maker(ex=ex)
        r = RUN.Runner(m, sleep=lambda _s: None)
        r.init(RNOW, nestor_state={"open_order_tickers": [], "position_tickers": []})
        r.iteration(RNOW + 1)
        self.assertTrue(m.orders, "fixture never armed")
        return r, ex, tk, RNOW

    def test_a_fill_starts_the_cooldown_and_phi_judges_the_return(self):
        """WAS asserting the replenish returns after the cooldown — that pass was an
        artifact: the second placement it saw was the (now-removed) auto-shed.  The true
        behavior: a lot eaten within seconds measures a huge φ, and (★) refuses to re-enter
        the rung this period — the φ discipline, not a stuck requoter.  The cooldown's own
        job (nothing re-posts inside the window) still asserts."""
        r, ex, tk, NOW_ = self._armed()
        oid = list(r.m.orders)[0]
        ex.take(oid, 999, now=NOW_ + 2)                   # the flow eats the whole lot
        r.iteration(NOW_ + 3)                             # fill books via the fills poll
        placed_before = len(ex.placed)
        for k in range(4, 30):
            r.iteration(NOW_ + k)                         # inside the 90s window
        self.assertEqual(len(ex.placed), placed_before,
                         "the replenish re-posted INSIDE the cooldown")
        for k in range(0, 40):
            r.iteration(NOW_ + 95 + k)                    # past the window
        self.assertEqual(len(ex.placed), placed_before,
                         "a rung whose lot was eaten in 2s must be φ-refused, not re-fed")
        self.assertFalse(r.m.halt.halted)

    def test_the_burst_breaker_is_unreachable_through_the_replenish(self):
        """The property that ends the halt class: even a flow that eats every lot on
        contact cannot draw 3 placements in 60s out of the requoter."""
        r, ex, tk, NOW_ = self._armed()
        t = NOW_ + 2
        for _ in range(6):                                # eat every lot, immediately
            for oid in list(ex.resting):
                ex.take(oid, 999, now=t)
            t += 1.0
            r.iteration(t)
        self.assertFalse(r.m.halt.halted,
                         "the burst breaker fired: the cooldown is not binding")


class TestClusterOwnershipSeed(LipTestCase):
    """The 1.155 incident (2026-07-30): v5 held 3 lots of EURUSD 1.155 with $0.26 accrued in
    its pool and funded sibling rung 1.153 from zero — the held rung produced no slot that
    cycle (post-restart classification gap) and slot-derived ownership let the sibling take
    the cluster's seat.  Ownership now seeds from the REAL book via `owner_seed`."""

    def test_a_held_but_slotless_rung_keeps_its_cluster_only_via_ACCRUAL(self):
        """REWRITTEN 2026-07-30 (D5′).  Ownership alone no longer excludes a sibling — the
        cluster\'s DOLLARS do the bounding — so the 1.155 incident\'s protection now rests
        entirely on the accrual rank: with a pot, the sibling is recalled; without one, it is
        funded out of the cluster\'s remaining room.
        ⚠ FLAG: in the live engine the slotless holder\'s dollars ARE inside `cluster_seed`,
        so the cap sees them.  This pure-test path passes no seed, so the sibling gets the
        whole $10 — the same shape as the incident, minus the accrual that made it costly."""
        sibling = _slot("KXEUR-1-T1153")
        # A reserve of exactly ONE lot: the slotless owner and the sibling want the same
        # dollars, so the pot decides.  (With the full $10 reserve both fit and neither is
        # touched — see TestRecallRequiresScarcity.)
        common = dict(cluster_cap_usd=C.SLOT_LOT_CAP_USD,
                      owner_seed={"KXEUR": ("KXEUR-1-T1155", "bid")})
        a_no_pot, _, _ = alloc.allocate([sibling], 300.0, RSTAR, **common)
        self.assertGreater(a_no_pot[sibling.key], 0)
        a_pot, _, _ = alloc.allocate([sibling], 300.0, RSTAR,
                                     owner_accrued={"KXEUR": 0.26}, **common)
        self.assertEqual(a_pot[sibling.key], 0, "accrual seniority must still recall it")

    def test_the_owner_rung_itself_still_funds(self):
        owner = _slot("KXEUR-1-T1155")
        a, _, _ = alloc.allocate([owner], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 owner_seed={"KXEUR": owner.key})
        self.assertGreater(a[owner.key], 0)

    def test_D9_IS_RETIRED_the_other_leg_is_now_quotable(self):
        """⚠ FLAG — A DEFERRED DECISION ARRIVED AS A SIDE EFFECT.  D9 ("one SIDE per cluster
        — one-sided for now") was enforced by the SAME owner check that enforced the rung
        count, so retiring the count retired D9 with it.  Both legs of one market now rest
        together, bounded by the cluster\'s dollars.

        The economics favour it — the filing normalises scores WITHIN EACH SIDE, so a
        one-sided quote can earn at most half a pool and a two-sided one addresses both — and
        the legs cannot cross (a YES bid at p and a NO bid at p′ are a YES ask at 1−p′; they
        cross only if p + p′ > 1, which is a box trade in OUR favour).  But it was deferred
        on purpose, and it should be RATIFIED rather than inherited."""
        bid = _slot("KXEUR-1-T1155")
        ask = alloc.Slot("KXEUR-1-T1155", "ask", rho=6.25, S=100.0, p=0.12,
                         phi=0.001, d=0.0, l_eff=8.0, hours_left=16.0, window_h=16.0,
                         venue="KXEUR")
        a, spent, _ = alloc.allocate([bid, ask], 300.0, RSTAR, cluster_cap_usd=10.0,
                                     owner_seed={"KXEUR": bid.key})
        self.assertGreater(a[bid.key], 0)
        self.assertGreater(a[ask.key], 0)
        self.assertLessEqual(spent, 10.0 + 1e-9)         # still one cluster, still $10


class TestOwnerDisplacement(LipTestCase):
    """Ryan, 2026-07-30: "1.153 has earned one cent, 1.155 has earned 26 — cancel 1.153 and
    open 1.155."  Accrued credit is banked EV and outranks a sibling's resting order: the
    displaced rung's seed is withheld so the requoter's q=0 path recalls it."""

    def test_a_resting_sibling_is_displaced_by_an_accrued_owner(self):
        sib = _slot("KXEUR-1-T1153", accrued=0.01)
        a, _, _ = alloc.allocate([sib], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 resting={sib.key: 62.0},
                                 owner_seed={"KXEUR": ("KXEUR-1-T1155", "bid")},
                                 owner_accrued={"KXEUR": 0.26})
        self.assertEqual(a[sib.key], 0, "the displaced rung must be recalled, not kept")

    def test_no_displacement_without_owner_accrual(self):
        """An owner with nothing banked does not evict a funded sibling (D12 holds)."""
        sib = _slot("KXEUR-1-T1153")
        a, _, _ = alloc.allocate([sib], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 resting={sib.key: 62.0},
                                 owner_seed={"KXEUR": ("KXEUR-1-T1155", "bid")})
        self.assertGreaterEqual(a[sib.key], 62)

    def test_no_displacement_when_the_sibling_banked_more(self):
        sib = _slot("KXEUR-1-T1153", accrued=0.50)
        a, _, _ = alloc.allocate([sib], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 resting={sib.key: 62.0},
                                 owner_seed={"KXEUR": ("KXEUR-1-T1155", "bid")},
                                 owner_accrued={"KXEUR": 0.26})
        self.assertGreaterEqual(a[sib.key], 62)


class TestRecallRequiresScarcity(LipTestCase):
    """THE LIVE CANCEL WAVE, 2026-07-30 — the regression that must never come back.

    D5\' retired the one-rung-per-cluster COUNT, and the accrual recall that survived it fired
    on RANK ALONE: `owner_accrued[cluster] > slot.accrued`.  The estimates feed credits ONE
    owner per cluster, so nearly every resting rung read as "poorer than the owner":
    `pass2_refused` showed owner_recalled on 94 of 105 candidates and the requoter\'s q=0 path
    cancelled the ENTIRE resting book, ~10 rungs.  No fills and no losses — and presence at
    zero, which is the only thing this program sells.

    THE ERROR WAS CATEGORY, NOT DEGREE.  "1.153 has earned one cent, 1.155 has earned 26 —
    cancel 1.153, open 1.155" answers a question that only EXISTS when the two cannot both be
    funded.  Under D5\' the cluster is bounded by dollars, so when the reserve affords both,
    BOTH REST — that is the feature.  Accrual rank decides who wins a contest; it does not
    create one.
    """

    def rungs(self, sib_p=0.10):
        """Owner and sibling in one cluster at 10c, so the dollars are readable directly."""
        owner = _slot("KXEUR-1-T1155", p=0.10, S=100.0, rho=6.25, accrued=0.30)
        sib = _slot("KXEUR-1-T1153", p=sib_p, S=100.0, rho=6.25, accrued=0.0)
        return owner, sib

    def test_a_reserve_that_affords_BOTH_recalls_nothing(self):
        """(a) $10 reserve; the owner already rests $5 of it; the sibling wants a $4 lot.
        Nine dollars of a ten dollar reserve: no contest, no cancel, both rest."""
        owner, sib = self.rungs()
        a, _, _ = alloc.allocate([owner, sib], 300.0, RSTAR,
                                 caps=alloc.Caps(inv_cap_usd=4.0),
                                 cluster_cap_usd=10.0,
                                 resting={owner.key: 50.0},          # 50 x $0.10 = $5.00
                                 owner_seed={"KXEUR": owner.key},
                                 owner_accrued={"KXEUR": 0.30})
        self.assertGreater(a[sib.key], 0, "the sibling was cancelled with room to spare")
        self.assertGreaterEqual(a[owner.key], 50)
        self.assertEqual(self.logs_of("rung_recalled"), [])

    def test_a_reserve_that_cannot_fund_the_OWNER_recalls_the_poorer_rung(self):
        """(b) The owner\'s floor-clearing lot is $8 and the sibling rests $5 of a $10
        reserve: keeping the sibling really does deny the owner, so the pot wins and the
        freed dollars land on it."""
        owner = _slot("KXEUR-1-T1155", p=0.10, S=720.0, rho=1.0, hours_left=24.0,
                      window_h=24.0, accrued=0.30)
        sib = _slot("KXEUR-1-T1153", p=0.10, S=100.0, rho=6.25, accrued=0.0)
        self.assertAlmostEqual(alloc.cliff_clearing_q(owner) * owner.p, 8.00, places=6)
        a, _, _ = alloc.allocate([owner, sib], 300.0, RSTAR,
                                 caps=alloc.Caps(inv_cap_usd=8.0),
                                 cluster_cap_usd=10.0,
                                 resting={sib.key: 50.0},            # 50 x $0.10 = $5.00
                                 owner_seed={"KXEUR": owner.key},
                                 owner_accrued={"KXEUR": 0.30})
        self.assertEqual(a[sib.key], 0, "the poorer rung must yield the contested dollars")
        self.assertGreater(a[owner.key], 0, "…and the owner must actually get them")

    def test_the_recall_says_who_took_the_seat_and_what_it_cost(self):
        """The wave was diagnosed from a counter that could only say `owner_recalled`.  A
        recall CANCELS A LIVE ORDER; one journal line must carry both accruals and the
        dollars in dispute."""
        owner = _slot("KXEUR-1-T1155", p=0.10, S=720.0, rho=1.0, hours_left=24.0,
                      window_h=24.0, accrued=0.30)
        sib = _slot("KXEUR-1-T1153", p=0.10, S=100.0, rho=6.25, accrued=0.02)
        alloc.allocate([owner, sib], 300.0, RSTAR, caps=alloc.Caps(inv_cap_usd=8.0),
                       cluster_cap_usd=10.0, resting={sib.key: 50.0},
                       owner_seed={"KXEUR": owner.key}, owner_accrued={"KXEUR": 0.30})
        rows = self.logs_of("rung_recalled")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["ticker"], r["side"]), ("KXEUR-1-T1153", "bid"))
        self.assertEqual((r["kept"], r["kept_side"]), ("KXEUR-1-T1155", "bid"))
        self.assertAlmostEqual(r["recalled_accrued"], 0.02, places=6)
        self.assertAlmostEqual(r["kept_accrued"], 0.30, places=6)
        self.assertAlmostEqual(r["cluster_cap_usd"], 10.0, places=6)
        self.assertAlmostEqual(r["sibling_claim_usd"], 5.00, places=6)
        self.assertAlmostEqual(r["owner_need_usd"], 8.00, places=6)
        self.assertAlmostEqual(r["freed_usd"], 5.00, places=6)
        self.assertLess(r["room_for_owner_usd"], r["owner_need_usd"])

    def test_a_POSITION_is_never_recalled_because_a_cancel_frees_nothing(self):
        """Held inventory rides.  A rung that holds and rests nothing cannot resolve any
        contest, so ranking it against the owner is meaningless."""
        owner, sib = self.rungs()
        a, _, _ = alloc.allocate([owner, sib], 300.0, RSTAR,
                                 caps=alloc.Caps(inv_cap_usd=8.0),
                                 cluster_cap_usd=10.0,
                                 held={sib.key: 50.0},
                                 owner_seed={"KXEUR": owner.key},
                                 owner_accrued={"KXEUR": 0.30})
        self.assertEqual(self.logs_of("rung_recalled"), [])

    def test_the_whole_book_is_not_cancelled_when_every_cluster_has_room(self):
        """The wave\'s exact shape: ten rungs, ten clusters, one credited owner each, every
        reserve roomy.  Zero recalls."""
        slots, seeds, accs, rest = [], {}, {}, {}
        for i in range(10):
            ck = "KXV%d" % i
            owner = _slot("%s-1-T155" % ck, p=0.10, S=100.0, rho=6.25, accrued=0.30)
            sib = _slot("%s-1-T153" % ck, p=0.10, S=100.0, rho=6.25, accrued=0.0)
            slots += [owner, sib]
            seeds[ck] = owner.key
            accs[ck] = 0.30
            rest[sib.key] = 20.0                                     # $2.00 resting each
        a, _, _ = alloc.allocate(slots, 300.0, RSTAR, caps=alloc.Caps(inv_cap_usd=4.0),
                                 cluster_cap_usd=10.0, resting=rest,
                                 owner_seed=seeds, owner_accrued=accs)
        self.assertEqual(self.logs_of("rung_recalled"), [])
        self.assertEqual(sum(1 for k in rest if a.get(k, 0) > 0), 10,
                         "the resting book was cancelled: the wave is back")


class TestOwnerRanksByAccruedDollars(LipTestCase):
    """Ryan: 1c may not tie with 26c — the AMOUNT is the weight.  And a program whose only
    claim is banked accrual (position predating the state archive) still owns its cluster,
    side-wildcard."""

    def test_the_bigger_pot_wins_over_bigger_committed_basis_WHEN_CONTESTED(self):
        """Ryan\'s ordering is intact — the pot outranks the stake — but it now answers a
        question that has to EXIST first: the reserve here holds $7.44 of sibling against a
        cap that cannot also fund the owner\'s floor-clearing lot."""
        rich_pot = _slot("KXEUR-1-T1155", accrued=0.26)
        big_stake = _slot("KXEUR-1-T1153", accrued=0.01)
        a, _, _ = alloc.allocate([rich_pot, big_stake], 300.0, RSTAR, cluster_cap_usd=7.5,
                                 resting={big_stake.key: 62.0},   # 62 x $0.12 = $7.44
                                 owner_seed={"KXEUR": rich_pot.key},
                                 owner_accrued={"KXEUR": 0.26})
        self.assertEqual(a[big_stake.key], 0, "the 1c rung must be displaced")
        self.assertGreater(a[rich_pot.key], 0, "the 26c rung must be funded")
        row = self.logs_of("rung_recalled")[0]
        self.assertEqual(row["ticker"], "KXEUR-1-T1153")
        self.assertEqual(row["kept"], "KXEUR-1-T1155")
        self.assertAlmostEqual(row["recalled_accrued"], 0.01, places=6)
        self.assertAlmostEqual(row["kept_accrued"], 0.26, places=6)

    def test_the_bigger_pot_takes_NOTHING_when_the_reserve_fits_both(self):
        """The live wave, in one assertion: the same ranking, a reserve that affords both,
        and no cancel."""
        rich_pot = _slot("KXEUR-1-T1155", accrued=0.26)
        big_stake = _slot("KXEUR-1-T1153", accrued=0.01)
        a, _, _ = alloc.allocate([rich_pot, big_stake], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 resting={big_stake.key: 62.0},
                                 owner_seed={"KXEUR": rich_pot.key},
                                 owner_accrued={"KXEUR": 0.26})
        self.assertGreaterEqual(a[big_stake.key], 62)
        self.assertGreater(a[rich_pot.key], 0)
        self.assertEqual(self.logs_of("rung_recalled"), [])

    def test_a_wildcard_owner_admits_either_side_of_its_ticker(self):
        bid = _slot("KXEUR-1-T1155")
        a, _, _ = alloc.allocate([bid], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 owner_seed={"KXEUR": ("KXEUR-1-T1155", None)},
                                 owner_accrued={"KXEUR": 0.26})
        self.assertGreater(a[bid.key], 0, "the wildcard owner's own rung was refused")

    def test_a_wildcard_owner_does_NOT_block_a_sibling_the_reserve_can_afford(self):
        """REWRITTEN 2026-07-30 after the live cancel wave — was
        `test_a_wildcard_owner_still_blocks_siblings`.  Outranking is not a reason to cancel:
        with a $10 reserve, a slotless owner needing one $5 lot and a sibling taking one $5
        lot, NOTHING is contested and both rest.  The old assertion is the incident's shape."""
        sib = _slot("KXEUR-1-T1153")
        a, _, _ = alloc.allocate([sib], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 owner_seed={"KXEUR": ("KXEUR-1-T1155", None)},
                                 owner_accrued={"KXEUR": 0.26})
        self.assertGreater(a[sib.key], 0)
        self.assertEqual(self.logs_of("rung_recalled"), [])

    def test_a_wildcard_owner_DOES_recall_when_the_reserve_holds_only_one_lot(self):
        """The same pair, one lot of room: now the dollars really are claimed twice."""
        sib = _slot("KXEUR-1-T1153")
        a, _, _ = alloc.allocate([sib], 300.0, RSTAR, cluster_cap_usd=C.SLOT_LOT_CAP_USD,
                                 owner_seed={"KXEUR": ("KXEUR-1-T1155", None)},
                                 owner_accrued={"KXEUR": 0.26})
        self.assertEqual(a[sib.key], 0)
        self.assertTrue(self.logs_of("rung_recalled"))


class TestDisplacementCoversTheRescue(LipTestCase):
    """The 3-lot leak: displacement withheld the resting seed but the RESCUE path topped the
    displaced rung back up through the forfeit gate.  Same rule, same seniority, both doors."""

    def test_a_displaced_program_is_not_rescued(self):
        sib = _slot("KXEUR-1-T1153", accrued=0.01, S=3000.0, rho=0.60, p=0.10,
                    program_id="P153")
        a, _, _, dropped = alloc.allocate_with_forfeit_gate(
            [sib], 300.0, RSTAR, cluster_cap_usd=10.0,
            owner_seed={"KXEUR": ("KXEUR-1-T1155", "bid")},
            owner_accrued={"KXEUR": 0.26})
        self.assertEqual(a[sib.key], 0, "the displaced rung was rescued back in")
        self.assertNotIn("P153", dropped)


class TestExchangeEstimatesFeed(LipTestCase):
    """SF-4c — the /v1 estimates feed (Ryan found it; the trading key signs /v1).  The
    exchange's per-program accrued number re-anchors self.accrued each poll; the model
    interpolates between polls; restarts replay TRUTH via accrual rows."""

    def _maker(self):
        from .test_engine import EngineCase
        class T(EngineCase):
            def runTest(self):
                pass
        t = T()
        t.setUp()                             # its setUp re-points the log sink at ITS list
        self.logs = t.logs                    # share it, so self.logs_of() sees the capture
        self.addCleanup(t.doCleanups)
        return t.maker()

    def test_the_poll_reanchors_accrued_in_dollars(self):
        import unittest.mock as mock
        m = self._maker()
        m.accrued["p-155"] = 0.063                    # the model's wrong number
        m.ex.estimates_rows = [{"program_id": "p-155", "reward_centicents": 2553},
                               {"program_id": "p-153", "reward_centicents": 244}]
        with mock.patch.object(C, "KALSHI_USER_ID", "u-1"):
            n = m.poll_estimates(1000.0)
        self.assertEqual(n, 2)
        self.assertAlmostEqual(m.accrued["p-155"], 0.2553, places=6)
        self.assertAlmostEqual(m.accrued["p-153"], 0.0244, places=6)

    def test_truth_persists_as_accrual_rows_for_replay(self):
        import unittest.mock as mock
        m = self._maker()
        m.ex.estimates_rows = [{"program_id": "p-1", "reward_centicents": 2600}]
        with mock.patch.object(C, "KALSHI_USER_ID", "u-1"):
            m.poll_estimates(1000.0)
        rows = [r for r in m.ledger.read()
                if (r.get("k") or r.get("kind")) == "accrual"
                and r.get("src") == "exchange_estimates"]
        self.assertTrue(rows)
        self.assertAlmostEqual(rows[-1]["accrued"], 0.26, places=6)

    def test_no_user_id_is_loud_not_silent(self):
        import unittest.mock as mock
        from .. import runtime as RT_
        m = self._maker()
        RT_._LOGGED_ONCE.clear()                     # log_once dedupes per process; reset for capture
        with mock.patch.object(C, "KALSHI_USER_ID", None):
            self.assertEqual(m.poll_estimates(1000.0), 0)
        self.assertTrue(self.logs_of("estimates_unwired"))

    def test_the_cadence_holds(self):
        import unittest.mock as mock
        m = self._maker()
        m.ex.estimates_rows = [{"program_id": "p-1", "reward_centicents": 100}]
        with mock.patch.object(C, "KALSHI_USER_ID", "u-1"):
            m.poll_estimates(1000.0)
            self.assertEqual(m.poll_estimates(1030.0), 0)   # inside 60s: no re-poll


class TestReplayIsGone(LipTestCase):
    """STAGE 5, 2026-07-30 — `TestBookReinstatement` and its whole feature are deleted.

    SF-4d snapshotted the resting book and re-placed it on init, so the book became a
    function of what the book used to be: two processes on the same world would quote
    differently depending on which one had a snapshot.  Ryan's concept forbids exactly this —
    "it comes to the exact same conclusions it had, and places all the same orders, as a
    symptom of how it works, not as a directed rule."

    The motivation was real (capital deployed in minutes, not hours) and is answered from the
    other side: derivation must be fast and complete enough to reproduce the book by itself.
    The acceptance test in test_convergence.py is what holds that promise now.  Memory of the
    WORLD survives untouched — the close cache is still persisted, still flushed on age."""

    def test_the_runner_cannot_replay_a_book(self):
        from .. import runner as RUN
        for gone in ("reinstate", "reinstate_pass", "pending_reinstate"):
            self.assertFalse(hasattr(RUN.Runner, gone), gone)

    def test_the_cycle_persists_no_resting_book(self):
        from .. import engine as E
        import inspect
        src = inspect.getsource(E.Maker.cycle)
        self.assertNotIn("BOOK_SNAPSHOT_PATH", src)

    def test_world_memory_is_UNTOUCHED(self):
        """The line the concept draws: the close cache is a fact about markets, not about
        us, and it stays."""
        from .. import scan
        self.assertTrue(hasattr(scan.Classifier, "_persist_closes"))


from .. import runtime as R_


class TestClosingFillsReplayAsClosing(LipTestCase):
    """The −$78 Skubal short: a shed's fill replayed as OPENING the order's side, so a
    closing sell of held YES became a fresh NO short, both legs stacked, inventory_basis
    hit $315 of a $300 ceiling and the budget starved to zero.  fill_obs rows now carry
    closing/closed_leg and the replay reduces the held leg."""

    def test_a_closing_fill_row_reduces_the_held_leg(self):
        from .. import cutover as CU
        rows = [
            {"k": "adopt", "ticker": "T", "side": "yes", "net": 26.0, "basis": 0.16},
            {"k": "fill_obs", "ticker": "T", "side": "ask", "count": 26.0,
             "price_c": 3, "fill_id": "f1", "order_id": "o9",
             "closing": True, "closed_leg": "yes"},
        ]
        st = CU.V4Positions().replay(rows)
        got = {(r["ticker"], r["side"]): r["net"] for r in st.rows()}
        self.assertNotIn(("T", "no"), got, "the closing sell replayed as a NO short")
        self.assertLessEqual(got.get(("T", "yes"), 0.0), 1e-9)

    def test_a_plain_opening_fill_still_opens(self):
        from .. import cutover as CU
        rows = [
            {"k": "place_resp", "order_id": "o1", "ticker": "T", "side": "bid",
             "price": 0.06, "size": 40, "remaining_count": "40.00"},
            {"k": "fill_obs", "ticker": "T", "side": "bid", "count": 40.0,
             "price_c": 6, "fill_id": "f2", "order_id": "o1", "closing": False},
        ]
        st = CU.V4Positions().replay(rows)
        got = {(r["ticker"], r["side"]): r["net"] for r in st.rows()}
        self.assertAlmostEqual(got.get(("T", "yes"), 0.0), 40.0, places=6)
