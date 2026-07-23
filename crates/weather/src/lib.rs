//! Weather sleeve — the daily forecast-buy strategy.
//!
//! At ~9am ET, for each tradeable city: forecast -> bias-correct -> map to the
//! correct day's 2F bucket -> skip wet days -> emit a Signal (flat sizing). The
//! Risk layer decides size / approves; the engine executes (live) or simulates
//! (paper). Hold to settlement (closed out by the reconcile job).

use anyhow::Result;
use async_trait::async_trait;
use chrono::Datelike;
use chrono_tz::America::New_York;
use engine::config::City;
use engine::kalshi::Market;
use engine::strategy::ExecOutcome;
use engine::{alert, logging, Engine, Side, Signal, SizingHint, Strategy};
use serde_json::json;

pub mod probe;

const LOG: &str = "weather_trades.jsonl";

pub struct Weather;

impl Weather {
    /// Event date we trade — today in ET (the 9am ET run trades today).
    fn target_date() -> chrono::NaiveDate {
        chrono::Utc::now().with_timezone(&New_York).date_naive()
    }

    /// Kalshi ticker date segment, e.g. 2026-07-22 -> "26JUL22".
    fn date_code(d: chrono::NaiveDate) -> String {
        const MON: [&str; 12] = [
            "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
        ];
        format!(
            "{:02}{}{:02}",
            d.year() % 100,
            MON[(d.month() - 1) as usize],
            d.day()
        )
    }

    /// Pick the market for `date_code` whose bucket best fits `temp_f`:
    /// prefer a range bucket [floor,cap] containing it; else the correct
    /// open-ended threshold bucket beyond the ladder.
    fn bucket_market<'a>(
        markets: &'a [Market],
        temp_f: f64,
        date_code: &str,
    ) -> Option<&'a Market> {
        let tag = format!("-{date_code}-");
        let day: Vec<&Market> = markets.iter().filter(|m| m.ticker.contains(&tag)).collect();
        for m in &day {
            if let (Some(lo), Some(hi)) = (m.floor_strike, m.cap_strike) {
                if lo <= temp_f && temp_f <= hi {
                    return Some(m);
                }
            }
        }
        for m in &day {
            match (m.floor_strike, m.cap_strike) {
                (Some(lo), None) if temp_f >= lo => return Some(m),
                (None, Some(hi)) if temp_f <= hi => return Some(m),
                _ => {}
            }
        }
        None
    }
}

#[async_trait]
impl Strategy for Weather {
    fn name(&self) -> &str {
        "weather"
    }

    async fn run(&self, eng: &Engine) -> Result<()> {
        let td = Self::target_date();
        let dcode = Self::date_code(td);
        let date_str = td.format("%Y-%m-%d").to_string();
        eng.begin_day(&date_str);
        let st = eng.risk.lock().unwrap_or_else(|e| e.into_inner()).status();
        logging::info(format!(
            "weather start — mode={:?} date={date_str} ({dcode}) bankroll=${:.2} halted={}",
            eng.mode, st.bankroll, st.halted
        ));
        if st.halted {
            alert::notify(
                &eng.http,
                &format!(
                    "weather HALTED — placing no trades (bankroll ${:.2})",
                    st.bankroll
                ),
            )
            .await;
        }

        let cities: Vec<City> = eng.cities.iter().filter(|c| c.tradeable).cloned().collect();
        for c in &cities {
            if let Err(e) = self.run_city(eng, c, &dcode, &date_str).await {
                logging::info(format!("{}: error ({e}) — skip", c.code));
            }
        }
        let st = eng.risk.lock().unwrap_or_else(|e| e.into_inner()).status();
        logging::info(format!(
            "weather done — bankroll=${:.2} drawdown={:.1}%",
            st.bankroll,
            st.drawdown * 100.0
        ));
        Ok(())
    }
}

impl Weather {
    async fn run_city(&self, eng: &Engine, c: &City, dcode: &str, date_str: &str) -> Result<()> {
        let (fc, precip) = engine::weather::forecast_for(&eng.http, c.lat, c.lon, date_str).await?;
        let corrected = fc - c.bias;

        if precip > 0.0 {
            logging::record(
                LOG,
                json!({"event":"skip_wet","city":c.code,"fc":fc,"corrected":corrected,"precip":precip}),
            );
            logging::info(format!("{}: wet day (precip={precip}) — skip", c.code));
            return Ok(());
        }

        let markets = eng.kalshi.markets(&c.series, "open").await?;
        let mkt = match Self::bucket_market(&markets, corrected, dcode) {
            Some(m) => m,
            None => {
                logging::info(format!(
                    "{}: no {dcode} bucket for {corrected:.1}F — skip",
                    c.code
                ));
                return Ok(());
            }
        };

        let ask = match mkt.yes_ask_cents() {
            Some(a) => a,
            None => {
                logging::info(format!("{}: {} unpriced — skip", c.code, mkt.ticker));
                return Ok(());
            }
        };

        let signal = Signal {
            strategy: "weather".into(),
            ticker: mkt.ticker.clone(),
            side: Side::Yes,
            limit_cents: ask,
            cluster: format!("weather:{date_str}"),
            sizing: SizingHint::Flat,
            fill_wait_secs: 5,
        };

        let outcome = eng.execute(signal).await;
        let mut rec = json!({
            "event":"decision","city":c.code,"fc":fc,"corrected":corrected,"precip":precip,
            "ticker":mkt.ticker,"bucket":mkt.yes_sub_title,"ask_cents":ask
        });
        match &outcome {
            ExecOutcome::Filled { fill, response, .. } if fill.simulated => {
                rec["result"] = json!({"paper": true, "count": fill.filled, "simulated": true, "order": response});
                logging::info(format!(
                    "{}: [paper] buy {}x {} @ {ask}c (fc {fc:.1} -> {corrected:.1}F)",
                    c.code, fill.filled, mkt.ticker
                ));
            }
            ExecOutcome::Filled { fill, response, .. } => {
                rec["result"] = json!({"filled": true, "count": fill.filled,
                    "fill_price": fill.fill_price_cents, "partial": fill.partial,
                    "canceled": fill.canceled, "order": response});
                logging::info(format!(
                    "{}: FILLED {}x {} @ {}c{}",
                    c.code,
                    fill.filled,
                    mkt.ticker,
                    fill.fill_price_cents,
                    if fill.partial { " (partial)" } else { "" }
                ));
                alert::notify(
                    &eng.http,
                    &format!(
                        "{}: FILLED {}x {} @ {}c",
                        c.code, fill.filled, mkt.ticker, fill.fill_price_cents
                    ),
                )
                .await;
            }
            ExecOutcome::Missed { fill, .. } => {
                rec["result"] = json!({"missed": true, "canceled": fill.canceled});
                logging::info(format!(
                    "{}: MISSED (no fill, canceled) {}",
                    c.code, mkt.ticker
                ));
            }
            ExecOutcome::Rejected(r) => {
                rec["result"] = json!({"rejected": format!("{r:?}")});
                logging::info(format!("{}: rejected ({r:?}) — {}", c.code, mkt.ticker));
            }
            ExecOutcome::OrderError(e) => {
                rec["result"] = json!({"error": e});
                logging::info(format!("{}: ORDER FAILED ({e})", c.code));
                alert::notify(
                    &eng.http,
                    &format!("{}: ORDER FAILED {} ({e})", c.code, mkt.ticker),
                )
                .await;
            }
        }
        logging::record(LOG, rec);
        Ok(())
    }
}
