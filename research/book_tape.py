#!/usr/bin/env python3
"""Reader for the order-book tape recorded by book_snap.py / book-tape.yml.

The tape lives on the `book-tapes` branch as tapes/YYYY-MM-DD/HHMMZ.json.gz.
Fetch it locally with:

  git fetch origin book-tapes && git worktree add /tmp/book-tapes book-tapes

Then load snapshots here. The contract consumers rely on:

  * nearest(ts_ms, max_gap_s=900) returns the snapshot closest to a cycle
    timestamp, or None when the gap exceeds max_gap_s -- a missing snapshot
    is an HONEST GAP, never interpolated (same discipline as the collector).
  * Snapshot format: {"v":1, "ts_ms":..., "books":{SYM:{"asks":[[px,sz]..],
    "bids":[...], "ts":...}}, "tickers":{SYM:{...}}, "errors":{...}}.

Intended consumer (future work, once >=3 weeks of tape exist): replay
enrichment swaps its degraded order-book fallback for tape.nearest(bar_ts)
books on XAG -- making metals replay-validatable for the first time -- and
equity-perp slippage models read real depth. Nothing imports this yet; it
ships with the collector so the format has a reference reader from day one.
"""
import gzip
import json
from bisect import bisect_left
from pathlib import Path


class BookTape:
    def __init__(self, root):
        """root = the tapes/ directory of a book-tapes checkout."""
        self.root = Path(root)
        self._index = []          # sorted [(ts_ms, path)]
        for p in sorted(self.root.glob("*/*.json.gz")):
            # filename encodes UTC day+time; cheap index without opening files
            day = p.parent.name          # YYYY-MM-DD
            hm = p.stem.split(".")[0]    # HHMMZ
            try:
                y, mo, d = (int(x) for x in day.split("-"))
                h, mi = int(hm[0:2]), int(hm[2:4])
            except (ValueError, IndexError):
                continue
            # days->ms via a civil-date rata die (no datetime: keep zero-dep)
            a = (14 - mo) // 12; yy = y + 4800 - a; mm = mo + 12 * a - 3
            jdn = d + (153 * mm + 2) // 5 + 365 * yy + yy // 4 - yy // 100 + yy // 400 - 32045
            ts = ((jdn - 2440588) * 86400 + h * 3600 + mi * 60) * 1000
            self._index.append((ts, p))
        self._index.sort()

    def __len__(self):
        return len(self._index)

    @staticmethod
    def load(path):
        with gzip.open(path, "rt") as f:
            return json.load(f)

    def nearest(self, ts_ms, max_gap_s=900):
        """Snapshot nearest to ts_ms, or None if the gap exceeds max_gap_s."""
        if not self._index:
            return None
        keys = [t for t, _ in self._index]
        j = bisect_left(keys, int(ts_ms))
        best = None
        for k in (j - 1, j):
            if 0 <= k < len(self._index):
                gap = abs(self._index[k][0] - int(ts_ms))
                if best is None or gap < best[0]:
                    best = (gap, self._index[k][1])
        if best is None or best[0] > max_gap_s * 1000:
            return None
        return self.load(best[1])


if __name__ == "__main__":
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else "/tmp/book-tapes/tapes"
    t = BookTape(root)
    print(f"{len(t)} snapshots under {root}")
    if len(t):
        ts, p = t._index[-1]
        s = t.load(p)
        print(f"latest: {p}  books={list(s['books'])}  tickers={len(s['tickers'])}")
