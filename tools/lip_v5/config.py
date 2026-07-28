"""
lip_v5.config — EVERY constant, with its spec-section derivation AND its note-23 §IV
MIRROR answer.

Note 23 §II implementor stanza: a constant without a derivation is an undeclared claim and
must not exist in this file.  Note 23 §IV: every GUARD carries the question *"this guards
one end/side/direction — name the other end; who guards it?"* answered beside it.  An
unnamed mirror is an unshipped incident.

Anything the spec itself flags as UNDERIVED (spec §9) is tagged `UNDERIVED §9.x` here, so
`grep UNDERIVED config.py` is the flag-upward list.
"""

import os

# =============================================================================================
# ENDPOINTS / TRANSPORT  (spec §3.5 "REST is used ONLY for ...")
# =============================================================================================
BASE = "https://api.elections.kalshi.com"    # v3/v4 prod host, unchanged
PREFIX = "/trade-api/v2"
ORDERS_PATH = "/portfolio/events/orders"     # v1 §4.7 — /portfolio/orders 410s
HTTP_TIMEOUT = 15                            # v3-proven

# =============================================================================================
# IDENTITY AND PATHS  (spec §6.1 — v5 NEVER writes any v4 path; that is what makes rollback
# one command)
# MIRROR (v5 writes ↔ v4 writes): disjoint coid prefix + disjoint files + the fresh-v4-
# heartbeat startup refusal below.  Two makers on one rung is self-trade plus double
# collateral, so the guard is symmetric by construction: v4 refuses nothing, v5 refuses
# everything, and v5 is the one being deployed.
# =============================================================================================
COID_PREFIX = "v5-"                          # §6.1 / §11 "Collisions"; NO run-id (v1 §9.5)
NESTOR_HOME = os.environ.get("NESTOR_HOME", os.path.expanduser("~/nestor"))
DATA_DIR = os.environ.get("LIP_V5_DATA_DIR", os.path.join(NESTOR_HOME, "data", "lip"))
LEDGER_PATH = os.path.join(DATA_DIR, "v5_ledger.jsonl")            # §6.1
PRESENCE_PATH = os.path.join(DATA_DIR, "v5_presence.jsonl")        # §6.2 N2 — own file
PRESENCE_DAILY_PATH = os.path.join(DATA_DIR, "v5_presence_daily.jsonl")   # §6.2 compaction
RECON_PATH = os.path.join(DATA_DIR, "v5_recon.jsonl")              # §6.1
SEQ_PATH = os.path.join(DATA_DIR, "v5_coid_seq")                   # §6.1
ADOPT_PATH = os.path.join(DATA_DIR, "v5_adopt.json")               # §6.3 C step 1
HANDBACK_PATH = os.path.join(DATA_DIR, "v5_handback.json")         # §6.3 rollback (SF-2)
V4_LEDGER_PATH = os.path.join(DATA_DIR, "v4_ledger.jsonl")         # READ-ONLY, §6.3 C.1
V4_HEARTBEAT_GLOB = os.path.join(DATA_DIR, "v4_*.jsonl")           # §6.1 freshness refusal
CASH_FEED_PATH = os.path.join(NESTOR_HOME, "data", "lip_cash_feed.json")   # §5.1
NESTOR_STATE_PATH = os.path.join(NESTOR_HOME, "data", "state.json")  # §11 Collisions
NTFY_TOPIC = "senate-nestor-2732e947"                              # §11 Alerts

# §6.1 — "Startup refuses if a v4 heartbeat is fresh (< 120 s)".
# Derivation: 120 s is 2× v4's own SAFETY_RESYNC_S (60 s), so one missed v4 cycle does not
# read as a dead v4.  MIRROR (refuse when v4 is alive ↔ refuse when v4 is dead but holding
# inventory): the second end is NOT a heartbeat question — it is §6.3-C's adoption gate,
# which enumerates every exchange position and refuses to quote any it did not adopt.
V4_HEARTBEAT_FRESH_S = 120.0

# =============================================================================================
# THE UNIT  (spec §0.2 "pool_usd = period_reward × 1e-4 WITH the startup refusal assertion
# (v1 §0.3) kept verbatim")
# MIRROR (unit too big ↔ unit too small): one assertion kills both directions — at 1e-3 every
# live program reads $1,000 and at 1e-5 every one reads $10.00, so the MATCH COUNT collapses
# to zero either way rather than degrading.  That is why the assertion counts matches instead
# of checking one program.
# =============================================================================================
PERIOD_REWARD_UNIT_USD = 1e-4
UNIT_ASSERT_EXPECT_USD = 100.00              # the MODAL pool
UNIT_ASSERT_TOL_USD = 0.01
UNIT_ASSERT_MIN_MATCHES = 30                 # v4-proven: ~20× margin against program mix
REFUSE_ON_UNIT_MISMATCH = True               # v1 §0.3 "or REFUSE TO RUN"

