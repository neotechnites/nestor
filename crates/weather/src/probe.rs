//! `nestor probe-weather` — read-only reality check for the weather config.
//!
//! For each city it (1) confirms the configured Kalshi series ticker resolves
//! to live markets and reads the settlement subtitle, retrying the alternate
//! `KXHIGH`/`KXHIGHT` spelling when the configured one comes back empty, and
//! (2) confirms the IEM station returns a daily max temp, retrying the id with
//! the leading `K` dropped (IEM ASOS ids are 3-letter, e.g. `MIA` not `KMIA`).
//! It prints a report and NEVER edits config — it only tells you what to fix.

use anyhow::Result;
use chrono::Datelike;
use chrono_tz::America::New_York;
use engine::config::City;
use engine::kalshi::{Kalshi, Market};

const IEM_DAILY: &str = "https://mesonet.agron.iastate.edu/api/1/daily.json";

/// One city's verdict, ready to render into the report table.
#[derive(Debug, Clone, PartialEq)]
pub struct CityProbe {
    pub code: String,
    pub configured_series: String,
    pub series_ok: bool,
    /// The series that actually returned markets (config or the alternate).
    pub working_series: Option<String>,
    pub sample_ticker: Option<String>,
    pub settlement: Option<String>,
    pub configured_station: String,
    pub station_ok: bool,
    /// The station id that actually returned data (config or the alternate).
    pub working_station: Option<String>,
    /// Latest (date, max_tmpf) IEM returned for the working station.
    pub iem_sample: Option<(String, f64)>,
    pub notes: String,
}

/// Outcome of a single series lookup (parsing kept separate from the network).
struct SeriesLookup {
    ok: bool,
    sample_ticker: Option<String>,
    settlement: Option<String>,
    error: Option<String>,
}

/// Outcome of a single IEM lookup.
struct IemLookup {
    sample: Option<(String, f64)>,
    error: Option<String>,
}

/// Toggle the `KXHIGH<city>` / `KXHIGHT<city>` spelling the forward test saw
/// mixed. Returns the alternate spelling, or `None` for non-KXHIGH tickers.
pub fn alternate_series(series: &str) -> Option<String> {
    if let Some(rest) = series.strip_prefix("KXHIGHT") {
        Some(format!("KXHIGH{rest}"))
    } else {
        series
            .strip_prefix("KXHIGH")
            .map(|rest| format!("KXHIGHT{rest}"))
    }
}

/// IEM's ASOS network keys stations by the 3-letter id (e.g. `MIA`), not the
/// 4-letter ICAO (`KMIA`). If the configured id looks like a K-prefixed ICAO,
/// return the id with the `K` dropped; otherwise `None`.
pub fn alternate_station(station: &str) -> Option<String> {
    if station.len() == 4 && station.starts_with('K') {
        Some(station[1..].to_string())
    } else {
        None
    }
}

/// Extract the most recent (date, max_tmpf) from an IEM daily.json body. The
/// field is `max_tmpf` (falls back to `max_temp_f` for safety). Rows are date
/// ascending, so we scan from the end for the first numeric high. Pure and
/// network-free so it is unit-testable.
pub fn parse_iem_high(body: &str) -> Option<(String, f64)> {
    let v: serde_json::Value = serde_json::from_str(body).ok()?;
    let data = v.get("data")?.as_array()?;
    data.iter().rev().find_map(|row| {
        let t = row
            .get("max_tmpf")
            .and_then(|v| v.as_f64())
            .or_else(|| row.get("max_temp_f").and_then(|v| v.as_f64()))?;
        let d = row
            .get("date")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        Some((d, t))
    })
}

fn summarize(markets: &[Market]) -> SeriesLookup {
    SeriesLookup {
        ok: !markets.is_empty(),
        sample_ticker: markets.first().map(|m| m.ticker.clone()),
        settlement: markets.first().and_then(|m| m.yes_sub_title.clone()),
        error: None,
    }
}

async fn series_lookup(kalshi: &Kalshi, series: &str) -> SeriesLookup {
    match kalshi.probe_series(series, "open", 5).await {
        Ok(markets) => summarize(&markets),
        Err(e) => SeriesLookup {
            ok: false,
            sample_ticker: None,
            settlement: None,
            error: Some(e.to_string()),
        },
    }
}

async fn iem_lookup(
    http: &reqwest::Client,
    station: &str,
    network: &str,
    sdate: &str,
    edate: &str,
) -> IemLookup {
    let fetch = http
        .get(IEM_DAILY)
        .query(&[
            ("station", station),
            ("network", network),
            ("sdate", sdate),
            ("edate", edate),
        ])
        .send()
        .await
        .and_then(|r| r.error_for_status());
    match fetch {
        Ok(resp) => match resp.text().await {
            Ok(body) => IemLookup {
                sample: parse_iem_high(&body),
                error: None,
            },
            Err(e) => IemLookup {
                sample: None,
                error: Some(e.to_string()),
            },
        },
        Err(e) => IemLookup {
            sample: None,
            error: Some(e.to_string()),
        },
    }
}

