"""NOTE 52 — the presence-reserve strategy round (settled with Ryan 2026-07-29 night).

D4  the settlement gate (market close, not program window, decides entry)
D5  one rung per cluster; cluster reserve = ceiling/N
D6  the lot container; replenish re-posts the SAME lot; "fewer rungs, never smaller lots"
D11 the variance instrument lives in the PLAN, not only the rail
D12 a funded rung is never shrunk, zeroed, or evicted mid-period by re-planning

Every guard here is MUTATION-CHECKED by construction of the assertions: each test funds or
refuses through the assembled path, so deleting the guard flips the observable, not a mock.
"""

import unittest

from .. import alloc, config as C, scan
from .base import LipTestCase
from .test_d2round import Table, prog, sides
from .test_engine import NOW

RSTAR = 0.00625


def _slot(tk, cluster_series=None, p=0.12, S=100.0, rho=6.25, **kw):
    kw.setdefault("phi", 0.001)
    kw.setdefault("d", 0.0)
    kw.setdefault("l_eff", 8.0)
    kw.setdefault("hours_left", 16.0)
    kw.setdefault("window_h", 16.0)
    return alloc.Slot(tk, "bid", rho=rho, S=S, p=p,
                      venue=cluster_series or tk.split("-")[0], **kw)


class TestConfigIdentities(LipTestCase):
    """The cap stack is ONE derivation; these identities are what stops its three constants
    drifting apart silently (the B16 'replaces it' lesson, applied in advance)."""

    def test_lot_times_reserve_is_the_cluster_reserve(self):
        """lot = reserve/2: at least ONE re-post for the largest admissible lot; refills per
        rung are EMERGENT (reserve/lot − 1), not fixed — the measured board's median
        cost-to-clear ($3.68) must fit or the book starves (measured: it did)."""
        self.assertAlmostEqual(C.SLOT_LOT_CAP_USD * 2.0,
                               300.0 / C.N_TARGET_CLUSTERS, places=9)

    def test_the_entry_floor_IS_the_credit_target(self):
        self.assertAlmostEqual(C.ENTRY_FLOOR_USD,
                               C.CREDIT_TARGET_USD * C.CREDIT_TARGET_MARGIN, places=9)

    def test_the_day_stop_bound_holds_transitively(self):
        """ceiling/N ≤ 0.5 × day_stop_floor(= 0.2×ceiling) ⇔ N ≥ 10."""
        self.assertGreaterEqual(C.N_TARGET_CLUSTERS, 10)

    def test_the_inv_cap_is_the_lot_container(self):
        self.assertAlmostEqual(C.INV_CAP_USD, C.SLOT_LOT_CAP_USD, places=9)
        self.assertAlmostEqual(C.slot_cap_usd(9999.0), C.SLOT_LOT_CAP_USD, places=9)

    def test_the_horizon_grace_is_the_settlement_horizon(self):
        self.assertAlmostEqual(C.HORIZON_GRACE_H, C.SETTLE_HORIZON_H, places=9)


class TestD4SettlementGate(LipTestCase):
    def test_a_far_settling_market_is_refused_at_entry(self):
        t = Table(close_ts=NOW + (C.SETTLE_HORIZON_H + 48) * 3600)
        self.assertEqual(scan.build_slots([prog()], t, NOW), [])
        self.assertTrue(self.logs_of("settle_horizon_refused"))

    def test_an_unknown_close_REFUSES_entry(self):
        """The prog-end fallback makes a 2032 market wearing a 5-day program look NEAR —
        exactly the wrong direction for this gate, so unknown refuses."""
        t = Table(close_ts=None)
        self.assertEqual(scan.build_slots([prog()], t, NOW), [])
        self.assertTrue(self.logs_of("settle_close_unknown"))

    def test_a_near_settling_market_is_admitted(self):
        t = Table(close_ts=NOW + 24 * 3600)
        self.assertEqual(sides(scan.build_slots([prog()], t, NOW)), ["ask", "bid"])

    def test_held_is_exempt_from_both_refusals(self):
        """D1: a market we are inside is not asking an entry question — the shed and the
        requote must keep their slot whatever the close says."""
        from .test_d2round import TK
        for t in (Table(close_ts=None),
                  Table(close_ts=NOW + (C.SETTLE_HORIZON_H + 48) * 3600)):
            slots = scan.build_slots([prog()], t, NOW, held={TK})
            self.assertEqual(sides(slots), ["ask", "bid"], t.table[TK]["close_ts"])

    def test_candidates_stop_paying_classify_budget_for_known_far_closes(self):
        cl = scan.Classifier()
        p = prog()
        cl.close_ts[p["tickers"][0]] = NOW + (C.SETTLE_HORIZON_H + 48) * 3600
        self.assertEqual(cl.candidates([p], NOW), [])
        cl2 = scan.Classifier()                       # unknown close stays IN (to be learned)
        self.assertEqual(len(cl2.candidates([p], NOW)), 1)


