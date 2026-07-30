"""The PURE half of the requoting stage (spec §4 / v1 §4.3-4.5) — triggers, shed geometry,
the whole-second policy.  These are v4's prod-proven shapes; the tests pin the shapes so a
port drift fails here rather than on the wire."""

import unittest

from .. import config as C, quote as Q
from .base import LipTestCase


class TestTriggers(LipTestCase):
    def _trig(self, **kw):
        args = dict(our_price_c=40, best_price_c=40, remaining=10, target_q=10,
                    S_now=50.0, S_ref=50.0, qualifies_now=True, qualifies_ref=True,
                    resting_age_s=120.0, since_resync_s=0.0)
        args.update(kw)
        return Q.requote_triggers(**args)

    def test_at_best_and_stable_fires_nothing(self):
        self.assertEqual(self._trig(), [])

    def test_a_off_best(self):
        self.assertIn(Q.TRIG_OFF_BEST, self._trig(our_price_c=39))

    def test_b_refill_below_half_of_target(self):
        self.assertIn(Q.TRIG_REFILL, self._trig(remaining=4.9))
        self.assertNotIn(Q.TRIG_REFILL, self._trig(remaining=5.0))

    def test_c_S_moved_a_quarter(self):
        """(c) still fires on a >25% rival-score move -- but only when the requote would
        CHANGE something.  At the touch with no refill due there is nothing to alter, and the
        no-change suppression removes it (see test_no_change_suppression_*)."""
        self.assertIn(Q.TRIG_S_MOVED, self._trig(S_now=63.0, our_price_c=39))
        self.assertNotIn(Q.TRIG_S_MOVED, self._trig(S_now=60.0, our_price_c=39))

    def test_d_qualification_flip(self):
        self.assertIn(Q.TRIG_QUALIFIES, self._trig(qualifies_now=False))

    def test_e_safety_resync_at_60s(self):
        """Same conditioning as (c): the staleness timer fires, but a resync that finds nothing
        to change must not cancel-and-replace identically."""
        self.assertIn(Q.TRIG_RESYNC,
                      self._trig(since_resync_s=C.SAFETY_RESYNC_S, our_price_c=39))
        self.assertNotIn(Q.TRIG_RESYNC, self._trig(since_resync_s=10.0, our_price_c=39))


class TestNoChangeSuppression(LipTestCase):
    """MEASURED: median order lifetime 1.9 SECONDS, and 73.9% of 4,267 re-posts were at the SAME
    PRICE as the post they replaced.  The book had an order actually resting 10.6% of the time,
    and accrual is proportional to presence, so that alone capped earnings at a tenth of the same
    capital's potential before any other decision was made.

    A requote that alters neither price nor size cannot alter score, and it is not free: it
    surrenders queue position, opens a gap in a once-per-second sampled metric, and spends a rate
    round trip."""

    def _trig(self, **kw):
        args = dict(our_price_c=40, best_price_c=40, remaining=10, target_q=10,
                    S_now=50.0, S_ref=50.0, qualifies_now=True, qualifies_ref=True,
                    resting_age_s=120.0, since_resync_s=0.0)
        args.update(kw)
        return Q.requote_triggers(**args)

    def test_S_moving_alone_does_NOT_requote_us_at_the_same_price(self):
        self.assertEqual(self._trig(S_now=1000.0), [])

    def test_the_staleness_timer_alone_does_NOT_requote_us_at_the_same_price(self):
        self.assertEqual(self._trig(since_resync_s=10 * C.SAFETY_RESYNC_S), [])

    def test_a_PRICE_change_is_never_suppressed(self):
        """(a) is the whole point of requoting -- being off best halves our score per tick."""
        self.assertIn(Q.TRIG_OFF_BEST, self._trig(our_price_c=39,
                                                  since_resync_s=10 * C.SAFETY_RESYNC_S))

    def test_a_SIZE_change_is_never_suppressed(self):
        """(b) refill: a partially-filled order is smaller than the target, so replacing it
        does change our score."""
        self.assertIn(Q.TRIG_REFILL, self._trig(remaining=1.0))
        self.assertIn(Q.TRIG_S_MOVED, self._trig(remaining=1.0, S_now=1000.0))

    def test_a_QUALIFICATION_flip_is_never_suppressed(self):
        """(d): the side appeared or vanished, which changes whether we score at all."""
        self.assertIn(Q.TRIG_QUALIFIES, self._trig(qualifies_now=False))

    def test_min_resting_life_suppresses_all_but_a_and_d(self):
        """§4.4 anti-gaming P1: a voluntary requote inside 30 s is indistinguishable from
        cancel-on-approach.  Trigger (a) overrides — a genuine price move is not a dodge."""
        got = self._trig(resting_age_s=5.0, our_price_c=39, remaining=1.0, S_now=100.0,
                         qualifies_now=False, since_resync_s=100.0)
        self.assertEqual(sorted(got), sorted([Q.TRIG_OFF_BEST, Q.TRIG_QUALIFIES]))

    def test_after_min_life_everything_fires(self):
        got = self._trig(resting_age_s=30.0, our_price_c=39, remaining=1.0, S_now=100.0,
                         qualifies_now=False, since_resync_s=100.0)
        self.assertEqual(len(got), 5)


class TestTheShedGeometryIsGone(LipTestCase):
    """LAW CHANGE (owner decision, 2026-07-30): "it's either running and placing orders, or
    it's not running."  THE BOT NEVER SELLS.

    This class replaces `TestShedGeometry`, which asserted the arithmetic of leaving a
    position: `shed_side` (a held YES sells through the ask slot), `held_leg_of`,
    `shed_price` (join the OPPOSING best, refuse a crossed book) and `shed_qty` (clamp at
    |net| so a shed cannot flip).  Every one of those functions is deleted, and the test is
    now that they are ABSENT — because their existence is what let `engine` re-derive an exit
    price.  A helper nobody calls today is a helper somebody calls next quarter.

    What is NOT gone, and is asserted below so the two can never be confused again: ask-side
    QUOTING.  An ask posts NO-side collateral as an OPENING maker quote and earns the NO half
    of the pool.  `would_cross` still guards it on both sides."""

    def test_no_exit_geometry_survives_in_the_quote_module(self):
        for gone in ("shed_side", "shed_price", "shed_qty", "held_leg_of"):
            self.assertFalse(hasattr(Q, gone),
                             "quote.%s survived: the exit geometry is back" % gone)

    def test_ask_side_quoting_is_untouched(self):
        """The ask is the NO half of the pool, not a sale.  `would_cross` protects it exactly
        as it protects the bid: an ask at or below the yes-bid would take."""
        self.assertTrue(Q.would_cross("ask", 0.40, 0.40, 0.42))   # would take the 0.40 bid
        self.assertFalse(Q.would_cross("ask", 0.42, 0.40, 0.42))  # rests at its own best
        self.assertFalse(Q.would_cross("ask", 0.41, 0.40, 0.42))  # inside the spread, rests


class TestWholeSecond(LipTestCase):
    def test_same_integer_second_is_refused(self):
        self.assertTrue(Q.same_second(100.9, 100.1))
        self.assertFalse(Q.same_second(101.0, 100.9))
        self.assertFalse(Q.same_second(101.0, None))


if __name__ == "__main__":
    unittest.main()
