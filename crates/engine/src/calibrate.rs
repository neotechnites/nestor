//! Bias calibration (T003) — the edge.
//!
//! For each city, pull a trailing window of Open-Meteo **historical-forecast**
//! daily-max (the as-issued forecast, no lookahead) and the matching IEM actual
//! daily-max, then compute the per-city forecast bias and bias-corrected MAE:
//!
//! ```text
//! bias          = mean(forecast - actual)
//! corrected_mae = mean(|(forecast - bias) - actual|)
//! tradeable     = corrected_mae < 2.0F     (reproduces the DEN/SEA cut)
//! ```
//!
//! This is a maintenance subcommand (`nestor calibrate`), not a `Strategy`.
//! The HTTP calls are isolated behind functions so the math is unit-testable
//! without network access. Results print as a table and are written to
//! `data/biases.json`.

use std::collections::BTreeMap;

use anyhow::{Context, Result};
use chrono::{Duration, Utc};
use chrono_tz::America::New_York;
use serde::{Deserialize, Serialize};

use crate::config::{City, Settings};

const OM_HIST_FORECAST: &str = "https://historical-forecast-api.open-meteo.com/v1/forecast";
const IEM_DAILY: &str = "https://mesonet.agron.iastate.edu/api/1/daily.json";

/// A city is tradeable when its bias-corrected MAE is under this many °F.
pub const TRADEABLE_MAE_MAX: f64 = 2.0;

/// Calibration result for one city. `n_days` is shown in the table but omitted
/// from `biases.json` (which carries only the fields the weather sleeve reads).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Calibration {
    pub bias: f64,
    pub mae: f64,
    pub tradeable: bool,
    #[serde(skip_serializing)]
    pub n_days: usize,
}

/// Compute `bias`, bias-corrected `mae`, and the `tradeable` flag from a set of
/// `(forecast, actual)` pairs (°F). Pure — no I/O, so it can be tested directly.
///
/// An empty input yields a non-tradeable, infinite-MAE result rather than NaN.
pub fn calibrate_pairs(pairs: &[(f64, f64)]) -> Calibration {
    let n = pairs.len();
    if n == 0 {
        return Calibration {
            bias: 0.0,
            mae: f64::INFINITY,
            tradeable: false,
            n_days: 0,
        };
    }
    let bias = pairs.iter().map(|(f, a)| f - a).sum::<f64>() / n as f64;
    let mae = pairs
        .iter()
        .map(|(f, a)| ((f - bias) - a).abs())
        .sum::<f64>()
        / n as f64;
    Calibration {
        bias,
        mae,
        tradeable: mae < TRADEABLE_MAE_MAX,
        n_days: n,
    }
}

#[derive(Deserialize)]
struct OmHistDaily {
    time: Vec<String>,
    temperature_2m_max: Vec<Option<f64>>,
}
#[derive(Deserialize)]
struct OmHistResp {
    daily: OmHistDaily,
}

/// Open-Meteo historical-forecast daily-max (°F), keyed by date (YYYY-MM-DD).
/// This is the forecast *as it was issued* for each day — no hindsight.
pub async fn historical_forecast_maxes(
    http: &reqwest::Client,
    lat: f64,
    lon: f64,
    sdate: &str,
    edate: &str,
) -> Result<BTreeMap<String, f64>> {
    let resp: OmHistResp = http
        .get(OM_HIST_FORECAST)
        .query(&[
            ("latitude", lat.to_string()),
            ("longitude", lon.to_string()),
            ("daily", "temperature_2m_max".into()),
            ("start_date", sdate.into()),
            ("end_date", edate.into()),
            ("timezone", "America/New_York".into()),
            ("temperature_unit", "fahrenheit".into()),
        ])
        .send()
        .await?
        .error_for_status()?
        .json()
        .await
        .context("parse Open-Meteo historical-forecast response")?;

    let mut out = BTreeMap::new();
    for (day, temp) in resp
        .daily
        .time
        .into_iter()
        .zip(resp.daily.temperature_2m_max)
    {
        if let Some(t) = temp {
            out.insert(day, t);
        }
    }
    Ok(out)
}

