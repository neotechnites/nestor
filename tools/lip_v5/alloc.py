"""
lip_v5.alloc — the allocator, which since 2026-07-30 is THE OWNER'S LAW and nothing else.

The water-filling core, the r* fixpoint, the forfeit gate, the rescue, the qualification
pass, owner displacement/recall, the plan-side variance test and the pass-2 idle sweep are
all DELETED (owner decision, 2026-07-30) — replaced by one formula, stated in full at the
law header below: rank every candidate market by the CAPITAL NEEDED TO EARN $1.50 IN THE
NEXT 24 HOURS, fund cheapest-need first, one order per cluster, $10 per market, $300 total.

What survives above the law section is the CFTC scoring (score_side — the filing's own
algorithm), the Slot table, and the rail-side helpers (`Caps`/`n_cap`/`reserve_budget`)
that `place()` still reads.
"""

import math

from . import clusters as CL
from . import config as C
from . import money as M
from . import runtime as R



# =============================================================================================
# SCORING — the CFTC filing algorithm, verbatim (spec §0.2 "inherited, not re-litigated").
# =============================================================================================
class SideScore(object):
    __slots__ = ("ref_c", "S", "qualifies", "top_size", "cum_size", "reason")

    def __init__(self, ref_c=None, S=0.0, qualifies=False, top_size=0.0, cum_size=0.0,
                 reason=""):
        self.ref_c = ref_c
        self.S = S
        self.qualifies = qualifies
        self.top_size = top_size
        self.cum_size = cum_size
        self.reason = reason


def score_side(levels, target_size, df=C.DISCOUNT_FACTOR_DEFAULT, mode=C.S_MODE_RECON,
               max_price_c=C.MAX_LEGAL_PRICE_C):
    """`Score(bid) = DF^(RefPrice − Price) × Size`, walking DOWN from the reference until
    cumulative size reaches Target Size.  If bids run out first the qualifying set is CLEARED,
    not partial — that is the filing's own rule and the reason an unqualified side scores 0.

    A book whose best is AT the cap has NO reference price ("if it exists and is less than the
    highest possible price").
    """
    lv = sorted([(int(round(p)), float(s)) for p, s in (levels or []) if float(s) > 0],
                key=lambda x: -x[0])
    if not lv:
        return SideScore(reason="empty_side")
    ref, top = lv[0][0], lv[0][1]
    if ref >= max_price_c:
        return SideScore(ref_c=ref, S=0.0, qualifies=False, top_size=top, cum_size=0.0,
                         reason="ref_at_cap")
    S = cum = 0.0
    qualifies = False
    for i, (p, sz) in enumerate(lv):
        dist = (ref - p) if mode == "cents" else i
        S += sz * (df ** dist)
        cum += sz
        if cum >= target_size:
            qualifies = True
            break
    if not qualifies:
        S = 0.0
    return SideScore(ref_c=ref, S=S, qualifies=qualifies, top_size=top, cum_size=cum,
                     reason="" if qualifies else "target_size_not_reached")


def our_share(q, S):
    """Pro-rata by size at the same-side best; DF^0 = 1 for us."""
    if float(q) <= 0:
        return 0.0
    return float(q) / (float(q) + float(S))


def reward_rate(rho, q, S):
    """$/h of PAYOUT accrual at resting size q: `share × ρ/2` (per side)."""
    return our_share(q, S) * float(rho) / 2.0


def is_pinned(yes_bid_c, yes_ask_c):
    """Permanently unscoreable: no LEGAL resting price exists on the missing side.
    yes_bid at 99c ⇒ a NO bid would need a yes ask above 99c: illegal.
    yes_ask at 1c  ⇒ a YES bid would need a price below 1c: illegal."""
    if yes_bid_c is not None and yes_bid_c >= C.MAX_LEGAL_PRICE_C:
        return True
    if yes_ask_c is not None and yes_ask_c <= C.MIN_LEGAL_PRICE_C:
        return True
    return False


