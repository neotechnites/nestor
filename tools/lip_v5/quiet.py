"""lip_v5.quiet — THE QUIET LADDER-WIDE CLASS.  v6's centrepiece.

    "THE CENTREPIECE — ladder-wide presence in quiet venues.  The $70-day decomposition: v4
     grossed $70 via fat rungs (~$8) across ENTIRE ladders (dozens of per-strike pools, both
     sides, $448 deployed) — at −$195 of fills.  Net −$124: volume at negative margin.  v6
     builds the same revenue shape where the margin is ~zero: quiet families (treasuries,
     hourly-price ladders — phi≈0 structurally), every affordable strike, both sides where
     clean, seats sized to qualify walls.  One-market-per-cluster is RELAXED for this class:
     the cluster cap bounds DOLLARS."       — note 55, FINAL AMENDMENTS, item 2

── WHAT THIS MODULE DECIDES, AND WHAT IT DOES NOT ──────────────────────────────────────────
It decides ONE thing: which clusters are exempt from law §2's one-market-per-cluster rule.
It decides NOTHING about size, price, sides or funding — every one of those is the marginal
queue's, unchanged, per strike.  That separation is the whole safety argument: relaxing the
market count does not relax a single dollar bound, because

  * the CLUSTER DOLLAR RAIL (A = C/N, `dials`) is unchanged and is the correlation bound —
    note 55's own reconciliation: "the cluster cap bounds DOLLARS";
  * each funded strike must still clear its OWN forfeit cliff, which is `marginal.Curve`'s
    entry block: expected credit share x pool/2 x h must reach $1.00 per strike per window,
    or the strike is not funded.  "fat-enough-per-strike or not funded" is therefore already
    the arithmetic, and this module adds nothing to it;
  * each funded strike must still clear the fill-bleed viability screen (net entry > 0).

WHY THIS IS NOT v4's $70/-$195 DAY.  v4 spread fat rungs across whole ladders and PAID FOR IT
IN FILLS: $448 deployed, -$195 of inventory conversion.  The margin was negative and nothing
in the machine could see it, because the bleed term did not exist.  The identical revenue
shape is safe exactly where the fills do not happen — so the entry condition for the whole
class is a MEASUREMENT that they do not.

── THE MEASUREMENT: "phi ≈ 0 STRUCTURALLY" ─────────────────────────────────────────────────
Two pieces of evidence, and BOTH must hold (note 55: "measured via the shrinkage estimator's
own posteriors and the public-trades evidence").

(E1) OUR OWN TAPE, POOLED OVER THE FAMILY.  Zero fills, over enough exposure that zero means
     something.  Zero counts have a closed-form 95% bound this codebase already owns —
     `RULE_OF_THREE / exposure` (config, spec §2.4) — and the claim the class needs is
     precisely the claim `alloc.Need.evidence_bounds_a_turnover` already makes per rung:

         (RULE_OF_THREE / exposure) x h <= 1     ⇒     exposure >= 3h

     "this lot will not turn over inside the horizon", at 95%.  The ONLY change is that the
     evidence is POOLED ACROSS THE FAMILY rather than per strike — which is the entire point
     of the class: a treasury ladder's strikes share a fill process, so 72 contract-hours
     spread over eight tenors licenses presence on all eight, including tenors we have never
     rested in.  Per-strike evidence could never license a strike we have not touched, and a
     class that cannot open a new strike is not ladder-wide presence.
     ANY FILL AT ALL DISQUALIFIES THE FAMILY.  Not "few fills" — none.  "phi ≈ 0
     STRUCTURALLY" is a claim about the venue's nature, and one fill refutes it; a
     rate-threshold would be a constant nobody derived, and the whole class exists to be the
     zero-margin case.  The fills count is RECONSTRUCTED exactly from the posterior's own
     stored inputs (phi = (fills + k x prior)/(exposure + k) inverts to
     fills = phi x (exposure + k) - k x prior), so no new plumbing and no second source of
     truth.

(E2) THE PUBLIC TRADES TAPE.  Our own zero can mean "nobody trades here" or it can mean "we
     were never at the touch".  A print against a resting level IS someone's fill (note 55
     §6), so a family with public prints is NOT structurally quiet no matter what our own
     exposure says.  `trades_by_cluster` is that feed; it is OPTIONAL and its absence is
     LOGGED, never assumed away — when it is absent the class runs on (E1) alone and the
     `trades_evidence` field on every classification line says so, so the tape can be audited
     for exactly this weakening.

── THE PROBE IS DEDUCTIVELY EXEMPT, AND SAYS SO ────────────────────────────────────────────
(E1) needs 3h x horizon of our own pooled exposure.  On day one we have none, so no family
classifies quiet, so the centrepiece would never fire — and note 55's deploy plan is that the
centrepiece fires FIRST ("stake $120 in treasury + gas ... test earnings").  That is not a
contradiction, it is the shape of an experiment: the probe is what CREATES the evidence this
module needs.  So `probe.Probe` supplies its own families as multi-market by construction and
does not consult this module, and this module governs the ORDINARY book.  Both facts are
logged; neither is implicit.
"""

from . import clusters as CL
from . import config as C
from . import runtime as R


