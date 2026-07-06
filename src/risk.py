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
    entry_mode: str = "pullback"  # v0.5.0: "pullback" (limit) or "breakout" (market)


def _sl_min_fraction(symbol: str, cfg: dict) -> float:
    if symbol in _BTC_ETH:
        return float(cfg.get("sl_min_btc_eth_pct", "1.5")) / 100.0
    if symbol in _SOL_BNB:
        return float(cfg.get("sl_min_sol_bnb_pct", "1.2")) / 100.0
    return float(cfg.get("sl_min_alt_pct", "2.5")) / 100.0


def build_plan(feats: SymbolFeatures, cfg: dict, size_factor: float, side: str = "long",
               entry_mode: str = "pullback") -> Optional[TradePlan]:
    if not feats.ok or feats.high is None or feats.low is None or feats.vwap is None:
        return None

    high, low, vwap = feats.high, feats.low, feats.vwap
    # v0.2.0: prefer the real Wilder ATR from the kline engine; fall back to the
    # 24h-range proxy when enrichment was unavailable for this candidate.
    if getattr(feats, "kline_ok", False) and feats.atr and feats.atr > 0:
        atr = feats.atr
    else:
        atr = max((high - low) / 2.5, 0.0)
    atr_mult = float(cfg.get("atr_limit_mult", "0.3"))   # defaults aligned with manifest (v0.9.4)
    tp1_pct = float(cfg.get("tp1_pct", "5.0")) / 100.0
    tp2_pct = float(cfg.get("tp2_pct", "15.0")) / 100.0
    # v0.9.22 trade-type packs: each entry mode may carry its OWN tp2 backstop
    # width. Beyond tuning the backstop, a DISTINCT width is the stateless mode
    # marker: the attached TP plan order is the only surviving record of how a
    # position was opened, so execution._position_entry_mode recovers pullback-
    # vs-breakout from |tp - entry|/entry at manage time (the same exchange-is-
    # the-memory contract as the trail). 0 = inherit tp2_pct. NOTE (Phase-2 A/B,
    # 28-symbol set): widening the BREAKOUT backstop 20->25 costs 5-7pt net in
    # every window -- the 20% backstop genuinely governs breakout exits -- so the
    # marker is armed on the PULLBACK side (pullback_tp2_pct), whose 4h-capped
    # trades never run far enough for the backstop width to matter.
    try:
        _mk = "breakout_tp2_pct" if entry_mode == "breakout" else "pullback_tp2_pct"
        _m2 = float(cfg.get(_mk, "0") or "0") / 100.0
    except (TypeError, ValueError):
        _m2 = 0.0
    if _m2 > 0:
        tp2_pct = _m2
    trail_atr = float(cfg.get("trail_atr_mult", "2.0")) * atr
    be_pct = float(cfg.get("breakeven_pct", "2.0")) / 100.0
    sl_min = _sl_min_fraction(feats.symbol, cfg)

    if entry_mode == "breakout":
        # v0.5.0: enter at market (entry ~= last); the stop hugs the broken level
        # (24h high for a long, low for a short) widened to at least an ATR so a
        # clean breakout isn't wicked out, and floored at the per-symbol sl_min.
        if feats.last is None or feats.last <= 0:
            return None
        entry = feats.last
        buf = float(cfg.get("breakout_level_buffer_pct", "0.2")) / 100.0
        stop_atr_mult = float(cfg.get("breakout_stop_atr_mult", "1.0"))
        bk_tp1_pct = float(cfg.get("breakout_tp1_pct", "4.0")) / 100.0
        if side == "short":
            struct_stop = low * (1.0 + buf)            # just above the broken 24h low
            vol_stop = entry + stop_atr_mult * atr
            raw_stop = max(struct_stop, vol_stop)      # wider (higher) of the two
            sl_pct = max((raw_stop - entry) / entry, sl_min)
            sl_price = entry * (1.0 + sl_pct)
            tp1 = entry * (1.0 - bk_tp1_pct)
            tp2 = entry * (1.0 - tp2_pct)
            breakeven_price = entry * (1.0 - be_pct)
        else:
            struct_stop = high * (1.0 - buf)           # just below the broken 24h high
            vol_stop = entry - stop_atr_mult * atr
            raw_stop = min(struct_stop, vol_stop)      # wider (lower) of the two
            sl_pct = max((entry - raw_stop) / entry, sl_min)
            sl_price = entry * (1.0 - sl_pct)
            tp1 = entry * (1.0 + bk_tp1_pct)
            tp2 = entry * (1.0 + tp2_pct)
            breakeven_price = entry * (1.0 + be_pct)
    elif side == "short":
        # v0.9.28 candidate -- pullback_stop_buffer_pct: pad the structural stop
        # BEYOND the 24h extreme instead of sitting exactly ON it. The raw pullback
        # stop is the most crowded, most-hunted price on the chart (live 2026-07-06:
        # ETH short SL $1804.00, swing top $1804.37 -- tagged by 37 cents, then a
        # $17 reversal in the trade's favour). Breakout stops have carried exactly
        # this pad since v0.5.0 (breakout_level_buffer_pct); pullbacks never did.
        # Risk-neutral by construction: sizing solves backward from the wider stop,
        # so max_loss_usdt is unchanged -- the cost is a slightly smaller position,
        # not a bigger loss. 0 (default) = exact pre-v0.9.28 behaviour.
        try:
            pb_buf = float(cfg.get("pullback_stop_buffer_pct", "0") or "0") / 100.0
        except (TypeError, ValueError):
            pb_buf = 0.0
        entry = vwap + atr_mult * atr
        if entry <= 0:
            return None
        struct = high * (1.0 + pb_buf)
        raw_sl_pct = (struct - entry) / entry if struct > entry else 0.0
        sl_pct = max(raw_sl_pct, sl_min)
        sl_price = entry * (1.0 + sl_pct)
        tp1 = entry * (1.0 - tp1_pct)
        tp2 = entry * (1.0 - tp2_pct)
        breakeven_price = entry * (1.0 - be_pct)
    else:
        try:
            pb_buf = float(cfg.get("pullback_stop_buffer_pct", "0") or "0") / 100.0
        except (TypeError, ValueError):
            pb_buf = 0.0
        entry = vwap - atr_mult * atr
        if entry <= 0:
            return None
        struct = low * (1.0 - pb_buf)
        raw_sl_pct = (entry - struct) / entry if entry > struct else 0.0
        sl_pct = max(raw_sl_pct, sl_min)
        sl_price = entry * (1.0 - sl_pct)
        tp1 = entry * (1.0 + tp1_pct)
        tp2 = entry * (1.0 + tp2_pct)
        breakeven_price = entry * (1.0 + be_pct)

    if entry <= 0 or sl_pct <= 0 or tp1 <= 0:
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
        entry_mode=entry_mode,
    )
