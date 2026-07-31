"""THE OWNER'S LAW (Ryan, 2026-07-30) — the allocator's own tests.

The spec is the owner's sizing paragraph, verbatim in `alloc.py`'s law header, and the first
three tests reproduce his three examples NUMERICALLY.  Every other test here is
mutation-checked against a specific clause of the law: revert the clause and the named test
fails on the exact symptom.

The reading of "dollar-hours" and of phi-as-turnovers is stated once, in the law header in
`alloc.py`; these tests are written against that reading and are the record of it.
"""

import unittest

from .. import alloc, config as C, scan
from .base import LipTestCase


# rho recalibrated 1.25 -> 5/6 when the owner set the target to the forfeit floor exactly
# (CREDIT_TARGET_MARGIN 1.0, 2026-07-30 night): the fixtures pin W = $5.00, and W depends on
# s = target/((rho/2)*24) — the same s = 0.10 that 1.25 gave at $1.50 needs rho = 5/6 at $1.00.
def slot(ticker="KXLAW-26JUL30-T1", side="bid", rho=5.0 / 6.0, S=180.0, p=0.10, phi=0.0,
         hours_left=24.0, accrued=0.0, target_size=1000, cum_size=2000.0, **kw):
    """A candidate whose side already qualifies on rival depth (cum >= target), unless a
    test says otherwise.  Defaults reproduce owner example 1 when phi = 2.5/24."""
    return alloc.Slot(ticker, side, rho=rho, S=S, p=p, phi=phi, hours_left=hours_left,
                      accrued=accrued, target_size=target_size, cum_size=cum_size,
                      land_grab_price_c=C.ENTRY_BAND_LO_C, **kw)


