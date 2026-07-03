"""v0.9.4 scoring-engine golden tests (audit fix #5).

Pins the regime gate boundaries (2+ signals -> full size, 1 -> half, 0 ->
none; allow_short honored), the 5-dimension blended score (a maxed candidate
scores exactly 100), every hard skip (no_data / thin_volume / opposing wall /
overextension), the v0.5.0 breakout routing, and enrich_score (trend
bonus/penalty arithmetic, breakout confirmation, funding crowding skip and
soft penalty). These are the audit's "missing data must never become a
direction" guarantees, pinned.

Run: python3 tests/test_scoring.py
"""
import sys
import types
from pathlib import Path

_g = types.ModuleType("getagent"); sys.modules["getagent"] = _g
for _s in ("data", "trade", "runtime"):
    _m = types.ModuleType("getagent." + _s); setattr(_g, _s, _m)
    sys.modules["getagent." + _s] = _m
_SRC = Path(__file__).resolve().parent.parent / "src"
_pkg = types.ModuleType("src"); _pkg.__path__ = [str(_SRC)]; sys.modules["src"] = _pkg

import importlib.util  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SRC / (name.split(".")[-1] + ".py"))
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


features = _load("src.features")
scoring = _load("src.scoring")
SF = features.SymbolFeatures


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


_CFG = {"allow_short": True, "min_volume_usdt": "10000000", "bidask_full_ratio": "2.0",
        "bidask_partial_ratio": "1.2", "bidask_wall_ratio": "10.0",
        "max_vwap_ext_pct": "4.0", "trend_weight": "15.0",
        "breakout_trend_min": "0.6", "breakout_extreme_band": "0.015",
        "funding_skip_bps": "30", "funding_penalty_weight": "8.0"}


# ---- regime gate ----

def test_regime_full_half_none():
    up3 = SF("BTCUSDT", True, last=101.0, vwap=100.0, change_pct=2.0)
    r = scoring.regime(up3, 1.2, _CFG)          # up + above + taker buy = 3
    _assert(r.direction == "long" and r.size_factor == 1.0, "3 long signals -> long, full size")
    up1 = SF("BTCUSDT", True, last=99.0, vwap=100.0, change_pct=2.0)
    r = scoring.regime(up1, None, _CFG)          # up only (below VWAP, no taker)
    _assert(r.direction == "long" and r.size_factor == 0.5, "1 long signal -> long, HALF size")
    flat = SF("BTCUSDT", True, last=100.0, vwap=100.0, change_pct=0.0)
    r = scoring.regime(flat, None, _CFG)
    _assert(r.direction == "none" and r.size_factor == 0.0, "no signals -> none, size 0")


def test_regime_short_and_allow_short():
    dn3 = SF("BTCUSDT", True, last=99.0, vwap=100.0, change_pct=-2.0)
    r = scoring.regime(dn3, 0.8, _CFG)           # down + below + taker sell = 3
    _assert(r.direction == "short" and r.size_factor == 1.0, "3 short signals -> short, full")
    cfg = dict(_CFG); cfg["allow_short"] = False
    r = scoring.regime(dn3, 0.8, cfg)
    _assert(r.direction == "none", "allow_short=False blocks a full short gate")


# ---- score_universe: dimensions + hard skips ----

def _btc():
    return SF("BTCUSDT", True, last=100.0, vwap=100.0, change_pct=0.0, quote_volume=1e9)


def _cand(sym="AAAUSDT", **kw):
    d = dict(ok=True, last=102.0, vwap=100.0, high=103.0, low=95.0, change_pct=5.0,
             quote_volume=5e7, bid_volume=30.0, ask_volume=10.0)
    d.update(kw)
    return SF(sym, d.pop("ok"), **d)


def test_perfect_long_scores_100():
    # strongest rel-strength, above VWAP, top third of range, bid/ask 3.0, top volume
    weak = _cand("BBBUSDT", last=95.5, change_pct=-1.0, quote_volume=2e7,
                 bid_volume=10.0, ask_volume=10.0)
    rows = scoring.score_universe([_cand(), weak], _btc(), _CFG, "long")
    best = [r for r in rows if r.symbol == "AAAUSDT"][0]
    _assert(best.score == 100.0, "maxed candidate scores exactly 100 -> " + str(best.score))
    _assert(not best.skip and best.entry_mode == "pullback", "qualified, pullback mode")


def test_missing_data_scores_zero():
    rows = scoring.score_universe([SF("XUSDT", False, note="no_data")], _btc(), _CFG, "long")
    _assert(rows[0].score == 0.0 and rows[0].skip and rows[0].skip_reason == "no_data",
            "ok=False -> score 0 + skip (missing data NEVER becomes a direction)")


