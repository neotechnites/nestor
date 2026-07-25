//! Volbook sleeve — BUILD #2 (strategy #2, metal daily-wing seller).
//! Sells systematically-rich OTM wings on metal daily ladders (gold/silver/copper)
//! Mon-Wed near T-3h by buying NO, ranked/limited by the corpus calibration gap
//! (implied vs realized touch). Verdict: work/verify-b9-widened.md. PAPER-ONLY
//! until sized — see `strategy` module gating.

pub mod calib;
pub mod signal;
pub mod strategy;
