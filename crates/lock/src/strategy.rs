//! Live lock sleeve — one scan pass over open KXBTC15M markets. For each market in
//! its final 2–4 minutes, compute Z from live Coinbase spot, and if a favorite
//! qualifies (93–97¢, Z≥4, distance on its side), route a Signal through the Risk
//! layer. Same `Strategy` contract as weather; the binary loops this for the
//! always-on sleeve. Orders go through `Engine::execute` (paper logs, live places).

use std::collections::HashSet;

use anyhow::Result;
use async_trait::async_trait;
use engine::strategy::ExecOutcome;
use engine::{alert, logging, Engine, Side, Signal, SizingHint, Strategy};
use serde_json::json;

use crate::coinbase;
use crate::signal::{self, LockParams};

const LOG: &str = "lock_trades.jsonl";
const SERIES: &str = "KXBTC15M";

pub struct Lock;

#[async_trait]
impl Strategy for Lock {
    fn name(&self) -> &str {
        "lock"
    }

    async fn run(&self, eng: &Engine) -> Result<()> {
        let candles = coinbase::recent_1min(&eng.http).await?;
        let (spot, med) = match (coinbase::spot(&candles), coinbase::median_move(&candles)) {
            (Some(s), Some(m)) if m > 0.0 => (s, m),
            _ => {
                logging::info("lock: no coinbase spot/median this pass — skip");
                return Ok(());
            }
        };

        let markets = eng.kalshi.markets(SERIES, "open").await?;
        let params = LockParams::default();

        // Don't re-enter a market we already hold.
        let held: HashSet<String> = {
            let r = eng.risk.lock().unwrap_or_else(|e| e.into_inner());
            r.open_positions()
                .iter()
                .map(|p| p.ticker.clone())
                .collect()
        };

        // Capture `now` AFTER the network fetches so `sb` isn't stale at the 30s floor.
        let now = chrono::Utc::now().timestamp();
        let (mut in_window, mut qualified) = (0usize, 0usize);
        for m in &markets {
            let close = match m.close_unix() {
                Some(c) => c,
                None => continue,
            };
            let sb = close - now;
            if !(30..=240).contains(&sb) {
                continue; // only the final 2-4 min
            }
            let strike = match m.floor_strike {
                Some(k) => k,
                None => continue,
            };
            // Deci-cent asks for both sides. The favorite is the higher-priced side;
            // gate the band on the ACTUAL ask we would pay (not 100-yes_ask=no_bid).
            let (yes_ask, no_ask) = match (m.yes_ask_cents_f64(), m.no_ask_cents_f64()) {
                (Some(y), Some(n)) => (y, n),
                _ => continue,
            };
            in_window += 1;
            let fav_is_yes = yes_ask > 50.0;
            let fav_ask = if fav_is_yes { yes_ask } else { no_ask };

            let entry = match signal::evaluate_favorite(
                fav_ask,
                fav_is_yes,
                spot,
                strike,
                med,
                sb as f64 / 60.0,
                &params,
            ) {
                Some(e) => e,
                None => continue,
            };
            qualified += 1;

            if held.contains(&m.ticker) {
                continue;
            }
            let ask = fav_ask.round() as i64; // order price to Kalshi in whole cents
            let side = if fav_is_yes { Side::Yes } else { Side::No };
            let sig = Signal {
                strategy: "lock".into(),
                ticker: m.ticker.clone(),
                side,
                limit_cents: ask,
                cluster: format!("btc:{close}"), // all positions in one 15-min window = one bet
                sizing: SizingHint::Fraction,
            };

            let outcome = eng.execute(sig).await;
            logging::record(
                LOG,
                json!({"event":"lock_signal","ticker":m.ticker,"sb":sb,"z":entry.z,
                       "fav_price":entry.fav_price,"fav_is_yes":entry.fav_is_yes,"ask_cents":ask,
                       "outcome":format!("{outcome:?}")}),
            );
            match &outcome {
                ExecOutcome::Paper(o) => logging::info(format!(
                    "lock: [paper] buy {}x {} @ {ask}c (Z={:.1}, {sb}s left)",
                    o.count, m.ticker, entry.z
                )),
                ExecOutcome::Filled { order, .. } => {
                    logging::info(format!(
                        "lock: BOUGHT {}x {} @ {ask}c (Z={:.1})",
                        order.count, m.ticker, entry.z
                    ));
                    alert::notify(
                        &eng.http,
                        &format!(
                            "lock BOUGHT {}x {} @ {ask}c (Z={:.1})",
                            order.count, m.ticker, entry.z
                        ),
                    )
                    .await;
                }
                ExecOutcome::Rejected(r) => {
                    logging::info(format!("lock: rejected ({r:?}) {}", m.ticker))
                }
                ExecOutcome::OrderError(e) => {
                    logging::info(format!("lock: ORDER FAILED {} ({e})", m.ticker));
                    alert::notify(&eng.http, &format!("lock ORDER FAILED {} ({e})", m.ticker))
                        .await;
                }
            }
        }
        logging::info(format!(
            "lock scan — {} open markets, {in_window} in-window, {qualified} qualified",
            markets.len()
        ));
        Ok(())
    }
}
