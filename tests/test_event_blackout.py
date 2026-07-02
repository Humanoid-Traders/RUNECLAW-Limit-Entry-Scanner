"""v0.9.5 macro event-blackout tests (audit: "no news/event filter on RWA
equity perps") + the _universes passthrough BUG FIX.

Pins: event-timestamp parsing (ISO / epoch s / epoch ms), the pure window +
importance matching, the fail-open contract (disabled -> no calendar call;
unreadable calendar -> no blackout), the universe wiring (an opted-in universe
loses candidacy inside the window; others are untouched), and the v0.9.5 fix
for the per-universe flag passthrough -- `breakout`/`overrides`/`event_blackout`
were silently DROPPED by _universes() since v0.6.0, so the manifest's equities
flags never reached _scan_universe. Absent-key semantics are pinned too (the
crypto name-based breakout default must survive the fix).

Run: python3 tests/test_event_blackout.py
"""
import types
from datetime import datetime, timezone

from _stub import stub_getagent, load_src

stub_getagent()
features = load_src("features")
execution = load_src("execution")  # main_live imports it; load against stubs
risk = load_src("risk")
scoring = load_src("scoring")
ml = load_src("main_live")
SF = features.SymbolFeatures


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def _now_ms():
    return datetime.now(timezone.utc).timestamp() * 1000.0


# ---- _parse_event_ts ----

def test_parse_event_ts():
    iso = "2026-07-02T14:30:00+00:00"
    want = datetime.fromisoformat(iso).timestamp() * 1000.0
    _assert(features._parse_event_ts(iso) == want, "ISO with offset parsed")
    _assert(features._parse_event_ts("2026-07-02T14:30:00Z") == want, "Z suffix handled")
    _assert(features._parse_event_ts("2026-07-02T14:30:00") == want, "naive ISO treated as UTC")
    _assert(features._parse_event_ts(1700000000) == 1700000000000.0, "epoch seconds -> ms")
    _assert(features._parse_event_ts(1700000000000) == 1700000000000.0, "epoch ms unchanged")
    _assert(features._parse_event_ts("not a date") is None, "junk -> None")


# ---- _blackout_event (pure window + importance logic) ----

def _ev(offset_h, importance="High", name="CPI"):
    ts = datetime.fromtimestamp((_now_ms() + offset_h * 3_600_000) / 1000.0,
                                tz=timezone.utc).isoformat()
    return {"date": ts, "importance": importance, "event": name}


def test_blackout_window_logic():
    now = _now_ms()
    hit = features._blackout_event([_ev(1.0)], now, 2.0, "high")
    _assert(hit is not None and hit["event"] == "CPI", "event 1h ahead inside a 2h window -> blackout")
    _assert(features._blackout_event([_ev(-1.5)], now, 2.0, "high") is not None,
            "event 1.5h AGO inside the window -> still blacked (post-print volatility)")
    _assert(features._blackout_event([_ev(3.0)], now, 2.0, "high") is None,
            "event 3h ahead outside a 2h window -> clear")
    _assert(features._blackout_event([_ev(1.0, importance="Low")], now, 2.0, "high") is None,
            "low-importance event does not trigger a 'high' blackout")
    _assert(features._blackout_event([_ev(1.0, importance="HIGH")], now, 2.0, "high") is not None,
            "importance match is case-insensitive")
    _assert(features._blackout_event([{"importance": "High", "event": "no ts"}], now, 2.0, "high") is None,
            "event without a parseable timestamp is ignored (fail-open per event)")
    _assert(features._blackout_event([_ev(0.5)], now, 0.0, "high") is None,
            "window 0 -> guard off")


# ---- event_blackout (fetch wrapper: opt-in + fail-open) ----

def test_disabled_makes_no_call():
    called = {"n": 0}
    def _cal(**k):
        called["n"] += 1
        return []
    features.data.economy = types.SimpleNamespace(calendar=_cal)
    _assert(features.event_blackout({"event_blackout_hours": "0"}) is None,
            "hours=0 -> None")
    _assert(called["n"] == 0, "disabled guard never calls the calendar")