def test_hard_skips():
    thin = _cand("THINUSDT", quote_volume=5e6)      # < $10M floor
    rows = scoring.score_universe([thin], _btc(), _CFG, "long")
    _assert(rows[0].skip and rows[0].skip_reason == "thin_volume", "thin volume -> hard skip")
    wall = _cand("WALLUSDT", bid_volume=1.0, ask_volume=15.0)   # ask/bid 15 >= 10
    rows = scoring.score_universe([wall], _btc(), _CFG, "long")
    _assert(rows[0].skip and rows[0].skip_reason == "ask_wall", "opposing wall -> hard skip")


def test_overextension_skip_vs_breakout_routing():
    ext = _cand("EXTUSDT", last=105.0, vwap=100.0, high=105.5)  # +5% > 4% cap
    rows = scoring.score_universe([ext], _btc(), _CFG, "long", allow_breakout=False)
    _assert(rows[0].skip and rows[0].skip_reason == "overextended_above_vwap",
            "extension past cap -> skip when breakout off")
    rows = scoring.score_universe([ext], _btc(), _CFG, "long", allow_breakout=True)
    _assert(not rows[0].skip and rows[0].entry_mode == "breakout",
            "same candidate routes to breakout mode when enabled (v0.5.0)")


# ---- enrich_score: trend adjust, breakout confirm, funding ----

def _scored(score=70.0, entry_mode="pullback", side="long", feats=None):
    return scoring.Scored("AAAUSDT", side, score, {"total": score}, False, "",
                          feats or _cand(), entry_mode=entry_mode)


def test_trend_bonus_and_penalty():
    f = _cand(); f.kline_ok = True; f.trend_dir = "long"; f.trend_strength = 0.8
    adj, extra, skip, _ = scoring.enrich_score(_scored(70.0, feats=f), f, _CFG)
    _assert(abs(adj - 82.0) < 1e-9, "aligned trend: 70 + 15*0.8 = 82")
    f.trend_dir = "short"
    adj, extra, skip, _ = scoring.enrich_score(_scored(70.0, feats=f), f, _CFG)
    _assert(abs(adj - 58.0) < 1e-9, "opposed trend: 70 - 12 = 58")


def test_breakout_confirmation_gate():
    f = _cand(last=103.0, high=103.0)  # at the session extreme
    f.kline_ok = True; f.trend_dir = "long"; f.trend_strength = 0.7
    _, _, skip, reason = scoring.enrich_score(_scored(80.0, "breakout", feats=f), f, _CFG)
    _assert(not skip, "aligned strong trend AT the extreme -> breakout confirmed")
    f.trend_strength = 0.3  # below breakout_trend_min 0.6
    _, _, skip, reason = scoring.enrich_score(_scored(80.0, "breakout", feats=f), f, _CFG)
    _assert(skip and reason == "breakout_unconfirmed", "weak trend -> breakout demoted")


def test_funding_crowding():
    f = _cand(); f.funding_ok = True; f.funding_now = 0.0040   # +40bps, long = crowded
    _, _, skip, reason = scoring.enrich_score(_scored(80.0, feats=f), f, _CFG)
    _assert(skip and reason == "funding_crowded_long", "funding past skip_bps -> hard skip")
    f.funding_now = 0.0015                                     # +15bps adverse
    adj, extra, skip, _ = scoring.enrich_score(_scored(80.0, feats=f), f, _CFG)
    _assert(not skip and abs(adj - (80.0 - 8.0 * 0.5)) < 1e-9,
            "milder adverse funding: soft penalty 8*(15/30) = 4")


def test_funding_scoped_to_crypto():
    # v0.9.12: funding is sourced from a CRYPTO endpoint (data.crypto.futures.
    # funding_rate) with no RWA-equity/metals coverage, so its reading is a data
    # artifact on those perps (the live MSTR funding_cr glitch). The skip+penalty
    # must apply ONLY where funding is native (crypto); equity/metals are exempt.
    f = _cand(); f.funding_ok = True; f.funding_now = 0.0040   # +40bps, would skip on crypto

    def _u(universe):
        s = _scored(80.0, feats=f); s.universe = universe
        return scoring.enrich_score(s, f, _CFG)

    _, _, skip_c, reason_c = _u("crypto")
    _assert(skip_c and reason_c == "funding_crowded_long",
            "crypto keeps the funding skip at full strength (real crowding protection preserved)")

    adj_e, extra_e, skip_e, _ = _u("equities")
    _assert(not skip_e, "equity perp is NOT funding-skipped (crypto-endpoint artifact ignored)")
    _assert(abs(adj_e - 80.0) < 1e-9, "equity: no funding penalty either -> full score")
    _assert("funding_bps" not in extra_e, "equity: funding not even read (block skipped)")

    _, _, skip_m, _ = _u("metals")
    _assert(not skip_m, "metals perp is NOT funding-skipped")

    _, _, skip_blank, _ = _u("")
    _assert(skip_blank, "empty/unknown universe FAILS OPEN -> funding still applies (crypto default)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} scoring tests passed.")
