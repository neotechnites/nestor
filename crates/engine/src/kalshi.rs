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
/// Websocket v2 path (the string that is RSA-signed for the ws handshake, and the
/// suffix of [`ws_url`]). Distinct from [`PREFIX`] — the ws lives under `/ws/`.
const WS_PATH: &str = "/trade-api/ws/v2";

/// Websocket URL derived from [`api_base`]: same host (so KALSHI_API_BASE selects
/// prod vs demo exactly as REST does), https->wss, plus [`WS_PATH`]. Prod
/// `api.elections.kalshi.com` empirically serves the ws (OSS note 26; demo mirror
/// `demo-api.kalshi.co`). Verify the handshake on demo (`ws::demo_ws_connect_and_book`).
pub fn ws_url() -> String {
    let base = api_base();
    let ws = base
        .strip_prefix("https://")
        .map(|h| format!("wss://{h}"))
        .or_else(|| base.strip_prefix("http://").map(|h| format!("ws://{h}")))
        .unwrap_or_else(|| base.clone());
    format!("{ws}{WS_PATH}")
}

/// Owned, cloneable signer for the websocket handshake. Carries just the key id +
/// signing key so the ws maintainer (a spawned task that outlives any borrow of
/// [`Kalshi`]) can re-sign fresh auth headers on every reconnect. Same RSA-PSS
/// scheme and same 3 `KALSHI-ACCESS-*` headers as the REST [`Kalshi::sign_headers`],
/// signing `{ts_ms}GET/trade-api/ws/v2` (path only — no query, per the bare-path
/// signing rule the REST client already relies on).
#[derive(Clone)]
pub struct WsAuth {
    key_id: String,
    signing_key: SigningKey<Sha256>,
}

impl WsAuth {
    /// Freshly-signed handshake headers (regenerate per (re)connect so the
    /// timestamp stays inside Kalshi's replay window).
    pub fn headers(&self) -> Vec<(String, String)> {
        let ts = chrono::Utc::now().timestamp_millis().to_string();
        let msg = format!("{ts}GET{WS_PATH}");
        let mut rng = rand::thread_rng();
        let sig = self.signing_key.sign_with_rng(&mut rng, msg.as_bytes());
        let b64 = base64::engine::general_purpose::STANDARD.encode(sig.to_bytes());
        vec![
            ("KALSHI-ACCESS-KEY".into(), self.key_id.clone()),
            ("KALSHI-ACCESS-SIGNATURE".into(), b64),
            ("KALSHI-ACCESS-TIMESTAMP".into(), ts),
        ]
    }
}

/// Read a response header as an owned String (case-insensitive name lookup is
/// handled by reqwest's `HeaderMap`).
fn header(resp: &reqwest::Response, name: &str) -> Option<String> {
    resp.headers()
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
}

/// A parsed Kalshi error body (item 5). Kalshi returns errors as
/// `{"error":{"code","message"}}`, occasionally flat (`{"code","message"}`),
/// `{"message":..}`, or `{"error":"..."}`. Non-JSON leaves both fields None.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ApiError {
    pub code: Option<String>,
    pub message: Option<String>,
}

fn str_field(v: &serde_json::Value, k: &str) -> Option<String> {
    v.get(k).and_then(|x| x.as_str()).map(|s| s.to_string())
}

/// Parse a Kalshi error body into `{code, message}`, tolerating the nested and
/// flat encodings. Pure + unit-tested; used for logging AND branching (e.g. the
/// benign-409 `order_already_exists` classification in strategy::execute_live).
pub fn parse_api_error(body: &str) -> ApiError {
    let Ok(v) = serde_json::from_str::<serde_json::Value>(body) else {
        return ApiError::default();
    };
    match v.get("error") {
        Some(e @ serde_json::Value::Object(_)) => ApiError {
            code: str_field(e, "code"),
            message: str_field(e, "message"),
        },
        Some(serde_json::Value::String(s)) => ApiError {
            code: None,
            message: Some(s.clone()),
        },
        // flat {"code","message"} / {"message"} at the top level
        _ => ApiError {
            code: str_field(&v, "code"),
            message: str_field(&v, "message"),
        },
    }
}

/// Consume a response into its body text, turning a non-2xx into a rich anyhow
/// error carrying: the status as an "HTTP <code>" token (so `net::http_status`
/// recovers it for backoff), the parsed error `code`, the `x-request-id` (support
/// forensics, item 5), an already-resolved `retry-after-secs` when the 429 sent a
/// `Retry-After` (item 3), and the RAW body. Signed AND public calls share it.
async fn text_or_error(resp: reqwest::Response, ctx: &str) -> Result<String> {
    let status = resp.status().as_u16();
    let reqid = header(&resp, "x-request-id").or_else(|| header(&resp, "request-id"));
    let retry_after = header(&resp, "retry-after");
    let body = resp.text().await?;
    if (200..300).contains(&status) {
        return Ok(body);
    }
    let api = parse_api_error(&body);
    let mut msg = format!("{ctx} HTTP {status}");
    if let Some(code) = api.code.as_deref() {
        msg.push_str(&format!(" code={code}"));
    }
    if let Some(r) = reqid.as_deref() {
        msg.push_str(&format!(" request-id={r}"));
    }
    if let Some(ra) = retry_after.as_deref() {
        if let Some(secs) = crate::net::parse_retry_after(ra, chrono::Utc::now()) {
            msg.push_str(&format!(" retry-after-secs={secs}"));
        }
    }
    msg.push_str(&format!(": {body}"));
    Err(anyhow!(msg))
}

