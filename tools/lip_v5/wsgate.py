"""
lip_v5.wsgate — the W2 trust gate around the vendored `ws_feed` (spec §3.5, kept VERBATIM
from v4 on its merits).

A websocket book is a RECONSTRUCTION; a REST book is the EXCHANGE'S OWN STATEMENT.  Until the
reconstruction has been shown to match the statement, IT MAY NOT PRICE A QUOTE.

Derivation of `WS_AGREE_REQUIRED = 3` (v4's, re-checked and kept): the dominant risk is a
systematic parse error — a dollars-vs-cents unit slip, an inverted side, a wrong field — and
every one of those disagrees on the FIRST non-empty comparison, so N = 1 already kills them.
What N > 1 buys is protection against certifying on a DEGENERATE sample (both books empty, an
untraded market): three agreements at the 60 s cadence span ~3 minutes, over which the measured
20%/45 s best-change rate makes at least one book change ~63% likely, so the gate is usually
proven against a MOVING book.  Residual, stated: on a genuinely static book three agreements
prove only that the two sources agree on a static book — staleness is the independent control
for that case.

MIRROR (trusting the WS too SOON ↔ never trusting it and losing breadth): the gate and the
60 s re-proof guard the first end; the per-market REST fallback guards the second, so a market
whose gate never passes is SLOWER, never WRONG.  Breadth lifts 6 → 32 only while connected.
"""

from . import config as C
from . import runtime as R
from . import ws_feed

WS_AGREE, WS_DIVERGE, WS_DEGENERATE = "agree", "diverge", "degenerate"
WS_UNIT_RATIO_TOL = 0.25          # a dollars-vs-cents slip shows up as a ~100× ratio


def best_from_book(body):
    """(yes_bid_c, yes_ask_c) from a Kalshi `orderbook_fp` body.  Both sides are quoted as
    BIDS in their own currency, so `yes_ask = 100 − best_no_bid`."""
    ob = body.get("orderbook") if isinstance(body, dict) else None
    if not isinstance(ob, dict):
        ob = body if isinstance(body, dict) else {}
    fp = ob.get("orderbook_fp") or (body or {}).get("orderbook_fp") or {}
    if not isinstance(fp, dict):
        return None, None
    yc = _levels_cents(fp.get("yes_dollars"))
    nc = _levels_cents(fp.get("no_dollars"))
    yb = max([p for p, _ in yc]) if yc else None
    nb = max([p for p, _ in nc]) if nc else None
    return yb, ((100 - nb) if nb is not None else None)


def _levels_cents(levels):
    out = []
    for lv in levels or []:
        try:
            out.append((int(round(float(lv[0]) * 100)), float(lv[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def ws_compare(ws_body, rest_body):
    """Compare a reconstructed WS book against the exchange's REST statement.

    `degenerate` means the sample PROVES NOTHING (one or both sides missing on BOTH sources)
    and MUST NOT be counted toward the gate — certifying on an empty book is certifying on
    nothing.

    The unit probe lives HERE rather than in the feed because this is the place where REST
    tells us the answer: if the WS book were in dollars while we read it as cents (or the
    reverse), the best prices differ by ~100×, reported explicitly as `unit_mismatch` rather
    than as an ordinary disagreement.  Naming it is the difference between "the feed is flaky"
    and "the feed is 100× wrong", which are opposite operational responses.
    """
    wb, wa = best_from_book(ws_body)
    rb, ra = best_from_book(rest_body)
    if (wb is None and wa is None) or (rb is None and ra is None):
        return WS_DEGENERATE, {"ws": (wb, wa), "rest": (rb, ra), "why": "empty_side"}
    detail = {"ws_bid": wb, "ws_ask": wa, "rest_bid": rb, "rest_ask": ra}
    for w, r in ((wb, rb), (wa, ra)):
        if w is None or r is None or w == r:
            continue
        if r != 0:
            ratio = float(w) / float(r)
            for factor in (100.0, 0.01):
                if abs(ratio - factor) <= WS_UNIT_RATIO_TOL * factor:
                    detail["unit_mismatch"] = ratio
                    R.log("unit_mismatch", ratio=ratio, **detail)
                    return WS_DIVERGE, detail
        return WS_DIVERGE, detail
    if wb != rb or wa != ra:
        return WS_DIVERGE, detail
    return WS_AGREE, detail


class WsGate(object):
    """Per-market agreement counters plus the epoch re-proof.

    On a reconnect the whole gate is CLEARED: a new socket is a new reconstruction, and
    agreements earned by the previous one say nothing about this one's sequence handling.
    """

    def __init__(self, required=C.WS_AGREE_REQUIRED):
        self.required = int(required)
        self.agreements = {}
        self.epoch = None

    def on_epoch(self, epoch):
        """v4's `reproof_epoch()`: a reconnect invalidates every gate."""
        if self.epoch is not None and epoch != self.epoch and self.agreements:
            R.log("ws_gate_reset", markets=sorted(self.agreements), epoch=epoch)
            self.agreements.clear()
        self.epoch = epoch

    def passed(self, ticker):
        return self.agreements.get(ticker, 0) >= self.required

    def observe(self, ticker, ws_body, rest_body):
        """One comparison.  Returns (verdict, passed_now, detail).  Reverts to REST on ANY
        divergence — the counter resets to zero, it does not decrement."""
        verdict, detail = ws_compare(ws_body, rest_body)
        if verdict == WS_DEGENERATE:
            return verdict, self.passed(ticker), detail
        if verdict == WS_AGREE:
            n = self.agreements.get(ticker, 0) + 1
            self.agreements[ticker] = n
            if n == self.required:
                R.log("ws_gate_passed", ticker=ticker, agreements=n, **detail)
            return verdict, n >= self.required, detail
        prev = self.agreements.get(ticker, 0)
        self.agreements[ticker] = 0
        if prev:
            R.log("ws_gate_lost", ticker=ticker, agreements_lost=prev, **detail)
        return verdict, False, detail

    def book_for(self, ticker, feed, now, rest_body=None):
        """The book that may PRICE A QUOTE for `ticker`: the WS book iff the gate has passed
        AND the book is fresh; otherwise the REST body.  Per-market fallback, so one bad
        market never costs the whole feed."""
        if feed is not None and self.passed(ticker):
            b = feed.book_or_none(ticker, now)
            if b is not None:
                return b, "ws"
        return rest_body, "rest"

    def breadth(self, connected):
        """Breadth 6 → 32 ONLY while connected (spec §3.5).  Off the socket we are back on the
        REST clamp, and pretending otherwise is how a disconnect becomes a coverage hole."""
        return C.MAX_WS_MARKETS if connected else C.MAX_REST_MARKETS


def attach(auth=None, tickers=(), **kw):
    """Start the vendored feed.  Returns None when `websockets` is unavailable — REST-only is
    a supported mode, and a missing optional dependency must degrade, never crash."""
    if not C.WS_ENABLED:
        return None
    try:
        return ws_feed.attach(auth=auth, tickers=tickers, **kw)
    except Exception as exc:                                 # pragma: no cover
        R.log("ws_attach_failed", err="%s: %s" % (type(exc).__name__, exc))
        return None
