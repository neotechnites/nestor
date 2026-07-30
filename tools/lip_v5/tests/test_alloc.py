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
        """The note-52 LOT CONTAINER ($5.00 = reserve/2): floor(5.00/p)."""
        s = slot("T", p=0.50)
        a, _, _ = alloc.allocate([s], 10_000.0, RSTAR)
        self.assertLessEqual(a[("T", "bid")], alloc.n_cap(0.50))
        self.assertEqual(alloc.n_cap(0.50), 10)
        self.assertEqual(alloc.n_cap(0.02), 250)

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
        """SUPERSEDED IN DERIVATION (note 52 D6): the per-order cap is the LOT CONTAINER —
        ceiling/(N × (1+refills)) — and no longer moves with the day stop; the day-stop bound
        holds transitively through the cluster reserve (asserted in test_config)."""
        self.assertAlmostEqual(C.slot_cap_usd(20.0), C.SLOT_LOT_CAP_USD, places=9)
        self.assertAlmostEqual(C.slot_cap_usd(150.0), C.SLOT_LOT_CAP_USD, places=9)
        self.assertAlmostEqual(C.slot_cap_usd(0.0, ceiling_usd=300.0), 5.00, places=9)
        self.assertAlmostEqual(C.slot_cap_usd(0.0, ceiling_usd=600.0), 10.00, places=9)
        # the surviving constant IS the lot container, and the identity is the derivation
        self.assertAlmostEqual(C.INV_CAP_USD, C.SLOT_LOT_CAP_USD, places=9)

    def test_a_contested_rung_is_bounded_by_the_PER_MARKET_cap_not_the_slot_cap(self):
        """WAS `test_a_contested_rung_whose_reward_supports_50_gets_50`, asserting $50.

        D2 supersedes the $50: `PER_MARKET_BUDGET_FRAC` is now `MARKET_CAP_FRAC` (0.10), so on a
        $300 budget one MARKET may hold $30 — and $30 < the $50 slot cap, so at this budget the
        per-market cap binds FIRST.  That is the variance constraint working, not a regression:
        MARKET_CAP_FRAC's own derivation is that a single weight above 0.10 collapses
        `N_eff = 1/Σwᵢ²` below ~20, and $50 of $300 is a weight of 0.167 (N_eff ≈ 6 for that
        market).  The charter's "$50 per rung at a day stop ≥ $100" predates that derivation and
        `slot_cap_usd`'s docstring already restated it as "$50 per CLUSTER".
        The plan ⊆ rail chain still holds: plan gross $30 ≤ rail per-leg
        `max(slot_cap, 0.10 × ceiling)` = $30.
        """
        from .. import clusters as CL
        s = slot("KXBIG-1", p=0.50, S=50, phi=0.01)      # contested share, thin fill risk
        caps = alloc.Caps(inv_cap_usd=50.0)   # explicit $50: tests the CAP HIERARCHY, not
                                              # the constant (the live lot container is $2.50)
        a, spent, _ = alloc.allocate([s], 300.0, RSTAR, caps=caps)
        self.assertAlmostEqual(spent, C.MARKET_CAP_FRAC * 300.0, places=6)
        self.assertLess(spent, 50.0, "the per-market cap must bind before the slot cap here")
        # ...and the SAME day stop's cluster cap admits the order it sized
        ok, reason, _ = CL.cluster_admits(
            [], {"ticker": "KXBIG-1", "side": "yes", "n": a[s.key], "basis": 0.50},
            CL.cluster_cap_usd(100.0))
        self.assertTrue(ok, reason)

    def test_a_rung_where_share_saturates_stays_small_despite_the_cap(self):
        """Amendment T2: sizing is bought by MARGINAL reward — owning the book pays nothing
        more (note 43 §7 saturation), so a thin-S rung stops by arithmetic, not by cap."""
        s = slot("KXSAT-1", p=0.50, S=2, phi=0.08)
        caps = alloc.Caps(inv_cap_usd=50.0)   # explicit $50 so the cap is NOT the binder —
                                              # this test is about saturation arithmetic
        a, spent, _ = alloc.allocate([s], 300.0, RSTAR, caps=caps)
        self.assertGreater(spent, 0.0)
        # SATURATION, not the cap, is what stops it: the rung takes ~$18 of a $50 cap and
        # lands at ~95% of the side's score, where further size buys fill risk rather than
        # share.  (The old $15 bound was calibrated to the pre-2026-07-28 admission hurdle of
        # 0.00625/h, which sat ABOVE the rate this program actually pays.)
        self.assertLess(spent, caps.inv_cap_usd,
                        "share saturation must bind before the per-rung cap does")
        self.assertLess(spent, 25.0)

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


