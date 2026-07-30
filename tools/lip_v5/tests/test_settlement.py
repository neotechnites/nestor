"""SETTLEMENT RELEASE — the exit every position that is not shed takes.

The adversarial review's finding: `cashfeed.resolve()` had ZERO call sites.  When a market
settled (daily treasuries settle every afternoon) the exchange returned the position's value
as cash, but v5's books never released it: `cash.inventory` kept the basis,
`inventory_basis` kept consuming the ceiling, and the budget
(`ceiling − inventory_basis − resting`) starved on capital Kalshi had already paid back.
The only mitigation was `self.resolved` skipping the CLUSTER charge — the cash side never
released at all.

Every test here is mutation-checked against the specific line it guards:
  * drop the `cash.resolve()` call        → the budget tests fail (inventory_basis stays);
  * drop the release                      → realized-P&L and pending tests fail;
  * drop `_close_settled_position`        → the day-stop coherence test fails (phantom loss);
  * true-down a settled ticker instead    → the T-C2 tests fail (delta above truth);
  * settle on a clock instead of status   → the active-market hand-sale test fails;
  * release on a PARSED zero              → the missing-revenue test fails;
  * drop the `settlement` ledger rows     → the restart-replay tests fail.
"""

import unittest

from .. import config as C, engine as E, exchange as X, runner as RUN
from .test_engine import NOW, EngineCase

TK = "KXUST10AD-26JUL30-T4.10"


class SettlementCase(EngineCase):
    def maker_with_fill(self, n=10, price=0.50):
        """A position built END-TO-END through the one path: place → taker → fills poll.
        No book state is hand-assembled, so these tests exercise the same coherence the
        live engine has to keep."""
        m = self.maker()
        ok, reason, _ = m.place(TK, "bid", price, n, NOW + 3600, NOW,
                                available_cash_usd=1000.0)
        self.assertTrue(ok, reason)
        oid = list(m.orders)[0]
        m.ex.take(oid, n, now=NOW + 1)
        m.poll_fills(NOW + 2)
        self.assertAlmostEqual(m.cash.inventory[TK]["n"], float(n), places=9)
        return m

    def settlement_rows_in_ledger(self, m):
        return [r for r in m.ledger.read() if r.get("k") == "settlement"]


class TestWinnerReleases(SettlementCase):
    """(a) a settled WINNER releases its basis to the budget, realizes payout − basis, and
    stops charging its cluster."""

    def _settled(self):
        m = self.maker_with_fill()               # 10 @ $0.50 → basis $5.00
        self.assertAlmostEqual(m.cash.inventory_basis, 5.0, places=9)
        m.ex.settle(TK, "yes", now=NOW + 100)    # pays 10 × $1.00 = $10.00
        m.reconcile(NOW + 200)
        return m

    def test_the_basis_leaves_the_budget(self):
        m = self._settled()
        self.assertNotIn(TK, m.cash.inventory)
        self.assertAlmostEqual(m.cash.inventory_basis, 0.0, places=9)

    def test_the_realized_pnl_is_payout_minus_basis(self):
        m = self._settled()
        self.assertAlmostEqual(m.cash.realized_pnl, 5.0, places=9)
        self.assertAlmostEqual(m.cash.settled_awaiting_payout, 0.0, places=9)
        self.assertEqual(m.cash.pending, {})

    def test_delta_dollars_equals_the_truth_exactly(self):
        """T-C2 at the release: $5 went out as collateral, $10 came back — the published
        number must be +$5, neither above (forbidden) nor below (the starved state)."""
        m = self._settled()
        self.assertAlmostEqual(m.cash.delta_dollars, 5.0, places=9)

    def test_the_cluster_stops_charging_it(self):
        m = self._settled()
        self.assertIn(TK, m.resolved)
        ctx = m.place_context(available_cash_usd=1000.0)
        self.assertEqual([p for p in ctx.positions if p["ticker"] == TK], [])

    def test_the_engine_books_close_coherently(self):
        m = self._settled()
        self.assertNotIn(TK, m.positions)
        self.assertNotIn(TK, m.position_cost)
        self.assertNotIn((TK, "yes"), m.entry_basis)

    def test_a_winning_settlement_never_reads_as_a_loss(self):
        """(4) The day stop reads `mark_to_market_pnl(positions, position_cost, …)`.
        Removing the position but leaving its cost reads value 0 − cost $5 = −$5: a day
        stop tripped by the exchange PAYING us.  The drawdown guard's equity carries the
        realized +$5, so equity RISES."""
        m = self._settled()
        out = m.cycle(NOW + 300, yes_mids={})
        self.assertAlmostEqual(out["pnl"], 0.0, places=9)
        self.assertNotIn("day_stop", out)
        self.assertEqual(out["drawdown"], 0.0)
        self.assertFalse(m.halt.halted)

    def test_the_ledger_carries_resolve_and_release(self):
        m = self._settled()
        rows = self.settlement_rows_in_ledger(m)
        self.assertEqual([r.get("released") for r in rows], [False, True])
        self.assertAlmostEqual(rows[0]["basis_usd"], 5.0, places=6)
        self.assertAlmostEqual(rows[1]["paid_usd"], 10.0, places=6)
        self.assertAlmostEqual(rows[1]["realized_usd"], 5.0, places=6)

    def test_the_settlements_tape_replaying_every_poll_is_idempotent(self):
        """The endpoint returns the FULL tape on every poll; a second reconcile must not
        mint a phantom pending claim or another cent of P&L."""
        m = self._settled()
        m.reconcile(NOW + 400)
        self.assertAlmostEqual(m.cash.realized_pnl, 5.0, places=9)
        self.assertEqual(m.cash.pending, {})
        self.assertAlmostEqual(m.cash.settled_payout_expected, 0.0, places=9)
        self.assertEqual(len(self.settlement_rows_in_ledger(m)), 2)


