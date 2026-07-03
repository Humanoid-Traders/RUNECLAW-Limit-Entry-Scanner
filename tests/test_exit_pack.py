"""v0.9.7 exit-pack tests: step-lock ladder, scale-out, breaker observability.

Pins the three shipped pieces of the --ab-exitpack validation round:
  ladder  -- `steplock` rungs lift the protective floor as the move extends; the
             highest armed rung wins, malformed rungs are ignored, the
             inside-market guard still applies, shorts mirror.
  scaleout -- OPT-IN partial close at scaleout_trigger_pct: frac 0 is a no-op,
             the direction is pinned (long -> SELL close), the already-trimmed
             guard reads exchange fills (stateless), and every unreadable input
             (open time, fills, qty) is a fail-closed no-op -- never a blind order.
  breaker  -- manage_open_state emits loss_breaker_threshold/headroom so the
             operator never re-derives them (the recurring equity*frac misread).

Run: python3 tests/test_exit_pack.py
"""
import types
from datetime import datetime, timezone

from _stub import stub_getagent, load_src

trade = stub_getagent()
features = load_src("features")
execution = load_src("execution")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


_NOW = int(datetime.now(timezone.utc).timestamp() * 1000)
_ENTRY = 100.0
_LADDER_CFG = {"trail_atr_mult": "2.0", "atr_period": 14, "kline_interval": "1h",
               "breakeven_pct": "2.0", "breakeven_lock_pct": "1.5",
               "steplock": "2:1.5,4:3,6:4.5"}


# ---- step-lock ladder (via _trail_stop; ATR 5 -> raw trail rides 10 behind) ----

def _wire_sl(cur_sl, atr=5.0):
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
    execution.features.fetch_klines = lambda symbol, interval="1h", limit=30: [1] * 30
    execution.features._closed_bars = lambda bars, interval: bars  # v0.9.13: pass-through the mocked ATR pipeline
    execution.features._wilder_atr = lambda bars, period: atr
    return cap


def _trail(current, cfg=None, cur_sl=97.5, side="long", be_armed=True):
    cap = _wire_sl(cur_sl)
    diag = {}
    moved = execution._trail_stop("ALTUSDT", side, current, cfg or _LADDER_CFG,
                                  [], {"controls_active": {}}, diag,
                                  entry=_ENTRY, be_armed=be_armed)
    return moved, cap["modify"], diag


def test_ladder_rung_two_beats_single_lock():
    # +4.2%: raw trail 104.2-10 = 94.2 (useless); single lock = 101.5; rung 4:3
    # lifts the floor to 103.0 -- the ladder banks more than v0.9.6 alone could.
    moved, modify, diag = _trail(current=104.2)
    _assert(moved and modify is not None, "rung 2 armed -> stop moves")
    _assert(abs(float(modify["trigger_price"]) - 103.0) < 1e-9,
            "floor = entry*(1+3%) from the 4:3 rung (not the 1.5 single lock)")


def test_highest_armed_rung_wins():
    moved, modify, _ = _trail(current=106.5)
    _assert(moved and abs(float(modify["trigger_price"]) - 104.5) < 1e-9,
            "+6.5% arms the 6:4.5 rung -> floor 104.5")


def test_unarmed_move_keeps_pure_trail():
    # +1% arms nothing (be_armed False, below every rung): trail = 101-10 < cur SL
    moved, modify, diag = _trail(current=101.0, be_armed=False)
    _assert(not moved and modify is None, "below all arms -> pure trail no-op")
    _assert("be_lock" not in diag, "no lock trace when nothing armed")


def test_malformed_ladder_ignored():
    cfg = dict(_LADDER_CFG); cfg["steplock"] = "junk,4:x,:3,5"
    moved, modify, _ = _trail(current=103.0, cfg=cfg)
    _assert(moved and abs(float(modify["trigger_price"]) - 101.5) < 1e-9,
            "malformed rungs skipped; the be_lock fallback (101.5) still applies")


def test_rung_lock_above_market_suppressed():
    # arm 2 with an aggressive 5%% lock: at +3%% the lock px (105) is ABOVE market
    # (103) -> the inside-market guard must suppress it (never self-trigger).
    cfg = dict(_LADDER_CFG); cfg["steplock"] = "2:5"; cfg["breakeven_lock_pct"] = "0"
    moved, modify, _ = _trail(current=103.0, cfg=cfg, be_armed=False)
    _assert(not moved and modify is None, "above-market rung lock suppressed")


