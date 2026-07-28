"""
lip_v5.clusters — the UNDERLYING-CLUSTER CAP.

**Why this file exists: a live loss, today, on the treasury book.**  v4's caps are per-MARKET
and per-SERIES.  Neither is the unit of risk.  Fifteen rungs of a "yield ≥ X" ladder are not
fifteen bets — they are ONE bet on rates, expressed fifteen times, and a falling-yield morning
ran over all of them together for ~$150 of a single direction that every per-market check had
individually approved.

The per-market cap cannot see this by construction: it asks "is this market too big?" and each
rung answers honestly no.  The question nobody was asking is **"how much of ONE UNDERLYING do
we now own, across every series that settles off it?"**  Precedent: nestor's `cluster_cap_frac`.

Two measures, because one is not enough (this is the note-23 §IV mirror, answered):

  * `signed_delta_usd` — NET DIRECTIONAL exposure.  Caps the live failure: many rungs, one
    direction.
  * `worst_case_loss_usd` — the MOST we can lose across every possible settlement of the
    underlying.  **This is the mirror's answer.**  A net cap alone is defeated by a
    both-sides-heavy book: buying YES at 4.15 and NO at 4.10 nets to ZERO directionally, but if
    the underlying lands in [4.10, 4.15) *both legs lose*, so the true exposure is the sum, not
    the difference.  A signed cap would wave that through; the worst-case measure refuses it.

Both are capped, and the worst-case measure is the binding one.
"""

import re

from . import config as C


# =============================================================================================
# CLUSTER IDENTITY — "all rungs of all series sharing a SETTLE SOURCE".
# =============================================================================================
# The rates complex settles off the Treasury yield curve.  2Y/5Y/7Y/10Y/30Y are separate series
# but ONE underlying for this purpose: a falling-yield morning moves the whole curve, which is
# exactly the correlation that produced the loss.  (The review said "the three UST series"; the
# repo carries FIVE — 2/5/7/10/30 — and all five are included, because a cluster cap that omits
# a correlated series is the same defect one series smaller.)
CLUSTER_MAP = {
    "RATES": ("KXUST2AD", "KXUST5AD", "KXUST7AD", "KXUST10AD", "KXUST30AD"),
}

# Everything else is its own cluster, keyed by its series: a gas ladder is one cluster, an
# index ladder is one cluster.  That is the DEFAULT, not a special case — a series is the
# smallest thing that can carry a ladder, so "series" is the right fallback identity.


def cluster_of(ticker, cluster_map=None):
    """The cluster a ticker belongs to.  Explicit map first, else its own series.

    Series prefix, not equality: Kalshi tickers are `SERIES-EVENT-STRIKE`.
    """
    cluster_map = CLUSTER_MAP if cluster_map is None else cluster_map
    t = str(ticker or "").upper()
    for cluster, members in cluster_map.items():
        for s in members:
            if t == s or t.startswith(s + "-"):
                return cluster
    return t.split("-", 1)[0]


_STRIKE_RE = re.compile(r"^[TBG]?(\d+(?:\.\d+)?)$")


def parse_threshold(ticker):
    """The strike of a THRESHOLD market (`...-T4.65`), or None.

    Returns None for anything we cannot read as a threshold — including `B`-style range
    markets, whose payoff is not `1[X ≥ k]` and therefore does not net the same way.  A None
    strike is treated as UNNETTABLE by `worst_case_loss_usd`, i.e. its full basis counts as
    loss.  That OVERSTATES the exposure of a mutually-exclusive range book (where exactly one
    rung pays), which is the safe direction: it refuses earlier than necessary rather than
    netting a structure it has not verified.
    """
    parts = str(ticker or "").rsplit("-", 1)
    if len(parts) < 2:
        return None
    m = _STRIKE_RE.match(parts[1].upper())
    if not m:
        return None
    # A `T` prefix is the threshold form we have verified.  Bare numerics are accepted only
    # when unambiguous; `B` (range) is deliberately refused above by returning None.
    if not parts[1].upper().startswith("T"):
        return None
    return float(m.group(1))