class TestLoserReleases(SettlementCase):
    """(b) a settled LOSER releases the same way, with the loss realized.  The zero payout
    needs no cash confirmation — there is no cash to wait for — and `delta_dollars` does
    not move (+basis out of the consumed sum, −basis into realized)."""

    def _settled(self):
        m = self.maker_with_fill()               # 10 @ $0.50, result NO → pays $0.00
        m.ex.settle(TK, "no", now=NOW + 100)
        m.reconcile(NOW + 200)
        return m

    def test_the_basis_leaves_the_budget_with_the_loss_realized(self):
        m = self._settled()
        self.assertAlmostEqual(m.cash.inventory_basis, 0.0, places=9)
        self.assertAlmostEqual(m.cash.realized_pnl, -5.0, places=9)
        self.assertEqual(m.cash.pending, {})
        self.assertAlmostEqual(m.cash.settled_awaiting_payout, 0.0, places=9)

    def test_delta_dollars_does_not_move_no_cash_arrived(self):
        m = self._settled()
        self.assertAlmostEqual(m.cash.delta_dollars, -5.0, places=9)

    def test_a_missing_revenue_field_is_NOT_an_explicit_zero(self):
        """A row whose revenue field is ABSENT parses to nothing, and nothing must not
        release: it resolves (the market IS settled) but the claim waits for a row that
        states the paid amount, bounded by the 6 h page."""
        m = self.maker_with_fill()
        m.ex.settle(TK, "no", now=NOW + 100)
        del m.ex.settlement_rows[0]["revenue"]
        m.reconcile(NOW + 200)
        self.assertIn(TK, m.cash.pending)        # resolved, not released
        self.assertAlmostEqual(m.cash.settled_awaiting_payout, 5.0, places=9)
        self.assertAlmostEqual(m.cash.realized_pnl, 0.0, places=9)
        self.assertAlmostEqual(m.cash.delta_dollars, -5.0, places=9)


