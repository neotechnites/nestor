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
    def test_a_second_rung_in_the_same_cluster_is_refused(self):
        ss = [_slot("KXG-1-T1"), _slot("KXG-1-T2")]      # same series ⇒ one cluster
        a, spent, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 1)

    def test_two_clusters_get_one_rung_each(self):
        ss = [_slot("KXG-1-T1"), _slot("KXH-1-T1")]
        a, spent, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 2)

    def test_money_already_in_a_cluster_owns_it(self):
        """held/resting money makes its key the owner: the plan may grow THAT rung and no
        other in the cluster."""
        ss = [_slot("KXG-1-T1"), _slot("KXG-1-T2")]
        a, _, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0,
                                 resting={ss[1].key: 5.0})
        self.assertEqual(a[ss[0].key], 0, "the un-owned rung must not be funded")
        self.assertGreater(a[ss[1].key], 0)

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

    def test_a_held_but_slotless_rung_still_owns_its_cluster(self):
        sibling = _slot("KXEUR-1-T1153")
        a, _, _ = alloc.allocate([sibling], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 owner_seed={"KXEUR": ("KXEUR-1-T1155", "bid")})
        self.assertEqual(a[sibling.key], 0,
                         "the sibling took a cluster whose rung is merely unclassified")

    def test_the_owner_rung_itself_still_funds(self):
        owner = _slot("KXEUR-1-T1155")
        a, _, _ = alloc.allocate([owner], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 owner_seed={"KXEUR": owner.key})
        self.assertGreater(a[owner.key], 0)

    def test_one_side_per_cluster_still_holds_under_ticker_seeding(self):
        """D9: the owner key carries a SIDE — the same ticker's other leg is refused."""
        bid = _slot("KXEUR-1-T1155")
        ask = alloc.Slot("KXEUR-1-T1155", "ask", rho=6.25, S=100.0, p=0.12,
                         phi=0.001, d=0.0, l_eff=8.0, hours_left=16.0, window_h=16.0,
                         venue="KXEUR")
        a, _, _ = alloc.allocate([bid, ask], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 owner_seed={"KXEUR": bid.key})
        self.assertGreater(a[bid.key], 0)
        self.assertEqual(a[ask.key], 0)


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


class TestOwnerRanksByAccruedDollars(LipTestCase):
    """Ryan: 1c may not tie with 26c — the AMOUNT is the weight.  And a program whose only
    claim is banked accrual (position predating the state archive) still owns its cluster,
    side-wildcard."""

    def test_the_bigger_pot_wins_over_bigger_committed_basis(self):
        rich_pot = _slot("KXEUR-1-T1155", accrued=0.26)
        big_stake = _slot("KXEUR-1-T1153", accrued=0.01)
        a, _, _ = alloc.allocate([rich_pot, big_stake], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 resting={big_stake.key: 62.0},
                                 owner_seed={"KXEUR": rich_pot.key},
                                 owner_accrued={"KXEUR": 0.26})
        self.assertEqual(a[big_stake.key], 0, "the 1c rung must be displaced")
        self.assertGreater(a[rich_pot.key], 0, "the 26c rung must be funded")

    def test_a_wildcard_owner_admits_either_side_of_its_ticker(self):
        bid = _slot("KXEUR-1-T1155")
        a, _, _ = alloc.allocate([bid], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 owner_seed={"KXEUR": ("KXEUR-1-T1155", None)},
                                 owner_accrued={"KXEUR": 0.26})
        self.assertGreater(a[bid.key], 0, "the wildcard owner's own rung was refused")

    def test_a_wildcard_owner_still_blocks_siblings(self):
        sib = _slot("KXEUR-1-T1153")
        a, _, _ = alloc.allocate([sib], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 owner_seed={"KXEUR": ("KXEUR-1-T1155", None)},
                                 owner_accrued={"KXEUR": 0.26})
        self.assertEqual(a[sib.key], 0)


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