def test_failopen_on_calendar_error():
    def _raise(**k):
        raise RuntimeError("calendar down")
    features.data.economy = types.SimpleNamespace(calendar=_raise)
    out = features.event_blackout({"event_blackout_hours": "2"})
    _assert(out is None, "unreadable calendar -> no blackout (fail-open, never blocks)")


def test_active_blackout_detected():
    # calendar returns an OBBject-like envelope: rows live under .results
    rows = [_ev(0.5, importance="High", name="FOMC Rate Decision")]
    features.data.economy = types.SimpleNamespace(
        calendar=lambda **k: types.SimpleNamespace(results=rows))
    out = features.event_blackout({"event_blackout_hours": "2"})
    _assert(out is not None and out["event"].startswith("FOMC"),
            "high-importance event inside the window -> blackout dict")


# ---- _universes passthrough (the v0.9.5 bug fix) ----

_UNI_CFG = {"min_score": 70, "min_volume_usdt": "0",
            "universes": [
                {"name": "crypto", "leader": "BTCUSDT", "symbols": ["ETHUSDT"], "breakout": True},
                {"name": "equities", "leader": "QQQUSDT", "symbols": ["TSLAUSDT"],
                 "breakout": True, "event_blackout": True,
                 "overrides": {"max_vwap_ext_pct": "2.5"}},
                {"name": "metals", "leader": "XAUUSDT", "symbols": ["XAGUSDT"]},
            ]}


def test_universes_passthrough_fixed():
    unis = {u["name"]: u for u in ml._universes(_UNI_CFG)}
    eq = unis["equities"]
    _assert(eq.get("breakout") is True, "equities breakout flag now reaches the scan (was dropped)")
    _assert(eq.get("event_blackout") is True, "equities event_blackout flag passes through")
    _assert(eq.get("overrides", {}).get("max_vwap_ext_pct") == "2.5",
            "equities per-universe override passes through (was silently ignored)")
    _assert("breakout" not in unis["metals"],
            "absent flag stays ABSENT (metals) -> .get(key, default) semantics intact")


# ---- _scan_universe wiring: blackout suppresses candidacy, scoped correctly ----

def test_scan_universe_blackout_wiring():
    def _fake_fetch(symbol, exchange="bitget"):
        if symbol in ("QQQUSDT", "BTCUSDT"):   # leaders: up + above VWAP -> long gate
            return SF(symbol, True, last=101.0, vwap=100.0, high=102.0, low=99.0,
                      change_pct=2.0, quote_volume=1e9)
        return SF(symbol, True, last=102.0, vwap=100.0, high=103.0, low=95.0,
                  change_pct=5.0, quote_volume=5e7, bid_volume=30.0, ask_volume=10.0)
    orig_fetch, orig_taker = ml.features.fetch_symbol, ml.features.taker_buy_ratio
    ml.features.fetch_symbol = _fake_fetch
    ml.features.taker_buy_ratio = lambda s, exchange="bitget": 1.5
    try:
        unis = {u["name"]: u for u in ml._universes(_UNI_CFG)}
        blk = {"event": "FOMC", "ts": _now_ms(), "importance": "High"}
        clear = ml._scan_universe(unis["equities"], _UNI_CFG, blackout=None)
        _assert(len(clear["qualified"]) == 1, "no blackout -> equities candidate qualifies")
        hit = ml._scan_universe(unis["equities"], _UNI_CFG, blackout=blk)
        _assert(hit["qualified"] == [] and hit["event_blackout"] == blk,
                "blackout + opted-in universe -> candidacy withdrawn, event surfaced")
        _assert(hit["scored"], "scores stay on the board for visibility")
        crypto = ml._scan_universe(unis["crypto"], _UNI_CFG, blackout=blk)
        _assert(len(crypto["qualified"]) == 1 and crypto["event_blackout"] is None,
                "universe WITHOUT the flag is untouched by an active blackout")
    finally:
        ml.features.fetch_symbol, ml.features.taker_buy_ratio = orig_fetch, orig_taker


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} event-blackout tests passed.")
