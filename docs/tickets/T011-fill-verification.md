# T011 — Verify actual fills before recording positions

**Priority:** P0 · **Status:** DONE (2026-07-23, redirect EXECUTION TRUTH) — implemented in Engine::execute: fills polled after placement, actual price/count/timestamps recorded, partial fills feed risk with filled count only, unfilled remainder canceled at deadline, paper marks simulated:true. Fill-schema parsing is tolerant (cents/dollars, id variants) and unit-tested; `selftest-order` prints raw fills JSON to confirm the real schema at the $1 test.

## Problem (from the full-repo review)
`Engine::execute` records `on_fill(&order)` on a successful `place_limit_buy`
HTTP 200 — i.e. it assumes the order **fully filled at the limit price**. A
limit order can rest unfilled or partially fill on a thin weather bucket. Then
the ledger holds a position that doesn't match reality, and `reconcile` would
later book P&L on a bet not actually held.

## Why it's deferred (not a blind fix)
The correct fix reads the ACTUAL fill (filled count + avg price) from Kalshi's
order/fills response — whose exact schema we can't confirm until a real order
runs. `nestor selftest-order` (T007) is that first real order; capture its
response shape, then implement against it. Fixing it blind risks guessing the
JSON wrong.

## Scope (after T007 shows the schema)
- After `place_limit_buy`, read the order status / `/portfolio/fills` and record
  the real filled count + price (not the requested order). Handle: fully filled,
  partial, resting-unfilled (record nothing or a pending marker), and cancel a
  stale resting order if desired.
- `reconcile` must only settle positions that actually filled.
- Tests against the captured real response fixtures.

## Interim safety
Fine at $10 while hand-watching fills in the Kalshi UI. **Must be done before the
VPS runs weather unattended, and before the lock edge (last-2-min fills, no time
to watch).**
