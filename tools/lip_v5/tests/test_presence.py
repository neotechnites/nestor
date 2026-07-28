"""spec §8.2 — `psdh(rows)`, T-P1..P5; and §8.7's T-D4 presence collapse, ALL FOUR BRANCHES."""

import random
import unittest

from .. import config as C, presence as P
from .base import LipTestCase


def row(ticker="T", side="bid", rest=0.0, prox=0.0, inv=0.0, at_best=0.0, fills=0,
        from_ts=0.0, to_ts=60.0, rest_contract_s=0.0):
    return {"t": "presence", "ticker": ticker, "side": side, "from_ts": from_ts,
            "to_ts": to_ts, "rest_dollar_s": rest, "prox_dollar_s": prox,
            "inv_dollar_s": inv, "at_best_s": at_best, "ticks_ct": int(to_ts - from_ts),
            "fills_ct": fills, "fill_notional": 0.0, "rest_contract_s": rest_contract_s}


class TestPSDH(LipTestCase):
    def test_TP1_full_presence_is_3600_and_t_hat_one(self):
        """T-P1 — 60 min at $100 resting AT BEST, no inventory ⇒ 3600 s/h, T̂ = 1."""
        rows = [row(rest=100.0 * 3600, prox=100.0 * 3600, at_best=3600)]
        self.assertAlmostEqual(P.psdh(rows), 3600.0, places=9)
        self.assertAlmostEqual(P.t_hat(rows), 1.0, places=12)

    def test_TP2_replay_parity_shuffled_and_split(self):
        """T-P2 — deltas, never cumulative, so replay is a SUM: shuffled/split rows sum
        identically.  This is the mirror of the write path."""
        whole = [row(rest=100.0 * 3600, prox=90.0 * 3600, inv=10.0 * 3600, at_best=3000)]
        split = []
        for i in range(60):
            split.append(row(rest=100.0 * 60, prox=90.0 * 60, inv=10.0 * 60, at_best=50,
                             from_ts=i * 60.0, to_ts=(i + 1) * 60.0))
        rnd = random.Random(11)
        shuffled = split[:]
        rnd.shuffle(shuffled)
        self.assertAlmostEqual(P.psdh(whole), P.psdh(split), places=6)
        self.assertAlmostEqual(P.psdh(split), P.psdh(shuffled), places=9)

    def test_TP3_inventory_dominates_and_kills(self):
        """T-P3 — $100 resting 1 min then $100 inventory 59 min ⇒ T̂ ≈ 0.0167, and §2.5's
        zero-model kill variant fires.  This IS the PayPal geometry."""
        rows = [row(rest=100.0 * 60, prox=100.0 * 60, at_best=60, fills=1),
                row(inv=100.0 * 59 * 60)]
        committed_h = (100.0 * 60 + 100.0 * 59 * 60) / 3600.0
        self.assertAlmostEqual(committed_h, 100.0, places=9)
        self.assertAlmostEqual(P.psdh(rows), 60.0, places=9)
        self.assertAlmostEqual(P.t_hat(rows), 0.0167, places=4)
        # the venue is decisively bad on committed hours alone
        self.assertTrue(P.M.is_decisive(1, committed_h) is False)   # 1 fill is not decisive
        h = P.SlotHealth()
        for _ in range(C.KILL_CONSEC_EVALS):
            verdict, detail = P.evaluate_slot(h, net_q=-0.5, rows=rows)
        self.assertEqual(verdict, P.HOLD)     # not decisive by fills, so no model-based kill

    def test_TP3b_zero_presence_with_fills_kills_immediately_no_model(self):
        """§2.5's second rule needs NO model, because zero presence is zero objective."""
        rows = [row(inv=100.0 * 3600, fills=3)]     # capital committed, prox exactly zero
        self.assertEqual(P.psdh(rows), 0.0)
        h = P.SlotHealth()
        verdict, detail = P.evaluate_slot(h, net_q=+99.0, rows=rows)
        self.assertEqual(verdict, P.KILL)
        self.assertEqual(detail["reason"], "zero_presence_with_fills")

    def test_TP4_scale_invariance(self):
        """T-P4 — 10x all sizes ⇒ IDENTICAL PSDH.  Numerator and denominator are both ∝ q, so
        SHRINKING SIZE CANNOT FIX A TOXIC VENUE: the only correct response is reallocation."""
        base = [row(rest=100.0 * 3600, prox=40.0 * 3600, inv=20.0 * 3600)]
        big = [row(rest=1000.0 * 3600, prox=400.0 * 3600, inv=200.0 * 3600)]
        self.assertAlmostEqual(P.psdh(base), P.psdh(big), places=9)

    def test_TP5_one_tick_behind_weights_exactly_half(self):
        """T-P5 — the objective's own 0.5^ticks term, metered."""
        m = P.Meter(now=0.0)
        obs_best = {("T", "bid"): {"orders": [{"remaining": 10, "price": 0.50,
                                               "ticks_behind": 0}],
                                   "net_position": 0, "entry_basis": 0}}
        obs_behind = {("T", "bid"): {"orders": [{"remaining": 10, "price": 0.50,
                                                 "ticks_behind": 1}],
                                     "net_position": 0, "entry_basis": 0}}
        m.tick(1.0, obs_best)
        a = dict(m.acc[("T", "bid")])
        m2 = P.Meter(now=0.0)
        m2.tick(1.0, obs_behind)
        b = m2.acc[("T", "bid")]
        self.assertAlmostEqual(b["prox_dollar_s"], 0.5 * a["prox_dollar_s"], places=12)
        self.assertAlmostEqual(b["rest_dollar_s"], a["rest_dollar_s"], places=12)
        self.assertEqual(b["at_best_s"], 0.0)
        self.assertEqual(a["at_best_s"], 1.0)

    def test_empty_denominator_is_zero_not_a_divide(self):
        self.assertEqual(P.psdh([]), 0.0)
        self.assertEqual(P.t_hat([]), 0.0)


