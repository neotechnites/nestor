"""
lip_v5.guards — THE RAILS.  Every one of these is a refusal the run loop consults before it
spends, and they are ordered: `place_allowed()` runs them in dependency order and returns the
FIRST refusal, because a book that is halted does not also need to be told its cluster is full.

Enumerated by cross-bot review (B1..B13).  The through-line: v4 had CONSTANTS for several of
these and no implementation — `DAY_STOP_FRAC` existed, was tested as a pure function, and had
zero call sites.  A guard with no call site is not a guard, it is a comment with a unit test,
and this file exists so that can be checked in one place.
"""

import math
import os

from . import clusters as CL
from . import config as C
from . import runtime as R


# =============================================================================================
# B2 — DAY STOP.  Ported from v4's exact shape, including the two corrections that shape
# already carries.
# =============================================================================================
def day_stop_usd(projected_day_reward_usd,
                 frac=C.DAY_STOP_FRAC, floor=C.DAY_STOP_FLOOR_USD, cap=C.DAY_STOP_CAP_USD,
                 ceiling_usd=None):
    """`min($150, max($20, 0.35 × projected_day_reward))` — returned POSITIVE, as a loss
    magnitude.  The largest drag that still leaves the day net-positive."""
    # THE FLOOR SCALES WITH CAPITAL.  $20 dates from the $45-capital era; at a $300 ceiling
    # it pins day_stop at $20, hence the rung cap at $10, and the book deployed $5.42 of
    # $300 — the guard sized for a toy account throttling a real one.  20% of the ceiling is
    # the same statement the original $20 made about $45-$100 of capital, and it stays well
    # inside the 35% drawdown halt that bounds the day regardless.
    # MIRROR (floor too HIGH ↔ too low): too high lets a bad day run further before the stop
    # (bounded by the drawdown halt and the per-cluster cap); too low is what we measured —
    # a book that cannot deploy, earns nothing, and therefore never raises the reward-derived
    # term that would have unpinned it.
    eff_floor = max(float(floor), 0.20 * float(ceiling_usd)) if ceiling_usd else float(floor)
    return min(float(cap), max(eff_floor, float(frac) * float(projected_day_reward_usd)))


def portfolio_variance(positions, resting_basis=None, extra=None, denominator_usd=None):
    """`V = Σ wᵢ²(1−pᵢ)/pᵢ` over CLUSTERS, and `N_eff = 1/Σ wᵢ²`.  Returns `(V, N_eff)`.

    ── `denominator_usd` — THE WEIGHTS ARE FRACTIONS OF THE CEILING, NOT OF WHAT IS DEPLOYED. ──
    This is the difference between a rail that works and one that cannot start.  Against DEPLOYED
    capital the first order in an empty book is one cluster at w = 1.0, so V = (1−p)/p = 7.33 at
    12c — over any sane tolerance — and a variance rail would refuse EVERY first order and the
    book could never reach the breadth that satisfies it.  Measured: gating on deployed weights
    broke 59 tests, all of them "nothing reached the exchange".
    Against the CEILING the same order is w = 1/30 and V = 0.008, and V rises as the book fills,
    reaching tolerance exactly when the capital is fully deployed:
        30 clusters × $10 of a $300 ceiling, all at 12c → V = 0.244  (the target book)
        1 cluster × $300                                → V = 7.33   (refused, correctly:
                                                                      that is one bet)
    So the same threshold that describes the finished book also admits the path to it, and the
    constraint is FORWARD-LOOKING — it asks "is the book we are building diversified", not "is
    the book we have so far diversified", which is the question that has no useful answer at
    N = 1.  Falls back to deployed capital when no ceiling is supplied.

    V is variance per CEILING dollar of a held-to-settlement book, so it is scale-free — the
    same number governs a $45 book and a $300 one.  `(1−p)/p` is the payoff variance of one
    dollar at price p on a binary that pays $1 or $0; `basis` IS that price, which is why this
    needs no extra input beyond the book the caps already read.

    CLUSTERS, not markets: a threshold ladder is one bet wearing many tickers (note 43 §3), so
    weighting by ticker would report N_eff = 30 for thirty rungs of one gas ladder — the error
    that produced the −$587 unmatched residual.  Intra-cluster netting is IGNORED, so V is an
    upper bound and errs toward refusing.

    `extra` — an order under consideration, counted as though it had filled.  A resting order is
    contingent, but with no exit NET exposure equals GROSS the moment it fills, and a variance
    rail that only sees positions learns about concentration from the fills.

    An EMPTY book has V = 0 and N_eff = 0: no deployed dollars, no variance.  A leg whose basis
    is outside (0, 1) contributes nothing and is logged, because a price we cannot read is a
    variance we cannot compute and silently treating it as zero is the dangerous direction.
    """
    rows = list(positions or []) + list(resting_basis or [])
    if extra is not None:
        rows = rows + [extra]
    by_cluster = {}
    for p in rows:
        n = abs(float(p.get("n", 0) or 0.0))
        b = float(p.get("basis", 0.0) or 0.0)
        usd = n * b
        if usd <= 0:
            continue
        if not (0.0 < b < 1.0):
            R.log_once("portfolio_var_unpriced_leg", ticker=p.get("ticker"), basis=b,
                       note="basis outside (0,1): excluded from V")
            continue
        ck = CL.cluster_of(p.get("ticker"))
        agg = by_cluster.setdefault(ck, [0.0, 0.0])
        agg[0] += usd
        agg[1] += usd * b                     # capital-weighted price, for the cluster's own p
    deployed = sum(a[0] for a in by_cluster.values())
    if deployed <= 0:
        return 0.0, 0.0
    total = max(float(denominator_usd or 0.0), deployed) or deployed
    v = 0.0
    sw2 = 0.0
    for usd, usd_px in by_cluster.values():
        w = usd / total
        p = usd_px / usd                      # capital-weighted mean price in the cluster
        sw2 += w * w
        v += w * w * (1.0 - p) / p
    return v, (1.0 / sw2 if sw2 > 0 else 0.0)


