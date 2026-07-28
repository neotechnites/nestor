# lip_maker_v4 — deploy (spec-lip-maker-v1)
Python 3.12 + requests + cryptography. Tests: `python3 -m unittest discover tools/lip_maker_v4/` (67, no network).

## Deploy
    scp tools/lip_maker_v4/lip_maker_v4.py      ubuntu@VPS:~/kalshi_data/scripts/
    scp tools/lip_maker_v4/lip-maker-v4.service ubuntu@VPS:/tmp/
    ssh ubuntu@VPS 'sudo mv /tmp/lip-maker-v4.service /etc/systemd/system/ && sudo systemctl daemon-reload'
    ssh ubuntu@VPS 'mkdir -p ~/nestor/data/lip && python3 ~/kalshi_data/scripts/lip_maker_v4.py --check'

## First-run checklist (all must pass before --live)
0. **`python3 -m unittest discover tools/lip_maker_v4/` — 299 tests, must print `OK`.** Run it on the
   VPS against the file you just scp'd, not only on the Mac.
1. `--check` prints `[OK ]` for all four startup assertions: `env_and_pem`, `data_dir_writable`,
   `ledger_replay_clean`, `taker_exit_decision_matches_ceiling`, and **`unit_assertion_eq_100.00`** — at least 30
   live programs must read `$100.00 ± $0.01` (the modal pool; ~585 of 771 do). Gas, when live, is an
   extra belt. The process REFUSES TO RUN on failure (§0.3).
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

## Websocket (SEPARATE deploy, mid-morning — never with a capital raise)
`ws_feed.py` ships inert: `WS_ENABLED = False` in lip_maker_v4.py. To turn it on:
    ssh ubuntu@VPS 'pip3 install --user websockets'      # the one authorized extra dependency
    scp tools/lip_maker_v4/ws_feed.py ubuntu@VPS:~/kalshi_data/scripts/
    # then set WS_ENABLED = True and restart
A ws book may drive quoting only after 3 consecutive agreements with a REST poll of the same
market (`ws_gate_passed`), re-proven every 60s, and reverts to REST + alerts on ANY divergence
(`ws_divergence`; a dollars/cents slip names itself as `unit_mismatch`).
Breadth lifts from 6 to 32 markets ONLY while the socket is connected; any market whose WS
book is missing/stale/gapped/corrupt falls back to its REST poll. `grep ws_ ~/nestor/data/lip/
v4_ledger.jsonl` for connect/gap/resubscribe events. Roll back = set the flag False + restart.

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

Inventory does not self-clear beyond the maker shed (`TAKER_EXIT_ENABLED=False`); at a $300 ceiling the
startup assertion refuses to run until that decision is made explicitly.
**Position divergence.** v4 reconciles against `GET /portfolio/positions` at startup and every
600s. If the exchange and the ledger disagree on a market v4 has touched, it FREEZES that market
(quoting and recycling) and pages with both numbers. There is deliberately no auto-import — the
endpoint has no cost basis. Check the real position, fix the cause, then `--clear-freeze TICKER`.

**Frozen market (§9.4b `assume_filled`)** — v4 froze quoting AND recycling on a market because a
fill was unverifiable. Reconcile the position by hand on the exchange, then:
    python3 ~/kalshi_data/scripts/lip_maker_v4.py --clear-freeze KXXXX-26JUL28-4.100
    sudo systemctl restart lip-maker-v4
Never clear it without checking the real position first — the freeze exists because the ledger and
the exchange disagreed.

**Credits ritual (daily, 16:00 MT).** v4 alerts `CREDITS RITUAL DUE` when programs have
flipped `paid_out` with no credit row. Append them to `pools_operator.jsonl` the same evening:
`{"ts":"...","kind":"credit","program_id":"<id>","paid_usd":5.40,"date":"YYYY-MM-DD"}`.
**Two days without credits HALTS deployment (§12.3b)** — the reminder exists so that halt
never surprises anyone.

Ledger `~/nestor/data/lip/v4_ledger.jsonl` · alerts ntfy `senate-nestor-2732e947` ·
stop `sudo systemctl stop lip-maker-v4` (SIGTERM cancels all; `expiration_ts` = close−4min backstop).