# =============================================================================================
# SCORING  (spec §0.2 inherited, not re-litigated)
# =============================================================================================
DISCOUNT_FACTOR_DEFAULT = 0.50               # discount_factor_bps 5000 on 100% of programs;
                                             # spec §0.1: this IS the objective's 0.5^ticks
MAX_LEGAL_PRICE_C = 99                       # v1 §1.3
MIN_LEGAL_PRICE_C = 1
S_MODE_ENTRY = "levels"                      # v1 §1.5 conservative for ENTRY
S_MODE_RECON = "cents"                       # v1 §1.5 our reading of the filing

# =============================================================================================
# (★) THE OBJECTIVE  (spec §0.3-§0.4) — the whole spec
# =============================================================================================
LAMBDA_MIN = 0.10                            # v1 §2.3, UNDERIVED §9.5 (inherited, still open)
LAMBDA_MIN_WINDOW_HOURS = 16.0               # "per 16h-equivalent" ⇒ λ_min/16 in $/h
FLOOR_RATE_PER_H = LAMBDA_MIN / LAMBDA_MIN_WINDOW_HOURS      # = 0.00625 /h, spec §0.4
STEP_FRACTION = 0.02                         # v1 §2.5 coarsest step landing within 2%
D_SEED_USD = 0.07                            # spec §2.4 / v1 §15.4, UNDERIVED §9.4
DRIFT_HORIZON_S = 60.0                       # spec §2.4 (v3's 5-9¢ cross-cycle horizon),
                                             # UNDERIVED §9.4
D_TRAILING_FILLS = 20                        # spec §2.4 "trailing 20 fills"
RULE_OF_THREE = 3.0                          # spec §2.4 — 95% upper bound on a zero-count
                                             # Poisson rate is 3/exposure.  DERIVED (it is
                                             # the closed form), which is precisely why it
                                             # replaces v1's guessed PHI_MID/PHI_CHEAP seeds.
PHI_SEED_MID = 0.08                          # spec §2.4 "seeds remain ONLY as the ceiling on
PHI_SEED_CHEAP = 0.001                       # φ_ub at zero exposure" — never as the estimate
PHI_CHEAP_PRICE_CUT = 0.05                   # v1 §2.2 band boundary
DECISIVE_FILLS = 10                          # spec §2.4: rel. s.d. 1/√10 = 32%, resolves a
                                             # 2× difference at ~2σ
DECISIVE_COMMITTED_H = 2.0                   # spec §2.4: Rule-of-Three bound at 2 $·h is
                                             # 1.5/h ... already below the hurdle at any
                                             # plausible d/p, so zero fills is decisive there
MAX_GATE_PASSES = 8                          # v1 §2.4 re-water-fill loop, bounded

# --- the r* fixed point (spec §1.3) ---
# MIRROR (r* too high ↔ too low): too HIGH prices carry high, admits fewer venues, allocates
# less — the conservative direction, which is why non-convergence takes the MAX.  Too LOW
# makes carry look cheap, which is the PayPal direction; the §1.4 unverified cap is the guard
# on that end, and it is the only guard on it, which is why cold start is capped not tuned.
# Spec §1.3 wrote 4, deriving it from RESIDUAL reduction (4 damped steps cut the residual 16×,
# which is exactly true).  But the STOP RULE is a 5% relative step change, and reaching that
# band from initial relative error `e` needs `k ≥ log2(e/0.05)`: 5 steps for a 2× seed, 9 for a
# 16× one.  At 4 the rule could never trip, so the fixpoint always fell back to
# `max(r*_0..r*_4)` and `rstar_no_converge` fired every cycle — alarm fatigue on a control that
# was doing nothing.  9 is the smallest value that makes the spec's OWN two statements
# consistent: it covers the 16× seed error §1.3 names, at the 5% tolerance §1.3 sets.
RSTAR_MAX_ITERS = 9                          # spec §1.3, reconciled (see money.solve_rstar)
RSTAR_DAMPING = 0.5                          # prevents 2-cycles (spec §1.3)
RSTAR_CONVERGE_FRAC = 0.05                   # ALLOCATE's own step resolution is 2%; chasing
                                             # below its own noise is theatre (spec §1.3)
RSTAR_NO_CONVERGE_ALERT_CYCLES = 3           # spec §1.3 "alert if 3 cycles in a row"
RSTAR_TRAILING_DAYS = 7                      # spec §1.3 "trailing-7d achieved marginal rate"

# =============================================================================================
# LIQUIDITY HORIZON  (spec §1.2) — ALL QUANTITIES IN HOURS.  The type error matters.
# =============================================================================================
SETTLE_LAG_H = 0.7                           # R171 measured 41 min.  BLOCKER-grade FLOOR:
                                             # nothing is ever liquid faster than the
                                             # settlement lag.