class TestCliffRecovery(LipTestCase):
    """SECOND CHARTER AMENDMENT (Ryan): the forfeit cliff.  Today's tape: $4.82 estimated
    across 16 programs, only $2.38 above the $1 cliffs; $0.87 and $0.83 forfeiting.  These
    fixtures pin both directions at A = $0.70."""

    # A starved rung with 70¢ at stake: q=4 resting, S=100 rivals, ρ=$0.50/h pool rate.
    CLIFF = dict(A=0.70, h=8.0, rho=0.5, S=100.0, q=4, p=0.5, r_star=0.00625,
                 C_slot=2.0, phi=0.02, d=0.07)

    def _rescue(self, **over):
        kw = dict(self.CLIFF)
        kw.update(over)
        kw["rate_now"] = alloc.reward_rate(kw["rho"], kw["q"], kw["S"])
        return alloc.rescue(**kw)

    def test_at_70c_the_next_30c_is_worth_a_dollar_plus(self):
        """THE 70¢ CLIFF-RECOVERY FIXTURE.  Reaching $1.10 needs qq=25 (Δq=21); its cost is
        redeploy $0.625 + fill $0.28 = $0.905.  The next 40¢ of NEW accrual alone ($0.40)
        does NOT cover that — only the recovered 70¢ does (0.40 < 0.905 < 1.10).  A rescue
        that priced the marginal accrual at face value would forfeit; this one tops up."""
        res = self._rescue(max_q=100)                     # amendment-1 cap: day stop $100
        self.assertEqual(res.action, alloc.TOP_UP)
        self.assertEqual(res.delta_q, 21)
        self.assertAlmostEqual(res.proj, 1.10, places=9)
        # the load-bearing arithmetic, pinned: new accrual alone loses, A+new wins
        new_accrual = alloc.reward_rate(0.5, 25, 100.0) * 8.0
        cost = (2.0 + 21 * 0.5) * 0.00625 * 8.0 + 0.02 * 0.07 * 25 * 8.0
        self.assertLess(new_accrual, cost)                # 0.40 < 0.905: naive forfeits
        self.assertGreater(0.70 + new_accrual, cost)      # the stranded 70¢ decides

    def test_the_amendments_compose_the_flat_cap_could_not_reach_the_cliff(self):
        """Under the inherited flat $10 rung (n_cap = 20 at 50c), qq=25 is UNREACHABLE and
        the same program is ABANDONED — bigger derived rungs make cliff-clearing easier,
        which is exactly the first amendment's composition clause."""
        res = self._rescue(max_q=alloc.n_cap(0.5))        # flat cap ⇒ 20 < 25
        self.assertEqual(res.action, alloc.ABANDON)

    def test_a_genuinely_unreachable_cliff_is_abandoned(self):
        """Don't throw good money after dead accrual: at h=1 even the ρ/2 ceiling attains
        $0.95 < $1.10, so P(recover) = 0 by construction and redeploy wins."""
        res = self._rescue(h=1.0, max_q=100)
        self.assertEqual(res.action, alloc.ABANDON)
        self.assertGreater(res.abandon_value, res.hold_value)

    def test_a_healthy_projection_keeps(self):
        res = self._rescue(rho=6.25, q=25, max_q=100)
        self.assertEqual(res.action, alloc.KEEP)

    def test_hold_when_alone_and_recovery_is_possible(self):
        """§3.7: with one live program abandon_value is 0 identically, so HOLD wins while
        the cliff is still attainable and fill risk is small."""
        res = self._rescue(max_q=100, r_star=0.5, has_other_program=False, p_recover=0.5)
        self.assertEqual(res.action, alloc.HOLD)

    # ---- the gate, both directions -----------------------------------------------------
    def _cliff_slot(self, **kw):
        kw.setdefault("hours_left", 8.0)
        kw.setdefault("window_h", 8.0)
        kw.setdefault("accrued", 0.70)
        kw.setdefault("S", 100.0)
        return slot("KXCLIFF-1", rho=0.5, p=0.5, phi=0.02, d=0.07, l_eff=8.0,
                    t_hat=1.0, program_id="PC", **kw)

    def test_the_gate_rescues_a_cliff_recoverable_program(self):
        """(★) refuses the rung (marginal net < floor), the $2 entry floor is unreachable —
        and the program is still NOT dropped, because 70¢ is at stake and $1.10 is
        reachable: the top-up Δq is applied so the requoter posts it."""
        s = self._cliff_slot()
        caps = alloc.Caps(inv_cap_usd=50.0)   # explicit: this tests the GATE, not the
                                              # live lot container (which is $2.50)
        a, spent, marg, dropped = alloc.allocate_with_forfeit_gate([s], 300.0, RSTAR,
                                                                   caps=caps)
        self.assertNotIn("PC", dropped)
        self.assertEqual(a[s.key], 25)                    # the Δq that reaches $1.10
        self.assertAlmostEqual(spent, 12.5, places=9)
        self.assertTrue(self.logs_of("cliff_top_up"))

    def test_the_gate_abandons_dead_accrual_and_frees_its_dollars(self):
        """A rung (★) still funds (S=20 ⇒ marginal admits) whose 70¢ is DEAD at h=1 (even
        ρ/2 attains $0.95 < $1.10): the gate drops it, freeing the collateral the water
        level had committed — good money stops following dead accrual."""
        s = self._cliff_slot(S=20.0, hours_left=1.0, window_h=8.0)
        caps = alloc.Caps(inv_cap_usd=C.slot_cap_usd(100.0))
        a, spent, marg, dropped = alloc.allocate_with_forfeit_gate([s], 300.0, RSTAR,
                                                                   caps=caps)
        self.assertIn("PC", dropped)
        self.assertEqual(a[s.key], 0)
        self.assertTrue(self.logs_of("cliff_abandon"))

    def test_under_the_flat_cap_the_same_program_is_not_rescued(self):
        """Composition with amendment 1, at the gate: the flat $10 cap cannot reach qq=25,
        so no top-up posts and the 70¢ rides to forfeit — exactly today's tape.  (It parks
        as an unfunded HOLD, not a drop: with zero committed there is nothing to free.)"""
        s = self._cliff_slot()
        a, _, _, dropped = alloc.allocate_with_forfeit_gate(
            [s], 300.0, RSTAR, caps=alloc.Caps(inv_cap_usd=C.INV_CAP_USD))
        self.assertEqual(a[s.key], 0)
        self.assertFalse(self.logs_of("cliff_top_up"))

    def test_zero_accrual_below_the_floor_still_drops(self):
        """The entry floor is untouched where nothing is at stake."""
        s = self._cliff_slot(accrued=0.0)
        s2 = slot("TSY", program_id="P2")                 # a healthy program alongside
        caps = alloc.Caps(inv_cap_usd=C.slot_cap_usd(100.0))
        a, _, _, dropped = alloc.allocate_with_forfeit_gate([s, s2], 300.0, RSTAR,
                                                            caps=caps)
        self.assertEqual(a[s.key], 0)
        self.assertNotIn("P2", dropped)


