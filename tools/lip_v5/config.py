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
# Charter D — the G3 operator gate artifact.  `--live` alone NEVER starts a quoting loop; the
# operator writes this file BY HAND (README step 4) and the binary verifies it before arming.
# MIRROR (starting without a human ↔ a human unable to start it): the artifact is the first
# guard; the README's exact `cat > v5_go.json` recipe is the second's answer — the gate is one
# hand-typed file, not a ceremony that can rot.
GO_ARTIFACT_NAME = "v5_go.json"
GO_ARTIFACT_PATH = os.path.join(DATA_DIR, GO_ARTIFACT_NAME)
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
# THE HURDLE, RE-DERIVED FROM MEASUREMENT (2026-07-28).
# v1 set λ_min = 0.10 per 16 h ⇒ 0.00625 $/h per collateral-$, i.e. 0.625%/HOUR, and flagged
# it UNDERIVED.  It is not merely underived, it is WRONG BY A FACTOR OF ~2 AGAINST OUR OWN
# TAPE: the measured, actually-credited reward rate on deployed capital is ~0.36%/h
# (work/audit-2026-07-28.md, the $7.482 payout).  A hurdle set ABOVE the rate the program has
# ever paid refuses every venue that pays exactly what this program pays — measured live, the
# book deployed $5.84 of a $300 ceiling with ZERO guard refusals, because only the very
# thinnest books cleared the bar and a thin book saturates after a dollar.
#
# WHAT THE FLOOR ACTUALLY IS: the opportunity cost of the marginal dollar — "worth nothing
# over the alternative use of the dollar" (money.admits).  The alternative is idle cash at
# 3.25% APY ≈ 0.0000037 $/h per dollar.  A floor 100x above that is still a strict filter and
# is three orders of magnitude below the aspirational number it replaces.
#
# MIRROR (floor too LOW ↔ too high): too low admits marginal venues and spreads capital thin —
# bounded, because (★) still RANKS by net rate so the best venues fill first, the per-cluster
# and per-rung caps bound each one, and the ratchet withholds size until accrual is verified.
# Too high is what we measured: a book that cannot deploy, cannot earn, and cannot learn.
# When capital becomes genuinely scarce the binding constraint is r* (the achieved marginal
# rate), which rises on its own as the book fills — that is the mechanism that should ration
# capital, not a constant.
LAMBDA_MIN = 0.10                            # v1 §2.3, UNDERIVED §9.5 (inherited)
LAMBDA_MIN_WINDOW_HOURS = 16.0
FLOOR_RATE_PER_H = LAMBDA_MIN / LAMBDA_MIN_WINDOW_HOURS      # = 0.00625 /h, spec §0.4
# ...but that number is the REFERENCE RATE the kill hysteresis, the rescue and the presence
# maths are all calibrated against, and it is SEPARATELY wrong as an ADMISSION hurdle, so the
# two uses are now separate constants.  ADMIT_FLOOR is what `money.admits` compares against.
# 0.002 $/h per collateral-$ = 0.2 %/h, i.e. HALF the measured achieved rate of ~0.36 %/h
# (work/audit-2026-07-28.md).  Below the rate the program actually pays, so venues paying
# what this program pays are admitted; far enough above zero that the water level still stops
# at genuine diminishing returns — on a book with no rivals our share is already ~100% and
# extra contracts buy fill risk, not score (test_a_rung_where_share_saturates...).
# The old 0.00625 was ~2x ABOVE the measured rate: it refused everything that really pays.
ADMIT_FLOOR_RATE_PER_H = 0.002
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
# SECOND CHARTER AMENDMENT (Ryan) — the forfeit CLIFF.  Payout is $0 below $1.00, so accrual
# under the cliff is CONDITIONAL, not banked: at $0.70 accrued, the next $0.30 of accrual is
# worth $1.00+ (it unlocks the stranded 70¢), never $0.30.  RESCUE_TARGET = $1.00 boundary
# + 1c round-down + 9c buffer (v1 §3.3, prod-proven).  Today's tape: $4.82 estimated across
# 16 programs, only $2.38 above the cliffs; $0.87 and $0.83 forfeiting for want of this term.
# MIRROR (rescuing dead accrual ↔ forfeiting live accrual): `alloc.rescue` prices BOTH ends —
# top-up only when recovery beats redeploy + fill cost, abandon when the cliff is unreachable
# under the (derived) rung cap.
RESCUE_TARGET_USD = 1.10
# Accrual persistence cadence: one `accrual` money row per program per minute, so a crash
# loses ≤60 s of accrued-value memory — the same bound the presence rows carry, because the
# cliff decision is only as good as the A it remembers.
ACCRUAL_WRITE_S = 60.0
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
# GENERALIZED 2026-07-28 (Ryan's call, with the evidence).  §1.4 sized these for an
# UNVERIFIED MECHANISM: probe one venue, wait a settlement, believe the credit, scale.  That
# is the right shape when you do not know whether presence pays at all.  It is the wrong shape
# now: the mechanism is VERIFIED BY RECEIPT — $7.482 credited with per-rung line items, and
# Kalshi's own documentation describes snapshot scoring on resting size with no trade
# condition.  What remains unverified per venue is only whether OUR share holds there, which
# the accrual meter measures continuously and which the forfeit gate prices every cycle.
# Measured cost of the old sizing: 8 venues admitted at $1.33-$2.17 each = ~$16 deployable of
# a $300 ceiling, and a 34-day program window that cannot absorb one-venue-per-settlement
# sequencing.
# WHAT STILL BOUNDS THE RISK, unchanged: the per-cluster worst-case cap (1/6 of ceiling per
# settle source), the per-rung cap, the day stop, the 35% drawdown halt, the turnover cap and
# the §2.5 toxicity kill.  This layer was the one that rationed by IGNORANCE; the others
# ration by RISK, and they are the ones that were paid for in losses.
UNVERIFIED_EXPOSURE_FRAC = 1.00              # the ceiling itself; risk is bounded by the caps
N_UNVERIFIED_MAX = 40                        # breadth is the strategy (note 43 §7)
# The opening size measures SHARE rather than asking whether rewards exist at all, so rung 0
# is seeded at a MULTIPLE of the floor-clearing size instead of at the bare floor.  4x keeps
# an opening probe inside the per-rung cap while making it large enough that its accrual is
# distinguishable from the $1 forfeit cliff — a probe that can only just clear the floor
# cannot tell "this venue pays" from "we barely qualified".
RUNG0_FLOOR_MULT = 4.0
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
# T̂ PRIOR.  T̂ is the fraction of a committed dollar-hour that survives AT BEST — 1.0 means
# presence is never interrupted, 0 means we are filled the moment we quote.
#
# The old note here said 0.5 "affects probe ORDER only, never exposure, because rung-0 caps
# bind first".  That was true while rung 0 was a $2 probe.  It is FALSE now: since the ratchet
# was generalized, T̂ multiplies gross inside (★) and therefore gates ADMISSION — measured
# live, only ~4% of 368 slots cleared the hurdle and the book deployed $5.80 of $300.  An
# underived constant quietly became load-bearing in a role it was never derived for.
#
# 0.5 describes a BUSY venue: half our capital-time eaten before we have a single observation.
# The median Kalshi market is the opposite — thin, rarely traded, which is exactly why
# presence pays there at all (note 43 §4: the absent market-maker is the fish, and presence
# itself is the product).  0.8 is the prior for "mostly undisturbed, some interruption",
# still strictly below the 1.0 a truly dead book would show.
# This is a PRIOR only: `t_hat_shrunk` moves to the measured value at a 2 $·h pseudo-weight,
# so one venue-hour of real tape overrides it.  Still UNDERIVED as a distribution — the honest
# fix is to measure T̂ per venue class from our own presence log and set this to the median.
SHRINK_PRIOR_DEFAULT = 0.8
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


