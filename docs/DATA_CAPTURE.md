# Data capture (redirect 2026-07-23)

Nestor keeps **everything it generates**: the participation record + decision
context for the streak sleeve. (The observational firehose is owned separately
by the research machines in `~/kalshi_data/` — nestor does not duplicate them.)
Nothing is deleted.

## Outputs

| File | Written by | Cadence | Contents |
|------|-----------|---------|----------|
| `data/obs/YYYY-MM-DD.jsonl` | `streak::strategy::scan_series` | every poll (~1s in entry windows, ~12s outside) | `ts_ms, ticker, yes_ask, no_ask` (asks at deci-cent resolution; `null` when unpriced) |
| `data/streak_week1.jsonl` | `streak::strategy::enter` / `log_skip` | every signal — traded, skipped, or risk-rejected | participation record (below), each carrying the order-book snapshot (`book`) taken at the decision moment |
| `settlements.jsonl` | `engine::reconcile::run` | 60s sweep | `event=settlement, strategy, ticker, won, pnl, result, ts` |
| `data/state.json` | risk layer (atomic) | on every fill/settlement | live bankroll, open positions, settled tail, kill-switch |
| `data/streak_week1.jsonl` (derive rows) | `streak::strategy::derive_prev` | each decisive derivation | `event=streak_derive, series, ticker, close_unix, predicted, derived_avg, derived_margin_bp, strike, buf_samples, used` |
| `data/derive_verify.jsonl` | `streak::strategy::verify_pending` | when official result lands for a derived window | `event=derive_verify, series, ticker, close_unix, predicted, official, used, agree, derived_avg, derived_margin_bp` |
| `data/derive_disabled` (marker) | `streak::strategy::verify_pending` | on a USED-derivation disagreement | presence disables derivation at startup AND at every runtime check; contents note the offending ticker/prediction |

### Derived fourth result (derive-fourth)

Kalshi's REST result lags the close (closed 0-10s → finalized+`result` ~10s →
settled-filter 36s+), often past the streak sleeve's 60s entry window. Since
Kalshi crypto settles on a **60s BRTI average ending at close**, the sampler
takes one Coinbase spot tick per coin per second across the final
`SAMPLE_WINDOW_SECS` (75s) before each boundary and `derive::derive` reconstructs
that average, comparing it to the just-closed market's `floor_strike` with a
`DERIVE_MARGIN` (5bp) decisiveness band. A decisive result synthesizes the 4th
window so the streak can be evaluated at close+0s; entries built on it carry
`derived_fourth:true, derived_avg, derived_margin_bp` in their `streak_signal`
row. Every derivation is verified against the official result when it arrives; a
used-derivation disagreement is a CRITICAL alert and trips `data/derive_disabled`
(the already-risk-managed position stays).

Order-book snapshots (`GET /markets/{ticker}/orderbook`) are fetched **only at
decision moments** (one extra request per signal), never per poll.

## Participation record schema (`data/streak_week1.jsonl`)

Written at **signal time** (`event: streak_signal` for an entry attempt,
`event: streak_skip` for a logged skip):

`ts_signal, series, ticker, streak_dir, side_bought, ask_at_signal (deci-cent),
limit_placed, ts_submit, ts_ack, filled (bool), partial (bool), ts_fill,
fill_price (¢), filled_count, canceled_count, fee_cents, simulated (bool),
order_id, book, reject_reason`

Paper-mode fills carry `simulated: true`. Missed fills (placed, nothing filled,
remainder canceled) carry `reject_reason: "missed_fill"`; risk rejections carry
`reject_reason: "risk:<Rejection>"`.

### Settlement fields join by ticker

`ts_settle, result, pnl_cents` are **not** known at signal time — they are
realized later by the reconcile loop and written to `settlements.jsonl`. The
end-of-week report joins the two files on `ticker`:

- `ts_settle` = the settlement record's `ts`
- `result`    = the settlement record's `result` ("yes"/"no")
- `pnl_cents` = the settlement record's `pnl` × 100 (net of the taker fee already
  charged at fill time)

One `streak_signal` row + one `settlement` row per filled ticker = the complete
lifecycle. Skips and missed fills have no settlement row (nothing was held).

## Nightly compression

Dated observation logs older than **today** are gzipped (10-20× shrink); live
files are never touched, `.gz` files are skipped (idempotent). Two ways to run it:

1. **In-process (default).** `nestor run` spawns an hourly task
   (`compress_old_obs_logs` in `nestor_bin/src/main.rs`) — no cron needed.
2. **Standalone / cron.** `scripts/compress_obs.sh` does the same thing for
   operators who run the scanner some other way. Example crontab (05:00 daily):

   ```
   0 5 * * *  cd /path/to/nestor && scripts/compress_obs.sh >> data/obs/compress.log 2>&1
   ```

Both keep everything and delete nothing.