class TestHeldAwareAllocation(LipTestCase):
    """── SUPERSEDED SEMANTICS (note 52 D6).  v1 §8.1 bound NET exposure (held + resting ≤
    n_cap), which killed the replenish by construction: a fully-filled lot left zero room and
    presence died on the first fill.  The per-slot cap now bounds THE RESTING LOT; cumulative
    acquisition is the CLUSTER RESERVE's job (cap = lot × (1 + refills)), seeded from held +
    resting so the re-post after the last refill is refused at the cluster, cleanly."""

    def test_held_inventory_does_NOT_shrink_the_replenish_lot(self):
        s = slot("TSY")                                   # n_cap = 10 at 50c under $5.00
        a_flat, _, _ = alloc.allocate([s], 300.0, RSTAR, cluster_cap_usd=10.0)
        a_held, _, _ = alloc.allocate([s], 300.0, RSTAR, held={s.key: 3},
                                      cluster_cap_usd=10.0)
        self.assertEqual(a_flat[s.key], 10)
        self.assertEqual(a_held[s.key], 10)               # the SAME lot re-posts (D6)

    def test_a_consumed_cluster_reserve_ends_the_replenish(self):
        """held = 3 lots + the resting lot = the whole reserve: the NEXT lot is refused at
        the cluster term — that is (1 + refills) enforced by the rail the plan mirrors."""
        s = slot("TSY")
        a, spent, _ = alloc.allocate([s], 300.0, RSTAR, held={s.key: 18},
                                     cluster_cap_usd=10.0)   # $9 held of a $10 reserve
        self.assertLessEqual(a[s.key] * 0.50, 1.0 + 1e-9)    # ≤ the $1 of reserve room

    def test_held_inventory_counts_against_the_venue_cap(self):
        """A filled probe IS the venue's exposure: replenish must fit under what remains."""
        s = slot("TSY", venue="V")
        a, spent, _ = alloc.allocate([s], 300.0, RSTAR, held={s.key: 10},
                                     venue_caps={"V": 7.0})
        # held basis $5 leaves $2 of venue room ⇒ at most 4 more contracts
        self.assertLessEqual(a[s.key], 4)


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


