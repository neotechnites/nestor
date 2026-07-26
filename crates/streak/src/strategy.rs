//! Live streak sleeve — scan pass over KXBTC15M + KXETH15M (redirect
//! 2026-07-23). Detects a settled 4-streak and buys the reversal side inside the
//! new market's first 60s, one entry episode per market, hold to settlement.
//!
//! EXECUTION POLICY (2026-07-26, `work/verify-streak-execution.md` + note 39
//! rulings; every constant and its derivation lives in [`crate::exec`]):
//!   1. **Rest at 40¢.** As soon as the 4th result is known (official, or
//!      derive-fourth at T0+0..1s) post a full-size RESTING limit BUY at 40¢ on
//!      the reversal side, `good_till_canceled` + `expiration_ts = T0+60`.
//!   2. **Cancel on flip.** If the official result contradicts the derivation
//!      that opened the position, cancel at once. The CANCEL RESPONSE is truth;
//!      the resting-orders list is eventually-consistent and is never polled for
//!      truth (only for the startup orphan sweep).
//!   3. **Backstop at T0+45s.** Unfilled → cancel, then IOC marketable-limit at
//!      the 46¢ ceiling with the existing retry ladder. Ask > ceiling all window
//!      is the correct no-trade.
//!   4. **Late signal** (known after ~T0+40) → taker-only, no maker leg.
//!
//! The maker leg is a STATE MACHINE, not a blocking wait: `supervise_makers`
//! runs at the top of every 1 Hz scan pass so the sleeve keeps polling, keeps
//! logging, and keeps the other coin's window alive while a bid rests.
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
use std::sync::{Arc, Mutex};

use anyhow::Result;
use async_trait::async_trait;
use engine::kalshi::{self, Market};
use engine::risk::taker_fee;
use engine::strategy::{in_window, ExecOutcome, Mode, RestOutcome, IN_WINDOW_TIMEOUT};
use engine::ws::WsBook;
use engine::{alert, logging, Engine, Order, Side, Signal, SizingHint, Strategy};
use serde_json::json;

use crate::derive::{self, Derivation, Verify};
use crate::exec::{self, EntryPath};
use crate::signal::{self, Candidate, SettledWindow, Skip};

const WEEK1_LOG: &str = "data/streak_week1.jsonl";
/// WS-vs-REST ask divergence tape (charter VALIDATION LOGGING). One line per
/// in-entry-window pass, ALWAYS (flag-independent) — one day of this proves the
/// latency win and any correctness gap before STREAK_WS flips.
const WS_DIVERGENCE_LOG: &str = "data/ws_divergence.jsonl";
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

/// How long past a maker leg's `expiration_ts` we keep RETRYING a failed cancel
/// before giving up on it. Kalshi enforces expiry lazily (~2-3min sweep,
/// demo-measured), so until then a bid we failed to cancel may still be live and
/// we must not assume it is gone. Past this we release the cap reservation and
/// alert loudly — the exchange sweep is the only remaining backstop.
const CANCEL_RETRY_GRACE_SECS: i64 = 240;

/// Clock slack applied to `expiration_ts` when deciding what a cancel-404 MEANS
/// (FIX 6 — reality F3, sensors P2c). Before its `expiration_ts` a bid can only
/// leave the book by FILLING, so a 404 proves a fill. AT OR AFTER it, the
/// exchange's lazy expiry sweep is a second, equally likely producer of 404 —
/// and the cancel-retry loop is GUARANTEED to reach that state (it retries every
/// pass until `expiration_ts + CANCEL_RETRY_GRACE_SECS`, while the sweep lands
/// somewhere in the first ~2-3min of that window). Reading those 404s as "it
/// filled" withholds the backstop forever, pins the cap reservation for up to
/// 300s, and fires a false CRITICAL page.
///
/// 5s: our clock is compared against the exchange's `Date` header every reconcile
/// pass and alerts past 30s of skew, so normal NTP drift is far inside this;
/// 5s of extra "404 ⇒ filled" exposure past expiry is the cost of not
/// mis-classifying a fill that landed in the last instant before expiry.
const EXPIRY_404_SLACK_SECS: i64 = 5;

/// What a cancel-404 on a maker leg can mean at time `now` (FIX 6).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Gone404 {
    /// Before `expiration_ts` (+slack): the only way off the book is a FILL.
    /// Demo-proven; withhold the backstop and keep polling fills.
    Filled,
    /// At/after expiry: the exchange's lazy sweep is an equally good explanation.
    /// Close the episode as `expired_unfilled` — no CRITICAL alert, no permanent
    /// backstop withhold, no cap held for 5 more minutes.
    MaybeExpired,
}

/// Pure classifier for the cancel-404 branch — unit-tested.
fn classify_cancel_404(now: i64, expiration_ts: i64) -> Gone404 {
    if now > expiration_ts + EXPIRY_404_SLACK_SECS {
        Gone404::MaybeExpired
    } else {
        Gone404::Filled
    }
}

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

/// Everything the participation record needs about WHY we entered, carried from
/// the detection moment through whichever leg finally resolves.
#[derive(Clone)]
struct EntryMeta {
    ts_signal: i64,
    streak_dir: &'static str,
    side: Side,
    /// Reversal-side ask observed at the decision moment (diagnostic only).
    ask_at_signal: Option<f64>,
    /// Some((avg, margin_bp)) when a DERIVED 4th result opened this entry.
    derived: Option<(f64, f64)>,
    /// The derived prediction + the window it belongs to, for cancel-on-flip.
    predicted: Option<String>,
    jc_close: i64,
    /// Order-book snapshot at the decision moment.
    book: serde_json::Value,
}

/// A resting 40¢ maker leg under supervision. One per ticker at most; dropped
/// the moment it fills, flips, or hands off to the taker backstop.
struct MakerLeg {
    series: String,
    meta: EntryMeta,
    /// The risk-sized order actually posted (count + the 40¢ limit).
    order: Order,
    /// Exchange order_id — what we cancel. In paper it is a `paper-` sentinel.
    order_id: String,
    t0: i64,
    close_unix: i64,
    backstop_at: i64,
    expiration_ts: i64,
    placed_ms: i64,
    /// Key under which this leg's stake is reserved against the risk caps.
    reserve_key: String,
    /// Latest observed reversal ask, refreshed each pass — the PAPER fill model's
    /// only input (live mode asks the exchange).
    last_ask: Option<f64>,
    /// Effective taker ceiling captured at placement, so the dial can't shift
    /// under a live episode.
    ceiling: i64,
    paper: bool,
    /// Set when a cancel attempt failed: the bid may still be live, so the
    /// backstop is WITHHELD (a second fill on this ticker would be unbookable)
    /// and the cancel is retried on later passes.
    cancel_failed: bool,
    /// Cancel returned 404: the bid is off the book (i.e. it filled) but the
    /// fill has not surfaced in /portfolio/fills yet. NO backstop, ever.
    off_book: bool,
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
    /// Shared websocket book, when the maintainer task is running. Reads are
    /// non-blocking and a dead ws NEVER halts an entry — REST is the floor.
    /// Present regardless of the STREAK_WS flag (divergence logging is always on).
    ws: Option<Arc<WsBook>>,
    /// Resting maker legs under supervision, keyed by ticker.
    maker: Mutex<HashMap<String, MakerLeg>>,
    /// One-shot startup orphan sweep guard.
    swept: Mutex<bool>,
}

