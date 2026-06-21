"""Follow-trade execution for RUNECLAW.

This module is invoked only through ``runtime.emit_signal_or_follow``'s
``execute_trade`` callback, which the runtime calls only when the subscription
is in follow-trade mode and the signal is actionable. Signal-only runs never
reach this code. It places a resting limit long with a tick-aligned stop and
first take-profit target, after a flat-account / no-pending-entry pre-check.
"""
from decimal import ROUND_DOWN, Decimal
from typing import Any

from getagent import trade


def _align(price: Any, step: Any) -> str:
    """Quantize a price down to the instrument tick size; return a string price."""
    try:
        quoted = Decimal(str(price))
    except Exception:
        return str(price)
    if step in (None, "", 0, "0"):
        return str(quoted)
    try:
        increment = Decimal(str(step))
        if increment <= 0:
            return str(quoted)
        aligned = (quoted / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        return str(aligned)
    except Exception:
        return str(quoted)


def execute(decision: dict, cfg: dict) -> dict:
    plan = decision.get("plan") or {}
    symbol = str(decision.get("symbol", ""))
    leverage = max(int(cfg.get("leverage", 10)), 1)
    entry = plan.get("entry")
    sl_price = plan.get("sl_price")
    tp1 = plan.get("tp1")
    margin = plan.get("margin_usdt")

    if not symbol or entry is None or sl_price is None or tp1 is None or not margin:
        return {"placed": False, "reason": "incomplete_plan"}

    # PRE-CHECK: do not stack onto an existing position or a resting entry.
    try:
        position = trade.helpers.find_contract_position(
            trade.contract.current_position(symbol=symbol), symbol
        )
    except Exception as exc:
        return {"placed": False, "reason": "position_check_error:" + type(exc).__name__}
    if position is not None:
        return {"placed": False, "reason": "already_in_position"}

    try:
        pending = trade.contract.pending_orders(symbol=symbol)
        existing = trade.helpers.select_contract_order(pending, symbol=symbol)
    except Exception:
        existing = None
    if existing is not None and getattr(existing, "order_id", ""):
        return {"placed": False, "reason": "entry_already_pending"}

    # Align the limit price to the instrument tick size.
    try:
        step = getattr(trade.helpers.contract_rules(symbol), "price_step", None)
    except Exception:
        step = None
    entry_price = _align(entry, step)

    # Size qty from the margin budget at the entry price.
    try:
        qty_plan = trade.helpers.compute_qty(
            symbol=symbol,
            market="contract",
            budget_amount=str(margin),
            leverage=leverage,
            price=str(entry_price),
        )
    except Exception as exc:
        return {"placed": False, "reason": "compute_qty_error:" + type(exc).__name__}

    # Validate + tick-align the stop and first take-profit trigger prices.
    try:
        tpsl = trade.helpers.resolve_contract_tpsl(
            symbol=symbol,
            side="long",
            leverage=leverage,
            tp_trigger_price=str(tp1),
            sl_trigger_price=str(sl_price),
            reference_price=str(entry_price),
        )
    except Exception as exc:
        return {"placed": False, "reason": "tpsl_error:" + type(exc).__name__}

    result = trade.contract.open_long_limit(
        symbol=symbol,
        qty=qty_plan.qty,
        price=entry_price,
        leverage=leverage,
        tp_trigger_price=tpsl.tp_trigger_price,
        sl_trigger_price=tpsl.sl_trigger_price,
    )
    placed = bool(trade.is_success(result))
    return {
        "placed": placed,
        "symbol": symbol,
        "qty": str(getattr(qty_plan, "qty", "")),
        "entry": str(entry_price),
        "tp1": str(tpsl.tp_trigger_price),
        "sl": str(tpsl.sl_trigger_price),
    }
