"""RUNECLAW scoring engine: BTC regime (long/short/neutral) + 0-100 directional score.

v0.1.0 adds short-side coverage. The BTC gate now resolves a *regime*:
- long  when BTC is up on the day and above VWAP (taker-buy bonus),
- short when BTC is down on the day and below VWAP (taker-sell bonus),
- none  otherwise (scanner reports scores but opens nothing).

Each coin is scored 0-100 *for the active direction*: long rewards relative
strength / above-VWAP / upper-range / bid-heavy book; short mirrors each.
"""
from dataclasses import dataclass
from typing import Optional

from .features import SymbolFeatures


@dataclass
class Regime:
    direction: str          # "long" | "short" | "none"
    size_factor: float      # 1.0 full, 0.5 reduced, 0.0 blocked
    score: int              # active gate sub-score (0-3)
    detail: dict


@dataclass
class Scored:
    symbol: str
    side: str
    score: float
    dims: dict
    skip: bool
    skip_reason: str
    features: SymbolFeatures


def regime(btc: SymbolFeatures, taker_ratio: Optional[float], cfg: dict) -> Regime:
    allow_short = bool(cfg.get("allow_short", True))
    up = bool(btc.ok and btc.change_pct is not None and btc.change_pct > 0)
    down = bool(btc.ok and btc.change_pct is not None and btc.change_pct < 0)
    above = bool(btc.ok and btc.last is not None and btc.vwap is not None and btc.last > btc.vwap)
    below = bool(btc.ok and btc.last is not None and btc.vwap is not None and btc.last < btc.vwap)
    taker_buy = bool(taker_ratio is not None and taker_ratio > 1.0)
    taker_sell = bool(taker_ratio is not None and taker_ratio < 1.0)

    long_score = (1 if up else 0) + (1 if above else 0) + (1 if taker_buy else 0)
    short_score = (1 if down else 0) + (1 if below else 0) + (1 if taker_sell else 0)

    detail = {
        "btc_change_pct": btc.change_pct,
        "above_vwap": above,
        "below_vwap": below,
        "taker_ratio": taker_ratio,
        "long_gate_score": long_score,
        "short_gate_score": short_score,
        "allow_short": allow_short,
    }

    if long_score >= 2:
        return Regime("long", 1.0, long_score, detail)
    if allow_short and short_score >= 2:
        return Regime("short", 1.0, short_score, detail)
    if long_score == 1 and long_score >= short_score:
        return Regime("long", 0.5, long_score, detail)
    if allow_short and short_score == 1:
        return Regime("short", 0.5, short_score, detail)
    return Regime("none", 0.0, max(long_score, short_score), detail)


def _minmax(values: list) -> tuple:
    if not values:
        return (0.0, 0.0)
    return (min(values), max(values))


def score_universe(feats_list: list, btc: SymbolFeatures, cfg: dict, direction: str) -> list:
    """Score every coin for ``direction`` ("long"/"short"; "none" falls back to
    long scores for board visibility). Returns Scored rows sorted best-first."""
    side = "short" if direction == "short" else "long"
    ok = [f for f in feats_list if f.ok]
    btc_change = btc.change_pct if (btc.ok and btc.change_pct is not None) else 0.0

    rel = {f.symbol: (f.change_pct - btc_change) for f in ok if f.change_pct is not None}
    rel_min, rel_max = _minmax(list(rel.values()))
    vol_min, vol_max = _minmax([f.quote_volume for f in ok if f.quote_volume is not None])

    min_vol = float(cfg.get("min_volume_usdt", "10000000"))
    full_ratio = float(cfg.get("bidask_full_ratio", "2.0"))
    partial_ratio = float(cfg.get("bidask_partial_ratio", "1.2"))
    wall_ratio = float(cfg.get("bidask_wall_ratio", "10.0"))

    results = []
    for f in feats_list:
        if not f.ok:
            results.append(Scored(f.symbol, side, 0.0, {"total": 0.0}, True, f.note or "no_data", f))
            continue

        dims: dict = {}
        skip = False
        reason = ""

        # Momentum 0-25: long rewards strength, short rewards weakness.
        r = rel.get(f.symbol)
        if r is None or rel_max <= rel_min:
            momentum = 12.5
        else:
            norm = (r - rel_min) / (rel_max - rel_min)
            momentum = 25.0 * norm if side == "long" else 25.0 * (1.0 - norm)
        dims["momentum"] = round(momentum, 2)
        dims["rel_strength"] = round(r, 4) if r is not None else None

        # VWAP position 0-20.
        if f.vwap and f.last is not None:
            above = f.last > f.vwap * 1.001
            below = f.last < f.vwap * 0.999
            if side == "long":
                vwap_score = 20.0 if above else (0.0 if below else 10.0)
            else:
                vwap_score = 20.0 if below else (0.0 if above else 10.0)
        else:
            vwap_score = 10.0
        dims["vwap"] = vwap_score

        # Range position 0-20.
        span = (f.high - f.low) if (f.high is not None and f.low is not None) else 0.0
        range_pos: Optional[float] = None
        if span > 0 and f.last is not None:
            range_pos = (f.last - f.low) / span
            if side == "long":
                range_score = 20.0 if range_pos > 0.66 else (10.0 if range_pos >= 0.33 else 0.0)
            else:
                range_score = 20.0 if range_pos < 0.34 else (10.0 if range_pos <= 0.67 else 0.0)
        else:
            range_score = 0.0
        dims["range"] = range_score
        dims["range_pos"] = round(range_pos, 3) if range_pos is not None else None

        # Order book 0-20 (best bid/ask resting imbalance).
        bid_vol, ask_vol = f.bid_volume, f.ask_volume
        if bid_vol is not None and ask_vol not in (None, 0) and bid_vol > 0:
            ratio = bid_vol / ask_vol  # > 1 bid-heavy
            dims["bidask_ratio"] = round(ratio, 3)
            favor = ratio if side == "long" else (ask_vol / bid_vol)
            if favor >= full_ratio:
                orderbook_score = 20.0
            elif favor >= partial_ratio:
                orderbook_score = 12.0
            elif favor >= 0.8:
                orderbook_score = 8.0
            else:
                orderbook_score = 4.0
            # Hard skip into an opposing wall.
            if side == "long" and (ask_vol / bid_vol) >= wall_ratio:
                skip, reason = True, "ask_wall"
            elif side == "short" and ratio >= wall_ratio:
                skip, reason = True, "bid_wall"
        else:
            orderbook_score = 8.0
            dims["bidask_ratio"] = None
            dims["orderbook_degraded"] = True
        dims["orderbook"] = orderbook_score

        # Volume 0-15 (cross-sectional) + thin-liquidity disqualifier.
        quote_volume = f.quote_volume
        if quote_volume is not None and quote_volume < min_vol:
            skip = True
            reason = reason or "thin_volume"
        if quote_volume is None or vol_max <= vol_min:
            volume_score = 7.5
        else:
            volume_score = 15.0 * (quote_volume - vol_min) / (vol_max - vol_min)
        dims["volume"] = round(volume_score, 2)
        dims["quote_volume"] = quote_volume

        total = momentum + vwap_score + range_score + orderbook_score + volume_score
        dims["total"] = round(total, 2)
        results.append(Scored(f.symbol, side, total, dims, skip, reason, f))

    results.sort(key=lambda s: (not s.skip, s.score), reverse=True)
    return results
