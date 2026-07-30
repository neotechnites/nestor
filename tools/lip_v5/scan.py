"""
lip_v5.scan — programs feed → classify sweep → SLOT TABLE.

This is the front of the cycle: it turns "what exists on the exchange" into the `slots` argument
`engine.cycle()` consumes.  Three stages, each on its own cadence and each drawing from the same
rate budget:

    scan      (SCAN_REFRESH_S)     the LIP programs feed — pools, windows, series
    classify  (CLASSIFY_REFRESH_S) per-market book reads: pinned? qualifies? S? p?
    build     (every cycle)        slots, from the two above plus our own tape

**Why classify is a separate, slower stage** (v4's B1 fix, kept on merits): ranking by ρ alone is
DEGENERATE INSIDE ONE EVENT — every rung of a gas daily carries the identical `period_reward` and
the identical window, so ρ cannot separate them and the ticker tie-break decides.  Measured live:
a ρ-ranked clamp picked six deep-ITM rungs of which FOUR were pinned and could never pay, and
never polled the three best slots on the board.  So: classify first (cheap, low cadence, learns
pinned/qualifies/S/p), then rank by the ALLOCATOR'S OWN first-dollar rate.

Pinned-ness changes only when a 99c/1c tick-boundary order moves, which is a 15-minute
timescale — far slower than the 1 Hz quoting loop.  That is the cadence's derivation, and it is
also why `classify` is degrade step 1: it is the cheapest requests to give up.
"""

import json
import os

from . import alloc, clusters as CL, config as C, money as M, presence as P
from . import runtime as R


# =============================================================================================
# STAGE 1 — the programs feed.
# =============================================================================================
def parse_iso(s):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def pool_usd(period_reward):
    """spec §0.2 — `pool_usd = period_reward × 1e-4`.  A wrong unit is a 10x or 10,000x sizing
    error, which is why `lip_v5.unit_assertion_check` refuses to run on a mismatch."""
    return float(period_reward or 0.0) * C.PERIOD_REWARD_UNIT_USD


def window_hours(start_ts, end_ts):
    if start_ts is None or end_ts is None:
        return None
    h = (float(end_ts) - float(start_ts)) / 3600.0
    return h if h > 0 else None


def pool_rate(period_reward, window_h):
    """`ρ = pool_usd / window_hours` ($/h).  Program periods are MULTI-DAY (modal 228 h), so
    every window quantity is a FRACTION of that program's own [start_date, end_date] — never a
    calendar day (spec §0.2)."""
    if not window_h or window_h <= 0:
        return 0.0
    return pool_usd(period_reward) / float(window_h)


def parse_programs(body):
    """The LIP programs feed → normalized program dicts.  Unparseable rows are DROPPED, loudly:
    a program we cannot read is a program we must not size against."""
    out = []
    rows = ((body or {}).get("incentive_programs")
            or (body or {}).get("liquidity_incentive_programs")
            or (body or {}).get("programs") or [])
    for row in rows:
        try:
            start = parse_iso(row.get("start_date"))
            end = parse_iso(row.get("end_date"))
            wh = window_hours(start, end)
            if wh is None:
                R.log("program_unparseable", program_id=row.get("id"))
                continue
            out.append({
                "program_id": row.get("id") or row.get("program_id"),
                "series": row.get("series_ticker") or row.get("series"),
                "tickers": list(row.get("market_tickers") or ([row["market_ticker"]]
                                                              if row.get("market_ticker")
                                                              else [])),
                "period_reward": row.get("period_reward"),
                "start_ts": start, "end_ts": end, "window_h": wh,
                "rho": pool_rate(row.get("period_reward"), wh),
                "target_size": float(row.get("target_size_fp") or row.get("target_size")
                                     or 1000.0),
                "paid_out": bool(row.get("paid_out")),
            })
        except Exception as exc:                              # noqa: BLE001 - row is untrusted
            R.log("program_unparseable", err="%s: %s" % (type(exc).__name__, exc))
    return out


class Scanner(object):
    """Cadence-gated programs pull.  Every request goes through the rate bucket's
    `classify_sweep` lane — the LOWEST priority, because the programs feed is the most
    deferrable thing we read: pools re-price on a multi-day timescale."""

    def __init__(self, refresh_s=C.SCAN_REFRESH_S):
        self.refresh_s = float(refresh_s)
        self.last_scan = None
        self.programs = []

    def due(self, now):
        return self.last_scan is None or (float(now) - self.last_scan) >= self.refresh_s

    def scan(self, ex, bucket, now, max_pages=C.SCAN_MAX_PAGES):
        if not self.due(now):
            return self.programs
        pages, cursor, collected = 0, None, []
        while pages < int(max_pages):
            ok, _ = bucket.admit("classify_sweep", now)
            if not ok:
                # Out of budget mid-scan: keep what we have.  A PARTIAL program table is
                # correct-but-narrow (we simply see fewer venues); a stale one is also fine,
                # since pools move on a multi-day timescale.  Neither is wrong, so neither halts.
                R.log("scan_deferred", pages=pages)
                break
            status, body = ex.programs(cursor)
            if status == 429:
                bucket.on_429(now)            # SF-2: a 429 yields, whoever it belonged to
            if status != 200:
                R.log("scan_failed", status=status)
                break
            collected.extend(parse_programs(body))
            cursor = (body or {}).get("next_cursor") or (body or {}).get("cursor")
            pages += 1
            if not cursor:
                break
        if collected:
            self.programs = collected
            self.last_scan = float(now)
        return self.programs


