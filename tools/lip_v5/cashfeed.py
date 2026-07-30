"""
lip_v5.cashfeed — the COMPUTED cash feed (spec §5, charter "derives fresh" #1).

v4 + nestor share one account through a hand-patched external-cash band that HALTED NESTOR
FOUR TIMES IN 24 H.  This file is the answer, and it has exactly one invariant:

    **v5's published expected-cash is NEVER ABOVE THE TRUTH, only below.**  (T-C2)

Two mechanisms produce it, and neither is optional:

 1. **Write BEFORE the wire call (§5.3).**  The feed is written and fsync'd with the pending
    order's collateral ALREADY INCLUDED before any cash-consuming POST, and corrected after
    the response.  Cost: one fsync+rename (~1 ms) against a ~100 ms HTTP call.

 2. **Release `settled_awaiting_payout` on CASH CONFIRMATION, never on result (§5.2a,
    BLOCKER-1).**  R171 measured a 41-MINUTE settlement-index lag.  Releasing on result raises
    v5's published expected-cash before the real dollars land, and nestor's breaker reads
    exactly that as MISSING MONEY — **v5 would halt nestor through the very interface built to
    stop v5 halting nestor.**  The sign is the same as the four hand-patched halts; only the
    author would have changed.

Why a SECOND file (§5.1): nestor's breaker SUMS every line of `data/external_cash.jsonl`
(`reconcile.rs:848`).  A cumulative computed value appended there would double-count against
the operator's hand rows, and two writers on one file is a collision.  So v5 owns
`data/lip_cash_feed.json` — a SINGLE JSON object, rewritten atomically.
"""

import os

from . import config as C
from . import runtime as R


class PendingSettlement(object):
    """A market that has RESOLVED but whose credit has not been confirmed IN CASH.

    `baseline_balance` / `baseline_delta` are captured together, and BOTH are needed — see
    `CashState.observe_balance` for why a balance delta alone is not evidence of a credit.
    """

    __slots__ = ("ticker", "basis_usd", "expected_credit_usd", "resolved_ts",
                 "baseline_balance", "baseline_delta", "paged")

    def __init__(self, ticker, basis_usd, expected_credit_usd, resolved_ts,
                 baseline_balance=None, baseline_delta=None):
        self.ticker = ticker
        self.basis_usd = float(basis_usd)
        self.expected_credit_usd = float(expected_credit_usd)
        self.resolved_ts = float(resolved_ts)
        self.baseline_balance = baseline_balance
        self.baseline_delta = baseline_delta
        self.paged = False


