#!/usr/bin/env python3
"""
lip_v5 — presence-portfolio maker.  STAGED-INERT: this binary deploys ONLY on Ryan's explicit
word, never touches v4's paths, and every spending path sits behind its own human gate (§7).

=================================================================================================
NOTE 23 §III — THE FIVE, ANSWERED BEFORE FIRST LAUNCH  (spec §11 drafted them; these are
verified against the code in this package, not copied)
=================================================================================================

CASH.  v5 consumes collateral and creates inventory IN THE SHARED ACCOUNT until G7.  Every
  movement is published to `lip_cash_feed.json` BEFORE it happens (`cashfeed.publish_before_wire`,
  §5.3), so the published expected-cash is never above the truth.  Resolved-but-unpaid positions
  stay counted as CONSUMED CASH until the credit is confirmed IN CASH (`CashState.resolve` does
  not move `delta_dollars`; only `observe_balance`/`settlement_row` do — §5.2a, BLOCKER-1).
  nestor's breaker adds the feed to expected cash behind G0's flag.  ZERO HAND ENTRIES in steady
  state; `external_cash.jsonl` remains for deposits/manual trades only, and its v4-era rows are
  zeroed at G8.

BREAKER.  It reads `(external_cash.jsonl sum) + (lip_cash_feed.json, behind G0's flag, default
  IGNORE)`.  The NEGATIVE side (missing money — the dangerous direction) stays tight because v5
  OVER-reports consumption; the POSITIVE side is widened by `rewards_accrued_unpaid +
  inventory_settle_max + settled_payout_expected`.  Staleness > 120 s PAGES WITHOUT HALTING
  (§5.4) — halting on a stale feed would convert v5 dying into nestor dying.  `mode:"shared"`
  with G0's flag false is a v5 STARTUP REFUSAL, not a warning
  (`cashfeed.startup_refusal_reason`).

SCHEDULE.  Program `paid_out` flips ~2 h post-close (poll every 30 min) → `credit_pending` →
  ratchet input.  Settlements land per market close + ~41 min (R171), which is why
  `SETTLE_LAG_H = 0.7` is L_eff's floor.  `expiration_ts` = close − 240 s backstops EVERY order.
  The credits ritual reminder fires daily; two days without credits halts deployment.

COLLISIONS.  coid prefix `v5-` is disjoint from `v4-` and from nestor's.  v5 REFUSES TO START on
  a fresh v4 heartbeat (<120 s) — two makers on one rung is self-trade plus double collateral.
  Separate ledger/recon/seq/presence paths; ONE WRITER PER FILE; rate lanes §3.3; STP
  `taker_at_cross` on every order.  v5 never quotes a ticker nestor holds an open order on, read
  from nestor's own state file at cycle start — IF UNAVAILABLE THAT IS A STARTUP REFUSAL, NOT A
  WARNING.

ALERTS.  ntfy `senate-nestor-2732e947`: halt, poison, day stop, `assume_filled` freeze, venue
  stand-down, presence collapse, `lip_cash_feed_stale`, `settlement_cash_unconfirmed` (6 h),
  `orphan_position`, `adopt_basis_rejected`, `rate_starved` (10 min), `cancel_share_exceeded`,
  `idle_capital` (1 h), `rstar_no_converge` (3 cycles), coverage < 90% for 10 min, credits ritual
  due.  `NTFY_DISABLE` is honored BY CONSTRUCTION, and `runtime.ntfy` additionally refuses to
  page while the process is not `--live`, so a test can never reach a phone.  **The human is on
  the topic before G3.**

=================================================================================================
COMMANDS (each maps to one human gate in README.md; no gate bundles a capital change with a
code change)
=================================================================================================
  --check          G1: assertions only, no capital, no network writes.  Prints OK/FAIL per check.
                   NOTE-G: `--check` proves the BINARY, not the deployment.  It runs against
                   whatever data dir it is pointed at, does not read live programs unless a
                   scan is supplied (the unit assertion then prints SKIP, not OK), and reports
                   nestor-state readability as ADVISORY because at G1 v5 quotes nothing.  A
                   green `--check` therefore means "this artifact is internally consistent and
                   its money rule reproduces spec §0.4" — it does NOT mean "safe to quote".
                   That claim belongs to G2's shadow read-out and G3's first `allocate` line.
  --gen-adopt      §6.3-C.1: read v4's ledger READ-ONLY, write `v5_adopt.json`.  Re-runnable.
  --handback       §6.3 SF-2: write `v5_handback.json` from current state (also on SIGTERM).
  --shadow         G2: quote nothing, meter PSDH, publish a ZEROED cash feed.
  --live           G3+: required for any order or any page.  Absent ⇒ the runtime is INERT.
"""

