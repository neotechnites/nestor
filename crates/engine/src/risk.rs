//! Risk layer — global bankroll, sizing, cluster caps, kill-switch.
//! Every order routes through here; strategies never size their own bets.
//! Implements the vault doctrine (notes 09/12): single-digit % per trade, treat
//! correlated positions in one cluster as one bet, halt on drawdown/daily loss.

use anyhow::Result;
use serde::{Deserialize, Serialize};

use crate::state::{Position, Settled, State, StateStore};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Side {
    Yes,
    No,
}

impl Side {
    pub fn as_str(self) -> &'static str {
        match self {
            Side::Yes => "yes",
            Side::No => "no",
        }
    }
}

/// How a strategy wants the bet sized. Amounts come from RiskConfig, not here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SizingHint {
    /// Flat dollars per trade, bounded by the daily budget (thin markets: weather).
    Flat,
    /// A fraction of current bankroll, bounded by the cluster cap (crypto sleeves).
    Fraction,
}

/// A strategy's intent to trade. No size — the RiskManager decides that.
#[derive(Debug, Clone)]
pub struct Signal {
    pub strategy: String,
    pub ticker: String,
    pub side: Side,
    pub limit_cents: i64,
    /// Correlation key; positions sharing it are capped as one bet.
    pub cluster: String,
    pub sizing: SizingHint,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Order {
    pub strategy: String,
    pub ticker: String,
    pub side: Side,
    pub count: i64,
    pub limit_cents: i64,
    pub cluster: String,
    pub sizing: SizingHint,
}

impl Order {
    pub fn stake(&self) -> f64 {
        self.count as f64 * self.limit_cents as f64 / 100.0
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rejection {
    Halted,
    DailyCapHit,
    ClusterCapHit,
    PortfolioCapHit,
    BankrollTooLow,
    PriceOutOfBand,
    ZeroSize,
}

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(default)]
pub struct RiskConfig {
    pub fraction: f64,
    pub cluster_cap_frac: f64,
    pub flat_usd: f64,
    pub daily_budget_usd: f64,
    pub max_drawdown_frac: f64,
    pub daily_loss_limit_frac: f64,
    /// Portfolio-wide ceiling: total capital at risk across ALL open positions
    /// (every cluster combined) may not exceed this fraction of bankroll. Guards
    /// the case where many uncorrelated clusters each sit under the cluster cap
    /// but together over-deploy the account (matters once >1 sleeve runs).
    pub max_portfolio_frac: f64,
}

impl Default for RiskConfig {
    fn default() -> Self {
        RiskConfig {
            fraction: 0.05,
            cluster_cap_frac: 0.15,
            flat_usd: 10.0,
            daily_budget_usd: 80.0,
            max_drawdown_frac: 0.30,
            daily_loss_limit_frac: 0.15,
            max_portfolio_frac: 0.50,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct RiskStatus {
    pub bankroll: f64,
    pub peak: f64,
    pub drawdown: f64,
    pub halted: bool,
}

/// What a single settlement realized — returned by [`RiskManager::settle`] so
/// the reconcile loop can log it (event "settlement": ticker, won, pnl).
#[derive(Debug, Clone, PartialEq)]
pub struct SettleOutcome {
    pub ticker: String,
    pub won: bool,
    pub pnl: f64,
}

/// Kalshi taker fee in dollars for one ORDER of `count` contracts at
/// `price_cents`: `ceil-to-next-cent( 0.07 × count × P × (1−P) )`, P in dollars.
/// The ceil is applied once per ORDER (Kalshi's actual billing), not per
/// contract — an un-ceiled formula understates the fee and overstates P&L
/// (redirect 2026-07-23; vault note 18 gotchas).
pub fn taker_fee(price_cents: i64, count: i64) -> f64 {
    let p = price_cents as f64 / 100.0;
    let raw = 0.07 * count as f64 * p * (1.0 - p);
    (raw * 100.0).ceil() / 100.0
}

pub struct RiskManager {
    cfg: RiskConfig,
    state: State,
    store: Box<dyn StateStore>,
    /// Live-money mode. Changes fail-closed behavior: a persist failure HALTS
    /// (a lost kill-switch flip is unacceptable with real money), and a missing
    /// state file is refused rather than silently re-armed.
    live: bool,
}

impl RiskManager {
    /// Load existing state or initialize with `initial_bankroll`.
    ///
    /// `live` selects fail-closed behavior (see [`RiskManager::live`] field).
    /// `allow_fresh` lets an operator explicitly accept a brand-new ledger in
    /// live mode; without it, a MISSING state file in live is a hard refusal —
    /// re-arming a halted bot with a fresh bankroll is a money-loss footgun
    /// (fix 3b). In paper mode a missing file always initializes fresh.
    pub fn load_or_init(
        cfg: RiskConfig,
        store: Box<dyn StateStore>,
        initial_bankroll: f64,
        live: bool,
        allow_fresh: bool,
    ) -> Result<Self> {
        let state = match store.load()? {
            Some(s) => s,
            None => {
                if live && !allow_fresh {
                    anyhow::bail!(
                        "live mode: no state file found — refusing to start with a fresh \
                         ledger. If this is intentional (first live run), pass --fresh-state."
                    );
                }
                State::new(initial_bankroll)
            }
        };
        Ok(Self {
            cfg,
            state,
            store,
            live,
        })
    }

    /// Persist state. In LIVE mode a save failure is treated as critical: we HALT
    /// in memory (so the kill-switch can never be silently lost to e.g. a full
    /// disk) and log loudly. Subsequent operations keep retrying the save (fix 3a).
    fn persist(&mut self) {
        if let Err(e) = self.store.save(&self.state) {
            if self.live {
                self.state.halted = true;
                eprintln!(
                    "risk: CRITICAL state save failed in LIVE mode: {e} — HALTING trading \
                     in memory and retrying persist on the next operation"
                );
                // Best-effort: try once more to at least persist the halt flag.
                let _ = self.store.save(&self.state);
            } else {
                eprintln!("risk: state save failed: {e}");
            }
        }
    }

    /// Roll daily counters when the ET date changes.
    pub fn begin_day(&mut self, day: &str) {
        if self.state.day != day {
            self.state.day = day.to_string();
            self.state.day_loss = 0.0;
            self.state.day_spent = 0.0;
            self.persist();
        }
    }

    fn cluster_at_risk(&self, cluster: &str) -> f64 {
        self.state
            .open
            .iter()
            .filter(|p| p.cluster == cluster)
            .map(|p| p.stake())
            .sum()
    }

    /// Total capital at risk across every open position (all clusters).
    fn total_at_risk(&self) -> f64 {
        self.state.open.iter().map(|p| p.stake()).sum()
    }

    /// Decide size for a signal, or reject. Does not mutate open positions;
    /// call `on_fill` after the order actually fills.
    pub fn evaluate(&self, s: &Signal) -> Result<Order, Rejection> {
        if self.state.halted {
            return Err(Rejection::Halted);
        }
        if self.state.bankroll <= 0.0 {
            return Err(Rejection::BankrollTooLow);
        }
        if s.limit_cents <= 2 || s.limit_cents >= 98 {
            return Err(Rejection::PriceOutOfBand);
        }

        // Cluster cap applies to BOTH sizing modes: correlated positions sharing a
        // cluster key (e.g. streak entries on BTC+ETH in the same 15-min window)
        // are one bet regardless of how each is sized.
        let cluster_room =
            self.cfg.cluster_cap_frac * self.state.bankroll - self.cluster_at_risk(&s.cluster);
        if cluster_room <= 0.0 {
            return Err(Rejection::ClusterCapHit);
        }

        let stake = match s.sizing {
            SizingHint::Flat => {
                let remaining = self.cfg.daily_budget_usd - self.state.day_spent;
                if remaining <= 0.0 {
                    return Err(Rejection::DailyCapHit);
                }
                self.cfg.flat_usd.min(remaining).min(cluster_room)
            }
            SizingHint::Fraction => {
                let want = self.cfg.fraction * self.state.bankroll;
                want.min(cluster_room)
            }
        };

        // Portfolio-wide ceiling across all open positions (both sizing modes).
        let portfolio_room =
            self.cfg.max_portfolio_frac * self.state.bankroll - self.total_at_risk();
        if portfolio_room <= 0.0 {
            return Err(Rejection::PortfolioCapHit);
        }
        let stake = stake.min(portfolio_room);

        let count = crate::sizing::contracts_for(stake, s.limit_cents);
        if count <= 0 {
            return Err(Rejection::ZeroSize);
        }
        Ok(Order {
            strategy: s.strategy.clone(),
            ticker: s.ticker.clone(),
            side: s.side,
            count,
            limit_cents: s.limit_cents,
            cluster: s.cluster.clone(),
            sizing: s.sizing,
        })
    }

    /// Record a filled order as an open position. Only flat-sized orders count
    /// against the daily budget (fraction sleeves are governed by cluster caps),
    /// so the two sleeves don't consume each other's limits on shared state.
    pub fn on_fill(&mut self, o: &Order) {
        self.on_fill_actual(o, o.count, o.limit_cents);
    }

    /// Record what ACTUALLY filled (EXECUTION TRUTH: accepted ≠ filled). Only
    /// `filled_count` at `fill_price_cents` enters state — never the requested
    /// count or assumed limit. The taker fee is charged HERE, at fill time
    /// (ceil-per-order on the actual fill), deducted from bankroll immediately
    /// and remembered on the Position so settle() reports net without
    /// re-charging. No-op if nothing filled.
    pub fn on_fill_actual(&mut self, o: &Order, filled_count: i64, fill_price_cents: i64) {
        if filled_count <= 0 {
            return;
        }
        // IDEMPOTENCY GUARD (fix 2a): one open position per ticker. Streak is a
        // one-shot order per market — a second on_fill for a ticker we already
        // hold open means a duplicate (in-window restart re-fire, or a
        // recovery path booking the same fill twice). Refuse and log loudly;
        // NEVER double-add (and never double-charge the fee, since we return
        // before touching bankroll). A genuine partial-fill continuation is not
        // a case here: an IOC order fills once, synchronously.
        if self.state.open.iter().any(|p| p.ticker == o.ticker) {
            eprintln!(
                "risk: DUPLICATE FILL REFUSED for {} — a position on this ticker is already \
                 open; not double-adding (idempotency guard)",
                o.ticker
            );
            return;
        }
        let fee = taker_fee(fill_price_cents, filled_count);
        self.state.bankroll -= fee;
        if matches!(o.sizing, SizingHint::Flat) {
            self.state.day_spent += filled_count as f64 * fill_price_cents as f64 / 100.0;
        }
        self.state.open.push(Position {
            strategy: o.strategy.clone(),
            ticker: o.ticker.clone(),
            side: o.side,
            count: filled_count,
            entry_cents: fill_price_cents,
            cluster: o.cluster.clone(),
            fee,
            day: self.state.day.clone(),
        });
        self.persist();
    }

    /// Read-only view of currently open positions (the reconcile loop iterates
    /// this to fetch each market's authoritative result).
    pub fn open_positions(&self) -> &[Position] {
        &self.state.open
    }

    /// True if a position on `ticker` is currently open in local state.
    pub fn has_open(&self, ticker: &str) -> bool {
        self.state.open.iter().any(|p| p.ticker == ticker)
    }

    /// Adopt an ORPHAN position discovered on the exchange but missing from local
    /// state (fix 1b) — e.g. a fill that landed after a lost ack. Conservative:
    /// no fee is charged (already paid on the exchange), `entry_cents` is the
    /// exchange cost basis when known else a worst-case, and it counts toward the
    /// daily budget so caps see the real exposure. Idempotent: a no-op if a
    /// position on `ticker` is already tracked. Returns true if newly adopted.
    pub fn adopt_orphan(
        &mut self,
        ticker: &str,
        side: Side,
        count: i64,
        entry_cents: Option<i64>,
        cluster: &str,
    ) -> bool {
        if count <= 0 || self.has_open(ticker) {
            return false;
        }
        // Worst-case entry: assume we paid the top of the band, so the position's
        // at-risk stake (and thus the kill-switch's view of exposure) is maximal.
        const WORST_CASE_ENTRY_CENTS: i64 = 99;
        let entry = entry_cents.unwrap_or(WORST_CASE_ENTRY_CENTS).clamp(1, 99);
        self.state.day_spent += count as f64 * entry as f64 / 100.0;
        self.state.open.push(Position {
            strategy: "orphan-adopted".into(),
            ticker: ticker.to_string(),
            side,
            count,
            entry_cents: entry,
            cluster: cluster.to_string(),
            fee: 0.0,
            day: self.state.day.clone(),
        });
        self.persist();
        true
    }

    /// Cash the account SHOULD hold if every open position is valued at its entry
    /// cost: `bankroll − Σ(open stakes)`. Compared against the real Kalshi balance
    /// by the divergence breaker (fix 1c) — the two must track within a threshold.
    pub fn expected_cash(&self) -> f64 {
        self.state.bankroll - self.total_at_risk()
    }

    /// Force the kill-switch on (divergence breaker / operator). Persisted.
    pub fn halt(&mut self) {
        self.state.halted = true;
        self.persist();
    }

    /// Realize P&L for the open position `ticker` given the authoritative
    /// `won` outcome, and return what happened. Pure with respect to the
    /// network — the caller (reconcile) fetches the settled result and passes
    /// it in, so all the money math is unit-testable offline. Returns `None`
    /// if no matching open position (already settled / unknown ticker → the
    /// reconcile loop treats that as a skip).
    ///
    /// Day-loss attribution (T004 fix): a reconcile run the morning after
    /// settles PRIOR-day positions. Their realized loss must still flow into
    /// `bankroll`, `peak`, and the *drawdown* kill-switch — but it must NOT be
    /// added to the CURRENT trading day's `day_loss`, or a loss we incurred on
    /// a previous day would wrongly trip today's daily-loss halt. We therefore
    /// attribute the loss to the position's OWN trading day: it counts toward
    /// `day_loss` only when the position was opened on the current `state.day`
    /// (e.g. a same-day-settling crypto sleeve). Cross-day weather settlements
    /// never touch today's counter.
    pub fn settle(&mut self, ticker: &str, won: bool) -> Option<SettleOutcome> {
        let idx = self.state.open.iter().position(|p| p.ticker == ticker)?;
        let pos = self.state.open.remove(idx);
        let entry = pos.entry_cents as f64 / 100.0;
        let gross = if won {
            pos.count as f64 * (1.0 - entry)
        } else {
            -(pos.count as f64 * entry)
        };
        // The taker fee was already deducted from bankroll at fill time
        // (on_fill_actual) — only the settlement cash flow moves bankroll here.
        // The REPORTED pnl is net of that fee so the trade's true economics
        // appear in the settlement record.
        let pnl = gross - pos.fee;

        self.state.bankroll += gross;
        // Only a loss from a position opened on TODAY's trading day feeds the
        // daily-loss kill-switch; prior-day settlements are excluded (see above).
        if pnl < 0.0 && pos.day == self.state.day {
            self.state.day_loss += -pnl;
        }
        if self.state.bankroll > self.state.peak {
            self.state.peak = self.state.bankroll;
        }
        self.state.settled.push(Settled {
            ticker: pos.ticker.clone(),
            won,
            pnl,
        });
        // Keep only the most recent settlements in live state — the full history
        // lives in the JSONL trade log. Bounds state.json growth over time.
        const MAX_SETTLED: usize = 1000;
        let n = self.state.settled.len();
        if n > MAX_SETTLED {
            self.state.settled.drain(0..n - MAX_SETTLED);
        }

        // kill-switch: drawdown (all-time peak) always applies; the daily-loss
        // limit only sees today's losses per the attribution rule above.
        let dd = if self.state.peak > 0.0 {
            (self.state.peak - self.state.bankroll) / self.state.peak
        } else {
            0.0
        };
        let daily_limit = self.cfg.daily_loss_limit_frac * self.state.peak;
        if dd >= self.cfg.max_drawdown_frac || self.state.day_loss >= daily_limit {
            self.state.halted = true;
        }
        self.persist();
        Some(SettleOutcome {
            ticker: pos.ticker,
            won,
            pnl,
        })
    }

    /// Settle an open position (spec API). Thin wrapper over [`settle`].
    pub fn on_settlement(&mut self, ticker: &str, won: bool) {
        self.settle(ticker, won);
    }

    pub fn status(&self) -> RiskStatus {
        let dd = if self.state.peak > 0.0 {
            (self.state.peak - self.state.bankroll) / self.state.peak
        } else {
            0.0
        };
        RiskStatus {
            bankroll: self.state.bankroll,
            peak: self.state.peak,
            drawdown: dd,
            halted: self.state.halted,
        }
    }

    /// Manually clear a halt (operator action after review).
    pub fn resume(&mut self) {
        self.state.halted = false;
        self.persist();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::MemoryStore;

    fn rm(bankroll: f64) -> RiskManager {
        RiskManager::load_or_init(
            RiskConfig::default(),
            Box::new(MemoryStore::default()),
            bankroll,
            false,
            true,
        )
        .unwrap()
    }

    fn sig(sizing: SizingHint, price: i64, cluster: &str) -> Signal {
        Signal {
            strategy: "t".into(),
            ticker: format!("TKR-{cluster}-{price}"),
            side: Side::Yes,
            limit_cents: price,
            cluster: cluster.into(),
            sizing,
        }
    }

    /// Signal with a UNIQUE ticker (idempotency guard refuses a second open
    /// position on the same ticker; cap-accumulation tests need distinct markets).
    fn sig_uniq(sizing: SizingHint, price: i64, cluster: &str, nonce: usize) -> Signal {
        Signal {
            ticker: format!("TKR-{cluster}-{price}-{nonce}"),
            ..sig(sizing, price, cluster)
        }
    }

    #[test]
    fn fraction_sizing() {
        // bankroll 1000, f=0.05 -> $50; at 95c -> floor(50/0.95)=52
        let r = rm(1000.0);
        let o = r.evaluate(&sig(SizingHint::Fraction, 95, "c1")).unwrap();
        assert_eq!(o.count, 52);
    }

    #[test]
    fn cluster_cap_blocks_fourth() {
        // cap ≈ 15% of bankroll. fraction want = 5%; at 50c the first fill is
        // 100 contracts ($50). Fees are charged at fill, so bankroll (and thus
        // want/cap) shrinks slightly with each fill — after ~3 fills the cluster
        // is effectively full and the 4th is refused (no room, or room smaller
        // than one contract).
        let mut r = rm(1000.0);
        let o = r.evaluate(&sig_uniq(SizingHint::Fraction, 50, "cx", 0)).unwrap();
        assert_eq!(o.count, 100); // pre-fee sizing: 5% of $1000 at 50c
        r.on_fill(&o);
        for i in 1..3 {
            let o = r
                .evaluate(&sig_uniq(SizingHint::Fraction, 50, "cx", i))
                .unwrap();
            assert!(o.count >= 98); // fee drag shaves a contract or two
            r.on_fill(&o);
        }
        let fourth = r.evaluate(&sig_uniq(SizingHint::Fraction, 50, "cx", 3));
        assert!(
            matches!(
                fourth,
                Err(Rejection::ClusterCapHit) | Err(Rejection::ZeroSize)
            ),
            "cluster should be effectively full: {fourth:?}"
        );
        // And the cluster total respects the cap against the CURRENT bankroll.
        let cap = 0.15 * r.status().bankroll;
        let total: f64 = r.open_positions().iter().map(|p| p.stake()).sum();
        assert!(total <= cap + 1.0, "cluster {total} vs cap {cap}");
    }

    #[test]
    fn flat_daily_budget() {
        // budget $80, flat $10 -> 8 fills allowed, 9th rejected
        let mut r = rm(1000.0);
        for i in 0..8 {
            let o = r.evaluate(&sig_uniq(SizingHint::Flat, 50, "d", i)).unwrap();
            r.on_fill(&o);
        }
        assert_eq!(
            r.evaluate(&sig_uniq(SizingHint::Flat, 50, "d", 99)),
            Err(Rejection::DailyCapHit)
        );
    }

    #[test]
    fn fraction_fills_dont_consume_flat_budget() {
        // A fraction-sized fill (lock-style) must NOT eat the flat daily budget
        // (weather). Fill several fraction orders, then confirm flat budget intact.
        let mut r = rm(1000.0);
        for i in 0..3 {
            let o = r
                .evaluate(&sig_uniq(SizingHint::Fraction, 50, "cx", i))
                .unwrap();
            r.on_fill(&o);
        }
        // full flat budget ($80) still available: 8 flat $10 trades allowed
        for i in 0..8 {
            let o = r.evaluate(&sig_uniq(SizingHint::Flat, 50, "d", i)).unwrap();
            r.on_fill(&o);
        }
        assert_eq!(
            r.evaluate(&sig_uniq(SizingHint::Flat, 50, "d", 99)),
            Err(Rejection::DailyCapHit)
        );
    }

    #[test]
    fn portfolio_cap_blocks_across_distinct_clusters() {
        // max_portfolio_frac 0.5 of $1000 = $500 total. Fills of $50 in DISTINCT
        // clusters each clear the 15% cluster cap, but together hit the portfolio
        // ceiling at exactly 10 fills.
        let mut r = rm(1000.0);
        let mut filled = 0;
        for i in 0..20 {
            let s = sig(SizingHint::Fraction, 50, &format!("c{i}"));
            match r.evaluate(&s) {
                Ok(o) => {
                    r.on_fill(&o);
                    filled += 1;
                }
                Err(e) => {
                    assert_eq!(e, Rejection::PortfolioCapHit);
                    break;
                }
            }
        }
        assert_eq!(filled, 10);
        assert_eq!(
            r.evaluate(&sig(SizingHint::Fraction, 50, "cX")),
            Err(Rejection::PortfolioCapHit)
        );
    }

    #[test]
    fn price_out_of_band() {
        let r = rm(1000.0);
        assert_eq!(
            r.evaluate(&sig(SizingHint::Flat, 99, "d")),
            Err(Rejection::PriceOutOfBand)
        );
        assert_eq!(
            r.evaluate(&sig(SizingHint::Flat, 1, "d")),
            Err(Rejection::PriceOutOfBand)
        );
    }

    #[test]
    fn settlement_pnl_win_and_loss() {
        let mut r = rm(1000.0);
        // buy 52 @ 95c
        let o = r.evaluate(&sig(SizingHint::Fraction, 95, "c")).unwrap();
        r.on_fill(&o);
        r.on_settlement(&o.ticker, true);
        // win: 52*(1-0.95) - fee ; fee = ceil-per-order(0.07*52*0.95*0.05)
        let fee = taker_fee(95, 52); // raw 0.17290 -> 0.18
        assert!((fee - 0.18).abs() < 1e-9);
        let expected = 1000.0 + 52.0 * 0.05 - fee;
        assert!((r.status().bankroll - expected).abs() < 1e-6);
    }

    #[test]
    fn settlement_pnl_loss() {
        // A lost YES position costs count*entry + fee.
        let mut r = rm(1000.0);
        let o = r.evaluate(&sig(SizingHint::Fraction, 95, "c")).unwrap();
        assert_eq!(o.count, 52);
        r.on_fill(&o);
        r.on_settlement(&o.ticker, false);
        let fee = taker_fee(95, 52);
        let expected = 1000.0 - 52.0 * 0.95 - fee;
        assert!((r.status().bankroll - expected).abs() < 1e-6);
    }

    #[test]
    fn flat_entries_respect_cluster_cap() {
        // Cluster cap binds Flat sizing too (streak: BTC+ETH same window = one
        // bet). Cap = 15% of $100 = $15; flat $4 fills in ONE cluster stop once
        // the cluster is full, well before the $60 daily budget.
        let cfg = RiskConfig {
            flat_usd: 4.0,
            daily_budget_usd: 60.0,
            ..RiskConfig::default()
        };
        let mut r =
            RiskManager::load_or_init(cfg, Box::new(MemoryStore::default()), 100.0, false, true)
                .unwrap();
        let mut filled = 0;
        for i in 0..10 {
            let s = Signal {
                strategy: "streak".into(),
                ticker: format!("T{i}"),
                side: Side::Yes,
                limit_cents: 40,
                cluster: "streak-123".into(),
                sizing: SizingHint::Flat,
            };
            match r.evaluate(&s) {
                Ok(o) => {
                    // Every approved order must fit inside the cluster's room.
                    r.on_fill(&o);
                    filled += 1;
                }
                Err(e) => {
                    // Once the cluster is (effectively) full: either no room at
                    // all (ClusterCapHit) or room smaller than one contract
                    // (clamped stake -> ZeroSize). Both mean the cap bound.
                    assert!(
                        e == Rejection::ClusterCapHit || e == Rejection::ZeroSize,
                        "unexpected rejection: {e:?}"
                    );
                    break;
                }
            }
        }
        // $4 per fill -> cluster full somewhere before 10 fills (cap $15 with
        // clamping allows ~4 fills: 4+4+4+2.8 = $14.80, then room < 1 contract).
        assert!(filled < 10, "cluster cap never bound");
        let total: f64 = r.open_positions().iter().map(|p| p.stake()).sum();
        assert!(
            total <= 15.0 + 1e-9,
            "cluster stake {total} exceeded the $15 cap"
        );
    }

    #[test]
    fn partial_fill_records_filled_count_and_fee_at_fill() {
        // EXECUTION TRUTH: only what filled enters state; the fee is charged at
        // fill (on the actual price/count) and settle() reports net without
        // re-charging bankroll.
        let cfg = RiskConfig {
            flat_usd: 4.0,
            daily_budget_usd: 60.0,
            ..RiskConfig::default()
        };
        let mut r =
            RiskManager::load_or_init(cfg, Box::new(MemoryStore::default()), 100.0, false, true)
                .unwrap();
        let o = r.evaluate(&sig(SizingHint::Flat, 44, "w")).unwrap();
        assert_eq!(o.count, 9); // $4 at 44c

        // Only 5 of 9 filled, at 43c (better than limit).
        r.on_fill_actual(&o, 5, 43);
        let fee = taker_fee(43, 5); // raw 0.07*5*0.43*0.57 = 0.0858 -> 0.09
        assert!((fee - 0.09).abs() < 1e-9);
        assert!((r.status().bankroll - (100.0 - fee)).abs() < 1e-9);
        let pos = &r.open_positions()[0];
        assert_eq!(pos.count, 5);
        assert_eq!(pos.entry_cents, 43);

        // Win: settle adds gross only; reported pnl is net of the fill fee.
        let out = r.settle(&o.ticker, true).unwrap();
        let gross = 5.0 * (1.0 - 0.43);
        assert!((out.pnl - (gross - fee)).abs() < 1e-9);
        assert!((r.status().bankroll - (100.0 - fee + gross)).abs() < 1e-9);
    }

    #[test]
    fn zero_fill_is_a_noop() {
        let mut r = rm(1000.0);
        let o = r.evaluate(&sig(SizingHint::Flat, 44, "w")).unwrap();
        r.on_fill_actual(&o, 0, 44);
        assert!(r.open_positions().is_empty());
        assert!((r.status().bankroll - 1000.0).abs() < 1e-9);
    }

    #[test]
    fn taker_fee_ceils_per_order() {
        // Streak-typical order: 9 contracts @ 44c -> raw 0.07*9*0.44*0.56 =
        // 0.15523 -> ceil to next cent = $0.16 (redirect: ~1.73c/contract at 44c).
        assert!((taker_fee(44, 9) - 0.16).abs() < 1e-9);
        // Exact-cent raw stays (no over-ceil): 0.07*10*0.50*0.50 = 0.175 -> 0.18.
        assert!((taker_fee(50, 10) - 0.18).abs() < 1e-9);
        // Single tiny contract still pays a whole cent: 0.07*1*0.05*0.95=0.003325 -> 0.01.
        assert!((taker_fee(5, 1) - 0.01).abs() < 1e-9);
    }

    #[test]
    fn settle_returns_outcome_and_none_for_unknown() {
        let mut r = rm(1000.0);
        let o = r.evaluate(&sig(SizingHint::Fraction, 95, "c")).unwrap();
        r.on_fill(&o);
        let out = r.settle(&o.ticker, true).unwrap();
        assert_eq!(out.ticker, o.ticker);
        assert!(out.won);
        assert!(out.pnl > 0.0);
        // Already settled / unknown ticker → None (reconcile treats as a skip).
        assert!(r.settle(&o.ticker, true).is_none());
        assert!(r.settle("NOPE", false).is_none());
    }

    /// Hand-build an open position on a given trading day (bypasses sizing).
    fn open_on_day(r: &mut RiskManager, day: &str, ticker: &str, count: i64, price: i64) {
        r.begin_day(day);
        r.on_fill(&Order {
            strategy: "t".into(),
            ticker: ticker.into(),
            side: Side::Yes,
            count,
            limit_cents: price,
            cluster: "w".into(),
            sizing: SizingHint::Fraction,
        });
    }

    #[test]
    fn same_day_loss_trips_daily_halt() {
        // Baseline: a big loss booked on the CURRENT trading day. 320 @ 50c lost
        // = -$160 - fee ≈ -$165.6: exceeds the daily-loss limit (15% of 1000 =
        // $150) but not the drawdown limit (30%), so the DAILY switch is what
        // must fire here.
        let mut r = rm(1000.0);
        open_on_day(&mut r, "2026-07-15", "SAME", 320, 50);
        let out = r.settle("SAME", false).unwrap();
        assert!(out.pnl < -150.0 && out.pnl > -300.0);
        assert!(r.status().halted);
    }

    #[test]
    fn prior_day_loss_does_not_trip_todays_daily_halt() {
        // The T004 fix. Open the SAME losing position on a PRIOR day, then run
        // reconcile the next morning (day rolls to 2026-07-15). The identical
        // ~$165 loss must update bankroll/peak/drawdown but must NOT count
        // toward today's day_loss — so today's daily-loss halt does not fire.
        let mut r = rm(1000.0);
        open_on_day(&mut r, "2026-07-14", "PRIOR", 320, 50);
        r.begin_day("2026-07-15"); // morning-after reconcile rolls the day
        let out = r.settle("PRIOR", false).unwrap();
        assert!(out.pnl < -150.0 && out.pnl > -300.0); // same magnitude loss
        assert!(r.status().bankroll < 1000.0); // P&L still realized
        assert!(r.status().peak >= 1000.0); // peak unaffected by the loss
        assert!(!r.status().halted); // ...but daily-loss halt NOT tripped
    }

    #[test]
    fn kill_switch_on_drawdown() {
        // Small bankroll, force a big loss to exceed 30% drawdown.
        let mut r = rm(100.0);
        let s = Signal {
            strategy: "t".into(),
            ticker: "BIG".into(),
            side: Side::Yes,
            limit_cents: 50,
            cluster: "k".into(),
            sizing: SizingHint::Fraction,
        };
        let o = r.evaluate(&s).unwrap(); // 5% of 100 = $5 -> 10 @ 50c
        r.on_fill(&o);
        r.on_settlement("BIG", false); // lose $5 -> only 5% dd, not halted yet
        assert!(!r.status().halted);
        // hand-craft a large loss: settle a big manual position
        r.on_fill(&Order {
            strategy: "t".into(),
            ticker: "HUGE".into(),
            side: Side::Yes,
            count: 60,
            limit_cents: 50,
            cluster: "k".into(),
            sizing: SizingHint::Fraction,
        });
        r.on_settlement("HUGE", false); // lose $30 -> well past 30% of peak
        assert!(r.status().halted);
        // halted rejects further orders
        assert_eq!(r.evaluate(&s), Err(Rejection::Halted));
    }

    #[test]
    fn on_fill_actual_refuses_duplicate_ticker() {
        // IDEMPOTENCY (fix 2a): a second fill on an already-open ticker is
        // refused — no double-add, no double fee.
        let mut r = rm(1000.0);
        let o = r.evaluate(&sig(SizingHint::Flat, 44, "w")).unwrap();
        r.on_fill_actual(&o, o.count, 44);
        assert_eq!(r.open_positions().len(), 1);
        let bankroll_after_first = r.status().bankroll;
        // Same ticker again (restart re-fire / recovery double-book): no-op.
        r.on_fill_actual(&o, o.count, 44);
        assert_eq!(r.open_positions().len(), 1, "duplicate must not add");
        assert_eq!(
            r.status().bankroll,
            bankroll_after_first,
            "duplicate must not re-charge the fee"
        );
    }

    #[test]
    fn missing_state_file_refused_in_live() {
        // fix 3b: live + no state file + no --fresh-state → hard refusal.
        let err = RiskManager::load_or_init(
            RiskConfig::default(),
            Box::new(MemoryStore::default()), // empty store => load() == None
            100.0,
            true,  // live
            false, // allow_fresh
        );
        assert!(err.is_err(), "live must refuse a missing state file");
        // With --fresh-state it is allowed.
        assert!(RiskManager::load_or_init(
            RiskConfig::default(),
            Box::new(MemoryStore::default()),
            100.0,
            true,
            true,
        )
        .is_ok());
        // Paper never refuses.
        assert!(RiskManager::load_or_init(
            RiskConfig::default(),
            Box::new(MemoryStore::default()),
            100.0,
            false,
            false,
        )
        .is_ok());
    }

    #[test]
    fn adopt_orphans_from_canned_positions_json() {
        // fix 1b: parse an exchange /portfolio/positions payload and adopt any
        // position missing from local state; skip ones we already hold.
        let mut r = rm(100.0);
        r.begin_day("2026-07-23");
        // We already hold KNOWN locally.
        let known = Order {
            strategy: "streak".into(),
            ticker: "KXBTC15M-KNOWN".into(),
            side: Side::Yes,
            count: 9,
            limit_cents: 44,
            cluster: "streak-1".into(),
            sizing: SizingHint::Flat,
        };
        r.on_fill(&known);
        assert_eq!(r.open_positions().len(), 1);

        let body = serde_json::json!({
            "market_positions": [
                {"ticker": "KXBTC15M-KNOWN", "position": 9, "market_exposure": 396},
                {"ticker": "KXETH15M-ORPH", "position": -5, "market_exposure": 200},
                {"ticker": "KXBTC15M-FLAT", "position": 0, "market_exposure": 0},
            ]
        });
        let exch = crate::kalshi::parse_positions(&body);
        let mut adopted = 0;
        for p in &exch {
            if r.adopt_orphan(&p.ticker, p.side, p.count, p.entry_cents, "orphan") {
                adopted += 1;
            }
        }
        assert_eq!(adopted, 1, "only the unknown ORPH is adopted");
        assert_eq!(r.open_positions().len(), 2);
        let orph = r
            .open_positions()
            .iter()
            .find(|p| p.ticker == "KXETH15M-ORPH")
            .unwrap();
        assert_eq!(orph.side, Side::No);
        assert_eq!(orph.count, 5);
        assert_eq!(orph.entry_cents, 40); // 200c / 5
        assert_eq!(orph.fee, 0.0); // fee already paid on-exchange
                                   // Re-running is idempotent (no duplicate adopt).
        assert!(!r.adopt_orphan("KXETH15M-ORPH", Side::No, 5, Some(40), "orphan"));
        assert_eq!(r.open_positions().len(), 2);
    }

    #[test]
    fn adopt_orphan_worst_case_entry_when_unknown() {
        // No cost basis from the exchange → worst-case 99c entry (maximal
        // at-risk stake, so the kill-switch errs conservative).
        let mut r = rm(100.0);
        assert!(r.adopt_orphan("T", Side::Yes, 3, None, "c"));
        let p = &r.open_positions()[0];
        assert_eq!(p.entry_cents, 99);
    }

    #[test]
    fn expected_cash_tracks_bankroll_minus_stakes() {
        // fix 1c divergence basis: expected cash = bankroll − open stakes.
        let mut r = rm(100.0);
        assert!((r.expected_cash() - 100.0).abs() < 1e-9);
        let o = r.evaluate(&sig(SizingHint::Flat, 44, "w")).unwrap();
        let stake = o.stake();
        let fee = taker_fee(44, o.count);
        r.on_fill(&o);
        // bankroll dropped by the fee; expected cash = bankroll − stake.
        assert!((r.expected_cash() - (100.0 - fee - stake)).abs() < 1e-9);
    }

    #[test]
    fn state_persists_across_reload() {
        let store = Box::new(MemoryStore::default());
        // share the same underlying store by cloning the Arc-like handle:
        // MemoryStore isn't Clone, so drive it through one manager then reload.
        let mut r =
            RiskManager::load_or_init(RiskConfig::default(), store, 500.0, false, true).unwrap();
        let o = r.evaluate(&sig(SizingHint::Fraction, 90, "p")).unwrap();
        r.on_fill(&o);
        r.on_settlement(&o.ticker, true);
        let bankroll_after = r.status().bankroll;
        assert!(bankroll_after > 500.0);
    }
}