def test_ladder_short_mirrors():
    # short from 100, mark 95.8 (+4.2%): rung 4:3 -> floor 97.0, tighter than SL 102
    cap = _wire_sl(cur_sl=102.0)
    moved = execution._trail_stop("ALTUSDT", "short", 95.8, _LADDER_CFG,
                                  [], {"controls_active": {}}, {}, entry=_ENTRY, be_armed=True)
    _assert(moved and abs(float(cap["modify"]["trigger_price"]) - 97.0) < 1e-9,
            "short mirror: floor = entry*(1-3%) = 97.0")


# ---- scale-out (direct _scale_out; fail-closed contracts) ----

def _pos_record(open_age_min=60.0, size=1.52):
    return {"symbol": "TSLAUSDT", "holdSide": "long", "total": str(size),
            "open_price_avg": str(_ENTRY),
            "cTime": str(_NOW - int(open_age_min * 60_000))}


def _wire_so(fills, place_capture):
    def _place(**k):
        place_capture["order"] = k
        return {"code": 0}
    trade.contract = types.SimpleNamespace(fills=lambda **k: fills, place_order=_place)
    trade.is_success = lambda r: (r.get("code", 1) == 0 if isinstance(r, dict) else True)


_OTHER_FILL = {"symbol": "ETHUSDT", "profit": "1",
               "cTime": str(_NOW - 5 * 3_600_000)}  # unrelated symbol, hours old


def _run_so(current, cfg_over=None, fills=None, record=None):
    cap = {"order": None}
    # NB: an EMPTY fills book fail-closes (_read_fills returns None on no rows --
    # an empty success can be a parse lie, the pT0 lesson). Live there is always
    # at least the entry fill, so tests wire a benign unrelated fill by default.
    _wire_so([_OTHER_FILL] if fills is None else fills, cap)
    cfg = {"scaleout_frac": "0.5", "scaleout_trigger_pct": "3.5",
           "margin_mode": "crossed"}
    cfg.update(cfg_over or {})
    actions, diag = [], {}
    execution._scale_out("TSLAUSDT", "long", current, _ENTRY, record or _pos_record(),
                         cfg, actions, {"controls_active": {}}, diag)
    return cap["order"], actions, diag


def test_scaleout_default_off():
    order, actions, _ = _run_so(current=110.0, cfg_over={"scaleout_frac": "0"})
    _assert(order is None and not actions, "frac 0 (the ship default) -> never places")


def test_scaleout_below_trigger_noop():
    order, actions, _ = _run_so(current=102.0)  # +2% < 3.5% trigger
    _assert(order is None and not actions, "below the trigger -> no trim")


def test_scaleout_places_pinned_close():
    order, actions, diag = _run_so(current=104.0)  # +4% >= 3.5%
    _assert(order is not None, "armed + untrimmed -> order placed")
    _assert(order["side"] == "sell" and order["trade_side"] == "close"
            and order["pos_side"] == "long" and order["order_type"] == "market",
            "direction pinned: closing a long is a SELL close on pos_side long")
    _assert(abs(float(order["qty"]) - 0.76) < 1e-9, "qty = size * frac = 1.52*0.5")
    _assert(actions and "scale_out" in actions[0], "action surfaced")
    _assert(str(diag.get("so", "")).startswith("trimmed"), "diag records the trim")


def test_scaleout_qty_aligned_via_compute_qty():
    # v0.9.13: half a lot-aligned position size is generally NOT itself lot-aligned,
    # and Bitget rejects an unaligned contract qty (the armed reduce was silently
    # rejected on most symbols). The reduce is now aligned by reusing compute_qty;
    # when it returns an aligned lot, the placed qty is that lot, not the raw 0.76.
    cap = {"order": None}
    _wire_so([_OTHER_FILL], cap)
    trade.helpers = types.SimpleNamespace(
        compute_qty=lambda **k: types.SimpleNamespace(qty="0.7"))   # aligned lot != raw 0.76
    actions, diag = [], {}
    execution._scale_out("TSLAUSDT", "long", 104.0, _ENTRY, _pos_record(),
                         {"scaleout_frac": "0.5", "scaleout_trigger_pct": "3.5",
                          "margin_mode": "crossed", "leverage": 10},
                         actions, {"controls_active": {}}, diag)
    _assert(cap["order"] is not None and cap["order"]["qty"] == "0.7",
            "reduce qty is the compute_qty-aligned lot (not the raw 1.52*0.5=0.76)")
    _assert(actions and actions[0]["qty"] == "0.7", "action records the aligned qty")


