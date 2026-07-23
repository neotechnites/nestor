# Nestor

> **REDIRECT 2026-07-23:** production = `nestor run` (streak ≤44¢ scanner + settlement).
> Lock is parked (decay-dead), weather parked (unverdicted). The vault redirect file
> (`Obsidian/nestor/implementation/03 - REDIRECT...`) supersedes older plans below.

Automated trading system for Kalshi retail-priced markets. Part of **Pantheon**.

Strategy research + forward-test results live in the Obsidian vault
(`Documents/Obsidian/nestor`). **Production is all Rust**; Python is kept only
as research/backtest reference under `reference/`.

## Why Rust
Nestor is an ever-growing platform. The engine is the permanent, shared
infrastructure (exchange client, market-data, order router, risk, scheduling)
and benefits from speed, memory safety, and single-binary deploys. Strategies
that clear research become Rust modules — a deliberate quality gate before real
money. Latency-sensitive future strategies (lock edge, cross-venue) get the fast
path natively; no polyglot boundary to maintain.

## Architecture (Cargo workspace)
```
crates/engine/    the shared engine (a lib)
  kalshi.rs       Kalshi API client: public market data + RSA-signed orders
  weather.rs      Open-Meteo forecast + IEM actuals feeds
  config.rs       tradeable cities (series, station, lat/lon, bias)
  sizing.rs       stake -> contract count
  strategy.rs     Strategy trait + shared Engine context  <- the extension point
  logging.rs      stdout + JSONL trade log
crates/weather/   the weather sleeve (implements Strategy)
nestor_bin/       the `nestor` binary — wires engine + strategies, runs one
reference/python/ the original Python scaffold, kept for reference only
```
New strategy = new crate implementing `Strategy`, wired into `nestor_bin`.

## Build & run
```bash
cargo build --release
cp .env.example .env          # NESTOR_ENV=paper by default
./target/release/nestor weather   # paper: logs picks, places no orders
```

## Modes
- `NESTOR_ENV=paper` — logs what it *would* buy, no orders. Safe default.
- `NESTOR_ENV=live` — places real orders (needs Kalshi keys + funded account).

## Secrets
`.env` and `secrets/*.pem` are gitignored. On the VPS / in GitHub they live in
environment secrets, never in the repo.

## Before live
- Verify each city's exact Kalshi series ticker + IEM settlement station.
- Calibrate per-city `bias` from a trailing IEM window (currently placeholder 1.5).
- Test one $1 order round-trip (place -> fill -> settle -> reconcile).
- Confirm the live Open-Meteo forecast endpoint serves the morning run.

## Sleeves
- **Weather** (this build): daily forecast-buy. Forward-test HELD 2026-07-15.
- **Lock** (next): always-on BTC favorite fade in the final 2-4 min of 15-min
  markets. Needs a long-running poller (why we run on a VPS, not GitHub Actions).
