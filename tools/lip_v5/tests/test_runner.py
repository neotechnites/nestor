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

    def test_an_unqualified_side_is_REFUSED_not_funded(self):
        """REPLACES test_an_unqualified_side_gets_the_qualification_size (FREE_RIDE_ONLY armed
        2026-07-29).

        The old contract: a side short of target_size gets FUNDED to reach it, at
        LAND_GRAB_PRICE_C.  That path posted 990 contracts at 1c here, and on the live account it
        posted 999 in gas and 1,500/3,000 in TRUEV -- the largest objects in the whole tape.
        The CFTC filing says what they were worth: the qualifying walk stops once cumulative size
        reaches target, so size beyond it scores EXACTLY ZERO.  We were buying the -100% cohort's
        geometry in exchange for nothing.
        The new contract: qualification is worth the same to us whether we or a rival created it,
        and a rival's is free -- so a side that does not clear target WITHOUT us is skipped."""
        from .. import ratelimit as RL

        class Thin(ScanExchange):
            def book(self, ticker):
                return 200, book(yes=(("0.40", "10"),), no=(("0.58", "1200"),))

        ex = Thin()
        progs = scan.parse_programs(ex._programs)
        c = scan.Classifier()
        c.sweep(ex, RL.Bucket(NOW), progs, NOW)
        slots = scan.build_slots(progs, c, NOW)
        # bid side has 10 resting against a 1000 target -> does not qualify without us -> gone
        self.assertEqual([s for s in slots if s.side == "bid"], [],
                         "a side short of target must be REFUSED, never funded at 1c")
        # and the ask side, which DOES clear target on rival size alone (1200 >= 1000), survives
        asks = [s for s in slots if s.side == "ask"]
        self.assertTrue(asks, "a side that already qualifies on rival depth must still be quoted")
        self.assertEqual(asks[0].land_grab_size, 0, "free-riding never funds a grab")

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
        # ...and when it IS supplied it OBSERVES rather than excludes (config.P6_ADVISORY):
        # Kalshi's own docs pay for resting "even if your orders don't get filled", so an
        # untraded market is an UNCONTESTED one, not a worthless one.
        none_traded = scan.build_slots(progs, c, NOW, p6=lambda t: False)
        self.assertTrue(none_traded, "advisory P6 must not delete the quiet long tail")
        self.assertTrue(self.logs_of("p6_would_refuse"))
        # ...and the refusal end still works when the flag is turned off.
        try:
            C.P6_ADVISORY = False
            self.assertEqual(scan.build_slots(progs, c, NOW, p6=lambda t: False), [])
        finally:
            C.P6_ADVISORY = True
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

    def test_triage_runs_at_init_when_adopting(self):
        import unittest.mock as mock
        p = mock.patch.object(C, "CUTOVER_TRIAGE_ENABLED", True)
        p.start()
        self.addCleanup(p.stop)
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


