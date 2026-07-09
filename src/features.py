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
    # v0.9.34 swing-structure engine (populated by enrich(); consumers opt-in)
    swing_high: Optional[float] = None   # most recent CONFIRMED pivot high (lags k bars)
    swing_low: Optional[float] = None    # most recent CONFIRMED pivot low
    structure_dir: str = "neutral"       # HH+HL -> "long", LH+LL -> "short", else neutral
    candle_veto_long: str = ""           # counter-candle on the last closed bar vs a LONG
    candle_veto_short: str = ""          # ... vs a SHORT ("" = clean)


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
_BAR_O = ("open", "o", "open_price")          # v0.9.34: candle-pattern inputs
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


def realized_vol(bars: list, lookback: int = 30, ppy: int = 8760) -> Optional[float]:
    """v0.9.20: annualized realized volatility (%) = std of log returns over the
    last ``lookback`` closed 1h bars x sqrt(periods/yr) x 100. ppy=8760 (hourly).
    None if too few clean bars. Byte-mirrors research/replay.py:realized_vol so the
    sweep-validated vol-ceiling threshold transfers to live -- used by the vol-regime
    gate to stand aside on chaos-vol names (empirically the deepest-drawdown class:
    excluding them halved maxDD in the 21/35/42d replay windows). Reads closes via
    the same _bar_f accessor as _wilder_atr so it is robust to the live bar shape."""
    closes = []
    for b in bars[-(lookback + 1):]:
        c = _bar_f(b, _BAR_C)
        if c is not None and c > 0:
            closes.append(c)
    if len(closes) < lookback + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return (var ** 0.5) * (ppy ** 0.5) * 100.0


# --- v0.9.34 swing-structure + candle engine (pure, closed-bars-in) ----------
# The system's only "structure" was the ROLLING 24h high/low -- a window that
# shifts as old extremes age out, and the most crowded stop anchor on the chart
# (the live $1804.00 stop vs the $1804.37 swing top). These pure functions add
# real pivots. Data is computed unconditionally in enrich() (the bars are
# already in hand); every CONSUMER is a separate opt-in cfg gate, default off.

def swing_points(bars: list, k: int = 3) -> tuple:
    """Confirmed pivot highs/lows from CLOSED bars: bar i is a swing high when
    its high is the maximum of the 2k+1 bars centred on i (strictly greater
    than all k bars after it, >= the k before, so flat-top ties confirm on the
    LAST touch). Mirrored for lows. A pivot needs k closing bars after it, so
    confirmation lags k bars by construction -- that lag is the point: an
    unconfirmed extreme is just the current price. Returns (highs, lows) as
    [(index, price), ...] oldest->newest."""
    highs, lows = [], []
    n = len(bars)
    if n < 2 * k + 1 or k < 1:
        return (highs, lows)
    hs = [_bar_f(b, _BAR_H) for b in bars]
    ls = [_bar_f(b, _BAR_L) for b in bars]
    for i in range(k, n - k):
        h, l = hs[i], ls[i]
        if h is not None and all(x is not None for x in hs[i - k:i + k + 1]):
            if all(h >= hs[j] for j in range(i - k, i)) \
                    and all(h > hs[j] for j in range(i + 1, i + k + 1)):
                highs.append((i, h))
        if l is not None and all(x is not None for x in ls[i - k:i + k + 1]):
            if all(l <= ls[j] for j in range(i - k, i)) \
                    and all(l < ls[j] for j in range(i + 1, i + k + 1)):
                lows.append((i, l))
    return (highs, lows)


def structure_read(bars: list, k: int = 3) -> tuple:
    """(last_swing_high, last_swing_low, structure_dir) for enrich() to stash.
    structure_dir needs two pivots per side: HH+HL -> 'long', LH+LL -> 'short',
    anything mixed/insufficient -> 'neutral' (fail-neutral, never fail-directional)."""
    highs, lows = swing_points(bars, k)
    sh = highs[-1][1] if highs else None
    sl = lows[-1][1] if lows else None
    sdir = "neutral"
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        if hh and hl:
            sdir = "long"
        elif lh and ll:
            sdir = "short"
    return (sh, sl, sdir)


