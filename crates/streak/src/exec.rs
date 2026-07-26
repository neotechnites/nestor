//! Fitted execution policy for the 4-streak reversal signal — pure, testable
//! (`work/verify-streak-execution.md`, Fable rulings of note 39 applied).
//!
//! THE SHAPE OF THE ANSWER. Cont & Kukanov's single-exchange closed form says
//! that when the underfill penalty is small (not trading costs us only forgone
//! EV, ~4-13¢ against a 100¢ notional) and the target size is tiny, the optimum
//! is `(M*, L*) = (0, S)`: post everything as a limit, take nothing. Our problem
//! bends that in one place — a hard 60s deadline with information decay — which
//! the latency-execution literature answers with "tilt toward taking as the
//! deadline nears". Net: **rest cheap for the bulk of capture, backstop-take
//! before the deadline up to a +EV ceiling.** Our own tape produces the same
//! prescription independently (§3 of the ledger).
//!
//! WHY THESE NUMBERS. Reversal-side ask path, first 60s after T0, 174
//! windows/coin over 2.3 days:
//!   - the reversal side opens at a MEDIAN 53¢ — above the 50.3¢ breakeven at a
//!     52% win rate. Taking at open is −EV. Do not chase.
//!   - it DIPS during the window (median min 47¢ after prev-1, 44¢ after a real
//!     4-streak) and the dip bottoms EARLY (median time-to-min 4.8s after a
//!     4-streak), then sweeps back up (55% sweep ≥3¢). The cheap fills are
//!     transient: you must already be resting.
//!   - P(min ask ≤ 40 within 60s) = 24% (prev-1, the conservative set).
//!
//! EV(p) = w·100 − p − fee, fee(p) = 7·p·(100−p)/10000 ¢. At w=52%: EV(40) =
//! +10.3¢, EV(46) = +4.3¢, breakeven 50.3¢. At w=54.7%: EV(40) = +13.0¢,
//! EV(46) = +7.0¢, breakeven 53.0¢.

use engine::strategy::Mode;

/// Resting maker bid, in cents. DERIVED: single level, not a ladder. The ladder
/// [44,38,32] tested +0.1¢ better in the shallow-dip prev-1 regime but WORSE in
/// the deep-dip 4-streak regime that is actually ours — its 44 leg fills at 44
/// exactly where a lone 40 bid would have filled at 40. 40 is the knee: P(fill)
/// 24% at EV +10.3¢ beats 21%@38 (+12.3¢) and 30%@42 (+8.3¢) on the product,
/// and it sits below the 44¢ median dip of a real 4-streak so a normal dip
/// clears it rather than stopping just above.
pub const MAKER_PRICE_CENTS: i64 = 40;

/// Taker backstop ceiling, in cents — SHIPS AT 46, NOT the researcher's 48.
/// Fable ruling (note 39): the 45-48 window population is NEW (the old 44¢ gate
/// never traded it) and the researcher's own biggest flagged risk — that the
/// conditional win rate in swept/never-dipped windows is below the assumed 52% —
/// lives exactly there. 46 keeps a ~2.5× fee cushion (EV +4.3¢ vs 1.74¢ fee) at
/// the CONSERVATIVE 52% rate. This is a DIAL, not a constant of nature: it walks
/// to [`TAKER_CEILING_MAX`] only on live evidence that fills at 45-46 win at the
/// assumed rate. Override with `STREAK_CEILING` (clamped to the band below).
pub const TAKER_CEILING_CENTS: i64 = 46;
/// Hard upper stop for the ceiling dial: the researcher's own +EV ceiling at the
/// conservative win rate (breakeven 50.3¢, so 48 keeps a 2.3¢ margin). Nothing
/// may push the ceiling above this without new research.
pub const TAKER_CEILING_MAX: i64 = 48;
/// Hard lower stop: below the maker price the backstop would be a worse price
/// than the bid we just cancelled — incoherent.
pub const TAKER_CEILING_MIN: i64 = MAKER_PRICE_CENTS;

/// Seconds after T0 at which the maker leg is cancelled and the taker backstop
/// fires. DERIVED from two facts pulling opposite ways: (a) the taker fill rate
/// RISES through the window (15% at open → 21% at t≈45-55s) as the ask
/// mean-reverts down, so later is better; (b) the whole sequence must finish
/// inside the 60s entry window. At 45s the cancel round-trip (~0.2-0.4s) plus
/// the full IOC retry ladder (4 attempts × 2s ≈ 7s) lands the last attempt at
/// ~T0+52s, comfortably inside the `MIN_TTC_SECS` guard.
pub const BACKSTOP_AT_SECS: i64 = 45;

/// `expiration_ts` for the maker leg, as seconds after T0. DERIVED: the entry
/// window ends at T0+60 and a fill after it is a position we never decided to
/// take, so the order must not outlive the window. This is the DEAD-PROCESS
/// backstop only — in normal operation we actively cancel at
/// [`BACKSTOP_AT_SECS`] or on a flip. Kalshi enforces expiry LAZILY (~2-3min
/// sweep, demo-measured), so a crash can still leave the bid live past T0+60;
/// accepted at $4 of size, exactly as the house sleeve accepted it.
pub const MAKER_EXPIRY_SECS: i64 = 60;

