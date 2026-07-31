"""lip_v5.probe — THE 120/480 DEPLOY SPLIT.  v6's boot mode.

    "THE DEPLOY PLAN — RYAN'S 120/480.  Ryan: stake $120 in treasury + gas (the measured top
     earners), 'not worry about risk,' test earnings; the rest stabilizes.  VERIFIED AGAINST
     THE MATH: worst case of the $120 (everything fills, everything settles wrong) = exactly
     d x C = 20% of $600 — the concentrated probe is precisely as safe as the diversified book
     by the day-stop's own arithmetic (the ruin formula inverted).  Conditions that keep it
     honest: WINGS AND WALLS ONLY (treasury qualification walls across tenors at 1-2c sides
     ~$10-20 each + gas ladder wings across strikes; NEVER mid-priced fat legs — that's what
     killed v4, not concentration); never-sell unchanged; treasuries+gas = two settle sources,
     both-die-day = the full $120, priced and accepted.  The $480 runs the ordinary law book
     (denominator + steady credits).  3x the signal of the $40 probe, verdict within one settle
     cycle (both families run daily windows).  v6's boot mode executes exactly this split."
                                                                     — note 55, THE DEPLOY PLAN

── THE CAP IS DERIVED, AND IT IS THE RUIN FORMULA INVERTED ─────────────────────────────────
The ordinary book's safety statement is: a z-sigma day costs at most d x C, where d = 0.20 is
the day-stop policy the cluster rail A = C/N is solved from.  The probe's safety statement is
the SAME NUMBER reached from the other direction: its worst possible day — every dollar
fills, every position settles against us, both settle sources die together — is its own size.
So a probe of exactly d x C is exactly as safe as the diversified book, by the diversified
book's own arithmetic, and NO NEW RISK MACHINERY IS NEEDED.

    probe_cap = RUIN_D x C          $120 at C = $600

It is written as that expression and never as 120: at C = $1,000 the probe is $200, and a
literal would have silently made a $1,000 deploy safer-than-stated (and a $300 one
catastrophically looser).  `test_probe` asserts the identity at C = 600.

── WHAT THE PROBE MAY BUY: WINGS AND WALLS ONLY ────────────────────────────────────────────
"NEVER mid-priced fat legs — that's what killed v4, not concentration."  The probe's whole
licence is that its worst case is bounded and its expected fill rate is ~0; a mid-priced leg
has neither property.  So a leg is probe-eligible only when its own collateral price is at or
below `PROBE_WING_MAX_C`, and that edge is not a taste: 1c, 2c, 3c and 4c are the cent buckets
that each clear the calibration table's N_MIN = 300 observations ALONE (`bleed.G_TABLE`), and
5c is the first cent that has to be POOLED to be measurable.  The wing is where the
measurement still resolves single cents; past it we are extrapolating, and the probe is
precisely the thing that must not extrapolate.

── WHY THE PROBE IS EXEMPT FROM THE CLUSTER RAIL, AND ONLY FROM THAT ───────────────────────
$120 across two settle sources is 3x the $21.43 rail, deliberately: "treasuries+gas = two
settle sources, both-die-day = the full $120, priced and accepted."  The rail exists to bound
a z-sigma DAY at d x C; the probe's whole worst case IS d x C, so the rail's own guarantee is
already met by the cap, and stacking both would refuse the experiment Ryan authorised.
EVERYTHING ELSE STILL BINDS on a probe order: the per-strike $1 forfeit cliff, the fill-bleed
viability screen, never-sell, own-orders-only, never-cross, B14, the rate lanes, the
collateral ceiling.  The probe relaxes ONE bound and buys back its guarantee with an equality.

── THE VERDICT INSTRUMENTATION ─────────────────────────────────────────────────────────────
"estimates-feed accrual per probed market logged across reward batches, loudly."  A REWARD
BATCH is an observed CHANGE in the estimates feed's accrual for a probed market — the feed is
the exchange's own truth about what it will pay us, and a change in it is a batch landing.
After `VERDICT_BATCHES` batches the probe states a verdict: PASS if the probed markets have
accrued anything at all, FAIL if two batches have landed and they have not.  The verdict is
LOGGED AND PAGED; it does not gate code, because Ryan's plan runs the $480 book concurrently
and the decision it feeds ("scale to $1k, or rework the thesis") is his, not the bot's.
"""

from . import clusters as CL
from . import config as C
from . import runtime as R


PROBE, BOOK = "probe", "book"
VERDICT_BATCHES = 2                  # note 55 final amendment 5: "2 reward batches"


def probe_cap_usd(capital_usd):
    """`RUIN_D x C` — the module header's derivation, as an expression.  $120 at $600."""
    return float(C.RUIN_D) * max(0.0, float(capital_usd))


