//! Live BTC spot + short-term volatility from Coinbase (public, no auth). Used by
//! the lock strategy to compute Z in real time.

use anyhow::Result;

const CANDLES: &str = "https://api.exchange.coinbase.com/products/BTC-USD/candles";

/// Recent BTC 1-min closes as (unix_secs, close), ascending. Coinbase returns
/// `[[time, low, high, open, close, vol], ...]` newest-first (up to 300 rows).
pub async fn recent_1min(http: &reqwest::Client) -> Result<Vec<(i64, f64)>> {
    let rows: Vec<Vec<f64>> = http
        .get(CANDLES)
        .query(&[("granularity", "60")])
        .header("User-Agent", "nestor")
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    let mut v: Vec<(i64, f64)> = rows
        .iter()
        .filter_map(|r| (r.len() >= 5).then_some((r[0] as i64, r[4])))
        .collect();
    v.sort_by_key(|x| x.0);
    Ok(v)
}

/// Latest spot (last close).
pub fn spot(candles: &[(i64, f64)]) -> Option<f64> {
    candles.last().map(|x| x.1)
}

/// Median absolute 1-min move over the last 15 COMPLETED minutes. The newest
/// candle is the in-progress minute (a partial move that would deflate the median
/// and inflate Z), so it's excluded from the volatility estimate — `spot()` still
/// uses it for the freshest price.
pub fn median_move(candles: &[(i64, f64)]) -> Option<f64> {
    let m = candles.len();
    if m < 17 {
        return None; // need 16 completed bars for 15 diffs, plus the partial one
    }
    let c = &candles[..m - 1]; // drop the in-progress bar
    let n = c.len();
    let mut diffs: Vec<f64> = (n - 15..n).map(|j| (c[j].1 - c[j - 1].1).abs()).collect();
    diffs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    Some(diffs[diffs.len() / 2])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spot_and_median() {
        let c: Vec<(i64, f64)> = (0..20).map(|i| (i * 60, 100.0 + i as f64)).collect();
        assert_eq!(spot(&c), Some(119.0));
        // every 1-min move is exactly 1.0 -> median 1.0
        assert_eq!(median_move(&c), Some(1.0));
        assert_eq!(median_move(&c[..10]), None); // <16 bars
    }
}
