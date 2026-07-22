# T004 — Settlement / reconcile loop + P&L

**Priority:** P0 · **Status:** built-local (PR pending personal remote) · **Gated on:** T001

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

## Implementation notes (built-local)
- `nestor reconcile` subcommand (`nestor_bin/src/main.rs`): iterates open
  positions, fetches each Kalshi market (`Kalshi::market(ticker)` — public GET
  `markets/{ticker}`, added in `kalshi.rs`), reads the authoritative `result`,
  and settles won = (our side matches). Not-yet-settled / void markets are
  skipped and retried next run. Logic lives in `engine::reconcile`.
- P&L realized through `RiskManager::settle(ticker, won) -> Option<SettleOutcome>`
  (pure w.r.t. network — reconcile fetches the result and passes it in, so the
  money math is unit-testable offline). `on_settlement` is now a thin wrapper.
- Each settlement appends `{event:"settlement", ticker, won, pnl, result}` to
  `logs/weather_trades.jsonl`.
- **Day-loss misattribution fix:** a `Position` now records the ET trading day
  it was opened on. A realized loss feeds the *current* day's `day_loss` (the
  daily-loss kill-switch) only when `position.day == state.day`; prior-day
  settlements still update bankroll/peak/drawdown but never today's day_loss.
  Reconcile rolls the day to today (ET) first. So a next-morning reconcile of
  yesterday's loss cannot trip today's daily-loss halt, while a same-day
  settlement (crypto) still does. Tested both directions.
- Tests: `risk::tests::{settlement_pnl_loss, settle_returns_outcome_and_none_for_unknown,
  same_day_loss_trips_daily_halt, prior_day_loss_does_not_trip_todays_daily_halt}`
  and `reconcile::tests::{settled_yes_market, settled_no_side, not_settled_is_skipped}`.
