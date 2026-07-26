//! Settlement / reconcile loop (T004). Closes the loop on open positions:
//! for each one, fetch its Kalshi market, read the authoritative `result`,
//! decide win/loss, realize P&L through the risk layer, and append a
//! settlement record to the trade log. Run daily (morning-after for weather).
//!
//! Kalshi is the settlement source of truth (a single `GET markets/{ticker}`).
//! Not-yet-settled markets are skipped and retried on the next run.

use anyhow::{Context, Result};
use chrono_tz::America::New_York;
use serde_json::json;

use crate::strategy::{Engine, Mode};
use crate::{alert, kalshi, logging};
use crate::risk::Side;

/// Settlements from ALL strategies land here (strategy-tagged), so per-strategy
/// reports (e.g. streak week-1) can join their entry logs by ticker.
const LOG: &str = "settlements.jsonl";

/// Divergence breaker threshold (fix 1c): if the REAL Kalshi cash balance and the
/// risk layer's expected cash (`bankroll − open stakes`) differ by more than this
/// many dollars, something is wrong with our accounting — HALT. $2 absorbs
/// sub-cent fee rounding and in-flight settlement timing across a handful of open
/// positions without masking a genuine miscount.
const DIVERGENCE_THRESHOLD_USD: f64 = 2.0;

/// Tape of what `/portfolio/balance` actually did while an order rested. One
/// line per divergence check that saw a non-zero resting reservation — the
/// cheapest possible way to convert reality-F1's INFERRED into PROVEN: the
/// first live maker leg answers it for free.
const RESTING_COLLATERAL_LOG: &str = "data/resting_collateral.jsonl";

/// The divergence breaker's tolerance for this pass, in dollars (FIX 1 —
/// reality F1, constants F1, moneypath F3).
///
/// `DIVERGENCE_THRESHOLD_USD` alone is WRONG whenever a maker leg is resting,
/// and it is wrong in a way that HALTS THE WHOLE BOT on either answer to an
/// exchange fact we have never proven in prod: does `/portfolio/balance` debit
/// the collateral of a RESTING (unfilled) buy order?
///   - if it does NOT (demo, 2026-07-26): real cash is unmoved while
///     `expected_cash` has already dropped by the reservation → Δ = the full
///     resting notional ($4.00 for a streak maker leg) → halt.
///   - if it DOES (prod, unverified): Δ = 0 for streak, but house's two-sided
///     quotes reserve nothing at all → their notional shows up as Δ instead.
///
/// Widening by the CURRENTLY-RESTING reservations is robust to BOTH branches:
/// the true divergence attributable to unproven collateral treatment is bounded
/// by exactly that number, so `$2.00 + Σ resting` cannot false-halt, while any
/// miscount LARGER than the resting notional still trips.
///
/// Deliberately NOT "exclude reservations from expected_cash": that inverts the
/// halt if prod does debit on rest. Deliberately NOT the full `reserved_total`:
/// a reservation held past a KNOWN fill (cancel-404) covers cash that certainly
/// moved, and widening for it would mask a genuine miscount for minutes.
fn divergence_threshold(resting_reserved: f64) -> f64 {
    DIVERGENCE_THRESHOLD_USD + resting_reserved.max(0.0)
}

/// Which branch of the unproven resting-collateral question this observation is
/// consistent with. Only meaningful when `resting_reserved > 0`.
fn collateral_branch(real_cash: f64, expected_cash: f64, resting_reserved: f64) -> &'static str {
    if resting_reserved <= 0.0 {
        return "no_resting_orders";
    }
    let delta = real_cash - expected_cash;
    // Locking ⇒ real tracks expected (Δ≈0). Not locking ⇒ real is HIGHER than
    // expected by the resting notional. Split at the midpoint.
    if delta > resting_reserved / 2.0 {
        "does_not_lock_collateral"
    } else if delta.abs() <= resting_reserved / 2.0 {
        "locks_collateral"
    } else {
        "inconsistent_with_both"
    }
}

