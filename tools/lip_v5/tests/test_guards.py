"""The RAILS — B1..B13.

The through-line under test: v4 had CONSTANTS for several of these and NO CALL SITE.  So every
guard here is tested twice — once as arithmetic, and once through `place_allowed`, which is the
only thing the engine actually calls.
"""

import unittest

from .. import config as C, guards as G, runtime as R
from .base import LipTestCase


def order(ticker="KXAAAGASD-26JUL29-B4.120", side="yes", n=10, basis=0.50, closing=False):
    return {"ticker": ticker, "side": side, "n": n, "basis": basis, "fully_closing": closing}


class TestB2DayStop(LipTestCase):
    def test_the_v4_shape_exactly(self):
        self.assertAlmostEqual(G.day_stop_usd(100.0), 35.0)       # 0.35 x projected
        self.assertAlmostEqual(G.day_stop_usd(10.0), 20.0)        # floor
        self.assertAlmostEqual(G.day_stop_usd(1e6), 150.0)        # cap

    def test_breach_is_on_the_LOSS_magnitude(self):
        self.assertTrue(G.day_stop_breached(-35.0, 100.0))
        self.assertFalse(G.day_stop_breached(-34.99, 100.0))
        self.assertFalse(G.day_stop_breached(+500.0, 100.0))      # a win never stops us

    def test_UNPRICED_positions_mark_AT_COST_not_at_zero(self):
        """v4's NEW-2, carried: a PINNED rung is one-sided BY DEFINITION, so it cannot be
        marked.  Marking it at zero reads inventory as a TOTAL LOSS — two pinned $10 slots
        alone print −$20, exactly the day-stop floor, and the stop then cancels everything
        mid-window on precisely the gas books we are there for."""
        positions = {"P1": {"yes": 10.0, "no": 0.0}, "P2": {"yes": 10.0, "no": 0.0}}
        cost = {"P1": 10.0, "P2": 10.0}
        self.assertAlmostEqual(G.mark_to_market_pnl(positions, cost, {}), 0.0, places=9)
        self.assertFalse(G.day_stop_breached(G.mark_to_market_pnl(positions, cost, {}), 100.0))
        self.assertEqual(G.unpriced_positions(positions, {}), ["P1", "P2"])

    def test_a_priced_position_marks_normally(self):
        pnl = G.mark_to_market_pnl({"T": {"yes": 10.0, "no": 0.0}}, {"T": 4.0}, {"T": 0.50})
        self.assertAlmostEqual(pnl, 1.0, places=9)

    def test_a_NO_leg_marks_at_one_minus_the_yes_mid(self):
        pnl = G.mark_to_market_pnl({"T": {"yes": 0.0, "no": 10.0}}, {"T": 4.0}, {"T": 0.50})
        self.assertAlmostEqual(pnl, 1.0, places=9)

    def test_THE_FULLY_CLOSING_EXEMPTION(self):
        """A halted book must still be able to LEAVE.  Refusing a closing order would trap us
        in the position that tripped the stop."""
        ctx = G.PlaceContext(day_stopped=True)
        ok, reason, _ = G.place_allowed(ctx, order())
        self.assertFalse(ok)
        self.assertEqual(reason, "day_stop")
        ok, _, _ = G.place_allowed(ctx, order(closing=True))
        self.assertTrue(ok)


