//! Streak sleeve — BUILD #1 per the 2026-07-23 redirect (vault: implementation/03).
//! Post-4-streak reversal on KXBTC15M + KXETH15M, first 60s only, reversal ask
//! ≤ 44¢, taker-only, hold to settle. 2-yr regime-proof (56-57% every slice,
//! both coins, fees in). Week-1 live purpose is MECHANICS measurement, not
//! efficacy — see `data/streak_week1.jsonl`.

pub mod signal;
pub mod strategy;
