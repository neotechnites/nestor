//! Nestor entrypoint. Loads config (nestor.toml + env), builds the shared Engine
//! (Kalshi client + Risk layer + cities), and runs the selected strategy.
//! Called by cron/systemd on the VPS.
//!
//! Usage: `nestor [strategy]`  (default: weather)

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

    let which = std::env::args().nth(1).unwrap_or_else(|| "weather".into());

    // `backtest-lock` re-confirms the lock edge in-code against cached data.
    // Read-only, no keys, no engine.
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

    // `run` = the production runtime: ONE process hosting every strategy as tokio
    // tasks that share this ONE in-memory RiskManager. This is what makes the shared
    // bankroll safe — no second process writes state.json concurrently (which would
    // clobber updates and bypass the kill-switch), and settlement runs intraday so
    // lock's losses actually feed the daily-loss halt.
    if which == "run" {
        return run_all(eng).await;
    }

    // The lock sleeve is always-on (unlike the daily weather cron): `lock` loops a
    // scan pass every 15s; `lock-once` runs a single pass (for testing). Same
    // Strategy contract as weather — the binary just chooses the cadence. In
    // production use `run` (above); these are for manual/isolated operation.
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
    engine::logging::info(
        "nestor run — lock (15s) + weather (9am ET) + settlement (60s), one process",
    );

    // Settlement: sweep every 60s so lock's 15-min markets settle intraday (same
    // trading day -> their losses feed the daily-loss kill-switch) and weather
    // settles the morning after. Each iteration is panic-caught so one bad cycle
    // can't silently kill the loop (which would disable the kill-switch).
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

    // Weather: fire once daily at ~9am ET.
    {
        let e = eng.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(until_next_9am_et()).await;
                let r = std::panic::AssertUnwindSafe(weather::Weather.run(&e))
                    .catch_unwind()
                    .await;
                report("weather", r);
                // avoid re-firing within the same minute the timer landed on
                tokio::time::sleep(Duration::from_secs(90)).await;
            }
        });
    }

    // Lock: continuous scanner in the foreground (keeps the process alive).
    let lock = lock::strategy::Lock;
    loop {
        let r = std::panic::AssertUnwindSafe(lock.run(&eng))
            .catch_unwind()
            .await;
        report("lock", r);
        tokio::time::sleep(Duration::from_secs(15)).await;
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

/// Duration until the next 09:00 America/New_York (DST-correct: 09:00 wall-clock
/// on the target date, not now+24h which drifts across a DST transition).
fn until_next_9am_et() -> std::time::Duration {
    use chrono::{Datelike, TimeZone};
    use chrono_tz::America::New_York;
    let now = chrono::Utc::now().with_timezone(&New_York);
    let at_9 = |d: chrono::NaiveDate| {
        New_York
            .with_ymd_and_hms(d.year(), d.month(), d.day(), 9, 0, 0)
            .single()
    };
    let target = match at_9(now.date_naive()) {
        Some(t) if now < t => t,
        _ => at_9(now.date_naive() + chrono::Duration::days(1))
            .unwrap_or(now + chrono::Duration::days(1)),
    };
    let secs = (target - now).num_seconds().max(0) as u64;
    std::time::Duration::from_secs(secs)
}

/// Age of the biases file in whole days, or None if it doesn't exist / unreadable.
fn biases_age_days(path: &str) -> Option<u64> {
    let modified = std::fs::metadata(path).ok()?.modified().ok()?;
    Some(modified.elapsed().ok()?.as_secs() / 86_400)
}