def candle_veto(bars: list, side: str, doji_body_frac: float = 0.15) -> str:
    """Counter-candle check on the LAST CLOSED bar for a candidate `side`.
    Returns '' (clean) or a compact veto reason:
      'doji'    -- body <= doji_body_frac of the bar's range (indecision at the
                   would-be entry; direction-agnostic)
      'engulf'  -- the last bar's body engulfs the prior bar's body AND closes
                   against `side` (bearish engulfing vetoes a long, bullish
                   engulfing vetoes a short).
    Fail-open: missing open/close/range data -> '' (never blocks on bad data)."""
    if len(bars) < 2:
        return ""
    o2, c2 = _bar_f(bars[-1], _BAR_O), _bar_f(bars[-1], _BAR_C)
    h2, l2 = _bar_f(bars[-1], _BAR_H), _bar_f(bars[-1], _BAR_L)
    o1, c1 = _bar_f(bars[-2], _BAR_O), _bar_f(bars[-2], _BAR_C)
    if None in (o2, c2, h2, l2):
        return ""
    rng = h2 - l2
    if rng > 0 and abs(c2 - o2) <= doji_body_frac * rng:
        return "doji"
    if None in (o1, c1):
        return ""
    against = (c2 < o2) if side == "long" else (c2 > o2)
    engulfs = max(o2, c2) >= max(o1, c1) and min(o2, c2) <= min(o1, c1) and abs(c2 - o2) > abs(c1 - o1)
    if against and engulfs:
        return "engulf"
    return ""


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


# --- v0.9.30 earnings blackout (per-symbol RWA binary-event guard) -----------
# The macro calendar above knows FOMC/CPI/NFP -- it does NOT know that MSTR
# reports earnings tonight. An RWA equity perp held (or entered) into a report
# is binary-event roulette the SL cannot price. This guard suppresses a SYMBOL's
# candidacy around its own report date. Same contract as event_blackout:
# opt-in (earnings_blackout_hours 0 = off), entries only (positions/limits are
# untouched), and FAIL-OPEN everywhere -- an unreadable calendar, an unexpected
# SDK method name, or an unparseable row can only ever mean "no blackout".
# Cost bound: ~1 report day per quarter per symbol (~1% of days) -- the reason
# this is shippable armed where the session gate (70% of hours) failed data
# validation.

def _earnings_window(rows: list, symbol: str, now_ms: float, pad_h: float):
    """Pure windowing helper (unit-testable). The calendar carries a report DATE
    plus a text reporting_time ('after market close'), not a precise timestamp,
    so the blackout is day-granular: [report 00:00 UTC - pad_h, report 24:00 UTC
    + pad_h]. That covers a pre-open print from the prior evening and an
    after-close print into the next session. Returns the matching row summary
    or None."""
    if pad_h <= 0:
        return None
    day_ms = 86_400_000.0
    pad_ms = pad_h * 3_600_000.0
    want = str(symbol or "").upper().replace("USDT", "")
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).upper()
        if want and sym and sym != want and want not in sym:
            continue
        ts = _parse_event_ts(row.get("report_date"))
        if ts is None:
            continue  # unparseable date -> ignore that row (fail-open per row)
        if ts - pad_ms <= now_ms <= ts + day_ms + pad_ms:
            return {"symbol": sym or want, "report_ts": ts,
                    "reporting_time": str(row.get("reporting_time", ""))[:24]}
    return None


