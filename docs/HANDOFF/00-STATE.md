# Nestor — Session Handoff (STATE)

> Written 2026-07-22 so a fresh Claude (new account, SAME Mac) continues seamlessly.
> Read this first, then `01-CONVERSATION.md` (verbatim history). You have local
> access to BOTH locations below. Nothing here is theoretical — it's all built + pushed.

## Who / goal
- User: **Ryan**, US person. This is a **personal** project (umbrella name **Olympus**),
  kept strictly SEPARATE from his company **Stack Integrated** and from his other
  personal life. Separation is a HARD requirement (see "Identity" — there was a
  mistake that was cleaned up).
- **Nestor** = an automated trading system for **Kalshi** retail-priced prediction
  markets. Goal: high daily return by exploiting inefficiencies retail/simple-bots leave.
- **Premise (treat as gospel):** these markets are beatable. NEVER call anything
  "efficient" as a blocker — assume nothing is; hunt for the edge. The novelty of
  Nestor is the EDGES (from the research vault). The risk/sizing/bankroll layers are
  a solved problem — plumbing that must be correct and boring, not where alpha lives.

## The two locations (both on this Mac)
1. **Research vault (Obsidian):** `~/Documents/Obsidian/nestor/` — the edge research.
   Key notes: `00 - START HERE.md`, `12 - Independent Verification (external review).md`,
   `13 - Forward Test (Jul 2026).md` (most recent ground truth), `08 - Broad-Kalshi &
   Cross-Venue.md` (WEATHER edge), `08 - The Lock Edge...`, `09 - Lock Edge - Failure
   Rate & Sizing.md`, `implementation/00 - Implementation Overview.md`.
2. **Code repo (Rust):** `~/Documents/olympus/nestor/` → GitHub **`neotechnites/nestor`** (private).
   Also research data/scripts at `~/kalshi_data/` (231 py scripts, `forward_*.json`).

## The edges (forward-tested 2026-07-15, vault note 13 — real OOS, ~20 days)
| Edge | Verdict | Status |
|---|---|---|
| **Lock / deep-longshot fade** (BTC, off-50) | ✅ HELD 99.3% win, +3.25%/trade | **Signal + backtest BUILT in Rust (T009, `nestor backtest-lock` reproduces 138 trades / 99.28% / +3.26%); live always-on sleeve remains (needs keys/VPS/T011)** |
| **Weather forecast-buy** | ✅ HELD, ~+3.5–5¢/city durable | **BUILT, code-complete** |
| Gold × BTC-drop | ⚠️ DECAYED (still +, at-50, noisy) | NOT building |
| Streak/breadth reversal | ✅ direction only, Kalshi break-even after fees | NOT building (Poly-only) |

- **Lock edge, one sentence:** late in a 15-min BTC up/down market, buy the near-certain
  winner at 93–97¢ and hold — it's ~99% to win but Kalshi's price cap + 60-sec settlement
  average leave it slightly underpriced. Needs live order-book + last-2-min timing (a
  WebSocket poller, always-on) — that's why the VPS matters and why it's the harder build.
- **Kalshi only.** Polymarket was the research LAB, never a trading venue. Kalshi = US-only
  (US KYC, geo-blocked abroad). Global Polymarket bars US persons. Ryan HAS access to the
  new **US-regulated Polymarket** (QCEX/CFTC re-entry), so a Kalshi↔US-Polymarket arb is a
  legitimate FUTURE (both US-regulated) — parked; the one-codebase design absorbs it later.

## Architecture (Rust Cargo workspace — all production is Rust; Python only for research)
```
crates/engine/   shared infra (a lib)
  kalshi.rs      Kalshi client: public market data + RSA-PSS signed orders; balance/positions
  weather.rs     Open-Meteo forecast + IEM actuals feeds
  calibrate.rs   `calibrate` job: per-city bias from Open-Meteo-vs-IEM -> data/biases.json
  reconcile.rs   `reconcile` job: settle open positions vs Kalshi result -> P&L
  selftest.rs    `selftest-order`: live $1 order-path test (T007)
  risk.rs        RiskManager: sizing (flat/fraction), cluster + portfolio caps, kill-switch
  state.rs       StateStore (JsonStore atomic write) — bankroll, open positions, settled
  sizing.rs      contracts_for (integer-cents)
  strategy.rs    Strategy trait + Engine + Engine::execute (THE only order path)
  logging.rs     stdout + JSONL trade log (logs/weather_trades.jsonl)
  alert.rs       best-effort webhook (ALERT_WEBHOOK_URL)
  config.rs      Settings (nestor.toml), City table, apply_biases overlay
crates/weather/  the weather edge (impl Strategy) + probe.rs (probe-weather)
nestor_bin/      the `nestor` binary; subcommands dispatched in main.rs
docs/specs/, docs/tickets/ (markdown board), docs/WORKFLOW.md
```
- **Contract:** strategies emit a `Signal`; `Engine::execute` routes it through the Risk
  layer and places (live) or simulates (paper). Strategies NEVER place raw orders. New edge
  = new crate impl'ing `Strategy`, wired into `nestor_bin`. Adding lock = no engine change.