impl Streak {
    pub fn new() -> Self {
        Streak {
            seen: Mutex::new(HashSet::new()),
            settled_cache: Mutex::new(HashMap::new()),
            spot_buf: Mutex::new(HashMap::new()),
            derive_pending: Mutex::new(HashMap::new()),
            skip_alarm: Mutex::new(HashMap::new()),
            ws: None,
            maker: Mutex::new(HashMap::new()),
            swept: Mutex::new(false),
        }
    }

    /// Attach the shared websocket book (built + spawned by the binary). The book
    /// feeds always-on divergence logging and, behind STREAK_WS=1, the entry ask.
    pub fn with_ws(mut self, ws: Arc<WsBook>) -> Self {
        self.ws = Some(ws);
        self
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

    /// WS integration for one candidate market. No-op when no ws book is attached.
    /// (1) marks the ticker wanted so the maintainer subscribes it; (2) inside the
    /// entry window, appends a ws-vs-REST divergence line ALWAYS (flag-independent);
    /// (3) behind STREAK_WS=1, overrides the candidate asks with the ws book ONLY
    /// when it is synced and fresh (<1s). Any miss leaves the REST asks untouched.
    fn apply_ws(&self, cur: &Market, cand: &mut Candidate, now: i64) {
        let Some(ws) = &self.ws else { return };
        ws.want(&cur.ticker);
        let q = ws.quote(&cur.ticker);

        // Divergence tape, gated to the entry window (first 60s) to bound volume.
        let ttc = cand.close_unix - now;
        if (signal::MIN_TTC_SECS..=signal::WINDOW_SECS).contains(&ttc) {
            logging::record_path(
                WS_DIVERGENCE_LOG,
                json!({
                    "ts_ms": chrono::Utc::now().timestamp_millis(),
                    "ticker": cur.ticker,
                    "ws_yes_ask": q.as_ref().and_then(|x| x.yes_ask),
                    "ws_no_ask": q.as_ref().and_then(|x| x.no_ask),
                    "rest_yes_ask": cand.yes_ask,
                    "rest_no_ask": cand.no_ask,
                    "ws_age_ms": q.as_ref().map(|x| x.age.as_millis() as u64),
                    "ws_synced": q.as_ref().map(|x| x.synced),
                }),
            );
        }

        // Flag-gated override: fresh, synced ws book drives the entry ask.
        if std::env::var("STREAK_WS").as_deref() != Ok("1") {
            return;
        }
        if let Some(q) = q {
            let fresh = q.synced && q.age < std::time::Duration::from_secs(1);
            if fresh {
                if let Some(a) = q.yes_ask {
                    cand.yes_ask = Some(a as f64);
                }
                if let Some(a) = q.no_ask {
                    cand.no_ask = Some(a as f64);
                }
            }
        }
    }

    /// STARTUP ORPHAN SWEEP — once per process, live only. A restart mid-window
    /// forgets its resting bid but the exchange does not; the forgotten order
    /// would then fill into a `seen`-cleared strategy that re-posts under the
    /// SAME deterministic coid (409) and supervises nothing. Cancel anything
    /// resting on OUR series only — the account may legitimately hold house-sleeve
    /// quotes, and a blanket sweep would kill them.
    async fn sweep_orphan_rests(&self, eng: &Engine) {
        {
            let mut swept = self.swept.lock().unwrap_or_else(|e| e.into_inner());
            if *swept {
                return;
            }
            *swept = true;
        }
        if eng.mode != Mode::Live {
            return;
        }
        let body = match eng.kalshi.resting_orders(None).await {
            Ok(b) => b,
            Err(e) => {
                eprintln!("[streak] startup resting_orders read failed: {e}");
                return;
            }
        };
        let ours: Vec<_> = kalshi::parse_resting_orders(&body)
            .into_iter()
            .filter(|o| SERIES.iter().any(|s| o.ticker.starts_with(s)))
            .collect();
        if ours.is_empty() {
            return;
        }
        logging::info(format!(
            "streak: startup sweep — cancelling {} orphaned resting order(s)",
            ours.len()
        ));
        for o in &ours {
            match eng.kalshi.cancel_order(&o.order_id).await {
                Ok(_) => logging::info(format!("streak: swept orphan {} on {}", o.order_id, o.ticker)),
                Err(e) => eprintln!("[streak] orphan sweep cancel {} failed: {e}", o.order_id),
            }
        }
    }

    async fn scan_series(&self, eng: &Engine, series: &str) -> Result<()> {
        self.sweep_orphan_rests(eng).await;
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
        // FIX 5c (moneypath F4): this fetch must NOT be able to skip a backstop
        // deadline. It used to `?`-return ABOVE `supervise_makers`, which
        // directly contradicts the invariant the code claims for itself below —
        // the guard had been placed against the wrong fetch. `recent_closed` is
        // not deadline-bounded, is uncached and re-run ~1/s during the
        // settlement-lag phase, and a 429 storm or a 30s hang there leaves the
        // 40¢ bid uncancelled past T0+45 with the exchange's expiry enforced
        // lazily — a fill at T0+150 on a signal whose 60s edge is long gone.
        //
        // So: on error, still supervise (with NO settled windows — the flip
        // check simply cannot fire, and it only ever CANCELS, so an absent flip
        // is the safe direction), THEN propagate so the driving loop still backs
        // off on a retryable status.
        let raw = match self.settled_for(eng, series, window_id, force).await {
            Ok(r) => r,
            Err(e) => {
                logging::info(format!(
                    "streak {series}: settled-results fetch failed ({e}) — running maker \
                     supervision anyway, then backing off"
                ));
                self.supervise_makers(eng, series, &[], now).await;
                return Err(e);
            }
        };
        let settled = settled_windows(&raw);

        // VERIFY (item 4): any pending derivation whose official result has now
        // landed gets compared here; a used-derivation disagreement disables
        // derivation loudly.
        self.verify_pending(eng, series, &settled).await;

        // MAKER SUPERVISION runs BEFORE the open-markets fetch on purpose: that
        // fetch can time out, and a timed-out pass must never be able to skip a
        // backstop deadline or a cancel-on-flip. Everything it needs is stored on
        // the leg (the paper fill model's `last_ask` is therefore one ~1s pass
        // stale — paper-only, and documented).
        self.supervise_makers(eng, series, &settled, now).await;

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
        let mut cand = Candidate {
            open_unix: cur.open_unix(),
            close_unix: cur.close_unix().unwrap_or(now + signal::WINDOW_SECS),
            yes_ask: cur.yes_ask_cents_f64(),
            no_ask: cur.no_ask_cents_f64(),
        };

        // DATA CAPTURE 1 — observation log: one compact line per poll, always.
        // Records the REST asks (the ws override below happens after this).
        logging::record_path(
            &obs_path(now_dt),
            json!({
                "ts_ms": now_dt.timestamp_millis(),
                "ticker": cur.ticker,
                "yes_ask": cand.yes_ask,
                "no_ask": cand.no_ask,
            }),
        );

        // WEBSOCKET: register interest, log ws-vs-REST divergence (ALWAYS, in the
        // entry window), and — only behind STREAK_WS=1 with a fresh synced book —
        // let the ws ask drive the entry. A dead/stale ws silently falls back to
        // the REST asks already in `cand` (never blocks an entry).
        self.apply_ws(cur, &mut cand, now);

        // Refresh the supervised leg's view of the reversal ask (paper fill model
        // + the participation record's price context).
        self.refresh_leg_ask(&cur.ticker, &cand);

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

    /// Refresh a supervised leg's view of the reversal ask. Live ignores it (the
    /// exchange decides fills); paper's fill model is driven entirely by it.
    fn refresh_leg_ask(&self, ticker: &str, cand: &Candidate) {
        let mut legs = self.maker.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(leg) = legs.get_mut(ticker) {
            leg.last_ask = match leg.meta.side {
                Side::Yes => cand.yes_ask,
                Side::No => cand.no_ask,
            };
        }
    }

    /// The participation-record skeleton every entry path shares.
    fn base_record(&self, series: &str, ticker: &str, meta: &EntryMeta, path: EntryPath) -> serde_json::Value {
        let mut rec = json!({
            "event": "streak_signal",
            "ts_signal": meta.ts_signal,
            "series": series,
            "ticker": ticker,
            "streak_dir": meta.streak_dir,
            "side_bought": meta.side.as_str(),
            "ask_at_signal": meta.ask_at_signal,
            "entry_path": path.as_str(),
            "maker_price": exec::MAKER_PRICE_CENTS,
            "filled": false,
            "book": meta.book,
        });
        if let Some((avg, margin_bp)) = meta.derived {
            rec["derived_fourth"] = json!(true);
            rec["derived_avg"] = json!(avg);
            rec["derived_margin_bp"] = json!(margin_bp);
        }
        rec
    }

    /// Entry dispatcher. One episode per market, ever (dedupes across restarts).
    /// Rest at 40¢ whenever there is runway before the backstop; otherwise the
    /// signal arrived too late to rest anything and we go taker-only.
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
        if !self.first_time(cur.ticker.clone()) {
            return Ok(());
        }

        // DATA CAPTURE 2 — decision snapshot at the entry moment (fetched before
        // any order so the book reflects what we saw when deciding).
        let book = match in_window(eng.kalshi.orderbook(&cur.ticker)).await {
            Ok(Ok(b)) => b,
            _ => json!(null), // timeout or error: book snapshot is best-effort
        };

        let side = if entry.buy_yes { Side::Yes } else { Side::No };
        let t0 = cand.close_unix - signal::WINDOW_SECS;
        // The JUST-CLOSED window is the one whose close == this market's open
        // (T0) — the same key `derive_prev` files its pending derivation under.
        // Get this wrong and cancel-on-flip silently never fires.
        let jc_close = cur.open_unix().unwrap_or(t0);
        let meta = EntryMeta {
            ts_signal: now,
            streak_dir: entry.streak_dir,
            side,
            ask_at_signal: entry.ask,
            derived,
            predicted: None, // set by place_maker when a derivation drove this
            jc_close,
            book,
        };
        let ceiling = exec::taker_ceiling();

        if exec::maker_eligible(now, t0) {
            self.place_maker(eng, series, cur, cand, meta, t0, ceiling, now)
                .await
        } else {
            // LATE SIGNAL: no runway to rest. Taker-only at the ceiling.
            logging::info(format!(
                "streak {series}: {} late signal (T0+{}s) — taker-only at {ceiling}c",
                cur.ticker,
                now - t0
            ));
            self.taker_leg(
                eng,
                series,
                &cur.ticker,
                &meta,
                cand.close_unix,
                ceiling,
                entry.ask,
                EntryPath::TakerLate,
                json!(null),
            )
            .await
        }
    }

    /// Post the resting 40¢ maker leg and hand it to the supervisor.
    #[allow(clippy::too_many_arguments)]
    async fn place_maker(
        &self,
        eng: &Engine,
        series: &str,
        cur: &Market,
        cand: &Candidate,
        mut meta: EntryMeta,
        t0: i64,
        ceiling: i64,
        now: i64,
    ) -> Result<()> {
        let ticker = cur.ticker.clone();
        // The derived 4th result (if any) is what a later official result may
        // contradict — carry it so cancel-on-flip has something to compare.
        if meta.derived.is_some() {
            meta.predicted = self
                .derive_pending
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .get(&format!("{series}|{}", meta.jc_close))
                .map(|p| p.predicted.clone());
        }

        let expiration_ts = exec::maker_expiration(t0);
        let reserve_key = format!("streak-maker|{ticker}");
        // Distinct from the taker coid namespace (`streak-{ticker}[-r{n}]`) so a
        // maker leg and a backstop IOC on the same market never collide into a
        // duplicate-coid 409.
        let coid = format!("streak-{ticker}-m{}", exec::MAKER_PRICE_CENTS);
        let sig = Signal {
            strategy: "streak".into(),
            ticker: ticker.clone(),
            side: meta.side,
            limit_cents: exec::MAKER_PRICE_CENTS,
            // Window close shared across coins: simultaneous BTC+ETH = ONE bet.
            cluster: format!("streak-{}", cand.close_unix),
            sizing: SizingHint::Flat,
        };

        match eng
            .place_resting(sig, &reserve_key, &coid, expiration_ts)
            .await
        {
            RestOutcome::Resting {
                order,
                order_id,
                response,
            } => {
                let leg = MakerLeg {
                    series: series.to_string(),
                    meta,
                    order,
                    order_id: order_id.clone(),
                    t0,
                    close_unix: cand.close_unix,
                    backstop_at: exec::backstop_at(t0),
                    expiration_ts,
                    placed_ms: chrono::Utc::now().timestamp_millis(),
                    reserve_key,
                    last_ask: None,
                    ceiling,
                    paper: eng.mode != Mode::Live,
                    cancel_failed: false,
                    off_book: false,
                };
                logging::info(format!(
                    "streak {series}: {}RESTING {}x {} {ticker} @ {}c (T0+{}s, backstop T0+{}s, exp {expiration_ts}) id={order_id}",
                    if leg.paper { "[paper] " } else { "" },
                    leg.order.count,
                    leg.meta.side.as_str(),
                    exec::MAKER_PRICE_CENTS,
                    now - t0,
                    exec::BACKSTOP_AT_SECS,
                ));
                logging::record_path(
                    WEEK1_LOG,
                    json!({
                        "event": "streak_maker_rest",
                        "series": series,
                        "ticker": ticker,
                        "side": leg.meta.side.as_str(),
                        "price": exec::MAKER_PRICE_CENTS,
                        "count": leg.order.count,
                        "order_id": order_id,
                        "expiration_ts": expiration_ts,
                        "backstop_at": leg.backstop_at,
                        "ceiling": ceiling,
                        "paper": leg.paper,
                        "order": response,
                    }),
                );
                self.maker
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .insert(ticker, leg);
                Ok(())
            }
            // The 40¢ bid crossed a cheaper ask at post — a TAKER fill at a price
            // better than we asked for. Nothing to supervise.
            RestOutcome::ImmediateFill {
                order,
                fill,
                response,
            } => {
                let mut rec = self.base_record(series, &ticker, &meta, EntryPath::MakerRest);
                rec["crossed_at_post"] = json!(true);
                rec["rest_secs"] = json!(0);
                self.record_fill(eng, series, &ticker, rec, &order, &fill, Some(response))
                    .await;
                Ok(())
            }
            RestOutcome::Rejected(r) => {
                let mut rec = self.base_record(series, &ticker, &meta, EntryPath::MakerRest);
                rec["reject_reason"] = json!(format!("risk:{r:?}"));
                logging::info(format!("streak {series}: rejected ({r:?}) {ticker}"));
                logging::record_path(WEEK1_LOG, rec);
                Ok(())
            }
            // The exchange told us it created nothing → taking is safe, and the
            // signal is still live. Fall through to the taker leg.
            RestOutcome::RestError {
                msg,
                may_be_resting: false,
            } => {
                logging::info(format!(
                    "streak {series}: maker post FAILED for {ticker} ({msg}) — nothing resting, \
                     falling back to taker at {ceiling}c"
                ));
                self.taker_leg(
                    eng,
                    series,
                    &ticker,
                    &meta,
                    cand.close_unix,
                    ceiling,
                    meta.ask_at_signal,
                    EntryPath::TakerLate,
                    json!({"maker_error": msg}),
                )
                .await
            }
            // AMBIGUOUS: the POST may have landed and we have no order_id to
            // cancel or supervise. Sending an IOC now risks a second fill on a
            // ticker whose ledger can hold only one position, so we STAND DOWN
            // and let `expiration_ts` (+ reconcile's orphan adoption) resolve it.
            RestOutcome::RestError {
                msg,
                may_be_resting: true,
            } => {
                let mut rec = self.base_record(series, &ticker, &meta, EntryPath::MakerRest);
                rec["reject_reason"] = json!("maker_place_ambiguous");
                rec["maker_error"] = json!(msg.clone());
                logging::record_path(WEEK1_LOG, rec);
                logging::info(format!(
                    "streak {series}: CRITICAL maker post ambiguous for {ticker} ({msg}) — an \
                     order MAY be resting with no id; standing down (exp {expiration_ts})"
                ));
                alert::notify(
                    &eng.http,
                    &format!(
                        "streak maker post AMBIGUOUS on {ticker} ({msg}) — possible unsupervised \
                         resting bid, expires {expiration_ts}; no backstop sent"
                    ),
                )
                .await;
                Ok(())
            }
        }
    }

    /// Advance every supervised maker leg belonging to `series`.
    async fn supervise_makers(
        &self,
        eng: &Engine,
        series: &str,
        settled: &[SettledWindow],
        now: i64,
    ) {
        let tickers: Vec<String> = {
            let legs = self.maker.lock().unwrap_or_else(|e| e.into_inner());
            legs.values()
                .filter(|l| l.series == series)
                .map(|l| l.ticker())
                .collect()
        };
        for t in tickers {
            self.supervise_one(eng, series, &t, settled, now).await;
        }
    }

    /// One supervision step: flip → fill → deadline. Everything it needs lives on
    /// the leg, so a timed-out market fetch earlier in the pass cannot starve it.
    async fn supervise_one(
        &self,
        eng: &Engine,
        series: &str,
        ticker: &str,
        settled: &[SettledWindow],
        now: i64,
    ) {
        let leg = {
            let legs = self.maker.lock().unwrap_or_else(|e| e.into_inner());
            match legs.get(ticker) {
                Some(l) => l.snapshot(),
                None => return,
            }
        };

        // (1) CANCEL ON FLIP — the official result for the window our derivation
        // synthesized has landed and contradicts it. The signal is void; the bid
        // must come off the book before it can fill on a streak that never was.
        if let Some(pred) = leg.meta.predicted.as_deref() {
            if let Some(off) = settled.iter().find(|w| w.close_unix == leg.meta.jc_close) {
                if off.result != pred {
                    logging::info(format!(
                        "streak {series}: FLIP on {ticker} — derived {pred} but official {} — \
                         cancelling the resting bid",
                        off.result
                    ));
                    self.abandon_leg(eng, &leg, "flipped", now).await;
                    return;
                }
            }
        }

        // (2) FILL CHECK. /portfolio/fills by order_id is the truth; the resting
        // list is eventually-consistent and is never consulted here.
        if let Some(f) = self.poll_leg_fill(eng, &leg, now).await {
            self.settle_maker_fill(eng, &leg, f, now).await;
            return;
        }

        // (3) Still resting and the deadline has not arrived — leave it alone.
        //     A leg with a failed cancel, or one already off the book, keeps
        //     being worked every pass regardless of the clock.
        if now < leg.backstop_at && !leg.cancel_failed && !leg.off_book {
            return;
        }

        // (3b) An off-book leg (cancel 404) is waiting on a lagging fills API.
        //      It must never reach the cancel/backstop branch again.
        if leg.off_book {
            self.mark_gone(eng, &leg, now, Gone404::Filled).await;
            return;
        }

        // (4) DEADLINE: cancel FIRST. A live bid plus an IOC on the same ticker
        //     could produce two fills, and the risk ledger books one position per
        //     ticker — the second would be real money it cannot see.
        //
        //     FIX 5a (constants F3): the cancel is DEADLINE-BOUNDED. Unwrapped it
        //     inherits the shared client's 30s budget, and a stalled cancel puts
        //     the backstop IOC outside the 60s window every one of its measured
        //     numbers was fitted on. A timeout is treated exactly like any other
        //     cancel failure: the bid may still be live, so no backstop, retry.
        let cancel = if leg.paper {
            Ok(Ok(json!({"paper": true})))
        } else {
            in_window(eng.kalshi.cancel_order(&leg.order_id)).await
        };
        let cancel = match cancel {
            Ok(r) => r,
            Err(_elapsed) => {
                let msg = format!(
                    "cancel exceeded the {}s in-window deadline",
                    IN_WINDOW_TIMEOUT.as_secs()
                );
                self.mark_cancel_failed(eng, &leg, &msg, now).await;
                return;
            }
        };
        let cancel_resp = match cancel {
            Ok(v) => v,
            // DEMO-PROVEN 2026-07-26: cancelling an order that is no longer
            // resting returns **HTTP 404 `not_found`**, not a 200 with counts.
            // Before `expiration_ts` the only way a bid leaves the book is by
            // FILLING, so a 404 here means "it filled" — and firing the backstop
            // on top of it would open a second position on a ticker whose ledger
            // holds one. NEVER backstop on a 404: keep polling fills (they can
            // lag the matching engine by seconds) until the fill shows up.
            Err(e) if engine::net::http_status(&e) == Some(404) => {
                // FIX 6: 404 ⇒ filled ONLY before expiry. Past it, the lazy
                // expiry sweep produces the identical 404.
                let meaning = classify_cancel_404(now, leg.expiration_ts);
                logging::info(format!(
                    "streak {series}: cancel 404 on {ticker} at T0+{}s (exp {}) — {}",
                    now - leg.t0,
                    leg.expiration_ts,
                    match meaning {
                        Gone404::Filled =>
                            "before expiry, so the bid left the book by FILLING; withholding the \
                             backstop and polling fills",
                        Gone404::MaybeExpired =>
                            "PAST expiry, so this is as likely the exchange's lazy sweep as a \
                             fill; closing the episode as expired_unfilled",
                    }
                ));
                self.mark_gone(eng, &leg, now, meaning).await;
                return;
            }
            Err(e) => {
                self.mark_cancel_failed(eng, &leg, &e.to_string(), now).await;
                return;
            }
        };

        // (4b) THE CANCEL RESPONSE IS TRUTH. Demo-proven: it carries
        //      `reduced_by` = the quantity still resting when we pulled it.
        //      `/portfolio/fills` lags the matching engine by seconds, so this —
        //      not a fills poll — is what decides whether a backstop is safe.
        match kalshi::parse_cancel_reduced_by(&cancel_resp) {
            // The whole order came off the book: nothing filled. Backstop is safe.
            Some(r) if r >= leg.order.count => {}
            // Something filled before the cancel landed. A backstop would try to
            // open a SECOND position on this ticker, which the ledger cannot
            // book. Withhold it and wait for the fill to surface.
            Some(r) => {
                logging::info(format!(
                    "streak {series}: cancel on {ticker} reduced_by {r} of {} — a fill landed \
                     first; backstop WITHHELD",
                    leg.order.count
                ));
                // A partial cancel proves a FILL, whatever the clock says — the
                // exchange handed back a quantity, it did not 404.
                self.mark_gone(eng, &leg, now, Gone404::Filled).await;
                return;
            }
            // Field absent/unparseable — fall back to a fills poll.
            None => {
                if let Some(f) = self.poll_leg_fill(eng, &leg, now).await {
                    self.settle_maker_fill(eng, &leg, f, now).await;
                    return;
                }
            }
        }

        // (5) Truly unfilled → the taker backstop at the ceiling.
        self.drop_leg(eng, ticker, &leg.reserve_key);
        logging::info(format!(
            "streak {series}: {} maker unfilled at T0+{}s — cancelled, IOC backstop at {}c",
            ticker,
            now - leg.t0,
            leg.ceiling
        ));
        let _ = self
            .taker_leg(
                eng,
                series,
                ticker,
                &leg.meta,
                leg.close_unix,
                leg.ceiling,
                leg.last_ask,
                EntryPath::TakerBackstop,
                json!({
                    "maker_order_id": leg.order_id,
                    "maker_rest_secs": now - leg.t0,
                    "maker_cancel": cancel_resp,
                }),
            )
            .await;
    }

    /// The exchange says the bid is off the book (cancel 404) but /portfolio/fills
    /// has not shown the fill yet. Hold the leg — and its cap reservation — and
    /// keep polling; the backstop stays withheld forever for this episode, since
    /// a second position on this ticker could not be booked. If the fill never
    /// materialises by the end of the window, close the episode loudly and let
    /// reconcile's orphan adoption find any real position.
    async fn mark_gone(&self, eng: &Engine, leg: &MakerLeg, now: i64, meaning: Gone404) {
        // FIX 6 (reality F3 / sensors P2c): a 404 that arrives PAST the order's
        // expiration is at least as likely the exchange's lazy sweep as a fill.
        // Treating it as a fill costs the backstop forever, pins $4 of cap for
        // 5 more minutes, and fires a CRITICAL page for a position that does not
        // exist — the alert-fatigue machine. Release it as expired_unfilled.
        if meaning == Gone404::MaybeExpired {
            self.release_expired_unfilled(eng, leg, now);
            return;
        }
        if now <= leg.expiration_ts + CANCEL_RETRY_GRACE_SECS {
            // FIX 1 (reality F1): the bid is OFF the book, so its collateral has
            // certainly moved. The reservation stays (the cap must not be
            // re-spent while the fill surfaces) but it must stop widening the
            // divergence breaker, or a genuine miscount hides for 5 minutes.
            eng.mark_reservation_off_book(&leg.reserve_key);
            let mut legs = self.maker.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(l) = legs.get_mut(&leg.ticker()) {
                l.off_book = true; // worked every pass, but NEVER sends an IOC
            }
            return;
        }
        self.drop_leg(eng, &leg.ticker(), &leg.reserve_key);
        let mut rec = self.base_record(&leg.series, &leg.ticker(), &leg.meta, EntryPath::MakerRest);
        rec["reject_reason"] = json!("maker_off_book_no_fill_seen");
        rec["maker_order_id"] = json!(leg.order_id);
        logging::record_path(WEEK1_LOG, rec);
        alert::notify(
            &eng.http,
            &format!(
                "streak: resting bid {} on {} left the book (cancel 404) but no fill ever                  appeared in /portfolio/fills — check positions",
                leg.order_id,
                leg.ticker()
            ),
        )
        .await;
    }

    /// FIX 6 terminal: the bid left the book at or after its `expiration_ts`, so
    /// the exchange's lazy expiry sweep explains it as well as a fill does. Close
    /// the episode, free the cap immediately, and record it — NO alert (nothing
    /// anomalous happened) and NO permanent backstop withhold (there is no
    /// backstop to send: `expiration_ts` is the end of the entry window).
    ///
    /// If a fill DID land, reconcile's orphan adoption books it within 60s from
    /// `/portfolio/positions` — exchange truth, not an inference from a 404.
    fn release_expired_unfilled(&self, eng: &Engine, leg: &MakerLeg, now: i64) {
        self.drop_leg(eng, &leg.ticker(), &leg.reserve_key);
        let mut rec =
            self.base_record(&leg.series, &leg.ticker(), &leg.meta, EntryPath::MakerRest);
        rec["reject_reason"] = json!("expired_unfilled");
        rec["maker_order_id"] = json!(leg.order_id);
        rec["expiration_ts"] = json!(leg.expiration_ts);
        rec["secs_past_expiry"] = json!(now - leg.expiration_ts);
        rec["rest_secs"] = json!(now - leg.t0);
        logging::record_path(WEEK1_LOG, rec);
        logging::info(format!(
            "streak {}: {} resting bid expired unfilled ({}s past expiry) — episode closed, \
             cap released, no alert (reconcile adopts any real position)",
            leg.series,
            leg.ticker(),
            now - leg.expiration_ts
        ));
    }

    /// A cancel round-trip failed: the bid may still be live, so withhold the
    /// backstop and retry next pass. Past the lazy-expiry grace we give up, free
    /// the cap reservation, and shout.
    async fn mark_cancel_failed(&self, eng: &Engine, leg: &MakerLeg, err: &str, now: i64) {
        if now > leg.expiration_ts + CANCEL_RETRY_GRACE_SECS {
            self.drop_leg(eng, &leg.ticker(), &leg.reserve_key);
            logging::info(format!(
                "streak: GIVING UP cancelling {} ({}) after {}s past expiry — {err}",
                leg.order_id,
                leg.ticker(),
                now - leg.expiration_ts
            ));
            alert::notify(
                &eng.http,
                &format!(
                    "streak: could not cancel resting bid {} on {} ({err}) — relying on the \
                     exchange expiry sweep; check /portfolio/orders",
                    leg.order_id,
                    leg.ticker()
                ),
            )
            .await;
            return;
        }
        {
            let mut legs = self.maker.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(l) = legs.get_mut(&leg.ticker()) {
                l.cancel_failed = true;
            }
        }
        eprintln!(
            "[streak] cancel {} ({}) failed: {err} — backstop WITHHELD, retrying next pass",
            leg.order_id,
            leg.ticker()
        );
    }

    /// Cancel a leg and close its episode with no position (cancel-on-flip).
    async fn abandon_leg(&self, eng: &Engine, leg: &MakerLeg, reason: &str, now: i64) {
        if !leg.paper {
            if let Err(e) = eng.kalshi.cancel_order(&leg.order_id).await {
                // 404 = already off the book, i.e. it FILLED before the flip
                // reached us. We now hold a position on a signal that turned out
                // not to exist. It STAYS (risk-managed) — same doctrine as a
                // used-derivation disagreement. Book it and stop cancelling, or
                // the flip check would re-enter this every pass forever.
                if engine::net::http_status(&e) == Some(404) {
                    // FIX 6: same 404 ambiguity as the deadline cancel.
                    let meaning = classify_cancel_404(now, leg.expiration_ts);
                    logging::info(format!(
                        "streak: flip-cancel 404 on {} — {}",
                        leg.ticker(),
                        match meaning {
                            Gone404::Filled =>
                                "before expiry, so the bid had already filled; booking the \
                                 position (it stays, risk-managed)",
                            Gone404::MaybeExpired =>
                                "past expiry, so it may simply have been swept; closing as \
                                 expired_unfilled",
                        }
                    ));
                    self.mark_gone(eng, leg, now, meaning).await;
                    return;
                }
                eprintln!("[streak] flip-cancel {} failed: {e}", leg.order_id);
                // Transient failure: retry next pass, bounded by the same grace.
                self.mark_cancel_failed(eng, leg, &e.to_string(), now).await;
                return;
            }
        }
        self.drop_leg(eng, &leg.ticker(), &leg.reserve_key);
        let mut rec = self.base_record(&leg.series, &leg.ticker(), &leg.meta, EntryPath::MakerRest);
        rec["reject_reason"] = json!(reason);
        rec["maker_order_id"] = json!(leg.order_id);
        logging::record_path(WEEK1_LOG, rec);
    }

    /// Forget a leg and release its cap reservation. Both must happen together.
    fn drop_leg(&self, eng: &Engine, ticker: &str, reserve_key: &str) {
        self.maker
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(ticker);
        eng.release_reservation(reserve_key);
    }

    /// Poll for a fill on a resting leg. LIVE: `/portfolio/fills` filtered by
    /// order_id (the exchange's truth). PAPER: the ledger's own model — a bid at
    /// L fills at L iff the reversal ask trades at or below L.
    async fn poll_leg_fill(&self, eng: &Engine, leg: &MakerLeg, now: i64) -> Option<LegFill> {
        if leg.paper {
            if !exec::paper_maker_fills(leg.last_ask, leg.order.limit_cents) {
                return None;
            }
            return Some(LegFill {
                count: leg.order.count,
                price_cents: leg.order.limit_cents,
                ts_ms: Some(now * 1000),
                // Paper models the MAKER fee the demo measured: zero.
                actual_fee_cents: Some(0.0),
                all_maker: Some(true),
                raw: json!(null),
                simulated: true,
            });
        }
        let body = match in_window(eng.kalshi.fills(&leg.ticker())).await {
            Ok(Ok(b)) => b,
            _ => return None, // a missed poll is retried next pass
        };
        let fills = kalshi::parse_fills(
            &body,
            Some(&leg.order_id),
            leg.meta.side.as_str(),
            leg.placed_ms,
        );
        let (total, avg, ts) = kalshi::fills_summary(&fills);
        let filled = total.min(leg.order.count);
        if filled <= 0 {
            return None;
        }
        Some(LegFill {
            count: filled,
            price_cents: avg.unwrap_or(leg.order.limit_cents),
            ts_ms: ts,
            actual_fee_cents: kalshi::fills_fee_cents(&fills),
            all_maker: kalshi::fills_all_maker(&fills),
            raw: body,
            simulated: false,
        })
    }

    /// Book a maker fill, close the episode, write the record.
    ///
    /// PARTIAL FILLS DO NOT GET TOPPED UP. `RiskManager::on_fill_actual` holds a
    /// one-open-position-per-ticker invariant (its duplicate-fill guard), so a
    /// backstop IOC for the remainder would fill real contracts the ledger and
    /// kill-switch could never see. The forgone EV is ~4.3¢ on a handful of
    /// contracts; the invariant is worth more.
    ///
    /// FIX 3 (moneypath F1): not topping up is NOT the same as leaving the
    /// remainder alive. `drop_leg` forgets the order and releases its
    /// reservation, but the exchange keeps the unfilled contracts on the book
    /// until `expiration_ts` — and enforces THAT lazily (~2-3min). A second fill
    /// on the same ticker is then refused by BOTH `on_fill_actual_fee` and
    /// `adopt_orphan` (`has_open`), so those contracts are real money that
    /// bankroll, `day_loss`, the drawdown kill-switch and settlement can never
    /// see; and the un-reserved collateral pushes the divergence breaker into a
    /// sticky halt no log explains. So: CANCEL THE REMAINDER FIRST, then drop.
    async fn settle_maker_fill(&self, eng: &Engine, leg: &MakerLeg, f: LegFill, now: i64) {
        let remainder = leg.order.count - f.count;
        let mut remainder_cancel = json!(null);
        let mut remainder_canceled = false;
        if remainder > 0 && !leg.paper {
            match in_window(eng.kalshi.cancel_order(&leg.order_id)).await {
                Ok(Ok(v)) => {
                    remainder_canceled = true;
                    logging::info(format!(
                        "streak {}: partial maker fill on {} ({} of {}) — cancelled the {} \
                         resting remainder (reduced_by {:?})",
                        leg.series,
                        leg.ticker(),
                        f.count,
                        leg.order.count,
                        remainder,
                        kalshi::parse_cancel_reduced_by(&v)
                    ));
                    remainder_cancel = v;
                }
                // 404 here is BENIGN: the rest of the order is already gone
                // (filled through, or swept). Nothing left to cancel.
                Ok(Err(e)) if engine::net::http_status(&e) == Some(404) => {
                    remainder_canceled = true;
                    remainder_cancel = json!({"status": 404, "note": "already gone"});
                    logging::info(format!(
                        "streak {}: partial maker fill on {} — remainder cancel 404 (already \
                         fully off the book); benign",
                        leg.series,
                        leg.ticker()
                    ));
                }
                Ok(Err(e)) => {
                    remainder_cancel = json!({"error": e.to_string()});
                    eprintln!(
                        "[streak] CRITICAL: partial fill on {} left {remainder} contracts \
                         resting and the cancel FAILED: {e}",
                        leg.ticker()
                    );
                    alert::notify(
                        &eng.http,
                        &format!(
                            "streak PARTIAL FILL on {}: {} of {} filled, the {remainder}-contract \
                             remainder could NOT be cancelled ({e}) — it may still fill and the \
                             ledger cannot book a second position on this ticker; check \
                             /portfolio/orders",
                            leg.ticker(),
                            f.count,
                            leg.order.count
                        ),
                    )
                    .await;
                }
                Err(_elapsed) => {
                    remainder_cancel = json!({"error": "in_window timeout"});
                    eprintln!(
                        "[streak] CRITICAL: partial fill on {} left {remainder} contracts \
                         resting and the cancel TIMED OUT",
                        leg.ticker()
                    );
                    alert::notify(
                        &eng.http,
                        &format!(
                            "streak PARTIAL FILL on {}: {} of {} filled, the remainder cancel \
                             timed out — check /portfolio/orders",
                            leg.ticker(),
                            f.count,
                            leg.order.count
                        ),
                    )
                    .await;
                }
            }
        }

        eng.book_resting_fill(&leg.order, f.count, f.price_cents, f.actual_fee_cents);
        self.drop_leg(eng, &leg.ticker(), &leg.reserve_key);

        let fee_cents = taker_fee(f.price_cents, f.count) * 100.0;
        let mut rec = self.base_record(&leg.series, &leg.ticker(), &leg.meta, EntryPath::MakerRest);
        rec["filled"] = json!(true);
        rec["simulated"] = json!(f.simulated);
        rec["partial"] = json!(f.count < leg.order.count);
        rec["fill_price"] = json!(f.price_cents);
        rec["filled_count"] = json!(f.count);
        rec["canceled_count"] = json!(remainder);
        // FIX 3 forensics: did the unfilled remainder actually come off the book?
        if remainder > 0 {
            rec["remainder_canceled"] = json!(remainder_canceled);
            rec["remainder_cancel"] = remainder_cancel;
            rec["remainder_reduced_by"] =
                json!(kalshi::parse_cancel_reduced_by(&rec["remainder_cancel"]));
        }
        rec["limit_placed"] = json!(leg.order.limit_cents);
        rec["ts_submit"] = json!(leg.placed_ms);
        rec["ts_fill"] = json!(f.ts_ms);
        rec["rest_secs"] = json!(now - leg.t0);
        rec["order_id"] = json!(leg.order_id);
        // BOTH numbers, always: our taker-model estimate and the exchange's own
        // `fee_cost` from the fills row. The bankroll is charged the exchange
        // figure when present (a maker fill billed 0.000000 on demo) and the
        // estimate only as a fallback.
        rec["fee_cents"] = json!(fee_cents); // our estimate
        rec["actual_fee_cents"] = json!(f.actual_fee_cents); // exchange truth
        rec["is_maker_fill"] = json!(f.all_maker);
        rec["fee_basis"] = json!(if f.actual_fee_cents.is_some() {
            "exchange_fee_cost"
        } else {
            "taker_estimate_fallback"
        });
        if !f.simulated {
            rec["fills_raw"] = f.raw.clone();
        }
        logging::record_path(WEEK1_LOG, rec);

        logging::info(format!(
            "streak {}: {}MAKER FILLED {}x {} {} @ {}c (rested {}s)",
            leg.series,
            if f.simulated { "[paper] " } else { "" },
            f.count,
            leg.meta.side.as_str(),
            leg.ticker(),
            f.price_cents,
            now - leg.t0
        ));
        if !f.simulated {
            alert::notify(
                &eng.http,
                &format!(
                    "streak MAKER FILLED {}x {} {} @ {}c (fade {}, rested {}s)",
                    f.count,
                    leg.meta.side.as_str(),
                    leg.ticker(),
                    f.price_cents,
                    leg.meta.streak_dir,
                    now - leg.t0
                ),
            )
            .await;
        }
    }

    /// A TAKER leg: IOC marketable-limit at the ceiling with the retry ladder.
    ///
    /// LIVE sends the ceiling unconditionally — IOC price improvement pays the
    /// real ask when it is lower, and an order that crosses nothing simply
    /// returns fill_count 0, which IS the correct no-trade. That removes any
    /// dependence on a REST quote that lags 0.5-3s.
    #[allow(clippy::too_many_arguments)]
    async fn taker_leg(
        &self,
        eng: &Engine,
        series: &str,
        ticker: &str,
        meta: &EntryMeta,
        close_unix: i64,
        ceiling: i64,
        observed_ask: Option<f64>,
        path: EntryPath,
        extra: serde_json::Value,
    ) -> Result<()> {
        let mut rec = self.base_record(series, ticker, meta, path);
        rec["ceiling"] = json!(ceiling);
        if let Some(obj) = extra.as_object() {
            for (k, v) in obj {
                rec[k.as_str()] = v.clone();
            }
        }

        let limit = match exec::taker_limit(eng.mode, observed_ask, ceiling) {
            Some(l) => l,
            None => {
                // PAPER only: the observed ask is above the ceiling, so no cross.
                rec["reject_reason"] = json!("above_ceiling");
                rec["observed_ask"] = json!(observed_ask);
                logging::record_path(WEEK1_LOG, rec);
                logging::info(format!(
                    "streak {series}: [paper] {ticker} no cross — ask {observed_ask:?} > {ceiling}c"
                ));
                return Ok(());
            }
        };
        rec["limit_placed"] = json!(limit);

        // FIX 5b (constants F3): attempt 1 gets the SAME entry-window guard as
        // attempts 2-4. The ladder's `ttc < MIN_TTC_SECS + 3` check only ran
        // BETWEEN attempts, so a stalled deadline cancel (or any slow pass)
        // could fire the first — and, once the guard broke the loop, the ONLY —
        // IOC outside the 60s window every number in the policy was fitted on:
        // the 52% win rate, the 24% dip probability, the 21% taker fill rate.
        // Outside the window that order prices a regime we have never measured.
        let ttc0 = close_unix - chrono::Utc::now().timestamp();
        if ttc0 < signal::MIN_TTC_SECS + 3 {
            rec["reject_reason"] = json!("out_of_entry_window");
            rec["ttc_at_attempt"] = json!(ttc0);
            logging::record_path(WEEK1_LOG, rec);
            logging::info(format!(
                "streak {series}: {ticker} {} SUPPRESSED — ttc {ttc0}s is outside the entry \
                 window (needs ≥ {}s); the policy was never measured there",
                path.as_str(),
                signal::MIN_TTC_SECS + 3
            ));
            return Ok(());
        }

        let sig = Signal {
            strategy: "streak".into(),
            ticker: ticker.to_string(),
            side: meta.side,
            limit_cents: limit,
            cluster: format!("streak-{close_unix}"),
            sizing: SizingHint::Flat,
        };

        let mut attempts: u32 = 1;
        let mut retry_books: Vec<serde_json::Value> = Vec::new();
        let outcome = loop {
            let out = eng.execute_attempt(sig.clone(), attempts).await;
            if !matches!(&out, ExecOutcome::Missed { .. }) || attempts >= exec::MAX_ENTRY_ATTEMPTS {
                break out;
            }
            // Next attempt must still land inside the entry window: 2s spacing
            // + ~1s of order round-trip margin.
            let ttc = close_unix - chrono::Utc::now().timestamp();
            if ttc < signal::MIN_TTC_SECS + 3 {
                break out;
            }
            logging::info(format!(
                "streak {series}: {ticker} zero-fill IOC (attempt {attempts}/{}) — retrying in {}ms",
                exec::MAX_ENTRY_ATTEMPTS,
                exec::RETRY_SPACING_MS
            ));
            tokio::time::sleep(std::time::Duration::from_millis(exec::RETRY_SPACING_MS)).await;
            // DIAGNOSTIC (R154): snapshot the book at each retry so the record
            // shows what was actually resting when each order arrived
            // (phantom-quote vs real-but-taken vs repricing-away).
            let snap = match in_window(eng.kalshi.orderbook(ticker)).await {
                Ok(Ok(b)) => b,
                _ => json!(null),
            };
            retry_books.push(json!({"attempt": attempts + 1, "ts_ms": chrono::Utc::now().timestamp_millis(), "book": snap}));
            attempts += 1;
        };
        rec["attempts"] = json!(attempts);
        if !retry_books.is_empty() {
            rec["retry_books"] = json!(retry_books);
        }

        match &outcome {
            ExecOutcome::Filled { fill, response, order } => {
                self.record_fill(eng, series, ticker, rec, order, fill, Some(response.clone()))
                    .await;
            }
            ExecOutcome::RecoveredFill { fill, order } => {
                // Lost-ack recovery: a fill that landed despite a placement error.
                // execute_live already fired its own alert; no second one here.
                let mut rec = rec;
                rec["recovered"] = json!(true);
                self.record_fill_quiet(series, ticker, rec, order, fill);
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
                    "streak {series}: MISSED (no fill, canceled {}) {ticker}",
                    order.count
                ));
                logging::record_path(WEEK1_LOG, rec);
            }
            ExecOutcome::Rejected(r) => {
                rec["reject_reason"] = json!(format!("risk:{r:?}"));
                logging::info(format!("streak {series}: rejected ({r:?}) {ticker}"));
                logging::record_path(WEEK1_LOG, rec);
            }
            ExecOutcome::OrderError(e) => {
                rec["reject_reason"] = json!(format!("order_error:{e}"));
                logging::info(format!("streak {series}: ORDER FAILED {ticker} ({e})"));
                logging::record_path(WEEK1_LOG, rec);
                alert::notify(&eng.http, &format!("streak ORDER FAILED {ticker} ({e})")).await;
            }
        }
        Ok(())
    }

    /// Write a filled participation record and alert (live fills only).
    #[allow(clippy::too_many_arguments)]
    async fn record_fill(
        &self,
        eng: &Engine,
        series: &str,
        ticker: &str,
        rec: serde_json::Value,
        order: &Order,
        fill: &engine::strategy::FillReport,
        response: Option<serde_json::Value>,
    ) {
        let mut rec = rec;
        if let Some(r) = response {
            if !fill.simulated {
                rec["order"] = r;
            }
        }
        self.record_fill_quiet(series, ticker, rec, order, fill);
        if !fill.simulated {
            alert::notify(
                &eng.http,
                &format!(
                    "streak FILLED {}x {} {} @ {}c{}",
                    fill.filled,
                    order.side.as_str(),
                    ticker,
                    fill.fill_price_cents,
                    if fill.partial { " partial" } else { "" }
                ),
            )
            .await;
        }
    }

    /// The fill fields shared by every path (no alert, no network).
    fn record_fill_quiet(
        &self,
        series: &str,
        ticker: &str,
        mut rec: serde_json::Value,
        order: &Order,
        fill: &engine::strategy::FillReport,
    ) {
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
        logging::info(format!(
            "streak {series}: {}FILLED {}x {} {ticker} @ {}c{}",
            if fill.simulated { "[paper] " } else { "" },
            fill.filled,
            order.side.as_str(),
            fill.fill_price_cents,
            if fill.partial { " (partial)" } else { "" }
        ));
        logging::record_path(WEEK1_LOG, rec);
    }
}

