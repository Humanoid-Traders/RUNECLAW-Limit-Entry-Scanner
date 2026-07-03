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

v0.8.0 adds the portfolio-risk value-tests (see docs/DESIGN_v0.8.0_portfolio_risk.md):
tail metrics (maxDD / worst-trade / PF on the exit-ordered curve) and three A/B
sweeps -- correlation-weighted exposure (--ab-corr), concurrent-heat breaker
(--ab-heat), and the validated realized rolling-loss breaker (--ab-loss).

Usage:
  python3 research/replay_mp.py --days 30 --breakout
  python3 research/replay_mp.py --days 30 --breakout --exit-mode trail \
      --trail 2.0 --time-stop 12     # test the breakout trail at 3-slot concurrency
  python3 research/replay_mp.py --days 35 --breakout --exit-mode trail --trail 2.0 \
      --time-stop 12 --ab-loss --loss-window 24   # the validated drawdown breaker A/B
"""
import argparse
import replay as R

features, scoring, risk = R.features, R.scoring, R.risk


# --- v0.8.0 correlation-weighted exposure budget (research model) -------------
# The live Rule-7 cap treats every alt as fully BTC-correlated (count cap of 2,
# tighten to 1 if BTC/ETH held). That is crude in two ways: it blocks genuinely
# decorrelated diversifiers, and it does NOT tighten when correlations actually
# spike toward 1 -- the crash-candle regime where 3 same-side names become one
# trade and the left tail lives. This model replaces the raw count with a
# correlation-WEIGHTED same-side exposure: highly-correlated names cost more
# budget, opposite-side names earn a hedge credit. When all corr~=1 it reproduces
# the count cap (safe floor); when corr spikes it auto-tightens (the point).
_CORR_PRIOR = 0.85          # assume crypto names ~0.85 corr when history is thin
_MIN_RET_PTS = 6


def _closes(kl, sym, i, look):
    s = kl.get(sym)
    if not s:
        return None
    seg = s[max(0, i - look):i + 1]
    return [b[4] for b in seg] if len(seg) >= 8 else None


def _rets(cl):
    return [cl[k] / cl[k - 1] - 1.0 for k in range(1, len(cl)) if cl[k - 1] > 0]


def _corr(a, b):
    n = min(len(a), len(b))
    if n < _MIN_RET_PTS:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((a[k] - ma) * (b[k] - mb) for k in range(n))
    return cov / (va ** 0.5 * vb ** 0.5)


def _pair_corr(kl, s1, s2, i, look):
    """Rolling return correlation of two symbols over the trailing `look` bars."""
    c1, c2 = _closes(kl, s1, i, look), _closes(kl, s2, i, look)
    if not c1 or not c2:
        return None
    return _corr(_rets(c1), _rets(c2))


def _corr_exposure(kl, cand, cside, held, i, look, hedge_credit):
    """Correlation-weighted same-side exposure of opening `cand` (side `cside`)
    given already-held (sym, side) commitments. Same-side names add |corr|,
    opposite-side names subtract hedge_credit*|corr|. Returns the budget cost."""
    cost = 0.0
    for hsym, hside in held:
        if hsym == cand:
            continue
        c = _pair_corr(kl, cand, hsym, i, look)
        if c is None:
            c = _CORR_PRIOR
        c = max(0.0, c)
        cost += c if hside == cside else -hedge_credit * c
    return cost


def _efficiency_ratio(kl, sym, i, look):
    """Kaufman efficiency ratio of `sym` over the trailing `look` bars:
    |net move| / sum(|bar-to-bar move|). ~1.0 = clean directional trend,
    ~0.0 = chop/range (price goes nowhere relative to its own wiggle). One
    number that separates BOTH no-trade regimes (range + high-vol chop) from
    tradable trends -- the leader-level gate Part 3's regime table calls for."""
    s = kl.get(sym)
    if not s:
        return None
    seg = s[max(0, i - look):i + 1]
    if len(seg) < 6:
        return None
    closes = [b[4] for b in seg]
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[k] - closes[k - 1]) for k in range(1, len(closes)))
    if path <= 0:
        return None
    return net / path


