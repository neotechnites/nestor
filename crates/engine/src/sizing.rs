//! Sizing. Weather sleeve = flat small dollars (thin markets), not % of bankroll.

pub fn contracts_for(stake_usd: f64, entry_price_cents: i64) -> i64 {
    if entry_price_cents <= 0 {
        return 0;
    }
    (stake_usd / (entry_price_cents as f64 / 100.0)).floor() as i64
}