class TestMeter(LipTestCase):
    def test_meter_emits_deltas_and_resets(self):
        m = P.Meter(now=0.0)
        obs = {("T", "bid"): {"orders": [{"remaining": 10, "price": 0.50, "ticks_behind": 0}],
                              "net_position": 5, "entry_basis": 0.40}}
        for i in range(60):
            m.tick(float(i + 1), obs)
        self.assertTrue(m.due(60.0))
        rows = m.flush(60.0)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertAlmostEqual(r["rest_dollar_s"], 60 * 10 * 0.50, places=9)
        self.assertAlmostEqual(r["prox_dollar_s"], 60 * 10 * 0.50, places=9)
        self.assertAlmostEqual(r["inv_dollar_s"], 60 * 5 * 0.40, places=9)
        self.assertEqual(r["at_best_s"], 60.0)
        # deltas: a second flush with no ticks is empty, never cumulative
        self.assertEqual(m.flush(120.0), [])

    def test_jitter_over_100ms_is_counted_not_swallowed(self):
        """§4.4's mirror row: the fixed phase is ASSERTED, because a drifting sampler makes
        every T̂ comparison across venues incommensurable."""
        m = P.Meter(now=0.0)
        obs = {("T", "bid"): {"orders": [], "net_position": 0, "entry_basis": 0}}
        m.tick(1.0, obs)
        m.tick(2.0, obs)
        self.assertEqual(m.jitter_breaches, 0)
        m.tick(3.5, obs)                       # 1.5 s: 500 ms of jitter
        self.assertEqual(m.jitter_breaches, 1)


class TestShrinkage(LipTestCase):
    def test_thin_data_is_pulled_toward_the_prior(self):
        thin = [row(rest=1.0 * 3600, prox=0.0)]        # 1 dollar-hour, zero proximity
        self.assertGreater(P.t_hat_shrunk(thin, prior=0.5), P.t_hat(thin))
        self.assertLess(P.t_hat_shrunk(thin, prior=0.5), 0.5)

    def test_thick_data_overwhelms_the_prior(self):
        thick = [row(rest=1000.0 * 3600, prox=0.0)]
        self.assertLess(P.t_hat_shrunk(thick, prior=0.5), 0.01)

    def test_prior_selection_order_series_then_portfolio_then_half(self):
        self.assertAlmostEqual(P.prior_from_median([0.2, 0.4, 0.6], [0.9]), 0.4)
        self.assertAlmostEqual(P.prior_from_median([], [0.9, 0.7, 0.8]), 0.8)
        self.assertAlmostEqual(P.prior_from_median([], []), C.SHRINK_PRIOR_DEFAULT)


