//! Sizing. Weather sleeve = flat small dollars (thin markets), not % of bankroll.

/// Contracts affordable for `stake_usd` at `entry_price_cents`. Computed in
/// integer cents (floor division) so there's no float-truncation surprise on a
/// value that decides how much money is at risk.
pub fn contracts_for(stake_usd: f64, entry_price_cents: i64) -> i64 {
    if entry_price_cents <= 0 {
        return 0;
    }
    let stake_cents = (stake_usd * 100.0).round() as i64;
    if stake_cents <= 0 {
        return 0;
    }
    stake_cents / entry_price_cents
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integer_floor_division() {
        assert_eq!(contracts_for(50.0, 95), 52); // 5000c / 95c = 52 (95*53=5035 > 5000)
        assert_eq!(contracts_for(10.0, 50), 20);
        assert_eq!(contracts_for(1.0, 99), 1);
        assert_eq!(contracts_for(0.5, 99), 0); // 50c / 99c = 0
    }

    #[test]
    fn nonpositive_inputs_are_zero() {
        assert_eq!(contracts_for(10.0, 0), 0);
        assert_eq!(contracts_for(10.0, -5), 0);
        assert_eq!(contracts_for(0.0, 50), 0);
        assert_eq!(contracts_for(-10.0, 50), 0);
    }
}
