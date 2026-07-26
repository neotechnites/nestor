//! Runtime configuration, loaded from `nestor.toml` (with sensible defaults if
//! absent). Secrets stay in env/.env — never here. Calibrated per-city biases
//! (T003) are written back into the `[[cities]]` table.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::risk::RiskConfig;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct City {
    pub code: String,
    pub series: String,
    pub lat: f64,
    pub lon: f64,
    pub station: String,
    pub network: String,
    /// deg F, mean(forecast - actual) over a trailing window (T003 fills this).
    pub bias: f64,
    pub tradeable: bool,
}

/// Hard ceiling on the live-money bankroll seed. Refuse to start live with a
/// bankroll above this — a lost/oversized nestor.toml must never set halts at a
/// multiple of the real account or make it drainable (fix 4).
///
/// DERIVED (constants F5, 2026-07-26): **current live bankroll $106.03** (see
/// `data/state.json`; real Kalshi balance $106.28, the Δ$0.25 being the standing
/// genesis offset) **+ ~40% of headroom = $150**. At $100 the cap had already
/// fallen BELOW the live account, so a lost state file could not be recovered:
/// `--fresh-state` with the true balance bailed ("out of range"), and seeding
/// the legal ≤$100 instead set `peak` $6 under the real cash — whereupon the
/// FIRST reconcile pass computed Δ$6.28 > $2.00 and halted immediately. The
/// number is still a SEED SANITY CAP, not a risk limit: it bounds a typo'd or
/// stale config, and the drawdown / daily-loss / divergence switches do the real
/// work. Bump it deliberately whenever the account grows past ~2/3 of it.
pub const MAX_LIVE_BANKROLL: f64 = 150.0;

/// Default bankroll used ONLY in paper mode when nothing is configured. Live mode
/// has NO default — it must be set explicitly and within [`MAX_LIVE_BANKROLL`].
pub const PAPER_DEFAULT_BANKROLL: f64 = 1000.0;

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct Trading {
    pub env: String,
    /// Bankroll seed. `None` = unset (no `[trading] bankroll` in config). Live
    /// mode REQUIRES an explicit value (config or env) — see
    /// [`resolve_bankroll`]; paper falls back to [`PAPER_DEFAULT_BANKROLL`].
    pub bankroll: Option<f64>,
}

impl Default for Trading {
    fn default() -> Self {
        Trading {
            env: "paper".into(),
            bankroll: None,
        }
    }
}

/// Resolve the starting bankroll, enforcing the live seed-pinning rules (fix 4).
/// Precedence: env `NESTOR_BANKROLL` > config `[trading] bankroll`.
/// - Live: a value is MANDATORY and must be in `(0, MAX_LIVE_BANKROLL]` — else
///   refuse (a silent $1000 default would set the kill-switch at 10× the account).
/// - Paper: falls back to [`PAPER_DEFAULT_BANKROLL`] when unset.
pub fn resolve_bankroll(
    live: bool,
    env_bankroll: Option<f64>,
    cfg_bankroll: Option<f64>,
) -> anyhow::Result<f64> {
    let chosen = env_bankroll.or(cfg_bankroll);
    if live {
        let b = chosen.ok_or_else(|| {
            anyhow::anyhow!(
                "live mode requires an explicit bankroll (set NESTOR_BANKROLL or \
                 [trading] bankroll in nestor.toml) — refusing to start on a default"
            )
        })?;
        if !(b > 0.0 && b <= MAX_LIVE_BANKROLL) {
            anyhow::bail!(
                "live bankroll ${b:.2} out of range (0, ${MAX_LIVE_BANKROLL:.2}] — refusing to \
                 start (guards against an oversized/lost config draining the account)"
            );
        }
        Ok(b)
    } else {
        Ok(chosen.unwrap_or(PAPER_DEFAULT_BANKROLL))
    }
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(default)]
pub struct Settings {
    pub trading: Trading,
    pub risk: RiskConfig,
    pub cities: Vec<City>,
}