# MIRROR (floor on L_eff ↔ ceiling on L_eff): there is no ceiling and there must not be one.
# Without the floor, (close_ts − now) turns negative after close, carry turns NEGATIVE and the
# model ADMITS a venue on the strength of being stuck.  A ceiling would recreate exactly that
# by capping the punishment for being stuck.  The past-due escalation below is the other end.
PAST_DUE_ESCALATION = 2.0                    # spec §1.2; the no-information DIRECTION is
                                             # derived, the coefficient is UNDERIVED §9.4
L_SHED_TRAILING = 20                         # spec §1.2 "trailing 20 completed sheds"
HORIZON_GRACE_H = 24.0                       # spec §1.2 hard horizon exclusion, "+24 h grace
                                             # covers same-day-after settlement".
                                             # UNDERIVED §9.4.
HORIZON_EXEMPT_RUNG = 2                      # spec §1.2 "unless ratchet rung ≥ 2"

# =============================================================================================
# VERIFIED-ACCRUAL RATCHET  (spec §1.4)
# MIRROR (ratchet up ↔ ratchet down ↔ revive): up-1/down-2 is the down end; the revive end is
# REVIVE_REQUIRES_NEW_PERIOD + the T̂-posterior predicate.  Nothing revives on a timer.
# =============================================================================================
ENTRY_FLOOR_USD = 2.00                       # v1 §3.1 = 2× the $1.00 payout cliff.
                                             # UNDERIVED §9.5 (recalibrate to
                                             # $1.00/q05(actual/projected) after 5 periods)
RATCHET_UP = 1                               # spec §1.4 — a false up-step costs CAPITAL at a
RATCHET_DOWN = 2                             # venue that does not pay; a false down-step
                                             # costs only RATE at a venue re-verifiable
                                             # tomorrow.  Hence the asymmetry.
VERIFY_BAND = (0.5, 2.0)                     # spec §1.4 — the system's own declared model
                                             # tolerance (v1 §3.1/§12.3a), self-consistent by
                                             # construction.  UNDERIVED §9.4 as a measured
                                             # distribution.
RATCHET_BREAKEVEN_ACCURACY = 2.0 / 3.0       # spec §1.4: drift per reading is 3a − 2, so
                                             # a = 2/3 is the ladder's characteristic number.
                                             # This is the SENSOR-QUALITY REQUIREMENT.
UNVERIFIED_EXPOSURE_FRAC = 0.20              # spec §1.4 / §9.3 — ≤$60 at a $300 ceiling
N_UNVERIFIED_MAX = 8                         # spec §1.4 concurrent unverified venues
OVERSIZED_PROBE_FRAC = 0.02                  # spec §9.3: now a CLASSIFICATION threshold, not
                                             # a cap
OVERSIZED_PROBE_MAX = 2                      # spec §1.4 concurrent oversized-probe slots
STANDDOWN_DAYS = 2                           # spec §1.4 / charter §5
# MIRROR (unverified-exposure CEILING ↔ exploration FLOOR): spec §4.4 — a cap on learning is
# a cap on earning.  If unverified exposure < this while the queue is non-empty, admit the
# next queued venue.
EXPLORATION_FLOOR_FRAC = 0.05                # spec §4.4 mirror row

# =============================================================================================
# PRESENCE METERING  (spec §2)
# MIRROR (sampler bias UP from sampling just after our own requote ↔ bias DOWN from sampling
# inside a coverage gap): ONE guard kills both — a fixed monotonic 1 Hz phase that is never
# triggered by our own actions, with jitter asserted below 100 ms.
# =============================================================================================
METER_HZ = 1.0                               # spec §2.1 "a monotonic 1 Hz tick, FIXED phase"
METER_MAX_JITTER_S = 0.100                   # spec §4.4 mirror row — asserted, not hoped
PRESENCE_ROW_S = 60.0                        # spec §2.2 — deltas, so a crash loses ≤60 s
PRESENCE_COMPACT_DAYS = 7                    # spec §6.2 — DERIVED: exactly the trailing
                                             # window T̂'s shrinkage and §8.7's collapse
                                             # median require.  (Daily rotation granularity
                                             # is UNDERIVED §9.4.)
SHRINK_PSEUDO_DOLLAR_H = 2.0                 # spec §2.3 pseudo-weight k
SHRINK_PRIOR_DEFAULT = 0.5                   # spec §2.3 T₀ fallback.  UNDERIVED §9.4 —
                                             # affects probe ORDER only, never exposure,
                                             # because rung-0 caps bind first.
PSDH_MAX = 3600.0                            # every committed dollar resting at best every
                                             # second.  T̂ = PSDH/3600 needs NO threshold.