def leg_sign(side):
    """Directional sign of a leg on the UNDERLYING.

    A YES on `X ≥ k` pays when the underlying is HIGH: long.  A NO on the same market pays when
    it is LOW: short.  So `yes/bid` = +1 and `no/ask` = −1, and rungs of one ladder held on the
    same side ACCUMULATE rather than diversify — which is the whole finding.
    """
    s = str(side or "").strip().lower()
    return 1.0 if s in ("yes", "bid") else -1.0


# =============================================================================================
# THE TWO MEASURES
# =============================================================================================
def signed_delta_usd(positions):
    """`Σ sign × n × basis` over one cluster — NET directional exposure, in dollars of basis.

    Crude by design (the review's "crude-but-safe v1"): it prices each contract at what we paid
    rather than at a modelled delta.  Basis is the right crude unit because it is exactly the
    capital at risk on that leg, it is already in the ledger, and it needs no model of the
    underlying — a modelled delta would import a distributional assumption into a risk cap,
    which is how caps become opinions.
    """
    return sum(leg_sign(p["side"]) * float(p["n"]) * _at_risk_per_contract(p)
               for p in positions or [])


def _at_risk_per_contract(p):
    """What one contract of this position can still LOSE.

    **note 43 §2: the entry price is sunk.**  A contract bought at $0.40 and now marked $0.05
    can lose $0.05, not $0.40 — the other $0.35 is already gone and no cap can un-spend it.
    Using basis would make a risk cap tighten as positions moved AGAINST us (refusing new,
    possibly good, exposure because of an old loss) and loosen as they moved for us: precisely
    the anchor-on-entry behaviour that "cuts winners and rides losers by construction".

    Fallback `mark → basis` is the unpriced case only, matching the day stop's mark-at-cost
    convention.  At PLACEMENT the two coincide, so a prospective order is unaffected either way.
    """
    m = p.get("mark")
    return float(p["basis"] if m is None else m)


def gross_basis_usd(positions):
    return sum(abs(float(p["n"])) * _at_risk_per_contract(p) for p in positions or [])


def worst_case_loss_usd(positions):
    """The MOST this cluster can lose, over every possible settlement of the underlying.

    Payoff is piecewise constant in the underlying `X`, with breakpoints only at the strikes:

        payoff(X) = Σ_yes n_i·1[X ≥ k_i]  +  Σ_no m_j·1[X < k_j]
        worst_loss = total_basis − min_X payoff(X)

    so it suffices to evaluate at each strike and once below all of them.  This is EXACT for a
    threshold ladder — no distributional assumption, no model — which is why it, and not the
    signed measure, is the cap that binds.

    Positions whose strike we cannot read contribute their full basis and never contribute
    payoff: the conservative reading, and the reason `parse_threshold` refuses to guess.

    MIRROR (cap the NET ↔ what about a gross/both-sides-heavy book?): THIS IS THAT ANSWER.
    YES@4.15 + NO@4.10 nets to zero on `signed_delta_usd` while both legs lose together if the
    underlying lands in [4.10, 4.15).  Here that book's worst case is the SUM of both bases, so
    it is refused at the same bound that refuses fifteen rungs pointing one way.
    """
    # Annotate unconditionally.  Requiring the caller to have called `annotate` first would
    # make the SAFE reading depend on a step someone can forget, and forgetting it here fails
    # OPEN (an un-annotated book has no strikes, so nothing nets... which is conservative, but
    # a risk cap must not have two behaviours for one input).
    positions = annotate(positions)
    if not positions:
        return 0.0
    total_basis = gross_basis_usd(positions)
    nettable = [p for p in positions if p.get("strike") is not None]
    unnettable_basis = sum(abs(float(p["n"])) * _at_risk_per_contract(p)
                           for p in positions if p.get("strike") is None)
    if not nettable:
        return total_basis
    strikes = sorted({float(p["strike"]) for p in nettable})
    test_points = [strikes[0] - 1.0] + strikes
    best_payoff = None
    for x in test_points:
        payoff = 0.0
        for p in nettable:
            k = float(p["strike"])
            n = abs(float(p["n"]))
            if leg_sign(p["side"]) > 0:
                if x >= k:
                    payoff += n            # YES pays $1 per contract
            else:
                if x < k:
                    payoff += n            # NO pays $1 per contract
        best_payoff = payoff if best_payoff is None else min(best_payoff, payoff)
    # Unnettable legs are assumed to pay nothing in the worst case; they are already inside
    # `total_basis`, so nothing more is added here.
    del unnettable_basis
    return total_basis - (best_payoff or 0.0)


