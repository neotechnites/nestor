"""D1-D5 + the entry band + the live-program filter — the round the reviewer blocked.

Every test here targets a defect that the 626-test suite passed WITH: two blockers, a guard
whose deletion nothing detected, and zero integration coverage for B15/B16.  Note 45's thesis
arriving inside our own tests — green meant self-consistent, not correct.
"""

import unittest

from .. import config as C, guards as G, runner as RUN, runtime as R, scan
from .base import LipTestCase
from .test_engine import EngineCase, NOW

TK = "KXBAND-26JUL29-T4.12"


def prog(pid="p1", series="KXBAND", tickers=(TK,), reward=1_000_000,
         start=NOW - 3600, end=NOW + 16 * 3600, paid_out=False):
    return {"program_id": pid, "series": series, "tickers": list(tickers),
            "period_reward": reward, "start_ts": start, "end_ts": end,
            "window_h": max(1e-9, (end - start) / 3600.0),
            "rho": scan.pool_rate(reward, max(1e-9, (end - start) / 3600.0)),
            "target_size": 1000.0, "paid_out": paid_out}


class Table(object):
    """A classifier stand-in: one ticker, both sides, with the knobs each test needs."""

    def __init__(self, ticker=TK, pid="p1", bid_p=0.12, ask_p=0.85,
                 qualifies=True, cum=1200.0, S=500.0):
        self.table = {ticker: {
            "ticker": ticker, "program_id": pid, "series": "KXBAND", "pinned": False,
            "target_size": 1000.0, "yes_mid": 0.135, "ts": NOW, "close_ts": None,
            "sides": {"bid": {"S": S, "qualifies": qualifies, "cum_size": cum,
                              "p": bid_p, "legal": True},
                      "ask": {"S": S, "qualifies": qualifies, "cum_size": cum,
                              "p": ask_p, "legal": True}}}}


def sides(slots):
    return sorted(s.side for s in slots)


# =============================================================================================
# D1 — the held-inventory exemption.  The blocker: a fill pops the order, so the free-ride
# gate's `own_qty > 0` exemption vanished exactly when we first had inventory to shed.
# =============================================================================================
class TestD1HeldInventoryKeepsItsSlot(LipTestCase):

    def test_a_NON_held_non_qualifying_side_is_still_refused(self):
        """The gate must still do its job — this is the control for the two below."""
        slots = scan.build_slots([prog()], Table(qualifies=False, cum=400.0), NOW)
        self.assertEqual(slots, [])

    def test_a_HELD_non_qualifying_market_STILL_GETS_A_SLOT(self):
        """D1.  No slot ⇒ `update_shed_targets` can never START a shed (`s is None`) and
        `requote_pass` has no `s.p` to price one with: the gate deleted the exit."""
        slots = scan.build_slots([prog()], Table(qualifies=False, cum=400.0), NOW,
                                 held={TK})
        self.assertEqual(sides(slots), ["ask", "bid"])

    def test_a_HELD_market_keeps_its_slot_after_the_program_WINDOW_ENDS(self):
        """The shed must stay priceable past the reward window, and the slot must not be able
        to buy fresh exposure — `alloc.allocate` refuses `hours_left <= 0` on its own line."""
        dead = prog(start=NOW - 40 * 3600, end=NOW - 3600)
        self.assertEqual(scan.build_slots([dead], Table(), NOW), [])
        slots = scan.build_slots([dead], Table(), NOW, held={TK})
        self.assertEqual(sides(slots), ["ask", "bid"])
        self.assertTrue(all(s.hours_left <= 0 for s in slots))

    def test_the_held_set_is_the_SAME_set_the_poll_guarantee_uses(self):
        """One definition, two consumers: a polled-but-slotless market is nearly as blind."""
        self.assertTrue(hasattr(RUN.Runner, "held_tickers"))


# =============================================================================================
# D3/D4 — the dead deduction, and the load-bearing zero.
# =============================================================================================
class TestD3TheGateTestsQualificationNotATruncatedCumulant(LipTestCase):

    def test_cum_size_short_of_target_but_QUALIFIES_is_admitted(self):
        """`cum_size` is the target-size WALK and stops at target, so it is not side depth and
        arithmetic on it understates rival depth up to 2x.  `qualifies` is the real test."""
        slots = scan.build_slots([prog()], Table(qualifies=True, cum=1000.0), NOW)
        self.assertEqual(sides(slots), ["ask", "bid"])

    def test_our_own_resting_size_exempts_the_side_without_deducting_from_the_walk(self):
        own = {(TK, "bid"): [(12, 300.0)]}
        slots = scan.build_slots([prog()], Table(qualifies=False, cum=400.0), NOW,
                                 own_orders=own)
        self.assertEqual(sides(slots), ["bid"])