class TestTheCliffSetsMinimumSize(LipTestCase):
    """v5 live 2026-07-28 held eight treasury rungs at $3-9 each and earned 2-9 CENTS per
    market, while v4 put $10-50 on a handful and cleared dollars.  Reward is share, share is
    ~q/S, so earnings are LINEAR in size — and below the $1 forfeit cliff, linear-in-nothing
    is nothing.  A rung is funded ABOVE its cliff or not at all."""

    def _rung(self, tk, S=1000.0, p=0.50, rho=6.25, h=16.0):
        return alloc.Slot(tk, "bid", rho=rho, S=S, p=p, hours_left=h, venue="KXV",
                          window_h=16.0)

    def test_the_cliff_size_matches_the_closed_form(self):
        s = self._rung("KXV-1")
        # Targets ENTRY_FLOOR ($1.50 = CREDIT_TARGET x MARGIN, note 52 D7), margin for
        # fills, rival dilution and model error.  share = 1.5/50 = 3%;
        # q = ceil(1000 x 0.03/0.97) = 31.
        self.assertEqual(alloc.cliff_clearing_q(s), 31)
        self.assertEqual(alloc.cliff_clearing_q(s, 1.0), 21)

    def test_an_unreachable_cliff_returns_None(self):
        s = self._rung("KXV-1", h=0.1)                       # side's whole pool < $1
        self.assertIsNone(alloc.cliff_clearing_q(s))

    def test_funded_rungs_all_clear_the_cliff(self):
        rungs = [self._rung("KXV-%d" % i) for i in range(6)]
        caps = alloc.Caps(inv_cap_usd=30.0)                  # the live derived rung cap
        a, spent, _ = alloc.allocate(rungs, 300.0, 0.0625, caps=caps, cluster_cap_usd=75.0)
        funded = {k: q for k, q in a.items() if q > 0}
        self.assertTrue(funded, "nothing funded at all")
        for key, q in funded.items():
            s = [x for x in rungs if x.key == key][0]
            self.assertGreaterEqual(q, alloc.cliff_clearing_q(s),
                                    "%s funded at %d, below its cliff size" % (key[0], q))

    def test_a_thin_spread_becomes_fewer_bigger_rungs(self):
        """The whole point: a budget too small to clear every rung funds FEWER, not thinner."""
        rungs = [self._rung("KXV-%d" % i) for i in range(10)]
        caps = alloc.Caps(inv_cap_usd=30.0)
        a, spent, _ = alloc.allocate(rungs, 40.0, 0.0625, caps=caps, cluster_cap_usd=75.0)
        funded = [q for q in a.values() if q > 0]
        self.assertLess(len(funded), 10, "must not spread below the cliff across all rungs")
        for q in funded:
            self.assertGreaterEqual(q, 42)


class TestTheRungCapMustExceedTheCliff(LipTestCase):
    """A per-rung cap BELOW the cliff-clearing size makes every mid-priced rung unearnable:
    20 contracts is the most a $10 cap buys at $0.50, and the cliff needs 21.  Live this
    would silently fund nothing — so the drop is LOGGED, never silent."""

    def test_a_cap_below_the_cliff_funds_nothing_and_says_so(self):
        # THE BUDGET MUST BE SCARCE FOR THE CONTAINER TO BE THE LAST WORD (2026-07-30, the
        # pass-2 sweep): with idle dollars the second pass re-offers this exact rung at the
        # cluster reserve, which is the feature.  Here the budget cannot fund the
        # cliff-clearing lot at all, so the container's refusal stands and must be LOGGED.
        rungs = [alloc.Slot("KXV-0", "bid", rho=6.25, S=1000.0, p=0.50, hours_left=16.0,
                            venue="KXV", window_h=16.0)]
        caps = alloc.Caps(inv_cap_usd=10.0)                  # one contract short of the cliff
        a, spent, _ = alloc.allocate(rungs, 10.0, 0.0625, caps=caps, cluster_cap_usd=75.0)
        self.assertEqual(sum(a.values()), 0)
        self.assertTrue(self.logs_of("below_cliff_dropped"),
                        "capital refused for being unearnable must be logged, never silent")

    def test_the_live_cap_clears_it(self):
        rungs = [alloc.Slot("KXV-0", "bid", rho=6.25, S=1000.0, p=0.50, hours_left=16.0,
                            venue="KXV", window_h=16.0)]
        caps = alloc.Caps(inv_cap_usd=30.0)
        a, _, _ = alloc.allocate(rungs, 300.0, 0.0625, caps=caps, cluster_cap_usd=75.0)
        self.assertGreaterEqual(a[("KXV-0", "bid")], 31)