# --- automatic size-down / kill (spec §2.5) ---
# MIRROR (per-slot kill ↔ book-wide kill): §8.7's presence-collapse predicate.
# MIRROR (kill ↔ revive): §1.4's new-period revive predicate.
ZETA_RATCHET_UP = 1.5                        # spec §2.5
ZETA_HOLD = 1.0                              # spec §2.5
KILL_CONSEC_EVALS = 3                        # spec §2.5 — 45 min is the shortest interval
                                             # that cannot be tripped by one fill burst
                                             # inside one 15-min bucket.  UNDERIVED §9.4.
EVAL_INTERVAL_S = 900.0                      # spec §2.5 "every 15 min"

# --- presence collapse, book-wide (spec §8.7 T-D4) ---
COLLAPSE_WINDOW_H = 2.0                      # spec §8.7 — 2 h at ≥$300 committed is ≥600 $·h,
                                             # decisive by §2.4
COLLAPSE_FRAC = 0.25                         # spec §8.7 — a 4× book-wide degradation is the
                                             # same magnitude (2 ratchet rungs) that stands a
                                             # single venue down
COLLAPSE_MEDIAN_DAYS = 7                     # spec §8.7(c) minimum history
COLLAPSE_MIN_FUNDABLE_H_PER_DAY = 6.0        # spec §8.7(c) — "a median over 3 samples is not
                                             # a median"
COLLAPSE_STARVATION_FRAC = 0.20              # spec §8.7(b) — starvation is not toxicity.
                                             # UNDERIVED §9.4.

# =============================================================================================
# RATE BUDGET  (spec §3)
# =============================================================================================
RATE_CAP_HZ = 4.0                            # spec §3.1 — nestor's calls are trade-critical
                                             # and un-deferrable; v5's are deferrable, so the
                                             # deferrable consumer takes the RESIDUAL of the
                                             # ~10 req/s shared limit, not half.
                                             # UNDERIVED §9.1 (the 10 req/s itself).
RATE_BURST_TOKENS = 8.0                      # spec §3.1 — 2 s of burst = one requote round
                                             # trip
RATE_AIMD_DECREASE = 0.5                     # spec §3.2 — multiplicative decrease guarantees
                                             # we yield faster than we take.  DERIVED FORM.
RATE_AIMD_INCREASE = 1.25                    # spec §3.2 / §9.2 — UNDERIVED (convention)
RATE_AIMD_HOLD_S = 60.0                      # spec §3.2
RATE_AIMD_STEP_S = 60.0                      # spec §3.2
# MIRROR (AIMD decrease/yield ↔ AIMD increase/reclaim): silent permanent yielding is
# indistinguishable from a dead bot, so the reclaim end gets a FLOOR and an ALARM.
RATE_MIN_HZ = 0.5                            # spec §4.4 — below it we cannot hold even one
                                             # market
RATE_STARVED_ALERT_S = 600.0                 # spec §4.4 — alert if B < 0.5×cap for 10 min
RATE_STARVED_FRAC = 0.5                      # spec §4.4
RATE_LANE_RESERVE_TOKENS = 1.0               # spec §3.3 — the bucket refuses to fall below 1
                                             # token for ANY lane except exit_cancel: a rate
                                             # budget must never be the reason an order
                                             # cannot be cancelled.
# MIRROR (rate CEILING ↔ rate FLOOR): degrade step 3 sheds markets rather than holding all of
# them badly — the floor is enforced by shedding breadth, not by breaching the ceiling.
# Cancel-lane bound, SF-1.  MIRROR (over-reserving the cancel lane ↔ starving everything
# else): the headroom is a PRIORITY FLOOR, not a partition, so an idle exit lane costs zero;
# the 25% share is what stops a preempting lane becoming a starvation weapon.
CANCEL_SHARE_MAX = 0.25                      # spec §3.3 — one cancel per requote round trip
                                             # (place+cancel+verify+poll ≈ 4 requests).
                                             # UNDERIVED §9.4.
CANCEL_SHARE_WINDOW_S = 60.0                 # spec §3.3 rolling window
CANCEL_SHARE_POISON_BREACHES = 3             # spec §3.3 "3 in 10 min ⇒ poison it"
CANCEL_SHARE_POISON_WINDOW_S = 600.0         # spec §3.3

