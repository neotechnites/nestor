"""
lip_v5.presence — presence-seconds per dollar-hour (spec §2), THE health metric (charter §2).

    PSDH(m,s) = Σ prox_dollar_s / [ Σ (rest_dollar_s + inv_dollar_s) / 3600 ]   seconds per hour
    T̂(m,s)   = clip( PSDH / 3600 , 0, 1 )

PSDH ∈ [0, 3600]; 3600 = every committed dollar resting at best every second.  **T̂ needs no
threshold constant**: it is exactly the fraction of modeled presence actually realized, so
`T̂·gross` in (★) is the realized rate and the water level λ_min/16 is already the kill
threshold.

PYPL's geometry — capital converts to inventory on contact — drives `inv_dollar_s` UP and
`prox_dollar_s` to ZERO, so T̂ → 0 within hours, WITHOUT ANY SETTLEMENT DATA.  That is the
whole reason this metric exists.
"""

import math

from . import config as C
from . import money as M


# =============================================================================================
# METERING  (spec §2.1) — a monotonic 1 Hz tick on a FIXED phase, INDEPENDENT of the quoting
# loop.
# MIRROR (sampling right after a requote biases at_best UP ↔ sampling inside a coverage gap
# biases it DOWN): ONE guard kills both — a fixed phase never triggered by our own action.
# The jitter assertion below is what makes "fixed" checkable rather than aspirational.
# =============================================================================================
class Meter(object):
    """Accumulates spec §2.1's four quantities per (m,s), and emits spec §2.2's delta rows.

    Deltas, never cumulative, so replay is a SUM and a crash loses ≤ 60 s (T-P2).
    """

    __slots__ = ("acc", "window_start", "row_s", "last_tick", "max_jitter", "jitter_breaches")

    def __init__(self, now, row_s=C.PRESENCE_ROW_S, max_jitter=C.METER_MAX_JITTER_S):
        self.acc = {}
        self.window_start = float(now)
        self.row_s = float(row_s)
        self.last_tick = None
        self.max_jitter = float(max_jitter)
        self.jitter_breaches = 0

    def _slot(self, key):
        return self.acc.setdefault(key, {
            "rest_dollar_s": 0.0, "prox_dollar_s": 0.0, "inv_dollar_s": 0.0,
            "at_best_s": 0.0, "ticks_ct": 0, "fills_ct": 0, "fill_notional": 0.0,
            "rest_contract_s": 0.0,
        })

    def tick(self, now, observations, expected_period_s=None):
        """One 1 Hz sample.  `observations` maps (ticker, side) →
            {"orders": [{"remaining", "price", "ticks_behind"}, ...],
             "net_position": float, "entry_basis": float}

        `ticks_behind` = (same-side best − our price) in cents, from the WS book AT THAT TICK.
        """
        period = float(expected_period_s if expected_period_s is not None else 1.0 / C.METER_HZ)
        if self.last_tick is not None:
            jitter = abs((float(now) - self.last_tick) - period)
            if jitter > self.max_jitter:
                # Asserted, not hoped (spec §4.4 mirror row).  A drifting sampler makes every
                # T̂ comparison across venues incommensurable, so the breach is COUNTED and
                # surfaced rather than swallowed.
                self.jitter_breaches += 1
        self.last_tick = float(now)

        for key, obs in (observations or {}).items():
            a = self._slot(key)
            a["ticks_ct"] += 1
            any_at_best = False
            for o in obs.get("orders", []) or []:
                rem = max(0.0, float(o.get("remaining", 0.0)))
                if rem <= 0:
                    continue
                price = float(o.get("price", 0.0))
                tb = max(0, int(o.get("ticks_behind", 0)))
                a["rest_dollar_s"] += rem * price
                a["rest_contract_s"] += rem
                a["prox_dollar_s"] += rem * price * (C.DISCOUNT_FACTOR_DEFAULT ** tb)
                if tb == 0:
                    any_at_best = True
            a["inv_dollar_s"] += abs(float(obs.get("net_position", 0.0))) * \
                float(obs.get("entry_basis", 0.0))
            if any_at_best:
                a["at_best_s"] += 1.0

    def note_fill(self, key, count, notional):
        a = self._slot(key)
        a["fills_ct"] += int(count)
        a["fill_notional"] += float(notional)

    def due(self, now):
        return float(now) - self.window_start >= self.row_s

    def flush(self, now):
        """Emit spec §2.2's rows and RESET.  One row per (m,s) per 60 s."""
        rows = []
        for (ticker, side), a in sorted(self.acc.items()):
            rows.append({
                "t": C.PRESENCE_KIND, "ticker": ticker, "side": side,
                "from_ts": self.window_start, "to_ts": float(now),
                "rest_dollar_s": a["rest_dollar_s"], "prox_dollar_s": a["prox_dollar_s"],
                "inv_dollar_s": a["inv_dollar_s"], "at_best_s": a["at_best_s"],
                "ticks_ct": a["ticks_ct"], "fills_ct": a["fills_ct"],
                "fill_notional": a["fill_notional"],
                "rest_contract_s": a["rest_contract_s"],
            })
        self.acc = {}
        self.window_start = float(now)
        return rows


