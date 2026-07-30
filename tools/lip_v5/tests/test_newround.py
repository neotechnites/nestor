"""NEW ROUND — the two findings of the adversarial re-verify of de6bb84.

NEW-1  the MBB x cluster-cap REQUOTE DEADLOCK.  `config.slot_cap_usd` and
       `clusters.cluster_cap_usd` are the identical expression, so a slot at its own cap IS
       the whole cluster cap.  Make-before-break places the replacement while the old order
       still rests and `place_allowed` measured BOTH in one cluster reading — so the
       replacement was refused `cluster_worst_case_cap`, `_requote_slot` returned False, and
       nothing armed the cancel-first degrade (it latches only on an exchange
       `insufficient balance` reject).  The slot then re-offered the same refused order every
       cycle, FOREVER, at its stale price.  Second face: `allocate()` carried per-market and
       per-venue caps but NO cluster term, so four rungs of one series planned $34.56 while
       `place()` funded exactly one rung at $10.00 — 264 refusals in 90 cycles.

NEW-2  NO FILLS POLL WHILE HALTED.  `poll_fills_due` lived in `cycle()`, which a halted
       iteration never reaches, so a halted shed that FILLED on the wire stayed in our books
       as a position AND as a live order for as long as the halt lasted — and
       `halted_closing_pass` would not repost, because it still saw its own (dead) order.

Every test here drives the ASSEMBLED loop and asserts on what the EXCHANGE saw.
"""

import unittest

from .. import alloc, clusters as CL, config as C, exchange as X, guards as G
from .. import runner as RUN, runtime as R
from .base import LipTestCase
from .test_engine import EngineCase, NOW
from .test_runner import program_body

TK = "KXAAAGASD-26JUL29-T4.12"
RUNGS = ["KXAAAGASD-26JUL29-T%d.0" % i for i in range(1, 5)]
NESTOR = {"open_order_tickers": [], "position_tickers": []}


def bk(yes_px="0.06", yes_sz="1200", no_px="0.93", no_sz="1200"):
    return {"orderbook": {"orderbook_fp": {"yes_dollars": [[yes_px, yes_sz]],
                                           "no_dollars": [[no_px, no_sz]]}}}


class ProgExchange(X.FakeExchange):
    def __init__(self, programs, books, **kw):
        kw.setdefault("balance_cents", 1_000_000)
        super(ProgExchange, self).__init__(books=books, **kw)
        self._programs = programs

    def programs(self, cursor=None):
        return 200, self._programs


class NewRoundCase(EngineCase):
    def runner(self, ex):
        m = self.maker(ex=ex)
        return RUN.Runner(m, sleep=lambda _s: None)

    def mature(self, m, series="KXAAAGASD", rung=5):
        """STAGE 1, 2026-07-30: a NO-OP, kept as a seam.  It used to promote a venue past its
        ratchet ramp so the CLUSTER cap was the guard under test rather than the rung cap.
        There is no ramp and no rung: every candidate competes on its numbers from the first
        cycle, and the cluster's DOLLARS are the only thing that was ever really guarding
        these tests."""
        return None

    def refusals(self, reason=None):
        rows = [r for r in self.logs if r.get("t") == "place_refused"]
        if reason is not None:
            rows = [r for r in rows if r.get("refused_by") == reason]
        return rows


