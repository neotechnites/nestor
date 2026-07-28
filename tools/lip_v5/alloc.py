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
                       max_frac=C.LAND_GRAB_MAX_COLLATERAL_FRAC):
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
    """
    caps = caps or Caps()
    alloc, spent = {}, 0.0
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
            alloc[s.key] = qty
            spent += cost
            took = True
        if took:
            markets += 1
    return alloc, spent


# =============================================================================================
# ALLOCATE  (spec §1.3)
# =============================================================================================
def allocate(slots, budget_usd, r_star, caps=None, floor_rate=C.FLOOR_RATE_PER_H,
             venue_caps=None, step_fraction=C.STEP_FRACTION):
    """Marginal-rate water-filling under (★).  Returns (alloc, spent, marginal_at_stop).

    `venue_caps`: {venue: cap_usd} from the ratchet (spec §1.4).  MIRROR (ratchet raises venue
    caps ↔ Σ venue caps vs the global ceiling): Σ caps MAY exceed the ceiling; **Σ ALLOCATED
    NEVER DOES**, because `budget_usd` binds here independently of every cap (T-R4b).

    v4's derived deviation from v1's pseudocode is KEPT: line 10's `break` when the CURRENT
    best slot can no longer afford one contract would abandon the remaining budget even when a
    CHEAPER slot could still absorb it.  Such a slot is marked permanently unaffordable
    (budget only ever decreases, so the exclusion can never be wrong) and the loop continues;
    it breaks only when NO slot can afford one more contract.
    """
    caps = caps or Caps()
    venue_caps = venue_caps or {}
    budget_usd = max(0.0, float(budget_usd))                 # a negative budget funds NOTHING

    alloc = {}
    q_alloc, q_spent = qualification_pass(slots, budget_usd, caps)
    budget_usd_rem = max(0.0, budget_usd - q_spent)

    elig = []
    for s in slots:
        alloc[s.key] = q_alloc.get(s.key, 0)
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
            if q + 1 > n_cap(s.p, caps):                     # v1 §8.1 per-slot inventory cap
                continue
            if per_market.get(s.ticker, 0.0) + s.p > market_cap_usd(s, budget_usd, caps) + 1e-9:
                continue
            vcap = venue_caps.get(s.venue)
            if vcap is not None and per_venue.get(s.venue, 0.0) + s.p > float(vcap) + 1e-9:
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
        step = min(step, n_cap(best.p, caps) - alloc[best.key])
        room = market_cap_usd(best, budget_usd, caps) - per_market.get(best.ticker, 0.0)
        step = min(step, int(room / best.p + 1e-9))
        vcap = venue_caps.get(best.venue)
        if vcap is not None:
            step = min(step, int((float(vcap) - per_venue.get(best.venue, 0.0)) / best.p + 1e-9))
        if spent + step * best.p > budget_usd + 1e-12:
            step = int((budget_usd - spent) / best.p + 1e-9)
        if step < 1:
            unaffordable.add(best.key)
            continue
        alloc[best.key] += step
        spent += step * best.p
        per_market[best.ticker] = per_market.get(best.ticker, 0.0) + step * best.p
        per_venue[best.venue] = per_venue.get(best.venue, 0.0) + step * best.p
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


def allocate_with_forfeit_gate(slots, budget_usd, r_star, caps=None,
                               floor_usd=C.ENTRY_FLOOR_USD, floor_rate=C.FLOOR_RATE_PER_H,
                               venue_caps=None, max_passes=C.MAX_GATE_PASSES):
    """v1 §2.4 lines 12-15 — the forfeit gate is per PROGRAM-PERIOD, applied AFTER
    water-filling, and a dropped program's dollars are RE-WATER-FILLED.

    MIRROR (ENTRY_FLOOR as an entry test ↔ the exit): the three-way KEEP/TOP_UP/HOLD/ABANDON
    (v1 §3.5-3.7) is the exit end, kept verbatim there; this is the entry end.
    """
    caps = caps or Caps()
    dropped = set()
    alloc, spent, marginal = {}, 0.0, 0.0
    for _ in range(int(max_passes)):
        live = [s for s in slots if s.program_id not in dropped]
        alloc, spent, marginal = allocate(live, budget_usd, r_star, caps, floor_rate,
                                          venue_caps)
        by_prog = {}
        for s in live:
            by_prog.setdefault(s.program_id, []).append(s)
        newly = []
        for pid, ps in sorted(by_prog.items(), key=lambda kv: str(kv[0])):
            proj = projected_period_payout(ps, alloc)
            if proj <= 0.0 or proj >= floor_usd:
                continue
            newly.append(pid)
        if not newly:
            break
        dropped |= set(newly)
    for s in slots:
        alloc.setdefault(s.key, 0)
        if s.program_id in dropped:
            alloc[s.key] = 0
    return alloc, spent, marginal, dropped


def allocate_with_rstar(slots, budget_usd, caps=None, trailing_rate=None,
                        floor_rate=C.FLOOR_RATE_PER_H, venue_caps=None):
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
    r0 = M.rstar_seed(trailing_rate, floor_rate)

    def run(r_star):
        a, sp, marg, dropped = allocate_with_forfeit_gate(
            slots, budget_usd, r_star, caps, floor_rate=floor_rate, venue_caps=venue_caps)
        return (a, sp, dropped), (marg if marg > 0 else floor_rate)

    res = M.solve_rstar(lambda r: run(r), r0)
    if not res.converged:
        R.log("rstar_no_converge", r_star=res.r_star, trace=res.trace)
    alloc, spent, dropped = res.alloc
    res.dropped = dropped
    return alloc, spent, res