def _max_drawdown(seq):
    """Max drawdown of the running cumulative-return curve (loss clustering)."""
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    for r in seq:
        eq += r
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return mdd


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
    leader = cfg.get("leader", "BTCUSDT")   # v0.9.x: configurable per-universe leader
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
    # v0.9.6 breakeven floor under the trail: once a position has moved be_pct in
    # favour (hw past the trigger), the ratcheting trail is floored at a small
    # fee-clearing lock above/below entry, so a name that ran less than the trail's
    # tmult*ATR width can no longer give back the whole move and stop below entry
    # (the live MSTR case). 0 => off (pure trail, the pre-v0.9.6 behaviour).
    be_pct = float(cfg.get("breakeven_pct", "2.0")) / 100.0
    be_lock = float(cfg.get("breakeven_lock_pct", "0")) / 100.0
    # v0.9.7 candidate A -- "Rule 9" scale-out: when price first touches tp1, bank
    # scaleout_frac of the position at tp1 and let the remainder ride the trail.
    # 0 => off. Exit fees split pro-rata across the legs, so total fee stays 2*fee.
    so_frac = float(cfg.get("scaleout_frac", "0"))
    # v0.9.7 candidate B -- step-lock ladder: generalize the v0.9.6 single
    # breakeven lock to rising floors, "arm:lock,arm:lock,..." in % of entry
    # (e.g. "2:1.5,4:3,6:4.5" = at +2% lock +1.5%, at +4% lock +3%, ...).
    # The highest armed rung wins. Empty => off (single be_lock still applies).
    ladder = []
    for rung in str(cfg.get("steplock", "") or "").split(","):
        if ":" in rung:
            arm_s, lock_s = rung.split(":", 1)
            try:
                ladder.append((float(arm_s) / 100.0, float(lock_s) / 100.0))
            except ValueError:
                pass
    ladder.sort()
    # v0.9.7 candidate C -- time-stop profit guard: 12h clock closes losers/flats
    # only; a position in profit keeps riding the trail (the trail/locks govern).
    tstop_guard = str(cfg.get("tstop_guard", "0")).strip() in ("1", "true", "yes")
    # v0.9.9 candidate -- signal-strength preemption: when the slot/correlation
    # budget is full, allow a fresh candidate whose score beats the WEAKEST resting
    # limit by >= preempt_delta to cancel-and-replace it (never a filled position).
    # Targets the live 2026-07-03 lockup: two unfilling limits (TSLA 86q + demoted
    # TAO) held the 2-slot budget ~4h while MSTR printed 100q untraded. 0 => off =
    # first-come-first-served (the current live behavior / control).
    preempt_delta = float(cfg.get("preempt_delta", "0"))

    def _lock_floor(move_pct):
        """Highest armed protective floor (fraction of entry) given the ladder,
        the single be_lock, and how far the high-water has moved in favour."""
        floor = None
        if be_lock > 0 and move_pct >= be_pct:
            floor = be_lock
        for arm, lock in ladder:
            if move_pct >= arm:
                floor = lock if floor is None else max(floor, lock)
        return floor
    # v0.8.0 correlation budget: <=0 => legacy count cap (unchanged); >0 => the
    # correlation-weighted same-side exposure model.
    corr_budget = float(cfg.get("corr_budget", "0"))
    corr_look = int(cfg.get("corr_lookback", 48))
    hedge_credit = float(cfg.get("hedge_credit", "0.5"))
    # v0.8.1 stateless aggregate-heat breaker: pause NEW entries when the combined
    # mark-to-market unrealized return across open positions is below -heat_pause_pct
    # (sum of per-slot unrealized %). 0 => off. This is the cascade control the
    # count cap can't be (it catches 3 correlated names ALL underwater together) and
    # the dead .state-dependent equity breaker should have been -- it needs no
    # persistence, only the live open book each cycle.
    heat_pause = float(cfg.get("heat_pause_pct", "0"))
    # v0.8.1b stateless realized-loss breaker: pause NEW entries when the sum of
    # REALIZED returns from trades closed within the trailing loss_window_bars is
    # <= -loss_pause_pct. Targets sequential bleed (a losing streak), which the
    # concurrent-heat breaker misses and which is what drives the multi-day maxDD.
    # This is the behaviour the dead .state equity breaker was meant to provide;
    # live it must be sourced from exchange-side realized PnL (no local persistence).
    loss_pause = float(cfg.get("loss_pause_pct", "0"))
    loss_window = int(cfg.get("loss_window_bars", 24))
    # v0.9.0 leader-level chop/range no-trade gate (Part 3 regime classifier): stand
    # aside when the leader's efficiency ratio is below er_floor (directionless --
    # range OR high-vol chop). 0 => off.
    er_floor = float(cfg.get("er_floor", "0"))
    er_look = int(cfg.get("er_lookback", 12))

    opens = []   # {sym, side, mode, fill_px, sl, tp1, atr, fill_i, hw, trail}
    pends = []   # {sym, side, entry, placed_i, plan}
    trades = []; n_sig = 0; n_chase = 0; n_expire = 0; n_fill = 0
    n_corr_block = 0; n_heat_block = 0; n_loss_block = 0; n_chop_block = 0; n_preempt = 0

    def close(p, px, why, at):
        long = p["side"] == "long"
        r = ((px - p["fill_px"]) / p["fill_px"]) if long else ((p["fill_px"] - px) / p["fill_px"])
        # blend any scale-out leg banked at tp1 with the remainder's exit; exit
        # fees split pro-rata across the legs, so the total stays 2*fee.
        total = p.get("banked", 0.0) + p.get("w", 1.0) * r
        trades.append({"sym": p["sym"], "side": p["side"], "mode": p["mode"],
                       "ret_pct": round((total - 2 * fee) * 100, 3), "reason": why, "exit_i": at})

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
                        # scale-out trim AFTER the exit checks (conservative: a bar
                        # touching both trail and tp1 counts as the full-size exit).
                        if so_frac > 0 and not p.get("trimmed") and hi >= p["tp1"]:
                            p["banked"] = so_frac * (p["tp1"] - p["fill_px"]) / p["fill_px"]
                            p["w"] = 1.0 - so_frac
                            p["trimmed"] = True
                        p["hw"] = max(p["hw"], hi)
                        nt = p["hw"] - tmult * p["atr"]
                        floor = _lock_floor((p["hw"] - p["fill_px"]) / p["fill_px"])
                        if floor is not None:
                            nt = max(nt, p["fill_px"] * (1.0 + floor))
                        p["trail"] = max(p["trail"], nt)
                else:
                    if hi >= p["trail"]:
                        ex = (p["trail"], "trail" if p["trail"] < p["sl"] else "sl")
                    elif lo <= p["tp2"]:
                        ex = (p["tp2"], "tp2")
                    else:
                        if so_frac > 0 and not p.get("trimmed") and lo <= p["tp1"]:
                            p["banked"] = so_frac * (p["fill_px"] - p["tp1"]) / p["fill_px"]
                            p["w"] = 1.0 - so_frac
                            p["trimmed"] = True
                        p["hw"] = min(p["hw"], lo)
                        nt = p["hw"] + tmult * p["atr"]
                        floor = _lock_floor((p["fill_px"] - p["hw"]) / p["fill_px"])
                        if floor is not None:
                            nt = min(nt, p["fill_px"] * (1.0 - floor))
                        p["trail"] = min(p["trail"], nt)
            else:
                if long:
                    if lo <= p["sl"]: ex = (p["sl"], "sl")
                    elif hi >= p["tp1"]: ex = (p["tp1"], "tp1")
                else:
                    if hi >= p["sl"]: ex = (p["sl"], "sl")
                    elif lo <= p["tp1"]: ex = (p["tp1"], "tp1")
            if ex is None and (i - p["fill_i"]) >= tstop:
                # profit guard: an in-profit position keeps riding the trail/locks
                # instead of being clock-closed (candidate C; guard off = legacy).
                in_profit = ((c - p["fill_px"]) / p["fill_px"] if long
                             else (p["fill_px"] - c) / p["fill_px"]) > 2 * fee
                if not (tstop_guard and in_profit):
                    ex = (c, "time_stop")
            if ex:
                close(p, ex[0], ex[1], i)
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
                              "atr": pl.atr or 0.0, "fill_i": i, "hw": q["entry"], "trail": pl.sl_price,
                              "score": q.get("score", 0.0)})
                continue
            run = ((c - q["entry"]) / q["entry"]) if long else ((q["entry"] - c) / q["entry"])
            if run > chase: n_chase += 1; continue
            if (i - q["placed_i"]) >= expiry: n_expire += 1; continue
            keepp.append(q)
        pends = keepp

        # 3) consider a new entry under the slot + correlation caps
        held = [(p["sym"], p["side"]) for p in opens] + [(q["sym"], q["side"]) for q in pends]
        owned = [h[0] for h in held]
        # v0.9.9: compute whether the budget is full, but DEFER the block -- with
        # preemption on we still select the best candidate below and, if it clears
        # the delta over the weakest resting limit, replace it. delta 0 hard-blocks
        # exactly as before (control), so this is a strict superset of old behavior.
        blocked = len(owned) >= max_conc
        if not blocked and corr_budget <= 0:
            max_corr = max_corr_base
            if any(s in ("BTCUSDT", "ETHUSDT") for s in owned):
                max_corr = min(max_corr, 1)
            blocked = len(set(owned)) >= max_corr
        if blocked and preempt_delta <= 0:
            continue
        # v0.8.1 aggregate-heat breaker: if the open book is collectively underwater
        # past the threshold, stand aside this cycle (don't add to a bleeding book).
        if heat_pause > 0 and opens:
            heat = 0.0
            for p in opens:
                cur = kl[p["sym"]][i][4]
                heat += ((cur - p["fill_px"]) / p["fill_px"] if p["side"] == "long"
                         else (p["fill_px"] - cur) / p["fill_px"]) * 100.0
            if heat <= -heat_pause:
                n_heat_block += 1
                continue
        if loss_pause > 0:
            recent = sum(t["ret_pct"] for t in trades if 0 <= i - t["exit_i"] <= loss_window)
            if recent <= -loss_pause:
                n_loss_block += 1
                continue
        # (the legacy count cap is folded into `blocked` above so preemption can act on it)
        # corr_budget > 0: the weighted gate is applied below, once the candidate
        # symbol+side is known (it depends on which name we'd actually open).
        lead = R.recon_features(leader, kl[leader][i - 24:i + 1])
        reg = scoring.regime(lead, None, cfg)
        if reg.direction not in ("long", "short"):
            continue
        # v0.9.0 chop/range no-trade gate: skip when the leader is directionless.
        if er_floor > 0:
            er = _efficiency_ratio(kl, leader, i, er_look)
            if er is not None and er < er_floor:
                n_chop_block += 1
                continue
        feats = [R.recon_features(s, kl[s][i - 24:i + 1]) for s in syms if s not in owned]
        scored = scoring.score_universe(feats, lead, cfg, reg.direction, allow_breakout=use_breakout)
        qual = [s for s in scored if not s.skip and s.score >= min_score]
        if not qual:
            continue
        best = qual[0]
        # v0.8.0: correlation-weighted exposure gate (depends on the candidate).
        # Cheap to compute and placed before enrich so a budget-blocked name does
        # not consume the per-cycle enrich call budget either.
        if corr_budget > 0 and held:
            if _corr_exposure(kl, best.symbol, best.side, held, i, corr_look, hedge_credit) >= corr_budget:
                n_corr_block += 1
                continue
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
        # v0.9.9 preemption resolution: if we reached here while the budget was full,
        # the fresh candidate must beat the WEAKEST resting limit by >= preempt_delta
        # to justify the cancel-and-replace churn. A budget full of FILLED positions
        # is never preempted (we don't close a live trade for a signal).
        if blocked:
            if not pends:
                continue
            weakest = min(pends, key=lambda q: q.get("score", 0.0))
            if best.score < weakest.get("score", 0.0) + preempt_delta:
                continue
            pends.remove(weakest); n_preempt += 1
        n_sig += 1
        if plan.entry_mode == "breakout":
            opens.append({"sym": best.symbol, "side": best.side, "mode": "breakout",
                          "fill_px": best.features.last, "sl": plan.sl_price, "tp1": plan.tp1, "tp2": plan.tp2,
                          "atr": plan.atr or 0.0, "fill_i": i, "hw": best.features.last, "trail": plan.sl_price,
                          "score": best.score})
        else:
            cur = best.features.last
            gap = ((plan.entry - cur) / plan.entry) if best.side == "short" else ((cur - plan.entry) / plan.entry)
            if gap > chase:
                continue  # pre-placement staleness skip (entry_too_far)
            pends.append({"sym": best.symbol, "side": best.side, "entry": plan.entry,
                          "placed_i": i, "plan": plan, "score": best.score})

    # force-close any still-open positions at the last bar so stats are unbiased
    last = n - 1
    for p in opens:
        close(p, kl[p["sym"]][last][4], "eow", last)

    return {"n_sig": n_sig, "n_fill": n_fill, "n_chase": n_chase, "n_expire": n_expire,
            "n_corr_block": n_corr_block, "n_heat_block": n_heat_block,
            "n_loss_block": n_loss_block, "n_chop_block": n_chop_block,
            "n_preempt": n_preempt, "trades": trades, "max_open": max_conc}


