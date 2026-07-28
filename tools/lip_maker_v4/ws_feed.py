#!/usr/bin/env python3
"""
ws_feed — Kalshi `orderbook_delta` websocket book feed for `lip_maker_v4` (spec §4.6).

§4.6 is the only place in the spec where breadth is capped by TRANSPORT rather than by the
money: "1 Hz REST book polls x N markets against a ~10 req/s shared budget => max 6 markets
on REST.  Breadth past 6 REQUIRES the websocket `orderbook_delta` subscription — implement
WS first-class; REST 1 Hz is the degraded fallback with an automatic clamp to 6 markets."
This file is that subscription.  It is additive: `lip_maker_v4.py` is not modified, and with
this file absent, unimportable, or the `websockets` library uninstalled the consumer keeps
its REST path unchanged.

=============================================================================================
THE ONE CORRECTNESS PROPERTY
=============================================================================================
A delta feed is a stateful reconstruction of somebody else's data structure.  If one delta is
lost, every subsequent quote is priced off a book that is CONFIDENTLY WRONG — which is
strictly worse than having no book at all, because the REST fallback would have been correct.
Therefore, in this file:

  * a `seq` gap on a DELTA is unrecoverable.  The book is unknowable.  Drop it, resubscribe,
    and report the market as not-fresh so the consumer falls back to REST (§4.6's clamp to 6).
  * a delta whose resulting level size would be NEGATIVE is corruption, never a clamp to
    zero.  A silent `max(0, size)` is exactly the bug that produces a wrong-but-plausible
    book, so the level is not repaired: the whole market's book is discarded.
  * a delta for a market we hold no snapshot for is IGNORED, never applied to an empty book.
    Half a book looks like a thin book, and a thin book is a buy signal to §2's allocator.
  * every failure path degrades to "no fresh books".  Nothing in this module raises into the
    caller.

=============================================================================================
CONCURRENCY MODEL — background thread, not `pump()`.  Derived.
=============================================================================================
The two options were (a) a non-blocking `pump()` the 1 Hz `Maker.cycle()` calls, and (b) a
background reader thread publishing a lock-protected book state.  (b) wins on one argument:
`pump()` would make the feed's drain rate equal to the CONSUMER's cycle rate.  `Maker.cycle()`
is not a fixed-cost loop — it posts and cancels orders inside the same pass, and each of those
is an HTTP round trip of up to HTTP_TIMEOUT (15 s).  A cycle that stalls 15 s on a slow POST
would leave 15 s of `orderbook_delta` frames in the socket buffer, and the very next `pump()`
would apply a 15-second-old burst as if it were current — or, at `max_queue`, the library
would drop frames and manufacture the exact seq gap this module exists to detect.  Decoupling
the drain from the cycle is therefore not an optimisation, it is what keeps the gap detector
measuring the EXCHANGE's losses rather than our own scheduling.  The reader is an asyncio loop
(the `websockets` client is asyncio-first and present in every version) run by
`asyncio.run()` on one daemon thread; book state lives behind a single `RLock`, and the
accessors hand back freshly built dicts so no mutable state escapes the lock.

=============================================================================================
INTEGRATION — the seam, in `Maker.cycle()`.  Three lines.
=============================================================================================
Inside `Maker.cycle()`, immediately after `progs` is computed and before the poll loop:

    ws   = ws_feed.attach(self, self.auth, [p["market_ticker"] for p in progs])   # idempotent
    cap  = MAX_WS_MARKETS if ws.health()["connected"] else MAX_REST_MARKETS       # §4.6 clamp
    body = ws_feed.ws_book_or_none(tk)      # inside the per-ticker loop; None => fall to REST

with the existing REST call becoming the fallback arm of the third line:

    if body is None:
        st, body = public_get("/markets/%s/orderbook" % tk, {"depth": "50"})
        if st != 200: ...                                    # unchanged
    ...
    chosen = [t for t in market_poll_rank(self.classified, max_markets=cap) if t in by_ticker]

`attach()` is idempotent and cheap: the first call starts the reader thread, every later call
just republishes the desired ticker set (and resubscribes only if it actually changed).  The
consumer needs no other change: `ws_book_or_none()` returns EXACTLY the dict shape
`book_levels()` / `best_from_book()` already parse, or `None`.

Python 3.12 stdlib + `websockets` (this file only).  `websockets` is imported defensively;
with it absent every entry point still works and simply reports "not connected", so
`python3 -m unittest discover tools/lip_maker_v4/` passes on a box with no network and no
`websockets` installed.
"""

import asyncio
import base64
import json
import random
import threading

import lip_maker_v4 as M
from lip_maker_v4 import Auth                    # noqa: F401  (re-exported; see ws_auth_headers)

try:                                             # tests must import with no websockets, no net
    import websockets
except Exception:                                # pragma: no cover
    websockets = None


# =============================================================================================
# CONFIG — every constant carries its derivation.  A constant without a derivation is an
# undeclared claim (note 23 §II) and must not exist in this block.
# =============================================================================================

# ---- endpoint / protocol ----------------------------------------------------------------
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"    # trade-api v2 websocket
WS_SIGNED_PATH = "/trade-api/ws/v2"     # the path the auth signature covers.  NOT
                                        # lip_maker's PREFIX + something: see ws_auth_headers.
WS_CHANNEL = "orderbook_delta"          # §4.6 names this channel explicitly
WS_METHOD = "GET"                       # the handshake is an HTTP GET upgrade

