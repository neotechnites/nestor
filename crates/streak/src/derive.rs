//! Derive a just-closed 15-min window's result from our own spot samples — the
//! "4th result" the streak entry needs while it is still inside the new market's
//! 60s entry window (derive-fourth, 2026-07-24).
//!
//! WHY THIS EXISTS. Kalshi's REST result lags the close: post-close a market
//! progresses closed (0-10s) → finalized + `result` (~10s) → included by the
//! `status=settled` filter (36s+). Even the ~10s finalized result can arrive
//! after we've missed the fast-moving entry price. Kalshi crypto settles on a
//! 60-SECOND BRTI AVERAGE ending at close. We sample spot once per second across
//! that final minute (see `engine::spot`) and reconstruct the average ourselves
//! at close+0s, comparing it to the window's own strike to call yes/no.
//!
//! This module is PURE and unit-tested: no I/O, no clock. It takes a slice of
//! `(unix_secs, price)` samples, the window's `floor_strike`, and the window's
//! `close_unix`, and returns a [`Derivation`]. Everything decisive is gated on a
//! sample-count + span floor and a decisiveness margin so we never synthesize a
//! result from thin or borderline data.

/// Decisiveness margin: a derivation is only decisive when the reconstructed
/// average sits at least this fraction away from the strike. 0.0005 = 5 basis
/// points. RATIONALE: our 1 Hz Coinbase spot stream is not the exact BRTI print
/// Kalshi settles on (different venue mix, sub-second timing, our sampling
/// jitter), so a coin that lands within a few bp of the strike is a genuine
/// coin-flip we must NOT call — near the strike our reconstruction error and the
/// true outcome are the same order of magnitude. 5 bp is a deliberately
/// conservative STARTING value; it will be CALIBRATED from the derived-vs-official
/// agreement history (`data/derive_verify.jsonl`) once enough closes accumulate.
pub const DERIVE_MARGIN: f64 = 0.0005;

/// Length of Kalshi's crypto settlement averaging window (seconds ending at
/// close).
pub const AVG_WINDOW_SECS: i64 = 60;

/// Minimum in-window samples required to derive. Below this the 60s average is
/// under-sampled and we refuse (INSUFFICIENT). At ~1 Hz this is 40 of a possible
/// ~60 samples — tolerates dropped Coinbase ticks but demands real coverage.
pub const MIN_SAMPLES: usize = 40;

/// Minimum span (last − first sample timestamp, seconds) the in-window samples
/// must cover. Guards against 40 samples all clustered in a few seconds (e.g. a
/// burst after a stall): we require the samples to actually straddle the minute.
pub const MIN_SPAN_SECS: i64 = 50;

/// Outcome of a derivation attempt.
#[derive(Debug, Clone, PartialEq)]
pub enum Derivation {
    /// Decisive: `result` is "yes" or "no" (yes = average strictly above strike).
    /// `avg` is the reconstructed 60s average; `margin_bp` its distance from the
    /// strike in basis points (always ≥ `DERIVE_MARGIN`×10000 here).
    Derived {
        result: &'static str,
        avg: f64,
        margin_bp: f64,
    },
    /// Enough samples, but the average is within the margin of the strike — a
    /// coin-flip we decline to call. `avg`/`margin_bp` recorded for logging.
    Marginal { avg: f64, margin_bp: f64 },
    /// Not enough coverage of the settlement window to attempt a call.
    Insufficient { samples: usize, span_secs: i64 },
}

/// Basis-points distance of `avg` from `strike` (unsigned).
fn margin_bp(avg: f64, strike: f64) -> f64 {
    ((avg - strike).abs() / strike) * 10_000.0
}

/// Derive the result for the window closing at `close_unix` from `samples`
/// (`(unix_secs, price)`, any order). Only samples in the settlement window
/// `[close_unix - AVG_WINDOW_SECS, close_unix]` are used.
///
/// Requires `strike > 0`. Averaging is a simple arithmetic mean of the in-window
/// samples: at our ~1 Hz uniform cadence the arithmetic mean is the time-average
/// of the 60s window to within our sampling jitter, and the `DERIVE_MARGIN` gate
/// absorbs the residual. The count + span floors guarantee the samples actually
/// cover the minute before this mean is trusted.
pub fn derive(samples: &[(i64, f64)], floor_strike: f64, close_unix: i64) -> Derivation {
    let lo = close_unix - AVG_WINDOW_SECS;
    let mut win: Vec<(i64, f64)> = samples
        .iter()
        .copied()
        .filter(|&(t, _)| t >= lo && t <= close_unix)
        .collect();
    win.sort_by_key(|&(t, _)| t);

    let n = win.len();
    let span = match (win.first(), win.last()) {
        (Some(&(a, _)), Some(&(b, _))) => b - a,
        _ => 0,
    };
    if n < MIN_SAMPLES || span < MIN_SPAN_SECS {
        return Derivation::Insufficient {
            samples: n,
            span_secs: span,
        };
    }

    let avg = win.iter().map(|&(_, p)| p).sum::<f64>() / n as f64;
    let bp = margin_bp(avg, floor_strike);
    // Decisive iff |avg - strike| / strike >= DERIVE_MARGIN. Exact-boundary
    // (== margin) counts as decisive (>=), matched by comparing basis points.
    if bp >= DERIVE_MARGIN * 10_000.0 {
        // yes = settlement value strictly above the strike (Kalshi "above"
        // crypto markets settle YES when the BRTI average exceeds floor_strike).
        let result = if avg > floor_strike { "yes" } else { "no" };
        Derivation::Derived {
            result,
            avg,
            margin_bp: bp,
        }
    } else {
        Derivation::Marginal {
            avg,
            margin_bp: bp,
        }
    }
}

