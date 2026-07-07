"""Live-vs-replay reconciliation (research-only, zero-dependency).

The missing feedback loop from DESIGN_v0.9.1: compare what the LIVE engine
actually realized (exchange fills) against what the replay harness would have
expected over the same window. Until v0.9.27 this was impossible -- the live
fills journal was timestamp-blind (the -b?t saga); with fills now parseable the
loop closes. The operator pastes fills (or exports them) into a small CSV and
this prints both sides in comparable units.

Input CSV (header required, one closed trade per row):
    symbol,side,entry,exit,pnl,closed_utc
    ETHUSDT,short,1779.28,1791.52,-6.365,2026-07-05T22:00
    ETHUSDT,long,1771.61,1777.41,3.248,2026-07-06T04:45

Usage:
    python3 research/reconcile.py fills.csv --days 7
    python3 research/reconcile.py fills.csv --days 7 --notional 600

The replay side runs research/replay_mp.py with the LIVE baseline over the
covering window and reports ret-units; live PnL is normalized to ret-units via
--notional (the full-size slot, default $600) so the two columns are
comparable. HONESTY CONTRACT: the replay is an approximate 3-slot ranking
tool, not a P&L promise (its own docstring) -- reconciliation flags DIVERGENCE
CLASSES (live win% far below replay's, systematic slippage, a symbol class
bleeding live that replay loves), not penny differences.
"""
import argparse
import csv
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LIVE_SYMS = ("BTCUSDT ETHUSDT SOLUSDT LABUSDT ZECUSDT XRPUSDT DOGEUSDT TAOUSDT "
             "HYPEUSDT BNBUSDT SUIUSDT ADAUSDT LINKUSDT ENAUSDT ONDOUSDT BCHUSDT "
             "AVAXUSDT NEARUSDT AAVEUSDT WLDUSDT XPLUSDT XLMUSDT TRUMPUSDT MUSDT "
             "INJUSDT SEIUSDT PEPEUSDT SHIBUSDT").split()
LIVE_BASE = ("--exit-mode trail --trail 2.0 --time-stop 12 --be-lock 1.5 "
             "--steplock 2:1.5,4:3,6:4.5 --scaleout 0.35").split()
LIVE_SET = ("tp2_pct=20,breakout_trend_min=0.7,max_vwap_ext_pct=5.0,"
            "pullback_time_stop_hours=4,pullback_tp2_pct=22,"
            "loss_pause_pct=3,regime_chg_deadzone_pct=0.3")  # joint baseline since v0.9.32


def read_fills(path):
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                rows.append({
                    "symbol": r["symbol"].strip().upper(),
                    "side": r["side"].strip().lower(),
                    "entry": float(r["entry"]), "exit": float(r["exit"]),
                    "pnl": float(r["pnl"]),
                    "closed": r.get("closed_utc", "").strip(),
                })
            except (KeyError, TypeError, ValueError) as exc:
                print(f"  ! skipping row {r}: {exc}")
    return rows


def live_summary(fills, notional):
    n = len(fills)
    wins = sum(1 for f in fills if f["pnl"] > 0)
    net_usd = sum(f["pnl"] for f in fills)
    net_ret = 100.0 * net_usd / notional if notional else 0.0
    per_sym = {}
    for f in fills:
        s = per_sym.setdefault(f["symbol"], {"n": 0, "usd": 0.0})
        s["n"] += 1
        s["usd"] += f["pnl"]
    return {"n": n, "win_pct": (100.0 * wins / n if n else 0.0),
            "net_usd": net_usd, "net_ret": net_ret, "per_sym": per_sym}


def replay_summary(days):
    cmd = ([sys.executable, str(Path(__file__).with_name("replay_mp.py")),
            "--days", str(days), "--breakout", "--symbols", *LIVE_SYMS,
            *LIVE_BASE, "--set", LIVE_SET])
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=900).stdout
    keep = [ln for ln in out.splitlines()
            if ln.startswith(("  pullback", "  breakout", "  ALL", "  tail"))]
    return "\n".join(keep) or "(replay produced no summary -- see raw output)"


def main():
    ap = argparse.ArgumentParser(description="Live-vs-replay reconciliation.")
    ap.add_argument("fills_csv")
    ap.add_argument("--days", type=int, default=7,
                    help="replay window covering the fills (default 7)")
    ap.add_argument("--notional", type=float, default=600.0,
                    help="full-size slot notional for USD->ret-unit conversion")
    a = ap.parse_args()

    fills = read_fills(a.fills_csv)
    live = live_summary(fills, a.notional)
    print(f"=== LIVE (from {a.fills_csv}: {live['n']} closed trades) ===")
    print(f"  win%     : {live['win_pct']:.0f}%")
    print(f"  net      : {live['net_usd']:+.2f} USDT  (~{live['net_ret']:+.2f} ret-units"
          f" at ${a.notional:.0f}/slot)")
    for sym, s in sorted(live["per_sym"].items()):
        print(f"    {sym:<10} n={s['n']}  {s['usd']:+.2f} USDT")
    print(f"\n=== REPLAY (same live baseline, {a.days}d window) ===")
    print(replay_summary(a.days))
    print("\nRead DIVERGENCE CLASSES, not pennies: live win% far under replay's;"
          "\nper-symbol bleed replay loves; systematic entry/exit slippage."
          "\nThe replay is a ranking tool, not a P&L promise.")


if __name__ == "__main__":
    main()