# =============================================================================================
# SLOTS AND CAPS
# =============================================================================================
class Caps(object):
    __slots__ = ("inv_cap_usd", "per_market_pool_mult", "per_market_budget_frac",
                 "venue_cap_usd")

    def __init__(self, inv_cap_usd=C.INV_CAP_USD,
                 per_market_pool_mult=C.PER_MARKET_POOL_MULT,
                 per_market_budget_frac=C.PER_MARKET_BUDGET_FRAC, venue_cap_usd=None):
        self.inv_cap_usd = inv_cap_usd
        self.per_market_pool_mult = per_market_pool_mult
        self.per_market_budget_frac = per_market_budget_frac
        # spec §4.4 row 1's NEW per-VENUE cap.  None = derive from the day stop at call time.
        self.venue_cap_usd = venue_cap_usd


class Slot(object):
    """One (market, side).  `S` is the QUALIFYING-SET score for that side and already includes
    any wall resting at the best price."""

    # `p6_ok` and `assume_filled` are GONE (owner's law §7/§9, 2026-07-30): p6 informs phi
    # only — a slot cannot carry a refusal flag for a gate that no longer exists — and the
    # assume-filled freeze already acts through `frozen` at slot-build, not through the plan.
    __slots__ = ("ticker", "side", "venue", "rho", "S", "p", "pinned", "denied",
                 "legal_price_exists", "phi", "d", "l_eff", "t_hat", "program_id",
                 "window_h", "hours_left", "hours_to_start", "accrued",
                 "target_size", "cum_size", "land_grab_size", "land_grab_price_c",
                 "moneyness", "close_ts", "program_end_ts", "rung")

    def __init__(self, ticker, side, rho, S, p, venue=None, pinned=False, denied=False,
                 legal_price_exists=True, phi=0.0, d=None, l_eff=C.SETTLE_LAG_H,
                 t_hat=1.0, program_id=None, window_h=16.0, hours_left=None,
                 hours_to_start=0.0, accrued=0.0, target_size=1000,
                 cum_size=0.0, land_grab_size=0, land_grab_price_c=C.ENTRY_BAND_LO_C,
                 moneyness=50.0, close_ts=None, program_end_ts=None, rung=0):
        self.ticker = ticker
        self.side = side                                # "bid" | "ask"
        self.venue = venue if venue is not None else ticker
        self.rho = float(rho)                           # $/h over the program's OWN window
        self.S = float(S)
        self.p = float(p)                               # collateral $/contract at same-side best
        self.pinned = pinned
        self.denied = denied
        self.legal_price_exists = legal_price_exists
        self.phi = float(phi)
        self.d = M.d_estimate(None, p) if d is None else float(d)
        self.l_eff = float(l_eff)
        self.t_hat = float(t_hat)
        self.program_id = program_id if program_id is not None else ticker
        self.window_h = float(window_h)
        self.hours_left = float(window_h) if hours_left is None else float(hours_left)
        self.hours_to_start = max(0.0, float(hours_to_start))
        self.accrued = float(accrued)
        self.target_size = target_size
        self.cum_size = cum_size
        self.land_grab_size = int(land_grab_size or 0)
        self.land_grab_price_c = int(land_grab_price_c)
        self.moneyness = float(moneyness)
        self.close_ts = close_ts
        self.program_end_ts = program_end_ts
        self.rung = int(rung)

    @property
    def key(self):
        return (self.ticker, self.side)

    @property
    def is_land_grab(self):
        return self.land_grab_size > 0

    def __repr__(self):
        return "Slot(%s/%s p=%.4f S=%.2f rho=%.4f)" % (self.ticker, self.side, self.p,
                                                       self.S, self.rho)


