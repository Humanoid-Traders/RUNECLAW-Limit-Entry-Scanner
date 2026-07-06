"""v0.8.0 stateless realized-loss breaker tests.

The .state-backed equity circuit breaker is dead in the ephemeral runtime
(state_runs stuck at 1 -> day_start_equity never round-trips). v0.8.0 replaces it
with a breaker sourced from exchange fills (trade.contract.fills): pause NEW entries
when trailing-window realized PnL <= -loss_breaker_frac * margin_budget * leverage.
Validated in research/replay_mp (DESIGN_v0.8.0). These tests pin the live wiring:
the fills sum, the window cutoff, snake/camel tolerance, fail-open on a read error,
the manage_open_state trip/no-trip, and the open_if_allowed pause.

Run: python3 tests/test_loss_breaker.py
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


def _fill(profit, age_h, sym="ETHUSDT", profit_key="profit", time_key="cTime"):
    return {"symbol": sym, profit_key: str(profit), time_key: str(_NOW - int(age_h * 3_600_000))}


# ---- _coerce_ms ----

def test_coerce_ms():
    _assert(execution._coerce_ms("1700000000000") == 1700000000000, "13-digit ms unchanged")
    _assert(execution._coerce_ms("1700000000") == 1700000000000, "10-digit seconds -> ms")
    _assert(execution._coerce_ms("not-a-time") is None, "junk -> None")
    _assert(execution._coerce_ms("0") is None, "0 -> None")


# ---- _trailing_realized_pnl ----

def _wire_fills(fills_obj):
    def _fills(**k):
        return fills_obj
    _trade.contract = types.SimpleNamespace(fills=_fills)
    _trade.is_success = lambda r: (bool(r.get("code", 0) == 0) if isinstance(r, dict) else True)


def test_realized_sum_within_window():
    _wire_fills([_fill(-30, 1), _fill(-25, 5), _fill(-40, 48)])  # last one outside 24h
    r = execution._trailing_realized_pnl(24)
    _assert(abs(r - (-55.0)) < 1e-6, "sums only fills inside 24h window -> -55 (the 48h -40 excluded)")


def test_realized_snake_case_key():
    # the v0.6.7 lesson: a snake_case-only profit field must still be read
    _wire_fills([_fill(-50, 2, profit_key="realized_pl")])
    r = execution._trailing_realized_pnl(24)
    _assert(abs(r - (-50.0)) < 1e-6, "snake_case realized_pl is read -> -50")


def test_realized_failopen_on_error():
    def _raise(**k):
        raise RuntimeError("Failed_to_call")
    _trade.contract = types.SimpleNamespace(fills=_raise)
    _trade.is_success = lambda r: True
    _assert(execution._trailing_realized_pnl(24) is None, "fills raises -> None (fail-open)")


def test_realized_failopen_on_empty():
    _wire_fills([])
    _assert(execution._trailing_realized_pnl(24) is None, "no fills -> None (no false pause)")


# ---- manage_open_state integration ----

def _wire_state(fills_obj, frac="0.08"):
    """Flat account (no positions/pending), with a fills history."""
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
    return {"trail_atr_mult": "2.0", "margin_budget": "100", "leverage": 10,
            "loss_breaker_frac": frac, "loss_breaker_window_hours": "24"}


def test_breaker_trips_past_threshold():
    # threshold = 0.08 * 100 * 10 = 80; realized -90 -> trip
    cfg = _wire_state([_fill(-90, 2)])
    st = execution.manage_open_state(cfg)
    _assert(st.get("loss_breaker") is True, "realized -90 <= -80 threshold -> loss_breaker on")
    _assert(st["controls_active"]["loss_breaker"] is True, "controls_active flag set")
    _assert(abs(st.get("realized_window_pnl") - (-90.0)) < 1e-6, "surfaces realized_window_pnl")


def test_breaker_holds_above_threshold():
    cfg = _wire_state([_fill(-50, 2)])  # -50 > -80 -> no trip
    st = execution.manage_open_state(cfg)
    _assert(not st.get("loss_breaker"), "realized -50 above -80 threshold -> no breaker")


def test_breaker_off_when_frac_zero():
    cfg = _wire_state([_fill(-500, 2)], frac="0")  # huge loss but breaker disabled
    st = execution.manage_open_state(cfg)
    _assert(not st.get("loss_breaker"), "frac 0 -> breaker off regardless of loss")
    _assert("realized_window_pnl" not in st, "breaker disabled -> no realized_window_pnl field")
    # journal is on by default and reads fills independently of the breaker
    _assert(st.get("fills_journal"), "journal still emits when breaker is off")


def test_breaker_failopen_unreadable_fills():
    def _raise(**k):
        raise RuntimeError("Failed_to_call")
    cfg = _wire_state([])
    _trade.contract.fills = _raise  # override to raise
    st = execution.manage_open_state(cfg)
    _assert(not st.get("loss_breaker"), "unreadable fills -> no breaker (fail-open, no false pause)")
    _assert(st.get("realized_window_pnl") is None, "realized None recorded")


def test_breaker_empty_window_is_full_headroom_not_blind():
    # v0.9.18: a CLEAN empty fills read (breaker armed, nothing traded in the window)
    # is realized 0 -> full headroom, so _breaker_token reads -b<threshold> (armed,
    # room), NOT the ambiguous -b? a genuine read failure gives. This is the fix for
    # the perpetual -b? on a fresh/quiet deployment (0 fills != read failure).
    cfg = _wire_state([])  # fills(**k) -> [] : a SUCCESSFUL read with no rows
    st = execution.manage_open_state(cfg)
    _assert(st.get("realized_window_pnl") == 0.0,
            "empty-but-readable window -> realized 0.0 (not None)")
    _assert(abs(st.get("loss_breaker_threshold") - 80.0) < 1e-6, "threshold 0.08*100*10 = 80")
    _assert(abs(st.get("loss_breaker_headroom") - 80.0) < 1e-6,
            "headroom = threshold + 0 = 80 -> token reads -b80 (armed, full room), not -b?")
    _assert(not st.get("loss_breaker"), "0 realized never trips")


def test_breaker_read_failure_still_blind():
    # the empty-window fix must NOT mask a broken fills endpoint: a genuine read
    # failure stays realized None -> headroom absent -> -b? (breaker blind, correct).
    def _raise(**k):
        raise RuntimeError("boom")
    cfg = _wire_state([])
    _trade.contract.fills = _raise
    st = execution.manage_open_state(cfg)
    _assert(st.get("realized_window_pnl") is None, "read failure -> realized None (blind)")
    _assert(st.get("loss_breaker_headroom") is None,
            "no headroom when blind -> -b? preserved (blind-detection intact)")


# ---- open_if_allowed pause ----

def test_open_if_allowed_pauses_on_breaker():
    decision = {"symbol": "ETHUSDT", "plan": {"side": "long", "entry": 100.0,
                "sl_price": 98.0, "tp1": 105.0, "margin_usdt": "100"}}
    res = execution.open_if_allowed(decision, {"trail_atr_mult": "2.0"}, {"loss_breaker": True})
    _assert(res.get("placed") is False and res.get("reason") == "loss_breaker",
            "mgmt.loss_breaker -> open refused with reason loss_breaker")


# ---- v0.9.25: stale-window false-blind fix + blind-stage classification ----

def test_stale_window_is_quiet_not_blind():
    # THE v0.9.25 bug fix: fills exist but are ALL OLDER than the 24h window ->
    # that is a QUIET window (realized 0, full headroom, token -b<threshold>),
    # NOT a blind -b?. Pre-fix, _realized_pnl returned None here, so any account
    # whose last fills aged past 24h showed a chronic false-blind breaker.
    cfg = _wire_state([_fill(-40, 48), _fill(25, 60)])   # readable ts, outside window
    st = execution.manage_open_state(cfg)
    _assert(st.get("realized_window_pnl") == 0.0,
            "all fills older than the window -> realized 0.0 (quiet), not None (blind)")
    _assert(abs(st.get("loss_breaker_headroom") - 80.0) < 1e-6,
            "full headroom -> token reads -b80, the never-before-seen number")
    _assert("loss_breaker_blind" not in st, "not blind -> no blind-stage recorded")


def test_blind_stage_read_failure():
    def _raise(**k):
        raise RuntimeError("boom")
    cfg = _wire_state([])
    _trade.contract.fills = _raise
    st = execution.manage_open_state(cfg)
    _assert(st.get("loss_breaker_blind") == "r", "read failure -> blind stage 'r' -> token -b?r")


def test_blind_stage_no_timestamps():
    # rows present but NO row carries a parseable open-time -> can't window -> blind 't'
    cfg = _wire_state([{"symbol": "ETHUSDT", "profit": "-30"}])
    st = execution.manage_open_state(cfg)
    _assert(st.get("realized_window_pnl") is None, "unwindowable rows -> realized None")
    _assert(st.get("loss_breaker_blind") == "t", "no timestamps -> blind stage 't' -> token -b?t")


def test_blind_stage_no_profit_key():
    # in-window fill whose realized PnL sits under an unrecognised key -> blind 'k'
    row = {"symbol": "ETHUSDT", "cTime": str(_NOW - 3_600_000), "weirdPnlField": "-30"}
    cfg = _wire_state([row])
    st = execution.manage_open_state(cfg)
    _assert(st.get("realized_window_pnl") is None, "unreadable profit field -> realized None")
    _assert(st.get("loss_breaker_blind") == "k", "profit key mismatch -> blind stage 'k' -> token -b?k")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} loss-breaker tests passed.")
