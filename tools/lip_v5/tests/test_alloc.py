"""ALLOCATE under (★) — spec §1.3, plus the inherited v1 §14.1 invariants that survived
adversarial fire (T1-T7 in spirit), and T-R4b's allocation half."""

import unittest

from .. import alloc, config as C, money as M, ratchet as RT
from .base import LipTestCase

RSTAR = 0.00625


def slot(ticker="T", side="bid", rho=6.25, S=50.0, p=0.50, phi=0.08, d=0.07, l_eff=8.0,
         **kw):
    return alloc.Slot(ticker, side, rho=rho, S=S, p=p, phi=phi, d=d, l_eff=l_eff, **kw)


class TestScoreSide(LipTestCase):
    def test_the_filing_algorithm(self):
        levels = [(40, 600), (39, 500)]
        s = alloc.score_side(levels, target_size=1000)
        self.assertTrue(s.qualifies)
        self.assertAlmostEqual(s.S, 600 + 500 * 0.5, places=9)
        self.assertEqual(s.ref_c, 40)

    def test_the_qualifying_set_is_CLEARED_not_partial(self):
        s = alloc.score_side([(40, 100)], target_size=1000)
        self.assertFalse(s.qualifies)
        self.assertEqual(s.S, 0.0)
        self.assertEqual(s.reason, "target_size_not_reached")

    def test_a_book_at_the_cap_has_no_reference_price(self):
        s = alloc.score_side([(99, 5000)], target_size=1000)
        self.assertEqual(s.reason, "ref_at_cap")
        self.assertEqual(s.S, 0.0)

    def test_levels_mode_is_conservative_for_entry(self):
        levels = [(40, 600), (30, 500)]
        cents = alloc.score_side(levels, 1000, mode=C.S_MODE_RECON).S
        lvls = alloc.score_side(levels, 1000, mode=C.S_MODE_ENTRY).S
        self.assertGreaterEqual(lvls, cents)          # v1 §1.5: S_levels >= S_cents ALWAYS

    def test_pinned_detection(self):
        self.assertTrue(alloc.is_pinned(99, None))
        self.assertTrue(alloc.is_pinned(None, 1))
        self.assertFalse(alloc.is_pinned(50, 52))