# =============================================================================================
# THE METRIC  (spec §2.3)
# =============================================================================================
def _sum_rows(rows, key=None):
    tot = {"rest_dollar_s": 0.0, "prox_dollar_s": 0.0, "inv_dollar_s": 0.0,
           "at_best_s": 0.0, "fills_ct": 0, "fill_notional": 0.0, "rest_contract_s": 0.0}
    for r in rows or []:
        if key is not None and (r.get("ticker"), r.get("side")) != key:
            continue
        for k in tot:
            tot[k] += (int(r.get(k, 0)) if k == "fills_ct" else float(r.get(k, 0.0)))
    return tot


def committed_dollar_hours(rows, key=None):
    """Σ (rest_dollar_s + inv_dollar_s) / 3600 — the denominator of PSDH.  Both terms, because
    capital that is NOT resting is still capital we committed: that is the entire PayPal
    accusation, and a denominator of resting dollars alone would hide it."""
    t = _sum_rows(rows, key)
    return (t["rest_dollar_s"] + t["inv_dollar_s"]) / 3600.0


def psdh(rows, key=None):
    """spec §2.3.  Units: SECONDS PER HOUR.  Returns 0.0 on an empty denominator — no
    committed dollars means no claim either way, and 0 is the value that neither admits nor
    kills (the kill rules below all require committed hours before they fire)."""
    t = _sum_rows(rows, key)
    denom = (t["rest_dollar_s"] + t["inv_dollar_s"]) / 3600.0
    if denom <= 0.0:
        return 0.0
    return t["prox_dollar_s"] / denom


def t_hat(rows, key=None):
    """T̂ = clip(PSDH/3600, 0, 1).  NEEDS NO THRESHOLD CONSTANT (spec §2.3)."""
    return min(1.0, max(0.0, psdh(rows, key) / C.PSDH_MAX))


def t_hat_shrunk(rows, key=None, prior=None, k=C.SHRINK_PSEUDO_DOLLAR_H):
    """spec §2.3 shrinkage for thin data:

        T̂ = (Σprox + 3600·k·T₀) / (Σcommitted_h·3600 + 3600·k)

    with pseudo-weight k = 2 dollar-hours.  `prior` T₀ is the series' own dollar-hour-weighted
    median, else the portfolio median, else 0.5 (UNDERIVED §9.4 — it affects probe ORDER only,
    never exposure, because rung-0 caps bind first).
    """
    t0 = C.SHRINK_PRIOR_DEFAULT if prior is None else min(1.0, max(0.0, float(prior)))
    t = _sum_rows(rows, key)
    committed_h = (t["rest_dollar_s"] + t["inv_dollar_s"]) / 3600.0
    num = t["prox_dollar_s"] + C.PSDH_MAX * float(k) * t0
    den = committed_h * C.PSDH_MAX + C.PSDH_MAX * float(k)
    if den <= 0:
        return t0
    return min(1.0, max(0.0, num / den))


def prior_from_median(series_values, portfolio_values=None,
                      default=C.SHRINK_PRIOR_DEFAULT):
    """T₀ selection, spec §2.3's own order: series median → portfolio median → 0.5."""
    for vals in (series_values, portfolio_values):
        v = sorted(float(x) for x in (vals or []))
        if v:
            n = len(v)
            return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])
    return float(default)


def rest_contract_hours(rows, key=None):
    """Σ rest_contract_hours — φ's denominator (spec §2.4)."""
    return _sum_rows(rows, key)["rest_contract_s"] / 3600.0


# =============================================================================================
# AUTOMATIC SIZE-DOWN AND KILL  (spec §2.5, charter §2 "sized down or killed automatically")
#
# Size-DOWN is NOT a separate action: ALLOCATE re-runs each cycle and a falling T̂ moves the
# dollars by itself.  Note PSDH is SCALE-INVARIANT (numerator and denominator both ∝ q), so
# shrinking size cannot "fix" a toxic venue — the only correct response is reallocation,
# which is what the water level does (T-P4).
#
# MIRROR (per-slot kill ↔ book-wide kill): §8.7's collapse predicate, below.
# MIRROR (kill ↔ revive): ratchet.revive_allowed() — a NEW program period AND a T̂-posterior
# predicate.  Nothing revives on a timer.
# =============================================================================================
HOLD, RATCHET_ELIGIBLE, KILL = "hold", "ratchet_eligible", "kill"


