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
    pub yes_sub_title: Option<String>,
    #[serde(default)]
    pub result: Option<String>,
}

impl Market {
    /// YES ask in cents (0-100), or None if unpriced.
    pub fn yes_ask_cents(&self) -> Option<i64> {
        self.yes_ask_dollars
            .as_ref()
            .and_then(|s| s.parse::<f64>().ok())
            .map(|d| (d * 100.0).round() as i64)
    }
}

#[derive(Debug, Deserialize)]
struct MarketsResp {
    #[serde(default)]
    markets: Vec<Market>,
    #[serde(default)]
    cursor: Option<String>,
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
            http: reqwest::Client::new(),
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
            http: reqwest::Client::new(),
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

    /// Place a limit buy. `yes_price_cents` = YES price in cents (1-99). Signed.
    pub async fn place_limit_buy(
        &self,
        ticker: &str,
        side: &str,
        count: i64,
        yes_price_cents: i64,
        client_order_id: &str,
    ) -> Result<serde_json::Value> {
        let path = format!("{PREFIX}/portfolio/orders");
        let headers = self.sign_headers("POST", &path)?;
        let body = json!({
            "ticker": ticker, "action": "buy", "side": side,
            "type": "limit", "count": count,
            "yes_price": yes_price_cents, "client_order_id": client_order_id,
        });
        let mut req = self.http.post(format!("{BASE}{path}")).json(&body);
        for (k, v) in headers {
            req = req.header(k, v);
        }
        Ok(req.send().await?.error_for_status()?.json().await?)
    }
}
