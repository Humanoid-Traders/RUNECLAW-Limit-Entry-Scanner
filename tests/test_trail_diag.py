"""v0.6.4 trail_diag tests: prove _trail_stop records WHY it acted / no-op'd into
diag['trail'] at every exit, so a silently-inert trail is diagnosable. The key case
is modify_err -- the suspected live failure (modify_stop_loss raising in hedge mode)
must surface a reason instead of a bare fail-safe no-op.

Run: python3 tests/test_trail_diag.py
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


def _wire(sl_order, modify_raises=False):
    """Stub the SDK surface _trail_stop touches; fix ATR via the features module."""
    execution.features.fetch_klines = lambda *a, **k: [{"high": 1, "low": 1, "close": 1}]
    execution.features._wilder_atr = lambda bars, period: 0.03413  # NEAR live ATR

    def _modify(**kw):
        if modify_raises:
            raise RuntimeError("hedge_mode posSide required")
        return types.SimpleNamespace(ok=True)

    _trade.contract = types.SimpleNamespace(
        plan_pending_orders=lambda **k: [sl_order],
        modify_stop_loss=_modify,
    )
    _trade.helpers = types.SimpleNamespace(
        select_sl_plan_order=lambda plan, **k: plan[0] if plan else None,
        contract_rules=lambda *a, **k: types.SimpleNamespace(price_step=None),
    )


_CFG = {"trail_atr_mult": "2.0", "atr_period": 14, "kline_interval": "1h"}


def _run(current, sl_order, modify_raises=False):
    _wire(sl_order, modify_raises)
    diag, actions, status = {}, [], {"controls_active": {}}
    moved = execution._trail_stop("NEARUSDT", "long", current, _CFG, actions, status, diag)
    return moved, diag.get("trail"), actions


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


# NEAR live shape: triggerPrice present, dictifiable
_SL = {"orderId": "1454849391757078529", "triggerPrice": "1.7776", "planType": "loss_plan"}


def test_ratchet_success_records_set():
    moved, reason, actions = _run(1.8907, _SL)
    _assert(moved is True, "ratchet returns True")
    _assert(reason.startswith("set:"), "success records set:<price> -> " + str(reason))
    _assert(any("trail_stop" in a for a in actions), "trail_stop action appended")


def test_modify_raise_is_visible():
    # THE live-suspect case: SL readable, geometry says ratchet, but modify raises.
    moved, reason, _ = _run(1.8907, _SL, modify_raises=True)
    _assert(moved is False, "modify failure -> no move (fail-safe)")
    _assert(reason.startswith("modify_err:"), "modify failure is NAMED, not silent -> " + str(reason))


def test_geometry_hold_records_reason():
    # current low enough that trail (current-2*ATR) <= cur_sl
    moved, reason, _ = _run(1.80, _SL)
    _assert(moved is False, "no improvement -> no move")
    _assert(reason.startswith("hold:"), "geometry no-op records hold: -> " + str(reason))


def test_no_sl_order_records_reason():
    moved, reason, _ = _run(1.8907, {"triggerPrice": "1.7776"})  # no orderId
    _assert(moved is False, "no order id -> no move")
    _assert(reason == "no_sl_order", "missing SL order id named -> " + str(reason))


def test_attribute_fallback_reads_trigger():
    # SL exposes trigger only as an attribute (no dict) -> fallback must still read it
    obj = types.SimpleNamespace(order_id="999", triggerPrice="1.7776")
    moved, reason, _ = _run(1.8907, obj)
    _assert(moved is True, "attribute-only SL still ratchets via fallback")
    _assert(reason.startswith("set:"), "attribute fallback path sets -> " + str(reason))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} trail_diag tests passed.")
