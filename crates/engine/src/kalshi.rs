//! Kalshi trade-api v2 client.
//!
//! Public market data needs no auth. Portfolio + order placement require RSA
//! request signing: sign  `timestamp_ms + METHOD + path`  with RSA-PSS/SHA-256
//! (salt length = digest length), base64 it, send KALSHI-ACCESS-{KEY,SIGNATURE,
//! TIMESTAMP} headers.

use anyhow::{anyhow, Context, Result};
use base64::Engine as _;
use rsa::pkcs1::DecodeRsaPrivateKey;
use rsa::pkcs8::DecodePrivateKey;
use rsa::pss::SigningKey;
use rsa::signature::{RandomizedSigner, SignatureEncoding};
use rsa::RsaPrivateKey;
use serde::Deserialize;
use serde_json::json;
use sha2::Sha256;

/// Kalshi API host. Override with KALSHI_API_BASE for the demo environment
/// (https://demo-api.kalshi.co) — demo needs its own account + API key, and its
/// books have no real counterparties: plumbing-grade only, never fill-truth.
fn api_base() -> String {
    std::env::var("KALSHI_API_BASE")
        .unwrap_or_else(|_| "https://api.elections.kalshi.com".to_string())
}
const PREFIX: &str = "/trade-api/v2";

#[derive(Debug, Clone, Deserialize)]
pub struct Market {
    pub ticker: String,
    #[serde(default)]
    pub floor_strike: Option<f64>,
    #[serde(default)]
    pub cap_strike: Option<f64>,
    #[serde(default)]
    pub yes_ask_dollars: Option<String>,
    #[serde(default)]
    pub no_ask_dollars: Option<String>,
    #[serde(default)]
    pub yes_sub_title: Option<String>,
    #[serde(default)]
    pub result: Option<String>,
    /// RFC3339 open time.
    #[serde(default)]
    pub open_time: Option<String>,
    /// RFC3339 close time (e.g. "2026-07-16T04:00:00Z").
    #[serde(default)]
    pub close_time: Option<String>,
}

fn dollars_to_cents_f64(s: &Option<String>) -> Option<f64> {
    s.as_ref()
        .and_then(|s| s.parse::<f64>().ok())
        .map(|d| d * 100.0)
}

impl Market {
    /// YES ask in ¢ at deci-cent resolution (0-100), or None if unpriced.
    pub fn yes_ask_cents_f64(&self) -> Option<f64> {
        dollars_to_cents_f64(&self.yes_ask_dollars)
    }

    /// NO ask in ¢ at deci-cent resolution (0-100), or None if unpriced.
    pub fn no_ask_cents_f64(&self) -> Option<f64> {
        dollars_to_cents_f64(&self.no_ask_dollars)
    }

    /// YES ask rounded to whole cents (0-100), or None if unpriced.
    pub fn yes_ask_cents(&self) -> Option<i64> {
        self.yes_ask_cents_f64().map(|c| c.round() as i64)
    }

    /// NO ask rounded to whole cents (0-100), or None if unpriced.
    pub fn no_ask_cents(&self) -> Option<i64> {
        self.no_ask_cents_f64().map(|c| c.round() as i64)
    }

    /// Close time as a unix timestamp (seconds), parsed from `close_time`.
    pub fn close_unix(&self) -> Option<i64> {
        parse_rfc3339_unix(&self.close_time)
    }

    /// Open time as a unix timestamp (seconds), parsed from `open_time`.
    pub fn open_unix(&self) -> Option<i64> {
        parse_rfc3339_unix(&self.open_time)
    }
}

fn parse_rfc3339_unix(s: &Option<String>) -> Option<i64> {
    s.as_ref()
        .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|dt| dt.timestamp())
}

#[derive(Debug, Deserialize)]
struct MarketsResp {
    #[serde(default)]
    markets: Vec<Market>,
    #[serde(default)]
    cursor: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MarketResp {
    market: Market,
}

pub struct Kalshi {
    http: reqwest::Client,
    key_id: Option<String>,
    signing_key: Option<SigningKey<Sha256>>,
}

impl Kalshi {
    /// Public-only client (market data, no orders).
    pub fn public() -> Self {
        Self {
            http: crate::http_client(),
            key_id: None,
            signing_key: None,
        }
    }

    /// Authenticated client for order placement.
    pub fn authenticated(key_id: String, private_key_pem_path: &str) -> Result<Self> {
        let pem = std::fs::read_to_string(private_key_pem_path)
            .with_context(|| format!("reading Kalshi key at {private_key_pem_path}"))?;
        let key = RsaPrivateKey::from_pkcs8_pem(&pem)
            .or_else(|_| RsaPrivateKey::from_pkcs1_pem(&pem))
            .context("parsing Kalshi private key (expected PKCS#8 or PKCS#1 PEM)")?;
        Ok(Self {
            http: crate::http_client(),
            key_id: Some(key_id),
            signing_key: Some(SigningKey::<Sha256>::new(key)),
        })
    }

