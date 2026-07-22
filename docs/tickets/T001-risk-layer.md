# T001 — Risk layer + persistent state store

**Priority:** P0 · **Status:** in-progress · **Spec:** [01-risk-layer](../specs/01-risk-layer.md)

## Goal
Add `engine::risk` (RiskManager) + a persistent StateStore so all orders route
through global bankroll/sizing/cluster/kill-switch logic. Migrate weather off its
inline flat-stake + daily cap.

## Scope
- `Signal`, `Order`, `Side`, `Rejection`, `SizingHint`, `RiskConfig` types.
- `RiskManager::{evaluate, on_fill, on_settlement, status}`.
- `StateStore` trait + JSON impl (atomic write) → `data/state.json`.
- Cluster keying (crypto window / weather-day).
- Kill-switch (drawdown + daily-loss), persists halted across restart.
- Weather emits `Signal`s; engine gains `execute(order)` (paper logs / live places).
- Unit tests per the spec's test list.

## Done when
- All spec tests pass; `cargo clippy -D warnings` + `fmt` clean.
- Weather runs in paper mode routing through RiskManager (bankroll/day cap enforced).
- No behavior regression in the paper run.
