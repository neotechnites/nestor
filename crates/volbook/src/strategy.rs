//! Live volbook sleeve (strategy #2) — metal daily-wing seller.
//!
//! Scans the METAL daily ladders (KXGOLDD, KXSILVERD, KXCOPPERD) Mon-Wed near
//! T-3h and SELLS the systematically-rich OTM wings by buying NO on rungs whose
//! implied YES sits in the calibrated wing band [0.05, 0.35). The limit is a
//! willingness-to-pay ceiling derived from the corpus realized-touch table
//! (`data/volbook_calib.json`), never a transcription of the book. Orders route
//! through `Engine::execute` (taker IOC); positions hold to the daily settlement
//! and are closed by the shared reconcile loop.
//!
//! GATING: this sleeve is PAPER-ONLY until sized. Standalone `volbook` is banned
//! in live mode by nestor_bin (like weather/lock), it is NOT scheduled in `run`,
//! and — defense in depth — it refuses to place a REAL order unless the operator
//! sets `VOLBOOK_LIVE=1` explicitly. Paper mode shadows the full strategy so the
//! edge can be measured forward before any real money is sized against it.
//!
//! DATA CAPTURE: every qualifying decision and every skip-with-a-reason is logged
//! to `data/volbook.jsonl` with the order-book snapshot — nestor keeps everything.

use std::collections::HashSet;
use std::sync::Mutex;

use anyhow::Result;
use async_trait::async_trait;
use chrono_tz::America::New_York;
use engine::kalshi::Market;
use engine::risk::taker_fee;
use engine::strategy::{in_window, ExecOutcome, Mode};
use engine::{alert, logging, Engine, Side, Signal, SizingHint, Strategy};
use serde_json::json;

use crate::calib::Calib;
use crate::signal::{self, Params, Skip};

const LOG: &str = "data/volbook.jsonl";
/// Default calibration artifact path (override with `VOLBOOK_CALIB_PATH`).
const DEFAULT_CALIB: &str = "data/volbook_calib.json";
/// Env flag that must be `1` for the sleeve to place a REAL (live) order.
const LIVE_ENABLE_ENV: &str = "VOLBOOK_LIVE";
/// Entry attempts per rung episode: 1 initial IOC + 1 retry. The wing NO books
/// carry deep resting size, so a cross at/under the ceiling normally fills at
/// once; a single retry covers a transient partial without chasing.
const MAX_ENTRY_ATTEMPTS: u32 = 2;
const RETRY_SPACING_MS: u64 = 1000;
/// How many LATER passes a rung that ended `Missed` may be retried on (FIX 11,
/// constants F4). BOUNDED, not unlimited: the entry window is 3600s scanned every
/// 60s, so an unbounded re-arm would fire 2 IOCs per pass × 60 passes × ~11
/// qualifying rungs = ~1300 signed writes a day against a rate limit we have
/// never measured (reality F5), and would bury the tape in `missed_fill` records.
/// 5 is the horizon the sizing probe itself measures — verify-volbook-execution
/// P3 asks for the fraction of rungs still at/below the ceiling on passes k+1..k+5
/// — so it captures the persistence the fix exists to recover and stops exactly
/// where the evidence stops. Raise it only with that measurement in hand.
const MAX_REARMS: u32 = 5;

pub struct Volbook {
    calib: Calib,
    params: Params,
    /// (series, family, weight) universe from the enabled families.
    universe: Vec<(String, String, f64)>,
    /// Dedup: one entry episode per rung ticker (per process/day), and one skip
    /// record per (ticker, reason).
    seen: Mutex<HashSet<String>>,
    /// ticker -> how many times a `Missed` episode has been re-armed (FIX 11),
    /// bounded by [`MAX_REARMS`].
    rearms: Mutex<std::collections::HashMap<String, u32>>,
}

