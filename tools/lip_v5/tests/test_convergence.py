"""THE ACCEPTANCE TEST OF THE CONVERGENCE REFACTOR — the spine of the whole build.

Ryan, 2026-07-30: "if I close all the orders, it comes to the exact same conclusions it had,
and places all the same orders, as a symptom of how it works, not as a directed rule."

That sentence is a TESTABLE PROPERTY, and this file is its test.  The book must be a pure
function of (live programs, order books, measurements of the world, positions, ceiling).
Memory of the WORLD is legal — close caches, phi tape, the exchange's own estimates.  Memory
of OUR OWN PAST DECISIONS as an input is the disease: a rung we climbed, a venue we admitted,
a snapshot of what we were resting.  Every one of those makes the book a function of its own
history, and two processes with the same world and different histories then quote differently.

WHAT THIS TEST DOES.  Drives the assembled runner to a steady book against a fake exchange,
takes a fingerprint of that book, then CANCELS EVERY ORDER EXCHANGE-SIDE — the wire's own
state changes underneath us, exactly as if a human had flattened the account — and runs more
cycles.  The same book must come back, with no replay path in the process at all.

WHY THE FINGERPRINT IS (ticker, side, size-within-hysteresis) AND NOT AN ORDER ID: the claim
is about the CONCLUSIONS, not about the objects.  A re-derived rung is a different order with
the same economics, which is precisely the point.
"""

import unittest

from .. import config as C, exchange as X, runner as RUN, runtime as R
from .base import LipTestCase
from .test_engine import EngineCase
from .test_runner import NOW, program_body

TK_A = "KXAAAGASD-26JUL29-T4.12"
TK_B = "KXCONVB-26JUL29-T2.00"
NESTOR = {"open_order_tickers": [], "position_tickers": []}


def cheap_book():
    return {"orderbook": {"orderbook_fp": {
        "yes_dollars": [["0.06", "1200"]], "no_dollars": [["0.93", "1200"]]}}}


class ConvergenceExchange(X.FakeExchange):
    """A world that does not change while we look away — so any difference in the book is
    OURS, not the board's."""

    def __init__(self, programs, books, **kw):
        kw.setdefault("balance_cents", 1_000_000)
        kw.setdefault("now", NOW)
        super(ConvergenceExchange, self).__init__(books=books, **kw)
        self._programs = programs

    def programs(self, cursor=None):
        return 200, self._programs

    def cancel_all_exchange_side(self):
        """THE EVENT UNDER TEST.  Every resting order disappears from the wire without our
        asking — a hand flatten, an exchange sweep, a cancel-all from another console.  Our
        own books are NOT touched: discovering it is part of what convergence means."""
        n = len(self.resting)
        self.resting.clear()
        return n


class ConvergenceCase(EngineCase):
    def runner(self, **kw):
        ex = ConvergenceExchange(program_body(tickers=(TK_A,)), {TK_A: cheap_book()})
        ex.market_closes[TK_A] = NOW + 16 * 3600
        m = self.maker(ex=ex, **kw)
        r = RUN.Runner(m, sleep=lambda _s: None)
        r.classifier.close_ts[TK_A] = NOW + 16 * 3600
        return r, ex

    def settle(self, r, t0, n=12, step=1.0):
        """Run the loop until the book stops changing."""
        t = t0
        for _ in range(int(n)):
            t += step
            r.iteration(t)
        return t

    def fingerprint(self, ex):
        """(ticker, side) -> resting size, as the EXCHANGE sees it.  Internal state is not
        evidence here: the claim is about what reaches the wire."""
        out = {}
        for body in ex.resting.values():
            key = (body.get("ticker"), body.get("side"))
            out[key] = out.get(key, 0.0) + float(body.get("count", 0))
        return out


class TestTheBookIsAPureFunctionOfTheWorld(ConvergenceCase):

    def test_cancelling_EVERY_order_reproduces_the_SAME_book(self):
        """THE SPINE.  No replay path exists in the process — reinstate and the book snapshot
        are deleted — so the only way the book can come back is by being re-derived from the
        same world."""
        r, ex = self.runner()
        ok, refusals = r.init(NOW, nestor_state=NESTOR)
        self.assertTrue(ok, refusals)
        t = self.settle(r, NOW)
        before = self.fingerprint(ex)
        self.assertTrue(before, "the fixture never reached a steady book")

        killed = ex.cancel_all_exchange_side()
        self.assertGreater(killed, 0)
        self.assertEqual(self.fingerprint(ex), {})

        # THE DERIVED LATENCY, stated rather than guessed: the wire is re-read every
        # RECON_POSITIONS_S, and an order the resting list does not carry then goes through
        # the §9.4a disambiguation — up to two clean fills reads FILLS_REQUERY_DELAY_S apart
        # — before it is terminal.  Only then is the rung absent, and the requoter re-derives
        # it.  Anything faster would mean skipping a check; anything slower is a bug.
        budget_s = C.RECON_POSITIONS_S + 2 * C.FILLS_REQUERY_DELAY_S + 4 * C.BOOK_SNAPSHOT_S
        t = self.settle(r, t, n=int(budget_s / 5.0) + 4, step=5.0)
        after = self.fingerprint(ex)
        self.assertEqual(sorted(after), sorted(before),
                         "a different SET of rungs came back: the book is not a function of "
                         "the world")
        for key, q in before.items():
            self.assertAlmostEqual(after[key], q, delta=max(1.0, 0.10 * q),
                                   msg="rung %s came back at a different size" % (key,))

    def test_no_replay_path_is_reachable_from_the_runner(self):
        """The mutation guard for the assertion above: if any replay path returns, the test
        above could pass for the wrong reason."""
        for gone in ("reinstate", "reinstate_pass", "pending_reinstate"):
            self.assertFalse(hasattr(RUN.Runner, gone), gone)
        self.assertFalse(hasattr(C, "BOOK_SNAPSHOT_PATH"))

    def test_no_PERMISSION_state_gates_the_second_derivation(self):
        """Stage 1's property, stated as convergence: the second derivation must not be
        cheaper or dearer than the first because of what the first one learned about its own
        entitlements."""
        r, _ex = self.runner()
        r.init(NOW, nestor_state=NESTOR)
        for gone in ("venues", "venue_status", "admit_venues", "venue_floor_usd"):
            self.assertFalse(hasattr(r.m, gone), gone)


class TestTheRiskRailsSurviveConvergence(ConvergenceCase):
    """Convergence may not be bought by removing a rail.  Every one of these bounds the book
    in DOLLARS and none of them remembers a decision."""

    def test_the_ceiling_and_the_cluster_reserve_still_bind(self):
        r, ex = self.runner()
        r.init(NOW, nestor_state=NESTOR)
        out = None
        t = NOW
        for _ in range(12):
            t += 1.0
            out = r.iteration(t)
        spent = out["allocate"]["spent"]
        self.assertLessEqual(spent, r.m.ceiling_usd + 1e-9)
        self.assertLessEqual(spent, out["allocate"]["cluster_cap_usd"] + 1e-9)

    def test_a_halt_still_stops_everything(self):
        r, ex = self.runner()
        r.init(NOW, nestor_state=NESTOR)
        self.settle(r, NOW, n=4)
        r.m.halt.halt("test_halt", NOW + 5)
        placed_before = len(ex.placed)
        ex.cancel_all_exchange_side()
        self.settle(r, NOW + 5, n=10)
        self.assertEqual(len(ex.placed), placed_before,
                         "a halted book re-derived itself onto the wire")


if __name__ == "__main__":
    unittest.main()