# =============================================================================================
# VENUE DENY LIST — MEASURED, not assumed (charter: "v3/v4's lessons as MEASURED INPUTS").
# =============================================================================================
# These eight are v4's measured-toxic venues, carried forward as evidence rather than
# re-discovered at v5's expense.  Two families, one mechanism each:
#   * MENTION / event markets (BA, MLB, PYPL, WNBA) — the PayPal geometry itself: fills on
#     contact, accrual ≈ 0, and a close months out so carry runs to the horizon.  (★) refuses
#     these on the numbers; the deny list means we do not pay $16 again to re-derive it.
#   * INDEX HOURLIES (KXINXHUD, KXNDQHUD, KXDXYDUD) and KXRAIN — heavy informed taker flow
#     against a maker who cannot reprice fast enough: we are the fish (P6 inverted).
# MIRROR (denying too MUCH ↔ too little): the deny list is the "too little" guard.  Its own
# mirror — a venue denied that has since become good — is §1.4's REVIVE predicate: a new
# program period AND a 95% T̂-posterior clearing the hurdle.  So this is a prior, not a
# sentence, and nothing here revives on a timer.
DENY_SERIES = {
    "KXRAIN",                      # v4 §7.4 seed deny: measured toxic, 40 markets wide
    "KXINXHUD", "KXNDQHUD",        # index hourlies — informed flow
    "KXDXYDUD",                    # index hourly (Ryan, 2026-07-28)
    "KXMLBMENTION", "KXWNBAMENTION",
    "KXEARNINGSMENTIONBA", "KXEARNINGSMENTIONPYPL",   # the $16 lesson, by name
}


def series_denied(ticker, deny=None):
    """A ticker belongs to a denied series iff the series is its prefix.  Prefix, not equality:
    Kalshi tickers are `SERIES-EVENT-STRIKE`, so the series is what the deny list can name."""
    deny = DENY_SERIES if deny is None else deny
    t = str(ticker or "").upper()
    return any(t == s or t.startswith(s + "-") or t.startswith(s) for s in deny)


LANES = ("exit_cancel", "requote_cancel", "place", "verify", "book_poll", "classify_sweep")
LANE_PRIORITY = {name: i for i, name in enumerate(LANES)}          # spec §3.3, strict order
LANE_NEVER_REFUSED = "exit_cancel"                                  # spec §3.3

# spec §3.4 degrade ladder, ordered by marginal objective cost per request saved
DEGRADE_STEPS = (
    "classify_5hz_to_1hz",       # 1. pinned-ness changes on a 15-min timescale
    "drop_redundant_book_polls",  # 2. WS book fresh AND gate-passed ⇒ strictly redundant
    "drop_lowest_net_markets",    # 3. by construction the smallest objective contribution
    "ws_less_poll_1hz_to_half",   # 4. costs ≤0.5 s of coverage per requote
    "recon_600_to_1800",          # 5. NEVER dropped; it is the truth-reader
)
DEGRADE_NEVER = ("exit_cancel", "t3_close_sweep", "day_stop_flatten", "cash_feed_write")
CLASSIFY_HZ = 5.0                            # spec §3.4 step 1 from
CLASSIFY_HZ_DEGRADED = 1.0                   # spec §3.4 step 1 to
CLASSIFY_REFRESH_S = 900.0                   # v4-proven; pinned-ness timescale
BOOK_POLL_HZ = 1.0                           # spec §3.4 step 4 from
BOOK_POLL_HZ_DEGRADED = 0.5                  # spec §3.4 step 4 to
RECON_POSITIONS_S = 600.0                    # spec §3.4 step 5 from
RECON_POSITIONS_S_DEGRADED = 1800.0          # spec §3.4 step 5 to (never dropped)

# =============================================================================================
# WEBSOCKET  (spec §3.5 — inherit v4's ws_feed.py on its merits, kept VERBATIM)
# =============================================================================================
WS_ENABLED = True
WS_AGREE_REQUIRED = 3                        # v4 §4.6 W2 gate — 3 agreements before a WS book
                                             # may price anything.  MIRROR (trusting WS too
                                             # soon ↔ never trusting it): the 60 s re-proof is
                                             # the first end, per-market REST fallback the
                                             # second; breadth only lifts 6→32 while connected.
WS_VERIFY_INTERVAL_S = 60.0                  # = SAFETY_RESYNC_S; same job, same number
MAX_WS_MARKETS = 32                          # v4-proven breadth while connected
MAX_REST_MARKETS = 6                         # v4 §4.6 clamp when WS-less

# =============================================================================================
# QUOTING  (spec §4)
# =============================================================================================
MAKE_BEFORE_BREAK = True                     # v1 §4.1 strictly dominant when balance exists
# MIRROR (make-before-break ↔ break-before-make): the automatic degrade at T* on ANY
# insufficient-balance reject, plus an `mbb_degraded` ledger row so the degrade is visible.
CANCEL_FIRST_PERIOD_S = 46                   # v1 §4.2 T* = sqrt(2g/a), g=1.2 s, a=1/900/s
MIN_RESTING_LIFE_S = 30                      # v1 §4.4 anti-gaming P1; trigger (a) overrides.
                                             # UNDERIVED §9.5.
SAFETY_RESYNC_S = 60                         # v1 §4.3(e), doubles as the WS re-proof
REFILL_TRIGGER_FRAC = 0.50                   # v1 §4.3(b)
S_MOVE_TRIGGER_FRAC = 0.25                   # v1 §4.3(c)
CLOSE_MARGIN_S = 240                         # v1 §4.7 expiration_ts = close − 4 min.
                                             # MIRROR (window END ↔ window START): the
                                             # pre-positioning lead below.
