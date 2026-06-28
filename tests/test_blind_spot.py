"""v0.6.6 blind-spot detector tests.

The 12:33 incident: current_position() returned an empty SUCCESS while an ETH short
was live (a flaky-bridge "read lie"), so the playbook read own0 and over-opened on
top. The v0.6.5 interlock only catches read ERRORS, not a successful-but-empty read.
v0.6.6 cross-checks the account: if positions read empty but position margin is still
locked, treat it as blind. These tests use the REAL total_value shape (operator dump).

Run: python3 tests/test_blind_spot.py
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


def _total_value(crossed="0", isolated="0", ok=True):
    """Real total_value shape from the operator's dump."""
    return {
        "code": 0 if ok else 1, "message": "success" if ok else "fail",
        "data": {
            "user_id": "9147408786", "account_type": "CLASSIC",
            "contract_assets": [{
                "marginCoin": "USDT", "locked": "0", "available": "152.57",
                "accountEquity": "152.57", "usdtEquity": "152.57",
                "unrealizedPL": "0", "crossedRiskRate": "0",
                "crossedMargin": crossed, "isolatedMargin": isolated,
            }],
            "spot_assets": [], "unified_assets": [],
        },
    }


# ---- _account_position_margin extraction ----

def test_margin_extraction_open():
    _trade.account = types.SimpleNamespace(total_value=lambda **k: _total_value(crossed="59.93"))
    m = execution._account_position_margin()
    _assert(abs(m - 59.93) < 1e-6, "sums crossedMargin from real shape -> 59.93")


def test_margin_extraction_flat():
    _trade.account = types.SimpleNamespace(total_value=lambda **k: _total_value())  # 0/0
    m = execution._account_position_margin()
    _assert(m == 0.0, "flat account -> 0.0 margin")


def test_margin_extraction_unreadable():
    def _raise(**k):
        raise RuntimeError("Failed_to_call")
    _trade.account = types.SimpleNamespace(total_value=_raise)
    m = execution._account_position_margin()
    _assert(m is None, "total_value raises -> None (no false-positive)")


# ---- manage_open_state integration ----

def _wire(current_position_fn, total_value_obj):
    _trade.account = types.SimpleNamespace(total_value=lambda **k: total_value_obj)
    _trade.contract = types.SimpleNamespace(
        current_position=current_position_fn,
        pending_orders=lambda **k: [],
        plan_pending_orders=lambda **k: [],
    )
    _trade.helpers = types.SimpleNamespace(
        contract_position_records=lambda p: (p if isinstance(p, list) else []),
        select_sl_plan_order=lambda *a, **k: None,
        contract_price=lambda s: 1.0,
    )
    _trade.is_success = lambda r: (bool(r.get("code", 0) == 0) if isinstance(r, dict) else True)


_CFG = {"trail_atr_mult": "2.0"}


def test_blind_spot_detected():
    # positions read empty (success) BUT margin locked -> blind
    _wire(lambda **k: [], _total_value(crossed="59.93"))
    st = execution.manage_open_state(_CFG)
    _assert(st.get("state_blind") is True, "empty positions + locked margin -> state_blind")
    _assert(st.get("blind_reason", "").startswith("pos_margin_"), "records blind_reason")
    _assert(st.get("open_count") == 0, "blind -> open_count 0")


def test_genuinely_flat_not_blind():
    # positions empty AND margin 0 -> genuinely flat, NOT blind
    _wire(lambda **k: [], _total_value())  # 0 margin
    st = execution.manage_open_state(_CFG)
    _assert(not st.get("state_blind"), "empty positions + 0 margin -> NOT blind (flat)")


def test_unreadable_account_no_false_positive():
    # positions empty, total_value raises -> margin None -> NOT blind (fail-open)
    def _raise(**k):
        raise RuntimeError("Failed_to_call")
    _trade_account = types.SimpleNamespace(total_value=_raise)
    _wire(lambda **k: [], None)
    _trade.account = _trade_account  # override with raising total_value
    st = execution.manage_open_state(_CFG)
    _assert(not st.get("state_blind"), "empty positions + unreadable account -> NOT blind")


def test_positions_present_skips_crosscheck():
    # positions read non-empty -> no cross-check needed, not blind
    pos = [{"symbol": "ETHUSDT", "holdSide": "short", "total": "0.56",
            "openPriceAvg": "1583.97", "crossedMargin": "88"}]
    _wire(lambda **k: pos, _total_value(crossed="88"))
    st = execution.manage_open_state(_CFG)
    _assert(not st.get("state_blind"), "positions visible -> not blind")
    _assert(st.get("open_count") == 1, "positions visible -> own1")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} blind-spot tests passed.")
