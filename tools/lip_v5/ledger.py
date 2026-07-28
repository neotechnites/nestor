"""
lip_v5.ledger — the append-only money record (spec §6.2), and the SEPARATE presence file.

Two files, and the split is derived, not tidiness (spec §6.2 N2):

  (a) 14 MB/day of metering would be replayed on every restart by a path that needs NONE of
      it, lengthening the one procedure that must be fast and correct;
  (b) the ORDER LEDGER is the money record and must stay append-only FOREVER, whereas metering
      must be COMPACTABLE.

MIRROR (write path ↔ replay path): presence accumulators are DELTAS, never cumulative, so
replay is a sum and any split/shuffle of the rows gives the same answer (T-P2).  The order
ledger's mirror is the schema-mismatch ABORT: a ledger we cannot replay exactly is a ledger we
must not act on.
"""

import os

from . import config as C
from . import runtime as R


class SchemaMismatch(Exception):
    """v1 §9.1, inherited verbatim: a schema we do not recognise ABORTS the restart.  Guessing
    at an unknown row is how a bookkeeping gap becomes a real position."""


class Ledger(object):
    """One writer, one file (spec §11 Collisions)."""

    def __init__(self, path=None, schema=C.LEDGER_SCHEMA):
        self.path = path or C.LEDGER_PATH
        self.schema = schema
        self.seq = 0

    def write(self, kind, **fields):
        if kind not in C.LEDGER_KINDS:
            raise SchemaMismatch("unknown ledger kind %r" % kind)
        self.seq += 1
        rec = {"schema": self.schema, "k": kind, "ts": R._now(), "seq": self.seq}
        rec.update(fields)
        R.append_jsonl(self.path, rec, fsync=(kind in ("place_req", "cash_feed")))
        return rec

    def read(self):
        """Replay input.  Aborts on ANY row whose schema we do not own — including rows a
        FUTURE v5 wrote, which is the case a version check silently gets wrong by ignoring."""
        rows = R.read_jsonl(self.path)
        for r in rows:
            s = r.get("schema")
            if s is not None and s != self.schema:
                raise SchemaMismatch("ledger schema %r != %r" % (s, self.schema))
        return rows


class PresenceLog(object):
    """`v5_presence.jsonl`, rotated daily, NEVER in the order ledger (spec §6.2 N2)."""

    def __init__(self, path=None, daily_path=None, compact_days=C.PRESENCE_COMPACT_DAYS):
        self.path = path or C.PRESENCE_PATH
        self.daily_path = daily_path or C.PRESENCE_DAILY_PATH
        self.compact_days = int(compact_days)

    def segment_path(self, ts):
        base, ext = os.path.splitext(self.path)
        return "%s-%s%s" % (base, R._utc_day(ts), ext)

    def write_rows(self, rows, ts=None):
        for row in rows or []:
            R.append_jsonl(self.segment_path(ts if ts is not None else row.get("from_ts", 0.0)),
                           row)
        return len(rows or [])

    def read_segment(self, ts):
        return R.read_jsonl(self.segment_path(ts))

    def compact(self, now, compact_fn):
        """Fold segments older than 7 days into per-(m,s)-per-day aggregates.

        **NEVER rewrites a file in place**: write the aggregate, fsync, THEN unlink the
        segment.  A metering record that can be silently rewritten is a metering record that
        cannot be trusted — and the ordering is what makes a crash mid-compaction lose nothing
        (worst case the aggregate is written twice, which a re-run detects, rather than the
        segment being gone with no aggregate).
        """
        base, ext = os.path.splitext(self.path)
        d = os.path.dirname(os.path.abspath(self.path)) or "."
        prefix = os.path.basename(base) + "-"
        cutoff_day = int(float(now) // 86400) - self.compact_days
        folded = []
        if not os.path.isdir(d):
            return folded
        for name in sorted(os.listdir(d)):
            if not name.startswith(prefix) or not name.endswith(ext):
                continue
            try:
                day = int(name[len(prefix):-len(ext)] if ext else name[len(prefix):])
            except ValueError:
                continue
            if day > cutoff_day:
                continue
            seg = os.path.join(d, name)
            rows = R.read_jsonl(seg)
            for agg in compact_fn(rows):
                R.append_jsonl(self.daily_path, agg, fsync=True)
            os.unlink(seg)                                   # ONLY after the aggregate landed
            folded.append(seg)
        return folded


def coid_seq_load(path=None):
    """v1 §9.5 — the persisted counter.  NO run-id anywhere near it."""
    path = path or C.SEQ_PATH
    try:
        with open(path) as fh:
            return int(fh.read().strip() or 0)
    except (IOError, ValueError):
        return 0


def coid_seq_store(seq, path=None):
    path = path or C.SEQ_PATH
    R._check_write_path(path)
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(str(int(seq)))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