class TestD5OneRungPerCluster(LipTestCase):
    def test_a_second_rung_in_the_same_cluster_is_refused(self):
        ss = [_slot("KXG-1-T1"), _slot("KXG-1-T2")]      # same series ⇒ one cluster
        a, spent, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 1)

    def test_two_clusters_get_one_rung_each(self):
        ss = [_slot("KXG-1-T1"), _slot("KXH-1-T1")]
        a, spent, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 2)

    def test_money_already_in_a_cluster_owns_it(self):
        """held/resting money makes its key the owner: the plan may grow THAT rung and no
        other in the cluster."""
        ss = [_slot("KXG-1-T1"), _slot("KXG-1-T2")]
        a, _, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0,
                                 resting={ss[1].key: 5.0})
        self.assertEqual(a[ss[0].key], 0, "the un-owned rung must not be funded")
        self.assertGreater(a[ss[1].key], 0)

    def test_a_zeroed_rung_frees_its_cluster(self):
        """A rung the cliff pass drops (cannot clear) hands the cluster to the next
        candidate rather than squatting on it."""
        hopeless = _slot("KXG-1-T1", rho=0.05, S=5000.0)  # side pool $0.40: unclearable
        good = _slot("KXG-1-T2")
        a, _, _ = alloc.allocate([hopeless, good], 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertEqual(a[hopeless.key], 0)
        self.assertGreater(a[good.key], 0)


class TestD6LotSemantics(LipTestCase):
    def test_the_lot_container_bounds_the_resting_order(self):
        s = _slot("KXG-1-T1", p=0.10)
        a, spent, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertLessEqual(a[s.key] * s.p, C.SLOT_LOT_CAP_USD + 1e-9)

    def test_the_replenish_reposts_the_SAME_lot_not_the_difference(self):
        """v1 §8.1's NET cap killed presence on the first fill (held ate the room).  The
        reserve semantics: the lot re-posts whole; cumulative acquisition is the cluster
        reserve's job."""
        s = _slot("KXG-1-T1", p=0.10)
        a0, _, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0)
        a1, _, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0,
                                  held={s.key: float(a0[s.key])})
        self.assertEqual(a1[s.key], a0[s.key], "the SAME lot must re-post after a fill")

    def test_the_reserve_ends_the_replenish_after_its_refills(self):
        """(1 + refills) lots of cumulative acquisition = the reserve; the next re-post is
        refused at the cluster term, cleanly, and the period ends for that rung."""
        s = _slot("KXG-1-T1", p=0.10)
        lot = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0)[0][s.key]
        held_full = float(lot * (1 + C.RUNG_REFILLS))
        a, _, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 held={s.key: held_full})
        self.assertLessEqual(a[s.key], max(0, int((10.0 - held_full * 0.10) / 0.10)))


