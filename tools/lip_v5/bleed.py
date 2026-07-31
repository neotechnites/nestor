"""
lip_v5.bleed — g(p), THE EXPECTED FILL LOSS PER DOLLAR OF COLLATERAL FILLED.

── THE INCIDENT (2026-07-30, live) ────────────────────────────────────────────────────────
The owner's law ranks candidates by CAPITAL NEEDED to earn the target.  Capital only.  When
the old allocator was deleted, its (*) hurdle — which charged `phi x loss-per-fill` against
every rung — went with it, and nothing in the new law replaced it: `law_need`'s `max(1, T)`
charges turnovers as CAPITAL CONSUMPTION (the same dollars re-committed T times) but never as
EV LOSS (the dollars that do not come back).

Capital-per-unit-of-score is minimised by the CHEAPEST contract.  So the queue did exactly
what it was told and slid the book toward the toxic end.  Measured tonight: the resting book
averaged 8.2c — two 300-lot walls at 3c and a rung at 1c — against a held-position average of
12.3c and a design average of ~15c.  The entry band's 6c floor was the only guard, and a
guard on the PRICE of one rung cannot stop a RANKING that prefers cheap:

    "how the fuck did that happen. turn off v5. fix it"   — the owner, 2026-07-30

── THE MEASUREMENT ────────────────────────────────────────────────────────────────────────
`data/calib2.json`, the owner's calibration pull: n = 8,240 SETTLED markets, fields
`mid` (the market's YES mid at the observation) and `res` ("yes"|"no", the settlement).

Posted prices at the cheap end are systematically ABOVE the realised frequency.  Per dollar of
collateral filled at price p the expected permanent loss is

    g(p) = 1 - realised(p) / posted(p),   clamped to [0, 1]

i.e. a fill at a bucket whose posted mean is 1.58% but whose realised win rate is 0.00% loses
100% of that collateral in expectation.  g is a fraction OF THE COLLATERAL, not of the notional,
which is why it multiplies W (dollars at risk) and not q (contracts).

BOTH SIDES.  A market is two positions.  The YES position's price is `mid` and it wins on
res == "yes"; the NO position's price is `1 - mid` and it wins on res == "no".  Each settled
market therefore contributes TWO observations, one to bucket(mid) and one to bucket(1 - mid).
This is not double-counting: the bot quotes both sides, `unit_collateral` already prices a NO
rung at 1 - p, and a NO at 97c-YES *is* a 3c position with a 3c position's bleed.  It is also
the only way the cheap end gets data from the expensive half of the tape.  n below is
observations (16,480 total), not markets.

── BUCKETS ────────────────────────────────────────────────────────────────────────────────
The calibration file's own resolution is HALF A CENT (`mid` takes values .005, .010, .015,
...), so the finest honest bucket is ONE CENT:

    bucket(p) = floor(p x 100 + 0.5)      -> .005 and .010 land in 1c;  .015 and .020 in 2c

This reproduces the owner's cited headline exactly (1c: posted 0.60%, realised 0.03%;
2c: posted 1.57%, realised 0.00%, n = 765 markets = 1,368 side-observations), which is the
check that the bucket edge is the one that was measured.

NOT `scan.phi_bucket`.  That function's DECILES are the codebase's existing price-bucket
scheme, but they are the wrong instrument here and using them would erase the finding: one
decile covers 1c..9c, over which g runs from 0.95 to 0.38, and pooling it would charge a 9c
rung the 1c rung's bleed and vice-versa.  `phi_bucket` exists to BORROW a fill hazard from a
price neighbourhood and its own docstring marks the 10c width UNDERIVED; g is measured
directly at 1c resolution, so it has no reason to inherit that width.  (Keeping this module
off scan.py also keeps the phi-shrinkage branch's edits to that file conflict-free.)

── THE INSUFFICIENT-n RULE (stated, as required) ──────────────────────────────────────────
A cent bucket at 30c holds ~55 observations; its realised rate carries a +-6pp standard error,
which is larger than the whole effect being measured.  Two rules, in order:

  (R1) N_MIN POOLING.  Sweep cent buckets from 1c upward, accumulating into a BAND until the
       band holds N_MIN = 300 observations, then close it and start the next.  A band's g is
       computed from the POOLED posted and realised totals, never from an average of ratios.
       Every band therefore reports its own n and no bucket ever inherits a value from a
       neighbourhood it was not pooled with.  The trailing remainder (< N_MIN) is merged into
       the last closed band.  N_MIN = 300 puts the realised-rate standard error at or below
       ~2.9pp everywhere, which is small against the g's it must separate.
       => the cheap end, where the observations are, keeps 1c resolution (1c and 2c each
          clear N_MIN alone); the thin middle widens until it is measurable.

  (R2) ISOTONIC (pool-adjacent-violators), NON-INCREASING IN PRICE, APPLIED FROM 3c UP.
       The bias being measured is a longshot premium that decays as price rises; an inversion
       between adjacent bands (10-13c reads 0.269, 14-18c reads 0.359) is noise, not a
       finding, and left alone it would charge a 15c rung MORE than an 11c rung.  PAVA pools
       violating neighbours BY THEIR TOTALS — the pooled block's g is `1 - sum(wins)/
       sum(posted)`, the maximum-likelihood shared g under the monotone prior, i.e. exactly
       what the block would have read had it been cut as one band.  It touches nothing that
       is already ordered.  (An n-weighted mean of the two RATIOS is a different and slightly
       lower number — 0.3396 against 0.3508 on the 10-28c block.  See the note in
       derive_g_table: the totals form is both the correct one and the conservative one.)
       EXEMPTION, 1c AND 2c: the monotone prior is about mispricing decaying with price, and
       at the exchange's MINIMUM TICK price cannot decay further — 1c and 2c are both pinned
       against the tick floor, both read ~1.0, and their ordering (0.948 vs 1.000) is one
       winning market out of 3,205.  Merging them would destroy the two values the owner
       quotes.  They are carried raw; PAVA runs over 3c..99c.

  DIRECTION OF ERROR (mirror): g too HIGH refuses a rung that would have paid — bounded, we
  lose the credit of one rung and the next-best is funded instead.  g too LOW funds a rung
  that bleeds — that is tonight, unbounded in dollars until someone looks at the book.  Where
  the rules leave a choice, take the higher g.

── THE TABLE ──────────────────────────────────────────────────────────────────────────────
Derived by `derive_g_table()` below, which re-runs the whole derivation from data/calib2.json.
`test_bleed` re-derives and asserts EVERY row against these constants, so a corrupted
derivation cannot ship silently and a re-pull that moves the tape shows up as a red test.

    band      n      posted   realised    g
    1c      3205     0.0060    0.0003    0.9484
    2c      1368     0.0158    0.0000    1.0000
    3c       447     0.0269    0.0089    0.6669
    4c       305     0.0377    0.0131    0.6523
    5-6c     353     0.0543    0.0283    0.4785
    7-9c     358     0.0806    0.0503    0.3763
    10-28c  1305     0.1865    0.1211    0.3508   (PAVA pool of 10-13/14-18/19-23/24-28)
    29-34c   303     0.3129    0.2376    0.2406
    35-50c   610     0.4186    0.3738    0.1071   (PAVA pool of 35-41/42-50)
    51-99c  5691     0.8806    0.9155    0.0000   (realised >= posted everywhere; clamped)

Because PAVA pools by TOTALS, every row above is exactly `1 - realised/posted` of its own
band — the table can be read straight off the tape with no reference to how the bands were
cut.  `test_bleed.test_every_bands_g_is_its_own_pooled_totals` is that invariant.

NOTE, FLAGGED: the brief said the gap is "~0 by ~15-20c".  The file does not say that.  Pooled
10-28c is posted 18.65% against realised 12.11% on 1,305 observations (SE of the realised rate
0.9pp) — a six-point gap at better than 6 sigma, and it persists to ~50c.  The prose is the
owner's impression; the file is the measurement; the file wins and the discrepancy is reported
rather than smoothed.  The consequence is that g at 15c is 0.35, not 0, so a 15c rung is
charged a real bleed too — it simply loses to nothing at 3c, which is the property that
matters and which `test_bleed`/`test_law` pin.
"""