def annotate(positions):
    """Attach `cluster` and `strike` to raw positions `{ticker, side, n, basis}`."""
    out = []
    for p in positions or []:
        q = dict(p)
        q.setdefault("cluster", cluster_of(p["ticker"]))
        q.setdefault("strike", parse_threshold(p["ticker"]))
        out.append(q)
    return out


def by_cluster(positions):
    groups = {}
    for p in annotate(positions):
        groups.setdefault(p["cluster"], []).append(p)
    return groups


# =============================================================================================
# THE CAP
# =============================================================================================
def cluster_cap_usd(day_stop_threshold_usd, inv_cap_usd=C.INV_CAP_USD):
    """`max(INV_CAP_USD, 0.5 × day_stop_threshold)` — the SAME derivation as `cap_series_usd`,
    for the same reason and at the same factor: **no single correlated bet may trip the global
    day stop on its own**, because one underlying halting the whole book contradicts charter §5
    (stand a VENUE down, never the bot).

    Why it must exist even though `cap_series_usd` already says this: caps DO NOT COMPOSE.  A
    cluster spanning five UST series inherits five per-series caps, i.e. `2.5 ×
    day_stop` of permission for one underlying — which is precisely how ~$150 of one direction
    passed fifteen individually-approved checks.  The cluster cap is the one that binds; the
    series cap survives as a belt for the single-series case.

    MIRROR (cluster cap too TIGHT ↔ too loose): too tight refuses correlated rungs we could
    have afforded — a RATE loss, and the water level simply reallocates to another cluster,
    which is the diversification we wanted anyway.  Too loose is this morning.  The asymmetry
    is why the factor matches the day-stop-derived series cap rather than being tuned up.
    """
    return max(float(inv_cap_usd), 0.5 * float(day_stop_threshold_usd))


ADMIT, REFUSE_SIGNED, REFUSE_WORST = "admit", "cluster_signed_cap", "cluster_worst_case_cap"


def cluster_admits(existing, prospective, cap_usd):
    """May `prospective` be added?  Checked in `place()`, BESIDE the per-market check — not
    instead of it, and not after the order is live.

    `existing` / `prospective`: `{ticker, side, n, basis}`.  Returns (ok, reason, detail).

    Both measures are enforced against the same bound.  The worst-case measure is the strictly
    stronger of the two on any book that is not perfectly one-directional, so in practice it is
    what refuses; the signed measure is kept because it names the failure mode in the log
    (`cluster_signed_cap` reads as "too much of one direction", which is the sentence the
    operator needs).
    """
    cluster = cluster_of(prospective["ticker"])
    same = [p for p in annotate(existing) if p["cluster"] == cluster]
    after = same + annotate([prospective])
    signed = abs(signed_delta_usd(after))
    worst = worst_case_loss_usd(after)
    detail = {"cluster": cluster, "signed_usd": signed, "worst_case_usd": worst,
              "cap_usd": float(cap_usd), "n_positions": len(after)}
    if worst > float(cap_usd) + 1e-9:
        return False, REFUSE_WORST, detail
    if signed > float(cap_usd) + 1e-9:
        return False, REFUSE_SIGNED, detail
    return True, ADMIT, detail


def cluster_report(positions, cap_usd):
    """Per-cluster telemetry for the cycle log, so the operator sees concentration BEFORE it
    becomes a loss rather than after."""
    out = []
    for cluster, ps in sorted(by_cluster(positions).items()):
        out.append({"cluster": cluster, "n_positions": len(ps),
                    "signed_usd": round(signed_delta_usd(ps), 4),
                    "gross_usd": round(gross_basis_usd(ps), 4),
                    "worst_case_usd": round(worst_case_loss_usd(ps), 4),
                    "cap_usd": round(float(cap_usd), 4),
                    "utilization": round(worst_case_loss_usd(ps) / float(cap_usd), 4)
                    if float(cap_usd) > 0 else None})
    return out
