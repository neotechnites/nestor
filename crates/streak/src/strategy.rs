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
use engine::strategy::{in_window, ExecOutcome, IN_WINDOW_TIMEOUT};
use engine::{alert, logging, Engine, Side, Signal, SizingHint, Strategy};
use serde_json::json;

use crate::derive::{self, Derivation, Verify};
use crate::signal::{self, Candidate, SettledWindow, Skip};

const WEEK1_LOG: &str = "data/streak_week1.jsonl";
const SERIES: [&str; 2] = ["KXBTC15M", "KXETH15M"];
/// Fast-poll horizon after each 15-min boundary: covers the 60s entry window
/// plus settlement-lag slack (a late-settling previous window can still convert
/// a PrevNotSettled skip into an entry inside the window).
const FAST_WINDOW_SECS: i64 = 75;
/// Pre-close spot-sampling horizon: the final N seconds BEFORE each 15-min
/// boundary, during which we sample coin spot at 1 Hz to reconstruct Kalshi's
/// 60s BRTI settlement average (derive-fourth). This zone is fast-polled too so
/// `scan_series` runs ~1/s and can take one sample per pass; 75s (> the 60s
/// settlement window) leaves slack for dropped Coinbase ticks.
const SAMPLE_WINDOW_SECS: i64 = 75;
/// How long a spot sample is retained: ~2× the sample horizon so the final
/// minute's samples survive across the boundary into the next window's entry
/// phase, where the derivation for the just-closed window consumes them.
const SPOT_RETENTION_SECS: i64 = 2 * SAMPLE_WINDOW_SECS;

/// Marker file whose PRESENCE disables derivation — checked both at startup and
/// live on every attempt. A USED-derivation disagreement with the official
/// result creates it (item 4); the position it opened stays (risk-managed).
const DERIVE_DISABLED_MARKER: &str = "data/derive_disabled";
/// Audit trail of derived-vs-official comparisons (agree/disagree).
const DERIVE_VERIFY_LOG: &str = "data/derive_verify.jsonl";

/// Adaptive polling cadence (redirect: 1-2s during entry windows, lazy outside;
/// a 15s cadence is too coarse for a 60s window). Fast (1s) in TWO zones per
/// window: the first `FAST_WINDOW_SECS` after a boundary (the entry window +
/// settlement-lag slack) AND the final `SAMPLE_WINDOW_SECS` before the next
/// boundary (the spot-sampling horizon feeding derive-fourth). Lazy 12s in the
/// idle middle. Pure — unit-tested. Never oversleeps past the next boundary.
pub fn next_poll_delay(now_unix: i64) -> std::time::Duration {
    let into_window = now_unix.rem_euclid(900);
    // Fast in the post-boundary entry zone or the pre-boundary sample zone.
    let in_idle_middle = (FAST_WINDOW_SECS..900 - SAMPLE_WINDOW_SECS).contains(&into_window);
    let secs = if in_idle_middle {
        (900 - into_window).clamp(1, 12)
    } else {
        1
    };
    std::time::Duration::from_secs(secs as u64)
}

/// Coinbase product id for a Kalshi crypto series, or None if unmapped.
fn coin_product(series: &str) -> Option<&'static str> {
    match series {
        "KXBTC15M" => Some("BTC-USD"),
        "KXETH15M" => Some("ETH-USD"),
        _ => None,
    }
}

/// `floor_strike` of the market that closes at `close_unix`, if present in `raw`.
fn strike_for_close(raw: &[Market], close_unix: i64) -> Option<f64> {
    raw.iter()
        .find(|m| m.close_unix() == Some(close_unix))
        .and_then(|m| m.floor_strike)
}

/// Derivation is enabled unless the disable marker exists (re-checked live so a
/// mid-run disagreement takes effect on the very next attempt).
fn derive_enabled() -> bool {
    !std::path::Path::new(DERIVE_DISABLED_MARKER).exists()
}

