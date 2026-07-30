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


def _is_next_day(prev_day, day):
    """Consecutive in the settlement-day sequence.  Integer day keys compare arithmetically;
    anything else falls back to "different day", which is the conservative reading (it can
    only make a stand-down EASIER, and standing a venue down costs rate, not capital)."""
    try:
        return int(day) - int(prev_day) == 1
    except (TypeError, ValueError):
        return True


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
