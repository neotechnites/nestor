"""The WS integration (spec §3.5's W2 gate over the vendored feed) and the ledger/presence
file split (spec §6.2).  Also `--check` (G1) and the external-effect guards themselves."""

import os
import unittest

from .. import config as C, ledger as L, lip_v5 as BIN, presence as P, runtime as R
from .. import ws_feed, wsgate
from .base import LipTestCase


def book(yes_c=None, no_c=None, size=10.0):
    fp = {}
    fp["yes_dollars"] = [["%.4f" % (yes_c / 100.0), "%.2f" % size]] if yes_c else []
    fp["no_dollars"] = [["%.4f" % (no_c / 100.0), "%.2f" % size]] if no_c else []
    return {"orderbook": {"orderbook_fp": fp}}


class TestVendoredFeed(LipTestCase):
    """The vendored state machine and parsers, exercised through v5's package so the import
    adaptation is proven, not assumed."""

    def test_snapshot_applies_the_live_wire_shape(self):
        b = ws_feed.BookState("T")
        self.assertEqual(b.apply_snapshot(
            {"market_ticker": "T", "yes_dollars_fp": [["0.3000", "10.00"]],
             "no_dollars_fp": [["0.6500", "5.00"]]}, now=100.0), "ok")
        yb, ya = wsgate.best_from_book(b.to_orderbook_body())
        self.assertEqual((yb, ya), (30, 35))

    def test_a_snapshot_replaces_wholesale_never_merges(self):
        b = ws_feed.BookState("T")
        b.apply_snapshot({"yes_dollars_fp": [["0.3000", "10.00"]], "no_dollars_fp": []}, 1.0)
        b.apply_snapshot({"yes_dollars_fp": [["0.4000", "10.00"]], "no_dollars_fp": []}, 2.0)
        yb, _ = wsgate.best_from_book(b.to_orderbook_body())
        self.assertEqual(yb, 40)                     # 30 is GONE, not merged

    def test_staleness(self):
        b = ws_feed.BookState("T")
        b.apply_snapshot({"yes_dollars_fp": [], "no_dollars_fp": []}, now=100.0)
        self.assertFalse(b.is_stale(105.0))
        self.assertTrue(b.is_stale(200.0))

    def test_the_runtime_adaptation_supplies_every_symbol_the_feed_needs(self):
        """The coupling surface is exactly four symbols; if any were missing the module would
        not import, but assert them by name so a future edit cannot quietly drop one."""
        for name in ("_now", "price_str", "log"):
            self.assertTrue(hasattr(R, name), name)
        self.assertTrue(hasattr(ws_feed, "Auth"))
        self.assertIs(ws_feed.M, R)


class TestWsGate(LipTestCase):
    def test_three_agreements_are_required_before_a_ws_book_may_price(self):
        g = wsgate.WsGate()
        b = book(30, 65)
        self.assertFalse(g.passed("T"))
        for i in range(2):
            verdict, passed, _ = g.observe("T", b, b)
            self.assertEqual(verdict, wsgate.WS_AGREE)
            self.assertFalse(passed)
        verdict, passed, _ = g.observe("T", b, b)
        self.assertTrue(passed)
        self.assertTrue(self.logs_of("ws_gate_passed"))

    def test_any_divergence_resets_to_zero_not_decrement(self):
        g = wsgate.WsGate()
        b = book(30, 65)
        for _ in range(3):
            g.observe("T", b, b)
        self.assertTrue(g.passed("T"))
        g.observe("T", book(31, 65), b)
        self.assertFalse(g.passed("T"))
        self.assertEqual(g.agreements["T"], 0)
        self.assertTrue(self.logs_of("ws_gate_lost"))

    def test_a_degenerate_sample_proves_nothing_and_is_not_counted(self):
        """"certifying on an empty book is certifying on nothing"."""
        g = wsgate.WsGate()
        empty = book(None, None)
        for _ in range(5):
            verdict, _, _ = g.observe("T", empty, empty)
            self.assertEqual(verdict, wsgate.WS_DEGENERATE)
        self.assertFalse(g.passed("T"))

    def test_a_dollars_vs_cents_slip_is_NAMED_not_merely_a_disagreement(self):
        """The difference between "the feed is flaky" and "the feed is 100x wrong" is the
        difference between two opposite operational responses."""
        g = wsgate.WsGate()
        ws = {"orderbook": {"orderbook_fp": {"yes_dollars": [["30.0000", "10.00"]],
                                             "no_dollars": []}}}
        rest = book(30, None)
        verdict, _, detail = g.observe("T", ws, rest)
        self.assertEqual(verdict, wsgate.WS_DIVERGE)
        self.assertIn("unit_mismatch", detail)
        self.assertTrue(self.logs_of("unit_mismatch"))

    def test_a_reconnect_clears_every_gate(self):
        g = wsgate.WsGate()
        b = book(30, 65)
        for _ in range(3):
            g.observe("T", b, b)
        g.on_epoch(1)
        g.on_epoch(2)
        self.assertFalse(g.passed("T"))
        self.assertTrue(self.logs_of("ws_gate_reset"))

    def test_an_ungated_market_falls_back_to_REST_per_market(self):
        g = wsgate.WsGate()
        rest = book(30, 65)
        body, src = g.book_for("T", feed=None, now=0.0, rest_body=rest)
        self.assertEqual(src, "rest")
        self.assertIs(body, rest)

    def test_breadth_lifts_only_while_connected(self):
        g = wsgate.WsGate()
        self.assertEqual(g.breadth(True), C.MAX_WS_MARKETS)
        self.assertEqual(g.breadth(False), C.MAX_REST_MARKETS)


