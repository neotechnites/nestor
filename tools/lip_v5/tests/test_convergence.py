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

    def test_the_MARGINAL_QUEUE_extends_the_spine_EXACTLY_not_within_hysteresis(self):
        """THE SPINE, EXTENDED TO V6 (note 55 item 4a's anti-churn).

        A switch toll is a hysteresis, and a hysteresis is normally the death of convergence:
        if "do I already rest here" were an input, the book after a cancel-all would differ
        from the book before it, and the most that could be claimed is agreement WITHIN the
        toll.  v6 claims more, and this test is the claim: the toll's two halves are

          (a) stranded sub-cliff accrual — the estimates feed's own number, and
          (b) transit presence loss, charged as `h_eff = h - transit_h` for a market we are
              not PRESENT in, where presence is read from ACCRUED and not from our orders,

        so BOTH are world facts that a cancel-all cannot touch.  The allocation is therefore
        reproduced EXACTLY, not approximately.  Revert `Curve.present` to read a resting order
        and this test fails on the exact symptom: the second derivation pays the toll the
        first one did not.
        """
        from .. import marginal as MQ

        def board(accrued):
            return [
                MQ.A.Slot("KXSPINE-26JUL31-T1", "bid", rho=2.0, S=200.0, p=0.20,
                          hours_left=24.0, accrued=accrued, target_size=1000,
                          cum_size=2000.0),
                MQ.A.Slot("KXSPINE2-26JUL31-T1", "bid", rho=2.0, S=200.0, p=0.20,
                          hours_left=24.0, accrued=0.0, target_size=1000, cum_size=2000.0),
            ]
        # steady state: one market holds $0.60 of conditional accrual and is PRESENT.
        first, _s1, _r1 = MQ.allocate_marginal(board(0.60), budget_usd=60.0,
                                               per_market_cap_usd=21.43,
                                               cluster_cap_usd=21.43)
        # cancel-all: every order of ours is gone.  The accrual is NOT — the exchange already
        # credited it — so the identical inputs produce the identical book.
        again, _s2, _r2 = MQ.allocate_marginal(board(0.60), budget_usd=60.0,
                                               per_market_cap_usd=21.43,
                                               cluster_cap_usd=21.43)
        self.assertEqual(first, again)
        self.assertTrue(any(q > 0 for q in first.values()), "the fixture funded nothing")

    def test_the_queue_carries_NO_memory_of_its_own_previous_allocation(self):
        """The mutation guard for the test above: `allocate_marginal` is a pure function, so
        there is nowhere for a previous allocation to hide."""
        import inspect
        from .. import marginal as MQ
        sig = inspect.signature(MQ.allocate_marginal)
        for forbidden in ("previous", "last_alloc", "prior_alloc", "incumbent", "resting"):
            self.assertNotIn(forbidden, sig.parameters, forbidden)
        src = inspect.getsource(MQ.Curve.__init__)
        self.assertNotIn("orders", src,
                         "the toll must key on ACCRUED, never on our own resting orders")

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


