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
# THE PAYOUT HORIZON.  (★) maximises RATE and is blind to WHEN a window closes, so it cannot
# tell "earn $16 by Saturday" from "earn $16 by tomorrow" — and it kept buying weeklies.
# Measured 2026-07-28 from Ryan's own popovers: a treasury DAILY earned $0.44 on a $4.99 order
# while a Trump WEEKLY earned $0.10 on a $30 order — ~40x the return per capital-hour, purely
# because a daily packs its pool into ~20 h (rho ≈ $5/h) where a weekly spreads $100 over
# 86-166 h (rho ≈ $1/h).
#
# The economics the rate misses: capital committed to a 166-hour program is capital that
# CANNOT be redeployed into the six dailies that open in the meantime.  So accrual beyond the
# horizon is not worth its face value to us — it is worth what the same dollars would earn
# recycling through nearer windows.  Projecting only the accrual that lands INSIDE the horizon
# is the conservative form of that: a weekly is judged on the day of it we can actually
# collect, a daily is unaffected.
#
# 24 h is the redeployment cycle: it is how often a fresh set of dailies opens, and it is the
# interval at which we can act on a credit.  MIRROR (horizon too SHORT ↔ too long): too short
# refuses genuinely good multi-day venues (bounded — they are still admitted, just projected
# on one day's worth); too long is what we measured, a book full of week-long accrual while
# the operator needed money tomorrow.
PAYOUT_HORIZON_H = 24.0
# ...and a program whose WINDOW is far longer than the horizon is refused outright, not merely
# sized down.  Measured per capital-dollar per DAY: a treasury daily returned ~$12/day on
# $4.99 while a Trump weekly returned ~$3.6/day on $16.68 — ~11x.  A weekly is not unprofitable,
# it is CAPITAL-INEFFICIENT, and with a $300 ceiling every dollar parked in one is a dollar not
# recycling through dailies.  2x the horizon keeps 2-day programs (still bankable inside the
# decision cycle) and refuses the 86-166 h weeklies.
# MIRROR (refusing long windows ↔ refusing all breadth): breadth comes from MORE DAILIES across
# more clusters, not from longer windows; the diversity ranking already finds them.  If dailies
# ever run out this is the first constant to relax, and the `window_too_long` count says so.
#
# ── SUPERSEDED 2026-07-29 night (note 52 D4/§6): NO CALL SITES, kept as the derivation
# record.  The window filter was the correlation disaster's mechanism — at 2x24h the eligible
# universe was ELEVEN clusters — and its premise ("a treasury daily returns ~11-40x a weekly
# per capital-hour") is NOT SUPPORTED by the live feed: gas is 6.26 $/h on a 16h window, two
# 166h programs are 6.03 $/h.  The variation is POOL SIZE, not window length; dailies looked
# special because dailies were all this filter let us see.  The horizon that actually matters
# is the market's SETTLEMENT date — see SETTLE_HORIZON_H.
MAX_WINDOW_MULT = 2.0
# DAILIES ONLY — DERIVED, STAGED-INERT, NOT SWITCHED ON (2026-07-28).  The argument for 1.0:
# 2.0 was derived when the only cost of a long window was capital EFFICIENCY ("2x the horizon
# keeps 2-day programs, still bankable inside the decision cycle").  Deriving the LOSING path
# makes the horizon a SOLVENCY term instead — settlement is the guaranteed close of an un-netted
# position (note 43 §1, "settlement is the only truth"), so the program window is the worst-case
# time-to-exit for every contract we buy, and at 48 h a position nobody takes rents its
# collateral for two days with no other terminal event.  The efficiency evidence points the same
# way: a $100 daily packs its pool into ~20 h (rho ≈ $5/h) where a $345 monthly spreads it over
# ~730 h (rho ≈ $0.47/h) — ~13x per capital-hour, the same statement the treasury-daily vs
# Trump-weekly measurement above makes at a shorter tenor (~11x per capital-day).
# WHY IT IS NOT LIVE: tonight's backtest covered the quoting rules, not the window filter, so
# switching it on would put an UNMEASURED change on the money path in the same commit that
# reverts two MEASURED-BAD ones.  It is one constant, one commit, whenever the coordinator wants
# it — and the `window_too_long` count is already the instrument that says whether dailies alone
# can fill the book.
DAILIES_ONLY_WINDOW_MULT = 1.0               # staged-inert: assign to MAX_WINDOW_MULT to arm
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
# ── THE SETTLEMENT HORIZON (note 52 D4, settled with Ryan 2026-07-29 night). ────────────────
# THERE ARE TWO CLOCKS AND THE OLD FILTER READ THE WRONG ONE.  The program WINDOW is how long
# we can earn; the market SETTLEMENT is how long a fill traps the money — and they are only
# weakly coupled (KXGDPYEAR-32 carries a 123.9h program on a market settling in 2032).  The
# old `MAX_WINDOW_MULT` filter bounded the program window at 48h, and MEASURED 2026-07-29 that
# left ELEVEN eligible clusters (TWO at 24h: gas and the treasury curve) — the mechanism of
# the -$195 correlation night.  We did not fail to diversify; the filter deleted the board.
# The entry gate is therefore on the MARKET's close: settle within 168h or we do not enter.
# 168h is where measured supply supports the book: 54 clusters settle inside 7 days, ~38 can
# clear $1.00 at half presence — against N_TARGET_CLUSTERS = 30 (note 52 §3b).  A fill in
# this universe is capital committed for AT MOST a week, and the program pays THROUGH the
# hold, which is what makes the hold rentable at all.
# MIRROR (horizon too SHORT ↔ too long): too short is the old filter — a two-cluster book;
# too long readmits the PYPL geometry (inventory that outlives its subsidy by months).  An
# UNKNOWN close refuses entry — the markets this gate exists for are exactly the ones whose
# close is far from their program, so "unknown" may not default to admit (held is exempt; a
# market we are inside is not asking an entry question).
SETTLE_HORIZON_H = 168.0
HORIZON_GRACE_H = SETTLE_HORIZON_H           # spec §1.2 hard horizon exclusion.  Was 24h
                                             # ("same-day-after settlement"), which excluded
                                             # most of the ≤7d universe the settlement gate
                                             # deliberately admits: any market settling more
                                             # than a day after its program ended was refused
                                             # even though the gate bounds the carry at 168h
                                             # and (★)'s carry term PRICES the gap.  The
                                             # exclusion survives as the backstop BEHIND the
                                             # settlement gate, at the same 168h boundary.
