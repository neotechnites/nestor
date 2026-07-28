"""spec §8.4 — `cash_feed(state)`, T-C1..C6.

T-C2 is THE LOAD-BEARING ONE: over a random wire sequence, published expected-cash ≤ true cash
at EVERY step, and the sequence MUST include a resolve whose cash credit arrives 41 MINUTES
LATER (R171).  The naive "release on result" implementation fails it at minute 0 — which is
exactly BLOCKER-1, and `test_TC2b` proves the naive version fails so the test cannot rot into
a tautology.
"""

import json
import os
import random
import unittest

from .. import cashfeed as F, config as C, runtime as R
from .base import LipTestCase

SETTLEMENT_LAG_S = 41 * 60.0        # R171, measured


class WireSim(object):
    """A model of the EXCHANGE's cash, independent of v5's bookkeeping.

    `true_cash` moves when the exchange moves it: down at placement, up at cancel refund, up
    when a settlement credit actually lands.  `published` is what nestor's breaker would
    compute.  The invariant under test is `published ≤ true_cash` AT EVERY STEP.
    """

    def __init__(self, state, start_cash=1000.0, naive_release=False):
        self.st = state
        self.start_cash = start_cash
        self.true_cash = start_cash
        self.naive = naive_release
        self.violations = []
        self.steps = 0

    def published(self):
        return self.start_cash + self.st.delta_dollars

    def check(self, label):
        self.steps += 1
        if self.published() > self.true_cash + 1e-9:
            self.violations.append((label, self.published(), self.true_cash))

    # -- wire events ---------------------------------------------------------------------
    def place(self, oid, collateral):
        self.st.reserve_order(oid, collateral)      # §5.3: publish BEFORE the wire call
        self.check("pre_post:%s" % oid)
        self.true_cash -= collateral                # the exchange takes it
        self.st.confirm_order(oid, collateral)
        self.check("post:%s" % oid)

    def reject(self, oid, collateral):
        self.st.reserve_order(oid, collateral)
        self.check("pre_post_rejected:%s" % oid)
        self.st.reject_order(oid)                   # no cash moved at all
        self.check("rejected:%s" % oid)

    def cancel(self, oid, collateral):
        self.true_cash += collateral                # the exchange refunds
        self.st.release_order(oid)                  # only AFTER the response
        self.check("cancel:%s" % oid)

    def fill(self, ticker, oid, n, unit):
        self.st.fill(ticker, oid, n, unit)          # collateral -> basis; no cash moves
        self.check("fill:%s" % ticker)

    def close_fill(self, ticker, oid, n, proceeds):
        """A SHED filling: contracts leave inventory and the exchange returns cash.  This is
        the path BLOCKER-A got wrong, and it is the FIRST thing cutover triage does."""
        held = abs(self.st.inventory.get(ticker, {}).get("n", 0.0))
        n = min(n, held)
        if n <= 0:
            return
        self.true_cash += n * proceeds              # the exchange pays the proceeds
        self.st.fill(ticker, oid, n, proceeds, side_sign=-1.0)
        self.check("close_fill:%s" % ticker)

    def resolve(self, ticker, credit, now):
        self.st.resolve(ticker, credit, now)        # NO cash yet — R171's 41 minutes
        self.check("resolve:%s" % ticker)
        if self.naive:
            # BLOCKER-1's failure mode, reproduced deliberately: release on RESULT.
            self.st._release(ticker)
            self.check("naive_release:%s" % ticker)

    def credit_lands(self, ticker, credit):
        self.true_cash += credit                    # the dollars actually arrive
        self.check("credit_landed:%s" % ticker)

    def balance_read(self, now=None):
        self.st.observe_balance(self.true_cash, now)
        self.check("balance_read")

    def settlement_row(self, ticker, paid):
        self.st.settlement_row(ticker, paid)
        self.check("settlement_row:%s" % ticker)

    def nestor_credit(self, amount):
        """BLOCKER-B: in the SHARED account nestor is paid too, and its credits are exogenous
        to v5's `delta_dollars` in exactly the way a real credit is."""
        self.true_cash += amount
        self.check("nestor_credit")


