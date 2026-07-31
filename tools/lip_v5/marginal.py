"""lip_v5.marginal — THE MARGINAL QUEUE.  v6's allocator core.

    "v6 is a LOGIC change: new allocator core — rank -> enter at cliff cost -> marginally
     deepen (next dollar to highest marginal rate) -> stay/move EMERGENT.  Not dials.
     Proof: v5 complete at $300; v6 has nothing to buy below ~$600."
                                             — note 55, RECONCILED WITH RYAN, item 1

v5's law is the SPECIAL CASE of this file at $300 with a flat $10 seat: rank by capital-to-
target, fund cheapest first, one seat each, stop.  v6 keeps every input v5 measures — the
share equation, the qualification walk, the turnover screen, the fill-bleed table, the phi
posterior, the cluster rail — and replaces the SEAT with a queue over dollars.

── THE OBJECT BEING MAXIMISED ──────────────────────────────────────────────────────────────
For one (market, side) resting q contracts at collateral price p, over its horizon h:

    share(q)      = q / (q + S)                     the CFTC filing's own scoring, DF⁰ = 1
    credit(q)     = share(q) x (rho/2) x h_eff      the side's half-pool (SCORE_SIDES = 2)
    raw(q)        = accrued + credit(q)             accrued is the estimates feed's own number
    paid(q)       = raw(q) if raw(q) >= cliff else 0        <-- THE FORFEIT CLIFF, $1.00/window
    bleed(q)      = q x p x T x g(p)                T = phi x h;  g from bleed.G_TABLE
    NET(q)        = paid(q) - bleed(q)              what a dollar here is WORTH
    CAPITAL(q)    = q x p x max(1, T)               what a dollar here COSTS the rails

`NET` is CONCAVE in q above the cliff (share is concave, bleed is linear) and `CAPITAL` is
linear, so the marginal rate

    r(q) = dNET/dCAPITAL = [ S/(q+S)^2 x (rho/2) x h_eff / p  -  T x g ] / max(1, T)

is monotonically DECREASING in q.  That single fact is what makes the queue below both
correct and cheap: greedy allocation of the next dollar to the highest marginal rate is the
exact optimum of a separable concave objective under one budget, and the equalising rate
lambda* at which capital runs out is the KNEE — emergent, never a constant.

    "Most clusters stop at their knee (~$15-25) because 'the other $40 earns more
     elsewhere'."                                                       — Ryan, note 55 §1

── THE CLIFF IS A LUMP, AND IT IS WHY PRE-FLOOR DOLLARS ARE SPECIAL ────────────────────────
Below the cliff `paid` is ZERO, so every contract from 1 to q_cliff has NEGATIVE marginal net
(-bleed) on its own.  They are not a queue item; they are ONE indivisible ENTRY BLOCK whose
value is the whole first dollar of credit.  Its rate is an AVERAGE rate over the block, which
is the standard and honest relaxation of a lumpy item into a marginal queue.

The consequence note 55 asks for falls straight out of the arithmetic with no extra term:

    "the cliff making pre-floor dollars carry rescue value (sub-$1 accrual is conditional)"

A market holding $0.70 of accrued needs only $0.30 more of credit to convert $0.70 of
CONDITIONAL accrual into $1.00 of PAID accrual — the entry block's value is the full $1.00
against a fraction of the capital, so its rate is enormous and the queue rescues it before it
opens anything new.  A market holding $0.00 pays for the whole dollar.  Same formula.  No
rescue rule, no DONE rule, no timer.

    "No DONE rule in v6.  Banked credit is sunk; enforced skip pulls capital from proven
     rates to unproven ones."                                    — note 55, item 3

A market already past the cliff is NOT skipped: its accrued is inside `raw`, its remaining
credit is real, and it competes for the next dollar on plain rate like everything else.

── THE SWITCH TOLL (anti-churn without timers) ─────────────────────────────────────────────
    "a switch pays its TRUE cost — stranded sub-cliff accrual at risk + transit presence
     loss — small differences can't pay the toll, big real ones pay instantly"
                                                                  — note 55, item 4a

Both halves are priced INSIDE the value function, and — this is the part that matters for the
convergence spine — both are functions of WORLD STATE ONLY:

  (a) STRANDED SUB-CLIFF ACCRUAL.  Already priced, exactly, by the cliff lump above: leaving
      a market with $0.70 accrued forfeits $0.70, and the queue sees that as a $0.70 discount
      on the incumbent's entry cost rather than as a penalty on the challenger.  `accrued`
      comes from the exchange's estimates feed.  It survives a cancel-all.

  (b) TRANSIT PRESENCE LOSS.  Moving a dollar from A to B costs the time the dollar spends in
      neither book.  The floor on that time is STRUCTURAL and already in this codebase: a
      plan-driven cancel may not touch a rung younger than MIN_RESTING_LIFE_S, and the new
      rung must be placed, acknowledged and rest before it scores.  So a market we are NOT
      already present in earns over (h − transit_h), not over h:

          h_eff = h                      if present here (accrued > 0)
          h_eff = max(0, h − transit_h)   otherwise

      That is the toll, in the only unit a toll can honestly be paid in — HOURS OF PRESENCE —
      and it scales correctly: on a 24 h horizon 30 s is 0.03% and a genuinely better market
      pays it instantly; on the last 5 minutes of a window it is 10% and nothing moves.
      PRESENCE IS READ FROM `accrued`, NOT FROM OUR RESTING ORDERS.  A market with accrual is
      a market we have been resting in — the estimates feed says so — and that reading is
      what makes the toll survive `cancel_all_exchange_side`: the convergence test cancels
      our ORDERS, it cannot cancel the accrual the exchange has already credited us.  Keying
      the toll on live orders would have made the book a function of its own history, which is
      the disease `test_convergence` exists to detect.

── WHAT IS *NOT* HERE ──────────────────────────────────────────────────────────────────────
No timer, no cooldown, no hysteresis constant, no minimum-improvement threshold, no DONE
flag, no rotation rule.  Stay-vs-move is the comparison of two rates with one of them holding
a toll, and that is the whole of it.
"""