/// Parse an HTTP `Date` header (RFC 7231 IMF-fixdate, e.g.
/// "Sun, 06 Nov 1994 08:49:37 GMT") to unix seconds. Pure + unit-tested (item 7).
pub fn parse_http_date_unix(s: &str) -> Option<i64> {
    chrono::DateTime::parse_from_rfc2822(s.trim())
        .ok()
        .map(|dt| dt.timestamp())
}

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
    /// Lifecycle status. Kalshi progresses `active` → `closed` → `determined` →
    /// `finalized`; both `determined` and `finalized` mean the outcome is known
    /// (settlement gate, item 6). Absent on some payloads → treated as unknown.
    #[serde(default)]
    pub status: Option<String>,
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

    /// A cloneable websocket signer, or None on a public (unauthenticated)
    /// client. The ws maintainer uses it to re-sign the handshake per reconnect.
    pub fn ws_auth(&self) -> Option<WsAuth> {
        match (&self.key_id, &self.signing_key) {
            (Some(k), Some(s)) => Some(WsAuth {
                key_id: k.clone(),
                signing_key: s.clone(),
            }),
            _ => None,
        }
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
        let resp = self
            .http
            .get(format!("{}{PREFIX}/markets", api_base()))
            .query(&[
                ("series_ticker", series_ticker),
                ("status", status),
                ("limit", limit.as_str()),
            ])
            .send()
            .await?;
        let body = text_or_error(resp, "probe_series markets").await?;
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
            let text = text_or_error(req.send().await?, "markets").await?;
            let resp: MarketsResp =
                serde_json::from_str(&text).context("parsing markets response")?;
            let got = resp.markets.len();
            out.extend(resp.markets);
            match resp.cursor {
                Some(c) if got > 0 && !c.is_empty() => cursor = Some(c),
                _ => break,
            }
        }
        Ok(out)
    }

    /// Recently-closed markets for a series, STATUS-AGNOSTIC (time-bounded).
    /// The `status=settled` filter lags the actual result: post-close markets
    /// progress closed→determined→finalized and carry a usable `result` before
    /// the settled filter includes them (live finding 2026-07-24 — 3/3 streak
    /// entries skipped `prev_not_settled` inside the 60s window). Callers filter
    /// on non-empty `result`, so status is irrelevant here.
    pub async fn recent_closed(
        &self,
        series_ticker: &str,
        lookback_secs: i64,
        limit: u32,
    ) -> Result<Vec<Market>> {
        let now = chrono::Utc::now().timestamp();
        let limit = limit.to_string();
        let min_ts = (now - lookback_secs).to_string();
        let max_ts = now.to_string();
        let body = self
            .http
            .get(format!("{}{PREFIX}/markets", api_base()))
            .query(&[
                ("series_ticker", series_ticker),
                ("min_close_ts", min_ts.as_str()),
                ("max_close_ts", max_ts.as_str()),
                ("limit", limit.as_str()),
            ])
            .send()
            .await?;
        let text = text_or_error(body, "recent_closed").await?;
        parse_markets(&text)
    }

    /// Fetch a single market by ticker (public GET, no auth). The response
    /// carries the authoritative settlement `result` ("yes"/"no" once settled,
    /// empty while open) — the source of truth for the reconcile loop.
    pub async fn market(&self, ticker: &str) -> Result<Market> {
        let url = format!("{}{PREFIX}/markets/{ticker}", api_base());
        let text = text_or_error(self.http.get(url).send().await?, "market").await?;
        let resp: MarketResp =
            serde_json::from_str(&text).context("parsing market response")?;
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
        let (status, text, reqid) = self
            .place_limit_buy_raw(ticker, side, count, price_cents, client_order_id)
            .await?;
        if !(200..300).contains(&status) {
            // Keep the raw body in the error so the lost-ack recovery path
            // (strategy::execute_live) and operators can see WHAT Kalshi said;
            // capture x-request-id for support forensics (item 5).
            let rid = reqid
                .map(|r| format!(" (request-id {r})"))
                .unwrap_or_default();
            return Err(anyhow!("order placement HTTP {status}{rid}: {text}"));
        }
        serde_json::from_str(&text).context("parsing create-order response JSON")
    }

    /// Low-level create-order POST returning `(http_status, raw_body_text,
    /// x_request_id)` WITHOUT treating a non-2xx as an error — so callers can
    /// inspect the exact response (e.g. the duplicate-`client_order_id` demo
    /// probe, fix 2b; the benign-409 classification in execute_live, item 2). The
    /// request-id is captured for support forensics (item 5). Signed.
    pub async fn place_limit_buy_raw(
        &self,
        ticker: &str,
        side: &str,
        count: i64,
        price_cents: i64,
        client_order_id: &str,
    ) -> Result<(u16, String, Option<String>)> {
        let path = format!("{PREFIX}/portfolio/events/orders");
        let headers = self.sign_headers("POST", &path)?;
        // The coid is sanitized INSIDE the body builder — the wire is the only
        // place the no-'.' invariant can be enforced once for every caller.
        let body = taker_order_body(ticker, side, count, price_cents, client_order_id);
        let mut req = self.http.post(format!("{}{path}", api_base())).json(&body);
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status().as_u16();
        let reqid = header(&resp, "x-request-id").or_else(|| header(&resp, "request-id"));
        let text = resp.text().await?;
        Ok((status, text, reqid))
    }

    /// Account cash balance in cents. Signed.
    pub async fn balance_cents(&self) -> Result<i64> {
        let path = format!("{PREFIX}/portfolio/balance");
        let headers = self.sign_headers("GET", &path)?;
        let mut req = self.http.get(format!("{}{path}", api_base()));
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let body = text_or_error(req.send().await?, "balance").await?;
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
        let text = text_or_error(req.send().await?, "positions").await?;
        serde_json::from_str(&text).context("parsing positions response")
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
        let text = text_or_error(req.send().await?, "fills").await?;
        serde_json::from_str(&text).context("parsing fills response")
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
        let text = text_or_error(req.send().await?, "cancel_order").await?;
        serde_json::from_str(&text).context("parsing cancel response")
    }

    // -----------------------------------------------------------------------
    // MAKER capability (house probe) — RESTING two-sided quotes. Parallel to the
    // IOC taker path; NEVER shares its risk semantics. The load-bearing safety
    // property is `expiration_ts`: every resting order auto-cancels at a future
    // unix-second deadline, so a dead process leaves NOTHING resting beyond ~75s.
    // -----------------------------------------------------------------------

    /// Place a RESTING limit order via the V2 create-order endpoint
    /// (`POST /trade-api/v2/portfolio/events/orders`), the maker analogue of
    /// [`place_limit_buy_raw`]. The critical difference: instead of
    /// `time_in_force=immediate_or_cancel`, we set **`expiration_ts`** to a FUTURE
    /// unix-second deadline (GTD, good-till-date). Kalshi V2 semantics:
    ///   - `expiration_ts` in the future → order rests until then, then auto-cancels
    ///   - `expiration_ts` omitted        → GTC, rests forever (WE NEVER DO THIS)
    ///   - `expiration_ts` in the past    → treated as IOC
    ///
    /// We ALWAYS pass a future ts, so a crashed process can leave nothing resting
    /// beyond the expiry. `time_in_force` is intentionally OMITTED (its presence as
    /// immediate_or_cancel would make the order a taker). `side`/`price_cents` use
    /// the same call boundary as the taker path (yes/no + our-side whole cents),
    /// translated to the single-book YES-leg via [`book_side`]/[`order_price_dollars`].
    /// Returns `(http_status, raw_body, x_request_id)` WITHOUT erroring on non-2xx
    /// so the caller can inspect the response (resting orders return fill_count 0,
    /// remaining_count == count, status "resting", and an order_id). Signed.
    pub async fn place_resting_limit_raw(
        &self,
        ticker: &str,
        side: &str,
        count: i64,
        price_cents: i64,
        expiration_ts: i64,
        client_order_id: &str,
    ) -> Result<(u16, String, Option<String>)> {
        let path = format!("{PREFIX}/portfolio/events/orders");
        let headers = self.sign_headers("POST", &path)?;
        // The coid is sanitized INSIDE the body builder (see [`sanitize_coid`]):
        // the house sleeve builds `house-{ticker}-{side}-{ts}` raw, and on a
        // dotted ticker every quote 400'd until this invariant moved to the wire.
        let body = resting_order_body(
            ticker,
            side,
            count,
            price_cents,
            expiration_ts,
            client_order_id,
        );
        let mut req = self.http.post(format!("{}{path}", api_base())).json(&body);
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status().as_u16();
        let reqid = header(&resp, "x-request-id").or_else(|| header(&resp, "request-id"));
        let text = resp.text().await?;
        Ok((status, text, reqid))
    }

    /// List the account's currently-resting orders (signed GET
    /// `/portfolio/orders?status=resting[&ticker=...]`). Used for the startup
    /// orphan sweep (cancel any quote a prior crash left alive) and to confirm a
    /// resting order actually auto-cancelled at its `expiration_ts` on demo.
    /// Returns the raw JSON; parsing is delegated to [`parse_resting_orders`].
    pub async fn resting_orders(&self, ticker: Option<&str>) -> Result<serde_json::Value> {
        let path = format!("{PREFIX}/portfolio/orders");
        let headers = self.sign_headers("GET", &path)?;
        let mut req = self
            .http
            .get(format!("{}{path}", api_base()))
            .query(&[("status", "resting"), ("limit", "200")]);
        if let Some(t) = ticker {
            req = req.query(&[("ticker", t)]);
        }
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let text = text_or_error(req.send().await?, "resting_orders").await?;
        serde_json::from_str(&text).context("parsing resting orders response")
    }

    /// Order book for a market (public). Captured as the decision snapshot at
    /// every signal moment (DATA CAPTURE, redirect 2026-07-23).
    pub async fn orderbook(&self, ticker: &str) -> Result<serde_json::Value> {
        let url = format!("{}{PREFIX}/markets/{ticker}/orderbook", api_base());
        let text = text_or_error(self.http.get(url).send().await?, "orderbook").await?;
        serde_json::from_str(&text).context("parsing orderbook response")
    }

    /// Server clock (unix seconds) from the HTTP `Date` header of a public GET
    /// (`/exchange/status`) — no auth, no signing (item 7). Used once per live
    /// reconcile pass to detect clock skew BEFORE it 401s every signed call (a Mac
    /// sleep desyncs the clock; public data still flows, signed calls fail).
    pub async fn server_time(&self) -> Result<i64> {
        let url = format!("{}{PREFIX}/exchange/status", api_base());
        let resp = self.http.get(url).send().await?;
        let date =
            header(&resp, "date").ok_or_else(|| anyhow!("no Date header on /exchange/status"))?;
        parse_http_date_unix(&date).ok_or_else(|| anyhow!("unparseable Date header: {date}"))
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
    /// ACTUAL fee for THIS fill, in cents. DEMO-VERIFIED 2026-07-26: a fills row
    /// carries `fee_cost` in DOLLARS and it is the TOTAL for the row, not a
    /// per-contract figure (unlike create-order's `average_fee_paid`). This is
    /// the only place a RESTING order's fee is ever visible — the create
    /// response for a resting order reports no fee because nothing filled yet.
    pub fee_cents: Option<f64>,
    /// DEMO-VERIFIED 2026-07-26: `is_taker` is present on every fills row. A
    /// maker fill (`false`) came off the book we posted to — and on demo it
    /// billed `fee_cost: 0.000000`, i.e. maker fills were FREE.
    pub is_taker: Option<bool>,
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

/// THE COID CHOKE POINT. Kalshi rejects any `client_order_id` containing '.'
/// with **400 `invalid_parameters`** (LIVE-PROVEN 2026-07-27: every volbook
/// order on a dotted ticker like `KXCOPPERD-26JUL2717-T6.40` 400'd while
/// dot-free gold tickers on the same pass went through; the LIP probe and the
/// house sleeve hit the identical wall within the same 24h). Dotted tickers are
/// how Kalshi encodes fractional strikes, so any sleeve quoting metals/politics
/// mints one by default — the trap fires per call site, forever, until the
/// invariant lives at the WIRE, which is here.
///
/// Contract: '.' -> '_', nothing else. Properties the callers depend on:
///   - **deterministic + idempotent** — the same raw coid always maps to the
///     same wire coid, so re-POSTing after a lost ack still collides with the
///     original and earns the benign 409 `order_already_exists` that
///     `recover_lost_ack` / `classify_resting_failure` rely on. `sanitize_coid`
///     is also a fixed point on its own output (no '.' left to map).
///   - **prefix-preserving** — never touches the leading `{sleeve}-` namespace,
///     so `is_house_order`'s `starts_with("house-")` and every series filter
///     keep working on the exchange-echoed form.
///   - **same mapping as the entry path** (commit 482afd2, `entry_coid`), so a
///     coid built raw and one pre-sanitized by a caller land on the SAME string
///     and never split a dedupe namespace.
///
/// Applied inside [`Kalshi::place_limit_buy_raw`] and
/// [`Kalshi::place_resting_limit_raw`] — the only two functions in the codebase
/// that POST an order — so no coid with a '.' can leave the client regardless of
/// what a caller builds. Any code comparing a locally-built coid against an
/// exchange-echoed one MUST compare the sanitized form.
pub fn sanitize_coid(raw: &str) -> String {
    raw.replace('.', "_")
}

/// The exact JSON body of a TAKER (IOC) create-order POST. Pure, so the wire
/// shape — including the sanitized coid — is unit-testable without a network.
fn taker_order_body(
    ticker: &str,
    side: &str,
    count: i64,
    price_cents: i64,
    client_order_id: &str,
) -> serde_json::Value {
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
    map.insert(
        "client_order_id".into(),
        json!(sanitize_coid(client_order_id)),
    );
    serde_json::Value::Object(map)
}

/// The exact JSON body of a RESTING (GTD) create-order POST. Pure; see
/// [`Kalshi::place_resting_limit_raw`] for why `expiration_ts` is load-bearing.
fn resting_order_body(
    ticker: &str,
    side: &str,
    count: i64,
    price_cents: i64,
    expiration_ts: i64,
    client_order_id: &str,
) -> serde_json::Value {
    let mut map = serde_json::Map::new();
    map.insert("ticker".into(), json!(ticker));
    map.insert("side".into(), json!(book_side(side)));
    map.insert("count".into(), json!(count_fp(count)));
    map.insert("price".into(), json!(order_price_dollars(side, price_cents)));
    // GTD resting: time_in_force is REQUIRED by the API (demo-proven 2026-07-25:
    // omitting it -> 400 "failed on the 'required' tag"); a resting order uses
    // good_till_cancelled + a FUTURE expiration_ts, which bounds its life (the
    // safety property — never omit expiration_ts, that would rest forever).
    map.insert("time_in_force".into(), json!("good_till_canceled"));
    map.insert("expiration_ts".into(), json!(expiration_ts));
    // Demo-proven: "cancel_both" fails the API's oneof validation; use the same
    // taker_at_cross the IOC path uses (our two legs sit 2c apart and never
    // cross each other, so STP should never fire at all).
    map.insert(
        "self_trade_prevention_type".into(),
        json!("taker_at_cross"),
    );
    map.insert(
        "client_order_id".into(),
        json!(sanitize_coid(client_order_id)),
    );
    serde_json::Value::Object(map)
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
        // Side: legacy `side` OR the current generation's `outcome_side`
        // (yes/no in both). Same field feeds both order-match fallback and price.
        let fill_side = f
            .get("side")
            .or_else(|| f.get("outcome_side"))
            .and_then(|v| v.as_str());
        // order match
        let matches = match order_id {
            Some(id) => f.get("order_id").and_then(|v| v.as_str()) == Some(id),
            None => {
                let side_ok = fill_side == Some(side);
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
        // Our side's price, tolerating BOTH schema generations:
        //   (a) legacy per-side field: `<side>_price` / `<side>_price_dollars`
        //   (b) current single YES-space field: `yes_price_dollars` / `yes_price`
        //       — fold to our side (NO price = 100 − yes-cents).
        // (a) also transparently catches the current YES fill, whose
        // `yes_price_dollars` IS `<side>_price_dollars` when side=="yes".
        let price_key = format!("{side}_price");
        let price = f
            .get(&price_key)
            .and_then(price_to_cents)
            .or_else(|| {
                f.get(format!("{price_key}_dollars").as_str())
                    .and_then(price_to_cents)
            })
            .or_else(|| {
                let yes = f
                    .get("yes_price_dollars")
                    .and_then(price_to_cents)
                    .or_else(|| f.get("yes_price").and_then(price_to_cents))?;
                Some(if side.eq_ignore_ascii_case("no") {
                    100 - yes
                } else {
                    yes
                })
            });
        let Some(price_cents) = price else { continue };
        // Fee: `fee_cost` (demo-verified) in dollars, total for the row. Older /
        // alternate spellings tolerated; anything unparseable stays None rather
        // than guessing a number into the P&L.
        let fee_cents = ["fee_cost", "fee_cost_dollars", "fee", "fee_dollars"]
            .iter()
            .find_map(|k| f.get(*k).and_then(dollars_f64))
            .map(|d| d * 100.0);
        let is_taker = f.get("is_taker").and_then(|v| v.as_bool());
        out.push(ParsedFill {
            count,
            price_cents,
            ts_ms: fill_ts_ms(f),
            fee_cents,
            is_taker,
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
/// Contracts REMOVED from the book by a cancel, from the V2 cancel response.
///
/// DEMO-VERIFIED 2026-07-26: a successful cancel returns
/// `{"order_id": "...", "reduced_by": "1.00", "ts_ms": ...}` — `reduced_by` is
/// the quantity that was still resting, i.e. the UNFILLED remainder. This is the
/// only synchronous, non-lagging answer to "did any of it fill before I pulled
/// it?": `/portfolio/fills` trails the matching engine by seconds, so a caller
/// that must decide immediately (send a backstop order or not?) reads this.
/// `None` = the field was absent/unparseable — the caller must fall back to
/// polling fills rather than assume either way.
pub fn parse_cancel_reduced_by(resp: &serde_json::Value) -> Option<i64> {
    resp.get("reduced_by")
        .or_else(|| resp.get("order").and_then(|o| o.get("reduced_by")))
        .and_then(count_to_i64)
}

/// Total ACTUAL fee across `fills` in cents, or None if no row reported one.
/// Rows that omit the fee contribute nothing — a partial answer is still better
/// than our own estimate, and the caller records both.
pub fn fills_fee_cents(fills: &[ParsedFill]) -> Option<f64> {
    let known: Vec<f64> = fills.iter().filter_map(|f| f.fee_cents).collect();
    if known.is_empty() {
        None
    } else {
        Some(known.iter().sum())
    }
}

/// True when EVERY row that reported `is_taker` reported `false` — i.e. the
/// whole quantity was a maker fill. None when no row said.
pub fn fills_all_maker(fills: &[ParsedFill]) -> Option<bool> {
    let known: Vec<bool> = fills.iter().filter_map(|f| f.is_taker).collect();
    if known.is_empty() {
        None
    } else {
        Some(known.iter().all(|t| !t))
    }
}

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
        // Field-name migration (EMPIRICALLY OBSERVED on demo 2026-07-24): the
        // live API returns `position_fp` ("-1.00" fixed-point string), NOT the
        // legacy `position` int. Reading only `position` drops every row and
        // silently blinds orphan adoption + the divergence breaker. Read both.
        let net = r
            .get("position")
            .or_else(|| r.get("position_fp"))
            .and_then(count_to_i64)
            .unwrap_or(0);
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

/// One resting (open, unfilled-remainder) order as the exchange sees it, from
/// `/portfolio/orders?status=resting`. The startup orphan sweep only needs
/// `order_id` (to cancel) + `ticker` (to log); side/price/remaining/expiration
/// are best-effort for the participation record and expiration-audit.
#[derive(Debug, Clone, PartialEq)]
pub struct RestingOrder {
    pub order_id: String,
    /// The `client_order_id` we minted, when the payload echoes it. Every sleeve
    /// namespaces its coids (`streak-…-m40`, `house-{ticker}-{side}-{ts}`), so
    /// this is what lets a sweep cancel ONLY its own orders (moneypath F5).
    /// `None` when absent — callers must fall back to a ticker/series filter
    /// rather than assume ownership either way.
    pub client_order_id: Option<String>,
    pub ticker: String,
    /// Our-side ("yes"/"no") folded from the YES-book bid/ask, when derivable.
    pub side: Option<String>,
    /// Unfilled contracts still resting.
    pub remaining_count: i64,
    /// Our-side limit in whole cents, when derivable.
    pub price_cents: Option<i64>,
    /// Expiration deadline as unix seconds, when present (the safety property).
    pub expiration_ts: Option<i64>,
}

/// Parse a `/portfolio/orders` response into resting orders. TOLERANT of schema
/// variants (Kalshi's fixed-point `_dollars` migration + numeric/string encodings)
/// because the exact resting-order schema is confirmed only on the demo shakeout —
/// `order_id` is the sole hard requirement (it is what the sweep cancels). Rows
/// without an order_id, or with remaining_count <= 0, are dropped.
pub fn parse_resting_orders(body: &serde_json::Value) -> Vec<RestingOrder> {
    let empty = vec![];
    let rows = body
        .get("orders")
        .and_then(|v| v.as_array())
        .unwrap_or(&empty);
    let mut out = Vec::new();
    for r in rows {
        let order_id = match r
            .get("order_id")
            .or_else(|| r.get("id"))
            .and_then(|v| v.as_str())
        {
            Some(s) => s.to_string(),
            None => continue,
        };
        let ticker = r
            .get("ticker")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        // remaining: prefer remaining_count[_fp]; fall back to count.
        let remaining_count = r
            .get("remaining_count")
            .or_else(|| r.get("remaining_count_fp"))
            .or_else(|| r.get("count"))
            .or_else(|| r.get("count_fp"))
            .and_then(count_to_i64)
            .unwrap_or(0);
        if remaining_count <= 0 {
            continue;
        }
        // Side: prefer an explicit yes/no `side`; else fold the YES-book bid/ask
        // action (bid = buy YES, ask = sell YES = our NO).
        let side = r.get("side").and_then(|v| v.as_str()).map(|s| {
            if s.eq_ignore_ascii_case("ask") {
                "no".to_string()
            } else if s.eq_ignore_ascii_case("bid") {
                "yes".to_string()
            } else {
                s.to_ascii_lowercase()
            }
        });
        // Price: prefer our-side field, else fold the YES price.
        let price_cents = r
            .get("yes_price_dollars")
            .and_then(price_to_cents)
            .or_else(|| r.get("yes_price").and_then(price_to_cents))
            .map(|yes| match side.as_deref() {
                Some("no") => 100 - yes,
                _ => yes,
            })
            .or_else(|| r.get("price").and_then(price_to_cents));
        // Expiration: numeric unix seconds, or an RFC3339 `expiration_time`.
        let expiration_ts = r
            .get("expiration_ts")
            .and_then(|v| v.as_i64())
            .or_else(|| {
                r.get("expiration_time")
                    .and_then(|v| v.as_str())
                    .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
                    .map(|dt| dt.timestamp())
            });
        // The coid we minted, tolerant of both spellings the V2 payloads use.
        let client_order_id = r
            .get("client_order_id")
            .or_else(|| r.get("client_order_id_str"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        out.push(RestingOrder {
            order_id,
            client_order_id,
            ticker,
            side,
            remaining_count,
            price_cents,
            expiration_ts,
        });
    }
    out
}

/// Best YES bid/ask (whole cents) from a `/markets/{ticker}/orderbook` response,
/// and the derived mid. The LIVE schema (verified 2026-07-25) is
/// `{"orderbook_fp":{"yes_dollars":[["0.4800","30.00"],...],
/// "no_dollars":[["0.0100","130.00"],...]}}` — string-DOLLAR prices under an
/// `_fp` envelope. We also tolerate the legacy `{"orderbook":{"yes":[[48,..]]}}`
/// integer-cents form (`price_to_cents` handles both encodings). Each price level
/// is a resting BUY: the best YES bid is the highest yes price; the best YES ask =
/// 100 − (highest NO buy price), since a NO bid at n offers YES at 100−n. Returns
/// `(best_bid, best_ask, mid)` in whole cents; any component is None when that
/// side is empty. Pure + unit-tested so the quote loop's mid never depends on the
/// network to be tested.
pub fn orderbook_mid(body: &serde_json::Value) -> (Option<i64>, Option<i64>, Option<i64>) {
    let ob = body
        .get("orderbook_fp")
        .or_else(|| body.get("orderbook"))
        .unwrap_or(body);
    // Try the `_dollars` key first (live), then the bare key (legacy).
    let best = |dollars_key: &str, bare_key: &str| -> Option<i64> {
        ob.get(dollars_key)
            .or_else(|| ob.get(bare_key))
            .and_then(|v| v.as_array())
            .and_then(|levels| {
                levels
                    .iter()
                    .filter_map(|lvl| lvl.as_array().and_then(|a| a.first()))
                    .filter_map(price_to_cents)
                    .max()
            })
    };
    let best_bid = best("yes_dollars", "yes");
    let best_ask = best("no_dollars", "no").map(|no| 100 - no);
    let mid = match (best_bid, best_ask) {
        (Some(b), Some(a)) => Some(((b + a) as f64 / 2.0).round() as i64),
        _ => None,
    };
    (best_bid, best_ask, mid)
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

    /// THE CLASS REGRESSION. A dotted ticker (Kalshi's fractional-strike form,
    /// e.g. KXAPRPOTUS-26JUL31-40.9) must produce a dot-free coid on BOTH
    /// order-placement paths — the taker POST and the resting POST are the only
    /// two functions in the codebase that send a client_order_id, and each has
    /// now been the site of an independent live 400 (engine entry 482afd2, the
    /// LIP probe, the house sleeve). The caller is deliberately the NAIVE one:
    /// the house sleeve's raw `house-{ticker}-{side}-{ts}` format, unmodified.
    #[test]
    fn dotted_tickers_never_reach_the_wire_on_any_placement_path() {
        let ticker = "KXAPRPOTUS-26JUL31-40.9";
        let naive_coid = format!("house-{ticker}-yes-1769900000");
        assert!(naive_coid.contains('.'), "the caller really is naive");

        let taker = taker_order_body(ticker, "yes", 1, 40, &naive_coid);
        let resting = resting_order_body(ticker, "no", 2, 40, 1769900075, &naive_coid);
        for (path, body) in [("taker", &taker), ("resting", &resting)] {
            let sent = body["client_order_id"].as_str().expect("coid on the wire");
            assert!(!sent.contains('.'), "{path} coid reached the wire dotted: {sent}");
            assert_eq!(sent, "house-KXAPRPOTUS-26JUL31-40_9-yes-1769900000");
            // The TICKER field keeps its dot — only the coid is rewritten, or we
            // would be ordering on a market that does not exist.
            assert_eq!(body["ticker"].as_str(), Some(ticker));
        }
    }

    /// Idempotency + namespace stability — the properties the dedupe path rests
    /// on. Recovery re-POSTs the SAME logical coid expecting Kalshi's 409
    /// `order_already_exists`; that only works if sanitize is deterministic and
    /// a fixed point on its own output (a pre-sanitized coid must not be
    /// rewritten a second time into a different string).
    #[test]
    fn sanitize_coid_is_deterministic_idempotent_and_prefix_preserving() {
        let raw = "volbook-KXCOPPERD-26JUL2717-T6.40";
        let once = sanitize_coid(raw);
        assert_eq!(once, "volbook-KXCOPPERD-26JUL2717-T6_40");
        assert_eq!(sanitize_coid(raw), once, "not deterministic");
        assert_eq!(sanitize_coid(&once), once, "not idempotent");
        // Multiple dots (a hypothetical multi-decimal strike) all map.
        assert_eq!(sanitize_coid("a.b.c"), "a_b_c");
        // Untouched when clean — every historical crypto/gold coid is byte-identical,
        // so restart-dedupe against pre-fix orders still collides.
        assert_eq!(sanitize_coid("streak-KXBTC15M-26JUL251000-00"), "streak-KXBTC15M-26JUL251000-00");
        // Sleeve prefix (what every ownership matcher keys on) is never altered.
        assert!(sanitize_coid("house-KXAPRPOTUS-26JUL31-40.9-yes-1").starts_with("house-"));
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
    fn parse_fills_current_schema_outcome_side_yes() {
        // Current generation: `outcome_side`, single YES-space `yes_price_dollars`,
        // `count_fp`, `fill_id`, `order_id`. A YES fill reads yes_price_dollars
        // directly (it IS <side>_price_dollars when side=="yes").
        let body = serde_json::json!({"fills": [
            {"order_id": "O1", "fill_id": "F1", "outcome_side": "yes",
             "count_fp": "3.00", "yes_price_dollars": "0.41",
             "created_time": "2026-07-23T18:00:01Z"},
            // foreign order, excluded by order_id match
            {"order_id": "O2", "fill_id": "F2", "outcome_side": "yes",
             "count_fp": "9.00", "yes_price_dollars": "0.50"},
        ]});
        let fills = parse_fills(&body, Some("O1"), "yes", 0);
        assert_eq!(fills.len(), 1);
        assert_eq!(fills[0].count, 3);
        assert_eq!(fills[0].price_cents, 41);
    }

    #[test]
    fn parse_fills_current_schema_no_side_folds_yes_price() {
        // Current generation NO fill: the exchange reports the YES-book price in
        // `yes_price_dollars`; our NO cost = 100 − yes-cents. 0.62 -> NO 38c.
        let body = serde_json::json!({"fills": [
            {"order_id": "N1", "fill_id": "FN1", "outcome_side": "no",
             "count_fp": "5.00", "yes_price_dollars": "0.6200",
             "created_time": "2026-07-23T18:00:03Z"},
        ]});
        let fills = parse_fills(&body, Some("N1"), "no", 0);
        assert_eq!(fills.len(), 1);
        assert_eq!(fills[0].count, 5);
        assert_eq!(fills[0].price_cents, 38); // 100 - 62
    }

    #[test]
    fn parse_fills_current_schema_no_fold_via_time_fallback() {
        // NO fold AND the order_id-less fallback path (lost-ack recovery): match by
        // outcome_side + submit-time window, fold yes_price_dollars to NO cents.
        let body = serde_json::json!({"fills": [
            {"fill_id": "F", "outcome_side": "no", "count_fp": "2.00",
             "yes_price_dollars": "0.55", "created_time": "2026-07-23T18:00:05Z"},
            // wrong side, excluded
            {"fill_id": "G", "outcome_side": "yes", "count_fp": "4.00",
             "yes_price_dollars": "0.50", "created_time": "2026-07-23T18:00:05Z"},
        ]});
        let since = chrono::DateTime::parse_from_rfc3339("2026-07-23T18:00:00Z")
            .unwrap()
            .timestamp_millis();
        let fills = parse_fills(&body, None, "no", since);
        assert_eq!(fills.len(), 1);
        assert_eq!(fills[0].count, 2);
        assert_eq!(fills[0].price_cents, 45); // 100 - 55
    }

    #[test]
    fn fills_summary_empty_is_zero() {
        assert_eq!(fills_summary(&[]), (0, None, None));
    }

    #[test]
    fn parse_api_error_nested_flat_and_string() {
        // Nested (the common Kalshi shape).
        let a = parse_api_error(r#"{"error":{"code":"order_already_exists","message":"dup"}}"#);
        assert_eq!(a.code.as_deref(), Some("order_already_exists"));
        assert_eq!(a.message.as_deref(), Some("dup"));
        // Flat.
        let b = parse_api_error(r#"{"code":"insufficient_balance","message":"broke"}"#);
        assert_eq!(b.code.as_deref(), Some("insufficient_balance"));
        // {"error":"string"} variant.
        let c = parse_api_error(r#"{"error":"rate limited"}"#);
        assert_eq!(c.code, None);
        assert_eq!(c.message.as_deref(), Some("rate limited"));
        // Non-JSON → empty.
        let d = parse_api_error("<html>502</html>");
        assert_eq!(d, ApiError::default());
    }

    #[test]
    fn parse_http_date_reads_imf_fixdate() {
        // RFC 7231 IMF-fixdate (the HTTP Date header form). 1994-11-06 08:49:37Z.
        assert_eq!(
            parse_http_date_unix("Sun, 06 Nov 1994 08:49:37 GMT"),
            Some(784_111_777)
        );
        // Trailing/leading whitespace tolerated.
        assert_eq!(
            parse_http_date_unix("  Sun, 06 Nov 1994 08:49:37 GMT "),
            Some(784_111_777)
        );
        assert_eq!(parse_http_date_unix("not a date"), None);
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
    fn parse_positions_reads_live_fp_schema_verbatim() {
        // VERBATIM capture from demo /portfolio/positions 2026-07-24 — the
        // schema that returned PARSED:[] before the position_fp fix.
        let body = serde_json::json!({"cursor":"","market_positions":[
            {"fees_paid_dollars":"0.013800","market_exposure_dollars":"0.730000",
             "position_fp":"-1.00","realized_pnl_dollars":"0.000000",
             "ticker":"KXHIGHNY-26JUL24-T81","total_traded_dollars":"0.730000"},
            {"fees_paid_dollars":"0.004600","market_exposure_dollars":"0.070000",
             "position_fp":"1.00","realized_pnl_dollars":"0.000000",
             "ticker":"KXHIGHNY-26JUL24-B87.5","total_traded_dollars":"0.070000"}
        ]});
        let ps = parse_positions(&body);
        assert_eq!(ps.len(), 2);
        assert_eq!(ps[0].count, 1);
        assert_eq!(ps[0].entry_cents, Some(73)); // NO held at 73c
        assert_eq!(ps[1].entry_cents, Some(7)); // YES held at 7c
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
    fn parse_resting_orders_reads_id_ticker_side_price_expiry() {
        // A YES bid @ 49c and a NO leg (YES-book ask, side "ask") @ our-NO 49c
        // (yes_price 51 -> 100-51). A filled/zero-remaining row is dropped, and a
        // row with no order_id is dropped (can't be swept).
        let body = serde_json::json!({"orders": [
            {"order_id": "O1", "ticker": "KXAPRPOTUS-T50", "side": "bid",
             "remaining_count": 1, "yes_price_dollars": "0.49",
             "expiration_ts": 1_800_000_075},
            {"order_id": "O2", "ticker": "KXAPRPOTUS-T50", "side": "ask",
             "remaining_count_fp": "1.00", "yes_price_dollars": "0.51",
             "expiration_time": "2027-01-15T00:00:00Z"},
            {"order_id": "O3", "ticker": "KXAPRPOTUS-T50", "side": "bid",
             "remaining_count": 0, "yes_price_dollars": "0.49"}, // filled -> drop
            {"ticker": "NOID", "remaining_count": 1}, // no id -> drop
        ]});
        let ords = parse_resting_orders(&body);
        assert_eq!(ords.len(), 2);
        assert_eq!(ords[0].order_id, "O1");
        assert_eq!(ords[0].side.as_deref(), Some("yes"));
        assert_eq!(ords[0].price_cents, Some(49));
        assert_eq!(ords[0].remaining_count, 1);
        assert_eq!(ords[0].expiration_ts, Some(1_800_000_075));
        assert_eq!(ords[1].order_id, "O2");
        assert_eq!(ords[1].side.as_deref(), Some("no"));
        assert_eq!(ords[1].price_cents, Some(49)); // 100 - 51
        assert!(ords[1].expiration_ts.is_some());
    }

    #[test]
    fn parse_resting_orders_empty_and_missing() {
        assert!(parse_resting_orders(&serde_json::json!({})).is_empty());
        assert!(parse_resting_orders(&serde_json::json!({"orders": []})).is_empty());
    }

    #[test]
    fn orderbook_mid_from_yes_and_no_books() {
        // YES best bid = highest yes price = 48. NO best = 50 -> YES ask = 50.
        // mid = (48+50)/2 = 49.
        let body = serde_json::json!({"orderbook": {
            "yes": [[45, 100], [48, 30]],
            "no":  [[49, 20], [50, 10]],
        }});
        let (bid, ask, mid) = orderbook_mid(&body);
        assert_eq!(bid, Some(48));
        assert_eq!(ask, Some(50)); // 100 - 50
        assert_eq!(mid, Some(49));
    }

    #[test]
    fn orderbook_mid_one_sided_has_no_mid() {
        let body = serde_json::json!({"orderbook": {"yes": [[48, 10]], "no": []}});
        let (bid, ask, mid) = orderbook_mid(&body);
        assert_eq!(bid, Some(48));
        assert_eq!(ask, None);
        assert_eq!(mid, None);
    }

    #[test]
    fn orderbook_mid_live_fp_dollars_schema() {
        // VERBATIM live shape (2026-07-25): orderbook_fp with string-dollar prices.
        // YES best bid = 0.48 = 48. NO best = 0.50 = 50 -> YES ask 50. mid 49.
        let body = serde_json::json!({"orderbook_fp": {
            "yes_dollars": [["0.4500", "100.00"], ["0.4800", "30.00"]],
            "no_dollars":  [["0.4900", "20.00"], ["0.5000", "10.00"]],
        }});
        let (bid, ask, mid) = orderbook_mid(&body);
        assert_eq!(bid, Some(48));
        assert_eq!(ask, Some(50));
        assert_eq!(mid, Some(49));
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

    /// VERBATIM capture from the demo cancel of a resting order, 2026-07-26
    /// (`demo_streak_rest_cancel_ioc` step 3). `reduced_by` is the quantity that
    /// was STILL RESTING — the streak backstop reads it to decide, synchronously,
    /// whether any of the maker leg filled before the cancel landed.
    #[test]
    fn parse_cancel_reduced_by_reads_the_demo_response() {
        let resp: serde_json::Value = serde_json::from_str(
            r#"{"order_id":"e968ca51-efd7-4efa-9cec-e28ec3319774","reduced_by":"1.00","ts_ms":1785089698808}"#,
        )
        .unwrap();
        assert_eq!(parse_cancel_reduced_by(&resp), Some(1));
        // Envelope tolerance + absence.
        assert_eq!(
            parse_cancel_reduced_by(&json!({"order": {"reduced_by": "10.00"}})),
            Some(10)
        );
        assert_eq!(parse_cancel_reduced_by(&json!({"paper": true})), None);
        assert_eq!(parse_cancel_reduced_by(&json!(null)), None);
    }

    /// VERBATIM capture of TWO demo fills rows, 2026-07-26 — one MAKER fill (our
    /// resting NO bid at 15¢ got hit) and one TAKER fill (the IOC). Settles the
    /// two fields the maker path depends on: `fee_cost` (dollars, TOTAL for the
    /// row) and `is_taker`. The maker fill billed 0.000000 — maker fills are FREE
    /// on demo, so charging them at taker rates would invent losses.
    #[test]
    fn parse_fills_reads_demo_fee_cost_and_is_taker() {
        let body: serde_json::Value = serde_json::from_str(
            r#"{"fills":[
              {"count_fp":"1.00","fee_cost":"0.009900","is_taker":true,
               "no_price_dollars":"0.1700","order_id":"TAKER","outcome_side":"no",
               "side":"no","ts":1785089516,"yes_price_dollars":"0.8300"},
              {"count_fp":"1.00","fee_cost":"0.000000","is_taker":false,
               "no_price_dollars":"0.1500","order_id":"MAKER","outcome_side":"no",
               "side":"no","ts":1785089511,"yes_price_dollars":"0.8500"}
            ]}"#,
        )
        .unwrap();

        let maker = parse_fills(&body, Some("MAKER"), "no", 0);
        assert_eq!(maker.len(), 1);
        assert_eq!(maker[0].price_cents, 15);
        assert_eq!(maker[0].is_taker, Some(false));
        assert_eq!(maker[0].fee_cents, Some(0.0));
        assert_eq!(fills_all_maker(&maker), Some(true));
        assert_eq!(fills_fee_cents(&maker), Some(0.0));

        let taker = parse_fills(&body, Some("TAKER"), "no", 0);
        assert_eq!(taker[0].price_cents, 17);
        assert_eq!(taker[0].is_taker, Some(true));
        // 0.0099 dollars -> 0.99 cents, sub-cent resolution preserved.
        assert!((taker[0].fee_cents.unwrap() - 0.99).abs() < 1e-9);
        assert_eq!(fills_all_maker(&taker), Some(false));

        // A row with neither field must not fabricate one.
        let bare: serde_json::Value =
            serde_json::from_str(r#"{"fills":[{"count_fp":"2.00","side":"yes","yes_price_dollars":"0.4000","order_id":"X"}]}"#)
                .unwrap();
        let f = parse_fills(&bare, Some("X"), "yes", 0);
        assert_eq!(f[0].fee_cents, None);
        assert_eq!(fills_fee_cents(&f), None);
        assert_eq!(fills_all_maker(&f), None);
    }

    /// EMPIRICAL probe for deep-review FIX 4 (reality F2) + FIX 3's cancel shape.
    /// Answers, in ~8 calls and $0.01 of demo money:
    ///   (a) does re-POSTing a RESTING order under a duplicate coid return
    ///       **409 `order_already_exists`** — the exact classification
    ///       `classify_resting_failure` now branches `may_be_resting: true` on?
    ///   (b) does the coid dedupe SURVIVE the order's death (the restart case:
    ///       re-POST after the original is cancelled)?
    ///   (c) what does a successful cancel of a fully-resting order return
    ///       (`reduced_by` shape, which FIX 3 logs on the partial path)?
    ///   (d) bonus, free: does `/portfolio/balance` debit a RESTING order's
    ///       collateral? (reality F1 — the whole reason FIX 1 widens the breaker
    ///       rather than excluding reservations.)
    ///
    /// Ignored (needs demo keys + network). Prices 1¢ on a market whose ask is
    /// far above, so it CANNOT fill; everything placed is cancelled before exit.
    ///   KALSHI_API_BASE=https://demo-api.kalshi.co \
    ///   KALSHI_API_KEY_ID=<id> KALSHI_PRIVATE_KEY_PATH=secrets/Demo.txt \
    ///   NESTOR_TEST_TICKER=<open-demo-ticker> \
    ///   cargo test -p engine demo_resting_409 -- --ignored --nocapture
    #[tokio::test]
    #[ignore]
    async fn demo_resting_409_and_cancel_shape() {
        let key_id = std::env::var("KALSHI_API_KEY_ID").expect("KALSHI_API_KEY_ID");
        let key_path = std::env::var("KALSHI_PRIVATE_KEY_PATH").expect("KALSHI_PRIVATE_KEY_PATH");
        let ticker = std::env::var("NESTOR_TEST_TICKER").expect("NESTOR_TEST_TICKER");
        let k = Kalshi::authenticated(key_id, &key_path).unwrap();
        let coid = format!("nestor-rest409-{}", chrono::Utc::now().timestamp());
        let exp = chrono::Utc::now().timestamp() + 180;

        let b0 = k.balance_cents().await.expect("balance b0");
        println!("=== REST-409 PROBE === balance b0 = {b0}c");

        let (s1, b1t, _) = k
            .place_resting_limit_raw(&ticker, "yes", 1, 10, exp, &coid)
            .await
            .expect("first resting POST");
        println!("--- (1) first resting POST  : HTTP {s1}\n{b1t}");
        let v1: serde_json::Value = serde_json::from_str(&b1t).unwrap_or_default();
        let placed = parse_place_response(&v1, "yes");
        let order_id = placed.order_id.clone();

        let b1 = k.balance_cents().await.expect("balance b1");
        println!(
            "--- (d) balance WHILE RESTING: {b1}c (b0 {b0}c, Δ {}c) → {}",
            b1 - b0,
            if b1 == b0 {
                "does NOT lock collateral"
            } else {
                "LOCKS collateral"
            }
        );

        let (s2, b2t, _) = k
            .place_resting_limit_raw(&ticker, "yes", 1, 10, exp, &coid)
            .await
            .expect("duplicate resting POST");
        let api2 = parse_api_error(&b2t);
        println!(
            "--- (a) duplicate coid WHILE RESTING: HTTP {s2} code={:?}\n{b2t}",
            api2.code
        );
        println!(
            "        classify_resting_failure({s2}, {:?}) = {:?}",
            api2.code,
            crate::strategy::classify_resting_failure_pub(s2, api2.code.as_deref())
        );

        if let Some(oid) = &order_id {
            let cancel = k.cancel_order(oid).await;
            match &cancel {
                Ok(v) => println!(
                    "--- (c) cancel of a FULLY-RESTING order: {v}\n        reduced_by = {:?}",
                    parse_cancel_reduced_by(v)
                ),
                Err(e) => println!("--- (c) cancel FAILED: {e}"),
            }
        }

        let (s3, b3t, _) = k
            .place_resting_limit_raw(&ticker, "yes", 1, 10, exp, &coid)
            .await
            .expect("post-death duplicate POST");
        let api3 = parse_api_error(&b3t);
        println!(
            "--- (b) duplicate coid AFTER the order died: HTTP {s3} code={:?}\n{b3t}",
            api3.code
        );
        // If the third POST was ACCEPTED, a new order is now resting — kill it.
        if (200..300).contains(&s3) {
            let v3: serde_json::Value = serde_json::from_str(&b3t).unwrap_or_default();
            if let Some(oid) = parse_place_response(&v3, "yes").order_id {
                println!("        (3rd POST created {oid} — cancelling)");
                let _ = k.cancel_order(&oid).await;
            }
        }

        let b2 = k.balance_cents().await.expect("balance b2");
        println!("--- balance after cancel: {b2}c (b0 {b0}c)");

        // MUST exit with nothing of ours resting.
        let resting = k.resting_orders(Some(&ticker)).await.expect("resting list");
        let ours: Vec<_> = parse_resting_orders(&resting)
            .into_iter()
            // Compare against the SANITIZED form: the exchange echoes what the
            // wire carried, which is `sanitize_coid(coid)` — matching the raw
            // string would silently miss on any dotted input and report "no
            // leak" while an order of ours is still resting.
            .filter(|o| o.client_order_id.as_deref() == Some(sanitize_coid(&coid).as_str()))
            .collect();
        println!("--- exit check: {} of our orders still resting", ours.len());
        for o in &ours {
            println!("    LEAKED {} — cancelling", o.order_id);
            let _ = k.cancel_order(&o.order_id).await;
        }
        assert!(ours.is_empty(), "probe leaked a resting order");
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

        let (s1, b1, _r1) = k
            .place_limit_buy_raw(&ticker, "yes", 1, 2, coid)
            .await
            .expect("first POST");
        println!("=== DUP-COID PROBE first  === HTTP {s1}\n{b1}");
        let (s2, b2, _r2) = k
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

#[cfg(test)]
mod schema_probes {
    use super::*;

    /// Ignored network probe: print the RAW /portfolio/positions JSON from demo
    /// (we hold real demo positions) to settle the position vs position_fp /
    /// market_exposure vs _dollars field-name question empirically.
    #[tokio::test]
    #[ignore]
    async fn demo_positions_schema_probe() {
        let k = Kalshi::authenticated(
            std::env::var("KALSHI_API_KEY_ID").unwrap(),
            &std::env::var("KALSHI_PRIVATE_KEY_PATH").unwrap(),
        )
        .unwrap();
        let raw = k.positions().await.unwrap();
        println!("RAW POSITIONS:\n{}", serde_json::to_string_pretty(&raw).unwrap());
        let parsed = parse_positions(&raw);
        println!("PARSED: {parsed:?}");
    }
}

#[cfg(test)]
mod maker_demo_probes {
    use super::*;

    /// EMPIRICAL maker-mechanics probe (the house-probe demo shakeout). Proves the
    /// five load-bearing behaviors on DEMO before any prod maker order exists:
    ///   1. RESTING placement — a create with a FUTURE expiration_ts and NO
    ///      immediate_or_cancel returns 201, fill_count 0, remaining == count,
    ///      status resting, an order_id (i.e. it did NOT instantly IOC).
    ///   2. It appears in /portfolio/orders?status=resting.
    ///   3. cancel-by-id removes it (resting_orders no longer lists it).
    ///   4. expiration auto-cancel — place a SHORT-expiry (now+8s) order, sleep
    ///      past it, confirm it is gone from resting_orders WITHOUT us cancelling.
    ///   5. startup orphan sweep — place one, then cancel EVERY resting order the
    ///      account shows (the sweep), confirm none remain.
    /// Fill detection is proven by the existing fills() path (a resting order that
    /// crosses shows up in /portfolio/fills by order_id, same parser as the taker).
    ///
    /// Ignored (needs the DEMO account's key id + network). Run with:
    ///   KALSHI_API_BASE=https://demo-api.kalshi.co \
    ///   KALSHI_API_KEY_ID=<DEMO key id> KALSHI_PRIVATE_KEY_PATH=secrets/Demo.txt \
    ///   NESTOR_TEST_TICKER=<open demo ticker, priced ~40-60c> \
    ///   cargo test -p engine maker_demo_resting_lifecycle -- --ignored --nocapture
    #[tokio::test]
    #[ignore]
    async fn maker_demo_resting_lifecycle() {
        let key_id = std::env::var("KALSHI_API_KEY_ID").expect("KALSHI_API_KEY_ID (demo)");
        let key_path = std::env::var("KALSHI_PRIVATE_KEY_PATH").expect("KALSHI_PRIVATE_KEY_PATH");
        let ticker = std::env::var("NESTOR_TEST_TICKER").expect("NESTOR_TEST_TICKER");
        let k = Kalshi::authenticated(key_id, &key_path).unwrap();
        let now = chrono::Utc::now().timestamp();

        // (1) RESTING placement: a far-from-market YES bid at 2c (won't cross an
        // ~40-60c book) with expiration_ts = now+75s.
        let exp = now + 75;
        let coid = format!("nestor-house-demo-{now}");
        let (s1, b1, _r1) = k
            .place_resting_limit_raw(&ticker, "yes", 1, 2, exp, &coid)
            .await
            .expect("resting POST");
        println!("=== (1) RESTING PLACE === HTTP {s1}\n{b1}");
        let placed = parse_place_response(
            &serde_json::from_str(&b1).unwrap_or(serde_json::Value::Null),
            "yes",
        );
        println!(
            "    fill_count={} remaining={} order_id={:?} (expect 0 / 1 / Some — did NOT IOC)",
            placed.fill_count, placed.remaining_count, placed.order_id
        );
        let oid = placed.order_id.clone();

        // (2) appears in resting_orders.
        let resting = k.resting_orders(Some(&ticker)).await.expect("resting GET");
        let parsed = parse_resting_orders(&resting);
        println!("=== (2) RESTING LIST === {} order(s): {parsed:?}", parsed.len());

        // (3) cancel-by-id, confirm gone.
        if let Some(id) = &oid {
            let c = k.cancel_order(id).await;
            println!("=== (3) CANCEL BY ID === {c:?}");
            let after = parse_resting_orders(&k.resting_orders(Some(&ticker)).await.unwrap());
            let still = after.iter().any(|o| Some(&o.order_id) == oid.as_ref());
            println!("    still resting after cancel: {still} (expect false)");
        }

        // (4) expiration auto-cancel: place now+8s, sleep 12s, confirm gone.
        let exp2 = chrono::Utc::now().timestamp() + 8;
        let coid2 = format!("nestor-house-demo-exp-{}", chrono::Utc::now().timestamp());
        let (s4, b4, _r4) = k
            .place_resting_limit_raw(&ticker, "yes", 1, 2, exp2, &coid2)
            .await
            .expect("short-expiry POST");
        let oid2 = parse_place_response(
            &serde_json::from_str(&b4).unwrap_or(serde_json::Value::Null),
            "yes",
        )
        .order_id;
        println!("=== (4) SHORT-EXPIRY PLACE === HTTP {s4} order_id={oid2:?} exp={exp2}");
        // Poll up to ~2.5 min past expiry: measures the enforcement LAG, not just
        // a single point (demo 2026-07-25: 12s past expiry was NOT enough).
        let mut survived = true;
        for i in 1..=10 {
            tokio::time::sleep(std::time::Duration::from_secs(15)).await;
            let after_exp =
                parse_resting_orders(&k.resting_orders(Some(&ticker)).await.unwrap());
            survived = after_exp.iter().any(|o| Some(&o.order_id) == oid2.as_ref());
            let elapsed = chrono::Utc::now().timestamp() - exp2;
            println!("    +{elapsed}s past expiry (poll {i}): survived={survived}");
            if !survived {
                break;
            }
        }
        println!("    FINAL: survived={survived} (expect false — AUTO-CANCELLED)");

        // (5) startup orphan sweep: cancel EVERY resting order, confirm none remain.
        let all = parse_resting_orders(&k.resting_orders(None).await.unwrap());
        println!("=== (5) ORPHAN SWEEP === {} resting to cancel", all.len());
        for o in &all {
            let _ = k.cancel_order(&o.order_id).await;
        }
        let remaining = parse_resting_orders(&k.resting_orders(None).await.unwrap());
        println!("    remaining after sweep: {} (expect 0)", remaining.len());
        println!("=== VERDICT === record HTTP/fill_count/survived/remaining above into the charter Decisions section.");
    }

    /// EMPIRICAL probe for the STREAK execution policy: the exact production
    /// sequence **rest → (deadline) cancel → IOC backstop**, end to end, on demo.
    /// Proves the five things the policy depends on and that unit tests cannot:
    ///   1. a `good_till_canceled` + future-`expiration_ts` BUY at the maker
    ///      price RESTS (201, fill_count 0, remaining == count, an order_id) —
    ///      it does NOT silently IOC;
    ///   2. the resting order is visible by ticker (sanity only — the policy
    ///      never treats this eventually-consistent list as truth);
    ///   3. CANCEL BY ID succeeds and its RAW response is printed, settling
    ///      whether the cancel response reports fill/remaining counts (the policy
    ///      treats the cancel response as truth and then re-polls fills anyway);
    ///   4. an IOC at a crossing limit, sent under a DISTINCT coid immediately
    ///      after the cancel, fills — i.e. the two legs never collide on the
    ///      duplicate-coid 409;
    ///   5. the RAW `/portfolio/fills` row for that fill is printed, settling
    ///      which fee field a fills row carries (the create-order response gives
    ///      `average_fee_paid`; a resting fill is only ever seen through /fills).
    /// Finally: nothing of ours remains resting on the ticker.
    ///
    /// Ignored (needs the DEMO account's key id + network). Run with:
    ///   KALSHI_API_BASE=https://demo-api.kalshi.co \
    ///   KALSHI_API_KEY_ID=<DEMO key id> KALSHI_PRIVATE_KEY_PATH=secrets/Demo.txt \
    ///   NESTOR_TEST_TICKER=<open demo ticker with a live ask> \
    ///   cargo test -p engine demo_streak_rest_cancel_ioc -- --ignored --nocapture
    #[tokio::test]
    #[ignore]
    async fn demo_streak_rest_cancel_ioc() {
        let key_id = std::env::var("KALSHI_API_KEY_ID").expect("KALSHI_API_KEY_ID (demo)");
        let key_path = std::env::var("KALSHI_PRIVATE_KEY_PATH").expect("KALSHI_PRIVATE_KEY_PATH");
        let ticker = std::env::var("NESTOR_TEST_TICKER").expect("NESTOR_TEST_TICKER");
        // The maker price and the crossing IOC limit. Defaults mirror production
        // (rest 40, take 46); override when the demo book sits elsewhere.
        let rest_px: i64 = std::env::var("NESTOR_TEST_REST_PX")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(40);
        let ioc_px: i64 = std::env::var("NESTOR_TEST_IOC_PX")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(46);
        let side = std::env::var("NESTOR_TEST_SIDE").unwrap_or_else(|_| "yes".into());
        let k = Kalshi::authenticated(key_id, &key_path).unwrap();

        let book = k.orderbook(&ticker).await.unwrap_or(serde_json::Value::Null);
        println!("=== BOOK BEFORE ===\n{}", serde_json::to_string(&book).unwrap());

        // (1) REST at the maker price, expiring at the end of a notional entry
        //     window (T0+60 in production).
        let t0 = chrono::Utc::now().timestamp();
        let exp = t0 + 60;
        let coid_m = format!("streak-{ticker}-m{rest_px}-probe{t0}");
        let (s1, b1, _) = k
            .place_resting_limit_raw(&ticker, &side, 1, rest_px, exp, &coid_m)
            .await
            .expect("resting POST");
        println!("=== (1) REST @{rest_px}c === HTTP {s1}\n{b1}");
        let placed = parse_place_response(
            &serde_json::from_str(&b1).unwrap_or(serde_json::Value::Null),
            &side,
        );
        println!(
            "    fill_count={} remaining={} order_id={:?} (expect 0 / 1 / Some — RESTED, not IOC)",
            placed.fill_count, placed.remaining_count, placed.order_id
        );
        let oid = placed.order_id.clone().expect("resting order_id");

        // (2) visible in the (eventually-consistent) resting list.
        let listed = parse_resting_orders(&k.resting_orders(Some(&ticker)).await.unwrap());
        println!(
            "=== (2) RESTING LIST === {} order(s); ours present: {}",
            listed.len(),
            listed.iter().any(|o| o.order_id == oid)
        );

        // (3) DEADLINE CANCEL — the raw response is the schema question.
        tokio::time::sleep(std::time::Duration::from_secs(5)).await;
        let cancel = k.cancel_order(&oid).await;
        match &cancel {
            Ok(v) => println!(
                "=== (3) CANCEL BY ID === RAW:\n{}",
                serde_json::to_string_pretty(v).unwrap()
            ),
            Err(e) => println!("=== (3) CANCEL BY ID === ERROR {e}"),
        }

        // (4) IOC BACKSTOP immediately after, distinct coid namespace.
        let coid_t = format!("streak-{ticker}-probe{t0}");
        let (s4, b4, _) = k
            .place_limit_buy_raw(&ticker, &side, 1, ioc_px, &coid_t)
            .await
            .expect("IOC POST");
        println!("=== (4) IOC @{ioc_px}c === HTTP {s4}\n{b4}");
        let took = parse_place_response(
            &serde_json::from_str(&b4).unwrap_or(serde_json::Value::Null),
            &side,
        );
        println!(
            "    fill_count={} price={:?} actual_fee_cents={:?} order_id={:?}",
            took.fill_count, took.fill_price_cents, took.actual_fee_cents, took.order_id
        );

        // (5) RAW fills row — which fee field does a FILL carry?
        if let Ok(fills) = k.fills(&ticker).await {
            let rows = fills
                .get("fills")
                .and_then(|f| f.as_array())
                .cloned()
                .unwrap_or_default();
            println!("=== (5) RAW FILLS (newest first, up to 2) ===");
            for r in rows.iter().take(2) {
                println!("{}", serde_json::to_string_pretty(r).unwrap());
            }
            let parsed = parse_fills(&fills, took.order_id.as_deref(), &side, 0);
            println!("    parsed for our IOC order: {parsed:?}");
        }

        // (6) nothing of ours left resting.
        let after = parse_resting_orders(&k.resting_orders(Some(&ticker)).await.unwrap());
        println!(
            "=== (6) AFTER === {} resting on {ticker}; ours still there: {} (expect false)",
            after.len(),
            after.iter().any(|o| o.order_id == oid)
        );
        println!("=== VERDICT === rest→cancel→IOC proven end-to-end; record the cancel + fills schemas above.");
    }
}

#[cfg(test)]
mod admin_probes {
    use super::*;

    /// Ignored operator probe: upgrade the account to Advanced tier (free,
    /// POST /account/api_usage_level/upgrade, needs 1 API order in last 100 —
    /// satisfied by tonight's selftest orders), then read /account/limits.
    #[tokio::test]
    #[ignore]
    async fn admin_upgrade_tier_and_read_limits() {
        let k = Kalshi::authenticated(
            std::env::var("KALSHI_API_KEY_ID").unwrap(),
            &std::env::var("KALSHI_PRIVATE_KEY_PATH").unwrap(),
        )
        .unwrap();
        for (method, path, is_post) in [
            ("POST", "/trade-api/v2/account/api_usage_level/upgrade", true),
            ("GET", "/trade-api/v2/account/limits", false),
        ] {
            let headers = k.sign_headers(method, path).unwrap();
            let url = format!("{}{}", api_base(), path);
            let mut req = if is_post {
                k.http.post(&url).json(&serde_json::json!({}))
            } else {
                k.http.get(&url)
            };
            for (h, v) in headers {
                req = req.header(h, v);
            }
            let resp = req.send().await.unwrap();
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            println!("{method} {path} -> {status}\n{body}\n");
        }
    }
}
