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

from . import features  # v0.6.3: recompute ATR at manage-time for the trailing stop

_STATE_DIR = Path("/workspace/.state")
_STATE_FILE = _STATE_DIR / "runeclaw_scanner.json"

# SDK serialises order/position records to snake_case (to_dict/model_dump), so the
# raw Bitget camelCase cTime/createTime never matched here -- a real ETH limit sat
# ~8h past its 4h limit_expiry because create_time was absent, so age was unknown
# and the time-expiry silently no-op'd. The position time-stop reads the same list,
# so it was latently broken too. Carry both cases, like every other key list. (v0.1.18)
_OPEN_TIME_KEYS = ("cTime", "ctime", "c_time", "create_time", "created_time", "createTime",
                   "createdTime", "openTime", "open_time", "uTime", "u_time", "update_time",
                   "updateTime")
# v0.6.7: the live position record serialises to snake_case (open_price_avg), but
# this list (and _record_notional) only carried the camelCase openPriceAvg + the
# unrelated average_open_price/open_price -- so the entry price read None on every
# position. That made _record_notional return None -> _runeclaw_sized False ->
# EVERY position was excluded from ownership -> _best_effort_position_controls never
# ran -> the trail/time-stop never fired all session (the "frozen SL" was the trail
# never executing, not modify_err). Carry the snake_case open_price_avg too.
_ENTRY_PRICE_KEYS = ("openPriceAvg", "open_price_avg", "averageOpenPrice",
                     "average_open_price", "avgPrice", "avg_price", "openAvgPrice",
                     "open_avg_price", "entryPrice", "entry_price", "open_price")
_UPNL_KEYS = ("unrealizedPL", "unrealized_pnl", "unrealizedPnl", "upl", "uplValue")
_SIZE_KEYS = ("total", "size", "holdSize", "available", "openDelegateSize")
_HOLD_SIDE_KEYS = ("holdSide", "hold_side", "side")
_TRIGGER_KEYS = ("triggerPrice", "trigger_price", "slTriggerPrice", "sl_trigger_price",
                 "presetStopLossPrice", "price", "executePrice")
_EQUITY_KEYS = ("usdtEquity", "accountEquity", "totalEquity", "equity",
                "usdt_equity", "totalAmount", "accountValue", "unifiedTotalEquity")
# v0.6.6: per-asset POSITION margin (non-zero iff a position is open, regardless of
# PnL direction -- unlike unrealizedPL which is ~0 at breakeven, and unlike `locked`
# which a resting limit also consumes). Used to detect the read-lies-empty blind-spot.
_POSITION_MARGIN_KEYS = ("crossedMargin", "isolatedMargin")


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


def _time_key_probe(record: Any) -> str:
    """Diagnose why an order's open-time may be unreadable. Returns the first known
    time key present (``has:<key>``), else the first time-ish key the SDK exposed
    (``alt:<key>`` -- a renamed field we can simply add to ``_OPEN_TIME_KEYS``),
    else ``none``. This is what turns a silent limit-expiry no-op into a one-line
    fix: on Classic the 4H expiry never fired because the SDK pending-order record
    shape was never confirmed to carry a key we look for. (v0.4.2)"""
    mapping = _to_mapping(record) or {}
    for k in _OPEN_TIME_KEYS:
        if k in mapping and mapping[k] not in (None, ""):
            return "has:" + k
    for k in mapping.keys():
        ks = str(k).lower()
        if "time" in ks or ks == "ts" or ks.endswith("ts"):
            return "alt:" + str(k)[:14]
    return "none"


def _find_open_time_value(record: Any, depth: int = 0) -> Any:
    """First present, non-empty open-time value, returned RAW (uncoerced),
    recursing into the usual envelope wrappers. Mirrors ``_find_number``'s
    traversal but keeps the original value so a non-epoch shape (e.g. an ISO-8601
    string) survives to ``_to_epoch_ms`` instead of being dropped by a premature
    ``float()``. (v0.4.3)"""
    if depth > 4:
        return None
    mapping = _to_mapping(record)
    if mapping is not None:
        for k in _OPEN_TIME_KEYS:
            if k in mapping and mapping[k] not in (None, ""):
                return mapping[k]
        for nk in ("data", "result", "list", "assets", "account"):
            if nk in mapping:
                v = _find_open_time_value(mapping[nk], depth + 1)
                if v is not None:
                    return v
    elif isinstance(record, (list, tuple)):
        for item in record:
            v = _find_open_time_value(item, depth + 1)
            if v is not None:
                return v
    return None