# DENY BY FAMILY, NOT BY THE TICKERS THAT HAPPENED TO BURN US.  PYPL and BA cost $16 and
# went on the list by name; MLB and WNBA followed; then KXEARNINGSMENTIONF turned up in a live
# book purely because nobody had lost money on Ford yet.  Every one of these shares ONE
# mechanism (note 43 §4): a mention market resolves off a news event, so informed flow takes
# the quote the instant the news moves, accrual is ~0, and the position outlives the reward
# window.  A list that enumerates victims re-learns the same lesson at full price for every
# new ticker; a list that names the MECHANISM refuses the whole family at once.
# "MENTION": the news-event family (PYPL, BA, MLB, WNBA, F) — informed flow takes the quote
#   the instant the news moves; see the block above.
# "KXRAIN":  v4's measured-toxic rain family, 40 markets wide.  It was on DENY_SERIES as a
#   PREFIX, and tightening prefix matching (so KXRAINBOW would not be caught) made it too
#   strict: the real series are KXRAINAUSM / KXRAINHOUM / … and stopped matching entirely, so
#   rain came back into a live book on 2026-07-28.  A family that was banned by measurement
#   must not be un-banned by a string-matching refinement.
DENY_SUBSTRINGS = ("MENTION", "KXRAIN")


def family_denied(ticker):
    t = str(ticker or "").upper()
    return any(sub in t for sub in DENY_SUBSTRINGS)


