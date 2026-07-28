"""FINAL FIX ROUND — the real-wire aliveness suite plus the seam tests for every fix.

The reviewer's finding that sized this round: the FakeExchange's leniency (cancel of a gone
order returned 200/reduced_by=0) let the engine "learn" fills from a call the real wire
refuses, which hid the entire missing feedback half — 630 cycles, 0 fills calls, a
taker-filled market frozen as a position_divergence at t+601 s.  Every test here drives the
ASSEMBLED loop against the honest fake: fills are learnable ONLY via the fills API, cancels
of gone orders 404, and the public book reflects our own resting orders.
"""

import os
import unittest

from .. import config as C, exchange as X, ratchet as RT, runner as RUN, runtime as R
from .. import scan
from .base import LipTestCase
from .test_engine import EngineCase, NOW
from .test_aliveness import AliveExchange, cheap_book, NESTOR

TK = "KXAAAGASD-26JUL29-T4.12"
CLIFF_TK = "KXCLIFF-26JUL29-T1"


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def program_body(series="KXAAAGASD", tickers=(TK,), reward=1_000_000,
                 start=NOW - 3600, end=NOW + 16 * 3600, pid="prog-1"):
    return {"liquidity_incentive_programs": [{
        "id": pid, "series_ticker": series, "market_tickers": list(tickers),
        "period_reward": reward, "start_date": iso(start), "end_date": iso(end),
        "target_size_fp": 1000}]}


class CountingExchange(AliveExchange):
    def __init__(self, *a, **kw):
        super(CountingExchange, self).__init__(*a, **kw)
        self.fills_calls = 0

    def fills(self, min_ts=None, order_id=None):
        self.fills_calls += 1
        return super(CountingExchange, self).fills(min_ts=min_ts, order_id=order_id)


class FixRoundCase(EngineCase):
    def runner(self, ex):
        m = self.maker(ex=ex)
        return RUN.Runner(m, sleep=lambda _s: None)


# =============================================================================================
# FOUNDATION — the fake speaks the real wire.
# =============================================================================================
class TestFakeFidelity(LipTestCase):
    def test_cancel_of_a_gone_order_404s(self):
        ex = X.FakeExchange()
        status, body = ex.cancel("nope")
        self.assertEqual(status, 404)
        self.assertNotIn("reduced_by", body)

    def test_take_emits_a_real_shape_fills_row_and_moves_the_position(self):
        ex = X.FakeExchange(balance_cents=100_000)
        _, resp = ex.place({"ticker": "T", "side": "bid", "count": "10.00",
                            "price": "0.0200", "client_order_id": "v5-x-1"})
        oid = resp["order"]["order_id"]
        row = ex.take(oid, 10, now=NOW)
        self.assertEqual(row["side"], "yes")
        self.assertEqual(row["action"], "buy")
        self.assertEqual(row["yes_price"], 2)              # CENTS, the real unit
        self.assertTrue(row["trade_id"])
        self.assertNotIn(oid, ex.resting)                  # gone: only fills can teach it
        self.assertEqual(ex.positions()[1]["market_positions"][0]["position"], 10.0)
        self.assertEqual(ex.cancel(oid)[0], 404)

    def test_the_public_book_reflects_our_own_resting_orders(self):
        ex = X.FakeExchange(books={"T": cheap_book()})
        ex.place({"ticker": "T", "side": "bid", "count": "50.00", "price": "0.0200",
                  "client_order_id": "v5-x-1"})
        _, body = ex.book("T")
        yes = body["orderbook"]["orderbook_fp"]["yes_dollars"]
        self.assertAlmostEqual(float(yes[0][1]), 1250.0)   # 1200 rivals + our 50

    def test_fills_scoped_by_order_id(self):
        ex = X.FakeExchange()
        ex.fills_rows = [{"order_id": "a", "count": 1}, {"order_id": "b", "count": 2}]
        _, body = ex.fills(order_id="b")
        self.assertEqual([r["order_id"] for r in body["fills"]], ["b"])


