#!/usr/bin/env python3
"""Phase 3 value-test: does open-interest divergence separate winners from losers?

Before building an OI signal into the strategy, prove it has value -- the discipline
the session is built on. Bitget has no public OI-history endpoint, so we use Binance
futures OI history as a PROXY (the divergence sign -- is OI confirming the move? --
transfers across venues even if the absolute number doesn't).

Method: run the backtest (replay.simulate), then for each trade look at OI change
over the trailing window at entry and bucket trades into OI-CONFIRMED (OI rising into
the entry = new money behind the move) vs OI-DIVERGED (OI falling = short-covering /
liquidation, likely to fade). If confirmed trades have materially higher expectancy,
OI is worth wiring into scoring; if not, we don't build it.

Usage: python3 research/oi_signal.py --days 18 --window 6
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay      # noqa: E402
import analytics   # noqa: E402

BINANCE = "https://fapi.binance.com/futures/data/openInterestHist"


def fetch_oi(symbol, period="1h", limit=500):
    """Binance OI history -> [(ts_ms, oi_usd), ...] oldest first. [] on failure."""
    url = f"{BINANCE}?symbol={symbol}&period={period}&limit={limit}"
    try:
        out = subprocess.run(["curl", "-s", "--max-time", "30", url],
                             capture_output=True, text=True, timeout=40).stdout
        rows = json.loads(out)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    pts = []
    for r in rows:
        try:
            pts.append((int(r["timestamp"]), float(r["sumOpenInterestValue"])))
        except Exception:
            continue
    pts.sort()
    return pts


def _oi_at(pts, ts):
    """Last OI value at or before ts (None if no point covers it)."""
    v = None
    for t, o in pts:
        if t <= ts:
            v = o
        else:
            break
    return v


def oi_change(pts, ts, window_h):
    """OI % change over the trailing window_h hours ending at ts. None if unavailable."""
    now = _oi_at(pts, ts)
    then = _oi_at(pts, ts - window_h * 3_600_000)
    if now is None or then is None or then <= 0:
        return None
    return (now - then) / then * 100.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=18)
    ap.add_argument("--window", type=int, default=6, help="trailing hours for OI trend")
    ap.add_argument("--breakout", action="store_true", default=True)
    ap.add_argument("--symbols", nargs="*", default=[
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT",
        "ADAUSDT", "LINKUSDT", "AVAXUSDT", "NEARUSDT", "INJUSDT", "SEIUSDT"])
    a = ap.parse_args()
    cfg = {"min_score": 70, "min_volume_usdt": "0", "max_vwap_ext_pct": "4.0",
           "atr_limit_mult": "0.5", "tp1_pct": "3.5", "tp2_pct": "7.0", "trail_atr_mult": "2.0",
           "breakeven_pct": "2.0", "sl_min_btc_eth_pct": "1.5", "sl_min_sol_bnb_pct": "1.2",
           "sl_min_alt_pct": "2.5", "max_loss_usdt": "15", "leverage": 10, "margin_budget": "100",
           "limit_expiry_hours": "4", "limit_chase_pct": "3.0", "time_stop_hours": "12",
           "atr_period": 14, "trend_lookback": 12, "trend_norm": "0.05", "trend_weight": "15.0",
           "breakout_trend_min": "0.6", "breakout_extreme_band": "0.015", "breakout_stop_atr_mult": "1.0",
           "breakout_level_buffer_pct": "0.2", "breakout_tp1_pct": "4.0", "allow_short": True,
           "funding_skip_bps": "30", "funding_penalty_weight": "8.0",
           "exit_mode": "fixed", "fee_pct": "0.06"}
    data = replay.fetch_all(a.symbols, a.days)
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = replay.simulate(cfg, a.symbols, a.days, a.breakout, data=data)
    trades = (res or {}).get("trades", [])
    print(f"backtest: {len(trades)} trades")

    print("fetching OI history (Binance proxy)...")
    oi = {}
    for s in set(t["sym"] for t in trades):
        pts = fetch_oi(s)
        if pts:
            oi[s] = pts
    print(f"OI history available for {len(oi)}/{len(set(t['sym'] for t in trades))} symbols")

    confirmed, diverged, nodata = [], [], []
    for t in trades:
        pts = oi.get(t["sym"])
        ch = oi_change(pts, t["fill_ts"], a.window) if pts else None
        if ch is None:
            nodata.append(t)
        elif ch > 0:
            confirmed.append(t)   # OI rising into entry = new money behind the move
        else:
            diverged.append(t)    # OI falling = covering/liquidation, likely to fade

    print("\n" + "=" * 60)
    print(f"OI VALUE TEST (trailing {a.window}h, days={a.days})")
    print("=" * 60)
    for name, grp in (("OI-CONFIRMED (rising)", confirmed),
                      ("OI-DIVERGED  (falling)", diverged),
                      ("no OI data", nodata)):
        if not grp:
            print(f"  {name:24}: 0 trades")
            continue
        ag = analytics.analyze(grp)
        print(f"  {name:24}: {ag['n_trades']:3} trades  win {ag['win_rate']:4.0f}%  "
              f"exp {ag['expectancy_pct']:+.3f}%  total {ag['total_pct']:+.1f}%")
    print("=" * 60)
    if confirmed and diverged:
        ce = analytics.analyze(confirmed)["expectancy_pct"]
        de = analytics.analyze(diverged)["expectancy_pct"]
        gap = ce - de
        print(f"VERDICT: confirmed - diverged expectancy gap = {gap:+.3f}%/trade")
        print("  -> BUILD IT" if gap > 0.25 else "  -> WEAK/NO separation -- do NOT build (no validated edge)")


if __name__ == "__main__":
    main()