def unpriced_positions(positions, yes_mids):
    """Tickers holding inventory for which NO two-sided mid exists."""
    return sorted(t for t, pos in (positions or {}).items()
                  if (abs(pos.get("yes", 0.0)) + abs(pos.get("no", 0.0))) > 0
                  and yes_mids.get(t) is None)


def mark_to_market_pnl(positions, position_cost, yes_mids, fees_paid_usd=0.0):
    """Realised + unrealised P&L on inventory.  Cost comes from the LEDGER, never from an
    exchange index.

    **UNPRICED POSITIONS MARK AT COST** (v4's NEW-2, carried verbatim because the reasoning is
    unchanged).  A position on a market with no two-sided mid — a PINNED rung is one-sided BY
    DEFINITION — cannot be marked.  Subtracting its full cost while contributing no value reads
    that inventory as a TOTAL LOSS: two pinned $10 slots alone print −$20, which is exactly the
    day-stop floor, and the stop then cancels everything mid-window on precisely the gas books
    we are there for.  Marking at cost contributes zero P&L, the only honest statement about a
    price we cannot observe, and the COUNT is surfaced separately so "we cannot see it" never
    reads as "it is fine".
    """
    value = 0.0
    cost = dict(position_cost or {})
    for ticker, pos in (positions or {}).items():
        mid = yes_mids.get(ticker)
        if mid is None:
            value += cost.get(ticker, 0.0)                    # marks at cost ⇒ contributes 0
            continue
        value += pos.get("yes", 0.0) * float(mid) + \
            pos.get("no", 0.0) * (1.0 - float(mid))
    return value - sum(cost.values()) - float(fees_paid_usd)


def day_stop_breached(pnl_usd, projected_day_reward_usd, **kw):
    """Breach when the LOSS reaches the stop.  On breach: cancel-all → flatten → alert → halt."""
    return -float(pnl_usd) >= day_stop_usd(projected_day_reward_usd, **kw) - 1e-12


def day_stop_exempt(order_is_fully_closing):
    """**THE FULLY-CLOSING EXEMPTION.**  A halted book must still be able to LEAVE.  An order
    that only reduces inventory cannot increase exposure — its worst case is that we end up
    flat — so refusing it would trap us in the position that tripped the stop, which is the
    opposite of what a stop is for.  Every other order is refused."""
    return bool(order_is_fully_closing)


