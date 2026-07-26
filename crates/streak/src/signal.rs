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
    /// Current market is past its first 60 seconds (ttc < 14 min). Terminal.
    NotEntryWindow { ttc: i64 },
}

impl Skip {
    /// Retryable skips may still convert to an entry on a later pass within the
    /// entry window; terminal ones cannot.
    pub fn retryable(&self) -> bool {
        matches!(self, Skip::PrevNotSettled)
    }

    pub fn as_str(&self) -> String {
        match self {
            Skip::InsufficientHistory => "insufficient_history".into(),
            Skip::NotConsecutive => "not_consecutive".into(),
            Skip::NoStreak => "no_streak".into(),
            Skip::PrevNotSettled => "prev_not_settled".into(),
            Skip::WindowMismatch => "window_mismatch".into(),
            Skip::NotEntryWindow { ttc } => format!("not_entry_window(ttc={ttc}s)"),
        }
    }
}

/// 15-minute window length in seconds.
pub const WINDOW_SECS: i64 = 900;
/// Entry only while time-to-close ≥ 14 min (= within 60s of open).
pub const MIN_TTC_SECS: i64 = 840;

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

    // First 60 seconds only (redirect rule 4: ttc ≥ 14 min).
    let ttc = cur.close_unix - now;
    if !(MIN_TTC_SECS..=WINDOW_SECS).contains(&ttc) {
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