/// A fill observed on a resting leg.
struct LegFill {
    count: i64,
    price_cents: i64,
    ts_ms: Option<i64>,
    /// ACTUAL fee in cents from the fills row's `fee_cost` (demo-verified).
    actual_fee_cents: Option<f64>,
    /// `Some(true)` when every row said `is_taker: false` — a true maker fill.
    all_maker: Option<bool>,
    /// Raw `/portfolio/fills` body (fee forensics); null in paper.
    raw: serde_json::Value,
    simulated: bool,
}

impl MakerLeg {
    fn ticker(&self) -> String {
        self.order.ticker.clone()
    }

    /// A detached copy, so supervision never holds the state lock across an await.
    fn snapshot(&self) -> MakerLeg {
        MakerLeg {
            series: self.series.clone(),
            meta: self.meta.clone(),
            order: self.order.clone(),
            order_id: self.order_id.clone(),
            t0: self.t0,
            close_unix: self.close_unix,
            backstop_at: self.backstop_at,
            expiration_ts: self.expiration_ts,
            placed_ms: self.placed_ms,
            reserve_key: self.reserve_key.clone(),
            last_ask: self.last_ask,
            ceiling: self.ceiling,
            paper: self.paper,
            cancel_failed: self.cancel_failed,
            off_book: self.off_book,
        }
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
            skip_kind(&Skip::NotEntryWindow { ttc: 800 }),
            Some("missed_entry_window")
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

    #[test]
    fn cancel_404_means_filled_only_before_expiry() {
        // FIX 6 (reality F3). T0 = window open; expiration_ts = T0+60.
        let t0 = 1_784_987_100i64;
        let exp = t0 + exec::MAKER_EXPIRY_SECS;

        // The normal deadline cancel at T0+45: a 404 can only be a FILL.
        assert_eq!(classify_cancel_404(t0 + 45, exp), Gone404::Filled);
        // Right at expiry, and inside the clock slack: still a fill.
        assert_eq!(classify_cancel_404(exp, exp), Gone404::Filled);
        assert_eq!(
            classify_cancel_404(exp + EXPIRY_404_SLACK_SECS, exp),
            Gone404::Filled
        );

        // The guaranteed-to-happen case: mark_cancel_failed retries every pass
        // until expiration_ts + CANCEL_RETRY_GRACE_SECS (T0+300), while the
        // exchange's lazy sweep expires the order somewhere in T0+60..T0+240.
        // Every retry past that point 404s, and the old code read all of them as
        // "it filled" → backstop withheld forever + a false CRITICAL page.
        assert_eq!(
            classify_cancel_404(exp + EXPIRY_404_SLACK_SECS + 1, exp),
            Gone404::MaybeExpired
        );
        assert_eq!(classify_cancel_404(t0 + 180, exp), Gone404::MaybeExpired);
        assert_eq!(
            classify_cancel_404(exp + CANCEL_RETRY_GRACE_SECS, exp),
            Gone404::MaybeExpired
        );
    }

    #[test]
    fn expiry_slack_cannot_swallow_the_backstop_deadline() {
        // The slack must stay far below the gap between the backstop cancel
        // (T0+45) and expiry (T0+60), or a genuine pre-expiry fill would be
        // mis-read as an expiry.
        const _: () = assert!(EXPIRY_404_SLACK_SECS > 0);
        // The slack must not reach back to the backstop deadline (T0+45), or a
        // genuine pre-expiry fill would be mis-read as an expiry.
        const _: () =
            assert!(EXPIRY_404_SLACK_SECS < exec::MAKER_EXPIRY_SECS - exec::BACKSTOP_AT_SECS);
        // And a leg cancelled at the deadline is unambiguously inside it.
        let t0 = 0i64;
        assert_eq!(
            classify_cancel_404(t0 + exec::BACKSTOP_AT_SECS, t0 + exec::MAKER_EXPIRY_SECS),
            Gone404::Filled
        );
    }

    #[test]
    fn attempt_one_window_guard_matches_the_ladder_guard() {
        // FIX 5b (constants F3): the check applied to attempt 1 is byte-identical
        // to the one the ladder applies between attempts 2-4 — `ttc <
        // MIN_TTC_SECS + 3`. Expressed against the window: it bites at T0+57.
        let t0 = 1_784_987_100i64;
        let close = t0 + signal::WINDOW_SECS;
        let bites_at = |now: i64| close - now < signal::MIN_TTC_SECS + 3;
        assert!(!bites_at(t0)); // at the open
        assert!(!bites_at(t0 + 45)); // the normal backstop moment
        assert!(!bites_at(t0 + 56));
        assert!(bites_at(t0 + 58)); // past the ladder's own cutoff
        // The failure this closes: a 30s-stalled deadline cancel returning at
        // T0+75 used to fire one un-modelled IOC 15s outside the window.
        assert!(bites_at(t0 + 75));
    }
}
