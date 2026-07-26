//! Kalshi trade-api v2 websocket market-data client (`orderbook_delta`).
//!
//! WHY (R155/R156): the REST poll path serves quotes ~0.5-3s stale (poll age +
//! Kalshi's REST cache lag). The websocket streams orderbook deltas straight off
//! the event flow, so a maintained in-memory book is fresh to network RTT. Streak
//! reads it behind `STREAK_WS=1` when it is fresh; REST is always the floor.
//!
//! DESIGN (charter Decisions 1-8; verify on demo, docs are evidence not authority):
//!   * Endpoint = REST api_base with https->wss + `/trade-api/ws/v2`
//!     ([`crate::kalshi::ws_url`]); prod `api.elections.kalshi.com` empirically
//!     works (OSS note 26; demo mirror + real fills).
//!   * Auth = sign `{ts_ms}GET/trade-api/ws/v2` RSA-PSS/SHA256 on the upgrade
//!     request, re-signed every (re)connect so the timestamp stays fresh
//!     ([`crate::kalshi::WsAuth`]). ALL channels need auth now (OSS note 26 R2).
//!   * The Kalshi book is BIDS-ONLY: two sides of resting bids (`yes`,`no`). The
//!     ask for a side = 100 - best opposite bid (same convention as the REST
//!     [`crate::kalshi::orderbook_mid`]). yes_ask = 100 - best_no_bid;
//!     no_ask = 100 - best_yes_bid.
//!   * Per-sid sequence numbers advance by exactly 1; any gap invalidates the
//!     book -> drop it + unsubscribe so the reconcile tick resubscribes for a
//!     fresh snapshot. While unsynced, streak falls back to REST.
//!   * Wire form is tolerant of int-cents (`yes`/`no`, `price`, `delta`) AND
//!     string-dollars (`yes_dollars`/`no_dollars`, `price_dollars`, `delta_fp`) —
//!     openpx (same API generation) emits the *_dollars/_fp strings on deltas.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Mutex;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Result};
use futures::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio_tungstenite::tungstenite::http::{HeaderName, HeaderValue};
use tokio_tungstenite::tungstenite::Message;

use crate::kalshi::WsAuth;

/// Reconnect backoff after a socket error (capped exponential handled inline).
const RECONNECT_DELAY_SECS: u64 = 3;
/// How long a ticker stays subscribed after streak last asked for it. A 15-min
/// market is wanted for its whole life; 300s of slack past the last `want()`
/// covers the lazy-poll gap before the maintainer unsubscribes the closed one.
const WANT_TTL_SECS: u64 = 300;
/// Subscription-reconcile cadence: diff wanted-vs-subscribed and (un)subscribe.
const RECONCILE_TICK: Duration = Duration::from_millis(1000);

/// One side's resting bids: price in whole cents -> resting quantity.
type Levels = BTreeMap<i64, f64>;

/// In-memory order book for one market, rebuilt from a snapshot then advanced by
/// deltas. `synced` is false until a snapshot lands and true only while the seq
/// chain is unbroken — streak must not trust an unsynced book.
#[derive(Debug, Clone, Default)]
pub struct Book {
    /// Resting YES bids (price_cents -> qty).
    pub yes: Levels,
    /// Resting NO bids (price_cents -> qty).
    pub no: Levels,
    /// Last applied top-level `seq`, or None before the first snapshot.
    pub seq: Option<i64>,
    /// Local (receive) time of the last applied update — the age streak reads.
    pub updated: Option<Instant>,
    /// Have a snapshot AND no seq gap since — safe to read.
    pub synced: bool,
}

