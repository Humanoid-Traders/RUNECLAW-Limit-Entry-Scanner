"""v0.6.4 isolated-margin dispatch tests.

The SDK is runner-managed and cannot be imported offline, so we stub ``getagent``
(same approach as research/replay.py) and assert the DISPATCH + direction mapping:

  - margin_mode 'crossed' (default) routes to the proven open_* wrappers, never place_order
  - margin_mode 'isolated' routes to place_order(margin_mode='isolated') with the correct
    buy/sell side, hedge pos_side/trade_side, order_type, and threaded qty/tp/sl
  - a rejected isolated open is fail-closed: it returns placed=False (exchange_reject)
    and does NOT silently fall back to a crossed wrapper open

Run: python3 tests/test_isolated_margin.py
"""
import sys
import types
from pathlib import Path

# --- stub getagent (engine modules import it at module load) ---
_g = types.ModuleType("getagent"); sys.modules["getagent"] = _g
for _sub in ("data", "trade", "runtime"):
    _m = types.ModuleType("getagent." + _sub); setattr(_g, _sub, _m)
    sys.modules["getagent." + _sub] = _m

_SRC = Path(__file__).resolve().parent.parent / "src"
_pkg = types.ModuleType("src"); _pkg.__path__ = [str(_SRC)]; sys.modules["src"] = _pkg

_trade = sys.modules["getagent.trade"]


class _Box:
    def __init__(self, **kw): self.__dict__.update(kw)


class _Recorder:
    """Records every contract/helper call; lets each test set is_success."""
    def __init__(self, success=True):
        self.calls = []        # list of (method, kwargs)
        self._success = success

    # -- contract surface --
    def _rec(self, name):
        def fn(**kw):
            self.calls.append((name, kw))
            return _Box(ok=self._success, name=name, raw=kw)
        return fn

    def names(self):
        return [c[0] for c in self.calls]

    def kw(self, name):
        for n, kw in self.calls:
            if n == name:
                return kw
        return None


def _install(rec):
    contract = types.SimpleNamespace(
        current_position=rec._rec("current_position"),
        pending_orders=rec._rec("pending_orders"),
        change_leverage=rec._rec("change_leverage"),
        place_order=rec._rec("place_order"),
        open_long_limit=rec._rec("open_long_limit"),
        open_short_limit=rec._rec("open_short_limit"),
        open_long_market=rec._rec("open_long_market"),
        open_short_market=rec._rec("open_short_market"),
    )
    helpers = types.SimpleNamespace(
        count_open_contract_positions=lambda *a, **k: 0,
        select_contract_order=lambda *a, **k: None,
        contract_rules=lambda *a, **k: _Box(price_step=None),
        compute_qty=lambda **k: _Box(qty="313"),
        resolve_contract_tpsl=lambda **k: _Box(tp_trigger_price="2.1993",
                                               sl_trigger_price="1.8646"),
    )
    _trade.contract = contract
    _trade.helpers = helpers
    _trade.is_success = lambda r: bool(getattr(r, "ok", False))


# import AFTER the stub is in place
import importlib.util  # noqa: E402

def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SRC / (name.split(".")[-1] + ".py"))
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

# features is imported by execution at load; load it under the stub first
_load("src.features")
execution = _load("src.execution")


def _decision(side="long", entry_mode="pullback"):
    return {"symbol": "NEARUSDT", "plan": {
        "side": side, "entry": 1.9134, "sl_price": 1.8646, "tp1": 2.0,
        "tp2": 2.1993, "margin_usdt": "60", "entry_mode": entry_mode}}


def _cfg(margin_mode):
    return {"margin_mode": margin_mode, "leverage": 10, "trail_atr_mult": "2.0",
            "max_concurrent": 3, "max_correlated_alts": 2}


_MGMT = {"circuit": "ok", "open_count": 0, "open_symbols": []}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def test_crossed_uses_wrapper():
    rec = _Recorder(success=True); _install(rec)
    out = execution.open_if_allowed(_decision("long"), _cfg("crossed"), _MGMT)
    _assert(out["placed"], "crossed long places")
    _assert("open_long_limit" in rec.names(), "crossed long -> open_long_limit wrapper")
    _assert("place_order" not in rec.names(), "crossed NEVER calls place_order")
    _assert(out["margin_mode"] == "crossed", "result surfaces margin_mode=crossed")


def test_isolated_long_uses_place_order():
    rec = _Recorder(success=True); _install(rec)
    out = execution.open_if_allowed(_decision("long"), _cfg("isolated"), _MGMT)
    _assert(out["placed"], "isolated long places")
    _assert("place_order" in rec.names(), "isolated long -> place_order")
    _assert("open_long_limit" not in rec.names(), "isolated does NOT call the wrapper")
    _assert("change_leverage" in rec.names(), "isolated sets leverage first")
    kw = rec.kw("place_order")
    _assert(kw["margin_mode"] == "isolated", "place_order margin_mode=isolated")
    _assert(kw["side"] == "buy", "long -> side=buy")
    _assert(kw["pos_side"] == "long", "long -> pos_side=long")
    _assert(kw["trade_side"] == "open", "trade_side=open")
    _assert(kw["order_type"] == "limit", "pullback -> order_type=limit")
    _assert(kw["qty"] == "313", "qty threaded through")
    _assert(kw["sl_trigger_price"] == "1.8646", "sl threaded through")
    _assert(out["margin_mode"] == "isolated", "result surfaces margin_mode=isolated")


def test_isolated_short_maps_sell():
    rec = _Recorder(success=True); _install(rec)
    out = execution.open_if_allowed(_decision("short"), _cfg("isolated"), _MGMT)
    kw = rec.kw("place_order")
    _assert(kw["side"] == "sell", "short -> side=sell")
    _assert(kw["pos_side"] == "short", "short -> pos_side=short")
    _assert(out["placed"], "isolated short places")


def test_isolated_breakout_is_market():
    rec = _Recorder(success=True); _install(rec)
    execution.open_if_allowed(_decision("long", "breakout"), _cfg("isolated"), _MGMT)
    kw = rec.kw("place_order")
    _assert(kw["order_type"] == "market", "breakout -> order_type=market")
    _assert(kw["price"] == "", "market -> empty price")


def test_isolated_reject_is_fail_closed():
    rec = _Recorder(success=False); _install(rec)
    out = execution.open_if_allowed(_decision("long"), _cfg("isolated"), _MGMT)
    _assert(not out["placed"], "rejected isolated open -> placed False")
    _assert(out.get("reason", "").startswith("exchange_reject"), "surfaces exchange_reject")
    _assert("open_long_limit" not in rec.names(), "NO silent fallback to crossed wrapper")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} isolated-margin tests passed.")