# ---- THE BREADTH CAP (§4.6) --------------------------------------------------------------
# §4.6 derives MAX_REST_MARKETS = 6 from "1 Hz book poll x N markets vs a ~10 req/s shared
# budget".  WS deletes that term outright: N markets cost ONE subscribe command on a socket
# that is not on the REST budget at all.  The WS cap is therefore set by what REST traffic
# REMAINS, plus a sanity check that the feed can physically keep up.
#
#   Leg 1 — residual REST, the binding one.
#     Budget                                                            10.000 req/s (§4.6)
#     less the largest burst this binary makes: the classification sweep
#     runs at CLASSIFY_RATE_HZ = 5.0 and must never be throttled by the
#     quoting path, so it is reserved outright                          -5.000 req/s
#     ---------------------------------------------------------------------------------
#     sustained budget for the quoting path                              5.000 req/s
#
#     Per-market sustained REST cost with the book poll gone:
#       requotes: 2 sides x (POST + DELETE) per requote (§4.1 make-before-break), at the
#                 FASTEST rate §4.4's 30 s minimum resting life permits
#                 = 2 * 2 / 30                                           0.1333 req/s/market
#       settlement / market truth reads at CLASSIFY_REFRESH_S = 900 s
#                 = 1 / 900                                              0.0011 req/s/market
#       ---------------------------------------------------------------------------------
#       worst case                                                       0.1344 req/s/market
#
#     5.000 / 0.1344 = 37.2 markets.
#
#   Leg 2 — feed throughput, checked, not binding.  At 32 markets and an observed <=5 book
#   events/s on an active Kalshi rung that is <=160 frames/s; one thread doing json.loads +
#   a dict update runs ~1e4 frames/s, i.e. 60x headroom.  Message rate is not the constraint.
#
#   Leg 3 — the round-down.  37 is a WORST-CASE-derived number: it assumes every slot on
#   every market requotes as fast as anti-gaming permits, forever.  Two things are not in it:
#   (a) a market-wide move can requote every slot in the same second, a burst above the
#   30 s-averaged rate; (b) Kalshi's per-connection subscription limit is UNVERIFIED (flagged
#   for live-traffic confirmation).  32 keeps ~14% margin on leg 1 and is a round number to
#   resubscribe in one frame.  At the MEASURED requote rate (best changes ~20%/45 s => ~1/225
#   per side per second, §4.2) the same arithmetic gives 263 markets, so 32 carries ~8x
#   realised headroom.  This is the poll/observe cap only; how many markets get FUNDED is
#   ALLOCATE's answer (§2), not this number's.
MAX_WS_MARKETS = 32                     # 5.3x the §4.6 REST clamp of 6

# ---- staleness (§4.5 coverage budget) ----------------------------------------------------
# How long may we quote off the last book we saw?  §4.2 measures the same-side best changing
# at ~20% per 45 s, i.e. a Poisson rate of 1/225 s per side.  The probability the best moved
# during B seconds of blindness is 1 - exp(-B/225); §4.5's coverage target is >=95%, so the
# whole blindness budget is 5%:  B <= -225 * ln(0.95) = 11.54 s.  Round DOWN to 10 s.
WS_MAX_BOOK_AGE_S = 10.0
# A quiet market is not a stale market.  An illiquid LIP rung can legitimately emit no delta
# for minutes, and staling it would hand the whole breadth gain back to the REST clamp.  What
# proves a quiet book is still CURRENT is the socket being alive, and what proves the socket
# is alive is the library's own PING/PONG keepalive: with these two settings a peer that stops
# answering is torn down within PING_INTERVAL + PING_TIMEOUT = 10 s, matching the 11.5 s
# blindness budget above.  On teardown `connected` goes False and books_for() returns {}
# IMMEDIATELY -- staleness is not a second 10 s on top of the detection latency.
WS_PING_INTERVAL_S = 5.0
WS_PING_TIMEOUT_S = 5.0
# Reader wake cadence.  The loop refreshes its liveness stamp at least this often, so while
# connected the liveness term is never what stales a book: 2.0 s is 5x under WS_MAX_BOOK_AGE_S.
# It is also the granularity at which a `stop()` is noticed.
WS_RECV_TIMEOUT_S = 2.0
# Forced re-snapshot.  The one failure this design cannot otherwise see is a subscription
# silently dropped behind a socket that is still answering pings: books would freeze while
# looking fresh.  A full resubscribe costs a ~1 s book blackout on every subscribed market
# (they fall back to REST-6 for that second), so at 300 s the blackout duty cycle is <=0.33%
# -- an order of magnitude inside §4.5's 5% coverage budget -- while bounding the exposure to
# a frozen book at 300 s.  It is the WS analogue of §4.3(e)'s 60 s "safety re-sync regardless".
WS_RESNAPSHOT_S = 300.0

# ---- reconnect backoff -------------------------------------------------------------------
# Base 1.0 s: the consumer evaluates triggers at BOOK_POLL_HZ = 1 Hz, so reconnecting faster
# than 1 Hz cannot deliver a book any earlier than the consumer can use one, and a sub-second
# retry loop against a down exchange is an abusive pattern in its own right.
WS_BACKOFF_BASE_S = 1.0
WS_BACKOFF_MULT = 2.0                   # doubling: halves our request rate on each failure
# Cap 30 s.  While disconnected the consumer is on REST clamped to 6 markets (§4.6) -- it
# loses BREADTH, never PRESENCE, so a long backoff is cheap.  30 s is half §4.3(e)'s 60 s
# safety re-sync, so at worst ONE re-sync cycle runs REST-only before the feed is back.
WS_BACKOFF_CAP_S = 30.0
# +/-25% multiplicative jitter.  This process is a single client, so jitter is NOT for
# thundering-herd: it is to de-phase our retry ladder from the exchange's own restart cadence,
# which a fixed 1/2/4/8/16 ladder can otherwise land inside on every attempt.  25% is the
# smallest spread that de-phases within 3 attempts while leaving the expected delay unchanged.
# The cap is applied AFTER the jitter so WS_BACKOFF_CAP_S is a true hard bound.
WS_BACKOFF_JITTER = 0.25

