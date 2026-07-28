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


def bk(yes_px="0.02", yes_sz="1200", no_px="0.97", no_sz="1200"):
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
        """A venue past its ramp: the §1.4 rung cap no longer binds, so the CLUSTER cap is
        the guard under test rather than the ratchet."""
        v = m.venues[series]
        v.verified = True
        v.rung = rung
        return v

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
        self.assertAlmostEqual(float(old["price"]), 0.02, places=6)
        ex.books[TK] = bk("0.03", "3000", "0.96", "1200")     # rivals lift the best
        t = NOW + 40
        for _ in range(6):
            r.iteration(t)
            t += 1.0
        prices = sorted({round(float(b["price"]), 4) for b in ex.placed})
        self.assertIn(0.03, prices,
                      "NO order at the NEW best ever reached the exchange: %s" % (prices,))

    def test_the_deadlock_leaves_no_cluster_refusal_behind(self):
        """The refusal is the deadlock's own fingerprint: a make-before-break REPLACEMENT is
        not an addition, so it must never be measured against the cluster as one."""
        r, ex = self._armed()
        ex.books[TK] = bk("0.03", "3000", "0.96", "1200")
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
        ex.books[TK] = bk("0.03", "3000", "0.96", "1200")
        t = NOW + 40
        for _ in range(6):
            r.iteration(t)
            t += 1.0
        live = [o for o in r.m.orders.values() if o.get("remaining", 0) > 0]
        self.assertEqual(len(live), 1, "expected exactly one live quote, got %s" % (live,))
        self.assertAlmostEqual(float(live[0]["price"]), 0.03, places=6)


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
        cap = CL.cluster_cap_usd(G.day_stop_usd(r.m.projected_day_reward))
        rested = sum(float(b["count"]) * float(b["price"]) for b in ex.resting.values())
        self.assertLessEqual(rested, cap + 1e-9)
        self.assertGreater(rested, 0.0, "nothing rests at all")


class TestAllocateCarriesTheClusterTerm(LipTestCase):
    """The pure half of NEW-1b: the water level itself refuses to over-plan a cluster."""

    def slots(self, n=4, p=0.02):
        return [alloc.Slot("KXAAAGASD-26JUL29-T%d.0" % i, "bid", rho=6.25, S=50.0, p=p,
                           phi=0.0, d=0.0, l_eff=0.0)
                for i in range(1, n + 1)]

    def test_one_cluster_is_capped_across_all_its_rungs(self):
        a, spent, _ = alloc.allocate(self.slots(), 300.0, 0.0,
                                     caps=alloc.Caps(inv_cap_usd=10.0),
                                     cluster_cap_usd=10.0)
        self.assertLessEqual(spent, 10.0 + 1e-9)
        self.assertGreater(sum(a.values()), 0)

    def test_separate_clusters_each_get_their_own_cap(self):
        ss = [alloc.Slot("KXGAS-26JUL29-T1.0", "bid", rho=6.25, S=50.0, p=0.02,
                         phi=0.0, d=0.0, l_eff=0.0),
              alloc.Slot("KXIDX-26JUL29-T1.0", "bid", rho=6.25, S=50.0, p=0.02,
                         phi=0.0, d=0.0, l_eff=0.0)]
        a, spent, _ = alloc.allocate(ss, 300.0, 0.0, caps=alloc.Caps(inv_cap_usd=10.0),
                                     cluster_cap_usd=10.0)
        self.assertGreater(a[ss[0].key] * 0.02, 0.0)
        self.assertGreater(a[ss[1].key] * 0.02, 0.0)
        self.assertLessEqual(spent, 20.0 + 1e-9)

    def test_held_inventory_consumes_the_clusters_room(self):
        ss = self.slots(n=2)
        held = {ss[0].key: 500.0}                              # $10 of held at 2c: cap full
        a, spent, _ = alloc.allocate(ss, 300.0, 0.0, caps=alloc.Caps(inv_cap_usd=10.0),
                                     cluster_cap_usd=10.0, held=held)
        self.assertEqual(spent, 0.0)
        self.assertEqual(sum(a.values()), 0)

    def test_no_cluster_cap_is_the_old_behaviour(self):
        a, spent, _ = alloc.allocate(self.slots(), 300.0, 0.0,
                                     caps=alloc.Caps(inv_cap_usd=10.0))
        self.assertGreater(spent, 10.0)


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
        m.requote_pass(NOW + 1, ss, {ss[0].key: 0, ss[1].key: 500}, 0.0)
        self.assertEqual(len(m.orders), 1)
        n_before = len(self.logs)
        # The plan FLIPS: the later rung releases $10, the earlier one claims it.  Total
        # unchanged, and every rung of the pair is alphabetically hostile.
        m.requote_pass(NOW + 60, ss, {ss[0].key: 500, ss[1].key: 0}, 0.0)
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
    def _halted_with_a_resting_shed(self):
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
        r.iteration(NOW + 1)
        r.m.position_cost[TK] = 1000.0                        # crush the mark: day stop trips
        out = r.iteration(NOW + 2)
        self.assertTrue(out.get("day_stop"))
        self.assertTrue(r.m.halt.halted)
        t = NOW + 3
        for _ in range(4):
            r.iteration(t)
            t += 30.0
        shed = [oid for oid, b in ex.resting.items() if b["side"] == "ask"]
        self.assertEqual(len(shed), 1, "the halted closing pass never posted a shed")
        return r, ex, shed[0], t

    def test_a_fill_during_the_halt_reaches_our_books(self):
        """THE ADVERSARIAL TEST.  Pre-fix: 600 s of halted iterations and the position and the
        dead order both still sit in our books."""
        r, ex, oid, t = self._halted_with_a_resting_shed()
        ex.take(oid, 20, now=t)
        for _ in range(20):
            r.iteration(t)
            t += 30.0
        self.assertLess(abs(r.m.net_position(TK)), 1.0,
                        "the halted shed FILLED on the wire and our books never learned")
        live = [o for o in r.m.orders.values() if o.get("remaining", 0) > 0]
        self.assertEqual(live, [], "a dead order is still counted as our presence")

    def test_the_halted_fills_poll_cannot_crash_the_loop(self):
        """SF-3's exception-safety, extended over the new call: a raising fills read must not
        take the loop down, and must not cost the closing pass its turn."""
        r, ex, oid, t = self._halted_with_a_resting_shed()

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
        r, ex, oid, t = self._halted_with_a_resting_shed()
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
