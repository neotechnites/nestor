# Spec 01 — Risk layer

The gap flagged 2026-07. Implements the vault's sizing doctrine (notes 09/12) as
code every order routes through. No strategy sizes its own bets.

## Responsibilities
1. **Bankroll tracking** — current equity, updated as positions settle.
2. **Sizing** — turn a strategy `Signal` into an approved `Order` (contract count)
   or a rejection, per configured rules.
3. **Position tracking** — what's open now, grouped into correlation *clusters*.
4. **Cluster caps** — total at-risk in one cluster ≤ a % of bankroll.
5. **Kill-switch** — halt new orders on drawdown / daily-loss breach.
6. **Persistence** — bankroll + open positions + settled history survive restarts.

## Types (engine::risk)
```rust
pub struct Signal {
    pub strategy: String,
    pub ticker: String,
    pub side: Side,              // Yes | No
    pub limit_cents: i64,        // price we'd pay
    pub cluster: String,         // correlation key (see below)
    pub sizing: SizingHint,      // Flat($) | Fraction(f)  — strategy's intent
}
pub struct Order { pub ticker: String, pub side: Side, pub count: i64, pub limit_cents: i64 }
pub enum Rejection { Halted, DailyCapHit, ClusterCapHit, BankrollTooLow, PriceOutOfBand, ZeroSize }

pub struct RiskManager { /* config + handle to StateStore */ }
impl RiskManager {
    pub fn evaluate(&mut self, s: &Signal) -> Result<Order, Rejection>;
    pub fn on_fill(&mut self, order: &Order);            // record open position
    pub fn on_settlement(&mut self, ticker: &str, won: bool); // realize P&L, update bankroll
    pub fn status(&self) -> RiskStatus;                  // equity, peak, drawdown, halted
}
```

## Sizing rules (config-driven)
- **Fraction mode** (lock, most crypto): stake = `fraction × current_bankroll`,
  clamped so the *cluster* total ≤ `cluster_cap_frac × bankroll`. Default
  fraction 0.05, cluster_cap 0.15 (note 12).
- **Flat mode** (weather, thin markets): stake = `flat_usd`, capped by remaining
  daily budget. Default $10/trade, $80/day.
- `count = floor(stake / (limit_cents/100))`. Reject if `count == 0` or price ∉ (2,98)¢.

## Cluster key
Groups positions that would move together (a crash flips them at once):
- Crypto 15-min: `"<asset>:<close_ts_rounded_to_window>"` → all 5 assets in the
  same 15-min window share a cluster.
- Weather: `"weather:<date>"` → a day's city trades are one cluster.
Cluster cap prevents one correlated event from exceeding the configured bankroll %.

## Kill-switch
Halt (reject all with `Halted`) when either:
- drawdown from peak equity ≥ `max_drawdown_frac` (default 0.30), or
- realized loss today ≥ `daily_loss_limit_frac × bankroll` (default 0.15).
Halt persists across restarts until manually cleared (a `resume` command / flag).

## Persistence (StateStore)
- v1: a JSON file (`data/state.json`): `{ bankroll, peak, halted, day, day_loss,
  open: [Position], settled: [...] }`. Atomic write (temp + rename).
- Upgrade path: SQLite if concurrency/scale needs it. Keep behind a trait so the
  swap is local.

## Integration
- Weather migrates from its inline flat-stake + daily cap to `RiskManager.evaluate`.
- The Engine gains a `RiskManager` (constructed from config); `Engine.execute(order)`
  places (live) or logs (paper) and calls `on_fill`.
- Settlement/reconcile (separate ticket) calls `on_settlement` to close the loop.

## Tests (mandatory — money code)
- Fraction sizing: bankroll 1000, f=0.05, price 95¢ → count = floor(50/0.95)=52.
- Cluster cap: three signals in one cluster stop at 15% of bankroll.
- Flat/daily cap: 9th $10 trade rejected once $80 spent.
- Kill-switch: drawdown 30% → subsequent evaluate = Halted; persists reload.
- Round-trip: on_fill then on_settlement(won/lost) moves bankroll by the right amount.
