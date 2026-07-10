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
    # v0.9.49: enumeration defaults OFF (live-adjudicated cross-exchange), so
    # with no futures bulk and no watchlist the diag reads "off", not "nomethod"
    _assert(got == [] and note == "no_bulk_surface;e=off",
            "no bulk SDK surface -> empty + named note w/ enum diag (fail-open): " + note)


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
    # v0.9.43: expanded allowlist -- long-tail stocks/ETFs no longer fall to BTC
    _assert(features.classify_asset("ASML") == "equities", "ASML stock -> equities (was BTC)")
    _assert(features.classify_asset("GS") == "equities", "Goldman stock -> equities (was BTC)")
    _assert(features.classify_asset("SMH") == "equities", "SMH ETF -> equities (was BTC)")
    _assert(features.classify_asset("SGOV") == "equities", "SGOV treasury ETF -> equities (was BTC)")
    # v0.9.43: pre-IPO-style names are stock perps -> equities (no separate class)
    _assert(features.classify_asset("OPENAI") == "equities", "preOPAI -> equities (QQQ)")
    _assert(features.classify_asset("ANTHROPIC") == "equities", "ANTHROPIC -> equities (QQQ)")
    # v0.9.43: energy bases fixed to the venue's real forms (were falling to BTC)
    _assert(features.classify_asset("BZ") == "metals", "Brent (BZ) -> commodity/metals leader (was BTC)")
    _assert(features.classify_asset("NATGAS") == "metals", "NatGas (NATGAS) -> commodity/metals leader (was BTC)")


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


def test_discovery_watchlist_fallback():
    # v0.9.45: bulk surface BLIND -> probe the named watchlist per-symbol
    features.data.crypto = types.SimpleNamespace(futures=types.SimpleNamespace())  # no bulk
    probe = {
        "ASMLUSDT": features.SymbolFeatures(symbol="ASMLUSDT", ok=True, last=1700.0,
            vwap=1700.0, high=1750.0, low=1650.0, quote_volume=8e7, change_pct=1.2),
        "CLUSDT": features.SymbolFeatures(symbol="CLUSDT", ok=True, last=74.0,
            vwap=74.0, high=75.0, low=73.0, quote_volume=2.5e8, change_pct=4.9),
        "THINUSDT": features.SymbolFeatures(symbol="THINUSDT", ok=True, last=1.0,
            vwap=1.0, high=1.1, low=0.9, quote_volume=1e6, change_pct=0.0),   # sub-floor
        "BROKENUSDT": features.SymbolFeatures(symbol="BROKENUSDT", ok=False),  # not ok
    }
    features.fetch_symbol = lambda sym, exchange="bitget": probe.get(
        sym, features.SymbolFeatures(symbol=sym, ok=False))
    cfg = {"discovery_min_volume_usdt": "30000000", "discovery_max_per_class": 4,
           "discovery_probe_max": 12,
           "discovery_watchlist": ["ASMLUSDT", "CLUSDT", "THINUSDT", "BROKENUSDT", "BTCUSDT"]}
    got, how = features.discovery_scan({"BTCUSDT"}, cfg)
    # v0.9.48: source carries the enum diag -> "watchlist;e=nomethod" (no derivatives_tickers here)
    _assert(how.split(";")[0] == "watchlist", "bulk blind + watchlist -> base source 'watchlist': " + how)
    by = {f.symbol: cls for f, cls in got}
    _assert(by == {"ASMLUSDT": "equities", "CLUSDT": "metals"},
            "floor(THIN)+ok(BROKEN)+exclusion(BTC) applied, classes routed: " + str(by))
    # probe_max bounds per-cycle cost -> only the first watchlist name is read
    cfg1 = dict(cfg); cfg1["discovery_probe_max"] = 1
    got1, _ = features.discovery_scan({"BTCUSDT"}, cfg1)
    _assert([f.symbol for f, c in got1] == ["ASMLUSDT"],
            "probe_max=1 reads only the first name: " + str([f.symbol for f, c in got1]))
    # bulk blind + NO watchlist -> no_bulk_surface (base), with enum diag suffix
    _, how2 = features.discovery_scan(set(), {"discovery_watchlist": []})
    _assert(how2.split(";")[0] == "no_bulk_surface", "bulk blind + empty watchlist -> no_bulk_surface: " + how2)


