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

/// Series prefixes nestor's own strategies trade — the ADOPTION BOUNDARY.
/// Positions outside these series belong to operator side-operations and are
/// accounted via data/external_cash.jsonl, never adopted (verify-ops-map F1).
const NESTOR_SERIES: [&str; 7] = [
    "KXBTC15M", "KXETH15M", "KXGOLDD", "KXSILVERD", "KXCOPPERD", "KXAPRPOTUS", "KXCPIYOY",
];

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

/// FIX F8 (moneypath review; fired live 2026-07-27 12:45Z, 52min idle over the
/// bot's own $10 win): asymmetric breaker tolerance. EXTRA money bounded by the
/// maximum payout of our own unbooked open positions is a settlement credit
/// outrunning the lagging settle index (36s-settled-filter family) — a check
/// clearing, not a disagreement. MISSING money gets no grace: it halts at the
/// tight threshold regardless of what we're owed.
///
/// FIX F1 of the settled-guard review (R171 / incident #5): `unpaid_settled` is
/// the MIRROR of `pending_payout` and the only new grace on the missing-money
/// side. When we book a winner, `expected_cash` rises by the full $1.00 × count
/// payout the instant we settle (bankroll gains the win, the stake leaves
/// `total_at_risk`), but the exchange pays minutes-to-hours later — so real cash
/// is legitimately SHORT by exactly that payout until it lands. Before the
/// settled-set guard, the re-adoption bug accidentally masked most of this by
/// putting the stake back; with the guard, the gap is fully exposed and a
/// 10-contract winner would false-halt a $2 threshold on its own settlement.
///
/// It is evidence-bounded exactly like `resting_reserved`, and that matters more
/// here than anywhere else because this widens the side F8 deliberately kept
/// tight: the caller may only count a ticker whose position the EXCHANGE STILL
/// SHOWS in this same pass AND whose local `Settled` record says `won` — i.e.
/// money we can prove we are owed. A settled LOSER contributes nothing (its
/// payout is $0.00 and its delta is exactly zero). The grace self-extinguishes:
/// once the payout lands the position leaves the exchange's list and the
/// widening disappears on the next pass.
fn breaker_threshold(
    delta_signed: f64,
    resting_reserved: f64,
    pending_payout: f64,
    unpaid_settled: f64,
) -> f64 {
    if delta_signed > 0.0 {
        divergence_threshold(resting_reserved) + pending_payout.max(0.0)
    } else {
        divergence_threshold(resting_reserved) + unpaid_settled.max(0.0)
    }
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

/// Is this market OVER — trading finished and the outcome known or imminent?
///
/// Deliberately NOT [`is_settleable`], and deliberately an OR where that is an
/// AND. The two functions answer opposite-facing questions and so round in
/// opposite directions: `is_settleable` asks "may we BOOK MONEY on this?", where
/// the conservative answer is NO unless the outcome is unambiguous; this asks
/// "could this exchange position still be a live orphan worth adopting?", where
/// the conservative answer is NO on ANY evidence the market is finished. A
/// non-empty `result` alone is enough, and so is a terminal status with no result
/// yet (the transient determined-with-empty-result state books nothing but is
/// still not something to open a position on).
///
/// `closed` is NOT terminal: trading has stopped but the outcome is still
/// pending, and a position on a closed-undetermined market is a genuine orphan
/// that must remain adoptable — refusing it would leave real exposure invisible
/// to the caps.
fn is_over(status: &str, result: &str) -> bool {
    if !result.trim().is_empty() {
        return true;
    }
    matches!(
        status.trim().to_ascii_lowercase().as_str(),
        "determined" | "finalized" | "settled"
    )
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
                // Two outcomes share this None (review F7): nothing open under
                // that ticker, or the settled-set guard refused and dropped a
                // phantom. Deliberately NOT split into an enum return: the risk
                // layer already emits the authoritative record for the second
                // case — named settled row, dropped stake, HALT — so an enum
                // would buy a second, weaker copy of that message at the cost of
                // changing every caller's signature. This line just stops
                // asserting the first case as if it were the only one.
                logging::info(format!(
                    "{ticker}: nothing booked — no open position, or the settled-set guard \
                     refused it (see the risk-layer log) — skip"
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
    // Settlement payouts the exchange still owes us, in dollars, evidenced by
    // this very pass: a ticker we booked as a WIN whose position the exchange is
    // still showing. Feeds the divergence breaker's missing-money side (F1).
    let mut unpaid_settled = 0.0f64;
    for p in &exchange {
        // ADOPTION BOUNDARY (verify-ops-map F1, 2026-07-27): only adopt positions
        // on series NESTOR'S OWN STRATEGIES trade. The account is shared with
        // operator side-operations (LIP probe, manual event trades — external_cash
        // ledger territory); adopting those double-books their cash effects,
        // eats the daily budget/portfolio caps, and would have booked a 1,000-lot
        // penny order at WORST_CASE 99c = a phantom $990 stake. Six of the ops-map
        // attack's ten findings die on this filter.
        if !NESTOR_SERIES.iter().any(|s| p.ticker.starts_with(s)) {
            continue;
        }
        // Cheap, local, network-free questions first: an empty row or a ticker we
        // already track needs no work — and, critically, no market fetch.
        let (tracked, settled) = {
            let r = eng.risk.lock().unwrap_or_else(|e| e.into_inner());
            (
                p.count <= 0 || r.has_open(&p.ticker),
                r.settled_record(&p.ticker),
            )
        };
        if tracked {
            continue;
        }

        // SETTLED-SET REFUSAL (R171 / incident #5). The exchange keeps showing a
        // position for minutes-to-hours after we book its settlement; adopting
        // that re-opens what we just closed (+$2.17/pass, 8 passes, on
        // 2026-07-27). Refused here rather than inside `adopt_orphan` — which
        // still enforces it for every other caller — because THIS is where the
        // payout the exchange still owes us can be measured for the divergence
        // breaker (F1): the position in hand is the evidence.
        if let Some(rec) = settled {
            let owed = if rec.won { p.count as f64 } else { 0.0 };
            unpaid_settled += owed;
            logging::info(format!(
                "{}: adoption refused — already settled (won={} pnl=${:.2}); exchange still \
                 shows {}x, payout owed ${owed:.2} — payout lag, not an orphan",
                p.ticker, rec.won, rec.pnl, p.count
            ));
            continue;
        }

        // MARKET-TRUTH GUARD (review F4) — the PRIMARY, stateless test: a market
        // that is OVER can never be an orphan worth adopting, whatever local
        // state says. It needs no memory, so it holds even if `state.settled` is
        // lost, truncated, or hand-edited away; the settled set is the backstop
        // that holds when the network is down. Placement is the cheapest correct
        // one: the fetch runs ONLY for would-be adoptees, which are rare (every
        // tracked, empty, or settled row has already `continue`d above), so the
        // normal pass adds zero calls.
        //
        // A failed fetch REFUSES (fail-closed): unverified means unknown, an
        // un-adopted genuine orphan is retried next pass and meanwhile shows up
        // as unexplained cash the breaker will halt on — loud and safe — whereas
        // adopting a finished market is the incident this fix exists to end.
        match eng.kalshi.market(&p.ticker).await {
            Ok(m) => {
                let result = m.result.unwrap_or_default();
                let status = m.status.unwrap_or_default();
                if is_over(&status, &result) {
                    logging::info(format!(
                        "{}: adoption refused — market is over (status={status:?} \
                         result={result:?}); a finished market cannot be an orphan",
                        p.ticker
                    ));
                    continue;
                }
            }
            Err(e) => {
                logging::info(format!(
                    "{}: adoption deferred — market fetch failed ({e}); refusing to adopt an \
                     unverified market, will retry next pass",
                    p.ticker
                ));
                continue;
            }
        }

        // adopt_orphan is idempotent: it no-ops (returns false) for a ticker we
        // already track. So this only fires for genuine orphans, and the alert
        // below stays a real signal.
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
    // EXTERNAL-CASH LEDGER (2026-07-27, fired live: the LIP probe's fills moved
    // $11.07 of real cash in the SHARED account and the breaker correctly saw
    // "missing money" and halted — the money wasn't missing, it was in probe
    // positions nestor's ledger can't see. Until side operations live in their
    // own subaccount (R153), the operator records their cash effects in
    // data/external_cash.jsonl: {"delta_dollars": -11.07, "note": "..."} shifts
    // expected cash; {"pending_payout_dollars": 26.0, ...} widens ONLY the
    // positive side (expected future credits, e.g. probe settlements/rewards).
    // Missing-money protection stays tight around the shifted expectation.
    let (ext_cash, ext_pending) = read_external_cash();
    let (expected_cash, resting_reserved, house_cash, pending_payout) = {
        let r = eng.risk.lock().unwrap_or_else(|e| e.into_inner());
        (
            r.expected_cash(),
            r.resting_reserved(),
            r.house_cash(),
            r.pending_payout(),
        )
    };
    let expected_cash = expected_cash + ext_cash;
    let delta_signed = real_cash - expected_cash;
    let divergence = delta_signed.abs();
    let threshold = breaker_threshold(
        delta_signed,
        resting_reserved,
        pending_payout + ext_pending,
        unpaid_settled,
    );

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
             ${DIVERGENCE_THRESHOLD_USD:.2} + ${resting_reserved:.2} resting + \
             ${unpaid_settled:.2} settled-unpaid) — real cash ${real_cash:.2} vs expected \
             ${expected_cash:.2} (house bridge ${house_cash:.2}). HALTING (state/exchange \
             disagree).",
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

#[cfg(test)]
mod f8_tests {
    use super::*;

    #[test]
    fn credit_within_pending_payout_is_tolerated_but_missing_money_is_not() {
        // The live incident: +$10.26 delta with a 10-contract winner unbooked.
        let pending = 10.0;
        assert!(10.26 < breaker_threshold(10.26, 0.0, pending, 0.0)); // no halt
        // Same magnitude MISSING money: no grace from what we're owed.
        assert!(10.26 > breaker_threshold(-10.26, 0.0, pending, 0.0)); // halts
        // Extra money BEYOND anything we're owed still halts.
        assert!(13.0 > breaker_threshold(13.0, 0.0, pending, 0.0));
        // No open positions: the old tight symmetric behavior exactly.
        assert_eq!(
            breaker_threshold(5.0, 0.0, 0.0, 0.0),
            breaker_threshold(-5.0, 0.0, 0.0, 0.0)
        );
    }
}

#[cfg(test)]
mod settled_guard_tests {
    use super::*;

    /// Would the breaker HALT on a signed delta with this evidence?
    fn halts(delta: f64, resting: f64, pending: f64, unpaid: f64) -> bool {
        delta.abs() > breaker_threshold(delta, resting, pending, unpaid)
    }

    #[test]
    fn a_settled_winner_awaiting_payout_does_not_false_halt() {
        // REVIEW F1. We book a 10-contract winner; expected_cash immediately
        // rises by the whole $10.00 payout, the exchange pays minutes-to-hours
        // later, and this pass still sees the position on the exchange (which is
        // exactly why adoption was refused). Real cash is short by $10.00 for
        // reasons we can prove — that must not halt.
        let unpaid = 10.0;
        assert!(!halts(-10.0, 0.0, 0.0, unpaid), "false-halted on our own winnings");
        // Without that evidence the SAME missing $10 halts at the tight
        // threshold: the grace is bounded by what the exchange still shows, not
        // granted to missing money in general.
        assert!(halts(-10.0, 0.0, 0.0, 0.0));
        // Missing MORE than we are owed still halts — widened, not disabled.
        assert!(halts(-12.01, 0.0, 0.0, unpaid));
        // A settled LOSER owes us nothing, so it contributes nothing: its real
        // delta is exactly zero, and $10 going missing next to it still halts.
        assert!(halts(-10.0, 0.0, 0.0, 0.0));
        // The POSITIVE side is untouched by this term (F8 owns that side).
        assert_eq!(
            breaker_threshold(5.0, 0.0, 0.0, 10.0),
            breaker_threshold(5.0, 0.0, 0.0, 0.0)
        );
        // Garbage never shrinks the tolerance below the base.
        assert_eq!(
            breaker_threshold(-1.0, 0.0, 0.0, -50.0),
            DIVERGENCE_THRESHOLD_USD
        );
    }

    #[test]
    fn a_finished_market_is_never_an_orphan_worth_adopting() {
        // REVIEW F4, the stateless primary guard. Any evidence the market is
        // over disqualifies adoption...
        assert!(is_over("determined", "yes"));
        assert!(is_over("finalized", "no"));
        assert!(is_over("SETTLED", "")); // terminal status, result not in yet
        assert!(is_over("", "yes")); // result is authoritative without status
        assert!(is_over("active", "yes")); // stale status, outcome known
        // ...while a market that is merely not trading is still adoptable: a
        // position on it is a real orphan whose outcome has not happened.
        assert!(!is_over("closed", ""));
        assert!(!is_over("active", ""));
        assert!(!is_over("", ""));
        assert!(!is_over("paused", "  "));

        // The two questions round in OPPOSITE directions, on purpose. These are
        // exactly the states where "may we book money?" and "could this be a
        // live orphan?" disagree — both answers conservative for their own side.
        assert!(!is_settleable("determined", "") && is_over("determined", ""));
        assert!(!is_settleable("active", "yes") && is_over("active", "yes"));
    }
}

/// Sum the operator's external-cash ledger (`data/external_cash.jsonl`).
/// Returns (Σ delta_dollars, Σ pending_payout_dollars). Absent file = (0, 0).
/// Malformed lines are skipped loudly rather than trusted.
fn read_external_cash() -> (f64, f64) {
    let mut cash = 0.0;
    let mut pending = 0.0;
    if let Ok(text) = std::fs::read_to_string("data/external_cash.jsonl") {
        for line in text.lines() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            match serde_json::from_str::<serde_json::Value>(line) {
                Ok(v) => {
                    cash += v.get("delta_dollars").and_then(|x| x.as_f64()).unwrap_or(0.0);
                    pending += v
                        .get("pending_payout_dollars")
                        .and_then(|x| x.as_f64())
                        .unwrap_or(0.0);
                }
                Err(e) => eprintln!("[reconcile] external_cash.jsonl bad line skipped: {e}"),
            }
        }
    }
    (cash, pending)
}
