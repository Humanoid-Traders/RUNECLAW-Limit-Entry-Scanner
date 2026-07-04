"""v0.9.20 vol-regime gate tests.

Pins the opt-in [vol_floor, vol_ceiling] stand-aside that build_decision applies to
the CHOSEN best (mirrors research/replay_mp's single-best gate, so the sweep-validated
threshold transfers to live): a chaos-vol best is stood down (watch vol_regime_<n>); a
calm best trades; the gate is OFF when both bounds are 0 (default); and it FAIL-OPENS
when realized vol is unreadable, so a kline glitch never blocks a trade.

Run: python3 tests/test_vol_gate.py
"""
import types

from _stub import stub_getagent, load_src

stub_getagent()
features = load_src("features")
execution = load_src("execution")   # main_live imports it at module load
risk = load_src("risk")
scoring = load_src("scoring")
ml = load_src("main_live")
ml.runtime.run_id = "test"           # build_decision stamps run_id into meta
SF = features.SymbolFeatures


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


_CFG = {
    "min_score": 70, "max_scan_symbols": 28, "enrich_top_n": 8,
    "kline_interval": "1h", "vol_lookback": 30, "limit_chase_pct": "3.0",
    "trail_atr_mult": "2.0", "allow_short": True, "min_volume_usdt": "10000000",
    "bidask_full_ratio": "2.0", "bidask_partial_ratio": "1.2", "bidask_wall_ratio": "10.0",
    "max_vwap_ext_pct": "5.0", "trend_weight": "15.0", "breakout_trend_min": "0.6",
    "breakout_extreme_band": "0.015", "funding_skip_bps": "30", "funding_penalty_weight": "8.0",
    "universes": [{"name": "crypto", "leader": "BTCUSDT", "symbols": ["CANDUSDT"]}],
}


def _fetch(symbol, exchange="bitget"):
    if symbol == "BTCUSDT":            # leader: up + above VWAP -> long gate
        return SF(symbol, True, last=101.0, vwap=100.0, high=102.0, low=99.0,
                  change_pct=2.0, quote_volume=1e9)
    return SF(symbol, True, last=102.0, vwap=100.0, high=103.0, low=95.0,   # strong long candidate
              change_pct=5.0, quote_volume=5e7, bid_volume=30.0, ask_volume=10.0)


def _enrich(feats, cfg, exchange="bitget"):     # keep the candidate (aligned strong trend, no funding)
    feats.kline_ok = True
    feats.trend_dir, feats.trend_strength = "long", 0.8
    feats.funding_ok = False
    return feats


def _plan(feats, cfg, size_factor, side="long", entry_mode="pullback"):
    # a valid, fillable plan: entry == current price so the staleness skip never fires
    return risk.TradePlan(
        symbol="CANDUSDT", side=side, entry=feats.last, atr=1.0,
        sl_price=feats.last * 0.97, sl_pct=0.03, tp1=feats.last * 1.05,
        tp2=feats.last * 1.20, trail_atr=2.0, breakeven_price=feats.last * 1.005,
        notional_usdt=300.0, margin_usdt=30.0, leverage=10,
        size_factor=size_factor, sizing_ok=True, note="", entry_mode=entry_mode)


def _decide(cfg, vol_value):
    ml.features.fetch_symbol = _fetch
    ml.features.taker_buy_ratio = lambda s, exchange="bitget": 1.5
    ml.features.enrich = _enrich
    ml.features.fetch_klines = lambda *a, **k: [{"close": 1.0}] * 33
    ml.features._closed_bars = lambda bars, interval: bars
    ml.features.realized_vol = lambda bars, lookback=30: vol_value
    ml.risk.build_plan = _plan
    return ml.build_decision(dict(cfg), {"circuit": "ok"})


def test_chaos_vol_best_is_gated():
    cfg = dict(_CFG); cfg["vol_ceiling"] = "200"; cfg["vol_floor"] = "0"
    d = _decide(cfg, 300.0)   # 300% ann vol > 200 ceiling -> stood down
    _assert(d["action"] == "watch", "vol above the ceiling -> watch (not traded)")
    _assert(str(d["meta"]["reason"]) == "vol_regime_300",
            "reason names the vol-regime skip + the reading -> " + str(d["meta"]["reason"]))


def test_calm_vol_best_trades():
    cfg = dict(_CFG); cfg["vol_ceiling"] = "200"; cfg["vol_floor"] = "0"
    d = _decide(cfg, 100.0)   # 100% < 200 ceiling -> allowed
    _assert(d["action"] == "long" and d["symbol"] == "CANDUSDT",
            "vol under the ceiling -> the trade is placed")


def test_gate_off_when_bounds_zero():
    cfg = dict(_CFG); cfg["vol_ceiling"] = "0"; cfg["vol_floor"] = "0"
    d = _decide(cfg, 9999.0)  # extreme vol, but the gate is disabled
    _assert(d["action"] == "long", "both bounds 0 -> gate off, trades regardless of vol")


def test_fail_open_on_unreadable_vol():
    cfg = dict(_CFG); cfg["vol_ceiling"] = "200"; cfg["vol_floor"] = "0"
    d = _decide(cfg, None)    # realized_vol None (kline glitch) -> must NOT block
    _assert(d["action"] == "long", "unreadable vol -> fail-open, trade proceeds")


def test_floor_gates_dead_vol():
    cfg = dict(_CFG); cfg["vol_ceiling"] = "0"; cfg["vol_floor"] = "20"
    d = _decide(cfg, 5.0)     # 5% < 20 floor -> gated (too dead/ranging to trade)
    _assert(d["action"] == "watch" and str(d["meta"]["reason"]) == "vol_regime_5",
            "vol below the floor -> watch (stood down as dead-vol)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} vol-gate tests passed.")
