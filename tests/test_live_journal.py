"""v0.9.1 Phase-4 live journal tests.

The journal emits closed-trade realized records (from trade.contract.fills, the same
read the loss breaker uses) so live results can be reconciled against the backtest --
the audit's missing live-vs-backtest feedback loop. Realized PnL only: live MAE/MFE
needs an intra-trade high-water track the stateless runtime can't keep. These tests
pin the journal build (fields, window, cap, sort) + the manage_open_state wiring.

Run: python3 tests/test_live_journal.py
"""
import sys
import time
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


_NOW = int(time.time() * 1000)


def _fill(profit, age_h, sym="ETHUSDT", side="buy", tid="t1",
          profit_key="profit", id_key="tradeId", side_key="side"):
    return {"symbol": sym, profit_key: str(profit), side_key: side, id_key: tid,
            "cTime": str(_NOW - int(age_h * 3_600_000))}


# ---- _fills_journal ----

def test_journal_fields_and_window():
    rows = [_fill(-30, 1, tid="a"), _fill(25, 5, sym="INJUSDT", side="sell", tid="b"),
            _fill(-40, 48, tid="c")]  # last one outside 24h
    j = execution._fills_journal(rows, 24)
    _assert(len(j) == 2, "window excludes the 48h-old fill -> 2 records")
    ids = [r["id"] for r in j]
    _assert("c" not in ids, "old fill 'c' excluded")
    rec = [r for r in j if r["id"] == "a"][0]
    _assert(rec["sym"] == "ETHUSDT" and rec["side"] == "buy" and rec["profit"] == -30.0,
            "record carries sym/side/profit")


def test_journal_sorted_recent_first():
    rows = [_fill(1, 10, tid="old"), _fill(2, 1, tid="new"), _fill(3, 5, tid="mid")]
    j = execution._fills_journal(rows, 24)
    _assert([r["id"] for r in j] == ["new", "mid", "old"], "sorted most-recent first")


def test_journal_cap():
    rows = [_fill(1, i * 0.1, tid=str(i)) for i in range(80)]
    j = execution._fills_journal(rows, 24, cap=50)
    _assert(len(j) == 50, "journal capped at 50 records")


def test_journal_snake_case_fields():
    rows = [_fill(-12, 2, side="short", tid="x", id_key="trade_id", side_key="pos_side")]
    j = execution._fills_journal(rows, 24)
    _assert(j[0]["id"] == "x" and j[0]["side"] == "short", "snake_case trade_id/pos_side read")


# ---- manage_open_state wiring ----

def _wire_state(fills_obj, cfg_extra=None):
    _trade.account = types.SimpleNamespace(total_value=lambda **k: {"code": 0, "data": {}})
    _trade.contract = types.SimpleNamespace(
        current_position=lambda **k: [],
        pending_orders=lambda **k: [],
        plan_pending_orders=lambda **k: [],
        fills=lambda **k: fills_obj,
    )
    _trade.helpers = types.SimpleNamespace(
        contract_position_records=lambda p: (p if isinstance(p, list) else []),
        select_sl_plan_order=lambda *a, **k: None,
        contract_price=lambda s: 1.0,
    )
    _trade.is_success = lambda r: (bool(r.get("code", 0) == 0) if isinstance(r, dict) else True)
    cfg = {"trail_atr_mult": "2.0", "loss_breaker_frac": "0"}
    if cfg_extra:
        cfg.update(cfg_extra)
    return cfg


def test_journal_emitted_by_default():
    cfg = _wire_state([_fill(-15, 1, tid="z")])
    st = execution.manage_open_state(cfg)
    _assert(st.get("fills_journal"), "journal on by default -> fills_journal present")
    _assert(st["fills_journal"][0]["id"] == "z", "journal carries the fill record")


def test_journal_disabled():
    called = {"n": 0}
    def _fills(**k):
        called["n"] += 1
        return [_fill(-15, 1)]
    cfg = _wire_state([])
    _trade.contract.fills = _fills
    cfg["journal_enabled"] = "false"   # and frac 0
    # v0.9.39: the account-day guard (circuit_pause/stop_usdt, default 30/40)
    # also consumes the single fills read, so "no consumers" now additionally
    # requires the day guard off. With ALL THREE off, fills() is never called.
    cfg["circuit_pause_usdt"] = "0"
    cfg["circuit_stop_usdt"] = "0"
    st = execution.manage_open_state(cfg)
    _assert("fills_journal" not in st, "journal disabled -> no fills_journal")
    _assert(called["n"] == 0, "journal + breaker + day guard all off -> fills() never called")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} live-journal tests passed.")
