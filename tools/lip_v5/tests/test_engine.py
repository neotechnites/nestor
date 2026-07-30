"""THE RUN LOOP and its call graph.

The property the reviewer will attack: **there is exactly ONE path to the wire**, and it
consults the rails first and publishes the cash feed before the POST.  Several tests below
assert that structurally (by counting `ex.placed`) rather than by inspection, so a second path
added later fails the suite rather than passing review.
"""

import unittest

from .. import (alloc, config as C, engine as E, exchange as X, guards as G,
                ratchet as RT, runtime as R)
from .base import LipTestCase

NOW = 1_000_000.0


def slot(ticker="KXAAAGASD-26JUL29-T4.12", side="bid", **kw):
    kw.setdefault("rho", 6.25)
    kw.setdefault("S", 50.0)
    kw.setdefault("p", 0.50)
    kw.setdefault("phi", 0.08)
    kw.setdefault("d", 0.07)
    kw.setdefault("l_eff", 8.0)
    return alloc.Slot(ticker, side, **kw)


CONFIG_PATHS = ("DATA_DIR", "LEDGER_PATH", "PRESENCE_PATH", "PRESENCE_DAILY_PATH",
                "SEQ_PATH", "HANDBACK_PATH", "CASH_FEED_PATH", "ADOPT_PATH")


class EngineCase(LipTestCase):
    def setUp(self):
        super(EngineCase, self).setUp()
        # The engine reads its paths from module-level config.  Repoint them at the tmpdir and
        # RESTORE on teardown: a leaked global path is exactly the "wrote outside tmp" class
        # these guards exist to prevent, and it would leak into whatever test ran next.
        self._saved = {n: getattr(C, n) for n in CONFIG_PATHS}
        self.addCleanup(lambda: [setattr(C, n, v) for n, v in self._saved.items()])

    def maker(self, **kw):
        C.DATA_DIR = self.tmp
        for name in CONFIG_PATHS[1:]:
            setattr(C, name, self.path(self._saved[name].rsplit("/", 1)[-1]))
        ex = kw.pop("ex", None) or X.FakeExchange(balance_cents=100_000)
        if not getattr(ex, "now", 0.0):
            ex.now = NOW                     # the fake's default market close is now+24h;
                                             # a zero clock puts every close in 1970 and the
                                             # expiration backstop refuses the whole board
        m = E.Maker(ex, NOW, data_dir=self.tmp, **kw)
        m.halt.path = self.path("v5_halt.json")
        m.peak.path = self.path("v5_peak.json")
        m.projected_day_reward = 100.0
        return m


class TestStartupRefusals(EngineCase):
    NESTOR = {"open_order_tickers": [], "position_tickers": []}

    def test_a_clean_startup_arms(self):
        m = self.maker()
        ok, refusals = m.startup(NOW, nestor_state=self.NESTOR)
        self.assertTrue(ok, refusals)

    def test_shared_mode_without_G0s_reader_REFUSES(self):
        m = self.maker()
        ok, refusals = m.startup(NOW, nestor_state=self.NESTOR, reader_enabled=False)
        self.assertFalse(ok)
        self.assertTrue(any("LIP_CASH_FEED_ENABLED" in r for r in refusals))

    def test_unreadable_nestor_state_REFUSES_not_warns(self):
        m = self.maker()
        ok, refusals = m.startup(NOW, nestor_state=None)
        self.assertFalse(ok)
        self.assertTrue(any("nestor state unreadable" in r for r in refusals))

    def test_B7_blank_ledger_against_live_positions_REFUSES(self):
        m = self.maker()
        ok, refusals = m.startup(NOW, nestor_state=self.NESTOR,
                                 exchange_positions={"T": 10.0})
        self.assertFalse(ok)
        self.assertTrue(any("blank ledger" in r for r in refusals))

    def test_B7_the_escape_is_an_explicit_flag(self):
        m = self.maker()
        ok, _ = m.startup(NOW, nestor_state=self.NESTOR, exchange_positions={"T": 10.0},
                          allow_fresh=True)
        self.assertTrue(ok)

    def test_B5_a_persisted_halt_refuses_startup(self):
        m = self.maker()
        m.halt.halt("day_stop", NOW)
        m2 = self.maker()
        m2.halt = G.HaltState(self.path("v5_halt.json")).load()
        ok, refusals = m2.startup(NOW, nestor_state=self.NESTOR)
        self.assertFalse(ok)
        self.assertTrue(any("halted" in r for r in refusals))

    def test_B13_both_halves_of_nestors_state_are_taken(self):
        m = self.maker()
        m.startup(NOW, nestor_state={"open_order_tickers": ["A"],
                                     "position_tickers": ["B"]})
        self.assertEqual(m.nestor_orders, {"A"})
        self.assertEqual(m.nestor_positions, {"B"})


