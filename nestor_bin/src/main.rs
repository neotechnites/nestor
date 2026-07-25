//! Nestor entrypoint. Loads config (nestor.toml + env), builds the shared Engine
//! (Kalshi client + Risk layer), and runs the selected subcommand.
//!
//! PRODUCTION = `nestor run`: streak scanner (15s) + settlement sweep (60s) in
//! one process (redirect 2026-07-23). Lock (decay-dead) and weather (unverdicted)
//! are PARKED — their subcommands remain for manual/re-entry checks, but nothing
//! schedules them.
//!
//! Usage: `nestor <run|streak|streak-once|calibrate|reconcile|probe-weather|
//!                 backtest-lock|selftest-order|resume|weather|lock|lock-once>`

use anyhow::{Context, Result};
use engine::config::{self, Settings};
use engine::state::JsonStore;
use engine::{Engine, Mode, RiskManager, Strategy};

#[tokio::main]
async fn main() -> Result<()> {
    dotenvy::dotenv().ok();

    let mut settings = Settings::load(&env_str("NESTOR_CONFIG", "nestor.toml"))?;

    // Overlay calibrated per-city biases (from `calibrate`) over the config
    // placeholders, so the bot bets on the bias-corrected forecast. No-op if the
    // biases file is absent. Does not change which cities are tradeable.
    let biases_path = env_str("NESTOR_BIASES_PATH", "data/biases.json");
    let applied = engine::config::apply_biases(&mut settings.cities, &biases_path);
    if applied > 0 {
        eprintln!("nestor: applied {applied} calibrated city biases");
        if let Some(days) = biases_age_days(&biases_path) {
            if days > 14 {
                eprintln!(
                    "nestor: WARNING calibrated biases are {days} days old — run `nestor calibrate`"
                );
            }
        }
    }

    // No default subcommand: with lock/weather parked and streak live-gated,
    // a bare invocation should never silently pick a strategy.
    let which = std::env::args().nth(1).context(
        "usage: nestor <run|streak|streak-once|volbook|volbook-once|house|house-once|\
         house-report|calibrate|reconcile|probe-weather|backtest-lock|selftest-order|\
         resume|weather|lock|lock-once>",
    )?;

    // `backtest-lock` re-confirms the (parked) lock edge in-code against cached
    // data — kept as a re-entry check. Read-only, no keys, no engine.
    if which == "backtest-lock" {
        return lock::backtest::run();
    }

    // `house-report` summarizes the four house-probe metrics from the
    // participation log. Read-only, no keys, no engine.
    if which == "house-report" {
        let path = env_str("HOUSE_LOG_PATH", house::strategy::LOG);
        return house::report::run(&path);
    }

    // `calibrate` is a maintenance job (not a strategy): it needs neither the
    // Kalshi client nor the risk layer, so handle it before building the Engine.
    if which == "calibrate" {
        let out = env_str("NESTOR_BIASES_PATH", "data/biases.json");
        return engine::calibrate::run(&settings, 60, &out).await;
    }

    // Read-only reality check for the weather config (T005). No orders, no risk
    // layer, no state — just probes Kalshi + IEM and prints a report.
    if which == "probe-weather" {
        let kalshi = engine::Kalshi::public();
        let http = engine::http_client();
        return weather::probe::run(&kalshi, &http, &settings.cities).await;
    }

    // Live order-path self-test (T007): places ONE tiny real order to prove auth
    // + signing + order placement before any strategy trades live. Needs keys.
    // Usage: nestor selftest-order <ticker> <yes_price_cents> [count]
    //
    // OPERATOR-ONLY: this places a real order that BYPASSES the risk layer
    // entirely (no sizing, no caps, no kill-switch, no state) — it talks straight
    // to the Kalshi client. Only run it by hand with a tiny known price/count to
    // validate plumbing; never wire it into an automated path.
    if which == "selftest-order" {
        let ticker = std::env::args()
            .nth(2)
            .context("usage: nestor selftest-order <ticker> <yes_price_cents> [count]")?;
        let price: i64 = std::env::args()
            .nth(3)
            .context("need <yes_price_cents>")?
            .parse()
            .context("yes_price_cents must be an integer 1..=99")?;
        let count: i64 = std::env::args()
            .nth(4)
            .and_then(|s| s.parse().ok())
            .unwrap_or(1);
        let side = std::env::args().nth(5).unwrap_or_else(|| "yes".into());
        let key_id = std::env::var("KALSHI_API_KEY_ID").context("KALSHI_API_KEY_ID required")?;
        let key_path =
            std::env::var("KALSHI_PRIVATE_KEY_PATH").context("KALSHI_PRIVATE_KEY_PATH required")?;
        let kalshi = engine::Kalshi::authenticated(key_id, &key_path)?;
        return engine::selftest::run(&kalshi, &ticker, price, count, &side).await;
    }

    // Secrets + mode come from env (env wins over the file's default).
    let mode = Mode::from_env(&std::env::var("NESTOR_ENV").unwrap_or(settings.trading.env.clone()));
    let live = mode == Mode::Live;

    // GATE STANDALONE STRATEGIES OUT OF LIVE (fix 6): a standalone strategy loop
    // has NO reconcile task, so there is NO kill-switch (settlement never runs
    // intraday and the divergence/orphan checks never fire). Only `run` wires the
    // reconcile task. Paper standalone stays allowed. `streak-once`/`lock-once`
    // are gated too — they still place a real order with no kill-switch.
    if live
        && matches!(
            which.as_str(),
            "streak"
                | "streak-once"
                | "lock"
                | "lock-once"
                | "weather"
                | "volbook"
                | "volbook-once"
                | "house"
                | "house-once"
        )
    {
        anyhow::bail!(
            "`{which}` standalone is disabled in live mode — use `run` (the kill-switch \
             requires the reconcile task). Paper standalone is still allowed."
        );
    }

    // SEED PINNING (fix 4): live bankroll MUST be explicit (env or config) and
    // within the hard cap; paper falls back to the built-in default. Refuses to
    // start on a silent/oversized seed.
    let env_bankroll = std::env::var("NESTOR_BANKROLL")
        .ok()
        .and_then(|v| v.parse::<f64>().ok());
    let bankroll = config::resolve_bankroll(live, env_bankroll, settings.trading.bankroll)?;

    // Single-writer lock: only one process may hold the state file. Refuses to
    // start if another nestor (e.g. a stray `lock` or `weather`) is already writing
    // it — that would clobber state and bypass the kill-switch. Held for the whole
    // process via `_state_lock`.
    let state_path = env_str("NESTOR_STATE_PATH", "data/state.json");
    let _state_lock = acquire_state_lock(&state_path)?;
    let store = Box::new(JsonStore::new(state_path));
    // STATE INTEGRITY (fix 3b): in live, a MISSING state file is refused unless the
    // operator explicitly passes `--fresh-state` (accepting a fresh ledger).
    let allow_fresh = std::env::args().any(|a| a == "--fresh-state");
    let mut risk = RiskManager::load_or_init(settings.risk, store, bankroll, live, allow_fresh)?;

    // `resume` clears a persisted kill-switch halt (operator action after review).
    if which == "resume" {
        risk.resume();
        let st = risk.status();
        println!(
            "halt cleared — bankroll ${:.2} drawdown {:.1}% halted={}",
            st.bankroll,
            st.drawdown * 100.0,
            st.halted
        );
        return Ok(());
    }

    let kalshi = if mode == Mode::Live {
        let key_id =
            std::env::var("KALSHI_API_KEY_ID").context("KALSHI_API_KEY_ID required for live")?;
        let key_path = std::env::var("KALSHI_PRIVATE_KEY_PATH")
            .context("KALSHI_PRIVATE_KEY_PATH required for live")?;
        engine::Kalshi::authenticated(key_id, &key_path)?
    } else {
        engine::Kalshi::public()
    };

    let eng = Engine::new(kalshi, engine::http_client(), mode, risk, settings.cities);

    // `reconcile` is not a strategy: it closes open positions against Kalshi's
    // settled result and realizes P&L (T004). Everything else is a strategy.
    if which == "reconcile" {
        return engine::reconcile::run(&eng).await;
    }

    // `run` = the production runtime: ONE process, tokio tasks over ONE in-memory
    // RiskManager (no second state.json writer; kill-switch honored everywhere;
    // settlement runs intraday so same-day losses feed the daily-loss halt).
    // Per the 2026-07-23 redirect it schedules STREAK ONLY — lock and weather are
    // parked and never scheduled here.
    if which == "run" {
        return run_all(eng).await;
    }

    // Streak standalone: `streak` loops the scan at the adaptive cadence (1s in
    // entry windows, lazy outside; no settlement task — use `run` in
    // production); `streak-once` runs a single pass for testing.
    if which == "streak" || which == "streak-once" {
        let strat = streak::strategy::Streak::new();
        let mut backoff = 0u32;
        loop {
            let mut retry_after = 0u64;
            match strat.run(&eng).await {
                Ok(()) => backoff = 0,
                Err(e) => {
                    eprintln!("streak: scan error: {e}");
                    // Honor a server-sent Retry-After (item 3) if the 429 carried it.
                    retry_after = engine::net::retry_after_secs_in_message(&e.to_string())
                        .unwrap_or(0);
                    // Only rate-limit/server-class errors back off (fix 5).
                    backoff = next_backoff(&e, backoff);
                }
            }
            if which == "streak-once" {
                break;
            }
            let base =
                streak::strategy::next_poll_delay(chrono::Utc::now().timestamp());
            tokio::time::sleep(backoff_sleep(base, backoff, retry_after)).await;
        }
        return Ok(());
    }

    // Volbook standalone: paper-mode inspection (`volbook` loops at 60s,
    // `volbook-once` single pass; live standalone banned above). PRODUCTION
    // volbook runs inside `run` (scheduled 2026-07-25) where real orders
    // additionally require VOLBOOK_LIVE=1.
    if which == "volbook" || which == "volbook-once" {
        let strat = volbook::strategy::Volbook::new()?;
        engine::logging::info(format!("volbook — {}", strat.universe_summary()));
        loop {
            if let Err(e) = strat.run(&eng).await {
                eprintln!("volbook: scan error: {e}");
            }
            if which == "volbook-once" {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_secs(60)).await;
        }
        return Ok(());
    }

    // House fill-probe (maker sleeve): standalone is paper/SHADOW-only (live
    // standalone banned above; production runs inside `run` under HOUSE_PROBE=1).
    // `house` loops the quote/fill/markout pass at 3s; `house-once` is a single
    // pass. A ctrl-c handler cancels every resting house order on shutdown
    // (charter §2) — belt-and-suspenders, since expiration_ts auto-cancels in 75s.
    if which == "house" || which == "house-once" {
        let strat = house::House::new();
        engine::logging::info(format!(
            "house probe — mode={:?} live_orders={} (HOUSE_PROBE={})",
            eng.mode,
            eng.mode == Mode::Live && std::env::var("HOUSE_PROBE").ok().as_deref() == Some("1"),
            std::env::var("HOUSE_PROBE").unwrap_or_default()
        ));
        if which == "house-once" {
            if let Err(e) = strat.run(&eng).await {
                eprintln!("house: scan error: {e}");
            }
            return Ok(());
        }
        loop {
            // ctrl-c races the scan+sleep: on shutdown, sweep every resting house
            // order before exiting (charter §2). `&eng` only — no move.
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {
                    engine::logging::info("house: ctrl-c — sweeping resting orders before exit");
                    house::House::cancel_all_house_orders(&eng).await;
                    return Ok(());
                }
                _ = async {
                    if let Err(e) = strat.run(&eng).await {
                        eprintln!("house: scan error: {e}");
                    }
                    tokio::time::sleep(std::time::Duration::from_secs(3)).await;
                } => {}
            }
        }
    }

    // PARKED sleeves — manual invocation only, nothing schedules them.
    // lock: decay-dead (kill-scan +1.72¢→−1.07¢/contract); kept for re-entry checks.
    // weather: unverdicted (forward capture running, ~3-4 wks); do not calibrate/run
    // for production until the vault verdicts TRADE.
    if which == "lock" || which == "lock-once" {
        let strat = lock::strategy::Lock;
        loop {
            if let Err(e) = strat.run(&eng).await {
                eprintln!("lock: scan error: {e}");
            }
            if which == "lock-once" {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_secs(15)).await;
        }
        return Ok(());
    }

    let strat: Box<dyn Strategy> = match which.as_str() {
        "weather" => Box::new(weather::Weather),
        other => anyhow::bail!("unknown strategy: {other}"),
    };

    strat.run(&eng).await
}

