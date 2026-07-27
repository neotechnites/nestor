//! Streak sleeve — post-4-streak reversal on KXBTC15M + KXETH15M, first 60s
//! only, hold to settle. 2-yr regime-proof (56-57% every slice, both coins, fees
//! in). Week-1 live purpose is MECHANICS measurement, not efficacy — see
//! `data/streak_week1.jsonl`.
//!
//! EXECUTION (2026-07-26, `work/verify-streak-execution.md`): the taker-only
//! "fire iff observed ask ≤44" entry is superseded by a fitted Cont-Kukanov
//! policy — rest a full-size 40¢ bid on the reversal side, cancel on a flip, and
//! backstop with an IOC at a 46¢ ceiling at T0+45s. See [`exec`] for every
//! number and its derivation.
//!
//! DISCOVERY + REST TIMING (2026-07-27, `work/build-pret0-discovery.md`;
//! evidence in `work/lane-VENUE-MECHANICS-jul27.md`). NOTHING ABOUT THE POLICY
//! MOVED — the rung is still 40¢, the ceiling still 46¢, the backstop still
//! T0+45, the expiry still T0+60. Only the CLOCK moved:
//!   - markets are discovered by CONSTRUCTED TICKER through the uncached
//!     single-market GET, not through `GET /markets?status=open` — which is a
//!     15.00s per-series phase-locked cache grid whose first sighting of a new
//!     window lands at a median T0+21.2s (BTC) / T0+31.9s (ETH). Over n=536
//!     windows only 1.5% were seen by the T0+4.8s dip the 40¢ rest is fitted on;
//!     the policy was being executed ~26s late, every window.
//!   - when derive-fourth is already decisive before the boundary, the bid is
//!     rested at T0−10s on the NEXT window's market, and re-confirmed against
//!     the COMPLETE settlement window the instant the boundary passes.

pub mod derive;
pub mod exec;
pub mod signal;
pub mod strategy;