class TestTheOwnersRulingOnSizing(LipTestCase):
    """THE OWNER'S RULING (2026-07-30), which SUPERSEDES his examples' literal arithmetic —
    he set the examples aside where they conflict and ruled from the machine:

      1. ORDER = W, the full resting size the share-math demands.  Never shrunk to stretch
         across turnovers.
      2. Turnovers enter ONLY the affordability screen: W x max(1, T) <= $10, else SKIP with
         the numbers logged.  The screen compares the UNROUNDED W.
      3. Oversize beyond W only when the low phi is OUR OWN HISTORY'S — own exposure > k,
         the prior's strength (owner, 2026-07-30 night; supersedes G3's phi_source test,
         which two quiet contract-hours were enough to satisfy).  Tested in
         TestOversizeRequiresAMeasurement below.

    Fixtures pin the ruling's own four cases (a)-(d)."""

    def test_a_W5_at_T2_5_is_SKIPPED_unaffordable_with_the_numbers(self):
        """(a) need $5 resting, T = 2.5: total $12.50 > $10 ⇒ SKIP.  NOTE: this INVERTS the
        owner's old example 1 ("we will put in 5 dollars") — the ruling wins, explicitly:
        turnovers are an affordability screen, and $5 x 2.5 does not fit the allocation."""
        s = slot(p=0.25, phi=2.5 / 24.0)          # q_rest = 20 @ 25c ⇒ W = $5; T = 2.5
        n = alloc.law_need(s)
        self.assertAlmostEqual(n.rest_usd, 5.0, places=9)
        self.assertAlmostEqual(n.turnovers, 2.5, places=9)
        self.assertAlmostEqual(n.total_usd, 12.5, places=9)
        a, spent, rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], 0)
        self.assertEqual(spent, 0.0)
        self.assertEqual(rep["reasons"].get("unaffordable"), 1)
        ex = self.logs_of("law_example")
        self.assertTrue(ex and ex[0]["rest_usd"] == 5.0 and ex[0]["turnovers"] == 2.5
                        and ex[0]["total_usd"] == 12.5,
                        "the skip must carry W, T and the total: %s" % ex)

    def test_b_W5_at_T2_funds_at_order_exactly_5(self):
        """(b) the same market at T = 2.0: total $10.00 fits, and the ORDER IS W — five
        dollars exactly, never oversized (T > 1) and never shrunk (rule 1)."""
        s = slot(p=0.25, phi=2.0 / 24.0)
        n = alloc.law_need(s)
        self.assertAlmostEqual(n.total_usd, 10.0, places=9)
        a, spent, _rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], 20)
        self.assertAlmostEqual(a[s.key] * s.p, 5.0, places=9)

    def test_c_the_example_2_boundary_funds_the_42c_order(self):
        """(c) the owner's example-2 world with integer rounding: q_raw = 20.83 at 2c rounds
        to a 21-contract, 42c order whose ROUNDED consumption is $10.08.  The affordability
        screen compares the UNROUNDED W x T (= $10.00 exactly) against the allocation —
        stated choice, per the ruling: a skip caused by rounding one contract up would
        refuse the owner's own example-2 market.  MUST FUND, at order 42c."""
        # rho 0.625 -> 5/12: the $10 boundary case recalibrated for the $1.00 target
        s = slot(rho=5.0/12.0, S=83.3333333333, p=0.02, phi=1.0)   # T = 24
        n = alloc.law_need(s)
        self.assertEqual(n.q_rest, 21)
        self.assertAlmostEqual(n.rest_usd, 0.42, places=9)
        self.assertAlmostEqual(n.total_usd, 10.0, places=6)       # unrounded, exactly
        a, _spent, rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], 21)
        self.assertAlmostEqual(a[s.key] * s.p, 0.42, places=9)
        self.assertEqual(rep["reasons"], {})

    def test_d_measured_low_phi_small_need_puts_all_ten(self):
        """(d) = the owner's example 3: "we can earn a dollar in 24 hours with only one
        dollar, we will put all 10."  phi here is ASSERTED by the caller as a fact of zero
        (`phi_exposure_h` unset — see `alloc.Slot`), so the oversize stands.  A phi that came
        from a SHRUNK estimate has to earn it with exposure; see the incident test below."""
        s = slot(rho=5.0/6.0, S=90.0, p=0.10, phi=0.0)      # exposure unset: a given fact
        n = alloc.law_need(s)
        self.assertAlmostEqual(n.total_usd, 1.0, places=9)
        a, spent, _rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertAlmostEqual(a[s.key] * s.p, 10.0, places=6)
        self.assertAlmostEqual(spent, 10.0, places=6)

    def test_unaffordable_is_skipped_with_numbers_never_silent(self):
        """"if it doesn't fit in there, we can't afford it" — the ruling KEEPS this screen
        verbatim (three silent-refusal incidents on 2026-07-30 are why the numbers ride
        every skip)."""
        s = slot(phi=6.0 / 24.0)                  # W = $2, T = 6: total = $12
        n = alloc.law_need(s)
        self.assertAlmostEqual(n.total_usd, 12.0, places=9)
        a, spent, rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], 0)
        self.assertEqual(spent, 0.0)
        self.assertEqual(rep["reasons"].get("unaffordable"), 1)
        self.assertTrue(self.logs_of("law_reasons"))


