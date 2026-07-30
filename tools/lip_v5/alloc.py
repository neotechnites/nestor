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


# =============================================================================================
# THE SIZING RULE (2026-07-29) — derived from the payout floor, not from a dollar budget.
#
# WHAT WE ARE PAID.  Per market per program period, from the CFTC filing:
#     credit = pool × (our_score / total_score) ÷ 2 × presence
# The ÷2 is NOT optional and was missing from every earlier estimate in this program: scores
# normalise WITHIN EACH SIDE, so a one-sided quote can earn at most half a pool.  Below $1.00
# the accrual is forfeited ENTIRELY — it is a cliff, not a taper, and 43 of 67 rungs on the
# live tape fell off it, burning 167 dollar-hours of capital for exactly zero.
#
# SOLVING FOR SIZE.  At the touch (0 ticks, DF^0 = 1) our score is our resting size, so with
# rival score Q the share needed for a target credit T is `s = 2T/pool`, and:
#     q = Q · s/(1 − s)          s = 2·T/pool
# Note what is ABSENT: the price.  Score is denominated in contracts and the floor is
# denominated in dollars, so the contracts required to clear it do not depend on what a
# contract costs.  The price enters only when converting q to collateral (q·p), which is why
# the same $1.00 costs ~$1 of capital at 3c and ~$30 at 90c — and why `floor($10/p)` had the
# relationship exactly backwards.
#
# WHY A MARGIN, AND WHY IT IS THE ONLY JOB SIZE HAS.  Sizing to exactly $1.00 is sizing to
# exactly the cliff edge: any shortfall in presence, any rival arriving mid-period, any error
# in Q, and the whole rung pays zero.  The margin is what buys the difference between "earns
# $1.00 in expectation" and "earns at least $1.00 in practice", and it is the reason this is a
# TARGET rather than a minimum.
# MIRROR (margin too SMALL ↔ too large): too small forfeits the rung entirely — the measured
# failure, 64% of rungs.  Too large buys credit at a declining rate (share is concave) while
# buying loss at a linear one, and is bounded by the per-rung dollar cap above.
# UNDERIVED: the 1.5 multiplier itself.  It is a judgement that one half of headroom is worth
# more than the marginal credit it displaces, and it should be recalibrated to
# `$1.00 / q05(actual/projected)` once a per-market accrual reading exists — which today it
# does not, because credits are labelled by EVENT and the bot's own accrual model was measured
# 2.27x off the receipt.
# =============================================================================================
def floor_clearing_size(rival_S, pool_usd, target_usd=None, margin=None,
                        sides=C.SCORE_SIDES):
    """Contracts needed at the touch for `target_usd` of credit against rival score `rival_S`.

    Returns 0 when the target is unreachable — share ≥ 1 is not a size, it is a refusal, and
    the caller must skip the rung rather than post an unbounded order at it.
    """
    pool = float(pool_usd or 0.0)
    target = float(C.CREDIT_TARGET_USD if target_usd is None else target_usd)
    mult = float(C.CREDIT_TARGET_MARGIN if margin is None else margin)
    if pool <= 0.0:
        return 0
    share = (float(sides) * target * mult) / pool
    if share >= 1.0:
        return 0                      # even owning the whole side cannot reach the target
    Q = max(0.0, float(rival_S))
    if Q <= 0.0:
        return 1                      # uncontested: one contract takes the whole side's share
    return int(math.ceil(Q * share / (1.0 - share)))


def slot_target_q(slot, caps=None):
    """STAGED-INERT as of 2026-07-29 — DERIVED AND TESTED, NOT WIRED.  Zero call sites.

    Wiring it as a hard cap inside the water-fill STOPPED THE BOOK: 10 tests failed and
    `test_orders_appear_within_three_cycles` went to zero orders on the exchange, because the
    target also feeds the forfeit gate's `q_min` eligibility test and the rescue path, so
    shrinking the cap silently changed which slots are ADMITTED rather than only how large they
    are.  That is the same plan/rail class of defect this commit set out to remove, so it is not
    going in behind a green-looking suite.  The correct integration makes the floor-clearing size
    a TARGET the water level aims at, not a bound the hurdle sees — one more pass.

    The binding per-slot contract cap: the dollar bound AND the floor-clearing target.

    `n_cap` is a dollar bound and says nothing about earning; `floor_clearing_size` is what the
    $1.00 payout floor actually requires against this slot's rivals.  The cap is the MINIMUM of
    the two, because they refuse for different reasons and both refusals are real:
      * above the floor-clearing target, share is concave and the marginal dollar buys a
        declining slice of a capped pool while buying loss at a linear rate — so the target is
        where the objective stops paying for size;
      * above the dollar bound we exceed the per-rung risk the day stop permits.
    A slot whose pool cannot reach the target at ANY size returns 0 and must be skipped, not
    posted small: sub-floor accrual is forfeited entirely, so a rung that cannot clear the floor
    is capital at risk for a guaranteed zero.
    """
    caps = caps or Caps()
    dollar_cap = n_cap(slot.p, caps)
    pool = float(getattr(slot, "rho", 0.0)) * float(getattr(slot, "window_h", 0.0) or 0.0)
    target = floor_clearing_size(slot.S, pool)
    if target <= 0:
        return 0
    return int(min(dollar_cap, target))