# ---- connection hygiene ------------------------------------------------------------------
WS_OPEN_TIMEOUT_S = 15.0                # matches lip_maker's HTTP_TIMEOUT: same exchange,
WS_CLOSE_TIMEOUT_S = 5.0                # same patience.  Close is a courtesy, not a gate.
# Inbound queue.  At leg 2's 160 frames/s a 1024-frame queue is ~6 s of buffer, comfortably
# longer than any pause the reader can take (WS_RECV_TIMEOUT_S = 2 s) and short enough that a
# genuinely wedged reader trips the library's overflow -- which surfaces as a disconnect, i.e.
# the safe direction, rather than as unbounded memory growth.
WS_MAX_QUEUE = 1024

# ---- book encoding -----------------------------------------------------------------------
# The integration contract: `to_orderbook_body()` must emit what `book_levels()` /
# `best_from_book()` already parse.  Those call `_levels_cents`, which does
# `int(round(float(price) * 100))` -- i.e. the price field is DOLLARS, and lip_maker's own
# wire format for a price is `price_str()` = "%.4f".  Reuse it rather than restate it.
WS_SIZE_FMT = "%.2f"                    # Kalshi *_fp size fields are 2-dp strings (v3-proven)


# =============================================================================================
# AUTH — the SAME signing shape as lip_maker_v4.Auth.headers(), over the WS path.
# =============================================================================================
def ws_auth_headers(auth, path=WS_SIGNED_PATH, method=WS_METHOD, ts_ms=None):
    """Signed handshake headers for the trade-api v2 websocket.

    WHY THIS IS NOT `Auth.headers()` CALLED DIRECTLY, stated plainly because forking crypto
    is the last thing anyone should do quietly:  `Auth.headers(method, path)` signs
    `ts + METHOD + PREFIX + path`, with `PREFIX` HARDCODED to the REST prefix
    "/trade-api/v2".  The websocket path is "/trade-api/ws/v2" -- the "ws" sits INSIDE the
    prefix, so no value of `path` can make `PREFIX + path` produce it.  The only way to reuse
    `Auth.headers()` verbatim would be to swap the module global `M.PREFIX` around the call,
    and `Maker` signs REST requests from another thread: that swap is a live race that would
    intermittently sign a REST call with the WS prefix and get a 401 mid-quote.  So the
    MESSAGE is built here and the SIGNATURE is delegated to the same key object with the same
    PSS/MGF1-SHA256/digest-length-salt parameters.  `test_ws_feed.py` asserts byte-for-byte
    that this function's message differs from `Auth.headers()`'s in the path segment ALONE and
    that the padding and hash objects are structurally identical -- so a change to lip_maker's
    signing that this file failed to follow fails a test rather than a live handshake.

    Never raises: with no key, no `cryptography`, or an unusable key it returns {} and the
    connection is attempted unauthenticated, which fails cleanly into the reconnect path.
    """
    if auth is None or getattr(auth, "sk", None) is None:
        return {}
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int((M._now() if ts_ms is None else float(ts_ms) / 1000.0) * 1000))
        bare = str(path).split("?", 1)[0]                 # R166: query string EXCLUDED
        msg = (ts + str(method).upper() + bare).encode()
        sig = auth.sk.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256().digest_size),
            hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": auth.key_id,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "KALSHI-ACCESS-TIMESTAMP": ts}
    except Exception:                                     # never raise into the reader
        return {}


# =============================================================================================
# PURE — reconnect backoff
# =============================================================================================
def backoff_delay(attempt, rng=None, base=WS_BACKOFF_BASE_S, mult=WS_BACKOFF_MULT,
                  cap=WS_BACKOFF_CAP_S, jitter=WS_BACKOFF_JITTER):
    """Seconds to wait before reconnect attempt `attempt` (0-based).  Bounded and jittered.

    `0 < delay <= cap` for EVERY attempt and every value `rng()` can return -- the cap is
    applied after the jitter precisely so that bound is exact and not `cap * (1 + jitter)`.
    """
    r = random.random if rng is None else rng
    try:
        k = max(0, int(attempt))
    except (TypeError, ValueError):
        k = 0
    raw = base * (mult ** min(k, 64))                     # min(): no overflow on a stuck loop
    raw = min(raw, cap)
    try:
        u = float(r())
    except Exception:
        u = 0.5
    factor = 1.0 + jitter * (2.0 * min(max(u, 0.0), 1.0) - 1.0)
    return max(1e-3, min(raw * factor, cap))