def n_cap(p, caps=None):
    """v1 §8.1 — floor($10/p) on NET.  Scales as 1/p: 25 at 40c, 500 at 2c.

    ⚠ THIS IS A DOLLAR CAP, NOT A SIZING RULE, AND IT MUST NOT BE USED AS ONE.
    Read `floor_clearing_size` below before adding a call site.  As a sizing rule this
    function is the trap expressed in code: it buys MORE contracts as price falls, so it
    deliberately buys the most of the least likely thing, with no reference to the pool it is
    trying to earn from or to the rivals it is competing with.  Measured against what the
    $1.00 payout floor actually required, it was 32x oversized in one rung (3,000 posted
    against 93 needed) and 0.3x UNDER-sized in another (12 posted against 40) — i.e. random
    with respect to the objective, in both directions at once.  It survives only as the
    per-rung DOLLAR bound (`inv_cap_usd`, itself derived from the day stop), which is the one
    thing it is dimensionally honest about.
    """
    caps = caps or Caps()
    if float(p) <= 0:
        return 0
    return int(math.floor(caps.inv_cap_usd / float(p)))



def reserve_budget(ceiling_usd, max_slot_collateral_usd):
    """v1 §2.4 (B3) — budget = ceiling − max_slot_collateral.  Make-before-break transiently
    holds TWO copies of one slot's collateral; without the reserve the LARGEST slot's requote
    is rejected exactly when the book is moving, i.e. the failure is CORRELATED with the moment
    presence matters most.

    MIRROR (reserving too much ↔ too little): too much idles one slot's worth of capital and is
    caught by the `idle_capital` alert; too little breaks MBB and is caught by `mbb_degraded`.
    """
    return max(0.0, float(ceiling_usd) - max(0.0, float(max_slot_collateral_usd)))


# =============================================================================================
# THE QUALIFICATION PRE-PASS  (spec §4.5, N1) — a DISCRETE PRECONDITION, not a rate.
# =============================================================================================
def t0_qualification_size(cum_size, target_size, min_floor_q=0):
    """`max(target_size − cum_size, min q clearing ENTRY_FLOOR)` — spec §4.5.

    **Do NOT size up into an empty book** (v1 D2): at S ≈ 0, `share = q/(q+S) ≈ 1` for any q,
    so extra size buys NO share — only fill risk and carry.  THE MINIMUM QUALIFYING SIZE IS THE
    MAXIMUM OF THE OBJECTIVE.
    """
    return int(max(0, math.ceil(float(target_size) - float(cum_size)), int(min_floor_q)))



