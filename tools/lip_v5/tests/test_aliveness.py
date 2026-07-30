"""THE ALIVENESS TESTS (briefs/implementor.md; finish-round charter D).

The finding this file exists to prevent from returning: the assembled system computed
allocations and DROPPED them — `Maker.place()` had zero call sites, every module was tested
pure, the loop ran, and no order could ever reach the exchange.  "Modules tested pure + a
loop that runs" is NOT proof of life; these tests assert the system's AFFIRMATIVE PURPOSE
occurs through the assembled loop:

    * a FakeExchange plus one good venue  →  orders APPEAR within N cycles;
    * a failing adopted position          →  a maker-shed order APPEARS;
    * a completed shed                    →  feeds the l_shed measurement.

Every assertion here is on what the EXCHANGE saw, not on internal state — the seam between
sections is the classic home of the missing action.
"""

import unittest

from .. import config as C, exchange as X, ratchet as RT, runner as RUN, runtime as R
from .base import LipTestCase
from .test_engine import EngineCase
from .test_runner import NOW, program_body

ALIVE_TICKER = "KXAAAGASD-26JUL29-T4.12"
SHED_TICKER = "KXUSTALIVE-26JUL29-T4.12"


def cheap_book():
    """A qualifying cheap side: the gas geometry, the venue (★) ADMITS on spec §0.4's own
    numbers (gross ≈ 0.12/h per $, carry and drift negligible at φ_seed_cheap)."""
    return {"orderbook": {"orderbook_fp": {
        "yes_dollars": [["0.06", "1200"]], "no_dollars": [["0.93", "1200"]]}}}


def treasury_book():
    """A mid-priced book whose slot FAILS (★) at the cold-start prior (T̂ = 0.5): gross×T̂ ≈
    0.003 < carry+drift ≈ 0.022 — the venue v5 would never have entered."""
    return {"orderbook": {"orderbook_fp": {
        "yes_dollars": [["0.40", "1200"]], "no_dollars": [["0.58", "1200"]]}}}


class AliveExchange(X.FakeExchange):
    def __init__(self, programs, books, **kw):
        kw.setdefault("balance_cents", 1_000_000)
        super(AliveExchange, self).__init__(books=books, **kw)
        self._programs = programs

    def programs(self, cursor=None):
        return 200, self._programs


NESTOR = {"open_order_tickers": [], "position_tickers": []}


