"""RUNECLAW scoring engine: BTC regime (long/short/neutral) + 0-100 directional score.

v0.1.0 adds short-side coverage. The BTC gate now resolves a *regime*:
- long  when BTC is up on the day and above VWAP (taker-buy bonus),
- short when BTC is down on the day and below VWAP (taker-sell bonus),
- none  otherwise (scanner reports scores but opens nothing).

Each coin is scored 0-100 *for the active direction*: long rewards relative
strength / above-VWAP / upper-range / bid-heavy book; short mirrors each.
"""
from dataclasses import dataclass
from typing import Optional

from .features import SymbolFeatures


@dataclass
class Regime:
    direction: str          # "long" | "short" | "none"
    size_factor: float      # 1.0 full, 0.5 reduced, 0.0 blocked
    score: int              # active gate sub-score (0-3)
    detail: dict


@dataclass
class Scored:
    symbol: str
    side: str
    score: float
    dims: dict
    skip: bool
    skip_reason: str
    features: SymbolFeatures
    size_factor: float = 1.0   # carried from the candidate's universe regime
    universe: str = ""         # which universe (leader) produced this candidate
    entry_mode: str = "pullback"  # v0.5.0: "pullback" (limit) or "breakout" (market)


def regime(btc: SymbolFeatures, taker_ratio: Optional[float], cfg: dict) -> Regime:
    allow_short = bool(cfg.get("allow_short", True))
    # v0.9.31 -- vote DEAD-ZONES (signal-audit finding #2). All three regime votes
    # were razor-edged (change_pct vs 0, last vs vwap, taker vs 1.0): a leader
    # hovering at +0.05% or brushing VWAP flipped the WHOLE universe long<->short
    # between 15-minute cycles (the live L<->s whipsaw: limits placed, stale-
    # cancelled, replaced). A dead-zone withdraws a vote when its signal is inside
    # the noise band, so weak tape yields fewer votes -> reduced-size or none
    # regimes instead of full-size direction flips. STATELESS (no cycle memory --
    # the runtime forbids it); all defaults 0 = bit-exact legacy edges.
    # regime_taker_vote gates the third vote entirely: the replay harness has
    # NEVER simulated taker (it passes None), so every validation ever run is of
    # the 2-vote gate -- "0" restores exact live/replay parity (the v0.9.23
    # argument); "1" (default) keeps current live behaviour. Card-tunable.
    def _dz(key: str) -> float:
        try:
            return max(float(cfg.get(key, "0") or "0"), 0.0)
        except (TypeError, ValueError):
            return 0.0
    chg_dz = _dz("regime_chg_deadzone_pct")          # % day-change band with no vote
    vwap_dz = _dz("regime_vwap_deadzone_pct") / 100.0  # % distance-from-VWAP band
    tk_dz = _dz("regime_taker_deadzone")             # ratio band around 1.0
    taker_on = str(cfg.get("regime_taker_vote", "1")).strip().lower() not in ("0", "false", "no")

    up = bool(btc.ok and btc.change_pct is not None and btc.change_pct > chg_dz)
    down = bool(btc.ok and btc.change_pct is not None and btc.change_pct < -chg_dz)
    above = bool(btc.ok and btc.last is not None and btc.vwap is not None
                 and btc.last > btc.vwap * (1.0 + vwap_dz))
    below = bool(btc.ok and btc.last is not None and btc.vwap is not None
                 and btc.last < btc.vwap * (1.0 - vwap_dz))
    taker_buy = bool(taker_on and taker_ratio is not None and taker_ratio > 1.0 + tk_dz)
    taker_sell = bool(taker_on and taker_ratio is not None and taker_ratio < 1.0 - tk_dz)

    long_score = (1 if up else 0) + (1 if above else 0) + (1 if taker_buy else 0)
    short_score = (1 if down else 0) + (1 if below else 0) + (1 if taker_sell else 0)

    detail = {
        "btc_change_pct": btc.change_pct,
        "above_vwap": above,
        "below_vwap": below,
        "taker_ratio": taker_ratio,
        "long_gate_score": long_score,
        "short_gate_score": short_score,
        "allow_short": allow_short,
    }

    if long_score >= 2:
        return Regime("long", 1.0, long_score, detail)
    if allow_short and short_score >= 2:
        return Regime("short", 1.0, short_score, detail)
    if long_score == 1 and long_score >= short_score:
        return Regime("long", 0.5, long_score, detail)
    if allow_short and short_score == 1:
        return Regime("short", 0.5, short_score, detail)
    return Regime("none", 0.0, max(long_score, short_score), detail)