    fn sign_headers(&self, method: &str, path: &str) -> Result<Vec<(String, String)>> {
        let key_id = self
            .key_id
            .as_ref()
            .ok_or_else(|| anyhow!("no API key configured"))?;
        let sk = self
            .signing_key
            .as_ref()
            .ok_or_else(|| anyhow!("no signing key configured"))?;
        let ts = chrono::Utc::now().timestamp_millis().to_string();
        let msg = format!("{ts}{}{path}", method.to_uppercase());
        let mut rng = rand::thread_rng();
        let sig = sk.sign_with_rng(&mut rng, msg.as_bytes());
        let b64 = base64::engine::general_purpose::STANDARD.encode(sig.to_bytes());
        Ok(vec![
            ("KALSHI-ACCESS-KEY".into(), key_id.clone()),
            ("KALSHI-ACCESS-SIGNATURE".into(), b64),
            ("KALSHI-ACCESS-TIMESTAMP".into(), ts),
        ])
    }

    /// Probe a series with a single (non-paginated) request for up to `limit`
    /// markets. Public, read-only. An empty result usually means the series
    /// ticker is wrong or has no markets in that status. Parsing is delegated
    /// to [`parse_markets`] so it can be unit-tested without the network.
    pub async fn probe_series(
        &self,
        series_ticker: &str,
        status: &str,
        limit: u32,
    ) -> Result<Vec<Market>> {
        let limit = limit.to_string();
        let body = self
            .http
            .get(format!("{}{PREFIX}/markets", api_base()))
            .query(&[
                ("series_ticker", series_ticker),
                ("status", status),
                ("limit", limit.as_str()),
            ])
            .send()
            .await?
            .error_for_status()?
            .text()
            .await?;
        parse_markets(&body)
    }

    /// All markets for a series (paginated). Public.
    pub async fn markets(&self, series_ticker: &str, status: &str) -> Result<Vec<Market>> {
        let mut out = Vec::new();
        let mut cursor: Option<String> = None;
        loop {
            let mut req = self.http.get(format!("{}{PREFIX}/markets", api_base())).query(&[
                ("series_ticker", series_ticker),
                ("status", status),
                ("limit", "1000"),
            ]);
            if let Some(c) = &cursor {
                req = req.query(&[("cursor", c)]);
            }
            let resp: MarketsResp = req.send().await?.error_for_status()?.json().await?;
            let got = resp.markets.len();
            out.extend(resp.markets);
            match resp.cursor {
                Some(c) if got > 0 && !c.is_empty() => cursor = Some(c),
                _ => break,
            }
        }
        Ok(out)
    }

    /// Fetch a single market by ticker (public GET, no auth). The response
    /// carries the authoritative settlement `result` ("yes"/"no" once settled,
    /// empty while open) — the source of truth for the reconcile loop.
    pub async fn market(&self, ticker: &str) -> Result<Market> {
        let url = format!("{}{PREFIX}/markets/{ticker}", api_base());
        let resp: MarketResp = self
            .http
            .get(url)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(resp.market)
    }

    /// Place a taker limit buy via the V2 create-order endpoint
    /// (`POST /trade-api/v2/portfolio/events/orders`). The legacy
    /// `POST /trade-api/v2/portfolio/orders` was retired (410 on prod AND demo;
    /// docs list it deprecated no earlier than 2026-05-06 but it is already dead).
    ///
    /// The call boundary stays strategy-friendly: `side` is "yes"/"no" and
    /// `price_cents` is the whole-cent price (1-99) for THAT side. We translate to
    /// the V2 single-book YES-leg semantics internally.
    ///
    /// V2 SEMANTICS (docs.kalshi.com/api-reference/orders/create-order-v2):
    /// "bid means buy YES, ask means sell YES. Selling YES economically equals
    /// buying NO at 1 - price, but you express it as an ask on the YES book at the
    /// corresponding YES price. For example, buying NO at 0.40 would be posting an
    /// ask at 0.60 on the YES side." So:
    ///   - buy YES @ p¢  -> side "bid",  price = p/100 dollars
    ///   - buy NO  @ p¢  -> side "ask",  price = (100 - p)/100 dollars  (YES price)
    ///
    /// Get this wrong and every NO order is catastrophically mispriced.
    ///
    /// count/price are fixed-point STRINGS ("1.00", "0.6000"). ALL orders are
    /// taker-only: `time_in_force=immediate_or_cancel` +
    /// `self_trade_prevention_type=taker_at_cross` — the exchange fills what it can
    /// against resting liquidity and cancels the remainder itself, natively
    /// enforcing our no-resting-orders doctrine. The 201 response carries
    /// SYNCHRONOUS fill truth (fill_count/remaining_count/average_fill_price/
    /// average_fee_paid/ts_ms) — see [`parse_place_response`]. Signed.
    pub async fn place_limit_buy(
        &self,
        ticker: &str,
        side: &str,
        count: i64,
        price_cents: i64,
        client_order_id: &str,
    ) -> Result<serde_json::Value> {
        let (status, text) = self
            .place_limit_buy_raw(ticker, side, count, price_cents, client_order_id)
            .await?;
        if !(200..300).contains(&status) {
            // Keep the raw body in the error so the lost-ack recovery path
            // (strategy::execute_live) and operators can see WHAT Kalshi said.
            return Err(anyhow!("order placement HTTP {status}: {text}"));
        }
        serde_json::from_str(&text).context("parsing create-order response JSON")
    }