/// Minimum seconds a maker leg must be able to rest before the backstop for
/// posting it to be worth the round-trips. DERIVED from cost, not from a
/// fill-rate threshold: posting costs one create + one cancel RTT (~0.4-0.8s
/// total) and delays the backstop by the cancel leg; the benefit is a shot at
/// a −6¢-better price. So the rule is "post whenever there is time to place,
/// observe at least one 1 Hz poll, and cancel" — 5s covers all three with
/// margin. Below it, go straight to the taker.
pub const MIN_REST_SECS: i64 = 5;

/// Entry attempts for a TAKER leg: 1 initial IOC + 3 retries. The ask flickers
/// and RETURNS — P(still/again ≤ limit at +5s | ≤ limit at t) = 0.926 on the
/// 100ms week-1 tape; measured one-shot fill 70.5% → 88.5% with the ladder
/// (verify-streak-retry 2026-07-25).
pub const MAX_ENTRY_ATTEMPTS: u32 = 4;
/// Spacing between taker retries (2s: past most sub-second flicker; 4 attempts
/// still span only ~7s).
pub const RETRY_SPACING_MS: u64 = 2000;

/// Env dial for the taker ceiling.
const CEILING_ENV: &str = "STREAK_CEILING";

/// Which leg actually produced (or attempted) the entry — the §5
/// adverse-selection risk is MEASURED, not assumed, so every participation
/// record carries this.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryPath {
    /// Filled from the resting 40¢ bid (or it crossed at post).
    MakerRest,
    /// Maker leg went unfilled to the deadline; filled/attempted by the IOC at
    /// the ceiling.
    TakerBackstop,
    /// Signal arrived too late to rest anything — taker-only.
    TakerLate,
}

impl EntryPath {
    pub fn as_str(self) -> &'static str {
        match self {
            EntryPath::MakerRest => "maker_rest",
            EntryPath::TakerBackstop => "taker_backstop",
            EntryPath::TakerLate => "taker_late",
        }
    }
}

/// The effective taker ceiling in cents: [`TAKER_CEILING_CENTS`] unless
/// `STREAK_CEILING` overrides it, always clamped to
/// `[TAKER_CEILING_MIN, TAKER_CEILING_MAX]`. A garbage value falls back to the
/// default rather than trading on a typo.
pub fn taker_ceiling() -> i64 {
    ceiling_from(std::env::var(CEILING_ENV).ok().as_deref())
}

/// Pure core of [`taker_ceiling`] (unit-tested without touching the env).
pub fn ceiling_from(raw: Option<&str>) -> i64 {
    raw.and_then(|s| s.trim().parse::<i64>().ok())
        .map(|v| v.clamp(TAKER_CEILING_MIN, TAKER_CEILING_MAX))
        .unwrap_or(TAKER_CEILING_CENTS)
}

/// Unix second at which the maker leg is cancelled and the backstop fires.
pub fn backstop_at(t0: i64) -> i64 {
    t0 + BACKSTOP_AT_SECS
}

/// `expiration_ts` for a maker leg posted in the window opening at `t0`.
pub fn maker_expiration(t0: i64) -> i64 {
    t0 + MAKER_EXPIRY_SECS
}

/// Is there enough runway to bother resting? See [`MIN_REST_SECS`].
pub fn maker_eligible(now: i64, t0: i64) -> bool {
    backstop_at(t0) - now >= MIN_REST_SECS
}

/// Which leg a signal discovered at `now` (window opened at `t0`) starts on.
pub fn initial_path(now: i64, t0: i64) -> EntryPath {
    if maker_eligible(now, t0) {
        EntryPath::MakerRest
    } else {
        EntryPath::TakerLate
    }
}

/// PAPER fill model for the resting leg, mirroring the ledger's §3 model
/// exactly: a bid at `price` fills at `price` iff the reversal ask trades at or
/// below it. Live mode never calls this — the exchange decides.
pub fn paper_maker_fills(observed_ask: Option<f64>, price: i64) -> bool {
    observed_ask.is_some_and(|a| a <= price as f64)
}

