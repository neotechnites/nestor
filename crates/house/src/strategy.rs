//! House fill-probe (H10/H9) — the maker sleeve: two-sided RESTING quotes on the
//! two vehicles the trade-print markout could not settle (KXAPRPOTUS front-weekly
//! in-band strike; KXCPIYOY nearest "Exactly" rung). It measures ONLY fill
//! realization + between-print gap risk (protocol work/probe-house.md).
//!
//! SAFETY (all non-negotiable, charter §§1-5):
//!   1. Every resting order carries `expiration_ts = now + 75s` — a dead process
//!      leaves NOTHING resting beyond ~75s. THE load-bearing property.
//!   2. Cancel-all-house-orders on startup (orphan sweep) AND on shutdown
//!      (ctrl-c/SIGTERM handler wired in nestor_bin for the `house` subcommand).
//!   3. −$20 cumulative hard stop IN CODE (probe cent-ledger incl. fees) + the
//!      protocol's −5¢-markout-in-60s gap-through stop → sticky halt + cancel all.
//!   4. Spread gate ≥2¢, catalyst-window pull (T±15min), 1-5 contracts/side.
//!   5. Live-gated: real orders only when mode==Live AND HOUSE_PROBE=1. Standalone
//!      `house` is banned in live by nestor_bin. Paper / live-without-the-flag =
//!      log-only SHADOW quoting (no real orders, no fills).
//!
//! DATA CAPTURE: every quote-live tick, fill, markout, gate-pull and shadow is
//! logged to `data/house_probe.jsonl` — the `house-report` subcommand summarizes
//! the four protocol metrics from it.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use anyhow::Result;
use async_trait::async_trait;
use chrono_tz::America::New_York;
use engine::kalshi::{self, Market};
use engine::risk::taker_fee;
use engine::strategy::{in_window, Mode};
use engine::{alert, logging, Engine, Side, Strategy};
use serde_json::json;

use crate::signal::{self, ProbeLedger, ORDER_TTL_SECS};

pub const LOG: &str = "data/house_probe.jsonl";
/// Env flag that must be `1` for the sleeve to place a REAL (live) maker order.
const LIVE_ENABLE_ENV: &str = "HOUSE_PROBE";
/// Optional operator-supplied catalyst timestamps (comma-separated unix seconds).
const CATALYST_ENV: &str = "HOUSE_CATALYST_TS";
/// Contracts per side (1-5; charter §4). Override with HOUSE_SIZE.
const SIZE_ENV: &str = "HOUSE_SIZE";
const DEFAULT_SIZE: i64 = 1;

/// A probe book (vehicle) to quote.
struct Book {
    /// Kalshi series ticker (UNDERIVED — confirm against live /markets).
    series: &'static str,
    label: &'static str,
    /// In-band YES-mid selection window (whole cents).
    band_lo: i64,
    band_hi: i64,
    /// PREFER an "Exactly" rung when the ladder has them (CPI nearest-print), but
    /// fall back to the nearest-centre in-band rung when it does not. EMPIRICAL
    /// (2026-07-25): KXCPIYOY currently exposes ONLY "Above X%" cumulative rungs —
    /// zero "Exactly" rungs — so a hard requirement would mean the CPI book never
    /// quotes; the in-band "Above" rungs near 50¢ are valid balanced targets.
    prefer_exactly: bool,
}

fn books() -> Vec<Book> {
    vec![
        Book {
            series: "KXAPRPOTUS",
            label: "potus",
            band_lo: 30,
            band_hi: 70,
            prefer_exactly: false,
        },
        Book {
            series: "KXCPIYOY",
            label: "cpi",
            band_lo: 10,
            band_hi: 90,
            prefer_exactly: true,
        },
    ]
}

/// Prefix every house `client_order_id` carries (`place_leg`).
const COID_PREFIX: &str = "house-";

/// Does this resting order belong to the house sleeve? (FIX 2c, moneypath F5.)
///
/// PRIMARY evidence is the coid namespace — every house leg is posted as
/// `house-{ticker}-{side}-{ts}`. Kalshi's resting-orders schema is only
/// confirmed on the demo shakeout though, so when the payload does not echo the
/// coid we fall back to house's own series, which are DISJOINT from streak's
/// (KXBTC15M/KXETH15M) and volbook's (metal dailies). Either way the sweep can
/// never reach another sleeve's order — the property the fix exists to restore.
fn is_house_order(o: &kalshi::RestingOrder) -> bool {
    match o.client_order_id.as_deref() {
        Some(coid) => coid.starts_with(COID_PREFIX),
        None => books().iter().any(|b| o.ticker.starts_with(b.series)),
    }
}

/// Live per-market quote state.
#[derive(Default)]
struct BookQuote {
    bid_order_id: Option<String>,
    ask_order_id: Option<String>,
    bid_price: i64,
    ask_price: i64,
    quoted_mid: i64,
    quoted_ts: i64,
    since_ms: i64,
    /// Contracts already booked from each leg (to book only the per-pass delta).
    booked_bid: i64,
    booked_ask: i64,
    /// Unix second up to which quote-live time has already been emitted, so each
    /// `house_quote_live` record carries a DELTA the report can sum (FIX 8/I3).
    /// 0 = nothing emitted yet; the first delta runs from `quoted_ts`.
    last_accrued_ts: i64,
}