def market_cap_usd(slot, budget_usd, caps=None):
    """v1 §8.2 — collateral ≤ min(4·ρ·H, `PER_MARKET_BUDGET_FRAC`·budget).  ρ·H is the market's
    own pool.  This is the PLAN side and it is measured GROSS across both legs of the ticker
    (`per_market` in `allocate` is keyed by `s.ticker`).

    ── D2: HOW THIS RELATES TO THE RAIL, AND WHY PLAN ⊆ RAIL IS NOW A PROOF. ─────────────────
    `guards`' B16 is the RAIL and after D2 it is measured PER LEG at
    `config.market_leg_cap_usd(ceiling, day_stop) = max(slot_cap, MARKET_CAP_FRAC·ceiling)`.
    Two different measures of "one market" is exactly the plan-refuses-plan defect NEW-1c
    found at the cluster cap, so the containment has to be shown, not assumed:

        plan_leg  ≤  plan_gross  ≤  PER_MARKET_BUDGET_FRAC · budget      (this function)
                  ≤  MARKET_CAP_FRAC · ceiling                            (frac equal; budget ≤ ceiling)
                  ≤  max(slot_cap, MARKET_CAP_FRAC · ceiling)  =  rail_leg

    Every step is an inequality that holds by construction, and the middle one holds ONLY
    BECAUSE `PER_MARKET_BUDGET_FRAC == MARKET_CAP_FRAC` — which is asserted in config, because
    if the two fractions ever drift apart the plan can propose a leg the rail must refuse and
    nothing arms a degrade on that path (the slot re-offers the same refused order forever).
    Keeping the plan GROSS is deliberate: gross ≥ per-leg, so the plan stays the tighter of the
    two and the 4·pool term keeps its per-MARKET meaning (a two-sided quote can earn at most one
    pool, so the risk bound against that prize is a per-market statement, not a per-leg one).
    """
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
             cluster_cap_usd=None, cluster_seed=None, cluster_seed_px=None,
             ceiling_usd=None, owner_seed=None, owner_accrued=None):
    """Marginal-rate water-filling under (★).  Returns (alloc, spent, marginal_at_stop).

    ── NOTE 52 (2026-07-29 night): three strategy rules now live INSIDE the plan. ───────────
    D5  ONE RUNG PER CLUSTER.  A settle source gets one (ticker, side); the reserve
        architecture (cluster cap = lot × (1+refills)) is per-cluster, so a second rung in
        the same cluster spends the first rung's refill reserve — the fills then convert the
        reserve into inventory and presence dies mid-period.  A cluster's owner is whichever
        key already has money in it (held/resting), else the first key the water level funds.
    D11 THE VARIANCE INSTRUMENT IS A PLAN INPUT, NOT ONLY A RAIL.  `place()`'s B18 refusal
        arms no degrade, so a plan the rail must refuse re-offers forever — the same
        plan-⊄-rail deadlock as NEW-1/D2, one guard later.  The plan therefore tests V (per
        cluster, weights against the CEILING, each candidate charged at its full lot
        container n_cap·p — conservative: the most this slot could ever hold) and skips a
        slot whose funding would breach PORTFOLIO_VAR_MAX.  Skipped ≠ refused-forever: every
        pass retests, and a higher-priced candidate in the same cluster can pass where a
        cheap one was skipped — this is the mechanism that steers the book's AVERAGE price
        (Ryan: "instead of a hard cap just track our average variance").  `ceiling_usd=None`
        (pure tests) disables the test; the rail still backstops.
    D12 THE PERIOD LOCK.  A key in `locked` (= any key with money resting or held — funded
        this period by definition, rebuilt each cycle, restart-safe with no new state) is
        never zeroed by the cliff pass and its program is never dropped by the forfeit gate's
        entry test: cancelling a funded rung mid-period forfeits the whole $1.00 for a
        fraction.  Shed, kill, day-stop and the rescue's ABANDON still override — those are
        exit decisions, not re-planning.

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
    # NEW-1c (2026-07-29) — THE SEED MUST NOT BE KEYED OFF THE SLOT LIST.
    # The loop below walks `slots`, so a cluster's exposure was only counted when that exact
    # (ticker, side) still PRODUCED a slot this cycle.  Any market we hold but no longer quote —
    # frozen, denied, past its window, P6-refused, or (as of FREE_RIDE_ONLY) not qualifying on
    # rival depth — became invisible to the plan while remaining fully visible to the rails,
    # which read the whole positions/resting book and not the slot list.
    # MEASURED: arming FREE_RIDE_ONLY dropped one rung of a four-rung ladder, and the plan then
    # concentrated into the survivors and breached the cluster cap 61 times in 90 cycles while
    # DEPLOYING LESS ($56.16 resting where 4 slots rested $75.00).  Worse on both axes.
    # This also falsifies the claim in this module's header that "Gross >= worst-case ALWAYS, so
    # the allocator ... can never plan an order place() refuses": the inequality holds only when
    # both sides measure the SAME SET of positions.  The defect was never the measure, it was
    # the domain.
    # `cluster_seed` is the caller's own book — the identical positions + resting_basis it hands
    # `guards.PlaceContext` — so plan and rail cannot disagree by construction.  It carries its
    # own basis, which is why it must come from the caller: a market with no slot has no `s.p`
    # for the plan to price it with.
    per_cluster = dict(cluster_seed or {})
    # D11 — the price side of the variance ledger: Σ usd·p per cluster, so p̄ = usd_px/usd.
    per_cluster_px = dict(cluster_seed_px or {})
    # D5 — the cluster's OWNER: the one TICKER allowed to carry this settle source's rung.
    # ── OWNERSHIP IS TICKER-LEVEL AND SEEDED FROM THE REAL BOOK (2026-07-30, the 1.155
    # incident).  The first cut derived owners from the held/resting SLOT keys — but a held
    # market can transiently produce no slot (unclassified right after a restart, a crossed
    # book), and in that gap a SIBLING rung took the cluster's seat: v5 held 3 lots of
    # EURUSD 1.155 with $0.26 accrued in its pool and funded 1.153 from zero.  Accrual is per
    # MARKET, so presence moved to a rung whose pot starts at $0 while the accrued one
    # drifted toward the forfeit cliff — the exact inversion of "capital on an already-
    # earning rung is worth more" (note 52 D6, Ryan's own derivation).  `owner_seed`
    # ({cluster: ticker}) comes from the caller's positions+orders — the book that survives
    # restarts and never depends on classification timing — with accrued rungs preferred.
    # Owner values are (ticker, side) KEYS: one rung per cluster stays one SIDE too (D9 —
    # one-sided for now), and the seed carries the side straight from the position's leg.
    cluster_owner = dict(owner_seed or {})
    for k, v in sorted(list((held or {}).items()) + list((resting or {}).items())):
        if float(v) > 0:
            cluster_owner.setdefault(CL.cluster_of(k[0]), k)
    seen_cluster_held = set()
    for s in slots:
        if cluster_seed is not None:
            break            # the caller's book is authoritative; do not double-count it
        # LEGACY PATH (no cluster_seed supplied).  RESTING ORDERS COUNT TOO: `place()` measures
        # the cluster over OPEN POSITIONS PLUS RESTING ORDERS; seeding the plan from held
        # inventory alone made the planner see an empty cluster every cycle while the rails saw
        # a full one — so it planned a second order and place() refused it, 180 times a minute
        # per rung, with $4.71 deployed of a $300 ceiling.
        h_q = float((held or {}).get(s.key, 0.0)) + float((resting or {}).get(s.key, 0.0))
        if h_q > 0 and s.key not in seen_cluster_held:
            seen_cluster_held.add(s.key)
            ck = _cluster_key(s)
            per_cluster[ck] = per_cluster.get(ck, 0.0) + h_q * s.p
            per_cluster_px[ck] = per_cluster_px.get(ck, 0.0) + h_q * s.p * s.p
    q_alloc, q_spent = qualification_pass(slots, budget_usd, caps, venue_caps=venue_caps,
                                          cluster_cap_usd=cluster_cap_usd,
                                          per_cluster=per_cluster)
    budget_usd_rem = max(0.0, budget_usd - q_spent)

    # D12 — the funded set: any key with money resting or held.  Rebuilt every call from the
    # same inputs the caps read, so it needs no persistence and survives restart for free.
    locked = {k for k, v in list((held or {}).items()) + list((resting or {}).items())
              if float(v) > 0}

    def _plan_var(add_ck=None, add_usd=0.0, add_px=0.0):
        """D11 — V = Σ wᵢ²(1−p̄ᵢ)/p̄ᵢ over clusters, weights against the CEILING (the
        forward-looking denominator, same argument as guards.portfolio_variance), with an
        optional candidate charged on top.  p̄ is the capital-weighted price.

        EVERY cluster with money is charged at its RESERVE, not its current dollars: a
        funded rung refills to (1+refills) lots through the requoter without re-entering
        this loop, so what a cluster holds today understates what it is committed to
        becoming.  Charging incumbents at actuals and candidates at reserves double-counts
        nothing and under-counts everything — measured: it admitted forty 2c clusters where
        the reserve charge stops at four."""
        v = 0.0
        for ck in set(per_cluster) | ({add_ck} if add_ck else set()):
            usd = per_cluster.get(ck, 0.0) + (add_usd if ck == add_ck else 0.0)
            px = per_cluster_px.get(ck, 0.0) + (add_px if ck == add_ck else 0.0)
            if usd <= 0:
                continue
            p_bar = px / usd
            if not (0.0 < p_bar < 1.0):
                continue                      # unpriceable leg: the rail logs it; skip here
            usd_eff = max(usd, float(cluster_cap_usd)) if cluster_cap_usd else usd
            w = usd_eff / float(ceiling_usd)
            v += w * w * (1.0 - p_bar) / p_bar
        return v

    # D5′ (2026-07-30) — WITHHOLDING A SEED IS NOT ENOUGH ONCE THE COUNT REFUSAL IS GONE.
    # Owner displacement works by NOT seeding the displaced rung, on the assumption that
    # something downstream then declines to fund it — and that something used to be D5's
    # one-rung-per-cluster refusal.  With the count retired (see the water-fill), the very
    # next pass would re-fund from zero the rung we had just recalled: the cancel and the
    # re-place in one cycle, which is the cancel/replace oscillation the seeding rule exists
    # to prevent, wearing displacement's clothes.  So the recall is now stated ONCE, here,
    # and honored everywhere: `recalled` is the accrual-ranked refusal, and it is the ONLY
    # part of the old ownership gate that survives.  It is not a count — a cluster may hold
    # any number of rungs — it is "this rung's own settle source has a strictly richer pot
    # elsewhere, so its capital belongs there this period".
    #
    # ── SCARCITY IS THE PRECONDITION.  A RECALL RESOLVES A CONFLICT; IT IS NOT A PURGE. ────
    # (2026-07-30, LIVE INCIDENT: the first cut fired on accrual RANK ALONE — `owner_accrued
    # [ck] > slot.accrued` — and the estimates feed credits ONE owner per cluster, so nearly
    # every resting rung read as "poorer than the owner".  `pass2_refused` showed
    # owner_recalled on 94 of 105 candidates and the requoter's q=0 path cancelled the ENTIRE
    # resting book, ~10 rungs.  No fills and no losses, but presence went to zero — the one
    # thing this program is for.  Rolled back.)
    # THE ERROR WAS CATEGORY, NOT DEGREE.  Ryan's rule ("1.153 has earned one cent, 1.155 has
    # earned 26 — cancel 1.153, open 1.155") is an answer to a question that only EXISTS when
    # the two rungs cannot both be funded.  Under D5′ the cluster is bounded by DOLLARS, so
    # when the reserve can afford both, BOTH REST — that is the entire feature.  Accrual rank
    # decides WHO WINS a contest; it does not create one.
    # So the recall now needs a contest to resolve: keeping this rung must actually DENY the
    # richer owner the lot it qualifies for.  Room is measured the way the cluster cap
    # measures everything — dollars — and the owner's claim is bounded by the LOT container,
    # the smallest presence it can re-post with, which is the conservative (fewer recalls)
    # end of the mirror.
    # MIRROR (recalling too EAGERLY ↔ never recalling): too eagerly is measured above, a book
    # cancelled to zero by a rule that never asked whether anything was scarce; never
    # recalling re-admits the 1.155 incident, where new capital funded a $0 pot while the
    # earning rung drifted toward the forfeit cliff.  The seam between them is whether the
    # SAME DOLLARS are being claimed twice.
    # BUDGET-scarcity is deliberately NOT tested here: it is not knowable before the water
    # level runs, and it already has its own resolver — the displacement pass at capacity,
    # which ranks by expected credit with the banked pot on the incumbent's side.
    slot_by_key = {}
    for _s in slots:
        slot_by_key.setdefault(_s.key, _s)

    def _recall_conflict(s, ck, owner_key):
        """Do this rung's dollars actually stand between the owner and a fundable lot?

        Returns (contested, detail).  `detail` is logged with every recall so a future wave is
        one journal line, not an archaeology project."""
        if cluster_cap_usd is None:
            return False, {}                  # no dollar bound ⇒ no contested dollar
        cap = float(cluster_cap_usd)
        # WHAT A RECALL WOULD ACTUALLY FREE — that, and not a cent more, is this rung's claim
        # on the contested dollars.  Only RESTING dollars are recallable: a cancel returns
        # collateral, a POSITION rides (2026-07-30, "positions RIDE"), so a rung that holds
        # inventory and rests nothing can be cancelled all day and free nothing — there is no
        # contest for it to resolve.  A rung with neither would take a fresh lot, so a lot is
        # what it claims.  (Charging a resting rung for growth it MIGHT later be granted
        # inflates every contest toward a recall, which is the direction the live wave went.)
        sib_resting = float((resting or {}).get(s.key, 0.0)) * s.p
        sib_held = float((held or {}).get(s.key, 0.0)) * s.p
        if sib_resting > 0:
            sib_claim = sib_resting
        elif sib_held > 0:
            return False, {}                  # nothing a cancel can free
        else:
            sib_claim = min(float(caps.inv_cap_usd), cap)
        o_slot = slot_by_key.get(owner_key)
        if o_slot is None and owner_key[1] is None:
            o_slot = next((x for x in slots if x.ticker == owner_key[0]), None)
        o_p = o_slot.p if o_slot is not None else s.p
        o_hq = float((held or {}).get(owner_key, 0.0)) + \
            float((resting or {}).get(owner_key, 0.0))
        owner_committed = o_hq * o_p
        if o_slot is not None:
            q_o = cliff_clearing_q(o_slot)
            owner_target = 0.0 if q_o is None else min(max(1, int(q_o)) * o_p,
                                                       float(caps.inv_cap_usd))
        else:
            # The owner has no slot this cycle (the classification gap that WAS the 1.155
            # incident).  It cannot be sized, so it is credited with exactly one lot — the
            # minimum presence it needs when it reappears — and no more.
            owner_target = float(caps.inv_cap_usd)
        owner_need = max(0.0, owner_target - owner_committed)
        others = max(0.0, per_cluster.get(ck, 0.0) - sib_resting)
        room_for_owner = cap - others - sib_claim
        contested = owner_need > 0.0 and room_for_owner + 1e-9 < owner_need
        return contested, {"cluster": ck, "cluster_cap_usd": round(cap, 4),
                           "committed_usd": round(per_cluster.get(ck, 0.0), 4),
                           "sibling_claim_usd": round(sib_claim, 4),
                           "owner_need_usd": round(owner_need, 4),
                           "room_for_owner_usd": round(room_for_owner, 4),
                           "freed_usd": round(sib_resting, 4)}

    recalled = set()
    elig = []
    for s in slots:
        # ── OWNER DISPLACEMENT (Ryan, 2026-07-30: "1.153 has earned one cent, 1.155 has
        # earned 26 — cancel 1.153, open 1.155").  Accrued credit is BANKED expected value:
        # a cluster whose owner holds strictly more accrued credit than a resting non-owner
        # rung RECALLS that rung — its resting seed is withheld, the requoter's q=0 path
        # cancels it, and the reserve points at the owner.  This deliberately overrides the
        # period lock for the displaced rung: forfeiting 1c of accrual to keep presence
        # compounding a 26c pot is the trade, every time.  Displacement requires the owner's
        # accrual to STRICTLY exceed the sibling's (no flapping: the ordering only deepens).
        _own_acc = owner_accrued or {}
        # A RESTING ORDER IS ALREADY THIS SLOT'S ALLOCATION, not a rival for it.  Seeding the
        # cluster tally from resting orders (so the plan stops over-planning) without ALSO
        # crediting the slot that owns them makes the plan oscillate: cycle 1 plans and
        # places, cycle 2 sees the cluster full and plans 0 so the requoter CANCELS, cycle 3
        # plans again — a cancel/replace loop that also reads as a bookkeeping failure to the
        # burst breaker (it halted the four-rung ladder at iteration 33).  Starting the slot
        # at what it already rests makes "keep it" the plan's default and leaves the water
        # level to decide only about the MARGIN.
        _ck0 = _cluster_key(s)
        _owner0 = cluster_owner.get(_ck0)
        _sib_acc = float(getattr(s, "accrued", 0.0) or 0.0)
        _own_a = float(_own_acc.get(_ck0, 0.0))
        _outranked = (_owner0 is not None and _owner0 != s.key
                      and not (_owner0[1] is None and _owner0[0] == s.ticker)
                      and _own_a > _sib_acc + 1e-9)
        _contested, _detail = _recall_conflict(s, _ck0, _owner0) if _outranked else (False, {})
        if _outranked and _contested:
            alloc[s.key] = int(q_alloc.get(s.key, 0))     # displaced: no resting seed ⇒
                                                          # the requoter recalls the order
            recalled.add(s.key)                           # …and nothing re-funds it today
            # A RECALL IS A CANCEL, AND CANCELLED DOLLARS COME HOME.  Without this the
            # cluster tally still counted the recalled rung's collateral, so the seat we
            # just emptied was still full: the owner could not fund, and the book paid a
            # cancel for nothing — strictly worse than leaving the rung alone.  Held
            # inventory is NOT returned (it rides; only resting collateral is freed).
            _freed = float((resting or {}).get(s.key, 0.0)) * s.p
            if _freed > 0:
                per_cluster[_ck0] = max(0.0, per_cluster.get(_ck0, 0.0) - _freed)
                per_cluster_px[_ck0] = max(0.0,
                                           per_cluster_px.get(_ck0, 0.0) - _freed * s.p)
            # ONE LINE, BOTH SIDES, AND THE DOLLARS IN DISPUTE.  The live wave was diagnosed
            # from a pass2_refused counter that could only say "owner_recalled"; a recall is a
            # CANCEL of a live order and must say who took the seat and what it cost.
            R.log("rung_recalled", ticker=s.ticker, side=s.side,
                  recalled_accrued=round(_sib_acc, 6),
                  kept=_owner0[0], kept_side=_owner0[1],
                  kept_accrued=round(_own_a, 6), **_detail)
        else:
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

    # The eligible set BEFORE the cliff pass starts removing from it — pass 2 below must be
    # able to reconsider exactly the rungs the cliff pass zeroed, since "its cliff-clearing
    # lot did not fit the lot container" is the failure pass 2 exists to answer.
    elig_all = list(elig)

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

    # THE CLIFF PASS FREES CAPITAL — SOMETHING MUST THEN SPEND IT.  Water-filling runs to
    # exhaustion, then the cliff pass zeroes rungs that cannot reach $2 and returns their
    # dollars to the budget.  With a single pass those dollars simply EVAPORATED: the loop
    # that would spend them had already finished, so the book deployed $30 of $300 while
    # nothing was refused and nothing was over-cap — capital was not moved elsewhere, it was
    # not allocated at all.  So: water-fill, prune, and if the prune freed anything, water-fill
    # AGAIN over the survivors.  Bounded by MAX_GATE_PASSES, and monotone because every pass
    # permanently removes at least one slot from `elig`.
    cliff_dead = set()
    for _cliff_pass in range(int(C.MAX_GATE_PASSES)):
        elig = [x for x in elig if x.key not in cliff_dead]
        unaffordable = set()
        var_blocked = set()         # D11 — retested every pass, never a permanent refusal
        _why, _ex = {}, []          # WHY each slot was passed over — one line per cycle
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
                # ── SUPERSEDED 2026-07-29 night (note 52 D6): the per-slot cap bounds THE
                # RESTING LOT, not net exposure.  v1 §8.1's NET form (held + resting ≤ n_cap)
                # killed the replenish by construction: a fully-filled lot left zero room, so
                # presence died on the first fill — the exact failure the reserve exists to
                # prevent.  Cumulative acquisition is the CLUSTER RESERVE's job now: the
                # cluster term below is seeded with held + resting, so after (1 + refills)
                # lots the cluster is at cap and the re-post is refused THERE, cleanly.
                if q + 1 > n_cap(s.p, caps):
                    _why["slot_cap"] = _why.get("slot_cap", 0) + 1
                    continue
                if per_market.get(s.ticker, 0.0) + s.p > market_cap_usd(s, budget_usd, caps) + 1e-9:
                    _why["market_cap"] = _why.get("market_cap", 0) + 1
                    continue
                vcap = venue_caps.get(s.venue)
                if vcap is not None and per_venue.get(s.venue, 0.0) + s.p > float(vcap) + 1e-9:
                    _why["venue_cap"] = _why.get("venue_cap", 0) + 1
                    continue
                # NEW-1b: the cluster cap `place()` will apply, applied HERE so the plan is
                # fundable.  Caps do not compose (clusters.py's own note): a cluster spanning
                # several series inherits several per-venue caps, so this term is not implied by
                # the one above.
                if cluster_cap_usd is not None and \
                        per_cluster.get(_cluster_key(s), 0.0) + s.p > float(cluster_cap_usd) + 1e-9:
                    _why["cluster_cap"] = _why.get("cluster_cap", 0) + 1
                    continue
                # D5′ (Ryan, 2026-07-30: "why shouldn't we make that not a requirement") —
                # THE CLUSTER BOUND IS DOLLARS, NOT A RUNG COUNT.  The count refusal that
                # stood here is GONE; the line above it is the whole rule now.
                #
                # WHY THE COUNT WAS NEVER THE RISK STATEMENT.  D5's argument was that a second
                # rung in a cluster spends the first rung's refill reserve.  True — and the
                # thing that stops it is the reserve itself, which `cluster_cap_usd` already
                # enforces on the same dollars, per cluster, in the plan AND at the rail.  The
                # count added nothing to the worst case (one $10 rung and four $2.50 rungs lose
                # the same $10 if the settle source goes against us) and subtracted a great
                # deal of earning: MEASURED 2026-07-30, `cluster_owned` refused 270 candidates
                # per cycle and ALL 76 pass-2 candidates, with ~$234 of the budget idle.
                # The correlation evidence is what makes the dollar bound sufficient: the
                # treasury tenors' daily settle directions agreed 9/9 across pairs over 13
                # settled days, i.e. a cluster really is one bet — so bounding the BET in
                # dollars is the honest control, and rationing it into one rung is a control on
                # the wrong variable.  General, not treasury-only: the same arithmetic holds
                # for any cluster, because the worst case is the cap either way.
                # WHAT REMAINS OF D5: `cluster_owner` is still tracked, because OWNER
                # DISPLACEMENT (the accrual-ranked seed withholding above) still needs to know
                # which rung the cluster's accrued credit belongs to.  Only the REFUSAL is
                # retired, never the bookkeeping.
                # MIRROR (many small rungs ↔ one big one): many small rungs inside the same
                # dollar cap diversify MARKET risk within the settle source and cost more
                # refills; one big rung concentrates it and refills cheaply.  The cliff pass
                # already prefers fewer/bigger where the budget cannot fund both, so the plan
                # keeps that preference without needing a hard count.
                # What DID survive is the accrual-ranked recall — see `recalled` above.
                if s.key in recalled:
                    _why["owner_recalled"] = _why.get("owner_recalled", 0) + 1
                    continue
                _ck = _cluster_key(s)
                # D11 — the plan-side variance test, charged at the CLUSTER RESERVE: a funded
                # rung's cluster can grow to (1+refills) lots through the requoter's re-posts
                # without ever passing this loop again, so the conservative charge is what the
                # cluster can BECOME, not the first lot.  (Charging one lot passes 73 clusters
                # of 2c rungs; charging the reserve stops at ~4 — computed, and the reserve is
                # the number the rail actually permits.)
                if ceiling_usd and s.key not in var_blocked:
                    _reserve = float(cluster_cap_usd) if cluster_cap_usd else \
                        n_cap(s.p, caps) * s.p * (1.0 + C.RUNG_REFILLS)
                    _cont = max(0.0, _reserve - per_cluster.get(_ck, 0.0))
                    if _plan_var(_ck, _cont, _cont * s.p) > C.PORTFOLIO_VAR_MAX + 1e-12:
                        var_blocked.add(s.key)
                        _why["plan_var"] = _why.get("plan_var", 0) + 1
                        continue
                elif s.key in var_blocked:
                    _why["plan_var"] = _why.get("plan_var", 0) + 1
                    continue
                # ---- THE ONE SUBSTITUTION (spec §1.3): (★) replaces v1 §2.2's hurdle line ----
                r = s.net_at(q, r_star)
                if not M.admits(r, floor_rate):
                    _why["below_hurdle"] = _why.get("below_hurdle", 0) + 1
                    if len(_ex) < 3:
                        t = M.net_terms(s.rho, s.S, s.p, q, s.phi, s.d, s.l_eff, r_star,
                                        s.t_hat)
                        _ex.append({"tk": s.ticker, "side": s.side,
                                    "net": round(t["net"], 6), "gross": round(t["gross"], 6),
                                    "carry": round(t["carry"], 6),
                                    "drift": round(t["drift"], 6),
                                    "t_hat": round(t["t_hat"], 3), "S": round(s.S, 1),
                                    "p": s.p, "rho": round(s.rho, 4)})
                    continue
                # -------------------------------------------------------------------------------
                if r > best_rate + 1e-15 or (abs(r - best_rate) <= 1e-15 and best is not None
                                             and (s.ticker, s.side) < (best.ticker, best.side)):
                    best, best_rate = s, r
            if best is None:
                break
            step = max(1, int(round(step_fraction * budget_usd / best.p)))    # v1 §2.5
            step = min(step, n_cap(best.p, caps) - alloc[best.key])   # lot container (D6)
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
            per_cluster_px[best_ck] = per_cluster_px.get(best_ck, 0.0) + step * best.p * best.p
            cluster_owner.setdefault(best_ck, best.key)       # D5 — the funded rung owns it
            last_rate = best_rate
        # ---- THE CLIFF PASS.  A rung under $2 of projected accrual pays ZERO. ------------
        # Water-filling maximises RATE and is blind to the forfeit cliff, so it happily leaves
        # eight rungs at $3-9 each — which is what v5 did live on 2026-07-28, earning 2-9 CENTS
        # per treasury market while v4, with $10-50 on a handful of rungs, cleared dollars.
        # Reward is share and share is ~q/S, so size is LINEAR in earnings; below the cliff,
        # linear-in-nothing is nothing.  Every funded rung is therefore raised to its
        # cliff-clearing size if the caps and budget allow, and ZEROED if they do not — freeing
        # its capital for a rung that can clear.  Richest-first, because when the budget cannot
        # fund every rung above the cliff the right book is FEWER, BIGGER rungs.
        freed_any = False
        by_rate = sorted((k for k, q in alloc.items() if q > 0),
                         key=lambda k: -next((sl.net_at(alloc[k], r_star)
                                              for sl in elig if sl.key == k), 0.0))
        slot_of = {sl.key: sl for sl in elig}
        for key in by_rate:
            sl = slot_of.get(key)
            if sl is None:
                continue                                  # land-grab rungs: sized by §4.5
            if key in locked and alloc.get(key, 0) > 0:
                continue                                  # D12 — a FUNDED rung is locked for
                                                          # its period: zeroing it cancels the
                                                          # resting order and forfeits the
                                                          # whole $1.00 for a fraction.  Shed,
                                                          # kill and day-stop still override.
            if float(getattr(sl, "accrued", 0.0) or 0.0) > 0:
                continue                                  # accrued dollars are the RESCUE's
                                                          # business: it prices the recovery
                                                          # value of the stranded accrual and
                                                          # decides TOP_UP vs ABANDON.  A blunt
                                                          # cliff drop here would throw away the
                                                          # accrual the rescue exists to save.
            q_min = cliff_clearing_q(sl)
            q_now = alloc[key]
            if q_min is not None and q_now >= q_min:
                continue                                  # already clears
            if q_min is not None:
                add = q_min - q_now
                cost = add * sl.p
                room_ok = (spent + cost <= budget_usd + 1e-9
                           and q_min <= n_cap(sl.p, caps)
                           and per_market.get(sl.ticker, 0.0) + cost
                           <= market_cap_usd(sl, budget_usd, caps) + 1e-9)
                ck = _cluster_key(sl)
                if room_ok and cluster_cap_usd is not None:
                    room_ok = per_cluster.get(ck, 0.0) + cost <= float(cluster_cap_usd) + 1e-9
                if room_ok:
                    alloc[key] = q_min
                    spent += cost
                    per_market[sl.ticker] = per_market.get(sl.ticker, 0.0) + cost
                    per_venue[sl.venue] = per_venue.get(sl.venue, 0.0) + cost
                    per_cluster[ck] = per_cluster.get(ck, 0.0) + cost
                    per_cluster_px[ck] = per_cluster_px.get(ck, 0.0) + cost * sl.p
                    continue
            # cannot reach the cliff here — free the capital for a rung that can
            freed = q_now * sl.p
            alloc[key] = 0
            spent = max(0.0, spent - freed)
            per_market[sl.ticker] = max(0.0, per_market.get(sl.ticker, 0.0) - freed)
            per_venue[sl.venue] = max(0.0, per_venue.get(sl.venue, 0.0) - freed)
            ck = _cluster_key(sl)
            per_cluster[ck] = max(0.0, per_cluster.get(ck, 0.0) - freed)
            per_cluster_px[ck] = max(0.0, per_cluster_px.get(ck, 0.0) - freed * sl.p)
            if cluster_owner.get(ck) == sl.key:
                cluster_owner.pop(ck, None)               # D5 — a zeroed rung frees its
                                                          # cluster for another candidate
            cliff_dead.add(key)
            freed_any = freed_any or freed > 0
            R.log("below_cliff_dropped", ticker=sl.ticker, side=sl.side, had=q_now,
                  needed=q_min, freed_usd=round(freed, 4))
        # WHY THE BOOK IS THE SIZE IT IS.  Four hours of eliminating one candidate per deploy
        # cycle is what this line replaces: it names the term that passed each slot over, and
        # carries three worked examples so the numbers can be argued with directly.
        if _why:
            R.log("alloc_reasons", eligible=len(elig), spent=round(spent, 2),
                  r_star=round(r_star, 6), **{k: v for k, v in sorted(_why.items())})
            for e in _ex:
                R.log("alloc_example", **e)
        # Nothing pruned ⇒ the plan is stable; anything pruned ⇒ re-water-fill its dollars.
        if not freed_any:
            break

    # =========================================================================================
    # PASS 2 — THE IDLE-CAPITAL SWEEP, and DISPLACEMENT AT CAPACITY.
    #
    # IDLE CAPITAL IS WASTED (Ryan, 2026-07-30).  Everything above sizes a rung inside the LOT
    # CONTAINER — `INV_CAP_USD` = reserve/2 — because the container is what makes refills
    # possible: a $2 lot leaves the cluster reserve room for four re-posts as fills convert
    # resting into inventory.  That is the right shape for capital that is SCARCE.  It is the
    # wrong shape for capital that is IDLE: a rung whose floor-clearing lot costs $8 is
    # refused outright by a $5 container, so its dollars earn NOTHING, while the same $8 as a
    # single reserve-consuming lot with zero refills earns whatever that rung pays.  Zero
    # refills is a worse rung, not a worthless one, and a worse rung beats an empty one.
    #
    # So: once the water-fill + cliff passes have converged, any budget still unspent gets ONE
    # more pass over the rungs the container refused, with the per-slot dollar bound raised
    # from the lot to the FULL CLUSTER RESERVE (`cluster_cap_usd`) — the same dollars the
    # cluster rail already permits for that settle source, so nothing downstream has to be
    # relaxed to let it through.  EVERYTHING ELSE STILL BINDS, and binds through the same
    # `_fund_lot` gate the displacement below uses: one rung per cluster (D5), the plan-side
    # variance test charged at the reserve (D11 — unchanged, because it was ALREADY charging
    # the reserve, which is exactly why pass 2 needs no new variance argument), the cluster
    # rail, the per-market and per-venue caps, the (★) hurdle, and the budget.
    #
    # THE ADMISSION CRITERION IS THE ONE THAT ALREADY EXISTS: `cliff_clearing_q`.  A rung is
    # funded at the smallest size that reaches the forfeit floor, or not at all — GUARANTEED
    # FORFEIT BEATS NOTHING is false, and its converse is the whole point of the cliff pass.
    # A rung that cannot reach the floor at any size (q is None) is never funded here, at any
    # level of idleness.
    # MIRROR (pass 2 too EAGER ↔ absent): too eager spends the refill reserve of a cluster
    # that would rather have re-posted — bounded, because a cluster with an owner is skipped
    # and the reserve is per-cluster, so pass 2 can only ever consume the reserve of a cluster
    # that has NO rung at all.  Absent is measured: idle dollars, which is the state this book
    # has been in every cycle its container refused an $8 lot.
    # =========================================================================================
    def _apply_delta(sl, dq):
        """Move a rung's allocation by `dq` contracts, carrying every running tally with it —
        so a funding and an un-funding are the same operation with opposite sign, and neither
        can leave the tallies disagreeing with `alloc` (the class of defect that let the plan
        and the rails measure different books)."""
        nonlocal spent
        ck = _cluster_key(sl)
        cost = dq * sl.p
        alloc[sl.key] = alloc.get(sl.key, 0) + dq
        spent = max(0.0, spent + cost)
        per_market[sl.ticker] = max(0.0, per_market.get(sl.ticker, 0.0) + cost)
        per_venue[sl.venue] = max(0.0, per_venue.get(sl.venue, 0.0) + cost)
        per_cluster[ck] = max(0.0, per_cluster.get(ck, 0.0) + cost)
        per_cluster_px[ck] = max(0.0, per_cluster_px.get(ck, 0.0) + cost * sl.p)
        if dq > 0:
            cluster_owner.setdefault(ck, sl.key)
        elif alloc.get(sl.key, 0) <= 0 and cluster_owner.get(ck) == sl.key:
            cluster_owner.pop(ck, None)

    def _fund_lot(sl, q_target, lot_cap_usd):
        """Try to raise `sl` to `q_target` as ONE lot.  Returns (funded, blocking_term).

        THE BUDGET IS TESTED LAST, DELIBERATELY.  "budget" as the blocking term therefore
        means *every other rail admitted this rung and only the money was missing* — which is
        precisely the precondition displacement needs, and the reason it can be read off this
        function instead of re-derived (a second derivation of the same admission is how a
        plan and a rail come to disagree)."""
        q_now = alloc.get(sl.key, 0)
        if q_target <= q_now:
            return False, "already_funded"
        dq = q_target - q_now
        cost = dq * sl.p
        if sl.key in recalled:
            return False, "owner_recalled"                # accrual seniority, not a count
        ck = _cluster_key(sl)
        # D5′ 2026-07-30 — the one-rung-per-cluster refusal is RETIRED here too, and this is
        # where it bit hardest: MEASURED, `pass2_refused` reported cluster_owned as the
        # blocking term for ALL 76 candidates while ~$234 sat idle, i.e. the sweep built to
        # spend idle capital was refusing every single rung on a COUNT while the DOLLAR cap
        # below had room.  The cluster cap is the bound; the count was a second, cruder copy
        # of it that could only ever be more restrictive.
        if q_target * sl.p > float(lot_cap_usd) + 1e-9:
            return False, "lot_cap"
        if per_market.get(sl.ticker, 0.0) + cost > market_cap_usd(sl, budget_usd, caps) + 1e-9:
            return False, "market_cap"
        vcap = venue_caps.get(sl.venue)
        if vcap is not None and per_venue.get(sl.venue, 0.0) + cost > float(vcap) + 1e-9:
            return False, "venue_cap"
        if cluster_cap_usd is not None and \
                per_cluster.get(ck, 0.0) + cost > float(cluster_cap_usd) + 1e-9:
            return False, "cluster_cap"
        if ceiling_usd:
            # D11, charged exactly as the water-fill charges it: at what the cluster can
            # BECOME, not at this lot.
            _cont = max(0.0, (float(cluster_cap_usd) if cluster_cap_usd else cost)
                        - per_cluster.get(ck, 0.0))
            if _plan_var(ck, _cont, _cont * sl.p) > C.PORTFOLIO_VAR_MAX + 1e-12:
                return False, "plan_var"
        if not M.admits(sl.net_at(q_target, r_star), floor_rate):
            return False, "below_hurdle"                      # (★) still governs entry
        if spent + cost > budget_usd + 1e-9:
            return False, "budget"
        _apply_delta(sl, dq)
        return True, "ok"

    def _credit_usd(sl, q):
        """Expected credit in DOLLARS over the horizon we are judged on:
        `share(q,S) × (ρ/2) × min(hours_left, PAYOUT_HORIZON_H)` — the same product
        `projected_period_payout` uses, so the two can never disagree about what a rung is
        worth."""
        h_eff = min(max(0.0, float(sl.hours_left)), C.PAYOUT_HORIZON_H)
        return our_share(q, sl.S) * (float(sl.rho) / 2.0) * h_eff

    candidates = []
    if cluster_cap_usd is not None:
        for sl in elig_all:
            if alloc.get(sl.key, 0) > 0:
                continue                                      # already carries money
            q_min = cliff_clearing_q(sl)
            if not q_min:
                continue                                      # unreachable, or already clear
            candidates.append((sl, int(q_min)))
        candidates.sort(key=lambda sq: (-_credit_usd(sq[0], sq[1]), sq[0].ticker, sq[0].side))

    blocked = []
    p2_n, p2_usd = 0, 0.0
    p2_refused = {}
    for sl, q_min in candidates:
        ok, why = _fund_lot(sl, q_min, float(cluster_cap_usd))
        if ok:
            p2_n += 1
            p2_usd += q_min * sl.p
        else:
            p2_refused[why] = p2_refused.get(why, 0) + 1
            if why == "budget":
                blocked.append((sl, q_min))
    if p2_n:
        R.log("pass2_funded", rungs=p2_n, usd=round(p2_usd, 4),
              idle_left=round(max(0.0, budget_usd - spent), 4),
              lot_cap_usd=round(float(cluster_cap_usd), 4))
    if p2_refused:
        # No silent caps: an idle-capital pass that funds nothing must say WHICH gate ate
        # every candidate, or "$170 idle, pass 2 dark" is undiagnosable from the tape.
        R.log("pass2_refused", candidates=len(candidates), funded=p2_n,
              idle_left=round(max(0.0, budget_usd - spent), 4), reasons=p2_refused)

    # ── DISPLACEMENT AT CAPACITY — CALCULABLE, NOT RULED (Ryan: "which makes more total is
    # calculable").  Only reached when the budget is the ONLY thing refusing a candidate, i.e.
    # the book is full.  Then the question is not "may we?" but "which rung makes more?", and
    # both sides are the same product in the same units:
    #
    #     E_new  = share(q_new , S_new ) × (ρ_new /2) × min(h_new , PAYOUT_HORIZON_H)
    #     E_keep = accrued_worst + share(q_worst, S_worst) × (ρ_worst/2) × min(h_worst, …)
    #
    # THE ACCRUED TERM IS THE WHOLE HYSTERESIS, and it is why no anti-flap constant is needed.
    # Cancelling a rung's presence forfeits its banked pot (the cliff is a cliff, not a
    # taper), so the incumbent is charged with what it would DESTROY as well as what it would
    # stop earning — and that pot only grows with every hour it keeps resting.  A displaced
    # rung is therefore progressively harder to displace the longer it has earned, which is
    # exactly the seniority note 52 D6 argues for, arriving from the arithmetic instead of
    # from a threshold.  Ties KEEP THE INCUMBENT (strict >): an equal-value swap is pure churn
    # — two wire calls, a forfeited pot, and the same expected dollars.
    #
    # POSITIONS ARE NEVER DISPLACED.  Only a RESTING allocation can be recalled; held
    # inventory rides (the 2026-07-30 "positions RIDE" decision — every exit path is DELETED,
    # and pretending otherwise would have the planner "free" dollars that no cancel can
    # actually free.
    #
    # The recall itself reuses the OWNER-DISPLACEMENT mechanism above: zeroing the incumbent's
    # alloc withholds the keep-it resting seed, so the requoter's q=0 path cancels the order.
    # This deliberately overrides D12's period lock for the displaced rung, on the same
    # argument the owner case already makes ("forfeiting 1c of accrual to keep presence
    # compounding a 26c pot is the trade, every time") — and now with the pot on BOTH sides of
    # the inequality rather than assumed.
    for sl, q_min in blocked:
        if alloc.get(sl.key, 0) > 0:
            continue
        e_new = _credit_usd(sl, q_min)
        slot_by_key = {x.key: x for x in slots}
        worst_key, worst_e, worst_sl = None, None, None
        for k, q in sorted(alloc.items()):
            if q <= 0 or k == sl.key:
                continue
            if float((held or {}).get(k, 0.0)) > 0:
                continue                                      # a POSITION: never displaced
            x = slot_by_key.get(k)
            if x is None:
                continue                                      # land grab / no slot this cycle
            e_keep = float(getattr(x, "accrued", 0.0) or 0.0) + _credit_usd(x, q)
            if worst_e is None or e_keep < worst_e:
                worst_key, worst_e, worst_sl = k, e_keep, x
        if worst_key is None or not (e_new > worst_e + 1e-12):
            continue                                          # ties keep the incumbent
        freed_q = alloc[worst_key]
        _apply_delta(worst_sl, -freed_q)
        ok, why = _fund_lot(sl, q_min, float(cluster_cap_usd))
        if not ok:
            _apply_delta(worst_sl, freed_q)                   # the swap did not pay: restore
            continue
        R.log("rung_displaced", took=sl.ticker, took_side=sl.side, took_q=q_min,
              e_new_usd=round(e_new, 6),
              dropped=worst_sl.ticker, dropped_side=worst_sl.side, dropped_q=freed_q,
              e_keep_usd=round(worst_e, 6),
              accrued_at_risk_usd=round(float(getattr(worst_sl, "accrued", 0.0) or 0.0), 6))

    return alloc, spent, last_rate


def cliff_clearing_q(slot, target_usd=None):
    """The SMALLEST size on this rung that can still reach the $1 forfeit cliff.

    Solve `share(q) x (rho/2) x hours_left + accrued >= target` for q, with
    `share = q/(q+S)`:

        need  = target - accrued
        avail = (rho/2) x hours_left          the whole side's remaining pool
        share_needed = need / avail
        q = S x share_needed / (1 - share_needed)

    THE POINT.  A rung that accrues $0.90 pays ZERO, so capital below this size is not "less
    earning", it is NO earning — measured live 2026-07-28: v5 held eight treasury rungs at
    $3-9 each and their per-market estimates were 2-9 CENTS, while v4 put $10-50 on a handful
    of rungs and cleared dollars.  Reward is share, share is ~q/S for q << S, so it is LINEAR
    in size: a third of the size earns a third as much, and below the cliff a third as much
    of nothing is nothing.

    Returns None when the cliff is unreachable at ANY size (share_needed >= 1): the whole
    side's remaining pool cannot pay $1, so no amount of capital rescues it.
    """
    # TARGET THE ENTRY FLOOR, NOT THE BARE CLIFF.  v1 §3.1 set ENTRY_FLOOR_USD = 2.00 as
    # "2x the $1.00 payout cliff", and the doubling is margin for the three things that move
    # between sizing and payout: FILLS take us out of the book (resting hours < window hours),
    # RIVALS add size and dilute our share, and our own rate model can be optimistic.  Sizing
    # to exactly $1.00 leaves zero headroom for any of them, and a rung that lands at $0.95
    # pays the same as a rung that lands at zero.
    target_usd = C.ENTRY_FLOOR_USD if target_usd is None else float(target_usd)
    # Same horizon: a rung that only clears $2 by Saturday has not cleared it for a book that
    # is judged tomorrow, and sizing it as if it had is how capital ends up parked in weeklies.
    avail = (float(slot.rho) / 2.0) * min(max(0.0, float(slot.hours_left)),
                                          C.PAYOUT_HORIZON_H)
    need = max(0.0, float(target_usd) - float(getattr(slot, "accrued", 0.0) or 0.0))
    if need <= 0:
        return 0
    if avail <= 0:
        return None
    share_needed = need / avail
    if share_needed >= 1.0:
        return None                                   # unreachable at any size
    return int(math.ceil(float(slot.S) * share_needed / (1.0 - share_needed)))


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
        # ONLY THE ACCRUAL WE CAN COLLECT INSIDE THE HORIZON COUNTS (config.PAYOUT_HORIZON_H):
        # a 166-hour weekly is judged on the day of it we can actually bank, because the
        # capital behind it cannot be recycled into nearer windows meanwhile.
        h_eff = min(max(0.0, s.hours_left), C.PAYOUT_HORIZON_H)
        total += our_share(q, s.S) * (s.rho / 2.0) * h_eff
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
    max_q = n_cap(best.p, caps)          # the LOT container bounds the top-up order (D6);
                                         # cumulative acquisition is the cluster reserve's job
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
                               cluster_cap_usd=None, cluster_seed=None,
                               cluster_seed_px=None, ceiling_usd=None, owner_seed=None,
                               owner_accrued=None):
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
    # D12 — programs carrying a FUNDED key (money resting or held) are exempt from the
    # entry-floor drop below: dropping a funded program cancels its resting order mid-period
    # and forfeits the whole $1.00.  The rescue's ABANDON (A > 0) is untouched — that is an
    # exit decision with the stranded accrual priced, not an entry re-litigation.
    funded_keys = {k for k, v in list((held or {}).items()) + list((resting or {}).items())
                   if float(v) > 0}
    alloc, spent, marginal = {}, 0.0, 0.0
    for _ in range(int(max_passes)):
        live = [s for s in slots if s.program_id not in dropped]
        alloc, spent, marginal = allocate(live, budget_usd, r_star, caps, floor_rate,
                                          venue_caps, held=held, resting=resting,
                                          cluster_cap_usd=cluster_cap_usd,
                                          cluster_seed=cluster_seed,
                                          cluster_seed_px=cluster_seed_px,
                                          ceiling_usd=ceiling_usd, owner_seed=owner_seed,
                                          owner_accrued=owner_accrued)
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
            # OWNER DISPLACEMENT AT THE GATE: a program whose cluster is owned by a
            # DIFFERENT rung with strictly more banked accrual is neither rescued nor
            # re-litigated here — its capital already belongs to the owner (the 3-lot
            # top-up that re-entered 1.153 through this path, 2026-07-30).
            _ck1 = _cluster_key(ps[0])
            _own1 = (owner_seed or {}).get(_ck1)
            _displaced = _own1 is not None and                 all(_own1 != s.key and
                    not (_own1[1] is None and _own1[0] == s.ticker) for s in ps) and                 float((owner_accrued or {}).get(_ck1, 0.0)) > A + 1e-9
            if _displaced:
                continue
            if A <= 0.0:
                if any(s.key in funded_keys for s in ps):
                    continue                                 # D12: funded ⇒ never re-litigated
                if proj > 0.0:
                    newly.append(pid)                        # pure entry: the floor decides
                continue
            res, _best = _cliff_decision(ps, alloc, r_star, caps, venue_caps, held,
                                         budget_usd - spent, pv_spend,
                                         cluster_cap_usd, pc_spend)
            if res.action == ABANDON:
                # ── D12 × note 49 R1: AN UNMEASURED PROBABILITY MAY NOT EVICT A FUNDED RUNG.
                # `rescue`'s p_recover is an input nobody measures, so it defaults to 0 — and
                # at p_recover = 0, ABANDON beats HOLD for every funded-but-below-floor rung,
                # which turns the gate into a churn engine: rivals deepen the book, the floor
                # recedes, and the gate cancels a resting order mid-period, forfeiting the
                # accrual for a fraction.  While the cliff is still REACHABLE at the ρ/2
                # ceiling, "we cannot price the recovery" is a reason to HOLD what is funded,
                # not to evict it.  A cliff unreachable at the ceiling is different in kind:
                # that p_recover = 0 is COMPUTED, not defaulted, and the abandon stands.
                reachable = A + (float(_best.rho) / 2.0) * max(0.0, float(_best.hours_left)) \
                    >= C.RESCUE_TARGET_USD - 1e-12
                if reachable and any(s.key in funded_keys for s in ps):
                    R.log("cliff_hold_funded", program_id=str(pid), accrued=A,
                          proj=res.proj, note="unmeasured p_recover; funded rung holds")
                    continue
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
        _ck2 = _cluster_key(ps[0])
        _own2 = (owner_seed or {}).get(_ck2)
        if _own2 is not None and                 all(_own2 != s.key and
                    not (_own2[1] is None and _own2[0] == s.ticker) for s in ps) and                 float((owner_accrued or {}).get(_ck2, 0.0)) > A + 1e-9:
            continue                          # displaced: the owner's pot outranks this one
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
                        cluster_cap_usd=None, cluster_seed=None,
                        cluster_seed_px=None, ceiling_usd=None, owner_seed=None,
                        owner_accrued=None):
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
            held=held, resting=resting, cluster_cap_usd=cluster_cap_usd,
            cluster_seed=cluster_seed, cluster_seed_px=cluster_seed_px,
            ceiling_usd=ceiling_usd, owner_seed=owner_seed, owner_accrued=owner_accrued)
        return (a, sp, dropped), (marg if marg > 0 else floor_rate)

    res = M.solve_rstar(lambda r: run(r), r0)
    if not res.converged:
        R.log("rstar_no_converge", r_star=res.r_star, trace=res.trace)
    alloc, spent, dropped = res.alloc
    res.dropped = dropped
    return alloc, spent, res
