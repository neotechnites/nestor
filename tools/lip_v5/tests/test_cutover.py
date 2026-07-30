"""spec §8.8 — cutover (`--gen-adopt`, adoption gate), T-A1..A4, plus the CUTOVER TRIAGE.

The round trip under test is: v4 ledger → `--gen-adopt` → W2 adoption gate → triage → first
fill → `rollback_clean` flips → SIGTERM writes `v5_handback.json`.
"""

import json
import unittest

from .. import config as C, cutover as CU, money as M, runtime as R
from .base import LipTestCase

RSTAR = 0.00625


def place(oid, ticker, side, price, size, fill_count=0):
    return {"k": "place_resp", "order_id": oid, "ticker": ticker, "side": side,
            "price": price, "size": size, "remaining_count": size - fill_count,
            "fill_count": fill_count}


class TestGenAdopt(LipTestCase):
    def test_reconstructs_positions_and_basis_from_v4s_ledger(self):
        recs = [
            place("1", "TSY", "bid", 0.40, 50, fill_count=50),
            place("2", "GAS", "ask", 0.98, 100),
            {"k": "cancel_resp", "order_id": "2", "http": 200, "reduced_by": 60.0},
        ]
        obj = CU.gen_adopt(recs, now=1000.0)
        rows = {(r["ticker"], r["side"]): r for r in obj["positions"]}
        self.assertAlmostEqual(rows[("TSY", "yes")]["net"], 50.0)
        self.assertAlmostEqual(rows[("TSY", "yes")]["basis"], 0.40, places=6)
        # ask at 0.98 ⇒ the NO leg at (1 − 0.98) = $0.02; 40 learned fills
        self.assertAlmostEqual(rows[("GAS", "no")]["net"], 40.0)
        self.assertAlmostEqual(rows[("GAS", "no")]["basis"], 0.02, places=6)

    def test_settlement_zeroes_a_position(self):
        recs = [place("1", "T", "bid", 0.40, 50, fill_count=50),
                {"k": "settlement", "ticker": "T"}]
        self.assertEqual(CU.gen_adopt(recs, 0.0)["positions"], [])

    def test_duplicate_place_resp_never_double_books(self):
        """v4's N2 defect: a retried write, a copied ledger, an fsync that landed twice."""
        recs = [place("1", "T", "bid", 0.40, 10, fill_count=10),
                place("1", "T", "bid", 0.40, 10, fill_count=10)]
        rows = CU.gen_adopt(recs, 0.0)["positions"]
        self.assertAlmostEqual(rows[0]["net"], 10.0)

    def test_fill_obs_is_deduped_by_fill_id(self):
        """S2: §9.4's crash-gap window re-reads OVERLAPPING ranges by construction."""
        recs = [place("1", "T", "bid", 0.40, 10),
                {"k": "fill_obs", "order_id": "1", "count": 4, "fill_id": "f1"},
                {"k": "fill_obs", "order_id": "1", "count": 4, "fill_id": "f1"}]
        rows = CU.gen_adopt(recs, 0.0)["positions"]
        self.assertAlmostEqual(rows[0]["net"], 4.0)

    def test_a_sell_decreases_the_position(self):
        """B3: `action` was once dropped entirely, so an operator's manual SALE imported as
        MORE inventory."""
        recs = [{"k": "fill_obs", "ticker": "T", "side": "yes", "action": "buy",
                 "count": 10, "price_c": 40, "fill_id": "a"},
                {"k": "fill_obs", "ticker": "T", "side": "yes", "action": "sell",
                 "count": 4, "price_c": 40, "fill_id": "b"}]
        rows = CU.gen_adopt(recs, 0.0)["positions"]
        self.assertAlmostEqual(rows[0]["net"], 6.0)

    def test_normalize_fill_maps_the_yes_leg_correctly(self):
        """The sign-inversion v4 shipped: a buy of 25 YES booked as no:25."""
        self.assertEqual(CU.normalize_fill("yes", "buy"), ("bid", 1.0))
        self.assertEqual(CU.normalize_fill("no", "buy"), ("ask", 1.0))
        self.assertEqual(CU.normalize_fill("yes", "sell"), ("bid", -1.0))

    def test_SFD_fill_obs_then_cancel_matches_v4s_invariant(self):
        """SF-D — v4's per-order invariant is

            filled = fill_count + max(0, remaining_count − reduced_by) + extra_fills

        with `remaining_count` the ORIGINAL remaining, NEVER decremented.  Decrementing it on
        `fill_obs` and then computing `learned` from the reduced value subtracts the same fills
        twice and the count comes out LOW — which is a `net` disagreement at the W2 gate, so
        the market is EXCLUDED and FROZEN.  The bug therefore froze exactly the tickers v4's
        404/crash-gap recovery had just rescued.
        """
        recs = [
            place("1", "T", "bid", 0.40, 100),                  # 100 resting
            {"k": "fill_obs", "order_id": "1", "count": 8, "fill_id": "f1"},
            {"k": "cancel_resp", "order_id": "1", "http": 200, "reduced_by": 70.0},
        ]
        rows = CU.gen_adopt(recs, 0.0)["positions"]
        # v4: extra_fills 8 + max(0, 100 − 70) = 8 + 30 = 38
        self.assertAlmostEqual(rows[0]["net"], 38.0,
                               msg="must match v4's replay arithmetic exactly")

    def test_SFD_the_pre_fix_arithmetic_ran_low_by_the_fill_obs_count(self):
        """The defect's signature: exactly the `fill_obs` count low — the reviewer's 8."""
        recs = [
            place("1", "T", "bid", 0.40, 100),
            {"k": "fill_obs", "order_id": "1", "count": 8, "fill_id": "f1"},
            {"k": "cancel_resp", "order_id": "1", "http": 200, "reduced_by": 70.0},
        ]
        correct = CU.gen_adopt(recs, 0.0)["positions"][0]["net"]
        buggy = 8 + max(0.0, (100 - 8) - 70)                    # decrement-then-subtract
        self.assertEqual(correct - buggy, 8.0)

    def test_SFD_multiple_fill_obs_rows_accumulate_in_extra_fills(self):
        recs = [
            place("1", "T", "bid", 0.40, 100),
            {"k": "fill_obs", "order_id": "1", "count": 5, "fill_id": "f1"},
            {"k": "fill_obs", "order_id": "1", "count": 3, "fill_id": "f2"},
            {"k": "cancel_resp", "order_id": "1", "http": 200, "reduced_by": 90.0},
        ]
        rows = CU.gen_adopt(recs, 0.0)["positions"]
        self.assertAlmostEqual(rows[0]["net"], 5 + 3 + (100 - 90))

    def test_SFD_partial_fill_at_placement_anchors_remaining_count(self):
        """`remaining_count = size − fill_count` at placement, then immutable."""
        recs = [
            place("1", "T", "bid", 0.40, 100, fill_count=20),   # remaining_count = 80
            {"k": "cancel_resp", "order_id": "1", "http": 200, "reduced_by": 50.0},
        ]
        rows = CU.gen_adopt(recs, 0.0)["positions"]
        self.assertAlmostEqual(rows[0]["net"], 20 + (80 - 50))

    def test_SFD_expired_credits_nothing(self):
        recs = [place("1", "T", "bid", 0.40, 100),
                {"k": "expired", "order_id": "1"}]
        self.assertEqual(CU.gen_adopt(recs, 0.0)["positions"], [])

    def test_SFD_assume_filled_credits_the_ORIGINAL_remaining(self):
        recs = [place("1", "T", "bid", 0.40, 100),
                {"k": "assume_filled", "order_id": "1", "ticker": "T"}]
        rows = CU.gen_adopt(recs, 0.0)["positions"]
        self.assertAlmostEqual(rows[0]["net"], 100.0)

    def test_SFD_a_reduced_by_over_remaining_is_clamped(self):
        recs = [place("1", "T", "bid", 0.40, 100),
                {"k": "cancel_resp", "order_id": "1", "http": 200, "reduced_by": 500.0}]
        self.assertEqual(CU.gen_adopt(recs, 0.0)["positions"], [])

    def test_gen_adopt_is_rerunnable(self):
        """A pure function of v4's ledger, so running it twice cannot drift."""
        recs = [place("1", "T", "bid", 0.40, 50, fill_count=50)]
        a = CU.gen_adopt(recs, 1.0)
        b = CU.gen_adopt(recs, 2.0)
        self.assertEqual(a["positions"], b["positions"])

    def test_writes_a_single_json_object(self):
        recs = [place("1", "T", "bid", 0.40, 50, fill_count=50)]
        p = self.path("v5_adopt.json")
        R.atomic_write_json(p, CU.gen_adopt(recs, 0.0))
        with open(p) as fh:
            obj = json.load(fh)
        self.assertEqual(obj["schema"], "lip_v5_adopt/1")