class TestLedger(LipTestCase):
    def test_an_unknown_kind_is_refused_at_write_time(self):
        lg = L.Ledger(self.path("v5_ledger.jsonl"))
        lg.write("place_req", ticker="T")
        with self.assertRaises(L.SchemaMismatch):
            lg.write("not_a_real_kind", ticker="T")

    def test_every_v5_row_the_spec_names_is_writable(self):
        """spec §6.2 — the v1 vocabulary PLUS `cash_feed`, `rate_yield`, `ratchet`,
        `venue_kill`, `venue_out_of_reach`, `shade_decision`, `orphan_position`."""
        lg = L.Ledger(self.path("v5_ledger.jsonl"))
        for k in ("cash_feed", "rate_yield", "ratchet", "venue_kill", "venue_out_of_reach",
                  "shade_decision", "orphan_position"):
            lg.write(k, ticker="T")
        self.assertEqual(len(lg.read()), 7)

    def test_a_foreign_schema_ABORTS_the_replay(self):
        """v1 §9.1 — guessing at an unknown row is how a bookkeeping gap becomes a position."""
        p = self.path("v5_ledger.jsonl")
        R.append_jsonl(p, {"schema": "lip_v9_ledger/3", "k": "place_req"})
        with self.assertRaises(L.SchemaMismatch):
            L.Ledger(p).read()

    def test_presence_rows_never_enter_the_order_ledger(self):
        """spec §6.2 N2 — two derivations force the split."""
        self.assertNotIn(C.PRESENCE_KIND, C.LEDGER_KINDS)

    def test_presence_rows_rotate_daily_into_segments(self):
        pl = L.PresenceLog(self.path("v5_presence.jsonl"), self.path("v5_presence_daily.jsonl"))
        pl.write_rows([{"ticker": "T", "side": "bid", "from_ts": 0.0, "rest_dollar_s": 1.0}])
        pl.write_rows([{"ticker": "T", "side": "bid", "from_ts": 86400.0,
                        "rest_dollar_s": 2.0}])
        self.assertEqual(len(pl.read_segment(0.0)), 1)
        self.assertEqual(len(pl.read_segment(86400.0)), 1)

    def test_compaction_writes_the_aggregate_BEFORE_unlinking_the_segment(self):
        """"a metering record that can be silently rewritten is a metering record that cannot
        be trusted"."""
        pl = L.PresenceLog(self.path("v5_presence.jsonl"), self.path("v5_presence_daily.jsonl"))
        old_ts = 0.0
        pl.write_rows([{"ticker": "T", "side": "bid", "from_ts": old_ts,
                        "rest_dollar_s": 10.0, "prox_dollar_s": 4.0}])
        now = (C.PRESENCE_COMPACT_DAYS + 2) * 86400.0
        folded = pl.compact(now, P.compact_rows)
        self.assertEqual(len(folded), 1)
        self.assertFalse(os.path.exists(pl.segment_path(old_ts)))
        agg = R.read_jsonl(pl.daily_path)
        self.assertEqual(len(agg), 1)
        self.assertAlmostEqual(agg[0]["rest_dollar_s"], 10.0)

    def test_compaction_leaves_recent_segments_alone(self):
        pl = L.PresenceLog(self.path("v5_presence.jsonl"), self.path("v5_presence_daily.jsonl"))
        recent = 10 * 86400.0
        pl.write_rows([{"ticker": "T", "side": "bid", "from_ts": recent}])
        self.assertEqual(pl.compact(recent + 86400.0, P.compact_rows), [])
        self.assertTrue(os.path.exists(pl.segment_path(recent)))

    def test_coid_seq_round_trips_with_no_run_id(self):
        p = self.path("v5_coid_seq")
        L.coid_seq_store(41207, p)
        self.assertEqual(L.coid_seq_load(p), 41207)

    def test_coids_are_v5_prefixed_dot_free_and_restart_stable(self):
        """spec §11 Collisions — disjoint from v4- and from nestor's, and NO run-id, so the
        restart sweep recognises the previous process's orders."""
        coid = R.make_coid("KXAAAGASD-26JUL29-B4.120", "bid", 7)
        self.assertTrue(coid.startswith("v5-"))
        self.assertNotIn(".", coid)
        self.assertTrue(R.owns_coid(coid))
        self.assertFalse(R.owns_coid("v4-lipm-T-y-7"))
        self.assertEqual(coid, R.make_coid("KXAAAGASD-26JUL29-B4.120", "bid", 7))


