# lip_v5 — presence-portfolio maker

**STATUS: STAGED-INERT.** Built, tested, committed on a branch. It has never run against the
exchange, never placed an order, and never written to a live data path. It deploys ONLY on
Ryan's explicit word, one gate at a time.

v4 keeps earning, untouched, throughout. v5 writes no v4 path — that is what makes rollback
one command.

---

## What this is

v5 is not v4-plus-patches. It is re-derived from one objective:

> Maximize `Σ_(market,side,second) resting_dollars × 0.5^ticks × pool_rate`, NET of fill costs,
> inventory carry (`capital × time-to-liquidity`, **PRICED**), and operational risk.

Everything else is a corollary of **(★)**:

```
gross(q)   = ρ·S / (2·p·(q+S)²)            $/h per collateral-$    [v4 had this]
carry_cost = φ · L_eff · r*                $/h per collateral-$    [NEW — the $16 lesson]
drift_cost = φ · d / p                     $/h per collateral-$    [v4 had this]
net(q)     = T̂ · gross(q) − carry_cost − drift_cost                            (★)
```

`ALLOCATE` admits a slot iff `net(q) > λ_min/16`. There is no separate hurdle.

**The one number that matters.** On spec §0.4's own inputs, the PayPal mention market scores:

| venue | gross | carry | drift | net | verdict |
|---|---|---|---|---|---|
| PYPL mention (closes DEC 31) | 0.0146 | **11.70** | 0.117 | **−11.80** | EXCLUDED, ~800× |
| treasury daily | 0.125 | 0.0040 | 0.0112 | **+0.110** | KEEP, 17.6× floor |
| gas cheap side | 3.125 | 0.00005 | 0.001 | **+3.12** | KEEP |

v4 would have funded the first row. v5 refuses it by three orders of magnitude, using a term
v4 did not have, **before any settlement data exists**. That is the whole build.

The second refusal path is independent of the model: **presence-seconds per dollar-hour**.
`T̂ = PSDH/3600` is the fraction of modeled presence actually realized. A market that fills you
on contact converts capital to inventory, drives `prox_dollar_s` to zero, and `T̂ → 0` within
hours — no settlement, no credit, no waiting.

---

## Module map

| file | lines | what |
|---|---|---|
| `config.py` | 527 | every constant, with its spec derivation AND its note-23 §IV mirror answer |
| `money.py` | 418 | **(★)**, `L_eff` and the past-due escalation, φ/d estimation, the `r*` fixpoint, shading, dose-response |
| `presence.py` | 377 | the 1 Hz meter, PSDH/T̂, per-slot kill, the four-branch collapse predicate, compaction |
| `ratchet.py` | 370 | verified-accrual ladder, `floor_q`, admission bounds, OUT_OF_REACH, stand-down, revive |
| `cashfeed.py` | 511 | the computed cash feed and its one invariant |
| `alloc.py` | 450 | ALLOCATE under (★) — v4's water-filling with exactly one substitution |
| `clusters.py` | 255 | **the underlying-cluster cap** — signed delta + exact worst-case loss per settle source |
| `guards.py` | 495 | **the rails, B1..B13** — day stop, halt machine, drawdown, daily loss, capital floor, cross-bot, dedupe, refill, UNKNOWN bound, clock skew, and the ORDERED gate `place()` calls |
| `engine.py` | 515 | **the run cycle** — startup/refusals/adopt/triage, `place()` (the one path to the wire), fills, meter, recon, shutdown |
| `exchange.py` | 123 | the one wire seam, plus the `FakeExchange` the suite drives |
| `ratelimit.py` | 265 | token bucket, AIMD, lanes, the SF-1 cancel bound, the degrade ladder |
| `cutover.py` | 455 | `--gen-adopt`, the W2 adoption gate, **cutover triage**, handback/rollback |
| `ledger.py` | 131 | the money record, and the separate presence file + compaction |
| `wsgate.py` | 156 | the W2 3-agreement gate over the vendored feed |
| `ws_feed.py` | 1221 | **vendored verbatim from v4** — see below |
| `scan.py` | 450 | **programs feed → classify sweep → slot table** — window guards, runway, deny list, REAL market close, the P6 public-tape check |
| `quote.py` | 130 | **the pure half of the REQUOTER** — §4.3 triggers, shed geometry (never crossing), whole-second policy |
| `runner.py` | 171 | **the outer loop** — init/recovery, the systemd cycle, always-shutdown |
| `runtime.py` | 363 | the only clock, the only logger, every external effect behind a stubbable seam |
| `lip_v5.py` | 386 | the binary; note 23 §III's five answered in its header |
| `tests/` | 4200 | **491 tests**, `python3 -m unittest` green — including the ALIVENESS suite (`test_aliveness.py`): FakeExchange + one good venue ⇒ orders APPEAR; a failing adopted position ⇒ a shed APPEARS |