PREPOSITION_LEAD_H = 0.25                    # v4 window-START guard, kept: never quote a
                                             # program before start_date
COVERAGE_ALERT_FLOOR = 0.90                  # spec §11 Alerts — coverage <90% for 10 min
COVERAGE_ALERT_WINDOW_S = 600
MAX_SHADE_TICKS = 1                          # spec §4.3 "NEVER consider k ≥ 2": score ≤25%
                                             # and those dollars beat it at the water level in
                                             # another venue

# =============================================================================================
# RISK CAPS  (spec §4.4 table)
# =============================================================================================
INV_CAP_USD = 10.00                          # v1 §8.1 per-SLOT net inventory cap
# MIRROR (per-slot inventory cap ↔ per-VENUE): NEW in v5 — no single venue may trip the
# global day stop alone, else one venue halts the whole book, contradicting charter §5.
PER_MARKET_POOL_MULT = 4.0                   # v1 §8.2 never risk 4× a market's own max prize
PER_MARKET_BUDGET_FRAC = 0.25                # v1 §8.2 no single-market concentration
MAX_TOTAL_COLLATERAL_USD = 300.0             # R168 ladder rung.  G5 owns changes to this: one
                                             # constant, one commit, funded by the PREVIOUS
                                             # window's OBSERVED print, never the model.
DAY_STOP_FRAC = 0.35                         # v1 §8.4.  UNDERIVED §9.5.
DAY_STOP_FLOOR_USD = 20.0
DAY_STOP_CAP_USD = 150.0
# MIRROR (day stop for LOSS ↔ day stop for WIN): none needed, and the consideration is the
# record — a large POSITIVE divergence is the settlement/credit path, covered by §5.2's
# pending widening.  MIRROR (day stop ↔ IDLE capital): losing nothing and earning nothing is
# also a failure; `idle_capital` below is its alarm.
IDLE_CAPITAL_COMMITTED_FRAC = 0.50           # spec §4.4 — committed >50% of ceiling while
IDLE_CAPITAL_WINDOW_S = 3600.0               # book-wide net < λ_min/16 for 1 h
LAND_GRAB_MAX_COLLATERAL_FRAC = 0.25         # spec §4.5 / v1 §6.2
LAND_GRAB_MAX_MARKETS = 6
LAND_GRAB_PRICE_C = 1
# --- B9: refill / turnover cap ---
# v1 §8.7 — post-size and refill-cap are DECOUPLED knobs (the v3 lesson).  Beyond 4 turnovers
# of its own inventory cap in one window a slot is a FLOW MAGNET, not a maker.  This is the
# 1 Hz-timescale bound that §2.5's 15-minute kill cadence structurally cannot provide.
REFILL_CAP_TURNOVERS = 4
UNKNOWN_RETRY_S = 300                        # v4 D7 — retry an ST_UNKNOWN cancel on this
UNKNOWN_MAX_RETRIES = 3                      # cadence, then book it FILLED (conservative)
# --- B3: all-time peak / drawdown halt ---
# A DAILY loss limit cannot see a slow bleed — lose 4% a day for ten days and no day trips.
# Drawdown-from-peak is the measure that does.  35% matches DAY_STOP_FRAC deliberately: the
# largest single-day drag we accept is also the largest cumulative one, because a bleed that
# reaches the same magnitude more slowly is not more acceptable for being patient.
MAX_DRAWDOWN_FRAC = 0.35
# --- B4: daily loss limit ---
# Same magnitude as the day stop, computed on REALIZED + unrealized with OPEN-DAY attribution;
# the day stop is the intraday circuit and this is the end-of-day accounting check.
DAILY_LOSS_LIMIT_USD = DAY_STOP_CAP_USD
# --- B6: persist-failure fail-closed ---
# 3 attempts: one covers a transient fsync hiccup, three distinguishes that from a full disk
# without turning a stall into a long stall.  Matches MAX_CONSEC_CANCEL_ANOMALIES' shape.
PERSIST_MAX_RETRIES = 3
# --- B11: capital floor ---
# v5 spending the last dollars is v5 deciding, unilaterally, that nestor does not get to
# trade.  $25 is one nestor position at the observed sizes — the smallest floor that still
# leaves the other bot ABLE to act, which is the property being bought.  UNDERIVED as a
# distribution; derived as "not zero, and at least one action's worth".
CAPITAL_FLOOR_USD = 25.0
# --- B12: clock skew ---
# Our signatures and every `expiration_ts` come from the LOCAL clock.  Kalshi's own auth window
# is on the order of tens of seconds, and CLOSE_MARGIN_S is 240 s, so 30 s is well inside the
# margin that would move an order's effective lifetime.
CLOCK_SKEW_TOL_S = 30.0
MAX_CONSEC_CANCEL_ANOMALIES = 3              # v1 §8.5 poison, v3-inherited, non-negotiable
MAX_POST_ERRORS = 6
POST_ERROR_WINDOW_S = 300