class TestOrderBody(LipTestCase):
    def test_the_v3_proven_body(self):
        b = R.order_body("T", "bid", 0.30, 1_700_000_000, "v5-lipm-T-y-1", 25)
        self.assertEqual(b["time_in_force"], "good_till_canceled")
        self.assertEqual(b["self_trade_prevention_type"], "taker_at_cross")
        self.assertEqual(b["price"], "0.3000")
        self.assertEqual(b["count"], "25.00")
        self.assertIsInstance(b["expiration_ts"], int)

    def test_unit_collateral_on_the_single_yes_book(self):
        self.assertAlmostEqual(R.unit_collateral("bid", 0.30), 0.30)
        self.assertAlmostEqual(R.unit_collateral("ask", 0.30), 0.70)


class TestCheckGate(LipTestCase):
    """G1's read-out (spec §7)."""

    def test_check_runs_green_with_no_network_and_no_key(self):
        os.environ[C.NESTOR_READER_FLAG_ENV] = "true"
        try:
            ok, results = BIN.run_check(C.CASH_MODE_SHARED, data_dir=self.tmp, programs=None)
        finally:
            os.environ.pop(C.NESTOR_READER_FLAG_ENV, None)
        names = {n for n, _, _ in results}
        for expected in ("data_dir", "unit_assertion", "ledger_replay", "cash_feed_write",
                         "ws_gate", "g0_flag_matches_mode", "v4_not_running",
                         "star_reproduces_spec_0_4"):
            self.assertIn(expected, names)
        self.assertTrue(ok, [r for r in results if r[1] is False])

    def test_check_FAILS_when_mode_is_shared_and_G0s_flag_is_false(self):
        """§4.4's mirror — an unconsumed feed is a silent regression to the hand ledger."""
        os.environ.pop(C.NESTOR_READER_FLAG_ENV, None)
        ok, results = BIN.run_check(C.CASH_MODE_SHARED, data_dir=self.tmp)
        by = {n: (o, d) for n, o, d in results}
        self.assertFalse(by["g0_flag_matches_mode"][0])
        self.assertFalse(ok)

    def test_subaccount_mode_does_not_require_the_flag(self):
        os.environ.pop(C.NESTOR_READER_FLAG_ENV, None)
        ok, results = BIN.run_check(C.CASH_MODE_SUBACCOUNT, data_dir=self.tmp)
        by = {n: o for n, o, _ in results}
        self.assertTrue(by["g0_flag_matches_mode"])

    def test_check_refuses_while_a_v4_heartbeat_is_fresh(self):
        """spec §6.1 — two makers on one rung is self-trade plus double collateral."""
        R.append_jsonl(os.path.join(self.tmp, "v4_ledger.jsonl"), {"k": "place_req"})
        fresh, ts = BIN.v4_heartbeat_fresh(self.tmp, now=R._now())
        self.assertTrue(fresh)
        ok, results = BIN.run_check(C.CASH_MODE_SUBACCOUNT, data_dir=self.tmp)
        by = {n: o for n, o, _ in results}
        self.assertFalse(by["v4_not_running"])

    def test_a_stale_v4_file_does_not_refuse(self):
        p = os.path.join(self.tmp, "v4_ledger.jsonl")
        R.append_jsonl(p, {"k": "place_req"})
        old = R._now() - 10 * C.V4_HEARTBEAT_FRESH_S
        os.utime(p, (old, old))
        fresh, _ = BIN.v4_heartbeat_fresh(self.tmp, now=R._now())
        self.assertFalse(fresh)

    def test_the_unit_assertion_needs_30_matching_programs(self):
        good = [{"period_reward": 1_000_000}] * C.UNIT_ASSERT_MIN_MATCHES
        self.assertTrue(BIN.unit_assertion_check(good)[0])
        self.assertFalse(BIN.unit_assertion_check(good[:-1])[0])

    def test_a_wrong_unit_collapses_the_match_count_to_zero(self):
        """"at 1e-3 every one of them reads $1,000 and at 1e-5 every one reads $10.00, so the
        matching count collapses to zero rather than degrading"."""
        for reward in (10_000_000, 100_000):           # 10x and 0.1x the modal pool
            progs = [{"period_reward": reward}] * 500
            ok, n = BIN.unit_assertion_check(progs)
            self.assertFalse(ok)
            self.assertEqual(n, 0)

    def test_nestor_state_unreadable_is_reported(self):
        tickers, ok = BIN.nestor_open_tickers(self.path("no_such_state.json"))
        self.assertFalse(ok)
        self.assertEqual(tickers, set())

    def test_nestor_open_tickers_are_parsed(self):
        p = self.path("state.json")
        R.atomic_write_json(p, {"open_orders": [{"ticker": "KXNESTOR-1"}]})
        tickers, ok = BIN.nestor_open_tickers(p)
        self.assertTrue(ok)
        self.assertEqual(tickers, {"KXNESTOR-1"})


