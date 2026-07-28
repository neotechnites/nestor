# lip_maker_v4 — deploy (spec-lip-maker-v1)
Python 3.12 + requests + cryptography. Tests: `python3 -m unittest discover tools/lip_maker_v4/` (67, no network).

## Deploy
    scp tools/lip_maker_v4/lip_maker_v4.py      ubuntu@VPS:~/kalshi_data/scripts/
    scp tools/lip_maker_v4/lip-maker-v4.service ubuntu@VPS:/tmp/
    ssh ubuntu@VPS 'sudo mv /tmp/lip-maker-v4.service /etc/systemd/system/ && sudo systemctl daemon-reload'
    ssh ubuntu@VPS 'mkdir -p ~/nestor/data/lip && python3 ~/kalshi_data/scripts/lip_maker_v4.py --check'

## First-run checklist (all must pass before --live)
0. **`python3 -m unittest discover tools/lip_maker_v4/` — 84 tests, must print `OK`.** Run it on the
   VPS against the file you just scp'd, not only on the Mac.
1. `--check` prints `[OK ]` for all four startup assertions: `env_and_pem`, `data_dir_writable`,
   `ledger_replay_clean`, and **`unit_assertion_KXAAAGASD_eq_100.00`** — a live gas rung must read
   `$100.00 ± $0.01`; the process REFUSES TO RUN on failure (§0.3).
2. `~/nestor/.env` has `KALSHI_API_KEY_ID`; `~/nestor/secrets/prod.pem` exists. Config read-back: `MAX_TOTAL_COLLATERAL_USD = 45.0`, `EVENT_ALLOWLIST = []` (OFF — scanner ranks
   everything; set to e.g. `["KXAAAGASD-26JUL29"]` to pen the shakeout to gas), `COID_PREFIX = "v4-"`.
3. `lip_maker_v4.py --dry` — one cycle, zero POST/DELETE; inspect the `allocate` line.
4. Start it, then **read the first `classify_sweep_done` and `allocate` lines** — this check catches
   the class of defect that shipped a poll budget onto rungs that can never pay:
       sudo systemctl enable --now lip-maker-v4
       journalctl -u lip-maker-v4 -f | grep -E "classify_sweep_done|allocate|day_stop"
   HEALTHY: `classify_sweep_done {"n_classified":N,"pinned":P,"chosen":[...6 tickers...]}` where NO
   chosen ticker is a pinned rung and `chosen_values` is descending and non-zero; then
   `allocate {"budget":B,"spent":S,"dropped":[],"alloc":{...}}` with `0 < S <= B`, `B < 45.00`
   (the §2.4 reserve), and every allocated ticker present in `chosen`.
   ABORT if: `chosen` is empty, any chosen ticker is pinned, `spent` is 0 for more than a few
   cycles, `budget` equals 45.00 (reserve not applied), or `day_stop_breached` appears.

## The $1 make-before-break pair test (§4.2a/§15.6) — run BOTH halves
**A. Normal (free balance).** Proves the exchange PERMITS overlapping same-side orders. On one live
1¢ side: POST order #1 (`count "1.00"`, `good_till_canceled`, `expiration_ts`, `taker_at_cross`) to
`/portfolio/events/orders`; record `order_id`. WITHOUT cancelling, POST #2 at the same price/side.
PASS = two order_ids coexist, no margin reject, then `DELETE /portfolio/events/orders/{id}` on both
returns 200 with `reduced_by`.
**B. At zero free balance (fully deployed).** Proves the reject path degrades cleanly. Deploy to the
ceiling, repeat A. The make leg must be REJECTED, the ledger must log `mbb_degraded` for that slot,
and the slot must still hold a resting order afterwards (cancel-first at 46s). A silent presence
drop is a FAIL and blocks deploy.

Ledger `~/nestor/data/lip/v4_ledger.jsonl` · alerts ntfy `senate-nestor-2732e947` ·
stop `sudo systemctl stop lip-maker-v4` (SIGTERM cancels all; `expiration_ts` = close−4min backstop).
