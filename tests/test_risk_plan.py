"""v0.9.4 (audit T-1) risk-engine sizing tests -- the first direct unit tests
for risk.build_plan. They pin the one invariant every dollar depends on:

    loss at the stop  =  notional * sl_pct  <=  max_loss_usdt

plus the per-tier stop floors, the margin-budget cap (which may only REDUCE
risk), side mirroring, the breakout wider-of stop, and size_factor scaling.

Run: python3 tests/test_risk_plan.py
"""
from _stub import stub_getagent, load_src

stub_getagent()
features = load_src("features")
risk = load_src("risk")

SF = features.SymbolFeatures


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


_CFG = {"atr_limit_mult": "0.3", "tp1_pct": "5.0", "tp2_pct": "15.0",
        "trail_atr_mult": "2.0", "breakeven_pct": "2.0",
        "sl_min_btc_eth_pct": "1.5", "sl_min_sol_bnb_pct": "1.2",
        "sl_min_alt_pct": "2.5", "max_loss_usdt": "15", "leverage": 10,
        "margin_budget": "100"}


def _sf(symbol="ALTUSDT", last=100.0, vwap=100.0, high=101.0, low=99.5):
    return SF(symbol=symbol, ok=True, last=last, vwap=vwap, high=high, low=low,
              change_pct=1.0, quote_volume=50e6)


def test_loss_at_stop_equals_max_loss_uncapped():
    # alt floor 2.5% dominates the tight structure -> notional = 15/0.025 = 600,
    # margin 60 < budget 100 -> uncapped; loss at the stop is exactly max_loss.
    plan = risk.build_plan(_sf(), _CFG, size_factor=1.0, side="long")
    _assert(plan is not None and plan.sizing_ok, "plan builds")
    _assert(abs(plan.sl_pct - 0.025) < 1e-9, "alt stop floored at 2.5%")
    loss_at_stop = plan.notional_usdt * plan.sl_pct
    _assert(abs(loss_at_stop - 15.0) < 1e-6, "loss at stop == max_loss_usdt (the invariant)")
    _assert(abs(plan.margin_usdt - 60.0) < 1e-6, "margin = notional/leverage = 60")


def test_budget_cap_only_reduces_risk():
    # SOL floor 1.2% -> notional 15/0.012 = 1250 -> margin 125 > budget 100 ->
    # capped to notional 1000; loss at stop drops to 12 (< max_loss, never above).
    plan = risk.build_plan(_sf("SOLUSDT"), _CFG, size_factor=1.0, side="long")
    _assert(plan.note == "capped_by_margin_budget", "cap noted")
    _assert(abs(plan.margin_usdt - 100.0) < 1e-6 and abs(plan.notional_usdt - 1000.0) < 1e-6,
            "capped to margin_budget * leverage")
    _assert(plan.notional_usdt * plan.sl_pct <= 15.0 + 1e-6,
            "budget cap can only REDUCE loss at stop")


def test_structural_stop_used_when_wider_than_floor():
    # deep 24h low -> structural stop wider than the 2.5% floor
    f = _sf(low=90.0)  # entry ~= vwap - 0.3*atr; atr proxy = (high-low)/2.5
    plan = risk.build_plan(f, _CFG, size_factor=1.0, side="long")
    _assert(plan.sl_pct > 0.025, "structural stop (to the 24h low) wider than the floor is kept")
    _assert(plan.sl_price < plan.entry, "long stop below entry")


def test_short_mirrors():
    plan = risk.build_plan(_sf(), _CFG, size_factor=1.0, side="short")
    _assert(plan.entry > _sf().vwap, "short limit rests above VWAP")
    _assert(plan.sl_price > plan.entry, "short stop above entry")
    _assert(plan.tp1 < plan.entry and plan.tp2 < plan.tp1, "short TPs below entry, tp2 wider")
    _assert(abs(plan.notional_usdt * plan.sl_pct - 15.0) < 1e-6,
            "short obeys the same loss-at-stop invariant")


def test_size_factor_scales_notional():
    full = risk.build_plan(_sf(), _CFG, size_factor=1.0, side="long")
    half = risk.build_plan(_sf(), _CFG, size_factor=0.5, side="long")
    _assert(abs(half.notional_usdt - full.notional_usdt / 2.0) < 1e-6,
            "half-size regime halves the notional")


def test_breakout_stop_takes_wider_of_structure_and_atr():
    # long breakout at the session high; ATR (proxy (high-low)/2.5 = 4) makes the
    # vol stop (entry - 4) wider than the structure stop just below the high.
    f = SF(symbol="ALTUSDT", ok=True, last=110.0, vwap=100.0, high=110.0, low=100.0,
           change_pct=9.0, quote_volume=50e6)
    plan = risk.build_plan(f, _CFG, size_factor=1.0, side="long", entry_mode="breakout")
    _assert(plan.entry_mode == "breakout" and abs(plan.entry - 110.0) < 1e-9,
            "breakout enters at market (last)")
    atr = (110.0 - 100.0) / 2.5
    expected_sl_pct = (110.0 - (110.0 - atr)) / 110.0  # vol stop wider than structure
    _assert(abs(plan.sl_pct - max(expected_sl_pct, 0.025)) < 1e-9,
            "breakout stop = wider of structure/ATR, floored")
    _assert(plan.notional_usdt * plan.sl_pct <= 15.0 + 1e-6,
            "breakout obeys the loss-at-stop invariant")


def test_unusable_features_return_none():
    bad = SF(symbol="ALTUSDT", ok=False)
    _assert(risk.build_plan(bad, _CFG, 1.0) is None, "not-ok features -> no plan")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} risk-plan tests passed.")
