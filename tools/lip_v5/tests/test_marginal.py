"""THE MARGINAL QUEUE (v6 core) — mutation-checked, clause by clause.

Every test here names the clause of `marginal.py` it pins; revert that clause and the named
test fails on the exact symptom, not on a rounding difference.  The reference queue in
`TestTheBlockJumpIsTheGreedy` is a BRUTE-FORCE per-contract implementation of note 55's
sentence ("keep allocating each next dollar wherever marginal expected net credit is
highest") — the block-jump in the shipped code is pinned equal to it, so the optimisation
cannot silently become an approximation.
"""

import heapq

from .. import config as C, marginal as MQ, smooth as SM
from .. import alloc
from .base import LipTestCase


def slot(ticker="KXMQ-26JUL31-T1", side="bid", rho=1.0, S=100.0, p=0.20, phi=0.0,
         hours_left=24.0, accrued=0.0, target_size=1000, cum_size=2000.0, **kw):
    """A candidate whose side already qualifies on rival depth, unless a test says otherwise.
    p = 20c sits in the g = 0.3508 band; phi = 0 makes T = 0 so the bleed term is inert
    unless a test switches it on deliberately."""
    return alloc.Slot(ticker, side, rho=rho, S=S, p=p, phi=phi, hours_left=hours_left,
                      accrued=accrued, target_size=target_size, cum_size=cum_size,
                      land_grab_price_c=C.ENTRY_BAND_LO_C, **kw)


def brute_force_queue(slots, budget_usd, per_market_cap_usd=None, cluster_cap_usd=None):
    """THE LITERAL READING OF NOTE 55, one contract at a time.  No blocks, no bisection.
    Used only as a reference: the shipped queue must reproduce it exactly."""
    curves = {}
    for s in slots:
        cv = MQ.Curve(s)
        if cv.reason or cv.entry_rate() <= 0:
            continue
        curves[s.key] = cv
    q = {k: 0 for k in curves}
    spent = 0.0
    by_market, by_cluster, cluster_market = {}, {}, {}
    while True:
        best, best_rate = None, 0.0
        for k, cv in sorted(curves.items()):
            if q[k] == 0:
                rate, dq = cv.entry_rate(), cv.q_entry
            else:
                rate, dq = cv.marginal_rate(q[k]), 1
            if rate <= 0:
                continue
            charge = cv.capital(q[k] + dq) - cv.capital(q[k])
            room = budget_usd - spent
            if per_market_cap_usd is not None:
                room = min(room, per_market_cap_usd - by_market.get(cv.slot.ticker, 0.0))
            if cluster_cap_usd is not None:
                room = min(room, cluster_cap_usd - by_cluster.get(cv.cluster, 0.0))
            if charge > room + 1e-9:
                continue
            if q[k] == 0 and cv.cluster in cluster_market \
                    and cv.slot.ticker not in cluster_market[cv.cluster]:
                continue
            if rate > best_rate + 1e-15:
                best, best_rate = (k, dq, charge), rate
        if best is None:
            break
        k, dq, charge = best
        cv = curves[k]
        q[k] += dq
        spent += charge
        by_market[cv.slot.ticker] = by_market.get(cv.slot.ticker, 0.0) + charge
        by_cluster[cv.cluster] = by_cluster.get(cv.cluster, 0.0) + charge
        cluster_market.setdefault(cv.cluster, set()).add(cv.slot.ticker)
    return q


class TestTheBlockJumpIsTheGreedy(LipTestCase):
    """`marginal._block_to_rate` — "an implementation of the greedy, not an approximation"."""

    def test_block_queue_reproduces_the_contract_at_a_time_queue(self):
        slots = [slot("KXAA-26JUL31-T1", rho=1.0, S=100.0, p=0.20),
                 slot("KXBB-26JUL31-T1", rho=2.0, S=400.0, p=0.30),
                 slot("KXCC-26JUL31-T1", rho=0.5, S=40.0, p=0.10),
                 slot("KXDD-26JUL31-T1", rho=3.0, S=900.0, p=0.45)]
        a, spent, rep = MQ.allocate_marginal(slots, budget_usd=120.0)
        ref = brute_force_queue(slots, budget_usd=120.0)
        for s in slots:
            self.assertEqual(a[s.key], ref.get(s.key, 0),
                             "%s: block queue %s vs per-contract queue %s"
                             % (s.ticker, a[s.key], ref.get(s.key, 0)))
        self.assertGreater(spent, 0.0)

    def test_it_also_matches_under_the_per_market_and_cluster_rails(self):
        slots = [slot("KXAA-26JUL31-T1", rho=1.0, S=100.0, p=0.20),
                 slot("KXBB-26JUL31-T1", rho=2.0, S=400.0, p=0.30),
                 slot("KXCC-26JUL31-T1", rho=0.5, S=40.0, p=0.10)]
        a, _s, _r = MQ.allocate_marginal(slots, budget_usd=600.0, per_market_cap_usd=21.0,
                                         cluster_cap_usd=21.0)
        ref = brute_force_queue(slots, 600.0, per_market_cap_usd=21.0, cluster_cap_usd=21.0)
        for s in slots:
            self.assertEqual(a[s.key], ref.get(s.key, 0), s.ticker)