class TestAllocate(LipTestCase):
    def test_a_pypl_slot_is_never_funded(self):
        """The whole point: (★) refuses it by three orders of magnitude, so the water level
        never reaches it."""
        s = slot("PYPL", rho=0.439, S=50, p=0.30, phi=0.50, d=0.07, l_eff=3744.0)
        a, spent, _ = alloc.allocate([s], 300.0, RSTAR)
        self.assertEqual(a[("PYPL", "bid")], 0)
        self.assertEqual(spent, 0.0)

    def test_a_treasury_slot_is_funded(self):
        s = slot("TSY")
        a, spent, marg = alloc.allocate([s], 300.0, RSTAR)
        self.assertGreater(a[("TSY", "bid")], 0)
        self.assertGreater(spent, 0.0)
        self.assertGreater(marg, 0.0)

    def test_the_cheap_side_wins_the_water_level(self):
        """(★) tilts cheap — and v5 owns that this is a LARGER exposure than v4's (§4.6)."""
        cheap = slot("GAS", p=0.02, phi=0.001)
        mid = slot("TSY", p=0.50, phi=0.08)
        a, _, _ = alloc.allocate([cheap, mid], 300.0, RSTAR)
        self.assertGreater(a[("GAS", "bid")], 0)

    def test_never_exceeds_the_budget(self):
        slots = [slot("M%d" % i, p=0.10 + 0.01 * i) for i in range(10)]
        for budget in (1.0, 7.5, 50.0, 300.0):
            a, spent, _ = alloc.allocate(slots, budget, RSTAR)
            self.assertLessEqual(spent, budget + 1e-9, "budget %s" % budget)

    def test_a_negative_budget_funds_nothing(self):
        a, spent, _ = alloc.allocate([slot()], -50.0, RSTAR)
        self.assertEqual(spent, 0.0)

    def test_per_slot_inventory_cap_binds(self):
        """v1 §8.1 — floor($10/p) on NET."""
        s = slot("T", p=0.50)
        a, _, _ = alloc.allocate([s], 10_000.0, RSTAR)
        self.assertLessEqual(a[("T", "bid")], alloc.n_cap(0.50))
        self.assertEqual(alloc.n_cap(0.50), 20)
        self.assertEqual(alloc.n_cap(0.02), 500)

    def test_no_lazy_under_fill(self):
        """v1 T5 — an expensive slot that cannot afford one more contract must not abandon the
        budget while a CHEAPER slot could still absorb it."""
        rich = slot("RICH", p=0.90, rho=6.25, S=50)
        cheap = slot("CHEAP", p=0.05, rho=6.25, S=50, phi=0.001)
        a, spent, _ = alloc.allocate([rich, cheap], 40.0, RSTAR)
        self.assertGreater(a[("CHEAP", "bid")], 0)

    def test_excluded_slots_are_never_funded(self):
        for kw in ({"pinned": True}, {"denied": True}, {"legal_price_exists": False},
                   {"p6_ok": False}, {"assume_filled": True}, {"hours_left": 0.0},
                   {"hours_to_start": 10.5}):
            s = slot("X", **kw)
            a, spent, _ = alloc.allocate([s], 300.0, RSTAR)
            self.assertEqual(a[("X", "bid")], 0, "not excluded by %s" % kw)

    def test_the_window_start_guard_admits_inside_the_lead(self):
        """v4's live defect: three WNBA slots whose programs opened 10.5 h later locked ~$11
        while live-window posts were refused on `collateral_ceiling`."""
        a, _, _ = alloc.allocate([slot("SOON", hours_to_start=0.1)], 300.0, RSTAR)
        self.assertGreater(a[("SOON", "bid")], 0)

    def test_the_hard_horizon_exclusion_is_wired_into_allocate(self):
        s = slot("MENTION", rho=0.439, S=50, p=0.30, phi=0.001, d=0.07, l_eff=1.0,
                 close_ts=200 * 86400.0, program_end_ts=30 * 86400.0, hours_left=16.0)
        a, _, _ = alloc.allocate([s], 300.0, RSTAR)
        self.assertEqual(a[("MENTION", "bid")], 0)

    def test_determinism(self):
        slots = [slot("A"), slot("B"), slot("C", p=0.30)]
        first = alloc.allocate(slots, 100.0, RSTAR)[0]
        for _ in range(5):
            self.assertEqual(alloc.allocate(slots, 100.0, RSTAR)[0], first)

    def test_a_higher_rstar_allocates_no_more(self):
        """The monotonicity the r* tie-break depends on."""
        slots = [slot("A", phi=0.5, l_eff=100.0), slot("B", p=0.30, phi=0.4, l_eff=80.0)]
        prev = None
        for r in (0.00625, 0.05, 0.5, 5.0):
            _, spent, _ = alloc.allocate(slots, 300.0, r)
            if prev is not None:
                self.assertLessEqual(spent, prev + 1e-9, "r*=%s" % r)
            prev = spent


