# Nestor tickets (board)

One `.md` file per ticket. Status: `todo` | `in-progress` | `review` (PR open) | `done`.
See [../WORKFLOW.md](../WORKFLOW.md). Priority: P0 (now) → P2 (later/gated).

| ID | Title | Pri | Status | Gated on |
|----|-------|-----|--------|----------|
| [T001](T001-risk-layer.md) | Risk layer + persistent state store | P0 | built-local | — |
| [T002](T002-config-file.md) | TOML config (mode, sizing, risk, cities) | P0 | built-local | — |
| [T003](T003-bias-calibration.md) | Bias calibration job (the edge) | P0 | built-local | — |
| [T004](T004-settlement-reconcile.md) | Settlement / reconcile loop + P&L | P0 | todo | T001 |
| [T005](T005-verify-tickers-stations.md) | Verify Kalshi series tickers + IEM stations | P1 | todo | — |
| [T006](T006-ci-on-prs.md) | CI on PRs + test scaffolding | P0 | todo | — |
| [T007](T007-live-order-path-test.md) | Live $1 order-path test | P1 | Kalshi keys | account |
| [T008](T008-vps-provisioning.md) | VPS provisioning + systemd timer + deploy secrets | P1 | VPS pick | — |
| [T009](T009-lock-sleeve.md) | Lock sleeve (always-on BTC favorite fade) | P2 | epic | T001,T004 |

**Repo state:** no remote yet (the personal GitHub account isn't set up — the earlier
company repo was deleted). `built-local` tickets are done + self-reviewed on stacked
branches; they'll be pushed and opened as PRs (in order) once a personal GitHub exists.

**Needs Ryan:** personal GitHub account (to push + open PRs), Kalshi API keys + funded
account (T007), VPS provider pick (T008). Everything else is buildable now with no account.