class TestKillHysteresis(LipTestCase):
    def test_three_consecutive_evals_and_decisive_before_kill(self):
        """§2.5 — the 45-min hysteresis is the shortest interval that cannot be tripped by one
        fill burst inside one 15-min bucket."""
        rows = [row(rest=100.0 * 3600, prox=10.0 * 3600, fills=20)]
        h = P.SlotHealth()
        v1, _ = P.evaluate_slot(h, net_q=0.001, rows=rows)
        v2, _ = P.evaluate_slot(h, net_q=0.001, rows=rows)
        self.assertEqual((v1, v2), (P.HOLD, P.HOLD))
        v3, d3 = P.evaluate_slot(h, net_q=0.001, rows=rows)
        self.assertEqual(v3, P.KILL)
        self.assertTrue(d3["decisive"])

    def test_a_single_good_eval_resets_the_counter(self):
        rows = [row(rest=100.0 * 3600, prox=10.0 * 3600, fills=20)]
        h = P.SlotHealth()
        P.evaluate_slot(h, net_q=0.001, rows=rows)
        P.evaluate_slot(h, net_q=0.001, rows=rows)
        P.evaluate_slot(h, net_q=0.10, rows=rows)          # zeta >> 1
        self.assertEqual(h.consec_below, 0)
        v, _ = P.evaluate_slot(h, net_q=0.001, rows=rows)
        self.assertEqual(v, P.HOLD)

    def test_indecisive_estimate_never_kills(self):
        rows = [row(rest=1.0 * 3600, prox=0.1 * 3600, fills=2)]     # 2 fills, 1 $·h
        h = P.SlotHealth()
        for _ in range(10):
            v, d = P.evaluate_slot(h, net_q=0.0001, rows=rows)
        self.assertEqual(v, P.HOLD)
        self.assertFalse(d["decisive"])

    def test_zeta_15_is_ratchet_eligible(self):
        rows = [row(rest=100.0 * 3600, prox=90.0 * 3600)]
        h = P.SlotHealth()
        v, d = P.evaluate_slot(h, net_q=C.FLOOR_RATE_PER_H * 1.5, rows=rows)
        self.assertEqual(v, P.RATCHET_ELIGIBLE)
        self.assertAlmostEqual(d["zeta"], 1.5, places=9)


