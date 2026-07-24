//! The Strategy trait + shared Engine context + the order-execution router.
//! New edges implement `Strategy` and route every order through `Engine::execute`,
//! which applies the Risk layer. Strategies never place raw orders themselves.
//!
//! EXECUTION TRUTH (redirect 2026-07-23): accepted ≠ filled. Live execution
//! verifies fills via `/portfolio/fills`, records ACTUAL price/count/timestamps,
//! feeds risk only the filled count, and cancels any unfilled remainder — a
//! resting order is never left alive (taker-only doctrine).

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::kalshi::{self, Kalshi};
use crate::risk::{Order, Rejection, RiskManager, Signal};
use crate::{alert, net};

/// Consecutive failed order placements / signed-call errors before the sticky
/// kill-switch trips (OSS addendum #3). Sticky until `resume` — no auto-cooldown.
pub const MAX_CONSEC_ERRORS: u32 = 5;
/// Consecutive signed-request 401s before a loud alert (clock-skew after Mac
/// sleep: public data flows, signed calls 401). Alert-only — a resync recovers.
pub const MAX_CONSEC_AUTH_FAILS: u32 = 5;
/// In-window network deadline: the shared client's 30s timeout is half a 60s
/// entry window. Fail fast so the window can continue (OSS addendum #5).
pub const IN_WINDOW_TIMEOUT: Duration = Duration::from_secs(5);
/// Live pre-order balance cache TTL — one balance read per scan pass, reused
/// across the BTC+ETH orders in that pass (OSS addendum #4).
const BALANCE_CACHE_TTL: Duration = Duration::from_secs(5);

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
    pub actual_fee_cents: Option<f64>,
    /// True for paper-mode simulated fills.
    pub simulated: bool,
    /// True when `fill_price_cents` is a FALLBACK (the order filled but the
    /// exchange gave us no average_fill_price, so we used the order limit). The
    /// participation record is tagged `price_estimated` so week-1 accounting
    /// knows the price is not exchange-confirmed (fix 7a).
    pub price_estimated: bool,
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
    /// Live placement returned an error, BUT a follow-up fills/positions query
    /// found that a fill actually landed (lost-ack recovery, fix 1a). The fill is
    /// recorded in risk state exactly as a success would be — a real position is
    /// NEVER booked as "nothing happened".
    RecoveredFill { order: Order, fill: FillReport },
    /// Live order placement errored AND no fill was found on recovery (nothing is
    /// resting — IOC — and nothing filled; a re-run with the deterministic
    /// client_order_id is safe).
    OrderError(String),
}

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

/// Run `fut` under the in-window network deadline ([`IN_WINDOW_TIMEOUT`]). Lets
/// strategy crates fail fast on hot-path calls without depending on tokio
/// directly. `Err(Elapsed)` = the deadline passed (OSS addendum #5).
pub async fn in_window<F: std::future::Future>(
    fut: F,
) -> Result<F::Output, tokio::time::error::Elapsed> {
    tokio::time::timeout(IN_WINDOW_TIMEOUT, fut).await
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
    /// Consecutive signed-call / placement failures (sticky-halt breaker).
    pub consec_errors: AtomicU32,
    /// Consecutive signed-request 401s (clock-skew alert counter).
    pub consec_auth_fails: AtomicU32,
    /// Cached live cash balance (cents) + when read, for the per-scan-pass
    /// pre-order affordability check.
    balance_cache: Mutex<Option<(Instant, i64)>>,
}

impl Engine {
    /// Build an Engine with the failure counters zeroed and no cached balance.
    pub fn new(
        kalshi: Kalshi,
        http: reqwest::Client,
        mode: Mode,
        risk: RiskManager,
        cities: Vec<crate::config::City>,
    ) -> Self {
        Engine {
            kalshi,
            http,
            mode,
            risk: Mutex::new(risk),
            cities,
            exec_lock: tokio::sync::Mutex::new(()),
            consec_errors: AtomicU32::new(0),
            consec_auth_fails: AtomicU32::new(0),
            balance_cache: Mutex::new(None),
        }
    }

    /// Reset the consecutive-failure and 401 breakers after any signed-call
    /// success (order placed / recovered / clean settlement pass).
    pub fn note_signed_success(&self) {
        self.consec_errors.store(0, Ordering::Relaxed);
        self.consec_auth_fails.store(0, Ordering::Relaxed);
    }