class TestTheMultiMarketBookIsAPureFunctionOfTheWorld(ConvergenceCase):
    """The spine, EXTENDED per the owner's law §10 (2026-07-30): a steady MULTI-market book
    — two programs, two clusters, different prices so the law sizes them differently — is
    cancelled exchange-side and the SAME book re-emerges from the ranking alone.  This is
    the law's determinism doing the work: same world ⇒ same needs ⇒ same cheapest-first
    ranking ⇒ same orders, with no discovery-order dependence and no replay path."""

    def runner2(self):
        def prog(pid, series, tk, reward):
            from datetime import datetime, timezone
            iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            return {"id": pid, "series_ticker": series, "market_tickers": [tk],
                    "period_reward": reward, "start_date": iso(NOW - 3600),
                    "end_date": iso(NOW + 16 * 3600), "target_size_fp": 1000}

        def book(px, npx):
            return {"orderbook": {"orderbook_fp": {
                "yes_dollars": [[px, "1200"]], "no_dollars": [[npx, "1200"]]}}}

        programs = {"incentive_programs": [
            prog("prog-1", "KXAAAGASD", TK_A, 1_000_000),
            prog("prog-2", "KXCONVB", TK_B, 800_000),
        ]}
        ex = ConvergenceExchange(programs, {TK_A: book("0.06", "0.93"),
                                            TK_B: book("0.11", "0.88")})
        for tk in (TK_A, TK_B):
            ex.market_closes[tk] = NOW + 16 * 3600
        m = self.maker(ex=ex)
        r = RUN.Runner(m, sleep=lambda _s: None)
        for tk in (TK_A, TK_B):
            r.classifier.close_ts[tk] = NOW + 16 * 3600
        return r, ex

    def test_two_clusters_come_back_at_the_same_sizes(self):
        r, ex = self.runner2()
        ok, refusals = r.init(NOW, nestor_state=NESTOR)
        self.assertTrue(ok, refusals)
        t = self.settle(r, NOW, n=20)
        before = self.fingerprint(ex)
        self.assertGreaterEqual(len({k[0] for k in before}), 2,
                                "the fixture never funded both clusters: %s" % before)

        killed = ex.cancel_all_exchange_side()
        self.assertGreaterEqual(killed, 2)
        budget_s = C.RECON_POSITIONS_S + 2 * C.FILLS_REQUERY_DELAY_S + 4 * C.BOOK_SNAPSHOT_S
        t = self.settle(r, t, n=int(budget_s / 5.0) + 4, step=5.0)
        after = self.fingerprint(ex)
        self.assertEqual(sorted(after), sorted(before),
                         "a different SET of rungs came back")
        for key, q in before.items():
            self.assertAlmostEqual(after[key], q, delta=max(1.0, 0.10 * q),
                                   msg="rung %s came back at a different size" % (key,))