class TestExactDollars(LipTestCase):
    def test_TC1_hand_built_state(self):
        st = F.CashState()
        st.confirm_order("o1", 183.42)
        st.inventory["TSY"] = {"n": 120.0, "basis": 0.405}
        st.pending["GAS"] = F.PendingSettlement("GAS", 17.50, 25.00, 0.0, None)
        st.realized_pnl = 2.39
        st.fees_paid = 0.00
        st.rewards_accrued_unpaid = 6.94
        st.settled_payout_expected = 6.26
        c = st.components()
        self.assertAlmostEqual(c["resting_collateral"], 183.42, places=6)
        self.assertAlmostEqual(c["inventory_basis"], 48.60, places=6)
        self.assertAlmostEqual(c["settled_awaiting_payout"], 17.50, places=6)
        self.assertAlmostEqual(c["inventory_settle_max"], 120.00, places=6)
        # delta = −(183.42 + 48.60 + 17.50) + 2.39 − 0.00
        self.assertAlmostEqual(st.delta_dollars, -247.13, places=6)
        # pending = 6.94 + 120.00 + 6.26
        self.assertAlmostEqual(st.pending_payout_dollars, 133.20, places=6)

    def test_fill_does_not_move_delta(self):
        """A fill converts collateral into basis; the exchange took the cash at PLACEMENT."""
        st = F.CashState()
        st.confirm_order("o1", 10.0)
        before = st.delta_dollars
        st.fill("T", "o1", 20, 0.50)
        self.assertAlmostEqual(st.delta_dollars, before, places=9)

    def test_resolve_does_not_move_delta(self):
        """BLOCKER-1 in one assertion: resolution is not cash."""
        st = F.CashState()
        st.confirm_order("o1", 10.0)
        st.fill("T", "o1", 20, 0.50)
        before = st.delta_dollars
        st.resolve("T", expected_credit_usd=20.0, now=0.0)
        self.assertAlmostEqual(st.delta_dollars, before, places=9)
        self.assertAlmostEqual(st.settled_awaiting_payout, 10.0, places=9)


