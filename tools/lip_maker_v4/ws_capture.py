#!/usr/bin/env python3
"""
ws_capture.py — capture RAW Kalshi websocket frames, verbatim, and exit.

WHY THIS EXISTS.  First live contact: `ws_connect` OK (so the signed path is right) and
`ws_subscribed` OK (so the server ACKED the subscription) — but ZERO snapshot/delta frames in
3+ minutes.  An ack with no data has exactly two causes, and they need OPPOSITE fixes:

  (A) the subscribe PARAMS are wrong — the server acked a subscription that binds nothing;
  (B) the frames ARE arriving and our reader does not RECOGNISE them — a different `type`
      string, a different envelope, or the payload under a different key.

`ws_feed.handle_frame()` cannot tell these apart, because anything it fails to recognise it
drops.  So this script parses NOTHING: it writes every byte the socket produces, and it tries
several subscribe shapes on separate connections so the answer to (A) is a table rather than a
guess.

READ-ONLY.  No orders, no cancels, no writes to the ledger.  It touches only its own output
file and exits.

USAGE (on the VPS)
  python3 ws_capture.py                        # auto-picks live tickers, tries every variant
  python3 ws_capture.py --variant A --seconds 60
  python3 ws_capture.py --tickers KXAAAGASD-26JUL29-4.100,KXAAAGASD-26JUL29-4.105
  python3 ws_capture.py --frames 200 --out ~/nestor/data/lip/ws_raw_frames.jsonl

OUTPUT  ~/nestor/data/lip/ws_raw_frames.jsonl — one JSON record per line:
  {"n":1,"ts":...,"variant":"A","dir":"send","raw":"{...}"}      the exact subscribe sent
  {"n":2,"ts":...,"variant":"A","dir":"recv","raw":"{...}"}      verbatim, unparsed
Paste the first ~20 lines and the printed SUMMARY table back for round 2.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lip_maker_v4 as M
import ws_feed as W

try:
    import websockets
except Exception:
    websockets = None

import asyncio


DEFAULT_OUT = os.path.join(M.DATA_DIR, "ws_raw_frames.jsonl")


# =============================================================================================
# SUBSCRIBE VARIANTS — the (A) hypothesis, made falsifiable.
# Each is a documented or plausible shape.  They run on SEPARATE connections so one rejection
# cannot suppress another's data, and the summary reports which produced data frames.
# =============================================================================================
def variants(tickers):
    one = tickers[:1]
    return [
        ("A", "current: channels[] + market_tickers[] (all)",
         {"id": 1, "cmd": "subscribe",
          "params": {"channels": ["orderbook_delta"], "market_tickers": list(tickers)}}),
        ("B", "single ticker in market_tickers[]",
         {"id": 1, "cmd": "subscribe",
          "params": {"channels": ["orderbook_delta"], "market_tickers": list(one)}}),
        ("C", "singular market_ticker (string)",
         {"id": 1, "cmd": "subscribe",
          "params": {"channels": ["orderbook_delta"], "market_ticker": one[0] if one
                     else ""}}),
        ("D", "channel as a STRING, not a list",
         {"id": 1, "cmd": "subscribe",
          "params": {"channel": "orderbook_delta", "market_tickers": list(one)}}),
        ("E", "no tickers at all (server-side firehose / rejection probe)",
         {"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"]}}),
        ("F", "ticker (singular key name used by some Kalshi endpoints)",
         {"id": 1, "cmd": "subscribe",
          "params": {"channels": ["orderbook_delta"], "ticker": one[0] if one else ""}}),
    ]


def pick_tickers(n=8):
    """Live, open, two-sided markets — a subscription bound to a dead market proves nothing.
    Prefers the gas dailies (the pilot venue) and falls back to whatever the scanner ranks."""
    try:
        progs = M.scan_programs(cache=False)
    except Exception as exc:
        print("scanner failed (%s); pass --tickers explicitly" % exc)
        return []
    gas = [p for p in progs if p["series"] == "KXAAAGASD"]
    pool = gas or progs
    pool = sorted(pool, key=lambda p: str(p["market_ticker"]))
    out = []
    for p in pool:
        tk = p["market_ticker"]
        try:
            st, body = M.public_get("/markets/%s/orderbook" % tk, {"depth": "5"})
            if st != 200:
                continue
            yb, ya = M.best_from_book(body)
            if yb is None or ya is None:
                continue                      # one-sided: least likely to produce traffic
        except Exception:
            continue
        out.append(tk)
        if len(out) >= n:
            break
    return out


async def capture_one(url, headers, sub, seconds, frames, sink, tag):
    """One connection, one subscribe shape.  Returns (n_recv, types_seen, error)."""
    types, n_recv, err = {}, 0, None
    try:
        conn = await asyncio.wait_for(
            websockets.connect(url, additional_headers=headers,
                               open_timeout=W.WS_OPEN_TIMEOUT_S,
                               close_timeout=W.WS_CLOSE_TIMEOUT_S),
            timeout=W.WS_OPEN_TIMEOUT_S + 5)
    except TypeError:
        # older websockets uses extra_headers
        conn = await asyncio.wait_for(
            websockets.connect(url, extra_headers=headers,
                               open_timeout=W.WS_OPEN_TIMEOUT_S,
                               close_timeout=W.WS_CLOSE_TIMEOUT_S),
            timeout=W.WS_OPEN_TIMEOUT_S + 5)
    except Exception as exc:
        return 0, {}, "%s: %s" % (type(exc).__name__, exc)
    try:
        payload = json.dumps(sub)
        sink({"variant": tag, "dir": "send", "raw": payload})
        await conn.send(payload)
        deadline = time.time() + seconds
        while time.time() < deadline and n_recv < frames:
            try:
                raw = await asyncio.wait_for(conn.recv(),
                                             timeout=max(0.5, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            except Exception as exc:
                err = "%s: %s" % (type(exc).__name__, exc)
                break
            n_recv += 1
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8", "replace")
                except Exception:
                    raw = repr(raw)
            sink({"variant": tag, "dir": "recv", "raw": raw})
            # type histogram ONLY — the record on disk stays verbatim
            t = "<unparseable>"
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    t = str(obj.get("type") or obj.get("event")
                            or "<no type key: %s>" % sorted(obj.keys())[:6])
            except Exception:
                pass
            types[t] = types.get(t, 0) + 1
    finally:
        try:
            await conn.close()
        except Exception:
            pass
    return n_recv, types, err


async def run(args, tickers, out_path):
    n = [0]
    fh = open(out_path, "a")

    def sink(rec):
        n[0] += 1
        rec = dict(rec)
        rec["n"] = n[0]
        rec["ts"] = time.time()
        fh.write(json.dumps(rec) + "\n")
        fh.flush()

    auth, note = M.load_auth()
    print("auth: %s" % note)
    headers = W.ws_auth_headers(auth)
    if not headers:
        print("WARNING: no signed headers — the handshake will be unauthenticated")
    chosen = [v for v in variants(tickers)
              if args.variant in ("all", v[0])]
    if not chosen:
        print("unknown --variant %s" % args.variant)
        return 2
    results = []
    for tag, desc, sub in chosen:
        print("\n--- variant %s: %s" % (tag, desc))
        print("    subscribe: %s" % json.dumps(sub)[:200])
        # a fresh signature per connection: the timestamp is part of the signed message
        headers = W.ws_auth_headers(auth)
        got, types, err = await capture_one(W.WS_URL, headers, sub, args.seconds,
                                            args.frames, sink, tag)
        data = sum(c for t, c in types.items()
                   if "orderbook" in t or "snapshot" in t or "delta" in t)
        results.append((tag, got, data, types, err))
        print("    frames=%d  DATA frames=%d  err=%s" % (got, data, err))
        for t, c in sorted(types.items(), key=lambda kv: -kv[1]):
            print("      %-46s %d" % (t[:46], c))
        if data and args.stop_on_data:
            print("    >>> this variant produces DATA — stopping here")
            break
    fh.close()

    print("\n" + "=" * 78)
    print("SUMMARY   (raw frames appended to %s)" % out_path)
    print("%-4s %-8s %-8s %s" % ("var", "frames", "data", "note"))
    for tag, got, data, types, err in results:
        note = err or ("DATA OK" if data else
                       "acked but silent" if got else "nothing at all")
        print("%-4s %-8d %-8d %s" % (tag, got, data, note))
    winners = [t for t, g, d, _, _ in results if d]
    print("\nverdict: %s" % (
        "variant(s) %s produce data -> the SUBSCRIBE PARAMS were the problem (cause A)"
        % ", ".join(winners) if winners else
        "NO variant produced data frames -> either every shape is wrong, or the account/"
        "markets produce no traffic; paste the raw file so the frames themselves can be read"
        " (cause B)"))
    print("=" * 78)
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description="capture raw Kalshi ws frames (read-only)")
    ap.add_argument("--variant", default="all",
                    help="A|B|C|D|E|F|all (default all)")
    ap.add_argument("--seconds", type=float, default=25.0,
                    help="seconds to listen per variant (default 25)")
    ap.add_argument("--frames", type=int, default=200,
                    help="max frames to capture per variant (default 200)")
    ap.add_argument("--tickers", default="",
                    help="comma-separated; default = auto-pick live two-sided markets")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--stop-on-data", action="store_true",
                    help="stop at the first variant that yields data frames")
    args = ap.parse_args(argv[1:])

    if websockets is None:
        print("FATAL: the `websockets` library is not installed.\n"
              "       pip3 install --user websockets")
        return 3
    print("websockets %s" % getattr(websockets, "__version__", "?"))

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        print("picking live two-sided markets ...")
        tickers = pick_tickers()
    if not tickers:
        print("FATAL: no tickers; pass --tickers")
        return 4
    print("tickers (%d): %s" % (len(tickers), ", ".join(tickers[:6])
                                + (" ..." if len(tickers) > 6 else "")))

    out_path = os.path.expanduser(args.out)
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
    except Exception:
        pass
    print("writing raw frames to %s" % out_path)
    try:
        return asyncio.run(run(args, tickers, out_path))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv))
