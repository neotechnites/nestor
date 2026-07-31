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
    return {"incentive_programs": [{
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
        body = {"incentive_programs": [{"id": "x", "start_date": "nonsense"}]}
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

    def test_a_scan_that_DRAINED_the_feed_reports_itself_complete(self):
        """Absence is only evidence once the map that would have carried it finished
        arriving — the precondition every pure re-derivation needs before it may read a
        missing ticker as "the world does not contain it"."""
        ex = ScanExchange()
        s = scan.Scanner()
        from .. import ratelimit as RL
        self.assertFalse(s.last_scan_complete)               # nothing asked yet
        s.scan(ex, RL.Bucket(NOW), NOW)
        self.assertTrue(s.last_scan_complete)

    def test_a_scan_still_PAGINATING_reports_itself_incomplete(self):
        ex = ScanExchange()
        body = dict(program_body())
        body["next_cursor"] = "more"
        ex.programs = lambda cursor=None: (200, body)
        s = scan.Scanner()
        from .. import ratelimit as RL
        b = RL.Bucket(NOW)
        b.tokens, b.b = 1.0, 1.0                             # one page, then the lane closes
        s.scan(ex, b, NOW)
        self.assertFalse(s.last_scan_complete)

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

    def _cache_cls(self):
        """A classifier whose cache lives in the tmpdir — DATA_DIR is patched BEFORE
        construction, because the constructor reads the cache path it derives there.  (Until
        today this function wrote through a bare open() and the suite was quietly persisting
        fixture closes into the live data dir; the write now goes through atomic_write_json,
        so base.py's write-root guard covers it and a mispointed test fails loudly.)"""
        import unittest.mock as mock
        p = mock.patch.object(C, "DATA_DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        return scan.Classifier()

    def _cached(self):
        import json as _json
        try:
            with open(self.path("v5_close_cache.json")) as fh:
                return _json.load(fh).get("close_ts") or {}
        except IOError:
            return None

    def test_the_close_cache_flushes_on_AGE_not_only_on_a_count(self):
        """A COUNTER THAT RESETS ON RESTART IS NOT A FLUSH POLICY.  `_close_dirty` starts at
        zero in every process, so at today's restart cadence a run rarely reaches 25 NEW
        closes before it is replaced — the counter kept being thrown away one short of a
        write and the next process booted on a stale cache that both the D4 settlement gate
        and the slot builder read."""
        c = self._cache_cls()
        c.close_ts["A"] = NOW + 3600
        c._persist_closes(now=NOW)                 # first close of the process: written at once
        self.assertEqual(sorted(self._cached()), ["A"])
        c.close_ts["B"] = NOW + 3600
        c._persist_closes(now=NOW + 1)             # 1 of 25, and the clock has barely moved
        self.assertEqual(sorted(self._cached()), ["A"])
        c.close_ts["C"] = NOW + 3600
        c._persist_closes(now=NOW + C.BOOK_SNAPSHOT_S + 1)      # aged out: flushed
        self.assertEqual(sorted(self._cached()), ["A", "B", "C"])

    def test_the_age_bound_is_DERIVED_from_the_snapshot_cadence(self):
        """No new arbitrary constant: BOOK_SNAPSHOT_S is the interval at which this program
        already decided restart-critical state must be on disk, and a cache of world-facts is
        the same class of state."""
        import inspect
        self.assertIn("max_age_s=C.BOOK_SNAPSHOT_S",
                      inspect.getsource(scan.Classifier._persist_closes))

    def test_the_first_full_sweep_flushes_what_it_learned(self):
        """The sweep that learns the most closes is the first one, and it is the likeliest to
        be the one a restart interrupts."""
        from .. import ratelimit as RL
        ex = ScanExchange()
        c = self._cache_cls()
        c.sweep(ex, RL.Bucket(NOW), scan.parse_programs(ex._programs), NOW)
        self.assertTrue(self._cached(), "the first sweep left nothing on disk")
        self.assertIn("KXAAAGASD-26JUL29-T4.12", self._cached())

    def test_a_sweep_that_ran_OUT_OF_BUDGET_is_not_the_first_full_sweep(self):
        """It is 'the first FULL sweep': a pass that broke early has not finished learning,
        so it does not consume the one free flush."""
        from .. import ratelimit as RL
        ex = ScanExchange()
        c = self._cache_cls()
        b = RL.Bucket(NOW)
        b.tokens, b.b = 0.0, 0.0
        c.sweep(ex, b, scan.parse_programs(ex._programs), NOW)
        self.assertFalse(c._first_sweep_flushed)

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

    def test_runway_targets_the_CLIFF_when_accrual_is_at_stake(self):
        """Second amendment (b): with 70¢ accrued the reachability target is $1.10 (the
        forfeit cliff), not $2.00 — the runway guard must not confiscate the very accrual
        the rescue exists to recover.  Same window, zero accrual: still excluded."""
        self.assertTrue(scan.runway_ok(0.5, 4.0, accrued_usd=0.70))   # need $0.40 ≤ $0.50
        self.assertFalse(scan.runway_ok(0.5, 4.0, accrued_usd=0.0))   # need $2.00 > $0.50

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

    def test_an_unqualified_side_is_PRICED_and_ranked_unaffordable(self):
        """REWRITTEN under the owner's law §7a (2026-07-30; was ..._REFUSED_not_funded under
        FREE_RIDE_ONLY, and before that ..._gets_the_qualification_size at 1c).

        The law's contract: a side short of target WITHOUT us is neither funded at 1c (the
        -100% cohort's geometry) nor refused by a gate — it is PRICED.  The slot carries its
        self-qualifying walk at the band floor, and the allocator skips it as unaffordable
        with the arithmetic in the log ($10/market cannot buy a 990-contract walk)."""
        from .. import alloc, ratelimit as RL

        class Thin(ScanExchange):
            def book(self, ticker):
                return 200, book(yes=(("0.40", "10"),), no=(("0.58", "1200"),))

        ex = Thin()
        progs = scan.parse_programs(ex._programs)
        c = scan.Classifier()
        c.sweep(ex, RL.Bucket(NOW), progs, NOW)
        slots = scan.build_slots(progs, c, NOW)
        bids = [s for s in slots if s.side == "bid"]
        self.assertTrue(bids, "the law prices the thin side; it does not delete it")
        self.assertEqual(bids[0].land_grab_size, 990)     # the walk gap, carried
        own_axis = bids[0].land_grab_price_c
        self.assertEqual(own_axis, C.ENTRY_BAND_LO_C, "never 1c again")
        a2, spent, rep = alloc.allocate_law(bids, 300.0)
        self.assertEqual(spent, 0.0)
        self.assertEqual(rep["reasons"].get("unaffordable"), 1)
        # and the ask side, which DOES clear target on rival size alone, rides free
        asks = [s for s in slots if s.side == "ask"]
        self.assertTrue(asks)
        self.assertEqual(asks[0].land_grab_size, 0, "free-riding costs nothing")

    def test_P6_the_pre_entry_filter_seam_exists_and_its_ABSENCE_is_logged(self):
        """note 43 §5's mirror, under the owner's law §7 (2026-07-30): p6 informs PHI ONLY,
        never refuses.  The seam still exists (the absence of a tape source is logged once),
        and a supplied p6 OBSERVES — `p6_quiet` — with no refusal end left to toggle."""
        scan._P6_WARNED = False
        slots, progs, c = self._slots()
        self.assertTrue(self.logs_of("p6_pre_entry_filter_UNWIRED"))
        none_traded = scan.build_slots(progs, c, NOW, p6=lambda t: False)
        self.assertTrue(none_traded, "p6 must not delete the quiet long tail")
        self.assertTrue(self.logs_of("p6_quiet"))
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
        # v5's OWN dialect: a fill is a fill_obs row.  engine.place records `fill_count` and
        # books nothing from it — the immediate cross is learned by the fills poll — so the
        # position on a v5 tape is the SUM OF THE ROWS, never v4's order-response inference.
        r.m.ledger.write("place_resp", order_id="1", ticker="T", side="bid", price=0.40,
                         size=10, fill_count=10, remaining_count=0)
        r.m.ledger.write("fill_obs", order_id="1", ticker="T", side="bid", count=10,
                         price_c=40, fill_id="f1")
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertAlmostEqual(r.m.positions["T"]["yes"], 10.0, places=9)
        self.assertAlmostEqual(r.m.entry_basis[("T", "yes")], 0.40, places=9)

    def test_recovery_order_replay_THEN_fills_THEN_reconcile(self):
        """Reconciling before replay would compare the exchange against an EMPTY book and
        freeze everything."""
        r = self.runner()
        r.m.ledger.write("place_resp", order_id="1", ticker="T", side="bid", price=0.40,
                         size=10, fill_count=10, remaining_count=0)
        r.m.ledger.write("fill_obs", order_id="1", ticker="T", side="bid", count=10,
                         price_c=40, fill_id="f1")
        r.m.ex._positions = [{"ticker": "T", "position": 10}]
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertNotIn("T", r.m.frozen)                     # agrees, so no freeze

    def test_adopting_with_venues_triages_NOTHING_into_an_order(self):
        """LAW CHANGE (owner decision, 2026-07-30).  This was
        `test_triage_runs_at_init_when_adopting`, and it asserted `cutover_triage_summary` on
        the log — the summary of an init pass that fed MAKER_SHED verdicts into
        `m.triage_shed` so the requoter could post cap-exempt closing orders.  That block and
        its `C.CUTOVER_TRIAGE_ENABLED` gate (patched True here) are deleted.  Init still
        ADOPTS positions; it no longer judges them into an exit."""
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
        self.assertFalse(self.logs_of("cutover_triage_summary"))
        self.assertAlmostEqual(r.m.positions["KXUST10AD-1"]["yes"], 20.0)  # adopted, riding
        self.assertEqual(r.m.ex.placed, [])                               # and untouched


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


class CheapScanExchange(ScanExchange):
    """A GOOD venue (the gas geometry): (★) admits it, so the assembled loop actually
    quotes — which is what makes the one-path test non-vacuous."""

    def book(self, ticker):
        self.book_calls += 1
        return 200, book(yes=(("0.06", "1200"),), no=(("0.93", "1200"),))


class TestOnePathToTheWireAssembled(RunnerCase):
    """The property extended to the ASSEMBLED loop, not just `place()` in isolation."""

    NESTOR = {"open_order_tickers": [], "position_tickers": []}

    def test_no_write_reaches_the_exchange_except_through_place(self):
        """Charter D: the vacuous form of this test (0 == 0 on a book nobody quoted) is
        exactly how the missing requoter passed review.  It now runs against a venue the
        allocator ADMITS and asserts the exchange saw REAL orders — all of them via place."""
        r = self.runner(ex=CheapScanExchange(balance_cents=1_000_000))
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
            r.iteration(NOW + 1 + i)
        # the system is ALIVE...
        self.assertGreater(len(r.m.ex.placed), 0, "the assembled loop placed NOTHING")
        # ...and everything the exchange saw was placed by Maker.place
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


class TestStartupAdoptsNoOrders(RunnerCase):
    """LAW CHANGE (owner decision, 2026-07-30: "it's either running and placing orders, or
    it's not running").  **STARTUP IS IDENTICAL TO STEADY STATE: THE ORDER BOOK STARTS EMPTY.**

    THIS CLASS WAS `TestRecoveryOrders`, and its docstring read: "BLOCKER-1 — resting orders
    survive a restart: replay rebuilds them, the cash feed counts their collateral, and the
    §9.4 step-4 prefix sweep reconciles both directions."  Every one of those behaviours is
    now forbidden, and the tests below are the same fixtures with the assertions inverted.

    WHY.  The 2026-07-30 halted closing pass put GTC closing orders on the wire, sized from
    books the halt had already declared wrong.  They survived EVERY restart, because the sweep
    adopted whatever wore our prefix as legitimately ours and the requoter then reasoned from
    it.  An adopted order is a decision this process never made, admitted without any of the
    rails that would have refused making it — the same defect as the deleted `reinstate`, one
    layer down.  And the account is SHARED: nestor and other systems place orders here, so
    "not ours" is the correct reading of everything resting at startup.

    Note the deliberate asymmetry: startup neither adopts NOR cancels what it finds.  Those
    orders are not this process's concern.

    ALSO DELETED HERE: `TestRecoveryOrders`'s rebuild-selection cases
    (`..._a_cancelled_or_expired_order_is_not_rebuilt`, `..._past_its_expiration_backstop_...`,
    `..._fill_obs_rows_shrink_the_rebuilt_remaining`) and the whole
    `TestTheSweepSpeaksTheWiresDialect` class, which asserted that the SWEEP parsed the wire's
    `*_fp` / `*_dollars` / `book_side` dialect correctly.  All of them described how to adopt
    an order well; none has a subject any more.  (`book_fill_row` still owns that dialect for
    FILLS, and `test_the_fake_emits_no_invented_price_key`'s contract survives there.)
    """

    NESTOR = {"open_order_tickers": [], "position_tickers": []}

    def test_replay_does_NOT_rebuild_a_live_order(self):
        """Was `test_replay_rebuilds_a_live_order_and_counts_its_collateral`."""
        r = self.runner()
        r.m.ledger.write("place_resp", order_id="o1", coid="v5-lipm-T-y-1", ticker="T",
                         side="bid", price=0.40, size=10, fill_count=0, remaining_count=10,
                         expiration_ts=int(NOW + 3600))
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertEqual(r.m.orders, {}, "a previous process's order entered our book")
        self.assertAlmostEqual(r.m.cash.resting_collateral, 0.0, places=9)

    def test_the_prefix_sweep_is_gone_the_wire_is_left_alone(self):
        """Was `test_the_prefix_sweep_finds_exchange_orders_replay_never_saw`.  An order
        wearing our own old prefix is treated exactly like nestor's: not adopted, not
        cancelled, not handed to B10 — not ours."""
        r = self.runner()
        r.m.ex.resting["x9"] = {"ticker": "T", "side": "bid", "count": 7,
                                "price": "0.5000", "client_order_id": "v5-lipm-T-y-99"}
        r.m.ex.resting["theirs"] = {"ticker": "T", "side": "bid", "count": 3,
                                    "price": "0.5000", "client_order_id": "v4-lipm-T-y-1"}
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertEqual(r.m.orders, {})
        self.assertEqual(dict(r.m.unknown.pending), {})
        self.assertAlmostEqual(r.m.cash.resting_collateral, 0.0, places=9)
        self.assertFalse(self.logs_of("recovery_unknown_order"))
        self.assertEqual(r.m.ex.cancelled, [], "startup cancelled an inherited order")
        self.assertEqual(sorted(r.m.ex.resting), ["theirs", "x9"])   # both still resting

    def test_a_replayed_order_the_exchange_does_not_show_is_a_NON_EVENT(self):
        """Was `test_a_replay_live_order_the_exchange_no_longer_shows_goes_to_UNKNOWN`.  With
        no replayed order book there is nothing to diff against the wire, so there is no
        UNKNOWN to open."""
        r = self.runner()
        r.m.ledger.write("place_resp", order_id="gone", coid="v5-lipm-T-y-1", ticker="T",
                         side="bid", price=0.40, size=10, remaining_count=10,
                         expiration_ts=int(NOW + 3600))
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertEqual(dict(r.m.unknown.pending), {})
        self.assertFalse(self.logs_of("recovery_order_gone"))

    def test_the_world_is_still_recovered(self):
        """The half that MUST survive: money truth.  A restart still rebuilds positions,
        basis and cost from the fill tape — only the ORDER book is refused."""
        r = self.runner()
        r.m.ledger.write("fill_obs", order_id="o1", ticker="T", side="bid", count=10,
                         price_c=40, fill_id="f1", closing=False)
        r.m.ex._positions = [{"ticker": "T", "position": 10}]
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertAlmostEqual(r.m.positions["T"]["yes"], 10.0, places=9)
        self.assertAlmostEqual(r.m.entry_basis[("T", "yes")], 0.40, places=9)
        self.assertAlmostEqual(r.m.position_cost["T"], 4.0, places=9)
        self.assertIn("f1", r.m.dedupe.seen)              # crash-gap dedupe still seeded
        self.assertEqual(r.m.orders, {})

    def test_the_adoption_machinery_itself_is_gone(self):
        """Structural, so a later edit cannot half-revive it."""
        self.assertFalse(hasattr(RUN.Runner, "recover_orders"))
        self.assertFalse(hasattr(RUN.Runner, "parse_order_row"))


class TestCancelAllIsScopedToOurOwnOrders(RunnerCase):
    """**THE SHARED-ACCOUNT LAW.** (Owner decision, 2026-07-30: "it's either running and
    placing orders, or it's not running.")  nestor and other systems trade this same Kalshi
    account.  Every cancel this program issues must name an order THIS PROCESS placed —
    identified by membership of `self.orders`, which since the startup adoption sweep was
    deleted contains exactly that and nothing else.  There is no account-wide cancel endpoint
    call anywhere in this binary, and there must never be one: an account-wide sweep would
    silently flatten another system's book, and the halt is exactly the moment we are least
    entitled to act on a wide reading of the world.

    WHY THIS CLASS EXISTS.  `flatten()` is correct and always was — it iterates `self.orders`
    — but correctness with no test is a property that survives only until the next edit.  The
    scope evidence that existed covered STARTUP only (`test_the_prefix_sweep_is_gone_...`
    asserts `ex.cancelled == []` at init, which is before any flatten runs), so an
    account-wide sweep appended to `flatten` shipped through a fully green suite.  These tests
    close that: they exercise the two paths that actually call `flatten` — the HALT and
    SHUTDOWN — with foreign orders resting on the wire.

    TWO KINDS OF FOREIGN ORDER, deliberately, because they fail differently:
      * `nestors-1` wears someone else's coid.  A prefix check catches it.
      * `ours-but-stale-1` wears OUR OWN prefix but was placed by a previous process, so it is
        NOT in `self.orders`.  A prefix check does NOT catch it — only membership does — and
        it is the one the deleted recovery sweep used to adopt.  It is also not ours to
        cancel: startup neither adopts NOR cancels what it finds.
    """

    NESTOR = {"open_order_tickers": [], "position_tickers": []}
    FOREIGN = {"nestors-1": {"ticker": "T", "side": "bid", "count": 5, "price": "0.5000",
                             "client_order_id": "nestor-abc-1"},
               "ours-but-stale-1": {"ticker": "T", "side": "bid", "count": 9,
                                    "price": "0.5000",
                                    "client_order_id": "v5-lipm-T-y-77"}}

    def _armed_with_one_of_ours_and_two_foreign(self):
        """One order WE placed this process, plus the two foreign ones, all resting."""
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        ok, why, _ = r.m.place("T", "bid", 0.40, 10, int(NOW + 3600), NOW,
                               available_cash_usd=100_000.0)
        self.assertTrue(ok, why)
        ours = sorted(r.m.orders)[0]
        for oid, body in self.FOREIGN.items():
            r.m.ex.resting[oid] = dict(body)
        return r, ours

    def _assert_foreign_untouched(self, r):
        for oid in self.FOREIGN:
            self.assertIn(oid, r.m.ex.resting,
                          "%s was cancelled off a SHARED account" % oid)
            self.assertNotIn(oid, r.m.ex.cancelled,
                             "we sent a cancel for %s, which is not ours" % oid)
            self.assertNotIn(oid, r.m.orders)

    def test_the_HALT_cancels_ours_and_leaves_both_foreign_orders_resting(self):
        r, ours = self._armed_with_one_of_ours_and_two_foreign()
        r.m.halt.halt("books_integrity", NOW + 1)
        for i in range(4):
            r.iteration(NOW + 2 + i * 30.0)
        self.assertIn(ours, r.m.ex.cancelled, "the halt did not cancel our own order")
        self.assertNotIn(ours, r.m.ex.resting)
        self._assert_foreign_untouched(r)
        # ...and the halt placed nothing, pass after pass (clause 2's other half).
        self.assertEqual(len(r.m.ex.placed), 1)               # only the pre-halt quote

    def test_SHUTDOWN_cancels_ours_and_leaves_both_foreign_orders_resting(self):
        r, ours = self._armed_with_one_of_ours_and_two_foreign()
        r.shutdown(NOW + 5, reason="sigterm")
        self.assertIn(ours, r.m.ex.cancelled, "shutdown did not cancel our own order")
        self.assertNotIn(ours, r.m.ex.resting)
        self._assert_foreign_untouched(r)

    def test_flatten_itself_names_only_ids_in_our_book(self):
        """The unit statement under the two integration ones: whatever `flatten` sends, the
        set of ids is a SUBSET of `self.orders` as it stood on entry.  A future account-wide
        sweep appended anywhere in the call fails here even if it is added below the loop."""
        r, ours = self._armed_with_one_of_ours_and_two_foreign()
        before = set(r.m.orders)
        r.m.flatten(NOW + 5)
        self.assertTrue(set(r.m.ex.cancelled) <= before,
                        "flatten cancelled ids it never placed: %s"
                        % (set(r.m.ex.cancelled) - before))
        self.assertEqual(set(r.m.ex.cancelled), {ours})

    def test_the_day_stop_flatten_is_scoped_the_same_way(self):
        """The third caller of `flatten` — `cycle()`'s day-stop branch — is the one that runs
        while the loop is otherwise healthy, so it gets its own statement rather than
        inheriting the halt's."""
        r, ours = self._armed_with_one_of_ours_and_two_foreign()
        r.m.position_cost["T"] = 1000.0                       # crush the mark: day stop trips
        out = r.iteration(NOW + 2)
        self.assertTrue(out.get("day_stop"))
        self.assertIn(ours, r.m.ex.cancelled)
        self._assert_foreign_untouched(r)


class TestSyncOrdersReconcilesOneDirectionOnly(RunnerCase):
    """`sync_orders` runs on the reconcile cadence and its MEANING changed with the law
    (2026-07-30): it may drop from our books what the wire says is gone, and it may NEVER
    import a wire order this process did not place.  That is the runtime counterpart of the
    deleted startup sweep — without this end, adoption simply returns 120 seconds later."""

    NESTOR = {"open_order_tickers": [], "position_tickers": []}

    def test_a_foreign_or_inherited_wire_order_is_never_imported(self):
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        r.m.ex.resting["x9"] = {"ticker": "T", "side": "bid", "count": 7, "price": "0.5000",
                                "client_order_id": "v5-lipm-T-y-99"}   # our OWN old prefix
        r.m.last_orders_sync = 0.0
        r.m.sync_orders(NOW + 10_000.0)
        self.assertEqual(r.m.orders, {})
        self.assertAlmostEqual(r.m.cash.resting_collateral, 0.0, places=9)

    def test_an_order_WE_placed_that_vanished_from_the_wire_is_still_dropped(self):
        """The direction that survives: our own order gone from the exchange's complete list
        goes through §9.4a disambiguation rather than resting in our books as phantom
        presence (the convergence finding — the requoter declines to re-place forever)."""
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        ok, why, _ = r.m.place("T", "bid", 0.40, 10, int(NOW + 3600), NOW,
                               available_cash_usd=100_000.0)
        self.assertTrue(ok, why)
        oid = sorted(r.m.orders)[0]
        r.m.ex.resting.clear()                            # a hand flatten, exchange-side
        r.m.last_orders_sync = 0.0
        r.m.sync_orders(NOW + 10_000.0)
        self.assertTrue(self.logs_of("order_gone_from_wire"))
        self.assertTrue(r.m.orders[oid].get("gone_404") or oid not in r.m.orders)


class TestCrashGapDoesNotReBookTheTape(RunnerCase):
    """B8's dedupe is IN-MEMORY and reborn empty every process, while the crash-gap window is
    overlapping BY CONSTRUCTION — so recovery re-booked fills the dying process had already
    written.  Three damages, all unsafe: the position doubles, the second booking drives the
    order's remaining to 0 so book_fill POPS an order that is still resting (uncancellable
    thereafter), and the pop releases collateral the exchange is still holding, publishing
    delta_dollars ABOVE truth.  recover() seeds the dedupe from the replayed tape."""

    NESTOR = {"open_order_tickers": [], "position_tickers": []}

    def tape(self, r):
        r.m.ledger.write("place_resp", order_id="o1", coid="v5-lipm-T-y-1", ticker="T",
                         side="bid", price=0.40, size=10, remaining_count=10,
                         expiration_ts=int(NOW + 3600), fully_closing=False)
        r.m.ledger.write("fill_obs", order_id="o1", ticker="T", side="bid", count=6,
                         price_c=40, fill_id="t-1", closing=False)
        # The SAME fill still sitting in the exchange's index inside the lookback window.
        r.m.ex.fills_rows = [{"trade_id": "t-1", "fill_id": "t-1", "order_id": "o1",
                              "ticker": "T", "market_ticker": "T", "book_side": "bid",
                              "side": "yes", "action": "buy", "count_fp": "6.00",
                              "yes_price_dollars": "0.4000", "no_price_dollars": "0.6000",
                              "fee_cost": "0.000000", "is_taker": False,
                              "created_time": NOW, "ts": int(NOW)}]
        r.m.ex.resting["o1"] = {"ticker": "T", "side": "bid", "count": 4, "price": "0.4000",
                                "client_order_id": "v5-lipm-T-y-1"}
        r.m.ex._positions = [{"ticker": "T", "position": 6}]

    def test_the_replayed_fill_is_not_booked_a_second_time(self):
        r = self.runner()
        self.tape(r)
        r.init(NOW + 30, nestor_state=self.NESTOR)
        self.assertAlmostEqual(r.m.positions["T"]["yes"], 6.0, places=9)   # not 12
        self.assertTrue(self.logs_of("dedupe_seeded"))
        # Asserted on the TAPE as well as on state: reconcile's exchange-truth resync would
        # quietly repair a doubled position at the next cycle, so state alone cannot prove
        # the re-book did not happen — a second fill_obs row for the same fill_id can.
        obs = [x for x in r.m.ledger.read()
               if (x.get("k") or x.get("kind")) == "fill_obs" and x.get("fill_id") == "t-1"]
        self.assertEqual(len(obs), 1)

    def test_the_still_resting_order_is_NOT_adopted_and_the_dedupe_still_holds(self):
        """LAW CHANGE (2026-07-30).  Was
        `test_the_still_resting_order_survives_recovery_with_its_collateral`, asserting the
        pre-crash order came back into `self.orders` with its 4 × $0.40 of collateral counted.
        Startup adopts no orders now, so that assertion inverts — and the FINDING this class
        actually owns, that the crash-gap window must not re-book the tape's fills, is
        unaffected and is re-asserted here on the same fixture.

        THE ACKNOWLEDGED COST, flagged rather than papered over: that resting order does hold
        $1.60 of real exchange collateral which `resting_collateral` no longer counts, so
        published expected-cash sits above the free dollars until `reconcile` reads the
        exchange's own balance.  The alternative is adoption, which is what put the 2026-07-30
        closing orders back on the wire after every restart."""
        r = self.runner()
        self.tape(r)
        r.init(NOW + 30, nestor_state=self.NESTOR)
        self.assertEqual(r.m.orders, {})
        self.assertAlmostEqual(r.m.cash.resting_collateral, 0.0, places=9)
        self.assertAlmostEqual(r.m.positions["T"]["yes"], 6.0, places=9)   # not 12


class TestAdoptionIdempotent(RunnerCase):
    """BLOCKER-2 — adoption is a money event: it writes `adopt` rows, replay rebuilds them,
    and a re-supplied adopt file is SKIPPED, so `position_cost` can never double."""

    NESTOR = {"open_order_tickers": [], "position_tickers": []}
    TK = "KXUST10AD-1"
    ADOPT = {"positions": [{"ticker": TK, "side": "yes", "net": 20.0, "basis": 0.50}]}
    EXPO = {(TK, "yes"): 20.0}

    def test_restart_with_the_same_adopt_file_does_not_double(self):
        r = self.runner()
        ok, refusals = r.init(NOW, allow_fresh=True, adopt_obj=self.ADOPT,
                              exchange_positions=self.EXPO,
                              marks={(self.TK, "yes"): 0.52}, nestor_state=self.NESTOR)
        self.assertTrue(ok, refusals)
        cost1 = r.m.position_cost[self.TK]
        self.assertAlmostEqual(cost1, 10.0, places=9)
        # restart, SAME adopt file supplied again (the crash-loop case)
        r2 = self.runner()
        ok, refusals = r2.init(NOW + 60, adopt_obj=self.ADOPT,
                               exchange_positions=self.EXPO,
                               marks={(self.TK, "yes"): 0.52}, nestor_state=self.NESTOR)
        self.assertTrue(ok, refusals)
        self.assertTrue(self.logs_of("adopt_skipped_already_adopted"))
        self.assertAlmostEqual(r2.m.position_cost[self.TK], cost1, places=9)
        self.assertAlmostEqual(r2.m.positions[self.TK]["yes"], 20.0, places=9)

    def test_restart_WITHOUT_the_adopt_file_keeps_the_position(self):
        """The other half of writing adopt rows: the position survives on replay alone."""
        r = self.runner()
        r.init(NOW, allow_fresh=True, adopt_obj=self.ADOPT, exchange_positions=self.EXPO,
               marks={(self.TK, "yes"): 0.52}, nestor_state=self.NESTOR)
        r2 = self.runner()
        r2.init(NOW + 60, nestor_state=self.NESTOR)
        self.assertAlmostEqual(r2.m.positions[self.TK]["yes"], 20.0, places=9)
        self.assertAlmostEqual(r2.m.entry_basis[(self.TK, "yes")], 0.50, places=9)
        self.assertAlmostEqual(r2.m.cash.inventory_basis, 10.0, places=9)

    def test_an_adoption_freeze_survives_restart(self):
        r = self.runner()
        r.init(NOW, allow_fresh=True,
               adopt_obj={"positions": [{"ticker": self.TK, "side": "yes", "net": 20.0,
                                         "basis": 0.50}]},
               exchange_positions={(self.TK, "yes"): 7.0},     # net DISAGREES ⇒ frozen
               marks={}, nestor_state=self.NESTOR)
        self.assertIn(self.TK, r.m.frozen)
        r2 = self.runner()
        r2.init(NOW + 60, nestor_state=self.NESTOR)
        self.assertIn(self.TK, r2.m.frozen)


class TestHaltedIdle(RunnerCase):
    """SF-3 — every halt path flattens once; the halted loop idles slow, keeps the
    heartbeat, and never spins or crash-loops."""

    NESTOR = {"open_order_tickers": [], "position_tickers": []}

    def test_a_halt_flattens_ONCE_and_only_once(self):
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        r.m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW,
                  available_cash_usd=1000.0)
        r.m.halt.halt("iteration_error", NOW)
        out = r.iteration(NOW + 1)
        self.assertTrue(out["halted"])
        self.assertEqual(r.m.orders, {})                  # flattened
        cancels = len(r.m.ex.cancelled)
        r.iteration(NOW + 2)
        self.assertEqual(len(r.m.ex.cancelled), cancels)  # once, not per iteration

    def test_the_halted_loop_keeps_the_cash_feed_heartbeat(self):
        """A halted-but-alive v5 still holds inventory; a stale feed would page nestor's
        operator about a process that is fine."""
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        r.m.halt.halt("day_stop", NOW)
        r.m.publisher.publish(NOW)
        r.iteration(NOW + C.CASH_FEED_HEARTBEAT_S + 1)
        self.assertGreater(r.m.publisher.last_publish_ts, NOW)

    def test_the_halted_loop_sleeps_the_idle_cadence(self):
        naps = []
        r = self.runner()
        r._sleep = naps.append
        r.init(NOW, nestor_state=self.NESTOR)
        r.m.halt.halt("day_stop", NOW)
        r.run(max_iterations=2)
        self.assertEqual(len(naps), 2)
        for n in naps:
            self.assertGreater(n, C.HALTED_IDLE_S - 5.0)  # ~30 s, not the 1 s live cadence

    def test_an_exception_inside_the_halted_branch_does_not_crash_the_loop(self):
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        r.m.halt.halt("day_stop", NOW)
        r.m.flatten = lambda now: (_ for _ in ()).throw(RuntimeError("boom"))
        out = r.iteration(NOW + 1)                        # must not raise
        self.assertTrue(out["halted"])
        self.assertTrue(self.logs_of("halted_idle_error"))


class TestPlumbingWakes(RunnerCase):
    """Charter B: the wiring that wakes dormant guards, proven through the ASSEMBLED loop."""

    NESTOR = {"open_order_tickers": [], "position_tickers": []}
    TICKER = "KXAAAGASD-26JUL29-T4.12"

    def test_B2_the_day_stop_sees_mids_from_the_classifier(self):
        """The day stop was dormant because nothing fed it `yes_mids`.  Now a marked loss on
        classified books trips it through the loop, with no caller-supplied prices."""
        r = self.runner()
        r.init(NOW, nestor_state=self.NESTOR)
        r.m.positions[self.TICKER] = {"yes": 200.0, "no": 0.0}
        r.m.position_cost[self.TICKER] = 150.0            # marked ~0.41 ⇒ ~$68 of loss —
                                                          # past the ceiling-scaled floor
                                                          # ($60 at $300), which the breach
                                                          # check now honors
        out = r.iteration(NOW + 1)
        self.assertTrue(out.get("day_stop"))
        self.assertTrue(r.m.halt.halted)

    def test_the_slot_carries_the_MARKET_close_not_the_program_end(self):
        ex = ScanExchange(balance_cents=1_000_000)
        # settles 5 DAYS past its 16h program: inside SETTLE_HORIZON_H, so it is admitted —
        # and the slot must carry the MARKET close so (★)'s carry term prices the 5-day gap
        ex.market_closes[self.TICKER] = NOW + 120 * 3600
        r = self.runner(ex=ex)
        r.init(NOW, nestor_state=self.NESTOR)
        r.iteration(NOW + 1)
        s = [s for s in r.slots if s.side == "bid"][0]
        self.assertAlmostEqual(s.close_ts, NOW + 120 * 3600, places=3)
        self.assertAlmostEqual(s.program_end_ts, NOW + 16 * 3600, places=3)
        self.assertGreater(s.l_eff, 100.0)                # carry runs to the REAL horizon

    def test_the_horizon_exclusion_wakes_on_the_real_close(self):
        """The PYPL geometry (T_settle ≫ program end) is refused at the SETTLEMENT GATE
        now (note 52 D4) — before a slot even exists, which is one stage earlier than the
        old ALLOCATE exclusion and strictly cheaper."""
        ex = ScanExchange(balance_cents=1_000_000)
        ex.market_closes[self.TICKER] = NOW + 90 * 86400
        r = self.runner(ex=ex)
        r.init(NOW, nestor_state=self.NESTOR)
        out = r.iteration(NOW + 1)
        self.assertEqual([s for s in r.slots if s.ticker == self.TICKER], [])
        self.assertTrue(self.logs_of("settle_horizon_refused"))
        for key, q in (out.get("alloc") or {}).items():
            self.assertEqual(q, 0, key)

    def test_P6_a_market_nobody_trades_IS_a_slot_and_is_recorded(self):
        """VERIFIED against Kalshi's documentation 2026-07-28: liquidity rewards are paid for
        maintaining resting orders "even if your orders don't get filled" — scoring samples the
        BOOK, never the tape.  So silence is not evidence of a worthless venue; it is evidence
        of an uncontested one.  Measured at G2: with P6 refusing, 200 classified markets made
        ZERO slots and v5 quoted nothing.  The observation is still recorded so the first
        payout settles it with evidence."""
        ex = ScanExchange(balance_cents=1_000_000)
        ex.trades_rows = []                               # 5 days of public tape: silence
        r = self.runner(ex=ex)
        r.init(NOW, nestor_state=self.NESTOR)
        out = r.iteration(NOW + 1)
        self.assertTrue(out["slots"], "the quiet long tail must remain quotable")
        self.assertTrue(self.logs_of("p6_quiet"))

    def test_P6_a_traded_market_is_admitted(self):
        r = self.runner()                                 # default fake: one recent trade
        r.init(NOW, nestor_state=self.NESTOR)
        out = r.iteration(NOW + 1)
        self.assertGreater(out["slots"], 0)

    def test_NO_venue_permission_binds_in_the_assembled_loop(self):
        """REWRITTEN 2026-07-30 (stage 1) — was `test_venue_caps_bind_in_the_assembled_loop`,
        which asserted §1.4's rung-0 cap bound the spend.  There is no rung, no probe and no
        admission: a venue we have never touched competes on its numbers in the first cycle,
        and what bounds the spend is the DOLLAR stack (cluster reserve, lot container,
        ceiling).  A book that had to be granted permission could not be a pure function of
        the world — it would also be a function of which venues we happened to probe first."""
        r = self.runner(ex=CheapScanExchange(balance_cents=1_000_000))
        r.init(NOW, nestor_state=self.NESTOR)
        out = r.iteration(NOW + 1)
        self.assertFalse(hasattr(r.m, "venues"))
        spent = out["allocate"]["spent"]
        self.assertGreater(spent, 0.0, "a fresh venue must be fundable on sight")
        self.assertLessEqual(spent, r.m.ceiling_usd + 1e-6)


if __name__ == "__main__":
    unittest.main()


class TestClassifyDiscoversBreadth(LipTestCase):
    """Capital is capped PER CLUSTER, so a new cluster is worth a whole fresh cap while the
    eleventh rung of a full cluster is worth nothing.  Ranking classify candidates by rho
    alone loaded the entire budget onto treasury (five tenors, ONE cluster, ONE $75 cap) and
    never discovered a second underlying."""

    def _programs(self):
        now = 1785268000.0
        mk = lambda pid, tks, rho: {"program_id": pid, "series": tks[0].split("-")[0],
                                    "tickers": list(tks), "period_reward": 1000000,
                                    "start_ts": now - 3600, "end_ts": now + 36000,
                                    "window_h": 11.0, "rho": rho, "target_size": 1000.0,
                                    "paid_out": False}
        # one fat cluster with many rungs, two thinner but DISTINCT clusters
        return [mk("p1", ["KXUST10AD-26JUL29-T%d" % i for i in range(10)], 9.0),
                mk("p2", ["KXAAAGASD-26JUL29-4.1"], 5.0),
                mk("p3", ["KXTRUEV-26JUL28-T1"], 4.0)], now

    def test_every_cluster_is_reached_before_one_is_exhausted(self):
        progs, now = self._programs()
        c = scan.Classifier(max_markets=5)
        picked = [tk for _, tk, _, _ in c.candidates(progs, now)]
        clusters = {t.split("-")[0] for t in picked}
        self.assertIn("KXAAAGASD", clusters, "a distinct cluster must be discovered early")
        self.assertIn("KXTRUEV", clusters, "and so must the third")
        self.assertEqual(len(picked), 5)

    def test_the_strongest_cluster_still_leads(self):
        progs, now = self._programs()
        c = scan.Classifier(max_markets=5)
        picked = [tk for _, tk, _, _ in c.candidates(progs, now)]
        self.assertTrue(picked[0].startswith("KXUST10AD"),
                        "within the rounds, the richest cluster still goes first")

    def test_ACCRUAL_ranks_the_read_set(self):
        """Ryan, 2026-07-30 night: gas 4.095 held $0.63 banked (need $0.37, the cheapest
        finish in its cluster) and NEVER GOT A BOOK READ — this ranking saw two identical
        rhos and broke the tie by feed order toward the $1.00-need sibling.  The estimates
        feed (SF-4c) knows per-program accrual request-free; a market carrying banked
        credit must always make the read set, first in its cluster."""
        now = 1785268000.0
        mk = lambda pid, tk: {"program_id": pid, "series": tk.split("-")[0],
                              "tickers": [tk], "period_reward": 1000000,
                              "start_ts": now - 3600, "end_ts": now + 36000,
                              "window_h": 11.0, "rho": 5.0, "target_size": 1000.0,
                              "paid_out": False}
        # identical rho, identical window, same cluster: only accrual separates them —
        # and the plain-ticker tie-break would put 4.095 SECOND ("4.100" < "4.095" is
        # False: lexicographic order favors 4.095... so pin the tie-break the hard way:
        # give the accrued market the LOSING ticker string).
        progs = [mk("pa", "KXAAAGASD-26JUL31-4.095"), mk("pb", "KXAAAGASD-26JUL31-4.100")]
        c = scan.Classifier(max_markets=2)
        # without accrual: lexicographic tie-break, 4.095 first (control)
        plain = [tk for _, tk, _, _ in c.candidates(progs, now)]
        self.assertEqual(plain[0], "KXAAAGASD-26JUL31-4.095")
        # with $0.63 banked on 4.100 (the lexicographic LOSER), it must lead the cluster
        ranked = [tk for _, tk, _, _ in c.candidates(progs, now, accrued={"pb": 0.63})]
        self.assertEqual(ranked[0], "KXAAAGASD-26JUL31-4.100",
                         "banked accrual must beat the tie-break: it is the cheapest finish")
        # and a cluster with banked accrual leads OTHER equal clusters in the rounds
        progs2 = progs + [mk("pc", "KXTRUEV-26JUL30-T1")]
        lead = [tk for _, tk, _, _ in scan.Classifier(max_markets=3).candidates(
            progs2, now, accrued={"pc": 0.63})][0]
        self.assertEqual(lead, "KXTRUEV-26JUL30-T1",
                         "the cheapest-need cluster leads the read rounds")


class TestPartialScanRetiresNothing(LipTestCase):
    """ABSENCE IS EVIDENCE ONLY WHEN THE MAP FINISHED ARRIVING (red team, 2026-07-31).
    Measured live the same night: 36 rate-starved partial scans and healthy top-earning
    rungs recalled `retired_venue_recalled` because the retirement diff ran against a
    partial program table. `last_scan_complete` existed for exactly this and had no
    production consumer."""

    def test_a_partial_map_does_not_retire_held_orders(self):
        from .. import runner as RUN, scan
        m = object.__new__(type("M", (), {}))
        class FakeMaker:
            orders = {"o1": {"ticker": "KXHELD-26AUG01-T1", "side": "bid", "remaining": 5}}
            retired_tickers = {"KXPREV-RETIRED"}
        r = RUN.Runner.__new__(RUN.Runner)
        r.scanner = scan.Scanner()
        r.m = FakeMaker()
        # partial map missing the held ticker
        r.scanner.last_scan_complete = False
        live = [{"tickers": ["KXOTHER-1"]}]
        live_tk = set()
        for prog in live:
            live_tk.update(prog.get("tickers") or [])
        if r.scanner.last_scan_complete:
            r.m.retired_tickers = {t for t in {o["ticker"] for o in r.m.orders.values()}
                                   if t not in live_tk}
        self.assertEqual(r.m.retired_tickers, {"KXPREV-RETIRED"},
                         "a partial map may not mint retirements")
        # complete map: retirement fires normally
        r.scanner.last_scan_complete = True
        if r.scanner.last_scan_complete:
            r.m.retired_tickers = {t for t in {o["ticker"] for o in r.m.orders.values()}
                                   if t not in live_tk}
        self.assertEqual(r.m.retired_tickers, {"KXHELD-26AUG01-T1"})

    def test_the_production_path_carries_the_gate(self):
        """Structural: the gate must guard the REAL retirement diff in runner.iteration,
        not just this test's replica — a mutation there must be caught here."""
        import inspect
        from .. import runner as RUN
        body = inspect.getsource(RUN.Runner.iteration)
        i_gate = body.find("if self.scanner.last_scan_complete:")
        i_diff = body.find("self.m.retired_tickers = ")
        self.assertGreater(i_gate, -1, "the completeness gate is gone from iteration()")
        self.assertGreater(i_diff, i_gate,
                           "the retirement diff must sit INSIDE the completeness gate")