    /// Low-level create-order POST returning `(http_status, raw_body_text)` WITHOUT
    /// treating a non-2xx as an error — so callers can inspect the exact response
    /// (e.g. the duplicate-`client_order_id` demo probe, fix 2b). Signed.
    pub async fn place_limit_buy_raw(
        &self,
        ticker: &str,
        side: &str,
        count: i64,
        price_cents: i64,
        client_order_id: &str,
    ) -> Result<(u16, String)> {
        let path = format!("{PREFIX}/portfolio/events/orders");
        let headers = self.sign_headers("POST", &path)?;
        let mut map = serde_json::Map::new();
        map.insert("ticker".into(), json!(ticker));
        map.insert("side".into(), json!(book_side(side)));
        map.insert("count".into(), json!(count_fp(count)));
        map.insert("price".into(), json!(order_price_dollars(side, price_cents)));
        map.insert("time_in_force".into(), json!("immediate_or_cancel"));
        map.insert(
            "self_trade_prevention_type".into(),
            json!("taker_at_cross"),
        );
        map.insert("client_order_id".into(), json!(client_order_id));
        let body = serde_json::Value::Object(map);
        let mut req = self.http.post(format!("{}{path}", api_base())).json(&body);
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status().as_u16();
        let text = resp.text().await?;
        Ok((status, text))
    }

    /// Account cash balance in cents. Signed.
    pub async fn balance_cents(&self) -> Result<i64> {
        let path = format!("{PREFIX}/portfolio/balance");
        let headers = self.sign_headers("GET", &path)?;
        let mut req = self.http.get(format!("{}{path}", api_base()));
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let body = req.send().await?.error_for_status()?.text().await?;
        parse_balance(&body)
    }

    /// Raw portfolio positions (signed) — used to confirm a fill in the self-test.
    pub async fn positions(&self) -> Result<serde_json::Value> {
        let path = format!("{PREFIX}/portfolio/positions");
        let headers = self.sign_headers("GET", &path)?;
        let mut req = self.http.get(format!("{}{path}", api_base()));
        for (k, v) in headers {
            req = req.header(k, v);
        }
        Ok(req.send().await?.error_for_status()?.json().await?)
    }

    /// Raw fills for a ticker (signed). Used to verify what ACTUALLY filled after
    /// an order is accepted — accepted ≠ filled (EXECUTION TRUTH, redirect
    /// 2026-07-23). Parsing lives in [`parse_fills`] (tolerant, unit-tested);
    /// callers keep the raw JSON in their records so week-1 validates the schema.
    pub async fn fills(&self, ticker: &str) -> Result<serde_json::Value> {
        // Kalshi signs `timestamp + METHOD + path` over the PATH ONLY — the query
        // string must NOT be in the signed message (including it returns 401).
        // Sign the bare path; attach the filters via reqwest's query builder.
        let path = format!("{PREFIX}/portfolio/fills");
        let headers = self.sign_headers("GET", &path)?;
        let mut req = self
            .http
            .get(format!("{}{path}", api_base()))
            .query(&[("ticker", ticker), ("limit", "200")]);
        for (k, v) in headers {
            req = req.header(k, v);
        }
        Ok(req.send().await?.error_for_status()?.json().await?)
    }

    /// Cancel an order via the V2 endpoint
    /// (`DELETE /trade-api/v2/portfolio/events/orders/{order_id}`; the legacy
    /// `/portfolio/orders/{id}` path shares the retired-order deprecation). With
    /// IOC taker orders the exchange already cancels any remainder for us, so this
    /// is now a belt-and-suspenders cleanup rather than the primary mechanism.
    /// Signed.
    pub async fn cancel_order(&self, order_id: &str) -> Result<serde_json::Value> {
        let path = format!("{PREFIX}/portfolio/events/orders/{order_id}");
        let headers = self.sign_headers("DELETE", &path)?;
        let mut req = self.http.delete(format!("{}{path}", api_base()));
        for (k, v) in headers {
            req = req.header(k, v);
        }
        Ok(req.send().await?.error_for_status()?.json().await?)
    }

    /// Order book for a market (public). Captured as the decision snapshot at
    /// every signal moment (DATA CAPTURE, redirect 2026-07-23).
    pub async fn orderbook(&self, ticker: &str) -> Result<serde_json::Value> {
        let url = format!("{}{PREFIX}/markets/{ticker}/orderbook", api_base());
        Ok(self
            .http
            .get(url)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?)
    }
}

/// One parsed fill relevant to an order.
#[derive(Debug, Clone, PartialEq)]
pub struct ParsedFill {
    pub count: i64,
    /// Price paid for OUR side, in whole cents.
    pub price_cents: i64,
    /// Fill creation time in unix ms (None if unparseable).
    pub ts_ms: Option<i64>,
}

// ---------------------------------------------------------------------------
// V2 create-order translation (call boundary: side "yes"/"no" + whole cents).
// ---------------------------------------------------------------------------

/// Map our side ("yes"/"no") to the V2 single-book YES-leg `side`.
/// bid = buy YES; ask = sell YES = buy NO. (create-order-v2 docs.)
pub fn book_side(side: &str) -> &'static str {
    if side.eq_ignore_ascii_case("no") {
        "ask"
    } else {
        "bid"
    }
}

