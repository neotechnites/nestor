//! Live streak sleeve — scan pass over KXBTC15M + KXETH15M (redirect
//! 2026-07-23). Detects a settled 4-streak, buys the reversal side in the new
//! market's first 60s if its ask ≤ 44¢, taker-only, one order per market, hold
//! to settlement. Orders route through `Engine::execute`, which verifies REAL
//! fills (accepted ≠ filled) and cancels any unfilled remainder.
//!
//! Cadence: the binary polls at 1s inside each 60s entry window and lazily
//! (~12s) outside it — see [`next_poll_delay`]. In-window passes fetch only the
//! open markets (settled results are cached per window; refetched only while
//! the previous window is still settling), keeping the fast-poll rate at ~2
//! requests/second across both series.
//!
//! DATA CAPTURE: every poll appends an observation line (`data/obs/`); every
//! signal decision stores the order book alongside the participation record
//! (`data/streak_week1.jsonl`). Nestor keeps everything it generates.

use std::collections::{HashMap, HashSet};
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
/// Fast-poll horizon after each 15-min boundary: covers the 60s entry window
/// plus settlement-lag slack (a late-settling previous window can still convert
/// a PrevNotSettled skip into an entry inside the window).
const FAST_WINDOW_SECS: i64 = 75;

/// Adaptive polling cadence (redirect: 1-2s during entry windows, lazy outside;
/// a 15s cadence is too coarse for a 60s window). Pure — unit-tested. Never
/// oversleeps past the next 15-min boundary.
pub fn next_poll_delay(now_unix: i64) -> std::time::Duration {
    let into_window = now_unix.rem_euclid(900);
    let secs = if into_window < FAST_WINDOW_SECS {
        1
    } else {
        (900 - into_window).clamp(1, 12)
    };
    std::time::Duration::from_secs(secs as u64)
}

pub struct Streak {
    /// Dedup for participation records and order attempts: "{ticker}" for an
    /// entry attempt (one order per market, ever), "{ticker}|{kind}" for skip
    /// records (one per skip kind per market — a retryable skip may later
    /// convert, giving that market two records: the skip + the entry).
    seen: Mutex<HashSet<String>>,
    /// Settled-results cache per series: (window_id, windows). Valid for the
    /// whole 15-min window unless the previous window is still settling (the
    /// PrevNotSettled case marks a refetch for the next pass).
    settled_cache: Mutex<HashMap<String, (i64, Vec<SettledWindow>)>>,
}

impl Streak {
    pub fn new() -> Self {
        Streak {
            seen: Mutex::new(HashSet::new()),
            settled_cache: Mutex::new(HashMap::new()),
        }
    }

    fn first_time(&self, key: String) -> bool {
        self.seen
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(key)
    }