class TestOrdersAppear(EngineCase):
    """FakeExchange + one good venue → orders appear.  The whole chain, no shortcuts:
    scan → classify → slots → venue admission → r*/ALLOCATE → forfeit gate → REQUOTE →
    place() → the wire."""

    def _runner(self):
        ex = AliveExchange(program_body(tickers=(ALIVE_TICKER,)),
                           {ALIVE_TICKER: cheap_book()})
        ex.market_closes[ALIVE_TICKER] = NOW + 16 * 3600  # the gas shape: settles at its
                                                          # program end, not the +24h default
        m = self.maker(ex=ex)
        return RUN.Runner(m, sleep=lambda _s: None)

    def test_orders_appear_within_three_cycles(self):
        r = self._runner()
        ok, refusals = r.init(NOW, nestor_state=NESTOR)
        self.assertTrue(ok, refusals)
        for i in range(3):
            out = r.iteration(NOW + 1 + i)
        self.assertGreater(len(r.m.ex.placed), 0, "NO ORDER REACHED THE EXCHANGE")
        self.assertGreater(len(r.m.ex.resting), 0, "nothing RESTING on the exchange")
        self.assertGreater(out["requote"]["placed"] + len(r.m.orders), 0)

    def test_the_resting_order_is_true_to_the_allocation(self):
        r = self._runner()
        r.init(NOW, nestor_state=NESTOR)
        out = r.iteration(NOW + 1)
        body = r.m.ex.placed[0]
        self.assertEqual(body["ticker"], ALIVE_TICKER)
        self.assertEqual(body["side"], "bid")
        self.assertAlmostEqual(float(body["price"]), 0.06, places=6)   # joins the best
        alloc_q = out["alloc"][(ALIVE_TICKER, "bid")]
        self.assertGreater(alloc_q, 0)
        self.assertAlmostEqual(float(body["count"]), float(alloc_q), places=6)

    def test_the_order_carries_the_close_backstop_and_v5_identity(self):
        r = self._runner()
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        body = r.m.ex.placed[0]
        self.assertTrue(body["client_order_id"].startswith("v5-"))
        self.assertEqual(body["self_trade_prevention_type"], "taker_at_cross")
        # close_ts falls back to the program end here (FakeExchange serves no market close);
        # the backstop is close − 240 s either way.
        self.assertEqual(body["expiration_ts"], int(NOW + 16 * 3600 - C.CLOSE_MARGIN_S))

    def test_the_cash_feed_counted_the_collateral_before_the_wire(self):
        r = self._runner()
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        self.assertGreater(r.m.cash.resting_collateral, 0.0)
        self.assertLess(r.m.cash.raw_delta, 0.0)          # published below truth, never above

    def test_the_probe_is_floor_clearing_and_venue_capped(self):
        """G3's read-out line: no venue funded below its floor_q, none above its rung-0 cap."""
        r = self._runner()
        r.init(NOW, nestor_state=NESTOR)
        out = r.iteration(NOW + 1)
        q = out["alloc"][(ALIVE_TICKER, "bid")]
        spent = q * 0.06
        self.assertGreaterEqual(q, 1)
        self.assertLessEqual(spent, 0.20 * r.m.ceiling_usd + 1e-9)   # unverified bound
        st = r.m.venues["KXAAAGASD"]
        self.assertEqual(st.rung, 0)
        self.assertLessEqual(spent, st.rung0_cap_usd + 1e-9)

    def test_a_dying_allocation_cancels_the_resting_order(self):
        """The diff runs BOTH ways: when the target drops to zero the quote leaves."""
        r = self._runner()
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        self.assertTrue(r.m.orders)
        # Freeze the market: it vanishes from the slot table; its order must be cancelled
        # rather than left to rot (the requoter owns the whole lifecycle).
        r.m.frozen.add(ALIVE_TICKER)
        # advance past MIN_RESTING_LIFE so the cancel is not a P1-suppressed dodge
        out = r.iteration(NOW + C.MIN_RESTING_LIFE_S + 2)
        self.assertEqual(out["slots"], 0)
        # a frozen market keeps its (still-live) order visible; nothing new placed
        self.assertEqual(len(r.m.ex.placed), 1)