/// A fill awaiting its +60s markout stamp.
struct Pending {
    ticker: String,
    label: String,
    side: Side,
    entry_cents: i64,
    ts_ms: i64,
    in_catalyst: bool,
    /// Shared id with the `house_fill` record, so metric 2 can net the fee (I5).
    fill_id: String,
    fee_cents: f64,
    count: i64,
}

#[derive(Default)]
struct HouseState {
    ledger: ProbeLedger,
    quotes: HashMap<String, BookQuote>, // ticker -> live quote
    pending: Vec<Pending>,
    /// (ticker, gate reason) pairs already logged once at human volume.
    gates_logged: std::collections::HashSet<String>,
    /// ticker -> last known mid, for the cross-book −$20 ledger mark.
    last_mid: HashMap<String, i64>,
    /// ticker -> unix second `last_mid` was refreshed, so a markout can say how
    /// STALE the mid it used was (I6). A mid is only refreshed on a pass that
    /// reaches that ticker with a two-sided book.
    last_mid_ts: HashMap<String, i64>,
    halted: bool,
}

pub struct House {
    books: Vec<Book>,
    catalysts: Vec<i64>,
    size: i64,
    state: Mutex<HouseState>,
    swept: AtomicBool,
    /// True while the sleeve is standing down for the GLOBAL risk halt (FIX 2a).
    /// Edge-triggered so the cancel-all fires once per halt episode, not every
    /// 3s pass, and so an operator `resume` brings the sleeve back by itself.
    stood_down: AtomicBool,
}

impl Default for House {
    fn default() -> Self {
        Self::new()
    }
}

impl House {
    pub fn new() -> Self {
        let catalysts = std::env::var(CATALYST_ENV)
            .ok()
            .map(|s| {
                s.split(',')
                    .filter_map(|x| x.trim().parse::<i64>().ok())
                    .collect()
            })
            .unwrap_or_default();
        let size = std::env::var(SIZE_ENV)
            .ok()
            .and_then(|s| s.parse::<i64>().ok())
            .unwrap_or(DEFAULT_SIZE)
            .clamp(1, 5);
        House {
            books: books(),
            catalysts,
            size,
            state: Mutex::new(HouseState::default()),
            swept: AtomicBool::new(false),
            stood_down: AtomicBool::new(false),
        }
    }

    fn live_enabled(eng: &Engine) -> bool {
        eng.mode == Mode::Live && std::env::var(LIVE_ENABLE_ENV).ok().as_deref() == Some("1")
    }

    /// Cancel every resting order **that belongs to THIS sleeve** (startup orphan
    /// sweep AND the shutdown handler AND every halt). Best-effort: logs
    /// failures, never panics. Public so the nestor_bin ctrl-c/SIGTERM handler
    /// can call it on shutdown.
    ///
    /// FIX 2c (moneypath F5). This used to cancel EVERY resting order on the
    /// account. `House::halt` can fire at any time from the −$20 ledger stop or
    /// the −5¢ gap-through stop, and when it did it killed **streak's live 40¢
    /// maker leg** — whose supervisor then reads the resulting cancel-404 as
    /// "it filled", withholds the backstop forever, pins $4 of cap for ~5
    /// minutes and fires a false CRITICAL page. Streak's own sweep was already
    /// correctly series-filtered; the damage was one-directional, house → streak.
    pub async fn cancel_all_house_orders(eng: &Engine) {
        let body = match eng.kalshi.resting_orders(None).await {
            Ok(b) => b,
            Err(e) => {
                eprintln!("[house] resting_orders read failed during sweep: {e}");
                return;
            }
        };
        let all = kalshi::parse_resting_orders(&body);
        let total = all.len();
        let ours: Vec<_> = all.into_iter().filter(is_house_order).collect();
        if ours.is_empty() {
            if total > 0 {
                logging::info(format!(
                    "house: sweep found {total} resting order(s), none of them house's — \
                     leaving them alone"
                ));
            }
            return;
        }
        logging::info(format!(
            "house: sweeping {} of {total} resting order(s) (house-owned only)",
            ours.len()
        ));
        for o in &ours {
            match eng.kalshi.cancel_order(&o.order_id).await {
                Ok(_) => {}
                Err(e) => eprintln!("[house] cancel {} failed during sweep: {e}", o.order_id),
            }
        }
    }

    async fn halt(&self, eng: &Engine, reason: &str) {
        {
            let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if st.halted {
                return;
            }
            st.halted = true;
            st.quotes.clear();
        }
        Self::cancel_all_house_orders(eng).await;
        logging::info(format!("house: STICKY HALT — {reason}"));
        logging::record_path(LOG, json!({"event": "house_halt", "reason": reason}));
        alert::notify(&eng.http, &format!("house probe HALTED — {reason}")).await;
    }
}

#[async_trait]
impl Strategy for House {
    fn name(&self) -> &str {
        "house"
    }

