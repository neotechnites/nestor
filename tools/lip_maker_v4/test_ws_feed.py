#!/usr/bin/env python3
"""
§4.6 websocket feed tests.  NO NETWORK, and every test passes with `websockets` ABSENT.

Docstrings state the DEFECT being prevented, not the behaviour being described — a
confidently-wrong book is the failure this whole module exists to stop, and it is the one
failure that produces confident quotes rather than an obvious outage.
"""

import os
import unittest

os.environ.setdefault("NTFY_DISABLE", "1")   # never page a human from the unit suite

import lip_maker_v4 as M
import ws_feed as W


T = "KXAAAGASD-26JUL28-4.100"


def snap(yes=None, no=None, ticker=T):
    return {"market_ticker": ticker, "yes": yes or [], "no": no or []}


def delta(price, d, side="yes", ticker=T):
    return {"market_ticker": ticker, "price": price, "delta": d, "side": side}


class BookStateRebuild(unittest.TestCase):

    def test_snapshot_then_deltas_rebuild_the_exact_book(self):
        b = W.BookState(T)
        self.assertEqual(b.apply_snapshot(snap([[68, 35], [67, 40]], [[31, 5]]), 1.0), "ok")
        self.assertEqual(b.yes, {68: 35.0, 67: 40.0})
        self.assertEqual(b.apply_delta(delta(68, 15), 2.0), "ok")
        self.assertEqual(b.yes[68], 50.0)
        self.assertEqual(b.apply_delta(delta(66, 10), 3.0), "ok")     # a NEW level
        self.assertEqual(b.yes[66], 10.0)
        self.assertEqual(b.apply_delta(delta(31, -2, "no"), 4.0), "ok")
        self.assertEqual(b.no, {31: 3.0})
        self.assertEqual(b.last_update_ts, 4.0)

    def test_a_snapshot_replaces_wholesale_and_never_merges(self):
        """Merging would RESURRECT a level the exchange no longer lists, and a resurrected
        level is depth that is not there — which reads to §1.2 as a thicker book."""
        b = W.BookState(T)
        b.apply_snapshot(snap([[68, 35], [67, 40]]), 1.0)
        b.apply_snapshot(snap([[68, 10]]), 2.0)
        self.assertEqual(b.yes, {68: 10.0})
        self.assertNotIn(67, b.yes)

    def test_a_level_reaching_zero_is_removed_not_kept_at_zero(self):
        """A 0-size level left in the dict is a phantom price that best_from_book()'s max()
        would happily return as the best bid."""
        b = W.BookState(T)
        b.apply_snapshot(snap([[68, 35], [67, 40]]), 1.0)
        self.assertEqual(b.apply_delta(delta(68, -35), 2.0), "ok")
        self.assertNotIn(68, b.yes)
        yb, _ = M.best_from_book(b.to_orderbook_body())
        self.assertEqual(yb, 67)

    def test_a_negative_result_is_CORRUPTION_and_is_never_clamped(self):
        """max(0, ...) here converts a PROVABLY LOST message into a book that is merely
        wrong, and a wrong book prices confident quotes.  Latch and discard instead."""
        b = W.BookState(T)
        b.apply_snapshot(snap([[68, 35]]), 1.0)
        self.assertEqual(b.apply_delta(delta(68, -40), 2.0), "corrupt")
        self.assertTrue(b.corrupt)
        self.assertIn("negative_size", b.corrupt_reason)
        self.assertEqual(b.yes[68], 35.0)              # untouched, not clamped to 0
        self.assertTrue(b.is_stale(2.0))               # and unusable at any age
        # only a fresh snapshot clears it
        b.apply_snapshot(snap([[68, 5]]), 3.0)
        self.assertFalse(b.corrupt)
        self.assertFalse(b.is_stale(3.0))

    def test_a_delta_with_no_snapshot_under_it_mutates_nothing(self):
        """Applying it to an empty dict SYNTHESISES a one-level book, and a one-level book
        reads as a thin book — which is a buy signal."""
        b = W.BookState(T)
        self.assertEqual(b.apply_delta(delta(68, 35), 1.0), "no_base")
        self.assertEqual(b.yes, {})
        self.assertTrue(b.is_stale(1.0))
        self.assertEqual(b.last_update_ts, 0.0)

    def test_malformed_frames_are_refused_without_partial_application(self):
        b = W.BookState(T)
        b.apply_snapshot(snap([[68, 35]]), 1.0)
        for bad in (None, "nope", {"side": "yes"}, {"side": "wat", "price": 1, "delta": 1},
                    {"side": "yes", "price": "x", "delta": 1}):
            self.assertIn(b.apply_delta(bad, 2.0), ("bad_delta", "no_base"))
        self.assertEqual(b.yes, {68: 35.0})
        # a half-readable SNAPSHOT must not half-apply
        self.assertEqual(b.apply_snapshot({"yes": [[68, 35], ["x"]]}, 3.0), "bad_snapshot")
        self.assertEqual(b.yes, {68: 35.0})

    def test_staleness(self):
        b = W.BookState(T)
        b.apply_snapshot(snap([[68, 35]]), 100.0)
        self.assertFalse(b.is_stale(100.0 + W.WS_MAX_BOOK_AGE_S - 0.1))
        self.assertTrue(b.is_stale(100.0 + W.WS_MAX_BOOK_AGE_S + 0.1))
        # "we never had it" never becomes fresh with age
        self.assertTrue(W.BookState("X").is_stale(0.0))


