"""
lip_v5.ratelimit — the rate budget as a SCHEDULED RESOURCE (spec §3, charter "derives fresh"
#2).

The constraint is SHARED and INVISIBLE: ~10 req/s observed, 429s at 05:58Z on 2026-07-27 with
nestor + LIP pollers both running, and we cannot tell whose 429 it is.  Three consequences,
all derived:

 * **v5 takes the RESIDUAL, not half** (§3.1).  nestor's calls are trade-critical and
   un-deferrable (signal → order); v5's are presence-maintaining and deferrable (the WS carries
   the book).  Under a shared constraint the deferrable consumer yields.
 * **AIMD** (§3.2).  Multiplicative decrease guarantees we yield FASTER THAN WE TAKE, which is
   the charter's "degrade breadth before degrading another bot's calls" expressed as a policy
   rather than a hope.
 * **Strict priority from ONE bucket** (§3.3), not a partition — a partition would waste the
   reserve when idle.
"""

from . import config as C
from . import runtime as R


# The cancel-share bound is "1 in 4"; below 4 admitted requests in the window it is not
# MEASURABLE, and enforcing it there would refuse the FIRST cancel of every window — a
# starvation weapon of the opposite sign to the one SF-1 names.  DERIVED from the ratio itself.
CANCEL_SHARE_MIN_SAMPLES = 4


