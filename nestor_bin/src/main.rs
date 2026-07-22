//! Nestor entrypoint. Builds the shared Engine from env and runs the selected
//! strategy. Called by cron/systemd on the VPS.
//!
//! Usage: `nestor [strategy]`  (default: weather)

use anyhow::{Context, Result};
use engine::{Engine, Mode, Strategy};

#[tokio::main]
async fn main() -> Result<()> {
    dotenvy::dotenv().ok();

    let mode = Mode::from_env(&std::env::var("NESTOR_ENV").unwrap_or_else(|_| "paper".into()));
    let stake_usd = env_f64("NESTOR_STAKE_USD", 10.0);
    let max_daily_usd = env_f64("NESTOR_MAX_DAILY_USD", 80.0);

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
        stake_usd,
        max_daily_usd,
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