class TestV5TapeIsTheSumOfItsFillRows(LipTestCase):
    """v4 had to INFER fills from order responses because it did not write a row per fill.
    v5 does — book_fill is the single door and it always writes fill_obs — so running v4's
    inference over a v5 tape counts the same contracts TWICE.  Four cases, every one on a
    real v5 path, every one doubling toward phantom inventory (the $315-of-a-$300-ceiling
    budget starvation).  Truth here is always the sum of the fill_obs rows."""

    def net(self, recs, **kw):
        rows = CU.V4Positions().replay(recs, **kw).rows()
        return rows[0]["net"] if rows else 0.0

    def test_a_partial_fill_plus_its_cancel_reduced_by(self):
        # (a) engine.cancel books the learned remainder as fill_id="cancel:<oid>" AND writes
        # the cancel_resp; the inference adds `remaining − reduced_by` on top of the row.
        recs = [place("o1", "T", "bid", 0.40, 10),
                {"k": "fill_obs", "order_id": "o1", "ticker": "T", "side": "bid",
                 "count": 5, "price_c": 40, "fill_id": "cancel:o1"},
                {"k": "cancel_resp", "order_id": "o1", "http": 200, "reduced_by": 5.0}]
        self.assertAlmostEqual(self.net(recs), 5.0)
        self.assertAlmostEqual(self.net(recs, v4_tape=True), 10.0)   # the double

    def test_a_fill_plus_the_cancel_404_assume_path(self):
        # (b) assume_404_filled books the whole remainder (fill_id="assume404:<oid>") and
        # THEN writes assume_filled, which the inference re-applies in full.
        recs = [place("o2", "T", "bid", 0.40, 10),
                {"k": "fill_obs", "order_id": "o2", "ticker": "T", "side": "bid",
                 "count": 10, "price_c": 40, "fill_id": "assume404:o2"},
                {"k": "assume_filled", "order_id": "o2", "ticker": "T",
                 "why": "fills_query_error"}]
        self.assertAlmostEqual(self.net(recs), 10.0)
        self.assertAlmostEqual(self.net(recs, v4_tape=True), 20.0)

    def test_an_assume_filled_row_and_its_OWN_fill_obs(self):
        # (c) the B10 UNKNOWN-exhausted path: book_fill (fill_id="assume:<oid>") then the
        # assume_filled row.  On a v5 tape that row is a FREEZE record, not a quantity.
        recs = [place("o3", "T", "bid", 0.40, 10),
                {"k": "fill_obs", "order_id": "o3", "ticker": "T", "side": "bid",
                 "count": 10, "price_c": 40, "fill_id": "assume:o3"},
                {"k": "assume_filled", "order_id": "o3", "ticker": "T"}]
        self.assertAlmostEqual(self.net(recs), 10.0)
        self.assertAlmostEqual(self.net(recs, v4_tape=True), 20.0)

    def test_a_place_resp_fill_count_and_the_polled_fill_obs(self):
        # (d) an immediate cross: engine.place RECORDS fill_count and books nothing from it —
        # the cross is learned by the fills poll, which writes the row.
        recs = [place("o4", "T", "bid", 0.40, 3, fill_count=3),
                {"k": "fill_obs", "order_id": "o4", "ticker": "T", "side": "bid",
                 "count": 3, "price_c": 40, "fill_id": "f4"}]
        self.assertAlmostEqual(self.net(recs), 3.0)
        self.assertAlmostEqual(self.net(recs, v4_tape=True), 6.0)

    def test_gen_adopt_still_reads_V4s_tape_with_V4s_inference(self):
        """The flag's other end: v4's ledger has no fill_obs for these, so the inference is
        the ONLY thing that knows the position — dropping it there would adopt ZERO."""
        recs = [place("1", "T", "bid", 0.40, 100, fill_count=20),
                {"k": "cancel_resp", "order_id": "1", "http": 200, "reduced_by": 50.0}]
        self.assertAlmostEqual(CU.gen_adopt(recs, 0.0)["positions"][0]["net"], 50.0)

    def test_a_closing_fill_row_still_reduces_the_held_leg_on_a_v5_tape(self):
        """The closing/closed_leg flags added for the −$78 Skubal short are honored by the
        v5 path too — the sum of the rows is SIGNED."""
        recs = [{"k": "adopt", "ticker": "T", "side": "yes", "net": 26.0, "basis": 0.16},
                {"k": "fill_obs", "ticker": "T", "side": "ask", "count": 26.0,
                 "price_c": 3, "fill_id": "f1", "order_id": "o9",
                 "closing": True, "closed_leg": "yes"}]
        self.assertEqual(CU.V4Positions().replay(recs).rows(), [])


