# Nestor tickets (board)

One `.md` file per ticket. Status: `todo` | `in-progress` | `review` (PR open) | `done`.
See [../WORKFLOW.md](../WORKFLOW.md). Priority: P0 (now) → P2 (later/gated).

| ID | Title | Pri | Status | Gated on |
|----|-------|-----|--------|----------|
| [T001](T001-risk-layer.md) | Risk layer + persistent state store | P0 | done | — |
| [T002](T002-config-file.md) | TOML config (mode, sizing, risk, cities) | P0 | done | — |
| [T003](T003-bias-calibration.md) | Bias calibration job (the edge) | P0 | done | — |
| [T004](T004-settlement-reconcile.md) | Settlement / reconcile loop + P&L | P0 | done | T001 |
| [T005](T005-verify-tickers-stations.md) | Verify Kalshi series tickers + IEM stations | P1 | done | — |
| [T006](T006-ci-on-prs.md) | CI on PRs + test scaffolding | P0 | todo | — |
| [T007](T007-live-order-path-test.md) | Live $1 order-path test | P1 | built (awaits keys) | account |
| [T008](T008-vps-provisioning.md) | VPS provisioning + systemd timer + deploy secrets | P1 | VPS pick | — |
| [T009](T009-lock-sleeve.md) | Lock sleeve (always-on BTC favorite fade) | P2 | epic | T001,T004 |
| [T010](T010-consume-biases.md) | Weather sleeve consumes calibrated biases + season-aware city filter | P0 | done | T003 |

**Repo state:** on personal GitHub (`neotechnites/nestor`), bound via a dedicated
SSH key. T001–T005 merged to `master` (foundational batch — pushed without PR review
per Ryan; future work gets PRs). Later tickets build on stacked branches → PRs.

**Needs Ryan:** Kalshi API keys + funded account (T007), VPS provider pick (T008).
Everything else is buildable now with no account.