HORIZON_EXEMPT_RUNG = 2                      # spec §1.2 "unless ratchet rung ≥ 2"

# =============================================================================================
# VERIFIED-ACCRUAL RATCHET  (spec §1.4)
# MIRROR (ratchet up ↔ ratchet down ↔ revive): up-1/down-2 is the down end; the revive end is
# REVIVE_REQUIRES_NEW_PERIOD + the T̂-posterior predicate.  Nothing revives on a timer.
# =============================================================================================
# ── RECONCILED 2026-07-29 night (note 52 D7).  v1 §3.1 set this at $2.00 ("2x the cliff");
# the sizing rule sets the credit target at CREDIT_TARGET_USD x CREDIT_TARGET_MARGIN = $1.50.
# TWO different targets for one floor make the cliff pass zero the very rung the lot funds —
# the lot clears $1.50 and the cliff pass demands $2.00, so every correctly-sized rung reads
# sub-cliff and is dropped.  One number, and the sizing rule's is the derived one (margin 1.5
# = half a floor of headroom, note 52 D7).  `test_config` asserts the identity.
ENTRY_FLOOR_USD = 1.50                       # == CREDIT_TARGET_USD x CREDIT_TARGET_MARGIN.
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
# ── CORRECTED 2026-07-29 night (note 52 D6).  Under the presence-reserve stack the per-order
# bound IS the lot ($2.50), so a 4x probe exceeds its own container and `rung0_cap` reads
# every venue UNPROBEABLE — the whole book refuses to open.  The distinguishability the 4x
# bought is already carried by CREDIT_TARGET_MARGIN: the lot targets $1.50 against a $1.00
# cliff, so a credited lot is 50% above the forfeit boundary — measurable, not marginal.
RUNG0_FLOOR_MULT = 1.0
# How many cliff-sized rungs one venue may hold.  A venue is a SERIES and a series carries a
# ladder of strikes, each strike its own pool with its own $1 cliff — so the earning shape is
# several rungs per venue, not one.  4 keeps a venue's budget meaningfully below the cluster
# cap (which bounds the correlated group) while letting the ladder actually be occupied.
RUNGS_PER_VENUE = 4.0
# ── CORRECTED 2026-07-29 night.  THIS 2% WAS THE REAL BREADTH LIMIT, AND IT WAS INVISIBLE. ───
# MEASURED, end to end: 40 distinct venues offered, 40 classified, 80 slots built — and TWO
# ORDERS RESTING, $60 of a $300 ceiling.  Not the poll breadth, not the caps, not (★).  This.
# HOW.  `ratchet.classify_probe` calls a venue OVERSIZED when its floor-clearing size exceeds
# `OVERSIZED_PROBE_FRAC × ceiling` = $6 at $300, and `admit` allows only `OVERSIZED_PROBE_MAX = 2`
# oversized probes concurrently.  The strategy plans ~$10 per rung (a $300 ceiling across ~30
# settle sources), so EVERY planned rung is "oversized" by this threshold and venues 3..40 are
# refused.  Breadth then grows two per verification cycle, and verification needs a credit
# receipt — i.e. ~15 days to reach thirty venues.
# WHY IT HID.  The two constants beside it were already loosened FOR breadth
# (`UNVERIFIED_EXPOSURE_FRAC = 1.00`, `N_UNVERIFIED_MAX = 40`, "breadth is the strategy"), which
# made this one binding without anyone editing it.  A threshold that was calibrated as an
# exception became the rule when the rule around it changed.
# THE DERIVATION.  A classification threshold for "unusually large probe" must sit ABOVE the
# size the plan normally asks for, or it classifies normal as unusual.  The largest a single
# market may be is `MARKET_CAP_FRAC`, and `rung0_cap` already refuses anything past it — so a
# probe inside the per-market cap is by construction NOT unusual, and one that exceeds it is
# already impossible.  Tying the two together makes "oversized" mean what it says.
# WHAT STILL BOUNDS RISK, unchanged: `N_UNVERIFIED_MAX`, the per-leg cap, the cluster cap, the
# tracked portfolio variance, the day stop, the drawdown halt and the §2.5 toxicity kill.  This
# layer rationed by IGNORANCE — its own comment above says so — and the others ration by RISK.
# Written as a literal because `MARKET_CAP_FRAC` is defined further down this file; the two are
# ONE number by intent and `test_config` asserts the identity so they cannot silently diverge.
OVERSIZED_PROBE_FRAC = 0.10                  # == MARKET_CAP_FRAC.  A CLASSIFICATION
                                             # threshold, not a cap (spec §9.3)
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
# ── WIDENED 2026-07-29 night (note 52 D4's cost).  The candidates rank is by ρ across ALL
# live programs, and most of the top of that rank settles FAR (measured: of the top 40
# clusters by ρ, 24 settle >30 days out) — each far ticker costs one close-learn before the
# request-free pre-filter can drop it, so the budget that reaches the NEAR universe is the
# tail of this bound.  400 at the classify lane's own amortization (400/900 s ≈ 0.44 req/s)
# stays far inside the 4 Hz budget and lets the ≤7-day universe (54 clusters, note 52 §3b)
# fill in within a few sweeps instead of a few hours.
CLASSIFY_MAX_MARKETS = 400                   # bound the cold-start sweep.  ρ DOES rank ACROSS
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
# (★★) THE FATE OF WHAT WE BUY  (note 23 Part V; note 43 §1, §5, §7 — 2026-07-28)
#
# *** EVERY CONSTANT IN THIS BLOCK IS STAGED-INERT.  ZERO CALL SITES, BY DESIGN. ***
# It changes no behaviour.  It is the DERIVATION RECORD plus the MEASUREMENT that judged it,
# committed together so the next reader inherits both halves and not just the argument.
# `grep -n BAND_ FREE_RIDE_ONLY DAILIES_ONLY_WINDOW_MULT` across the package must return this
# file and nothing else; `test_config.TestTheFateBlockIsInert` asserts it.
#
# WHAT PROMPTED IT.  v5 maximised REWARD RATE and never derived the losing path.  Measured on
# real money (work/audit-2026-07-28.md): −$74.52 on $928.70 deployed, of which 84% came from
# fifteen markets holding ~5.6c inventory that returned EXACTLY −100.0% (1,123 contracts,
# $0.00 recovered); only 28.1% of acquired inventory ever netted; and 47% of the pairs that
# DID form cost MORE than $1.00 combined.
#
# THE STRUCTURAL HAZARD, named (note 43 §7): scoring is denominated in CONTRACT COUNT, count
# is cheapest exactly where contracts are worthless, so the rung that maximises the subsidy is
# BY CONSTRUCTION the rung that maximises capital destruction.  Two denominators, one
# optimiser.  That hazard is REAL and remains unaddressed in live code.
#
# NO FATE SENTENCE IS ASSERTED HERE.  The draft one — "ends by netting against its own resting
# sell leg or settling within 24 h, worth ~$1.00 per completed pair against ≤98c paid" — named
# a mechanism (the sell leg) that the backtest then measured at −$40.30 against doing nothing.
# A fate sentence whose mechanism has been refuted is not a partial answer, it is a wrong one,
# and note 23 Part V is explicit that the blanks must come from MEASUREMENT.  The blank
# "a position acquired by this system ends by ____" is therefore still UNDERIVED and is flagged
# upward, unfilled.  No acquiring behaviour may ship until it is filled.
#
# THE BACKTEST that judged this design: 66 settled markets, 27,181 one-minute bars, pipeline
# validated to $0.000.  Full design −$23.70 (t = −2.24), and −$28.70 (t = −2.92) with the top 3
# markets removed — a loss that STRENGTHENS under outlier removal is a real loss, not variance.
# =============================================================================================

