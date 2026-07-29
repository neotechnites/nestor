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


class TestShedGeometry(LipTestCase):
    def test_shed_side_is_the_opposing_slot(self):
        """v1 §5.4 / D4: a YES ask IS a NO bid — the shed is the opposing slot's quote."""
        self.assertEqual(Q.shed_side("yes"), "ask")
        self.assertEqual(Q.shed_side("no"), "bid")

    def test_held_leg(self):
        self.assertEqual(Q.held_leg_of(5), "yes")
        self.assertEqual(Q.held_leg_of(-5), "no")

    def test_shed_price_joins_and_never_crosses(self):
        self.assertEqual(Q.shed_price("yes", 0.40, 0.42), 0.42)   # join the ask queue
        self.assertEqual(Q.shed_price("no", 0.40, 0.42), 0.40)    # join the bid queue

    def test_a_crossed_or_locked_book_refuses_to_price(self):
        """Joining a crossed book would in fact TAKE — the G6-off guarantee would be broken
        by arithmetic, not by intent."""
        self.assertIsNone(Q.shed_price("yes", 0.42, 0.42))
        self.assertIsNone(Q.shed_price("yes", 0.43, 0.42))
        self.assertIsNone(Q.shed_price("yes", None, 0.42))
        self.assertIsNone(Q.shed_price("yes", 0.40, None))

    def test_shed_qty_clamps_at_net_and_never_flips(self):
        """C8: a 40-lot shed against 20 held is a fresh opposite position wearing a shed's
        name."""
        self.assertEqual(Q.shed_qty(20.0, target=40.0), 20)
        self.assertEqual(Q.shed_qty(-20.0, target=40.0), 20)
        self.assertEqual(Q.shed_qty(20.0, target=5.0), 5)
        self.assertEqual(Q.shed_qty(0.6), 0)                      # dust cannot trade


class TestWholeSecond(LipTestCase):
    def test_same_integer_second_is_refused(self):
        self.assertTrue(Q.same_second(100.9, 100.1))
        self.assertFalse(Q.same_second(101.0, 100.9))
        self.assertFalse(Q.same_second(101.0, None))


if __name__ == "__main__":
    unittest.main()