class CashState(object):
    """Every component of spec §5.2, and the ONLY writer of them.

    MIRROR (cash-feed SPEND ↔ refund/credit): the spend side moves `delta_dollars` (the
    breaker's tight, dangerous direction); the credit side moves `pending_payout_dollars`,
    which widens the POSITIVE side ONLY.  A credit may never tighten the negative side, which
    is why `rewards_accrued_unpaid` and `inventory_settle_max` never appear in `delta_dollars`.
    """

    def __init__(self, mode=C.CASH_MODE_SHARED, ceiling_usd=C.MAX_TOTAL_COLLATERAL_USD):
        self.mode = mode
        self.ceiling_usd = float(ceiling_usd)
        self.seq = 0
        # components
        self.resting_by_order = {}          # oid -> collateral $ (includes in-flight)
        self.inventory = {}                 # ticker -> {"n": contracts, "basis": $/contract}
        self.pending = {}                   # ticker -> PendingSettlement
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.rewards_accrued_unpaid = 0.0
        self.settled_payout_expected = 0.0
        self.last_balance = None
        self.last_exchange_delta = None     # captured WITH last_balance, same instant
        self.inflight = {}                  # oid -> collateral $ reserved pre-POST

    # -- components ---------------------------------------------------------------------
    @property
    def resting_collateral(self):
        return sum(self.resting_by_order.values()) + sum(self.inflight.values())

    @property
    def inventory_basis(self):
        return sum(abs(v["n"]) * v["basis"] for v in self.inventory.values())

    @property
    def settled_awaiting_payout(self):
        return sum(p.basis_usd for p in self.pending.values())

    @property
    def inventory_settle_max(self):
        """`Σ n × $1.00` over UNSETTLED inventory — the LARGEST credit that could land
        unannounced.  It widens the positive side only, so over-stating it is safe and
        under-stating it is what produces a false halt on a genuine settlement."""
        return sum(abs(v["n"]) * 1.00 for v in self.inventory.values())

    @property
    def max_inflight_usd(self):
        return sum(self.inflight.values())

    # -- the two published numbers ------------------------------------------------------
    @property
    def raw_delta(self):
        """`−(resting_collateral + inventory_basis + settled_awaiting_payout) + realized_pnl
        − fees_paid` — v5's ACTUAL cash position, always computed, never zeroed.

        Kept separate from `delta_dollars` because §5.5's subaccount zeroing is a PUBLICATION
        rule, not an accounting one.  Zeroing the internal number too would silently break
        every calculation that reasons about our own cash movements — the settlement-
        confirmation arithmetic in `observe_balance` most of all, which would then read our own
        spending as unexplained cash and confirm credits that had not landed.  Publication
        zeroes; accounting never does.
        """
        return (-(self.resting_collateral + self.inventory_basis +
                  self.settled_awaiting_payout) + self.realized_pnl - self.fees_paid)

    @property
    def delta_dollars(self):
        """What we PUBLISH — ADD to nestor's expected_cash."""
        if self.mode == C.CASH_MODE_SUBACCOUNT:
            return 0.0                      # §5.5 — the wall replaces the feed
        return self.raw_delta

    @property
    def pending_payout_dollars(self):
        """`rewards_accrued_unpaid + inventory_settle_max + settled_payout_expected` — widens
        the POSITIVE side only."""
        if self.mode == C.CASH_MODE_SUBACCOUNT:
            return 0.0
        return (self.rewards_accrued_unpaid + self.inventory_settle_max +
                self.settled_payout_expected)

    @property
    def exchange_delta(self):
        """`delta_dollars` restricted to movements THE EXCHANGE HAS ACTUALLY MADE.

        §5.3 has us publish an order's collateral BEFORE the POST, so `delta_dollars`
        deliberately runs ahead of the exchange by the in-flight amount.  That head start is
        exactly right for the breaker (it is the conservative direction) and exactly WRONG as
        a baseline for settlement confirmation: an in-flight reservation would look like cash
        we had spent but the exchange had not taken, inflating the "unexplained" balance and
        confirming a credit that has not landed.  So confirmation arithmetic adds the in-flight
        amount back.
        """
        return self.raw_delta + self.max_inflight_usd

    # -- wire events --------------------------------------------------------------------
    def reserve_order(self, oid, collateral_usd):
        """§5.3 — called BEFORE the cash-consuming POST.  The collateral is published as
        already spent, so between this call and the exchange's acknowledgement the published
        number is BELOW the truth, which is the safe side."""
        self.inflight[oid] = float(collateral_usd)

    def confirm_order(self, oid, collateral_usd=None):
        """After the POST response: move in-flight to resting, correcting the amount."""
        amt = self.inflight.pop(oid, 0.0)
        self.resting_by_order[oid] = float(collateral_usd) if collateral_usd is not None else amt

    def reject_order(self, oid):
        """The POST failed: release the reservation.  Published rises back to the truth."""
        self.inflight.pop(oid, None)

    def release_order(self, oid, released_usd=None):
        """A CONFIRMED cancel or a terminal order: release its resting collateral.  Called
        only after the cancel response, never optimistically — an optimistic release raises
        published expected-cash before the exchange has refunded, breaking the invariant."""
        cur = self.resting_by_order.get(oid, 0.0)
        if released_usd is None or float(released_usd) >= cur:
            self.resting_by_order.pop(oid, None)
        else:
            self.resting_by_order[oid] = cur - float(released_usd)

    def fill(self, ticker, oid, contracts, unit_collateral_usd, side_sign=1.0,
             proceeds_per_contract=None):
        """A fill.  TWO CASES, and conflating them was BLOCKER-A.

        **OPENING** (`side_sign > 0`): resting collateral becomes inventory basis.
        `delta_dollars` is UNCHANGED by construction — both terms sit inside the same negative
        sum — which is exactly right, because the exchange took the cash at PLACEMENT, not at
        fill.

        **CLOSING** (`side_sign < 0`, i.e. a shed or any disposal): `n` contracts leave
        inventory at basis `b` and the exchange returns `n × proceeds` in cash.  The basis
        leaving raises `delta_dollars` by `n·b`, but the cash that actually arrived is
        `n·proceeds` — so WITHOUT realizing the P&L the published number rises by the basis
        instead of by the proceeds, and on any shed at a LOSS that is a rise ABOVE THE TRUTH.

        Worked, from the review: shed 10 @ $0.20 against a $0.40 basis.  Basis leaving is
        $4.00; cash arriving is $2.00.  Booking only the basis publishes +$4.00 against a true
        +$2.00 — $2.00 above truth, on the FIRST TRIAGE EXIT.  Realizing
        `n·(proceeds − basis) = −$2.00` makes the net movement `+4.00 − 2.00 = +2.00`, exactly
        the cash received.

        MIRROR (a close booked too RICH ↔ too POOR): too rich is the above and is forbidden;
        too poor only understates us, so `proceeds_per_contract=None` falls back to
        `unit_collateral_usd` — the price we transacted at — rather than to anything optimistic.
        """
        n = float(contracts)
        sign = float(side_sign)
        unit = float(unit_collateral_usd)

        if sign >= 0:
            cost = n * unit
            cur = self.resting_by_order.get(oid, 0.0)
            self.resting_by_order[oid] = max(0.0, cur - cost)
            if self.resting_by_order[oid] <= 1e-12:
                self.resting_by_order.pop(oid, None)
            inv = self.inventory.setdefault(ticker, {"n": 0.0, "basis": 0.0})
            prev_total = inv["n"] * inv["basis"]
            inv["n"] += n
            inv["basis"] = ((prev_total + cost) / abs(inv["n"])) if abs(inv["n"]) > 1e-12 \
                else 0.0
            return 0.0

        proceeds = unit if proceeds_per_contract is None else float(proceeds_per_contract)
        inv = self.inventory.get(ticker)
        held = abs(inv["n"]) if inv else 0.0
        closing = min(n, held)
        realized = 0.0
        if closing > 0:
            basis = inv["basis"]
            inv["n"] -= closing * (1.0 if inv["n"] > 0 else -1.0)
            realized = closing * (proceeds - basis)
            self.realized_pnl += realized
            if abs(inv["n"]) <= 1e-12:
                self.inventory.pop(ticker, None)
        excess = n - closing
        if excess > 0:
            # Closing more than we hold FLIPS the position: the tail is an OPENING fill on the
            # other leg.  Booking it as opening adds basis, i.e. MORE consumption — the safe
            # direction — rather than manufacturing proceeds we did not receive.
            inv2 = self.inventory.setdefault(ticker, {"n": 0.0, "basis": 0.0})
            prev_total = inv2["n"] * inv2["basis"]
            cost = excess * unit
            inv2["n"] -= excess
            inv2["basis"] = ((prev_total + cost) / abs(inv2["n"])) if abs(inv2["n"]) > 1e-12 \
                else 0.0
        return realized

    def accrue_reward(self, usd):
        self.rewards_accrued_unpaid += float(usd)

    def reward_paid(self, usd):
        """N3 — MIRROR (accrue ↔ pay): a credit that LANDS retires the accrued-unpaid claim
        it satisfies, or the positive band widens forever and every later credit looks
        smaller than the pending it should extinguish.  Floored at zero: a credit larger
        than the claim is the exchange's statement, not a debt of ours."""
        self.rewards_accrued_unpaid = max(0.0, self.rewards_accrued_unpaid - float(usd))

    def pay_fee(self, usd):
        self.fees_paid += float(usd)

    def resolve(self, ticker, expected_credit_usd, now):
        """§5.2a — the market RESOLVED.  Its basis moves from `inventory_basis` into
        `settled_awaiting_payout` and **STAYS INSIDE `delta_dollars` AS CONSUMED CASH.**

        `delta_dollars` is deliberately UNCHANGED here.  That is the whole of BLOCKER-1: the
        naive implementation releases at this moment and fails T-C2 at minute 0.
        """
        inv = self.inventory.pop(ticker, None)
        basis_usd = (abs(inv["n"]) * inv["basis"]) if inv else 0.0
        existing = self.pending.get(ticker)
        if existing is not None:
            # MERGE, never replace.  `pending` is keyed by ticker, so a second resolve on a
            # ticker that already has an unconfirmed credit would otherwise DROP the first
            # entry's basis out of `delta_dollars` — publishing expected-cash above the truth,
            # which is the one thing this file exists to prevent.  Merging keeps both consumed
            # amounts booked and defers release until the combined credit is confirmed.  The
            # baseline is deliberately NOT reset: the older, earlier baseline is the more
            # conservative of the two.
            existing.basis_usd += basis_usd
            existing.expected_credit_usd += float(expected_credit_usd)
            existing.resolved_ts = min(existing.resolved_ts, float(now))
            self.settled_payout_expected += float(expected_credit_usd)
            return basis_usd
        p = PendingSettlement(ticker, basis_usd, float(expected_credit_usd), float(now))
        self.pending[ticker] = p
        self.settled_payout_expected += float(expected_credit_usd)
        if self.last_balance is not None and self.last_exchange_delta is not None:
            # The baseline is a PAIR and both halves must come from the SAME INSTANT.  Taking
            # `last_balance` from an old read while taking the delta from now mixes two clocks:
            # any cash the exchange returned in between (a cancel refund) then reads as
            # unexplained, and confirms a credit that has not landed.  Only the pair recorded
            # together by `observe_balance` is admissible; otherwise the baseline stays None
            # and the next balance read establishes it.
            p.baseline_balance = self.last_balance
            p.baseline_delta = self.last_exchange_delta
        return basis_usd

    def observe_balance(self, balance_usd, now=None):
        """A verify-lane balance read.  Releases a pending settlement iff the balance shows
        UNEXPLAINED cash of at least the expected credit (§5.2a's first release path).

        **Why a raw balance increase is NOT evidence.**  §5.2a says "shows an increase ≥ the
        expected credit", and read literally that is unsafe: a cancel REFUND raises the balance
        by real dollars that have nothing to do with the settlement, so a busy requote cycle
        would "confirm" a credit that has not landed and publish expected-cash above the truth —
        the exact BLOCKER-1 failure arriving through a different door.  (Caught by T-C2's random
        sequence, which is why that test exists.)

        v5 already KNOWS its own cash movements: they are `delta_dollars`, published to the
        cent.  So the confirmable quantity is the balance change NOT explained by them:

            expected_without_credit = baseline_balance + (delta_now − baseline_delta)
            unexplained             = balance_now − expected_without_credit
            release iff unexplained ≥ expected_credit

        A cancel refund raises `balance_now` and `delta_now` by the same amount and cancels out
        exactly, leaving only genuinely exogenous cash — which, for a resolved position, is the
        credit.

        Items are processed ONE AT A TIME with `delta_now` recomputed each step: releasing item
        A raises `delta`, and reusing a pre-release `delta` for item B would understate B's
        `expected_without_credit` and make B EASIER to release — the unsafe direction.

        **BLOCKER-B — this path is DISABLED IN SHARED MODE.**  The arithmetic above nets out
        v5's OWN cash movements, and that is all it can net out.  In the shared account nestor
        is trading the same balance, and ITS settlement credits are exogenous to v5's
        `delta_dollars` in exactly the way a real credit is — so nestor being paid confirms v5's
        pending settlement.  There is no correction available from inside v5: the whole method
        is "cash we cannot explain", and in a shared account there is a second explanation v5
        cannot see.

        So while `mode == "shared"` the `/portfolio/settlements` row is the ONLY release path.
        It is exact (it carries the paid amount for OUR ticker) and it needs no inference.  The
        balance path is reserved for `mode == "subaccount"`, where the account holds nothing
        but v5's own activity and "unexplained" genuinely means unexplained.

        Cost of the restriction: releases wait for the settlements row instead of the next
        balance read.  That direction only makes v5 look POORER than it is, which is the safe
        side and the same reason the 6 h timeout pages rather than releases.

        MIRROR (releasing too EARLY ↔ too LATE): early is the halt-nestor direction and is
        structurally forbidden — by the inequality above, and in shared mode by refusing the
        inference entirely.  Late is bounded by `settlement_cash_unconfirmed`.
        """
        bal = float(balance_usd)
        released = []
        if self.mode == C.CASH_MODE_SHARED:
            # Record the balance for the components/telemetry, confirm NOTHING.
            self.last_balance = bal
            self.last_exchange_delta = self.exchange_delta
            for ticker in sorted(self.pending):
                p = self.pending[ticker]
                if p.baseline_balance is None:
                    p.baseline_balance = bal
                    p.baseline_delta = self.exchange_delta
            return released
        for ticker in sorted(self.pending):
            p = self.pending.get(ticker)
            if p is None:
                continue
            if p.baseline_balance is None:
                # First read since the resolve: capture the baseline, confirm nothing.  A
                # missing baseline is missing evidence, and missing evidence never releases.
                p.baseline_balance = bal
                p.baseline_delta = self.exchange_delta
                continue
            expected_wo_credit = p.baseline_balance + (self.exchange_delta - p.baseline_delta)
            if bal - expected_wo_credit >= p.expected_credit_usd - 1e-9:
                self._release(ticker)
                released.append(ticker)
        self.last_balance = bal
        self.last_exchange_delta = self.exchange_delta
        return released

    def settlement_row(self, ticker, paid_usd):
        """`/portfolio/settlements` row carrying the PAID amount — §5.2a's second release
        path, and the exact one.  A row that reports a paid amount is cash confirmation."""
        if ticker not in self.pending:
            return False
        if float(paid_usd) <= 0:
            return False
        self._release(ticker, float(paid_usd))
        return True

    def settlement_zero(self, ticker):
        """The settlements record whose paid amount is EXPLICITLY ZERO — a LOST position.

        NOT the same door as `settlement_row(t, 0.0)`, and the split is the safety: a zero
        that arrives by PARSING (a row whose revenue field is missing, defaulted to 0)
        must never release a winner's basis before its cash lands, so `settlement_row`
        refuses ≤ 0.  The caller of THIS method states that the exchange's own row carries
        the zero — and a zero credit needs no cash confirmation, because there is no cash
        to wait for: the money is gone, and the only honest bookkeeping is to say so NOW.

        T-C2 holds by arithmetic, not by timing: the basis leaves the consumed sum
        (+basis to `delta_dollars`) and the SAME basis lands in `realized_pnl` as loss
        (−basis), so the published number does not move.  Deferring the release would not
        be conservative, it would be a phantom `settled_awaiting_payout` claim that pages
        `settlement_cash_unconfirmed` about cash that is never coming."""
        if ticker not in self.pending:
            return False
        self._release(ticker, 0.0)
        return True

    def restore_pending(self, ticker, basis_usd, expected_credit_usd, resolved_ts):
        """Restart replay's half of `resolve()`: rebuild a settled-awaiting-payout claim
        from its `settlement` ledger row.  The basis comes from the ROW, never from
        inventory — replay has already zeroed the position (cutover.V4Positions), so
        calling `resolve()` here would book a basis of $0 and let `delta_dollars` rise by
        the real basis with no cash confirmed, the one forbidden direction.  No balance
        baseline is restored: a baseline pair must come from one instant observed by THIS
        process (`observe_balance`'s rule), and missing evidence never releases."""
        p = PendingSettlement(ticker, float(basis_usd), float(expected_credit_usd),
                              float(resolved_ts))
        existing = self.pending.get(ticker)
        if existing is not None:
            # Same MERGE rule as resolve(): replace would drop the first claim's basis
            # out of delta_dollars.
            existing.basis_usd += p.basis_usd
            existing.expected_credit_usd += p.expected_credit_usd
            existing.resolved_ts = min(existing.resolved_ts, p.resolved_ts)
        else:
            self.pending[ticker] = p
        self.settled_payout_expected += float(expected_credit_usd)
        return p

    def _release(self, ticker, paid_usd=None):
        p = self.pending.pop(ticker, None)
        if p is None:
            return
        credit = p.expected_credit_usd if paid_usd is None else float(paid_usd)
        self.settled_payout_expected = max(0.0, self.settled_payout_expected -
                                           p.expected_credit_usd)
        # The basis leaves the consumed-cash sum and the P&L on the position is realized in
        # the same step, so `delta_dollars` rises by exactly the credit that landed.
        self.realized_pnl += credit - p.basis_usd

    def unconfirmed_overdue(self, now, timeout_s=C.SETTLEMENT_CASH_TIMEOUT_S):
        """§5.2a — still unreleased after 6 h ⇒ page `settlement_cash_unconfirmed`.
        **NEVER auto-release on a timer.**  Returns the tickers to page about (once each)."""
        out = []
        for ticker in sorted(self.pending):
            p = self.pending[ticker]
            if not p.paged and float(now) - p.resolved_ts >= float(timeout_s):
                p.paged = True
                out.append(ticker)
        return out

    # -- publication --------------------------------------------------------------------
    def components(self):
        return {
            "resting_collateral": round(self.resting_collateral, 6),
            "inventory_basis": round(self.inventory_basis, 6),
            "settled_awaiting_payout": round(self.settled_awaiting_payout, 6),
            "realized_pnl": round(self.realized_pnl, 6),
            "fees_paid": round(self.fees_paid, 6),
            "rewards_accrued_unpaid": round(self.rewards_accrued_unpaid, 6),
            "inventory_settle_max": round(self.inventory_settle_max, 6),
            "settled_payout_expected": round(self.settled_payout_expected, 6),
        }

    def feed(self, now=None, heartbeat_s=C.CASH_FEED_HEARTBEAT_S):
        """spec §5.2's schema.  §5.5: `mode:"subaccount"` publishes ZEROS for the two numbers
        while still publishing `components` and the heartbeat — the wall replaces the feed, but
        the STALENESS MONITOR (hence the alarm chain) survives cutover.  A silent
        disappearance of the alarm at the moment of an account-structure change is exactly the
        class we keep paying for."""
        self.seq += 1
        return {
            "schema": C.CASH_FEED_SCHEMA,
            "ts": float(now if now is not None else R._now()),
            "seq": self.seq,
            "process": "lip_v5",
            "pid": os.getpid(),
            "mode": self.mode,
            "delta_dollars": round(self.delta_dollars, 6),
            "pending_payout_dollars": round(self.pending_payout_dollars, 6),
            "components": self.components(),
            "ceiling_usd": self.ceiling_usd,
            "max_inflight_usd": round(self.max_inflight_usd, 6),
            "heartbeat_s": heartbeat_s,
        }

    def zeroed_feed(self, now=None):
        """§5.4 MIRROR (stale ↔ absent): an ABSENT file is (0,0), which is correct ONLY if v5
        is truly flat.  So the SIGTERM path writes a final ZEROED feed AFTER cancel-all + shed,
        and only then may the file be removed.  A `-9` kill leaves the last conservative value
        plus the staleness page."""
        f = self.feed(now)
        f["delta_dollars"] = 0.0
        f["pending_payout_dollars"] = 0.0
        f["zeroed"] = True
        return f