fn env_str(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.into())
}

/// Next backoff step for a loop error (fix 5): increment only for retryable
/// (429/5xx) statuses so a rate-limit/outage backs off exponentially; any other
/// error resets to 0 (backoff is for bans/outages, not logic errors).
fn next_backoff(err: &anyhow::Error, current: u32) -> u32 {
    match engine::net::http_status(err) {
        Some(s) if engine::net::is_retryable_status(s) => current.saturating_add(1),
        _ => 0,
    }
}

/// Combined loop sleep (item 3): the larger of the adaptive poll `base`, the
/// exponential backoff for `backoff` consecutive retryable errors, and any
/// server-sent `Retry-After` (whole seconds). Honoring Retry-After keeps a
/// rate-limit ban from being prolonged by retrying before the server said to.
fn backoff_sleep(
    base: std::time::Duration,
    backoff: u32,
    retry_after_secs: u64,
) -> std::time::Duration {
    base.max(engine::net::backoff_delay(backoff))
        .max(std::time::Duration::from_secs(retry_after_secs))
}

/// The production runtime: one process, one shared in-memory RiskManager, every
/// strategy as a tokio task. No cross-process state race; kill-switch honored by all.
async fn run_all(eng: Engine) -> Result<()> {
    use engine::Strategy;
    use futures::FutureExt;
    use std::time::Duration;

    let eng = std::sync::Arc::new(eng);
    engine::logging::info("nestor run — streak (adaptive 1s-in-window/12s-lazy) + volbook (60s, VOLBOOK_LIVE-gated) + settlement (60s) + nightly compression, one process");

    // Settlement: sweep every 60s so streak's 15-min markets settle intraday
    // (same trading day -> losses feed the daily-loss kill-switch). Each
    // iteration is panic-caught so one bad cycle can't silently kill the loop
    // (which would disable the kill-switch).
    {
        let e = eng.clone();
        tokio::spawn(async move {
            let mut backoff = 0u32;
            loop {
                let r = std::panic::AssertUnwindSafe(engine::reconcile::run(&e))
                    .catch_unwind()
                    .await;
                // reconcile's exchange-truth pass uses SIGNED calls (positions/
                // balance): feed the consecutive-failure + 401 breakers (fix 5,
                // addendum #3) and back off on rate-limit/server-class errors.
                let mut retry_after = 0u64;
                match r {
                    Ok(Ok(())) => {
                        e.note_signed_success();
                        backoff = 0;
                    }
                    Ok(Err(err)) => {
                        eprintln!("settlement task error: {err}");
                        let status = engine::net::http_status(&err);
                        e.note_signed_failure(status).await;
                        retry_after =
                            engine::net::retry_after_secs_in_message(&err.to_string())
                                .unwrap_or(0);
                        backoff = next_backoff(&err, backoff);
                    }
                    Err(_) => eprintln!("settlement task PANICKED — continuing"),
                }
                tokio::time::sleep(backoff_sleep(Duration::from_secs(60), backoff, retry_after))
                    .await;
            }
        });
    }

    // Volbook (strategy #2, metal daily-wing seller): 60s-cadence scan task —
    // scheduled in `run` as of 2026-07-25 (Ryan authorized implementation; sizing
    // derivation in enchiridion R148). Real orders ADDITIONALLY require
    // VOLBOOK_LIVE=1 (the strategy's own gate) — live without the flag
    // shadow-logs would-be entries; paper mode simulates. Panic-caught like the
    // other loops.
    {
        let e = eng.clone();
        tokio::spawn(async move {
            let strat = match volbook::strategy::Volbook::new() {
                Ok(s) => s,
                Err(err) => {
                    eprintln!("volbook: failed to load calibration — NOT scheduling: {err}");
                    return;
                }
            };
            engine::logging::info(format!("volbook scheduled — {}", strat.universe_summary()));
            loop {
                let r = std::panic::AssertUnwindSafe(strat.run(&e)).catch_unwind().await;
                match r {
                    Ok(Err(err)) => eprintln!("volbook task error: {err}"),
                    Err(_) => eprintln!("volbook task PANICKED — continuing"),
                    _ => {}
                }
                tokio::time::sleep(Duration::from_secs(60)).await;
            }
        });
    }

    // House fill-probe (maker sleeve): scheduled in `run` ONLY when HOUSE_PROBE=1
    // (charter §5). At 3s cadence it quotes/detects fills/marks out; real orders
    // require mode==Live AND HOUSE_PROBE=1 (the strategy's own gate) — otherwise it
    // SHADOW-logs. On startup it sweeps orphan resting orders; expiration_ts (75s)
    // is the load-bearing auto-cancel. Panic-caught like the other loops.
    if std::env::var("HOUSE_PROBE").ok().as_deref() == Some("1") {
        let e = eng.clone();
        tokio::spawn(async move {
            let strat = house::House::new();
            engine::logging::info("house probe scheduled — HOUSE_PROBE=1 (maker two-sided quotes)");
            loop {
                let r = std::panic::AssertUnwindSafe(strat.run(&e)).catch_unwind().await;
                match r {
                    Ok(Err(err)) => eprintln!("house task error: {err}"),
                    Err(_) => eprintln!("house task PANICKED — continuing"),
                    _ => {}
                }
                tokio::time::sleep(Duration::from_secs(3)).await;
            }
        });
    }

    // Nightly compression: gzip yesterday's (and older) dated observation logs
    // (DATA CAPTURE 4 — keep everything, delete nothing; 10-20x shrink). Checks
    // hourly; only compresses files whose date < today, so live files are never
    // touched. Panic-caught like the other loops so a bad cycle can't kill it.
    tokio::spawn(async move {
        loop {
            let r = std::panic::AssertUnwindSafe(async { compress_old_obs_logs() })
                .catch_unwind()
                .await;
            if r.is_err() {
                eprintln!("compress task PANICKED — continuing");
            }
            tokio::time::sleep(Duration::from_secs(3600)).await;
        }
    });

    // Streak: continuous scanner in the foreground (keeps the process alive) at
    // the adaptive cadence — 1s inside each 60s entry window (60 looks at the
    // ask vs 4 at lock's old 15s), lazy ~12s outside, never oversleeping a
    // boundary. Lock (decay-dead) and weather (unverdicted) are parked — NOT
    // spawned.
    let streak = streak::strategy::Streak::new();
    let mut backoff = 0u32;
    loop {
        let r = std::panic::AssertUnwindSafe(streak.run(&eng))
            .catch_unwind()
            .await;
        let mut retry_after = 0u64;
        match &r {
            Ok(Ok(())) => backoff = 0,
            Ok(Err(err)) => {
                retry_after =
                    engine::net::retry_after_secs_in_message(&err.to_string()).unwrap_or(0);
                backoff = next_backoff(err, backoff);
            }
            Err(_) => {}
        }
        report("streak", r);
        let base = streak::strategy::next_poll_delay(chrono::Utc::now().timestamp());
        tokio::time::sleep(backoff_sleep(base, backoff, retry_after)).await;
    }
}