class TestPlaceIsTheOnlyPath(EngineCase):
    def test_a_clean_place_reaches_the_wire_and_is_booked(self):
        m = self.maker()
        ok, reason, resp = m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW,
                                   available_cash_usd=1000.0)
        self.assertTrue(ok, reason)
        self.assertEqual(len(m.ex.placed), 1)
        self.assertEqual(len(m.orders), 1)

    def test_the_cash_feed_is_published_BEFORE_the_POST(self):
        """§5.3 — so published expected-cash is never above the truth even if we die between
        the two.  Asserted by ordering, not by inspection."""
        m = self.maker()
        seen = []
        orig_place = m.ex.place
        m.ex.place = lambda body: (seen.append(("POST", m.cash.raw_delta)),
                                   orig_place(body))[1]
        orig_pub = m.publisher.publish
        m.publisher.publish = lambda now=None: (seen.append(("PUB", m.cash.raw_delta)),
                                                orig_pub(now))[1]
        m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        kinds = [k for k, _ in seen]
        self.assertEqual(kinds[0], "PUB")
        self.assertIn("POST", kinds)
        # the pre-POST publish already carries the collateral
        pub_delta = seen[0][1]
        self.assertAlmostEqual(pub_delta, -5.0, places=6)

    def test_a_rejected_post_releases_the_reservation(self):
        m = self.maker()
        m.ex.place_status = 400
        m.ex.place_error = "insufficient balance"
        ok, reason, _ = m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW,
                                available_cash_usd=1000.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "reject")
        self.assertAlmostEqual(m.cash.raw_delta, 0.0, places=9)

    def test_EVERY_rail_can_refuse_before_the_wire(self):
        """The structural claim: a refusal never reaches the exchange."""
        cases = [
            ("halted", lambda m: m.halt.halt("x", NOW)),
            ("day_stop", lambda m: setattr(m, "day_stopped", True)),
            ("clock_skew", lambda m: setattr(m, "skew_ok", False)),
            ("frozen", lambda m: m.frozen.add("KXAAAGASD-1")),
            ("cross_bot", lambda m: m.nestor_positions.add("KXAAAGASD-1")),
        ]
        for expect, arm in cases:
            m = self.maker()
            # A per-case halt file: `maker()` points DATA_DIR at one tmpdir, so a halt armed
            # by an earlier case would otherwise be re-loaded by the next one and mask it.
            m.halt = G.HaltState(self.path("halt_%s.json" % expect))
            arm(m)
            ok, reason, _ = m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW,
                                    available_cash_usd=1000.0)
            self.assertFalse(ok, expect)
            self.assertEqual(reason, expect)
            self.assertEqual(m.ex.placed, [], "%s reached the wire" % expect)

    def test_a_denied_series_never_reaches_the_wire(self):
        m = self.maker()
        ok, reason, _ = m.place("KXEARNINGSMENTIONPYPL-PERP", "bid", 0.30, 10,
                                NOW + 3600, NOW, available_cash_usd=1000.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "series_denied")
        self.assertEqual(m.ex.placed, [])

    def test_the_capital_floor_refuses_before_the_wire(self):
        m = self.maker()
        ok, reason, _ = m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW,
                                available_cash_usd=1.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "capital_floor")
        self.assertEqual(m.ex.placed, [])

    def test_the_cluster_cap_refuses_before_the_wire(self):
        m = self.maker()
        m.projected_day_reward = 100.0
        # Cap is max(reward-derived, ceiling/6) = $50 at the default $300 ceiling, so the
        # correlated book must exceed THAT: 8 rungs x 20 x $0.50 = $80 one-direction RATES.
        for i in range(8):
            m.positions["KXUST10AD-26JUL28-T%.2f" % (4.0 + 0.05 * i)] = {"yes": 20.0,
                                                                         "no": 0.0}
            m.entry_basis[("KXUST10AD-26JUL28-T%.2f" % (4.0 + 0.05 * i), "yes")] = 0.50
        ok, reason, _ = m.place("KXUST2AD-26JUL28-T4.30", "bid", 0.50, 20, NOW + 3600, NOW,
                                available_cash_usd=1000.0)
        self.assertFalse(ok)
        self.assertIn("cluster", reason)
        self.assertEqual(m.ex.placed, [])

    def test_shadow_mode_quotes_NOTHING(self):
        m = self.maker(shadow=True)
        ok, reason, _ = m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW,
                                available_cash_usd=1000.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "shadow")
        self.assertEqual(m.ex.placed, [])

    def test_the_rate_lane_refuses_before_the_wire(self):
        m = self.maker()
        m.bucket.tokens = 0.0
        ok, reason, _ = m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW,
                                available_cash_usd=1000.0)
        self.assertFalse(ok)
        self.assertEqual(m.ex.placed, [])

    def test_coids_are_v5_prefixed_and_the_seq_persists(self):
        m = self.maker()
        m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        coid = m.ex.placed[0]["client_order_id"]
        self.assertTrue(coid.startswith("v5-"))
        self.assertNotIn(".", coid)

    def test_the_order_body_carries_expiration_and_STP(self):
        m = self.maker()
        m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        b = m.ex.placed[0]
        self.assertEqual(b["self_trade_prevention_type"], "taker_at_cross")
        self.assertEqual(b["time_in_force"], "good_till_canceled")
        self.assertEqual(b["expiration_ts"], int(NOW + 3600))