# =============================================================================================
# PURE — per-market book state machine
# =============================================================================================
class BookState(object):
    """One market's reconstructed orderbook, in the exchange's own units (price in CENTS,
    size in contracts), on both sides quoted as BIDS in their own currency -- which is the
    same convention `best_from_book()` already assumes ("yes_ask = 100 - best no bid").

    `corrupt` is a one-way latch.  Nothing in this class repairs a book; a corrupt book is
    for the feed to DISCARD, because the only honest recovery from an inconsistent delta
    stream is a fresh snapshot.
    """

    __slots__ = ("ticker", "yes", "no", "last_update_ts", "has_snapshot", "corrupt",
                 "corrupt_reason", "snapshots", "deltas")

    def __init__(self, ticker):
        self.ticker = ticker
        self.yes = {}                   # price_cents -> size
        self.no = {}
        self.last_update_ts = 0.0
        self.has_snapshot = False
        self.corrupt = False
        self.corrupt_reason = ""
        self.snapshots = 0
        self.deltas = 0

    # -- mutation ------------------------------------------------------------------------
    def apply_snapshot(self, msg, now):
        """Replace the book WHOLESALE.  A snapshot is a total statement about the book, not a
        merge: any level the exchange no longer lists is gone, and merging would resurrect it.
        A snapshot also CLEARS `corrupt` -- it is the one thing that can, since it depends on
        no prior state.  Returns "ok", or "bad_snapshot" if the frame is unusable (in which
        case nothing is mutated: a half-applied snapshot is worse than no snapshot)."""
        if not isinstance(msg, dict):
            return "bad_snapshot"
        yes = _levels_from_wire(msg.get("yes"))
        no = _levels_from_wire(msg.get("no"))
        if yes is None or no is None:
            return "bad_snapshot"
        self.yes = yes
        self.no = no
        self.has_snapshot = True
        self.corrupt = False
        self.corrupt_reason = ""
        self.last_update_ts = now
        self.snapshots += 1
        return "ok"

    def apply_delta(self, msg, now):
        """Mutate ONE price level by a SIGNED size change.

        Returns "ok" | "no_base" | "bad_delta" | "corrupt".

          * a level whose resulting size is <= 0 is REMOVED.  Exactly 0 is the normal way a
            level dies and is not an error.
          * a resulting size < 0 is CORRUPTION and is NOT clamped.  `max(0, ...)` here would
            convert a provably-lost message into a book that is merely wrong, and a wrong
            book prices confident quotes -- the whole failure this module exists to prevent.
            The book is latched corrupt and left untouched for the feed to discard.
          * a delta arriving with no snapshot underneath it returns "no_base" and mutates
            NOTHING.  Applying it to an empty dict would synthesise a one-level book, and a
            one-level book reads to §1.2 as a thin book, which is a buy signal.
        """
        if not isinstance(msg, dict):
            return "bad_delta"
        side = msg.get("side")
        book = self.yes if side == "yes" else (self.no if side == "no" else None)
        if book is None:
            return "bad_delta"
        try:
            price = int(round(float(msg["price"])))
            delta = float(msg["delta"])
        except (KeyError, TypeError, ValueError):
            return "bad_delta"
        if not self.has_snapshot or self.corrupt:
            return "no_base"
        new = book.get(price, 0.0) + delta
        if new < -1e-9:
            self.corrupt = True
            self.corrupt_reason = "negative_size %s %s@%d -> %.4f" % (
                self.ticker, side, price, new)
            return "corrupt"
        if new <= 1e-9:
            book.pop(price, None)
        else:
            book[price] = new
        self.last_update_ts = now
        self.deltas += 1
        return "ok"

    # -- read ----------------------------------------------------------------------------
    def is_stale(self, now, max_age_s=WS_MAX_BOOK_AGE_S):
        """A book with no snapshot, or a corrupt one, is stale by definition -- there is no
        age at which "we never had it" becomes fresh."""
        if self.corrupt or not self.has_snapshot:
            return True
        return (now - self.last_update_ts) > max_age_s

    def to_orderbook_body(self):
        """THE INTEGRATION CONTRACT.  Exactly the dict `book_levels()` and `best_from_book()`
        already consume from the REST `/markets/{t}/orderbook` response, so the consumer needs
        no change:

            {"orderbook": {"orderbook_fp": {"yes_dollars": [[price, size], ...],
                                            "no_dollars":  [[price, size], ...]}}}

        Prices are 4-dp DOLLAR strings via lip_maker's own `price_str()` (its parsers do
        `int(round(float(price) * 100))`).  Levels are emitted best-first; both consumers are
        order-independent (`best_from_book` takes a max, `score_side` sorts) so the ordering
        is for humans reading the ledger, not for correctness.
        """
        return {"orderbook": {"orderbook_fp": {
            "yes_dollars": _levels_to_wire(self.yes),
            "no_dollars": _levels_to_wire(self.no)}}}


def _levels_from_wire(rows):
    """[[price_cents, size], ...] -> {price_cents: size}.  None on anything unusable, so the
    caller can refuse the whole snapshot rather than accept a partial one.  A missing side is
    an EMPTY side (a legally empty book), not an error."""
    if rows is None:
        return {}
    if not isinstance(rows, (list, tuple)):
        return None
    out = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            return None
        try:
            price = int(round(float(row[0])))
            size = float(row[1])
        except (TypeError, ValueError):
            return None
        if size > 0:
            out[price] = out.get(price, 0.0) + size
    return out


def _levels_to_wire(book):
    return [[M.price_str(p / 100.0), WS_SIZE_FMT % s]
            for p, s in sorted(book.items(), key=lambda kv: -kv[0])]


# =============================================================================================
# PURE — sequence tracking.  THE most important class in the file (§4.6 / this module's §1).
# =============================================================================================
SEQ_OK = "ok"
SEQ_GAP = "gap"
SEQ_DUPLICATE = "duplicate"
SEQ_RESET = "reset"


