//! The Strategy trait + shared Engine context + the order-execution router.
//! New edges implement `Strategy` and route every order through `Engine::execute`,
//! which applies the Risk layer. Strategies never place raw orders themselves.
//!
//! EXECUTION TRUTH (redirect 2026-07-23): accepted ≠ filled. Live execution
//! verifies fills via `/portfolio/fills`, records ACTUAL price/count/timestamps,
//! feeds risk only the filled count, and cancels any unfilled remainder — a
//! resting order is never left alive (taker-only doctrine).

use std::sync::Mutex;

use crate::kalshi::{self, Kalshi};
use crate::risk::{Order, Rejection, RiskManager, Signal};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// Log decisions, place no orders (but still simulate fills for accounting).
    Paper,
    /// Place real orders.
    Live,
}

impl Mode {
    pub fn from_env(s: &str) -> Self {
        if s.eq_ignore_ascii_case("live") {
            Mode::Live
        } else {
            Mode::Paper
        }
    }
}

/// What ACTUALLY happened to an order — real numbers from the fills API, or a
/// simulated equivalent in paper mode. Every field feeds the participation record.
#[derive(Debug, Clone)]
pub struct FillReport {
    /// Contracts requested (the sized order).
    pub requested: i64,
    /// Contracts actually filled (0..=requested).
    pub filled: i64,
    /// Weighted-average actual fill price in cents (the limit in paper mode).
    /// Meaningless when `filled == 0`.
    pub fill_price_cents: i64,
    /// Unfilled remainder that was canceled at deadline.
    pub canceled: i64,
    pub partial: bool,
    /// True for paper-mode simulated fills.
    pub simulated: bool,
    /// Unix-ms timestamps for latency measurement (week-1 deliverable).
    pub ts_submit_ms: i64,
    pub ts_ack_ms: Option<i64>,
    pub ts_fill_ms: Option<i64>,
    pub order_id: Option<String>,
}

/// Result of routing a signal through risk + execution.
#[derive(Debug)]
pub enum ExecOutcome {
    /// Something filled (fully or partially — check `fill.partial`); the filled
    /// count is recorded in risk state. Paper fills carry `fill.simulated`.
    Filled {
        order: Order,
        fill: FillReport,
        response: serde_json::Value,
    },
    /// Order was placed but NOTHING filled before the deadline; the remainder
    /// was canceled. No position recorded. A missed fill is DATA — log it.
    Missed { order: Order, fill: FillReport },
    /// Risk layer refused.
    Rejected(Rejection),
    /// Live order placement errored (nothing known to be resting; if the order
    /// was accepted but the response was lost, the deterministic client_order_id
    /// makes a retry safe).
    OrderError(String),
}

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

/// Shared context handed to every strategy run.
pub struct Engine {
    pub kalshi: Kalshi,
    pub http: reqwest::Client,
    pub mode: Mode,
    pub risk: Mutex<RiskManager>,
    pub cities: Vec<crate::config::City>,
    /// Serializes the whole evaluate→place→verify-fill sequence across concurrent
    /// strategy tasks. Without it, two tasks could both clear a cap in `evaluate`
    /// before either records its fill (the risk lock is dropped across the network
    /// await). An async mutex is held across that await; the std risk lock is not.
    pub exec_lock: tokio::sync::Mutex<()>,
}

impl Engine {
    /// Roll the risk layer's daily counters for `day` (ET, YYYY-MM-DD).
    pub fn begin_day(&self, day: &str) {
        self.risk
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .begin_day(day);
    }

    /// Route a signal through the Risk layer, then execute (live, with fill
    /// verification) or simulate (paper). Never holds the std risk lock across
    /// a network await.
    pub async fn execute(&self, signal: Signal) -> ExecOutcome {
        // Serialize evaluate→place→verify→on_fill across concurrent tasks so
        // two strategies can't both pass a cap before either records its fill.
        let _exec = self.exec_lock.lock().await;
        let order = match self
            .risk
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .evaluate(&signal)
        {
            Ok(o) => o,
            Err(r) => return ExecOutcome::Rejected(r),
        };

        if self.mode == Mode::Live {
            self.execute_live(order, &signal).await
        } else {
            // Paper: simulate an immediate full fill at the limit. Same
            // accounting path (fee charged at fill) so paper P&L is honest.
            let ts = now_ms();
            self.risk
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .on_fill(&order);
            let fill = FillReport {
                requested: order.count,
                filled: order.count,
                fill_price_cents: order.limit_cents,
                canceled: 0,
                partial: false,
                simulated: true,
                ts_submit_ms: ts,
                ts_ack_ms: Some(ts),
                ts_fill_ms: Some(ts),
                order_id: None,
            };
            ExecOutcome::Filled {
                order,
                fill,
                response: serde_json::Value::Null,
            }
        }
    }

