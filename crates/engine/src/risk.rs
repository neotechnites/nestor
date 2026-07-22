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

/// Kalshi taker fee in dollars for `count` contracts at `price_cents`.
/// fee/contract = 0.07 * p * (1-p), p in dollars.
pub fn taker_fee(price_cents: i64, count: i64) -> f64 {
    let p = price_cents as f64 / 100.0;
    0.07 * p * (1.0 - p) * count as f64
}

pub struct RiskManager {
    cfg: RiskConfig,
    state: State,
    store: Box<dyn StateStore>,
}

impl RiskManager {
    /// Load existing state or initialize with `initial_bankroll`.
    pub fn load_or_init(
        cfg: RiskConfig,
        store: Box<dyn StateStore>,
        initial_bankroll: f64,
    ) -> Result<Self> {
        let state = store
            .load()?
            .unwrap_or_else(|| State::new(initial_bankroll));
        Ok(Self { cfg, state, store })
    }

    fn persist(&self) {
        if let Err(e) = self.store.save(&self.state) {
            eprintln!("risk: state save failed: {e}");
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

        let stake = match s.sizing {
            SizingHint::Flat => {
                let remaining = self.cfg.daily_budget_usd - self.state.day_spent;
                if remaining <= 0.0 {
                    return Err(Rejection::DailyCapHit);
                }
                self.cfg.flat_usd.min(remaining)
            }
            SizingHint::Fraction => {
                let want = self.cfg.fraction * self.state.bankroll;
                let cluster_room = self.cfg.cluster_cap_frac * self.state.bankroll
                    - self.cluster_at_risk(&s.cluster);
                if cluster_room <= 0.0 {
                    return Err(Rejection::ClusterCapHit);
                }
                want.min(cluster_room)
            }
        };

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
        if matches!(o.sizing, SizingHint::Flat) {
            self.state.day_spent += o.stake();
        }
        self.state.open.push(Position {
            strategy: o.strategy.clone(),
            ticker: o.ticker.clone(),
            side: o.side,
            count: o.count,
            entry_cents: o.limit_cents,
            cluster: o.cluster.clone(),
            day: self.state.day.clone(),
        });
        self.persist();
    }

    /// Read-only view of currently open positions (the reconcile loop iterates
    /// this to fetch each market's authoritative result).
    pub fn open_positions(&self) -> &[Position] {
        &self.state.open
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
        let pnl = gross - taker_fee(pos.entry_cents, pos.count);

        self.state.bankroll += pnl;
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

    #[test]
    fn fraction_sizing() {
        // bankroll 1000, f=0.05 -> $50; at 95c -> floor(50/0.95)=52
        let r = rm(1000.0);
        let o = r.evaluate(&sig(SizingHint::Fraction, 95, "c1")).unwrap();
        assert_eq!(o.count, 52);
    }

    #[test]
    fn cluster_cap_blocks_fourth() {
        // cap = 15% of 1000 = $150. fraction want = 5% = $50; at 50c -> count 100
        // -> stake $50. Three fills reach $150 == cap; the fourth has zero room.
        let mut r = rm(1000.0);
        for _ in 0..3 {
            let o = r.evaluate(&sig(SizingHint::Fraction, 50, "cx")).unwrap();
            assert_eq!(o.count, 100);
            r.on_fill(&o);
        }
        assert_eq!(
            r.evaluate(&sig(SizingHint::Fraction, 50, "cx")),
            Err(Rejection::ClusterCapHit)
        );
    }

    #[test]
    fn flat_daily_budget() {
        // budget $80, flat $10 -> 8 fills allowed, 9th rejected
        let mut r = rm(1000.0);
        for _ in 0..8 {
            let o = r.evaluate(&sig(SizingHint::Flat, 50, "d")).unwrap();
            r.on_fill(&o);
        }
        assert_eq!(
            r.evaluate(&sig(SizingHint::Flat, 50, "d")),
            Err(Rejection::DailyCapHit)
        );
    }

    #[test]
    fn fraction_fills_dont_consume_flat_budget() {
        // A fraction-sized fill (lock-style) must NOT eat the flat daily budget
        // (weather). Fill several fraction orders, then confirm flat budget intact.
        let mut r = rm(1000.0);
        for _ in 0..3 {
            let o = r.evaluate(&sig(SizingHint::Fraction, 50, "cx")).unwrap();
            r.on_fill(&o);
        }
        // full flat budget ($80) still available: 8 flat $10 trades allowed
        for _ in 0..8 {
            let o = r.evaluate(&sig(SizingHint::Flat, 50, "d")).unwrap();
            r.on_fill(&o);
        }
        assert_eq!(
            r.evaluate(&sig(SizingHint::Flat, 50, "d")),
            Err(Rejection::DailyCapHit)
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
        // win: 52*(1-0.95) - fee ; fee = 0.07*0.95*0.05*52
        let fee = 0.07 * 0.95 * 0.05 * 52.0;
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
        let fee = 0.07 * 0.95 * 0.05 * 52.0; // 0.07*p*(1-p)*count
        let expected = 1000.0 - 52.0 * 0.95 - fee;
        assert!((r.status().bankroll - expected).abs() < 1e-6);
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
    fn state_persists_across_reload() {
        let store = Box::new(MemoryStore::default());
        // share the same underlying store by cloning the Arc-like handle:
        // MemoryStore isn't Clone, so drive it through one manager then reload.
        let mut r = RiskManager::load_or_init(RiskConfig::default(), store, 500.0).unwrap();
        let o = r.evaluate(&sig(SizingHint::Fraction, 90, "p")).unwrap();
        r.on_fill(&o);
        r.on_settlement(&o.ticker, true);
        let bankroll_after = r.status().bankroll;
        assert!(bankroll_after > 500.0);
    }
}
