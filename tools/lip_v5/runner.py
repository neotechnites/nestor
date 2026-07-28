"""
lip_v5.runner — THE OUTER LOOP.  The systemd-shaped entry: what `ExecStart` actually runs.

    init      refusals → ledger replay → RECOVERY sweep → adoption gate + triage (if adopting)
    run       while not stopping:  halt check → scan → classify → slots → engine.cycle() → sleep
    shutdown  cancel-all → handback (ALWAYS) → zeroed cash feed

Three properties this file exists to hold, each of which a naive loop loses:

 1. **B5's halt is checked at the TOP of every iteration**, not only inside `place()`.  A halted
    process must stop doing work, not merely stop placing: continuing to scan and allocate
    against a halted book burns rate budget, writes misleading telemetry, and makes the halt
    look like a bug rather than a decision.
 2. **The one-path-to-the-wire property survives assembly.**  The loop calls `engine.cycle()`
    and the scanner; neither reaches the exchange except through `Maker.place`/`Maker.cancel`
    (guarded) or through a rate-lane-admitted read.  `test_runner.py` asserts this on the
    ASSEMBLED loop, not just on `place()` in isolation.
 3. **Shutdown runs on every exit path** — signal, exception, or clean stop — because the
    handback and the zeroed feed are what make v4 restartable onto reality.  An exception that
    skipped them would leave the exact "v5 dead, v4 blind" state SF-2 exists to prevent.
"""

import time

from . import config as C, cutover, engine, guards as G, ledger as LG
from . import runtime as R, scan


