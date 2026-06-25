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

    # --- Pass 2 (v0.2.0): the cheap ticker scan picked the field; now refine only
    # the finalists. Enrich the top-N qualified with kline (real ATR + higher-TF
    # trend) and funding, apply trend alignment + funding crowding, then re-rank.
    # Bounded call budget (~2 calls x enrich_top_n), graceful-degrades per symbol.
    enrich_n = max(int(cfg.get("enrich_top_n", 8)), 1)
    enriched = []
    for s in qualified[:enrich_n]:
        features.enrich(s.features, cfg)
        adj, extra, skip, reason = scoring.enrich_score(s, s.features, cfg)
        s.dims = {**s.dims, **extra}
        s.score, s.skip, s.skip_reason = adj, skip, reason
        if not skip:
            enriched.append(s)
    enriched.sort(key=lambda s: s.score, reverse=True)
    if not enriched:
        return watch(top_symbol, "no_setup_after_enrichment", {"tradable_candidates": 0})

    best = enriched[0]
    plan = risk.build_plan(best.features, cfg, reg.size_factor, side=direction)
    if plan is None or not plan.sizing_ok:
        return watch(best.symbol, "sizing_failed", {"tradable_candidates": len(enriched)})

    # Pre-placement staleness skip (v0.1.17): if the limit entry is already more
    # than limit_chase_pct from current price, it is not a fillable pullback -- in
    # a fast move VWAP lags and the entry lands far from market, so it would just
    # rest dead and clog a slot. Stand aside (WATCH) instead of placing it. Uses
    # the price already fetched for this candidate; no pending-order query needed,
    # so it is immune to the management-layer parse bug. The chase-guard remains a
    # backstop for orders that drift stale AFTER a fillable placement.
    chase_pct = float(cfg.get("limit_chase_pct", "3.0")) / 100.0
    cur = best.features.last
    if chase_pct > 0 and cur and cur > 0 and plan.entry and plan.entry > 0:
        gap = ((plan.entry - cur) / plan.entry) if plan.side == "short" else ((cur - plan.entry) / plan.entry)
        if gap > chase_pct:
            return watch(best.symbol, "entry_too_far_{:.1f}pct".format(gap * 100.0),
                         {"tradable_candidates": len(enriched)})

    metrics = dict(base_metrics)
    metrics.update({
        "tradable_candidates": len(enriched),
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
        "kline_ok": bool(best.features.kline_ok),
        "funding_ok": bool(best.features.funding_ok),
        "trend_dir": best.features.trend_dir,
        "funding_now": best.features.funding_now,
    })
    meta = dict(base_meta)
    meta.update({
        "score_dims": best.dims,
        "atr14_est": plan.atr,
        "atr_source": "kline_wilder" if best.features.kline_ok else "range_proxy",
        "trend": {"dir": best.features.trend_dir,
                  "strength": round(best.features.trend_strength, 3)},
        "funding": {"now": best.features.funding_now, "avg": best.features.funding_avg},
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
    try:
        exec_mode = str(runtime.execution_mode())
    except Exception:
        exec_mode = "?"

    mgmt: dict = {"circuit": "ok"}
    if follow:
        try:
            mgmt = execution.manage_open_state(cfg)
        except Exception as exc:
            mgmt = {"circuit": "ok", "mgmt_error": type(exc).__name__}

    decision = build_decision(cfg, mgmt)
    decision["meta"]["follow_trade"] = follow

    # v0.1.7 diagnostic: keep the sanctioned emit_signal_or_follow trade path, but
    # capture (a) whether the runtime actually invoked our trade callback and
    # (b) the placement result, via a closure. Then emit a second, readable signal
    # encoding it -- the catalog exposes only action/symbol/confidence, so the
    # diagnosis is packed into those.
    captured = {"called": False}

    def _execute():
        captured["called"] = True
        try:
            res = execution.open_if_allowed(decision, cfg, mgmt) or {}
        except Exception as exc:
            res = {"placed": False, "reason": "exc_" + type(exc).__name__}
        captured.update(res)
        return res

    runtime.emit_signal_or_follow(
        action=decision["action"],
        symbol=decision["symbol"],
        confidence=decision["confidence"],
        metrics=_sanitize(decision["metrics"]),
        meta=_sanitize(decision["meta"]),
        execute_trade=_execute,
    )

    # v0.1.15 MANAGEMENT DIAGNOSTIC. Two stale-limit-prune fixes failed, so stop
    # guessing and surface every link of the management chain on a readable
    # (actionable-primary) channel: did manage_open_state run at all (f =
    # is_follow_trade), how many live pending orders it saw (pT) vs recognised as
    # ours by size (oP / own), what it actually did (act), and any error (e) --
    # plus the trade outcome (c/p/reason). Plain emit_signal on a sentinel symbol,
    # so it trades nothing; it only reports. Decode:
    #   f0...      -> management never ran (is_follow_trade gate) <- bug is the gate
    #   f1-own0-pT>0 -> ran but recognised none of our orders <- size-scoping
    #   f1-own>0-act0 with a stale order live -> chase-guard logic <- prune/extract
    called = bool(captured.get("called"))
    placed = captured.get("placed")
    full_reason = str(captured.get("reason", "")) or "none"
    pcode = "1" if placed is True else ("0" if placed is False else "X")
    emc = {"follow_trade": "F", "signal_only": "S"}.get(exec_mode, "?")
    own = mgmt.get("open_count", 0)
    pT = mgmt.get("pending_total", "?")
    oP = mgmt.get("owned_pending", "?")
    acts = len(mgmt.get("actions", []) or [])
    merr = str(mgmt.get("mgmt_error") or mgmt.get("position_query_error") or "ok")[:16]
    rshort = full_reason.replace(" ", "_")[:32]
    pshape = str(mgmt.get("pending_shape", ""))[:30]
    preason = str(mgmt.get("pending_reason", ""))[:24]
    # Tail, finalized (v0.1.20). Diagnostics are complete: the pending read path is
    # confirmed working live and pT0 is simply a flat / just-placed book, so the old
    # `shp.<envelope-keys>` empty-book dump is retired -- it was pure noise that read
    # as an error (misdiagnosed 3x). Two cases remain, in priority order:
    #   1. perr.<code:msg> -- the pending fetch genuinely FAILED (actionable error)
    #   2. <reason>        -- why nothing was placed / what was decided, untruncated:
    #      none, correlation_budget, entry_already_pending, stale_limit_cancel, ...
    # The full envelope shape is preserved in metrics (pending_shape) for deep debug.
    if str(pT) == "0" and preason:
        tail = "perr." + preason
    else:
        tail = rshort
    dbg = ("DBG-f{f}{em}-own{own}-pT{pt}-oP{op}-act{a}-c{c}p{p}-{t}"
           .format(f=int(follow), em=emc, own=own, pt=pT, op=oP, a=acts,
                   c=int(called), p=pcode, t=tail))[:63]
    runtime.emit_signal(
        action="close", symbol=dbg, confidence=0.222,
        metrics={"dbg": dbg, "follow": follow, "exec_mode": exec_mode,
                 "open_count": own, "pending_total": pT, "owned_pending": oP,
                 "mgmt_actions": mgmt.get("actions", []), "mgmt_error": merr,
                 "pending_reason": mgmt.get("pending_reason", ""),
                 "pending_shape": mgmt.get("pending_shape", ""),
                 "state_runs": mgmt.get("state_runs"),
                 "called": called, "placed": placed, "reason": full_reason[:120]},
        meta={"dbg": dbg, "mgmt": _sanitize(mgmt)},
    )


if __name__ == "__main__":
    run()