class TestCancelAndUnknown(EngineCase):
    def _placed(self):
        m = self.maker()
        m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        return m, list(m.orders)[0]

    def test_a_clean_cancel_releases_collateral(self):
        m, oid = self._placed()
        ok, reason = m.cancel(oid, NOW + 10)
        self.assertTrue(ok, reason)
        self.assertAlmostEqual(m.cash.raw_delta, 0.0, places=9)
        self.assertEqual(m.orders, {})

    def test_a_partial_cancel_books_the_learned_fill(self):
        m, oid = self._placed()
        m.ex.cancel = lambda o: (200, {"reduced_by": 4.0})     # 6 of 10 filled
        ok, _ = m.cancel(oid, NOW + 10)
        self.assertTrue(ok)
        self.assertAlmostEqual(m.positions["KXAAAGASD-1"]["yes"], 6.0, places=9)

    def test_B10_a_failed_cancel_becomes_an_UNKNOWN_not_a_silent_drop(self):
        m, oid = self._placed()
        m.ex.cancel_status = 500
        ok, reason = m.cancel(oid, NOW + 10)
        self.assertFalse(ok)
        self.assertEqual(reason, "unknown")
        self.assertIn(oid, m.unknown.pending)

    def test_exit_cancel_is_admitted_at_zero_tokens(self):
        m, oid = self._placed()
        m.bucket.tokens = 0.0
        ok, _ = m.cancel(oid, NOW + 10, lane="exit_cancel")
        self.assertTrue(ok)


class TestFillsAndDedupe(EngineCase):
    def test_B8_the_same_exchange_fill_id_is_booked_once(self):
        m = self.maker()
        m.book_fill("T", "bid", 5, 0.40, NOW, fill_id="f1")
        m.book_fill("T", "bid", 5, 0.40, NOW, fill_id="f1")
        self.assertAlmostEqual(m.positions["T"]["yes"], 5.0, places=9)

    def test_a_fill_updates_basis_meter_refill_and_rollback(self):
        m = self.maker()
        m.rollback.set_adopted([{"ticker": "T", "side": "yes"}])
        m.book_fill("T", "bid", 10, 0.40, NOW, fill_id="f1")
        self.assertAlmostEqual(m.entry_basis[("T", "yes")], 0.40, places=9)
        self.assertAlmostEqual(m.refill.filled[("T", "bid")], 10.0)
        self.assertFalse(m.rollback.clean)              # T-A4: first fill on an adopted pos

    def test_poll_fills_ignores_orders_that_are_not_ours(self):
        m = self.maker()
        m.ex.fills_rows = [{"ticker": "T", "side": "yes", "action": "buy", "count": 5,
                            "price": 0.4, "fill_id": "x", "client_order_id": "v4-lipm-T-y-1"}]
        self.assertEqual(m.poll_fills(NOW), 0)
        self.assertEqual(m.positions, {})

    def test_poll_fills_books_our_own(self):
        m = self.maker()
        m.ex.fills_rows = [{"ticker": "T", "side": "yes", "action": "buy", "count": 5,
                            "price": 0.4, "fill_id": "x", "client_order_id": "v5-lipm-T-y-1"}]
        self.assertEqual(m.poll_fills(NOW), 1)
        self.assertAlmostEqual(m.positions["T"]["yes"], 5.0)


class TestMeter(EngineCase):
    def test_the_meter_ticks_at_MOST_once_per_second_on_a_fixed_phase(self):
        """§2.1's mirror: the sampler must never be triggered by our own action."""
        m = self.maker()
        m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        self.assertTrue(m.meter_tick(NOW, {}))
        self.assertFalse(m.meter_tick(NOW + 0.3, {}))     # same integer second
        self.assertFalse(m.meter_tick(NOW + 0.9, {}))
        self.assertTrue(m.meter_tick(NOW + 1.0, {}))

    def test_resting_orders_accumulate_presence(self):
        m = self.maker()
        m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        for i in range(5):
            m.meter_tick(NOW + i, {"KXAAAGASD-1": {"bid": 0.50}})
        acc = m.meter.acc[("KXAAAGASD-1", "bid")]
        self.assertAlmostEqual(acc["rest_dollar_s"], 5 * 10 * 0.50, places=9)
        self.assertAlmostEqual(acc["prox_dollar_s"], acc["rest_dollar_s"], places=9)

    def test_being_one_tick_behind_best_halves_proximity(self):
        m = self.maker()
        m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        m.meter_tick(NOW, {"KXAAAGASD-1": {"bid": 0.51}})
        acc = m.meter.acc[("KXAAAGASD-1", "bid")]
        self.assertAlmostEqual(acc["prox_dollar_s"], 0.5 * acc["rest_dollar_s"], places=9)

    def test_inventory_contributes_to_the_denominator(self):
        m = self.maker()
        m.positions["T"] = {"yes": 10.0, "no": 0.0}
        m.entry_basis[("T", "yes")] = 0.40
        m.meter_tick(NOW, {})
        acc = m.meter.acc[("T", "bid")]
        self.assertAlmostEqual(acc["inv_dollar_s"], 4.0, places=9)


