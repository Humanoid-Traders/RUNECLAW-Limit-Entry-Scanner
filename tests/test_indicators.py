"""v0.9.4 golden-value indicator tests (audit fixes #3/#6/#10).

The execution layer was fortress-tested; the signal math was not. These pin
the two computed indicators (_wilder_atr, _ema_trend) to hand-computed known
samples, the new forming-bar guard (_closed_bars -- deterministic: a closed
last bar is never dropped, only a genuinely in-progress one), and the new
ATR contiguity fail-safe (a malformed MID-series bar must yield None -> the
documented range-proxy fallback, never a stitched fictitious true-range).

Run: python3 tests/test_indicators.py
"""
import sys
import time
import types
from pathlib import Path

_g = types.ModuleType("getagent"); sys.modules["getagent"] = _g
for _s in ("data", "trade", "runtime"):
    _m = types.ModuleType("getagent." + _s); setattr(_g, _s, _m)
    sys.modules["getagent." + _s] = _m
_SRC = Path(__file__).resolve().parent.parent / "src"
_pkg = types.ModuleType("src"); _pkg.__path__ = [str(_SRC)]; sys.modules["src"] = _pkg

import importlib.util  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SRC / (name.split(".")[-1] + ".py"))
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


features = _load("src.features")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def _bar(h, l, c, ts=None):
    b = {"high": h, "low": l, "close": c}
    if ts is not None:
        b["timestamp"] = ts
    return b


# ---- _wilder_atr goldens ----

def test_atr_constant_range():
    # every bar h=10.5 l=9.5 c=10.0: TR = max(1.0, 0.5, 0.5) = 1.0 forever
    bars = [_bar(10.5, 9.5, 10.0)] * 16
    atr = features._wilder_atr(bars, 14)
    _assert(abs(atr - 1.0) < 1e-12, "constant-range bars -> ATR exactly 1.0")


def test_atr_hand_computed():
    # bars (h,l,c): (12,10,11) (13,11,12) (15,12,14) (14,12,13), period 2
    # TR1 = max(2, |13-11|, |11-11|) = 2 ; TR2 = max(3, |15-12|, |12-12|) = 3
    # TR3 = max(2, |14-14|, |12-14|) = 2
    # seed = (2+3)/2 = 2.5 ; wilder: (2.5*1 + 2)/2 = 2.25
    bars = [_bar(12, 10, 11), _bar(13, 11, 12), _bar(15, 12, 14), _bar(14, 12, 13)]
    atr = features._wilder_atr(bars, 2)
    _assert(abs(atr - 2.25) < 1e-12, "hand-computed Wilder ATR(2) = 2.25")


def test_atr_warmup_guard():
    bars = [_bar(10.5, 9.5, 10.0)] * 14  # needs period+1 = 15
    _assert(features._wilder_atr(bars, 14) is None, "13 TRs < period -> None (warm-up enforced)")


def test_atr_midseries_gap_failsafe():
    # v0.9.4: malformed bar in the MIDDLE -> stitched TR would be fictitious -> None
    bars = [_bar(10.5, 9.5, 10.0)] * 20
    bars[10] = {"high": 10.5}  # missing low/close
    _assert(features._wilder_atr(bars, 14) is None,
            "mid-series malformed bar -> None (degrade to proxy, never stitch)")


def test_atr_trailing_junk_trims_clean():
    # a malformed row at the END trims cleanly; contiguous prefix still computes
    bars = [_bar(10.5, 9.5, 10.0)] * 16 + [{"note": "summary row"}]
    atr = features._wilder_atr(bars, 14)
    _assert(atr is not None and abs(atr - 1.0) < 1e-12,
            "trailing junk row trims; contiguous series still computes")


# ---- _ema_trend goldens ----