    async fn run(&self, eng: &Engine) -> Result<()> {
        // STARTUP ORPHAN SWEEP (charter §2): cancel anything a prior crash left
        // resting, exactly once, before the first quote. Only meaningful when we
        // can actually place/cancel (live+flag); harmless otherwise.
        if !self.swept.swap(true, Ordering::SeqCst) && Self::live_enabled(eng) {
            Self::cancel_all_house_orders(eng).await;
        }

        // FIX 2a (moneypath F2 / constants F1): THE GLOBAL KILL-SWITCH BINDS
        // HERE TOO. House deliberately does not route orders through
        // `risk.evaluate` (it is two-sided by design and would trip the
        // one-position-per-ticker invariant), which meant it never saw
        // `Rejection::Halted` — so a drawdown halt, a divergence halt or five
        // consecutive placement failures stopped streak and volbook INSTANTLY
        // while house kept quoting and filling real money at 3s cadence,
        // indefinitely. That is the definition of a blind kill-switch.
        //
        // Stand down = cancel our own resting legs ONCE per halt episode and
        // place nothing. NOT sticky in house's own flag: an operator `resume`
        // must bring the sleeve back without a restart.
        if eng.risk_halted() {
            if !self.stood_down.swap(true, Ordering::SeqCst) {
                {
                    let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
                    st.quotes.clear();
                }
                if Self::live_enabled(eng) {
                    Self::cancel_all_house_orders(eng).await;
                }
                logging::info(
                    "house: GLOBAL RISK HALT — standing down (quotes cancelled, no new orders \
                     until the risk layer resumes)",
                );
                logging::record_path(
                    LOG,
                    json!({"event": "house_stand_down", "reason": "global_risk_halt"}),
                );
                alert::notify(
                    &eng.http,
                    "house probe standing down — the GLOBAL risk halt is engaged",
                )
                .await;
            }
            return Ok(());
        }
        if self.stood_down.swap(false, Ordering::SeqCst) {
            logging::info("house: global risk halt cleared — resuming quoting");
            logging::record_path(
                LOG,
                json!({"event": "house_resume", "reason": "global_risk_halt_cleared"}),
            );
        }

        if self.state.lock().unwrap_or_else(|e| e.into_inner()).halted {
            return Ok(());
        }

        for book in &self.books {
            if let Err(e) = self.run_book(eng, book).await {
                let retryable =
                    engine::net::http_status(&e).is_some_and(engine::net::is_retryable_status);
                if retryable {
                    return Err(e); // let the caller back off
                }
                logging::info(format!("house {}: {e} — skip pass", book.label));
            }
        }
        // Process any pending markouts that have aged past the 60s horizon.
        self.settle_markouts(eng).await;
        Ok(())
    }
}