import json
import math
import os


N_MIN = 300                      # (R1) — observations per band before it closes
CALIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "calib2.json")

# (lo_cent, hi_cent inclusive, g, n) — code-generated by derive_g_table(), see module header.
G_TABLE = (
    (1, 1, 0.9484, 3205),
    (2, 2, 1.0000, 1368),
    (3, 3, 0.6669, 447),
    (4, 4, 0.6523, 305),
    (5, 6, 0.4785, 353),
    (7, 9, 0.3763, 358),
    (10, 28, 0.3508, 1305),
    (29, 34, 0.2406, 303),
    (35, 50, 0.1071, 610),
    (51, 99, 0.0000, 5691),
)


def price_bucket(p):
    """The calibration's own resolution: one cent, `floor(p x 100 + 0.5)`, clamped to [1, 99].

    Clamping is not cosmetic.  A sub-half-cent price is not tradeable (1c is the minimum tick)
    but it can arrive as a mid, and it belongs at the WORST bucket, not at a bucket-0 hole that
    would return g = 0 and re-open the exact hole this module closes.
    """
    return min(99, max(1, int(math.floor(float(p) * 100.0 + 0.5))))


def g_for_price(p):
    """g(p) — expected permanent loss per DOLLAR OF COLLATERAL filled at collateral price p.

    `p` is the collateral price of the position, i.e. `runtime.unit_collateral(side, price)`:
    a NO rung against a 97c YES is a 3c position and is charged 3c's bleed.  Callers must pass
    the side-corrected price; `Need.unit_usd` already is one.
    """
    b = price_bucket(p)
    for lo, hi, g, _n in G_TABLE:
        if lo <= b <= hi:
            return g
    return 0.0                                       # unreachable: the table covers 1..99