    fn seen_contains(&self, key: &str) -> bool {
        self.seen
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .contains(key)
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

/// Observation log path for a given UTC timestamp (daily rotation by filename).
fn obs_path(now: chrono::DateTime<chrono::Utc>) -> String {
    format!("data/obs/{}.jsonl", now.format("%Y-%m-%d"))
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
    /// Settled windows for `series`, cached per 15-min window; `force` refetches.
    async fn settled_for(
        &self,
        eng: &Engine,
        series: &str,
        window_id: i64,
        force: bool,
    ) -> Result<Vec<SettledWindow>> {
        if !force {
            let cache = self.settled_cache.lock().unwrap_or_else(|e| e.into_inner());
            if let Some((wid, windows)) = cache.get(series) {
                if *wid == window_id {
                    return Ok(windows.clone());
                }
            }
        }
        let raw = eng.kalshi.probe_series(series, "settled", 8).await?;
        let windows = settled_windows(&raw);
        self.settled_cache
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(series.to_string(), (window_id, windows.clone()));
        Ok(windows)
    }

    fn refetch_key(series: &str, window_id: i64) -> String {
        format!("refetch|{series}|{window_id}")
    }

    async fn scan_series(&self, eng: &Engine, series: &str) -> Result<()> {
        let now_dt = chrono::Utc::now();
        let now = now_dt.timestamp();
        let window_id = now.div_euclid(900);

        // Refetch settled results while the previous window is still settling
        // (flagged by a prior pass's PrevNotSettled), else serve from cache.
        let force = self.seen_contains(&Self::refetch_key(series, window_id));
        let settled = self.settled_for(eng, series, window_id, force).await?;
        let opens = eng.kalshi.markets(series, "open").await?;

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

        // DATA CAPTURE 1 — observation log: one compact line per poll, always.
        logging::record_path(
            &obs_path(now_dt),
            json!({
                "ts_ms": now_dt.timestamp_millis(),
                "ticker": cur.ticker,
                "yes_ask": cand.yes_ask,
                "no_ask": cand.no_ask,
            }),
        );

        match signal::detect(&settled, &cand, now) {
            Ok(entry) => self.enter(eng, series, cur, &cand, entry, now).await,
            Err(skip) => {
                if skip == Skip::PrevNotSettled {
                    // Ask subsequent passes in this window to refetch settled.
                    self.first_time(Self::refetch_key(series, window_id));
                }
                self.log_skip(eng, series, &cur.ticker, &skip).await;
                Ok(())
            }
        }
    }

    /// Log a skip once per (ticker, kind), with the order-book decision snapshot.
    /// No-signal cases stay silent — only streak-relevant dispositions are data.
    async fn log_skip(&self, eng: &Engine, series: &str, ticker: &str, skip: &Skip) {
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
        // DATA CAPTURE 2 — decision snapshot at the skip moment.
        let book = eng.kalshi.orderbook(ticker).await.unwrap_or(json!(null));
        logging::record_path(
            WEEK1_LOG,
            json!({
                "event": "streak_skip",
                "series": series,
                "ticker": ticker,
                "reject_reason": skip.as_str(),
                "retryable": skip.retryable(),
                "book": book,
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
        // One order attempt per market, ever (missed fills are DATA, never
        // chased; the deterministic client_order_id also dedupes across restarts).
        if !self.first_time(cur.ticker.clone()) {
            return Ok(());
        }

        // DATA CAPTURE 2 — decision snapshot at the entry moment (fetched before
        // the order so the book reflects what we saw when deciding).
        let book = eng
            .kalshi
            .orderbook(&cur.ticker)
            .await
            .unwrap_or(json!(null));

        let side = if entry.buy_yes { Side::Yes } else { Side::No };
        let limit = entry.ask.round() as i64;
        // Fill-verification budget: time left in the entry window (execute caps
        // it at 8s — "window close or a few seconds, whichever comes first").
        let entry_window_end = cand.close_unix - signal::WINDOW_SECS + 60;
        let fill_wait = (entry_window_end - now).clamp(2, 60) as u64;
        let sig = Signal {
            strategy: "streak".into(),
            ticker: cur.ticker.clone(),
            side,
            limit_cents: limit,
            // Window close shared across coins: simultaneous BTC+ETH = ONE bet.
            cluster: format!("streak-{}", cand.close_unix),
            sizing: SizingHint::Flat,
            fill_wait_secs: fill_wait,
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
            "book": book,
        });

        match &outcome {
            ExecOutcome::Filled { fill, response, .. } => {
                let fee_cents = taker_fee(fill.fill_price_cents, fill.filled) * 100.0;
                rec["filled"] = json!(true);
                rec["partial"] = json!(fill.partial);
                rec["simulated"] = json!(fill.simulated);
                rec["ts_submit"] = json!(fill.ts_submit_ms);
                rec["ts_ack"] = json!(fill.ts_ack_ms);
                rec["ts_fill"] = json!(fill.ts_fill_ms);
                rec["fill_price"] = json!(fill.fill_price_cents);
                rec["filled_count"] = json!(fill.filled);
                rec["canceled_count"] = json!(fill.canceled);
                rec["fee_cents"] = json!(fee_cents);
                rec["order_id"] = json!(fill.order_id);
                if !fill.simulated {
                    rec["order"] = response.clone();
                }
                logging::info(format!(
                    "streak {series}: {}FILLED {}x {} {} @ {}c (fade {}, ask {:.1}){}",
                    if fill.simulated { "[paper] " } else { "" },
                    fill.filled,
                    side.as_str(),
                    cur.ticker,
                    fill.fill_price_cents,
                    entry.streak_dir,
                    entry.ask,
                    if fill.partial { " (partial)" } else { "" }
                ));
                if !fill.simulated {
                    alert::notify(
                        &eng.http,
                        &format!(
                            "streak FILLED {}x {} {} @ {}c (fade {}){}",
                            fill.filled,
                            side.as_str(),
                            cur.ticker,
                            fill.fill_price_cents,
                            entry.streak_dir,
                            if fill.partial { " partial" } else { "" }
                        ),
                    )
                    .await;
                }
            }
            ExecOutcome::Missed { order, fill } => {
                rec["partial"] = json!(false);
                rec["simulated"] = json!(false);
                rec["ts_submit"] = json!(fill.ts_submit_ms);
                rec["ts_ack"] = json!(fill.ts_ack_ms);
                rec["filled_count"] = json!(0);
                rec["canceled_count"] = json!(fill.canceled);
                rec["order_id"] = json!(fill.order_id);
                rec["reject_reason"] = json!("missed_fill");
                logging::info(format!(
                    "streak {series}: MISSED (no fill, canceled {}) {}",
                    order.count, cur.ticker
                ));
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

    #[test]
    fn poll_delay_fast_in_entry_window_lazy_outside() {
        // A window boundary at a multiple of 900: first 75s → 1s cadence.
        let boundary = 900_000i64; // divisible by 900
        assert_eq!(next_poll_delay(boundary).as_secs(), 1);
        assert_eq!(next_poll_delay(boundary + 74).as_secs(), 1);
        // Outside the fast window → lazy 12s.
        assert_eq!(next_poll_delay(boundary + 75).as_secs(), 12);
        assert_eq!(next_poll_delay(boundary + 400).as_secs(), 12);
        // Never oversleeps past the next boundary: 5s before it → ≤5s sleep.
        assert_eq!(next_poll_delay(boundary + 895).as_secs(), 5);
        assert_eq!(next_poll_delay(boundary + 899).as_secs(), 1);
    }

    #[test]
    fn entry_window_end_math() {
        // close = open + 900; entry window ends open + 60 = close - 840.
        let close = 1_000_900i64;
        let end = close - signal::WINDOW_SECS + 60;
        assert_eq!(end, close - 840);
    }
}
