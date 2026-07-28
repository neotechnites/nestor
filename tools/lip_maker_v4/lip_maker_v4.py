#!/usr/bin/env python3
"""
lip_maker_v4 — production Kalshi Liquidity-Incentive-Program maker.

Implements `enchiridion/work/spec-lip-maker-v1.md` (2026-07-28, post-adversarial-review).
Predecessor: `~/kalshi_data/scripts/lip_requoter_v3.py` (probe grade).  Every proven mechanic
of v3 is carried over verbatim in shape; every named v3 DEFECT is fixed here and the fix is
named at the site.

Python 3.12 stdlib + `requests` + `cryptography` only.

=============================================================================================
OPERATIONS DESCENT (note 23 §III) — the five answers.  No five answers, no operation.
=============================================================================================
1. CASH — what moves, when, which ledger sees it.
   Collateral leaves the balance only on FILL, not on rest (R161, demo+prod verified).  A
   resting bid of q at price p can consume at most q*p; a resting ask at price a can consume
   at most q*(1-a) (an ask IS a NO bid at 100-a).  MAX_TOTAL_COLLATERAL_USD ($45 first run,
   spec §8.3 config) is the hard GROSS ceiling and is checked before every post, against a
   ledger-reconstructed number (spec §9.3), never against an exchange index (§8.6).
   ALLOCATE never sees the ceiling: it sees `budget = ceiling - max_slot_collateral`
   (§2.4), so the transient double-collateral of make-before-break (§4.1) always fits.
   Ledger visibility: this process runs standalone (§13.2 decision).  Until the separate
   subaccount exists (§13.4) the whole sleeve sits inside a FIXED, pre-declared external-cash
   reservation equal to the ceiling.  This process NEVER writes nestor's state.json or
   external_cash.jsonl — two writers on those files is itself a defect.
2. BREAKER — what the divergence check reads, both directions.
   Negative side: a fill converts cash -> position with no nestor-side offset, pushing the
   negative divergence breaker.  Bound = MAX_TOTAL_COLLATERAL_USD, which is why the ceiling
   (not the order-count cap) is the load-bearing safety property of this file.  Positive side
   (settlement + LIP credit) only ever moves divergence back toward zero.  Cancels return
   collateral and can never consume it.
3. SCHEDULE — what fires later, each pre-covered.
   Per PROGRAM (windows are 16h..228h+, §0.5 — nothing here is a clock offset):
     * program start_date            -> T0 land-grab + qualification gate (§6.1/6.2)
     * 25/50/80/94% of the window    -> rescue checkpoints (§3.4)
     * end_date - CLOSE_MARGIN (4m)  -> T3 hard cancel-all + flatten (§6.5)
     * expiration_ts on every order  = end_date - 4m, belt-and-braces behind the explicit
       cancel-all (v3 note: expiration_ts is an UNPROVEN gate, never the first line).
   Nothing this process starts fires after the last live program's end_date.
4. COLLISIONS — coids, self-trade, rate budget, state writers, dedupe.
   * coids: "v4-{ticker}-{y|n}-{seq}", '.'->'_' at construction (R167: Kalshi 400s a coid
     with a dot).  `seq` is a PERSISTED counter, so coids are unique forever; the PREFIX
     carries NO run-id, so a restarted process can sweep-cancel its predecessor's orders
     (§9.5 — a run-id in the prefix is exactly v3's loss).
   * self-trade: self_trade_prevention_type=taker_at_cross on every order.  Make-before-break
     posts the NEW order on the SAME side at a non-crossing price, so the two legs can never
     trade against each other (§4.1).
   * rate budget: 1 Hz book poll per market against ~10 req/s shared => REST is clamped to
     MAX_REST_MARKETS = 6 (§4.6).  Breadth past 6 needs the websocket orderbook_delta feed,
     which is NOT implementable inside this dependency set — see UNDERIVED U7 in the report.
   * state writers: this process writes only ~/nestor/data/lip/*.
   * dedupe/self-knowledge: the resting-orders and positions INDEXES ARE NEVER READ (R169,
     §8.6).  The single exception is the fills endpoint, scoped by order_id or by time, used
     ONLY in the §9.4 restart disambiguation.
5. ALERTS — who gets paged at 3am.
   ntfy topic senate-nestor-2732e947 (§13.6) on: halt, poison, stop-loss, coverage <90% for
   10 min, §12.3 stand-down, and every exit.  KNOWN DARK LINK: Ryan may not be subscribed, so
   this process is FAIL-CLOSED — every failure path cancels everything it owns and exits
   rather than continuing to quote, and expiration_ts backstops even a SIGKILL.

USAGE
  python3 lip_maker_v4.py --dry     # live books + live scanner, ZERO POST/DELETE
  python3 lip_maker_v4.py --live    # arms the exchange writes
  python3 -m unittest discover tools/lip_maker_v4/      # money rules, no network
"""

import argparse
import base64
import json
import math
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone

try:                                              # tests must import with no network stack
    import requests
except Exception:                                 # pragma: no cover
    requests = None


# =============================================================================================
# CONFIG — every constant carries its spec-section derivation.  A constant without a
# derivation is an undeclared claim (note 23 §II) and must not exist in this block.
# =============================================================================================

# ---- endpoints -------------------------------------------------------------------------
BASE = "https://api.elections.kalshi.com"          # §7.1 / v3-proven prod host
PREFIX = "/trade-api/v2"
ORDERS_PATH = "/portfolio/events/orders"           # §4.7 — /portfolio/orders 410s (v1 defect)
HTTP_TIMEOUT = 15                                  # v3-proven

# ---- identity / paths ------------------------------------------------------------------
COID_PREFIX = "v4-"                                # §9.5 stable across restarts: NO run-id
NESTOR_HOME = os.environ.get("NESTOR_HOME", os.path.expanduser("~/nestor"))
ENV_CANDIDATES = [os.environ.get("NESTOR_ENV_FILE", ""),
                  os.path.join(NESTOR_HOME, ".env"),
                  os.path.expanduser("~/Documents/senate/nestor/.env")]
PEM_DEFAULT = os.path.join(NESTOR_HOME, "secrets", "prod.pem")
DATA_DIR = os.environ.get("LIP_DATA_DIR", os.path.join(NESTOR_HOME, "data", "lip"))
LEDGER_PATH = os.path.join(DATA_DIR, "v4_ledger.jsonl")          # §9.1
RECON_PATH = os.path.join(DATA_DIR, "recon.jsonl")               # §12.1
OPERATOR_PATH = os.path.join(DATA_DIR, "pools_operator.jsonl")   # §7.2
SEQ_PATH = os.path.join(DATA_DIR, "v4_coid_seq")                 # §9.5 persisted counter
PROGRAMS_CACHE_FMT = os.path.join(DATA_DIR, "programs-%Y%m%d.json")   # §7.1
NTFY_TOPIC = "senate-nestor-2732e947"                            # §13.6

# ---- THE UNIT (§0.3 / §14.5-T28b / §15.0) ----------------------------------------------
# period_reward is in units of $1e-4.  VERIFIED on two direct anchors (min=10,000=$1.00 = the
# filing's minimum payout; 1,000,000=$100.00 = the help-centre worked example) plus the
# filing's $10-$1,000/day cap.  A wrong unit is a 10x or 10,000x sizing error, so it is
# additionally enforced by a STARTUP ASSERTION against a live program.
PERIOD_REWARD_UNIT_USD = 1e-4
UNIT_ASSERT_SERIES = "KXAAAGASD"        # §0.3's original anchor: a live gas daily rung
UNIT_ASSERT_EXPECT_USD = 100.00
UNIT_ASSERT_TOL_USD = 0.01
REFUSE_ON_UNIT_MISMATCH = True          # §0.3 "or REFUSE TO RUN"
# The assertion's PURPOSE is verifying PERIOD_REWARD_UNIT_USD against known truth — not
# verifying gas.  Pinning it to one series made the process unstartable in the gap between a
# gas window closing (03:59Z) and the next day's rungs listing, which is a self-inflicted
# outage on a $1e-4 constant that 585 of 771 live programs attest to simultaneously.
# New form: at least UNIT_ASSERT_MIN_MATCHES live programs must read exactly $100.00 (the
# MODAL pool).  30 gives ~20x margin against program-mix drift while still failing loudly,
# because a unit error is not subtle — at 1e-3 every one of them reads $1,000 and at 1e-5
# every one reads $10.00, so the matching count collapses to zero rather than degrading.
# Gas, when live, is kept as a non-blocking belt on the original anchor.
UNIT_ASSERT_MIN_MATCHES = 30

# ---- scoring (§0.2 / §11.5) -------------------------------------------------------------
DISCOUNT_FACTOR_DEFAULT = 0.50          # discount_factor_bps 5000 on 100% of live programs
MAX_LEGAL_PRICE_C = 99                  # §1.3 highest valid Kalshi limit price
MIN_LEGAL_PRICE_C = 1
S_MODE_ENTRY = "levels"                 # §1.5 conservative (S_levels >= S_cents) for ENTRY
S_MODE_RECON = "cents"                  # §1.5 our reading of the filing, for reconciliation

# ---- allocation (§2) --------------------------------------------------------------------
LAMBDA_MIN = 0.10                       # §2.3 reward-$/collateral-$ per 16h-equivalent.
LAMBDA_MIN_WINDOW_HOURS = 16.0          # §2.3 "per 16h-equivalent" => lambda_min/16 in $/h
STEP_FRACTION = 0.02                    # §2.5 coarsest step landing within 2% of optimum
PHI_MID = 0.08                          # §2.2 fills/h/contract, mid-priced side (R174)
PHI_CHEAP = 0.001                       # §2.2 p<0.05: deep-OTM strikes trade in blocks
# ^ N1, FLAGGED BY REVIEW: this seed may be ~10x LOW.  verify-lip-gas §3d measured the mid
#   rungs at 100-300 contracts/hour, and §2.2's own PHI_MID (0.08 fills/h/contract at q~100)
#   is calibrated off that flow; if deep-OTM strikes saw even a tenth of mid-rung flow the
#   implied phi would be ~0.01, not 0.001.  §2.2 asserts they do not ("deep-OTM strikes
#   trade in blocks, not flow") and the seed follows the spec.  The consequence of the seed
#   being 10x low is a 10x-low hurdle on cheap sides (0.001 -> 0.01 /h), i.e. we would
#   over-fund cheap slots whose fill cost we are under-charging.  KEPT AS SPEC'D, listed as
#   UNDERIVED (§15.4): replace with the own-tape estimate per (series, side-band) at n>=200
#   fills, which is the measurement that settles it.  Do not tune it by hand in between.
PHI_CHEAP_PRICE_CUT = 0.05              # §2.2
D_SEED_USD = 0.07                       # §2.2 drift 5-9c/cross-cycle pair, midpoint
PHI_D_MIN_OWN_FILLS = 200               # §2.2 seeds hold until 200 own fills per (series,band)
MAX_GATE_PASSES = 8                     # §2.4 lines 12-15 re-water-fill loop, bounded

# ---- forfeit floor (§3) -----------------------------------------------------------------
ENTRY_FLOOR_USD = 2.00                  # §3.1 = 2x the $1.00 payout cliff = the system's own
                                        #       declared tolerable model error (charter §8)
RESCUE_TARGET_USD = 1.10                # §3.3 = $1.00 boundary + 1c round-down + 9c buffer
CHECKPOINT_FRACTIONS = (0.25, 0.50, 0.80, 0.94)   # §3.4 WINDOW FRACTIONS, never clock offsets
# ---- runway guard (LIVE DEFECT: late entry at scale) ------------------------------------
# Observed live: 735 lots posted on gas 4.120 and 50 on 4.110 with under 25 minutes left in
# that program's window.  ALLOCATE optimises a RATE ($/h per collateral-$) and is blind to
# how many hours remain to earn it, so a dying program looks identical to a fresh one — and
# the §3.1 forfeit gate cannot save us either, because it grades the PERIOD projection,
# which late in a window is dominated by accrual we will never make.
# Derivation (never hardcoded): entering is only rational if the §3.1 ENTRY_FLOOR is still
# REACHABLE in the time left.  A slot's reward rate is (rho/2)*share.  Use a CONSERVATIVE
# share, not the sole-qualifier ideal of 1.0 — assuming we take the whole side is exactly
# the optimism that produces late entries:
#     ENTRY_SHARE_ASSUMPTION * (rho/2) * h  >=  ENTRY_FLOOR
#     =>  h  >=  ENTRY_FLOOR / (ENTRY_SHARE_ASSUMPTION * rho / 2)
# At rho = $6.25/h (a gas rung) and ENTRY_FLOOR = $2.00 this is 1.28h.  It scales with the
# pool: a fat program needs less runway, a thin one more, which is the correct shape.
ENTRY_SHARE_ASSUMPTION = 0.5
# ---- window START guard + pre-positioning lead ------------------------------------------
# The runway guard checks the window END and nothing checked the window START, so v4 entered
# three WNBA-mention slots whose programs open 10.5h later (15:00Z), locking ~$11 while
# live-window PYPL posts were being turned away on `collateral_ceiling` in the same second.
# UNDER A BINDING CEILING EVERY NON-EARNING DOLLAR DISPLACES AN EARNING DOLLAR 1:1, so a
# pre-start quote is not merely idle, it is a transfer out of the earning book.
# A short lead is still worth buying, because §6.1/6.2's land-grab is real: books are
# near-empty at the open and the qualification gate there is worth up to half a period pool.
# Derivation of the default.  Before its window opens a resting order earns EXACTLY ZERO and
# carries the full §2.2 marginal fill cost phi*d per contract-hour.  What the lead buys is
# certainty of being at-best when the window opens; what it must cover is one cold classify
# sweep (200 markets at CLASSIFY_RATE_HZ = 40s), a book poll, and a couple of SAFETY_RESYNC_S
# cycles -- call it ~3 minutes, so 15 minutes is a 5x margin on the thing being bought.
# What it costs at the seeded phi/d on a mid-priced 100-lot quote is
# phi*d*q*h = 0.08*0.07*100*0.25 = $0.14 of expected drift, against a gate worth tens of
# dollars -- two orders of magnitude in favour.  The cost is LINEAR in the lead while the
# benefit saturates within minutes, which is exactly why 10.5h was catastrophic and 0.25h is
# cheap: at the observed 10.5h the same quote pays $5.88 of drift PLUS the displacement.
# 0.25h is also 1.5% of the shortest program window (16h), so it can never dominate.
PREPOSITION_LEAD_H = 0.25

# ---- requote / cadence (§4) -------------------------------------------------------------
MAKE_BEFORE_BREAK = True                # §4.1 strictly dominant when the balance exists
CANCEL_FIRST_PERIOD_S = 46              # §4.2 T* = sqrt(2g/a), g=1.2s, a=1/900/s -> 46s
MIN_RESTING_LIFE_S = 30                 # §4.4 anti-gaming P1; trigger (a) overrides
REFILL_TRIGGER_FRAC = 0.50              # §4.3(b) remaining < 50% of target q -> top up
S_MOVE_TRIGGER_FRAC = 0.25              # §4.3(c) S moved >25% -> re-run ALLOCATE
SAFETY_RESYNC_S = 60                    # §4.3(e) catches missed stream events
BOOK_POLL_HZ = 1.0                      # §4.3 triggers evaluated at 1 Hz per market
MAX_REST_MARKETS = 6                    # §4.6 1 Hz x N vs ~10 req/s shared budget
# ---- classification sweep: the input to the §4.6 clamp (B1 fix) -------------------------
# The clamp must choose WHICH 6 markets to poll at 1 Hz.  Ranking by rho alone is degenerate
# INSIDE one event — every rung of a gas daily carries the identical period_reward and the
# identical window, so rho cannot separate them and the ticker tie-break decides.  On a gas
# daily the low tickers are the deep-ITM rungs, which are PINNED: measured 2026-07-28T02:11Z,
# a rho-ranked clamp picked 4.070/4.075/4.080/4.085/4.090/4.095, of which FOUR can never pay,
# and never polled 4.100/4.105/4.110 — the three best slots on the board.  Fix: classify
# first (a cheap low-cadence sweep that learns pinned/qualifies/S/p per market), then clamp
# the 1 Hz loop to the best 6 by the ALLOCATOR'S OWN first-dollar marginal rate.
CLASSIFY_REFRESH_S = 900                # re-classify every 15 min: pinned-ness changes only
                                        # when a 99c/1c tick-boundary order moves, which is
                                        # far slower than the 1 Hz quoting loop
CLASSIFY_MAX_MARKETS = 200              # bound the cold-start sweep.  rho DOES rank across
                                        # events (different pools), it only fails within one,
                                        # so the top-200 by rho is a sound candidate net.
CLASSIFY_RATE_HZ = 5.0                  # half the ~10 req/s shared budget; the sweep is
                                        # one-shot at start and every CLASSIFY_REFRESH_S
REVIVAL_PRICE_USD = 0.01                # §6.2 the cheapest legal price on a dead side
# NEW-1: §6.2's T0 qualification path (post `target_size - cum_size` on a short side) is
# DERIVED and TESTED (`t0_qualification_size`) but has ZERO CALL SITES — the same class of
# defect as the unwired day stop.  Until it is wired, an unqualified side is worth exactly
# nothing to us: ALLOCATE sees S=0, computes a marginal rate of 0, and funds it $0 forever.
# Ranking such a market highly therefore burns one of six REST slots on a market that
# receives no capital (measured: a REVIVE market ranked #1 at 0.3125 and allocated 0).
# While this is False the §4.6 clamp ignores unqualified sides.  Flip it ONLY together with
# the T0 call site — the flag exists so the two can never drift apart again.
T0_QUALIFICATION_WIRED = False
COVERAGE_TARGET = 0.95                  # §4.5 probe §4
COVERAGE_ALERT_FLOOR = 0.90             # §4.5 alert if a slot is <90% for 10 min
COVERAGE_ALERT_WINDOW_S = 600           # §4.5
CLOSE_MARGIN_S = 240                    # §4.7/§6.5 expiration_ts and T3 = window_close - 4min

# ---- inventory recycling (§5) -----------------------------------------------------------
TAKER_FEE_RATE = 0.07                   # §5.1 F = ceil(0.07*n*p*(1-p)) rounded up to the cent
SHED_PATIENCE_S = 1800                  # §5.4(ii) shed quote unfilled for 30 min
SHED_ESCALATE_HOURS_LEFT = 2.0          # §5.4(iii) h < 2
# TAKER_EXIT is DECIDED and LOGGED but NOT PLACED at this rung of the ladder.  Derivation:
# §5.4 makes the maker shed strictly preferred and confines escalation to (h<2 or a global
# cap breach) AFTER 30 unfilled shed minutes.  At MAX_TOTAL_COLLATERAL_USD = $45 the §8.1
# net cap bounds stranded inventory at $10 per slot, so what a taker exit can recover is the
# blocked-slot rate over the last <2h of a window — single dollars.  Against that, this
# would be the ONLY code path in this binary able to cross the spread, and §8.8 says abort
# on "a fill at a price we did not intend": the tail loss is the whole sleeve, not $10.
# The arithmetic flips as the ceiling rises (R_blocked scales with deployed size while the
# crossing risk stays a fixed tail), so this is a LADDER-RUNG decision, not a permanent one:
# re-enable at the $300 rung.  Every suppressed exit logs the value forgone, so the choice
# is measured rather than asserted.  README documents that inventory does not self-clear
# beyond the maker shed while this is False.
TAKER_EXIT_ENABLED = False
# NEW-5: the derivation above is a function of the CEILING, so the two must not drift.  At
# or above this ceiling a startup assertion REFUSES TO RUN while TAKER_EXIT_ENABLED is
# False.  Deliberately NOT an auto-enable: crossing the spread is a human decision and this
# forces it to be taken explicitly at the rung, not inherited from a smaller one.
TAKER_EXIT_REQUIRED_ABOVE_USD = 300.0

# ---- risk caps (§8) ---------------------------------------------------------------------
INV_CAP_USD = 10.00                     # §8.1 n_cap = floor($10/p) on NET.  A slot's max
                                        #      earning is ~$50/window; $10 = 20% of that.
PER_MARKET_POOL_MULT = 4.0              # §8.2 never risk 4x a market's own maximum prize
PER_MARKET_BUDGET_FRAC = 0.25           # §8.2 no single-market concentration
MAX_TOTAL_COLLATERAL_USD = 45.0         # §8.3 FIRST-RUN rung of the R168 ladder.  Each rung
                                        #      is funded by the previous window's OBSERVED
                                        #      print, never by the model.  Hard refuse.