class TestD11PlanSideVariance(LipTestCase):
    def _cheap(self, i):
        # 2c rungs across DISTINCT clusters: individually harmless, jointly the ruin book
        return _slot("KXC%02d-1-T1" % i, p=0.02, S=100.0)

    def test_a_cheap_book_is_stopped_by_the_plan_not_the_rail(self):
        ss = [self._cheap(i) for i in range(40)]
        a, spent, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0,
                                     ceiling_usd=300.0)
        funded = [k for k, q in a.items() if q > 0]
        # charged at the cluster RESERVE ($10, what a funded cluster can become), 2c carries
        # (10/300)^2 x 49 = 0.0544 of V per cluster -> the tolerance holds ~4, never 40
        self.assertLess(len(funded), 8, "the plan admitted a 2c book: no variance "
                                        "instrument in the planner")
        self.assertGreater(len(funded), 0, "the instrument must steer, not shut the book")
        v = sum((10.0 / 300.0) ** 2 * (1 - 0.02) / 0.02 for _ in funded)
        self.assertLessEqual(v, C.PORTFOLIO_VAR_MAX + 0.06)

    def test_without_a_ceiling_the_test_is_off_pure_test_compat(self):
        ss = [self._cheap(i) for i in range(40)]
        a, _, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 40)

    def test_a_mid_priced_book_is_not_blocked(self):
        ss = [_slot("KXM%02d-1-T1" % i, p=0.15) for i in range(30)]
        a, _, _ = alloc.allocate(ss, 300.0, RSTAR, cluster_cap_usd=10.0,
                                 ceiling_usd=300.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 30)

    def test_the_steering_a_dearer_rung_passes_where_a_cheap_one_was_blocked(self):
        """The whole point of plan-side: skipped ≠ refused-forever — the book's AVERAGE is
        steered by admitting the dearer candidate once cheap has eaten the tolerance."""
        cheap = [self._cheap(i) for i in range(40)]
        dear = _slot("KXDEAR-1-T1", p=0.40, S=100.0)
        a, _, _ = alloc.allocate(cheap + [dear], 300.0, RSTAR, cluster_cap_usd=10.0,
                                 ceiling_usd=300.0)
        self.assertGreater(a[dear.key], 0, "the dear rung must pass the variance test even "
                                           "with the cheap tolerance consumed")


class TestD12PeriodLock(LipTestCase):
    def test_a_funded_rung_is_not_zeroed_by_the_cliff_pass(self):
        """Un-funded, this rung cannot clear the floor and is dropped; funded (money resting),
        it holds — zeroing cancels the order and forfeits the whole $1.00 for a fraction."""
        s = _slot("KXG-1-T1", p=0.10, S=3000.0, rho=0.60)  # floor needs > container: sub-cliff
        a0, _, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0)
        self.assertEqual(a0[s.key], 0, "control: unfunded, the cliff pass drops it")
        a1, _, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0,
                                  resting={s.key: 20.0})
        self.assertGreaterEqual(a1[s.key], 20, "funded, the rung must hold its size (D12)")

    def test_a_funded_program_is_not_dropped_by_the_entry_floor(self):
        s = _slot("KXG-1-T1", p=0.10, S=3000.0, rho=0.60, program_id="PL")
        a, _, _, dropped = alloc.allocate_with_forfeit_gate(
            [s], 300.0, RSTAR, cluster_cap_usd=10.0, resting={s.key: 20.0})
        self.assertNotIn("PL", dropped)
        self.assertGreaterEqual(a[s.key], 20)

    def test_an_unmeasured_p_recover_cannot_evict_a_funded_rung(self):
        """The churn engine, pinned: rivals deepen the book, the floor recedes, rescue's
        p_recover defaults to 0 and ABANDON evicts a funded rung mid-period.  While the
        cliff is REACHABLE, the funded rung holds (note 49 R1: no number enters a decision
        naked)."""
        s = _slot("KXG-1-T1", p=0.10, S=3000.0, rho=1.0, program_id="PL", accrued=0.30)
        a, _, _, dropped = alloc.allocate_with_forfeit_gate(
            [s], 300.0, RSTAR, cluster_cap_usd=10.0, resting={s.key: 20.0})
        self.assertNotIn("PL", dropped)
        self.assertTrue(self.logs_of("cliff_hold_funded") or a[s.key] >= 20)

    def test_dead_accrual_still_abandons_funded_or_not(self):
        """The mirror: a cliff UNREACHABLE at the ρ/2 ceiling is a COMPUTED zero, not a
        defaulted one — the abandon stands even for a funded rung."""
        s = _slot("KXG-1-T1", p=0.10, S=3000.0, rho=0.05, hours_left=1.0,
                  program_id="PL", accrued=0.30)          # ceiling: 0.30+0.025 < $1.10
        a, _, _, dropped = alloc.allocate_with_forfeit_gate(
            [s], 300.0, RSTAR, cluster_cap_usd=10.0, resting={s.key: 20.0})
        self.assertIn("PL", dropped)


if __name__ == "__main__":
    unittest.main()