/// Decide the settlement action for a position given the market's raw `result`.
/// `None` = not settled yet (or void/unknown) → skip and retry next run.
/// `Some(won)` = settled; `won` is whether our `side` matches the outcome.
/// Kalshi `result` is "yes"/"no": a YES holder wins iff result == "yes".
fn settlement_won(side: Side, result: &str) -> Option<bool> {
    match result.trim().to_ascii_lowercase().as_str() {
        "yes" => Some(side == Side::Yes),
        "no" => Some(side == Side::No),
        // "" (still open) or "void"/anything unexpected: don't settle.
        _ => None,
    }
}

/// Whether a market is settleable NOW (item 6). Kalshi status progresses
/// `active` → `closed` → `determined` → `finalized`; both `determined` and
/// `finalized` mean the outcome is KNOWN, so we free exposure + feed the
/// kill-switch as soon as `determined` lands (not only at `finalized`). Guards:
///   - an EMPTY `result` is NEVER settleable, even at status=="determined" (no
///     outcome to book — determined-with-empty-result is a transient state);
///   - a missing/empty `status` is tolerated as long as `result` is populated
///     (`result` is the authoritative outcome; some payloads omit status).
fn is_settleable(status: &str, result: &str) -> bool {
    if result.trim().is_empty() {
        return false;
    }
    match status.trim().to_ascii_lowercase().as_str() {
        "" | "determined" | "finalized" | "settled" => true,
        // active/closed/paused/etc. with a result present is anomalous — don't
        // settle on it; wait for the status to reach determined/finalized.
        _ => false,
    }
}

/// Sample the exchange clock once per live reconcile pass (item 7). A Mac sleep
/// desyncs the local clock; public data still flows but EVERY signed call then
/// 401s. Reading the server's HTTP `Date` header makes that failure PROACTIVE
/// (a loud alert) instead of reactive (the 401 breaker after 5 failures).
async fn check_clock_skew(eng: &Engine) {
    match eng.kalshi.server_time().await {
        Ok(server) => {
            let local = chrono::Utc::now().timestamp();
            let skew = (local - server).abs();
            if skew > 30 {
                let msg = format!(
                    "CLOCK SKEW {skew}s (local {local} vs server {server}) — signed calls WILL \
                     401 (likely Mac sleep); resync NTP before the bot can trade"
                );
                logging::info(format!("ALERT: {msg}"));
                eprintln!("[reconcile] ALERT: {msg}");
                alert::notify(&eng.http, &msg).await;
            } else {
                logging::info(format!("clock-skew check OK ({skew}s)"));
            }
        }
        Err(e) => logging::info(format!("clock-skew check failed ({e}) — skip")),
    }
}