def earnings_blackout(symbol: str, cfg: dict):
    """The active earnings blackout for one RWA equity symbol, or None. One
    bounded calendar read per qualified opted-in candidate per cycle (equities =
    at most 3 symbols). FAIL-OPEN on any failure."""
    try:
        pad_h = float(cfg.get("earnings_blackout_hours", "0") or "0")
    except (TypeError, ValueError):
        pad_h = 0.0
    if pad_h <= 0:
        return None
    base = str(symbol or "").upper().replace("USDT", "")
    if not base:
        return None
    from datetime import datetime, timezone
    now_ms = datetime.now(timezone.utc).timestamp() * 1000.0
    try:
        obb = data.equity.calendar.earnings(
            symbol=base,
            start_time=int(now_ms - 2 * 86_400_000),
            end_time=int(now_ms + 2 * 86_400_000))
        rows = [r for r in _to_records(obb) if isinstance(r, dict)]
    except Exception:
        return None  # unreadable/renamed endpoint -> no blackout (fail-open)
    return _earnings_window(rows, base, now_ms, pad_h)


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

    # v0.9.34: swing structure + candle read from the SAME closed entry-TF bars
    # (zero extra fetches). Data always populated; every consumer is a separate
    # opt-in gate. Fail-neutral/fail-open on thin or malformed bars.
    if bars:
        try:
            swing_k = int(cfg.get("swing_k", 3))
        except (TypeError, ValueError):
            swing_k = 3
        feats.swing_high, feats.swing_low, feats.structure_dir = structure_read(bars, swing_k)
        feats.candle_veto_long = candle_veto(bars, "long")
        feats.candle_veto_short = candle_veto(bars, "short")

    f_int = str(cfg.get("funding_interval", "1h"))
    f_win = int(cfg.get("funding_window", 8))
    feats.funding_now, feats.funding_avg, feats.funding_ok = fetch_funding(
        feats.symbol, interval=f_int, limit=f_win, exchange=exchange)
    return feats


# v0.9.42 -- asset-class classifier for MULTI-CLASS shadow discovery. Bitget
# lists a large, growing non-crypto RWA catalog: tokenized stocks
# (memory/semi-heavy -- SNDK/MU/SKHYNIX/DRAM/INTC/MRVL + big tech), ETFs
# (SOXL/SPCX/KORU/TQQQ/SQQQ/SPY/IWM/SMH/VOO...), and commodities (CL crude,
# XAUT tokenized gold). Each class needs its OWN regime leader -- scoring a
# stock perp under BTC is garbage (the v0.9.13 taker bug). This map routes each
# base to the right universe leader so discovery can score EVERY class
# correctly. Curated (a classifier allowlist, not exhaustive): an unlisted name
# defaults to crypto, which is the safe direction for the CRYPTO regime.
# v0.9.43 -- allowlist expanded to the fuller venue catalog read off the
# Stock-perps (Stocks + ETF) and Commodity-perps (Energy) tabs 2026-07-08,
# because the previous ~55-name curation silently routed the long tail
# (GE/MDB/ASML/GS/COST/SMH/VOO/KWEB/SGOV...) to BTC. Two notes:
#   * pre-IPO-style names (OPENAI = "preOPAI", ANTHROPIC) are listed under
#     Stock perps -> Stocks, so they class as EQUITIES (QQQ) -- there is no
#     distinct pre-IPO perp class on the venue (SPCX = space/SpaceX, classed
#     etf); the pre-IPO names are just stock perps.
#   * ENERGY bases fixed: the venue uses CL (WTI) / BZ (Brent) / NATGAS, not
#     the earlier aspirational BRENT/WTI/NG tokens (only CL ever matched, so
#     BZ/NATGAS were falling through to BTC). They route to the metals (XAU)
#     COMMODITY leader as the best available commodity proxy. A DEDICATED
#     energy leader (WTI/Brent-gated) is the right long-run home -- oil's
#     regime is not gold's -- but that is deferred: energy is unvalidated and
#     TRADING any RWA class stays a separate validated decision. This is a
#     shadow-log-quality fix (discovery logs, never trades), not a trade path.
_DISC_STOCK = frozenset("""SNDK MU SKHYNIX DRAM INTC MRVL SAMSUNG AMD MSFT PLTR
HOOD BABA META ORCL AMZN AAPL AVGO TSM GOOGL GOOG COIN NFLX XOM LLY KO CVX MCD
GME UNH WMT V ABNB NKE BA JPM CRCL OPEN TSLA NVDA MSTR DIS SBET DJT MARA RIOT
CLSK SQ SHOP PYPL UBER DELL SMCI ARM QCOM TXN CSCO IBM
GE MDB TER LRCX PANW COHR ASML GEV GS COST LIN LMT APP BRKB KIOXIA ROK CIEN
NOC BNC SUMIELEC DOOSENER DOOSBOT SMR ISRG COP SPIR NTAP PDD TENCENT SONY
ACHR NETEASE QUBT WDC OPENAI ANTHROPIC""".split())
_DISC_ETF = frozenset("""SOXL SPCX KORU TQQQ SQQQ SPY IWM QQQ GLD SLV USO UNG DIA
SOXS SPXL TNA UVXY VXX
EWY EWT EWJ EWH KWEB SOXX SMH VOO INDA SGOV RAMU CONL EUV DISK KSTR DIASTOCK
DFEN XLU""".split())
_DISC_COMMODITY = frozenset("""CL BZ NATGAS XAU XAG XAUT XAGT BRENT WTI NG HG PL PA""".split())