class SeqTracker(object):
    """`seq` is per-subscription (per `sid`) and MONOTONIC.  A gap means messages were lost.

    Verdicts, and why each one is what it is:

      "ok"        first message on a sid (any starting value -- we do not assume 1), or
                  exactly `last + 1`.  Recorded.
      "duplicate" exactly `last`.  A retransmit.  IGNORED rather than reapplied, because
                  applying a delta twice is not idempotent: `+5` twice is `+10`.
      "gap"       `seq > last + 1`.  Messages were lost; the book is unknowable.  NOT
                  recorded -- recording is a policy decision the FEED makes via `rebase()`
                  once it has dropped the book, so that one gap costs exactly one resubscribe
                  instead of storming.
      "reset"     `seq < last` on a sid we ARMED via `arm_reset()` immediately before sending
                  a resubscribe.  The stream restarted its counter; accept and rebase.

    An UNARMED backwards jump is deliberately reported as "gap", not "duplicate".  Delivery on
    a websocket is ordered, so an unexplained rewind is not a late message -- it is a stream
    we no longer understand, and the safe reading of "we no longer understand this stream" is
    identical to the safe reading of a forward gap: throw the book away.  Calling it
    "duplicate" would IGNORE real deltas and let the book drift silently, which is precisely
    the confidently-wrong-book failure.
    """

    __slots__ = ("last", "armed")

    def __init__(self):
        self.last = {}
        self.armed = set()

    def check(self, sid, seq):
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            return SEQ_GAP                       # an unreadable seq is a lost seq
        last = self.last.get(sid)
        if last is None:
            self.last[sid] = seq
            self.armed.discard(sid)
            return SEQ_OK
        if seq == last + 1:
            self.last[sid] = seq
            return SEQ_OK
        if seq == last:
            return SEQ_DUPLICATE
        if seq > last + 1:
            return SEQ_GAP
        if sid in self.armed:                    # seq < last, and we asked for a restart
            self.armed.discard(sid)
            self.last[sid] = seq
            return SEQ_RESET
        return SEQ_GAP

    def rebase(self, sid, seq):
        """Force the counter to `seq` without a verdict.  The feed calls this after it has
        handled a gap, so the NEXT contiguous message is "ok" and one loss costs one
        resubscribe."""
        try:
            self.last[sid] = int(seq)
        except (TypeError, ValueError):
            self.last.pop(sid, None)

    def arm_reset(self, sid=None):
        """Declare that a counter restart is expected -- called right before a resubscribe."""
        if sid is None:
            self.armed.update(self.last.keys())
        else:
            self.armed.add(sid)

    def forget(self, sid=None):
        if sid is None:
            self.last.clear()
            self.armed.clear()
        else:
            self.last.pop(sid, None)
            self.armed.discard(sid)


