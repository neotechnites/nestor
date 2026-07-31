"""lip_v5.dials — THE CAPITAL DIALS, DERIVED FROM C AT BOOT.

    "A = C/N, and N is capital-independent.  N >= z^2 p(1-p)/(d-p)^2 where d = day-stop
     fraction (0.2), z = confidence (2), p = per-cluster daily wipe probability."
                                       — note 54, THE CAPITAL-SCALING PROCEDURE, step 1

    "Couplings (decided 2026-07-30): floor↓ ⇒ funded-mix p↑ ⇒ N↑ ⇒ A↓ — compute the cap from
     the ACTUAL funded mix's p."                        — note 55, THE CLUSTER CAP, DERIVED

Nothing in this module is a constant that was chosen.  Two policy numbers enter — d and z —
and both carry their derivation in `config` beside them; everything else is measured off the
board, the calibration table and the phi posterior.

── THE DERIVATION, IN FULL (note 55, "THE CLUSTER CAP, DERIVED") ───────────────────────────
(1) WHY CLUSTERS.  Markets resolving from one fact settle together (measured: treasury tenors
    9/9 same-direction over 13 settle-days).  Diversification across them is fake; the
    catastrophe unit is the SETTLE SOURCE.
(2) A cluster's worst day is its whole allocation A, 100% loss-given-default.
(3) p = P(a cluster is wiped in a day) = P(its allocation converts to inventory in the day)
    x P(that inventory settles against us).
(4) Clusters are independent BY CONSTRUCTION (that is what `clusters.py` builds), so same-day
    wipes ~ Binomial(N, p) and the z-sigma day is  pN + z·sqrt(N p (1-p)).
(5) The day stop says A x (z-sigma wipes) <= d·C.  Substituting A = C/N and solving:

        N >= z^2 · p(1-p) / (d - p)^2                      <-- `n_required` below

    C cancels: N IS CAPITAL-INDEPENDENT.  A = C/N scales linearly.  p -> d ⇒ N -> infinity,
    which is the formula saying DON'T PLAY, and this module says it out loud rather than
    silently clamping (see `Dials.feasible`).

── THE TWO FACTORS OF p, AND THE ONE PLACE THIS BUILD READS THE NOTE AGAINST ITSELF ────────
note 55 §4 states p in two ways that cannot both ship, and the disagreement has to be
resolved in code:

  READING A (§4 sentence 1, and note 54 step 1 sentence 1):
      p = P(full allocation fills in a day) x P(settles against | filled)
  READING B (§4 sentence 4, note 54's parenthetical):
      "under the always-filled worst case (phi dropped deliberately) ... p reduces to the tail
       of FAIR draws"  — i.e. the first factor is 1.

READING B CANNOT BE IMPLEMENTED AS WRITTEN, and the arithmetic says so in one line.  Our
funded mix is wing-priced by design (measured natural average 19.7c — bleed.py's own
headline).  A filled 20c YES settles against us with probability
1 - 0.197 x (1 - 0.3508) = 0.872 after the calibration degrade.  With the first factor at 1
that is p = 0.872 >> d = 0.20, so `n_required` returns infinity and v6 funds NOTHING.  The
note's own headline numbers (p ~ 8-10%, N ~ 25-36, "run 30", A ~ $66 at $2k, ~$20 seats at
$600) are unreachable under reading B — and the note says where they DID come from:
"Re-measure p from cluster-days tape (loss >= 80% of cluster allocation / cluster-days)",
i.e. p is a MEASURED PRODUCT, and the measurement necessarily includes the days the
allocation never converted.

So the product is ANCHORED where it was measured and MOVED by the only factor the board can
price:

    p_against(mix)  = 1 - price x (1 - g(price))    P(settles against), the board price
                      DEGRADED BY THE MEASURED CALIBRATION GAP — note 55 §4's own words —
                      capital-weighted per cluster, then across clusters.
    p               = RUIN_P_BASE x p_against(mix) / p_against(RUIN_P_REF_PRICE)

  * RUIN_P_BASE = 0.09 is note 54's measured prior ("p ~ 8-10%"), and RUIN_P_REF_PRICE =
    0.197 is the price mix that prior was measured on (bleed.py: "funded-book natural average
    ~19.7c").  At the reference mix the ratio is exactly 1 and p = 0.09 ⇒ N = 28 ⇒ A = $21.43
    at C = 600 — which is the note's own "run 30 / ~$20 seats", computed instead of rounded.
  * THE COUPLING IS EXACTLY THE ONE RYAN DECIDED: "floor↓ ⇒ funded-mix p↑ ⇒ N↑ ⇒ A↓".  Drop
    the mix to 5c and p_against goes 0.870 -> 0.974, p goes 0.09 -> 0.101, N goes 28 -> 38,
    the rail goes $21.43 -> $15.79.  Cheaper rungs BUY A SMALLER RAIL, automatically.
  * THE ASSUMPTION IS MADE FALSIFIABLE, not hidden: `Dials.p_fill_implied` = p / p_against is
    logged every derivation.  It is the first factor this build is implicitly asserting
    (~0.103 at the reference mix), and the cluster-days tape will either confirm it or not.
    `p_from_tape` below is the socket the measurement plugs into the moment it exists.
  * `RUIN_ALWAYS_FILLED = True` forces reading B for anyone who wants to see the strict worst
    case's answer.  It is an instrument, not a mode; it returns "don't play", which is the
    honest content of reading B at these prices.

FLAGGED FOR THE REVIEWER: this is the single place where I read the spec against itself.  The
alternative — shipping reading B — is a bot that deploys zero dollars, which cannot be what
"turn it on tomorrow" means.

── THE FIXPOINT (the coupling is circular, and it converges) ───────────────────────────────
p depends on the FUNDED mix; the funded mix depends on A; A = C/N depends on p.  Resolved by
iterating from a seed rail, at most `MAX_ITERS` times, stopping when N stops moving.  Pure
function of (slots, C) — no state, so a restart re-derives the same dials from the same board
and the convergence spine is untouched.  The trajectory is logged.

── THE PRICE FLOOR DIAL ────────────────────────────────────────────────────────────────────
    "lower the floor until the marginal admitted rung's (expected credit - measured bleed)
     equals the deepening margin.  Both sides from the price-bucket calibration."
                                                                — note 54 step 3, note 55 §1

That sentence describes lambda — the marginal queue's own equalising rate.  Under v5 the
floor had to be a PRICE because the ranking had no bleed term and a price was the only guard
available (`ENTRY_BAND_LO_C = 6`, and the 2026-07-30 8.2c-book incident is what a ranking
without a bleed term does).  Under v6 the bleed term exists (`bleed.G_TABLE`, n = 8,240) and
the queue refuses any entry whose net is not positive, at any rank — so the floor is no
longer a number to set, it is a number to READ: `emergent_floor_c` reports the cheapest rung
the queue actually admitted this pass, and that is the floor, measured.  The hard band drops
to the exchange's own minimum tick under v6, which is also what the centrepiece requires
(1-2c qualification walls are refused outright by a 6c band).
"""

