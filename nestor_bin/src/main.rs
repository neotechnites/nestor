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

use std::sync::Mutex;

use anyhow::{Context, Result};
use engine::config::Settings;
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
        "usage: nestor <run|streak|streak-once|calibrate|reconcile|probe-weather|\
         backtest-lock|selftest-order|resume|weather|lock|lock-once>",
    )?;

    // `backtest-lock` re-confirms the (parked) lock edge in-code against cached
    // data — kept as a re-entry check. Read-only, no keys, no engine.
    if which == "backtest-lock" {
        return lock::backtest::run();
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
        let key_id = std::env::var("KALSHI_API_KEY_ID").context("KALSHI_API_KEY_ID required")?;
        let key_path =
            std::env::var("KALSHI_PRIVATE_KEY_PATH").context("KALSHI_PRIVATE_KEY_PATH required")?;
        let kalshi = engine::Kalshi::authenticated(key_id, &key_path)?;
        return engine::selftest::run(&kalshi, &ticker, price, count).await;
    }

    // Secrets + mode come from env (env wins over the file's default).
    let mode = Mode::from_env(&std::env::var("NESTOR_ENV").unwrap_or(settings.trading.env.clone()));
    let bankroll = env_f64("NESTOR_BANKROLL", settings.trading.bankroll);

    // Single-writer lock: only one process may hold the state file. Refuses to
    // start if another nestor (e.g. a stray `lock` or `weather`) is already writing
    // it — that would clobber state and bypass the kill-switch. Held for the whole
    // process via `_state_lock`.
    let state_path = env_str("NESTOR_STATE_PATH", "data/state.json");
    let _state_lock = acquire_state_lock(&state_path)?;
    let store = Box::new(JsonStore::new(state_path));
    let mut risk = RiskManager::load_or_init(settings.risk, store, bankroll)?;

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

    let eng = Engine {
        kalshi,
        http: engine::http_client(),
        mode,
        risk: Mutex::new(risk),
        cities: settings.cities,
        exec_lock: tokio::sync::Mutex::new(()),
    };

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
        loop {
            if let Err(e) = strat.run(&eng).await {
                eprintln!("streak: scan error: {e}");
            }
            if which == "streak-once" {
                break;
            }
            tokio::time::sleep(streak::strategy::next_poll_delay(
                chrono::Utc::now().timestamp(),
            ))
            .await;
        }
        return Ok(());
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

fn env_f64(key: &str, default: f64) -> f64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn env_str(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.into())
}

/// The production runtime: one process, one shared in-memory RiskManager, every
/// strategy as a tokio task. No cross-process state race; kill-switch honored by all.
async fn run_all(eng: Engine) -> Result<()> {
    use engine::Strategy;
    use futures::FutureExt;
    use std::time::Duration;

    let eng = std::sync::Arc::new(eng);
    engine::logging::info("nestor run — streak (adaptive 1s-in-window/12s-lazy) + settlement (60s) + nightly compression, one process");

    // Settlement: sweep every 60s so streak's 15-min markets settle intraday
    // (same trading day -> losses feed the daily-loss kill-switch). Each
    // iteration is panic-caught so one bad cycle can't silently kill the loop
    // (which would disable the kill-switch).
    {
        let e = eng.clone();
        tokio::spawn(async move {
            loop {
                let r = std::panic::AssertUnwindSafe(engine::reconcile::run(&e))
                    .catch_unwind()
                    .await;
                report("settlement", r);
                tokio::time::sleep(Duration::from_secs(60)).await;
            }
        });
    }

    // Nightly compression: gzip yesterday's (and older) dated observation logs
    // (DATA CAPTURE 4 — keep everything, delete nothing; 10-20x shrink). Checks
    // hourly; only compresses files whose date < today, so live files are never
    // touched.
    tokio::spawn(async move {
        loop {
            compress_old_obs_logs();
            tokio::time::sleep(Duration::from_secs(3600)).await;
        }
    });

    // Streak: continuous scanner in the foreground (keeps the process alive) at
    // the adaptive cadence — 1s inside each 60s entry window (60 looks at the
    // ask vs 4 at lock's old 15s), lazy ~12s outside, never oversleeping a
    // boundary. Lock (decay-dead) and weather (unverdicted) are parked — NOT
    // spawned.
    let streak = streak::strategy::Streak::new();
    loop {
        let r = std::panic::AssertUnwindSafe(streak.run(&eng))
            .catch_unwind()
            .await;
        report("streak", r);
        tokio::time::sleep(streak::strategy::next_poll_delay(
            chrono::Utc::now().timestamp(),
        ))
        .await;
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
