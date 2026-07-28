"""THE ASSEMBLED LOOP — scan → classify → slots → cycle, and the outer runner.

The property extended here from `test_engine.py`: **one path to the wire, in the assembled
system**.  `test_no_write_reaches_the_exchange_except_through_place` drives whole iterations and
asserts that everything the exchange saw was placed by `Maker.place`.
"""

import unittest

from .. import config as C, cutover, exchange as X, guards as G, runner as RUN, runtime as R
from .. import scan
from .base import LipTestCase
from .test_engine import CONFIG_PATHS, EngineCase

NOW = 1_000_000.0


def book(yes=(("0.40", "1200"),), no=(("0.58", "1200"),)):
    return {"orderbook": {"orderbook_fp": {
        "yes_dollars": [list(x) for x in yes], "no_dollars": [list(x) for x in no]}}}


def program_body(series="KXAAAGASD", tickers=("KXAAAGASD-26JUL29-T4.12",),
                 reward=1_000_000, start=NOW - 3600, end=NOW + 16 * 3600):
    from datetime import datetime, timezone
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"liquidity_incentive_programs": [{
        "id": "prog-1", "series_ticker": series, "market_tickers": list(tickers),
        "period_reward": reward, "start_date": iso(start), "end_date": iso(end),
        "target_size_fp": 1000}]}


class ScanExchange(X.FakeExchange):
    def __init__(self, programs=None, **kw):
        super(ScanExchange, self).__init__(**kw)
        self._programs = programs if programs is not None else program_body()
        self.program_calls = 0
        self.book_calls = 0

    def programs(self, cursor=None):
        self.program_calls += 1
        return 200, self._programs

    def book(self, ticker):
        self.book_calls += 1
        return 200, book()


class TestScanStage(LipTestCase):
    def test_programs_parse_into_rho_over_the_programs_OWN_window(self):
        progs = scan.parse_programs(program_body())
        self.assertEqual(len(progs), 1)
        p = progs[0]
        self.assertAlmostEqual(scan.pool_usd(1_000_000), 100.00, places=6)
        self.assertAlmostEqual(p["window_h"], 17.0, places=6)   # start NOW−1h, end NOW+16h
        self.assertAlmostEqual(p["rho"], 100.0 / 17.0, places=6)

    def test_an_unparseable_program_is_DROPPED_loudly(self):
        body = {"liquidity_incentive_programs": [{"id": "x", "start_date": "nonsense"}]}
        self.assertEqual(scan.parse_programs(body), [])
        self.assertTrue(self.logs_of("program_unparseable"))

    def test_the_scan_is_cadence_gated(self):
        ex = ScanExchange()
        s = scan.Scanner()
        from .. import ratelimit as RL
        b = RL.Bucket(NOW)
        s.scan(ex, b, NOW)
        self.assertEqual(ex.program_calls, 1)
        s.scan(ex, b, NOW + 10)
        self.assertEqual(ex.program_calls, 1)                 # not due
        s.scan(ex, b, NOW + C.SCAN_REFRESH_S + 1)
        self.assertEqual(ex.program_calls, 2)

    def test_the_scan_draws_from_the_rate_budget_and_defers_when_empty(self):
        ex = ScanExchange()
        s = scan.Scanner()
        from .. import ratelimit as RL
        b = RL.Bucket(NOW)
        b.tokens = 0.0
        self.assertEqual(s.scan(ex, b, NOW), [])
        self.assertEqual(ex.program_calls, 0)
        self.assertTrue(self.logs_of("scan_deferred"))

    def test_a_partial_scan_keeps_what_it_has_and_does_not_halt(self):
        ex = ScanExchange()
        s = scan.Scanner()
        from .. import ratelimit as RL
        b = RL.Bucket(NOW)
        s.scan(ex, b, NOW)
        first = list(s.programs)
        b.tokens = 0.0
        self.assertEqual(s.scan(ex, b, NOW + C.SCAN_REFRESH_S + 1), first)