class Bucket(object):
    """Token bucket, capacity 8 (2 s of burst — one requote round trip), refill rate `B`.

    MIRROR (rate CEILING ↔ rate FLOOR): the ceiling is `cap_hz` and AIMD's decrease; the floor
    is `RATE_MIN_HZ` plus the `rate_starved` alert, because SILENT PERMANENT YIELDING IS
    INDISTINGUISHABLE FROM A DEAD BOT.  Both ends are alarmed; neither is silent.
    """

    def __init__(self, now, cap_hz=C.RATE_CAP_HZ, capacity=C.RATE_BURST_TOKENS,
                 min_hz=C.RATE_MIN_HZ):
        self.cap_hz = float(cap_hz)
        self.min_hz = float(min_hz)
        self.b = float(cap_hz)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.last = float(now)
        self.hold_until = 0.0
        self.last_increase = float(now)
        self.starved_since = None
        self.starved_alerted = False
        self.yield_events = 0
        self.admitted = []                  # [(ts, lane)] rolling, for the cancel-share bound
        self.cancel_breaches = []           # [(ts, key)]
        self.refused = 0

    # -- clock -------------------------------------------------------------------------
    def refill(self, now):
        dt = max(0.0, float(now) - self.last)
        self.tokens = min(self.capacity, self.tokens + dt * self.b)
        self.last = float(now)

    def step(self, now, increase=C.RATE_AIMD_INCREASE, step_s=C.RATE_AIMD_STEP_S,
             starved_frac=C.RATE_STARVED_FRAC, starved_s=C.RATE_STARVED_ALERT_S):
        """AIMD recovery: `B ← min(4.0, B×1.25)` every 60 s once the 60 s hold has elapsed.
        The multiplicative-DECREASE half is derived; the recovery rate is UNDERIVED §9.2 —
        which is safe precisely because a wrong number is self-correcting under AIMD."""
        self.refill(now)
        alerts = []
        if float(now) >= self.hold_until and float(now) - self.last_increase >= float(step_s):
            if self.b < self.cap_hz:
                self.b = min(self.cap_hz, self.b * float(increase))
            self.last_increase = float(now)
        if self.b < float(starved_frac) * self.cap_hz:
            if self.starved_since is None:
                self.starved_since = float(now)
            elif not self.starved_alerted and \
                    float(now) - self.starved_since >= float(starved_s):
                self.starved_alerted = True
                alerts.append("rate_starved")
        else:
            self.starved_since = None
            self.starved_alerted = False
        return alerts

    def on_429(self, now, decrease=C.RATE_AIMD_DECREASE, hold_s=C.RATE_AIMD_HOLD_S):
        """On ANY 429 — ours or another process's, since we cannot tell (§3.2)."""
        self.refill(now)
        self.b = max(self.min_hz, self.b * float(decrease))
        self.hold_until = float(now) + float(hold_s)
        self.last_increase = float(now)
        self.yield_events += 1
        R.log("rate_yield", b=self.b, hold_until=self.hold_until)
        return self.b

    # -- admission ---------------------------------------------------------------------
    def _prune(self, now, window_s=C.CANCEL_SHARE_WINDOW_S):
        cutoff = float(now) - float(window_s)
        self.admitted = [(t, l) for (t, l) in self.admitted if t >= cutoff]
        pcut = float(now) - C.CANCEL_SHARE_POISON_WINDOW_S
        self.cancel_breaches = [(t, k) for (t, k) in self.cancel_breaches if t >= pcut]

    def cancel_share(self, now):
        """`requote_cancel` count over admitted requests, EXCLUDING `exit_cancel` from both
        numerator and denominator (§3.3: exit_cancel "is never counted against the bound")."""
        self._prune(now)
        counted = [l for (_, l) in self.admitted if l != C.LANE_NEVER_REFUSED]
        if not counted:
            return 0.0, 0
        cancels = sum(1 for l in counted if l == "requote_cancel")
        return float(cancels) / len(counted), len(counted)

    def admit(self, lane, now, key=None, reserve=C.RATE_LANE_RESERVE_TOKENS,
              share_max=C.CANCEL_SHARE_MAX):
        """Admit one request on `lane`.  Returns (ok, reason).

        **`exit_cancel` is NEVER refused** (§3.3): a rate budget must never be the reason an
        order cannot be cancelled.  It is admitted at ZERO tokens (T-B3) and may drive the
        bucket negative — an honest debt the refill repays — because the alternative is an
        uncancellable order, which is unbounded risk against a bounded rate saving.

        Every other lane must leave `reserve` = 1 token behind, so the bucket never falls
        below one token for them.

        SF-1 CANCEL-LANE BOUND.  An unbounded preempting lane is a STARVATION WEAPON: a requote
        loop stuck in a cancel/replace oscillation would consume the whole bucket at top
        priority and silently stop every other function, WHICH LOOKS EXACTLY LIKE A DEAD BOT.
        So `requote_cancel` degrades first once cancels pass 25% of admitted requests over a
        rolling 60 s; its slot falls back to leaving the resting order in place until the next
        tick — a STALE QUOTE, which is a rate loss, not a risk.
        """
        self.refill(now)
        if lane == C.LANE_NEVER_REFUSED:
            self.tokens -= 1.0
            self.admitted.append((float(now), lane))
            return True, "exit_cancel_never_refused"

        if lane == "requote_cancel":
            share, n = self.cancel_share(now)
            if n >= CANCEL_SHARE_MIN_SAMPLES:
                cancels = sum(1 for (_, l) in self.admitted if l == "requote_cancel")
                if (cancels + 1.0) / (n + 1.0) > float(share_max):
                    self.cancel_breaches.append((float(now), key))
                    R.log("cancel_share_exceeded", key=key, share=share, n=n)
                    return False, "cancel_share_exceeded"

        if self.tokens - 1.0 < float(reserve):
            self.refused += 1
            return False, "reserve_floor"
        self.tokens -= 1.0
        self.admitted.append((float(now), lane))
        return True, "ok"

    def poison_due(self, key, now, breaches=C.CANCEL_SHARE_POISON_BREACHES):
        """§3.3 — "3 in 10 min ⇒ poison it"."""
        self._prune(now)
        return sum(1 for (_, k) in self.cancel_breaches if k == key) >= int(breaches)


class Scheduler(object):
    """Strict priority queue drawing from ONE bucket (§3.3).  A partition would waste the
    reserve when idle; a priority floor costs zero when the exit lane is idle, which is the
    MIRROR answer to "are we over-reserving?"."""

    def __init__(self, bucket):
        self.bucket = bucket
        self.queue = []                     # [(priority, seq, lane, key)]
        self._seq = 0

    def submit(self, lane, key=None):
        self._seq += 1
        self.queue.append((C.LANE_PRIORITY[lane], self._seq, lane, key))
        return self._seq

    def drain(self, now):
        """Serve in strict priority order, then FIFO within a lane.  Returns (served,
        deferred)."""
        served, deferred = [], []
        for pri, seq, lane, key in sorted(self.queue):
            ok, reason = self.bucket.admit(lane, now, key=key)
            (served if ok else deferred).append((lane, key, reason))
        self.queue = [(C.LANE_PRIORITY[l], i, l, k)
                      for i, (l, k, _) in enumerate(deferred)]
        return served, deferred