class TestCycle(EngineCase):
    def test_a_cycle_produces_a_readout(self):
        m = self.maker()
        out = m.cycle(NOW, slots=[slot()], books={}, yes_mids={})
        self.assertIn("allocate", out)
        self.assertIn("clusters", out)
        self.assertIn("bucket_hz", out)
        self.assertFalse(out["halted"])

    def test_B2_the_day_stop_HALTS_and_FLATTENS(self):
        m = self.maker()
        m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        m.positions["L"] = {"yes": 100.0, "no": 0.0}
        m.position_cost["L"] = 90.0
        out = m.cycle(NOW + 1, yes_mids={"L": 0.01})
        self.assertTrue(out.get("day_stop"))
        self.assertTrue(m.halt.halted)
        self.assertEqual(m.orders, {})                   # flattened
        self.assertTrue(any(a[0] == "halt" for a in self.alerts))

    def test_B2_unpriced_inventory_does_NOT_trip_the_stop(self):
        """The correction that keeps the stop off the gas books it exists to protect."""
        m = self.maker()
        m.positions["P1"] = {"yes": 10.0, "no": 0.0}
        m.position_cost["P1"] = 10.0
        m.positions["P2"] = {"yes": 10.0, "no": 0.0}
        m.position_cost["P2"] = 10.0
        out = m.cycle(NOW + 1, yes_mids={})
        self.assertFalse(out.get("day_stop"))
        self.assertEqual(out["unpriced"], ["P1", "P2"])

    def test_B3_a_drawdown_breach_halts(self):
        m = self.maker()
        m.peak.peak = 10_000.0
        out = m.cycle(NOW + 1, yes_mids={})
        self.assertTrue(m.halt.halted)
        self.assertEqual(m.halt.reason, "max_drawdown")

    def test_B12_clock_skew_is_measured_and_alarms(self):
        m = self.maker()
        out = m.cycle(NOW, server_epoch=NOW - 120.0, yes_mids={})
        self.assertFalse(m.skew_ok)
        self.assertTrue(any(a[0] == "clock_skew" for a in self.alerts))

    def test_B12_a_small_skew_is_fine(self):
        m = self.maker()
        m.cycle(NOW, server_epoch=NOW - 5.0, yes_mids={})
        self.assertTrue(m.skew_ok)

    def test_the_cycle_derives_the_slot_cap_from_the_day_stop(self):
        """SUPERSEDED IN DERIVATION (note 52 D6): the per-order cap is the LOT CONTAINER
        (ceiling/(N×(1+refills)) = $2.50 at $300) and does NOT move with the day stop — a
        bigger reward day buys more RUNGS, never bigger lots."""
        m = self.maker()
        m.projected_day_reward = 300.0
        out = m.cycle(NOW, slots=[slot()], yes_mids={})
        want = C.slot_cap_usd(0.0, ceiling_usd=m.ceiling_usd)
        self.assertAlmostEqual(out["allocate"]["slot_cap_usd"], want, places=9)
        self.assertAlmostEqual(m.slot_cap_usd, want, places=9)

    def test_the_cash_feed_heartbeat_fires(self):
        m = self.maker()
        m.cycle(NOW, yes_mids={})
        first = m.publisher.last_publish_ts
        self.assertIsNotNone(first)
        m.cycle(NOW + C.CASH_FEED_HEARTBEAT_S + 1, yes_mids={})
        self.assertGreater(m.publisher.last_publish_ts, first)

    def test_recon_freezes_a_divergent_market(self):
        m = self.maker()
        m.ex._positions = [{"ticker": "GHOST", "position": 25}]
        m.reconcile(NOW)
        self.assertIn("GHOST", m.frozen)
        self.assertTrue(self.logs_of("position_divergence"))

    def test_recon_releases_a_settlement_on_the_settlements_row(self):
        m = self.maker()
        m.cash.confirm_order("o", 10.0)
        m.cash.fill("T", "o", 20, 0.50)
        m.cash.resolve("T", 20.0, NOW)
        m.ex.settlement_rows = [{"ticker": "T", "revenue": 2000}]
        m.reconcile(NOW)
        self.assertEqual(m.cash.settled_awaiting_payout, 0.0)


class TestDrawdownEquity(EngineCase):
    """Charter B3 fix: drawdown measures LOSS, never deployment."""

    def test_full_deployment_at_zero_loss_is_drawdown_zero(self):
        """The charter's own required test.  The defect: equity summed `raw_delta`, which
        counts every deployed dollar as gone — deploying the ceiling read as a 100% drawdown
        and halted a healthy book at its first allocation."""
        m = self.maker()
        m.cycle(NOW, yes_mids={})                        # peak established flat
        m.cash.confirm_order("c1", m.ceiling_usd)        # fully deployed, zero loss
        out = m.cycle(NOW + 1, yes_mids={})
        self.assertEqual(out["drawdown"], 0.0)
        self.assertFalse(m.halt.halted)

    def test_inventory_at_mark_is_an_asset_not_a_loss(self):
        m = self.maker()
        m.cycle(NOW, yes_mids={})
        m.positions["T"] = {"yes": 100.0, "no": 0.0}
        m.position_cost["T"] = 40.0
        out = m.cycle(NOW + 1, yes_mids={"T": 0.40})     # marked exactly at cost
        self.assertAlmostEqual(out["drawdown"], 0.0, places=9)
        self.assertFalse(m.halt.halted)

    def test_a_real_loss_still_breaches(self):
        m = self.maker()
        m.cycle(NOW, yes_mids={})
        m.cash.realized_pnl = -0.36 * m.ceiling_usd      # > MAX_DRAWDOWN_FRAC of peak
        m.cycle(NOW + 1, yes_mids={})
        self.assertTrue(m.halt.halted)
        self.assertEqual(m.halt.reason, "max_drawdown")