class TestOversizeRequiresAMeasurement(LipTestCase):
    """REWRITTEN 2026-07-30 night, after the incident this class FAILED TO PREVENT.

    G3 gated the oversize on `phi_source` — was phi "measured" (or borrowed from a measured
    neighborhood) rather than the seed.  Under the old ladder "measured" INCLUDED zero fills
    over DECISIVE_COMMITTED_H = 2 contract-hours, which for a 166-contract rung is 43
    seconds.  A quiet afternoon put several rungs at phi = 0 on nothing, the full $10 rested
    on each, and the evening flow ate them: 42 fills, ~$76 of inventory conversion in 8h.

    The owner's fix (Ryan, verbatim intent): "we should take our global average and use the
    rung's history to adjust it until the history is very long."  phi is now shrunk toward
    its price-bucket prior with strength k (`scan.phi_posterior` / `scan.phi_k`), and the
    oversize asks the only question that survives: IS THIS RUNG'S OWN HISTORY WHAT MADE THE
    NUMBER LOW — own exposure > k (`Need.history_dominates`)?"""

    def _big_need(self, **kw):
        # q_raw = 60 @ 10c ⇒ W = $6 > the $5 lot container
        kw.setdefault("rho", 1.25); kw.setdefault("S", 540.0); kw.setdefault("p", 0.10)
        return slot(**kw)

    def test_THE_INCIDENT_two_quiet_hours_may_not_unlock_the_envelope(self):
        """TONIGHT'S EXACT SHAPE, pinned (2026-07-30).  A rung whose price bucket measures
        ~0.3 fills per contract-hour rests quietly for 2 contract-hours: zero fills.

        Under the ladder that was a "measured" phi of 0.0 ⇒ oversize ⇒ the whole $10.
        Under shrinkage k = 0.3 / v_seed = 577 contract-hours, so the two hours buy 0.35% of
        the answer, the posterior stays at 0.2990 — the prior, essentially untouched — and
        `history_dominates` is FALSE.  The order sizes to the rung's actual need, tranched
        at the lot container so the requote reserve is held back.  MUTATION: restore the
        ladder (phi = 0.0 with phi_source "measured") and this test fails on the incident's
        exact symptom — $10 resting on 2 hours of quiet."""
        prior, expo = 0.3, 2.0
        k = scan.phi_k(prior)
        self.assertAlmostEqual(k, 577.0, places=0)
        phi = scan.phi_posterior(0, expo, prior, k)
        self.assertAlmostEqual(phi, 0.2990, places=4)
        self.assertGreater(phi, 0.99 * prior, "the posterior must stay AT the prior")
        # The incident's rung, affordable so the SIZING is what is on trial: W = $1.00 at
        # 10c (q_rest = 10), T = 7.2 turnovers ⇒ total $7.18, inside the $10 allocation.
        s = slot(rho=5.0 / 6.0, S=90.0, p=0.10, phi=phi, phi_source="bucket",
                 phi_prior=prior, phi_k=k, phi_exposure_h=expo)
        n = alloc.law_need(s)
        self.assertEqual(n.q_rest, 10)
        self.assertFalse(n.history_dominates)
        self.assertLessEqual(n.total_usd, 10.0)
        a, spent, _rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], n.q_rest, "the order must size to the NEED, not the $10")
        self.assertAlmostEqual(a[s.key] * s.p, 1.0, places=6)
        self.assertLessEqual(a[s.key] * s.p, C.SLOT_LOT_CAP_USD + 1e-9,
                             "THE INCIDENT: 2 quiet hours put the full envelope down")
        # The requote reserve IS what is held back: the rung rests $1.00 of the $10 it is
        # allowed, so the flow that ate the oversized seats can only reach a tenth of it.
        self.assertGreaterEqual(C.ALLOC_PER_MARKET_USD - a[s.key] * s.p, 9.0)
        self.assertAlmostEqual(spent, n.total_usd, places=6)   # the CHARGE is W x T (law §1)

    def test_a_LONG_quiet_rung_still_earns_the_envelope(self):
        """The other half, and the reason this is shrinkage and not a refusal: the SAME rung
        after 5,000 contract-hours of quiet has a posterior of 0.032, its own history owns
        90% of that number, and example 3 stands — "we can earn a dollar in 24 hours with
        only one dollar, we will put all 10"."""
        prior, expo = 0.3, 5000.0
        k = scan.phi_k(prior)
        phi = scan.phi_posterior(0, expo, prior, k)
        s = self._big_need(phi=phi, phi_source="bucket", phi_prior=prior, phi_k=k,
                           phi_exposure_h=expo)
        n = alloc.law_need(s)
        self.assertTrue(n.history_dominates)
        self.assertLessEqual(n.turnovers, 1.0)
        a, _spent, _rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertAlmostEqual(a[s.key] * s.p, 10.0, places=6)

    def test_a_thinly_observed_slot_never_orders_above_the_lot_container(self):
        """Prior-dominated ⇒ tranche at SLOT_LOT_CAP_USD (the per-source reserve halved so
        one re-post is guaranteed — an existing derivation, no new constant): a rung we have
        not watched longer than its prior is worth can never be one fill from done."""
        s = self._big_need(phi=0.001, phi_source="seed", phi_prior=0.001,
                           phi_k=10.0, phi_exposure_h=1.0)
        a, _spent, _rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertGreater(a[s.key], 0)
        self.assertLessEqual(a[s.key] * s.p, C.SLOT_LOT_CAP_USD + 1e-9)

    def test_the_gate_is_exposure_vs_k_not_the_priors_name(self):
        """phi_source no longer gates anything — it explains the prior.  The SAME source
        label sizes both ways, decided only by exposure against k."""
        for src_ in ("bucket", "global", "seed"):
            thin = self._big_need(phi=0.0, phi_source=src_, phi_prior=0.05, phi_k=100.0,
                                  phi_exposure_h=99.0)
            a, _s, _r = alloc.allocate_law([thin], budget_usd=300.0)
            self.assertLessEqual(a[thin.key] * thin.p, C.SLOT_LOT_CAP_USD + 1e-9, msg=src_)
            long_ = self._big_need(phi=0.0, phi_source=src_, phi_prior=0.05, phi_k=100.0,
                                   phi_exposure_h=101.0)
            a, _s, _r = alloc.allocate_law([long_], budget_usd=300.0)
            self.assertAlmostEqual(a[long_.key] * long_.p, 10.0, places=6, msg=src_)

    def test_the_posteriors_composition_rides_the_funded_log_line(self):
        """No silent behavior: the tape must show WHY a size was chosen — the prior, its
        strength, our own exposure and which of the two won."""
        s = self._big_need(phi=0.02, phi_source="bucket", phi_prior=0.3, phi_k=577.0,
                           phi_exposure_h=2.0)
        alloc.allocate_law([s], budget_usd=300.0)
        f = self.logs_of("law_funded")
        self.assertTrue(f, "no law_funded line")
        self.assertEqual(f[0]["phi_source"], "bucket")
        self.assertAlmostEqual(f[0]["phi_prior"], 0.3, places=6)
        self.assertAlmostEqual(f[0]["phi_k"], 577.0, places=4)
        self.assertAlmostEqual(f[0]["own_exposure_h"], 2.0, places=4)
        self.assertIs(f[0]["history_dominates"], False)

    def test_a_self_qualifying_walk_posts_the_walk_exactly(self):
        """The walk is ALL-OR-NOTHING (the filing's step function): a sub-walk order scores
        zero, so the qualify case posts q_rest exactly — no seed tranche (which would buy a
        worthless sub-walk) and no oversize (at S = 0 extra size buys no share)."""
        s = slot(S=0.0, cum_size=0.0, target_size=100, p=0.06, rho=1.25,
                 phi=0.001, phi_source="seed")
        a, _spent, _rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], 101)


