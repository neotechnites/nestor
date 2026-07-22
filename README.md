# Nestor

Automated trading system for Kalshi retail-priced markets. Part of **Olympus**.

Strategy research + forward-test results live in the Obsidian vault
(`Documents/Obsidian/nestor`). This repo is the live implementation.

## Sleeves
- **Weather** (first build, this repo): daily forecast-buy on Kalshi high-temp
  markets. Bias-correct Open-Meteo forecast → 2°F bucket → buy ~9am ET on dry
  days in the 6 tradeable cities → hold to settlement.
- **Lock** (later): always-on BTC favorite fade in the final 2–4 min of 15-min
  markets. Needs a long-running poller (why we run on a VPS, not GitHub Actions).

## Layout
```
nestor/         core modules
  weather.py    Open-Meteo forecast + IEM actuals
  kalshi.py     Kalshi API client (public data + signed order placement)
  sizing.py     stake -> contract count
  weather_bot.py the daily job
  logutil.py    stdout + JSONL trade log
config/cities.py  tradeable cities (series ticker, station, lat/lon, bias)
scripts/run_weather.py  cron entrypoint
.github/workflows/deploy.yml  deploy-to-VPS on push (fill in once VPS exists)
```

## Run locally
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # NESTOR_ENV=paper by default
python3 scripts/run_weather.py  # paper mode: logs picks, places no orders
```

## Modes
- `NESTOR_ENV=paper` — logs what it *would* buy, no orders. Safe default.
- `NESTOR_ENV=live` — places real orders (needs Kalshi keys + funded account).

## Secrets
`.env` and `secrets/*.pem` are gitignored. On the VPS / in GitHub they live in
environment secrets, never in the repo.

## Before live
- Verify each city's exact Kalshi series ticker + IEM settlement station.
- Calibrate per-city `bias` from a trailing IEM window.
- Test one $1 order round-trip (place → fill → settle → reconcile).
- Confirm the live Open-Meteo `forecast` endpoint serves the morning run.
