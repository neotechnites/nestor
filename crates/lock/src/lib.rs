//! Lock sleeve — the deep-longshot fade edge (vault's strongest). Pure signal
//! (`signal`), a backtest reproducing the forward test in-code (`backtest`), a live
//! Coinbase feed (`coinbase`), and the live `Strategy` (`strategy::Lock`) — one scan
//! pass the binary loops for the always-on sleeve. See docs/specs/02-lock-sleeve.md.

pub mod backtest;
pub mod coinbase;
pub mod signal;
pub mod strategy;