DAY_STOP_FRAC = 0.35                    # §8.4 largest drag leaving the day net-positive
DAY_STOP_FLOOR_USD = 20.0               # §8.4 probe convention
DAY_STOP_CAP_USD = 150.0                # §8.4 a larger single-day loss invalidates the ladder
MAX_CONSEC_CANCEL_ANOMALIES = 3         # §8.5 v3-inherited, non-negotiable
MAX_POST_ERRORS = 6                     # §8.5 6 post errors in 5 min globally
POST_ERROR_WINDOW_S = 300               # §8.5
REFILL_CAP_TURNOVERS = 4                # §8.7 4*n_cap per window; beyond that the slot is a
                                        #      flow magnet, not a maker

# ---- anti-gaming (§10) ------------------------------------------------------------------
P3_TWO_SIDED_COLLATERAL_MIN = 0.40      # §10.3-P3 UNDERIVED (§15.8)
P3_TWO_SIDED_MARKET_MIN = 1.0 / 3.0     # §10.3-P3 UNDERIVED (§15.8)
P4_FILL_HONOR_TARGET = 0.95             # §10.3-P4 investigate below 0.90 for a day
P4_FILL_HONOR_FLOOR = 0.90
P6_LOOKBACK_DAYS = 5                    # §10.3-P6 / UNDERIVED (§15.9)
P7_MAX_REVIVAL_MARKETS = 3              # §10.3-P7
P7_MAX_SIDE_SHARE = 0.90                # §10.3-P7 never >90% of a qualifying side ...
P7_MAX_SIDE_SHARE_DAYS = 5              # ... for more than 5 consecutive days
P5_CHEAP_SIDE_ALERT = 0.95              # §10.3-P5 telemetry alert, NEVER a block
P5_CHEAP_SIDE_ALERT_DAYS = 3
DISCLOSE_ABOVE_DEPLOYED_USD = 10000.0   # §10.4 disclose before any deployment above $10k
NO_DISCLOSE_BELOW_DEPLOYED_USD = 2000.0 # §10.4 do not disclose below $2k deployed
DISCLOSE_RUNRATE_SHARE = 0.05           # §10.4 or immediately if >5% of exchange-wide/day
EXCHANGE_RUNRATE_USD_DAY = 41698.0      # §10.4 measured live LIP run-rate
PROGRAM_EV_LOW_USD = 3400.0             # §10.1 print both numbers every run so the
PROGRAM_EV_HIGH_USD = 8000.0            #       revocation tradeoff is never made implicitly
REVIVAL_EV_USD = 3400.0                 # §10.1 ~$100/day x 34 days

# ---- reconciliation / stand-down (§12) --------------------------------------------------
PAID_OUT_POLL_S = 1800                  # §7.3 poll every 30 min; flips within ~2h of close
CREDIT_MISSING_ALERT_S = 86400          # §7.3a alert if paid_out and no credit row after 24h
STANDDOWN_LOG2_RATIO = 1.0              # §12.3(a) |log2(paid/model)| > 1 == worse than 2x
STANDDOWN_DAYS = 2                      # §12.3 two consecutive settlement days
BOOK_SNAPSHOT_S = 30                    # §12.4 sample the window's OWN book every 30s

# ---- restart / recovery (§9) ------------------------------------------------------------
FILLS_REQUERY_DELAY_S = 36              # §9.4a 3x the ~12s worst observed index lag
CRASH_GAP_LOOKBACK_S = 60               # §9.4(4) fills query over [last_ledger_ts-60s, now]

# ---- scanner (§7) -----------------------------------------------------------------------
SCAN_PAGE_LIMIT = 1000                  # §7.1 cursor-paged, ~120 pages at limit=1000
SCAN_MAX_PAGES = 200                    # bound the pull; 119,615 programs / 1000 = ~120
DENY_SERIES = {"KXRAIN"}                # §7.4 seed deny: measured toxic, 40 markets wide
EVENT_ALLOWLIST = []                    # FIRST-RUN: EMPTY == OFF.  The scanner ranks
                                        # everything (§7.4).  Populate to restrict the
                                        # shakeout to one event, e.g. ["KXAAAGASD-26JUL29"].

DRY = True                              # set by main(); every write path asserts on it


# =============================================================================================
# LOGGING — every event is one JSONL record on the ledger (v3's log() pattern, §9.1).
# =============================================================================================
def _now():
    """The ONLY clock.  time.time(), everywhere, always (no Date.now-style traps)."""
    return time.time()


def _utcstamp(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else _now(),
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(event, **fields):
    rec = {"t": _now(), "ts": _utcstamp(), "event": event, "dry": DRY}
    rec.update(fields)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LEDGER_PATH, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())          # §9.1 fsync'd BEFORE the next wire call
    except Exception as exc:               # logging must never kill us
        print("LOGFAIL %s: %s" % (type(exc).__name__, exc))
    print("[%s] %s %s" % (rec["ts"], event,
                          json.dumps({k: v for k, v in fields.items()}, default=str)[:400]))


# =============================================================================================
# PURE — numbers, units, coids
# =============================================================================================
def num(v, default=0.0):
    """Kalshi fixed-point fields arrive as "10.00" strings OR numbers (v3-proven)."""
    if v is None:
        return default
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return default


def dig(body, key):
    """Field lookup tolerant of the {"order": {...}} envelope (v3-proven)."""
    if not isinstance(body, dict):
        return None
    if key in body:
        return body[key]
    inner = body.get("order")
    if isinstance(inner, dict) and key in inner:
        return inner[key]
    return None


def pool_usd(period_reward):
    """§0.3 / T28b.  period_reward is in units of $1e-4."""
    return num(period_reward, 0.0) * PERIOD_REWARD_UNIT_USD


def unit_assertion_ok(period_reward, expect_usd=UNIT_ASSERT_EXPECT_USD,
                      tol=UNIT_ASSERT_TOL_USD):
    """§0.3 startup assertion, as a pure predicate so it is testable without network."""
    return abs(pool_usd(period_reward) - expect_usd) <= tol


def unit_assertion_check(programs, expect_usd=UNIT_ASSERT_EXPECT_USD,
                        tol=UNIT_ASSERT_TOL_USD, min_matches=UNIT_ASSERT_MIN_MATCHES,
                        series=UNIT_ASSERT_SERIES):
    """§0.3 startup assertion, as a pure function.  Returns (ok, detail_dict).

    PASS iff at least `min_matches` live liquidity programs convert to exactly `expect_usd`.
    If any program of `series` is live, one of them must ALSO read `expect_usd` — a belt on
    the original anchor that is skipped, not failed, when the series is between windows.
    """
    matches = [p for p in (programs or [])
               if abs(pool_usd(p.get("period_reward")) - expect_usd) <= tol]
    in_series = [p for p in (programs or []) if p.get("series") == series]
    series_ok = None                                  # None == not live, nothing to assert
    if in_series:
        series_ok = any(abs(pool_usd(p.get("period_reward")) - expect_usd) <= tol
                        for p in in_series)
    ok = len(matches) >= min_matches and series_ok is not False
    return ok, {
        "n_programs": len(programs or []),
        "n_at_expect": len(matches),
        "min_required": min_matches,
        "samples": [str(p.get("market_ticker")) for p in matches[:3]],
        "series": series,
        "series_live": len(in_series),
        "series_ok": series_ok,
    }


def pool_rate(period_reward, window_hours):
    """rho = pool_usd / window_hours, $/h (§0.4).  Windows are 16h..228h+ (§0.5)."""
    if window_hours is None or window_hours <= 0:
        return 0.0
    return pool_usd(period_reward) / float(window_hours)


def window_hours(start_ts, end_ts):
    """§0.5 — the program's OWN window, from the program object.  Never hardcoded."""
    if start_ts is None or end_ts is None:
        return 0.0
    return max(0.0, (float(end_ts) - float(start_ts)) / 3600.0)


def sanitize(s):
    """R167: Kalshi 400s any client_order_id containing a dot.  '.' -> '_' AT CONSTRUCTION."""
    return str(s).replace(".", "_")


def make_coid(ticker, side, seq):
    """§9.5 `lipm-{ticker_sanitized}-{y|n}-{seq}` under the STABLE v4- prefix.

    The prefix carries NO run-id: the restart sweep (§9.4 step 4) must be able to recognise
    the PREVIOUS process's orders.  A run-id in the prefix is exactly v3's loss.
    """
    yn = "y" if side == "bid" else "n"
    return sanitize("%slipm-%s-%s-%d" % (COID_PREFIX, ticker, yn, int(seq)))


def owns_coid(coid):
    """§9.4 step 4 sweep predicate — prefix only, so it spans restarts (T35)."""
    return isinstance(coid, str) and coid.startswith(COID_PREFIX)


def price_str(p):
    return "%.4f" % p


def unit_collateral(side, price_dollars):
    """Collateral per contract on the single YES book (v3-proven, kalshi.rs:413-414).

    side "bid" = buy YES at price          -> price
    side "ask" = sell YES at price = buy NO at (1 - price) -> 1 - price
    """
    return float(price_dollars) if side == "bid" else (1.0 - float(price_dollars))


def closing_rooms(net_yes):
    """FIX-A — closing capacity a position offers, per side, before anything consumes it.

    `net_yes` = yes contracts held minus no contracts held, on that market.
      * long YES (net > 0): an ASK sells YES, closing up to `net_yes`.
      * long NO  (net < 0): a BID buys YES, closing up to `-net_yes`.
    """
    net = float(net_yes)
    return {"ask": max(0.0, net), "bid": max(0.0, -net)}


def allocate_closing_room(orders, net_yes):
    """FIX-A-1 — assign a position's closing capacity to the orders already RESTING against
    it, and report what is left.  Returns (resting_collateral_usd, remaining_room).

    This is the single source of truth for "how much closing room is still available", and
    both the resting-collateral view and the pre-placement ceiling check must read it.  They
    previously disagreed: placement netted against the RAW net position, so with 20 YES held
    and one 20-lot closing ask already resting, a SECOND 20-lot ask also priced at $0 and
    the ceiling approved it — then both rested and `resting_collateral` jumped by $8.00 that
    the check had priced at zero.  Make-before-break creates exactly that two-orders-one-side
    shape on every requote, and because closing orders priced at $0 the FIX-B ceiling guard
    never fired for them, so the ceiling was BREACHED rather than degraded.

    Deterministic in order_id so the split is stable across replays.
    """
    room = closing_rooms(net_yes)
    total = 0.0
    per_order = {}
    for o in sorted(orders, key=lambda x: str(x.order_id)):
        r = o.resting
        if r <= 0:
            continue
        c = min(r, room.get(o.side, 0.0))
        room[o.side] = room.get(o.side, 0.0) - c
        per_order[str(o.order_id)] = c
        total += (r - c) * unit_collateral(o.side, o.price)
    return total, room, per_order


def closing_qty(side, size, net_yes=0.0, room=None):
    """How much of an order of `size` on `side` CLOSES inventory already held.  Pass `room`
    to charge against capacity that resting orders have NOT already consumed (FIX-A-1); it
    falls back to the raw position when no resting orders exist."""
    if room is None:
        room = closing_rooms(net_yes).get(side, 0.0)
    return min(float(size), max(0.0, float(room)))


def order_collateral_usd(side, price_dollars, size, net_yes=0.0, room=None):
    """FIX-A — collateral an order COMMITS, net of the part that closes held inventory.

    The live deadlock this fixes: at a saturated ceiling the recycler decided MakerShed
    every second, but the shed order was charged as if it were fresh exposure
    (`would_add $2.88` against 19.95 held), so the ceiling check skipped it forever and the
    inventory could not recycle — it locked until settlement, which is precisely the
    "inventory is expensive because it BLOCKS THE SLOT" failure §5.3 exists to prevent.

    A closing order cannot increase exposure: its worst case is that we end up FLAT.
    Charging it is not conservatism, it is a model error, and it makes the ceiling
    self-sealing exactly when the recycler is trying to unseal it.

    Partial case (the one that bites): a 25-lot ask against 19.95 held is 19.95 closing
    plus 5.05 opening — only the 5.05 tail is charged.
    """
    closing = closing_qty(side, size, net_yes, room)
    opening = max(0.0, float(size) - closing)
    return opening * unit_collateral(side, price_dollars)