class TestB5HaltStateMachine(LipTestCase):
    def _halt(self):
        h = G.HaltState(self.path("v5_halt.json"))
        return h

    def test_halt_is_places_FIRST_check(self):
        h = self._halt().halt("test_reason", now=1.0)
        ctx = G.PlaceContext(halt_state=h, day_stopped=True, skew_ok=False)
        ok, reason, _ = G.place_allowed(ctx, order())
        self.assertFalse(ok)
        self.assertEqual(reason, "halted")           # not day_stop, not clock_skew

    def test_a_halt_SURVIVES_restart(self):
        """A halt a restart clears is not a halt — and every incident here ends with a process
        restarting into the condition that halted it."""
        self._halt().halt("day_stop", now=1.0)
        reloaded = G.HaltState(self.path("v5_halt.json")).load()
        self.assertTrue(reloaded.halted)
        self.assertEqual(reloaded.reason, "day_stop")

    def test_resume_requires_an_explicit_operator_record_never_a_timer(self):
        h = self._halt().halt("day_stop", now=1.0)
        with self.assertRaises(ValueError):
            h.resume("", now=2.0)
        h.resume("Ryan checked the book, cause understood", now=2.0)
        self.assertFalse(h.halted)
        self.assertFalse(G.HaltState(self.path("v5_halt.json")).load().halted)

    def test_halting_pages(self):
        self._halt().halt("day_stop", now=1.0)
        self.assertTrue(any(a[0] == "halt" for a in self.alerts))

    def test_a_halted_book_can_still_close(self):
        h = self._halt().halt("day_stop", now=1.0)
        ctx = G.PlaceContext(halt_state=h)
        ok, _, _ = G.place_allowed(ctx, order(closing=True))
        self.assertTrue(ok)


class TestB3Drawdown(LipTestCase):
    def test_the_peak_is_PERSISTED_or_the_bleed_erases_its_own_evidence(self):
        p = G.PeakRecord(self.path("peak.json"))
        p.observe(1000.0, now=1.0)
        reloaded = G.PeakRecord(self.path("peak.json")).load()
        self.assertAlmostEqual(reloaded.peak, 1000.0)

    def test_drawdown_from_peak_breaches(self):
        p = G.PeakRecord(self.path("peak.json"))
        p.observe(1000.0, now=1.0)
        dd, breached = p.observe(900.0, now=2.0)
        self.assertAlmostEqual(dd, 0.10, places=9)
        self.assertFalse(breached)
        dd, breached = p.observe(600.0, now=3.0)
        self.assertAlmostEqual(dd, 0.40, places=9)
        self.assertTrue(breached)

    def test_a_new_high_resets_the_drawdown_and_raises_the_peak(self):
        p = G.PeakRecord(self.path("peak.json"))
        p.observe(1000.0, now=1.0)
        p.observe(800.0, now=2.0)
        dd, breached = p.observe(1200.0, now=3.0)
        self.assertEqual((dd, breached), (0.0, False))
        self.assertAlmostEqual(p.peak, 1200.0)

    def test_a_slow_bleed_no_single_day_would_catch(self):
        """4% a day for ten days: no daily limit trips, the drawdown does."""
        p = G.PeakRecord(self.path("peak.json"))
        eq = 1000.0
        p.observe(eq, now=0.0)
        breached_on = None
        for day in range(1, 15):
            eq *= 0.96
            dd, breached = p.observe(eq, now=float(day))
            if breached:
                breached_on = day
                break
        self.assertIsNotNone(breached_on)
        self.assertLessEqual(breached_on, 12)


class TestB4DailyLossAttribution(LipTestCase):
    ROWS = [
        {"ticker": "OLD", "realized_pnl": -80.0, "settled_day": 10, "opened_day": 3},
        {"ticker": "NEW", "realized_pnl": -5.0, "settled_day": 10, "opened_day": 10},
        {"ticker": "WIN", "realized_pnl": +40.0, "settled_day": 10, "opened_day": 2},
    ]

    def test_a_multi_day_settlement_does_NOT_trip_todays_limit(self):
        """The whole guard: charging an old position's loss to its SETTLEMENT day halts a
        healthy book for a bet today never made."""
        today = G.daily_realized_loss(self.ROWS, day_key=10)
        self.assertAlmostEqual(today, -5.0, places=9)
        self.assertFalse(G.daily_loss_breached(today, 0.0, limit_usd=20.0))

    def test_naive_settlement_day_attribution_WOULD_have_tripped(self):
        naive = sum(r["realized_pnl"] for r in self.ROWS if r["settled_day"] == 10)
        self.assertAlmostEqual(naive, -45.0, places=9)
        self.assertTrue(G.daily_loss_breached(naive, 0.0, limit_usd=20.0))

    def test_an_old_win_does_not_fund_todays_risk_either(self):
        self.assertAlmostEqual(G.daily_realized_loss(self.ROWS, day_key=2), 40.0)
        self.assertAlmostEqual(G.daily_realized_loss(self.ROWS, day_key=3), -80.0)

    def test_an_unknowable_open_day_is_charged_where_it_landed(self):
        rows = [{"ticker": "X", "realized_pnl": -10.0, "settled_day": 10}]
        self.assertAlmostEqual(G.daily_realized_loss(rows, day_key=10), -10.0)

    def test_a_lookup_supplies_a_missing_open_day(self):
        rows = [{"ticker": "X", "realized_pnl": -10.0, "settled_day": 10}]
        self.assertAlmostEqual(
            G.daily_realized_loss(rows, day_key=10, open_day_of=lambda t: 4), 0.0)


