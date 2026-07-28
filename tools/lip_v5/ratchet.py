"""
lip_v5.ratchet — verified-accrual ratchet (spec §1.4, charter §3 "verified accrual before
size").  PYPL got $16 of exposure on ZERO verified accrual; this file is the answer.

The load-bearing observation, and the one a naive implementation gets wrong:

    **A probe smaller than `floor_q` MEASURES NOTHING** — it cannot pay (the $1.00 cliff and
    the $2.00 entry floor), so its non-payment is not evidence about the venue.  Therefore a
    naive `min(floor_q, 0.02 × ceiling)` is SELF-CONTRADICTING: it funds probes that are
    structurally unable to verify, then reads their silence as a DISAGREE and stands the venue
    down for a fact about our own sizing.  That expression must not be written, and the
    OUT_OF_REACH verdict below exists so it can never be reintroduced by accident.

The ladder's characteristic number: expected drift per reading with verifier accuracy `a` is
`a·(+1) + (1−a)·(−2) = 3a − 2`, so **a = 2/3**.  A verifier worse than 2/3 accurate can never
climb, and a coin-flip verifier drifts to rung 0 (T-R3, T-R7).
"""

import math

from . import config as C


VERIFY, DISAGREE, OUT_OF_REACH = "verify", "disagree", "out_of_reach"
ADMITTED, QUEUED, OVERSIZED, UNPROBEABLE, STOOD_DOWN = \
    "admitted", "queued", "oversized_probe", "unprobeable", "stand_down"


# =============================================================================================
# floor_q — the smallest allocation whose projected payout over the PROGRAM PERIOD clears
# ENTRY_FLOOR.
# =============================================================================================
def floor_q_contracts(rho, S, window_h, entry_floor=C.ENTRY_FLOOR_USD):
    """Smallest q (contracts) with `share(q)·(ρ/2)·window_h ≥ ENTRY_FLOOR`.

        q/(q+S) · A ≥ F    with A = (ρ/2)·window_h
        ⇒ q·(A − F) ≥ F·S
        ⇒ q ≥ F·S/(A − F)                                     provided A > F

    `A ≤ F` means the venue's ENTIRE side pool over the whole period is below the entry floor,
    so NO q clears it: returns None (∞).  That is not a rounding case — it is a venue that
    cannot pay us the minimum no matter how much capital we commit, and funding it is the
    PayPal error in miniature.

    MIRROR (probe too SMALL to verify ↔ probe too LARGE for an unverified venue): this
    function guards the first end; `UNVERIFIED_EXPOSURE_FRAC` / `N_UNVERIFIED_MAX` /
    `OVERSIZED_PROBE_MAX` guard the second.  Neither may override the other — which is exactly
    why the 20%/8/≤2 bounds REPLACE the per-venue 2% as the binding constraint rather than
    capping `floor_q` itself.
    """
    A = (float(rho) / 2.0) * float(window_h)
    F = float(entry_floor)
    if F <= 0:
        # BLOCKER-2 (rescue exemption): a floor already met (RESCUE_TARGET − A ≤ 0) needs
        # only the minimum tradeable presence — one contract — to keep earning.
        return 1
    if A <= F:
        return None
    S = float(S)
    if S <= 0:
        # Sole qualifier: share ≈ 1 for any q ≥ the qualification size, so the floor is
        # cleared by the qualification size itself (spec §4.5 — do NOT size up into an empty
        # book).  One contract is the arithmetic answer here; the qualification path, not the
        # ratchet, sets the real size.
        return 1
    return max(1, int(math.ceil(F * S / (A - F))))


def floor_q_usd(rho, S, p, window_h, entry_floor=C.ENTRY_FLOOR_USD):
    """`floor_q` in DOLLARS of collateral — the unit every §1.4 comparison is in
    (`cap_usd`, `0.02 × global_ceiling`, `0.20 × global_ceiling`)."""
    n = floor_q_contracts(rho, S, window_h, entry_floor)
    if n is None:
        return None
    return float(n) * float(p)