class TestTC2Property(LipTestCase):
    """T-C2 — published expected-cash ≤ true cash at EVERY step of a random wire sequence."""

    def _run(self, seed, naive=False):
        st = F.CashState()
        sim = WireSim(st, naive_release=naive)
        rnd = random.Random(seed)
        now = 0.0
        live = {}
        pending = []
        for i in range(200):
            now += rnd.uniform(1.0, 30.0)
            action = rnd.choice(["place", "place", "fill", "fill", "cancel", "reject",
                                 "shed_loss", "shed_loss", "shed_gain",
                                 "resolve", "credit", "settle_row", "balance"])
            if action == "place":
                oid = "o%d" % i
                col = round(rnd.uniform(0.5, 20.0), 2)
                sim.place(oid, col)
                live[oid] = {"col": col, "ticker": "T%d" % (i % 7), "n": 0}
            elif action == "reject":
                sim.reject("r%d" % i, round(rnd.uniform(0.5, 20.0), 2))
            elif action == "fill" and live:
                oid = rnd.choice(list(live))
                o = live[oid]
                if o["col"] > 0.5:
                    n, unit = 1, 0.5
                    sim.fill(o["ticker"], oid, n, unit)
                    o["col"] -= n * unit
                    o["n"] += n
            elif action == "cancel" and live:
                oid = rnd.choice(list(live))
                o = live.pop(oid)
                if o["col"] > 0:
                    sim.cancel(oid, o["col"])
            elif action in ("shed_loss", "shed_gain"):
                # A shed fill.  BLOCKER-A lived here: at a LOSS the basis leaving exceeds the
                # cash arriving, so booking only the basis publishes ABOVE the truth.
                tickers = [t for t in list(st.inventory) if abs(st.inventory[t]["n"]) > 0]
                if tickers:
                    t = rnd.choice(tickers)
                    basis = st.inventory[t]["basis"]
                    proceeds = (round(basis * rnd.uniform(0.1, 0.9), 2)
                                if action == "shed_loss"
                                else round(basis * rnd.uniform(1.05, 1.6), 2))
                    n = max(1, int(abs(st.inventory[t]["n"]) * rnd.uniform(0.2, 1.0)))
                    sim.close_fill(t, "shed%d" % i, n, max(0.01, proceeds))
            elif action == "settle_row":
                for t in list(st.pending):
                    p = st.pending[t]
                    if now >= p.resolved_ts + SETTLEMENT_LAG_S:
                        due = [x for x in pending if x[1] == t]
                        for entry in due:
                            pending.remove(entry)
                            sim.credit_lands(t, entry[2])
                        sim.settlement_row(t, p.expected_credit_usd)
            elif action == "resolve":
                tickers = [t for t in list(st.inventory) if abs(st.inventory[t]["n"]) > 0]
                if tickers:
                    t = rnd.choice(tickers)
                    credit = abs(st.inventory[t]["n"]) * 1.00
                    sim.resolve(t, credit, now)
                    # R171: the credit lands 41 MINUTES LATER, not now.
                    pending.append((now + SETTLEMENT_LAG_S, t, credit))
            elif action == "credit":
                due = [p for p in pending if p[0] <= now]
                for ts, t, credit in due:
                    pending.remove((ts, t, credit))
                    sim.credit_lands(t, credit)
                    sim.balance_read(now)
            elif action == "balance":
                sim.balance_read(now)
        return sim

    def test_TC2_invariant_holds_over_random_sequences(self):
        for seed in range(12):
            sim = self._run(seed)
            self.assertEqual(sim.violations, [],
                             "seed %d: published exceeded true cash" % seed)
            self.assertGreater(sim.steps, 100)

    def test_TC2b_the_naive_release_on_result_FAILS_at_minute_zero(self):
        """The proof that T-C2 is not a tautology: releasing on RESULT breaks the invariant
        immediately, and that is precisely how v5 would halt nestor through the very interface
        built to stop v5 halting nestor."""
        found = False
        for seed in range(12):
            sim = self._run(seed, naive=True)
            if sim.violations:
                found = True
                self.assertTrue(any("naive_release" in v[0] or "resolve" in v[0]
                                    for v in sim.violations))
                break
        self.assertTrue(found, "naive release must violate the invariant")

    def test_TC2c_explicit_41_minute_window(self):
        """The minimal reproduction, with the measured lag spelled out."""
        st = F.CashState()
        sim = WireSim(st)
        sim.place("o1", 10.0)
        sim.fill("T", "o1", 20, 0.50)
        sim.resolve("T", credit=20.0, now=0.0)
        for t in (60.0, 600.0, SETTLEMENT_LAG_S - 1.0):
            sim.balance_read(t)                  # nothing has landed; nothing releases
            self.assertAlmostEqual(st.settled_awaiting_payout, 10.0, places=9)
        sim.credit_lands("T", 20.0)
        # Shared mode: the settlements row is the ONLY release path (BLOCKER-B).
        sim.balance_read(SETTLEMENT_LAG_S)
        self.assertAlmostEqual(st.settled_awaiting_payout, 10.0, places=9)
        sim.settlement_row("T", 20.0)
        self.assertEqual(st.settled_awaiting_payout, 0.0)
        self.assertEqual(sim.violations, [])
        # after the credit, published rises by exactly the credit that landed
        self.assertAlmostEqual(sim.published(), sim.true_cash, places=6)