impl House {
    /// One book pass: select the in-band market, apply gates, detect fills,
    /// re-quote. Returns Err only for retryable network faults (so the loop can
    /// back off); everything else is logged and swallowed.
    async fn run_book(&self, eng: &Engine, book: &Book) -> Result<()> {
        let now = chrono::Utc::now().timestamp();

        // Select the in-band target market.
        let opens = match in_window(eng.kalshi.markets(book.series, "open")).await {
            Ok(r) => r?,
            Err(_) => {
                logging::info(format!("house {}: markets fetch timed out", book.label));
                return Ok(());
            }
        };
        let debug = std::env::var("HOUSE_DEBUG").is_ok();
        let Some(m) = self.pick_market(book, &opens) else {
            if debug {
                eprintln!("[house-dbg] {}: {} open, NO in-band pick", book.label, opens.len());
            }
            self.log_pass(book, None, None, None, None, None, "no_pick", false);
            return Ok(()); // no in-band market right now
        };
        let ticker = m.ticker.clone();

        // Live orderbook → best bid/ask/mid.
        let ob = match in_window(eng.kalshi.orderbook(&ticker)).await {
            Ok(Ok(b)) => b,
            _ => return Ok(()),
        };
        let (best_bid, best_ask, mid) = kalshi::orderbook_mid(&ob);
        if debug {
            eprintln!(
                "[house-dbg] {}: pick {ticker} bid={best_bid:?} ask={best_ask:?} mid={mid:?} spread_ok={}",
                book.label,
                signal::spread_ok(best_bid, best_ask)
            );
        }
        let spread = best_ask.zip(best_bid).map(|(a, b)| a - b);
        let Some(mid) = mid else {
            self.pull_quotes(eng, &ticker, "no_two_sided_book").await;
            self.log_pass(book, Some(&ticker), best_bid, best_ask, spread, None,
                          "no_two_sided_book", false);
            self.log_gate_once(book, &ticker, "no_two_sided_book");
            return Ok(());
        };
        {
            let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            st.last_mid.insert(ticker.clone(), mid);
            st.last_mid_ts.insert(ticker.clone(), now);
        }

        // GATE: catalyst window (protocol §2) — pull all quotes T±15min.
        if signal::in_catalyst_window(now, &self.catalysts) {
            self.pull_quotes(eng, &ticker, "catalyst_window").await;
            logging::record_path(
                LOG,
                json!({"event": "house_gate", "book": book.label, "ticker": ticker,
                       "reason": "catalyst_window"}),
            );
            self.log_pass(book, Some(&ticker), best_bid, best_ask, spread, Some(mid),
                          "catalyst_window", false);
            return Ok(());
        }
        // GATE: spread ≥2¢ (protocol §1).
        if !signal::spread_ok(best_bid, best_ask) {
            self.pull_quotes(eng, &ticker, "spread_lt_2c").await;
            self.log_pass(book, Some(&ticker), best_bid, best_ask, spread, Some(mid),
                          "spread_lt_2c", false);
            self.log_gate_once(book, &ticker, "spread_lt_2c");
            return Ok(());
        }
        self.log_pass(book, Some(&ticker), best_bid, best_ask, spread, Some(mid), "ok", true);

        let live = Self::live_enabled(eng);

        // Detect own fills on the currently-resting legs (live only).
        if live {
            self.detect_fills(eng, book, &ticker, mid, now).await;
        }

        // −$20 hard stop (charter §3): mark the whole ledger at per-ticker mids.
        let breached = {
            let st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            st.ledger.hard_stop_breached(&st.last_mid)
        };
        if breached {
            self.halt(eng, "−$20 cumulative hard stop").await;
            return Ok(());
        }

        // Re-quote decision.
        let (need, age) = {
            let st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            match st.quotes.get(&ticker) {
                Some(q) => (
                    signal::should_requote(Some(q.quoted_mid), mid, now - q.quoted_ts),
                    now - q.quoted_ts,
                ),
                None => (true, 0),
            }
        };
        let legs = signal::quote_legs(mid);
        if need {
            if live {
                self.requote(eng, book, &ticker, mid, legs, now).await;
            } else {
                // SHADOW (paper / live-without-flag): log the intended quote only.
                logging::record_path(
                    LOG,
                    json!({"event": "house_shadow", "book": book.label, "ticker": ticker,
                           "mid": mid, "bid": legs.bid_price_cents, "ask": legs.ask_price_cents,
                           "spread": best_ask.zip(best_bid).map(|(a, b)| a - b), "size": self.size}),
                );
            }
        } else {
            // Quote still good — accrue quote-live time for metric 1.
            //
            // FIX 8 / I3 (sensors F2): `quote_secs` is the DELTA since the
            // previous record, not the quote's cumulative age. report.rs SUMS
            // this field, so emitting the cumulative age turned 60 real seconds
            // of quoting into Σ(3,6,…,57) = 570s — a ~9.5× overstatement of
            // quote-hours, i.e. metric 1 (the PROMOTE gate) understated ~10×.
            // A sleeve genuinely filling 5/hr reported 0.5/hr → spurious KILL.
            // Worse, the bias was an uncontrolled function of mid volatility
            // (a requote resets `quoted_ts`), so it was not even a fixable
            // constant factor.
            let delta = {
                let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
                match st.quotes.get_mut(&ticker) {
                    Some(q) => {
                        let last = if q.last_accrued_ts > 0 {
                            q.last_accrued_ts
                        } else {
                            q.quoted_ts
                        };
                        q.last_accrued_ts = now;
                        (now - last).max(0)
                    }
                    None => 0,
                }
            };
            logging::record_path(
                LOG,
                json!({"event": "house_quote_live", "book": book.label, "ticker": ticker,
                       "quote_secs": delta, "quote_age_secs": age.max(0), "mid": mid}),
            );
        }
        Ok(())
    }

    /// FIX 8 / I2 (sensors F4): one record EVERY pass, whatever happens. Before
    /// this, the two gates that actually fire (`no_two_sided_book`,
    /// `spread_lt_2c`) and the no-pick path wrote NOTHING, so
    /// `data/house_probe.jsonl` did not exist after a full weekend live with
    /// HOUSE_PROBE=1 — and "no quotes because the gate works" was an inference
    /// from an ABSENT FILE, indistinguishable from a timed-out markets fetch, a
    /// flag that never reached the process, or a panicked task. This record is
    /// the probe's DENOMINATOR: quotable-spread fraction (H5) and quote uptime
    /// (H6), neither of which was computable at all.
    #[allow(clippy::too_many_arguments)]
    fn log_pass(
        &self,
        book: &Book,
        ticker: Option<&str>,
        best_bid: Option<i64>,
        best_ask: Option<i64>,
        spread: Option<i64>,
        mid: Option<i64>,
        gate: &str,
        quoting: bool,
    ) {
        logging::record_path(
            LOG,
            json!({"event": "house_pass", "book": book.label, "ticker": ticker,
                   "best_bid": best_bid, "best_ask": best_ask, "spread": spread,
                   "mid": mid, "gate": gate, "quoting": quoting, "size": self.size}),
        );
    }

    /// Log a gate ONCE per (ticker, reason) at human volume — the per-pass
    /// denominator lives in `house_pass`, but a first-occurrence line is what an
    /// operator actually reads (sensors F4). Dedup is in-memory, so a restart
    /// re-logs; that is the correct behaviour for a "this is why it is quiet" line.
    fn log_gate_once(&self, book: &Book, ticker: &str, reason: &str) {
        let key = format!("{ticker}|{reason}");
        let first = {
            let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            st.gates_logged.insert(key)
        };
        if first {
            logging::info(format!(
                "house {}: {ticker} standing down — {reason} (first occurrence; per-pass \
                 detail in house_pass records)",
                book.label
            ));
            logging::record_path(
                LOG,
                json!({"event": "house_gate", "book": book.label, "ticker": ticker,
                       "reason": reason, "first_occurrence": true}),
            );
        }
    }

