#!/usr/bin/env python3
"""Reduce accrued live trade-journal records into live performance metrics.

Phase 4 of the validation loop: the playbook emits closed-trade realized records
(`fills_journal`) into the DBG metrics every cycle (src/execution.py:_fills_journal).
Those snapshots OVERLAP -- the same fill appears every cycle until it ages out of the
window -- so the first job is to DEDUP by fill id. Then we reduce to the live metrics
that are directly comparable with the backtest (research/analytics.py): realized
total, win rate, profit factor, avg win/loss, and a by-symbol / by-side breakdown.

This is the missing live-vs-backtest feedback loop (audit #30): point it at a dump of
the DBG metrics (or the raw fills_journal arrays) and compare the live edge to what
replay_mp projected. Realized PnL (USDT) only -- live MAE/MFE is not reconstructable
from a stateless 15-min runtime, so excursion metrics stay backtest-only by design.

Usage:
  python3 research/live_journal.py path/to/metrics_dump.json
  # the JSON may be: a list of fills records; a list of {fills_journal:[...]} dicts;
  # or a single {fills_journal:[...]} / {metrics:{fills_journal:[...]}} object.
"""
import argparse
import json
import sys


def _collect(blob):
    """Pull every fill record out of whatever JSON shape was dumped."""
    out = []

    def walk(v):
        if isinstance(v, dict):
            if "fills_journal" in v and isinstance(v["fills_journal"], list):
                out.extend(r for r in v["fills_journal"] if isinstance(r, dict))
                # already captured the journal list -- recurse into OTHER values only,
                # so its records are not also picked up by the bare-record branch.
                for k, val in v.items():
                    if k != "fills_journal" and isinstance(val, (dict, list)):
                        walk(val)
                return
            # a bare fill record (has profit + an id/symbol)
            if "profit" in v and ("id" in v or "sym" in v or "symbol" in v):
                out.append(v)
                return
            for val in v.values():
                if isinstance(val, (dict, list)):
                    walk(val)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    walk(blob)
    return out


def _key(rec, idx):
    """Dedup key: prefer the fill id; fall back to (sym, ts, profit) then position."""
    rid = rec.get("id")
    if rid:
        return ("id", str(rid))
    return ("syn", str(rec.get("sym") or rec.get("symbol")), rec.get("ts"),
            rec.get("profit"), idx if not rec.get("ts") else "")


def dedup(records):
    """Dedup overlapping journal snapshots by fill id (synthetic key if no id)."""
    seen = {}
    for i, r in enumerate(records):
        seen.setdefault(_key(r, i), r)
    return list(seen.values())


def reduce(records):
    """Live metrics from deduped fill records. Network-free -> unit-testable."""
    recs = dedup(records)
    profits = []
    for r in recs:
        p = r.get("profit")
        if p is None:
            continue
        try:
            profits.append(float(p))
        except (TypeError, ValueError):
            continue
    out = {"n_fills": len(recs), "n_realized": len(profits)}
    if not profits:
        return out
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    gl = abs(sum(losses))
    out["realized_total"] = round(sum(profits), 4)
    out["win_rate"] = round(len(wins) / len(profits) * 100, 1)
    out["profit_factor"] = round(sum(wins) / gl, 2) if gl > 0 else None
    out["avg_win"] = round(sum(wins) / len(wins), 4) if wins else 0.0
    out["avg_loss"] = round(-gl / len(losses), 4) if losses else 0.0

    def _grp(field, alt=None):
        g = {}
        for r in recs:
            p = r.get("profit")
            if p is None:
                continue
            try:
                p = float(p)
            except (TypeError, ValueError):
                continue
            k = r.get(field) or (r.get(alt) if alt else None) or "?"
            g.setdefault(str(k).upper(), []).append(p)
        return {k: {"n": len(v), "realized": round(sum(v), 4),
                    "win_rate": round(sum(1 for x in v if x > 0) / len(v) * 100, 0)}
                for k, v in sorted(g.items(), key=lambda kv: sum(kv[1]), reverse=True)}
    out["by_symbol"] = _grp("sym", "symbol")
    out["by_side"] = _grp("side")
    return out


def report(m):
    print("\n" + "=" * 56)
    print("LIVE TRADE JOURNAL (realized, deduped by fill id)")
    print("=" * 56)
    print(f"  fills seen        : {m['n_fills']}  ({m.get('n_realized', 0)} with realized PnL)")
    if not m.get("n_realized"):
        print("  no realized fills yet -- nothing to reduce."); return
    print(f"  realized total    : {m['realized_total']:+.4f} USDT")
    print(f"  win rate          : {m['win_rate']}%")
    print(f"  profit factor     : {m['profit_factor']}")
    print(f"  avg win / loss    : {m['avg_win']:+.4f} / {m['avg_loss']:+.4f}")
    print(f"  by symbol         : {m['by_symbol']}")
    print(f"  by side           : {m['by_side']}")
    print("=" * 56)
    print("Compare against research/analytics.py (backtest) to spot live edge decay.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", help="JSON dump of DBG metrics or fills_journal arrays")
    a = ap.parse_args()
    try:
        blob = json.loads(open(a.dump).read())
    except Exception as exc:
        print(f"cannot read {a.dump}: {exc}"); sys.exit(1)
    report(reduce(_collect(blob)))


if __name__ == "__main__":
    main()
