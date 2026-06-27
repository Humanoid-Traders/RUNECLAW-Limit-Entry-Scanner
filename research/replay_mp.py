#!/usr/bin/env python3
"""RUNECLAW multi-position research replay (APPROXIMATE -- see replay.py limits).

The single-position replay (replay.py) confounded time_stop_hours and the
breakout-exit question: holding one trade at a time means a longer hold or a
trailing exit just starves throughput, so the numbers don't reflect live, which
runs up to max_concurrent positions under the correlation budget.

This sim carries a portfolio: up to max_concurrent commitments (open positions +
resting pullback limits), gated by the same Rule-7 correlation budget as
execution.open_if_allowed (every open name counts as BTC-correlated; tighten to 1
slot when BTC/ETH is held). Each bar it (1) manages open positions
(SL/TP1/time-stop, or a ratcheting trail if exit_mode=trail), (2) fills/chases/
expires resting limits, (3) places one new entry if a slot + the corr budget
allow. Reuses the REAL engine modules via replay.py -> no logic drift.

Same limitations as replay.py (no order book, bar-touch fills, fee modeled). A
ranking tool at live concurrency, not a P&L promise.

Usage:
  python3 research/replay_mp.py --days 30 --breakout
  python3 research/replay_mp.py --days 30 --breakout --exit-mode trail \
      --trail 2.0 --time-stop 12     # test the breakout trail at 3-slot concurrency
"""
import argparse
import replay as R

features, scoring, risk = R.features, R.scoring, R.risk


def _enrich(best, kl, kl4, i, cfg):
    b1 = R._dictify(kl[best.symbol][max(0, i - 30):i + 1])
    best.features.atr = features._wilder_atr(b1, int(cfg.get("atr_period", 14)))
    best.features.kline_ok = best.features.atr is not None
    td, ts = R.trend_4h(kl4.get(best.symbol, []), kl[best.symbol][i][0],
                        int(cfg.get("trend_lookback", 12)), float(cfg.get("trend_norm", "0.05")))
    best.features.trend_dir, best.features.trend_strength = td, ts
    _, _, skip, reason = scoring.enrich_score(best, best.features, cfg)
    return skip


