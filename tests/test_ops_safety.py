"""v0.9.39 ops-safety layer tests: day guard + invariant sentinel + entries_paused.

Pins the three mechanisms (all zero-signal-change):
  1. Account-day guard: realized fills since UTC midnight, UNSCOPED (account
     frame) -- soft threshold WARNS only (day_warn), hard threshold trips the
     circuit (entries halt); fail-open on unreadable fills; the rolling
     loss_breaker is untouched and independent.
  2. Invariant sentinel: clk (per-mode clocks armed, mode unknown), mgn
     (crossed while manifest isolated), sl (stop implies risk > max_loss x
     1.3); observation-only; first breach wins; default on, card-off.
  3. entries_paused: _WATCH_SHORT carries the tail short form; the gate lives
     in build_decision after the circuit check (management always ran first).

Run: python3 tests/test_ops_safety.py
"""
import types
from datetime import datetime, timezone

from _stub import stub_getagent, load_src

_trade = stub_getagent()
features = load_src("features")
execution = load_src("execution")
ml = load_src("main_live")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def _now_ms(hours_ago=0.0):
    return int((datetime.now(timezone.utc).timestamp() - hours_ago * 3600) * 1000)


def _fill(profit, hours_ago):
    return {"symbol": "ETHUSDT", "profit": profit, "cTime": _now_ms(hours_ago),
            "size": 0.3, "price": 1800.0}


def _wire(fills, positions=None, plan=None):
    """Flat-account harness (same shape as the loss-breaker suite)."""
    _trade.account = types.SimpleNamespace(total_value=lambda **k: {"code": 0, "data": {}})
    _trade.contract = types.SimpleNamespace(
        current_position=lambda **k: positions or [],
        pending_orders=lambda **k: [],
        plan_pending_orders=lambda **k: plan or [],
        fills=lambda **k: fills,
    )
    _trade.helpers = types.SimpleNamespace(
        contract_position_records=lambda p: (p if isinstance(p, list) else []),
        select_sl_plan_order=lambda *a, **k: None,
        contract_price=lambda s: 1800.0,
        contract_rules=lambda s: types.SimpleNamespace(price_step=0.01),
    )
    _trade.is_success = lambda r: True
    return {"trail_atr_mult": "0", "margin_budget": "100", "leverage": 10,
            "loss_breaker_frac": "0", "journal_enabled": "false",
            "circuit_pause_usdt": "30", "circuit_stop_usdt": "40",
            "time_stop_hours": "12", "pullback_time_stop_hours": "4",
            "max_loss_usdt": "15", "size_scope_mult": "1.5"}


# hours since UTC midnight right now -- fills placed inside/outside the day
_H = datetime.now(timezone.utc).hour + datetime.now(timezone.utc).minute / 60.0


def test_day_guard_warn_only_at_soft():
    if _H < 0.6:
        print("  ok: (skipped -- too close to UTC midnight for a stable day window)")
        return
    cfg = _wire([_fill(-31.0, min(0.5, _H / 2))])
    st = execution.manage_open_state(cfg)
    _assert(st.get("day_warn") is True, "-$31 day -> Rule-10 WARN flagged")
    _assert(st.get("circuit") == "ok", "soft level never halts -- circuit stays ok")
    _assert(abs(st.get("day_realized") + 31.0) < 1e-6, "day_realized surfaced")


def test_day_guard_trips_at_hard():
    if _H < 0.6:
        print("  ok: (skipped -- too close to UTC midnight)")
        return
    cfg = _wire([_fill(-25.0, min(0.5, _H / 2)), _fill(-16.0, min(0.4, _H / 3))])
    st = execution.manage_open_state(cfg)
    _assert(st.get("circuit") == "tripped", "-$41 day -> Rule-13 halt (circuit tripped)")
    _assert(st.get("day_halt") is True, "day_halt flag set")


def test_day_guard_yesterday_does_not_count():
    if _H > 22.0:
        print("  ok: (skipped -- too close to UTC midnight rollover)")
        return
    cfg = _wire([_fill(-90.0, _H + 1.5)])   # big loss, but BEFORE UTC midnight
    st = execution.manage_open_state(cfg)
    _assert(st.get("circuit") == "ok" and not st.get("day_warn"),
            "yesterday's loss is outside the account-day frame -> no warn, no halt")


def test_day_guard_failopen_on_unreadable_fills():
    def _raise(**k):
        raise RuntimeError("bridge")
    cfg = _wire([])
    _trade.contract.fills = _raise
    st = execution.manage_open_state(cfg)
    _assert(st.get("circuit") == "ok" and "day_realized" not in st,
            "unreadable fills -> no day read -> no false halt (fail-open)")


