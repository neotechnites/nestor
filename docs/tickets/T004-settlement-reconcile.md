# T004 — Settlement / reconcile loop + P&L

**Priority:** P0 · **Status:** todo · **Gated on:** T001

## Goal
Close the loop: after a market settles, fetch the actual outcome, mark the
position won/lost, realize P&L into the bankroll, and log it. This is what makes
the JSONL log a real live forward test.

## Scope
- `nestor reconcile` subcommand (run daily, morning-after for weather):
  for each open position, fetch IEM actual + Kalshi `result`, determine win/loss,
  call `RiskManager.on_settlement`, append a settlement record to the log.
- Handle not-yet-settled (skip, retry next run) and truth-source disagreement.

## Done when
- Open weather positions settle correctly against IEM/Kalshi; bankroll updates;
  P&L per trade + running equity logged; tests on the win/loss + P&L math.