def simulate_mp(cfg, symbols, days, use_breakout, data=None):
    leader = "BTCUSDT"
    kl, kl4 = data if data is not None else R.fetch_all(symbols, days)
    if leader not in kl:
        print("no leader data"); return None
    syms = [s for s in symbols if s in kl and s != leader]
    n = min(len(kl[leader]), *(len(kl[s]) for s in syms)) if syms else 0
    if n < 40:
        print("insufficient history"); return None

    cfg = dict(cfg); cfg["breakout_enabled"] = use_breakout
    max_conc = int(cfg.get("max_concurrent", 3))
    max_corr_base = int(cfg.get("max_correlated_alts", 2))
    expiry = int(float(cfg.get("limit_expiry_hours", "4")))
    chase = float(cfg.get("limit_chase_pct", "3.0")) / 100.0
    tstop = int(float(cfg.get("time_stop_hours", "4")))
    min_score = float(cfg.get("min_score", 70))
    exit_mode = str(cfg.get("exit_mode", "fixed"))
    tmult = float(cfg.get("trail_atr_mult", "1.0"))
    fee = float(cfg.get("fee_pct", "0")) / 100.0

    opens = []   # {sym, side, mode, fill_px, sl, tp1, atr, fill_i, hw, trail}
    pends = []   # {sym, side, entry, placed_i, plan}
    trades = []; n_sig = 0; n_chase = 0; n_expire = 0; n_fill = 0

    def close(p, px, why):
        long = p["side"] == "long"
        r = ((px - p["fill_px"]) / p["fill_px"]) if long else ((p["fill_px"] - px) / p["fill_px"])
        trades.append({"sym": p["sym"], "side": p["side"], "mode": p["mode"],
                       "ret_pct": round((r - 2 * fee) * 100, 3), "reason": why})

    for i in range(25, n - 1):
        # 1) manage open positions
        keep = []
        for p in opens:
            b = kl[p["sym"]][i]; lo, hi, c = b[3], b[2], b[4]
            long = p["side"] == "long"; ex = None
            if exit_mode == "trail":
                # v0.6.3 live-faithful: a wide TP2 backstop is attached at entry,
                # the ratcheting trail does the rest.
                if long:
                    if lo <= p["trail"]:
                        ex = (p["trail"], "trail" if p["trail"] > p["sl"] else "sl")
                    elif hi >= p["tp2"]:
                        ex = (p["tp2"], "tp2")
                    else:
                        p["hw"] = max(p["hw"], hi); p["trail"] = max(p["trail"], p["hw"] - tmult * p["atr"])
                else:
                    if hi >= p["trail"]:
                        ex = (p["trail"], "trail" if p["trail"] < p["sl"] else "sl")
                    elif lo <= p["tp2"]:
                        ex = (p["tp2"], "tp2")
                    else:
                        p["hw"] = min(p["hw"], lo); p["trail"] = min(p["trail"], p["hw"] + tmult * p["atr"])
            else:
                if long:
                    if lo <= p["sl"]: ex = (p["sl"], "sl")
                    elif hi >= p["tp1"]: ex = (p["tp1"], "tp1")
                else:
                    if hi >= p["sl"]: ex = (p["sl"], "sl")
                    elif lo <= p["tp1"]: ex = (p["tp1"], "tp1")
            if ex is None and (i - p["fill_i"]) >= tstop:
                ex = (c, "time_stop")
            if ex:
                close(p, ex[0], ex[1])
            else:
                keep.append(p)
        opens = keep

        # 2) manage resting pullback limits: fill / chase / expire
        keepp = []
        for q in pends:
            b = kl[q["sym"]][i]; lo, hi, c = b[3], b[2], b[4]
            long = q["side"] == "long"
            if lo <= q["entry"] <= hi:
                pl = q["plan"]; n_fill += 1
                opens.append({"sym": q["sym"], "side": q["side"], "mode": "pullback",
                              "fill_px": q["entry"], "sl": pl.sl_price, "tp1": pl.tp1, "tp2": pl.tp2,
                              "atr": pl.atr or 0.0, "fill_i": i, "hw": q["entry"], "trail": pl.sl_price})
                continue
            run = ((c - q["entry"]) / q["entry"]) if long else ((q["entry"] - c) / q["entry"])
            if run > chase: n_chase += 1; continue
            if (i - q["placed_i"]) >= expiry: n_expire += 1; continue
            keepp.append(q)
        pends = keepp

        # 3) consider a new entry under the slot + correlation caps
        owned = [p["sym"] for p in opens] + [q["sym"] for q in pends]
        if len(owned) >= max_conc:
            continue
        max_corr = max_corr_base
        if any(s in ("BTCUSDT", "ETHUSDT") for s in owned):
            max_corr = min(max_corr, 1)
        if len(set(owned)) >= max_corr:
            continue
        lead = R.recon_features(leader, kl[leader][i - 24:i + 1])
        reg = scoring.regime(lead, None, cfg)
        if reg.direction not in ("long", "short"):
            continue
        feats = [R.recon_features(s, kl[s][i - 24:i + 1]) for s in syms if s not in owned]
        scored = scoring.score_universe(feats, lead, cfg, reg.direction, allow_breakout=use_breakout)
        qual = [s for s in scored if not s.skip and s.score >= min_score]
        if not qual:
            continue
        best = qual[0]
        if _enrich(best, kl, kl4, i, cfg):
            continue
        # AlphaAgent-style vol-regime gate: stand aside outside [vol_floor, vol_ceiling]
        vlo = float(cfg.get("vol_floor", "0")); vhi = float(cfg.get("vol_ceiling", "99999"))
        if vlo > 0 or vhi < 99999:
            rv = R.realized_vol(kl[best.symbol][max(0, i - 31):i + 1], int(cfg.get("vol_lookback", 30)))
            if rv is not None and (rv < vlo or rv > vhi):
                continue
        plan = risk.build_plan(best.features, cfg, best.size_factor, side=best.side, entry_mode=best.entry_mode)
        if plan is None or not plan.sizing_ok:
            continue
        n_sig += 1
        if plan.entry_mode == "breakout":
            opens.append({"sym": best.symbol, "side": best.side, "mode": "breakout",
                          "fill_px": best.features.last, "sl": plan.sl_price, "tp1": plan.tp1, "tp2": plan.tp2,
                          "atr": plan.atr or 0.0, "fill_i": i, "hw": best.features.last, "trail": plan.sl_price})
        else:
            cur = best.features.last
            gap = ((plan.entry - cur) / plan.entry) if best.side == "short" else ((cur - plan.entry) / plan.entry)
            if gap > chase:
                continue  # pre-placement staleness skip (entry_too_far)
            pends.append({"sym": best.symbol, "side": best.side, "entry": plan.entry,
                          "placed_i": i, "plan": plan})

    # force-close any still-open positions at the last bar so stats are unbiased
    last = n - 1
    for p in opens:
        close(p, kl[p["sym"]][last][4], "eow")

    return {"n_sig": n_sig, "n_fill": n_fill, "n_chase": n_chase, "n_expire": n_expire,
            "trades": trades, "max_open": max_conc}


