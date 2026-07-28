"""The UNDERLYING-CLUSTER CAP — from today's live treasury loss.

The fixture that matters is `test_the_live_shape_this_morning_REFUSES`: ~$150 of one direction
across correlated rungs, which every per-market check individually approved.
"""

import unittest

from .. import clusters as CL, config as C
from .base import LipTestCase


def pos(ticker, side, n, basis):
    return {"ticker": ticker, "side": side, "n": n, "basis": basis}


class TestClusterIdentity(LipTestCase):
    def test_all_five_UST_series_are_ONE_rates_cluster(self):
        for s in ("KXUST2AD", "KXUST5AD", "KXUST7AD", "KXUST10AD", "KXUST30AD"):
            self.assertEqual(CL.cluster_of("%s-26JUL28-T4.65" % s), "RATES")

    def test_a_gas_ladder_is_its_own_cluster(self):
        self.assertEqual(CL.cluster_of("KXAAAGASD-26JUL29-B4.120"), "KXAAAGASD")

    def test_unrelated_series_are_separate_clusters(self):
        self.assertNotEqual(CL.cluster_of("KXAAAGASD-X"), CL.cluster_of("KXINXU-X"))

    def test_threshold_strikes_parse_and_ranges_refuse(self):
        self.assertAlmostEqual(CL.parse_threshold("KXUST10AD-26JUL28-T4.65"), 4.65)
        self.assertIsNone(CL.parse_threshold("KXAAAGASD-26JUL29-B4.120"))
        self.assertIsNone(CL.parse_threshold("KXPYPL-PERP"))

    def test_leg_sign_is_directional_on_the_UNDERLYING(self):
        self.assertEqual(CL.leg_sign("yes"), 1.0)
        self.assertEqual(CL.leg_sign("bid"), 1.0)
        self.assertEqual(CL.leg_sign("no"), -1.0)
        self.assertEqual(CL.leg_sign("ask"), -1.0)


class TestMeasures(LipTestCase):
    def test_same_direction_rungs_ACCUMULATE_they_do_not_diversify(self):
        """The finding, in one assertion: 15 rungs of one ladder are ONE bet."""
        book = [pos("KXUST10AD-26JUL28-T%.2f" % (4.00 + 0.05 * i), "yes", 20, 0.50)
                for i in range(15)]
        self.assertAlmostEqual(CL.signed_delta_usd(book), 150.0, places=6)
        self.assertAlmostEqual(CL.gross_basis_usd(book), 150.0, places=6)

    def test_a_perfect_offset_nets_to_zero_signed(self):
        book = [pos("KXUST10AD-26JUL28-T4.10", "yes", 20, 0.50),
                pos("KXUST10AD-26JUL28-T4.10", "no", 20, 0.50)]
        self.assertAlmostEqual(CL.signed_delta_usd(book), 0.0, places=9)

    def test_worst_case_is_EXACT_on_a_threshold_ladder(self):
        """YES@4.10 and NO@4.15: whatever the underlying does, at least one pays — a genuine
        spread, and the worst case is total basis minus that certain $1 per contract."""
        book = [pos("KXUST10AD-26JUL28-T4.10", "yes", 20, 0.50),
                pos("KXUST10AD-26JUL28-T4.15", "no", 20, 0.50)]
        self.assertAlmostEqual(CL.gross_basis_usd(book), 20.0, places=6)
        self.assertAlmostEqual(CL.worst_case_loss_usd(book), 0.0, places=6)

    def test_THE_MIRROR_a_book_that_nets_to_zero_but_can_lose_BOTH_legs(self):
        """YES@4.15 + NO@4.10: nets to ZERO directionally, but if the underlying lands in
        [4.10, 4.15) BOTH legs expire worthless.  A signed-only cap waves this through; the
        worst-case measure is what refuses it."""
        book = [pos("KXUST10AD-26JUL28-T4.15", "yes", 20, 0.50),
                pos("KXUST10AD-26JUL28-T4.10", "no", 20, 0.50)]
        self.assertAlmostEqual(CL.signed_delta_usd(book), 0.0, places=9)
        self.assertAlmostEqual(CL.worst_case_loss_usd(book), 20.0, places=6)
        self.assertAlmostEqual(CL.worst_case_loss_usd(book), CL.gross_basis_usd(book),
                               places=6)

    def test_one_directional_ladder_worst_case_loses_all_but_the_lowest_rung(self):
        """15 YES rungs: if the underlying settles below every strike, all 15 lose."""
        book = [pos("KXUST10AD-26JUL28-T%.2f" % (4.00 + 0.05 * i), "yes", 20, 0.50)
                for i in range(15)]
        self.assertAlmostEqual(CL.worst_case_loss_usd(book), 150.0, places=6)

    def test_unparseable_strikes_are_UNNETTABLE_and_count_in_full(self):
        """The conservative default: refuse earlier rather than net a structure we have not
        verified (range markets do not net like thresholds)."""
        book = [pos("KXAAAGASD-26JUL29-B4.120", "yes", 100, 0.02),
                pos("KXAAAGASD-26JUL29-B4.125", "yes", 100, 0.02)]
        self.assertAlmostEqual(CL.worst_case_loss_usd(book), 4.0, places=6)

    def test_an_empty_cluster_is_zero(self):
        self.assertEqual(CL.worst_case_loss_usd([]), 0.0)
        self.assertEqual(CL.signed_delta_usd([]), 0.0)