def bleed_usd(size_usd, turnovers, p):
    """EXPECTED PERMANENT LOSS over the horizon for `size_usd` of collateral resting at price
    `p` and expected to turn over `turnovers` times.

        bleed = W x T x g(p)

    T (not max(1, T)) on purpose: `max(1, T)` in `law_need` is a CAPITAL bound — the dollars
    have to be there even if the lot never fills — but a lot that never fills never LOSES.
    The bleed is charged per expected FILL, so it vanishes with the fill rate.  This is the
    term's self-limiting property, and it is what makes the oversize path safe (law_order_q
    rule 3 only oversizes at T <= 1, and a measured-low-phi rung has T ~ 0).
    """
    return max(0.0, float(size_usd)) * max(0.0, float(turnovers)) * g_for_price(p)


# =============================================================================================
# THE DERIVATION, EXECUTABLE.  `test_bleed` runs this against data/calib2.json and compares
# every row to G_TABLE above — that is the anti-corruption check: change the arithmetic here
# and the pinned constants fail; re-pull the tape and the diff is visible.
# =============================================================================================
def _observations(rows):
    """One settled market -> TWO side-observations (module header, "BOTH SIDES").
    Yields (collateral_price, won)."""
    for r in rows:
        m = float(r["mid"])
        res = str(r["res"]).lower()
        yield m, (res == "yes")
        yield 1.0 - m, (res == "no")


