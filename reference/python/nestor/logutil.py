"""Structured logging: human line to stdout + a machine JSONL trade log.
The JSONL log is the live forward test — every decision is recorded."""
import json
import os
from datetime import datetime, timezone

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
TRADE_LOG = os.path.join(LOG_DIR, "weather_trades.jsonl")


def _now():
    return datetime.now(timezone.utc).isoformat()


def info(msg):
    print(f"[{_now()}] {msg}", flush=True)


def record(event):
    """Append one event dict to the JSONL trade log."""
    os.makedirs(LOG_DIR, exist_ok=True)
    event = {"ts": _now(), **event}
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps(event) + "\n")
    return event