class IntegrationContract(unittest.TestCase):
    """The consumer must not need to change.  These assert against lip_maker_v4's OWN
    parsers, so a drift in either file fails here rather than in production."""

    def test_ws_body_parses_identically_to_a_REST_body(self):
        b = W.BookState(T)
        b.apply_snapshot(snap([[68, 35], [67, 40], [66, 16]],
                              [[31, 5], [30, 8]]), 1.0)
        ws_body = b.to_orderbook_body()
        rest_body = {"orderbook": {"orderbook_fp": {
            "yes_dollars": [["0.6800", "35.00"], ["0.6700", "40.00"], ["0.6600", "16.00"]],
            "no_dollars": [["0.3100", "5.00"], ["0.3000", "8.00"]]}}}
        self.assertEqual(M.best_from_book(ws_body), M.best_from_book(rest_body))
        self.assertEqual(sorted(M.book_levels(ws_body)[0]),
                         sorted(M.book_levels(rest_body)[0]))
        self.assertEqual(sorted(M.book_levels(ws_body)[1]),
                         sorted(M.book_levels(rest_body)[1]))
        for mode in ("cents", "levels"):
            a = M.score_side(M.book_levels(ws_body)[0], 10, 0.5, mode)
            c = M.score_side(M.book_levels(rest_body)[0], 10, 0.5, mode)
            self.assertAlmostEqual(a.S, c.S, places=9)
            self.assertEqual(a.ref_c, c.ref_c)

    def test_the_measured_gas_fixture_survives_the_round_trip(self):
        """verify-lip-gas §3b: 4.100's yes side scores 60.5.  If the wire conversion is
        wrong anywhere, this is the number that moves."""
        levels = [(68, 35), (67, 40), (66, 16), (65, 8), (64, 8), (40, 2379), (1, 1003)]
        b = W.BookState(T)
        b.apply_snapshot(snap([[p, s] for p, s in levels]), 1.0)
        got = M.score_side(M.book_levels(b.to_orderbook_body())[0], 1000, 0.5, "cents")
        self.assertAlmostEqual(got.S, 60.5, delta=0.1)
        self.assertEqual(got.ref_c, 68)
        self.assertTrue(got.qualifies)

    def test_fractional_sizes_survive(self):
        """The measured NO side of 4.100 has a 0.16-lot top level."""
        b = W.BookState(T)
        b.apply_snapshot(snap([], [[31, 0.16], [30, 8.08]]), 1.0)
        _, no = M.book_levels(b.to_orderbook_body())
        self.assertEqual(dict(no)[31], 0.16)

    def test_an_empty_book_is_legal_and_parses(self):
        b = W.BookState(T)
        b.apply_snapshot(snap([], []), 1.0)
        self.assertEqual(M.best_from_book(b.to_orderbook_body()), (None, None))


