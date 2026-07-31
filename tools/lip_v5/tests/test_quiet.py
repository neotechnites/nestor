"""THE QUIET LADDER-WIDE CLASS AND THE WALLS (v6 stage 3) — note 55's centrepiece.

The wall economics are worked NUMERICALLY here at C = 600, because the deploy decision rests
on them: a 1,000-contract qualification wall at 1c costs $10.00 of collateral, fits inside the
$21.43 rail, bleeds ~$0 because the family is measurably quiet, and pays the half-pool.
"""

from .. import alloc, config as C, dials as D, marginal as MQ, quiet as QT
from .base import LipTestCase


def slot(ticker, side="bid", rho=1.0, S=0.0, p=0.01, phi=0.0, phi_prior=0.3, phi_k=10.0,
         exposure=0.0, hours_left=24.0, accrued=0.0, target_size=1000, cum_size=0.0,
         land_grab_price_c=None, **kw):
    # the wall's price on the side's OWN collateral axis — exactly what scan.build_slots does
    if land_grab_price_c is None:
        land_grab_price_c = (C.V6_PRICE_FLOOR_C if side == "bid"
                             else 100 - C.V6_PRICE_FLOOR_C)
    return alloc.Slot(ticker, side, rho=rho, S=S, p=p, phi=phi, phi_prior=phi_prior,
                      phi_k=phi_k, phi_exposure_h=exposure, hours_left=hours_left,
                      accrued=accrued, target_size=target_size, cum_size=cum_size,
                      land_grab_price_c=land_grab_price_c, **kw)


def quiet_slot(ticker, exposure=100.0, **kw):
    """A rung with `exposure` contract-hours of OUR tape and ZERO fills.  phi is the posterior
    those inputs produce: (0 + k x prior)/(exposure + k)."""
    prior, k = kw.pop("phi_prior", 0.3), kw.pop("phi_k", 10.0)
    phi = (0.0 + k * prior) / (exposure + k)
    return slot(ticker, phi=phi, phi_prior=prior, phi_k=k, exposure=exposure, **kw)


class TestTheEvidenceIsRECOVERED(LipTestCase):
    """No new plumbing: the fill count the posterior was built from inverts out of the four
    numbers the Slot already carries."""

    def test_zero_fills_invert_to_zero(self):
        s = quiet_slot("KXQ-26JUL31-T1", exposure=100.0)
        self.assertAlmostEqual(QT.fills_from_posterior(s), 0.0, places=9)

    def test_a_real_fill_count_inverts_exactly(self):
        prior, k, e, fills = 0.3, 10.0, 40.0, 7.0
        phi = (fills + k * prior) / (e + k)
        s = slot("KXQ-26JUL31-T1", phi=phi, phi_prior=prior, phi_k=k, exposure=e)
        self.assertAlmostEqual(QT.fills_from_posterior(s), fills, places=6)

    def test_an_ASSERTED_phi_is_not_evidence(self):
        s = alloc.Slot("KXQ-26JUL31-T1", "bid", rho=1.0, S=0.0, p=0.01, phi=0.0)
        self.assertIsNone(QT.fills_from_posterior(s))
        ok, nums = QT.is_quiet([s], 24.0)
        self.assertFalse(ok)
        self.assertEqual(nums["why"], "phi_asserted_not_measured")