### The rails (`guards.py`) — B1..B13

`place()` calls ONE function, `guards.place_allowed`, which runs the rails in dependency order
and returns the FIRST refusal. The order is derived: a halted book needs no further reasoning;
eligibility precedes sizing; caps are meaningless on a market we were not allowed to quote.

| # | guard | the failure it names |
|---|---|---|
| B5 | halt/resume machine | a halt a restart clears is not a halt; resume is an operator record, never a timer |
| B2 | day stop | constants existed in v4 with **zero call sites**; unpriced inventory marks AT COST, and a fully-closing order is exempt so a halted book can still leave |
| B1 | cluster cap | 15 rungs of one ladder are ONE bet — today's live treasury loss |
| B3 | peak/drawdown | a slow bleed no daily limit catches; the peak is persisted or the bleed erases its own evidence |
| B4 | daily loss limit | open-day attribution, so a multi-day settlement cannot trip today's limit |
| B6 | persist fail-closed | a write failure while live halts, because every control reasons from persisted state |
| B7 | fresh-state refusal | blank ledger against a non-flat account — the invisible-position class, refused on cycle 1 |
| B8 | fill dedupe | exchange-fill-id keyed, at the STATE layer, because there are three paths into state |
| B9 | refill/turnover cap | the 1 Hz bound the 15-minute kill cadence structurally cannot provide |
| B10 | UNKNOWN bound | an unresolved order holds collateral forever; 3 tries then book-filled + freeze |
| B11 | capital floor | v5 spending the last dollars is v5 deciding nestor cannot trade |
| B12 | clock skew | our signatures and every `expiration_ts` come from the local clock |
| B13 | cross-bot exclusion | orders AND positions — nestor can hold a position in a market it has no order in |

**`ws_feed.py` is vendored, not imported.** The coupling surface to its v4 host was verified by
grep before copying and is exactly four symbols (`_now`, `price_str`, `log`, `Auth`), all
supplied by `runtime`. The single edit is the import head. v5 must not import `lip_maker_v4` at
runtime: v4 is FROZEN and deployed separately, so a live coupling would mean a v4 edit silently
changing v5, and a v5 deploy needing v4's tree on the box.

---

## Running the tests

```bash
cd tools && python3 -m unittest discover -s lip_v5/tests -t .
```

491 tests, ~0.2 s, no network, no filesystem outside the tmpdir, no possibility of paging.

**The suite cannot page and cannot write outside tmp**, structurally, not by convention — two
real incidents this week were a unit suite firing a push to a phone and a unit suite writing
outside tmp. Three independent guards, each asserted by `TestNoExternalEffects`:

- `runtime.set_write_roots([tmpdir])` — any write elsewhere raises `PermissionError`.
- `runtime.set_alert_sink(...)` captures pages; `NTFY_DISABLE` is a second belt; and
  `runtime.ntfy` refuses to send while the process is not `--live` — a third.