def fills_from_posterior(slot):
    """The fill count the shrinkage posterior was built from, recovered exactly.

        phi = (fills + k x prior) / (exposure + k)   ⇒   fills = phi x (exposure + k) - k x prior

    Returns None when the slot asserts phi as a fact (`phi_exposure_h is None`, the hand-built
    Slot idiom) — an asserted phi is not evidence and cannot license a family.
    """
    e = getattr(slot, "phi_exposure_h", None)
    if e is None:
        return None
    k = max(0.0, float(getattr(slot, "phi_k", 0.0) or 0.0))
    prior = float(getattr(slot, "phi_prior", slot.phi))
    return max(0.0, float(slot.phi) * (float(e) + k) - k * prior)


def family_evidence(slots):
    """Pool (E1) over one family.  Returns (fills, exposure_h, decisive) where `decisive` is
    False when any member asserts its phi rather than measuring it."""
    fills = exposure = 0.0
    decisive = True
    for s in slots:
        f = fills_from_posterior(s)
        if f is None:
            decisive = False
            continue
        fills += f
        exposure += max(0.0, float(s.phi_exposure_h or 0.0))
    return fills, exposure, decisive


# A reconstructed fill count is a float built from a division; "zero fills" has to tolerate
# the float, and half a fill is the only tolerance that cannot round a real fill away.
FILL_EPS = 0.5


def is_quiet(slots, horizon_h, trades=None):
    """Is this family structurally quiet?  Returns (bool, numbers-dict-for-the-log)."""
    fills, exposure, decisive = family_evidence(slots)
    bound = (float(C.RULE_OF_THREE) / exposure) if exposure > 0 else float("inf")
    need_exposure = float(C.RULE_OF_THREE) * float(horizon_h)
    why = ""
    ok = True
    if not decisive:
        ok, why = False, "phi_asserted_not_measured"
    elif fills >= FILL_EPS:
        ok, why = False, "own_fills_observed"
    elif exposure < need_exposure:
        ok, why = False, "exposure_below_3h"
    elif trades is not None and float(trades) > 0:
        ok, why = False, "public_trades_observed"
    return ok, {"fills": round(fills, 4), "exposure_h": round(exposure, 3),
                "phi_upper_95": (None if bound == float("inf") else round(bound, 6)),
                "need_exposure_h": round(need_exposure, 2),
                "trades_evidence": ("absent" if trades is None else float(trades)),
                "quiet": ok, "why": why}


def family_phi_bound(exposure_h):
    """THE FAMILY'S OWN 95% UPPER BOUND ON phi — `RULE_OF_THREE / pooled exposure`.

    WHY A QUIET FAMILY MUST ALSO SUPPLY ITS STRIKES' phi.  `scan.phi_posterior` shrinks each
    rung toward a PRICE-BUCKET neighbourhood: a 1c treasury wall borrows its prior from every
    other 1c rung on the board, most of which are nothing like it.  At 30 contract-hours
    against a k of 10 that borrowed prior still carries 25% of the weight, and 25% of a busy
    board's phi is enough bleed (g(1c) = 0.9484) to make the wall read NEGATIVE — so the
    centrepiece could never fire, not because the venue fills but because the estimator was
    pooling over the wrong neighbourhood.

    The right pooling unit for a structurally quiet venue is the FAMILY, and the family's
    evidence is zero fills over pooled exposure, whose closed-form 95% bound this codebase
    already owns.  The BOUND is used rather than the point estimate because it is the
    conservative direction — it OVER-states the bleed — and because it is the same quantity
    the class's own admission test is built on, so one number does both jobs.
    It is applied ONLY where it LOWERS a strike's phi (`min` at the call site): family
    evidence may license presence, never manufacture it.
    """
    e = max(0.0, float(exposure_h))
    if e <= 0.0:
        return float("inf")
    return float(C.RULE_OF_THREE) / e


def classify(slots, horizon_h, trades_by_cluster=None):
    """THE ONE ENTRY POINT.  Returns `(quiet_clusters, phi_by_cluster)` — the clusters exempt
    from one-market-per-cluster, and the family-pooled phi bound each of them supplies to its
    own strikes.  Logs one line per family with every number that decided it: a family
    admitted (or refused) to the centrepiece without its arithmetic on the tape is the next
    incident.
    """
    by_cluster = {}
    for s in slots:
        by_cluster.setdefault(CL.cluster_of(s.ticker), []).append(s)
    out, phi_by_cluster = set(), {}
    for ck in sorted(by_cluster):
        members = by_cluster[ck]
        trades = None if trades_by_cluster is None else trades_by_cluster.get(ck, 0)
        ok, nums = is_quiet(members, horizon_h, trades=trades)
        if ok:
            out.add(ck)
            _fills, exposure, _dec = family_evidence(members)
            phi_by_cluster[ck] = family_phi_bound(exposure)
        R.log("quiet_family", cluster=ck, markets=len({s.ticker for s in members}),
              sides=len(members), family_phi=phi_by_cluster.get(ck), **nums)
    if trades_by_cluster is None:
        R.log_once("quiet_trades_feed_absent",
                   note="the public-trades half of the quiet test (note 55 §6) is not wired "
                        "on this host; the class is running on our own pooled zero-fill "
                        "evidence alone and every quiet_family line says trades_evidence=absent")
    return out, phi_by_cluster