import math

from . import bleed as B
from . import clusters as CL
from . import config as C
from . import runtime as R


MAX_ITERS = 6                    # the fixpoint's bound.  N is integer-valued and monotone in
                                 # the mix, so it settles in 2-3; 6 is the "it cannot spin"
                                 # guard, not a tuning knob.  A non-converging derivation logs
                                 # and takes the LAST (most conservative) iterate.


def n_required(p, d=None, z=None):
    """`N >= z^2 p(1-p) / (d-p)^2` — the ruin formula, note 55's derivation step (5).

    Returns +inf when p >= d: the formula's own way of saying the game is unplayable at this
    wipe rate, and it is returned rather than clamped so the caller has to decide out loud.
    """
    d = float(C.RUIN_D if d is None else d)
    z = float(C.RUIN_Z if z is None else z)
    p = max(0.0, min(1.0, float(p)))
    if p >= d:
        return float("inf")
    return (z * z) * p * (1.0 - p) / ((d - p) ** 2)


def p_against(price_usd):
    """P(this position settles against us) = 1 - realised win rate at its posted price.

    The realised rate is the POSTED price degraded by the measured calibration gap:
    `realised = price x (1 - g(price))`, g off `bleed.G_TABLE` (n = 8,240 settled markets,
    16,480 side-observations).  This is note 55 §4's "READ OFF THE BOARD (the price), degraded
    by the measured calibration gap", verbatim, in one line.
    """
    price = max(0.0, min(1.0, float(price_usd)))
    return max(0.0, min(1.0, 1.0 - price * (1.0 - B.g_for_price(price))))