def _to_epoch_ms(value: Any) -> Optional[float]:
    """Coerce an order open-time to epoch milliseconds. Accepts epoch ms, epoch
    seconds, a numeric string, an ISO-8601 string (optionally ``Z``-suffixed), or a
    ``datetime``. Returns None when nothing usable.

    v0.4.3 root cause: live on GetClaw Classic the pending-order ``create_time``
    came back as a non-epoch value, so the old ``float()``-only lookup returned
    None, the 4h expiry never computed an age, and the order rested indefinitely
    (the v0.4.2 ``xpd.no_ts:has:create_time`` contradiction: key present, value
    unparseable)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000.0
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = None
    if n is not None:
        if n <= 0:
            return None
        # < 1e11 is implausible as epoch-ms (year 1973) but normal as epoch-s,
        # so treat it as seconds and scale up; otherwise it is already ms.
        return n * 1000.0 if n < 1e11 else n
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000.0
    return None


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


def _account_position_margin() -> Optional[float]:
    """v0.6.6: total open-position margin (crossed + isolated) summed across the
    account's contract assets. Non-zero iff a position is open -- the reliable
    cross-check for the read-lies-empty blind-spot (current_position() returns an
    empty success while a position is actually live). Returns None if the account
    snapshot can't be read or carries no margin field, so an unreadable snapshot
    NEVER triggers state_blind (no false-positive on a genuinely flat account)."""
    try:
        result = trade.account.total_value()
    except Exception:
        return None
    mapping = _to_mapping(result)
    if mapping is None:
        return None
    # contract_assets lives under data on the live shape; tolerate a flattened shape.
    data_map = _to_mapping(mapping.get("data")) if mapping.get("data") is not None else None
    assets = (data_map or mapping).get("contract_assets")
    if not isinstance(assets, list):
        return None
    total = 0.0
    found = False
    for asset in assets:
        amap = _to_mapping(asset) or {}
        for key in _POSITION_MARGIN_KEYS:
            if key in amap:
                try:
                    total += abs(float(amap[key]))
                    found = True
                except (TypeError, ValueError):
                    pass
    return total if found else None


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _extract_rows(value: Any, depth: int = 0) -> list:
    """Recursively locate a list of record dicts (each carrying a 'symbol') inside
    a varied SDK result envelope. The unfiltered pending_orders() result nests its
    rows in a shape the old flat .get('data'/'list') parse missed -> the live DBG
    showed manage_open_state seeing zero pending orders that actually existed (pT0).
    This finds the row list wherever it is."""
    if depth > 6:
        return []
    if isinstance(value, (list, tuple)):
        recs = [m for m in (_to_mapping(x) for x in value) if m]
        if recs and all("symbol" in r for r in recs):
            return recs
        out = []
        for item in value:
            out.extend(_extract_rows(item, depth + 1))
        return out
    mapping = _to_mapping(value)
    if not mapping:
        return []
    for key in ("data", "list", "orders", "rows", "records", "result", "items",
                "entrustedList", "entrusted_list", "orderList", "raw"):
        if key in mapping:
            found = _extract_rows(mapping[key], depth + 1)
            if found:
                return found
    for nested in mapping.values():
        if isinstance(nested, (list, dict)):
            found = _extract_rows(nested, depth + 1)
            if found:
                return found
    return []


def _record_notional(record: dict) -> Optional[float]:
    """USDT notional (qty * price) of a live order or position record."""
    qty = _find_number(record, _SIZE_KEYS + ("qty", "baseVolume", "base_volume"))
    # v0.6.7: order-price keys first (limit orders), then entry-price (positions, now
    # incl. snake_case via _ENTRY_PRICE_KEYS), then mark as a last-resort proxy. The
    # old list lacked the snake position price, so positions sized to None -> excluded.
    price = _find_number(record, ("price", "orderPrice", "order_price", "limitPrice",
                                  "limit_price", "executePrice", "execute_price")
                                 + _ENTRY_PRICE_KEYS + ("markPrice", "mark_price"))
    if qty is None or price is None or qty <= 0 or price <= 0:
        return None
    return qty * price


def _runeclaw_sized(record: dict, cfg: dict) -> bool:
    """Stateless ownership: recognise RUNECLAW's own orders/positions by size.

    The runtime does not persist ``.state/`` between scheduled runs, so we cannot
    remember which orders we placed. RUNECLAW risk-sizes every order to at most
    ``margin_budget * leverage``; the user's manual trades have been ~10x larger.
    We therefore manage only records whose notional is within our own envelope
    (cap * size_scope_mult), which can never reach the user's bigger manual trades.
    """
    notional = _record_notional(record)
    if notional is None or notional <= 0:
        return False
    leverage = max(int(cfg.get("leverage", 10)), 1)
    budget = float(cfg.get("margin_budget", "100") or "100")
    mult = float(cfg.get("size_scope_mult", "1.5"))
    return notional <= budget * leverage * mult


def _shape(value: Any, depth: int = 0) -> str:
    """Compact structural description of a result envelope, recursing one level
    into 'data', so a parse miss (rows present) vs an empty payload is visible."""
    if depth > 3:
        return "."
    mapping = _to_mapping(value)
    if mapping is not None:
        ks = ";".join(str(k) for k in list(mapping.keys())[:4])
        if "data" in mapping and depth < 2:
            return ks + ">(" + _shape(mapping["data"], depth + 1) + ")"
        return ks
    if isinstance(value, (list, tuple)):
        if not value:
            return "L0"
        first = _to_mapping(value[0])
        inner = (";".join(str(k) for k in list(first.keys())[:4]) if first
                 else type(value[0]).__name__)
        return "L{}:{}".format(len(value), inner)
    return type(value).__name__


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

    # --- .state/ persistence probe (DEEP_AUDIT #1) ---------------------------
    # The circuit breaker assumes .state/ carries day_start_equity across runs;
    # the ownership layer assumes .state/ does NOT persist. The runtime docs say
    # .state/ IS the only supported persisted path -- but the runner only
    # "may optionally" hydrate it. Carry a monotonic run counter so the live DBG
    # shows whether state actually round-trips: state_runs climbing across cycles
    # == persists (circuit breaker valid, ownership-by-size is an unjustified
    # workaround); stuck at 1 == ephemeral (circuit breaker is non-functional).
    state = _read_state()
    state["runs"] = int(state.get("runs", 0) or 0) + 1
    status["state_runs"] = state["runs"]

    # --- circuit breaker via account equity persisted in .state/ ---
    equity = _account_equity()
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
    # Persist regardless of equity availability so the run counter survives even
    # when the account-equity parse fails this cycle.
    _write_state(state)

    # --- live snapshot: positions + pending orders (the only source of truth;
    # .state/ does not persist between scheduled runs) ---
    try:
        positions = trade.contract.current_position()
        # v0.6.5: a non-raising error envelope (live shape {code,message,data,...})
        # must NOT read as "flat" -- that blinds the open-gate the same way the
        # pending path was blinded (see below). Probe success; a failed position
        # query sets state_blind so open_if_allowed refuses new entries.
        try:
            positions_ok = bool(trade.is_success(positions))
        except Exception:
            positions_ok = True
        if not positions_ok:
            status["position_query_reason"] = _result_reason(positions)
            status["state_blind"] = True
        records = trade.helpers.contract_position_records(positions) or []
    except Exception as exc:
        status["position_query_error"] = type(exc).__name__
        status["state_blind"] = True
        records = []
    # v0.6.6: blind-spot detector. current_position() can return an empty SUCCESS while
    # a position is actually open (a flaky trade-bridge "read lie" -- the 12:33 incident:
    # ETH live, read own0, playbook over-opened on top). The v0.6.5 interlock only catches
    # read ERRORS, not a successful-but-empty read. Cross-check the account: if positions
    # read empty but margin is still locked against open positions, treat it as blind so
    # open_if_allowed refuses to ADD. Fail-open -- unreadable margin -> no block, so a
    # genuinely flat account (margin 0) is never falsely blinded.
    if not records and not status.get("state_blind"):
        pos_margin = _account_position_margin()
        if pos_margin is not None and pos_margin > 0:
            status["state_blind"] = True
            status["blind_reason"] = ("pos_margin_%.4f_vs_empty" % pos_margin)[:32]
    try:
        pending_raw = trade.contract.pending_orders()
    except Exception as exc:
        pending_raw = None
        status["pending_error"] = type(exc).__name__
    # A non-raising error envelope (live shape: {code, message, data, trace_id})
    # must NOT be read as "no pending orders" -- that silently blinds limit-expiry
    # and the circuit cancel loop (the live pT0). Probe success and surface the
    # exchange code:msg, so a failed query is distinguishable from an empty book
    # and the next cycle's DBG says whether pT0 is an error or a parse miss.
    if pending_raw is not None:
        try:
            pending_ok = bool(trade.is_success(pending_raw))
        except Exception:
            pending_ok = True
        if not pending_ok:
            status["pending_reason"] = _result_reason(pending_raw)
    pending_records = _extract_rows(pending_raw) if pending_raw is not None else []
    status["pending_shape"] = _shape(pending_raw)[:40] if pending_raw is not None else "none"

    # --- STATELESS ownership: scope to RUNECLAW-sized live orders/positions ---
    owned_position_records = [r for r in records if _runeclaw_sized(r, cfg)]
    owned_pending_records = [r for r in pending_records if _runeclaw_sized(r, cfg)]
    pos_symbols = {_find_string(r, ("symbol",)).upper()
                   for r in owned_position_records if _find_string(r, ("symbol",))}
    pend_symbols = {_find_string(r, ("symbol",)).upper()
                    for r in owned_pending_records if _find_string(r, ("symbol",))}
    owned = pos_symbols | pend_symbols

    status["all_account_symbols"] = sorted({_find_string(r, ("symbol",)).upper()
                                            for r in records if _find_string(r, ("symbol",))})
    status["owned_symbols"] = sorted(owned)
    # Resting limits AND filled positions both count toward the concurrency cap,
    # so "max N" means N total commitments (resting + filled). (v0.1.12/0.1.14)
    status["open_symbols"] = sorted(owned)
    status["open_count"] = len(owned)
    status["filled_symbols"] = sorted(pos_symbols)
    # Diagnostic counts: total live pending vs the subset we recognise as ours.
    status["pending_total"] = len(pending_records)
    status["owned_pending"] = len(owned_pending_records)
    status["ran"] = True

    if status["circuit"] == "tripped":
        _flatten_owned(cfg, owned_position_records, owned_pending_records, actions)
        return status

    _best_effort_position_controls(cfg, owned_position_records, status, actions)
    _best_effort_limit_expiry(cfg, owned_pending_records, actions, status)
    return status


def _flatten_owned(cfg: dict, owned_position_records: list, owned_pending_records: list,
                   actions: list) -> None:
    """Circuit hard-stop: cancel ONLY RUNECLAW-sized resting orders + close ONLY
    RUNECLAW-sized positions. Never touches the user's larger manual trades."""
    for rec in owned_pending_records:
        symbol = _find_string(rec, ("symbol",))
        order_id = _find_string(rec, ("orderId", "order_id", "clientOid"))
        if symbol and order_id:
            try:
                trade.contract.cancel_order(symbol=symbol, order_id=order_id)
                actions.append({"circuit_cancel": symbol})
            except Exception:
                pass

    for record in owned_position_records:
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

    # v0.6.1 exit-path observability: record each managed position's state every
    # cycle (age, uPnL, move%, whether breakeven is armed, and whether the open-time
    # even parsed) so the never-yet-observed exit machinery is visible when a fill
    # finally runs -- the position analogue of the xpd limit-expiry diagnostic.
    diags = []
    for record in records:
        symbol = _find_string(record, ("symbol",))
        hold_side = _find_string(record, _HOLD_SIDE_KEYS)
        if not symbol or not hold_side:
            continue

        diag = {"sym": symbol, "side": hold_side.lower()}

        # Intraday time-stop (best effort: requires a parseable open time).
        # v0.6.1: coerce any open-time shape (ISO string / epoch s / ms / datetime)
        # to epoch ms -- same fix as the limit-expiry path (v0.4.3). The old
        # float()-only lookup returned None for the live ISO create_time, so the
        # position time-stop silently never fired.
        open_ms = _to_epoch_ms(_find_open_time_value(record))
        age_h = (now_ms - open_ms) / 3_600_000.0 if (open_ms and open_ms > 0) else None
        diag["age_h"] = round(age_h, 2) if age_h is not None else None
        diag["ts_ok"] = age_h is not None  # False => open-time unreadable (time-stop blind)
        if age_h is not None and 0 < age_h <= 240 and age_h >= max_age_h:
            try:
                trade.contract.close_position(symbol=symbol, hold_side=hold_side)
                actions.append({"time_stop_close": symbol, "age_h": round(age_h, 2)})
                status["controls_active"]["time_stop"] = True
                diag["acted"] = "time_stop_close"
                diags.append(diag)
                continue
            except Exception as exc:
                diag["ts_err"] = _exc_brief(exc)[:24]

        # Auto-breakeven (best effort: requires entry price + current price).
        entry = _find_number(record, _ENTRY_PRICE_KEYS)
        upnl = _find_number(record, _UPNL_KEYS)
        diag["upnl"] = round(upnl, 4) if upnl is not None else None
        if entry is None or entry <= 0:
            diag["note"] = "no_entry_price"
            diags.append(diag)
            continue
        try:
            current = float(trade.helpers.contract_price(symbol))
        except Exception:
            diag["note"] = "no_current_price"
            diags.append(diag)
            continue
        is_long = hold_side.lower() in ("long", "buy")
        move_pct = (current - entry) / entry if is_long else (entry - current) / entry
        diag["move_pct"] = round(move_pct * 100.0, 3)
        be_armed = (move_pct >= be_trigger_pct or (upnl is not None and upnl >= be_trigger_usdt))
        diag["be_armed"] = be_armed
        # v0.6.3: ratchet a trailing stop (strictly additive, fail-safe no-op) --
        # it subsumes auto-breakeven (the trail crosses entry as price runs). With
        # trail_atr_mult <= 0 the trail is disabled and the old breakeven-only
        # behaviour applies.
        if float(cfg.get("trail_atr_mult", "1.0")) > 0:
            # v0.6.4: _trail_stop records WHY it acted / no-op'd into diag["trail"]
            # (no_atr / no_sl_order / no_sl_trigger / hold:<trail>v<cur> / tick /
            # modify_err:<msg> / set:<price>) so a silently-inert trail is visible
            # the way xpd surfaced the silent limit-expiry. See DESIGN_v0.6.4.md.
            if _trail_stop(symbol, hold_side, current, cfg, actions, status, diag):
                diag["acted"] = "trail_stop"
        elif be_armed:
            _move_stop_to_breakeven(symbol, entry, actions, status)
            diag["acted"] = "auto_be"
        diags.append(diag)

    status["position_diag"] = diags


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


