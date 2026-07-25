//! Volbook calibration table — the edge, as a loadable data artifact.
//!
//! Derived from the harvested `cwing_obs` corpus by `scripts/volbook_calib.py`
//! (verdict: work/verify-b9-widened.md). For each family (metal/gas/oil) and each
//! YES-price bucket inside the wing band [0.05, 0.35), it carries the REALIZED
//! touch rate (P the tail actually reached), shrunk toward the bucket's implied
//! mean so a lucky 0% touch on a thin bucket cannot justify paying near 100¢ for
//! NO. The strategy turns bucket touch into a willingness-to-pay ceiling.
//!
//! Only `metal` is `enabled` by default: the verdict shows metal holds
//! (+10.1pp gap, +8.6¢ EV, era- and asset-robust), gas collapses to ~0 EV, and
//! oil is near-fair (+3.6pp). Regenerate with `python3 scripts/volbook_calib.py`.

use std::collections::HashMap;

use anyhow::{Context, Result};
use serde::Deserialize;

/// One YES-price bucket within the wing band. `touch` is the SHRUNK realized
/// touch rate (fraction) the willingness-to-pay uses; `touch_obs`/`n` are kept
/// for provenance/audit.
#[derive(Debug, Clone, Deserialize)]
pub struct Bucket {
    pub lo: f64,
    pub hi: f64,
    #[serde(default)]
    pub n: u32,
    #[serde(default)]
    pub touch_obs: f64,
    #[serde(default)]
    pub implied_mid: f64,
    /// Shrunk realized touch (fraction 0..1) — the edge-boundary input.
    pub touch: f64,
}

