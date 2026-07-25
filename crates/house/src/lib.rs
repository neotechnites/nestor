//! House fill-probe (BUILD H10/H9) — the MAKER sleeve. Posts two-sided RESTING
//! quotes on KXAPRPOTUS (front-weekly in-band strike) and KXCPIYOY (nearest
//! "Exactly" rung) to measure the one thing trade-print markout can't: does a
//! resting quote actually get FILLED at the assumed spread, and does the mid gap
//! through it between prints? Protocol: work/probe-house.md. PAPER/SHADOW until
//! HOUSE_PROBE=1 in live — see `strategy` module gating.

pub mod report;
pub mod signal;
pub mod strategy;

pub use strategy::House;