class TestClassifyStage(LipTestCase):
    def _cls(self, ex=None):
        from .. import ratelimit as RL
        ex = ex or ScanExchange()
        b = RL.Bucket(NOW)
        progs = scan.parse_programs(ex._programs)          # the EXCHANGE's body, not a default
        c = scan.Classifier()
        c.sweep(ex, b, progs, NOW)
        return c, ex

    def test_it_learns_pinned_qualifies_S_and_p(self):
        c, _ = self._cls()
        rec = c.table["KXAAAGASD-26JUL29-T4.12"]
        self.assertFalse(rec["pinned"])
        self.assertTrue(rec["sides"]["bid"]["qualifies"])
        self.assertAlmostEqual(rec["sides"]["bid"]["p"], 0.40, places=6)
        self.assertGreater(rec["sides"]["bid"]["S"], 0)

    def test_a_denied_series_never_costs_a_request(self):
        """The deny list is applied BEFORE we spend budget on the market."""
        ex = ScanExchange(programs=program_body(
            series="KXEARNINGSMENTIONPYPL", tickers=("KXEARNINGSMENTIONPYPL-PERP",)))
        c, ex = self._cls(ex)
        self.assertEqual(ex.book_calls, 0)
        self.assertEqual(c.table, {})

    def test_it_is_cadence_gated_per_ticker(self):
        c, ex = self._cls()
        before = ex.book_calls
        from .. import ratelimit as RL
        c.sweep(ex, RL.Bucket(NOW), scan.parse_programs(program_body()), NOW + 10)
        self.assertEqual(ex.book_calls, before)
        c.sweep(ex, RL.Bucket(NOW), scan.parse_programs(program_body()),
                NOW + C.CLASSIFY_REFRESH_S + 1)
        self.assertGreater(ex.book_calls, before)

    def test_a_pinned_book_is_detected(self):
        class Pinned(ScanExchange):
            def book(self, ticker):
                return 200, book(yes=(("0.99", "1200"),), no=(("0.005", "1200"),))
        c, _ = self._cls(Pinned())
        self.assertTrue(c.table["KXAAAGASD-26JUL29-T4.12"]["pinned"])


class TestSlotTable(LipTestCase):
    def _slots(self, now=NOW, **kw):
        from .. import ratelimit as RL
        ex = ScanExchange(**kw)
        progs = scan.parse_programs(ex._programs)
        c = scan.Classifier()
        c.sweep(ex, RL.Bucket(now), progs, now)
        return scan.build_slots(progs, c, now), progs, c

    def test_slots_carry_every_input_star_needs(self):
        slots, _, _ = self._slots()
        self.assertTrue(slots)
        s = slots[0]
        for attr in ("rho", "S", "p", "phi", "d", "l_eff", "t_hat", "close_ts",
                     "program_end_ts", "hours_left", "venue"):
            self.assertIsNotNone(getattr(s, attr), attr)
        self.assertEqual(s.venue, "KXAAAGASD")

    def test_the_window_END_guard_excludes_a_dying_program(self):
        """735 lots with 25 minutes left: ALLOCATE optimises a RATE and cannot see the clock."""
        slots, _, _ = self._slots(now=NOW + 16 * 3600 - 60)
        self.assertEqual(slots, [])

    def test_the_window_START_guard_excludes_a_program_that_has_not_opened(self):
        ex_kw = {"programs": program_body(start=NOW + 10.5 * 3600, end=NOW + 26 * 3600)}
        slots, _, _ = self._slots(**ex_kw)
        self.assertEqual(slots, [])

    def test_a_program_inside_the_prepositioning_lead_IS_admitted(self):
        ex_kw = {"programs": program_body(start=NOW + 600, end=NOW + 16 * 3600)}
        slots, _, _ = self._slots(**ex_kw)
        self.assertTrue(slots)

    def test_runway_ok_is_the_reachability_test(self):
        self.assertTrue(scan.runway_ok(6.25, 2.0))
        self.assertFalse(scan.runway_ok(6.25, 0.1))
        self.assertTrue(scan.runway_ok(6.25, 0.01, accrued_usd=C.ENTRY_FLOOR_USD))

    def test_a_denied_series_never_becomes_a_slot(self):
        ex_kw = {"programs": program_body(series="KXRAIN", tickers=("KXRAIN-1",))}
        slots, _, _ = self._slots(**ex_kw)
        self.assertEqual(slots, [])

    def test_a_frozen_ticker_never_becomes_a_slot(self):
        slots, progs, c = self._slots()
        frozen = scan.build_slots(progs, c, NOW, frozen={"KXAAAGASD-26JUL29-T4.12"})
        self.assertEqual(frozen, [])

    def test_a_full_book_needs_no_land_grab(self):
        slots, _, _ = self._slots()
        self.assertTrue(all(s.land_grab_size == 0 for s in slots))

    def test_an_unqualified_side_gets_the_qualification_size(self):
        """§4.5 — at S≈0 ALLOCATE correctly assigns nothing; the qualification path is what
        creates the side, and its size is `target_size − cum_size`."""
        from .. import ratelimit as RL

        class Thin(ScanExchange):
            def book(self, ticker):
                return 200, book(yes=(("0.40", "10"),), no=(("0.58", "1200"),))

        ex = Thin()
        progs = scan.parse_programs(ex._programs)
        c = scan.Classifier()
        c.sweep(ex, RL.Bucket(NOW), progs, NOW)
        slots = scan.build_slots(progs, c, NOW)
        bid = [s for s in slots if s.side == "bid"][0]
        self.assertEqual(bid.land_grab_size, 990)          # 1000 target − 10 resting

    def test_P6_the_pre_entry_filter_seam_exists_and_its_ABSENCE_is_logged(self):
        """note 43 §5's mirror: "zero fills forever means either the perfect rewards venue or a
        market nobody wants — the difference is whether ANYONE EVER TRADES THERE AT ALL."

        The filter needs the public trade tape, which this build does not pull.  An unwired
        filter that LOOKS wired is the same defect class as a constant with no call site, so the
        absence is logged once per process and the seam is a real parameter.
        """
        scan._P6_WARNED = False
        slots, progs, c = self._slots()
        self.assertTrue(self.logs_of("p6_pre_entry_filter_UNWIRED"))
        # ...and when it IS supplied, it excludes
        none_traded = scan.build_slots(progs, c, NOW, p6=lambda t: False)
        self.assertEqual(none_traded, [])
        all_traded = scan.build_slots(progs, c, NOW, p6=lambda t: True)
        self.assertTrue(all_traded)

    def test_the_poll_set_ALWAYS_includes_markets_we_hold(self):
        """The inventory-slot guarantee: a de-polled market is never requoted, never cancelled,
        and its fills are never learned."""
        slots, _, _ = self._slots()
        out = scan.poll_set(slots, always_tickers={"HELD-1", "HELD-2"}, connected=False)
        self.assertIn("HELD-1", out)
        self.assertIn("HELD-2", out)
        self.assertLessEqual(len(out), C.MAX_REST_MARKETS)

    def test_breadth_lifts_only_while_connected(self):
        slots, _, _ = self._slots()
        self.assertLessEqual(len(scan.poll_set(slots, set(), connected=False)),
                             C.MAX_REST_MARKETS)
        self.assertLessEqual(len(scan.poll_set(slots, set(), connected=True)),
                             C.MAX_WS_MARKETS)


