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

    let store = Box::new(JsonStore::new(env_str(
        "NESTOR_STATE_PATH",
        "data/state.json",
    )));
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
    };

    // `reconcile` is not a strategy: it closes open positions against Kalshi's
    // settled result and realizes P&L (T004). Everything else is a strategy.
    if which == "reconcile" {
        return engine::reconcile::run(&eng).await;
    }

    // The lock sleeve is always-on (unlike the daily weather cron): `lock` loops a
    // scan pass every 15s; `lock-once` runs a single pass (for testing). Same
    // Strategy contract as weather — the binary just chooses the cadence.
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

/// Age of the biases file in whole days, or None if it doesn't exist / unreadable.
fn biases_age_days(path: &str) -> Option<u64> {
    let modified = std::fs::metadata(path).ok()?.modified().ok()?;
    Some(modified.elapsed().ok()?.as_secs() / 86_400)
}