impl Volbook {
    /// Build from the calibration artifact at `path`.
    pub fn from_path(path: &str) -> Result<Self> {
        let calib = Calib::load(path)?;
        let params = Params {
            wing_lo: calib.wing_lo,
            wing_hi: calib.wing_hi,
            ttc_lo: calib.entry_ttc_lo_secs,
            ttc_hi: calib.entry_ttc_hi_secs,
            margin_cents: calib.margin_cents,
        };
        let universe = calib.enabled_series();
        Ok(Volbook {
            calib,
            params,
            universe,
            seen: Mutex::new(HashSet::new()),
            rearms: Mutex::new(std::collections::HashMap::new()),
        })
    }

    /// Build from the default (or `VOLBOOK_CALIB_PATH`) artifact path.
    pub fn new() -> Result<Self> {
        let path =
            std::env::var("VOLBOOK_CALIB_PATH").unwrap_or_else(|_| DEFAULT_CALIB.to_string());
        Self::from_path(&path)
    }

    /// A summary line of the loaded, enabled universe (for startup logging).
    pub fn universe_summary(&self) -> String {
        let series: Vec<String> = self
            .universe
            .iter()
            .map(|(s, _, w)| format!("{s}×{w}"))
            .collect();
        format!("{} enabled series: {}", self.universe.len(), series.join(", "))
    }

    fn first_time(&self, key: String) -> bool {
        self.seen.lock().unwrap_or_else(|e| e.into_inner()).insert(key)
    }

    /// Release a rung's one-episode-per-ticker dedupe so a LATER pass in the same
    /// entry window may try again (FIX 11). Only ever called for a zero-fill
    /// `Missed` outcome — nothing was bought, nothing is resting (IOC), and the
    /// next attempt re-runs `evaluate` against a fresh book and fresh caps.
    /// Returns the re-arm number, or None once [`MAX_REARMS`] is spent.
    fn rearm(&self, ticker: &str) -> Option<u32> {
        let n = {
            let mut r = self.rearms.lock().unwrap_or_else(|e| e.into_inner());
            let c = r.entry(ticker.to_string()).or_insert(0);
            *c += 1;
            *c
        };
        if n > MAX_REARMS {
            return None;
        }
        self.seen
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(ticker);
        Some(n)
    }
}

#[async_trait]
impl Strategy for Volbook {
    fn name(&self) -> &str {
        "volbook"
    }

