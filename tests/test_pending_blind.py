"""v0.9.4 (audit S-3) pending-read blind tests.

A failed pending_orders() read used to become pending_records=[] with only a
diagnostic -- resting limits vanished from open_count, so the concurrency cap
and correlation budget under-counted and a redundant entry could be placed
during a bridge outage. The position read already blinded the open-gate
(v0.6.5); these tests pin the same rule for the pending read:

  1. pending_orders RAISES -> state_blind (open_if_allowed refuses).
  2. pending_orders returns a non-success error envelope -> state_blind.
  3. a clean empty pending book does NOT blind (no regression).

Run: python3 tests/test_pending_blind.py
"""
import types

from _stub import stub_getagent, load_src

_trade = stub_getagent()
load_src("features")
execution = load_src("execution")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


_CFG = {"trail_atr_mult": "2.0", "margin_budget": "100", "leverage": 10}


def _wire(pending):
    """Healthy account + positions; `pending` is a callable or a value."""
    _trade.account = types.SimpleNamespace(total_value=lambda **k: {"code": 0, "data": {}})
    pend = pending if callable(pending) else (lambda **k: pending)
    _trade.contract = types.SimpleNamespace(
        current_position=lambda **k: [],
        pending_orders=pend,
        plan_pending_orders=lambda **k: [],
        fills=lambda **k: [],
    )
    _trade.helpers = types.SimpleNamespace(
        contract_position_records=lambda p: (p if isinstance(p, list) else []),
        select_sl_plan_order=lambda *a, **k: None,
        contract_price=lambda s: 1.0,
    )
    _trade.is_success = lambda r: (r.get("code", 0) == 0 if isinstance(r, dict) else True)


def test_pending_raise_blinds():
    def _raise(**k):
        raise RuntimeError("Failed_to_call")
    _wire(_raise)
    st = execution.manage_open_state(_CFG)
    _assert(st.get("state_blind") is True, "pending_orders raises -> state_blind")
    _assert(st.get("pending_error") == "RuntimeError", "error type still surfaced")


def test_pending_error_envelope_blinds():
    _wire({"code": 40012, "message": "service unavailable", "data": None})
    st = execution.manage_open_state(_CFG)
    _assert(st.get("state_blind") is True, "non-success pending envelope -> state_blind")
    _assert("40012" in str(st.get("pending_reason", "")), "exchange code:msg surfaced")


def test_clean_empty_pending_not_blind():
    _wire([])
    st = execution.manage_open_state(_CFG)
    _assert(not st.get("state_blind"), "clean empty pending book -> not blind (no regression)")


def test_open_refused_when_pending_blind():
    def _raise(**k):
        raise RuntimeError("Failed_to_call")
    _wire(_raise)
    mgmt = execution.manage_open_state(_CFG)
    decision = {"symbol": "ETHUSDT", "plan": {"side": "long", "entry": 100.0,
                "sl_price": 98.0, "tp1": 105.0, "margin_usdt": "100"}}
    res = execution.open_if_allowed(decision, _CFG, mgmt)
    _assert(res.get("placed") is False and res.get("reason") == "state_blind",
            "pending-blind cycle -> open refused (cap can no longer overshoot)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} pending-blind tests passed.")
