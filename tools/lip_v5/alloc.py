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
                 "legal_price_exists", "phi", "phi_source", "phi_prior", "phi_k",
                 "phi_exposure_h", "d", "l_eff", "t_hat",
                 "program_id", "window_h", "hours_left", "hours_to_start", "accrued",
                 "target_size", "cum_size", "land_grab_size", "land_grab_price_c",
                 "moneyness", "close_ts", "program_end_ts", "rung")

    # ── THE PHI POSTERIOR'S COMPOSITION RIDES THE SLOT (owner, 2026-07-30 night). ──────────
    # `phi` is no longer a rung of a ladder; it is the shrunk estimate
    #     phi = (fills + phi_k x phi_prior) / (phi_exposure_h + phi_k)
    # computed in `scan.phi_posterior`.  The three inputs travel with it because the sizing
    # law needs them, not merely the log: the oversize gate (law_order_q rule 3) is now
    # "own exposure has outgrown the prior's strength", which is unanswerable from `phi`
    # alone — a phi of 0.02 means opposite things at 0.5 exposure-hours and at 500.
    # `phi_source` survives with a NARROWED meaning: where the PRIOR came from ("bucket" |
    # "global" | "seed").  It no longer gates anything; it explains the prior in the log.
    # DEFAULTS.  `phi_exposure_h=None` means "the caller is asserting this phi as a fact"
    # (the same idiom the old `phi_source="measured"` default carried, and for the same
    # reason: a hand-built Slot's phi is given, not inferred).  `scan.build_slots` ALWAYS
    # stamps all three, so no production path relies on the default.
    def __init__(self, ticker, side, rho, S, p, venue=None, pinned=False, denied=False,
                 legal_price_exists=True, phi=0.0, phi_source="measured",
                 phi_prior=None, phi_k=0.0, phi_exposure_h=None, d=None,
                 l_eff=C.SETTLE_LAG_H,
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
        self.phi_source = str(phi_source)
        self.phi_prior = float(phi) if phi_prior is None else float(phi_prior)
        self.phi_k = max(0.0, float(phi_k or 0.0))
        self.phi_exposure_h = (None if phi_exposure_h is None
                               else max(0.0, float(phi_exposure_h)))
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
# THE RULING ON §4 (the owner, 2026-07-30 — supersedes the examples' literal arithmetic;
# the owner explicitly set the examples aside where they conflict and ruled from the machine):
#   1. ORDER SIZE = W, the full resting size the share-math demands (q_rest x p).  NEVER
#      shrunk to stretch across turnovers — a shrunk order under-earns every hour and misses
#      the target with certainty.
#   2. TURNOVERS ENTER ONLY THE AFFORDABILITY SCREEN: W x max(1, T) <= the $10 allocation,
#      else SKIP with the number logged ("if it doesn't fit in there, we can't afford it").
#      T = phi x h (phi is fills/hour/resting-contract here, so T — the lot's expected
#      turnovers over the horizon — is size-independent).  The screen compares the UNROUNDED
#      W (see law_need): a skip caused by rounding one contract up would refuse the owner's
#      own example-2 market.  The requote budget is the allocation minus consumed basis
#      (market_spent, read off the exchange's own positions — restart-safe); refills re-post
#      at full W until the allocation is spent.
#   3. OVERSIZE beyond W toward the full $10 ONLY on MEASURED-low phi (G3, grafted from the
#      allocator-law branch): example 3 is conditioned on a FACT ("its phi is very low"),
#      not on the absence of one.  A seed-phi market tranches at the LOT CONTAINER instead —
#      derivation in law_order_q.
# HISTORY, kept honest: before the ruling this header carried a "total-need" reading under
# which the order was env/max(1,T) — it reproduced example 2's "1000/24 cents, 24 times"
# exactly but posted $4 where example 1 said "we will put in 5 dollars".  No single formula
# reproduces both examples; the owner resolved the fork BY RULE (order = W), and the old
# example-1 arithmetic is set aside with this note as the record.
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
                 "turnovers", "qualify_q", "qualify_usd", "unit_usd", "total_usd",
                 "phi_source", "reason")

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
        self.total_usd = total_usd                # THE RANKING NUMBER (law §1) — computed
                                                  # from the UNROUNDED resting need (ruling)
        self.phi_source = getattr(slot, "phi_source", "measured")   # where the PRIOR came
                                                  # from: "bucket" | "global" | "seed"
        self.reason = reason

    @property
    def history_dominates(self):
        """Has this rung's OWN tape outgrown the prior's strength (own exposure > k)?

        THE OVERSIZE GATE, rewritten 2026-07-30 night.  It used to ask whether `phi_source`
        was a measurement — and under the ladder "measured" included ZERO FILLS OVER TWO
        HOURS, so a quiet afternoon unlocked the full $10 envelope on several rungs and the
        evening flow ate them (42 fills, ~$76 of inventory conversion in 8 hours).  Under
        shrinkage that question is no longer answerable from a label, and it is no longer
        the right question: the posterior only READS low when either the prior is low or
        the tape is long, so the thing to test is which of those two it was.  k is exactly
        the crossover — at exposure = k own tape and prior carry equal weight — so
        `exposure > k` is "more than half of this number is our own history", and the full
        envelope rests only on a rung whose quietness we have actually earned the right to
        believe.  Two hours against a k of 10 exposure-hours is 17% weight: NOT dominant,
        and the incident cannot recur by this path.
        MIRROR (gate too tight ↔ too loose): too tight leaves a genuinely dead-quiet rung
        tranching at the lot container, costing share on a safe seat — bounded, and it
        self-clears as exposure accumulates past k; too loose is tonight's incident, which
        is unbounded in inventory until the window closes.
        `None` exposure = the caller asserted phi as a fact (see `Slot.__init__`)."""
        e = getattr(self.slot, "phi_exposure_h", None)
        if e is None:
            return True
        return float(e) > float(getattr(self.slot, "phi_k", 0.0) or 0.0)

    def numbers(self):
        """The log payload: no refusal without its arithmetic."""
        return {"ticker": self.slot.ticker, "side": self.slot.side, "cluster": self.cluster,
                "target_usd": round(self.target_usd, 4), "need_usd": round(self.need_usd, 4),
                "h": round(self.h, 2), "q_rest": self.q_rest,
                "rest_usd": round(self.rest_usd, 4), "turnovers": round(self.turnovers, 3),
                "qualify_q": self.qualify_q, "qualify_usd": round(self.qualify_usd, 4),
                "total_usd": round(self.total_usd, 4), "phi_source": self.phi_source,
                # THE POSTERIOR'S COMPOSITION, not just its value (owner, 2026-07-30 night):
                # a size chosen off phi must show whether phi came from this rung's own tape
                # or from its neighborhood, or the tape cannot audit tonight's incident.
                "phi": round(float(self.slot.phi), 6),
                "phi_prior": round(float(getattr(self.slot, "phi_prior", self.slot.phi)), 6),
                "phi_k": round(float(getattr(self.slot, "phi_k", 0.0) or 0.0), 4),
                "own_exposure_h": (None if getattr(self.slot, "phi_exposure_h", None) is None
                                   else round(float(self.slot.phi_exposure_h), 4)),
                "history_dominates": self.history_dominates,
                "accrued": round(self.slot.accrued, 4)}


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
    # THE AFFORDABILITY NUMBER USES THE UNROUNDED NEED (owner's ruling, 2026-07-30): the
    # order posts whole contracts (q_rest = ceil), but a skip caused by rounding ONE
    # CONTRACT up would refuse the owner's own example-2 market (q_raw = 20.83 -> 21
    # contracts pushes 24 x W from exactly $10.00 to $10.08).  So `total_usd` — the ranking
    # AND the screen — is q_raw x p x max(1, T), and the rounding lives only in the order.
    q_raw = max(1.0, float(slot.S) * s_needed / (1.0 - s_needed))
    q_rest = max(1, int(math.ceil(q_raw)))
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
        q_raw = max(q_raw, float(qual_gap))
    total = q_raw * unit * max(1.0, T)
    return Need(slot, ck, target, need, h, q_rest=q_rest, rest_usd=rest, turnovers=T,
                qualify_q=qual_gap, qualify_usd=qual_usd, unit_usd=unit, total_usd=total)