class TestAdoptionGate(LipTestCase):
    ROWS = [{"ticker": "TSY", "side": "yes", "net": 50.0, "basis": 0.40},
            {"ticker": "BAD", "side": "yes", "net": 10.0, "basis": 1.50},
            {"ticker": "DIS", "side": "yes", "net": 20.0, "basis": 0.30}]

    def test_clean_adoption(self):
        ex = {("TSY", "yes"): 50.0}
        res = CU.adoption_gate([self.ROWS[0]], ex, marks={("TSY", "yes"): 0.42})
        self.assertEqual(len(res["adopted"]), 1)
        self.assertEqual(res["excluded"], [])
        self.assertEqual(res["orphans"], [])

    def test_TA1_basis_outside_the_band_is_excluded_and_frozen(self):
        """T-A1 — basis outside [0.01, 0.99] ⇒ excluded + frozen + `adopt_basis_rejected`."""
        ex = {("BAD", "yes"): 10.0}
        res = CU.adoption_gate([self.ROWS[1]], ex)
        self.assertEqual(res["adopted"], [])
        self.assertEqual(res["excluded"][0]["reason"], CU.EXCLUDED_BASIS)
        self.assertIn("BAD", res["frozen"])
        self.assertIn("BAD", res["refused_for_quoting"])
        self.assertTrue(self.logs_of("adopt_basis_rejected"))

    def test_TA1_basis_over_twice_the_mark_is_excluded(self):
        row = {"ticker": "M", "side": "yes", "net": 5.0, "basis": 0.80}
        res = CU.adoption_gate([row], {("M", "yes"): 5.0}, marks={("M", "yes"): 0.30})
        self.assertEqual(res["excluded"][0]["why"], "basis_over_2x_mark")

    def test_basis_band_boundaries(self):
        self.assertTrue(CU.basis_ok(0.01)[0])
        self.assertTrue(CU.basis_ok(0.99)[0])
        self.assertFalse(CU.basis_ok(0.0)[0])
        self.assertFalse(CU.basis_ok(1.0)[0])

    def test_TA2_an_exchange_position_absent_from_the_file_is_an_orphan(self):
        """T-A2 — MIRROR (adopt too much ↔ adopt too little).  Without this end, an unadopted
        position is invisible to every control in the binary, forever."""
        ex = {("TSY", "yes"): 50.0, ("GHOST", "no"): 7.0}
        res = CU.adoption_gate([self.ROWS[0]], ex, marks={("TSY", "yes"): 0.42})
        self.assertEqual(len(res["orphans"]), 1)
        self.assertEqual(res["orphans"][0]["ticker"], "GHOST")
        self.assertIn("GHOST", res["refused_for_quoting"])
        self.assertTrue(self.logs_of("orphan_position"))

    def test_TA3_net_disagreement_freezes_quoting_AND_recycling(self):
        """T-A3 — "a quote-only freeze is a naked-short generator" (v1 §9.4b)."""
        ex = {("DIS", "yes"): 25.0}                       # exchange says 25, ledger says 20
        res = CU.adoption_gate([self.ROWS[2]], ex)
        self.assertEqual(res["excluded"][0]["reason"], CU.EXCLUDED_NET)
        self.assertIn("DIS", res["frozen"])
        self.assertIn("DIS", res["refused_for_quoting"])

    def test_a_ledger_position_the_exchange_does_not_have_is_also_a_disagreement(self):
        res = CU.adoption_gate([self.ROWS[0]], {})
        self.assertEqual(res["excluded"][0]["reason"], CU.EXCLUDED_NET)
        self.assertIsNone(res["excluded"][0]["exchange_net"])

    def test_the_whole_mixed_book_at_once(self):
        ex = {("TSY", "yes"): 50.0, ("BAD", "yes"): 10.0, ("DIS", "yes"): 25.0,
              ("GHOST", "no"): 7.0}
        res = CU.adoption_gate(self.ROWS, ex, marks={("TSY", "yes"): 0.42})
        self.assertEqual([a["ticker"] for a in res["adopted"]], ["TSY"])
        self.assertEqual(len(res["excluded"]), 2)
        self.assertEqual(len(res["orphans"]), 1)