def _trail_stop(symbol: str, hold_side: str, current: float, cfg: dict,
                actions: list, status: dict, diag: Optional[dict] = None) -> bool:
    """v0.6.3 trailing stop. Ratchets the position's SL in the protective direction
    only -- to ``current -/+ trail_atr_mult*ATR`` -- moving it ONLY if that is
    strictly more protective than the live SL. STRICTLY ADDITIVE and fail-safe:
    if the ATR, the current price, or the live SL trigger cannot be read, it does
    NOTHING (the existing fixed SL stays in force). It never cancels, widens, or
    removes a stop. Stateless -- the exchange SL order is the trail's memory.
    Returns True iff it moved the stop. (validated in research/replay_mp.py)

    v0.6.4: records a one-token reason into ``diag['trail']`` at every exit so a
    silently-inert trail is diagnosable (the position analogue of the xpd
    limit-expiry diag). A bare fail-safe no-op was indistinguishable from a working
    trail that simply had nothing to do -- this names which it was."""
    def _why(reason: str) -> bool:  # record the no-op reason, return False
        if diag is not None:
            diag["trail"] = reason
        return False

    tmult = float(cfg.get("trail_atr_mult", "1.0"))
    if tmult <= 0 or not current or current <= 0:
        return _why("off")
    # Recompute ATR live; no ATR -> no trail this cycle (fail-safe).
    try:
        period = int(cfg.get("atr_period", 14))
        bars = features.fetch_klines(symbol, interval=str(cfg.get("kline_interval", "1h")),
                                     limit=max(period + 5, 30))
        atr = features._wilder_atr(bars, period) if bars else None
    except Exception as exc:
        return _why("atr_err:" + _exc_brief(exc)[:20])
    if not atr or atr <= 0:
        return _why("no_atr")
    is_long = hold_side.lower() in ("long", "buy")
    trail = current - tmult * atr if is_long else current + tmult * atr
    if trail <= 0:
        return _why("no_atr")
    # Read the live SL plan order + its trigger. Unreadable -> NEVER blind-set.
    try:
        sl = trade.helpers.select_sl_plan_order(trade.contract.plan_pending_orders(symbol=symbol),
                                                symbol=symbol)
    except Exception as exc:
        return _why("sl_err:" + _exc_brief(exc)[:20])
    order_id = _find_string(_to_mapping(sl) or {}, ("orderId", "order_id", "clientOid")) \
        or str(getattr(sl, "order_id", "") or "")
    cur_sl = _find_number(sl, _TRIGGER_KEYS)
    if cur_sl is None:  # attribute fallback (mirror the order_id read above)
        for _a in ("triggerPrice", "stopLossTriggerPrice", "trigger_price"):
            try:
                cur_sl = float(getattr(sl, _a))
                break
            except (TypeError, ValueError, AttributeError):
                continue
    if not order_id:
        return _why("no_sl_order")
    if cur_sl is None or cur_sl <= 0:
        return _why("no_sl_trigger")
    # Move ONLY in the protective direction, and only on a meaningful (>0.1%) tick
    # to avoid hammering modify_stop_loss every cycle.
    improves = (trail > cur_sl) if is_long else (trail < cur_sl)
    if not improves:
        return _why("hold:%.4f<=%.4f" % (trail, cur_sl))
    if abs(trail - cur_sl) < 0.001 * current:
        return _why("tick")
    try:
        step = getattr(trade.helpers.contract_rules(symbol), "price_step", None)
        trade.contract.modify_stop_loss(symbol=symbol, order_id=order_id,
                                        trigger_price=_align(trail, step))
        actions.append({"trail_stop": symbol, "to": round(trail, 6)})
        status["controls_active"]["trail"] = True
        if diag is not None:
            diag["trail"] = "set:%.4f" % trail
        return True
    except Exception as exc:
        return _why("modify_err:" + _exc_brief(exc)[:24])


