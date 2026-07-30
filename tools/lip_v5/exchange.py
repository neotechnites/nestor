"""
lip_v5.exchange — the ONE seam between the run loop and the wire.

Every network call the engine makes goes through this object.  The engine never touches
`runtime.http` directly, which is what makes the whole cycle testable with a `FakeExchange` and
what makes "no test can reach the wire" checkable rather than hoped for.

Each method returns `(status, body)` and NEVER raises: transport failure is `(0, {...})`, which
the engine treats as "unknown", never as "no".  A raise here would unwind the cycle mid-way and
leave the ledger describing a world that does not exist.
"""

from . import config as C
from . import runtime as R


class Exchange(object):
    """The live implementation.  Refuses to construct unless the runtime is live, so an inert
    process cannot hold one by accident."""

    def __init__(self, auth):
        if not R.is_live():
            raise RuntimeError("Exchange requires a live runtime (--live)")
        self.auth = auth

    # -- reads -------------------------------------------------------------------------
    def book(self, ticker):                                  # pragma: no cover - network
        return R.public_get("/markets/%s/orderbook" % ticker)

    def market(self, ticker):                                # pragma: no cover - network
        return R.public_get("/markets/%s" % ticker)

    def programs(self, cursor=None):                         # pragma: no cover - network
        params = {"limit": C.SCAN_PAGE_LIMIT} if hasattr(C, "SCAN_PAGE_LIMIT") else {}
        if cursor:
            params["cursor"] = cursor
        return R.public_get("/incentive_programs", params=params)

    def estimates(self, user_id):                            # pragma: no cover - network
        """SF-4c — the /v1 accrued-rewards feed (see runtime.signed_v1's capture note)."""
        return R.signed_v1(self.auth, "GET", C.ESTIMATES_PATH % user_id)

    def trades(self, ticker, min_ts=None, limit=1):          # pragma: no cover - network
        """Public trade tape — P6's evidence source ("does ANYONE ever trade here?").
        `limit=1` because P6 needs existence, not volume: one row answers the question."""
        params = {"ticker": ticker, "limit": int(limit)}
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        return R.public_get("/markets/trades", params=params)

    def positions(self):                                     # pragma: no cover - network
        return R.signed(self.auth, "GET", "/portfolio/positions")

    def orders(self):                                        # pragma: no cover - network
        """Resting orders — the v1 §9.4 step-4 recovery sweep's evidence.  Same path family
        as place/cancel (`/portfolio/orders` 410s; the events form is the live one)."""
        # GET is /portfolio/orders (200, {cursor, orders}); the events form 404s on GET.
        # C.ORDERS_PATH is the POST path — the 410 in its comment is about PLACING, not reading.
        return R.signed(self.auth, "GET", "/portfolio/orders", params={"status": "resting"})

    def balance(self):                                       # pragma: no cover - network
        return R.signed(self.auth, "GET", "/portfolio/balance")

    def fills(self, min_ts=None, order_id=None):             # pragma: no cover - network
        """`order_id` scoping is the v1 §9.4a disambiguation read (v4's `do_fills` shape);
        `min_ts` is the cadenced live poll and the §9.4(4) crash-gap window."""
        params = {"limit": 200}
        if min_ts:
            params["min_ts"] = int(min_ts)
        if order_id:
            params["order_id"] = order_id
        return R.signed(self.auth, "GET", "/portfolio/fills", params=params)

    def settlements(self):                                   # pragma: no cover - network
        return R.signed(self.auth, "GET", "/portfolio/settlements")

    # -- writes ------------------------------------------------------------------------
    def place(self, body):                                   # pragma: no cover - network
        return R.signed(self.auth, "POST", C.ORDERS_PATH, body=body)

    def cancel(self, order_id):                              # pragma: no cover - network
        return R.signed(self.auth, "DELETE", "%s/%s" % (C.ORDERS_PATH, order_id))