# =============================================================================================
# STAGE 2 — the classify sweep.
# =============================================================================================
class Classifier:
    """Per-market: pinned? does each side qualify?  S and p per side.

    Cadence `CLASSIFY_REFRESH_S`; rate `CLASSIFY_HZ`, degrading to `CLASSIFY_HZ_DEGRADED` as
    §3.4 step 1.  Bounded to `CLASSIFY_MAX_MARKETS` by ρ, which DOES rank across events (they
    have different pools) — it only fails WITHIN one, which is precisely why the rank that
    matters is computed after this stage, not before it.
    """

    def __init__(self, refresh_s=C.CLASSIFY_REFRESH_S, max_markets=C.CLASSIFY_MAX_MARKETS):
        self.refresh_s = float(refresh_s)
        self.max_markets = int(max_markets)
        self.table = {}                      # ticker -> classification
        self.last = {}                       # ticker -> ts
        # REAL market close (charter B): a market's settlement close is FIXED at listing, so
        # one fetch per ticker suffices — cached forever, never re-spent.
        # ── PERSISTED (2026-07-30 morning).  "Forever" was one process long: the cache was
        # memory-only, so every restart re-learned THOUSANDS of closes at ≤4 req/s — the
        # measured hour-long ramp on each of tonight's three restarts, most of it spent
        # re-discovering that the same far-settling markets are still far.  Closes are fixed
        # at listing; the cache is append-only truth and loads in one read.
        self.close_ts = {}                   # ticker -> epoch s (market close, NOT program end)
        self.close_missing = set()           # tickers whose market object carried no close
        self._close_cache_path = os.path.join(C.DATA_DIR, "v5_close_cache.json")
        self._close_dirty = 0
        try:
            with open(self._close_cache_path) as f:
                obj = json.load(f)
            self.close_ts.update({str(k): float(v) for k, v in
                                  (obj.get("close_ts") or {}).items()})
            self.close_missing.update(obj.get("missing") or ())
        except Exception:
            pass                             # no cache yet: first run learns it
        # P6 — public-trade-tape existence, per ticker, re-checked at P6_RECHECK_S.
        self.p6 = {}                         # ticker -> bool (True = someone trades here)
        self.p6_ts = {}                      # ticker -> last check ts

    def _persist_closes(self, every=25):
        """Write-behind: one atomic dump per `every` new closes — cheap against the read
        it saves (each entry is one REST call a restart never re-spends)."""
        self._close_dirty += 1
        if self._close_dirty < int(every):
            return
        self._close_dirty = 0
        try:
            tmp = self._close_cache_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"close_ts": self.close_ts,
                           "missing": sorted(self.close_missing)}, f)
            os.replace(tmp, self._close_cache_path)
        except Exception:
            pass                             # cache is an optimization, never a halt

    def due(self, ticker, now):
        t = self.last.get(ticker)
        return t is None or (float(now) - t) >= self.refresh_s

    def candidates(self, programs, now):
        """Top-N markets by ρ, with EVERY request-free exclusion applied first.

        Denied series were already excluded here.  The window ones were not, and that was the
        whole classify budget: measured live at G2, 4,517 of 7,000 programs had ALREADY ENDED,
        and because a dead program keeps its ρ (pool ÷ its own window) the ranking is dominated
        by windows that closed — 200 markets classified, ZERO slots, because `build_slots` then
        drops every one of them on `hours_left <= 0`.  We paid ~200 requests per sweep to learn
        the shape of books we can never be paid for.

        The exclusions here must be exactly those `build_slots` applies for free, or the budget
        is spent on markets the next stage discards: ended, not-yet-open (the pre-position
        guard), and unreachable-floor (the runway guard).
        """
        rows = []
        for prog in programs:
            hours_left = (float(prog["end_ts"]) - float(now)) / 3600.0
            if hours_left <= 0:
                continue                       # the window is over: nothing here can be earned
            hours_to_start = max(0.0, (float(prog["start_ts"]) - float(now)) / 3600.0)
            if not preposition_ok(hours_to_start):
                continue                       # not open yet — a pre-start quote earns zero
            # ── THE WINDOW FILTER IS GONE (note 52 D4, 2026-07-29 night). ────────────────
            # `MAX_WINDOW_MULT × PAYOUT_HORIZON_H` (48h) stood here and it was the mechanism
            # of the correlation disaster: MEASURED, it left ELEVEN eligible clusters (TWO at
            # 24h — gas and the treasury curve), so $300 of "diversification" was two settle
            # sources.  The horizon that matters is the MARKET'S SETTLEMENT, gated per ticker
            # below and hard-gated in `build_slots` — a program's window length carries no
            # information about how long a fill traps capital (KXGDPYEAR-32: a 123.9h program
            # on a market settling in 2032).
            # NO RUNWAY CHECK HERE, deliberately.  Accrual is per (market, side) and unknown
            # until `build_slots`; a program sitting at $0.87 needs only $0.23 more to clear the
            # cliff, so ANY from-scratch floor applied here would starve the rescue of exactly
            # the programs it exists to save (BLOCKER-2's mirror, one stage earlier — the test
            # `test_venue_floor_uses_the_rescue_target_not_the_entry_floor` catches it).  The
            # two exclusions above are accrual-INDEPENDENT and unambiguous, and they are the
            # ones that were costing the whole budget.
            for tk in prog["tickers"]:
                if C.series_denied(tk):
                    continue
                # Settlement pre-filter, REQUEST-FREE: once `learn_close` has cached a
                # ticker's market close, a far-settling ticker stops consuming classify
                # budget (book reads every 15 min, forever, on markets `build_slots` will
                # always refuse).  Unknown closes stay IN — the sweep is what learns them;
                # the hard gate at slot-build refuses entry until the close is known.
                known_close = self.close_ts.get(tk)
                if known_close is not None and \
                        (float(known_close) - float(now)) / 3600.0 > C.SETTLE_HORIZON_H:
                    continue
                rows.append((prog["rho"], tk, prog))
        # RANK BY CLUSTER DIVERSITY, NOT BY RAW POOL.  Sorting on rho alone loads the whole
        # classify budget onto the biggest cluster: measured live, the top of the board is
        # treasury rungs, all five tenors are ONE cluster sharing ONE $75 cap, and the sweep
        # kept surfacing the tenth rung of a cluster that was already full while never
        # discovering a second underlying.  Capital is capped PER CLUSTER, so a new cluster is
        # worth a whole fresh cap and the eleventh rung of an existing one is worth nothing.
        #
        # Round-robin: take each cluster's best market in turn, then each cluster's second,
        # and so on.  Within a cluster the order is still rho-descending, so the ranking that
        # matters inside a cluster is unchanged — what changes is that breadth is discovered
        # FIRST rather than after 200 rungs of one ladder.
        #
        # MIRROR (diversity ↔ concentration): this does not spread CAPITAL thin — the cliff
        # pass and the caps still decide sizing, and a cluster with nothing worth funding
        # simply gets none.  It spreads DISCOVERY, which is free.
        by_cluster = {}
        for rho, tk, prog in rows:
            by_cluster.setdefault(CL.cluster_of(tk), []).append((rho, tk, prog))
        for lst in by_cluster.values():
            lst.sort(key=lambda r: (-r[0], str(r[1])))
        # clusters themselves ordered by their best market, so the strongest lead the rounds
        order = sorted(by_cluster, key=lambda c: (-by_cluster[c][0][0], c))
        out, depth = [], 0
        while len(out) < self.max_markets:
            took = False
            for c in order:
                lst = by_cluster[c]
                if depth < len(lst):
                    out.append(lst[depth])
                    took = True
                    if len(out) >= self.max_markets:
                        break
            if not took:
                break
            depth += 1
        return out

    def classify_one(self, ticker, book_body, program, now):
        yes_levels, no_levels = _book_levels(book_body)
        ys = alloc.score_side(yes_levels, program["target_size"], mode=C.S_MODE_ENTRY)
        ns = alloc.score_side(no_levels, program["target_size"], mode=C.S_MODE_ENTRY)
        yes_bid_c = ys.ref_c
        no_bid_c = ns.ref_c
        yes_ask_c = (100 - no_bid_c) if no_bid_c is not None else None
        rec = {
            "ticker": ticker, "program_id": program["program_id"],
            "series": program["series"] or str(ticker).split("-", 1)[0],
            "pinned": alloc.is_pinned(yes_bid_c, yes_ask_c),
            "target_size": program["target_size"],
            "sides": {
                "bid": {"S": ys.S, "qualifies": ys.qualifies, "cum_size": ys.cum_size,
                        "p": (yes_bid_c / 100.0) if yes_bid_c else None,
                        "legal": yes_bid_c is not None and yes_bid_c < C.MAX_LEGAL_PRICE_C},
                "ask": {"S": ns.S, "qualifies": ns.qualifies, "cum_size": ns.cum_size,
                        "p": (no_bid_c / 100.0) if no_bid_c else None,
                        "legal": no_bid_c is not None and no_bid_c < C.MAX_LEGAL_PRICE_C},
            },
            "yes_mid": _mid(yes_bid_c, yes_ask_c),
            "close_ts": self.close_ts.get(ticker),
            "ts": float(now),
        }
        self.table[ticker] = rec
        self.last[ticker] = float(now)
        return rec

    def learn_close(self, ex, bucket, ticker, now):
        """Fetch the market's SETTLEMENT close once, ever (charter B: `Slot.close_ts` must be
        the market close, NOT the program window end — the PYPL geometry is exactly a market
        that settles months after its reward window).  MIRROR (close unknown ↔ close wrong):
        an unfetchable close falls back to the PROGRAM end downstream — logged once per
        ticker as `market_close_unknown`, because that fallback UNDERSTATES carry on any
        market that outlives its program, which is the dangerous direction."""
        if ticker in self.close_ts or ticker in self.close_missing:
            return
        ok, _ = bucket.admit("classify_sweep", now)
        if not ok:
            return
        status, body = ex.market(ticker)
        if status == 429:
            bucket.on_429(now)                # SF-2
        if status != 200:
            return                            # transient; retried next sweep
        mkt = (body or {}).get("market") or body or {}
        ts = parse_iso(mkt.get("close_time") or mkt.get("expiration_time"))
        if ts is None:
            self.close_missing.add(ticker)
            R.log("market_close_unknown", ticker=ticker,
                  note="falling back to program end_ts; carry may be UNDERSTATED")
        else:
            self.close_ts[ticker] = ts
        self._persist_closes()

    def learn_p6(self, ex, bucket, ticker, now,
                 lookback_days=C.P6_LOOKBACK_DAYS, recheck_s=C.P6_RECHECK_S):
        """P6 pre-entry filter (charter B / note 43 §5's mirror): does ANYONE ever trade
        here?  One `trades` read per ticker per P6_RECHECK_S through the classify lane.
        MIRROR (refusing on a failed read ↔ admitting a dead book): a transient read failure
        leaves the verdict UNKNOWN, and unknown ADMITS (p6_ok) — refusing every market on an
        endpoint hiccup would stop the whole bot; a dead book admitted for one recheck period
        costs presence-rate only, and the §2.5 kill still covers it once we are there."""
        last = self.p6_ts.get(ticker)
        if last is not None and float(now) - last < float(recheck_s):
            return
        ok, _ = bucket.admit("classify_sweep", now)
        if not ok:
            return
        status, body = ex.trades(ticker, min_ts=float(now) - lookback_days * 86400.0)
        if status == 429:
            bucket.on_429(now)                # SF-2
        if status != 200:
            return                            # unknown, retried; p6_ok admits meanwhile
        traded = bool((body or {}).get("trades"))
        if not traded:
            R.log("p6_refused", ticker=ticker, lookback_days=lookback_days)
        self.p6[ticker] = traded
        self.p6_ts[ticker] = float(now)

    def p6_ok(self, ticker):
        return self.p6.get(ticker, True)      # unknown admits; see learn_p6's mirror

    def sweep(self, ex, bucket, programs, now, books=None):
        """One pass.  Returns the number of markets (re)classified."""
        books = books or {}
        n = 0
        # DAILIES FIRST (Ryan, 2026-07-30).  The close-learn order used to be the ρ rank,
        # which front-loads the far-settling giants: each costs one read before the gate can
        # skip it, and the near board waits behind them.  Sort the sweep instead by
        # (close unknown?, program end): markets whose close we KNOW and which end soonest
        # classify first — the quoting set — then unknowns in soonest-ending order, because a
        # program that ends tomorrow is the best prior that its market settles tomorrow (the
        # dailies), and a 5-month program can wait its turn to be refused.
        rows = sorted(self.candidates(programs, now),
                      key=lambda r: (r[1] not in self.close_ts,
                                     float(r[2].get("end_ts") or 0)))
        for rho, ticker, program in rows:
            self.learn_close(ex, bucket, ticker, now)
            self.learn_p6(ex, bucket, ticker, now)
            if not self.due(ticker, now):
                continue
            body = books.get(ticker)
            if body is None:
                ok, _ = bucket.admit("classify_sweep", now)
                if not ok:
                    break                     # out of budget: the rest waits for the next pass
                status, body = ex.book(ticker)
                if status == 429:
                    bucket.on_429(now)        # SF-2
                if status != 200:
                    continue
            self.classify_one(ticker, body, program, now)
            n += 1
        return n


