"""v0.9.34 swing-structure + candle engine tests.

The system's only prior "structure" was the rolling 24h high/low. These pin the
new pure functions (swing_points pivot detection with confirmation lag,
structure_read's HH/HL vs LH/LL direction, candle_veto's doji/engulfing reads)
and every opt-in consumer's gate + fail-open contract (structure stop in risk,
structure/candle vetoes and breakout structure-confirm in scoring). All
defaults must be bit-exact legacy.

Run: python3 tests/test_structure.py
"""
from _stub import stub_getagent, load_src

stub_getagent()
features = load_src("features")
risk = load_src("risk")
scoring = load_src("scoring")
SF = features.SymbolFeatures


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def _bar(h, l, o=None, c=None):
    mid = (h + l) / 2.0
    return {"high": h, "low": l, "open": o if o is not None else mid,
            "close": c if c is not None else mid}


# ---- swing_points / structure_read ----

def test_swing_detection_and_lag():
    #                     0   1   2   3(SH) 4   5   6   7
    bars = [_bar(10, 9), _bar(11, 10), _bar(12, 11), _bar(15, 12),
            _bar(13, 11), _bar(12, 10), _bar(11, 9), _bar(12, 10)]
    highs, lows = features.swing_points(bars, k=3)
    _assert(highs == [(3, 15)], "pivot high at index 3 (15), confirmed k=3 bars later")
    _assert(lows == [], "no confirmed pivot low in this window (edge bars can't confirm)")
    # the last 3 bars can never hold a confirmed pivot -- that lag is the design
    bars2 = bars + [_bar(16, 14)]   # a NEW extreme on the last bar
    highs2, _ = features.swing_points(bars2, k=3)
    _assert(highs2 == [(3, 15)], "an unconfirmed last-bar extreme is NOT a pivot yet")


def test_structure_direction():
    up = [_bar(10, 9), _bar(10, 9), _bar(10, 9), _bar(12, 8),      # pivot H 12 / pivot L 8
          _bar(10, 9), _bar(10, 9), _bar(10, 9), _bar(9.8, 8.5),   # pivot L 8.5 (HL vs 8)
          _bar(10, 9), _bar(10, 9), _bar(10, 9), _bar(14, 9.5),    # pivot H 14 (HH vs 12)
          _bar(11, 10), _bar(11, 10), _bar(11, 10)]
    sh, sl, sdir = features.structure_read(up, k=3)
    _assert(sdir == "long" and sh == 14 and sl == 8.5,
            "HH (14>12) + HL (8.5>8) -> structure long, last pivots surfaced")
    flat = [_bar(10, 9)] * 10
    _, _, sdir2 = features.structure_read(flat, k=3)
    _assert(sdir2 == "neutral", "no distinct pivots -> neutral (fail-neutral)")


# ---- candle_veto ----

def test_candle_doji_and_engulf():
    doji = [_bar(10, 9, o=9.5, c=9.52), _bar(11, 9, o=10.0, c=10.05)]
    _assert(features.candle_veto(doji, "long") == "doji", "tiny body vs range -> doji (either side)")
    # bearish engulfing vs a long: prior small up bar, last big down bar engulfing it
    eng = [_bar(10.6, 9.9, o=10.0, c=10.5), _bar(10.8, 9.4, o=10.6, c=9.6)]
    _assert(features.candle_veto(eng, "long") == "engulf", "bearish engulfing vetoes a long")
    _assert(features.candle_veto(eng, "short") == "", "same bar is FINE for a short")
    _assert(features.candle_veto([{"high": 10}], "long") == "", "malformed/short data -> '' (fail-open)")


# ---- risk: structure stop (opt-in) ----