class TestB6PersistFailClosed(LipTestCase):
    def test_a_write_failure_while_LIVE_halts_and_pages(self):
        h = G.HaltState(self.path("halt.json"))
        pg = G.PersistGuard(h)
        R.set_live(True)
        try:
            ok, err = pg.write(lambda: (_ for _ in ()).throw(IOError("disk full")))
        finally:
            R.set_live(False)
        self.assertFalse(ok)
        self.assertTrue(h.halted)
        self.assertEqual(h.reason, "persist_failure")

    def test_it_retries_before_halting(self):
        h = G.HaltState(self.path("halt.json"))
        pg = G.PersistGuard(h)
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise IOError("transient")
            return "ok"

        ok, res = pg.write(flaky)
        self.assertTrue(ok)
        self.assertEqual(res, "ok")
        self.assertFalse(h.halted)

    def test_while_INERT_it_logs_instead_of_halting(self):
        h = G.HaltState(self.path("halt.json"))
        pg = G.PersistGuard(h)
        ok, _ = pg.write(lambda: (_ for _ in ()).throw(IOError("x")))
        self.assertFalse(ok)
        self.assertFalse(h.halted)
        self.assertTrue(self.logs_of("persist_failure_inert"))


class TestB7FreshStateRefusal(LipTestCase):
    def test_blank_ledger_plus_an_adopt_file_REFUSES(self):
        msg = G.fresh_state_refusal([], adopt_exists=True, exchange_positions={})
        self.assertIsNotNone(msg)

    def test_blank_ledger_plus_live_positions_REFUSES(self):
        msg = G.fresh_state_refusal([], adopt_exists=False,
                                    exchange_positions={"T": 10.0})
        self.assertIsNotNone(msg)

    def test_a_genuinely_new_account_is_allowed(self):
        self.assertIsNone(G.fresh_state_refusal([], False, {}))
        self.assertIsNone(G.fresh_state_refusal([], False, {"T": 0.0}))

    def test_a_populated_ledger_is_always_fine(self):
        self.assertIsNone(G.fresh_state_refusal([{"k": "place_req"}], True, {"T": 5.0}))

    def test_the_escape_must_be_STATED_not_inferred(self):
        self.assertIsNone(G.fresh_state_refusal([], True, {"T": 5.0}, allow_flag=True))


class TestB8FillDedupe(LipTestCase):
    def test_the_same_exchange_fill_id_is_booked_once(self):
        d = G.FillDedupe()
        self.assertTrue(d.is_new("f1"))
        self.assertFalse(d.is_new("f1"))

    def test_an_unkeyed_fill_is_ACCEPTED_and_surfaced(self):
        """Dropping a real fill understates inventory — the naked-short direction."""
        d = G.FillDedupe()
        self.assertTrue(d.is_new(None))
        self.assertTrue(self.logs_of("fill_unkeyed"))

    def test_a_fallback_key_dedupes_the_crash_gap_class(self):
        d = G.FillDedupe()
        self.assertTrue(d.is_new(None, fallback_key="T|bid|5|40"))
        self.assertFalse(d.is_new(None, fallback_key="T|bid|5|40"))