def _book_levels(body):
    ob = (body or {}).get("orderbook") if isinstance(body, dict) else None
    fp = (ob or {}).get("orderbook_fp") or (body or {}).get("orderbook_fp") or {}
    def lv(rows):
        out = []
        for r in rows or []:
            try:
                out.append((int(round(float(r[0]) * 100)), float(r[1])))
            except (TypeError, ValueError, IndexError):
                continue
        return out
    return lv(fp.get("yes_dollars")), lv(fp.get("no_dollars"))


def _mid(yes_bid_c, yes_ask_c):
    if yes_bid_c is None or yes_ask_c is None:
        return None
    return (yes_bid_c + yes_ask_c) / 200.0


# =============================================================================================
# STAGE 3 — the slot table.
# =============================================================================================
_P6_WARNED = False


def _warn_p6_unwired():
    """Say it ONCE per process, loudly enough to appear in the G2 read-out."""
    global _P6_WARNED
    _P6_WARNED = True
    R.log("p6_pre_entry_filter_UNWIRED",
          note="no public-trade-tape source supplied; markets are admitted without the "
               "revealed-usefulness check (note 43 §5). Wire `p6=` before G3.")


def runway_ok(rho, hours_left, accrued_usd=0.0, floor_usd=C.ENTRY_FLOOR_USD,
              share=C.ENTRY_SHARE_ASSUMPTION):
    """The window-END guard.  ALLOCATE optimises a RATE and is blind to how many hours remain to
    earn it, so a dying program looks identical to a fresh one — measured live as 735 lots posted
    with under 25 minutes left.  Entering is only rational if the entry floor is still REACHABLE:

        share · (ρ/2) · h ≥ floor − accrued

    with a CONSERVATIVE share, because assuming we take the whole side is exactly the optimism
    that produces late entries.

    SECOND AMENDMENT (b): with accrual AT STAKE the reachability target is the forfeit CLIFF
    ($1.10), not the entry floor ($2.00) — this is an EXIT question, and excluding a program
    whose 70¢ could still be rescued is the runway guard confiscating the very accrual the
    rescue exists to recover (v4 carried the same exemption).

    MIRROR (window END ↔ window START): `preposition_ok` below.
    """
    floor = float(floor_usd)
    if float(accrued_usd) > 0.0:
        floor = min(floor, C.RESCUE_TARGET_USD)
    need = max(0.0, floor - float(accrued_usd))
    if need <= 0:
        return True
    if float(rho) <= 0:
        return False
    return float(share) * (float(rho) / 2.0) * float(hours_left) >= need