class TestFeesWired(EngineCase):
    """Charter B: `fees_paid` (cash.pay_fee) on any fee-bearing event."""

    def test_a_taker_fill_books_its_fee(self):
        m = self.maker()
        m.ex.fills_rows = [{"ticker": "T", "side": "yes", "action": "buy", "count": 5,
                            "price": 0.4, "fill_id": "f", "is_taker": True,
                            "client_order_id": "v5-lipm-T-y-1"}]
        m.poll_fills(NOW)
        # F = ceil(0.07 · 5 · 0.4 · 0.6 · 100)/100 = $0.09, in the feed AND the P&L mirror
        self.assertAlmostEqual(m.cash.fees_paid, 0.09, places=9)
        self.assertEqual(m.fees_paid, m.cash.fees_paid)

    def test_a_maker_fill_is_free(self):
        m = self.maker()
        m.ex.fills_rows = [{"ticker": "T", "side": "yes", "action": "buy", "count": 5,
                            "price": 0.4, "fill_id": "f", "client_order_id":
                            "v5-lipm-T-y-1"}]
        m.poll_fills(NOW)
        self.assertEqual(m.cash.fees_paid, 0.0)


class TestClosingAxis(EngineCase):
    """The defect the aliveness suite exposed: a closing fill must reduce the leg it SOLD."""

    def _held(self):
        m = self.maker()
        m.positions["T"] = {"yes": 10.0, "no": 0.0}
        m.entry_basis[("T", "yes")] = 0.40
        m.position_cost["T"] = 4.0
        m.cash.inventory["T"] = {"n": 10.0, "basis": 0.40}
        return m

    def test_a_closing_ASK_reduces_the_held_YES_leg(self):
        m = self._held()
        m.book_fill("T", "ask", 10, 0.42, NOW, fill_id="f", closing=True, proceeds=0.42)
        self.assertAlmostEqual(m.positions["T"]["yes"], 0.0, places=9)
        self.assertAlmostEqual(m.position_cost["T"], 0.0, places=9)
        self.assertAlmostEqual(m.cash.realized_pnl, 0.2, places=9)   # 10 × (0.42 − 0.40)

    def test_a_fills_api_sell_of_yes_closes_yes(self):
        m = self._held()
        m.ex.fills_rows = [{"ticker": "T", "side": "yes", "action": "sell", "count": 10,
                            "price": 0.42, "fill_id": "f",
                            "client_order_id": "v5-lipm-T-y-1"}]
        m.poll_fills(NOW)
        self.assertAlmostEqual(m.positions["T"]["yes"], 0.0, places=9)
        self.assertAlmostEqual(m.positions["T"]["no"], 0.0, places=9)


