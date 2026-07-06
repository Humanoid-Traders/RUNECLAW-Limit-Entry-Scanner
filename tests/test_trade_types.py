"""v0.9.22 trade-type exit-pack tests.

Pins the three mechanisms behind per-type trading (all opt-in, all fail-safe to
pre-v0.9.22 behaviour when unarmed):

  1. risk.build_plan: `breakout_tp2_pct` gives breakouts their own tp2 backstop
     width -- the DISTINCT width is also the stateless mode marker.
  2. execution._position_entry_mode: recovers pullback-vs-breakout at manage time
     from the attached TP plan order's distance from entry (exchange-is-the-memory);
     refuses to classify on unarmed marker / unreadable plan / foreign TP widths.
  3. execution._best_effort_position_controls: per-mode hold caps -- an armed
     pullback_time_stop_hours closes an over-age pullback while the same-age
     breakout keeps riding the global cap.
  4. main_live._session_open: RWA session gate -- weekday-window logic, weekends
     closed, malformed spec fail-OPEN.

Run: python3 tests/test_trade_types.py
"""
import datetime as _dt
import types

from _stub import stub_getagent, load_src

stub_getagent()
features = load_src("features")
execution = load_src("execution")
risk = load_src("risk")
scoring = load_src("scoring")
ml = load_src("main_live")
SF = features.SymbolFeatures
# the getagent stub is bare modules; give execution the namespaces tests mock onto
execution.trade.contract = types.SimpleNamespace()
execution.trade.helpers = types.SimpleNamespace()


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def _feats():
    return SF("CANDUSDT", True, last=100.0, vwap=100.0, high=102.0, low=95.0,
              change_pct=3.0, quote_volume=5e7)


_CFG = {"tp2_pct": "20.0", "atr_limit_mult": "0.3", "tp1_pct": "5.0",
        "trail_atr_mult": "2.0", "breakeven_pct": "2.0", "sl_min_alt_pct": "2.5",
        "max_loss_usdt": "15", "leverage": 10, "margin_budget": "100",
        "breakout_level_buffer_pct": "0.2", "breakout_stop_atr_mult": "1.0",
        "breakout_tp1_pct": "4.0"}


# ---- 1. risk: per-mode tp2 backstop ----

def test_breakout_tp2_armed():
    cfg = dict(_CFG); cfg["breakout_tp2_pct"] = "25.0"
    bk = risk.build_plan(_feats(), cfg, 1.0, side="long", entry_mode="breakout")
    pb = risk.build_plan(_feats(), cfg, 1.0, side="long", entry_mode="pullback")
    _assert(abs(bk.tp2 - bk.entry * 1.25) < 1e-9, "armed: breakout tp2 = entry x 1.25")
    _assert(abs(pb.tp2 - pb.entry * 1.20) < 1e-9, "armed: pullback tp2 keeps entry x 1.20")


def test_breakout_tp2_inherits_when_zero():
    bk = risk.build_plan(_feats(), dict(_CFG), 1.0, side="long", entry_mode="breakout")
    _assert(abs(bk.tp2 - bk.entry * 1.20) < 1e-9,
            "unarmed (0/absent): breakout inherits the global tp2_pct")


# ---- 2. execution: stateless mode recovery from the TP plan order ----

def _mode(plan_rows, entry=100.0, side="long", bk2="25.0", tp2="20.0"):
    execution.trade.contract.plan_pending_orders = lambda symbol: plan_rows
    cfg = {"tp2_pct": tp2, "breakout_tp2_pct": bk2}
    return execution._position_entry_mode("CANDUSDT", side, entry, cfg)


def test_mode_recovery_long():
    _assert(_mode([{"triggerPrice": 96.0}, {"triggerPrice": 125.0}]) == "breakout",
            "long: profit-side TP at +25% -> breakout (SL row on the protective side ignored)")
    _assert(_mode([{"triggerPrice": 96.0}, {"triggerPrice": 120.0}]) == "pullback",
            "long: profit-side TP at +20% -> pullback")