class TestRankingAndAccrual(LipTestCase):

    def test_cheapest_need_first_until_capital_is_gone(self):
        """Law §1: fund cheapest-need first.  Three markets in three clusters, budget $20:
        the two cheapest fund, the third logs budget_exhausted."""
        cheap = slot(ticker="KXA-26-T1", rho=1.25, S=90.0, p=0.05)     # need $0.50
        mid = slot(ticker="KXB-26-T1", rho=1.25, S=90.0, p=0.10)       # need $1.00
        dear = slot(ticker="KXC-26-T1", rho=1.25, S=90.0, p=0.50)      # need $5.00
        a, spent, rep = alloc.allocate_law([dear, mid, cheap], budget_usd=20.0)
        self.assertGreater(a[cheap.key], 0)
        self.assertGreater(a[mid.key], 0)
        self.assertEqual(a[dear.key], 0)
        self.assertEqual(rep["reasons"].get("budget_exhausted"), 1)

    def test_accrued_subtracts_from_the_need(self):
        """Law §1: accrued banked there subtracts.  Same market, $1.00 already accrued ⇒
        need $0.50 ⇒ q_rest shrinks.  Mutation: drop the subtraction and the two are equal."""
        bare = alloc.law_need(slot())
        banked = alloc.law_need(slot(accrued=0.5))
        self.assertLess(banked.q_rest, bare.q_rest)
        self.assertAlmostEqual(banked.need_usd, 0.5, places=9)

    def test_accrued_at_target_is_DONE_and_frees_its_cluster(self):
        """Law §1: "a market with accrual >= $1.50 is DONE — skip, fund next-best in its
        cluster or elsewhere."  Two rungs of one ladder: the cheaper is DONE, the sibling is
        funded in its place."""
        done = slot(ticker="KXLAD-26-T1", accrued=1.55, p=0.05)
        sib = slot(ticker="KXLAD-26-T2", p=0.10)
        a, _spent, rep = alloc.allocate_law([done, sib], budget_usd=300.0)
        self.assertEqual(a[done.key], 0)
        self.assertGreater(a[sib.key], 0)
        self.assertEqual(rep["reasons"].get("done"), 1)

    def test_restart_produces_the_same_ranking_no_discovery_order(self):
        """Law §10: same world ⇒ same book, whatever order the slots arrive in."""
        ss = [slot(ticker="KX%s-26-T1" % ch, p=0.05 + i * 0.01)
              for i, ch in enumerate("ABCDEFG")]
        a1, s1, _r1 = alloc.allocate_law(list(ss), budget_usd=40.0)
        a2, s2, _r2 = alloc.allocate_law(list(reversed(ss)), budget_usd=40.0)
        self.assertEqual(a1, a2)
        self.assertAlmostEqual(s1, s2, places=9)