# --- 1. THE PRICE BAND -----------------------------------------------------------------------
# MEASURED (note 43 §1): "Below roughly 15c neither an exit nor an offsetting fill exists at
# any price, so the position has one fate."  15c is therefore not a preference, it is the
# observed boundary of the region in which our own fate sentence is UNWRITEABLE — there is no
# blank to fill because there is no exit to name.
#
# THE MARKET BAND [15, 85].  BOTH sides of the market must live inside it.  Its ceiling is the
# floor's exact mirror: YES + NO = $1 always, so a market whose YES trades at 90c is a market
# whose NO trades at 10c — the SAME dead zone, entered from the other end.  A guard on one end
# only would have refused the 5.6c rungs and admitted their 94.4c twins, which are the same
# rungs.
#
# THE OUR-LEG BAND [20, 50], strictly inside it.  Two derivations, both from the fate sentence:
#   (a) FLOOR = 15 + 5.  A position bought AT the market floor and then walked against by the
#       book is a position whose exit has to exist at a WORSE price than the entry.  The buffer
#       is one observed round-trip: TAKER_EXIT_MAX_SLIPPAGE_C = 3c is the measured spread on
#       qualifying rungs, rounded up to the next 5c so the buffer survives one spread of adverse
#       movement and still lands at or above the measured 15c boundary.  (The DIRECTION is
#       derived from measurement; the 5-vs-3 rounding is a convention — see UNDERIVED below.)
#   (b) CEILING = 50, and it is the JOINT-SUM guard's shadow, not an independent number.  A pair
#       may cost at most 98c; our leg at c forces the completing leg to ≤ (98 − c).
#       At c = 50 the completing leg must be ≤48c — comfortably inside the market band.  At
#       c = 85 the completing leg would have to be bought at ≤13c, i.e. BELOW the dead-zone
#       floor: an expensive leg is a cheap leg wearing a disguise, and the pair can never close.
#   (c) claimed: IT COSTS NO MARKETS, only sides — YES + NO = 100 − spread ≤ 99, so on ANY
#       market at least one leg is ≤49c, and the band merely chooses which SIDE we may be on.
# MIRROR (band too NARROW ↔ too wide): too narrow forgoes rungs whose fate we could in fact
# have named; too wide is what we measured and it costs PRINCIPAL: 84% of all losses.
#
# *** CLAIM (c) IS FALSE FOR TWO-SIDED QUOTING, AND THE BACKTEST FOUND IT. ***
# (c) is true of ONE leg and false of the PAIR.  Requiring BOTH legs ≤50c on a binary is not a
# constraint on the legs at all — since they sum to (100 − spread), it is a constraint on the
# MID, and it demands a market sitting within a couple of ticks of 50/50.  Measured on the
# tape: satisfied in 2.16% of bar-minutes.  As written, the band makes two-sided quoting
# structurally unreachable, which is a different system from the one that was specified.
# WHAT SURVIVES THE REFUTATION, and it is the important half: the MARKET band [15, 85] is
# independent of (c) and rests directly on measurement (note 43 §1 — below ~15c neither an exit
# nor an offsetting fill exists at any price; the −100% cohort averaged 5.6c).  Its ceiling is
# its own mirror, since a 90c YES is a 10c NO — the same dead zone from the other end.
# WHAT MUST BE RE-DERIVED before anything ships: the OUR-LEG band, which needs to be a
# per-side rule that a two-sided quoter can actually satisfy, not a disguised mid filter.
BAND_MARKET_MIN_C = 15                       # staged-inert: measured, unrefuted, unwired
BAND_MARKET_MAX_C = 85                       # staged-inert: mirror of the floor, unwired
BAND_OUR_LEG_MIN_C = 20                      # staged-inert: REFUTED AS SPECIFIED — see above
BAND_OUR_LEG_MAX_C = 50                      # staged-inert: REFUTED AS SPECIFIED — see above

# --- 2. JOIN BEST, NEVER IMPROVE PAST IT -----------------------------------------------------
# DERIVED FROM THE SCORING FUNCTION ITSELF, which is already in this file: score contribution is
# `size × DISCOUNT_FACTOR_DEFAULT^(ticks behind reference)` = size × 0.5^k.  Therefore:
#   * at-best (k = 0) scores 2× one-tick-back (k = 1) at IDENTICAL capital, so joining the best
#     is the whole of the cheap score;
#   * improving PAST the best (paying a tick more as a bid, accepting a tick less as an ask)
#     moves the reference price with us — k is still 0 — so it buys ZERO extra score and costs
#     a cent of entry price on every contract.  It is a strictly dominated action.
# VERIFIED AGAINST THE LIVE CODE, NO CHANGE REQUIRED — this is the one design item that was
# already true.  `engine.requote_pass` prices at `s.p if side == "bid" else (1.0 - s.p)`, and
# `s.p` is the SAME-SIDE BEST in its own collateral currency (`scan.build_slots` fills it from
# `alloc.score_side(...).ref_c`), so the quote joins the best exactly and never improves past
# it.  `quote.at_best` is the same statement, and requote trigger (a) `TRIG_OFF_BEST` is what
# keeps it true as the book moves.  Nothing to build; recorded here so the next reader does not
# re-derive it, and so a future edit that starts shading has to argue with this paragraph.
# MIRROR (improving past best ↔ resting behind best): resting BEHIND is legal, and
# MAX_SHADE_TICKS = 1 bounds it, because 0.5^2 = 25% of the score is worth less than the same
# dollars at the water level in another venue.