/// A recent date (2 days back, ET). IEM's daily.json currently returns the
/// station's full history regardless, but we still bound the request.
fn recent_range() -> (String, String) {
    let today = chrono::Utc::now().with_timezone(&New_York).date_naive();
    let start = today - chrono::Duration::days(14);
    let fmt = |d: chrono::NaiveDate| format!("{:04}-{:02}-{:02}", d.year(), d.month(), d.day());
    (fmt(start), fmt(today))
}

async fn probe_city(
    kalshi: &Kalshi,
    http: &reqwest::Client,
    c: &City,
    sdate: &str,
    edate: &str,
) -> CityProbe {
    let mut notes: Vec<String> = Vec::new();

    // --- Kalshi series ---
    let primary = series_lookup(kalshi, &c.series).await;
    let (series_ok, working_series, sample_ticker, settlement) = if primary.ok {
        (
            true,
            Some(c.series.clone()),
            primary.sample_ticker,
            primary.settlement,
        )
    } else {
        if let Some(err) = &primary.error {
            notes.push(format!("series lookup errored: {err}"));
        }
        match alternate_series(&c.series) {
            Some(alt) => {
                let alt_res = series_lookup(kalshi, &alt).await;
                if alt_res.ok {
                    notes.push(format!(
                        "series '{}' returned nothing; alternate '{alt}' WORKS — fix config",
                        c.series
                    ));
                    (false, Some(alt), alt_res.sample_ticker, alt_res.settlement)
                } else {
                    notes.push(format!(
                        "neither series '{}' nor alternate '{alt}' returned markets",
                        c.series
                    ));
                    (false, None, None, None)
                }
            }
            None => {
                notes.push(format!("series '{}' returned no markets", c.series));
                (false, None, None, None)
            }
        }
    };

    // --- IEM station ---
    let primary_iem = iem_lookup(http, &c.station, &c.network, sdate, edate).await;
    let (station_ok, working_station, iem_sample) = if primary_iem.sample.is_some() {
        (true, Some(c.station.clone()), primary_iem.sample)
    } else {
        if let Some(err) = &primary_iem.error {
            notes.push(format!("IEM {}/{} errored: {err}", c.station, c.network));
        }
        match alternate_station(&c.station) {
            Some(alt) => {
                let alt_res = iem_lookup(http, &alt, &c.network, sdate, edate).await;
                if alt_res.sample.is_some() {
                    notes.push(format!(
                        "station '{}' returned nothing; alternate '{alt}' WORKS — fix config",
                        c.station
                    ));
                    (false, Some(alt), alt_res.sample)
                } else {
                    notes.push(format!(
                        "neither station '{}' nor alternate '{alt}' returned max_tmpf",
                        c.station
                    ));
                    (false, None, None)
                }
            }
            None => {
                notes.push(format!("station '{}' returned no max_tmpf", c.station));
                (false, None, None)
            }
        }
    };

    CityProbe {
        code: c.code.clone(),
        configured_series: c.series.clone(),
        series_ok,
        working_series,
        sample_ticker,
        settlement,
        configured_station: c.station.clone(),
        station_ok,
        working_station,
        iem_sample,
        notes: notes.join("; "),
    }
}

fn flag(ok: bool, working: bool) -> &'static str {
    if ok {
        "OK"
    } else if working {
        "USE-ALT"
    } else {
        "MISSING"
    }
}

/// Render the probe results as a fixed-width report table.
pub fn render_table(rows: &[CityProbe]) -> String {
    let mut out = String::new();
    out.push_str(&format!(
        "{:<5} {:<13} {:<8} {:<24} {:<7} {:<8} {}\n",
        "city", "series", "series?", "sample ticker", "stn", "IEM?", "notes"
    ));
    out.push_str(&format!("{}\n", "-".repeat(110)));
    for r in rows {
        out.push_str(&format!(
            "{:<5} {:<13} {:<8} {:<24} {:<7} {:<8} {}\n",
            r.code,
            r.configured_series,
            flag(r.series_ok, r.working_series.is_some()),
            r.sample_ticker.as_deref().unwrap_or("-"),
            r.configured_station,
            flag(r.station_ok, r.working_station.is_some()),
            r.notes,
        ));
    }
    out
}