- `runtime.http()` raises while not live, so no test can reach the wire even by accident.

---

## STAGED DEPLOY — each step is a SEPARATE human call (R186)

**No step bundles a capital change with a code change.** Do not run two of these in one
sitting. Each has one decision, one read-out, one rollback.

Build on the VPS, native aarch64 — **no shared-tree builds, worktree only**.

### Step 0 — G0: the nestor reader (Ryan owns the flag; the patch owns its own review)

The patch is `tools/lip_v5/g0-nestor-reader.patch`, **NOT APPLIED**. It ships behind
`LIP_CASH_FEED_ENABLED`, default FALSE.

```bash
# 0a. verify it applies, review it (SEPARATE review, per SF-4), then apply:
git apply --check tools/lip_v5/g0-nestor-reader.patch    # MUST pass — part of the read-out
git apply         tools/lip_v5/g0-nestor-reader.patch
cargo test -p engine reconcile          # 19 pass, incl. 7 g0_* tests
sudo systemctl restart nestor
# READ-OUT: with the flag unset, `divergence` is byte-identical to today across >=1 pass.
```
```bash
# 0b. LATER, and only after 0a has been observed clean — flip the flag.
#     Place a HAND-BUILT fixture first so the read-out is checkable against a known number:
cat > ~/nestor/data/lip_cash_feed.json <<'EOF'
{"schema":"lip_cash_feed/1","ts":<now>,"seq":1,"process":"lip_v5","mode":"shared",
 "delta_dollars":-10.00,"pending_payout_dollars":4.00,"components":{},"heartbeat_s":30}
EOF
sudo systemctl edit nestor      # Environment=LIP_CASH_FEED_ENABLED=true
sudo systemctl restart nestor
# READ-OUT, against that fixture:
#   * `expected_cash` moves by exactly -10.00
#   * `pending_payout` moves by exactly +4.00
#   * one `lip_cash_feed OK - seq 1 delta $-10.00 pending $4.00 age Ns` line per pass
#   * remove the fixture; an ABSENT file returns to (0,0) with no page
```
**Rollback:** unset the flag. The reader is inert code until then.

### Step 1 — G1: arm inert (Fable)

```bash
git -C ~/nestor worktree add -b lip-v5-deploy ~/nestor-v5 lip-v5-build
cd ~/nestor-v5/tools && python3 -m unittest discover -s lip_v5/tests -t .
python3 -m lip_v5.lip_v5 --check
```
**READ-OUT:** every line `OK`, including `g0_flag_matches_mode` — `mode=shared` with G0's flag
false is a **startup refusal**, not a warning — and `v4_not_running`.
**Rollback:** delete the unit / worktree. No capital has moved.

### Step 2 — G2: shadow (Fable)

Quote nothing for **≥1 full program period**. Meter PSDH, score venues, publish a zeroed feed.

```bash
python3 -m lip_v5.lip_v5 --shadow --mode shared
```
**READ-OUT:** `venue_rank` lines vs v4's realized accrual; PSDH populated for ≥10 (m,s).
**Rollback:** stop.

### Step 3 — cutover (Ryan) — `--gen-adopt`, then adopt, then TRIAGE

```bash
# 3a. v4 down by its PROVEN path (SIGTERM = cancel-all)
sudo systemctl stop lip-maker-v4

# 3b. v5 GENERATES the adoption file itself, reading v4's ledger READ-ONLY.
#     This is an owned, re-runnable step, NOT a hand entry — a hand-typed position
#     table is the highest-stakes hand entry in the whole program.
python3 -m lip_v5.lip_v5 --gen-adopt
cat ~/nestor/data/lip/v5_adopt.json      # REVIEW IT. This is the gate.
```
> **B7 requires you to say so.** A blank v5 ledger plus an adopt file is exactly the state the
> fresh-state guard refuses — starting flat against a non-flat account. The cutover is the one
> legitimate instance, so it must be STATED (`--allow-fresh`), not inferred from the absence of
> evidence. If that flag surprises you here, stop: it means the ledger is not where you think.

