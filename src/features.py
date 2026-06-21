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


def _to_records(obb: Any) -> list:
    """Normalize an OBBject / DataFrame / dict response into a list of dict rows."""
    if obb is None:
        return []
    for attr in ("to_records", "to_dict"):
        fn = getattr(obb, attr, None)
        if not callable(fn):
            continue
        try:
            out = fn()
        except Exception:
            continue
        if isinstance(out, list):
            return [r for r in out if isinstance(r, dict)]
        if isinstance(out, dict):
            values = list(out.values())
            if values and all(isinstance(v, (list, tuple)) for v in values):
                keys = list(out.keys())
                length = min(len(out[k]) for k in keys)
                return [{k: out[k][i] for k in keys} for i in range(length)]
            return [out]
    to_df = getattr(obb, "to_dataframe", None)
    if callable(to_df):
        try:
            return to_df().to_dict("records")
        except Exception:
            return []
    return []


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