import argparse
import os
import sys

if __package__ in (None, ""):                                # allow `python3 lip_v5.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "lip_v5"

from . import alloc, cashfeed, config as C, cutover, ledger, money, presence
from . import ratchet, ratelimit, runtime as R, scan, wsgate


# =============================================================================================
# STARTUP REFUSALS — spec §6.1, §4.4, §11.  Each is a REFUSAL, not a warning: every one of
# them describes a state in which running quietly is worse than not running.
# =============================================================================================
def v4_heartbeat_fresh(data_dir=None, now=None, fresh_s=C.V4_HEARTBEAT_FRESH_S):
    """spec §6.1 — refuse if a v4 heartbeat is fresh (<120 s) in `~/nestor/data/lip/`.

    MIRROR (v4 alive while v5 starts ↔ v4 dead while v5 assumes it alive): this guards the
    first; the ADOPTION GATE guards the second by enumerating every exchange position and
    refusing to quote any it did not adopt.
    """
    d = data_dir or C.DATA_DIR
    now = R._now() if now is None else float(now)
    newest = None
    if not os.path.isdir(d):
        return False, None
    for name in os.listdir(d):
        if not name.startswith("v4_") or not name.endswith(".jsonl"):
            continue
        try:
            m = os.path.getmtime(os.path.join(d, name))
        except OSError:
            continue
        newest = m if newest is None else max(newest, m)
    if newest is None:
        return False, None
    return (now - newest) < float(fresh_s), newest


def nestor_reader_enabled(env=None):
    """G0's flag, as v5 sees it.  Absent is FALSE — an unset flag means the reader is inert."""
    env = os.environ if env is None else env
    return str(env.get(C.NESTOR_READER_FLAG_ENV, "")).strip().lower() in ("1", "true", "yes")


def unit_assertion_check(programs, expect_usd=C.UNIT_ASSERT_EXPECT_USD,
                         tol=C.UNIT_ASSERT_TOL_USD, min_matches=C.UNIT_ASSERT_MIN_MATCHES):
    """spec §0.2 — `pool_usd = period_reward × 1e-4` WITH v1 §0.3's refusal assertion kept
    verbatim.  At least `min_matches` live programs must read exactly the MODAL $100.00 pool.

    A unit error is not subtle: at 1e-3 every program reads $1,000 and at 1e-5 every one reads
    $10.00, so the MATCH COUNT COLLAPSES TO ZERO rather than degrading.  That is why the
    assertion counts matches instead of checking one program — pinning it to one series once
    made v4 unstartable in the gap between a gas window closing and the next day's rungs
    listing, a self-inflicted outage on a constant that hundreds of programs attest to at once.
    """
    n = 0
    for p in programs or []:
        try:
            usd = float(p.get("period_reward", 0)) * C.PERIOD_REWARD_UNIT_USD
        except (TypeError, ValueError):
            continue
        if abs(usd - float(expect_usd)) <= float(tol):
            n += 1
    return n >= int(min_matches), n


def nestor_open_tickers(path=None):
    """spec §11 Collisions — v5 never quotes a ticker nestor holds an open ORDER on.
    Returns (set, ok).  `ok=False` is a STARTUP REFUSAL, not a warning: silently quoting into
    nestor's book because we could not read its state is the collision we are preventing."""
    obj = R.read_json(path or C.NESTOR_STATE_PATH, default=None)
    if obj is None:
        return set(), False
    tickers = set()
    for o in (obj.get("open_orders") or []):
        t = o.get("ticker") if isinstance(o, dict) else o
        if t:
            tickers.add(str(t))
    return tickers, True


