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
from . import money as M, ratchet as RT, ratelimit as RL, runtime as R, scan


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
        # --- BLOCKER-3: the book-poll lane ---
        self.book_polled = {}                # ticker -> ts of last successful poll
        self.degrade_steps = ()              # last applied §3.4 ladder (logged on change)
        self.coverage_bad_since = None       # spec §11: coverage <90% for 10 min pages
        self.coverage_alerted = False

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

        if adopt_obj is not None and venues:
            # Venue readings supplied up front: triage NOW and feed the verdicts to the shed
            # path (charter A: "maker-shed orders for cutover-triage verdicts").  With no
            # readings the positions sit in `m.pending_triage` and are judged per position
            # as the classify sweep produces a slot for them — never blind.
            verdicts = self.m.triage(now, venues)
            for v in verdicts:
                if v.get("exit_path") == cutover.MAKER_SHED:
                    self.m.triage_shed.add(v["ticker"])
            triaged = {v["ticker"] for v in verdicts}
            self.m.pending_triage = [p for p in self.m.pending_triage
                                     if p["ticker"] not in triaged]
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
        what we INTENDED — positions AND resting orders (BLOCKER-1: the earlier build rebuilt
        positions only, so every pre-crash resting order became invisible: uncancellable,
        unfilled-in-our-books, and its collateral vanished from the cash feed).  Then the
        step-4 coid-prefix sweep against the exchange's own order list, because it can only
        be DIFFED against a book we have already reconstructed.  Then the crash-gap fills
        window, bounded by the ledger's last timestamp.  Then positions, the exchange's
        statement, compared last — reconciling before replay would compare the exchange
        against an empty book and freeze everything.
        """
        rows = self.m.ledger.read()
        st = cutover.V4Positions().replay(rows)               # same arithmetic, v5's own tape
        # Positions/costs are ASSIGNED from replay, never accumulated onto whatever startup
        # already staged (BLOCKER-2: adoption writes `adopt` rows, replay rebuilds them, and
        # adding on top would double `position_cost` on every restart).
        for r in st.rows():
            leg = r["side"]
            self.m.positions.setdefault(r["ticker"], {"yes": 0.0, "no": 0.0})[leg] = r["net"]
            self.m.entry_basis[(r["ticker"], leg)] = r["basis"]
            self.m.cash.inventory[r["ticker"]] = {"n": r["net"], "basis": r["basis"]}
        for ticker in {r["ticker"] for r in st.rows()}:
            self.m.position_cost[ticker] = sum(
                r["net"] * r["basis"] for r in st.rows() if r["ticker"] == ticker)
        # A persisted freeze survives restart (v1 §9.4b: a freeze a restart clears is a
        # naked-short generator).
        cleared = {r.get("ticker") for r in rows
                   if (r.get("k") or r.get("kind")) == "assume_filled_clear"}
        for r in rows:
            if (r.get("k") or r.get("kind")) == "assume_filled" and \
                    r.get("ticker") not in cleared:
                self.m.frozen.add(r.get("ticker"))
        # SECOND AMENDMENT (b): accrued value survives restart — the cliff decision is only
        # as good as the A it remembers, and a restart that forgot 70¢ of accrual would
        # abandon the very program the rescue exists to recover.  Rows are cumulative; the
        # LAST per program wins.
        for r in rows:
            if (r.get("k") or r.get("kind")) == "accrual" and r.get("program_id"):
                self.m.accrued[r["program_id"]] = float(r.get("accrued") or 0.0)
        for r in rows:
            kind = r.get("k") or r.get("kind")
            # SF-6: the turnover count survives restart — replayed fills carry their
            # timestamps, and the first `set_window` of the new process drops the ones from
            # prior program periods.  Without this a restart amnesties a flow magnet.
            if kind == "fill_obs":
                self.m.refill.note_fill(r.get("ticker"), r.get("side"),
                                        float(r.get("count") or 0.0),
                                        ts=float(r.get("ts") or 0.0))
            # SF-4/ratchet: verification evidence is MONEY state and survives restart —
            # rung, verified-ness, stand-down and the rung0 cap come back from the
            # `ratchet` rows, and `readings_line` resumes past consumed file rows so no
            # reading is ever applied twice.
            elif kind == "ratchet" and r.get("venue"):
                v = r["venue"]
                st = self.m.venues.get(v)
                if st is None:
                    st = RT.VenueState(v)
                    self.m.venues[v] = st
                st.rung = int(r.get("rung_after", st.rung) or 0)
                if r.get("verdict") == RT.VERIFY:
                    st.verified = True
                if r.get("stood_down") or r.get("stand_down"):
                    st.stood_down = True
                if r.get("rung0_cap"):
                    st.rung0_cap_usd = float(r["rung0_cap"])
                if r.get("src") == "readings_file" and r.get("line_no"):
                    self.m.readings_line = max(self.m.readings_line, int(r["line_no"]))

        self.recover_orders(rows, now)

        last_ts = max([float(x.get("ts", 0.0)) for x in rows] or [0.0])
        self.m.coid_seq = max(self.m.coid_seq, LG.coid_seq_load())

        # Crash-gap fills: [last_ledger_ts − 60 s, now].  Overlapping BY CONSTRUCTION, which is
        # exactly why B8's dedupe lives at the state layer.
        if last_ts:
            self.m.poll_fills(now, since=last_ts - C.CRASH_GAP_LOOKBACK_S)
        self.m.reconcile(now)
        R.log("recovered", ledger_rows=len(rows), positions=len(self.m.positions),
              orders=len(self.m.orders), coid_seq=self.m.coid_seq, last_ledger_ts=last_ts)
        return st

    def recover_orders(self, rows, now):
        """BLOCKER-1 — rebuild `self.orders` from replay, then the v1 §9.4 step-4 sweep.

        Replay half: an order is live iff its `place_resp` succeeded and no terminal row
        (cancel_resp 200 / expired / assume_filled) followed; `fill_obs` rows carrying its
        order_id reduce its remaining.  Every rebuilt order's collateral is re-counted into
        the cash feed (keyed by its coid, exactly as `place()` counted it) — the invariant
        "published never above truth" REQUIRES counting it, because the exchange is still
        holding those dollars.

        Sweep half: every exchange resting order carrying our coid prefix that replay does
        NOT know is registered, its collateral counted (same invariant), and handed to the
        B10 UNKNOWN machinery — which retries its cancel and, exhausted, books it FILLED and
        freezes the market (the conservative direction).  Replay-live orders the exchange no
        longer shows go to the SAME machinery: their cancel either confirms (reduced_by → a
        learned fill or a clean release) or 404s into assume_filled.  Symmetric treatment,
        one resolution path.
        """
        live = {}
        for rec in rows:
            kind = rec.get("k") or rec.get("kind")
            oid = str(rec.get("order_id")) if rec.get("order_id") is not None else None
            if kind == "place_resp" and oid and not rec.get("err"):
                size = float(rec.get("size") or 0.0)
                rc = rec.get("remaining_count")
                live[oid] = {"order_id": oid, "coid": rec.get("coid"),
                             "ticker": rec.get("ticker"), "side": rec.get("side"),
                             "price": float(rec.get("price") or 0.0), "size": size,
                             "remaining": float(size if rc is None else rc),
                             "fully_closing": bool(rec.get("fully_closing")),
                             "expiration_ts": rec.get("expiration_ts"),
                             "placed_ts": float(rec.get("ts") or 0.0)}
            elif kind in ("cancel_resp", "expired") and oid:
                if kind == "cancel_resp" and int(rec.get("http", 0) or 0) != 200:
                    continue                  # a failed cancel is not terminal (B10 owns it)
                live.pop(oid, None)
            elif kind == "assume_filled" and oid:
                live.pop(oid, None)
            elif kind == "fill_obs" and oid and oid in live:
                live[oid]["remaining"] -= float(rec.get("count") or 0.0)
                if live[oid]["remaining"] <= 1e-9:
                    live.pop(oid, None)
        for oid, o in sorted(live.items()):
            exp = o.get("expiration_ts")
            if exp is not None and float(exp) <= float(now):
                continue                      # the backstop already fired; nothing rests
            self.m.orders[oid] = o
            if not o.get("fully_closing") and o.get("coid"):
                self.m.cash.resting_by_order[o["coid"]] = \
                    o["remaining"] * R.unit_collateral(o["side"], o["price"])

        # --- step-4 sweep against the exchange ---
        admitted, _ = self.m.bucket.admit("verify", now)
        if not admitted:
            return
        status, body = self.m.ex.orders()
        self.m.note_http(status, now)                         # SF-2
        if status != 200:
            R.log("recovery_sweep_failed", http=status)
            return
        exch = {}
        for row in (body or {}).get("orders") or []:
            coid = row.get("client_order_id") or ""
            if not R.owns_coid(coid):
                continue                      # never touch another process's orders
            exch[str(row.get("order_id"))] = row
        for oid, row in sorted(exch.items()):
            if oid in self.m.orders:
                continue
            remaining = float(row.get("remaining_count") or 0.0)
            price = float(row.get("price") or 0.0)
            side = row.get("side") if row.get("side") in ("bid", "ask") else \
                ("bid" if str(row.get("side")).lower() == "yes" else "ask")
            o = {"order_id": oid, "coid": row.get("client_order_id"),
                 "ticker": row.get("ticker"), "side": side, "price": price,
                 "size": remaining, "remaining": remaining, "placed_ts": 0.0}
            self.m.orders[oid] = o
            if o["coid"]:
                self.m.cash.resting_by_order[o["coid"]] = \
                    remaining * R.unit_collateral(side, price)
            self.m.unknown.note(oid, o["ticker"], side, remaining, now)
            R.log("recovery_unknown_order", order_id=oid, ticker=o["ticker"],
                  why="exchange shows our prefix; replay does not know it")
        for oid in sorted(set(self.m.orders) - set(exch)):
            o = self.m.orders[oid]
            if o.get("placed_ts", 0.0) == 0.0:
                continue
            self.m.unknown.note(oid, o["ticker"], o["side"], o.get("remaining", 0.0), now)
            R.log("recovery_order_gone", order_id=oid, ticker=o["ticker"],
                  why="replay says live; exchange does not show it")

    # =========================================================================================
    # THE LOOP
    # =========================================================================================
    def iteration(self, now=None, books=None, yes_mids=None, server_epoch=None):
        """ONE pass of the outer loop.  Returns the cycle read-out (or a halt read-out)."""
        now = self.clock() if now is None else float(now)
        self.iterations += 1

        # (1) B5 FIRST — a halted process stops doing work, not just stops placing.
        # SF-3: every halt path flattens ONCE (the in-cycle halts already flattened; this
        # catches iteration_error and any halt armed outside the cycle), and the cash-feed
        # heartbeat keeps publishing — a halted-but-alive v5 still holds inventory, and a
        # stale feed would page nestor's operator about a process that is fine.
        if self.m.halt.halted:
            try:
                if not self.m.halt_flatten_done:
                    self.m.flatten(now)
                    self.m.halt_flatten_done = True
                if self.m.publisher.due(now):
                    self.m.publisher.publish(now)
                # SF-3: the closing-only pass — a halted book must still be able to LEAVE.
                # Runs at the halted-idle cadence, posts only fully_closing sheds.
                self.m.halted_closing_pass(now)
            except Exception as exc:                          # noqa: BLE001 - SF-3: no crash
                R.log("halted_idle_error", err="%s: %s" % (type(exc).__name__, exc))
            R.log("iteration_skipped_halted", reason=self.m.halt.reason)
            return {"halted": True, "reason": self.m.halt.reason}

        # (2) scan → classify → slots.  Each is cadence-gated and rate-laned inside.
        programs = self.scanner.scan(self.m.ex, self.m.bucket, now)
        self.classifier.sweep(self.m.ex, self.m.bucket, programs, now, books=books)

        # (2b) BLOCKER-3: the book_poll lane — held/ordered markets ALWAYS, best-ranked
        # rest up to breadth, refreshing the classification so the requoter's price
        # reference, trigger (a), the day-stop mids and the meter's ticks_behind all read
        # a ≤1 s book instead of a ≤15 min one.
        self.book_poll_pass(now, programs)

        # (2a) charter B: books/yes_mids for the day stop and the meter come from the
        # classify table (the exchange's own statements), with any WS-fed entries the caller
        # passed taking precedence (they are fresher when the gate has passed).  Both sides
        # are on the YES axis — the ask's same-side best is 1 − best_no_bid.
        books = dict(books or {})
        yes_mids = dict(yes_mids or {})
        for tk, rec in self.classifier.table.items():
            yb = rec["sides"]["bid"]["p"]
            nb = rec["sides"]["ask"]["p"]
            entry = books.setdefault(tk, {})
            entry.setdefault("bid", yb)
            entry.setdefault("ask", (1.0 - nb) if nb is not None else None)
            if tk not in yes_mids and rec.get("yes_mid") is not None:
                yes_mids[tk] = rec["yes_mid"]

        seg = self.m.presence_log.read_segment(now)
        l_shed = {k: M.l_shed_median_h(v)
                  for k, v in self.m.shed_completed_h.items()}
        self.slots = scan.build_slots(programs, self.classifier, now,
                                      presence_rows=seg, frozen=self.m.frozen,
                                      l_shed=l_shed, p6=self.classifier.p6_ok,
                                      accrued=self.m.accrued,
                                      own_orders=self.own_orders())
        # SF-1: `projected_day_reward` is OURS (share × ρ/2 over funded slots), computed by
        # the cycle from its own allocation — the board-pool sum that used to live here
        # saturated the day stop at $150 against a ≤$60 deployment: untrippable.

        # (3) the cycle
        out = self.m.cycle(now, slots=self.slots, books=books, yes_mids=yes_mids,
                           server_epoch=server_epoch)
        out["programs"] = len(programs)
        out["classified"] = len(self.classifier.table)
        out["slots"] = len(self.slots)
        self.last_cycle_ts = now
        return out

    def own_orders(self):
        """SF-5's input: our live resting orders per slot key, prices on the SLOT's axis
        (a bid slot prices in YES cents; an ask slot in NO cents = 100 − YES)."""
        own = {}
        for o in self.m.orders.values():
            if o.get("remaining", 0) <= 0 or o.get("gone_404"):
                continue
            if o["side"] == "bid":
                key, px_c = (o["ticker"], "bid"), int(round(o["price"] * 100))
            else:
                key, px_c = (o["ticker"], "ask"), int(round((1.0 - o["price"]) * 100))
            own.setdefault(key, []).append((px_c, float(o["remaining"])))
        return own

    def book_poll_pass(self, now, programs):
        """BLOCKER-3 — the book_poll lane.

        THE SET: every market we hold or rest an order in, ALWAYS (the inventory-slot
        guarantee — a de-polled held market is never requoted, never shed, and its fills
        arrive as surprises), plus the best-ranked rest up to `MAX_REST_MARKETS`.

        THE CADENCE, derived with the §3.4 ladder: demand = classify (amortized) + fills
        (1/15) + recon (1/600) + one poll per market at BOOK_POLL_HZ.  When demand exceeds
        the bucket's CURRENT AIMD budget, `degrade_plan` applies the spec's ladder — held
        markets carry net = +inf so breadth-shedding (step 3) drops them LAST, and step 4
        halves the cadence rather than dropping the held set.  Derivation for the WS
        question (charter: "decide with a derivation"): the 1 Hz contract for the HELD set
        holds whenever `held ≤ B − fixed ≈ 3.7` markets at full budget; beyond that the
        ladder degrades non-held breadth first, then cadence to 0.5 Hz.  WS's value is
        BREADTH (6 → 32), not the held set's cadence — so ws_feed stays vendored+gated and
        un-wired this round, and `MAX_REST_MARKETS = 6` is the binding breadth.

        MIRROR (polling a market we left ↔ never polling one we hold): the always-set is
        the second end's guard; the first costs one deferrable request and is shed by the
        ladder.  Coverage below 90% for 10 min pages `coverage_low` (spec §11).
        """
        m = self.m
        held = {t for t, p in m.positions.items()
                if abs(p.get("yes", 0.0)) + abs(p.get("no", 0.0)) > 0}
        ordered = {o["ticker"] for o in m.orders.values()
                   if o.get("remaining", 0) > 0 and not o.get("gone_404")}
        always = held | ordered
        tickers = scan.poll_set(self.slots, always, connected=False)
        if not tickers:
            return {"polled": 0, "due": 0}
        net_by = {}
        for s in self.slots:
            net_by[s.ticker] = max(net_by.get(s.ticker, float("-inf")),
                                   s.net_at(0, C.FLOOR_RATE_PER_H))
        markets = [{"ticker": t,
                    "net": float("inf") if t in always else net_by.get(t, 0.0),
                    "ws_fresh_gated": False} for t in tickers]
        classify_amortized = max(1, len(self.classifier.table)) / C.CLASSIFY_REFRESH_S
        demand = RL.Demand(markets, classify_hz=classify_amortized,
                           book_poll_hz=C.BOOK_POLL_HZ, recon_s=C.RECON_POSITIONS_S,
                           fixed_hz=1.0 / C.FILLS_POLL_S)
        steps, demand = RL.degrade_plan(demand, m.bucket.b)
        if tuple(steps) != self.degrade_steps:
            self.degrade_steps = tuple(steps)
            R.log("degrade_plan", steps=list(steps), dropped=list(demand.dropped),
                  budget_hz=m.bucket.b)
        interval = 1.0 / demand.book_poll_hz
        due = [x["ticker"] for x in demand.polled()
               if now - self.book_polled.get(x["ticker"], float("-inf"))
               >= interval - 1e-9]
        by_prog = {p["program_id"]: p for p in programs}
        polled = 0
        for tk in due:
            admitted, _ = m.bucket.admit("book_poll", now)
            if not admitted:
                break
            status, body = m.ex.book(tk)
            m.note_http(status, now)
            if status != 200:
                continue
            self.book_polled[tk] = float(now)
            polled += 1
            rec = self.classifier.table.get(tk)
            prog = by_prog.get(rec["program_id"]) if rec else next(
                (p for p in programs if tk in p["tickers"]), None)
            if prog is not None:
                self.classifier.classify_one(tk, body, prog, now)
        self.note_coverage(now, polled, len(due))
        return {"polled": polled, "due": len(due)}

    def note_coverage(self, now, polled, due):
        """spec §11: coverage < 90% for 10 min pages — the alarm on the seam between the
        poll plan and the budget that must fund it."""
        frac = 1.0 if due == 0 else polled / float(due)
        if frac < C.COVERAGE_ALERT_FLOOR:
            if self.coverage_bad_since is None:
                self.coverage_bad_since = float(now)
            elif not self.coverage_alerted and \
                    float(now) - self.coverage_bad_since >= C.COVERAGE_ALERT_WINDOW_S:
                self.coverage_alerted = True
                R.ntfy("coverage_low", "lip_v5 book coverage %.0f%% for %d s"
                       % (100 * frac, C.COVERAGE_ALERT_WINDOW_S))
        else:
            self.coverage_bad_since = None
            self.coverage_alerted = False

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
                halted_pass = False
                try:
                    out = self.iteration(started)
                    halted_pass = bool(out and out.get("halted"))
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
                # SF-3: a halted loop drops to the slow idle cadence — no spin, no crash-loop
                # exit; the halt is a persisted decision and the loop's only remaining duties
                # (heartbeat, SIGTERM) run fine at HALTED_IDLE_S.
                remaining = (C.HALTED_IDLE_S if halted_pass else period) - elapsed
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