def test_mode_recovery_short():
    _assert(_mode([{"triggerPrice": 103.0}, {"triggerPrice": 75.0}], side="short") == "breakout",
            "short: profit side is BELOW entry -> -25% TP reads breakout")


def test_mode_recovery_pullback_marker():
    # the SHIPPED arrangement: pullback carries the distinct width, breakout inherits
    execution.trade.contract.plan_pending_orders = lambda symbol: [{"triggerPrice": 122.0}]
    cfg = {"tp2_pct": "20.0", "pullback_tp2_pct": "22.0"}
    _assert(execution._position_entry_mode("CANDUSDT", "long", 100.0, cfg) == "pullback",
            "pullback-side marker: +22% TP -> pullback")
    execution.trade.contract.plan_pending_orders = lambda symbol: [{"triggerPrice": 120.0}]
    _assert(execution._position_entry_mode("CANDUSDT", "long", 100.0, cfg) == "breakout",
            "pullback-side marker: +20% TP (inherited base) -> breakout")


def test_mode_recovery_refusals():
    _assert(_mode([{"triggerPrice": 96.0}, {"triggerPrice": 125.0}], bk2="0") == "",
            "marker unarmed (bk2=0) -> refuses to classify")
    _assert(_mode([{"triggerPrice": 96.0}]) == "", "no profit-side trigger -> unknown")
    _assert(_mode([{"triggerPrice": 110.0}]) == "",
            "foreign TP width (+10%, near neither 20 nor 25) -> refuses to classify")
    execution.trade.contract.plan_pending_orders = lambda symbol: (_ for _ in ()).throw(RuntimeError("api"))
    cfg = {"tp2_pct": "20.0", "breakout_tp2_pct": "25.0"}
    _assert(execution._position_entry_mode("CANDUSDT", "long", 100.0, cfg) == "",
            "plan fetch raises -> fail-safe unknown (global cap stays in force)")


# ---- 3. execution: per-mode hold caps in the manager ----

def _record(entry=100.0, age_h=5.0):
    now_ms = execution._now_ms()
    return {"symbol": "CANDUSDT", "holdSide": "long", "openPriceAvg": entry,
            "cTime": now_ms - age_h * 3_600_000.0, "unrealizedPL": 0.0}


def _run_controls(cfg, tp_trigger):
    closed = []
    execution.trade.contract.plan_pending_orders = lambda symbol: [{"triggerPrice": tp_trigger}]
    execution.trade.contract.close_position = lambda symbol, hold_side: closed.append(symbol)
    execution.trade.helpers.contract_price = lambda symbol: 100.0
    status = {"controls_active": {"circuit_breaker": False, "time_stop": False,
                                  "auto_be": False, "trail": False}}
    actions = []
    execution._best_effort_position_controls(cfg, [_record()], status, actions)
    return closed, actions, status


_MGMT = {"time_stop_hours": "12", "tp2_pct": "20.0", "breakout_tp2_pct": "25.0",
         "pullback_time_stop_hours": "4", "trail_atr_mult": "0",
         "breakeven_pct": "50.0", "breakeven_trigger_usdt": "99999",
         "universes": [{"name": "crypto", "leader": "BTCUSDT", "symbols": ["CANDUSDT"]}]}


def test_pullback_cap_closes_early():
    closed, actions, status = _run_controls(dict(_MGMT), tp_trigger=120.0)  # width 20% = pullback
    _assert(closed == ["CANDUSDT"], "5h-old pullback > 4h pullback cap -> time-stop close")
    _assert(status["controls_active"]["time_stop"], "controls_active.time_stop set")


def test_breakout_rides_global_cap():
    closed, actions, status = _run_controls(dict(_MGMT), tp_trigger=125.0)  # width 25% = breakout
    _assert(closed == [], "5h-old breakout (no breakout cap armed) -> rides the 12h global cap")