def classify_asset(base: str) -> str:
    """Route a perp base symbol to its asset class -> regime leader universe.
    stock/etf -> the equities (QQQ) universe; commodity/energy -> metals (XAU);
    everything else -> crypto (BTC). Curated; unknown defaults to crypto."""
    b = str(base or "").upper()
    if b in _DISC_STOCK or b in _DISC_ETF:
        return "equities"
    if b in _DISC_COMMODITY:
        return "metals"
    return "crypto"


def discovery_scan(exclude: set, cfg: dict, exchange: str = "bitget") -> tuple:
    """v0.9.38 -- whole-exchange new-listing catcher (SHADOW-mode data source).

    The 28-symbol core already contains every deep crypto perp on the venue
    (measured 2026-07-08: 697 listed perps, only ~12 non-core crypto names
    above the core's own $10M floor -- and the interesting ones are exactly
    the fresh-listing momentum class the operator has been adding by hand,
    weeks late: LAB/XPL/TRUMP/M, and today EVAA at +167%/$80M days after
    listing). A discovery rule CANNOT be honestly backtested -- today's
    ticker list only shows the survivors -- so this feeds a forward-test:
    candidates are scanned, scored, and logged every cycle; nothing trades
    off them unless the operator arms it after the forward record earns it.

    One bulk read serves the whole exchange; probes a small set of likely
    SDK bulk surfaces and fails OPEN (empty result + note) if none exists.
    Returns (list[SymbolFeatures] ranked by quote volume, note)."""
    try:
        min_qv = float(cfg.get("discovery_min_volume_usdt", "30000000") or "30000000")
        top_k = int(cfg.get("discovery_max", 4))
        per_class = int(cfg.get("discovery_max_per_class", top_k) or top_k)
    except (TypeError, ValueError):
        min_qv, top_k, per_class = 3e7, 4, 4
    block = {str(b).upper() for b in (cfg.get("discovery_blocklist") or [])}
    # v0.9.42 -- multi-class: classify each candidate and cap PER CLASS so
    # stocks/ETFs never crowd out crypto (or vice versa). Set discovery_classes
    # on the card to restrict (e.g. "crypto" for the old crypto-only behaviour).
    want = {c.strip().lower() for c in str(cfg.get("discovery_classes",
            "crypto,equities,metals") or "").split(",") if c.strip()}
    rows, how = None, ""
    for call in ("tickers", "ticker"):
        try:
            fn = getattr(data.crypto.futures, call, None)
            if fn is None:
                continue
            obb = fn(exchange=exchange) if call == "tickers" else fn(symbol="", exchange=exchange)
            got = _to_records(obb)
            if not got and isinstance(obb, (list, tuple)):   # bare row-list envelope
                got = [r for r in (_model_dict(x) or (x if isinstance(x, dict) else None)
                                   for x in obb) if r]
            if got and len(got) > 1:
                rows, how = got, call
                break
        except Exception:
            continue
    if not rows:
        # v0.9.45 -- BULK SURFACE BLIND (confirmed live 2026-07-09: the SDK has no
        # tickers()/ticker(symbol="") -- DISC-no_bulk_surface every cycle). Fall
        # back to probing a BOUNDED, explicitly-named watchlist per-symbol via the
        # proven single-symbol read (fetch_symbol -> ticker(symbol=X), the exact
        # call the core 28 already use). HONEST LIMITATION: this can only see names
        # we NAME -- it cannot enumerate the venue, so it monitors known non-core
        # names (RWA + any curated crypto) and CANNOT catch a brand-new unknown
        # listing the way a real bulk read would. Bounded at discovery_probe_max
        # symbols/cycle to cap the added ticker cost; fail-open per symbol.
        watchlist = cfg.get("discovery_watchlist") or []
        if watchlist:
            try:
                probe_max = int(cfg.get("discovery_probe_max", 12))
            except (TypeError, ValueError):
                probe_max = 12
            return (_discovery_watchlist_scan(
                watchlist, exclude, block, want, min_qv, per_class, probe_max,
                exchange), "watchlist")
        return [], "no_bulk_surface"
    out = []
    for row in rows:
        sym = str(row.get("symbol", "") or "").upper().replace("_", "").replace("-", "")
        if not sym.endswith("USDT") or sym in exclude:
            continue
        if sym[:-4] in block or sym in block:
            continue
        qv = _f(row.get("quote_volume"))
        if qv is None or qv < min_qv:
            continue
        vwap = _f(row.get("vwap"))
        if vwap is None:
            bv = _f(row.get("base_volume")) or _f(row.get("volume"))
            if bv and bv > 0 and qv:
                vwap = qv / bv          # 24h quote/base volume == the rolling VWAP
        f = SymbolFeatures(
            symbol=sym, ok=True,
            last=_f(row.get("last")), vwap=vwap,
            high=_f(row.get("high")), low=_f(row.get("low")),
            change_pct=_f(row.get("change_percent")),
            quote_volume=qv,
            bid_volume=_f(row.get("bid_volume")),
            ask_volume=_f(row.get("ask_volume")),
        )
        if f.last is None or f.high is None or f.low is None or f.vwap is None:
            continue
        cls = classify_asset(sym[:-4])
        if want and cls not in want:
            continue
        out.append((f, cls))
    out.sort(key=lambda fc: fc[0].quote_volume or 0.0, reverse=True)
    # per-class cap
    seen = {}
    capped = []
    for f, cls in out:
        n = seen.get(cls, 0)
        if n >= max(per_class, 0):
            continue
        seen[cls] = n + 1
        capped.append((f, cls))
    return capped, how


