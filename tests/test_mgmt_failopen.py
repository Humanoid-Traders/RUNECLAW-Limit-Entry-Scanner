"""v0.9.4 (audit S-1) management-crash fail-open tests.

The old run() fallback for a crashed manage_open_state was {"circuit": "ok"} --
no state_blind, open_count 0 -- which open_if_allowed read as a healthy flat
book: the concurrency cap, the correlation budget, and the loss breaker were
all silently disabled while a new order was still placed. These tests pin the
fix: only a manage_open_state that ran to completion may authorize opens.

  1. _safe_manage returns state_blind when manage_open_state RAISES.
  2. _safe_manage returns state_blind when not in follow-trade mode.
  3. _safe_manage passes a healthy snapshot through untouched.
  4. open_if_allowed refuses to place against the crash fallback.

Run: python3 tests/test_mgmt_failopen.py
"""
import types

from _stub import stub_getagent, load_src

_trade = stub_getagent()
load_src("features")
execution = load_src("execution")
load_src("scoring")
load_src("risk")
main_live = load_src("main_live")
main_live.execution = execution  # bind the orchestrator to the same instance


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def test_crash_fallback_is_blind():
    def _boom(cfg):
        raise RuntimeError("Failed_to_call")
    main_live.execution = types.SimpleNamespace(manage_open_state=_boom)
    mgmt = main_live._safe_manage({}, follow=True)
    _assert(mgmt.get("state_blind") is True, "manage_open_state raises -> fallback is state_blind")
    _assert(mgmt.get("mgmt_error") == "RuntimeError", "crash type surfaced in mgmt_error")
    main_live.execution = execution


def test_not_follow_is_blind():
    mgmt = main_live._safe_manage({}, follow=False)
    _assert(mgmt.get("state_blind") is True, "management never ran (not follow) -> state_blind")


def test_healthy_snapshot_passes_through():
    healthy = {"circuit": "ok", "open_count": 1, "open_symbols": ["ETHUSDT"]}
    main_live.execution = types.SimpleNamespace(manage_open_state=lambda cfg: healthy)
    mgmt = main_live._safe_manage({}, follow=True)
    _assert(mgmt is healthy and not mgmt.get("state_blind"),
            "completed manage_open_state passes through unblinded")
    main_live.execution = execution


def test_open_refused_against_crash_fallback():
    def _boom(cfg):
        raise RuntimeError("Failed_to_call")
    main_live.execution = types.SimpleNamespace(manage_open_state=_boom)
    mgmt = main_live._safe_manage({}, follow=True)
    main_live.execution = execution
    decision = {"symbol": "ETHUSDT", "plan": {"side": "long", "entry": 100.0,
                "sl_price": 98.0, "tp1": 105.0, "margin_usdt": "100"}}
    res = execution.open_if_allowed(decision, {"trail_atr_mult": "2.0"}, mgmt)
    _assert(res.get("placed") is False and res.get("reason") == "state_blind",
            "crash fallback -> open refused with reason state_blind (was: placed with 0 gates)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} mgmt-failopen tests passed.")