# =============================================================================================
# NEW-1a — THE DEADLOCK ITSELF.  A slot allocated at its cap; rivals lift the best; a NEW
# order at the NEW best must actually reach the exchange.
# =============================================================================================
class TestRequoteAtTheNewBestReachesTheWire(NewRoundCase):
    def _armed(self):
        ex = ProgExchange(program_body(tickers=(TK,)), {TK: bk()})
        r = self.runner(ex)
        ok, refusals = r.init(NOW, nestor_state=NESTOR)
        self.assertTrue(ok, refusals)
        r.iteration(NOW + 1)
        self.mature(r.m, rung=4)
        self.assertTrue(r.m.orders, "no order rests: the fixture never armed")
        return r, ex

    def test_a_slot_at_its_cap_requotes_to_the_new_best(self):
        """THE ADVERSARIAL TEST.  Pre-fix this asserts nothing at 3c ever reaches the wire."""
        r, ex = self._armed()
        old = list(r.m.orders.values())[0]
        self.assertAlmostEqual(float(old["price"]), 0.06, places=6)
        ex.books[TK] = bk("0.07", "3000", "0.92", "1200")     # rivals lift the best
        t = NOW + 40
        for _ in range(6):
            r.iteration(t)
            t += 1.0
        prices = sorted({round(float(b["price"]), 4) for b in ex.placed})
        self.assertIn(0.07, prices,
                      "NO order at the NEW best ever reached the exchange: %s" % (prices,))

    def test_the_deadlock_leaves_no_cluster_refusal_behind(self):
        """The refusal is the deadlock's own fingerprint: a make-before-break REPLACEMENT is
        not an addition, so it must never be measured against the cluster as one."""
        r, ex = self._armed()
        ex.books[TK] = bk("0.07", "3000", "0.92", "1200")
        t = NOW + 40
        for _ in range(6):
            r.iteration(t)
            t += 1.0
        self.assertEqual(self.refusals(CL.REFUSE_WORST), [],
                         "the replacement was refused as though it were an addition")

    def test_the_stale_quote_actually_leaves(self):
        """MBB's other half: once the replacement rests, the old one is cancelled — the slot
        must not end with two live orders (or with only the stale one)."""
        r, ex = self._armed()
        ex.books[TK] = bk("0.07", "3000", "0.92", "1200")
        t = NOW + 40
        for _ in range(6):
            r.iteration(t)
            t += 1.0
        # PER SIDE, since D5′ retired D9 (see below): the BID slot must hold exactly one
        # live quote, at the new best.  The market's other leg is now separately quotable and
        # is not this test's subject.
        live = [o for o in r.m.orders.values()
                if o.get("remaining", 0) > 0 and o["side"] == "bid"]
        self.assertEqual(len(live), 1, "expected exactly one live BID quote, got %s" % (live,))
        self.assertAlmostEqual(float(live[0]["price"]), 0.07, places=6)
        # ⚠ FLAG (D5′, 2026-07-30): the ownership refusal that retired the one-rung-per-
        # cluster COUNT also enforced D9 ("one SIDE per cluster — one-sided for now"), so the
        # ask leg of the SAME market now rests beside the bid.  Both sides score separately
        # (the filing normalises within each side, so a one-sided quote earns at most half a
        # pool), which is why this is desirable — but it was a DEFERRED decision, not a
        # settled one, and it arrived as a side effect rather than a choice.
        asks = [o for o in r.m.orders.values()
                if o.get("remaining", 0) > 0 and o["side"] == "ask"]
        self.assertLessEqual(len(asks), 1)