# =============================================================================================
# VENUE STATE
# =============================================================================================
class VenueState(object):
    """Per venue (= series, spec §1.1): the coarsest key at which enough settlements
    accumulate to verify anything.  Toxicity is measured per (market, side) and aggregated
    here by dollar-hour weight; RATCHET CAPS LIVE AT THE VENUE, KILL DECISIONS AT (m,s)."""

    __slots__ = ("venue", "rung", "rung0_cap_usd", "last_verify_ts", "verify_history",
                 "consec_disagree_days", "last_disagree_day", "stood_down", "out_of_reach",
                 "oversized", "verified", "last_period_id")

    def __init__(self, venue, rung=0, rung0_cap_usd=0.0):
        self.venue = venue
        self.rung = int(rung)
        self.rung0_cap_usd = float(rung0_cap_usd)
        self.last_verify_ts = None
        self.verify_history = []          # [(ts, verdict, ratio)]
        self.consec_disagree_days = 0
        self.last_disagree_day = None
        self.stood_down = False
        self.out_of_reach = False         # holds the rung, stops funding THIS period
        self.oversized = False
        self.verified = False             # has ever recorded a VERIFY
        self.last_period_id = None

    def cap_usd(self, per_market_cap_usd, global_ceiling_usd):
        """`rung k: cap_usd = min( 2^k × rung0_cap , per-market cap §8.2 , global ceiling )`.

        MIRROR (ratchet RAISES venue caps ↔ Σ venue caps vs the GLOBAL ceiling): Σ caps MAY
        exceed the ceiling; Σ *allocated* never does, because ALLOCATE's budget binds
        independently (T-R4b).  Caps are permissions, the budget is the money.
        """
        if self.stood_down:
            return 0.0
        return min((2.0 ** self.rung) * self.rung0_cap_usd,
                   float(per_market_cap_usd), float(global_ceiling_usd))

    def __repr__(self):
        return "Venue(%s rung=%d cap0=%.2f%s)" % (
            self.venue, self.rung, self.rung0_cap_usd,
            " STOOD_DOWN" if self.stood_down else "")


# =============================================================================================
# ADMISSION AT RUNG 0  (spec §1.4)
# =============================================================================================
def classify_probe(floor_usd, global_ceiling_usd, oversized_frac=C.OVERSIZED_PROBE_FRAC):
    """`if floor_q(v) > 0.02 × global_ceiling: the venue is OVERSIZED-PROBE` — still
    admissible, but it consumes an oversized-probe slot (≤2 concurrent).  The 2% figure is now
    a CLASSIFICATION threshold, not a cap (spec §9.3)."""
    if floor_usd is None:
        return True
    return float(floor_usd) > float(oversized_frac) * float(global_ceiling_usd)


def rung0_cap(floor_usd, inv_cap_usd, per_market_cap_usd):
    """`rung 0: cap_usd = min( floor_q(v) , INV_CAP_USD per slot , per-market cap §8.2 )`.

    RESIDUAL DECISION (RD-1, derived, surfaced in the report).  The spec writes the second
    term as "INV_CAP_USD/p per slot", which is a CONTRACT count while the other two terms are
    DOLLARS.  Taken literally the min is dimensionally incoherent.  Derivation of the reading
    used here: `n_cap = floor(INV_CAP_USD/p)` contracts is worth `n_cap × p ≈ INV_CAP_USD` of
    collateral, so the dollar-denominated form of that term IS `INV_CAP_USD`.  Both readings
    coincide at the cap; only the units differ.

    Returns (cap_usd, status).  If the binding minimum falls BELOW `floor_q`, the probe would
    measure nothing, and spec §1.4 says NEVER SHRINK THE PROBE BELOW floor_q — so the venue is
    UNPROBEABLE rather than funded at a size whose silence we would then misread as evidence.
    """
    if floor_usd is None:
        return 0.0, UNPROBEABLE
    other = min(float(inv_cap_usd), float(per_market_cap_usd))
    if other + 1e-9 < float(floor_usd):
        return 0.0, UNPROBEABLE
    return float(floor_usd), ADMITTED


