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
        return R.public_get("/liquidity_incentive_programs", params=params)

    def positions(self):                                     # pragma: no cover - network
        return R.signed(self.auth, "GET", "/portfolio/positions")

    def balance(self):                                       # pragma: no cover - network
        return R.signed(self.auth, "GET", "/portfolio/balance")

    def fills(self, min_ts=None):                            # pragma: no cover - network
        params = {"min_ts": int(min_ts)} if min_ts else None
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
    that drifts from it silently is worse than no fake at all."""

    def __init__(self, books=None, positions=None, balance_cents=0, now=0.0):
        self.books = dict(books or {})
        self._positions = list(positions or [])
        self.balance_cents = int(balance_cents)
        self.placed = []
        self.cancelled = []
        self.orders = {}
        self._oid = 0
        self.place_status = 201
        self.place_error = None
        self.cancel_status = 200
        self.fills_rows = []
        self.settlement_rows = []
        self.now = now

    # -- reads -------------------------------------------------------------------------
    def book(self, ticker):
        b = self.books.get(ticker)
        return (200, b) if b is not None else (404, {})

    def market(self, ticker):
        return 200, {"status": "active", "ticker": ticker}

    def programs(self, cursor=None):
        return 200, {"liquidity_incentive_programs": []}

    def positions(self):
        return 200, {"market_positions": list(self._positions)}

    def balance(self):
        return 200, {"balance": self.balance_cents}

    def fills(self, min_ts=None):
        return 200, {"fills": list(self.fills_rows)}

    def settlements(self):
        return 200, {"settlements": list(self.settlement_rows)}

    # -- writes ------------------------------------------------------------------------
    def place(self, body):
        self.placed.append(dict(body))
        if self.place_error is not None:
            return self.place_status, {"error": {"message": self.place_error}}
        self._oid += 1
        oid = "fake-%d" % self._oid
        self.orders[oid] = dict(body)
        return self.place_status, {"order": {"order_id": oid,
                                             "client_order_id": body.get("client_order_id"),
                                             "status": "resting",
                                             "remaining_count": float(body.get("count", 0)),
                                             "fill_count": 0}}

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        if self.cancel_status != 200:
            return self.cancel_status, {}
        body = self.orders.pop(order_id, None)
        remaining = float(body.get("count", 0)) if body else 0.0
        return 200, {"reduced_by": remaining}