# =============================================================================================
# NEW-1b — THE ALLOCATOR MUST NOT PLAN WHAT place() WILL REFUSE.
# =============================================================================================
class TestTheLadderPlanIsFundable(NewRoundCase):
    def _ladder(self):
        ex = ProgExchange(program_body(tickers=tuple(RUNGS)), {t: bk() for t in RUNGS})
        r = self.runner(ex)
        ok, refusals = r.init(NOW, nestor_state=NESTOR)
        self.assertTrue(ok, refusals)
        r.iteration(NOW + 1)
        self.mature(r.m)
        return r, ex

    def test_four_rungs_of_one_cluster_plan_only_what_place_accepts(self):
        """THE ADVERSARIAL TEST.  All four rungs share one series ⇒ ONE cluster.  Pre-fix the
        allocator planned $34.56 against a $10 cluster cap and place() refused 264 times."""
        r, ex = self._ladder()
        t = NOW + 2
        for _ in range(90):
            out = r.iteration(t)
            t += 1.0
        planned = {k: q for k, q in (out.get("alloc") or {}).items() if q}
        self.assertTrue(planned, "the allocator planned nothing: the fixture never armed")
        self.assertEqual(self.refusals(CL.REFUSE_WORST), [],
                         "the allocator planned %d cluster-refused orders"
                         % len(self.refusals(CL.REFUSE_WORST)))
        self.assertEqual(self.refusals(CL.REFUSE_SIGNED), [])
        self.assertEqual(len(ex.resting), len(planned),
                         "planned %d slots, %d rest on the exchange"
                         % (len(planned), len(ex.resting)))

    def test_the_plan_itself_fits_under_the_cluster_cap(self):
        r, ex = self._ladder()
        t = NOW + 2
        for _ in range(90):
            out = r.iteration(t)
            t += 1.0
        cap = CL.cluster_cap_usd(G.day_stop_usd(r.m.projected_day_reward),
                                 ceiling_usd=r.m.ceiling_usd)
        rested = sum(float(b["count"]) * float(b["price"]) for b in ex.resting.values())
        self.assertLessEqual(rested, cap + 1e-9)
        self.assertGreater(rested, 0.0, "nothing rests at all")


class TestAllocateCarriesTheClusterTerm(LipTestCase):
    """NEW-1b's survivor under the owner's law (2026-07-30): the PLAN folds the same
    cluster reserve the rails read into its envelope, so it can never propose an order
    `place()` must refuse.  The water level is gone; the envelope is the whole rule."""

    def slots(self, n=4, p=0.02):
        # cum_size >= target: the side qualifies on rival depth, so the law's qualify term
        # is $0 and the tests below isolate the CLUSTER arithmetic.
        return [alloc.Slot("KXAAAGASD-26JUL29-T%d.0" % i, "bid", rho=6.25, S=50.0, p=p,
                           phi=0.0, d=0.0, l_eff=0.0, cum_size=2000.0)
                for i in range(1, n + 1)]

    def test_one_cluster_funds_one_order_inside_the_cap(self):
        a, spent, _ = alloc.allocate_law(self.slots(), 300.0, cluster_cap_usd=10.0)
        self.assertLessEqual(spent, 10.0 + 1e-9)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 1)   # law §2

    def test_separate_clusters_each_get_their_own_cap(self):
        ss = [alloc.Slot("KXGAS-26JUL29-T1.0", "bid", rho=6.25, S=50.0, p=0.02,
                         phi=0.0, d=0.0, l_eff=0.0, cum_size=2000.0),
              alloc.Slot("KXIDX-26JUL29-T1.0", "bid", rho=6.25, S=50.0, p=0.02,
                         phi=0.0, d=0.0, l_eff=0.0, cum_size=2000.0)]
        a, spent, _ = alloc.allocate_law(ss, 300.0, cluster_cap_usd=10.0)
        self.assertGreater(a[ss[0].key], 0)
        self.assertGreater(a[ss[1].key], 0)
        self.assertLessEqual(spent, 20.0 + 1e-9)

    def test_held_inventory_consumes_the_clusters_room(self):
        ss = self.slots(n=2)
        ck = CL.cluster_of(ss[0].ticker)
        a, spent, _ = alloc.allocate_law(ss, 300.0, cluster_cap_usd=10.0,
                                         cluster_spent={ck: 10.0},
                                         market_spent={ss[0].ticker: 10.0})
        self.assertEqual(spent, 0.0)
        self.assertEqual(sum(a.values()), 0)

    def test_with_NO_cluster_cap_the_market_allocation_still_binds(self):
        """REWRITTEN under the law (2026-07-30).  The old assertion documented a HOLE: with
        `cluster_cap_usd=None` the count-less water level sprayed the ladder unbounded.
        The law closes it structurally — one order per cluster and $10 per market bind with
        or without the rail's cap, so the None path is bounded by the law itself."""
        a, spent, _ = alloc.allocate_law(self.slots(), 300.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 1)
        self.assertLessEqual(spent, C.ALLOC_PER_MARKET_USD + 1e-9)


