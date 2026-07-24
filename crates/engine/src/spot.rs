//! Live crypto spot from Coinbase (public ticker endpoint, keyless REST). Used by
//! the streak sleeve to DERIVE a just-closed 15-min window's result at close+0s,
//! before Kalshi publishes the official result (post-close progression: closed
//! (0-10s) → finalized+result (~10s) → settled-filter (36s+); measured
//! 2026-07-24). Kalshi crypto settles on a 60-second BRTI average ending at
//! close, so a per-second spot sample stream over the final minute reconstructs
//! that average closely enough to call the outcome ahead of the REST result.
//!
//! Adapted from `lock::coinbase` (candles) — this is the leaner single-price
//! variant: the `/ticker` endpoint returns just the last-trade price, one cheap
//! GET, which is all the sampler needs once per second.

use anyhow::Result;

fn ticker_url(product: &str) -> String {
    format!("https://api.exchange.coinbase.com/products/{product}/ticker")
}

/// Latest spot price for a Coinbase product (e.g. "BTC-USD", "ETH-USD"). One
/// keyless GET against the public exchange ticker; returns the last-trade price.
/// Cheap enough to poll at 1 Hz. Best-effort by contract: callers treat an error
/// as "no sample this tick", never as a fatal.
pub async fn spot_price(http: &reqwest::Client, product: &str) -> Result<f64> {
    #[derive(serde::Deserialize)]
    struct Ticker {
        price: String,
    }
    let t: Ticker = http
        .get(ticker_url(product))
        .header("User-Agent", "nestor")
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    Ok(t.price.parse::<f64>()?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_shape() {
        assert_eq!(
            ticker_url("BTC-USD"),
            "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
        );
    }
}
