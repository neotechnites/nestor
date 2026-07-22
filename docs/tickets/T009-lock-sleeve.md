# T009 — Lock sleeve (always-on BTC favorite fade)

**Priority:** P2 (epic) · **Status:** todo · **Gated on:** T001, T004

## Goal
The dependable engine edge (forward test: 99.3% win, +3.25%/trade). Always-on:
poll BTC 15-min markets, and in the final 2–4 min buy the favorite at 93–97¢ when
Z ≥ 4 clear of strike; hold to settle.

## Scope (breaks into sub-tickets)
- Kalshi **WebSocket** market-data (not polling) for live book + last price.
- Coinbase BTC 1-min feed + `Z = |spot−strike| / (median_1min × √min_left)`.
- Signal at first checkpoint where 93≤fav<97 and Z≥4 and distance on favorite side.
- Route through Risk layer (fraction sizing, crypto-window cluster cap, kill-switch).
- Order-book fill check at entry (the vault's open question — needs live depth).
- Long-running service (systemd), not a cron one-shot.

## Done when
- Runs live at tiny size, logs every checkpoint/entry/settlement, and the live
  win-rate/EV log tracks the forward-test numbers. (Own spec doc when started.)