def is_probe_family(ticker):
    ck = CL.cluster_of(ticker)
    return any(str(ck).upper().startswith(pfx) for pfx in C.PROBE_FAMILIES)


def is_wing(unit_price_usd):
    """At or below the last cent the calibration table resolves on its own."""
    return int(round(float(unit_price_usd) * 100)) <= int(C.PROBE_WING_MAX_C)


class Probe(object):
    """The boot mode.  Constructed by the engine when `config.PROBE_ARMED`; `None` otherwise,
    and with it `None` the queue is the ordinary book with no probe code on any path."""

    __slots__ = ("capital_usd", "cap_usd", "batches", "last_accrued", "verdict")

    def __init__(self, capital_usd):
        self.capital_usd = float(capital_usd)
        self.cap_usd = probe_cap_usd(capital_usd)
        self.batches = {}                     # ticker -> observed accrual changes
        self.last_accrued = {}                # ticker -> last accrual read
        self.verdict = None
        R.log("probe_armed", capital_usd=round(self.capital_usd, 2),
              probe_cap_usd=round(self.cap_usd, 2),
              book_cap_usd=round(self.capital_usd - self.cap_usd, 2),
              d=C.RUIN_D, wing_max_c=C.PROBE_WING_MAX_C, families=list(C.PROBE_FAMILIES))

    # ── the lane split ──────────────────────────────────────────────────────────────────
    def eligible(self, curve):
        """Wings and walls, in the probe's families, only.

        A WALL is a self-qualifying side — `curve.qualify_q > 0` — and it is priced at the
        floor dial, so it is a wing by price too; the price test therefore covers both, and
        the qualify test is kept only so the log can tell them apart."""
        return is_probe_family(curve.slot.ticker) and is_wing(curve.p)

    def lane_of(self, curve):
        return PROBE if self.eligible(curve) else BOOK

    def room_usd(self, curve, spent_by_lane):
        lane = self.lane_of(curve)
        cap = self.cap_usd if lane == PROBE else (self.capital_usd - self.cap_usd)
        return max(0.0, cap - float((spent_by_lane or {}).get(lane, 0.0)))

    def rail_exempt(self, curve):
        """The probe lane is exempt from the CLUSTER RAIL and from nothing else — see the
        module header: its worst case is already d x C by construction."""
        return self.lane_of(curve) == PROBE

    def clusters(self, slots):
        """The probe's families, as clusters — handed to the queue as multi-market by
        construction.  The quiet classifier cannot license them on day one (it needs 3h x
        horizon of our own pooled exposure and we have none); the probe is the experiment that
        CREATES that evidence, which is why it supplies its own exemption and says so."""
        out = {CL.cluster_of(s.ticker) for s in slots if is_probe_family(s.ticker)}
        if out:
            R.log_once("probe_clusters_exempt", clusters=sorted(out),
                       note="the probe's families are multi-market by construction; the quiet "
                            "classifier cannot license them until the probe has built the tape")
        return out

    # ── the verdict ─────────────────────────────────────────────────────────────────────
    def observe(self, slots):
        """One estimates-feed reading.  Logs per probed market, LOUDLY, and states the verdict
        once `VERDICT_BATCHES` batches have landed."""
        touched = False
        for s in slots:
            if not is_probe_family(s.ticker):
                continue
            acc = float(s.accrued or 0.0)
            prev = self.last_accrued.get(s.ticker)
            if prev is None:
                self.last_accrued[s.ticker] = acc
                R.log("probe_accrual", ticker=s.ticker, cluster=CL.cluster_of(s.ticker),
                      accrued_usd=round(acc, 4), batch=0, first_read=True)
                touched = True
                continue
            if abs(acc - prev) > 1e-9:
                self.batches[s.ticker] = self.batches.get(s.ticker, 0) + 1
                self.last_accrued[s.ticker] = acc
                R.log("probe_accrual", ticker=s.ticker, cluster=CL.cluster_of(s.ticker),
                      accrued_usd=round(acc, 4), delta_usd=round(acc - prev, 4),
                      batch=self.batches[s.ticker])
                touched = True
        if touched:
            self._maybe_verdict()
        return self.verdict

    def _maybe_verdict(self):
        if self.verdict is not None:
            return
        batches = max(self.batches.values()) if self.batches else 0
        if batches < VERDICT_BATCHES:
            return
        earned = sum(self.last_accrued.values())
        self.verdict = "pass" if earned > 0 else "fail"
        R.log("probe_verdict", verdict=self.verdict, batches=batches,
              accrued_usd=round(earned, 4), markets=len(self.last_accrued),
              probe_cap_usd=round(self.cap_usd, 2))
        R.ntfy("probe_verdict",
               "lip_v6 PROBE %s after %d reward batches: $%.4f accrued across %d markets"
               % (self.verdict.upper(), batches, earned, len(self.last_accrued)))