/// Settle every open position whose Kalshi market has a final `result`.
pub async fn run(eng: &Engine) -> Result<()> {
    // Roll the risk layer to today (ET) first. This resets the daily counters
    // for the new trading day, so prior-day positions we settle below are NOT
    // attributed to today's day_loss (see RiskManager::settle). A no-op if the
    // day already matches (e.g. reconcile and the strategy ran the same day).
    let today = chrono::Utc::now()
        .with_timezone(&New_York)
        .date_naive()
        .format("%Y-%m-%d")
        .to_string();
    eng.begin_day(&today);

    // Snapshot open tickers+sides+strategy so we never hold the risk lock across
    // the network fetch (mirrors Engine::execute's discipline).
    let open: Vec<(String, Side, String)> = {
        let r = eng.risk.lock().unwrap_or_else(|e| e.into_inner());
        r.open_positions()
            .iter()
            .map(|p| (p.ticker.clone(), p.side, p.strategy.clone()))
            .collect()
    };

    logging::info(format!(
        "reconcile start — day={today} {} open position(s)",
        open.len()
    ));
    let mut settled = 0usize;
    let mut pending = 0usize;

    for (ticker, side, strategy) in open {
        let market = match eng.kalshi.market(&ticker).await {
            Ok(m) => m,
            Err(e) => {
                logging::info(format!("{ticker}: market fetch failed ({e}) — skip"));
                continue;
            }
        };
        let result = market.result.unwrap_or_default();
        let status = market.status.unwrap_or_default();
        // Gate on status too (item 6): determined/finalized with a non-empty
        // result is settleable now; determined with an EMPTY result is not.
        if !is_settleable(&status, &result) {
            pending += 1;
            logging::info(format!(
                "{ticker}: not settled (status={status:?} result={result:?}) — skip"
            ));
            continue;
        }
        let won = match settlement_won(side, &result) {
            Some(w) => w,
            None => {
                pending += 1;
                logging::info(format!("{ticker}: not settled (result={result:?}) — skip"));
                continue;
            }
        };

        // Realize P&L (money math + kill-switch live in the risk layer).
        let outcome = eng
            .risk
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .settle(&ticker, won);
        match outcome {
            Some(o) => {
                settled += 1;
                logging::record(
                    LOG,
                    json!({
                        "event": "settlement",
                        "strategy": strategy,
                        "ticker": o.ticker,
                        "won": o.won,
                        "pnl": o.pnl,
                        "result": result,
                    }),
                );
                logging::info(format!(
                    "{}: settled won={} pnl=${:.2}",
                    o.ticker, o.won, o.pnl
                ));
            }
            None => {
                logging::info(format!(
                    "{ticker}: no open position (already settled?) — skip"
                ));
            }
        }
    }

    // EXCHANGE-TRUTH reconciliation (live only): adopt orphan positions and run
    // the bankroll-vs-real-balance divergence breaker. Paper has no authenticated
    // exchange view. Runs AFTER settlement so a signed-call failure here can't
    // block realizing settled P&L. May return Err (signed-call failure) so the
    // caller's loop can classify/backoff/count 401s.
    if eng.mode == Mode::Live {
        // Proactive clock-skew alert (item 7) — once per pass, BEFORE the signed
        // exchange-truth calls that clock skew would 401. Best-effort (never
        // blocks settlement): a failed/absent Date header just logs.
        check_clock_skew(eng).await;
        reconcile_exchange_truth(eng).await?;
    }

    let st = eng.risk.lock().unwrap_or_else(|e| e.into_inner()).status();
    logging::info(format!(
        "reconcile done — settled={settled} pending={pending} bankroll=${:.2} drawdown={:.1}% halted={}",
        st.bankroll,
        st.drawdown * 100.0,
        st.halted
    ));
    Ok(())
}

