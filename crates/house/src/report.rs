//! `house-report`: summarize the protocol's four metrics from the participation
//! log (`data/house_probe.jsonl`). The aggregation is a pure function over the
//! parsed records so it is unit-tested without a real log file.

use anyhow::{Context, Result};
use serde_json::Value;

use crate::signal::Metrics;

/// Event names written by the quote loop (`strategy.rs`).
const EV_QUOTE_LIVE: &str = "house_quote_live";
const EV_FILL: &str = "house_fill";
const EV_MARKOUT: &str = "house_markout";

fn f64_of(v: &Value, k: &str) -> Option<f64> {
    v.get(k).and_then(|x| x.as_f64())
}
fn i64_of(v: &Value, k: &str) -> Option<i64> {
    v.get(k).and_then(|x| x.as_i64())
}
fn bool_of(v: &Value, k: &str) -> bool {
    v.get(k).and_then(|x| x.as_bool()).unwrap_or(false)
}

/// Aggregate the four protocol metrics from parsed JSONL records.
///
/// - Metric 1 (fill rate): Σ fill counts / quote-hours. Quote-hours come from the
///   `quote_secs` accrued on every `house_quote_live` record (both legs resting).
/// - Metric 2 (realized half-spread): mean of `half_spread_cents` on `house_markout`
///   records that carry it (a completed round-trip). None if no round-trips yet.
/// - Metric 3 (gap-through freq): fraction of `house_markout` records flagged
///   `gap_through` (mid moved ≥3¢ against within 60s).
/// - Metric 4 (adverse-in-catalyst): of markouts with markout_cents < 0, the
///   fraction also flagged `in_catalyst`. None if no adverse fills.
pub fn summarize(records: &[Value]) -> Metrics {
    let mut quote_secs = 0.0f64;
    let mut fills = 0i64;
    let mut net_markouts: Vec<f64> = Vec::new();
    let mut markouts = 0i64;
    let mut gap_throughs = 0i64;
    let mut adverse = 0i64;
    let mut adverse_catalyst = 0i64;

    for r in records {
        match r.get("event").and_then(|e| e.as_str()) {
            Some(EV_QUOTE_LIVE) => {
                // FIX 8 / I3: `quote_secs` is now a per-pass DELTA (it used to be
                // the quote's cumulative age, and summing those overstated
                // quote-hours ~9.5x → metric 1 understated ~10x → spurious KILL).
                quote_secs += f64_of(r, "quote_secs").unwrap_or(0.0);
            }
            Some(EV_FILL) => {
                fills += i64_of(r, "count").unwrap_or(0);
            }
            Some(EV_MARKOUT) => {
                markouts += 1;
                if bool_of(r, "gap_through") {
                    gap_throughs += 1;
                }
                // FIX 8 / I5 (sensors F3): metric 2 averages EVERY markout, net
                // of the fill's fee. It used to average only `half_spread_cents`,
                // which the writer populated `if markout > 0` — so metric 2 was
                // E[markout | markout > 0], structurally positive, and the
                // "≥ +0.6¢ net of fees" promote gate COULD NOT FAIL. Fees were
                // never subtracted at all despite the gate saying "net of fees".
                if let Some(mk) = f64_of(r, "markout_cents") {
                    let fee = f64_of(r, "fee_cents").unwrap_or(0.0);
                    let count = i64_of(r, "count").unwrap_or(1).max(1) as f64;
                    // markout is per contract; the fee row is the whole fill.
                    net_markouts.push(mk - fee / count);
                    if mk < 0.0 {
                        adverse += 1;
                        if bool_of(r, "in_catalyst") {
                            adverse_catalyst += 1;
                        }
                    }
                }
            }
            _ => {}
        }
    }

    let quote_minutes = quote_secs / 60.0;
    let quote_hours = quote_secs / 3600.0;
    let fill_rate_per_hour = if quote_hours > 0.0 {
        fills as f64 / quote_hours
    } else {
        0.0
    };
    let avg_half_spread_cents = if net_markouts.is_empty() {
        None
    } else {
        Some(net_markouts.iter().sum::<f64>() / net_markouts.len() as f64)
    };
    let gap_through_frac = if markouts > 0 {
        gap_throughs as f64 / markouts as f64
    } else {
        0.0
    };
    let adverse_in_catalyst_frac = if adverse > 0 {
        Some(adverse_catalyst as f64 / adverse as f64)
    } else {
        None
    };

    Metrics {
        quote_minutes,
        fills,
        fill_rate_per_hour,
        avg_half_spread_cents,
        gap_through_frac,
        adverse_in_catalyst_frac,
    }
}