import heapq
import math

from . import alloc as A
from . import bleed as B
from . import clusters as CL
from . import config as C
from . import runtime as R


# THE TRANSIT TIME, DERIVED.  `MIN_RESTING_LIFE_S` is the anti-gaming floor a plan-driven
# cancel already obeys (v1 §4.4 P1, and the B14 gate that made it structural on 2026-07-30):
# our own machine CANNOT complete a switch faster than this, so it is the true lower bound on
# the presence a switch forfeits, and a lower bound is the conservative end for a toll that
# gates churn (under-charging the toll is churn; over-charging is one rung's rate for one
# window).  It is NOT a new constant — it is the constant the placement path already enforces.
def transit_h():
    return float(C.MIN_RESTING_LIFE_S) / 3600.0


# Queue outcomes.  Every one is logged with its numbers — a skip that cannot say why is a
# defect, not a policy (three separate incidents on 2026-07-30 were silent refusals).
ENTERED, DEEPENED = "entered", "deepened"
NO_POOL, UNREACHABLE_CLIFF, NEGATIVE_ENTRY, CANT_AFFORD_ENTRY, CLUSTER_TAKEN, RAIL_FULL, \
    BUDGET_EXHAUSTED, UNQUOTABLE, WINDOW = (
        "no_pool", "cliff_unreachable", "entry_net_negative", "cant_afford_entry",
        "cluster_taken", "cluster_rail_full", "budget_exhausted", "unquotable", "window")


