# T012 — Streak ≤44¢ sleeve (BUILD #1 per redirect 2026-07-23)

**Priority:** P0 · **Status:** built (paper) · **Spec:** vault `implementation/03 - REDIRECT - Build Streak (2026-07-23).md` (supersedes repo docs where they conflict)

## What
Post-4-streak reversal on KXBTC15M + KXETH15M: when the last 4 settled 15-min
windows printed the same direction, buy the opposite side of the new window in
its **first 60s**, only if that side's ask **≤ 44¢**. Taker-only, one order per
market, hold to settle. 2-yr regime-proof (56-57% every slice, both coins, fees in).

## Why streak now (verdicts are dated)
- Lock: DEAD by decay (kill-scan +1.72¢→−1.07¢/contract) → parked, unscheduled.
- Weather: unverdicted (forward capture ~3-4 wks) → parked, unscheduled.
- Streak ≤44¢ was wrongly benched under the old unconditional ~50¢ result; the
  price-gated rule survives every slice. Live week-1 = MECHANICS, not efficacy.

## Delivered
- `crates/streak`: pure `signal::detect` (consecutiveness, streak direction,
  abutment incl. prev-not-settled lag diagnostic, 60s entry window, 44¢ gate) +
  `strategy::Streak` (scan pass; Flat sizing; cluster `streak-<close>` shared
  across coins = one bet; one-attempt-per-market dedup; week-1 JSONL logging).
- `nestor run` rewired: streak foreground (15s) + settlement sweep (60s). Lock &
  weather parked (manual subcommands only). No default subcommand.
- Fee fix: `taker_fee` = ceil-per-ORDER of 0.07·count·P·(1−P) (was un-ceiled →
  P&L overstated). Cluster cap now binds Flat sizing too.
- Week-1 log: `data/streak_week1.jsonl` (signals, fills, fees, gated/missed/
  risk-rejected skips); settlements strategy-tagged in `logs/settlements.jsonl`.
- Week-1 config in nestor.toml: bankroll $100, flat $4, daily $60.
- 60 tests; clippy/fmt clean; live-API smoke verified (no-streak silence matched
  the actual settled feed).

## Remaining
- Paper 1-2 days → verify signal frequency/dedup/settlement.
- Ryan: Kalshi keys → `selftest-order` → live $100 × 7 days → mechanics report
  (fill realization vs 60% assumption, slippage, frequency, fee actual vs formula,
  settlement timing; win% context-only).