class TestSettlementIsNotDivergence(SettlementCase):
    """(c)/(e) — settlement and the reconcile divergence path must never be confused."""

    def test_a_settlement_does_not_freeze_or_page(self):
        m = self.maker_with_fill()
        m.ex.settle(TK, "yes", now=NOW + 100)
        m.reconcile(NOW + 200)
        self.assertNotIn(TK, m.frozen)
        self.assertEqual([r for r in self.logs_of("position_divergence")
                          if r.get("ticker") == TK], [])
        self.assertEqual(self.alerts, [])

    def test_a_determined_market_with_no_settlements_row_resolves_not_true_downs(self):
        """The positions row went to ZERO, the market says `determined`, no settlements
        row yet (the ~41 min index lag).  The basis must move to settled_awaiting_payout —
        NOT be silently trued down, which zeroes `inventory_basis` with no realized offset
        and publishes `delta_dollars` above the truth on every losing settlement."""
        m = self.maker_with_fill()
        m.ex.market_statuses[TK] = "determined"
        m.ex.market_results[TK] = "yes"
        for p in m.ex._positions:
            if p.get("ticker") == TK:
                p["position"] = 0.0
        m.reconcile(NOW + 200)
        self.assertNotIn(TK, m.frozen)
        self.assertNotIn(TK, m.positions)
        self.assertAlmostEqual(m.cash.inventory_basis, 0.0, places=9)   # budget freed NOW
        self.assertAlmostEqual(m.cash.settled_awaiting_payout, 5.0, places=9)
        self.assertAlmostEqual(m.cash.pending[TK].expected_credit_usd, 10.0, places=9)
        self.assertAlmostEqual(m.cash.delta_dollars, -5.0, places=9)    # cash unconfirmed
        self.assertEqual([r for r in self.logs_of("position_divergence")
                          if r.get("ticker") == TK], [])
        # ... and the settlements row later releases it, cash-confirmed.
        m.ex.settlement_rows.append({"ticker": TK, "revenue": 1000})
        m.reconcile(NOW + 400)
        self.assertEqual(m.cash.pending, {})
        self.assertAlmostEqual(m.cash.realized_pnl, 5.0, places=9)
        self.assertAlmostEqual(m.cash.delta_dollars, 5.0, places=9)

    def test_a_lagging_positions_index_does_not_freeze_a_settled_ticker(self):
        """The settlements row can land while the positions endpoint STILL lists the
        position (the indices have independent lags).  Once settled, our books are closed
        — 0 against the stale row's 10 reads as an UP divergence with no resting size to
        defer to, which without the `resolved` skip freezes and pages a human about the
        exchange doing its job."""
        m = self.maker_with_fill()
        m.ex.settlement_rows.append({"ticker": TK, "revenue": 1000})   # index lag: the
        m.reconcile(NOW + 200)                   # position row still shows 10
        self.assertNotIn(TK, m.frozen)
        self.assertEqual(self.alerts, [])
        self.assertAlmostEqual(m.cash.realized_pnl, 5.0, places=9)

    def test_a_zeroed_position_on_an_ACTIVE_market_still_trues_down(self):
        """The hand-sale case the true-up exists for: no settled word from the exchange,
        so the settlement path must NOT claim it — status, never a clock."""
        m = self.maker_with_fill()
        for p in m.ex._positions:
            if p.get("ticker") == TK:
                p["position"] = 0.0
        m.reconcile(NOW + 200)
        self.assertEqual(m.cash.pending, {})     # no phantom settlement claim
        self.assertNotIn(TK, m.resolved)
        self.assertTrue(self.logs_of("position_trued_down"))

    def test_a_genuine_divergence_on_an_unsettled_position_still_freezes(self):
        m = self.maker()
        m.ex._positions = [{"ticker": "GHOST", "position": 25}]
        m.reconcile(NOW)
        self.assertIn("GHOST", m.frozen)
        self.assertTrue(self.logs_of("position_divergence"))


class TestRestartReplay(SettlementCase):
    """(d) restart replay reproduces the released state — the `settlement` ledger rows are
    what make the release survive a crash."""

    def _recovered(self, m):
        m2 = self.maker(ex=m.ex)
        r = RUN.Runner(m2, sleep=lambda _s: None)
        r.recover(NOW + 500)
        return m2

    def test_a_released_settlement_survives_restart(self):
        m = self.maker_with_fill()
        m.ex.settle(TK, "yes", now=NOW + 100)
        m.reconcile(NOW + 200)
        m2 = self._recovered(m)
        self.assertNotIn(TK, m2.positions)
        self.assertNotIn(TK, m2.cash.inventory)
        self.assertAlmostEqual(m2.cash.inventory_basis, 0.0, places=9)
        self.assertAlmostEqual(m2.cash.realized_pnl, 5.0, places=9)
        self.assertEqual(m2.cash.pending, {})
        self.assertAlmostEqual(m2.cash.settled_payout_expected, 0.0, places=9)
        self.assertIn(TK, m2.resolved)

    def test_an_unreleased_claim_survives_restart_as_pending_not_as_free_cash(self):
        """Crash between RESOLVE and the settlements row: replay must rebuild the
        settled-awaiting-payout claim from the row's basis — forgetting it would raise
        `delta_dollars` by the basis with no cash confirmed (the forbidden direction),
        and rebuilding it as inventory would re-starve the budget."""
        m = self.maker_with_fill()
        m.ex.market_statuses[TK] = "determined"
        m.ex.market_results[TK] = "yes"
        for p in m.ex._positions:
            if p.get("ticker") == TK:
                p["position"] = 0.0
        m.reconcile(NOW + 200)                   # resolve only; no settlements row yet
        m2 = self._recovered(m)
        self.assertAlmostEqual(m2.cash.inventory_basis, 0.0, places=9)
        self.assertIn(TK, m2.cash.pending)
        self.assertAlmostEqual(m2.cash.pending[TK].basis_usd, 5.0, places=9)
        self.assertAlmostEqual(m2.cash.settled_awaiting_payout, 5.0, places=9)
        self.assertAlmostEqual(m2.cash.delta_dollars, -5.0, places=9)
        # The row lands after the restart: the recovered process releases it.
        m.ex.settlement_rows.append({"ticker": TK, "revenue": 1000})
        m2.reconcile(NOW + 600)
        self.assertEqual(m2.cash.pending, {})
        self.assertAlmostEqual(m2.cash.realized_pnl, 5.0, places=9)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