def _best_effort_limit_expiry(cfg: dict, owned_pending_records: list, actions: list,
                              status: Optional[dict] = None) -> None:
    """Cancel ONLY RUNECLAW-sized stale resting limits -- either past the time
    budget (``limit_expiry_hours``) OR left behind when price ran more than
    ``limit_chase_pct`` past the entry in the direction the limit can never fill
    from (a short's sell-limit sits above market and dies if price collapses; a
    long's buy-limit sits below market and dies if price runs up). Operates only on
    the pre-scoped RUNECLAW-sized order records. (v0.1.13/0.1.14)"""
    max_age_h = float(cfg.get("limit_expiry_hours", "4"))
    chase_pct = float(cfg.get("limit_chase_pct", "3.0")) / 100.0
    now_ms = _now_ms()
    for record in owned_pending_records:
        symbol = _find_string(record, ("symbol",))
        order_id = _find_string(record, ("orderId", "order_id", "clientOid"))
        if not symbol or not order_id:
            continue

        # 1) Time-based expiry. v0.4.3: read the open-time value RAW, then coerce
        # any shape (epoch ms/s, numeric string, ISO-8601, datetime) to epoch ms.
        # The old float()-only lookup returned None for the live ISO create_time,
        # so age was never computed and the order rested past 4h untouched.
        raw_ct = _find_open_time_value(record)
        created = _to_epoch_ms(raw_ct)
        if created and created > 0:
            age_h = (now_ms - created) / 3_600_000.0
            if status is not None:
                # Track the oldest owned order's age so a stuck (un-expired) limit
                # is visible in the DBG even when it did not trip the cancel.
                if age_h > (status.get("pending_max_age_h") or 0.0):
                    status["pending_max_age_h"] = round(age_h, 2)
            if max_age_h <= age_h <= 240:
                try:
                    trade.contract.cancel_order(symbol=symbol, order_id=order_id)
                    actions.append({"limit_expiry_cancel": symbol, "age_h": round(age_h, 2)})
                    continue
                except Exception as exc:
                    # Timestamp parsed and the order IS over-age, but the cancel
                    # API rejected -- surface the cause instead of silently chasing.
                    if status is not None:
                        status["expiry_diag"] = ("cxl_err:" + _exc_brief(exc))[:30]
        elif status is not None:
            # Still no usable open-time. Distinguish "no key at all" (no_ts:<probe>)
            # from "key present but value uncoercible" (bad_ts:<type>:<value>) so any
            # residual shape names itself instead of hiding.
            if raw_ct in (None, ""):
                status["expiry_diag"] = ("no_ts:" + _time_key_probe(record))[:30]
            else:
                status["expiry_diag"] = (
                    "bad_ts:" + type(raw_ct).__name__ + ":" + str(raw_ct))[:30]

        # 2) Price-distance "left behind" cancel: the market has run past the
        # entry by more than limit_chase_pct in the un-fillable direction, so the
        # pullback this limit was waiting for is gone. Free the slot + margin; the
        # next scan re-places at the current VWAP level if the name still qualifies.
        if chase_pct <= 0:
            continue
        entry_price = _find_number(record, ("price", "orderPrice", "limitPrice", "executePrice"))
        if entry_price is None or entry_price <= 0:
            continue
        side = (_find_string(record, ("side",)) + " "
                + _find_string(record, ("posSide", "holdSide", "tradeSide"))).lower()
        try:
            current = float(trade.helpers.contract_price(symbol))
        except Exception:
            continue
        if not current or current <= 0:
            continue
        is_short = ("sell" in side) or ("short" in side)
        gap = (entry_price - current) / entry_price if is_short else (current - entry_price) / entry_price
        if gap > chase_pct:
            try:
                trade.contract.cancel_order(symbol=symbol, order_id=order_id)
                actions.append({"stale_limit_cancel": symbol, "gap_pct": round(gap * 100, 2)})
            except Exception:
                pass


