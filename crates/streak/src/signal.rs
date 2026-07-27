//! Streak signal — pure, testable (redirect 2026-07-23; execution policy
//! 2026-07-26).
//!
//! After 4 consecutive settled 15-min windows printing the same direction, buy
//! the OPPOSITE side of the new window, only in its first 60 seconds. Detection
//! uses settled-market `result` fields ONLY (candles are banned: the 1-min-candle
//! lookahead trap produced a fake 71% signal in research — vault note 18 gotchas).
//!
//! PRICE IS NO LONGER PART OF THE SIGNAL (work/verify-streak-execution.md). The
//! old `observed ask ≤ 44¢` gate is SUPERSEDED by the fitted execution policy:
//! willingness-to-pay is expressed by the 40¢ resting bid and the 46¢ taker
//! ceiling in [`crate::exec`], not by a pre-filter on a quote that is stale by
//! 0.5-3s and whose median value (53¢) would veto the maker leg outright. The
//! reversal-side ask is still carried on [`Entry`] — for the paper fill model,
//! the participation record, and diagnostics — but it never rejects a signal.
//! Consequence: `Skip::PriceAboveGate` and `Skip::Unpriced` are GONE; an
//! unpriced reversal side is a perfectly good place to rest a 40¢ bid.

/// One settled market, newest-first ordering is the caller's job.
#[derive(Debug, Clone)]
pub struct SettledWindow {
    pub close_unix: i64,
    /// "yes" or "no" (non-empty; caller filters unsettled out).
    pub result: String,
}

/// The open market being evaluated for entry.
#[derive(Debug, Clone)]
pub struct Candidate {
    pub open_unix: Option<i64>,
    pub close_unix: i64,
    /// Deci-cent asks in ¢ (None = unpriced).
    pub yes_ask: Option<f64>,
    pub no_ask: Option<f64>,
}

/// A qualifying entry: buy the `buy_yes` side. `ask` is the reversal side's
/// OBSERVED ask (¢, deci-cent) at the decision moment — diagnostic and the
/// paper fill model's input, never a gate. `None` = that side is unpriced.
#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    pub buy_yes: bool,
    pub ask: Option<f64>,
    /// "up" or "down" — the direction of the 4-streak being faded.
    pub streak_dir: &'static str,
}

/// Why a market did NOT produce an entry. Retryable(...) reasons may convert on
/// a later scan pass inside the entry window; the rest are terminal for this
/// market. All are logged — week-1 is a mechanics-measurement exercise.
#[derive(Debug, Clone, PartialEq)]
pub enum Skip {
    /// Fewer than 4 settled results available.
    InsufficientHistory,
    /// The newest 4 settled windows are not exactly 15 min apart.
    NotConsecutive,
    /// Newest 4 settled are not all the same direction — no signal. (The common
    /// case; callers normally don't log this one.)
    NoStreak,
    /// A streak exists but the window immediately before the current market has
    /// not settled yet (newest settled close == current open − 900). Retryable —
    /// it may settle within the entry window. Measures settlement lag.
    PrevNotSettled,
    /// The settled chain doesn't abut the current market at all.
    WindowMismatch,
    /// Current market is past its first 60 seconds (ttc < 14 min). TERMINAL —
    /// the entry window for this market is over and cannot come back.
    NotEntryWindow { ttc: i64 },
    /// The market has not opened yet and is further out than the pre-T0 rest
    /// lead allows (ttc > 900 + [`PRE_T0_LEAD_SECS`]). RETRYABLE: "too early" is
    /// a state the clock cures, unlike "too late".
    ///
    /// SPLIT 2026-07-27 (charter item 3). Both cases used to return
    /// `NotEntryWindow`, and `retryable()` was true only for `PrevNotSettled`,
    /// so ANY market first evaluated before its open was rejected terminally and
    /// never re-examined — which made a pre-T0 maker leg unreachable by
    /// construction (lane-VENUE-MECHANICS-jul27 §"Second blocker").
    TooEarly { ttc: i64 },
}