def nestor_position_tickers(path=None):
    """Guard B13's second half — v5 never quotes a ticker nestor holds a POSITION on either.

    The order half alone is not enough: nestor can hold a position on a market it currently has
    no resting order in, and v5 quoting there attributes nestor's inventory to itself at the
    next position reconcile.  The two halves are ONE guard, which is why they are read together.
    """
    obj = R.read_json(path or C.NESTOR_STATE_PATH, default=None)
    if obj is None:
        return set()
    tickers = set()
    for p in (obj.get("positions") or []):
        t = p.get("ticker") if isinstance(p, dict) else p
        if t:
            tickers.add(str(t))
    return tickers


def nestor_state(path=None):
    """Both halves, in the shape `Maker.startup` consumes.  `None` when unreadable — which
    `startup` treats as a REFUSAL."""
    orders, ok = nestor_open_tickers(path)
    if not ok:
        return None
    return {"open_order_tickers": sorted(orders),
            "position_tickers": sorted(nestor_position_tickers(path))}


def go_artifact_ok(path=None):
    """The G3 gate artifact (charter D).  `--live` alone NEVER starts a quoting loop; the
    operator writes `v5_go.json` BY HAND (README step 4) and this verifies it.  Three
    requirements, each refusing a distinct accident:
      * the file exists and parses — a touch(1) or a truncated write is not a decision;
      * `operator` is non-empty — the decision has an author;
      * `gate` == "G3" — a leftover artifact from some other ceremony cannot arm this one.
    MIRROR (arming by accident ↔ unable to arm): the exact recipe lives in README step 4 and
    in this refusal's own output, so the gate is one hand-typed file, not folklore.
    """
    p = path or C.GO_ARTIFACT_PATH
    obj = R.read_json(p, default=None)
    if not isinstance(obj, dict):
        return False, ("gate artifact %s missing or unparseable; write it by hand per "
                       "README step 4" % p)
    if not str(obj.get("operator", "")).strip():
        return False, "gate artifact has no non-empty 'operator' field"
    if obj.get("gate") != "G3":
        return False, "gate artifact 'gate' != 'G3' (found %r)" % obj.get("gate")
    return True, "ok"


# =============================================================================================
# --live  (gate G3+) — the staged live branch (charter D)
# =============================================================================================
def run_live(args, now=None):                                # pragma: no cover - network
    """The real ExecStart.  Every refusal here happens BEFORE `set_live`, so a refused start
    can neither page nor reach the wire.  Sequence: gate artifact → key → arm → unit
    assertion (v1 §0.3, verbatim) → v4-heartbeat refusal → adopt file if present → init
    (which runs every startup refusal in `Maker.startup`) → the loop, whose every exit path
    runs shutdown."""
    from . import exchange as X, runner as RUN
    now = R._now() if now is None else float(now)
    go_path = os.path.join(args.data_dir, C.GO_ARTIFACT_NAME)
    ok, why = go_artifact_ok(go_path)
    if not ok:
        print("--live REFUSED: %s" % why)
        return 2
    auth, note = R.load_auth()
    if auth is None:
        print("--live REFUSED: no key (%s)" % note)
        return 2
    R.set_live(True)
    ex = X.Exchange(auth)

    # v1 §0.3's startup refusal assertion, kept verbatim: a wrong pool unit is a 10x-10,000x
    # sizing error and the binary must not run on one.
    status, body = ex.programs(None)
    programs = scan.parse_programs(body) if status == 200 else []
    ok_unit, n = unit_assertion_check(programs)
    if not ok_unit and C.REFUSE_ON_UNIT_MISMATCH:
        print("--live REFUSED: unit assertion failed (%d matches, need %d)"
              % (n, C.UNIT_ASSERT_MIN_MATCHES))
        return 2

    fresh, ts = v4_heartbeat_fresh(args.data_dir, now)
    if fresh:
        print("--live REFUSED: v4 heartbeat fresh (%.0fs ago) — two makers on one rung"
              % (now - ts))
        return 2

    adopt_obj = R.read_json(C.ADOPT_PATH, default=None)
    exchange_positions = None
    if adopt_obj is not None:
        st_p, pb = ex.positions()
        if st_p != 200:
            print("--live REFUSED: adopt file present but positions unreadable (HTTP %d)"
                  % st_p)
            return 2
        exchange_positions = {}
        for row in (pb or {}).get("market_positions") or []:
            net = float(row.get("position", 0))
            if net > 0:
                exchange_positions[(row.get("ticker"), "yes")] = net
            elif net < 0:
                exchange_positions[(row.get("ticker"), "no")] = -net

    r = RUN.build(ex, now, mode=args.mode, live=True)
    r.m.install_signal_handlers()
    ok, refusals = r.init(now, adopt_obj=adopt_obj, exchange_positions=exchange_positions,
                          nestor_state=nestor_state(), allow_fresh=args.allow_fresh,
                          reader_enabled=nestor_reader_enabled())
    if not ok:
        for msg in refusals:
            print("REFUSED: %s" % msg)
        return 2
    R.log("live_armed", operator_gate=go_path, mode=args.mode)
    r.run()
    return 0


