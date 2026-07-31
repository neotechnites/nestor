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

    Fixtures pin the ruling's own four cases (a)-(d).

    FIXTURE PRICES MOVED TO 62.5c, 2026-07-30 night: these cases test the AFFORDABILITY
    screen (W x max(1, T) vs the $10 allocation) and nothing else.  The fill-bleed viability
    screen added the same night (alloc's "THE FILL-BLEED TERM" header) refuses the original
    10c/25c fixtures on EV before affordability is ever reached — correctly, that is the
    incident being fixed — which would have made these tests assert the wrong screen's
    reason.  62.5c sits in the g = 0 band, so the bleed term is exactly zero here and the
    arithmetic under test is unchanged: W, T and the $10 boundary are identical.  The bleed
    screen has its own tests in TestTheFillBleedTerm."""

    def test_a_W5_at_T2_5_is_SKIPPED_unaffordable_with_the_numbers(self):
        """(a) need $5 resting, T = 2.5: total $12.50 > $10 ⇒ SKIP.  NOTE: this INVERTS the
        owner's old example 1 ("we will put in 5 dollars") — the ruling wins, explicitly:
        turnovers are an affordability screen, and $5 x 2.5 does not fit the allocation."""
        s = slot(S=72.0, p=0.625, phi=2.5 / 24.0)  # q_rest = 8 @ 62.5c ⇒ W = $5; T = 2.5
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
        s = slot(S=72.0, p=0.625, phi=2.0 / 24.0)
        n = alloc.law_need(s)
        self.assertAlmostEqual(n.total_usd, 10.0, places=9)
        a, spent, _rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], 8)
        self.assertAlmostEqual(a[s.key] * s.p, 5.0, places=9)

    def test_c_the_unrounded_screen_funds_the_boundary_order(self):
        """(c) INTEGER ROUNDING AT THE $10 BOUNDARY.  q_raw = 6.4 at 62.5c rounds to a
        7-contract, $4.375 order whose ROUNDED consumption is 7 x 62.5c x 2.5 = $10.9375.
        The affordability screen compares the UNROUNDED W x T (= $10.00 exactly) against the
        allocation — stated choice, per the ruling: a skip caused by rounding one contract up
        would refuse a market the owner's own example 2 says to fund.  MUST FUND, at order 7.

        RE-PRICED 2026-07-30 night, and the ORIGINAL FIXTURE IS NOW test_c2 BELOW.  This case
        used to be run at the owner's example-2 world literally — 2c, T = 24 — and that world
        is exactly the 2c wall the fill-bleed term exists to refuse (g(2c) = 1.0000: every
        dollar filled there is lost).  The ROUNDING property is what this test is for, so it
        is preserved at a price where the bleed is zero; the example-2 world's new verdict is
        pinned separately, because a refusal that used to be a funding is a fact about the
        law, not a lost test."""
        s = slot(S=57.6, p=0.625, phi=2.5 / 24.0)                 # q_raw = 6.4, T = 2.5
        n = alloc.law_need(s)
        self.assertEqual(n.q_rest, 7)
        self.assertAlmostEqual(n.rest_usd, 4.375, places=9)
        self.assertAlmostEqual(n.total_usd, 10.0, places=6)       # unrounded, exactly
        self.assertAlmostEqual(7 * s.p * n.turnovers, 10.9375, places=6)   # rounded: over
        a, _spent, rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], 7)
        self.assertEqual(rep["reasons"], {})

    def test_c2_the_example_2_world_is_now_REFUSED_for_bleed(self):
        """(c2) THE ORIGINAL (c), kept verbatim as a fixture and re-adjudicated.  The owner's
        example-2 world is a 2c rung expected to turn over 24 times inside the horizon.  Its
        capital arithmetic is unchanged and still lands exactly on the $10 boundary — the
        ruling's unrounded screen still passes it.  It is refused anyway, and for the reason
        the whole 2026-07-30-night fix exists:

            g(2c) = 1.0000 (n = 1,368 side-observations; realised 0.00%)
            bleed = W x T x g = $0.41667 x 24 x 1.0000 = $10.00
                    (W is the UNROUNDED resting need, the same quantity total_usd is built
                     from — the ruling's rounding tolerance applies to the bleed too, so the
                     bleed cannot flip a decision on one contract either)
            credit the pool would pay over the horizon = $1.00

        $10.00 of expected permanent loss to earn $1.00.  This is the 2c/3c wall, and it is
        never funded at any price or any rank."""
        s = slot(rho=5.0/12.0, S=83.3333333333, p=0.02, phi=1.0)   # T = 24
        n = alloc.law_need(s)
        self.assertEqual(n.q_rest, 21)
        self.assertAlmostEqual(n.rest_usd, 0.42, places=9)
        self.assertAlmostEqual(n.total_usd, 10.0, places=6)        # capital: still affordable
        self.assertAlmostEqual(n.g, 1.0, places=9)
        self.assertAlmostEqual(n.bleed_usd, 10.0, places=6)
        self.assertAlmostEqual(n.effective_usd, 20.0, places=6)    # committed + destroyed
        self.assertEqual(n.reason, alloc.BLEED)
        a, spent, rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], 0)
        self.assertEqual(spent, 0.0)
        self.assertEqual(rep["reasons"].get(alloc.BLEED), 1)

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
        s = slot(S=28.8, p=0.625, phi=6.0 / 24.0)  # W = $2, T = 6: total = $12
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

    # PRICE MOVED 10c -> 62.5c, 2026-07-30 night (the fill-bleed term).  These cases are
    # about the OVERSIZE GATE — how much of the $10 a rung's evidence entitles it to — and
    # nothing else.  The fill-bleed viability screen added the same night (alloc's "THE
    # FILL-BLEED TERM" header) refuses a 10c rung at these turnover rates on EV before the
    # sizing question is ever reached, which would leave these tests asserting a different
    # screen's verdict.  62.5c sits in the g = 0 band, so the bleed is exactly zero here and
    # W, T, the $5 lot container and the $10 envelope are all unchanged.  The bleed screen's
    # own cases live in TestTheFillBleedTerm.
    def _big_need(self, **kw):
        # q_raw = 9.6 @ 62.5c ⇒ W = $6 > the $5 lot container (q_rest = 10, rest = $6.25)
        kw.setdefault("rho", 1.25); kw.setdefault("S", 134.4); kw.setdefault("p", 0.625)
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
        # The incident's rung, affordable so the SIZING is what is on trial: W = $1.25 at
        # 62.5c (q_rest = 2), T = 7.18 turnovers ⇒ total $8.98, inside the $10 allocation.
        # (Priced at 62.5c for the reason given on `_big_need` above: g = 0 there, so this
        # test asks the oversize gate its question and only its question.)
        s = slot(rho=5.0 / 6.0, S=18.0, p=0.625, phi=phi, phi_source="bucket",
                 phi_prior=prior, phi_k=k, phi_exposure_h=expo)
        n = alloc.law_need(s)
        self.assertEqual(n.q_rest, 2)
        self.assertAlmostEqual(n.bleed_usd, 0.0, places=9)
        self.assertFalse(n.history_dominates)
        # and the second clause refuses independently: 3/2 x 24h = 36 turnovers cannot be
        # ruled out on two contract-hours (see `Need.evidence_bounds_a_turnover`)
        self.assertFalse(n.evidence_bounds_a_turnover)
        self.assertLessEqual(n.total_usd, 10.0)
        a, spent, _rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], n.q_rest, "the order must size to the NEED, not the $10")
        self.assertAlmostEqual(a[s.key] * s.p, 1.25, places=6)
        self.assertLessEqual(a[s.key] * s.p, C.SLOT_LOT_CAP_USD + 1e-9,
                             "THE INCIDENT: 2 quiet hours put the full envelope down")
        # The requote reserve IS what is held back: the rung rests $1.25 of the $10 it is
        # allowed, so the flow that ate the oversized seats can only reach an eighth of it.
        self.assertGreaterEqual(C.ALLOC_PER_MARKET_USD - a[s.key] * s.p, 8.75)
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


class TestTheFillBleedTerm(LipTestCase):
    """THE 2026-07-30-NIGHT INCIDENT, as tests.

    The law ranked by CAPITAL NEEDED and by nothing else, so the cheapest contract always won
    the queue, and the book walked to the toxic end of the price axis:

        resting book average price 8.2c — two 300-lot walls at 3c and a rung at 1c — against
        a held-position average of 12.3c and a design average of ~15c.

        "how the fuck did that happen. turn off v5. fix it"   — the owner, 2026-07-30

    The derivation is in `alloc.py`'s "THE FILL-BLEED TERM" header; g comes from
    `bleed.G_TABLE` (n = 8,240 settled markets), pinned in `test_bleed`.

    MUTATION CONTRACT.  Delete the bleed term from `law_rank`/`_priced` and
    `test_the_3c_walls_and_the_1c_rung_are_refused` funds all three rungs and fails on the
    first assertion; `test_at_equal_capital_need_the_15c_rung_wins` fails on the ordering.
    Corrupt g's derivation and `test_bleed.test_pinned_bucket_values` fails first.

    NOTE — NO AVERAGE-PRICE RULE EXISTS ANYWHERE.  Nothing in the allocator looks at the
    book's mean price.  `test_the_book_reprices_itself_as_a_consequence` asserts the 12-15c
    shape as an OUTPUT of the ranking, which is the only way it is allowed to be true.
    """

    # T = phi x h; h = 24 here, so phi = T/24.  T = 1.1 is "a moderate phi": the lot is
    # expected to turn over just past once inside the horizon — above the oversize gate's
    # T <= 1 so the order is W exactly, and low enough that the CAPITAL screen (W x max(1,T)
    # <= $10) passes for every rung below.  Every refusal in these tests is therefore the
    # BLEED screen and nothing else, which is what makes them mutation-decisive.
    T = 1.1
    PHI = 1.1 / 24.0

    def _rung(self, ticker, p, w_usd, **kw):
        """A rung at price `p` whose share-math need is exactly `w_usd` of resting collateral.
        s = target/((rho/2)h) = 1.00/10 = 0.10 at the default rho, so q_raw = S/9."""
        q_raw = w_usd / p
        kw.setdefault("phi", self.PHI)
        return slot(ticker, S=9.0 * q_raw, p=p, **kw)

    def test_the_3c_walls_and_the_1c_rung_are_refused(self):
        """TONIGHT'S BOOK, AT TONIGHT'S PARAMETERS.  A 300-lot wall at 3c is $9.00 of resting
        collateral; the 1c rung, same lot shape, is $3.00.

        Both were FUNDABLE under the old law and that is the bug: their capital arithmetic
        ($9.90 and $3.30 committed at T = 1.1) sits inside the $10 allocation, and the 1c rung
        was the CHEAPEST thing in the queue, so it ranked first.  Both are now refused on EV:

            3c wall: bleed = $9.00 x 1.1 x 0.6669 = $6.60  against $1.00 of credit
            1c rung: bleed = $3.00 x 1.1 x 0.9484 = $3.13  against $1.00 of credit
        """
        wall = self._rung("KXWALL-3C", 0.03, 9.00)
        rung = self._rung("KXRUNG-1C", 0.01, 3.00)
        nw, nr = alloc.law_need(wall), alloc.law_need(rung)
        # (i) the capital screen does NOT refuse them — this is what made the incident possible
        self.assertAlmostEqual(nw.total_usd, 9.90, places=6)
        self.assertAlmostEqual(nr.total_usd, 3.30, places=6)
        self.assertLess(nw.total_usd, C.ALLOC_PER_MARKET_USD)
        self.assertLess(nr.total_usd, C.ALLOC_PER_MARKET_USD)
        # (ii) the bleed screen does
        self.assertAlmostEqual(nw.g, 0.6669, places=4)
        self.assertAlmostEqual(nr.g, 0.9484, places=4)
        self.assertAlmostEqual(nw.bleed_usd, 9.00 * 1.1 * 0.6669, places=6)
        self.assertAlmostEqual(nr.bleed_usd, 3.00 * 1.1 * 0.9484, places=6)
        self.assertGreater(nw.bleed_usd, nw.target_usd)
        self.assertGreater(nr.bleed_usd, nr.target_usd)
        self.assertEqual(nw.reason, alloc.BLEED)
        self.assertEqual(nr.reason, alloc.BLEED)
        a, spent, rep = alloc.allocate_law([wall, rung], budget_usd=300.0)
        self.assertEqual(a[wall.key], 0)
        self.assertEqual(a[rung.key], 0)
        self.assertEqual(spent, 0.0)
        self.assertEqual(rep["reasons"].get(alloc.BLEED), 2)
        # (iii) no silent terms: the refusal cites g and the dollars
        ex = self.logs_of("law_example")
        self.assertTrue(ex, "a bleed refusal with no numbers is the defect, not the policy")
        for e in ex:
            self.assertIn("g", e)
            self.assertIn("bleed_usd", e)
            self.assertIn("effective_usd", e)
            self.assertEqual(e["reason"], alloc.BLEED)

    def test_at_equal_capital_need_the_15c_rung_wins(self):
        """THE RANKING PROPERTY, isolated.  Three rungs at 1c, 3c and 15c, each needing the
        SAME $2.50 of resting collateral and the same T — identical capital need ($2.75), so
        under the old law they were a three-way tie broken by ticker and all three funded.

        With the bleed charged, the capital need is the same but the TRUE need is not:

            1c : 2.75 + 2.50 x 1.1 x 0.9484 = $5.36   refused (bleed $2.61 > $1.00 credit)
            3c : 2.75 + 2.50 x 1.1 x 0.6669 = $4.58   refused (bleed $1.83 > $1.00 credit)
            15c: 2.75 + 2.50 x 1.1 x 0.3508 = $3.71   FUNDED  (bleed $0.96 < $1.00 credit)

        Mutation: drop `bleed_usd` from `effective_usd` and the ordering assertion fails on a
        tie; drop the viability screen and all three fund."""
        one = self._rung("KXA-1C", 0.01, 2.50)
        three = self._rung("KXB-3C", 0.03, 2.50)
        fifteen = self._rung("KXC-15C", 0.15, 2.50)
        needs = [alloc.law_need(x) for x in (one, three, fifteen)]
        for n in needs:                                    # identical CAPITAL need
            self.assertAlmostEqual(n.total_usd, 2.75, places=6)
        ranked = alloc.law_rank(needs)
        self.assertEqual([n.slot.ticker for n in ranked], ["KXC-15C", "KXB-3C", "KXA-1C"])
        self.assertAlmostEqual(ranked[0].effective_usd, 2.75 + 2.50 * 1.1 * 0.3508, places=6)
        a, spent, rep = alloc.allocate_law([one, three, fifteen], budget_usd=300.0)
        self.assertEqual(a[one.key], 0)
        self.assertEqual(a[three.key], 0)
        self.assertGreater(a[fifteen.key], 0)
        self.assertAlmostEqual(a[fifteen.key] * 0.15, 2.55, places=6)   # W, ceil to 17 lots
        self.assertEqual(rep["reasons"].get(alloc.BLEED), 2)
        f = self.logs_of("law_funded")
        self.assertEqual(len(f), 1)
        self.assertAlmostEqual(f[0]["g"], 0.3508, places=4)
        self.assertAlmostEqual(f[0]["bleed_usd"], 2.50 * 1.1 * 0.3508, places=4)
        self.assertIn("order_bleed_usd", f[0])

    def test_the_book_reprices_itself_as_a_consequence(self):
        """THE OWNER'S ACCEPTANCE SHAPE, and the reason it may not be a rule.  Offer the
        allocator the whole cheap axis at once — 1c through 40c, one cluster each, every one
        of them affordable and every one of them needing the same capital.  Nothing here reads
        an average price; the allocator simply refuses the rungs whose fills lose more than
        the pool pays and ranks the rest by true cost.  The book's average price is whatever
        falls out of that, and what falls out is >= 12c."""
        rungs = [self._rung("KX%02dC" % c, c / 100.0, 2.50) for c in
                 (1, 2, 3, 4, 6, 9, 12, 15, 20, 30, 40)]
        a, _spent, _rep = alloc.allocate_law(rungs, budget_usd=300.0)
        funded = [(s, a[s.key]) for s in rungs if a[s.key] > 0]
        self.assertTrue(funded)
        for s, _q in funded:
            self.assertGreaterEqual(s.p, 0.12, "a sub-12c rung funded: %s" % s.ticker)
        usd = sum(q * s.p for s, q in funded)
        avg_c = 100.0 * usd / sum(q for _s, q in funded)
        self.assertGreaterEqual(avg_c, 12.0)

    def test_low_phi_stays_cheap_to_oversize(self):
        """THE TERM SELF-LIMITS WHERE FILLS ARE RARE (header, "WHY T AND NOT max(1, T)").  A
        MEASURED phi of zero means T = 0, so the bleed is zero at ANY size — the oversize path
        (law_order_q rule 3) still puts the whole $10 on the rung, exactly as owner example 3
        requires.  Charging the bleed with max(1, T) instead of T would refuse this rung for a
        loss it will never take."""
        s = self._rung("KXQUIET", 0.03, 1.00, phi=0.0)        # measured phi = 0 ⇒ T = 0
        n = alloc.law_need(s)
        self.assertAlmostEqual(n.turnovers, 0.0, places=12)
        self.assertAlmostEqual(n.g, 0.6669, places=4)         # g is still the 3c value...
        self.assertAlmostEqual(n.bleed_usd, 0.0, places=12)   # ...and the charge is still zero
        self.assertAlmostEqual(n.effective_usd, n.total_usd, places=12)
        a, spent, _rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], 333)                       # $9.99 — the whole envelope
        self.assertAlmostEqual(spent, 9.99, places=6)

    def test_the_oversize_order_is_charged_at_the_ENVELOPE_not_at_W(self):
        """THE OVERSIZE PATH MULTIPLIES EXPOSURE, so it must be screened at the size actually
        posted.  This rung needs only $1.00 of resting collateral at 3c and would pass the
        W-level screen ($1.00 x 1.0 x 0.6669 = $0.67 < $1.00 of credit).  But its T is exactly
        1.0, which arms law_order_q's oversize, and the order that would rest is the FULL $10
        envelope — 333 contracts whose expected bleed is $6.66.  Refused.

        Mutation: screen the order at W instead of at `q x unit` and this test funds a $10
        wall at 3c — the incident, re-entering through the oversize door."""
        s = self._rung("KXOVER-3C", 0.03, 1.00, phi=1.0 / 24.0)      # T = 1.0 exactly
        n = alloc.law_need(s)
        self.assertAlmostEqual(n.turnovers, 1.0, places=9)
        self.assertAlmostEqual(n.bleed_usd, 1.00 * 1.0 * 0.6669, places=6)
        self.assertEqual(n.reason, "")                        # W-level screen PASSES
        self.assertEqual(alloc.law_order_q(n, 10.0), 333)     # ...and the order is the $10
        a, spent, rep = alloc.allocate_law([s], budget_usd=300.0)
        self.assertEqual(a[s.key], 0)
        self.assertEqual(spent, 0.0)
        self.assertEqual(rep["reasons"].get(alloc.BLEED), 1)
        ex = [e for e in self.logs_of("law_example") if e["reason"] == alloc.BLEED]
        self.assertAlmostEqual(ex[0]["bleed_usd"], (333 - 1) * 0.03 * 1.0 * 0.6669, places=4)

    def test_a_no_rung_is_charged_its_own_side_of_the_book(self):
        """`unit_usd` is already side-corrected, so an ASK rung resting at 97c-YES collateral
        is charged the 97c bucket's g (zero), not the 3c bucket's.  The mirror case — a NO
        position that IS cheap — is the 3c wall above.  Getting this backwards would charge
        the expensive half of the book for the cheap half's toxicity."""
        s = self._rung("KXRICH", 0.97, 2.50, side="ask")
        n = alloc.law_need(s)
        self.assertAlmostEqual(n.unit_usd, 0.97, places=9)
        self.assertAlmostEqual(n.g, 0.0, places=6)
        self.assertAlmostEqual(n.bleed_usd, 0.0, places=9)
        self.assertAlmostEqual(n.effective_usd, n.total_usd, places=9)


class TestThereIsGenuinelyOneFormula(LipTestCase):
    """REVIEWER SEND-BACK, 2026-07-30 night.  The fill-bleed term went into `law_rank` and
    THREE other consumers were left ranking on `total_usd`, each under a docstring promising
    "the ONE formula":

        scan.law_poll_key       — the 1 Hz book-poll clamp: which markets we watch at all
        runner's degrade ladder — which breadth gets SHED first under rate pressure
        engine.shadow_readout   — the venue_rank board a human reads before arming

    That is the incident surviving in the plumbing.  On the reviewer's own fixture — two
    VIABLE rungs of equal capital need, 3c and 15c at T = 0.5 — the allocator funds 15c first
    while the poll clamp polled 3c first: we would have funded the expensive rung and spent
    our poll budget, and our shed order, defending the cheap one.

    The ranking is now ONE callable, `alloc.law_sort_key`, and its one inversion
    `alloc.law_shed_score`.  Nothing re-spells it.

    MUTATION: revert any single consumer to `total_usd` and
    `test_the_poll_clamp_orders_exactly_as_the_allocator_funds` (scan),
    `test_the_degrade_ladder_sheds_what_the_allocator_funds_last` (runner) or
    `TestShadowReadout.test_venue_rank_orders_by_the_EFFECTIVE_need` in test_engine.py
    (engine) fails; `test_no_consumer_re_spells_the_key` fails for any of the three even when
    the reverted ordering happens to tie.
    """

    # THE REVIEWER'S FIXTURE: two rungs of comparable capital need at 3c and 15c, T = 0.5
    # (<= 1, so max(1, T) = 1 and capital need IS W), both VIABLE — 3c bleeds $0.80 and 15c
    # $0.44 against $1.00 of credit, so neither is refused and this is a pure ordering
    # question, not a screening one.
    #
    # THE 3c RUNG IS GIVEN THE CHEAPER CAPITAL ($2.40 against $2.50) ON PURPOSE.  The
    # reviewer's fixture was equal-capital and relied on the (ticker, side) tie-break to
    # expose the stale ordering — which works, but makes the whole mutation check hang on
    # two floats comparing exactly equal (they are `750/49 x 0.03` and `150/49 x 0.15`: equal
    # in algebra, not necessarily in IEEE 754).  A 10c capital gap makes the stale key prefer
    # the 3c rung UNCONDITIONALLY and for the honest reason — it really is the cheaper
    # capital — so the test states the incident rather than a rounding coincidence.
    T = 0.5
    W_3C, W_15C = 2.40, 2.50

    def _pair(self):
        return (slot("KXA-3C", S=9.0 * (self.W_3C / 0.03), p=0.03, phi=self.T / 24.0),
                slot("KXB-15C", S=9.0 * (self.W_15C / 0.15), p=0.15, phi=self.T / 24.0))

    def test_the_fixture_is_an_ordering_question_not_a_screening_one(self):
        """Both rungs are live candidates, and the CHEAPER capital is the 3c one — so the
        allocator preferring 15c is the bleed term overriding capital, which is the whole
        fix."""
        three, fifteen = self._pair()
        n3, n15 = alloc.law_need(three), alloc.law_need(fifteen)
        for n in (n3, n15):
            self.assertEqual(n.reason, "")                       # neither is refused
        self.assertAlmostEqual(n3.total_usd, 2.40, places=6)     # the CHEAPER capital...
        self.assertAlmostEqual(n15.total_usd, 2.50, places=6)
        self.assertLess(n3.total_usd, n15.total_usd)
        self.assertAlmostEqual(n3.bleed_usd, 2.40 * 0.5 * 0.6669, places=6)
        self.assertAlmostEqual(n15.bleed_usd, 2.50 * 0.5 * 0.3508, places=6)
        self.assertGreater(n3.effective_usd, n15.effective_usd)  # ...and the DEARER rung
        # THE STALE KEY, spelled out, so the divergence is on the record: on capital alone
        # these tie and the tie-break puts the 3c rung first.
        stale = sorted([n3, n15], key=lambda n: (n.reason != "", n.total_usd,
                                                 n.slot.ticker, n.slot.side))
        self.assertEqual([n.slot.ticker for n in stale], ["KXA-3C", "KXB-15C"])
        self.assertEqual([n.slot.ticker for n in alloc.law_rank([n3, n15])],
                         ["KXB-15C", "KXA-3C"])

    def test_the_poll_clamp_orders_exactly_as_the_allocator_funds(self):
        """`scan.law_poll_key` and the funding order are the SAME order.  Asserted against
        the allocator's actual `law_funded` sequence, not against a re-derivation of it — the
        envelope is trimmed to $2.60 so the oversize path does not change the subject."""
        three, fifteen = self._pair()
        polled = [s.ticker for s in sorted([three, fifteen], key=scan.law_poll_key)]
        self.assertEqual(polled, ["KXB-15C", "KXA-3C"])
        a, _spent, _rep = alloc.allocate_law([three, fifteen], budget_usd=300.0,
                                             alloc_cap_usd=2.60)
        self.assertGreater(a[three.key], 0)
        self.assertGreater(a[fifteen.key], 0)
        funded_order = [f["ticker"] for f in self.logs_of("law_funded")]
        self.assertEqual(funded_order, ["KXB-15C", "KXA-3C"])
        self.assertEqual(polled, funded_order,
                         "the poll clamp must watch the book the allocator funds")

    def test_the_degrade_ladder_sheds_what_the_allocator_funds_last(self):
        """`alloc.law_shed_score` is `law_sort_key` inverted, so the ladder's shed order is
        the funding order reversed.  On `total_usd` this ladder defended the 3c rung and shed
        the 15c one — exactly inverted."""
        three, fifteen = self._pair()
        scored = sorted([(alloc.law_shed_score(alloc.law_need(s)), s.ticker)
                         for s in (three, fifteen)])
        self.assertEqual([t for _sc, t in scored], ["KXA-3C", "KXB-15C"])   # shed 3c first
        self.assertGreater(alloc.law_shed_score(alloc.law_need(fifteen)),
                           alloc.law_shed_score(alloc.law_need(three)))
        # a refused rung is shed before any live one, whatever its capital
        dead = slot("KXC-2C", S=9.0 * (2.50 / 0.02), p=0.02, phi=1.0)
        self.assertEqual(alloc.law_need(dead).reason, alloc.BLEED)
        self.assertEqual(alloc.law_shed_score(alloc.law_need(dead)), float("-inf"))

    @staticmethod
    def _code_only(fn):
        """A function's EXECUTABLE source, with docstrings and comments removed.

        The three consumer sites all NAME `alloc.law_sort_key` in their prose — that is the
        point of the prose — so a raw substring search over `inspect.getsource` would pass on
        a consumer whose BODY had been reverted underneath its own comment, which is exactly
        the failure mode this test exists to catch.  Round-tripping through `ast` drops
        comments (they are not in the tree) and the docstring node explicitly, so what is
        left is what actually runs."""
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        body = tree.body[0].body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]                        # the docstring
        return "\n".join(ast.unparse(node) for node in body)

    def test_no_consumer_re_spells_the_key(self):
        """THE STRUCTURAL GUARD, and the reason this class exists.  A consumer that re-spells
        the ordering is one that can go stale silently — that is the entire send-back.  Every
        site that claims the law's order must CALL `alloc.law_sort_key` (or its inversion
        `law_shed_score`), so reverting one to `total_usd` fails here even in a world where
        the two orderings happen to tie and the functional tests above stay green."""
        from .. import engine as E, runner as RUN
        for label, fn in (("scan.law_poll_key", scan.law_poll_key),
                          ("runner.book_poll_pass", RUN.Runner.book_poll_pass),
                          ("engine.shadow_readout", E.Maker.shadow_readout)):
            body = self._code_only(fn)
            self.assertTrue("law_sort_key" in body or "law_shed_score" in body,
                            "%s does not call the law's one ordering key" % label)
            # `total_usd` may still be READ in code (the $10 rail's own question is capital),
            # but it may never be a sort key, a rank score, or a tuple handed to a sort.
            for line in body.splitlines():
                if "total_usd" in line:
                    self.assertNotIn("sort", line, "%s sorts on total_usd" % label)
                    self.assertNotIn("score", line, "%s scores on total_usd" % label)
                    self.assertNotIn("append", line,
                                     "%s builds its sort key from total_usd" % label)