class TestTheGuardHierarchy(LipTestCase):
    """The identity `slot_cap == cluster_cap` is DELIBERATE (the derivation lives in
    `config.slot_cap_usd`).  What must never happen is the INVERSION — a finer cap looser
    than the coarser one it sits inside, which makes the coarser guard decoration.  Asserted
    structurally so a future edit to either function fails the suite instead of going quiet.
    """

    DAY_STOPS = (0.0, 5.0, 20.0, 40.0, 100.0, 150.0, 300.0, 1000.0)

    def test_the_finer_cap_never_exceeds_the_coarser(self):
        for ds in self.DAY_STOPS:
            self.assertLessEqual(C.slot_cap_usd(ds), CL.cluster_cap_usd(ds) + 1e-9,
                                 "slot cap looser than its cluster cap at day stop %s" % ds)
            self.assertLessEqual(C.cap_series_usd(ds), CL.cluster_cap_usd(ds) + 1e-9,
                                 "series cap looser than its cluster cap at day stop %s" % ds)

    def test_no_single_cluster_can_trip_the_day_stop_alone(self):
        """The sentence every one of these caps derives from."""
        for ds in self.DAY_STOPS:
            if ds >= 2 * C.INV_CAP_USD:
                self.assertLessEqual(CL.cluster_cap_usd(ds), 0.5 * ds + 1e-9)


class TestReleasesPrecedeClaims(EngineCase):
    """NEW-1's residual, found while fixing it: a plan whose TOTAL fits the cluster cap was
    still refused for the ORDER IT WAS APPLIED IN.  The requoter walked slots by ticker, so
    when the water level moved dollars from a later rung to an earlier one, the earlier rung's
    CLAIM was measured against the later rung's not-yet-released collateral.  Self-resolving
    next cycle, so not the forever-loop — but it costs the WHOLE cluster its presence for a
    cycle, at exactly the moment the water level decided to move.  An allocation is a
    SIMULTANEOUS statement; applying it sequentially must not change what it means.
    """

    def _slots(self):
        from .. import alloc as A
        return [A.Slot(t, "bid", rho=6.25, S=50.0, p=0.02, phi=0.0, d=0.0, l_eff=0.0,
                       close_ts=NOW + 16 * 3600, program_end_ts=NOW + 16 * 3600)
                for t in ("KXAAAGASD-26JUL29-T1.0", "KXAAAGASD-26JUL29-T2.0")]

    def test_a_reallocation_across_rungs_is_never_self_refused(self):
        m = self.maker(ex=X.FakeExchange(balance_cents=1_000_000))
        m.projected_day_reward = 40.0                         # ⇒ a $10 cluster cap
        ss = self._slots()
        m.requote_pass(NOW + 1, ss, {ss[0].key: 0, ss[1].key: 500})
        self.assertEqual(len(m.orders), 1)
        n_before = len(self.logs)
        # The plan FLIPS: the later rung releases $10, the earlier one claims it.  Total
        # unchanged, and every rung of the pair is alphabetically hostile.
        m.requote_pass(NOW + 60, ss, {ss[0].key: 500, ss[1].key: 0})
        refused = [r for r in self.logs[n_before:]
                   if r.get("t") == "place_refused"
                   and r.get("refused_by") in (CL.REFUSE_WORST, CL.REFUSE_SIGNED)]
        self.assertEqual(refused, [], "the claim was measured against an unreleased rung")
        live = {o["ticker"]: o["remaining"] for o in m.orders.values()
                if o.get("remaining", 0) > 0}
        self.assertEqual(live, {ss[0].ticker: 500.0},
                         "the cluster lost its presence for a whole cycle")


