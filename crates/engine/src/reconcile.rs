//! Settlement / reconcile loop (T004). Closes the loop on open positions:
//! for each one, fetch its Kalshi market, read the authoritative `result`,
//! decide win/loss, realize P&L through the risk layer, and append a
//! settlement record to the trade log. Run daily (morning-after for weather).
//!
//! Kalshi is the settlement source of truth (a single `GET markets/{ticker}`).
//! Not-yet-settled markets are skipped and retried on the next run.

use anyhow::{Context, Result};
use chrono_tz::America::New_York;
use serde_json::json;

use crate::strategy::{Engine, Mode};
use crate::{alert, kalshi, logging};
use crate::risk::Side;

/// Settlements from ALL strategies land here (strategy-tagged), so per-strategy
/// reports (e.g. streak week-1) can join their entry logs by ticker.
const LOG: &str = "settlements.jsonl";

/// Divergence breaker threshold (fix 1c): if the REAL Kalshi cash balance and the
/// risk layer's expected cash (`bankroll − open stakes`) differ by more than this
/// many dollars, something is wrong with our accounting — HALT. $2 absorbs
/// sub-cent fee rounding and in-flight settlement timing across a handful of open
/// positions without masking a genuine miscount.
const DIVERGENCE_THRESHOLD_USD: f64 = 2.0;

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

    // Snapshot open tickers+sides+strategy so we never hold the risk lock across
    // the network fetch (mirrors Engine::execute's discipline).
    let open: Vec<(String, Side, String)> = {
        let r = eng.risk.lock().unwrap_or_else(|e| e.into_inner());
        r.open_positions()
            .iter()
            .map(|p| (p.ticker.clone(), p.side, p.strategy.clone()))
            .collect()
    };

    logging::info(format!(
        "reconcile start — day={today} {} open position(s)",
        open.len()
    ));
    let mut settled = 0usize;
    let mut pending = 0usize;

    for (ticker, side, strategy) in open {
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
        let outcome = eng
            .risk
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .settle(&ticker, won);
        match outcome {
            Some(o) => {
                settled += 1;
                logging::record(
                    LOG,
                    json!({
                        "event": "settlement",
                        "strategy": strategy,
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

    // EXCHANGE-TRUTH reconciliation (live only): adopt orphan positions and run
    // the bankroll-vs-real-balance divergence breaker. Paper has no authenticated
    // exchange view. Runs AFTER settlement so a signed-call failure here can't
    // block realizing settled P&L. May return Err (signed-call failure) so the
    // caller's loop can classify/backoff/count 401s.
    if eng.mode == Mode::Live {
        reconcile_exchange_truth(eng).await?;
    }

    let st = eng.risk.lock().unwrap_or_else(|e| e.into_inner()).status();
    logging::info(format!(
        "reconcile done — settled={settled} pending={pending} bankroll=${:.2} drawdown={:.1}% halted={}",
        st.bankroll,
        st.drawdown * 100.0,
        st.halted
    ));
    Ok(())
}

/// Live exchange-truth pass: (1b) adopt any exchange position absent from local
/// state as an ORPHAN into risk state; (1c) compare real cash against expected
/// cash and HALT on divergence. Both use SIGNED calls — an error propagates so
/// the reconcile loop can back off / count consecutive 401s.
async fn reconcile_exchange_truth(eng: &Engine) -> Result<()> {
    // --- (1b) ORPHAN ADOPTION ------------------------------------------------
    let raw = eng
        .kalshi
        .positions()
        .await
        .context("positions read (orphan check)")?;
    let exchange = kalshi::parse_positions(&raw);
    for p in &exchange {
        // adopt_orphan is idempotent: it no-ops (returns false) for a ticker we
        // already track, so this only fires for genuine orphans.
        let cluster = format!("orphan-{}", p.ticker);
        let adopted = {
            let mut r = eng.risk.lock().unwrap_or_else(|e| e.into_inner());
            r.adopt_orphan(&p.ticker, p.side, p.count, p.entry_cents, &cluster)
        };
        if adopted {
            let msg = format!(
                "ORPHAN ADOPTED {} {} {}x @ {} — exchange position missing from local state; \
                 adopted into risk (entry basis {})",
                p.ticker,
                p.side.as_str(),
                p.count,
                p.entry_cents
                    .map(|c| format!("{c}c"))
                    .unwrap_or_else(|| "worst-case 99c".into()),
                p.entry_cents
                    .map(|c| format!("{c}c"))
                    .unwrap_or_else(|| "unknown".into()),
            );
            logging::info(format!("ALERT: {msg}"));
            eprintln!("[reconcile] ALERT: {msg}");
            alert::notify(&eng.http, &msg).await;
        }
    }

    // --- (1c) DIVERGENCE BREAKER --------------------------------------------
    let bal_cents = eng
        .kalshi
        .balance_cents()
        .await
        .context("balance read (divergence check)")?;
    let real_cash = bal_cents as f64 / 100.0;
    let expected_cash = {
        let r = eng.risk.lock().unwrap_or_else(|e| e.into_inner());
        r.expected_cash()
    };
    let divergence = (real_cash - expected_cash).abs();
    if divergence > DIVERGENCE_THRESHOLD_USD {
        let msg = format!(
            "BANKROLL DIVERGENCE ${divergence:.2} > ${DIVERGENCE_THRESHOLD_USD:.2} — real cash \
             ${real_cash:.2} vs expected ${expected_cash:.2}. HALTING (state/exchange disagree).",
        );
        logging::info(format!("ALERT: {msg}"));
        eprintln!("[reconcile] ALERT: {msg}");
        eng.risk.lock().unwrap_or_else(|e| e.into_inner()).halt();
        alert::notify(&eng.http, &msg).await;
    } else {
        logging::info(format!(
            "divergence check OK — real ${real_cash:.2} vs expected ${expected_cash:.2} (Δ${divergence:.2})"
        ));
    }
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