class TestReplenish(EngineCase):
    """THE REPLENISH FIXTURE (second charter amendment, Ryan's complaint 2a).  The v4 tape:
    enter a rung, accrue ~7¢, get filled, NEVER REQUOTE — capital rides to settlement
    earning zero while the rung's accrual dies below the $1.00 cliff.  This fixture fails
    on any requoter that goes silent after the first fill."""

    def _runner(self):
        ex = AliveExchange(program_body(tickers=(ALIVE_TICKER,)),
                           {ALIVE_TICKER: cheap_book()})
        ex.market_closes[ALIVE_TICKER] = NOW + 16 * 3600  # the gas shape: settles at its
                                                          # program end, not the +24h default
        m = self.maker(ex=ex)
        return RUN.Runner(m, sleep=lambda _s: None)

    def _filled(self, r):
        """Enter, then the taker takes the WHOLE order."""
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        self.assertEqual(len(r.m.ex.placed), 1)
        oid = list(r.m.orders)[0]
        n = r.m.orders[oid]["remaining"]
        # a filled probe is verified evidence in this fixture's world: promote the venue so
        # the replenish is not the (correct) unverified-probe refusal
        st = r.m.venues["KXAAAGASD"]
        st.verified = True
        st.rung = 2
        r.m.ex.resting.pop(oid, None)
        r.m.book_fill(ALIVE_TICKER, "bid", n, 0.02, NOW + 2, fill_id="fill-1",
                      order_id=oid)
        return r, n

    def test_the_requoter_does_NOT_go_silent_after_the_fill(self):
        r, n = self._filled(self._runner())
        for i in range(3):                    # inside the post-fill cooldown: waits
            r.iteration(NOW + 3 + i)
        for i in range(3):                    # past it: the replenish returns
            r.iteration(NOW + C.POST_FILL_COOLDOWN_S + 4 + i)
        self.assertGreater(len(r.m.ex.placed), 1, "SILENT AFTER THE FIRST FILL — v4's tape")
        self.assertTrue(r.m.orders, "nothing resting after the fill: presence died")
        self.assertTrue(r.m.ex.resting, "the exchange book is empty: accrual is dying")

    def test_the_replenish_is_sized_at_net_cap_minus_held(self):
        """v1 §8.1 binds NET exposure: held + resting stays inside the venue cap — the
        replenish shrinks as inventory builds instead of doubling exposure (which the
        cluster cap would then refuse, silencing the requoter through a guard)."""
        r, n = self._filled(self._runner())
        r.iteration(NOW + 3)
        r.iteration(NOW + C.POST_FILL_COOLDOWN_S + 4)     # past the post-fill cooldown
        st = r.m.venues["KXAAAGASD"]
        vcap = st.cap_usd(0.25 * r.m.ceiling_usd, r.m.ceiling_usd)
        resting = sum(o["remaining"] * 0.02 for o in r.m.orders.values())
        held = abs(r.m.net_position(ALIVE_TICKER)) * 0.02
        self.assertGreater(resting, 0.0)
        self.assertLessEqual(held + resting, vcap + 0.03)   # one contract of rounding
    def test_the_replenish_respects_the_turnover_bound(self):
        """'Within refill-cap bounds': a slot churned past 4 turnovers of its cap is a FLOW
        MAGNET (B9) — the requoter goes silent there BY DESIGN, with the refusal named."""
        r, n = self._filled(self._runner())
        r.m.refill.filled[(ALIVE_TICKER, "bid")] = 1e9      # turnovers exhausted
        before = len(r.m.ex.placed)
        r.iteration(NOW + C.POST_FILL_COOLDOWN_S + 3)       # past the cooldown: B9 refuses
        self.assertEqual(len(r.m.ex.placed), before)
        refused = [x for x in self.logs_of("place_refused")
                   if x.get("refused_by") == "refill_cap"]
        self.assertTrue(refused, "the silence must carry its reason")


class TestAccrualIntegration(EngineCase):
    """Second amendment (b) plumbing: accrual integrates over allocated presence, feeds the
    cash feed's positive side, persists as money rows, and survives restart — the cliff
    decision is only as good as the A it remembers."""

    def _runner(self):
        ex = AliveExchange(program_body(tickers=(ALIVE_TICKER,)),
                           {ALIVE_TICKER: cheap_book()})
        ex.market_closes[ALIVE_TICKER] = NOW + 16 * 3600  # the gas shape: settles at its
                                                          # program end, not the +24h default
        m = self.maker(ex=ex)
        return RUN.Runner(m, sleep=lambda _s: None)

    def test_accrual_integrates_and_widens_the_feeds_positive_side(self):
        r = self._runner()
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        r.iteration(NOW + 2)
        self.assertGreater(r.m.accrued.get("prog-1", 0.0), 0.0)
        self.assertGreater(r.m.cash.rewards_accrued_unpaid, 0.0)
        self.assertAlmostEqual(r.m.cash.rewards_accrued_unpaid,
                               sum(r.m.accrued.values()), places=9)

    def test_accrued_value_survives_restart(self):
        r = self._runner()
        r.init(NOW, nestor_state=NESTOR)
        r.iteration(NOW + 1)
        r.iteration(NOW + 2)
        r.iteration(NOW + 2 + C.ACCRUAL_WRITE_S + 1)      # the persistence cadence elapses
        val = r.m.accrued["prog-1"]
        self.assertTrue([x for x in r.m.ledger.read()
                         if (x.get("k")) == "accrual"])
        r2 = self._runner()
        r2.init(NOW + 10, nestor_state=NESTOR)
        self.assertAlmostEqual(r2.m.accrued["prog-1"], round(val, 6), places=6)

    def test_slots_carry_the_accrued_memory(self):
        r = self._runner()
        r.init(NOW, nestor_state=NESTOR)
        r.m.accrued["prog-1"] = 0.70
        r.iteration(NOW + 1)
        s = [s for s in r.slots if s.side == "bid"][0]
        self.assertAlmostEqual(s.accrued, 0.70, places=6)


