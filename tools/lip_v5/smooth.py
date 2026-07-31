"""lip_v5.smooth — THE COMPETITION SMOOTHING WINDOW, and the smoothed S it produces.

── WHY (note 55, "RECONCILED WITH RYAN", item 4b) ──────────────────────────────────────────
    "Anti-churn without timers: (a) a switch pays its TRUE cost ...; (b) rank on SMOOTHED S
     (window derived from the recorder's measured flicker rate, first night).  30s
     min-resting-life + B14 remain the structural floor."

The marginal queue re-ranks every cycle.  Ranking on the SNAPSHOT rival score S makes the
queue chase book flicker: a rival's 200-lot at the touch that appears and vanishes inside a
second moves `share = q/(q+S)` by a factor of three, and every one of those moves is a
candidate reallocation.  The switch toll (`marginal.transit_h`) prices a move that is REAL;
smoothing is what stops us paying the toll to chase a move that was never there.

── THE WINDOW IS DERIVED, NEVER CHOSEN ─────────────────────────────────────────────────────
TWO derivations, in priority order, and the one that fires is logged by name.

(D1) FROM THE RECORDER'S OWN TAPE — `~/kalshi_data/competition/deltas-YYYYMMDD.jsonl.gz`
     (note 54: WS orderbook_delta for every in-window rewarded market, ms-stamped, full
     snapshot each 15-min reconnect ⇒ exact S(t) at any second).  A FLICKER is a change at a
     book's touch that is REVERTED — the level returns to its previous size.  Measure the
     revert time of every reverted change; take its MEDIAN, τ½.  A moving average of length
     w removes a flicker of duration τ by averaging it against (w − τ) of unflickered book,
     so a window of τ½ halves the flicker energy BY CONSTRUCTION (half of all flickers are
     shorter than it).  Then double it, for the anti-alias reason in (D2), which is the same
     reason a sampled system always doubles: w = 2·τ½.

(D2) THE DERIVED FALLBACK — used when the recorder's tape is not reachable from the machine
     the bot boots on (it lives on the VPS; the builder's laptop cannot see it), and it is
     LOGGED AS A FALLBACK, loudly, every boot.  The floor is structural and already in this
     codebase: a plan-driven cancel may not touch a rung younger than `MIN_RESTING_LIFE_S`
     (30 s, B14's companion), so 30 s is the shortest period at which our own book can
     respond to anything.  A control loop that is smoothed FASTER than twice its own minimum
     action period is aliasing — it will read structure it cannot act on and act on structure
     that is not there (Nyquist, and it is the same arithmetic).  So

         w_fallback = 2 × MIN_RESTING_LIFE_S = 60 s

     and this is a FLOOR on (D1) as well, for the same reason: a measured τ½ of 3 s does not
     entitle the queue to a 6 s window it cannot act inside.

MIRROR (window too LONG ↔ too short): too long ranks on a book that no longer exists — the
queue defends a seat whose rivals left, and the cost is the share we could have taken, which
is bounded by one cycle of one rung's rate and self-corrects as the average catches up.  Too
short is churn: every flicker is a candidate switch, each switch costs the toll, and the
30 s resting floor turns that into a book that spends its life in transit.  The asymmetric
one is churn, so where the two derivations disagree the LONGER wins (the `max` below).
"""

import gzip
import json
import math
import os

from . import config as C
from . import runtime as R


# The recorder's output directory (note 54).  Absolute, expanded at call time — the bot boots
# on the VPS where this exists; the derivation degrades to (D2) anywhere else.
DELTAS_GLOB_DIR = "~/kalshi_data/competition"
DELTAS_PREFIX = "deltas-"

# (D2), stated once as an expression so the derivation cannot drift from the constant:
# twice the structural minimum action period.  The 2 is Nyquist's, not a taste.
ANTI_ALIAS_MULT = 2.0


def fallback_window_s():
    """(D2) — 2 x MIN_RESTING_LIFE_S.  See the module header."""
    return ANTI_ALIAS_MULT * float(C.MIN_RESTING_LIFE_S)