class TestPrunedCapitalIsRedeployed(LipTestCase):
    """Ryan, 2026-07-28: "it's not allocating that capital at all, not just in a different
    place."  Exactly right — the cliff pass zeroed sub-$2 rungs and returned their dollars to
    the budget, but water-filling had already finished, so the freed capital evaporated: $30
    deployed of $300 with nothing refused and nothing over-cap."""

    def _good(self, i):
        # One series per good rung: under note 52 D5 (one rung per cluster) three rungs of
        # one series are ONE fundable slot, and this test is about REDEPLOYMENT, not D5.
        return alloc.Slot("KXA%d-%d" % (i, i), "bid", rho=9.0, S=800.0, p=0.30,
                          hours_left=16.0, venue="KXA%d" % i, window_h=16.0)

    def _hopeless(self, i):
        return alloc.Slot("KXB-%d" % i, "bid", rho=0.4, S=5000.0, p=0.30, hours_left=16.0,
                          venue="KXB", window_h=16.0)

    def test_freed_dollars_land_on_rungs_that_can_clear(self):
        slots = [self._good(i) for i in range(3)] + [self._hopeless(i) for i in range(5)]
        caps = alloc.Caps(inv_cap_usd=30.0)
        a, spent, _ = alloc.allocate(slots, 300.0, 0.0625, caps=caps, cluster_cap_usd=75.0)
        funded = {k: q for k, q in a.items() if q > 0}
        self.assertTrue(all(k[0].startswith("KXA") for k in funded),
                        "only rungs that can reach the floor may be funded")
        self.assertGreater(spent, 60.0,
                           "the pruned dollars must be REDEPLOYED, not evaporated")
        for k, q in funded.items():
            sl = [x for x in slots if x.key == k][0]
            self.assertGreaterEqual(q, alloc.cliff_clearing_q(sl))

    def test_a_book_with_nothing_fundable_spends_nothing(self):
        """The mirror: re-filling must not manufacture spend where no rung can clear."""
        slots = [self._hopeless(i) for i in range(5)]
        caps = alloc.Caps(inv_cap_usd=30.0)
        a, spent, _ = alloc.allocate(slots, 300.0, 0.0625, caps=caps, cluster_cap_usd=75.0)
        self.assertEqual(sum(a.values()), 0)
        self.assertAlmostEqual(spent, 0.0, places=6)


class TestFloorClearingSize(LipTestCase):
    """The sizing rule, derived from the payout floor rather than from a dollar budget.

    STAGED-INERT: `floor_clearing_size` / `slot_target_q` have no call site yet (wiring them as
    a hard cap inside the water-fill stopped the book -- see the docstring on `slot_target_q`).
    Tested now because the arithmetic is what the next pass will wire, and because an untested
    derivation is how `floor($10/p)` survived for two versions."""

    def test_the_price_is_absent_from_the_rule(self):
        """Score is denominated in CONTRACTS and the floor in DOLLARS, so the contracts needed
        to clear it cannot depend on what a contract costs.  This is the whole reason
        floor($10/p) had the relationship backwards."""
        a = alloc.floor_clearing_size(500.0, 100.0)
        self.assertEqual(a, alloc.floor_clearing_size(500.0, 100.0))
        self.assertGreater(a, 0)

    def test_it_scales_with_the_rivals_and_inversely_with_the_pool(self):
        self.assertLess(alloc.floor_clearing_size(100.0, 100.0),
                        alloc.floor_clearing_size(1000.0, 100.0))
        self.assertLess(alloc.floor_clearing_size(500.0, 200.0),
                        alloc.floor_clearing_size(500.0, 100.0))

    def test_an_uncontested_side_needs_exactly_one_contract(self):
        """At Q = 0 our share is 1 for any positive size, so the minimum legal post takes the
        whole side's half-pool.  Sizing up there buys fill risk and no score (v1 D2)."""
        self.assertEqual(alloc.floor_clearing_size(0.0, 100.0), 1)

    def test_a_pool_too_small_to_reach_the_target_returns_ZERO_not_a_size(self):
        """share >= 1 is not a size, it is a refusal.  Posting small into a rung that cannot
        clear the floor is capital at risk for a guaranteed zero -- the mechanism that burned
        167 dollar-hours across 43 forfeited rungs."""
        self.assertEqual(alloc.floor_clearing_size(500.0, 2.0), 0)
        self.assertEqual(alloc.floor_clearing_size(500.0, 0.0), 0)

    def test_the_PER_SIDE_HALVING_is_applied(self):
        """Scores normalise within each side, so a one-sided quote earns at most pool/2 and
        needs TWICE the share a naive model would ask for.  Every credit estimate this program
        produced before 2026-07-29 omitted this divisor and was 2x hot."""
        self.assertEqual(C.SCORE_SIDES, 2.0)
        one_side = alloc.floor_clearing_size(1000.0, 100.0, sides=2.0)
        whole_pool = alloc.floor_clearing_size(1000.0, 100.0, sides=1.0)
        self.assertGreater(one_side, whole_pool)

    def test_the_margin_sizes_above_the_cliff_not_at_it(self):
        at_floor = alloc.floor_clearing_size(1000.0, 100.0, margin=1.0)
        with_margin = alloc.floor_clearing_size(1000.0, 100.0)
        self.assertGreater(with_margin, at_floor)
        self.assertAlmostEqual(C.CREDIT_TARGET_MARGIN, 1.5)
        self.assertAlmostEqual(C.CREDIT_TARGET_USD, 1.00)

    def test_realistic_boards_cost_a_couple_of_dollars_not_ten(self):
        """Sanity against the receipt: a paying rung cost a median $9.70 at ~9% presence.  At
        full presence the same $1.00 should cost on the order of $1-2 of collateral."""
        for Q, pool, px in ((500.0, 100.0, 0.10), (1826.0, 100.0, 0.03), (123.0, 100.0, 0.20)):
            q = alloc.floor_clearing_size(Q, pool)
            self.assertLess(q * px, 3.00, "Q=%s pool=%s px=%s cost %.2f" % (Q, pool, px, q * px))

    def test_slot_target_q_is_the_MINIMUM_of_the_dollar_bound_and_the_target(self):
        s = alloc.Slot("KXX-26JUL29-T1", "bid", rho=6.25, S=500.0, p=0.02, window_h=16.0)
        caps = alloc.Caps(inv_cap_usd=10.0)
        self.assertEqual(alloc.slot_target_q(s, caps),
                         min(alloc.n_cap(0.02, caps),
                             alloc.floor_clearing_size(500.0, 6.25 * 16.0)))

    def test_slot_target_q_has_NO_call_site_yet(self):
        """The pair is staged-inert on purpose.  If you wire it, delete this test in the same
        commit and prove the book still places orders -- wiring it as a hard cap took
        test_orders_appear_within_three_cycles to zero."""
        import inspect
        src = inspect.getsource(alloc)
        body = src.split("def slot_target_q", 1)[1]
        self.assertNotIn("slot_target_q(", body.split("return int(min", 1)[1])


