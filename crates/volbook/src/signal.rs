//! Volbook signal — pure, testable. Decide whether to SELL a metal daily wing
//! (buy NO on a systematically-rich OTM rung) and at what limit.
//!
//! Mechanism (verdict: work/verify-b9-widened.md): thin daily metal ladders quote
//! a ~flat ~13% OTM touch premium Mon-Wed that realizes only ~3.6% — so the wing's
//! NO leg is systematically cheap. We buy NO in the band where YES ∈ [0.05, 0.35),
//! near T-3h, on Mon-Wed only.
//!
//! LIMIT = WILLINGNESS-TO-PAY, not a transcription of the book (enchiridion 15).
//! The ceiling is derived from the edge boundary: fair NO value is
//! (1 − realized_touch); we will pay NO no more than that minus fee minus a margin
//! (the guaranteed per-contract EV floor). A NO ask above the ceiling has no edge
//! left and is not traded. An IOC placed at the ceiling still fills at the resting
//! ask when the ask is below it (price improvement), so the ceiling only bounds
//! the worst price we accept — it never worsens a good fill.

use crate::calib::Family;

/// Parameters lifted from the calibration artifact for one evaluation.
#[derive(Debug, Clone, Copy)]
pub struct Params {
    pub wing_lo: f64,
    pub wing_hi: f64,
    pub ttc_lo: i64,
    pub ttc_hi: i64,
    pub margin_cents: f64,
}

/// A qualifying wing-sell (buy NO).
#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    /// Willingness-to-pay ceiling in ¢ (the limit we place; the edge boundary).
    pub ceiling_cents: i64,
    /// Observed NO ask in ¢ (the price we expect to actually pay).
    pub no_ask_cents: i64,
    /// Implied YES (band-membership price), percent 0..100.
    pub implied_pct: f64,
    /// Calibrated realized touch (fraction) for this rung's bucket.
    pub touch: f64,
    /// Calibration gap in pp (implied − realized) — the ranking key.
    pub gap_pp: f64,
    /// Expected EV per contract (¢) at the observed ask, using calibrated touch.
    pub ev_at_ask_cents: f64,
}

/// Why a rung produced no wing-sell.
#[derive(Debug, Clone, PartialEq)]
pub enum Skip {
    /// Weekday not in the Mon-Wed gate.
    NotTradingDay { weekday: u32 },
    /// Outside the T-3h entry window.
    NotEntryWindow { ttc: i64 },
    /// No NO ask (can't buy NO).
    Unpriced,
    /// Implied YES outside the wing band [wing_lo, wing_hi).
    OutOfBand { implied_pct: f64 },
    /// No calibrated bucket for this implied (should not happen inside the band).
    NoCalib,
    /// NO ask above the willingness-to-pay ceiling — no edge left; do not chase.
    NoEdge { ceiling_cents: i64, no_ask_cents: i64 },
}

impl Skip {
    pub fn as_str(&self) -> String {
        match self {
            Skip::NotTradingDay { weekday } => format!("not_trading_day(wd={weekday})"),
            Skip::NotEntryWindow { ttc } => format!("not_entry_window(ttc={ttc}s)"),
            Skip::Unpriced => "unpriced".into(),
            Skip::OutOfBand { implied_pct } => format!("out_of_band(impl={implied_pct:.1})"),
            Skip::NoCalib => "no_calib".into(),
            Skip::NoEdge {
                ceiling_cents,
                no_ask_cents,
            } => format!("no_edge(ask={no_ask_cents} > ceiling={ceiling_cents})"),
        }
    }
}

/// Kalshi taker fee in ¢/contract at NO price fraction `p`: 0.07·p·(1−p)·100.
/// (Per-contract, un-ceiled — the ceil-per-order billing lives in the risk layer;
/// here we only need a cents-scale fee to set the edge boundary.)
fn fee_cents(p: f64) -> f64 {
    7.0 * p * (1.0 - p)
}