# =============================================================================================
# THE I/O SHELL
# =============================================================================================
class WsFeed(object):
    """Kalshi `orderbook_delta` subscription.  Never raises into the caller.

    The protocol core (`subscribe_frame`, `handle_frame`, `on_open`, `on_close`) is pure and
    takes no I/O, so the entire state machine -- subscribe, snapshot, delta, gap, corruption,
    resubscribe, staleness, disconnect -- is driven in tests without a socket, without the
    `websockets` library, and without a clock.
    """

    def __init__(self, auth=None, tickers=(), url=WS_URL, max_age_s=WS_MAX_BOOK_AGE_S,
                 clock=None, rng=None, log_fn=None, max_markets=MAX_WS_MARKETS):
        self._lock = threading.RLock()
        self._auth = auth
        self._url = url
        self._max_age_s = float(max_age_s)
        self._max_markets = int(max_markets)
        self._clock = clock or M._now
        self._rng = rng or random.random
        self._log_fn = log_fn or M.log

        self._tickers = ()
        self._books = {}                # ticker -> BookState
        self._seq = SeqTracker()
        self._sid_tickers = {}          # sid -> tuple of tickers that subscription covers
        self._cmd_id = 0
        self._connected = False
        self._liveness_ts = 0.0         # last time the reader confirmed the socket open
        self._last_msg_ts = 0.0
        self._last_subscribe_ts = 0.0
        self._need_resubscribe = False

        self._msgs = 0
        self._gaps = 0
        self._duplicates = 0
        self._resets = 0
        self._corruptions = 0
        self._resubscribes = 0
        self._reconnects = 0
        self._errors = 0
        self._last_error = None

        self._thread = None
        self._stop = threading.Event()

        self.set_tickers(tickers)

    # -- configuration -------------------------------------------------------------------
    def set_tickers(self, tickers):
        """Set the desired subscription set, clamped to MAX_WS_MARKETS in the caller's own
        order (which is `market_poll_rank`'s ranking -- best first, §4.6).  Returns True if
        the set actually CHANGED, i.e. if a resubscribe is owed."""
        seen, clean = set(), []
        for t in (tickers or ()):
            if isinstance(t, str) and t and t not in seen:
                seen.add(t)
                clean.append(t)
        if len(clean) > self._max_markets:
            self._log("ws_ticker_clamp", asked=len(clean), cap=self._max_markets)
            clean = clean[:self._max_markets]
        new = tuple(clean)
        with self._lock:
            changed = new != self._tickers
            self._tickers = new
            if changed:
                self._need_resubscribe = True
                for t in list(self._books):
                    if t not in seen:
                        self._books.pop(t, None)
        return changed

    @property
    def tickers(self):
        with self._lock:
            return self._tickers

    # -- protocol core, no I/O -----------------------------------------------------------
    def subscribe_frame(self):
        """The subscribe command.  ONE command covers every ticker, which is the documented
        shape and costs one frame; the price is that `seq` is then per-SUBSCRIPTION, so a gap
        invalidates every market on it, not one.  That is the conservative reading and the one
        implemented.  Per-ticker subscriptions would narrow the blast radius and are the first
        optimisation to make if gaps prove common -- at the cost of multiplying the
        subscription count against a per-connection limit this file has not verified."""
        with self._lock:
            self._cmd_id += 1
            self._need_resubscribe = False
            self._last_subscribe_ts = self._clock()
            self._seq.arm_reset()
            return {"id": self._cmd_id, "cmd": "subscribe",
                    "params": {"channels": [WS_CHANNEL],
                               "market_tickers": list(self._tickers)}}

    def on_open(self, now=None):
        """The socket came up.  Books are NOT restored here: everything must be re-snapshotted
        because deltas were certainly missed while we were down."""
        now = self._clock() if now is None else now
        with self._lock:
            self._connected = True
            self._liveness_ts = now
            self._last_msg_ts = now
            self._books.clear()
            self._seq.forget()
            self._sid_tickers.clear()
            self._need_resubscribe = True
        self._log("ws_connect", url=self._url, n_tickers=len(self.tickers))

    def on_close(self, reason="", now=None):
        """The socket went away.  `connected` goes False and books_for() returns {} at once --
        the consumer's REST fallback (§4.6, clamped to 6) is correct where this is not."""
        with self._lock:
            was = self._connected
            self._connected = False
            self._books.clear()
            self._seq.forget()
            self._sid_tickers.clear()
        if was:
            self._log("ws_disconnect", reason=str(reason)[:200])

    def touch(self, now=None):
        """Reader heartbeat: the socket is confirmed open right now.  Called on every recv AND
        on every recv timeout, so a market that is merely QUIET stays fresh while a socket
        that is merely OPEN-BUT-DEAD is torn down by the library's PING/PONG inside
        WS_PING_INTERVAL_S + WS_PING_TIMEOUT_S."""
        with self._lock:
            self._liveness_ts = self._clock() if now is None else now

    def handle_frame(self, frame, now=None):
        """Apply one decoded exchange frame.  Returns the ACTION the I/O shell owes:

            "ok"          applied
            "ignored"     not ours / unknown / duplicate / a delta with no book under it
            "resubscribe" a gap or a corruption: the book has been DROPPED, resubscribe now
            "error"       an exchange `error` frame -- counted, and the loop keeps running

        Never raises.  An unparseable frame is "ignored", never fatal: one malformed message
        must not take down a feed that is carrying 32 markets.
        """
        now = self._clock() if now is None else now
        try:
            return self._handle(frame, now)
        except Exception as exc:                          # pragma: no cover - belt and braces
            with self._lock:
                self._errors += 1
                self._last_error = "%s: %s" % (type(exc).__name__, exc)
            return "ignored"

    def _handle(self, frame, now):
        if not isinstance(frame, dict):
            return "ignored"
        typ = frame.get("type")
        msg = frame.get("msg")
        with self._lock:
            self._msgs += 1
            self._last_msg_ts = now
            self._liveness_ts = now

        if typ == "subscribed":
            sid = _first_int(frame.get("sid"), (msg or {}).get("sid")
                             if isinstance(msg, dict) else None)
            if sid is not None:
                with self._lock:
                    self._sid_tickers[sid] = self._tickers
                    self._seq.forget(sid)
            self._log("ws_subscribed", sid=sid, n_tickers=len(self.tickers))
            return "ok"

        if typ == "error":
            with self._lock:
                self._errors += 1
                self._last_error = json.dumps(msg, default=str)[:300] if msg else "error"
            # An error frame is a REPLY to a command, not a statement about the book.  It must
            # not drop books and must not kill the loop -- an unsubscribable ticker would
            # otherwise take the other 31 markets down with it.
            self._log("ws_error_frame", err=(self._last_error or "")[:200])
            return "error"

        if typ not in ("orderbook_snapshot", "orderbook_delta"):
            return "ignored"

        sid = _first_int(frame.get("sid"))
        verdict = SEQ_OK
        if "seq" in frame:
            with self._lock:
                verdict = self._seq.check(sid, frame.get("seq"))

        if verdict == SEQ_DUPLICATE:
            with self._lock:
                self._duplicates += 1
            return "ignored"
        if verdict == SEQ_RESET:
            with self._lock:
                self._resets += 1

        if verdict == SEQ_GAP:
            if typ == "orderbook_snapshot":
                # A snapshot HEALS a gap: it is a total replacement, not a mutation, so it
                # depends on nothing that was lost.  This is also the loop-breaker -- if the
                # exchange keeps one sid across a resubscribe and continues its counter, the
                # snapshot we asked for would itself read as a gap and we would resubscribe
                # forever, dropping the book each time and never quoting off WS at all.
                with self._lock:
                    self._gaps += 1
                    self._seq.rebase(sid, frame.get("seq"))
                self._log("ws_gap_healed_by_snapshot", sid=sid, seq=frame.get("seq"))
            else:
                # THE property this file exists for.  One lost delta and the book is
                # unknowable; there is no interpolation, no "probably fine".
                with self._lock:
                    self._gaps += 1
                    self._seq.rebase(sid, frame.get("seq"))
                    dropped = self._drop_sid(sid)
                    self._need_resubscribe = True
                self._log("ws_seq_gap", sid=sid, seq=frame.get("seq"),
                          dropped=sorted(dropped))
                return "resubscribe"

        if not isinstance(msg, dict):
            return "ignored"
        ticker = msg.get("market_ticker")
        if not isinstance(ticker, str) or not ticker:
            return "ignored"
        with self._lock:
            if ticker not in self._tickers:
                return "ignored"                          # not a market we asked for
            if sid is not None and sid not in self._sid_tickers:
                self._sid_tickers[sid] = self._tickers
            book = self._books.get(ticker)

            if typ == "orderbook_snapshot":
                if book is None:
                    book = BookState(ticker)
                res = book.apply_snapshot(msg, now)
                if res != "ok":
                    self._books.pop(ticker, None)     # never keep a half-applied snapshot
                    return "ignored"
                self._books[ticker] = book
                return "ok"

            if book is None:
                # A delta with no snapshot under it -- we dropped this book and are awaiting
                # the resubscribe.  Applying it would MANUFACTURE a one-level book, and a thin
                # book reads to §1.2 as an opportunity.  Drop it.
                return "ignored"

            res = book.apply_delta(msg, now)
            if res == "ok":
                return "ok"
            if res in ("no_base", "bad_delta"):
                # No snapshot underneath (we dropped it and are awaiting a resubscribe), or an
                # unreadable frame.  Applying it would MANUFACTURE a book.  Drop it silently.
                return "ignored"
            # res == "corrupt"
            self._corruptions += 1
            reason = book.corrupt_reason
            self._books.pop(ticker, None)
            self._need_resubscribe = True
        self._log("ws_book_corrupt", ticker=ticker, reason=reason)
        return "resubscribe"

    def _drop_sid(self, sid):
        """Discard every book carried by a subscription.  Called with the lock held."""
        tickers = self._sid_tickers.get(sid)
        if not tickers:
            tickers = tuple(self._books)                  # unknown sid: assume it is ours
        for t in tickers:
            self._books.pop(t, None)
        return tuple(tickers)

    # -- accessors the consumer calls ----------------------------------------------------
    def needs_resubscribe(self):
        """True when the feed knows its subscription state is untrustworthy — after a
        connect, a seq gap, a corruption, or a resubscribe request.  Exposed because it is
        part of the contract a reviewer and a test must be able to assert on, not merely an
        internal flag the reader thread happens to consult."""
        with self._lock:
            return bool(self._need_resubscribe)

    def is_fresh(self, ticker, now=None):
        now = self._clock() if now is None else now
        with self._lock:
            if not self._connected:
                return False
            book = self._books.get(ticker)
            if book is None or book.corrupt or not book.has_snapshot:
                return False
            # A quiet market is not a stale market: the liveness stamp (refreshed on every
            # reader wake, and underwritten by the library's PING/PONG) is what proves the
            # book we hold is still the exchange's book.
            age = now - max(book.last_update_ts, self._liveness_ts)
            return age <= self._max_age_s

    def books_for(self, tickers=None, now=None):
        """{ticker: orderbook_body} for the FRESH ones only.  Markets that are stale, corrupt,
        un-snapshotted, or on a feed that is not connected are simply ABSENT -- the consumer
        reads an absence as "poll this one over REST", which is the correct degradation."""
        now = self._clock() if now is None else now
        out = {}
        try:
            with self._lock:
                want = self._tickers if tickers is None else tuple(tickers)
                for t in want:
                    if self.is_fresh(t, now):
                        out[t] = self._books[t].to_orderbook_body()
        except Exception:                                 # pragma: no cover
            return {}
        return out

    def book_or_none(self, ticker, now=None):
        now = self._clock() if now is None else now
        try:
            with self._lock:
                if self.is_fresh(ticker, now):
                    return self._books[ticker].to_orderbook_body()
        except Exception:                                 # pragma: no cover
            return None
        return None

    def health(self, now=None):
        now = self._clock() if now is None else now
        try:
            with self._lock:
                fresh = sum(1 for t in self._tickers if self.is_fresh(t, now))
                return {"available": websockets is not None,
                        "running": bool(self._thread is not None
                                        and self._thread.is_alive()),
                        "connected": bool(self._connected),
                        "subscribed_n": len(self._tickers),
                        "fresh_n": fresh,
                        "stale_n": len(self._tickers) - fresh,
                        "last_msg_age_s": (None if not self._last_msg_ts
                                           else round(now - self._last_msg_ts, 3)),
                        "reconnects": self._reconnects,
                        "gaps": self._gaps,
                        "duplicates": self._duplicates,
                        "resets": self._resets,
                        "corruptions": self._corruptions,
                        "resubscribes": self._resubscribes,
                        "errors": self._errors,
                        "msgs": self._msgs,
                        "last_error": self._last_error}
        except Exception:                                 # pragma: no cover
            return {"available": websockets is not None, "connected": False,
                    "subscribed_n": 0, "fresh_n": 0, "stale_n": 0,
                    "last_msg_age_s": None, "reconnects": 0, "gaps": 0,
                    "duplicates": 0, "resets": 0, "corruptions": 0,
                    "resubscribes": 0, "errors": 0, "msgs": 0, "last_error": None,
                    "running": False}

    # -- lifecycle -----------------------------------------------------------------------
    def start(self):
        """Start the reader thread.  Returns True if a reader is running.

        Returns False -- and never raises -- when `websockets` is not installed or there is no
        auth: the feed then reports `connected: False` forever and `books_for()` stays empty,
        so the consumer runs on REST exactly as it does today."""
        if websockets is None:
            self._log("ws_unavailable", why="websockets library not installed")
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._thread_main,
                                            name="ws_feed", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout=5.0):
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            try:
                t.join(timeout)
            except Exception:                             # pragma: no cover
                pass
        self.on_close("stopped")
        with self._lock:
            self._thread = None

    # -- the reader ----------------------------------------------------------------------
    def _thread_main(self):                               # pragma: no cover - I/O only
        try:
            asyncio.run(self._session_loop())
        except Exception as exc:
            self._log("ws_thread_died", err="%s: %s" % (type(exc).__name__, exc))
        finally:
            self.on_close("thread_exit")

    async def _session_loop(self):                        # pragma: no cover - I/O only
        attempt = 0
        while not self._stop.is_set():
            ok = False
            try:
                ok = await self._one_connection()
            except Exception as exc:
                self._log("ws_conn_error", err="%s: %s" % (type(exc).__name__, exc))
            self.on_close("session_end")
            if self._stop.is_set():
                break
            attempt = 0 if ok else attempt + 1
            with self._lock:
                self._reconnects += 1
            delay = backoff_delay(attempt, self._rng)
            self._log("ws_reconnect_wait", attempt=attempt, delay_s=round(delay, 3))
            waited = 0.0
            while waited < delay and not self._stop.is_set():
                step = min(0.25, delay - waited)
                await asyncio.sleep(step)
                waited += step

    async def _one_connection(self):                      # pragma: no cover - I/O only
        conn = await self._connect()
        try:
            self.on_open()
            await self._send(conn, self.subscribe_frame())
            last_resnap = self._clock()
            got_data = False
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(conn.recv(), timeout=WS_RECV_TIMEOUT_S)
                except asyncio.TimeoutError:
                    self.touch()                          # socket still open; keepalive proves it
                    raw = None
                if raw is not None:
                    got_data = True
                    self.touch()
                    action = self.handle_frame(_loads(raw))
                    if action == "resubscribe":
                        await self._resubscribe(conn)
                        continue
                now = self._clock()
                if self._need_resubscribe or (now - last_resnap) >= WS_RESNAPSHOT_S:
                    last_resnap = now
                    await self._resubscribe(conn)
            return got_data
        finally:
            try:
                await conn.close()
            except Exception:
                pass

    async def _resubscribe(self, conn):                   # pragma: no cover - I/O only
        with self._lock:
            self._resubscribes += 1
            self._books.clear()
        await self._send(conn, self.subscribe_frame())

    async def _send(self, conn, obj):                     # pragma: no cover - I/O only
        await conn.send(json.dumps(obj))

    async def _connect(self):                             # pragma: no cover - I/O only
        """`websockets` renamed `extra_headers` -> `additional_headers` in 14.0 and kept both
        spellings nowhere.  Try the modern name, fall back once.  Getting this wrong is a
        TypeError at connect time, i.e. a feed that never starts on a box with the other
        version installed -- the failure most likely to be discovered in production."""
        hdrs = ws_auth_headers(self._auth)
        kw = {"ping_interval": WS_PING_INTERVAL_S,
              "ping_timeout": WS_PING_TIMEOUT_S,
              "open_timeout": WS_OPEN_TIMEOUT_S,
              "close_timeout": WS_CLOSE_TIMEOUT_S,
              "max_queue": WS_MAX_QUEUE}
        try:
            return await websockets.connect(self._url, additional_headers=hdrs, **kw)
        except TypeError:
            return await websockets.connect(self._url, extra_headers=hdrs, **kw)

    # -- misc ----------------------------------------------------------------------------
    def _log(self, event, **fields):
        try:
            self._log_fn(event, **fields)
        except Exception:                                 # logging must never kill the feed
            pass