class TestTheReplacementExemption(EngineCase):
    """The pure half of NEW-1a: the cluster measure omits the order being replaced."""

    def test_place_context_omits_the_order_under_replacement(self):
        m = self.maker()
        m.orders["o1"] = {"order_id": "o1", "ticker": TK, "side": "bid", "price": 0.02,
                          "size": 500.0, "remaining": 500.0, "placed_ts": NOW}
        full = m.place_context()
        self.assertEqual(len(full.resting_basis), 1)
        exempt = m.place_context(replacing_order_id="o1")
        self.assertEqual(exempt.resting_basis, [],
                         "the order being REPLACED was still counted as an addition")

    def test_an_unrelated_order_is_never_exempted(self):
        m = self.maker()
        m.orders["o1"] = {"order_id": "o1", "ticker": TK, "side": "bid", "price": 0.02,
                          "size": 500.0, "remaining": 500.0, "placed_ts": NOW}
        ctx = m.place_context(replacing_order_id="somebody-else")
        self.assertEqual(len(ctx.resting_basis), 1)


# =============================================================================================
# NEW-2 — THE HALTED BOOK MUST STILL SEE ITS OWN FILLS.
# =============================================================================================
class TestFillsPollWhileHalted(NewRoundCase):
    """NEW-2 survives the 2026-07-30 law change, with a different order under the microscope.

    LAW CHANGE (owner decision: "it's either running and placing orders, or it's not
    running").  The helper below used to be `_halted_with_a_resting_shed`, and it MANUFACTURED
    its subject by letting the halted closing pass post one — `assertEqual(len(shed), 1, "the
    halted closing pass never posted a shed")`.  A halted bot places nothing now, so there is
    no shed to observe.

    NEW-2'S FINDING IS UNCHANGED AND STILL LOAD-BEARING: a halted process must keep reading
    its own fills.  The order at risk is now an ENTRY quote placed BEFORE the halt whose
    cancel the wire refused (a 503 — B10's UNKNOWN state, which is precisely the case where
    the order is still live and can still fill).  If a halted iteration stopped polling fills,
    that order would fill on the wire and sit in our books as presence forever — the same
    defect NEW-2 named, reached through the door that still exists.
    """

    def _halted_with_a_stuck_entry_order(self):
        ex = ProgExchange(program_body(tickers=(TK,)), {TK: bk("0.40", "1200", "0.58", "1200")},
                          positions=[{"ticker": TK, "position": 20}])
        r = self.runner(ex)
        ok, refusals = r.init(
            NOW, allow_fresh=True,
            adopt_obj={"positions": [{"ticker": TK, "side": "yes", "net": 20.0,
                                      "basis": 0.40}]},
            exchange_positions={(TK, "yes"): 20.0}, marks={(TK, "yes"): 0.41},
            nestor_state=NESTOR)
        self.assertTrue(ok, refusals)
        # An ENTRY quote of ours rests on the wire before the halt.
        okp, why, _ = r.m.place(TK, "ask", 0.58, 20, int(NOW + 8 * 3600), NOW + 1,
                                available_cash_usd=100_000.0)
        self.assertTrue(okp, why)
        oid = [o for o in ex.resting][0]
        # The cancel-all the halt performs cannot remove it: the wire 503s (B10 UNKNOWN —
        # the order may still be live, and here it is).
        ex.cancel_status = 503
        r.m.position_cost[TK] = 1000.0                        # crush the mark: day stop trips
        out = r.iteration(NOW + 2)
        self.assertTrue(out.get("day_stop"))
        self.assertTrue(r.m.halt.halted)
        t = NOW + 3
        for _ in range(4):
            r.iteration(t)
            t += 30.0
        self.assertIn(oid, ex.resting, "the fixture lost its stuck order")
        self.assertEqual([b for b in ex.placed if float(b.get("count", 0)) and
                          b is not ex.placed[0]], [],
                         "the halted bot placed something")
        return r, ex, oid, t

    def test_a_fill_during_the_halt_reaches_our_books(self):
        """THE ADVERSARIAL TEST.  Pre-fix: 600 s of halted iterations and the position and the
        dead order both still sit in our books."""
        r, ex, oid, t = self._halted_with_a_stuck_entry_order()
        ex.take(oid, 20, now=t)
        for _ in range(20):
            r.iteration(t)
            t += 30.0
        # The 20-lot ask fills against the 20 YES we hold: NET goes flat.  (Whether the
        # books record that as a closed YES leg or an opened NO leg is `book_fill_row`'s
        # business — see `TestAFillThatNetsIsStillBookedAsClosing`; either way the NET must
        # move, and pre-NEW-2 it did not move at all.)
        self.assertLess(abs(r.m.net_position(TK)), 1.0,
                        "the stuck order FILLED on the wire and our books never learned")
        live = [o for o in r.m.orders.values() if o.get("remaining", 0) > 0]
        self.assertEqual(live, [], "a dead order is still counted as our presence")

    def test_the_halted_fills_poll_cannot_crash_the_loop(self):
        """Exception-safety: a raising fills read must not take the loop down.  (It used to
        say "and must not cost the closing pass its turn" — there is no closing pass; the
        duty it must not cost is the cash-feed heartbeat, which lives in the sibling try.)"""
        r, ex, oid, t = self._halted_with_a_stuck_entry_order()

        def boom(*_a, **_kw):
            raise RuntimeError("fills exploded")

        r.m.poll_fills_due = boom
        out = r.iteration(t + 30.0)
        self.assertTrue(out.get("halted"))
        self.assertTrue([x for x in self.logs if x.get("t") == "halted_idle_error"],
                        "the failure was swallowed without a trace")

    def test_the_halted_poll_keeps_the_cheap_cadence(self):
        """The halted branch runs at the idle cadence; the fills poll must not turn it into a
        1 Hz read of the fills index."""
        r, ex, oid, t = self._halted_with_a_stuck_entry_order()
        calls = []
        real = r.m.ex.fills
        r.m.ex.fills = lambda *a, **kw: (calls.append(1), real(*a, **kw))[1]
        for _ in range(10):
            t += 1.0
            r.iteration(t)
        self.assertLessEqual(len(calls), 1,
                             "the halted branch polled fills %d times in 10 s" % len(calls))
        # ...and it is a CADENCE, not an absence: past one FILLS_POLL_S it reads.
        r.iteration(t + 2 * C.FILLS_POLL_S)
        self.assertGreaterEqual(len(calls), 1, "the halted branch never polls fills at all")