# =============================================================================================
# DEGRADE LADDER  (spec §3.4) — ordered by MARGINAL OBJECTIVE COST PER REQUEST SAVED, cheapest
# first.  Step 5 is never dropped (it is the truth-reader); the §3.4 NEVER list is never
# touched at all.
# =============================================================================================
class Demand(object):
    """The request demand v5 would generate at full breadth, in req/s."""

    def __init__(self, markets, classify_hz=C.CLASSIFY_HZ, book_poll_hz=C.BOOK_POLL_HZ,
                 recon_s=C.RECON_POSITIONS_S, fixed_hz=0.0):
        # markets: [{"ticker", "net", "ws_fresh_gated": bool}]
        self.markets = [dict(m) for m in markets]
        self.classify_hz = float(classify_hz)
        self.book_poll_hz = float(book_poll_hz)
        self.recon_s = float(recon_s)
        self.fixed_hz = float(fixed_hz)
        self.drop_redundant = False
        self.dropped = []

    def polled(self):
        out = []
        for m in self.markets:
            if self.drop_redundant and m.get("ws_fresh_gated"):
                continue
            out.append(m)
        return out

    def hz(self):
        return (self.classify_hz + len(self.polled()) * self.book_poll_hz +
                (1.0 / self.recon_s) + self.fixed_hz)


def degrade_plan(demand, budget_hz):
    """Apply spec §3.4's ladder until projected demand ≤ budget.  Returns (steps, demand).

    1. classify sweep 5 Hz → 1 Hz   (pinned-ness changes on a 15-min timescale)
    2. drop book polls on markets whose WS book is fresh AND gate-passed (STRICTLY REDUNDANT)
    3. breadth: drop the LOWEST-`net`-rate markets — by construction the smallest objective
       contribution — cancel-all on the dropped ones FIRST
    4. book-poll cadence on WS-less markets 1 Hz → 0.5 Hz (≤0.5 s of coverage per requote)
    5. positions/recon poll 600 s → 1800 s — **NEVER DROPPED**; it is the truth-reader

    MIRROR (degrading too EARLY ↔ too LATE): too early costs breadth we could have afforded
    (a rate loss, recovered by AIMD's increase); too late costs another process's calls, which
    is the thing the charter forbids.  The ladder therefore starts with steps whose objective
    cost is ~zero, so early degradation is nearly free and the asymmetry does not bite.
    """
    steps = []
    budget = float(budget_hz)
    if demand.hz() <= budget:
        return steps, demand

    demand.classify_hz = C.CLASSIFY_HZ_DEGRADED
    steps.append("classify_5hz_to_1hz")
    if demand.hz() <= budget:
        return steps, demand

    demand.drop_redundant = True
    steps.append("drop_redundant_book_polls")
    if demand.hz() <= budget:
        return steps, demand

    # 3. LOWEST net first — cancel-all on the dropped ones before they stop being polled.
    order = sorted(demand.polled(), key=lambda m: (float(m.get("net", 0.0)), str(m["ticker"])))
    for m in order:
        if demand.hz() <= budget:
            break
        demand.markets = [x for x in demand.markets if x["ticker"] != m["ticker"]]
        demand.dropped.append(m["ticker"])
    if demand.dropped:
        steps.append("drop_lowest_net_markets")
    if demand.hz() <= budget:
        return steps, demand

    demand.book_poll_hz = C.BOOK_POLL_HZ_DEGRADED
    steps.append("ws_less_poll_1hz_to_half")
    if demand.hz() <= budget:
        return steps, demand

    demand.recon_s = C.RECON_POSITIONS_S_DEGRADED
    steps.append("recon_600_to_1800")
    return steps, demand