class TestTC6Release(LipTestCase):
    """T-C6 — releases on a BALANCE READ showing the credit, does NOT release on the resolve
    event, and at +6 h unconfirmed pages WITHOUT RELEASING."""

    def _resolved(self, mode=C.CASH_MODE_SHARED):
        st = F.CashState(mode=mode)
        st.confirm_order("o1", 10.0)
        st.fill("T", "o1", 20, 0.50)
        # A real verify-lane balance read BEFORE the resolve, so the (balance, delta) baseline
        # pair is recorded at one instant — the only admissible baseline.
        st.observe_balance(500.0)
        st.resolve("T", expected_credit_usd=20.0, now=0.0)
        return st

    def test_does_not_release_on_resolve(self):
        st = self._resolved()
        self.assertAlmostEqual(st.settled_awaiting_payout, 10.0, places=9)

    def test_BLOCKER_B_shared_mode_NEVER_releases_on_a_balance_read(self):
        """In the shared account nestor is trading the same balance, and ITS credits are
        exogenous to v5's `delta_dollars` in exactly the way a real credit is.  v5 cannot tell
        them apart from inside, so in shared mode it does not try."""
        st = self._resolved(C.CASH_MODE_SHARED)
        st.observe_balance(1_000_000.0)         # any amount at all
        self.assertAlmostEqual(st.settled_awaiting_payout, 10.0, places=9)

    def test_BLOCKER_B_shared_mode_releases_ONLY_on_the_settlements_row(self):
        st = self._resolved(C.CASH_MODE_SHARED)
        self.assertTrue(st.settlement_row("T", 20.0))
        self.assertEqual(st.settled_awaiting_payout, 0.0)
        self.assertAlmostEqual(st.realized_pnl, 10.0, places=9)   # 20.00 credit − 10.00 basis

    def test_subaccount_mode_may_release_on_a_balance_read(self):
        """Reserved for the account that holds nothing but v5's own activity, where
        "unexplained" genuinely means unexplained."""
        st = self._resolved(C.CASH_MODE_SUBACCOUNT)
        st.observe_balance(519.0)               # short of the expected credit
        self.assertAlmostEqual(st.settled_awaiting_payout, 10.0, places=9)
        st.observe_balance(520.0)               # exactly the credit
        self.assertEqual(st.settled_awaiting_payout, 0.0)

    def test_releases_on_a_settlements_row_carrying_the_paid_amount(self):
        st = self._resolved()
        self.assertTrue(st.settlement_row("T", 20.0))
        self.assertEqual(st.settled_awaiting_payout, 0.0)

    def test_a_settlements_row_with_no_paid_amount_is_not_confirmation(self):
        st = self._resolved()
        self.assertFalse(st.settlement_row("T", 0.0))
        self.assertAlmostEqual(st.settled_awaiting_payout, 10.0, places=9)

    def test_six_hours_pages_WITHOUT_releasing(self):
        st = self._resolved()
        self.assertEqual(st.unconfirmed_overdue(now=5 * 3600.0), [])
        due = st.unconfirmed_overdue(now=6 * 3600.0 + 1)
        self.assertEqual(due, ["T"])
        # NEVER auto-release on a timer: a lingering entry only makes v5 look POORER than it
        # is, which is the safe direction.
        self.assertAlmostEqual(st.settled_awaiting_payout, 10.0, places=9)
        # and it pages once, not every cycle
        self.assertEqual(st.unconfirmed_overdue(now=7 * 3600.0), [])