class TestOneOrderPerCluster(LipTestCase):

    def test_one_ladder_one_order(self):
        """Law §2.  Two strikes of one series are one cluster: the cheaper wins the seat,
        the sibling logs cluster_taken."""
        r1 = slot(ticker="KXGAS-26-T3.10", p=0.05)
        r2 = slot(ticker="KXGAS-26-T3.20", p=0.10)
        a, _s, rep = alloc.allocate_law([r1, r2], budget_usd=300.0)
        self.assertGreater(a[r1.key], 0)
        self.assertEqual(a[r2.key], 0)
        self.assertEqual(rep["reasons"].get("cluster_taken"), 1)

    def test_both_sides_of_one_market_are_one_cluster(self):
        """Law §2: "both sides of one market" resolve the same — only one side funds."""
        bid = slot(side="bid", p=0.10)
        ask = slot(side="ask", p=0.12)
        a, _s, _rep = alloc.allocate_law([bid, ask], budget_usd=300.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 1)
        self.assertGreater(a[bid.key], 0, "the cheaper side takes the seat")

    def test_treasury_tenors_are_one_cluster(self):
        """Law §2: all treasury tenors are one settle source (clusters.CLUSTER_MAP)."""
        t2 = slot(ticker="KXUST2AD-26-T4.1", p=0.05)
        t30 = slot(ticker="KXUST30AD-26-T4.8", p=0.10)
        a, _s, rep = alloc.allocate_law([t2, t30], budget_usd=300.0)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 1)
        self.assertEqual(rep["reasons"].get("cluster_taken"), 1)


class TestTheFloor(LipTestCase):

    def test_short_window_target_clamps_at_the_cliff_not_prorated(self):
        """Law §5: "pro-rating a short window earns credit that forfeits."  8h left ⇒ the
        pro-rated pace is $0.50, which Kalshi pays ZERO for — the target clamps at $1.00.
        Mutation: remove the clamp and target reads 0.50."""
        target, h = alloc.law_target_usd(8.0)
        self.assertAlmostEqual(h, 8.0)
        self.assertAlmostEqual(target, C.CREDIT_TARGET_USD)

    def test_long_window_targets_the_150_pace(self):
        target, h = alloc.law_target_usd(100.0)
        self.assertAlmostEqual(h, 24.0)
        self.assertAlmostEqual(target, C.ENTRY_FLOOR_USD)

    def test_a_window_that_cannot_reach_a_dollar_is_unreachable(self):
        """Never below $1.00 by window end: a 2h window whose half-pool is $0.60 cannot
        clear the cliff at ANY size — skipped as unreachable, never funded small."""
        s = slot(rho=0.6, hours_left=2.0)         # avail = 0.3 x 2 = $0.60 < $1.00
        n = alloc.law_need(s)
        self.assertEqual(n.reason, "unreachable")
        a, spent, rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], 0)
        self.assertEqual(rep["reasons"].get("unreachable"), 1)