    /// Record a signed-call / placement failure. Non-retryable (or unknown/
    /// timeout) statuses feed the sticky-halt breaker; a 401 additionally feeds
    /// the clock-skew alert counter. Retryable (429/5xx) statuses are handled by
    /// backoff in the loops and do NOT trip the sticky halt. Alerts + halts as
    /// thresholds are crossed.
    pub async fn note_order_failure(&self, status: Option<u16>) {
        if status == Some(401) {
            let n = self.consec_auth_fails.fetch_add(1, Ordering::Relaxed) + 1;
            if n >= MAX_CONSEC_AUTH_FAILS {
                eprintln!("[breaker] {n} consecutive signed-request 401s — likely clock skew (Mac sleep). Resync time.");
                alert::notify(
                    &self.http,
                    &format!("{n} consecutive 401s on signed calls — CLOCK SKEW likely (resync NTP); bot cannot trade"),
                )
                .await;
            }
        }
        // A retryable status is transient — let backoff handle it, don't trip the
        // sticky halt. Everything else (incl. timeouts/None, 4xx) counts.
        if status.is_none_or(|s| !net::is_retryable_status(s)) {
            let n = self.consec_errors.fetch_add(1, Ordering::Relaxed) + 1;
            if n >= MAX_CONSEC_ERRORS {
                eprintln!("[breaker] {n} consecutive placement/signed-call failures — HALTING (sticky until resume)");
                self.risk
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .halt();
                alert::notify(
                    &self.http,
                    &format!("{n} consecutive placement/signed-call failures — HALTED (sticky; run `resume` after fixing)"),
                )
                .await;
            }
        }
    }

    /// Note a signed GET failure (reconcile/scan). Same breaker + 401 accounting
    /// as [`note_order_failure`]; named for the caller's clarity.
    pub async fn note_signed_failure(&self, status: Option<u16>) {
        self.note_order_failure(status).await;
    }