class TestTheKneeIsEmergent(LipTestCase):
    """note 55 §1: "Most clusters stop at their knee because 'the other $40 earns more
    elsewhere'" — and note 55 item 1: the knee is EMERGENT, never a constant."""

    def test_a_lone_market_deepens_until_its_own_marginal_rate_dies(self):
        """With nowhere else to go, the queue keeps deepening one market until its OWN
        marginal rate reaches zero or the rail stops it — no seat, no constant."""
        s = slot("KXSOLO-26JUL31-T1", rho=4.0, S=500.0, p=0.20, phi=0.02)
        a, spent, rep = MQ.allocate_marginal([s], budget_usd=600.0, per_market_cap_usd=21.0)
        self.assertGreater(a[s.key], 0)
        self.assertAlmostEqual(spent, 21.0, delta=0.25,
                               msg="a lone market with a live marginal rate should fill its "
                                   "rail, not a $10 seat: %s" % spent)

    def test_the_same_market_stops_far_short_when_better_entries_compete(self):
        """THE KNEE.  Identical market, but now nine fresh markets are on the board: the
        deepening dollars lose to their entry blocks and the market stops well inside its
        rail.  Revert the queue to v5's one-seat law and this asymmetry disappears."""
        rich = slot("KXSOLO-26JUL31-T1", rho=4.0, S=500.0, p=0.20, phi=0.02)
        alone, _s0, _r0 = MQ.allocate_marginal([rich], budget_usd=600.0,
                                               per_market_cap_usd=21.0)
        board = [rich] + [slot("KXO%02d-26JUL31-T1" % i, rho=3.0, S=400.0, p=0.20, phi=0.02)
                          for i in range(9)]
        with_peers, spent, rep = MQ.allocate_marginal(board, budget_usd=60.0,
                                                      per_market_cap_usd=21.0)
        self.assertLess(with_peers[rich.key], alone[rich.key],
                        "the knee: competing entries must pull dollars off the deepening "
                        "market (%s vs %s)" % (with_peers[rich.key], alone[rich.key]))
        self.assertGreaterEqual(sum(1 for k, q in with_peers.items() if q > 0), 3,
                                "capital should have spread across entries: %s" % with_peers)

    def test_the_equalising_rate_is_reported(self):
        board = [slot("KXO%02d-26JUL31-T1" % i, rho=2.0, S=300.0, p=0.20) for i in range(6)]
        _a, _s, rep = MQ.allocate_marginal(board, budget_usd=40.0, per_market_cap_usd=21.0)
        self.assertGreater(rep["lam"], 0.0)
        self.assertTrue(self.logs_of("mq_reasons"), "the pass must log its lambda")


