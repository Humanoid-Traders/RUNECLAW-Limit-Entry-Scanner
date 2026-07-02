"""v0.9.4 (audit S-2 / S-4) ownership-scope tests.

Size-scoped ownership alone adopts ANY account trade under the ~$1,500 envelope,
so a small manual position could be time-stop-closed or cancel-pruned by the
bot, and manual fills distorted the loss breaker / journal. These tests pin the
two scoping rules:

  S-2  destructive management (time-stop close, limit cancel) acts ONLY on
       symbols the bot can open (universe candidates minus leaders); out-of-set
       records still COUNT toward the caps, and an ABSENT universe config means
       no restriction (a missing cfg must never disable the trail -- v0.6.7).
  S-4  fills with a readable notional above the ownership envelope are dropped
       from the breaker/journal read; unreadable-size fills are KEPT
       (fail-conservative: a shape change can never blind the breaker).

Run: python3 tests/test_ownership_scope.py
"""
import time
import types

from _stub import stub_getagent, load_src

_trade = stub_getagent()
load_src("features")
execution = load_src("execution")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


_NOW = int(time.time() * 1000)

_CFG = {"leverage": 10, "margin_budget": "100", "size_scope_mult": "1.5",
        "trail_atr_mult": "0", "time_stop_hours": "12", "limit_expiry_hours": "4",
        "limit_chase_pct": "0", "breakeven_pct": "2.0", "breakeven_trigger_usdt": "20",
        "trading_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "universes": [{"name": "crypto", "leader": "BTCUSDT"},
                      {"name": "metals", "leader": "XAUUSDT", "symbols": ["XAGUSDT"]}]}


# ---- _managed_symbols ----

def test_managed_symbols_excludes_leaders():
    syms = execution._managed_symbols(_CFG)
    _assert(syms == {"ETHUSDT", "SOLUSDT", "XAGUSDT"},
            "universe candidates minus leaders (BTC/XAU excluded)")


def test_managed_symbols_empty_cfg_means_no_restriction():
    _assert(execution._managed_symbols({}) == set(),
            "no symbol lists -> empty set (callers treat as unrestricted)")


# ---- S-2: destructive management scoped ----

def _pos(sym, age_h=20.0):
    return {"symbol": sym, "hold_side": "long", "size": "1", "available": "1",
            "open_price_avg": "100.0", "unrealized_pnl": "0.5",
            "create_time": str(_NOW - int(age_h * 3_600_000))}


def _wire(closed, cancelled):
    _trade.account = types.SimpleNamespace(total_value=lambda **k: {"code": 0, "data": {}})
    _trade.contract = types.SimpleNamespace(
        current_position=lambda **k: [],
        pending_orders=lambda **k: [],
        plan_pending_orders=lambda **k: [],
        fills=lambda **k: [],
        close_position=lambda symbol, hold_side: closed.append(symbol),
        cancel_order=lambda symbol, order_id: cancelled.append(symbol),
    )
    _trade.helpers = types.SimpleNamespace(
        contract_position_records=lambda p: (p if isinstance(p, list) else []),
        select_sl_plan_order=lambda *a, **k: None,
        contract_price=lambda s: 100.0,
    )
    _trade.is_success = lambda r: True


def test_time_stop_skips_manual_out_of_universe_position():
    closed, cancelled = [], []
    _wire(closed, cancelled)
    status = {"controls_active": {}}
    actions = []
    # DOGEUSDT is small enough to be size-owned but NOT in any universe:
    # aged 20h > 12h time-stop, it must NOT be closed. ETH (in-universe) must be.
    execution._best_effort_position_controls(
        _CFG, [_pos("DOGEUSDT"), _pos("ETHUSDT")], status, actions)
    _assert(closed == ["ETHUSDT"], "time-stop closes in-universe ETH only (manual DOGE untouched)")
    doge = [d for d in status["position_diag"] if d["sym"] == "DOGEUSDT"][0]
    _assert(doge.get("note") == "unmanaged_symbol", "manual position named unmanaged_symbol in diag")


def test_limit_expiry_skips_out_of_universe_order():
    closed, cancelled = [], []
    _wire(closed, cancelled)
    recs = [{"symbol": "DOGEUSDT", "order_id": "1", "price": "100", "size": "1",
             "create_time": str(_NOW - 6 * 3_600_000)},
            {"symbol": "ETHUSDT", "order_id": "2", "price": "100", "size": "1",
             "create_time": str(_NOW - 6 * 3_600_000)}]
    actions = []
    execution._best_effort_limit_expiry(_CFG, recs, actions, {})
    _assert(cancelled == ["ETHUSDT"], "expiry cancels in-universe ETH only (manual DOGE limit kept)")


def test_no_universe_cfg_manages_everything():
    # v0.6.7 lesson: a missing universe config must never disable management.
    closed, cancelled = [], []
    _wire(closed, cancelled)
    cfg = {k: v for k, v in _CFG.items() if k not in ("trading_symbols", "universes")}
    execution._best_effort_position_controls(cfg, [_pos("DOGEUSDT")], {"controls_active": {}}, [])
    _assert(closed == ["DOGEUSDT"], "no symbol cfg at all -> unrestricted management (fail-safe)")


def test_out_of_universe_still_counts_toward_caps():
    closed, cancelled = [], []
    _wire(closed, cancelled)
    _trade.contract.current_position = lambda **k: [_pos("DOGEUSDT", age_h=1.0)]
    st = execution.manage_open_state(_CFG)
    _assert(st.get("open_count") == 1 and "DOGEUSDT" in st.get("owned_symbols", []),
            "manual small DOGE still counts toward concurrency/correlation (conservative)")


# ---- S-4: fills scoping ----

def _fill(profit, age_h=1.0, sym="ETHUSDT", **extra):
    row = {"symbol": sym, "profit": str(profit),
           "cTime": str(_NOW - int(age_h * 3_600_000))}
    row.update(extra)
    return row


def test_oversized_fill_excluded_from_breaker_and_journal():
    closed, cancelled = [], []
    _wire(closed, cancelled)
    # manual whale fill: 100 @ 100 = 10,000 notional > 1,500 envelope -> dropped;
    # bot-sized losses alone (-90) still trip the 0.08*100*10 = -80 threshold.
    _trade.contract.fills = lambda **k: [
        _fill(500, price="100", size="100"),          # manual win would mask the losses
        _fill(-50, price="100", size="5"),            # bot-sized loss (500 notional)
        _fill(-40, price="100", size="4")]
    cfg = dict(_CFG, loss_breaker_frac="0.08", loss_breaker_window_hours="24",
               journal_enabled="true", journal_window_hours="24")
    st = execution.manage_open_state(cfg)
    _assert(abs(st.get("realized_window_pnl") - (-90.0)) < 1e-6,
            "manual +500 whale fill excluded -> window PnL -90 (was +410, breaker blinded)")
    _assert(st.get("loss_breaker") is True, "breaker trips on bot-sized losses alone")
    syms = {r["sym"] for r in st.get("fills_journal", [])}
    _assert(len(st.get("fills_journal", [])) == 2, "journal carries only the 2 scoped fills")


def test_unreadable_size_fill_kept_failconservative():
    closed, cancelled = [], []
    _wire(closed, cancelled)
    _trade.contract.fills = lambda **k: [_fill(-90)]  # no price/size -> notional unreadable
    cfg = dict(_CFG, loss_breaker_frac="0.08", loss_breaker_window_hours="24")
    st = execution.manage_open_state(cfg)
    _assert(st.get("loss_breaker") is True,
            "unreadable-size fill KEPT -> a shape change can never blind the breaker")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} ownership-scope tests passed.")
