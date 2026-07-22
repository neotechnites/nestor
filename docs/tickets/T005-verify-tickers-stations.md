# T005 — Verify Kalshi series tickers + IEM stations

**Priority:** P1 · **Status:** built-local

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

## Delivered
- `nestor probe-weather` (read-only) — probes Kalshi `/markets?series_ticker=`
  + IEM `daily.json` per city and prints a report; does NOT edit config, it
  flags what to fix. Retries the alternate `KXHIGH`/`KXHIGHT` series spelling
  and the alternate IEM station id (leading `K` dropped) when the configured
  one comes back empty.
- Code: `crates/engine/src/kalshi.rs` (`probe_series` + `parse_markets`),
  `crates/weather/src/probe.rs`, `nestor_bin/src/main.rs` (dispatch).

## Findings (live run 2026-07-21)
- **Kalshi series: all 8 correct as configured** — every series returned open
  markets. The `KXHIGH`/`KXHIGHT` mix in config is real and correct:
  `KXHIGHMIA`, `KXHIGHTATL`, `KXHIGHNY`, `KXHIGHTBOS`, `KXHIGHTPHX`,
  `KXHIGHCHI`, `KXHIGHDEN`, `KXHIGHTSEA` all live. No series changes needed.
- **IEM stations: all 8 wrong** — config uses the 4-letter ICAO with a leading
  `K` (`KMIA`, `KATL`, …), but the IEM ASOS network keys stations by the
  3-letter id (`MIA`, `ATL`, `NYC`, `BOS`, `PHX`, `MDW`, `DEN`, `SEA`). The
  K-prefixed ids return **zero** rows; the stripped ids return full data
  (verified current highs for 2026-07-21). **Fix:** drop the leading `K` from
  every `station` in the `[[cities]]` table / `default_cities()`.
- **Latent bug flagged (out of scope here):** `engine::weather::actual_high`
  reads the JSON field `max_temp_f`, but IEM's `daily.json` field is
  `max_tmpf`; it also reads `data.first()` while the endpoint returns full
  history ascending (so `.first()` is the *oldest* row, not the requested
  date, and `sdate/edate` appear to be ignored). Both will break settlement
  truth (T004) even after the station ids are fixed — worth its own ticket.
