"""
lip_v5.engine — THE RUN CYCLE.

    startup  → refusals → ledger replay → adopt (positions) → arm.  NO order adoption:
               the order book starts empty and only orders THIS process places enter it.
    cycle    → clock/rate → classify → slots → r*+ALLOCATE → quote(MBB) → fills
             → meter → recycler → cash feed → recon → checkpoints → health
    shutdown → cancel-all (OURS only) → handback (ALWAYS) → zeroed cash feed

Two structural rules make this testable and make the guards real:

 1. **Every wire call goes through `self.ex`** (an `exchange.Exchange` or `FakeExchange`) and
    through a rate LANE.  The engine never touches `runtime.http`.
 2. **Every order goes through `self.place()`**, which consults `guards.place_allowed` FIRST
    and publishes the cash feed BEFORE the wire call.  There is exactly one path to the wire,
    so "is this guard enforced?" is answerable by reading one function.

v4's proven cycle shapes are reused on merits: make-before-break with the cancel-first degrade,
the classify-then-clamp ordering, the inventory-slot guarantee, and the restart sweep's
prefix-only coid ownership.
"""

import os
import signal

from . import alloc, cashfeed, clusters as CL, config as C, cutover
from . import alarm as AL, dials as DI, marginal as MQ, probe as PR
from . import quiet as QT, smooth as SM
from . import guards as G, ledger as LG, presence as P
from . import quote as Q
from . import ratchet as RT, ratelimit as RL, runtime as R, wsgate
# `scan` left with the halted closing pass: its only use here was `scan._book_levels`,
# reading a book in order to price a shed.