def preposition_ok(hours_to_start, lead_h=C.PREPOSITION_LEAD_H):
    """The window-START guard.  Before its window opens a resting order earns EXACTLY ZERO and
    carries the full marginal fill cost — and under a binding ceiling EVERY NON-EARNING DOLLAR
    DISPLACES AN EARNING DOLLAR 1:1, so a pre-start quote is a transfer out of the earning book.
    v4 locked ~$11 in slots whose programs opened 10.5 h later while live-window posts were being
    refused on `collateral_ceiling` in the same second."""
    return float(hours_to_start) <= float(lead_h) + 1e-12


def rival_S(S, ref_p, our_orders, df=C.DISCOUNT_FACTOR_DEFAULT):
    """SF-5 — the spec defines S as the RIVAL qualifying score, and the public book (which
    the classifier scored) REFLECTS our own resting orders.  Subtract our contribution:
    `qty × DF^(ref − our_price)` in cents.  Two honest notes on the approximation, both
    conservative:
      * the classifier scores in LEVELS mode (DF^level_index); cents-distance ≥ level index,
        so the cents form UNDERSTATES our contribution and leaves S slightly HIGH — the
        direction that under-allocates, never over-allocates.  At our usual price (the best
        itself) the two coincide exactly.
      * clamped at 0: when we ARE the whole qualifying side, S_rival = 0 and ALLOCATE
        correctly refuses to size up into our own book (spec §4.5) — the requoter's
        sole-qualifier hold keeps the minimum presence instead.
    """
    if not our_orders or ref_p is None:
        return float(S)
    ref_c = int(round(float(ref_p) * 100))
    own = 0.0
    for px_c, qty in our_orders:
        own += float(qty) * (float(df) ** max(0, ref_c - int(px_c)))
    return max(0.0, float(S) - own)