/// Count as a fixed-point string ("1" -> "1.00"), per FixedPointCount.
pub fn count_fp(count: i64) -> String {
    format!("{count}.00")
}

/// The YES-book limit price (fixed-point dollars string) for buying `side` at
/// `price_cents`. YES: p/100. NO: (100 - p)/100 — you post an ASK at the
/// corresponding YES price (buy NO @ 40¢ -> ask @ "0.6000"). Getting this
/// inverted mis-prices every NO order, so it is unit-tested below.
pub fn order_price_dollars(side: &str, price_cents: i64) -> String {
    let yes_cents = if side.eq_ignore_ascii_case("no") {
        100 - price_cents
    } else {
        price_cents
    };
    format!("{:.4}", yes_cents as f64 / 100.0)
}

/// Translate a YES-book fill price (dollars) back into OUR side's whole cents.
/// YES: round(p*100). NO: 100 - round(p*100) (inverse of [`order_price_dollars`]).
fn yes_dollars_to_side_cents(side: &str, yes_dollars: f64) -> i64 {
    let yes_cents = (yes_dollars * 100.0).round() as i64;
    if side.eq_ignore_ascii_case("no") {
        100 - yes_cents
    } else {
        yes_cents
    }
}

/// Synchronous fill truth parsed from the 201 create-order-v2 response — the
/// PRIMARY record of what happened (fills poll is only a cross-check now).
#[derive(Debug, Clone, PartialEq)]
pub struct PlacedOrder {
    pub order_id: Option<String>,
    /// Contracts filled immediately (0 for an IOC that crossed nothing).
    pub fill_count: i64,
    /// Unfilled remainder — the exchange has already canceled it for an IOC.
    pub remaining_count: i64,
    /// VWAP fill price in OUR side's whole cents (None when fill_count == 0).
    pub fill_price_cents: Option<i64>,
    /// ACTUAL total fee paid in cents (average_fee_paid × fill_count). Gold for
    /// the mechanics week — recorded alongside our own estimate. None if unfilled.
    /// Total fee actually charged, in CENTS with sub-cent resolution
    /// (exchange reports per-contract dollars like "0.0046" = 0.46c).
    pub actual_fee_cents: Option<f64>,
    /// Matching-engine timestamp (unix ms) — used as ts_ack / ts_fill.
    pub ts_ms: Option<i64>,
}

/// Parse the create-order-v2 201 response. `side` ("yes"/"no") is needed to map
/// the YES-book `average_fill_price` back to our side's cents. Tolerant of the
/// response being wrapped in an `{"order": {...}}` envelope vs flat.
pub fn parse_place_response(resp: &serde_json::Value, side: &str) -> PlacedOrder {
    // Fields may sit at the top level or under an "order" envelope.
    let get = |k: &str| resp.get(k).or_else(|| resp.get("order").and_then(|o| o.get(k)));

    let order_id = get("order_id")
        .or_else(|| resp.get("order").and_then(|o| o.get("id")))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    let fill_count = get("fill_count").and_then(count_to_i64).unwrap_or(0);
    let remaining_count = get("remaining_count").and_then(count_to_i64).unwrap_or(0);

    let (fill_price_cents, actual_fee_cents) = if fill_count > 0 {
        let px = get("average_fill_price")
            .and_then(dollars_f64)
            .map(|d| yes_dollars_to_side_cents(side, d));
        let fee = get("average_fee_paid")
            .and_then(dollars_f64)
            // average_fee_paid is per-contract dollars -> total cents, KEEPING
            // sub-cent resolution (demo-observed: 1ct @ 7c fill -> "0.0046" = 0.46c;
            // an i64 round would report 0).
            .map(|per| per * fill_count as f64 * 100.0);
        (px, fee)
    } else {
        (None, None)
    };

    let ts_ms = get("ts_ms").and_then(|v| v.as_i64()).or_else(|| {
        get("ts_ms")
            .and_then(|v| v.as_str())
            .and_then(|s| s.parse::<i64>().ok())
    });

    PlacedOrder {
        order_id,
        fill_count,
        remaining_count,
        fill_price_cents,
        actual_fee_cents,
        ts_ms,
    }
}

/// A fixed-point-dollars field ("0.6000" or 0.6) -> f64 dollars.
fn dollars_f64(v: &serde_json::Value) -> Option<f64> {
    if let Some(n) = v.as_f64() {
        return Some(n);
    }
    v.as_str().and_then(|s| s.parse::<f64>().ok())
}