class TestCap(LipTestCase):
    def test_the_cap_shares_the_day_stop_derivation_with_the_series_cap(self):
        day_stop = 105.0
        self.assertAlmostEqual(CL.cluster_cap_usd(day_stop), 52.5, places=9)
        self.assertAlmostEqual(CL.cluster_cap_usd(day_stop), C.cap_series_usd(day_stop),
                               places=9)
        self.assertLess(CL.cluster_cap_usd(day_stop), day_stop)

    def test_the_cap_never_falls_below_the_per_slot_inventory_cap(self):
        self.assertGreaterEqual(CL.cluster_cap_usd(1.0), C.INV_CAP_USD)

    def test_caps_DO_NOT_COMPOSE_which_is_why_this_cap_exists(self):
        """Five UST series inherit five per-series caps: 2.5x day_stop of permission for ONE
        underlying.  That is exactly how ~$150 passed fifteen approved checks."""
        day_stop = 40.0
        series_total = 5 * C.cap_series_usd(day_stop)
        self.assertGreater(series_total, day_stop)
        self.assertLess(CL.cluster_cap_usd(day_stop), series_total)


class TestAdmission(LipTestCase):
    CAP = 52.5                                    # cluster_cap_usd(day_stop = 105)

    def test_15_same_direction_rungs_across_3_series_REFUSE_at_the_bound(self):
        """The required test: rungs spread across SEPARATE SERIES still hit one cluster bound,
        because they settle off one underlying."""
        series = ("KXUST2AD", "KXUST10AD", "KXUST30AD")
        book, refused_at = [], None
        for i in range(15):
            s = series[i % 3]
            p = pos("%s-26JUL28-T%.2f" % (s, 4.00 + 0.05 * i), "yes", 20, 0.50)
            ok, reason, detail = CL.cluster_admits(book, p, self.CAP)
            if not ok:
                refused_at = (i, reason, detail)
                break
            book.append(p)
        self.assertIsNotNone(refused_at, "the cluster cap never bound — the live defect")
        i, reason, detail = refused_at
        self.assertEqual(detail["cluster"], "RATES")
        self.assertLess(i, 15)
        self.assertLessEqual(CL.worst_case_loss_usd(CL.annotate(book)), self.CAP + 1e-9)

    def test_offsetting_positions_NET_and_are_admitted(self):
        """A genuine spread must still be allowed — the cap bounds risk, not activity."""
        book = [pos("KXUST10AD-26JUL28-T4.10", "yes", 100, 0.50)]
        p = pos("KXUST10AD-26JUL28-T4.15", "no", 100, 0.50)
        ok, reason, detail = CL.cluster_admits(book, p, self.CAP)
        self.assertTrue(ok, detail)
        self.assertAlmostEqual(detail["worst_case_usd"], 0.0, places=6)

    def test_a_zero_net_but_both_legs_losing_book_is_REFUSED(self):
        """The mirror, enforced: netting to zero is not the same as being hedged."""
        book = [pos("KXUST10AD-26JUL28-T4.15", "yes", 100, 0.50)]
        p = pos("KXUST10AD-26JUL28-T4.10", "no", 100, 0.50)
        ok, reason, detail = CL.cluster_admits(book, p, self.CAP)
        self.assertFalse(ok)
        self.assertEqual(reason, CL.REFUSE_WORST)
        self.assertAlmostEqual(detail["signed_usd"], 0.0, places=9)

    def test_a_different_cluster_is_unaffected_by_a_full_one(self):
        """One underlying filling up must not stop the book — that is the whole charter §5
        argument, applied to clusters."""
        rates = [pos("KXUST10AD-26JUL28-T%.2f" % (4.0 + 0.05 * i), "yes", 20, 0.50)
                 for i in range(5)]
        gas = pos("KXAAAGASD-26JUL29-B4.120", "yes", 50, 0.02)
        ok, reason, detail = CL.cluster_admits(rates, gas, self.CAP)
        self.assertTrue(ok, detail)
        self.assertEqual(detail["cluster"], "KXAAAGASD")

    def test_the_live_shape_this_morning_REFUSES(self):
        """THE FIXTURE THAT MATTERS: ~$150 of one direction across correlated treasury rungs,
        every one of which the per-market cap approved individually.  Under the cluster cap it
        is refused, and refused well before $150."""
        morning = []
        for i in range(15):
            s = ("KXUST2AD", "KXUST5AD", "KXUST10AD")[i % 3]
            morning.append(pos("%s-26JUL28-T%.2f" % (s, 4.00 + 0.05 * i), "yes", 20, 0.50))
        self.assertAlmostEqual(CL.gross_basis_usd(morning), 150.0, places=6)

        # every rung passes the per-market cap on its own: $10 of basis each
        for p in morning:
            self.assertLessEqual(p["n"] * p["basis"], C.INV_CAP_USD + 1e-9)

        # and the cluster refuses long before the book reaches $150
        accepted = []
        for p in morning:
            ok, _, _ = CL.cluster_admits(accepted, p, self.CAP)
            if not ok:
                break
            accepted.append(p)
        self.assertLess(CL.gross_basis_usd(accepted), 150.0)
        self.assertLessEqual(CL.worst_case_loss_usd(CL.annotate(accepted)), self.CAP + 1e-9)
        self.assertLess(len(accepted), 15)

    def test_the_report_surfaces_utilization_before_it_becomes_a_loss(self):
        book = [pos("KXUST10AD-26JUL28-T%.2f" % (4.0 + 0.05 * i), "yes", 20, 0.50)
                for i in range(4)]
        rep = CL.cluster_report(book, self.CAP)
        self.assertEqual(len(rep), 1)
        self.assertEqual(rep[0]["cluster"], "RATES")
        self.assertGreater(rep[0]["utilization"], 0.0)


if __name__ == "__main__":
    unittest.main()
