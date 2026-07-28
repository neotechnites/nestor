"""
lip_v5.engine — THE RUN CYCLE.

    startup  → refusals → ledger replay → adopt → triage → arm
    cycle    → clock/rate → classify → slots → r*+ALLOCATE → quote(MBB) → fills
             → meter → recycler → cash feed → recon → checkpoints → health
    shutdown → cancel-all → shed → handback (ALWAYS) → zeroed cash feed

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
from . import guards as G, ledger as LG, money as M, presence as P
from . import quote as Q
from . import ratchet as RT, ratelimit as RL, runtime as R, scan, wsgate


class Maker(object):
    def __init__(self, ex, now, mode=C.CASH_MODE_SHARED, data_dir=None, live=False,
                 shadow=False, ceiling_usd=C.MAX_TOTAL_COLLATERAL_USD):
        self.ex = ex
        self.mode = mode
        self.live = bool(live)
        self.shadow = bool(shadow)
        self.ceiling_usd = float(ceiling_usd)
        self.data_dir = data_dir or C.DATA_DIR

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
        self.venues = {}                     # venue -> RT.VenueState (ADMITTED only; a venue
                                             # absent here allocates ZERO — see admit_venues)
        self.venue_status = {}               # venue -> last admission status (telemetry)
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
        self.cycles = 0

        # --- requoter state (charter A) ---
        self.S_ref = {}                      # (ticker, side) -> S at last requote (§4.3(c))
        self.qual_ref = {}                   # (ticker, side) -> qualifies at last look (§4.3(d))
        self.mbb_degraded = {}               # (ticker, side) -> ts of the balance reject
        # §4.3(e) PER SLOT (final fix round, BLOCKER-3): the earlier scalar was set to `now`
        # at the end of EVERY pass, so trigger (e) could never fire — a dead guard wearing a
        # live constant.  Resync tracks when each slot was last EXAMINED with fresh data.
        self.slot_examined = {}              # (ticker, side) -> ts last examined
        self.land_grabbed = set()            # log land_grab once per (ticker, side)
        # --- the shed path (charter A) ---
        self.triage_shed = set()             # tickers the cutover triage sentenced to shed
        self.pending_triage = []             # adopted positions awaiting a venue reading
        self.shed_target = {}                # (ticker, shed_side) -> contracts to unwind
        self.shed_since = {}                 # ticker -> ts the shed began (l_shed's clock)
        self.shed_held = {}                  # ticker -> held leg at shed start
        self.shed_completed_h = {}           # (ticker, held_leg) -> [hours open->flat]
        # --- SF-3 ---
        self.halt_flatten_done = False       # every halt path flattens ONCE
        # Charter amendment: per-rung cap, derived per cycle from the day stop; the FLOOR
        # before the first cycle (conservative — no reward projection exists yet).
        self.slot_cap_usd = C.INV_CAP_USD
        # --- SECOND AMENDMENT (b): accrued projected payout, per program.  The cliff
        # decision is only as good as the A it remembers, so this persists via `accrual`
        # money rows (≤60 s crash loss) and recovers with replay.
        self.accrued = {}                    # program_id -> $ accrued (model, conditional)
        self.last_accrual_ts = None
        self.last_accrual_write = 0.0
        self._accrual_written = {}           # program_id -> last persisted value
        # --- FINAL FIX ROUND state ---
        self.last_fills_poll = None          # BLOCKER-1: fills cadence on the verify lane
        self.pending_404 = {}                # oid -> {"requery_at", "second_read"} (§9.4a)
        self.last_alloc = {}                 # key -> qty from the last ALLOCATE (SF-1 input)
        self.readings_line = 0               # SF-4: v5_readings.jsonl lines consumed
        self._readings_stat = None           # (mtime, size) — skip unchanged files
        self.close_cache = {}                # ticker -> close_ts (halted closing pass)
        self.resolved = set()                # tickers whose market is determined/settled:
                                             # outcome fixed, no variance left, so they hold
                                             # no CLUSTER risk — only pending cash

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
        self.pending_triage = list(res["adopted"])     # triaged when a venue reading exists
        R.log("adopt", adopted=len(res["adopted"]), excluded=len(res["excluded"]),
              orphans=len(res["orphans"]))
        return res

    def triage(self, now, venues, r_star=C.FLOOR_RATE_PER_H):
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
            cluster_cap_usd=CL.cluster_cap_usd(G.day_stop_usd(self.projected_day_reward, ceiling_usd=self.ceiling_usd),
                                              ceiling_usd=self.ceiling_usd),
            frozen=self.frozen, refill=self.refill,
            n_cap_fn=lambda p: alloc.n_cap(p, caps),
            day_stopped=self.day_stopped, skew_ok=self.skew_ok)

    def place(self, ticker, side, price, count, expiration_ts, now,
              fully_closing=False, available_cash_usd=None, lane="place",
              replacing_order_id=None):
        """The ONLY way an order reaches the exchange.  Returns (ok, reason, resp).

        Order of operations is load-bearing:
          1. the RAILS (`guards.place_allowed`) — refuse before anything is spent or written
          2. the RATE LANE — refuse before the wire, not at it
          3. **publish the cash feed BEFORE the POST** (§5.3), so published expected-cash is
             never above the truth even if we die between the two
          4. the wire
          5. correct the feed from the response
        """
        order = {"ticker": ticker, "side": "yes" if side == "bid" else "no",
                 "n": float(count), "basis": float(price), "fully_closing": fully_closing}
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
        if blind and len(hist) >= C.PLACE_BURST_MAX and not fully_closing:
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
        self.publisher.publish_before_wire(coid, 0.0 if fully_closing else collateral, now)

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
        self.cash.confirm_order(coid, 0.0 if fully_closing else collateral)
        self.orders[oid] = {"order_id": oid, "coid": coid, "ticker": ticker, "side": side,
                            "price": float(price), "size": float(count),
                            "remaining": float(o.get("remaining_count", count)),
                            "fully_closing": fully_closing,
                            "expiration_ts": int(expiration_ts),
                            "placed_ts": now}
        self.ledger.write("place_resp", ticker=ticker, side=side, coid=coid, order_id=oid,
                          price=price, size=count,
                          remaining_count=o.get("remaining_count"),
                          fill_count=o.get("fill_count", 0), seq=self.coid_seq,
                          expiration_ts=int(expiration_ts),
                          fully_closing=fully_closing)
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
                               closing=bool(o.get("fully_closing")))
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
            self.cash.fill(ticker, coid or "shed", count, value, side_sign=-1.0,
                           proceeds_per_contract=proceeds)
        else:
            prev = pos[leg] * self.entry_basis.get((ticker, leg), 0.0)
            pos[leg] += float(count)
            self.entry_basis[(ticker, leg)] = ((prev + count * unit) / pos[leg]) \
                if pos[leg] > 0 else 0.0
            self.position_cost[ticker] = self.position_cost.get(ticker, 0.0) + count * unit
            self.cash.fill(ticker, coid or "o", count, unit)
        if fee_usd:
            self.pay_fee(fee_usd)
        self.refill.note_fill(ticker, side, count, ts=now)    # SF-6: window-keyed
        self.meter.note_fill((ticker, side), count, count * unit)
        self.rollback.note_fill(ticker, leg, now)
        self.ledger.write("fill_obs", ticker=ticker, side=side, count=count,
                          price_c=int(round(price * 100)), fill_id=fill_id,
                          order_id=order_id)
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
        tell them apart.  Attribution order: the ORDER the row names (its side and
        `fully_closing` are authoritative), else the position (enough held ⇒ closing —
        v4's apply_fill netting, the crash-gap case)."""
        leg, sign = cutover.normalize_fill(row.get("side"), row.get("action", "buy"))
        count = float(row.get("count", 0))
        yp = row.get("yes_price")
        price = (float(yp) / 100.0) if yp is not None else float(row.get("price", 0))
        # One YES-axis fact: was this execution bid-shaped (acquires YES) or ask-shaped
        # (acquires NO / sheds YES)?  (yes, sell) and (no, buy) are the same ask-shaped act.
        ask_like = (leg == "bid" and sign < 0) or (leg == "ask" and sign > 0)
        ticker = row.get("ticker")
        o = self.orders.get(str(row.get("order_id"))) if row.get("order_id") is not None \
            else None
        if o is not None:
            side, closing = o["side"], bool(o.get("fully_closing"))
        else:
            side = "ask" if ask_like else "bid"
            pos = self.positions.get(ticker) or {}
            held = pos.get("yes", 0.0) if ask_like else pos.get("no", 0.0)
            closing = held >= count - 1e-9
        # Fee-bearing event (charter B): a maker fill is free; `is_taker` means we crossed
        # (should be unreachable under STP, but if the exchange says we paid, we book it —
        # a fee we refuse to book is a silent divergence in the cash feed).
        fee = cutover.taker_fee_usd(count, price) if row.get("is_taker") else 0.0
        # v4 NEW-3: the fallback key carries order_id, price, time AND the enumeration
        # index — a keyless 5+5 split at one price must not collide (colliding keys DROP
        # the second fill: the naked-short direction).
        fid = row.get("trade_id") or row.get("fill_id") or row.get("id")
        fallback = "syn-%s|%s|%s|%s|%s|%s|%s" % (
            row.get("order_id"), ticker, row.get("side"), count,
            yp if yp is not None else row.get("price"), row.get("created_time"),
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
                       closing=bool(o.get("fully_closing")))
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

        # --- day stop (B2) ---
        pnl = G.mark_to_market_pnl(self.positions, self.position_cost, yes_mids or {},
                                   self.cash.fees_paid)
        out["pnl"] = pnl
        out["unpriced"] = G.unpriced_positions(self.positions, yes_mids or {})
        if G.day_stop_breached(pnl, self.projected_day_reward):
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
        if breached:
            self.halt.halt("max_drawdown", now, {"drawdown": dd, "peak": self.peak.peak})
            self.flatten(now)
            self.halt_flatten_done = True
            return out

        # --- allocate → REQUOTE (charter A: the stage that was never written) ---
        if slots:
            # Charter amendment: the per-rung cap DERIVES from the day stop each cycle
            # (0.5×, floored at $10); the reward side of sizing is (★)'s own saturation.
            self.slot_cap_usd = C.slot_cap_usd(
                G.day_stop_usd(self.projected_day_reward, ceiling_usd=self.ceiling_usd))
            caps = alloc.Caps(inv_cap_usd=self.slot_cap_usd)
            venue_caps = self.admit_venues(now, slots)
            # SECOND AMENDMENT (a): held inventory attributed per slot leg, so the cap binds
            # NET exposure and the replenish target after a fill is n_cap − held — presence
            # continues, correctly sized, instead of the v4 tape's silence-to-settlement.
            held = self.held_by_slot(slots)
            # MBB's reserve is one copy of the LARGEST slot (the derived cap), and the
            # budget plans against what is GENUINELY available: held positions already
            # consume the ceiling (v4 D5 — planning against the raw ceiling makes place()
            # ration an infeasible plan first-come).
            budget = alloc.reserve_budget(
                self.ceiling_usd - self.cash.inventory_basis, self.slot_cap_usd)
            # SF-6: the turnover window is the slot's own PROGRAM PERIOD, set before the
            # requoter consults the B9 guard.
            for s in slots:
                if s.program_end_ts is not None and s.window_h:
                    self.refill.set_window(s.ticker, s.side,
                                           s.program_end_ts - s.window_h * 3600.0)
                if s.close_ts is not None:
                    self.close_cache[s.ticker] = s.close_ts   # halted closing pass (SF-3)
            # NEW-1b: the SAME cluster cap the rails read, brought inside the plan — an
            # allocator that plans what `place()` must refuse is not a plan (264 refusals in
            # 90 cycles on a 4-rung ladder, every cycle, forever).
            cluster_cap = CL.cluster_cap_usd(G.day_stop_usd(self.projected_day_reward, ceiling_usd=self.ceiling_usd),
                                             ceiling_usd=self.ceiling_usd)
            # The plan must measure the same book the rails do: OPEN positions (`held`) PLUS
            # RESTING orders.  Omitting the second made every cycle plan an order `place()`
            # would refuse on a cluster already full of our own quotes.
            resting_by_slot = {}
            for o in self.orders.values():
                if o.get("remaining", 0) > 0 and not o.get("gone_404"):
                    k = (o["ticker"], o["side"])
                    resting_by_slot[k] = resting_by_slot.get(k, 0.0) + float(o["remaining"])
            a, spent, res = alloc.allocate_with_rstar(slots, budget, caps=caps,
                                                      venue_caps=venue_caps, held=held,
                                                      resting=resting_by_slot,
                                                      cluster_cap_usd=cluster_cap)
            self.last_alloc = dict(a)
            out["allocate"] = {"spent": spent, "r_star": res.r_star,
                               "converged": res.converged, "slots": len(slots),
                               "slot_cap_usd": self.slot_cap_usd,
                               "cluster_cap_usd": cluster_cap,
                               "venues_admitted": len(self.venues),
                               "dropped_programs": sorted(str(d) for d in
                                                          (res.dropped or ()))}
            out["alloc"] = a
            out["requote"] = self.requote_pass(now, slots, a, res.r_star)
            out["accrued"] = self.integrate_accrual(now, slots, a)
            # SF-1: the day stop's scale is OUR projected accrual — share × ρ/2 over the
            # slots we actually fund (allocated or resting) — never the board's pools.  A
            # board-pool projection saturated the stop at $150 against a ≤$60 launch
            # deployment: a stop that could never trip.  Computed AFTER allocation, so it
            # scales the NEXT cycle's stop with what is genuinely at risk.
            self.projected_day_reward = self.project_day_reward(slots, a)
            out["projected_day_reward"] = self.projected_day_reward

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
        if now - self.last_recon >= C.RECON_POSITIONS_S:
            self.reconcile(now)
            self.last_recon = now

        # --- health read-out ---
        out["clusters"] = CL.cluster_report(
            [{"ticker": t, "side": leg, "n": abs(p[leg]),
              "basis": self.entry_basis.get((t, leg), 0.0)}
             for t, p in self.positions.items() for leg in ("yes", "no") if abs(p[leg]) > 0],
            CL.cluster_cap_usd(G.day_stop_usd(self.projected_day_reward, ceiling_usd=self.ceiling_usd),
                               ceiling_usd=self.ceiling_usd))
        out["bucket_hz"] = self.bucket.b
        out["halted"] = self.halt.halted
        out["rollback_clean"] = self.rollback.clean
        R.log("cycle", **{k: v for k, v in out.items() if k != "alloc"})
        return out

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
            exch[row.get("ticker")] = float(row.get("position", 0))
        for t, n in exch.items():
            ours = self.positions.get(t, {})
            our_net = ours.get("yes", 0.0) - ours.get("no", 0.0)
            if abs(our_net - n) > 0.5:
                R.log("position_divergence", ticker=t, ours=our_net, exchange=n)
                R.ntfy("assume_filled", "lip_v5 position divergence %s" % t)
                self.frozen.add(t)
        sb, bal = self.ex.balance()
        self.note_http(sb, now)                               # SF-2
        if sb == 200:
            self.cash.observe_balance(float(bal.get("balance", 0)) / 100.0, now)
        ss, srows = self.ex.settlements()
        self.note_http(ss, now)                               # SF-2
        if ss == 200:
            for row in (srows.get("settlements") or []):
                self.cash.settlement_row(row.get("ticker"), float(row.get("revenue", 0)) / 100.0)
                # The exchange has SETTLED it: outcome fixed, cash pending.  It leaves the
                # cluster's risk budget (see place_context) but stays in the cash feed.
                self.resolved.add(row.get("ticker"))
        return exch

    # =========================================================================================
    # VENUE ADMISSION (charter B: populate self.venues — wakes the §1.4 caps in allocation)
    # =========================================================================================
    def admit_venues(self, now, slots):
        """Run §1.4's rung-0 admission over the venues the slot table names, and return the
        `venue_caps` map ALLOCATE binds against.

        THE CONTRACT THAT WAKES THE CAP: every venue in `slots` gets an entry — admitted
        venues get their ratchet cap, everything else (queued, unprobeable, stood-down,
        never-seen) gets 0.0.  A venue absent from the map would be UNCAPPED in ALLOCATE,
        which is the dormant-guard state this round exists to end.

        `floor_q` is computed over the REMAINING window (hours_left), not the full one: the
        probe must clear ENTRY_FLOOR from NOW, or the forfeit gate drops the very program the
        probe was sized for — a probe and a gate that disagree fund nothing forever.  For a
        rung-0 unverified venue the cap TRACKS the floor upward as the window shrinks (spec:
        "never shrink the probe below floor_q"); once the floor no longer fits under the
        per-slot/per-market caps the venue reads UNPROBEABLE and its cap drops to 0 — the
        runway death, arriving through the same door as `runway_ok`.
        """
        by_venue = {}
        for s in slots:
            by_venue.setdefault(s.venue, []).append(s)

        per_market = C.PER_MARKET_BUDGET_FRAC * self.ceiling_usd
        candidates = []
        for venue, ss in sorted(by_venue.items()):
            floor_usd = self.venue_floor_usd(ss)
            st = self.venues.get(venue)
            if st is None:
                net0 = max(s.net_at(0, C.FLOOR_RATE_PER_H) for s in ss)
                candidates.append((venue, floor_usd, net0))
            elif st.rung == 0 and not st.verified and not st.stood_down:
                # RUNG-0 IS SIZED TO MEASURE SHARE, NOT TO ASK WHETHER REWARDS EXIST.  The
                # mechanism is verified by receipt ($7.482 credited, per-rung line items), so
                # the opening size no longer needs to be the bare floor-clearing probe §1.4
                # specifies for an unknown mechanism — a probe that can only just clear the
                # $1 cliff cannot distinguish "this venue pays" from "we barely qualified".
                # Still bounded by the per-rung cap and the per-market cap inside rung0_cap,
                # and by the cluster cap, day stop and drawdown halt outside it.
                cap, status = RT.rung0_cap(floor_usd * C.RUNG0_FLOOR_MULT,
                                           self.slot_cap_usd, per_market)
                st.rung0_cap_usd = cap        # tracks floor_q up; 0.0 when UNPROBEABLE
                if status == RT.UNPROBEABLE:
                    self.venue_status[venue] = RT.UNPROBEABLE

        # Admission, ranked by net(0) (spec §1.4 "QUEUE it (ranked by net(0))").  Trying the
        # queue every cycle IS the exploration-floor mirror: the moment the unverified bounds
        # have room, the best queued venue is admitted.
        unverified = [st for st in self.venues.values()
                      if not st.verified and not st.stood_down]
        expo = sum(st.rung0_cap_usd * (2.0 ** st.rung) for st in unverified)
        n_unver = len(unverified)
        n_oversized = sum(1 for st in unverified if st.oversized)
        for venue, floor_usd, _net0 in sorted(candidates, key=lambda kv: (-kv[2], kv[0])):
            vs = RT.VenueState(venue)
            status, cap, detail = RT.admit(vs, floor_usd, self.slot_cap_usd, per_market,
                                           self.ceiling_usd, expo, n_unver, n_oversized)
            prev = self.venue_status.get(venue)
            if status != prev:
                R.log("venue_admission", venue=venue, status=status, cap_usd=cap, **detail)
                if status == RT.OVERSIZED:
                    self.ledger.write("probe_oversized", venue=venue, cap_usd=cap)
            self.venue_status[venue] = status
            if status in (RT.ADMITTED, RT.OVERSIZED):
                self.venues[venue] = vs
                expo += cap
                n_unver += 1
                n_oversized += 1 if vs.oversized else 0

        caps = {}
        for venue in by_venue:
            st = self.venues.get(venue)
            caps[venue] = st.cap_usd(per_market, self.ceiling_usd) if st else 0.0
        return caps

    def venue_floor_usd(self, venue_slots):
        """The venue's probe floor in dollars — with BLOCKER-2's RESCUE_TARGET exemption.

        `floor_q` normally sizes against ENTRY_FLOOR over the REMAINING window, so late in a
        program the floor stops fitting and the venue reads UNPROBEABLE — the runway death.
        `runway_ok` (the slot-table twin) already carries the exemption: with accrual A > 0
        at stake, the reachability target is the forfeit CLIFF, not the entry floor.  The
        MIRROR was applied to one twin and not the other, so venue admission zeroed the cap
        at exactly the moment the cliff rescue needed room to fire (reviewer's P3).  Here:
        when a slot's program has A > 0 and the cliff is REACHABLE at the ρ/2 ceiling, the
        floor becomes the top-up itself — `RESCUE_TARGET − A` — which is the smallest probe
        that still measures (it can pay: the stranded A makes it pay).  A venue whose cliff
        is UNREACHABLE gets no exemption: dead accrual buys no cap room (the abandon end).

        The exemption is a FALLBACK, never a discount: while the ENTRY floor still fits
        under the caps, it stands — a probe sized only to clear $1.10 projects below
        ENTRY_FLOOR and its reading would be OUT_OF_REACH, i.e. a cheaper probe that
        cannot verify.  Only when the entry floor has died (unreachable pool, or no longer
        fitting under the slot/market caps) does the rescue floor take over the runway.
        """
        per_market = C.PER_MARKET_BUDGET_FRAC * self.ceiling_usd
        fits = min(self.slot_cap_usd, per_market)
        floors = []
        for s in venue_slots:
            if s.S <= 0 or s.p <= 0:
                continue
            f = RT.floor_q_usd(s.rho, s.S, s.p, s.hours_left)
            if f is None or f > fits + 1e-9:
                A = float(s.accrued)
                if A > 0 and A + (s.rho / 2.0) * s.hours_left \
                        >= C.RESCUE_TARGET_USD - 1e-12:
                    f = RT.floor_q_usd(s.rho, s.S, s.p, s.hours_left,
                                       entry_floor=max(0.0, C.RESCUE_TARGET_USD - A))
            if f is not None:
                floors.append(f)
        return min(floors) if floors else None

    def venue_reading(self, venue, reading_usd, projection_usd, now, settlement_day=None,
                      src=None, line_no=None):
        """§1.4's verification input (operator popover_estimate or paid credit).  Moves the
        rung, writes the `ratchet` money row, pages on stand-down.  Fed by the SF-4 watched
        file (`consume_readings`) or called directly.  The row carries `src`/`line_no` (so
        a restart never re-applies a consumed reading) and `rung0_cap` (so replay can
        rebuild a climbed venue's cap, not just its rung)."""
        st = self.venues.get(venue)
        if st is None:
            # A reading about a venue not currently admitted still moves its ladder —
            # verification evidence outlives admission (stand-downs and revives depend on
            # it).  cap_usd stays 0 until admission funds it.
            st = RT.VenueState(venue)
            self.venues[venue] = st
        verdict, ratio, detail = RT.apply_reading(st, reading_usd, projection_usd, ts=now,
                                                  settlement_day=settlement_day)
        fields = dict(detail)
        fields.update(venue=venue, verdict=verdict, ratio=ratio, src=src, line_no=line_no,
                      rung0_cap=st.rung0_cap_usd, stood_down=st.stood_down)
        self.ledger.write("ratchet", **fields)
        if verdict == RT.OUT_OF_REACH:
            self.ledger.write("venue_out_of_reach", venue=venue,
                              projection=projection_usd)
            R.ntfy("venue_out_of_reach", "lip_v5 venue out of reach: %s" % venue)
        if st.stood_down:
            R.ntfy("venue_stand_down", "lip_v5 venue stand-down: %s" % venue)
        return verdict

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
    # THE SHED PATH (charter A): triage verdicts + inventory whose venue fails (★) ongoing
    # =========================================================================================
    def net_position(self, ticker):
        p = self.positions.get(ticker) or {}
        return float(p.get("yes", 0.0)) - float(p.get("no", 0.0))

    def run_pending_triage(self, now, slots, r_star):
        """Adopted positions are triaged as soon as a venue reading exists — the slot table
        IS the reading (rho, S, p, phi, d, close/program end all live there).  Triage at
        adoption time would run with NO readings and sentence the whole book to shed on
        `no_venue_reading`; deferring until the classify sweep has spoken judges each
        position on evidence.  MIRROR (never judged ↔ judged blind): a position whose market
        never classifies again stays pending — reported every cycle in the read-out, held,
        and ridden to settlement rather than shed at a price nobody derived."""
        if not self.pending_triage:
            return
        by_key = {s.key: s for s in slots}
        still = []
        for pos in self.pending_triage:
            leg = pos.get("side")
            s = by_key.get((pos["ticker"], "bid" if leg == "yes" else "ask"))
            if s is None:
                still.append(pos)
                continue
            venue = {"rho": s.rho, "S": s.S, "p": s.p, "phi": s.phi, "d": s.d,
                     "close_ts": s.close_ts, "program_end_ts": s.program_end_ts,
                     "l_shed_h": None, "t_hat": s.t_hat}
            v = cutover.triage_position(pos, venue, now, r_star)
            R.log("cutover_triage", **v)
            if v.get("exit_path") == cutover.MAKER_SHED:
                self.triage_shed.add(pos["ticker"])
        self.pending_triage = still

    def update_shed_targets(self, now, slots, r_star):
        """Recompute the shed set each cycle:
          (1) cutover-triage verdicts (permanent for the position they judged), and
          (2) inventory whose venue fails (★) NOW — the same equation that would refuse the
              entry, applied to capital already inside (the carry term is forward-looking;
              that the dollars are already there changes nothing about where they should be).
        Completed sheds (position reaches flat, or untradeable dust) feed the `l_shed`
        measurement — spec §1.2's liquidity horizon learns from its own exits.
        """
        by_key = {s.key: s for s in slots}
        for ticker in sorted(set(self.positions) | set(self.shed_since)):
            net = self.net_position(ticker)
            if abs(net) < 1.0:
                # Flat (or dust, which cannot trade — v4 T1).  If a shed was running, it
                # COMPLETED: record hours open→flat for L_shed.
                if ticker in self.shed_since:
                    dur_h = (float(now) - self.shed_since.pop(ticker)) / 3600.0
                    held = self.shed_held.pop(ticker, "yes")
                    self.shed_completed_h.setdefault((ticker, held), []).append(dur_h)
                    self.shed_target.pop((ticker, Q.shed_side(held)), None)
                    R.log("shed_complete", ticker=ticker, hours=round(dur_h, 4),
                          dust=abs(net) > 1e-9)
                continue
            if ticker in self.frozen:
                continue                      # assume_filled freeze covers RECYCLING (§9.4b)
            held = Q.held_leg_of(net)
            reason = None
            if ticker in self.triage_shed:
                reason = "cutover_triage"
            else:
                s = by_key.get((ticker, "bid" if held == "yes" else "ask"))
                if s is not None:
                    fails_star = not M.admits(s.net_at(abs(net), r_star))
                    horizon = (s.close_ts is not None and s.program_end_ts is not None and
                               M.horizon_excluded(s.close_ts, now, s.program_end_ts, s.rung))
                    if fails_star or horizon:
                        reason = "horizon_excluded" if horizon else "venue_fails_star"
                # No slot this cycle: KEEP the previous verdict rather than judging blind —
                # a transient classify gap must not start (or stop) a shed.
                elif ticker in self.shed_since:
                    reason = "held_from_last_reading"
            key = (ticker, Q.shed_side(held))
            if reason:
                if ticker not in self.shed_since:
                    self.shed_since[ticker] = float(now)
                    self.shed_held[ticker] = held
                    R.log("shed_started", ticker=ticker, held=held, net=net, reason=reason)
                self.shed_target[key] = Q.shed_qty(net)
            else:
                if self.shed_target.pop(key, None) is not None:
                    R.log("shed_stopped", ticker=ticker, reason="venue_passes_star_again")
                self.shed_since.pop(ticker, None)
                self.shed_held.pop(ticker, None)

    # =========================================================================================
    # THE REQUOTING STAGE (charter A) — diff the post-forfeit-gate allocation against the
    # resting book; every emission goes through place()/cancel(), the rails stay the one path.
    # =========================================================================================
    def requote_pass(self, now, slots, alloc_map, r_star):
        """One requote pass.  Returns the read-out {placed, cancelled, sheds, skipped}."""
        self.run_pending_triage(now, slots, r_star)
        self.update_shed_targets(now, slots, r_star)

        stats = {"placed": 0, "cancelled": 0, "sheds": 0, "skipped": 0,
                 "pending_triage": len(self.pending_triage)}
        slot_by_key = {s.key: s for s in slots}
        live_by_slot = {}
        for oid, o in sorted(self.orders.items()):
            # gone_404 orders are NOT on the wire (the exchange said so) — counting them as
            # presence would suppress the very replenish their fill should trigger.
            if o.get("remaining", 0) > 0 and not o.get("gone_404"):
                live_by_slot.setdefault((o["ticker"], o["side"]), []).append(o)

        def target_q(s):
            """The size this pass intends for `s` — the same `max(alloc, shed)` the loop
            computes, hoisted so the ORDER of application can be derived from it."""
            shed = Q.shed_qty(self.net_position(s.ticker), self.shed_target.get(s.key)) \
                if s.key in self.shed_target else 0
            return max(int(alloc_map.get(s.key, 0)), shed)

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
            shed_q = Q.shed_qty(self.net_position(s.ticker),
                                self.shed_target.get(key)) \
                if key in self.shed_target else 0
            q = max(q_alloc, shed_q)
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
                    ok, _ = self.cancel(cur["order_id"], now, lane="requote_cancel")
                    if ok:
                        stats["cancelled"] += 1
                self.slot_examined[key] = now
                continue

            # Price, on the YES axis (order bodies speak YES; `s.p` is the SAME-SIDE best in
            # its own collateral currency, so the ask converts).
            if s.is_land_grab and 0 < q_alloc <= s.land_grab_size and q == q_alloc:
                price = s.land_grab_price_c / 100.0
                if key not in self.land_grabbed:
                    self.land_grabbed.add(key)
                    R.log("land_grab", ticker=s.ticker, side=s.side, size=q,
                          price_c=s.land_grab_price_c, target_size=s.target_size,
                          cum_size=s.cum_size)
            elif shed_q > 0:
                # v1 §5.4: the shed joins the OPPOSING queue and NEVER crosses (G6 stays
                # off).  `shed_price` refuses a crossed/locked book outright — joining a
                # crossed book would in fact take.
                bid_s = slot_by_key.get((s.ticker, "bid"))
                ask_s = slot_by_key.get((s.ticker, "ask"))
                px = Q.shed_price(self.shed_held.get(s.ticker, "yes"),
                                  bid_s.p if bid_s else None,
                                  (1.0 - ask_s.p) if ask_s else None)
                if px is None:
                    R.log("shed_unpriced", ticker=s.ticker, side=s.side,
                          why="crossed_or_one_sided_book")
                    stats["skipped"] += 1
                    continue
                price = px
            else:
                price = s.p if s.side == "bid" else (1.0 - s.p)
            price = round(price, 4)
            # A MAKER NEVER TAKES (quote.would_cross).  Checked against the OPPOSING slot's
            # best on the YES axis, for every path — entry, replenish, land grab and shed
            # alike — because the exit path was the only one that had this guard, and the
            # entry path is what paid the spread.  Skipping costs one cycle of presence;
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

            # Fully-closing iff the WHOLE order reduces inventory (the halt/day-stop/cap
            # exemptions apply to it; a combined earning+shed order is not exempt).
            room = abs(self.net_position(s.ticker))
            fully_closing = shed_q > 0 and q <= room + 1e-9

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
                placed = self._requote_slot(key, s.ticker, s.side, price, q, exp, now,
                                            fully_closing, shed_q, cur)
                if placed:
                    stats["placed"] += 1
                    if shed_q > 0:
                        stats["sheds"] += 1
                else:
                    stats["skipped"] += 1
        # A shed target whose opposing slot vanished from the table cannot be priced this
        # cycle.  Say so — a shed that silently never posts is the locked-inventory state
        # the shed path exists to end.
        slot_keys = {s.key for s in slots}
        for key in sorted(set(self.shed_target) - slot_keys):
            R.log("shed_unpriced", ticker=key[0], side=key[1],
                  target=self.shed_target[key])
        # BLOCKER-3's other half: an ORDER whose slot vanished from the table is examined
        # by nobody above.  Surface every such order past the resync deadline — the runner's
        # poll set always contains ordered tickers, so the fix is a fresh classify, and this
        # log is the tripwire if that contract ever breaks.
        for key, orders_ in sorted(live_by_slot.items()):
            if key in slot_keys:
                continue
            last = self.slot_examined.get(key, min(o.get("placed_ts", now)
                                                   for o in orders_))
            if now - last >= C.SAFETY_RESYNC_S:
                self.slot_examined[key] = now                 # log once per lapse, not 1 Hz
                R.log("resync_overdue", ticker=key[0], side=key[1],
                      since_s=round(now - last, 1))
        return stats

    def _requote_slot(self, key, ticker, side, price, q, exp, now, fully_closing, shed_q,
                      cur):
        """MAKE-BEFORE-BREAK (v1 §4.1) with the §4.2 automatic cancel-first degrade, plus
        v4's C4 shed retry.  Returns True iff a new order is resting.

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
                                          fully_closing=fully_closing,
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
            elif shed_q > 0 and q > shed_q and reason not in ("shadow",):
                # C4 (v4): the COMBINED order (earning tail + shed) can fail a cap as one
                # order and the shed inside it then vanishes — the inventory locks.  A
                # closing-only order passes by netting; the earning tail is forgone this
                # cycle, unwinding the inventory is not.
                R.log("shed_retry_after_combined_reject", ticker=ticker, side=side,
                      combined=q, shed_only=shed_q, refused_by=reason)
                ok2, _, _ = self.place(ticker, side, price, shed_q, exp, now,
                                       fully_closing=True,
                                       available_cash_usd=self._available_cash(),
                                       replacing_order_id=replacing)
                if ok2 and cur is not None:
                    self.cancel(cur["order_id"], now, lane="requote_cancel")
                return ok2
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
                                       fully_closing=fully_closing,
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
        """Cancel-all on the EXIT lane — never refused, never counted against the cancel share."""
        for oid in list(self.orders):
            self.cancel(oid, now, lane="exit_cancel")

    def halted_closing_pass(self, now):
        """SF-3 — while HALTED, a closing-only requote pass so the book can LEAVE.

        The halt/day-stop exemption in `place_allowed` admits only `fully_closing` orders;
        this pass posts exactly those: one maker shed per held market, priced at the
        opposing best (never crossing — G6 stays off), re-posted each halted-idle pass while
        the position remains.  Without it a halted book holds its inventory to settlement —
        the day stop's flatten cancels ORDERS but cannot exit POSITIONS, and the normal shed
        path is dead because a halted iteration never reaches the requoter.

        MIRROR (a halted book that cannot leave ↔ a halted book that keeps trading): the
        fully_closing flag is the second end's guard — this pass can only REDUCE exposure,
        and its book reads run on the book_poll lane at the halted-idle cadence (≤ one read
        per held market per 30 s).
        """
        placed = 0
        for ticker in sorted(self.positions):
            net = self.net_position(ticker)
            if abs(net) < 1.0 or ticker in self.frozen:
                continue                      # dust cannot trade; frozen covers recycling
            held = Q.held_leg_of(net)
            side = Q.shed_side(held)
            live = [o for o in self.orders.values()
                    if o["ticker"] == ticker and o["side"] == side
                    and o.get("remaining", 0) > 0 and not o.get("gone_404")]
            if live:
                continue                      # a shed already rests; let it work
            admitted, _ = self.bucket.admit("book_poll", now)
            if not admitted:
                break
            status, body = self.ex.book(ticker)
            self.note_http(status, now)
            if status != 200:
                continue
            yes_lv, no_lv = scan._book_levels(body)
            yes_bid = max(p for p, _ in yes_lv) / 100.0 if yes_lv else None
            no_bid = max(p for p, _ in no_lv) / 100.0 if no_lv else None
            yes_ask = (1.0 - no_bid) if no_bid is not None else None
            px = Q.shed_price(held, yes_bid, yes_ask)
            if px is None:
                R.log("shed_unpriced", ticker=ticker, side=side,
                      why="halted_pass_crossed_or_one_sided")
                continue
            close = self.close_cache.get(ticker)
            exp = int(close - C.CLOSE_MARGIN_S) if close \
                else int(now + C.HALTED_SHED_TTL_S)
            if exp <= now:
                continue
            ok, _reason, _ = self.place(ticker, side, round(px, 4), Q.shed_qty(net), exp,
                                        now, fully_closing=True,
                                        available_cash_usd=self._available_cash())
            if ok:
                placed += 1
        if placed:
            R.log("halted_closing_pass", placed=placed)
        return placed

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
            terms = M.net_terms(s.rho, s.S, s.p, 0, s.phi, s.d, s.l_eff,
                                C.FLOOR_RATE_PER_H, s.t_hat)
            rows.append({"ticker": s.ticker, "side": s.side, "venue": s.venue,
                         "net": round(terms["net"], 6), "gross": round(terms["gross"], 6),
                         "carry": round(terms["carry"], 6), "drift": round(terms["drift"], 6),
                         "t_hat": round(terms["t_hat"], 4),
                         "admits": M.admits(terms["net"])})
        rows.sort(key=lambda r: -r["net"])
        for r in rows:
            R.log("venue_rank", **r)
        seg = self.presence_log.read_segment(now)
        covered = sorted({(r["ticker"], r["side"]) for r in seg})
        self.publisher.publish_zeroed(now)
        return {"venue_rank": rows, "psdh_covered": len(covered),
                "cash_feed": "zeroed", "quoted": 0}
