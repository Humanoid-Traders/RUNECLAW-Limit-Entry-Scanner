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
# v0.9.4: version + provenance stamp on every emitted analysis record, so
# downstream consumers (journal reducer, dashboards, future reconciliation)
# can attribute any output to the exact analysis generation that produced it.
# The engine is deterministic end-to-end -- no LLM in the decision path.
ANALYSIS_VERSION = "0.9.4"
THESIS_SOURCE = "deterministic_rules"


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


def _universes(cfg: dict) -> list:
    """List of {name, leader, symbols, allow_short}. Falls back to the legacy
    single BTC-led universe from ``trading_symbols`` when ``universes`` is unset."""
    default_syms = [str(s).upper() for s in (cfg.get("trading_symbols") or [])]
    out = []
    for u in (cfg.get("universes") or []):
        if not isinstance(u, dict):
            continue
        leader = str(u.get("leader", _GATE)).upper()
        # a universe with no explicit symbols inherits the default trading_symbols
        syms = [str(s).upper() for s in (u.get("symbols") or [])] or default_syms
        if syms:
            out.append({"name": str(u.get("name", leader)), "leader": leader,
                        "symbols": syms, "allow_short": u.get("allow_short")})
    if out:
        return out
    return [{"name": "crypto", "leader": _GATE, "symbols": default_syms, "allow_short": None}]


def _scan_universe(uni: dict, cfg: dict) -> dict:
    """Resolve one universe's regime from its own leader and score its symbols.
    Each candidate is tagged with the universe name and its regime size_factor."""
    leader_sym = uni["leader"]
    # v0.6.0: per-universe config overrides for the pass-1 scan. allow_short
    # (legacy) plus an optional `overrides` map -- e.g. equities lower
    # max_vwap_ext_pct so a smaller move routes to breakout. Pass-2 enrichment
    # runs on the merged pool with global cfg, so only pass-1 keys are tunable here.
    over = {}
    if uni.get("allow_short") is not None:
        over["allow_short"] = bool(uni["allow_short"])
    extra_over = uni.get("overrides")
    if isinstance(extra_over, dict):
        over.update(extra_over)
    ucfg = {**cfg, **over} if over else cfg
    leader_feats = features.fetch_symbol(leader_sym)
    taker = features.taker_buy_ratio(leader_sym)
    reg = scoring.regime(leader_feats, taker, ucfg)
    direction = reg.direction
    scan_direction = direction if direction in ("long", "short") else "long"
    max_scan = int(cfg.get("max_scan_symbols", len(uni["symbols"])) or len(uni["symbols"]))
    min_score = float(cfg.get("min_score", 70))
    candidates = [s for s in uni["symbols"] if s != leader_sym][: max(max_scan, 0)]
    feats = [features.fetch_symbol(s) for s in candidates]
    # v0.6.0: breakout is per-universe. A universe opts in with `breakout: true`;
    # absent the flag it defaults to crypto-only (v0.5.x behavior). Gated by the
    # global breakout_enabled master switch. (equities now opt in -- they trend too
    # and the pullback-only path kept chase-cancelling into clean down-moves.)
    allow_breakout = (bool(cfg.get("breakout_enabled", False))
                      and bool(uni.get("breakout", uni["name"] == "crypto")))
    scored = scoring.score_universe(feats, leader_feats, ucfg, scan_direction,
                                    allow_breakout=allow_breakout)
    for s in scored:
        s.universe = uni["name"]
        s.size_factor = reg.size_factor
    qualified = ([s for s in scored if not s.skip and s.score >= min_score]
                 if direction in ("long", "short") else [])
    return {"name": uni["name"], "leader": leader_sym, "regime": reg,
            "leader_feats": leader_feats, "direction": direction,
            "scored": scored, "qualified": qualified}