# =============================================================================================
# THE OWNER'S LAW (Ryan, 2026-07-30) — the allocator.  Everything above this line in the
# ALLOCATE family (water-filling, r*, gates, bolt-ons) is replaced by what follows.
#
# THE LAW, verbatim where it is sizing:
#   1. RANKING — for every candidate market compute CAPITAL NEEDED TO EARN $1.50 IN THE NEXT
#      24 HOURS from the live window's pool, competition, time left, phi, and accrued already
#      banked there.  Accrual >= target is DONE — skip, fund next-best.  Fund cheapest-need
#      first until capital is gone.
#   2. ONE ORDER PER CLUSTER (clusters.py grouping).
#   3. CAPS — $10 per market, $300 total (config.ALLOC_PER_MARKET_USD /
#      MAX_TOTAL_COLLATERAL_USD, the only two strategy constants).
#   4. SIZING — "if it costs 5 dollar-hours to earn 1.5, and phi is 2.5, we will put in
#      5 dollars, and requote when it fills.  if phi is 24, and it costs 10 dollar-hours, we
#      will put in 1000/24 cents, 24 times.  if it doesn't fit in there, we can't afford it,
#      because going above 10 dollars makes us too concentrated.  if somehow this market is
#      awesome and we can earn a dollar in 24 hours with only one dollar, we will put all 10,
#      because that capital can't go to any other rung or it would be too consolidated."
#   5. FLOOR — $1.50 per 24 hours AND never below $1.00 by window end.
#
# THE READING OF §4, stated once so the arithmetic can be argued with (the three examples are
# reproduced numerically in tests/test_law.py):
#   * "dollar-hours" is the TOTAL capital the market consumes over the horizon: the resting
#     lot, replaced each time a fill converts it into inventory.  phi in his examples counts
#     TURNOVERS of the lot over the horizon; in this codebase phi is fills/hour/resting-
#     contract, so turnovers T = phi x h (fills scale linearly with size, so T is
#     size-independent).  Total consumption of a lot L held through the horizon is
#     L x max(1, T): the lot itself, T times over ("1000/24 cents, 24 times" = $10 total at
#     T = 24; "put in 5 dollars, and requote when it fills" = $5 total at T = 2.5).
#   * the NEED is the smallest lot that earns the target: W = q_rest x p from the share
#     equation below.  total_need = W x max(1, T) (+ self-qualification where the side does
#     not qualify on rival depth — law §7a).  total_need > $10 ⇒ we can't afford it: SKIP,
#     logged with the numbers, never silent.
#   * the ORDER is OVERSIZED up to the envelope: lot = env / max(1, T) where env = min($10,
#     budget room, $10 − basis already bought here).  At T <= 1 that is the whole $10 ("we
#     will put all 10") and at T = 24 it is 1000/24 cents; affordability (total_need <= env)
#     guarantees lot >= W, so the order never undershoots the need.  Oversizing is free in
#     the owner's frame because the excess "can't go to any other rung [of this cluster] or
#     it would be too consolidated" — and share is monotone in q, so the extra size earns.
#   * env shrinks as fills accumulate basis in the market (market_spent), so the remainder of
#     the $10 IS the requote budget, consumed as fills happen, with no bookkeeping beyond the
#     positions the exchange already confirms — restart-safe by construction.
# =============================================================================================
LAW_HORIZON_H = 24.0                          # "in the next 24 hours" — the law's own horizon


def law_target_usd(hours_left, floor_24h=None, cliff=None, horizon_h=LAW_HORIZON_H):
    """The credit this market must reach inside the horizon, and the hours it has.

    Law §5: "$1.50 per 24 hours AND never below $1.00 by window end (sub-$1 windows forfeit;
    pro-rating a short window earns credit that forfeits)."  A window longer than the horizon
    is judged on the $1.50 pace.  A window SHORTER than the horizon may pro-rate the pace —
    but never below the $1.00 forfeit cliff, because $1.50 x (4h/24h) = $0.25 is credit
    Kalshi pays ZERO for: the pro-rated target is clamped at the cliff, and a window whose
    pool cannot reach $1.00 by its end is skipped as unreachable, not funded small.

    Returns (target_usd, h) with h = min(hours_left, horizon).
    """
    floor_24h = C.ENTRY_FLOOR_USD if floor_24h is None else float(floor_24h)
    cliff = C.CREDIT_TARGET_USD if cliff is None else float(cliff)
    h = min(max(0.0, float(hours_left)), float(horizon_h))
    if float(hours_left) >= float(horizon_h):
        return floor_24h, h
    return max(cliff, floor_24h * h / float(horizon_h)), h


# Skip reasons — every one is logged with numbers (three separate incidents on 2026-07-30
# were caused by silent refusal paths; a skip that cannot say why is a defect, not a policy).
DONE, UNREACHABLE, UNAFFORDABLE, CLUSTER_TAKEN, BUDGET_EXHAUSTED, EXHAUSTED, FUND = (
    "done", "unreachable", "unaffordable", "cluster_taken", "budget_exhausted",
    "allocation_exhausted", "funded")