/// Gzip dated observation logs older than today (`data/obs/YYYY-MM-DD.jsonl`).
/// Shells out to the system `gzip` (present on macOS + Linux) — no extra deps.
/// Idempotent: already-compressed files end in .gz and are skipped.
fn compress_old_obs_logs() {
    let today = chrono::Utc::now().format("%Y-%m-%d").to_string();
    let dir = std::path::Path::new("data/obs");
    let Ok(entries) = std::fs::read_dir(dir) else {
        return; // no obs dir yet
    };
    for e in entries.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        // only dated .jsonl files strictly older than today
        if let Some(date) = name.strip_suffix(".jsonl") {
            if date.len() == 10 && date < today.as_str() {
                let path = e.path();
                match std::process::Command::new("gzip")
                    .arg("-f")
                    .arg(&path)
                    .status()
                {
                    Ok(s) if s.success() => {
                        engine::logging::info(format!("compressed {}", path.display()))
                    }
                    Ok(s) => eprintln!("gzip {} exited {s}", path.display()),
                    Err(err) => eprintln!("gzip {} failed: {err}", path.display()),
                }
            }
        }
    }
}

/// Log a supervised task iteration; a caught panic lets the loop survive.
fn report(task: &str, r: std::thread::Result<Result<()>>) {
    match r {
        Ok(Ok(())) => {}
        Ok(Err(err)) => eprintln!("{task} task error: {err}"),
        Err(_) => eprintln!("{task} task PANICKED — continuing"),
    }
}

/// Exclusive single-writer lock on the state file's `.lock` sibling. Refuses to
/// start if another nestor already holds it. The returned File must be kept alive
/// for the whole process (dropping it releases the lock).
fn acquire_state_lock(state_path: &str) -> Result<std::fs::File> {
    use fs2::FileExt;
    let lock_path = format!("{state_path}.lock");
    if let Some(dir) = std::path::Path::new(&lock_path).parent() {
        std::fs::create_dir_all(dir).ok();
    }
    let f = std::fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(&lock_path)
        .with_context(|| format!("opening state lock {lock_path}"))?;
    f.try_lock_exclusive().map_err(|_| {
        anyhow::anyhow!(
            "another nestor process holds the state lock ({lock_path}) — only one writer allowed"
        )
    })?;
    Ok(f)
}

/// Age of the biases file in whole days, or None if it doesn't exist / unreadable.
fn biases_age_days(path: &str) -> Option<u64> {
    let modified = std::fs::metadata(path).ok()?.modified().ok()?;
    Some(modified.elapsed().ok()?.as_secs() / 86_400)
}