class SeqTracking(unittest.TestCase):
    """The single most important property in the file: a silently-wrong book produces
    confidently-wrong quotes, where an outage merely produces no quotes."""

    def test_contiguous_is_ok_and_any_start_value_is_accepted(self):
        s = W.SeqTracker()
        self.assertEqual(s.check(1, 8342), W.SEQ_OK)     # we do not assume it starts at 1
        self.assertEqual(s.check(1, 8343), W.SEQ_OK)
        self.assertEqual(s.check(1, 8344), W.SEQ_OK)

    def test_a_gap_is_a_gap_and_is_not_recorded(self):
        """Not recording is what makes one loss cost exactly ONE resubscribe instead of
        storming: the feed decides, via rebase(), when the counter moves on."""
        s = W.SeqTracker()
        s.check(1, 10)
        self.assertEqual(s.check(1, 12), W.SEQ_GAP)
        self.assertEqual(s.check(1, 12), W.SEQ_GAP)      # still a gap, not silently accepted
        s.rebase(1, 12)
        self.assertEqual(s.check(1, 13), W.SEQ_OK)

    def test_a_duplicate_is_ignored_because_deltas_are_not_idempotent(self):
        """Applying +5 twice is +10.  A retransmit reapplied is a corrupt book that never
        reports itself."""
        s = W.SeqTracker()
        s.check(1, 10)
        self.assertEqual(s.check(1, 10), W.SEQ_DUPLICATE)
        self.assertEqual(s.check(1, 11), W.SEQ_OK)

    def test_an_UNARMED_backwards_jump_is_a_gap_not_a_duplicate(self):
        """Delivery is ordered, so an unexplained rewind is a stream we no longer understand.
        Calling it a duplicate would IGNORE real deltas and let the book drift silently."""
        s = W.SeqTracker()
        s.check(1, 100)
        self.assertEqual(s.check(1, 40), W.SEQ_GAP)

    def test_an_ARMED_restart_is_accepted_after_a_resubscribe(self):
        s = W.SeqTracker()
        s.check(1, 100)
        s.arm_reset(1)
        self.assertEqual(s.check(1, 1), W.SEQ_RESET)
        self.assertEqual(s.check(1, 2), W.SEQ_OK)
        # arming is one-shot: a second rewind is a gap again
        self.assertEqual(s.check(1, 1), W.SEQ_GAP)

    def test_an_unreadable_seq_is_a_lost_seq(self):
        s = W.SeqTracker()
        s.check(1, 10)
        for bad in (None, "x", {}):
            self.assertEqual(s.check(1, bad), W.SEQ_GAP)

    def test_sids_are_tracked_independently(self):
        s = W.SeqTracker()
        s.check(1, 10)
        s.check(2, 500)
        self.assertEqual(s.check(1, 11), W.SEQ_OK)
        self.assertEqual(s.check(2, 501), W.SEQ_OK)
        s.forget(1)
        self.assertEqual(s.check(1, 999), W.SEQ_OK)      # forgotten -> first message again
        self.assertEqual(s.check(2, 502), W.SEQ_OK)