def report(tag, st):
    t = st["trades"]
    print(f"\n===== MULTI-POSITION  {tag} =====")
    print(f"  entries placed  : {st['n_sig']}   pullback fills: {st.get('n_fill','?')}   "
          f"chase: {st['n_chase']}   expire: {st['n_expire']}   "
          f"corr-block: {st.get('n_corr_block', 0)}   heat-block: {st.get('n_heat_block', 0)}   "
          f"loss-block: {st.get('n_loss_block', 0)}   chop-block: {st.get('n_chop_block', 0)}   "
          f"preempt: {st.get('n_preempt', 0)}")
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
    # tail metrics -- what a correlation cap is actually FOR (cut the left tail).
    # Order trades by exit bar to get the realized return SEQUENCE, then measure
    # max drawdown of the cumulative curve + the worst single trade.
    seq = [x["ret_pct"] for x in sorted(t, key=lambda x: x.get("exit_i", 0))]
    losses = [r for r in seq if r < 0]
    gw = sum(r for r in seq if r > 0); gl = abs(sum(losses))
    pf = round(gw / gl, 2) if gl > 0 else None
    print(f"  tail      : maxDD {_max_drawdown(seq):+.1f}%  worst-trade {min(seq):+.1f}%  "
          f"PF {pf}  (return-units, exit-ordered)")
    if st.get("by_symbol"):
        bys = {}
        for x in t:
            bys.setdefault(x["sym"], []).append(x["ret_pct"])
        rows = sorted(bys.items(), key=lambda kv: sum(kv[1]), reverse=True)
        print("  by-symbol (net% / trades / avg):")
        for sym, rs in rows:
            w = sum(1 for r in rs if r > 0)
            print(f"    {sym:10} {sum(rs):+7.1f}%  n={len(rs):2}  avg {sum(rs)/len(rs):+.2f}%  win {w/len(rs)*100:3.0f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--breakout", action="store_true")
    ap.add_argument("--exit-mode", default="fixed", choices=["fixed", "trail"])
    ap.add_argument("--trail", default="1.0")
    ap.add_argument("--be-lock", default="0",
                    help="breakeven floor under the trail: lock %% above/below entry once "
                         "the move passes breakeven_pct (0=off, pure trail)")
    ap.add_argument("--ab-belock", action="store_true",
                    help="A/B pure trail vs a sweep of breakeven-lock floors (needs --exit-mode trail)")
    ap.add_argument("--scaleout", default="0",
                    help="fraction of the position banked at tp1, remainder rides the trail (0=off)")
    ap.add_argument("--steplock", default="",
                    help="rising lock ladder 'arm:lock,arm:lock' in %% of entry (empty=off)")
    ap.add_argument("--tstop-guard", default="0",
                    help="1 = the time-stop only closes losers/flats; winners keep the trail")
    ap.add_argument("--preempt", default="0",
                    help="signal-strength preemption: a fresh candidate beating the weakest "
                         "resting limit by >= this many score points cancel-replaces it (0=off)")
    ap.add_argument("--ab-preempt", action="store_true",
                    help="A/B first-come-first-served vs a sweep of preemption deltas")
    ap.add_argument("--ab-exitpack", action="store_true",
                    help="A/B the v0.9.7 exit candidates vs the live v0.9.6 baseline (trail+belock 1.5)")
    ap.add_argument("--time-stop", default="4")
    ap.add_argument("--corr-budget", default="0",
                    help="0=legacy count cap; >0=correlation-weighted exposure budget")
    ap.add_argument("--corr-lookback", default="48", help="bars for rolling correlation")
    ap.add_argument("--ab-corr", action="store_true",
                    help="A/B legacy count cap vs a sweep of correlation budgets")
    ap.add_argument("--heat-pause", default="0",
                    help="pause new entries when open-book unrealized %% <= -this (0=off)")
    ap.add_argument("--ab-heat", action="store_true",
                    help="A/B no breaker vs a sweep of aggregate-heat pause thresholds")
    ap.add_argument("--loss-pause", default="0",
                    help="pause new entries when trailing-window realized %% <= -this (0=off)")
    ap.add_argument("--loss-window", default="24", help="bars in the realized-loss window")
    ap.add_argument("--ab-loss", action="store_true",
                    help="A/B no breaker vs a sweep of realized rolling-loss thresholds")
    ap.add_argument("--er-floor", default="0",
                    help="skip entries when leader efficiency ratio < this (0=off)")
    ap.add_argument("--er-lookback", default="12", help="bars for the efficiency ratio")
    ap.add_argument("--ab-chop", action="store_true",
                    help="A/B no gate vs a sweep of leader efficiency-ratio floors")
    ap.add_argument("--by-symbol", action="store_true",
                    help="print per-symbol net/trades/avg (edge-concentration audit)")
    ap.add_argument("--leader", default="BTCUSDT",
                    help="regime leader for this universe (e.g. XAUUSDT for metals, QQQUSDT for equities)")
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
        "exit_mode": a.exit_mode, "corr_budget": a.corr_budget, "corr_lookback": a.corr_lookback,
        "heat_pause_pct": a.heat_pause,
        "loss_pause_pct": a.loss_pause, "loss_window_bars": a.loss_window,
        "er_floor": a.er_floor, "er_lookback": a.er_lookback, "leader": a.leader,
        "breakeven_lock_pct": a.be_lock, "scaleout_frac": a.scaleout,
        "steplock": a.steplock, "tstop_guard": a.tstop_guard,
        "preempt_delta": a.preempt,
    }
    fetch_syms = list(dict.fromkeys(list(a.symbols) + [a.leader]))  # leader must be fetched too
    data = R.fetch_all(fetch_syms, a.days)
    base = f"leader={a.leader} breakout={'ON' if a.breakout else 'off'} exit={a.exit_mode} " \
           f"trail={a.trail} ts={a.time_stop} ({len(a.symbols)}syms {a.days}d, 3-slot)"
    if a.ab_corr:
        # legacy count cap vs correlation-weighted budgets -- watch maxDD (the tail),
        # not just expectancy: the cap's job is to cut clustered correlated losses.
        for cb in ("0", "1.0", "1.3", "1.6", "2.0"):
            cfg2 = dict(cfg); cfg2["corr_budget"] = cb
            st = simulate_mp(cfg2, a.symbols, a.days, a.breakout, data=data)
            if st:
                report(("LEGACY count-cap" if cb == "0" else f"corr-budget={cb}") + "  " + base, st)
        print("\nNOTE: approximate multi-position replay -- ranking tool, not P&L promise.")
        return
    if a.ab_heat:
        # no breaker vs aggregate-heat pause thresholds -- the tail test: does
        # standing aside on a bleeding book cut maxDD without gutting expectancy?
        for hp in ("0", "6", "9", "12", "15"):
            cfg2 = dict(cfg); cfg2["heat_pause_pct"] = hp
            st = simulate_mp(cfg2, a.symbols, a.days, a.breakout, data=data)
            if st:
                report(("NO breaker" if hp == "0" else f"heat-pause={hp}%") + "  " + base, st)
        print("\nNOTE: approximate multi-position replay -- ranking tool, not P&L promise.")
        return
    if a.ab_loss:
        # no breaker vs realized rolling-loss pause thresholds (window = loss_window
        # bars). The sequential-bleed tail test -- this is the lever the dead .state
        # equity breaker should pull.
        for lp in ("0", "4", "6", "8", "10"):
            cfg2 = dict(cfg); cfg2["loss_pause_pct"] = lp
            st = simulate_mp(cfg2, a.symbols, a.days, a.breakout, data=data)
            if st:
                report(("NO breaker" if lp == "0" else f"loss-pause={lp}% / {a.loss_window}b") + "  " + base, st)
        print("\nNOTE: approximate multi-position replay -- ranking tool, not P&L promise.")
        return
    if a.ab_chop:
        # no gate vs leader efficiency-ratio floors -- does standing aside when the
        # leader is directionless (range/chop) improve expectancy AND the tail?
        for er in ("0", "0.15", "0.25", "0.35", "0.45"):
            cfg2 = dict(cfg); cfg2["er_floor"] = er
            st = simulate_mp(cfg2, a.symbols, a.days, a.breakout, data=data)
            if st:
                report(("NO gate" if er == "0" else f"er-floor={er}") + "  " + base, st)
        print("\nNOTE: approximate multi-position replay -- ranking tool, not P&L promise.")
        return
    if a.ab_preempt:
        # first-come-first-served vs signal-strength preemption. Watch fills and
        # PnL (does reallocating the budget to stronger signals pay?) AND the
        # preempt count (churn cost) -- a delta too small thrashes the book, too
        # large never fires. The live lockup motivated this; the sweep generalizes it.
        for pd in ("0", "5", "10", "15", "20"):
            cfg2 = dict(cfg); cfg2["preempt_delta"] = pd
            st = simulate_mp(cfg2, a.symbols, a.days, a.breakout, data=data)
            if st:
                report(("FCFS (no preempt)" if pd == "0" else f"preempt-delta={pd}q") + "  " + base, st)
        print("\nNOTE: approximate multi-position replay -- ranking tool, not P&L promise.")
        return
    if a.ab_exitpack:
        # v0.9.7 exit-management candidates vs the LIVE baseline (trail 2.0 +
        # be-lock 1.5 = the deployed 0.6.15 exit stack). One knob per variant so
        # any win is attributable; the last rows stack the individual winners.
        variants = [
            ("LIVE baseline (belock 1.5)", {}),
            ("A: scale-out 25% @tp1",      {"scaleout_frac": "0.25"}),
            ("A: scale-out 50% @tp1",      {"scaleout_frac": "0.5"}),
            ("B: ladder 2:1.5,4:3",        {"breakeven_lock_pct": "0", "steplock": "2:1.5,4:3"}),
            ("B: ladder 2:1.5,4:3,6:4.5",  {"breakeven_lock_pct": "0", "steplock": "2:1.5,4:3,6:4.5"}),
            ("C: tstop profit-guard",      {"tstop_guard": "1"}),
            ("B+C stacked",                {"breakeven_lock_pct": "0", "steplock": "2:1.5,4:3,6:4.5",
                                            "tstop_guard": "1"}),
            ("A50+B+C stacked",            {"scaleout_frac": "0.5", "breakeven_lock_pct": "0",
                                            "steplock": "2:1.5,4:3,6:4.5", "tstop_guard": "1"}),
        ]
        cfg_t = dict(cfg); cfg_t["exit_mode"] = "trail"; cfg_t["breakeven_lock_pct"] = "1.5"
        for tag2, over in variants:
            cfg2 = dict(cfg_t); cfg2.update(over)
            st = simulate_mp(cfg2, a.symbols, a.days, a.breakout, data=data)
            if st:
                report(tag2 + "  " + base, st)
        print("\nNOTE: approximate multi-position replay -- ranking tool, not P&L promise.")
        return
    if a.ab_belock:
        # pure trail vs a breakeven floor under it (needs exit-mode trail). The tail
        # test for the live MSTR give-back: does locking near flat once armed protect
        # the winners that reverse WITHOUT choking runners on normal pullback noise?
        cfg_t = dict(cfg); cfg_t["exit_mode"] = "trail"
        for bl in ("0", "0.15", "0.5", "1.0", "1.5"):
            cfg2 = dict(cfg_t); cfg2["breakeven_lock_pct"] = bl
            st = simulate_mp(cfg2, a.symbols, a.days, a.breakout, data=data)
            if st:
                report(("PURE trail" if bl == "0" else f"be-lock={bl}%") + "  " + base, st)
        print("\nNOTE: approximate multi-position replay -- ranking tool, not P&L promise.")
        return
    st = simulate_mp(cfg, a.symbols, a.days, a.breakout, data=data)
    if st:
        st["by_symbol"] = a.by_symbol
        report(base, st)
    print("\nNOTE: approximate multi-position replay -- ranking tool, not P&L promise.")


if __name__ == "__main__":
    main()