# --- 3. THE JOINT SUM GUARD — REFUTED, NOT IMPLEMENTED ---------------------------------------
# The design called for "our YES bid + our NO bid ≤ 98c, enforced on every reprice", on the
# reasoning that a pair acquired for ≤98c is profit by construction.  The reasoning is sound and
# the guard is still NOT BUILT, because the backtest (66 settled markets, 27,181 one-minute
# bars, pipeline validated to $0.000) measured its effect at approximately ZERO: no pair in the
# tape exceeded $1.00 with or without it.  The 47%-over-$1.00 disaster it was designed against
# did not come from the two legs of ONE market; it came from LADDER rungs repricing
# independently, which the at-best invariant and the requote triggers already prevent.
# A guard whose measured effect is zero is not free: it is a refusal path on the money path,
# it must be reasoned about forever, and it makes the thing it does not fix look fixed.
# NO CONSTANT IS DEFINED HERE ON PURPOSE — an unused constant is how a dormant guard gets
# switched on later by someone who reads the derivation and not the measurement.

# --- 4. THE RESTING SELL LEG — REFUTED AND REVERTED ------------------------------------------
# The design called for resting the sell of the position at fill + 2c instead of buying the
# opposing side, billed as simultaneously the exit and the netting leg at zero new capital.
# THE TAPE SAYS IT IS THE LOSS, and it is the largest single term in the measurement:
#     with it     −$23.70   (t = −2.24; −$28.70, t = −2.92 with the top 3 markets removed)
#     without it  +$16.60
#     delta       −$40.30
# THE MECHANISM, measured: it netted ZERO contracts against 230 without it.  It fires BEFORE the
# opposing side can fill, so it is neither the exit nor the netting leg it was billed as — it
# pre-empts the very netting it was supposed to cause.  Its breakeven exit rate is 94.4% and the
# realised rate is 87.4%; the 12 fills that did not exit lost 33.6c each.
# This is a derivation-scope failure of exactly the class note 23 Part V names, one level down:
# the fate sentence was written, and the mechanism chosen to make it true was never measured
# against the alternative of doing nothing.  NOT IMPLEMENTED, and no constant is left behind.

# --- 6. FREE-RIDE ON DEPTH, NEVER FUND IT -----------------------------------------------------
# The 1000-contract qualification is a DISCRETE precondition: if a side is short of target the
# snapshot is excluded and nobody is paid.  v5 therefore FUNDED it — `qualification_pass` posts
# `target_size − cum_size` contracts at LAND_GRAB_PRICE_C = 1c.  Read against note 43 §7 that is
# the structural hazard in its purest form: the cheapest possible proximity, at the price where
# a contract is worth nothing, in the size that maximises count.  It is the machine that bought
# the −100% cohort.
# The derivation of the replacement: qualification is worth exactly the same to us whether WE
# created it or someone else did, and someone else's costs nothing.  So enter only where the
# rival book ALREADY clears target WITHOUT our size — free-ride on depth, never fund it.  The
# test must deduct our own resting size, because the public book reflects it (SF-5's finding,
# applied to `qualifies` instead of only to `S`); without the deduction the check certifies our
# own land grab back to us.
# MIRROR (never funding ↔ never entering): the fear is that no market qualifies without us and
# the book deploys nothing.  That fear is NOT yet measured — the backtest priced the quoting
# rules, not this one — which is exactly why the flag is inert.  Arming it means gating
# `scan.build_slots` on a RIVAL qualification (our own resting size deducted, per SF-5) and
# zeroing `alloc.qualification_pass`; the machinery stays in the tree so the decision remains
# one constant in either direction, and `free_ride_refused` would be the instrument.
# ALSO STILL TRUE AND UNADDRESSED: `LAND_GRAB_PRICE_C = 1` is live today, and a 1c land grab is
# the −100% cohort's own geometry (note 43 §7).  Nothing in this commit changes that; it is the
# single most direct surviving link between the live code and the measured loss, and it is
# flagged upward as the next thing to price.
# ARMED 2026-07-29.  Wired in `scan.build_slots` as a RIVAL-qualification gate (our own
# resting size deducted per SF-5) plus a hard zero on `land_grab`.  What changed since the
# staged-inert note above: the mirror's fear -- "no market qualifies without us and the book
# deploys nothing" -- was MEASURED and refuted.  5,681 of 5,695 live book-sides (99.75%) reach
# target_size on rival size alone; among sides with a competing score of 3 or less, 99.3%
# qualify.  And the CFTC filing settles what the funded size was ever worth: the qualifying
# walk STOPS once cumulative size reaches target, so contracts posted beyond it score exactly
# zero -- the 999-contract 1c gas rung and the 1,500/3,000-contract TRUEV rungs were the
# largest objects in the tape and earned nothing at all.  `free_ride_refused` is the instrument
# if the fear ever becomes real.
FREE_RIDE_ONLY = True