def median_revert_s(events):
    """The MEDIAN revert time of reverted touch changes — (D1)'s statistic, factored out so it
    can be tested against a hand-built tape with no file and no gzip.

    `events` — an iterable of (ts_seconds, book_key, level_size) at the touch, in time order.
    A REVERT is a change followed by a return to the pre-change size at the same book; its
    duration is the time between them.  Changes that never revert inside the tape are not
    flicker and contribute nothing (they are the drift the window must still track).
    Returns None when the tape contains no reverted change at all — the caller then has no
    measurement and must say so rather than invent one.
    """
    last = {}                                    # book -> (ts, size)
    prev = {}                                    # book -> (ts, size) BEFORE the last change
    durations = []
    for ts, key, size in events:
        ts = float(ts)
        size = float(size)
        if key not in last:
            last[key] = (ts, size)
            continue
        l_ts, l_size = last[key]
        if size == l_size:
            continue                             # not a change
        p = prev.get(key)
        if p is not None and size == p[1]:
            # returned to the size that preceded the last change: that change was a flicker,
            # and its duration is how long the book spent away from it.
            durations.append(ts - l_ts)
        prev[key] = (l_ts, l_size)
        last[key] = (ts, size)
    if not durations:
        return None
    durations.sort()
    n = len(durations)
    if n % 2:
        return durations[n // 2]
    return 0.5 * (durations[n // 2 - 1] + durations[n // 2])


def _iter_delta_events(path):
    """Yield (ts_s, book_key, touch_size) from one recorder file.  Tolerant by design: the
    recorder's schema is not ours and a field rename must degrade to (D2), never crash the
    boot.  A line we cannot read is skipped; a file we cannot read raises to the caller, which
    catches and falls back."""
    op = gzip.open if str(path).endswith(".gz") else open
    with op(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = row.get("ts") or row.get("timestamp") or row.get("t")
            tk = row.get("ticker") or row.get("market_ticker")
            side = row.get("side") or row.get("s")
            price = row.get("price") or row.get("p")
            size = row.get("size") if "size" in row else row.get("q")
            if ts is None or tk is None or size is None:
                continue
            try:
                ts = float(ts)
            except Exception:
                continue
            if ts > 1e11:                         # ms stamps (note 54: "ms-stamped")
                ts = ts / 1000.0
            yield ts, (tk, side, price), size


def derive_window_s(dir_path=None, max_files=1, now=None):
    """THE BOOT HOOK.  Returns (window_s, source) with source in {"deltas", "fallback"}.

    Reads at most `max_files` of the recorder's newest daily files, measures τ½ with
    `median_revert_s`, and returns `max(2·τ½, 2·MIN_RESTING_LIFE_S)`.  Any failure — directory
    absent (the builder's laptop), file unreadable, schema unrecognised, no reverted change in
    the tape — returns the (D2) fallback WITH ITS NAME, so the log always says which
    derivation the running bot is using.  Silence here would be the defect: a window is a
    behaviour, and a behaviour whose provenance is unreadable is unauditable.
    """
    d = os.path.expanduser(dir_path or DELTAS_GLOB_DIR)
    fb = fallback_window_s()
    try:
        names = sorted([n for n in os.listdir(d) if n.startswith(DELTAS_PREFIX)],
                       reverse=True)[:max(1, int(max_files))]
    except Exception:
        R.log("smoothing_window", source="fallback", window_s=fb, reason="deltas_dir_absent",
              dir=d, floor_s=fb)
        return fb, "fallback"
    if not names:
        R.log("smoothing_window", source="fallback", window_s=fb, reason="no_delta_files",
              dir=d, floor_s=fb)
        return fb, "fallback"
    events = []
    for n in names:
        try:
            events.extend(_iter_delta_events(os.path.join(d, n)))
        except Exception as e:
            R.log("smoothing_window", source="fallback", window_s=fb,
                  reason="delta_read_failed", file=n, err=str(e)[:120], floor_s=fb)
            return fb, "fallback"
    events.sort(key=lambda e: e[0])
    tau = median_revert_s(events)
    if tau is None:
        R.log("smoothing_window", source="fallback", window_s=fb, reason="no_reverted_change",
              events=len(events), floor_s=fb)
        return fb, "fallback"
    w = max(ANTI_ALIAS_MULT * float(tau), fb)
    R.log("smoothing_window", source="deltas", window_s=round(w, 3),
          median_revert_s=round(float(tau), 4), events=len(events), files=len(names),
          floor_s=fb)
    return w, "deltas"


class SmoothedS(object):
    """An exponential moving average of the rival score per (ticker, side), with time constant
    `window_s`.

    WHY AN EWMA AND NOT A BOXCAR: the cycle is not evenly sampled (poll clamps, rate lanes,
    reconnects), and a boxcar over unevenly-spaced samples weights a busy minute more than a
    quiet one — which is exactly backwards, since the busy minute is the flicker.  The EWMA's
    decay is a function of ELAPSED TIME, so a sample that arrives after a gap of one window
    carries e⁻¹ of the old estimate and nothing is double-counted:

        a = 1 − exp(−Δt / w);   S̄ ← S̄ + a·(S − S̄)

    CONVERGENCE (the spine).  This is memory of the WORLD — a sequence of readings of other
    people's books — not memory of our own decisions, which is the line note 55's convergence
    test draws ("Memory of the WORLD is legal ... Memory of OUR OWN PAST DECISIONS as an input
    is the disease").  Cancelling every order of ours exchange-side does not change one sample
    in here, so the smoothed book is identical across the cancel and the same allocation
    re-emerges.  `rival_S` has already removed our own resting size from S before it arrives.
    """

    __slots__ = ("window_s", "state")

    def __init__(self, window_s=None):
        self.window_s = float(window_s if window_s is not None else fallback_window_s())
        self.state = {}                          # key -> (last_ts, S_bar)

    def observe(self, key, s_value, now):
        """Fold one reading in and return the smoothed value."""
        s_value = max(0.0, float(s_value))
        now = float(now)
        prev = self.state.get(key)
        if prev is None or self.window_s <= 0.0:
            self.state[key] = (now, s_value)
            return s_value
        t0, bar = prev
        dt = max(0.0, now - t0)
        a = 1.0 - math.exp(-dt / self.window_s) if dt > 0 else 0.0
        bar = bar + a * (s_value - bar)
        self.state[key] = (now, bar)
        return bar

    def get(self, key, default=0.0):
        v = self.state.get(key)
        return default if v is None else v[1]


# THE BOOT DERIVATION, CACHED.  The window is a property of the recorder's tape, not of a
# cycle, so it is derived ONCE per process and every consumer reads the same number — a
# per-Maker derivation would re-read the tape on every construction and, worse, would let two
# Makers in one process rank on two different windows.
_BOOT_WINDOW = None


def boot_window_s(dir_path=None):
    global _BOOT_WINDOW
    if _BOOT_WINDOW is None:
        _BOOT_WINDOW = derive_window_s(dir_path=dir_path)
    return _BOOT_WINDOW[0]


def _reset_boot_window():
    """Tests only — the cache is process-wide by design."""
    global _BOOT_WINDOW
    _BOOT_WINDOW = None
