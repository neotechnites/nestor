//! Pure house-probe logic — everything decidable WITHOUT the network, so the
//! quote loop's math (gates, quote prices, markout, the −$20 / −5¢ stops, and the
//! report metrics) is unit-tested offline. The networked orchestration lives in
//! `strategy.rs`; it only calls into here.

use std::collections::HashMap;

use engine::risk::Side;

/// Resting-order lifetime in seconds (charter §1: "≈75s"). A dead process leaves
/// nothing resting beyond this — the load-bearing safety property. `expiration_ts`
/// on every order = now + this.
pub const ORDER_TTL_SECS: i64 = 75;
/// Minimum book spread to quote inside (protocol §1). At 1¢ the edge collapses.
pub const MIN_SPREAD_CENTS: i64 = 2;
/// Catalyst pull half-width (protocol §2): pull all quotes T±15min.
pub const CATALYST_HALF_WIDTH_SECS: i64 = 15 * 60;
/// Re-quote when the mid has moved at least this much from our quoted mid.
pub const REQUOTE_MID_MOVE_CENTS: i64 = 1;
/// Active re-quote staleness (protocol §Re-quote (c)); expiration_ts also handles
/// this passively.
pub const REQUOTE_STALE_SECS: i64 = 60;
/// Markout horizon for the gap-through detector (protocol metric 3 / stop).
pub const MARKOUT_HORIZON_SECS: i64 = 60;
/// A single fill marked out worse than this within the horizon trips the stop
/// (protocol Hard stop) AND counts as a gap-through (metric 3 uses ≥3¢ against).
pub const GAP_THROUGH_STOP_CENTS: f64 = -5.0;
/// Gap-through classification threshold for metric 3 (mid moved ≥3¢ against).
pub const GAP_THROUGH_METRIC_CENTS: f64 = -3.0;
/// Cumulative probe hard stop in cents (charter §3: −$20).
pub const HARD_STOP_CENTS: f64 = -2000.0;

/// The two legs of a two-sided resting quote around `mid` (whole cents). Both are
/// expressed via the taker call boundary (our-side yes/no + our-side price):
///   - bid leg = buy YES at mid−1  → posts as a YES-book bid at (mid−1)/100
///   - ask leg = buy NO  at 99−mid → posts as a YES-book ask at (mid+1)/100
///
/// So a filled bid leaves us long YES @ mid−1; a filled ask leaves us long NO @
/// 99−mid — the two legs straddle the mid by ±1¢ exactly as the protocol requires.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct QuoteLegs {
    pub bid_price_cents: i64,
    pub ask_price_cents: i64,
}

impl QuoteLegs {
    /// (side, our-side price) for the YES-buy (bid) leg.
    pub fn bid(&self) -> (Side, i64) {
        (Side::Yes, self.bid_price_cents)
    }
    /// (side, our-side price) for the NO-buy (ask) leg.
    pub fn ask(&self) -> (Side, i64) {
        (Side::No, self.ask_price_cents)
    }
}

/// Quote legs around `mid`: YES bid at mid−1, NO buy at 99−mid (= YES ask at
/// mid+1). Prices clamped to the risk-tradeable band [3,97] so the risk layer's
/// `PriceOutOfBand` (≤2 / ≥98) never rejects a valid in-band quote.
pub fn quote_legs(mid: i64) -> QuoteLegs {
    let clamp = |c: i64| c.clamp(3, 97);
    QuoteLegs {
        bid_price_cents: clamp(mid - 1),
        ask_price_cents: clamp(99 - mid),
    }
}

/// Rough YES mid (whole cents) from a market's quoted asks: yes_bid = 100 −
/// no_ask, mid = (yes_ask + yes_bid)/2. Used for in-band VEHICLE SELECTION only
/// (the actual quote mid comes from the live orderbook). None if either ask is
/// missing.
pub fn market_mid_cents(yes_ask: Option<f64>, no_ask: Option<f64>) -> Option<i64> {
    match (yes_ask, no_ask) {
        (Some(ya), Some(na)) => Some(((ya + (100.0 - na)) / 2.0).round() as i64),
        _ => None,
    }
}

/// Spread gate (protocol §1): only quote when best_ask − best_bid ≥ 2¢.
pub fn spread_ok(best_bid: Option<i64>, best_ask: Option<i64>) -> bool {
    matches!((best_bid, best_ask), (Some(b), Some(a)) if a - b >= MIN_SPREAD_CENTS)
}

/// Catalyst gate (protocol §2): true when `now` is within ±15min of ANY scheduled
/// catalyst timestamp — pull all quotes. Empty list → never pulled by schedule.
pub fn in_catalyst_window(now: i64, catalysts: &[i64]) -> bool {
    catalysts
        .iter()
        .any(|&t| (now - t).abs() <= CATALYST_HALF_WIDTH_SECS)
}

/// Re-quote decision (protocol §Re-quote): re-post when we have no live quote,
/// the mid moved ≥1¢ from what we quoted around, or the quote is ≥60s stale.
pub fn should_requote(quoted_mid: Option<i64>, current_mid: i64, quote_age_secs: i64) -> bool {
    match quoted_mid {
        None => true,
        Some(q) => {
            (current_mid - q).abs() >= REQUOTE_MID_MOVE_CENTS || quote_age_secs >= REQUOTE_STALE_SECS
        }
    }
}