def test_scaleout_already_trimmed_guard():
    newer = {"symbol": "TSLAUSDT", "profit": "5",
             "cTime": str(_NOW - int(10 * 60_000))}  # fill 10min ago, open 60min ago
    order, actions, diag = _run_so(current=104.0, fills=[newer])
    _assert(order is None and not actions, "fill newer than open -> no re-trim (no Zeno)")
    _assert(diag.get("so") == "already_trimmed", "guard reason surfaced")


def test_scaleout_entry_fill_not_a_trim():
    entry_fill = {"symbol": "TSLAUSDT", "profit": "0",
                  "cTime": str(_NOW - int(59.5 * 60_000))}  # ~= the open time
    order, _, _ = _run_so(current=104.0, fills=[entry_fill])
    _assert(order is not None, "the entry fill itself (2-min grace) does not block the trim")


def test_scaleout_failclosed_unreadables():
    # unreadable fills -> no order
    cap = {"order": None}
    def _raise(**k):
        raise RuntimeError("down")
    trade.contract = types.SimpleNamespace(fills=_raise,
                                           place_order=lambda **k: cap.__setitem__("order", k))
    trade.is_success = lambda r: True
    diag = {}
    execution._scale_out("TSLAUSDT", "long", 104.0, _ENTRY, _pos_record(),
                         {"scaleout_frac": "0.5"}, [], {"controls_active": {}}, diag)
    _assert(cap["order"] is None and diag.get("so") == "fills_unreadable",
            "unreadable fills -> fail-closed no-op")
    # unreadable open time -> no order
    rec = _pos_record(); rec.pop("cTime")
    order, _, diag2 = _run_so(current=104.0, record=rec)
    _assert(order is None and diag2.get("so") == "no_open_ts",
            "unreadable open time -> fail-closed no-op")


def test_scaleout_reject_surfaced():
    cap = {"order": None}
    trade.contract = types.SimpleNamespace(
        fills=lambda **k: [_OTHER_FILL],
        place_order=lambda **k: {"code": 400172, "msg": "qty precision"})
    trade.is_success = lambda r: (r.get("code", 1) == 0 if isinstance(r, dict) else True)
    actions, diag = [], {}
    execution._scale_out("TSLAUSDT", "long", 104.0, _ENTRY, _pos_record(),
                         {"scaleout_frac": "0.5"}, actions, {"controls_active": {}}, diag)
    _assert(not actions and str(diag.get("so", "")).startswith("reject:"),
            "exchange reject surfaced in diag, no action logged, never retried blind")


# ---- breaker observability (manage_open_state emits its own arithmetic) ----

def _wire_state(fills_obj, frac="0.08"):
    trade.account = types.SimpleNamespace(total_value=lambda **k: {"code": 0, "data": {}})
    trade.contract = types.SimpleNamespace(
        current_position=lambda **k: [], pending_orders=lambda **k: [],
        plan_pending_orders=lambda **k: [], fills=lambda **k: fills_obj)
    trade.helpers = types.SimpleNamespace(
        contract_position_records=lambda p: (p if isinstance(p, list) else []),
        select_sl_plan_order=lambda *a, **k: None, contract_price=lambda s: 1.0)
    trade.is_success = lambda r: (bool(r.get("code", 0) == 0) if isinstance(r, dict) else True)
    return {"trail_atr_mult": "2.0", "margin_budget": "100", "leverage": 10,
            "loss_breaker_frac": frac, "loss_breaker_window_hours": "24"}


def test_breaker_arithmetic_emitted():
    fills = [{"symbol": "NVDAUSDT", "profit": "-12.08",
              "cTime": str(_NOW - 2 * 3_600_000)}]
    st = execution.manage_open_state(_wire_state(fills))
    _assert(abs(st.get("loss_breaker_threshold", 0) - 80.0) < 1e-9,
            "threshold emitted: 0.08*100*10 = 80 (frac*margin*lev, NOT equity*frac)")
    _assert(abs(st.get("loss_breaker_headroom", 0) - 67.92) < 1e-9,
            "headroom emitted: 80 - 12.08 = 67.92 (the July-2 SITREP number, machine-computed)")


def test_breaker_arithmetic_absent_when_off():
    st = execution.manage_open_state(_wire_state([], frac="0"))
    _assert("loss_breaker_threshold" not in st and "loss_breaker_headroom" not in st,
            "frac 0 -> no breaker fields (nothing misleading emitted)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} exit-pack tests passed.")