def _exc_brief(exc: Exception) -> str:
    """Compact exception *message* (not just the class name) so a real SDK or
    exchange validation cause surfaces in the diagnostic instead of a bare type."""
    msg = str(exc).strip().replace("\n", " ").replace(",", ";")
    return (msg or type(exc).__name__)[:80]


def _open_isolated(side: str, entry_mode: str, symbol: str, qty: Any,
                   entry_price: Any, leverage: int, tpsl: Any) -> Any:
    """v0.6.4: open a position with ISOLATED margin via the lower-level
    ``place_order``. The composite ``open_*`` wrappers do not expose ``margin_mode``
    and always inherit ``place_order``'s ``crossed`` default, so the only route to
    isolated margin is to call ``place_order`` directly. This mirrors the wrapper
    (``change_leverage`` then place the order) but forces ``margin_mode='isolated'``.

    SAFETY -- this path is OPT-IN (``cfg['margin_mode']``, default ``crossed`` keeps
    the proven wrapper path unchanged) and FAIL-CLOSED. Trade direction is pinned by
    ``side`` -> buy/sell below, so a wrong hedge-field mapping makes the exchange
    reject the order (``placed: False``, one skipped entry) rather than ever opening
    a wrong-direction trade. ``pos_side``/``trade_side`` are set for HEDGE mode
    (this account's mode -- see the flat-slot note in ``open_if_allowed``); if the
    account is later confirmed one-way, empty them. Untested offline (the SDK is
    runner-managed and cannot be imported here) -- trial in ``signal_only`` / tiny
    size before trusting at normal size. See docs/DESIGN_v0.6.4.md."""
    is_short = (side == "short")
    order_side = "sell" if is_short else "buy"
    pos_side = "short" if is_short else "long"
    order_type = "market" if entry_mode == "breakout" else "limit"
    price = "" if entry_mode == "breakout" else entry_price
    try:
        trade.contract.change_leverage(symbol=symbol, leverage=leverage)
    except Exception:
        pass  # best-effort; place_order still opens at the symbol's set leverage
    return trade.contract.place_order(
        symbol=symbol, side=order_side, order_type=order_type, qty=qty,
        price=price, margin_mode="isolated", pos_side=pos_side, trade_side="open",
        tp_trigger_price=tpsl.tp_trigger_price, sl_trigger_price=tpsl.sl_trigger_price,
    )


