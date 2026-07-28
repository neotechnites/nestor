"""
lip_v5.cutover — spec §6.3 option C: HOT HANDOFF WITH A W2-GATED ADOPTION BOUNDARY.

Option B (replay v4's ledger into v5's state) is refused: v4's ledger carries known
divergences from the buggy hours, and an import bug manufactures phantom inventory — the class
that produces real naked shorts.  Option A (cold) costs hours of presence and idle dollars on
multi-day inventory.  So:

  1. **v5 GENERATES the adoption file itself** (`--gen-adopt`), reading v4's ledger READ-ONLY.
     An owned, testable, re-runnable step — NOT a hand entry.  The charter's "zero hand
     entries" applies to the cutover too: a hand-typed position table is the highest-stakes
     hand entry in the whole program.
  2. **W2 trust gate at startup** against `GET /portfolio/positions`.  The EXCHANGE is
     authoritative on `net`; v4's LEDGER is authoritative on `basis`.
  3. **Basis sanity**, because a bad basis silently mis-sizes every later cap.
  4. Adopted positions may be SHED but seed no new quote until the first clean recon pass;
     every exchange position NOT adopted is `orphan_position`.

Plus **CUTOVER TRIAGE**: v5 re-judges every adopted position against its OWN net-rate equation
and actively leaves the ones that fail.  Adoption without triage would inherit v4's book
wholesale, including precisely the venues (★) exists to refuse — the position would be carried
by a bot that would never have opened it.
"""

import math

from . import config as C
from . import money as M
from . import runtime as R


# =============================================================================================
# STEP 1 — `--gen-adopt`: reconstruct v4's positions from v4's ledger, READ-ONLY.
# =============================================================================================
def _leg(side):
    """v4's order axis: "bid" = the YES leg, "ask" = the NO leg (v1 B3's normalization)."""
    return "yes" if side == "bid" else "no"


def normalize_fill(side, action="buy"):
    """v4's B3 translation, carried on its merits.  Returns (leg_side, sign).

    The fills payload speaks (side=yes|no, action=buy|sell); the ledger speaks the ORDER axis.
    v4 once mapped raw fills-payload sides with `"yes" if side == "bid" else "no"`, so a row
    carrying side="yes" fell through to the NO leg — a buy of 25 YES at 30c booked as no:25 at
    70c, i.e. net −25 instead of +25.  SIGN-INVERTED, on the exact path that imports fills we
    did not see.  And `action` was dropped, so a SELL booked as an acquisition.  Both are why
    this function exists rather than an inline expression.
    """
    s = str(side or "").strip().lower()
    a = str(action or "buy").strip().lower()
    if s in ("yes", "bid"):
        leg = "bid"
    elif s in ("no", "ask"):
        leg = "ask"
    else:
        leg = "bid"
    return leg, (-1.0 if a == "sell" else 1.0)