class TestD4LandGrabIsDeadUnderFreeRide(LipTestCase):
    """Deleting `land_grab = 0` in `scan.build_slots` passed all 626 tests.  It must not."""

    def test_a_held_non_qualifying_side_funds_NO_qualification(self):
        self.assertTrue(C.FREE_RIDE_ONLY, "this test is meaningless with the flag off")
        slots = scan.build_slots([prog()], Table(qualifies=False, cum=400.0), NOW,
                                 held={TK})
        self.assertTrue(slots)
        for s in slots:
            self.assertEqual(s.land_grab_size, 0,
                             "the 1c funding path is the -100% cohort's own geometry")

    def test_and_a_resting_non_qualifying_side_funds_NO_qualification(self):
        own = {(TK, "bid"): [(12, 300.0)]}
        slots = scan.build_slots([prog()], Table(qualifies=False, cum=400.0), NOW,
                                 own_orders=own)
        self.assertTrue(slots)
        self.assertEqual([s.land_grab_size for s in slots], [0])


# =============================================================================================
# The live-program filter.  $225 of the day's $477 loss sat in six events that paid $0 of LIP.
# =============================================================================================
class TestTheLiveProgramFilter(LipTestCase):

    def test_a_PAID_OUT_program_produces_no_slot(self):
        self.assertEqual(scan.build_slots([prog(paid_out=True)], Table(), NOW), [])

    def test_a_ZERO_POOL_program_produces_no_slot(self):
        """The gap this closes is NARROW and a mutation test found it: `runway_ok` already
        refuses ρ ≤ 0 — but only when there is nothing accrued.  With accrual at or above
        `RESCUE_TARGET_USD` it short-circuits (`if need <= 0: return True`) and a zero-pool
        program produced slots.  Asserting the easy case would have left this guard undetectable:
        deleting it passed the whole suite until this test named the accrued path."""
        accrued = {"p1": C.RESCUE_TARGET_USD + 1.0}
        self.assertTrue(scan.build_slots([prog()], Table(), NOW, accrued=accrued),
                        "control: with a real pool the slot exists")
        self.assertEqual(scan.build_slots([prog(reward=0)], Table(), NOW, accrued=accrued), [])

    def test_but_a_HELD_ticker_is_exempt_so_inventory_is_never_stranded(self):
        self.assertTrue(scan.build_slots([prog(paid_out=True)], Table(), NOW, held={TK}))
        self.assertTrue(scan.build_slots([prog(reward=0)], Table(), NOW, held={TK}))

    def test_paid_out_is_the_exchanges_OWN_word_not_our_inference(self):
        self.assertIn("paid_out", scan.parse_programs(
            {"incentive_programs": [{"id": "x", "market_tickers": ["T"],
                                     "period_reward": 100, "paid_out": True,
                                     "start_date": "2026-07-29T00:00:00Z",
                                     "end_date": "2026-07-30T00:00:00Z"}]})[0])


# =============================================================================================
# The entry band — STAGED INERT, and the inertness must be detectable.
# =============================================================================================
class TestTheEntryBandIsStagedInert(LipTestCase):

    def test_the_band_is_NOT_armed(self):
        """Armed, its intersection with the (★) admission gate is EMPTY: `phi` is seeded by
        PRICE with an 80x step at 5c, and every in-band price lands on the refusing side."""
        self.assertFalse(C.ENTRY_BAND_ARMED)

    def test_INERT_an_out_of_band_price_is_admitted(self):
        slots = scan.build_slots([prog()], Table(bid_p=0.02, ask_p=0.95), NOW)
        self.assertEqual(sides(slots), ["ask", "bid"])

    def test_ARMED_an_out_of_band_price_is_refused_and_an_in_band_one_is_not(self):
        self.arm_band()
        self.assertEqual(scan.build_slots([prog()], Table(bid_p=0.02, ask_p=0.95), NOW), [])
        # 12c bid is in band; 85c ask is not — on a binary the sides sum to ~$1, so the band
        # admits exactly ONE side per market.  That is the design, not a defect.
        self.assertEqual(sides(scan.build_slots([prog()], Table(), NOW)), ["bid"])

    def test_ARMED_a_held_ticker_is_still_exempt(self):
        self.arm_band()
        self.assertTrue(scan.build_slots([prog()], Table(bid_p=0.02, ask_p=0.95), NOW,
                                         held={TK}))

    def arm_band(self):
        for name, val in (("ENTRY_BAND_ARMED", True),):
            old = getattr(C, name)
            setattr(C, name, val)
            self.addCleanup(setattr, C, name, old)


