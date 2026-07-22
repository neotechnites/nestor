# Nestor development workflow

Spec-driven. Claude executes autonomously; Ryan is the final merge gate.

## Roles
- **Ryan:** approves spec direction, does the final review + merge of PRs on GitHub.
  Cannot read Rust deeply — so does not rely on line-by-line review for correctness.
- **Claude:** writes specs (proposing decisions for Ryan), turns specs into tickets,
  implements tickets on branches, self-reviews, opens PRs. Must land code that is
  correct *without* relying on Ryan's review.

## The loop
1. **Spec** — a doc in `docs/specs/`. Source of truth for a decision or component.
2. **Tickets** — `docs/tickets/`, one file per ticket + `README.md` board. Each
   ticket is small, testable, and links to its spec.
3. **Branch** — one branch per ticket: `feat/<id>-<slug>` (or `fix/…`, `chore/…`).
4. **Self-review gate** (must all pass before opening a PR):
   - `cargo fmt --check`
   - `cargo clippy -- -D warnings`
   - `cargo test`
   - `cargo build --release`
   - a Claude code-review pass over the diff (correctness + simplicity)
   - the strategy still runs end-to-end in paper mode where relevant
5. **PR** — opened via `gh`, body links the ticket + spec, lists what was verified.
   CI (`.github/workflows/ci.yml`) re-runs the gate on the PR.
6. **Merge** — Ryan reviews + merges on GitHub. Squash-merge to main.
7. Ticket marked `done` in its file + the board; branch deleted.

## Correctness bar (since Ryan can't verify Rust)
- Every non-trivial module has unit tests for its logic (sizing, risk, bucket
  mapping, bias math, date codes).
- Anything touching money is tested against explicit numeric cases.
- Live/external calls are isolated behind functions that can be tested with
  fixtures; paper mode never places orders.
- CI is the objective gate. Red CI = not mergeable, full stop.

## State lives on disk, not in Claude's memory
Specs + tickets + code are the durable brain. Claude's context resets between
sessions; the board tells the next session exactly what's done and what's next.