class TestTheQuietTest(LipTestCase):
    """(E1): zero fills, pooled over the family, over exposure >= 3h — the SAME Rule-of-Three
    line `alloc.Need.evidence_bounds_a_turnover` already draws per rung, pooled."""

    def test_a_quiet_family_with_enough_pooled_exposure_qualifies(self):
        fam = [quiet_slot("KXUST-26JUL31-T%d" % i, exposure=30.0) for i in range(4)]
        ok, nums = QT.is_quiet(fam, 24.0)
        self.assertTrue(ok, nums)
        self.assertGreaterEqual(nums["exposure_h"], 3.0 * 24.0)
        self.assertLessEqual(nums["phi_upper_95"] * 24.0, 1.0,
                             "the class's whole claim: this lot will not turn over today")

    def test_pooling_is_what_licenses_a_strike_we_have_never_touched(self):
        """Per-strike evidence could never open a new tenor; the family's can.  Revert the
        pooling and this test fails on the exact symptom."""
        fam = [quiet_slot("KXUST-26JUL31-T%d" % i, exposure=30.0) for i in range(4)]
        virgin = quiet_slot("KXUST-26JUL31-T9", exposure=0.0)
        self.assertFalse(QT.is_quiet([virgin], 24.0)[0])
        self.assertTrue(QT.is_quiet(fam + [virgin], 24.0)[0])

    def test_ANY_fill_disqualifies_the_family(self):
        fam = [quiet_slot("KXUST-26JUL31-T%d" % i, exposure=30.0) for i in range(4)]
        prior, k, e = 0.3, 10.0, 30.0
        fam.append(slot("KXUST-26JUL31-T8", phi=(1.0 + k * prior) / (e + k),
                        phi_prior=prior, phi_k=k, exposure=e))
        ok, nums = QT.is_quiet(fam, 24.0)
        self.assertFalse(ok)
        self.assertEqual(nums["why"], "own_fills_observed")

    def test_thin_exposure_is_refused_by_the_rule_of_three(self):
        fam = [quiet_slot("KXUST-26JUL31-T%d" % i, exposure=2.0) for i in range(4)]
        ok, nums = QT.is_quiet(fam, 24.0)
        self.assertFalse(ok)
        self.assertEqual(nums["why"], "exposure_below_3h")
        self.assertAlmostEqual(nums["need_exposure_h"], 3.0 * 24.0, places=6)

    def test_public_prints_disqualify_a_family_our_own_tape_calls_quiet(self):
        """(E2): our zero can mean 'nobody trades here' or 'we were never at the touch'."""
        fam = [quiet_slot("KXUST-26JUL31-T%d" % i, exposure=30.0) for i in range(4)]
        self.assertTrue(QT.is_quiet(fam, 24.0, trades=0)[0])
        ok, nums = QT.is_quiet(fam, 24.0, trades=5)
        self.assertFalse(ok)
        self.assertEqual(nums["why"], "public_trades_observed")

    def test_the_missing_trades_feed_is_LOGGED_not_assumed_away(self):
        fam = [quiet_slot("KXUST-26JUL31-T%d" % i, exposure=30.0) for i in range(4)]
        QT.classify(fam, 24.0)
        rec = self.logs_of("quiet_family")
        self.assertTrue(rec and rec[0]["trades_evidence"] == "absent", rec)

    def test_classification_logs_every_number_that_decided_it(self):
        fam = [quiet_slot("KXUST-26JUL31-T%d" % i, exposure=30.0) for i in range(4)]
        out, phi = QT.classify(fam, 24.0)
        self.assertEqual(out, {"KXUST"})
        self.assertAlmostEqual(phi["KXUST"], C.RULE_OF_THREE / 120.0, places=9)
        rec = self.logs_of("quiet_family")[0]
        for field in ("fills", "exposure_h", "phi_upper_95", "need_exposure_h", "quiet"):
            self.assertIn(field, rec)


class TestLadderWidePresence(LipTestCase):
    """note 55 final amendment 2: one-market-per-cluster is RELAXED for this class, and the
    DOLLAR rail is the correlation bound."""

    def _ladder(self, n=6, rho=0.6):
        # A quiet treasury ladder: n strikes, empty sides (S = 0), 1c walls of 1,000.
        return [quiet_slot("KXUST-26JUL31-T%d" % i, rho=rho, S=0.0, p=0.01,
                           target_size=1000, cum_size=0.0, exposure=30.0)
                for i in range(n)]

    def _phi(self, ladder):
        return QT.classify(ladder, 24.0)[1]

    def test_without_the_relaxation_ONE_strike_takes_the_cluster(self):
        ladder = self._ladder()
        a, _s, rep = MQ.allocate_marginal(ladder, budget_usd=600.0,
                                          per_market_cap_usd=21.43, cluster_cap_usd=21.43,
                                          phi_by_cluster=self._phi(ladder))
        self.assertEqual(sum(1 for q in a.values() if q > 0), 1)
        self.assertGreaterEqual(rep["reasons"].get(MQ.CLUSTER_TAKEN, 0), 1)

    def test_with_it_the_whole_ladder_rests_inside_ONE_dollar_rail(self):
        ladder = self._ladder()
        a, spent, _rep = MQ.allocate_marginal(ladder, budget_usd=600.0,
                                              per_market_cap_usd=21.43,
                                              cluster_cap_usd=21.43,
                                              multi_market_clusters={"KXUST"},
                                              phi_by_cluster=self._phi(ladder))
        funded = [k for k, q in a.items() if q > 0]
        self.assertGreaterEqual(len(funded), 2,
                                "ladder-wide presence must fund more than one strike: %s" % a)
        self.assertLessEqual(spent, 21.43 + 1e-6,
                             "the CLUSTER DOLLAR RAIL is the correlation bound: %s" % spent)

    def test_a_strike_that_cannot_clear_its_OWN_dollar_is_not_funded(self):
        """"fat-enough-per-strike or not funded" — and it is the cliff doing it, per strike,
        with no new rule: a strike whose whole half-pool cannot reach $1.00 in the window is
        `cliff_unreachable`."""
        thin = self._ladder(n=3, rho=0.02)                 # half-pool over 24h = $0.24
        a, spent, rep = MQ.allocate_marginal(thin, budget_usd=600.0,
                                             per_market_cap_usd=21.43,
                                             cluster_cap_usd=21.43,
                                             multi_market_clusters={"KXUST"},
                                             phi_by_cluster=self._phi(thin))
        self.assertEqual(spent, 0.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 0)
        self.assertEqual(rep["reasons"].get(MQ.UNREACHABLE_CLIFF), 3)

    def test_the_relaxation_does_not_relax_a_single_dollar_bound(self):
        ladder = self._ladder(n=20)
        _a, spent, _rep = MQ.allocate_marginal(ladder, budget_usd=600.0,
                                               per_market_cap_usd=21.43,
                                               cluster_cap_usd=21.43,
                                               multi_market_clusters={"KXUST"},
                                               phi_by_cluster=self._phi(ladder))
        self.assertLessEqual(spent, 21.43 + 1e-6)