    /// Live cash balance in cents, cached for [`BALANCE_CACHE_TTL`] so one read
    /// covers all orders in a scan pass. Returns None if the balance can't be
    /// read (caller decides — a failed read must not silently allow an order).
    async fn live_balance_cents(&self) -> Option<i64> {
        if let Some((at, bal)) = *self.balance_cache.lock().unwrap_or_else(|e| e.into_inner()) {
            if at.elapsed() < BALANCE_CACHE_TTL {
                return Some(bal);
            }
        }
        match self.kalshi.balance_cents().await {
            Ok(bal) => {
                *self.balance_cache.lock().unwrap_or_else(|e| e.into_inner()) =
                    Some((Instant::now(), bal));
                Some(bal)
            }
            Err(e) => {
                eprintln!("[balance] live balance read failed: {e}");
                None
            }
        }
    }

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
            self.execute_live(order).await
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
                price_estimated: false,
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
    async fn execute_live(&self, order: Order) -> ExecOutcome {
        // Deterministic client_order_id (strategy + market ticker): if we die
        // after Kalshi accepts but before recording, a re-run resends the SAME
        // id and Kalshi dedupes it. One order per market is the design.
        //
        // EMPIRICALLY VERIFIED on demo 2026-07-23 (kalshi::tests::
        // demo_duplicate_coid_behavior, fix 2b): a duplicate client_order_id is
        // REJECTED with HTTP 409 {"code":"order_already_exists"} — Kalshi does NOT
        // echo the original order as a 2xx. So a re-fire with the same coid can
        // never double-book P&L: the safe branch. (We still never auto-re-POST an
        // order — a lost ack is resolved by the fills-query recovery below, not a
        // retry.)
        let coid = format!("{}-{}", order.strategy, order.ticker);

        // Pre-order affordability check (OSS addendum #4): don't fire a doomed
        // order the account can't cover. One balance read per scan pass (cached).
        // A failed balance read is fail-open (trading must not stall on a single
        // read miss; risk caps still bound the order).
        let cost_cents = order.count * order.limit_cents;
        if let Some(bal) = self.live_balance_cents().await {
            if bal < cost_cents {
                eprintln!(
                    "[balance] refusing {} order: need {cost_cents}c, balance {bal}c",
                    order.ticker
                );
                alert::notify(
                    &self.http,
                    &format!(
                        "refused {} order — insufficient balance (need {cost_cents}c, have {bal}c)",
                        order.ticker
                    ),
                )
                .await;
                return ExecOutcome::OrderError(format!(
                    "insufficient balance: need {cost_cents}c, have {bal}c"
                ));
            }
        }

        let ts_submit_ms = now_ms();
        // Fail fast in-window (OSS addendum #5): a 5s deadline, not the client's
        // 30s. NOTE: the order POST is NEVER retried (double-submit risk) — a
        // timeout/error goes down the lost-ack recovery path (a fills GET).
        let place_res = tokio::time::timeout(
            IN_WINDOW_TIMEOUT,
            self.kalshi.place_limit_buy(
                &order.ticker,
                order.side.as_str(),
                order.count,
                order.limit_cents,
                &coid,
            ),
        )
        .await;
        let response = match place_res {
            Ok(Ok(r)) => r,
            // LOST-ACK RECOVERY (fix 1a): the POST errored/timed out, but Kalshi
            // may have ACCEPTED and FILLED it. A real position booked as "nothing
            // happened" would make the kill-switch compute on a lie. Query the
            // truth (a fills GET) before giving up.
            Ok(Err(e)) => return self.recover_lost_ack(order, ts_submit_ms, &e).await,
            Err(_elapsed) => {
                let e = anyhow::anyhow!(
                    "order placement timed out after {}s",
                    IN_WINDOW_TIMEOUT.as_secs()
                );
                return self.recover_lost_ack(order, ts_submit_ms, &e).await;
            }
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
        // fill_price fallback: if the exchange omitted average_fill_price on a
        // filled order, fall back to the order limit and TAG it estimated (fix 7a).
        let price_estimated = filled > 0 && placed.fill_price_cents.is_none();
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
        // A completed placement (fill or clean IOC no-cross) resets the
        // consecutive-signed-failure breaker (fix: OSS addendum #3).
        self.note_signed_success();

        let fill = FillReport {
            requested: order.count,
            filled,
            fill_price_cents: avg_price,
            canceled,
            partial: filled > 0 && filled < order.count,
            actual_fee_cents: placed.actual_fee_cents,
            simulated: false,
            price_estimated,
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

    /// Lost-ack recovery (fix 1a): the order POST errored, but Kalshi may have
    /// accepted and FILLED it. Query fills (best-effort, single GET — we NEVER
    /// re-POST an order: double-submit risk). If a fill landed, record it exactly
    /// as a success and return `RecoveredFill`; otherwise `OrderError`.
    async fn recover_lost_ack(
        &self,
        order: Order,
        ts_submit_ms: i64,
        place_err: &anyhow::Error,
    ) -> ExecOutcome {
        let coid = format!("{}-{}", order.strategy, order.ticker);
        // fills(&ticker) is scoped to this market; match by side + submit-time
        // window (we don't hold the exchange order_id — the ack was lost — and
        // one order per market is the design, so this is unambiguous).
        let recovered = match self.kalshi.fills(&order.ticker).await {
            Ok(body) => {
                let fills =
                    kalshi::parse_fills(&body, None, order.side.as_str(), ts_submit_ms);
                let (total, avg, ts) = kalshi::fills_summary(&fills);
                let filled = total.min(order.count);
                if filled > 0 {
                    Some((filled, avg, ts))
                } else {
                    None
                }
            }
            Err(fe) => {
                eprintln!(
                    "[recover] {} fills read failed during lost-ack recovery: {fe} \
                     (original place error: {place_err})",
                    order.ticker
                );
                None
            }
        };

        if let Some((filled, avg, ts_fill)) = recovered {
            let price_estimated = avg.is_none();
            let avg_price = avg.unwrap_or(order.limit_cents);
            self.risk
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .on_fill_actual(&order, filled, avg_price);
            // A recovered fill is a SUCCESS: reset the failure breaker.
            self.note_signed_success();
            let canceled = order.count - filled;
            eprintln!(
                "[recover] RECOVERED lost-ack fill for {} (coid {coid}): {filled}x @ {avg_price}c \
                 — a real position was NOT booked as nothing-happened",
                order.ticker
            );
            alert::notify(
                &self.http,
                &format!(
                    "RECOVERED lost-ack fill {} {}x @ {}c (place errored: {})",
                    order.ticker,
                    filled,
                    avg_price,
                    place_err
                ),
            )
            .await;
            let fill = FillReport {
                requested: order.count,
                filled,
                fill_price_cents: avg_price,
                canceled,
                partial: filled < order.count,
                actual_fee_cents: None,
                simulated: false,
                price_estimated,
                ts_submit_ms,
                ts_ack_ms: ts_fill,
                ts_fill_ms: ts_fill,
                order_id: None,
            };
            return ExecOutcome::RecoveredFill { order, fill };
        }

        // No fill found: a genuine placement failure. Feed the consecutive-error
        // breaker and return OrderError (a re-run with the deterministic coid is
        // safe — IOC leaves nothing resting).
        self.note_order_failure(net::http_status(place_err)).await;
        ExecOutcome::OrderError(place_err.to_string())
    }
}

#[async_trait::async_trait]
pub trait Strategy {
    fn name(&self) -> &str;
    async fn run(&self, eng: &Engine) -> anyhow::Result<()>;
}