class TestTheCliffAndTheRescueValue(LipTestCase):
    """note 55 item 3: "the cliff makes PRE-floor dollars special (they unlock stranded
    accrual)" — and it must be EMERGENT, not a rescue rule."""

    def test_a_sub_cliff_market_outranks_its_identical_fresh_twin(self):
        stranded = slot("KXSTRAND-26JUL31-T1", rho=1.0, S=100.0, p=0.20, accrued=0.70)
        fresh = slot("KXFRESH-26JUL31-T1", rho=1.0, S=100.0, p=0.20, accrued=0.0)
        cs, cf = MQ.Curve(stranded), MQ.Curve(fresh)
        self.assertGreater(cs.entry_rate(), cf.entry_rate(),
                           "$0.70 of conditional accrual must make the next $0.30 of credit "
                           "worth a whole dollar: %.4f vs %.4f"
                           % (cs.entry_rate(), cf.entry_rate()))
        # and with only enough capital for one of them, the queue rescues.
        a, _s, _r = MQ.allocate_marginal([stranded, fresh], budget_usd=cs.capital(cs.q_entry))
        self.assertGreater(a[stranded.key], 0)
        self.assertEqual(a[fresh.key], 0)

    def test_credit_below_the_cliff_is_paid_ZERO(self):
        """The forfeit itself: a size whose total accrual cannot reach $1.00 is worth nothing,
        which is why the entry is a LUMP and not a sequence of contracts."""
        cv = MQ.Curve(slot(rho=1.0, S=100.0, p=0.20))
        self.assertEqual(cv.paid(1), 0.0)
        self.assertGreaterEqual(cv.paid(cv.q_entry), C.CREDIT_TARGET_USD - 1e-9)

    def test_there_is_no_DONE_rule(self):
        """note 55 item 3: "No DONE rule in v6.  Banked credit is sunk."  v5's `law_need`
        returns DONE for accrued >= target; the queue must still fund it on plain rate."""
        done = slot("KXDONE-26JUL31-T1", rho=1.0, S=100.0, p=0.20, accrued=2.50)
        self.assertEqual(alloc.law_need(done).reason, alloc.DONE)
        a, _s, _r = MQ.allocate_marginal([done], budget_usd=60.0, per_market_cap_usd=21.0)
        self.assertGreater(a[done.key], 0,
                           "a market past the cliff is not done — its remaining credit "
                           "competes for the next dollar like everything else")


class TestTheSwitchToll(LipTestCase):
    """note 55 item 4a — the toll is stranded accrual + TRANSIT PRESENCE LOSS, and it is
    priced in hours of presence, not in a hysteresis constant."""

    def test_transit_is_the_structural_min_resting_life(self):
        self.assertAlmostEqual(MQ.transit_h(), C.MIN_RESTING_LIFE_S / 3600.0, places=12)

    def test_a_market_we_are_present_in_earns_over_the_full_horizon(self):
        here = MQ.Curve(slot("KXHERE-26JUL31-T1", accrued=0.01))
        there = MQ.Curve(slot("KXTHERE-26JUL31-T1", accrued=0.0))
        self.assertAlmostEqual(here.h_eff, here.h, places=12)
        self.assertAlmostEqual(there.h_eff, there.h - MQ.transit_h(), places=12)
        self.assertGreater(here.h_eff, there.h_eff)

    def test_a_tiny_edge_cannot_pay_the_toll_but_a_real_one_pays_instantly(self):
        """Small differences can't pay the toll; big real ones pay instantly (note 55)."""
        # Incumbent: present (accrued > 0).  Challenger: identical pool + epsilon.
        inc = slot("KXINC-26JUL31-T1", rho=1.0, S=100.0, p=0.20, accrued=0.0001)
        tiny = slot("KXTINY-26JUL31-T1", rho=1.0001, S=100.0, p=0.20)
        big = slot("KXBIG-26JUL31-T1", rho=4.0, S=100.0, p=0.20)
        self.assertGreater(MQ.Curve(inc).entry_rate(), MQ.Curve(tiny).entry_rate(),
                           "a 0.01% better pool must not out-rank a seat we already hold")
        self.assertGreater(MQ.Curve(big).entry_rate(), MQ.Curve(inc).entry_rate(),
                           "a 4x pool must pay the toll instantly")

    def test_the_toll_shrinks_to_nothing_on_a_long_horizon_and_bites_on_a_short_one(self):
        long_h = MQ.Curve(slot("KXL-26JUL31-T1", hours_left=24.0))
        short_h = MQ.Curve(slot("KXS-26JUL31-T1", hours_left=0.1))
        self.assertLess(1.0 - long_h.h_eff / long_h.h, 0.001)
        self.assertGreater(1.0 - short_h.h_eff / short_h.h, 0.05)

    def test_presence_is_read_from_accrual_not_from_our_orders(self):
        """THE SPINE.  The toll must be a function of WORLD state; `Curve` may not read an
        order book of ours at all.  Cancelling every order changes no input here."""
        self.assertTrue(MQ.Curve(slot(accrued=0.5)).present)
        self.assertFalse(MQ.Curve(slot(accrued=0.0)).present)