class Need(object):
    """One candidate's LAW arithmetic — every number a skip or a fund line must cite."""

    __slots__ = ("slot", "cluster", "target_usd", "need_usd", "h", "q_rest", "rest_usd",
                 "turnovers", "qualify_q", "qualify_usd", "unit_usd", "total_usd", "reason")

    def __init__(self, slot, cluster, target_usd, need_usd, h, q_rest=0, rest_usd=0.0,
                 turnovers=0.0, qualify_q=0, qualify_usd=0.0, unit_usd=0.0, total_usd=0.0,
                 reason=""):
        self.slot = slot
        self.cluster = cluster
        self.target_usd = target_usd
        self.need_usd = need_usd
        self.h = h
        self.q_rest = int(q_rest)
        self.rest_usd = rest_usd
        self.turnovers = turnovers
        self.qualify_q = int(qualify_q)
        self.qualify_usd = qualify_usd
        self.unit_usd = unit_usd                  # collateral $/contract at the ORDER's price
        self.total_usd = total_usd                # THE RANKING NUMBER (law §1)
        self.reason = reason

    def numbers(self):
        """The log payload: no refusal without its arithmetic."""
        return {"ticker": self.slot.ticker, "side": self.slot.side, "cluster": self.cluster,
                "target_usd": round(self.target_usd, 4), "need_usd": round(self.need_usd, 4),
                "h": round(self.h, 2), "q_rest": self.q_rest,
                "rest_usd": round(self.rest_usd, 4), "turnovers": round(self.turnovers, 3),
                "qualify_q": self.qualify_q, "qualify_usd": round(self.qualify_usd, 4),
                "total_usd": round(self.total_usd, 4), "accrued": round(self.slot.accrued, 4)}


def law_need(slot):
    """CAPITAL NEEDED TO EARN THE TARGET IN THE NEXT 24 HOURS (law §1) — from the live
    window's pool (slot.rho), competition (slot.S, the RIVAL score), time left
    (slot.hours_left), phi (slot.phi, law §6's chain, resolved in scan.build_slots) and
    accrued already banked there (slot.accrued, the estimates truth feed).

    THE SHARE EQUATION.  Credit = share x (rho/2) x h with share = q/(q+S) at the touch
    (DF^0 = 1; the CFTC filing's own scoring).  Solving share x (rho/2) x h >= need for q:

        s = need / ((rho/2) x h)          the share of the side's remaining half-pool needed
        q_rest = S x s / (1 - s)          contracts;  s >= 1 ⇒ UNREACHABLE at any size

    Note what is absent from q_rest: the price.  Score counts CONTRACTS and the target is
    DOLLARS, so the contracts needed do not depend on what one costs — price enters only in
    W = q_rest x p, which is why the same target costs ~7x less at 6c than at 40c.

    QUALIFICATION (law §7a — a former gate, now a formula input).  A side scores ZERO until
    the resting-depth walk reaches target_size.  Where rivals' depth already qualifies the
    side, our quote rides free and this term is $0.  Where it does not, the cost includes
    self-qualifying: the missing (target_size − cum_size) contracts at a scoring-legal price
    — priced at the ENTRY BAND floor, because the band is preserved law (§7b) and 1c is the
    price it exists to refuse (n = 8,240: 2c realised 0.00% on 765 markets).  Our own resting
    size counts toward the walk, so the self-qualifying depth IS the order.  At $10/market
    this practically ranks empty sides unaffordable (1,000 x 6c = $60) — and the skip is
    logged with those numbers, never silent.
    """
    ck = CL.cluster_of(slot.ticker)
    target, h = law_target_usd(slot.hours_left)
    need = target - float(slot.accrued or 0.0)
    if need <= 1e-12:
        # DONE (law §1): a market that has already banked the target earns its keep with no
        # further presence; its allocation funds the next-best in its cluster or elsewhere.
        return Need(slot, ck, target, need, h, reason=DONE)
    avail = (float(slot.rho) / 2.0) * h           # the whole side's half-pool over the horizon
    if avail <= 0.0:
        return Need(slot, ck, target, need, h, reason=UNREACHABLE)
    s_needed = need / avail
    if s_needed >= 1.0:
        # Even owning the whole side cannot reach the target inside the horizon (this is
        # also the never-below-$1.00-by-window-end clause refusing a dying window).
        return Need(slot, ck, target, need, h, reason=UNREACHABLE)
    T = max(0.0, float(slot.phi)) * h             # expected turnovers of the lot (see header)
    qual_gap = max(0, int(math.ceil(float(slot.target_size) - float(slot.cum_size))))
    if qual_gap > 0 and slot.S <= 0:
        # Self-qualification: we become the side.  Rival score is ~0, so one contract past
        # the walk takes the whole share; the walk itself is the cost.  Unit price is the
        # slot's land-grab price (scan prices it at the band floor on the side's own axis).
        unit = R.unit_collateral(slot.side, slot.land_grab_price_c / 100.0)
        q_rest = qual_gap + 1
        rest = q_rest * unit
        total = rest * max(1.0, T)
        return Need(slot, ck, target, need, h, q_rest=q_rest, rest_usd=rest, turnovers=T,
                    qualify_q=qual_gap, qualify_usd=qual_gap * unit, unit_usd=unit,
                    total_usd=total)
    q_rest = max(1, int(math.ceil(float(slot.S) * s_needed / (1.0 - s_needed))))
    unit = float(slot.p)
    rest = q_rest * unit
    qual_usd = 0.0
    if qual_gap > 0:
        # Rival depth is short of the walk AND rivals exist: our order must both fill the
        # gap and earn.  The order is one object at one price, so the binding size is the
        # larger of the two and the cost carries the gap explicitly (law §7a's parenthetical
        # is `target_size x price`; the gap form is never larger, because rivals' partial
        # depth and our own resting size count toward the walk).
        qual_usd = qual_gap * unit
        q_rest = max(q_rest, qual_gap)            # the order covers the gap, so the gap's
        rest = q_rest * unit                      # cost is inside `rest` from here on
    total = rest * max(1.0, T)
    return Need(slot, ck, target, need, h, q_rest=q_rest, rest_usd=rest, turnovers=T,
                qualify_q=qual_gap, qualify_usd=qual_usd, unit_usd=unit, total_usd=total)


