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

const BASE: &str = "https://api.elections.kalshi.com";
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
            .get(format!("{BASE}{PREFIX}/markets"))
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
            let mut req = self.http.get(format!("{BASE}{PREFIX}/markets")).query(&[
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
        let url = format!("{BASE}{PREFIX}/markets/{ticker}");
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

    /// Place a limit buy. `price_cents` = the price in cents (1-99) for the chosen
    /// `side` — placed as `yes_price` for a YES order, `no_price` for a NO order
    /// (Kalshi keys the limit to the side being bought). Signed.
    pub async fn place_limit_buy(
        &self,
        ticker: &str,
        side: &str,
        count: i64,
        price_cents: i64,
        client_order_id: &str,
    ) -> Result<serde_json::Value> {
        let path = format!("{PREFIX}/portfolio/orders");
        let headers = self.sign_headers("POST", &path)?;
        let price_key = if side == "no" {
            "no_price"
        } else {
            "yes_price"
        };
        let mut map = serde_json::Map::new();
        map.insert("ticker".into(), json!(ticker));
        map.insert("action".into(), json!("buy"));
        map.insert("side".into(), json!(side));
        map.insert("type".into(), json!("limit"));
        map.insert("count".into(), json!(count));
        map.insert(price_key.into(), json!(price_cents));
        map.insert("client_order_id".into(), json!(client_order_id));
        let body = serde_json::Value::Object(map);
        let mut req = self.http.post(format!("{BASE}{path}")).json(&body);
        for (k, v) in headers {
            req = req.header(k, v);
        }
        Ok(req.send().await?.error_for_status()?.json().await?)
    }

    /// Account cash balance in cents. Signed.
    pub async fn balance_cents(&self) -> Result<i64> {
        let path = format!("{PREFIX}/portfolio/balance");
        let headers = self.sign_headers("GET", &path)?;
        let mut req = self.http.get(format!("{BASE}{path}"));
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
        let mut req = self.http.get(format!("{BASE}{path}"));
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
        let path = format!("{PREFIX}/portfolio/fills?ticker={ticker}&limit=200");
        let headers = self.sign_headers("GET", &path)?;
        let mut req = self.http.get(format!("{BASE}{path}"));
        for (k, v) in headers {
            req = req.header(k, v);
        }
        Ok(req.send().await?.error_for_status()?.json().await?)
    }

    /// Cancel a resting order (signed). Mandatory cleanup for any unfilled
    /// remainder — a stranded resting order violates the taker-only doctrine.
    pub async fn cancel_order(&self, order_id: &str) -> Result<serde_json::Value> {
        let path = format!("{PREFIX}/portfolio/orders/{order_id}");
        let headers = self.sign_headers("DELETE", &path)?;
        let mut req = self.http.delete(format!("{BASE}{path}"));
        for (k, v) in headers {
            req = req.header(k, v);
        }
        Ok(req.send().await?.error_for_status()?.json().await?)
    }

    /// Order book for a market (public). Captured as the decision snapshot at
    /// every signal moment (DATA CAPTURE, redirect 2026-07-23).
    pub async fn orderbook(&self, ticker: &str) -> Result<serde_json::Value> {
        let url = format!("{BASE}{PREFIX}/markets/{ticker}/orderbook");
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
}
