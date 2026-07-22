//! Weather sleeve — the daily forecast-buy strategy.
//!
//! At ~9am ET, for each tradeable city: forecast -> bias-correct -> map to the
//! correct day's 2F bucket -> skip wet days -> buy tiny (or paper-log) -> hold
//! to settlement.

use anyhow::Result;
use async_trait::async_trait;
use chrono::Datelike;
use chrono_tz::America::New_York;
use engine::config::{tradeable_cities, City};
use engine::kalshi::Market;
use engine::sizing::contracts_for;
use engine::{logging, Engine, Mode, Strategy};
use serde_json::json;

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
        logging::info(format!(
            "weather start — mode={:?} date={date_str} ({dcode}) stake=${} max_daily=${}",
            eng.mode, eng.stake_usd, eng.max_daily_usd
        ));

        let mut spent = 0.0_f64;
        for c in tradeable_cities() {
            if let Err(e) = self.run_city(eng, &c, &dcode, &date_str, &mut spent).await {
                logging::info(format!("{}: error ({e}) — skip", c.code));
            }
            if spent + eng.stake_usd > eng.max_daily_usd {
                logging::info(format!("daily cap ${} reached — stop", eng.max_daily_usd));
                break;
            }
        }
        logging::info(format!("weather done — deployed ${spent}"));
        Ok(())
    }
}

impl Weather {
    async fn run_city(
        &self,
        eng: &Engine,
        c: &City,
        dcode: &str,
        date_str: &str,
        spent: &mut f64,
    ) -> Result<()> {
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

        let markets = eng.kalshi.markets(c.series, "open").await?;
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
            Some(a) if a > 2 && a < 98 => a,
            other => {
                let shown = other
                    .map(|a| format!("{a}c"))
                    .unwrap_or_else(|| "none".into());
                logging::info(format!(
                    "{}: {} ask={shown} out of band — skip",
                    c.code, mkt.ticker
                ));
                return Ok(());
            }
        };

        let n = contracts_for(eng.stake_usd, ask);
        let mut decision = json!({
            "event":"signal","city":c.code,"fc":fc,"corrected":corrected,"precip":precip,
            "ticker":mkt.ticker,"bucket":mkt.yes_sub_title,"ask_cents":ask,
            "contracts":n,"stake":eng.stake_usd,"mode":format!("{:?}",eng.mode)
        });

        if eng.mode == Mode::Live && n > 0 {
            let coid = uuid::Uuid::new_v4().to_string();
            match eng
                .kalshi
                .place_limit_buy(&mkt.ticker, "yes", n, ask, &coid)
                .await
            {
                Ok(resp) => {
                    decision["order"] = resp;
                    *spent += eng.stake_usd;
                    logging::info(format!("{}: BOUGHT {n}x {} @ {ask}c", c.code, mkt.ticker));
                }
                Err(e) => {
                    decision["error"] = json!(e.to_string());
                    logging::info(format!("{}: ORDER FAILED ({e})", c.code));
                }
            }
        } else {
            *spent += eng.stake_usd;
            logging::info(format!(
                "{}: [paper] would buy {n}x {} @ {ask}c (fc {fc:.1} -> {corrected:.1}F)",
                c.code, mkt.ticker
            ));
        }

        logging::record(LOG, decision);
        Ok(())
    }
}
