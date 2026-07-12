#!/usr/bin/env python3
"""Periodic venue-listings watcher (research/CI-side, zero-dependency).

THE GAP IT CLOSES (2026-07-12): the engine cannot enumerate the venue -- the
SDK has no Bitget-scoped bulk surface (futures.tickers never existed;
derivatives_tickers is cross-exchange, adjudicated + parked), so shadow
discovery reads only the NAMED watchlist and new listings (fresh coins, new
stock perps) are invisible until a human notices. This script uses Bitget's
PUBLIC REST API -- which CI can call freely, no SDK constraints, no auth --
to diff the venue's full USDT-M perp list against a committed snapshot and
report what's new, classified and volume-ranked, so the update ritual
(extend the classifier allowlists / discovery_watchlist / core universe)
is triggered by data instead of luck.

Runs from .github/workflows/listings-watch.yml on a daily cron (report-only,
same philosophy as baseline-drift: it never blocks anything). Also runnable
by hand:

  python3 research/listings_watch.py            # diff against the snapshot
  python3 research/listings_watch.py --update   # rewrite the snapshot (do this
                                                # in the same commit that acts
                                                # on the findings)

Exit code is always 0 (report-only); the workflow decides what to surface.
If new listings exist, a machine-readable dump is written to
research/.listings_new.json for the workflow's issue step.
"""
import json
import sys
import urllib.request
from pathlib import Path

BASE = "https://api.bitget.com"
CONTRACTS = BASE + "/api/v2/mix/market/contracts?productType=usdt-futures"
TICKERS = BASE + "/api/v2/mix/market/tickers?productType=usdt-futures"
HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "listings_snapshot.json"
NEW_DUMP = HERE / ".listings_new.json"
DISCOVERY_FLOOR_USD = 30_000_000   # mirrors manifest discovery_min_volume_usdt


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "runeclaw-listings-watch"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _classify(base):
    """Route through the REAL engine classifier so the report shows exactly how
    discovery would class each new name (unknown stock perps default to crypto
    -- which is itself the finding that says 'extend the allowlist')."""
    sys.path.insert(0, str(HERE.parent / "tests"))
    from _stub import stub_getagent, load_src
    stub_getagent()
    features = load_src("features")
    return features.classify_asset(base)


def main():
    update = "--update" in sys.argv

    contracts = _get(CONTRACTS)
    rows = contracts.get("data") or []
    live = {}
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        if sym:
            live[sym] = {"base": str(r.get("baseCoin", "")).upper(),
                         "status": str(r.get("symbolStatus", ""))}
    if len(live) < 100:   # sanity: a partial/failed feed must not nuke the diff
        print(f"ABORT: contracts feed returned only {len(live)} rows -- not trusted")
        return

    if update or not SNAPSHOT.exists():
        SNAPSHOT.write_text(json.dumps(sorted(live), indent=0) + "\n")
        print(f"snapshot {'updated' if update else 'seeded'}: {len(live)} symbols -> {SNAPSHOT.name}")
        if not update:
            print("first run: nothing to diff against; future runs report changes")
        return

    known = set(json.loads(SNAPSHOT.read_text()))
    added = sorted(set(live) - known)
    removed = sorted(known - set(live))

    print(f"venue perps: {len(live)}   snapshot: {len(known)}   "
          f"new: {len(added)}   delisted: {len(removed)}")

    if not added and not removed:
        print("no changes -- listings snapshot is current")
        NEW_DUMP.unlink(missing_ok=True)
        return

    vol = {}
    if added:
        try:
            for t in (_get(TICKERS).get("data") or []):
                vol[str(t.get("symbol", "")).upper()] = float(t.get("usdtVolume") or 0)
        except Exception as exc:   # volume is enrichment, never fatal
            print(f"(ticker volumes unavailable: {type(exc).__name__})")

    report = []
    for sym in added:
        base = live[sym]["base"] or sym.replace("USDT", "")
        cls = _classify(base)
        v = vol.get(sym)
        above = v is not None and v >= DISCOVERY_FLOOR_USD
        report.append({"symbol": sym, "base": base, "class": cls,
                       "usd_volume_24h": v, "above_discovery_floor": above})
        vtxt = f"${v/1e6:.1f}M" if v is not None else "?"
        flag = "  <-- WATCHLIST CANDIDATE (above $30M floor)" if above else ""
        print(f"  NEW  {sym:<18} class={cls:<9} vol24h={vtxt}{flag}")
        if cls == "crypto":
            print(f"       ^ classes as CRYPTO by default -- verify it isn't a "
                  f"stock/ETF/commodity needing an allowlist entry")
    for sym in removed:
        print(f"  DELISTED  {sym}")

    NEW_DUMP.write_text(json.dumps({"added": report, "removed": removed}, indent=1))
    print(f"\nACTION RITUAL (when acting on this): extend features._DISC_STOCK/"
          f"_ETF/_COMMODITY and/or discovery_watchlist as needed, then rerun "
          f"with --update in the SAME commit so the snapshot matches the code.")


if __name__ == "__main__":
    main()