GRAB_TICKER = "KXGRABALIVE-26JUL29-T4.12"


def thin_book():
    """A YES side short of target (10 of 1000) with a healthy NO side: the §4.5 case where
    ALLOCATE is right about size and wrong about entry — qualification is a DISCRETE
    precondition, created by the land grab."""
    return {"orderbook": {"orderbook_fp": {
        "yes_dollars": [["0.06", "10"]], "no_dollars": [["0.50", "1200"]]}}}


class TestLandGrabAppears(EngineCase):
    """The qualification pass reaches the wire: a side short of target gets its land-grab
    order, at the cheapest legal price on the side being created, inside the §1.4 rung-0
    venue cap."""

    def _runner(self):
        # reward sized so the venue's floor_q (from the qualifying NO side) covers the grab:
        # rho ≈ 15.4/h ⇒ floor ≈ $10 ≥ the $9.90 grab.
        ex = AliveExchange(program_body(series="KXGRABALIVE", tickers=(GRAB_TICKER,),
                                        reward=2_620_000),
                           {GRAB_TICKER: thin_book()})
        m = self.maker(ex=ex)
        return RUN.Runner(m, sleep=lambda _s: None)

    def test_NO_land_grab_order_reaches_the_exchange(self):
        """REPLACES test_the_land_grab_order_appears (FREE_RIDE_ONLY armed 2026-07-29).

        This test used to assert that a 990-contract order at 1c REACHED THE WIRE and to fail
        loudly if it did not.  It is now inverted: the 1c path is the single most direct
        surviving link between the live code and the measured loss, and size beyond the
        target-size walk scores zero, so nothing should arrive at that price at all."""
        r = self._runner()
        ok, refusals = r.init(NOW, nestor_state=NESTOR)
        self.assertTrue(ok, refusals)
        for i in range(2):
            r.iteration(NOW + 1 + i)
        grabs = [b for b in r.m.ex.placed
                 if abs(float(b["price"]) - 0.01) < 1e-9
                 or abs(float(b["price"]) - 0.99) < 1e-9]
        self.assertEqual(grabs, [], "a 1c/99c land-grab order reached the exchange")
        self.assertFalse(self.logs_of("land_grab"))
        self.assertTrue(self.logs_of("free_ride_refused"),
                        "the refusal must be INSTRUMENTED, not silent")

    def test_the_grab_respects_the_venue_rung0_cap(self):
        """Under the note-52 lot container ($2.50) this venue's only quotable side carries a
        floor-clearing lot of ~$13 (deep NO side, ~93c) — it does not fit, so the venue reads
        UNPROBEABLE and nothing is spent on it.  That is D6 refusing a rung it cannot reserve
        for, one stage earlier than the rung0 cap used to."""
        r = self._runner()
        r.init(NOW, nestor_state=NESTOR)
        out = r.iteration(NOW + 1)
        st = r.m.venues.get("KXGRABALIVE")
        if st is None:
            self.assertEqual(r.m.venue_status.get("KXGRABALIVE"), RT.UNPROBEABLE)
            self.assertEqual(out["allocate"]["spent"], 0.0)
        else:
            self.assertLessEqual(out["allocate"]["spent"], st.rung0_cap_usd + 1e-6)