    /// Live path: place → verify fills → record ONLY what filled → cancel any
    /// remainder. "Window close or a few seconds, whichever comes first" caps
    /// the wait at min(signal.fill_wait_secs, 8).
    async fn execute_live(&self, order: Order, signal: &Signal) -> ExecOutcome {
        // Deterministic client_order_id (strategy + market ticker): if we die
        // after Kalshi accepts but before recording, a re-run resends the SAME
        // id and Kalshi dedupes it. One order per market is the design.
        let coid = format!("{}-{}", order.strategy, order.ticker);

        let ts_submit_ms = now_ms();
        let response = match self
            .kalshi
            .place_limit_buy(
                &order.ticker,
                order.side.as_str(),
                order.count,
                order.limit_cents,
                &coid,
            )
            .await
        {
            Ok(r) => r,
            Err(e) => return ExecOutcome::OrderError(e.to_string()),
        };
        let ts_ack_ms = now_ms();
        let order_id = kalshi::parse_order_id(&response);
        if order_id.is_none() {
            // Schema surprise: keep going via the fills fallback (side+time
            // match), but scream — cancel-by-id won't be possible.
            eprintln!(
                "[execute] no order_id in place response for {} — using fills fallback; raw: {response}",
                order.ticker
            );
        }

        // Poll fills until fully filled or deadline (≤8s or the entry window,
        // whichever is smaller). Taker limits at the ask normally fill on the
        // first poll.
        let deadline_ms = ts_submit_ms + (signal.fill_wait_secs.min(8) as i64).max(1) * 1000;
        let mut filled = 0i64;
        let mut avg_price = order.limit_cents;
        let mut ts_fill_ms = None;
        loop {
            tokio::time::sleep(std::time::Duration::from_millis(400)).await;
            match self.kalshi.fills(&order.ticker).await {
                Ok(body) => {
                    let fills = kalshi::parse_fills(
                        &body,
                        order_id.as_deref(),
                        order.side.as_str(),
                        ts_submit_ms,
                    );
                    let (total, avg, ts) = kalshi::fills_summary(&fills);
                    filled = total.min(order.count);
                    if let Some(a) = avg {
                        avg_price = a;
                    }
                    ts_fill_ms = ts;
                }
                Err(e) => eprintln!("[execute] fills poll failed for {}: {e}", order.ticker),
            }
            if filled >= order.count || now_ms() >= deadline_ms {
                break;
            }
        }

        // Cancel any unfilled remainder — never leave a resting order alive.
        let canceled = order.count - filled;
        if canceled > 0 {
            match &order_id {
                Some(id) => {
                    if let Err(e) = self.kalshi.cancel_order(id).await {
                        // Could not confirm the cancel: possible stranded resting
                        // order. Loud alert — this violates taker-only doctrine.
                        eprintln!("[execute] CANCEL FAILED for {} ({id}): {e}", order.ticker);
                        crate::alert::notify(
                            &self.http,
                            &format!(
                                "CANCEL FAILED {} order {id} — possible resting order, check Kalshi UI",
                                order.ticker
                            ),
                        )
                        .await;
                    }
                }
                None => {
                    crate::alert::notify(
                        &self.http,
                        &format!(
                            "no order_id for {} — cannot cancel remainder ({} unfilled), check Kalshi UI",
                            order.ticker, canceled
                        ),
                    )
                    .await;
                }
            }
        }

        // Feed risk ONLY the filled count at the ACTUAL average price.
        if filled > 0 {
            self.risk
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .on_fill_actual(&order, filled, avg_price);
        }

        let fill = FillReport {
            requested: order.count,
            filled,
            fill_price_cents: avg_price,
            canceled,
            partial: filled > 0 && filled < order.count,
            simulated: false,
            ts_submit_ms,
            ts_ack_ms: Some(ts_ack_ms),
            ts_fill_ms,
            order_id,
        };
        if filled > 0 {
            ExecOutcome::Filled {
                order,
                fill,
                response,
            }
        } else {
            ExecOutcome::Missed { order, fill }
        }
    }
}

#[async_trait::async_trait]
pub trait Strategy {
    fn name(&self) -> &str;
    async fn run(&self, eng: &Engine) -> anyhow::Result<()>;
}