class TestB9RefillCap(LipTestCase):
    def test_turnover_beyond_the_cap_stops_the_slot(self):
        rt = G.RefillTracker()
        n_cap = lambda p: int(10.0 / p)                     # noqa: E731 - test stub
        self.assertFalse(rt.exhausted("T", "bid", 0.50, n_cap))
        rt.note_fill("T", "bid", 4 * 20)                    # 4 turnovers of n_cap=20
        self.assertTrue(rt.exhausted("T", "bid", 0.50, n_cap))

    def test_it_bounds_a_1Hz_failure_the_15_minute_kill_cannot(self):
        """§2.5 evaluates every 15 min; a slot can turn over its whole cap many times inside
        one bucket."""
        rt = G.RefillTracker()
        n_cap = lambda p: int(10.0 / p)                     # noqa: E731
        for _ in range(80):
            rt.note_fill("T", "bid", 1)
        self.assertTrue(rt.exhausted("T", "bid", 0.50, n_cap))

    def test_the_window_resets(self):
        rt = G.RefillTracker()
        n_cap = lambda p: int(10.0 / p)                     # noqa: E731
        rt.note_fill("T", "bid", 1000)
        rt.reset_window()
        self.assertFalse(rt.exhausted("T", "bid", 0.50, n_cap))

    def test_it_is_enforced_through_place_allowed(self):
        """The tracker is fed by `book_fill` in the ORDER axis ("bid"/"ask"); the guard
        converts the order dict's leg axis to match.  The earlier form of this test noted
        fills keyed "yes" — encoding the exact axis mismatch that kept the guard from ever
        firing in the assembled loop (found by the replenish fixture)."""
        rt = G.RefillTracker()
        rt.note_fill("KXAAAGASD-1", "bid", 1000)          # what book_fill actually writes
        ctx = G.PlaceContext(refill=rt, n_cap_fn=lambda p: int(10.0 / p))
        ok, reason, _ = G.place_allowed(ctx, order(ticker="KXAAAGASD-1"))
        self.assertFalse(ok)
        self.assertEqual(reason, "refill_cap")


class TestB10UnknownOrders(LipTestCase):
    def test_retries_are_BOUNDED_then_booked_conservatively(self):
        u = G.UnknownOrders()
        u.note("o1", "T", "bid", 10.0, now=0.0)
        for i in range(1, C.UNKNOWN_MAX_RETRIES + 1):
            t = i * C.UNKNOWN_RETRY_S
            self.assertIn("o1", u.due(now=t))
            u.attempted("o1", now=t)
        self.assertEqual(u.due(now=10 * C.UNKNOWN_RETRY_S), [])
        self.assertEqual([oid for oid, _ in u.exhausted()], ["o1"])

    def test_it_is_not_retried_faster_than_the_cadence(self):
        u = G.UnknownOrders()
        u.note("o1", "T", "bid", 10.0, now=0.0)
        u.attempted("o1", now=0.0)
        self.assertEqual(u.due(now=10.0), [])
        self.assertEqual(u.due(now=C.UNKNOWN_RETRY_S + 1), ["o1"])

    def test_resolution_clears_it(self):
        u = G.UnknownOrders()
        u.note("o1", "T", "bid", 10.0, now=0.0)
        u.resolved("o1")
        self.assertEqual(u.exhausted(), [])


class TestB11CapitalFloor(LipTestCase):
    def test_below_the_floor_placement_refuses(self):
        self.assertFalse(G.capital_floor_ok(10.0))
        self.assertTrue(G.capital_floor_ok(C.CAPITAL_FLOOR_USD))

    def test_it_is_enforced_through_place_allowed(self):
        ctx = G.PlaceContext(available_cash_usd=1.0)
        ok, reason, _ = G.place_allowed(ctx, order())
        self.assertFalse(ok)
        self.assertEqual(reason, "capital_floor")

    def test_a_closing_order_is_exempt(self):
        """Leaving must never require capital we do not have."""
        ctx = G.PlaceContext(available_cash_usd=0.0)
        ok, _, _ = G.place_allowed(ctx, order(closing=True))
        self.assertTrue(ok)