/// Band-membership implied YES (percent 0..100) from the two exposed asks. The
/// market's mid YES = midpoint of the YES ask and the YES bid (= 100 − NO ask).
/// With only one side priced, fall back to that side. `None` if neither is priced.
pub fn implied_yes_pct(yes_ask: Option<f64>, no_ask: Option<f64>) -> Option<f64> {
    match (yes_ask, no_ask) {
        (Some(ya), Some(na)) => Some((ya + (100.0 - na)) / 2.0),
        (Some(ya), None) => Some(ya),
        (None, Some(na)) => Some(100.0 - na),
        (None, None) => None,
    }
}

/// Willingness-to-pay ceiling (¢) for buying NO given the calibrated realized
/// `touch` (fraction) and a margin (¢). Fair NO value is (1 − touch)·100¢; the
/// ceiling is that minus the fee (evaluated at the fair NO price) minus the
/// margin, so any fill at or below it clears at least `margin` ¢ of EV on the
/// calibrated distribution. Floored to a whole cent.
pub fn willingness_to_pay_cents(touch: f64, margin_cents: f64) -> i64 {
    let fair_no = 1.0 - touch; // NO fair price, fraction
    let ceiling = fair_no * 100.0 - fee_cents(fair_no) - margin_cents;
    ceiling.floor() as i64
}