def build_slots(programs, classifier, now, presence_rows=None, tape=None, frozen=None,
                l_shed=None, prior_t_hat=None, p6=None, accrued=None, own_orders=None,
                held=None):
    """The slot table `engine.cycle()` consumes.

    `held` — D1, THE INVENTORY-SLOT GUARANTEE, EXTENDED FROM POLLING TO SLOT-BUILDING.
    A set of tickers we hold a position in or rest an order in.  `rank_for_poll` has always
    carried this guarantee ("a de-polled held market is never requoted, never cancelled, and its
    fills are never learned"), but it guaranteed only that a held market gets POLLED — and a poll
    with no SLOT is nearly as blind, because `engine.update_shed_targets` reads
    `by_key = {s.key: s for s in slots}` and on a miss can only KEEP a previous verdict
    (`held_from_last_reading`), never START a shed, and `requote_pass` has no `s.p` to price one
    with.  So every entry-only exclusion below is skipped for a held ticker.
    HOW THE BLOCKER ARRIVED: the free-ride gate's exemption was `own_qty > 0` read from LIVE
    ORDERS, and `book_fill` pops an order at `remaining <= 0` — so the exemption vanished at
    exactly the moment a fill first gave us inventory to shed.  The gate deleted the exit.
    WHICH EXCLUSIONS ARE ENTRY-ONLY, and why each is skipped rather than kept: `hours_left <= 0`,
    `preposition_ok`, `runway_ok`, P6, the entry price band and the free-ride gate ALL answer "is
    it worth ENTERING here", which is not the question a market we are already inside asks.
    Skipping them yields a slot with `hours_left = 0`, which `alloc.allocate` refuses for NEW
    allocation on its own line — so the slot buys a shed and a requote and cannot buy exposure.
    WHAT IS NOT SKIPPED: `series_denied` and `frozen`.  Those are kill switches, not opportunity
    tests; a frozen ticker is frozen because our books and the wire disagree about it, and
    quoting into that disagreement is the emergency, not the cure.
    MIRROR (exempting too much ↔ D1): the exemption cannot ADD exposure (see the `hours_left`
    note above, and B16/B15 still bind at `place()`), and it is scoped to tickers where we
    already have money — a set that only shrinks as positions close.

    Every exclusion that can be decided WITHOUT a request is applied here, so the rate budget is
    never spent on a market we would refuse anyway.  What this stage may NOT do is decide size —
    that is ALLOCATE's, under (★).

    `p6(ticker) -> bool` is the P6 PRE-ENTRY FILTER (note 43 §5's mirror: "zero fills forever
    means either the perfect rewards venue or a market nobody wants — the difference is whether
    ANYONE EVER TRADES THERE AT ALL").  It needs the public trade tape, which this build does not
    yet pull, so the seam is explicit and its absence is LOGGED rather than silently defaulting
    to admit.  An unwired filter that looks wired is the same defect class as a constant with no
    call site.
    """
    if p6 is None and not _P6_WARNED:
        _warn_p6_unwired()
    frozen = frozen or set()
    tape = tape or {}
    accrued = accrued or {}                   # program_id -> $ accrued (the cliff's memory)
    presence_rows = presence_rows or []
    held = set(held or ())                    # D1 — tickers we hold or rest an order in
    slots = []
    by_prog = {p["program_id"]: p for p in programs}

    for ticker, rec in sorted(classifier.table.items()):
        prog = by_prog.get(rec["program_id"])
        if prog is None or C.series_denied(ticker) or ticker in frozen:
            continue
        # D1: a market we are already inside is not asking whether entry is worthwhile.
        is_held = ticker in held
        hours_left = max(0.0, (prog["end_ts"] - float(now)) / 3600.0)
        hours_to_start = max(0.0, (prog["start_ts"] - float(now)) / 3600.0)
        if not is_held and (hours_left <= 0 or not preposition_ok(hours_to_start)):
            continue
        if not is_held and not runway_ok(prog["rho"], hours_left,
                                        accrued_usd=accrued.get(prog["program_id"], 0.0)):
            continue
        # ── THE LIVE-PROGRAM FILTER (2026-07-29 night). ─────────────────────────────────────
        # REWARDS ARE THE ONLY REASON WE ARE IN ANY OF THESE MARKETS.  A program that is not
        # currently paying turns every rung under it into a naked directional bet wearing a
        # market-making rationale — and that is not hypothetical: **$225 of the day's $477 loss
        # (47%) sat in six events that paid $0.00 of LIP.**  Refusing them costs exactly zero
        # credit, which makes this the only change on the board with no trade-off to price.
        # WHAT IS ACTUALLY TESTED HERE, stated narrowly on purpose.  The $0-credit events
        # decompose into at least two mechanisms and this closes the cheaper one:
        #   (a) `paid_out` — the program has already settled its period, so resting in it now
        #       accrues nothing toward a payout that has been made.  The field was PARSED by
        #       `parse_programs` and read by NOTHING; `lip_v5.py`'s own schedule note describes
        #       it flipping ~2 h post-close, so its meaning was never in doubt, only unused.
        #   (b) `pool_usd == 0` — a program with no period_reward cannot pay any share of it.
        #       `runway_ok` refuses ρ ≤ 0 today, but ONLY on the entry path and NOT when accrual
        #       already exceeds the rescue target (`if need <= 0: return True`), so a zero-pool
        #       program could still produce slots.  Made explicit rather than left as a
        #       side-effect of a different guard.
        # NOT CLAIMED: that this recovers the whole 47%.  The other mechanism in that $225 is
        # the $1.00 forfeit cliff (note 47 §1 — UST2AD/30AD absent from the receipt because
        # every rung was under a dollar), which is `floor_clearing_size`'s job, not this one.
        # Per note 49 R1, the honest scope is "this closes (a) and (b)", and the instrument that
        # will size the remainder is a per-event credit join we do not have yet.
        # MIRROR (refusing a live program ↔ quoting a dead one): a held ticker is exempt, so this
        # can never strand inventory (D1); and `paid_out` is the exchange's OWN word about its
        # own program, never our inference from a clock.
        if not is_held:
            if bool(prog.get("paid_out")):
                R.log("program_paid_out", ticker=ticker, program_id=prog.get("program_id"))
                continue
            if pool_usd(prog.get("period_reward")) <= 0.0:
                R.log("program_no_pool", ticker=ticker, program_id=prog.get("program_id"))
                continue

        if not is_held and p6 is not None and not p6(ticker):
            # ADVISORY by default (config.P6_ADVISORY, derivation there): rewards are paid for
            # RESTING, not for trading, so an untraded market is an uncontested one.  The
            # observation is still recorded per ticker so the first payout settles the question
            # with evidence — if quiet venues turn out not to credit, the forfeit gate and the
            # ratchet refuse them on measurement.
            R.log("p6_would_refuse", ticker=ticker, advisory=bool(C.P6_ADVISORY))
            if not C.P6_ADVISORY:
                continue
        # Charter B: `close_ts` is the MARKET's settlement close; `program_end_ts` is the
        # reward window's end.  They differ on exactly the markets the horizon exclusion and
        # the carry term exist for (PYPL settles months after its window).  Fallback to the
        # program end when the market object carried no close — logged in `learn_close`,
        # understates carry, and is the reason the classify sweep fetches the real one.
        market_close = rec.get("close_ts")
        close_ts = market_close if market_close is not None else prog["end_ts"]
        # ── THE SETTLEMENT GATE (note 52 D4).  ENTRY-ONLY; held is exempt (D1). ────────────
        # A fill is capital committed until settlement or an exit someone must take, so the
        # market's close is the worst-case time-to-liquidity of every contract bought here.
        # Enter only what settles inside SETTLE_HORIZON_H.  An UNKNOWN close REFUSES — the
        # fallback `close_ts = prog.end_ts` above makes a 2032 market wearing a 5-day program
        # look NEAR, which is exactly the wrong direction for this gate; the classify sweep
        # fetches the real close within one cadence, so the refusal costs one sweep of entry
        # latency, not the venue.
        if not is_held:
            if market_close is None:
                R.log_once("settle_close_unknown", ticker=ticker)
                continue
            settle_h = (float(market_close) - float(now)) / 3600.0
            if settle_h > C.SETTLE_HORIZON_H:
                # log_once per ticker: a far close is a PROPERTY of the market, not an
                # event — measured on the live probe, plain log emitted it 359x/cycle.
                R.log_once("settle_horizon_refused", ticker=ticker,
                           max_h=C.SETTLE_HORIZON_H)
                continue
        for side in ("bid", "ask"):
            sd = rec["sides"][side]
            p = sd["p"]
            if p is None or p <= 0:
                # THE EMPTY BOOK.  `p` is the collateral at the SAME-SIDE BEST, and an empty
                # side has no best — so this guard skipped it and such a market could never be
                # entered.  Measured live at G2: the highest-rho programs on the board are
                # EMPTY on both sides, so 200 classified markets produced ZERO slots and v5
                # would have quoted nothing at all.
                #
                # An empty side is not an absent opportunity, it is the cheapest one: no rival
                # score to split (S = 0), the whole pool addressable, and note 43 §6 — "empty
                # book moments are when presence is cheapest to establish".  The qualification
                # pass (spec §4.5) exists exactly for this and prices it as a DISCRETE
                # PRECONDITION rather than a rate; it simply never received a slot to act on.
                #
                # A legal price always exists on an empty side, so the slot is built at the
                # LAND-GRAB collateral — 1c either way, since a yes bid at 1c costs 1c and the
                # ask side's 99c is a no bid at 1c.  Pinned markets are excluded above, so no
                # illegal price can reach here.  MIRROR (entering an empty book ↔ entering a
                # book that is empty because the market is dead): P6 is that guard, and it is
                # applied before this loop.
                if rec["pinned"]:
                    continue
                p = C.LAND_GRAB_PRICE_C / 100.0
            elif not sd["legal"]:
                continue
            key = (ticker, side)
            # ── THE ENTRY PRICE BAND (config.ENTRY_BAND_LO_C..HI_C, 2026-07-29 night) ───────
            # FREE-RIDING IS RIGHT ABOUT WHO PAYS FOR QUALIFICATION AND SILENT ABOUT WHERE, and
            # left unbanded it lands on 1c — because 1c is where rival depth is deepest and so
            # where a free ride is ALWAYS available.  That is the whole content of notes 46/47
            # §6's "actively harmful: a covert instruction to quote the cheap crowded side", and
            # it is why arming `FREE_RIDE_ONLY` without this band arms half a rule.  The band's
            # two independent derivations (measured bias, n=8,240; measured competition and
            # cost-to-clear) are in config, along with why this is NOT the refuted
            # `p_min = k/bankroll`.
            # `p` IS THE RIGHT VARIABLE: it is the same-side best in its own collateral currency
            # — what one contract on THIS side costs us — which is exactly the axis note 47 §3's
            # calibration table is indexed by.  For an ask that is the NO price, not 100−yes.
            # ENTRY ONLY (`is_held`), for the same reason the free-ride gate is entry-only:
            # accrual is per PERIOD, so leaving mid-period forfeits everything earned for nothing.
            # ── THE BIAS FLOOR IS A BOOTSTRAP GUARD, NOT A STANDING RULE (Ryan, 2026-07-29). ──
            # "there shouldnt be a floor, there should be an average.  we can have a 1 cent so long
            # as all the others are 90 c."  Correct, and the average is now an actual instrument:
            # `PORTFOLIO_VAR_MAX` reads the realised mix, so a 1c rung beside a book of 90c rungs
            # contributes almost nothing to V and is admitted on its own merits.
            # WHY ANY FLOOR SURVIVES AT ALL, AND ONLY HERE.  The floor was never a variance
            # argument — it is measured EV (note 47 §3, n = 8,240: 2c realised 0.00% on 765
            # markets), and averaging does not repair a negative mean; it only spreads it.  But
            # (★) ALREADY charges that: `d_estimate` caps drift at `p`, so at 1c it charges the
            # whole penny.  The reason that charge was inert is that `drift_cost` scales with φ,
            # and φ's numerator was never wired (see `presence.fills_ct`) — so drift was ~0 and
            # the floor was standing in for a term that existed and could not fire.
            # NOW THAT φ IS WIRED, the floor is only needed where φ still has NO EVIDENCE — a
            # venue we have never rested in, whose drift charge is therefore still the seed rather
            # than a measurement.  So: floor the UNMEASURED, and let (★) judge the measured.  One
            # venue-hour of our own fills lifts it.
            # MIRROR (floor forever ↔ no floor at all): forever is what Ryan refused and what a
            # variance instrument makes unnecessary; none at all reproduces the 1c book on day one,
            # before any fill exists to price it.  The evidence test is the seam between them.
            # The evidence read is hoisted here, above the floor, because the floor now DEPENDS on
            # it.  Both halves come from `presence_rows` — see `presence.fills_ct` for why the
            # numerator was missing for the whole life of this program.
            rows = [r for r in presence_rows if (r.get("ticker"), r.get("side")) == key]
            t = tape.get(key, {})
            fills = int(t.get("fills_ct", P.fills_ct(rows, key) if rows else 0))
            rest_ch = float(t.get("rest_contract_hours",
                                  P.rest_contract_hours(rows, key) if rows else 0.0))
            p_c = int(round(float(p) * 100))
            unmeasured = fills <= 0 and rest_ch <= 0.0
            if C.ENTRY_BAND_ARMED and not is_held and unmeasured and \
                    not (C.ENTRY_BAND_LO_C <= p_c <= C.ENTRY_BAND_HI_C):
                R.log("entry_band_refused", ticker=ticker, side=side, p_c=p_c,
                      lo=C.ENTRY_BAND_LO_C, hi=C.ENTRY_BAND_HI_C)
                continue
            # SF-5: S is the RIVAL score — the classified book contains our own orders.
            S_riv = rival_S(sd["S"], p, (own_orders or {}).get(key))
            # -------------------------------------------------------------------------------
            # FREE-RIDE ON DEPTH, NEVER FUND IT  (C.FREE_RIDE_ONLY, armed 2026-07-29)
            # The 1,000-contract target is a DISCRETE precondition: short of it the snapshot
            # qualifies nobody.  v5 FUNDED the gap at LAND_GRAB_PRICE_C — the cheapest possible
            # proximity, at the price where a contract is worth nothing, in the size that
            # maximises count.  That path bought the 999-contract 1c gas rung and the 1,500- and
            # 3,000-contract TRUEV rungs; those postings are the largest single objects in the
            # whole tape and they score EXACTLY ZERO, because sizes beyond the target-size walk
            # are outside the qualifying set (CFTC filing: "If the Qualifying Yes Total Size is
            # greater than or equal to the target size, the procedure is stopped here").
            # DERIVATION OF THE REPLACEMENT: qualification is worth the same to us whether WE
            # created it or a rival did, and a rival's costs nothing.  So enter only where the
            # book ALREADY clears target WITHOUT our size.  The test must deduct our own resting
            # size (SF-5, applied to `qualifies` and not only to `S`) or it certifies our own
            # land grab back to us.
            # MIRROR (never funding ↔ never entering): the fear is that nothing qualifies without
            # us and the book deploys nothing.  MEASURED on the live board: 5,681 of 5,695
            # book-sides (99.75%) already reach target on rival size alone, and of the sides with
            # a competing score at or below 3, 99.3% qualify.  The fear is refuted; the
            # instrument if it ever bites is the `free_ride_refused` count below.
            # THE GATE IS ON ENTRY, NOT ON TENURE.  First cut of this refused any side whose
            # rival depth was short of target — including sides we were ALREADY resting in, and
            # there the test caught it: our own size is inside `cum_size`, so deducting it makes
            # `rival_cum` fall as our presence GROWS, and the slot count decayed 4 -> 2 over 90
            # cycles while four orders still rested unrefreshed.  That is the sole-qualifier case
            # `rival_S` already documents ("when we ARE the whole qualifying side ... the
            # requoter's sole-qualifier hold keeps the minimum presence instead"), and evicting
            # there is strictly worse than holding: accrual is per PERIOD, so abandoning mid-period
            # forfeits everything earned so far in exchange for nothing.
            # So: refuse to ENTER a book that does not qualify without us; once resting, hand the
            # decision to the requoter, which already prices staying.
            # D3 — WHAT THIS TEST IS AND IS NOT.  The first cut subtracted our own resting size
            # from `cum_size` "per SF-5" and then guarded the whole test on `own_qty <= 0`, so
            # the subtraction was UNREACHABLE — inside that branch our own size is zero by
            # definition and `rival_cum == cum_size` always.  Worse, the deduction could not have
            # been right even where it ran: `cum_size` is the TARGET-SIZE QUALIFYING WALK, which
            # STOPS at `target_size` (CFTC filing), so it is not side depth and subtracting from
            # it understates rival depth by up to 2x — verified, a side with 1,800 of depth
            # against a 1,000 target reports `cum_size = 1,200`, not 1,800.  SF-5's deduction is
            # correct for `S` (a sum over levels, which our order really is inside) and wrong for
            # a walk that terminates.  So the honest statement of this gate is the one written
            # below: **does the walk reach target at all** — with our own orders exempted by
            # `is_held`/`own_qty` rather than by arithmetic on a truncated cumulant.
            if C.FREE_RIDE_ONLY and not is_held:
                own_qty = sum(float(q) for _, q in ((own_orders or {}).get(key) or []))
                if own_qty <= 0 and not sd["qualifies"]:
                    R.log("free_ride_refused", ticker=ticker, side=side,
                          cum_size=round(float(sd["cum_size"]), 2),
                          target=float(rec["target_size"]))
                    continue
            # THE FILL-RATE SEED MUST REFLECT THE EVIDENCE WE ALREADY HAVE.  With no tape of
            # our own, phi falls back to PHI_SEED_MID = 0.08 fills/hour/resting-contract —
            # a v1 guess, flagged UNDERIVED, describing a BUSY book.  Since the ratchet was
            # generalized that seed gates admission, and it prices every fresh venue out:
            # measured live, gross 0.005-0.02 against carry 0.012 + drift 0.011-0.062, so
            # every unmeasured venue was pre-judged unprofitable before a single observation.
            #
            # But P6 already asked the exchange "does ANYONE trade here?", and a venue with no
            # public trades in P6_LOOKBACK_DAYS cannot be filling us at 0.08/h — that is the
            # definition of a quiet book, and note 43 §4 says a quiet book is where presence
            # is the product.  So the seed follows the tape: untraded ⇒ the CHEAP seed.
            # This is still a seed: one venue-hour of our OWN fills overrides it via the
            # Rule of Three, which is an upper bound and errs toward refusing.
            quiet = (p6 is not None and not p6(ticker))
            phi = M.phi_estimate(fills, rest_ch,
                                 p=(C.PHI_CHEAP_PRICE_CUT / 2.0) if quiet else p)
            d = M.d_estimate(t.get("drift_samples"), p)
            l_eff = M.l_eff_h(close_ts, now,
                              (l_shed or {}).get(key), settled=False)
            t_hat = (P.t_hat_shrunk(rows, key, prior=prior_t_hat) if rows
                     else (prior_t_hat if prior_t_hat is not None
                           else C.SHRINK_PRIOR_DEFAULT))
            land_grab = 0
            # Cheapest legal price ON THE SIDE BEING CREATED, on the YES axis: 1c for a bid,
            # 99c for an ask (v4's shape, kept — the default-1c-for-both form would sell YES
            # at 1c, an instantly-marketable CROSS wearing a land grab's name).
            lg_px_c = C.LAND_GRAB_PRICE_C if side == "bid" \
                else (100 - C.LAND_GRAB_PRICE_C)
            # D4 — THIS ZERO IS LOAD-BEARING, AND THE COMMENT THAT SAID OTHERWISE WAS FALSE.
            # The previous note here claimed the `elif` was unreachable under FREE_RIDE_ONLY
            # because the gate above had already `continue`d every non-qualifying side.  It had
            # not: the gate exempts sides we already rest in (`own_qty > 0`) and, since D1, every
            # ticker we hold (`is_held`) — and BOTH exemptions can carry a side whose walk does
            # NOT reach target.  So `not sd["qualifies"]` is reachable with FREE_RIDE_ONLY armed,
            # and without this branch those sides would fund the 1c land grab: the exact geometry
            # of the 999-contract 1c gas rung and the 1,500/3,000-contract TRUEV rungs, the
            # largest objects in the whole tape, which scored ZERO because the qualifying walk
            # stops at target.  `test_fixround.TestLandGrabIsDeadUnderFreeRide` now fails if this
            # line is deleted; before D4 the whole suite passed without it, which is note 45's
            # thesis arriving inside our own tests — green meant self-consistent, not correct.
            if C.FREE_RIDE_ONLY:
                land_grab = 0
            elif not sd["qualifies"] and not rec["pinned"]:
                land_grab = alloc.t0_qualification_size(sd["cum_size"], rec["target_size"])
                # Never cross the OTHER side (v4, kept): a bid grab must sit below the yes
                # ask; an ask grab above the yes bid.
                yes_bid_c = rec["sides"]["bid"]["p"]
                no_bid_c = rec["sides"]["ask"]["p"]
                yes_ask_c = (100 - int(round(no_bid_c * 100))) if no_bid_c else None
                yes_bid_c = int(round(yes_bid_c * 100)) if yes_bid_c else None
                if side == "bid" and yes_ask_c is not None and lg_px_c >= yes_ask_c:
                    land_grab = 0
                if side == "ask" and yes_bid_c is not None and lg_px_c <= yes_bid_c:
                    land_grab = 0
            slots.append(alloc.Slot(
                ticker, side, rho=prog["rho"], S=S_riv, p=p, venue=rec["series"],
                pinned=rec["pinned"], legal_price_exists=sd["legal"],
                phi=phi, d=d, l_eff=l_eff, t_hat=t_hat,
                program_id=prog["program_id"], window_h=prog["window_h"],
                hours_left=hours_left, hours_to_start=hours_to_start,
                target_size=rec["target_size"], cum_size=sd["cum_size"],
                land_grab_size=land_grab, land_grab_price_c=lg_px_c,
                accrued=float(accrued.get(prog["program_id"], 0.0)),
                close_ts=close_ts, program_end_ts=prog["end_ts"],
                moneyness=abs((rec["yes_mid"] or 0.5) - 0.5) * 100.0))
    return slots