# --- THE ENTRY BAND: FREE-RIDE IS ONLY CORRECT INSIDE A PRICE BAND (2026-07-29 night) ---
# THE CONFLICT THIS RESOLVES.  Enchiridion notes 46 and 47 §6 record `FREE_RIDE_ONLY` as
# "ACTIVELY HARMFUL — a covert instruction to quote the cheap crowded side."  The backtest
# (`work/audit-nonlip-strategies-2026-07-28.md`) records the same rule as the STRONGEST single
# improvement on the tape (−$51.40 against −$75.40 for the variant without it).  Both readings
# are correct and they are not about the same thing: free-riding is right about WHO PAYS FOR
# QUALIFICATION and silent about WHERE, and left unbanded it lands on 1c, because 1c is where
# rival depth is deepest and therefore where a free ride is always available.  Arming the rule
# without a band is arming half of it.
#
# WHY THE BAND IS 7-20c, MEASURED, NOT CHOSEN.  Two independent readings pick the same window:
#   (1) BIAS (note 47 §3, n = 8,240 settled markets, real two-sided quotes at close−60min).
#       Realised frequency against posted price: 1c −94.8%, 2c **0.00% realised on 765 markets**
#       (−100%), 3–5c −64.6%, and 6–20c −13% to −19% NOT SIGNIFICANT.  6c is where the measured
#       negative bias stops being distinguishable from zero.  This is the whole reason the cheap
#       end is not free money: on Kalshi A RESTING CHEAP BID IS A BUY, so we were on the same
#       side as the retail flow that overpays for longshots.
#   (2) COMPETITION AND COST-TO-CLEAR (note 47 §4, `~/compmap.json`).  Median competing score
#       is 6,618 at 1c, **403 at 11–20c**, 2,877 at 96–99c — everyone runs the same
#       score-per-dollar arithmetic and crowds both extremes.  Median cost to clear the $1.00
#       floor: $6.36 at 1–5c, **$3.68 at 11–20c**, $244 at 96–99c.  So the band is simultaneously
#       the emptiest side of the book, the cheapest floor to clear, and the only stretch with no
#       measured negative bias.  There is no trade-off to make here; it is the same window.
# SUPPLY IS MEASURED, NOT ASSUMED: **317 rungs at 7–12% clear $1.00 for ≤$20, across 177
# INDEPENDENT CLUSTERS** (note 47 §4).  The band therefore cannot starve a 30-market book.
#
# WHY NOT WIDER ON THE UPSIDE.  Note 47 §4: our presence was 76% below 20c, **0.3% between
# 20–80c**, 23% above 80c — strikes sit deep-OTM or deep-ITM and almost nothing rests near 50c.
# "A 20–80c band does not filter a ladder — it DELETES it."  20c is the top of the populated
# cheap cohort, not a risk preference.
#
# THIS IS NOT `p_min = k/bankroll`, WHICH IS REFUTED (note 47 §5/§6, "do not rediscover").  That
# idea derived a price floor from RUIN, and it is wrong because floor-clearing size is a CONTRACT
# count with no price term, so cost per rung is `Q·p` and FALLS with price; at a fixed budget you
# afford `N = B/(Q·p)` rungs, `E[hits] = N·p = B/Q`, and the price CANCELS.  This band is derived
# from measured BIAS and measured COMPETITION instead, which do not cancel.  A reviewer should
# hold this constant to that standard: if it is ever re-justified by variance, it is the refuted
# idea wearing a new name and it must come out.
#
# ENTRY ONLY, exactly like the free-ride gate itself and for the same reason: accrual is per
# PERIOD, so abandoning a market mid-period forfeits everything earned in it for nothing.  Held
# inventory and live orders are exempt (`held` in `scan.build_slots`); the requoter prices
# staying.  MIRROR (band too NARROW ↔ too wide): too narrow starves the book and shows up as
# `entry_band_refused` counts with idle capital — the instrument is in place; too wide is the
# 2c cohort, which is a measured −100% on 765 markets.
# ── CORRECTED 2026-07-29 night (Ryan).  THE BAND IS A BIAS FLOOR, NOT A VARIANCE INSTRUMENT. ──
# The first cut of this was a 7-20c BAND and it was justified on two grounds at once: measured
# bias, and ruin.  The ruin half was wrong, and the specification asked for the right thing:
# "instead of a hard cap just track our average variance and make sure its above that".
# WHY A PRICE CAP CANNOT BE THE VARIANCE INSTRUMENT.  Portfolio variance per deployed dollar is
# `V = Σ wᵢ²(1−pᵢ)/pᵢ` and `CV = √V`.  At the 0.25 tolerance below, EVERY one of these books
# passes — computed, not asserted:
#     30 markets @ 12c → V = 0.244, CV 49%, P(zero winners) 2.2%
#     50 markets @  8c → V = 0.230, CV 48%, P(zero winners) 1.6%
#    100 markets @  4c → V = 0.240, CV 49%, P(zero winners) 1.7%
#    200 markets @  2c → V = 0.245, CV 49%, P(zero winners) 1.8%
# A book made ENTIRELY of 2c rungs is inside tolerance at N = 200.  So price carries no
# variance information on its own — only price TOGETHER WITH breadth does, which is exactly what
# V measures and what a per-rung price cap cannot see.  Any price floor derived from ruin is
# `p_min = k/bankroll` again (note 47 §5/§6, "do not rediscover"), and it fails the same way: at
# a fixed budget cheap rungs cost less, so N rises as p falls and `E[hits] = B/Q` is
# price-independent.  Tracking the realised book sidesteps the error entirely, because it never
# assumes N — it reads it.  See `PORTFOLIO_VAR_MAX` and `guards.portfolio_variance`.
# WHAT SURVIVES, ON A DIFFERENT GROUND ENTIRELY: BIAS.  Note 47 §3, n = 8,240 settled markets,
# real two-sided quotes at close−60min — realised frequency against posted price: 1c −94.8%
# (n=3,205), 2c **0.00% realised on 765 markets** (−100%), 3–5c −64.6% (n=333), and 6–20c −13%
# to −19% NOT SIGNIFICANT.  A −100% cohort is not a variance problem and no amount of breadth
# diversifies it away; it is a losing bet at every weight.  6c is where the measured bias stops
# being distinguishable from zero, and that — not ruin — is the whole content of this floor.
# THERE IS NO UPPER BOUND.  The old 20c ceiling was capital efficiency ("81–99c is the worst
# reward real estate on the board", note 47 §4), which the objective already prices through
# `gross ∝ 1/p`: an expensive rung simply loses the water-filling comparison.  A hard ceiling
# added nothing and it is what emptied the book.
ENTRY_BAND_LO_C = 6
ENTRY_BAND_HI_C = 99                         # no ceiling: MAX_LEGAL_PRICE_C already bounds it
# ── STAGED INERT.  THE BAND IS DERIVED AND CORRECT AND CANNOT BE ARMED YET. ─────────────────
# MEASURED, on arming it: the band's intersection with the (★) ADMISSION GATE IS EMPTY, so the
# book rests NOTHING AT ALL.  Not a fixture artifact — the cause is a single input:
#
#     phi (fills/hour per resting contract) is SEEDED BY PRICE for any venue with no tape of
#     our own: `PHI_SEED_CHEAP = 0.001` below `PHI_CHEAP_PRICE_CUT = 0.05`, `PHI_SEED_MID =
#     0.08` at or above it.  AN 80x STEP AT A PRICE CUTOFF.
#
# Holding everything else fixed (pool $100, rival score 1,200, q = 100) and varying ONLY phi:
#     phi = 0.001  → `admits` is TRUE at every price from 1c to 40c
#     phi = 0.080  → `admits` is TRUE at 1c ONLY
# So the seed is not a prior on one venue; it is a GLOBAL ON/OFF SWITCH FOR THE WHOLE BOOK, and
# the 5c cut is the router that decides which side of the switch each venue lands on.  Every
# price in a 7-20c band is above the cut, therefore gets 0.08, therefore is refused.
#
# THIS IS THE MECHANISM BEHIND "98.4% OF CONTRACTS PLACED AT <=5c".  It was never the sizing
# rule — it is ADMISSION.  And the cohort the switch admits is the one note 47 §3 measured at
# −94.8% (1c, n=3,205) and −100% (2c, 0 of 765 markets ever paid).  The gate's binding input
# selects, by construction, the only prices whose realised EV we have measured to be total loss.
# `scan.build_slots` already says the mid seed "prices every fresh venue out"; what was not
# said is that the cheap seed prices every venue IN, and that the switch is a price rule
# masquerading as a fill-rate estimate.
#
# WHY NOT JUST PICK A NUMBER.  Because there is no measurement to pick from — note 47 §7 still
# lists "fill probability on resting cheap bids" as OPEN, and what we DO know contradicts the
# price form of the prior: fill rate is a MARKET property, not a price or size property (the
# larger half of rungs had the higher fill rate in 1 of 10 events, P = 1.1%).  Choosing 0.001
# arms every venue on the board; choosing 0.08 arms none.  Under note 49 R1 a constant that
# swings the entire book and has no interval is not an estimate, and under R2 it may not be set
# by whichever value makes the current change look good.  Arming this band therefore REQUIRES
# the λ measurement (note 50 §5's classify-sweep field), not a choice.
# Instrument if this is ever armed: `entry_band_refused` against `idle_capital`.
# ARMED 2026-07-29 night, together with the one-seed phi (`money.seed_phi`).  THE TWO MUST SHIP
# TOGETHER AND NEITHER IS CORRECT ALONE: the phi step was price discipline wearing a fill-hazard
# estimate's name, so removing it without the band leaves NO price discipline at all — and
# `ACQUIRE_FLOOR_C` is still in `git stash`, `n_cap` still buys MORE contracts as price falls, so
# the unbanded book's cheapest rung gets its biggest order.  That is the 999-contract 1c gas rung.
# Armed, the band is the floor the phi step was crudely proxying, derived from measurement
# instead: bias (n = 8,240) and competition, both in the note above.
ENTRY_BAND_ARMED = True

