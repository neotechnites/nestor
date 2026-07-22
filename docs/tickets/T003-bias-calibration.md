# T003 — Bias calibration job (the edge)

**Priority:** P0 · **Status:** todo

## Goal
Compute real per-city forecast bias (mean forecast − actual over a trailing
window) instead of the placeholder 1.5°F. Without this, weather buys the raw
hot forecast, not the edge.

## Scope
- `nestor calibrate` subcommand: for each city, pull N trailing days of
  Open-Meteo **historical-forecast** max vs IEM actual max, compute
  `bias = mean(forecast − actual)` and `mae = mean(|corrected − actual|)`.
- Write biases + MAE back to config (T002). Flag cities with MAE ≥ ~2°F as
  non-tradeable automatically (reproduce the DEN/SEA cut).
- Log the calibration table.

## Done when
- Real biases populated; MAE-based tradeable flag matches the vault ranking
  (MIA/ATL/NY/BOS/PHX/CHI tradeable; DEN/SEA not); tests on the bias/MAE math.