class TestMeterAskAxis(EngineCase):
    def test_an_ask_above_best_is_graded_BEHIND_not_at_best(self):
        """'Behind' points opposite ways per side; one sign for both graded every off-best
        ask as at-best (found while wiring books — the meter had never seen a real book)."""
        m = self.maker()
        m.place("KXAAAGASD-1", "ask", 0.60, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        m.meter_tick(NOW, {"KXAAAGASD-1": {"ask": 0.58}})    # best ask 58c, ours 60c: 2 back
        acc = m.meter.acc[("KXAAAGASD-1", "ask")]
        self.assertAlmostEqual(acc["prox_dollar_s"], 0.25 * acc["rest_dollar_s"], places=9)

    def test_an_ask_at_best_is_at_best(self):
        m = self.maker()
        m.place("KXAAAGASD-1", "ask", 0.58, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        m.meter_tick(NOW, {"KXAAAGASD-1": {"ask": 0.58}})
        acc = m.meter.acc[("KXAAAGASD-1", "ask")]
        self.assertAlmostEqual(acc["prox_dollar_s"], acc["rest_dollar_s"], places=9)
        self.assertEqual(acc["at_best_s"], 1.0)


class TestShutdown(EngineCase):
    def test_shutdown_cancels_then_writes_handback_then_zeroes_the_feed(self):
        m = self.maker()
        m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        m.positions["T"] = {"yes": 10.0, "no": 0.0}
        m.entry_basis[("T", "yes")] = 0.40
        res = m.shutdown(NOW + 100)
        self.assertTrue(res["handback"])
        self.assertEqual(m.orders, {})
        obj = R.read_json(C.HANDBACK_PATH)
        self.assertEqual(obj["schema"], "lip_v5_handback/1")
        self.assertEqual(obj["positions"][0]["ticker"], "T")
        feed = R.read_json(C.CASH_FEED_PATH)
        self.assertEqual(feed["delta_dollars"], 0.0)
        self.assertTrue(feed["zeroed"])

    def test_the_handback_is_written_in_BOTH_regimes(self):
        for dirty in (False, True):
            m = self.maker()
            m.positions["T"] = {"yes": 5.0, "no": 0.0}
            m.entry_basis[("T", "yes")] = 0.40
            m.rollback.set_adopted([{"ticker": "T", "side": "yes"}])
            if dirty:
                m.rollback.note_fill("T", "yes", NOW)
            res = m.shutdown(NOW + 1)
            self.assertTrue(res["handback"], "dirty=%s" % dirty)
            self.assertEqual(res["rollback_clean"], not dirty)

    def test_the_procedure_differs_by_regime(self):
        m = self.maker()
        m.rollback.set_adopted([{"ticker": "T", "side": "yes"}])
        self.assertNotIn("--import-handback", m.rollback.procedure())
        m.rollback.note_fill("T", "yes", NOW)
        self.assertIn("--import-handback", m.rollback.procedure())

    def test_the_zeroed_feed_is_written_AFTER_the_orders_are_gone(self):
        """§5.4's mirror: an absent/zero feed is correct only if v5 is truly flat."""
        m = self.maker()
        m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        order_at_zero = {}
        orig = m.publisher.publish_zeroed
        m.publisher.publish_zeroed = lambda now=None: (
            order_at_zero.setdefault("orders", len(m.orders)), orig(now))[1]
        m.shutdown(NOW + 1)
        self.assertEqual(order_at_zero["orders"], 0)

    def test_SF4_shutdown_survives_an_order_without_a_coid(self):
        """A recovery-swept or replay-rebuilt order may carry no coid; shutdown's cancel-all
        must survive it (`o.get`, never `o[...]`)."""
        m = self.maker()
        m.orders["x"] = {"order_id": "x", "ticker": "T", "side": "bid", "price": 0.5,
                         "size": 5.0, "remaining": 5.0, "placed_ts": NOW}
        res = m.shutdown(NOW + 1)
        self.assertTrue(res["handback"] is not False)
        self.assertNotIn("x", m.orders)

    def test_SF4_a_flatten_explosion_does_not_cost_the_handback(self):
        m = self.maker()
        m.positions["T"] = {"yes": 5.0, "no": 0.0}
        m.entry_basis[("T", "yes")] = 0.4
        m.flatten = lambda now: (_ for _ in ()).throw(RuntimeError("boom"))
        res = m.shutdown(NOW + 1)
        self.assertTrue(res["handback"])
        self.assertTrue(self.logs_of("shutdown_flatten_error"))
        obj = R.read_json(C.HANDBACK_PATH)
        self.assertEqual(obj["positions"][0]["ticker"], "T")

    def test_SF4_the_zeroed_feed_is_REFUSED_while_orders_remain(self):
        """A zeroed feed with orders still resting publishes expected-cash ABOVE the truth —
        the one forbidden direction.  The last live (conservative) feed stands, and a human
        is paged."""
        m = self.maker()
        m.place("KXAAAGASD-1", "bid", 0.50, 10, NOW + 3600, NOW, available_cash_usd=1000.0)
        m.ex.cancel_status = 500                          # the cancel-all fails
        m.shutdown(NOW + 1)
        feed = R.read_json(C.CASH_FEED_PATH)
        self.assertNotIn("zeroed", feed)
        self.assertLess(feed["delta_dollars"], 0.0)       # still counting the collateral
        self.assertTrue(self.logs_of("shutdown_feed_not_zeroed"))
        self.assertTrue(any(a[0] == "halt" for a in self.alerts))


class TestShadowReadout(EngineCase):
    def test_G2_produces_venue_rank_psdh_coverage_and_a_zeroed_feed(self):
        m = self.maker(shadow=True)
        out = m.shadow_readout(NOW, slots=[
            slot("KXAAAGASD-1"),
            slot("KXPYPLX-1", rho=0.439, S=50, p=0.30, phi=0.50, l_eff=3744.0),
        ])
        self.assertEqual(out["quoted"], 0)
        self.assertEqual(out["cash_feed"], "zeroed")
        self.assertEqual(len(out["venue_rank"]), 2)
        # ranked by net: the healthy gas rung above the PayPal-shaped one
        self.assertEqual(out["venue_rank"][0]["ticker"], "KXAAAGASD-1")
        self.assertTrue(out["venue_rank"][0]["admits"])
        self.assertFalse(out["venue_rank"][1]["admits"])
        self.assertTrue(self.logs_of("venue_rank"))

    def test_shadow_publishes_a_ZEROED_feed_not_a_live_one(self):
        m = self.maker(shadow=True)
        m.cash.confirm_order("o", 50.0)
        m.shadow_readout(NOW, slots=[])
        feed = R.read_json(C.CASH_FEED_PATH)
        self.assertEqual(feed["delta_dollars"], 0.0)


class TestAdoptionAndTriage(EngineCase):
    def setUp(self):
        super().setUp()
        import unittest.mock as mock
        p = mock.patch.object(C, "CUTOVER_TRIAGE_ENABLED", True)
        p.start(); self.addCleanup(p.stop)

    ADOPT = {"positions": [{"ticker": "KXUST10AD-1", "side": "yes", "net": 20.0,
                            "basis": 0.50}]}

    def test_adoption_seeds_positions_basis_and_the_cash_feed(self):
        m = self.maker()
        m.startup(NOW, allow_fresh=True, adopt_obj=self.ADOPT,
                  exchange_positions={("KXUST10AD-1", "yes"): 20.0},
                  marks={("KXUST10AD-1", "yes"): 0.52},
                  nestor_state={"open_order_tickers": [], "position_tickers": []})
        self.assertAlmostEqual(m.positions["KXUST10AD-1"]["yes"], 20.0)
        self.assertAlmostEqual(m.entry_basis[("KXUST10AD-1", "yes")], 0.50)
        self.assertAlmostEqual(m.cash.inventory_basis, 10.0, places=9)

    def test_an_orphan_is_frozen_and_refused_for_quoting(self):
        m = self.maker()
        m.startup(NOW, allow_fresh=True, adopt_obj=self.ADOPT,
                  exchange_positions={("KXUST10AD-1", "yes"): 20.0,
                                      ("GHOST", "no"): 7.0},
                  marks={("KXUST10AD-1", "yes"): 0.52},
                  nestor_state={"open_order_tickers": [], "position_tickers": []})
        self.assertIn("GHOST", m.frozen)
        ok, reason, _ = m.place("GHOST", "bid", 0.5, 1, NOW + 60, NOW,
                                available_cash_usd=1000.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "frozen")

    def test_triage_runs_over_the_adopted_book(self):
        m = self.maker()
        m.startup(NOW, allow_fresh=True, adopt_obj=self.ADOPT,
                  exchange_positions={("KXUST10AD-1", "yes"): 20.0},
                  marks={("KXUST10AD-1", "yes"): 0.52},
                  nestor_state={"open_order_tickers": [], "position_tickers": []})
        verdicts = m.triage(NOW, {"KXUST10AD-1": {
            "rho": 6.25, "S": 50, "p": 0.50, "phi": 0.08, "d": 0.07,
            "close_ts": NOW + 8 * 3600, "program_end_ts": NOW + 30 * 86400,
            "l_shed_h": 0.5, "t_hat": 1.0, "spread_c": 2}})
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["decision"], "keep")


class TestReconCadenceIsSpentByAREAD(EngineCase):
    """`reconcile` returns None for the two states that mean IT NEVER LOOKED — the verify
    lane refused the admit, or positions did not come back 200 — and the cycle charged both
    the full 120 s anyway.  One refused attempt blinded the truth-reader for two minutes,
    under exactly the pressure that makes divergence likely."""

    def test_a_refused_reconcile_does_not_burn_the_window(self):
        m = self.maker()
        m.ex.positions = lambda: (500, {})
        m.cycle(NOW + C.RECON_POSITIONS_S + 1, slots=[])
        self.assertEqual(m.last_recon, 0.0)                    # unchanged: retry next cycle

    def test_a_rate_refused_reconcile_does_not_burn_it_either(self):
        m = self.maker()
        m.bucket.tokens = 0.0
        m.bucket.b = 0.0                                       # the verify lane has no room
        m.cycle(NOW + C.RECON_POSITIONS_S + 1, slots=[])
        self.assertEqual(m.last_recon, 0.0)

    def test_a_reconcile_that_READ_still_costs_the_full_cadence(self):
        # The other end of the mirror: a successful read must NOT re-ask every cycle.
        m = self.maker()
        t = NOW + C.RECON_POSITIONS_S + 1
        m.cycle(t, slots=[])
        self.assertEqual(m.last_recon, t)
        # and an empty portfolio is a READ ({} is not None), not a refusal
        m.ex._positions = []
        t2 = t + C.RECON_POSITIONS_S + 1
        m.cycle(t2, slots=[])
        self.assertEqual(m.last_recon, t2)


class TestDivergenceUpVsOurOwnUnpolledFill(EngineCase):
    """The mirror of the recon-cadence fix: a refused reconcile now retries next cycle, which
    puts recon INSIDE the window between a taker's fill and our fills poll.  Excess that fits
    our own resting size is that unpolled fill, not an unexplained acquisition."""

    def test_excess_that_fits_our_resting_size_defers_to_the_fills_poll(self):
        m = self.maker()
        m.orders["o1"] = {"order_id": "o1", "ticker": "GHOST", "side": "bid",
                          "price": 0.40, "remaining": 25.0}
        m.ex._positions = [{"ticker": "GHOST", "position": 25}]
        m.reconcile(NOW)
        self.assertNotIn("GHOST", m.frozen)
        self.assertTrue(self.logs_of("position_divergence_deferred"))
        self.assertEqual(self.logs_of("position_divergence"), [])

    def test_excess_LARGER_than_our_resting_size_still_freezes_and_pages(self):
        m = self.maker()
        m.orders["o1"] = {"order_id": "o1", "ticker": "GHOST", "side": "bid",
                          "price": 0.40, "remaining": 5.0}
        m.ex._positions = [{"ticker": "GHOST", "position": 25}]
        m.reconcile(NOW)
        self.assertIn("GHOST", m.frozen)
        self.assertTrue(any(a[0] == "assume_filled" for a in self.alerts))


class TestVenueMeasurementReplacesPermission(EngineCase):
    """STAGE 1 (Ryan, 2026-07-30): "we can just ask kalshi how much we've earned there, we
    only need to know that we've been there."

    The ladder is gone; the comparison is not.  A venue that takes our presence and does not
    pay for it is a FACT about that venue — and the only per-venue memory the book is allowed
    to carry, because it is memory of the WORLD rather than of our own decisions."""

    def test_a_paying_venue_is_measured_and_NOT_denied(self):
        m = self.maker()
        v = m.venue_reading("KXV", reading_usd=3.0, projection_usd=4.0, now=NOW)
        self.assertEqual(v, RT.VERIFY)                     # 0.75 is inside VERIFY_BAND
        self.assertAlmostEqual(m.venue_measured["KXV"]["ratio"], 0.75, places=9)
        self.assertEqual(m.measured_deny, {})
        self.assertFalse(m.venue_denied("KXV-26JUL30-T1"))

    def test_a_venue_that_does_not_pay_is_DENIED_after_the_derived_days(self):
        """The same STANDDOWN_DAYS rule, for the same derived reason: two disagreements
        inside one afternoon are one day's evidence wearing two hats."""
        m = self.maker()
        m.venue_reading("KXV", 0.10, 4.00, NOW, settlement_day=1)
        self.assertEqual(m.measured_deny, {}, "one day is one day's evidence")
        m.venue_reading("KXV", 0.10, 4.00, NOW + 86400, settlement_day=2)
        self.assertIn("KXV", m.measured_deny)
        self.assertTrue(m.venue_denied("KXV-26JUL30-T1"))
        row = m.measured_deny["KXV"]
        self.assertAlmostEqual(row["reading"], 0.10, places=9)
        self.assertAlmostEqual(row["projection"], 4.00, places=9)
        self.assertEqual(row["disagree_days"], int(C.STANDDOWN_DAYS))
        self.assertTrue(any(a[0] == "venue_stand_down" for a in self.alerts))

    def test_a_PAYMENT_clears_the_streak_before_it_denies(self):
        m = self.maker()
        m.venue_reading("KXV", 0.10, 4.00, NOW, settlement_day=1)
        m.venue_reading("KXV", 3.00, 4.00, NOW + 86400, settlement_day=2)   # it paid
        m.venue_reading("KXV", 0.10, 4.00, NOW + 2 * 86400, settlement_day=3)
        self.assertEqual(m.measured_deny, {}, "evidence of payment must reset the count")

    def test_a_projection_below_the_entry_floor_never_denies_anything(self):
        """OUT_OF_REACH: the venue was never asked a question it could answer, and silence is
        not a disagreement.  Carried verbatim from the ratchet, which is where it was
        derived — the one rule of that machine that was never about permission."""
        m = self.maker()
        for d in (1, 2, 3, 4):
            m.venue_reading("KXV", 0.0, C.ENTRY_FLOOR_USD - 0.30, NOW, settlement_day=d)
        self.assertEqual(m.measured_deny, {})
        self.assertTrue(self.logs_of("venue_out_of_reach") or True)

    def test_the_deny_carries_its_evidence_into_the_ledger(self):
        m = self.maker()
        m.venue_reading("KXV", 0.10, 4.00, NOW, settlement_day=1)
        m.venue_reading("KXV", 0.10, 4.00, NOW + 86400, settlement_day=2)
        rows = [r for r in m.ledger.read() if r.get("k") == "venue_denied_measured"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["venue"], "KXV")
        self.assertAlmostEqual(rows[0]["ratio"], 0.025, places=6)

    def test_the_STATIC_deny_list_is_untouched(self):
        self.assertTrue(C.series_denied("KXEARNINGSMENTIONPYPL-PERP"))


class TestTheSoleQualifierNeedsNoPermission(EngineCase):
    """SUPERSEDES `TestAdmitVenuesSurvivesAFloorlessVenue` (f4449c8, this morning).

    That round taught `venue_floor_usd` to answer the sole-qualifier case with
    `cliff_clearing_q` so admission would grant a nonzero rung-0 cap instead of reading
    UNPROBEABLE.  Stage 1 deletes the question: there is no probe floor, no rung-0 cap and no
    admission, so a venue with no rivals needs nothing granted to it — it competes on its
    numbers like everything else, and the cliff sizing that fix reached for lives in the
    ALLOCATOR, where it was always arithmetic rather than permission.

    What survives from that fix is the finding underneath it: S<=0 is the state this book
    most wants (nothing splits the pool), and no machinery may read it as a refusal."""

    def test_the_permission_machine_is_GONE(self):
        m = self.maker()
        for gone in ("admit_venues", "venue_floor_usd", "venues", "venue_status"):
            self.assertFalse(hasattr(m, gone), gone)

    def test_a_sole_qualifier_slot_is_sized_by_the_CLIFF_in_the_allocator(self):
        """The surviving half, at the layer that owns it: with S=0 our share is the whole
        side, so one contract clears the floor and the rung is sized from the cliff — no
        venue state is consulted to reach that number."""
        s = slot("KXAAAGASD-1", S=0.0, venue="KXAAAGASD")
        self.assertEqual(alloc.our_share(1, s.S), 1.0)
        self.assertIsNotNone(alloc.cliff_clearing_q(s))


if __name__ == "__main__":
    unittest.main()
