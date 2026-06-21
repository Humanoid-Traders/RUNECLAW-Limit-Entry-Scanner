"""Limit price, stop, take-profit ladder, and dollar-risk sizing for RUNECLAW.

v0.1.0 is side-aware. For longs the limit rests below VWAP and the stop sits
below the 24h low; for shorts the limit rests above VWAP and the stop sits above
the 24h high. Sizing is always solved backward from a fixed per-trade dollar
risk cap, then capped by the margin budget.
"""
from dataclasses import dataclass
from typing import Optional

from .features import SymbolFeatures

_BTC_ETH = {"BTCUSDT", "ETHUSDT"}
_SOL_BNB = {"SOLUSDT", "BNBUSDT"}


@dataclass
class TradePlan:
    symbol: str
    side: str
    entry: float
    atr: float
    sl_price: float
    sl_pct: float
    tp1: float
    tp2: float
    trail_atr: float
    breakeven_price: float
    notional_usdt: float
    margin_usdt: float
    leverage: int
    size_factor: float
    sizing_ok: bool
    note: str = ""


def _sl_min_fraction(symbol: str, cfg: dict) -> float:
    if symbol in _BTC_ETH:
        return float(cfg.get("sl_min_btc_eth_pct", "1.5")) / 100.0
    if symbol in _SOL_BNB:
        return float(cfg.get("sl_min_sol_bnb_pct", "1.2")) / 100.0
    return float(cfg.get("sl_min_alt_pct", "2.5")) / 100.0


def build_plan(feats: SymbolFeatures, cfg: dict, size_factor: float, side: str = "long") -> Optional[TradePlan]:
    if not feats.ok or feats.high is None or feats.low is None or feats.vwap is None:
        return None

    high, low, vwap = feats.high, feats.low, feats.vwap
    atr = max((high - low) / 2.5, 0.0)
    atr_mult = float(cfg.get("atr_limit_mult", "0.5"))
    tp1_pct = float(cfg.get("tp1_pct", "3.5")) / 100.0
    tp2_pct = float(cfg.get("tp2_pct", "7.0")) / 100.0
    trail_atr = float(cfg.get("trail_atr_mult", "1.0")) * atr
    be_pct = float(cfg.get("breakeven_pct", "2.0")) / 100.0
    sl_min = _sl_min_fraction(feats.symbol, cfg)

    if side == "short":
        entry = vwap + atr_mult * atr
        if entry <= 0:
            return None
        raw_sl_pct = (high - entry) / entry if high > entry else 0.0
        sl_pct = max(raw_sl_pct, sl_min)
        sl_price = entry * (1.0 + sl_pct)
        tp1 = entry * (1.0 - tp1_pct)
        tp2 = entry * (1.0 - tp2_pct)
        breakeven_price = entry * (1.0 - be_pct)
    else:
        entry = vwap - atr_mult * atr
        if entry <= 0:
            return None
        raw_sl_pct = (entry - low) / entry if entry > low else 0.0
        sl_pct = max(raw_sl_pct, sl_min)
        sl_price = entry * (1.0 - sl_pct)
        tp1 = entry * (1.0 + tp1_pct)
        tp2 = entry * (1.0 + tp2_pct)
        breakeven_price = entry * (1.0 + be_pct)

    if sl_pct <= 0 or tp1 <= 0:
        return None

    max_loss = float(cfg.get("max_loss_usdt", "15"))
    leverage = max(int(cfg.get("leverage", 10)), 1)
    notional = (max_loss / sl_pct) * max(size_factor, 0.0)
    margin = notional / leverage

    budget = float(cfg.get("margin_budget", "100"))
    note = ""
    if budget > 0 and margin > budget:
        margin = budget
        notional = margin * leverage
        note = "capped_by_margin_budget"

    return TradePlan(
        symbol=feats.symbol,
        side=side,
        entry=entry,
        atr=atr,
        sl_price=sl_price,
        sl_pct=sl_pct,
        tp1=tp1,
        tp2=tp2,
        trail_atr=trail_atr,
        breakeven_price=breakeven_price,
        notional_usdt=notional,
        margin_usdt=margin,
        leverage=leverage,
        size_factor=size_factor,
        sizing_ok=(notional > 0 and margin > 0),
        note=note,
    )
