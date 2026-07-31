"""THE 120/480 DEPLOY SPLIT (v6 stage 5) — note 55's "RYAN'S 120/480", in tests.

The load-bearing assertion is `test_the_cap_IS_d_times_C`: the probe's worst case must EQUAL
the day stop's own budget, derived, so a $1,000 deploy scales it to $200 instead of silently
shipping a literal 120.
"""

from .. import alloc, config as C, marginal as MQ, probe as PR
from .base import LipTestCase


def slot(ticker, side="bid", rho=1.0, S=0.0, p=0.01, phi=0.0, hours_left=24.0,
         target_size=1000, cum_size=2000.0, accrued=0.0, **kw):
    lg = C.V6_PRICE_FLOOR_C if side == "bid" else 100 - C.V6_PRICE_FLOOR_C
    return alloc.Slot(ticker, side, rho=rho, S=S, p=p, phi=phi, hours_left=hours_left,
                      target_size=target_size, cum_size=cum_size, accrued=accrued,
                      land_grab_price_c=lg, **kw)


class TestTheCapIsDerived(LipTestCase):
    def test_the_cap_IS_d_times_C(self):
        """note 55: "worst case of the $120 ... = exactly d x C = 20% of $600 — the
        concentrated probe is precisely as safe as the diversified book by the day-stop's own
        arithmetic (the ruin formula inverted)."  """
        self.assertAlmostEqual(PR.probe_cap_usd(600.0), C.RUIN_D * 600.0, places=9)
        self.assertAlmostEqual(PR.probe_cap_usd(600.0), 120.0, places=9)

    def test_it_scales_with_capital_and_is_never_a_literal(self):
        self.assertAlmostEqual(PR.probe_cap_usd(1000.0), 200.0, places=9)
        self.assertAlmostEqual(PR.probe_cap_usd(300.0), 60.0, places=9)
        # structural, not textual: the function's body is one expression over RUIN_D and C,
        # with no numeric literal in it at all (the docstring's "$120 at $600" is prose).
        import ast
        import inspect
        body = ast.parse(inspect.getsource(PR.probe_cap_usd)).body[0].body
        code = [n for n in body if not isinstance(n, ast.Expr)]
        consts = [n.value for stmt in code for n in ast.walk(stmt)
                  if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))]
        self.assertEqual(consts, [0.0], "a derived cap may not carry a magic number: %s"
                         % consts)
        self.assertIn("RUIN_D", ast.dump(ast.Module(body=code, type_ignores=[])))

    def test_the_split_is_120_480_at_the_deploy_capital(self):
        p = PR.Probe(600.0)
        self.assertAlmostEqual(p.cap_usd, 120.0, places=9)
        self.assertAlmostEqual(p.capital_usd - p.cap_usd, 480.0, places=9)
        rec = self.logs_of("probe_armed")
        self.assertTrue(rec and rec[0]["probe_cap_usd"] == 120.0
                        and rec[0]["book_cap_usd"] == 480.0, rec)


class TestWingsAndWallsOnly(LipTestCase):
    """"NEVER mid-priced fat legs — that's what killed v4, not concentration"."""

    def test_the_wing_edge_is_where_the_calibration_table_stops_resolving_cents(self):
        from .. import bleed as B
        singles = [lo for lo, hi, _g, _n in B.G_TABLE if lo == hi]
        self.assertEqual(max(singles), C.PROBE_WING_MAX_C,
                         "the wing edge is the last SINGLE-CENT band in the g-table, not a "
                         "chosen number: %s" % singles)

    def test_a_1c_wall_in_a_probe_family_is_eligible(self):
        p = PR.Probe(600.0)
        cv = MQ.Curve(slot("KXUST-26JUL31-T1", p=0.01, cum_size=0.0))
        self.assertTrue(p.eligible(cv))
        self.assertEqual(p.lane_of(cv), PR.PROBE)

    def test_a_MID_PRICED_leg_in_a_probe_family_is_NOT(self):
        p = PR.Probe(600.0)
        cv = MQ.Curve(slot("KXUST-26JUL31-T2", p=0.35))
        self.assertFalse(p.eligible(cv))
        self.assertEqual(p.lane_of(cv), PR.BOOK)

    def test_a_wing_OUTSIDE_the_probe_families_is_NOT(self):
        p = PR.Probe(600.0)
        cv = MQ.Curve(slot("KXOTHER-26JUL31-T1", p=0.01))
        self.assertFalse(p.eligible(cv))
        self.assertEqual(p.lane_of(cv), PR.BOOK)