class V4Positions(object):
    """The narrowest possible reader of v4's ledger: positions and cost legs, nothing else.

    Deliberately NOT a full v4 replay.  We need exactly {ticker, side, net, basis}; importing
    v4's order state machine, poison set, accrual and 404 disambiguation would import its bugs
    along with its facts, and every one of those fields is re-derived by v5 from its own tape
    anyway.  What we cannot re-derive is the cost BASIS, which is why that is all we take.
    """

    def __init__(self):
        self.positions = {}                  # ticker -> {"yes": n, "no": n}
        self.cost_leg = {}                   # ticker -> {"yes": $, "no": $}
        self.orders = {}                     # oid -> dict
        self.seen_fill_ids = set()

    def _apply(self, ticker, side, n, price_dollars, sign=1.0):
        n = float(n)
        if n <= 0 or not ticker:
            return
        leg = _leg(side)
        pos = self.positions.setdefault(ticker, {"yes": 0.0, "no": 0.0})
        legs = self.cost_leg.setdefault(ticker, {"yes": 0.0, "no": 0.0})
        unit = R.unit_collateral(side, price_dollars)
        if sign < 0:
            n = min(n, max(0.0, pos[leg]))   # a disposal can only release what is held
            if n <= 0:
                return
        pos[leg] += sign * n
        legs[leg] = max(0.0, legs[leg] + sign * n * unit)

    def replay(self, records):
        for rec in records or []:
            kind = rec.get("k") or rec.get("kind") or rec.get("t")
            if kind == "place_resp":
                if rec.get("err"):
                    continue
                oid = rec.get("order_id")
                if not oid or str(oid) in self.orders:
                    continue                 # N2: a duplicated place_resp must not double-book
                o = {"ticker": rec.get("ticker"), "side": rec.get("side"),
                     "price": float(rec.get("price") or 0.0),
                     "size": float(rec.get("size") or 0.0),
                     "remaining": float(rec.get("remaining_count")
                                        if rec.get("remaining_count") is not None
                                        else rec.get("size") or 0.0)}
                self.orders[str(oid)] = o
                fc = float(rec.get("fill_count") or 0.0)
                if fc > 0:
                    self._apply(o["ticker"], o["side"], fc, o["price"])
                    o["remaining"] = max(0.0, o["remaining"] - fc)
            elif kind == "cancel_resp":
                o = self.orders.get(str(rec.get("order_id")))
                if o is None or int(rec.get("http", 0) or 0) != 200:
                    continue
                rb = rec.get("reduced_by")
                if rb is None:
                    continue                 # a 200 we cannot read is not a cancel we trust
                learned = max(0.0, o["remaining"] - float(rb))
                if learned:
                    self._apply(o["ticker"], o["side"], learned, o["price"])
                o["remaining"] = 0.0
            elif kind == "fill_obs":
                fid = rec.get("fill_id")
                if fid is not None:
                    if str(fid) in self.seen_fill_ids:
                        continue             # S2: overlapping crash-gap windows by design
                    self.seen_fill_ids.add(str(fid))
                oid = rec.get("order_id")
                o = self.orders.get(str(oid)) if oid else None
                n = float(rec.get("count") or 0.0)
                if o is not None:
                    self._apply(o["ticker"], o["side"], n, o["price"])
                    o["remaining"] = max(0.0, o["remaining"] - n)
                else:
                    leg, sign = normalize_fill(rec.get("side"), rec.get("action", "buy"))
                    self._apply(rec.get("ticker"), leg, n,
                                float(rec.get("price_c") or 0.0) / 100.0,
                                float(rec.get("sign", sign)))
            elif kind == "assume_filled":
                o = self.orders.get(str(rec.get("order_id"))) if rec.get("order_id") else None
                if o is not None and o["remaining"] > 0:
                    self._apply(o["ticker"], o["side"], o["remaining"], o["price"])
                    o["remaining"] = 0.0
            elif kind == "expired":
                o = self.orders.get(str(rec.get("order_id")))
                if o is not None:
                    o["remaining"] = 0.0
            elif kind == "settlement":
                tk = rec.get("ticker")
                self.positions[tk] = {"yes": 0.0, "no": 0.0}
                self.cost_leg[tk] = {"yes": 0.0, "no": 0.0}
        return self

    def rows(self):
        """`{ticker, side, net, basis}` per held leg.  `side` is the LEG ("yes"/"no")."""
        out = []
        for ticker in sorted(self.positions):
            for leg in ("yes", "no"):
                n = self.positions[ticker].get(leg, 0.0)
                if n <= 1e-9:
                    continue
                cost = self.cost_leg.get(ticker, {}).get(leg, 0.0)
                out.append({"ticker": ticker, "side": leg, "net": round(n, 6),
                            "basis": round(cost / n, 6) if n else 0.0})
        return out


def gen_adopt(v4_ledger_records, now):
    """`lip_v5 --gen-adopt` — the whole of step 1.  Pure; the caller writes the file."""
    rows = V4Positions().replay(v4_ledger_records).rows()
    return {"schema": "lip_v5_adopt/1", "ts": float(now), "source": "v4_ledger",
            "positions": rows}


# =============================================================================================
# STEPS 2-4 — the W2 trust gate, basis sanity, orphan enumeration.
# =============================================================================================
ADOPTED, EXCLUDED_NET, EXCLUDED_BASIS, ORPHAN = \
    "adopted", "excluded_net_disagreement", "adopt_basis_rejected", "orphan_position"