class TestRecoveryOrders(RunnerCase):
    """BLOCKER-1 — resting orders survive a restart: replay rebuilds them, the cash feed
    counts their collateral, and the §9.4 step-4 prefix sweep reconciles both directions."""

    NESTOR = {"open_order_tickers": [], "position_tickers": []}

    def test_replay_rebuilds_a_live_order_and_counts_its_collateral(self):
        r = self.runner()
        r.m.ledger.write("place_resp", order_id="o1", coid="v5-lipm-T-y-1", ticker="T",
                         side="bid", price=0.40, size=10, fill_count=0, remaining_count=10,
                         expiration_ts=int(NOW + 3600), fully_closing=False)
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertIn("o1", r.m.orders)
        self.assertAlmostEqual(r.m.orders["o1"]["remaining"], 10.0, places=9)
        # THE INVARIANT: the exchange still holds these dollars, so the feed counts them.
        self.assertAlmostEqual(r.m.cash.resting_collateral, 4.0, places=9)

    def test_a_cancelled_or_expired_order_is_not_rebuilt(self):
        r = self.runner()
        for oid, terminal in (("o1", ("cancel_resp", {"http": 200, "reduced_by": 10})),
                              ("o2", ("expired", {}))):
            r.m.ledger.write("place_resp", order_id=oid, coid="v5-lipm-T-y-9", ticker="T",
                             side="bid", price=0.40, size=10, remaining_count=10,
                             expiration_ts=int(NOW + 3600))
            r.m.ledger.write(terminal[0], order_id=oid, ticker="T", **terminal[1])
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertEqual(r.m.orders, {})
        self.assertEqual(r.m.cash.resting_collateral, 0.0)

    def test_fill_obs_rows_shrink_the_rebuilt_remaining(self):
        r = self.runner()
        r.m.ledger.write("place_resp", order_id="o1", coid="v5-lipm-T-y-1", ticker="T",
                         side="bid", price=0.40, size=10, remaining_count=10,
                         expiration_ts=int(NOW + 3600))
        r.m.ledger.write("fill_obs", order_id="o1", ticker="T", side="bid", count=6,
                         price_c=40)
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertAlmostEqual(r.m.orders["o1"]["remaining"], 4.0, places=9)
        self.assertAlmostEqual(r.m.cash.resting_collateral, 1.6, places=9)

    def test_an_order_past_its_expiration_backstop_is_not_rebuilt(self):
        r = self.runner()
        r.m.ledger.write("place_resp", order_id="o1", coid="v5-lipm-T-y-1", ticker="T",
                         side="bid", price=0.40, size=10, remaining_count=10,
                         expiration_ts=int(NOW - 10))
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertNotIn("o1", r.m.orders)

    def test_the_prefix_sweep_finds_exchange_orders_replay_never_saw(self):
        """An exchange order wearing OUR prefix that replay does not know holds collateral
        and may fill: it enters `self.orders` (so it can be cancelled), the feed counts it,
        and B10's UNKNOWN machinery owns its resolution."""
        r = self.runner()
        r.m.ex.resting["x9"] = {"ticker": "T", "side": "bid", "count": 7,
                                "price": "0.5000", "client_order_id": "v5-lipm-T-y-99"}
        r.m.ex.resting["theirs"] = {"ticker": "T", "side": "bid", "count": 3,
                                    "price": "0.5000", "client_order_id": "v4-lipm-T-y-1"}
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertIn("x9", r.m.orders)
        self.assertIn("x9", r.m.unknown.pending)
        self.assertNotIn("theirs", r.m.orders)            # never touch another's orders
        self.assertAlmostEqual(r.m.cash.resting_collateral, 3.5, places=9)
        self.assertTrue(self.logs_of("recovery_unknown_order"))

    def test_a_replay_live_order_the_exchange_no_longer_shows_goes_to_UNKNOWN(self):
        r = self.runner()
        r.m.ledger.write("place_resp", order_id="gone", coid="v5-lipm-T-y-1", ticker="T",
                         side="bid", price=0.40, size=10, remaining_count=10,
                         expiration_ts=int(NOW + 3600))
        r.init(NOW, nestor_state=self.NESTOR)
        self.assertIn("gone", r.m.unknown.pending)
        self.assertTrue(self.logs_of("recovery_order_gone"))


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

    def test_the_still_resting_order_survives_recovery_with_its_collateral(self):
        r = self.runner()
        self.tape(r)
        r.init(NOW + 30, nestor_state=self.NESTOR)
        self.assertIn("o1", r.m.orders)                        # popped ⇒ uncancellable forever
        self.assertAlmostEqual(r.m.orders["o1"]["remaining"], 4.0, places=9)
        # published expected-cash NEVER above truth: the exchange still holds 4 × $0.40.
        self.assertAlmostEqual(r.m.cash.resting_collateral, 1.6, places=9)


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
        self.assertTrue(self.logs_of("p6_would_refuse"))

    def test_P6_a_traded_market_is_admitted(self):
        r = self.runner()                                 # default fake: one recent trade
        r.init(NOW, nestor_state=self.NESTOR)
        out = r.iteration(NOW + 1)
        self.assertGreater(out["slots"], 0)

    def test_venue_caps_bind_in_the_assembled_loop(self):
        """§1.4 was dormant because `self.venues` was never populated.  Now every venue in
        the slot table is capped: admitted at rung-0, or zero."""
        r = self.runner(ex=CheapScanExchange(balance_cents=1_000_000))
        r.init(NOW, nestor_state=self.NESTOR)
        out = r.iteration(NOW + 1)
        self.assertIn("KXAAAGASD", r.m.venues)
        st = r.m.venues["KXAAAGASD"]
        spent = out["allocate"]["spent"]
        self.assertGreater(spent, 0.0)
        self.assertLessEqual(spent, st.rung0_cap_usd + 1e-6)


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
        picked = [tk for _, tk, _ in c.candidates(progs, now)]
        clusters = {t.split("-")[0] for t in picked}
        self.assertIn("KXAAAGASD", clusters, "a distinct cluster must be discovered early")
        self.assertIn("KXTRUEV", clusters, "and so must the third")
        self.assertEqual(len(picked), 5)

    def test_the_strongest_cluster_still_leads(self):
        progs, now = self._programs()
        c = scan.Classifier(max_markets=5)
        picked = [tk for _, tk, _ in c.candidates(progs, now)]
        self.assertTrue(picked[0].startswith("KXUST10AD"),
                        "within the rounds, the richest cluster still goes first")