/// Live exchange-truth pass: (1b) adopt any exchange position absent from local
/// state as an ORPHAN into risk state; (1c) compare real cash against expected
/// cash and HALT on divergence. Both use SIGNED calls — an error propagates so
/// the reconcile loop can back off / count consecutive 401s.
async fn reconcile_exchange_truth(eng: &Engine) -> Result<()> {
    // --- (1b) ORPHAN ADOPTION ------------------------------------------------
    let raw = eng
        .kalshi
        .positions()
        .await
        .context("positions read (orphan check)")?;
    let exchange = kalshi::parse_positions(&raw);
    for p in &exchange {
        // adopt_orphan is idempotent: it no-ops (returns false) for a ticker we
        // already track, so this only fires for genuine orphans.
        let cluster = format!("orphan-{}", p.ticker);
        let adopted = {
            let mut r = eng.risk.lock().unwrap_or_else(|e| e.into_inner());
            r.adopt_orphan(&p.ticker, p.side, p.count, p.entry_cents, &cluster)
        };
        if adopted {
            let msg = format!(
                "ORPHAN ADOPTED {} {} {}x @ {} — exchange position missing from local state; \
                 adopted into risk (entry basis {})",
                p.ticker,
                p.side.as_str(),
                p.count,
                p.entry_cents
                    .map(|c| format!("{c}c"))
                    .unwrap_or_else(|| "worst-case 99c".into()),
                p.entry_cents
                    .map(|c| format!("{c}c"))
                    .unwrap_or_else(|| "unknown".into()),
            );
            logging::info(format!("ALERT: {msg}"));
            eprintln!("[reconcile] ALERT: {msg}");
            alert::notify(&eng.http, &msg).await;
        }
    }

    // --- (1c) DIVERGENCE BREAKER --------------------------------------------
    let bal_cents = eng
        .kalshi
        .balance_cents()
        .await
        .context("balance read (divergence check)")?;
    let real_cash = bal_cents as f64 / 100.0;
    let (expected_cash, resting_reserved, house_cash) = {
        let r = eng.risk.lock().unwrap_or_else(|e| e.into_inner());
        (r.expected_cash(), r.resting_reserved(), r.house_cash())
    };
    let divergence = (real_cash - expected_cash).abs();
    let threshold = divergence_threshold(resting_reserved);

    // FREE EVIDENCE (reality F1): the first live pass that lands while an order
    // rests answers the resting-collateral question outright. Record it.
    if resting_reserved > 0.0 {
        let branch = collateral_branch(real_cash, expected_cash, resting_reserved);
        logging::record_path(
            RESTING_COLLATERAL_LOG,
            json!({
                "event": "resting_collateral_observation",
                "real_cash": real_cash,
                "expected_cash": expected_cash,
                "resting_reserved": resting_reserved,
                "house_cash": house_cash,
                "delta": real_cash - expected_cash,
                "threshold": threshold,
                "branch": branch,
            }),
        );
        logging::info(format!(
            "resting-collateral observation — resting ${resting_reserved:.2}, real \
             ${real_cash:.2} vs expected ${expected_cash:.2} (Δ${:.2}) → {branch}",
            real_cash - expected_cash
        ));
    }

    if divergence > threshold {
        let msg = format!(
            "BANKROLL DIVERGENCE ${divergence:.2} > ${threshold:.2} (base \
             ${DIVERGENCE_THRESHOLD_USD:.2} + ${resting_reserved:.2} resting) — real cash \
             ${real_cash:.2} vs expected ${expected_cash:.2} (house bridge ${house_cash:.2}). \
             HALTING (state/exchange disagree).",
        );
        logging::info(format!("ALERT: {msg}"));
        eprintln!("[reconcile] ALERT: {msg}");
        eng.risk.lock().unwrap_or_else(|e| e.into_inner()).halt();
        alert::notify(&eng.http, &msg).await;
    } else {
        logging::info(format!(
            "divergence check OK — real ${real_cash:.2} vs expected ${expected_cash:.2} \
             (Δ${divergence:.2} ≤ ${threshold:.2})"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settled_yes_market() {
        // Our YES wins on "yes", loses on "no".
        assert_eq!(settlement_won(Side::Yes, "yes"), Some(true));
        assert_eq!(settlement_won(Side::Yes, "no"), Some(false));
    }

    #[test]
    fn settled_no_side() {
        // Our NO wins on "no", loses on "yes".
        assert_eq!(settlement_won(Side::No, "no"), Some(true));
        assert_eq!(settlement_won(Side::No, "yes"), Some(false));
    }

    #[test]
    fn settleable_on_determined_and_finalized_with_result() {
        // Both terminal-ish statuses settle as soon as a result is present.
        assert!(is_settleable("determined", "yes"));
        assert!(is_settleable("finalized", "no"));
        assert!(is_settleable("FINALIZED", "yes")); // case-insensitive
        assert!(is_settleable("settled", "no"));
        // Missing/empty status tolerated when result is authoritative.
        assert!(is_settleable("", "yes"));
    }

    #[test]
    fn not_settleable_on_determined_with_empty_result() {
        // The critical guard: determined but NO result yet = nothing to book.
        assert!(!is_settleable("determined", ""));
        assert!(!is_settleable("finalized", "   "));
        assert!(!is_settleable("", ""));
        // A result present but status still active/closed is anomalous — wait.
        assert!(!is_settleable("active", "yes"));
        assert!(!is_settleable("closed", "no"));
    }

    /// Would the breaker HALT on this observation?
    fn halts(real: f64, expected: f64, resting: f64) -> bool {
        (real - expected).abs() > divergence_threshold(resting)
    }

    #[test]
    fn divergence_tolerance_absorbs_a_resting_maker_leg_on_both_branches() {
        // FIX 1. Streak maker leg: 10 @ 40c = $4.00 reserved on a $106.03
        // bankroll, plus the standing +$0.25 offset present in all 2730 live
        // checks to date (reality F13). expected_cash = 106.03 − 4.00 = 102.03.
        let expected = 102.03;
        let resting = 4.00;
        // Branch A — balance does NOT debit resting collateral (demo-proven):
        // real cash is still 106.28 (106.03 + 0.25). Δ = $4.25.
        assert!(
            !halts(106.28, expected, resting),
            "must not halt on the no-lock branch"
        );
        // Branch B — balance DOES debit it (prod unverified): real = 102.28.
        assert!(
            !halts(102.28, expected, resting),
            "must not halt on the locking branch"
        );
        // Without the widening, branch A is a $4.25 > $2.00 HALT — the confirmed
        // defect this fix exists to remove.
        assert!((106.28f64 - expected).abs() > DIVERGENCE_THRESHOLD_USD);
    }

    #[test]
    fn divergence_still_halts_on_a_real_miscount() {
        // The breaker is WIDENED, not disabled: anything beyond the resting
        // notional plus the base tolerance still trips, on either branch.
        // Δ = 4.25 + 2.01 = 6.26 > 6.00; and 102.28 − 6.30 → Δ = 6.05 > 6.00.
        assert!(halts(106.28 + 2.01, 102.03, 4.00));
        assert!(halts(102.28 - 6.30, 102.03, 4.00));
        // And with nothing resting the threshold is exactly the old one.
        assert!((divergence_threshold(0.0) - DIVERGENCE_THRESHOLD_USD).abs() < 1e-9);
        assert!(halts(106.28, 102.03, 0.0));
        assert!(!halts(106.28, 106.03, 0.0)); // the standing Δ$0.25 stays OK
        // A garbage negative never shrinks the tolerance below the base.
        assert!((divergence_threshold(-5.0) - DIVERGENCE_THRESHOLD_USD).abs() < 1e-9);
    }

    #[test]
    fn collateral_branch_classifies_the_free_evidence() {
        // The observation that converts reality-F1 INFERRED → PROVEN.
        // $4 resting; real unmoved vs expected already debited → no lock.
        assert_eq!(
            collateral_branch(106.28, 102.03, 4.0),
            "does_not_lock_collateral"
        );
        // Real moved with expected → the exchange locked the collateral.
        assert_eq!(collateral_branch(102.28, 102.03, 4.0), "locks_collateral");
        // Cash gone that neither branch explains — worth seeing in the tape.
        assert_eq!(
            collateral_branch(95.00, 102.03, 4.0),
            "inconsistent_with_both"
        );
        assert_eq!(collateral_branch(106.28, 106.03, 0.0), "no_resting_orders");
    }

    #[test]
    fn not_settled_is_skipped() {
        // Empty result (still open) → skip. Case/whitespace tolerant.
        assert_eq!(settlement_won(Side::Yes, ""), None);
        assert_eq!(settlement_won(Side::No, "  "), None);
        assert_eq!(settlement_won(Side::Yes, "YES"), Some(true));
        // Unexpected/void outcome → skip rather than book a phantom loss.
        assert_eq!(settlement_won(Side::Yes, "void"), None);
    }
}