class TestPlanGrowthReachesTheWire(ConvergenceCase):
    """G1's spine half: the plan can move while the BOOK sits still, and the growth must
    reach the wire — otherwise the wire is a function of WHEN a measurement arrived rather
    than of the world.  Only trigger (f) TARGET_MOVED carries a pure size increase.

    ── REWRITTEN FOR V6 (2026-07-31) ───────────────────────────────────────────────────────
    The v5 version of this test used the phi chain crossing from SEED to MEASURED to flip
    the order from the lot-container tranche (83 contracts) to the full $10 allocation (166).
    BOTH of those objects are deleted by v6: there is no lot container and no seat, so there
    is no staircase to climb — the marginal queue enters at the cliff block and deepens
    continuously to wherever the next dollar stops being the best dollar, in ONE placement.

    So the same spine property is tested against v6's own growth driver, which is the one
    note 55 actually deploys on: A CAPITAL EVENT.  "v6 goes live the moment the deposit
    lands" — the deposit raises C, the rail A = C/N grows with it, the plan grows, and the
    growth has to reach the wire.  The book never moves; only C does.
    """

    def _one_sided_runner(self, ceiling_usd):
        one_sided = {"orderbook": {"orderbook_fp": {
            "yes_dollars": [["0.06", "1200"]], "no_dollars": []}}}
        ex = ConvergenceExchange(program_body(tickers=(TK_A,), reward=287_000),
                                 {TK_A: one_sided})
        ex.market_closes[TK_A] = NOW + 16 * 3600
        m = self.maker(ex=ex, ceiling_usd=ceiling_usd)
        r = RUN.Runner(m, sleep=lambda _s: None)
        r.classifier.close_ts[TK_A] = NOW + 16 * 3600
        return r, ex, m

    @unittest.skipUnless(C.MARGINAL_QUEUE_ARMED,
                         "v6 growth driver: the rail is A = C/N only under the armed core")
    def test_the_entry_block_rests_first_and_thin_tape_may_NOT_deepen_it(self):
        """G2 AT THE WIRE.  The queue enters at the cliff block in ONE placement — no v5
        staircase — and it may NOT deepen past it while the rung's own tape is thin.  Revert
        `Curve.may_deepen` and this rung goes straight to the rail on its first second of
        life, which is the quiet-afternoon incident through the deepening door."""
        r, ex, m = self._one_sided_runner(300.0)
        ok, refusals = r.init(NOW, nestor_state=NESTOR)
        self.assertTrue(ok, refusals)
        self.settle(r, NOW, n=60)
        early = self.fingerprint(ex)
        self.assertEqual(early.get((TK_A, "bid")), 96.0,
                         "the ENTRY BLOCK is what rests first: %s" % early)
        self.assertLess(early[(TK_A, "bid")] * 0.06, m.cluster_rail_usd(),
                        "thin tape took the whole rail")

    @unittest.skipUnless(C.MARGINAL_QUEUE_ARMED,
                         "v6 growth driver: the rail is A = C/N only under the armed core")
    def test_earned_depth_then_a_DEPOSIT_both_reach_the_wire_via_trigger_f(self):
        """G1's spine half, in v6's two growth drivers — and both are world facts, not
        memories of ours.

        (1) EARNED DEPTH.  The rung's own exposure crosses `may_deepen`'s two clauses
            (own exposure > k AND >= 3 x h = 48 contract-hours, ~30 min of wall clock at 96
            contracts resting) and the queue deepens it toward the rail.  Nothing about the
            board moved; only the TAPE did.
        (2) THE DEPOSIT.  C doubles; the rail A = C/N doubles with it because N is
            capital-INDEPENDENT, and the plan grows again.  "v6 goes live the moment the
            deposit lands" — this is that event, at the wire.

        Only trigger (f) TARGET_MOVED carries a pure size increase, so this is also its test.
        """
        r, ex, m = self._one_sided_runner(300.0)
        ok, refusals = r.init(NOW, nestor_state=NESTOR)
        self.assertTrue(ok, refusals)
        t = self.settle(r, NOW, n=60)
        entry = self.fingerprint(ex)[(TK_A, "bid")]
        # (1) the rung earns its own depth — TRUE 1 Hz, because the meter accrues `remaining`
        # once per TICK and this test is about how much exposure exists.
        t = self.settle(r, t, n=2_200, step=1.0)
        earned = self.fingerprint(ex)[(TK_A, "bid")]
        rail_before = m.cluster_rail_usd()
        self.assertGreater(earned, entry,
                           "earned depth never reached the wire: %s -> %s" % (entry, earned))
        self.assertAlmostEqual(earned * 0.06, rail_before, delta=0.06,
                               msg="deepening should now run to the rail: %s at $%s"
                                   % (earned, rail_before))
        # (2) THE DEPOSIT.  Nothing about the board changes.
        m.ceiling_usd = 600.0
        m.cash.ceiling_usd = 600.0
        t = self.settle(r, t, n=int(C.MIN_RESTING_LIFE_S) + 30, step=1.0)
        after = self.fingerprint(ex)[(TK_A, "bid")]
        rail_after = m.cluster_rail_usd()
        self.assertAlmostEqual(rail_after, 2.0 * rail_before, places=6,
                               msg="N is capital-independent, so the rail must double: "
                                   "%s -> %s" % (rail_before, rail_after))
        self.assertGreater(after, earned,
                           "the grown plan never reached the wire: %s -> %s"
                           % (earned, after))

    @unittest.skipUnless(C.MARGINAL_QUEUE_ARMED, "v6 dials")
    def test_N_is_capital_independent(self):
        """note 54 step 1: "N is capital-independent" — C cancels out of the ruin formula, so
        the rail scales LINEARLY and the diversification count does not move."""
        r3, _ex3, m3 = self._one_sided_runner(300.0)
        r6, _ex6, m6 = self._one_sided_runner(600.0)
        for r in (r3, r6):
            r.init(NOW, nestor_state=NESTOR)
            self.settle(r, NOW, n=8)
        self.assertEqual(m3.dials.n_clusters, m6.dials.n_clusters,
                         "N moved with capital: %s vs %s" % (m3.dials.n_clusters,
                                                             m6.dials.n_clusters))
        self.assertAlmostEqual(m6.dials.rail_usd / m3.dials.rail_usd, 2.0, places=6)


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