**READ-OUT before proceeding:** every basis in `[0.01, 0.99]`; the position count matches
`GET /portfolio/positions`; no ticker you do not recognise.

At startup v5 then applies the **W2 adoption gate**: the exchange is authoritative on `net`,
v4's ledger on `basis`. Any `net` disagreement is excluded and frozen for quoting **and
recycling**. Any exchange position absent from the file is an `orphan_position` — alerted, and
its market refused for quoting.

Then **CUTOVER TRIAGE** runs once: every adopted position is re-judged against (★) and the
failures are exited. Verdicts are logged as `cutover_triage` rows `{ticker, net_rate, decision,
exit_path}`.

- passes (★) → `keep`, managed normally
- fails, and the shed is cheaper than the spread → `maker_shed`
- fails, and the carry avoided exceeds the spread paid → `taker_cross`

> **`taker_cross` DOES NOT EXECUTE at this step.** Crossing the spread is spec §7's **G6**, a
> separate Ryan-owned gate with its own rollback, and it ships `TAKER_EXIT_ENABLED = False`.
> The triage still COMPUTES the crossing verdict and logs `value_forgone_usd`, then falls back
> to the maker shed — so the choice is measured rather than asserted, and enabling it later is
> one flag. See "Open decisions" below; this is the one item where an instruction to the
> implementor conflicted with the spec, and it was resolved in favour of the gate.

**Rollback:** `v5 SIGTERM` (cancel-all → zeroed cash feed) → `systemctl start lip-maker-v4`.
This is clean **ONLY before the first fill on an adopted position**. v5 logs
`rollback_clean=true|false` every cycle so you never have to guess, and its SIGTERM path
ALWAYS writes `v5_handback.json`. Past the boundary the procedure is
`systemctl start lip-maker-v4 --import-handback`.

### Step 4 — G3: probe capital (Ryan)

**The arm is one hand-written file.** `--live` alone refuses (exit 2); the binary requires the
operator gate artifact, written BY HAND — the file's existence IS the human decision:

```bash
cat > ~/nestor/data/lip/v5_go.json <<'EOF'
{"gate": "G3", "operator": "ryan", "note": "G2 shadow observed clean; probe capital approved",
 "ts": "<date -u>"}
EOF
sudo systemctl start lip-v5        # unit already carries --live; the artifact arms it
```

Rung-0 caps live: floor-clearing probes only, ≤20% of ceiling unverified in total, ≤8
concurrent, ≤2 oversized.
**READ-OUT** on the first `allocate` line: `Σ unverified ≤ 0.20×ceiling`,
`count(unverified) ≤ 8`, `count(probe_oversized) ≤ 2`, and **no venue funded below its
`floor_q`**.
**Rollback:** SIGTERM, then delete `v5_go.json` (un-arms the unit; a restart refuses again).
**The human must be on the ntfy topic BEFORE this step** — the alarm chain needs a human at the
end of it.

### Step 5 — G4: ratchet enable (Ryan)
Allow caps to climb on verified accrual. **READ-OUT:** `ratchet` rows show only `+1` on in-band
verifications. **Rollback:** flag false.

### Step 6 — G5: ceiling rung (Ryan)
One constant, one commit. Each rung funded by the **previous window's observed print, never the
model** (R168). **Rollback:** previous rung.

### Step 7 — G6: taker exit (Ryan)
Enables the crossing exit, including the triage path above. **READ-OUT:** the §5.2 inequality
logged before the first exit. **Rollback:** flag false.

### Step 8 — G7: subaccount (Ryan)
Cash feed → `mode:"subaccount"`. Key-capability probe first (GTC + `expiration_ts` + coid
cancels, one $1 order). **Rollback:** mode shared.

