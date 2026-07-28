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

import signal

from . import alloc, cashfeed, clusters as CL, config as C, cutover
from . import guards as G, ledger as LG, money as M, presence as P
from . import ratchet as RT, ratelimit as RL, runtime as R, wsgate


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
        self.frozen = set()
        self.venues = {}                     # venue -> RT.VenueState
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

        # §6.3-C — adoption, then triage.
        if adopt_obj is not None:
            self.adopt(now, adopt_obj, exchange_positions or {}, marks or {})
        return True, []

    def adopt(self, now, adopt_obj, exchange_positions, marks):
        res = cutover.adoption_gate(adopt_obj.get("positions", []), exchange_positions, marks)
        for tk in res["frozen"] + res["refused_for_quoting"]:
            self.frozen.add(tk)
        for a in res["adopted"]:
            leg = a["side"]
            pos = self.positions.setdefault(a["ticker"], {"yes": 0.0, "no": 0.0})
            pos[leg] = float(a["net"])
            self.entry_basis[(a["ticker"], leg)] = float(a["basis"])
            self.position_cost[a["ticker"]] = \
                self.position_cost.get(a["ticker"], 0.0) + a["net"] * a["basis"]
            self.cash.inventory[a["ticker"]] = {"n": float(a["net"]), "basis": float(a["basis"])}
        self.rollback.set_adopted(res["adopted"])
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
    def place_context(self, available_cash_usd=None):
        open_pos, resting = [], []
        for t, p in self.positions.items():
            for leg in ("yes", "no"):
                if abs(p.get(leg, 0.0)) > 0:
                    open_pos.append({"ticker": t, "side": leg, "n": abs(p[leg]),
                                     "basis": self.entry_basis.get((t, leg), 0.0)})
        for o in self.orders.values():
            if o.get("remaining", 0) > 0:
                resting.append({"ticker": o["ticker"],
                                "side": "yes" if o["side"] == "bid" else "no",
                                "n": o["remaining"], "basis": o["price"]})
        return G.PlaceContext(
            halt_state=self.halt, positions=open_pos, resting_basis=resting,
            nestor_orders=self.nestor_orders, nestor_positions=self.nestor_positions,
            available_cash_usd=available_cash_usd,
            cluster_cap_usd=CL.cluster_cap_usd(G.day_stop_usd(self.projected_day_reward)),
            frozen=self.frozen, refill=self.refill, n_cap_fn=alloc.n_cap,
            day_stopped=self.day_stopped, skew_ok=self.skew_ok)

    def place(self, ticker, side, price, count, expiration_ts, now,
              fully_closing=False, available_cash_usd=None, lane="place"):
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
        ctx = self.place_context(available_cash_usd)
        ok, reason, detail = G.place_allowed(ctx, order)
        if not ok:
            R.log("place_refused", ticker=ticker, side=side, refused_by=reason,
                  detail=detail)
            return False, reason, None

        if self.shadow:
            R.log("shadow_place", ticker=ticker, side=side, price=price, count=count)
            return False, "shadow", None

        admitted, why = self.bucket.admit(lane, now, key=(ticker, side))
        if not admitted:
            return False, why, None

        self.coid_seq += 1
        self.persist.write(LG.coid_seq_store, self.coid_seq)
        coid = R.make_coid(ticker, side, self.coid_seq)
        body = R.order_body(ticker, side, price, expiration_ts, coid, count)
        collateral = float(count) * R.unit_collateral(side, price)

        self.ledger.write("place_req", ticker=ticker, side=side, price=price,
                          size=count, coid=coid, seq=self.coid_seq)
        # §5.3 — write (and fsync) the feed with this collateral ALREADY INCLUDED, then POST.
        self.publisher.publish_before_wire(coid, 0.0 if fully_closing else collateral, now)

        status, resp = self.ex.place(body)
        if status not in (200, 201) or not (resp.get("order") or {}).get("order_id"):
            self.cash.reject_order(coid)
            self.publisher.publish(now)
            self.ledger.write("place_resp", ticker=ticker, side=side, coid=coid,
                              err=str(resp)[:200])
            return False, "reject", resp

        o = resp["order"]
        oid = str(o["order_id"])
        self.cash.confirm_order(coid, 0.0 if fully_closing else collateral)
        self.orders[oid] = {"order_id": oid, "coid": coid, "ticker": ticker, "side": side,
                            "price": float(price), "size": float(count),
                            "remaining": float(o.get("remaining_count", count)),
                            "placed_ts": now}
        self.ledger.write("place_resp", ticker=ticker, side=side, coid=coid, order_id=oid,
                          price=price, size=count,
                          remaining_count=o.get("remaining_count"),
                          fill_count=o.get("fill_count", 0), seq=self.coid_seq)
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
        if status == 200 and resp.get("reduced_by") is not None:
            reduced = float(resp["reduced_by"])
            learned = max(0.0, o["remaining"] - reduced)
            if learned:
                self.book_fill(o["ticker"], o["side"], learned, o["price"], now,
                               fill_id="cancel:%s" % oid)
            self.cash.release_order(o["coid"])
            o["remaining"] = 0.0
            self.orders.pop(str(oid), None)
            self.unknown.resolved(oid)
            self.ledger.write("cancel_resp", ticker=o["ticker"], order_id=oid, http=200,
                              reduced_by=reduced)
            self.publisher.publish(now)
            return True, "ok"
        # B10 — anything else leaves the order UNKNOWN: it may be live, and it holds collateral.
        self.unknown.note(oid, o["ticker"], o["side"], o["remaining"], now)
        self.ledger.write("cancel_resp", ticker=o["ticker"], order_id=oid, http=status)
        return False, "unknown"

    # =========================================================================================
    # FILLS
    # =========================================================================================
    def book_fill(self, ticker, side, count, price, now, fill_id=None, closing=False,
                  proceeds=None):
        """The single entry point for a fill into state, so B8's dedupe cannot be bypassed."""
        if not self.dedupe.is_new(fill_id, fallback_key="%s|%s|%s|%s" %
                                  (ticker, side, count, price)):
            return False
        leg = "yes" if side == "bid" else "no"
        pos = self.positions.setdefault(ticker, {"yes": 0.0, "no": 0.0})
        unit = R.unit_collateral(side, price)
        if closing:
            pos[leg] = max(0.0, pos[leg] - float(count))
            self.cash.fill(ticker, "shed", count, unit, side_sign=-1.0,
                           proceeds_per_contract=proceeds)
        else:
            prev = pos[leg] * self.entry_basis.get((ticker, leg), 0.0)
            pos[leg] += float(count)
            self.entry_basis[(ticker, leg)] = ((prev + count * unit) / pos[leg]) \
                if pos[leg] > 0 else 0.0
            self.position_cost[ticker] = self.position_cost.get(ticker, 0.0) + count * unit
            self.cash.fill(ticker, "o", count, unit)
        self.refill.note_fill(ticker, side, count)
        self.meter.note_fill((ticker, side), count, count * unit)
        self.rollback.note_fill(ticker, leg, now)
        self.ledger.write("fill_obs", ticker=ticker, side=side, count=count,
                          price_c=int(round(price * 100)), fill_id=fill_id)
        return True

    def poll_fills(self, now, since=None):
        admitted, _ = self.bucket.admit("verify", now)
        if not admitted:
            return 0
        status, body = self.ex.fills(since)
        if status != 200:
            return 0
        n = 0
        for row in (body.get("fills") or []):
            if not R.owns_coid(row.get("client_order_id", "")):
                continue                     # never trust the index about someone else's order
            leg, sign = cutover.normalize_fill(row.get("side"), row.get("action", "buy"))
            if self.book_fill(row.get("ticker"), leg, float(row.get("count", 0)),
                              float(row.get("price", 0)), now, fill_id=row.get("fill_id"),
                              closing=(sign < 0)):
                n += 1
        return n

    # =========================================================================================
    # THE CYCLE
    # =========================================================================================
    def cycle(self, now, slots=None, books=None, yes_mids=None, server_epoch=None):
        """One pass.  Returns a read-out dict — the same one `--shadow` prints."""
        self.cycles += 1
        out = {"ts": now, "cycle": self.cycles}

        # --- clock / rate ---
        self.bucket.step(now)
        if server_epoch is not None:                                          # B12
            skew = G.clock_skew_s(server_epoch, now)
            self.skew_ok = not G.clock_skew_alarming(skew)
            out["clock_skew_s"] = skew
            if not self.skew_ok:
                R.ntfy("clock_skew", "lip_v5 clock skew %.1fs" % skew)

        # --- metering: FIXED 1 Hz phase, independent of the quoting loop ---
        self.meter_tick(now, books or {})

        # --- day stop (B2) ---
        pnl = G.mark_to_market_pnl(self.positions, self.position_cost, yes_mids or {},
                                   self.fees_paid)
        out["pnl"] = pnl
        out["unpriced"] = G.unpriced_positions(self.positions, yes_mids or {})
        if G.day_stop_breached(pnl, self.projected_day_reward):
            self.day_stopped = True
            self.halt.halt("day_stop", now, {"pnl": pnl})
            self.flatten(now)
            out["day_stop"] = True
            return out

        # --- drawdown (B3) ---
        equity = pnl + self.cash.raw_delta + self.ceiling_usd
        dd, breached = self.peak.observe(equity, now)
        out["drawdown"] = dd
        if breached:
            self.halt.halt("max_drawdown", now, {"drawdown": dd, "peak": self.peak.peak})
            self.flatten(now)
            return out

        # --- allocate ---
        if slots:
            venue_caps = {v: st.cap_usd(self.ceiling_usd * C.PER_MARKET_BUDGET_FRAC,
                                        self.ceiling_usd)
                          for v, st in self.venues.items()}
            budget = alloc.reserve_budget(self.ceiling_usd, C.INV_CAP_USD)
            a, spent, res = alloc.allocate_with_rstar(slots, budget, venue_caps=venue_caps)
            out["allocate"] = {"spent": spent, "r_star": res.r_star,
                               "converged": res.converged, "slots": len(slots)}
            out["alloc"] = a

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
            CL.cluster_cap_usd(G.day_stop_usd(self.projected_day_reward)))
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
            if o.get("remaining", 0) <= 0:
                continue
            key = (o["ticker"], o["side"])
            best = (books.get(o["ticker"]) or {}).get(o["side"])
            ticks_behind = 0 if best is None else max(
                0, int(round((best - o["price"]) * 100)))
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
        if sb == 200:
            self.cash.observe_balance(float(bal.get("balance", 0)) / 100.0, now)
        ss, srows = self.ex.settlements()
        if ss == 200:
            for row in (srows.get("settlements") or []):
                self.cash.settlement_row(row.get("ticker"), float(row.get("revenue", 0)) / 100.0)
        return exch

    def flatten(self, now):
        """Cancel-all on the EXIT lane — never refused, never counted against the cancel share."""
        for oid in list(self.orders):
            self.cancel(oid, now, lane="exit_cancel")

    # =========================================================================================
    # SHUTDOWN
    # =========================================================================================
    def install_signal_handlers(self):                        # pragma: no cover - process
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, "stopping", True))

    def shutdown(self, now, reason="sigterm"):
        """cancel-all → shed → handback (ALWAYS) → zeroed cash feed.

        ORDER IS THE GUARANTEE.  §5.4's mirror: an ABSENT feed file reads as (0,0), which is
        correct only if v5 is truly flat — so the zeroed feed is written LAST, after the orders
        are actually gone.  And the handback is written in BOTH regimes (§6.3 SF-2), not only
        the dirty one, because a file that exists only sometimes is a file nobody trusts when
        it matters.
        """
        R.log("shutdown", reason=reason, rollback_clean=self.rollback.clean)
        self.flatten(now)
        held = [{"ticker": t, "side": leg, "net": p[leg],
                 "basis": self.entry_basis.get((t, leg), 0.0)}
                for t, p in sorted(self.positions.items())
                for leg in ("yes", "no") if abs(p.get(leg, 0.0)) > 0]
        ok, _ = self.persist.write(R.atomic_write_json, C.HANDBACK_PATH,
                                   cutover.handback(held, now))
        self.publisher.publish_zeroed(now)
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