def series_denied(ticker, deny=None):
    """A ticker belongs to a denied series iff the series IS the ticker or is its
    dash-delimited prefix.  Kalshi tickers are `SERIES-EVENT-STRIKE`, so `KXRAIN-...` is
    denied by "KXRAIN" — but `KXRAINBOW-...` is NOT: the earlier bare-`startswith` clause
    denied every series that merely SHARED A SPELLING with a toxic one, an overbroad match
    that silently widened the deny list beyond what its evidence supports (finish-round
    charter item D).  MIRROR (denying too much ↔ too little): the dash-anchored match is the
    "too much" fix; the deny list itself remains the "too little" guard."""
    deny = DENY_SERIES if deny is None else deny
    t = str(ticker or "").upper()
    if family_denied(t):
        return True
    return any(t == s or t.startswith(s + "-") for s in deny)


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
CLASSIFY_MAX_MARKETS = 200                   # bound the cold-start sweep.  ρ DOES rank ACROSS
                                             # events (different pools); it only fails WITHIN
                                             # one, which is why the rank that matters is
                                             # computed AFTER classification, not before.
# Programs re-price on a multi-day timescale (modal window 228 h), so the feed is the most
# deferrable read we make — hence the lowest lane and the slowest cadence.  15 min is 1% of the
# modal window: fast enough that a new program is quotable within one classify cycle of listing,
# slow enough that the scan never competes with quoting.
SCAN_REFRESH_S = 900.0
SCAN_PAGE_LIMIT = 1000                       # cursor-paged
SCAN_MAX_PAGES = 200                         # ~120 pages observed at limit=1000; bound the pull
ENTRY_SHARE_ASSUMPTION = 0.5                 # the runway guard's CONSERVATIVE share.  Assuming
                                             # we take the whole side is exactly the optimism
                                             # that produced 735 lots posted with 25 min left.
# --- the outer loop ---
# 1 Hz: the quoting loop's own cadence (v1 §4.3 evaluates requote triggers at 1 Hz), and the
# metering tick's fixed phase is 1 Hz by construction (§2.1), so one cycle per second keeps the
# two in step without the sampler ever being triggered by our own action.
CYCLE_HZ = 1.0
# A cycle that overruns its budget is a cycle whose telemetry lies about cadence; log it rather
# than silently drifting.  100 ms is 10% of the period.
CYCLE_OVERRUN_WARN_S = 0.100
# SF-3 — the HALTED-IDLE cadence.  A halted process has exactly two remaining duties: keep the
# cash-feed heartbeat fresh (a halted-but-alive v5 still holds inventory, and a stale feed
# pages nestor's operator about a process that is fine) and notice SIGTERM.  The heartbeat's
# own cadence (30 s) is therefore the loop's cadence — spinning faster does no work, and
# sleeping longer than 120 s would trip the staleness page.  30 s leaves a 4x margin.
# MIRROR (halted loop spinning ↔ halted loop dead): the idle cadence is the spin guard; the
# heartbeat it publishes is what distinguishes "halted, alive" from "dead" on nestor's side.
HALTED_IDLE_S = 30.0
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
DAY_STOP_FRAC = 0.35                         # v1 §8.4.  UNDERIVED §9.5.
DAY_STOP_FLOOR_USD = 20.0
DAY_STOP_CAP_USD = 150.0