impl Skip {
    /// Retryable skips may still convert to an entry on a later pass within the
    /// entry window; terminal ones cannot.
    pub fn retryable(&self) -> bool {
        matches!(self, Skip::PrevNotSettled | Skip::TooEarly { .. })
    }

    pub fn as_str(&self) -> String {
        match self {
            Skip::InsufficientHistory => "insufficient_history".into(),
            Skip::NotConsecutive => "not_consecutive".into(),
            Skip::NoStreak => "no_streak".into(),
            Skip::PrevNotSettled => "prev_not_settled".into(),
            Skip::WindowMismatch => "window_mismatch".into(),
            Skip::NotEntryWindow { ttc } => format!("not_entry_window(ttc={ttc}s)"),
            Skip::TooEarly { ttc } => format!("too_early(ttc={ttc}s)"),
        }
    }
}

/// 15-minute window length in seconds.
pub const WINDOW_SECS: i64 = 900;
/// Entry only while time-to-close ≥ 14 min (= within 60s of open).
pub const MIN_TTC_SECS: i64 = 840;

/// How far BEFORE a window's T0 an entry may be decided (and a maker leg
/// rested). ttc for a not-yet-open window is `WINDOW_SECS + (T0 − now)`, so this
/// widens `detect`'s upper bound to `WINDOW_SECS + PRE_T0_LEAD_SECS`.
///
/// DERIVED, NOT CHOSEN. The pre-T0 entry is only as good as the derive-fourth
/// call that authorises it, and `crate::derive` refuses any call whose in-window
/// samples span less than `derive::MIN_SPAN_SECS` (50s) of the
/// `derive::AVG_WINDOW_SECS` (60s) settlement average. At 1 Hz sampling the
/// earliest instant at which that EXISTING, UNCHANGED gate can be satisfied is
/// therefore `AVG_WINDOW_SECS − MIN_SPAN_SECS = 10s` before the close. Resting
/// earlier is impossible, not merely unwise — so the bound is the derivation's,
/// and this constant just states it where `detect` can see it.
///
/// Venue side (demo-proven 2026-07-27): a POST at T0−34.9s returns 201 and the
/// order rests across the boundary; T0−399s returns 503. 10s is inside the
/// proven-good region with 25s of margin.
pub const PRE_T0_LEAD_SECS: i64 = crate::derive::AVG_WINDOW_SECS - crate::derive::MIN_SPAN_SECS;

