# T002 — TOML config

**Priority:** P0 · **Status:** built-local (PR pending personal remote)

## Goal
Move runtime config out of env/hardcode into a versioned `nestor.toml` (with env
overrides for secrets/mode). Covers: mode, per-strategy sizing, risk params
(fraction, cluster_cap, max_drawdown, daily_loss_limit), city table + biases.

## Scope
- `engine::config` reads `nestor.toml` (serde). Secrets stay in env/.env.
- City table + calibrated biases live here (T003 writes biases back).
- `nestor.toml.example` committed; real `nestor.toml` optional override.

## Done when
- Binary loads config from TOML; tests cover parsing + defaults; fmt/clippy clean.