# --- the PER-RUNG size bound, DERIVED (charter amendment, Ryan, finish round) ---
# The flat $10 `INV_CAP_USD` was INHERITED, NOT DERIVED — it predates knowing pools run
# ~$100/rung, and it refused $50 on rungs whose reward supported it.  Per-rung size now
# derives from its two real bounds:
#   (a) REWARD: (★)'s own share saturation — marginal gross ∝ S/(q+S)², so the water level
#       stops adding to a rung as we come to own its book.  No constant needed; a rung where
#       $10 saturates share stays small BY ARITHMETIC (test: amendment T2).
#   (b) RISK: no single rung's worst case may trip the global day stop alone — the SAME 0.5×
#       factor, for the same charter-§5 reason, as the cluster and per-series caps (one rung
#       halting the whole book stands a venue down the hard way).
#           slot_cap = max( 0.5 × DAY_STOP_FLOOR , 0.5 × day_stop ) ∈ [$10, $75]
#       $50/rung becomes reachable exactly when the funded day stop is ≥ $100 — "statistically
#       safe" is priced by the same instrument that bounds the loss.  The blast radius scales
#       WITH the cap: the B9 turnover bound is 4 × n_cap (proportional) and the §2.5 kill
#       cadence is size-independent, so a $50 rung is bounded the same way a $10 one was —
#       Ryan's complaint (1), informed takers, is answered by d/PSDH/turnover, never by
#       starving the rung.
# MIRROR (cap too LOOSE ↔ too TIGHT): loose is bounded by the day stop it derives from and by
# the cluster cap at place(); tight is the old defect — refusing reward-supported size — and
# is what this derivation removes.
# INV_CAP_USD survives ONLY as the FLOOR, and the floor itself is now derived:
# 0.5 × DAY_STOP_FLOOR_USD = $10, i.e. the slot cap at the smallest fundable day.
INV_CAP_USD = 0.5 * DAY_STOP_FLOOR_USD       # = $10 — the slot cap's FLOOR, not the cap