class Runner(object):
    def __init__(self, maker, scanner=None, classifier=None, clock=None, sleep=None):
        self.m = maker
        self.scanner = scanner or scan.Scanner()
        self.classifier = classifier or scan.Classifier()
        self.clock = clock or R._now
        self._sleep = sleep or time.sleep
        self.slots = []
        self.iterations = 0
        self.last_cycle_ts = None
        self.started = False

    # =========================================================================================
    # INIT
    # =========================================================================================
    def init(self, now=None, adopt_obj=None, exchange_positions=None, marks=None,
             nestor_state=None, allow_fresh=False, reader_enabled=True, venues=None):
        """Returns (ok, refusals).  Nothing is placed and no capital moves here."""
        now = self.clock() if now is None else float(now)
        ok, refusals = self.m.startup(
            now, adopt_obj=adopt_obj, exchange_positions=exchange_positions, marks=marks,
            nestor_state=nestor_state, allow_fresh=allow_fresh,
            reader_enabled=reader_enabled)
        if not ok:
            return False, refusals

        self.recover(now)

        if adopt_obj is not None:
            verdicts = self.m.triage(now, venues or {})
            R.log("cutover_triage_summary",
                  keep=sum(1 for v in verdicts if v["decision"] == cutover.KEEP),
                  shed=sum(1 for v in verdicts if v["exit_path"] == cutover.MAKER_SHED),
                  cross=sum(1 for v in verdicts if v["decision"] == cutover.TAKER_CROSS))
        self.started = True
        return True, []

    def recover(self, now):
        """v1 §9.4's restart procedure, in the order that makes each step's evidence available
        to the next.

        The ORDER is the derivation.  Replay first, because it is the only source that knows
        what we INTENDED.  Then the crash-gap fills window, because the ledger's last timestamp
        is what bounds it.  Then positions, because that is the exchange's statement and it can
        only be COMPARED against something we have already reconstructed — reconciling before
        replay would compare the exchange against an empty book and freeze everything.
        """
        rows = self.m.ledger.read()
        st = cutover.V4Positions().replay(rows)               # same arithmetic, v5's own tape
        for r in st.rows():
            leg = r["side"]
            self.m.positions.setdefault(r["ticker"], {"yes": 0.0, "no": 0.0})[leg] = r["net"]
            self.m.entry_basis[(r["ticker"], leg)] = r["basis"]
            self.m.position_cost[r["ticker"]] = \
                self.m.position_cost.get(r["ticker"], 0.0) + r["net"] * r["basis"]
            self.m.cash.inventory[r["ticker"]] = {"n": r["net"], "basis": r["basis"]}
        last_ts = max([float(x.get("ts", 0.0)) for x in rows] or [0.0])
        self.m.coid_seq = max(self.m.coid_seq, LG.coid_seq_load())

        # Crash-gap fills: [last_ledger_ts − 60 s, now].  Overlapping BY CONSTRUCTION, which is
        # exactly why B8's dedupe lives at the state layer.
        if last_ts:
            self.m.poll_fills(now, since=last_ts - C.CRASH_GAP_LOOKBACK_S)
        self.m.reconcile(now)
        R.log("recovered", ledger_rows=len(rows), positions=len(self.m.positions),
              coid_seq=self.m.coid_seq, last_ledger_ts=last_ts)
        return st

    # =========================================================================================
    # THE LOOP
    # =========================================================================================
    def iteration(self, now=None, books=None, yes_mids=None, server_epoch=None):
        """ONE pass of the outer loop.  Returns the cycle read-out (or a halt read-out)."""
        now = self.clock() if now is None else float(now)
        self.iterations += 1

        # (1) B5 FIRST — a halted process stops doing work, not just stops placing.
        if self.m.halt.halted:
            R.log("iteration_skipped_halted", reason=self.m.halt.reason)
            return {"halted": True, "reason": self.m.halt.reason}

        # (2) scan → classify → slots.  Each is cadence-gated and rate-laned inside.
        programs = self.scanner.scan(self.m.ex, self.m.bucket, now)
        self.classifier.sweep(self.m.ex, self.m.bucket, programs, now, books=books)
        seg = self.m.presence_log.read_segment(now)
        self.slots = scan.build_slots(programs, self.classifier, now,
                                      presence_rows=seg, frozen=self.m.frozen)
        self.m.projected_day_reward = sum(
            (s.rho / 2.0) * min(24.0, s.hours_left) for s in self.slots) or \
            self.m.projected_day_reward

        # (3) the cycle
        out = self.m.cycle(now, slots=self.slots, books=books, yes_mids=yes_mids,
                           server_epoch=server_epoch)
        out["programs"] = len(programs)
        out["classified"] = len(self.classifier.table)
        out["slots"] = len(self.slots)
        self.last_cycle_ts = now
        return out

    def run(self, max_iterations=None, deadline=None):
        """The systemd loop.  Exits on `stopping` (SIGTERM/SIGINT), the deadline, or the
        iteration cap; ALWAYS runs `shutdown` on the way out, including on an exception."""
        period = 1.0 / float(C.CYCLE_HZ)
        n = 0
        try:
            while not self.m.stopping:
                if max_iterations is not None and n >= int(max_iterations):
                    break
                started = self.clock()
                if deadline is not None and started >= deadline:
                    break
                try:
                    self.iteration(started)
                except Exception as exc:                      # noqa: BLE001
                    # An exception inside ONE iteration must not take the process down without
                    # the shutdown path: that would leave orders resting and no handback.
                    R.log("iteration_error", err="%s: %s" % (type(exc).__name__, exc))
                    self.m.halt.halt("iteration_error", started,
                                     {"err": "%s: %s" % (type(exc).__name__, exc)})
                n += 1
                elapsed = self.clock() - started
                if elapsed > period + C.CYCLE_OVERRUN_WARN_S:
                    R.log("cycle_overrun", elapsed=elapsed, period=period)
                remaining = period - elapsed
                if remaining > 0:
                    self._sleep(remaining)
        finally:
            self.shutdown(self.clock())
        return n

    def shutdown(self, now=None, reason="stop"):
        now = self.clock() if now is None else float(now)
        return self.m.shutdown(now, reason=reason)


# =============================================================================================
# THE systemd ENTRY
# =============================================================================================
def build(ex, now, mode=C.CASH_MODE_SHARED, live=False, shadow=False,
          ceiling_usd=C.MAX_TOTAL_COLLATERAL_USD):
    m = engine.Maker(ex, now, mode=mode, live=live, shadow=shadow, ceiling_usd=ceiling_usd)
    return Runner(m)