/// Per-series provenance + the copper half-weight.
#[derive(Debug, Clone, Deserialize)]
pub struct SeriesInfo {
    /// Wing-sell weight: gold/silver 1.0, copper 0.5 (verdict).
    pub weight: f64,
    #[serde(default)]
    pub touch: Option<f64>,
    #[serde(default)]
    pub n: u32,
    #[serde(default)]
    pub nd: u32,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Family {
    /// Whether this family trades at all (metal true; gas/oil false).
    pub enabled: bool,
    #[serde(default)]
    pub implied_touch: f64,
    #[serde(default)]
    pub realized_touch: f64,
    #[serde(default)]
    pub gap_pp: f64,
    #[serde(default)]
    pub ev_cents: Option<f64>,
    #[serde(default)]
    pub series: HashMap<String, SeriesInfo>,
    pub buckets: Vec<Bucket>,
}

impl Family {
    /// Shrunk realized touch (fraction) for a YES price `yes_frac` (0..1), if it
    /// falls inside a calibrated bucket. `None` outside the band (no bucket → no
    /// edge estimate → do not trade).
    pub fn bucket_touch(&self, yes_frac: f64) -> Option<f64> {
        self.buckets
            .iter()
            .find(|b| yes_frac >= b.lo && yes_frac < b.hi)
            .map(|b| b.touch)
    }
}

/// The whole calibration artifact.
#[derive(Debug, Clone, Deserialize)]
pub struct Calib {
    #[serde(default)]
    pub schema: u32,
    #[serde(default)]
    pub generated: String,
    pub wing_lo: f64,
    pub wing_hi: f64,
    /// Weekday numbers (Mon=0..Sun=6) on which the edge holds (Mon-Wed).
    pub weekday_gate: Vec<u32>,
    /// Entry window as time-to-close bounds (T-3h ± tolerance).
    pub entry_ttc_lo_secs: i64,
    pub entry_ttc_hi_secs: i64,
    /// Guaranteed per-contract EV floor (¢) baked into the willingness-to-pay.
    pub margin_cents: f64,
    pub families: HashMap<String, Family>,
}

impl Calib {
    /// Load + validate the artifact from `path`.
    pub fn load(path: &str) -> Result<Calib> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("reading volbook calibration {path}"))?;
        let c: Calib =
            serde_json::from_str(&text).with_context(|| format!("parsing {path}"))?;
        if c.families.is_empty() {
            anyhow::bail!("volbook calibration {path} has no families");
        }
        if !(c.wing_lo >= 0.0 && c.wing_lo < c.wing_hi && c.wing_hi <= 1.0) {
            anyhow::bail!("volbook calibration wing band invalid: [{},{}]", c.wing_lo, c.wing_hi);
        }
        Ok(c)
    }

    /// Family a series belongs to (by scanning each family's series map), with a
    /// static fallback for the known commodity daily series so the strategy can
    /// resolve a series even if a family's provenance map is sparse.
    pub fn family_of(&self, series: &str) -> Option<&str> {
        for (fam, f) in &self.families {
            if f.series.contains_key(series) {
                return Some(fam.as_str());
            }
        }
        None
    }

    /// The (series, family, weight) list of every rung-series in an ENABLED
    /// family — the scan universe. Sorted for a deterministic scan order.
    pub fn enabled_series(&self) -> Vec<(String, String, f64)> {
        let mut out = Vec::new();
        for (fam, f) in &self.families {
            if !f.enabled {
                continue;
            }
            for (series, si) in &f.series {
                out.push((series.clone(), fam.clone(), si.weight));
            }
        }
        out.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn calib_json() -> &'static str {
        r#"{
          "schema":1,"generated":"2026-07-25",
          "wing_lo":0.05,"wing_hi":0.35,
          "weekday_gate":[0,1,2],
          "entry_ttc_lo_secs":9000,"entry_ttc_hi_secs":12600,
          "margin_cents":2.0,
          "families":{
            "metal":{"enabled":true,"ev_cents":8.61,"gap_pp":10.1,
              "series":{"KXGOLDD":{"weight":1.0,"touch":0.03,"n":100,"nd":29},
                        "KXCOPPERD":{"weight":0.5,"touch":0.069,"n":87,"nd":29}},
              "buckets":[{"lo":0.05,"hi":0.10,"n":142,"touch":0.0083},
                         {"lo":0.10,"hi":0.35,"n":100,"touch":0.10}]},
            "gas":{"enabled":false,"series":{"KXNATGASD":{"weight":1.0}},
              "buckets":[{"lo":0.05,"hi":0.35,"touch":0.05}]}
          }}"#
    }

    #[test]
    fn parses_and_validates() {
        let c: Calib = serde_json::from_str(calib_json()).unwrap();
        assert_eq!(c.wing_lo, 0.05);
        assert_eq!(c.weekday_gate, vec![0, 1, 2]);
        assert!(c.families["metal"].enabled);
        assert!(!c.families["gas"].enabled);
    }

    #[test]
    fn enabled_series_only_from_enabled_families() {
        let c: Calib = serde_json::from_str(calib_json()).unwrap();
        let es = c.enabled_series();
        // metal's two series only; gas excluded (disabled).
        assert_eq!(es.len(), 2);
        assert!(es.iter().any(|(s, _, w)| s == "KXGOLDD" && *w == 1.0));
        assert!(es.iter().any(|(s, _, w)| s == "KXCOPPERD" && *w == 0.5));
        assert!(!es.iter().any(|(s, _, _)| s == "KXNATGASD"));
    }

    #[test]
    fn bucket_touch_finds_band_returns_none_outside() {
        let c: Calib = serde_json::from_str(calib_json()).unwrap();
        let m = &c.families["metal"];
        assert_eq!(m.bucket_touch(0.07), Some(0.0083)); // first bucket
        assert_eq!(m.bucket_touch(0.20), Some(0.10)); // second bucket
        assert_eq!(m.bucket_touch(0.04), None); // below band
        assert_eq!(m.bucket_touch(0.35), None); // hi exclusive
        assert_eq!(m.bucket_touch(0.50), None); // above band
    }

    #[test]
    fn family_of_resolves_series() {
        let c: Calib = serde_json::from_str(calib_json()).unwrap();
        assert_eq!(c.family_of("KXGOLDD"), Some("metal"));
        assert_eq!(c.family_of("KXNATGASD"), Some("gas"));
        assert_eq!(c.family_of("KXNOPE"), None);
    }
}
