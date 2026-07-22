//! Nestor engine — the shared, permanent infrastructure every strategy runs on.
//! Exchange client + signing, data feeds, sizing, risk, logging, and the
//! Strategy trait. Strategies are separate crates that implement `Strategy`
//! and are wired into the `nestor` binary.

pub mod config;
pub mod kalshi;
pub mod logging;
pub mod sizing;
pub mod strategy;
pub mod weather;

pub use kalshi::Kalshi;
pub use strategy::{Engine, Mode, Strategy};