# =============================================================================================
# --check  (gate G1)
# =============================================================================================
def run_check(argv_mode=C.CASH_MODE_SHARED, data_dir=None, programs=None, now=None,
              env=None, require_nestor_state=False):
    """G1's read-out: "prints OK for unit assertion, ledger replay, data dir, cash-feed write,
    WS gate, AND that G0's flag state matches v5's `mode`".

    Returns (ok, [(name, ok, detail)]).  NOTHING here writes outside the data dir and nothing
    here touches the network.
    """
    now = R._now() if now is None else float(now)
    data_dir = data_dir or C.DATA_DIR
    out = []

    # 1. data dir
    try:
        os.makedirs(data_dir, exist_ok=True)
        out.append(("data_dir", os.path.isdir(data_dir), data_dir))
    except OSError as exc:
        out.append(("data_dir", False, str(exc)))

    # 2. unit assertion (spec §0.2 — REFUSE TO RUN on mismatch)
    if programs is None:
        out.append(("unit_assertion", None, "skipped: no program snapshot (needs --live scan)"))
    else:
        ok, n = unit_assertion_check(programs)
        out.append(("unit_assertion", ok, "%d programs read $%.2f (need %d)"
                    % (n, C.UNIT_ASSERT_EXPECT_USD, C.UNIT_ASSERT_MIN_MATCHES)))

    # 3. ledger replay (schema-mismatch ABORT is the point of the check)
    lg = ledger.Ledger(os.path.join(data_dir, os.path.basename(C.LEDGER_PATH)))
    try:
        rows = lg.read()
        out.append(("ledger_replay", True, "%d rows" % len(rows)))
    except ledger.SchemaMismatch as exc:
        out.append(("ledger_replay", False, str(exc)))

    # 4. cash-feed write (temp+rename, into the data dir, never the live path)
    try:
        st = cashfeed.CashState(mode=argv_mode)
        pub = cashfeed.CashFeedPublisher(os.path.join(data_dir, "v5_cash_feed_check.json"), st)
        f = pub.publish(now)
        os.unlink(pub.path)
        out.append(("cash_feed_write", f["schema"] == C.CASH_FEED_SCHEMA, "seq=%d" % f["seq"]))
    except Exception as exc:
        out.append(("cash_feed_write", False, "%s: %s" % (type(exc).__name__, exc)))

    # 5. WS gate — the module imports and the gate refuses an ungated market by default
    try:
        g = wsgate.WsGate()
        out.append(("ws_gate", not g.passed("ANY"), "requires %d agreements" % g.required))
    except Exception as exc:
        out.append(("ws_gate", False, "%s: %s" % (type(exc).__name__, exc)))

    # 6. G0's flag state vs v5's mode (spec §4.4 mirror; §7 G1's own read-out)
    reader = nestor_reader_enabled(env)
    refusal = cashfeed.startup_refusal_reason(argv_mode, reader)
    out.append(("g0_flag_matches_mode", refusal is None,
                refusal or "mode=%s reader=%s" % (argv_mode, reader)))

    # 7. v4 heartbeat freshness (spec §6.1)
    fresh, ts = v4_heartbeat_fresh(data_dir, now)
    out.append(("v4_not_running", not fresh,
                "newest v4 file %s" % ("none" if ts is None else "%.0fs ago" % (now - ts))))

    # 8. NOTE-H — nestor collision state (spec §11 Collisions).  WIRED: `Maker.startup`
    #    REFUSES when this is unreadable, and takes BOTH halves (open orders AND positions,
    #    guard B13).  It is reported here as advisory rather than fatal because `--check` runs
    #    at G1, where v5 quotes nothing: refusing the arming step for a file that only matters
    #    once we intend to quote would block the gate that proves the binary is sound.
    #    `require_nestor_state=True` makes it fatal, and G3's command sets it.
    tickers, ok_state = nestor_open_tickers()
    positions = nestor_position_tickers()
    out.append(("nestor_state_readable", ok_state if require_nestor_state else (ok_state or None),
                "%s (%d open orders, %d positions)%s"
                % (C.NESTOR_STATE_PATH, len(tickers), len(positions),
                   "" if ok_state else " — REFUSAL at G3, advisory at G1")))

    # 9. (★) self-check against spec §0.4's own worked numbers.  A binary whose money rule
    #    disagrees with the spec's own table must not start.
    t = money.net_terms(0.439, 50, 0.30, 0, 0.50, 0.07, 3744.0, 0.00625, 1.0)
    out.append(("star_reproduces_spec_0_4", abs(t["net"] - (-11.80)) < 5e-3,
                "PYPL net=%.5f (spec −11.80)" % t["net"]))

    ok_all = all(o is not False for _, o, _ in out)
    return ok_all, out


