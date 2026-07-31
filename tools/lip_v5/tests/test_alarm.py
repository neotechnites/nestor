"""THE BUG ALARM (v6 stage 4) — note 55's risk frame, in tests.

    "Variance losses never halt the earner ... Model-impossible losses: the machine is broken."

The two properties that matter are opposites and both are pinned: a large MODEL-CONSISTENT
loss must NOT halt, and a small MODEL-IMPOSSIBLE one MUST.
"""

import math

from .. import alarm as AL, bleed as B, config as C
from .base import LipTestCase


class TestTheThresholdIsDerived(LipTestCase):
    def test_z_comes_from_a_family_wise_budget_over_the_programs_own_checks(self):
        z, m, alpha = AL.derive_z()
        self.assertAlmostEqual(m, C.LIP_PROGRAM_DAYS_REMAINING * 86400.0
                               / C.RECON_POSITIONS_S, places=6)
        self.assertAlmostEqual(alpha, C.ALARM_FAMILY_ALPHA / m, places=12)
        # and z IS that alpha's one-sided normal quantile, to machine precision
        self.assertAlmostEqual(0.5 * math.erfc(z / math.sqrt(2.0)), alpha, places=10)

    def test_the_deploy_geometry_gives_z_about_4_59(self):
        z, m, _a = AL.derive_z()
        self.assertAlmostEqual(m, 23040.0, places=6)
        self.assertAlmostEqual(z, 4.5944, places=3)

    def test_more_checks_buy_a_stricter_z(self):
        z_short, _m, _a = AL.derive_z(days_remaining=1.0)
        z_long, _m2, _a2 = AL.derive_z(days_remaining=365.0)
        self.assertGreater(z_long, z_short,
                           "a longer program runs the test more often and must demand more")

    def test_the_alarm_says_where_its_threshold_came_from(self):
        AL.BugAlarm()
        rec = self.logs_of("bug_alarm_armed")
        self.assertTrue(rec)
        for f in ("z", "checks", "alpha", "family_alpha", "days_remaining", "cadence_s"):
            self.assertIn(f, rec[0])


class TestTheTablesPrediction(LipTestCase):
    def test_expected_loss_is_the_g_table(self):
        mean, _var = AL.fill_moments(10.0, 0.20)
        self.assertAlmostEqual(mean, 10.0 * B.g_for_price(0.20), places=9)
        self.assertAlmostEqual(mean, 3.508, places=6)

    def test_variance_is_the_bernoulli_of_the_DEGRADED_win_rate(self):
        _m, var = AL.fill_moments(10.0, 0.20)
        w = 0.20 * (1.0 - B.g_for_price(0.20))
        self.assertAlmostEqual(var, (10.0 / 0.20) ** 2 * w * (1.0 - w), places=6)

    def test_a_51c_plus_fill_is_predicted_to_lose_nothing(self):
        mean, _v = AL.fill_moments(10.0, 0.60)
        self.assertAlmostEqual(mean, 0.0, places=9)


class TestVarianceLossesNEVERHalt(LipTestCase):
    """The whole point of deleting the stopper."""

    def test_losing_every_dollar_of_a_big_book_is_model_CONSISTENT(self):
        a = AL.BugAlarm()
        for _ in range(60):
            a.observe_fill(10.0, 0.20)               # $600 of 20c inventory
        halt, why, nums = a.check(loss_usd=600.0, committed_usd=600.0)
        self.assertFalse(halt, nums)
        self.assertEqual(why, "")

    def test_the_realised_loss_may_be_far_above_expectation_and_still_not_halt(self):
        a = AL.BugAlarm()
        for _ in range(60):
            a.observe_fill(10.0, 0.20)
        a.observe_settlement(3.0 * a.exp_loss)       # 3x the table's prediction
        halt, _w, nums = a.check(loss_usd=600.0, committed_usd=600.0)
        self.assertFalse(halt, nums)
        self.assertLess(nums["sigmas"], nums["z"])


class TestAlarm1ImpossibleLoss(LipTestCase):
    def test_a_loss_larger_than_everything_ever_at_risk_halts(self):
        a = AL.BugAlarm()
        a.observe_fill(10.0, 0.20)
        halt, why, nums = a.check(loss_usd=500.0, committed_usd=20.0)
        self.assertTrue(halt)
        self.assertEqual(why, "loss_exceeds_always_filled_worst_case")
        self.assertAlmostEqual(nums["worst_case_bound_usd"], 30.0, places=6)

    def test_an_adopted_position_is_inside_the_bound(self):
        """A position we never filled (inherited, reconciled) has a COST but no fill of ours;
        the bound reads that cost, or the first mark of a book we did not open pages."""
        a = AL.BugAlarm()
        halt, _w, _n = a.check(loss_usd=90.0, committed_usd=90.0)
        self.assertFalse(halt)

    def test_it_fires_on_the_exact_boundary_plus_a_cent(self):
        a = AL.BugAlarm()
        a.observe_fill(100.0, 0.50)
        self.assertFalse(a.check(loss_usd=100.0)[0])
        self.assertTrue(a.check(loss_usd=100.01)[0])


class TestAlarm2OutsideCalibration(LipTestCase):
    def test_a_loss_the_table_calls_impossible_halts(self):
        a = AL.BugAlarm()
        a.observe_fill(10.0, 0.50)                   # g(50c) = 0.1071, tiny sigma
        a.observe_settlement(1_000.0)
        halt, why, nums = a.check(loss_usd=0.0, committed_usd=100_000.0)
        self.assertTrue(halt)
        self.assertEqual(why, "loss_per_fill_outside_calibration")
        self.assertGreater(nums["sigmas"], nums["z"])

    def test_a_book_with_no_settlements_yet_cannot_fire_alarm_2(self):
        a = AL.BugAlarm()
        a.observe_fill(10.0, 0.50)
        self.assertFalse(a.outside_calibration()[0])

    def test_gains_count_toward_the_running_sum(self):
        a = AL.BugAlarm()
        for _ in range(50):
            a.observe_fill(10.0, 0.20)
        a.observe_settlement(500.0)
        a.observe_settlement(-500.0)                 # a winner cancels it
        self.assertLess(a.excess(), 0.0)
        self.assertFalse(a.check(loss_usd=0.0)[0])


class TestTheAlarmIsWorldStateOnly(LipTestCase):
    """The convergence doctrine: memory of the WORLD is legal, memory of our own decisions is
    the disease.  Fills and settlements are the wire's facts."""

    def test_nothing_here_reads_an_order_or_an_allocation(self):
        """Structural, not textual: the module's IMPORTS are the whole surface it can reach,
        and none of them is a book of ours.  (A prose grep would trip on this file's own
        header, which is why the check is on the AST.)"""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(AL))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported.add(a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.name)
        self.assertEqual(imported, {"math", "bleed", "config", "runtime"},
                         "the alarm reached for something that is not a world fact: %s"
                         % imported)