def cap_series_usd(day_stop_threshold_usd, inv_cap_usd=INV_CAP_USD):
    """spec §4.4 row 1, the NEW per-VENUE cap answering the per-slot cap's mirror.

    `cap_series = max(INV_CAP_USD, 0.5 × day_stop_threshold)`.  Derivation is the mirror
    itself: at 0.5× no single venue's inventory can trip the global day stop on its own, so
    one bad venue degrades the book instead of halting it (charter §5 stands DOWN a venue,
    never the bot).  The `max` keeps it from falling below the per-slot cap, which would make
    the venue cap bind before the slot cap and invert the two guards.
    """
    return max(float(inv_cap_usd), 0.5 * float(day_stop_threshold_usd))

# =============================================================================================
# ANTI-GAMING  (spec §4.6 — carried forward VERBATIM from v1 §10.3)
# =============================================================================================
P3_TWO_SIDED_COLLATERAL_MIN = 0.40           # UNDERIVED §9.5
P3_TWO_SIDED_MARKET_MIN = 1.0 / 3.0          # UNDERIVED §9.5
P4_FILL_HONOR_TARGET = 0.95
P4_FILL_HONOR_FLOOR = 0.90
P6_LOOKBACK_DAYS = 5                         # MIRROR (kill for too MANY fills — we are the
                                             # fish ↔ kill for ZERO fills ever — a decorative
                                             # book): §2.5 is the first end, P6 the second.
P7_MAX_REVIVAL_MARKETS = 3
P7_MAX_SIDE_SHARE = 0.90
P7_MAX_SIDE_SHARE_DAYS = 5
P5_CHEAP_SIDE_ALERT = 0.95                   # spec §4.6 — an ALERT, never a block.  P5 stays
P5_CHEAP_SIDE_ALERT_DAYS = 3                 # DELETED and v5 owns that the exposure is LARGER
PROGRAM_EV_LOW_USD = 3400.0                  # spec §4.6 — both numbers print every cycle so
PROGRAM_EV_HIGH_USD = 8000.0                 # the revocation tradeoff is never implicit

# =============================================================================================
# DOSE-RESPONSE  (spec §1.5)
# MIRROR (perturbing costs rate ↔ not perturbing costs knowledge): the budget below is the
# guard on the first end; the exploration floor (§1.4 mirror) is the guard on the second.
# =============================================================================================
DOSE_MULTIPLIERS = (0.5, 1.0, 2.0)           # spec §1.5
DOSE_MIN_SLOTS = 3                           # spec §1.5.  UNDERIVED §9.4 (panel of 3).
DOSE_RATE_BUDGET_FRAC = 0.02                 # spec §1.5 — DERIVED: ALLOCATE's own step is 2%
                                             # of budget, so information bought inside that
                                             # resolution is free

# =============================================================================================
# CASH FEED  (spec §5)
# =============================================================================================
CASH_FEED_SCHEMA = "lip_cash_feed/1"         # spec §5.2
CASH_FEED_HEARTBEAT_S = 30.0                 # spec §5.2/§5.3
CASH_FEED_STALE_S = 120.0                    # spec §5.4 — 4× heartbeat: survives one miss
                                             # plus jitter.  UNDERIVED §9.4.
                                             # MIRROR (stale ↔ absent): an absent file is
                                             # (0,0), correct ONLY if v5 is truly flat, so
                                             # SIGTERM writes a final ZEROED feed after
                                             # cancel-all + shed, and only then may the file
                                             # be removed.
SETTLEMENT_CASH_TIMEOUT_S = 6 * 3600.0       # spec §5.2a — ≈9× the 41-min observed lag.
                                             # NEVER auto-releases; it PAGES.  UNDERIVED §9.4.
                                             # MIRROR (released too EARLY ↔ too LATE): early
                                             # is the halt-nestor direction and is forbidden;
                                             # late only makes v5 look poorer than it is, so
                                             # the timeout pages instead of releasing.
CASH_MODE_SHARED = "shared"                  # spec §5.2
CASH_MODE_SUBACCOUNT = "subaccount"          # spec §5.5
# MIRROR (v5 stops PUBLISHING the feed ↔ nestor stops CONSUMING it): v5 reads nestor's
# LIP_CASH_FEED_ENABLED at startup; mode "shared" with the reader disabled is a STARTUP
# REFUSAL — an unconsumed feed is a silent regression to the hand ledger.
NESTOR_READER_FLAG_ENV = "LIP_CASH_FEED_ENABLED"