### Step 9 — G8: decommission v4 (Ryan)
After **3 clean v5 settlement days AND v4 verified FLAT** (zero positions, zero resting — the
rows offset cash v4 had CONSUMED, so zeroing them while it still holds inventory creates a false
positive divergence): append one offsetting entry equal to `−Σ(v4-era delta_dollars)` naming the
rows it cancels, then verify `divergence ≈ $0.00`.
**Rollback:** restart v4; the offsetting entry is itself reversed by another append.

---

## Contradictions with note 43 (THE MONEY GAME)

43 is now the transmission layer. Checked the build against it; one real contradiction, one
incompleteness, everything else consistent.

1. **CONTRADICTION, FIXED — §2's sunk-cost rule.** "The exit's price of impatience is spread +
   taker fee; its price of patience is carry. Both are computable, and **entry price belongs in
   neither** (sunk — a rule that anchors on entry cuts winners and rides losers by
   construction)." Two places anchored on entry basis:
   - `cutover.triage_position` computed `hold_cost = n · basis · H_wait · r*`. What holding a
     position rents is the capital recoverable *today* (`n × mark`); the gap to basis is spent
     and no decision can un-spend it. Anchoring on basis systematically overstates the carry of
     underwater positions and so **crosses the spread to escape a sunk number**.
   - `clusters` measured exposure in basis, which made the risk cap *tighten* as positions moved
     against us and *loosen* as they moved for us — the same anchor, in a guard.
   Both now use `mark → p → basis`, the last being the unpriced case only (matching the day
   stop's mark-at-cost). At placement mark == basis, so prospective orders are unaffected.

2. **INCOMPLETENESS, flagged — §7's "fills reduce reward earning TWICE".** (★) prices the first
   ("the capital leaves the book") through `carry_cost` and the presence metric. The second
   ("the exit consumes room") is modelled only qualitatively — a shed order occupies a slot that
   could hold a fresh quote, and v4's "inventory BLOCKS THE SLOT" lineage carries that as prose.
   It is not a term in (★). Under-pricing fills in the *permissive* direction, so it is worth a
   decision rather than a silent omission.

3. **Consistent, checked:** §1 (YES+NO=$1, netting, collateral = price of what you bought);
   §3 (settle-source clusters, and a box nets to riskless — asserted); §4 (maker fees ≡ 0, taker
   only on the crossing exit; adverse selection measured per (m,s) so a trending day's
   one-sided flow shows up); §5 (PSDH, and its zero-fills mirror — P6, now wired through the
   classify sweep's public-tape check);
   §6 (horizon as cost, and the value of paying to exit decaying as settlement nears — the
   triage's `H_wait` produces exactly that); §7 (per-pool saturation: marginal rate → 0 as share
   → 1, so ALLOCATE moves to breadth by itself); §8 (one writer per file, attribution by our own
   ledger, and the cash feed as the "tell the others what you did" mechanism).

## Open decisions for Ryan

Full derivations are in the build report and in the code beside each item.

1. **`r*` fixpoint (D3) — RESOLVED this round.** `RSTAR_MAX_ITERS` is now **9**, the smallest
   value making spec §1.3's own two statements consistent (it covers the 16× seed error §1.3
   names, at the 5% tolerance §1.3 sets). The old 4 could never trip the stop rule, so
   `rstar_no_converge` fired every cycle on a control that was inert.
   Also corrected: the fallback direction. `max(trace)` errs high relative to the **seed**, not
   the **truth** — on a LOW seed the trace ascends and lands *below* the true fixed point,
   pricing carry too cheaply, which is the PayPal direction. §1.4's unverified-exposure cap,
   not this tie-break, is the cold-start guard.
2. **Triage crossing-exit vs G6** — resolved in favour of the gate; see Step 3.
3. **`rung0_cap` units (RD-1)** — spec §1.4 mixes contracts and dollars in one `min`; read as
   dollars, derivation in `ratchet.rung0_cap`.
4. **UNPROBEABLE venues (RD-2)** — if the per-slot or per-market cap falls below `floor_q`, the
   venue is refused rather than funded at a size whose silence we would misread as evidence.

5. **Per-rung cap, DERIVED (charter amendment, this round).** The flat `INV_CAP_USD = $10`
   was inherited, not derived. Per-rung size now comes from (★)'s own share saturation (the
   reward side needs no constant) bounded by `slot_cap = max($10, 0.5 × day_stop)` — the same
   "no single bet may trip the day stop alone" factor as the cluster and series caps, re-derived
   every cycle. $50/rung is reachable exactly when the funded day stop is ≥ $100; the $10
   floor is itself derived (0.5 × the day-stop floor). B9's turnover bound scales with it, so
   the informed-taker blast radius stays proportional.