class Curve(object):
    """One (market, side)'s value/capital curve — the module header's arithmetic, per slot.

    Everything the queue needs is precomputed here so a heap pop costs two multiplications:
    the queue is re-run every cycle over the full universe, and a ranking that is expensive is
    a ranking that gets clamped, which is how a poll clamp came to disagree with the law on
    2026-07-30.
    """

    __slots__ = ("slot", "cluster", "S", "rho", "h", "h_eff", "cliff", "accrued", "p", "T",
                 "g", "present", "q_entry", "qualify_q", "reason", "half_pool", "phi")

    def __init__(self, slot, cliff=None, horizon_h=None, transit=None, s_override=None,
                 phi_override=None):
        cliff = C.CREDIT_TARGET_USD if cliff is None else float(cliff)
        horizon = A.LAW_HORIZON_H if horizon_h is None else float(horizon_h)
        tr = transit_h() if transit is None else float(transit)
        self.slot = slot
        self.cluster = CL.cluster_of(slot.ticker)
        self.S = float(slot.S if s_override is None else s_override)
        self.rho = float(slot.rho)
        self.h = min(max(0.0, float(slot.hours_left)), horizon)
        self.cliff = cliff
        self.accrued = max(0.0, float(slot.accrued or 0.0))
        # PRESENCE = ACCRUAL (see the header's toll derivation): the estimates feed's own
        # number, which survives a cancel-all and therefore keeps the spine intact.
        self.present = self.accrued > 0.0
        self.h_eff = self.h if self.present else max(0.0, self.h - tr)
        # PHI, with the QUIET FAMILY's pooled bound allowed to LOWER it and never to raise it
        # (quiet.family_phi_bound): family evidence may license presence, never manufacture it.
        _phi = max(0.0, float(slot.phi))
        if phi_override is not None:
            _phi = min(_phi, max(0.0, float(phi_override)))
        self.T = _phi * self.h
        self.phi = _phi
        self.half_pool = (self.rho / C.SCORE_SIDES) * self.h_eff
        self.reason = ""
        # THE SELF-QUALIFYING WALK (law §7a, unchanged from v5's `law_need`): a side whose
        # resting depth is short of `target_size` scores ZERO until the walk is complete, so
        # the missing contracts are part of the ENTRY BLOCK and are priced at the entry-band
        # floor on the side's own collateral axis.  Where rivals already qualify the side our
        # quote rides free and this is 0.
        self.qualify_q = max(0, int(math.ceil(float(slot.target_size)
                                              - float(slot.cum_size))))
        if self.qualify_q > 0 and self.S <= 0.0:
            self.p = R.unit_collateral(slot.side, slot.land_grab_price_c / 100.0)
        else:
            self.p = float(slot.p)
        self.g = B.g_for_price(self.p)
        self.q_entry = self._entry_q()

    # ── the curve itself ────────────────────────────────────────────────────────────────
    def share(self, q):
        q = float(q)
        return 0.0 if q <= 0 else q / (q + self.S)

    def credit(self, q):
        return self.share(q) * self.half_pool

    def paid(self, q):
        raw = self.accrued + self.credit(q)
        return raw if raw >= self.cliff - 1e-12 else 0.0

    def bleed(self, q):
        return float(q) * self.p * self.T * self.g

    def net(self, q):
        return self.paid(q) - self.bleed(q)

    def capital(self, q):
        """CAPITAL COMMITTED — `q x p x max(1, T)`, the identical quantity v5's `total_usd`
        measures and the identical quantity the $-rails enforce over positions.  `max(1, T)`
        because the dollars have to BE there for the lot to rest even if it never fills, and
        because a lot that turns over T times re-commits them T times (law §4)."""
        return float(q) * self.p * max(1.0, self.T)

    def _entry_q(self):
        """The ENTRY BLOCK: the smallest whole-contract size that clears BOTH the forfeit
        cliff and the qualifying walk.  Reject-with-a-reason otherwise."""
        if self.p <= 0.0 or self.h_eff <= 0.0:
            self.reason = WINDOW
            return 0
        if self.half_pool <= 0.0:
            self.reason = NO_POOL
            return 0
        need = self.cliff - self.accrued
        if need <= 1e-12:
            # Already past the cliff on banked accrual alone.  NOT "done" (note 55 item 3):
            # one contract is the smallest presence that keeps earning, and the queue decides
            # from there on plain marginal rate like every other dollar.
            q = 1
        else:
            s_needed = need / self.half_pool
            if s_needed >= 1.0:
                # Owning the whole side cannot reach $1.00 inside the window: this is law §5's
                # never-below-$1.00-by-window-end clause, refusing a dying or a starved window.
                self.reason = UNREACHABLE_CLIFF
                return 0
            if self.S <= 0.0:
                q = 1                              # empty side: one contract takes the share
            else:
                q = max(1, int(math.ceil(self.S * s_needed / (1.0 - s_needed))))
        if self.qualify_q > 0:
            # THE WALK IS ALL-OR-NOTHING (the filing's step function) — a sub-walk order scores
            # zero, so the entry block cannot be smaller than the walk, and at S <= 0 one
            # contract past it takes the whole side.
            q = max(q, self.qualify_q + (1 if self.S <= 0.0 else 0))
        return int(q)

    # ── the queue's two questions ───────────────────────────────────────────────────────
    def entry_rate(self):
        """The AVERAGE net rate over the indivisible entry block — value ÷ capital."""
        cap = self.capital(self.q_entry)
        if cap <= 0.0:
            return 0.0
        return self.net(self.q_entry) / cap

    def marginal_rate(self, q):
        """dNET/dCAPITAL at resting size q (q >= q_entry).  Closed form, module header."""
        if self.p <= 0.0:
            return 0.0
        dq_credit = (self.S / ((q + self.S) ** 2)) * self.half_pool if self.S > 0 else 0.0
        dq_bleed = self.p * self.T * self.g
        return (dq_credit - dq_bleed) / (self.p * max(1.0, self.T))

    def numbers(self, q=None):
        """The log payload.  NO SILENT TERMS: every quantity that moved the decision — the
        toll's hours, the cliff, the bleed, the presence reading — is on the line."""
        q = self.q_entry if q is None else int(q)
        return {"ticker": self.slot.ticker, "side": self.slot.side, "cluster": self.cluster,
                "q": q, "unit_usd": round(self.p, 4),
                "capital_usd": round(self.capital(q), 4),
                "credit_usd": round(self.credit(q), 4),
                "paid_usd": round(self.paid(q), 4),
                "bleed_usd": round(self.bleed(q), 4),
                "net_usd": round(self.net(q), 4),
                "marginal_rate": round(self.marginal_rate(q), 6),
                "entry_rate": round(self.entry_rate(), 6),
                "q_entry": self.q_entry, "qualify_q": self.qualify_q,
                "S": round(self.S, 3), "rho": round(self.rho, 5),
                "h": round(self.h, 3), "h_eff": round(self.h_eff, 3),
                "present": self.present, "accrued": round(self.accrued, 4),
                "cliff_usd": round(self.cliff, 4),
                "T": round(self.T, 4), "g": round(self.g, 4),
                "phi": round(self.phi, 6),
                "phi_slot": round(float(self.slot.phi), 6)}