/// Repeat-skip alarm thresholds (item 5). A settlement-lag stall shows as
/// consecutive `prev_not_settled` skips — 2 in a row is already worth a shout;
/// any other reason repeating 5× (e.g. a market stuck unpriced) also warrants
/// one. Both are counted per series across scan passes.
const PREV_NOT_SETTLED_ALARM: u32 = 2;
const ANY_REASON_ALARM: u32 = 5;

/// The alarm/log kind for a skip, or None for the silent no-signal skips that
/// must NOT feed the repeat-skip alarm (they are the normal resting state, not a
/// malfunction). Shared by `log_skip` and the alarm so the two stay in step.
fn skip_kind(skip: &Skip) -> Option<&'static str> {
    match skip {
        Skip::NoStreak | Skip::InsufficientHistory | Skip::NotConsecutive => None,
        Skip::PrevNotSettled => Some("prev_not_settled"),
        Skip::WindowMismatch => Some("window_mismatch"),
        Skip::NotEntryWindow { .. } => Some("missed_entry_window"),
        Skip::Unpriced => Some("unpriced"),
        Skip::PriceAboveGate { .. } => Some("price_above_gate"),
    }
}

/// Advance the per-series consecutive-skip counter. Same `reason` as last
/// increments; a different reason resets to 1. Fires (once) on the exact
/// threshold crossing — at `PREV_NOT_SETTLED_ALARM` for a prev_not_settled run
/// and at `ANY_REASON_ALARM` for any run — so a stuck series alerts without
/// spamming every 1s pass. Pure — unit-tested.
fn skip_alarm_step(prev: Option<(&str, u32)>, reason: &str) -> (u32, bool) {
    let count = match prev {
        Some((r, c)) if r == reason => c + 1,
        _ => 1,
    };
    let fire = (reason == "prev_not_settled" && count == PREV_NOT_SETTLED_ALARM)
        || count == ANY_REASON_ALARM;
    (count, fire)
}

/// A derivation logged for later verification against the official result.
#[derive(Clone)]
struct PendingDerive {
    close_unix: i64,
    predicted: String,
    /// Whether the derived result actually drove an entry (auto-disable scope).
    used: bool,
    avg: f64,
    margin_bp: f64,
    ticker: String,
}

pub struct Streak {
    /// Dedup for participation records and order attempts: "{ticker}" for an
    /// entry attempt (one order per market, ever), "{ticker}|{kind}" for skip
    /// records (one per skip kind per market — a retryable skip may later
    /// convert, giving that market two records: the skip + the entry).
    seen: Mutex<HashSet<String>>,
    /// Recently-closed RAW markets cache per series: (window_id, markets). Valid
    /// for the whole 15-min window unless the previous window is still settling
    /// (the PrevNotSettled case marks a refetch for the next pass). Raw (not
    /// pre-filtered to results) so the just-closed market's `floor_strike` stays
    /// available for derivation.
    settled_cache: Mutex<HashMap<String, (i64, Vec<Market>)>>,
    /// Per-coin rolling spot samples (Coinbase product → (unix, price)), covering
    /// ~`SPOT_RETENTION_SECS` so the final-minute samples survive across the
    /// boundary into the next window's entry phase.
    spot_buf: Mutex<HashMap<String, Vec<(i64, f64)>>>,
    /// Derivations awaiting the official result, keyed "{series}|{close_unix}".
    derive_pending: Mutex<HashMap<String, PendingDerive>>,
    /// Per-series consecutive-skip run: (reason_kind, count) for the repeat-skip
    /// alarm. Reset on any successful evaluation / no-signal pass.
    skip_alarm: Mutex<HashMap<String, (String, u32)>>,
}

impl Streak {
    pub fn new() -> Self {
        Streak {
            seen: Mutex::new(HashSet::new()),
            settled_cache: Mutex::new(HashMap::new()),
            spot_buf: Mutex::new(HashMap::new()),
            derive_pending: Mutex::new(HashMap::new()),
            skip_alarm: Mutex::new(HashMap::new()),
        }
    }