class TestBlockerA(LipTestCase):
    """BLOCKER-A — a closing fill must realize P&L, or a shed at a LOSS publishes ABOVE truth.
    This fires on the FIRST triage exit, which is the first thing v5 does at cutover."""

    def test_the_reviewers_exact_case_shed_10_at_20c_against_a_40c_basis(self):
        st = F.CashState()
        sim = WireSim(st)
        sim.place("o1", 4.00)                    # 10 contracts of basis at $0.40
        sim.fill("T", "o1", 10, 0.40)
        before_pub, before_true = sim.published(), sim.true_cash
        sim.close_fill("T", "shed1", 10, 0.20)   # shed at $0.20 — a $2.00 loss
        self.assertEqual(sim.violations, [])
        # true cash rose by the PROCEEDS, $2.00 — not by the $4.00 of basis that left
        self.assertAlmostEqual(sim.true_cash - before_true, 2.00, places=9)
        self.assertAlmostEqual(sim.published() - before_pub, 2.00, places=9)
        self.assertAlmostEqual(st.realized_pnl, -2.00, places=9)
        self.assertEqual(st.inventory, {})

    def test_a_shed_at_a_gain_also_books_exactly_the_proceeds(self):
        st = F.CashState()
        sim = WireSim(st)
        sim.place("o1", 4.00)
        sim.fill("T", "o1", 10, 0.40)
        sim.close_fill("T", "shed1", 10, 0.55)
        self.assertEqual(sim.violations, [])
        self.assertAlmostEqual(st.realized_pnl, 1.50, places=9)

    def test_a_partial_shed_leaves_the_remaining_basis_booked(self):
        st = F.CashState()
        sim = WireSim(st)
        sim.place("o1", 4.00)
        sim.fill("T", "o1", 10, 0.40)
        sim.close_fill("T", "shed1", 4, 0.20)
        self.assertEqual(sim.violations, [])
        self.assertAlmostEqual(st.inventory["T"]["n"], 6.0, places=9)
        self.assertAlmostEqual(st.inventory_basis, 2.40, places=9)
        self.assertAlmostEqual(st.realized_pnl, -0.80, places=9)

    def test_closing_more_than_held_flips_and_books_the_tail_as_OPENING(self):
        """The safe direction: the excess adds basis (more consumption) rather than
        manufacturing proceeds we never received."""
        st = F.CashState()
        st.confirm_order("o1", 4.00)
        st.fill("T", "o1", 10, 0.40)
        st.fill("T", "shed1", 15, 0.20, side_sign=-1.0)
        self.assertAlmostEqual(st.realized_pnl, -2.00, places=9)     # only the 10 closed
        self.assertAlmostEqual(st.inventory["T"]["n"], -5.0, places=9)
        self.assertAlmostEqual(st.inventory_basis, 1.00, places=9)   # 5 x $0.20 opened

    def test_the_naive_close_would_have_published_above_truth(self):
        """Proof the guard is load-bearing: booking only the basis (no realized P&L) is the
        pre-fix behaviour, and it breaks the invariant by exactly the loss."""
        st = F.CashState()
        sim = WireSim(st)
        sim.place("o1", 4.00)
        sim.fill("T", "o1", 10, 0.40)
        sim.true_cash += 10 * 0.20               # the exchange pays the proceeds
        st.inventory.pop("T")                    # ...and the naive code just drops the basis
        sim.check("naive_close")
        self.assertTrue(sim.violations)
        label, pub, true = sim.violations[0]
        self.assertAlmostEqual(pub - true, 2.00, places=9)


class TestBlockerB(LipTestCase):
    """BLOCKER-B — in the shared account, nestor's credits are indistinguishable from ours."""

    def test_nestors_credit_cannot_confirm_our_settlement(self):
        st = F.CashState(mode=C.CASH_MODE_SHARED)
        sim = WireSim(st)
        sim.place("o1", 10.0)
        sim.fill("T", "o1", 20, 0.50)
        sim.balance_read(0.0)
        sim.resolve("T", credit=20.0, now=0.0)
        sim.nestor_credit(500.0)                 # nestor gets paid; v5's credit has NOT landed
        sim.balance_read(60.0)
        self.assertAlmostEqual(st.settled_awaiting_payout, 10.0, places=9)
        self.assertEqual(sim.violations, [])

    def test_the_pre_fix_inference_would_have_released_on_it(self):
        """Same tape in subaccount mode, where the inference IS licensed: it releases. That
        contrast is the whole of BLOCKER-B — the arithmetic is fine, the ACCOUNT is not."""
        st = F.CashState(mode=C.CASH_MODE_SUBACCOUNT)
        st.confirm_order("o1", 10.0)
        st.fill("T", "o1", 20, 0.50)
        st.observe_balance(500.0)
        st.resolve("T", expected_credit_usd=20.0, now=0.0)
        st.observe_balance(1000.0)               # exogenous cash, whatever its origin
        self.assertEqual(st.settled_awaiting_payout, 0.0)


