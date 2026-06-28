"""v0.6.7 position-ownership tests — the snake_case entry-price root cause.

Live position records serialise to snake_case (open_price_avg, mark_price), but
_record_notional / _ENTRY_PRICE_KEYS only carried camelCase openPriceAvg + the
unrelated average_open_price/open_price. So price read None -> notional None ->
_runeclaw_sized False -> EVERY position was excluded from ownership ->
_best_effort_position_controls never ran -> trail/time-stop never fired all session.

These tests use the EXACT AAVE position record the operator dumped.

Run: python3 tests/test_position_ownership.py
"""
import sys
import types
from pathlib import Path

_g = types.ModuleType("getagent"); sys.modules["getagent"] = _g
for _sub in ("data", "trade", "runtime"):
    _m = types.ModuleType("getagent." + _sub); setattr(_g, _sub, _m)
    sys.modules["getagent." + _sub] = _m
_SRC = Path(__file__).resolve().parent.parent / "src"
_pkg = types.ModuleType("src"); _pkg.__path__ = [str(_SRC)]; sys.modules["src"] = _pkg
_trade = sys.modules["getagent.trade"]

import importlib.util  # noqa: E402

def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SRC / (name.split(".")[-1] + ".py"))
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

_load("src.features")
execution = _load("src.execution")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


_CFG = {"leverage": 10, "margin_budget": "100", "size_scope_mult": "1.5", "trail_atr_mult": "2.0"}

# EXACT operator dump (snake_case)
AAVE_SNAKE = {
    "symbol": "AAVEUSDT", "hold_side": "short", "size": "6.8", "available": "6.8",
    "leverage": 10, "margin_mode": "crossed", "margin_size": "60.1324",
    "open_price_avg": "88.176176470587", "mark_price": "88.43",
    "break_even_price": "88.075412876978", "unrealized_pnl": "-1.726",
    "create_time": "1782657940370", "update_time": "1782662404025",
}
AAVE_CAMEL = {"symbol": "AAVEUSDT", "holdSide": "short", "size": "6.8",
              "openPriceAvg": "88.176176470587", "markPrice": "88.43"}


def test_notional_reads_snake_case_position():
    n = execution._record_notional(AAVE_SNAKE)
    _assert(n is not None and abs(n - 599.6) < 0.1, "snake_case position notional ~599.6 (was None)")


def test_owned_snake_case_position():
    _assert(execution._runeclaw_sized(AAVE_SNAKE, _CFG) is True, "snake_case AAVE owned (599 < 1500)")


def test_camelcase_still_owned_no_regression():
    _assert(execution._runeclaw_sized(AAVE_CAMEL, _CFG) is True, "camelCase still owned (no regression)")


def test_oversized_still_excluded():
    big = dict(AAVE_SNAKE, size="200")  # 200 * 88 = 17600 > 1500
    _assert(execution._runeclaw_sized(big, _CFG) is False, "oversized position still excluded")


def test_manage_open_state_owns_snake_position():
    # current_position returns the snake record -> it must land in owned -> own1
    execution.features.fetch_klines = lambda *a, **k: []  # trail no-op (no ATR), fine
    _trade.account = types.SimpleNamespace(total_value=lambda **k: {"data": {"contract_assets": []}})
    _trade.contract = types.SimpleNamespace(
        current_position=lambda **k: [AAVE_SNAKE],
        pending_orders=lambda **k: [],
        plan_pending_orders=lambda **k: [],
    )
    _trade.helpers = types.SimpleNamespace(
        contract_position_records=lambda p: (p if isinstance(p, list) else []),
        select_sl_plan_order=lambda *a, **k: None,
        contract_price=lambda s: 88.43,
    )
    _trade.is_success = lambda r: True
    st = execution.manage_open_state(_CFG)
    _assert(st.get("open_count") == 1, "snake position -> own1 (now owned, was own0)")
    _assert("AAVEUSDT" in st.get("filled_symbols", []), "AAVE in filled_symbols (managed)")
    _assert(not st.get("state_blind"), "owned position -> not blind")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} position-ownership tests passed.")
