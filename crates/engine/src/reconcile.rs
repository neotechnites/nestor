//! Settlement / reconcile loop (T004). Closes the loop on open positions:
//! for each one, fetch its Kalshi market, read the authoritative `result`,
//! decide win/loss, realize P&L through the risk layer, and append a
//! settlement record to the trade log. Run daily (morning-after for weather).
//!
//! Kalshi is the settlement source of truth (a single `GET markets/{ticker}`).
//! Not-yet-settled markets are skipped and retried on the next run.

use anyhow::Result;
use chrono_tz::America::New_York;
use serde_json::json;

use crate::logging;
use crate::risk::Side;
use crate::strategy::Engine;

const LOG: &str = "weather_trades.jsonl";

/// Decide the settlement action for a position given the market's raw `result`.
/// `None` = not settled yet (or void/unknown) → skip and retry next run.
/// `Some(won)` = settled; `won` is whether our `side` matches the outcome.
/// Kalshi `result` is "yes"/"no": a YES holder wins iff result == "yes".
fn settlement_won(side: Side, result: &str) -> Option<bool> {
    match result.trim().to_ascii_lowercase().as_str() {
        "yes" => Some(side == Side::Yes),
        "no" => Some(side == Side::No),
        // "" (still open) or "void"/anything unexpected: don't settle.
        _ => None,
    }
}

/// Settle every open position whose Kalshi market has a final `result`.
pub async fn run(eng: &Engine) -> Result<()> {
    // Roll the risk layer to today (ET) first. This resets the daily counters
    // for the new trading day, so prior-day positions we settle below are NOT
    // attributed to today's day_loss (see RiskManager::settle). A no-op if the
    // day already matches (e.g. reconcile and the strategy ran the same day).
    let today = chrono::Utc::now()
        .with_timezone(&New_York)
        .date_naive()
        .format("%Y-%m-%d")
        .to_string();
    eng.begin_day(&today);

    // Snapshot open tickers+sides so we never hold the risk lock across the
    // network fetch (mirrors Engine::execute's discipline).
    let open: Vec<(String, Side)> = {
        let r = eng.risk.lock().unwrap();
        r.open_positions()
            .iter()
            .map(|p| (p.ticker.clone(), p.side))
            .collect()
    };

    logging::info(format!(
        "reconcile start — day={today} {} open position(s)",
        open.len()
    ));
    let mut settled = 0usize;
    let mut pending = 0usize;

    for (ticker, side) in open {
        let market = match eng.kalshi.market(&ticker).await {
            Ok(m) => m,
            Err(e) => {
                logging::info(format!("{ticker}: market fetch failed ({e}) — skip"));
                continue;
            }
        };
        let result = market.result.unwrap_or_default();
        let won = match settlement_won(side, &result) {
            Some(w) => w,
            None => {
                pending += 1;
                logging::info(format!("{ticker}: not settled (result={result:?}) — skip"));
                continue;
            }
        };

        // Realize P&L (money math + kill-switch live in the risk layer).
        let outcome = eng.risk.lock().unwrap().settle(&ticker, won);
        match outcome {
            Some(o) => {
                settled += 1;
                logging::record(
                    LOG,
                    json!({
                        "event": "settlement",
                        "ticker": o.ticker,
                        "won": o.won,
                        "pnl": o.pnl,
                        "result": result,
                    }),
                );
                logging::info(format!(
                    "{}: settled won={} pnl=${:.2}",
                    o.ticker, o.won, o.pnl
                ));
            }
            None => {
                logging::info(format!(
                    "{ticker}: no open position (already settled?) — skip"
                ));
            }
        }
    }

    let st = eng.risk.lock().unwrap().status();
    logging::info(format!(
        "reconcile done — settled={settled} pending={pending} bankroll=${:.2} drawdown={:.1}% halted={}",
        st.bankroll,
        st.drawdown * 100.0,
        st.halted
    ));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settled_yes_market() {
        // Our YES wins on "yes", loses on "no".
        assert_eq!(settlement_won(Side::Yes, "yes"), Some(true));
        assert_eq!(settlement_won(Side::Yes, "no"), Some(false));
    }

    #[test]
    fn settled_no_side() {
        // Our NO wins on "no", loses on "yes".
        assert_eq!(settlement_won(Side::No, "no"), Some(true));
        assert_eq!(settlement_won(Side::No, "yes"), Some(false));
    }

    #[test]
    fn not_settled_is_skipped() {
        // Empty result (still open) → skip. Case/whitespace tolerant.
        assert_eq!(settlement_won(Side::Yes, ""), None);
        assert_eq!(settlement_won(Side::No, "  "), None);
        assert_eq!(settlement_won(Side::Yes, "YES"), Some(true));
        // Unexpected/void outcome → skip rather than book a phantom loss.
        assert_eq!(settlement_won(Side::Yes, "void"), None);
    }
}
