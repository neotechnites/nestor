//! Lock-edge backtest — reproduces the vault's forward-test result IN the
//! production signal code, against cached data (`~/kalshi_data/forward_lock_*`).
//! Public, read-only, no keys. Confirms the Rust `signal::evaluate` yields the
//! same ~99.3% win / +3.25%/trade the Python research found.

use std::collections::HashMap;

use anyhow::{Context, Result};
use serde::Deserialize;

use crate::signal::{self, LockParams};

const CHECKPOINTS: [i64; 8] = [240, 180, 150, 120, 90, 60, 45, 30];

#[derive(Debug, Deserialize)]
struct Market {
    ticker: String,
    result: String,
    #[serde(rename = "K")]
    strike: f64,
    #[allow(dead_code)]
    open: i64,
    close: i64,
}

/// One tick: [unix_secs, yes_price_cents, taker_side].
type Tick = (i64, f64, String);

pub struct Summary {
    pub n: usize,
    pub wins: usize,
    pub mean_ev_cents: f64,
    pub mean_ev_pct: f64,
}

impl Summary {
    pub fn win_pct(&self) -> f64 {
        if self.n == 0 {
            0.0
        } else {
            self.wins as f64 / self.n as f64 * 100.0
        }
    }
}

fn load_btc(path: &str) -> Result<Vec<(i64, f64)>> {
    let text = std::fs::read_to_string(path).with_context(|| format!("reading {path}"))?;
    let mut v = Vec::new();
    for line in text.lines() {
        let mut it = line.split(',');
        if let (Some(a), Some(b)) = (it.next(), it.next()) {
            if let (Ok(ts), Ok(px)) = (a.trim().parse::<i64>(), b.trim().parse::<f64>()) {
                v.push((ts, px));
            }
        }
    }
    v.sort_by_key(|x| x.0);
    Ok(v)
}

/// Last value at/before `t` (within 180s staleness), via binary search.
fn spot_at(btc: &[(i64, f64)], t: i64) -> Option<f64> {
    let i = btc.partition_point(|x| x.0 <= t);
    if i == 0 {
        return None;
    }
    let (ts, px) = btc[i - 1];
    if t - ts > 180 {
        None
    } else {
        Some(px)
    }
}

/// Median absolute 1-min move over the 15 min prior to `t`.
fn median_1min(btc: &[(i64, f64)], t: i64) -> Option<f64> {
    let i = btc.partition_point(|x| x.0 <= t);
    if i < 16 {
        return None;
    }
    let mut diffs: Vec<f64> = (i - 15..i)
        .map(|j| (btc[j].1 - btc[j - 1].1).abs())
        .collect();
    diffs.sort_by(|a, b| a.total_cmp(b));
    Some(diffs[diffs.len() / 2])
}

/// Last YES price at/before `t`, via binary search on the ascending tick list.
fn price_at(ticks: &[Tick], t: i64) -> Option<f64> {
    let i = ticks.partition_point(|x| x.0 <= t);
    if i == 0 {
        None
    } else {
        Some(ticks[i - 1].1)
    }
}

/// Kalshi taker fee in ¢ for one contract at `ask` ¢.
fn fee_cents(ask: f64) -> f64 {
    0.07 * ask * (1.0 - ask / 100.0)
}

fn run_cell(
    markets: &[Market],
    ticks: &HashMap<String, Vec<Tick>>,
    btc: &[(i64, f64)],
    params: &LockParams,
    spread: f64,
) -> Summary {
    let (mut n, mut wins, mut sum_ev, mut sum_pct) = (0usize, 0usize, 0.0, 0.0);
    for m in markets {
        let tk = match ticks.get(&m.ticker) {
            Some(t) => t,
            None => continue,
        };
        for sb in CHECKPOINTS {
            let tg = m.close - sb;
            let (p, s, mv) = match (price_at(tk, tg), spot_at(btc, tg), median_1min(btc, tg)) {
                (Some(p), Some(s), Some(mv)) if mv > 0.0 => (p, s, mv),
                _ => continue,
            };
            let entry = match signal::evaluate(p, s, m.strike, mv, sb as f64 / 60.0, params) {
                Some(e) => e,
                None => continue,
            };
            // One entry per market (first qualifying checkpoint).
            let ask = entry.fav_price + spread;
            let won = if entry.fav_is_yes {
                m.result == "yes"
            } else {
                m.result == "no"
            };
            let ev = if won {
                100.0 - ask - fee_cents(ask)
            } else {
                -ask
            };
            n += 1;
            if won {
                wins += 1;
            }
            sum_ev += ev;
            sum_pct += ev / ask * 100.0;
            break;
        }
    }
    Summary {
        n,
        wins,
        mean_ev_cents: if n > 0 { sum_ev / n as f64 } else { 0.0 },
        mean_ev_pct: if n > 0 { sum_pct / n as f64 } else { 0.0 },
    }
}

/// Load the cached forward-lock data and print the main cell + a robustness grid.
pub fn run() -> Result<()> {
    let home = std::env::var("HOME").context("HOME not set")?;
    let dir = format!("{home}/kalshi_data");
    let markets: Vec<Market> = serde_json::from_str(
        &std::fs::read_to_string(format!("{dir}/forward_lock_markets.json"))
            .context("reading forward_lock_markets.json")?,
    )
    .context("parsing markets")?;
    let ticks: HashMap<String, Vec<Tick>> = serde_json::from_str(
        &std::fs::read_to_string(format!("{dir}/forward_lock_ticks.json"))
            .context("reading forward_lock_ticks.json")?,
    )
    .context("parsing ticks")?;
    let btc = load_btc(&format!("{dir}/forward_btc_1min.csv"))?;

    println!(
        "lock backtest — {} markets, {} with ticks, {} btc 1-min bars",
        markets.len(),
        ticks.len(),
        btc.len()
    );
    let spread = 0.5;

    println!("\nMAIN cell (fav 93-97c, Z>=4, +0.5c spread):");
    let m = run_cell(&markets, &ticks, &btc, &LockParams::default(), spread);
    println!(
        "  n={} win={:.2}% losses={} netEV={:+.2}c ({:+.2}%/trade)",
        m.n,
        m.win_pct(),
        m.n - m.wins,
        m.mean_ev_cents,
        m.mean_ev_pct
    );

    println!("\nrobustness (netEV %/trade, n, win%):");
    for (lo, hi, label) in [
        (93.0, 97.0, "93-97"),
        (93.0, 95.0, "93-95"),
        (95.0, 97.0, "95-97"),
    ] {
        for zmin in [4.0, 5.0, 6.0] {
            let s = run_cell(
                &markets,
                &ticks,
                &btc,
                &LockParams {
                    z_min: zmin,
                    price_lo: lo,
                    price_hi: hi,
                },
                spread,
            );
            println!(
                "  {label:>6} Z>={zmin}: {:+.2}% n={} win={:.1}%",
                s.mean_ev_pct,
                s.n,
                s.win_pct()
            );
        }
    }
    Ok(())
}
