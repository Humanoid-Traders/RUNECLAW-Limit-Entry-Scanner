"""v0.9.38 universe-expansion tests: shadow discovery + extra_symbols watchlist.

Pins features.discovery_scan (bulk read, floor/blocklist/exclusion filters,
quote/base VWAP derivation, volume ranking + top-k, fail-open on a missing
bulk surface) and main_live._universes extra_symbols merge (dedupe, uppercase,
appended to the FIRST universe only). The shadow property -- discovery never
enters the qualified pool -- is structural (build_decision only logs it), and
the default-off gate is pinned here.

Run: python3 tests/test_discovery.py
"""
import types

from _stub import stub_getagent, load_src

stub_getagent()
features = load_src("features")
ml = load_src("main_live")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def _row(sym, qv, last=1.0, high=1.1, low=0.9, vwap=None, base_vol=None, chg=5.0):
    r = {"symbol": sym, "quote_volume": qv, "last": last, "high": high,
         "low": low, "change_percent": chg}
    if vwap is not None:
        r["vwap"] = vwap
    if base_vol is not None:
        r["base_volume"] = base_vol
    return r


def _wire_bulk(rows):
    features.data.crypto = types.SimpleNamespace(
        futures=types.SimpleNamespace(tickers=lambda exchange: rows))


CFG = {"discovery_min_volume_usdt": "30000000", "discovery_max": 2,
       "discovery_blocklist": ["SNDK", "MU"]}


def test_discovery_filters_and_ranking():
    _wire_bulk([
        _row("EVAAUSDT", 8e7, vwap=1.0),          # in: fresh listing, big volume
        _row("BLURUSDT", 4e7, vwap=1.0),          # in: above floor
        _row("VANRYUSDT", 3.5e7, vwap=1.0),       # above floor but ranked 3rd -> cut by top_k 2
        _row("EPICUSDT", 1e7, vwap=1.0),          # out: below the $30M discovery floor
        _row("SNDKUSDT", 2e8, vwap=1.0),          # out: RWA blocklist (base)
        _row("MUUSDT", 2e8, vwap=1.0),            # out: blocklist 'MU' (base of MUUSDT)
        _row("BTCUSDT", 9e9, vwap=1.0),           # out: excluded (core)
        _row("FOOUSD", 9e9, vwap=1.0),            # out: not a USDT perp
    ])
    got, how = features.discovery_scan({"BTCUSDT"}, CFG)
    syms = [f.symbol for f in got]
    _assert(how == "tickers", "bulk surface identified: " + how)
    _assert(syms == ["EVAAUSDT", "BLURUSDT"],
            "floor+blocklist+exclusion applied, ranked by volume, top-k capped -> " + str(syms))


def test_discovery_vwap_derived_from_volumes():
    # bulk ticker rows may lack a vwap field; quote/base volume IS the 24h VWAP
    _wire_bulk([_row("EVAAUSDT", 8e7, base_vol=4e7), _row("FILLERUSDT", 1e6, vwap=1.0)])
    got, _ = features.discovery_scan(set(), CFG)
    _assert(len(got) == 1 and abs(got[0].vwap - 2.0) < 1e-9,
            "missing vwap derived as quote/base volume = 2.0")
    # no vwap AND no base volume -> candidate dropped (core fields incomplete)
    _wire_bulk([_row("EVAAUSDT", 8e7), _row("FILLERUSDT", 1e6, vwap=1.0)])
    got2, _ = features.discovery_scan(set(), CFG)
    _assert(got2 == [], "no vwap and no base volume -> dropped, not half-built")


def test_discovery_failopen_no_bulk_surface():
    features.data.crypto = types.SimpleNamespace(futures=types.SimpleNamespace())
    got, note = features.discovery_scan(set(), CFG)
    _assert(got == [] and note == "no_bulk_surface",
            "no bulk SDK surface -> empty + named note (fail-open)")


def test_extra_symbols_merge():
    cfg = {"trading_symbols": ["BTCUSDT", "ETHUSDT"],
           "extra_symbols": ["evaausdt", "ETHUSDT", "BLURUSDT"]}
    unis = ml._universes(cfg)
    _assert(unis[0]["symbols"] == ["BTCUSDT", "ETHUSDT", "EVAAUSDT", "BLURUSDT"],
            "extra_symbols uppercased, deduped, appended to the first universe")
    cfg2 = {"trading_symbols": ["BTCUSDT"]}
    _assert(ml._universes(cfg2)[0]["symbols"] == ["BTCUSDT"],
            "absent extra_symbols -> exact legacy universe (bit-exact default)")


def test_discovery_default_off():
    _assert(str({}.get("universe_discovery", "0")) == "0" and
            "universe_discovery: \"0\"" in open(
                __file__.replace("tests/test_discovery.py", "manifest.yaml")).read(),
            "manifest ships universe_discovery '0' -- shadow scan off by default")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} discovery tests passed.")
