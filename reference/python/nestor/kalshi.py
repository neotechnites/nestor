"""Minimal Kalshi trade-api v2 client.

Public market data (markets, trades, orderbook) needs no auth. Portfolio and
order placement require RSA request signing (API key + private key).

Auth: sign  timestamp_ms + METHOD + path  with RSA-PSS/SHA256, base64 it, and
send KALSHI-ACCESS-{KEY,SIGNATURE,TIMESTAMP} headers.
"""
import base64
import time
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE = "https://api.elections.kalshi.com"
PREFIX = "/trade-api/v2"


class Kalshi:
    def __init__(self, key_id=None, private_key_path=None):
        self.key_id = key_id
        self._pk = None
        if private_key_path:
            with open(private_key_path, "rb") as f:
                self._pk = serialization.load_pem_private_key(f.read(), password=None)

    # ---- auth ----
    def _headers(self, method, path):
        if not (self.key_id and self._pk):
            raise RuntimeError("Kalshi private auth requires key_id + private_key_path")
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = self._pk.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def _get_public(self, path, params=None):
        r = requests.get(BASE + PREFIX + path, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _signed(self, method, path, body=None):
        h = self._headers(method, PREFIX + path)
        r = requests.request(method, BASE + PREFIX + path, headers=h, json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    # ---- public market data ----
    def markets(self, series_ticker, status="open", limit=1000):
        out, cursor = [], None
        while True:
            p = {"series_ticker": series_ticker, "status": status, "limit": limit}
            if cursor:
                p["cursor"] = cursor
            d = self._get_public("/markets", p)
            out.extend(d.get("markets", []))
            cursor = d.get("cursor")
            if not cursor or not d.get("markets"):
                break
        return out

    # ---- portfolio / trading (signed) ----
    def balance(self):
        return self._signed("GET", "/portfolio/balance")

    def place_order(self, ticker, side, count, yes_price_cents, client_order_id, action="buy"):
        """Limit order. yes_price_cents = the YES price in cents (1-99)."""
        body = {
            "ticker": ticker, "action": action, "side": side,
            "type": "limit", "count": count,
            "yes_price": int(yes_price_cents),
            "client_order_id": client_order_id,
        }
        return self._signed("POST", "/portfolio/orders", body)