impl Book {
    /// Highest YES bid price (cents) with resting size.
    pub fn best_yes_bid(&self) -> Option<i64> {
        self.yes.iter().rev().find(|(_, &q)| q > 1e-9).map(|(&p, _)| p)
    }
    /// Highest NO bid price (cents) with resting size.
    pub fn best_no_bid(&self) -> Option<i64> {
        self.no.iter().rev().find(|(_, &q)| q > 1e-9).map(|(&p, _)| p)
    }
    /// Cost to BUY YES = 100 - best NO bid (a NO bid at n offers YES at 100-n).
    pub fn yes_ask(&self) -> Option<i64> {
        self.best_no_bid().map(|n| 100 - n)
    }
    /// Cost to BUY NO = 100 - best YES bid.
    pub fn no_ask(&self) -> Option<i64> {
        self.best_yes_bid().map(|y| 100 - y)
    }
    /// Load a fresh snapshot's levels, replacing any prior state, and mark synced.
    fn load_snapshot(&mut self, yes: Levels, no: Levels, seq: Option<i64>, at: Instant) {
        self.yes = yes;
        self.no = no;
        self.seq = seq;
        self.updated = Some(at);
        self.synced = true;
    }
    /// Apply one delta level; qty<=~0 removes the level.
    fn apply_delta(&mut self, side: Side, price_cents: i64, delta: f64, seq: Option<i64>, at: Instant) {
        let map = match side {
            Side::Yes => &mut self.yes,
            Side::No => &mut self.no,
        };
        let e = map.entry(price_cents).or_insert(0.0);
        *e += delta;
        if *e <= 1e-9 {
            map.remove(&price_cents);
        }
        self.seq = seq;
        self.updated = Some(at);
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Side {
    Yes,
    No,
}

impl Side {
    fn parse(s: &str) -> Option<Side> {
        match s.to_ascii_lowercase().as_str() {
            "yes" => Some(Side::Yes),
            "no" => Some(Side::No),
            _ => None,
        }
    }
}

/// A freshness-stamped quote handed to strategies. `age` is time since the last
/// applied update (network + processing latency); `synced` reflects seq health.
#[derive(Debug, Clone)]
pub struct Quote {
    pub yes_ask: Option<i64>,
    pub no_ask: Option<i64>,
    pub age: Duration,
    pub synced: bool,
}

/// Shared, thread-safe websocket book store. The maintainer task writes it; any
/// number of strategy tasks read it lock-briefly and never block on ws health.
pub struct WsBook {
    books: Mutex<HashMap<String, Book>>,
    /// Tickers streak wants subscribed, each with the last-wanted instant (TTL).
    wanted: Mutex<HashMap<String, Instant>>,
}

impl Default for WsBook {
    fn default() -> Self {
        Self::new()
    }
}

impl WsBook {
    pub fn new() -> Self {
        WsBook {
            books: Mutex::new(HashMap::new()),
            wanted: Mutex::new(HashMap::new()),
        }
    }

    /// Register interest in `ticker` (refreshes its subscription TTL). Cheap —
    /// streak calls it once per scan pass for the current market.
    pub fn want(&self, ticker: &str) {
        self.wanted
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(ticker.to_string(), Instant::now());
    }

    /// Current best-ask quote for `ticker`, or None if we hold no book for it.
    pub fn quote(&self, ticker: &str) -> Option<Quote> {
        let books = self.books.lock().unwrap_or_else(|e| e.into_inner());
        let b = books.get(ticker)?;
        Some(Quote {
            yes_ask: b.yes_ask(),
            no_ask: b.no_ask(),
            age: b.updated.map(|t| t.elapsed()).unwrap_or(Duration::MAX),
            synced: b.synced,
        })
    }

    /// Tickers wanted within the TTL (the maintainer's target subscription set).
    fn wanted_recent(&self) -> HashSet<String> {
        let ttl = Duration::from_secs(WANT_TTL_SECS);
        self.wanted
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .filter(|(_, &t)| t.elapsed() < ttl)
            .map(|(k, _)| k.clone())
            .collect()
    }

    /// Mark every book unsynced (called on disconnect: a reconnected socket must
    /// re-snapshot before any book is trusted again).
    fn invalidate_all(&self) {
        let mut books = self.books.lock().unwrap_or_else(|e| e.into_inner());
        for b in books.values_mut() {
            b.synced = false;
        }
    }
}

// ---------------------------------------------------------------------------
// Pure wire parsing (network-free, unit-tested). Tolerant of the int-cents and
// string-dollars encodings Kalshi emits across snapshot vs delta.
// ---------------------------------------------------------------------------

/// A JSON scalar -> f64, tolerating number-or-string.
fn to_f64(v: &Value) -> Option<f64> {
    v.as_f64().or_else(|| v.as_str().and_then(|s| s.parse::<f64>().ok()))
}

/// A price field -> whole cents. Values ≥1 are already cents (e.g. 3 or "3");
/// values <1 are dollars (e.g. 0.03 or "0.0300"). Mirrors kalshi::price_to_cents.
fn price_cents(v: &Value) -> Option<i64> {
    let n = to_f64(v)?;
    Some(if n < 1.0 {
        (n * 100.0).round() as i64
    } else {
        n.round() as i64
    })
}

/// Parse a snapshot level `[price, size]` pair into (price_cents, qty).
fn parse_level(v: &Value) -> Option<(i64, f64)> {
    let arr = v.as_array()?;
    let p = price_cents(arr.first()?)?;
    let q = arr.get(1).and_then(to_f64).unwrap_or(0.0);
    Some((p, q))
}

/// Parse one side's snapshot levels from `msg`. The LIVE demo wire (verified
/// 2026-07-26) uses `<side>_dollars_fp` — arrays of `["0.4400","335.00"]`
/// string-dollar price + string-qty pairs; we also tolerate `<side>_dollars`
/// and the bare int-cents `<side>` documented elsewhere. Whichever key is
/// present wins (checked fp -> dollars -> bare).
fn parse_side_levels(msg: &Value, bare: &str, dollars: &str, dollars_fp: &str) -> Levels {
    let arr = msg
        .get(dollars_fp)
        .or_else(|| msg.get(dollars))
        .or_else(|| msg.get(bare))
        .and_then(|v| v.as_array());
    let mut out = Levels::new();
    if let Some(levels) = arr {
        for lvl in levels {
            if let Some((p, q)) = parse_level(lvl) {
                if q > 1e-9 {
                    out.insert(p, q);
                }
            }
        }
    }
    out
}

/// A decoded, actionable websocket event (control frames -> `Other`).
#[derive(Debug, Clone, PartialEq)]
enum WsEvent {
    Snapshot {
        ticker: String,
        seq: Option<i64>,
        yes: Levels,
        no: Levels,
    },
    Delta {
        ticker: String,
        seq: Option<i64>,
        side: Side,
        price_cents: i64,
        delta: f64,
    },
    /// A `subscribed` ack: maps our command `id` -> the server `sid`.
    Subscribed {
        id: Option<i64>,
        sid: Option<i64>,
    },
    /// A server `error` frame (logged).
    Error(String),
    Other,
}

/// Decode a text frame into a [`WsEvent`]. Never panics; unknown shapes -> Other.
/// Thin string wrapper over [`parse_value`] — the connection loop parses once and
/// calls `parse_value` directly, so this is used only by the unit tests.
#[cfg(test)]
fn parse_event(text: &str) -> WsEvent {
    match serde_json::from_str::<Value>(text) {
        Ok(v) => parse_value(&v),
        Err(_) => WsEvent::Other,
    }
}

/// [`parse_event`] over an already-parsed value (the connection loop parses once
/// for both the sequence check and the type dispatch).
fn parse_value(v: &Value) -> WsEvent {
    let typ = v.get("type").and_then(|t| t.as_str()).unwrap_or_default();
    // seq lives at the top level alongside type/sid/msg.
    let seq = v.get("seq").and_then(|s| s.as_i64());
    let msg = v.get("msg").unwrap_or(&Value::Null);
    match typ {
        "orderbook_snapshot" => {
            let ticker = msg
                .get("market_ticker")
                .and_then(|t| t.as_str())
                .unwrap_or_default()
                .to_string();
            WsEvent::Snapshot {
                ticker,
                seq,
                yes: parse_side_levels(msg, "yes", "yes_dollars", "yes_dollars_fp"),
                no: parse_side_levels(msg, "no", "no_dollars", "no_dollars_fp"),
            }
        }
        "orderbook_delta" => {
            let ticker = msg
                .get("market_ticker")
                .and_then(|t| t.as_str())
                .unwrap_or_default()
                .to_string();
            let side = msg
                .get("side")
                .and_then(|s| s.as_str())
                .and_then(Side::parse);
            let price = msg
                .get("price")
                .or_else(|| msg.get("price_dollars"))
                .and_then(price_cents);
            let delta = msg
                .get("delta")
                .or_else(|| msg.get("delta_fp"))
                .and_then(to_f64);
            match (side, price, delta) {
                (Some(side), Some(price_cents), Some(delta)) if !ticker.is_empty() => {
                    WsEvent::Delta { ticker, seq, side, price_cents, delta }
                }
                _ => WsEvent::Other,
            }
        }
        "subscribed" => WsEvent::Subscribed {
            id: v.get("id").and_then(|i| i.as_i64()),
            sid: msg.get("sid").and_then(|s| s.as_i64()),
        },
        "error" => WsEvent::Error(v.to_string()),
        _ => WsEvent::Other,
    }
}

// ---------------------------------------------------------------------------
// Connection maintainer.
// ---------------------------------------------------------------------------

/// Run the websocket maintainer forever: connect (signed), subscribe to the
/// wanted tickers, apply snapshots/deltas into `book`, resync on seq gaps, and
/// reconnect with backoff on any failure. Spawn this as a background task — it
/// never returns and never blocks strategy reads (they hold their own Mutex).
pub async fn run(book: Arc<WsBook>, url: String, auth: Option<WsAuth>) {
    let mut fail_streak: u32 = 0;
    if auth.is_none() {
        crate::logging::info(
            "ws: no API key — attempting unauthenticated market-data connect (may be rejected)",
        );
    }
    loop {
        match connect_and_serve(&book, &url, auth.as_ref()).await {
            Ok(()) => fail_streak = 0, // clean close (server-side); reconnect promptly
            Err(e) => {
                fail_streak = fail_streak.saturating_add(1);
                crate::logging::info(format!(
                    "ws: connection ended ({e}) — reconnect in {}s (streak {fail_streak})",
                    backoff_secs(fail_streak)
                ));
            }
        }
        book.invalidate_all();
        tokio::time::sleep(Duration::from_secs(backoff_secs(fail_streak))).await;
    }
}

/// Capped exponential backoff: 3, 6, 12, 24, 48, 60… seconds.
fn backoff_secs(fail_streak: u32) -> u64 {
    if fail_streak == 0 {
        return RECONNECT_DELAY_SECS;
    }
    (RECONNECT_DELAY_SECS * (1u64 << (fail_streak - 1).min(6))).min(60)
}

/// One connection lifecycle: handshake -> subscribe loop -> receive/apply until
/// the socket errors, closes, or a seq gap forces a resync. Returns Ok on a
/// graceful close, Err on any fault (both reconnect; Err just backs off). A seq
/// gap returns Err too, tearing down the socket so a fresh connection re-snapshots
/// every ticker (DRADIS model — no per-sid resync bookkeeping).
async fn connect_and_serve(book: &Arc<WsBook>, url: &str, auth: Option<&WsAuth>) -> Result<()> {
    let req = build_request(url, auth)?;
    let (stream, _resp) = tokio_tungstenite::connect_async(req)
        .await
        .map_err(|e| anyhow!("connect {url}: {e}"))?;
    let (mut write, mut read) = stream.split();
    crate::logging::info(format!("ws: connected {url}"));

    // ticker -> server sid (once its `subscribed` ack lands).
    let mut subscribed: HashMap<String, i64> = HashMap::new();
    // outstanding subscribe cmd id -> ticker (awaiting ack).
    let mut pending: HashMap<i64, String> = HashMap::new();
    // Per-sid sequence tracker (connection-scoped): a gap = a dropped frame.
    let mut seq_by_sid: HashMap<i64, i64> = HashMap::new();
    let mut next_id: i64 = 1;
    let mut reconcile = tokio::time::interval(RECONCILE_TICK);
    reconcile.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

    loop {
        tokio::select! {
            biased;
            _ = reconcile.tick() => {
                reconcile_subscriptions(
                    book, &mut write, &mut subscribed, &mut pending, &mut next_id,
                ).await?;
            }
            msg = read.next() => {
                let msg = match msg {
                    Some(Ok(m)) => m,
                    Some(Err(e)) => return Err(anyhow!("stream error: {e}")),
                    None => return Ok(()), // socket closed
                };
                let text = match msg {
                    Message::Text(t) => t.to_string(),
                    Message::Binary(b) => String::from_utf8_lossy(&b).into_owned(),
                    // Reply to server heartbeats so it doesn't drop us (10s ping).
                    Message::Ping(p) => {
                        write.send(Message::Pong(p)).await.map_err(|e| anyhow!("pong: {e}"))?;
                        continue;
                    }
                    Message::Pong(_) | Message::Frame(_) => continue,
                    Message::Close(_) => return Ok(()),
                };
                // Parse once. FIRST enforce per-sid sequence continuity across
                // EVERY seq-bearing frame (snapshots/`ok`/deltas share one counter
                // per sid) — a gap means a dropped frame, so reconnect for fresh
                // snapshots. THEN dispatch by type.
                let v: Value = match serde_json::from_str(&text) {
                    Ok(v) => v,
                    Err(_) => continue, // non-JSON control frame
                };
                if let (Some(sid), Some(seq)) = (
                    v.get("sid").and_then(|x| x.as_i64()),
                    v.get("seq").and_then(|x| x.as_i64()),
                ) {
                    if !seq_in_order(&mut seq_by_sid, sid, seq) {
                        let prev = seq_by_sid.get(&sid).copied().unwrap_or(-1);
                        return Err(anyhow!(
                            "seq gap sid={sid} {prev}->{seq} — resync via reconnect"
                        ));
                    }
                }
                // Route subscribe acks here (they touch pending/subscribed);
                // book-mutating frames go to apply_event.
                match parse_value(&v) {
                    WsEvent::Subscribed { id: Some(id), sid: Some(sid) } => {
                        if let Some(ticker) = pending.remove(&id) {
                            subscribed.insert(ticker, sid);
                        }
                    }
                    WsEvent::Error(e) => {
                        crate::logging::info(format!("ws: server error frame: {e}"));
                    }
                    ev => apply_event(book, ev),
                }
            }
        }
    }
}

/// Diff wanted-vs-subscribed and (un)subscribe. One ticker per subscribe command
/// so each gets its own sid for clean unsubscribe (charter Decision 3).
async fn reconcile_subscriptions<W>(
    book: &Arc<WsBook>,
    write: &mut W,
    subscribed: &mut HashMap<String, i64>,
    pending: &mut HashMap<i64, String>,
    next_id: &mut i64,
) -> Result<()>
where
    W: SinkExt<Message> + Unpin,
    W::Error: std::fmt::Display,
{
    let wanted = book.wanted_recent();
    // Subscribe to wanted tickers we neither hold a sid for nor have pending.
    let pending_tickers: HashSet<&String> = pending.values().collect();
    let to_add: Vec<String> = wanted
        .iter()
        .filter(|t| !subscribed.contains_key(*t) && !pending_tickers.contains(*t))
        .cloned()
        .collect();
    for t in to_add {
        let id = *next_id;
        *next_id += 1;
        let cmd = json!({
            "id": id,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": [t]},
        });
        write
            .send(Message::Text(cmd.to_string()))
            .await
            .map_err(|e| anyhow!("subscribe send: {e}"))?;
        pending.insert(id, t);
    }
    // Unsubscribe tickers no longer wanted.
    let to_drop: Vec<String> = subscribed
        .keys()
        .filter(|t| !wanted.contains(*t))
        .cloned()
        .collect();
    for t in to_drop {
        if let Some(sid) = subscribed.remove(&t) {
            let id = *next_id;
            *next_id += 1;
            let cmd = json!({"id": id, "cmd": "unsubscribe", "params": {"sids": [sid]}});
            write
                .send(Message::Text(cmd.to_string()))
                .await
                .map_err(|e| anyhow!("unsubscribe send: {e}"))?;
        }
        book.books
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&t);
    }
    Ok(())
}

/// Per-`sid` sequence continuity check. Kalshi numbers frames with ONE monotonic
/// `seq` per subscription id — a single stream that spans every seq-bearing frame
/// on that sid: snapshots for ALL multiplexed market_tickers, `ok` control acks,
/// AND deltas (verified on prod 2026-07-26 — `1,2(ok),3(snapshot#2),4,5…` unbroken
/// under one sid). So the gap check MUST be per-sid/connection, never per-ticker
/// (per-ticker saw `1 -> 4` across the interleaved `ok`+2nd-snapshot and falsely
/// "gapped" on every connect). Returns false on a real gap (caller reconnects for
/// fresh snapshots); the first frame for a sid seeds the baseline.
fn seq_in_order(seq_by_sid: &mut HashMap<i64, i64>, sid: i64, seq: i64) -> bool {
    if let Some(&prev) = seq_by_sid.get(&sid) {
        if seq != prev + 1 {
            return false;
        }
    }
    seq_by_sid.insert(sid, seq);
    true
}

/// Apply one book-mutating event (Snapshot/Delta) to the store. Sequence
/// continuity is enforced upstream per-sid ([`seq_in_order`]); here we only load
/// snapshots and advance books. Acks/errors/control frames never reach here.
fn apply_event(book: &Arc<WsBook>, ev: WsEvent) {
    match ev {
        WsEvent::Snapshot { ticker, seq, yes, no } => {
            if ticker.is_empty() {
                return;
            }
            book.books
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .entry(ticker)
                .or_default()
                .load_snapshot(yes, no, seq, Instant::now());
        }
        WsEvent::Delta { ticker, seq, side, price_cents, delta } => {
            let mut books = book.books.lock().unwrap_or_else(|e| e.into_inner());
            let b = books.entry(ticker).or_default();
            // A delta before this ticker's snapshot (or after a disconnect cleared
            // `synced`) is untrusted — the per-sid seq guard guarantees no frame
            // was dropped, so `synced` flips true as soon as the snapshot lands.
            if !b.synced {
                return;
            }
            b.apply_delta(side, price_cents, delta, seq, Instant::now());
        }
        _ => {}
    }
}

/// Build the signed handshake request (or unauthenticated if `auth` is None).
fn build_request(
    url: &str,
    auth: Option<&WsAuth>,
) -> Result<tokio_tungstenite::tungstenite::handshake::client::Request> {
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;
    let mut req = url
        .into_client_request()
        .map_err(|e| anyhow!("ws request build: {e}"))?;
    if let Some(a) = auth {
        for (k, val) in a.headers() {
            let name = HeaderName::from_bytes(k.as_bytes())
                .map_err(|e| anyhow!("bad header name {k}: {e}"))?;
            let value = HeaderValue::from_str(&val)
                .map_err(|e| anyhow!("bad header value: {e}"))?;
            req.headers_mut().insert(name, value);
        }
    }
    Ok(req)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ask_is_complement_of_best_opposite_bid() {
        let mut b = Book { synced: true, ..Default::default() };
        // YES bids at 40,42; NO bids at 55,53.
        b.yes.insert(40, 100.0);
        b.yes.insert(42, 50.0);
        b.no.insert(55, 80.0);
        b.no.insert(53, 20.0);
        assert_eq!(b.best_yes_bid(), Some(42));
        assert_eq!(b.best_no_bid(), Some(55));
        // Buy YES = 100 - best no bid (55) = 45.
        assert_eq!(b.yes_ask(), Some(45));
        // Buy NO = 100 - best yes bid (42) = 58.
        assert_eq!(b.no_ask(), Some(58));
    }

    #[test]
    fn empty_side_yields_no_ask() {
        let b = Book::default();
        assert_eq!(b.yes_ask(), None);
        assert_eq!(b.no_ask(), None);
    }

    #[test]
    fn delta_add_and_remove_level_moves_best() {
        let mut b = Book { synced: true, ..Default::default() };
        b.no.insert(55, 80.0);
        let t = Instant::now();
        // A better NO bid at 57 appears -> yes_ask tightens to 43.
        b.apply_delta(Side::No, 57, 30.0, Some(2), t);
        assert_eq!(b.yes_ask(), Some(43));
        // That level is fully removed -> yes_ask falls back to 100-55=45.
        b.apply_delta(Side::No, 57, -30.0, Some(3), t);
        assert_eq!(b.best_no_bid(), Some(55));
        assert_eq!(b.yes_ask(), Some(45));
    }

    #[test]
    fn parse_snapshot_int_cents_and_dollars() {
        // int-cents `yes`/`no` arrays (IntelIP schema).
        let s = r#"{"type":"orderbook_snapshot","sid":1,"seq":7,
            "msg":{"market_ticker":"KXBTC15M-X","yes":[[40,100],[42,50]],
                   "no":[[55,80]]}}"#;
        match parse_event(s) {
            WsEvent::Snapshot { ticker, seq, yes, no } => {
                assert_eq!(ticker, "KXBTC15M-X");
                assert_eq!(seq, Some(7));
                assert_eq!(yes.get(&42), Some(&50.0));
                assert_eq!(no.get(&55), Some(&80.0));
            }
            other => panic!("expected snapshot, got {other:?}"),
        }
        // LIVE demo wire form: `<side>_dollars_fp` string-dollar/qty pairs
        // (verified 2026-07-26). Also yields the correct complement ask.
        let d = r#"{"type":"orderbook_snapshot","sid":1,"seq":1,
            "msg":{"market_ticker":"KXBTC15M-26JUL261330-30",
                   "no_dollars_fp":[["0.4400","335.00"],["0.5700","5.00"],
                                    ["0.8200","5.00"],["0.9900","26.00"]]}}"#;
        match parse_event(d) {
            WsEvent::Snapshot { yes, no, .. } => {
                assert!(yes.is_empty()); // no YES bids in this snapshot
                assert_eq!(no.get(&44), Some(&335.0));
                assert_eq!(no.get(&99), Some(&26.0));
            }
            other => panic!("expected snapshot, got {other:?}"),
        }
    }

    #[test]
    fn parse_delta_dollars_and_cents_forms() {
        // openpx-style string-dollars delta.
        let d = r#"{"type":"orderbook_delta","seq":8,
            "msg":{"market_ticker":"T","price_dollars":"0.4300","delta_fp":"25","side":"yes"}}"#;
        match parse_event(d) {
            WsEvent::Delta { ticker, seq, side, price_cents, delta } => {
                assert_eq!(ticker, "T");
                assert_eq!(seq, Some(8));
                assert_eq!(side, Side::Yes);
                assert_eq!(price_cents, 43);
                assert!((delta - 25.0).abs() < 1e-9);
            }
            other => panic!("expected delta, got {other:?}"),
        }
        // int-cents form.
        let c = r#"{"type":"orderbook_delta","seq":9,
            "msg":{"market_ticker":"T","price":3,"delta":1079,"side":"no"}}"#;
        match parse_event(c) {
            WsEvent::Delta { side, price_cents, delta, .. } => {
                assert_eq!(side, Side::No);
                assert_eq!(price_cents, 3);
                assert!((delta - 1079.0).abs() < 1e-9);
            }
            other => panic!("expected delta, got {other:?}"),
        }
    }

    #[test]
    fn parse_subscribed_ack() {
        let a = r#"{"type":"subscribed","id":4,"msg":{"channel":"orderbook_delta","sid":22}}"#;
        assert_eq!(parse_event(a), WsEvent::Subscribed { id: Some(4), sid: Some(22) });
    }

    #[test]
    fn control_frames_are_other() {
        assert_eq!(parse_event("not json"), WsEvent::Other);
        assert_eq!(parse_event(r#"{"type":"ok"}"#), WsEvent::Other);
    }

    #[test]
    fn per_sid_seq_is_global_not_per_ticker() {
        // Reproduces the prod stream: ONE sid, seq spans snapshots for TWO
        // tickers + an `ok` control frame + deltas, unbroken. Per-ticker tracking
        // would have "gapped" BTC from seq 1 -> 4; per-sid tracking accepts it.
        let mut m = HashMap::new();
        assert!(seq_in_order(&mut m, 1, 1)); // BTC snapshot
        assert!(seq_in_order(&mut m, 1, 2)); // ok
        assert!(seq_in_order(&mut m, 1, 3)); // ETH snapshot
        assert!(seq_in_order(&mut m, 1, 4)); // delta
        assert!(seq_in_order(&mut m, 1, 5)); // delta
        // A real dropped frame (5 -> 7) is a gap.
        assert!(!seq_in_order(&mut m, 1, 7));
        // A second sid keeps its OWN independent counter (first frame seeds it).
        assert!(seq_in_order(&mut m, 2, 1));
        assert!(seq_in_order(&mut m, 2, 2));
        assert!(!seq_in_order(&mut m, 2, 9));
    }

    #[test]
    fn apply_snapshot_then_delta_builds_book() {
        let book = Arc::new(WsBook::new());
        // Snapshot: NO bid at 55 -> yes_ask 45.
        let snap = r#"{"type":"orderbook_snapshot","sid":1,"seq":1,"msg":{"market_ticker":"T","no":[[55,80]]}}"#;
        apply_event(&book, parse_event(snap));
        assert!(book.quote("T").unwrap().synced);
        assert_eq!(book.quote("T").unwrap().yes_ask, Some(45));
        // In-order delta: better NO bid at 57 -> yes_ask tightens to 43.
        let d2 = r#"{"type":"orderbook_delta","sid":1,"seq":2,"msg":{"market_ticker":"T","price":57,"delta":10,"side":"no"}}"#;
        apply_event(&book, parse_event(d2));
        assert_eq!(book.quote("T").unwrap().yes_ask, Some(43));
    }

    #[test]
    fn delta_before_snapshot_is_ignored() {
        let book = Arc::new(WsBook::new());
        let d = r#"{"type":"orderbook_delta","sid":1,"seq":2,"msg":{"market_ticker":"T","price":57,"delta":10,"side":"no"}}"#;
        apply_event(&book, parse_event(d));
        // Book exists but is unsynced (no snapshot) with no usable ask.
        let q = book.quote("T").unwrap();
        assert!(!q.synced);
        assert_eq!(q.yes_ask, None);
    }

    #[test]
    fn want_ttl_and_quote_absent() {
        let book = WsBook::new();
        assert!(book.quote("NONE").is_none());
        book.want("T");
        assert!(book.wanted_recent().contains("T"));
    }

    #[test]
    fn backoff_progression() {
        assert_eq!(backoff_secs(0), 3);
        assert_eq!(backoff_secs(1), 3);
        assert_eq!(backoff_secs(2), 6);
        assert_eq!(backoff_secs(3), 12);
        assert_eq!(backoff_secs(10), 60);
    }
}

#[cfg(test)]
mod demo_probe {
    use super::*;
    use crate::kalshi::{ws_url, Kalshi};

    /// EMPIRICAL websocket handshake + schema probe (charter Decisions 1/2/5).
    /// Connects to the demo ws, subscribes orderbook_delta for one open ticker,
    /// prints the RAW snapshot + first deltas, and reports the maintained best
    /// asks — settling the endpoint, auth, and exact wire schema on demo before
    /// any prod dependence.
    ///
    /// Ignored (needs demo keys + network). Run with:
    ///   KALSHI_API_BASE=https://demo-api.kalshi.co \
    ///   KALSHI_API_KEY_ID=<demo id> KALSHI_PRIVATE_KEY_PATH=secrets/Demo.txt \
    ///   NESTOR_TEST_TICKER=<open demo ticker> \
    ///   cargo test -p engine ws::demo_probe -- --ignored --nocapture
    #[tokio::test]
    #[ignore]
    async fn demo_ws_connect_and_book() {
        let key_id = std::env::var("KALSHI_API_KEY_ID").expect("KALSHI_API_KEY_ID (demo)");
        let key_path = std::env::var("KALSHI_PRIVATE_KEY_PATH").expect("KALSHI_PRIVATE_KEY_PATH");
        // Comma-separate NESTOR_TEST_TICKER to subscribe MULTIPLE tickers through
        // the maintainer — the case that exposed the false per-ticker seq gap.
        let tickers: Vec<String> = std::env::var("NESTOR_TEST_TICKER")
            .expect("NESTOR_TEST_TICKER")
            .split(',')
            .map(|s| s.trim().to_string())
            .collect();
        let k = Kalshi::authenticated(key_id, &key_path).unwrap();
        let auth = k.ws_auth();
        let url = ws_url();
        println!("=== WS URL === {url} (auth={}) tickers={tickers:?}", auth.is_some());

        let book = Arc::new(WsBook::new());
        for t in &tickers {
            book.want(t);
        }
        let b2 = book.clone();
        let handle = tokio::spawn(async move { run(b2, url, auth).await });

        // Also dump raw frames on a second connection so the exact schema is
        // printed verbatim into the charter Decisions section.
        let raw = tokio::spawn(dump_raw_frames(k.ws_auth(), tickers[0].clone()));

        for i in 1..=15 {
            tokio::time::sleep(Duration::from_secs(1)).await;
            for t in &tickers {
                if let Some(q) = book.quote(t) {
                    let tail = t.rsplit('-').next().unwrap_or(t);
                    println!(
                        "  t+{i}s …{tail}: yes_ask={:?} no_ask={:?} age_ms={} synced={}",
                        q.yes_ask, q.no_ask, q.age.as_millis(), q.synced
                    );
                }
            }
        }
        handle.abort();
        raw.abort();
        println!("=== VERDICT === record endpoint/auth-ok/snapshot-schema/best-asks above into charter Decisions.");
    }

    /// DIAGNOSTIC: subscribe MULTIPLE tickers on one connection and print
    /// `type/sid/seq/ticker` for ~20s — settles whether `seq` is per-connection
    /// (global, interleaves across sids) or per-sid (per subscription). Comma-
    /// separate NESTOR_TEST_TICKER. Run:
    ///   KALSHI_API_KEY_ID=<id> KALSHI_PRIVATE_KEY_PATH=<abs>/secrets/prod.pem \
    ///   NESTOR_TEST_TICKER=KXBTC15M-...,KXETH15M-... \
    ///   cargo test -p engine ws::demo_probe::prod_seq_probe -- --ignored --nocapture
    #[tokio::test]
    #[ignore]
    async fn prod_seq_probe() {
        let key_id = std::env::var("KALSHI_API_KEY_ID").expect("KALSHI_API_KEY_ID");
        let key_path = std::env::var("KALSHI_PRIVATE_KEY_PATH").expect("KALSHI_PRIVATE_KEY_PATH");
        let tickers = std::env::var("NESTOR_TEST_TICKER").expect("NESTOR_TEST_TICKER");
        let k = Kalshi::authenticated(key_id, &key_path).unwrap();
        let req = build_request(&ws_url(), k.ws_auth().as_ref()).unwrap();
        let (stream, resp) = tokio_tungstenite::connect_async(req).await.unwrap();
        println!("handshake HTTP {}", resp.status());
        let (mut write, mut read) = stream.split();
        for (i, t) in tickers.split(',').enumerate() {
            let sub = json!({"id": i as i64 + 1, "cmd": "subscribe",
                "params": {"channels": ["orderbook_delta"], "market_tickers": [t.trim()]}});
            write.send(Message::Text(sub.to_string())).await.unwrap();
        }
        let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
        let mut n = 0;
        while tokio::time::Instant::now() < deadline && n < 60 {
            let Ok(Some(Ok(msg))) =
                tokio::time::timeout(Duration::from_secs(3), read.next()).await
            else {
                break;
            };
            if let Message::Text(t) = msg {
                let v: Value = serde_json::from_str(&t).unwrap_or(Value::Null);
                let typ = v.get("type").and_then(|x| x.as_str()).unwrap_or("?");
                let sid = v.get("sid").and_then(|x| x.as_i64());
                let seq = v.get("seq").and_then(|x| x.as_i64());
                let tk = v.get("msg").and_then(|m| m.get("market_ticker"))
                    .and_then(|x| x.as_str()).unwrap_or("");
                let tail = tk.rsplit('-').next().unwrap_or(tk);
                println!("  type={typ:<18} sid={sid:?} seq={seq:?} ticker=…{tail}");
                n += 1;
            }
        }
        println!("=== VERDICT === compare seq streams grouped by sid vs global ordering.");
    }

    /// Open a raw ws, subscribe, and print the first ~8 text frames verbatim.
    async fn dump_raw_frames(auth: Option<WsAuth>, ticker: String) {
        let req = match build_request(&crate::kalshi::ws_url(), auth.as_ref()) {
            Ok(r) => r,
            Err(e) => {
                println!("RAW: request build failed: {e}");
                return;
            }
        };
        let (stream, resp) = match tokio_tungstenite::connect_async(req).await {
            Ok(x) => x,
            Err(e) => {
                println!("RAW: connect failed: {e}");
                return;
            }
        };
        println!("RAW: handshake HTTP {}", resp.status());
        let (mut write, mut read) = stream.split();
        let sub = json!({
            "id": 1, "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": [ticker]},
        });
        let _ = write.send(Message::Text(sub.to_string())).await;
        let mut n = 0;
        while let Some(Ok(msg)) = read.next().await {
            if let Message::Text(t) = msg {
                println!("RAW FRAME {n}: {t}");
                n += 1;
                if n >= 8 {
                    break;
                }
            }
        }
    }
}