if __name__ == "__main__":                                    # pragma: no cover
    unittest.main()


class TestAMakerNeverTakes(__import__('unittest').TestCase):
    """Paid live 2026-07-28: a 3c bid onto a rung whose opposing side was AT 3c was taken on
    contact — 4 placements, ~200 contracts, $6.05 — and a fully-taken order leaves nothing
    resting, so the next cycle re-posted into the same trap until the burst breaker halted it.
    Crossing costs the spread AND the presence."""

    def test_a_locked_book_is_never_crossed(self):
        from lip_v5 import quote as Q
        self.assertTrue(Q.would_cross("bid", 0.03, 0.03, 0.03), "bid at the ask is a TAKE")
        self.assertTrue(Q.would_cross("ask", 0.59, 0.59, 0.59), "ask at the bid is a TAKE")

    def test_joining_the_book_is_not_crossing(self):
        from lip_v5 import quote as Q
        self.assertFalse(Q.would_cross("bid", 0.02, 0.02, 0.03))
        self.assertFalse(Q.would_cross("ask", 0.61, 0.59, 0.61))

    def test_an_unseen_opposing_side_does_not_refuse(self):
        """The mirror: refusing on missing data would silently empty the whole book."""
        from lip_v5 import quote as Q
        self.assertFalse(Q.would_cross("bid", 0.03, None, None))
        self.assertFalse(Q.would_cross("ask", 0.50, None, None))

    def test_the_requoter_skips_rather_than_crossing(self):
        """End to end through the assembled requote pass: a locked book places NOTHING."""
        from lip_v5 import alloc, engine, exchange as X
        ex = X.FakeExchange(balance_cents=1_000_000)
        m = engine.Maker(ex, 1785268000.0, live=False)
        m.nestor_orders, m.nestor_positions = set(), set()
        # bid best 3c and ask best 3c on the YES axis => locked.
        bid = alloc.Slot("KXLOCK-26JUL29-T1", "bid", rho=9.0, S=500.0, p=0.03,
                         venue="KXLOCK", hours_left=10.0)
        ask = alloc.Slot("KXLOCK-26JUL29-T1", "ask", rho=9.0, S=500.0, p=0.97,
                         venue="KXLOCK", hours_left=10.0)
        stats = m.requote_pass(1785268000.0, [bid, ask],
                               {bid.key: 50, ask.key: 50})
        self.assertEqual(stats["placed"], 0, "a locked book must place NOTHING")
        self.assertEqual(ex.placed, [], "nothing may reach the wire")