def p_fill_ratio(phi, h):
    """P(the resting lot turns over at least once inside the day) = 1 - exp(-phi x h).

    NOT the ruin formula's first factor — see the header: that factor is inside the measured
    anchor.  This is the INSTRUMENT the always-filled reading uses, and the quantity the
    cluster-days tape will eventually replace the anchor with.  Poisson hazard at the phi
    posterior's own rate (fills per hour per resting contract, `scan.phi_posterior`).
    """
    lam = max(0.0, float(phi)) * max(0.0, float(h))
    return 1.0 - math.exp(-lam)


def p_from_mix(price_usd):
    """THE PER-CLUSTER WIPE PROBABILITY at a mix whose capital-weighted collateral price is
    `price_usd` — the header's anchored-and-coupled form, in one line.

    Under `RUIN_ALWAYS_FILLED` the anchor is dropped and p IS p_against (reading B).
    """
    pa = p_against(price_usd)
    if C.RUIN_ALWAYS_FILLED:
        return pa
    ref = p_against(C.RUIN_P_REF_PRICE)
    if ref <= 0.0:
        return pa
    return max(0.0, min(1.0, float(C.RUIN_P_BASE) * pa / ref))


def p_from_tape(wipe_cluster_days, cluster_days, prior=None, prior_days=None):
    """THE SOCKET FOR THE MEASUREMENT (note 54 step 1: "Re-measure p from cluster-days tape
    (loss >= 80% of cluster allocation / cluster-days) before scaling").

    Beta-binomial shrinkage toward the note's prior, with the prior's own strength expressed
    in cluster-days so a thin tape cannot move the rail: `RUIN_P_PRIOR_DAYS` is the number of
    cluster-days the 8-10% prior is worth, and it is DERIVED, not chosen — the prior is stated
    to one significant figure (8-10%, i.e. +-1pp around 0.09), and a binomial proportion has
    standard error sqrt(p(1-p)/n), so the n that reproduces that stated precision is
    n = p(1-p)/se^2 = 0.09 x 0.91 / 0.01^2 ~ 819 cluster-days.  Anything less than that much
    of our own tape does not get to overrule it.
    """
    prior = float(C.RUIN_P_BASE if prior is None else prior)
    k = float(C.RUIN_P_PRIOR_DAYS if prior_days is None else prior_days)
    n = max(0.0, float(cluster_days))
    w = max(0.0, float(wipe_cluster_days))
    if n + k <= 0:
        return prior
    return (w + k * prior) / (n + k)


class Dials(object):
    """The boot-derived numbers, and every input that produced them."""

    __slots__ = ("capital_usd", "p", "p_fill_measured", "p_against", "n_required",
                 "n_clusters", "rail_usd", "feasible", "iters", "mix_clusters", "floor_c",
                 "mix_price")

    def __init__(self, capital_usd, p, pf, pa, n_req, n, rail, feasible, iters=0,
                 mix_clusters=0, floor_c=None, mix_price=0.0):
        self.capital_usd = float(capital_usd)
        self.p = float(p)
        self.p_fill_measured = float(pf)
        self.p_against = float(pa)
        self.mix_price = float(mix_price)
        self.n_required = n_req
        self.n_clusters = n
        self.rail_usd = float(rail)
        self.feasible = bool(feasible)
        self.iters = int(iters)
        self.mix_clusters = int(mix_clusters)
        self.floor_c = floor_c

    @property
    def p_fill_implied(self):
        """THE ASSUMPTION, MADE READABLE (module header).  p / p_against is the first factor
        this build is implicitly asserting — the fraction of cluster-days on which the
        allocation actually converts.  Logged every derivation so the cluster-days tape can
        falsify it."""
        return (self.p / self.p_against) if self.p_against > 0 else 0.0

    def numbers(self):
        return {"capital_usd": round(self.capital_usd, 2), "p": round(self.p, 5),
                "mix_price_c": round(self.mix_price * 100.0, 2),
                "p_fill_implied": round(self.p_fill_implied, 5),
                "p_fill_measured": round(self.p_fill_measured, 5),
                "p_against": round(self.p_against, 5),
                "p_base": C.RUIN_P_BASE, "p_ref_price": C.RUIN_P_REF_PRICE,
                "d": C.RUIN_D, "z": C.RUIN_Z,
                "n_required": (None if self.n_required == float("inf")
                               else round(self.n_required, 3)),
                "n_clusters": self.n_clusters, "rail_usd": round(self.rail_usd, 4),
                "feasible": self.feasible, "iters": self.iters,
                "mix_clusters": self.mix_clusters, "floor_c": self.floor_c,
                "always_filled": C.RUIN_ALWAYS_FILLED}