def slot_cap_usd(day_stop_threshold_usd, floor_usd=None):
    """The derived per-rung collateral cap: `max(floor, 0.5 × day_stop)`.  Same shape as
    `cap_series_usd`/`cluster_cap_usd` at the same factor, one level finer.

    ── NEW-1: `slot_cap_usd == cluster_cap_usd` IS DELIBERATE.  THE DERIVATION. ──────────
    The re-verify established the identity exactly (day stops $20/$40/$100/$150/$300) and
    asked whether the two caps should be broken apart at a different factor.  They should
    not, and the reasoning is worth writing down because the identity LOOKS like a defect:

    R1 NESTING.  A finer cap may never EXCEED the coarser one it sits inside, or the coarser
       is the only one that can ever bind and the finer is decoration — `cap_series_usd`'s
       own `max` names that inversion as the thing to avoid.  So `slot_cap ≤ cluster_cap`.
       That is the binding formal requirement; everything below is about where inside it.

    R2 ONLY ONE OF THE TWO HAS AN INDEPENDENT DERIVATION.  Both were derived from the same
       sentence — "no single X may trip the global day stop on its own" ⇒ cap ≤ 0.5 × day
       stop.  But that sentence is about the UNIT OF RISK, and clusters.py's whole thesis is
       that the unit of risk is the CLUSTER, not the rung: fifteen rungs of a ladder are not
       fifteen bets, they are one bet expressed fifteen times.  So the day stop binds the
       cluster at 0.5×, and the rung inherits the bound TRANSITIVELY — a rung inside a capped
       cluster already cannot trip the stop, because the whole cluster cannot.  The slot
       cap's own clause (b) above is therefore SUBSUMED, not independent.

    R3 NOTHING BINDS THE RUNG STRICTLY TIGHTER.  The candidate is intra-cluster
       diversification ("don't put the cluster's whole budget in one rung").  It does not
       yield a constant, for three reasons:
         (i)  the rungs of a threshold ladder NET, and `clusters.worst_case_loss_usd`
              computes that netting EXACTLY.  Four rungs at K/4 and one rung at K do not
              carry the same worst case, and the measure already prices the difference; a
              flat 1/R factor would substitute a guess for a measurement that is exact.
         (ii) R (rungs per ladder) is a property of the VENUE, not of the risk.  Any constant
              fraction is either an UNDERIVED default — a bug that has not fired — or it is
              R-dependent and therefore not a constant at all.
         (iii)the objective ALREADY spreads: marginal gross ∝ S/(q+S)², so a rung saturates
              its own share and the next dollar prefers a different rung BY ARITHMETIC
              (clause (a) above).  Spreading the objective produces needs no cap to enforce.

    R4 THE DEADLOCK WAS NOT THE EQUALITY.  Under the identity a slot at its cap saturates its
       cluster, which made make-before-break's transient double-count a CERTAIN refusal
       rather than an occasional one — so the identity set the blast radius, but the defect
       was measuring a REPLACEMENT as an ADDITION (fixed in `engine.place_context`).  A cap
       whose correctness depends on a measurement error staying small is not a cap; fix the
       measurement.  And note that the alternative instrument is degenerate here: reserving
       one slot's collateral inside the cluster cap — the analogue of `reserve_budget` at the
       ceiling — reserves the ENTIRE cap when slot_cap == cluster_cap, funding nothing.  That
       degeneracy is itself the proof that the exemption is the right instrument.

    CONCLUSION: keep the identity.  `slot_cap ≤ cluster_cap` is the requirement; equality is
    the tightest legal choice given that only the cluster bound is independently derived.
    `test_newround.TestTheGuardHierarchy` asserts the ordering so a future change to either
    function cannot silently invert it.

    WHAT IT COSTS, MEASURED, POST-FIX: a 4-rung single-cluster gas ladder at a $20 day stop
    plans $10 across its rungs, not the $34.56 across four that place() then refused.  That
    is the CLUSTER cap being right, not the slot cap being wrong — all four rungs are one bet
    on gas, and $10 is what a $20 day stop permits one bet.  The charter amendment's "$50 per
    rung becomes reachable at a day stop ≥ $100" still holds and now reads "$50 per CLUSTER",
    which is what the risk statement always said.
    """
    f = INV_CAP_USD if floor_usd is None else float(floor_usd)
    return max(f, 0.5 * float(day_stop_threshold_usd))


PER_MARKET_POOL_MULT = 4.0                   # v1 §8.2 never risk 4× a market's own max prize
PER_MARKET_BUDGET_FRAC = 0.25                # v1 §8.2 no single-market concentration
MAX_TOTAL_COLLATERAL_USD = 300.0             # R168 ladder rung.  G5 owns changes to this: one
                                             # constant, one commit, funded by the PREVIOUS
                                             # window's OBSERVED print, never the model.
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
# without turning a stall into a long stall.  Matches # --- B14: PLACEMENT-RATE CIRCUIT BREAKER (the 130-order loop, 2026-07-28) ---
# A slot may legitimately re-place only when a requote trigger fires, and MIN_RESTING_LIFE_S
# (30 s) bounds that at 2 per minute; 3 allows one make-before-break transient on top.  Beyond
# that the loop is not requoting, it is FAILING TO SEE ITS OWN ORDERS — which is what happened
# when a successful placement parsed as a rejection: 130 identical orders in 130 seconds.
# This is a HALT, not a refusal.  A refusal would leave the real defect (our books disagreeing
# with the wire) running silently against every other slot; the disagreement itself is the
# emergency, and a human must look.  MIRROR (breaker too tight ↔ too loose): too tight halts a
# legitimately busy slot — bounded, visible, and resumable by operator record; too loose is an
# unbounded order count on a live account, which is what this cost us.
PLACE_BURST_MAX = 3
PLACE_BURST_WINDOW_S = 60.0
# ...matching MAX_CONSEC_CANCEL_ANOMALIES' shape.
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
# P6 IS ADVISORY, NOT A REFUSAL — VERIFIED AGAINST KALSHI'S OWN DOCUMENTATION (2026-07-28).
# The help centre states the liquidity program pays "for maintaining orders on the books that
# help other traders get better prices, EVEN IF YOUR ORDERS DON'T GET FILLED", and scoring is
# snapshot-based on resting size and share of qualifying liquidity with two-sided depth rules.
# There is NO trade or volume condition (that is the separate VOLUME incentive program).  So
# "nobody trades here" is not evidence that presence buys nothing — it is evidence of an
# UNCONTESTED venue, which is the cheapest presence on the board (note 43 §6).
# Measured at G2: with P6 refusing, 200 classified markets produced ZERO slots — v5 would have
# quoted nothing at all, because every empty book is by construction untraded.
# MIRROR (admitting a dead venue ↔ refusing a quiet good one): the refusal end was costing us
# the entire long tail; the admission end is bounded by the forfeit gate (a venue that pays
# nothing cannot clear the $1 cliff and is dropped) and by the verified-accrual ratchet, which
# needs a CREDITED reward before it raises any cap.  Both are evidence-driven, so a venue that
# genuinely pays nothing is refused by measurement rather than by assumption.
P6_ADVISORY = True
P6_LOOKBACK_DAYS = 5                         # MIRROR (kill for too MANY fills — we are the
                                             # fish ↔ kill for ZERO fills ever — a decorative
                                             # book): §2.5 is the first end, P6 the second.