/// Extract the order id from a place-order response, tolerating schema variants
/// (`{"order":{"order_id":..}}`, `{"order":{"id":..}}`, `{"order_id":..}`).
pub fn parse_order_id(resp: &serde_json::Value) -> Option<String> {
    let cands = [
        resp.get("order").and_then(|o| o.get("order_id")),
        resp.get("order").and_then(|o| o.get("id")),
        resp.get("order_id"),
    ];
    cands
        .into_iter()
        .flatten()
        .find_map(|v| v.as_str().map(|s| s.to_string()))
}

/// Price field → whole cents, tolerating integer-cents (e.g. 44), float-dollars
/// (0.44), or string-dollars ("0.44"). Values < 1.0 are dollars (a real fill
/// price is 1–99¢, i.e. ≥1 in cents form).
fn price_to_cents(v: &serde_json::Value) -> Option<i64> {
    if let Some(n) = v.as_f64() {
        return Some(if n < 1.0 {
            (n * 100.0).round() as i64
        } else {
            n.round() as i64
        });
    }
    if let Some(s) = v.as_str() {
        let n: f64 = s.parse().ok()?;
        return Some(if n < 1.0 {
            (n * 100.0).round() as i64
        } else {
            n.round() as i64
        });
    }
    None
}

fn count_to_i64(v: &serde_json::Value) -> Option<i64> {
    if let Some(n) = v.as_i64() {
        return Some(n);
    }
    if let Some(f) = v.as_f64() {
        return Some(f.round() as i64);
    }
    v.as_str()
        .and_then(|s| s.parse::<f64>().ok())
        .map(|f| f.round() as i64)
}

/// Parse a `/portfolio/fills` response into the fills belonging to one order.
/// Match by `order_id` when available; otherwise fall back to (side matches AND
/// created_time ≥ since_ms) — our orders are the only ones we place on a ticker.
/// Tolerant of cents-vs-dollars and numeric-vs-string field encodings.
pub fn parse_fills(
    body: &serde_json::Value,
    order_id: Option<&str>,
    side: &str,
    since_ms: i64,
) -> Vec<ParsedFill> {
    let empty = vec![];
    let fills = body
        .get("fills")
        .and_then(|f| f.as_array())
        .unwrap_or(&empty);
    let mut out = Vec::new();
    for f in fills {
        // order match
        let matches = match order_id {
            Some(id) => f.get("order_id").and_then(|v| v.as_str()) == Some(id),
            None => {
                let side_ok = f.get("side").and_then(|v| v.as_str()) == Some(side);
                let ts_ok = fill_ts_ms(f).is_none_or(|t| t >= since_ms - 2_000);
                side_ok && ts_ok
            }
        };
        if !matches {
            continue;
        }
        let count = f
            .get("count")
            .or_else(|| f.get("count_fp"))
            .and_then(count_to_i64)
            .unwrap_or(0);
        if count <= 0 {
            continue;
        }
        // Our side's price: <side>_price, falling back to <side>_price_dollars.
        let price_key = format!("{side}_price");
        let price = f.get(&price_key).and_then(price_to_cents).or_else(|| {
            f.get(format!("{price_key}_dollars").as_str())
                .and_then(price_to_cents)
        });
        let Some(price_cents) = price else { continue };
        out.push(ParsedFill {
            count,
            price_cents,
            ts_ms: fill_ts_ms(f),
        });
    }
    out
}

fn fill_ts_ms(f: &serde_json::Value) -> Option<i64> {
    f.get("created_time")
        .and_then(|v| v.as_str())
        .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|dt| dt.timestamp_millis())
}

/// Summarize fills: (total filled count, weighted-avg price in cents, latest ts_ms).
pub fn fills_summary(fills: &[ParsedFill]) -> (i64, Option<i64>, Option<i64>) {
    let total: i64 = fills.iter().map(|f| f.count).sum();
    if total == 0 {
        return (0, None, None);
    }
    let weighted: i64 = fills.iter().map(|f| f.count * f.price_cents).sum();
    let avg = (weighted as f64 / total as f64).round() as i64;
    let ts = fills.iter().filter_map(|f| f.ts_ms).max();
    (total, Some(avg), ts)
}

/// One open position as the EXCHANGE sees it (from `/portfolio/positions`),
/// normalized to our side/count/entry vocabulary. Kalshi's `market_positions`
/// entries carry a signed net `position` (+ = net long YES, − = net long NO) and
/// a `market_exposure` = current cost basis in CENTS of the open side. Zero-net
/// rows (fully closed/settled) are dropped.
#[derive(Debug, Clone, PartialEq)]
pub struct ExchangePosition {
    pub ticker: String,
    pub side: crate::risk::Side,
    /// Absolute contract count held.
    pub count: i64,
    /// Average entry price per contract in whole cents, if derivable from
    /// `market_exposure` (else None → the caller uses a conservative worst-case).
    pub entry_cents: Option<i64>,
}