class Plan(object):
    """The queue's answer for one (market, side)."""

    __slots__ = ("curve", "q", "capital_usd", "how")

    def __init__(self, curve, q, capital_usd, how):
        self.curve = curve
        self.q = int(q)
        self.capital_usd = float(capital_usd)
        self.how = how


def _block_to_rate(curve, q0, q_hi, floor_rate):
    """The largest q in (q0, q_hi] whose AVERAGE rate from q0 still clears `floor_rate`.

    WHY A BLOCK AND NOT A CONTRACT.  The queue is exactly "give the next dollar to the highest
    marginal rate"; taken literally that is one heap pop per contract, and at 1c wall prices a
    $600 book is 60,000 pops per cycle.  Because `net` is concave and `capital` is linear, the
    contracts a market would win BEFORE the runner-up's rate takes over are exactly those
    whose average rate still beats the runner-up — so pushing them all at once is the SAME
    allocation the contract-at-a-time queue produces, bisected instead of enumerated.  This is
    an implementation of the greedy, not an approximation of it: `test_marginal` pins the
    identity against a brute-force per-contract reference queue.
    """
    if q_hi <= q0:
        return q0
    base_n, base_c = curve.net(q0), curve.capital(q0)

    def ok(q):
        dc = curve.capital(q) - base_c
        if dc <= 0:
            return True
        return (curve.net(q) - base_n) / dc >= floor_rate - 1e-15

    if not ok(q0 + 1):
        return q0
    lo, hi = q0 + 1, int(q_hi)
    if ok(hi):
        return hi
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