class CashFeedPublisher(object):
    """Owns the file.  ONE writer per file (spec §11 Collisions)."""

    def __init__(self, path=None, state=None, atomic_write=None):
        self.path = path or C.CASH_FEED_PATH
        self.state = state
        self._write = atomic_write or R.atomic_write_json
        self.last_publish_ts = None

    def publish(self, now=None):
        f = self.state.feed(now)
        self._write(self.path, f)
        self.last_publish_ts = f["ts"]
        R.log("cash_feed", seq=f["seq"], delta_dollars=f["delta_dollars"],
              pending_payout_dollars=f["pending_payout_dollars"], mode=f["mode"])
        return f

    def publish_before_wire(self, oid, collateral_usd, now=None):
        """§5.3 — the derived answer to four hand-patched halts in 24 h.  Reserve, publish
        (fsync'd), THEN the caller may POST."""
        self.state.reserve_order(oid, collateral_usd)
        return self.publish(now)

    def publish_zeroed(self, now=None):
        f = self.state.zeroed_feed(now)
        self._write(self.path, f)
        return f

    def due(self, now, heartbeat_s=C.CASH_FEED_HEARTBEAT_S):
        return self.last_publish_ts is None or \
            (float(now) - self.last_publish_ts) >= float(heartbeat_s)