def print_check(results):
    for name, ok, detail in results:
        mark = "OK  " if ok else ("SKIP" if ok is None else "FAIL")
        print("[%s] %-28s %s" % (mark, name, detail))


# =============================================================================================
# --gen-adopt / --handback  (spec §6.3)
# =============================================================================================
def run_gen_adopt(v4_ledger_path=None, out_path=None, now=None):
    """READ-ONLY on v4's ledger.  Writes v5's own adopt file.  Re-runnable by construction —
    it is a pure function of v4's ledger, so running it twice cannot drift."""
    now = R._now() if now is None else float(now)
    recs = R.read_jsonl(v4_ledger_path or C.V4_LEDGER_PATH)
    obj = cutover.gen_adopt(recs, now)
    R.atomic_write_json(out_path or C.ADOPT_PATH, obj)
    return obj


def run_handback(positions=None, out_path=None, now=None, adopt_path=None):
    """§6.3 SF-2 — write a v4-readable position statement.

    With no `positions` supplied this reconstructs them the same way a cold operator would: from
    v5's own ledger if it has one, else from the adopt file.  A handback command that only works
    while the process is alive is useless in precisely the case it exists for — v5 already
    dead, and v4 needing to be restarted onto reality.
    """
    now = R._now() if now is None else float(now)
    if positions is None:
        rows = R.read_jsonl(C.LEDGER_PATH)
        if rows:
            positions = cutover.V4Positions().replay(rows).rows()
        else:
            adopt = R.read_json(adopt_path or C.ADOPT_PATH, default=None) or {}
            positions = adopt.get("positions", [])
    obj = cutover.handback(positions, now)
    R.atomic_write_json(out_path or C.HANDBACK_PATH, obj)
    return obj