`grep UNDERIVED config.py` lists everything spec §9 flags as unmeasured.

---

## What is and is not built

**The build is complete, and ALIVE.** Every money rule, all thirteen rails, the scan/classify
sweep, the slot table, the run cycle, **the requoting stage** (the finish-round charter's
finding: v5 previously computed allocations and DROPPED them — `Maker.place()` had zero call
sites), the shed path (triage verdicts + ongoing (★) failure, never crossing, feeding
`l_shed`), venue admission (§1.4 caps bind: unadmitted venues allocate ZERO), restart recovery
of resting orders with the §9.4 prefix sweep, idempotent adoption via `adopt` ledger rows, the
outer systemd loop with always-shutdown and the SF-3 halted-idle, the one path to the wire,
`--gen-adopt`, `--shadow`, `--handback`, `--live` behind the G3 artifact, and the unit file.

**The property to attack:** there is exactly ONE path to the wire (`Maker.place`), it consults
`guards.place_allowed` before spending anything, and it publishes the cash feed before the POST.
`test_engine.py` asserts it on `place()` and `test_runner.py` asserts it on the **assembled
loop** — by driving whole iterations against a venue the allocator ADMITS, asserting the
exchange saw REAL orders (`placed > 0`; the earlier 0 == 0 form is how the missing requoter
passed review), all of them via `Maker.place`. `test_aliveness.py` is the affirmative proof:
orders appear, sheds appear, completions are measured.

**Known gaps, stated rather than hidden:**
- **The §2.5 kill loop (`presence.evaluate_slot`) and the §8.7 collapse predicate are pure and
  tested but not yet driven from the cycle** — the finish-round charter did not enumerate them;
  ALLOCATE's re-run each cycle (a falling T̂ moves the dollars) is the live mechanism meanwhile.
  Same for the `idle_capital` alert and dose-response perturbation.
- **The v1 §3.5-3.7 KEEP/TOP_UP/HOLD/ABANDON decision IS ported** (second charter amendment):
  `alloc.rescue` prices the forfeit cliff — at $0.70 accrued the next $0.30 is worth $1.00+
  (it unlocks the stranded 70¢) — and the forfeit gate applies it: top-up posted when recovery
  beats redeploy + fill cost, abandon when the cliff is unreachable, entry floor untouched at
  zero accrual. Accrual integrates over allocated presence, persists as `accrual` money rows
  (≤60 s crash loss), and survives restart. It runs per cycle from the gate rather than at v4's
  window-fraction checkpoints — a deliberate simplification: the gate already re-runs every
  cycle, so a separate checkpoint scheduler would be a second cadence for the same decision.
- **`venue_reading` is a seam** — the ratchet climbs only when the credits ritual (or a later
  feed) calls it with popover/credit readings.
- **`--shadow` without `--live` runs against a `FakeExchange`** and says so. It rehearses the
  loop's shape; it is not a venue ranking. G2's real read-out needs `--shadow --live`.
- **Note 43 §7's "the exit consumes room"** is modelled qualitatively (inventory blocks the
  slot) but is not a term in (★). See "Contradictions with note 43" below.