def test_discovery_derivatives_enumeration():
    # v0.9.46: futures.tickers absent -> enumerate via crypto.derivatives_tickers,
    # then fetch_symbol the top-by-volume for full features + scoring
    def drv():
        return [
            {"market": "Bitget Futures", "symbol": "NEWCOINUSDT", "contract_type": "perpetual", "volume_24h": 2e8},
            {"market": "Bitget", "symbol": "MIDUSDT", "contract_type": "perpetual", "volume_24h": 5e7},
            {"market": "Bitget", "symbol": "THINUSDT", "contract_type": "perpetual", "volume_24h": 1e6},   # sub-floor
            {"market": "Binance", "symbol": "OTHERUSDT", "contract_type": "perpetual", "volume_24h": 9e9},  # wrong venue
            {"market": "Bitget", "symbol": "BTCUSDT", "contract_type": "perpetual", "volume_24h": 9e9},     # core (excluded)
            {"market": "Bitget", "symbol": "DATEDUSDT", "contract_type": "futures", "volume_24h": 3e8},     # dated, not perp
        ]
    features.data.crypto = types.SimpleNamespace(
        futures=types.SimpleNamespace(), derivatives_tickers=drv)   # no bulk futures surface
    probe = {
        "NEWCOINUSDT": features.SymbolFeatures(symbol="NEWCOINUSDT", ok=True, last=2.0,
            vwap=2.0, high=2.2, low=1.8, quote_volume=2e8, change_pct=12.0),
        "MIDUSDT": features.SymbolFeatures(symbol="MIDUSDT", ok=True, last=1.0,
            vwap=1.0, high=1.1, low=0.9, quote_volume=5e7, change_pct=3.0),
    }
    features.fetch_symbol = lambda sym, exchange="bitget": probe.get(
        sym, features.SymbolFeatures(symbol=sym, ok=False))
    # v0.9.49: enumeration is OPT-IN (live-adjudicated cross-exchange; default off)
    cfg = {"discovery_min_volume_usdt": "30000000", "discovery_max_per_class": 4,
           "discovery_probe_max": 12, "discovery_enumerate": "1"}
    got, how = features.discovery_scan({"BTCUSDT"}, cfg)
    _assert(how == "derivatives_tickers", "bulk blind + enum opted-in -> source: " + how)
    syms = [f.symbol for f, cls in got]
    _assert(syms == ["NEWCOINUSDT", "MIDUSDT"],
            "venue+perp+floor+core filters, volume-ranked, fetched+scored: " + str(syms))
    en, diag = features._discovery_enumerate({"BTCUSDT"}, set(), 3e7)
    _assert(en == ["NEWCOINUSDT", "MIDUSDT"] and diag == "ok",
            "enumerate keeps venue perps by volume, diag ok: " + str(en) + "/" + diag)
    # toggle off -> enumeration skipped -> no watchlist -> no_bulk_surface;e=off
    cfg_off = dict(cfg); cfg_off["discovery_enumerate"] = "0"
    _, how_off = features.discovery_scan({"BTCUSDT"}, cfg_off)
    _assert(how_off == "no_bulk_surface;e=off",
            "discovery_enumerate=0 -> no_bulk_surface;e=off: " + how_off)
    # v0.9.48 diag: method absent -> 'nomethod'; the diag rides the watchlist source
    features.data.crypto = types.SimpleNamespace(futures=types.SimpleNamespace())  # no derivatives_tickers
    en2, diag2 = features._discovery_enumerate(set(), set(), 3e7)
    _assert(en2 == [] and diag2 == "nomethod", "no derivatives_tickers method -> diag nomethod: " + diag2)
    # v0.9.48 diag: rows returned but 0 matched -> sampled 'm0of<N>:...' (SWAP suffix normalizes)
    features.data.crypto = types.SimpleNamespace(
        futures=types.SimpleNamespace(),
        derivatives_tickers=lambda: [{"market": "Binance", "symbol": "OTHERUSDT",
                                      "contract_type": "perpetual", "volume_24h": 9e9}])
    en3, diag3 = features._discovery_enumerate({"BTCUSDT"}, set(), 3e7)
    _assert(en3 == [] and diag3.startswith("m0of1:") and "Binance" in diag3,
            "0-match samples the first row for diagnosis: " + diag3)
    # SWAP/slash symbol formats normalize to a plain USDT perp
    features.data.crypto = types.SimpleNamespace(
        futures=types.SimpleNamespace(),
        derivatives_tickers=lambda: [{"market": "bitget", "symbol": "NEW-USDT-SWAP",
                                      "contract_type": "perpetual", "volume_24h": 8e7}])
    en4, diag4 = features._discovery_enumerate(set(), set(), 3e7)
    _assert(en4 == ["NEWUSDT"] and diag4 == "ok", "BTC-USDT-SWAP-style normalizes to NEWUSDT: " + str(en4))