class TestB12ClockSkew(LipTestCase):
    def test_skew_beyond_tolerance_alarms(self):
        self.assertFalse(G.clock_skew_alarming(G.clock_skew_s(1000.0, 1010.0)))
        self.assertTrue(G.clock_skew_alarming(G.clock_skew_s(1000.0, 1040.0)))
        self.assertTrue(G.clock_skew_alarming(G.clock_skew_s(1040.0, 1000.0)))

    def test_it_blocks_opening_orders_but_not_closing_ones(self):
        ctx = G.PlaceContext(skew_ok=False)
        ok, reason, _ = G.place_allowed(ctx, order())
        self.assertFalse(ok)
        self.assertEqual(reason, "clock_skew")
        ok, _, _ = G.place_allowed(ctx, order(closing=True))
        self.assertTrue(ok)


class TestB13CrossBotExclusion(LipTestCase):
    def test_BOTH_halves_exclude(self):
        self.assertTrue(G.cross_bot_excluded("T", {"T"}, set()))
        self.assertTrue(G.cross_bot_excluded("T", set(), {"T"}))
        self.assertFalse(G.cross_bot_excluded("T", {"OTHER"}, {"OTHER"}))

    def test_the_POSITION_half_is_the_one_that_was_missing(self):
        """nestor can hold a position on a market it has no resting order in; v5 quoting there
        attributes nestor's inventory to itself at the next reconcile."""
        ctx = G.PlaceContext(nestor_orders=set(), nestor_positions={"KXAAAGASD-1"})
        ok, reason, _ = G.place_allowed(ctx, order(ticker="KXAAAGASD-1"))
        self.assertFalse(ok)
        self.assertEqual(reason, "cross_bot")

    def test_it_applies_to_closing_orders_too(self):
        """We must not touch nestor's market at all — including to 'close', since the position
        we would be closing may be nestor's."""
        ctx = G.PlaceContext(nestor_positions={"KXAAAGASD-1"})
        ok, reason, _ = G.place_allowed(ctx, order(ticker="KXAAAGASD-1", closing=True))
        self.assertFalse(ok)


class TestDenyList(LipTestCase):
    def test_the_eight_measured_toxic_venues_are_refused(self):
        for s in ("KXRAIN", "KXINXHUD", "KXNDQHUD", "KXMLBMENTION",
                  "KXEARNINGSMENTIONBA", "KXEARNINGSMENTIONPYPL", "KXWNBAMENTION",
                  "KXDXYDUD"):
            self.assertTrue(C.series_denied("%s-26JUL29-T1" % s), s)
            ok, reason, _ = G.place_allowed(G.PlaceContext(),
                                            order(ticker="%s-26JUL29-T1" % s))
            self.assertFalse(ok)
            self.assertEqual(reason, "series_denied")

    def test_a_good_venue_is_not_denied(self):
        self.assertFalse(C.series_denied("KXAAAGASD-26JUL29-B4.120"))

    def test_a_series_that_merely_shares_a_spelling_is_NOT_denied(self):
        """Finish-round charter D: the bare-`startswith` clause denied every series whose
        name merely EXTENDED a toxic one — evidence about KXRAIN is not evidence about
        KXRAINBOW.  The match is exact-or-dash-anchored."""
        self.assertTrue(C.series_denied("KXRAIN"))
        self.assertTrue(C.series_denied("KXRAIN-26JUL29-T1"))
        # KXRAIN* is DELIBERATELY BROAD (see config.DENY_SUBSTRINGS): the real toxic series
        # are KXRAINAUSM / KXRAINHOUM / ..., so an exact-prefix rule un-banned the whole
        # family and rain came back into a live book on 2026-07-28.  A hypothetical
        # "KXRAINBOW" is therefore caught too — an acceptable false positive, because
        # admitting measured-toxic rain costs money while over-denying one hypothetical
        # venue costs one venue.  Un-deny it explicitly if it ever exists and matters.
        self.assertTrue(C.series_denied("KXRAINBOW-26JUL29-T1"))
        # The over-matching guard itself still holds for everything else:
        self.assertFalse(C.series_denied("KXUSTAR-26JUL29-T1"))     # not KXUST2AD etc.
        self.assertFalse(C.series_denied("KXGASOLINE-26JUL29-T1"))
        self.assertFalse(C.series_denied("KXINXHUDX-1"))