#[derive(Deserialize)]
struct IemResp {
    #[serde(default)]
    data: Vec<serde_json::Value>,
}

/// IEM actual daily-max highs (°F) over a date range, keyed by date
/// (YYYY-MM-DD). Settlement-grade truth; the range analogue of
/// [`crate::weather::actual_high`].
pub async fn actual_maxes(
    http: &reqwest::Client,
    station: &str,
    network: &str,
    sdate: &str,
    edate: &str,
) -> Result<BTreeMap<String, f64>> {
    let resp: IemResp = http
        .get(IEM_DAILY)
        .query(&[
            ("station", station),
            ("network", network),
            ("sdate", sdate),
            ("edate", edate),
        ])
        .send()
        .await?
        .error_for_status()?
        .json()
        .await
        .context("parse IEM daily response")?;

    // IEM daily.json ignores sdate/edate and returns the station's full history
    // ascending; fields are `date` (YYYY-MM-DD) and `max_tmpf`. We build the full
    // date->max map and let join_pairs filter to the forecast window.
    let mut out = BTreeMap::new();
    for row in &resp.data {
        let day = row.get("date").and_then(|v| v.as_str());
        let max = row.get("max_tmpf").and_then(|v| v.as_f64());
        if let (Some(day), Some(max)) = (day, max) {
            out.insert(day.to_string(), max);
        }
    }
    Ok(out)
}

/// Join forecast and actual by date into aligned `(forecast, actual)` pairs.
/// Only days present (and non-null) in *both* feeds are kept.
fn join_pairs(forecast: &BTreeMap<String, f64>, actual: &BTreeMap<String, f64>) -> Vec<(f64, f64)> {
    forecast
        .iter()
        .filter_map(|(day, &f)| actual.get(day).map(|&a| (f, a)))
        .collect()
}

/// Calibrate a single city: fetch both feeds, join by date, compute.
async fn calibrate_city(
    http: &reqwest::Client,
    city: &City,
    sdate: &str,
    edate: &str,
) -> Result<Calibration> {
    let forecast = historical_forecast_maxes(http, city.lat, city.lon, sdate, edate).await?;
    let actual = actual_maxes(http, &city.station, &city.network, sdate, edate).await?;
    let pairs = join_pairs(&forecast, &actual);
    if pairs.is_empty() {
        anyhow::bail!("no overlapping forecast/actual days for {}", city.code);
    }
    Ok(calibrate_pairs(&pairs))
}

