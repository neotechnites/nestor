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
    let mut half_spreads: Vec<f64> = Vec::new();
    let mut markouts = 0i64;
    let mut gap_throughs = 0i64;
    let mut adverse = 0i64;
    let mut adverse_catalyst = 0i64;

    for r in records {
        match r.get("event").and_then(|e| e.as_str()) {
            Some(EV_QUOTE_LIVE) => {
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
                if let Some(hs) = f64_of(r, "half_spread_cents") {
                    half_spreads.push(hs);
                }
                if f64_of(r, "markout_cents").unwrap_or(0.0) < 0.0 {
                    adverse += 1;
                    if bool_of(r, "in_catalyst") {
                        adverse_catalyst += 1;
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
    let avg_half_spread_cents = if half_spreads.is_empty() {
        None
    } else {
        Some(half_spreads.iter().sum::<f64>() / half_spreads.len() as f64)
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
            "  2. realized half-spread: {hs:+.2}¢  (promote if ≥ +0.6¢ net of fees)"
        ),
        None => println!("  2. realized half-spread: n/a (no completed round-trips)"),
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
            // 3 markouts: one gap-through & adverse-in-catalyst, one adverse-not, one favorable roundtrip.
            json!({"event": "house_markout", "markout_cents": -4.0, "gap_through": true, "in_catalyst": true}),
            json!({"event": "house_markout", "markout_cents": -1.0, "gap_through": false, "in_catalyst": false}),
            json!({"event": "house_markout", "markout_cents": 1.0, "gap_through": false, "half_spread_cents": 0.8}),
        ];
        let m = summarize(&recs);
        assert_eq!(m.fills, 5);
        assert!((m.quote_minutes - 60.0).abs() < 1e-9);
        assert!((m.fill_rate_per_hour - 5.0).abs() < 1e-9); // 5 fills / 1h
        assert_eq!(m.avg_half_spread_cents, Some(0.8));
        assert!((m.gap_through_frac - (1.0 / 3.0)).abs() < 1e-9);
        // adverse = 2 (the -4 and -1); in catalyst = 1 -> 0.5
        assert_eq!(m.adverse_in_catalyst_frac, Some(0.5));
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
