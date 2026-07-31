"""lip_v5.alarm — THE BUG ALARM.  v6 deletes the money-lost stopper and puts this in its place.

    "NO MONEY-LOST STOPPER (Ryan, agreed).  Variance losses never halt the earner — the sizing
     priced them, and halting adds a $0 day on top of the loss.  The stopper becomes a BUG
     ALARM: halt only on losses INCONSISTENT with the priced model (faster than the
     always-filled worst case; loss-per-fill outside calibration).  Model-consistent losses:
     keep earning.  Model-impossible losses: the machine is broken."
                                          — note 55, THE RISK FRAME, RYAN'S DOOR

── WHY THE OLD STOPPER HAD TO GO ───────────────────────────────────────────────────────────
`day_stop_breached` halts on a LOSS MAGNITUDE.  But the whole of v6's sizing — the cluster
rail A = C/N, the ruin formula, d = 0.20 — exists to PRICE that magnitude in advance: a
z-sigma day is supposed to cost d x C, and it is supposed to be survivable, which is the only
reason the rail is as wide as it is.  A stop that fires at a loss the sizing already accepted
does not reduce risk; it converts a priced bad day into a priced bad day PLUS a zero day, and
it does it exactly when the board is paying most.

What a halt IS for is the case the sizing cannot price: the machine doing something other
than what the model says it is doing.  Two such cases are detectable from numbers we already
keep, and this module is those two tests and nothing else.

── ALARM 1: FASTER THAN THE ALWAYS-FILLED WORST CASE ───────────────────────────────────────
We never sell (safety law, unchanged) and we hold to settlement, so loss-given-fill is bounded
by the COLLATERAL of the filled contract: the worst possible outcome of $B of collateral is
$B.  Therefore, at every instant,

        cumulative loss  <=  cumulative collateral CONVERTED TO INVENTORY

is not a policy, it is arithmetic — and it holds under the ALWAYS-FILLED worst case too,
which is the strictest reading of note 55 §4.  A loss that exceeds it did not come from our
inventory settling badly.  It came from somewhere the model has no term for: an order we did
not authorise, a cap that was bypassed, a crossing trade booked as a maker fill, a fee schedule
we are not modelling, or our books disagreeing with the wire.  Every one of those is a BUG, and
a human must look.  MIRROR: this test cannot fire on a variance loss no matter how large,
because a variance loss is bounded by the same collateral it is measured against.

── ALARM 2: LOSS-PER-FILL OUTSIDE THE CALIBRATION TABLE ────────────────────────────────────
`bleed.G_TABLE` (n = 8,240 settled markets) says what a dollar filled at price p is expected
to lose: g(p).  So the book carries a running PREDICTION,

        E[loss]  = sum over fills of  basis x g(price)
        Var[loss]= sum over fills of  basis^2 x w(1-w)/p^2,   w = p x (1 - g(p))

(the per-fill loss is a two-point variable: the position is worth basis/p x 1 with probability
w and 0 otherwise, so its variance is the Bernoulli's, scaled).  Realised settlement losses are
compared against that prediction with a one-sided z-test.  The table being WRONG — a regime the
8,240 markets do not describe, or a mis-priced side, or fills at prices we did not think we
were quoting — shows up here and nowhere else, and it is a bug in the model rather than a bad
draw.

── THE THRESHOLD IS DERIVED, NOT PICKED ────────────────────────────────────────────────────
The test runs on the recon cadence, so over the program's remaining life it runs

        m = days_remaining x 86400 / RECON_POSITIONS_S

times, and a fixed per-test alpha would fire on noise m times.  Bonferroni over a FAMILY-WISE
error budget of ALARM_FAMILY_ALPHA gives per-test alpha = ALARM_FAMILY_ALPHA / m, and z is its
one-sided normal quantile.  At the deploy geometry (LIP ends 2026-09-01, ~32 days, 120 s
cadence ⇒ m ~ 23,040) that is alpha = 2.2e-6 and z ~ 4.59: the alarm is allowed to be wrong
about the whole program once in twenty programs, which is the right budget for something whose
consequence is a human being woken up.  Bonferroni is conservative under the positive
dependence a running sum has, and conservative is the correct direction for a halt.
m and z are LOGGED at construction; a build that cannot say why its threshold is 4.59 has a
magic number in it.

── WHAT THIS MODULE DOES NOT TOUCH ─────────────────────────────────────────────────────────
Never sells, own-orders-only, ignore-inherited-orders, B14's placement breaker, never-cross,
the rate lanes, the collateral ceiling, the cluster rail: all unchanged.  This replaces the
LOSS STOPPER, which is one rail, and it replaces it with a stricter question rather than a
looser one.
"""

import math

from . import bleed as B
from . import config as C
from . import runtime as R


def _z_from_alpha(alpha):
    """One-sided normal quantile.  Acklam's rational approximation is overkill here and
    bisection on `erfc` is exact to machine precision in ~60 iterations, deterministic, and
    has no table to go stale."""
    alpha = min(0.5, max(1e-15, float(alpha)))
    lo, hi = 0.0, 40.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        # P(Z > mid) = 0.5 x erfc(mid / sqrt(2))
        if 0.5 * math.erfc(mid / math.sqrt(2.0)) > alpha:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def checks_remaining(days_remaining, cadence_s=None):
    cadence = float(C.RECON_POSITIONS_S if cadence_s is None else cadence_s)
    if cadence <= 0:
        return 1.0
    return max(1.0, float(days_remaining) * 86400.0 / cadence)