def basis_ok(basis, mark=None, lo=C.ADOPT_BASIS_MIN, hi=C.ADOPT_BASIS_MAX,
             mark_mult=C.ADOPT_BASIS_MARK_MULT):
    """§6.3-C.3 — accept `basis` only if `0.01 ≤ basis ≤ 0.99` AND `basis ≤ 2 × current mark`.

    A ledger-era basis of $0.00 or $1.50 would otherwise make `inv_dollar_s`, `INV_CAP_USD`
    and the cash feed ALL WRONG IN THE SAME DIRECTION AT ONCE — the failure mode where three
    independent guards agree because they share one corrupted input.

    MIRROR (basis too HIGH ↔ too LOW): too high over-states inventory and under-sizes us (a
    rate loss); too low under-states it and lets the inventory cap admit more than $10 of real
    exposure (a capital loss).  Both are refused, because the band is two-sided — and the
    `2 × mark` test is the one that catches a basis that is INTERNALLY plausible but wrong for
    THIS market.
    """
    b = float(basis)
    if not (float(lo) <= b <= float(hi)):
        return False, "basis_out_of_band"
    if mark is not None and b > float(mark_mult) * float(mark) + 1e-9:
        return False, "basis_over_%gx_mark" % mark_mult
    return True, "ok"


def adoption_gate(adopt_rows, exchange_positions, marks=None, net_tol=1e-6):
    """§6.3-C steps 2-4.  Returns a dict of verdicts.

    `exchange_positions`: {(ticker, side): net}.  THE EXCHANGE IS AUTHORITATIVE ON `net`;
    v4's ledger is authoritative on `basis`.

    Any market where `net` disagrees is EXCLUDED from adoption and marked `assume_filled`
    (frozen for quoting AND RECYCLING) — a quote-only freeze is a naked-short generator
    (v1 §9.4b, kept verbatim).

    MIRROR (adopt too MUCH ↔ adopt too LITTLE): every exchange position NOT adopted is
    enumerated as `orphan_position`, alerted, and its market REFUSED FOR QUOTING — v4's
    inventory-slot guarantee, inverted.  Without this end, an unadopted position is invisible
    to every control in the binary, forever, which is exactly the live PYPL gap.
    """
    marks = marks or {}
    by_key = {(r["ticker"], r["side"]): r for r in (adopt_rows or [])}
    adopted, excluded, orphans, frozen, refused = [], [], [], set(), set()

    for key, row in sorted(by_key.items()):
        ticker, side = key
        ex_net = exchange_positions.get(key)
        if ex_net is None or abs(float(ex_net) - float(row["net"])) > net_tol:
            excluded.append({"ticker": ticker, "side": side, "reason": EXCLUDED_NET,
                             "ledger_net": row["net"], "exchange_net": ex_net})
            frozen.add(ticker)               # assume_filled: quoting AND recycling
            refused.add(ticker)
            continue
        ok, why = basis_ok(row["basis"], marks.get(key))
        if not ok:
            excluded.append({"ticker": ticker, "side": side, "reason": EXCLUDED_BASIS,
                             "basis": row["basis"], "mark": marks.get(key), "why": why})
            frozen.add(ticker)
            refused.add(ticker)
            R.log("adopt_basis_rejected", ticker=ticker, side=side, basis=row["basis"],
                  mark=marks.get(key), why=why)
            continue
        adopted.append({"ticker": ticker, "side": side, "net": float(row["net"]),
                        "basis": float(row["basis"])})

    for key in sorted(exchange_positions):
        if abs(float(exchange_positions[key])) <= net_tol:
            continue
        if key in by_key:
            continue
        orphans.append({"ticker": key[0], "side": key[1],
                        "exchange_net": float(exchange_positions[key])})
        refused.add(key[0])
        R.log("orphan_position", ticker=key[0], side=key[1],
              exchange_net=float(exchange_positions[key]))

    return {"adopted": adopted, "excluded": excluded, "orphans": orphans,
            "frozen": sorted(frozen), "refused_for_quoting": sorted(refused)}


# =============================================================================================
# CUTOVER TRIAGE — run ONCE at adoption.
# =============================================================================================
KEEP, MAKER_SHED, TAKER_CROSS = "keep", "maker_shed", "taker_cross"


