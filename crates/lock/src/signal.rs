//! Lock-edge signal — pure, testable. `Z = |spot-strike| / (median_1min·√min_left)`;
//! a qualifying entry is a favorite priced 93–97¢, Z≥4, with the distance on the
//! favorite's side. Prices are ¢ as f64 (Kalshi's deep book is deci-cent). Used by
//! both the backtest and (later) the live sleeve.

/// How many "normal remaining moves" the underlying is clear of the strike.
/// 0.0 for degenerate inputs (no volatility estimate / no time left).
pub fn z_score(spot: f64, strike: f64, median_1min_move: f64, minutes_left: f64) -> f64 {
    if median_1min_move <= 0.0 || minutes_left <= 0.0 {
        return 0.0;
    }
    (spot - strike).abs() / (median_1min_move * minutes_left.sqrt())
}

#[derive(Debug, Clone, Copy)]
pub struct LockParams {
    pub z_min: f64,
    /// Favorite price band in ¢, inclusive lo, exclusive hi. Default [93, 97).
    pub price_lo: f64,
    pub price_hi: f64,
}

impl Default for LockParams {
    fn default() -> Self {
        LockParams {
            z_min: 4.0,
            price_lo: 93.0,
            price_hi: 97.0,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LockEntry {
    /// Favorite price in ¢ (max of yes / 100-yes).
    pub fav_price: f64,
    /// True if the favorite is the YES side.
    pub fav_is_yes: bool,
    pub z: f64,
}

/// Core check on the FAVORITE you would actually pay for. `fav_ask` = the ask ¢
/// of the favorite side (what the fill costs), deci-cent (f64) — NOT `100 -
/// yes_ask` (that is `no_bid`, not `no_ask`). `fav_is_yes` says which side leads.
pub fn evaluate_favorite(
    fav_ask: f64,
    fav_is_yes: bool,
    spot: f64,
    strike: f64,
    median_1min_move: f64,
    minutes_left: f64,
    p: &LockParams,
) -> Option<LockEntry> {
    // Band-gate on the price actually paid, before any rounding, so a 92.9¢
    // favorite is NOT admitted into [93,97) (below 93 the edge inverts, note 08).
    if fav_ask < p.price_lo || fav_ask >= p.price_hi {
        return None;
    }
    let z = z_score(spot, strike, median_1min_move, minutes_left);
    if z < p.z_min {
        return None;
    }
    // Distance must be on the favorite's side (don't buy a favorite the underlying
    // contradicts). YES favorite ⇔ spot above strike.
    if fav_is_yes != (spot > strike) {
        return None;
    }
    Some(LockEntry {
        fav_price: fav_ask,
        fav_is_yes,
        z,
    })
}

/// Backtest convenience: derive the favorite from a single YES price (the cached
/// tick data has trade prices, not separate yes/no asks). Live code uses
/// `evaluate_favorite` with the real ask.
pub fn evaluate(
    yes_price: f64,
    spot: f64,
    strike: f64,
    median_1min_move: f64,
    minutes_left: f64,
    p: &LockParams,
) -> Option<LockEntry> {
    let fav = yes_price.max(100.0 - yes_price);
    let fav_is_yes = yes_price > 50.0;
    evaluate_favorite(
        fav,
        fav_is_yes,
        spot,
        strike,
        median_1min_move,
        minutes_left,
        p,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn z_score_basic() {
        assert!((z_score(1040.0, 1000.0, 10.0, 1.0) - 4.0).abs() < 1e-9);
        assert_eq!(z_score(1040.0, 1000.0, 0.0, 1.0), 0.0);
        assert_eq!(z_score(1040.0, 1000.0, 10.0, 0.0), 0.0);
    }

    #[test]
    fn qualifies_yes_favorite_above_strike() {
        let e = evaluate(95.0, 1100.0, 1000.0, 10.0, 1.0, &LockParams::default()).unwrap();
        assert!((e.fav_price - 95.0).abs() < 1e-9);
        assert!(e.fav_is_yes);
        assert!(e.z >= 4.0);
    }

    #[test]
    fn qualifies_no_favorite_below_strike() {
        // YES 5¢ => NO favorite 95¢; spot far below strike.
        let e = evaluate(5.0, 900.0, 1000.0, 10.0, 1.0, &LockParams::default()).unwrap();
        assert!((e.fav_price - 95.0).abs() < 1e-9);
        assert!(!e.fav_is_yes);
    }

    #[test]
    fn rejects_price_out_of_band() {
        assert!(evaluate(99.0, 1100.0, 1000.0, 10.0, 1.0, &LockParams::default()).is_none());
        assert!(evaluate(90.0, 1100.0, 1000.0, 10.0, 1.0, &LockParams::default()).is_none());
    }

    #[test]
    fn rejects_low_z() {
        assert!(evaluate(95.0, 1010.0, 1000.0, 10.0, 1.0, &LockParams::default()).is_none());
    }

    #[test]
    fn favorite_gates_on_ask_and_decicent_floor() {
        // NO favorite priced at its 95¢ ask, spot below strike -> qualifies at 95.
        let e = evaluate_favorite(
            95.0,
            false,
            900.0,
            1000.0,
            10.0,
            1.0,
            &LockParams::default(),
        )
        .unwrap();
        assert!(!e.fav_is_yes);
        assert!((e.fav_price - 95.0).abs() < 1e-9);
        // 92.9¢ favorite is below the 93 floor (integer rounding would wrongly admit 93).
        assert!(evaluate_favorite(
            92.9,
            true,
            1100.0,
            1000.0,
            10.0,
            1.0,
            &LockParams::default()
        )
        .is_none());
    }

    #[test]
    fn rejects_distance_on_wrong_side() {
        // YES favorite 95¢ but spot below strike -> contradicts.
        assert!(evaluate(95.0, 900.0, 1000.0, 10.0, 1.0, &LockParams::default()).is_none());
    }
}