    async fn run(&self, eng: &Engine) -> Result<()> {
        let mut retryable: Option<anyhow::Error> = None;
        let mut pool: Vec<Candidate> = Vec::new();
        // Sequential scan: this is an hourly-scale strategy (T-3h entry window),
        // not a sub-second one — no need for the concurrency streak needs. Gather
        // candidates across ALL metal series FIRST so they can be ranked together.
        for (series, family, weight) in &self.universe {
            match self.collect_series(eng, series, family, *weight).await {
                Ok(mut cands) => pool.append(&mut cands),
                Err(e) => {
                    let is_retryable = engine::net::http_status(&e)
                        .is_some_and(engine::net::is_retryable_status);
                    if is_retryable && retryable.is_none() {
                        retryable = Some(e);
                    } else {
                        logging::info(format!("volbook {series}: scan error ({e}) — skip"));
                    }
                }
            }
        }
        // RANK by calibration gap (verdict rule 5): richest wings first, so when
        // the shared per-day metal cluster cap binds, the highest-edge rungs win
        // (the cluster key pools gold+silver+copper on one ET day into one bet).
        pool.sort_by(|a, b| {
            b.entry
                .gap_pp
                .partial_cmp(&a.entry.gap_pp)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        for c in pool {
            self.enter(eng, &c.series, &c.family, c.weight, &c.market, c.close_unix, c.entry)
                .await;
        }
        match retryable {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }
}

/// A qualifying rung awaiting entry, carried so the whole day's metal wings can be
/// ranked by calibration gap before any order is placed.
struct Candidate {
    series: String,
    family: String,
    weight: f64,
    market: Market,
    close_unix: i64,
    entry: signal::Entry,
}

/// Mon=0..Sun=6 in America/New_York for a unix close time.
fn weekday_et(close_unix: i64) -> u32 {
    use chrono::Datelike;
    chrono::DateTime::from_timestamp(close_unix, 0)
        .map(|dt| {
            dt.with_timezone(&New_York)
                .weekday()
                .num_days_from_monday()
        })
        .unwrap_or(7)
}

/// ET date (YYYY-MM-DD) of a unix close time — the per-day cluster key suffix.
fn close_date_et(close_unix: i64) -> String {
    chrono::DateTime::from_timestamp(close_unix, 0)
        .map(|dt| dt.with_timezone(&New_York).format("%Y-%m-%d").to_string())
        .unwrap_or_default()
}

impl Volbook {
    /// Fetch a metal series' open rungs and return the qualifying wing-sells
    /// (skips are logged here; entry is deferred to the ranked pool in `run`).
    async fn collect_series(
        &self,
        eng: &Engine,
        series: &str,
        family: &str,
        weight: f64,
    ) -> Result<Vec<Candidate>> {
        let now = chrono::Utc::now().timestamp();
        let fam = match self.calib.families.get(family) {
            Some(f) => f,
            None => return Ok(Vec::new()), // universe only holds known families
        };

        // Fail fast in-window like the rest of the codebase.
        let opens = match in_window(eng.kalshi.markets(series, "open")).await {
            Ok(r) => r?,
            Err(_) => {
                logging::info(format!("volbook {series}: open-markets fetch timed out — skip pass"));
                return Ok(Vec::new());
            }
        };

        let mut cands = Vec::new();
        for m in &opens {
            let close_unix = match m.close_unix() {
                Some(c) => c,
                None => continue,
            };
            let ttc = close_unix - now;
            let weekday = weekday_et(close_unix);
            let yes_ask = m.yes_ask_cents_f64();
            let no_ask = m.no_ask_cents_f64();

            match signal::evaluate(
                &self.params,
                &self.calib.weekday_gate,
                weekday,
                ttc,
                yes_ask,
                no_ask,
                fam,
            ) {
                Ok(entry) => cands.push(Candidate {
                    series: series.to_string(),
                    family: family.to_string(),
                    weight,
                    market: m.clone(),
                    close_unix,
                    entry,
                }),
                Err(skip) => self.log_skip(series, &m.ticker, &skip, ttc, close_unix, no_ask),
            }
        }
        Ok(cands)
    }

    /// Log a skip once per (ticker, reason) — only the informative ones (window/
    /// weekday no-signals are the resting state and stay silent to avoid spam).
    fn log_skip(
        &self,
        series: &str,
        ticker: &str,
        skip: &Skip,
        ttc: i64,
        close_unix: i64,
        no_ask: Option<f64>,
    ) {
        // Silence the two "not even a candidate yet" reasons (the common case).
        if matches!(
            skip,
            Skip::NotTradingDay { .. } | Skip::NotEntryWindow { .. }
        ) {
            return;
        }
        let reason = skip.as_str();
        if !self.first_time(format!("{ticker}|{reason}")) {
            return;
        }
        // FIX 11 / I13 (sensors F13): NUMERIC fields, not just the formatted
        // reason string. The Monday prediction is a BUCKET MIX (§5 of
        // verify-volbook-execution: 4.90/2.48/1.45/0.93/1.10/0.31 across the six
        // implied buckets), and the implied YES was reachable only by regex on
        // `out_of_band(impl=23.4)`. There was also no way to confirm a skip was
        // evaluated INSIDE the 17:30-18:30Z window rather than outside it.
        let (implied_pct, ceiling_cents, no_ask_cents) = match skip {
            Skip::OutOfBand { implied_pct } => (Some(*implied_pct), None, None),
            Skip::NoEdge {
                ceiling_cents,
                no_ask_cents,
            } => (None, Some(*ceiling_cents), Some(*no_ask_cents)),
            _ => (None, None, None),
        };
        logging::record_path(
            LOG,
            json!({
                "event": "volbook_skip",
                "series": series,
                "ticker": ticker,
                "reject_reason": reason,
                "implied_pct": implied_pct,
                "ceiling_cents": ceiling_cents,
                "no_ask_cents": no_ask_cents,
                "no_ask": no_ask,
                "ttc": ttc,
                "close_unix": close_unix,
            }),
        );
    }

    #[allow(clippy::too_many_arguments)]
    async fn enter(
        &self,
        eng: &Engine,
        series: &str,
        family: &str,
        weight: f64,
        m: &Market,
        close_unix: i64,
        entry: signal::Entry,
    ) {
        // One entry episode per rung (per process/day).
        if !self.first_time(m.ticker.clone()) {
            return;
        }

        // LIVE GATE (defense in depth): refuse a real order unless explicitly
        // enabled. Paper always shadows.
        let live_enabled = std::env::var(LIVE_ENABLE_ENV).ok().as_deref() == Some("1");
        if eng.mode == Mode::Live && !live_enabled {
            logging::info(format!(
                "volbook {series}: LIVE but {LIVE_ENABLE_ENV}!=1 — shadow only, NOT placing {} \
                 (would buy NO ceiling {}c, ask {}c, EV~{:+.1}c)",
                m.ticker, entry.ceiling_cents, entry.no_ask_cents, entry.ev_at_ask_cents
            ));
            logging::record_path(
                LOG,
                json!({
                    "event": "volbook_shadow",
                    "series": series, "ticker": m.ticker,
                    "ceiling_cents": entry.ceiling_cents, "no_ask_cents": entry.no_ask_cents,
                    "implied_pct": entry.implied_pct, "touch": entry.touch,
                    "gap_pp": entry.gap_pp, "ev_at_ask_cents": entry.ev_at_ask_cents,
                }),
            );
            return;
        }

        // Decision-moment book snapshot (best-effort).
        let book = match in_window(eng.kalshi.orderbook(&m.ticker)).await {
            Ok(Ok(b)) => b,
            _ => json!(null),
        };

        // LIMIT = willingness-to-pay ceiling in LIVE (IOC price-improves to the
        // resting ask); in PAPER use the observed ask so simulated P&L books the
        // price we'd actually have paid, not the ceiling (mirrors streak).
        let limit = if eng.mode == Mode::Live {
            entry.ceiling_cents
        } else {
            entry.no_ask_cents
        };
        // All metal wings on one ET day are one correlated bet (within-day
        // correlated rungs across gold/silver/copper) — one cluster.
        let cluster = format!("volbook-{family}-{}", close_date_et(close_unix));
        let sig = Signal {
            strategy: "volbook".into(),
            ticker: m.ticker.clone(),
            side: Side::No,
            limit_cents: limit,
            cluster,
            sizing: SizingHint::Flat,
        };

        let mut attempts: u32 = 1;
        let outcome = loop {
            let out = eng.execute_attempt(sig.clone(), attempts).await;
            if !matches!(&out, ExecOutcome::Missed { .. }) || attempts >= MAX_ENTRY_ATTEMPTS {
                break out;
            }
            tokio::time::sleep(std::time::Duration::from_millis(RETRY_SPACING_MS)).await;
            attempts += 1;
        };

        let mut rec = json!({
            "event": "volbook_signal",
            "ts_signal": chrono::Utc::now().timestamp(),
            "series": series,
            "family": family,
            "weight": weight,
            "ticker": m.ticker,
            "floor_strike": m.floor_strike,
            "close_unix": close_unix,
            "side": "no",
            "implied_pct": entry.implied_pct,
            "touch": entry.touch,
            "gap_pp": entry.gap_pp,
            "ev_at_ask_cents": entry.ev_at_ask_cents,
            "ceiling_cents": entry.ceiling_cents,
            "no_ask_cents": entry.no_ask_cents,
            "limit_placed": limit,
            "attempts": attempts,
            "filled": false,
            "book": book,
        });

        match &outcome {
            ExecOutcome::Filled { fill, response, .. } => {
                let fee_cents = taker_fee(fill.fill_price_cents, fill.filled) * 100.0;
                rec["filled"] = json!(true);
                rec["partial"] = json!(fill.partial);
                rec["simulated"] = json!(fill.simulated);
                rec["fill_price"] = json!(fill.fill_price_cents);
                rec["filled_count"] = json!(fill.filled);
                rec["fee_cents"] = json!(fee_cents);
                rec["actual_fee_cents"] = json!(fill.actual_fee_cents);
                rec["order_id"] = json!(fill.order_id);
                if !fill.simulated {
                    rec["order"] = response.clone();
                }
                logging::info(format!(
                    "volbook {series}: {}FILLED {}x NO {} @ {}c (implied {:.1}%, touch {:.1}%, \
                     gap {:+.1}pp, EV~{:+.1}c){}",
                    if fill.simulated { "[paper] " } else { "" },
                    fill.filled,
                    m.ticker,
                    fill.fill_price_cents,
                    entry.implied_pct,
                    entry.touch * 100.0,
                    entry.gap_pp,
                    entry.ev_at_ask_cents,
                    if fill.partial { " (partial)" } else { "" }
                ));
                if !fill.simulated {
                    alert::notify(
                        &eng.http,
                        &format!(
                            "volbook FILLED {}x NO {} @ {}c (gap {:+.1}pp)",
                            fill.filled, m.ticker, fill.fill_price_cents, entry.gap_pp
                        ),
                    )
                    .await;
                }
            }
            ExecOutcome::RecoveredFill { fill, .. } => {
                let fee_cents = taker_fee(fill.fill_price_cents, fill.filled) * 100.0;
                rec["filled"] = json!(true);
                rec["recovered"] = json!(true);
                rec["simulated"] = json!(false);
                rec["fill_price"] = json!(fill.fill_price_cents);
                rec["filled_count"] = json!(fill.filled);
                rec["fee_cents"] = json!(fee_cents);
                rec["order_id"] = json!(fill.order_id);
                logging::info(format!(
                    "volbook {series}: RECOVERED FILL {}x NO {} @ {}c",
                    fill.filled, m.ticker, fill.fill_price_cents
                ));
            }
            ExecOutcome::Missed { order, fill } => {
                rec["reject_reason"] = json!("missed_fill");
                rec["canceled_count"] = json!(fill.canceled);
                // FIX 11 (constants F4): RE-ARM. A rung that ends Missed gets
                // 2 IOCs spanning 2 SECONDS of a 3600-second entry window, and
                // the permanent `seen` dedupe then blanked the remaining ~59
                // passes. A market maker pulling the NO ask for five seconds
                // killed the rung for the life of the process — and because the
                // pool is ranked by `gap_pp` descending, the rung most likely to
                // be lost that way is the day's HIGHEST-EDGE one, recorded as
                // `missed_fill`, indistinguishable in the ledger from "no edge
                // existed". Dedupe still holds for FILLED / REJECTED / errored
                // episodes: those either hold a position or were refused by risk,
                // and re-running them would double-book or spam.
                let rearmed = self.rearm(&m.ticker);
                rec["rearm"] = json!(rearmed);
                logging::info(format!(
                    "volbook {series}: MISSED (no fill, canceled {}) {} — {}",
                    order.count,
                    m.ticker,
                    match rearmed {
                        Some(n) => format!("RE-ARMED ({n}/{MAX_REARMS}) for a later pass"),
                        None => format!(
                            "re-arm budget spent ({MAX_REARMS}) — rung closed for the window"
                        ),
                    }
                ));
            }
            ExecOutcome::Rejected(r) => {
                rec["reject_reason"] = json!(format!("risk:{r:?}"));
                logging::info(format!("volbook {series}: rejected ({r:?}) {}", m.ticker));
            }
            ExecOutcome::OrderError(e) => {
                rec["reject_reason"] = json!(format!("order_error:{e}"));
                logging::info(format!("volbook {series}: ORDER FAILED {} ({e})", m.ticker));
                alert::notify(
                    &eng.http,
                    &format!("volbook ORDER FAILED {} ({e})", m.ticker),
                )
                .await;
            }
        }
        logging::record_path(LOG, rec);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn weekday_et_maps_close_to_et_weekday() {
        // 2026-07-27 21:00Z = Monday 17:00 EDT -> weekday 0 (Mon).
        let mon = chrono::DateTime::parse_from_rfc3339("2026-07-27T21:00:00Z")
            .unwrap()
            .timestamp();
        assert_eq!(weekday_et(mon), 0);
        // 2026-07-23 21:00Z = Thursday -> 3.
        let thu = chrono::DateTime::parse_from_rfc3339("2026-07-23T21:00:00Z")
            .unwrap()
            .timestamp();
        assert_eq!(weekday_et(thu), 3);
    }

    #[test]
    fn close_date_et_is_et_calendar_day() {
        // 01:00Z Tuesday is still Monday evening in ET.
        let t = chrono::DateTime::parse_from_rfc3339("2026-07-28T01:00:00Z")
            .unwrap()
            .timestamp();
        assert_eq!(close_date_et(t), "2026-07-27");
    }

    #[test]
    fn a_missed_rung_re_arms_but_a_filled_one_does_not() {
        // FIX 11 (constants F4). MAX_ENTRY_ATTEMPTS=2 x RETRY_SPACING_MS=1000
        // gives a rung 2 IOCs spanning 2 SECONDS of a 3600s entry window scanned
        // every 60s — 60 passes available, 1 usable, because `seen` was never
        // cleared. A maker pulling the NO ask for five seconds killed the rung
        // for the life of the process, and since the pool is ranked by `gap_pp`
        // descending it was the day's HIGHEST-EDGE rung that died that way.
        let v = Volbook::from_path(DEFAULT_CALIB);
        let Ok(v) = v else { return }; // no calib artifact in this checkout
        let t = "KXGOLDD-26JUL27-3400";
        assert!(v.first_time(t.to_string()), "first episode must be allowed");
        assert!(!v.first_time(t.to_string()), "dedupe holds within an episode");

        // Zero-fill Missed: nothing bought, nothing resting (IOC) -> re-arm.
        assert_eq!(v.rearm(t), Some(1));
        assert!(v.first_time(t.to_string()), "a missed rung must retry later");

        // A FILLED / REJECTED episode never calls rearm, so the dedupe stands
        // and a later pass cannot double-book or re-spam a refused signal.
        assert!(!v.first_time(t.to_string()));

        // The budget is BOUNDED: unlimited re-arms would fire 2 IOCs per pass
        // for 60 passes on every missed rung, against an unmeasured rate limit.
        for n in 2..=MAX_REARMS {
            assert_eq!(v.rearm(t), Some(n));
            assert!(v.first_time(t.to_string()));
            assert!(!v.first_time(t.to_string()));
        }
        assert_eq!(v.rearm(t), None, "budget must be spent after MAX_REARMS");
        assert!(!v.first_time(t.to_string()), "a spent rung stays closed");

        // Re-arming one rung must not disturb another, or a skip record's
        // (ticker|reason) dedupe.
        assert!(v.first_time("KXSILVERD-26JUL27-40".to_string()));
        assert!(v.first_time(format!("{t}|out_of_band(impl=23.4)")));
        let _ = v.rearm(t);
        assert!(
            !v.first_time(format!("{t}|out_of_band(impl=23.4)")),
            "re-arming an entry must not clear that ticker's skip dedupe"
        );
    }

    #[test]
    fn arithmetic_of_the_entry_window_the_rearm_unlocks() {
        // 3600s window (entry_ttc 9000..12600) scanned every 60s = 60 passes.
        const ENTRY_WINDOW_SECS: i64 = 12600 - 9000;
        const SCAN_SECS: i64 = 60;
        assert_eq!(ENTRY_WINDOW_SECS / SCAN_SECS, 60);
        // The ladder itself spans 1 retry x 1000ms = ~1s of that.
        assert_eq!(MAX_ENTRY_ATTEMPTS, 2);
        assert!((MAX_ENTRY_ATTEMPTS as u64 - 1) * RETRY_SPACING_MS < 2000);
        // Bounded re-arm: worst case MAX_REARMS+1 episodes x 2 IOCs = 12 order
        // POSTs per rung per day, not 120.
        assert_eq!((MAX_REARMS + 1) * MAX_ENTRY_ATTEMPTS, 12);
    }
}