class TestOrderedGate(LipTestCase):
    """The ORDER of the rails is derived, and asserted, because a reordering that still passes
    every individual guard test can change which reason an operator is shown."""

    def test_halt_beats_everything(self):
        h = G.HaltState(self.path("h.json")).halt("x", 1.0)
        ctx = G.PlaceContext(halt_state=h, day_stopped=True, skew_ok=False,
                             available_cash_usd=0.0,
                             nestor_positions={"KXAAAGASD-1"})
        ok, reason, _ = G.place_allowed(ctx, order(ticker="KXAAAGASD-1"))
        self.assertEqual(reason, "halted")

    def test_day_stop_beats_the_eligibility_and_size_rails(self):
        ctx = G.PlaceContext(day_stopped=True, available_cash_usd=0.0,
                             nestor_positions={"KXAAAGASD-1"})
        ok, reason, _ = G.place_allowed(ctx, order(ticker="KXAAAGASD-1"))
        self.assertEqual(reason, "day_stop")

    def test_eligibility_beats_the_size_caps(self):
        """A market we may not quote at all needs no cluster arithmetic."""
        ctx = G.PlaceContext(nestor_positions={"KXAAAGASD-1"}, cluster_cap_usd=0.0)
        ok, reason, _ = G.place_allowed(ctx, order(ticker="KXAAAGASD-1"))
        self.assertEqual(reason, "cross_bot")

    def test_a_clean_order_passes_every_rail(self):
        ctx = G.PlaceContext(available_cash_usd=100.0, cluster_cap_usd=100.0)
        ok, reason, _ = G.place_allowed(ctx, order())
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_B1_the_cluster_cap_is_enforced_in_the_gate(self):
        existing = [{"ticker": "KXUST10AD-26JUL28-T%.2f" % (4.0 + 0.05 * i),
                     "side": "yes", "n": 20, "basis": 0.50} for i in range(5)]
        ctx = G.PlaceContext(positions=existing, cluster_cap_usd=52.5,
                             available_cash_usd=100.0)
        ok, reason, detail = G.place_allowed(
            ctx, order(ticker="KXUST2AD-26JUL28-T4.30", n=20, basis=0.50))
        self.assertFalse(ok)
        self.assertIn("cluster", reason)
        self.assertEqual(detail["cluster"], "RATES")

    def test_B1_counts_RESTING_basis_as_well_as_open_positions(self):
        """"apply to open+resting basis" — an order that has not filled yet is exposure we
        have already committed to."""
        resting = [{"ticker": "KXUST10AD-26JUL28-T%.2f" % (4.0 + 0.05 * i),
                    "side": "yes", "n": 20, "basis": 0.50} for i in range(5)]
        ctx = G.PlaceContext(resting_basis=resting, cluster_cap_usd=52.5,
                             available_cash_usd=100.0)
        ok, reason, _ = G.place_allowed(
            ctx, order(ticker="KXUST2AD-26JUL28-T4.30", n=20, basis=0.50))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()