def normalize_fill(side, action="buy"):
    """B3 — translate the FILLS API's vocabulary into the ledger's own.  Returns
    (leg_side, sign) where leg_side is "bid"|"ask" and sign is +1 for an acquisition and
    -1 for a disposal.

    The fills payload speaks (side=yes|no, action=buy|sell).  The ledger speaks the ORDER
    axis: "bid" means the YES leg, "ask" means the NO leg.  Replay previously mapped raw
    fills-payload sides with `"yes" if side == "bid" else "no"`, so a fills row carrying
    side="yes" fell through to the NO leg — a buy of 25 YES at 30c was booked as no:25 at
    70c, i.e. net -25 instead of +25.  Sign-INVERTED, on the exact path that imports fills
    we did not see.
    And `action` was dropped entirely, so a SELL was booked as an acquisition — which is
    precisely how an operator's manual sale of a position would import as MORE inventory.
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


def order_body(ticker, side, price_dollars, expiration_ts, coid, count):
    """§4.7 — the exact v3-proven V2 resting-order body."""
    return {
        "ticker": ticker,
        "side": side,                                   # bid = buy YES, ask = sell YES
        "count": "%.2f" % count,
        "price": price_str(price_dollars),              # 4-dp dollars, YES axis
        "time_in_force": "good_till_canceled",          # §4.7
        "expiration_ts": int(expiration_ts),            # = window_close - 4 min
        "self_trade_prevention_type": "taker_at_cross", # §4.7 STP
        "client_order_id": coid,                        # dot-free by construction
    }


# =============================================================================================
# PURE — the CFTC scoring algorithm (§0.2, §1.2, §11.5).  T23-T27.
# =============================================================================================
class SideScore(object):
    __slots__ = ("ref_c", "S", "qualifies", "top_size", "next_level_gap", "cum_size", "reason")

    def __init__(self, ref_c=None, S=0.0, qualifies=False, top_size=0.0,
                 next_level_gap=None, cum_size=0.0, reason=""):
        self.ref_c = ref_c
        self.S = S
        self.qualifies = qualifies
        self.top_size = top_size
        self.next_level_gap = next_level_gap
        self.cum_size = cum_size
        self.reason = reason

    def __repr__(self):
        return ("SideScore(ref=%s S=%.4f qual=%s top=%s)"
                % (self.ref_c, self.S, self.qualifies, self.top_size))


def score_side(levels, target_size, df=DISCOUNT_FACTOR_DEFAULT, mode=S_MODE_RECON,
               max_price_c=MAX_LEGAL_PRICE_C):
    """The filing algorithm, verbatim.  `levels` = [(price_cents, size), ...] on ONE side,
    quoted as bids in that side's own currency.

    1. Reference Price = the highest bid on that side, "if it exists and is less than the
       highest possible price".  A book whose best is AT the cap has no reference price.
    2. Walk DOWN from the reference accumulating size; every level touched joins the
       qualifying set; stop once cumulative >= Target Size.
    3. If bids run out before Target Size is reached the qualifying set is CLEARED (T25).
    4. Score(bid) = df^(RefPrice - Price) * Size.   `mode`:
         "cents"  -> exponent is the arithmetic cent distance (our reading of the filing)
         "levels" -> exponent is the book-level index (Q5's other reading)
       §1.5: S_levels >= S_cents always, so S_levels is the CONSERVATIVE entry input.
    """
    lv = sorted([(int(round(p)), float(s)) for p, s in (levels or []) if float(s) > 0],
                key=lambda x: -x[0])
    if not lv:
        return SideScore(reason="empty_side")
    ref = lv[0][0]
    top = lv[0][1]
    gap = (ref - lv[1][0]) if len(lv) > 1 else None
    if ref >= max_price_c:
        # "if it exists and is less than the highest possible price" — no reference price.
        return SideScore(ref_c=ref, S=0.0, qualifies=False, top_size=top,
                         next_level_gap=gap, cum_size=0.0, reason="ref_at_cap")
    S = 0.0
    cum = 0.0
    qualifies = False
    for i, (p, sz) in enumerate(lv):
        dist = (ref - p) if mode == "cents" else i
        S += sz * (df ** dist)
        cum += sz
        if cum >= target_size:
            qualifies = True
            break
    if not qualifies:
        S = 0.0                       # T25: the qualifying set is CLEARED, not partial
    return SideScore(ref_c=ref, S=S, qualifies=qualifies, top_size=top,
                     next_level_gap=gap, cum_size=cum,
                     reason="" if qualifies else "target_size_not_reached")


def our_share(q, S):
    """§0.2/§11.2 — pro-rata by size at the same-side best; DF^0 = 1 for us.  T24."""
    if q <= 0:
        return 0.0
    return float(q) / (float(q) + float(S))


def is_pinned(yes_bid_c, yes_ask_c):
    """§1.3 — permanently unscoreable: no LEGAL resting price exists on the missing side.

    yes_bid at 99c  => a NO bid would need a yes ask above 99c: illegal.
    yes_ask at 1c   => a YES bid would need a price below 1c: illegal.
    """
    if yes_bid_c is not None and yes_bid_c >= MAX_LEGAL_PRICE_C:
        return True
    if yes_ask_c is not None and yes_ask_c <= MIN_LEGAL_PRICE_C:
        return True
    return False


def is_revivable(yes_bid_c, yes_ask_c, yes_qualifies, no_qualifies):
    """§1.4 — one side fails the Target-Size gate but a legal price exists on it."""
    if is_pinned(yes_bid_c, yes_ask_c):
        return False
    return not (yes_qualifies and no_qualifies)


def best_from_book(body):
    """(yes_bid_c, yes_ask_c) — v3-proven orderbook_fp parsing.  Both sides of a Kalshi
    *_fp book are quoted as BIDS in their own currency; yes_ask = 100 - best no bid."""
    ob = body.get("orderbook") if isinstance(body, dict) else None
    if not isinstance(ob, dict):
        ob = body if isinstance(body, dict) else {}
    fp = ob.get("orderbook_fp") or (body or {}).get("orderbook_fp") or {}
    if not isinstance(fp, dict):
        return None, None
    yc = _levels_cents(fp.get("yes_dollars"))
    nc = _levels_cents(fp.get("no_dollars"))
    yb = max([p for p, _ in yc]) if yc else None
    nb = max([p for p, _ in nc]) if nc else None
    ya = (100 - nb) if nb is not None else None
    return yb, ya


def _levels_cents(levels):
    out = []
    for lv in levels or []:
        try:
            out.append((int(round(float(lv[0]) * 100)), float(lv[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def book_levels(body):
    """(yes_levels, no_levels) in [(price_cents, size)] form, for score_side."""
    ob = body.get("orderbook") if isinstance(body, dict) else None
    if not isinstance(ob, dict):
        ob = body if isinstance(body, dict) else {}
    fp = ob.get("orderbook_fp") or (body or {}).get("orderbook_fp") or {}
    if not isinstance(fp, dict):
        return [], []
    return _levels_cents(fp.get("yes_dollars")), _levels_cents(fp.get("no_dollars"))


# =============================================================================================
# PURE — allocation (§2).  T1-T7.
# =============================================================================================
class Caps(object):
    """§8.1/§8.2.  Caps are a PARAMETER of allocate, decoupled from post size (§8.7)."""
    __slots__ = ("inv_cap_usd", "per_market_pool_mult", "per_market_budget_frac")

    def __init__(self, inv_cap_usd=INV_CAP_USD,
                 per_market_pool_mult=PER_MARKET_POOL_MULT,
                 per_market_budget_frac=PER_MARKET_BUDGET_FRAC):
        self.inv_cap_usd = inv_cap_usd
        self.per_market_pool_mult = per_market_pool_mult
        self.per_market_budget_frac = per_market_budget_frac


class Slot(object):
    """One (market, side).  `S` is the QUALIFYING-SET score for that side and already
    includes any wall resting at the best price, so production slots carry W = 0.  `W` exists
    only so the §2.7 wall arithmetic can be exercised with a wall stated separately."""
    __slots__ = ("ticker", "side", "rho", "S", "p", "W", "pinned", "denied",
                 "legal_price_exists", "p6_ok", "phi", "d", "program_id", "window_h",
                 "pool", "assume_filled", "target_size", "cum_size", "hours_left",
                 "accrued", "hours_to_start")

    def __init__(self, ticker, side, rho, S, p, W=0.0, pinned=False, denied=False,
                 legal_price_exists=True, p6_ok=True, phi=None, d=None, program_id=None,
                 window_h=16.0, pool=None, assume_filled=False, target_size=1000,
                 cum_size=0.0, hours_left=None, accrued=0.0, hours_to_start=0.0):
        self.ticker = ticker
        self.side = side                    # "bid" | "ask"
        self.rho = float(rho)               # $/h over the program's OWN window (§0.4/§0.5)
        self.S = float(S)                   # rival qualifying score (S_levels for entry)
        self.p = float(p)                   # collateral $/contract at the same-side best
        self.W = float(W)
        self.pinned = pinned
        self.denied = denied
        self.legal_price_exists = legal_price_exists
        self.p6_ok = p6_ok                  # §10.3-P6 pre-entry filter (ALLOCATE line 2)
        self.phi = PHI_CHEAP if (phi is None and p < PHI_CHEAP_PRICE_CUT) else (
            PHI_MID if phi is None else float(phi))
        self.d = seed_drift(p) if d is None else float(d)
        self.program_id = program_id if program_id is not None else ticker
        self.window_h = float(window_h)
        self.pool = pool if pool is not None else float(rho) * float(window_h)
        self.assume_filled = assume_filled
        self.target_size = target_size
        self.cum_size = cum_size
        # hours of the program's OWN window still to run, and what we have accrued in it
        self.hours_left = float(window_h) if hours_left is None else float(hours_left)
        self.accrued = float(accrued)
        self.hours_to_start = max(0.0, float(hours_to_start))

    @property
    def key(self):
        return (self.ticker, self.side)

    def __repr__(self):
        return "Slot(%s/%s p=%.4f S=%.2f rho=%.4f)" % (self.ticker, self.side, self.p,
                                                       self.S, self.rho)


def seed_drift(p):
    """§2.2 seeded d.  The $0.07 SEED is capped at p because the max adverse move on a bid
    of price p is p itself.  The cap is a property of the SEED, not of the formula: a
    caller-supplied measured `d` is used as given (this is what makes T4c reachable)."""
    return min(D_SEED_USD, float(p))


def seed_phi(p):
    """§2.2.  Deep-OTM strikes trade in blocks, not flow."""
    return PHI_CHEAP if float(p) < PHI_CHEAP_PRICE_CUT else PHI_MID


def hurdle(phi, d, p):
    """§2.2 (B4) — MARGINAL, not average.

        hurdle = d(fillcost_rate)/dq / p = phi * d / p      $/h per collateral-$

    Fill rate is linear in resting size (f(q) = phi*q) so the marginal fill cost is
    SIZE-INDEPENDENT: computable at q = 0, no division by zero, no empty allocation.
    The average form fillcost_rate/(q*p) divides by zero at q=0 and must never be written.
    """
    if p <= 0:
        return float("inf")
    return float(phi) * float(d) / float(p)


def marginal_rate(rho, S_eff, q, p):
    """§0.4 — d(reward_rate)/dq / p = rho*S / (2*p*(q+S)^2), $/h per collateral-$."""
    if p <= 0:
        return 0.0
    denom = (float(q) + float(S_eff))
    if denom <= 0:
        return 0.0
    return float(rho) * float(S_eff) / (2.0 * float(p) * denom * denom)


def reward_rate(rho, q, S_eff):
    """§0.4 — (rho/2) * q/(q+S), $/h."""
    if q <= 0:
        return 0.0
    return (float(rho) / 2.0) * float(q) / (float(q) + float(S_eff))


def wall_indifference_size(rho, W, S, p, r_star):
    """§2.7 — q* = sqrt(rho*(W+S)/(2*p*r*)) - (W+S).  q* < 0  =>  do not quote at all."""
    ws = float(W) + float(S)
    if ws <= 0 or p <= 0 or r_star <= 0:
        return 0.0
    return math.sqrt(float(rho) * ws / (2.0 * float(p) * float(r_star))) - ws


def should_improve(rho, q, S, r_star, tick=0.01):
    """§2.6 — improving makes us the reference and halves every incumbent's score.
    Improve iff (rho/2)*[q/(q+S/2) - q/(q+S)] > q*tick*r*.  Evaluated PER SLOT; there is
    no price-band shortcut (N5)."""
    if q <= 0 or S <= 0:
        return False
    gain = (float(rho) / 2.0) * (q / (q + S / 2.0) - q / (q + S))
    cost = float(q) * float(tick) * float(r_star)
    return gain > cost


def slot_first_dollar_rate(rho, S, p, qualifies=True, legal=True,
                           target_size=1000.0, revival_price=REVIVAL_PRICE_USD):
    """§0.4 marginal rate at q = 0, i.e. `rho/(2*p*S)` — the value of this slot's FIRST
    collateral dollar, which is exactly the quantity ALLOCATE maximises.  Used to rank
    markets for the §4.6 poll clamp so that the clamp and the allocator agree.

    A legal-but-UNQUALIFIED side (§1.4 revival) has S = 0, where the marginal form is
    degenerate.  The size we would have to post to create the qualifying side IS
    `target_size`, at the minimum legal price, so its first-dollar rate is
    `rho/(2*revival_price*target_size)` — which is why revivals rank high (§1.4 "highest
    return on the board") rather than being silently dropped as S = 0.
    """
    if not legal or rho <= 0:
        return 0.0
    if not qualifies:
        return marginal_rate(rho, float(target_size), 0, float(revival_price))
    if S <= 0 or p <= 0:
        return 0.0
    return marginal_rate(rho, float(S), 0, float(p))


def market_rank_value(entry, count_unqualified=None):
    """Best first-dollar rate available on one market.  `entry` carries the LAST OBSERVED
    classification: {"rho", "pinned", "denied", "sides": [{"S","p","qualifies","legal"}]}.
    A pinned or denied market is worth exactly zero — no snapshot on it can ever be
    included, so no dollar spent on it can ever be paid (§1.3).

    NEW-1: an UNQUALIFIED side is likewise worth zero TO THE CLAMP while
    T0_QUALIFICATION_WIRED is False, because nothing in this binary will ever post the size
    that creates the qualifying side, so ALLOCATE funds it $0 and the REST slot is wasted.
    The revival arithmetic in `slot_first_dollar_rate` stays correct and tested; this gates
    USING it to spend a poll budget.
    """
    if count_unqualified is None:
        count_unqualified = T0_QUALIFICATION_WIRED
    if entry.get("pinned") or entry.get("denied"):
        return 0.0
    rho = float(entry.get("rho", 0.0))
    best = 0.0
    for s in entry.get("sides", []):
        if not s.get("qualifies", True) and not count_unqualified:
            continue
        best = max(best, slot_first_dollar_rate(
            rho, s.get("S", 0.0), s.get("p", 0.0), s.get("qualifies", True),
            s.get("legal", True), s.get("target_size", 1000.0)))
    return best


def market_poll_rank(classified, max_markets=MAX_REST_MARKETS, count_unqualified=None):
    """§4.6 CLASSIFY-THEN-CLAMP.  Returns the tickers to poll at 1 Hz, best first.

    Ranking by rho alone is degenerate within one event (every rung shares one pool and one
    window), so the ticker tie-break decides — and on a gas daily the low tickers are the
    deep-ITM PINNED rungs.  Ranking by the allocator's own first-dollar rate makes the clamp
    agree with ALLOCATE, and excluding pinned markets OUTRIGHT (not merely down-ranking
    them) guarantees no REST slot is spent on a market that can never pay.
    """
    scored = []
    for ticker, entry in classified.items():
        if entry.get("pinned") or entry.get("denied"):
            continue
        v = market_rank_value(entry, count_unqualified)
        if v <= 0:
            continue
        scored.append((-v, -float(entry.get("rho", 0.0)), str(ticker)))
    scored.sort()
    return [t for _, _, t in scored[:max_markets]]


def unpriced_positions(positions, yes_mids):
    """Tickers holding inventory for which no two-sided mid exists (§8.4 / NEW-2)."""
    return sorted(t for t, pos in (positions or {}).items()
                  if (abs(pos.get("yes", 0.0)) + abs(pos.get("no", 0.0))) > 0
                  and yes_mids.get(t) is None)


def mark_to_market_pnl(positions, position_cost, yes_mids, fees_paid_usd=0.0):
    """§8.4 realised+unrealised P&L on inventory.  `yes_mids` is {ticker: yes mid in $}.
    A YES contract marks at the yes mid; a NO contract marks at (1 - yes mid).  Cost comes
    from the ledger replay (§9.3), never from an exchange index.

    NEW-2: a position on a market with NO two-sided mid (a PINNED rung is one-sided BY
    DEFINITION, §1.3) cannot be marked.  Subtracting its full cost while contributing no
    value reads that inventory as a TOTAL LOSS: two pinned $10 slots alone print -$20,
    which is exactly the §8.4 floor, and the day stop then cancels everything mid-window on
    precisely the gas books we are there for.  An unmarkable position marks AT COST — zero
    P&L contribution, the only honest statement about a price we cannot observe.  The count
    is surfaced separately so "we cannot see it" never reads as "it is fine".
    """
    value = 0.0
    cost = dict(position_cost or {})
    for ticker, pos in (positions or {}).items():
        mid = yes_mids.get(ticker)
        if mid is None:
            value += cost.get(ticker, 0.0)          # mark at cost => contributes 0.0
            continue
        value += pos.get("yes", 0.0) * float(mid) + pos.get("no", 0.0) * (1.0 - float(mid))
    return value - sum(cost.values()) - float(fees_paid_usd)


def day_stop_breached(pnl_usd, projected_day_reward_usd):
    """§8.4 — breach when the LOSS reaches the stop.  On breach: cancel-all -> flatten ->
    alert -> exit."""
    return -float(pnl_usd) >= day_stop_usd(projected_day_reward_usd) - 1e-12


def preposition_ok(hours_to_start, lead_h=PREPOSITION_LEAD_H):
    """Window START guard: a program may only be entered once it has started, or is within
    `lead_h` of starting.  `hours_to_start` is 0 for a program already running."""
    return float(hours_to_start) <= float(lead_h) + 1e-12


def in_window(hours_to_start, hours_left, lead_h=PREPOSITION_LEAD_H):
    """True iff a program is earning now, or will be within the pre-positioning lead."""
    return preposition_ok(hours_to_start, lead_h) and float(hours_left) > 0.0


def min_runway_h(rho, floor_usd=ENTRY_FLOOR_USD, share=ENTRY_SHARE_ASSUMPTION):
    """Hours of window a slot needs for the §3.1 ENTRY_FLOOR to be REACHABLE at a
    conservative share.  See the ENTRY_SHARE_ASSUMPTION derivation."""
    if rho <= 0 or share <= 0:
        return float("inf")
    return float(floor_usd) / (float(share) * float(rho) / 2.0)


def runway_ok(rho, hours_left, accrued_usd=0.0, floor_usd=ENTRY_FLOOR_USD,
              share=ENTRY_SHARE_ASSUMPTION, rescue_target=RESCUE_TARGET_USD):
    """Runway guard.  Refuse to ENTER or TOP UP a slot whose program cannot still reach the
    entry floor — UNLESS we have already accrued past RESCUE_TARGET there, in which case
    this is a rescue and §3.5 owns the decision (topping up a nearly-paid program late is
    exactly what §3.6 says beats redeploy)."""
    if float(accrued_usd) >= float(rescue_target) - 1e-12:
        return True
    return float(hours_left) >= min_runway_h(rho, floor_usd, share)


def n_cap(p, caps=None):
    """§8.1 — floor($10/p) on NET.  Scales as 1/p: 25 at 40c, 500 at 2c."""
    caps = caps or Caps()
    if p <= 0:
        return 0
    return int(math.floor(caps.inv_cap_usd / float(p)))


def market_cap_usd(slot, budget_usd, caps=None):
    """§8.2 — collateral <= min(4*rho*H, 0.25*budget).  rho*H is the market's own pool."""
    caps = caps or Caps()
    return min(caps.per_market_pool_mult * slot.rho * slot.window_h,
               caps.per_market_budget_frac * float(budget_usd))


def reserve_budget(ceiling_usd, max_slot_collateral_usd):
    """§2.4 (B3) — budget = ceiling - max_slot_collateral.  Make-before-break transiently
    holds TWO copies of one slot's collateral; without the reserve the LARGEST slot's requote
    is rejected exactly when the book is moving, i.e. the failure is correlated with the
    moment presence matters most.  T4b."""
    return max(0.0, float(ceiling_usd) - max(0.0, float(max_slot_collateral_usd)))


def allocate(slots, budget_usd, caps=None, lambda_min=LAMBDA_MIN, r_star_wall=None):
    """ALLOCATE (§2.4).  Marginal-rate water-filling.  `budget_usd` is the §2.4 budget, NOT
    the raw ceiling.  Returns (alloc {(ticker,side): qty}, spent_usd).

    Deviation from the spec pseudocode, DERIVED and surfaced (see report D-IMPL-1):
    line 10's `break` when the CURRENT best slot can no longer afford one contract would
    abandon the remaining budget even when a CHEAPER slot could still absorb it, which
    contradicts T5 ("no lazy under-fill").  Here such a slot is marked permanently
    unaffordable (budget only ever decreases, so the exclusion can never be wrong) and the
    loop continues; it breaks only when NO slot can afford one more contract.
    """
    caps = caps or Caps()
    # N3: a negative budget is reachable from §2.4 (reserve_budget clamps at 0, but a caller
    # may pass ceiling - max_slot directly).  A negative budget must fund NOTHING, not wrap
    # into a step of floor(negative/p) somewhere downstream.
    budget_usd = max(0.0, float(budget_usd))
    lam_h = float(lambda_min) / LAMBDA_MIN_WINDOW_HOURS     # §2.3 in $/h
    rw = lam_h if r_star_wall is None else float(r_star_wall)

    alloc = {}
    elig = []
    for s in slots:
        alloc[s.key] = 0
        if s.pinned or s.denied or not s.legal_price_exists:
            continue
        if not s.p6_ok:                                     # §10.3-P6 pre-entry filter
            continue
        if s.assume_filled:                                 # §9.4b freeze, T32b
            continue
        if not runway_ok(s.rho, s.hours_left, s.accrued):   # runway guard (window END)
            continue
        if not preposition_ok(s.hours_to_start):            # window START guard
            continue
        if s.p <= 0 or s.rho <= 0 or (s.S + s.W) <= 0:
            continue
        # §2.7 wall skip.  With no opportunity cost (rw <= 0) there is no wall at which
        # quoting stops being worth it, so the test is vacuous rather than exclusionary.
        if rw > 0 and wall_indifference_size(s.rho, s.W, s.S, s.p, rw) <= 0:    # T3
            continue
        elig.append(s)

    spent = 0.0
    per_market = {}
    unaffordable = set()
    guard = 0
    while True:
        guard += 1
        if guard > 200000:                                   # never spin
            break
        best = None
        best_rate = float("-inf")
        for s in elig:
            if s.key in unaffordable:
                continue
            q = alloc[s.key]
            if q + 1 > n_cap(s.p, caps):                     # §8.1
                continue
            mcap = market_cap_usd(s, budget_usd, caps)        # §8.2
            if per_market.get(s.ticker, 0.0) + s.p > mcap + 1e-9:
                continue
            r = marginal_rate(s.rho, s.S + s.W, q, s.p)
            if r < max(lam_h, hurdle(s.phi, s.d, s.p)):       # §2.2 / §2.3, $/h
                continue
            if r > best_rate + 1e-15 or (
                    abs(r - best_rate) <= 1e-15 and best is not None
                    and (s.ticker, s.side) < (best.ticker, best.side)):
                best, best_rate = s, r
        if best is None:
            break
        step = max(1, int(round(STEP_FRACTION * budget_usd / best.p)))   # §2.5
        step = min(step, n_cap(best.p, caps) - alloc[best.key])
        room = market_cap_usd(best, budget_usd, caps) - per_market.get(best.ticker, 0.0)
        step = min(step, int(room / best.p + 1e-9))
        if spent + step * best.p > budget_usd + 1e-12:
            step = int((budget_usd - spent) / best.p + 1e-9)
        if step < 1:
            unaffordable.add(best.key)
            continue
        alloc[best.key] += step
        spent += step * best.p
        per_market[best.ticker] = per_market.get(best.ticker, 0.0) + step * best.p
    return alloc, spent


def projected_period_payout(program_slots, alloc):
    """§0.2/§3.5 — projected PERIOD payout:  accrued + share * (rho/2) * hours_left.

    C3: this previously multiplied the FULL-period pool by the CURRENT share regardless of
    how much window remained, so a program with two hours left on a 228h window projected
    as if it had all 228 — the forfeit gate waved through entries whose reachable payout was
    $0.22 as $25 projections.  A gate that mis-grades in the permissive direction is worse
    than no gate, because it launders a bad entry as a checked one.
    Only the UN-ACCRUED portion scales; `accrued` is already banked (§3.6) and is a property
    of the program, carried identically on each of its slots.
    """
    accrued = max([s.accrued for s in program_slots] or [0.0])
    total = 0.0
    for s in program_slots:
        q = alloc.get(s.key, 0)
        if q <= 0:
            continue
        total += our_share(q, s.S + s.W) * (s.rho / 2.0) * max(0.0, s.hours_left)
    return accrued + total


def top_up_to_floor(program_slots, alloc, spent, budget_usd, caps=None,
                    floor_usd=ENTRY_FLOOR_USD, lambda_min=LAMBDA_MIN):
    """ALLOCATE line 14 — smallest affordable top-up that clears the floor AND still beats
    the hurdle.  Returns (new_alloc_delta {key: extra_qty}, extra_cost) or (None, 0.0)."""
    caps = caps or Caps()
    lam_h = float(lambda_min) / LAMBDA_MIN_WINDOW_HOURS
    funded = [s for s in program_slots if alloc.get(s.key, 0) > 0]
    if not funded:
        return None, 0.0
    # top up the slot with the highest marginal rate at its current size
    funded.sort(key=lambda s: -marginal_rate(s.rho, s.S + s.W, alloc[s.key], s.p))
    s = funded[0]
    q = alloc[s.key]
    cap = n_cap(s.p, caps)
    while q < cap:
        q += 1
        trial = dict(alloc)
        trial[s.key] = q
        cost = (q - alloc[s.key]) * s.p
        if spent + cost > budget_usd + 1e-12:
            return None, 0.0
        if marginal_rate(s.rho, s.S + s.W, q, s.p) < max(lam_h, hurdle(s.phi, s.d, s.p)):
            return None, 0.0
        if projected_period_payout(program_slots, trial) >= floor_usd:
            return {s.key: q - alloc[s.key]}, cost
    return None, 0.0


def allocate_with_forfeit_gate(slots, budget_usd, caps=None, lambda_min=LAMBDA_MIN,
                               floor_usd=ENTRY_FLOOR_USD, r_star_wall=None):
    """ALLOCATE lines 12-15 (§2.4) — the forfeit gate is per PROGRAM-PERIOD (§0.5), applied
    AFTER water-filling, and a dropped program's dollars are re-water-filled.
    Returns (alloc, spent, dropped_program_ids)."""
    caps = caps or Caps()
    dropped = set()
    alloc, spent = {}, 0.0
    for _ in range(MAX_GATE_PASSES):
        live = [s for s in slots if s.program_id not in dropped]
        alloc, spent = allocate(live, budget_usd, caps, lambda_min, r_star_wall)
        by_prog = {}
        for s in live:
            by_prog.setdefault(s.program_id, []).append(s)
        newly = []
        for pid, ps in sorted(by_prog.items(), key=lambda kv: str(kv[0])):
            proj = projected_period_payout(ps, alloc)
            if proj <= 0.0:
                continue                                    # not funded: nothing to gate
            if proj >= floor_usd:
                continue
            delta, cost = top_up_to_floor(ps, alloc, spent, budget_usd, caps, floor_usd,
                                          lambda_min)
            if delta:
                for k, extra in delta.items():
                    alloc[k] += extra
                spent += cost
                continue
            newly.append(pid)
        if not newly:
            break
        dropped |= set(newly)
    for s in slots:
        alloc.setdefault(s.key, 0)
        if s.program_id in dropped:
            alloc[s.key] = 0
    return alloc, spent, dropped


def t0_qualification_size(side_score, target_size):
    """§6.2 — the T0 action that matters is the QUALIFICATION GATE, not size.  With an empty
    book neither side reaches target_size, every snapshot is EXCLUDED and nobody earns.
    Post `target_size - cum_size` at the cheapest legal price on the short side.

    Note the interaction with ALLOCATE, which is correct and deliberate: at S ~ 0 the
    marginal rate is 0 (share is already ~1 for any q), so ALLOCATE assigns 0 here.  The
    land-grab is a SEPARATE path, and §6.1 says use the MINIMUM q clearing the floor and
    caps — do not size up into an empty book (charter divergence D2)."""
    have = side_score.cum_size if side_score else 0.0
    return int(max(0, math.ceil(float(target_size) - float(have))))


# =============================================================================================
# PURE — forfeit floor, rescue, three-way abandon (§3).  T8-T13b.
# =============================================================================================
KEEP, TOP_UP, HOLD, ABANDON = "KEEP", "TOP_UP", "HOLD", "ABANDON"


def forfeit_gate(projected_usd, floor_usd=ENTRY_FLOOR_USD):
    """§3.1 — enter iff the PERIOD projection clears the floor.  Boundary inclusive (T8)."""
    return float(projected_usd) >= float(floor_usd) - 1e-12


def payable(earned_usd, cliff_usd=1.00):
    """§0.2 — paid iff >= $1.00, rounded down to the cent.  T9."""
    if float(earned_usd) < cliff_usd - 1e-12:
        return 0.0
    return math.floor(float(earned_usd) * 100.0 + 1e-9) / 100.0


def checkpoint_times(start_ts, end_ts, fractions=CHECKPOINT_FRACTIONS):
    """§3.4 — WINDOW FRACTIONS, never clock offsets.  16h -> +4/8/12.8/15.04h;
    228h -> +57/114/182.4/214.32h.  T13b."""
    span = float(end_ts) - float(start_ts)
    return [float(start_ts) + f * span for f in fractions]


class RescueResult(object):
    __slots__ = ("action", "delta_q", "proj", "abandon_value", "hold_value", "note")

    def __init__(self, action, delta_q=0, proj=0.0, abandon_value=0.0, hold_value=0.0,
                 note=""):
        self.action = action
        self.delta_q = delta_q
        self.proj = proj
        self.abandon_value = abandon_value
        self.hold_value = hold_value
        self.note = note

    def __repr__(self):
        return "RescueResult(%s dq=%s proj=%.4f av=%.4f hv=%.4f)" % (
            self.action, self.delta_q, self.proj, self.abandon_value, self.hold_value)


def rescue(A, rate_now, h, rho, S, q, p, r_star, C, phi=None, d=None,
           p_recover=0.0, has_other_program=True, target_usd=RESCUE_TARGET_USD,
           max_delta_q=None):
    """§3.5/§3.6/§3.7 — the checkpoint decision.  All quantities are PER PROGRAM-PERIOD.

      A        accrued projected payout ($)          h    hours left IN THE PERIOD
      rate_now current $/h of payout accrual         C    current collateral ($) on the slot
      r_star   achieved water level ($/h per collateral-$)

    The rescue term counts A at FULL value because abandoning yields 0 here — accrued score
    is not sunk, it is CONDITIONAL on clearing $1.00 (§3.6).  Abandon is a THREE-WAY (§3.7):
    with one live program abandon_value is 0 IDENTICALLY, so HOLD wins unless the residual
    fill risk phi*q*d*h exceeds the option value.
    """
    phi = seed_phi(p) if phi is None else float(phi)
    d = seed_drift(p) if d is None else float(d)
    proj = float(A) + float(rate_now) * float(h)
    if proj >= float(target_usd) - 1e-12:
        return RescueResult(KEEP, 0, proj, note="projection_clears_target")

    # -- TOP_UP: exists a Delta q reaching the target that beats redeploy + fill cost -----
    cap = n_cap(p) if max_delta_q is None else int(q + max_delta_q)
    best_dq = None
    qq = int(q)
    while qq < cap:
        qq += 1
        r_new = reward_rate(rho, qq, S)
        if float(A) + r_new * float(h) < float(target_usd) - 1e-12:
            continue
        dq = qq - int(q)
        redeploy = (float(C) + dq * float(p)) * float(r_star) * float(h)
        fillcost = phi * d * float(qq) * float(h)
        if float(A) + r_new * float(h) > redeploy + fillcost:
            best_dq = dq
            proj = float(A) + r_new * float(h)
        break
    if best_dq:
        return RescueResult(TOP_UP, best_dq, proj, note="top_up_clears_target")

    # -- three-way (§3.7).  P(recover) is 0 BY CONSTRUCTION when even the rho/2 ceiling
    #    cannot reach the target in the remaining window.
    max_attainable = float(A) + (float(rho) / 2.0) * float(h)
    p_rec = 0.0 if max_attainable < float(target_usd) else float(p_recover)
    abandon_value = (float(r_star) * float(C) * float(h)) if has_other_program else 0.0
    hold_value = p_rec * float(target_usd) - phi * float(q) * d * float(h)
    if abandon_value > hold_value:
        return RescueResult(ABANDON, 0, proj, abandon_value, hold_value,
                            note="abandon_value_exceeds_hold_value")
    return RescueResult(HOLD, 0, proj, abandon_value, hold_value,
                        note="no_redeploy_benefit" if not has_other_program else "hold")


# =============================================================================================
# PURE — inventory recycling (§5).  T14-T18, T32b.
# =============================================================================================
NO_ACTION, MAKER_SHED, TAKER_EXIT = "NoAction", "MakerShed", "TakerExit"
RECYCLE_HOLD = "Hold"


def taker_fee_usd(n, p):
    """§5.1/T17 — F = 0.07*n*p*(1-p), rounded UP to the cent.  The maker/shed path is
    fee-exempt (universal, permanent, prod-proven) and charges zero."""
    raw_cents = TAKER_FEE_RATE * float(n) * float(p) * (1.0 - float(p)) * 100.0
    return math.ceil(raw_cents - 1e-9) / 100.0


def recycle(n_yes, n_no, p_mid, p_bid, h, r_star, R_blocked, shed_age_s=0.0,
            global_cap_breached=False, assume_filled=False, fee_fn=taker_fee_usd):
    r"""§5.2/§5.4/§5.6 — the recycling decision.

        TAKER-EXIT iff  n*(p_mid - p_bid) + F  <  h*[ r*·n·p_bid + R_blocked ]
                        \___ exit cost ____/       \_ freed capital _/ \_ unblocked slot _/

    R_blocked usually dominates 10-20x: inventory is expensive because it BLOCKS THE SLOT,
    not because of the capital (§5.3).  Maker shed is strictly preferred and is not a
    separate action (§5.4, charter divergence D4): a YES ask IS a NO bid, so the shed quote
    still scores and consumes zero incremental collateral.  Escalate to a taker exit only
    when the inequality holds AND the shed quote has been unfilled 30 min AND (h<2 or a
    global cap is breached).
    """
    if assume_filled:
        # §5.6/§9.4b (S1): acting on unverified inventory converts a bookkeeping ambiguity
        # into a real naked short.  The freeze covers RECYCLING as well as quoting.
        return NO_ACTION, {"why": "assume_filled_freeze"}
    net = float(n_yes) - float(n_no)
    if abs(net) < 1e-9:
        # §5.5 two-sided fills are a locked box (pay ~99c, receive exactly $1.00).  Cap NET.
        return RECYCLE_HOLD, {"why": "net_flat_locked_box", "net": 0.0}
    n = abs(net)
    lhs = n * (float(p_mid) - float(p_bid)) + fee_fn(n, p_mid)
    rhs = float(h) * (float(r_star) * n * float(p_bid) + float(R_blocked))
    info = {"lhs": lhs, "rhs": rhs, "net": net}
    if lhs >= rhs:
        return RECYCLE_HOLD, dict(info, why="exit_destroys_value")
    if shed_age_s >= SHED_PATIENCE_S and (float(h) < SHED_ESCALATE_HOURS_LEFT
                                          or global_cap_breached):
        return TAKER_EXIT, dict(info, why="shed_stale_and_deadline")
    return MAKER_SHED, dict(info, why="shed_preferred")


def shed_slot(side_held):
    """§5.4/D4 — the shed of a YES position is an ASK order, which IS a NO bid at 100-a: it
    still SCORES, needs no new collateral (the position covers it), and unwinds the
    inventory.  So the shed is not a separate action — it is the OPPOSING slot's quote,
    floored at the size of the position being unwound."""
    return "ask" if side_held == "bid" else "bid"


# =============================================================================================
# PURE — requote triggers, at-best, coverage (§4).  T19-T22.
# =============================================================================================
TRIG_OFF_BEST, TRIG_REFILL, TRIG_S_MOVED, TRIG_QUALIFIES, TRIG_RESYNC = (
    "a_off_best", "b_refill", "c_S_moved", "d_qualifies_flipped", "e_safety_resync")


def at_best(our_price_c, best_price_c):
    """§4.5 — our resting price equals the same-side best."""
    return our_price_c is not None and best_price_c is not None and \
        int(our_price_c) == int(best_price_c)


def requote_triggers(our_price_c, best_price_c, remaining, target_q, S_now, S_ref,
                     qualifies_now, qualifies_ref, resting_age_s, since_resync_s):
    """§4.3 — evaluated at 1 Hz off the book, not a timer.  §4.4: minimum resting life 30s
    applies to VOLUNTARY requotes while still at best; trigger (a) overrides it because a
    genuine price improvement is not a dodge."""
    trig = []
    if not at_best(our_price_c, best_price_c):
        trig.append(TRIG_OFF_BEST)                                   # (a) overrides §4.4
    if target_q and float(remaining) < REFILL_TRIGGER_FRAC * float(target_q) - 1e-12:
        trig.append(TRIG_REFILL)                                     # (b)
    if S_ref and abs(float(S_now) - float(S_ref)) > S_MOVE_TRIGGER_FRAC * float(S_ref):
        trig.append(TRIG_S_MOVED)                                    # (c)
    if bool(qualifies_now) != bool(qualifies_ref):
        trig.append(TRIG_QUALIFIES)                                  # (d)
    if float(since_resync_s) >= SAFETY_RESYNC_S:
        trig.append(TRIG_RESYNC)                                     # (e)
    # §4.4: suppress everything except (a) and (d) inside the minimum resting life.
    if float(resting_age_s) < MIN_RESTING_LIFE_S:
        trig = [t for t in trig if t in (TRIG_OFF_BEST, TRIG_QUALIFIES)]
    return trig


def coverage(at_best_seconds, window_seconds):
    """§4.5 — coverage = at_best_seconds / window_seconds.  The best leading indicator."""
    if window_seconds <= 0:
        return 0.0
    return float(at_best_seconds) / float(window_seconds)


def coverage_from_cycle(window_seconds, cycle_seconds, gap_seconds):
    """T21 — a tape with `gap_seconds` lost per `cycle_seconds`.  Cancel-first at 60s with a
    1.2s gap => 98.0%; make-before-break (gap 0) => 100.0%.  The metered 2% must match v3's
    MEASURED 2% loss — the model validating itself."""
    if cycle_seconds <= 0:
        return 1.0
    cycles = float(window_seconds) / float(cycle_seconds)
    lost = cycles * float(gap_seconds)
    return coverage(max(0.0, float(window_seconds) - lost), window_seconds)


def cancel_first_optimum_s(g=1.2, a=1.0 / 900.0):
    """§4.2 — T* = sqrt(2g/a).  g = seconds lost per requote (round-trip + 1, because a
    partially-rested second does not count); a = 1/2 * lambda_eff, lambda_eff ~ 1/2 the
    measured best-change rate (20%/45s => 1/450/s => a = 1/900/s).  T* = 46s."""
    return math.sqrt(2.0 * g / a)


def cancel_first_efficiency(T, g=1.2, a=1.0 / 900.0):
    """§4.2 — E(T) = [1 - g/T] * (1 - e^{-aT})/(aT).  Flat: 94.9% at 46s, 94.8% at 60s."""
    if T <= 0:
        return 0.0
    return (1.0 - g / T) * (1.0 - math.exp(-a * T)) / (a * T)


# =============================================================================================
# PURE — risk caps and anti-gaming telemetry (§8, §10)
# =============================================================================================
def day_stop_usd(projected_day_reward_usd):
    """§8.4 — -max($20, 0.35 * projected_day_reward), capped at -$150.  Returned POSITIVE
    as a loss magnitude."""
    return min(DAY_STOP_CAP_USD,
               max(DAY_STOP_FLOOR_USD, DAY_STOP_FRAC * float(projected_day_reward_usd)))


def refill_cap(p, caps=None):
    """§8.7 — post-size and refill-cap are DECOUPLED knobs (the v3 lesson).  4 turnovers."""
    return REFILL_CAP_TURNOVERS * n_cap(p, caps)


def two_sided_metrics(resting):
    """§10.3-P3 — measured at the PORTFOLIO level, not the slot level (S4).  `resting` is
    [{"ticker","side","collateral","excluded"}]; excluded = pinned or being shed.
    Returns (two_sided_collateral_pct, two_sided_market_pct)."""
    by_mkt = {}
    for r in resting:
        if r.get("excluded"):
            continue
        m = by_mkt.setdefault(r["ticker"], {"bid": 0.0, "ask": 0.0})
        m[r["side"]] += float(r.get("collateral", 0.0))
    total = sum(v["bid"] + v["ask"] for v in by_mkt.values())
    two_c = sum(v["bid"] + v["ask"] for v in by_mkt.values() if v["bid"] > 0 and v["ask"] > 0)
    n_two = sum(1 for v in by_mkt.values() if v["bid"] > 0 and v["ask"] > 0)
    return ((two_c / total if total > 0 else 0.0),
            (float(n_two) / len(by_mkt) if by_mkt else 0.0))


def fill_honor_ratio(fills_taken, cancels_within_2s_of_touch):
    """§10.3-P4 — published daily; target >= 0.95, investigate below 0.90 for a day."""
    denom = float(fills_taken) + float(cancels_within_2s_of_touch)
    return (float(fills_taken) / denom) if denom > 0 else 1.0


def cheap_side_score_pct(scores_by_slot, cheap_cut=PHI_CHEAP_PRICE_CUT):
    """§10.3-P5 — MONITORED TELEMETRY with a human-review alert at >95% for 3 consecutive
    days.  An alert, never a block.  P5 (the cheap-side cap) is DELETED with its price
    stated: forcing 40% of score onto sides costing ~34x more per score point costs
    $50-150/day, i.e. $1,700-5,100 over the program's remaining life (§10.3/D5/§15.3)."""
    tot = sum(v for _, v in scores_by_slot) or 0.0
    cheap = sum(v for p, v in scores_by_slot if p < cheap_cut)
    return (cheap / tot) if tot > 0 else 0.0


def p7_revival_allowed(revival_candidates, max_markets=P7_MAX_REVIVAL_MARKETS):
    """§10.3-P7 — at most 3 CONCURRENT revival markets.  `revival_candidates` is
    [(ticker, rank_value)]; the highest-rank `max_markets` are allowed and the rest are
    denied.  Deterministic: ties break on ticker."""
    ranked = sorted(revival_candidates, key=lambda x: (-float(x[1]), str(x[0])))
    return {t for t, _ in ranked[:max_markets]}


def p7_side_share_breach(daily_side_shares, max_share=P7_MAX_SIDE_SHARE,
                         max_days=P7_MAX_SIDE_SHARE_DAYS):
    """§10.3-P7 — never >90% of a qualifying side for more than 5 consecutive days on the
    same market (the state where appearance risk peaks)."""
    run = 0
    for s in daily_side_shares:
        run = run + 1 if float(s) > max_share else 0
        if run > max_days:
            return True
    return False


def p6_pre_entry_ok(side_traded_contracts_trailing, lookback_days=P6_LOOKBACK_DAYS):
    """§10.3-P6 pre-entry filter (ALLOCATE line 2) — exclude a candidate whose market has
    traded ZERO contracts on that side over the trailing 5 days of public tape.  This stops
    us entering decorative books rather than paying 5 days to learn it."""
    return float(side_traded_contracts_trailing) > 0.0


def p6_prune(own_fills_by_program_day, lookback_days=P6_LOOKBACK_DAYS):
    """§10.3-P6 pruner — zero taker fills over 5 consecutive program-days => drop."""
    tail = list(own_fills_by_program_day)[-lookback_days:]
    return len(tail) >= lookback_days and sum(tail) == 0


def standdown_ratio_breach(ratios):
    """§12.3(a) — |log2(paid/model)| > 1 on the settled aggregate for 2 consecutive days."""
    tail = [r for r in ratios][-STANDDOWN_DAYS:]
    if len(tail) < STANDDOWN_DAYS:
        return False
    for r in tail:
        if r is None or r <= 0:
            return False
        if abs(math.log2(r)) <= STANDDOWN_LOG2_RATIO:
            return False
    return True


def standdown_nodata_breach(reconcilable_rows_by_day):
    """§12.3(b) — 2 consecutive days with ZERO reconcilable rows.  A silent reconciliation
    loop is worse than a bad one: it looks identical to a good day while capital scales."""
    tail = list(reconcilable_rows_by_day)[-STANDDOWN_DAYS:]
    return len(tail) >= STANDDOWN_DAYS and all(int(x) == 0 for x in tail)


def disclosure_state(deployed_usd, our_daily_usd,
                     exchange_runrate=EXCHANGE_RUNRATE_USD_DAY):
    """§10.4 — do not disclose below $2k deployed; disclose before any deployment above $10k,
    or immediately if we ever exceed 5% of the daily exchange-wide run-rate."""
    share = (float(our_daily_usd) / exchange_runrate) if exchange_runrate > 0 else 0.0
    if share > DISCLOSE_RUNRATE_SHARE:
        return "DISCLOSE_NOW", share
    if float(deployed_usd) > DISCLOSE_ABOVE_DEPLOYED_USD:
        return "DISCLOSE_BEFORE_DEPLOY", share
    if float(deployed_usd) < NO_DISCLOSE_BELOW_DEPLOYED_USD:
        return "NO_DISCLOSE", share
    return "MONITOR", share


# =============================================================================================
# PURE — ledger, replay, restart (§9).  T29-T35.  This is the part v3 got wrong.
# =============================================================================================
ST_LIVE, ST_CLOSED, ST_UNKNOWN = "live", "closed", "unknown"


class OrderState(object):
    __slots__ = ("order_id", "coid", "ticker", "side", "price", "size", "fill_count",
                 "remaining_count", "reduced_by", "state", "extra_fills", "http")

    def __init__(self, order_id, coid, ticker, side, price, size,
                 fill_count=0.0, remaining_count=None):
        self.order_id = str(order_id)
        self.coid = coid
        self.ticker = ticker
        self.side = side
        self.price = float(price)
        self.size = float(size)
        self.fill_count = float(fill_count)
        self.remaining_count = float(size - fill_count if remaining_count is None
                                     else remaining_count)
        self.reduced_by = None
        self.state = ST_CLOSED if self.remaining_count <= 0 else ST_LIVE
        self.extra_fills = 0.0        # from the fills API (§9.4a / §9.4 step 4)
        self.http = None

    @property
    def filled(self):
        """§9.2 filled invariant, per order:
             filled = fill_count + (remaining_count - reduced_by)
        plus any fills the fills API attributed to this order (404 disambiguation)."""
        if self.reduced_by is None:
            return self.fill_count + self.extra_fills
        return self.fill_count + max(0.0, self.remaining_count - self.reduced_by) \
            + self.extra_fills

    @property
    def resting(self):
        if self.state in (ST_LIVE, ST_UNKNOWN) and self.reduced_by is None:
            return max(0.0, self.remaining_count)
        return 0.0


class LedgerState(object):
    """Everything reconstructable from the append-only JSONL, and NOTHING else.  The
    resting-orders and positions INDEXES are never read (§8.6)."""

    def __init__(self):
        self.orders = {}
        self.filled_cum = {}          # (ticker, side) -> contracts  (§9.2)
        self.positions = {}           # ticker -> {"yes": n, "no": n}
        self.position_cost = {}       # ticker -> $ paid for the held contracts
        self.position_cost_leg = {}   # ticker -> {"yes": $, "no": $}  (C9 entry basis)
        self.realized_pnl = 0.0       # C2: released at settlement, feeds the §8.4 stop
        self.assume_filled = set()    # §9.4b freeze, survives restart
        self.phantom_risk = set()     # B2: markets where a POST timed out mid-flight
        self.poisoned = set()         # §8.5
        self.unknown_orders = []      # §9.4 step 2
        self.consec_cancel_anomalies = 0
        self.post_error_ts = []
        self.last_ts = 0.0
        self.coid_seq = 0
        self.unresolved_404 = []      # order_ids needing §9.4a disambiguation
        self.seen_fill_ids = set()    # S2: idempotency keys for fills-API rows
        self.unkeyed_fill_rows = 0    # S2: pre-fix crash-gap rows deduped by content
        self.accrued = {}             # S3: program_id -> $ of MODEL payout accrued
        self.checkpoints_done = {}    # S3: program_id -> set(checkpoint fraction)

    # -- views ---------------------------------------------------------------------------
    def filled(self, ticker, side):
        return self.filled_cum.get((ticker, side), 0.0)

    def entry_basis(self, ticker, leg):
        """C9 — average dollars PAID per contract on a leg.  The inventory cap must bind
        against what we actually paid, not against the current price: a contract-count cap
        of floor($10/p) re-permits at p=$0.20 against inventory bought at $0.34, which is
        how a $10 cap silently becomes a $17 one."""
        qty = (self.positions.get(ticker) or {}).get(leg, 0.0)
        if qty <= 0:
            return 0.0
        return (self.position_cost_leg.get(ticker, {}).get(leg, 0.0)) / qty

    def net_exposure_usd(self, ticker, side, prospective_size=0.0,
                         prospective_price=None):
        """C1 — the per-market net-inventory DOLLAR exposure if `prospective_size` on `side`
        filled in full, INCLUDING what orders already resting on that side could still add.

        The defect this closes (the DXY root cause): place()'s old check bounded `net+size`
        but was blind to orders ALREADY RESTING on the same side.  Make-before-break puts
        two orders on one side by construction, so both passed the check independently and
        both filled — 2x the cap, reproduced at 58 modelled against 59 observed.

            |net_after| * entry_basis  +  sum(resting OPENING size, same side) * unit_collat
                                                                            <= INV_CAP_USD
        """
        net = self.net_position(ticker)
        delta = float(prospective_size) if side == "bid" else -float(prospective_size)
        net_after = net + delta
        leg = "yes" if net_after > 0 else "no"
        basis = self.entry_basis(ticker, leg)
        if basis <= 0:
            # no inventory on that leg yet: the exposure is being created at this order's
            # own price, which is the only basis that exists.
            basis = float(prospective_price) if prospective_price is not None else 0.0
        held = abs(net_after) * basis
        resting = [o for o in self.orders.values() if o.ticker == ticker and o.resting > 0]
        _, _, closing = allocate_closing_room(resting, net)
        opening = 0.0
        for o in resting:
            if o.side != side:
                continue
            open_qty = max(0.0, o.resting - closing.get(str(o.order_id), 0.0))
            opening += open_qty * unit_collateral(o.side, o.price)
        return held + opening

    def net_position(self, ticker):
        pos = self.positions.get(ticker, {})
        return pos.get("yes", 0.0) - pos.get("no", 0.0)

    @property
    def resting_collateral(self):
        """FIX-A — resting orders are netted against held inventory exactly as placement is.

        If placement nets a closing order to $0 but this view then charges it in full once
        it rests, the ceiling re-seals on the next cycle and the deadlock returns one tick
        later.  The two must agree.  Closing capacity is allocated per ticker, deterministic
        in order_id, so the split is stable across replays.
        """
        total = 0.0
        for ticker, orders in self._resting_by_ticker().items():
            total += allocate_closing_room(orders, self.net_position(ticker))[0]
        return total

    def _resting_by_ticker(self):
        out = {}
        for o in self.orders.values():
            if o.resting > 0:
                out.setdefault(o.ticker, []).append(o)
        return out

    def closing_room(self, ticker, side):
        """FIX-A-1 — closing capacity on (ticker, side) NOT already consumed by resting
        orders.  This is what a new order may net against; anything beyond it opens."""
        orders = self._resting_by_ticker().get(ticker, [])
        return allocate_closing_room(orders, self.net_position(ticker))[1].get(side, 0.0)

    @property
    def position_collateral(self):
        return sum(self.position_cost.values())

    @property
    def collateral(self):
        """§9.3 — sum_live(remaining*price) + sum_positions(n*entry_p).  Both from ledger
        REPLAY, never from an exchange index.  v3 reset this to zero on restart."""
        return self.resting_collateral + self.position_collateral

    def live_orders(self):
        return [o for o in self.orders.values() if o.state in (ST_LIVE, ST_UNKNOWN)]

    def budget_tripped(self, now=None):
        if self.consec_cancel_anomalies >= MAX_CONSEC_CANCEL_ANOMALIES:
            return "consecutive_cancel_anomalies=%d" % self.consec_cancel_anomalies
        now = _now() if now is None else now
        recent = [t for t in self.post_error_ts if now - t <= POST_ERROR_WINDOW_S]
        if len(recent) >= MAX_POST_ERRORS:
            return "post_errors=%d_in_%ds" % (len(recent), POST_ERROR_WINDOW_S)
        return None

    # -- mutation (shared by the live path and by replay) --------------------------------
    def _credit_fill(self, order, n):
        self.apply_fill(order.ticker, order.side, n, order.price, 1.0)

    def apply_fill(self, ticker, side, n, price_dollars, sign=1.0):
        """B3 — book `n` contracts on `side` ("bid" = YES leg, "ask" = NO leg) at
        `price_dollars` on the YES axis.  `sign` is -1 for a DISPOSAL (an `action=sell`
        fill, e.g. an operator flattening a position by hand while this process is down),
        which must DECREASE the position rather than add to it."""
        n = float(n)
        if n <= 0:
            return
        leg = "yes" if side == "bid" else "no"
        # filled_cum is a GROSS cumulative fill counter (v3 F7) and only ever increases
        k = (ticker, side)
        self.filled_cum[k] = self.filled_cum.get(k, 0.0) + n
        pos = self.positions.setdefault(ticker, {"yes": 0.0, "no": 0.0})
        unit = unit_collateral(side, price_dollars)
        if sign < 0:
            # a disposal can only release what is actually held
            n = min(n, max(0.0, pos[leg]))
            if n <= 0:
                return
        pos[leg] += sign * n
        spent = sign * n * unit
        self.position_cost[ticker] = max(0.0, self.position_cost.get(ticker, 0.0) + spent)
        legs = self.position_cost_leg.setdefault(ticker, {"yes": 0.0, "no": 0.0})
        legs[leg] = max(0.0, legs[leg] + spent)


def ledger_replay(records):
    """§9.4 step 1 — replay the append-only ledger into per-order state, filled_cum,
    positions and COLLATERAL.  v3 lost filled_cum AND collateral on restart; this is the fix
    and T34 is the test v3 would have failed."""
    st = LedgerState()
    pending = {}
    for rec in records:
        t = rec.get("t_", rec.get("event", rec.get("t")))
        ts = float(rec.get("ts_", rec.get("t", 0.0)) or 0.0)
        st.last_ts = max(st.last_ts, ts)
        kind = rec.get("k") or rec.get("kind") or t
        if kind == "place_req":
            pending[rec.get("coid")] = rec
            st.coid_seq = max(st.coid_seq, int(rec.get("seq", 0) or 0))
        elif kind == "place_resp":
            if rec.get("err"):
                st.post_error_ts.append(ts)
                if rec.get("poison"):
                    st.poisoned.add(rec.get("ticker"))
                continue
            oid = rec.get("order_id")
            if oid and str(oid) in st.orders:
                # N2: a duplicated place_resp line (a retried write, a copied ledger, an
                # fsync that landed twice) must not create a second order or double-credit
                # its immediate fill_count.  order_id is the exchange's own identity.
                st.coid_seq = max(st.coid_seq, int(rec.get("seq", 0) or 0))
                continue
            if not oid:
                st.post_error_ts.append(ts)
                st.poisoned.add(rec.get("ticker"))     # cannot cancel what we cannot name
                continue
            o = OrderState(oid, rec.get("coid"), rec.get("ticker"), rec.get("side"),
                           num(rec.get("price")), num(rec.get("size")),
                           num(rec.get("fill_count"), 0.0),
                           None if rec.get("remaining_count") is None
                           else num(rec.get("remaining_count")))
            st.orders[o.order_id] = o
            if o.fill_count > 0:
                st._credit_fill(o, o.fill_count)
            st.coid_seq = max(st.coid_seq, int(rec.get("seq", 0) or 0))
        elif kind == "cancel_req":
            pass
        elif kind == "cancel_resp":
            oid = str(rec.get("order_id"))
            o = st.orders.get(oid)
            http = int(rec.get("http", 0) or 0)
            if o is None:
                st.consec_cancel_anomalies += 1
                continue
            o.http = http
            if http == 200:
                rb = rec.get("reduced_by")
                if rb is None:
                    # a 200 we cannot read is not a cancel we can trust (v3-proven)
                    o.state = ST_UNKNOWN
                    st.poisoned.add(o.ticker)
                    st.consec_cancel_anomalies += 1
                    continue
                o.reduced_by = max(0.0, min(o.remaining_count, num(rb, 0.0)))
                learned = max(0.0, o.remaining_count - o.reduced_by)
                o.state = ST_CLOSED
                st.consec_cancel_anomalies = 0
                if learned:
                    st._credit_fill(o, learned)
            elif http == 404:
                # §9.4(3) 404 is AMBIGUOUS (fully filled OR expired) -> §9.4a.  NEVER book
                # zero silently, and never book the remainder without a fills read.
                o.state = ST_UNKNOWN
                st.consec_cancel_anomalies = 0
                st.unresolved_404.append(oid)
            else:
                # 410 (the dead legacy path v1 logged as SUCCESS), 5xx, transport: the order
                # MAY BE LIVE.  Poison the market, keep the order in the live set.  T33.
                o.state = ST_UNKNOWN
                st.poisoned.add(o.ticker)
                st.consec_cancel_anomalies += 1
        elif kind == "fill_obs":
            # S2: the fills API is queried over OVERLAPPING windows by construction — §9.4
            # step 4 re-reads [last_ledger_ts - 60s, now] on every restart, so a crash LOOP
            # re-observes the same fills.  Without an idempotency key replay double-books
            # them (measured: filled_cum 20 on a truth of 10, collateral $8 on a truth of
            # $4).  The key is the fills API's own immutable fill/trade id.
            fid = rec.get("fill_id")
            if fid is None and rec.get("why") == "crash_gap":
                # A crash-gap row written by a PRE-FIX binary carries no key.  Its identity
                # is nonetheless well defined for this class: crash-gap rows exist only
                # because §9.4 step 4 re-reads an OVERLAPPING window, so a duplicate row is
                # by construction the same API fill re-observed, and its content is the only
                # identity available.  Scoped to this class alone — order-scoped rows always
                # carry a real key from the runner, so no genuine second fill can be
                # collapsed by this path.
                fid = "unkeyed-%s|%s|%s|%s" % (rec.get("ticker"), rec.get("side"),
                                               num(rec.get("count"), 0.0),
                                               rec.get("price_c"))
                st.unkeyed_fill_rows += 1
            if fid is not None:
                if str(fid) in st.seen_fill_ids:
                    continue
                st.seen_fill_ids.add(str(fid))
            oid = rec.get("order_id")
            n = num(rec.get("count"), 0.0)
            o = st.orders.get(str(oid)) if oid else None
            if o is not None:
                o.extra_fills += n
                o.reduced_by = 0.0 if o.reduced_by is None else o.reduced_by
                o.state = ST_CLOSED
                st._credit_fill(o, n)
                if str(oid) in st.unresolved_404:
                    st.unresolved_404.remove(str(oid))
            else:
                # §9.4 step 4 (S3): a crash-gap fill belonging to no specific UNKNOWN order.
                # B3: side/action are NORMALISED at write time, so replay speaks one
                # vocabulary.  `sign` is honoured, so a sell DECREASES the position.
                leg, sign = normalize_fill(rec.get("side"), rec.get("action", "buy"))
                st.apply_fill(rec.get("ticker"), leg, n,
                              num(rec.get("price_c"), 0.0) / 100.0,
                              num(rec.get("sign"), sign))
        elif kind == "expired":
            oid = str(rec.get("order_id"))
            o = st.orders.get(oid)
            if o is not None:
                o.reduced_by = o.remaining_count       # nothing filled
                o.state = ST_CLOSED
                if oid in st.unresolved_404:
                    st.unresolved_404.remove(oid)
        elif kind == "assume_filled":
            oid = rec.get("order_id")
            o = st.orders.get(str(oid)) if oid else None
            st.assume_filled.add(rec.get("ticker"))
            st.poisoned.add(rec.get("ticker"))
            if o is not None:
                n = max(0.0, o.remaining_count)
                o.reduced_by = 0.0
                o.state = ST_CLOSED
                st._credit_fill(o, n)
                if str(oid) in st.unresolved_404:
                    st.unresolved_404.remove(str(oid))
        elif kind == "assume_filled_clear":
            # §9.4b — clears ONLY on an explicit operator record.
            st.assume_filled.discard(rec.get("ticker"))
            st.poisoned.discard(rec.get("ticker"))
        elif kind == "phantom_risk":
            # B2 — a POST whose transport failed.  The order may be LIVE with no order_id,
            # so the market stays poisoned and the restart sweep must look for its fills.
            st.phantom_risk.add(rec.get("ticker"))
            st.poisoned.add(rec.get("ticker"))
        elif kind == "poison":
            st.poisoned.add(rec.get("ticker"))
        elif kind == "settlement":
            # C2 — positions and position_cost had NO writer that decremented, so the
            # ledger accumulated forever and replay faithfully rebuilt every ghost: a
            # synthetic 16h tape reconstructed $3,612 of position_cost and the §8.3 ceiling
            # self-sealed on window 2.  This is the release.  Idempotent by construction:
            # it zeroes, so a duplicate row releases a position that is already zero.
            tk = rec.get("ticker")
            pos = st.positions.get(tk)
            if pos and (abs(pos.get("yes", 0.0)) + abs(pos.get("no", 0.0))) > 0:
                st.realized_pnl += num(rec.get("realized_pnl"), 0.0)
            st.positions[tk] = {"yes": 0.0, "no": 0.0}
            st.position_cost[tk] = 0.0
            st.position_cost_leg[tk] = {"yes": 0.0, "no": 0.0}
        elif kind == "accrual":
            # S3: `accrued` and `checkpoints_done` are the ONLY pieces of checkpoint state
            # that cannot be re-derived from orders and fills — A is an integral over the
            # presence we actually had.  Losing it on restart zeroes A, refires every passed
            # checkpoint, and ABANDONS a program that had genuinely accrued past the cliff
            # (reproduced: live A=$0.95 KEEP -> post-restart ABANDON), forfeiting real money
            # to a bookkeeping gap.  This row is AUTHORITATIVE, unlike "snapshot" (§9.1),
            # which stays advisory precisely so an advisory row can never move positions.
            pid = rec.get("program_id")
            if pid is not None:
                st.accrued[pid] = num(rec.get("accrued_usd"), 0.0)
                st.checkpoints_done[pid] = set(
                    float(f) for f in (rec.get("checkpoints_done") or []))
        elif kind == "coid_seq":
            st.coid_seq = max(st.coid_seq, int(rec.get("seq", 0) or 0))
        # "snapshot" is ADVISORY only (§9.1) and contributes nothing to state.
    st.unknown_orders = [o.order_id for o in st.orders.values() if o.state == ST_UNKNOWN]
    return st


SETTLEABLE_STATUSES = ("settled", "finalized", "determined", "closed")


def settlement_release(positions, position_cost, result):
    """C2 — what a settled market returns.  `result` is the exchange's own, from the PUBLIC
    market endpoint: "yes" pays $1.00 per YES contract, "no" pays $1.00 per NO contract, and
    the losing leg pays zero.  Returns (released_yes, released_no, cost_released,
    realized_pnl).

    Doctrine (§8.6): the market result is MARKET TRUTH from a public endpoint, not a
    portfolio index.  It is also the only thing that can authorise a release — see
    `is_settleable`.  A release without an exchange result is impossible by construction
    because `result` has no default and no inference path.
    """
    yes = float((positions or {}).get("yes", 0.0))
    no = float((positions or {}).get("no", 0.0))
    cost = float(position_cost or 0.0)
    if result == "yes":
        proceeds = yes * 1.0
    elif result == "no":
        proceeds = no * 1.0
    else:
        return 0.0, 0.0, 0.0, 0.0            # not a settlement; release nothing
    return yes, no, cost, proceeds - cost


def is_settleable(market_body):
    """C2 — a market may only release a position when the EXCHANGE says it resolved.
    Requires a non-empty result AND a settleable status; either alone is not enough."""
    m = (market_body or {}).get("market") or market_body or {}
    if not isinstance(m, dict):
        return False, None
    result = str(m.get("result") or "").strip().lower()
    status = str(m.get("status") or "").strip().lower()
    if result not in ("yes", "no"):
        return False, None
    if status not in SETTLEABLE_STATUSES:
        return False, None
    return True, result


class FillsRead(object):
    """One read of the fills endpoint, scoped by order_id.  `ok=False` means the query
    ERRORED (not "no fills")."""
    __slots__ = ("ok", "count")

    def __init__(self, ok=True, count=0.0):
        self.ok = bool(ok)
        self.count = float(count or 0.0)


R404_FILLED, R404_EXPIRED, R404_ASSUME_FILLED, R404_NEED_REQUERY = (
    "filled", "expired", "assume_filled", "need_requery")


def disambiguate_404(order, read1, read2=None, now=None):
    """§9.4a (S2) — NEVER BOOK ZERO SILENTLY.

    404 on cancel is ambiguous: fully filled OR expired.  Query the fills endpoint for that
    order_id.
      * fills present                       -> filled = fill_count + fills_since.  Done.
      * NO fills                            -> do NOT conclude "expired" on the first read;
                                               the fills index has its own propagation lag.
                                               Re-query once after 36s (3x the ~12s worst
                                               observed lag, the same conservatism class as
                                               the 410 rule).
      * still no fills on the re-query      -> expired (filled = fill_count).
      * query ERROR, or the two reads DISAGREE -> ASSUME FULLY FILLED and freeze the market.

    Returns (verdict, filled_total, requery_at_ts_or_None).
    """
    now = _now() if now is None else now
    if not read1.ok:
        return R404_ASSUME_FILLED, order.fill_count + order.remaining_count, None
    if read1.count > 0:
        if read2 is not None and read2.ok and read2.count > 0 and \
                abs(read2.count - read1.count) > 1e-9:
            return R404_ASSUME_FILLED, order.fill_count + order.remaining_count, None
        return R404_FILLED, order.fill_count + read1.count, None
    if read2 is None:
        return R404_NEED_REQUERY, None, now + FILLS_REQUERY_DELAY_S
    if not read2.ok:
        return R404_ASSUME_FILLED, order.fill_count + order.remaining_count, None
    if read2.count > 0:
        # The case a SINGLE read would have booked as zero (T31b).
        return R404_FILLED, order.fill_count + read2.count, None
    return R404_EXPIRED, order.fill_count, None


def crash_gap_window(last_ledger_ts, now=None, lookback_s=CRASH_GAP_LOOKBACK_S):
    """§9.4 step 4 (S3) — ONE time-windowed fills query over [last_ledger_ts - 60s, now] to
    capture fills that occurred while the process was dead and belong to no specific
    UNKNOWN order."""
    now = _now() if now is None else now
    return (float(last_ledger_ts) - float(lookback_s), float(now))


# =============================================================================================
# HTTP + AUTH — v3-proven signing.  The SIGNED path EXCLUDES the query string (R166).
# =============================================================================================
class Auth(object):
    def __init__(self, key_id, private_key):
        self.key_id = key_id
        self.sk = private_key

    def headers(self, method, path):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(_now() * 1000))
        bare = path.split("?", 1)[0]                 # R166: query string EXCLUDED
        msg = (ts + method.upper() + PREFIX + bare).encode()
        sig = self.sk.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256().digest_size),
            hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "KALSHI-ACCESS-TIMESTAMP": ts}