    /// Advance (or reset) the repeat-skip alarm for `series`. `None` reason =
    /// successful evaluation / no-signal → reset. A `Some(kind)` that crosses a
    /// threshold emits a loud line and fires a webhook alert if configured.
    async fn note_skip_alarm(&self, eng: &Engine, series: &str, reason: Option<&str>) {
        let reason = match reason {
            Some(r) => r,
            None => {
                self.skip_alarm
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .remove(series);
                return;
            }
        };
        let (count, fire) = {
            let mut st = self.skip_alarm.lock().unwrap_or_else(|e| e.into_inner());
            let prev = st.get(series).map(|(r, c)| (r.as_str(), *c));
            let (count, fire) = skip_alarm_step(prev, reason);
            st.insert(series.to_string(), (reason.to_string(), count));
            (count, fire)
        };
        if fire {
            let msg = format!(
                "streak {series}: REPEAT-SKIP ALARM — {count} consecutive '{reason}' skips"
            );
            logging::info(format!("!!! {msg}"));
            alert::notify(&eng.http, &msg).await;
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

/// Current UTC time as RFC3339 (for the disable-marker provenance note).
fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339()
}

#[async_trait]
impl Strategy for Streak {
    fn name(&self) -> &str {
        "streak"
    }

    async fn run(&self, eng: &Engine) -> Result<()> {
        // CONCURRENT SCAN (item 4): the two series' scans are independent
        // read-heavy network round-trips; running them serially spent two RTTs per
        // pass inside a 60s entry window. `tokio::join!` drives both concurrently
        // on THIS task — while one awaits the network the other proceeds. This is
        // safe: the only place→record critical section runs through
        // `Engine::execute`, which serializes it under `exec_lock` (an async
        // Mutex held across the whole evaluate→place→verify→on_fill), so two
        // concurrent scans can never both clear a cap before either records its
        // fill. The shared `seen`/`settled_cache` mutexes are only ever held for
        // short, await-free critical sections, so concurrency can't deadlock them.
        let results = futures::future::join(
            self.scan_series(eng, SERIES[0]),
            self.scan_series(eng, SERIES[1]),
        )
        .await;

        // Per-series error isolation preserved: surface the FIRST rate-limit/
        // server-class error so the driving loop can back off (fix 5);
        // non-retryable per-series errors stay logged+swallowed (one coin's bad
        // pass must not abort the other's — already run above, independently).
        let mut retryable: Option<anyhow::Error> = None;
        for (series, res) in [(SERIES[0], results.0), (SERIES[1], results.1)] {
            if let Err(e) = res {
                let is_retryable = engine::net::http_status(&e)
                    .is_some_and(engine::net::is_retryable_status);
                if is_retryable && retryable.is_none() {
                    retryable = Some(e);
                } else {
                    logging::info(format!("streak {series}: scan error ({e}) — skip"));
                }
            }
        }
        match retryable {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }
}

impl Streak {
    /// Recently-closed RAW markets for `series`, cached per 15-min window;
    /// `force` refetches. Raw so callers keep `floor_strike` for derivation;
    /// `settled_windows()` filters to non-empty results on demand.
    async fn settled_for(
        &self,
        eng: &Engine,
        series: &str,
        window_id: i64,
        force: bool,
    ) -> Result<Vec<Market>> {
        if !force {
            let cache = self.settled_cache.lock().unwrap_or_else(|e| e.into_inner());
            if let Some((wid, markets)) = cache.get(series) {
                if *wid == window_id {
                    return Ok(markets.clone());
                }
            }
        }
        // Status-agnostic: settled-filter lags results (2026-07-24 live finding);
        // fetch by close-time window, settled_windows() keeps non-empty results.
        let raw = eng.kalshi.recent_closed(series, 3 * 3600, 12).await?;
        self.settled_cache
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(series.to_string(), (window_id, raw.clone()));
        Ok(raw)
    }

    fn refetch_key(series: &str, window_id: i64) -> String {
        format!("refetch|{series}|{window_id}")
    }

    async fn scan_series(&self, eng: &Engine, series: &str) -> Result<()> {
        let now_dt = chrono::Utc::now();
        let now = now_dt.timestamp();
        let window_id = now.div_euclid(900);

        // SPOT SAMPLER (item 1): in the final SAMPLE_WINDOW_SECS before the next
        // boundary — fast-polled, so ~1 pass/s — take one cheap Coinbase spot
        // sample per coin, building the buffer that derive-fourth consumes after
        // the boundary. Lazy phase never reaches here.
        if now.rem_euclid(900) >= 900 - SAMPLE_WINDOW_SECS {
            if let Some(product) = coin_product(series) {
                self.maybe_sample_spot(eng, product, now).await;
            }
        }

        // Refetch settled results while the previous window is still settling
        // (flagged by a prior pass's PrevNotSettled), else serve from cache.
        let force = self.seen_contains(&Self::refetch_key(series, window_id));
        let raw = self.settled_for(eng, series, window_id, force).await?;
        let settled = settled_windows(&raw);

        // VERIFY (item 4): any pending derivation whose official result has now
        // landed gets compared here; a used-derivation disagreement disables
        // derivation loudly.
        self.verify_pending(eng, series, &settled).await;
        // Fail fast in-window (addendum #5): a 5s deadline beats the client's 30s
        // (half an entry window). A timeout skips THIS pass; the loop retries.
        let opens = match in_window(eng.kalshi.markets(series, "open")).await {
            Ok(r) => r?,
            Err(_) => {
                logging::info(format!(
                    "streak {series}: open-markets fetch exceeded {}s — skip pass",
                    IN_WINDOW_TIMEOUT.as_secs()
                ));
                return Ok(());
            }
        };

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
            Ok(entry) => {
                self.note_skip_alarm(eng, series, None).await; // evaluated → reset
                self.enter(eng, series, cur, &cand, entry, now, None).await
            }
            Err(Skip::PrevNotSettled) => {
                // Ask subsequent passes in this window to refetch settled (the
                // official result may still arrive and supersede any derivation).
                self.first_time(Self::refetch_key(series, window_id));
                // DERIVE-FOURTH (item 3): synthesize the just-closed window's
                // result from our spot buffer rather than waiting out the lag.
                // derive_prev notes the skip alarm on its own non-entry paths.
                self.derive_prev(eng, series, cur, &cand, &raw, &settled, now)
                    .await
            }
            Err(skip) => {
                self.note_skip_alarm(eng, series, skip_kind(&skip)).await;
                self.log_skip(eng, series, &cur.ticker, &skip).await;
                Ok(())
            }
        }
    }

    /// Sample coin spot once per second at most; append to the rolling buffer and
    /// prune to `SPOT_RETENTION_SECS`. Best-effort — a Coinbase error drops the
    /// tick, never the pass.
    async fn maybe_sample_spot(&self, eng: &Engine, product: &str, now: i64) {
        {
            // One sample per wall-clock second per coin (a pass that repeats
            // within the same second must not double-sample).
            let buf = self.spot_buf.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(v) = buf.get(product) {
                if v.last().is_some_and(|&(t, _)| t >= now) {
                    return;
                }
            }
        }
        let price = match engine::spot::spot_price(&eng.http, product).await {
            Ok(p) => p,
            Err(e) => {
                logging::info(format!("streak spot sample {product} failed: {e}"));
                return;
            }
        };
        let mut buf = self.spot_buf.lock().unwrap_or_else(|e| e.into_inner());
        let v = buf.entry(product.to_string()).or_default();
        v.push((now, price));
        let cutoff = now - SPOT_RETENTION_SECS;
        v.retain(|&(t, _)| t >= cutoff);
    }

    /// Compare any matured pending derivations for `series` against the official
    /// result; write the audit record and, on a USED disagreement, disable
    /// derivation (marker file) with a CRITICAL alert. The position stays.
    async fn verify_pending(&self, eng: &Engine, series: &str, official: &[SettledWindow]) {
        let prefix = format!("{series}|");
        let matured: Vec<(String, PendingDerive, String)> = {
            let pend = self.derive_pending.lock().unwrap_or_else(|e| e.into_inner());
            pend.iter()
                .filter(|(k, _)| k.starts_with(&prefix))
                .filter_map(|(k, p)| {
                    official
                        .iter()
                        .find(|w| w.close_unix == p.close_unix)
                        .map(|w| (k.clone(), p.clone(), w.result.clone()))
                })
                .collect()
        };
        for (key, p, off) in matured {
            let outcome = derive::verify(&p.predicted, &off, p.used);
            logging::record_path(
                DERIVE_VERIFY_LOG,
                json!({
                    "event": "derive_verify",
                    "series": series,
                    "ticker": p.ticker,
                    "close_unix": p.close_unix,
                    "predicted": p.predicted,
                    "official": off,
                    "used": p.used,
                    "agree": outcome == Verify::Agree,
                    "derived_avg": p.avg,
                    "derived_margin_bp": p.margin_bp,
                }),
            );
            match outcome {
                Verify::Agree => logging::info(format!(
                    "streak {series}: derive VERIFIED {} == official {off} ({})",
                    p.predicted,
                    if p.used { "used" } else { "unused" }
                )),
                Verify::DisagreeUnused => logging::info(format!(
                    "streak {series}: WARN derive DISAGREE (unused) predicted {} \
                     official {off} for close {} — margin {:.1}bp",
                    p.predicted, p.close_unix, p.margin_bp
                )),
                Verify::DisagreeUsed => {
                    logging::info(format!(
                        "streak {series}: CRITICAL derive DISAGREE (USED) predicted {} \
                         official {off} for {} (close {}, {:.1}bp) — DISABLING derivation; \
                         position stays (risk-managed)",
                        p.predicted, p.ticker, p.close_unix, p.margin_bp
                    ));
                    if let Err(e) = std::fs::write(
                        DERIVE_DISABLED_MARKER,
                        format!(
                            "disabled {} — used derivation on {} predicted {} but official {off}\n",
                            now_iso(),
                            p.ticker,
                            p.predicted
                        ),
                    ) {
                        logging::info(format!(
                            "streak {series}: FAILED writing derive-disable marker: {e}"
                        ));
                    }
                    alert::notify(
                        &eng.http,
                        &format!(
                            "streak CRITICAL: derived-fourth WRONG on {} (predicted {}, official \
                             {off}) — derivation DISABLED, position held",
                            p.ticker, p.predicted
                        ),
                    )
                    .await;
                }
            }
            self.derive_pending
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .remove(&key);
        }
    }

    /// PrevNotSettled handler: try to derive the just-closed window's result from
    /// the spot buffer. Decisive → re-run detection with the synthesized 4th
    /// result and enter (tagged derived) if it qualifies. Marginal/Insufficient
    /// → current behavior (log the skip, retry next pass).
    #[allow(clippy::too_many_arguments)]
    async fn derive_prev(
        &self,
        eng: &Engine,
        series: &str,
        cur: &Market,
        cand: &Candidate,
        raw: &[Market],
        settled: &[SettledWindow],
        now: i64,
    ) -> Result<()> {
        let jc_close = cur
            .open_unix()
            .unwrap_or(cand.close_unix - signal::WINDOW_SECS);

        // Gate: feature on, coin mapped, strike known. Any miss → normal skip
        // (still a prev_not_settled-class skip for the repeat-skip alarm).
        if !derive_enabled() {
            self.note_skip_alarm(eng, series, Some("prev_not_settled")).await;
            self.log_skip(eng, series, &cur.ticker, &Skip::PrevNotSettled).await;
            return Ok(());
        }
        let product = match coin_product(series) {
            Some(p) => p,
            None => {
                self.note_skip_alarm(eng, series, Some("prev_not_settled")).await;
                self.log_skip(eng, series, &cur.ticker, &Skip::PrevNotSettled).await;
                return Ok(());
            }
        };
        let strike = match strike_for_close(raw, jc_close) {
            Some(s) if s > 0.0 => s,
            _ => {
                self.note_skip_alarm(eng, series, Some("prev_not_settled")).await;
                self.log_skip(eng, series, &cur.ticker, &Skip::PrevNotSettled).await;
                return Ok(());
            }
        };
        let samples = self
            .spot_buf
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(product)
            .cloned()
            .unwrap_or_default();

        match derive::derive(&samples, strike, jc_close) {
            Derivation::Derived {
                result,
                avg,
                margin_bp,
            } => {
                // Prepend the synthesized (newest) window and re-run detection.
                let mut settled2 = Vec::with_capacity(settled.len() + 1);
                settled2.push(SettledWindow {
                    close_unix: jc_close,
                    result: result.to_string(),
                });
                settled2.extend_from_slice(settled);
                let redetect = signal::detect(&settled2, cand, now);
                let used = redetect.is_ok();

                self.derive_pending.lock().unwrap_or_else(|e| e.into_inner()).insert(
                    format!("{series}|{jc_close}"),
                    PendingDerive {
                        close_unix: jc_close,
                        predicted: result.to_string(),
                        used,
                        avg,
                        margin_bp,
                        ticker: cur.ticker.clone(),
                    },
                );
                logging::record_path(
                    WEEK1_LOG,
                    json!({
                        "event": "streak_derive",
                        "series": series,
                        "ticker": cur.ticker,
                        "close_unix": jc_close,
                        "predicted": result,
                        "derived_avg": avg,
                        "derived_margin_bp": margin_bp,
                        "strike": strike,
                        "buf_samples": samples.len(),
                        "used": used,
                    }),
                );
                logging::info(format!(
                    "streak {series}: DERIVED {result} for {} (avg {avg:.2} vs strike {strike:.2}, \
                     {margin_bp:.1}bp, {} buf) — {}",
                    cur.ticker,
                    samples.len(),
                    if used { "completes streak, entering" } else { "no entry" }
                ));

                match redetect {
                    Ok(entry) => {
                        // Derived result completed the streak → a real evaluation.
                        self.note_skip_alarm(eng, series, None).await;
                        self.enter(eng, series, cur, cand, entry, now, Some((avg, margin_bp)))
                            .await
                    }
                    Err(skip) => {
                        self.note_skip_alarm(eng, series, Some("prev_not_settled")).await;
                        self.log_skip(eng, series, &cur.ticker, &skip).await;
                        Ok(())
                    }
                }
            }
            Derivation::Marginal { avg, margin_bp } => {
                logging::info(format!(
                    "streak {series}: derive MARGINAL for {} (avg {avg:.2} vs strike {strike:.2}, \
                     {margin_bp:.1}bp < gate) — normal skip",
                    cur.ticker
                ));
                self.note_skip_alarm(eng, series, Some("prev_not_settled")).await;
                self.log_skip(eng, series, &cur.ticker, &Skip::PrevNotSettled).await;
                Ok(())
            }
            Derivation::Insufficient {
                samples: nsamp,
                span_secs,
            } => {
                logging::info(format!(
                    "streak {series}: derive INSUFFICIENT for {} ({nsamp} samples / {span_secs}s \
                     span) — normal skip",
                    cur.ticker
                ));
                self.note_skip_alarm(eng, series, Some("prev_not_settled")).await;
                self.log_skip(eng, series, &cur.ticker, &Skip::PrevNotSettled).await;
                Ok(())
            }
        }
    }

    /// Log a skip once per (ticker, kind), with the order-book decision snapshot.
    /// No-signal cases stay silent — only streak-relevant dispositions are data.
    async fn log_skip(&self, eng: &Engine, series: &str, ticker: &str, skip: &Skip) {
        let kind = match skip_kind(skip) {
            Some(k) => k,
            None => return, // silent no-signal skips
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

    #[allow(clippy::too_many_arguments)]
    async fn enter(
        &self,
        eng: &Engine,
        series: &str,
        cur: &Market,
        cand: &Candidate,
        entry: signal::Entry,
        now: i64,
        // Some((avg, margin_bp)) when this entry rests on a DERIVED 4th result —
        // tags the participation record derived_fourth:true (item 3).
        derived: Option<(f64, f64)>,
    ) -> Result<()> {
        // One order attempt per market, ever (missed fills are DATA, never
        // chased; the deterministic client_order_id also dedupes across restarts).
        if !self.first_time(cur.ticker.clone()) {
            return Ok(());
        }

        // DATA CAPTURE 2 — decision snapshot at the entry moment (fetched before
        // the order so the book reflects what we saw when deciding).
        let book = match in_window(eng.kalshi.orderbook(&cur.ticker)).await {
            Ok(Ok(b)) => b,
            _ => json!(null), // timeout or error: book snapshot is best-effort
        };

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
            "book": book,
        });
        if let Some((avg, margin_bp)) = derived {
            rec["derived_fourth"] = json!(true);
            rec["derived_avg"] = json!(avg);
            rec["derived_margin_bp"] = json!(margin_bp);
        }

        match &outcome {
            ExecOutcome::Filled { fill, response, .. } => {
                let fee_cents = taker_fee(fill.fill_price_cents, fill.filled) * 100.0;
                rec["filled"] = json!(true);
                rec["partial"] = json!(fill.partial);
                rec["simulated"] = json!(fill.simulated);
                rec["price_estimated"] = json!(fill.price_estimated);
                rec["ts_submit"] = json!(fill.ts_submit_ms);
                rec["ts_ack"] = json!(fill.ts_ack_ms);
                rec["ts_fill"] = json!(fill.ts_fill_ms);
                rec["fill_price"] = json!(fill.fill_price_cents);
                rec["filled_count"] = json!(fill.filled);
                rec["canceled_count"] = json!(fill.canceled);
                rec["fee_cents"] = json!(fee_cents); // our pre-trade estimate
                rec["actual_fee_cents"] = json!(fill.actual_fee_cents); // exchange truth
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
            ExecOutcome::RecoveredFill { fill, .. } => {
                // Lost-ack recovery (fix 1a): a fill that landed despite a placement
                // error, recovered via the fills API. Recorded exactly like a normal
                // fill, tagged `recovered` so week-1 accounting can see it happened.
                let fee_cents = taker_fee(fill.fill_price_cents, fill.filled) * 100.0;
                rec["filled"] = json!(true);
                rec["recovered"] = json!(true);
                rec["partial"] = json!(fill.partial);
                rec["simulated"] = json!(false);
                rec["price_estimated"] = json!(fill.price_estimated);
                rec["ts_submit"] = json!(fill.ts_submit_ms);
                rec["ts_ack"] = json!(fill.ts_ack_ms);
                rec["ts_fill"] = json!(fill.ts_fill_ms);
                rec["fill_price"] = json!(fill.fill_price_cents);
                rec["filled_count"] = json!(fill.filled);
                rec["canceled_count"] = json!(fill.canceled);
                rec["fee_cents"] = json!(fee_cents);
                rec["actual_fee_cents"] = json!(fill.actual_fee_cents);
                rec["order_id"] = json!(fill.order_id);
                logging::info(format!(
                    "streak {series}: RECOVERED FILL {}x {} {} @ {}c (fade {}, lost-ack)",
                    fill.filled,
                    side.as_str(),
                    cur.ticker,
                    fill.fill_price_cents,
                    entry.streak_dir,
                ));
                // execute_live already fired a recovery alert; no second alert here.
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
    fn poll_delay_fast_in_both_zones_lazy_middle() {
        // A window boundary at a multiple of 900: first 75s → 1s cadence.
        let boundary = 900_000i64; // divisible by 900
        assert_eq!(next_poll_delay(boundary).as_secs(), 1);
        assert_eq!(next_poll_delay(boundary + 74).as_secs(), 1);
        // Idle middle [75, 825) → lazy 12s.
        assert_eq!(next_poll_delay(boundary + 75).as_secs(), 12);
        assert_eq!(next_poll_delay(boundary + 400).as_secs(), 12);
        assert_eq!(next_poll_delay(boundary + 824).as_secs(), 12);
        // Pre-close SAMPLE zone [825, 900) → fast 1s so the sampler runs ~1/s.
        assert_eq!(next_poll_delay(boundary + 825).as_secs(), 1);
        assert_eq!(next_poll_delay(boundary + 895).as_secs(), 1);
        assert_eq!(next_poll_delay(boundary + 899).as_secs(), 1);
    }

    #[test]
    fn coin_product_maps_both_series() {
        assert_eq!(coin_product("KXBTC15M"), Some("BTC-USD"));
        assert_eq!(coin_product("KXETH15M"), Some("ETH-USD"));
        assert_eq!(coin_product("KXDOGE15M"), None);
    }

    #[test]
    fn skip_alarm_prev_not_settled_fires_at_two() {
        // First prev_not_settled → count 1, no fire.
        let (c1, f1) = skip_alarm_step(None, "prev_not_settled");
        assert_eq!((c1, f1), (1, false));
        // Second consecutive → count 2, FIRE.
        let (c2, f2) = skip_alarm_step(Some(("prev_not_settled", 1)), "prev_not_settled");
        assert_eq!((c2, f2), (2, true));
        // Third → count 3, no re-fire (only the exact threshold crossing alerts).
        let (c3, f3) = skip_alarm_step(Some(("prev_not_settled", 2)), "prev_not_settled");
        assert_eq!((c3, f3), (3, false));
        // Fifth → count 5, fires again on the any-reason threshold.
        let (c5, f5) = skip_alarm_step(Some(("prev_not_settled", 4)), "prev_not_settled");
        assert_eq!((c5, f5), (5, true));
    }

    #[test]
    fn skip_alarm_other_reason_fires_at_five_only() {
        // A non-prev_not_settled reason must NOT fire at 2.
        let (_, f2) = skip_alarm_step(Some(("price_above_gate", 1)), "price_above_gate");
        assert!(!f2);
        // Five in a row → fire.
        let (c5, f5) = skip_alarm_step(Some(("price_above_gate", 4)), "price_above_gate");
        assert_eq!((c5, f5), (5, true));
    }

    #[test]
    fn skip_alarm_different_reason_resets() {
        // Switching reason resets the run to 1 regardless of prior count.
        let (c, f) = skip_alarm_step(Some(("prev_not_settled", 4)), "unpriced");
        assert_eq!((c, f), (1, false));
    }

    #[test]
    fn skip_kind_silences_no_signal() {
        assert_eq!(skip_kind(&Skip::NoStreak), None);
        assert_eq!(skip_kind(&Skip::InsufficientHistory), None);
        assert_eq!(skip_kind(&Skip::NotConsecutive), None);
        assert_eq!(skip_kind(&Skip::PrevNotSettled), Some("prev_not_settled"));
        assert_eq!(
            skip_kind(&Skip::PriceAboveGate { ask: 50.0 }),
            Some("price_above_gate")
        );
    }

    #[test]
    fn strike_for_close_finds_just_closed_market() {
        let m: Market = serde_json::from_value(json!({
            "ticker": "B",
            "close_time": "2026-07-23T10:15:00Z",
            "floor_strike": 118250.0,
        }))
        .unwrap();
        let close = chrono::DateTime::parse_from_rfc3339("2026-07-23T10:15:00Z")
            .unwrap()
            .timestamp();
        let ms = vec![m];
        assert_eq!(strike_for_close(&ms, close), Some(118250.0));
        assert_eq!(strike_for_close(&ms, close + 1), None); // no market at that close
    }
}
