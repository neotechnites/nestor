# T005 — Verify Kalshi series tickers + IEM stations

**Priority:** P1 · **Status:** todo

## Goal
Confirm each weather city's exact Kalshi series ticker and its settlement
station, so we trade the right market and settle against the right truth.
Forward test saw a KXHIGH<city> / KXHIGHT<city> mix.

## Scope
- `nestor probe-weather`: for each city, hit Kalshi `/events` + `/markets` to
  confirm the series exists and read its settlement rule/subtitle; cross-check
  the IEM station matches Kalshi's settlement source.
- Correct the config table; document each city's verified series + station.

## Done when
- All 8 cities' series + stations verified against live Kalshi/IEM and committed.