# =============================================================================================
# B5 — HALT / RESUME STATE MACHINE.  One persisted halt, and every stand-down lands in it.
# =============================================================================================
class HaltState(object):
    """`place()`'s FIRST check.

    Persisted, because a halt that a restart clears is not a halt — and every historical
    incident here ends with a process restarting into the condition that halted it.  Resume is
    an EXPLICIT OPERATOR RECORD, never a timer: a timer converts "a human must look at this"
    into "wait long enough", which is the failure the halt existed to prevent.
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(C.DATA_DIR, "v5_halt.json")
        self.halted = False
        self.reason = None
        self.ts = None
        self.detail = {}

    def load(self):
        obj = R.read_json(self.path, default=None)
        if isinstance(obj, dict) and obj.get("halted"):
            self.halted = True
            self.reason = obj.get("reason")
            self.ts = obj.get("ts")
            self.detail = obj.get("detail") or {}
        return self

    def halt(self, reason, now, detail=None, persist=True):
        self.halted = True
        self.reason = reason
        self.ts = float(now)
        self.detail = dict(detail or {})
        R.log("halt", reason=reason, **self.detail)
        R.ntfy("halt", "lip_v5 HALT: %s" % reason)
        if persist:
            R.atomic_write_json(self.path, {"halted": True, "reason": reason,
                                            "ts": self.ts, "detail": self.detail})
        return self

    def resume(self, operator_note, now):
        """Explicit operator record ONLY."""
        if not operator_note:
            raise ValueError("resume requires an explicit operator note")
        self.halted = False
        self.reason = None
        R.log("halt_resume", note=operator_note, ts=float(now))
        R.atomic_write_json(self.path, {"halted": False, "resumed_ts": float(now),
                                        "note": operator_note})
        return self


# =============================================================================================
# B3 — ALL-TIME PEAK / DRAWDOWN HALT.  Persisted peak record.
# =============================================================================================
class PeakRecord(object):
    """Equity high-water mark, persisted.

    A daily loss limit cannot see a slow bleed: lose 4% a day for ten days and no day trips.
    The drawdown-from-peak is the measure that does, and it must be PERSISTED or every restart
    resets the peak to the current (lower) equity and the drawdown silently becomes zero —
    the bleed erases its own evidence.
    """

    def __init__(self, path=None, max_drawdown_frac=None):
        self.path = path or os.path.join(C.DATA_DIR, "v5_peak.json")
        self.peak = None
        self.max_drawdown_frac = (C.MAX_DRAWDOWN_FRAC if max_drawdown_frac is None
                                  else float(max_drawdown_frac))

    def load(self):
        obj = R.read_json(self.path, default=None)
        if isinstance(obj, dict) and obj.get("peak") is not None:
            self.peak = float(obj["peak"])
        return self

    def observe(self, equity_usd, now, persist=True):
        """Returns (drawdown_frac, breached)."""
        eq = float(equity_usd)
        if self.peak is None or eq > self.peak:
            self.peak = eq
            if persist:
                R.atomic_write_json(self.path, {"peak": self.peak, "ts": float(now)})
            return 0.0, False
        if self.peak <= 0:
            return 0.0, False
        dd = (self.peak - eq) / self.peak
        return dd, dd >= self.max_drawdown_frac


# =============================================================================================
# B4 — DAILY LOSS LIMIT, WITH OPEN-DAY ATTRIBUTION.
# =============================================================================================
def daily_realized_loss(settlements, day_key, open_day_of=None):
    """Realized P&L attributable to `day_key`, attributed by the day the position was OPENED.

    **The attribution is the whole guard.**  A multi-day position settles on ONE day but was a
    decision taken on ANOTHER, and charging its whole loss to the settlement day trips today's
    limit for a bet today never made — halting a healthy book because an old one resolved.
    Symmetrically, a good settlement today must not fund fresh risk today.

    `settlements`: [{"ticker", "realized_pnl", "settled_day", "opened_day"}].
    `open_day_of`: optional fallback lookup for rows lacking `opened_day`.
    """
    total = 0.0
    for s in settlements or []:
        opened = s.get("opened_day")
        if opened is None and open_day_of is not None:
            opened = open_day_of(s.get("ticker"))
        if opened is None:
            opened = s.get("settled_day")          # unknowable ⇒ charge it where it landed
        if opened == day_key:
            total += float(s.get("realized_pnl", 0.0))
    return total


def daily_loss_breached(realized_today, unrealized_today, limit_usd):
    loss = -(float(realized_today) + float(unrealized_today))
    return loss >= float(limit_usd) - 1e-12


# =============================================================================================
# B6 — PERSIST-FAILURE FAIL-CLOSED.
# =============================================================================================
class PersistGuard(object):
    """A write failure while LIVE is a HALT, not a log line.

    Every control in this binary reasons from persisted state — the ledger, the halt record,
    the peak, the cash feed.  If a write fails and we continue, each of those controls is now
    reasoning from a world that diverges further every cycle, and NOTHING detects it.  The
    cheapest correct response is to stop spending.

    MIRROR (fail-closed on a transient ↔ running blind on a real failure): the retry bound is
    the first end — a single fsync hiccup costs a retry, not a halt; the second is that after
    `max_retries` we halt rather than continue, because a persistent write failure is
    indistinguishable from a full disk, and a full disk is how a ledger silently stops being
    the record.
    """

    def __init__(self, halt_state, max_retries=C.PERSIST_MAX_RETRIES):
        self.halt_state = halt_state
        self.max_retries = int(max_retries)
        self.failures = 0

    def write(self, fn, *args, **kwargs):
        """Run `fn`, retrying; halt on persistent failure.  Returns (ok, result_or_error)."""
        last = None
        for attempt in range(self.max_retries):
            try:
                res = fn(*args, **kwargs)
                self.failures = 0
                return True, res
            except Exception as exc:                          # noqa: BLE001 - deliberate
                last = "%s: %s" % (type(exc).__name__, exc)
                R.log("persist_retry", attempt=attempt + 1, err=last)
        self.failures += 1
        if R.is_live():
            self.halt_state.halt("persist_failure", R._now(), {"err": last})
        else:
            R.log("persist_failure_inert", err=last)
        return False, last


# =============================================================================================
# B7 — FRESH-STATE REFUSAL (before G3).
# =============================================================================================
def fresh_state_refusal(ledger_rows, adopt_exists, exchange_positions, allow_flag=False):
    """A BLANK ledger plus (an adopt file OR live exchange positions) is a REFUSAL.

    That combination means exactly one thing: we are about to start quoting as though flat
    while the account is not.  Every cap, the ceiling, the day stop and the cash feed would all
    be computed against zero inventory that demonstrably exists — the invisible-position class,
    entered deliberately on the first cycle instead of drifting into it.

    The escape is an EXPLICIT flag, because there is a legitimate case (a genuinely new
    account) and it should have to be stated rather than inferred from the absence of evidence.
    """
    if allow_flag:
        return None
    if ledger_rows:
        return None
    has_positions = any(abs(float(v)) > 0 for v in (exchange_positions or {}).values())
    if adopt_exists or has_positions:
        return ("blank ledger with %s — refusing to start flat against a non-flat account"
                % ("an adopt file" if adopt_exists else "live exchange positions"))
    return None


# =============================================================================================
# B9 — REFILL / TURNOVER CAP (the 1 Hz fast-path bound).
# =============================================================================================
class RefillTracker(object):
    """`REFILL_CAP_TURNOVERS × n_cap` per (m,s) per window.

    Why this exists ALONGSIDE the §2.5 kill: the kill evaluates every 15 MINUTES, and a slot
    being churned by informed flow can turn over its whole inventory cap many times inside one
    of those buckets.  T̂'s cadence cannot bound a 1 Hz failure; this can.  Beyond the cap the
    slot is a FLOW MAGNET, not a maker, which is a statement about the venue and not about our
    sizing.

    SF-6 (final fix round): the WINDOW is the slot's own PROGRAM PERIOD, not the process
    lifetime.  The prior form reset only on restart — wrong BOTH directions: a long-lived
    process carried yesterday's turnovers into today's period (refusing legitimate refills),
    and a restart amnestied a flow magnet mid-period.  `set_window` keys the count to the
    program's [start, end); fills carry their timestamp so a restart REBUILDS the current
    period's count from ledger replay and DROPS prior periods' — surviving restart in both
    directions.  MIRROR (window too long ↔ too short): too long is the process-lifetime
    defect; too short (per-cycle) would never bind at all — the period is the one window the
    turnover bound's own derivation names (v1 §8.7 "in one window").
    """

    def __init__(self, turnovers=C.REFILL_CAP_TURNOVERS):
        self.turnovers = int(turnovers)
        self.filled = {}                                      # (ticker, side) -> contracts
        self.events = {}                                      # key -> [(ts_or_None, n)]
        self.window_start = {}                                # key -> program period start ts

    def note_fill(self, ticker, side, contracts, ts=None):
        """`ts=None` (tests/manual) counts in EVERY window; live fills and replayed
        `fill_obs` rows pass their timestamp so period boundaries can drop them."""
        k = (ticker, side)
        n = abs(float(contracts))
        self.filled[k] = self.filled.get(k, 0.0) + n
        self.events.setdefault(k, []).append((None if ts is None else float(ts), n))

    def set_window(self, ticker, side, start_ts):
        """Called each cycle with the slot's program-period start.  On a CHANGE (first
        sighting, or a new period), the count is rebuilt from timestamped events at or after
        the new start — untimed (manual) entries always survive."""
        k = (ticker, side)
        start = float(start_ts)
        if self.window_start.get(k) == start:
            return
        self.window_start[k] = start
        ev = [(t, n) for (t, n) in self.events.get(k, []) if t is None or t >= start]
        self.events[k] = ev
        self.filled[k] = sum(n for _, n in ev)

    def cap_for(self, price, n_cap_fn):
        return self.turnovers * n_cap_fn(price)

    def exhausted(self, ticker, side, price, n_cap_fn):
        return self.filled.get((ticker, side), 0.0) >= self.cap_for(price, n_cap_fn)

    def reset_window(self):
        self.filled = {}
        self.events = {}


# =============================================================================================
# B10 — UNKNOWN-ORDER RETRY BOUND.
# =============================================================================================
class UnknownOrders(object):
    """An order left in ST_UNKNOWN holds collateral FOREVER.

    Retry its cancel on a cadence; after `max_retries` book it as FILLED (the conservative
    direction — assume we own it) and FREEZE the market.  Booking it as filled overstates our
    inventory, which costs us capacity; booking it as cancelled would understate it, which
    creates a naked short.  The freeze is because an order we could never resolve is evidence
    about that market, not just about that order.
    """

    def __init__(self, max_retries=C.UNKNOWN_MAX_RETRIES, retry_s=C.UNKNOWN_RETRY_S):
        self.max_retries = int(max_retries)
        self.retry_s = float(retry_s)
        self.pending = {}                    # oid -> {"attempts", "last_ts", "ticker", ...}

    def note(self, oid, ticker, side, remaining, now):
        # `last_ts` starts at `now`, not 0: the cadence is measured from when we LEARNED the
        # order was unknown.  Starting at 0 would make a fresh unknown instantly "due" on a
        # real clock, retrying inside the same second as the cancel that confused us.
        e = self.pending.setdefault(str(oid), {"attempts": 0, "last_ts": float(now),
                                               "ticker": ticker, "side": side,
                                               "remaining": float(remaining)})
        e["remaining"] = float(remaining)
        return e

    def due(self, now):
        return [oid for oid, e in sorted(self.pending.items())
                if float(now) - e["last_ts"] >= self.retry_s
                and e["attempts"] < self.max_retries]

    def attempted(self, oid, now):
        e = self.pending.get(str(oid))
        if e:
            e["attempts"] += 1
            e["last_ts"] = float(now)

    def resolved(self, oid):
        self.pending.pop(str(oid), None)

    def exhausted(self):
        """[(oid, entry)] to book as filled and freeze."""
        return [(oid, e) for oid, e in sorted(self.pending.items())
                if e["attempts"] >= self.max_retries]


# =============================================================================================
# B12 — CLOCK-SKEW CHECK.
# =============================================================================================
def clock_skew_s(server_epoch_s, local_epoch_s):
    return float(local_epoch_s) - float(server_epoch_s)


def clock_skew_alarming(skew_s, tol=C.CLOCK_SKEW_TOL_S):
    """Our signature timestamps and every `expiration_ts` are computed from the LOCAL clock.
    A skewed clock silently produces orders that expire early (lost presence) or late (an order
    living past the window guard), and rejects that look like auth failures.  One unsigned GET
    per cycle costs one request and makes the failure legible."""
    return abs(float(skew_s)) > float(tol)


# =============================================================================================
# B11 — CAPITAL FLOOR.
# =============================================================================================
def capital_floor_ok(available_cash_usd, floor_usd=C.CAPITAL_FLOOR_USD):
    """Below the floor, placement refuses and pages.

    The floor is not about our own sizing — the ceiling already bounds that.  It is about the
    SHARED ACCOUNT: v5 spending the last dollars is v5 deciding, unilaterally, that nestor does
    not get to trade.  Leaving a floor is the same courtesy as the rate budget's residual.
    """
    return float(available_cash_usd) >= float(floor_usd)


# =============================================================================================
# B13 — CROSS-BOT EXCLUSION (positions AND orders — the pair is the guard).
# =============================================================================================
def cross_bot_excluded(ticker, nestor_order_tickers, nestor_position_tickers):
    """v5 never quotes a ticker nestor holds an OPEN ORDER on (spec §11) — and never one it
    holds a POSITION on either.

    The order half alone is not enough, and the asymmetry is the bug: nestor can hold a
    position on a market it currently has no resting order in, and v5 quoting there attributes
    nestor's inventory to itself at the next position reconcile — a divergence that freezes the
    market, or worse, is netted into v5's own caps.  The two halves are one guard.
    """
    t = str(ticker)
    return t in (nestor_order_tickers or set()) or t in (nestor_position_tickers or set())


# =============================================================================================
# B8 — DUPLICATE-FILL GUARD, AT THE STATE LAYER.
# =============================================================================================
class FillDedupe(object):
    """Keyed on the EXCHANGE's own fill id.

    The fills API is queried over OVERLAPPING windows BY CONSTRUCTION (the crash-gap re-read),
    so a restart loop re-observes the same fills; v4 measured `filled_cum` at 20 against a truth
    of 10.  Dedupe belongs at the STATE layer rather than in each reader, because there is more
    than one path into state (live fills, the crash-gap sweep, cancel `reduced_by` learning)
    and a guard that lives in one of them is absent from the others.
    """

    def __init__(self):
        self.seen = set()

    def is_new(self, fill_id, fallback_key=None):
        key = str(fill_id) if fill_id is not None else fallback_key
        if key is None:
            # An unkeyed fill cannot be deduped.  Accept it — dropping a real fill understates
            # inventory, which is the naked-short direction — and surface the count.
            R.log("fill_unkeyed")
            return True
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


# =============================================================================================
# THE ORDERED GATE.  `place()` calls THIS, not the individual guards.
# =============================================================================================
class PlaceContext(object):
    """Everything `place_allowed` needs, gathered once per cycle."""

    def __init__(self, halt_state=None, positions=None, resting_basis=None,
                 nestor_orders=None, nestor_positions=None, available_cash_usd=None,
                 cluster_cap_usd=None, frozen=None, denied_ok=True, refill=None,
                 n_cap_fn=None, day_stopped=False, skew_ok=True,
                 ceiling_usd=None, market_cap_usd=None,
                 portfolio_var_max=None):
        self.halt_state = halt_state
        self.positions = positions or []          # [{ticker, side, n, basis}] OPEN inventory
        self.resting_basis = resting_basis or []  # [{ticker, side, n, basis}] RESTING orders
        self.nestor_orders = nestor_orders or set()
        self.nestor_positions = nestor_positions or set()
        self.available_cash_usd = available_cash_usd
        self.cluster_cap_usd = cluster_cap_usd
        self.frozen = frozen or set()
        self.refill = refill
        self.n_cap_fn = n_cap_fn
        self.day_stopped = day_stopped
        self.skew_ok = skew_ok
        self.ceiling_usd = ceiling_usd            # B15 — the PLACEMENT-time ceiling
        self.market_cap_usd = market_cap_usd      # B16 — per-market acquisition cap
        # B18 — the TRACKED variance tolerance (Ryan's spec): a rail on the aggregate,
        # replacing the per-rung price floor that was standing in for one.
        self.portfolio_var_max = portfolio_var_max


def place_allowed(ctx, order):
    """The rails, in dependency order.  Returns (ok, reason, detail).

    `order`: {ticker, side, n, basis, fully_closing}

    ORDER MATTERS and is derived: the halt is first because a halted book needs no further
    reasoning; the day stop is next because it is the only other condition that shuts
    EVERYTHING; then the exclusions that make a market ineligible at all; then the caps, which
    are about size and therefore only meaningful on a market we were allowed to quote.
    """
    ticker = order["ticker"]
    fully_closing = bool(order.get("fully_closing"))

    # B5 — halt is place()'s FIRST check.  The fully-closing exemption lets a halted book LEAVE.
    if ctx.halt_state is not None and ctx.halt_state.halted:
        if not day_stop_exempt(fully_closing):
            return False, "halted", {"reason": ctx.halt_state.reason}

    # B2 — day stop, same exemption for the same reason.
    if ctx.day_stopped and not day_stop_exempt(fully_closing):
        return False, "day_stop", {}

    # B12 — a skewed clock makes `expiration_ts` and our signatures unreliable.
    if not ctx.skew_ok and not fully_closing:
        return False, "clock_skew", {}

    # Frozen (assume_filled / adoption exclusion / orphan) — quoting AND recycling.
    if ticker in ctx.frozen and not fully_closing:
        return False, "frozen", {}

    # B13 — cross-bot exclusion, orders AND positions.
    if cross_bot_excluded(ticker, ctx.nestor_orders, ctx.nestor_positions):
        return False, "cross_bot", {}

    # DENY_SERIES — measured-toxic venues (charter: lessons as measured inputs).
    if C.series_denied(ticker):
        return False, "series_denied", {}

    # B11 — capital floor: never spend the shared account's last dollars.
    if not fully_closing and ctx.available_cash_usd is not None \
            and not capital_floor_ok(ctx.available_cash_usd):
        return False, "capital_floor", {"available": ctx.available_cash_usd}

    # B9 — refill / turnover cap: the 1 Hz bound the 15-min kill cadence cannot provide.
    # The tracker is keyed on the ORDER axis ("bid"/"ask" — what `book_fill` notes); the
    # order dict speaks the leg axis ("yes"/"no").  Convert, or the guard silently never
    # fires (found by the replenish fixture: 4-turnover churn sailed through).
    if not fully_closing and ctx.refill is not None and ctx.n_cap_fn is not None:
        order_side = "bid" if order["side"] == "yes" else "ask"
        if ctx.refill.exhausted(ticker, order_side, order["basis"], ctx.n_cap_fn):
            return False, "refill_cap", {}

    # B1 — the cluster cap, on OPEN + RESTING basis, before the ceiling.
    if not fully_closing and ctx.cluster_cap_usd is not None:
        existing = list(ctx.positions) + list(ctx.resting_basis)
        ok, reason, detail = CL.cluster_admits(existing, order, ctx.cluster_cap_usd)
        if not ok:
            return False, reason, detail

    # -----------------------------------------------------------------------------------------
    # B16 — THE PER-MARKET ACQUISITION CAP.  (2026-07-29)
    # WHY IT EXISTS: we do not exit.  Measured on the full operation's tape, 149 of 6,149
    # acquired contracts were ever closed (2.4%), across SEVEN closing orders in the whole
    # history, all of them takers.  With no exit, NET exposure equals GROSS, so the only
    # instrument that bounds directional risk in a market is refusing to acquire more of it.
    # The cluster cap above bounds a SETTLE SOURCE (correlated group); this bounds ONE market,
    # which is what the unmatched-leg loss was denominated in — decomposing the settled book,
    # matched pairs earned +$39.63 (+6.88c/pair) and the unmatched residual lost -$587.42.
    # Breadth cannot fix that: thirty markets each fully net-long is thirty directional bets.
    # MIRROR (cap too LOW ↔ too high): too low forfeits credit by refusing to reach the $1.00
    # payout floor in a market that would have paid — bounded, visible as `market_cap` refusals
    # against the accrual, and recoverable next period.  Too high is the -$587: an unbounded
    # one-sided position in a single market with no way out of it.
    # ── B18 — THE TRACKED PORTFOLIO VARIANCE (Ryan's specification, 2026-07-29 night). ──────
    # "instead of a hard cap just track our average variance and make sure its above that."
    # This is the ruin instrument, and it REPLACES the price floor that was standing in for one.
    # A per-rung price cap cannot see variance: 200 markets at 2c and 30 at 12c sit at the SAME
    # V ≈ 0.245, so price only carries variance information together with breadth.  V reads the
    # breadth off the actual book instead of assuming it, which is also why it does not
    # rediscover `p_min = k/bankroll` (note 47 §6) — that error came from fixing N.
    # NO "ONLY IF IT WORSENS V" CLAUSE, and the first cut had one — it was DEAD CODE.  With
    # weights denominated in the CEILING, every added dollar raises Σwᵢ² and therefore raises V:
    # there is no such thing as a diluting ORDER, only a diluting SETTLEMENT or shed.  A condition
    # that can never be false is not a safeguard, it is a comment that reads like one.
    # SO CAN THE BOOK GET STUCK ABOVE TOLERANCE?  No: `fully_closing` is exempt above, so the shed
    # path always runs, and a position that settles leaves the book on its own.  The rail stops us
    # ADDING to a concentrated book, which is the whole ask.
    if not fully_closing and ctx.portfolio_var_max is not None:
        _den = ctx.ceiling_usd
        v_now, _ = portfolio_variance(ctx.positions, ctx.resting_basis,
                                      denominator_usd=_den)
        v_next, n_eff = portfolio_variance(ctx.positions, ctx.resting_basis, order,
                                           denominator_usd=_den)
        if v_next > float(ctx.portfolio_var_max) + 1e-12:
            return False, "portfolio_var", {"v_now": round(v_now, 4),
                                            "v_next": round(v_next, 4),
                                            "n_eff": round(n_eff, 2),
                                            "max": float(ctx.portfolio_var_max)}

    # ── D2: THE CAP IS PER LEG, NOT PER MARKET-GROSS.  THE DERIVATION. ──────────────────────
    # First cut summed BOTH legs of a ticker with no side test.  That is the wrong measure, and
    # it is wrong in the direction that contradicts this guard's own stated purpose.
    # WHAT A BINARY CAN ACTUALLY LOSE.  Exactly one outcome pays.  If YES settles, every yes
    # contract pays $1 (they cannot lose) and only the NO-side collateral is lost; if NO settles,
    # only the YES-side collateral is lost.  So
    #     worst-case market loss = max(yes_side_collateral, no_side_collateral)
    # and bounding EACH SIDE at the cap bounds the market's worst case at the cap EXACTLY.  The
    # gross sum bounds the same quantity at the same number while CHARGING TWICE for it, and what
    # it over-charges is precisely the two-sided book.
    # WHY THAT MATTERS HERE.  Decomposing the settled tape, MATCHED PAIRS EARNED +$39.63
    # (+6.88c/pair) AND THE UNMATCHED RESIDUAL LOST -$587.42 (90% of the loss).  A gross cap
    # reaches its limit fastest on a fully matched book — the only configuration that made money —
    # and never distinguishes it from thirty one-sided bets, which is what the cap exists to
    # refuse.  Per-leg is the measure the loss was denominated in.
    # AND IT IS THE OTHER HALF OF D2.  At a $300 ceiling `slot_cap_usd` is $30 and this cap is
    # $30, so under a gross sum ONE FULL BID LEG REFUSES THE ASK OUTRIGHT: `place()` returns
    # False, no degrade arms (the cancel-first path latches only on an exchange `insufficient
    # balance` reject), and the slot re-offers the same refused order every cycle forever.
    # Per-leg cannot deadlock that way, because `slot_cap <= market_cap` is enforced in
    # `config.market_leg_cap_usd` and one leg is one slot.
    # MIRROR (per-leg too permissive ↔ gross too tight): per-leg admits a market holding the cap
    # on BOTH sides — which is a box, whose worst case is still one side's collateral, and whose
    # >$1.00-sum failure mode is `joint_sub_dollar` above and one-rung-per-side upstream, not this
    # cap.  Total dollars remain bound by the cluster cap and by B15's ceiling.
    if not fully_closing and ctx.market_cap_usd is not None:
        leg = order.get("side")
        held = sum(float(p.get("n", 0)) * float(p.get("basis", 0.0))
                   for p in list(ctx.positions) + list(ctx.resting_basis)
                   if p.get("ticker") == ticker and p.get("side") == leg)
        add = float(order.get("n", 0)) * float(order.get("basis", 0.0))
        if held + add > float(ctx.market_cap_usd) + 1e-9:
            return False, "market_cap", {"held": round(held, 4), "add": round(add, 4),
                                         "side": leg, "cap": float(ctx.market_cap_usd)}

    # -----------------------------------------------------------------------------------------
    # B15 — THE COLLATERAL CEILING, ENFORCED AT PLACEMENT.  (2026-07-29)
    # The ceiling was previously a PLAN-TIME budget only: `engine` computes
    # `reserve_budget(ceiling - inventory_basis, slot_cap)` once per allocation cycle and the
    # allocator plans inside it.  Nothing downstream re-checked it, so the authorised number was
    # only as accurate as `cash.inventory_basis` — and measured on the tape, resting notional
    # reached p99 $1,358 and a peak of $6,077 against a ledger-declared ceiling of $45.  A limit
    # that binds a plan and not an order is not a limit; it is an intention.
    # DERIVATION OF THE PLACE: last, because it is the only guard denominated in the WHOLE
    # book — every cheaper refusal above should fire first so the ledger reason names the
    # specific cause rather than the aggregate one.
    # MIRROR (ceiling too tight ↔ too loose): too tight starves the book and is visible as
    # `ceiling` refusals while `idle_capital` alerts — the exact failure we measured on
    # 2026-07-28 when $5.84 of $300 deployed.  Too loose is unbounded exposure on a shared
    # account, which is what this closes.  The fully-closing exemption is mandatory: a book at
    # its ceiling must always be able to LEAVE.
    if not fully_closing and ctx.ceiling_usd is not None:
        committed = sum(float(p.get("n", 0)) * float(p.get("basis", 0.0))
                        for p in list(ctx.positions) + list(ctx.resting_basis))
        add = float(order.get("n", 0)) * float(order.get("basis", 0.0))
        if committed + add > float(ctx.ceiling_usd) + 1e-9:
            return False, "ceiling", {"committed": round(committed, 4), "add": round(add, 4),
                                      "ceiling": float(ctx.ceiling_usd)}

    return True, "ok", {}