/// Evaluate one metal daily rung for a wing-sell. `weekday` is Mon=0..Sun=6 (from
/// the market's close time in ET); `ttc` is seconds to close.
pub fn evaluate(
    p: &Params,
    weekday_gate: &[u32],
    weekday: u32,
    ttc: i64,
    yes_ask: Option<f64>,
    no_ask: Option<f64>,
    fam: &Family,
) -> Result<Entry, Skip> {
    if !weekday_gate.contains(&weekday) {
        return Err(Skip::NotTradingDay { weekday });
    }
    if !(p.ttc_lo..=p.ttc_hi).contains(&ttc) {
        return Err(Skip::NotEntryWindow { ttc });
    }
    // We buy NO — a NO ask is mandatory.
    let na = match no_ask {
        Some(a) => a,
        None => return Err(Skip::Unpriced),
    };
    let implied_pct = match implied_yes_pct(yes_ask, no_ask) {
        Some(x) => x,
        None => return Err(Skip::Unpriced),
    };
    let implied_frac = implied_pct / 100.0;
    if !(implied_frac >= p.wing_lo && implied_frac < p.wing_hi) {
        return Err(Skip::OutOfBand { implied_pct });
    }
    let touch = match fam.bucket_touch(implied_frac) {
        Some(t) => t,
        None => return Err(Skip::NoCalib),
    };
    let ceiling_cents = willingness_to_pay_cents(touch, p.margin_cents);
    let no_ask_cents = na.round() as i64;
    if no_ask_cents > ceiling_cents {
        return Err(Skip::NoEdge {
            ceiling_cents,
            no_ask_cents,
        });
    }
    // EV at the price we'd actually pay (the resting ask), on calibrated touch.
    let ask_frac = no_ask_cents as f64 / 100.0;
    let ev_at_ask_cents = (1.0 - touch) * 100.0 - no_ask_cents as f64 - fee_cents(ask_frac);
    Ok(Entry {
        ceiling_cents,
        no_ask_cents,
        implied_pct,
        touch,
        gap_pp: implied_pct - touch * 100.0,
        ev_at_ask_cents,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::calib::Bucket;

    fn fam(touch: f64) -> Family {
        Family {
            enabled: true,
            implied_touch: 0.137,
            realized_touch: 0.036,
            gap_pp: 10.1,
            ev_cents: Some(8.61),
            series: Default::default(),
            buckets: vec![Bucket {
                lo: 0.05,
                hi: 0.35,
                n: 336,
                touch_obs: touch,
                implied_mid: 0.137,
                touch,
            }],
        }
    }

    fn params() -> Params {
        Params {
            wing_lo: 0.05,
            wing_hi: 0.35,
            ttc_lo: 9000,
            ttc_hi: 12600,
            margin_cents: 2.0,
        }
    }

    #[test]
    fn willingness_to_pay_from_edge_boundary() {
        // touch 3.6% -> fair NO 96.4¢; fee(0.964)=0.243¢; margin 2 -> floor(94.16)=94.
        assert_eq!(willingness_to_pay_cents(0.036, 2.0), 94);
        // touch 24% -> fair NO 76¢; fee(0.76)=1.277¢; margin 2 -> floor(72.72)=72.
        assert_eq!(willingness_to_pay_cents(0.24, 2.0), 72);
    }

    #[test]
    fn implied_mid_of_two_asks() {
        // yes_ask 14, no_ask 88 -> yes_bid 12 -> mid 13.
        assert_eq!(implied_yes_pct(Some(14.0), Some(88.0)), Some(13.0));
        assert_eq!(implied_yes_pct(None, Some(88.0)), Some(12.0));
        assert_eq!(implied_yes_pct(Some(14.0), None), Some(14.0));
        assert_eq!(implied_yes_pct(None, None), None);
    }

    #[test]
    fn qualifying_wing_sell_uses_ceiling_and_reports_edge() {
        // Rich wing: implied ~13%, realized 3.6%. NO ask 87 (< ceiling 94) -> Entry.
        let e = evaluate(
            &params(),
            &[0, 1, 2],
            1, // Tue
            10_800,
            Some(14.0),
            Some(88.0),
            &fam(0.036),
        )
        .unwrap();
        // implied mid = (14 + 12)/2 = 13
        assert!((e.implied_pct - 13.0).abs() < 1e-9);
        assert_eq!(e.ceiling_cents, 94);
        assert_eq!(e.no_ask_cents, 88);
        assert!((e.gap_pp - (13.0 - 3.6)).abs() < 1e-9);
        // EV at ask 88: (1-.036)*100 - 88 - fee(.88) ≈ 96.4 - 88 - 0.739 ≈ +7.66
        assert!(e.ev_at_ask_cents > 6.0 && e.ev_at_ask_cents < 9.0);
    }

    #[test]
    fn no_edge_when_ask_above_ceiling() {
        // A near-fair rung: touch 24% -> ceiling 72. NO ask 80 > 72 -> NoEdge.
        let err = evaluate(
            &params(),
            &[0, 1, 2],
            2,
            10_800,
            Some(20.0), // implied mid = (20 + (100-80))/2 = 20
            Some(80.0),
            &fam(0.24),
        )
        .unwrap_err();
        assert_eq!(
            err,
            Skip::NoEdge {
                ceiling_cents: 72,
                no_ask_cents: 80
            }
        );
    }

    #[test]
    fn gated_off_weekday_and_window() {
        // Thursday -> not a trading day.
        assert_eq!(
            evaluate(&params(), &[0, 1, 2], 3, 10_800, Some(14.0), Some(88.0), &fam(0.036)),
            Err(Skip::NotTradingDay { weekday: 3 })
        );
        // Tue but ttc far outside the T-3h window.
        assert_eq!(
            evaluate(&params(), &[0, 1, 2], 1, 3_600, Some(14.0), Some(88.0), &fam(0.036)),
            Err(Skip::NotEntryWindow { ttc: 3_600 })
        );
    }

    #[test]
    fn out_of_band_and_unpriced() {
        // Implied 45% -> above the wing band.
        assert_eq!(
            evaluate(&params(), &[0, 1, 2], 1, 10_800, Some(46.0), Some(56.0), &fam(0.036)),
            Err(Skip::OutOfBand { implied_pct: 45.0 })
        );
        // No NO ask -> can't buy NO.
        assert_eq!(
            evaluate(&params(), &[0, 1, 2], 1, 10_800, Some(14.0), None, &fam(0.036)),
            Err(Skip::Unpriced)
        );
    }
}