def allocate_marginal(slots, budget_usd, market_spent=None, cluster_spent=None,
                      cluster_cap_usd=None, per_market_cap_usd=None, cliff=None,
                      s_smoothed=None, multi_market_clusters=None, horizon_h=None,
                      probe=None, phi_by_cluster=None):
    """THE MARGINAL QUEUE.  Returns `(alloc {(ticker, side): q}, spent_usd, report)` — the
    same shape `alloc.allocate_law` returns, so it is a drop-in for the engine's plan step.

    `budget_usd`         — C, less inventory basis and the MBB reserve (the engine's own read).
    `market_spent`       — {ticker: $ basis already converted there}, off the exchange's
                           positions, so a restart re-derives it (law §10: no state of ours).
    `cluster_spent` / `cluster_cap_usd` — the SAME rail `place()` enforces, inside the plan; a
                           plan the rail must refuse re-offers forever.
    `per_market_cap_usd` — the per-MARKET bound.  v6's default is the cluster rail itself
                           (note 55: "the knee is where money stops"; the cap is a ruin guard
                           that binds only for freak markets), passed explicitly by the caller.
    `s_smoothed`         — {(ticker, side): S̄} from `smooth.SmoothedS`.  Ranking on the
                           SNAPSHOT is what churns (note 55 item 4b).
    `phi_by_cluster`     — the quiet class's family-pooled phi bound, applied where it LOWERS
                           a strike's own posterior (quiet.family_phi_bound).
    `multi_market_clusters` — clusters exempt from one-market-per-cluster (the quiet
                           ladder-wide class, note 55 final amendment 2).  The DOLLAR rail is
                           the correlation bound either way.
    `probe`              — optional `probe.Probe`; see that module.  It partitions the budget
                           and restricts what the probe half may fund.  None = ordinary book.

    PURE FUNCTION of its inputs.  Deterministic tie-breaks on (ticker, side).  Re-ranked from
    scratch every pass — there is no carried queue, no incumbency flag, and no memory of a
    previous allocation anywhere in this function, which is what lets the convergence spine
    extend to it unchanged.
    """
    market_spent = market_spent or {}
    cluster_spent = dict(cluster_spent or {})
    multi = set(multi_market_clusters or ())
    budget = max(0.0, float(budget_usd))
    alloc, curves, why, examples = {}, {}, {}, []

    def skip(cv, reason):
        why[reason] = why.get(reason, 0) + 1
        if len(examples) < 3:
            ex = cv.numbers()
            ex["reason"] = reason
            examples.append(ex)

    for s in slots:
        alloc[s.key] = 0
        if s.pinned or s.denied or not s.legal_price_exists:
            why[UNQUOTABLE] = why.get(UNQUOTABLE, 0) + 1
            continue
        if s.hours_left <= 0 or s.hours_to_start > C.PREPOSITION_LEAD_H:
            why[WINDOW] = why.get(WINDOW, 0) + 1
            continue
        cv = Curve(s, cliff=cliff, horizon_h=horizon_h,
                   s_override=(s_smoothed or {}).get(s.key),
                   phi_override=(phi_by_cluster or {}).get(CL.cluster_of(s.ticker)))
        if cv.reason:
            skip(cv, cv.reason)
            continue
        if cv.entry_rate() <= 0.0:
            # THE VIABILITY SCREEN, generalised.  v5 refused a rung whose expected fill bleed
            # exceeded the credit it was funded to earn (`bleed_exceeds_credit`).  Here that is
            # the same statement in the queue's own units — an entry whose NET value is not
            # positive is a dollar that buys a loss, at any rank and any capital.
            skip(cv, NEGATIVE_ENTRY)
            continue
        curves[s.key] = cv

    # ── THE HEAP ────────────────────────────────────────────────────────────────────────
    # Items are (-rate, ticker, side, kind).  Negated rate for a min-heap; (ticker, side) makes
    # a restart reproduce the same order from the same world (law §10).
    heap = []
    for key, cv in curves.items():
        heapq.heappush(heap, (-cv.entry_rate(), cv.slot.ticker, cv.slot.side, "entry"))

    spent = 0.0
    by_market, by_cluster = {}, {}                 # in-pass charges
    cluster_market = {}                            # cluster -> set of tickers entered
    lam = 0.0                                      # the EQUALISING RATE — the emergent knee
    entered = {}

    def room(cv):
        """Dollars this (market, side) may still commit, over every rail at once."""
        r = budget - spent
        if probe is not None:
            r = min(r, probe.room_usd(cv, spent_by_lane))
        if per_market_cap_usd is not None:
            r = min(r, float(per_market_cap_usd)
                    - float(market_spent.get(cv.slot.ticker, 0.0))
                    - by_market.get(cv.slot.ticker, 0.0))
        if cluster_cap_usd is not None:
            r = min(r, float(cluster_cap_usd) - float(cluster_spent.get(cv.cluster, 0.0))
                    - by_cluster.get(cv.cluster, 0.0))
        return r

    spent_by_lane = {}
    while heap:
        neg_rate, tk, side, kind = heapq.heappop(heap)
        rate = -neg_rate
        cv = curves[(tk, side)]
        if rate <= 0.0:
            # THE QUEUE'S OWN TERMINATION.  Everything left in the heap has a rate at or below
            # zero — no remaining dollar buys net credit anywhere on the board.  This is the
            # "marginal returns equalise" end of note 55 item 1, as opposed to the "capital
            # ends" end below.
            break
        lam = rate
        avail = room(cv)
        if avail <= 0.0:
            if budget - spent <= 0.0:
                skip(cv, BUDGET_EXHAUSTED)
                break
            skip(cv, RAIL_FULL)
            continue
        runner_up = -heap[0][0] if heap else 0.0
        if kind == "entry":
            if (cv.cluster in cluster_market and cv.slot.ticker not in cluster_market[cv.cluster]
                    and cv.cluster not in multi):
                # LAW §2, one MARKET per cluster — a cluster's markets settle from one fact,
                # so they are one bet expressed many times.  BOTH SIDES of the entered market
                # are still legal (note 55 final amendment 1: the second side is a fresh
                # half-pool from the same seat), and the whole rule is RELAXED for the quiet
                # ladder-wide class, where the DOLLAR rail is the correlation bound instead.
                skip(cv, CLUSTER_TAKEN)
                continue
            need_usd = cv.capital(cv.q_entry)
            if need_usd > avail + 1e-9:
                # "if it doesn't fit in there, we can't afford it" — with the number logged.
                skip(cv, CANT_AFFORD_ENTRY)
                continue
            q_new = cv.q_entry
            q_old = 0
        else:
            q_old = alloc[(tk, side)]
            q_max_by_room = q_old + int((avail) / (cv.p * max(1.0, cv.T)) + 1e-9)
            if q_max_by_room <= q_old:
                skip(cv, RAIL_FULL)
                continue
            # Deepen as far as this market stays the best dollar on the board.
            q_new = _block_to_rate(cv, q_old, q_max_by_room, runner_up)
            if q_new <= q_old:
                q_new = q_old + 1
                if cv.capital(q_new) - cv.capital(q_old) > avail + 1e-9:
                    skip(cv, RAIL_FULL)
                    continue
        charge = cv.capital(q_new) - cv.capital(q_old)
        alloc[(tk, side)] = q_new
        spent += charge
        by_market[tk] = by_market.get(tk, 0.0) + charge
        by_cluster[cv.cluster] = by_cluster.get(cv.cluster, 0.0) + charge
        if probe is not None:
            lane = probe.lane_of(cv)
            spent_by_lane[lane] = spent_by_lane.get(lane, 0.0) + charge
        cluster_market.setdefault(cv.cluster, set()).add(tk)
        entered[(tk, side)] = cv
        R.log("mq_" + (ENTERED if kind == "entry" else DEEPENED),
              charge_usd=round(charge, 4), q_from=q_old, spent_usd=round(spent, 4),
              rate=round(rate, 6), lam=round(lam, 6), **cv.numbers(q_new))
        nxt = cv.marginal_rate(q_new)
        if nxt > 0.0:
            heapq.heappush(heap, (-nxt, tk, side, "deepen"))

    funded = {}
    for (tk, side), q in alloc.items():
        if q > 0:
            funded.setdefault(curves[(tk, side)].cluster, []).append((tk, side))
    if why or funded:
        R.log("mq_reasons", candidates=len(curves), funded_sides=sum(1 for q in alloc.values()
                                                                    if q > 0),
              funded_clusters=len(funded), spent=round(spent, 2), budget=round(budget, 2),
              lam=round(lam, 6), transit_h=round(transit_h(), 6),
              **{k: v for k, v in sorted(why.items())})
        for ex in examples:
            R.log("mq_example", **ex)
    return alloc, spent, {"reasons": why, "funded": funded, "lam": lam, "curves": curves}