def derive_z(days_remaining=None, family_alpha=None, cadence_s=None):
    """The module header's derivation, executable.  Returns (z, m, alpha)."""
    days = float(C.LIP_PROGRAM_DAYS_REMAINING if days_remaining is None else days_remaining)
    fam = float(C.ALARM_FAMILY_ALPHA if family_alpha is None else family_alpha)
    m = checks_remaining(days, cadence_s)
    alpha = fam / m
    return _z_from_alpha(alpha), m, alpha


def fill_moments(basis_usd, price_usd):
    """(expected loss, variance of loss) for `basis_usd` of collateral filled at `price_usd`.

    The position is worth `basis/p` at settlement with probability w = p x (1 - g(p)) — the
    board price DEGRADED by the measured calibration gap, the same quantity `dials.p_against`
    is built from — and 0 otherwise.  Loss = basis - value.
    """
    basis = max(0.0, float(basis_usd))
    p = min(1.0, max(1e-6, float(price_usd)))
    g = B.g_for_price(p)
    w = max(0.0, min(1.0, p * (1.0 - g)))
    mean = basis * g
    var = (basis / p) ** 2 * w * (1.0 - w)
    return mean, var


class BugAlarm(object):
    """The two tests, and the running sums they need.

    STATE DISCIPLINE: everything here is an accumulation of WORLD EVENTS (fills the wire
    reported, settlements the wire paid), never of our own decisions, so it is the same class
    of memory `test_convergence` licenses for the phi tape and the close cache.  Cancelling
    every order changes nothing in here — correctly, because the question "is the machine
    broken" is not a question about what is currently resting.
    """

    __slots__ = ("z", "m", "alpha", "converted_usd", "exp_loss", "var_loss", "realized_loss",
                 "fills", "settles", "fired")

    def __init__(self, days_remaining=None, family_alpha=None, cadence_s=None):
        self.z, self.m, self.alpha = derive_z(days_remaining, family_alpha, cadence_s)
        self.converted_usd = 0.0      # collateral turned into inventory (alarm 1's bound)
        self.exp_loss = 0.0
        self.var_loss = 0.0
        self.realized_loss = 0.0
        self.fills = 0
        self.settles = 0
        self.fired = None
        R.log("bug_alarm_armed", z=round(self.z, 4), checks=round(self.m, 1),
              alpha=self.alpha, family_alpha=C.ALARM_FAMILY_ALPHA,
              days_remaining=C.LIP_PROGRAM_DAYS_REMAINING,
              cadence_s=C.RECON_POSITIONS_S)

    # ── the world's events ───────────────────────────────────────────────────────────────
    def observe_fill(self, basis_usd, price_usd):
        mean, var = fill_moments(basis_usd, price_usd)
        self.converted_usd += max(0.0, float(basis_usd))
        self.exp_loss += mean
        self.var_loss += var
        self.fills += 1

    def observe_settlement(self, loss_usd):
        """`loss_usd` — realised loss on one settled position (POSITIVE for a loss, negative
        for a gain).  Gains count: the test is two-sided in its inputs and one-sided only in
        its conclusion."""
        self.realized_loss += float(loss_usd)
        self.settles += 1

    # ── the two tests ────────────────────────────────────────────────────────────────────
    def impossible_loss(self, loss_usd, committed_usd=0.0):
        """ALARM 1.  A loss larger than every dollar that has ever been at risk.

        `committed_usd` is the CURRENT basis at risk read off the engine's own books (resting
        collateral + the cost of every position held), which is what makes the bound honest
        for positions we ADOPTED rather than filled — an inherited or reconciled position has
        a cost but never passed through `observe_fill`, and a bound that missed it would page
        on the first mark of a book we did not open."""
        bound = self.converted_usd + max(0.0, float(committed_usd))
        return float(loss_usd) > bound + 1e-9, bound

    def sigma(self):
        return math.sqrt(max(0.0, self.var_loss))

    def excess(self):
        return self.realized_loss - self.exp_loss

    def outside_calibration(self):
        """ALARM 2.  Realised settlement losses beyond z sigma of the table's prediction."""
        s = self.sigma()
        if self.settles <= 0 or s <= 0.0:
            return False, 0.0
        return self.excess() > self.z * s, self.excess() / s

    def numbers(self, loss_usd=None, committed_usd=0.0):
        out = {"fills": self.fills, "settles": self.settles,
               "converted_usd": round(self.converted_usd, 4),
               "expected_loss_usd": round(self.exp_loss, 4),
               "realized_loss_usd": round(self.realized_loss, 4),
               "sigma_usd": round(self.sigma(), 4),
               "excess_usd": round(self.excess(), 4),
               "z": round(self.z, 4), "checks": round(self.m, 1)}
        if loss_usd is not None:
            out["book_loss_usd"] = round(float(loss_usd), 4)
            out["worst_case_bound_usd"] = round(self.converted_usd + max(0.0,
                                                float(committed_usd)), 4)
        return out

    def check(self, loss_usd=0.0, committed_usd=0.0):
        """THE ONE CALL THE ENGINE MAKES.  Returns `(halt: bool, reason: str, numbers: dict)`.

        A model-CONSISTENT loss returns False and is LOGGED as such — "variance losses never
        halt the earner" has to be visible on the tape, or an operator watching a drawdown
        cannot tell a working machine from a broken one that has not been caught yet.
        """
        nums = self.numbers(loss_usd, committed_usd)
        impossible, bound = self.impossible_loss(loss_usd, committed_usd)
        if impossible:
            self.fired = "loss_exceeds_always_filled_worst_case"
            nums["alarm"] = self.fired
            return True, self.fired, nums
        outside, sigmas = self.outside_calibration()
        nums["sigmas"] = round(sigmas, 3)
        if outside:
            self.fired = "loss_per_fill_outside_calibration"
            nums["alarm"] = self.fired
            return True, self.fired, nums
        return False, "", nums
