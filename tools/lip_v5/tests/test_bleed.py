"""g(p), THE FILL-BLEED CALIBRATION — `lip_v5.bleed`'s own tests.

The 2026-07-30-night incident (resting book at 8.2c average, two 300-lot walls at 3c) is
adjudicated in `test_law.TestTheFillBleedTerm`.  THIS file guards the MEASUREMENT the
adjudication rests on: the table's derivation from `data/calib2.json`, its bucket edges, and
the two values the owner quotes.

MUTATION CONTRACT.  Corrupt the derivation in any way that moves a number — change the bucket
edge, drop the NO side, average the ratios instead of pooling the totals, skip the isotonic
step, move N_MIN, forget the [0,1] clamp — and `test_table_is_reproducible_from_the_file` or a
pinned-value test fails.  The constants and the code that produced them are checked against
each other on every run; neither can drift alone.
"""

import json
import math
import unittest

from .. import bleed as B
from .base import LipTestCase


class TestTheGTableIsTheMeasurement(LipTestCase):

    def test_table_is_reproducible_from_the_file(self):
        """THE ANTI-CORRUPTION CHECK.  G_TABLE is code-generated; re-run the generator against
        the shipped calibration file and every row must come back identical — band edges, g
        to 4 decimals, and n.  This is what makes the constants readable as a measurement
        rather than as numbers somebody typed."""
        self.assertEqual(B.derive_g_table(), B.G_TABLE)

    def test_the_owners_two_headline_buckets(self):
        """The owner's cited calibration, verbatim: "1c posted 0.60% realized 0.03%; 2c posted
        1.57% realized 0.00%".  Reproducing BOTH sides of that from the file is the check that
        the bucket EDGE is the one that was measured (floor(p*100 + 0.5), so the file's .005
        and .010 mids are the 1c bucket and .015/.020 are the 2c bucket).  If this fails, the
        table is describing a different partition of the price axis than the owner measured."""
        with open(B.CALIB_PATH) as fh:
            rows = json.load(fh)
        acc = {}
        for price, won in B._observations(rows):
            b = B.price_bucket(price)
            if b > 2:
                continue
            a = acc.setdefault(b, [0.0, 0, 0])
            a[0] += price
            a[1] += 1
            a[2] += 1 if won else 0
        one, two = acc[1], acc[2]
        self.assertEqual(one[1], 3205)                       # 1c: side-observations
        self.assertAlmostEqual(one[0] / one[1], 0.0060, places=4)      # posted 0.60%
        self.assertAlmostEqual(one[2] / one[1], 0.0003, places=4)      # realised 0.03%
        self.assertEqual(two[1], 1368)                       # 2c: 765 markets x ... see below
        self.assertAlmostEqual(two[0] / two[1], 0.0158, places=4)      # posted ~1.57%
        self.assertEqual(two[2], 0)                                    # realised 0.00%

    def test_pinned_bucket_values(self):
        """PINNED.  The load-bearing rows, by hand, from the table in bleed.py's header.
        A derivation that silently changes any of these changes which rungs the allocator
        funds; it must not be possible to do that and stay green."""
        self.assertAlmostEqual(B.g_for_price(0.01), 0.9484, places=4)   # n = 3,205
        self.assertAlmostEqual(B.g_for_price(0.02), 1.0000, places=4)   # n = 1,368
        self.assertAlmostEqual(B.g_for_price(0.03), 0.6669, places=4)   # n =   447
        self.assertAlmostEqual(B.g_for_price(0.04), 0.6523, places=4)   # n =   305
        self.assertAlmostEqual(B.g_for_price(0.06), 0.4785, places=4)   # n =   353  (5-6c)
        self.assertAlmostEqual(B.g_for_price(0.09), 0.3763, places=4)   # n =   358  (7-9c)
        self.assertAlmostEqual(B.g_for_price(0.15), 0.3508, places=4)   # n = 1,305 (10-28c)
        self.assertAlmostEqual(B.g_for_price(0.40), 0.1071, places=4)   # n =   610 (35-50c)
        self.assertAlmostEqual(B.g_for_price(0.60), 0.0000, places=4)   # n = 5,691 (51-99c)

    def test_g_is_non_increasing_in_price(self):
        """(R2), the monotone prior the isotonic step enforces: the longshot premium decays as
        price rises.  Without PAVA the raw bands invert (10-13c reads 0.269 against 14-18c's
        0.359) and an 11c rung would be charged LESS than a 15c rung — noise deciding money."""
        gs = [B.g_for_price(c / 100.0) for c in range(3, 100)]      # 3c up: PAVA's domain
        for a, b in zip(gs, gs[1:]):
            self.assertGreaterEqual(a + 1e-12, b)
        # THE TICK-FLOOR EXEMPTION, pinned as the deliberate exception it is: 1c reads BELOW
        # 2c (0.9484 vs 1.0000 — one winning market out of 3,205) and is carried raw, because
        # at the minimum tick price cannot decay further and the monotone prior does not
        # apply.  Merging them would erase the two values the owner quotes.
        self.assertLess(B.g_for_price(0.01), B.g_for_price(0.02))
        self.assertGreater(B.g_for_price(0.01), 0.9)

    def test_every_legal_price_has_a_g_and_the_cheap_end_is_the_worst(self):
        """No holes: 1c..99c all resolve inside a band, all in [0, 1], and sub-tick prices
        clamp UP to the 1c bucket rather than falling through to g = 0 (which would re-open
        the exact hole this module closes)."""
        for c in range(1, 100):
            g = B.g_for_price(c / 100.0)
            self.assertTrue(0.0 <= g <= 1.0)
        self.assertEqual(B.price_bucket(0.0001), 1)
        self.assertEqual(B.price_bucket(0.004), 1)       # below the tick ⇒ worst bucket
        self.assertEqual(B.price_bucket(2.0), 99)
        self.assertAlmostEqual(B.g_for_price(0.0001), B.g_for_price(0.01), places=9)

    def test_both_sides_are_counted(self):
        """A market is TWO positions, and the pull is one row per market.  Dropping the NO
        side would halve n everywhere and would gut the bands the walls live in."""
        with open(B.CALIB_PATH) as fh:
            rows = json.load(fh)
        self.assertEqual(sum(1 for _ in B._observations(rows)), 2 * len(rows))
        self.assertEqual(len(rows), 8240)
        # The NO side is where most of the mid-range evidence comes from: at 3c the file
        # holds 175 YES observations and 272 NO ones, so dropping it would cut the 3c bucket
        # by 61% and leave it under N_MIN — the band would widen and 3c would inherit a
        # cheaper neighbourhood's g, which is the direction that funds walls.
        yes3 = sum(1 for r in rows if B.price_bucket(r["mid"]) == 3)
        no3 = sum(1 for r in rows if B.price_bucket(1.0 - r["mid"]) == 3)
        self.assertEqual((yes3, no3), (175, 272))
        self.assertEqual(yes3 + no3, 447)                # == the 3c band's n in G_TABLE
        # 1c is the exception and it is worth stating: the file's richest mid is 98.5c, whose
        # NO side lands at 1.5c, so NOTHING reaches the 1c bucket from the expensive half.
        # 1c's n = 3,205 is all YES-side, and that is a fact about the pull, not a bug.
        self.assertEqual(sum(1 for r in rows if B.price_bucket(1.0 - r["mid"]) == 1), 0)

    def test_bleed_usd_is_linear_in_size_and_vanishes_with_turnovers(self):
        """`bleed = W x T x g`, with the BARE T (not max(1, T)): a lot that never fills never
        loses.  This is the term's self-limiting property and the reason the oversize path is
        safe on measured-low phi — delete the `max(0.0, turnovers)` guard's honesty here and
        a zero-phi rung would be charged as if it traded."""
        self.assertAlmostEqual(B.bleed_usd(9.0, 1.1, 0.03), 9.0 * 1.1 * 0.6669, places=9)
        self.assertAlmostEqual(B.bleed_usd(9.0, 0.0, 0.03), 0.0, places=12)
        self.assertAlmostEqual(B.bleed_usd(18.0, 1.1, 0.03),
                               2.0 * B.bleed_usd(9.0, 1.1, 0.03), places=9)
        self.assertAlmostEqual(B.bleed_usd(9.0, 1.1, 0.60), 0.0, places=12)

    def test_a_no_rung_is_charged_its_own_collateral_price(self):
        """A NO position against a 97c YES IS a 3c position and bleeds like one.  Callers pass
        `unit_collateral`, which is already side-corrected; the table is indexed on that."""
        self.assertAlmostEqual(B.g_for_price(1.0 - 0.97), B.g_for_price(0.03), places=9)
        self.assertNotAlmostEqual(B.g_for_price(0.97), B.g_for_price(0.03), places=3)

    def test_every_bands_g_is_its_own_pooled_totals(self):
        """REVIEWER NOTE (a), 2026-07-30 night, ADOPTED.  PAVA merges violating neighbours BY
        THEIR TOTALS, not at an n-weighted mean of the two ratios, so every shipped row is
        exactly `1 - realised/posted` of its own band and the table can be read straight off
        the tape.  The first cut used the weighted mean, which gave the 10-28c block 0.3396
        where its own totals say 0.3508 — lower, and therefore against this module's own
        stated mirror ("where the rules leave a choice, take the higher g").  The totals form
        is also the maximum-likelihood shared g under the monotone prior, so there was no
        trade to weigh.

        MUTATION: restore the n-weighted mean and this fails on the 10-28c and 35-50c rows
        (the only two blocks PAVA actually pools)."""
        with open(B.CALIB_PATH) as fh:
            rows = json.load(fh)
        acc = {}
        for price, won in B._observations(rows):
            # the derivation's own inclusion rule: the RAW cent bucket, dropping anything
            # outside 1c..99c (0.995 is not a legal price).  `price_bucket` CLAMPS instead,
            # because a lookup must never fall through a hole; the two differ only outside
            # the tradeable range, and both give g = 0 up there.
            b = int(math.floor(price * 100.0 + 0.5))
            if b < 1 or b > 99:
                continue
            acc.setdefault(b, [0.0, 0, 0])
            acc[b][0] += price
            acc[b][1] += 1
            acc[b][2] += 1 if won else 0
        for lo, hi, g, n in B.G_TABLE:
            sp = sum(acc[b][0] for b in range(lo, hi + 1) if b in acc)
            ct = sum(acc[b][1] for b in range(lo, hi + 1) if b in acc)
            wn = sum(acc[b][2] for b in range(lo, hi + 1) if b in acc)
            self.assertEqual(ct, n, "band %d-%dc: n disagrees with the file" % (lo, hi))
            own = max(0.0, min(1.0, 1.0 - (float(wn) / ct) / (sp / ct)))
            self.assertAlmostEqual(g, own, places=4,
                                   msg="band %d-%dc: g is not its own pooled totals"
                                       % (lo, hi))
        self.assertAlmostEqual(B.g_for_price(0.15), 0.3508, places=4)   # not 0.3396

    def test_n_min_pooling_rule_is_what_sets_the_band_widths(self):
        """(R1) stated as a test: every band carries at least N_MIN observations (the last
        absorbs the remainder), so no g in the table is a small-sample artefact."""
        for lo, hi, _g, n in B.G_TABLE:
            self.assertGreaterEqual(n, B.N_MIN, "band %d-%dc is under N_MIN" % (lo, hi))
        self.assertEqual(B.G_TABLE[0][0], 1)
        self.assertEqual(B.G_TABLE[-1][1], 99)
        for (_lo, hi, _g, _n), (lo2, _h2, _g2, _n2) in zip(B.G_TABLE, B.G_TABLE[1:]):
            self.assertEqual(lo2, hi + 1)                 # contiguous, no gaps, no overlap


if __name__ == "__main__":
    unittest.main()