impl Settings {
    /// Load from a TOML path. Missing file → all defaults. Empty `[[cities]]`
    /// → the built-in default city table.
    pub fn load(path: &str) -> anyhow::Result<Settings> {
        let mut s: Settings = match std::fs::read_to_string(path) {
            Ok(text) => toml::from_str(&text)?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Settings::default(),
            Err(e) => return Err(e.into()),
        };
        if s.cities.is_empty() {
            s.cities = default_cities();
        }
        Ok(s)
    }

    pub fn tradeable_cities(&self) -> Vec<City> {
        self.cities
            .iter()
            .filter(|c| c.tradeable)
            .cloned()
            .collect()
    }
}

/// Built-in default city table (used when nestor.toml has no `[[cities]]`).
/// series + station MUST be re-verified live (T005); biases are placeholders
/// until calibration (T003). Tradeable-6 per the 2026-07-15 forward test.
pub fn default_cities() -> Vec<City> {
    fn c(
        code: &str,
        series: &str,
        lat: f64,
        lon: f64,
        station: &str,
        network: &str,
        tradeable: bool,
    ) -> City {
        City {
            code: code.into(),
            series: series.into(),
            lat,
            lon,
            station: station.into(),
            network: network.into(),
            bias: 1.5,
            tradeable,
        }
    }
    vec![
        c("MIA", "KXHIGHMIA", 25.79, -80.29, "MIA", "FL_ASOS", true),
        c("ATL", "KXHIGHTATL", 33.63, -84.44, "ATL", "GA_ASOS", true),
        c("NY", "KXHIGHNY", 40.78, -73.97, "NYC", "NY_ASOS", true),
        c("BOS", "KXHIGHTBOS", 42.36, -71.01, "BOS", "MA_ASOS", true),
        c("PHX", "KXHIGHTPHX", 33.43, -112.00, "PHX", "AZ_ASOS", true),
        c("CHI", "KXHIGHCHI", 41.79, -87.75, "MDW", "IL_ASOS", true),
        c("DEN", "KXHIGHDEN", 39.85, -104.66, "DEN", "CO_ASOS", false),
        c("SEA", "KXHIGHTSEA", 47.44, -122.31, "SEA", "WA_ASOS", false),
    ]
}

#[derive(Deserialize)]
struct BiasEntry {
    bias: f64,
}

/// Overlay calibrated biases (from `data/biases.json`, produced by `calibrate`)
/// onto the city table, overriding each matched city's `bias`. Returns the count
/// applied. Does NOT touch `tradeable`: the tradeable allowlist is a config /
/// season-policy decision, not something a short calibration window should flip
/// (a rosy 60-day MAE must not promote DEN/SEA — see T010). Missing/invalid file
/// is a no-op.
pub fn apply_biases(cities: &mut [City], path: &str) -> usize {
    match std::fs::read_to_string(path) {
        Ok(text) => apply_biases_str(cities, &text),
        Err(_) => 0,
    }
}