class TestTriage(LipTestCase):
    """CUTOVER TRIAGE — v5 judges every adopted position against its OWN net-rate equation.

    LAW CHANGE (owner decision, 2026-07-30: "it's either running and placing orders, or it's
    not running").  These tests once described a path to the wire: triage judged, and the ones
    that failed were ACTIVELY LEFT via a maker shed, armed by `C.CUTOVER_TRIAGE_ENABLED`
    (which this class used to patch True).  That constant is deleted along with the execution
    path, and the tests below are unchanged in substance for one reason: the ARITHMETIC is
    still worth having on the tape.  What they no longer imply is that a MAKER_SHED verdict
    causes an order.  Nothing consumes a verdict; positions ride to settlement.
    `triage(..., enabled=False)` survives as an explicit caller-supplied parameter only."""

    NOW = 1_000_000.0

    def _book(self):
        """A mixed book: a passing treasury daily, a toxic-fill index hourly, and a
        long-horizon mention market."""
        adopted = [
            {"ticker": "TSY", "side": "yes", "net": 20.0, "basis": 0.50},
            {"ticker": "IDXH", "side": "yes", "net": 20.0, "basis": 0.50},
            {"ticker": "MENTION", "side": "yes", "net": 20.0, "basis": 0.30},
        ]
        venues = {
            # passes (★): the §0.4 treasury row
            "TSY": {"rho": 6.25, "S": 50, "p": 0.50, "phi": 0.08, "d": 0.07,
                    "close_ts": self.NOW + 8 * 3600.0,
                    "program_end_ts": self.NOW + 30 * 86400.0,
                    "l_shed_h": 0.5, "t_hat": 1.0, "spread_c": 2},
            # TOXIC FILL: huge measured φ on a short horizon — fails (★) on drift, and the
            # shed clears quickly, so the maker path is cheaper than crossing.
            "IDXH": {"rho": 6.25, "S": 50, "p": 0.50, "phi": 8.0, "d": 0.07,
                     "close_ts": self.NOW + 1 * 3600.0,
                     "program_end_ts": self.NOW + 30 * 86400.0,
                     "l_shed_h": 0.25, "t_hat": 0.05, "spread_c": 2},
            # HORIZON-CARRY FAILURE: the PYPL geometry.  No shed history, close in December.
            "MENTION": {"rho": 0.439, "S": 50, "p": 0.30, "phi": 0.50, "d": 0.07,
                        "close_ts": self.NOW + 156 * 86400.0,
                        "program_end_ts": self.NOW + 30 * 86400.0,
                        "l_shed_h": None, "t_hat": 0.02, "spread_c": 2},
        }
        return adopted, venues

    def test_three_verdicts_on_a_mixed_book(self):
        adopted, venues = self._book()
        verdicts = CU.triage(adopted, venues, self.NOW, RSTAR)
        by = {v["ticker"]: v for v in verdicts}
        self.assertEqual(by["TSY"]["decision"], CU.KEEP)
        self.assertIsNone(by["TSY"]["exit_path"])
        self.assertEqual(by["IDXH"]["decision"], CU.MAKER_SHED)
        self.assertEqual(by["MENTION"]["decision"], CU.TAKER_CROSS)

    def test_the_ledger_shows_all_three_verdicts(self):
        adopted, venues = self._book()
        CU.triage(adopted, venues, self.NOW, RSTAR)
        rows = self.logs_of("cutover_triage")
        self.assertEqual(len(rows), 3)
        for r in rows:
            for field in ("ticker", "net_rate", "decision", "exit_path"):
                self.assertIn(field, r)

    def test_the_mention_market_fails_on_HORIZON_not_on_drift(self):
        """The reason matters: this is the carry term v4 did not have."""
        adopted, venues = self._book()
        v = CU.triage_position(adopted[2], venues["MENTION"], self.NOW, RSTAR)
        self.assertTrue(v["horizon_excluded"])
        self.assertGreater(v["hold_cost_usd"], v["cross_cost_usd"])
        self.assertGreater(v["l_eff_h"], 1000.0)

    def test_the_index_hourly_fails_on_TOXICITY_and_sheds_rather_than_crosses(self):
        """A short horizon means holding is cheap, so paying the spread is not worth it — the
        equation, not a preference, chooses the path."""
        adopted, venues = self._book()
        v = CU.triage_position(adopted[1], venues["IDXH"], self.NOW, RSTAR)
        self.assertFalse(v["horizon_excluded"])
        self.assertEqual(v["reason"], "fails_star")
        self.assertLess(v["hold_cost_usd"], v["cross_cost_usd"])

    def test_G6_gates_the_crossing_exit_and_logs_the_value_forgone(self):
        """spec §7 assigns the taker-exit to Ryan as its own gate.  With the flag FALSE the
        verdict is still computed and the value forgone recorded — the choice is MEASURED
        rather than asserted — and the path falls back to the maker shed."""
        adopted, venues = self._book()
        v = CU.triage_position(adopted[2], venues["MENTION"], self.NOW, RSTAR,
                               taker_enabled=False)
        self.assertEqual(v["decision"], CU.TAKER_CROSS)
        self.assertEqual(v["exit_path"], CU.MAKER_SHED)
        self.assertEqual(v["gate"], "G6_disabled_fallback_maker_shed")
        self.assertGreater(v["value_forgone_usd"], 0.0)
        self.assertFalse(C.TAKER_EXIT_ENABLED, "G6 must ship FALSE")

    def test_G6_enabled_places_the_crossing_exit(self):
        adopted, venues = self._book()
        v = CU.triage_position(adopted[2], venues["MENTION"], self.NOW, RSTAR,
                               taker_enabled=True)
        self.assertEqual(v["exit_path"], CU.TAKER_CROSS)
        self.assertEqual(v["gate"], "G6_enabled")

    def test_a_position_with_no_venue_reading_sheds_rather_than_guesses(self):
        v = CU.triage([{"ticker": "X", "side": "yes", "net": 1.0, "basis": 0.5}], {},
                      self.NOW, RSTAR)[0]
        self.assertEqual(v["decision"], CU.MAKER_SHED)
        self.assertEqual(v["reason"], "no_venue_reading")

    def test_triage_can_be_disabled_wholesale(self):
        adopted, venues = self._book()
        self.assertEqual(CU.triage(adopted, venues, self.NOW, RSTAR, enabled=False), [])

    def test_NOTE43_S2_carry_uses_the_MARK_not_the_sunk_entry_basis(self):
        """note 43 §2 — "The exit's price of impatience is spread + taker fee; its price of
        patience is carry.  Both are computable, and ENTRY PRICE BELONGS IN NEITHER (sunk — a
        rule that anchors on entry cuts winners and rides losers by construction)."

        A deeply underwater position rents only what it could still recover.  Anchoring on
        basis overstates its carry and crosses the spread to escape a sunk number.
        """
        adopted, venues = self._book()
        v = dict(venues["MENTION"])
        v["mark"] = 0.02                                  # basis was 0.30; it collapsed
        out = CU.triage_position(adopted[2], v, self.NOW, RSTAR)
        self.assertAlmostEqual(out["mark_used"], 0.02, places=9)
        self.assertAlmostEqual(out["locked_usd"], 20.0 * 0.02, places=9)
        # the basis-anchored figure would have been 15x larger
        self.assertLess(out["hold_cost_usd"], 20.0 * 0.30 * out["h_wait_h"] * RSTAR)

    def test_NOTE43_S2_an_unpriced_position_falls_back_to_cost(self):
        """The day stop's mark-at-cost convention, reused: the only honest statement about a
        price we cannot observe."""
        adopted, venues = self._book()
        v = dict(venues["MENTION"])
        v.pop("mark", None)
        v.pop("p", None)
        out = CU.triage_position(adopted[2], v, self.NOW, RSTAR)
        self.assertAlmostEqual(out["mark_used"], adopted[2]["basis"], places=9)

    def test_taker_fee_rounds_UP_to_the_cent(self):
        """Rounding down would under-price every crossing decision in the direction that makes
        crossing look better than it is."""
        self.assertAlmostEqual(CU.taker_fee_usd(1, 0.50), 0.02, places=9)
        self.assertAlmostEqual(CU.taker_fee_usd(100, 0.50), 1.75, places=9)

    def test_slippage_is_bounded_by_the_marketable_limit(self):
        """A MARKETABLE LIMIT, never a market order: the worst fill is bounded and KNOWN."""
        adopted, venues = self._book()
        v = dict(venues["MENTION"])
        v["spread_c"] = 50                                 # a wildly wide book
        out = CU.triage_position(adopted[2], v, self.NOW, RSTAR)
        capped = 20.0 * (C.TAKER_EXIT_MAX_SLIPPAGE_C / 100.0) + CU.taker_fee_usd(20.0, 0.30)
        self.assertAlmostEqual(out["cross_cost_usd"], capped, places=9)


