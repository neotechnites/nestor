//! Live streak sleeve — one scan pass over KXBTC15M + KXETH15M (redirect
//! 2026-07-23). Detects a settled 4-streak, buys the reversal side in the new
//! market's first 60s if its ask ≤ 44¢, taker-only, one order per market, hold
//! to settlement. Orders route through `Engine::execute` (Flat sizing; cluster
//! = the window close shared across coins, so BTC+ETH together are ONE bet).
//!
//! Week-1 purpose is MECHANICS, not efficacy: every signal — entered, gated,
//! missed, or risk-rejected — is appended to `data/streak_week1.jsonl`.

use std::collections::HashSet;
use std::sync::Mutex;

use anyhow::Result;
use async_trait::async_trait;
use engine::kalshi::Market;
use engine::risk::taker_fee;
use engine::strategy::ExecOutcome;
use engine::{alert, logging, Engine, Side, Signal, SizingHint, Strategy};
use serde_json::json;

use crate::signal::{self, Candidate, SettledWindow, Skip};

const WEEK1_LOG: &str = "data/streak_week1.jsonl";
const SERIES: [&str; 2] = ["KXBTC15M", "KXETH15M"];

pub struct Streak {
    /// Dedup for week-1 records and order attempts: "{ticker}" for an entry
    /// attempt (one order per market, ever), "{ticker}|{kind}" for skip records
    /// (at most one record per skip kind per market — retryable skips may later
    /// convert to an entry, giving that market two records: the skip + the entry).
    seen: Mutex<HashSet<String>>,
}

impl Streak {
    pub fn new() -> Self {
        Streak {
            seen: Mutex::new(HashSet::new()),
        }
    }

    fn first_time(&self, key: String) -> bool {
        self.seen
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(key)
    }
}

impl Default for Streak {
    fn default() -> Self {
        Self::new()
    }
}

/// Newest-first settled windows with non-empty results.
fn settled_windows(markets: &[Market]) -> Vec<SettledWindow> {
    let mut v: Vec<SettledWindow> = markets
        .iter()
        .filter_map(|m| {
            let result = m.result.clone().unwrap_or_default();
            if result.is_empty() {
                return None;
            }
            m.close_unix()
                .map(|close_unix| SettledWindow { close_unix, result })
        })
        .collect();
    v.sort_by_key(|w| std::cmp::Reverse(w.close_unix));
    v
}

/// The current window's market: already open (open_time ≤ now, when present),
/// not yet closed, closing soonest.
fn current_market(markets: &[Market], now: i64) -> Option<&Market> {
    markets
        .iter()
        .filter(|m| m.open_unix().is_none_or(|o| o <= now))
        .filter_map(|m| m.close_unix().map(|c| (m, c)))
        .filter(|&(_, c)| c > now)
        .min_by_key(|&(_, c)| c)
        .map(|(m, _)| m)
}

#[async_trait]
impl Strategy for Streak {
    fn name(&self) -> &str {
        "streak"
    }

    async fn run(&self, eng: &Engine) -> Result<()> {
        for series in SERIES {
            if let Err(e) = self.scan_series(eng, series).await {
                logging::info(format!("streak {series}: scan error ({e}) — skip"));
            }
        }
        Ok(())
    }
}

impl Streak {
    async fn scan_series(&self, eng: &Engine, series: &str) -> Result<()> {
        let settled_raw = eng.kalshi.probe_series(series, "settled", 8).await?;
        let settled = settled_windows(&settled_raw);
        let opens = eng.kalshi.markets(series, "open").await?;

        let now = chrono::Utc::now().timestamp();
        let cur = match current_market(&opens, now) {
            Some(m) => m,
            None => return Ok(()),
        };
        let cand = Candidate {
            open_unix: cur.open_unix(),
            close_unix: cur.close_unix().unwrap_or(now + signal::WINDOW_SECS),
            yes_ask: cur.yes_ask_cents_f64(),
            no_ask: cur.no_ask_cents_f64(),
        };

        match signal::detect(&settled, &cand, now) {
            Ok(entry) => self.enter(eng, series, cur, &cand, entry, now).await,
            Err(skip) => {
                self.log_skip(series, &cur.ticker, &skip);
                Ok(())
            }
        }
    }

    /// Log a skip once per (ticker, kind). No-signal cases stay silent — only
    /// streak-relevant dispositions are week-1 data.
    fn log_skip(&self, series: &str, ticker: &str, skip: &Skip) {
        let kind = match skip {
            Skip::NoStreak | Skip::InsufficientHistory | Skip::NotConsecutive => return,
            Skip::PrevNotSettled => "prev_not_settled",
            Skip::WindowMismatch => "window_mismatch",
            Skip::NotEntryWindow { .. } => "missed_entry_window",
            Skip::Unpriced => "unpriced",
            Skip::PriceAboveGate { .. } => "price_above_gate",
        };
        if !self.first_time(format!("{ticker}|{kind}")) {
            return;
        }
        logging::record_path(
            WEEK1_LOG,
            json!({
                "event": "streak_skip",
                "series": series,
                "ticker": ticker,
                "reject_reason": skip.as_str(),
                "retryable": skip.retryable(),
            }),
        );
        logging::info(format!(
            "streak {series}: {ticker} skip — {}",
            skip.as_str()
        ));
    }

