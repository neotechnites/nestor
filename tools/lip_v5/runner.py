"""
lip_v5.runner — THE OUTER LOOP.  The systemd-shaped entry: what `ExecStart` actually runs.

    init      refusals → ledger replay → adoption gate (positions only; no order adoption)
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
from . import ratchet as RT, ratelimit as RL, runtime as R, scan   # `money` left with the
                                                                  # shed path: its only use
                                                                  # here was l_shed_median_h


class Runner(object):
    def __init__(self, maker, scanner=None, classifier=None, clock=None, sleep=None):
        self.m = maker
        self.scanner = scanner or scan.Scanner()
        self.classifier = classifier or scan.Classifier()
        self.clock = clock or R._now
        self._sleep = sleep or time.sleep
        self.slots = []
        self.iterations = 0
        self.started_ts = None               # startup-burst clock (first iteration)
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

        # ── THE TRIAGE→SHED WIRING IS GONE (owner decision, 2026-07-30). ────────────────
        # This block ran `Maker.triage` at init and fed every MAKER_SHED verdict into
        # `m.triage_shed`, which the requoter then turned into cap-exempt closing orders at
        # the opposing best.  `cutover.triage` survives as a MEASUREMENT (see
        # `Maker.triage`'s docstring) but nothing here consumes a verdict, because the bot
        # does not sell.  Adopted positions ride to settlement like every other position.
        # ── STAGE 5 (2026-07-30): REINSTATE IS GONE. ──────────────────────────────────
        # It replayed the last snapshot of OUR OWN RESTING BOOK through the safety checks
        # and re-placed it — the most direct possible violation of the concept: the book
        # became a function of what the book used to be.  Its motivation (capital deployed
        # in minutes, not hours) is real and is answered the other way round, by making
        # DERIVATION fast and complete enough that the same world reproduces the same book
        # on its own.  If re-derivation is slow, that is the bug to fix; replay only hid it.

        self.started = True
        return True, []

    def recover(self, now):
        """RECOVER THE WORLD, NOT THE BOOK.  (Owner decision, 2026-07-30.)

        What a restart rebuilds is MONEY TRUTH: positions, entry basis, position cost, the
        persisted freezes, settlements, the readings watermark, the coid sequence, and the
        fill-dedupe seed.  Every one of those is a fact about the world that exists whether
        this process runs or not, and losing it is how a restart mis-states inventory.

        **WHAT IT NO LONGER REBUILDS IS `self.orders`.**  The order book starts EMPTY and only
        orders THIS PROCESS places enter it.  `recover_orders` is deleted — both halves:

          * the REPLAY half, which resurrected pre-crash orders from `place_resp` rows, and
          * the step-4 coid-prefix SWEEP, which adopted whatever wore our prefix on the wire.

        WHY, measured 2026-07-30.  The halted closing pass put GTC closing orders on the wire
        sized from broken books.  They then survived EVERY restart, because the sweep adopted
        whatever rested there as legitimately ours and the requoter reasoned from it.  An
        adopted order is a decision this process never made, admitted without any of the rails
        that would have refused making it — the same defect as the deleted `reinstate`, one
        layer down: the book became a function of what the book used to be.

        AND THE ACCOUNT IS SHARED.  nestor and other systems place orders here.  "Not ours" is
        the correct reading of everything on the wire at startup, including things wearing an
        old prefix of ours: they are not this process's concern, and this process will not
        adopt them, re-judge them or cancel them.

        THE COST, STATED PLAINLY (and flagged for review).  A pre-crash order still resting
        holds real exchange collateral that `resting_by_order` no longer counts, so published
        expected-cash sits ABOVE the dollars actually free — the direction §5.3 exists to
        prevent.  Two things bound it: `reconcile` reads the exchange's own BALANCE on its
        cadence (the truth, not our memory), and the expiration backstop on every order we
        ever place is `close_ts − CLOSE_MARGIN_S`, so nothing rests indefinitely.  The
        alternative — adopting the orders to make the arithmetic tidy — is exactly the
        behaviour that put a 98-contract $93 buy back on the wire after every restart.

        The ORDER of what remains is still the derivation: replay first (the only source that
        knows what we intended), then the crash-gap fills window bounded by the ledger's last
        timestamp, then positions — the exchange's statement — compared last, because
        reconciling before replay would compare the exchange against an empty book and freeze
        everything.
        """
        rows = self.m.ledger.read()
        # v5's OWN tape: positions are the SUM OF fill_obs ROWS, not v4's inference over
        # order responses (which double-counts every fill v5 also wrote a row for — see
        # V4Positions.replay's four cases).  `v4_tape=False` is the default; passed
        # explicitly here because this is the call the doubling was measured on.
        st = cutover.V4Positions().replay(rows, v4_tape=False)
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
        # SETTLEMENT rows are MONEY state and replay them EXACTLY (the position half is
        # already done: V4Positions.replay zeroes a settled ticker's book, so st.rows()
        # above rebuilt neither its inventory nor its cost — the released budget state).
        # The cash half comes back here, row by row in tape order:
        #   released=False → the claim is still awaiting cash: rebuild the
        #     settled-awaiting-payout entry from the ROW's basis (restore_pending — replay
        #     zeroed the inventory, so resolve() would book $0 of basis and let
        #     delta_dollars rise unconfirmed), and the 6 h page clock restarts from the
        #     row's own ts, not from boot.
        #   released=True → the cash was CONFIRMED before the restart: retire the pending
        #     claim the earlier row rebuilt and re-book the realized P&L, so the drawdown
        #     guard's equity term does not read a paid-out winner as capital that
        #     evaporated across a restart.
        # Every settlement row re-seeds `resolved`: the cluster must not re-charge a
        # determined market after a restart, the divergence loop must not freeze it, and
        # the settlements tape (returned in FULL every poll) must stay deduped.
        for r in rows:
            if (r.get("k") or r.get("kind")) != "settlement":
                continue
            tk = r.get("ticker")
            if not tk:
                continue
            self.m.resolved.add(tk)
            if r.get("released"):
                p = self.m.cash.pending.pop(tk, None)
                if p is not None:
                    self.m.cash.settled_payout_expected = max(
                        0.0, self.m.cash.settled_payout_expected - p.expected_credit_usd)
                self.m.cash.realized_pnl += float(r.get("realized_usd") or 0.0)
            elif float(r.get("basis_usd") or 0.0) or float(r.get("expected_usd") or 0.0):
                self.m.cash.restore_pending(tk, float(r.get("basis_usd") or 0.0),
                                            float(r.get("expected_usd") or 0.0),
                                            float(r.get("ts") or 0.0))
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
            # STAGE 1 (2026-07-30) — THE LADDER IS NOT REPLAYED, BECAUSE IT NO LONGER
            # EXISTS.  This rebuilt rung, verified-ness, stand-down and the rung-0 cap from
            # the `ratchet` rows: a restart inheriting the PERMISSIONS the last process had
            # climbed to, which is precisely "memory of our own past decisions as an input".
            # Two processes on the same world would quote different books depending on which
            # had probed longer.  What replay still takes from these rows is the MEASUREMENT
            # bookkeeping: which lines of the operator's readings file have already been
            # consumed, so one reading is never counted twice.  The measured DENY rebuilds
            # itself from the readings the same way it was built the first time.
            elif kind in ("ratchet", "venue_measured", "venue_denied_measured"):
                if r.get("src") == "readings_file" and r.get("line_no"):
                    self.m.readings_line = max(self.m.readings_line, int(r["line_no"]))

        last_ts = max([float(x.get("ts", 0.0)) for x in rows] or [0.0])
        self.m.coid_seq = max(self.m.coid_seq, LG.coid_seq_load())

        # THE DEDUPE IS MEMORY; THE LEDGER IS THE RECORD.  B8's FillDedupe is an in-process
        # set, reborn EMPTY every start — so the deliberately-overlapping crash-gap window
        # below re-booked every fill the dying process had already written, and did it
        # through book_fill, the one door that is supposed to make double-booking
        # impossible.  Three damages from one re-book, all in the unsafe direction:
        # positions DOUBLE (v4 measured filled_cum 20 against a truth of 10 — the same
        # incident, one layer up); the second booking drives the order's `remaining` to 0 so
        # book_fill POPS an order that is STILL RESTING on the exchange, leaving it
        # uncancellable by us forever; and popping it releases its resting collateral, which
        # publishes delta_dollars ABOVE truth — the one thing §5.3 exists to prevent.
        # Seed the set from the tape we just replayed: every fill_obs row carries the
        # exchange's own fill_id (engine.book_fill writes it), which is exactly the key
        # is_new() tests.  MIRROR (double-book ↔ never book): rows with no fill_id seed
        # nothing, so an unkeyed fill is still ACCEPTED on re-read — understating inventory
        # is the naked-short direction, and guards.FillDedupe makes the same choice.
        seeded = 0
        for r in rows:
            if (r.get("k") or r.get("kind")) == "fill_obs" and r.get("fill_id"):
                self.m.dedupe.seen.add(str(r["fill_id"]))
                seeded += 1
        if seeded:
            R.log("dedupe_seeded", fills=seeded,
                  why="crash-gap window re-reads fills this tape already booked")

        # Crash-gap fills: [last_ledger_ts − 60 s, now].  Overlapping BY CONSTRUCTION, which is
        # exactly why B8's dedupe lives at the state layer.
        if last_ts:
            self.m.poll_fills(now, since=last_ts - C.CRASH_GAP_LOOKBACK_S)
        self.m.reconcile(now)
        R.log("recovered", ledger_rows=len(rows), positions=len(self.m.positions),
              orders=len(self.m.orders), coid_seq=self.m.coid_seq, last_ledger_ts=last_ts)
        return st

    # ── `recover_orders` AND `parse_order_row` ARE GONE (owner decision, 2026-07-30). ────
    # `recover_orders` rebuilt `self.orders` from `place_resp` replay AND ran the v1 §9.4
    # step-4 coid-prefix sweep against `GET /portfolio/orders`, registering every resting
    # order wearing our prefix that replay did not know, counting its collateral, and handing
    # it to B10's UNKNOWN ladder.  `parse_order_row` existed only to read those swept rows.
    #
    # Both are deleted because ADOPTION IS THE DEFECT.  See `recover`'s docstring for the
    # derivation; the short form is that the 2026-07-30 closing orders outlived every restart
    # by being re-adopted, and that an order this process did not place is an order this
    # process's rails never approved.  Startup is now identical to steady state: zero special
    # paths, an empty order book, and orders enter it only through `Maker.place`.
    #
    # The runtime counterpart is `engine.sync_orders`, which reconciles in ONE direction only
    # (drop what the wire says is gone) and can never import.

    # =========================================================================================
    # THE LOOP
    # =========================================================================================
    def iteration(self, now=None, books=None, yes_mids=None, server_epoch=None):
        """ONE pass of the outer loop.  Returns the cycle read-out (or a halt read-out)."""
        now = self.clock() if now is None else float(now)
        self.iterations += 1
        # THE STARTUP BURST (config derivation): discovery runs at the boosted cap for the
        # first RATE_CAP_STARTUP_S, then the standing residual.  AIMD still rules under it.
        if self.started_ts is None:
            self.started_ts = now
        boost = (now - self.started_ts) < C.RATE_CAP_STARTUP_S
        cap = C.RATE_CAP_HZ_STARTUP if boost else C.RATE_CAP_HZ
        if self.m.bucket.cap_hz != cap:
            self.m.bucket.cap_hz = cap
            self.m.bucket.b = min(self.m.bucket.b if self.m.bucket.b > 0 else cap, cap)                 if not boost else cap
            R.log("rate_cap_shift", cap_hz=cap, boost=boost)

        # (1) B5 FIRST — a halted process stops doing work, not just stops placing.
        # SF-3: every halt path flattens ONCE (the in-cycle halts already flattened; this
        # catches iteration_error and any halt armed outside the cycle), and the cash-feed
        # heartbeat keeps publishing — a halted-but-alive v5 still holds inventory, and a
        # stale feed would page nestor's operator about a process that is fine.
        if self.m.halt.halted:
            # ── THE HALT DOES EXACTLY ONE THING: CANCEL OUR OWN ORDERS, THEN NOTHING. ─────
            # (Owner decision, 2026-07-30: "it's either running and placing orders, or it's
            # not running.")  A halt is the statement "our books are not trustworthy".  Every
            # action that reads those books to decide what to send is therefore disqualified,
            # and the closing pass that used to live here was the worst case of it: it sized
            # cap-EXEMPT closing orders from the books the halt had just condemned, and put a
            # 98-contract $93 buy at 95c on the wire against a phantom short.
            #
            # WHAT SURVIVES AND WHY EACH ONE IS ALLOWED:
            #   * `flatten` — cancels, once.  A cancel can only REDUCE exposure and is the
            #     one act whose correctness does not depend on the books being right.  It is
            #     scoped to `self.orders`, i.e. orders THIS process placed, so nestor's and
            #     every other system's orders on the shared account are untouched.
            #   * `poll_fills_due` — a READ.  The world keeps moving while we are halted
            #     (resting orders can still fill before the cancel lands, positions settle);
            #     refusing to learn that would make the halt a source of NEW book error.  Its
            #     own try/except so a failing read cannot cost the heartbeat.
            #   * the cash-feed heartbeat — a WRITE OF WHAT WE KNOW, no wire order.  A stale
            #     feed pages nestor's operator about a process that is merely idle.
            # Nothing here places.  Positions ride to settlement.
            try:
                self.m.poll_fills_due(now)
            except Exception as exc:                          # noqa: BLE001 - no crash
                R.log("halted_idle_error", where="poll_fills",
                      err="%s: %s" % (type(exc).__name__, exc))
            try:
                if not self.m.halt_flatten_done:
                    self.m.flatten(now)
                    self.m.halt_flatten_done = True
                    R.log("halted_own_orders_cancelled",
                          remaining=len(self.m.orders),
                          why="halt cancels only orders this process placed; then idle")
                if self.m.publisher.due(now):
                    self.m.publisher.publish(now)
            except Exception as exc:                          # noqa: BLE001 - no crash
                R.log("halted_idle_error", err="%s: %s" % (type(exc).__name__, exc))
            R.log("iteration_skipped_halted", reason=self.m.halt.reason)
            return {"halted": True, "reason": self.m.halt.reason}

        # (1b) SF-4c FIRST: the exchange's accrual feed is ONE request a minute and it was
        # measured starved to zero polls when it ran after the discovery stages — scan,
        # classify and the book polls drain the bucket to its reserve every iteration, so a
        # cycle-time verify admit never sees a free token.  The smallest consumer goes first.
        self.m.poll_estimates(now)

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
        # L_SHED IS PERMANENTLY UNMEASURED, and that is the truth rather than a gap: it is
        # the median hours a completed SHED took, and this program completes none.  Passing
        # nothing makes `money.l_eff_h` take its `l_shed_h is None ⇒ ∞` branch, so `l_eff`
        # is the real horizon — time to close plus the settle lag — which is exactly how
        # long the capital is now committed for.
        # Tickers whose PROGRAM the scanner now refuses (window too long, denied family) are
        # retired: the requoter recalls any order resting on them so the capital returns to a
        # venue that is still eligible.
        live_tk = set()
        for prog in programs:
            live_tk.update(prog.get("tickers") or [])
        self.m.retired_tickers = {t for t in list(self.m.orders and
                                                  {o["ticker"] for o in self.m.orders.values()}
                                                  or set())
                                  if t not in live_tk}
        # THE 1.155 INCIDENT, root closed: ticker→program must not depend on the slot
        # table (the owner-picker's accrued lookup read it, so an unclassified held rung's
        # credit scored $0 and its sibling won the basis tiebreak — the same trap one level
        # deeper).  The programs FEED already carries every ticker's program, request-free.
        for _prog in programs:
            for _tk in _prog.get("tickers") or ():
                self.m.ticker_program[_tk] = _prog.get("program_id")
        self.slots = scan.build_slots(programs, self.classifier, now,
                                      presence_rows=seg, frozen=self.m.frozen,
                                      p6=self.classifier.p6_ok,
                                      accrued=self.m.accrued,
                                      own_orders=self.own_orders(),
                                      held=self.held_tickers())
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

    def held_tickers(self):
        """D1 — every ticker we have money in: an open position OR a live resting order.

        This is the SAME SET `book_poll_pass` builds for the inventory-slot guarantee, and that
        is the point: the guarantee that a held market is always polled is worth little if the
        same market can fail to produce a SLOT, because the shed and the requote both read the
        slot table and not the poll set.  One definition, two consumers.
        """
        m = self.m
        out = {t for t, p in m.positions.items()
               if abs(p.get("yes", 0.0)) + abs(p.get("no", 0.0)) > 0}
        out |= {o["ticker"] for o in m.orders.values()
                if o.get("remaining", 0) > 0 and not o.get("gone_404")}
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