def rank_for_poll(slots, r_star=C.FLOOR_RATE_PER_H, limit=None):
    """The §4.6 clamp: which markets get the 1 Hz book poll.  Ranked by the ALLOCATOR'S OWN
    first-dollar rate under (★), so the clamp and the allocator agree — the whole point of the
    classify-then-rank ordering.

    MIRROR (rank picks the best ↔ inventory we already hold): the INVENTORY-SLOT GUARANTEE.  A
    market with a nonzero position or a non-terminal order is ALWAYS polled, whatever it ranks:
    fills are learned from cancels and polls, so a de-polled market is never requoted, never
    cancelled, and its fills are never learned — the position becomes invisible to our own books
    and no shed is ever posted.  Callers pass those tickers in `always`; `rank_for_poll` only
    orders the rest.
    """
    scored = [(s.net_at(0, r_star), s) for s in slots]
    scored.sort(key=lambda kv: (-kv[0], kv[1].ticker, kv[1].side))
    out = [s for _, s in scored]
    return out[:int(limit)] if limit else out


def poll_set(slots, always_tickers, connected, r_star=C.FLOOR_RATE_PER_H):
    """The tickers to poll this cycle: every market we hold or have an order in, plus the best
    of the rest up to the breadth the connection supports (6 REST, 32 while the WS is up)."""
    breadth = C.MAX_WS_MARKETS if connected else C.MAX_REST_MARKETS
    held = [t for t in sorted(always_tickers)]
    ranked = [s.ticker for s in rank_for_poll(slots, r_star)]
    out = list(held)
    for t in ranked:
        if len(out) >= breadth:
            break
        if t not in out:
            out.append(t)
    return out