class Backoff(unittest.TestCase):

    def test_it_grows_bounded_and_jittered(self):
        import random
        rng = random.Random(7)
        delays = [W.backoff_delay(i, rng) for i in range(12)]
        for d in delays:
            self.assertGreaterEqual(d, 0.0)
            self.assertLessEqual(d, W.WS_BACKOFF_CAP_S * (1.0 + W.WS_BACKOFF_JITTER))
        self.assertLess(delays[0], delays[5])            # grows
        self.assertLessEqual(delays[-1], W.WS_BACKOFF_CAP_S * (1.0 + W.WS_BACKOFF_JITTER))

    def test_jitter_makes_reconnects_disagree(self):
        """Without jitter every disconnected client returns at the same instant, which is
        how a recovering exchange gets knocked over a second time."""
        import random
        a = W.backoff_delay(4, random.Random(1).random)
        b = W.backoff_delay(4, random.Random(2).random)
        self.assertNotEqual(a, b)
        # and the PRODUCTION path (rng=None -> random.random) really does jitter
        live = {W.backoff_delay(4) for _ in range(8)}
        self.assertGreater(len(live), 1)
        self.assertNotEqual(live, {W.WS_BACKOFF_BASE_S * W.WS_BACKOFF_MULT ** 4})

    def test_a_broken_rng_degrades_to_UNJITTERED_not_to_a_crash(self):
        """`rng` is a CALLABLE returning a float, not a Random INSTANCE.  Passing the
        instance makes backoff_delay swallow the TypeError and fall back to u=0.5, i.e. NO
        jitter at all — lockstep reconnects with no signal that jitter was lost.  The
        production call passes rng=None so this is not live, but the swallow is wide enough
        to hide it and a reviewer should know the shape."""
        import random
        instance = random.Random(1)
        self.assertFalse(callable(instance))
        unjittered = W.backoff_delay(4, instance)
        self.assertAlmostEqual(unjittered, W.WS_BACKOFF_BASE_S * W.WS_BACKOFF_MULT ** 4,
                               places=9)
        self.assertEqual(W.backoff_delay(4, instance), unjittered)   # deterministic: no jitter