class TestTheTwoLanesRunConcurrently(LipTestCase):
    """"The $480 runs the ordinary law book (denominator + steady credits)" — CONCURRENTLY,
    not after: this is not a gate, it is a split."""

    def _board(self):
        walls = [slot("KXUST-26JUL31-T%d" % i, rho=0.6, S=0.0, p=0.01, cum_size=0.0)
                 for i in range(6)]
        wings = [slot("KXNATGAS-26JUL31-T%d" % i, rho=0.6, S=0.0, p=0.02, cum_size=0.0)
                 for i in range(6)]
        book = [slot("KXORD%02d-26JUL31-T1" % i, rho=2.0, S=200.0, p=0.20, cum_size=2000.0)
                for i in range(20)]
        return walls + wings + book

    def _run(self, probe=None):
        board = self._board()
        multi = set()
        if probe is not None:
            multi = probe.clusters(board)
        return board, MQ.allocate_marginal(
            board, budget_usd=600.0, per_market_cap_usd=21.43, cluster_cap_usd=21.43,
            multi_market_clusters=multi, probe=probe)

    def test_both_lanes_fund_in_the_same_pass(self):
        p = PR.Probe(600.0)
        board, (a, spent, _rep) = self._run(p)
        probe_usd = sum(MQ.Curve(s).capital(a[s.key]) for s in board
                        if a[s.key] > 0 and PR.is_probe_family(s.ticker))
        book_usd = sum(MQ.Curve(s).capital(a[s.key]) for s in board
                       if a[s.key] > 0 and not PR.is_probe_family(s.ticker))
        self.assertGreater(probe_usd, 0.0, "the probe never funded")
        self.assertGreater(book_usd, 0.0, "the ordinary book never funded")

    def test_the_probe_lane_cannot_exceed_d_times_C(self):
        p = PR.Probe(600.0)
        board, (a, _s, _r) = self._run(p)
        probe_usd = sum(MQ.Curve(s).capital(a[s.key]) for s in board
                        if a[s.key] > 0 and PR.is_probe_family(s.ticker))
        self.assertLessEqual(probe_usd, p.cap_usd + 1e-6,
                             "the probe's worst case must stay at d x C: %s" % probe_usd)

    def test_the_probe_is_exempt_from_the_CLUSTER_RAIL_and_uses_it(self):
        """$120 across two settle sources is 3x the $21.43 rail, deliberately — and it must
        actually happen, or the split is decorative."""
        p = PR.Probe(600.0)
        board, (a, _s, _r) = self._run(p)
        by_cluster = {}
        for s in board:
            if a[s.key] <= 0 or not PR.is_probe_family(s.ticker):
                continue
            ck = MQ.CL.cluster_of(s.ticker)
            by_cluster[ck] = by_cluster.get(ck, 0.0) + MQ.Curve(s).capital(a[s.key])
        self.assertTrue(by_cluster, "no probe cluster funded")
        self.assertGreater(max(by_cluster.values()), 21.43,
                           "the probe lane did not exceed the rail it is exempt from: %s"
                           % by_cluster)

    def test_the_ORDINARY_book_is_still_bound_by_the_rail(self):
        p = PR.Probe(600.0)
        board, (a, _s, _r) = self._run(p)
        by_cluster = {}
        for s in board:
            if a[s.key] <= 0 or PR.is_probe_family(s.ticker):
                continue
            ck = MQ.CL.cluster_of(s.ticker)
            by_cluster[ck] = by_cluster.get(ck, 0.0) + MQ.Curve(s).capital(a[s.key])
        self.assertTrue(by_cluster)
        self.assertLessEqual(max(by_cluster.values()), 21.43 + 1e-6,
                             "the exemption leaked into the ordinary book: %s" % by_cluster)

    def test_a_disarmed_probe_leaves_no_probe_code_on_the_path(self):
        board, (a, spent, _r) = self._run(None)
        for s in board:
            if a[s.key] > 0 and PR.is_probe_family(s.ticker):
                self.assertLessEqual(MQ.Curve(s).capital(a[s.key]), 21.43 + 1e-6)


class TestTheVerdictInstrumentation(LipTestCase):
    """note 55 final amendment 5: the estimates feed watched across 2 reward batches, loudly."""

    def _slots(self, accrued):
        return [slot("KXUST-26JUL31-T1", accrued=accrued)]

    def test_the_first_read_is_a_baseline_not_a_batch(self):
        p = PR.Probe(600.0)
        p.observe(self._slots(0.0))
        self.assertIsNone(p.verdict)
        rec = self.logs_of("probe_accrual")
        self.assertTrue(rec and rec[0]["first_read"])

    def test_two_batches_with_accrual_PASS_and_page(self):
        p = PR.Probe(600.0)
        p.observe(self._slots(0.0))
        p.observe(self._slots(0.4))
        self.assertIsNone(p.verdict, "one batch is not a verdict")
        p.observe(self._slots(0.9))
        self.assertEqual(p.verdict, "pass")
        self.assertTrue(any(a[0] == "probe_verdict" for a in self.alerts), self.alerts)
        v = self.logs_of("probe_verdict")[0]
        self.assertEqual(v["batches"], 2)
        self.assertAlmostEqual(v["accrued_usd"], 0.9, places=6)

    def test_two_batches_with_NOTHING_earned_FAIL(self):
        """The thesis' load-bearing link, measured: share flowing to a wall we qualified."""
        p = PR.Probe(600.0)
        p.observe(self._slots(0.0))
        p.observe(self._slots(0.0))
        self.assertIsNone(p.verdict, "an unchanged feed is not a batch")
        p.batches["KXUST-26JUL31-T1"] = 2               # two batches landed, still $0
        p._maybe_verdict()
        self.assertEqual(p.verdict, "fail")

    def test_every_batch_is_logged_with_its_delta(self):
        p = PR.Probe(600.0)
        p.observe(self._slots(0.0))
        p.observe(self._slots(0.4))
        rec = [r for r in self.logs_of("probe_accrual") if r.get("batch") == 1]
        self.assertTrue(rec)
        self.assertAlmostEqual(rec[0]["delta_usd"], 0.4, places=6)
        self.assertEqual(rec[0]["cluster"], "KXUST")

    def test_the_verdict_does_not_gate_the_book(self):
        """Ryan's plan runs the $480 concurrently and the scale-or-rework decision is HIS.
        The probe must therefore not be able to stop anything."""
        import inspect
        src = inspect.getsource(PR)
        self.assertNotIn("halt", src)
        self.assertNotIn("day_stopped", src)
