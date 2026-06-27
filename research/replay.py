#!/usr/bin/env python3
"""RUNECLAW offline research replay (APPROXIMATE -- not publishable evidence).

Why this exists: RUNECLAW cannot use the official getagent backtest engine. That
engine requires a NautilusTrader Strategy subclass, and RUNECLAW's score blends
live order-book imbalance (bid/ask resting size) which historical OHLCV cannot
reconstruct. So `backtest_support` stays `none`; this is a *research* tool for our
own iteration, deliberately outside the package (research/, not src/**).

What it does: pulls public Bitget 1h/4h klines + funding, reconstructs a
SymbolFeatures per bar from the trailing 24h, and runs the REAL engine modules
(scoring.score_universe / enrich_score, risk.build_plan) -- so the decision logic
never drifts from live. The single unreconstructable dimension, order-book
imbalance, degrades to the engine's own documented fallback (orderbook_score=8.0,
bid/ask volume = None). Then it simulates pullback fills (limit must be touched
within limit_expiry_hours, else chase-cancel if price ran > limit_chase_pct) and
exits (SL / TP1 / time-stop) bar by bar, and reports aggregate stats.

LIMITATIONS (read before trusting a number):
- no order-book dimension (degraded) -> scores differ slightly from live
- VWAP approximated as 24h close*volume / volume (engine uses the ticker VWAP)
- TREND is reconstructed on 1h bars here, but live enrich uses 4h
  (trend_interval). The 1h gap-from-EMA is smaller, so this replay systematically
  UNDER-fires breakouts -- a 0/low breakout count here is partly a fidelity
  artifact, NOT proof the gate never triggers live. (Diagnostic over volatile
  movers: breakout ROUTING works -- 1201/1201 over-extended names routed -- but
  0 passed trend_strength>=0.6. Re-check breakout trigger rate in live
  signal_only before concluding the gate is mis-tuned.)
- fills are bar-touch approximations (no intrabar path, no partial fills, no fees
  unless --fee set); breakout entries fill at the bar close
- one position at a time per run (no portfolio concurrency / correlation caps)
This is a directional sanity-check, not a P&L promise. Its one robust output is
the chase-cancel RATE (pullback signals that never fill): ~69% across 20d of
majors -- the live structural miss, quantified.

Usage:
  python3 research/replay.py --days 30 --symbols BTCUSDT ETHUSDT SOLUSDT ...
  python3 research/replay.py --days 30 --breakout      # compare with breakout on
"""
import argparse
import json
import subprocess
import sys
import types
from pathlib import Path

# --- import the REAL engine modules (stub getagent; they import it at module load) ---
_g = types.ModuleType("getagent"); sys.modules["getagent"] = _g
for _sub in ("data", "trade", "runtime"):
    _m = types.ModuleType("getagent." + _sub); setattr(_g, _sub, _m); sys.modules["getagent." + _sub] = _m
_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC.parent))
import importlib.util
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod
    spec.loader.exec_module(mod); return mod
_pkg = types.ModuleType("src"); _pkg.__path__ = [str(_SRC)]; sys.modules["src"] = _pkg
features = _load("src.features", _SRC / "features.py")
scoring = _load("src.scoring", _SRC / "scoring.py")
risk = _load("src.risk", _SRC / "risk.py")
SF = features.SymbolFeatures

BG = "https://api.bitget.com/api/v2/mix/market/candles"


def fetch_klines(symbol, granularity="1H", limit=1000):
    """Public Bitget mix candles -> list of [ts, o, h, l, c, baseVol, quoteVol] (oldest first)."""
    url = f"{BG}?symbol={symbol}&productType=usdt-futures&granularity={granularity}&limit={limit}"
    try:
        out = subprocess.run(["curl", "-s", "--max-time", "30", url], capture_output=True, text=True, timeout=40).stdout
        rows = json.loads(out).get("data") or []
    except Exception as e:
        print(f"  ! fetch {symbol} failed: {e}"); return []
    bars = []
    for r in rows:
        try:
            bars.append([int(r[0])] + [float(x) for x in r[1:7]])
        except Exception:
            continue
    bars.sort(key=lambda b: b[0])
    return bars