# =============================================================================================
# BLOCKER-1 — fill → booked → replenished, through the ASSEMBLED loop at TRUE 1 Hz.
# =============================================================================================
class TestFillReplenishAtOneHz(FixRoundCase):
    def _filled_runner(self, verified=True):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        ok, refusals = r.init(NOW, nestor_state=NESTOR)
        self.assertTrue(ok, refusals)
        r.iteration(NOW + 1)
        self.assertEqual(len(ex.placed), 1)
        oid = list(r.m.orders)[0]
        n = r.m.orders[oid]["remaining"]
        if verified:
            st = r.m.venues["KXAAAGASD"]
            st.verified = True
            st.rung = 2
        ex.take(oid, n, now=NOW + 2)                       # the taker eats the WHOLE order
        return r, ex, oid, n

    def test_fill_learned_via_fills_api_and_replenished(self):
        r, ex, oid, n = self._filled_runner(verified=True)
        t = NOW + 2
        for _ in range(2 * int(C.FILLS_POLL_S) + 5):       # true 1 Hz
            t += 1.0
            r.iteration(t)
        self.assertGreater(ex.fills_calls, 0, "the fills API was NEVER polled")
        self.assertAlmostEqual(r.m.positions[TK]["yes"], n, places=6)
        self.assertNotIn(oid, r.m.orders, "the filled order is still counted as resting")
        self.assertTrue(ex.resting, "NO REPLENISH ORDER RESTS: presence died after a fill")
        self.assertGreater(len(ex.placed), 1, "silent after the first fill — v4's tape")
        self.assertNotIn(TK, r.m.frozen)

    def test_the_fill_updates_collateral_so_the_feed_stays_true(self):
        r, ex, oid, n = self._filled_runner(verified=True)
        t = NOW + 2
        for _ in range(int(C.FILLS_POLL_S) + 2):
            t += 1.0
            r.iteration(t)
        # the filled order's collateral became inventory basis; only the REPLENISH rests
        live = sum(o["remaining"] * 0.02 for o in r.m.orders.values()
                   if not o.get("gone_404"))
        self.assertAlmostEqual(r.m.cash.resting_collateral, live, places=6)
        self.assertAlmostEqual(r.m.cash.inventory_basis, n * 0.02, places=6)

    def test_a_fill_NEVER_freezes_its_own_market(self):
        """The reviewer's exact repro, inverted: 1 Hz through the whole recon window and
        past it — no position_divergence, no assume_filled, no freeze, ever."""
        r, ex, oid, n = self._filled_runner(verified=False)
        t = NOW + 2
        for _ in range(int(C.RECON_POSITIONS_S) + 30):
            t += 1.0
            r.iteration(t)
        self.assertNotIn(TK, r.m.frozen)
        self.assertEqual(self.logs_of("position_divergence"), [])
        self.assertEqual([a for a in self.alerts if a[0] == "assume_filled"], [])
        self.assertAlmostEqual(r.m.positions[TK]["yes"], n, places=6)

    def test_a_partial_fill_updates_remaining_for_the_refill_trigger(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        oid = list(r.m.orders)[0]
        n = r.m.orders[oid]["remaining"]
        taken = int(n * 0.6)
        ex.take(oid, taken, now=NOW + 2)
        t = NOW + 2
        for _ in range(int(C.FILLS_POLL_S) + 2):
            t += 1.0
            r.iteration(t)
        self.assertAlmostEqual(r.m.positions[TK]["yes"], taken, places=6)
        # remaining is either updated on the surviving order or the order was requoted;
        # in both worlds our books agree with the wire about what rests
        wire = sum(float(b.get("count", 0)) for b in ex.resting.values())
        ours = sum(o["remaining"] for o in r.m.orders.values() if not o.get("gone_404"))
        self.assertAlmostEqual(ours, wire, places=6)


# =============================================================================================
# BLOCKER-1 — the §9.4a 404 disambiguation (FILLS_REQUERY_DELAY_S finally implemented).
# =============================================================================================
class Test404Disambiguation(EngineCase):
    def _placed(self, ex=None):
        m = self.maker(ex=ex or X.FakeExchange(books={TK: cheap_book()},
                                               balance_cents=100_000))
        ok, why, _ = m.place(TK, "bid", 0.02, 50, NOW + 3600, NOW,
                             available_cash_usd=1000.0)
        self.assertTrue(ok, why)
        return m, list(m.orders)[0]

    def test_404_with_fills_books_the_fill_not_a_freeze(self):
        m, oid = self._placed()
        m.ex.take(oid, 50, now=NOW + 5)
        ok, reason = m.cancel(oid, NOW + 40)
        self.assertEqual(reason, "gone_404")
        self.assertAlmostEqual(m.positions[TK]["yes"], 50.0, places=6)
        self.assertNotIn(oid, m.orders)
        self.assertNotIn(TK, m.frozen)
        self.assertAlmostEqual(m.cash.resting_collateral, 0.0, places=9)
        self.assertAlmostEqual(m.cash.inventory_basis, 1.0, places=9)

    def test_404_with_no_fills_requeries_after_36s_then_expires(self):
        m, oid = self._placed()
        m.ex.resting.pop(oid, None)                        # gone with NO fills (expired)
        m.cancel(oid, NOW + 40)
        self.assertIn(oid, m.pending_404)
        self.assertIn(oid, m.orders)                       # collateral stays counted
        self.assertGreater(m.cash.resting_collateral, 0.0)
        m.pump_404(NOW + 41)                               # before the delay: nothing
        self.assertIn(oid, m.pending_404)
        m.pump_404(NOW + 40 + C.FILLS_REQUERY_DELAY_S + 1)
        self.assertNotIn(oid, m.orders)
        self.assertNotIn(oid, m.pending_404)
        self.assertAlmostEqual(m.cash.resting_collateral, 0.0, places=9)
        self.assertEqual(m.positions.get(TK), None)        # ZERO booked — but not silently:
        rows = [x for x in m.ledger.read() if x.get("k") == "expired"]
        self.assertTrue(rows, "an expired 404 must leave a terminal ledger row")
        self.assertNotIn(TK, m.frozen)

    def test_404_fills_that_arrive_during_the_lag_are_booked_on_requery(self):
        """T31b's shape: the case a single read would have booked as zero."""
        m, oid = self._placed()
        n = m.orders[oid]["remaining"]
        m.ex.resting.pop(oid, None)
        m.cancel(oid, NOW + 40)                            # read 1: no fills yet
        m.ex.fills_rows.append({"trade_id": "late-1", "order_id": oid, "ticker": TK,
                                "side": "yes", "action": "buy", "count": n,
                                "yes_price": 2, "is_taker": False})
        m.pump_404(NOW + 40 + C.FILLS_REQUERY_DELAY_S + 1)
        self.assertAlmostEqual(m.positions[TK]["yes"], n, places=6)
        self.assertNotIn(oid, m.orders)
        self.assertNotIn(TK, m.frozen)

    def test_404_with_a_fills_query_error_assumes_filled_and_freezes(self):
        class BrokenFills(X.FakeExchange):
            def fills(self, min_ts=None, order_id=None):
                return 500, {}
        m, oid = self._placed(ex=BrokenFills(books={TK: cheap_book()},
                                             balance_cents=100_000))
        m.ex.resting.pop(oid, None)
        m.cancel(oid, NOW + 40)
        self.assertAlmostEqual(m.positions[TK]["yes"], 50.0, places=6)  # conservative
        self.assertIn(TK, m.frozen)
        self.assertTrue([a for a in self.alerts if a[0] == "assume_filled"])

    def test_shutdown_resolves_a_pending_404_conservatively(self):
        m, oid = self._placed()
        m.ex.resting.pop(oid, None)
        m.cancel(oid, NOW + 40)
        self.assertIn(oid, m.pending_404)
        res = m.shutdown(NOW + 41)
        self.assertNotIn(oid, m.orders)
        self.assertIn(TK, m.frozen)
        self.assertTrue(res["handback"] is not False)


# =============================================================================================
# BLOCKER-2 — the cliff rescue fires for an UNVERIFIED venue (the launch regime).
# =============================================================================================
class TestRescueUnverifiedVenue(FixRoundCase):
    def _runner(self, reward=25_000):
        ex = CountingExchange(
            program_body(series="KXCLIFF", tickers=(CLIFF_TK,), reward=reward),
            {CLIFF_TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.m.accrued["prog-1"] = 0.87                      # the stranded accrual
        return r, ex

    def test_the_top_up_reaches_the_wire_with_no_venue_state(self):
        r, ex = self._runner()
        out = r.iteration(NOW + 1)
        self.assertIn(r.m.venue_status.get("KXCLIFF"), (RT.ADMITTED, RT.OVERSIZED))
        self.assertTrue(self.logs_of("cliff_top_up"), "the rescue never fired")
        self.assertGreater(len(ex.placed), 0, "NO RESCUE ORDER REACHED THE WIRE")
        q = out["alloc"][(CLIFF_TK, "bid")]
        # the size reaches the cliff: A + share·(ρ/2)·h ≥ $1.10
        s = [s for s in r.slots if s.side == "bid"][0]
        proj = 0.87 + (q / (q + s.S)) * (s.rho / 2.0) * s.hours_left
        self.assertGreaterEqual(proj, C.RESCUE_TARGET_USD - 1e-6)

    def test_dead_accrual_gets_no_cap_room(self):
        """The mirror: a cliff UNREACHABLE at the ρ/2 ceiling earns no exemption."""
        r, ex = self._runner(reward=200)                  # ρ/2·h ≈ $0.009: unreachable
        r.iteration(NOW + 1)
        self.assertEqual(self.logs_of("cliff_top_up"), [])
        self.assertEqual(len(ex.placed), 0)

    def test_venue_floor_uses_the_rescue_target_not_the_entry_floor(self):
        r, _ = self._runner()
        r.iteration(NOW + 1)
        s = [s for s in r.slots if s.side == "bid"][0]
        floor = r.m.venue_floor_usd([s])
        entry = RT.floor_q_usd(s.rho, s.S, s.p, s.hours_left)
        self.assertIsNotNone(floor)
        if entry is not None:
            self.assertLess(floor, entry)                 # the exemption bought room


# =============================================================================================
# BLOCKER-4 — accrual over CONFIRMED presence only; shadow writes zero money rows.
# =============================================================================================
class TestAccrualOverWirePresence(FixRoundCase):
    def test_accrual_never_integrates_over_unlanded_allocation(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        ex.place_status = 500                              # every POST fails: nothing rests
        ex.place_error = "wire down"
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        for i in range(5):
            r.iteration(NOW + 1 + i)
        self.assertEqual(r.m.orders, {})
        self.assertEqual(r.m.accrued, {}, "accrued over an allocation nobody landed")
        self.assertAlmostEqual(r.m.cash.rewards_accrued_unpaid, 0.0, places=9)

    def test_shadow_writes_zero_accrual_money_rows(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        m = self.maker(ex=ex, shadow=True)
        r = RUN.Runner(m, sleep=lambda _s: None)
        r.init(NOW, nestor_state=NESTOR)
        for i in range(3):
            r.iteration(NOW + 1 + i)
        r.iteration(NOW + 4 + C.ACCRUAL_WRITE_S)           # past the persistence cadence
        self.assertEqual(ex.placed, [])                    # shadow quotes NOTHING
        self.assertEqual(m.accrued, {})
        self.assertAlmostEqual(m.cash.rewards_accrued_unpaid, 0.0, places=9)
        money_rows = [x for x in m.ledger.read()
                      if x.get("k") in ("accrual", "place_req", "place_resp", "fill_obs")]
        self.assertEqual(money_rows, [], "shadow contaminated the live replay")


# =============================================================================================
# SF-1 — the day stop's scale is OUR projected accrual.
# =============================================================================================
class TestDayStopScale(FixRoundCase):
    def test_projected_day_reward_is_our_share_not_the_board_pool(self):
        ex = CountingExchange(program_body(reward=1_000_000), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        out = r.iteration(NOW + 1)
        board = sum((s.rho / 2.0) * min(24.0, s.hours_left) for s in r.slots)
        self.assertGreater(board, 40.0)                    # the board number is huge
        self.assertLess(r.m.projected_day_reward, 5.0)     # ours is what we can EARN
        self.assertGreater(r.m.projected_day_reward, 0.0)
        from .. import guards as G
        self.assertAlmostEqual(G.day_stop_usd(r.m.projected_day_reward),
                               C.DAY_STOP_FLOOR_USD, places=9)   # trippable at launch


# =============================================================================================
# SF-2 — a 429 yields, on EVERY path.
# =============================================================================================
class Test429Yields(EngineCase):
    class RateLimited(X.FakeExchange):
        def __init__(self, limited, **kw):
            super(Test429Yields.RateLimited, self).__init__(**kw)
            self.limited = set(limited)

        def _maybe(self, name, real):
            return (429, {}) if name in self.limited else real

        def place(self, body):
            return self._maybe("place", super(Test429Yields.RateLimited, self).place(body))

        def positions(self):
            return self._maybe("positions",
                               super(Test429Yields.RateLimited, self).positions())

        def fills(self, min_ts=None, order_id=None):
            return self._maybe("fills", super(Test429Yields.RateLimited, self).fills(
                min_ts=min_ts, order_id=order_id))

    def test_a_429_on_place_halves_the_bucket(self):
        m = self.maker(ex=self.RateLimited({"place"}, balance_cents=100_000))
        b0 = m.bucket.b
        m.place(TK, "bid", 0.02, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        self.assertAlmostEqual(m.bucket.b, b0 / 2.0, places=9)
        self.assertTrue(self.logs_of("rate_yield"))

    def test_a_429_on_fills_and_positions_halves_the_bucket(self):
        m = self.maker(ex=self.RateLimited({"fills", "positions"},
                                           balance_cents=100_000))
        b0 = m.bucket.b
        m.poll_fills(NOW)
        self.assertAlmostEqual(m.bucket.b, b0 / 2.0, places=9)
        m.reconcile(NOW + 120)
        self.assertLess(m.bucket.b, b0 / 2.0)

    def test_a_429_on_the_classify_book_read_halves_the_bucket(self):
        class Limited(AliveExchange):
            def book(self, ticker):
                return 429, {}
        ex = Limited(program_body(), {TK: cheap_book()})
        r = RUN.Runner(self.maker(ex=ex), sleep=lambda _s: None)
        r.init(NOW, nestor_state=NESTOR)
        b0 = r.m.bucket.cap_hz
        r.iteration(NOW + 1)
        self.assertLess(r.m.bucket.b, b0)


# =============================================================================================
# SF-3 — a halted book can LEAVE: the closing-only pass.
# =============================================================================================
class TestHaltedClosingPass(FixRoundCase):
    def test_a_halted_book_posts_its_shed(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.m.positions[TK] = {"yes": 20.0, "no": 0.0}
        r.m.entry_basis[(TK, "yes")] = 0.02
        r.m.halt.halt("day_stop", NOW + 1)
        out = r.iteration(NOW + 2)
        self.assertTrue(out["halted"])
        sheds = [b for b in ex.placed if b["side"] == "ask"]
        self.assertTrue(sheds, "halted book cannot leave: no shed posted")
        # joins the opposing best (1 − 0.97 = 0.03), never crossing the 0.02 bid
        self.assertAlmostEqual(float(sheds[0]["price"]), 0.03, places=6)
        self.assertAlmostEqual(float(sheds[0]["count"]), 20.0, places=6)
        # and it is not re-posted while one rests
        r.iteration(NOW + 3)
        self.assertEqual(len([b for b in ex.placed if b["side"] == "ask"]), 1)

    def test_the_halted_pass_never_opens(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.m.halt.halt("day_stop", NOW + 1)                 # halted, FLAT
        r.iteration(NOW + 2)
        self.assertEqual(ex.placed, [])


# =============================================================================================
# SF-4 / N3 — the watched readings file feeds the ratchet; paid credits retire the claim.
# =============================================================================================
class TestReadingsFile(FixRoundCase):
    def _write_reading(self, r, row):
        import json
        path = os.path.join(r.m.data_dir, C.READINGS_NAME)
        with open(path, "a") as fh:
            fh.write(json.dumps(row) + "\n")

    def test_a_reading_moves_the_rung_through_the_live_cycle(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)                               # venue admitted at rung 0
        self._write_reading(r, {"venue": "KXAAAGASD", "reading_usd": 3.0,
                                "projection_usd": 4.0})
        r.iteration(NOW + 2)
        self.assertEqual(r.m.venues["KXAAAGASD"].rung, 1)
        self.assertTrue(r.m.venues["KXAAAGASD"].verified)

    def test_a_consumed_reading_is_never_reapplied_across_restart(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        self._write_reading(r, {"venue": "KXAAAGASD", "reading_usd": 3.0,
                                "projection_usd": 4.0})
        r.iteration(NOW + 2)
        self.assertEqual(r.m.venues["KXAAAGASD"].rung, 1)
        r2 = RUN.Runner(self.maker(ex=ex), sleep=lambda _s: None)
        r2.init(NOW + 10, nestor_state=NESTOR)
        self.assertEqual(r2.m.venues["KXAAAGASD"].rung, 1)  # replayed, not re-applied
        r2.iteration(NOW + 11)
        self.assertEqual(r2.m.venues["KXAAAGASD"].rung, 1)  # file row consumed exactly once

    def test_N3_a_paid_credit_retires_accrued_unpaid(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        r.m.accrued["prog-1"] = 0.50
        r.m.cash.rewards_accrued_unpaid = 0.50
        self._write_reading(r, {"venue": "KXAAAGASD", "reading_usd": 0.40,
                                "projection_usd": 0.50, "paid": True,
                                "program_id": "prog-1"})
        r.iteration(NOW + 2)
        # places=3: the live cycle legitimately accrues ~3e-5 for the second of resting
        # presence between the two iterations — the paid credit netted the $0.40.
        self.assertAlmostEqual(r.m.cash.rewards_accrued_unpaid, 0.10, places=3)
        self.assertAlmostEqual(r.m.accrued["prog-1"], 0.10, places=3)

    def test_a_bad_row_is_skipped_and_never_wedges_the_file(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        self._write_reading(r, {"nonsense": True})
        self._write_reading(r, {"venue": "KXAAAGASD", "reading_usd": 3.0,
                                "projection_usd": 4.0})
        r.iteration(NOW + 2)
        self.assertTrue(self.logs_of("reading_bad_row"))
        self.assertEqual(r.m.venues["KXAAAGASD"].rung, 1)   # the good row still applied


# =============================================================================================
# SF-5 — S is the RIVAL score.
# =============================================================================================
class TestRivalS(FixRoundCase):
    def test_rival_S_subtracts_our_size_at_our_distance(self):
        self.assertAlmostEqual(scan.rival_S(1250.0, 0.02, [(2, 50.0)]), 1200.0, places=9)
        self.assertAlmostEqual(scan.rival_S(1250.0, 0.02, [(1, 50.0)]),
                               1250.0 - 50.0 * 0.5, places=9)   # one tick back: half weight
        self.assertAlmostEqual(scan.rival_S(40.0, 0.02, [(2, 100.0)]), 0.0, places=9)

    def test_through_the_loop_our_resting_order_leaves_S_at_the_rivals(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)                               # we now rest at 2c
        # force a fresh classification (the honest book now contains our order)
        self.classifier_refresh(r, NOW + 2)
        r.iteration(NOW + 3)
        s = [s for s in r.slots if s.side == "bid"][0]
        self.assertAlmostEqual(s.S, 1200.0, places=3)      # rivals only, not us
        # and the loop does not oscillate: the order survives the rival-S allocation
        self.assertTrue(r.m.orders)

    def classifier_refresh(self, r, now):
        for tk in list(r.classifier.last):
            r.classifier.last[tk] = now - C.CLASSIFY_REFRESH_S - 1


# =============================================================================================
# SF-6 — the B9 turnover window is the program period, surviving restart.
# =============================================================================================
class TestTurnoverWindow(FixRoundCase):
    def test_a_new_period_resets_the_count(self):
        from .. import guards as G
        rt = G.RefillTracker()
        rt.note_fill("T", "bid", 100, ts=NOW - 86400)      # last period's churn
        rt.note_fill("T", "bid", 10, ts=NOW + 5)
        rt.set_window("T", "bid", NOW)                     # the current period starts NOW
        self.assertAlmostEqual(rt.filled[("T", "bid")], 10.0, places=9)
        rt.set_window("T", "bid", NOW)                     # same window: no churn of state
        self.assertAlmostEqual(rt.filled[("T", "bid")], 10.0, places=9)
        rt.set_window("T", "bid", NOW + 100)               # NEW period: old fills drop
        self.assertAlmostEqual(rt.filled.get(("T", "bid"), 0.0), 0.0, places=9)

    def test_the_count_survives_restart_within_the_period(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        oid = list(r.m.orders)[0]
        ex.take(oid, 5, now=NOW + 2)
        t = NOW + 2
        for _ in range(int(C.FILLS_POLL_S) + 2):
            t += 1.0
            r.iteration(t)
        filled_before = r.m.refill.filled.get((TK, "bid"), 0.0)
        self.assertGreaterEqual(filled_before, 5.0)
        r2 = RUN.Runner(self.maker(ex=ex), sleep=lambda _s: None)
        r2.init(t + 1, nestor_state=NESTOR)
        r2.iteration(t + 2)                                # set_window runs; period same
        self.assertGreaterEqual(r2.m.refill.filled.get((TK, "bid"), 0.0), 5.0,
                                "restart amnestied the turnover count")


# =============================================================================================
# BLOCKER-3 — books through the loop; the resync trigger is alive.
# =============================================================================================
class TestBooksWired(FixRoundCase):
    def test_the_requoter_follows_a_book_move_within_seconds(self):
        """The classify cadence is 15 min; the book_poll lane must carry a price move to
        the requoter in ~1 s — trigger (a) through the ASSEMBLED loop."""
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        self.assertAlmostEqual(float(ex.placed[0]["price"]), 0.02, places=6)
        # rivals move the best bid to 3c
        ex.books[TK] = {"orderbook": {"orderbook_fp": {
            "yes_dollars": [["0.03", "1200"]], "no_dollars": [["0.96", "1200"]]}}}
        t = NOW + 1
        for _ in range(5):
            t += 1.0
            r.iteration(t)
        self.assertTrue(any(abs(float(b["price"]) - 0.03) < 1e-9 for b in ex.placed),
                        "the price reference never followed the book: books unwired")

    def test_held_and_ordered_markets_are_always_in_the_poll_set(self):
        slots = []
        out = scan.poll_set(slots, {"HELD-1", "ORD-2"}, connected=False)
        self.assertIn("HELD-1", out)
        self.assertIn("ORD-2", out)

    def test_trigger_e_fires_for_a_slot_whose_examination_lapsed(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        placed_before = len(ex.placed)
        key = (TK, "bid")
        # simulate a lapse: the slot was last examined > SAFETY_RESYNC_S ago
        r.m.slot_examined[key] = NOW + 1 - C.SAFETY_RESYNC_S - 5
        # age the order past MIN_RESTING_LIFE so (e) is not suppressed by P1
        r.m.orders[list(r.m.orders)[0]]["placed_ts"] = NOW - C.MIN_RESTING_LIFE_S - 10
        r.iteration(NOW + 2)
        self.assertGreater(len(ex.placed), placed_before,
                           "trigger (e) is still dead: the lapsed slot was not re-proven")

    def test_a_requote_pass_does_NOT_reset_resync_globally(self):
        """The defect: `last_resync = now` every pass.  Now per-slot: an untouched slot's
        clock keeps running."""
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        self.assertFalse(hasattr(r.m, "last_resync"))


if __name__ == "__main__":
    unittest.main()
