# T010 — Weather sleeve consumes calibrated biases + season-aware city filter

**Priority:** P0 · **Status:** todo · **Gated on:** T003

## Why
T003's `calibrate` produces real per-city bias + MAE in `data/biases.json`, but
the weather sleeve still reads the **placeholder 1.5°F** from `nestor.toml` — so
the edge isn't actually live yet. Wire the calibrated values in.

## Scope
- On weather startup (or config load), if `data/biases.json` exists, override
  each city's `bias` (and optionally `tradeable`) from it. Or have `calibrate`
  write the values back into `nestor.toml`. Decide one; keep it simple.
- Log which biases are in effect each run.

## Season-aware city filter (the real caveat)
The 60-day calibration marked **DEN and SEA tradeable** (recent MAE 0.83 / 1.39),
but the vault's 2-year data says they're unreliable (2yr MAE 2.65 / 2.51, bad in
spring/summer) and the forward test showed **DEN 0/17, SEA 6%** — losers. So a
short-window MAE must NOT auto-promote a city to tradeable. Either:
- keep a hard `tradeable=false` allowlist for known-bad cities (DEN/SEA), or
- make the filter **season-aware** (per the vault: trade a city only in its
  low-MAE seasons), calibrating over matching seasons / longer windows.
Do NOT let `calibrate` flip DEN/SEA to tradeable off 60 days.

## Done when
- Weather uses calibrated biases; DEN/SEA stay excluded regardless of a rosy
  short-window MAE; tests cover the bias-override + the city-filter guard.