class TestTheRailsAndTheClusterLaw(LipTestCase):
    def test_one_market_per_cluster_still_binds(self):
        a = slot("KXTWIN-26JUL31-T1", rho=2.0, S=100.0, p=0.20)
        b = slot("KXTWIN-26JUL31-T2", rho=2.0, S=100.0, p=0.20)
        al, _s, rep = MQ.allocate_marginal([a, b], budget_usd=600.0, per_market_cap_usd=21.0,
                                           cluster_cap_usd=21.0)
        self.assertTrue((al[a.key] > 0) != (al[b.key] > 0), "exactly one market per cluster")
        self.assertEqual(rep["reasons"].get(MQ.CLUSTER_TAKEN), 1)

    def test_both_sides_of_the_entered_market_are_still_legal(self):
        bid = slot("KXTWO-26JUL31-T1", side="bid", rho=2.0, S=100.0, p=0.20)
        ask = slot("KXTWO-26JUL31-T1", side="ask", rho=2.0, S=100.0, p=0.20)
        al, _s, _r = MQ.allocate_marginal([bid, ask], budget_usd=600.0,
                                          per_market_cap_usd=21.0, cluster_cap_usd=21.0)
        self.assertGreater(al[bid.key], 0)
        self.assertGreater(al[ask.key], 0)

    def test_the_cluster_dollar_rail_bounds_the_whole_cluster(self):
        bid = slot("KXTWO-26JUL31-T1", side="bid", rho=4.0, S=500.0, p=0.20, phi=0.02)
        ask = slot("KXTWO-26JUL31-T1", side="ask", rho=4.0, S=500.0, p=0.20, phi=0.02)
        _al, spent, _r = MQ.allocate_marginal([bid, ask], budget_usd=600.0,
                                              per_market_cap_usd=21.0, cluster_cap_usd=21.0)
        self.assertLessEqual(spent, 21.0 + 1e-6)

    def test_a_negative_entry_is_refused_with_its_numbers(self):
        """The fill-bleed viability screen, generalised: a 1c rung with real turnover cannot
        pay for its own fills (g = 0.9484) and is refused at any rank."""
        toxic = slot("KXTOX-26JUL31-T1", rho=0.4, S=50.0, p=0.01, phi=1.0)
        al, spent, rep = MQ.allocate_marginal([toxic], budget_usd=600.0)
        self.assertEqual(al[toxic.key], 0)
        self.assertEqual(spent, 0.0)
        self.assertEqual(rep["reasons"].get(MQ.NEGATIVE_ENTRY), 1)
        ex = self.logs_of("mq_example")
        self.assertTrue(ex and ex[0]["g"] == 0.9484 and ex[0]["bleed_usd"] > 0,
                        "the refusal must carry g and the bleed: %s" % ex)

    def test_an_unreachable_cliff_is_refused_with_its_numbers(self):
        starved = slot("KXSTARVE-26JUL31-T1", rho=0.01, S=100.0, p=0.20, hours_left=0.5)
        al, _s, rep = MQ.allocate_marginal([starved], budget_usd=600.0)
        self.assertEqual(al[starved.key], 0)
        self.assertEqual(rep["reasons"].get(MQ.UNREACHABLE_CLIFF), 1)


class TestDeterminism(LipTestCase):
    def test_the_same_world_gives_the_same_book_in_any_input_order(self):
        board = [slot("KX%02d-26JUL31-T1" % i, rho=1.0 + 0.1 * i, S=100.0 + i, p=0.20)
                 for i in range(8)]
        a1, s1, _r = MQ.allocate_marginal(board, budget_usd=80.0, per_market_cap_usd=21.0)
        a2, s2, _r = MQ.allocate_marginal(list(reversed(board)), budget_usd=80.0,
                                          per_market_cap_usd=21.0)
        self.assertEqual(a1, a2)
        self.assertAlmostEqual(s1, s2, places=9)