def admit(venue_state, floor_usd, inv_cap_usd, per_market_cap_usd, global_ceiling_usd,
          unverified_exposure_usd, unverified_count, oversized_count,
          frac=C.UNVERIFIED_EXPOSURE_FRAC, n_max=C.N_UNVERIFIED_MAX,
          oversized_max=C.OVERSIZED_PROBE_MAX):
    """spec §1.4's rung-0 admission, verbatim:

        ADMIT the venue at rung 0 iff  Σ unverified exposure + cap_usd ≤ 0.20 × global_ceiling
                                  AND  count(unverified venues) < N_UNVERIFIED_MAX = 8
        ... consumes an oversized-probe slot (≤2 concurrent) if floor_q > 0.02 × ceiling
        if it cannot be admitted now: QUEUE it (ranked by net(0)); never shrink below floor_q

    Derivation of the 20% / 8 / ≤2 bounds: they REPLACE the per-venue 2% as the binding
    constraint, because the per-venue number cannot be allowed to override the floor-clearing
    size without destroying the verifier.  At a $300 ceiling that is ≤$60 unverified at once —
    under four PayPal incidents' worth, and each one MEASURABLE.  (UNDERIVED §9.3: scaled from
    the $16 loss as a magnitude, not from a distribution.  Recalibrate after 10 verified
    venues.)

    Returns (status, cap_usd, detail).
    """
    if venue_state.stood_down:
        return STOOD_DOWN, 0.0, {"reason": "venue_stood_down"}
    cap, status = rung0_cap(floor_usd, inv_cap_usd, per_market_cap_usd)
    if status == UNPROBEABLE:
        return UNPROBEABLE, 0.0, {"reason": "floor_q_unreachable_under_caps",
                                  "floor_usd": floor_usd}
    oversized = classify_probe(floor_usd, global_ceiling_usd)
    detail = {"floor_usd": floor_usd, "oversized": oversized,
              "unverified_exposure": unverified_exposure_usd,
              "unverified_count": unverified_count}
    if float(unverified_exposure_usd) + cap > frac * float(global_ceiling_usd) + 1e-9:
        detail["reason"] = "unverified_exposure_cap"
        return QUEUED, 0.0, detail
    if int(unverified_count) >= int(n_max):
        detail["reason"] = "unverified_count_cap"
        return QUEUED, 0.0, detail
    if oversized and int(oversized_count) >= int(oversized_max):
        detail["reason"] = "oversized_probe_slots_full"
        return QUEUED, 0.0, detail
    venue_state.rung0_cap_usd = cap
    venue_state.oversized = oversized
    return (OVERSIZED if oversized else ADMITTED), cap, detail


def exploration_floor_admits(unverified_exposure_usd, global_ceiling_usd, queue_len,
                             frac=C.EXPLORATION_FLOOR_FRAC):
    """spec §4.4 MIRROR (unverified-exposure CEILING ↔ exploration FLOOR).

    "If unverified exposure < 5% of ceiling while the §1.4 queue is non-empty, admit the next
    queued venue.  A cap on learning is a cap on earning" (Ryan's capital corollary:
    boundedness never answers "why is this dollar here instead of where it earns").
    """
    return int(queue_len) > 0 and \
        float(unverified_exposure_usd) < float(frac) * float(global_ceiling_usd)


def rank_queue(queued):
    """spec §1.4 "QUEUE it (ranked by net(0))".  `queued` = [(venue, net0), ...]; ties broken
    by venue name so the order is deterministic across restarts."""
    return [v for v, _ in sorted(queued, key=lambda kv: (-float(kv[1]), str(kv[0])))]