def recon_features(symbol, win):
    """Reconstruct a SymbolFeatures from a trailing 24-bar (1h) window. Order-book
    fields stay None -> engine uses its degraded orderbook fallback."""
    if len(win) < 25:
        return SF(symbol=symbol, ok=False, note="warmup")
    last = win[-1][4]
    highs = [b[2] for b in win]; lows = [b[3] for b in win]
    closes = [b[4] for b in win]; bvols = [b[5] for b in win]; qvols = [b[6] for b in win]
    vwap = (sum(c * v for c, v in zip(closes, bvols)) / sum(bvols)) if sum(bvols) > 0 else last
    chg = ((last - closes[0]) / closes[0] * 100.0) if closes[0] else 0.0
    return SF(symbol=symbol, ok=True, last=last, vwap=vwap, high=max(highs), low=min(lows),
              change_pct=chg, quote_volume=sum(qvols), bid_volume=None, ask_volume=None)


def _dictify(bars):
    """[ts,o,h,l,c,bv,qv] -> dict bars with the keys _wilder_atr/_ema_trend expect
    (they use _bar_f on dict keys; raw list bars silently yield None/neutral)."""
    return [{"timestamp": b[0], "open": b[1], "high": b[2], "low": b[3],
             "close": b[4], "baseVolume": b[5], "quoteVolume": b[6]} for b in bars]


def trend_4h(kl4, ts_ms, lookback, norm):
    """EMA trend on the trailing 4h bars up to ts_ms -- matches live trend_interval
    (the replay base is 1h, but enrich computes trend on 4h)."""
    win = [b for b in kl4 if b[0] <= ts_ms][-(lookback + 5):]
    return features._ema_trend(_dictify(win), lookback, norm)


def fetch_all(symbols, days):
    """Fetch 1h+4h klines once; reuse across a parameter sweep."""
    leader = "BTCUSDT"
    need = days * 24 + 30
    allsyms = set(symbols + [leader])
    print(f"fetching {len(allsyms)} symbols x ~{need} 1h + 4h bars ...")
    kl = {s: fetch_klines(s, "1H", min(need, 1000)) for s in allsyms}
    kl4 = {s: fetch_klines(s, "4H", 400) for s in allsyms}
    return {s: b for s, b in kl.items() if len(b) >= 25}, kl4


def simulate(cfg, symbols, days, use_breakout, data=None):
    leader = "BTCUSDT"
    kl, kl4 = data if data is not None else fetch_all(symbols, days)
    if leader not in kl:
        print("no leader data; abort"); return
    syms = [s for s in symbols if s in kl and s != leader]
    n = min(len(kl[leader]), *(len(kl[s]) for s in syms)) if syms else 0
    if n < 30:
        print("insufficient overlapping history; abort"); return

    cfg = dict(cfg); cfg["breakout_enabled"] = use_breakout
    expiry = int(float(cfg.get("limit_expiry_hours", "4")))
    chase = float(cfg.get("limit_chase_pct", "3.0")) / 100.0
    tstop = int(float(cfg.get("time_stop_hours", "4")))

    trades = []; n_sig = 0; n_chase = 0; in_trade_until = -1
    for i in range(25, n - 1):
        if i < in_trade_until:
            continue
        # reconstruct the board at bar i
        lead = recon_features(leader, kl[leader][i - 24:i + 1])
        reg = scoring.regime(lead, None, cfg)
        if reg.direction not in ("long", "short"):
            continue
        feats = [recon_features(s, kl[s][i - 24:i + 1]) for s in syms]
        scored = scoring.score_universe(feats, lead, cfg, reg.direction,
                                        allow_breakout=use_breakout)
        qual = [s for s in scored if not s.skip and s.score >= float(cfg.get("min_score", 70))]
        if not qual:
            continue
        best = qual[0]
        # cheap enrich: real Wilder ATR + EMA trend from the same klines
        bars1h = _dictify(kl[best.symbol][max(0, i - 30):i + 1])
        best.features.atr = features._wilder_atr(bars1h, int(cfg.get("atr_period", 14)))
        best.features.kline_ok = best.features.atr is not None
        td, ts = trend_4h(kl4.get(best.symbol, []), kl[best.symbol][i][0],
                          int(cfg.get("trend_lookback", 12)), float(cfg.get("trend_norm", "0.05")))
        best.features.trend_dir, best.features.trend_strength = td, ts
        _, extra, eskip, ereason = scoring.enrich_score(best, best.features, cfg)
        if eskip:
            continue
        plan = risk.build_plan(best.features, cfg, best.size_factor, side=best.side,
                               entry_mode=best.entry_mode)
        if plan is None or not plan.sizing_ok:
            continue
        n_sig += 1
        long = plan.side == "long"
        fwd = kl[best.symbol]

        # --- entry fill ---
        if plan.entry_mode == "breakout":
            fill_i, fill_px = i, fwd[i][4]  # market at bar close
        else:
            fill_i = fill_px = None
            for j in range(i + 1, min(i + 1 + expiry, n)):
                lo, hi = fwd[j][3], fwd[j][2]
                # chase-cancel: price ran limit_chase_pct the un-fillable way
                run = ((fwd[j][4] - plan.entry) / plan.entry) if long else ((plan.entry - fwd[j][4]) / plan.entry)
                if (lo <= plan.entry <= hi):
                    fill_i, fill_px = j, plan.entry; break
                if run > chase:
                    n_chase += 1; break
            if fill_i is None:
                continue  # expired or chased, no trade

        # --- exit: SL / TP1 / time-stop, bar by bar ---
        exit_px = exit_reason = None
        for j in range(fill_i + 1, min(fill_i + 1 + tstop, n)):
            lo, hi = fwd[j][3], fwd[j][2]
            if long:
                if lo <= plan.sl_price: exit_px, exit_reason = plan.sl_price, "sl"; break
                if hi >= plan.tp1: exit_px, exit_reason = plan.tp1, "tp1"; break
            else:
                if hi >= plan.sl_price: exit_px, exit_reason = plan.sl_price, "sl"; break
                if lo <= plan.tp1: exit_px, exit_reason = plan.tp1, "tp1"; break
        if exit_px is None:
            j = min(fill_i + tstop, n - 1); exit_px, exit_reason = fwd[j][4], "time_stop"
        in_trade_until = j
        r = ((exit_px - fill_px) / fill_px) if long else ((fill_px - exit_px) / fill_px)
        fee = float(cfg.get("fee_pct", "0")) / 100.0  # round-turn taker fee, % of notional
        trades.append({"sym": best.symbol, "side": plan.side, "mode": plan.entry_mode,
                       "ret_pct": round((r - 2 * fee) * 100, 3), "reason": exit_reason})

    report(use_breakout, n_sig, n_chase, trades)
    return {"n_sig": n_sig, "n_chase": n_chase, "trades": trades}