def law_rank(needs):
    """Cheapest-need first (law §1), ties broken by (ticker, side) so a restart with the
    same world produces the same ranking — no discovery-order dependence (law §10)."""
    return sorted(needs, key=lambda n: (n.total_usd, n.slot.ticker, n.slot.side))


def law_order_q(need, env_usd):
    """THE ORDER IS W (owner's RULING, 2026-07-30 — supersedes the examples' literal
    arithmetic; the ruling is derivation-first, and the owner set the examples aside where
    they conflict):

      1. ORDER SIZE = W, the full resting size the share-math demands (q_rest).  NEVER
         shrunk to stretch across turnovers — a shrunk order under-earns every hour and
         misses the target with certainty.  (The previous `env / max(1, T)` tranche formula
         is RULED OUT and deleted.)
      2. Turnovers enter ONLY the affordability screen (law_need's total_usd, unrounded);
         the requote budget is the allocation minus consumed basis, and refills re-post at
         full W until the allocation is spent.
      3. OVERSIZE beyond W toward the full envelope ONLY when the LOW PHI IS OUR OWN
         HISTORY'S (rewritten 2026-07-30 night; supersedes G3's `phi_source` test).
         Example 3 is conditioned on a FACT — "somehow this market is awesome and [its phi
         is very low]" — and under shrinkage a low posterior is a fact about THIS rung only
         once its own exposure outweighs the prior, i.e. `Need.history_dominates`
         (own exposure > k).  Then T <= 1 means the lot is not expected to turn over inside
         the horizon and the whole envelope may rest.
         Below that exposure the low number is mostly the neighborhood's, borrowed — we do
         not know THIS rung is quiet, we know we have not looked long enough — so the order
         tranches at the LOT CONTAINER (SLOT_LOT_CAP_USD, the per-source reserve halved so
         at least one re-post is guaranteed; an existing derivation, no new constant): a
         thinly-observed market can never put its whole $10 one fill from done-for-the-day,
         and it sizes to its actual need with the requote reserve held back.
         THIS IS TONIGHT'S INCIDENT, NAILED SHUT: two quiet hours against a k of ~10
         exposure-hours is 17% weight, the posterior stays at its ~0.3/h prior, and the
         envelope stays closed.  A rung quiet for 40 hours against the same k still earns it.
    MIRROR (oversizing ↔ tranching): oversizing buys share on a rung our own history says
    is safe, bounded by the envelope; tranching keeps a re-post alive on a rung we have not
    measured long enough to distinguish from its neighbors, bounded below by one contract.
    Both ends are the same $10."""
    if need.unit_usd <= 0:
        return 0
    dominant = need.history_dominates
    if need.qualify_q > 0:
        # THE WALK IS ALL-OR-NOTHING (the filing's step function): a sub-walk order scores
        # ZERO, so the seed tranche may not undercut it (it would buy a worthless sub-walk),
        # and at S = 0 extra size buys no share, so the oversize may not inflate it
        # (t0_qualification_size: "the minimum qualifying size is the maximum of the
        # objective").  The qualify order is q_rest, exactly.
        q = need.q_rest
    elif not dominant:
        lot_usd = min(need.rest_usd, float(C.SLOT_LOT_CAP_USD), float(env_usd))
        q = max(1, int(lot_usd / need.unit_usd + 1e-9))
    elif need.turnovers <= 1.0 + 1e-12:
        lot_usd = max(need.rest_usd, float(env_usd))
        q = max(need.q_rest, int(lot_usd / need.unit_usd + 1e-9))
    else:
        q = need.q_rest                           # rule 1: the order is W, full stop
    # The ENVELOPE is a hard bound (law §3 — the allocation, not a turnover shrink): a W
    # whose ceil-rounding lands a fraction of a contract past what remains posts what the
    # allocation can hold, so the plan never proposes an order the $10 rail must refuse.
    return max(1, min(q, int(float(env_usd) / need.unit_usd + 1e-9)))


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


