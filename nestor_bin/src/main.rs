//! Nestor entrypoint. Builds the shared Engine (Kalshi client + Risk layer) from
//! env and runs the selected strategy. Called by cron/systemd on the VPS.
//!
//! Usage: `nestor [strategy]`  (default: weather)

use std::sync::Mutex;

use anyhow::{Context, Result};
use engine::risk::RiskConfig;
use engine::state::JsonStore;
use engine::{Engine, Mode, RiskManager, Strategy};

#[tokio::main]
async fn main() -> Result<()> {
    dotenvy::dotenv().ok();

    let mode = Mode::from_env(&std::env::var("NESTOR_ENV").unwrap_or_else(|_| "paper".into()));

    let cfg = RiskConfig {
        fraction: env_f64("NESTOR_FRACTION", 0.05),
        cluster_cap_frac: env_f64("NESTOR_CLUSTER_CAP", 0.15),
        flat_usd: env_f64("NESTOR_STAKE_USD", 10.0),
        daily_budget_usd: env_f64("NESTOR_MAX_DAILY_USD", 80.0),
        max_drawdown_frac: env_f64("NESTOR_MAX_DRAWDOWN", 0.30),
        daily_loss_limit_frac: env_f64("NESTOR_DAILY_LOSS_LIMIT", 0.15),
    };
    let bankroll = env_f64("NESTOR_BANKROLL", 1000.0);
    let store = Box::new(JsonStore::new(
        std::env::var("NESTOR_STATE_PATH").unwrap_or_else(|_| "data/state.json".into()),
    ));
    let risk = RiskManager::load_or_init(cfg, store, bankroll)?;

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
    };

    let which = std::env::args().nth(1).unwrap_or_else(|| "weather".into());
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
