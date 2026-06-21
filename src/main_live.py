"""Live scan + signal for the RUNECLAW Limit Entry Scanner.

Each scheduled pass: score the BTC regime gate, scan the universe, rank
candidates, and emit one managed signal. ``runtime.emit_signal_or_follow`` then
places the trade only when the subscription is in follow-trade mode and the
signal action is actionable.
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


def _board(scored: list, limit: int = 15) -> list:
    out = []
    for s in scored[:limit]:
        out.append(
            {
                "symbol": s.symbol,
                "score": round(s.score, 1),
                "skip": s.skip,
                "skip_reason": s.skip_reason,
                "dims": s.dims,
            }
        )
    return out


def build_decision(cfg: dict) -> dict:
    universe = [str(s).upper() for s in (cfg.get("trading_symbols") or [])]
    if _GATE not in universe:
        universe = [_GATE] + universe
    max_scan = int(cfg.get("max_scan_symbols", len(universe)) or len(universe))

    # --- BTC regime gate ---
    btc = features.fetch_symbol(_GATE)
    taker = features.taker_buy_ratio(_GATE)
    gate = scoring.btc_gate(btc, taker)

    if not gate.open:
        return {
            "action": "watch",
            "symbol": _GATE,
            "confidence": 0.0,
            "metrics": {"btc_gate_score": gate.score, "tradable_candidates": 0},
            "meta": {"reason": "btc_gate_closed", "gate": gate.detail, "board": []},
            "plan": None,
        }

    # --- scan candidates (BTC is gate-only and never traded) ---
    candidates = [s for s in universe if s != _GATE][: max(max_scan - 1, 0)]
    feats = [features.fetch_symbol(s) for s in candidates]
    scored = scoring.score_universe(feats, btc, cfg)

    min_score = float(cfg.get("min_score", 70))
    qualified = [s for s in scored if not s.skip and s.score >= min_score]
    board = _board(scored)
    size_mode = "full" if gate.size_factor >= 1.0 else "reduced"

    if not qualified:
        best_score = round(scored[0].score, 1) if scored else 0.0
        top_symbol = scored[0].symbol if scored else _GATE
        return {
            "action": "watch",
            "symbol": top_symbol,
            "confidence": 0.0,
            "metrics": {
                "btc_gate_score": gate.score,
                "best_score": best_score,
                "tradable_candidates": 0,
                "size_mode": size_mode,
            },
            "meta": {
                "reason": "no_setup_at_or_above_min_score",
                "min_score": min_score,
                "gate": gate.detail,
                "board": board,
            },
            "plan": None,
        }

    best = qualified[0]
    plan = risk.build_plan(best.features, cfg, gate.size_factor)
    if plan is None or not plan.sizing_ok:
        return {
            "action": "watch",
            "symbol": best.symbol,
            "confidence": 0.0,
            "metrics": {
                "btc_gate_score": gate.score,
                "best_score": round(best.score, 1),
                "tradable_candidates": len(qualified),
                "sizing_ok": False,
                "size_mode": size_mode,
            },
            "meta": {"reason": "sizing_failed", "gate": gate.detail, "board": board},
            "plan": None,
        }

    metrics = {
        "btc_gate_score": gate.score,
        "best_score": round(best.score, 1),
        "tradable_candidates": len(qualified),
        "size_mode": size_mode,
        "limit_price": plan.entry,
        "sl_price": plan.sl_price,
        "sl_pct": round(plan.sl_pct * 100.0, 3),
        "tp1_price": plan.tp1,
        "tp2_price": plan.tp2,
        "notional_usdt": round(plan.notional_usdt, 2),
        "margin_usdt": round(plan.margin_usdt, 2),
        "leverage": plan.leverage,
        "sizing_ok": plan.sizing_ok,
    }
    meta = {
        "gate": gate.detail,
        "score_dims": best.dims,
        "atr14_est": plan.atr,
        "size_factor": plan.size_factor,
        "ladder": {
            "tp1_pct": float(cfg.get("tp1_pct", "3.5")),
            "tp2_pct": float(cfg.get("tp2_pct", "7.0")),
            "tp1_size": 0.5,
            "tp2_size": 0.25,
            "runner_size": 0.25,
            "trail_atr": plan.trail_atr,
            "breakeven_price": plan.breakeven_price,
        },
        "execution_note": (
            "follow-trade places the limit entry with a protective stop and the first "
            "take-profit; the second target, runner, and trailing/breakeven are surfaced "
            "here for the management layer and manual subscribers"
        ),
        "sizing_note": plan.note or "sized_from_max_loss_usdt",
        "board": board,
        "run_id": runtime.run_id,
    }
    confidence = max(0.0, min(1.0, best.score / 100.0))
    return {
        "action": "long",
        "symbol": best.symbol,
        "confidence": confidence,
        "metrics": metrics,
        "meta": meta,
        "plan": {
            "entry": plan.entry,
            "sl_price": plan.sl_price,
            "tp1": plan.tp1,
            "tp2": plan.tp2,
            "margin_usdt": plan.margin_usdt,
            "notional_usdt": plan.notional_usdt,
        },
    }


def run() -> None:
    cfg = _cfg()
    decision = build_decision(cfg)
    runtime.emit_signal_or_follow(
        action=decision["action"],
        symbol=decision["symbol"],
        confidence=decision["confidence"],
        metrics=_sanitize(decision["metrics"]),
        meta=_sanitize(decision["meta"]),
        execute_trade=lambda: execution.execute(decision, cfg),
    )


if __name__ == "__main__":
    run()