/// Verdict when an official result finally lands for a window we derived — the
/// verify-and-auto-disable bookkeeping (item 4). `used` is whether the derivation
/// actually drove an entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verify {
    /// Prediction matched the official result.
    Agree,
    /// Prediction was WRONG and it drove a real entry — CRITICAL: disables
    /// derivation. The position stays (already risk-managed).
    DisagreeUsed,
    /// Prediction was wrong but was only a marginal/unused call — warn only.
    DisagreeUnused,
}

/// Compare a derived prediction to the official result.
pub fn verify(predicted: &str, official: &str, used: bool) -> Verify {
    if predicted == official {
        Verify::Agree
    } else if used {
        Verify::DisagreeUsed
    } else {
        Verify::DisagreeUnused
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build `n` samples ending at `close`, one per second, all at `price`.
    fn flat(close: i64, n: i64, price: f64) -> Vec<(i64, f64)> {
        (0..n).map(|i| (close - (n - 1) + i, price)).collect()
    }

    const CLOSE: i64 = 1_000_000;

    #[test]
    fn decisive_yes_when_avg_above_strike() {
        // 60 samples at 100.20, strike 100.00 → +20bp, decisive yes.
        let s = flat(CLOSE, 60, 100.20);
        match derive(&s, 100.00, CLOSE) {
            Derivation::Derived {
                result,
                avg,
                margin_bp,
            } => {
                assert_eq!(result, "yes");
                assert!((avg - 100.20).abs() < 1e-9);
                assert!((margin_bp - 20.0).abs() < 1e-6);
            }
            other => panic!("expected Derived yes, got {other:?}"),
        }
    }

    #[test]
    fn decisive_no_when_avg_below_strike() {
        let s = flat(CLOSE, 60, 99.80);
        match derive(&s, 100.00, CLOSE) {
            Derivation::Derived { result, .. } => assert_eq!(result, "no"),
            other => panic!("expected Derived no, got {other:?}"),
        }
    }

    #[test]
    fn exact_margin_boundary_is_decisive() {
        // Strike chosen so |avg-strike|/strike == exactly 5bp.
        let strike = 100.0;
        let avg = strike * (1.0 + DERIVE_MARGIN); // 100.05
        let s = flat(CLOSE, 60, avg);
        match derive(&s, strike, CLOSE) {
            Derivation::Derived { result, margin_bp, .. } => {
                assert_eq!(result, "yes");
                assert!((margin_bp - 5.0).abs() < 1e-6);
            }
            other => panic!("expected Derived at exact boundary, got {other:?}"),
        }
    }

    #[test]
    fn just_inside_margin_is_marginal() {
        // 4bp away (< 5bp gate) → Marginal, no call.
        let strike = 100.0;
        let avg = strike * (1.0 + 0.0004);
        let s = flat(CLOSE, 60, avg);
        match derive(&s, strike, CLOSE) {
            Derivation::Marginal { margin_bp, .. } => {
                assert!((margin_bp - 4.0).abs() < 1e-6);
            }
            other => panic!("expected Marginal, got {other:?}"),
        }
    }

    #[test]
    fn too_few_samples_is_insufficient() {
        let s = flat(CLOSE, 39, 100.20); // 39 < MIN_SAMPLES
        match derive(&s, 100.0, CLOSE) {
            Derivation::Insufficient { samples, .. } => assert_eq!(samples, 39),
            other => panic!("expected Insufficient, got {other:?}"),
        }
    }

    #[test]
    fn enough_samples_but_short_span_is_insufficient() {
        // 40 samples all within 10s → span 10 < MIN_SPAN_SECS.
        let s: Vec<(i64, f64)> = (0..40)
            .map(|i| (CLOSE - 10 + (i % 11), 100.20))
            .collect();
        match derive(&s, 100.0, CLOSE) {
            Derivation::Insufficient { samples, span_secs } => {
                assert_eq!(samples, 40);
                assert!(span_secs < MIN_SPAN_SECS);
            }
            other => panic!("expected Insufficient (short span), got {other:?}"),
        }
    }

    #[test]
    fn out_of_window_samples_excluded() {
        // 60 in-window at 100.20 plus 100 stale samples far before the window at
        // a wildly different price — the stale ones must not move the average.
        let mut s = flat(CLOSE, 60, 100.20);
        s.extend((0..100).map(|i| (CLOSE - 5000 + i, 50.0)));
        match derive(&s, 100.0, CLOSE) {
            Derivation::Derived { avg, result, .. } => {
                assert_eq!(result, "yes");
                assert!((avg - 100.20).abs() < 1e-9);
            }
            other => panic!("expected Derived (stale excluded), got {other:?}"),
        }
    }

    #[test]
    fn averages_rising_ramp() {
        // Linear ramp 99.0→101.0 over the minute: mean ≈ 100.0 == strike → the
        // ramp straddles the strike, lands within margin → Marginal (correct: a
        // genuine coin-flip we decline).
        let n = 60i64;
        let s: Vec<(i64, f64)> = (0..n)
            .map(|i| (CLOSE - (n - 1) + i, 99.0 + 2.0 * (i as f64) / (n - 1) as f64))
            .collect();
        match derive(&s, 100.0, CLOSE) {
            Derivation::Marginal { avg, .. } => assert!((avg - 100.0).abs() < 1e-6),
            other => panic!("expected Marginal for straddling ramp, got {other:?}"),
        }
    }

    #[test]
    fn verify_agreement_bookkeeping() {
        assert_eq!(verify("yes", "yes", true), Verify::Agree);
        assert_eq!(verify("no", "no", false), Verify::Agree);
        assert_eq!(verify("yes", "no", true), Verify::DisagreeUsed);
        assert_eq!(verify("yes", "no", false), Verify::DisagreeUnused);
    }
}