def mix_p(rows):
    """The funded mix's wipe probability p, capital-weighted, per the header's anchored form.

    `rows` — iterable of (cluster, capital_usd, unit_price_usd, phi, h).  The wipe unit is the
    CLUSTER, so each cluster's own capital-weighted price gives that cluster's p, and the
    portfolio p is the capital-weighted mean across clusters.  Returns
    (p, p_fill_measured, p_against, n_clusters, mix_price); an empty mix returns zeros and the
    caller must not derive from it.
    """
    per = {}
    for ck, cap, price, phi, h in rows:
        cap = max(0.0, float(cap))
        if cap <= 0:
            continue
        acc = per.setdefault(ck, [0.0, 0.0, 0.0, 0.0])
        acc[0] += cap
        acc[1] += cap * float(price)
        acc[2] += cap * max(0.0, float(phi))
        acc[3] += cap * max(0.0, float(h))
    if not per:
        return 0.0, 0.0, 0.0, 0, 0.0
    tot = sum(a[0] for a in per.values())
    p_sum = pf_sum = pa_sum = px_sum = 0.0
    for _ck, (cap, cp, cphi, ch) in per.items():
        price = cp / cap
        phi = cphi / cap
        h = ch / cap
        p_sum += cap * p_from_mix(price)
        pf_sum += cap * p_fill_ratio(phi, h)
        pa_sum += cap * p_against(price)
        px_sum += cap * price
    return p_sum / tot, pf_sum / tot, pa_sum / tot, len(per), px_sum / tot


def derive(capital_usd, rows, iters=0):
    """One step: funded mix -> p -> N -> A.  `rows` is `mix_p`'s input."""
    p, pf, pa, nclust, px = mix_p(rows)
    if nclust <= 0:
        # NO EVIDENCE ⇒ NO RE-DERIVATION.  An empty funded mix has p = 0, and p = 0 makes the
        # ruin formula return N = 0 ⇒ N = 1 ⇒ A = C — the WHOLE STACK on one settle source,
        # produced by having measured nothing at all.  That is the worst possible failure
        # direction for a ruin guard, and it is reachable on any cycle where the board is
        # empty, the poll was clamped, or every candidate was refused.  So an empty mix keeps
        # the SEED (v5's own derived N), loudly.
        R.log("dials_no_mix", capital_usd=round(float(capital_usd), 2), iters=iters,
              note="empty funded mix — holding the seed rail; p is not measurable from zero "
                   "funded clusters")
        d = seed_dials(capital_usd)
        d.iters = iters
        return d
    n_req = n_required(p)
    if n_req == float("inf"):
        # "p -> d ⇒ N -> infinity ('don't play')" — note 55.  The rail is ZERO and the queue
        # funds nothing.  This is NOT a halt (v6 has no loss stopper, note 55 risk frame): it
        # is the allocator refusing to buy at this wipe rate, and it clears itself the moment
        # the board's prices or the phi tape move.  Loud, because a book that deploys nothing
        # must never be mistaken for a book that is quietly working.
        R.ntfy("dials_dont_play",
               "lip_v6 ruin formula refuses to play: p=%.4f >= d=%.2f (rail $0)" % (p, C.RUIN_D))
        return Dials(capital_usd, p, pf, pa, n_req, 0, 0.0, False, iters, nclust,
                     mix_price=px)
    n = max(1, int(math.ceil(n_req)))
    return Dials(capital_usd, p, pf, pa, n_req, n, float(capital_usd) / n, True, iters,
                 nclust, mix_price=px)