# =============================================================================================
# CUTOVER  (spec §6.3 option C, the recommendation)
# MIRROR (adopt too MUCH ↔ adopt too LITTLE): every exchange position NOT adopted is
# enumerated as `orphan_position`, alerted, and its market refused for quoting — v4's
# inventory-slot guarantee, inverted.
# =============================================================================================
ADOPT_BASIS_MIN = 0.01                       # spec §6.3-C.3 — a ledger-era basis of $0.00 or
ADOPT_BASIS_MAX = 0.99                       # $1.50 would make inv_dollar_s, INV_CAP_USD and
ADOPT_BASIS_MARK_MULT = 2.0                  # the cash feed all wrong in the same direction
                                             # at once.  2× mark is the same [0.5,2.0] model
                                             # tolerance the ratchet uses, one-sided.

# --- CUTOVER TRIAGE (see cutover.triage) ---
# Runs ONCE at adoption: every adopted position is re-judged against (★) and the ones that
# fail are exited.  v4's orders are already gone (its SIGTERM cancel-all is the proven path),
# so triage decides only about POSITIONS.
CUTOVER_TRIAGE_ENABLED = True                # the decision + maker-shed path
TAKER_FEE_RATE = 0.07                        # v1 §5.1 F = ceil(0.07·n·p·(1−p)) up to the cent
TAKER_EXIT_MAX_SLIPPAGE_C = 3                # v1 — a MARKETABLE LIMIT, never a market order:
                                             # §8.8 aborts on "a fill at a price we did not
                                             # intend", and only a limit price makes that
                                             # statement checkable BEFORE the order is sent.
                                             # 3c is the observed spread on qualifying rungs.
# GATE G6 (spec §7): the taker-exit is the ONLY code path in this binary able to cross the
# spread, and spec §7 assigns it to Ryan as a separate human gate with its own rollback
# ("flag false").  It therefore ships FALSE.  While it is false the triage still COMPUTES the
# crossing verdict and LOGS THE VALUE FORGONE — the choice is measured rather than asserted —
# and falls back to the maker shed, which is v1 §5.4's strictly-preferred path anyway.
# MIRROR (crossing too eagerly ↔ never being able to leave): this flag guards the first end;
# the second is guarded by the fact that the maker shed is unconditional and never gated, so a
# position is always leaving, only more slowly.
TAKER_EXIT_ENABLED = False
TAKER_EXIT_DECISION = "off_accepted"         # "undecided" | "on" | "off_accepted".  The gate
                                             # is on the DECISION, not the answer: reaching a
                                             # rung having never MADE the choice refuses.

# =============================================================================================
# LEDGER VOCABULARY  (spec §6.2 = v1 §9.1 PLUS the v5 rows)
# =============================================================================================
LEDGER_SCHEMA = "lip_v5_ledger/1"            # schema-mismatch ⇒ ABORT (v1 §9.1, inherited)
V1_LEDGER_KINDS = ("place_req", "place_resp", "cancel_req", "cancel_resp", "fill_obs",
                   "snapshot", "expired", "assume_filled", "assume_filled_clear",
                   "position_divergence", "phantom_risk", "poison", "settlement",
                   "accrual", "coid_seq")
V5_LEDGER_KINDS = ("cash_feed", "rate_yield", "ratchet", "venue_kill", "venue_out_of_reach",
                   "shade_decision", "orphan_position", "adopt_basis_rejected",
                   "mbb_degraded", "rstar_no_converge", "cancel_share_exceeded",
                   "probe_oversized", "venue_rank", "allocate", "idle_capital",
                   "rollback_clean", "presence_collapse", "unit_mismatch")
LEDGER_KINDS = V1_LEDGER_KINDS + V5_LEDGER_KINDS
PRESENCE_KIND = "presence"                   # spec §6.2 N2 — its OWN file, never the ledger
FILLS_REQUERY_DELAY_S = 36                   # v1 §9.4a — 3× the ~12 s worst observed lag
CRASH_GAP_LOOKBACK_S = 60                    # v1 §9.4(4)

# =============================================================================================
# ALERTS  (spec §11) — every one of these is detect-and-page, never silent.
# NTFY_DISABLE is honored BY CONSTRUCTION in runtime.ntfy(); see also the fixture-ticker
# suppressor, because the suite fired a real push to a phone twice this week.
# =============================================================================================
ALERTS = (
    "halt", "poison", "day_stop", "assume_filled", "venue_stand_down", "presence_collapse",
    "lip_cash_feed_stale", "settlement_cash_unconfirmed", "orphan_position",
    "adopt_basis_rejected", "rate_starved", "cancel_share_exceeded", "idle_capital",
    "rstar_no_converge", "coverage_low", "credits_ritual_due", "venue_out_of_reach",
    "probe_oversized", "unit_mismatch", "ws_degraded",
)
