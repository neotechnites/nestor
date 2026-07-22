"""Weather sleeve — the daily job.

For each tradeable city at ~9am ET:
  forecast -> bias-correct -> map to 2F bucket -> skip wet days
  -> find that Kalshi bucket market -> buy tiny (or log in paper mode).

Run via scripts/run_weather.py (which loads .env). This module is import-safe.
"""
import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from config.cities import tradeable_cities
from nestor import weather, logutil
from nestor.kalshi import Kalshi
from nestor.sizing import contracts_for

ET = ZoneInfo("America/New_York")


def target_date():
    """The event date we trade — today in ET (the 9am ET run trades today)."""
    return datetime.now(ET).date()


def kalshi_date_code(d):
    """Kalshi ticker date segment, e.g. 2026-07-22 -> '26JUL22'."""
    return d.strftime("%y%b%d").upper()


def _bucket_market(markets, temp_f, date_code):
    """Pick the market for `date_code` whose bucket best fits temp_f.

    Prefer a range bucket [floor, cap] that contains temp_f; fall back to the
    correct open-ended threshold bucket only if temp is beyond the range ladder.
    """
    day = [m for m in markets if f"-{date_code}-" in m["ticker"]]
    # exact range-bucket hit
    for m in day:
        lo, hi = m.get("floor_strike"), m.get("cap_strike")
        if lo is not None and hi is not None and float(lo) <= temp_f <= float(hi):
            return m
    # else the appropriate threshold bucket (above top / below bottom)
    for m in day:
        lo, hi = m.get("floor_strike"), m.get("cap_strike")
        if lo is not None and hi is None and temp_f >= float(lo):
            return m
        if hi is not None and lo is None and temp_f <= float(hi):
            return m
    return None


def run():
    mode = os.getenv("NESTOR_ENV", "paper").lower()
    stake = float(os.getenv("NESTOR_STAKE_USD", "10"))
    max_daily = float(os.getenv("NESTOR_MAX_DAILY_USD", "80"))

    kal = Kalshi(
        key_id=os.getenv("KALSHI_API_KEY_ID"),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH") if mode == "live" else None,
    )
    td = target_date()
    dcode = kalshi_date_code(td)
    logutil.info(f"weather sleeve start — mode={mode} date={td} ({dcode}) "
                 f"stake=${stake} max_daily=${max_daily}")

    spent = 0.0
    for name, c in tradeable_cities().items():
        try:
            fc, precip = weather.forecast_for(c["lat"], c["lon"], td.isoformat())
        except Exception as e:
            logutil.info(f"{name}: forecast FAILED ({e}) — skip")
            continue

        corrected = fc - c["bias"]

        if precip > 0:
            logutil.record({"event": "skip_wet", "city": name, "fc": fc,
                            "corrected": corrected, "precip": precip})
            logutil.info(f"{name}: wet day (precip={precip}) — skip")
            continue

        try:
            markets = kal.markets(c["series"], status="open")
        except Exception as e:
            logutil.info(f"{name}: market pull FAILED ({e}) — skip")
            continue

        mkt = _bucket_market(markets, corrected, dcode)
        if not mkt:
            logutil.info(f"{name}: no {dcode} bucket for {corrected:.1f}F — skip")
            continue

        ask_cents = round(float(mkt.get("yes_ask_dollars", 0)) * 100)
        if not (2 < ask_cents < 98):
            logutil.info(f"{name}: bucket {mkt['ticker']} ask={ask_cents}c out of band — skip")
            continue

        if spent + stake > max_daily:
            logutil.info(f"{name}: daily cap ${max_daily} reached — stop")
            break

        n = contracts_for(stake, ask_cents)
        decision = {"event": "signal", "city": name, "fc": fc, "corrected": corrected,
                    "precip": precip, "ticker": mkt["ticker"],
                    "bucket": mkt.get("subtitle") or mkt.get("yes_sub_title"),
                    "ask_cents": ask_cents, "contracts": n, "stake": stake, "mode": mode}

        if mode == "live" and n > 0:
            try:
                resp = kal.place_order(mkt["ticker"], side="yes", count=n,
                                       yes_price_cents=ask_cents,
                                       client_order_id=str(uuid.uuid4()))
                decision["order"] = resp
                spent += stake
                logutil.info(f"{name}: BOUGHT {n}x {mkt['ticker']} @ {ask_cents}c")
            except Exception as e:
                decision["error"] = str(e)
                logutil.info(f"{name}: ORDER FAILED ({e})")
        else:
            spent += stake
            logutil.info(f"{name}: [paper] would buy {n}x {mkt['ticker']} @ {ask_cents}c "
                         f"(fc {fc:.1f} -> {corrected:.1f}F)")

        logutil.record(decision)

    logutil.info(f"weather sleeve done — deployed ${spent}")