class TestTheWallEconomics(LipTestCase):
    """THE WORKED NUMBERS, at C = 600 and the derived rail.

        rail          A = C/N = 600/28 = $21.43 at the reference mix
        wall          target_size 1,000 contracts at 1c = $10.00 of collateral
        share         S = 0 (we ARE the side) ⇒ share = 1,000/1,000 = 1.0
        credit        share x pool/2 x h  — must clear $1.00/window or it is not funded
        bleed         W x T x g(1c) = $10 x T x 0.9484 — ~$0 only because T ~ 0
    """

    def test_a_1000_contract_wall_at_1c_costs_ten_dollars_and_fits_the_rail(self):
        rail = D.derive(600.0, [("KXA", 20.0, C.RUIN_P_REF_PRICE, 0.0, 24.0)]).rail_usd
        self.assertAlmostEqual(rail, 21.4285714, places=6)
        w = quiet_slot("KXUST-26JUL31-T1", rho=0.6, S=0.0, p=0.01, target_size=1000,
                       cum_size=0.0, exposure=100.0)
        cv = MQ.Curve(w)
        self.assertEqual(cv.qualify_q, 1000)
        self.assertEqual(cv.q_entry, 1001, "the walk plus the one contract that takes it")
        self.assertAlmostEqual(cv.capital(cv.q_entry), 10.01, places=6)
        self.assertLess(cv.capital(cv.q_entry), rail)

    def test_the_wall_pays_because_the_family_is_quiet_and_not_otherwise(self):
        quiet_w = quiet_slot("KXUST-26JUL31-T1", rho=0.6, S=0.0, p=0.01, target_size=1000,
                             cum_size=0.0, exposure=100.0)
        hot_w = slot("KXHOT-26JUL31-T1", rho=0.6, S=0.0, p=0.01, target_size=1000,
                     cum_size=0.0, phi=0.05, phi_prior=0.05, phi_k=10.0, exposure=100.0)
        self.assertGreater(MQ.Curve(quiet_w).entry_rate(), 0.0)
        self.assertLessEqual(MQ.Curve(hot_w).entry_rate(), 0.0,
                             "g(1c) = 0.9484: a wall in a family that actually fills is a "
                             "loss, and the screen must say so")
        a, _s, rep = MQ.allocate_marginal([hot_w], budget_usd=600.0)
        self.assertEqual(a[hot_w.key], 0)
        self.assertEqual(rep["reasons"].get(MQ.NEGATIVE_ENTRY), 1)

    def test_the_wall_earns_its_own_dollar(self):
        w = quiet_slot("KXUST-26JUL31-T1", rho=0.6, S=0.0, p=0.01, target_size=1000,
                       cum_size=0.0, exposure=100.0)
        cv = MQ.Curve(w)
        # share = 1 (we are the whole side), half-pool = rho/2 x h_eff
        self.assertAlmostEqual(cv.share(cv.q_entry), 1.0, places=6)
        self.assertGreaterEqual(cv.paid(cv.q_entry), C.CREDIT_TARGET_USD)
        self.assertGreater(cv.net(cv.q_entry), 0.0)

    def test_both_sides_of_a_quiet_strike_can_qualify_from_one_seat(self):
        """note 55 final amendment 1: the second side is a fresh half-pool, and in the quiet
        class the two-sided condition clears because both sides rest unfilled."""
        bid = quiet_slot("KXUST-26JUL31-T1", side="bid", rho=0.6, S=0.0, p=0.01,
                         target_size=1000, cum_size=0.0, exposure=100.0)
        ask = quiet_slot("KXUST-26JUL31-T1", side="ask", rho=0.6, S=0.0, p=0.99,
                         target_size=1000, cum_size=0.0, exposure=100.0)
        a, spent, _r = MQ.allocate_marginal([bid, ask], budget_usd=600.0,
                                            per_market_cap_usd=21.43, cluster_cap_usd=21.43,
                                            phi_by_cluster=QT.classify([bid, ask], 24.0)[1])
        self.assertGreater(a[bid.key], 0)
        self.assertGreater(a[ask.key], 0, "the NO side is 1c of NO, not 99c of YES")
        self.assertLessEqual(spent, 21.43 + 1e-6)