def taker_fee_usd(n, p, rate=C.TAKER_FEE_RATE):
    """v1 §5.1 — `F = ceil(rate·n·p·(1−p))` ROUNDED UP TO THE CENT.  Rounding up is not
    conservatism theatre: the exchange rounds up, so rounding down would under-price every
    crossing decision in the direction that makes crossing look better than it is.

    The `round(..., 9)` is load-bearing, not cosmetic: `0.07·100·0.5·0.5` evaluates to
    1.7500000000000002 in binary floating point, and a bare `ceil` turns an exact $1.75 fee
    into $1.76.  A cent of phantom fee on every crossing decision biases the hold-vs-cross
    comparison toward HOLDING, which is the PayPal direction — so the dust is removed at the
    sub-cent scale where it can only be dust, before the deliberate rounding is applied.
    """
    p = float(p)
    cents = float(rate) * float(n) * p * (1.0 - p) * 100.0
    return math.ceil(round(cents, 9)) / 100.0


def triage_position(pos, venue, now, r_star, floor_rate=C.FLOOR_RATE_PER_H,
                    taker_enabled=C.TAKER_EXIT_ENABLED,
                    max_slippage_c=C.TAKER_EXIT_MAX_SLIPPAGE_C):
    """Judge ONE adopted position against (★) and choose its path.

    `pos`   : {"ticker","side","net","basis"}
    `venue` : {"rho","S","p","phi","d","close_ts","program_end_ts","l_shed_h","t_hat",
               "mark","spread_c","settled"}

    THE DECISION, in two parts:

    (1) **Does the venue still pass (★)?**  `net_rate(...) > λ_min/16`.  This is the same
        equation that would have refused the position at entry, applied to a position we did
        not open.  A venue that fails here is one v5 would never have entered — and the fact
        that the capital is ALREADY there changes nothing about whether it should be, because
        the carry term is forward-looking: it prices the hours we have not yet spent.

    (2) **If it fails, is LEAVING cheaper than WAITING?**  The maker shed is strictly preferred
        (v1 §5.4) and always available.  The crossing exit is worth its spread only when the
        carry we avoid exceeds the spread we pay:

            hold_cost  = n · basis · H_wait · r*            $ of opportunity cost while stuck
            cross_cost = n · slippage + taker_fee(n, p)     $ paid once, immediately
            CROSS iff cross_cost < hold_cost

        `H_wait` is the honest wait: the measured shed horizon if we HAVE one, otherwise
        `L_eff` — i.e. "the shed does not complete and we hold to settlement".  That default is
        what makes a long-dated mention market cross and a same-day daily shed: at L_eff =
        3744 h the hold cost is three orders of magnitude above any spread, and at L_eff = 8 h
        it is cents.

    G6: with `taker_enabled` false the crossing verdict is still COMPUTED and the VALUE
    FORGONE logged, and the path falls back to the maker shed.  The choice is measured rather
    than asserted, and one flag turns it on.
    """
    n = abs(float(pos["net"]))
    basis = float(pos["basis"])
    p = float(venue.get("p", basis))
    l_eff = M.l_eff_h(venue["close_ts"], now, venue.get("l_shed_h"),
                      settled=bool(venue.get("settled", False)))
    net = M.net_rate(venue["rho"], venue["S"], p, q=n,
                     phi=venue.get("phi", 0.0), d=venue.get("d", C.D_SEED_USD),
                     l_eff=l_eff, r_star=r_star, t_hat=venue.get("t_hat", 1.0))
    horizon_out = M.horizon_excluded(venue["close_ts"], now,
                                     venue.get("program_end_ts", venue["close_ts"]))
    verdict = {"ticker": pos["ticker"], "side": pos["side"], "net_rate": net,
               "l_eff_h": l_eff, "n": n, "basis": basis,
               "horizon_excluded": horizon_out}

    if M.admits(net, floor_rate) and not horizon_out:
        verdict["decision"] = KEEP
        verdict["exit_path"] = None
        verdict["reason"] = "passes_star"
        return verdict

    h_wait = venue["l_shed_h"] if venue.get("l_shed_h") is not None else l_eff
    hold_cost = n * basis * float(h_wait) * float(r_star)
    slippage = min(float(venue.get("spread_c", max_slippage_c)),
                   float(max_slippage_c)) / 100.0
    cross_cost = n * slippage + taker_fee_usd(n, p)
    verdict.update({"hold_cost_usd": hold_cost, "cross_cost_usd": cross_cost,
                    "h_wait_h": float(h_wait),
                    "reason": "horizon_excluded" if horizon_out else "fails_star"})

    if cross_cost < hold_cost:
        verdict["decision"] = TAKER_CROSS
        if taker_enabled:
            verdict["exit_path"] = TAKER_CROSS
            verdict["gate"] = "G6_enabled"
        else:
            # DECIDED AND LOGGED, NOT PLACED.  The value forgone is the whole point of
            # recording it: the choice becomes measured rather than asserted.
            verdict["exit_path"] = MAKER_SHED
            verdict["gate"] = "G6_disabled_fallback_maker_shed"
            verdict["value_forgone_usd"] = hold_cost - cross_cost
    else:
        verdict["decision"] = MAKER_SHED
        verdict["exit_path"] = MAKER_SHED
    return verdict