class TestThePlanMeasuresTheSameBookAsTheRails(__import__('unittest').TestCase):
    """Live 2026-07-28: the planner seeded cluster usage from HELD inventory only, while
    `place()` measures positions PLUS resting orders.  So every cycle the plan saw an empty
    RATES cluster, planned a second order into it, and the rails refused — 180 refusals a
    minute per rung, $4.71 deployed of a $300 ceiling."""

    def test_committed_basis_consumes_the_cluster_in_the_PLAN(self):
        """Under the law (2026-07-30) the plan's cluster tally is `cluster_spent`, built by
        the engine from the SAME `place_context()` rows the rails read — this test holds the
        pure half: an envelope fed a committed cluster never plans past the rail's cap."""
        from lip_v5 import alloc, clusters as CL
        mk = lambda tk, p: alloc.Slot(tk, "bid", rho=9.0, S=500.0, p=p, venue="KXUST5AD",
                                      hours_left=10.0, cum_size=2000.0)
        a = mk("KXUST5AD-26JUL29-T4.25", 0.30)
        b = mk("KXUST5AD-26JUL29-T4.35", 0.30)
        ck = CL.cluster_of(a.ticker)
        # $30 of the cluster already committed against a $50 rail cap.
        plan, spent, _ = alloc.allocate_law([a, b], 300.0, cluster_cap_usd=50.0,
                                            cluster_spent={ck: 30.0})
        self.assertLessEqual(spent + 30.0, 50.0 + 1e-9,
                             "the plan must not exceed the cluster the rails will measure")


class TestTheRiskMeasureAgreesWithTheMoney(__import__('unittest').TestCase):
    """Live 2026-07-28: four RATES orders holding ~$2 of real collateral were scored at
    $64.48 against a $50 cluster cap, because a sell-side order's basis was taken as its
    YES-axis price (0.84) rather than what it cost (0.16).  The cluster then refused
    everything and the book deployed $5.76 of a $300 ceiling."""

    def test_a_sell_side_order_is_valued_at_its_collateral(self):
        from lip_v5 import engine, exchange as X
        ex = X.FakeExchange(balance_cents=1_000_000)
        m = engine.Maker(ex, 1785268000.0, live=False)
        m.orders["o1"] = {"order_id": "o1", "coid": "c1", "ticker": "KXUST5AD-26JUL29-T4.31",
                          "side": "ask", "price": 0.84, "size": 100.0, "remaining": 100.0,
                          "placed_ts": 1785268000.0}
        ctx = m.place_context(available_cash_usd=1000.0)
        row = [r for r in ctx.resting_basis if r["ticker"] == "KXUST5AD-26JUL29-T4.31"][0]
        self.assertAlmostEqual(row["basis"], 0.16, places=6,
                               msg="an ask at 0.84 costs 0.16 per contract, not 0.84")
        self.assertAlmostEqual(row["n"] * row["basis"], 16.0, places=4)

    def test_a_buy_side_order_is_unchanged(self):
        from lip_v5 import engine, exchange as X
        ex = X.FakeExchange(balance_cents=1_000_000)
        m = engine.Maker(ex, 1785268000.0, live=False)
        m.orders["o2"] = {"order_id": "o2", "coid": "c2", "ticker": "KXUST5AD-26JUL29-T4.31",
                          "side": "bid", "price": 0.16, "size": 100.0, "remaining": 100.0,
                          "placed_ts": 1785268000.0}
        ctx = m.place_context(available_cash_usd=1000.0)
        row = [r for r in ctx.resting_basis if r["ticker"] == "KXUST5AD-26JUL29-T4.31"][0]
        self.assertAlmostEqual(row["basis"], 0.16, places=6)


