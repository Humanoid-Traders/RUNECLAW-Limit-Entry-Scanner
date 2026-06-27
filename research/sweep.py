#!/usr/bin/env python3
"""Parameter sweep over the RUNECLAW research replay (APPROXIMATE -- see replay.py
limitations: no order-book dim, bar-touch fills, single-position, fee modeled).

Fetches klines once, then runs the replay across a grid of one parameter, net of
fees, and prints fill-rate / win-rate / net-return per setting. For data-first
tuning of the chase-cancel and exit behavior before anything touches live.

Usage:
  python3 research/sweep.py --param atr_limit_mult --values 0.3 0.5 0.7 1.0
  python3 research/sweep.py --param tp1_pct --values 2.0 3.5 5.0 7.0 --days 30
"""
import argparse
import contextlib
import io
import os

import replay as R  # same dir


BASE = {
    "min_score": 70, "min_volume_usdt": "0", "max_vwap_ext_pct": "4.0",
    "atr_limit_mult": "0.5", "tp1_pct": "3.5", "tp2_pct": "7.0", "trail_atr_mult": "1.0",
    "breakeven_pct": "2.0", "sl_min_btc_eth_pct": "1.5", "sl_min_sol_bnb_pct": "1.2",
    "sl_min_alt_pct": "2.5", "max_loss_usdt": "15", "leverage": 10, "margin_budget": "100",
    "limit_expiry_hours": "4", "limit_chase_pct": "3.0", "time_stop_hours": "4",
    "atr_period": 14, "trend_lookback": 12, "trend_norm": "0.05", "trend_weight": "15.0",
    "breakout_trend_min": "0.6", "breakout_extreme_band": "0.015", "breakout_stop_atr_mult": "1.0",
    "breakout_level_buffer_pct": "0.2", "breakout_tp1_pct": "4.0", "allow_short": True,
    "funding_skip_bps": "30", "funding_penalty_weight": "8.0",
    "fee_pct": "0.04",  # per-side; round-turn = 2x in the replay
}


def run_one(cfg, symbols, days, breakout, data):
    with contextlib.redirect_stdout(io.StringIO()):  # mute the per-run report
        st = R.simulate(cfg, symbols, days, breakout, data=data)
    t = st["trades"]; nsig = st["n_sig"]
    if not t:
        return dict(trades=0, fill=0.0, win=0.0, net=0.0, avg=0.0, chase=st["n_chase"])
    wins = sum(1 for x in t if x["ret_pct"] > 0)
    net = sum(x["ret_pct"] for x in t)
    return dict(trades=len(t), fill=len(t) / max(nsig, 1) * 100, win=wins / len(t) * 100,
                net=net, avg=net / len(t), chase=st["n_chase"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--param", required=True)
    ap.add_argument("--values", nargs="+", required=True)
    ap.add_argument("--days", type=int, default=25)
    ap.add_argument("--breakout", action="store_true")
    ap.add_argument("--symbols", nargs="*", default=[
        "TRUMPUSDT", "PEPEUSDT", "WLDUSDT", "DOGEUSDT", "SEIUSDT", "INJUSDT",
        "TAOUSDT", "ENAUSDT", "ONDOUSDT", "XRPUSDT", "SOLUSDT", "AVAXUSDT"])
    a = ap.parse_args()

    data = R.fetch_all(a.symbols, a.days)
    print(f"\nSWEEP {a.param}  (breakout={'ON' if a.breakout else 'off'}, "
          f"{len(a.symbols)} syms, {a.days}d, net of {BASE['fee_pct']}%/side fee)")
    print(f"{a.param:>16} | trades | fill% | win% |  net%  | avg%/t | chase")
    print("-" * 68)
    for v in a.values:
        cfg = dict(BASE); cfg[a.param] = v if not _is_num_field(a.param) else v
        r = run_one(cfg, a.symbols, a.days, a.breakout, data)
        print(f"{v:>16} | {r['trades']:>6} | {r['fill']:>4.0f} | {r['win']:>4.0f} | "
              f"{r['net']:>+6.1f} | {r['avg']:>+6.2f} | {r['chase']:>5}")
    print("-" * 68)
    print("NOTE: approximate single-position replay. Directional comparison only --\n"
          "use to rank settings, not to predict live P&L.")


def _is_num_field(p):
    return p in ("max_scan_symbols", "atr_period", "trend_lookback", "leverage", "min_score")


if __name__ == "__main__":
    main()