/// PAPER limit for a taker leg: the OBSERVED ask when it is at or below the
/// ceiling, else `None` (no cross — the honest outcome). Paper must never
/// pretend it paid the ceiling for a cheaper ask, nor that it filled through
/// one it could not reach.
///
/// LIVE always sends the ceiling itself: IOC price improvement pays the resting
/// ask whenever that is lower (verified live — a 28¢ fill on a higher limit), so
/// the ceiling limit only widens flicker tolerance and cannot worsen the price
/// of liquidity that is already there. Sending it unconditionally also removes
/// any dependence on a REST quote that lags the engine 0.5-3s: an IOC at 46
/// against a 60¢ book simply returns `fill_count 0`, which IS the correct
/// no-trade.
pub fn taker_limit(mode: Mode, observed_ask: Option<f64>, ceiling: i64) -> Option<i64> {
    if mode == Mode::Live {
        return Some(ceiling);
    }
    match observed_ask {
        Some(a) if a.round() as i64 <= ceiling => Some(a.round() as i64),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ceiling_ships_at_46_and_is_a_clamped_dial() {
        assert_eq!(ceiling_from(None), 46);
        assert_eq!(ceiling_from(Some("48")), 48); // the walk-up target
        assert_eq!(ceiling_from(Some("52")), 48); // NEVER above the researcher's ceiling
        assert_eq!(ceiling_from(Some("44")), 44); // shading DOWN is allowed (§5 risk)
        assert_eq!(ceiling_from(Some("10")), 40); // never below the maker price
        assert_eq!(ceiling_from(Some("nonsense")), 46); // typo → default, not a trade
        assert_eq!(ceiling_from(Some(" 47 ")), 47);
    }

    #[test]
    fn backstop_and_expiry_sit_inside_the_entry_window() {
        let t0 = 1_784_987_100;
        assert_eq!(backstop_at(t0), t0 + 45);
        assert_eq!(maker_expiration(t0), t0 + 60);
        // The whole IOC ladder after the cancel must land inside 60s.
        let ladder_ms = (MAX_ENTRY_ATTEMPTS as u64 - 1) * RETRY_SPACING_MS;
        assert!(BACKSTOP_AT_SECS + (ladder_ms / 1000) as i64 + 1 < MAKER_EXPIRY_SECS);
        // And the maker leg must never outlive the window it was decided in.
        assert!(maker_expiration(t0) <= t0 + crate::signal::WINDOW_SECS);
    }

    #[test]
    fn maker_eligible_until_five_seconds_before_the_backstop() {
        let t0 = 1_000_000;
        assert!(maker_eligible(t0, t0)); // at the open
        assert!(maker_eligible(t0 + 40, t0)); // exactly MIN_REST_SECS of runway
        assert!(!maker_eligible(t0 + 41, t0)); // one second short
        assert!(!maker_eligible(t0 + 55, t0));
        assert_eq!(initial_path(t0 + 2, t0), EntryPath::MakerRest);
        assert_eq!(initial_path(t0 + 50, t0), EntryPath::TakerLate);
    }

    #[test]
    fn backstop_size_is_re_derived_at_its_own_price() {
        // The $4 flat is a CAP ON CAPITAL AT RISK per entry, not a contract
        // target — so the backstop must NOT reuse the maker leg's count. The
        // risk layer's `contracts_for(stake, limit)` already does this, which is
        // why the backstop needs no special sizing code: it just routes through
        // `evaluate` at the ceiling. Keeping 10 contracts at 46 would put $4.60
        // at risk, 15% over budget.
        const FLAT_USD: f64 = 4.0;
        let maker = engine::sizing::contracts_for(FLAT_USD, MAKER_PRICE_CENTS);
        let backstop = engine::sizing::contracts_for(FLAT_USD, TAKER_CEILING_CENTS);
        assert_eq!(maker, 10); // $4.00 at risk
        assert_eq!(backstop, 8); // $3.68 at risk — under budget, never over
        assert!(backstop as f64 * TAKER_CEILING_CENTS as f64 / 100.0 <= FLAT_USD);
        assert!((maker + 1) as f64 * MAKER_PRICE_CENTS as f64 / 100.0 > FLAT_USD);
    }

    #[test]
    fn paper_maker_fill_model_matches_the_ledger() {
        assert!(paper_maker_fills(Some(40.0), 40)); // at the bid → fills
        assert!(paper_maker_fills(Some(37.5), 40)); // through it → fills
        assert!(!paper_maker_fills(Some(40.5), 40)); // just above → no
        assert!(!paper_maker_fills(None, 40)); // unpriced → no
    }

    #[test]
    fn taker_limit_live_always_sends_the_ceiling() {
        // Live: the limit IS the gate; a stale/expensive quote must not veto.
        assert_eq!(taker_limit(Mode::Live, Some(60.0), 46), Some(46));
        assert_eq!(taker_limit(Mode::Live, None, 46), Some(46));
        assert_eq!(taker_limit(Mode::Live, Some(31.0), 46), Some(46));
    }

    #[test]
    fn taker_limit_paper_prices_at_the_observed_ask_or_declines() {
        assert_eq!(taker_limit(Mode::Paper, Some(31.4), 46), Some(31));
        assert_eq!(taker_limit(Mode::Paper, Some(46.0), 46), Some(46));
        assert_eq!(taker_limit(Mode::Paper, Some(46.6), 46), None); // rounds to 47 > 46
        assert_eq!(taker_limit(Mode::Paper, Some(53.0), 46), None);
        assert_eq!(taker_limit(Mode::Paper, None, 46), None);
    }
}
