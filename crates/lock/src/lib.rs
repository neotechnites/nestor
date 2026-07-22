//! Lock sleeve — the deep-longshot fade edge (vault's strongest). This crate holds
//! the pure signal (`signal`) and a backtest that reproduces the forward-test result
//! in-code (`backtest`). The live always-on sleeve (WebSocket + last-2-min entry)
//! is a later phase — see docs/specs/02-lock-sleeve.md.

pub mod backtest;
pub mod signal;
