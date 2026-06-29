"""v0.9.1 live-journal reducer tests (network-free, no SDK).

The playbook emits OVERLAPPING fills_journal snapshots every cycle (same fill recurs
until it ages out of the window), so the reducer must dedup by fill id before
computing live metrics. These pin the dedup + the realized aggregation that closes
the live-vs-backtest loop.

Run: python3 tests/test_journal_reduce.py
"""
import sys
from pathlib import Path

_RESEARCH = Path(__file__).resolve().parent.parent / "research"
sys.path.insert(0, str(_RESEARCH))
import live_journal as LJ  # noqa: E402  (self-contained, no replay import)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def _f(tid, sym, side, profit, ts=0):
    return {"id": tid, "sym": sym, "side": side, "profit": profit, "ts": ts}


def test_dedup_overlapping_snapshots():
    # three cycles; the same two fills recur, a third appears later
    snaps = [
        {"fills_journal": [_f("a", "ETHUSDT", "buy", 5.0), _f("b", "INJUSDT", "sell", -2.0)]},
        {"fills_journal": [_f("a", "ETHUSDT", "buy", 5.0), _f("b", "INJUSDT", "sell", -2.0)]},
        {"fills_journal": [_f("b", "INJUSDT", "sell", -2.0), _f("c", "WLDUSDT", "buy", 3.0)]},
    ]
    recs = LJ._collect(snaps)
    _assert(len(recs) == 6, "collected all 6 raw (overlapping) records")
    m = LJ.reduce(recs)
    _assert(m["n_fills"] == 3, "deduped to 3 unique fills by id")


def test_realized_metrics():
    recs = [_f("a", "ETHUSDT", "buy", 5.0), _f("b", "INJUSDT", "sell", -2.0),
            _f("c", "WLDUSDT", "buy", 3.0)]
    m = LJ.reduce(recs)
    _assert(m["realized_total"] == 6.0, "realized total 5-2+3 = 6.0")
    _assert(m["win_rate"] == round(2 / 3 * 100, 1), "win rate 2/3")
    _assert(m["profit_factor"] == 4.0, "PF (5+3)/2 = 4.0")
    _assert(m["avg_win"] == 4.0 and m["avg_loss"] == -2.0, "avg win/loss 4.0 / -2.0")


def test_by_symbol_and_side():
    recs = [_f("a", "ETHUSDT", "buy", 5.0), _f("b", "ETHUSDT", "buy", -1.0),
            _f("c", "INJUSDT", "sell", 3.0)]
    m = LJ.reduce(recs)
    _assert(m["by_symbol"]["ETHUSDT"]["n"] == 2 and m["by_symbol"]["ETHUSDT"]["realized"] == 4.0,
            "by_symbol aggregates ETH -> 2 fills, +4.0")
    _assert(m["by_side"]["SELL"]["realized"] == 3.0, "by_side aggregates sell -> +3.0")


def test_missing_profit_skipped():
    recs = [_f("a", "ETHUSDT", "buy", 5.0), {"id": "b", "sym": "X", "profit": None}]
    m = LJ.reduce(recs)
    _assert(m["n_fills"] == 2 and m["n_realized"] == 1, "None-profit fill counted but not reduced")


def test_empty():
    m = LJ.reduce([])
    _assert(m["n_fills"] == 0 and "realized_total" not in m, "empty -> no metrics")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} journal-reduce tests passed.")
