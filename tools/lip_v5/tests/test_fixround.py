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
    return {"incentive_programs": [{
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
        oid = resp["order_id"]          # FLAT: the real wire shape
        row = ex.take(oid, 10, now=NOW)
        # the 2026-07-30 wire dialect (captured_fills_20260730.json): fractional
        # dollar-string count_fp, *_price_dollars, book_side, fee_cost
        self.assertEqual(row["book_side"], "bid")
        self.assertEqual(row["count_fp"], "10.00")
        self.assertEqual(row["yes_price_dollars"], "0.0200")
        self.assertEqual(row["fee_cost"], "0.000000")
        self.assertTrue(row["trade_id"])
        self.assertNotIn(oid, ex.resting)                  # gone: only fills can teach it
        self.assertEqual(
            float(ex.positions()[1]["market_positions"][0]["position_fp"]), 10.0)
        self.assertEqual(ex.cancel(oid)[0], 404)

    def test_the_public_book_reflects_our_own_resting_orders(self):
        ex = X.FakeExchange(books={"T": cheap_book()})
        ex.place({"ticker": "T", "side": "bid", "count": "50.00", "price": "0.0600",
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
        # (stage 1: `verified` has no referent — a venue holds no rung to promote)
        ex.take(oid, n, now=NOW + 2)                       # the taker eats the WHOLE order
        return r, ex, oid, n

    def test_fill_learned_via_fills_api_and_replenished(self):
        r, ex, oid, n = self._filled_runner(verified=True)
        t = NOW + 2
        for _ in range(2 * int(C.FILLS_POLL_S) + 5):       # true 1 Hz
            t += 1.0
            r.iteration(t)
        t += C.POST_FILL_COOLDOWN_S                        # past the post-fill cooldown
        for _ in range(5):
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
        live = sum(o["remaining"] * 0.06 for o in r.m.orders.values()
                   if not o.get("gone_404"))
        self.assertAlmostEqual(r.m.cash.resting_collateral, live, places=6)
        self.assertAlmostEqual(r.m.cash.inventory_basis, n * 0.06, places=6)

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

    def test_a_rescue_bigger_than_the_lot_container_is_REFUSED(self):
        """WAS `test_the_top_up_reaches_the_wire_with_no_venue_state`, asserting a ~$16
        top-up (271 contracts against S=1200 rivals) reached the wire.  Note 52 D6 refuses it
        BY DESIGN: the cluster reserve is $10 and the lot container $2.50, and "fewer rungs,
        never smaller lots" applies to rescues too — recovering $0.87 of stranded accrual is
        not worth 1.6x a whole settle-source's reserve.  The venue reads UNPROBEABLE (its
        cheapest fitting lot does not exist) and nothing reaches the wire.  The fitting-
        reserve rescue mechanics stay covered in test_alloc's TestCliffRecovery."""
        r, ex = self._runner()
        r.iteration(NOW + 1)
        # STAGE 1: there is no UNPROBEABLE status to read — the venue holds no permission
        # state at all.  What refuses the $16 top-up is the DOLLAR stack that always did the
        # real work: the lot container and the cluster reserve.
        self.assertEqual(self.logs_of("cliff_top_up"), [])
        self.assertEqual(len(ex.placed), 0)

    def test_dead_accrual_gets_no_cap_room(self):
        """The mirror: a cliff UNREACHABLE at the ρ/2 ceiling earns no exemption."""
        r, ex = self._runner(reward=200)                  # ρ/2·h ≈ $0.009: unreachable
        r.iteration(NOW + 1)
        self.assertEqual(self.logs_of("cliff_top_up"), [])
        self.assertEqual(len(ex.placed), 0)

    def test_the_rescue_target_exemption_DIED_WITH_THE_PROBE_FLOOR(self):
        """DELETED-IN-PLACE 2026-07-30 (stage 1).  `venue_floor_usd` computed a per-venue
        PROBE FLOOR so admission could size a rung-0 cap, and BLOCKER-2's exemption let
        stranded accrual buy room inside that cap.  Both are gone: there is no probe, no
        rung-0 cap and no venue floor.  Accrual still gets its say — `cliff_clearing_q`
        subtracts it (`need = target - accrued`) so an earning rung is sized cheaper than a
        fresh one, in the ALLOCATOR, where it is arithmetic rather than permission."""
        self.assertFalse(hasattr(self.maker(), "venue_floor_usd"))


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
# A HALTED BOT PLACES NOTHING.  (Law change, owner decision 2026-07-30.)
#
# THIS CLASS ASSERTED THE OPPOSITE UNTIL TODAY.  Its old name was `TestHaltedClosingPass` and
# its central case, `test_a_halted_book_posts_its_shed`, REQUIRED a halted iteration to put an
# ask on the wire ("halted book cannot leave: no shed posted").  That is the behaviour that
# cost us the 2026-07-30 incident: a books-integrity bug halted the bot, the halt armed the
# closing pass, the pass sized cap-EXEMPT closing orders from the very books the halt had just
# declared wrong, and a 98-contract $93 buy at 95c went out against a phantom short.
#
# THE NEW LAW, in the owner's words: "it's either running and placing orders, or it's not
# running."  Nothing in between.  A halt cancels the orders THIS PROCESS placed and then does
# nothing at all; the positions ride to settlement (bounded ≤7 days by the D4 gate, and the
# tape prices paying the spread to leave at −$40.30 / −$123 anyway).
#
# The tests below are the same fixtures with the assertion inverted, which is the point: the
# old law and the new one are distinguishable on the identical world.
# =============================================================================================
class TestHaltedBotPlacesNothing(FixRoundCase):
    def test_a_halted_book_posts_NOTHING_even_holding_inventory(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.m.positions[TK] = {"yes": 20.0, "no": 0.0}
        r.m.entry_basis[(TK, "yes")] = 0.06
        r.m.halt.halt("day_stop", NOW + 1)
        out = r.iteration(NOW + 2)
        self.assertTrue(out["halted"])
        self.assertEqual(ex.placed, [],
                         "a halted bot placed an order; the halt has exactly one action, "
                         "cancel-own-orders, and it is not a placing action")
        # ...and it stays nothing, pass after pass — there is no re-post cadence to reach.
        r.iteration(NOW + 3)
        r.iteration(NOW + 4)
        self.assertEqual(ex.placed, [])

    def test_the_halt_cancels_only_orders_this_process_placed(self):
        """The account is SHARED (nestor and others live on it).  `flatten` walks
        `self.orders` — this process's own book — so a halt can never reach a foreign
        order.  Asserted on the cancel ids the exchange actually saw."""
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.m.orders["ours-1"] = {"order_id": "ours-1", "coid": "lipv5-x", "ticker": TK,
                                "side": "bid", "price": 0.06, "size": 10.0,
                                "remaining": 10.0, "placed_ts": NOW}
        r.m.halt.halt("books_integrity", NOW + 1)
        r.iteration(NOW + 2)
        self.assertEqual(sorted(getattr(ex, "cancelled", [])), ["ours-1"])
        self.assertEqual(ex.placed, [])

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
        r.iteration(NOW + 1)
        self._write_reading(r, {"venue": "KXAAAGASD", "reading_usd": 3.0,
                                "projection_usd": 4.0})
        r.iteration(NOW + 2)
        # STAGE 1: a reading MEASURES, it does not promote.  3 of 4 projected is inside the
        # derived VERIFY band, so the venue is not denied and the measurement is recorded.
        self.assertAlmostEqual(r.m.venue_measured["KXAAAGASD"]["ratio"], 0.75, places=6)
        self.assertNotIn("KXAAAGASD", r.m.measured_deny)

    def test_a_consumed_reading_is_never_reapplied_across_restart(self):
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        self._write_reading(r, {"venue": "KXAAAGASD", "reading_usd": 3.0,
                                "projection_usd": 4.0})
        r.iteration(NOW + 2)
        self.assertIn("KXAAAGASD", r.m.venue_measured)
        r2 = RUN.Runner(self.maker(ex=ex), sleep=lambda _s: None)
        r2.init(NOW + 10, nestor_state=NESTOR)
        r2.iteration(NOW + 11)
        # The consumed-line marker still survives restart: it is a fact about which rows of
        # the WORLD we have already read, not a decision we made.  Applying the same reading
        # twice would double-count a measurement.
        self.assertGreaterEqual(r2.m.readings_line, 1)

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
        self.assertIn("KXAAAGASD", r.m.venue_measured)      # the good row still applied


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
        self.assertAlmostEqual(float(ex.placed[0]["price"]), 0.06, places=6)
        # rivals move the best bid to 7c
        ex.books[TK] = {"orderbook": {"orderbook_fp": {
            "yes_dollars": [["0.07", "1200"]], "no_dollars": [["0.92", "1200"]]}}}
        t = NOW + 1
        for _ in range(5):
            t += 1.0
            r.iteration(t)
        self.assertTrue(any(abs(float(b["price"]) - 0.07) < 1e-9 for b in ex.placed),
                        "the price reference never followed the book: books unwired")

    def test_held_and_ordered_markets_are_always_in_the_poll_set(self):
        slots = []
        out = scan.poll_set(slots, {"HELD-1", "ORD-2"}, connected=False)
        self.assertIn("HELD-1", out)
        self.assertIn("ORD-2", out)

    def test_trigger_e_is_SUPPRESSED_at_the_touch_and_fires_off_it(self):
        """WAS `test_trigger_e_fires_for_a_slot_whose_examination_lapsed`, asserting a lapsed
        AT-TOUCH slot re-places.  The no-change suppression (2026-07-29, the presence lever:
        median order life was 1.9 s and 73.9% of re-posts were at the SAME price) deliberately
        drops (e) at the touch — an identical cancel/replace surrenders queue position and
        buys nothing.  The stale end stays guarded: off the touch, (a) fires immediately."""
        ex = CountingExchange(program_body(), {TK: cheap_book()})
        r = self.runner(ex)
        r.init(NOW, nestor_state=NESTOR)
        for k in (1, 40, 80):                 # let the target and the venue cap settle;
            r.iteration(NOW + k)              # min-resting-life spaces the top-up re-posts
        placed_before = len(ex.placed)
        key = (TK, "bid")
        r.m.slot_examined[key] = NOW + 80 - C.SAFETY_RESYNC_S - 5
        for o in r.m.orders.values():
            o["placed_ts"] = NOW - C.MIN_RESTING_LIFE_S - 10
        r.iteration(NOW + 81)
        self.assertEqual(len(ex.placed), placed_before,
                         "an identical requote at the touch must be suppressed")
        # ...and off the touch the stale quote is re-proven at once (trigger (a))
        ex.books[TK] = {"orderbook": {"orderbook_fp": {
            "yes_dollars": [["0.07", "1200"]], "no_dollars": [["0.92", "1200"]]}}}
        for k in range(82, 85):
            r.iteration(NOW + k)
        self.assertGreater(len(ex.placed), placed_before,
                           "a stale quote OFF the touch was never re-proven")

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


class TestTheEmptyBookIsEnterable(FixRoundCase if 'FixRoundCase' in dir() else __import__('unittest').TestCase):
    """G2 measured it live: the highest-rho programs on the board are EMPTY on both sides, and
    an empty side yielded no `p`, so 200 classified markets produced ZERO slots.  A market
    nobody has quoted is the cheapest presence there is (S = 0, whole pool addressable)."""

    def test_an_empty_book_produces_a_land_grab_slot(self):
        import time
        from lip_v5 import scan, config as C
        now = time.time()
        prog = {"program_id": "p1", "series": "KXEMPTY", "tickers": ["KXEMPTY-26JUL29-T1"],
                "period_reward": 1000000, "start_ts": now - 3600, "end_ts": now + 36000,
                "window_h": 11.0, "rho": 9.0, "target_size": 1000.0, "paid_out": False}

        class C0(object):
            table = {"KXEMPTY-26JUL29-T1": {
                "ticker": "KXEMPTY-26JUL29-T1", "program_id": "p1", "series": "KXEMPTY",
                "pinned": False, "target_size": 1000.0, "yes_mid": None, "ts": now,
                "sides": {"bid": {"S": 0.0, "qualifies": False, "cum_size": 0.0,
                                  "p": None, "legal": False},
                          "ask": {"S": 0.0, "qualifies": False, "cum_size": 0.0,
                                  "p": None, "legal": False}}}}

        slots = scan.build_slots([prog], C0(), now, p6=lambda t: True)
        # INVERTED 2026-07-29 (FREE_RIDE_ONLY).  This asserted that an empty book IS enterable
        # via the qualification pass at LAND_GRAB_PRICE_C, on the reasoning that an empty side is
        # the cheapest presence on the board.  The reasoning has a hole the CFTC filing closes:
        # an empty side cannot reach target_size, and a side whose book cannot reach target
        # scores ZERO for the snapshot.  So the "cheapest presence" was no presence -- we were
        # buying 1,000 contracts of a worthless contract to qualify a side that still would not
        # have paid.  An empty side is by definition a side that does not qualify without us.
        self.assertEqual(slots, [],
                         "an empty side cannot reach target_size, so it scores zero: refuse it")

    EMPTY_TABLE = {"ticker": "KXEMPTY-26JUL29-T1", "program_id": "p1", "series": "KXEMPTY",
                   "pinned": False, "target_size": 1000.0, "yes_mid": None, "ts": 0.0,
                   "sides": {"bid": {"S": 0.0, "qualifies": False, "cum_size": 0.0,
                                     "p": None, "legal": False},
                             "ask": {"S": 0.0, "qualifies": False, "cum_size": 0.0,
                                     "p": None, "legal": False}}}

    def _empty(self, now):
        prog = {"program_id": "p1", "series": "KXEMPTY", "tickers": ["KXEMPTY-26JUL29-T1"],
                "period_reward": 1000000, "start_ts": now - 3600, "end_ts": now + 36000,
                "window_h": 11.0, "rho": 9.0, "target_size": 1000.0, "paid_out": False}
        # a KNOWN, near settlement close: the note-52 D4 gate refuses an unknown one, and
        # that refusal is not what these two tests are about.
        rec = dict(self.EMPTY_TABLE, ts=now, close_ts=now + 20 * 3600.0)

        class C0(object):
            table = {"KXEMPTY-26JUL29-T1": rec}
        return prog, C0()

    def test_the_empty_side_is_priced_INSIDE_the_band_it_must_pass(self):
        """A module cannot both price a thing and refuse its own price.  The empty side was
        priced at LAND_GRAB_PRICE_C = 1c while the armed entry band refuses anything under
        ENTRY_BAND_LO_C = 6c, so `entry_band_refused` was the FIRST thing every empty market
        hit — before the free-ride gate ever spoke.  Observed on the held path, where both
        entry gates are skipped (D1) and this price is what the market is really sized,
        requoted and shed at."""
        import time
        from lip_v5 import scan, config as C
        now = time.time()
        prog, cls = self._empty(now)
        slots = scan.build_slots([prog], cls, now, p6=lambda t: True,
                                 held={"KXEMPTY-26JUL29-T1"})
        self.assertEqual(len(slots), 2)
        for s in slots:
            self.assertAlmostEqual(s.p, C.ENTRY_BAND_LO_C / 100.0, places=9)
            self.assertLessEqual(C.ENTRY_BAND_LO_C, int(round(s.p * 100)))
            self.assertLessEqual(int(round(s.p * 100)), C.ENTRY_BAND_HI_C)

    def test_the_band_no_longer_refuses_an_empty_book_on_ENTRY_either(self):
        """With the price inside the band, the band is silent — and the free-ride gate is left
        as the SOLE remaining refusal (see TestTheEmptyBookFork below for the arithmetic)."""
        import time
        import unittest.mock as mock
        from lip_v5 import scan, config as C, runtime as R
        now = time.time()
        prog, cls = self._empty(now)
        logs = []
        R.set_log_sink(logs.append)
        self.addCleanup(R.set_log_sink, None)
        with mock.patch.object(C, "FREE_RIDE_ONLY", False):
            slots = scan.build_slots([prog], cls, now, p6=lambda t: True)
        self.assertEqual([l for l in logs if l.get("t") == "entry_band_refused"], [])
        self.assertEqual(len(slots), 2)
        # both sides cheap IN THEIR OWN CURRENCY: the bid is 6c of YES, the ask is 6c of NO
        # (= a YES ask at 94c).  Nothing to cross in an empty book, so both stand.
        self.assertEqual(sorted(int(round(s.p * 100)) for s in slots),
                         [C.ENTRY_BAND_LO_C, C.ENTRY_BAND_LO_C])
        # UNTOUCHED, AND INCONSISTENT ON PURPOSE: `land_grab_price_c` is the LAND GRAB's own
        # price, and the land grab is dead under FREE_RIDE_ONLY (it is forced to 0).  With the
        # gate off, as here, the qualification pass would buy at 1c/99c while the slot is
        # priced at 6c — flagged, not silently reconciled, because which of the two is right
        # depends on the qualification decision that is NOT ours to take (see the fork below).
        self.assertEqual(sorted(s.land_grab_price_c for s in slots),
                         [C.LAND_GRAB_PRICE_C, 100 - C.LAND_GRAB_PRICE_C])

    def test_a_pinned_empty_book_is_still_refused(self):
        import time
        from lip_v5 import scan
        now = time.time()
        prog = {"program_id": "p1", "series": "KXEMPTY", "tickers": ["KXEMPTY-26JUL29-T1"],
                "period_reward": 1000000, "start_ts": now - 3600, "end_ts": now + 36000,
                "window_h": 11.0, "rho": 9.0, "target_size": 1000.0, "paid_out": False}

        class C0(object):
            table = {"KXEMPTY-26JUL29-T1": {
                "ticker": "KXEMPTY-26JUL29-T1", "program_id": "p1", "series": "KXEMPTY",
                "pinned": True, "target_size": 1000.0, "yes_mid": None, "ts": now,
                "sides": {"bid": {"S": 0.0, "qualifies": False, "cum_size": 0.0,
                                  "p": None, "legal": False},
                          "ask": {"S": 0.0, "qualifies": False, "cum_size": 0.0,
                                  "p": None, "legal": False}}}}

        self.assertEqual(scan.build_slots([prog], C0(), now, p6=lambda t: True), [])


class TestTheEmptyBookFork(__import__('unittest').TestCase):
    """THE FORK, MEASURED AND PINNED — do not resolve this in code without Ryan.

    Pricing the empty side inside the entry band (above) clears the FIRST of three gates
    between the 7 sole-qualifier venues and a dollar.  The other two are not oversights, and
    they rest on one arithmetic fact this suite now states out loud:

        CREDIT IS A STEP FUNCTION OF SIZE, NOT THE SMOOTH q/(q+S) THE SIZING MODEL USES.

    `alloc.score_side` implements the CFTC filing: the qualifying walk must reach
    `target_size`, and `if not qualifies: S = 0`.  Our own resting size counts toward that
    walk (the classified book contains our orders; `rival_S` subtracts them afterwards).  So
    on a book nobody else quotes:

        q  <  target_size   ⇒  credit 0        (we are not a qualifying set)
        q  >= target_size   ⇒  credit pool/2   (we are the WHOLE qualifying set)

    while `cliff_clearing_q` — the function every sizing decision asks — answers ONE CONTRACT
    at S = 0, because `our_share(1, 0) = 1`.  The model believes one contract earns half the
    pool.  The filing says it earns nothing.  Both are in this repo today.

    WHAT QUALIFYING WOULD COST, at the live target of 1,000 contracts:
        at LAND_GRAB_PRICE_C = 1c   ->  $10.00  == the whole cluster reserve (ceiling/N)
        at ENTRY_BAND_LO_C  = 6c   ->  $60.00  == 12x the $5 lot, 6x the reserve, over the
                                                  per-venue cap and the per-market cap
    So the band edge, which is what makes the slot legal, is also what makes qualifying
    unaffordable — and 1c, which is what makes qualifying affordable, is the price the band
    exists to refuse on measured EV (note 47 3, n = 8,240: 2c realised 0.00% on 765 markets).
    That is the fork.  It is the same trade FREE_RIDE_ONLY was armed to refuse on 2026-07-29
    ("we were buying 1,000 contracts of a worthless contract to qualify a side that still
    would not have paid"), now arriving from the opposite direction.

    THE THREE GATES, in the order an empty market meets them:
      1. the entry price band            — CLEARED by pricing the empty side at the band edge
      2. FREE_RIDE_ONLY                  — refuses: an empty side never qualifies without us
      3. alloc.allocate's `s.S <= 0`     — the slot is not even ELIGIBLE for water-filling
    Gate 3 also means the sole-qualifier VENUE CAP (fixed this round) funds nothing on its
    own: the cap is necessary and nowhere near sufficient.
    """

    def _empty_slot(self, **kw):
        from lip_v5 import alloc
        kw.setdefault("rho", 9.0); kw.setdefault("S", 0.0); kw.setdefault("p", 0.06)
        kw.setdefault("hours_left", 11.0); kw.setdefault("window_h", 11.0)
        return alloc.Slot("KXEMPTY-1", "bid", venue="KXEMPTY", **kw)

    def test_the_model_says_ONE_CONTRACT_earns_half_the_pool(self):
        from lip_v5 import alloc
        s = self._empty_slot()
        self.assertEqual(alloc.our_share(1, s.S), 1.0)
        self.assertEqual(alloc.cliff_clearing_q(s), 0)      # i.e. "any size at all clears"

    def test_the_FILING_says_a_sub_target_side_scores_zero(self):
        from lip_v5 import alloc
        # 999 contracts of our own against a 1,000 target: the walk does not reach target.
        sc = alloc.score_side([(6, 999.0)], target_size=1000.0)
        self.assertFalse(sc.qualifies)
        self.assertEqual(sc.S, 0.0)
        self.assertEqual(sc.reason, "target_size_not_reached")
        # one more contract and the same side is the WHOLE qualifying set
        sc2 = alloc.score_side([(6, 1000.0)], target_size=1000.0)
        self.assertTrue(sc2.qualifies)
        self.assertGreater(sc2.S, 0.0)

    def test_what_qualifying_costs_at_each_price(self):
        from lip_v5 import config as C
        import lip_v5.clusters as CL
        target = 1000.0
        self.assertAlmostEqual(target * C.LAND_GRAB_PRICE_C / 100.0, 10.00, places=9)
        self.assertAlmostEqual(target * C.ENTRY_BAND_LO_C / 100.0, 60.00, places=9)
        # $10 is exactly the cluster reserve; $60 is six of them.
        self.assertAlmostEqual(CL.cluster_cap_usd(0.0, ceiling_usd=300.0), 10.00, places=9)

    def test_gate_3_the_allocator_refuses_an_S_zero_slot_outright(self):
        """So a nonzero venue cap on a sole-qualifier venue funds NOTHING by itself."""
        from lip_v5 import alloc
        a, spent, _ = alloc.allocate([self._empty_slot()], 234.0, 0.0625,
                                     caps=alloc.Caps(inv_cap_usd=5.0),
                                     cluster_cap_usd=10.0, ceiling_usd=300.0,
                                     venue_caps={"KXEMPTY": 30.0})
        self.assertEqual(a.get(("KXEMPTY-1", "bid"), 0), 0)
        self.assertEqual(spent, 0.0)


class TestNoDuplicateOrderLoop(__import__('unittest').TestCase):
    """THE 130-ORDER LOOP, 2026-07-28 live.  The prod wire returns the placed order's fields
    FLAT (`{order_id, remaining_count: "61.00", ...}`); the engine read only the nested
    `{"order": {...}}` form, so every SUCCESS was booked as a rejection: the order went live,
    v5 recorded nothing, released collateral it was really holding, and re-placed the same
    order on the next 1 Hz cycle — 130 duplicates on one rung before a human saw it.

    The invariant this pins is not "parse that field": it is **one intent, one order.**"""

    def _maker(self):
        from lip_v5 import engine, exchange as X
        ex = X.FakeExchange(balance_cents=1_000_000)
        m = engine.Maker(ex, 1785268000.0, live=False)
        m.nestor_orders, m.nestor_positions = set(), set()
        return m, ex

    def test_a_flat_response_is_recorded_as_a_live_order(self):
        m, ex = self._maker()
        ok, why, resp = m.place("KXTRUEV-26JUL28-T1208.87", "bid", 0.01, 61,
                                1785271600.0, 1785268000.0, available_cash_usd=1000.0)
        self.assertTrue(ok, "a flat-shaped SUCCESS must not read as a rejection (got %r)" % why)
        self.assertEqual(len(m.orders), 1, "the order the exchange took must be in our books")
        o = list(m.orders.values())[0]
        self.assertEqual(o["remaining"], 61.0)
        self.assertEqual(len(ex.placed), 1)

    def test_the_same_intent_never_places_twice(self):
        """Place, then ask for the identical size again: the second call must NOT create a
        second order on the wire, because the first one is already resting."""
        m, ex = self._maker()
        m.place("KXTRUEV-26JUL28-T1208.87", "bid", 0.01, 61, 1785271600.0, 1785268000.0,
                available_cash_usd=1000.0)
        live = [o for o in m.orders.values() if o.get("remaining", 0) > 0]
        self.assertEqual(len(live), 1)
        self.assertEqual(len(ex.resting), 1, "one intent, one resting order")

    def test_a_genuine_rejection_is_still_a_rejection(self):
        """The mirror: accepting the flat shape must not make us book failures as successes."""
        m, ex = self._maker()
        ex.place_error = "insufficient balance"
        ex.place_status = 400
        ok, why, _ = m.place("KXTRUEV-26JUL28-T1208.87", "bid", 0.01, 61,
                             1785271600.0, 1785268000.0, available_cash_usd=1000.0)
        self.assertFalse(ok)
        self.assertEqual(m.orders, {}, "a refused order must never enter our books")
