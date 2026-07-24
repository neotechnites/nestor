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
    /// ACTUAL total fee in cents from the exchange's `average_fee_paid` (None in
    /// paper mode or when nothing filled). Recorded ALONGSIDE our own taker-fee
    /// estimate — real fees are the mechanics-week deliverable.
    pub actual_fee_cents: Option<i64>,
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
                actual_fee_cents: None,
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

    /// Live path (V2 IOC create-order): place → read SYNCHRONOUS fill truth from
    /// the 201 response → record ONLY what filled. The order is
    /// immediate_or_cancel, so the exchange has already canceled any unfilled
    /// remainder — there is no resting order to clean up (taker-only doctrine is
    /// enforced by the exchange, not a follow-up cancel). We keep one best-effort
    /// fills read as a reconciliation cross-check and log any discrepancy.
    async fn execute_live(&self, order: Order, _signal: &Signal) -> ExecOutcome {
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

        // PRIMARY fill truth: parse the placement response itself.
        let placed = kalshi::parse_place_response(&response, order.side.as_str());
        let ts_ack_ms = placed.ts_ms.unwrap_or_else(now_ms);
        let order_id = placed.order_id.clone();
        if order_id.is_none() {
            eprintln!(
                "[execute] no order_id in v2 place response for {} — raw: {response}",
                order.ticker
            );
        }

        let filled = placed.fill_count.min(order.count);
        let avg_price = placed.fill_price_cents.unwrap_or(order.limit_cents);
        // IOC: the exchange canceled the remainder itself.
        let canceled = order.count - filled;
        let ts_fill_ms = if filled > 0 { placed.ts_ms } else { None };

        // Reconciliation cross-check: one fills read (best-effort). The fills API
        // is still live in V2; a mismatch is logged but the 201 response wins.
        match self.kalshi.fills(&order.ticker).await {
            Ok(body) => {
                let fills = kalshi::parse_fills(
                    &body,
                    order_id.as_deref(),
                    order.side.as_str(),
                    ts_submit_ms,
                );
                let (recon_total, _recon_avg, _) = kalshi::fills_summary(&fills);
                if recon_total.min(order.count) != filled {
                    eprintln!(
                        "[execute] RECON MISMATCH {} — response fill_count={} but fills API sees {} (using response)",
                        order.ticker, filled, recon_total
                    );
                }
            }
            Err(e) => eprintln!("[execute] recon fills read failed for {}: {e}", order.ticker),
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
            actual_fee_cents: placed.actual_fee_cents,
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