    /// Select the in-band market: within [band_lo,band_hi] YES-mid, nearest to the
    /// band centre (proxy for the deepest-churn / most-balanced rung); CPI requires
    /// an "Exactly" rung.
    fn pick_market<'a>(&self, book: &Book, opens: &'a [Market]) -> Option<&'a Market> {
        let centre = (book.band_lo + book.band_hi) / 2;
        // In-band candidates with distance-to-centre and an "exactly" flag.
        let mut cands: Vec<(&Market, i64, bool)> = opens
            .iter()
            .filter_map(|m| {
                let mid = signal::market_mid_cents(m.yes_ask_cents_f64(), m.no_ask_cents_f64())?;
                if mid < book.band_lo || mid > book.band_hi {
                    return None;
                }
                let exactly = m
                    .yes_sub_title
                    .as_deref()
                    .is_some_and(|s| s.to_ascii_lowercase().contains("exactly"));
                Some((m, (mid - centre).abs(), exactly))
            })
            .collect();
        // Prefer "Exactly" rungs when the ladder has any; else fall back to all
        // in-band rungs (KXCPIYOY has only "Above" rungs — see Book.prefer_exactly).
        if book.prefer_exactly && cands.iter().any(|(_, _, ex)| *ex) {
            cands.retain(|(_, _, ex)| *ex);
        }
        cands.into_iter().min_by_key(|(_, d, _)| *d).map(|(m, _, _)| m)
    }

    /// Cancel any live legs on `ticker` and forget the quote (gate pull / stand-down).
    async fn pull_quotes(&self, eng: &Engine, ticker: &str, reason: &str) {
        let ids: Vec<String> = {
            let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            match st.quotes.remove(ticker) {
                Some(q) => q.bid_order_id.into_iter().chain(q.ask_order_id).collect(),
                None => return,
            }
        };
        if !Self::live_enabled(eng) {
            return;
        }
        for id in ids {
            if let Err(e) = eng.kalshi.cancel_order(&id).await {
                eprintln!("[house] pull {ticker} cancel {id} failed ({reason}): {e}");
            }
        }
    }

    /// Cancel existing legs and post a fresh two-sided quote around `mid` with
    /// `expiration_ts = now + 75s`.
    async fn requote(
        &self,
        eng: &Engine,
        book: &Book,
        ticker: &str,
        mid: i64,
        legs: signal::QuoteLegs,
        now: i64,
    ) {
        // Cancel old legs first.
        let old: Vec<String> = {
            let st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            st.quotes
                .get(ticker)
                .into_iter()
                .flat_map(|q| q.bid_order_id.iter().chain(q.ask_order_id.iter()).cloned())
                .collect()
        };
        for id in old {
            let _ = eng.kalshi.cancel_order(&id).await;
        }

        let exp = now + ORDER_TTL_SECS;
        let (_, bid_px) = legs.bid();
        let (_, ask_px) = legs.ask();
        let bid_id = self
            .place_leg(eng, ticker, Side::Yes, bid_px, exp, now)
            .await;
        let ask_id = self
            .place_leg(eng, ticker, Side::No, ask_px, exp, now)
            .await;

        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        st.quotes.insert(
            ticker.to_string(),
            BookQuote {
                bid_order_id: bid_id.clone(),
                ask_order_id: ask_id.clone(),
                bid_price: bid_px,
                ask_price: ask_px,
                quoted_mid: mid,
                quoted_ts: now,
                since_ms: chrono::Utc::now().timestamp_millis(),
                booked_bid: 0,
                booked_ask: 0,
                last_accrued_ts: 0,
            },
        );
        drop(st);
        logging::record_path(
            LOG,
            json!({"event": "house_quote", "book": book.label, "ticker": ticker, "mid": mid,
                   "bid": bid_px, "ask": ask_px, "size": self.size, "expiration_ts": exp,
                   "bid_order_id": bid_id, "ask_order_id": ask_id}),
        );
    }

    /// Place one resting leg; returns its order_id (None on failure — logged).
    async fn place_leg(
        &self,
        eng: &Engine,
        ticker: &str,
        side: Side,
        price_cents: i64,
        exp: i64,
        now: i64,
    ) -> Option<String> {
        let coid = format!("house-{ticker}-{}-{now}", side.as_str());
        match eng
            .kalshi
            .place_resting_limit_raw(ticker, side.as_str(), self.size, price_cents, exp, &coid)
            .await
        {
            Ok((status, body, _)) if (200..300).contains(&status) => {
                let v: serde_json::Value = serde_json::from_str(&body).unwrap_or(json!(null));
                let placed = kalshi::parse_place_response(&v, side.as_str());
                eng.note_signed_success();
                if placed.order_id.is_none() {
                    eprintln!("[house] no order_id posting {side:?} leg on {ticker}: {body}");
                }
                placed.order_id
            }
            Ok((status, body, _)) => {
                eprintln!("[house] place {side:?} {ticker} HTTP {status}: {body}");
                eng.note_order_failure(Some(status)).await;
                None
            }
            Err(e) => {
                eprintln!("[house] place {side:?} {ticker} error: {e}");
                eng.note_order_failure(engine::net::http_status(&e)).await;
                None
            }
        }
    }

    /// Poll fills for the resting legs and book only the per-pass DELTA. A booked
    /// fill registers a pending markout and (per protocol) triggers a flatten —
    /// implemented here as pulling the quote so the next pass re-posts fresh.
    async fn detect_fills(&self, eng: &Engine, book: &Book, ticker: &str, mid: i64, now: i64) {
        let (bid_id, ask_id, since_ms, booked_bid, booked_ask, bid_px, ask_px) = {
            let st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            match st.quotes.get(ticker) {
                Some(q) => (
                    q.bid_order_id.clone(),
                    q.ask_order_id.clone(),
                    q.since_ms,
                    q.booked_bid,
                    q.booked_ask,
                    q.bid_price,
                    q.ask_price,
                ),
                None => return,
            }
        };
        if bid_id.is_none() && ask_id.is_none() {
            return;
        }
        let body = match eng.kalshi.fills(ticker).await {
            Ok(b) => b,
            Err(e) => {
                eprintln!("[house] fills read {ticker} failed: {e}");
                return;
            }
        };
        let mut any = false;
        for (side, oid, prev, px) in [
            (Side::Yes, &bid_id, booked_bid, bid_px),
            (Side::No, &ask_id, booked_ask, ask_px),
        ] {
            let Some(oid) = oid else { continue };
            let fills = kalshi::parse_fills(&body, Some(oid), side.as_str(), since_ms);
            let (total, avg, ts) = kalshi::fills_summary(&fills);
            let delta = total - prev;
            if delta <= 0 {
                continue;
            }
            any = true;
            let entry = avg.unwrap_or(px);
            // FEE TRUTH (demo-verified 2026-07-26, work/verify-house-truth.md):
            // fills rows carry `fee_cost` (total dollars for the row) and maker
            // fills billed 0.000000 — the taker formula would invent ~1.7¢/fill
            // of phantom cost, enough to flip the probe's promote/kill verdict
            // (H9's whole edge is +0.5¢/fill). Book the exchange's own figure;
            // the formula is only the fallback when the field is absent, and a
            // fallback use is flagged in the record so the report can weigh it.
            //
            // FIX 7c (reality F12): the fold is PER ROW, not all-or-nothing. It
            // used to `try_fold`, so ONE row missing `fee_cost` silently
            // converted the WHOLE batch — including genuine 0.000000 maker
            // fills — to the taker formula, inventing ~1.7¢/contract of phantom
            // cost on exactly the sleeve whose entire gross edge is +0.5¢/fill.
            let mut fee = 0.0f64;
            let mut fee_rows_estimated = 0usize;
            for f in &fills {
                match f.fee_cents {
                    Some(c) => fee += c,
                    None => {
                        fee_rows_estimated += 1;
                        fee += taker_fee(f.price_cents, f.count) * 100.0;
                    }
                }
            }
            let fee_estimated = fee_rows_estimated > 0;
            let all_maker = kalshi::fills_all_maker(&fills);
            let in_catalyst = signal::in_catalyst_window(now, &self.catalysts);

            // FIX 2b (moneypath F2 / constants F1 / reality F1): tell the RISK
            // layer that real cash just left the account. House is deliberately
            // outside the position ledger, so without this the divergence
            // breaker sees `delta × entry + fee` of unexplained spending and
            // HALTS the whole bot — streak and volbook included — after roughly
            // $2.25 of cumulative house inventory. The bridge is released the
            // moment reconcile adopts the resulting position (or held forever
            // for a matched YES+NO pair, which nets to zero position but really
            // did cost ~98¢ of cash).
            let cash_out_cents = delta * entry + fee.round() as i64;
            eng.note_house_cash_cents(ticker, -cash_out_cents);

            // Shared id so `house_markout` can join back to this fill's fee —
            // metric 2 is "net of fees" and the two records had no join key (I5).
            let fill_id = format!("{oid}|{side:?}|{total}");

            {
                let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
                st.ledger
                    .on_fill(ticker, side, delta, entry, fee, ts.unwrap_or(now * 1000));
                if let Some(q) = st.quotes.get_mut(ticker) {
                    match side {
                        Side::Yes => q.booked_bid = total,
                        Side::No => q.booked_ask = total,
                    }
                }
                st.pending.push(Pending {
                    ticker: ticker.to_string(),
                    label: book.label.to_string(),
                    side,
                    entry_cents: entry,
                    ts_ms: ts.unwrap_or(now * 1000),
                    in_catalyst,
                    fill_id: fill_id.clone(),
                    fee_cents: fee,
                    count: delta,
                });
            }
            logging::info(format!(
                "house {}: FILLED {delta}x {side:?} {ticker} @ {entry}c (mid {mid})",
                book.label
            ));
            logging::record_path(
                LOG,
                json!({"event": "house_fill", "book": book.label, "ticker": ticker,
                       "side": side.as_str(), "count": delta, "entry_cents": entry,
                       "mid_at_fill": mid, "fee_cents": fee, "fee_estimated": fee_estimated,
                       "fee_rows_estimated": fee_rows_estimated, "fee_rows": fills.len(),
                       "all_maker": all_maker, "in_catalyst": in_catalyst,
                       "order_id": oid, "fill_id": fill_id}),
            );
            alert::notify(
                &eng.http,
                &format!("house FILLED {delta}x {side:?} {ticker} @ {entry}c"),
            )
            .await;
        }
        // Own fill → flatten: pull the quote so the next pass re-posts around the
        // (post-fill) mid, re-establishing the opposite side (protocol §Re-quote b).
        if any {
            self.pull_quotes(eng, ticker, "own_fill_flatten").await;
        }
    }

    /// Stamp +60s markouts on aged pending fills: compute markout at the current
    /// mid, log it with the gap-through flag, and fire the −5¢ gap-through stop.
    async fn settle_markouts(&self, eng: &Engine) {
        let now_ms = chrono::Utc::now().timestamp_millis();
        let now = now_ms / 1000;
        let mut done: Vec<MarkoutRow> = Vec::new();
        let mut trip_stop = false;
        {
            let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            let mids = st.last_mid.clone();
            let mid_ts = st.last_mid_ts.clone();
            let mut keep = Vec::new();
            for p in std::mem::take(&mut st.pending) {
                let age_secs = (now_ms - p.ts_ms) / 1000;
                if age_secs < signal::MARKOUT_HORIZON_SECS {
                    keep.push(p);
                    continue;
                }
                // FIX 8 / I6 (sensors F7): a markout computed against an ABSENT
                // mid used to fall back to the entry price, producing exactly
                // 0.0¢ — indistinguishable in the tape from a genuine flat
                // markout, and diluting metric 3 (the KILL number) by precisely
                // the cases where the book went one-sided, i.e. the gap-through
                // cases. Now the record says which mid was used, how old it was,
                // and whether it was real.
                let live = mids.get(&p.ticker).copied();
                let mid = live.unwrap_or(p.entry_cents);
                let mid_source = if live.is_some() { "last_book_mid" } else { "entry_fallback" };
                let mid_age_secs = mid_ts.get(&p.ticker).map(|t| now - t);
                let mk = signal::markout_cents(p.side, p.entry_cents, mid);
                let gap = signal::is_gap_through(mk);
                if signal::gap_through_stop(mk, age_secs) {
                    trip_stop = true;
                }
                done.push(MarkoutRow {
                    ticker: p.ticker,
                    label: p.label,
                    side: p.side,
                    entry_cents: p.entry_cents,
                    markout: mk,
                    gap,
                    in_catalyst: p.in_catalyst,
                    fill_id: p.fill_id,
                    fee_cents: p.fee_cents,
                    count: p.count,
                    mid_used: mid,
                    mid_source,
                    mid_age_secs,
                    age_secs,
                });
            }
            st.pending = keep;
        } // guard released here
        for d in &done {
            Self::log_markout(d);
        }
        if trip_stop {
            self.halt(eng, "−5¢ gap-through markout at the 60s horizon").await;
        }
    }

    fn log_markout(d: &MarkoutRow) {
        logging::record_path(
            LOG,
            json!({"event": "house_markout", "book": d.label, "ticker": d.ticker,
                   "side": d.side.as_str(), "entry_cents": d.entry_cents,
                   "markout_cents": d.markout, "gap_through": d.gap,
                   "in_catalyst": d.in_catalyst,
                   // FIX 8 / I5 (sensors F3): metric 2 is computed over ALL
                   // markouts, net of fees, by report.rs. It used to be
                   // `if markout > 0 { Some(markout) }` — E[markout | markout>0],
                   // which is structurally positive, so the "+0.6¢ net of fees"
                   // promote gate COULD NOT FAIL: 10 fills at +1,+1,+1 and 7×−4
                   // (true mean −2.5¢, a kill) reported +1.0¢ and PROMOTED.
                   // The field stays for backward compatibility with old tapes.
                   "half_spread_cents": if d.markout > 0.0 { Some(d.markout) } else { None },
                   // Metric-2 inputs (the fee lives on house_fill, which shared
                   // no id with this record until now — I5).
                   "fill_id": d.fill_id, "fee_cents": d.fee_cents, "count": d.count,
                   // Mid provenance (I6) — separates a real 0¢ from a fabricated one.
                   "mid_used": d.mid_used, "mid_source": d.mid_source,
                   "mid_age_secs": d.mid_age_secs, "markout_age_secs": d.age_secs}),
        );
    }
}

