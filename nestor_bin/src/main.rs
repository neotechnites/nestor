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

    let settings = Settings::load(&env_str("NESTOR_CONFIG", "nestor.toml"))?;

    let which = std::env::args().nth(1).unwrap_or_else(|| "weather".into());

    // `calibrate` is a maintenance job (not a strategy): it needs neither the
    // Kalshi client nor the risk layer, so handle it before building the Engine.
    if which == "calibrate" {
        let out = env_str("NESTOR_BIASES_PATH", "data/biases.json");
        return engine::calibrate::run(&settings, 60, &out).await;
    }

    // Secrets + mode come from env (env wins over the file's default).
    let mode = Mode::from_env(&std::env::var("NESTOR_ENV").unwrap_or(settings.trading.env.clone()));
    let bankroll = env_f64("NESTOR_BANKROLL", settings.trading.bankroll);

    let store = Box::new(JsonStore::new(env_str(
        "NESTOR_STATE_PATH",
        "data/state.json",
    )));
    let risk = RiskManager::load_or_init(settings.risk, store, bankroll)?;

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
        http: reqwest::Client::new(),
        mode,
        risk: Mutex::new(risk),
        cities: settings.cities,
    };

    // `reconcile` is not a strategy: it closes open positions against Kalshi's
    // settled result and realizes P&L (T004). Everything else is a strategy.
    if which == "reconcile" {
        return engine::reconcile::run(&eng).await;
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