def _minmax(values: list) -> tuple:
    if not values:
        return (0.0, 0.0)
    return (min(values), max(values))


def score_universe(feats_list: list, btc: SymbolFeatures, cfg: dict, direction: str,
                   allow_breakout: bool = False) -> list:
    """Score every coin for ``direction`` ("long"/"short"; "none" falls back to
    long scores for board visibility). Returns Scored rows sorted best-first.

    v0.5.0: when ``allow_breakout`` is set (master switch on AND this is a
    breakout-eligible universe), a name extended past ``max_vwap_ext_pct`` on the
    entry side is NOT skipped -- it is tagged ``entry_mode="breakout"`` so it keeps
    its momentum score and competes; pass-2 ``enrich_score`` then confirms the
    trend or demotes it. With the switch off, behavior is unchanged (hard skip)."""
    side = "short" if direction == "short" else "long"
    ok = [f for f in feats_list if f.ok]
    btc_change = btc.change_pct if (btc.ok and btc.change_pct is not None) else 0.0

    rel = {f.symbol: (f.change_pct - btc_change) for f in ok if f.change_pct is not None}
    rel_min, rel_max = _minmax(list(rel.values()))
    vol_min, vol_max = _minmax([f.quote_volume for f in ok if f.quote_volume is not None])

    min_vol = float(cfg.get("min_volume_usdt", "10000000"))
    full_ratio = float(cfg.get("bidask_full_ratio", "2.0"))
    partial_ratio = float(cfg.get("bidask_partial_ratio", "1.2"))
    wall_ratio = float(cfg.get("bidask_wall_ratio", "10.0"))
    max_ext_pct = float(cfg.get("max_vwap_ext_pct", "4.0")) / 100.0
    # v0.9.33 -- dimension weights as config (signal-audit finding #5: the
    # 25/20/20/20/15 split is v0.1.0 vintage, never questioned). Each dim still
    # computes on its original internal scale; the weight rescales its
    # CONTRIBUTION, so the defaults are bit-exact legacy and w=0 is a clean
    # ABLATION ("does this signal earn its weight?") -- the intended use. The
    # hard DISQUALIFIERS tied to these signals (ask/bid wall skip, thin-volume
    # skip, no-vwap skip) are NOT weights and remain in force at any weight.
    def _w(key: str, base: float) -> float:
        try:
            return max(float(cfg.get(key, str(base)) or str(base)), 0.0) / base
        except (TypeError, ValueError):
            return 1.0
    w_mom = _w("score_w_momentum", 25.0)
    w_vwap = _w("score_w_vwap", 20.0)
    w_range = _w("score_w_range", 20.0)
    w_book = _w("score_w_orderbook", 20.0)
    w_volm = _w("score_w_volume", 15.0)

    results = []
    for f in feats_list:
        if not f.ok:
            results.append(Scored(f.symbol, side, 0.0, {"total": 0.0}, True, f.note or "no_data", f))
            continue
        # v0.9.13 (audit C-1): VWAP is the entry anchor -- risk.build_plan is
        # VWAP-relative (entry = vwap -/+ k*ATR) and returns None without it. A
        # name with no VWAP is structurally untradeable, so HARD-SKIP it instead of
        # scoring the VWAP dim as a guessed-neutral 10/20. Otherwise a vwap-less name
        # can top the board, then build_plan None-fails and main_live stands the
        # WHOLE cycle down (watch sizing_failed) with no fallthrough to the runner-up.
        # Mirrors the no_data / thin_volume disqualifiers ("missing data is never
        # guessed, and must not be traded either").
        if not f.vwap or f.vwap <= 0:
            results.append(Scored(f.symbol, side, 0.0, {"total": 0.0}, True, "no_vwap", f))
            continue

        dims: dict = {}
        skip = False
        reason = ""
        entry_mode = "pullback"

        # Momentum 0-25: long rewards strength, short rewards weakness.
        r = rel.get(f.symbol)
        if r is None or rel_max <= rel_min:
            momentum = 12.5
        else:
            norm = (r - rel_min) / (rel_max - rel_min)
            momentum = 25.0 * norm if side == "long" else 25.0 * (1.0 - norm)
        dims["momentum"] = round(momentum, 2)
        dims["rel_strength"] = round(r, 4) if r is not None else None

        # VWAP position 0-20.
        if f.vwap and f.last is not None:
            above = f.last > f.vwap * 1.001
            below = f.last < f.vwap * 0.999
            if side == "long":
                vwap_score = 20.0 if above else (0.0 if below else 10.0)
            else:
                vwap_score = 20.0 if below else (0.0 if above else 10.0)
        else:
            vwap_score = 10.0
        dims["vwap"] = vwap_score

        # Extension guard: the entry is a VWAP-anchored pullback limit
        # (long: VWAP - k*ATR; short mirrored), so it can only fill if price has
        # not run too far from VWAP. Names extended beyond the cap on the entry
        # side are momentum breakouts the limit model structurally cannot catch
        # -- score them for the board but skip them as candidates.
        if f.vwap and f.last is not None:
            ext = (f.last - f.vwap) / f.vwap  # > 0 = above VWAP
            dims["vwap_ext_pct"] = round(ext * 100.0, 3)
            if not skip and max_ext_pct > 0:
                over = ((side == "long" and ext > max_ext_pct)
                        or (side == "short" and ext < -max_ext_pct))
                if over:
                    if allow_breakout:
                        # v0.5.0: route to the breakout path instead of discarding.
                        entry_mode = "breakout"
                        dims["breakout_eligible"] = True
                    else:
                        reason = ("overextended_above_vwap" if side == "long"
                                  else "overextended_below_vwap")
                        skip = True

        # Range position 0-20.
        span = (f.high - f.low) if (f.high is not None and f.low is not None) else 0.0
        range_pos: Optional[float] = None
        if span > 0 and f.last is not None:
            range_pos = (f.last - f.low) / span
            if side == "long":
                range_score = 20.0 if range_pos > 0.66 else (10.0 if range_pos >= 0.33 else 0.0)
            else:
                range_score = 20.0 if range_pos < 0.34 else (10.0 if range_pos <= 0.67 else 0.0)
        else:
            range_score = 0.0
        dims["range"] = range_score
        dims["range_pos"] = round(range_pos, 3) if range_pos is not None else None

        # Order book 0-20 (best bid/ask resting imbalance).
        bid_vol, ask_vol = f.bid_volume, f.ask_volume
        if bid_vol is not None and ask_vol not in (None, 0) and bid_vol > 0:
            ratio = bid_vol / ask_vol  # > 1 bid-heavy
            dims["bidask_ratio"] = round(ratio, 3)
            favor = ratio if side == "long" else (ask_vol / bid_vol)
            if favor >= full_ratio:
                orderbook_score = 20.0
            elif favor >= partial_ratio:
                orderbook_score = 12.0
            elif favor >= 0.8:
                orderbook_score = 8.0
            else:
                orderbook_score = 4.0
            # Hard skip into an opposing wall.
            if side == "long" and (ask_vol / bid_vol) >= wall_ratio:
                skip, reason = True, "ask_wall"
            elif side == "short" and ratio >= wall_ratio:
                skip, reason = True, "bid_wall"
        else:
            orderbook_score = 8.0
            dims["bidask_ratio"] = None
            dims["orderbook_degraded"] = True
        dims["orderbook"] = orderbook_score

        # Volume 0-15 (cross-sectional) + thin-liquidity disqualifier.
        # v0.9.4 (audit C-1): MISSING volume data is a disqualifier too. The old
        # `is not None and < min_vol` let a symbol whose quote_volume failed to
        # parse sail past the liquidity floor with neutral scores -- an
        # unknown-liquidity name could qualify. Missing data is never guessed
        # (the features.py rule); it must not be traded either.
        quote_volume = f.quote_volume
        if quote_volume is None:
            skip = True
            reason = reason or "no_volume_data"
        elif quote_volume < min_vol:
            skip = True
            reason = reason or "thin_volume"
        if quote_volume is None or vol_max <= vol_min:
            volume_score = 7.5
        else:
            volume_score = 15.0 * (quote_volume - vol_min) / (vol_max - vol_min)
        dims["volume"] = round(volume_score, 2)
        dims["quote_volume"] = quote_volume

        total = (momentum * w_mom + vwap_score * w_vwap + range_score * w_range
                 + orderbook_score * w_book + volume_score * w_volm)
        dims["total"] = round(total, 2)
        results.append(Scored(f.symbol, side, total, dims, skip, reason, f,
                              entry_mode=entry_mode))

    results.sort(key=lambda s: (not s.skip, s.score), reverse=True)
    return results