/// One resolved markout, ready to log (FIX 8 — the tuple grew past readable).
struct MarkoutRow {
    ticker: String,
    label: String,
    side: Side,
    entry_cents: i64,
    markout: f64,
    gap: bool,
    in_catalyst: bool,
    /// Joins this markout to its `house_fill` record (I5).
    fill_id: String,
    /// Exchange fee booked on that fill, in cents — metric 2 must net it.
    fee_cents: f64,
    count: i64,
    mid_used: i64,
    mid_source: &'static str,
    mid_age_secs: Option<i64>,
    age_secs: i64,
}

/// ET calendar day (YYYY-MM-DD) of a unix time — used by the CPI catalyst helper.
#[allow(dead_code)]
fn et_day(unix: i64) -> String {
    chrono::DateTime::from_timestamp(unix, 0)
        .map(|dt| dt.with_timezone(&New_York).format("%Y-%m-%d").to_string())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn resting(coid: Option<&str>, ticker: &str) -> kalshi::RestingOrder {
        kalshi::RestingOrder {
            order_id: "oid".into(),
            client_order_id: coid.map(|s| s.to_string()),
            ticker: ticker.into(),
            side: Some("yes".into()),
            remaining_count: 1,
            price_cents: Some(49),
            expiration_ts: None,
        }
    }

    #[test]
    fn sweep_never_touches_another_sleeves_resting_order() {
        // FIX 2c (moneypath F5). The concrete failure: HOUSE_PROBE=1, a POTUS
        // fill marks out −5¢ at T0+20s → House::halt → the old blanket sweep
        // cancelled streak's live 40¢ KXBTC15M bid; streak's own cancel then
        // 404s and is read as "it filled" → backstop withheld forever, $4 of
        // cap frozen ~5 minutes, false CRITICAL page.
        assert!(is_house_order(&resting(
            Some("house-KXAPRPOTUS-26APR-T50-yes-1784987100"),
            "KXAPRPOTUS-26APR-T50"
        )));
        assert!(!is_house_order(&resting(
            Some("streak-KXBTC15M-26JUL251000-00-m40"),
            "KXBTC15M-26JUL251000-00"
        )));
        assert!(!is_house_order(&resting(
            Some("volbook-KXGOLDD-3400"),
            "KXGOLDD-26JUL27-3400"
        )));
    }

    /// MISMATCH GUARD for the coid choke point. `place_leg` builds the coid RAW;
    /// the wire rewrites '.' -> '_' (`kalshi::sanitize_coid`), so what the
    /// exchange echoes back to the sweep is the SANITIZED string — never the
    /// one this crate formatted. Ownership must survive that rewrite, or the
    /// halt-sweep stops recognising its own quotes on exactly the dotted
    /// (fractional-strike) markets the sleeve is there to quote, and leaves them
    /// resting past a halt.
    #[test]
    fn house_ownership_survives_wire_coid_sanitization() {
        let ticker = "KXAPRPOTUS-26JUL31-40.9";
        // Byte-for-byte the format place_leg uses.
        let minted = format!("house-{ticker}-yes-1769900000");
        let echoed = kalshi::sanitize_coid(&minted);
        assert_ne!(echoed, minted, "this ticker must actually be rewritten");
        assert!(!echoed.contains('.'));
        assert!(is_house_order(&resting(Some(&echoed), ticker)));
        // And the rewrite cannot flip a foreign order into ours.
        assert!(!is_house_order(&resting(
            Some(&kalshi::sanitize_coid("volbook-KXCOPPERD-26JUL2717-T6.40")),
            "KXCOPPERD-26JUL2717-T6.40"
        )));
    }

    #[test]
    fn sweep_falls_back_to_house_series_when_the_coid_is_absent() {
        // Kalshi's resting-order schema is only demo-confirmed; if it does not
        // echo the coid, ownership falls back to house's OWN series — disjoint
        // from streak's crypto and volbook's metals, so the guarantee holds.
        for b in books() {
            assert!(is_house_order(&resting(None, &format!("{}-X", b.series))));
        }
        assert!(!is_house_order(&resting(None, "KXBTC15M-26JUL251000-00")));
        assert!(!is_house_order(&resting(None, "KXGOLDD-26JUL27-3400")));
        assert!(!is_house_order(&resting(None, "KXSILVERD-26JUL27-40")));
    }

    #[test]
    fn one_missing_fee_row_does_not_convert_the_whole_batch_to_the_formula() {
        // FIX 7c (reality F12). Per-row fold: a maker row billed 0.000000 keeps
        // its zero even when a sibling row omits `fee_cost`. Under the old
        // all-or-nothing `try_fold` the batch fell back to the taker formula and
        // invented ~1.7¢/contract on a sleeve whose gross edge is +0.5¢/fill.
        let rows = vec![
            kalshi::ParsedFill {
                count: 1,
                price_cents: 49,
                ts_ms: None,
                fee_cents: Some(0.0), // demo-proven maker fill
                is_taker: Some(false),
            },
            kalshi::ParsedFill {
                count: 1,
                price_cents: 49,
                ts_ms: None,
                fee_cents: None, // schema miss on ONE row
                is_taker: None,
            },
        ];
        let mut fee = 0.0f64;
        let mut estimated = 0usize;
        for f in &rows {
            match f.fee_cents {
                Some(c) => fee += c,
                None => {
                    estimated += 1;
                    fee += taker_fee(f.price_cents, f.count) * 100.0;
                }
            }
        }
        assert_eq!(estimated, 1);
        // Only the ONE unknown row is estimated: 0.07*1*0.49*0.51 = 0.017493
        // → ceil($0.0001) = $0.0175 = 1.75¢. The maker row stays free.
        assert!((fee - 1.75).abs() < 1e-9, "fee was {fee}");
        // The old behaviour charged BOTH rows the formula — 2x the phantom cost.
        let old_all_or_nothing = taker_fee(49, 2) * 100.0;
        assert!(old_all_or_nothing > fee * 1.9);
    }
}