class TestNoExternalEffects(LipTestCase):
    """The guards themselves.  Two real incidents this week: a unit suite paged a phone, and a
    unit suite wrote outside tmp.  A regression in these guards must fail the SUITE."""

    def test_a_write_outside_the_tmpdir_is_refused(self):
        with self.assertRaises(PermissionError):
            R.atomic_write_json("/tmp/lip_v5_should_never_exist.json", {"x": 1})
        with self.assertRaises(PermissionError):
            R.append_jsonl(os.path.expanduser("~/nestor/data/nope.jsonl"), {"x": 1})

    def test_a_write_inside_the_tmpdir_is_allowed(self):
        R.atomic_write_json(self.path("ok.json"), {"x": 1})
        self.assertTrue(os.path.exists(self.path("ok.json")))

    def test_no_network_call_is_possible_while_inert(self):
        self.assertFalse(R.is_live())
        with self.assertRaises(RuntimeError):
            R._session()

    def test_ntfy_is_captured_and_never_sent(self):
        R.ntfy("day_stop", "a fixture alert")
        self.assertEqual(self.alerts, [("day_stop", "a fixture alert")])

    def test_ntfy_without_a_sink_still_cannot_page(self):
        """Belt two (NTFY_DISABLE) and belt three (not live), independently."""
        R.set_alert_sink(None)
        self.assertEqual(R.ntfy("halt", "x"), "disabled")
        os.environ.pop("NTFY_DISABLE")
        try:
            self.assertEqual(R.ntfy("halt", "x"), "inert")
        finally:
            os.environ["NTFY_DISABLE"] = "1"

    def test_every_alert_the_spec_names_exists(self):
        for name in ("halt", "poison", "day_stop", "venue_stand_down", "presence_collapse",
                     "lip_cash_feed_stale", "settlement_cash_unconfirmed", "orphan_position",
                     "adopt_basis_rejected", "rate_starved", "cancel_share_exceeded",
                     "idle_capital", "rstar_no_converge"):
            self.assertIn(name, C.ALERTS)


if __name__ == "__main__":
    unittest.main()