class TestADeterminedMarketCarriesNoClusterRisk(__import__('unittest').TestCase):
    """Live 2026-07-28: the 26JUL28 treasury rungs resolved at 1:30pm (status `determined`)
    and were still charged against the RATES cluster cap, blocking the fresh 26JUL29 window
    in the same cluster.  A resolved position is worth exactly $1 or $0 per contract: the
    outcome cannot move, we cannot trade out of it, and no new order can compound it."""

    def _maker_with_position(self):
        from lip_v5 import engine, exchange as X
        ex = X.FakeExchange(balance_cents=1_000_000)
        m = engine.Maker(ex, 1785268000.0, live=False)
        m.nestor_orders, m.nestor_positions = set(), set()
        tk = "KXUST10AD-26JUL28-T4.61"
        m.positions[tk] = {"yes": 100.0, "no": 0.0}
        m.entry_basis[(tk, "yes")] = 0.56
        return m, tk

    def test_an_unresolved_position_consumes_its_cluster(self):
        m, tk = self._maker_with_position()
        ctx = m.place_context(available_cash_usd=1000.0)
        self.assertTrue([p for p in ctx.positions if p["ticker"] == tk],
                        "a LIVE position must still count as correlated risk")

    def test_a_resolved_position_frees_its_cluster(self):
        m, tk = self._maker_with_position()
        m.resolved.add(tk)
        ctx = m.place_context(available_cash_usd=1000.0)
        self.assertEqual([p for p in ctx.positions if p["ticker"] == tk], [],
                         "a determined market cannot lose more; it must not hold risk budget")


class TestTheProspectiveOrderIsMeasuredInCollateral(__import__('unittest').TestCase):
    """The resting-order basis was fixed on 2026-07-28; the PROSPECTIVE order's was missed.
    An ask at a 0.97 yes-price costs 0.03/contract to post and was scored at 0.97, so a
    routine 300-lot land grab read as a $291 order against a $75 cluster cap and was refused
    every cycle, forever."""

    def test_a_sell_side_order_is_scored_at_what_it_costs(self):
        from lip_v5 import engine, exchange as X, guards as G
        ex = X.FakeExchange(balance_cents=1_000_000)
        m = engine.Maker(ex, 1785268000.0, live=False)
        m.nestor_orders, m.nestor_positions = set(), set()
        seen = {}
        orig = G.place_allowed

        def spy(ctx, order):
            seen.update(order)
            return orig(ctx, order)

        G.place_allowed = spy
        try:
            m.place("KXTRUMPACT-26JUL26-T1", "ask", 0.97, 300, 1785271600.0,
                    1785268000.0, available_cash_usd=1000.0)
        finally:
            G.place_allowed = orig
        self.assertAlmostEqual(seen["basis"], 0.03, places=6)
        self.assertAlmostEqual(seen["n"] * seen["basis"], 9.0, places=4,
                               msg="300 lots at 3c is $9 of risk, not $291")