class TestSmoothedCompetition(LipTestCase):
    """note 55 item 4b — rank on SMOOTHED S, window DERIVED from the recorder's flicker."""

    def test_the_fallback_window_is_twice_the_min_resting_life(self):
        self.assertAlmostEqual(SM.fallback_window_s(), 2.0 * C.MIN_RESTING_LIFE_S, places=12)

    def test_an_absent_recorder_falls_back_LOUDLY(self):
        w, src = SM.derive_window_s(dir_path=self.path("no-such-dir"))
        self.assertEqual(src, "fallback")
        self.assertAlmostEqual(w, SM.fallback_window_s(), places=12)
        rec = self.logs_of("smoothing_window")
        self.assertTrue(rec and rec[0]["source"] == "fallback" and rec[0]["reason"],
                        "a fallback must name itself and say why: %s" % rec)

    def test_the_median_revert_time_is_measured_off_the_tape(self):
        # one book: 100 -> 300 (reverts after 4s) -> 100; 100 -> 300 (reverts after 10s)
        ev = [(0.0, "a", 100.0), (1.0, "a", 300.0), (5.0, "a", 100.0),      # revert: 4s
              (0.0, "b", 100.0), (1.0, "b", 300.0), (11.0, "b", 100.0)]     # revert: 10s
        self.assertAlmostEqual(SM.median_revert_s(ev), 7.0, places=9)
        self.assertIsNone(SM.median_revert_s([(0.0, "k", 1.0), (1.0, "k", 2.0)]))

    def test_a_derived_window_is_floored_at_the_anti_alias_bound(self):
        import gzip, json, os
        d = self.path("competition")
        os.makedirs(d)
        rows = [(0.0, 100), (1.0, 300), (2.0, 100), (3.0, 300), (4.0, 100)]
        with gzip.open(os.path.join(d, "deltas-20260731.jsonl.gz"), "wt") as fh:
            for ts, sz in rows:
                fh.write(json.dumps({"ts": ts, "ticker": "KXA", "side": "yes",
                                     "price": 6, "size": sz}) + "\n")
        w, src = SM.derive_window_s(dir_path=d)
        self.assertEqual(src, "deltas")
        self.assertAlmostEqual(w, SM.fallback_window_s(), places=9,
                               msg="a 1s flicker must not buy a 2s window — the floor is "
                                   "the structural action period")

    def test_the_ewma_decays_by_elapsed_time_not_by_sample_count(self):
        sm = SM.SmoothedS(window_s=60.0)
        self.assertAlmostEqual(sm.observe(("t", "bid"), 100.0, 0.0), 100.0, places=9)
        v = sm.observe(("t", "bid"), 0.0, 60.0)
        self.assertAlmostEqual(v, 100.0 * (1.0 - (1.0 - 2.718281828459045 ** -1.0)), places=6)

    def test_smoothing_is_what_the_queue_ranks_on(self):
        """A flickering rival must not move the book: pass the smoothed score and the queue
        sizes to it, not to the snapshot."""
        # Snapshot says a 1,000-lot rival is at the touch, so the entry block costs $18.20;
        # the smoothed book says the real rival is 100 and it costs $2.00.  At a $3 rail the
        # two readings decide OPPOSITE things, which is the point of smoothing at all.
        s = slot("KXFLICK-26JUL31-T1", rho=1.0, S=1000.0, p=0.20)   # snapshot: crowded
        a_snap, _s, rep = MQ.allocate_marginal([s], budget_usd=3.0)
        a_smooth, _s2, _r2 = MQ.allocate_marginal([s], budget_usd=3.0,
                                                  s_smoothed={s.key: 100.0})
        self.assertEqual(a_snap[s.key], 0)
        self.assertEqual(rep["reasons"].get(MQ.CANT_AFFORD_ENTRY), 1)
        self.assertGreater(a_smooth[s.key], 0,
                           "S is an input to the entry cost; the smoothed value must be the "
                           "one the queue uses")


