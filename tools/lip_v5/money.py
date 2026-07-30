"""
lip_v5.money — (★), spec §0.3.  THE WHOLE SPEC.

    gross(q)   = ρ·S / (2·p·(q+S)²)              $/h per collateral-$   [v1 §0.4, kept]
    carry_cost = φ · L_eff · r*                  $/h per collateral-$   [NEW — the $16 lesson]
    drift_cost = φ · d / p                       $/h per collateral-$   [v1 §2.2 hurdle, kept]
    net(q)     = T̂ · gross(q)  −  carry_cost  −  drift_cost                            (★)

ALLOCATE admits a slot iff `net(q) > λ_min/16`.  There is NO separate hurdle comparison —
v1 §2.2's hurdle is now INSIDE (★).

Everything in this file is a PURE FUNCTION of numbers.  No I/O, no clock, no network.  That
is what makes spec §8's test plan possible at all.
"""

import hashlib
import math

from . import config as C


# =============================================================================================
# THE LIQUIDITY HORIZON  (spec §1.2) — ALL QUANTITIES IN HOURS.  The type error matters.
# =============================================================================================
def t_settle_h(close_ts, now, settle_lag_h=C.SETTLE_LAG_H):
    """Hours until this market's cash is certainly liquid via settlement.

    BLOCKER-grade FLOOR: without it, (close_ts − now) turns negative after close, carry_cost
    turns negative, and the model ADMITS a venue *on the strength of being stuck* — the
    PayPal failure with the sign flipped.  Nothing is ever liquid faster than the settlement
    lag, so SETTLE_LAG_H is the floor.
    """
    return max(float(settle_lag_h), (float(close_ts) - float(now)) / 3600.0 + float(settle_lag_h))


def l_eff_h(close_ts, now, l_shed_h=None, settled=False, settle_lag_h=C.SETTLE_LAG_H,
            past_due_mult=C.PAST_DUE_ESCALATION):
    """spec §1.2.

        L_eff = max( SETTLE_LAG_H , min( T_settle , L_shed ) )
        PAST DUE (now > close_ts + SETTLE_LAG_H and no settlement observed):
            L_eff ← max( L_eff , 2 × hours_past_due )     # monotone, never shrinks

    `l_shed_h is None` ⇒ ∞ (unmeasured), which is the ONLY default consistent with charter
    requirement 1: "no cap may assume settlement bails it out."  A shed we have never
    completed is not a liquidity route we may price.

    Past due, the remaining wait carries no information; the no-information bound is that
    expected remaining wait grows at least linearly in elapsed overdue time, so the escalation
    is monotone and a stuck market is progressively EXCLUDED, never progressively favored.

    MIRROR (escalates too fast ↔ too slow): too fast only refuses a venue we could have held,
    a rate loss; too slow re-creates the PayPal admission, a capital loss.  The asymmetry is
    why the coefficient errs high and is flagged UNDERIVED (§9.4) rather than tuned down.
    """
    ts = t_settle_h(close_ts, now, settle_lag_h)
    shed = float("inf") if l_shed_h is None else max(0.0, float(l_shed_h))
    eff = max(float(settle_lag_h), min(ts, shed))
    if not settled and float(now) > float(close_ts) + float(settle_lag_h):
        hours_past_due = (float(now) - float(close_ts)) / 3600.0
        eff = max(eff, float(past_due_mult) * hours_past_due)
    return eff


def l_shed_median_h(completed_sheds_h, trailing=C.L_SHED_TRAILING):
    """Median hours open→flat via maker shed over the trailing N completed sheds.
    Fewer than one completed shed ⇒ None (unmeasured ⇒ ∞ in `l_eff_h`)."""
    vals = sorted(float(x) for x in (completed_sheds_h or [])[-int(trailing):])
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])