# =============================================================================================
# THE NESTOR-SIDE CONTRACT, implemented here so v5's tests own it too (spec §5.4).
# =============================================================================================
def read_feed_for_reader(path, now, stale_s=C.CASH_FEED_STALE_S, last_good=None):
    """What nestor's G0 reader does, expressed once, in the language of the writer.

    If `now − ts > 120 s` (4 × heartbeat: survives one miss plus jitter): page
    `lip_cash_feed_stale`, **KEEP using the last value** (it is conservative by §5.3), do NOT
    halt.  Halting on a stale feed would convert v5 DYING into nestor DYING.

    Returns (delta, pending, status).
    """
    obj = R.read_json(path, default=None)
    if obj is None:
        # MIRROR (stale ↔ absent): absent is (0,0) — correct only if v5 is truly flat, which
        # v5's SIGTERM path guarantees by writing a zeroed feed before the file may be removed.
        return 0.0, 0.0, "absent"
    if obj.get("schema") != C.CASH_FEED_SCHEMA:
        return (last_good or (0.0, 0.0))[0], (last_good or (0.0, 0.0))[1], "schema_mismatch"
    delta = float(obj.get("delta_dollars", 0.0))
    pending = float(obj.get("pending_payout_dollars", 0.0))
    if float(now) - float(obj.get("ts", 0.0)) > float(stale_s):
        return delta, pending, "stale"
    return delta, pending, "ok"


def startup_refusal_reason(mode, reader_enabled):
    """spec §4.4 MIRROR (v5 stops PUBLISHING ↔ nestor stops CONSUMING) and §7's G1 read-out.

    `mode:"shared"` with the reader DISABLED is a **STARTUP REFUSAL** — an unconsumed feed is
    a silent regression to the hand ledger, i.e. back to the four halts.  Not a warning: a
    warning is what a silent regression looks like from the inside.
    """
    if mode == C.CASH_MODE_SHARED and not reader_enabled:
        return ("cash feed mode=shared but nestor's %s is FALSE: an unconsumed feed is a "
                "silent regression to the hand ledger" % C.NESTOR_READER_FLAG_ENV)
    return None