class TestConfounders(LipTestCase):
    """Three defects T-C2's random sequence found in the first implementation.  Each is pinned
    here so it cannot return quietly: all three published expected-cash ABOVE the truth, which
    is the single failure mode this file exists to prevent."""

    def test_a_cancel_refund_is_not_a_settlement_credit(self):
        """DEFECT 1.  §5.2a's "balance shows an increase ≥ the expected credit", read
        literally, confirms a credit out of a cancel REFUND — real dollars with nothing to do
        with the settlement.  The confirmable quantity is the UNEXPLAINED balance change.

        Exercised in SUBACCOUNT mode, because that is the only mode where the balance path is
        live at all after BLOCKER-B: our own refunds are the confounder that remains once
        nestor's are excluded by construction.
        """
        st = F.CashState(mode=C.CASH_MODE_SUBACCOUNT)
        st.confirm_order("o1", 10.0)
        st.fill("T", "o1", 20, 0.50)
        st.confirm_order("o2", 30.0)              # a second, unrelated resting order
        st.observe_balance(500.0)
        st.resolve("T", expected_credit_usd=20.0, now=0.0)
        # the exchange refunds o2's $30 — balance jumps well past the $20 credit
        st.release_order("o2")
        st.observe_balance(530.0)
        self.assertAlmostEqual(st.settled_awaiting_payout, 10.0, places=9,
                               msg="a cancel refund must not confirm a settlement credit")
        # ...and the genuine credit still confirms on top of it
        st.observe_balance(550.0)
        self.assertEqual(st.settled_awaiting_payout, 0.0)

    def test_an_inflight_reservation_is_not_unexplained_cash(self):
        """DEFECT 2.  §5.3 publishes collateral BEFORE the POST, so `delta_dollars` runs ahead
        of the exchange.  Using it as the confirmation baseline would count our own head start
        as unexplained cash."""
        st = F.CashState(mode=C.CASH_MODE_SUBACCOUNT)
        st.confirm_order("o1", 10.0)
        st.fill("T", "o1", 20, 0.50)
        st.observe_balance(500.0)
        st.resolve("T", expected_credit_usd=20.0, now=0.0)
        st.reserve_order("o9", 20.0)              # in flight: published spends it, exchange has not
        st.observe_balance(500.0)
        self.assertAlmostEqual(st.settled_awaiting_payout, 10.0, places=9)

    def test_a_second_resolve_on_one_ticker_merges_and_never_drops(self):
        """DEFECT 3.  `pending` is keyed by ticker; replacing an entry would drop the first
        one's basis out of `delta_dollars` and publish above the truth."""
        st = F.CashState()
        st.confirm_order("o1", 10.0)
        st.fill("T", "o1", 20, 0.50)
        st.resolve("T", expected_credit_usd=20.0, now=0.0)
        st.confirm_order("o2", 5.0)
        st.fill("T", "o2", 10, 0.50)
        st.resolve("T", expected_credit_usd=10.0, now=100.0)
        self.assertAlmostEqual(st.settled_awaiting_payout, 15.0, places=9)
        self.assertAlmostEqual(st.pending["T"].expected_credit_usd, 30.0, places=9)
        # the EARLIER resolve timestamp is retained, so the 6 h page fires on the older claim
        self.assertEqual(st.pending["T"].resolved_ts, 0.0)


