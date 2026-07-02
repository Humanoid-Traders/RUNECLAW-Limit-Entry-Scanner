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
    # v0.2.0 kline engine (populated by enrich(); None/neutral until then)
    atr: Optional[float] = None
    trend_dir: str = "neutral"      # "long" | "short" | "neutral"
    trend_strength: float = 0.0     # [0, 1]
    kline_ok: bool = False
    # v0.2.0 funding engine
    funding_now: Optional[float] = None
    funding_avg: Optional[float] = None
    funding_ok: bool = False


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


# --- v0.2.0 kline + funding engines -----------------------------------------

_BAR_H = ("high", "h", "high_price")
_BAR_L = ("low", "l", "low_price")
_BAR_C = ("close", "c", "close_price")
_BAR_T = ("timestamp", "t", "time", "date", "ts", "start_time")


def _bar_f(bar: dict, keys: tuple) -> Optional[float]:
    for k in keys:
        if k in bar:
            v = _f(bar.get(k))
            if v is not None:
                return v
    return None


def fetch_klines(symbol: str, interval: str = "1h", limit: int = 50,
                 exchange: str = "bitget") -> list:
    """OHLC bars (oldest->newest) for ``symbol``; ``[]`` on any failure."""
    try:
        obb = data.crypto.futures.kline(symbol=symbol, interval=interval,
                                        limit=limit, exchange=exchange,
                                        data_type="ohlc")
    except Exception:
        return []
    bars = [r for r in _to_records(obb) if isinstance(r, dict)]
    if bars and _bar_f(bars[0], _BAR_T) is not None:
        bars.sort(key=lambda b: _bar_f(b, _BAR_T) or 0.0)
    return bars


_INTERVAL_S = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
               "1h": 3600, "4h": 14400, "1d": 86400}


def _closed_bars(bars: list, interval: str) -> list:
    """v0.9.4: drop the LAST bar iff its open-time says it is still forming
    (open_ts + interval > now). Deterministic and side-effect-free on a feed of
    closed candles: a closed last bar fails the condition and nothing changes;
    only a genuinely in-progress bar is excluded, so indicators (ATR / trend)
    never ingest a partial candle. Fail-open: an unparseable timestamp or
    unknown interval keeps the bar (prior behavior)."""
    if not bars:
        return bars
    step = _INTERVAL_S.get(interval)
    if step is None:
        return bars
    ts = _bar_f(bars[-1], _BAR_T)
    if ts is None or ts <= 0:
        return bars
    if ts > 10_000_000_000:  # ms epoch -> s
        ts /= 1000.0
    from datetime import datetime, timezone
    if ts + step > datetime.now(timezone.utc).timestamp():
        return bars[:-1]
    return bars


def _wilder_atr(bars: list, period: int) -> Optional[float]:
    """Wilder-smoothed ATR over ``period`` bars; None if too few clean bars.

    v0.9.4: fail-safe on a NON-CONTIGUOUS series -- if a malformed bar is
    dropped from the MIDDLE of the window, the surviving neighbors are not
    adjacent and their stitched true-range is fictitious (h_i vs a close from
    two bars back). Rather than compute a wrong ATR, return None and let
    ``enrich`` degrade to the documented range-proxy path. Malformed bars at
    the very ends trim cleanly and do not invalidate the series."""
    highs, lows, closes, idxs = [], [], [], []
    for i, b in enumerate(bars):
        h, l, c = _bar_f(b, _BAR_H), _bar_f(b, _BAR_L), _bar_f(b, _BAR_C)
        if h is None or l is None or c is None:
            continue
        highs.append(h); lows.append(l); closes.append(c); idxs.append(i)
    n = len(closes)
    if n < period + 1:
        return None
    if idxs and (idxs[-1] - idxs[0] + 1) != n:
        return None  # mid-series gap -> stitched TRs would be fictitious
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr if atr > 0 else None


def _ema_trend(bars: list, lookback: int, norm: float) -> tuple:
    """(trend_dir, trend_strength[0,1]) from last close vs EMA(lookback)."""
    closes = [c for c in (_bar_f(b, _BAR_C) for b in bars) if c is not None]
    if len(closes) < lookback + 1:
        return ("neutral", 0.0)
    k = 2.0 / (lookback + 1.0)
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1.0 - k)
    last = closes[-1]
    if ema <= 0:
        return ("neutral", 0.0)
    gap = (last - ema) / ema
    strength = min(abs(gap) / norm, 1.0) if norm > 0 else 0.0
    if gap > 0:
        return ("long", strength)
    if gap < 0:
        return ("short", strength)
    return ("neutral", 0.0)


def fetch_funding(symbol: str, interval: str = "1h", limit: int = 8,
                  exchange: str = "bitget") -> tuple:
    """(funding_now, funding_avg, ok): latest fr_close + trailing window mean."""
    try:
        obb = data.crypto.futures.funding_rate(symbol=symbol, interval=interval,
                                               limit=limit, exchange=exchange)
    except Exception:
        return (None, None, False)
    rows = [b for b in _to_records(obb) if isinstance(b, dict)]
    rates = [r for r in (_f(b.get("fr_close")) for b in rows) if r is not None]
    if not rates:  # some shapes expose a flat 'funding_rate'
        rates = [r for r in (_f(b.get("funding_rate")) for b in rows) if r is not None]
    if not rates:
        return (None, None, False)
    return (rates[-1], sum(rates) / len(rates), True)