class TestShedAppears(EngineCase):
    """A failing adopted position → a maker-shed order appears, fully closing, never
    crossing, and its completion feeds l_shed."""

    ADOPT = {"positions": [{"ticker": SHED_TICKER, "side": "yes", "net": 20.0,
                            "basis": 0.40}]}

    def _runner(self):
        ex = AliveExchange(program_body(series="KXUSTALIVE", tickers=(SHED_TICKER,)),
                           {SHED_TICKER: treasury_book()},
                           positions=[{"ticker": SHED_TICKER, "position": 20}])
        m = self.maker(ex=ex)
        return RUN.Runner(m, sleep=lambda _s: None)

    def _armed(self):
        r = self._runner()
        ok, refusals = r.init(
            NOW, allow_fresh=True, adopt_obj=self.ADOPT,
            exchange_positions={(SHED_TICKER, "yes"): 20.0},
            marks={(SHED_TICKER, "yes"): 0.41}, nestor_state=NESTOR)
        self.assertTrue(ok, refusals)
        return r

    def test_a_shed_order_appears(self):
        r = self._armed()
        for i in range(3):
            r.iteration(NOW + 1 + i)
        sheds = [b for b in r.m.ex.placed if b["side"] == "ask"]
        self.assertTrue(sheds, "NO SHED ORDER REACHED THE EXCHANGE")
        body = sheds[0]
        self.assertEqual(body["ticker"], SHED_TICKER)
        self.assertAlmostEqual(float(body["count"]), 20.0, places=6)   # exactly |net| (C8)
        # joins the ask queue at 1 − no_bid = 0.42: NEVER crosses the 0.40 bid (G6 off)
        self.assertAlmostEqual(float(body["price"]), 0.42, places=6)
        self.assertGreater(float(body["price"]), 0.40)
        self.assertIn(SHED_TICKER, r.m.triage_shed)

    def test_the_shed_holds_no_fresh_collateral(self):
        r = self._armed()
        r.iteration(NOW + 1)
        # inventory basis is counted; the fully-closing shed adds NO resting collateral
        self.assertAlmostEqual(r.m.cash.resting_collateral, 0.0, places=9)
        self.assertAlmostEqual(r.m.cash.inventory_basis, 8.0, places=9)

    def test_a_completed_shed_feeds_l_shed(self):
        r = self._armed()
        r.iteration(NOW + 1)
        oid = list(r.m.orders)[0]
        # the taker arrives: the shed fills completely
        r.m.book_fill(SHED_TICKER, "ask", 20, 0.42, NOW + 3600, fill_id="shed-fill",
                      closing=True, order_id=oid, proceeds=0.42)
        r.m.ex._positions = [{"ticker": SHED_TICKER, "position": 0}]
        r.iteration(NOW + 3601)
        key = (SHED_TICKER, "yes")
        self.assertIn(key, r.m.shed_completed_h)
        self.assertAlmostEqual(r.m.shed_completed_h[key][0], 1.0, places=2)
        self.assertTrue(self.logs_of("shed_complete"))

    def test_an_assume_filled_freeze_blocks_the_shed(self):
        """§9.4b: the freeze covers RECYCLING — acting on unverified inventory converts a
        bookkeeping ambiguity into a real naked short."""
        r = self._armed()
        r.m.frozen.add(SHED_TICKER)
        for i in range(3):
            r.iteration(NOW + 1 + i)
        self.assertEqual([b for b in r.m.ex.placed if b["side"] == "ask"], [])


if __name__ == "__main__":
    unittest.main()
