"""spec §8.5 — `rate_bucket`, T-B1..B5; and §8.6 — `shade_decision`, T-S1..S3."""

import unittest

from .. import config as C, money as M, ratelimit as RL
from .base import LipTestCase


class TestBucket(LipTestCase):
    def test_TB1_steady_four_per_second_sustained(self):
        b = RL.Bucket(now=0.0)
        t = 0.0
        admitted = 0
        for i in range(400):                       # 100 s of wall clock at 4 Hz
            t += 0.25
            ok, _ = b.admit("place", t)
            admitted += 1 if ok else 0
        self.assertGreaterEqual(admitted, 395)     # the burst absorbs the ramp
        self.assertEqual(b.b, C.RATE_CAP_HZ)

    def test_demand_above_the_cap_is_refused_not_borrowed(self):
        b = RL.Bucket(now=0.0)
        t = 0.0
        admitted = 0
        for i in range(400):                       # 40 s at 10 Hz — well over budget
            t += 0.1
            ok, _ = b.admit("book_poll", t)
            admitted += 1 if ok else 0
        self.assertLess(admitted, 200)
        self.assertGreater(b.refused, 0)

    def test_TB2_429_halves_holds_60s_then_recovers_geometrically(self):
        b = RL.Bucket(now=0.0)
        self.assertEqual(b.b, 4.0)
        b.on_429(now=0.0)
        self.assertEqual(b.b, 2.0)                 # multiplicative DECREASE
        self.assertTrue(self.logs_of("rate_yield"))
        b.step(now=30.0)
        self.assertEqual(b.b, 2.0)                 # held for 60 s
        b.step(now=61.0)
        self.assertAlmostEqual(b.b, 2.5, places=9)  # x1.25
        for k in range(2, 12):
            b.step(now=61.0 + 60.0 * k)
        self.assertAlmostEqual(b.b, 4.0, places=9)  # capped at the budget

    def test_TB2_floor_at_half_a_request_per_second(self):
        """"Below it we cannot hold even one market"."""
        b = RL.Bucket(now=0.0)
        for i in range(20):
            b.on_429(now=float(i))
        self.assertEqual(b.b, C.RATE_MIN_HZ)

    def test_TB2_rate_starved_alerts_after_ten_minutes_below_half_cap(self):
        """Silent permanent yielding is indistinguishable from a dead bot."""
        b = RL.Bucket(now=0.0)
        b.on_429(now=0.0)
        b.on_429(now=1.0)                          # B = 1.0, which is < 0.5 x 4.0
        alerts = b.step(now=2.0)
        self.assertEqual(alerts, [])
        alerts = b.step(now=2.0 + C.RATE_STARVED_ALERT_S + 1)
        self.assertIn("rate_starved", alerts)

    def test_recovery_clears_the_starvation_state(self):
        b = RL.Bucket(now=0.0)
        b.on_429(now=0.0)
        b.on_429(now=1.0)
        b.step(now=2.0)                                    # starts the starvation clock
        b.step(now=700.0)
        self.assertTrue(b.starved_alerted)
        for k in range(1, 12):
            b.step(now=700.0 + 60.0 * k)
        self.assertFalse(b.starved_alerted)

    def test_TB3_exit_cancel_is_admitted_at_zero_tokens_while_a_poll_is_refused(self):
        """T-B3 — "a rate budget must never be the reason an order cannot be cancelled"."""
        b = RL.Bucket(now=0.0)
        b.tokens = 0.0
        ok_poll, why_poll = b.admit("book_poll", 0.0)
        self.assertFalse(ok_poll)
        self.assertEqual(why_poll, "reserve_floor")
        ok_exit, why_exit = b.admit(C.LANE_NEVER_REFUSED, 0.0)
        self.assertTrue(ok_exit)
        self.assertEqual(why_exit, "exit_cancel_never_refused")

    def test_the_reserve_floor_keeps_one_token_for_the_exit_lane(self):
        b = RL.Bucket(now=0.0)
        b.tokens = 1.5
        self.assertFalse(b.admit("place", 0.0)[0])         # 1.5 − 1 < 1
        b.tokens = 2.0
        self.assertTrue(b.admit("place", 0.0)[0])

    def test_exit_cancel_may_drive_the_bucket_negative_and_refill_repays_it(self):
        b = RL.Bucket(now=0.0)
        b.tokens = 0.0
        for i in range(5):
            self.assertTrue(b.admit(C.LANE_NEVER_REFUSED, 0.0)[0])
        self.assertLess(b.tokens, 0.0)
        b.refill(10.0)
        self.assertGreater(b.tokens, 0.0)


