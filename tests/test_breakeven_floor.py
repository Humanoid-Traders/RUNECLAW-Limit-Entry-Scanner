"""v0.9.6 breakeven-floor-under-the-trail tests.

Pins the fix for the live 2026-07-02 MSTR give-back: a long peaked +4.37% but the
2*ATR trail still sat below entry, so a would-be flat stopped at -$3.75. Once
breakeven is armed, `_trail_stop` must floor the protective stop at
`breakeven_lock_pct` the other side of entry -- BUT only when that price is already
inside the market, so the upnl-armed path can never place an above-market stop that
self-triggers at a worse fill. `breakeven_lock_pct: 0` must be a byte-for-byte no-op
(pure trail, the pre-v0.9.6 behaviour).

Run: python3 tests/test_breakeven_floor.py
"""
import types

from _stub import stub_getagent, load_src

trade = stub_getagent()
features = load_src("features")
execution = load_src("execution")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


# ---- live SDK surface stub: a ratcheted SL plan order + a capturing modify ----

def _wire(cur_sl, atr=2.47):
    """Install the exchange-side reads `_trail_stop` makes and capture any
    modify_stop_loss call. Returns the mutable capture dict."""
    cap = {"modify": None}
    sl_order = {"order_id": "SL1", "triggerPrice": cur_sl}
    trade.contract = types.SimpleNamespace(
        plan_pending_orders=lambda **k: [sl_order],
        modify_stop_loss=lambda **k: cap.__setitem__("modify", k),
    )
    trade.helpers = types.SimpleNamespace(
        select_sl_plan_order=lambda plan, **k: sl_order,
        contract_rules=lambda s: types.SimpleNamespace(price_step=None),
    )
    # ATR is recomputed live off klines; pin both so the trail geometry is exact.
    execution.features.fetch_klines = lambda symbol, interval="1h", limit=30: [1] * 30
    execution.features._wilder_atr = lambda bars, period: atr
    return cap


# MSTR geometry: entry 99.23, ATR 2.47, trail_atr_mult 2.0 -> 2*ATR = 4.94.
_CFG = {"trail_atr_mult": "2.0", "atr_period": 14, "kline_interval": "1h",
        "breakeven_pct": "2.0"}
_ENTRY = 99.23


def _run(cur_sl, current, be_armed, lock, atr=2.47):
    cfg = dict(_CFG); cfg["breakeven_lock_pct"] = lock
    cap = _wire(cur_sl, atr)
    actions, status, diag = [], {"controls_active": {}}, {}
    moved = execution._trail_stop("MSTRUSDT", "long", current, cfg,
                                  actions, status, diag, entry=_ENTRY, be_armed=be_armed)
    return moved, cap["modify"], diag


def test_armed_lock_lifts_stop_above_entry():
    # price pulled back to 101.5 from the peak; the 2*ATR trail alone = 96.56 (BELOW
    # entry and below the ratcheted 98.63 SL -> pure trail would HOLD and later give
    # it all back). The 1.5% lock floors the stop at 99.23*1.015 = 100.72, inside the
    # market (< 101.5), so the stop lifts into profit -- the flat is banked.
    moved, modify, diag = _run(cur_sl=98.63, current=101.5, be_armed=True, lock="1.5")
    _assert(moved is True, "armed lock moves the stop")
    _assert(modify is not None, "modify_stop_loss was called")
    _assert(abs(float(modify["trigger_price"]) - _ENTRY * 1.015) < 1e-6,
            "stop lifted to entry * (1 + lock) = 100.72 (banked profit, not below entry)")
    _assert(abs(diag.get("be_lock", 0) - _ENTRY * 1.015) < 1e-6, "be_lock surfaced in diag")


def test_lock_zero_is_pure_trail_noop():
    # same geometry, lock 0: the floor is skipped, the raw 2*ATR trail (96.56) is
    # below the live SL, so nothing moves -- byte-for-byte the pre-v0.9.6 behaviour.
    moved, modify, diag = _run(cur_sl=98.63, current=101.5, be_armed=True, lock="0")
    _assert(moved is False, "lock 0 -> pure trail, no lift here")
    _assert(modify is None, "lock 0 never calls modify on a sub-SL trail")
    _assert("be_lock" not in diag, "lock 0 leaves no be_lock trace")


def test_not_armed_does_not_lock():
    # breakeven not yet armed -> the floor must not apply even with a lock configured.
    moved, modify, _ = _run(cur_sl=98.63, current=101.5, be_armed=False, lock="1.5")
    _assert(moved is False and modify is None,
            "un-armed position keeps the pure trail (no premature breakeven lock)")


def test_above_market_lock_is_suppressed():
    # the guard: current is only 99.5 (just above entry), so a 1.5% lock (100.72)
    # would sit ABOVE market and self-trigger at a worse fill. inside-market check
    # must suppress it -> no modify. This is the upnl-armed-early safety case.
    moved, modify, diag = _run(cur_sl=98.63, current=99.5, be_armed=True, lock="1.5")
    _assert(moved is False and modify is None,
            "lock above current price is suppressed (never an above-market stop)")
    _assert("be_lock" not in diag, "suppressed lock leaves no be_lock trace")


def test_lock_only_ever_tightens():
    # if the live SL is already ABOVE the lock (100.72), the lock must not loosen it.
    moved, modify, _ = _run(cur_sl=101.0, current=101.5, be_armed=True, lock="1.5")
    _assert(moved is False and modify is None,
            "a tighter existing stop is never loosened toward the lock")


def test_short_side_mirrors():
    # short entry 99.23, price pulled back UP to 97.0 from a low; 2*ATR trail = 101.94
    # (above entry). 1.5% lock floors at 99.23*0.985 = 97.74, inside market (> 97.0),
    # tighter than the 100.0 live SL -> stop pulls down into profit.
    cfg = dict(_CFG); cfg["breakeven_lock_pct"] = "1.5"
    cap = _wire(cur_sl=100.0)
    diag = {}
    moved = execution._trail_stop("MSTRUSDT", "short", 97.0, cfg, [],
                                  {"controls_active": {}}, diag, entry=_ENTRY, be_armed=True)
    _assert(moved is True and cap["modify"] is not None, "short lock moves the stop")
    _assert(abs(float(cap["modify"]["trigger_price"]) - _ENTRY * 0.985) < 1e-6,
            "short stop floored at entry * (1 - lock) = 97.74")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} breakeven-floor tests passed.")