def test_discovery_marker():
    # v0.9.44: dedicated DISC-<source> line for the Recent-Signals view (the SCAN
    # d: token is budget-dropped on scored boards, so it can't answer live/blind)
    live = {"discovery": {"source": "tickers", "candidates": [
        {"symbol": "EVAAUSDT", "score": 72.0}, {"symbol": "BLURUSDT", "score": 85.4}]}}
    _assert(ml._discovery_marker(live) == "DISC-tickers-2c-BLUR85",
            "live bulk surface -> " + ml._discovery_marker(live))
    blind = {"discovery": {"source": "no_bulk_surface", "candidates": []}}
    _assert(ml._discovery_marker(blind) == "DISC-no_bulk_surface-0c",
            "blind surface -> DISC-no_bulk_surface-0c")
    err = {"discovery": {"source": "error:AttributeError", "candidates": []}}
    _assert(ml._discovery_marker(err) == "DISC-error:AttributeError-0c",
            "exception path -> DISC-error:...-0c")
    _assert(ml._discovery_marker({}) == "", "discovery off -> empty marker (no behaviour change)")
    # un-scored candidate (a class with no leader this run) -> counted, no top token
    unscored = {"discovery": {"source": "tickers", "candidates": [{"symbol": "CLUSDT", "score": None}]}}
    _assert(ml._discovery_marker(unscored) == "DISC-tickers-1c",
            "un-scored candidate counted, no top token: " + ml._discovery_marker(unscored))


def test_discovery_marker_cadence():
    # LOUD every cycle while blind/errored; hourly heartbeat (first cycle of the
    # hour, minute < 15) while healthy -- v0.9.47 widened == 0 to < 15 because the
    # live schedule fires offset (:03/:18/:33/:48), so == 0 never matched.
    blind = {"discovery": {"source": "no_bulk_surface", "candidates": []}}
    _assert(ml._discovery_marker_due(blind, 17) and ml._discovery_marker_due(blind, 0),
            "blind -> due every cycle (loud)")
    live = {"discovery": {"source": "tickers", "candidates": [{"symbol": "BLURUSDT", "score": 85.4}]}}
    _assert(ml._discovery_marker_due(live, 0) and ml._discovery_marker_due(live, 3),
            "healthy -> due on the first cycle of the hour, incl. an offset :03")
    _assert(not ml._discovery_marker_due(live, 18) and not ml._discovery_marker_due(live, 33),
            "healthy -> NOT due on later cycles (:18/:33) -> once per hour, SCAN owns the rest")
    _assert(not ml._discovery_marker_due({}, 0), "discovery off -> never due")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} discovery tests passed.")
