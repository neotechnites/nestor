"""
lip_v5.runtime — the ONLY clock, the ONLY logger, the ONLY external-effect seams.

Every external effect (ntfy, HTTP, file write) lives here behind a seam the test suite
stubs.  Two real incidents this week were a unit suite paging a phone and a unit suite
writing outside tmp, so the guards are structural, not procedural:

  * `ntfy()` is a no-op unless BOTH `NTFY_DISABLE` is unset AND `set_live(True)` was called
    by `main()`.  A test process never calls `set_live`, so a test can never page.
  * `http()`/`signed()` raise unless `set_live(True)`.  A test process cannot reach the wire.
  * `atomic_write_json()` refuses any path outside `allowed_write_roots()`, which the test
    runner pins to its tmpdir.

Also re-exports the four symbols the vendored `ws_feed` needs from its host module
(`_now`, `price_str`, `log`, `Auth`) — that is the entire coupling surface, verified by
grep against v4's ws_feed.py before vendoring.
"""

import base64
import json
import os
import sys
import time

from . import config as C

try:
    import requests
except Exception:                                            # pragma: no cover
    requests = None


# =============================================================================================
# THE CLOCK — time.time(), everywhere, always (v4's rule, kept).
# =============================================================================================
def _now():
    return time.time()


def price_str(p):
    return "%.4f" % p


def _utc_day(ts):
    """Integer UTC day index — the rotation and compaction key.  Integer division rather than
    a formatted date so the arithmetic ("older than 7 days") is exact and timezone-free."""
    return int(float(ts) // 86400)


# =============================================================================================
# LIVE SEAM.  DRY/INERT is the DEFAULT; only main() may flip it.
# MIRROR (a test reaching prod ↔ prod silently running inert): the first end is this default;
# the second is that `main()` logs `live=True` on the startup line and `--check` prints it.
# =============================================================================================
_LIVE = False
_WRITE_ROOTS = None


def set_live(flag):
    global _LIVE
    _LIVE = bool(flag)


def is_live():
    return _LIVE


def set_write_roots(roots):
    """Pin every file write under these directories.  The test runner calls this with its
    tmpdir; production calls it with NESTOR_HOME."""
    global _WRITE_ROOTS
    _WRITE_ROOTS = None if roots is None else [os.path.abspath(r) for r in roots]


def allowed_write_roots():
    return list(_WRITE_ROOTS) if _WRITE_ROOTS is not None else None


def _check_write_path(path):
    roots = _WRITE_ROOTS
    if roots is None:
        return
    ap = os.path.abspath(path)
    for r in roots:
        if ap == r or ap.startswith(r + os.sep):
            return
    raise PermissionError("lip_v5 refuses to write outside %s: %s" % (roots, ap))


# =============================================================================================
# LOGGING — one JSONL record per event (v1 §9.1 pattern).
# =============================================================================================
_LOG_SINK = None


def set_log_sink(fn):
    """Tests capture the log instead of printing.  `fn(record_dict)` or None to restore."""
    global _LOG_SINK
    _LOG_SINK = fn


def log(event, **fields):
    rec = {"t": event, "ts": _now()}
    rec.update(fields)
    if _LOG_SINK is not None:
        _LOG_SINK(rec)
        return rec
    try:
        sys.stdout.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        sys.stdout.flush()
    except Exception:                                        # pragma: no cover
        pass
    return rec


_LOGGED_ONCE = set()


def log_once(event, **fields):
    """`log`, but at most once per (event, sorted field values) per process.

    For a condition that is a PROPERTY OF THE CONFIGURATION rather than an event — e.g. a cap
    hierarchy that inverts at the running ceiling.  Such a condition is true on every cycle, so
    plain `log` would emit it 86,400 times a day and the operator would filter it out, which is
    the same as not logging it.  Once is the honest cadence for a standing fact.

    Keyed on the fields as well as the event, so the SAME condition at a DIFFERENT ceiling still
    reports — a config change is new information even when the defect class is not.
    """
    key = (event, tuple(sorted((k, str(v)) for k, v in fields.items())))
    if key in _LOGGED_ONCE:
        return None
    _LOGGED_ONCE.add(key)
    return log(event, **fields)


def reset_log_once():
    """Tests only: a process-lifetime latch would make the second test in a file see nothing."""
    _LOGGED_ONCE.clear()


# =============================================================================================
# ALERTS — spec §11.  NTFY_DISABLE honored BY CONSTRUCTION.
# =============================================================================================
FIXTURE_TICKERS = ("T", "T1", "T2", "M1", "M2", "M3", "M4", "GOOD", "BAD", "PYPL-FIX",
                   "TEST", "FIX", "A", "B", "C", "V1", "V2", "V3")
_ALERT_SINK = None


def set_alert_sink(fn):
    """Tests capture alerts.  `fn(name, message)` or None to restore."""
    global _ALERT_SINK
    _ALERT_SINK = fn


def ntfy(name, message):
    """Page a human.  Three independent guards, all failing CLOSED for alerting only:
    a captured sink (tests), NTFY_DISABLE (env), and the not-live default (structure)."""
    if _ALERT_SINK is not None:
        _ALERT_SINK(name, message)
        return "sink"
    if os.environ.get("NTFY_DISABLE"):
        log("ntfy_disabled", alert=name, message=message)
        return "disabled"
    if not _LIVE:
        log("ntfy_inert", alert=name, message=message)
        return "inert"
    if any((" %s " % t) in (" " + str(message) + " ") for t in FIXTURE_TICKERS):
        log("ntfy_suppressed_fixture", alert=name, message=message)
        return "fixture"
    try:                                                     # pragma: no cover - network
        requests.post("https://ntfy.sh/" + C.NTFY_TOPIC, data=str(message).encode(),
                      headers={"Title": name, "Priority": "urgent"}, timeout=10)
        return "sent"
    except Exception as exc:                                 # pragma: no cover
        log("ntfy_fail", alert=name, err="%s: %s" % (type(exc).__name__, exc))
        return "fail"


# =============================================================================================
# ATOMIC FILE WRITE — spec §5.1 "a SINGLE JSON object, rewritten atomically (temp + rename)".
# MIRROR (a torn write the reader parses ↔ a write that never lands): temp+fsync+rename gives
# the reader either the old object or the new one and never a prefix of the new one; the
# `os.replace` is atomic within a filesystem, which is why the temp file is made in the SAME
# directory as the target rather than in /tmp.
# =============================================================================================
def atomic_write_json(path, obj, fsync=True):
    _check_write_path(path)
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp.%d" % (os.path.basename(path), os.getpid()))
    data = json.dumps(obj, sort_keys=True, default=str)
    with open(tmp, "w") as fh:
        fh.write(data)
        fh.flush()
        if fsync:
            os.fsync(fh.fileno())
    os.replace(tmp, path)
    return len(data)


def append_jsonl(path, obj, fsync=False):
    _check_write_path(path)
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(obj, sort_keys=True, default=str) + "\n")
        if fsync:
            fh.flush()
            os.fsync(fh.fileno())


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.loads(fh.read())
    except Exception:
        return default