def enrich_score(scored: Scored, feats: SymbolFeatures, cfg: dict) -> tuple:
    """v0.2.0 pass-2 adjustment for an already-qualified candidate: higher-TF
    trend alignment (bonus/penalty) plus a funding crowding skip + soft penalty.

    Returns ``(adjusted_score, extra_dims, skip, skip_reason)``. Degrades to a
    no-op when the candidate has no kline/funding data (enrichment failed)."""
    side = scored.side
    base = scored.score
    skip, reason = scored.skip, scored.skip_reason
    extra: dict = {"base_score": round(base, 2)}

    # --- trend alignment: reward agreement with the higher-TF trend, punish opposition
    trend_weight = float(cfg.get("trend_weight", "15.0"))
    trend_adj = 0.0
    if feats.kline_ok and feats.trend_dir in ("long", "short"):
        sign = 1.0 if feats.trend_dir == side else -1.0
        trend_adj = sign * trend_weight * max(0.0, min(feats.trend_strength, 1.0))
    extra["trend_dir"] = feats.trend_dir
    extra["trend_adj"] = round(trend_adj, 2)

    # --- v0.9.34 structure gates (opt-in, default off; data from features'
    # swing/candle engine, populated in enrich and mirrored in the harness):
    #  structure_trend_veto -- skip a candidate whose HH/HL-vs-LH/LL structure
    #    OPPOSES its side (a long into LH+LL structure). Neutral structure never
    #    vetoes (fail-open).
    #  candle_veto -- skip when the last CLOSED entry-TF bar is a counter-candle
    #    (doji at the would-be entry, or an engulfing bar against the side).
    if not skip and str(cfg.get("structure_trend_veto", "0")).strip().lower() in ("1", "true", "yes"):
        sdir = getattr(feats, "structure_dir", "neutral")
        opposed = "short" if side == "long" else "long"
        extra["structure_dir"] = sdir
        if sdir == opposed:
            skip, reason = True, "structure_opposed"
    if not skip and str(cfg.get("candle_veto", "0")).strip().lower() in ("1", "true", "yes"):
        cv = getattr(feats, "candle_veto_long" if side == "long" else "candle_veto_short", "")
        if cv:
            extra["candle_veto"] = cv
            skip, reason = True, "candle_" + cv

    # --- v0.5.0 breakout confirmation: a breakout-tagged candidate is only real if
    # the higher-TF trend is strong AND aligned AND price sits at the session
    # extreme. Otherwise it is just an overextended blip -> demote to no-trade.
    if scored.entry_mode == "breakout" and not skip:
        trend_min = float(cfg.get("breakout_trend_min", "0.6"))
        band = float(cfg.get("breakout_extreme_band", "0.015"))
        aligned = bool(feats.kline_ok and feats.trend_dir == side
                       and feats.trend_strength >= trend_min)
        near_extreme = False
        if feats.last is not None and feats.high is not None and feats.low is not None:
            if side == "long" and feats.high > 0:
                near_extreme = feats.last >= feats.high * (1.0 - band)
            elif side == "short" and feats.low > 0:
                near_extreme = feats.last <= feats.low * (1.0 + band)
        extra["breakout_aligned"] = aligned
        extra["breakout_near_extreme"] = near_extreme
        # v0.9.34 opt-in third condition -- breakout_structure_confirm: the move
        # must ALSO have broken the last CONFIRMED swing point (a break of real
        # structure, not just proximity to a rolling 24h extreme). Fail-open when
        # no swing is available (thin bars must never silently kill breakouts).
        struct_ok = True
        if str(cfg.get("breakout_structure_confirm", "0")).strip().lower() in ("1", "true", "yes"):
            if side == "long":
                sw = getattr(feats, "swing_high", None)
                struct_ok = (sw is None) or (feats.last is not None and feats.last > sw)
            else:
                sw = getattr(feats, "swing_low", None)
                struct_ok = (sw is None) or (feats.last is not None and feats.last < sw)
            extra["breakout_structure_ok"] = struct_ok
        if not (aligned and near_extreme and struct_ok):
            skip, reason = True, "breakout_unconfirmed"

    # --- funding: skip into a crowded extreme, soft-penalize milder adverse funding.
    # v0.9.12: funding is sourced from data.crypto.futures.funding_rate -- a CRYPTO
    # endpoint (Coinglass-backed) with NO RWA-equity/metals coverage. Applied to an
    # equity perp (MSTRUSDT, whose true funding settles at 0) it returns a foreign /
    # mis-resolved value that can exceed the 30bps skip, discarding the board's best
    # equity signal on pure data noise -- the live MSTR `funding_cr` flicker (skip at
    # >30bps one scan, empty->clean the next). Scope the whole funding block to the
    # universes where the reading is NATIVE (crypto). Fail-OPEN on an unknown/empty
    # universe so an unlabeled crypto candidate never loses crowding protection (the
    # single-universe fallback names it 'crypto'; recon/tests leave it '' with
    # funding_ok False anyway, so the block short-circuits regardless). Crypto keeps
    # BOTH the hard skip and the soft penalty at full strength -- a genuine mania at
    # 30-100bps on ETH/SOL still skips exactly as before.
    funding_penalty = 0.0
    funding_skip = False
    _uni = getattr(scored, "universe", "") or ""
    _funding_applies = (not _uni) or _uni in set(cfg.get("funding_universes", ["crypto"]))
    if _funding_applies and feats.funding_ok and feats.funding_now is not None:
        bps = feats.funding_now * 10000.0  # decimal funding rate -> basis points
        extra["funding_bps"] = round(bps, 3)
        skip_bps = float(cfg.get("funding_skip_bps", "30"))
        pen_weight = float(cfg.get("funding_penalty_weight", "8.0"))
        adverse = bps if side == "long" else -bps  # leaning into the crowded side
        if not skip and skip_bps > 0 and adverse > skip_bps:
            skip, reason, funding_skip = True, "funding_crowded_" + side, True
        elif adverse > 0 and skip_bps > 0:
            funding_penalty = pen_weight * min(adverse / skip_bps, 1.0)
    extra["funding_skip"] = funding_skip

    adjusted = max(base + trend_adj - funding_penalty, 0.0)
    extra["adjusted_score"] = round(adjusted, 2)
    return (adjusted, extra, skip, reason)