class TestTheDeepenEvidenceGate(LipTestCase):
    """G2 (adjudicator, 2026-07-31) — note 55 §2 "Oversize past W toward full A only on
    MEASURED-low phi" and the PHI SHRINKAGE section's "posterior-low AND history-dominates",
    ported onto the queue's DEPTH arm.

    THE MEASURED FAILURE this pins, reproduced by the adjudicator on this branch: a 15c rung
    with phi = 0.002 on HALF a contract-hour of its own tape was deepened from its $1.20 entry
    block to the whole rail, while the same-priced rung with real tape correctly stopped.  The
    queue could not tell earned-low phi from thin-low.
    """

    def measured(self, ticker, exposure, k=0.4, prior=0.002, **kw):
        """A rung whose posterior is 0.002 — the only difference between the two fixtures is
        HOW MUCH OF THAT NUMBER IS OURS."""
        return alloc.Slot(ticker, "bid", rho=2.0, S=200.0, p=0.15, phi=0.002,
                          phi_prior=prior, phi_k=k, phi_exposure_h=exposure,
                          hours_left=24.0, target_size=1000, cum_size=2000.0, **kw)

    def test_clause_2_the_rule_of_three_turnover_bound(self):
        """own exposure >= 3h: zero fills bound the rate at RULE_OF_THREE/exposure, so this is
        the bound that RULES OUT a turnover inside the horizon at 95%.  The adjudicator's
        fixture (0.5 contract-hours) is refused by a factor of 144."""
        thin = MQ.Curve(self.measured("KXTHIN-26JUL31-T1", exposure=0.5))
        self.assertFalse(thin.may_deepen)
        rich = MQ.Curve(self.measured("KXRICH-26JUL31-T1", exposure=3.0 * 24.0))
        self.assertTrue(rich.may_deepen)
        edge = MQ.Curve(self.measured("KXEDGE-26JUL31-T1", exposure=3.0 * 24.0 - 0.01))
        self.assertFalse(edge.may_deepen,
                         "the bound is own exposure >= 3h; one contract-hour short is short")

    def test_clause_1_history_must_DOMINATE_the_borrowed_prior(self):
        """own exposure > phi_k.  k is exactly the crossover at which our own tape and the
        prior carry equal weight, so this is "more than half of this number is ours".  It
        catches what clause 2 cannot: a rung with plenty of hours against a prior that is
        stronger still."""
        borrowed = MQ.Curve(self.measured("KXBORROW-26JUL31-T1", exposure=100.0, k=200.0))
        self.assertGreaterEqual(borrowed.slot.phi_exposure_h, 3.0 * 24.0)  # clause 2 passes
        self.assertFalse(borrowed.may_deepen, "clause 1 must still refuse it")
        ours = MQ.Curve(self.measured("KXOURS-26JUL31-T1", exposure=100.0, k=50.0))
        self.assertTrue(ours.may_deepen)

    def test_a_THIN_rung_keeps_its_entry_block_and_the_rail_goes_elsewhere(self):
        thin = self.measured("KXTHIN-26JUL31-T1", exposure=0.5)
        a, spent, rep = MQ.allocate_marginal([thin], budget_usd=600.0,
                                             per_market_cap_usd=20.0, cluster_cap_usd=20.0)
        cv = MQ.Curve(thin)
        self.assertEqual(a[thin.key], cv.q_entry)
        self.assertAlmostEqual(spent, cv.capital(cv.q_entry), places=6)
        self.assertLess(spent, 20.0 * 0.5,
                        "thin tape took most of the rail: $%.2f" % spent)
        self.assertEqual(rep["reasons"].get(MQ.UNMEASURED_DEPTH), 1,
                         "the declined depth must be COUNTED: %s" % rep["reasons"])

    def test_a_MEASURED_rung_deepens_to_the_rail(self):
        rich = self.measured("KXRICH-26JUL31-T1", exposure=3.0 * 24.0)
        a, spent, _rep = MQ.allocate_marginal([rich], budget_usd=600.0,
                                              per_market_cap_usd=20.0, cluster_cap_usd=20.0)
        self.assertGreater(a[rich.key], MQ.Curve(rich).q_entry)
        self.assertAlmostEqual(spent, 20.0, delta=0.15)

    def test_side_by_side_on_ONE_board_the_gate_is_the_only_difference(self):
        """Same price, same pool, same posterior phi — only the tape differs."""
        thin = self.measured("KXTHIN-26JUL31-T1", exposure=0.5)
        rich = self.measured("KXRICH-26JUL31-T1", exposure=3.0 * 24.0)
        a, _s, rep = MQ.allocate_marginal([thin, rich], budget_usd=600.0,
                                          per_market_cap_usd=20.0, cluster_cap_usd=20.0)
        thin_usd = MQ.Curve(thin).capital(a[thin.key])
        rich_usd = MQ.Curve(rich).capital(a[rich.key])
        self.assertGreater(rich_usd, thin_usd * 3.0,
                           "the measured rung must out-deploy the unmeasured one: "
                           "$%.2f vs $%.2f" % (rich_usd, thin_usd))
        self.assertGreaterEqual(rep["reasons"].get(MQ.UNMEASURED_DEPTH, 0), 1,
                                "the refusal must be COUNTED: %s" % rep["reasons"])

    def test_the_gate_bounds_DEPTH_and_never_PRESENCE(self):
        """Entry blocks — including a qualifying WALL — are always allowed: entering is how a
        rung's tape gets written, so a gate on entry would be self-sealing.  This is also why
        the quiet ladder-wide class is unaffected (amendment 2 relaxes market COUNT, not
        depth, and a quiet family's walls are entry blocks)."""
        wall = alloc.Slot("KXUST-26JUL31-T1", "bid", rho=0.6, S=0.0, p=0.01, phi=0.0125,
                          phi_prior=0.3, phi_k=10.0, phi_exposure_h=0.5,
                          hours_left=24.0, target_size=1000, cum_size=0.0,
                          land_grab_price_c=C.V6_PRICE_FLOOR_C)
        self.assertFalse(MQ.Curve(wall).may_deepen)
        a, spent, _rep = MQ.allocate_marginal([wall], budget_usd=600.0,
                                              per_market_cap_usd=20.0, cluster_cap_usd=20.0)
        self.assertEqual(a[wall.key], 1001, "the wall must still be funded in full")
        self.assertAlmostEqual(spent, 10.01, places=2)

    def test_an_ASSERTED_phi_is_still_treated_as_a_fact(self):
        """`phi_exposure_h is None` is the hand-built-Slot idiom for "this phi is given"; it
        must not become a refusal, or every fixture in the suite changes meaning."""
        s = alloc.Slot("KXGIVEN-26JUL31-T1", "bid", rho=2.0, S=200.0, p=0.15, phi=0.002,
                       hours_left=24.0, target_size=1000, cum_size=2000.0)
        self.assertTrue(MQ.Curve(s).may_deepen)

    def test_the_gate_is_on_the_log_line(self):
        thin = self.measured("KXTHIN-26JUL31-T1", exposure=0.5)
        MQ.allocate_marginal([thin], budget_usd=600.0, per_market_cap_usd=20.0)
        rec = self.logs_of("mq_entered")
        self.assertTrue(rec)
        self.assertIn("may_deepen", rec[0])
        self.assertFalse(rec[0]["may_deepen"])
        self.assertEqual(rec[0]["own_exposure_h"], 0.5)