class RunnerCase(EngineCase):
    def runner(self, ex=None, **kw):
        m = self.maker(ex=ex or ScanExchange(balance_cents=1_000_000), **kw)
        # The real loop sleeps to hold 1 Hz; the suite must not.
        return RUN.Runner(m, sleep=lambda _s: None)


class TestRunnerInit(RunnerCase):
    NESTOR = {"open_order_tickers": [], "position_tickers": []}

    def test_init_arms_and_recovers(self):
        r = self.runner()
        ok, refusals = r.init(NOW, nestor_state=self.NESTOR)
        self.assertTrue(ok, refusals)
        self.assertTrue(r.started)
        self.assertTrue(self.logs_of("recovered"))

    def test_init_refuses_and_does_NOT_recover(self):
        r = self.runner()
        ok, _ = r.init(NOW, nestor_state=None)
        self.assertFalse(ok)
        self.assertFalse(r.started)
        self.assertFalse(self.logs_of("recovered"))

    def test_RECOVERY_rebuilds_positions_from_v5s_own_ledger(self):
        r = self.runner()
        r.m.ledger.write("place_resp", order_id="1", ticker="T", side="bid", price=0.40,
                         size=10, fill_count=10, remaining_count=0)
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertAlmostEqual(r.m.positions["T"]["yes"], 10.0, places=9)
        self.assertAlmostEqual(r.m.entry_basis[("T", "yes")], 0.40, places=9)

    def test_recovery_order_replay_THEN_fills_THEN_reconcile(self):
        """Reconciling before replay would compare the exchange against an EMPTY book and
        freeze everything."""
        r = self.runner()
        r.m.ledger.write("place_resp", order_id="1", ticker="T", side="bid", price=0.40,
                         size=10, fill_count=10, remaining_count=0)
        r.m.ex._positions = [{"ticker": "T", "position": 10}]
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertNotIn("T", r.m.frozen)                     # agrees, so no freeze

    def test_triage_runs_at_init_when_adopting(self):
        r = self.runner()
        adopt = {"positions": [{"ticker": "KXUST10AD-1", "side": "yes", "net": 20.0,
                                "basis": 0.50}]}
        ok, _ = r.init(NOW, allow_fresh=True, adopt_obj=adopt,
                       exchange_positions={("KXUST10AD-1", "yes"): 20.0},
                       marks={("KXUST10AD-1", "yes"): 0.52},
                       nestor_state=self.NESTOR,
                       venues={"KXUST10AD-1": {
                           "rho": 6.25, "S": 50, "p": 0.50, "phi": 0.08, "d": 0.07,
                           "close_ts": NOW + 8 * 3600, "program_end_ts": NOW + 30 * 86400,
                           "l_shed_h": 0.5, "t_hat": 1.0, "spread_c": 2, "mark": 0.52}})
        self.assertTrue(ok)
        self.assertTrue(self.logs_of("cutover_triage_summary"))