def triage(adopted, venues, now, r_star, floor_rate=C.FLOOR_RATE_PER_H,
           taker_enabled=C.TAKER_EXIT_ENABLED, enabled=C.CUTOVER_TRIAGE_ENABLED):
    """Run the triage over the whole adopted book, ONCE, at adoption.  Returns the verdict
    list; every entry is written to the ledger as a `cutover_triage` row so the cutover is
    auditable after the fact.  `venues` maps ticker → the venue dict of `triage_position`."""
    if not enabled:
        return []
    out = []
    for pos in adopted or []:
        v = venues.get(pos["ticker"])
        if v is None:
            # No venue reading for a position we hold is itself a refusal condition: we cannot
            # judge it, so we leave rather than guess.  Failing to a shed is the safe default
            # because the maker shed never crosses the spread.
            out.append({"ticker": pos["ticker"], "side": pos["side"], "decision": MAKER_SHED,
                        "exit_path": MAKER_SHED, "reason": "no_venue_reading",
                        "net_rate": None})
            continue
        out.append(triage_position(pos, v, now, r_star, floor_rate, taker_enabled))
    for verdict in out:
        R.log("cutover_triage", **verdict)
    return out


# =============================================================================================
# ROLLBACK AND HANDBACK  (spec §6.3, SF-2's honest boundary)
# =============================================================================================
class RollbackState(object):
    """`rollback_clean` — "v5 SIGTERM → systemctl start lip-maker-v4" is clean **ONLY BEFORE
    THE FIRST FILL ON AN ADOPTED POSITION**.  After that, v4's ledger no longer describes
    reality and restarting v4 on it re-imports a stale world.

    v5 logs `rollback_clean=true|false` on EVERY cycle so the operator never has to guess
    which regime they are in — the guess is the failure, not the regime.
    """

    def __init__(self):
        self.clean = True
        self.adopted_keys = set()
        self.first_dirty_fill = None

    def set_adopted(self, adopted):
        self.adopted_keys = {(a["ticker"], a["side"]) for a in adopted or []}

    def note_fill(self, ticker, side, now=None):
        """Any fill against an adopted position flips the boundary, permanently."""
        if (ticker, side) in self.adopted_keys and self.clean:
            self.clean = False
            self.first_dirty_fill = {"ticker": ticker, "side": side, "ts": now}
        return self.clean

    def procedure(self):
        return ("systemctl start lip-maker-v4" if self.clean
                else "systemctl start lip-maker-v4 with --import-handback")


def handback(positions, now, source="v5"):
    """§6.3 — v5's SIGTERM path ALWAYS writes `v5_handback.json`, a v4-readable position
    statement covering EVERY position v5 holds, in BOTH regimes (T-A4).

    "Always", not "when dirty": the operator learns which regime they are in from
    `rollback_clean`, and a file that exists only sometimes is a file nobody trusts when it
    matters.  Writing it in the clean regime costs one small file.
    """
    rows = [{"ticker": p["ticker"], "side": p["side"], "net": float(p["net"]),
             "basis": float(p["basis"]), "source": source, "ts": float(now)}
            for p in sorted(positions or [], key=lambda x: (x["ticker"], x["side"]))]
    return {"schema": "lip_v5_handback/1", "ts": float(now), "positions": rows}