def horizon_excluded(close_ts, now, program_end_ts, rung=0, grace_h=C.HORIZON_GRACE_H,
                     exempt_rung=C.HORIZON_EXEMPT_RUNG, settle_lag_h=C.SETTLE_LAG_H):
    """spec §1.2 hard horizon exclusion — BOTH SIDES IN HOURS.

    Exclude iff `T_settle > H_prog + 24` unless ratchet rung ≥ 2, where
    `H_prog = (program_end_ts − now)/3600`.  Past program end, inventory carries with ZERO
    offsetting accrual — the exact PYPL geometry.
    """
    if int(rung) >= int(exempt_rung):
        return False
    h_prog = (float(program_end_ts) - float(now)) / 3600.0
    return t_settle_h(close_ts, now, settle_lag_h) > h_prog + float(grace_h)


# =============================================================================================
# φ AND d FROM OUR OWN TAPE  (spec §2.4)
# =============================================================================================
def phi_estimate(fills_ct, rest_contract_hours, p=None, rule_of_three=C.RULE_OF_THREE):
    """φ = fills/hour/resting-contract.

    ZERO-FILL venues use the RULE OF THREE: the 95% upper bound on a Poisson rate observed
    zero times over exposure E is 3/E.  This is a closed form, not a guess, which is exactly
    why it REPLACES v1's seeded PHI_MID/PHI_CHEAP: it is correct on day one and tightens with
    evidence.  The seeds survive only as the CEILING on φ_ub at zero exposure — with no tape
    at all there is nothing to bound, and an unbounded φ_ub would refuse every venue forever.

    MIRROR (φ estimated too LOW ↔ too HIGH): too low under-charges fill cost and over-funds a
    toxic venue (PayPal); too high refuses a good one.  The Rule of Three is deliberately an
    UPPER bound, i.e. it errs toward refusing — the direction that fails toward the lesson.
    """
    e = max(0.0, float(rest_contract_hours))
    n = max(0, int(fills_ct))
    if e <= 0.0:
        return seed_phi(p)
    if n == 0:
        ub = float(rule_of_three) / e
        seed = seed_phi(p)
        return min(ub, seed) if seed is not None else ub
    return float(n) / e


def seed_phi(p):
    """The zero-exposure ceiling on φ_ub (spec §2.4).  ONE seed, no price step.

    ── 2026-07-29: THE PRICE STEP WAS A BOOTSTRAP DEADLOCK.  THE MEASUREMENT. ────────────────
    v1 §2.2 stepped this seed by price: `PHI_SEED_CHEAP = 0.001` below 5c, `PHI_SEED_MID = 0.08`
    at or above.  An 80x step at a cutoff, and the docstring above `phi_estimate` is explicit
    that a seed is only ever the CEILING on the Rule-of-Three bound, "never the estimate".  But
    the ceiling decided everything, because the estimator can only run on exposure the gate has
    to permit first:

        φ_ub = min(3/E, seed).  At p = 12c the seed is 0.08, so E must exceed 37.5
        contract-hours for 3/E to bind at all — and the (★) gate REFUSES at φ = 0.08, so no
        order ever rests, so E stays at 0, so the seed stays at 0.08.  FOREVER.

    Measured, holding everything else fixed (pool $100, rival score 1,200, q = 100): φ = 0.001
    admits every price from 1c to 40c; φ = 0.08 admits 1c ONLY.  So the step was not a prior on
    one venue, it was a GLOBAL ON/OFF SWITCH, and the 5c cut was the router that decided which
    side of it each venue landed on.  THAT is the mechanism behind "98.4% of contracts placed at
    <=5c" — admission, never sizing — and the cohort it admitted is the one note 47 §3 measured
    at −94.8% (1c, n=3,205) and −100% (2c, 0 of 765 markets ever paid).  A gate whose binding
    input selects, by construction, the only prices whose realised EV is measured to be total
    loss, is not a gate; it is the loss with a derivation attached.

    WHY ONE SEED IS NOT "PICKING THE NUMBER THAT MAKES IT WORK".  The choice is not between two
    fill-rate beliefs; it is between letting the DERIVED estimator run and letting an UNDERIVED
    constant pre-empt it.  The Rule of Three is a closed form, it is an UPPER bound (so it errs
    toward refusing — the direction that fails toward the lesson), and it tightens with every
    contract-hour: at E = 100 it is already 0.030.  One venue-hour of our own tape replaces this
    number entirely.  Keeping a step that prevents that hour from ever existing preserves a guess
    at the cost of the measurement, which is backwards.
    AND THE PRICE QUESTION IS NOW ANSWERED WHERE IT HAS EVIDENCE: `config.ENTRY_BAND_LO_C..HI_C`
    is derived from measured BIAS (n = 8,240) and measured COMPETITION, not from a fill-rate
    proxy.  The step was price discipline wearing a hazard estimate's name; the band is price
    discipline with the measurement behind it.  Two mechanisms for one job, and this was the
    guessed one.
    MIRROR (seed too LOW ↔ too high): too low under-charges fill cost on a venue we have never
    rested in — bounded to the FIRST observation, because the Rule of Three overrides it the
    moment exposure exists, and bounded in dollars by the per-leg cap, the cluster cap and the
    ceiling.  Too high is the deadlock above: the gate refuses the venue AND destroys the
    evidence that would have cleared it, which is unbounded in time.
    """
    return C.PHI_SEED_CHEAP


