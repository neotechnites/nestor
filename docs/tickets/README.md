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
| [T009](T009-lock-sleeve.md) | Lock sleeve | — | **PARKED: decay-dead** (kill-scan +1.72¢→−1.07¢) | redirect |
| [T010](T010-consume-biases.md) | Weather sleeve consumes calibrated biases + season-aware city filter | P0 | done | T003 |
| [T011](T011-fill-verification.md) | Verify actual fills before recording positions | P0 | done (schema check at selftest) | — |
| [T012](T012-streak-sleeve.md) | **Streak ≤44¢ (BUILD #1, redirect 2026-07-23)** | P0 | built (paper; fill-verify + fees + data-capture + 1s cadence per updated redirect) | keys for live |

**⚠️ REDIRECT 2026-07-23:** the vault redirect file (implementation/03) supersedes any
conflicting plan here: lock=parked (decay-dead), weather=parked (unverdicted), streak=BUILD #1.

**Repo state:** on personal GitHub (`neotechnites/nestor`), bound via a dedicated
SSH key. T001–T005 merged to `master` (foundational batch — pushed without PR review
per Ryan; future work gets PRs). Later tickets build on stacked branches → PRs.

**Needs Ryan:** Kalshi API keys + funded account (T007), VPS provider pick (T008).
Everything else is buildable now with no account.