class TestRunnerLoop(RunnerCase):
    NESTOR = {"open_order_tickers": [], "position_tickers": []}

    def test_an_iteration_runs_the_whole_chain(self):
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        out = r.iteration(NOW + 1)
        self.assertGreaterEqual(out["programs"], 1)
        self.assertGreaterEqual(out["classified"], 1)
        self.assertGreaterEqual(out["slots"], 1)
        self.assertIn("allocate", out)

    def test_B5_a_halt_STOPS_THE_WORK_not_just_the_placing(self):
        """A halted process that keeps scanning burns rate budget and writes telemetry that
        makes the halt look like a bug."""
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        calls_before = r.m.ex.program_calls
        r.m.halt.halt("day_stop", NOW)
        out = r.iteration(NOW + C.SCAN_REFRESH_S + 10)
        self.assertTrue(out["halted"])
        self.assertEqual(r.m.ex.program_calls, calls_before)  # no scan happened
        self.assertNotIn("allocate", out)

    def test_the_loop_honours_the_iteration_cap_and_always_shuts_down(self):
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        n = r.run(max_iterations=3)
        self.assertEqual(n, 3)
        self.assertTrue(self.logs_of("shutdown_complete"))

    def test_stopping_exits_the_loop(self):
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        r.m.stopping = True
        self.assertEqual(r.run(max_iterations=100), 0)
        self.assertTrue(self.logs_of("shutdown_complete"))

    def test_an_iteration_EXCEPTION_halts_and_still_shuts_down(self):
        """An exception that skipped shutdown would leave orders resting and no handback — the
        exact 'v5 dead, v4 blind' state SF-2 exists to prevent."""
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        r.iteration = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        r.run(max_iterations=1)
        self.assertTrue(r.m.halt.halted)
        self.assertEqual(r.m.halt.reason, "iteration_error")
        self.assertTrue(self.logs_of("shutdown_complete"))

    def test_shutdown_writes_the_handback_and_zeroes_the_feed(self):
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        r.m.positions["T"] = {"yes": 5.0, "no": 0.0}
        r.m.entry_basis[("T", "yes")] = 0.4
        r.run(max_iterations=1)
        obj = R.read_json(C.HANDBACK_PATH)
        self.assertEqual(obj["positions"][0]["ticker"], "T")
        self.assertEqual(R.read_json(C.CASH_FEED_PATH)["delta_dollars"], 0.0)

    def test_a_cycle_overrun_is_logged_not_silently_absorbed(self):
        ticks = iter([NOW, NOW + 5.0] + [NOW + 5.0] * 20)
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        r.clock = lambda: next(ticks)
        r._sleep = lambda s: None
        r.run(max_iterations=1)
        self.assertTrue(self.logs_of("cycle_overrun"))


class TestOnePathToTheWireAssembled(RunnerCase):
    """The property extended to the ASSEMBLED loop, not just `place()` in isolation."""

    NESTOR = {"open_order_tickers": [], "position_tickers": []}

    def test_no_write_reaches_the_exchange_except_through_place(self):
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        placed_via_place = []
        orig = r.m.place

        def spy(*a, **kw):
            res = orig(*a, **kw)
            if res[0]:
                placed_via_place.append(a[0])
            return res

        r.m.place = spy
        for i in range(5):
            r.iteration(NOW + i)
        # everything the exchange saw was placed by Maker.place
        self.assertEqual(len(r.m.ex.placed), len(placed_via_place))

    def test_a_halted_loop_places_NOTHING(self):
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        r.m.halt.halt("x", NOW)
        for i in range(5):
            r.iteration(NOW + i)
        self.assertEqual(r.m.ex.placed, [])

    def test_a_denied_series_never_reaches_the_wire_through_the_loop(self):
        ex = ScanExchange(programs=program_body(series="KXINXHUD",
                                                tickers=("KXINXHUD-26JUL28-T5000",)),
                          balance_cents=1_000_000)
        r = self.runner(ex=ex)
        r.init(NOW, nestor_state=self.NESTOR)
        for i in range(3):
            r.iteration(NOW + i)
        self.assertEqual(r.m.ex.placed, [])
        self.assertEqual(r.slots, [])

    def test_shadow_mode_runs_the_whole_loop_and_places_nothing(self):
        r = self.runner(shadow=True)
        r.init(NOW, nestor_state=self.NESTOR)
        for i in range(3):
            out = r.iteration(NOW + i)
        self.assertGreaterEqual(out["slots"], 1)              # it DID the work
        self.assertEqual(r.m.ex.placed, [])                   # and quoted nothing


if __name__ == "__main__":
    unittest.main()