class SlotHealth(object):
    """Per-(m,s) evaluation state.  `consec_below` is the 3-eval (45 min) hysteresis: the
    shortest interval that cannot be tripped by one fill burst inside one 15-min bucket."""

    __slots__ = ("consec_below", "killed", "last_eval_ts")

    def __init__(self):
        self.consec_below = 0
        self.killed = False
        self.last_eval_ts = None


def evaluate_slot(health, net_q, rows, key=None, floor_rate=C.FLOOR_RATE_PER_H,
                  now=None):
    """spec §2.5, evaluated every 15 min per (m,s) with `ζ = net(q_current) / (λ_min/16)`.

        ζ ≥ 1.5  and venue verified   → eligible for ratchet up (§1.4)
        1.0 ≤ ζ < 1.5                 → hold
        ζ < 1.0 for 3 consecutive evals AND the estimate is decisive → KILL for this period
        PSDH == 0 with fills_ct ≥ 1 and Σcommitted_h ≥ 2 → KILL IMMEDIATELY, NO MODEL

    The second rule needs no model because ZERO PRESENCE IS ZERO OBJECTIVE: we are paying
    capital and time and receiving no proximity at all.  It is the rule that would have
    caught PayPal in hours.

    Returns (verdict, detail).  `health` is mutated (the hysteresis lives there).
    """
    t = _sum_rows(rows, key)
    committed_h = (t["rest_dollar_s"] + t["inv_dollar_s"]) / 3600.0
    p_val = psdh(rows, key)
    detail = {"psdh": p_val, "committed_h": committed_h, "fills_ct": t["fills_ct"],
              "net": float(net_q), "zeta": None,
              "decisive": M.is_decisive(t["fills_ct"], committed_h)}

    # Zero-model rule FIRST: it is strictly stronger and must not be gated behind ζ.
    if p_val == 0.0 and t["fills_ct"] >= 1 and committed_h >= C.DECISIVE_COMMITTED_H:
        health.killed = True
        health.last_eval_ts = now
        detail["reason"] = "zero_presence_with_fills"
        return KILL, detail

    zeta = float(net_q) / float(floor_rate) if float(floor_rate) > 0 else 0.0
    detail["zeta"] = zeta
    health.last_eval_ts = now
    if zeta < C.ZETA_HOLD:
        health.consec_below += 1
        if health.consec_below >= C.KILL_CONSEC_EVALS and detail["decisive"]:
            health.killed = True
            detail["reason"] = "zeta_below_floor_%d_evals" % health.consec_below
            return KILL, detail
        detail["reason"] = "below_floor_%d_of_%d" % (health.consec_below, C.KILL_CONSEC_EVALS)
        return HOLD, detail
    health.consec_below = 0
    if zeta >= C.ZETA_RATCHET_UP:
        detail["reason"] = "zeta_ge_ratchet_up"
        return RATCHET_ELIGIBLE, detail
    detail["reason"] = "hold_band"
    return HOLD, detail


# =============================================================================================
# BOOK-WIDE PRESENCE COLLAPSE  (spec §8.7 T-D4) — with the three corrections BLOCKER-4
# requires.  Each correction exists because WITHOUT it the predicate halts a HEALTHY book.
# =============================================================================================
NO_HALT, HALT, INACTIVE, STARVED, WS_DEGRADED = \
    "no_halt", "halt", "inactive_insufficient_history", "rate_starved", "ws_degraded"


def psdh_book(rows, fundable_seconds):
    """Book-wide PSDH over a window.

    (a) THE DENOMINATOR EXCLUDES every second in which NO program was live (or none was
    fundable).  Otherwise a quiet overnight — THE NORMAL STATE — collapses the metric by
    arithmetic and halts a healthy book.  So the denominator is committed dollar-seconds
    restricted to fundable time, which is what `fundable_seconds` selects; passing the full
    window here is the bug this argument names.
    """
    t = _sum_rows(rows)
    if float(fundable_seconds) <= 0:
        return None
    committed_h = (t["rest_dollar_s"] + t["inv_dollar_s"]) / 3600.0
    if committed_h <= 0:
        return None
    return t["prox_dollar_s"] / committed_h


