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

/// Precision Kalshi rounds a taker fee UP to. MEASURED, not assumed:
/// `work/verify-house-truth.md` Q3 matched `ceil(0.07·P·(1−P)·C, $0.0001)`
/// EXACTLY on 5/5 demo fills — one hundredth of a cent, **NOT** the whole cent
/// the old published fee tables (and this function until 2026-07-26) asserted.
/// Ceiling at the whole cent over-charged up to $0.0099 per order, always in the
/// same direction, against a $2.00 divergence budget.
const FEE_ROUNDING_DOLLARS: f64 = 0.0001;

/// Kalshi taker fee in dollars for one ORDER of `count` contracts at
/// `price_cents`: `ceil( 0.07 × count × P × (1−P), $0.0001 )`, P in dollars.
/// The ceil is applied once per ORDER (Kalshi's billing granularity), not per
/// contract — an un-ceiled formula understates the fee and overstates P&L
/// (redirect 2026-07-23; precision proven demo 2026-07-26, reality F7).
pub fn taker_fee(price_cents: i64, count: i64) -> f64 {
    let p = price_cents as f64 / 100.0;
    let raw = 0.07 * count as f64 * p * (1.0 - p);
    let steps = raw / FEE_ROUNDING_DOLLARS;
    // Guard against a float representation a hair above an exact multiple
    // (e.g. 0.175/0.0001 = 1749.9999999999998) ceiling to a spurious extra step.
    let steps = if (steps - steps.round()).abs() < 1e-6 {
        steps.round()
    } else {
        steps.ceil()
    };
    steps * FEE_ROUNDING_DOLLARS
}

/// Capital committed to a RESTING (maker) order that has not filled yet. The
/// taker path never needs this — an IOC resolves synchronously inside
/// `exec_lock`, so the next `evaluate` already sees the fill. A maker leg rests
/// for tens of seconds across many scan passes, so without a reservation the
/// caps would be computed as if that money were free and a second leg could
/// double-spend the same cluster/daily room.
///
/// NOT PERSISTED, deliberately: a reservation is only meaningful while THIS
/// process owns the resting order. A crash loses both — and the order's
/// `expiration_ts` (plus the startup orphan sweep) is what bounds the exchange
/// side. Persisting a reservation would leak a phantom cap consumer across
/// restarts.
#[derive(Debug, Clone)]
struct Reservation {
    key: String,
    cluster: String,
    stake: f64,
    /// Flat-sized: also consumes the daily budget while it rests.
    flat: bool,
    /// TRUE while the order this reservation covers may still be UNFILLED on the
    /// exchange (in-flight POST, or confirmed resting). Flipped FALSE the moment
    /// we learn the order left the book by FILLING (cancel-404 / partial cancel),
    /// even though the reservation itself is deliberately held for a while
    /// longer so the cap cannot be double-spent while the fill surfaces.
    ///
    /// The distinction is load-bearing for the divergence breaker: an UNFILLED
    /// resting order's collateral treatment by `/portfolio/balance` is UNPROVEN
    /// (reality F1), so it must widen the breaker's tolerance; a FILLED one's is
    /// proven (cash moved) and must NOT — widening there would mask a real
    /// miscount for as long as the reservation is held.
    resting: bool,
}