def test_ema_trend_directions():
    up = [_bar(0, 0, c) for c in range(1, 21)]
    d, s = features._ema_trend(up, 5, 0.05)
    _assert(d == "long" and 0.0 < s <= 1.0, "rising closes -> long, strength in (0,1]")
    down = [_bar(0, 0, c) for c in range(20, 0, -1)]
    d, s = features._ema_trend(down, 5, 0.05)
    _assert(d == "short" and 0.0 < s <= 1.0, "falling closes -> short")
    flat = [_bar(0, 0, 10.0)] * 20
    d, s = features._ema_trend(flat, 5, 0.05)
    _assert(d == "neutral" and s == 0.0, "flat closes -> neutral, strength 0")


def test_ema_trend_guards():
    d, s = features._ema_trend([_bar(0, 0, 10.0)] * 3, 5, 0.05)
    _assert(d == "neutral" and s == 0.0, "too few closes -> neutral (warm-up enforced)")
    up = [_bar(0, 0, c) for c in range(1, 21)]
    _, s = features._ema_trend(up, 5, 0.0001)
    _assert(s == 1.0, "strength capped at 1.0 for a huge gap vs tiny norm")


# ---- _closed_bars forming-candle guard (v0.9.4) ----

def test_closed_bars_drops_forming_only():
    now = time.time()
    closed = _bar(1, 1, 1, ts=now - 7200)   # 1h bar opened 2h ago -> closed
    forming = _bar(1, 1, 1, ts=now - 60)    # 1h bar opened 1min ago -> forming
    out = features._closed_bars([closed, forming], "1h")
    _assert(len(out) == 1 and out[0] is closed, "in-progress last bar dropped")
    out = features._closed_bars([closed, _bar(1, 1, 1, ts=now - 7200)], "1h")
    _assert(len(out) == 2, "closed last bar kept -- guard is a no-op on closed feeds")


def test_closed_bars_failopen():
    now_ms = time.time() * 1000
    out = features._closed_bars([_bar(1, 1, 1)], "1h")
    _assert(len(out) == 1, "no timestamp -> keep (fail-open)")
    out = features._closed_bars([_bar(1, 1, 1, ts=now_ms - 30_000)], "7h")
    _assert(len(out) == 1, "unknown interval -> keep (fail-open)")
    out = features._closed_bars([_bar(1, 1, 1, ts=now_ms - 30_000)], "1h")
    _assert(len(out) == 0, "ms-epoch timestamp coerced; forming bar dropped")
    _assert(features._closed_bars([], "1h") == [], "empty list passthrough")


# ---- realized_vol goldens (v0.9.20 vol-regime gate input) ----

def _cbar(c):
    return {"close": c}


def test_realized_vol_matches_definition():
    import math
    import statistics
    closes = [100, 105, 100, 105, 100]                 # lookback=4 -> 5 closes, 4 log-rets
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    expected = statistics.stdev(rets) * math.sqrt(8760) * 100.0   # sample std (n-1), hourly ppy
    got = features.realized_vol([_cbar(c) for c in closes], lookback=4)
    _assert(got is not None and abs(got - expected) < 1e-9,
            "realized_vol == std(log-rets) x sqrt(8760) x 100 -> %.1f%%" % got)


def test_realized_vol_constant_is_zero():
    _assert(features.realized_vol([_cbar(100.0)] * 40, lookback=30) == 0.0,
            "flat closes -> 0 realized vol")


def test_realized_vol_too_few_bars_none():
    _assert(features.realized_vol([_cbar(100.0)] * 5, lookback=30) is None,
            "fewer than lookback+1 clean closes -> None (the gate then fail-opens)")


def test_realized_vol_uses_only_last_lookback():
    tail = [_cbar(c) for c in [100, 101, 100, 101, 100]]           # lookback=4 window
    noisy = [_cbar(c) for c in [1, 1000, 1, 1000]] + tail          # older wild bars, must be ignored
    _assert(abs(features.realized_vol(noisy, 4) - features.realized_vol(tail, 4)) < 1e-9,
            "only the last lookback+1 bars enter the vol -> older bars ignored")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} indicator tests passed.")
