"""Live scan + signal for the RUNECLAW Limit Entry Scanner (v0.1.0).

Each scheduled pass: resolve the BTC regime (long / short / neutral), ALWAYS
score the whole universe for visibility, emit one managed signal with a ranked
board + gate transparency, and — in follow-trade mode — run the position
manager (circuit breaker, time-stops, auto-breakeven) and place the best
qualifying setup subject to the concurrent + correlation caps.
"""
import math
from typing import Any

from getagent import runtime

from . import execution, features, risk, scoring

_GATE = "BTCUSDT"


def _cfg() -> dict:
    return runtime.manifest.get("strategy_config", {}) or {}


def _sanitize(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


def _board(scored: list, limit: int = 8) -> list:
    out = []
    for s in scored[:limit]:
        out.append({
            "symbol": s.symbol,
            "side": s.side,
            "score": round(s.score, 1),
            "skip": s.skip,
            "skip_reason": s.skip_reason,
            "dims": s.dims,
        })
    return out


def _gate_summary(reg: scoring.Regime) -> str:
    detail = reg.detail
    bits = []
    if detail.get("above_vwap"):
        bits.append("BTC>VWAP")
    elif detail.get("below_vwap"):
        bits.append("BTC<VWAP")
    chg = detail.get("btc_change_pct")
    if chg is not None:
        bits.append("24h+" if chg > 0 else "24h-")
    state = reg.direction.upper() if reg.direction != "none" else "NEUTRAL"
    return (f"Regime {state} | long_gate {detail.get('long_gate_score')}/2 "
            f"short_gate {detail.get('short_gate_score')}/2 | " + ", ".join(bits))


def _field_health(ft) -> dict:
    """Which ticker-derived fields actually populated (None = missing in live data)."""
    return {k: (getattr(ft, k, None) is not None) for k in
            ("last", "vwap", "high", "low", "change_pct", "quote_volume", "bid_volume", "ask_volume")}


def build_decision(cfg: dict, mgmt: dict) -> dict:
    universe = [str(s).upper() for s in (cfg.get("trading_symbols") or [])]
    if _GATE not in universe:
        universe = [_GATE] + universe
    max_scan = int(cfg.get("max_scan_symbols", len(universe)) or len(universe))
    min_score = float(cfg.get("min_score", 70))

    # --- BTC regime gate ---
    btc = features.fetch_symbol(_GATE)
    taker = features.taker_buy_ratio(_GATE)
    reg = scoring.regime(btc, taker, cfg)
    direction = reg.direction
    scan_direction = direction if direction in ("long", "short") else "long"

    # --- ALWAYS scan the universe for visibility (Audit #1/#5/#9) ---
    candidates = [s for s in universe if s != _GATE][: max(max_scan - 1, 0)]
    feats = [features.fetch_symbol(s) for s in candidates]
    scored = scoring.score_universe(feats, btc, cfg, scan_direction)
    board = _board(scored)
    best_score = round(scored[0].score, 1) if scored else 0.0

    circuit = mgmt.get("circuit", "ok")
    size_mode = "full" if reg.size_factor >= 1.0 else ("reduced" if reg.size_factor > 0 else "blocked")

    btc_vs_vwap = round((btc.last / btc.vwap - 1.0) * 100.0, 3) if (btc.ok and btc.last and btc.vwap) else None
    base_metrics = {
        "regime": {"direction": direction, "gate_open": direction != "none", "btc_vs_vwap": btc_vs_vwap},
        "regime_dir": direction,
        "data_health": {
            "kline_ok": bool(btc.ok and btc.last is not None and btc.vwap is not None
                             and btc.high is not None and btc.low is not None),
            "book_ok": bool(btc.bid_volume is not None and btc.ask_volume is not None),
            "funding_ok": bool(taker is not None),
        },
        "long_gate_score": reg.detail.get("long_gate_score"),
        "short_gate_score": reg.detail.get("short_gate_score"),
        "best_score": best_score,
        "min_score": min_score,
        "scanned": len(candidates),
        "size_mode": size_mode,
        "circuit": circuit,
        "today_pnl": mgmt.get("today_pnl"),
        "open_count": mgmt.get("open_count", 0),
    }
    base_meta = {
        "gate": reg.detail,
        "gate_summary": _gate_summary(reg),
        "board": board,
        "open_symbols": mgmt.get("open_symbols", []),
        "mgmt_actions": mgmt.get("actions", []),
        "controls_active": mgmt.get("controls_active", {}),
        "data_health": {"BTC": _field_health(btc),
                        **{s.symbol: _field_health(s.features) for s in scored[:3]}},
        "run_id": runtime.run_id,
    }

    def watch(symbol: str, reason: str, extra_metrics: dict = None) -> dict:
        metrics = dict(base_metrics)
        metrics["tradable_candidates"] = 0
        if extra_metrics:
            metrics.update(extra_metrics)
        meta = dict(base_meta)
        meta["reason"] = reason
        return {"action": "watch", "symbol": symbol, "confidence": 0.0,
                "metrics": metrics, "meta": meta, "plan": None}

    top_symbol = scored[0].symbol if scored else _GATE

    if circuit in ("paused", "tripped"):
        return watch(top_symbol, "circuit_" + str(circuit))
    if direction == "none":
        return watch(top_symbol, "btc_regime_neutral")

    qualified = [s for s in scored if not s.skip and s.score >= min_score]
    if not qualified:
        return watch(top_symbol, "no_setup_at_or_above_min_score")

    best = qualified[0]
    plan = risk.build_plan(best.features, cfg, reg.size_factor, side=direction)
    if plan is None or not plan.sizing_ok:
        return watch(best.symbol, "sizing_failed", {"tradable_candidates": len(qualified)})

    metrics = dict(base_metrics)
    metrics.update({
        "tradable_candidates": len(qualified),
        "side": plan.side,
        "limit_price": plan.entry,
        "sl_price": plan.sl_price,
        "sl_pct": round(plan.sl_pct * 100.0, 3),
        "tp1_price": plan.tp1,
        "tp2_price": plan.tp2,
        "notional_usdt": round(plan.notional_usdt, 2),
        "margin_usdt": round(plan.margin_usdt, 2),
        "leverage": plan.leverage,
        "sizing_ok": plan.sizing_ok,
    })
    meta = dict(base_meta)
    meta.update({
        "score_dims": best.dims,
        "atr14_est": plan.atr,
        "size_factor": plan.size_factor,
        "ladder": {
            "tp1_pct": float(cfg.get("tp1_pct", "3.5")),
            "tp2_pct": float(cfg.get("tp2_pct", "7.0")),
            "tp1_size": 0.5, "tp2_size": 0.25, "runner_size": 0.25,
            "trail_atr": plan.trail_atr, "breakeven_price": plan.breakeven_price,
        },
        "sizing_note": plan.note or "sized_from_max_loss_usdt",
        "execution_note": "follow-trade places the limit entry with a stop and first target; "
                          "TP2/runner/trailing/breakeven are managed across scans",
    })
    confidence = max(0.0, min(1.0, best.score / 100.0))
    return {
        "action": plan.side,  # "long" or "short"
        "symbol": best.symbol,
        "confidence": confidence,
        "metrics": metrics,
        "meta": meta,
        "plan": {
            "side": plan.side, "entry": plan.entry, "sl_price": plan.sl_price,
            "tp1": plan.tp1, "tp2": plan.tp2,
            "margin_usdt": plan.margin_usdt, "notional_usdt": plan.notional_usdt,
        },
    }


def run() -> None:
    cfg = _cfg()
    try:
        follow = runtime.is_follow_trade()
    except Exception:
        follow = False

    mgmt: dict = {"circuit": "ok"}
    if follow:
        try:
            mgmt = execution.manage_open_state(cfg)
        except Exception as exc:
            mgmt = {"circuit": "ok", "mgmt_error": type(exc).__name__}

    decision = build_decision(cfg, mgmt)
    decision["meta"]["follow_trade"] = follow

    # v0.1.6: execute through the sanctioned runtime.emit_signal_or_follow flow.
    # The inline open_if_allowed() used in v0.1.2-0.1.5 emitted signals fine but
    # never placed orders -- the runtime only arms the managed trade proxy inside
    # the execute_trade callback, so direct calls are silently dropped. This puts
    # order placement back inside that callback.
    runtime.emit_signal_or_follow(
        action=decision["action"],
        symbol=decision["symbol"],
        confidence=decision["confidence"],
        metrics=_sanitize(decision["metrics"]),
        meta=_sanitize(decision["meta"]),
        execute_trade=lambda: execution.open_if_allowed(decision, cfg, mgmt),
    )


if __name__ == "__main__":
    run()
