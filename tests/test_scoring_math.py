"""v0.9.4 (audit T-1) scoring-engine math tests -- the first direct unit tests
for scoring.py (regime gate + 0-100 blend). Previously this layer was exercised
only through the research harness; the C-1 volume-escape bug is exactly the
class these pin.

Run: python3 tests/test_scoring_math.py
"""
from _stub import stub_getagent, load_src

stub_getagent()
features = load_src("features")
scoring = load_src("scoring")

SF = features.SymbolFeatures


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def _sf(symbol="ALTUSDT", last=100.0, vwap=100.0, high=110.0, low=90.0,
        change_pct=1.0, quote_volume=50e6, bid_volume=10.0, ask_volume=10.0, ok=True):
    return SF(symbol=symbol, ok=ok, last=last, vwap=vwap, high=high, low=low,
              change_pct=change_pct, quote_volume=quote_volume,
              bid_volume=bid_volume, ask_volume=ask_volume)


_CFG = {"min_volume_usdt": "10000000", "bidask_full_ratio": "2.0",
        "bidask_partial_ratio": "1.2", "bidask_wall_ratio": "10.0",
        "max_vwap_ext_pct": "4.0", "allow_short": True}


# ---- regime gate ----

def test_regime_full_long():
    btc = _sf("BTCUSDT", last=101.0, vwap=100.0, change_pct=2.0)
    reg = scoring.regime(btc, 1.2, _CFG)  # up + above VWAP + taker buy = 3/3
    _assert(reg.direction == "long" and reg.size_factor == 1.0, "3/3 long gate -> long @ full size")


def test_regime_half_size():
    btc = _sf("BTCUSDT", last=99.0, vwap=100.0, change_pct=2.0)
    reg = scoring.regime(btc, None, _CFG)  # up only (below VWAP, no taker) = 1/3
    _assert(reg.direction == "long" and reg.size_factor == 0.5, "1/3 long gate -> long @ half size")


def test_regime_neutral_on_conflict():
    btc = _sf("BTCUSDT", last=100.0, vwap=100.0, change_pct=0.0)
    reg = scoring.regime(btc, None, _CFG)  # no signal either way
    _assert(reg.direction == "none" and reg.size_factor == 0.0, "no signals -> none / size 0")


def test_regime_short_gated_by_allow_short():
    btc = _sf("BTCUSDT", last=98.0, vwap=100.0, change_pct=-2.0)
    reg = scoring.regime(btc, 0.8, _CFG)
    _assert(reg.direction == "short" and reg.size_factor == 1.0, "3/3 short gate -> short @ full")
    reg2 = scoring.regime(btc, 0.8, {**_CFG, "allow_short": False})
    _assert(reg2.direction == "none", "allow_short False blocks the short regime")


# ---- score_universe hard skips ----

def _score_one(f, direction="long", allow_breakout=False, cfg=None):
    btc = _sf("BTCUSDT", change_pct=1.0)
    rows = scoring.score_universe([f], btc, cfg or _CFG, direction,
                                  allow_breakout=allow_breakout)
    return rows[0]


def test_missing_volume_is_skipped():
    # v0.9.4 C-1 pin: quote_volume=None used to sail past the liquidity floor
    # with a neutral 7.5 volume score -- an unknown-liquidity name could qualify.
    s = _score_one(_sf(quote_volume=None))
    _assert(s.skip and s.skip_reason == "no_volume_data",
            "quote_volume None -> hard skip no_volume_data (was: qualified)")


def test_thin_volume_is_skipped():
    s = _score_one(_sf(quote_volume=5e6))
    _assert(s.skip and s.skip_reason == "thin_volume", "volume below floor -> thin_volume skip")


def test_opposing_wall_skips():
    s = _score_one(_sf(bid_volume=1.0, ask_volume=15.0))  # ask/bid 15 >= 10
    _assert(s.skip and s.skip_reason == "ask_wall", "long into a 10x ask wall -> skip")
    s2 = _score_one(_sf(bid_volume=15.0, ask_volume=1.0), direction="short")
    _assert(s2.skip and s2.skip_reason == "bid_wall", "short into a 10x bid wall -> skip")


def test_overextension_skip_vs_breakout_route():
    ext = _sf(last=105.0, vwap=100.0)  # +5% > 4% cap
    s = _score_one(ext)
    _assert(s.skip and s.skip_reason == "overextended_above_vwap",
            "extended past cap, breakout off -> hard skip")
    s2 = _score_one(_sf(last=105.0, vwap=100.0), allow_breakout=True)
    _assert(not s2.skip and s2.entry_mode == "breakout",
            "extended past cap, breakout on -> routed to breakout, not skipped")


def test_weights_sum_to_100_ceiling():
    # A maximal long candidate: strongest rel momentum, above VWAP, top of range,
    # 2x bid-heavy book, top cross-sectional volume.
    strong = _sf(last=109.9, vwap=100.0, high=110.0, low=90.0, change_pct=9.0,
                 quote_volume=100e6, bid_volume=20.0, ask_volume=10.0)
    weak = _sf("WEAKUSDT", last=91.0, vwap=100.0, high=110.0, low=90.0,
               change_pct=-3.0, quote_volume=20e6)
    btc = _sf("BTCUSDT", change_pct=1.0)
    cfg = {**_CFG, "max_vwap_ext_pct": "20.0"}  # disable ext skip for the ceiling check
    rows = scoring.score_universe([strong, weak], btc, cfg, "long")
    top = [r for r in rows if r.symbol == "ALTUSDT"][0]
    _assert(abs(top.score - 100.0) < 1e-6, "maximal candidate scores exactly 100 (weights sum)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} scoring-math tests passed.")