def d_estimate(drift_samples, p, seed=C.D_SEED_USD, trailing=C.D_TRAILING_FILLS):
    """`d` = mean of (mark_at_fill+Δ − fill_price) over the trailing 20 fills at Δ = 60 s.
    Unmeasured ⇒ the $0.07 seed.

    **`d` is CAPPED AT `p` in both cases** (v1 §2.2): the maximum adverse move against a
    position entered at price p is p itself — you cannot lose more than the contract cost.
    That cap is what makes spec §0.4's gas row reproduce (at p=$0.02, d/p = 1.0).
    """
    vals = list(drift_samples or [])[-int(trailing):]
    raw = (sum(float(x) for x in vals) / len(vals)) if vals else float(seed)
    return min(float(raw), float(p)) if p is not None else float(raw)


def is_decisive(fills_ct, committed_dollar_hours,
                min_fills=C.DECISIVE_FILLS, min_committed_h=C.DECISIVE_COMMITTED_H):
    """spec §2.4 — an estimate is decisive when `fills_ct ≥ 10` (rel. s.d. 1/√10 = 32%,
    resolves a 2× difference at ~2σ) OR `Σ committed_h ≥ 2` with ZERO fills (the Rule-of-Three
    bound is already below the hurdle there).

    MIRROR (acting on too little data ↔ never acting): this predicate guards the first end;
    §2.5's 3-consecutive-eval hysteresis guards it again in time.  The second end is guarded
    by the fact that KILL is the only decision requiring decisiveness — ALLOCATE keeps
    reallocating on the estimate as it stands, so an indecisive venue is sized down by the
    water level long before it is killed.
    """
    if int(fills_ct) >= int(min_fills):
        return True
    return int(fills_ct) == 0 and float(committed_dollar_hours) >= float(min_committed_h)


# =============================================================================================
# (★) ITSELF  (spec §0.3)
# =============================================================================================
def gross_rate(rho, S, p, q=0.0):
    """ρ·S / (2·p·(q+S)²)  — $/h per collateral-$.  v1 §0.4, kept.

    T-N5 (v1's B4 defect must not return): finite at q = 0.  The MARGINAL form is
    size-independent in its numerator and its denominator is (q+S)², which is > 0 whenever
    S > 0, so q = 0 is an ordinary point — it is the AVERAGE form that divides by zero and it
    must never be written.
    """
    p = float(p)
    S = float(S)
    if p <= 0.0:
        return 0.0
    denom = float(q) + S
    if denom <= 0.0:
        # spec §4.5 / N1: at S = 0 our marginal rate is exactly zero — we already own 100% of
        # the side.  ALLOCATE is RIGHT about size and WRONG about entry, which is why
        # qualification is a discrete precondition handled OUTSIDE the water-filling loop.
        return 0.0
    return float(rho) * S / (2.0 * p * denom * denom)


def carry_cost(phi, l_eff_hours, r_star):
    """φ · L_eff · r*  — $/h per collateral-$.  THE $16 LESSON, the term v4 did not have.

    A fill converts collateral into inventory; that inventory is illiquid for L_eff hours; and
    the portfolio's own achieved marginal rate r* is what those dollars would have earned
    elsewhere.  So the cost of a fill is the OPPORTUNITY COST of the capital it strands.

    Asserted non-negative by construction of `l_eff_h`'s floor.  A NEGATIVE carry must be
    unreachable (T-N6) — it would ADMIT a venue for being stuck.
    """
    return max(0.0, float(phi)) * max(0.0, float(l_eff_hours)) * max(0.0, float(r_star))


