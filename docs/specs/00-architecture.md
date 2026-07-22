# Spec 00 — Architecture

Nestor is an ever-growing, multi-strategy trading platform (the first Olympus
project). One Rust binary, many strategies, shared infrastructure.

## Layers
```
Strategy layer   weather (built), lock/gold/… (future) — each a crate impl'ing Strategy
Risk layer       bankroll, sizing, cluster caps, kill-switch — ALL orders route here
Engine layer     Kalshi client (+signing), data feeds, logging, scheduler, Strategy trait
Runtime          the `nestor` binary (async, tokio)
OS / scheduling  Linux + cron/systemd timer (on the VPS)
Cloud host       the VPS (always-on)
Source / CI-CD   GitHub repo + Actions (fmt/clippy/test → ship binary to VPS)
External         Kalshi API · Open-Meteo · IEM · Coinbase (lock)
```

## Core principles
1. **Strategies never size or place raw orders.** They emit a *signal* (what they
   want to buy/sell + confidence/context). The **Risk layer** decides size (or
   refuses), and the engine executes. This is what makes bankroll management
   global instead of per-strategy.
2. **Paper vs live is a mode, not a code path.** Same logic; live places orders,
   paper logs them. No strategy hardcodes order placement.
3. **State is persisted** (bankroll, open positions, settled P&L) so restarts and
   new sessions are consistent. Start with a JSON/SQLite store on the box.
4. **New strategy = new crate implementing `Strategy`**, wired into `nestor_bin`.
   No engine changes required to add an edge.

## Signal → order flow (target)
```
Strategy.run()
  └─ builds Signal { ticker, side, edge_context }   (no size)
      └─ RiskManager.evaluate(signal) -> Option<Order { ticker, side, count, limit }>
          ├─ None  → logged as "rejected: <reason>" (cap hit / halted / too thin)
          └─ Some  → Engine executes (live) or logs (paper); position recorded
```

## Current vs target (2026-07)
- Built: engine (Kalshi client + signing, feeds, logging), weather strategy, binary.
- **Missing: the Risk layer and the persistent state store** — see
  [01-risk-layer](01-risk-layer.md). Today weather uses a flat stake + a naive
  per-run daily-$ cap and tracks no bankroll. That is the top priority.
- Also missing (tickets): bias calibration, settlement/reconcile, ticker/station
  verification, config file, live order-path test, VPS provisioning, lock sleeve.
