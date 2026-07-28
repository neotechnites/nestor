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
| `config.py` | 458 | every constant, with its spec derivation AND its note-23 §IV mirror answer |
| `money.py` | 410 | **(★)**, `L_eff` and the past-due escalation, φ/d estimation, the `r*` fixpoint, shading, dose-response |
| `presence.py` | 377 | the 1 Hz meter, PSDH/T̂, per-slot kill, the four-branch collapse predicate, compaction |
| `ratchet.py` | 370 | verified-accrual ladder, `floor_q`, admission bounds, OUT_OF_REACH, stand-down, revive |
| `cashfeed.py` | 419 | the computed cash feed and its one invariant |
| `alloc.py` | 450 | ALLOCATE under (★) — v4's water-filling with exactly one substitution |
| `ratelimit.py` | 265 | token bucket, AIMD, lanes, the SF-1 cancel bound, the degrade ladder |
| `cutover.py` | 429 | `--gen-adopt`, the W2 adoption gate, **cutover triage**, handback/rollback |
| `ledger.py` | 131 | the money record, and the separate presence file + compaction |
| `wsgate.py` | 156 | the W2 3-agreement gate over the vendored feed |
| `ws_feed.py` | 1221 | **vendored verbatim from v4** — see below |
| `runtime.py` | 363 | the only clock, the only logger, every external effect behind a stubbable seam |
| `lip_v5.py` | 296 | the binary; note 23 §III's five answered in its header |
| `tests/` | 2120 | 242 tests, `python3 -m unittest` green |

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

242 tests, ~0.05 s, no network, no filesystem outside the tmpdir, no possibility of paging.

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
# 0a. review + apply the patch (SEPARATE review, per SF-4), then:
cargo test -p engine reconcile
sudo systemctl restart nestor
# READ-OUT: with the flag unset, `divergence` is byte-identical to today across >=1 pass.
```
```bash
# 0b. LATER, and only after 0a has been observed clean — flip the flag:
sudo systemctl edit nestor      # Environment=LIP_CASH_FEED_ENABLED=true
sudo systemctl restart nestor
# READ-OUT: `expected_cash` moves by exactly the feed's `delta_dollars`.
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

Rung-0 caps live: floor-clearing probes only, ≤20% of ceiling unverified in total, ≤8
concurrent, ≤2 oversized.
**READ-OUT** on the first `allocate` line: `Σ unverified ≤ 0.20×ceiling`,
`count(unverified) ≤ 8`, `count(probe_oversized) ≤ 2`, and **no venue funded below its
`floor_q`**.
**Rollback:** SIGTERM.
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

## Open decisions for Ryan

Full derivations are in the build report and in the code beside each item.

1. **`r*` fixpoint never converges as specified (D3, surfaced).** Spec §1.3's "4 iterations
   covers a 16× seed error" is true of the *residual* (verified: exactly 16.0×), but the *stop
   rule* is a 5% relative step change, which needs `log2(e/0.05)` steps — 5 for a 2× seed, 9 for
   16×. So the fixpoint always falls back to `max(r*_0..r*_4)` and `rstar_no_converge` fires
   every cycle. **This is safe** (the fallback errs high in both regimes, and a higher `r*`
   always allocates less), but it is not adaptive and it will produce alarm fatigue. Two
   one-line fixes: `RSTAR_MAX_ITERS = 9`, or make the stop rule a residual test. **Constants
   ship unchanged pending your call.**
2. **Triage crossing-exit vs G6** — resolved in favour of the gate; see Step 3.
3. **`rung0_cap` units (RD-1)** — spec §1.4 mixes contracts and dollars in one `min`; read as
   dollars, derivation in `ratchet.rung0_cap`.
4. **UNPROBEABLE venues (RD-2)** — if the per-slot or per-market cap falls below `floor_q`, the
   venue is refused rather than funded at a size whose silence we would misread as evidence.

`grep UNDERIVED config.py` lists everything spec §9 flags as unmeasured.

---

## What is NOT built

The quoting/metering **loop** is not wired — `--shadow` and the live path are the G2/G3 gates,
and each is a separate human call by design. What IS built and tested is every money rule,
every guard, every state machine and every file format that loop would use. The loop is
plumbing over a proven core; the core is the part that must be right before capital moves.