class FeedStateMachine(unittest.TestCase):
    """The protocol core is pure, so the whole state machine is exercised with no socket."""

    def setUp(self):
        self.now = [1000.0]
        self.feed = W.WsFeed(auth=None, tickers=[T], clock=lambda: self.now[0])

    def tearDown(self):
        try:
            self.feed.stop(timeout=0.1)
        except Exception:
            pass

    def _sub(self):
        f = self.feed.subscribe_frame()
        self.feed.handle_frame({"type": "subscribed", "id": f["id"], "sid": 7,
                                "msg": {"channel": W.WS_CHANNEL}})
        return 7

    def test_a_snapshot_makes_a_book_fresh_and_deltas_keep_it_fresh(self):
        self.feed.on_open()
        sid = self._sub()
        self.feed.handle_frame({"type": "orderbook_snapshot", "sid": sid, "seq": 1,
                                "msg": snap([[68, 35]], [[31, 5]])})
        self.assertIsNotNone(self.feed.book_or_none(T))
        self.assertTrue(self.feed.is_fresh(T))
        self.now[0] += 1.0
        self.feed.handle_frame({"type": "orderbook_delta", "sid": sid, "seq": 2,
                                "msg": delta(68, 5)})
        yb, _ = M.best_from_book(self.feed.book_or_none(T))
        self.assertEqual(yb, 68)
        self.assertEqual(M.book_levels(self.feed.book_or_none(T))[0][0][1], 40.0)

    def test_a_seq_gap_drops_the_book_and_demands_a_resubscribe(self):
        """A gap means the book is UNKNOWABLE.  Serving it anyway is the confidently-wrong
        book; serving None makes the consumer fall back to REST, which is merely slower."""
        self.feed.on_open()
        sid = self._sub()
        self.feed.handle_frame({"type": "orderbook_snapshot", "sid": sid, "seq": 1,
                                "msg": snap([[68, 35]])})
        self.assertIsNotNone(self.feed.book_or_none(T))
        self.feed.handle_frame({"type": "orderbook_delta", "sid": sid, "seq": 9,
                                "msg": delta(68, 5)})
        self.assertIsNone(self.feed.book_or_none(T))     # dropped, not served stale
        self.assertTrue(self.feed.needs_resubscribe())
        self.assertGreaterEqual(self.feed.health()["gaps"], 1)

    def test_corruption_drops_the_book_too(self):
        self.feed.on_open()
        sid = self._sub()
        self.feed.handle_frame({"type": "orderbook_snapshot", "sid": sid, "seq": 1,
                                "msg": snap([[68, 35]])})
        self.feed.handle_frame({"type": "orderbook_delta", "sid": sid, "seq": 2,
                                "msg": delta(68, -99)})
        self.assertIsNone(self.feed.book_or_none(T))
        self.assertGreaterEqual(self.feed.health()["corruptions"], 1)

    def test_a_duplicate_delta_is_not_applied_twice(self):
        self.feed.on_open()
        sid = self._sub()
        self.feed.handle_frame({"type": "orderbook_snapshot", "sid": sid, "seq": 1,
                                "msg": snap([[68, 35]])})
        self.feed.handle_frame({"type": "orderbook_delta", "sid": sid, "seq": 2,
                                "msg": delta(68, 5)})
        self.feed.handle_frame({"type": "orderbook_delta", "sid": sid, "seq": 2,
                                "msg": delta(68, 5)})
        self.assertEqual(M.book_levels(self.feed.book_or_none(T))[0][0][1], 40.0)

    def test_an_error_frame_does_not_kill_the_loop(self):
        self.feed.on_open()
        sid = self._sub()
        self.feed.handle_frame({"type": "error", "msg": {"code": 6, "msg": "bad ticker"}})
        self.feed.handle_frame({"type": "orderbook_snapshot", "sid": sid, "seq": 1,
                                "msg": snap([[68, 35]])})
        self.assertIsNotNone(self.feed.book_or_none(T))
        self.assertGreaterEqual(self.feed.health()["errors"], 1)

    def test_garbage_frames_never_raise(self):
        self.feed.on_open()
        for junk in (None, "", 12, [], {"type": "unknown"}, {"type": "orderbook_delta"},
                     {"type": "orderbook_snapshot", "msg": None}):
            self.feed.handle_frame(junk)
        self.assertIsNone(self.feed.book_or_none(T))

    def test_a_reconnect_forgets_every_book(self):
        """Deltas were CERTAINLY missed while we were down, so nothing may survive a
        reconnect — restoring the pre-disconnect book is the corrupt-book failure with extra
        steps."""
        self.feed.on_open()
        sid = self._sub()
        self.feed.handle_frame({"type": "orderbook_snapshot", "sid": sid, "seq": 1,
                                "msg": snap([[68, 35]])})
        self.assertIsNotNone(self.feed.book_or_none(T))
        self.feed.on_close("socket dropped")
        self.assertIsNone(self.feed.book_or_none(T))
        self.feed.on_open()
        self.assertIsNone(self.feed.book_or_none(T))
        self.assertTrue(self.feed.needs_resubscribe())

    def test_a_stale_book_is_withheld(self):
        self.feed.on_open()
        sid = self._sub()
        self.feed.handle_frame({"type": "orderbook_snapshot", "sid": sid, "seq": 1,
                                "msg": snap([[68, 35]])})
        self.assertIsNotNone(self.feed.book_or_none(T))
        self.now[0] += W.WS_MAX_BOOK_AGE_S + 1.0
        self.assertIsNone(self.feed.book_or_none(T))
        self.assertEqual(self.feed.books_for([T]), {})

    def test_books_for_returns_only_fresh_markets(self):
        other = "KXOTHER-1"
        self.feed.set_tickers([T, other])
        self.feed.on_open()
        sid = self._sub()
        self.feed.handle_frame({"type": "orderbook_snapshot", "sid": sid, "seq": 1,
                                "msg": snap([[68, 35]])})
        got = self.feed.books_for([T, other])
        self.assertIn(T, got)
        self.assertNotIn(other, got)                     # never snapshotted

    def test_health_reports_without_a_socket(self):
        h = self.feed.health()
        for k in ("connected", "subscribed_n", "stale_n", "reconnects", "gaps"):
            self.assertIn(k, h)
        self.assertFalse(h["connected"])


class DegradesWithoutTheLibrary(unittest.TestCase):
    """stdlib+requests is the floor.  If `websockets` is not installed on the box, the maker
    must run exactly as it does today on REST — not crash, and not silently quote off
    nothing."""

    def setUp(self):
        self._real = W.websockets
        W.websockets = None
        W.detach()

    def tearDown(self):
        W.websockets = self._real
        W.detach()

    def test_attach_returns_a_dead_feed_and_never_raises(self):
        feed = W.attach(maker=None, auth=None, tickers=[T])
        self.assertIsNotNone(feed)
        self.assertEqual(feed.books_for([T]), {})
        self.assertIsNone(W.ws_book_or_none(T))
        h = W.health()
        self.assertFalse(h["connected"])
        self.assertFalse(h.get("available", False))

    def test_the_accessor_is_none_with_no_feed_attached(self):
        W.detach()
        self.assertIsNone(W.ws_book_or_none(T))
        self.assertFalse(W.health()["attached"])

    def test_attach_is_idempotent(self):
        a = W.attach(tickers=[T])
        b = W.attach(tickers=[T, "KXOTHER-1"])
        self.assertIs(a, b)
        self.assertIn("KXOTHER-1", b.tickers)


