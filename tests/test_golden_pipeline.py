"""End-to-end golden regression: frozen REAL candles -> full decision pipeline
-> exact expected output.

Every unit test pins one function; nothing pins the COMPOSED pipeline, so a
refactor could drift live behaviour while the suite stays green ("bit-exact
legacy" was a claim, not a test). This freezes real Bitget bars
(fixtures/golden_bars.json, captured 2026-07-07) and the pipeline's complete
output on them (fixtures/golden_expected.json): regime votes -> pass-1 scores
and dims -> pass-2 enrichment skip/reasons -> risk plan prices. Any diff = a
behaviour change that must be either intentional (regen the golden in the
same commit and say why) or a bug.

Regenerate after an INTENTIONAL behaviour change:
    GOLDEN_REGEN=1 python3 tests/test_golden_pipeline.py

Run: python3 tests/test_golden_pipeline.py
"""
import json
import os
from pathlib import Path

from _stub import stub_getagent, load_src

stub_getagent()
features = load_src("features")
scoring = load_src("scoring")
risk = load_src("risk")
SF = features.SymbolFeatures

FIX = Path(__file__).parent / "fixtures"
CFG = {
    # the live manifest defaults that reach the pipeline under test
    "min_score": 70, "min_volume_usdt": "10000000", "max_vwap_ext_pct": "5.0",
    "allow_short": True, "atr_limit_mult": "0.3", "atr_period": 14,
    "tp1_pct": "5.0", "tp2_pct": "20", "pullback_tp2_pct": "22",
    "trail_atr_mult": "2.0", "breakeven_pct": "2.0",
    "sl_min_btc_eth_pct": "1.5", "sl_min_sol_bnb_pct": "1.2",
    "sl_min_alt_pct": "2.5", "max_loss_usdt": "15", "leverage": 10,
    "margin_budget": "100", "breakout_trend_min": "0.7",
    "breakout_extreme_band": "0.015", "regime_chg_deadzone_pct": "0.3",
    "swing_k": 3, "trend_lookback": 12, "trend_norm": "0.05",
}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def _sf_from_bars(sym, h1):
    """Rebuild pass-1 SymbolFeatures from a trailing 25-bar 1h window --
    the same reconstruction research/replay.py uses (kept in sync by eye;
    a drift here fails the golden loudly, which is the point)."""
    win = h1[-25:]
    last = win[-1][4]
    highs = [b[2] for b in win]; lows = [b[3] for b in win]
    closes = [b[4] for b in win]; bvols = [b[5] for b in win]
    qvols = [b[6] for b in win]
    vwap = (sum(c * v for c, v in zip(closes, bvols)) / sum(bvols)) if sum(bvols) > 0 else last
    chg = ((last - closes[0]) / closes[0] * 100.0) if closes[0] else 0.0
    return SF(symbol=sym, ok=True, last=last, vwap=vwap, high=max(highs),
              low=min(lows), change_pct=chg, quote_volume=sum(qvols),
              bid_volume=None, ask_volume=None)


def _dictify(h1):
    return [{"open": b[1], "high": b[2], "low": b[3], "close": b[4]} for b in h1]


def _trend_4h(h4, lookback, norm):
    """4h trend read mirroring research/replay.trend_4h (closed-bar slope)."""
    closes = [b[4] for b in h4][-lookback:]
    if len(closes) < 2 or closes[0] <= 0:
        return "", 0.0
    move = (closes[-1] - closes[0]) / closes[0]
    strength = min(abs(move) / norm, 1.0)
    return ("long" if move > 0 else "short"), round(strength, 6)


