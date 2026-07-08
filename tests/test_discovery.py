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
    syms = [f.symbol for f, cls in got]
    _assert(how == "tickers", "bulk surface identified: " + how)
    _assert(syms == ["EVAAUSDT", "BLURUSDT"],
            "floor+blocklist+exclusion applied, ranked by volume, per-class capped -> " + str(syms))


def test_discovery_vwap_derived_from_volumes():
    # bulk ticker rows may lack a vwap field; quote/base volume IS the 24h VWAP
    _wire_bulk([_row("EVAAUSDT", 8e7, base_vol=4e7), _row("FILLERUSDT", 1e6, vwap=1.0)])
    got, _ = features.discovery_scan(set(), CFG)
    _assert(len(got) == 1 and abs(got[0][0].vwap - 2.0) < 1e-9,
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




def test_discovery_token_and_fold():
    # v0.9.41: highest-scoring candidate surfaces as d:<SYM><score>
    m = {"discovery": {"source": "tickers", "candidates": [
        {"symbol": "EVAAUSDT", "score": 72.0}, {"symbol": "BLURUSDT", "score": 85.4}]}}
    _assert(ml._discovery_token(m) == "d:BLUR85", "top-score candidate -> d:BLUR85")
    _assert(ml._discovery_token({}) == "", "no discovery -> empty token")
    _assert(ml._discovery_token({"discovery": {"candidates": []}}) == "",
            "discovery on but nothing found -> empty token")
    # fold: a quiet line has room for the token; a full 3-universe line drops it
    quiet = ml._fold_exec_onto_scan("cry:n-met:n-equ:n", "0", "0", "-b50", "",
                                    "no.neutral", None, disc="d:BLUR85")
    _assert(quiet.endswith("|d:BLUR85") and len(quiet) <= 63,
            "quiet board surfaces the discovery token: " + quiet)
    busy = ml._fold_exec_onto_scan("cry:sLAB80q-met:sXAG54x-equ:sMSTR100q", "2", "1",
                                   "-b50", "", "hld.MSTR+2P.t4h", None, disc="d:BLUR85")
    _assert("d:BLUR85" not in busy and len(busy) <= 63,
            "busy board drops the token under the 63-char budget (never truncates the tail)")


def test_discovery_armed_default():
    import re as _re
    mf = open(__file__.replace("tests/test_discovery.py", "manifest.yaml")).read()
    _assert('universe_discovery: "1"' in mf,
            "v0.9.41 forward test ARMED: manifest ships universe_discovery '1'")
    _assert(ml._SHIPPED_DEFAULTS.get("universe_discovery") == "1",
            "shipped-defaults snapshot matches the armed manifest (config_overrides stays honest)")


def test_classify_asset_routing():
    # v0.9.42: each base routes to its regime-leader universe
    _assert(features.classify_asset("SNDK") == "equities", "tokenized stock -> equities (QQQ)")
    _assert(features.classify_asset("SOXL") == "equities", "leveraged ETF -> equities (QQQ)")
    _assert(features.classify_asset("CL") == "metals", "crude commodity -> metals (XAU) leader")
    _assert(features.classify_asset("XAUT") == "metals", "tokenized gold -> metals")
    _assert(features.classify_asset("EVAA") == "crypto", "unknown/crypto -> crypto (BTC) default")


def test_discovery_multiclass_and_per_class_cap():
    _wire_bulk([
        _row("EVAAUSDT", 1.2e8, vwap=1.0), _row("BZUSDT", 4e7, vwap=1.0),   # crypto x2
        _row("SNDKUSDT", 2e8, vwap=1.0), _row("MUXUSDT", 1.9e8, vwap=1.0),  # (MUX not a stock -> crypto)
        _row("SOXLUSDT", 1.5e8, vwap=1.0),                                  # etf -> equities
        _row("CLUSDT", 2.5e8, vwap=1.0), _row("XAUTUSDT", 1.6e7, vwap=1.0), # commodity -> metals
    ])
    cfg = {"discovery_min_volume_usdt": "30000000", "discovery_max": 4, "discovery_max_per_class": 1}
    got, _ = features.discovery_scan({"BTCUSDT"}, cfg)
    by_cls = {}
    for f, cls in got:
        by_cls.setdefault(cls, []).append(f.symbol)
    _assert(all(len(v) <= 1 for v in by_cls.values()),
            "per-class cap of 1 enforced across classes: " + str(by_cls))
    _assert("equities" in by_cls and "crypto" in by_cls and "metals" in by_cls,
            "all three classes represented: " + str(sorted(by_cls)))
    # XAUT ($16M) is below the $30M floor -> only CL survives metals
    _assert(by_cls.get("metals") == ["CLUSDT"], "metals top = CL (XAUT sub-floor)")


def test_discovery_classes_restrict():
    _wire_bulk([_row("EVAAUSDT", 1.2e8, vwap=1.0), _row("SNDKUSDT", 2e8, vwap=1.0),
                _row("FILLERUSDT", 1e6, vwap=1.0)])
    cfg = {"discovery_min_volume_usdt": "30000000", "discovery_max": 4,
           "discovery_classes": "crypto"}   # old crypto-only behaviour
    got, _ = features.discovery_scan(set(), cfg)
    syms = [f.symbol for f, cls in got]
    _assert(syms == ["EVAAUSDT"], "discovery_classes=crypto excludes the stock: " + str(syms))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} discovery tests passed.")