# UNDERIVED (note 23 §II), flagged upward rather than shipped silently:
#   * THE FATE SENTENCE ITSELF.  "A position acquired by this system ends by ____" has no
#     measured answer.  The mechanism proposed for it (the resting sell leg) was measured and
#     is worse than nothing (−$40.30).  This is the blocking item: note 23 Part V says no
#     acquiring system ships without it, and v5 acquires.
#   * BAND_OUR_LEG_*: refuted as specified (2.16% of bar-minutes).  Needs a per-side formulation
#     that a two-sided quoter can satisfy.
#   * BAND_MARKET_*: derived from measurement and unrefuted, but never itself backtested — the
#     15c boundary is read off the loss cohort, not off a controlled comparison.
#   * DAILIES_ONLY_WINDOW_MULT: derived, unmeasured.
#   * FREE_RIDE_ONLY: derived, unmeasured.

# =============================================================================================
# RISK CAPS  (spec §4.4 table)
# =============================================================================================
DAY_STOP_FRAC = 0.35                         # v1 §8.4.  UNDERIVED §9.5.
DAY_STOP_FLOOR_USD = 20.0
DAY_STOP_CAP_USD = 150.0

# =============================================================================================
# THE PRESENCE-RESERVE CAP STACK  (enchiridion note 52, D5/D6/D7 — settled with Ryan
# 2026-07-29 night).  One derivation produces every number in this block; nothing here is
# independently tunable, which is the point.
#
# THE STRATEGY'S SHAPE: ~N uncorrelated settle sources; per source ONE rung, sized to its own
# floor-clearing LOT; capital reserved to RE-POST the lot after each fill rather than posting
# a bigger order (Ryan: a 3x order holds 3x the inventory on its first fill; a 3x reserve
# holds one lot's worth and re-posts — same presence, a third of the inventory risk).
#
#     RUNG_REFILLS   = 3                     Ryan: "say on average we need to buy back 3
#                                            times to hold presence" — charitable phi, D8;
#                                            measured phi replaces it (the lipband capture).
#                                            A PRIOR for planning, not a per-rung guarantee —
#                                            see the lot cap below.
#     CLUSTER_RESERVE = ceiling / N          = $10 — the per-settle-source budget.  The
#                                            existing cluster rail (positions + resting basis
#                                            vs the cap) IMPLEMENTS the reserve: fills convert
#                                            resting into positions, re-posts keep coming
#                                            until the cluster is at cap — exactly the
#                                            (1 + phi*W) capital multiplier of note 51 §1,
#                                            in cap form.
#     SLOT_LOT_CAP   = CLUSTER_RESERVE / 2   = $5.00 — the LOT CONTAINER.  A rung whose
#                                            floor-clearing lot does not fit is REFUSED,
#                                            never shrunk ("fewer rungs, never smaller lots",
#                                            D6: below floor-clearing size a rung earns ZERO).
#                                            WHY /2 AND NOT /(1+refills): the first cut used
#                                            $2.50 (a fixed 4-lot reserve) and the LIVE BOARD
#                                            refused every venue — note 47 §4's own number is
#                                            a MEDIAN cost-to-clear of $3.68 in the emptiest
#                                            band, so a $2.50 container refuses the median
#                                            rung on the board.  /2 guarantees at least ONE
#                                            re-post for the largest admissible lot; a $2 lot
#                                            gets 4.  REFILLS PER RUNG ARE EMERGENT from the
#                                            fixed reserve (reserve/lot − 1), which is Ryan's
#                                            own tolerance: "we'll do 5 to be conservative,
#                                            and if that's too much we'll just earn slightly
#                                            less rewards."
#     N_TARGET_CLUSTERS = ceiling / CLUSTER_RESERVE = 30 at $300.
#
# WHY N IS DERIVED AND NOT CHOSEN: supply, MEASURED 2026-07-29 (note 52 §3b), is ~38 clusters
# that both settle inside SETTLE_HORIZON_H and can clear $1.00 at HALF presence — so 30 fits
# inside measured supply with margin.  If the ceiling rises, N rises with it and the supply
# number is the thing to re-measure first.
# MIRROR (N too HIGH ↔ too low): too high shrinks the reserve below the lot and every rung is
# refused as unfundable — the book deploys nothing, visible as `lot_unfundable` counts.  Too
# low concentrates: at N=2 (the old 48h window filter's real universe) it is the -$195 night.
# RUIN CHECK (note 52 §4): at N=30 equal weights, V <= PORTFOLIO_VAR_MAX = 0.25 requires
# average price >= 1/(1 + N/4) ~= 12c; the plan-side variance instrument (D11) enforces the
# average, the 6c ENTRY_BAND floors the measured-EV cohort, and neither is a per-rung price
# cap (p_min = k/bankroll stays refuted).
RUNG_REFILLS = 3
N_TARGET_CLUSTERS = 30
SLOT_LOT_CAP_USD = 5.00                      # == (300 / N_TARGET_CLUSTERS) / 2; test_config
                                             # asserts the identity at the live ceiling
