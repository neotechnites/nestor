"""What remains of alloc.py after THE OWNER'S LAW (Ryan, 2026-07-30): the CFTC scoring,
the Slot table, and the rail-side helpers.

THE OLD SUITE THAT LIVED HERE ENCODED THE OLD ALLOCATOR — water-filling under (★), the r*
fixpoint, the forfeit gate, the rescue, floor_clearing_size, the pass-2 idle sweep and
displacement at capacity.  All of that machinery is DELETED by the owner's decision of
2026-07-30 and its tests died with it, deliberately: a green test for a deleted mechanism
is a vote to resurrect it.  The allocator's law and every clause's mutation test live in
`test_law.py`; the engine-level wiring in `test_engine.py`/`test_runner.py`.
"""

import unittest

from .. import alloc, config as C
from .base import LipTestCase


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


class TestShare(LipTestCase):
    def test_our_share_is_pro_rata_at_the_touch(self):
        self.assertAlmostEqual(alloc.our_share(100, 300), 0.25)
        self.assertEqual(alloc.our_share(0, 300), 0.0)

    def test_reward_rate_is_share_times_half_pool(self):
        self.assertAlmostEqual(alloc.reward_rate(2.0, 100, 100), 0.5)


class TestRailHelpers(LipTestCase):
    def test_n_cap_is_the_dollar_bound_over_price(self):
        """The per-order contract bound `place()`'s B9 lane reads: floor(cap / p)."""
        self.assertEqual(alloc.n_cap(0.50, alloc.Caps(inv_cap_usd=10.0)), 20)
        self.assertEqual(alloc.n_cap(0.02, alloc.Caps(inv_cap_usd=10.0)), 500)
        self.assertEqual(alloc.n_cap(0.0), 0)

    def test_make_before_break_reserve(self):
        """v1 §2.4 B3 — MBB transiently holds TWO copies of one order's collateral."""
        self.assertAlmostEqual(alloc.reserve_budget(300.0, 25.0), 275.0)
        self.assertEqual(alloc.reserve_budget(10.0, 25.0), 0.0)

    def test_t0_qualification_size_is_the_walk_gap(self):
        self.assertEqual(alloc.t0_qualification_size(400.0, 1000), 600)
        self.assertEqual(alloc.t0_qualification_size(1200.0, 1000), 0)


class TestTheOldAllocatorIsGone(LipTestCase):
    """Mutation guard for the deletion itself (owner decision, 2026-07-30): if any of the
    old machinery returns, this fails before a reviewer has to notice a behavior."""

    def test_the_water_filling_family_is_deleted(self):
        for gone in ("allocate", "allocate_with_rstar", "allocate_with_forfeit_gate",
                     "qualification_pass", "rescue", "RescueResult", "floor_clearing_size",
                     "slot_target_q", "market_cap_usd", "cliff_clearing_q",
                     "projected_period_payout"):
            self.assertFalse(hasattr(alloc, gone), gone)

    def test_the_rstar_solver_is_deleted(self):
        from .. import money as M
        for gone in ("solve_rstar", "rstar_seed", "RStarResult"):
            self.assertFalse(hasattr(M, gone), gone)

    def test_the_gate_flags_are_deleted(self):
        for gone in ("FREE_RIDE_ONLY", "P6_ADVISORY", "LAND_GRAB_PRICE_C",
                     "LAND_GRAB_MAX_MARKETS", "LAND_GRAB_MAX_COLLATERAL_FRAC",
                     "P7_MAX_REVIVAL_MARKETS", "RSTAR_MAX_ITERS"):
            self.assertFalse(hasattr(C, gone), gone)

    def test_slots_carry_no_gate_plumbing(self):
        s = slot()
        for gone in ("p6_ok", "assume_filled", "net_at"):
            self.assertFalse(hasattr(s, gone), gone)


if __name__ == "__main__":
    unittest.main()