/// Parse a `/portfolio/positions` response into the currently-held positions.
/// Tolerant of numeric-vs-string encodings. `market_exposure` is total cents at
/// risk for the row; dividing by the contract count yields per-contract entry.
pub fn parse_positions(body: &serde_json::Value) -> Vec<ExchangePosition> {
    use crate::risk::Side;
    let empty = vec![];
    let rows = body
        .get("market_positions")
        .and_then(|v| v.as_array())
        .unwrap_or(&empty);
    let mut out = Vec::new();
    for r in rows {
        let ticker = match r.get("ticker").and_then(|v| v.as_str()) {
            Some(t) => t.to_string(),
            None => continue,
        };
        let net = r.get("position").and_then(count_to_i64).unwrap_or(0);
        if net == 0 {
            continue; // flat / settled row
        }
        let side = if net > 0 { Side::Yes } else { Side::No };
        let count = net.abs();
        // Cost basis: Kalshi is migrating fixed-point fields to `_dollars`
        // strings ("3.96"). Prefer `market_exposure_dollars` (dollars -> cents),
        // fall back to legacy integer-cents `market_exposure`. Reading the
        // dollar string through the int path would book "3.96" as 4 CENTS —
        // a ~100x cost-basis error feeding orphan adoption + the divergence
        // breaker (found via openpx comparative review, 2026-07-23).
        let exposure = r
            .get("market_exposure_dollars")
            .and_then(dollars_f64)
            .map(|d| (d * 100.0).round() as i64)
            .or_else(|| r.get("market_exposure").and_then(count_to_i64))
            .filter(|&e| e > 0);
        let entry_cents = exposure.map(|e| ((e as f64 / count as f64).round() as i64).clamp(1, 99));
        out.push(ExchangePosition {
            ticker,
            side,
            count,
            entry_cents,
        });
    }
    out
}

/// Parse `/portfolio/balance` into cents. Kalshi returns `{"balance": <int cents>}`.
pub fn parse_balance(body: &str) -> Result<i64> {
    let v: serde_json::Value = serde_json::from_str(body).context("parsing balance")?;
    v.get("balance")
        .and_then(|b| b.as_i64())
        .context("balance response missing integer `balance` field")
}