def load_auth():
    """(Auth, note).  Never raises: --dry must run on a box with no prod key."""
    key_id = os.environ.get("KALSHI_API_KEY_ID", "").strip()
    pem = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    src = "env"
    if not (key_id and pem):
        for cand in ENV_CANDIDATES:
            if not cand or not os.path.exists(cand):
                continue
            try:
                with open(cand) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        v = v.strip().strip('"').strip("'")
                        if k.strip() == "KALSHI_API_KEY_ID" and not key_id:
                            key_id = v
                        elif k.strip() == "KALSHI_PRIVATE_KEY_PATH" and not pem:
                            pem = v
                src = cand
            except Exception as exc:
                return None, "env read failed (%s): %s" % (cand, exc)
            if key_id and pem:
                break
    if not key_id:
        return None, "no KALSHI_API_KEY_ID found"
    pem = os.path.expanduser(pem) if pem else PEM_DEFAULT
    # nestor's .env stores the key path RELATIVE to the nestor dir ("./secrets/prod.pem"),
    # which would resolve against the systemd WorkingDirectory if taken literally.  Resolve
    # against the .env's own directory (v3 lesson).
    if not os.path.isabs(pem) and src != "env" and os.path.exists(src):
        pem = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(src)), pem))
    if not os.path.exists(pem):
        pem = PEM_DEFAULT
    if not os.path.exists(pem):
        return None, "private key not found at %s" % pem
    try:
        from cryptography.hazmat.primitives import serialization
        with open(pem, "rb") as fh:
            sk = serialization.load_pem_private_key(fh.read(), password=None)
    except Exception as exc:
        return None, "key load failed: %s" % exc
    return Auth(key_id, sk), "loaded from %s (key %s..., pem %s)" % (
        src, key_id[:8], os.path.basename(pem))


