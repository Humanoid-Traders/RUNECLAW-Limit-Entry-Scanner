"""RUNECLAW Limit Entry Scanner - sandbox entry point.

The package executes as ``python -m src.main``. This Playbook is live-only
(``backtest_support: none``) because its order-book dimension depends on live
depth that cannot be replayed, so the historical branch only emits a watch note
and never imports trading code.
"""
from getagent import runtime


def run() -> None:
    if runtime.is_live():
        # Import the live module lazily so historical runs never load trade code.
        from . import main_live

        main_live.run()
        return

    # backtest_support: none -> there is no historical replay path here.
    cfg = runtime.manifest.get("strategy_config", {}) or {}
    symbols = cfg.get("trading_symbols") or ["BTCUSDT"]
    runtime.emit_signal(
        action="watch",
        symbol=str(symbols[0]),
        confidence=0.0,
        metrics={"mode": "historical", "tradable_candidates": 0},
        meta={
            "reason": "RUNECLAW is live-only (order-book dependent); no historical backtest path",
            "run_id": runtime.run_id,
        },
    )


if __name__ == "__main__":
    run()
