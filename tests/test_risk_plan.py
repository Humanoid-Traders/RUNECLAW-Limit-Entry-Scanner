"""v0.9.4 risk.build_plan golden tests (audit fix #4).

Pins the trade-level math that was, until now, proven only by live behavior:
entry placement (VWAP -/+ k*ATR pullback; breakout at market), stop direction
(long SL BELOW entry, short SL ABOVE -- always on the losing side), the
max(structure, tier-floor) stop rule for both entry modes, backward-from-stop
sizing (notional = max_loss / sl_pct * size_factor), the margin-budget cap,
and the fail-closed None paths. The audit's directional-correctness rules
(no long SL above entry, no TP on the wrong side, no zero/negative risk)
are asserted explicitly.

Run: python3 tests/test_risk_plan.py
"""
import sys
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
risk = _load("src.risk")
SF = features.SymbolFeatures


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


_CFG = {"atr_limit_mult": "0.3", "tp1_pct": "5.0", "tp2_pct": "15.0",
        "trail_atr_mult": "2.0", "breakeven_pct": "2.0",
        "sl_min_btc_eth_pct": "1.5", "sl_min_sol_bnb_pct": "1.2", "sl_min_alt_pct": "2.5",
        "max_loss_usdt": "15", "leverage": 10, "margin_budget": "100",
        "breakout_level_buffer_pct": "0.2", "breakout_stop_atr_mult": "1.0",
        "breakout_tp1_pct": "4.0"}


def _feats(**kw):
    # 24h range 95-105, VWAP 100 -> proxy ATR = (105-95)/2.5 = 4.0
    d = dict(last=102.0, vwap=100.0, high=105.0, low=95.0)
    d.update(kw)
    return SF("ALTUSDT", True, **d)


# ---- pullback long ----

def test_long_pullback_geometry():
    p = risk.build_plan(_feats(), _CFG, 1.0, side="long")
    _assert(abs(p.entry - 98.8) < 1e-9, "entry = VWAP - 0.3*ATR = 100 - 1.2 = 98.8")
    _assert(p.sl_price < p.entry, "long SL is BELOW entry (losing side)")
    _assert(abs(p.sl_price - 95.0) < 1e-6, "structure stop lands at the 24h low (95)")
    _assert(p.tp1 > p.entry and p.tp2 > p.tp1, "long TPs above entry, laddered")
    expected_sl_pct = (98.8 - 95.0) / 98.8
    _assert(abs(p.sl_pct - expected_sl_pct) < 1e-12, "sl_pct from structure (>%.4f floor)" % 0.025)


def test_long_floor_beats_tight_structure():
    # NB: low also feeds the proxy ATR ((105-98.5)/2.5 = 2.6), so entry moves too
    p = risk.build_plan(_feats(low=98.5), _CFG, 1.0, side="long")   # structure ~0.7% < floor
    _assert(abs(p.sl_pct - 0.025) < 1e-12, "alt 2.5%% floor wins over tighter structure")
    _assert(abs(p.sl_price - p.entry * 0.975) < 1e-9, "SL = entry * (1 - floor)")


# ---- pullback short ----

def test_short_pullback_geometry():
    p = risk.build_plan(_feats(), _CFG, 1.0, side="short")
    _assert(abs(p.entry - 101.2) < 1e-9, "entry = VWAP + 0.3*ATR = 101.2")
    _assert(p.sl_price > p.entry, "short SL is ABOVE entry (losing side)")
    _assert(p.tp1 < p.entry and p.tp2 < p.tp1, "short TPs below entry, laddered")
    expected_sl_pct = (105.0 - 101.2) / 101.2   # structure to the 24h high
    _assert(abs(p.sl_pct - max(expected_sl_pct, 0.025)) < 1e-12, "short stop = max(structure, floor)")


# ---- breakout entries ----

def test_breakout_short_stop_above():
    p = risk.build_plan(_feats(last=95.2), _CFG, 1.0, side="short", entry_mode="breakout")
    _assert(p.entry == 95.2 and p.entry_mode == "breakout", "breakout enters at market (last)")
    _assert(p.sl_price > p.entry, "breakout-short SL above entry")
    _assert(p.sl_pct >= 0.025, "breakout stop respects the tier floor")


def test_breakout_long_stop_below():
    p = risk.build_plan(_feats(last=104.9), _CFG, 1.0, side="long", entry_mode="breakout")
    _assert(p.sl_price < p.entry, "breakout-long SL below entry")
    _assert(p.tp1 > p.entry, "breakout-long TP above entry")


# ---- sizing ----

def test_backward_from_stop_sizing():
    p = risk.build_plan(_feats(), _CFG, 1.0, side="long")
    _assert(abs(p.notional_usdt - 15.0 / p.sl_pct) < 1e-6,
            "notional = max_loss / sl_pct (dollar risk is the control variable)")
    _assert(abs(p.margin_usdt - p.notional_usdt / 10) < 1e-9, "margin = notional / leverage")
    _assert(p.sizing_ok and p.note == "", "uncapped plan sizes clean")
    half = risk.build_plan(_feats(), _CFG, 0.5, side="long")
    _assert(abs(half.notional_usdt - p.notional_usdt / 2) < 1e-6, "size_factor halves notional")


def test_margin_budget_cap():
    cfg = dict(_CFG); cfg["max_loss_usdt"] = "30"
    f = _feats(); f.symbol = "BTCUSDT"; f.low = 99.0        # floor 1.5% -> huge notional
    p = risk.build_plan(f, cfg, 1.0, side="long")
    _assert(p.note == "capped_by_margin_budget", "oversize plan is budget-capped with a note")
    _assert(abs(p.margin_usdt - 100.0) < 1e-9 and abs(p.notional_usdt - 1000.0) < 1e-9,
            "capped margin = budget; notional = budget * leverage")


def test_wider_stop_means_smaller_size():
    tight = risk.build_plan(_feats(low=98.5), _CFG, 1.0, side="long")   # floor 2.5%
    wide = risk.build_plan(_feats(low=90.0), _CFG, 1.0, side="long")    # structure ~8.9%
    _assert(wide.sl_pct > tight.sl_pct and wide.notional_usdt < tight.notional_usdt,
            "wider stop -> smaller position at the SAME dollar risk (the MSTR case)")


# ---- fail-closed paths + ATR preference ----

def test_fail_closed_and_atr_preference():
    _assert(risk.build_plan(SF("XUSDT", False), _CFG, 1.0) is None, "ok=False -> None")
    _assert(risk.build_plan(_feats(vwap=None), _CFG, 1.0) is None, "missing VWAP -> None")
    f = _feats(); f.kline_ok = True; f.atr = 2.0            # real Wilder ATR beats proxy 4.0
    p = risk.build_plan(f, _CFG, 1.0, side="long")
    _assert(abs(p.entry - (100.0 - 0.3 * 2.0)) < 1e-9, "kline ATR preferred over range proxy")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} risk-plan tests passed.")