/// Parse a `/markets` response body into its market list. Pure and network-free
/// so probe/parse logic is unit-testable. A non-empty result confirms the
/// series ticker resolved to live markets.
pub fn parse_markets(body: &str) -> Result<Vec<Market>> {
    let resp: MarketsResp = serde_json::from_str(body).context("parsing markets response")?;
    Ok(resp.markets)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn book_side_maps_yes_bid_no_ask() {
        assert_eq!(book_side("yes"), "bid");
        assert_eq!(book_side("YES"), "bid");
        assert_eq!(book_side("no"), "ask");
        assert_eq!(book_side("NO"), "ask");
        // anything not "no" is a YES bid (defensive default)
        assert_eq!(book_side("weird"), "bid");
    }

    #[test]
    fn count_fp_is_two_decimals() {
        assert_eq!(count_fp(1), "1.00");
        assert_eq!(count_fp(52), "52.00");
    }

    #[test]
    fn order_price_yes_is_identity_no_is_complement() {
        // YES @ 40¢ -> post bid at 0.40 on the YES book.
        assert_eq!(order_price_dollars("yes", 40), "0.4000");
        assert_eq!(order_price_dollars("yes", 2), "0.0200");
        assert_eq!(order_price_dollars("yes", 99), "0.9900");
        // NO @ 40¢ -> post ASK at 0.60 on the YES book (docs example).
        assert_eq!(order_price_dollars("no", 40), "0.6000");
        assert_eq!(order_price_dollars("no", 2), "0.9800");
        assert_eq!(order_price_dollars("no", 95), "0.0500");
    }

    #[test]
    fn fill_price_translation_round_trips_our_side() {
        // YES fill at YES-book 0.4100 -> 41¢ for us.
        assert_eq!(yes_dollars_to_side_cents("yes", 0.41), 41);
        // NO order filled at YES-book 0.6200 -> our NO cost is 38¢ (a BETTER
        // fill than the 40¢ limit, exactly as the ask semantics intend).
        assert_eq!(yes_dollars_to_side_cents("no", 0.62), 38);
    }

    #[test]
    fn parse_place_response_filled_yes() {
        let resp = serde_json::json!({
            "order_id": "ord_1",
            "fill_count": "2.00",
            "remaining_count": "0.00",
            "average_fill_price": "0.4100",
            "average_fee_paid": "0.0145",
            "ts_ms": 1_752_000_000_123i64
        });
        let p = parse_place_response(&resp, "yes");
        assert_eq!(p.order_id.as_deref(), Some("ord_1"));
        assert_eq!(p.fill_count, 2);
        assert_eq!(p.remaining_count, 0);
        assert_eq!(p.fill_price_cents, Some(41));
        // 0.0145 * 2 contracts = 0.029 dollars = 2.9¢ — kept at sub-cent truth.
        assert!((p.actual_fee_cents.unwrap() - 2.9).abs() < 1e-9);
        assert_eq!(p.ts_ms, Some(1_752_000_000_123));
    }

    #[test]
    fn parse_place_response_filled_no_translates_price() {
        // NO order: response average_fill_price is the YES-book price 0.6200.
        let resp = serde_json::json!({
            "order": {
                "order_id": "ord_2",
                "fill_count": "5.00",
                "remaining_count": "0.00",
                "average_fill_price": "0.6200",
                "average_fee_paid": "0.0100",
                "ts_ms": 1_752_000_000_999i64
            }
        });
        let p = parse_place_response(&resp, "no");
        assert_eq!(p.order_id.as_deref(), Some("ord_2"));
        assert_eq!(p.fill_count, 5);
        assert_eq!(p.fill_price_cents, Some(38)); // 100 - 62
        assert!((p.actual_fee_cents.unwrap() - 5.0).abs() < 1e-9); // 0.01 * 5 = 0.05 -> 5¢
    }

    #[test]
    fn parse_place_response_ioc_no_fill_empty_book() {
        // IOC that crossed nothing: 201 with fill_count 0, no price/fee fields.
        let resp = serde_json::json!({
            "order_id": "ord_3",
            "fill_count": "0.00",
            "remaining_count": "1.00",
            "ts_ms": 1_752_000_001_000i64
        });
        let p = parse_place_response(&resp, "yes");
        assert_eq!(p.fill_count, 0);
        assert_eq!(p.remaining_count, 1);
        assert_eq!(p.fill_price_cents, None);
        assert_eq!(p.actual_fee_cents, None);
        assert_eq!(p.order_id.as_deref(), Some("ord_3"));
    }

    #[test]
    fn parse_order_id_tolerates_variants() {
        let a = serde_json::json!({"order": {"order_id": "abc"}});
        let b = serde_json::json!({"order": {"id": "def"}});
        let c = serde_json::json!({"order_id": "ghi"});
        let d = serde_json::json!({"something": 1});
        assert_eq!(parse_order_id(&a).as_deref(), Some("abc"));
        assert_eq!(parse_order_id(&b).as_deref(), Some("def"));
        assert_eq!(parse_order_id(&c).as_deref(), Some("ghi"));
        assert_eq!(parse_order_id(&d), None);
    }

    #[test]
    fn parse_fills_by_order_id_cents_and_dollars() {
        // Two fills for our order (one integer-cents, one string-dollars), one
        // foreign fill that must be excluded.
        let body = serde_json::json!({"fills": [
            {"order_id": "A", "side": "no", "count": 5, "no_price": 44,
             "created_time": "2026-07-23T18:00:01Z"},
            {"order_id": "A", "side": "no", "count": 4, "no_price": "0.43",
             "created_time": "2026-07-23T18:00:02Z"},
            {"order_id": "B", "side": "no", "count": 9, "no_price": 44},
        ]});
        let fills = parse_fills(&body, Some("A"), "no", 0);
        assert_eq!(fills.len(), 2);
        let (total, avg, ts) = fills_summary(&fills);
        assert_eq!(total, 9);
        // 5*44 + 4*43 = 392 / 9 = 43.56 -> 44 rounded
        assert_eq!(avg, Some(44));
        assert!(ts.is_some());
    }

    #[test]
    fn parse_fills_fallback_matches_side_and_time() {
        let body = serde_json::json!({"fills": [
            {"side": "yes", "count": 9, "yes_price": 41,
             "created_time": "2026-07-23T18:00:05Z"},
            {"side": "no", "count": 3, "no_price": 60,
             "created_time": "2026-07-23T18:00:05Z"},
            {"side": "yes", "count": 2, "yes_price": 40,
             "created_time": "2026-07-23T17:00:00Z"}, // too old — excluded
        ]});
        let since = chrono::DateTime::parse_from_rfc3339("2026-07-23T18:00:00Z")
            .unwrap()
            .timestamp_millis();
        let fills = parse_fills(&body, None, "yes", since);
        assert_eq!(fills.len(), 1);
        assert_eq!(fills[0].count, 9);
        assert_eq!(fills[0].price_cents, 41);
    }

    #[test]
    fn fills_summary_empty_is_zero() {
        assert_eq!(fills_summary(&[]), (0, None, None));
    }

    #[test]
    fn parse_positions_reads_side_count_and_entry() {
        use crate::risk::Side;
        let body = serde_json::json!({
            "market_positions": [
                // net long YES 9 contracts, $3.96 exposure -> 44c entry
                {"ticker": "KXBTC15M-A", "position": 9, "market_exposure": 396},
                // net long NO 5 contracts, $2.00 exposure -> 40c entry
                {"ticker": "KXETH15M-B", "position": -5, "market_exposure": 200},
                // flat row dropped
                {"ticker": "KXBTC15M-C", "position": 0, "market_exposure": 0},
                // no exposure -> entry None
                {"ticker": "KXBTC15M-D", "position": 2},
            ]
        });
        let ps = parse_positions(&body);
        assert_eq!(ps.len(), 3);
        assert_eq!(ps[0].ticker, "KXBTC15M-A");
        assert_eq!(ps[0].side, Side::Yes);
        assert_eq!(ps[0].count, 9);
        assert_eq!(ps[0].entry_cents, Some(44));
        assert_eq!(ps[1].side, Side::No);
        assert_eq!(ps[1].count, 5);
        assert_eq!(ps[1].entry_cents, Some(40));
        assert_eq!(ps[2].ticker, "KXBTC15M-D");
        assert_eq!(ps[2].entry_cents, None);
    }

    #[test]
    fn parse_positions_prefers_dollars_fixed_point() {
        // Kalshi fixed-point migration: `market_exposure_dollars: "3.96"` must
        // read as 396 cents, NOT fall through the int path as 4 cents (~100x
        // cost-basis error). Legacy int rows must still work.
        let body = serde_json::json!({"market_positions": [
            {"ticker": "A", "position": "9.00", "market_exposure_dollars": "3.96"},
            {"ticker": "B", "position": -2, "market_exposure": 146}
        ]});
        let ps = parse_positions(&body);
        assert_eq!(ps.len(), 2);
        assert_eq!(ps[0].entry_cents, Some(44)); // 396c / 9 contracts
        assert_eq!(ps[1].entry_cents, Some(73)); // legacy int path intact
    }

    #[test]
    fn parse_positions_empty_is_empty() {
        assert!(parse_positions(&serde_json::json!({})).is_empty());
        assert!(parse_positions(&serde_json::json!({"market_positions": []})).is_empty());
    }

    #[test]
    fn parse_balance_reads_cents() {
        assert_eq!(parse_balance(r#"{"balance": 4237}"#).unwrap(), 4237);
        assert!(parse_balance(r#"{"nope": 1}"#).is_err());
        assert!(parse_balance("not json").is_err());
    }

    #[test]
    fn parse_markets_detects_series_and_reads_sample() {
        let body = r#"{
            "markets": [
                {
                    "ticker": "KXHIGHMIA-26JUL21-B92.5",
                    "floor_strike": 91.0,
                    "cap_strike": 94.0,
                    "yes_ask_dollars": "0.42",
                    "yes_sub_title": "91° to 94°, Miami Intl (MIA)"
                }
            ],
            "cursor": ""
        }"#;
        let markets = parse_markets(body).unwrap();
        assert_eq!(markets.len(), 1);
        assert_eq!(markets[0].ticker, "KXHIGHMIA-26JUL21-B92.5");
        assert_eq!(markets[0].yes_ask_cents(), Some(42));
        assert_eq!(
            markets[0].yes_sub_title.as_deref(),
            Some("91° to 94°, Miami Intl (MIA)")
        );
    }

    #[test]
    fn parse_markets_empty_means_series_absent() {
        let markets = parse_markets(r#"{"markets": [], "cursor": ""}"#).unwrap();
        assert!(markets.is_empty());
    }

    /// EMPIRICAL duplicate-`client_order_id` probe (fix 2b). Places the SAME 1ct
    /// 2¢ IOC order twice with a FIXED client_order_id against an empty demo book,
    /// printing BOTH raw (status, body) responses — settling whether Kalshi
    /// rejects a duplicate coid (error, safe) or echoes the original order (200,
    /// the dangerous branch that would let a re-fire double-book P&L).
    ///
    /// Ignored (needs demo keys + network). Run with:
    ///   KALSHI_API_BASE=https://demo-api.kalshi.co \
    ///   KALSHI_API_KEY_ID=<id> KALSHI_PRIVATE_KEY_PATH=secrets/Demo.txt \
    ///   NESTOR_TEST_TICKER=<open-demo-ticker> \
    ///   cargo test -p engine demo_duplicate_coid -- --ignored --nocapture
    #[tokio::test]
    #[ignore]
    async fn demo_duplicate_coid_behavior() {
        let key_id = std::env::var("KALSHI_API_KEY_ID").expect("KALSHI_API_KEY_ID");
        let key_path = std::env::var("KALSHI_PRIVATE_KEY_PATH").expect("KALSHI_PRIVATE_KEY_PATH");
        let ticker = std::env::var("NESTOR_TEST_TICKER").expect("NESTOR_TEST_TICKER");
        let k = Kalshi::authenticated(key_id, &key_path).unwrap();
        // FIXED coid so both requests are byte-identical.
        let coid = "nestor-dup-probe-fixed-0001";

        let (s1, b1) = k
            .place_limit_buy_raw(&ticker, "yes", 1, 2, coid)
            .await
            .expect("first POST");
        println!("=== DUP-COID PROBE first  === HTTP {s1}\n{b1}");
        let (s2, b2) = k
            .place_limit_buy_raw(&ticker, "yes", 1, 2, coid)
            .await
            .expect("second POST");
        println!("=== DUP-COID PROBE second === HTTP {s2}\n{b2}");
        println!(
            "=== VERDICT === first={s1} second={s2} — {}",
            if s2 >= 400 {
                "duplicate REJECTED (safe: a re-fire cannot double-book)"
            } else {
                "duplicate ECHOED/ACCEPTED (verify order_id identity before trusting re-fires)"
            }
        );
    }
}
