"""v0.9.4 risk.build_plan tests — UNION of two parallel audit efforts.

Two audits independently wrote the first direct unit tests for risk.build_plan
(PR #33 branch: the loss-at-stop invariant; PR #34 branch: geometry goldens).
The suites are complementary with zero name collisions, so this file keeps
every assertion from both:

  invariant suite:  loss at the stop = notional * sl_pct <= max_loss_usdt,
                    budget cap may only REDUCE risk, side mirroring,
                    breakout wider-of stop, size_factor scaling.
  geometry suite:   entry placement goldens (VWAP -/+ k*ATR; breakout at
                    market), SL always on the losing side, max(structure,
                    tier-floor) both entry modes and both cases, kline-ATR
                    preference over the range proxy, fail-closed None paths.

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
        "margin_budget": "100", "breakout_level_buffer_pct": "0.2",
        "breakout_stop_atr_mult": "1.0", "breakout_tp1_pct": "4.0"}


def _feats(**kw):
    # geometry fixture: 24h range 95-105, VWAP 100 -> proxy ATR = 10/2.5 = 4.0
    d = dict(last=102.0, vwap=100.0, high=105.0, low=95.0)
    d.update(kw)
    return SF("ALTUSDT", True, **d)


def _sf(symbol="ALTUSDT", last=100.0, vwap=100.0, high=101.0, low=99.5):
    # invariant fixture: tight range so the tier floor dominates
    return SF(symbol=symbol, ok=True, last=last, vwap=vwap, high=high, low=low,
              change_pct=1.0, quote_volume=50e6)


# ============ the loss-at-stop invariant suite (audit T-1 / PR #33) ============

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
    f = _sf(low=90.0)  # deep 24h low -> structural stop wider than the 2.5% floor
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


# ============ the geometry-golden suite (audit fix #4 / PR #34) ============

def test_long_pullback_geometry():
    p = risk.build_plan(_feats(), _CFG, 1.0, side="long")
    _assert(abs(p.entry - 98.8) < 1e-9, "entry = VWAP - 0.3*ATR = 100 - 1.2 = 98.8")
    _assert(p.sl_price < p.entry, "long SL is BELOW entry (losing side)")
    _assert(abs(p.sl_price - 95.0) < 1e-6, "structure stop lands at the 24h low (95)")
    _assert(p.tp1 > p.entry and p.tp2 > p.tp1, "long TPs above entry, laddered")
    expected_sl_pct = (98.8 - 95.0) / 98.8
    _assert(abs(p.sl_pct - expected_sl_pct) < 1e-12, "sl_pct from structure (> the 2.5%% floor)")


def test_long_floor_beats_tight_structure():
    # NB: low also feeds the proxy ATR ((105-98.5)/2.5 = 2.6), so entry moves too
    p = risk.build_plan(_feats(low=98.5), _CFG, 1.0, side="long")   # structure ~0.7% < floor
    _assert(abs(p.sl_pct - 0.025) < 1e-12, "alt 2.5%% floor wins over tighter structure")
    _assert(abs(p.sl_price - p.entry * 0.975) < 1e-9, "SL = entry * (1 - floor)")


def test_short_pullback_geometry():
    p = risk.build_plan(_feats(), _CFG, 1.0, side="short")
    _assert(abs(p.entry - 101.2) < 1e-9, "entry = VWAP + 0.3*ATR = 101.2")
    _assert(p.sl_price > p.entry, "short SL is ABOVE entry (losing side)")
    _assert(p.tp1 < p.entry and p.tp2 < p.tp1, "short TPs below entry, laddered")
    expected_sl_pct = (105.0 - 101.2) / 101.2   # structure to the 24h high
    _assert(abs(p.sl_pct - max(expected_sl_pct, 0.025)) < 1e-12, "short stop = max(structure, floor)")


def test_breakout_short_stop_above():
    p = risk.build_plan(_feats(last=95.2), _CFG, 1.0, side="short", entry_mode="breakout")
    _assert(p.entry == 95.2 and p.entry_mode == "breakout", "breakout enters at market (last)")
    _assert(p.sl_price > p.entry, "breakout-short SL above entry")
    _assert(p.sl_pct >= 0.025, "breakout stop respects the tier floor")


def test_breakout_long_stop_below():
    p = risk.build_plan(_feats(last=104.9), _CFG, 1.0, side="long", entry_mode="breakout")
    _assert(p.sl_price < p.entry, "breakout-long SL below entry")
    _assert(p.tp1 > p.entry, "breakout-long TP above entry")


def test_backward_from_stop_sizing():
    p = risk.build_plan(_feats(), _CFG, 1.0, side="long")
    _assert(abs(p.notional_usdt - 15.0 / p.sl_pct) < 1e-6,
            "notional = max_loss / sl_pct (dollar risk is the control variable)")
    _assert(abs(p.margin_usdt - p.notional_usdt / 10) < 1e-9, "margin = notional / leverage")
    _assert(p.sizing_ok and p.note == "", "uncapped plan sizes clean")


def test_wider_stop_means_smaller_size():
    tight = risk.build_plan(_feats(low=98.5), _CFG, 1.0, side="long")   # floor 2.5%
    wide = risk.build_plan(_feats(low=90.0), _CFG, 1.0, side="long")    # structure ~8.9%
    _assert(wide.sl_pct > tight.sl_pct and wide.notional_usdt < tight.notional_usdt,
            "wider stop -> smaller position at the SAME dollar risk (the live MSTR case)")


def test_fail_closed_and_atr_preference():
    _assert(risk.build_plan(_feats(vwap=None), _CFG, 1.0) is None, "missing VWAP -> None")
    f = _feats(); f.kline_ok = True; f.atr = 2.0            # real Wilder ATR beats proxy 4.0
    p = risk.build_plan(f, _CFG, 1.0, side="long")
    _assert(abs(p.entry - (100.0 - 0.3 * 2.0)) < 1e-9, "kline ATR preferred over range proxy")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} risk-plan tests passed.")
