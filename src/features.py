"""Market feature extraction for the RUNECLAW scanner.

All data is fetched through ``getagent.data``. One ticker call per symbol
provides the 24h snapshot used by every scoring dimension (last, vwap, high,
low, 24h change, quote volume, and best bid/ask resting volume). A taker-volume
call on the gate asset supplies the optional taker-buy-dominance bonus.
"""
import math
from dataclasses import dataclass
from typing import Any, Optional

from getagent import data


def _model_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            out = dump()
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}
    return {}


def _to_records(obb: Any) -> list:
    """Normalize an OBBject response into a list of dict rows.

    The SDK's OBBject exposes no instance ``to_*`` methods; the real data lives
    in ``obb.results`` (a dict for snapshots such as ticker, a list for series
    such as kline), and the sanctioned converters are the module-level
    ``data.to_records(obb)`` helpers.
    """
    if obb is None:
        return []
    converter = getattr(data, "to_records", None)
    if callable(converter):
        try:
            out = converter(obb)
            if isinstance(out, list):
                recs = [r for r in (_model_dict(item) for item in out) if r]
                if recs:
                    return recs
        except Exception:
            pass
    results = getattr(obb, "results", None)
    if isinstance(results, dict):
        return [results]
    if isinstance(results, list):
        return [r for r in (_model_dict(item) for item in results) if r]
    single = _model_dict(results)
    return [single] if single else []


def _f(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


@dataclass
class SymbolFeatures:
    symbol: str
    ok: bool
    last: Optional[float] = None
    vwap: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    change_pct: Optional[float] = None
    quote_volume: Optional[float] = None
    bid_volume: Optional[float] = None
    ask_volume: Optional[float] = None
    note: str = ""


def fetch_symbol(symbol: str, exchange: str = "bitget") -> SymbolFeatures:
    try:
        obb = data.crypto.futures.ticker(symbol=symbol, exchange=exchange)
    except Exception as exc:
        return SymbolFeatures(symbol=symbol, ok=False, note="ticker_error:" + type(exc).__name__)

    rows = _to_records(obb)
    if not rows:
        return SymbolFeatures(symbol=symbol, ok=False, note="no_ticker_rows")

    row = rows[-1]
    feats = SymbolFeatures(
        symbol=symbol,
        ok=True,
        last=_f(row.get("last")),
        vwap=_f(row.get("vwap")),
        high=_f(row.get("high")),
        low=_f(row.get("low")),
        change_pct=_f(row.get("change_percent")),
        quote_volume=_f(row.get("quote_volume")),
        bid_volume=_f(row.get("bid_volume")),
        ask_volume=_f(row.get("ask_volume")),
    )
    if feats.last is None or feats.high is None or feats.low is None:
        feats.ok = False
        feats.note = "missing_core_price_fields"
    return feats


def taker_buy_ratio(symbol: str, exchange: str = "bitget") -> Optional[float]:
    """Latest taker buy/sell ratio for the gate asset; None when unavailable."""
    try:
        obb = data.crypto.futures.taker_volume(symbol=symbol, period="1h", limit=1, exchange=exchange)
    except Exception:
        return None
    rows = _to_records(obb)
    if not rows:
        return None
    row = rows[-1]
    ratio = _f(row.get("buy_sell_ratio"))
    if ratio is not None:
        return ratio
    buy_vol, sell_vol = _f(row.get("buy_vol")), _f(row.get("sell_vol"))
    if buy_vol is not None and sell_vol not in (None, 0):
        return buy_vol / sell_vol
    return None
