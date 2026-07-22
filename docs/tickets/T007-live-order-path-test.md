# T007 — Live $1 order-path test

**Priority:** P1 · **Status:** built (awaits live run with keys) · **Gated on:** Kalshi API keys + funded account (Ryan)

## Goal
Prove the RSA signing + order placement actually works end-to-end against real
Kalshi, at trivial risk, before any strategy trades live.

## Scope
- `nestor selftest-order`: authenticate, read balance, place ONE limit order for
  ~$1 on a liquid market, confirm the fill, then confirm settlement + reconcile.
- Verify signing (timestamp+method+path, PSS/SHA-256, salt=digest len) is accepted.

## Done when
- A real $1 round-trip completes: placed → filled → settled → P&L reconciled,
  with the response logged. Confirms auth + order path for all future live trades.