def derive_g_table(rows=None, path=None, n_min=N_MIN):
    """Re-derive G_TABLE from the calibration file.  Returns the same shape as G_TABLE."""
    if rows is None:
        with open(path or CALIB_PATH) as fh:
            rows = json.load(fh)
    cents = {}                                        # cent -> [sum_posted, n, wins]
    for price, won in _observations(rows):
        b = int(math.floor(price * 100.0 + 0.5))
        if b < 1 or b > 99:
            continue                                  # outside the legal price range
        acc = cents.setdefault(b, [0.0, 0, 0])
        acc[0] += price
        acc[1] += 1
        acc[2] += 1 if won else 0
    # (R1) sweep upward, close a band at N_MIN observations.
    bands, cur = [], [None, None, 0.0, 0, 0]          # lo, hi, sum_posted, n, wins
    for b in range(1, 100):
        sp, n, w = cents.get(b, (0.0, 0, 0))
        if cur[0] is None:
            cur[0] = b
        cur[1], cur[2], cur[3], cur[4] = b, cur[2] + sp, cur[3] + n, cur[4] + w
        if cur[3] >= n_min:
            bands.append(cur)
            cur = [None, None, 0.0, 0, 0]
    if cur[0] is not None and bands:                  # trailing remainder joins the last band
        last = bands[-1]
        last[1], last[2], last[3], last[4] = cur[1], last[2] + cur[2], last[3] + cur[3], \
            last[4] + cur[4]
    elif cur[0] is not None:
        bands.append(cur)

    def _g(sum_posted, n, wins):
        if n <= 0 or sum_posted <= 0.0:
            return 1.0                                # no evidence at a price ⇒ worst case
        return max(0.0, min(1.0, 1.0 - (float(wins) / n) / (sum_posted / n)))

    # (R2) PAVA, non-increasing in price, over the bands starting at 3c (tick-floor exemption
    # for 1c/2c — see header).  A VIOLATING PAIR IS POOLED BY ITS TOTALS, not by an n-weighted
    # mean of the two ratios.
    #
    #   REVIEWER'S NOTE, 2026-07-30 night, ADOPTED.  The first cut merged at
    #   (g1 n1 + g2 n2)/(n1 + n2), which gave the 10-28c block 0.3396 where re-pooling its own
    #   posted and realised totals gives 0.3508 — the weighted mean of ratios is not the ratio
    #   of the sums, and the gap went the WRONG WAY against this module's own stated mirror
    #   ("where the rules leave a choice, take the higher g").  Pooling the totals is also the
    #   principled form: under the monotone prior a violating block is a set of observations
    #   believed to share ONE g, and the maximum-likelihood estimate of that shared g is
    #   1 - (sum wins)/(sum posted), i.e. exactly what the band would read if it had been cut
    #   as one band in the first place.  The n-weighted mean is an approximation to it that
    #   happens to be biased toward the arm with the smaller posted prices.  Adopted: it is
    #   both more correct and more conservative, so there is no trade to weigh.
    head = [list(b) for b in bands if b[1] <= 2]
    tail = [list(b) for b in bands if b[1] > 2]
    i = 0
    while i < len(tail) - 1:
        if _g(*tail[i][2:]) < _g(*tail[i + 1][2:]) - 1e-12:
            a, b = tail[i], tail[i + 1]
            tail[i:i + 2] = [[a[0], b[1], a[2] + b[2], a[3] + b[3], a[4] + b[4]]]
            i = max(0, i - 1)                         # re-check backwards, PAVA proper
        else:
            i += 1
    # COALESCE adjacent bands that agree on g.  PAVA only pools STRICT violations, so the
    # whole clamped-to-zero half of the axis (51c up, where realised >= posted everywhere)
    # would otherwise ship as 11 identical rows.  Coalescing changes no value; it makes the
    # table say what it means — "above 50c the tape shows no overpricing at all" — in one line.
    out = []
    for lo, hi, sp, n, w in head + tail:
        g = _g(sp, n, w)
        if out and abs(out[-1][2] - g) < 5e-5:
            out[-1][1], out[-1][3] = hi, out[-1][3] + n
        else:
            out.append([lo, hi, g, n])
    return tuple((lo, hi, round(g, 4), n) for lo, hi, g, n in out)
