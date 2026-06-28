"""Phase 1 validation-loop tests: analyze() aggregation is network-free, so we test
the metrics math on synthetic trades -- expectancy, profit factor, MAE/MFE, capture
ratio, the loser-MFE 'trail opportunity' signal, and the edge verdict.

Run: python3 tests/test_analytics.py
"""
import sys
import types
from pathlib import Path

# stub getagent so `import replay` (pulled in by analytics) doesn't need the SDK
_g = types.ModuleType("getagent"); sys.modules["getagent"] = _g
for _s in ("data", "trade", "runtime"):
    _m = types.ModuleType("getagent." + _s); setattr(_g, _s, _m)
    sys.modules["getagent." + _s] = _m

_RESEARCH = Path(__file__).resolve().parent.parent / "research"
sys.path.insert(0, str(_RESEARCH))
import analytics  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


TRADES = [
    {"sym": "BTCUSDT", "mode": "breakout", "reason": "tp1", "ret_pct": 5.0, "mfe_pct": 6.0, "mae_pct": -1.0},
    {"sym": "ETHUSDT", "mode": "pullback", "reason": "sl",  "ret_pct": -2.0, "mfe_pct": 3.0, "mae_pct": -2.5},
    {"sym": "BTCUSDT", "mode": "pullback", "reason": "tp1", "ret_pct": 3.0, "mfe_pct": 4.0, "mae_pct": -0.5},
    {"sym": "SOLUSDT", "mode": "breakout", "reason": "sl",  "ret_pct": -2.0, "mfe_pct": 0.5, "mae_pct": -2.2},
]


def test_core_metrics():
    a = analytics.analyze(TRADES)
    _assert(a["win_rate"] == 50.0, "win rate 50%")
    _assert(a["expectancy_pct"] == 1.0, "expectancy +1.0%/trade")
    _assert(a["total_pct"] == 4.0, "total +4.0%")
    _assert(a["profit_factor"] == 2.0, "profit factor 8/4 = 2.0")
    _assert(a["avg_win_pct"] == 4.0 and a["avg_loss_pct"] == -2.0, "avg win/loss 4.0 / -2.0")


def test_mae_mfe():
    a = analytics.analyze(TRADES)
    _assert(a["mae"]["min"] == -2.5, "worst MAE -2.5%")
    _assert(a["mfe"]["max"] == 6.0, "best MFE 6.0%")
    # capture: winners 5/6=.833, 3/4=.75 -> ~0.79
    _assert(abs(a["capture_ratio"] - 0.79) < 0.02, "capture ratio ~0.79")
    # losers' MFE: (3 + 0.5)/2 = 1.75 -> the trail/BE opportunity signal
    _assert(a["loser_avg_mfe_pct"] == 1.75, "losers' avg MFE 1.75% (trail opportunity)")
    _assert(a["winner_avg_mae_pct"] == -0.75, "winners' avg MAE -0.75%")


def test_breakdowns():
    a = analytics.analyze(TRADES)
    _assert(a["by_mode"]["breakout"]["n"] == 2 and a["by_mode"]["pullback"]["n"] == 2, "split by mode")
    _assert(a["by_reason"]["sl"]["n"] == 2 and a["by_reason"]["tp1"]["n"] == 2, "split by exit reason")
    _assert(a["by_symbol"]["BTCUSDT"]["n"] == 2, "BTC has 2 trades")


def test_verdict_flags_trail_opportunity():
    v = analytics.verdict(analytics.analyze(TRADES))
    _assert("EDGE" in v and "NO EDGE" not in v, "positive expectancy + PF>1 -> EDGE")
    # losers' MFE 1.75 > |avg_loss 2.0| * 0.8 = 1.6 -> trail/BE lever flagged
    _assert("TRAIL" in v.upper(), "flags the trail/BE lever from loser MFE -> " + v)


def test_empty():
    a = analytics.analyze([])
    _assert(a["n_trades"] == 0, "no trades -> n_trades 0")
    _assert("NO TRADES" in analytics.verdict(a), "empty -> NO TRADES verdict")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} analytics tests passed.")
