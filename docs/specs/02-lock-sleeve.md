# Spec 02 — Lock sleeve (deep-longshot fade)

The highest-value edge (vault: forward-tested 99.3% win, +3.25%/trade). A separate
`Strategy` crate on the shared engine — NOT part of weather.

## The rule (from the vault, note "08 - The Lock Edge")
In a KXBTC15M 15-min market, late in the window (~240s→30s before close), at the
FIRST checkpoint where ALL hold, buy the favorite and hold to settlement:
1. Favorite price ∈ [93, 97)¢ (favorite = max(yes, 100−yes)).
2. `Z ≥ 4`, where `Z = |spot − strike| / (median_1min_move × √minutes_left)`;
   `median_1min_move` = median absolute 1-min BTC move over the prior 15 min.
3. Distance is on the favorite's side (`fav_is_yes == (spot > strike)`).
Entry = favorite ask (+0.5¢ spread in backtest). BTC-only (alts were negative).

## Why it works
Kalshi settles on the 60-sec BRTI average and caps the quote near 99¢, so a
genuinely-locked favorite (far from strike, little time left) is underpriced at
93–97¢. You sell deep insurance the market misprices (~99% winners at ~95¢).

## Build phases
1. **Signal core** (`crates/lock/signal.rs`) — pure `z_score` + `evaluate`, unit-tested. ✅ this commit.
2. **Backtest** (`crates/lock/backtest.rs`, `nestor backtest-lock`) — reproduce the
   edge in-code against cached forward data, confirming ~99.3% win / +3.25%. ✅ this commit.
3. **Live sleeve** (later) — this is the hard part and needs keys/VPS:
   - Kalshi **WebSocket** market-data (react to book/last-price changes, not polling).
   - Coinbase BTC 1-min feed for spot + median move.
   - Always-on service (systemd), scanning live markets in the final 2–4 min.
   - Route orders through `Engine::execute` (fraction sizing, crypto-window cluster
     key `btc:<close_ts>`, the portfolio cap already added).
   - **Depends on T011 fill-verification** — lock fills in the last 2 minutes with no
     time to hand-watch, so real-fill confirmation is mandatory before it trades live.

## Risk integration
- Sizing: `SizingHint::Fraction` (default 5%/trade), cluster cap 15%, portfolio cap 50%.
- Cluster key groups a 15-min window so correlated flips count as one bet.
- Same kill-switch (drawdown / daily loss) as weather; shared bankroll.

## Open (unchanged from vault)
- Live order-book fill depth in the final 2–4 min (no historical order-book data).
- The all-assets-at-once crash tail (unobserved) — caps safe size.