def seed_dials(capital_usd):
    """THE FIXPOINT'S STARTING POINT, and the answer when there is no board yet.

    N_TARGET_CLUSTERS = 30 is v5's own derived diversification target (config: measured supply
    ~38 clusters that can clear $1.00 at half presence) and it is the SAME 30 note 55 quotes as
    the ruin formula's answer at p ~ 8-10% — so seeding there is seeding at the fixpoint's own
    neighbourhood, not at an arbitrary number.  It is a SEED ONLY: one pass of real board data
    replaces it, and the log shows the move.
    """
    n = int(C.N_TARGET_CLUSTERS)
    return Dials(capital_usd, 0.0, 0.0, 0.0, float(n), n, float(capital_usd) / n, True,
                 0, 0)


def rows_from_alloc(alloc, curves):
    """(cluster, capital, price, phi, h) rows from a marginal-queue allocation."""
    out = []
    for key, q in alloc.items():
        if q <= 0:
            continue
        cv = curves.get(key)
        if cv is None:
            continue
        out.append((cv.cluster, cv.capital(q), cv.p, float(cv.slot.phi), cv.h))
    return out


def emergent_floor_c(alloc, curves):
    """THE PRICE FLOOR, READ RATHER THAN SET (module header).  The cheapest collateral price
    the queue actually admitted this pass, in cents — the observable form of note 54 step 3's
    "lower the floor until the marginal admitted rung's (credit - bleed) equals the deepening
    margin", because that equality IS the queue's lambda and this is the rung that met it."""
    prices = [curves[k].p for k, q in alloc.items() if q > 0 and k in curves]
    if not prices:
        return None
    return int(round(min(prices) * 100))


def derive_from_slots(capital_usd, slots, allocate_fn, max_iters=MAX_ITERS, **alloc_kw):
    """THE BOOT/CYCLE DERIVATION.  Iterate rail -> queue -> mix -> p -> N -> rail until N
    stops moving.  Returns the final `Dials`.  `allocate_fn(slots, budget, rail, **kw)` must
    return `(alloc, spent, report)` with `report["curves"]` — i.e. `marginal.allocate_marginal`
    with its rails bound.  Pure, deterministic, logged at every iterate.
    """
    d = seed_dials(capital_usd)
    best = d                                     # the most CONSERVATIVE iterate seen
    seen = []
    for i in range(1, int(max_iters) + 1):
        if not d.feasible or d.rail_usd <= 0.0:
            break
        a, _spent, rep = allocate_fn(slots, float(capital_usd), d.rail_usd, **alloc_kw)
        rows = rows_from_alloc(a, rep.get("curves") or {})
        if not rows:
            # THE TIGHTER RAIL FUNDED NOTHING.  This is not "no evidence" — it is the rail
            # having become smaller than the cheapest entry block on the board, and it is the
            # oscillation this fixpoint would otherwise sit in forever (fund at $10 -> mix
            # says $8.33 -> $8.33 funds nothing -> no mix -> back to $10).  Stop and KEEP THE
            # TIGHTER RAIL: the ruin bound is a bound, and a board with nothing affordable
            # inside it is a board we do not buy, not a reason to widen the bound.
            R.log("dials_rail_starves_board", i=i, rail_usd=round(d.rail_usd, 4),
                  n_clusters=d.n_clusters,
                  note="no entry block fits the derived rail; holding it")
            return d
        nxt = derive(capital_usd, rows, iters=i)
        nxt.floor_c = emergent_floor_c(a, rep.get("curves") or {})
        R.log("dials_iterate", i=i, prev_n=d.n_clusters, prev_rail=round(d.rail_usd, 4),
              **nxt.numbers())
        seen.append(nxt.n_clusters)
        if not nxt.feasible:
            return nxt
        if nxt.n_clusters > best.n_clusters:
            best = nxt
        if nxt.n_clusters == d.n_clusters:
            return nxt
        d = nxt
    # NO FIXPOINT.  Take the most conservative iterate (largest N, smallest rail), never the
    # last: which iterate is "last" is an artefact of where the loop stopped, and a ruin
    # guard may not be decided by an artefact.  Logged, with the whole trajectory.
    R.log("dials_no_fixpoint", trajectory=seen, taking=best.n_clusters,
          rail_usd=round(best.rail_usd, 4),
          note="N oscillated inside the mix's own granularity; taking the tightest rail seen")
    return best
