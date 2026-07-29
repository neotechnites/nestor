"""THE INERTNESS GUARD for config.py's (★★) fate block.

Why this file exists.  On 2026-07-28 a six-rule redesign of v5's objective was derived, and a
backtest against our own tape (66 settled markets, 27,181 one-minute bars, pipeline validated
to $0.000) then REFUTED the core of it: the full design lost $23.70 (t = -2.24), and it lost
MORE with the top three markets removed ($28.70, t = -2.92).  The resting-sell-leg rule alone
was worth -$40.30 against doing nothing.

So the derivation was committed and the BEHAVIOUR was not.  That is only safe if "staged-inert"
is a fact rather than a comment, because the failure mode is specific and has happened before in
this codebase: a constant sits in config.py carrying a persuasive derivation, someone later
wires it up without reading the measurement beside it, and a refuted rule ships wearing the
authority of a reviewed constant.  guards.py's own header names the general form -- "a guard
with no call site is not a guard, it is a comment with a unit test".  This is the inverse and it
is the dangerous direction: a comment with a unit test that becomes a guard by accident.

The assertion is mechanical: these names must appear in config.py and NOWHERE else in the
package.  If a future change genuinely arms one of them, this test fails and its author has to
come here, read the measurement, and delete the name from the list deliberately.
"""

import os
import re
import unittest

from .. import config as C
from .base import LipTestCase


# Every name defined by the (★★) block.  Each is derived; each is unmeasured or refuted; none
# may have a call site.  Deleting a name from this list is how a rule gets ARMED, and it must
# be done in the same commit that wires it and cites the tape that justifies it.
STAGED_INERT_NAMES = (
    "BAND_MARKET_MIN_C",
    "BAND_MARKET_MAX_C",
    "BAND_OUR_LEG_MIN_C",
    "BAND_OUR_LEG_MAX_C",
    "DAILIES_ONLY_WINDOW_MULT",
    # FREE_RIDE_ONLY was ARMED 2026-07-29 and removed from this tuple in the same commit, per
    # the instruction in test_no_staged_constant_has_a_call_site.  Its mirror's fear -- "no
    # market qualifies without us" -- was measured and refuted: 5,681 of 5,695 live book-sides
    # reach target_size on rival size alone.  See TestFreeRideIsArmed below.
)

# The two rules the tape measured as harmful.  These must not exist AT ALL -- an unused constant
# is exactly how a dormant guard gets switched on later by someone who reads the derivation and
# not the measurement, so the refutation is enforced by ABSENCE, not by a False default.
REFUTED_NAMES = (
    "JOINT_SUM_MAX_C",       # measured effect ~zero: no pair in the tape exceeded $1.00
    "SELL_LEG_MARKUP_C",     # measured effect -$40.30; netted 0 contracts against 230 without
)


def _package_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _module_sources(skip=("config.py",)):
    """Every .py in the package except the ones named, as (filename, text).  Tests are included
    deliberately: a fixture that reads one of these names is a rule leaking into the suite's
    idea of correct behaviour, which is how a green suite launders an unshipped decision."""
    out = []
    root = _package_dir()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            if not fn.endswith(".py") or fn in skip:
                continue
            path = os.path.join(dirpath, fn)
            if os.path.abspath(path) == os.path.abspath(__file__):
                continue                      # this file names them all, by construction
            with open(path, "r") as fh:
                out.append((os.path.relpath(path, root), fh.read()))
    return out


class TestTheFateBlockIsInert(LipTestCase):
    """The (★★) block changes no behaviour.  Asserted, not asserted-in-a-comment."""

    def test_every_staged_name_exists_so_the_list_cannot_rot(self):
        """The mirror of the inertness test: a name that has been RENAMED away would pass an
        absence check vacuously, and the guard would be silently gone."""
        for name in STAGED_INERT_NAMES:
            self.assertTrue(hasattr(C, name),
                            "%s vanished from config -- update this list deliberately" % name)

    def test_no_staged_constant_has_a_call_site(self):
        for name in STAGED_INERT_NAMES:
            pat = re.compile(r"\b%s\b" % re.escape(name))
            for relpath, text in _module_sources():
                self.assertIsNone(
                    pat.search(text),
                    "%s is referenced in %s -- the (★★) block is meant to be "
                    "STAGED-INERT.  Read the measurement beside it in config.py before arming "
                    "it, then remove it from STAGED_INERT_NAMES in the same commit."
                    % (name, relpath))

    def test_the_refuted_rules_left_no_constant_behind(self):
        for name in REFUTED_NAMES:
            self.assertFalse(
                hasattr(C, name),
                "%s was measured HARMFUL on our own tape and must not exist as a constant "
                "waiting to be wired (see config.py rules 3 and 4)." % name)

    def test_the_live_window_filter_is_unchanged(self):
        """DAILIES ONLY is derived but unmeasured, so the LIVE multiplier must still be the
        value the running system was reviewed with.  Arming it is one assignment; this test is
        what makes that assignment visible rather than incidental."""
        self.assertEqual(C.MAX_WINDOW_MULT, 2.0)
        self.assertEqual(C.DAILIES_ONLY_WINDOW_MULT, 1.0)
        self.assertNotEqual(C.MAX_WINDOW_MULT, C.DAILIES_ONLY_WINDOW_MULT)


class TestFreeRideIsArmed(LipTestCase):
    """Replaces test_free_ride_is_off_as_well_as_unwired (armed 2026-07-29).

    WHY IT FLIPPED.  The staged note asked for the mirror to be measured before arming: the
    fear was that nothing qualifies without us and the book deploys nothing.  Measured on the
    live board -- 5,681 of 5,695 book-sides (99.75%) reach target_size on RIVAL size alone, and
    99.3% of sides with a competing score of 3 or less qualify.  And the CFTC filing settles
    what the funded size was worth: the qualifying walk stops once cumulative size reaches
    target, so contracts posted beyond it score EXACTLY ZERO.  The 999-contract 1c gas rung and
    the 1,500/3,000-contract TRUEV rungs were the largest objects in the whole tape and earned
    nothing at all."""

    def test_it_is_on(self):
        self.assertIs(C.FREE_RIDE_ONLY, True)

    def test_it_is_wired_in_scan(self):
        """Armed AND read.  A True constant nothing consults is worse than a False one: it
        reads as shipped."""
        import inspect
        from .. import scan
        src = inspect.getsource(scan)
        self.assertIn("FREE_RIDE_ONLY", src)
        self.assertIn("free_ride_refused", src)

    def test_the_land_grab_price_is_now_unreachable_from_build_slots(self):
        """LAND_GRAB_PRICE_C = 1 is the geometry of the -100% cohort.  Under free-ride the
        funding branch is dead: every non-qualifying side is refused before it, and the
        empty-book branch (which set p = 1c) is a non-qualifying side by definition."""
        import inspect
        from .. import scan
        src = inspect.getsource(scan.build_slots)
        i = src.index("FREE_RIDE_ONLY")
        j = src.index("land_grab = alloc.t0_qualification_size")
        self.assertLess(i, j, "the free-ride gate must precede the funding branch")


if __name__ == "__main__":
    unittest.main()