def run_shadow(now=None, ex=None, iterations=3, auth=None):
    """G2's REAL read-out, produced by the ASSEMBLED loop: scan → classify → slots → metering →
    `venue_rank` → zeroed cash feed, quoting NOTHING.

    `shadow=True` makes `Maker.place` refuse before the rate lane, so "quotes nothing" is a
    property of the one path to the wire rather than of this function remembering not to call it.

    **G2 reads but does not write.**  Metering and venue ranking need the programs feed and the
    books, so shadow needs the NETWORK — which `runtime.set_live` gates.  Without `--live` this
    runs against a `FakeExchange` and says so: an offline rehearsal of the loop's shape, useful
    for proving the wiring and useless as a venue ranking.  The distinction is printed, because
    a shadow read-out that silently ranked nothing would look exactly like a healthy quiet book.
    """
    from . import exchange as X, runner as RUN
    now = R._now() if now is None else float(now)
    online = ex is not None or (R.is_live() and auth is not None)
    ex = ex or (X.Exchange(auth) if (R.is_live() and auth is not None) else X.FakeExchange())
    r = RUN.build(ex, now, shadow=True)
    ok, refusals = r.init(now, nestor_state=nestor_state(), reader_enabled=True)
    if not ok:
        for msg in refusals:
            print("REFUSED: %s" % msg)
        return {"refused": refusals}
    for i in range(int(iterations)):
        r.iteration(now + i)
    out = r.m.shadow_readout(now + iterations, slots=r.slots)
    out["online"] = online
    print("source: %s" % ("LIVE exchange" if online
                          else "FakeExchange (offline rehearsal — NOT a venue ranking)"))
    print("programs %d, classified %d, slots %d"
          % (len(r.scanner.programs), len(r.classifier.table), len(r.slots)))
    print("venue_rank: %d slots ranked, %d admitted"
          % (len(out["venue_rank"]), sum(1 for r in out["venue_rank"] if r["admits"])))
    for r in out["venue_rank"][:20]:
        print("  %-32s %-4s net=%+.5f gross=%.5f carry=%.5f drift=%.5f T=%.2f %s"
              % (r["ticker"], r["side"], r["net"], r["gross"], r["carry"], r["drift"],
                 r["t_hat"], "ADMIT" if r["admits"] else "refuse"))
    print("PSDH populated for %d (m,s); cash feed %s; orders placed %d"
          % (out["psdh_covered"], out["cash_feed"], out["quoted"]))
    return out


# =============================================================================================
# MAIN
# =============================================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(prog="lip_v5", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="G1: assertions only, no capital")
    ap.add_argument("--gen-adopt", action="store_true", help="§6.3-C.1 adoption file")
    ap.add_argument("--handback", action="store_true", help="§6.3 SF-2 handback statement")
    ap.add_argument("--shadow", action="store_true", help="G2: meter only, zeroed cash feed")
    ap.add_argument("--live", action="store_true",
                    help="G3+: REQUIRED for any order or any page.  Absent = INERT.")
    ap.add_argument("--allow-fresh", action="store_true",
                    help="B7 escape: blank ledger against a non-flat account, STATED "
                         "(the cutover's one legitimate instance)")
    ap.add_argument("--mode", default=C.CASH_MODE_SHARED,
                    choices=[C.CASH_MODE_SHARED, C.CASH_MODE_SUBACCOUNT])
    ap.add_argument("--data-dir", default=C.DATA_DIR)
    args = ap.parse_args(argv)

    R.set_write_roots([C.NESTOR_HOME, args.data_dir])
    # `set_live` is NOT set from the bare flag here: the quoting branch arms itself only
    # after the G3 gate artifact and the key both check out (run_live), so a refused start
    # is inert end to end.  --shadow --live still needs the runtime armed for its reads.
    if args.live and args.shadow:
        R.set_live(True)

    if args.check:
        ok, results = run_check(args.mode, args.data_dir)
        print_check(results)
        print("\n--check: %s" % ("OK" if ok else "FAIL"))
        return 0 if ok else 1

    if args.gen_adopt:
        obj = run_gen_adopt()
        print("wrote %s: %d positions" % (C.ADOPT_PATH, len(obj["positions"])))
        return 0

    if args.handback:
        obj = run_handback()
        print("wrote %s: %d positions (%s)"
              % (C.HANDBACK_PATH, len(obj["positions"]),
                 "from v5 ledger" if R.read_jsonl(C.LEDGER_PATH) else "from adopt file"))
        return 0

    if args.shadow:
        print("G2 shadow mode: quoting disabled, metering only, cash feed zeroed.")
        auth, note = R.load_auth()
        if args.live and auth is None:
            print("--shadow --live needs a key: %s" % note)
            return 1
        run_shadow(auth=auth)
        return 0

    if args.live:
        # G3+ is a HUMAN GATE: `--live` alone NEVER quotes.  `run_live` verifies the
        # operator-created gate artifact (v5_go.json, README step 4) BEFORE arming anything;
        # without it this prints the refusal and exits 2 — and the unit file's
        # RestartPreventExitStatus honors that as "do not retry a human decision".
        return run_live(args)

    ap.print_help()
    return 2


if __name__ == "__main__":                                   # pragma: no cover
    sys.exit(main())
