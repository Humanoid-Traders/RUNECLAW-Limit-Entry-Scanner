"""v0.8.0 portfolio-risk research tooling tests (network-free).

These pin the PURE math added to research/replay_mp.py for the v0.8 value-tests:
rolling correlation, correlation-weighted exposure, and the cumulative-curve max
drawdown that measures loss CLUSTERING (the metric a tail-risk control is judged
on). The value-test verdicts themselves (corr cap rejected, realized-loss breaker
validated) live in docs/DESIGN_v0.8.0_portfolio_risk.md -- these tests guard the
measurement, not the conclusion.

Run: python3 tests/test_portfolio_risk.py
"""
import sys
import types
from pathlib import Path

# stub getagent so importing replay (pulled in by replay_mp) needs no SDK
_g = types.ModuleType("getagent"); sys.modules["getagent"] = _g
for _s in ("data", "trade", "runtime"):
    _m = types.ModuleType("getagent." + _s); setattr(_g, _s, _m)
    sys.modules["getagent." + _s] = _m

_RESEARCH = Path(__file__).resolve().parent.parent / "research"
sys.path.insert(0, str(_RESEARCH))
import replay_mp as M  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def _bar(close):
    # [ts, open, high, low, close, vol]; only close (index 4) is read by the helpers
    return [0, close, close, close, close, 0.0]


def test_corr_perfect_and_anti():
    a = [0.01, -0.02, 0.03, -0.01, 0.02, 0.0]
    b = [2 * x for x in a]              # perfectly correlated (positive scale)
    c = [-x for x in a]                # perfectly anti-correlated
    _assert(abs(M._corr(a, b) - 1.0) < 1e-9, "identical-shape returns -> corr +1")
    _assert(abs(M._corr(a, c) + 1.0) < 1e-9, "negated returns -> corr -1")


def test_corr_guards():
    _assert(M._corr([0.01, 0.02], [0.01, 0.02]) is None, "too few points -> None")
    _assert(M._corr([0.0] * 8, [0.01, -0.01] * 4) is None, "zero-variance series -> None")


def test_pair_corr_from_klines():
    # two symbols whose closes move together -> high positive correlation
    base = [100, 101, 99, 102, 98, 103, 97, 104, 96, 105]
    kl = {"AAA": [_bar(x) for x in base],
          "BBB": [_bar(x * 1.5) for x in base]}
    c = M._pair_corr(kl, "AAA", "BBB", len(base) - 1, 48)
    _assert(c is not None and c > 0.99, "co-moving closes -> pair corr ~+1 -> " + str(c))


def test_corr_exposure_same_vs_opposite_side():
    base = [100, 101, 99, 102, 98, 103, 97, 104, 96, 105]
    kl = {"CAND": [_bar(x) for x in base],
          "HELD": [_bar(x) for x in base]}      # identical -> corr +1
    i = len(base) - 1
    same = M._corr_exposure(kl, "CAND", "long", [("HELD", "long")], i, 48, 0.5)
    opp = M._corr_exposure(kl, "CAND", "long", [("HELD", "short")], i, 48, 0.5)
    _assert(same > 0.95, "same-side fully-correlated held -> exposure ~+1 -> " + str(round(same, 3)))
    _assert(opp < 0, "opposite-side held earns a hedge credit -> negative exposure -> " + str(round(opp, 3)))


def test_corr_exposure_thin_history_uses_prior():
    # no kline history for the held name -> fall back to the crypto corr prior
    kl = {"CAND": [_bar(100)] * 3}
    cost = M._corr_exposure(kl, "CAND", "long", [("NOHIST", "long")], 2, 48, 0.5)
    _assert(abs(cost - M._CORR_PRIOR) < 1e-9, "thin history -> uses _CORR_PRIOR " + str(M._CORR_PRIOR))


def test_max_drawdown():
    # cumulative curve: +5, +3, -4, -6, +2 -> equity 5,8,4,-2,0; peak 8; trough -2 -> DD -10
    _assert(M._max_drawdown([5, 3, -4, -6, 2]) == -10, "max drawdown of clustered losses = -10")
    _assert(M._max_drawdown([1, 2, 3]) == 0, "monotonic-up curve -> 0 drawdown")
    _assert(M._max_drawdown([]) == 0, "empty -> 0 drawdown")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} portfolio-risk tests passed.")