class BreadthCap(unittest.TestCase):

    def test_the_ws_cap_is_materially_larger_than_the_rest_clamp_and_bounded(self):
        """§4.6: 'breadth past 6 REQUIRES the websocket'.  A cap that is not much larger
        buys nothing; one that is unbounded ignores the rate budget the clamp exists for."""
        self.assertGreater(W.MAX_WS_MARKETS, M.MAX_REST_MARKETS)
        self.assertGreaterEqual(W.MAX_WS_MARKETS, 4 * M.MAX_REST_MARKETS)
        self.assertLessEqual(W.MAX_WS_MARKETS, 200)

    def test_the_subscription_is_clamped_to_the_cap(self):
        feed = W.WsFeed(auth=None, tickers=["M%03d" % i
                                            for i in range(W.MAX_WS_MARKETS + 25)])
        self.assertLessEqual(len(feed.tickers), W.MAX_WS_MARKETS)
        self.assertLessEqual(len(feed.subscribe_frame()["params"]["market_tickers"]),
                             W.MAX_WS_MARKETS)


class ConsumerSeam(unittest.TestCase):
    """The seam ships INERT in the morning capital-raise deploy and is turned on by one flag
    at mid-morning, so the ceiling raise and the websocket are never the same change."""

    def setUp(self):
        self.old = M.WS_ENABLED
        W.detach()

    def tearDown(self):
        M.WS_ENABLED = self.old
        W.detach()

    def test_the_lazy_import_actually_resolves(self):
        """ws_feed imports lip_maker_v4, so a module-scope import here is a CYCLE that
        fails, gets swallowed, and leaves the seam permanently None — WS_ENABLED would flip
        on at mid-morning and do nothing at all, silently falling back to REST.  A feature
        that looks deployed and is inert is worse than one that fails loudly."""
        mod = M.ws_module()
        self.assertIsNotNone(mod)
        self.assertIs(mod, W)
        self.assertIs(M.ws_module(), mod)                    # cached

    def test_it_is_off_by_default_and_the_maker_is_pure_REST(self):
        self.assertFalse(M.WS_ENABLED)
        m = M.Maker(None, M.LedgerState(), [])
        self.assertIsNone(m.attach_ws([]))
        self.assertIsNone(m.ws_book(T))
        self.assertEqual(m.poll_cap(), M.MAX_REST_MARKETS)

    def test_the_cap_only_lifts_while_the_socket_is_actually_connected(self):
        """A configured-but-DOWN websocket must not buy breadth it cannot serve — that is
        the one way this feature could lose money rather than merely fail."""
        m = M.Maker(None, M.LedgerState(), [])
        M.WS_ENABLED = True
        m.ws = type("F", (), {"health": lambda self: {"connected": False}})()
        self.assertEqual(m.poll_cap(), M.MAX_REST_MARKETS)
        m.ws = type("F", (), {"health": lambda self: {"connected": True}})()
        self.assertEqual(m.poll_cap(), max(M.MAX_REST_MARKETS, W.MAX_WS_MARKETS))
        self.assertGreater(m.poll_cap(), M.MAX_REST_MARKETS)

    def test_a_raising_feed_degrades_to_REST_rather_than_propagating(self):
        m = M.Maker(None, M.LedgerState(), [])
        M.WS_ENABLED = True

        class Boom(object):
            def health(self):
                raise RuntimeError("socket exploded")

        m.ws = Boom()
        self.assertEqual(m.poll_cap(), M.MAX_REST_MARKETS)   # no raise
        self.assertIsNone(m.ws_book(T))

    def test_the_inventory_guarantee_still_holds_at_the_wider_cap(self):
        """MIN_RANK_POLL_SLOTS and the shed-only rule are cap-relative, so lifting breadth
        must not let inventory be dropped again."""
        m = M.Maker(None, M.LedgerState(), [])
        M.WS_ENABLED = True
        m.ws = type("F", (), {"health": lambda self: {"connected": True}})()
        n = W.MAX_WS_MARKETS + 10
        tickers = ["M%03d" % i for i in range(n)]
        progs = {t: {"program_id": "P" + t, "market_ticker": t, "series": "KX",
                     "period_reward": 1e6, "target_size_fp": 1000.0,
                     "discount_factor_bps": 5000.0, "start_ts": 0.0, "end_ts": 9e9,
                     "paid_out": False} for t in tickers}
        for i, t in enumerate(tickers):
            m.classified[t] = {"rho": 6.25, "pinned": False, "denied": False,
                               "sides": [{"S": 10.0 + i, "p": 0.40, "qualifies": True}]}
        last = tickers[-1]
        m.st.positions[last] = {"yes": 25.0, "no": 0.0}
        m.st.position_cost[last] = 10.0
        m.st.position_cost_leg[last] = {"yes": 10.0, "no": 0.0}
        chosen, shed_only = m.poll_set(progs, 1000.0)
        self.assertLessEqual(len(chosen), m.poll_cap())
        self.assertIn(last, chosen)                          # inventory still never dropped
        self.assertIn(last, shed_only)
        rank = len([t for t in chosen if t not in shed_only])
        self.assertGreaterEqual(rank, M.MIN_RANK_POLL_SLOTS)
        self.assertGreater(len(chosen), M.MAX_REST_MARKETS)  # breadth really did lift