/// Evaluate one candidate market against the newest settled windows.
/// `settled_desc` must be sorted newest-first with non-empty results.
pub fn detect(settled_desc: &[SettledWindow], cur: &Candidate, now: i64) -> Result<Entry, Skip> {
    if settled_desc.len() < 4 {
        return Err(Skip::InsufficientHistory);
    }
    let last4 = &settled_desc[..4];

    // Exactly consecutive 15-min windows (any gap → no signal; redirect rule 2).
    for w in last4.windows(2) {
        if w[0].close_unix - w[1].close_unix != WINDOW_SECS {
            return Err(Skip::NotConsecutive);
        }
    }

    // All four the same direction (redirect rule 3).
    let first = last4[0].result.as_str();
    if !last4.iter().all(|s| s.result == first) {
        return Err(Skip::NoStreak);
    }
    let (streak_dir, buy_yes) = match first {
        "yes" => ("up", false), // 4 ups → fade with NO
        "no" => ("down", true), // 4 downs → fade with YES
        _ => return Err(Skip::NoStreak),
    };

    // The settled chain must abut the current market: newest settled close ==
    // current open (redirect rule 4). Distinguish "previous window still
    // settling" (retryable, measures settlement lag) from a genuine mismatch.
    let newest_close = last4[0].close_unix;
    let abuts = match cur.open_unix {
        Some(o) => o == newest_close,
        None => cur.close_unix == newest_close + WINDOW_SECS,
    };
    if !abuts {
        let prev_settling = match cur.open_unix {
            Some(o) => newest_close == o - WINDOW_SECS,
            None => cur.close_unix == newest_close + 2 * WINDOW_SECS,
        };
        return Err(if prev_settling {
            Skip::PrevNotSettled
        } else {
            Skip::WindowMismatch
        });
    }

    // First 60 seconds only (redirect rule 4: ttc ≥ 14 min) — plus the pre-T0
    // lead, during which the market exists but has not opened (ttc > 900).
    //
    // The two out-of-band directions are DISTINCT skips (charter item 3): above
    // the band the clock is still walking toward the window (retryable); below
    // it the window is gone (terminal).
    let ttc = cur.close_unix - now;
    if ttc > WINDOW_SECS + PRE_T0_LEAD_SECS {
        return Err(Skip::TooEarly { ttc });
    }
    if ttc < MIN_TTC_SECS {
        return Err(Skip::NotEntryWindow { ttc });
    }

    // The reversal side's observed ask rides along for the record and the paper
    // fill model. It is NOT a gate (see the module header): the 40¢ rest and the
    // 46¢ IOC ceiling are the willingness-to-pay.
    let ask = if buy_yes { cur.yes_ask } else { cur.no_ask };

    Ok(Entry {
        buy_yes,
        ask,
        streak_dir,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settled(closes_desc: &[i64], result: &str) -> Vec<SettledWindow> {
        closes_desc
            .iter()
            .map(|&c| SettledWindow {
                close_unix: c,
                result: result.into(),
            })
            .collect()
    }

    fn cand(open: i64, yes_ask: f64, no_ask: f64) -> Candidate {
        Candidate {
            open_unix: Some(open),
            close_unix: open + WINDOW_SECS,
            yes_ask: Some(yes_ask),
            no_ask: Some(no_ask),
        }
    }

    // newest settled closes at t=0; current market opens at 0, closes at 900.
    const T: i64 = 100_000;

    #[test]
    fn four_ups_buys_no_within_window_and_gate() {
        let s = settled(&[T, T - 900, T - 1800, T - 2700], "yes");
        let c = cand(T, 62.0, 40.0);
        let e = detect(&s, &c, T + 30).unwrap();
        assert!(!e.buy_yes); // fade the up-streak with NO
        assert_eq!(e.streak_dir, "up");
        assert_eq!(e.ask, Some(40.0));
    }

    #[test]
    fn four_downs_buys_yes() {
        let s = settled(&[T, T - 900, T - 1800, T - 2700], "no");
        let c = cand(T, 43.9, 58.0);
        let e = detect(&s, &c, T + 59).unwrap();
        assert!(e.buy_yes);
        assert_eq!(e.streak_dir, "down");
    }

    #[test]
    fn mixed_results_no_streak() {
        let mut s = settled(&[T, T - 900, T - 1800, T - 2700], "yes");
        s[2].result = "no".into();
        assert_eq!(
            detect(&s, &cand(T, 50.0, 52.0), T + 30),
            Err(Skip::NoStreak)
        );
    }

    #[test]
    fn gap_in_windows_not_consecutive() {
        // 30-min gap between the 2nd and 3rd newest.
        let s = settled(&[T, T - 900, T - 2700, T - 3600], "yes");
        assert_eq!(
            detect(&s, &cand(T, 50.0, 40.0), T + 30),
            Err(Skip::NotConsecutive)
        );
    }

    #[test]
    fn price_never_gates_the_signal() {
        // SUPERSEDED 44¢ gate: an expensive reversal side is still a SIGNAL —
        // the 40¢ rest / 46¢ ceiling decide what (if anything) we pay. A 53¢
        // ask (the population median at open) must NOT be rejected here.
        let s = settled(&[T, T - 900, T - 1800, T - 2700], "yes");
        let e = detect(&s, &cand(T, 47.0, 53.0), T + 30).unwrap();
        assert_eq!(e.ask, Some(53.0));
        // Even a 95¢ reversal side signals; the execution layer simply won't fill.
        assert_eq!(detect(&s, &cand(T, 5.0, 95.0), T + 30).unwrap().ask, Some(95.0));
    }

    #[test]
    fn unpriced_reversal_side_still_signals() {
        // An empty book is a fine place to rest a 40¢ bid — no ask, no veto.
        let s = settled(&[T, T - 900, T - 1800, T - 2700], "yes");
        let c = Candidate {
            open_unix: Some(T),
            close_unix: T + WINDOW_SECS,
            yes_ask: Some(60.0),
            no_ask: None,
        };
        assert_eq!(detect(&s, &c, T + 30).unwrap().ask, None);
    }

    #[test]
    fn entry_window_closes_after_60s() {
        let s = settled(&[T, T - 900, T - 1800, T - 2700], "yes");
        let c = cand(T, 60.0, 40.0);
        // 61s after open → ttc = 839 < 840 → out of window.
        assert_eq!(
            detect(&s, &c, T + 61),
            Err(Skip::NotEntryWindow { ttc: 839 })
        );
    }

    #[test]
    fn pre_t0_lead_is_the_derivations_own_floor() {
        // Not a chosen number: the earliest instant the UNCHANGED derive gate
        // (MIN_SPAN_SECS of AVG_WINDOW_SECS) can be met at 1 Hz.
        assert_eq!(PRE_T0_LEAD_SECS, 10);
        assert_eq!(
            PRE_T0_LEAD_SECS,
            crate::derive::AVG_WINDOW_SECS - crate::derive::MIN_SPAN_SECS
        );
    }

    #[test]
    fn next_window_is_enterable_inside_the_pre_t0_lead() {
        // The NEXT market opens at T (=T0) and closes at T+900. Evaluated 10s
        // before its open, ttc = 910 — which the old `..=WINDOW_SECS` bound
        // rejected terminally.
        let s = settled(&[T, T - 900, T - 1800, T - 2700], "yes");
        let c = Candidate {
            open_unix: Some(T),
            close_unix: T + WINDOW_SECS,
            yes_ask: None,
            no_ask: None,
        };
        let e = detect(&s, &c, T - PRE_T0_LEAD_SECS).unwrap();
        assert!(!e.buy_yes);
        assert_eq!(e.ask, None); // unpriced pre-T0: the book does not exist yet
    }

    #[test]
    fn too_early_and_too_late_are_distinct_and_only_too_early_retries() {
        let s = settled(&[T, T - 900, T - 1800, T - 2700], "yes");
        let c = cand(T, 60.0, 40.0);
        // 11s before open → ttc 911 > 900+10 → TOO EARLY, retryable.
        let early = detect(&s, &c, T - PRE_T0_LEAD_SECS - 1).unwrap_err();
        assert_eq!(early, Skip::TooEarly { ttc: 911 });
        assert!(early.retryable());
        // 61s after open → ttc 839 → TOO LATE, terminal.
        let late = detect(&s, &c, T + 61).unwrap_err();
        assert_eq!(late, Skip::NotEntryWindow { ttc: 839 });
        assert!(!late.retryable());
        assert_ne!(early.as_str(), late.as_str());
    }

    #[test]
    fn prev_window_not_settled_is_retryable() {
        // Newest settled closes one full window BEFORE the current open: the
        // window in between hasn't settled yet.
        let s = settled(&[T - 900, T - 1800, T - 2700, T - 3600], "yes");
        let c = cand(T, 60.0, 40.0);
        let skip = detect(&s, &c, T + 30).unwrap_err();
        assert_eq!(skip, Skip::PrevNotSettled);
        assert!(skip.retryable());
    }

    #[test]
    fn unrelated_history_is_window_mismatch() {
        let s = settled(&[T - 5400, T - 6300, T - 7200, T - 8100], "yes");
        assert_eq!(
            detect(&s, &cand(T, 60.0, 40.0), T + 30),
            Err(Skip::WindowMismatch)
        );
    }

    #[test]
    fn too_few_settled() {
        let s = settled(&[T, T - 900, T - 1800], "yes");
        assert_eq!(
            detect(&s, &cand(T, 60.0, 40.0), T + 30),
            Err(Skip::InsufficientHistory)
        );
    }
}