class TestPass2IdleCapitalSweep(LipTestCase):
    """IDLE CAPITAL IS WASTED (Ryan, 2026-07-30).  The lot container (reserve/2) is sized to
    leave room for refills, which is right when capital is scarce and wrong when it is idle:
    a rung whose floor-clearing lot costs $8 was refused outright by a $5 container and
    earned NOTHING, where the same $8 as one reserve-consuming lot with zero refills earns
    what that rung pays.  A worse rung beats an empty one."""

    R_STAR = 0.0625
    LOT, RESERVE, CEILING = 5.0, 10.0, 300.0

    def caps(self):
        return alloc.Caps(inv_cap_usd=self.LOT)

    def fat(self, tk="KXFAT-1", venue="KXFAT", **kw):
        """q_min = 32 at p=0.25 ⇒ an $8.00 lot: over the $5 container, under the $10 reserve."""
        kw.setdefault("rho", 1.0); kw.setdefault("S", 224.0); kw.setdefault("p", 0.25)
        kw.setdefault("hours_left", 24.0); kw.setdefault("window_h", 24.0)
        return alloc.Slot(tk, "bid", venue=venue, **kw)

    def lean(self, tk="KXLEAN-1", venue="KXLEAN", **kw):
        """q_min = 20 at p=0.25 ⇒ a $5.00 lot: fits the container, so pass 1 funds it."""
        kw.setdefault("rho", 1.0); kw.setdefault("S", 140.0); kw.setdefault("p", 0.25)
        kw.setdefault("hours_left", 24.0); kw.setdefault("window_h", 24.0)
        return alloc.Slot(tk, "bid", venue=venue, **kw)

    def _run(self, slots, budget, **kw):
        kw.setdefault("caps", self.caps())
        kw.setdefault("cluster_cap_usd", self.RESERVE)
        kw.setdefault("ceiling_usd", self.CEILING)
        return alloc.allocate(slots, budget, self.R_STAR, **kw)

    def test_the_cliff_clearing_lot_is_the_size_this_test_claims(self):
        self.assertEqual(alloc.cliff_clearing_q(self.fat()), 32)      # 32 × $0.25 = $8.00
        self.assertEqual(alloc.cliff_clearing_q(self.lean()), 20)     # 20 × $0.25 = $5.00

    def test_idle_budget_funds_the_over_container_rung_as_ONE_lot(self):
        a, spent, _ = self._run([self.fat()], 300.0)
        self.assertEqual(a[("KXFAT-1", "bid")], 32)
        self.assertAlmostEqual(spent, 8.00, places=9)
        rows = self.logs_of("pass2_funded")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["rungs"], 1)
        self.assertAlmostEqual(rows[0]["usd"], 8.00, places=9)
        # the rung the container refused is the SAME rung pass 2 took
        self.assertTrue(self.logs_of("below_cliff_dropped"))

    def test_when_pass_1_exhausts_the_budget_pass_2_changes_NOTHING(self):
        """The control: pass 2 is a sweep of what is LEFT, never a second helping.  Four lean
        rungs, each fitting the container, against a budget only three of them fit in — pass 1
        spends it, so the fourth (and the fat rung beside it) get nothing."""
        lean = [self.lean("KXL%d-1" % i, venue="KXL%d" % i) for i in range(10)]
        budget = 10 * 5.00
        a, spent, _ = self._run(lean + [self.fat()], budget)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 10)
        self.assertAlmostEqual(spent, budget, places=9)
        self.assertEqual(a[("KXFAT-1", "bid")], 0)
        self.assertEqual(self.logs_of("pass2_funded"), [])

    def test_the_cluster_RESERVE_still_bounds_the_relaxed_lot(self):
        """The bound moves from the lot to the reserve — and stops there."""
        a, spent, _ = self._run([self.fat()], 300.0, cluster_cap_usd=6.0)
        self.assertEqual(a[("KXFAT-1", "bid")], 0)
        self.assertAlmostEqual(spent, 0.0, places=9)
        self.assertEqual(self.logs_of("pass2_funded"), [])

    def test_a_dark_pass_2_says_WHICH_gate_ate_every_candidate(self):
        """No silent caps (Ryan, 2026-07-30: "we should be logging why we are refusing
        rungs").  Live, pass 2 funded zero against ~$170 idle and the tape could not say
        why — only successes were logged.  A refused candidate now lands in a reason tally."""
        self._run([self.fat()], 300.0, cluster_cap_usd=6.0)
        rows = self.logs_of("pass2_refused")
        self.assertTrue(rows)
        self.assertEqual(rows[-1]["funded"], 0)
        self.assertEqual(rows[-1]["candidates"], 1)
        self.assertEqual(rows[-1]["reasons"], {"lot_cap": 1})

    def test_the_cluster_DOLLARS_bound_pass_2_not_a_rung_COUNT(self):
        """REWRITTEN 2026-07-30 — was `test_ONE_RUNG_PER_CLUSTER_still_holds_in_pass_2`.
        D5′: a settle source is bounded by its DOLLARS.  Two $8 lots do not fit a $10
        reserve, so exactly one lands — for the dollar reason, provably, since raising the
        reserve to $20 lands both.  MEASURED motive: `pass2_refused` reported cluster_owned
        as the blocking term for ALL 76 candidates while ~$234 sat idle."""
        a, spent, _ = self._run([self.fat("KXFAT-1"), self.fat("KXFAT-2")], 300.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 1)
        self.assertAlmostEqual(spent, 8.00, places=9)

    def test_raising_the_cluster_DOLLARS_lands_both_rungs_of_one_cluster(self):
        a, spent, _ = self._run([self.fat("KXFAT-1"), self.fat("KXFAT-2")], 300.0,
                                cluster_cap_usd=20.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 2)
        self.assertAlmostEqual(spent, 16.00, places=9)
        rows = self.logs_of("pass2_funded")
        self.assertEqual(rows[0]["rungs"], 2)

    def test_the_plan_side_VARIANCE_test_still_refuses_in_pass_2(self):
        """D11 is unchanged by pass 2 because it was ALREADY charging the cluster reserve —
        which is exactly the container pass 2 hands out."""
        a, _, _ = self._run([self.fat()], 300.0, ceiling_usd=20.0)
        self.assertEqual(a[("KXFAT-1", "bid")], 0)

    def test_a_rung_that_cannot_REACH_the_floor_is_never_funded_at_any_idleness(self):
        """Guaranteed forfeit beats nothing is FALSE.  The whole side's remaining pool cannot
        pay the floor here, so no amount of idle capital rescues it."""
        hopeless = self.fat("KXDEAD-1", venue="KXDEAD", rho=0.01)
        self.assertIsNone(alloc.cliff_clearing_q(hopeless))
        a, spent, _ = self._run([hopeless], 300.0)
        self.assertEqual(a[("KXDEAD-1", "bid")], 0)
        self.assertAlmostEqual(spent, 0.0, places=9)