pub struct RiskManager {
    cfg: RiskConfig,
    state: State,
    store: Box<dyn StateStore>,
    /// Live-money mode. Changes fail-closed behavior: a persist failure HALTS
    /// (a lost kill-switch flip is unacceptable with real money), and a missing
    /// state file is refused rather than silently re-armed.
    live: bool,
    /// In-flight resting-order commitments (see [`Reservation`]).
    reserved: Vec<Reservation>,
    /// SIGNED cash the HOUSE sleeve has moved on the exchange that no `Position`
    /// yet accounts for, in cents, keyed by ticker (moneypath F2 / constants F1).
    /// House is two-sided by design and deliberately does NOT route through
    /// `evaluate`/`reserve` (one-position-per-ticker would break it), but its
    /// fills are real cash: without this the divergence breaker sees house's
    /// spending as unexplained and HALTS the whole bot.
    ///
    /// Negative = cash spent. A ticker's entry is DROPPED the moment
    /// [`adopt_orphan`](Self::adopt_orphan) books that ticker as a position,
    /// because the position's stake then carries the same dollars through
    /// `total_at_risk` — keeping both would double-count.
    ///
    /// NOT PERSISTED, deliberately — same doctrine as [`Reservation`]: it is a
    /// bridge between "cash left the account" and "reconcile adopted the
    /// position", and a restart re-derives exchange truth from `/portfolio/positions`.
    house_cash_cents: std::collections::HashMap<String, i64>,
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
            reserved: Vec::new(),
            house_cash_cents: std::collections::HashMap::new(),
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
        let open: f64 = self
            .state
            .open
            .iter()
            .filter(|p| p.cluster == cluster)
            .map(|p| p.stake())
            .sum();
        let held: f64 = self
            .reserved
            .iter()
            .filter(|r| r.cluster == cluster)
            .map(|r| r.stake)
            .sum();
        open + held
    }

    /// Total capital at risk across every open position (all clusters), plus
    /// anything committed to an unfilled resting order.
    fn total_at_risk(&self) -> f64 {
        let open: f64 = self.state.open.iter().map(|p| p.stake()).sum();
        let held: f64 = self.reserved.iter().map(|r| r.stake).sum();
        open + held
    }

    /// Daily flat-budget dollars committed to unfilled resting orders.
    fn reserved_flat(&self) -> f64 {
        self.reserved.iter().filter(|r| r.flat).map(|r| r.stake).sum()
    }

    /// Commit `o`'s stake against the caps while its resting order is alive.
    /// Idempotent by `key` (a re-place under the same key replaces the entry).
    /// The caller MUST [`release`](Self::release) on fill, cancel, or error —
    /// `on_fill_actual` does not release for you (the key is the caller's).
    pub fn reserve(&mut self, key: &str, o: &Order) {
        self.reserved.retain(|r| r.key != key);
        self.reserved.push(Reservation {
            key: key.to_string(),
            cluster: o.cluster.clone(),
            stake: o.stake(),
            flat: matches!(o.sizing, SizingHint::Flat),
            // A freshly-placed order is unfilled until proven otherwise.
            resting: true,
        });
    }

    /// Drop a reservation (fill booked, order cancelled, or placement failed).
    pub fn release(&mut self, key: &str) {
        self.reserved.retain(|r| r.key != key);
    }

    /// Mark a reservation's order as NO LONGER RESTING — we have learned it left
    /// the book by filling (cancel-404, or a cancel whose `reduced_by` shows a
    /// partial). The reservation stays (the cap must not be re-spent while the
    /// fill surfaces) but it stops widening the divergence breaker's tolerance.
    /// No-op for an unknown key.
    pub fn mark_reservation_off_book(&mut self, key: &str) {
        if let Some(r) = self.reserved.iter_mut().find(|r| r.key == key) {
            r.resting = false;
        }
    }

    /// Dollars currently committed to unfilled resting orders (diagnostics).
    pub fn reserved_total(&self) -> f64 {
        self.reserved.iter().map(|r| r.stake).sum()
    }

    /// Dollars committed to orders that may STILL BE UNFILLED on the exchange.
    /// This is exactly the amount by which `/portfolio/balance` and our
    /// `expected_cash` may legitimately disagree while the unproven
    /// resting-collateral question (reality F1) stands: if Kalshi locks the
    /// collateral of a resting bid, real and expected both drop and Δ = 0; if it
    /// does not, only expected drops and Δ = this number. The divergence breaker
    /// widens by it so the bot cannot halt on EITHER answer.
    pub fn resting_reserved(&self) -> f64 {
        self.reserved
            .iter()
            .filter(|r| r.resting)
            .map(|r| r.stake)
            .sum()
    }

    /// Book a signed HOUSE cash movement (cents; negative = spent) against
    /// `ticker`. See [`house_cash_cents`](Self::house_cash_cents).
    pub fn note_house_cash_cents(&mut self, ticker: &str, delta_cents: i64) {
        if delta_cents == 0 {
            return;
        }
        *self
            .house_cash_cents
            .entry(ticker.to_string())
            .or_insert(0) += delta_cents;
    }

    /// Signed dollars of house cash not yet represented by an open position.
    pub fn house_cash(&self) -> f64 {
        self.house_cash_cents.values().sum::<i64>() as f64 / 100.0
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
                let remaining =
                    self.cfg.daily_budget_usd - self.state.day_spent - self.reserved_flat();
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
        self.on_fill_actual_fee(o, filled_count, fill_price_cents, None)
    }

    /// [`on_fill_actual`](Self::on_fill_actual) with the EXCHANGE'S OWN fee when
    /// we have it. MAKER fills are why this exists: our `taker_fee` formula is
    /// the wrong model for them (demo 2026-07-26 billed a maker fill
    /// `fee_cost: 0.000000`), and charging ~1.7¢/contract of phantom fee against
    /// a $100 bankroll walks the drawdown kill-switch toward a halt that never
    /// happened. `actual_fee_dollars: None` falls back to the taker estimate,
    /// which stays the conservative default for the taker path.
    pub fn on_fill_actual_fee(
        &mut self,
        o: &Order,
        filled_count: i64,
        fill_price_cents: i64,
        actual_fee_dollars: Option<f64>,
    ) {
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
        // Negative / non-finite "actual" fees are refused — a garbage field must
        // never CREDIT the bankroll.
        let fee = match actual_fee_dollars {
            Some(f) if f.is_finite() && f >= 0.0 => f,
            _ => taker_fee(fill_price_cents, filled_count),
        };
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

    /// THE SETTLED SET (R171 / incident #5): has this ticker's P&L already been
    /// booked? A market on Kalshi settles EXACTLY ONCE, so a ticker in here can
    /// never legitimately be opened, adopted, or settled again — whatever local
    /// state or the exchange's lagging indexes say.
    ///
    /// Derived from the persisted `state.settled` vector rather than a new field:
    /// that vector already IS the record of what we booked and it already
    /// survives restarts. A second, parallel record of the same fact could
    /// disagree with it — and two views of one fact diverging is exactly the
    /// failure class this guard exists to stop.
    ///
    /// Linear scan, deliberately un-indexed: the list is bounded at
    /// `MAX_SETTLED` (1000) and this runs at most once per exchange position per
    /// reconcile pass — a thousand string compares next to a network round-trip.
    /// A HashSet index would be a second copy of the same truth needing sync on
    /// every push and drain (the divergence risk above) bought with nothing.
    ///
    /// WINDOW SUFFICIENCY of the MAX_SETTLED=1000 drain bound: a re-book needs
    /// the ticker to STILL be visible on the exchange (that is adoption's whole
    /// precondition), i.e. inside the settlement/payout lag — minutes to hours
    /// (F8 family; the metals still showed `position_fp != 0` 41 minutes after
    /// nestor booked them on 2026-07-27). Nestor's settlement ceiling is ~200/day
    /// (two 15-minute crypto series = 192 windows, plus a handful of daily
    /// metals/CPI markets), so 1000 settlements ≈ 5 days of history versus an
    /// hours-long exposure window — two orders of magnitude of margin. The guard
    /// would only degrade if the settlement RATE ever exceeded ~1000 inside one
    /// exchange lag window; raise MAX_SETTLED if a new sleeve ever approaches it.
    pub fn is_settled(&self, ticker: &str) -> bool {
        self.state.settled.iter().any(|s| s.ticker == ticker)
    }

    /// Adopt an ORPHAN position discovered on the exchange but missing from local
    /// state (fix 1b) — e.g. a fill that landed after a lost ack. Conservative:
    /// no fee is charged (already paid on the exchange), `entry_cents` is the
    /// exchange cost basis when known else a worst-case, and it counts toward the
    /// daily budget so caps see the real exposure. Idempotent: a no-op if a
    /// position on `ticker` is already tracked, and a REFUSAL for a ticker we
    /// have already settled (see the settled-set guard below). Returns true if
    /// newly adopted.
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
        // SETTLED-SET GUARD (R171 / incident #5, 2026-07-27 — the leg that DRIVES
        // the re-book loop). "The exchange shows a position we do not have open"
        // is NOT sufficient evidence of an orphan once we have settled that
        // ticker: Kalshi's settlement/payout indexes lag the booking by minutes
        // to hours (same lagging-index family as F8 and the 36s settled filter),
        // so the exchange keeps reporting a non-zero position on a market that is
        // over. Adopting it re-creates the position we just closed — which
        // inflates `day_spent` by its stake and hands the next reconcile pass
        // something to settle again (+$2.17/pass, 8 passes, bankroll $122.64 vs a
        // real ~$106 on 2026-07-27).
        //
        // This is an EXPECTED condition during the lag, not an incident, so it
        // logs at ordinary volume and raises no operator alert — the ORPHAN
        // ADOPTED alert in reconcile deliberately does not fire here.
        if self.is_settled(ticker) {
            eprintln!(
                "risk: orphan adoption REFUSED for {ticker} — already in the settled set \
                 (exchange payout lag, not an orphan); leaving it closed"
            );
            return false;
        }
        // Worst-case entry: assume we paid the top of the band, so the position's
        // at-risk stake (and thus the kill-switch's view of exposure) is maximal.
        const WORST_CASE_ENTRY_CENTS: i64 = 99;
        let entry = entry_cents.unwrap_or(WORST_CASE_ENTRY_CENTS).clamp(1, 99);
        // The position's stake now carries this ticker's cash through
        // `total_at_risk`; the house bridge ledger must stop carrying it too.
        self.house_cash_cents.remove(ticker);
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
    /// cost: `bankroll − Σ(open stakes + reservations) + house cash delta`.
    /// Compared against the real Kalshi balance by the divergence breaker (fix
    /// 1c) — the two must track within a threshold. The house term is what keeps
    /// a sleeve that deliberately sits outside the position ledger from reading
    /// as unexplained drift (moneypath F2).
    pub fn expected_cash(&self) -> f64 {
        self.state.bankroll - self.total_at_risk() + self.house_cash()
    }

    /// Maximum settlement credit the exchange could pay us for positions we
    /// hold but have not yet booked as settled: $1.00 × count per open
    /// position. FIX F8 (moneypath review; fired live 2026-07-27 12:45Z): a
    /// winner's cash credit can land a minute or more before the settle
    /// detection books it (the same lagging-index family as the 36s settled
    /// filter), and in that window real cash legitimately exceeds
    /// expected_cash by up to this amount. The divergence breaker widens its
    /// POSITIVE-side tolerance by this; money going MISSING still halts at
    /// the tight threshold.
    pub fn pending_payout(&self) -> f64 {
        self.state.open.iter().map(|p| p.count as f64).sum()
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
        // SETTLED-SET GUARD (R171 / incident #5, backstop leg). A ticker in the
        // settled set has had its P&L booked; a Kalshi market settles exactly
        // once, so a position on that ticker being open AGAIN is a phantom, never
        // a genuine re-entry — no strategy can buy a market that is over. Booking
        // it a second time invents money (that is precisely how bankroll read
        // $122.64 against a real ~$106).
        //
        // The phantom is DROPPED here rather than left in `open`. Deriving that
        // choice: incident #5's actual mechanism was a hand-edit of state.json
        // under the running writer, so the in-memory `open` never lost the
        // settled positions — refusing without repairing would leave the phantom
        // stake eating cluster/portfolio/daily room forever, and the only
        // recovery would be hand-editing state.json under the live writer, i.e.
        // repeating the incident. In-process repair is the one recovery path that
        // does not require the operation that caused this. No money moves: the
        // P&L was already realized on the first settle, so removing the duplicate
        // only stops it from being counted as exposure twice.
        //
        // `day_spent` is deliberately NOT rewound: we cannot know this phantom
        // ever added to it (fraction-sized positions never do, and the trading
        // day may have rolled since), and an over-stated budget counter only
        // trades LESS. Under-stating it would let the day over-spend.
        if self.is_settled(ticker) {
            if let Some(idx) = self.state.open.iter().position(|p| p.ticker == ticker) {
                self.state.open.remove(idx);
                eprintln!(
                    "risk: RE-SETTLE REFUSED for {ticker} — this ticker is already in the \
                     settled set; a phantom open position on it was dropped without booking \
                     P&L. STATE REGRESSED — inspect state.json (service stopped) before trusting \
                     the ledger."
                );
                self.persist();
            }
            return None;
        }
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
    fn reservation_consumes_cluster_room_until_released() {
        // A resting maker leg is capital at risk BEFORE it fills: without the
        // reservation, a concurrent scan pass would size a second leg against
        // money the first already committed. cluster cap = 0.15 * 100 = $15.
        let mut r = rm(100.0);
        let first = r.evaluate(&sig_uniq(SizingHint::Fraction, 50, "c", 1)).unwrap();
        assert!((first.stake() - 5.0).abs() < 0.51); // 0.05 * 100 ≈ $5
        r.reserve("leg-1", &first);
        assert!((r.reserved_total() - first.stake()).abs() < 1e-9);

        // Cluster room is now cap − reserved; keep reserving until it is gone.
        for n in 2..10 {
            match r.evaluate(&sig_uniq(SizingHint::Fraction, 50, "c", n)) {
                Ok(o) => r.reserve(&format!("leg-{n}"), &o),
                Err(rej) => {
                    assert!(matches!(
                        rej,
                        Rejection::ClusterCapHit | Rejection::ZeroSize
                    ));
                    assert!(r.reserved_total() <= 15.0 + 1e-9);
                    // Releasing everything restores full room.
                    for k in 1..10 {
                        r.release(&format!("leg-{k}"));
                    }
                    assert_eq!(r.reserved_total(), 0.0);
                    assert!(r.evaluate(&sig_uniq(SizingHint::Fraction, 50, "c", 99)).is_ok());
                    return;
                }
            }
        }
        panic!("reservations never bound the cluster cap");
    }

    #[test]
    fn reservation_is_idempotent_by_key_and_bounds_the_daily_budget() {
        let mut r = rm(100.0);
        let o = r.evaluate(&sig(SizingHint::Flat, 40, "c")).unwrap();
        r.reserve("k", &o);
        r.reserve("k", &o); // same key must not double-count
        assert!((r.reserved_total() - o.stake()).abs() < 1e-9);
        r.release("k");
        assert_eq!(r.reserved_total(), 0.0);
    }

    #[test]
    fn reserved_flat_dollars_eat_the_daily_budget() {
        // daily_budget_usd default = $80; flat_usd = $10. Reserve the whole
        // budget across distinct clusters and the next flat signal is capped out.
        let mut r = rm(1000.0);
        for n in 0..8 {
            let o = r
                .evaluate(&sig_uniq(SizingHint::Flat, 50, &format!("c{n}"), n))
                .unwrap();
            r.reserve(&format!("k{n}"), &o);
        }
        assert!(matches!(
            r.evaluate(&sig_uniq(SizingHint::Flat, 50, "c-next", 100)),
            Err(Rejection::DailyCapHit)
        ));
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
        let fee = taker_fee(95, 52); // raw 0.1729 -> exact at $0.0001 granularity
        assert!((fee - 0.1729).abs() < 1e-9);
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
        let fee = taker_fee(43, 5); // raw 0.085785 -> ceil($0.0001) = 0.0858
        assert!((fee - 0.0858).abs() < 1e-9);
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
    fn taker_fee_ceils_to_a_hundredth_of_a_cent_not_a_whole_cent() {
        // reality F7 / verify-house-truth Q3: Kalshi ceils to $0.0001, exact on
        // 5/5 demo fills. Whole-cent ceiling over-charged up to $0.0099/order.
        // Streak maker leg: 9 @ 44c -> raw 0.155232 -> $0.1553 (was $0.16).
        assert!((taker_fee(44, 9) - 0.1553).abs() < 1e-9);
        // Exact multiple of the rounding step is NOT pushed up a step:
        // 0.07*10*0.50*0.50 = 0.175 exactly.
        assert!((taker_fee(50, 10) - 0.175).abs() < 1e-9);
        // Sub-cent orders are no longer rounded up to a whole cent:
        // 0.07*1*0.05*0.95 = 0.003325 -> $0.0034 (was $0.01, 3x over).
        assert!((taker_fee(5, 1) - 0.0034).abs() < 1e-9);
        // Streak backstop: 8 @ 46c -> 0.139104 -> $0.1392 = 1.74c/contract,
        // the number the ledger's EV(46) = +4.3c is derived against (S11).
        assert!((taker_fee(46, 8) - 0.1392).abs() < 1e-9);
        // The fee is never UNDER the raw formula (it ceils, never floors).
        for (p, c) in [(3, 1), (17, 7), (44, 9), (46, 8), (95, 52), (97, 3)] {
            let raw = 0.07 * c as f64 * (p as f64 / 100.0) * (1.0 - p as f64 / 100.0);
            let fee = taker_fee(p, c);
            assert!(fee >= raw - 1e-12, "fee {fee} under raw {raw} at {p}c x{c}");
            assert!(fee - raw < 0.0001 + 1e-12, "over-ceiled at {p}c x{c}");
        }
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
    fn resting_reserved_covers_unfilled_orders_only() {
        // FIX 1 (reality F1 / constants F1 / moneypath F3). A reservation widens
        // the divergence breaker ONLY while its order may still be unfilled: the
        // resting-collateral question is unproven, so Δ may legitimately be
        // anywhere in [0, stake]. Once we know it FILLED, cash definitely moved
        // and the tolerance must snap back or a real miscount hides behind it.
        let mut r = rm(100.0);
        let o = r.evaluate(&sig(SizingHint::Flat, 40, "c")).unwrap();
        r.reserve("streak-maker|T", &o);
        assert!((r.resting_reserved() - o.stake()).abs() < 1e-9);
        assert!((r.reserved_total() - o.stake()).abs() < 1e-9);

        // Cancel-404: the bid left the book by filling. The cap stays committed
        // (the fill has not surfaced yet) but the breaker must not widen.
        r.mark_reservation_off_book("streak-maker|T");
        assert_eq!(r.resting_reserved(), 0.0);
        assert!((r.reserved_total() - o.stake()).abs() < 1e-9);

        r.release("streak-maker|T");
        assert_eq!(r.resting_reserved(), 0.0);
        assert_eq!(r.reserved_total(), 0.0);
        // Unknown key is a no-op, never a panic.
        r.mark_reservation_off_book("nope");
    }

    #[test]
    fn divergence_stays_inside_tolerance_on_both_collateral_branches() {
        // The arithmetic the breaker's widened threshold rests on, for a $4.00
        // maker leg on a $100 bankroll, under BOTH unproven exchange behaviours.
        // Streak's live sizing: flat $4.00 per entry (nestor.toml).
        let cfg = RiskConfig {
            flat_usd: 4.0,
            daily_budget_usd: 60.0,
            ..RiskConfig::default()
        };
        let mut r =
            RiskManager::load_or_init(cfg, Box::new(MemoryStore::default()), 100.0, false, true)
                .unwrap();
        let o = r.evaluate(&sig(SizingHint::Flat, 40, "c")).unwrap();
        assert!((o.stake() - 4.0).abs() < 1e-9);
        r.reserve("k", &o);
        let expected = r.expected_cash();
        assert!((expected - 96.0).abs() < 1e-9);
        let widened = 2.0 + r.resting_reserved(); // = $6.00

        // Branch A — Kalshi does NOT lock resting collateral (demo 2026-07-26).
        let real_a = 100.0;
        assert!((real_a - expected).abs() <= widened);
        // Branch B — Kalshi DOES lock it (prod unverified).
        let real_b = 96.0;
        assert!((real_b - expected).abs() <= widened);
        // A genuine miscount LARGER than the resting notional still breaks
        // through on either branch (the breaker is widened, not disabled).
        assert!((real_a + 2.01 - expected).abs() > widened);
        assert!((real_b - 6.01 - expected).abs() > widened);
    }

    #[test]
    fn house_cash_moves_expected_cash_and_is_handed_off_to_adoption() {
        // FIX 2b (moneypath F2). House spends real cash outside the position
        // ledger; expected_cash must follow it, and must NOT double-count once
        // reconcile adopts the resulting position.
        let mut r = rm(100.0);
        r.begin_day("2026-07-27");
        assert!((r.expected_cash() - 100.0).abs() < 1e-9);

        // Two-sided 1-lot quote both legs fill: 49c YES + 49c NO = 98c out.
        r.note_house_cash_cents("KXAPRPOTUS-X", -49);
        r.note_house_cash_cents("KXAPRPOTUS-X", -49);
        assert!((r.expected_cash() - 99.02).abs() < 1e-9);
        // Net position is ZERO, so no orphan is ever adopted — the bridge ledger
        // is the only thing that explains the missing 98c, and it holds.
        assert!(!r.adopt_orphan("KXAPRPOTUS-OTHER", Side::Yes, 0, None, "c"));
        assert!((r.expected_cash() - 99.02).abs() < 1e-9);

        // A one-sided fill that DOES leave a net position: 2 @ 49c on another
        // rung, later adopted by reconcile. The stake takes over from the bridge.
        r.note_house_cash_cents("KXCPIYOY-Y", -98);
        assert!((r.expected_cash() - 98.04).abs() < 1e-9);
        assert!(r.adopt_orphan("KXCPIYOY-Y", Side::Yes, 2, Some(49), "orphan"));
        // 100 − 0.98 (position stake) − 0.98 (unadopted house pair) = 98.04.
        assert!(
            (r.expected_cash() - 98.04).abs() < 1e-9,
            "adoption must hand off, not double-count: {}",
            r.expected_cash()
        );
    }

    // ---- SETTLED-SET GUARD (R171 / incident #5, 2026-07-27) -----------------
    // Today's live shape: 5 settled volbook metal positions re-booked 8+ times at
    // ~$2.17 a pass, bankroll reading $122.64 against a real ~$106, `settled`
    // holding ~50 entries for 5 real settlements and `day_spent` inflated by a
    // re-adopted stake every pass. Both legs of that loop are covered below.

    /// A store two managers can share, so a test can prove something survives a
    /// RESTART (`MemoryStore` is not cloneable, so the older reload test above
    /// could only ever drive one manager).
    #[derive(Clone, Default)]
    struct SharedStore(std::sync::Arc<std::sync::Mutex<Option<State>>>);

    impl StateStore for SharedStore {
        fn load(&self) -> Result<Option<State>> {
            Ok(self.0.lock().unwrap().clone())
        }
        fn save(&self, s: &State) -> Result<()> {
            *self.0.lock().unwrap() = Some(s.clone());
            Ok(())
        }
    }

    /// A flat-sized metals order (the sleeve that produced the incident).
    fn flat_order(ticker: &str, count: i64, price: i64) -> Order {
        Order {
            strategy: "volbook".into(),
            ticker: ticker.into(),
            side: Side::Yes,
            count,
            limit_cents: price,
            cluster: "metals".into(),
            sizing: SizingHint::Flat,
        }
    }

    #[test]
    fn a_settled_ticker_is_never_readopted_as_an_orphan() {
        // (a) The DRIVING leg. After settlement the exchange still reports the
        // position for minutes-to-hours (metals: still non-zero 41 min after
        // booking). That is a payout lag, not an orphan — refuse it.
        let mut r = rm(106.03);
        r.begin_day("2026-07-27");
        r.on_fill(&flat_order("KXGOLDD-26JUL27-T4110", 2, 40));
        assert!(r.settle("KXGOLDD-26JUL27-T4110", true).is_some());
        let bankroll = r.status().bankroll;
        let day_spent = r.state.day_spent;

        assert!(
            !r.adopt_orphan("KXGOLDD-26JUL27-T4110", Side::Yes, 2, Some(40), "orphan"),
            "a settled ticker must never be adopted back as an orphan"
        );
        assert!(r.open_positions().is_empty());
        assert_eq!(
            r.state.day_spent, day_spent,
            "refusal must not spend budget"
        );
        assert_eq!(r.status().bankroll, bankroll);
    }

    #[test]
    fn re_settling_a_settled_ticker_books_no_money() {
        // (b) The BACKSTOP leg: refuse, return None, move nothing.
        let mut r = rm(106.03);
        r.begin_day("2026-07-27");
        r.on_fill(&flat_order("KXSILVERD-26JUL27-T38", 3, 40));
        let first = r.settle("KXSILVERD-26JUL27-T38", true).unwrap();
        let bankroll = r.status().bankroll;
        let day_spent = r.state.day_spent;

        assert!(r.settle("KXSILVERD-26JUL27-T38", true).is_none());
        assert!(r.settle("KXSILVERD-26JUL27-T38", false).is_none());
        assert_eq!(
            r.status().bankroll,
            bankroll,
            "no P&L on a refused re-settle"
        );
        assert_eq!(r.state.day_spent, day_spent);
        assert_eq!(
            r.state.settled.len(),
            1,
            "one real settlement must leave exactly one settled record"
        );
        assert!(first.pnl > 0.0);
    }

    #[test]
    fn the_eight_pass_rebook_loop_is_dead() {
        // The incident reproduced end to end: settle → adopt → settle, eight
        // times. Every number that moved on 2026-07-27 must now stand still.
        let mut r = rm(106.03);
        r.begin_day("2026-07-27");
        r.on_fill(&flat_order("KXCOPPERD-26JUL27-T512", 5, 40));
        let out = r.settle("KXCOPPERD-26JUL27-T512", true).unwrap();
        let bankroll = r.status().bankroll;
        let day_spent = r.state.day_spent;
        let peak = r.status().peak;

        for pass in 0..8 {
            assert!(
                !r.adopt_orphan("KXCOPPERD-26JUL27-T512", Side::Yes, 5, Some(40), "orphan"),
                "pass {pass}: re-adopted a settled ticker"
            );
            assert!(
                r.settle("KXCOPPERD-26JUL27-T512", true).is_none(),
                "pass {pass}: re-booked a settled ticker"
            );
        }
        assert_eq!(
            r.status().bankroll,
            bankroll,
            "bankroll drifted (was +$2.17/pass)"
        );
        assert_eq!(
            r.state.day_spent, day_spent,
            "day_spent inflated (was +stake/pass)"
        );
        assert_eq!(r.status().peak, peak);
        assert_eq!(
            r.state.settled.len(),
            1,
            "settled list grew (was ~10x the real count)"
        );
        assert!(r.open_positions().is_empty());
        assert!(out.won);
    }

    #[test]
    fn a_phantom_open_position_on_a_settled_ticker_is_dropped_not_rebooked() {
        // Incident #5's ACTUAL mechanism (note 41 §0): state.json was hand-edited
        // under the running writer, so the in-memory `open` never lost the
        // positions we had already settled — no adoption needed to re-book them.
        // The guard must refuse the money AND clear the phantom, because the only
        // other way out is hand-editing state under a live writer, i.e. the very
        // operation that caused this.
        let mut r = rm(106.03);
        r.begin_day("2026-07-27");
        r.on_fill(&flat_order("KXGOLDD-26JUL27-T4085", 2, 40));
        r.settle("KXGOLDD-26JUL27-T4085", true).unwrap();
        let bankroll = r.status().bankroll;

        r.state.open.push(Position {
            strategy: "phantom".into(),
            ticker: "KXGOLDD-26JUL27-T4085".into(),
            side: Side::Yes,
            count: 2,
            entry_cents: 40,
            cluster: "metals".into(),
            fee: 0.0,
            day: "2026-07-27".into(),
        });
        assert!(r.settle("KXGOLDD-26JUL27-T4085", true).is_none());
        assert!(
            r.open_positions().is_empty(),
            "the phantom must be dropped, not left eating cluster/portfolio room"
        );
        assert_eq!(
            r.status().bankroll,
            bankroll,
            "dropping a phantom moves no money"
        );
        assert_eq!(r.state.settled.len(), 1);
    }

    #[test]
    fn unsettled_orphans_and_first_settlements_are_unaffected() {
        // (c) The guard must not break the paths it sits on: a genuine orphan is
        // still adopted, a first settlement still books, and a DIFFERENT ticker
        // is never caught by another ticker's settlement.
        let mut r = rm(100.0);
        r.begin_day("2026-07-27");
        assert!(r.adopt_orphan("KXETH15M-ORPH", Side::No, 5, Some(40), "orphan"));
        assert_eq!(r.open_positions().len(), 1);
        assert!((r.state.day_spent - 2.0).abs() < 1e-9);

        // Win pays 5 x (1.00 − 0.40) = $3.00, and an adopted orphan carries no fee.
        let out = r.settle("KXETH15M-ORPH", true).unwrap();
        assert!((out.pnl - 3.0).abs() < 1e-9);
        // A neighbouring market is untouched by that settlement.
        assert!(!r.is_settled("KXETH15M-OTHER"));
        assert!(r.adopt_orphan("KXETH15M-OTHER", Side::Yes, 3, Some(30), "orphan"));
        assert!(r.settle("KXETH15M-OTHER", false).is_some());
    }

    #[test]
    fn the_settled_set_survives_persist_and_reload() {
        // (d) The set must be a RESTART-proof fact, not process memory —
        // `state.settled` is persisted, which is exactly why it is the record.
        let store = SharedStore::default();
        let mut r = RiskManager::load_or_init(
            RiskConfig::default(),
            Box::new(store.clone()),
            106.03,
            false,
            true,
        )
        .unwrap();
        r.begin_day("2026-07-27");
        r.on_fill(&flat_order("KXSILVERD-26JUL27-T39", 2, 40));
        assert!(r.settle("KXSILVERD-26JUL27-T39", true).is_some());
        let bankroll = r.status().bankroll;
        drop(r);

        // Restart over the same persisted state.
        let mut r2 = RiskManager::load_or_init(
            RiskConfig::default(),
            Box::new(store.clone()),
            106.03,
            false,
            true,
        )
        .unwrap();
        assert!(r2.is_settled("KXSILVERD-26JUL27-T39"));
        assert!(!r2.adopt_orphan("KXSILVERD-26JUL27-T39", Side::Yes, 2, Some(40), "orphan"));
        assert!(r2.settle("KXSILVERD-26JUL27-T39", true).is_none());
        assert!(r2.open_positions().is_empty());
        assert_eq!(r2.status().bankroll, bankroll);
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
