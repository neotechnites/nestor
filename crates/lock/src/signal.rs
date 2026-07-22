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

/// Evaluate one checkpoint. `Some(entry)` if it qualifies, else `None`.
/// `yes_price` = the YES contract price in ¢ (0–100). `minutes_left` = seconds
/// to close / 60.
pub fn evaluate(
    yes_price: f64,
    spot: f64,
    strike: f64,
    median_1min_move: f64,
    minutes_left: f64,
    p: &LockParams,
) -> Option<LockEntry> {
    let fav = yes_price.max(100.0 - yes_price);
    if fav < p.price_lo || fav >= p.price_hi {
        return None;
    }
    let z = z_score(spot, strike, median_1min_move, minutes_left);
    if z < p.z_min {
        return None;
    }
    // Distance must be on the favorite's side (don't buy a favorite the underlying
    // contradicts). YES favorite ⇔ spot above strike.
    let fav_is_yes = yes_price > 50.0;
    let leader_up = spot > strike;
    if fav_is_yes != leader_up {
        return None;
    }
    Some(LockEntry {
        fav_price: fav,
        fav_is_yes,
        z,
    })
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
    fn rejects_distance_on_wrong_side() {
        // YES favorite 95¢ but spot below strike -> contradicts.
        assert!(evaluate(95.0, 900.0, 1000.0, 10.0, 1.0, &LockParams::default()).is_none());
    }
}