class TestDisplacementAtCapacity(LipTestCase):
    """CALCULABLE DISPLACEMENT (Ryan: "which makes more total is calculable").  At capacity the
    question is not "may we?" but "which rung makes more?", and both sides are the same
    product in the same units — expected credit over the horizon we are judged on, with the
    incumbent charged for the banked pot its cancellation forfeits."""

    R_STAR = 0.0625

    def lean(self, i):
        return alloc.Slot("KXL%d-1" % i, "bid", rho=1.0, S=140.0, p=0.25, hours_left=24.0,
                          venue="KXL%d" % i, window_h=24.0)

    def old(self, hours_left=1.0, accrued=0.02):
        """A DECAYED incumbent: 20 contracts resting, an hour of window left, pennies banked.
        E_keep = 0.02 + (20/160)×0.5×1 = $0.0825."""
        return alloc.Slot("KXOLD-1", "bid", rho=1.0, S=140.0, p=0.25, hours_left=hours_left,
                          venue="KXOLD", window_h=24.0, accrued=accrued)

    def fat(self):
        """The fresh candidate: big pool, whole window.  q_min = 32 ⇒ E_new = $1.50."""
        return alloc.Slot("KXFAT-1", "bid", rho=1.0, S=224.0, p=0.25, hours_left=24.0,
                          venue="KXFAT", window_h=24.0)

    def _run(self, extra, budget=100.0, **kw):
        """19 lean rungs at $5 each = $95 of a $100 budget: the fat rung's $8 lot cannot be
        funded without recalling something."""
        slots = [self.lean(i) for i in range(19)] + list(extra)
        kw.setdefault("caps", alloc.Caps(inv_cap_usd=5.0))
        kw.setdefault("cluster_cap_usd", 10.0)
        kw.setdefault("ceiling_usd", 300.0)
        return alloc.allocate(slots, budget, self.R_STAR, **kw)

    KOLD, KFAT = ("KXOLD-1", "bid"), ("KXFAT-1", "bid")

    def test_the_fat_candidate_displaces_the_penny_earner_and_shows_its_work(self):
        a, spent, _ = self._run([self.old(), self.fat()],
                                resting={self.KOLD: 20.0})
        self.assertEqual(a[self.KFAT], 32)
        self.assertEqual(a[self.KOLD], 0)             # zeroed ⇒ the requoter recalls it
        self.assertLessEqual(spent, 100.0 + 1e-9)
        rows = self.logs_of("rung_displaced")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["took"], r["dropped"]), ("KXFAT-1", "KXOLD-1"))
        self.assertAlmostEqual(r["e_new_usd"], 1.50, places=6)
        self.assertAlmostEqual(r["e_keep_usd"], 0.0825, places=6)
        self.assertAlmostEqual(r["accrued_at_risk_usd"], 0.02, places=6)
        self.assertGreater(r["e_new_usd"], r["e_keep_usd"])

    def test_a_BANKED_pot_defends_the_same_incumbent_against_the_same_candidate(self):
        """The hysteresis is the accrued term, not a constant: $2.00 banked makes E_keep
        larger than anything a fresh rung can promise, and nothing moves."""
        a, _, _ = self._run([self.old(accrued=2.00), self.fat()],
                            resting={self.KOLD: 20.0})
        self.assertEqual(a[self.KOLD], 20)
        self.assertEqual(a[self.KFAT], 0)
        self.assertEqual(self.logs_of("rung_displaced"), [])

    def test_a_POSITION_is_never_a_displacement_target(self):
        """Held inventory RIDES (2026-07-30).  Only a resting order can be recalled, so
        planning against inventory would "free" dollars no cancel can free."""
        a, _, _ = self._run([self.old(), self.fat()],
                            resting={self.KOLD: 20.0}, held={self.KOLD: 20.0})
        self.assertEqual(a[self.KOLD], 20)
        self.assertEqual(a[self.KFAT], 0)
        self.assertEqual(self.logs_of("rung_displaced"), [])

    def test_below_capacity_nothing_is_ever_displaced(self):
        """Pass 2 spends idle dollars; displacement only speaks when there are none."""
        a, _, _ = self._run([self.old(), self.fat()], budget=300.0,
                            resting={self.KOLD: 20.0})
        self.assertEqual(a[self.KFAT], 32)
        self.assertEqual(a[self.KOLD], 20)            # untouched
        self.assertEqual(self.logs_of("rung_displaced"), [])

    def test_NO_CHURN_the_swap_does_not_swap_back(self):
        """Feed the result back as the book: the displaced rung, now with a full window
        again, cannot displace the rung that took its place — both are sized to the same
        floor, so E_new never STRICTLY exceeds E_keep and ties keep the incumbent."""
        a, spent, _ = self._run([self.old(hours_left=24.0), self.fat()],
                                resting={self.KFAT: 32.0})
        self.assertEqual(a[self.KFAT], 32)            # the incumbent keeps its seat
        self.assertEqual(self.logs_of("rung_displaced"), [])
        self.assertLessEqual(spent, 100.0 + 1e-9)
        # (the recalled rung may be re-funded from dollars that are genuinely IDLE — that is
        # pass 2 doing its job, and it costs the incumbent nothing)