/// Markout in cents (signed, + = favorable) of a maker BUY fill valued at the
/// current mid. Long YES bought at `entry`: value ≈ mid, markout = mid − entry.
/// Long NO bought at `entry` (our NO price): value ≈ 100 − mid, markout =
/// (100 − mid) − entry. This is the between-print risk the trade-print markout
/// can't see (protocol metric 3).
pub fn markout_cents(side: Side, entry_cents: i64, mid_now: i64) -> f64 {
    let value = match side {
        Side::Yes => mid_now as f64,
        Side::No => (100 - mid_now) as f64,
    };
    value - entry_cents as f64
}

/// One filled leg awaiting its +60s markout (the gap-through measurement).
#[derive(Debug, Clone, PartialEq)]
pub struct OpenFill {
    pub ticker: String,
    pub side: Side,
    pub count: i64,
    pub entry_cents: i64,
    pub fee_cents: f64,
    pub ts_ms: i64,
}

/// The probe's own intraday cent-ledger for the −$20 stop and metrics. Kept in the
/// house crate (NOT the shared RiskManager, which settles on binary outcome, not
/// intraday round-trip markout). `realized_cents` accrues as legs are closed;
/// unrealized is marked at the current mid; fees accrue on every fill.
#[derive(Debug, Clone, Default)]
pub struct ProbeLedger {
    pub realized_cents: f64,
    pub fees_cents: f64,
    pub open: Vec<OpenFill>,
    pub fills_total: i64,
}

impl ProbeLedger {
    /// Book a maker fill: fee accrues now, the leg joins `open` to be marked/closed.
    pub fn on_fill(
        &mut self,
        ticker: &str,
        side: Side,
        count: i64,
        entry_cents: i64,
        fee_cents: f64,
        ts_ms: i64,
    ) {
        self.fees_cents += fee_cents;
        self.fills_total += count;
        self.open.push(OpenFill {
            ticker: ticker.to_string(),
            side,
            count,
            entry_cents,
            fee_cents,
            ts_ms,
        });
    }

    /// Unrealized cents of all open legs, each valued at ITS OWN market's mid from
    /// `mids` (ticker → mid cents). A leg whose ticker is absent from the map is
    /// valued at its entry (0 markout) — conservative for a stop, never inflates.
    pub fn unrealized_cents(&self, mids: &HashMap<String, i64>) -> f64 {
        self.open
            .iter()
            .map(|f| {
                let mid = mids.get(&f.ticker).copied().unwrap_or(f.entry_cents_for_mark());
                markout_cents(f.side, f.entry_cents, mid) * f.count as f64
            })
            .sum()
    }

    /// Total probe P&L in cents, NET of accrued fees. The number the −$20 hard
    /// stop compares against HARD_STOP_CENTS.
    pub fn total_pnl_cents(&self, mids: &HashMap<String, i64>) -> f64 {
        self.realized_cents + self.unrealized_cents(mids) - self.fees_cents
    }

    /// True if cumulative probe P&L has breached the −$20 hard stop.
    pub fn hard_stop_breached(&self, mids: &HashMap<String, i64>) -> bool {
        self.total_pnl_cents(mids) <= HARD_STOP_CENTS
    }
}

impl OpenFill {
    /// The mid that yields a 0 markout for this leg (used when the market's live
    /// mid is unknown): YES → entry, NO → 100 − entry.
    fn entry_cents_for_mark(&self) -> i64 {
        match self.side {
            Side::Yes => self.entry_cents,
            Side::No => 100 - self.entry_cents,
        }
    }
}

/// Gap-through stop (protocol Hard stop): a single fill marked out worse than −5¢
/// within the 60s horizon. `age_ms` is the fill's age; only fires once past a
/// meaningful fraction of the horizon isn't required — a −5¢ markout at any point
/// within 60s trips it.
pub fn gap_through_stop(markout: f64, age_secs: i64) -> bool {
    age_secs <= MARKOUT_HORIZON_SECS && markout <= GAP_THROUGH_STOP_CENTS
}

/// Metric-3 gap-through classification: mid moved ≥3¢ against within 60s.
pub fn is_gap_through(markout_at_60s: f64) -> bool {
    markout_at_60s <= GAP_THROUGH_METRIC_CENTS
}