/// Pure core of [`apply_biases`] (testable without a file).
pub fn apply_biases_str(cities: &mut [City], json: &str) -> usize {
    let map: HashMap<String, BiasEntry> = match serde_json::from_str(json) {
        Ok(m) => m,
        Err(_) => return 0,
    };
    let mut n = 0;
    for c in cities.iter_mut() {
        if let Some(e) = map.get(&c.code) {
            c.bias = e.bias;
            n += 1;
        }
    }
    n
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn apply_biases_overrides_bias_not_tradeable() {
        let mut cities = default_cities();
        // DEN is tradeable=false in defaults and has a rosy calibrated bias here.
        let json = r#"{"MIA":{"bias":-1.18,"mae":0.9,"tradeable":true},
                       "DEN":{"bias":-1.24,"mae":0.8,"tradeable":true}}"#;
        let n = apply_biases_str(&mut cities, json);
        assert_eq!(n, 2);
        let mia = cities.iter().find(|c| c.code == "MIA").unwrap();
        let den = cities.iter().find(|c| c.code == "DEN").unwrap();
        assert!((mia.bias - -1.18).abs() < 1e-9); // bias overridden
        assert!(!den.tradeable); // tradeable NOT flipped by calibration window
        assert!((den.bias - -1.24).abs() < 1e-9);
    }

    #[test]
    fn apply_biases_missing_or_bad_is_noop() {
        let mut cities = default_cities();
        assert_eq!(apply_biases(&mut cities, "/nonexistent/biases.json"), 0);
        assert_eq!(apply_biases_str(&mut cities, "not json"), 0);
        assert!((cities[0].bias - 1.5).abs() < 1e-9); // untouched placeholder
    }

    #[test]
    fn resolve_bankroll_seed_pinning() {
        // Paper: unset → paper default; set → honored.
        assert_eq!(
            resolve_bankroll(false, None, None).unwrap(),
            PAPER_DEFAULT_BANKROLL
        );
        assert_eq!(resolve_bankroll(false, Some(250.0), None).unwrap(), 250.0);
        // Live: unset (would silently default to $1000) → REFUSE.
        assert!(resolve_bankroll(true, None, None).is_err());
        // Live: oversized config bankroll → REFUSE.
        assert!(resolve_bankroll(true, None, Some(1000.0)).is_err());
        assert!(resolve_bankroll(true, None, Some(MAX_LIVE_BANKROLL + 0.01)).is_err());
        // Live: zero/negative → REFUSE.
        assert!(resolve_bankroll(true, Some(0.0), None).is_err());
        // Live: valid explicit seed → accepted; env overrides config.
        assert_eq!(resolve_bankroll(true, None, Some(100.0)).unwrap(), 100.0);
        // FIX 12 (constants F5): the cap must clear the CURRENT live account, or
        // a lost state file cannot be recovered — the recovery seed bails, and
        // any legal-but-lower seed halts on the first divergence pass.
        const LIVE_BANKROLL_TODAY: f64 = 106.03;
        const LIVE_REAL_CASH_TODAY: f64 = 106.28;
        assert_eq!(
            resolve_bankroll(true, Some(LIVE_BANKROLL_TODAY), None).unwrap(),
            LIVE_BANKROLL_TODAY
        );
        assert!(resolve_bankroll(true, Some(LIVE_REAL_CASH_TODAY), None).is_ok());
        // ...while still being a real cap, not a rubber stamp.
        assert!(resolve_bankroll(true, Some(1000.0), None).is_err());
        const _: () = assert!(MAX_LIVE_BANKROLL >= 132.85); // 106.28 * 1.25
        assert_eq!(resolve_bankroll(true, Some(50.0), Some(1000.0)).unwrap(), 50.0);
    }

    #[test]
    fn missing_file_uses_defaults() {
        let s = Settings::load("/nonexistent/nestor.toml").unwrap();
        assert_eq!(s.trading.env, "paper");
        assert_eq!(s.trading.bankroll, None); // unset → resolved to paper default
        assert_eq!(s.risk.fraction, 0.05);
        assert_eq!(s.cities.len(), 8);
        assert_eq!(s.tradeable_cities().len(), 6);
    }

    #[test]
    fn parses_toml_and_overrides() {
        let toml = r#"
            [trading]
            env = "live"
            bankroll = 5000.0
            [risk]
            fraction = 0.10
            [[cities]]
            code = "MIA"
            series = "KXHIGHMIA"
            lat = 25.79
            lon = -80.29
            station = "MIA"
            network = "FL_ASOS"
            bias = 0.9
            tradeable = true
        "#;
        let s: Settings = toml::from_str(toml).unwrap();
        assert_eq!(s.trading.env, "live");
        assert_eq!(s.trading.bankroll, Some(5000.0));
        assert_eq!(s.risk.fraction, 0.10);
        // unset risk fields keep defaults
        assert_eq!(s.risk.cluster_cap_frac, 0.15);
        assert_eq!(s.cities.len(), 1);
        assert_eq!(s.cities[0].bias, 0.9);
    }
}