/// Read a JSONL participation log and print the four metrics. Missing file → a
/// clear message (the probe may not have run yet), not an error.
pub fn run(path: &str) -> Result<()> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            println!("house-report: no log at {path} yet (probe has not run).");
            return Ok(());
        }
        Err(e) => return Err(e).with_context(|| format!("reading {path}")),
    };
    let records: Vec<Value> = text
        .lines()
        .filter(|l| !l.trim().is_empty())
        .filter_map(|l| serde_json::from_str(l).ok())
        .collect();
    let m = summarize(&records);
    println!("=== HOUSE PROBE REPORT ({} records from {path}) ===", records.len());
    println!("  quote-minutes         : {:.1}", m.quote_minutes);
    println!("  fills                 : {}", m.fills);
    println!(
        "  1. fill rate          : {:.2}/hr  (target ≥ a handful/hr)",
        m.fill_rate_per_hour
    );
    match m.avg_half_spread_cents {
        Some(hs) => println!(
            "  2. realized half-spread: {hs:+.2}¢/contract net of fees, over ALL markouts \
             (promote if ≥ +0.6¢)"
        ),
        None => println!("  2. realized half-spread: n/a (no markouts yet)"),
    }
    println!(
        "  3. gap-through freq   : {:.1}%  (kill number — if this eats the spread)",
        m.gap_through_frac * 100.0
    );
    match m.adverse_in_catalyst_frac {
        Some(f) => println!(
            "  4. adverse-in-catalyst: {:.1}%  (should be low — the gate should catch these)",
            f * 100.0
        ),
        None => println!("  4. adverse-in-catalyst: n/a (no adverse fills)"),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn summarize_computes_four_metrics() {
        let recs = vec![
            json!({"event": "house_quote_live", "quote_secs": 1800.0}), // 30 min
            json!({"event": "house_quote_live", "quote_secs": 1800.0}), // +30 min = 1h
            json!({"event": "house_fill", "count": 3}),
            json!({"event": "house_fill", "count": 2}),
            // 3 markouts: one gap-through & adverse-in-catalyst, one adverse-not, one favorable.
            json!({"event": "house_markout", "markout_cents": -4.0, "gap_through": true, "in_catalyst": true, "count": 1, "fee_cents": 0.0}),
            json!({"event": "house_markout", "markout_cents": -1.0, "gap_through": false, "in_catalyst": false, "count": 1, "fee_cents": 0.0}),
            json!({"event": "house_markout", "markout_cents": 1.0, "gap_through": false, "count": 1, "fee_cents": 0.0}),
        ];
        let m = summarize(&recs);
        assert_eq!(m.fills, 5);
        assert!((m.quote_minutes - 60.0).abs() < 1e-9);
        assert!((m.fill_rate_per_hour - 5.0).abs() < 1e-9); // 5 fills / 1h
        // Metric 2 now averages ALL THREE: (−4 −1 +1)/3 = −1.333.
        assert!((m.avg_half_spread_cents.unwrap() - (-4.0 / 3.0)).abs() < 1e-9);
        assert!((m.gap_through_frac - (1.0 / 3.0)).abs() < 1e-9);
        // adverse = 2 (the -4 and -1); in catalyst = 1 -> 0.5
        assert_eq!(m.adverse_in_catalyst_frac, Some(0.5));
    }

    #[test]
    fn metric_two_can_actually_fail_and_nets_fees() {
        // FIX 8 / I5 (sensors F3). The exact scenario the review named: 10 fills
        // marked out +1,+1,+1,−4×7. TRUE mean = −2.5¢ → KILL. The old metric
        // averaged only the favourable ones (`half_spread_cents` was populated
        // `if markout > 0`) and reported +1.0¢ → PROMOTE, allocating real capital
        // to a bleeding maker sleeve.
        let mut recs: Vec<Value> = Vec::new();
        for _ in 0..3 {
            recs.push(json!({"event": "house_markout", "markout_cents": 1.0,
                             "half_spread_cents": 1.0, "count": 1, "fee_cents": 0.0}));
        }
        for _ in 0..7 {
            recs.push(json!({"event": "house_markout", "markout_cents": -4.0,
                             "count": 1, "fee_cents": 0.0}));
        }
        let m = summarize(&recs);
        let mean = m.avg_half_spread_cents.unwrap();
        assert!((mean - (-2.5)).abs() < 1e-9, "metric 2 was {mean}");
        assert!(mean < 0.6, "the promote gate must be able to FAIL");

        // ...and it is net of fees: a +1.0¢ markout on a 2-contract fill billed
        // 1.0¢ TOTAL nets to +0.5¢/contract, which does NOT clear +0.6¢.
        let m = summarize(&[json!({"event": "house_markout", "markout_cents": 1.0,
                                   "count": 2, "fee_cents": 1.0})]);
        assert!((m.avg_half_spread_cents.unwrap() - 0.5).abs() < 1e-9);
        assert!(m.avg_half_spread_cents.unwrap() < 0.6);
    }

    #[test]
    fn quote_seconds_are_summed_as_deltas_not_cumulative_ages() {
        // FIX 8 / I3 (sensors F2). 60 real seconds of quoting on the 3s loop:
        // the writer now emits 3s per pass. The OLD writer emitted the quote's
        // cumulative age (3,6,…,57), which this same sum turned into 570s —
        // 9.5x the truth, so a probe genuinely filling 5/hr reported 0.53/hr.
        let deltas: Vec<Value> = (1..=20)
            .map(|_| json!({"event": "house_quote_live", "quote_secs": 3.0}))
            .collect();
        let m = summarize(&deltas);
        assert!((m.quote_minutes - 1.0).abs() < 1e-9);

        let cumulative: Vec<Value> = (1..=19)
            .map(|i| json!({"event": "house_quote_live", "quote_secs": (i * 3) as f64}))
            .collect();
        let old = summarize(&cumulative);
        assert!(old.quote_minutes > m.quote_minutes * 9.0);
    }

    #[test]
    fn summarize_empty_is_zeros() {
        let m = summarize(&[]);
        assert_eq!(m.fills, 0);
        assert_eq!(m.fill_rate_per_hour, 0.0);
        assert_eq!(m.avg_half_spread_cents, None);
        assert_eq!(m.adverse_in_catalyst_frac, None);
    }
}