class N2_ReconnectCostsTrust(unittest.TestCase):

    def test_the_feed_bumps_a_reproof_epoch_on_every_connect(self):
        f = W.WsFeed(auth=None, tickers=[T])
        self.assertEqual(f.reproof_epoch(), 0)
        f.on_open()
        self.assertEqual(f.reproof_epoch(), 1)
        f.on_close("dropped")
        f.on_open()
        self.assertEqual(f.reproof_epoch(), 2)

    def test_a_reconnect_drops_every_markets_trust_in_the_consumer(self):
        """A reconnect resubscribes and re-snapshots; a bad resubscribe snapshot arriving
        into RETAINED trust is exactly the unverified book driving quotes that W2 exists to
        prevent.  The gap is when the gate matters most, not a moment to skip it."""
        old = M.WS_ENABLED
        try:
            M.WS_ENABLED = True
            feed = W.WsFeed(auth=None, tickers=[T])
            feed.on_open()
            m = M.Maker(None, M.LedgerState(), [])
            m.ws = feed
            m.ws_epoch = feed.reproof_epoch()
            m.ws_agreements[T] = M.WS_AGREE_REQUIRED
            m.ws_verified_ts[T] = 1000.0
            self.assertTrue(m.ws_trusted(T, 1000.0))
            feed.on_close("socket dropped")
            feed.on_open()                        # epoch moves
            self.assertFalse(m.ws_trusted(T, 1000.0))
            self.assertEqual(m.ws_agreements.get(T, 0), 0)
        finally:
            M.WS_ENABLED = old

class AuthSigning(unittest.TestCase):

    def test_the_ws_signature_covers_the_ws_path_and_excludes_any_query(self):
        """R166 again, on a new surface: the signed path must be the bare ws path."""
        class FakeKey(object):
            def __init__(self):
                self.msg = None

            def sign(self, msg, *a, **k):
                self.msg = msg
                return b"sig"

        fk = FakeKey()
        h = W.ws_auth_headers(M.Auth("kid", fk))
        self.assertTrue(fk.msg.endswith(b"GET" + W.WS_SIGNED_PATH.encode()))
        self.assertNotIn(b"?", fk.msg)
        self.assertIn("KALSHI-ACCESS-KEY", h)
        self.assertIn("KALSHI-ACCESS-SIGNATURE", h)
        self.assertIn("KALSHI-ACCESS-TIMESTAMP", h)

    def test_no_auth_yields_no_headers_rather_than_a_crash(self):
        self.assertEqual(W.ws_auth_headers(None), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
