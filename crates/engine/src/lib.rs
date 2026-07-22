//! Nestor engine — the shared, permanent infrastructure every strategy runs on.
//! Exchange client + signing, data feeds, sizing, risk, logging, and the
//! Strategy trait. Strategies are separate crates that implement `Strategy`
//! and are wired into the `nestor` binary.

pub mod calibrate;
pub mod config;
pub mod kalshi;
pub mod logging;
pub mod reconcile;
pub mod risk;
pub mod selftest;
pub mod sizing;
pub mod state;
pub mod strategy;
pub mod weather;

pub use kalshi::Kalshi;
pub use risk::{Order, Rejection, RiskConfig, RiskManager, Side, Signal, SizingHint};
pub use strategy::{Engine, ExecOutcome, Mode, Strategy};

/// Shared HTTP client with sane timeouts. Every network call in Nestor uses this
/// so a hung request can never stall the live trading window indefinitely.
pub fn http_client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .connect_timeout(std::time::Duration::from_secs(10))
        .build()
        .expect("failed to build HTTP client")
}