def law_rank(needs):
    """Cheapest-need first (law §1), ties broken by (ticker, side) so a restart with the
    same world produces the same ranking — no discovery-order dependence (law §10)."""
    return sorted(needs, key=lambda n: (n.total_usd, n.slot.ticker, n.slot.side))


def law_order_q(need, env_usd):
    """The ORDER, oversized up to the envelope (law §4, reading in the header):

        lot = env / max(1, T)   — the largest lot whose T expected turnovers still fit the
                                  envelope, i.e. the whole $10 at T <= 1 ("we will put all
                                  10") and 1000/24 cents at T = 24.

    Affordability (total_need <= env) guarantees lot >= W, so the floor at q_rest below is a
    rounding guard, not a second policy."""
    if need.unit_usd <= 0:
        return 0
    lot_usd = float(env_usd) / max(1.0, need.turnovers)
    return max(need.q_rest, int(lot_usd / need.unit_usd + 1e-9))


def allocate_law(slots, budget_usd, market_spent=None, alloc_cap_usd=None,
                 total_cap_usd=None, cluster_spent=None, cluster_cap_usd=None):
    """THE LAW's allocation pass.  Returns (alloc {key: qty}, spent_usd, report).

    `market_spent` — {ticker: $ basis of inventory already bought there this window}: the
    consumed part of each market's $10 allocation (the requote budget, law §4).  Comes from
    the exchange's own positions, so a restart re-derives it — no state of our own.
    `budget_usd` — what remains of the $300 after inventory basis (the engine computes it
    from the same book the rails read).
    `cluster_spent` / `cluster_cap_usd` — the SAME cluster reserve `place()` enforces over
    positions, folded into the envelope so the plan can never propose an order the cluster
    rail must refuse (a plan the rail refuses re-offers forever; the plan and the rail must
    measure one book).

    Pure function of its inputs, deterministic, re-ranked every pass (law §8: capital events
    flow through reconcile/settlement into `budget_usd` and the next pass re-ranks —
    "the owner chose the easy way").  Every skip is aggregated into ONE `law_reasons` line
    per pass with per-reason counts plus worked examples; every funded market logs
    `law_funded` with its full arithmetic.  No silent refusals.
    """
    market_spent = market_spent or {}
    cluster_spent = dict(cluster_spent or {})
    alloc_cap = C.ALLOC_PER_MARKET_USD if alloc_cap_usd is None else float(alloc_cap_usd)
    total_cap = C.MAX_TOTAL_COLLATERAL_USD if total_cap_usd is None else float(total_cap_usd)
    budget = max(0.0, min(float(budget_usd), total_cap))
    alloc = {s.key: 0 for s in slots}
    needs, why, examples = [], {}, []

    def skip(n, reason):
        why[reason] = why.get(reason, 0) + 1
        if len(examples) < 3:
            ex = n.numbers()
            ex["reason"] = reason
            examples.append(ex)

    for s in slots:
        # Structural skips are COUNTED, not silent (2026-07-30 adjudication: the empty-side
        # slot was priced by scan, handed here with legal_price_exists=False, and vanished
        # with an empty reasons dict — a silent refusal wearing a type check).
        if s.pinned or s.denied or not s.legal_price_exists:
            why["unquotable"] = why.get("unquotable", 0) + 1
            continue
        if s.hours_left <= 0 or s.hours_to_start > C.PREPOSITION_LEAD_H:
            why["window"] = why.get("window", 0) + 1
            continue
        n = law_need(s)
        if n.reason in (DONE, UNREACHABLE):
            skip(n, n.reason)
            continue
        needs.append(n)

    spent = 0.0
    funded_clusters = {}
    for n in law_rank(needs):
        s = n.slot
        if n.cluster in funded_clusters:
            skip(n, CLUSTER_TAKEN)                # law §2: one order per cluster
            continue
        env = min(alloc_cap - float(market_spent.get(s.ticker, 0.0)), budget - spent)
        if cluster_cap_usd is not None:
            # The cluster rail's room, measured over the same positions the rail reads: an
            # envelope past it would fund an order `place()` refuses, forever.
            env = min(env, float(cluster_cap_usd)
                      - float(cluster_spent.get(n.cluster, 0.0)))
        if env < n.unit_usd:
            # This market's $10 is spent (fills consumed it) or the $300 is gone.  The
            # distinction matters in the log: one is the market's requote budget exhausted
            # (law §4 — presence here is complete for the window), the other ends the pass.
            if budget - spent < n.unit_usd:
                skip(n, BUDGET_EXHAUSTED)
                continue
            skip(n, EXHAUSTED)
            continue
        if n.total_usd > env + 1e-9:
            # "if it doesn't fit in there, we can't afford it, because going above 10
            # dollars makes us too concentrated."
            skip(n, UNAFFORDABLE)
            continue
        q = law_order_q(n, env)
        if q < 1:
            skip(n, UNAFFORDABLE)
            continue
        alloc[s.key] = q
        charge = min(q * n.unit_usd * max(1.0, n.turnovers), env)
        spent += charge
        funded_clusters[n.cluster] = s.key
        R.log("law_funded", q=q, order_usd=round(q * n.unit_usd, 4),
              env_usd=round(env, 4), charge_usd=round(charge, 4), **n.numbers())
    if why:
        R.log("law_reasons", candidates=len(needs) + sum(
            v for k, v in why.items() if k in (DONE, UNREACHABLE)),
            funded=len(funded_clusters), spent=round(spent, 2),
            budget=round(budget, 2), **{k: v for k, v in sorted(why.items())})
        for ex in examples:
            R.log("law_example", **ex)
    return alloc, spent, {"reasons": why, "funded": funded_clusters}