/// Run the calibration job over every city in `settings`, print a table, and
/// write `{ "MIA": {"bias":..,"mae":..,"tradeable":..}, ... }` to `out_path`.
///
/// `window_days` is the trailing window (e.g. 60). The window ends yesterday
/// (ET) so we never read a not-yet-settled actual.
pub async fn run(settings: &Settings, window_days: i64, out_path: &str) -> Result<()> {
    let http = crate::http_client();
    let today = Utc::now().with_timezone(&New_York).date_naive();
    let edate = today - Duration::days(1);
    let sdate = edate - Duration::days(window_days - 1);
    let (sdate, edate) = (
        sdate.format("%Y-%m-%d").to_string(),
        edate.format("%Y-%m-%d").to_string(),
    );

    println!(
        "calibrating {} cities over {sdate}..={edate}",
        settings.cities.len()
    );
    println!(
        "{:<5} {:>7} {:>8} {:>8}  tradeable",
        "city", "n_days", "bias", "mae"
    );

    let mut out: BTreeMap<String, Calibration> = BTreeMap::new();
    for city in &settings.cities {
        match calibrate_city(&http, city, &sdate, &edate).await {
            Ok(cal) => {
                println!(
                    "{:<5} {:>7} {:>+8.2} {:>8.2}  {}",
                    city.code, cal.n_days, cal.bias, cal.mae, cal.tradeable
                );
                out.insert(city.code.clone(), cal);
            }
            Err(e) => {
                println!(
                    "{:<5} {:>7} {:>8} {:>8}  ERROR ({e})",
                    city.code, "-", "-", "-"
                );
            }
        }
    }

    if let Some(parent) = std::path::Path::new(out_path).parent() {
        std::fs::create_dir_all(parent).with_context(|| format!("create dir for {out_path}"))?;
    }
    let json = serde_json::to_string_pretty(&out)?;
    std::fs::write(out_path, json).with_context(|| format!("write {out_path}"))?;
    println!("wrote {} city biases to {out_path}", out.len());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const EPS: f64 = 1e-9;

    #[test]
    fn constant_offset_gives_exact_bias_zero_mae() {
        // Forecast is always 2°F above actual → bias 2, corrected error 0.
        let pairs = [(72.0, 70.0), (85.0, 83.0), (60.0, 58.0)];
        let cal = calibrate_pairs(&pairs);
        assert!((cal.bias - 2.0).abs() < EPS, "bias={}", cal.bias);
        assert!(cal.mae < EPS, "mae={}", cal.mae);
        assert!(cal.tradeable);
        assert_eq!(cal.n_days, 3);
    }

    #[test]
    fn tradeable_when_corrected_mae_below_threshold() {
        // Residuals (f-a) = [1, 3] → bias 2 → corrected = [-1, 1] → mae 1.0 < 2.0.
        let pairs = [(71.0, 70.0), (73.0, 70.0)];
        let cal = calibrate_pairs(&pairs);
        assert!((cal.bias - 2.0).abs() < EPS, "bias={}", cal.bias);
        assert!((cal.mae - 1.0).abs() < EPS, "mae={}", cal.mae);
        assert!(cal.tradeable);
    }

    #[test]
    fn not_tradeable_when_corrected_mae_at_or_above_threshold() {
        // Residuals (f-a) = [0, 5] → bias 2.5 → corrected = [-2.5, 2.5] → mae 2.5.
        let pairs = [(70.0, 70.0), (75.0, 70.0)];
        let cal = calibrate_pairs(&pairs);
        assert!((cal.bias - 2.5).abs() < EPS, "bias={}", cal.bias);
        assert!((cal.mae - 2.5).abs() < EPS, "mae={}", cal.mae);
        assert!(!cal.tradeable, "mae {} should not be tradeable", cal.mae);
    }

    #[test]
    fn boundary_mae_exactly_two_is_not_tradeable() {
        // Residuals (f-a) = [0, 4] → bias 2 → corrected = [-2, 2] → mae exactly 2.0.
        let pairs = [(70.0, 70.0), (74.0, 70.0)];
        let cal = calibrate_pairs(&pairs);
        assert!((cal.mae - 2.0).abs() < EPS, "mae={}", cal.mae);
        assert!(!cal.tradeable, "mae == 2.0 must be excluded (strict <)");
    }

    #[test]
    fn empty_input_is_infinite_and_not_tradeable() {
        let cal = calibrate_pairs(&[]);
        assert_eq!(cal.n_days, 0);
        assert!(cal.mae.is_infinite());
        assert!(!cal.tradeable);
    }

    #[test]
    fn join_keeps_only_overlapping_days() {
        let mut fc = BTreeMap::new();
        fc.insert("2026-07-01".to_string(), 90.0);
        fc.insert("2026-07-02".to_string(), 91.0);
        fc.insert("2026-07-03".to_string(), 92.0);
        let mut act = BTreeMap::new();
        act.insert("2026-07-02".to_string(), 88.0);
        act.insert("2026-07-03".to_string(), 89.0);
        act.insert("2026-07-04".to_string(), 90.0);
        let pairs = join_pairs(&fc, &act);
        assert_eq!(pairs, vec![(91.0, 88.0), (92.0, 89.0)]);
    }

    #[test]
    fn calibration_serializes_without_n_days() {
        let cal = Calibration {
            bias: 1.25,
            mae: 0.75,
            tradeable: true,
            n_days: 42,
        };
        let v = serde_json::to_value(&cal).unwrap();
        assert_eq!(v["bias"], 1.25);
        assert_eq!(v["mae"], 0.75);
        assert_eq!(v["tradeable"], true);
        assert!(
            v.get("n_days").is_none(),
            "n_days must be omitted from json"
        );
    }
}