class TestTheCumulativeBleedIsBoundedByConstruction(LipTestCase):
    """The adjudicator's OPTIONAL graft (a cumulative deepen-bleed cap), evaluated: it falls
    out of the queue's own invariant and needs no machinery.  Every increment the queue takes
    has positive marginal net and the entry block has positive net, so `net(q)` is positive
    and non-decreasing along the path — i.e. bleed < paid at every reachable size."""

    def rung(self, ticker, p=0.15, phi=0.01, exposure=200.0, rho=2.0, S=200.0):
        return alloc.Slot(ticker, "bid", rho=rho, S=S, p=p, phi=phi, phi_prior=phi,
                          phi_k=1.0, phi_exposure_h=exposure, hours_left=24.0,
                          target_size=1000, cum_size=2000.0)

    def test_the_cumulative_bleed_can_never_exceed_the_credit(self):
        board = [self.rung("KXC%02d-26JUL31-T1" % i, p=0.02 + 0.03 * i, phi=0.002 + 0.004 * i)
                 for i in range(8)]
        a, _s, _r = MQ.allocate_marginal(board, budget_usd=600.0, per_market_cap_usd=20.0,
                                         cluster_cap_usd=20.0)
        funded = 0
        for s in board:
            q = a[s.key]
            if q <= 0:
                continue
            funded += 1
            cv = MQ.Curve(s)
            self.assertLess(cv.bleed(q), cv.paid(q),
                            "%s: bleed $%.4f vs paid $%.4f at q=%d"
                            % (s.ticker, cv.bleed(q), cv.paid(q), q))
            self.assertGreater(cv.net(q), 0.0)
        self.assertGreaterEqual(funded, 3, "the fixture funded almost nothing")

    def test_net_is_NON_DECREASING_along_the_path_the_queue_walks(self):
        s = self.rung("KXPATH-26JUL31-T1", p=0.10, phi=0.01)
        a, _sp, _r = MQ.allocate_marginal([s], budget_usd=600.0, per_market_cap_usd=20.0)
        cv = MQ.Curve(s)
        q_final = a[s.key]
        self.assertGreater(q_final, cv.q_entry)
        prev = cv.net(cv.q_entry)
        for q in range(cv.q_entry, q_final + 1):
            self.assertGreaterEqual(cv.net(q) + 1e-12, prev,
                                    "net fell at q=%d — a negative-marginal increment was "
                                    "taken" % q)
            prev = cv.net(q)