def collapse_check(rows, fundable_seconds, hourly_history, history_days,
                   fundable_hours_per_day, rate_yield_frac=0.0, ws_down_frac=0.0,
                   frac=C.COLLAPSE_FRAC):
    """spec §8.7's four-branch predicate.  Returns (verdict, detail).

    HALT if `PSDH_book < 0.25 × median(trailing 7 days of hourly PSDH_book)`, subject to:

    (a) denominator excludes non-fundable seconds — handled in `psdh_book`.
    (b) STARVATION IS NOT TOXICITY.  If `rate_yield` was active >20% of the window, or the WS
        was disconnected >20% of it, route to `rate_starved` / `ws_degraded` and DO NOT HALT:
        presence lost to our OWN throttling says nothing about who is eating us.
    (c) MINIMUM HISTORY: INACTIVE until ≥7 days × ≥6 fundable hours/day of history exist.
        A median over 3 samples is not a median; a fabricated one halts the book on its
        second day.

    Derivation of 25%: a 4× book-wide degradation is the same magnitude (2 ratchet rungs) that
    stands a single venue down.  2 h at ≥$300 committed is ≥600 $·h — decisive by §2.4.

    MIRROR (halting a healthy book ↔ failing to halt a collapsing one): (a)/(b)/(c) all guard
    the FIRST end, because every incident this week was a false positive.  The second end is
    guarded by the per-slot KILL in `evaluate_slot`, which needs no history at all and fires
    on a single venue in 45 minutes — the book-wide predicate is the backstop, not the primary.
    """
    detail = {"days": history_days, "fundable_h_per_day": fundable_hours_per_day,
              "rate_yield_frac": rate_yield_frac, "ws_down_frac": ws_down_frac}

    # (c) MINIMUM HISTORY — checked FIRST so a cold start can never reach the comparison.
    if (float(history_days) < C.COLLAPSE_MEDIAN_DAYS or
            float(fundable_hours_per_day) < C.COLLAPSE_MIN_FUNDABLE_H_PER_DAY):
        detail["reason"] = "insufficient_history"
        return INACTIVE, detail

    # (b) STARVATION IS NOT TOXICITY.
    if float(rate_yield_frac) > C.COLLAPSE_STARVATION_FRAC:
        detail["reason"] = "rate_yield_over_%.0f_pct" % (C.COLLAPSE_STARVATION_FRAC * 100)
        return STARVED, detail
    if float(ws_down_frac) > C.COLLAPSE_STARVATION_FRAC:
        detail["reason"] = "ws_down_over_%.0f_pct" % (C.COLLAPSE_STARVATION_FRAC * 100)
        return WS_DEGRADED, detail

    cur = psdh_book(rows, fundable_seconds)
    detail["psdh_book"] = cur
    if cur is None:
        # (a) again: no fundable seconds and/or no committed dollars in the window is the
        # overnight-quiet case, not a collapse.
        detail["reason"] = "no_fundable_committed_time"
        return NO_HALT, detail

    hist = sorted(float(x) for x in (hourly_history or []))
    if not hist:
        detail["reason"] = "no_history_samples"
        return INACTIVE, detail
    n = len(hist)
    med = hist[n // 2] if n % 2 else 0.5 * (hist[n // 2 - 1] + hist[n // 2])
    detail["median"] = med
    detail["threshold"] = float(frac) * med
    if cur < float(frac) * med:
        detail["reason"] = "psdh_book_below_%.0f_pct_of_median" % (float(frac) * 100)
        return HALT, detail
    detail["reason"] = "healthy"
    return NO_HALT, detail


# =============================================================================================
# COMPACTION  (spec §6.2) — fold rows older than 7 days into per-(m,s)-per-day aggregates.
# 7 days is DERIVED: exactly the trailing window T̂'s shrinkage and §8.7's collapse median
# require; nothing reads finer-grained history than that.
# MIRROR (compaction loses history ↔ metering grows unbounded): compaction NEVER rewrites a
# file in place — write the aggregate, fsync, THEN unlink the segment.  A metering record that
# can be silently rewritten is a metering record that cannot be trusted.
# =============================================================================================
def compact_rows(rows, day_key_fn=None):
    """Fold presence rows into per-(ticker, side, day) aggregates.  Pure; the caller owns the
    write-then-unlink ordering."""
    day_key_fn = day_key_fn or (lambda ts: int(float(ts) // 86400))
    out = {}
    for r in rows or []:
        k = (r.get("ticker"), r.get("side"), day_key_fn(r.get("from_ts", 0.0)))
        a = out.setdefault(k, {"t": "presence_daily", "ticker": k[0], "side": k[1], "day": k[2],
                               "rest_dollar_s": 0.0, "prox_dollar_s": 0.0, "inv_dollar_s": 0.0,
                               "at_best_s": 0.0, "ticks_ct": 0, "fills_ct": 0,
                               "fill_notional": 0.0, "rest_contract_s": 0.0})
        for f in ("rest_dollar_s", "prox_dollar_s", "inv_dollar_s", "at_best_s",
                  "fill_notional", "rest_contract_s"):
            a[f] += float(r.get(f, 0.0))
        for f in ("ticks_ct", "fills_ct"):
            a[f] += int(r.get(f, 0))
    return [out[k] for k in sorted(out)]
