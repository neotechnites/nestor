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


# ── DELETED WITH THE OLD ALLOCATOR (owner's law, 2026-07-30). ────────────────────────────────
# TestD5OneRungPerCluster, TestD6LotSemantics, TestD11PlanSideVariance, TestD12PeriodLock,
# TestClusterOwnershipSeed, TestOwnerDisplacement, TestRecallRequiresScarcity,
# TestOwnerRanksByAccruedDollars and TestDisplacementCoversTheRescue tested water-filling
# internals, the plan-side variance instrument, the period lock and the owner
# displacement/recall machinery — all replaced by the law: one order per cluster and the
# accrued-subtracts ranking are tested in test_law.py; re-ranking every pass IS the
# reallocation (there is no displacement pass to test); nothing recalls by accrual rank.


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
