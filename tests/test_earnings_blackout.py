"""v0.9.30 per-symbol earnings blackout tests.

The macro event blackout knows FOMC/CPI; it does not know MSTR reports tonight.
These pin the new guard: the day-granular window math (_earnings_window), the
fail-open contract at every layer, and the _scan_universe wiring (an
earnings-flagged symbol loses CANDIDACY while its scores stay on the board and
the runner-up trades).

Run: python3 tests/test_earnings_blackout.py
"""
from datetime import datetime, timezone

from _stub import stub_getagent, load_src

stub_getagent()
features = load_src("features")
execution = load_src("execution")
risk = load_src("risk")
scoring = load_src("scoring")
ml = load_src("main_live")
ml.runtime.run_id = "test"
SF = features.SymbolFeatures


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def _ms(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000.0


# ---- pure windowing helper ----

_ROW = {"symbol": "MSTR", "report_date": "2026-07-07", "reporting_time": "after market close"}


def test_window_covers_report_day_and_pad():
    _assert(features._earnings_window([_ROW], "MSTRUSDT", _ms("2026-07-07T12:00"), 4.0) is not None,
            "mid report day -> blackout")
    _assert(features._earnings_window([_ROW], "MSTRUSDT", _ms("2026-07-06T21:00"), 4.0) is not None,
            "prior evening inside the 4h pre-pad -> blackout")
    _assert(features._earnings_window([_ROW], "MSTRUSDT", _ms("2026-07-08T03:00"), 4.0) is not None,
            "post-close spillover inside the 4h post-pad -> blackout")
    _assert(features._earnings_window([_ROW], "MSTRUSDT", _ms("2026-07-06T10:00"), 4.0) is None,
            "well before the window -> clear")
    _assert(features._earnings_window([_ROW], "MSTRUSDT", _ms("2026-07-08T09:00"), 4.0) is None,
            "well after the window -> clear")


def test_window_failopen_paths():
    _assert(features._earnings_window([_ROW], "TSLAUSDT", _ms("2026-07-07T12:00"), 4.0) is None,
            "different symbol's report -> no blackout for TSLA")
    bad = {"symbol": "MSTR", "report_date": "not-a-date"}
    _assert(features._earnings_window([bad], "MSTRUSDT", _ms("2026-07-07T12:00"), 4.0) is None,
            "unparseable report_date -> row ignored (fail-open)")
    _assert(features._earnings_window([_ROW], "MSTRUSDT", _ms("2026-07-07T12:00"), 0.0) is None,
            "pad 0 -> guard off")


def test_fetch_failopen_when_sdk_absent():
    # the stub getagent.data has no equity namespace -> the read raises -> None
    cfg = {"earnings_blackout_hours": "4"}
    _assert(features.earnings_blackout("MSTRUSDT", cfg) is None,
            "missing/renamed SDK endpoint -> fail-open, no blackout")
    _assert(features.earnings_blackout("MSTRUSDT", {"earnings_blackout_hours": "0"}) is None,
            "hours 0 -> short-circuits off before any read")


# ---- _scan_universe wiring: candidacy withdrawn, runner-up trades ----

_CFG = {
    "min_score": 70, "max_scan_symbols": 28, "enrich_top_n": 8,
    "kline_interval": "1h", "limit_chase_pct": "3.0", "trail_atr_mult": "2.0",
    "allow_short": True, "min_volume_usdt": "10000000",
    "bidask_full_ratio": "2.0", "bidask_partial_ratio": "1.2", "bidask_wall_ratio": "10.0",
    "max_vwap_ext_pct": "5.0", "trend_weight": "15.0", "breakout_trend_min": "0.7",
    "breakout_extreme_band": "0.015", "funding_skip_bps": "30", "funding_penalty_weight": "8.0",
    "earnings_blackout_hours": "4",
    "universes": [{"name": "crypto", "leader": "BTCUSDT",
                   "symbols": ["CANDUSDT", "RUNRUSDT"], "earnings_blackout": True}],
}


def _fetch(symbol, exchange="bitget"):
    if symbol == "BTCUSDT":
        return SF(symbol, True, last=101.0, vwap=100.0, high=102.0, low=99.0,
                  change_pct=2.0, quote_volume=1e9)
    if symbol == "CANDUSDT":
        return SF(symbol, True, last=102.0, vwap=100.0, high=103.0, low=95.0,
                  change_pct=6.0, quote_volume=3e7, bid_volume=30.0, ask_volume=10.0)
    return SF(symbol, True, last=102.0, vwap=100.0, high=103.0, low=95.0,
              change_pct=4.0, quote_volume=6e7, bid_volume=30.0, ask_volume=10.0)


def _enrich(feats, cfg, exchange="bitget"):
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


def test_flagged_symbol_loses_candidacy_runner_up_trades():
    ml.features.fetch_symbol = _fetch
    ml.features.taker_buy_ratio = lambda s, exchange="bitget": 1.5
    ml.features.enrich = _enrich
    ml.risk.build_plan = _plan
    ml.features.earnings_blackout = (
        lambda sym, cfg: {"symbol": "CAND"} if sym == "CANDUSDT" else None)
    d = ml.build_decision(dict(_CFG), {"circuit": "ok", "owned_symbols": []})
    _assert(d["action"] == "long" and d["symbol"] == "RUNRUSDT",
            "best (CAND) in earnings blackout -> runner-up (RUNR) trades")
    _assert(d["metrics"].get("earnings_blackout_symbols") == ["CANDUSDT"],
            "stood-down symbol surfaced in metrics")
    _assert("CAND" in str(d["meta"].get("scan_digest", "")),
            "flagged name STAYS on the visible digest (candidacy withdrawn, not hidden)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} earnings-blackout tests passed.")