class TestCancelShare(LipTestCase):
    """T-B5 (SF-1) — an unbounded preempting lane is a STARVATION WEAPON."""

    def _busy(self, b, t0=0.0):
        """Four admitted non-cancel requests, so the 1-in-4 bound becomes measurable."""
        t = t0
        for lane in ("place", "place", "verify", "book_poll"):
            t += 0.5
            b.admit(lane, t)
        return t

    def test_TB5_requote_cancel_degrades_past_25_percent(self):
        b = RL.Bucket(now=0.0)
        t = self._busy(b)
        t += 0.5
        ok, why = b.admit("requote_cancel", t, key=("T", "bid"))
        self.assertTrue(ok)                                # 1 of 5 = 20%, inside the bound
        t += 0.5
        ok, why = b.admit("requote_cancel", t, key=("T", "bid"))
        self.assertFalse(ok)                               # 2 of 6 = 33%, over
        self.assertEqual(why, "cancel_share_exceeded")
        self.assertTrue(self.logs_of("cancel_share_exceeded"))

    def test_TB5_the_degrade_leaves_the_resting_order_in_place(self):
        """"its slot falls back to leaving the resting order in place until the next tick — a
        STALE QUOTE, which is a rate loss, not a risk"."""
        b = RL.Bucket(now=0.0)
        t = self._busy(b)
        for i in range(10):
            t += 0.5
            b.admit("requote_cancel", t, key=("T", "bid"))
        share, n = b.cancel_share(t)
        self.assertLessEqual(share, C.CANCEL_SHARE_MAX + 1e-9)

    def test_TB5_exit_cancel_is_never_degraded_nor_counted(self):
        b = RL.Bucket(now=0.0)
        t = self._busy(b)
        for i in range(20):
            t += 0.1
            ok, why = b.admit(C.LANE_NEVER_REFUSED, t, key=("T", "bid"))
            self.assertTrue(ok)
        share, n = b.cancel_share(t)
        self.assertEqual(share, 0.0)                       # not counted against the bound
        self.assertEqual(n, 4)                             # only the four non-exit requests

    def test_TB5_three_breaches_in_ten_minutes_poisons_the_market(self):
        b = RL.Bucket(now=0.0)
        key = ("T", "bid")
        t = self._busy(b)
        breaches = 0
        while breaches < 3 and t < 300.0:
            t += 0.5
            ok, why = b.admit("requote_cancel", t, key=key)
            if why == "cancel_share_exceeded":
                breaches += 1
        self.assertEqual(breaches, 3)
        self.assertTrue(b.poison_due(key, t))
        self.assertFalse(b.poison_due(("OTHER", "bid"), t))

    def test_the_bound_is_not_enforced_below_four_samples(self):
        """Enforcing 1-in-4 on fewer than 4 requests would refuse the FIRST cancel of every
        window — a starvation weapon of the opposite sign."""
        b = RL.Bucket(now=0.0)
        ok, why = b.admit("requote_cancel", 0.1, key=("T", "bid"))
        self.assertTrue(ok)

    def test_the_window_rolls(self):
        b = RL.Bucket(now=0.0)
        t = self._busy(b)
        b.admit("requote_cancel", t + 0.5, key=("T", "bid"))
        share_now, _ = b.cancel_share(t + 1.0)
        self.assertGreater(share_now, 0.0)
        share_later, n_later = b.cancel_share(t + C.CANCEL_SHARE_WINDOW_S + 1.0)
        self.assertEqual((share_later, n_later), (0.0, 0))


class TestScheduler(LipTestCase):
    def test_strict_priority_order(self):
        b = RL.Bucket(now=0.0)
        b.tokens = 3.0                                     # room for exactly two non-exit
        s = RL.Scheduler(b)
        s.submit("classify_sweep", "c")
        s.submit("book_poll", "p")
        s.submit("exit_cancel", "x")
        s.submit("place", "pl")
        served, deferred = s.drain(0.0)
        lanes = [l for l, _, _ in served]
        self.assertEqual(lanes[0], "exit_cancel")           # top priority, always served
        self.assertIn("place", lanes)
        self.assertNotIn("classify_sweep", lanes[:2])

    def test_deferred_requests_stay_queued(self):
        b = RL.Bucket(now=0.0)
        b.tokens = 2.0
        s = RL.Scheduler(b)
        for i in range(5):
            s.submit("book_poll", "p%d" % i)
        served, deferred = s.drain(0.0)
        self.assertEqual(len(served), 1)
        self.assertEqual(len(s.queue), 4)