class TestVenueCaps(LipTestCase):
    def test_TR4b_sum_allocated_never_exceeds_the_ceiling(self):
        """T-R4b's allocation half: Σ venue caps MAY exceed the global ceiling; Σ ALLOCATED
        never does, because ALLOCATE's budget binds independently."""
        slots, venue_caps = [], {}
        for i in range(6):
            v = "V%d" % i
            slots.append(slot("M%d" % i, venue=v, p=0.10, phi=0.001))
            venue_caps[v] = 100.0                     # 6 x $100 = $600 of permission
        self.assertGreater(sum(venue_caps.values()), 300.0)
        a, spent, _ = alloc.allocate(slots, 300.0, RSTAR, venue_caps=venue_caps)
        self.assertLessEqual(spent, 300.0 + 1e-9)

    def test_a_venue_cap_binds_within_the_budget(self):
        slots = [slot("M1", venue="V", p=0.10, phi=0.001),
                 slot("M2", venue="V", p=0.10, phi=0.001)]
        a, spent, _ = alloc.allocate(slots, 300.0, RSTAR, venue_caps={"V": 5.0})
        self.assertLessEqual(spent, 5.0 + 0.10 + 1e-9)

    def test_a_stood_down_venue_gets_a_zero_cap_and_others_keep_quoting(self):
        """T-R5/T-D1 — a venue standing down must leave every other venue quoting."""
        down = RT.VenueState("V1", rung=3, rung0_cap_usd=10.0)
        down.stood_down = True
        up = RT.VenueState("V2", rung=1, rung0_cap_usd=10.0)
        caps = {"V1": down.cap_usd(100.0, 300.0), "V2": up.cap_usd(100.0, 300.0)}
        slots = [slot("M1", venue="V1", phi=0.001), slot("M2", venue="V2", phi=0.001)]
        a, spent, _ = alloc.allocate(slots, 300.0, RSTAR, venue_caps=caps)
        self.assertEqual(a[("M1", "bid")], 0)
        self.assertGreater(a[("M2", "bid")], 0)

    def test_cap_series_stops_one_venue_tripping_the_global_day_stop(self):
        """§4.4 row 1's NEW per-VENUE cap: no single venue may halt the whole book."""
        day_stop = 105.0
        self.assertAlmostEqual(C.cap_series_usd(day_stop), 52.5, places=9)
        self.assertLess(C.cap_series_usd(day_stop), day_stop)
        self.assertGreaterEqual(C.cap_series_usd(1.0), C.INV_CAP_USD)


class TestDerivedSlotCap(LipTestCase):
    """CHARTER AMENDMENT (Ryan, finish round): the flat $10 per-rung cap was inherited, not
    derived.  Per-rung size now derives from (a) (★)'s own share saturation and (b) the day
    stop — no single rung's worst case may trip it alone (0.5×, the cluster/series factor)."""

    def test_the_cap_derives_from_the_day_stop(self):
        self.assertAlmostEqual(C.slot_cap_usd(20.0), 10.0, places=9)    # floor day
        self.assertAlmostEqual(C.slot_cap_usd(100.0), 50.0, places=9)   # Ryan's $50
        self.assertAlmostEqual(C.slot_cap_usd(150.0), 75.0, places=9)   # at the day-stop cap
        # the surviving constant is the FLOOR, and the floor itself is derived
        self.assertAlmostEqual(C.INV_CAP_USD, 0.5 * C.DAY_STOP_FLOOR_USD, places=9)

    def test_a_contested_rung_whose_reward_supports_50_gets_50(self):
        """Amendment T1: marginal rate supports it, cluster bounds allow it ⇒ ~$50 lands."""
        from .. import clusters as CL
        s = slot("KXBIG-1", p=0.50, S=50, phi=0.01)      # contested share, thin fill risk
        caps = alloc.Caps(inv_cap_usd=C.slot_cap_usd(100.0))   # day stop $100 ⇒ cap $50
        a, spent, _ = alloc.allocate([s], 300.0, RSTAR, caps=caps)
        self.assertAlmostEqual(spent, 50.0, places=6)
        # ...and the SAME day stop's cluster cap admits the order it sized
        ok, reason, _ = CL.cluster_admits(
            [], {"ticker": "KXBIG-1", "side": "yes", "n": a[s.key], "basis": 0.50},
            CL.cluster_cap_usd(100.0))
        self.assertTrue(ok, reason)

    def test_a_rung_where_share_saturates_stays_small_despite_the_cap(self):
        """Amendment T2: sizing is bought by MARGINAL reward — owning the book pays nothing
        more (note 43 §7 saturation), so a thin-S rung stops by arithmetic, not by cap."""
        s = slot("KXSAT-1", p=0.50, S=2, phi=0.08)
        caps = alloc.Caps(inv_cap_usd=C.slot_cap_usd(100.0))   # cap $50: NOT the binder
        a, spent, _ = alloc.allocate([s], 300.0, RSTAR, caps=caps)
        self.assertGreater(spent, 0.0)
        self.assertLess(spent, 15.0)

    def test_the_old_flat_cap_would_have_refused_the_50(self):
        """The defect, pinned: the same rung under the inherited flat floor stops at ~$10."""
        s = slot("KXBIG-1", p=0.50, S=50, phi=0.01)
        a, spent, _ = alloc.allocate([s], 300.0, RSTAR,
                                     caps=alloc.Caps(inv_cap_usd=C.INV_CAP_USD))
        self.assertLessEqual(spent, C.INV_CAP_USD + 1e-9)