# =============================================================================================
# READINGS  (spec §1.4) — VERIFY (+1) / DISAGREE (−2) / OUT OF REACH (neither)
# =============================================================================================
def classify_reading(reading_usd, projection_usd, band=C.VERIFY_BAND,
                     entry_floor=C.ENTRY_FLOOR_USD):
    """A `popover_estimate` or paid credit for a program in this venue, against the model's
    projection over the SAME window.

        VERIFY   (+1): ratio ∈ [0.5, 2.0]
        DISAGREE (−2): ratio outside the band — **ONLY IF the projection was ≥ ENTRY_FLOOR**
        OUT OF REACH: a reading on a program whose projection was < ENTRY_FLOOR ⇒ NEITHER;
                      log `venue_out_of_reach`, hold the rung, stop funding that venue this
                      period

    OUT_OF_REACH is checked FIRST and unconditionally.  A venue must never be stood down by a
    probe that could not have paid (T-R6) — that is the self-contradiction the whole §1.4
    preamble exists to forbid.

    [0.5, 2.0] is the system's own declared model tolerance (v1 §3.1, §12.3a) — self-consistent
    by construction, UNDERIVED §9.4 as a measured distribution.
    """
    proj = float(projection_usd)
    if proj < float(entry_floor):
        return OUT_OF_REACH, None
    if proj <= 0:
        return OUT_OF_REACH, None
    ratio = float(reading_usd) / proj
    lo, hi = band
    return (VERIFY if (lo <= ratio <= hi) else DISAGREE), ratio


def apply_reading(state, reading_usd, projection_usd, ts=None, settlement_day=None,
                  band=C.VERIFY_BAND, entry_floor=C.ENTRY_FLOOR_USD,
                  up=C.RATCHET_UP, down=C.RATCHET_DOWN, standdown_days=C.STANDDOWN_DAYS):
    """Move the rung.  Returns (verdict, ratio, detail); `state` is mutated.

    UP-1 / DOWN-2, derived: a false UP-step costs CAPITAL at a venue that does not pay; a
    false DOWN-step costs only RATE at a venue re-verifiable tomorrow.

    STAND DOWN (venue, NOT bot): DISAGREE on 2 consecutive settlement days (charter §5).
    MIRROR (stand a venue down ↔ halt the book): §8.7's book-wide predicate is the only thing
    that may halt; a venue standing down must leave every other venue quoting (T-R5, T-D1).
    """
    verdict, ratio = classify_reading(reading_usd, projection_usd, band, entry_floor)
    detail = {"reading": reading_usd, "projection": projection_usd, "ratio": ratio,
              "rung_before": state.rung}
    state.verify_history.append((ts, verdict, ratio))
    if verdict == OUT_OF_REACH:
        state.out_of_reach = True                       # hold the rung, stop funding
        detail["rung_after"] = state.rung
        detail["reason"] = "projection_below_entry_floor"
        return verdict, ratio, detail
    if verdict == VERIFY:
        state.rung += int(up)
        state.verified = True
        state.last_verify_ts = ts
        state.consec_disagree_days = 0
        state.last_disagree_day = None
    else:
        state.rung = max(0, state.rung - int(down))     # floor at rung 0 (T-R2)
        # "DISAGREE on 2 consecutive settlement DAYS" — days, not readings.  Counting readings
        # would stand a venue down on two disagreements inside one afternoon, which is one
        # day's evidence wearing two hats; the day key is what makes them independent.
        if settlement_day is None:
            state.consec_disagree_days += 1
        elif settlement_day != state.last_disagree_day:
            prev = state.last_disagree_day
            state.consec_disagree_days = (
                state.consec_disagree_days + 1
                if prev is not None and _is_next_day(prev, settlement_day) else 1)
            state.last_disagree_day = settlement_day
        if state.consec_disagree_days >= int(standdown_days):
            state.stood_down = True
            detail["stand_down"] = True
    detail["rung_after"] = state.rung
    return verdict, ratio, detail


