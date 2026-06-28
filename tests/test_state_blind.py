"""v0.6.5 state-blind interlock tests.

A playbook that cannot read its own positions must NOT open new trades. The 06:32
`Failed_to_call` incident proved the gap: a failed position read becomes records=[]
-> own0 -> open_count 0, and open_if_allowed would place on top of untracked live
positions. These tests prove:

  1. manage_open_state sets state_blind when current_position RAISES.
  2. manage_open_state sets state_blind when current_position returns a non-success
     error envelope (does not raise).
  3. open_if_allowed refuses to open when mgmt.state_blind is set (no wrapper/
     place_order call at all).
  4. a clean read (state_blind unset) still opens normally (no regression).

Run: python3 tests/test_state_blind.py
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


# ---- manage_open_state: blind on read failure ----------------------------------

def _wire_account(current_position_fn):
    """Minimal SDK stub so manage_open_state runs to the position read."""
    _trade.account = types.SimpleNamespace(total_value=lambda **k: types.SimpleNamespace(ok=False))
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
    _trade.is_success = lambda r: bool(getattr(r, "ok", False)) if not isinstance(r, list) else True


def test_blind_when_position_read_raises():
    def _raise(**k):
        raise RuntimeError("Failed_to_call")
    _wire_account(_raise)
    st = execution.manage_open_state({"trail_atr_mult": "2.0"})
    _assert(st.get("state_blind") is True, "raised position read -> state_blind True")
    _assert(st.get("open_count") == 0, "blind read -> open_count 0")
    _assert(st.get("position_query_error") == "RuntimeError", "records the error type")


def test_blind_when_position_read_error_envelope():
    # non-raising error envelope: is_success False
    env = types.SimpleNamespace(ok=False, code="50001", message="Failed_to_call")
    _wire_account(lambda **k: env)
    st = execution.manage_open_state({"trail_atr_mult": "2.0"})
    _assert(st.get("state_blind") is True, "error-envelope position read -> state_blind True")


def test_not_blind_on_clean_empty_read():
    # success envelope that yields no records = genuinely flat, NOT blind
    ok = []  # list -> is_success True, contract_position_records -> []
    _wire_account(lambda **k: ok)
    st = execution.manage_open_state({"trail_atr_mult": "2.0"})
    _assert(not st.get("state_blind"), "clean empty read -> NOT state_blind (genuinely flat)")
    _assert(st.get("open_count") == 0, "flat -> open_count 0")


# ---- open_if_allowed: refuse to open while blind --------------------------------

def _wire_open(rec):
    _trade.contract = types.SimpleNamespace(
        current_position=lambda **k: [],
        pending_orders=lambda **k: [],
        change_leverage=lambda **k: rec.append(("change_leverage", k)),
        place_order=lambda **k: (rec.append(("place_order", k)), types.SimpleNamespace(ok=True))[1],
        open_long_limit=lambda **k: (rec.append(("open_long_limit", k)), types.SimpleNamespace(ok=True))[1],
        open_short_limit=lambda **k: (rec.append(("open_short_limit", k)), types.SimpleNamespace(ok=True))[1],
        open_long_market=lambda **k: (rec.append(("open_long_market", k)), types.SimpleNamespace(ok=True))[1],
        open_short_market=lambda **k: (rec.append(("open_short_market", k)), types.SimpleNamespace(ok=True))[1],
    )
    _trade.helpers = types.SimpleNamespace(
        count_open_contract_positions=lambda *a, **k: 0,
        select_contract_order=lambda *a, **k: None,
        contract_rules=lambda *a, **k: types.SimpleNamespace(price_step=None),
        compute_qty=lambda **k: types.SimpleNamespace(qty="100"),
        resolve_contract_tpsl=lambda **k: types.SimpleNamespace(tp_trigger_price="2.0", sl_trigger_price="1.8"),
    )
    _trade.is_success = lambda r: bool(getattr(r, "ok", False))


_DECISION = {"symbol": "NEARUSDT", "plan": {"side": "long", "entry": 1.9, "sl_price": 1.85,
             "tp1": 2.0, "tp2": 2.19, "margin_usdt": "60", "entry_mode": "pullback"}}
_CFG = {"leverage": 10, "trail_atr_mult": "2.0", "max_concurrent": 3, "max_correlated_alts": 2}


def test_open_blocked_when_blind():
    rec = []; _wire_open(rec)
    out = execution.open_if_allowed(_DECISION, _CFG, {"state_blind": True, "open_count": 0, "open_symbols": []})
    _assert(not out["placed"], "blind -> not placed")
    _assert(out["reason"] == "state_blind", "blind -> reason state_blind")
    _assert(rec == [], "blind -> NO open/place call attempted at all")


def test_open_allowed_when_not_blind():
    rec = []; _wire_open(rec)
    out = execution.open_if_allowed(_DECISION, _CFG, {"open_count": 0, "open_symbols": []})
    _assert(out["placed"], "not blind + room -> places (no regression)")
    _assert(any(n == "open_long_limit" for n, _ in rec), "places via the proven wrapper")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} state-blind tests passed.")