# --- v0.9.5 macro event blackout (RWA equity gap risk; audit finding) --------
# RWA equity perps track underlying stocks that gap on high-importance US macro
# prints (FOMC, CPI, NFP); the SL/trail are tuned on crypto's continuous tape.
# This guard suppresses NEW entries for opted-in universes inside a window
# around such events. Opt-in (event_blackout_hours 0 = off) and FAIL-OPEN: an
# unreadable calendar never blocks trading -- the guard only ever adds caution
# when the data affirmatively shows an event.

_EVENT_TS_KEYS = ("date", "time", "timestamp", "datetime")


def _parse_event_ts(value: Any) -> Optional[float]:
    """Event timestamp -> epoch ms. Accepts epoch s/ms or an ISO-8601 string
    (with or without timezone; naive strings are treated as UTC). None if
    unparseable (that event is simply ignored -- fail-open per event)."""
    num = _f(value)
    if num is not None and num > 0:
        return num * 1000.0 if num < 10_000_000_000 else num
    if not isinstance(value, str) or not value.strip():
        return None
    from datetime import datetime, timezone
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def _blackout_event(events: list, now_ms: float, window_h: float,
                    importance: str) -> Optional[dict]:
    """First event within +/- window_h of now whose importance matches
    (case-insensitive substring, so 'high' matches 'High'; '3' matches '3').
    Pure -> unit-testable. Returns {'event', 'ts', 'importance'} or None."""
    if window_h <= 0:
        return None
    win_ms = window_h * 3_600_000.0
    imp_want = str(importance or "").strip().lower()
    for row in events:
        if not isinstance(row, dict):
            continue
        imp = str(row.get("importance", "")).strip().lower()
        if imp_want and imp_want not in imp:
            continue
        ts = None
        for k in _EVENT_TS_KEYS:
            if k in row:
                ts = _parse_event_ts(row.get(k))
                if ts is not None:
                    break
        if ts is None or abs(ts - now_ms) > win_ms:
            continue
        return {"event": str(row.get("event", "?"))[:48], "ts": ts,
                "importance": str(row.get("importance", ""))[:16]}
    return None


def event_blackout(cfg: dict) -> Optional[dict]:
    """The active macro-event blackout, or None. One bounded calendar call per
    cycle, made only when the guard is enabled. FAIL-OPEN on any failure."""
    try:
        window_h = float(cfg.get("event_blackout_hours", "0") or "0")
    except (TypeError, ValueError):
        window_h = 0.0
    if window_h <= 0:
        return None
    country = str(cfg.get("event_blackout_country", "US"))
    importance = str(cfg.get("event_blackout_importance", "high"))
    from datetime import datetime, timezone
    now_ms = datetime.now(timezone.utc).timestamp() * 1000.0
    win_ms = window_h * 3_600_000.0
    try:
        obb = data.economy.calendar(start_time=int(now_ms - win_ms),
                                    end_time=int(now_ms + win_ms),
                                    country=country, importance=importance)
        events = [r for r in _to_records(obb) if isinstance(r, dict)]
    except Exception:
        return None  # unreadable calendar -> no blackout (fail-open)
    return _blackout_event(events, now_ms, window_h, importance)


def enrich(feats: SymbolFeatures, cfg: dict, exchange: str = "bitget") -> SymbolFeatures:
    """Pass-2: populate kline (ATR + trend) and funding fields on ``feats``.

    Mutates and returns ``feats``. Any failure degrades gracefully -- kline_ok /
    funding_ok stay False, leaving the ATR proxy and neutral trend in effect."""
    if not feats.ok:
        return feats
    k_int = str(cfg.get("kline_interval", "1h"))
    atr_period = int(cfg.get("atr_period", 14))
    t_int = str(cfg.get("trend_interval", "4h"))
    t_look = int(cfg.get("trend_lookback", 12))
    t_norm = float(cfg.get("trend_norm", "0.05"))

    bars = _closed_bars(fetch_klines(feats.symbol, interval=k_int,
                                     limit=max(atr_period + 5, 30), exchange=exchange), k_int)
    atr = _wilder_atr(bars, atr_period) if bars else None
    if atr is not None:
        feats.atr = atr
        feats.kline_ok = True

    t_bars = bars if t_int == k_int else _closed_bars(fetch_klines(
        feats.symbol, interval=t_int, limit=max(t_look + 5, 20), exchange=exchange), t_int)
    feats.trend_dir, feats.trend_strength = _ema_trend(t_bars, t_look, t_norm)

    f_int = str(cfg.get("funding_interval", "1h"))
    f_win = int(cfg.get("funding_window", 8))
    feats.funding_now, feats.funding_avg, feats.funding_ok = fetch_funding(
        feats.symbol, interval=f_int, limit=f_win, exchange=exchange)
    return feats