def read_jsonl(path):
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    log("jsonl_bad_line", path=path)
    except IOError:
        return out
    return out


# =============================================================================================
# AUTH — v4's signing, kept verbatim on its merits (R166: query string EXCLUDED from the
# signed message; that is the bug that cost v3 a day).
# =============================================================================================
class Auth(object):
    def __init__(self, key_id, private_key):
        self.key_id = key_id
        self.sk = private_key

    def headers(self, method, path):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(_now() * 1000))
        bare = path.split("?", 1)[0]                         # R166
        msg = (ts + method.upper() + C.PREFIX + bare).encode()
        sig = self.sk.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256().digest_size),
            hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "KALSHI-ACCESS-TIMESTAMP": ts}


def load_auth(env_candidates=None):
    """(Auth, note).  Never raises: `--check` must run on a box with no prod key."""
    env_candidates = env_candidates or [
        os.environ.get("NESTOR_ENV_FILE", ""),
        os.path.join(C.NESTOR_HOME, ".env"),
    ]
    key_id = os.environ.get("KALSHI_API_KEY_ID", "").strip()
    pem = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    src = "env"
    if not (key_id and pem):
        for cand in env_candidates:
            if not cand or not os.path.exists(cand):
                continue
            try:
                with open(cand) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        v = v.strip().strip('"').strip("'")
                        if k.strip() == "KALSHI_API_KEY_ID" and not key_id:
                            key_id = v
                        elif k.strip() == "KALSHI_PRIVATE_KEY_PATH" and not pem:
                            pem = v
                src = cand
            except Exception as exc:
                return None, "env read failed (%s): %s" % (cand, exc)
            if key_id and pem:
                break
    if not key_id:
        return None, "no KALSHI_API_KEY_ID found"
    pem = os.path.expanduser(pem) if pem else ""
    # nestor's .env stores the key path RELATIVE to the nestor dir; resolve against the
    # .env's own directory, not the systemd WorkingDirectory (v3 lesson).
    if pem and not os.path.isabs(pem) and src != "env" and os.path.exists(src):
        pem = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(src)), pem))
    if not pem or not os.path.exists(pem):
        return None, "private key not found at %r" % pem
    try:
        from cryptography.hazmat.primitives import serialization
        with open(pem, "rb") as fh:
            sk = serialization.load_pem_private_key(fh.read(), password=None)
    except Exception as exc:
        return None, "key load failed: %s" % exc
    return Auth(key_id, sk), "loaded from %s (key %s...)" % (src, key_id[:8])