def _discovery_watchlist_scan(watchlist, exclude, block, want, min_qv,
                              per_class, probe_max, exchange="bitget"):
    """v0.9.45 -- per-symbol discovery fallback for when the bulk ticker surface
    is blind. Probes up to `probe_max` explicitly-named USDT perps through the
    proven single-symbol read (fetch_symbol), then applies the SAME floor /
    exclusion / blocklist / class-routing / per-class-cap logic as the bulk path.
    Fail-open: a symbol that errors, is thin, or lacks a VWAP is skipped, never
    fatal. Returns the capped [(SymbolFeatures, class)] list."""
    out, seen_syms = [], set()
    for raw in watchlist[:max(probe_max, 0)]:
        sym = str(raw or "").upper().replace("_", "").replace("-", "")
        if (not sym.endswith("USDT") or sym in exclude or sym in seen_syms
                or sym[:-4] in block or sym in block):
            continue
        seen_syms.add(sym)
        f = fetch_symbol(sym, exchange=exchange)
        if not (f.ok and f.last is not None and f.high is not None
                and f.low is not None and f.vwap is not None):
            continue
        if (f.quote_volume or 0.0) < min_qv:
            continue
        cls = classify_asset(sym[:-4])
        if want and cls not in want:
            continue
        out.append((f, cls))
    out.sort(key=lambda fc: fc[0].quote_volume or 0.0, reverse=True)
    seen, capped = {}, []
    for f, cls in out:
        n = seen.get(cls, 0)
        if n >= max(per_class, 0):
            continue
        seen[cls] = n + 1
        capped.append((f, cls))
    return capped