class FakeExchange(object):
    """A scriptable exchange for the suite.  Deliberately lives in the shipped package rather
    than in the tests: the engine's contract with the wire is part of the design, and a fake
    that drifts from it silently is worse than no fake at all.

    REAL-WIRE FIDELITY (final fix round — the fake's leniency is what hid the missing
    feedback half):
      * `cancel` of an order the book no longer holds returns **404**, never 200 with
        `reduced_by = 0` — on the real wire a fully-filled order is GONE, and the old fake
        let the engine "learn" its fill from a cancel that the wire would refuse.
      * the public `book` REFLECTS OUR OWN RESTING ORDERS — the real book does, which is
        why S must subtract our size (SF-5) and why a fake that hid us made that defect
        invisible.
      * `fills` rows carry the REAL payload shape v4's prod parsing consumes: `trade_id`,
        `order_id`, `side` ("yes"/"no"), `action`, `count`, `yes_price` in CENTS,
        `is_taker` — no invented `price`/`fill_id` dollars fields.
      * `take(oid, n)` executes a taker against a resting order: the ONLY way a fill
        becomes learnable is the fills API (plus a smaller `reduced_by` on a later cancel),
        exactly as on the wire.
    """

    def __init__(self, books=None, positions=None, balance_cents=0, now=0.0):
        self.books = dict(books or {})
        self._positions = list(positions or [])
        self.balance_cents = int(balance_cents)
        self.placed = []
        self.cancelled = []
        self.resting = {}                    # oid -> the placed body (the fake's own book)
        self._oid = 0
        self.place_status = 201
        self.place_error = None
        self.cancel_status = 200
        self.fills_rows = []
        self.settlement_rows = []
        self.market_closes = {}              # ticker -> close_ts (epoch s); default now+24h
        self.market_close_missing = set()    # tickers whose market payload carries NO close
        self.trades_rows = None              # None => "one recent trade" (P6 admits)
        self.now = now

    # -- reads -------------------------------------------------------------------------
    def book(self, ticker):
        b = self.books.get(ticker)
        if b is None:
            return 404, {}
        return 200, self.with_own_orders(ticker, b)

    def with_own_orders(self, ticker, body):
        """Merge OUR resting orders into the public book, as the real book does.  A bid
        joins `yes_dollars` at its YES price; an ask (a NO bid at 1 − p) joins
        `no_dollars`."""
        import copy
        out = copy.deepcopy(body)
        fp = ((out.get("orderbook") or {}).get("orderbook_fp")
              or out.get("orderbook_fp"))
        if fp is None:
            return out
        for o in self.resting.values():
            if o.get("ticker") != ticker:
                continue
            n = float(o.get("count", 0))
            if n <= 0:
                continue
            px = float(o.get("price", 0))
            if o.get("side") == "bid":
                key, level_px = "yes_dollars", px
            else:
                key, level_px = "no_dollars", round(1.0 - px, 4)
            levels = fp.setdefault(key, [])
            for lv in levels:
                if abs(float(lv[0]) - level_px) < 1e-9:
                    lv[1] = str(float(lv[1]) + n)
                    break
            else:
                levels.append(["%.4f" % level_px, str(n)])
        return out

    def market(self, ticker):
        body = {"status": "active", "ticker": ticker}
        # REAL-WIRE FIDELITY: every live market carries a `close_time` — it is not optional
        # on the wire, so a fake market without one models a market that does not exist.
        # Default = now + 24 h, i.e. the common near-settling case the settlement gate
        # (note 52 D4) admits; tests exercising the gate's REFUSE end set `market_closes`
        # explicitly (or `market_close_missing` for the pathological no-close payload).
        if ticker in getattr(self, "market_close_missing", ()):
            return 200, {"market": body}
        close = self.market_closes.get(ticker, float(self.now) + 24 * 3600.0)
        from datetime import datetime, timezone
        body["close_time"] = datetime.fromtimestamp(
            float(close), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return 200, {"market": body}

    def estimates(self, user_id):
        return 200, {"estimates": list(getattr(self, "estimates_rows", []) or []),
                     "updated_ts": None}

    def trades(self, ticker, min_ts=None, limit=1):
        if self.trades_rows is not None:
            return 200, {"trades": list(self.trades_rows)}
        return 200, {"trades": [{"ticker": ticker, "created_time": self.now}]}

    def positions(self):
        return 200, {"market_positions": list(self._positions)}

    def programs(self, cursor=None):
        return 200, {"incentive_programs": [], "next_cursor": None}

    def balance(self):
        return 200, {"balance": self.balance_cents}

    def fills(self, min_ts=None, order_id=None):
        rows = list(self.fills_rows)
        if order_id is not None:
            rows = [r for r in rows if str(r.get("order_id")) == str(order_id)]
        return 200, {"fills": rows}

    def settlements(self):
        return 200, {"settlements": list(self.settlement_rows)}

    def orders(self):
        rows = []
        for oid, body in sorted(self.resting.items()):
            rows.append({"order_id": oid,
                         "client_order_id": body.get("client_order_id"),
                         "ticker": body.get("ticker"), "side": body.get("side"),
                         "price": body.get("price"),
                         "remaining_count": float(body.get("count", 0))})
        return 200, {"orders": rows}

    # -- writes ------------------------------------------------------------------------
    def place(self, body):
        self.placed.append(dict(body))
        if self.place_error is not None:
            return self.place_status, {"error": {"message": self.place_error}}
        self._oid += 1
        oid = "fake-%d" % self._oid
        self.resting[oid] = dict(body)
        # THE REAL WIRE SHAPE, captured from prod 2026-07-28: order fields are FLAT at the top
        # level and the counts are DOLLAR-STRINGS, not floats.  The old fixture nested them
        # under "order" with float counts, so `place()` read every real success as a rejection
        # and re-placed the same order every second — 130 duplicates on one rung.  The fake is
        # the engine's contract with the wire; when it speaks a dialect the wire does not, a
        # green suite certifies nothing.
        return self.place_status, {"order_id": oid,
                                   "client_order_id": body.get("client_order_id"),
                                   "remaining_count": "%.2f" % float(body.get("count", 0)),
                                   "fill_count": "0.00",
                                   "ts_ms": int((now_ms := 0) or 1785268562482)}

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        if self.cancel_status != 200:
            return self.cancel_status, {}
        body = self.resting.pop(order_id, None)
        if body is None:
            # REAL wire: a fully-filled / already-gone order 404s.  It does NOT return
            # 200/reduced_by=0 — that leniency let the engine "learn" fills from a call the
            # exchange would refuse, hiding the missing fills poll.
            return 404, {"error": {"code": "order_not_found"}}
        return 200, {"reduced_by": float(body.get("count", 0))}

    # -- the taker ---------------------------------------------------------------------
    def take(self, order_id, count, now=None, trade_id=None):
        """A taker crosses into our resting order.  Emits the REAL consequences and nothing
        else: the resting size shrinks (gone at zero), a real-shaped `/portfolio/fills` row
        appears, and the net position moves.  The engine may learn this ONLY through the
        fills API (or a smaller `reduced_by` on a later cancel)."""
        body = self.resting.get(order_id)
        if body is None:
            return None
        n = min(float(count), float(body.get("count", 0)))
        body["count"] = float(body.get("count", 0)) - n
        if body["count"] <= 1e-9:
            self.resting.pop(order_id, None)
        # THE 2026-07-30 WIRE SHAPE, verbatim from captured_fills_20260730.json (a real
        # maker fill on KXUST10AD): fractional dollar-string `count_fp`, `*_price_dollars`,
        # `book_side`, `fee_cost`.  The old fixture spoke `count`/`yes_price` cents — and the
        # parser that consumed them read every REAL fill as ZERO contracts (found live,
        # first fill of the note-52 deploy).  The fake emits ONLY the wire's dialect so a
        # parser regression to the old fields fails the suite instead of the account.
        yes_p = float(body.get("price", 0))
        our_side = body.get("side")                       # "bid"/"ask", our order's side
        row = {"trade_id": trade_id or ("t-%d" % (len(self.fills_rows) + 1)),
               "fill_id": trade_id or ("t-%d" % (len(self.fills_rows) + 1)),
               "order_id": order_id,
               "ticker": body.get("ticker"), "market_ticker": body.get("ticker"),
               "book_side": our_side,
               "side": "yes" if our_side == "bid" else "no",
               "outcome_side": "yes" if our_side == "bid" else "no",
               "action": "buy" if our_side == "bid" else "sell",
               "count_fp": "%.2f" % n,
               "yes_price_dollars": "%.4f" % yes_p,
               "no_price_dollars": "%.4f" % (1.0 - yes_p),
               "fee_cost": "0.000000",
               "is_taker": False, "created_time": now,
               "ts": int(now or 0), "subaccount_number": 0}
        self.fills_rows.append(row)
        sign = 1 if our_side == "bid" else -1
        tk = body.get("ticker")
        for p in self._positions:
            if p.get("ticker") == tk:
                p["position"] = float(p.get("position", 0)) + sign * n
                break
        else:
            self._positions.append({"ticker": tk, "position": sign * n})
        return row