def _feats(swing_high=None, swing_low=None):
    # high chosen so the base (24h-extreme) stop clears the 1.5% sl_min floor:
    # atr proxy (1850-1720)/2.5 = 52, entry = 1769 + 0.3*52 = 1784.6,
    # base raw stop 3.66%, swing-1830 stop 2.54% -- both un-floored.
    f = SF("ETHUSDT", True, last=1770.0, vwap=1769.0, high=1850.0, low=1720.0,
           change_pct=-1.0, quote_volume=1e9)
    f.swing_high, f.swing_low = swing_high, swing_low
    return f


_RCFG = {"tp2_pct": "20", "sl_min_btc_eth_pct": "1.5", "max_loss_usdt": "15",
         "leverage": 10, "margin_budget": "100", "atr_limit_mult": "0.3"}


def test_structure_stop_optin_and_failopen():
    base = risk.build_plan(_feats(swing_high=1830.0), dict(_RCFG), 1.0, side="short")
    armed = risk.build_plan(_feats(swing_high=1830.0),
                            dict(_RCFG, pullback_structure_stop="1"), 1.0, side="short")
    _assert(armed.sl_price < base.sl_price,
            "armed: stop anchors to the 1830 pivot, tighter than the 1850 rolling high")
    fb = risk.build_plan(_feats(swing_high=None),
                         dict(_RCFG, pullback_structure_stop="1"), 1.0, side="short")
    _assert(abs(fb.sl_price - base.sl_price) < 1e-9,
            "no swing available -> falls back to the 24h extreme (fail-open)")
    _assert(abs(base.sl_pct * base.notional_usdt - 15.0) < 1e-6
            and abs(armed.sl_pct * armed.notional_usdt - 15.0) < 1e-6,
            "dollar risk unchanged either way (backward-from-stop sizing)")


# ---- scoring: vetoes + breakout structure confirm (opt-in) ----

def _scored(side="long", mode="pullback"):
    f = _feats()
    f.kline_ok, f.trend_dir, f.trend_strength = True, side, 0.9
    f.funding_ok = False
    f.last = 1845.0   # within 1.5% of the 1850 24h high -> breakout near_extreme true for long
    return scoring.Scored("ETHUSDT", side, 90.0, {}, False, "", f, entry_mode=mode)


def test_structure_and_candle_vetoes():
    s = _scored("long")
    s.features.structure_dir = "short"
    _, _, skip, reason = scoring.enrich_score(s, s.features, {"structure_trend_veto": "1"})
    _assert(skip and reason == "structure_opposed", "armed veto: long into LH+LL structure -> skip")
    _, _, skip2, _ = scoring.enrich_score(_scored("long"), _scored("long").features, {})
    _assert(not skip2, "default off: same candidate passes (bit-exact legacy)")
    s3 = _scored("long")
    s3.features.candle_veto_long = "engulf"
    _, _, skip3, reason3 = scoring.enrich_score(s3, s3.features, {"candle_veto": "1"})
    _assert(skip3 and reason3 == "candle_engulf", "armed candle veto -> skip with named reason")


def test_breakout_structure_confirm():
    cfg = {"breakout_trend_min": "0.7", "breakout_extreme_band": "0.015",
           "breakout_structure_confirm": "1"}
    s = _scored("long", mode="breakout")
    s.features.swing_high = 1860.0          # last (1845) has NOT broken the pivot
    _, _, skip, reason = scoring.enrich_score(s, s.features, cfg)
    _assert(skip and reason == "breakout_unconfirmed",
            "armed: near a rolling extreme but pivot unbroken -> unconfirmed")
    s2 = _scored("long", mode="breakout")
    s2.features.swing_high = 1840.0         # pivot broken
    _, _, skip2, _ = scoring.enrich_score(s2, s2.features, cfg)
    _assert(not skip2, "pivot broken -> breakout confirmed")
    s3 = _scored("long", mode="breakout")
    s3.features.swing_high = None           # no pivot data
    _, _, skip3, _ = scoring.enrich_score(s3, s3.features, cfg)
    _assert(not skip3, "no swing available -> fail-open (thin bars never kill breakouts)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} structure tests passed.")