class TestB15TheCeilingBindsAtPlacement(LipTestCase):
    """The ceiling was a PLAN-TIME budget only.  Measured on the tape: resting notional p99
    $1,358 and a peak of $6,077 against a ledger-declared ceiling of $45.  These tests exist
    because 601 tests passed while the guard did not exist -- absence of a refusal path is
    invisible to a suite that never constructs the breach."""

    def test_it_refuses_when_committed_plus_the_order_exceeds_the_ceiling(self):
        ctx = G.PlaceContext(
            ceiling_usd=100.0,
            positions=[{"ticker": "A-1", "side": "yes", "n": 100, "basis": 0.50}],   # $50
            resting_basis=[{"ticker": "B-1", "side": "yes", "n": 80, "basis": 0.50}])  # $40
        ok, reason, d = G.place_allowed(ctx, order(ticker="C-1", n=40, basis=0.50))   # +$20
        self.assertFalse(ok)
        self.assertEqual(reason, "ceiling")
        self.assertAlmostEqual(d["committed"], 90.0)
        self.assertAlmostEqual(d["add"], 20.0)

    def test_it_allows_the_order_that_lands_exactly_ON_the_ceiling(self):
        ctx = G.PlaceContext(
            ceiling_usd=100.0,
            positions=[{"ticker": "A-1", "side": "yes", "n": 100, "basis": 0.50}])
        ok, reason, _ = G.place_allowed(ctx, order(ticker="C-1", n=100, basis=0.50))
        self.assertTrue(ok, reason)

    def test_a_FULLY_CLOSING_order_is_exempt_so_a_book_at_its_ceiling_can_LEAVE(self):
        ctx = G.PlaceContext(
            ceiling_usd=10.0,
            positions=[{"ticker": "A-1", "side": "yes", "n": 1000, "basis": 0.50}])
        ok, reason, _ = G.place_allowed(ctx, order(ticker="A-1", n=100, basis=0.50, closing=True))
        self.assertTrue(ok, reason)

    def test_no_ceiling_configured_is_a_no_op_not_a_refusal(self):
        ctx = G.PlaceContext(positions=[{"ticker": "A-1", "side": "yes", "n": 1e6, "basis": 0.99}])
        ok, reason, _ = G.place_allowed(ctx, order())
        self.assertTrue(ok, reason)