def report(use_breakout, n_sig, n_chase, trades):
    print("\n" + "=" * 56)
    print(f"REPLAY  breakout={'ON' if use_breakout else 'off'}")
    print(f"  signals attempted : {n_sig}")
    print(f"  chase-cancelled   : {n_chase}")
    print(f"  trades taken      : {len(trades)}")
    if trades:
        wins = [t for t in trades if t["ret_pct"] > 0]
        tot = sum(t["ret_pct"] for t in trades)
        bk = [t for t in trades if t["mode"] == "breakout"]
        print(f"  win rate          : {len(wins)}/{len(trades)} = {len(wins)/len(trades)*100:.0f}%")
        print(f"  sum ret (pre-fee) : {tot:+.2f}%  | avg {tot/len(trades):+.3f}%/trade")
        print(f"  breakout entries  : {len(bk)} ({sum(1 for t in bk if t['ret_pct']>0)} win)")
        by = {}
        for t in trades:
            by[t["reason"]] = by.get(t["reason"], 0) + 1
        print(f"  exits by reason   : {by}")
    print("=" * 56)
    print("NOTE: approximate research replay -- no orderbook dim, bar-touch fills,\n"
          "      single-position, no fees unless modeled. Directional only.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--breakout", action="store_true", help="enable the breakout path")
    ap.add_argument("--symbols", nargs="*", default=[
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT",
        "ADAUSDT", "LINKUSDT", "AVAXUSDT", "NEARUSDT", "INJUSDT", "SEIUSDT"])
    a = ap.parse_args()
    cfg = {  # mirrors the manifest strategy_config defaults that matter for replay
        "min_score": 70, "min_volume_usdt": "0", "max_vwap_ext_pct": "4.0",
        "atr_limit_mult": "0.5", "tp1_pct": "3.5", "tp2_pct": "7.0", "trail_atr_mult": "1.0",
        "breakeven_pct": "2.0", "sl_min_btc_eth_pct": "1.5", "sl_min_sol_bnb_pct": "1.2",
        "sl_min_alt_pct": "2.5", "max_loss_usdt": "15", "leverage": 10, "margin_budget": "100",
        "limit_expiry_hours": "4", "limit_chase_pct": "3.0", "time_stop_hours": "4",
        "atr_period": 14, "trend_lookback": 12, "trend_norm": "0.05", "trend_weight": "15.0",
        "breakout_trend_min": "0.6", "breakout_extreme_band": "0.015", "breakout_stop_atr_mult": "1.0",
        "breakout_level_buffer_pct": "0.2", "breakout_tp1_pct": "4.0", "allow_short": True,
        "funding_skip_bps": "30", "funding_penalty_weight": "8.0",
    }
    simulate(cfg, a.symbols, a.days, a.breakout)


if __name__ == "__main__":
    main()