    async fn enter(
        &self,
        eng: &Engine,
        series: &str,
        cur: &Market,
        cand: &Candidate,
        entry: signal::Entry,
        now: i64,
    ) -> Result<()> {
        // One order attempt per market, ever (missed fills are DATA, never chased;
        // the deterministic client_order_id also dedupes across restarts).
        if !self.first_time(cur.ticker.clone()) {
            return Ok(());
        }

        let side = if entry.buy_yes { Side::Yes } else { Side::No };
        let limit = entry.ask.round() as i64;
        let sig = Signal {
            strategy: "streak".into(),
            ticker: cur.ticker.clone(),
            side,
            limit_cents: limit,
            // Window close shared across coins: simultaneous BTC+ETH = ONE bet.
            cluster: format!("streak-{}", cand.close_unix),
            sizing: SizingHint::Flat,
        };

        let outcome = eng.execute(sig).await;
        let mut rec = json!({
            "event": "streak_signal",
            "ts_signal": now,
            "series": series,
            "ticker": cur.ticker,
            "streak_dir": entry.streak_dir,
            "side_bought": side.as_str(),
            "ask_at_signal": entry.ask,
            "limit_placed": limit,
            "filled": false,
        });

        match &outcome {
            ExecOutcome::Paper(o) => {
                let fee_cents = taker_fee(o.limit_cents, o.count) * 100.0;
                rec["filled"] = json!(true); // paper simulates an immediate fill
                rec["paper"] = json!(true);
                rec["ts_fill"] = json!(now);
                rec["fill_price"] = json!(o.limit_cents);
                rec["count"] = json!(o.count);
                rec["fee_cents"] = json!(fee_cents);
                logging::info(format!(
                    "streak {series}: [paper] fade {} — buy {}x {} {} @ {}c (ask {:.1})",
                    entry.streak_dir,
                    o.count,
                    side.as_str(),
                    cur.ticker,
                    o.limit_cents,
                    entry.ask
                ));
            }
            ExecOutcome::Filled { order, response } => {
                let fee_cents = taker_fee(order.limit_cents, order.count) * 100.0;
                // Live: order ACCEPTED at the exchange. True fill confirmation is
                // T011; week-1 hand-watching covers the gap.
                rec["filled"] = json!(true);
                rec["ts_fill"] = json!(now);
                rec["fill_price"] = json!(order.limit_cents);
                rec["count"] = json!(order.count);
                rec["fee_cents"] = json!(fee_cents);
                rec["order"] = response.clone();
                logging::info(format!(
                    "streak {series}: BOUGHT {}x {} {} @ {}c",
                    order.count,
                    side.as_str(),
                    cur.ticker,
                    order.limit_cents
                ));
                alert::notify(
                    &eng.http,
                    &format!(
                        "streak BOUGHT {}x {} {} @ {}c (fade {})",
                        order.count,
                        side.as_str(),
                        cur.ticker,
                        order.limit_cents,
                        entry.streak_dir
                    ),
                )
                .await;
            }
            ExecOutcome::Rejected(r) => {
                rec["reject_reason"] = json!(format!("risk:{r:?}"));
                logging::info(format!("streak {series}: rejected ({r:?}) {}", cur.ticker));
            }
            ExecOutcome::OrderError(e) => {
                rec["reject_reason"] = json!(format!("order_error:{e}"));
                logging::info(format!(
                    "streak {series}: ORDER FAILED {} ({e})",
                    cur.ticker
                ));
                alert::notify(
                    &eng.http,
                    &format!("streak ORDER FAILED {} ({e})", cur.ticker),
                )
                .await;
            }
        }
        logging::record_path(WEEK1_LOG, rec);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mkt(ticker: &str, open: Option<&str>, close: &str, result: Option<&str>) -> Market {
        // Build via JSON to keep pace with Market's serde surface.
        serde_json::from_value(json!({
            "ticker": ticker,
            "open_time": open,
            "close_time": close,
            "result": result,
        }))
        .unwrap()
    }

    #[test]
    fn settled_windows_sorts_desc_and_drops_unsettled() {
        let ms = vec![
            mkt("A", None, "2026-07-23T10:00:00Z", Some("yes")),
            mkt("B", None, "2026-07-23T10:30:00Z", Some("no")),
            mkt("C", None, "2026-07-23T10:15:00Z", Some("")), // unsettled → dropped
        ];
        let w = settled_windows(&ms);
        assert_eq!(w.len(), 2);
        assert!(w[0].close_unix > w[1].close_unix);
        assert_eq!(w[0].result, "no");
    }

    #[test]
    fn current_market_picks_open_closing_soonest() {
        let ms = vec![
            mkt(
                "LATER",
                Some("2026-07-23T10:00:00Z"),
                "2026-07-23T10:30:00Z",
                None,
            ),
            mkt(
                "CURRENT",
                Some("2026-07-23T09:45:00Z"),
                "2026-07-23T10:15:00Z",
                None,
            ),
            mkt(
                "NOT_OPEN_YET",
                Some("2026-07-23T10:15:00Z"),
                "2026-07-23T10:45:00Z",
                None,
            ),
        ];
        // now = 10:05Z
        let now = chrono::DateTime::parse_from_rfc3339("2026-07-23T10:05:00Z")
            .unwrap()
            .timestamp();
        assert_eq!(current_market(&ms, now).unwrap().ticker, "CURRENT");
    }

    #[test]
    fn seen_dedup_is_once() {
        let s = Streak::new();
        assert!(s.first_time("X".into()));
        assert!(!s.first_time("X".into()));
    }
}