def open_if_allowed(decision: dict, cfg: dict, mgmt: dict) -> dict:
    plan = decision.get("plan") or {}
    symbol = str(decision.get("symbol", ""))
    side = str(plan.get("side", "long"))
    if not symbol or not plan:
        return {"placed": False, "reason": "incomplete_plan"}

    # v0.6.5: never open on an unreadable book. If manage_open_state could not read
    # current positions this cycle (state_blind), open_count is unreliable -- a
    # failed read looks identical to a flat book -- so refuse new entries. This
    # prevents stacking untracked positions during a trade-bridge outage (the 06:32
    # `Failed_to_call` incident: the playbook read own0 while holding 2 live legs and
    # tried to place on top). Existing positions keep their exchange SL/TP; the
    # playbook simply does not ADD while blind, and resumes when the read recovers.
    if mgmt.get("state_blind"):
        return {"placed": False, "reason": "state_blind"}

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
    # v0.6.3: when the trailing stop is active, attach the WIDER tp2 as a backstop
    # so the ratcheting trail (not a tight tp1) governs the upside; trail off keeps
    # tp1 (pre-v0.6.3 behaviour).
    tp_attach = (plan.get("tp2") or tp1) if float(cfg.get("trail_atr_mult", "1.0")) > 0 else tp1

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
    tp1_price = _align(tp_attach, step)
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

    # v0.5.0: a breakout enters at MARKET the cycle it is confirmed (the SDK has no
    # native stop/trigger entry), so it never rests as a limit and is never subject
    # to the chase guard or limit-expiry. A pullback rests a limit at entry_price.
    entry_mode = str(plan.get("entry_mode", "pullback"))
    # v0.6.4: isolated-margin entry path (OPT-IN; default 'crossed' keeps the
    # proven open_* wrapper path byte-for-byte unchanged). The wrappers cannot set
    # margin_mode, so isolated routes through place_order via _open_isolated, which
    # fails closed on a wrong hedge mapping (never a wrong-direction trade). Trial
    # in signal_only / tiny size before normal size. See docs/DESIGN_v0.6.4.md.
    margin_mode = str(cfg.get("margin_mode", "crossed")).lower()
    try:
        if margin_mode == "isolated":
            result = _open_isolated(side, entry_mode, symbol, qty_plan.qty,
                                    entry_price, leverage, tpsl)
        elif entry_mode == "breakout":
            mopener = (trade.contract.open_short_market if side == "short"
                       else trade.contract.open_long_market)
            result = mopener(
                symbol=symbol, qty=qty_plan.qty, leverage=leverage,
                tp_trigger_price=tpsl.tp_trigger_price, sl_trigger_price=tpsl.sl_trigger_price,
            )
        else:
            opener = (trade.contract.open_short_limit if side == "short"
                      else trade.contract.open_long_limit)
            result = opener(
                symbol=symbol, qty=qty_plan.qty, price=entry_price, leverage=leverage,
                tp_trigger_price=tpsl.tp_trigger_price, sl_trigger_price=tpsl.sl_trigger_price,
            )
    except Exception as exc:
        return {"placed": False, "reason": "open_raise:" + _exc_brief(exc),
                "symbol": symbol, "side": side, "entry_mode": entry_mode,
                "qty": str(getattr(qty_plan, "qty", "")), "entry": str(entry_price)}

    placed = bool(trade.is_success(result))
    out = {
        "placed": placed, "symbol": symbol, "side": side, "entry_mode": entry_mode,
        "qty": str(getattr(qty_plan, "qty", "")), "entry": str(entry_price),
        "tp1": str(tpsl.tp_trigger_price), "sl": str(tpsl.sl_trigger_price),
        "margin_mode": margin_mode,  # v0.6.4: observable in metrics for the trial
    }
    if not placed:
        # Surface the exchange's own rejection so a fully-sized, fully-guarded
        # order that never rests on the book stops being a silent no-op.
        out["reason"] = "exchange_reject:" + _result_reason(result)
    # Ownership is derived live by size each cycle (stateless), so no tagging.
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
