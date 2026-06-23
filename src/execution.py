"""Follow-trade execution + position management for RUNECLAW v0.1.0.

Two entry points, both reached only in follow-trade mode:

* ``manage_open_state(cfg)`` runs every scan before any new entry. It enforces
  the account-equity circuit breaker (state persisted in ``.state/``), and makes
  a best-effort pass at limit expiry, intraday time-stops, and auto-breakeven.
  Anything it cannot confidently parse from live exchange state becomes a safe
  no-op rather than a wrong action on real money.
* ``open_if_allowed(decision, cfg, mgmt)`` is the ``execute_trade`` callback. It
  applies the concurrent-position cap and correlation budget, then places a
  side-aware limit entry with a tick-aligned stop and first target.

Reliable controls (documented helpers / account equity): circuit breaker,
position cap, correlation budget, duplicate-entry guard. Best-effort controls
(depend on undocumented position/order fields, fail-safe to no-op): time-stop,
auto-breakeven, limit expiry.
"""
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Optional

from getagent import trade

_STATE_DIR = Path("/workspace/.state")
_STATE_FILE = _STATE_DIR / "runeclaw_scanner.json"

_OPEN_TIME_KEYS = ("cTime", "ctime", "openTime", "open_time", "createTime", "uTime")
_ENTRY_PRICE_KEYS = ("openPriceAvg", "averageOpenPrice", "average_open_price",
                     "avgPrice", "openAvgPrice", "entryPrice", "open_price")
_UPNL_KEYS = ("unrealizedPL", "unrealized_pnl", "unrealizedPnl", "upl", "uplValue")
_SIZE_KEYS = ("total", "size", "holdSize", "available", "openDelegateSize")
_HOLD_SIDE_KEYS = ("holdSide", "hold_side", "side")
_EQUITY_KEYS = ("usdtEquity", "accountEquity", "totalEquity", "equity",
                "usdt_equity", "totalAmount", "accountValue", "unifiedTotalEquity")


