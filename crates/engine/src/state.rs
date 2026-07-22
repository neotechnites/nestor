//! Persistent state: bankroll, open positions, settled history. Survives
//! restarts so a new run (or new session) is consistent. v1 = a JSON file with
//! atomic write; behind a trait so SQLite can replace it later.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use crate::risk::Side;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    pub strategy: String,
    pub ticker: String,
    pub side: Side,
    pub count: i64,
    pub entry_cents: i64,
    pub cluster: String,
    /// ET trading day (YYYY-MM-DD) the position was opened on. Settlement uses
    /// this to attribute a realized loss to the *right* day's daily-loss
    /// counter, so a next-morning reconcile of a prior day can't trip today's
    /// daily-loss kill-switch (T004). Defaulted for state files written pre-T004.
    #[serde(default)]
    pub day: String,
}

impl Position {
    /// Capital at risk in dollars (what a loss forfeits).
    pub fn stake(&self) -> f64 {
        self.count as f64 * self.entry_cents as f64 / 100.0
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Settled {
    pub ticker: String,
    pub won: bool,
    pub pnl: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct State {
    pub bankroll: f64,
    pub peak: f64,
    pub halted: bool,
    /// ET date (YYYY-MM-DD) the daily counters below belong to.
    pub day: String,
    pub day_loss: f64,
    pub day_spent: f64,
    pub open: Vec<Position>,
    pub settled: Vec<Settled>,
}

impl State {
    pub fn new(bankroll: f64) -> Self {
        State {
            bankroll,
            peak: bankroll,
            halted: false,
            day: String::new(),
            day_loss: 0.0,
            day_spent: 0.0,
            open: Vec::new(),
            settled: Vec::new(),
        }
    }
}

pub trait StateStore: Send {
    fn load(&self) -> Result<Option<State>>;
    fn save(&self, s: &State) -> Result<()>;
}

/// JSON file store with atomic write (temp file + rename).
pub struct JsonStore {
    path: std::path::PathBuf,
}

impl JsonStore {
    pub fn new(path: impl Into<std::path::PathBuf>) -> Self {
        Self { path: path.into() }
    }
}

impl StateStore for JsonStore {
    fn load(&self) -> Result<Option<State>> {
        if !self.path.exists() {
            return Ok(None);
        }
        let bytes =
            std::fs::read(&self.path).with_context(|| format!("reading {:?}", self.path))?;
        Ok(Some(
            serde_json::from_slice(&bytes).context("parsing state JSON")?,
        ))
    }

    fn save(&self, s: &State) -> Result<()> {
        if let Some(dir) = self.path.parent() {
            std::fs::create_dir_all(dir).ok();
        }
        let tmp = self.path.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(s)?).context("writing temp state")?;
        std::fs::rename(&tmp, &self.path).context("renaming temp state")?;
        Ok(())
    }
}

/// In-memory store for tests.
#[derive(Default)]
pub struct MemoryStore {
    inner: std::sync::Mutex<Option<State>>,
}

impl StateStore for MemoryStore {
    fn load(&self) -> Result<Option<State>> {
        Ok(self.inner.lock().unwrap().clone())
    }
    fn save(&self, s: &State) -> Result<()> {
        *self.inner.lock().unwrap() = Some(s.clone());
        Ok(())
    }
}
