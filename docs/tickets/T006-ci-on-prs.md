# T006 — CI on PRs + test scaffolding

**Priority:** P0 · **Status:** todo

## Goal
Gate every PR on the self-review checks so "green CI" is the objective merge bar
(Ryan can't verify Rust by eye).

## Scope
- `.github/workflows/ci.yml` on `pull_request` + pushes to non-main branches:
  `cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo test`, `cargo build`.
- Rust cache for speed. Keep deploy.yml (main → ship to VPS) separate.
- Confirm the runner builds the workspace with rustls (no OpenSSL).

## Done when
- Opening a PR triggers the full gate; a fmt/clippy/test failure blocks merge.