def build_decision(cfg: dict, mgmt: dict) -> dict:
    min_score = float(cfg.get("min_score", 70))
    unis = _universes(cfg)
    scans = [_scan_universe(u, cfg) for u in unis]

    # --- merge board + qualified pool across universes (mixed sides) ---
    all_scored = []
    for sc in scans:
        all_scored.extend(sc["scored"])
    all_scored.sort(key=lambda s: (not s.skip, s.score), reverse=True)
    board = _board(all_scored)
    best_score = round(all_scored[0].score, 1) if all_scored else 0.0

    qualified = []
    for sc in scans:
        qualified.extend(sc["qualified"])
    qualified.sort(key=lambda s: s.score, reverse=True)

    any_active = any(sc["direction"] in ("long", "short") for sc in scans)
    circuit = mgmt.get("circuit", "ok")

    regime_by_uni = {}
    for sc in scans:
        reg, lf = sc["regime"], sc["leader_feats"]
        regime_by_uni[sc["name"]] = {
            "leader": sc["leader"],
            "direction": sc["direction"],
            "size_factor": reg.size_factor,
            "leader_vs_vwap": (round((lf.last / lf.vwap - 1.0) * 100.0, 3)
                               if (lf.ok and lf.last and lf.vwap) else None),
            "long_gate": reg.detail.get("long_gate_score"),
            "short_gate": reg.detail.get("short_gate_score"),
        }

    base_metrics = {
        "regime_by_universe": regime_by_uni,
        "any_regime_open": any_active,
        "best_score": best_score,
        "min_score": min_score,
        "scanned": sum(len(sc["scored"]) for sc in scans),
        "universes": [sc["name"] for sc in scans],
        "circuit": circuit,
        "today_pnl": mgmt.get("today_pnl"),
        "open_count": mgmt.get("open_count", 0),
    }
    base_meta = {
        "regime_by_universe": regime_by_uni,
        "gate_summary": " || ".join("{}: {}".format(sc["name"], _gate_summary(sc["regime"]))
                                    for sc in scans),
        "board": board,
        "open_symbols": mgmt.get("open_symbols", []),
        "mgmt_actions": mgmt.get("actions", []),
        "controls_active": mgmt.get("controls_active", {}),
        "data_health": {sc["leader"]: _field_health(sc["leader_feats"]) for sc in scans},
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

    top_symbol = all_scored[0].symbol if all_scored else unis[0]["leader"]

    if circuit in ("paused", "tripped"):
        return watch(top_symbol, "circuit_" + str(circuit))
    if not any_active:
        return watch(top_symbol, "all_regimes_neutral")
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
    plan = risk.build_plan(best.features, cfg, best.size_factor, side=best.side,
                           entry_mode=best.entry_mode)
    if plan is None or not plan.sizing_ok:
        return watch(best.symbol, "sizing_failed", {"tradable_candidates": len(enriched)})

    # Pre-placement staleness skip (v0.1.17): if the limit entry is already more
    # than limit_chase_pct from current price, it is not a fillable pullback -- in
    # a fast move VWAP lags and the entry lands far from market, so it would just
    # rest dead and clog a slot. Stand aside (WATCH) instead of placing it. Uses
    # the price already fetched for this candidate; no pending-order query needed,
    # so it is immune to the management-layer parse bug. The chase-guard remains a
    # backstop for orders that drift stale AFTER a fillable placement.
    # v0.5.0: only the pullback path rests a limit that can land too far from
    # price. A breakout enters at market by design (it IS extended past VWAP), so
    # the staleness skip must not veto it.
    chase_pct = float(cfg.get("limit_chase_pct", "3.0")) / 100.0
    cur = best.features.last
    if (plan.entry_mode != "breakout" and chase_pct > 0 and cur and cur > 0
            and plan.entry and plan.entry > 0):
        gap = ((plan.entry - cur) / plan.entry) if plan.side == "short" else ((cur - plan.entry) / plan.entry)
        if gap > chase_pct:
            return watch(best.symbol, "entry_too_far_{:.1f}pct".format(gap * 100.0),
                         {"tradable_candidates": len(enriched)})

    metrics = dict(base_metrics)
    metrics.update({
        "tradable_candidates": len(enriched),
        "side": plan.side,
        "entry_mode": plan.entry_mode,
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
        "universe": best.universe,
        "analysis_version": ANALYSIS_VERSION,
        "thesis_source": THESIS_SOURCE,
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
            "tp1": plan.tp1, "tp2": plan.tp2, "entry_mode": plan.entry_mode,
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
    # v0.5.0: a placed breakout market entry shows c{c}pB (vs p1 for a pullback
    # limit), so a momentum entry is unmistakable on the compact line.
    pcode = "1" if placed is True else ("0" if placed is False else "X")
    if placed is True and str(captured.get("entry_mode", "")) == "breakout":
        pcode = "B"
    emc = {"follow_trade": "F", "signal_only": "S"}.get(exec_mode, "?")
    own = mgmt.get("open_count", 0)
    pT = mgmt.get("pending_total", "?")
    oP = mgmt.get("owned_pending", "?")
    mgmt_actions = mgmt.get("actions", []) or []
    acts = len(mgmt_actions)
    # v0.3.1: each action is {"<type>": symbol, ...}; surface the first action's
    # type so a fired chase/expiry/circuit/TP shows WHAT happened in the tail,
    # not just a bare act{N} (the verdict ambiguity hit live twice on 0.3.0).
    act_label = ""
    if mgmt_actions and isinstance(mgmt_actions[0], dict) and mgmt_actions[0]:
        act_label = str(next(iter(mgmt_actions[0].keys())))
    merr = str(mgmt.get("mgmt_error") or mgmt.get("position_query_error") or "ok")[:16]
    rshort = full_reason.replace(" ", "_")[:32]
    pshape = str(mgmt.get("pending_shape", ""))[:30]
    preason = str(mgmt.get("pending_reason", ""))[:24]
    # v0.4.2: a stuck owned-pending order that should have time-expired but did not
    # (no parseable open-time, or the cancel API rejected). This is the silent
    # failure that left a limit resting 5h+ on Classic with act0 and no cancel.
    xpd = str(mgmt.get("expiry_diag", ""))[:26]
    # Tail priority (v0.3.1). Three cases, in order:
    #   1. act.<type>      -- a management action fired this cycle (stale_limit_cancel,
    #      limit_expiry_cancel, circuit_cancel, time_stop_close, auto_be, ...)
    #   2. perr.<code:msg> -- the pending fetch genuinely FAILED (actionable error)
    #   3. <reason>        -- why nothing was placed / what was decided, untruncated
    # The full envelope shape is preserved in metrics (pending_shape) for deep debug.
    if acts and act_label:
        tail = "act." + act_label[:28]
    elif xpd:
        # Surface the stuck-expiry diagnostic over the scan reason: an order the bot
        # cannot time-expire is more urgent than why nothing was placed. (xpd and
        # perr are mutually exclusive in practice -- xpd needs an owned pending to
        # exist, perr fires only when pT==0.) Full scan reason stays in metrics.
        tail = "xpd." + xpd
    elif str(pT) == "0" and preason:
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
                 "expiry_diag": mgmt.get("expiry_diag", ""),
                 "pending_max_age_h": mgmt.get("pending_max_age_h"),
                 "position_diag": mgmt.get("position_diag", []),
                 "state_runs": mgmt.get("state_runs"),
                 # v0.9.0 observability: emit each control's running state every cycle
                 # (not only when it fires) so an INERT control is visible -- the trail
                 # was dead a whole session because nothing surfaced its status.
                 "controls_active": mgmt.get("controls_active", {}),
                 "loss_breaker": mgmt.get("loss_breaker", False),
                 "realized_window_pnl": mgmt.get("realized_window_pnl"),
                 # v0.9.1 Phase-4 live journal: closed-trade realized records for
                 # live-vs-backtest reconciliation (accrues over cycles via metrics).
                 "fills_journal": mgmt.get("fills_journal", []),
                 "analysis_version": ANALYSIS_VERSION,
                 "thesis_source": THESIS_SOURCE,
                 "called": called, "placed": placed, "reason": full_reason[:120]},
        meta={"dbg": dbg, "mgmt": _sanitize(mgmt)},
    )


if __name__ == "__main__":
    run()