class Maker(object):
    def __init__(self, ex, now, mode=C.CASH_MODE_SHARED, data_dir=None, live=False,
                 shadow=False, ceiling_usd=C.MAX_TOTAL_COLLATERAL_USD):
        self.ex = ex
        self.mode = mode
        self.live = bool(live)
        self.shadow = bool(shadow)
        self.ceiling_usd = float(ceiling_usd)
        self.data_dir = data_dir or C.DATA_DIR

        # ── V6: THE CAPITAL MACHINE (note 55).  Armed by `config.MARGINAL_QUEUE_ARMED`; with
        # it False every line below is inert and this build is v5, which is what makes the
        # frozen fallback binary real ("FORK, don't edit").
        # `dials` are DERIVED FROM C at boot and re-derived every pass off the ACTUAL funded
        # mix (the floor-cap coupling); the seed is v5's own N so the first cycle before any
        # board data still has a legal rail.  `smoothed` is the competition estimate the queue
        # ranks on — snapshot S is what churns (note 55 item 4b).
        self.dials = DI.seed_dials(self.ceiling_usd)
        self.smoothed = SM.SmoothedS(SM.boot_window_s())
        # Filled by the quiet-family classifier (stage 3) and the 120/480 probe (stage 5);
        # both are properties of the BOARD and of config, never of our own past decisions.
        self.quiet_clusters = set()
        self.quiet_phi = {}
        # THE 120/480 BOOT MODE (note 55, THE DEPLOY PLAN).  `None` when disarmed, and with it
        # None there is no probe code on any allocation path.
        self.probe = PR.Probe(self.ceiling_usd) if (C.MARGINAL_QUEUE_ARMED
                                                    and C.PROBE_ARMED) else None
        # THE BUG ALARM replaces the money-lost stopper (note 55's risk frame).  It accumulates
        # WORLD events — fills the wire reported, settlements the wire paid — never our own
        # decisions, so it is the same class of memory the convergence doctrine licenses.
        self.alarm = AL.BugAlarm()

        self.ledger = LG.Ledger()
        self.presence_log = LG.PresenceLog()
        self.cash = cashfeed.CashState(mode=mode, ceiling_usd=ceiling_usd)
        self.publisher = cashfeed.CashFeedPublisher(state=self.cash)
        self.bucket = RL.Bucket(now)
        self.gate = wsgate.WsGate()
        self.meter = P.Meter(now)

        # --- rails (guards.py) ---
        self.halt = G.HaltState().load()
        self.peak = G.PeakRecord().load()
        self.persist = G.PersistGuard(self.halt)
        self.refill = G.RefillTracker()
        self.unknown = G.UnknownOrders()
        self.dedupe = G.FillDedupe()
        self.fill_cooldown = {}              # (ticker, side) -> ts of last fill (cooldown)
        self.ticker_program = {}             # ticker -> program_id, from every slot seen
        self.skew_ok = True
        self.day_stopped = False

        # --- book state ---
        self.positions = {}                  # ticker -> {"yes": n, "no": n}
        self.position_cost = {}              # ticker -> $
        self.entry_basis = {}                # (ticker, leg) -> $/contract
        self.orders = {}                     # oid -> dict
        self.place_hist = {}                 # (ticker, side) -> [ts]  (B14 breaker)
        self._rate_refused = {}              # (ticker, side) -> last log ts
        self.frozen = set()
        # MEASUREMENT, NOT PERMISSION (stage 1).  The only per-venue memory that survives
        # is what the exchange told us it PAID us there: `venue_measured` keeps the last
        # comparison and `measured_deny` the venues whose payment diverged hard below
        # projection on consecutive settlement days.  Both are facts about the world.
        self.venue_measured = {}             # venue -> last (reading, projection, ratio)
        self.measured_deny = {}              # venue -> the evidence that denied it
        self._venue_disagree = {}            # venue -> (consecutive days, last day key)
        self.health = {}                     # (ticker, side) -> P.SlotHealth
        self.rollback = cutover.RollbackState()
        self.coid_seq = LG.coid_seq_load()
        self.nestor_orders = set()
        self.nestor_positions = set()
        self.projected_day_reward = 0.0
        self.fees_paid = 0.0
        self.stopping = False
        self.last_meter_tick = None
        self.last_recon = 0.0
        self.last_orders_sync = 0.0   # the wire's resting book vs ours (see sync_orders)
        self.cycles = 0
        # phi per (ticker, side) from the LAST slot table (the law §6 chain, resolved in
        # scan.build_slots) — the fill_selection_tripwire's model input.
        self.phi_by_key = {}

        # --- requoter state (charter A) ---
        self.S_ref = {}                      # (ticker, side) -> S at last requote (§4.3(c))
        self.qual_ref = {}                   # (ticker, side) -> qualifies at last look (§4.3(d))
        self.mbb_degraded = {}               # (ticker, side) -> ts of the balance reject
        # §4.3(e) PER SLOT (final fix round, BLOCKER-3): the earlier scalar was set to `now`
        # at the end of EVERY pass, so trigger (e) could never fire — a dead guard wearing a
        # live constant.  Resync tracks when each slot was last EXAMINED with fresh data.
        self.slot_examined = {}              # (ticker, side) -> ts last examined
        self.land_grabbed = set()            # log land_grab once per (ticker, side)
        # ── THE SHED PATH IS GONE (owner decision, 2026-07-30).  THE BOT NEVER SELLS. ────
        # There is no `triage_shed`, `shed_target`, `shed_since`, `shed_held` or
        # `shed_completed_h`, because there is no state a shed could be in.  A position
        # leaves this book exactly one way: SETTLEMENT.  The D4 gate bounds every ride at
        # ≤7 days, and our own tape prices the alternative — paying the spread to exit — at
        # −$40.30 (the +2c leg) and −$123 (instant flatten).  See `requote_pass`.
        # NOTE the distinction this deletion does NOT touch: an ASK is still quoted, all day,
        # on every eligible slot.  An ask is NO-side collateral posted as an OPENING maker
        # quote — it is half of how the pool is earned — and it is not a sale of anything.
        # What died is orders whose PURPOSE is to reduce a position we hold.
        # --- SF-3 ---
        self.halt_flatten_done = False       # every halt path flattens ONCE
        # Note 52 D6: the per-order LOT container, from the ceiling — same value the cycle
        # recomputes; no reward projection needed.
        self.slot_cap_usd = C.slot_cap_usd(0.0, ceiling_usd=self.ceiling_usd)
        # --- SECOND AMENDMENT (b): accrued projected payout, per program.  The cliff
        # decision is only as good as the A it remembers, so this persists via `accrual`
        # money rows (≤60 s crash loss) and recovers with replay.
        self.accrued = {}                    # program_id -> $ accrued (model, conditional)
        self.last_accrual_ts = None
        self.last_estimates_poll = None      # SF-4c cadence clock
        self.last_accrual_write = 0.0
        self._accrual_written = {}           # program_id -> last persisted value
        # --- FINAL FIX ROUND state ---
        self.last_fills_poll = None          # BLOCKER-1: fills cadence on the verify lane
        self.pending_404 = {}                # oid -> {"requery_at", "second_read"} (§9.4a)
        self.last_alloc = {}                 # key -> qty from the last ALLOCATE (SF-1 input)
        self.readings_line = 0               # SF-4: v5_readings.jsonl lines consumed
        self._readings_stat = None           # (mtime, size) — skip unchanged files
        self.resolved = set()                # tickers whose market is determined/settled/
                                             # finalized: outcome fixed, no variance left, so
                                             # they hold no CLUSTER risk and never enter the
                                             # divergence path.  Fed ONLY by the exchange's
                                             # own word (settlements rows, market status);
                                             # rebuilt on restart from `settlement` ledger
                                             # rows, and doubling as the dedupe against the
                                             # settlements endpoint, which returns the FULL
                                             # tape every poll.
        self.retired_tickers = set()         # windows the scanner refuses (too long, closed)

    # =========================================================================================
    # STARTUP
    # =========================================================================================
    def startup(self, now, adopt_obj=None, exchange_positions=None, marks=None,
                nestor_state=None, allow_fresh=False, reader_enabled=True):
        """Returns (ok, refusals).  EVERY item here is a REFUSAL, not a warning: each names a
        state in which running quietly is worse than not running."""
        refusals = []

        # §4.4 mirror — an unconsumed feed is a silent regression to the hand ledger.
        r = cashfeed.startup_refusal_reason(self.mode, reader_enabled)
        if r:
            refusals.append(r)

        # §11 Collisions — nestor's state must be READABLE, and we take BOTH halves (B13).
        if nestor_state is None:
            refusals.append("nestor state unreadable — refusing to quote into a shared account "
                            "we cannot see")
        else:
            self.nestor_orders = set(nestor_state.get("open_order_tickers") or [])
            self.nestor_positions = set(nestor_state.get("position_tickers") or [])

        rows = self.ledger.read()

        # B7 — blank ledger against a non-flat account.
        fr = G.fresh_state_refusal(rows, adopt_obj is not None, exchange_positions,
                                   allow_flag=allow_fresh)
        if fr:
            refusals.append(fr)

        # B5 — a persisted halt survives restart, by design.
        if self.halt.halted:
            refusals.append("halted: %s (resume requires an explicit operator record)"
                            % self.halt.reason)

        if refusals:
            for msg in refusals:
                R.log("startup_refusal", reason=msg)
            return False, refusals

        # §6.3-C — adoption, then triage.  BLOCKER-2: adoption is IDEMPOTENT — if replay
        # already shows `adopt` rows, a re-supplied adopt file is SKIPPED, so `position_cost`
        # can never double and a restart with the same command line is safe.
        if adopt_obj is not None:
            if any((r.get("k") or r.get("kind")) == "adopt" for r in rows):
                R.log("adopt_skipped_already_adopted",
                      note="ledger replay carries adopt rows; state comes from replay")
                self.rollback.set_adopted(
                    [{"ticker": r.get("ticker"), "side": r.get("side")}
                     for r in rows if (r.get("k") or r.get("kind")) == "adopt"])
            else:
                self.adopt(now, adopt_obj, exchange_positions or {}, marks or {})
        return True, []

    def adopt(self, now, adopt_obj, exchange_positions, marks):
        res = cutover.adoption_gate(adopt_obj.get("positions", []), exchange_positions, marks)
        for tk in res["frozen"] + res["refused_for_quoting"]:
            self.frozen.add(tk)
        for e in res["excluded"]:
            # Durable freeze: replay rebuilds `frozen` from these rows (v1 §9.4b — a freeze
            # that a restart clears is a naked-short generator).
            self.ledger.write("assume_filled", ticker=e["ticker"], reason=e["reason"])
        for a in res["adopted"]:
            leg = a["side"]
            pos = self.positions.setdefault(a["ticker"], {"yes": 0.0, "no": 0.0})
            pos[leg] = float(a["net"])
            self.entry_basis[(a["ticker"], leg)] = float(a["basis"])
            self.position_cost[a["ticker"]] = \
                self.position_cost.get(a["ticker"], 0.0) + a["net"] * a["basis"]
            self.cash.inventory[a["ticker"]] = {"n": float(a["net"]), "basis": float(a["basis"])}
            # BLOCKER-2: one MONEY row per adopted leg, so replay rebuilds the position and
            # a second adoption is detectable.
            self.ledger.write("adopt", ticker=a["ticker"], side=leg, net=a["net"],
                              basis=a["basis"])
        self.rollback.set_adopted(res["adopted"])
        R.log("adopt", adopted=len(res["adopted"]), excluded=len(res["excluded"]),
              orphans=len(res["orphans"]))
        return res

    def triage(self, now, venues, r_star=C.FLOOR_RATE_PER_H):
        """CLASSIFICATION ONLY — it returns verdicts and NOTHING CONSUMES THEM (owner
        decision, 2026-07-30: the bot never sells).  `cutover.triage` still computes, for the
        record, whether a position's venue passes (★) and what leaving would cost, because
        that is a measurement worth having on the tape.  There is no longer any code path
        from a MAKER_SHED verdict to `place()`: the field that carried it (`triage_shed`) and
        the pass that read it (`update_shed_targets`) are both deleted."""
        adopted = [{"ticker": t, "side": ("yes" if p.get("yes", 0) else "no"),
                    "net": p.get("yes", 0) or p.get("no", 0),
                    "basis": self.entry_basis.get(
                        (t, "yes" if p.get("yes", 0) else "no"), 0.0)}
                   for t, p in sorted(self.positions.items())
                   if (p.get("yes", 0) or p.get("no", 0))]
        return cutover.triage(adopted, venues, now, r_star)

    # =========================================================================================
    # THE ONE PATH TO THE WIRE
    # =========================================================================================
    def cluster_rail_usd(self):
        """THE CLUSTER RAIL, ONE NUMBER, READ BY BOTH THE PLAN AND THE RAILS.

        v6: `A = C/N` off `dials` (note 54's capital-scaling procedure).  v5: the day-stop
        derived reserve.  It must be ONE function because a plan that proposes what `place()`
        refuses re-offers forever — the 2026-07-30 plan-vs-rail mismatch, which is a live
        WATCH ITEM in note 55 and is structurally impossible if there is only one expression
        of the rail."""
        if C.MARGINAL_QUEUE_ARMED and self.dials is not None and self.dials.rail_usd > 0:
            return self.dials.rail_usd
        return CL.cluster_cap_usd(
            G.day_stop_usd(self.projected_day_reward, ceiling_usd=self.ceiling_usd),
            ceiling_usd=self.ceiling_usd)

    def plan_marginal(self, now, slots, budget_usd, market_spent, cluster_spent):
        """V6'S PLAN STEP — smoothed competition, dials from C, then the marginal queue.

        Returns `(alloc, spent, report, rail_usd)`.  Three things happen here and each is
        logged with its numbers:

          1. SMOOTH.  Every slot's rival score is folded into `self.smoothed` at the derived
             window and the queue ranks on the average, not on the snapshot (note 55 item 4b).
             This is memory of the WORLD, which is legal under the convergence doctrine;
             cancelling our own orders changes not one sample of it.
          2. DERIVE THE DIALS.  `dials.derive_from_slots` runs the queue against a seed rail,
             reads the ACTUAL funded mix's price, and re-solves N >= z^2 p(1-p)/(d-p)^2 until
             N stops moving — the floor-cap coupling, computed rather than assumed.
          3. ALLOCATE at the derived rail.

        The rail is `self.cluster_rail_usd()` for BOTH the per-market and the per-cluster
        bound: the knee (per-market, emergent) and the cap (per-cluster, law) are different
        objects, and note 55 is explicit that a $66 rail naturally holds ~2 knee markets —
        so the per-market bound must not be tighter than the rail or the second knee is
        refused in exactly the double-fast clusters where the best dollars live.
        """
        s_smoothed = {}
        for s in slots:
            s_smoothed[s.key] = self.smoothed.observe(s.key, s.S, now)
        # THE CENTREPIECE'S GATE (note 55 final amendment 2).  Which clusters may hold MORE
        # THAN ONE market — nothing else.  The dollar rail, the per-strike cliff and the
        # bleed screen are untouched, which is the whole safety argument for relaxing it.
        # The probe's families are multi-market BY CONSTRUCTION (it is the experiment that
        # creates the evidence `quiet` needs); see probe.py.
        self.quiet_clusters, self.quiet_phi = QT.classify(slots, MQ.A.LAW_HORIZON_H)
        if self.probe is not None:
            self.quiet_clusters = self.quiet_clusters | self.probe.clusters(slots)
            # THE VERDICT INSTRUMENTATION rides the same slot table the plan does, so it reads
            # the estimates feed's accrual on every pass and cannot drift from what we funded.
            self.probe.observe(slots)

        def _alloc(sl, budget, rail, **kw):
            return MQ.allocate_marginal(sl, min(budget_usd, budget), market_spent=market_spent,
                                        cluster_spent=cluster_spent, cluster_cap_usd=rail,
                                        per_market_cap_usd=rail, s_smoothed=s_smoothed,
                                        multi_market_clusters=self.quiet_clusters,
                                        phi_by_cluster=self.quiet_phi,
                                        probe=self.probe, **kw)

        self.dials = DI.derive_from_slots(self.ceiling_usd, slots, _alloc)
        R.log("dials", window_s=self.smoothed.window_s, **self.dials.numbers())
        rail = self.cluster_rail_usd()
        a, spent, rep = _alloc(slots, budget_usd, rail)
        return a, spent, rep, rail

    def place_context(self, available_cash_usd=None, replacing_order_id=None):
        """`replacing_order_id` — **NEW-1: a MAKE-BEFORE-BREAK REPLACEMENT IS NOT AN
        ADDITION.**  MBB posts the new quote while the old one still rests, and the caps
        measured both in one reading.  With `slot_cap_usd == cluster_cap_usd` (see the
        derivation in `config.slot_cap_usd`) a slot at its own cap IS the whole cluster cap,
        so the transient double-count made the refusal CERTAIN, not occasional: the
        replacement was refused `cluster_worst_case_cap`, `_requote_slot` returned False, and
        nothing armed the cancel-first degrade (it latches only on an exchange
        `insufficient balance` reject).  The slot re-offered the same refused order every
        cycle, forever, at its stale price.  Exempting exactly the order under replacement
        measures the book AS IT WILL BE one call later, which is the true statement.

        MIRROR (exempting too much ↔ counting a replacement twice): the exemption is scoped
        to ONE order id, named by the caller, and only on the requote path — an ADD never
        passes it, so no path can grow exposure through it.  If the follow-on cancel fails,
        the overlap is real; it is bounded by one slot's collateral and by one cycle, because
        the next cycle's context carries no exemption for it and the requoter's own
        multiple-live-order self-heal cancels the older copy.  Counting it twice is the
        permanent-deadlock end, which is strictly worse than a one-cycle understatement.
        """
        open_pos, resting = [], []
        for t, p in self.positions.items():
            # A DETERMINED MARKET CARRIES NO CORRELATED RISK.  Once the settle source has
            # published, the position is worth exactly $1 or $0 per contract: the outcome
            # cannot move, we cannot trade out of it, and no new order can compound it.
            # Charging it against the cluster cap reserves risk budget for a bet that has
            # already resolved — measured live, our closed 26JUL28 treasury rungs were
            # blocking the fresh 26JUL29 window in the same cluster.  The pending CASH is the
            # cash feed's job (settled_awaiting_payout), not the risk cap's.
            # MIRROR (excluding too early ↔ too late): "determined" is the exchange's own
            # word, taken from its market status — never inferred from a clock, because a
            # market we merely believe has resolved is exactly the naked-exposure case.
            if t in self.resolved:
                continue
            for leg in ("yes", "no"):
                if abs(p.get(leg, 0.0)) > 0:
                    open_pos.append({"ticker": t, "side": leg, "n": abs(p[leg]),
                                     "basis": self.entry_basis.get((t, leg), 0.0)})
        for oid, o in self.orders.items():
            if replacing_order_id is not None and str(oid) == str(replacing_order_id):
                continue                                      # NEW-1: replacement, not add
            if o.get("gone_404"):
                # D5 — A `gone_404` ORDER IS NOT ON THE WIRE AND HOLDS NO COLLATERAL.  The
                # exchange itself told us the id does not exist; `_mark_gone` keeps the row only
                # so the reconciler can still learn its fate.  Every other consumer of
                # `self.orders` in this file already excludes it — the presence measure, the
                # blindness test, the requoter, the cash feed (six call sites, all
                # `remaining > 0 and not gone_404`) — and this builder was the one that did not,
                # so B15's ceiling and B16's cap were charged for PHANTOM collateral.  Under a
                # binding ceiling a phantom dollar refuses a real one 1:1.
                continue
            if o.get("remaining", 0) > 0:
                # `basis` is WHAT ONE CONTRACT CAN LOSE, i.e. the collateral we actually
                # posted — and `o["price"]` is on the YES axis for BOTH sides.  A no-leg
                # order at a yes-price of 0.84 costs 0.16, not 0.84.  Passing the yes price
                # made every sell-side quote read as up to 6x its real risk: measured live,
                # four small RATES orders holding ~$2 of collateral were scored at $64.48
                # against a $50 cluster cap, so the whole cluster refused everything and the
                # book deployed $5.76 of a $300 ceiling.  `unit_collateral` is the same
                # function the cash feed and the caps already use, so this makes the risk
                # measure agree with the money.
                resting.append({"ticker": o["ticker"],
                                "side": "yes" if o["side"] == "bid" else "no",
                                "n": o["remaining"],
                                "basis": R.unit_collateral(o["side"], o["price"])})
        # B9's turnover bound scales WITH the derived slot cap (charter amendment): a $50
        # rung gets 4 turnovers of $50, a $10 rung 4 of $10 — proportional blast radius.
        caps = alloc.Caps(inv_cap_usd=self.slot_cap_usd)
        return G.PlaceContext(
            halt_state=self.halt, positions=open_pos, resting_basis=resting,
            nestor_orders=self.nestor_orders, nestor_positions=self.nestor_positions,
            available_cash_usd=available_cash_usd,
            cluster_cap_usd=self.cluster_rail_usd(),
            frozen=self.frozen, refill=self.refill,
            n_cap_fn=lambda p: alloc.n_cap(p, caps),
            day_stopped=self.day_stopped, skew_ok=self.skew_ok,
            # B15/B16 — the ceiling and the per-market cap now bind at PLACEMENT, not only in
            # the allocator's plan.  `ceiling_usd` is the same number the plan uses, so a
            # correct plan is unaffected and only a plan that has drifted from the book is
            # refused.  The per-market cap derives from the ceiling rather than being a fresh
            # constant: with no exit, one market's worst case must not be able to consume the
            # whole book, and MARKET_CAP_FRAC is the fraction that bound is set at.
            # D2: through `config.market_cap_usd`, which enforces `slot_cap ≤ market_cap` — the
            # bare `MARKET_CAP_FRAC × ceiling` inverts the hierarchy below ~$100 of ceiling and
            # a rail tighter than the plan's own per-slot cap is a permanent re-offer loop.
            ceiling_usd=self.ceiling_usd,
            market_cap_usd=C.market_leg_cap_usd(
                self.ceiling_usd,
                G.day_stop_usd(self.projected_day_reward, ceiling_usd=self.ceiling_usd)),
            # B18 — the tracked variance tolerance.  Scale-free, so it needs no ceiling term.
            portfolio_var_max=C.PORTFOLIO_VAR_MAX)

    def place(self, ticker, side, price, count, expiration_ts, now,
              available_cash_usd=None, lane="place", replacing_order_id=None):
        """The ONLY way an order reaches the exchange.  Returns (ok, reason, resp).

        **THERE IS NO `fully_closing` ARGUMENT AND THEREFORE NOTHING IS CAP-EXEMPT.**
        (Owner decision, 2026-07-30.)  It used to travel from here into `place_allowed` and
        switch OFF, for that one order: the halt, the day stop, the clock-skew rail, the
        frozen-market refusal, the capital floor, the refill cap, the cluster cap, the
        portfolio-variance rail, the per-market leg cap and the collateral ceiling — every
        rail this program has.  The justification was sound while the bot could sell ("a book
        at its ceiling must always be able to LEAVE"), and it is void now that it cannot: an
        exemption exists to admit an order class that no longer exists.

        WHAT IT COST WHILE IT DID EXIST.  2026-07-30: a books-integrity halt armed the closing
        pass, which sized a 98-contract $93 buy at 95c from the condemned books and marked it
        `fully_closing=True`.  Ten rails read that flag and stood aside.  A rail that any
        caller can switch off is not a rail; it is a suggestion with a bypass.

        Order of operations is load-bearing:
          1. the RAILS (`guards.place_allowed`) — refuse before anything is spent or written
          2. the RATE LANE — refuse before the wire, not at it
          3. **publish the cash feed BEFORE the POST** (§5.3), so published expected-cash is
             never above the truth even if we die between the two
          4. the wire
          5. correct the feed from the response
        """
        # `basis` is WHAT ONE CONTRACT CAN LOSE — the collateral posted — and `price` is on the
        # YES axis for BOTH sides.  The same defect as the resting-order basis, in the one
        # place it was missed: an ask at a 0.97 yes-price costs 0.03 to post and was being
        # scored at 0.97, so a routine land grab read as a $291 order against a $75 cluster
        # cap and was refused every cycle, forever.  Plan and rail must measure in the same
        # currency as the money.
        order = {"ticker": ticker, "side": "yes" if side == "bid" else "no",
                 "n": float(count), "basis": R.unit_collateral(side, price)}
        ctx = self.place_context(available_cash_usd,
                                 replacing_order_id=replacing_order_id)
        ok, reason, detail = G.place_allowed(ctx, order)
        if not ok:
            R.log("place_refused", ticker=ticker, side=side, refused_by=reason,
                  detail=detail)
            return False, reason, None

        if self.shadow:
            R.log("shadow_place", ticker=ticker, side=side, price=price, count=count)
            return False, "shadow", None

        # B14 — placement-rate circuit breaker.  Counted BEFORE the rate lane so a loop cannot
        # hide behind a throttle, and keyed per (ticker, side) because that is the unit a
        # requote trigger acts on.
        bkey = (ticker, side)
        # ONLY "BLIND" PLACEMENTS COUNT.  The condition this breaker exists to catch is our
        # books not seeing our own orders — in the 130-order loop every placement happened
        # with ZERO live orders recorded for the slot, because each success parsed as a
        # rejection.  A make-before-break replacement, by contrast, always has the outgoing
        # order still live in our books, and a slot legitimately follows a moving book more
        # than three times a minute — which is what tripped this on the ladder fixture at
        # iteration 33.  Counting only blind placements keeps the breaker aimed at the
        # bookkeeping failure instead of at ordinary requoting.
        blind = not any(o.get("remaining", 0) > 0 and not o.get("gone_404")
                        and (o["ticker"], o["side"]) == bkey
                        for o in self.orders.values())
        hist = [t for t in self.place_hist.get(bkey, []) if now - t < C.PLACE_BURST_WINDOW_S]
        if blind and len(hist) >= C.PLACE_BURST_MAX:
            self.place_hist[bkey] = hist
            R.log("place_burst", ticker=ticker, side=side, n=len(hist),
                  window_s=C.PLACE_BURST_WINDOW_S)
            self.halt.halt("place_burst", now,
                           {"ticker": ticker, "side": side, "n": len(hist),
                            "why": "placed %d times in %gs — our books are not seeing our own "
                                   "orders" % (len(hist), C.PLACE_BURST_WINDOW_S)})
            return False, "place_burst", None

        admitted, why = self.bucket.admit(lane, now, key=(ticker, side))
        if not admitted:
            # A SILENT refusal is indistinguishable from an order never attempted (it cost an
            # hour of live diagnosis).  Say it, at most once per slot per window.
            if self._rate_refused.get(bkey, 0.0) + 30.0 <= now:
                self._rate_refused[bkey] = now
                R.log("place_rate_refused", ticker=ticker, side=side, lane=lane, why=why)
            return False, why, None

        self.coid_seq += 1
        self.persist.write(LG.coid_seq_store, self.coid_seq)
        coid = R.make_coid(ticker, side, self.coid_seq)
        body = R.order_body(ticker, side, price, expiration_ts, coid, count)
        collateral = float(count) * R.unit_collateral(side, price)

        if blind:
            self.place_hist.setdefault(bkey, []).append(now)
        self.ledger.write("place_req", ticker=ticker, side=side, price=price,
                          size=count, coid=coid, seq=self.coid_seq)
        # §5.3 — write (and fsync) the feed with this collateral ALREADY INCLUDED, then POST.
        # Every order posts collateral now — a closing order was the only kind that did
        # not, because it was covered by the position it reduced, and there are none.
        self.publisher.publish_before_wire(coid, collateral, now)

        status, resp = self.ex.place(body)
        self.note_http(status, now)                           # SF-2
        # THE ORDER OBJECT MAY BE FLAT OR NESTED.  Measured live 2026-07-28: the prod wire
        # returns the order's fields AT THE TOP LEVEL
        #     {client_order_id, order_id, remaining_count: "61.00", fill_count: "0.00", ts_ms}
        # while the fixture returned {"order": {...}}.  Reading only the nested form made a
        # SUCCESSFUL placement look like a rejection: the order went live on the exchange, v5
        # recorded nothing, released collateral it was actually holding, and one second later
        # placed the same order again — 130 duplicates on one rung before a human noticed.  A
        # response shape is the wire's to declare, never ours to assume: accept BOTH, and key
        # the decision on the only field that means "the exchange took it" — order_id.
        _nested = resp.get("order") if isinstance(resp, dict) else None
        o_resp = _nested if isinstance(_nested, dict) else (resp if isinstance(resp, dict) else {})
        if status not in (200, 201) or not o_resp.get("order_id"):
            if status == 0:
                # TRANSPORT failure: the POST may have LANDED (v4's B2 class).  The order
                # would be live with no order_id in our books — freeze the market so nothing
                # quotes over an invisible order, and KEEP the reservation counted (§5.3's
                # invariant: released-but-landed would publish above the truth; kept-but-dead
                # only under-publishes, the safe side).  The fills poll and the restart
                # sweep are the mitigation.  MIRROR (order live ↔ order dead): dead costs
                # one frozen market and an under-published reservation until an operator
                # reconciles; live-and-untracked is a naked collateral hole.
                self.frozen.add(ticker)
                self.ledger.write("phantom_risk", ticker=ticker, coid=coid)
                R.ntfy("poison", "lip_v5 phantom risk (transport-failed POST): %s" % ticker)
            else:
                self.cash.reject_order(coid)
            self.publisher.publish(now)
            self.ledger.write("place_resp", ticker=ticker, side=side, coid=coid,
                              err=str(resp)[:200])
            return False, "reject", resp

        o = o_resp
        oid = str(o["order_id"])
        self.cash.confirm_order(coid, collateral)
        self.orders[oid] = {"order_id": oid, "coid": coid, "ticker": ticker, "side": side,
                            "price": float(price), "size": float(count),
                            "remaining": float(o.get("remaining_count", count)),
                            "expiration_ts": int(expiration_ts),
                            "placed_ts": now}
        self.ledger.write("place_resp", ticker=ticker, side=side, coid=coid, order_id=oid,
                          price=price, size=count,
                          remaining_count=o.get("remaining_count"),
                          fill_count=o.get("fill_count", 0), seq=self.coid_seq,
                          expiration_ts=int(expiration_ts))
        # ── AN INSTANT FILL STARTS THE COOLDOWN AT PLACE TIME (2026-07-30 ~22:12 MT). ────
        # The place response itself can report a fill (`fill_count` > 0: the quote crossed
        # a book that moved since our read, or a taker was waiting).  The 90 s post-fill
        # cooldown used to start only when the FILLS POLL observed the fill ~30 s later —
        # so an insta-filled rung read as simply unfunded, the plan re-placed it every
        # cycle, and three unpaced placements in 60 s tripped B14 (gas 4.105, taker fees
        # paid three times).  The BOOKING of the fill stays with fill_obs (the tape law —
        # one source of position truth); only the CLOCK starts here, which is exactly the
        # clock's job: "a fill happened at this rung, stand back."
        try:
            _fc = float(o.get("fill_count") or 0)
        except (TypeError, ValueError):
            _fc = 0.0
        if _fc > 0:
            self.fill_cooldown[(ticker, side)] = float(now)
            R.log("placed_and_instantly_filled", ticker=ticker, side=side,
                  fill_count=_fc, price=float(price))
        self.publisher.publish(now)
        return True, "ok", resp

    def cancel(self, oid, now, lane="requote_cancel"):
        """`lane` is `exit_cancel` for flatten / day-stop / T3 / poison — never refused, never
        counted against the SF-1 cancel share."""
        o = self.orders.get(str(oid))
        if o is None:
            return False, "unknown_order"
        admitted, why = self.bucket.admit(lane, now, key=(o["ticker"], o["side"]))
        if not admitted:
            return False, why
        self.ledger.write("cancel_req", ticker=o["ticker"], order_id=oid)
        status, resp = self.ex.cancel(oid)
        self.note_http(status, now)                           # SF-2
        if status == 200 and resp.get("reduced_by") is not None:
            reduced = float(resp["reduced_by"])
            learned = max(0.0, o["remaining"] - reduced)
            if learned:
                self.book_fill(o["ticker"], o["side"], learned, o["price"], now,
                               fill_id="cancel:%s" % oid, order_id=oid,
                               closing=self.fill_nets_against_inventory(
                                   o["ticker"], o["side"], learned))
            # SF-4: `.get`, never `[...]` — an order rebuilt from replay or the recovery
            # sweep may carry no coid, and shutdown's cancel-all must survive that.
            self.cash.release_order(o.get("coid"))
            o["remaining"] = 0.0
            self.orders.pop(str(oid), None)
            self.unknown.resolved(oid)
            self.ledger.write("cancel_resp", ticker=o["ticker"], order_id=oid, http=200,
                              reduced_by=reduced)
            self.publisher.publish(now)
            return True, "ok"
        if status == 404:
            # v1 §9.4a — AMBIGUOUS: fully filled OR expired.  NEVER book zero silently; the
            # fills endpoint disambiguates (BLOCKER-1: the constant existed with no
            # implementation, and the honest FakeExchange now exercises this branch).
            self.note_cancel_404(oid, o, now)
            return False, "gone_404"
        # B10 — anything else leaves the order UNKNOWN: it may be live, and it holds collateral.
        self.unknown.note(oid, o["ticker"], o["side"], o["remaining"], now)
        self.ledger.write("cancel_resp", ticker=o["ticker"], order_id=oid, http=status)
        return False, "unknown"

    # =========================================================================================
    # FILLS
    # =========================================================================================
    def fill_nets_against_inventory(self, ticker, side, count):
        """**BOOKKEEPING OF CLOSING FILLS SURVIVES THE DEATH OF CLOSING ORDERS.**

        The bot no longer places an order whose purpose is to reduce a position — but THE
        EXCHANGE NETS AUTOMATICALLY, so an ordinary OPENING quote can still execute against
        inventory we hold: quote an ask on a market where we are long YES and the fill
        retires YES rather than opening NO.  That is a fact about the venue, not an intention
        of ours, and the books must state it correctly or `position_cost`, the day stop's
        mark-to-market and the cash feed's realized P&L all drift.

        This is the derivation the deleted `fully_closing` FLAG used to short-circuit: the
        order declared its own intent and the fill inherited it.  With no such intent to
        declare, the answer comes from the only honest source — WHAT WE HOLD.  It is exactly
        the rule `book_fill_row` already applied to fills it could not attribute to an order
        (v4's `apply_fill` netting), now applied to every fill, which is also why the two
        paths can no longer disagree.

        An execution on `side` reduces the OPPOSITE leg from the one it would open: an ask
        opens NO and closes YES.  MIRROR (calling an open a close ↔ calling a close an open):
        the `>=` is strict about covering the WHOLE count, so a partially-covered fill books
        as an OPEN — understating what we retired, which understates inventory rather than
        inventing a short, and that is the same direction `guards.FillDedupe` chooses."""
        leg = "yes" if side == "ask" else "no"
        held = float((self.positions.get(ticker) or {}).get(leg, 0.0))
        return held >= float(count) - 1e-9

    def book_fill(self, ticker, side, count, price, now, fill_id=None, closing=False,
                  proceeds=None, order_id=None, fee_usd=0.0, closed_leg=None):
        """The single entry point for a fill into state, so B8's dedupe cannot be bypassed.

        `order_id` links the fill to the resting order that produced it: the order's
        `remaining` shrinks and its resting collateral (keyed by coid in the cash feed)
        shrinks with it.  Without the link a fill leaves the collateral counted AND books the
        inventory — published expected-cash drifts BELOW truth (the safe direction, but a
        drift), and the requoter reads a stale `remaining` and under-refills.

        CLOSING AXIS (defect found by the aliveness suite, fixed here): a closing fill
        reduces the leg it SOLD, which on the ORDER axis is the OPPOSITE of the side's
        opening leg — an ask that opens buys NO, but an ask that closes sells the YES we
        hold.  The earlier code reduced the side's own leg, so a shed-ask fill decremented a
        zero NO leg, positions diverged from the cash feed's (correct) inventory, and the
        next reconcile froze the very market the shed had just cleaned.  Fills-API rows
        speak the LEG axis and pass `closed_leg` explicitly.  The cash value realized is the
        CLOSED leg's price: p for YES, 1−p for NO.
        """
        if not self.dedupe.is_new(fill_id, fallback_key="%s|%s|%s|%s" %
                                  (ticker, side, count, price)):
            return False
        leg = "yes" if side == "bid" else "no"
        pos = self.positions.setdefault(ticker, {"yes": 0.0, "no": 0.0})
        unit = R.unit_collateral(side, price)
        o = self.orders.get(str(order_id)) if order_id is not None else None
        coid = o.get("coid") if o else None
        if o is not None:
            o["remaining"] = max(0.0, o.get("remaining", 0.0) - float(count))
            if o["remaining"] <= 1e-9:
                self.orders.pop(str(order_id), None)
        if closing:
            leg = closed_leg or ("yes" if side == "ask" else "no")
            n_closed = min(float(count), max(0.0, pos.get(leg, 0.0)))
            pos[leg] = max(0.0, pos.get(leg, 0.0) - float(count))
            basis = self.entry_basis.get((ticker, leg), 0.0)
            self.position_cost[ticker] = max(
                0.0, self.position_cost.get(ticker, 0.0) - n_closed * basis)
            value = float(price) if leg == "yes" else (1.0 - float(price))
            self.cash.fill(ticker, coid or "closing", count, value, side_sign=-1.0,
                           proceeds_per_contract=proceeds)
        else:
            prev = pos[leg] * self.entry_basis.get((ticker, leg), 0.0)
            pos[leg] += float(count)
            self.entry_basis[(ticker, leg)] = ((prev + count * unit) / pos[leg]) \
                if pos[leg] > 0 else 0.0
            self.position_cost[ticker] = self.position_cost.get(ticker, 0.0) + count * unit
            self.cash.fill(ticker, coid or "o", count, unit)
            # THE BUG ALARM'S PREDICTION (alarm.py): every opening fill adds its own
            # calibration-table expectation and variance.  Charged on OPENING fills only —
            # a closing fill converts inventory back to cash and has no g.
            self.alarm.observe_fill(float(count) * unit, unit)
        if fee_usd:
            self.pay_fee(fee_usd)
        self.refill.note_fill(ticker, side, count, ts=now)    # SF-6: window-keyed
        self.fill_cooldown[(ticker, side)] = float(now)       # post-fill cooldown clock
        self.meter.note_fill((ticker, side), count, count * unit)
        self.rollback.note_fill(ticker, leg, now)
        self.ledger.write("fill_obs", ticker=ticker, side=side, count=count,
                          price_c=int(round(price * 100)), fill_id=fill_id,
                          order_id=order_id, fee_usd=round(float(fee_usd), 6),
                          # REPLAY NEEDS THE DIRECTION (2026-07-30, the -$78 Skubal short):
                          # without it every closing sell replayed as an OPENING short,
                          # both legs stacked, inventory_basis hit $315 of a $300 ceiling
                          # and the budget starved to zero.  One boolean.
                          closing=bool(closing),
                          closed_leg=(leg if closing else None))
        return True

    def pay_fee(self, usd):
        """Charter B: `fees_paid` wired on any fee-bearing event.  ONE source of truth — the
        cash feed's component — mirrored into the engine field the P&L reads, so the two can
        never disagree."""
        self.cash.pay_fee(usd)
        self.fees_paid = self.cash.fees_paid

    def note_http(self, status, now):
        """SF-2: EVERY exchange response passes through here — a 429 (ours or anyone's,
        since we cannot tell) yields the bucket (§3.2 AIMD)."""
        if status == 429:
            self.bucket.on_429(now)
        return status

    def book_fill_row(self, row, now, idx=None):
        """Book ONE real-shape fills row: `trade_id`, `order_id`, `side` ("yes"/"no"),
        `action` ("buy"/"sell"), `count`, `yes_price` in CENTS, `is_taker` — the payload
        v4's prod parsing consumes (its `fill_key`/`normalize_fill`).  Legacy `price`
        (dollars) and `fill_id` are accepted as aliases.

        OPEN vs CLOSE is OUR book's fact, not the row's: the fills axis (yes, sell) is an
        ask-shaped execution that CLOSES a held YES leg or OPENS a NO leg — the row cannot
        tell them apart.  The ORDER the row names still supplies our SIDE (the wire's own
        `book_side` and the legacy (side, action) derivation are the fallbacks).  It no longer
        supplies the OPEN/CLOSE answer: the `fully_closing` flag it used to carry is deleted
        with the closing orders, so BOTH branches now ask `fill_nets_against_inventory` —
        v4's apply_fill netting, which was already the crash-gap branch's rule."""
        # ── THE 2026-07-30 WIRE SHAPE, from a CAPTURED live payload (contact doctrine,
        # note 45: fields are the wire's to declare).  `captured_fills_20260730.json`:
        #   count_fp: "1.03"            — FRACTIONAL contracts, dollar-string.  The old
        #                                 `count` int is GONE; parsing it read every fill as
        #                                 ZERO, so inventory was invisible to the caps, the
        #                                 shed and the variance rail (found live, first fill
        #                                 of the note-52 deploy).
        #   yes_price_dollars: "0.1300" — replaces `yes_price` cents.
        #   book_side: "bid"/"ask"      — OUR side of the book, directly.
        #   fee_cost: "0.000000"        — the ACTUAL charged fee.  Measured on our first two
        #                                 maker fills: $0.00 — the UI's per-ticket fee column
        #                                 is a projection, not a charge.  When the wire says
        #                                 we paid, we book it; per-venue fee evidence is how
        #                                 "maker is free" stops being a generalization.
        # Old field names remain as fallbacks so v4-era ledger replays still parse.
        cfp = row.get("count_fp")
        count = float(cfp) if cfp is not None else float(row.get("count", 0))
        if count <= 0:
            # A zero-count row is a NON-EVENT and must not consume its fill_id: the old
            # parser read count_fp rows as 0, and booking them would have marked the ids
            # seen — blocking the TRUE booking of the same fills forever.
            return False
        ypd = row.get("yes_price_dollars")
        yp = row.get("yes_price")
        if ypd is not None:
            price = float(ypd)
        elif yp is not None:
            price = float(yp) / 100.0
        else:
            price = float(row.get("price", 0))
        ticker = row.get("ticker") or row.get("market_ticker")
        o = self.orders.get(str(row.get("order_id"))) if row.get("order_id") is not None \
            else None
        bs = row.get("book_side")
        if o is not None:
            side = o["side"]
        elif bs in ("bid", "ask"):
            side = bs
        else:
            # legacy derivation: (yes, sell) and (no, buy) are the same ask-shaped act
            leg_, sign_ = cutover.normalize_fill(row.get("side"),
                                                 row.get("action", "buy"))
            ask_like = (leg_ == "bid" and sign_ < 0) or (leg_ == "ask" and sign_ > 0)
            side = "ask" if ask_like else "bid"
        closing = self.fill_nets_against_inventory(ticker, side, count)
        # Fee: the wire's own `fee_cost` when present (the truth, maker or taker); the
        # is_taker formula only as a legacy fallback.
        fc = row.get("fee_cost")
        if fc is not None:
            fee = float(fc)
        else:
            fee = cutover.taker_fee_usd(count, price) if row.get("is_taker") else 0.0
        # v4 NEW-3: the fallback key carries order_id, price, time AND the enumeration
        # index — a keyless 5+5 split at one price must not collide (colliding keys DROP
        # the second fill: the naked-short direction).
        fid = row.get("trade_id") or row.get("fill_id") or row.get("id")
        fallback = "syn-%s|%s|%s|%s|%s|%s|%s" % (
            row.get("order_id"), ticker, row.get("side"), count,
            price, row.get("created_time"),
            "?" if idx is None else int(idx))
        return self.book_fill(ticker, side, count, price, now,
                              fill_id=fid if fid else fallback, closing=closing,
                              order_id=row.get("order_id"), fee_usd=fee,
                              closed_leg=("yes" if side == "ask" else "no"))

    def poll_fills(self, now, since=None):
        """One `/portfolio/fills` read (verify lane).  Ownership is by `order_id` ∈ our
        books (the real payload's identity — v4 attributed by order_id) with the coid
        prefix as a fallback for rows that carry one; everything else is skipped — never
        trust the index about someone else's order (shared account, §8.6)."""
        admitted, _ = self.bucket.admit("verify", now)
        if not admitted:
            return 0
        status, body = self.ex.fills(min_ts=since)
        self.note_http(status, now)
        if status != 200:
            return 0
        n = 0
        for i, row in enumerate(body.get("fills") or []):
            oid = str(row.get("order_id")) if row.get("order_id") is not None else None
            ours = (oid in self.orders) or R.owns_coid(row.get("client_order_id", ""))
            if not ours:
                continue
            if self.book_fill_row(row, now, idx=i):
                n += 1
        self.last_fills_poll = float(now)
        return n

    def poll_fills_due(self, now):
        """BLOCKER-1 — the LIVE fills cadence (FILLS_POLL_S on the verify lane), with the
        §9.4(4) overlap so a boundary fill is never missed; B8's dedupe absorbs the
        re-reads.  The reviewer's proof of the missing half: 630 cycles, 0 fills calls, a
        taker-filled market frozen as a position_divergence at t+601 s."""
        if self.last_fills_poll is not None and \
                float(now) - self.last_fills_poll < C.FILLS_POLL_S:
            return 0
        since = None if self.last_fills_poll is None \
            else self.last_fills_poll - C.CRASH_GAP_LOOKBACK_S
        return self.poll_fills(now, since=since)

    # =========================================================================================
    # v1 §9.4a — 404-ON-CANCEL DISAMBIGUATION (fully filled OR expired; never book zero
    # silently).  FILLS_REQUERY_DELAY_S finally has its implementation.
    # =========================================================================================
    def fills_for_order(self, oid, now):
        """One order-scoped fills read.  Returns (ok, rows): ok=None rate-refused (ask again
        next cycle), ok=False the QUERY ERRORED (not "no fills")."""
        admitted, _ = self.bucket.admit("verify", now)
        if not admitted:
            return None, []
        status, body = self.ex.fills(order_id=oid)
        self.note_http(status, now)
        if status != 200:
            return False, []
        return True, [r for r in (body.get("fills") or [])
                      if str(r.get("order_id")) == str(oid)]

    def note_cancel_404(self, oid, o, now):
        """First contact with the ambiguity.  The order is NOT resting (the wire said so):
        mark it `gone_404` so the requoter stops counting it as presence, but KEEP its
        collateral counted (published below truth is the safe side) until the fills index
        speaks.  MIRROR (booking the fill twice ↔ never booking it): B8's dedupe guards the
        first end; the requery-then-assume ladder below guards the second."""
        o["gone_404"] = True
        self.ledger.write("cancel_resp", ticker=o["ticker"], order_id=oid, http=404)
        ok, rows = self.fills_for_order(oid, now)
        if ok is None:
            self.pending_404[str(oid)] = {"requery_at": float(now), "second_read": False}
            return
        if ok is False:
            # v1 §9.4a: query ERROR ⇒ assume fully filled and freeze (conservative — we
            # over-state inventory, never under-state it).
            self.assume_404_filled(oid, now, why="fills_query_error")
            return
        if rows:
            self.book_404_rows(oid, rows, now, verdict="filled_first_read")
            return
        # NO fills on the first read is NOT "expired": the fills index has its own
        # propagation lag (~12 s worst observed).  Re-query once after 36 s = 3× that.
        self.pending_404[str(oid)] = {"requery_at": float(now) + C.FILLS_REQUERY_DELAY_S,
                                      "second_read": True}

    def book_404_rows(self, oid, rows, now, verdict):
        """Fills explain the 404: book them; whatever the index does NOT explain was the
        expired remainder — its collateral comes home and the order is terminal (an
        `expired` row, so replay agrees)."""
        for i, row in enumerate(rows):
            self.book_fill_row(row, now, idx=i)
        o = self.orders.pop(str(oid), None)
        if o is not None and o.get("remaining", 0.0) > 1e-9:
            self.cash.release_order(o.get("coid"))
            self.ledger.write("expired", ticker=o["ticker"], order_id=oid,
                              remaining=o.get("remaining"))
        self.pending_404.pop(str(oid), None)
        self.unknown.resolved(oid)
        R.log("cancel_404_resolved", order_id=oid, verdict=verdict, fills=len(rows))
        self.publisher.publish(now)

    def assume_404_filled(self, oid, now, why):
        """v1 §9.4a's terminal branch: book the WHOLE remainder as filled and freeze the
        market (quoting AND recycling, §9.4b)."""
        self.pending_404.pop(str(oid), None)
        o = self.orders.get(str(oid))
        if o is None:
            return
        self.book_fill(o["ticker"], o["side"], o.get("remaining", 0.0), o["price"], now,
                       fill_id="assume404:%s" % oid, order_id=oid,
                       closing=self.fill_nets_against_inventory(
                           o["ticker"], o["side"], o.get("remaining", 0.0)))
        self.orders.pop(str(oid), None)
        self.frozen.add(o["ticker"])
        self.ledger.write("assume_filled", ticker=o["ticker"], order_id=oid, why=why)
        R.ntfy("assume_filled", "lip_v5 assume_filled %s (%s)" % (o["ticker"], why))
        self.unknown.resolved(oid)

    def pump_404(self, now):
        """Advance every pending disambiguation.  Two clean zero reads 36 s apart ⇒ EXPIRED
        (collateral home, order terminal); fills ⇒ booked; a read error ⇒ assume filled."""
        for oid in sorted(self.pending_404):
            e = self.pending_404.get(oid)
            if e is None or float(now) < e["requery_at"]:
                continue
            ok, rows = self.fills_for_order(oid, now)
            if ok is None:
                continue                      # rate-refused; ask again next cycle
            if ok is False:
                self.assume_404_filled(oid, now, why="fills_requery_error")
                continue
            if rows:
                self.book_404_rows(oid, rows, now, verdict="filled_on_requery")
                continue
            if not e["second_read"]:
                e["requery_at"] = float(now) + C.FILLS_REQUERY_DELAY_S
                e["second_read"] = True
                continue
            o = self.orders.pop(oid, None)
            self.pending_404.pop(oid, None)
            if o is not None:
                self.cash.release_order(o.get("coid"))
                self.ledger.write("expired", ticker=o["ticker"], order_id=oid,
                                  remaining=o.get("remaining"))
            self.unknown.resolved(oid)
            R.log("cancel_404_resolved", order_id=oid, verdict="expired", fills=0)
            self.publisher.publish(now)

    # =========================================================================================
    # THE CYCLE
    # =========================================================================================
    def cycle(self, now, slots=None, books=None, yes_mids=None, server_epoch=None):
        """One pass.  Returns a read-out dict — the same one `--shadow` prints."""
        self.cycles += 1
        out = {"ts": now, "cycle": self.cycles}

        # --- clock / rate ---
        for alert in self.bucket.step(now):
            # SF-2: the AIMD mirror's alarm — silent permanent yielding is
            # indistinguishable from a dead bot.
            R.ntfy(alert, "lip_v5 %s: bucket %.2f req/s (cap %.2f)"
                   % (alert, self.bucket.b, self.bucket.cap_hz))
        if server_epoch is not None:                                          # B12
            skew = G.clock_skew_s(server_epoch, now)
            self.skew_ok = not G.clock_skew_alarming(skew)
            out["clock_skew_s"] = skew
            if not self.skew_ok:
                R.ntfy("clock_skew", "lip_v5 clock skew %.1fs" % skew)

        # --- metering: FIXED 1 Hz phase, independent of the quoting loop ---
        self.meter_tick(now, books or {})

        # --- BLOCKER-1: the fills feedback half, BEFORE the day stop and the reconcile so
        # a taker fill is in our books before anything judges the book against the wire ---
        out["fills_booked"] = self.poll_fills_due(now)
        self.pump_404(now)

        # --- SF-4: the operator's venue readings (credits ritual → ratchet) ---
        self.consume_readings(now)
        # --- SF-4b: accrued overrides — the exchange's displayed pot outranks the model ---
        try:
            _ov = R.read_json(C.ACCRUED_OVERRIDES_PATH, default=None)
            if isinstance(_ov, dict):
                for _pid, _usd in _ov.items():
                    self.accrued[str(_pid)] = float(_usd)
        except Exception:
            pass

        # --- day stop (B2) ---
        pnl = G.mark_to_market_pnl(self.positions, self.position_cost, yes_mids or {},
                                   self.cash.fees_paid)
        out["pnl"] = pnl
        out["unpriced"] = G.unpriced_positions(self.positions, yes_mids or {})
        # ── V6: THE LOSS STOPPER IS GONE; THE BUG ALARM IS WHAT HALTS. ────────────────
        # "Variance losses never halt the earner — the sizing priced them, and halting adds a
        # $0 day on top of the loss.  Model-impossible losses: the machine is broken."
        # (note 55).  So a breached day stop under v6 is an OBSERVATION, logged with the
        # numbers that make it model-consistent, and the two alarms in `alarm.py` are the only
        # things that can stop the book.
        _breached = G.day_stop_breached(pnl, self.projected_day_reward,
                                        ceiling_usd=self.ceiling_usd)
        if C.MARGINAL_QUEUE_ARMED:
            # THE BOUND'S "at risk" TERM, off the engine's own books — the same numbers the
            # day stop and the cluster report read, so an ADOPTED position (cost, no fill of
            # ours) is inside the bound instead of reading as impossible.
            _committed = (self.cash.resting_collateral
                          + max(self.cash.inventory_basis,
                                sum(self.position_cost.values())))
            _halt, _why, _nums = self.alarm.check(loss_usd=max(0.0, -pnl),
                                                  committed_usd=_committed)
            if _breached:
                R.log("loss_within_model", pnl=round(pnl, 4),
                      day_stop_usd=round(G.day_stop_usd(self.projected_day_reward,
                                                        ceiling_usd=self.ceiling_usd), 4),
                      halting=_halt, **_nums)
            if _halt:
                self.day_stopped = True
                self.halt.halt("bug_alarm", now, _nums)
                self.flatten(now)
                self.halt_flatten_done = True
                R.ntfy("bug_alarm", "lip_v6 BUG ALARM %s: %s" % (_why, _nums))
                out["bug_alarm"] = _why
                return out
        elif _breached:
            self.day_stopped = True
            self.halt.halt("day_stop", now, {"pnl": pnl})
            self.flatten(now)
            self.halt_flatten_done = True
            out["day_stop"] = True
            return out

        # --- drawdown (B3, equity fix per finish-round charter) ---
        # Drawdown measures LOSS, never deployment: resting collateral and inventory are
        # ASSETS (collateral at par, inventory at mark — `pnl` above already carries mark
        # minus cost), so equity = ceiling + realized + unrealized − fees.  The earlier form
        # added `raw_delta`, which counts every deployed dollar as GONE — full deployment at
        # zero loss read as a 100% drawdown and halted a healthy book at its first allocation.
        equity = self.ceiling_usd + self.cash.realized_pnl + pnl
        dd, breached = self.peak.observe(equity, now)
        out["drawdown"] = dd
        if breached and C.MARGINAL_QUEUE_ARMED:
            # Same ruling as the day stop: a drawdown is a LOSS MAGNITUDE, and the sizing
            # priced it.  Observed loudly, never halting; the bug alarm above is the halt.
            R.log("drawdown_within_model", drawdown=round(dd, 4),
                  peak=round(self.peak.peak, 4), **self.alarm.numbers())
            breached = False
        if breached:
            self.halt.halt("max_drawdown", now, {"drawdown": dd, "peak": self.peak.peak})
            self.flatten(now)
            self.halt_flatten_done = True
            return out

        # --- allocate → REQUOTE: THE OWNER'S LAW (Ryan, 2026-07-30) ---
        # The whole plan is `alloc.allocate_law`: rank every candidate by the capital needed
        # to earn $1.50 in the next 24 hours, fund cheapest-need first, one order per
        # cluster, $10 per market, $300 total.  Water-filling, r*, the forfeit gate, the
        # rescue, owner displacement/recall, the plan-side variance test and the idle sweep
        # are all DELETED — their jobs are inside the formula (accrued subtracts from the
        # need; a short window clamps at the $1.00 cliff; qualification is a priced cost).
        if slots:
            # The per-ORDER bound IS the per-market allocation (law §3/§4: a single order
            # may carry the whole $10 — "we will put all 10").  B9's turnover cap and the
            # cluster rail still bound what fills may accumulate.
            # v6: the per-ORDER bound is the CLUSTER RAIL — the knee is where money stops
            # (note 55), the rail is only the ruin guard, so a single market may legitimately
            # hold the whole rail when its marginal curve stays the best dollar on the board.
            self.slot_cap_usd = (self.cluster_rail_usd() if C.MARGINAL_QUEUE_ARMED
                                 else C.ALLOC_PER_MARKET_USD)
            self.phi_by_key = {s.key: s.phi for s in slots}
            for s in slots:
                if s.program_id is not None:
                    self.ticker_program[s.ticker] = s.program_id
                # SF-6: the turnover window is the slot's own PROGRAM PERIOD, set before
                # the requoter consults the B9 guard.
                if s.program_end_ts is not None and s.window_h:
                    self.refill.set_window(s.ticker, s.side,
                                           s.program_end_ts - s.window_h * 3600.0)
            # THE REQUOTE BUDGET IS READ OFF THE EXCHANGE'S OWN BOOK (law §4/§8): the
            # consumed part of each market's $10 is the inventory basis bought there —
            # `place_context().positions`, the identical rows the rails measure, so plan
            # and rail cannot disagree about what a market has already spent, and a restart
            # re-derives the same numbers (law §10: no state of our own).
            _pc = self.place_context()
            market_spent, cluster_spent = {}, {}
            for _p in _pc.positions:
                _usd = float(_p.get("n", 0)) * float(_p.get("basis", 0.0))
                market_spent[_p["ticker"]] = market_spent.get(_p["ticker"], 0.0) + _usd
                _ck = CL.cluster_of(_p["ticker"])
                cluster_spent[_ck] = cluster_spent.get(_ck, 0.0) + _usd
            # MBB's reserve is one copy of the LARGEST order (one allocation), and the
            # budget plans against what is GENUINELY available: held positions already
            # consume the ceiling (settlements release them back through reconcile — law
            # §8's capital events, arriving through the machinery that already exists).
            budget = alloc.reserve_budget(
                self.ceiling_usd - self.cash.inventory_basis, self.cluster_rail_usd())
            if C.MARGINAL_QUEUE_ARMED:
                a, spent, rep, cluster_cap = self.plan_marginal(
                    now, slots, budget, market_spent, cluster_spent)
                out["allocate"] = {"spent": spent, "slots": len(slots),
                                   "alloc_cap_usd": cluster_cap,
                                   "cluster_cap_usd": cluster_cap,
                                   "rail_usd": self.dials.rail_usd,
                                   "n_clusters": self.dials.n_clusters,
                                   "p": self.dials.p, "lam": rep.get("lam", 0.0),
                                   "funded": len(rep["funded"]),
                                   "reasons": rep["reasons"]}
            else:
                # The SAME cluster cap the rails read, inside the plan — an allocator that
                # plans what `place()` must refuse is not a plan.
                cluster_cap = self.cluster_rail_usd()
                a, spent, rep = alloc.allocate_law(slots, budget,
                                                   market_spent=market_spent,
                                                   cluster_spent=cluster_spent,
                                                   cluster_cap_usd=cluster_cap)
                out["allocate"] = {"spent": spent, "slots": len(slots),
                                   "alloc_cap_usd": C.ALLOC_PER_MARKET_USD,
                                   "cluster_cap_usd": cluster_cap,
                                   "funded": len(rep["funded"]),
                                   "reasons": rep["reasons"]}
            self.last_alloc = dict(a)
            out["alloc"] = a
            out["requote"] = self.requote_pass(now, slots, a)
            out["accrued"] = self.integrate_accrual(now, slots, a)
            # SF-1: the day stop's scale is OUR projected accrual — share × ρ/2 over the
            # slots we actually fund (allocated or resting) — never the board's pools.  A
            # board-pool projection saturated the stop at $150 against a ≤$60 launch
            # deployment: a stop that could never trip.  Computed AFTER allocation, so it
            # scales the NEXT cycle's stop with what is genuinely at risk.
            self.projected_day_reward = self.project_day_reward(slots, a)
            out["projected_day_reward"] = self.projected_day_reward

        # ── STAGE 5: THE BOOK SNAPSHOT IS GONE (2026-07-30). ──────────────────────────
        # It persisted our own resting rungs so a restart could re-place them.  Nothing
        # world-fact needed it: market closes have their own cache (which IS world memory
        # and stays), and positions come from the exchange.  A file whose only reader was
        # replay dies with replay.
        # --- cash feed cadence (30 s heartbeat; every wire call publishes anyway) ---
        if self.publisher.due(now):
            self.publisher.publish(now)

        # --- settlement-cash timeout (§5.2a) ---
        for tk in self.cash.unconfirmed_overdue(now):
            R.ntfy("settlement_cash_unconfirmed", "lip_v5 settlement cash unconfirmed: %s" % tk)

        # --- B10: bounded UNKNOWN retries, then conservative book + freeze ---
        for oid in self.unknown.due(now):
            self.unknown.attempted(oid, now)
            self.cancel(oid, now, lane="exit_cancel")
        for oid, e in self.unknown.exhausted():
            self.book_fill(e["ticker"], e["side"], e["remaining"],
                           self.orders.get(oid, {}).get("price", 0.0), now,
                           fill_id="assume:%s" % oid)
            self.frozen.add(e["ticker"])
            self.ledger.write("assume_filled", ticker=e["ticker"], order_id=oid)
            R.ntfy("assume_filled", "lip_v5 assume_filled %s" % e["ticker"])
            self.unknown.resolved(oid)

        # --- recon (the truth-reader; never dropped, only slowed) ---
        # A CADENCE IS SPENT BY A READ, NOT BY AN ATTEMPT.  `last_recon` advanced even when
        # reconcile() returned None — which it does for the two states that mean IT NEVER
        # LOOKED: the verify lane rate-refused the admit, or the positions call did not come
        # back 200.  Both were charged the full RECON_POSITIONS_S, so a single refused
        # attempt blinded the truth-reader for 120 s, and a lane under sustained pressure
        # (exactly when divergence is most likely) could burn window after window without
        # ever reading the exchange.  The comment above already says the rule — "never
        # dropped, only slowed" — the clock just wasn't keeping it.  MIRROR (recon too OFTEN
        # ↔ too rarely): the rate bucket is what bounds the retries, so a refused reconcile
        # re-asks next cycle and is refused cheaply until the lane has room, while a
        # SUCCESSFUL read still costs the full cadence.
        if now - self.last_recon >= C.RECON_POSITIONS_S:
            if self.reconcile(now) is not None:
                self.last_recon = now
                # The adverse-selection tripwire rides the recon cadence (owner, 2026-07-30:
                # reuse a cadence, don't invent one) — and rides the SUCCESSFUL read, so its
                # numbers describe books the wire just verified.
                self.fill_selection_tripwire(now)
        # …and the other half of the same question: does the wire still hold our ORDERS?
        self.sync_orders(now)

        # --- health read-out ---
        out["clusters"] = CL.cluster_report(
            [{"ticker": t, "side": leg, "n": abs(p[leg]),
              "basis": self.entry_basis.get((t, leg), 0.0)}
             for t, p in self.positions.items() for leg in ("yes", "no") if abs(p[leg]) > 0],
            self.cluster_rail_usd())
        out["bucket_hz"] = self.bucket.b
        out["halted"] = self.halt.halted
        out["rollback_clean"] = self.rollback.clean
        R.log("cycle", **{k: v for k, v in out.items() if k != "alloc"})
        return out

    def poll_estimates(self, now):
        """SF-4c — one /v1 estimates read per ESTIMATES_POLL_S on the verify lane.

        Each poll RE-ANCHORS `self.accrued` to the exchange's own per-program number (the
        UI popover's source, centicents = 1e-4 dollars); `integrate_accrual`'s model deltas
        keep it moving between polls.  Changes ≥ half a cent persist as `accrual` money rows
        (src=exchange) so restarts replay TRUTH, not the model.  The model was measured 2-4x
        off in both directions on 2026-07-30 — it survives only as the between-polls
        interpolator and the no-user-id fallback.
        """
        if not C.KALSHI_USER_ID:
            R.log_once("estimates_unwired", note="KALSHI_USER_ID unset: accrual runs on the "
                                                 "model alone, measured 2-4x off — set it")
            return 0
        if self.last_estimates_poll is not None and \
                float(now) - self.last_estimates_poll < C.ESTIMATES_POLL_S:
            return 0
        admitted, _ = self.bucket.admit("verify", now)
        if not admitted:
            R.log_once("estimates_rate_refused", note="verify lane refused the poll")
            return 0
        status, body = self.ex.estimates(C.KALSHI_USER_ID)
        self.note_http(status, now)
        if status != 200:
            R.log_once("estimates_poll_failed", status=status)
            return 0
        self.last_estimates_poll = float(now)
        n = 0
        for row in (body or {}).get("estimates") or []:
            pid = str(row.get("program_id"))
            usd = float(row.get("reward_centicents") or 0) * 1e-4
            if abs(usd - float(self.accrued.get(pid, 0.0) or 0.0)) >= 0.005:
                self.ledger.write("accrual", program_id=pid, accrued=round(usd, 6),
                                  src="exchange_estimates")
                n += 1
            self.accrued[pid] = usd
        return n

    def meter_tick(self, now, books):
        """1 Hz on a FIXED PHASE, never triggered by our own action (spec §2.1's mirror)."""
        sec = int(now)
        if self.last_meter_tick == sec:
            return False
        self.last_meter_tick = sec
        obs = {}
        for o in self.orders.values():
            if o.get("remaining", 0) <= 0 or o.get("gone_404"):
                continue                      # a 404'd order is NOT resting on the wire
            key = (o["ticker"], o["side"])
            best = (books.get(o["ticker"]) or {}).get(o["side"])
            # Both sides on the YES axis.  "Behind" points OPPOSITE ways per side: a bid is
            # behind when BELOW the best bid, an ask when ABOVE the best ask.  One sign for
            # both would grade every off-best ask as at-best (mirror of the bid case).
            if best is None:
                ticks_behind = 0
            elif o["side"] == "bid":
                ticks_behind = max(0, int(round((best - o["price"]) * 100)))
            else:
                ticks_behind = max(0, int(round((o["price"] - best) * 100)))
            e = obs.setdefault(key, {"orders": [], "net_position": 0.0, "entry_basis": 0.0})
            e["orders"].append({"remaining": o["remaining"], "price": o["price"],
                                "ticks_behind": ticks_behind})
        for t, p in self.positions.items():
            for leg in ("yes", "no"):
                if abs(p.get(leg, 0.0)) <= 0:
                    continue
                key = (t, "bid" if leg == "yes" else "ask")
                e = obs.setdefault(key, {"orders": [], "net_position": 0.0,
                                         "entry_basis": 0.0})
                e["net_position"] = abs(p[leg])
                e["entry_basis"] = self.entry_basis.get((t, leg), 0.0)
        self.meter.tick(now, obs)
        if self.meter.due(now):
            rows = self.meter.flush(now)
            self.persist.write(self.presence_log.write_rows, rows, now)
        return True

    def sync_orders(self, now):
        """THE WIRE IS THE TRUTH ABOUT WHICH OF *OUR* ORDERS STILL REST — AND ONLY THAT.

        **ONE DIRECTION ONLY (owner decision, 2026-07-30).**  This pass may DROP from
        `self.orders` what the exchange's complete list no longer carries.  It may NEVER add:
        a wire order this process did not place is not ours, whatever prefix it wears.  That
        is the same law the deleted startup sweep broke, and without this end the law would
        simply be re-broken one reconcile cadence after boot — the shared account carries
        nestor's orders and our own pre-restart leftovers, and neither is a decision this
        process made.  Structurally it holds because the only loop below iterates
        `set(self.orders) - seen`; `seen` is used to SUBTRACT and is never a source of rows.

        Found by the convergence acceptance test, 2026-07-30: cancel every order
        exchange-side and the book NEVER CAME BACK.  Not because re-derivation was wrong —
        it was never asked.  `self.orders` was written once at recovery and thereafter only
        by our own actions, so a hand flatten, an exchange sweep or a cancel-all from
        another console left us believing we still had presence we did not have.  The
        requoter saw that phantom presence and declined to re-place, forever.

        That is the disease in its purest form and in the last place anyone looked: our own
        order book is MEMORY OF OUR OWN PAST DECISIONS, and it was the one piece of state
        never checked against the world.  Positions had `reconcile`; orders had nothing.

        A resting order the exchange's own complete list does not carry is exactly the
        evidence a 404 on cancel gives, so it goes through the SAME §9.4a disambiguation
        rather than a second one: fills explain it (book them), or two clean reads 36 s apart
        say expired (collateral home, order terminal).  Never popped silently — a phantom
        fill and a phantom order are both books that disagree with the wire.
        CADENCE, DERIVED: `RECON_POSITIONS_S`.  This asks the same question `reconcile` asks
        — does the wire agree with our books — about the other half of the same book, so it
        inherits that answer rather than inventing a second one.  The same window doubles as
        the propagation margin: an order placed inside the last cadence is not yet expected
        to appear, and is skipped.
        """
        if float(now) - self.last_orders_sync < C.RECON_POSITIONS_S:
            return None
        admitted, _ = self.bucket.admit("verify", now)
        if not admitted:
            return None                       # a cadence is spent by a READ, not an attempt
        status, body = self.ex.orders()
        self.note_http(status, now)
        if status != 200:
            return None
        self.last_orders_sync = float(now)
        seen = set()
        for row in (body or {}).get("orders") or []:
            if R.owns_coid(row.get("client_order_id") or ""):
                seen.add(str(row.get("order_id")))
        gone = 0
        for oid in sorted(set(self.orders) - seen):
            o = self.orders[oid]
            placed = float(o.get("placed_ts", 0.0) or 0.0)
            if placed == 0.0 or o.get("gone_404"):
                continue
            if float(now) - placed < C.RECON_POSITIONS_S:
                continue                      # inside the propagation margin
            R.log("order_gone_from_wire", order_id=oid, ticker=o.get("ticker"),
                  side=o.get("side"), remaining=o.get("remaining"),
                  why="the exchange's resting list does not carry it")
            self.note_cancel_404(oid, o, now)
            gone += 1
        return gone

    def reconcile(self, now):
        admitted, _ = self.bucket.admit("verify", now)
        if not admitted:
            return None
        status, body = self.ex.positions()
        self.note_http(status, now)                           # SF-2
        if status != 200:
            return None
        exch = {}
        for row in (body.get("market_positions") or []):
            # 2026-07-30 wire dialect: `position_fp` (fractional dollar-string) replaced
            # `position` — the old read returned 0 for EVERY market, which BLINDED the
            # divergence check exactly while phantom inventory (~$198 of assume_filled
            # double-books from the broken-fills era) starved the budget.  Same class as
            # count_fp; found from Ryan's portfolio screenshot.
            _pfp = row.get("position_fp")
            exch[row.get("ticker")] = float(_pfp) if _pfp is not None                 else float(row.get("position", 0))
        # ── SETTLEMENTS BEFORE THE TRUE-UP.  A settled ticker's positions row (if listed
        # at all) reads 0 — exactly the shape of a full true-down — and judging it there
        # first would zero the books SILENTLY: `cash.inventory` gets n=0, `inventory_basis`
        # drops with NO realized P&L booked, and `delta_dollars` rises by the basis before
        # any cash is confirmed — publishing above the truth, the one forbidden direction
        # (T-C2), on every losing settlement.  The settlements read is the exchange's own
        # settlement record and it carries the PAID amount, so it outranks the positions
        # delta it explains; process it first and the divergence loop below never sees a
        # settled ticker as a divergence.
        ss, srows = self.ex.settlements()
        self.note_http(ss, now)                               # SF-2
        if ss == 200:
            for row in (srows.get("settlements") or []):
                tk = row.get("ticker")
                if not tk:
                    continue
                # `revenue` is CENTS on the wire.  A row MISSING the field entirely is a
                # settlement signal with no cash statement: it resolves (the market IS
                # settled) but confirms nothing — settle_ticker's paid=None path.
                rev = row.get("revenue")
                self.settle_ticker(tk, None if rev is None else float(rev) / 100.0, now,
                                   src="settlements_row")
        # ── THE TRUE-UP (Ryan, 2026-07-30: "probe every minute and true up the ledger").
        # The exchange's read of our positions is the truth; the only question is which
        # DIRECTION the divergence runs.  DOWN (they show less than we book) is the safe
        # direction — a hand cancel, a hand sale — and is ADOPTED silently: freezing on it
        # is what made hand intervention "just fucking halt and explode".
        # UP (they show more than we book) is an unexplained acquisition — the dangerous
        # direction — and still freezes and pages.  ONLY tickers the response explicitly
        # lists are judged: truing-down on ABSENCE would zero real inventory on any partial
        # or paged response (and did exactly that to replay-held books in the fixtures).
        # Settled tickers leave via the settlement path above/below, never through here.
        for t in exch:
            if t in self.resolved:
                # A DETERMINED MARKET IS NOT A DIVERGENCE.  Its exit is the settlement
                # path: freezing it pages a human about the exchange doing its job, and
                # truing it down leaves basis behind (see the ordering note above).
                continue
            n = exch.get(t, 0.0)
            ours = self.positions.get(t, {})
            our_net = ours.get("yes", 0.0) - ours.get("no", 0.0)
            if abs(our_net - n) <= 0.5:
                continue
            if abs(n) < abs(our_net) and (n == 0 or (n > 0) == (our_net > 0)):
                if n == 0:
                    # A POSITION GONE TO ZERO WITH NO OBSERVED CLOSING FILL IS EITHER A
                    # HAND SALE OR A SETTLEMENT, and the exchange can say which: ask the
                    # market's own status (never a clock — a market we merely believe has
                    # resolved is the naked-exposure case).  Settled ⇒ the settlement
                    # path: basis moves to settled_awaiting_payout (budget frees, the
                    # published cash stays consumed — T-C2), the books close coherently,
                    # and the cash release waits for the settlements row, the one exact
                    # release path in shared mode.  The fills-poll case cannot land here:
                    # reconcile runs after poll_fills_due, so an observed closing fill has
                    # already trued the books and there is no divergence to judge.
                    settled, result = self.market_settled(t, now)
                    if settled:
                        inv = self.cash.inventory.get(t) or {}
                        n_held = abs(float(inv.get("n", 0.0)))
                        # The held LEG is the book's fact (`our_net` sign) — the cash
                        # feed's `inventory[n]` is a contract COUNT booked positive for
                        # both legs by book_fill/adopt, so its sign says nothing here.
                        held_leg = "yes" if our_net > 0 else "no"
                        if result in ("yes", "no"):
                            # The exchange named the outcome: a matching leg pays $1.00 a
                            # contract, the other $0.00 — exact, not estimated.
                            exp = n_held * (1.0 if result == held_leg else 0.0)
                        else:
                            # No result field: n × $1.00, the inventory_settle_max
                            # convention — the LARGEST credit that could land.  Over-
                            # stating widens the positive band only and makes the
                            # balance-path release HARDER, both the safe side.
                            exp = n_held * 1.00
                        self.settle_ticker(t, None, now,
                                           src="position_zero_determined",
                                           expected_usd=exp)
                        continue
                leg = "yes" if our_net > 0 else "no"
                new_leg = abs(n) if (n > 0) == (our_net > 0) or n == 0 else 0.0
                pos = self.positions.setdefault(t, {"yes": 0.0, "no": 0.0})
                basis = self.entry_basis.get((t, leg), 0.0)
                removed = pos.get(leg, 0.0) - new_leg
                pos[leg] = new_leg
                self.position_cost[t] = max(
                    0.0, self.position_cost.get(t, 0.0) - removed * basis)
                self.cash.inventory[t] = {"n": (new_leg if leg == "yes" else -new_leg),
                                          "basis": basis}
                self.ledger.write("position_divergence", ticker=t, ours=our_net,
                                  exchange=n, action="trued_down")
                R.log("position_trued_down", ticker=t, ours=our_net, exchange=n)
            else:
                # OUR OWN RESTING SIZE IS NOT AN UNEXPLAINED ACQUISITION.  The UP direction
                # means the exchange shows MORE than we book, which is dangerous — EXCEPT in
                # the one window every fill passes through: the taker crosses, the exchange's
                # position moves immediately, and we only learn it FILLS_POLL_S later.  Any
                # excess no larger than what we currently believe is RESTING on this ticker
                # is exactly that unpolled fill, so it defers to the fills poll instead of
                # freezing the market the fill just made productive (test_fixround: "a fill
                # NEVER freezes its own market" — the reviewer's original repro).  This mirror
                # was previously supplied by ACCIDENT: a rate-refused reconcile burned the
                # whole 120 s cadence, which usually skipped past the propagation window; now
                # that a refusal retries next cycle, the guard has to be real.  It cannot hide
                # a true divergence: once the fill IS observed the order is gone from
                # self.orders, resting drops to 0, and any remaining excess freezes and pages
                # on the next pass.
                resting = sum(float(o.get("remaining", 0.0))
                              for o in self.orders.values()
                              if o.get("ticker") == t and not o.get("gone_404"))
                if abs(n - our_net) <= resting + 0.5:
                    R.log("position_divergence_deferred", ticker=t, ours=our_net,
                          exchange=n, resting=resting,
                          why="excess fits our unpolled resting size")
                    continue
                R.log("position_divergence", ticker=t, ours=our_net, exchange=n)
                R.ntfy("assume_filled", "lip_v5 position divergence %s" % t)
                self.frozen.add(t)
        sb, bal = self.ex.balance()
        self.note_http(sb, now)                               # SF-2
        if sb == 200:
            self.cash.observe_balance(float(bal.get("balance", 0)) / 100.0, now)
        return exch

    # =========================================================================================
    # SETTLEMENT RELEASE — the exit every position that is not shed takes.  Daily treasuries
    # settle EVERY AFTERNOON; without this the basis stayed in `inventory_basis` forever,
    # the budget (`ceiling − inventory_basis − resting`) starved on capital Kalshi had
    # already paid back, and `cashfeed.resolve()` had zero call sites.
    # =========================================================================================
    def market_settled(self, ticker, now):
        """One market read: (settled, result).  `settled` is TRUE only for the exchange's
        own settled word (config.MARKET_SETTLED_STATUSES) — an error, a 404 or any other
        status is (False, None), because settlement inferred from anything but the
        exchange's statement is the naked-exposure case.  Costs one public read on the
        verify lane's admit that reconcile already holds, only ever asked for the rare
        held-position-went-to-zero transition."""
        status, body = self.ex.market(ticker)
        self.note_http(status, now)                           # SF-2
        if status != 200:
            return False, None
        mkt = (body or {}).get("market") or body or {}
        st = str(mkt.get("status") or "").lower()
        result = str(mkt.get("result") or "").lower()
        return (st in C.MARKET_SETTLED_STATUSES), (result if result in ("yes", "no")
                                                   else None)

    def settle_ticker(self, ticker, paid_usd, now, src, expected_usd=None):
        """The ONE settlement entry point, from either signal the exchange gives us — a
        `/portfolio/settlements` row (paid amount known, possibly an explicit zero) or a
        determined market whose position row went to zero (paid unknown ⇒ `paid_usd=None`).
        Idempotent against the settlements endpoint returning the FULL tape every poll:
        with no inventory and no pending claim the call is a no-op beyond `resolved`.

        Two steps, and each may fire without the other:

          RESOLVE (inventory held): the basis leaves `cash.inventory` for
          `settled_awaiting_payout` — the BUDGET frees NOW (allocation reads
          `ceiling − inventory_basis`) while `delta_dollars` still counts the basis as
          consumed (T-C2: the published number moves only on cash confirmation).  The
          engine books close coherently in the same step (positions/entry_basis/
          position_cost), the cluster stops charging it (`resolved`), and a `settlement`
          ledger row (released=False) makes replay rebuild exactly this state.

          RELEASE (pending claim + a cash statement): a paid amount > 0 is §5.2a's exact
          confirmation (`settlement_row`); an explicit zero is a LOST position with
          nothing to wait for (`settlement_zero`).  Realized P&L = payout − basis lands in
          `cash.realized_pnl` — the drawdown guard's equity term, so a winning settlement
          RAISES equity and can never read as a loss — and the released `settlement` row
          (released=True) carries it for replay.  `paid_usd=None` releases nothing: no
          cash statement, no release, the 6 h `settlement_cash_unconfirmed` page bounds
          the wait.
        """
        inv = self.cash.inventory.get(ticker)
        held = inv is not None and abs(float(inv.get("n", 0.0))) > 1e-9
        changed = False
        if held:
            n_held = abs(float(inv.get("n", 0.0)))
            expected = float(expected_usd) if expected_usd is not None else \
                (float(paid_usd) if paid_usd is not None else n_held * 1.00)
            basis = self.cash.resolve(ticker, expected, now)
            self._close_settled_position(ticker)
            self.ledger.write("settlement", ticker=ticker, n=n_held,
                              basis_usd=round(basis, 6),
                              expected_usd=round(expected, 6), released=False, src=src)
            R.log("settlement_resolved", ticker=ticker, n=n_held,
                  basis_usd=round(basis, 6), expected_usd=round(expected, 6), src=src)
            changed = True
        p = self.cash.pending.get(ticker)
        if p is not None and paid_usd is not None:
            paid = float(paid_usd)
            basis_p = p.basis_usd
            released = self.cash.settlement_row(ticker, paid) if paid > 0 \
                else self.cash.settlement_zero(ticker)
            if released:
                realized = paid - basis_p
                # ALARM 2's OBSERVATION: realised loss on this settled position, positive for
                # a loss.  It is the only place the table's prediction is ever scored.
                self.alarm.observe_settlement(-realized)
                self.ledger.write("settlement", ticker=ticker, paid_usd=round(paid, 6),
                                  realized_usd=round(realized, 6), released=True, src=src)
                R.log("settlement_released", ticker=ticker, paid_usd=round(paid, 6),
                      realized_usd=round(realized, 6), src=src)
                changed = True
        if changed:
            self.resolved.add(ticker)
            self.publisher.publish(now)
        else:
            # History (the tape replays every poll) or someone else's settlement in the
            # shared account: remember the settled word — it keeps the divergence loop and
            # this dedupe honest — but write and publish nothing.
            self.resolved.add(ticker)
        return changed

    def _close_settled_position(self, ticker):
        """The engine-book half of RESOLVE, kept in one place so positions, entry_basis
        and position_cost can never part ways: the day stop reads
        `mark_to_market_pnl(positions, position_cost, …)`, and removing one without the
        other reads a settled winner as `value 0 − cost basis` — a phantom loss the size
        of the position, i.e. a day stop tripped by the exchange paying us."""
        self.positions.pop(ticker, None)
        self.entry_basis.pop((ticker, "yes"), None)
        self.entry_basis.pop((ticker, "no"), None)
        self.position_cost.pop(ticker, None)
        # SETTLEMENT IS NOW THE ONLY EXIT, so there is no shed state to unwind here and no
        # L_shed sample to suppress.  (The old code had to be careful that a settlement's
        # reach-flat was not mistaken for shed evidence; with no sheds the question is moot.)

    def venue_retired(self, ticker):
        """True when a ticker is one the strategy has decided against — denied family, or a
        program whose window the scanner now refuses.  Distinct from "not in this cycle's
        table", which is usually just classify cadence: only a REASON to be gone counts, so a
        cadence gap can never cancel a healthy quote."""
        if C.series_denied(ticker):
            return True
        return ticker in self.retired_tickers

    def venue_of(self, ticker):
        """A venue is the SERIES (spec §1.1) — the coarsest key at which settlements
        accumulate.  Derived from the ticker so it works for held inventory and resting
        orders alike, without needing a slot to be present in this cycle's table."""
        return str(ticker or "").split("-", 1)[0].upper()

    def venue_reading(self, venue, reading_usd, projection_usd, now, settlement_day=None,
                      src=None, line_no=None):
        """A MEASUREMENT of what a venue actually paid us, and the only per-venue memory that
        survives stage 1.

        Ryan, 2026-07-30: "we can just ask kalshi how much we've earned there, we only need
        to know that we've been there."  What died with venue admission is the LADDER — the
        rung, the probe, the verified flag, the oversized slot — because a rung is a memory
        of our own past decisions and the book may not be a function of those.  What survives
        is the comparison itself: the exchange tells us what it paid, the plan tells us what
        it projected, and a venue that takes our presence and does not pay for it is a FACT
        about that venue, not a permission we granted or withheld.

        `classify_reading` keeps its derived band (VERIFY_BAND) and OUT_OF_REACH rule
        unchanged — they were always measurement, never permission.  DISAGREE on
        STANDDOWN_DAYS consecutive settlement days (the same derived rule the ratchet used,
        for the same reason: two disagreements inside one afternoon are one day's evidence
        wearing two hats) writes a MEASURED DENY, which joins the static deny list as an
        input to slot building.  It carries its evidence so an operator can argue with it.
        MIRROR (denying on measurement ↔ never denying): a false deny costs the rate of one
        venue and is visible in the row that created it; never denying is the PayPal geometry
        — presence bought at a venue that has already told us, in dollars, that it does not
        pay.  Nothing here funds anything: a venue not denied competes on its numbers like
        every other, with no memory of having been "admitted".
        """
        verdict, ratio = RT.classify_reading(reading_usd, projection_usd)
        self.venue_measured[venue] = {"reading": reading_usd,
                                      "projection": projection_usd, "ratio": ratio}
        fields = {"venue": venue, "verdict": verdict, "ratio": ratio,
                  "reading": reading_usd, "projection": projection_usd,
                  "src": src, "line_no": line_no}
        if verdict == RT.OUT_OF_REACH:
            # The projection never cleared the entry floor, so the venue was never asked a
            # question it could answer.  Silence is not a disagreement (ratchet.py's own
            # note): no deny, no day counted.
            self.ledger.write("venue_measured", **fields)
            self.ledger.write("venue_out_of_reach", venue=venue,
                              projection=projection_usd)
            R.ntfy("venue_out_of_reach", "lip_v5 venue out of reach: %s" % venue)
            return verdict
        days, last_day = self._venue_disagree.get(venue, (0, None))
        if verdict == RT.VERIFY:
            days, last_day = 0, None          # it paid: the count resets on evidence
        else:
            if settlement_day is None:
                days += 1
            elif settlement_day != last_day:
                days = days + 1 if (last_day is not None
                                    and RT._is_next_day(last_day, settlement_day)) else 1
                last_day = settlement_day
        self._venue_disagree[venue] = (days, last_day)
        fields["disagree_days"] = days
        if verdict != RT.VERIFY and days >= int(C.STANDDOWN_DAYS):
            self.measured_deny[venue] = dict(fields)
            fields["denied"] = True
            self.ledger.write("venue_denied_measured", **fields)
            R.ntfy("venue_stand_down",
                   "lip_v5 venue DENIED on measurement: %s (paid %.4f of %.4f projected)"
                   % (venue, float(reading_usd or 0.0), float(projection_usd or 0.0)))
        self.ledger.write("venue_measured", **fields)
        return verdict

    def venue_denied(self, ticker):
        """The measured half of the deny test (`config.series_denied` is the static half)."""
        return self.venue_of(ticker) in self.measured_deny

    def consume_readings(self, now):
        """SF-4 — the operator's entry point for venue readings: a WATCHED FILE
        (`v5_readings.jsonl`, mirror of v5_go.json's hand-written pattern).  The credits
        ritual appends rows `{"venue","reading_usd","projection_usd","settlement_day"?,
        "program_id"?,"paid"?}`; the live process consumes each row exactly once.

        Restart idempotence: every applied reading writes a `ratchet` ledger row carrying
        `src="readings_file"` + `line_no`; recovery replays those rows, so `readings_line`
        resumes past everything already applied and a restart can never double-move the
        ladder.  MIRROR (a row applied twice ↔ a row never applied): line accounting guards
        the first; consuming EVERY cycle (a stat() when unchanged) bounds the second at one
        cycle of latency.  A malformed row is logged and SKIPPED but still counted — a bad
        line must not wedge the file forever.
        """
        path = os.path.join(self.data_dir, C.READINGS_NAME)
        try:
            stt = os.stat(path)
        except OSError:
            return 0
        key = (stt.st_mtime, stt.st_size)
        if key == self._readings_stat:
            return 0
        self._readings_stat = key
        rows = R.read_jsonl(path)
        if len(rows) <= self.readings_line:
            return 0
        applied = 0
        for i in range(self.readings_line, len(rows)):
            row, line_no = rows[i], i + 1
            try:
                venue = str(row["venue"])
                reading = float(row["reading_usd"])
                projection = float(row["projection_usd"])
            except (KeyError, TypeError, ValueError):
                R.log("reading_bad_row", line_no=line_no)
                continue
            self.venue_reading(venue, reading, projection, now,
                               settlement_day=row.get("settlement_day"),
                               src="readings_file", line_no=line_no)
            if row.get("paid"):
                self.credit_paid(row.get("program_id"), reading, now)
            applied += 1
        self.readings_line = len(rows)
        return applied

    def credit_paid(self, program_id, paid_usd, now):
        """N3 — a PAID credit retires the accrued-unpaid claim it satisfies: the cash
        feed's positive band shrinks (`reward_paid`) and the program's accrued memory is
        drawn down and re-persisted, so replay and the live book agree that this accrual
        has become cash."""
        paid = max(0.0, float(paid_usd))
        self.cash.reward_paid(paid)
        if program_id is not None and program_id in self.accrued:
            newv = round(max(0.0, self.accrued[program_id] - paid), 6)
            self.accrued[program_id] = newv
            self.ledger.write("accrual", program_id=str(program_id), accrued=newv)
            self._accrual_written[program_id] = newv
        R.log("credit_paid", program_id=program_id, paid=paid)
        return paid

    # =========================================================================================
    # POSITIONS (there is no exit path here — see `requote_pass`)
    # =========================================================================================
    def net_position(self, ticker):
        p = self.positions.get(ticker) or {}
        return float(p.get("yes", 0.0)) - float(p.get("no", 0.0))

    # ── `run_pending_triage` AND `update_shed_targets` ARE GONE. ─────────────────────────
    # They were the two auto-exit paths, and both are deleted by the same decision (owner,
    # 2026-07-30: "it's either running and placing orders, or it's not running").
    #
    # `run_pending_triage` converted a cutover verdict of MAKER_SHED into membership of
    # `triage_shed`.  Measured live 2026-07-30 morning: after the truth-resync adoption it
    # sentenced freshly-adopted positions to leave at the opposing best — 26 Skubal YES
    # offered at 3c against a 16c basis.
    #
    # `update_shed_targets` was the SECOND, independent path: any inventory whose venue fails
    # (★) NOW gets a shed target.  Its own docstring argued that the carry term is
    # forward-looking so "the dollars are already there changes nothing about where they
    # should be".  That is wrong in the one term it omits: LEAVING COSTS THE SPREAD, and the
    # spread is not in (★).  Priced on our tape, exiting cost −$40.30 on the +2c leg and
    # −$123 to flatten instantly; the same day it crossed to a 95c ask to close an 18c-basis
    # NO (a guaranteed −$2.80).
    #
    # THE MIRROR THAT USED TO JUSTIFY THEM ("locked inventory ↔ paying to leave") is answered
    # by the D4 settlement gate, not by an exit: every position this book can hold settles
    # within 7 days, so the worst case of HOLDING is bounded and is measured cheaper than the
    # worst case of SELLING.  Positions ride.  L_shed can therefore never be measured again,
    # which `money.l_eff_h` already handles exactly right: `l_shed_h is None ⇒ ∞`, so `l_eff`
    # falls back to the real horizon (close + settle lag), which is now the truth.

    # =========================================================================================
    # THE REQUOTING STAGE (charter A) — diff the post-forfeit-gate allocation against the
    # resting book; every emission goes through place()/cancel(), the rails stay the one path.
    # =========================================================================================
    def requote_pass(self, now, slots, alloc_map):
        """One requote pass.  Returns the read-out {placed, cancelled, skipped}.

        **EVERY ORDER THIS PASS EMITS IS AN OPENING MAKER QUOTE.**  There is no exit branch,
        no shed target and no closing size (owner decision, 2026-07-30).  `q` is the
        allocation, full stop — it used to be `max(alloc, shed)`, and the shed half is gone.

        THE DISTINCTION THAT MUST NOT BE LOST: a slot whose side is "ask" is still quoted
        exactly as before.  An ask is NO-side collateral posted to earn the NO half of the
        pool; it opens a position, it does not close one.  What was deleted is the case where
        the SIZE came from inventory we hold and the PRICE came from the opposing best —
        that, and only that, was the sale."""
        stats = {"placed": 0, "cancelled": 0, "skipped": 0}
        slot_by_key = {s.key: s for s in slots}
        live_by_slot = {}
        for oid, o in sorted(self.orders.items()):
            # gone_404 orders are NOT on the wire (the exchange said so) — counting them as
            # presence would suppress the very replenish their fill should trigger.
            if o.get("remaining", 0) > 0 and not o.get("gone_404"):
                live_by_slot.setdefault((o["ticker"], o["side"]), []).append(o)

        def target_q(s):
            """The size this pass intends for `s`, hoisted so the ORDER of application can be
            derived from it (see `release_first`).  It is the allocation and nothing else."""
            return int(alloc_map.get(s.key, 0))

        def release_first(s):
            """NEW-1's residual: **RELEASES PRECEDE CLAIMS.**  An allocation is a
            SIMULTANEOUS statement, but the pass applies it one slot at a time, and the caps
            at `place()` see whatever has not been released yet.  Walking slots by ticker
            therefore let a plan whose TOTAL fits its cluster be refused for an accident of
            alphabet — the earlier rung's claim measured against the later rung's dead
            collateral — costing the whole cluster its presence for a cycle at exactly the
            moment the water level decided to move.  Shrinking first is FREE: a cancel is a
            reduction and no cap can refuse it, so this ordering can only admit strictly more
            of a plan the rails already approved in aggregate.
            MIRROR (release-first ↔ claim-first): claim-first would hold presence while the
            replacement is proven — which is exactly what make-before-break already does
            WITHIN a slot, and it is per-slot that presence is worth protecting.  ACROSS
            slots there is no presence to protect: the dollars are leaving that rung by the
            water level's own decision."""
            cur_total = sum(o.get("remaining", 0.0)
                            for o in (live_by_slot.get(s.key) or []))
            return (0 if target_q(s) < cur_total else 1, s.ticker, s.side)

        for s in sorted(slots, key=release_first):
            key = s.key
            if s.close_ts is None:
                continue                      # no close ⇒ no expiration backstop ⇒ no quote
            exp = int(s.close_ts - C.CLOSE_MARGIN_S)
            if exp <= now:
                continue                      # window END: the backstop would be in the past
            cur_list = sorted(live_by_slot.get(key) or [],
                              key=lambda o: o.get("placed_ts", 0.0))
            # Self-heal: >1 live order on a slot (an MBB cancel that was rate-refused).
            # Cancel the OLDER ones first; the newest is the quote.
            for stale in cur_list[:-1]:
                ok, _ = self.cancel(stale["order_id"], now, lane="requote_cancel")
                if ok:
                    stats["cancelled"] += 1
            cur = cur_list[-1] if cur_list else None

            q_alloc = int(alloc_map.get(key, 0))
            q = q_alloc
            if q <= 0:
                if cur is not None:
                    if s.S <= 0 and float(s.cum_size) >= float(s.target_size):
                        # SF-5 consequence, spec §4.5: S here is the RIVAL score, and zero
                        # rivals means WE are the qualifying side — ALLOCATE is right about
                        # size and wrong about entry.  Cancelling would un-qualify the
                        # snapshot we are being paid to create; the resting minimum stays.
                        R.log("sole_qualifier_hold", ticker=s.ticker, side=s.side,
                              remaining=cur["remaining"])
                        stats["skipped"] += 1
                        self.slot_examined[key] = now
                        continue
                    # ── A PLAN-DRIVEN ZERO RESPECTS THE MINIMUM RESTING LIFE (2026-07-30,
                    # final-round review; production halted twice this week on B14
                    # place_burst, 18:33 MT on KXTRUMPSAYCOMPANY the latest).  This branch
                    # cancelled a 1-second-old order because the ALLOCATOR flapped — DONE,
                    # unaffordable this pass, budget moved, cluster seat lost — and the
                    # next flap re-placed BLIND: measured, blind placements at t=0,2,4,
                    # three inside five seconds, which is B14's trip count reached exactly,
                    # with the surviving margin supplied by the cancel-share rail's
                    # parameters rather than by design.  The trigger path already embodies
                    # the principle (plan triggers are stripped under MIN_RESTING_LIFE_S;
                    # only book events override) and a plan-driven zero is not a book event
                    # either.
                    # THE BOUND, derived: place at t ⇒ earliest plan-driven cancel t+30 ⇒
                    # earliest blind re-place after t+30, so a rung's blind placements are
                    # >= 30 s apart — at most TWO strictly inside any 60 s window, and B14
                    # (3 in 60, strict) is unreachable through plan oscillation BY
                    # CONSTRUCTION, not by another rail's grace.
                    # SAFETY PATHS ARE EXPLICITLY UNAFFECTED: halt/shutdown `flatten`, the
                    # day stop, and the retired-venue recall (exit_cancel lanes) never pass
                    # through this branch — a cancel that REDUCES RISK must never wait on
                    # an anti-churn timer.  This gate delays only the plan's own
                    # change-of-mind about an order it placed seconds ago.
                    age_s = now - float(cur.get("placed_ts", now))
                    if age_s < C.MIN_RESTING_LIFE_S:
                        if not cur.get("plan_exit_deferred_logged"):
                            cur["plan_exit_deferred_logged"] = True    # once per order
                            R.log("plan_exit_deferred", ticker=s.ticker, side=s.side,
                                  age_s=round(age_s, 3),
                                  remaining=cur["remaining"], why="plan_zero_under_min_life")
                        stats["skipped"] += 1
                        self.slot_examined[key] = now
                        continue
                    ok, _ = self.cancel(cur["order_id"], now, lane="requote_cancel")
                    if ok:
                        stats["cancelled"] += 1
                self.slot_examined[key] = now
                continue

            # Price, on the YES axis (order bodies speak YES; `s.p` is the SAME-SIDE best in
            # its own collateral currency, so the ask converts).
            # A SELF-QUALIFYING slot prices at its scoring-legal band-floor price (law §7a:
            # the qualifying depth IS the order, and the allocator priced its whole cost at
            # this price).  The law may size it PAST the bare walk gap (the earning contract,
            # or the oversize) — the price is a property of the slot, not of the size.
            if s.is_land_grab and q_alloc > 0:
                price = s.land_grab_price_c / 100.0
                if key not in self.land_grabbed:
                    self.land_grabbed.add(key)
                    R.log("land_grab", ticker=s.ticker, side=s.side, size=q,
                          price_c=s.land_grab_price_c, target_size=s.target_size,
                          cum_size=s.cum_size)
            else:
                # THE ONLY REMAINING PRICE IS THE SAME-SIDE BEST.  The branch removed here
                # priced from the OPPOSING best, which is what a seller does; there is no
                # seller in this program any more.
                price = s.p if s.side == "bid" else (1.0 - s.p)
            price = round(price, 4)
            # A MAKER NEVER TAKES (quote.would_cross).  Checked against the OPPOSING slot's
            # best on the YES axis, for every remaining path — entry, replenish and land grab.
            # (Historically the EXIT path was the only one that carried this guard, via
            # `shed_price`'s crossed-book refusal, and the entry path is what paid the spread;
            # the exit is gone and the guard stayed where it was needed.)  Skipping costs one cycle of presence;
            # crossing costs the spread AND the presence, since a fully-taken order leaves
            # nothing resting and the next cycle re-posts into the same trap.
            _bid_s = slot_by_key.get((s.ticker, "bid"))
            _ask_s = slot_by_key.get((s.ticker, "ask"))
            _yes_bid = _bid_s.p if _bid_s else None
            _yes_ask = (1.0 - _ask_s.p) if _ask_s else None
            if Q.would_cross(s.side, price, _yes_bid, _yes_ask):
                R.log("would_cross_skipped", ticker=s.ticker, side=s.side, price=price,
                      yes_bid=_yes_bid, yes_ask=_yes_ask)
                stats["skipped"] += 1
                self.slot_examined[key] = now
                continue
            if not (C.MIN_LEGAL_PRICE_C / 100.0 <= price <= C.MAX_LEGAL_PRICE_C / 100.0):
                stats["skipped"] += 1
                continue

            # POST-FILL COOLDOWN (config derivation): an entry re-post inside the window
            # re-feeds the flow that just ate the lot.  EVERY order is an entry now, so the
            # cooldown has no exemption left to grant — the `fully_closing` escape that used
            # to sit here was for exits, and there are none.
            if cur is None:
                _cd = self.fill_cooldown.get(key)
                if _cd is not None and (now - _cd) < C.POST_FILL_COOLDOWN_S:
                    stats["skipped"] += 1
                    self.slot_examined[key] = now
                    continue

            our_c = int(round(cur["price"] * 100)) if cur else None
            best_c = int(round(price * 100))
            qualifies_now = float(s.cum_size) >= float(s.target_size)
            # §4.3(e), PER SLOT: how long since THIS slot was last examined with fresh data.
            # First sight counts as examined now (a fresh slot needs no resync); the trigger
            # fires only when a slot's examination genuinely lapsed — a classify gap, a
            # stalled loop — and forces the quote to be re-proven against the book.
            since_resync = now - self.slot_examined.get(key, now)
            trig = Q.requote_triggers(
                our_c, best_c, cur["remaining"] if cur else 0.0, q,
                s.S, self.S_ref.get(key, s.S), qualifies_now,
                self.qual_ref.get(key, qualifies_now),
                (now - cur["placed_ts"]) if cur else 0.0, since_resync)
            self.S_ref[key] = s.S
            self.qual_ref[key] = qualifies_now
            self.slot_examined[key] = now
            if cur is None or trig:
                placed = self._requote_slot(key, s.ticker, s.side, price, q, exp, now, cur)
                if placed:
                    stats["placed"] += 1
                else:
                    stats["skipped"] += 1
        slot_keys = {s.key for s in slots}
        # BLOCKER-3's other half: an ORDER whose slot vanished from the table is examined
        # by nobody above.  Surface every such order past the resync deadline — the runner's
        # poll set always contains ordered tickers, so the fix is a fresh classify, and this
        # log is the tripwire if that contract ever breaks.
        for key, orders_ in sorted(live_by_slot.items()):
            if key in slot_keys:
                continue
            last = self.slot_examined.get(key, min(o.get("placed_ts", now)
                                                   for o in orders_))
            # AN ORDER WHOSE VENUE IS NO LONGER ELIGIBLE MUST COME HOME.  A slot vanishes from
            # the table when its program is denied, its window is refused as too long, or it
            # closed — and until now the order simply rested on, unmanaged, holding capital in
            # a venue the strategy has decided against.  Cancelling releases it to a venue that
            # is still eligible; the cancel lane is never refused, and a cancel cannot increase
            # exposure.  (Held INVENTORY is untouched by this and by everything else: it
            # rides to settlement.  Recalling an ORDER releases capital; selling a POSITION
            # pays the spread, and that is the act this program no longer performs.)
            if self.venue_retired(key[0]):
                ok, _ = self.cancel(orders_[0]["order_id"], now, lane="exit_cancel")
                if ok:
                    stats["cancelled"] += 1
                    R.log("retired_venue_recalled", ticker=key[0], side=key[1])
                    continue
            if now - last >= C.SAFETY_RESYNC_S:
                self.slot_examined[key] = now                 # log once per lapse, not 1 Hz
                R.log("resync_overdue", ticker=key[0], side=key[1],
                      since_s=round(now - last, 1))
        return stats

    def _requote_slot(self, key, ticker, side, price, q, exp, now, cur):
        """MAKE-BEFORE-BREAK (v1 §4.1) with the §4.2 automatic cancel-first degrade.
        Returns True iff a new order is resting.

        v4's C4 SHED RETRY IS GONE (owner decision, 2026-07-30).  It existed because a
        COMBINED order (earning tail + shed) could fail a cap as one order and take the shed
        down with it, locking the inventory; the retry re-sent the shed alone as
        `fully_closing=True`, which passed BECAUSE closing orders were cap-exempt.  With no
        closing orders there is no combined order, no locked inventory to rescue, and — this
        is the part that mattered on 2026-07-30 — no order that a cap cannot refuse.

        MIRROR (make-before-break ↔ break-before-make): an insufficient-balance reject on
        the make leg latches THIS SLOT to cancel-first for one SAFETY_RESYNC_S period (v4
        retried MBB at its window checkpoints; v5 carries no checkpoint machinery, and one
        resync period bounds the retry cost at one reject per minute per slot) — recorded as
        an `mbb_degraded` money row.  Under cancel-first, voluntary requotes are paced at
        T* = 46 s (v1 §4.2's optimum) and never inside the placement's own second (§4.2
        whole-second policy).

        NEW-1: the make leg names the order it REPLACES, so the caps measure the book as it
        will be one call later instead of double-counting one slot (see `place_context`).
        Under cancel-first there is nothing to exempt — the old order is already gone.
        """
        replacing = cur.get("order_id") if cur is not None else None
        degraded_ts = self.mbb_degraded.get(key)
        if degraded_ts is not None and now - degraded_ts >= C.SAFETY_RESYNC_S:
            self.mbb_degraded.pop(key, None)              # retry MBB after one resync period
            degraded_ts = None

        if C.MAKE_BEFORE_BREAK and degraded_ts is None:
            ok, reason, resp = self.place(ticker, side, price, q, exp, now,
                                          available_cash_usd=self._available_cash(),
                                          replacing_order_id=replacing)
            if ok:
                if cur is not None:
                    self.cancel(cur["order_id"], now, lane="requote_cancel")
                return True
            if reason == "reject" and "insufficient" in str(resp).lower():
                self.mbb_degraded[key] = now
                self.ledger.write("mbb_degraded", ticker=ticker, side=side,
                                  cancel_first_period_s=C.CANCEL_FIRST_PERIOD_S,
                                  why="make_leg_insufficient_balance")
                # fall through to cancel-first NOW (v4's shape): the quote must not freeze
            else:
                return False

        # --- cancel-first path (§4.2) ---
        if cur is not None:
            if Q.same_second(now, cur.get("placed_ts")):
                return False                  # whole-second policy (§4.2)
            if now - cur.get("placed_ts", 0.0) < C.CANCEL_FIRST_PERIOD_S:
                return False                  # T* pacing: requotes cost g seconds each here
            ok, _ = self.cancel(cur["order_id"], now, lane="requote_cancel")
            if not ok:
                return False                  # stale quote stands; a rate loss, not a risk
        ok, reason, _resp = self.place(ticker, side, price, q, exp, now,
                                       available_cash_usd=self._available_cash())
        return ok

    def _available_cash(self):
        """B11's input: the last observed shared-account balance.  None (never read) skips
        the floor rather than fabricating a number — the reconcile pass reads it within its
        first cadence."""
        return self.cash.last_balance

    def held_by_slot(self, slots):
        """Net inventory attributed to each slot's leg, for v1 §8.1's NET cap (second
        amendment (a)): a bid slot carries a net-YES position, an ask slot a net-NO one."""
        held = {}
        for s in slots:
            net = self.net_position(s.ticker)
            if s.side == "bid" and net > 0:
                held[s.key] = net
            elif s.side == "ask" and net < 0:
                held[s.key] = -net
        return held

    def resting_by_slot(self):
        """Confirmed presence ON THE WIRE per (ticker, side): live orders only — never
        gone_404 ghosts, never allocation intent (BLOCKER-4)."""
        resting = {}
        for o in self.orders.values():
            if o.get("remaining", 0) > 0 and not o.get("gone_404"):
                k = (o["ticker"], o["side"])
                resting[k] = resting.get(k, 0.0) + float(o["remaining"])
        return resting

    def fill_selection_tripwire(self, now):
        """One log line against adverse selection in FILLS (owner, 2026-07-30).

        Fills are adverse-selected samples of our orders — cheap orders fill more — so the
        capital-weighted average PRICE of positions runs below that of resting orders.  The
        skew is already PRICED when every rung carries its own price-bucket phi — since
        2026-07-30 night the SHRUNK one, `scan.phi_posterior`, read straight off the slot
        table (`phi_by_key`), so the tripwire's prediction and the allocator's sizing are one
        number built from one prior; a tripwire fed a different phi than the sizer would be
        measuring its own disagreement instead of the book's (law §6):
        orders fill at rate phi_i x size_i, so the model's own predicted average position
        price is Σ(w_i·phi_i·p_i) / Σ(w_i·phi_i) over the resting book (w_i = collateral,
        p_i = per-contract basis), and the predicted gap is avg_order_price minus that.
        THE TRIPWIRE: a realized gap (avg_order − avg_position) persistently WIDER than the
        predicted one means the phi buckets are too coarse and the position book is rotting
        faster than modeled — refine `scan.phi_bucket`.  Three numbers, no refusal, at the
        recon cadence."""
        ow = ov = 0.0                 # resting orders: Σw, Σw·p
        fw = fv = 0.0                 # fill-rate-weighted: Σw·phi, Σw·phi·p
        for o in self.orders.values():
            if o.get("remaining", 0) <= 0 or o.get("gone_404"):
                continue
            basis = R.unit_collateral(o["side"], o["price"])
            w = float(o["remaining"]) * basis
            ow += w
            ov += w * basis
            phi = float(self.phi_by_key.get((o["ticker"], o["side"]), 0.0) or 0.0)
            fw += w * phi
            fv += w * phi * basis
        pw = pv = 0.0                 # positions: Σw, Σw·p at ENTRY basis
        for t, p in self.positions.items():
            for leg in ("yes", "no"):
                n = abs(float(p.get(leg, 0.0)))
                if n <= 0:
                    continue
                basis = float(self.entry_basis.get((t, leg), 0.0))
                w = n * basis
                pw += w
                pv += w * basis
        if ow <= 0 and pw <= 0:
            return None               # nothing resting, nothing held: nothing to compare
        avg_order = (ov / ow) if ow > 0 else None
        avg_position = (pv / pw) if pw > 0 else None
        predicted = (fv / fw) if fw > 0 else avg_order
        gap = (avg_order - predicted) if (avg_order is not None
                                          and predicted is not None) else None
        out = {"avg_order_price_usd": None if avg_order is None else round(avg_order, 6),
               "avg_position_price_usd": None if avg_position is None
               else round(avg_position, 6),
               "predicted_gap_usd": None if gap is None else round(gap, 6)}
        R.log("fill_selection_tripwire", **out)
        return out

    def project_day_reward(self, slots, alloc_map):
        """SF-1 — OUR projected day accrual: `share(q,S) × ρ/2 × min(24h, hours_left)` over
        slots we fund (q = the larger of resting and allocated — both are commitments the
        day's P&L rides on).  S is already the RIVAL score (SF-5), so `share` is the
        filing's own share."""
        resting = self.resting_by_slot()
        total = 0.0
        for s in slots:
            q = max(float(alloc_map.get(s.key, 0)), resting.get(s.key, 0.0))
            if q <= 0:
                continue
            total += alloc.our_share(q, s.S) * (s.rho / 2.0) * \
                min(24.0, max(0.0, s.hours_left))
        return total

    def integrate_accrual(self, now, slots, alloc_map=None,
                          write_s=C.ACCRUAL_WRITE_S):
        """SECOND AMENDMENT (b), corrected by BLOCKER-4: integrate the MODEL accrual ONLY
        over presence actually RESTING ON THE WIRE (confirmed orders) — `share(q,S) × ρ/2 ×
        dt` per program.  Never over allocation: the scorer samples the BOOK, and an
        allocation the requoter could not land (rate-refused, capital-refused, shadow) is
        presence nobody held — accruing it inflates the cliff's A with money the credits
        ritual will DISAGREE, walking the ratchet down on our own bookkeeping.  Feeds the
        cliff decision (`Slot.accrued` next cycle), widens the cash feed's positive side
        (over-stating is the safe direction ONLY about timing, §5.2 — not about phantom
        presence), and persists as `accrual` money rows (≤ ACCRUAL_WRITE_S crash loss).

        SHADOW writes ZERO accrual money rows (BLOCKER-4): shadow's place() refuses before
        the wire, so nothing rests and the integral is zero by construction — and the guard
        below makes it structural, so a later shadow-mode change cannot contaminate the
        live replay at G3.

        dt is capped at 5 s: a stalled loop must not mint accrual for presence nobody held.
        MIRROR (accrual over-counted ↔ under-counted): over-counting inflates only the
        pending band and the KEEP bias — bounded by the [0.5,2.0] verification band, which
        DISAGREES a venue whose credits undershoot the model; under-counting forfeits
        rescues, today's tape.  The ratchet is the referee either way.
        """
        if self.shadow:
            return 0.0
        dt_h = 0.0
        if self.last_accrual_ts is not None:
            dt_h = min(max(0.0, float(now) - self.last_accrual_ts), 5.0) / 3600.0
        self.last_accrual_ts = float(now)
        resting = self.resting_by_slot()
        delta_total = 0.0
        for s in slots:
            q = resting.get(s.key, 0.0)
            if q <= 0 or dt_h <= 0:
                continue
            d_acc = alloc.reward_rate(s.rho, q, s.S) * dt_h
            if d_acc > 0:
                self.accrued[s.program_id] = self.accrued.get(s.program_id, 0.0) + d_acc
                delta_total += d_acc
        if delta_total > 0:
            self.cash.accrue_reward(delta_total)
        if float(now) - self.last_accrual_write >= float(write_s):
            self.last_accrual_write = float(now)
            for pid in sorted(self.accrued, key=str):
                val = round(self.accrued[pid], 6)
                if self._accrual_written.get(pid) != val:
                    self.ledger.write("accrual", program_id=str(pid), accrued=val)
                    self._accrual_written[pid] = val
        return round(sum(self.accrued.values()), 6)

    def flatten(self, now):
        """Cancel-all on the EXIT lane — never refused, never counted against the cancel share.

        "ALL" MEANS ALL OF OURS, AND IT IS SCOPED BY CONSTRUCTION: the loop walks
        `self.orders`, which — since the startup order-adoption sweep was deleted on
        2026-07-30 — contains exactly the orders THIS PROCESS placed.  There is no
        account-wide cancel endpoint call anywhere in this program, and there must not be:
        nestor and other systems trade this same account and their orders are untouchable.
        This is the ONE action a halt performs, and the one shutdown performs first."""
        for oid in list(self.orders):
            self.cancel(oid, now, lane="exit_cancel")

    # ── THE HALTED CLOSING PASS IS GONE (owner decision, 2026-07-30). ────────────────────
    # It was `halted_closing_pass`: while halted, one maker shed per held market at the
    # OPPOSING best, re-posted every halted-idle pass, `fully_closing=True` and therefore
    # exempt from every entry rail — no ceiling, no cluster cap, no market cap, no variance
    # rail, not even the halt itself.
    #
    # WHAT THAT COST, measured 2026-07-30: a books-integrity bug halted the bot, and the halt
    # armed this pass.  It sized its "sheds" from the books the halt had just declared WRONG —
    # a 98-contract buy at 95c ($93) against a phantom short — and because the order was
    # cap-EXEMPT nothing downstream could refuse it.  The GTC orders then outlived every
    # restart, because the recovery sweep re-adopted whatever rested on the wire.
    #
    # THE DEFECT IS STRUCTURAL, NOT A SIZING BUG.  A halt is the statement "our books are not
    # trustworthy".  A pass that reads those books to decide what to sell is asking the
    # broken instrument for the reading, and it is doing it with the rails switched off.  The
    # only coherent halt behaviour is to place NOTHING.
    #
    # WHERE THE POSITIONS GO NOW: to settlement, which is where they were going anyway — the
    # D4 gate bounds every ride at ≤7 days, and the tape prices paying the spread to leave at
    # −$40.30 (+2c leg) / −$123 (instant flatten).  The halt keeps its ONE action, `flatten`,
    # which cancels our own resting ORDERS (exposure stops growing) and touches no position.

    # =========================================================================================
    # SHUTDOWN
    # =========================================================================================
    def install_signal_handlers(self):                        # pragma: no cover - process
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, "stopping", True))

    def shutdown(self, now, reason="sigterm"):
        """cancel-all → handback (ALWAYS) → zeroed cash feed.

        ORDER IS THE GUARANTEE.  §5.4's mirror: an ABSENT feed file reads as (0,0), which is
        correct only if v5 is truly flat — so the zeroed feed is written LAST, after the orders
        are actually gone.  And the handback is written in BOTH regimes (§6.3 SF-2), not only
        the dirty one, because a file that exists only sometimes is a file nobody trusts when
        it matters.

        SF-4 — CRASH-PROOF: each of the three steps survives the others' failure, and the
        HANDBACK survives everything, because it is the one artifact that lets v4 restart
        onto reality.  A cancel-all that raises must not cost the handback; a handback that
        raises must not cost the zeroed feed (the feed then honestly reports whatever state
        the cancel-all reached... except it reports ZERO, which is only correct if the
        cancel-all emptied the book — so the zeroed feed is SKIPPED when orders remain, and
        the last live feed stands as the conservative record, §5.4's own fallback).
        """
        R.log("shutdown", reason=reason, rollback_clean=self.rollback.clean)
        try:
            self.flatten(now)
            # A cancel that 404'd during the flatten cannot wait FILLS_REQUERY_DELAY_S in a
            # dying process.  v1 §9.4a's conservative terminal applies: assume fully
            # filled + freeze, so the handback covers the inventory we MIGHT hold rather
            # than omitting inventory we DO — over-stating costs capacity, under-stating is
            # the naked-short direction.
            for oid in sorted(list(self.pending_404)):
                self.assume_404_filled(oid, now, why="shutdown_unresolved_404")
        except Exception as exc:                              # noqa: BLE001 - SF-4
            R.log("shutdown_flatten_error", err="%s: %s" % (type(exc).__name__, exc))
        held = []
        try:
            held = [{"ticker": t, "side": leg, "net": p[leg],
                     "basis": self.entry_basis.get((t, leg), 0.0)}
                    for t, p in sorted(self.positions.items())
                    for leg in ("yes", "no") if abs(p.get(leg, 0.0)) > 0]
            ok, _ = self.persist.write(R.atomic_write_json, C.HANDBACK_PATH,
                                       cutover.handback(held, now))
        except Exception as exc:                              # noqa: BLE001 - SF-4
            R.log("shutdown_handback_error", err="%s: %s" % (type(exc).__name__, exc))
            ok = False
        try:
            if self.orders:
                # §5.4 mirror: a zeroed feed with orders still resting would publish
                # expected-cash ABOVE the truth — the one forbidden direction.  Leave the
                # last live feed standing (conservative) and page instead.
                R.log("shutdown_feed_not_zeroed", orders_remaining=len(self.orders))
                R.ntfy("halt", "lip_v5 shutdown left %d orders resting" % len(self.orders))
            else:
                self.publisher.publish_zeroed(now)
        except Exception as exc:                              # noqa: BLE001 - SF-4
            R.log("shutdown_feed_error", err="%s: %s" % (type(exc).__name__, exc))
        R.log("shutdown_complete", handback_written=ok, positions=len(held),
              rollback_clean=self.rollback.clean,
              procedure=self.rollback.procedure())
        return {"handback": ok, "positions": held, "rollback_clean": self.rollback.clean}

    # =========================================================================================
    # SHADOW READ-OUT (gate G2)
    # =========================================================================================
    def shadow_readout(self, now, slots=None):
        """G2's real read-out: `venue_rank` lines, PSDH coverage, and a ZEROED cash feed.
        Quotes NOTHING — `self.shadow` makes `place()` refuse before the rate lane."""
        rows = []
        for s in (slots or []):
            n = alloc.law_need(s)
            row = n.numbers()
            row["venue"] = s.venue
            row["reason"] = n.reason
            # `affordable` stays on total_usd: it answers the $10 RAIL's question (capital
            # committed), which is a different question from the ranking's.  A rung refused
            # for bleed carries reason == "bleed_exceeds_credit" and is excluded here by the
            # `reason == ""` clause, so the read-out never calls a bleeding rung affordable.
            row["affordable"] = (n.reason == "" and
                                 n.total_usd <= C.ALLOC_PER_MARKET_USD + 1e-9)
            rows.append((alloc.law_sort_key(n), row))
        # Cheapest EFFECTIVE need first — `alloc.law_sort_key`, the LAW's one ordering (skips
        # sort last, by name).  Sorted on the key OBJECT, not on a re-spelling of it over the
        # row dict: the re-spelling is what went stale on `total_usd` when the fill-bleed term
        # landed (reviewer send-back, 2026-07-30 night), so venue_rank showed the operator a
        # cheap-first board while the allocator funded expensive-first.
        rows.sort(key=lambda kr: kr[0])
        rows = [r for _k, r in rows]
        for r in rows:
            R.log("venue_rank", **r)
        seg = self.presence_log.read_segment(now)
        covered = sorted({(r["ticker"], r["side"]) for r in seg})
        self.publisher.publish_zeroed(now)
        return {"venue_rank": rows, "psdh_covered": len(covered),
                "cash_feed": "zeroed", "quoted": 0}