def test_unknown_mode_keeps_global_cap():
    closed, _, _ = _run_controls(dict(_MGMT), tp_trigger=110.0)  # foreign width -> mode unknown
    _assert(closed == [], "unrecoverable mode -> global 12h cap (fail-safe), not the 4h cap")


def test_unarmed_keys_are_pure_noop():
    cfg = dict(_MGMT); cfg["pullback_time_stop_hours"] = "0"
    calls = []
    execution.trade.contract.plan_pending_orders = lambda symbol: calls.append(1) or []
    execution.trade.contract.close_position = lambda symbol, hold_side: calls.append("close")
    execution.trade.helpers.contract_price = lambda symbol: 100.0
    status = {"controls_active": {"time_stop": False}}
    execution._best_effort_position_controls(cfg, [_record()], status, [])
    _assert("close" not in calls, "both keys 0 -> no close and no extra plan-order reads for the cap")


# ---- 4. main_live: RWA session gate ----

class _FakeDT:
    _fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def _sess(spec, iso):
    _FakeDT._fixed = _dt.datetime.fromisoformat(iso).replace(tzinfo=_dt.timezone.utc)
    real = ml.datetime
    ml.datetime = _FakeDT
    try:
        return ml._session_open(spec)
    finally:
        ml.datetime = real


def test_session_gate():
    _assert(_sess("13:30-20:00", "2026-07-06T15:00") is True, "Monday 15:00 UTC in-window -> open")
    _assert(_sess("13:30-20:00", "2026-07-06T02:00") is False, "Monday 02:00 UTC -> closed (overnight)")
    _assert(_sess("13:30-20:00", "2026-07-04T15:00") is False,
            "Saturday -> closed (the weekend-MSTR-grind class)")
    _assert(_sess("13:30-20:00", "2026-07-06T13:30") is True, "window start inclusive")
    _assert(_sess("13:30-20:00", "2026-07-06T20:00") is False, "window end exclusive")
    _assert(_sess("garbage", "2026-07-04T15:00") is True, "malformed spec -> fail-OPEN")

# ---- v0.9.28 candidate: pullback structural stop buffer ----

def test_pullback_stop_buffer():
    """The raw pullback stop sits ON the 24h extreme -- the most hunted price on
    the chart (live 2026-07-06: SL $1804.00, swing top $1804.37, tagged by 37
    cents then a $17 favourable reversal). The buffer pads the stop BEYOND the
    level; sizing solves backward from the wider stop so dollar risk is
    UNCHANGED. 0/absent = bit-exact legacy."""
    f = SF("ETHUSDT", True, last=1770.0, vwap=1769.0, high=1804.0, low=1720.0,
           change_pct=-1.0, quote_volume=1e9)
    cfg = {"tp2_pct": "20", "sl_min_btc_eth_pct": "1.0", "max_loss_usdt": "15",
           "leverage": 10, "margin_budget": "1000", "atr_limit_mult": "0.3"}
    p0 = risk.build_plan(f, dict(cfg), 1.0, side="short")
    cfg["pullback_stop_buffer_pct"] = "0.4"
    p4 = risk.build_plan(f, dict(cfg), 1.0, side="short")
    _assert(p4.sl_price > p0.sl_price, "buffer widens the short stop beyond the 24h high")
    _assert(p4.notional_usdt < p0.notional_usdt, "wider stop -> smaller size (backward sizing)")
    _assert(abs(p0.sl_pct * p0.notional_usdt - 15.0) < 1e-6
            and abs(p4.sl_pct * p4.notional_usdt - 15.0) < 1e-6,
            "dollar risk identical at $15 with and without the buffer")
    cfg["pullback_stop_buffer_pct"] = "0"
    pz = risk.build_plan(f, dict(cfg), 1.0, side="short")
    _assert(abs(pz.sl_price - p0.sl_price) < 1e-9, "buffer 0 -> bit-exact legacy stop")



if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} trade-type tests passed.")