/// Render the settlement subtitle + IEM sample lines, so we can eyeball what
/// Kalshi settles each series on vs. the configured IEM station's latest high.
pub fn render_settlement(rows: &[CityProbe]) -> String {
    let mut out =
        String::from("settlement subtitle (Kalshi) | latest IEM high (working station):\n");
    for r in rows {
        let src = r.settlement.as_deref().unwrap_or("(none)");
        let series = r.working_series.as_deref().unwrap_or(&r.configured_series);
        let iem = match &r.iem_sample {
            Some((d, t)) => format!(
                "{} {}={:.0}F",
                r.working_station
                    .as_deref()
                    .unwrap_or(&r.configured_station),
                d,
                t
            ),
            None => "(no IEM data)".to_string(),
        };
        out.push_str(&format!(
            "  {:<5} {:<13} {:<22} | {}\n",
            r.code, series, src, iem
        ));
    }
    out
}

/// Run the probe for every city and print the report. Read-only.
pub async fn run(kalshi: &Kalshi, http: &reqwest::Client, cities: &[City]) -> Result<()> {
    let (sdate, edate) = recent_range();
    println!("nestor probe-weather — Kalshi series + IEM station reality check");
    println!("(read-only; IEM window: {sdate}..{edate})\n");

    let mut rows = Vec::with_capacity(cities.len());
    for c in cities {
        rows.push(probe_city(kalshi, http, c, &sdate, &edate).await);
    }

    print!("{}", render_table(&rows));
    println!();
    print!("{}", render_settlement(&rows));

    let mismatches: Vec<&CityProbe> = rows
        .iter()
        .filter(|r| !r.series_ok || !r.station_ok)
        .collect();
    println!();
    if mismatches.is_empty() {
        println!("All cities verified: series live and IEM stations returning data.");
    } else {
        println!("{} city/cities need attention:", mismatches.len());
        for r in mismatches {
            println!("  - {}: {}", r.code, r.notes);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn alternate_series_toggles_kxhight_and_kxhigh() {
        assert_eq!(alternate_series("KXHIGHMIA").as_deref(), Some("KXHIGHTMIA"));
        assert_eq!(alternate_series("KXHIGHTATL").as_deref(), Some("KXHIGHATL"));
        // round-trips back to the original
        let alt = alternate_series("KXHIGHNY").unwrap();
        assert_eq!(alternate_series(&alt).as_deref(), Some("KXHIGHNY"));
    }

    #[test]
    fn alternate_series_ignores_non_kxhigh() {
        assert_eq!(alternate_series("KXRAINNYC"), None);
    }

    #[test]
    fn alternate_station_drops_leading_k() {
        assert_eq!(alternate_station("KMIA").as_deref(), Some("MIA"));
        assert_eq!(alternate_station("KDEN").as_deref(), Some("DEN"));
        assert_eq!(alternate_station("MIA"), None); // already 3-letter
        assert_eq!(alternate_station("KJFK").as_deref(), Some("JFK"));
    }

    #[test]
    fn parse_iem_high_reads_latest_max_tmpf() {
        let body = r#"{
            "schema": {},
            "data": [
                {"date": "2026-07-18", "max_tmpf": 90.0},
                {"date": "2026-07-19", "max_tmpf": 92.0},
                {"date": "2026-07-20", "max_tmpf": null}
            ]
        }"#;
        // Skips the null trailing row, returns the latest numeric high.
        assert_eq!(parse_iem_high(body), Some(("2026-07-19".to_string(), 92.0)));
    }

    #[test]
    fn parse_iem_high_empty_data_is_none() {
        assert_eq!(parse_iem_high(r#"{"schema":{},"data":[]}"#), None);
    }

    #[test]
    fn render_table_flags_series_and_station() {
        let rows = vec![
            CityProbe {
                code: "MIA".into(),
                configured_series: "KXHIGHMIA".into(),
                series_ok: true,
                working_series: Some("KXHIGHMIA".into()),
                sample_ticker: Some("KXHIGHMIA-26JUL21-T95".into()),
                settlement: Some("96° or above".into()),
                configured_station: "KMIA".into(),
                station_ok: false,
                working_station: Some("MIA".into()),
                iem_sample: Some(("2026-07-21".into(), 92.0)),
                notes: "station 'KMIA' returned nothing; alternate 'MIA' WORKS".into(),
            },
            CityProbe {
                code: "ATL".into(),
                configured_series: "KXHIGHATL".into(),
                series_ok: false,
                working_series: None,
                sample_ticker: None,
                settlement: None,
                configured_station: "KATL".into(),
                station_ok: true,
                working_station: Some("KATL".into()),
                iem_sample: Some(("2026-07-21".into(), 91.0)),
                notes: "series 'KXHIGHATL' returned no markets".into(),
            },
        ];
        let table = render_table(&rows);
        assert!(table.contains("USE-ALT")); // MIA station uses alternate
        assert!(table.contains("MISSING")); // ATL series missing
        assert!(table.contains("KXHIGHMIA-26JUL21-T95"));
    }
}