def drift_cost(phi, d, p):
    """φ · d / p  — $/h per collateral-$.  v1 §2.2's hurdle, kept, now a COST TERM inside (★).

    MARGINAL, not average: fill rate is linear in resting size (f(q) = φq), so marginal fill
    cost is SIZE-INDEPENDENT — computable at q = 0, no division by zero, no empty allocation.
    """
    p = float(p)
    if p <= 0.0:
        return float("inf")
    return max(0.0, float(phi)) * max(0.0, float(d)) / p


def net_rate(rho, S, p, q=0.0, phi=0.0, d=0.0, l_eff=C.SETTLE_LAG_H, r_star=C.FLOOR_RATE_PER_H,
             t_hat=1.0, cap_d_at_p=True):
    """(★).  `net = T̂·gross(q) − carry − drift`, $/h per collateral-$.

    T-N1..N3 reproduce spec §0.4's three rows to 1e-3 WITH `d` capped at `p`.
    """
    dd = min(float(d), float(p)) if (cap_d_at_p and p is not None) else float(d)
    g = gross_rate(rho, S, p, q)
    c = carry_cost(phi, l_eff, r_star)
    dr = drift_cost(phi, dd, p)
    return _clip01(t_hat) * g - c - dr


def net_terms(rho, S, p, q=0.0, phi=0.0, d=0.0, l_eff=C.SETTLE_LAG_H,
              r_star=C.FLOOR_RATE_PER_H, t_hat=1.0, cap_d_at_p=True):
    """Every term of (★), for the `venue_rank` / `allocate` log lines and for the tests.
    `H` is spec §0.5's DISPLAY quantity only — see DIVERGENCE D1 below."""
    dd = min(float(d), float(p)) if (cap_d_at_p and p is not None) else float(d)
    g = gross_rate(rho, S, p, q)
    th = _clip01(t_hat)
    c = carry_cost(phi, l_eff, r_star)
    dr = drift_cost(phi, dd, p)
    return {"gross": g, "t_hat": th, "carry": c, "drift": dr, "d_used": dd,
            "net": th * g - c - dr, "H": horizon_multiplier_display(th * g, c)}


def horizon_multiplier_display(t_hat_gross, carry):
    """spec §0.5, DIVERGENCE D1 (surfaced, not silently resolved).

    The charter writes horizon and toxicity as MULTIPLIERS; only toxicity is one.  Carry is a
    COST that can EXCEED gross (PYPL: 800×), and a multiplier cannot represent that without
    clipping to zero and losing the ranking — every hopeless venue would tie at H = 0 and the
    allocator could not tell an 8× refusal from an 800× one.  ADDITIVE IS CANONICAL.  `H` is
    computed here for display and for T-N1's "H clips to 0" assertion, and is never used to
    rank or to size.
    """
    tg = float(t_hat_gross)
    if tg <= 0.0:
        return 0.0
    return max(0.0, 1.0 - float(carry) / tg)


def admits(net, floor_rate=C.ADMIT_FLOOR_RATE_PER_H):
    """spec §0.3: ALLOCATE admits a slot iff `net(q) > λ_min/16`.  Strict, per the spec's
    own wording; the boundary case is worth exactly the floor and therefore worth nothing
    over the alternative use of the dollar."""
    return float(net) > float(floor_rate)


def _clip01(x):
    return min(1.0, max(0.0, float(x)))


# =============================================================================================
# THE r* FIXED POINT IS DELETED (owner's law, 2026-07-30).  The law's ranking is capital
# needed for a fixed dollar target, not a marginal rate, so there is no circular quantity
# left to solve for.  `solve_rstar`/`rstar_seed`/`RStarResult` died with the water-filling
# core; cutover's triage prices opportunity cost at the reference floor directly.
# =============================================================================================