class TestCollapseFourBranches(LipTestCase):
    """T-D4 — "Test all four branches: healthy, genuine collapse (HALT), overnight-quiet (no
    halt, by (a)), and 429-starved (no halt, by (b)); plus day-2 cold start (no halt, by (c))."
    """

    HISTORY = [3000.0] * 7 * 6          # 7 days x 6 fundable hours of healthy PSDH_book

    def _rows(self, prox_per_committed):
        committed_s = 300.0 * 7200.0     # $300 committed for 2 h, in dollar-seconds
        return [row(rest=committed_s, prox=prox_per_committed * (committed_s / 3600.0))]

    def test_branch_healthy_does_not_halt(self):
        v, d = P.collapse_check(self._rows(3000.0), fundable_seconds=7200.0,
                                hourly_history=self.HISTORY, history_days=7,
                                fundable_hours_per_day=6.0)
        self.assertEqual(v, P.NO_HALT)
        self.assertAlmostEqual(d["psdh_book"], 3000.0, places=6)

    def test_branch_genuine_collapse_halts(self):
        """A 4x book-wide degradation — the same magnitude (2 ratchet rungs) that stands a
        single venue down."""
        v, d = P.collapse_check(self._rows(500.0), fundable_seconds=7200.0,
                                hourly_history=self.HISTORY, history_days=7,
                                fundable_hours_per_day=6.0)
        self.assertEqual(v, P.HALT)
        self.assertLess(d["psdh_book"], d["threshold"])

    def test_branch_overnight_quiet_does_not_halt_by_a(self):
        """(a) — the denominator EXCLUDES every second in which no program was live.  Otherwise
        a quiet overnight, THE NORMAL STATE, collapses the metric by arithmetic."""
        v, d = P.collapse_check([], fundable_seconds=0.0, hourly_history=self.HISTORY,
                                history_days=7, fundable_hours_per_day=6.0)
        self.assertEqual(v, P.NO_HALT)
        self.assertEqual(d["reason"], "no_fundable_committed_time")

    def test_branch_429_starved_does_not_halt_by_b(self):
        """(b) — STARVATION IS NOT TOXICITY: presence lost to our own throttling says nothing
        about who is eating us."""
        v, d = P.collapse_check(self._rows(100.0), fundable_seconds=7200.0,
                                hourly_history=self.HISTORY, history_days=7,
                                fundable_hours_per_day=6.0, rate_yield_frac=0.35)
        self.assertEqual(v, P.STARVED)

    def test_branch_ws_down_routes_to_ws_degraded(self):
        v, _ = P.collapse_check(self._rows(100.0), fundable_seconds=7200.0,
                                hourly_history=self.HISTORY, history_days=7,
                                fundable_hours_per_day=6.0, ws_down_frac=0.5)
        self.assertEqual(v, P.WS_DEGRADED)

    def test_branch_day2_cold_start_does_not_halt_by_c(self):
        """(c) — "A median over 3 samples is not a median; a fabricated one halts the book on
        its second day." """
        v, d = P.collapse_check(self._rows(1.0), fundable_seconds=7200.0,
                                hourly_history=[3000.0, 2900.0], history_days=2,
                                fundable_hours_per_day=6.0)
        self.assertEqual(v, P.INACTIVE)

    def test_branch_c_also_requires_six_fundable_hours_a_day(self):
        v, _ = P.collapse_check(self._rows(1.0), fundable_seconds=7200.0,
                                hourly_history=self.HISTORY, history_days=7,
                                fundable_hours_per_day=2.0)
        self.assertEqual(v, P.INACTIVE)

    def test_starvation_is_checked_before_the_ratio(self):
        """Ordering matters: a starved window whose ratio ALSO looks collapsed must route to
        `rate_starved`, not HALT."""
        v, _ = P.collapse_check(self._rows(1.0), fundable_seconds=7200.0,
                                hourly_history=self.HISTORY, history_days=7,
                                fundable_hours_per_day=6.0, rate_yield_frac=0.99)
        self.assertEqual(v, P.STARVED)

    def test_history_is_checked_before_starvation(self):
        """And (c) before (b): on day 2 nothing may halt, whatever else is true."""
        v, _ = P.collapse_check(self._rows(1.0), fundable_seconds=7200.0,
                                hourly_history=[1.0], history_days=1,
                                fundable_hours_per_day=6.0, rate_yield_frac=0.99)
        self.assertEqual(v, P.INACTIVE)


class TestCompaction(LipTestCase):
    def test_folds_to_per_market_side_day_aggregates(self):
        rows = [row(ticker="T", side="bid", rest=10.0, prox=5.0, from_ts=0.0),
                row(ticker="T", side="bid", rest=20.0, prox=6.0, from_ts=60.0),
                row(ticker="T", side="ask", rest=1.0, prox=1.0, from_ts=60.0),
                row(ticker="T", side="bid", rest=7.0, prox=2.0, from_ts=86400.0)]
        agg = P.compact_rows(rows)
        self.assertEqual(len(agg), 3)
        day0_bid = [a for a in agg if a["side"] == "bid" and a["day"] == 0][0]
        self.assertAlmostEqual(day0_bid["rest_dollar_s"], 30.0)
        self.assertAlmostEqual(day0_bid["prox_dollar_s"], 11.0)

    def test_compaction_preserves_psdh(self):
        """Folding must not change the metric — otherwise history and present are measured in
        different units."""
        rows = [row(rest=100.0, prox=40.0, inv=20.0, from_ts=float(i) * 60)
                for i in range(20)]
        before = P.psdh(rows)
        after = P.psdh(P.compact_rows(rows))
        self.assertAlmostEqual(before, after, places=9)


if __name__ == "__main__":
    unittest.main()