def run_pipeline(bars):
    lead = _sf_from_bars("BTCUSDT", bars["BTCUSDT"]["h1"])
    reg = scoring.regime(lead, None, CFG)
    out = {"regime": {"direction": reg.direction,
                      "detail": {k: (round(v, 6) if isinstance(v, float) else v)
                                 for k, v in reg.detail.items()}}}
    cands = [s for s in bars if s != "BTCUSDT"]
    feats = [_sf_from_bars(s, bars[s]["h1"]) for s in cands]
    direction = reg.direction if reg.direction in ("long", "short") else "long"
    scored = scoring.score_universe(feats, lead, CFG, direction)
    out["scored"] = []
    for s in scored:
        rec = {"symbol": s.symbol, "side": s.side, "score": round(s.score, 4),
               "skip": s.skip, "skip_reason": s.skip_reason,
               "dims": {k: (round(v, 6) if isinstance(v, float) else v)
                        for k, v in s.dims.items()},
               "entry_mode": s.entry_mode}
        # pass-2 enrichment on every candidate (live enriches top-N; the golden
        # enriches all for coverage), then the risk plan for non-skipped ones
        h1 = _dictify(bars[s.symbol]["h1"])
        s.features.atr = features._wilder_atr(h1, int(CFG["atr_period"]))
        s.features.kline_ok = s.features.atr is not None
        td, ts = _trend_4h(bars[s.symbol]["h4"], CFG["trend_lookback"],
                           float(CFG["trend_norm"]))
        s.features.trend_dir, s.features.trend_strength = td, ts
        s.features.funding_ok = False   # deterministic: no live funding in fixtures
        (s.features.swing_high, s.features.swing_low,
         s.features.structure_dir) = features.structure_read(h1, int(CFG["swing_k"]))
        s.features.candle_veto_long = features.candle_veto(h1, "long")
        s.features.candle_veto_short = features.candle_veto(h1, "short")
        _, _, skip2, reason2 = scoring.enrich_score(s, s.features, CFG)
        rec["enrich"] = {"skip": bool(skip2), "reason": reason2,
                         "entry_mode": s.entry_mode,
                         "atr": round(s.features.atr, 8) if s.features.atr else None,
                         "trend": [td, ts],
                         "structure_dir": s.features.structure_dir,
                         "swing_high": s.features.swing_high,
                         "swing_low": s.features.swing_low}
        if not skip2:
            plan = risk.build_plan(s.features, CFG, s.size_factor,
                                   side=s.side, entry_mode=s.entry_mode)
            rec["plan"] = None if plan is None else {
                "entry": round(plan.entry, 8), "sl_price": round(plan.sl_price, 8),
                "sl_pct": round(plan.sl_pct, 8), "tp1": round(plan.tp1, 8),
                "tp2": round(plan.tp2, 8), "trail_atr": round(plan.trail_atr, 8),
                "notional_usdt": round(plan.notional_usdt, 4),
                "margin_usdt": round(plan.margin_usdt, 4),
                "entry_mode": plan.entry_mode, "note": plan.note}
        out["scored"].append(rec)
    return out


def test_golden_pipeline():
    bars = json.loads((FIX / "golden_bars.json").read_text())
    got = run_pipeline(bars)
    exp_path = FIX / "golden_expected.json"
    if os.environ.get("GOLDEN_REGEN") == "1":
        exp_path.write_text(json.dumps(got, indent=1, sort_keys=True))
        print("  REGENERATED", exp_path)
        return
    expected = json.loads(exp_path.read_text())
    got_j = json.loads(json.dumps(got, sort_keys=True))
    if got_j != expected:
        # name the first divergence precisely instead of dumping two blobs
        def _walk(a, b, path="$"):
            if type(a) is not type(b):
                return f"{path}: type {type(b).__name__} -> {type(a).__name__}"
            if isinstance(a, dict):
                for k in sorted(set(a) | set(b)):
                    if k not in a: return f"{path}.{k}: MISSING in got"
                    if k not in b: return f"{path}.{k}: NEW in got"
                    d = _walk(a[k], b[k], f"{path}.{k}")
                    if d: return d
            elif isinstance(a, list):
                if len(a) != len(b):
                    return f"{path}: len {len(b)} -> {len(a)}"
                for i, (x, y) in enumerate(zip(a, b)):
                    d = _walk(x, y, f"{path}[{i}]")
                    if d: return d
            elif a != b:
                return f"{path}: {b!r} -> {a!r}"
            return ""
        raise AssertionError("golden diff at " + (_walk(got_j, expected) or "?"))
    _assert(True, f"pipeline output bit-exact vs golden "
                  f"({len(expected['scored'])} candidates, regime "
                  f"{expected['regime']['direction']})")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} golden-pipeline tests passed.")
