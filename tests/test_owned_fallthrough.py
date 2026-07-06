"""v0.9.23 owned-symbol fall-through tests (signal-audit parity fix #1).

The replay harness has ALWAYS excluded already-owned symbols from scoring, so
every validation assumes a blocked best falls through to the next-best name.
Live kept the owned symbol as `best`, died on entry_already_pending, and placed
NOTHING that cycle. These pin the parity fix in build_decision: owned symbols
are withdrawn from CANDIDACY (next-best trades) while staying on the visible
board (digest unchanged), and the filter fails safe when ownership is unknown.

Run: python3 tests/test_owned_fallthrough.py
"""
from _stub import stub_getagent, load_src

stub_getagent()
features = load_src("features")
execution = load_src("execution")   # main_live imports it at module load
risk = load_src("risk")
scoring = load_src("scoring")
ml = load_src("main_live")
ml.runtime.run_id = "test"
SF = features.SymbolFeatures


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


_CFG = {
    "min_score": 70, "max_scan_symbols": 28, "enrich_top_n": 8,
    "kline_interval": "1h", "limit_chase_pct": "3.0", "trail_atr_mult": "2.0",
    "allow_short": True, "min_volume_usdt": "10000000",
    "bidask_full_ratio": "2.0", "bidask_partial_ratio": "1.2", "bidask_wall_ratio": "10.0",
    "max_vwap_ext_pct": "5.0", "trend_weight": "15.0", "breakout_trend_min": "0.7",
    "breakout_extreme_band": "0.015", "funding_skip_bps": "30", "funding_penalty_weight": "8.0",
    "universes": [{"name": "crypto", "leader": "BTCUSDT",
                   "symbols": ["CANDUSDT", "RUNRUSDT"]}],
}


def _fetch(symbol, exchange="bitget"):
    if symbol == "BTCUSDT":                 # leader: up + above VWAP -> long gate
        return SF(symbol, True, last=101.0, vwap=100.0, high=102.0, low=99.0,
                  change_pct=2.0, quote_volume=1e9)
    if symbol == "CANDUSDT":                # stronger momentum, thinner volume -> ~85
        return SF(symbol, True, last=102.0, vwap=100.0, high=103.0, low=95.0,
                  change_pct=6.0, quote_volume=3e7, bid_volume=30.0, ask_volume=10.0)
    return SF(symbol, True, last=102.0, vwap=100.0, high=103.0, low=95.0,     # RUNR ~75
              change_pct=4.0, quote_volume=6e7, bid_volume=30.0, ask_volume=10.0)


def _enrich(feats, cfg, exchange="bitget"):     # aligned strong trend, no funding read
    feats.kline_ok = True
    feats.trend_dir, feats.trend_strength = "long", 0.8
    feats.funding_ok = False
    return feats


def _plan(feats, cfg, size_factor, side="long", entry_mode="pullback"):
    return risk.TradePlan(
        symbol=feats.symbol, side=side, entry=feats.last, atr=1.0,
        sl_price=feats.last * 0.97, sl_pct=0.03, tp1=feats.last * 1.05,
        tp2=feats.last * 1.20, trail_atr=2.0, breakeven_price=feats.last * 1.005,
        notional_usdt=300.0, margin_usdt=30.0, leverage=10,
        size_factor=size_factor, sizing_ok=True, note="", entry_mode=entry_mode)


def _decide(mgmt):
    ml.features.fetch_symbol = _fetch
    ml.features.taker_buy_ratio = lambda s, exchange="bitget": 1.5
    ml.features.enrich = _enrich
    ml.risk.build_plan = _plan
    return ml.build_decision(dict(_CFG), mgmt)


def test_owned_best_falls_through_to_next():
    d = _decide({"circuit": "ok", "owned_symbols": ["CANDUSDT"]})
    _assert(d["action"] == "long" and d["symbol"] == "RUNRUSDT",
            "best (CAND) owned -> the runner-up (RUNR) trades, cycle NOT wasted")
    _assert("CAND" in str(d["meta"].get("scan_digest", "")),
            "owned name STAYS on the visible digest (candidacy withdrawn, not hidden)")


def test_unowned_board_keeps_best():
    d = _decide({"circuit": "ok", "owned_symbols": []})
    _assert(d["action"] == "long" and d["symbol"] == "CANDUSDT",
            "nothing owned -> the true best (CAND) trades, ranking untouched")


def test_all_owned_stands_down():
    d = _decide({"circuit": "ok", "owned_symbols": ["CANDUSDT", "RUNRUSDT"]})
    _assert(d["action"] == "watch",
            "every qualified name owned -> clean stand-down, not an error")


def test_blind_cycle_fails_safe_to_legacy():
    d = _decide({"circuit": "ok", "state_blind": True})   # no owned_symbols key at all
    _assert(d["symbol"] == "CANDUSDT",
            "ownership unknowable -> no filter (legacy best), exec guards backstop")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} owned-fallthrough tests passed.")