class TestRollbackAndHandback(LipTestCase):
    def test_TA4_rollback_clean_flips_on_the_first_fill_against_an_adopted_position(self):
        """T-A4 / SF-2 — "clean ONLY before the first fill on an adopted position"."""
        rb = CU.RollbackState()
        rb.set_adopted([{"ticker": "TSY", "side": "yes"}])
        self.assertTrue(rb.clean)
        rb.note_fill("OTHER", "yes", now=1.0)              # not adopted: still clean
        self.assertTrue(rb.clean)
        rb.note_fill("TSY", "yes", now=2.0)
        self.assertFalse(rb.clean)
        self.assertEqual(rb.first_dirty_fill["ticker"], "TSY")

    def test_the_boundary_is_permanent(self):
        rb = CU.RollbackState()
        rb.set_adopted([{"ticker": "T", "side": "yes"}])
        rb.note_fill("T", "yes", now=1.0)
        rb.note_fill("T", "yes", now=2.0)
        self.assertFalse(rb.clean)
        self.assertEqual(rb.first_dirty_fill["ts"], 1.0)   # the FIRST one, retained

    def test_the_procedure_differs_by_regime_so_nobody_has_to_guess(self):
        rb = CU.RollbackState()
        self.assertNotIn("--import-handback", rb.procedure())
        rb.set_adopted([{"ticker": "T", "side": "yes"}])
        rb.note_fill("T", "yes", now=1.0)
        self.assertIn("--import-handback", rb.procedure())

    def test_TA4_sigterm_writes_a_handback_in_BOTH_regimes(self):
        positions = [{"ticker": "TSY", "side": "yes", "net": 50.0, "basis": 0.40},
                     {"ticker": "GAS", "side": "no", "net": 40.0, "basis": 0.02}]
        obj = CU.handback(positions, now=123.0)
        self.assertEqual(obj["schema"], "lip_v5_handback/1")
        self.assertEqual(len(obj["positions"]), 2)
        for row in obj["positions"]:
            for field in ("ticker", "side", "net", "basis", "source", "ts"):
                self.assertIn(field, row)
            self.assertEqual(row["source"], "v5")

    def test_the_round_trip_gen_adopt_to_handback_preserves_the_book(self):
        """The whole cutover in one assertion: v4's ledger → adopt → gate → handback returns
        the same {ticker, side, net, basis} we adopted."""
        recs = [place("1", "TSY", "bid", 0.40, 50, fill_count=50)]
        adopt = CU.gen_adopt(recs, now=0.0)
        gate = CU.adoption_gate(adopt["positions"], {("TSY", "yes"): 50.0},
                                marks={("TSY", "yes"): 0.42})
        self.assertEqual(len(gate["adopted"]), 1)
        back = CU.handback(gate["adopted"], now=1.0)
        self.assertEqual(back["positions"][0]["ticker"], "TSY")
        self.assertAlmostEqual(back["positions"][0]["net"], 50.0)
        self.assertAlmostEqual(back["positions"][0]["basis"], 0.40, places=6)


if __name__ == "__main__":
    unittest.main()
