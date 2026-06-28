#!/usr/bin/env python3
"""RUNECLAW trade analytics + journal -- Phase 1 of the validation loop.

Runs the offline backtest (research/replay.py: full pipeline, fill-modeled, net of
fees) and turns the raw trades into the metrics that actually drive iteration:

  - expectancy, profit factor, win rate, avg win / avg loss
  - MAE / MFE -- the data that quantifies whether the trail / breakeven is worth it
    and whether stops are too tight, WITHOUT a months-long live trail test
  - capture ratio (how much of the available favorable move we keep on winners)
  - breakdowns by entry_mode, exit reason, and symbol
  - a one-line edge verdict + the headline lever to pull next

...and writes a structured per-trade journal (JSON) for deeper analysis and to
compare against the live trade journal (Phase 4).

This is the loop the session lacked: it would have shown the trail's value (or
lack of it) as a NUMBER -- losers' average MFE -- instead of a multi-week live wait.

Usage:
  python3 research/analytics.py --days 30 --breakout --exit-mode trail --fee 0.06
  python3 research/analytics.py --days 30 --journal out/journal.json
  python3 research/analytics.py --days 30 --ab            # fixed vs trail A/B
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay  # noqa: E402  (sets up engine stubs + simulate)


def _stats(vals):
    if not vals:
        return {"n": 0}
    s = sorted(vals)
    n = len(s)
    return {"n": n, "mean": round(sum(s) / n, 3), "median": round(s[n // 2], 3),
            "min": round(s[0], 3), "max": round(s[-1], 3)}


def analyze(trades):
    """Pure aggregation over the per-trade records replay.simulate produces.
    Network-free -> unit-testable on synthetic trades."""
    out = {"n_trades": len(trades)}
    if not trades:
        return out
    rets = [t["ret_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    out["win_rate"] = round(len(wins) / len(rets) * 100, 1)
    out["expectancy_pct"] = round(sum(rets) / len(rets), 3)
    out["total_pct"] = round(sum(rets), 2)
    out["profit_factor"] = round(gross_win / gross_loss, 2) if gross_loss > 0 else None
    out["avg_win_pct"] = round(gross_win / len(wins), 3) if wins else 0.0
    out["avg_loss_pct"] = round(-gross_loss / len(losses), 3) if losses else 0.0

    # --- MAE / MFE: the trail / stop diagnostics ---
    out["mfe"] = _stats([t.get("mfe_pct", 0.0) for t in trades])
    out["mae"] = _stats([t.get("mae_pct", 0.0) for t in trades])
    win_t = [t for t in trades if t["ret_pct"] > 0]
    lose_t = [t for t in trades if t["ret_pct"] <= 0]
    # capture ratio: fraction of the favorable move kept on winners (1.0 = exited at peak)
    cap = [t["ret_pct"] / t["mfe_pct"] for t in win_t if t.get("mfe_pct", 0) > 0.01]
    out["capture_ratio"] = round(sum(cap) / len(cap), 2) if cap else None
    # losers' MFE: how far they went favorable before reversing -> the trail/BE
    # opportunity. High loser-MFE means a trailing stop would have saved real money.
    out["loser_avg_mfe_pct"] = round(sum(t.get("mfe_pct", 0) for t in lose_t) / len(lose_t), 3) if lose_t else 0.0
    # winners' MAE: heat winners absorbed -> are stops nearly cutting winners?
    out["winner_avg_mae_pct"] = round(sum(t.get("mae_pct", 0) for t in win_t) / len(win_t), 3) if win_t else 0.0

    def _grp(key):
        g = {}
        for t in trades:
            g.setdefault(t.get(key, "?"), []).append(t["ret_pct"])
        return {k: {"n": len(v), "expectancy": round(sum(v) / len(v), 3),
                    "total": round(sum(v), 2),
                    "win_rate": round(sum(1 for x in v if x > 0) / len(v) * 100, 0)}
                for k, v in g.items()}
    out["by_mode"] = _grp("mode")
    out["by_reason"] = _grp("reason")
    out["by_symbol"] = _grp("sym")
    return out


def verdict(a):
    """One-line read: is there edge, and what's the biggest lever to pull next."""
    if not a.get("n_trades"):
        return "NO TRADES -- nothing to judge (likely the fill problem: limits never filled)."
    pf = a.get("profit_factor")
    exp = a.get("expectancy_pct")
    has_edge = bool(exp and exp > 0 and (pf is None or pf > 1.0))
    levers = []
    if a.get("loser_avg_mfe_pct", 0) > abs(a.get("avg_loss_pct", 0)) * 0.8:
        levers.append("losers gave back large MFE -> a TRAIL/BE likely adds expectancy")
    cap = a.get("capture_ratio")
    if cap is not None and cap < 0.5:
        levers.append("low capture ratio -> TPs too far / exits too late")
    if a.get("winner_avg_mae_pct", 0) < -abs(a.get("avg_loss_pct", 0)):
        levers.append("winners took heavy heat -> stops may be cutting would-be winners")
    tag = "EDGE" if has_edge else "NO EDGE"
    lev = (" | " + " ; ".join(levers)) if levers else ""
    return f"{tag}: expectancy {exp:+.3f}%/trade, PF {pf}, win {a.get('win_rate')}%{lev}"