def _loads(raw):
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        return json.loads(raw)
    except Exception:
        return None


def _first_int(*vals):
    for v in vals:
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


# =============================================================================================
# THE SEAM — what `Maker.cycle()` calls.  Three lines, documented in the module docstring.
# =============================================================================================
_ATTACHED = None
_ATTACH_LOCK = threading.Lock()


def attach(maker=None, auth=None, tickers=(), url=WS_URL, **kw):
    """Idempotent.  First call builds the feed and starts the reader; every later call just
    republishes the desired ticker set (resubscribing only if it actually changed).  Safe to
    call once per 1 Hz cycle, which is exactly how the consumer uses it.

    Returns a `WsFeed` ALWAYS -- including when `websockets` is not installed, in which case
    it is a feed that never connects, whose `books_for()` is empty and whose `health()` says
    `connected: False`.  It never raises, so the caller's `try/except` is not load-bearing.
    """
    global _ATTACHED
    try:
        with _ATTACH_LOCK:
            feed = _ATTACHED
            if feed is None:
                feed = WsFeed(auth=auth, tickers=tickers, url=url, **kw)
                _ATTACHED = feed
                feed.start()
            else:
                if auth is not None and feed._auth is None:
                    feed._auth = auth
                feed.set_tickers(tickers)
            if maker is not None:
                try:
                    maker.ws = feed
                except Exception:
                    pass
            return feed
    except Exception:                                     # pragma: no cover
        return WsFeed(auth=None, tickers=(), url=url)