/// The four protocol metrics, computed from the participation log (see
/// `report.rs`). All rates are per the observed quote-minutes / fills in the file.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Metrics {
    pub quote_minutes: f64,
    pub fills: i64,
    /// Metric 1: fills per quote-hour.
    pub fill_rate_per_hour: f64,
    /// Metric 2: realized avg half-spread capture (cents) on round-trips.
    pub avg_half_spread_cents: Option<f64>,
    /// Metric 3: fraction of fills that gapped through (≥3¢ against in 60s).
    pub gap_through_frac: f64,
    /// Metric 4: fraction of adverse fills that landed in a catalyst window.
    pub adverse_in_catalyst_frac: Option<f64>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quote_legs_straddle_mid_by_one_cent() {
        let q = quote_legs(50);
        // bid leg: buy YES @ 49; ask leg: buy NO @ 49 (= YES ask @ 51).
        assert_eq!(q.bid(), (Side::Yes, 49));
        assert_eq!(q.ask(), (Side::No, 49));
        // mid 40 -> YES bid 39, NO buy 59 (YES ask 41).
        let q = quote_legs(40);
        assert_eq!(q.bid_price_cents, 39);
        assert_eq!(q.ask_price_cents, 59);
    }

    #[test]
    fn quote_legs_clamped_into_tradeable_band() {
        // extreme mids can't produce a ≤2 / ≥98 price the risk layer would reject.
        let q = quote_legs(3);
        assert!(q.bid_price_cents >= 3 && q.ask_price_cents <= 97);
        let q = quote_legs(99);
        assert!(q.bid_price_cents >= 3 && q.ask_price_cents >= 3);
    }

    #[test]
    fn market_mid_from_asks() {
        // yes_ask 52, no_ask 49 -> yes_bid 51, mid (52+51)/2 = 51.5 -> 52.
        assert_eq!(market_mid_cents(Some(52.0), Some(49.0)), Some(52));
        assert_eq!(market_mid_cents(None, Some(49.0)), None);
    }

    #[test]
    fn spread_gate_needs_two_cents() {
        assert!(spread_ok(Some(48), Some(50))); // 2c
        assert!(spread_ok(Some(45), Some(50))); // 5c
        assert!(!spread_ok(Some(49), Some(50))); // 1c -> stand down
        assert!(!spread_ok(None, Some(50)));
        assert!(!spread_ok(Some(49), None));
    }

    #[test]
    fn catalyst_window_is_pm_fifteen_minutes() {
        let t = 1_800_000_000;
        assert!(in_catalyst_window(t, &[t]));
        assert!(in_catalyst_window(t + 899, &[t])); // just inside +15m
        assert!(!in_catalyst_window(t + 901, &[t])); // just outside
        assert!(!in_catalyst_window(t, &[])); // no scheduled catalysts
    }

    #[test]
    fn requote_triggers() {
        assert!(should_requote(None, 50, 0)); // no live quote
        assert!(should_requote(Some(50), 51, 5)); // mid moved 1c
        assert!(should_requote(Some(50), 50, 60)); // stale 60s
        assert!(!should_requote(Some(50), 50, 10)); // steady -> hold
    }

    #[test]
    fn markout_signs_are_correct() {
        // Long YES @ 49, mid rises to 52 -> +3c favorable.
        assert_eq!(markout_cents(Side::Yes, 49, 52), 3.0);
        // Long YES @ 49, mid falls to 45 -> -4c.
        assert_eq!(markout_cents(Side::Yes, 49, 45), -4.0);
        // Long NO @ 49 (bought at 49 NO), mid falls to 45 -> NO value 55, +6c.
        assert_eq!(markout_cents(Side::No, 49, 45), 6.0);
        // Long NO @ 49, mid rises to 55 -> NO value 45, -4c.
        assert_eq!(markout_cents(Side::No, 49, 55), -4.0);
    }

    fn mids(t: &str, m: i64) -> HashMap<String, i64> {
        let mut h = HashMap::new();
        h.insert(t.to_string(), m);
        h
    }

    #[test]
    fn ledger_pnl_nets_fees_and_marks_open() {
        let mut l = ProbeLedger::default();
        // Buy 1 YES @ 49, 1c fee. At mid 52 -> +3c mark, net of 1c fee = +2c.
        l.on_fill("T", Side::Yes, 1, 49, 1.0, 0);
        assert_eq!(l.fills_total, 1);
        assert!((l.total_pnl_cents(&mids("T", 52)) - 2.0).abs() < 1e-9);
        // Adverse mark: mid 45 -> -4c mark, net of 1c fee = -5c.
        assert!((l.total_pnl_cents(&mids("T", 45)) - (-5.0)).abs() < 1e-9);
        // Unknown ticker -> valued at entry (0 markout), only the 1c fee shows.
        assert!((l.total_pnl_cents(&HashMap::new()) - (-1.0)).abs() < 1e-9);
    }

    #[test]
    fn hard_stop_at_twenty_dollars() {
        let mut l = ProbeLedger {
            realized_cents: -2000.0,
            ..Default::default()
        };
        assert!(l.hard_stop_breached(&HashMap::new()));
        l.realized_cents = -1999.0;
        // no open legs, no fees -> -1999 > -2000, not breached.
        assert!(!l.hard_stop_breached(&HashMap::new()));
    }

    #[test]
    fn gap_through_stop_and_metric() {
        assert!(gap_through_stop(-5.0, 30)); // -5c within 60s -> stop
        assert!(!gap_through_stop(-5.0, 61)); // past horizon
        assert!(!gap_through_stop(-4.0, 30)); // not deep enough
        assert!(is_gap_through(-3.0)); // metric: 3c against
        assert!(!is_gap_through(-2.0));
    }
}
