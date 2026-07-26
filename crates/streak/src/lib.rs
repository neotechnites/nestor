//! Streak sleeve — post-4-streak reversal on KXBTC15M + KXETH15M, first 60s
//! only, hold to settle. 2-yr regime-proof (56-57% every slice, both coins, fees
//! in). Week-1 live purpose is MECHANICS measurement, not efficacy — see
//! `data/streak_week1.jsonl`.
//!
//! EXECUTION (2026-07-26, `work/verify-streak-execution.md`): the taker-only
//! "fire iff observed ask ≤44" entry is superseded by a fitted Cont-Kukanov
//! policy — rest a full-size 40¢ bid on the reversal side from T0, cancel on a
//! flip, and backstop with an IOC at a 46¢ ceiling at T0+45s. See [`exec`] for
//! every number and its derivation.

pub mod derive;
pub mod exec;
pub mod signal;
pub mod strategy;