class TestB16ThePerMarketAcquisitionCap(LipTestCase):
    """We do not exit -- 149 of 6,149 acquired contracts were ever closed (2.4%), across seven
    closing orders, all takers.  With no exit NET equals GROSS, so refusing to acquire is the
    only control on one market's directional exposure.  Matched pairs earned +$39.63; the
    unmatched residual lost -$587.42."""

    def test_it_counts_only_the_SAME_market(self):
        ctx = G.PlaceContext(market_cap_usd=10.0,
                             positions=[{"ticker": "OTHER-1", "side": "yes", "n": 1000, "basis": 0.50}])
        ok, reason, _ = G.place_allowed(ctx, order(ticker="MINE-1", n=10, basis=0.50))
        self.assertTrue(ok, reason)            # $500 elsewhere is irrelevant to MINE-1

    def test_it_refuses_once_one_market_would_exceed_its_cap(self):
        ctx = G.PlaceContext(market_cap_usd=10.0,
                             positions=[{"ticker": "MINE-1", "side": "yes", "n": 16, "basis": 0.50}])
        ok, reason, d = G.place_allowed(ctx, order(ticker="MINE-1", n=6, basis=0.50))
        self.assertFalse(ok)
        self.assertEqual(reason, "market_cap")
        self.assertAlmostEqual(d["held"], 8.0)

    def test_RESTING_size_counts_too_not_just_open_positions(self):
        """A resting order is a commitment to acquire.  Counting only positions would let the
        book queue up unbounded exposure and discover it on the fills."""
        ctx = G.PlaceContext(market_cap_usd=10.0,
                             resting_basis=[{"ticker": "MINE-1", "side": "yes", "n": 20, "basis": 0.50}])
        ok, reason, _ = G.place_allowed(ctx, order(ticker="MINE-1", n=2, basis=0.50))
        self.assertFalse(ok)
        self.assertEqual(reason, "market_cap")

    def test_a_FULLY_CLOSING_order_is_exempt(self):
        ctx = G.PlaceContext(market_cap_usd=1.0,
                             positions=[{"ticker": "MINE-1", "side": "yes", "n": 1000, "basis": 0.50}])
        ok, reason, _ = G.place_allowed(ctx, order(ticker="MINE-1", n=100, basis=0.50, closing=True))
        self.assertTrue(ok, reason)

    def test_the_cap_derives_from_the_ceiling_and_REPLACED_v1s_inherited_bound(self):
        """WAS `..._is_strictly_inside_v1s_inherited_bound`, asserting
        `MARKET_CAP_FRAC < PER_MARKET_BUDGET_FRAC`.

        D2: "strictly inside" was the defect, not the property.  B16's derivation claimed it
        REPLACED v1's 0.25 — but nothing assigned it, so `alloc.market_cap_usd` went on reading
        0.25 and THE PLAN PERMITTED $75 OF ONE MARKET WHILE THE RAIL REFUSED ABOVE $30.  A rail
        tighter than the plan arms no degrade: `place()` returns False and the slot re-offers the
        same refused order every cycle, forever.  So the two are now ONE number and this test
        asserts the identity the derivation always claimed.
        """
        self.assertEqual(C.MARKET_CAP_FRAC, C.PER_MARKET_BUDGET_FRAC,
                         "plan and rail must not hold separate opinions about one market")
        self.assertAlmostEqual(C.MARKET_CAP_FRAC * 300.0, 30.0)

    def test_the_per_market_cap_can_never_fall_below_the_per_slot_cap(self):
        """D2's nesting half.  `MARKET_CAP_FRAC × ceiling` alone INVERTS below ~$100 of ceiling:
        at $45 it is $4.50 against a $10 slot cap, and the allocator plans to its own $10.  An
        inverted cap is a permanent re-offer loop, not a conservative one."""
        for ceiling in (20.0, 45.0, 100.0, 150.0, 300.0, 1000.0):
            day_stop = G.day_stop_usd(0.0, ceiling_usd=ceiling)
            self.assertGreaterEqual(
                C.market_leg_cap_usd(ceiling, day_stop) + 1e-9, C.slot_cap_usd(day_stop),
                "market cap inverted under the slot cap at ceiling $%.0f" % ceiling)
        # and the bare fraction inverts below slot_cap/MARKET_CAP_FRAC of ceiling — at the
        # $2.50 lot container that boundary is $25 of ceiling, so the `max` is load-bearing
        # exactly there (a $45 book no longer inverts; a $20 one does)
        self.assertLess(C.MARKET_CAP_FRAC * 20.0,
                        C.slot_cap_usd(G.day_stop_usd(0.0, ceiling_usd=20.0)))

    def test_the_cap_is_PER_LEG_so_a_full_bid_leg_does_not_refuse_the_ask(self):
        """D2's side half.  Exactly one outcome pays, so worst-case market loss is
        `max(yes_collateral, no_collateral)` — never the sum.  Under a gross sum a bid leg at the
        cap refused the ask outright, and matched pairs (the ONLY profitable configuration on the
        tape, +$39.63) hit the cap fastest of all."""
        full_bid = [{"ticker": "MINE-1", "side": "yes", "n": 60, "basis": 0.50}]   # $30, at cap
        ctx = G.PlaceContext(market_cap_usd=30.0, resting_basis=full_bid)
        ok, reason, _ = G.place_allowed(ctx, order(ticker="MINE-1", side="no",
                                                  n=60, basis=0.50))
        self.assertTrue(ok, "the OPPOSING leg must remain quotable: %s" % reason)
        # ...and the SAME leg is still bounded
        ok2, reason2, _ = G.place_allowed(ctx, order(ticker="MINE-1", side="yes",
                                                     n=2, basis=0.50))
        self.assertFalse(ok2)
        self.assertEqual(reason2, "market_cap")