# =============================================================================================
# HTTP — refuses unless live.  A test process cannot reach the wire.
# =============================================================================================
_SESSION = None


def _session():
    global _SESSION
    if not _LIVE:
        raise RuntimeError("lip_v5 runtime is INERT: no network call may be made")
    if requests is None:                                     # pragma: no cover
        raise RuntimeError("requests unavailable")
    if _SESSION is None:                                     # pragma: no cover
        _SESSION = requests.Session()
    return _SESSION


def http(method, url, headers=None, body=None, params=None):   # pragma: no cover - network
    try:
        resp = _session().request(method, url, headers=headers, json=body, params=params,
                                  timeout=C.HTTP_TIMEOUT)
    except Exception as exc:
        return 0, {"_transport_error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        return resp.status_code, (resp.json() if resp.content else {})
    except ValueError:
        return resp.status_code, {"_text": resp.text[:500]}


def signed(auth, method, path, body=None, params=None):        # pragma: no cover - network
    return http(method, C.BASE + C.PREFIX + path, headers=auth.headers(method, path),
                body=body, params=params)


def signed_v1(auth, method, path, body=None, params=None):     # pragma: no cover - network
    """Sign a RAW /v1 (web-API) path — no /trade-api/v2 prefix in the URL or the message.
    CAPTURED 2026-07-30: the trading key's RSA-PSS signature is honored on /v1 — verified
    200 on /v1/incentives/users/{id}/estimates, the per-program accrued-rewards feed the
    trade API does not serve (23 paths probed 404).  The signature message is
    ts + METHOD + path, same scheme, different prefix."""
    import base64 as _b64
    import time as _t
    from cryptography.hazmat.primitives import hashes as _h
    from cryptography.hazmat.primitives.asymmetric import padding as _p
    ts = str(int(_t.time() * 1000))
    sig = auth.sk.sign((ts + method.upper() + path).encode(),
                       _p.PSS(mgf=_p.MGF1(_h.SHA256()),
                              salt_length=_p.PSS.DIGEST_LENGTH), _h.SHA256())
    hdrs = {"KALSHI-ACCESS-KEY": auth.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": _b64.b64encode(sig).decode()}
    return http(method, C.BASE + path, headers=hdrs, body=body, params=params)


def public_get(path, params=None):                             # pragma: no cover - network
    return http("GET", C.BASE + C.PREFIX + path, params=params)


# =============================================================================================
# ORDER CONSTRUCTION (v1 §4.7's exact V2 resting-order body, and the coid rules that survived)
# =============================================================================================
def sanitize(s):
    """R167: Kalshi 400s any client_order_id containing a dot.  '.' → '_' AT CONSTRUCTION."""
    return str(s).replace(".", "_")


def make_coid(ticker, side, seq):
    """spec §6.1 — `v5-lipm-{ticker}-{y|n}-{seq}`.  NO run-id: the restart sweep must
    recognise the PREVIOUS process's orders (v1 §9.5; a run-id in the prefix is v3's loss)."""
    yn = "y" if side == "bid" else "n"
    return sanitize("%slipm-%s-%s-%d" % (C.COID_PREFIX, ticker, yn, int(seq)))


def owns_coid(coid):
    """Prefix only, so it spans restarts.  Disjoint from v4- and from nestor's by
    construction (spec §11 Collisions)."""
    return isinstance(coid, str) and coid.startswith(C.COID_PREFIX)


def order_body(ticker, side, price_dollars, expiration_ts, coid, count):
    """v1 §4.7 — the v3-proven V2 body.  `expiration_ts` = close − CLOSE_MARGIN_S backstops
    every order (spec §11 Schedule); STP `taker_at_cross` on every order (spec §11
    Collisions)."""
    return {
        "ticker": ticker,
        "side": side,                                        # bid = buy YES, ask = sell YES
        "count": "%.2f" % count,
        "price": price_str(price_dollars),                   # 4-dp dollars, YES axis
        "time_in_force": "good_till_canceled",
        "expiration_ts": int(expiration_ts),
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": coid,
    }


def unit_collateral(side, price_dollars):
    """Collateral per contract on the single YES book (v3-proven).
    "bid" = buy YES at p → p;  "ask" = sell YES at p = buy NO at (1−p) → 1−p."""
    return float(price_dollars) if side == "bid" else (1.0 - float(price_dollars))