class TestForfeitGate(LipTestCase):
    def test_a_program_that_cannot_clear_the_floor_is_dropped(self):
        """v1 §3.1 — enter iff the PERIOD projection clears $2.00."""
        s = slot("TINY", rho=0.05, S=50, p=0.02, phi=0.001, window_h=16.0, hours_left=16.0,
                 program_id="P1")
        a, spent, marg, dropped = alloc.allocate_with_forfeit_gate([s], 300.0, RSTAR)
        if a[("TINY", "bid")] == 0:
            self.assertEqual(spent, 0.0)

    def test_a_healthy_program_survives_the_gate(self):
        s = slot("TSY", program_id="P1")
        a, spent, marg, dropped = alloc.allocate_with_forfeit_gate([s], 300.0, RSTAR)
        self.assertNotIn("P1", dropped)
        self.assertGreater(a[("TSY", "bid")], 0)

    def test_projection_scales_only_the_unaccrued_portion(self):
        """v4's C3 defect: a program with 2 h left on a 228 h window projected as if it had
        all 228, and a gate that mis-grades PERMISSIVELY launders a bad entry as a checked
        one."""
        s = slot("T", window_h=228.0, hours_left=2.0, accrued=0.0)
        proj = alloc.projected_period_payout([s], {("T", "bid"): 20})
        share = alloc.our_share(20, 50)
        self.assertAlmostEqual(proj, share * (6.25 / 2) * 2.0, places=9)

    def test_accrued_is_already_banked(self):
        s = slot("T", hours_left=0.0, accrued=5.0)
        self.assertAlmostEqual(alloc.projected_period_payout([s], {("T", "bid"): 10}), 5.0)


class TestReserveBudget(LipTestCase):
    def test_make_before_break_reserve(self):
        """v1 §2.4 B3 — MBB transiently holds TWO copies of one slot's collateral."""
        self.assertAlmostEqual(alloc.reserve_budget(300.0, 25.0), 275.0)
        self.assertEqual(alloc.reserve_budget(10.0, 25.0), 0.0)


class TestRStarIntegration(LipTestCase):
    def test_allocate_with_rstar_returns_a_usable_allocation(self):
        slots = [slot("TSY"), slot("GAS", p=0.02, phi=0.001)]
        a, spent, res = alloc.allocate_with_rstar(slots, 300.0, trailing_rate=0.02)
        self.assertLessEqual(spent, 300.0 + 1e-9)
        self.assertGreater(res.r_star, 0.0)
        self.assertGreaterEqual(len(res.trace), 2)

    def test_cold_start_seeds_at_the_floor(self):
        slots = [slot("TSY")]
        _, _, res = alloc.allocate_with_rstar(slots, 300.0, trailing_rate=None)
        self.assertEqual(res.trace[0], C.FLOOR_RATE_PER_H)


if __name__ == "__main__":
    unittest.main()
