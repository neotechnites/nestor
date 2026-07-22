//! Weather data feeds: Open-Meteo forecast (the signal) + IEM actuals (truth).

use anyhow::{Context, Result};
use serde::Deserialize;

const OPEN_METEO: &str = "https://api.open-meteo.com/v1/forecast";
const IEM_DAILY: &str = "https://mesonet.agron.iastate.edu/api/1/daily.json";

#[derive(Deserialize)]
struct OmDaily {
    temperature_2m_max: Vec<f64>,
    precipitation_sum: Vec<f64>,
}
#[derive(Deserialize)]
struct OmResp {
    daily: OmDaily,
}

/// Forecast daily-max (F) + precip (in) for a specific date (YYYY-MM-DD),
/// pinned so run-time-of-day can't shift which day we read.
pub async fn forecast_for(
    http: &reqwest::Client,
    lat: f64,
    lon: f64,
    date: &str,
) -> Result<(f64, f64)> {
    let resp: OmResp = http
        .get(OPEN_METEO)
        .query(&[
            ("latitude", lat.to_string()),
            ("longitude", lon.to_string()),
            ("daily", "temperature_2m_max,precipitation_sum".into()),
            ("start_date", date.into()),
            ("end_date", date.into()),
            ("timezone", "America/New_York".into()),
            ("temperature_unit", "fahrenheit".into()),
            ("precipitation_unit", "inch".into()),
        ])
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    let t = *resp
        .daily
        .temperature_2m_max
        .first()
        .context("no forecast temp")?;
    let p = *resp.daily.precipitation_sum.first().unwrap_or(&0.0);
    Ok((t, p))
}

#[derive(Deserialize)]
struct IemResp {
    #[serde(default)]
    data: Vec<serde_json::Value>,
}

/// Official daily-max high (F) from IEM for a date. Settlement-grade truth.
pub async fn actual_high(
    http: &reqwest::Client,
    station: &str,
    network: &str,
    date: &str,
) -> Result<Option<f64>> {
    let resp: IemResp = http
        .get(IEM_DAILY)
        .query(&[
            ("station", station),
            ("network", network),
            ("sdate", date),
            ("edate", date),
        ])
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    Ok(resp
        .data
        .first()
        .and_then(|r| r.get("max_temp_f"))
        .and_then(|v| v.as_f64()))
}