_SESSION = None


def _session():
    global _SESSION
    if _SESSION is None:
        if requests is None:
            raise RuntimeError("requests is not available; network calls are impossible")
        _SESSION = requests.Session()
    return _SESSION


def http(method, url, headers=None, body=None):
    """(status, parsed_body).  Never raises; transport failure returns (0, {...})."""
    try:
        resp = _session().request(method, url, headers=headers, json=body,
                                  timeout=HTTP_TIMEOUT)
    except Exception as exc:
        return 0, {"_transport_error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        return resp.status_code, (resp.json() if resp.content else {})
    except ValueError:
        return resp.status_code, {"_text": resp.text[:500]}


def public_get(path, params=None):
    try:
        resp = _session().get(BASE + PREFIX + path, params=params, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        return 0, {"_transport_error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        return resp.status_code, (resp.json() if resp.content else {})
    except ValueError:
        return resp.status_code, {"_text": resp.text[:500]}


def signed(auth, method, path, body=None, params=None):
    url = BASE + PREFIX + path
    hdrs = auth.headers(method, path)
    if params:
        try:
            resp = _session().request(method, url, headers=hdrs, params=params,
                                      json=body, timeout=HTTP_TIMEOUT)
        except Exception as exc:
            return 0, {"_transport_error": "%s: %s" % (type(exc).__name__, exc)}
        try:
            return resp.status_code, (resp.json() if resp.content else {})
        except ValueError:
            return resp.status_code, {"_text": resp.text[:500]}
    return http(method, url, headers=hdrs, body=body)


def ntfy(title, message):
    """§13.6 alerts."""
    if DRY:
        print("[dry] would ntfy: %s - %s" % (title, message))
        return
    try:
        _session().post("https://ntfy.sh/" + NTFY_TOPIC, data=message.encode(),
                        headers={"Title": title, "Priority": "urgent"}, timeout=10)
    except Exception as exc:
        print("NTFY FAIL %s: %s" % (type(exc).__name__, exc))


# =============================================================================================
# SCANNER (§7.1) — pools are MACHINE-READABLE.  Never hardcode $100/rung (this kills Q4).
# =============================================================================================
def parse_iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def scan_programs(now=None, cache=True):
    """Full cursor pull of /incentive_programs (no auth), filtered to LIVE liquidity
    programs with period_reward > 0.  Cached to ~/nestor/data/lip/programs-YYYYMMDD.json."""
    now = _now() if now is None else now
    out = []
    cursor = None
    for _ in range(SCAN_MAX_PAGES):
        params = {"limit": SCAN_PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        st, body = public_get("/incentive_programs", params)
        if st != 200:
            log("scan_error", http=st, body=json.dumps(body, default=str)[:200])
            break
        page = (body or {}).get("incentive_programs") or (body or {}).get("programs") or []
        out.extend(page)
        cursor = (body or {}).get("cursor")
        if not cursor or not page:
            break
    live = []
    for p in out:
        if str(p.get("incentive_type", p.get("type", "liquidity"))).lower() == "volume":
            continue
        end = parse_iso(p.get("end_date"))
        start = parse_iso(p.get("start_date"))
        if end is None or end <= now:
            continue
        if num(p.get("period_reward"), 0.0) <= 0:
            continue
        tk = p.get("market_ticker") or p.get("ticker")
        if not tk:
            continue
        series = str(tk).split("-")[0]
        if series in DENY_SERIES:                                    # §7.4 seed deny
            continue
        if EVENT_ALLOWLIST and not any(str(tk).startswith(e) for e in EVENT_ALLOWLIST):
            continue
        live.append({"program_id": p.get("id") or p.get("program_id"),
                     "market_ticker": tk, "series": series,
                     "period_reward": num(p.get("period_reward"), 0.0),
                     "target_size_fp": num(p.get("target_size_fp"), 1000.0),
                     "discount_factor_bps": num(p.get("discount_factor_bps"), 5000.0),
                     "start_ts": start, "end_ts": end,
                     "paid_out": bool(p.get("paid_out", False))})
    if cache:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            path = datetime.fromtimestamp(now, timezone.utc).strftime(PROGRAMS_CACHE_FMT)
            with open(path, "w") as fh:
                json.dump({"pulled_ts": now, "n_all": len(out), "n_live": len(live),
                           "programs": live}, fh)
        except Exception as exc:
            log("programs_cache_fail", err="%s: %s" % (type(exc).__name__, exc))
    return live


def slots_from_market(prog, book_body, now, denied=False, p6_yes=True, p6_no=True,
                      assume_filled=False, accrued=0.0):
    """§1.2 — build the two slots for one market from the live book."""
    yes_lv, no_lv = book_levels(book_body)
    yb, ya = best_from_book(book_body)
    tgt = prog["target_size_fp"]
    df = prog["discount_factor_bps"] / 10000.0
    y_entry = score_side(yes_lv, tgt, df, S_MODE_ENTRY)
    n_entry = score_side(no_lv, tgt, df, S_MODE_ENTRY)
    y_recon = score_side(yes_lv, tgt, df, S_MODE_RECON)
    n_recon = score_side(no_lv, tgt, df, S_MODE_RECON)
    pinned = is_pinned(yb, ya)
    H = window_hours(prog["start_ts"], prog["end_ts"])
    rho = pool_rate(prog["period_reward"], H)
    pool = pool_usd(prog["period_reward"])
    out = []
    for side, sc, best_c, p6 in (("bid", y_entry, yb, p6_yes), ("ask", n_entry, ya, p6_no)):
        if best_c is None:
            price = (MIN_LEGAL_PRICE_C / 100.0)
            legal = not pinned
        else:
            price = unit_collateral(side, best_c / 100.0)
            legal = MIN_LEGAL_PRICE_C <= best_c <= MAX_LEGAL_PRICE_C and not pinned
        out.append(Slot(prog["market_ticker"], side, rho, sc.S, max(price, 0.01),
                        pinned=pinned, denied=denied, legal_price_exists=legal, p6_ok=p6,
                        program_id=prog["program_id"], window_h=H, pool=pool,
                        assume_filled=assume_filled, target_size=tgt,
                        cum_size=sc.cum_size,
                        hours_left=max(0.0, (prog["end_ts"] - now) / 3600.0),
                        accrued=accrued,
                        hours_to_start=max(0.0, ((prog.get("start_ts") or 0.0) - now)
                                           / 3600.0)))
    return out, {"yes_entry": y_entry, "no_entry": n_entry,
                 "yes_recon": y_recon, "no_recon": n_recon,
                 "yes_bid_c": yb, "yes_ask_c": ya, "pinned": pinned}


# =============================================================================================
# STARTUP ASSERTIONS (§0.3, §9, deploy checklist).  Refuse to run on any failure.
# =============================================================================================
def startup_assertions(auth, auth_note, programs=None):
    """Returns (ok, [(name, ok, detail)]).  Any False => REFUSE TO RUN."""
    results = []

    # 1. .env + PEM present (live only; --dry may run on a box with no prod key)
    ok_auth = bool(auth) or DRY
    results.append(("env_and_pem", ok_auth, auth_note))

    # 2. data dir writable
    ok_dir = True
    detail = DATA_DIR
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        probe = os.path.join(DATA_DIR, ".writetest")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
    except Exception as exc:
        ok_dir = False
        detail = "%s: %s" % (type(exc).__name__, exc)
    results.append(("data_dir_writable", ok_dir, detail))

    # 3. ledger replay clean
    ok_led = True
    detail = "no ledger yet"
    try:
        st = replay_ledger_file(LEDGER_PATH)
        detail = ("orders=%d unknown=%d filled_cum=%d collateral=$%.2f frozen=%s"
                  % (len(st.orders), len(st.unknown_orders), len(st.filled_cum),
                     st.collateral, sorted(st.assume_filled)))
    except Exception as exc:
        ok_led = False
        detail = "REPLAY FAILED %s: %s" % (type(exc).__name__, exc)
    results.append(("ledger_replay_clean", ok_led, detail))

    # 4. THE UNIT ASSERTION (§0.3) — a live gas rung program must read $100.00 +- $0.01
    ok_unit = True
    detail = "skipped"
    try:
        progs = programs if programs is not None else scan_programs(cache=False)
        ok_unit, d = unit_assertion_check(progs)
        if not REFUSE_ON_UNIT_MISMATCH:
            ok_unit = True
        detail = ("%d/%d live programs read $%.2f+-%.2f (need >=%d) samples=%s; "
                  "%s live=%d belt=%s" % (
                      d["n_at_expect"], d["n_programs"], UNIT_ASSERT_EXPECT_USD,
                      UNIT_ASSERT_TOL_USD, d["min_required"], d["samples"],
                      d["series"], d["series_live"],
                      "n/a" if d["series_ok"] is None else d["series_ok"]))
    except Exception as exc:
        ok_unit = False
        detail = "%s: %s" % (type(exc).__name__, exc)
    results.append(("unit_assertion_eq_%.2f" % UNIT_ASSERT_EXPECT_USD, ok_unit, detail))

    # 5. NEW-5 — the ladder gate.  TAKER_EXIT_ENABLED is False because at a $45 ceiling
    #    the §8.1 net cap bounds what a taker exit could recover at single dollars (see the
    #    constant's derivation).  That argument is a FUNCTION OF THE CEILING and stops
    #    holding as it rises: at $300 the blocked-slot value scales with deployed size while
    #    the crossing risk stays a fixed tail.  Nothing structurally couples the two, so a
    #    ceiling bump would silently inherit a decision made for a different rung.  Refuse
    #    to run rather than auto-enable: crossing the spread is a human decision, and this
    #    forces it to be made explicitly AT the rung.
    ok_taker = not (MAX_TOTAL_COLLATERAL_USD >= TAKER_EXIT_REQUIRED_ABOVE_USD
                    and not TAKER_EXIT_ENABLED)
    results.append((
        "taker_exit_decision_matches_ceiling", ok_taker,
        "ceiling=$%.2f taker_exit_enabled=%s (an explicit decision is REQUIRED at or above "
        "$%.2f)" % (MAX_TOTAL_COLLATERAL_USD, TAKER_EXIT_ENABLED,
                    TAKER_EXIT_REQUIRED_ABOVE_USD)))

    return all(r[1] for r in results), results


def replay_ledger_file(path):
    recs = []
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue
    return ledger_replay(recs)


# =============================================================================================
# THE RUNNER
# =============================================================================================
class Maker(object):
    def __init__(self, auth, state, programs):
        self.auth = auth
        self.st = state
        self.programs = {p["program_id"]: p for p in programs}
        self.books = {}                 # ticker -> last book body
        self.scores = {}                # ticker -> score dict
        self.live_by_slot = {}          # (ticker, side) -> OrderState
        self.target_q = {}              # (ticker, side) -> intended qty
        self.at_best_s = {}             # (ticker, side) -> seconds at best
        self.rest_s = {}                # (ticker, side) -> seconds resting
        self.placed_ts = {}             # (ticker, side) -> ts of last placement
        self.S_ref = {}                 # (ticker, side) -> S at last ALLOCATE
        self.qual_ref = {}              # (ticker, side) -> qualifies at last ALLOCATE
        self.shed_since = {}            # ticker -> ts the shed quote went up
        self.shed_target = {}           # (ticker, side) -> contracts the shed must cover
        self.refilled = {}              # (ticker, side) -> contracts re-posted this window
        self.mbb_degraded = set()       # slots automatically on cancel-first (§4.2)
        # S3: seeded FROM THE LEDGER, so a restart preserves A and does not refire
        # checkpoints it already ran.
        self.checkpoints_done = {k: set(v) for k, v in state.checkpoints_done.items()}
        self.accrued = dict(state.accrued)
        self.last_accrual_ts = 0.0
        self.classified = {}            # ticker -> §4.6 classification (B1)
        self.last_classify = 0.0
        self.halted = False             # §8.4 day stop / §8.5 budget trip
        self.last_place_skip = None     # FIX-B: why the last place() declined, if it did
        self.released = set()           # program_ids already released as out-of-window
        self.defer_404 = False          # D1: suppress the blocking 36s re-query in bulk
        self.fees_paid = 0.0            # taker fees, for the §8.4 mark
        self.last_resync = 0.0
        self.last_snapshot = 0.0
        self.last_paidout_poll = 0.0
        self.stopping = False
        self.day_pnl = 0.0

    # -- coid sequence, persisted (§9.5) --------------------------------------------------
    def next_seq(self):
        self.st.coid_seq += 1
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(SEQ_PATH, "w") as fh:
                fh.write(str(self.st.coid_seq))
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            pass
        log("coid_seq", k="coid_seq", seq=self.st.coid_seq)
        return self.st.coid_seq

    # -- exchange writes ------------------------------------------------------------------
    def do_post(self, body):
        if DRY:
            print("    [dry] POST %s %s" % (ORDERS_PATH, json.dumps(body)))
            return 0, {"_dry": True}
        return signed(self.auth, "POST", ORDERS_PATH, body=body)

    def do_cancel(self, order_id):
        if DRY:
            print("    [dry] DELETE %s/%s" % (ORDERS_PATH, order_id))
            return 200, {"reduced_by": "0.00"}
        return signed(self.auth, "DELETE", "%s/%s" % (ORDERS_PATH, order_id))

    def do_fills(self, order_id=None, min_ts=None, max_ts=None):
        """§8.6's single exception: fills are IMMUTABLE HISTORICAL FACTS, not an index of
        live state.  Used ONLY in the §9.4 restart disambiguation."""
        params = {"limit": 200}
        if order_id:
            params["order_id"] = order_id
        if min_ts:
            params["min_ts"] = int(min_ts)
        if max_ts:
            params["max_ts"] = int(max_ts)
        st, body = signed(self.auth, "GET", "/portfolio/fills", params=params)
        if st != 200:
            return FillsRead(ok=False), []
        fills = (body or {}).get("fills") or []
        return FillsRead(ok=True, count=sum(num(f.get("count"), 0.0) for f in fills)), fills

    @staticmethod
    def fill_key(f, idx=None):
        """S2/NEW-3 — the fills API's own immutable identity for one fill.  Kalshi returns
        `trade_id`; `fill_id`/`id` are accepted as aliases.  If none is present we
        SYNTHESISE a key rather than writing None — a row with no key is a row replay cannot
        deduplicate, and §9.4 step 4 re-reads an overlapping window by construction.

        NEW-3: the first synthetic form keyed on (order_id, ticker, side, count, time) and
        COLLIDED on two genuinely distinct fills of equal size at the same timestamp — the
        exchange fills a 10-lot order as 5+5 routinely.  Colliding keys make replay DROP the
        second fill, i.e. UNDER-count inventory, which is the §9/§9.4b naked-short direction
        and the one error this whole subsystem exists to prevent.  The key therefore adds
        the fill PRICE and the fill's ENUMERATION INDEX within its response.

        Tradeoff, stated: the index makes the synthetic key response-order dependent, so a
        reordered re-read can fail to dedupe and DOUBLE-count.  That is the deliberate
        direction — over-counting inventory is conservative (§9.4a books the ambiguous case
        as fully filled), under-counting is not.  The synthetic path is a fallback only:
        real fills carry `trade_id`, which is stable under reordering.
        """
        for k in ("trade_id", "fill_id", "id"):
            v = f.get(k)
            if v:
                return str(v)
        return "syn-%s|%s|%s|%s|%s|%s|%s" % (
            f.get("order_id"), f.get("ticker"), f.get("side"), num(f.get("count"), 0.0),
            f.get("yes_price", f.get("price")), f.get("created_time") or f.get("ts"),
            "?" if idx is None else int(idx))

    # -- placement ------------------------------------------------------------------------
    def place(self, ticker, side, price_c, size, expiration_ts):
        self.last_place_skip = None
        if self.halted:
            self.last_place_skip = "halted"
            # §8.4/§8.5: a halted process posts NOTHING, on every path into placement.
            return None
        if ticker in self.st.poisoned or ticker in self.st.assume_filled:
            return None
        price = price_c / 100.0
        net = self.st.net_position(ticker)
        # FIX-A-1: net against the closing room RESTING ORDERS HAVE NOT ALREADY TAKEN.
        # Using the raw net position here let a second closing order on the same side price
        # at $0 and breach the ceiling once both rested.
        room = self.st.closing_room(ticker, side)
        add = order_collateral_usd(side, price, size, room=room)    # FIX-A
        closing = closing_qty(side, size, room=room)
        if self.st.collateral + add > MAX_TOTAL_COLLATERAL_USD + 1e-9:
            self.last_place_skip = "collateral_ceiling"             # FIX-B needs the reason
            log("skip_post", ticker=ticker, side=side, why="collateral_ceiling",
                committed=round(self.st.collateral, 4), would_add=round(add, 4),
                closing_qty=round(closing, 4), closing_room=round(room, 4),
                gross=round(size * unit_collateral(side, price), 4),
                ceiling=MAX_TOTAL_COLLATERAL_USD)
            return None
        cap = refill_cap(unit_collateral(side, price))                 # §8.7
        if self.refilled.get((ticker, side), 0) + size > cap:
            self.last_place_skip = "refill_cap"
            log("skip_post", ticker=ticker, side=side, why="refill_cap", cap=cap)
            return None
        # §8.1/§5.5 — the inventory cap is on NET, and it binds on the WORST CASE of this
        # order filling in full.  A shed order is exempt: it reduces |net| by construction.
        # §8.1/§5.5 + C1/C9 — the inventory cap is a DOLLAR cap on NET exposure, measured
        # against what we PAID, and it counts what orders already resting on this side could
        # still add.  The old form bounded `net + size` in CONTRACTS against the current
        # price and was blind to resting orders, so make-before-break's two-orders-one-side
        # shape let both pass independently and both fill — 2x the cap.
        exposure = self.st.net_exposure_usd(ticker, side, size,
                                            unit_collateral(side, price))
        worst = net + size if side == "bid" else net - size
        if exposure > INV_CAP_USD + 1e-9 and abs(worst) > abs(net):
            self.last_place_skip = "net_inventory_cap"
            log("skip_post", ticker=ticker, side=side, why="net_inventory_cap",
                net=net, worst=worst, exposure_usd=round(exposure, 4),
                cap_usd=INV_CAP_USD,
                basis=round(self.st.entry_basis(
                    ticker, "yes" if worst > 0 else "no"), 4))
            return None
        seq = self.next_seq()
        coid = make_coid(ticker, side, seq)
        body = order_body(ticker, side, price, expiration_ts, coid, size)
        log("place_req", k="place_req", coid=coid, ticker=ticker, side=side,
            price_c=price_c, size=size, seq=seq)
        status, resp = self.do_post(body)
        if DRY:
            return None
        if not (200 <= status < 300):
            self.st.post_error_ts.append(_now())
            # B2: status 0 is a TRANSPORT failure, not a rejection.  The exchange may well
            # have accepted the order — we simply never learned its order_id.  Booking that
            # as a plain post error leaves an order that is invisible to
            # resting_collateral, to cancel_all and to the restart sweep: unbounded,
            # untrackable exposure.  Treat it exactly like a 2xx without an order_id:
            # poison the market, and record the coid so the restart sweep knows to look for
            # fills on it.
            phantom = (status == 0)
            poison = (status == 409) or phantom                        # §8.5
            if poison:
                self.st.poisoned.add(ticker)
            if phantom:
                self.st.phantom_risk.add(ticker)
                log("phantom_risk", k="phantom_risk", coid=coid, ticker=ticker, side=side,
                    price=price, size=size,
                    why="place transport failure - order MAY BE LIVE with no order_id")
                ntfy("LIP v4 PHANTOM ORDER RISK",
                     "%s %s coid=%s: POST timed out; the order may be live and is not "
                     "tracked. Market poisoned." % (ticker, side, coid))
            log("place_resp", k="place_resp", coid=coid, ticker=ticker, side=side,
                price=price, size=size, err="http_%d" % status, poison=poison,
                phantom=phantom, body=json.dumps(resp, default=str)[:200])
            return None
        oid = dig(resp, "order_id")
        if not oid:
            # §8.5: a placement response with no order_id poisons the market immediately —
            # we cannot cancel what we cannot name.
            self.st.post_error_ts.append(_now())
            self.st.poisoned.add(ticker)
            log("place_resp", k="place_resp", coid=coid, ticker=ticker, side=side,
                price=price, size=size, err="2xx_without_order_id", poison=True)
            return None
        fc = num(dig(resp, "fill_count"), 0.0)
        rc = dig(resp, "remaining_count")
        o = OrderState(oid, coid, ticker, side, price, size, fc,
                       None if rc is None else num(rc))
        self.st.orders[o.order_id] = o
        if fc > 0:
            self.st._credit_fill(o, fc)
        self.refilled[(ticker, side)] = self.refilled.get((ticker, side), 0) + size
        log("place_resp", k="place_resp", order_id=o.order_id, coid=coid, ticker=ticker,
            side=side, price=price, size=size, fill_count=fc,
            remaining_count=o.remaining_count, seq=seq)
        return o

    def cancel(self, order):
        log("cancel_req", k="cancel_req", order_id=order.order_id, ticker=order.ticker)
        status, body = self.do_cancel(order.order_id)
        rb = dig(body, "reduced_by")
        log("cancel_resp", k="cancel_resp", order_id=order.order_id, ticker=order.ticker,
            http=status, reduced_by=rb,
            body=None if status in (200, 404) else json.dumps(body, default=str)[:200])
        if status == 200 and rb is not None:
            order.reduced_by = max(0.0, min(order.remaining_count, num(rb, 0.0)))
            learned = max(0.0, order.remaining_count - order.reduced_by)
            order.state = ST_CLOSED
            self.st.consec_cancel_anomalies = 0
            if learned:
                self.st._credit_fill(order, learned)
            return True
        if status == 404:
            order.state = ST_UNKNOWN
            self.st.consec_cancel_anomalies = 0
            self.resolve_404(order)
            return True
        # 410 / 5xx / transport / 200-without-reduced_by: the order MAY BE LIVE (§8.5).
        order.state = ST_UNKNOWN
        self.st.poisoned.add(order.ticker)
        self.st.consec_cancel_anomalies += 1
        log("poison", k="poison", ticker=order.ticker, why="cancel_anomaly_http_%s" % status)
        ntfy("LIP v4 cancel anomaly",
             "%s http=%s - order may be LIVE; market poisoned" % (order.ticker, status))
        return False

    def resolve_404(self, order):
        """§9.4a — never book zero silently.  Two reads, 36s apart.

        B4: this is idempotent.  Restart runs TWO passes that can both reach a given order
        (the UNKNOWN loop cancels it, gets a 404 and resolves here; then the unresolved_404
        loop calls here again), and the second pass recomputed the same `n` and credited it
        a second time — doubled inventory, which the recycler then sheds against contracts
        we never held: a real naked short out of pure bookkeeping.
        """
        if order.reduced_by is not None:
            log("resolve_404_skipped", order_id=order.order_id, ticker=order.ticker,
                why="already_resolved")
            return
        if self.defer_404:
            # D1: no blocking re-query on a bulk/shutdown path.  The cancel_resp 404 row is
            # already on the ledger, so replay will list it in unresolved_404 and
            # restart_recovery — now idempotent per B4 — owns it.
            log("resolve_404_deferred", order_id=order.order_id, ticker=order.ticker,
                why="bulk_cancel_path")
            return
        r1, f1 = self.do_fills(order_id=order.order_id)
        rows = list(f1)
        verdict, filled, requery_at = disambiguate_404(order, r1)
        if verdict == R404_NEED_REQUERY:
            log("fills_requery_scheduled", order_id=order.order_id, at=requery_at,
                delay_s=FILLS_REQUERY_DELAY_S)
            while _now() < requery_at and not self.stopping:
                time.sleep(min(1.0, max(0.0, requery_at - _now())))
            r2, f2 = self.do_fills(order_id=order.order_id)
            rows = list(f2)
            verdict, filled, _ = disambiguate_404(order, r1, r2)
        if verdict == R404_ASSUME_FILLED:
            self.st.assume_filled.add(order.ticker)
            self.st.poisoned.add(order.ticker)
            log("assume_filled", k="assume_filled", order_id=order.order_id,
                ticker=order.ticker, side=order.side,
                why="fills_query_error_or_disagreement")
            n = max(0.0, order.remaining_count)
            order.reduced_by = 0.0
            order.state = ST_CLOSED
            self.st._credit_fill(order, n)
            ntfy("LIP v4 assume_filled FREEZE",
                 "%s frozen for QUOTING AND RECYCLING until a human reconciles"
                 % order.ticker)
            return
        if verdict == R404_EXPIRED:
            order.reduced_by = order.remaining_count
            order.state = ST_CLOSED
            log("expired", k="expired", order_id=order.order_id, ticker=order.ticker)
            return
        n = max(0.0, filled - order.fill_count)
        log("fill_obs", k="fill_obs", order_id=order.order_id, ticker=order.ticker,
            side=order.side, count=n, price_c=int(round(order.price * 100)),
            src="fills_api",
            # S2: one order-scoped resolution is one idempotent event.  The key is the set
            # of underlying trade ids when we have them, and the order id otherwise, so a
            # repeated restart cannot resolve the same 404 twice.
            fill_id="o404-%s-%s" % (
                order.order_id,
                ",".join(sorted(self.fill_key(f, i) for i, f in enumerate(rows)))
                or "none"))
        order.extra_fills += n
        order.reduced_by = 0.0
        order.state = ST_CLOSED
        self.st._credit_fill(order, n)

    # -- §9.4 restart procedure -----------------------------------------------------------
    def restart_recovery(self):
        """(1) replay (already done) (2) UNKNOWNs (3) cancel each (4) prefix sweep + ONE
        time-windowed fills query (5) re-derive positions from filled_cum."""
        unknown = [self.st.orders[o] for o in self.st.unknown_orders
                   if o in self.st.orders]
        if unknown:
            log("restart_unknown_orders", n=len(unknown),
                ids=[o.order_id for o in unknown][:20])
        for o in unknown:
            if o.state == ST_UNKNOWN and o.reduced_by is None:
                self.cancel(o)
        for oid in list(self.st.unresolved_404):
            o = self.st.orders.get(oid)
            if o is not None:
                self.resolve_404(o)
            self.st.unresolved_404.remove(oid)
        # step 4: sweep-cancel every resting order carrying our STABLE coid prefix.
        for o in list(self.st.orders.values()):
            if o.state in (ST_LIVE, ST_UNKNOWN) and owns_coid(o.coid) and \
                    o.reduced_by is None:
                self.cancel(o)
        if self.st.phantom_risk:
            # B2: these markets had a POST whose transport failed, so an untracked order may
            # be live.  We cannot cancel what we cannot name, but we CAN discover what it
            # filled — the time-windowed fills query below covers them, and they stay
            # poisoned until an operator reconciles.
            log("phantom_risk_markets", tickers=sorted(self.st.phantom_risk),
                why="untracked order may be live; fills query and poison are the mitigation")
            ntfy("LIP v4 phantom-risk markets on restart",
                 "%s - poisoned; reconcile by hand" % sorted(self.st.phantom_risk))
        if self.st.last_ts > 0 and self.auth and not DRY:
            lo, hi = crash_gap_window(self.st.last_ts)
            read, fills = self.do_fills(min_ts=lo, max_ts=hi)
            if read.ok:
                known = set(self.st.orders.keys())
                for i, f in enumerate(fills):
                    if str(f.get("order_id")) in known:
                        continue                       # already attributed above
                    fid = self.fill_key(f, i)
                    if fid in self.st.seen_fill_ids:
                        # S2: the re-query window OVERLAPS the previous one by design, so a
                        # crash loop re-observes these.  Skip what the ledger already has.
                        continue
                    self.st.seen_fill_ids.add(fid)
                    # B3: normalise (side=yes|no, action=buy|sell) into the ledger's own
                    # (bid|ask, sign) BEFORE writing, so the row means the same thing to
                    # this process and to every future replay.
                    leg, sign = normalize_fill(f.get("side"), f.get("action"))
                    cnt = num(f.get("count"), 0.0)
                    px_c = int(round(num(f.get("yes_price"), 0.0)))
                    log("fill_obs", k="fill_obs", order_id=None, fill_id=fid,
                        ticker=f.get("ticker"), side=leg, sign=sign,
                        action=str(f.get("action") or "buy").lower(),
                        raw_side=f.get("side"), count=cnt,
                        price_c=px_c, src="fills_api", why="crash_gap")
                    # B3: and APPLY them.  They were logged but never folded into running
                    # state, so the process restarted believing it held nothing while the
                    # ledger said otherwise — live and replayed state disagreed from the
                    # first second.
                    self.st.apply_fill(f.get("ticker"), leg, cnt, px_c / 100.0, sign)
            else:
                log("crash_gap_query_failed", lo=lo, hi=hi)
        log("restart_recovered", collateral=round(self.st.collateral, 4),
            filled_cum={"%s|%s" % k: v for k, v in self.st.filled_cum.items()},
            positions=self.st.positions, frozen=sorted(self.st.assume_filled))

    # -- requote: MAKE BEFORE BREAK (§4.1/§4.2) -------------------------------------------
    def requote(self, ticker, side, price_c, size, expiration_ts):
        """§4.1 make-before-break, with the §4.2 fallback and the FIX-B ceiling path.

        FIX-B, from the live run: a re-CENTRE of an existing resting order was being costed
        as a brand-new post.  Make-before-break needs headroom for the transient overlap
        (§2.4 reserves it), but at a saturated ceiling that headroom is gone, so the make
        leg was skipped on `collateral_ceiling`, the requote never happened, and the quote
        froze off-best and decayed at 0.5^ticks — the exact coverage loss §4.1 exists to
        prevent, arriving through the risk control rather than through the exchange.

        A cancel-first requote of EQUAL OR SMALLER size is collateral-neutral to within one
        tick: the cancel releases the old order's commitment before the repost takes its
        own, and the two differ only by the price improvement being chased (at most a cent
        per contract).  The repost is still ceiling-checked, so even that residual is
        declined safely rather than breaching.  A ceiling block on the OVERLAP is therefore
        not a reason to skip, it is a reason to take the path §4.2 already built.  It is logged as `mbb_degraded_ceiling` and, unlike a balance reject,
        does NOT latch the slot into cancel-first — the ceiling is transient and the next
        cycle should try the overlap again.
        """
        key = (ticker, side)
        old = self.live_by_slot.get(key)
        if MAKE_BEFORE_BREAK and key not in self.mbb_degraded:
            new = self.place(ticker, side, price_c, size, expiration_ts)
            if new is not None:
                if old is not None:
                    self.cancel(old)                    # confirmed order_id, THEN cancel
                self.live_by_slot[key] = new
                self.placed_ts[key] = _now()
                return new
            if DRY:
                return None          # --dry never places, so there is nothing to degrade
            if old is None:
                # No overlap was attempted: this is a plain new post that the ceiling, the
                # refill cap or an inventory cap declined.  Nothing to degrade, and
                # cancel-first has nothing to cancel.
                return None
            if self.last_place_skip == "collateral_ceiling" and \
                    size <= old.resting + 1e-9:
                # FIX-B: transient, and cancel-first is collateral-neutral here.  Do NOT
                # latch mbb_degraded — that is reserved for the exchange telling us no.
                log("mbb_degraded_ceiling", ticker=ticker, side=side,
                    size=size, old_resting=old.resting,
                    committed=round(self.st.collateral, 4),
                    ceiling=MAX_TOTAL_COLLATERAL_USD,
                    why="overlap_headroom_unavailable_requote_is_collateral_neutral")
            else:
                # §4.2 AUTOMATIC degradation: an insufficient-balance / margin-reject on the
                # make leg latches THIS SLOT to cancel-first; it retries at the next
                # checkpoint.
                self.mbb_degraded.add(key)
                log("mbb_degraded", ticker=ticker, side=side,
                    why="make_leg_rejected", skip=self.last_place_skip,
                    cancel_first_period_s=CANCEL_FIRST_PERIOD_S)
        if old is not None:
            self.cancel(old)
            self.live_by_slot.pop(key, None)
        new = self.place(ticker, side, price_c, size, expiration_ts)
        if new is not None:
            self.live_by_slot[key] = new
            self.placed_ts[key] = _now()
        return new

    def cancel_all(self, reason):
        live = [o for o in self.st.orders.values()
                if o.state in (ST_LIVE, ST_UNKNOWN) and o.reduced_by is None]
        if not live:
            return
        log("cancel_all_begin", reason=reason, n=len(live))
        # D1: the §9.4a 36s re-query is a BLOCKING sleep.  Six pending 404s in a bulk cancel
        # is 216s against the unit's TimeoutStopSec, so systemd SIGKILLs us mid-shutdown —
        # stranded orders AND unresolved 404s, which is the B4 amplifier.  Defer the
        # re-query here; the single-cancel path in the live loop keeps it.
        prev = self.defer_404
        self.defer_404 = True
        try:
            for o in live:
                self.cancel(o)
        finally:
            self.defer_404 = prev
        self.live_by_slot.clear()

    # -- one cycle ------------------------------------------------------------------------
    def classify_market(self, prog, body, now, denied=False, assume_filled=False):
        """Fold one book poll into the classification table used by the §4.6 clamp.

        NEW-4: this is called from the 1 Hz loop as well as the sweep.  Every book the
        quoting loop already fetched is free classification evidence; not folding it in left
        the rank table up to CLASSIFY_REFRESH_S stale for the very markets being polled
        every second, so a rung that flipped pinned kept its REST slot for up to 15 minutes,
        and if all six flipped at once the `if not chosen` escape re-sweep never fired
        because `chosen` was computed from the stale table.  Money-safe (ALLOCATE still
        sees the fresh book and funds nothing) but it is B1 again in the time dimension.
        """
        tk = prog["market_ticker"]
        slots, info = slots_from_market(
            prog, body, now, denied=denied, assume_filled=assume_filled,
            accrued=self.accrued.get(prog["program_id"], 0.0))
        H = window_hours(prog["start_ts"], prog["end_ts"])
        rho = pool_rate(prog["period_reward"], H)
        yb, ya = info["yes_bid_c"], info["yes_ask_c"]
        sides = []
        for side, sc, best_c in (("bid", info["yes_entry"], yb),
                                 ("ask", info["no_entry"], ya)):
            p = unit_collateral(side, (best_c or MIN_LEGAL_PRICE_C) / 100.0)
            sides.append({"S": sc.S, "p": max(p, 0.01), "qualifies": sc.qualifies,
                          "legal": not info["pinned"],
                          "target_size": prog["target_size_fp"]})
        self.classified[tk] = {"rho": rho, "pinned": info["pinned"],
                               "denied": tk in self.st.poisoned
                               or tk in self.st.assume_filled,
                               "sides": sides, "ts": now}
        self.books[tk] = body
        self.scores[tk] = info
        return slots, info

    def classify_sweep(self, progs, now):
        """§4.6 cold start / low-cadence refresh.  One classification poll per candidate
        market, rate-limited to half the shared budget.  Candidates are the allowlisted
        event's markets when EVENT_ALLOWLIST is set, else the top CLASSIFY_MAX_MARKETS by
        rho — rho DOES rank across events, it only fails within one."""
        cands = sorted(progs, key=lambda p: (-pool_rate(
            p["period_reward"], window_hours(p["start_ts"], p["end_ts"])),
            str(p["market_ticker"])))
        if not EVENT_ALLOWLIST:
            cands = cands[:CLASSIFY_MAX_MARKETS]
        log("classify_sweep_begin", n=len(cands), allowlist=EVENT_ALLOWLIST or "OFF")
        pinned_ct = 0
        for p in cands:
            if self.stopping:
                break
            tk = p["market_ticker"]
            st, body = public_get("/markets/%s/orderbook" % tk, {"depth": "50"})
            if st != 200:
                log("book_error", ticker=tk, http=st, phase="classify")
                continue
            _, info = self.classify_market(p, body, now)
            if info["pinned"]:
                pinned_ct += 1
            time.sleep(1.0 / CLASSIFY_RATE_HZ)
        self.last_classify = now
        ranked = market_poll_rank(self.classified)
        log("classify_sweep_done", n_classified=len(self.classified), pinned=pinned_ct,
            chosen=ranked,
            chosen_values=[round(market_rank_value(self.classified[t]), 6)
                           for t in ranked])

    def release_out_of_window(self, now):
        """OUT-OF-WINDOW RELEASE — a market whose best program is not currently earning must
        not hold quotes.  That is BOTH ends of the window: a program that has ended, and one
        that has not started (beyond PREPOSITION_LEAD_H).  The pre-start case is the live
        defect: three WNBA-mention slots opening 10.5h later held ~$11 of a BINDING ceiling
        while live-window markets were turned away for lack of it.

        The allocator already drops an ended program on the next cycle (cycle() filters on
        `end_ts > now`, so no slots are built for it and nothing is allocated), but DROPPING
        A SLOT DOES NOT CANCEL WHAT IS ALREADY RESTING.  Those orders would sit on a dead
        pool earning nothing, holding collateral against the §8.3 ceiling and carrying live
        fill risk for no reward — strictly dominated.

        EXCEPT closing orders.  Inventory OUTLIVES the program that produced it (§5: the
        position settles on the market's own schedule, not the pool's), so a shed that is
        unwinding a position must persist past the window end.  Cancelling it would strand
        the inventory until settlement, which is the §5.3 failure the recycler exists to
        prevent — and, in the exact shape FIX-A fixed, would do so at the one moment the
        position can still be worked.
        """
        lead_s = PREPOSITION_LEAD_H * 3600.0

        def earning(q, at):
            """In-window now, or within the pre-positioning lead of opening."""
            return q["end_ts"] > at and (q.get("start_ts") or 0.0) <= at + lead_s

        for p in list(self.programs.values()):
            pid = p["program_id"]
            if earning(p, now):
                self.released.discard(pid)      # re-arm: a pre-start program will start
                continue
            if pid in self.released:
                continue
            tk = p["market_ticker"]
            reason = "ended" if p["end_ts"] <= now else "pre_start"
            # another EARNING program on the same market still wants these quotes
            if any(q["market_ticker"] == tk and earning(q, now)
                   for q in self.programs.values()):
                self.released.add(pid)
                log("out_of_window_release", program_id=pid, ticker=tk, reason=reason,
                    kept="another earning program on this market")
                continue
            orders = [o for o in self.st.orders.values()
                      if o.ticker == tk and o.resting > 0]
            _, _, closing = allocate_closing_room(orders, self.st.net_position(tk))
            cancelled, kept = [], []
            for o in sorted(orders, key=lambda x: str(x.order_id)):
                if closing.get(str(o.order_id), 0.0) > 0:
                    kept.append(o.order_id)          # inventory outlives the program
                    continue
                self.cancel(o)
                self.live_by_slot.pop((tk, o.side), None)
                cancelled.append(o.order_id)
            self.released.add(pid)
            self.classified.pop(tk, None)            # and it leaves the §4.6 poll ranking
            log("out_of_window_release", program_id=pid, ticker=tk, reason=reason,
                cancelled=cancelled, kept_closing=kept,
                net_position=self.st.net_position(tk))

    def sweep_settlements(self, now):
        """C2 — release settled inventory.  Runs on the classify sweep's 900s cadence, which
        is ample: settlement is a once-per-market event and the release only ever FREES
        capacity, so being late costs opportunity, never safety.

        Doctrine-clean (§8.6): this reads the PUBLIC market endpoint — market truth — never
        the portfolio positions index.  A position is released if and only if the exchange
        published a result for its market.
        """
        for ticker in sorted(self.st.positions.keys()):
            pos = self.st.positions.get(ticker) or {}
            if (abs(pos.get("yes", 0.0)) + abs(pos.get("no", 0.0))) <= 0:
                continue                      # nothing held: idempotency guard
            st_code, body = public_get("/markets/%s" % ticker)
            if st_code != 200:
                log("settlement_check_failed", ticker=ticker, http=st_code)
                continue
            ok, result = is_settleable(body)
            if not ok:
                continue
            ry, rn, cost, pnl = settlement_release(pos, self.st.position_cost.get(ticker),
                                                   result)
            log("settlement", k="settlement", ticker=ticker, result=result,
                released_yes=ry, released_no=rn, cost_released=round(cost, 4),
                realized_pnl=round(pnl, 4))
            self.st.realized_pnl += pnl
            self.st.positions[ticker] = {"yes": 0.0, "no": 0.0}
            self.st.position_cost[ticker] = 0.0
            self.st.position_cost_leg[ticker] = {"yes": 0.0, "no": 0.0}

    def check_day_stop(self, slots, alloc, now):
        """§8.4 global day stop.  Reads the ledger-reconstructed positions and cost (§9.3)
        marked against the current books — never an exchange index (§8.6)."""
        mids = {}
        for tk, info in self.scores.items():
            yb, ya = info.get("yes_bid_c"), info.get("yes_ask_c")
            if yb is not None and ya is not None:
                mids[tk] = (yb + ya) / 200.0
        pnl = mark_to_market_pnl(self.st.positions, self.st.position_cost, mids,
                                 self.fees_paid) + self.st.realized_pnl   # C2
        unpriced = unpriced_positions(self.st.positions, mids)
        if unpriced:
            # NEW-2: these are marked at cost, i.e. excluded from the stop's evidence.
            # Never let that silence look like a clean book.
            log("unpriced_positions", unpriced_position_count=len(unpriced),
                tickers=unpriced[:10])
        proj_day = sum(reward_rate(s.rho, alloc.get(s.key, 0), s.S + s.W)
                       for s in slots) * 24.0
        self.day_pnl = pnl
        if not day_stop_breached(pnl, proj_day):
            return False
        log("day_stop_breached", pnl_usd=round(pnl, 4),
            projected_day_reward_usd=round(proj_day, 4),
            stop_usd=round(day_stop_usd(proj_day), 4))
        ntfy("LIP v4 DAY STOP",
             "pnl $%.2f breached the $%.2f stop - cancelling all, flattening, exiting"
             % (pnl, day_stop_usd(proj_day)))
        self.halted = True                       # §8.4: no further posts, on any path
        self.cancel_all("day_stop")
        for tk in list(self.st.positions.keys()):
            self.run_recycler(tk, alloc, slots, now)      # §5.4 flatten via maker shed
        self.stopping = True
        return True

    def persist_accrual(self, program_id):
        """S3 — the one piece of checkpoint state that cannot be re-derived from orders and
        fills.  Written every accrual tick and immediately on every checkpoint."""
        log("accrual", k="accrual", program_id=program_id,
            accrued_usd=round(self.accrued.get(program_id, 0.0), 6),
            checkpoints_done=sorted(self.checkpoints_done.get(program_id, set())))

    def cycle(self, now=None):
        now = _now() if now is None else now
        self.release_out_of_window(now)
        lead_s = PREPOSITION_LEAD_H * 3600.0
        progs = [p for p in self.programs.values()
                 if p["end_ts"] > now
                 and (p.get("start_ts") or 0.0) <= now + lead_s      # window START guard
                 and p["market_ticker"] not in self.st.assume_filled
                 and p["market_ticker"] not in self.st.poisoned]
        if not progs:
            self.stopping = True
            return
        # §4.6 CLASSIFY-THEN-CLAMP (B1).  The 1 Hz REST budget covers 6 markets; WHICH 6 is
        # decided by the classification sweep, not by rho (degenerate inside one event —
        # see the CLASSIFY_* block).  Pinned markets are excluded outright.
        if now - self.last_classify >= CLASSIFY_REFRESH_S:
            self.sweep_settlements(now)               # C2, before re-ranking
            self.classify_sweep(progs, now)
        by_ticker = {p["market_ticker"]: p for p in progs}
        chosen = [t for t in market_poll_rank(self.classified) if t in by_ticker]
        if not chosen:
            # Nothing classified as usable yet (or every candidate is pinned).  Do NOT fall
            # back to a rho ordering — that is the defect.  Re-sweep instead.
            self.classify_sweep(progs, now)
            chosen = [t for t in market_poll_rank(self.classified) if t in by_ticker]
        if not chosen:
            log("no_pollable_markets", n_candidates=len(progs),
                n_classified=len(self.classified))
            return
        progs = [by_ticker[t] for t in chosen]
        slots = []
        for p in progs:
            tk = p["market_ticker"]
            st, body = public_get("/markets/%s/orderbook" % tk, {"depth": "50"})
            if st != 200:
                log("book_error", ticker=tk, http=st)
                continue
            # NEW-4: classify from the book the quoting loop just fetched, so the rank
            # table is never staler than the last poll of that market.
            s2, info = self.classify_market(
                p, body, now, denied=(tk in self.st.poisoned),
                assume_filled=(tk in self.st.assume_filled))
            slots.extend(s2)
        if not slots:
            return

        # §10.3-P7 — cap CONCURRENT revival markets at 3, ranked by rho (the only
        # pre-fill ordering available).  Beyond the cap the slot is denied, not sized down.
        revivals = []
        for p in progs:
            i = self.scores.get(p["market_ticker"])
            if not i or i["pinned"]:
                continue
            if not (i["yes_entry"].qualifies and i["no_entry"].qualifies):
                revivals.append((p["market_ticker"],
                                 pool_rate(p["period_reward"],
                                           window_hours(p["start_ts"], p["end_ts"]))))
        allowed = p7_revival_allowed(revivals)
        rev_tickers = {t for t, _ in revivals}
        for s in slots:
            if s.ticker in rev_tickers and s.ticker not in allowed:
                s.denied = True
        if len(rev_tickers) > len(allowed):
            log("p7_revival_cap", n_candidates=len(rev_tickers), allowed=sorted(allowed))

        # §2.4 budget reservation (B3): two passes to the max-slot fixpoint.
        budget = MAX_TOTAL_COLLATERAL_USD
        alloc, spent, dropped, max_slot = ({}, 0.0, set(), 0.0)
        for _ in range(4):
            alloc, spent, dropped = allocate_with_forfeit_gate(slots, budget)
            max_slot = max([alloc.get(s.key, 0) * s.p for s in slots] or [0.0])
            newb = min(budget, reserve_budget(MAX_TOTAL_COLLATERAL_USD, max_slot))
            if abs(newb - budget) < 1e-9:
                break
            budget = newb                       # monotone down: the fixpoint is reachable
        # §2.4 hard invariant: the make-before-break double of the LARGEST slot must fit.
        if spent + max_slot > MAX_TOTAL_COLLATERAL_USD + 1e-9:
            log("budget_reserve_violation", spent=round(spent, 4),
                max_slot=round(max_slot, 4), ceiling=MAX_TOTAL_COLLATERAL_USD)
            alloc = {k: 0 for k in alloc}
            spent = 0.0
        log("allocate", budget=round(budget, 4), spent=round(spent, 4),
            dropped=sorted(str(d) for d in dropped),
            alloc={"%s|%s" % k: v for k, v in alloc.items() if v})

        exp_by_ticker = {p["market_ticker"]: int(p["end_ts"] - CLOSE_MARGIN_S)
                         for p in progs}
        for s in slots:
            q = alloc.get(s.key, 0)
            # §5.4/D4 — the maker shed IS the opposing slot's quote, floored at the size of
            # the position being unwound.  It scores, costs no incremental collateral (the
            # position covers it) and does not stop the side the charter would have stopped.
            # C8: clamp the shed at |net| so it can never FLIP the sign.  A 40-lot shed
            # against 20 held would take -20 to +20 — not a shed at all, a fresh opposite
            # position wearing a shed's name.  Anything the ALLOCATOR wants beyond |net| is
            # an earning quote and is governed by the C1 dollar cap in place(), not by this.
            shed_q = int(min(self.shed_target.get(s.key, 0),
                             abs(self.st.net_position(s.ticker))))
            q = max(q, shed_q)
            info = self.scores.get(s.ticker, {})
            best_c = info.get("yes_bid_c") if s.side == "bid" else info.get("yes_ask_c")
            cur = self.live_by_slot.get(s.key)
            if q <= 0:
                if cur is not None:
                    self.cancel(cur)
                    self.live_by_slot.pop(s.key, None)
                continue
            if best_c is None:
                continue
            our_c = int(round(cur.price * 100)) if cur else None
            age = now - self.placed_ts.get(s.key, 0.0)
            trig = requote_triggers(
                our_c, best_c, cur.resting if cur else 0.0, q, s.S,
                self.S_ref.get(s.key, s.S), True, self.qual_ref.get(s.key, True),
                age, now - self.last_resync)
            self.S_ref[s.key] = s.S
            if cur is None or trig:
                placed = self.requote(s.ticker, s.side, best_c, q,
                                      exp_by_ticker[s.ticker])
                # C4: the combined order (allocator size OR shed size, whichever is larger)
                # can fail the C1 cap as ONE order, and the whole quote then vanishes —
                # including the shed inside it, so the inventory locks.  A CLOSING-only
                # order always passes by netting, so retry at exactly the shed size.  The
                # earning tail is forgone this cycle; unwinding the inventory is not.
                if placed is None and shed_q > 0 and q > shed_q:
                    log("shed_retry_after_combined_reject", ticker=s.ticker, side=s.side,
                        combined=q, shed_only=shed_q, skip=self.last_place_skip)
                    placed = self.requote(s.ticker, s.side, best_c, shed_q,
                                          exp_by_ticker[s.ticker])
                    if placed is not None:
                        q = shed_q
            self.target_q[s.key] = q
            if at_best(our_c, best_c):
                self.at_best_s[s.key] = self.at_best_s.get(s.key, 0.0) + 1.0
            self.rest_s[s.key] = self.rest_s.get(s.key, 0.0) + 1.0
        self.last_resync = now

        # §12.4 / §3.5 — integrate the MODEL accrual over the presence we actually had.
        dt_h = min(max(0.0, now - self.last_accrual_ts), 5.0) / 3600.0 \
            if self.last_accrual_ts else 0.0
        self.last_accrual_ts = now
        funded = set()
        for s in slots:
            if alloc.get(s.key, 0) > 0:
                funded.add(s.program_id)
                self.accrued[s.program_id] = self.accrued.get(s.program_id, 0.0) + \
                    reward_rate(s.rho, alloc[s.key], s.S + s.W) * dt_h
        if dt_h > 0 and now - self.last_snapshot >= BOOK_SNAPSHOT_S:
            for pid in funded:
                self.persist_accrual(pid)               # S3, at the 60s snapshot cadence

        # §3.4 checkpoints, at WINDOW FRACTIONS of each program's own period
        for p in progs:
            done = self.checkpoints_done.setdefault(p["program_id"], set())
            for frac, cp in zip(CHECKPOINT_FRACTIONS,
                                checkpoint_times(p["start_ts"], p["end_ts"])):
                if frac in done or now < cp:
                    continue
                done.add(frac)
                self.run_checkpoint(p, alloc, slots, now, len(funded))
                self.persist_accrual(p["program_id"])   # S3: fire-once must survive restart

        # §12.4 capture the window's OWN book snapshots for reconciliation
        if now - self.last_snapshot >= BOOK_SNAPSHOT_S:
            self.last_snapshot = now
            log("snapshot", k="snapshot", positions=self.st.positions,
                collateral_usd=round(self.st.collateral, 4),
                orders={o.order_id: o.resting for o in self.st.live_orders()},
                scores={t: {"yes_S": i["yes_recon"].S, "no_S": i["no_recon"].S}
                        for t, i in self.scores.items()})

        # §5 recycler — disabled on any assume_filled market (§5.6)
        for p in progs:
            tk = p["market_ticker"]
            self.run_recycler(tk, alloc, slots, now)

        if self.check_day_stop(slots, alloc, now):      # §8.4
            return

        trip = self.st.budget_tripped(now)
        if trip:
            log("error_budget_tripped", why=trip)
            ntfy("LIP v4 error budget tripped", trip + " - cancelling all and exiting")
            self.halted = True
            self.stopping = True

    def run_checkpoint(self, prog, alloc, slots, now, funded_programs=1):
        ps = [s for s in slots if s.program_id == prog["program_id"]]
        # §4.2: a degraded slot retries make-before-break at the next checkpoint.
        for s in ps:
            if s.key in self.mbb_degraded:
                self.mbb_degraded.discard(s.key)
                log("mbb_retry", ticker=s.ticker, side=s.side)
        C = sum(alloc.get(s.key, 0) * s.p for s in ps)
        if C <= 0:
            return                  # we are not in this program; there is nothing to rescue
        h = max(0.0, (prog["end_ts"] - now) / 3600.0)
        rate_now = sum(reward_rate(s.rho, alloc.get(s.key, 0), s.S + s.W) for s in ps)
        # A is the ACCRUED payout, integrated over the presence we actually had — NOT
        # rate_now * elapsed_window, which would credit us for hours before we started.
        A = self.accrued.get(prog["program_id"], 0.0)
        best = max(ps, key=lambda s: alloc.get(s.key, 0))
        r = rescue(A, rate_now, h, best.rho, best.S + best.W, alloc.get(best.key, 0),
                   best.p, LAMBDA_MIN / LAMBDA_MIN_WINDOW_HOURS, C,
                   has_other_program=funded_programs > 1)
        log("checkpoint", program_id=prog["program_id"], ticker=prog["market_ticker"],
            action=r.action, delta_q=r.delta_q, proj=round(r.proj, 4),
            A=round(A, 4), rate_h=round(rate_now, 6), hours_left=round(h, 3),
            abandon_value=round(r.abandon_value, 6), hold_value=round(r.hold_value, 6))
        if r.action == ABANDON:
            for s in ps:
                cur = self.live_by_slot.get(s.key)
                if cur is not None:
                    self.cancel(cur)
                    self.live_by_slot.pop(s.key, None)

    def run_recycler(self, ticker, alloc, slots, now):
        if ticker in self.st.assume_filled:
            log("recycler_frozen", ticker=ticker, why="assume_filled")
            return
        pos = self.st.positions.get(ticker, {"yes": 0.0, "no": 0.0})
        if abs(pos["yes"] - pos["no"]) < 1e-9:
            return
        info = self.scores.get(ticker, {})
        yb, ya = info.get("yes_bid_c"), info.get("yes_ask_c")
        if yb is None or ya is None:
            return
        held_side = "bid" if pos["yes"] > pos["no"] else "ask"
        p_bid = (yb / 100.0) if held_side == "bid" else ((100 - ya) / 100.0)
        p_mid = ((yb + ya) / 200.0) if held_side == "bid" else (1.0 - (yb + ya) / 200.0)
        ps = [s for s in slots if s.ticker == ticker and s.side == held_side]
        R_blocked = sum(reward_rate(s.rho, alloc.get(s.key, 0), s.S + s.W) for s in ps)
        h = max(0.0, max([(p["end_ts"] - now) / 3600.0
                          for p in self.programs.values()] or [0.0]))
        action, info2 = recycle(pos["yes"], pos["no"], p_mid, p_bid, h,
                                LAMBDA_MIN / LAMBDA_MIN_WINDOW_HOURS, R_blocked,
                                shed_age_s=now - self.shed_since.get(ticker, now),
                                assume_filled=False)
        log("recycle", ticker=ticker, action=action, **{k: round(v, 6)
                                                        if isinstance(v, float) else v
                                                        for k, v in info2.items()})
        if action == TAKER_EXIT and not TAKER_EXIT_ENABLED:
            # S4: decided, priced, logged — NOT placed at this rung.  See the
            # TAKER_EXIT_ENABLED derivation in the config block.  The value forgone is the
            # RHS minus the LHS of §5.2, i.e. what the exit would have recovered.
            log("taker_exit_suppressed", ticker=ticker,
                value_forgone_usd=round(info2.get("rhs", 0.0) - info2.get("lhs", 0.0), 4),
                net=info2.get("net"), hours_left=round(h, 3),
                why="TAKER_EXIT_ENABLED=False at ceiling $%.2f" % MAX_TOTAL_COLLATERAL_USD)
            action = MAKER_SHED                        # keep shedding as a maker
        shed_key = (ticker, shed_slot(held_side))
        if action == MAKER_SHED:
            self.shed_since.setdefault(ticker, now)
            self.shed_target[shed_key] = abs(pos["yes"] - pos["no"])
        else:
            self.shed_target.pop(shed_key, None)
            if action != TAKER_EXIT:
                self.shed_since.pop(ticker, None)

    # -- the loop -------------------------------------------------------------------------
    def run(self):
        self.restart_recovery()
        while not self.stopping:
            t0 = _now()
            try:
                self.cycle(t0)
            except Exception as exc:
                log("cycle_error", err="%s: %s" % (type(exc).__name__, exc))
                raise
            if DRY:
                log("dry_cycle_complete")
                break
            dt = (1.0 / BOOK_POLL_HZ) - (_now() - t0)
            if dt > 0:
                time.sleep(dt)


# =============================================================================================
# main
# =============================================================================================
def main(argv):
    global DRY
    ap = argparse.ArgumentParser(description="lip_maker_v4")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="run the startup assertions and exit")
    args = ap.parse_args(argv[1:])
    if not (args.live or args.dry or args.check):
        print(__doc__.split("USAGE")[1])
        return 2
    DRY = not args.live

    print("=" * 92)
    print("LIP MAKER v4  mode=%s  now=%s" % ("DRY" if DRY else "LIVE", _utcstamp()))
    auth, note = load_auth()
    print("auth: %s" % note)
    # §10.1 — print BOTH revocation-EV numbers every run so the tradeoff is never implicit.
    print("anti-gaming price (§10.1): remaining program EV $%.0f-$%.0f (plan against the "
          "$3.4k-$5k middle); revival EV $%.0f; each +10pp of revocation risk costs "
          "$%.0f-$%.0f" % (PROGRAM_EV_LOW_USD, PROGRAM_EV_HIGH_USD, REVIVAL_EV_USD,
                           0.10 * PROGRAM_EV_LOW_USD, 0.10 * PROGRAM_EV_HIGH_USD))
    print("ceiling=$%.2f  lambda_min=%.2f/16h  entry_floor=$%.2f  rescue=$%.2f  "
          "allowlist=%s" % (MAX_TOTAL_COLLATERAL_USD, LAMBDA_MIN, ENTRY_FLOOR_USD,
                            RESCUE_TARGET_USD, EVENT_ALLOWLIST or "OFF (scan everything)"))

    programs = None
    try:
        programs = scan_programs()
        print("scanner: %d live liquidity programs" % len(programs))
    except Exception as exc:
        print("scanner FAILED: %s: %s" % (type(exc).__name__, exc))

    ok, results = startup_assertions(auth, note, programs)
    for name, good, detail in results:
        print("  [%s] %-38s %s" % ("OK " if good else "FAIL", name, detail))
    if not ok:
        print("REFUSING TO RUN — a startup assertion failed (§0.3).")
        return 3
    if args.check:
        return 0
    if not auth and not DRY:
        print("FATAL: --live requires credentials.")
        return 3
    print("=" * 92)

    st = replay_ledger_file(LEDGER_PATH)
    if os.path.exists(SEQ_PATH):
        try:
            with open(SEQ_PATH) as fh:
                st.coid_seq = max(st.coid_seq, int(fh.read().strip() or 0))
        except Exception:
            pass
    m = Maker(auth, st, programs or [])

    def on_signal(signum, _frame):
        log("signal", signum=signum)
        m.stopping = True

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(s, on_signal)
        except (ValueError, OSError, AttributeError):
            pass

    log("start", mode="dry" if DRY else "live", n_programs=len(programs or []),
        ceiling=MAX_TOTAL_COLLATERAL_USD, coid_prefix=COID_PREFIX, seq=st.coid_seq)
    rc = 0
    try:
        m.run()
    except BaseException as exc:
        log("unhandled", err="%s: %s" % (type(exc).__name__, exc))
        ntfy("LIP v4 crashed", "%s: %s - cancelling all" % (type(exc).__name__, exc))
        rc = 1
    finally:
        try:
            m.cancel_all("shutdown")                 # §9.6 EVERY exit path cancels first
        except BaseException as exc:
            log("cleanup_failed", err="%s: %s" % (type(exc).__name__, exc))
            ntfy("LIP v4 CLEANUP FAILED", "orders may be LIVE until expiration_ts")
            rc = 1
        stranded = [o.order_id for o in st.live_orders() if o.reduced_by is None]
        log("exit", rc=rc, stranded=stranded, collateral=round(st.collateral, 4),
            filled_cum={"%s|%s" % k: v for k, v in st.filled_cum.items()},
            poisoned=sorted(st.poisoned), frozen=sorted(st.assume_filled))
        if stranded:
            ntfy("LIP v4 exited with STRANDED orders",
                 "%s - expiration_ts is the only backstop" % stranded)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