class TestQualificationIsAFormulaInputNotAGate(LipTestCase):

    def test_empty_side_prices_self_qualification_and_logs_the_skip(self):
        """Law §7a: where rival depth does not qualify the side, the cost includes
        self-qualifying — 1,000 contracts at the 6c band floor is $60 against a $10
        allocation, so the skip is UNAFFORDABLE, with the qualify numbers in the log."""
        s = slot(S=0.0, cum_size=0.0, target_size=1000, p=0.06)
        n = alloc.law_need(s)
        self.assertEqual(n.qualify_q, 1000)
        self.assertGreater(n.total_usd, C.ALLOC_PER_MARKET_USD)
        a, _spent, rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], 0)
        self.assertEqual(rep["reasons"].get("unaffordable"), 1)
        ex = self.logs_of("law_example")
        self.assertTrue(any(e.get("qualify_q") == 1000 for e in ex),
                        "the skip must carry the qualification arithmetic: %s" % ex)

    def test_a_small_walk_can_be_bought_and_rides_as_the_order(self):
        """The rare affordable case: a 100-contract walk at 6c is $6.06 — our own resting
        size counts toward the walk, so the qualifying depth IS the order."""
        s = slot(S=0.0, cum_size=0.0, target_size=100, p=0.06, rho=1.25)
        n = alloc.law_need(s)
        self.assertEqual(n.reason, "")
        self.assertLessEqual(n.total_usd, C.ALLOC_PER_MARKET_USD)
        a, _spent, _rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertGreaterEqual(a[s.key], 101)    # the walk plus the earning contract

    def test_free_riding_on_rival_depth_costs_nothing(self):
        """Where rivals already qualify the side, qualify_usd is $0 — the ride is free."""
        n = alloc.law_need(slot())
        self.assertEqual(n.qualify_q, 0)
        self.assertEqual(n.qualify_usd, 0.0)


class TestTheRequoteBudget(LipTestCase):

    def test_fills_consume_the_allocation_and_exhaust_it(self):
        """Law §4: the $10 is an allocation; inventory basis bought there is the consumed
        part.  At $9.95 spent the envelope is under one contract: allocation_exhausted."""
        s = slot()
        a, _spent, rep = alloc.allocate_law([s], budget_usd=300.0,
                                            market_spent={s.ticker: 9.95})
        self.assertEqual(a[s.key], 0)
        self.assertEqual(rep["reasons"].get("allocation_exhausted"), 1)

    def test_partial_consumption_shrinks_the_order(self):
        """$4 of the $10 already bought ⇒ the envelope is $6 and the order shrinks with it
        — the remainder IS the requote budget."""
        s = slot(phi=0.0)
        a_full, _s1, _r1 = alloc.allocate_law([s], budget_usd=300.0)
        a_part, _s2, _r2 = alloc.allocate_law([s], budget_usd=300.0,
                                              market_spent={s.ticker: 4.0})
        self.assertAlmostEqual(a_full[s.key] * s.p, 10.0, places=6)
        self.assertAlmostEqual(a_part[s.key] * s.p, 6.0, places=6)

    def test_the_total_cap_binds_at_300(self):
        """Law §3: $300 total.  31 clusters of $10 markets fund at most 30."""
        ss = [slot(ticker="KXM%02d-26-T1" % i, p=0.10) for i in range(31)]
        a, spent, _rep = alloc.allocate_law(ss, budget_usd=1e9)
        self.assertLessEqual(spent, C.MAX_TOTAL_COLLATERAL_USD + 1e-9)
        self.assertEqual(sum(1 for q in a.values() if q > 0), 30)


if __name__ == "__main__":
    unittest.main()