def print_report(a, title=""):
    print("\n" + "=" * 60)
    print(f"TRADE ANALYTICS {title}")
    print("=" * 60)
    if not a.get("n_trades"):
        print("  no trades"); print("VERDICT:", verdict(a)); return
    print(f"  trades            : {a['n_trades']}")
    print(f"  win rate          : {a['win_rate']}%")
    print(f"  expectancy        : {a['expectancy_pct']:+.3f}% / trade")
    print(f"  total return      : {a['total_pct']:+.2f}%")
    print(f"  profit factor     : {a['profit_factor']}")
    print(f"  avg win / loss    : {a['avg_win_pct']:+.3f}% / {a['avg_loss_pct']:+.3f}%")
    print(f"  MFE (favorable)   : mean {a['mfe']['mean']:+.2f}%  max {a['mfe']['max']:+.2f}%")
    print(f"  MAE (adverse)     : mean {a['mae']['mean']:+.2f}%  min {a['mae']['min']:+.2f}%")
    print(f"  capture ratio     : {a['capture_ratio']}  (win ret / win MFE; 1.0 = exit at peak)")
    print(f"  losers' avg MFE   : {a['loser_avg_mfe_pct']:+.2f}%  <- trail/BE opportunity")
    print(f"  winners' avg MAE  : {a['winner_avg_mae_pct']:+.2f}%  <- heat winners absorbed")
    print(f"  by entry_mode     : {a['by_mode']}")
    print(f"  by exit reason    : {a['by_reason']}")
    print("=" * 60)
    print("VERDICT:", verdict(a))


def _run(cfg, symbols, days, use_breakout, data):
    res = replay.simulate(cfg, symbols, days, use_breakout, data=data)
    return (res or {}).get("trades", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--breakout", action="store_true")
    ap.add_argument("--exit-mode", default="fixed", choices=["fixed", "trail"])
    ap.add_argument("--fee", default="0.06", help="round-turn taker fee %% (per side modeled x2)")
    ap.add_argument("--ab", action="store_true", help="A/B fixed vs trail exit")
    ap.add_argument("--journal", default="", help="write per-trade journal JSON to this path")
    ap.add_argument("--symbols", nargs="*", default=[
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT",
        "ADAUSDT", "LINKUSDT", "AVAXUSDT", "NEARUSDT", "INJUSDT", "SEIUSDT"])
    a = ap.parse_args()
    cfg = dict(replay.__dict__.get("DEFAULT_CFG", {}))
    if not cfg:  # mirror replay.main's defaults
        cfg = {"min_score": 70, "min_volume_usdt": "0", "max_vwap_ext_pct": "4.0",
               "atr_limit_mult": "0.5", "tp1_pct": "3.5", "tp2_pct": "7.0",
               "trail_atr_mult": "2.0", "breakeven_pct": "2.0", "sl_min_btc_eth_pct": "1.5",
               "sl_min_sol_bnb_pct": "1.2", "sl_min_alt_pct": "2.5", "max_loss_usdt": "15",
               "leverage": 10, "margin_budget": "100", "limit_expiry_hours": "4",
               "limit_chase_pct": "3.0", "time_stop_hours": "12", "atr_period": 14,
               "trend_lookback": 12, "trend_norm": "0.05", "trend_weight": "15.0",
               "breakout_trend_min": "0.6", "breakout_extreme_band": "0.015",
               "breakout_stop_atr_mult": "1.0", "breakout_level_buffer_pct": "0.2",
               "breakout_tp1_pct": "4.0", "allow_short": True, "funding_skip_bps": "30",
               "funding_penalty_weight": "8.0"}
    cfg["fee_pct"] = a.fee
    data = replay.fetch_all(a.symbols, a.days)

    if a.ab:
        for mode in ("fixed", "trail"):
            cfg["exit_mode"] = mode
            trades = _run(cfg, a.symbols, a.days, a.breakout, data)
            print_report(analyze(trades), title=f"[exit={mode}, breakout={'on' if a.breakout else 'off'}]")
        return
    cfg["exit_mode"] = a.exit_mode
    trades = _run(cfg, a.symbols, a.days, a.breakout, data)
    rep = analyze(trades)
    print_report(rep, title=f"[exit={a.exit_mode}, breakout={'on' if a.breakout else 'off'}]")
    if a.journal:
        Path(a.journal).parent.mkdir(parents=True, exist_ok=True)
        Path(a.journal).write_text(json.dumps({"summary": rep, "trades": trades}, indent=2))
        print(f"\njournal written: {a.journal} ({len(trades)} trades)")


if __name__ == "__main__":
    main()