# The day-stop-derived risk statement ("no single correlated bet may trip the global day
# stop") still holds and is STRICTLY LOOSER: 0.5 x day_stop >= 0.5 x max($20, 0.2 x ceiling)
# = $30 at $300, and ceiling/N = $10 <= $30 whenever N_TARGET_CLUSTERS >= 10.
# `test_config` asserts the identity lot x (1+refills) == ceiling/N so the three constants
# cannot silently drift apart.

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
# ── SUPERSEDED 2026-07-29 night (note 52 D6).  INV_CAP_USD is now the LOT CONTAINER: the
# per-order dollar bound equals SLOT_LOT_CAP_USD, because the presence-reserve stack sizes a
# resting order at its floor-clearing LOT and holds (1+RUNG_REFILLS)x that per cluster.  The
# old $10 form let one order eat the whole cluster reserve, which converts the reserve into
# inventory on the first fill — the exact failure the reserve exists to prevent.
INV_CAP_USD = SLOT_LOT_CAP_USD               # = $2.50 — the LOT container (note 52 D6)


def slot_cap_usd(day_stop_threshold_usd, floor_usd=None, ceiling_usd=None):
    """── SUPERSEDED IN DERIVATION 2026-07-29 night (note 52 D5/D6): the per-order cap is the
    LOT CONTAINER, `ceiling / (N_TARGET_CLUSTERS × (1 + RUNG_REFILLS))` = SLOT_LOT_CAP_USD at
    the $300 ceiling.  The presence-reserve stack replaces the day-stop derivation below: the
    unit that must not trip the day stop is the CLUSTER, the cluster reserve is ceiling/N, and
    the per-ORDER bound is the reserve divided by (1 + refills) so one fill converts at most a
    quarter of the reserve into inventory.  The day-stop bound survives transitively (see the
    N_TARGET_CLUSTERS block): ceiling/N ≤ 0.5 × day_stop for every N ≥ 10.
    `day_stop_threshold_usd` is kept in the signature for its call sites but no longer moves
    the answer; `ceiling_usd=None` returns the $300-era constant.

    The ORIGINAL derivation, kept because R1 (nesting) still governs and R4's degeneracy
    argument is why the reserve lives at the CLUSTER and not the slot:

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
    if ceiling_usd is not None:
        return float(ceiling_usd) / (N_TARGET_CLUSTERS * 2.0)
    return SLOT_LOT_CAP_USD


PER_MARKET_POOL_MULT = 4.0                   # v1 §8.2 never risk 4× a market's own max prize

# --- B16: THE PER-MARKET ACQUISITION CAP (2026-07-29) ---
# WHY A SECOND, TIGHTER PER-MARKET BOUND.  `PER_MARKET_BUDGET_FRAC = 0.25` was inherited from
# v1 and permits $75 of one market at a $300 ceiling — which is not a concentration limit, it is
# a rounding error away from the -$80.60 gas ladder.  This constant replaces it as the binding
# one, and unlike v1's it is derived from the RISK MODEL rather than from a round number.
#
# ── D2 CORRECTION (2026-07-29 night).  "REPLACES IT" WAS FALSE WHEN WRITTEN. ────────────────
# The sentence above claimed a replacement that did not happen: `PER_MARKET_BUDGET_FRAC` kept
# its own value of 0.25 and `alloc.market_cap_usd` kept reading it, so the PLAN went on
# permitting $75 of one market at a $300 ceiling while the new RAIL refused above $30 — and a
# rail tighter than the plan is a permanent re-offer loop, because `place()` returning False
# arms no degrade.  A derivation that says "replaces" must be enforced by an assignment, not by
# a comment.  So the two are now ONE number by construction, and `alloc.market_cap_usd`'s
# docstring carries the plan ⊆ rail proof that this identity is the load-bearing step of.
#
# THE DERIVATION.  We do not exit: 149 of 6,149 acquired contracts were ever closed (2.4%),
# across 7 closing orders in the operation's entire history, all takers.  So NET exposure equals
# GROSS, and the only control on directional risk is refusing to acquire.  The whole reason for
# breadth is variance: with N equally-weighted independent markets the book's SD scales as
# 1/sqrt(N), and with UNEQUAL weights the effective count is the inverse Herfindahl
# `N_eff = 1/sum(w_i^2)`.  Sizing is deliberately unequal (more capital where the pool is large
# and the rivals thin), so the cap's job is to stop one weight from collapsing N_eff.  At a
# maximum weight of 0.10 with the remainder spread, N_eff stays above ~20 — i.e. the cap buys
# the variance reduction that the whole strategy rests on, and 0.25 does not (one market at 0.25
# caps N_eff near 10 no matter how many markets are open).
# It is also strictly inside the cluster cap, which bounds the correlated GROUP; this bounds one
# MARKET, because the measured loss was denominated per market: matched pairs earned +$39.63
# (+6.88c/pair) while the unmatched residual lost -$587.42.
# MIRROR (too LOW ↔ too high): too low refuses size a market's pool would have paid for and
# shows up as `market_cap` refusals with accrual left on the table — bounded and recoverable
# next period.  Too high is the -$587: an unbounded one-sided position with no exit.
# --- THE TRACKED PORTFOLIO VARIANCE (2026-07-29 night, Ryan's specification) ---
# "instead of a hard cap just track our average variance and make sure its above that".
# THE QUANTITY.  A binary held to settlement pays $1 or $0, so one dollar at price p has payoff
# variance `(1−p)/p`.  Across a book with weights wᵢ (each cluster's share of deployed capital):
#     V = Σ wᵢ²(1−pᵢ)/pᵢ        CV = √V        N_eff = 1/Σ wᵢ²
# V is variance PER DEPLOYED DOLLAR, so it is scale-free: the same number governs a $45 book and
# a $300 one, which is why it can be a standing rail rather than a constant that needs re-deriving
# every time the ceiling moves.
# THE THRESHOLD.  0.25 ⇒ CV = 50%: a one-standard-deviation day moves half the deployed capital.
# Chosen because at N ≈ 30 it is also where `P(zero winners)` — the quantity that actually ruins a
# held-to-settlement book, since with no winners every stake is lost — falls to ~2%, and because
# it is satisfiable in many shapes rather than only one (30@12c, 50@8c, 100@4c, 200@2c all pass).
# UNDERIVED as a utility statement: 50% is a tolerance, not a theorem, and it is Ryan's to set.
# WHY PER CLUSTER AND NOT PER MARKET.  Independence is the unit variance is denominated in, and
# note 43 §3 / note 47 §5 settle what the unit is: a threshold ladder is ONE bet wearing many
# tickers (gas gave nine rungs on one settle number).  Weighting by market would report N_eff = 30
# for thirty rungs of one ladder — the exact error that produced the −$587 unmatched residual.
# Intra-cluster netting is deliberately IGNORED (it can only reduce true variance), so V is an
# upper bound and errs toward refusing.
PORTFOLIO_VAR_MAX = 0.25

MARKET_CAP_FRAC = 0.10
# ONE FRACTION, TWO CONSUMERS.  The plan (`alloc.market_cap_usd`) and the rail (B16) must not
# hold separate opinions about how much of the book one market may be; see the plan ⊆ rail proof
# in `alloc.market_cap_usd`, whose middle inequality is this line.  v1's 0.25 is superseded.
PER_MARKET_BUDGET_FRAC = MARKET_CAP_FRAC


def market_leg_cap_usd(ceiling_usd, day_stop_threshold_usd):
    """The B16 per-LEG bound: `max(slot_cap, MARKET_CAP_FRAC × ceiling)`.

    Named for the measure, not the level, because `alloc.market_cap_usd` is a DIFFERENT
    quantity — the plan-side GROSS per-ticker cap — and two functions called `market_cap_usd`
    measuring two things is how the plan and the rail came to disagree in the first place.

    ── D2: WHY THE `max` IS MANDATORY AND NOT DEFENSIVE PADDING. ─────────────────────────────
    `MARKET_CAP_FRAC × ceiling` alone INVERTS THE CAP HIERARCHY at small capital, and an
    inverted cap is not a conservative cap — it is a deadlock.  Worked at the two live
    ceilings, with `slot_cap = max(INV_CAP_USD, 0.5 × day_stop)`:

        ceiling $300 → day_stop $60 → slot_cap $30 ; 0.10 × 300 = $30  → equal, legal
        ceiling  $45 → day_stop $20 → slot_cap $10 ; 0.10 ×  45 = $4.50 → **INVERTED**

    At $45 the allocator may plan a $10 rung (that is its own cap) and the rail would refuse
    everything above $4.50.  `place()` returning False does not arm any degrade — the cancel-
    first path latches only on an exchange `insufficient balance` reject — so `_requote_slot`
    returns False and THE SLOT RE-OFFERS THE SAME REFUSED ORDER EVERY CYCLE, FOREVER.  This is
    the identical failure mode NEW-1 found and fixed at the cluster cap, arriving one level
    finer; `slot_cap_usd`'s R1 states the rule it breaks ("a finer cap may never exceed the
    coarser one it sits inside" — read from the market's side, a coarser cap may never fall
    below the finer one it contains).

    WHAT THE `max` COSTS, STATED PLAINLY: below `slot_cap / MARKET_CAP_FRAC` (≈ $100 of ceiling
    at today's $10 slot-cap floor) the per-market cap is NOT 10% of the book and the N_eff > 20
    variance guarantee in `MARKET_CAP_FRAC`'s derivation IS VOID.  That is not a defect of the
    `max`; it is arithmetic — a $45 ceiling cannot hold 30 markets at any per-market cap, since
    $45/30 = $1.50 and the payout floor alone needs more than that.  The honest statement is
    that the variance instrument requires capital to exist, and `market_cap_inverted` is logged
    at the boundary so the void guarantee is visible rather than assumed.

    MIRROR (max too permissive ↔ inverted cap): the max can only ever raise the per-market cap
    to the per-SLOT cap, which is already a rail the allocator plans inside, so nothing new
    becomes reachable through it — the cluster cap and the ceiling both still bind above.  The
    inverted end is a permanent re-offer loop against a live account.
    """
    slot = slot_cap_usd(day_stop_threshold_usd)
    derived = MARKET_CAP_FRAC * float(ceiling_usd)
    if derived < slot:
        from . import runtime as _R
        _R.log_once("market_cap_inverted", ceiling_usd=float(ceiling_usd),
                    derived=round(derived, 2), slot_cap=round(slot, 2),
                    note="per-market cap raised to the slot cap; the N_eff>20 variance "
                         "guarantee is VOID at this ceiling")
    return max(slot, derived)


# --- THE PAYOUT FLOOR AND THE SIZING TARGET (2026-07-29) ---
# SCORE_SIDES: scores normalise WITHIN EACH SIDE (CFTC filing, KalshiEX 2026-02-11), so the sum
# of every participant's Snapshot LP Score is 1 per side = 2 per qualifying snapshot, and a
# ONE-SIDED quote can earn at most `pool / 2`.  Every credit estimate this program produced
# before this line omitted the divisor and was therefore 2x hot.  It is a property of the
# filing, not a tunable: it is 2 because a binary has two sides.
SCORE_SIDES = 2.0
# CREDIT_TARGET_USD: the $1.00 minimum payout per market per program period.  Below it the
# accrual is forfeited ENTIRELY — visible in the receipt itself, whose smallest line item is
# exactly $1.00 and whose next three are $1.01, $1.06, $1.11.  Measured cost of ignoring it:
# 43 of 67 rungs earned something under a dollar and were paid nothing, burning 167 dollar-hours.
CREDIT_TARGET_USD = 1.00
# CREDIT_TARGET_MARGIN: size for 1.5x the floor, not 1.0x.  Sizing to exactly the floor is
# sizing to exactly the cliff edge — presence shortfall, a rival arriving mid-period, or an
# error in the rival-score estimate all send the rung to zero rather than to slightly less.
# UNDERIVED as a distribution; derived as "half a floor of headroom is worth more than the
# marginal credit it displaces".  Recalibrate to $1.00/q05(actual/projected) once a per-market
# accrual reading exists — it does not today: credits are labelled by EVENT, and the bot's own
# accrual model measured 2.27x off the receipt.
CREDIT_TARGET_MARGIN = 1.5
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