# P6 re-check cadence: 1/20 of the lookback window.  A market with zero public trades over 5
# DAYS does not become tradeable inside minutes, so re-asking every classify pass (15 min)
# buys nothing; at 6 h the admission latency for a venue that just came alive is bounded at
# 1/20 of the evidence window that judged it dead.  The FIRST check is immediate (at first
# classify), so a newly listed market with trades admits without waiting.
P6_RECHECK_S = P6_LOOKBACK_DAYS * 86400 / 20.0               # = 6 h
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
                   "rollback_clean", "presence_collapse", "unit_mismatch",
                   # BLOCKER-2: adoption is a MONEY event and must survive restart via the
                   # same replay path as every other money event — one `adopt` row per
                   # adopted (ticker, side).  Replay rebuilds the position from it, and its
                   # presence is what makes a second adoption a SKIP instead of a double.
                   "adopt")
LEDGER_KINDS = V1_LEDGER_KINDS + V5_LEDGER_KINDS
PRESENCE_KIND = "presence"                   # spec §6.2 N2 — its OWN file, never the ledger
FILLS_REQUERY_DELAY_S = 36                   # v1 §9.4a — 3× the ~12 s worst observed lag
CRASH_GAP_LOOKBACK_S = 60                    # v1 §9.4(4)
# FINAL FIX ROUND (BLOCKER-1): the live fills poll, on the verify lane.  Derivation of 15 s:
# half the MINIMUM RESTING LIFE (30 s) — the Nyquist bound on the shortest presence
# commitment, so a fill is OBSERVED within one half-life and the replenish decision happens
# inside the same resting period; also 1/4 of the 60 s drift-measurement horizon, so the
# d-sample's mark can still be read near the fill.  Cost: 1/15 req/s = 1.7% of the 4 Hz
# budget.  MIRROR (polling too fast ↔ too slow): fast burns the shared budget the residual
# doctrine protects; slow is the reviewer's proven failure — 630 cycles, 0 fills calls, a
# taker-filled market frozen as a position_divergence at t+601 s.
FILLS_POLL_S = 15.0
# SF-4: the operator's venue-reading entry point — a WATCHED FILE, mirror of v5_go.json's
# hand-written pattern: the credits ritual appends rows, the live process consumes them.
# Rows: {"venue","reading_usd","projection_usd","settlement_day"?,"program_id"?,"paid"?}.
READINGS_NAME = "v5_readings.jsonl"
READINGS_PATH = os.path.join(DATA_DIR, READINGS_NAME)
# SF-3: the halted closing pass posts fully-closing sheds so a halted book can LEAVE.  When
# the market's close is UNKNOWN (halt before the classify sweep learned it) the expiration
# backstop cannot discharge its real job, so the order's life is bounded by the halt's own
# human-review scale instead: one hour, re-posted each halted-idle pass while the position
# remains.  UNDERIVED as a distribution; derived as "bounded, and long enough to rest".
HALTED_SHED_TTL_S = 3600.0

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