class TestWriteAndModes(LipTestCase):
    def test_TC3_atomic_write_single_object(self):
        """T-C3 — a partially written file never parses as valid (temp + rename)."""
        st = F.CashState()
        st.confirm_order("o1", 12.34)
        path = self.path("lip_cash_feed.json")
        pub = F.CashFeedPublisher(path, st)
        pub.publish(now=1000.0)
        with open(path) as fh:
            obj = json.load(fh)
        self.assertEqual(obj["schema"], C.CASH_FEED_SCHEMA)
        self.assertAlmostEqual(obj["delta_dollars"], -12.34, places=6)
        # no temp file survives, and the directory holds exactly one object file
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith(".")], [])

    def test_TC3b_no_partial_file_is_ever_observable(self):
        """The rename is what guarantees it: write to a sibling temp, fsync, then replace."""
        st = F.CashState()
        path = self.path("feed.json")
        pub = F.CashFeedPublisher(path, st)
        seen = []

        def spy(p, obj, fsync=True):
            seen.append(sorted(os.listdir(self.tmp)))
            return R.atomic_write_json(p, obj, fsync)

        pub._write = spy
        pub.publish(now=1.0)
        pub.publish(now=2.0)
        with open(path) as fh:
            json.load(fh)                       # parses; never a prefix

    def test_TC4_subaccount_mode_zeros_with_components_intact(self):
        """T-C4 / §5.5 — the wall replaces the feed, but the STALENESS MONITOR survives
        cutover.  A silent disappearance of the alarm at an account-structure change is exactly
        the class we keep paying for."""
        st = F.CashState(mode=C.CASH_MODE_SUBACCOUNT)
        st.confirm_order("o1", 50.0)
        st.inventory["T"] = {"n": 10.0, "basis": 0.4}
        f = st.feed(now=1000.0)
        self.assertEqual(f["delta_dollars"], 0.0)
        self.assertEqual(f["pending_payout_dollars"], 0.0)
        self.assertAlmostEqual(f["components"]["resting_collateral"], 50.0, places=6)
        self.assertAlmostEqual(f["components"]["inventory_basis"], 4.0, places=6)
        self.assertEqual(f["heartbeat_s"], C.CASH_FEED_HEARTBEAT_S)

    def test_TC5_zeroed_final_feed_on_sigterm(self):
        """T-C5 — SIGTERM writes a zeroed feed AFTER cancel-all + shed, and only then may the
        file be removed."""
        st = F.CashState()
        st.confirm_order("o1", 50.0)
        path = self.path("feed.json")
        pub = F.CashFeedPublisher(path, st)
        pub.publish(now=1.0)
        st.release_order("o1")                  # cancel-all landed
        f = pub.publish_zeroed(now=2.0)
        self.assertEqual(f["delta_dollars"], 0.0)
        self.assertTrue(f["zeroed"])

    def test_publish_before_wire_is_the_ordering(self):
        st = F.CashState()
        pub = F.CashFeedPublisher(self.path("f.json"), st)
        f = pub.publish_before_wire("o1", 25.0, now=1.0)
        self.assertAlmostEqual(f["delta_dollars"], -25.0, places=6)
        self.assertAlmostEqual(f["max_inflight_usd"], 25.0, places=6)

    def test_heartbeat_cadence(self):
        st = F.CashState()
        pub = F.CashFeedPublisher(self.path("f.json"), st)
        self.assertTrue(pub.due(0.0))
        pub.publish(now=0.0)
        self.assertFalse(pub.due(10.0))
        self.assertTrue(pub.due(30.0))


class TestReaderContract(LipTestCase):
    def test_absent_file_is_zero_zero(self):
        d, p, s = F.read_feed_for_reader(self.path("nope.json"), now=0.0)
        self.assertEqual((d, p, s), (0.0, 0.0, "absent"))

    def test_stale_keeps_the_last_value_and_does_not_halt(self):
        """§5.4 — halting on a stale feed would convert v5 DYING into nestor DYING."""
        st = F.CashState()
        st.confirm_order("o1", 40.0)
        path = self.path("f.json")
        F.CashFeedPublisher(path, st).publish(now=1000.0)
        d, p, s = F.read_feed_for_reader(path, now=1000.0 + 121.0)
        self.assertEqual(s, "stale")
        self.assertAlmostEqual(d, -40.0, places=6)          # KEEP using it: it is conservative

    def test_fresh_is_ok(self):
        st = F.CashState()
        path = self.path("f.json")
        F.CashFeedPublisher(path, st).publish(now=1000.0)
        _, _, s = F.read_feed_for_reader(path, now=1000.0 + 119.0)
        self.assertEqual(s, "ok")

    def test_startup_refusal_when_shared_and_reader_disabled(self):
        """§4.4's mirror — an unconsumed feed is a silent regression to the hand ledger."""
        self.assertIsNotNone(F.startup_refusal_reason(C.CASH_MODE_SHARED, False))
        self.assertIsNone(F.startup_refusal_reason(C.CASH_MODE_SHARED, True))
        self.assertIsNone(F.startup_refusal_reason(C.CASH_MODE_SUBACCOUNT, False))


if __name__ == "__main__":
    unittest.main()