def _is_next_day(prev_day, day):
    """Consecutive in the settlement-day sequence.  Integer day keys compare arithmetically;
    anything else falls back to "different day", which is the conservative reading (it can
    only make a stand-down EASIER, and standing a venue down costs rate, not capital)."""
    try:
        return int(day) - int(prev_day) == 1
    except (TypeError, ValueError):
        return True


def expected_rung_drift(accuracy, up=C.RATCHET_UP, down=C.RATCHET_DOWN):
    """spec §1.4's characteristic number: `a·(+1) + (1−a)·(−2) = 3a − 2`.

    a = 2/3 ⇒ ZERO expected drift; a = 0.60 drifts down; a = 0.70 drifts up (T-R7).  This is
    the ladder's SENSOR-QUALITY REQUIREMENT: a verifier worse than 2/3 accurate can never
    climb, no matter how good the venue is.
    """
    a = float(accuracy)
    return a * float(up) + (1.0 - a) * (-float(down))


def breakeven_accuracy(up=C.RATCHET_UP, down=C.RATCHET_DOWN):
    """`a* = down/(up+down)` = 2/3 for (+1, −2)."""
    return float(down) / (float(up) + float(down))


# =============================================================================================
# REVIVE  (spec §1.4 MIRROR: ratchet up ↔ ratchet down ↔ revive)
# =============================================================================================
def t_hat_upper_95(prox_dollar_s, committed_dollar_h, k=C.SHRINK_PSEUDO_DOLLAR_H):
    """95% upper bound on the venue's own T̂ posterior.

    T̂ is a bounded mean of a proportion over `n` effective dollar-hour samples, so a Wilson-
    style normal bound is the cheapest defensible form: `p̂ + 1.96·sqrt(p̂(1−p̂)/n)`, clipped to
    [0,1], with `n` = committed dollar-hours (the exposure that produced the estimate) and the
    shrinkage pseudo-weight added so a venue with no history cannot revive on an empty
    posterior.

    MIRROR (reviving too EAGERLY ↔ never reviving): the UPPER bound is the eager end's guard
    — it revives on the OPTIMISTIC reading, so a venue is only refused when even optimism
    cannot clear the hurdle; the new-period requirement is the guard on the other end, since
    without it a venue would re-probe on a timer, which is what "nothing revives on a timer"
    forbids.
    """
    n = max(0.0, float(committed_dollar_h)) + float(k)
    if n <= 0:
        return 1.0
    p_hat = min(1.0, max(0.0, float(prox_dollar_s) / (n * C.PSDH_MAX)))
    se = math.sqrt(max(0.0, p_hat * (1.0 - p_hat) / n))
    return min(1.0, max(0.0, p_hat + 1.96 * se))


def revive_allowed(state, period_id, t_hat_ub, hurdle_t_hat):
    """spec §1.4 MIRROR: a killed (m,s) or stood-down venue re-probes ONLY at a NEW program
    period AND only if the 95% upper bound on its own T̂ posterior clears the hurdle.
    MEMORY IS RETAINED ACROSS PERIODS; NOTHING REVIVES ON A TIMER.
    """
    if state.last_period_id is not None and period_id == state.last_period_id:
        return False, "same_period"
    if float(t_hat_ub) < float(hurdle_t_hat):
        return False, "t_hat_ub_below_hurdle"
    return True, "new_period_and_posterior_clears"


def t_hat_hurdle(rho, S, p, phi, d, l_eff, r_star, floor_rate=C.FLOOR_RATE_PER_H):
    """The T̂ a venue would need for (★) to clear the water level at q = 0:

        T̂·gross − carry − drift > floor   ⇒   T̂ > (floor + carry + drift) / gross

    Returns >1.0 (unreachable) when no T̂ can save the venue — which is the correct answer for
    a long-dated market whose carry alone exceeds its entire gross rate.
    """
    from . import money as M
    g = M.gross_rate(rho, S, p, 0.0)
    if g <= 0:
        return float("inf")
    dd = min(float(d), float(p))
    return (float(floor_rate) + M.carry_cost(phi, l_eff, r_star) +
            M.drift_cost(phi, dd, p)) / g
