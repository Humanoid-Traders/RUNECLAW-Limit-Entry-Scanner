"""RUNECLAW scoring engine: BTC regime gate + 0-100 universe score.

The gate decides whether any long is allowed and whether size is full or
reduced. The universe score blends momentum, VWAP position, range position,
order-book imbalance, and liquidity, exactly as the RUNECLAW methodology
describes. Momentum and volume are scored cross-sectionally (relative to the
scanned peers), which is unit-robust and matches the spec's "vs BTC" and "vs
universe peers" language.
"""
from dataclasses import dataclass
from typing import Any, Optional

from .features import SymbolFeatures


@dataclass
class GateResult:
    score: int
    open: bool
    size_factor: float
    detail: dict


@dataclass
class Scored:
    symbol: str
    score: float
    dims: dict
    skip: bool
    skip_reason: str
    features: SymbolFeatures


def btc_gate(btc: SymbolFeatures, taker_ratio: Optional[float]) -> GateResult:
    """Score 0-3: +1 BTC 24h change positive, +1 above VWAP, +1 taker buy dominant."""
    score = 0
    change_positive = bool(btc.ok and btc.change_pct is not None and btc.change_pct > 0)
    if change_positive:
        score += 1
    above_vwap = bool(btc.ok and btc.last is not None and btc.vwap is not None and btc.last > btc.vwap)
    if above_vwap:
        score += 1
    taker_dominant = bool(taker_ratio is not None and taker_ratio > 1.0)
    if taker_dominant:
        score += 1

    detail = {
        "change_positive": change_positive,
        "above_vwap": above_vwap,
        "taker_buy_dominant": taker_dominant,
        "taker_ratio": taker_ratio,
        "btc_change_pct": btc.change_pct,
    }
    # Score >= 2 -> universe open (full size); == 1 -> reduced size; 0 -> closed.
    if score >= 2:
        return GateResult(score=score, open=True, size_factor=1.0, detail=detail)
    if score == 1:
        return GateResult(score=score, open=True, size_factor=0.5, detail=detail)
    return GateResult(score=score, open=False, size_factor=0.0, detail=detail)


def _minmax(values: list) -> tuple:
    if not values:
        return (0.0, 0.0)
    return (min(values), max(values))


def score_universe(feats_list: list, btc: SymbolFeatures, cfg: dict) -> list:
    """Return Scored rows sorted best-first (non-skipped, then score desc)."""
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
            results.append(Scored(f.symbol, 0.0, {"total": 0.0}, True, f.note or "no_data", f))
            continue

        dims: dict = {}
        skip = False
        reason = ""

        # Momentum 0-25 (cross-sectional relative strength vs BTC).
        r = rel.get(f.symbol)
        if r is None or rel_max <= rel_min:
            momentum = 12.5
        else:
            momentum = 25.0 * (r - rel_min) / (rel_max - rel_min)
        dims["momentum"] = round(momentum, 2)
        dims["rel_strength"] = round(r, 4) if r is not None else None

        # VWAP position 0-20.
        if f.vwap and f.last is not None:
            if f.last > f.vwap * 1.001:
                vwap_score = 20.0
            elif f.last >= f.vwap * 0.999:
                vwap_score = 10.0
            else:
                vwap_score = 0.0
        else:
            vwap_score = 10.0
        dims["vwap"] = vwap_score

        # Range position 0-20.
        span = (f.high - f.low) if (f.high is not None and f.low is not None) else 0.0
        range_pos: Optional[float] = None
        if span > 0 and f.last is not None:
            range_pos = (f.last - f.low) / span
            if range_pos > 0.66:
                range_score = 20.0
            elif range_pos >= 0.33:
                range_score = 10.0
            else:
                range_score = 0.0
        else:
            range_score = 0.0
        dims["range"] = range_score
        dims["range_pos"] = round(range_pos, 3) if range_pos is not None else None

        # Order book 0-20 (best bid/ask resting volume imbalance).
        bid_vol, ask_vol = f.bid_volume, f.ask_volume
        if bid_vol is not None and ask_vol not in (None, 0):
            ratio = bid_vol / ask_vol
            dims["bidask_ratio"] = round(ratio, 3)
            if ratio >= full_ratio:
                orderbook_score = 20.0
            elif ratio >= partial_ratio:
                orderbook_score = 12.0
            elif ratio >= 0.8:
                orderbook_score = 8.0
            else:
                orderbook_score = 4.0
            if bid_vol > 0 and (ask_vol / bid_vol) >= wall_ratio:
                skip = True
                reason = "ask_wall"
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
        results.append(Scored(f.symbol, total, dims, skip, reason, f))

    results.sort(key=lambda s: (not s.skip, s.score), reverse=True)
    return results
