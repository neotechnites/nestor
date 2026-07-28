"""
lip_v5.alloc — ALLOCATE (spec §1.3): marginal-rate water-filling, with (★) in place of v1
§2.2's hurdle line.

"Water-filling, step, budget reserve, caps and the per-program forfeit gate are INHERITED
UNCHANGED from v1 §2.4-2.7 / §3 (they survived adversarial fire and re-derive identically
under (★))" — so this file is v4's allocator with exactly ONE substitution, made explicit at
the single line where it happens:

    v4:  if marginal_rate(...) < max(lam_h, hurdle(phi, d, p)):  continue
    v5:  if not money.admits(net_rate(...)):                     continue

There is NO separate hurdle comparison; v1 §2.2's hurdle is now inside (★) as `drift_cost`,
joined by the carry term v4 did not have.
"""

import math

from . import clusters as CL
from . import config as C
from . import money as M
from . import runtime as R


# =============================================================================================
# NEW-1b — THE CLUSTER TERM.  `place()` enforces a cluster cap; the water level did not carry
# one, so on a ladder whose rungs share one series (= ONE cluster) it planned $34.56 against a
# $10 cluster cap and `place()` funded exactly one rung — 264 refusals in 90 cycles, every
# cycle, forever.  **AN ALLOCATOR THAT PLANS WHAT place() MUST REFUSE IS NOT A PLAN**: the
# refusal loop burns the rate budget, floods the log, and — worse — makes the funded book a
# function of ARRIVAL ORDER rather than of the marginal rate (first-come rationing, v4's D5).
#
# The measure here is GROSS collateral, while `clusters.worst_case_loss_usd` NETS a threshold
# ladder.  Gross ≥ worst-case ALWAYS (netting can only subtract a min payoff ≥ 0), so the
# allocator is the CONSERVATIVE of the two and can never plan an order place() refuses.  That
# one-directional inequality is the whole property the test asserts; the reverse (a permissive
# planner) is the defect being removed.
# MIRROR (allocator too tight ↔ too loose): too tight forgoes rate the cluster cap would in
# fact have admitted (bounded by the netting gap, and the water level simply spends those
# dollars in ANOTHER cluster — the diversification the cluster cap wanted anyway); too loose is
# this finding.
# =============================================================================================
def _cluster_key(slot):
    return CL.cluster_of(slot.ticker)


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

    __slots__ = ("ticker", "side", "venue", "rho", "S", "p", "pinned", "denied",
                 "legal_price_exists", "p6_ok", "phi", "d", "l_eff", "t_hat", "program_id",
                 "window_h", "hours_left", "hours_to_start", "accrued", "assume_filled",
                 "target_size", "cum_size", "land_grab_size", "land_grab_price_c",
                 "moneyness", "close_ts", "program_end_ts", "rung")

    def __init__(self, ticker, side, rho, S, p, venue=None, pinned=False, denied=False,
                 legal_price_exists=True, p6_ok=True, phi=0.0, d=None, l_eff=C.SETTLE_LAG_H,
                 t_hat=1.0, program_id=None, window_h=16.0, hours_left=None,
                 hours_to_start=0.0, accrued=0.0, assume_filled=False, target_size=1000,
                 cum_size=0.0, land_grab_size=0, land_grab_price_c=C.LAND_GRAB_PRICE_C,
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
        self.p6_ok = p6_ok
        self.phi = float(phi)
        self.d = M.d_estimate(None, p) if d is None else float(d)
        self.l_eff = float(l_eff)
        self.t_hat = float(t_hat)
        self.program_id = program_id if program_id is not None else ticker
        self.window_h = float(window_h)
        self.hours_left = float(window_h) if hours_left is None else float(hours_left)
        self.hours_to_start = max(0.0, float(hours_to_start))
        self.accrued = float(accrued)
        self.assume_filled = assume_filled
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

    def net_at(self, q, r_star):
        """(★) for this slot at size `q`."""
        return M.net_rate(self.rho, self.S, self.p, q, self.phi, self.d, self.l_eff,
                          r_star, self.t_hat)

    def __repr__(self):
        return "Slot(%s/%s p=%.4f S=%.2f rho=%.4f)" % (self.ticker, self.side, self.p,
                                                       self.S, self.rho)


def n_cap(p, caps=None):
    """v1 §8.1 — floor($10/p) on NET.  Scales as 1/p: 25 at 40c, 500 at 2c."""
    caps = caps or Caps()
    if float(p) <= 0:
        return 0
    return int(math.floor(caps.inv_cap_usd / float(p)))


def market_cap_usd(slot, budget_usd, caps=None):
    """v1 §8.2 — collateral ≤ min(4·ρ·H, 0.25·budget).  ρ·H is the market's own pool."""
    caps = caps or Caps()
    return min(caps.per_market_pool_mult * slot.rho * slot.window_h,
               caps.per_market_budget_frac * float(budget_usd))


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


def qualification_pass(slots, budget_usd, caps=None,
                       max_markets=C.P7_MAX_REVIVAL_MARKETS,
                       max_frac=C.LAND_GRAB_MAX_COLLATERAL_FRAC, venue_caps=None,
                       cluster_cap_usd=None, per_cluster=None):
    """spec §4.5's pre-ALLOCATE loop.

        for each candidate market, per side, before ALLOCATE:
          if not qualifies(side) and a legal price exists (not PINNED):
              post max(target_size − cum_size, min q clearing ENTRY_FLOOR) at the cheapest
              legal price, subject to P7 revival caps (≤3 concurrent revival markets), the
              LAND_GRAB_MAX_COLLATERAL_FRAC = 0.25 land-grab fraction, and the §1.4 rung-0 cap

    Why this is outside the water-filling loop at all: at S = 0, `gross(q) = 0` — with no
    rivals our MARGINAL rate is exactly zero, because we already own 100% of the side.  So
    ALLOCATE correctly assigns an empty book ZERO and would never enter one.  THAT IS RIGHT
    ABOUT SIZE AND WRONG ABOUT ENTRY: if either side fails `target_size_fp`, the snapshot is
    EXCLUDED and NOBODY is paid, us included.  Qualification is therefore a constraint, not a
    rate, and it is handled here.

    Returns (alloc {key: qty}, spent).

    `venue_caps` — spec §4.5 subjects the land grab to "the §1.4 rung-0 cap" too.  A grab
    that does not FIT under its venue's cap is SKIPPED, never shrunk: a sub-target grab
    cannot create the qualifying side, so shrinking it spends collateral on a side that
    still pays nobody (the same self-contradiction as a sub-floor_q probe).

    `cluster_cap_usd` / `per_cluster` — NEW-1b.  A land grab is a PLACEMENT, so it faces the
    same cluster cap at `place()` as any other order; a grab planned over that cap is refused
    on the wire and the qualifying side is never created.  `per_cluster` is the caller's
    running tally so the grab and the water level share one budget rather than each spending
    the cluster's room independently.  Skipped, never shrunk, for the reason above.
    """
    caps = caps or Caps()
    venue_caps = venue_caps or {}
    alloc, spent = {}, 0.0
    per_venue = {}
    per_cluster = per_cluster if per_cluster is not None else {}
    budget_cap = min(float(budget_usd), float(max_frac) * float(budget_usd))
    by_market = {}
    for s in slots:
        if not s.is_land_grab or s.pinned or s.denied or not s.legal_price_exists:
            continue
        if s.assume_filled or not s.p6_ok or s.hours_left <= 0:
            continue
        if s.hours_to_start > C.PREPOSITION_LEAD_H:          # window START guard
            continue
        by_market.setdefault(s.ticker, []).append(s)

    def rate(s):
        # The value of creating the qualifying side: our score becomes the whole side, so the
        # comparison across ladders is the first-dollar rate at the revival price.
        return M.gross_rate(s.rho, float(s.target_size), s.land_grab_price_c / 100.0, 0.0)

    ranked = sorted(by_market.items(),
                    key=lambda kv: (-max(rate(x) for x in kv[1]),
                                    min(x.moneyness for x in kv[1]), str(kv[0])))
    markets = 0
    for ticker, sides in ranked:
        if markets >= max_markets:
            break                                            # P7 revival cap
        took = False
        for s in sorted(sides, key=lambda x: (-rate(x), str(x.side))):
            unit = R.unit_collateral(s.side, s.land_grab_price_c / 100.0)
            qty = int(min(s.land_grab_size, n_cap(unit, caps)))
            if qty < 1:
                continue
            cost = qty * unit
            if spent + cost > budget_cap + 1e-9:
                continue
            mcap = market_cap_usd(s, budget_usd, caps)
            already = sum(alloc.get(x.key, 0) * R.unit_collateral(
                x.side, x.land_grab_price_c / 100.0) for x in sides)
            if already + cost > mcap + 1e-9:
                continue
            vcap = venue_caps.get(s.venue)
            if vcap is not None and per_venue.get(s.venue, 0.0) + cost > float(vcap) + 1e-9:
                continue                                     # §1.4 rung-0 cap binds the grab
            ck = _cluster_key(s)
            if cluster_cap_usd is not None and \
                    per_cluster.get(ck, 0.0) + cost > float(cluster_cap_usd) + 1e-9:
                continue                                     # NEW-1b: place() would refuse it
            alloc[s.key] = qty
            spent += cost
            per_venue[s.venue] = per_venue.get(s.venue, 0.0) + cost
            per_cluster[ck] = per_cluster.get(ck, 0.0) + cost
            took = True
        if took:
            markets += 1
    return alloc, spent


# =============================================================================================
# THE CLIFF DECISION — v1 §3.5-3.7 KEEP/TOP_UP/HOLD/ABANDON, ported from v4's prod-proven
# `rescue` (SECOND CHARTER AMENDMENT).  The one idea: accrued score below the $1.00 payout
# cliff is CONDITIONAL, not banked — abandoning yields $0, so A counts at FULL value in the
# top-up inequality.  That is exactly why the marginal value of the next 30¢ at A = $0.70 is
# $1.00+, and why (★) — which prices only MARGINAL presence — cannot make this call alone.
# MIRROR ((★) prices the flow ↔ rescue prices the cliff OPTION): the two disagree precisely
# when accrual is stranded, and rescue governs only there (proj below the entry floor with
# A > 0); everywhere else the water level decides.
# =============================================================================================
KEEP, TOP_UP, HOLD, ABANDON = "keep", "top_up", "hold", "abandon"


class RescueResult(object):
    __slots__ = ("action", "delta_q", "proj", "abandon_value", "hold_value", "note")

    def __init__(self, action, delta_q, proj, abandon_value=0.0, hold_value=0.0, note=""):
        self.action = action
        self.delta_q = int(delta_q)
        self.proj = proj
        self.abandon_value = abandon_value
        self.hold_value = hold_value
        self.note = note

    def __repr__(self):
        return "Rescue(%s dq=%d proj=%.4f)" % (self.action, self.delta_q, self.proj)


def rescue(A, rate_now, h, rho, S, q, p, r_star, C_slot, phi, d,
           p_recover=0.0, has_other_program=True, target_usd=C.RESCUE_TARGET_USD,
           max_q=None):
    """v1 §3.5/§3.6/§3.7, all quantities PER PROGRAM-PERIOD.

      A         accrued projected payout ($) — CONDITIONAL on clearing the cliff
      rate_now  current $/h of payout accrual        h       hours left in the period
      C_slot    current collateral ($) on the slot   r_star  achieved water level ($/h/$)
      max_q     ABSOLUTE cap on total size after top-up.  Callers pass the BINDING minimum
                of n_cap(derived slot cap) − held, venue-cap room, and budget room — which is
                where the FIRST amendment composes with this one: a bigger derived rung makes
                the cliff reachable (today's $0.87/$0.83 forfeits were unreachable under a
                flat $10 cap and reachable under a $50 one).

    KEEP     projection already clears the target.
    TOP_UP   the smallest Δq reaching the target whose value beats redeploy + fill cost:
                 A + r_new·h  >  (C + Δq·p)·r*·h  +  φ·d·(q+Δq)·h
             The A on the left is THE RECOVERED-ACCRUAL TERM — remove it and the inequality
             prices the next 30¢ at 30¢, which is today's forfeit tape (test T-CLIFF-1).
    ABANDON  the cliff is unreachable (even the ρ/2 ceiling cannot reach it, or no Δq under
             `max_q` pays) AND redeploying the collateral beats holding — never throw good
             money after dead accrual (test T-CLIFF-2).
    HOLD     otherwise: keep what rests, add nothing.
    """
    phi = float(phi)
    d = min(float(d), float(p))                              # d capped at p, as everywhere
    proj = float(A) + float(rate_now) * float(h)
    if proj >= float(target_usd) - 1e-12:
        return RescueResult(KEEP, 0, proj, note="projection_clears_target")

    # -- TOP_UP: the smallest Δq that clears the target and pays for itself ----------------
    cap = n_cap(p) if max_q is None else int(max_q)
    best_dq = None
    qq = int(q)
    while qq < cap:
        qq += 1
        r_new = reward_rate(rho, qq, S)
        if float(A) + r_new * float(h) < float(target_usd) - 1e-12:
            continue                                         # not there yet: bigger qq
        dq = qq - int(q)
        redeploy = (float(C_slot) + dq * float(p)) * float(r_star) * float(h)
        fillcost = phi * d * float(qq) * float(h)
        if float(A) + r_new * float(h) > redeploy + fillcost:
            best_dq = dq
            proj = float(A) + r_new * float(h)
        break                                                # smallest clearing qq decides
    if best_dq:
        return RescueResult(TOP_UP, best_dq, proj, note="top_up_clears_target")

    # -- three-way (§3.7).  P(recover) is 0 BY CONSTRUCTION when even the ρ/2 ceiling
    #    cannot reach the target in the remaining window.
    max_attainable = float(A) + (float(rho) / 2.0) * float(h)
    p_rec = 0.0 if max_attainable < float(target_usd) else float(p_recover)
    abandon_value = (float(r_star) * float(C_slot) * float(h)) if has_other_program else 0.0
    hold_value = p_rec * float(target_usd) - phi * float(q) * d * float(h)
    if abandon_value > hold_value:
        return RescueResult(ABANDON, 0, proj, abandon_value, hold_value,
                            note="abandon_value_exceeds_hold_value")
    return RescueResult(HOLD, 0, proj, abandon_value, hold_value, note="hold")


# =============================================================================================
# ALLOCATE  (spec §1.3)
# =============================================================================================
def allocate(slots, budget_usd, r_star, caps=None, floor_rate=C.ADMIT_FLOOR_RATE_PER_H,
             venue_caps=None, step_fraction=C.STEP_FRACTION, held=None, resting=None,
             cluster_cap_usd=None):
    """Marginal-rate water-filling under (★).  Returns (alloc, spent, marginal_at_stop).

    `venue_caps`: {venue: cap_usd} from the ratchet (spec §1.4).  MIRROR (ratchet raises venue
    caps ↔ Σ venue caps vs the global ceiling): Σ caps MAY exceed the ceiling; **Σ ALLOCATED
    NEVER DOES**, because `budget_usd` binds here independently of every cap (T-R4b).

    `cluster_cap_usd`: NEW-1b — the cap `place()` already enforces, brought INSIDE the plan.
    `None` keeps the pre-fix behaviour and is used only by the pure tests; the engine always
    passes it, from the same `clusters.cluster_cap_usd(day_stop)` the rails read.

    v4's derived deviation from v1's pseudocode is KEPT: line 10's `break` when the CURRENT
    best slot can no longer afford one contract would abandon the remaining budget even when a
    CHEAPER slot could still absorb it.  Such a slot is marked permanently unaffordable
    (budget only ever decreases, so the exclusion can never be wrong) and the loop continues;
    it breaks only when NO slot can afford one more contract.
    """
    caps = caps or Caps()
    venue_caps = venue_caps or {}
    held = held or {}
    # SECOND AMENDMENT (a), replenish: `held` = net inventory attributed to each slot's leg.
    # v1 §8.1's cap binds NET exposure — held PLUS resting — so after a fill the next target
    # is `n_cap − held`, shrinking as inventory builds instead of doubling exposure (which
    # the cluster cap would then refuse, silencing the requoter: the v4 tape's enter → fill
    # → silence-to-settlement failure, arriving through a guard).  Held inventory also
    # counts against the VENUE cap: a filled probe IS the venue's unverified exposure.
    budget_usd = max(0.0, float(budget_usd))                 # a negative budget funds NOTHING

    alloc = {}
    # NEW-1b: held inventory is already the cluster's exposure at `place()` (it reads OPEN
    # positions plus RESTING orders), so the running tally starts there — otherwise a filled
    # rung would be invisible to the plan and visible to the rails, which is the same
    # plan-refuses-plan defect one step later.
    per_cluster = {}
    seen_cluster_held = set()
    for s in slots:
        # RESTING ORDERS COUNT TOO.  `place()` measures the cluster over OPEN POSITIONS PLUS
        # RESTING ORDERS; seeding the plan from held inventory alone made the planner see an
        # empty cluster every cycle while the rails saw a full one — so it planned a second
        # order and place() refused it, 180 times a minute per rung, with $4.71 deployed of a
        # $300 ceiling.  The comment above this loop already SAID place() reads both; the code
        # read one.  Plan and rail must measure the same book or the plan is fiction.
        h_q = float((held or {}).get(s.key, 0.0)) + float((resting or {}).get(s.key, 0.0))
        if h_q > 0 and s.key not in seen_cluster_held:
            seen_cluster_held.add(s.key)
            ck = _cluster_key(s)
            per_cluster[ck] = per_cluster.get(ck, 0.0) + h_q * s.p
    q_alloc, q_spent = qualification_pass(slots, budget_usd, caps, venue_caps=venue_caps,
                                          cluster_cap_usd=cluster_cap_usd,
                                          per_cluster=per_cluster)
    budget_usd_rem = max(0.0, budget_usd - q_spent)

    elig = []
    for s in slots:
        # A RESTING ORDER IS ALREADY THIS SLOT'S ALLOCATION, not a rival for it.  Seeding the
        # cluster tally from resting orders (so the plan stops over-planning) without ALSO
        # crediting the slot that owns them makes the plan oscillate: cycle 1 plans and
        # places, cycle 2 sees the cluster full and plans 0 so the requoter CANCELS, cycle 3
        # plans again — a cancel/replace loop that also reads as a bookkeeping failure to the
        # burst breaker (it halted the four-rung ladder at iteration 33).  Starting the slot
        # at what it already rests makes "keep it" the plan's default and leaves the water
        # level to decide only about the MARGIN.
        alloc[s.key] = max(int(q_alloc.get(s.key, 0)),
                           int(float((resting or {}).get(s.key, 0.0))))
        if s.pinned or s.denied or not s.legal_price_exists:
            continue
        if not s.p6_ok or s.assume_filled:
            continue
        if s.hours_left <= 0 or s.hours_to_start > C.PREPOSITION_LEAD_H:
            continue
        if s.close_ts is not None and s.program_end_ts is not None:
            # spec §1.2's hard horizon exclusion — the PYPL geometry, refused before it costs
            # anything.  Evaluated in HOURS on both sides; the type error matters.
            # `now` is reconstructed from the PROGRAM end minus the program-relative
            # hours_left — never from close_ts, which is the MARKET close and (on exactly
            # the markets this guard exists for) a different, later time.
            if M.horizon_excluded(s.close_ts, s.program_end_ts - s.hours_left * 3600.0,
                                  s.program_end_ts, s.rung):
                continue
        if s.key in q_alloc or s.p <= 0 or s.rho <= 0 or s.S <= 0:
            continue
        elig.append(s)

    spent = q_spent
    per_market = {}
    per_venue = {}
    for s in slots:
        if s.key in q_alloc:
            unit = R.unit_collateral(s.side, s.land_grab_price_c / 100.0)
            per_market[s.ticker] = per_market.get(s.ticker, 0.0) + q_alloc[s.key] * unit
            per_venue[s.venue] = per_venue.get(s.venue, 0.0) + q_alloc[s.key] * unit
    seen_held = set()
    for s in slots:
        h_q = float(held.get(s.key, 0.0))
        if h_q > 0 and s.key not in seen_held:
            seen_held.add(s.key)
            per_venue[s.venue] = per_venue.get(s.venue, 0.0) + h_q * s.p

    unaffordable = set()
    last_rate = 0.0
    guard = 0
    while True:
        guard += 1
        if guard > 200000:                                   # never spin
            break
        best, best_rate = None, float("-inf")
        for s in elig:
            if s.key in unaffordable:
                continue
            q = alloc[s.key]
            # v1 §8.1: the per-slot cap binds NET exposure — held inventory + resting.
            if held.get(s.key, 0.0) + q + 1 > n_cap(s.p, caps):
                continue
            if per_market.get(s.ticker, 0.0) + s.p > market_cap_usd(s, budget_usd, caps) + 1e-9:
                continue
            vcap = venue_caps.get(s.venue)
            if vcap is not None and per_venue.get(s.venue, 0.0) + s.p > float(vcap) + 1e-9:
                continue
            # NEW-1b: the cluster cap `place()` will apply, applied HERE so the plan is
            # fundable.  Caps do not compose (clusters.py's own note): a cluster spanning
            # several series inherits several per-venue caps, so this term is not implied by
            # the one above.
            if cluster_cap_usd is not None and \
                    per_cluster.get(_cluster_key(s), 0.0) + s.p > float(cluster_cap_usd) + 1e-9:
                continue
            # ---- THE ONE SUBSTITUTION (spec §1.3): (★) replaces v1 §2.2's hurdle line ----
            r = s.net_at(q, r_star)
            if not M.admits(r, floor_rate):
                continue
            # -------------------------------------------------------------------------------
            if r > best_rate + 1e-15 or (abs(r - best_rate) <= 1e-15 and best is not None
                                         and (s.ticker, s.side) < (best.ticker, best.side)):
                best, best_rate = s, r
        if best is None:
            break
        step = max(1, int(round(step_fraction * budget_usd / best.p)))    # v1 §2.5
        step = min(step, n_cap(best.p, caps) - int(held.get(best.key, 0.0))
                   - alloc[best.key])
        room = market_cap_usd(best, budget_usd, caps) - per_market.get(best.ticker, 0.0)
        step = min(step, int(room / best.p + 1e-9))
        vcap = venue_caps.get(best.venue)
        if vcap is not None:
            step = min(step, int((float(vcap) - per_venue.get(best.venue, 0.0)) / best.p + 1e-9))
        best_ck = _cluster_key(best)
        if cluster_cap_usd is not None:
            step = min(step, int((float(cluster_cap_usd)
                                  - per_cluster.get(best_ck, 0.0)) / best.p + 1e-9))
        if spent + step * best.p > budget_usd + 1e-12:
            step = int((budget_usd - spent) / best.p + 1e-9)
        if step < 1:
            unaffordable.add(best.key)
            continue
        alloc[best.key] += step
        spent += step * best.p
        per_market[best.ticker] = per_market.get(best.ticker, 0.0) + step * best.p
        per_venue[best.venue] = per_venue.get(best.venue, 0.0) + step * best.p
        per_cluster[best_ck] = per_cluster.get(best_ck, 0.0) + step * best.p
        last_rate = best_rate
    return alloc, spent, last_rate


def projected_period_payout(program_slots, alloc):
    """v1 §3.5 — `accrued + share × (ρ/2) × hours_left`.

    Only the UN-ACCRUED portion scales.  v4's C3 defect (multiplying the FULL-period pool by
    the CURRENT share regardless of remaining window) graded a $0.22 reachable payout as a $25
    projection, and a gate that mis-grades in the PERMISSIVE direction is worse than no gate,
    because it launders a bad entry as a checked one.
    """
    accrued = max([s.accrued for s in program_slots] or [0.0])
    total = 0.0
    for s in program_slots:
        q = alloc.get(s.key, 0)
        if q <= 0:
            continue
        total += our_share(q, s.S) * (s.rho / 2.0) * max(0.0, s.hours_left)
    return accrued + total


def _cliff_decision(ps, alloc, r_star, caps, venue_caps, held, budget_room, per_venue_spend,
                    cluster_cap_usd=None, per_cluster_spend=None):
    """The SECOND AMENDMENT's decision for one below-entry-floor program WITH accrual at
    stake.  Returns (RescueResult, best_slot).  `max_q` is the binding minimum of the
    derived per-slot cap (amendment 1 composes here), the venue-cap room, the CLUSTER-cap
    room (NEW-1b — a top-up is a placement and faces the same rail) and the budget — so a
    top-up can never breach the bounds the water level honors."""
    best = max(ps, key=lambda s: (alloc.get(s.key, 0), -s.p))
    q = alloc.get(best.key, 0)
    A = max([s.accrued for s in ps] or [0.0])
    C_prog = sum(alloc.get(s.key, 0) * s.p for s in ps)
    h = max(0.0, best.hours_left)
    max_q = n_cap(best.p, caps) - int(held.get(best.key, 0.0))
    vcap = (venue_caps or {}).get(best.venue)
    if vcap is not None:
        room = max(0.0, float(vcap) - per_venue_spend.get(best.venue, 0.0))
        max_q = min(max_q, q + int(room / best.p))
    if cluster_cap_usd is not None:
        room = max(0.0, float(cluster_cap_usd)
                   - (per_cluster_spend or {}).get(_cluster_key(best), 0.0))
        max_q = min(max_q, q + int(room / best.p))
    max_q = min(max_q, q + int(max(0.0, budget_room) / best.p))
    res = rescue(A, reward_rate(best.rho, q, best.S), h, best.rho, best.S, q, best.p,
                 r_star, C_prog, phi=best.phi, d=best.d,
                 has_other_program=True, max_q=max_q)
    return res, best


def allocate_with_forfeit_gate(slots, budget_usd, r_star, caps=None,
                               floor_usd=C.ENTRY_FLOOR_USD, floor_rate=C.ADMIT_FLOOR_RATE_PER_H,
                               venue_caps=None, max_passes=C.MAX_GATE_PASSES, held=None,
                               resting=None,
                               cluster_cap_usd=None):
    """v1 §2.4 lines 12-15 — the forfeit gate is per PROGRAM-PERIOD, applied AFTER
    water-filling, and a dropped program's dollars are RE-WATER-FILLED.

    SECOND CHARTER AMENDMENT — the gate now prices the CLIFF (v1 §3.5-3.7):
      * A program below the entry floor with ZERO accrual is an ENTRY question: dropped, as
        before (nothing is at stake).
      * A program below the floor WITH accrued value is a RESCUE question: `rescue` decides.
        ABANDON drops it (the cliff is genuinely unreachable — dead accrual gets no more
        money); KEEP/TOP_UP/HOLD keep it, and a TOP_UP's Δq is APPLIED to the allocation
        after the drop loop converges, so the requoter posts the size that reaches $1.10.
    MIRROR (ENTRY_FLOOR as an entry test ↔ the exit): rescue IS the exit end, now wired.
    """
    caps = caps or Caps()
    held = held or {}
    dropped = set()
    alloc, spent, marginal = {}, 0.0, 0.0
    for _ in range(int(max_passes)):
        live = [s for s in slots if s.program_id not in dropped]
        alloc, spent, marginal = allocate(live, budget_usd, r_star, caps, floor_rate,
                                          venue_caps, held=held, resting=resting,
                                          cluster_cap_usd=cluster_cap_usd)
        by_prog = {}
        for s in live:
            by_prog.setdefault(s.program_id, []).append(s)
        pv_spend, pc_spend = {}, {}
        for s in live:
            used = alloc.get(s.key, 0) * s.p + float(held.get(s.key, 0.0)) * s.p
            pv_spend[s.venue] = pv_spend.get(s.venue, 0.0) + used
            ck = _cluster_key(s)
            pc_spend[ck] = pc_spend.get(ck, 0.0) + used
        newly = []
        for pid, ps in sorted(by_prog.items(), key=lambda kv: str(kv[0])):
            proj = projected_period_payout(ps, alloc)
            if proj >= floor_usd:
                continue
            A = max([s.accrued for s in ps] or [0.0])
            if A <= 0.0:
                if proj > 0.0:
                    newly.append(pid)                        # pure entry: the floor decides
                continue
            res, _best = _cliff_decision(ps, alloc, r_star, caps, venue_caps, held,
                                         budget_usd - spent, pv_spend,
                                         cluster_cap_usd, pc_spend)
            if res.action == ABANDON:
                newly.append(pid)
                R.log("cliff_abandon", program_id=str(pid), accrued=A, proj=res.proj,
                      abandon_value=res.abandon_value, hold_value=res.hold_value)
        if not newly:
            break
        dropped |= set(newly)
    # Apply TOP_UPs on the CONVERGED allocation (one application, no oscillation with the
    # drop loop): the Δq that reaches the cliff, bounded by every cap rescue already saw.
    by_prog = {}
    for s in slots:
        if s.program_id not in dropped:
            by_prog.setdefault(s.program_id, []).append(s)
    pv_spend, pc_spend = {}, {}
    for s in slots:
        if s.program_id in dropped:
            continue
        used = alloc.get(s.key, 0) * s.p + float(held.get(s.key, 0.0)) * s.p
        pv_spend[s.venue] = pv_spend.get(s.venue, 0.0) + used
        ck = _cluster_key(s)
        pc_spend[ck] = pc_spend.get(ck, 0.0) + used
    for pid, ps in sorted(by_prog.items(), key=lambda kv: str(kv[0])):
        proj = projected_period_payout(ps, alloc)
        if proj >= floor_usd:
            continue
        A = max([s.accrued for s in ps] or [0.0])
        if A <= 0.0:
            continue
        res, best = _cliff_decision(ps, alloc, r_star, caps, venue_caps, held,
                                    budget_usd - spent, pv_spend,
                                    cluster_cap_usd, pc_spend)
        if res.action == TOP_UP and res.delta_q > 0:
            alloc[best.key] = alloc.get(best.key, 0) + res.delta_q
            spent += res.delta_q * best.p
            pv_spend[best.venue] = pv_spend.get(best.venue, 0.0) + res.delta_q * best.p
            bck = _cluster_key(best)
            pc_spend[bck] = pc_spend.get(bck, 0.0) + res.delta_q * best.p
            R.log("cliff_top_up", program_id=str(pid), ticker=best.ticker, side=best.side,
                  accrued=A, delta_q=res.delta_q, proj=res.proj)
    for s in slots:
        alloc.setdefault(s.key, 0)
        if s.program_id in dropped:
            alloc[s.key] = 0
    return alloc, spent, marginal, dropped


def allocate_with_rstar(slots, budget_usd, caps=None, trailing_rate=None,
                        floor_rate=C.ADMIT_FLOOR_RATE_PER_H, venue_caps=None, held=None,
                        resting=None,
                        cluster_cap_usd=None):
    """The full cycle: solve spec §1.3's r* fixpoint around ALLOCATE — the FORFEIT-GATED
    ALLOCATE, because the allocation the requoter diffs against must be the post-gate one
    (finish-round charter A): quoting a program the gate would drop posts collateral into a
    slot whose projected payout cannot clear the $2.00 floor, i.e. a knowingly forfeited
    entry.

    Returns (alloc, spent, RStarResult); `res.dropped` carries the gate's drops.
    Non-convergence uses `max(r*_0..r*_k)` — the CONSERVATIVE direction — and logs
    `rstar_no_converge`.
    """
    caps = caps or Caps()
    # r* is the OPPORTUNITY COST of a dollar, not the admission bar: it stays seeded at the
    # reference floor (and rises with the book's own achieved rate).  The admission hurdle is
    # a separate, much lower number — see config.ADMIT_FLOOR_RATE_PER_H.
    r0 = M.rstar_seed(trailing_rate, C.FLOOR_RATE_PER_H)

    def run(r_star):
        a, sp, marg, dropped = allocate_with_forfeit_gate(
            slots, budget_usd, r_star, caps, floor_rate=floor_rate, venue_caps=venue_caps,
            held=held, resting=resting, cluster_cap_usd=cluster_cap_usd)
        return (a, sp, dropped), (marg if marg > 0 else floor_rate)

    res = M.solve_rstar(lambda r: run(r), r0)
    if not res.converged:
        R.log("rstar_no_converge", r_star=res.r_star, trace=res.trace)
    alloc, spent, dropped = res.alloc
    res.dropped = dropped
    return alloc, spent, res
