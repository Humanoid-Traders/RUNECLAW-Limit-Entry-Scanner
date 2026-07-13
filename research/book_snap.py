#!/usr/bin/env python3
"""Order-book tape collector (research/CI-side, zero-dependency).

THE WALL IT BREAKS (2026-07-13): metals is UNVALIDATABLE offline -- replay's
degraded order-book fallback caps XAG ~4pt below its live score, so every
metals question ends at "judge it on live results only." Historical order
books cannot be bought or backfilled; the only way to ever have them is to
START RECORDING THEM. This script snapshots Bitget's PUBLIC merge-depth for
a small named set (metals + the thin equity perps whose slippage we model
blind) plus the ticker rows for the discovery/census watch set, so that:

  * after ~3 weeks of tape, replay enrichment can consume REAL XAG books
    (nearest snapshot within the cycle window) instead of the fallback --
    metals becomes replay-validatable for the first time;
  * equity-perp book depth is on record for slippage modeling (the v0.6.0
    "market-entry slippage on these thinner books is watched" promise,
    currently watched with no instrument);
  * fresh listings (EVAA/KORU/SPCX class) accrue a forward volume/price
    ledger from DAY ONE, so the ~2026-08 expansion revisit inherits an
    unbroken record even if the venue prunes early klines.

Runs from .github/workflows/book-tape.yml every 30 minutes; snapshots land
on the dedicated `book-tapes` branch (orphan, data-only -- main stays
clean). A missing snapshot is an honest gap: the collector never fabricates
or interpolates. Also runnable by hand:

  python3 research/book_snap.py --outdir /tmp/snap   # one snapshot now

Read the tape back with research/book_tape.py.
"""
import argparse
import gzip
import json
import sys
import time as _time  # research-side only; `import time` is banned in src/ (platform lint)
import urllib.request
from pathlib import Path

BASE = "https://api.bitget.com"
DEPTH = BASE + "/api/v2/mix/market/merge-depth?productType=usdt-futures&symbol={sym}&precision=scale0&limit=50"
TICKERS = BASE + "/api/v2/mix/market/tickers?productType=usdt-futures"

# Books: the two standing blind spots. Metals (the fallback wall) and the
# thin equity perps (slippage watched blind since v0.6.0). Keep this list
# short -- every name is one API call per snapshot, forever.
BOOK_SYMBOLS = ["XAGUSDT", "XAUUSDT",
                "TSLAUSDT", "NVDAUSDT", "MSTRUSDT", "SOXLUSDT"]

# Ticker rows kept per snapshot: live universe leaders/candidates + the
# 2026-07-13 census + fresh listings + the discovery watchlist. One bulk
# call, filtered -- adding names here is free.
TICKER_WATCH = frozenset("""
XAGUSDT XAUUSDT QQQUSDT TSLAUSDT NVDAUSDT MSTRUSDT
SKHYNIXUSDT SKHYUSDT SNDKUSDT SOXLUSDT DRAMUSDT MUUSDT KORUUSDT SPCXUSDT
EVAAUSDT TUSDT VELVETUSDT
ASMLUSDT GSUSDT COSTUSDT LRCXUSDT ANTHROPICUSDT OPENAIUSDT SMHUSDT SOXSUSDT
KWEBUSDT CLUSDT BZUSDT NATGASUSDT
""".split())


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "runeclaw-book-snap"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def collect():
    snap = {"v": 1, "ts_ms": None, "books": {}, "tickers": {}, "errors": {}}
    for sym in BOOK_SYMBOLS:
        try:
            d = _get(DEPTH.format(sym=sym))
            data = d.get("data") or {}
            if snap["ts_ms"] is None:
                snap["ts_ms"] = int(d.get("requestTime") or 0) or None
            snap["books"][sym] = {"asks": data.get("asks") or [],
                                  "bids": data.get("bids") or [],
                                  "ts": data.get("ts")}
        except Exception as exc:
            snap["errors"][sym] = type(exc).__name__
    try:
        rows = _get(TICKERS).get("data") or []
        for t in rows:
            sym = str(t.get("symbol", "")).upper()
            if sym in TICKER_WATCH:
                snap["tickers"][sym] = {k: t.get(k) for k in
                                        ("lastPr", "bidPr", "askPr", "bidSz", "askSz",
                                         "high24h", "low24h", "usdtVolume", "indexPrice",
                                         "fundingRate", "holdingAmount", "ts")}
    except Exception as exc:
        snap["errors"]["__tickers__"] = type(exc).__name__
    if snap["ts_ms"] is None:
        snap["ts_ms"] = int(_time.time() * 1000)
    return snap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="snap_out",
                    help="directory to write the gzipped snapshot into")
    a = ap.parse_args()

    snap = collect()
    got = len(snap["books"])
    if got == 0 and not snap["tickers"]:
        print(f"FAIL: no books and no tickers collected (errors: {snap['errors']})")
        sys.exit(1)   # visible red run; a missing snapshot is an honest gap

    ts = snap["ts_ms"] / 1000.0
    day = _time.strftime("%Y-%m-%d", _time.gmtime(ts))
    hm = _time.strftime("%H%M", _time.gmtime(ts))
    out = Path(a.outdir) / day
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{hm}Z.json.gz"
    with gzip.open(path, "wt") as f:
        json.dump(snap, f, separators=(",", ":"))
    print(f"snapshot {path}  books={got}/{len(BOOK_SYMBOLS)}  "
          f"tickers={len(snap['tickers'])}  errors={snap['errors'] or 'none'}  "
          f"size={path.stat().st_size}B")


if __name__ == "__main__":
    main()