# =============================================================================================
# PER-VENUE SHADING  (spec §4.3) — DERIVED, no constant.
# =============================================================================================
def shade_decision(rho, q, S, p, d, l_eff, r_star, phi0, phi1=None,
                   trade_ge_depth_prob=None, max_ticks=C.MAX_SHADE_TICKS):
    """Shade to k = 1 tick behind best iff the halved score is worth less than the avoided
    adverse selection AND carry (spec §4.3):

        (ρ/2)·[0.5q/(0.5q+S)] − φ₁·q·(d + p·L_eff·r*)
            >  (ρ/2)·[q/(q+S)] − φ₀·q·(d + p·L_eff·r*)

    Score at k=1 is exactly DF = 0.5 of score at k=0 (spec §0.2), so our normalized share
    becomes 0.5q/(0.5q+S).

    Seed for unmeasured φ₁: `φ₁ = φ₀ × P(trade size ≥ depth at best)` from the public trade
    tape joined to our own book snapshots — A MEASUREMENT, NOT A GUESS.  With neither φ₁ nor
    that probability available we return k = 0: refusing to shade on no evidence keeps us at
    best, which is the objective's own preferred state.

    NEVER returns k ≥ 2 (spec §4.3): score ≤ 25% and those dollars beat it at the water level
    in another venue.

    MIRROR (shading too EAGERLY ↔ never shading): eager shading halves score for nothing and
    is caught by the inequality's left-hand side going negative; never shading is caught by
    the same inequality with φ₁ measured — one comparison guards both directions, which is why
    this is derived rather than a constant.  Both sides are logged per slot per cycle.
    """
    q = float(q)
    if q <= 0 or float(p) <= 0:
        return 0, None
    if phi1 is None:
        if trade_ge_depth_prob is None:
            return 0, None
        phi1 = float(phi0) * _clip01(trade_ge_depth_prob)
    cost_per_fill = float(d) + float(p) * float(l_eff) * float(r_star)
    at_best = (float(rho) / 2.0) * (q / (q + float(S))) - float(phi0) * q * cost_per_fill
    behind = (float(rho) / 2.0) * (0.5 * q / (0.5 * q + float(S))) - float(phi1) * q * cost_per_fill
    detail = {"k0": at_best, "k1": behind, "phi0": float(phi0), "phi1": float(phi1),
              "cost_per_fill": cost_per_fill}
    k = 1 if behind > at_best else 0
    return min(k, int(max_ticks)), detail


# =============================================================================================
# DOSE-RESPONSE SIZING  (spec §1.5)
# =============================================================================================
def dose_multiplier(ticker, side, period_id, multipliers=C.DOSE_MULTIPLIERS):
    """Deterministic by `hash(ticker, side, period)` so the panel is STABLE within a period —
    a panel that reshuffles every cycle measures noise, not the share curve.  blake2b rather
    than Python's `hash()` because the latter is salted per process and would silently
    reshuffle the panel on every restart."""
    h = hashlib.blake2b(("%s|%s|%s" % (ticker, side, period_id)).encode(), digest_size=8)
    return multipliers[int.from_bytes(h.digest(), "big") % len(multipliers)]


def dose_panel(slot_keys, period_id, min_slots=C.DOSE_MIN_SLOTS,
               multipliers=C.DOSE_MULTIPLIERS):
    """{key: multiplier} over at least `min_slots` slots, or {} if the book is too small to
    carry a panel (perturbing a 2-slot book measures nothing and costs rate)."""
    keys = sorted(slot_keys)
    if len(keys) < int(min_slots):
        return {}
    return {k: dose_multiplier(k[0], k[1], period_id, multipliers) for k in keys}


def dose_budget_ok(rate_given_up, portfolio_rate, frac=C.DOSE_RATE_BUDGET_FRAC):
    """spec §1.5 — the total modeled rate given up must be < 2% of the modeled portfolio rate.
    DERIVED, not chosen: ALLOCATE's own step is 2% of budget, so the allocation is only
    accurate to 2% anyway and information bought inside that resolution is free.  Slots on
    flat rate curves therefore get large perturbations and steep ones get none, AUTOMATICALLY.
    """
    if float(portfolio_rate) <= 0:
        return float(rate_given_up) <= 0
    return float(rate_given_up) < float(frac) * float(portfolio_rate)