def report(tag, st):
    t = st["trades"]
    print(f"\n===== MULTI-POSITION  {tag} =====")
    print(f"  entries placed  : {st['n_sig']}   pullback fills: {st.get('n_fill','?')}   "
          f"chase: {st['n_chase']}   expire: {st['n_expire']}")
    print(f"  trades closed   : {len(t)}  (incl. forced end-of-window closes)")
    if not t:
        return
    for mode in ("pullback", "breakout", "ALL"):
        sub = t if mode == "ALL" else [x for x in t if x["mode"] == mode]
        if not sub:
            continue
        w = sum(1 for x in sub if x["ret_pct"] > 0)
        net = sum(x["ret_pct"] for x in sub)
        print(f"  {mode:9}: {len(sub):3} trades  win {w/len(sub)*100:3.0f}%  net {net:+7.1f}%  avg {net/len(sub):+.2f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--breakout", action="store_true")
    ap.add_argument("--exit-mode", default="fixed", choices=["fixed", "trail"])
    ap.add_argument("--trail", default="1.0")
    ap.add_argument("--time-stop", default="4")
    ap.add_argument("--symbols", nargs="*", default=[
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT",
        "LINKUSDT", "AVAXUSDT", "NEARUSDT", "INJUSDT", "SEIUSDT", "TRUMPUSDT", "WLDUSDT"])
    a = ap.parse_args()
    cfg = {
        "min_score": 70, "min_volume_usdt": "0", "max_vwap_ext_pct": "4.0",
        "atr_limit_mult": "0.3", "tp1_pct": "5.0", "tp2_pct": "7.0", "trail_atr_mult": a.trail,
        "breakeven_pct": "2.0", "sl_min_btc_eth_pct": "1.5", "sl_min_sol_bnb_pct": "1.2",
        "sl_min_alt_pct": "2.5", "max_loss_usdt": "15", "leverage": 10, "margin_budget": "100",
        "limit_expiry_hours": "4", "limit_chase_pct": "3.0", "time_stop_hours": a.time_stop,
        "atr_period": 14, "trend_lookback": 12, "trend_norm": "0.05", "trend_weight": "15.0",
        "breakout_trend_min": "0.6", "breakout_extreme_band": "0.015", "breakout_stop_atr_mult": "1.0",
        "breakout_level_buffer_pct": "0.2", "breakout_tp1_pct": "4.0", "allow_short": True,
        "max_concurrent": 3, "max_correlated_alts": 2,
        "funding_skip_bps": "30", "funding_penalty_weight": "8.0", "fee_pct": "0.04",
        "exit_mode": a.exit_mode,
    }
    data = R.fetch_all(a.symbols, a.days)
    st = simulate_mp(cfg, a.symbols, a.days, a.breakout, data=data)
    if st:
        report(f"breakout={'ON' if a.breakout else 'off'} exit={a.exit_mode} "
               f"trail={a.trail} ts={a.time_stop} ({len(a.symbols)}syms {a.days}d, 3-slot)", st)
    print("\nNOTE: approximate multi-position replay -- ranking tool, not P&L promise.")


if __name__ == "__main__":
    main()