def _pos(sym="ETHUSDT", size=0.33, entry=1800.0, mm=None):
    r = {"symbol": sym, "hold_side": "long", "total": size, "open_price_avg": entry,
         "unrealized_pnl": 0.0, "cTime": _now_ms(1.0)}
    if mm:
        r["marginMode"] = mm
    return r


def test_sentinel_clk_breach_on_unknown_mode():
    # per-mode clocks armed + plan orders EMPTY -> mode recovery returns "" ->
    # tmode "?" -> the position is on the wrong (global) clock -> confess.
    cfg = _wire([], positions=[_pos()], plan=[])
    st = execution.manage_open_state(cfg)
    breaches = st.get("invariant_breaches") or []
    _assert(any(b.startswith("clk:") for b in breaches),
            "clocks armed + unknown mode -> clk breach: " + str(breaches))


def test_sentinel_mgn_breach_on_crossed():
    plan = [{"symbol": "ETHUSDT", "triggerPrice": 2196.0},   # +22% TP -> mode known
            {"symbol": "ETHUSDT", "triggerPrice": 1791.0}]
    cfg = _wire([], positions=[_pos(mm="crossed")], plan=plan)
    cfg["margin_mode"] = "isolated"
    cfg["pullback_tp2_pct"] = "22"
    cfg["tp2_pct"] = "20"
    st = execution.manage_open_state(cfg)
    breaches = st.get("invariant_breaches") or []
    _assert(any(b.startswith("mgn:") for b in breaches),
            "crossed position under isolated manifest -> mgn breach: " + str(breaches))


def test_sentinel_sl_breach_on_oversized_risk():
    # stop 5% below entry on ~$600 notional -> $30 risk > 15 x 1.3 = $19.5
    plan = [{"symbol": "ETHUSDT", "triggerPrice": 2196.0},
            {"symbol": "ETHUSDT", "triggerPrice": 1710.0}]
    cfg = _wire([], positions=[_pos()], plan=plan)
    cfg["pullback_tp2_pct"] = "22"
    cfg["tp2_pct"] = "20"
    st = execution.manage_open_state(cfg)
    breaches = st.get("invariant_breaches") or []
    _assert(any(b.startswith("sl:") for b in breaches),
            "5%-wide stop on full notional -> sl breach: " + str(breaches))


def test_sentinel_clean_position_no_breach():
    # healthy book: mode known, isolated, stop 1.5% -> risk ~$9 < $19.5
    plan = [{"symbol": "ETHUSDT", "triggerPrice": 2196.0},
            {"symbol": "ETHUSDT", "triggerPrice": 1773.0}]
    cfg = _wire([], positions=[_pos(mm="isolated")], plan=plan)
    cfg["margin_mode"] = "isolated"
    cfg["pullback_tp2_pct"] = "22"
    cfg["tp2_pct"] = "20"
    st = execution.manage_open_state(cfg)
    _assert(not st.get("invariant_breaches"),
            "healthy position -> sentinel silent (no false confessions)")


def test_sentinel_card_off():
    cfg = _wire([], positions=[_pos()], plan=[])
    cfg["invariant_sentinel"] = "0"
    st = execution.manage_open_state(cfg)
    _assert(not st.get("invariant_breaches"), "sentinel card-disabled -> no checks run")


def test_entries_paused_short_form_and_cx_tokens():
    _assert(ml._WATCH_SHORT.get("entries_paused") == "paused",
            "entries_paused maps to tail short 'paused'")
    # cx-slot assembly: day warn + first invariant breach ride the cx token
    cbx = ml._circuit_state_token({"controls_active": {}})
    _assert(cbx == "", "healthy state -> empty cx")


def test_config_overrides_detection():
    # v0.9.40: default config -> no overrides reported (silence when clean)
    _assert(ml._config_overrides({}) == {}, "shipped defaults -> empty overrides")
    _assert(ml._config_overrides({"loss_breaker_frac": "0.018"}) == {},
            "explicit-but-equal value -> not an override")
    ovr = ml._config_overrides({"loss_breaker_frac": "0.05", "entries_paused": "1"})
    _assert(ovr == {"loss_breaker_frac": "0.05", "entries_paused": "1"},
            "card-tuned frac + safe mode both surfaced: " + str(ovr))
    _assert(ml._config_overrides({"extra_symbols": ["EVAAUSDT"]}) ==
            {"extra_symbols": ["EVAAUSDT"]}, "list-typed override surfaced")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} ops-safety tests passed.")