- **Config flow:** `nestor.toml` (risk params, cities, biases) + env overrides (`NESTOR_ENV`
  paper|live, `NESTOR_BANKROLL`, `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, etc.) +
  `data/biases.json` overlay (from `calibrate`). `.env` holds secrets (gitignored).
- **Subcommands:** **`nestor run`** = the PRODUCTION runtime (one process hosting every
  strategy as tokio tasks sharing ONE in-memory RiskManager — lock 15s loop, weather 9am ET,
  settlement 60s sweep). Also: `weather` (default, one-shot), `lock`/`lock-once`, `calibrate`,
  `reconcile`, `probe-weather`, `backtest-lock`, `selftest-order`, `resume`.
- **CRITICAL: deploy `nestor run` (single process), NOT separate weather/lock/reconcile
  processes.** A code review found that separate processes sharing `state.json` clobber each
  other's writes and bypass the kill-switch (the always-on lock process froze its in-memory
  state). `run` fixes it by keeping everything in one process behind the Mutex. The VPS runs
  ONE `nestor run` service (update T008 from three timers to one service).

## WHERE WE ARE (exact)
- **Weather sleeve is code-complete**: forecast → per-city bias-correct → correct-day 2°F
  bucket → skip wet days → Risk-layer sizing → place bet → `reconcile` settles vs Kalshi →
  bankroll tracked. Runs today in **paper** mode end-to-end. Verified live: `probe-weather`
  (all 8 stations OK), `calibrate` (real biases written), weather run applies them.
- **Just finished a full senior code review** (my own; the 4 parallel review agents died on
  the org spend limit, since raised). **Fixed 10 of 11 findings** across 3 gated commits
  (`418d605`, `275cbf2`, + earlier): HTTP timeouts, deterministic idempotent `client_order_id`,
  integer-cents sizing, portfolio-wide exposure cap, bounded settled history, alerting,
  `resume` command, log-write failures surfaced, stale-bias warning.
- **Deferred (with cause): T011 fill-verification.** `Engine::execute` currently assumes an
  order fully fills at the limit; the correct fix reads the ACTUAL fill from Kalshi's order
  response, whose schema we can't confirm until a real order runs (T007). Safe at $10
  hand-watched; REQUIRED before unattended or lock. See `docs/tickets/T011-fill-verification.md`.
- **RSA signing** looks correct but is UNVERIFIED without keys — it fails safe (bad signing =
  orders rejected, no money lost). `selftest-order` (T007) is the verification.
- git: `master` @ `275cbf2` (as of handoff). Tickets T001–T010 done; T006 (CI) done;
  T007 built (awaits keys); T011 todo (gated on T007); T008 (VPS) needs a provider pick;
  T009 (lock edge) is the next epic.

## NEXT STEPS (in order)
1. **Ryan provides Kalshi API keys + funds ~$50** (his action). Put `KALSHI_API_KEY_ID` +
   the RSA `.pem` path in `.env`.
2. **Run `nestor selftest-order <ticker> <price>` (T007)** — proves auth/signing/order path;
   CAPTURE the real order-response JSON, then finalize **T011** (fill verification) against it.
3. **Go live at $10, hand-watched**: `NESTOR_ENV=live`, `NESTOR_STAKE_USD=10`,
   `NESTOR_MAX_DAILY_USD=10`; run `calibrate` then `nestor weather` at ~9am ET on the Mac;
   next morning `nestor reconcile`. Confirm real fills in the Kalshi UI.
4. **US-region VPS + 9am-ET cron (T008)** for unattended daily runs; wire `deploy.yml`
   (add repo secrets `VPS_HOST/VPS_USER/VPS_SSH_KEY`). VPS MUST present as US (Kalshi is US-only).
5. **Lock edge (T009)** — the real money. Own spec; WebSocket market data + Z calc + last-2-min
   entry + always-on service. Portfolio cap already in place for when it runs alongside weather.
- Scaling gates ($10→$100→$1000) are evidence-based (fills clean + live EV ≈ research + ≥2
  weather regimes). Weather is capacity-limited (~low-thousands total). Bigger money = lock.

## Identity / accounts (CRITICAL — keep personal separate from Stack Integrated)
- Repo is on personal GitHub **`neotechnites/nestor`**, bound to a **dedicated SSH key**
  `~/.ssh/olympus_ed25519` via host alias **`github-olympus`** in `~/.ssh/config`. Push with
  `git push` over that remote (`git@github-olympus:neotechnites/nestor.git`).
- **The `gh` CLI on this Mac is still logged in as the COMPANY account `RyanStackIntegrated`.**
  Do NOT use `gh` for this repo (it would hit the company account — that mistake happened once:
  a repo was created under RyanStackIntegrated + commits stamped `ryan@stackintegrated.com`;
  it was DELETED and local history scrubbed). Use git+SSH only. Local repo git identity is a
  placeholder `Ryan <ryan@olympus.local>` (update if Ryan gives a real personal email).
- Ryan bought a personal domain for Olympus and set up catch-all email forwarding (Cloudflare)
  to a personal inbox; created the `neotechnites` GitHub with an @domain address.
- **PRs:** Ryan opens/merges them in the browser (his Olympus Chrome profile) — I can't via
  `gh`. He authorized pushing FOUNDATIONAL work straight to `master` ("this is the base, I'll
  review in the future"); genuinely new/edge work should be PRs. He CANNOT read Rust, so
  **green CI + my own rigorous self-review are the correctness gate**, not his line review.

## Workflow + tooling
- Spec-driven: `docs/specs/`, `docs/tickets/` (markdown board + one file/ticket), `docs/WORKFLOW.md`.
- **Gate before ANY commit:** `source "$HOME/.cargo/env"` then `cargo fmt --check`,
  `cargo clippy --all-targets -- -D warnings`, `cargo test`, `cargo build --release`. All green.
- CI runs the same on PRs (`.github/workflows/ci.yml`); `deploy.yml` ships to the VPS (stub).
- Rust installed via rustup (`~/.cargo`). `reqwest` uses rustls (works, even through the env's
  intercepting proxy). Python `urllib` SSL is broken on this Mac — research scripts use `curl`.
- **API facts learned:** IEM `daily.json` ignores sdate/edate, returns full history ascending,
  fields `date` + `max_tmpf`; stations are 3-letter (MIA/ATL/NYC/BOS/PHX/MDW/DEN/SEA, no K).
  Kalshi series `KXHIGH*`/`KXHIGHT*` mix is real+correct. Kalshi `yes_ask_dollars` is a STRING;
  `result` is "yes"/"no"/empty. Open-Meteo needs `temperature_unit=fahrenheit`.

## Subagents (state)
- **NONE running now.** The 4 full-repo review agents (money-safety, concurrency, API-integration,
  architecture) all FAILED early on the org monthly spend limit (now raised) — I did the review
  myself instead. Their partial threads are folded into the review already applied.
- Prior COMPLETED subagents and where their output lives:
  - 4 backtest agents (lock/weather/gold/streak) → vault note 13 + `~/kalshi_data/forward_*.json`.
  - 3 build agents (T003 calibrate, T004 reconcile, T005 probe) → merged to `master`.
- Parallelism rule: fan out worktree-isolated agents ONLY for tickets that touch disjoint files;
  the engine's hot files (`risk.rs`, `strategy.rs`, `main.rs`) serialize, so those go sequential.

## Ryan's working style (from the session — follow exactly)
- **Brief.** A paragraph, not an essay. No multi-table walls for simple questions.
- **A question is a question, not a cue to act** — answer it, then wait; but during a run he
  already authorized, keep going, don't add caution.
- **Banned words/hedging:** never say "efficient" (as a blocker), "honestly", "to be clear".
- **Don't be a blocker; go fix things autonomously** once authorized ("just go fix stuff").
- He can't read Rust; he relies on CI + my self-review. He is the final PR merger.
- Separation of personal/work/other-personal is non-negotiable.
```