def ws_book_or_none(ticker, now=None):
    """The consumer's per-ticker accessor.  A fresh WS book, or None -> poll REST.

    None on every failure: no feed attached, library absent, socket down, no snapshot yet,
    seq gap, corruption, staleness.  There is deliberately no way for this to raise and no
    way for it to hand back a book the feed is not certain about."""
    feed = _ATTACHED
    if feed is None:
        return None
    try:
        return feed.book_or_none(ticker, now)
    except Exception:                                     # pragma: no cover
        return None


def health(now=None):
    feed = _ATTACHED
    if feed is None:
        return {"available": websockets is not None, "connected": False, "subscribed_n": 0,
                "fresh_n": 0, "stale_n": 0, "last_msg_age_s": None, "reconnects": 0,
                "gaps": 0, "duplicates": 0, "resets": 0, "corruptions": 0,
                "resubscribes": 0, "errors": 0, "msgs": 0, "last_error": None,
                "running": False, "attached": False}
    h = feed.health(now)
    h["attached"] = True
    return h


def detach():
    """Stop and forget the attached feed.  Used by the shutdown path and by tests."""
    global _ATTACHED
    with _ATTACH_LOCK:
        feed, _ATTACHED = _ATTACHED, None
    if feed is not None:
        try:
            feed.stop()
        except Exception:                                 # pragma: no cover
            pass
    return feed