class TestDegradeLadder(LipTestCase):
    """T-B4 — the ladder fires in §3.4's order under a shrinking bucket, and step 3 drops the
    LOWEST-`net` market first."""

    def _demand(self, n_ws=10, n_rest=10):
        markets = []
        for i in range(n_ws):
            markets.append({"ticker": "WS%d" % i, "net": 1.0 + i, "ws_fresh_gated": True})
        for i in range(n_rest):
            markets.append({"ticker": "R%d" % i, "net": 0.5 + i, "ws_fresh_gated": False})
        return RL.Demand(markets)

    def test_no_degrade_when_inside_budget(self):
        d = RL.Demand([{"ticker": "A", "net": 1.0, "ws_fresh_gated": False}])
        steps, d2 = RL.degrade_plan(d, budget_hz=100.0)
        self.assertEqual(steps, [])

    def test_TB4_step_one_is_the_classify_sweep(self):
        d = self._demand(n_ws=0, n_rest=1)
        # demand = 5.0 classify + 1.0 poll + tiny recon; a budget just under that needs step 1
        steps, d2 = RL.degrade_plan(d, budget_hz=3.0)
        self.assertEqual(steps[0], "classify_5hz_to_1hz")
        self.assertEqual(d2.classify_hz, C.CLASSIFY_HZ_DEGRADED)

    def test_TB4_step_two_drops_strictly_redundant_polls(self):
        d = self._demand(n_ws=10, n_rest=2)
        steps, d2 = RL.degrade_plan(d, budget_hz=4.0)
        self.assertEqual(steps[:2], ["classify_5hz_to_1hz", "drop_redundant_book_polls"])
        self.assertTrue(d2.drop_redundant)
        self.assertEqual(len(d2.polled()), 2)

    def test_TB4_step_three_drops_the_LOWEST_net_market_first(self):
        d = self._demand(n_ws=0, n_rest=6)
        steps, d2 = RL.degrade_plan(d, budget_hz=3.5)
        self.assertIn("drop_lowest_net_markets", steps)
        self.assertEqual(d2.dropped[0], "R0")               # net 0.5, the lowest
        remaining = {m["ticker"] for m in d2.markets}
        self.assertIn("R5", remaining)                      # net 5.5, the highest, retained

    def test_TB4_the_full_ladder_fires_in_order(self):
        d = self._demand(n_ws=4, n_rest=4)
        steps, d2 = RL.degrade_plan(d, budget_hz=0.5)
        self.assertEqual(steps, list(C.DEGRADE_STEPS))
        self.assertEqual(d2.recon_s, C.RECON_POSITIONS_S_DEGRADED)

    def test_recon_is_slowed_but_NEVER_dropped(self):
        d = self._demand(n_ws=1, n_rest=1)
        steps, d2 = RL.degrade_plan(d, budget_hz=0.001)
        self.assertGreater(1.0 / d2.recon_s, 0.0)           # still polling: it is the truth-reader
        self.assertEqual(d2.recon_s, C.RECON_POSITIONS_S_DEGRADED)

    def test_the_never_degraded_list_is_explicit(self):
        for name in ("exit_cancel", "t3_close_sweep", "day_stop_flatten", "cash_feed_write"):
            self.assertIn(name, C.DEGRADE_NEVER)
            self.assertNotIn(name, C.DEGRADE_STEPS)


class TestShade(LipTestCase):
    """spec §8.6 — T-S1..S3.  Derived from the inequality; there is no shading constant."""

    ARGS = dict(rho=6.25, q=100.0, S=50.0, p=0.50, d=0.07, l_eff=8.0, r_star=0.00625)

    def test_TS1_equal_phi_never_shades(self):
        """T-S1 — with φ₁ = φ₀ shading halves the score FOR NOTHING."""
        k, detail = M.shade_decision(phi0=0.08, phi1=0.08, **self.ARGS)
        self.assertEqual(k, 0)
        self.assertGreater(detail["k0"], detail["k1"])

    def test_TS2_zero_phi1_and_a_large_L_eff_shades(self):
        """T-S2 — when standing one tick back avoids all the adverse selection and the carry
        it drags, the halved score is the cheaper side."""
        args = dict(self.ARGS)
        args["l_eff"] = 3744.0
        k, detail = M.shade_decision(phi0=0.50, phi1=0.0, **args)
        self.assertEqual(k, 1)
        self.assertGreater(detail["k1"], detail["k0"])

    def test_TS3_k_ge_2_is_never_returned(self):
        """T-S3 — "score ≤ 25% and those dollars beat it at the water level in another venue"."""
        args = dict(self.ARGS)
        args["l_eff"] = 1e9
        k, _ = M.shade_decision(phi0=1.0, phi1=0.0, **args)
        self.assertLessEqual(k, C.MAX_SHADE_TICKS)
        self.assertEqual(C.MAX_SHADE_TICKS, 1)

    def test_unmeasured_phi1_with_no_trade_tape_refuses_to_shade(self):
        """No evidence ⇒ stay at best, which is the objective's own preferred state."""
        k, detail = M.shade_decision(phi0=0.08, phi1=None, **self.ARGS)
        self.assertEqual(k, 0)
        self.assertIsNone(detail)

    def test_the_seed_is_a_measurement_from_the_trade_tape(self):
        """`φ₁ = φ₀ × P(trade size ≥ depth at best)` — a measurement, not a guess."""
        k, detail = M.shade_decision(phi0=0.50, phi1=None, trade_ge_depth_prob=0.10,
                                     **dict(self.ARGS, l_eff=3744.0))
        self.assertAlmostEqual(detail["phi1"], 0.05, places=9)
        self.assertEqual(k, 1)

    def test_both_sides_of_the_inequality_are_reported_for_the_log(self):
        """§4.3: "Log `shade_decision` per slot per cycle with BOTH SIDES of the inequality"."""
        _, detail = M.shade_decision(phi0=0.08, phi1=0.04, **self.ARGS)
        for field in ("k0", "k1", "phi0", "phi1", "cost_per_fill"):
            self.assertIn(field, detail)


if __name__ == "__main__":
    unittest.main()