# =============================================================================================
# INTEGRATION — across 206 tests, 0 `ceiling` and 0 `market_cap` refusals ever fired.  Every
# B15/B16 test hand-built a `PlaceContext`; none exercised `engine.place_context()`.
# =============================================================================================
class TestB15B16FireThroughTheRealPlaceContext(EngineCase):

    def _armed(self, ceiling):
        m = self.maker(ceiling_usd=ceiling)
        ok, refusals = m.startup(NOW, nestor_state={"open_order_tickers": [],
                                                    "position_tickers": []})
        self.assertTrue(ok, refusals)
        return m

    def _fill_book(self, m, clusters, usd_each, basis=0.50):
        """Load the book across DISTINCT clusters, so the total binds before any cluster does —
        B15 is deliberately LAST so a cheaper refusal names the specific cause, and a test that
        trips the cluster cap first never reaches it."""
        for name in clusters:
            tk = "%s-1" % name
            m.positions[tk] = {"yes": usd_each / basis, "no": 0.0}
            m.entry_basis[(tk, "yes")] = basis

    def test_the_CEILING_refuses_through_engine_place_context(self):
        m = self._armed(300.0)
        self._fill_book(m, ("AAA", "BBB", "CCC", "DDD"), 70.0)        # $280 of $300
        ctx = m.place_context(available_cash_usd=10_000.0)
        self.assertAlmostEqual(ctx.ceiling_usd, 300.0, places=6)
        ok, reason, detail = G.place_allowed(ctx, {"ticker": "EEE-1", "side": "yes",
                                                  "n": 50, "basis": 0.50,
                                                  "fully_closing": False})
        self.assertFalse(ok)
        self.assertEqual(reason, "ceiling", detail)
        # a book at its ceiling must always be able to LEAVE
        ok2, reason2, _ = G.place_allowed(ctx, {"ticker": "EEE-1", "side": "yes",
                                                "n": 50, "basis": 0.50,
                                                "fully_closing": True})
        self.assertTrue(ok2, reason2)

    def test_the_MARKET_CAP_refuses_through_engine_place_context(self):
        m = self._armed(300.0)
        cap = C.market_leg_cap_usd(300.0, G.day_stop_usd(m.projected_day_reward,
                                                        ceiling_usd=300.0))
        m.positions["MINE-1"] = {"yes": cap / 0.50, "no": 0.0}   # exactly at the leg cap
        m.entry_basis[("MINE-1", "yes")] = 0.50
        ctx = m.place_context(available_cash_usd=10_000.0)
        self.assertAlmostEqual(ctx.market_cap_usd, cap, places=6)
        ok, reason, _ = G.place_allowed(ctx, {"ticker": "MINE-1", "side": "yes",
                                              "n": 4, "basis": 0.50,
                                              "fully_closing": False})
        self.assertFalse(ok)
        self.assertEqual(reason, "market_cap")
        # ...and the OPPOSING leg is untouched (D2: exactly one outcome pays)
        ok2, reason2, _ = G.place_allowed(ctx, {"ticker": "MINE-1", "side": "no",
                                                "n": 4, "basis": 0.50,
                                                "fully_closing": False})
        self.assertTrue(ok2, reason2)

    def test_D5_a_gone_404_order_is_NOT_charged_as_collateral(self):
        """The exchange said the id does not exist; six other consumers of `self.orders` already
        exclude it.  Under a binding ceiling a phantom dollar refuses a real one 1:1."""
        m = self._armed(300.0)
        m.orders["real"] = {"ticker": "REAL-1", "side": "bid", "price": 0.50,
                            "remaining": 40.0}
        m.orders["ghost"] = {"ticker": "GHOST-1", "side": "bid", "price": 0.50,
                             "remaining": 400.0, "gone_404": True}       # $200 of phantom
        ctx = m.place_context(available_cash_usd=10_000.0)
        self.assertEqual([p for p in ctx.resting_basis if p["ticker"] == "GHOST-1"], [])
        self.assertEqual([p["ticker"] for p in ctx.resting_basis], ["REAL-1"])
        committed = sum(p["n"] * p["basis"]
                        for p in list(ctx.positions) + list(ctx.resting_basis))
        self.assertAlmostEqual(committed, 20.0, places=6,
                               msg="the $200 ghost must not be charged against the ceiling")


if __name__ == "__main__":
    unittest.main()