def _to_mapping(value: Any) -> Optional[dict]:
    if isinstance(value, dict):
        return value
    for attr in ("to_dict", "dict", "model_dump"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return out
            except Exception:
                continue
    return None


def _find_number(value: Any, keys: tuple, depth: int = 0) -> Optional[float]:
    if depth > 4:
        return None
    mapping = _to_mapping(value)
    if mapping is not None:
        for key in keys:
            if key in mapping:
                try:
                    out = float(mapping[key])
                    return out
                except (TypeError, ValueError):
                    pass
        for nested_key in ("data", "result", "list", "assets", "account"):
            if nested_key in mapping:
                found = _find_number(mapping[nested_key], keys, depth + 1)
                if found is not None:
                    return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _find_number(item, keys, depth + 1)
            if found is not None:
                return found
    return None


def _find_string(record: dict, keys: tuple) -> str:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return str(record[key])
    return ""


def _result_reason(result: Any) -> str:
    """Compact exchange rejection reason from a non-success trade envelope.

    Bitget envelopes carry the original ``{"code", "msg"}`` on ``result.raw``;
    fall back to the envelope itself, then to ``str(result)``. This is what turns
    a silent ``placed=False`` into an operator-readable ``code:msg`` cause.
    """
    raw = getattr(result, "raw", None)
    mapping = _to_mapping(raw) or _to_mapping(result) or {}
    code = mapping.get("code") or mapping.get("retCode") or mapping.get("sCode")
    msg = (mapping.get("msg") or mapping.get("message") or mapping.get("retMsg")
           or mapping.get("sMsg"))
    if code not in (None, "") or msg not in (None, ""):
        return "{}:{}".format(code if code not in (None, "") else "?", msg or "")[:48]
    text = str(result).replace(" ", "_")
    return text[:48] if text else "unknown"


def _read_state() -> dict:
    try:
        if _STATE_FILE.exists():
            import json

            return json.loads(_STATE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _write_state(state: dict) -> None:
    try:
        import json

        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _account_equity() -> Optional[float]:
    try:
        result = trade.account.total_value()
    except Exception:
        return None
    return _find_number(result, _EQUITY_KEYS)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _read_owned() -> set:
    state = _read_state()
    raw = state.get("owned_symbols") or []
    return {str(s).upper() for s in raw if s}


def _write_owned(owned: set) -> None:
    state = _read_state()
    state["owned_symbols"] = sorted(owned)
    _write_state(state)


def _pending_symbols() -> set:
    """Symbols with a live RUNECLAW resting limit (or any pending order)."""
    try:
        pending = trade.contract.pending_orders()
    except Exception:
        return set()
    mapping = _to_mapping(pending) or {}
    rows = mapping.get("data") or mapping.get("list") or []
    if isinstance(rows, dict):
        rows = rows.get("list") or rows.get("entrustedList") or []
    out = set()
    if isinstance(rows, list):
        for row in rows:
            rec = _to_mapping(row)
            if rec:
                sym = _find_string(rec, ("symbol",))
                if sym:
                    out.add(sym.upper())
    return out


def manage_open_state(cfg: dict) -> dict:
    actions: list = []
    status = {
        "circuit": "ok",
        "today_pnl": None,
        "open_count": 0,
        "open_symbols": [],
        "owned_symbols": [],
        "all_account_symbols": [],
        "controls_active": {"circuit_breaker": False, "time_stop": False, "auto_be": False},
        "actions": actions,
    }

    soft = float(cfg.get("circuit_pause_usdt", "30"))
    hard = float(cfg.get("circuit_stop_usdt", "40"))

    # --- circuit breaker via account equity persisted in .state/ ---
    equity = _account_equity()
    state = _read_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if equity is not None:
        if state.get("date") != today or "day_start_equity" not in state:
            state["date"] = today
            state["day_start_equity"] = equity
        today_pnl = equity - float(state.get("day_start_equity", equity))
        status["today_pnl"] = round(today_pnl, 4)
        status["controls_active"]["circuit_breaker"] = True
        if today_pnl <= -abs(hard):
            status["circuit"] = "tripped"
        elif today_pnl <= -abs(soft):
            status["circuit"] = "paused"
        state["last_equity"] = equity
        _write_state(state)

    # --- live position snapshot (all account positions) ---
    try:
        positions = trade.contract.current_position()
        all_symbols = {s.upper() for s in (trade.helpers.contract_open_symbols(positions) or [])}
        records = trade.helpers.contract_position_records(positions) or []
    except Exception as exc:
        status["position_query_error"] = type(exc).__name__
        all_symbols, records = set(), []

    # --- ownership scope: only manage what RUNECLAW opened ---
    # Reconcile owned set: drop symbols that have neither a live position nor a
    # pending entry of ours; everything else stays.
    pending = _pending_symbols()
    owned = _read_owned()
    owned = {s for s in owned if s in all_symbols or s in pending}
    _write_owned(owned)

    owned_open = owned & all_symbols
    status["all_account_symbols"] = sorted(all_symbols)
    status["owned_symbols"] = sorted(owned)
    # Count resting limits AND filled positions toward the concurrency/correlation
    # caps. `owned` is reconciled above to RUNECLAW symbols that have either a live
    # position or a pending entry -- i.e. total open commitments -- so "max 3"
    # means 3 resting+filled together, not 3 fills on top of unbounded rests. (v0.1.12)
    status["open_symbols"] = sorted(owned)
    status["open_count"] = len(owned)
    status["filled_symbols"] = sorted(owned_open)

    owned_records = [r for r in records if _find_string(r, ("symbol",)).upper() in owned]

    if status["circuit"] == "tripped":
        _flatten_owned(cfg, owned_records, owned, actions)
        return status

    _best_effort_position_controls(cfg, owned_records, status, actions)
    _best_effort_limit_expiry(cfg, owned, actions)
    return status


def _flatten_owned(cfg: dict, owned_records: list, owned: set, actions: list) -> None:
    """Circuit hard-stop: cancel ONLY RUNECLAW's resting orders + close ONLY its
    positions. Never touches account positions opened outside the playbook."""
    try:
        pending = trade.contract.pending_orders()
    except Exception:
        pending = None
    mapping = _to_mapping(pending) or {}
    rows = mapping.get("data") or mapping.get("list") or []
    if isinstance(rows, dict):
        rows = rows.get("list") or rows.get("entrustedList") or []
    if isinstance(rows, list):
        for row in rows:
            rec = _to_mapping(row) or {}
            symbol = _find_string(rec, ("symbol",))
            order_id = _find_string(rec, ("orderId", "order_id", "clientOid"))
            if symbol and order_id and symbol.upper() in owned:
                try:
                    trade.contract.cancel_order(symbol=symbol, order_id=order_id)
                    actions.append({"circuit_cancel": symbol})
                except Exception:
                    pass

    for record in owned_records:
        symbol = _find_string(record, ("symbol",))
        hold_side = _find_string(record, _HOLD_SIDE_KEYS)
        if symbol and hold_side:
            try:
                trade.contract.close_position(symbol=symbol, hold_side=hold_side)
                actions.append({"circuit_close": symbol})
            except Exception:
                pass


def _best_effort_position_controls(cfg: dict, records: list, status: dict, actions: list) -> None:
    max_age_h = float(cfg.get("time_stop_hours", "4"))
    be_trigger_usdt = float(cfg.get("breakeven_trigger_usdt", "20"))
    be_trigger_pct = float(cfg.get("breakeven_pct", "2.0")) / 100.0
    now_ms = _now_ms()

    for record in records:
        symbol = _find_string(record, ("symbol",))
        hold_side = _find_string(record, _HOLD_SIDE_KEYS)
        if not symbol or not hold_side:
            continue

        # Intraday time-stop (best effort: requires a parseable open time).
        open_ms = _find_number(record, _OPEN_TIME_KEYS)
        if open_ms is not None and open_ms > 0:
            age_h = (now_ms - open_ms) / 3_600_000.0
            if 0 < age_h <= 240 and age_h >= max_age_h:
                try:
                    trade.contract.close_position(symbol=symbol, hold_side=hold_side)
                    actions.append({"time_stop_close": symbol, "age_h": round(age_h, 2)})
                    status["controls_active"]["time_stop"] = True
                    continue
                except Exception:
                    pass

        # Auto-breakeven (best effort: requires entry price + current price).
        entry = _find_number(record, _ENTRY_PRICE_KEYS)
        upnl = _find_number(record, _UPNL_KEYS)
        if entry is None or entry <= 0:
            continue
        try:
            current = float(trade.helpers.contract_price(symbol))
        except Exception:
            continue
        is_long = hold_side.lower() in ("long", "buy")
        move_pct = (current - entry) / entry if is_long else (entry - current) / entry
        if move_pct >= be_trigger_pct or (upnl is not None and upnl >= be_trigger_usdt):
            _move_stop_to_breakeven(symbol, entry, actions, status)


def _move_stop_to_breakeven(symbol: str, entry: float, actions: list, status: dict) -> None:
    try:
        rules = trade.helpers.contract_rules(symbol)
        step = getattr(rules, "price_step", None)
        plan = trade.contract.plan_pending_orders(symbol=symbol)
        sl = trade.helpers.select_sl_plan_order(plan, symbol=symbol)
        order_id = getattr(sl, "order_id", "")
        if not order_id:
            return
        trade.contract.modify_stop_loss(symbol=symbol, order_id=order_id, trigger_price=_align(entry, step))
        actions.append({"auto_be": symbol})
        status["controls_active"]["auto_be"] = True
    except Exception:
        pass


def _best_effort_limit_expiry(cfg: dict, owned: set, actions: list) -> None:
    """Cancel ONLY RUNECLAW's stale resting limits. Never cancels non-RUNECLAW
    orders the user placed by hand."""
    max_age_h = float(cfg.get("limit_expiry_hours", "4"))
    now_ms = _now_ms()
    try:
        pending = trade.contract.pending_orders()
    except Exception:
        return
    mapping = _to_mapping(pending) or {}
    rows = mapping.get("data") or mapping.get("list") or []
    if isinstance(rows, dict):
        rows = rows.get("list") or rows.get("entrustedList") or []
    if not isinstance(rows, list):
        return
    for row in rows:
        record = _to_mapping(row)
        if not record:
            continue
        symbol = _find_string(record, ("symbol",))
        if symbol.upper() not in owned:
            continue
        order_id = _find_string(record, ("orderId", "order_id", "clientOid"))
        created = _find_number(record, _OPEN_TIME_KEYS)
        if symbol and order_id and created and created > 0:
            age_h = (now_ms - created) / 3_600_000.0
            if max_age_h <= age_h <= 240:
                try:
                    trade.contract.cancel_order(symbol=symbol, order_id=order_id)
                    actions.append({"limit_expiry_cancel": symbol, "age_h": round(age_h, 2)})
                except Exception:
                    pass


def _exc_brief(exc: Exception) -> str:
    """Compact exception *message* (not just the class name) so a real SDK or
    exchange validation cause surfaces in the diagnostic instead of a bare type."""
    msg = str(exc).strip().replace("\n", " ").replace(",", ";")
    return (msg or type(exc).__name__)[:80]


def open_if_allowed(decision: dict, cfg: dict, mgmt: dict) -> dict:
    plan = decision.get("plan") or {}
    symbol = str(decision.get("symbol", ""))
    side = str(plan.get("side", "long"))
    if not symbol or not plan:
        return {"placed": False, "reason": "incomplete_plan"}

    if mgmt.get("circuit") in ("paused", "tripped"):
        return {"placed": False, "reason": "circuit_" + str(mgmt.get("circuit"))}

    open_count = int(mgmt.get("open_count", 0) or 0)
    open_symbols = [str(s).upper() for s in (mgmt.get("open_symbols") or [])]
    max_concurrent = int(cfg.get("max_concurrent", 3))
    if open_count >= max_concurrent:
        return {"placed": False, "reason": "max_concurrent_reached", "open_count": open_count}

    # Rule 7 correlation budget: treat every open alt as BTC-correlated; tighten
    # to a single fresh slot whenever BTC or ETH is already held.
    max_corr = int(cfg.get("max_correlated_alts", 2))
    if any(s in ("BTCUSDT", "ETHUSDT") for s in open_symbols):
        max_corr = min(max_corr, 1)
    if symbol.upper() not in open_symbols and len(open_symbols) >= max_corr:
        return {"placed": False, "reason": "correlation_budget", "open_symbols": open_symbols}

    leverage = max(int(cfg.get("leverage", 10)), 1)
    entry = plan.get("entry")
    sl_price = plan.get("sl_price")
    tp1 = plan.get("tp1")
    margin = plan.get("margin_usdt")
    if entry is None or sl_price is None or tp1 is None or not margin:
        return {"placed": False, "reason": "incomplete_plan"}

    # Duplicate guard: skip if already in a position or already resting an entry.
    # Best-effort ONLY -- a parse/type error here must never block an entry. The
    # v0.1.9 diagnostic proved find_contract_position raises TypeError on flat
    # hedge-mode slots (size returned as the string "0"), and the old
    # except-branch converted that into a hard skip that blocked 100% of orders.
    # count_open_contract_positions normalizes those shapes; on any error we
    # proceed and rely on max_concurrent + the exchange as backstops.
    try:
        pos_result = trade.contract.current_position(symbol=symbol)
        in_position = trade.helpers.count_open_contract_positions(pos_result, symbol=symbol) > 0
    except Exception:
        in_position = False
    if in_position:
        return {"placed": False, "reason": "already_in_position"}
    try:
        existing = trade.helpers.select_contract_order(trade.contract.pending_orders(symbol=symbol), symbol=symbol)
    except Exception:
        existing = None
    if existing is not None and getattr(existing, "order_id", ""):
        return {"placed": False, "reason": "entry_already_pending"}

    try:
        step = getattr(trade.helpers.contract_rules(symbol), "price_step", None)
    except Exception:
        step = None
    entry_price = _align(entry, step)
    tp1_price = _align(tp1, step)
    sl_price_aligned = _align(sl_price, step)

    try:
        qty_plan = trade.helpers.compute_qty(
            symbol=symbol, market="contract", budget_amount=str(margin),
            leverage=leverage, price=str(entry_price),
        )
    except Exception as exc:
        return {"placed": False, "reason": "compute_qty_error:" + _exc_brief(exc)}

    # Pass tick-aligned TP/SL trigger prices, and surface the real validation
    # text (not just the exception class) so any reject reason is actionable.
    try:
        tpsl = trade.helpers.resolve_contract_tpsl(
            symbol=symbol, side=side, leverage=leverage,
            tp_trigger_price=tp1_price, sl_trigger_price=sl_price_aligned,
            reference_price=str(entry_price),
        )
    except Exception as exc:
        return {"placed": False, "reason": "tpsl_error:" + _exc_brief(exc)}

    opener = trade.contract.open_short_limit if side == "short" else trade.contract.open_long_limit
    try:
        result = opener(
            symbol=symbol, qty=qty_plan.qty, price=entry_price, leverage=leverage,
            tp_trigger_price=tpsl.tp_trigger_price, sl_trigger_price=tpsl.sl_trigger_price,
        )
    except Exception as exc:
        return {"placed": False, "reason": "open_raise:" + _exc_brief(exc),
                "symbol": symbol, "side": side,
                "qty": str(getattr(qty_plan, "qty", "")), "entry": str(entry_price)}

    placed = bool(trade.is_success(result))
    out = {
        "placed": placed, "symbol": symbol, "side": side,
        "qty": str(getattr(qty_plan, "qty", "")), "entry": str(entry_price),
        "tp1": str(tpsl.tp_trigger_price), "sl": str(tpsl.sl_trigger_price),
    }
    if placed:
        # Tag the symbol as RUNECLAW-owned so management controls (time-stop,
        # auto-BE, limit-expiry, circuit-flatten) only ever touch this position.
        owned = _read_owned()
        owned.add(symbol.upper())
        _write_owned(owned)
    else:
        # Surface the exchange's own rejection so a fully-sized, fully-guarded
        # order that never rests on the book stops being a silent no-op.
        out["reason"] = "exchange_reject:" + _result_reason(result)
    return out


def _align(price: Any, step: Any) -> str:
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
        return str((quoted / increment).to_integral_value(rounding=ROUND_DOWN) * increment)
    except Exception:
        return str(quoted)
