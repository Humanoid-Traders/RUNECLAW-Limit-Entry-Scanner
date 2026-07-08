"""Live scan + signal for the RUNECLAW Limit Entry Scanner (v0.1.0).

Each scheduled pass: resolve the BTC regime (long / short / neutral), ALWAYS
score the whole universe for visibility, emit one managed signal with a ranked
board + gate transparency, and — in follow-trade mode — run the position
manager (circuit breaker, time-stops, auto-breakeven) and place the best
qualifying setup subject to the concurrent + correlation caps.
"""
import math
from datetime import datetime, timezone
from typing import Any

from getagent import runtime

from . import execution, features, risk, scoring

_GATE = "BTCUSDT"
# v0.9.4: version + provenance stamp on every emitted analysis record, so
# downstream consumers (journal reducer, dashboards, future reconciliation)
# can attribute any output to the exact analysis generation that produced it.
# The engine is deterministic end-to-end -- no LLM in the decision path.
ANALYSIS_VERSION = "0.9.38"
THESIS_SOURCE = "deterministic_rules"


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


def _board(scored: list, limit: int = 8) -> list:
    out = []
    for s in scored[:limit]:
        out.append({
            "symbol": s.symbol,
            "side": s.side,
            "score": round(s.score, 1),
            "skip": s.skip,
            "skip_reason": s.skip_reason,
            "dims": s.dims,
        })
    return out


def _gate_summary(reg: scoring.Regime) -> str:
    detail = reg.detail
    bits = []
    if detail.get("above_vwap"):
        bits.append("BTC>VWAP")
    elif detail.get("below_vwap"):
        bits.append("BTC<VWAP")
    chg = detail.get("btc_change_pct")
    if chg is not None:
        bits.append("24h+" if chg > 0 else "24h-")
    state = reg.direction.upper() if reg.direction != "none" else "NEUTRAL"
    return (f"Regime {state} | long_gate {detail.get('long_gate_score')}/2 "
            f"short_gate {detail.get('short_gate_score')}/2 | " + ", ".join(bits))


def _field_health(ft) -> dict:
    """Which ticker-derived fields actually populated (None = missing in live data)."""
    return {k: (getattr(ft, k, None) is not None) for k in
            ("last", "vwap", "high", "low", "change_pct", "quote_volume", "bid_volume", "ask_volume")}


def _session_open(spec: str) -> bool:
    """v0.9.22: True iff now-UTC is a WEEKDAY inside the "HH:MM-HH:MM" UTC window
    `spec`. The underlying-session gate for RWA universes (equities): perps trade
    24/7 but the cash market keeps hours, and outside them the perp has no price
    discovery. Weekends are always closed. Fail-OPEN on a malformed spec -- a
    config typo must never silently halt a universe. NOTE: the window is a fixed
    UTC range, so US DST shifts it by 1h twice a year (13:30-20:00 UTC = RTH in
    summer/EDT); adjust the manifest value seasonally or accept the 1h skew.
    Mirrored by research/replay_mp._session_open_ts so sweeps validate this gate."""
    try:
        a, b = spec.split("-", 1)
        h1, m1 = (int(x) for x in a.split(":"))
        h2, m2 = (int(x) for x in b.split(":"))
        now = datetime.now(timezone.utc)
        if now.weekday() >= 5:            # Sat/Sun: cash market closed
            return False
        return (h1 * 60 + m1) <= (now.hour * 60 + now.minute) < (h2 * 60 + m2)
    except (TypeError, ValueError, AttributeError):
        return True


def _universes(cfg: dict) -> list:
    """List of {name, leader, symbols, allow_short}. Falls back to the legacy
    single BTC-led universe from ``trading_symbols`` when ``universes`` is unset."""
    default_syms = [str(s).upper() for s in (cfg.get("trading_symbols") or [])]
    out = []
    for u in (cfg.get("universes") or []):
        if not isinstance(u, dict):
            continue
        leader = str(u.get("leader", _GATE)).upper()
        # a universe with no explicit symbols inherits the default trading_symbols
        syms = [str(s).upper() for s in (u.get("symbols") or [])] or default_syms
        if syms:
            row = {"name": str(u.get("name", leader)), "leader": leader,
                   "symbols": syms, "allow_short": u.get("allow_short")}
            # v0.9.5 BUG FIX: pass the per-universe flags through. _scan_universe
            # has read uni.get("breakout") / uni.get("overrides") since v0.6.0,
            # but this dict never carried them -- the manifest's equities
            # `breakout: true` and `overrides` were silently ignored (equities
            # ran pullback-only at the global ext cap). Copy only keys PRESENT
            # in the config so `.get(key, default)` semantics stay intact for
            # universes that omit a flag (crypto's name-based breakout default).
            for key in ("breakout", "overrides", "event_blackout",
                        "pullback", "session_hours_utc",   # v0.9.22 trade-type gates
                        "earnings_blackout"):              # v0.9.30 per-symbol guard
                if key in u:
                    row[key] = u[key]
            out.append(row)
    if not out:
        out = [{"name": "crypto", "leader": _GATE, "symbols": default_syms, "allow_short": None}]
    # v0.9.38 -- extra_symbols: a card-tunable manual watchlist appended to the
    # FIRST universe (crypto). Lets the operator add a fresh listing the day it
    # matters without a manifest edit + redeploy dark window (uptime beats
    # parameters -- reconciliation 2026-07-07). NOTE: pass-1 slices candidates
    # to max_scan_symbols, so adding names beyond 28 needs that key raised on
    # the card too, or the tail of the list is silently never scanned.
    extra = [str(x).upper() for x in (cfg.get("extra_symbols") or []) if x]
    if extra and out:
        seen = set(out[0]["symbols"])
        out[0]["symbols"] = out[0]["symbols"] + [x for x in extra if x not in seen]
    return out


def _scan_universe(uni: dict, cfg: dict, blackout: dict = None) -> dict:
    """Resolve one universe's regime from its own leader and score its symbols.
    Each candidate is tagged with the universe name and its regime size_factor."""
    leader_sym = uni["leader"]
    # v0.6.0: per-universe config overrides for the pass-1 scan. allow_short
    # (legacy) plus an optional `overrides` map -- e.g. equities lower
    # max_vwap_ext_pct so a smaller move routes to breakout. Pass-2 enrichment
    # runs on the merged pool with global cfg, so only pass-1 keys are tunable here.
    over = {}
    if uni.get("allow_short") is not None:
        over["allow_short"] = bool(uni["allow_short"])
    extra_over = uni.get("overrides")
    if isinstance(extra_over, dict):
        over.update(extra_over)
    ucfg = {**cfg, **over} if over else cfg
    leader_feats = features.fetch_symbol(leader_sym)
    # v0.9.13: taker_buy_ratio sources data.crypto.futures.taker_volume -- a CRYPTO
    # endpoint, SAME class as the funding bug. On the equities (QQQ) and metals (XAU)
    # leaders it returns a mis-resolved crypto-flow value that feeds scoring.regime's
    # taker gate and can FLIP the universe's side and size_factor (both drive
    # risk.build_plan). Read it only for crypto leaders; a non-crypto universe passes
    # taker=None and regime() degrades cleanly to the up/vwap 2-signal gate. Fail-OPEN
    # on an unknown/empty universe (legacy single-universe fallback names it 'crypto').
    _tk_unis = set(cfg.get("taker_universes", ["crypto"]))
    _uni_name = str(uni.get("name", "") or "")
    taker = (features.taker_buy_ratio(leader_sym)
             if (not _uni_name or _uni_name in _tk_unis) else None)
    reg = scoring.regime(leader_feats, taker, ucfg)
    direction = reg.direction
    scan_direction = direction if direction in ("long", "short") else "long"
    max_scan = int(cfg.get("max_scan_symbols", len(uni["symbols"])) or len(uni["symbols"]))
    min_score = float(cfg.get("min_score", 70))
    candidates = [s for s in uni["symbols"] if s != leader_sym][: max(max_scan, 0)]
    feats = [features.fetch_symbol(s) for s in candidates]
    # v0.6.0: breakout is per-universe. A universe opts in with `breakout: true`;
    # absent the flag it defaults to crypto-only (v0.5.x behavior). Gated by the
    # global breakout_enabled master switch. (equities now opt in -- they trend too
    # and the pullback-only path kept chase-cancelling into clean down-moves.)
    allow_breakout = (bool(cfg.get("breakout_enabled", False))
                      and bool(uni.get("breakout", uni["name"] == "crypto")))
    scored = scoring.score_universe(feats, leader_feats, ucfg, scan_direction,
                                    allow_breakout=allow_breakout)
    for s in scored:
        s.universe = uni["name"]
        s.size_factor = reg.size_factor
    qualified = ([s for s in scored if not s.skip and s.score >= min_score]
                 if direction in ("long", "short") else [])
    # v0.9.5 macro event blackout: an opted-in universe (RWA equities gap on
    # high-importance US prints) suppresses NEW entries inside the event window.
    # Scores stay on the board for visibility; only candidacy is withdrawn.
    # Existing positions/limits are untouched (this gates opens only).
    blacked = bool(blackout and uni.get("event_blackout"))
    if blacked:
        qualified = []
    # v0.9.22 trade-type gates (entry-side only; management is never touched, same
    # contract as the event blackout -- scores stay visible, candidacy withdrawn):
    #  (a) `pullback: false` -> this universe trades breakouts ONLY. Phase-1 replay
    #      on the QQQ-led equities set: pullback entries are net-NEGATIVE at EVERY
    #      hold cap (win 32-37%, n>=50) while breakouts pay (+17-20%, win 58-66%).
    #  (b) `session_hours_utc: "HH:MM-HH:MM"` -> new entries only while the
    #      underlying CASH market is open (weekends always closed). Perps trade
    #      24/7, but with the cash session shut an RWA perp has no price
    #      discovery -- the July-5 weekend MSTR hold was 8h of drift into the stop.
    if uni.get("pullback") is False:
        qualified = [s for s in qualified if s.entry_mode == "breakout"]
    sess = str(uni.get("session_hours_utc", "") or "")
    sess_closed = bool(sess) and not _session_open(sess)
    if sess_closed:
        qualified = []
    # v0.9.30 earnings blackout: the macro calendar (event_blackout above) does
    # not know MSTR reports tonight. For an opted-in universe, withdraw a
    # symbol's OWN candidacy around its report date (day-granular + pad; see
    # features._earnings_window). Same contract as every gate here: entries
    # only, scores stay visible, fail-open. Bounded cost: at most len(qualified)
    # calendar reads for a 3-symbol universe, only when opted in.
    earnings_out = []
    if uni.get("earnings_blackout") and qualified:
        kept = []
        for s in qualified:
            ev = features.earnings_blackout(s.symbol, cfg)
            if ev:
                earnings_out.append(s.symbol)
            else:
                kept.append(s)
        qualified = kept
    return {"name": uni["name"], "leader": leader_sym, "regime": reg,
            "leader_feats": leader_feats, "direction": direction,
            "scored": scored, "qualified": qualified,
            "session_closed": sess_closed,
            "earnings_blackout_symbols": earnings_out,
            "event_blackout": (blackout if blacked else None)}


def build_decision(cfg: dict, mgmt: dict) -> dict:
    min_score = float(cfg.get("min_score", 70))
    unis = _universes(cfg)
    # v0.9.5: one bounded calendar read per cycle, only if some universe opted
    # into the macro event blackout (fail-open -- see features.event_blackout).
    blackout = (features.event_blackout(cfg)
                if any(u.get("event_blackout") for u in unis) else None)
    scans = [_scan_universe(u, cfg, blackout=blackout) for u in unis]

    # v0.9.38 -- SHADOW-mode whole-exchange discovery (opt-in, observation only).
    # A discovery rule can't be honestly backtested (today's ticker list shows
    # only survivors), so the engine forward-tests it on itself: candidates are
    # scored under the crypto regime and LOGGED (metrics.discovery); they never
    # enter the qualified pool. discovery_trade stays "0" until the forward
    # record earns an arming decision. Fail-open everywhere: no bulk SDK
    # surface / any error -> empty log, zero effect on the decision path.
    discovery_log = None
    if str(cfg.get("universe_discovery", "0")).strip().lower() in ("1", "true", "yes"):
        try:
            _excl = set()
            for _u in unis:
                _excl.update(_u["symbols"]); _excl.add(_u["leader"])
            _dfeats, _dnote = features.discovery_scan(_excl, cfg)
            _csc = scans[0]   # first universe = crypto: its leader + regime gate the scan
            _dir = _csc["regime"].direction
            _dscored = (scoring.score_universe(
                _dfeats, _csc["leader_feats"], cfg,
                _dir if _dir in ("long", "short") else "long") if _dfeats else [])
            discovery_log = {"source": _dnote, "candidates": [
                {"symbol": d.symbol, "side": d.side, "score": round(d.score, 1),
                 "skip": d.skip, "skip_reason": d.skip_reason,
                 "qv_musd": round((d.features.quote_volume or 0) / 1e6, 1),
                 "chg_pct": d.features.change_pct} for d in _dscored]}
        except Exception as _exc:
            discovery_log = {"source": "error:" + type(_exc).__name__, "candidates": []}

    # --- merge board + qualified pool across universes (mixed sides) ---
    all_scored = []
    for sc in scans:
        all_scored.extend(sc["scored"])
    all_scored.sort(key=lambda s: (not s.skip, s.score), reverse=True)
    board = _board(all_scored)
    best_score = round(all_scored[0].score, 1) if all_scored else 0.0

    qualified = []
    for sc in scans:
        qualified.extend(sc["qualified"])
    qualified.sort(key=lambda s: s.score, reverse=True)
    # v0.9.23 PARITY FIX (signal audit finding #1): withdraw already-owned symbols
    # (RUNECLAW-sized positions + resting limits) from candidacy, exactly as the
    # replay harness has ALWAYS done (replay_mp scores `syms not in owned`). Every
    # replay validation therefore assumes a blocked best FALLS THROUGH to the
    # next-best name -- but live kept the owned symbol as `best`, died on the
    # execution-level entry_already_pending/already_in_position guard, and placed
    # NOTHING that cycle (the live 2h MSTR-limit window burned ~9 cycles this way
    # while qualified names sat behind it). Scores stay on the board (digest
    # unchanged -- same visibility contract as the blackout/session gates); only
    # candidacy is withdrawn. Fail-safe: a state-blind cycle carries no
    # owned_symbols -> no filter -> legacy behaviour, and open_if_allowed refuses
    # blind placement anyway. The per-symbol guards in execution remain as the
    # race backstop, so entry_already_pending becoming RARE on the feed is the
    # expected signature of this fix working.
    _owned = {str(s).upper() for s in (mgmt.get("owned_symbols") or [])}
    if _owned:
        qualified = [s for s in qualified if s.symbol.upper() not in _owned]

    any_active = any(sc["direction"] in ("long", "short") for sc in scans)
    circuit = mgmt.get("circuit", "ok")

    regime_by_uni = {}
    for sc in scans:
        reg, lf = sc["regime"], sc["leader_feats"]
        regime_by_uni[sc["name"]] = {
            "leader": sc["leader"],
            "direction": sc["direction"],
            "size_factor": reg.size_factor,
            "leader_vs_vwap": (round((lf.last / lf.vwap - 1.0) * 100.0, 3)
                               if (lf.ok and lf.last and lf.vwap) else None),
            "long_gate": reg.detail.get("long_gate_score"),
            "short_gate": reg.detail.get("short_gate_score"),
        }

    base_metrics = {
        "regime_by_universe": regime_by_uni,
        "any_regime_open": any_active,
        "best_score": best_score,
        "min_score": min_score,
        "scanned": sum(len(sc["scored"]) for sc in scans),
        "universes": [sc["name"] for sc in scans],
        "circuit": circuit,
        "today_pnl": mgmt.get("today_pnl"),
        "open_count": mgmt.get("open_count", 0),
        # v0.9.5: the active macro-event blackout ({event, ts, importance} or
        # None) and which universes it suppressed this cycle.
        "event_blackout": blackout,
        "blackout_universes": [sc["name"] for sc in scans if sc.get("event_blackout")],
        # v0.9.22: universes whose underlying cash session is closed this cycle
        # (their candidacy was withdrawn; scores remain on the board).
        "session_closed_universes": [sc["name"] for sc in scans if sc.get("session_closed")],
        # v0.9.30: symbols stood down around their own earnings report this cycle.
        "earnings_blackout_symbols": sorted(
            {s for sc in scans for s in (sc.get("earnings_blackout_symbols") or [])}),
    }
    base_meta = {
        "regime_by_universe": regime_by_uni,
        # v0.9.8: compact per-universe digest, carried on every path so the
        # emitted SCAN line surfaces per-name scores in the operator's feed.
        "scan_digest": _scan_digest(scans, min_score),
        "gate_summary": " || ".join("{}: {}".format(sc["name"], _gate_summary(sc["regime"]))
                                    for sc in scans),
        "board": board,
        "open_symbols": mgmt.get("open_symbols", []),
        "mgmt_actions": mgmt.get("actions", []),
        "controls_active": mgmt.get("controls_active", {}),
        "data_health": {sc["leader"]: _field_health(sc["leader_feats"]) for sc in scans},
        "run_id": runtime.run_id,
    }
    if discovery_log is not None:
        base_metrics["discovery"] = discovery_log

    def watch(symbol: str, reason: str, extra_metrics: dict = None) -> dict:
        metrics = dict(base_metrics)
        metrics["tradable_candidates"] = 0
        if extra_metrics:
            metrics.update(extra_metrics)
        meta = dict(base_meta)
        meta["reason"] = reason
        return {"action": "watch", "symbol": symbol, "confidence": 0.0,
                "metrics": metrics, "meta": meta, "plan": None}

    top_symbol = all_scored[0].symbol if all_scored else unis[0]["leader"]

    if circuit in ("paused", "tripped"):
        return watch(top_symbol, "circuit_" + str(circuit))
    if not any_active:
        return watch(top_symbol, "all_regimes_neutral")
    if not qualified:
        return watch(top_symbol, "no_setup_at_or_above_min_score")

    # --- Pass 2 (v0.2.0): the cheap ticker scan picked the field; now refine only
    # the finalists. Enrich the top-N qualified with kline (real ATR + higher-TF
    # trend) and funding, apply trend alignment + funding crowding, then re-rank.
    # Bounded call budget (~2 calls x enrich_top_n), graceful-degrades per symbol.
    enrich_n = max(int(cfg.get("enrich_top_n", 8)), 1)
    enriched = []
    # v0.9.9: trace the fate of the pre-enrichment BOARD LEADER (highest ticker-scored
    # qualified name). enrich_score OVERWRITES s.score, so the reason the scan leader
    # doesn't become the trade -- a trend/funding demotion or a hard skip -- was
    # invisible: SCAN showed the leader, the trade showed a different name, the step
    # between was dark. This is the answer to the 2026-07-03 08:49 question (ETH 87
    # led, TAO got the limit). leader_fate is emitted + folded onto the SCAN line.
    leader_sym = qualified[0].symbol if qualified else None
    leader_trace = None
    for s in qualified[:enrich_n]:
        pre = s.score
        features.enrich(s.features, cfg)
        adj, extra, skip, reason = scoring.enrich_score(s, s.features, cfg)
        s.dims = {**s.dims, **extra}
        s.score, s.skip, s.skip_reason = adj, skip, reason
        if s.symbol == leader_sym:  # capture the leader's pre/post/skip for the fate
            leader_trace = {"pre": pre, "post": adj, "skip": skip, "reason": reason,
                            "bps": extra.get("funding_bps")}  # v0.9.11: prove glitch vs real
        if not skip:
            enriched.append(s)
    enriched.sort(key=lambda s: s.score, reverse=True)
    if not enriched:
        # the leader itself was enrichment-skipped -> surface WHY, not a bare reason.
        return watch(top_symbol, "no_setup_after_enrichment",
                     {"tradable_candidates": 0,
                      "leader_fate": _leader_fate(leader_sym, leader_trace, None)})

    best = enriched[0]
    leader_fate = _leader_fate(leader_sym, leader_trace, best.symbol)
    # v0.9.20 vol-regime gate (OPT-IN; DEFAULT OFF as of v0.9.21). Stand aside when the
    # chosen best's annualized realized vol is outside [vol_floor, vol_ceiling].
    # SHIPPED OFF: validated on the 14-symbol replay set (capped maxDD, net up) but on
    # the LIVE 28-symbol universe it HALVES net every window (21/35/42d: +30/+69/+63% ->
    # +18/+35/+35%) for only a 6pt DD reduction -- the high-vol alts it refuses are the
    # winners here, not the drawdown drivers. Mechanism kept for per-universe opt-in via
    # the card. FAIL-OPEN (unreadable klines -> no gate) and cost-free when disabled: the
    # extra kline fetch happens ONLY when armed, ONLY for the one best.
    try:
        _vhi = float(cfg.get("vol_ceiling", "0") or "0")
        _vlo = float(cfg.get("vol_floor", "0") or "0")
    except (TypeError, ValueError):
        _vhi = _vlo = 0.0
    if _vhi > 0 or _vlo > 0:
        _k = str(cfg.get("kline_interval", "1h"))
        _vlb = int(cfg.get("vol_lookback", 30))
        _vb = features._closed_bars(
            features.fetch_klines(best.symbol, interval=_k, limit=_vlb + 3), _k)
        _rv = features.realized_vol(_vb, _vlb) if _vb else None
        if _rv is not None and ((_vlo > 0 and _rv < _vlo) or (_vhi > 0 and _rv > _vhi)):
            return watch(best.symbol, "vol_regime_%d" % int(round(_rv)),
                         {"tradable_candidates": len(enriched), "leader_fate": leader_fate})
    plan = risk.build_plan(best.features, cfg, best.size_factor, side=best.side,
                           entry_mode=best.entry_mode)
    if plan is None or not plan.sizing_ok:
        return watch(best.symbol, "sizing_failed",
                     {"tradable_candidates": len(enriched), "leader_fate": leader_fate})

    # Pre-placement staleness skip (v0.1.17): if the limit entry is already more
    # than limit_chase_pct from current price, it is not a fillable pullback -- in
    # a fast move VWAP lags and the entry lands far from market, so it would just
    # rest dead and clog a slot. Stand aside (WATCH) instead of placing it. Uses
    # the price already fetched for this candidate; no pending-order query needed,
    # so it is immune to the management-layer parse bug. The chase-guard remains a
    # backstop for orders that drift stale AFTER a fillable placement.
    # v0.5.0: only the pullback path rests a limit that can land too far from
    # price. A breakout enters at market by design (it IS extended past VWAP), so
    # the staleness skip must not veto it.
    chase_pct = float(cfg.get("limit_chase_pct", "3.0")) / 100.0
    cur = best.features.last
    if (plan.entry_mode != "breakout" and chase_pct > 0 and cur and cur > 0
            and plan.entry and plan.entry > 0):
        gap = ((plan.entry - cur) / plan.entry) if plan.side == "short" else ((cur - plan.entry) / plan.entry)
        if gap > chase_pct:
            return watch(best.symbol, "entry_too_far_{:.1f}pct".format(gap * 100.0),
                         {"tradable_candidates": len(enriched), "leader_fate": leader_fate})

    metrics = dict(base_metrics)
    metrics.update({
        "tradable_candidates": len(enriched),
        "side": plan.side,
        "entry_mode": plan.entry_mode,
        "limit_price": plan.entry,
        "sl_price": plan.sl_price,
        "sl_pct": round(plan.sl_pct * 100.0, 3),
        "tp1_price": plan.tp1,
        "tp2_price": plan.tp2,
        "notional_usdt": round(plan.notional_usdt, 2),
        "margin_usdt": round(plan.margin_usdt, 2),
        "leverage": plan.leverage,
        "sizing_ok": plan.sizing_ok,
        "kline_ok": bool(best.features.kline_ok),
        "funding_ok": bool(best.features.funding_ok),
        "trend_dir": best.features.trend_dir,
        "funding_now": best.features.funding_now,
        "universe": best.universe,
        # v0.9.9: null when the board leader IS the trade; else why it was passed
        # over (skip=<reason> / demote:pre->post / outrank:<leader>).
        "leader_fate": leader_fate,
        "analysis_version": ANALYSIS_VERSION,
        "thesis_source": THESIS_SOURCE,
    })
    meta = dict(base_meta)
    meta.update({
        "score_dims": best.dims,
        "atr14_est": plan.atr,
        "atr_source": "kline_wilder" if best.features.kline_ok else "range_proxy",
        "trend": {"dir": best.features.trend_dir,
                  "strength": round(best.features.trend_strength, 3)},
        "funding": {"now": best.features.funding_now, "avg": best.features.funding_avg},
        "size_factor": plan.size_factor,
        "ladder": {
            "tp1_pct": float(cfg.get("tp1_pct", "5.0")),
            "tp2_pct": float(cfg.get("tp2_pct", "15.0")),
            "tp1_size": 0.5, "tp2_size": 0.25, "runner_size": 0.25,
            "trail_atr": plan.trail_atr, "breakeven_price": plan.breakeven_price,
        },
        "sizing_note": plan.note or "sized_from_max_loss_usdt",
        "execution_note": "follow-trade places the limit entry with a stop and first target; "
                          "TP2/runner/trailing/breakeven are managed across scans",
    })
    confidence = max(0.0, min(1.0, best.score / 100.0))
    return {
        "action": plan.side,  # "long" or "short"
        "symbol": best.symbol,
        "confidence": confidence,
        "metrics": metrics,
        "meta": meta,
        "plan": {
            "side": plan.side, "entry": plan.entry, "sl_price": plan.sl_price,
            "tp1": plan.tp1, "tp2": plan.tp2, "entry_mode": plan.entry_mode,
            "margin_usdt": plan.margin_usdt, "notional_usdt": plan.notional_usdt,
        },
    }


def _breaker_token(mgmt: dict) -> str:
    """v0.9.8: a COMPACT breaker token for the visible DBG line. The SITREP tool
    reads only the dbg string (emit_signal's symbol field), not the metrics
    payload -- so the loss_breaker_threshold/headroom emitted since v0.9.7 were
    invisible, and the operator kept re-deriving the breaker by hand (the
    recurring equity*frac misread). This surfaces it where it is actually read:
      -b<headroom>  breaker armed, <headroom> = further realized loss to the trip
      -b!<over>     already TRIPPED (headroom <= 0), <over> past the threshold
      -b?<stage>    armed but BLIND this cycle; v0.9.25 names the failing stage
                    (r = fills read failed, t = no row timestamp parsed,
                     k = in-window fills carry no recognised profit field,
                     e = fills read EMPTY on a state-blind cycle -- "empty" is
                     untrustworthy while sibling reads fail, v0.9.36) so a
                    chronic blind diagnoses itself from the feed
      ""            breaker disabled (frac 0) -- nothing misleading emitted
    """
    hr = mgmt.get("loss_breaker_headroom")
    if hr is not None:
        return "-b{}".format(int(round(hr))) if hr > 0 else "-b!{}".format(int(round(-hr)))
    if mgmt.get("loss_breaker_threshold") is not None:
        tok = "-b?" + str(mgmt.get("loss_breaker_blind") or "")[:1]
        # v0.9.26: a t-blind carries the time-key probe (has:/alt:<key>) so the
        # feed names the SDK's actual field -- the fix becomes a one-line key add.
        probe = str(mgmt.get("loss_breaker_probe") or "")
        if ":" in probe:
            tok += "." + probe.split(":", 1)[1][:6]
        return tok
    return ""


def _blind_token(mgmt: dict) -> str:
    """v0.9.14 (observability-audit HIGH): state_blind means management could not
    read the book this cycle -- open_if_allowed refuses new entries AND open_count
    / actions are unreliable. It had NO compact token, so a blind cycle looked
    identical to a flat idle one (own0-act0-none). Name WHY the read failed, in
    priority order (management crash > position query raised > pending query raised
    > margin-locked-but-read-empty > non-raising reject envelopes). Returns "" when
    the book read cleanly."""
    if not mgmt.get("state_blind"):
        return ""
    if mgmt.get("mgmt_error"):
        return "bl.crash:" + str(mgmt["mgmt_error"])[:10]
    if mgmt.get("position_query_error"):
        return "bl.posq:" + str(mgmt["position_query_error"])[:10]
    if mgmt.get("pending_error"):
        return "bl.pendq:" + str(mgmt["pending_error"])[:10]
    if mgmt.get("blind_reason"):
        return "bl.margin"   # positions read empty but margin still locked (read-lie)
    if mgmt.get("position_query_reason"):
        return ("bl.posrej:" + str(mgmt["position_query_reason"]).replace(" ", "_"))[:22]
    if mgmt.get("pending_reason"):
        return ("bl.pendrej:" + str(mgmt["pending_reason"]).replace(" ", "_"))[:22]
    return "bl.?"


def _circuit_state_token(mgmt: dict) -> str:
    """v0.9.14 (observability-audit HIGH): the equity circuit breaker
    (circuit_pause_usdt/circuit_stop_usdt) needs day_start_equity to round-trip
    through .state/ across cycles. controls_active.circuit_breaker reports True
    whenever equity is merely READABLE this cycle -- so it claimed 'active' even
    when .state/ is ephemeral and day_start_equity is reset to current equity every
    run (today_pnl == 0 forever, the breaker can never trip). state_runs is the
    persistence probe: stuck at <=1 => .state/ does not carry => the equity circuit
    is non-functional. Emit -cx ONLY in that broken case (self-clears the moment
    runs climb); the fills-based loss_breaker (-b) is the real protection."""
    ca = mgmt.get("controls_active")
    if not isinstance(ca, dict):   # defensive: a malformed/absent controls map is never a warning
        return ""
    runs = mgmt.get("state_runs")
    if ca.get("circuit_breaker") and isinstance(runs, (int, float)) and runs <= 1:
        return "-cx"
    return ""


_WATCH_SHORT = {
    "all_regimes_neutral": "neutral",
    "no_setup_at_or_above_min_score": "lowscore",
    "no_setup_after_enrichment": "enrich0",
    "sizing_failed": "sizefail",
    "circuit_paused": "cbpause",
    "circuit_tripped": "cbtrip",
}


def _watch_short(reason: str) -> str:
    """v0.9.14 (observability-audit HIGH): the six build_decision stand-downs all
    collapsed to tail 'none', because full_reason is sourced only from
    open_if_allowed (which never runs on a watch action). Map the known verbose
    reasons to compact codes so the visible line finally answers 'why did the cycle
    place nothing'; dynamic reasons (entry_too_far_Npct) pass through truncated."""
    r = str(reason or "")
    if r in _WATCH_SHORT:
        return _WATCH_SHORT[r]
    if r.startswith("entry_too_far"):
        return r.replace("entry_too_far_", "far:")[:16]
    return r.replace(" ", "_")[:16]


def _held_token(mgmt: dict, cfg: dict) -> str:
    """v0.9.14 (observability-audit HIGH): when a position is held but no management
    action fires this cycle (the steady state), the compact line showed only
    own1-act0 -- the position's live state (how far in profit, whether breakeven /
    lock / scale-out is armed, how close to the time-stop) lived only in
    position_diag, which the SITREP never reads. Surface the OLDEST managed position
    (closest to the time-stop):
        hld.<sym><+move%><flags>.t<age>/<max>
      flags: a=breakeven armed  l=lock floored the stop  s=scaled/arming  r=trail set
      .t<age>/<max> = hours held / time_stop_hours ceiling (.t?/<max> if open-time
      unreadable -> the time-stop is blind on that position). Returns "" when nothing
      managed is held."""
    diags = mgmt.get("position_diag")
    if not isinstance(diags, list):   # defensive: only a list of diag dicts is ever iterable here
        return ""
    # move_pct must be numeric (not just present) so the int(round()) below can never raise on
    # a malformed diag -- this is a live DBG path; a crash here would drop the cycle's emit.
    managed = [d for d in diags if isinstance(d, dict) and isinstance(d.get("move_pct"), (int, float))]
    if not managed:
        return ""
    d = max(managed, key=lambda x: (x.get("age_h") is not None, x.get("age_h") or 0.0))
    sym = str(d.get("sym", "?")).replace("USDT", "")[:4]
    flags = ""
    if d.get("be_armed"):
        flags += "a"
    if d.get("be_lock") is not None:
        flags += "l"
    if str(d.get("so", "")).startswith("trimmed") or d.get("so") == "armed":
        flags += "s"
    if str(d.get("trail", "")).startswith("set:"):
        flags += "r"
    # v0.9.37 (ETH-short 12h incident): surface which HOLD CLOCK governs, from the
    # tp2-width mode recovery -- P = pullback (4h cap), B = breakout cap, nothing =
    # unknown/global. Mode recovery failing live was INVISIBLE (tmode sat in the
    # metrics payload the SITREP never reads) until a pullback ran to the 12h
    # global cap. One char here makes the clock auditable from the feed itself:
    # a pullback position whose hld token lacks 'P' is running the WRONG clock.
    _tm = str(d.get("tmode", "") or "")
    if _tm == "pullback":
        flags += "P"
    elif _tm == "breakout":
        flags += "B"
    # v0.9.18 fix: render the age as ".t<hours>h", NOT ".t<age>/<cap>". The cap
    # (time_stop_hours) is a fixed config constant, and appending it pushed a
    # flags-bearing held token + a full 3-universe digest to 64 chars, so the fold's
    # 63-char clip sheared the last digit of "/12" -> a misleading "/1" that read as
    # "time-stop at 1h, overdue" (the live LAB runner false alarm). Elapsed hours is
    # the actionable number and can never mis-clip; the 12h ceiling is documented.
    tok = "hld.%s%+d%s" % (sym, int(round(d["move_pct"])), flags)
    if d.get("ts_ok") and d.get("age_h") is not None:
        tok += ".t%dh" % int(d["age_h"])
    else:
        tok += ".t?h"     # open-time unreadable -> the time-stop is blind on this position
    return tok[:32]


def _leader_fate(leader_sym, trace, placed_sym):
    """v0.9.9: why the pre-enrichment BOARD LEADER (top ticker-scored qualified name)
    did not become the trade -- the step that was dark between the SCAN line (shows
    the leader) and the trade signal (shows a different name). enrich_score overwrites
    the score, so this is the only way to see it. Returns None when the leader IS the
    trade (nothing to explain) or is unknown. Otherwise, in priority order:
      skip=<reason>     hard-skipped in enrichment (funding-crowded, no-data, ...)
      demote:pre->post  trend/funding penalty dropped its score below the field
      outrank:<leader>  survived enrichment but a lower name's alignment beat it
    This is the 2026-07-03 08:49 answer (ETH 87 led, TAO took the limit)."""
    if not leader_sym or trace is None or placed_sym == leader_sym:
        return None
    if trace.get("skip"):
        reason = str(trace.get("reason") or "?")
        # v0.9.11: a funding-crowded skip carries the ACTUAL funding bps, because
        # the skip only fires above ~30 bps while real funding on these perps runs
        # <=~3 bps -- so a fired skip is almost certainly a data glitch (e.g. the
        # crypto funding feed returning anomalous values for RWA equity perps whose
        # true funding is 0). The number proves glitch (fcr+47) vs real (>30).
        bps = trace.get("bps")
        if reason.startswith("funding") and bps is not None:
            return "skip=fcr%+d" % round(bps)
        return "skip=" + reason[:10]
    pre, post = trace.get("pre"), trace.get("post")
    if pre is not None and post is not None and post < pre - 0.5:
        return "demote:%d->%d" % (round(pre), round(post))
    return "outrank:" + str(leader_sym).replace("USDT", "")[:5]


def _scan_digest(scans: list, min_score: float) -> str:
    """v0.9.8: compact per-universe scan summary for the VISIBLE feed. The SITREP
    reads signal symbol strings, not the metrics payload -- so per-name scores
    were invisible and a missed trend (crypto ripping while the bot sat out on an
    equity leg) could not be diagnosed from the operator's surface. One token per
    universe:
      <abbr>:n                       leader regime neutral -> universe stood down
      <abbr>:<L|s>-                  gated, but no candidates scored
      <abbr>:<L|s><SYM><score><q|x>  gated long/short; best candidate + score;
                                     q = qualified (>= min_score, not skipped),
                                     x = below the floor or hard-skipped
    e.g. SCAN-cry:LETH62x-met:n-equ:LMSTR78q => crypto gated LONG but ETH's 62 was
    below the 70 floor (the answer to 'why no crypto long in a +6% tape'); metals
    neutral; equities gated long and MSTR qualified at 78. This makes the
    pullback-fill / overextension hypotheses checkable from the feed alone."""
    parts = []
    for sc in scans:
        abbr = str(sc.get("name", "?"))[:3]
        d = sc.get("direction")
        gate = "L" if d == "long" else ("s" if d == "short" else "n")
        if gate == "n":
            parts.append("{}:n".format(abbr))
            continue
        scored = sc.get("scored") or []
        if not scored:
            parts.append("{}:{}-".format(abbr, gate))
            continue
        best = max(scored, key=lambda s: getattr(s, "score", 0) or 0)
        sym = str(getattr(best, "symbol", "?")).replace("USDT", "")[:4]
        score = int(round(getattr(best, "score", 0) or 0))
        ok = (not getattr(best, "skip", False)) and score >= min_score
        parts.append("{}:{}{}{}{}".format(abbr, gate, sym, score, "q" if ok else "x"))
    return ("SCAN-" + "-".join(parts))[:63]


def _fold_exec_onto_scan(scan_digest: str, own, pT, bkr: str, cbx: str,
                         tail: str, fate, follow: bool = True) -> str:
    """v0.9.17: the SITREP tool surfaces only the LAST per-cycle close-signal. Every
    cycle emits DBG (the exec line) and then SCAN -- both action="close" -- so the
    SCAN emit clobbers the DBG emit, and the exec-state (own/pending, the action
    tail, the breaker headroom, the -cx dead-circuit warning) has been INVISIBLE on
    the operator's surface since v0.9.8 first added the SCAN line after DBG. The
    whole DBG-token investment (the v0.9.14 hld./no./bl. tails included) was landing
    where the feed overwrites it. Fold the critical exec tokens onto the SCAN line --
    the one surface that reaches the operator:
        SCAN-<digest>|o<own>p<pend><breaker><cx>-<tail>[|<fate>]
    Budget (63-char signal-symbol cap): the digest and the exec tail are never
    sacrificed. The breaker headroom is dropped first under pressure (it stays on the
    DBG metrics + token), then the leader_fate is appended only if it still fits.
    DBG still emits unchanged for the metrics payload / deep debug.

    v0.9.18: a `nof` marker leads the exec segment on a NON-follow cycle (eval run /
    outside the execution window) so an opaque pre-window line (o0p?-none) announces
    itself as 'not following, so it scanned but did not trade' instead of reading as a
    malfunction. Follow cycles (the normal case) stay clean -- no marker."""
    lead = "" if follow else "nof-"
    def _build(bk: str) -> str:
        return "%s|%so%sp%s%s%s-%s" % (scan_digest, lead, own, pT, bk, cbx, tail)
    line = _build(bkr)
    if len(line) > 63 and bkr:           # protect the tail: shed breaker detail first
        # v0.9.27: degrade GRACEFULLY before dropping. The probe-suffixed blind
        # form (-b?t.<key>) rarely fits a 3-universe line, and shedding it whole
        # hid the blind/sighted verdict exactly when the investigation needed it
        # (the 11:47 absence). Trim to the 4-char stage form (-b?t / -b18) first;
        # drop entirely only if even that cannot fit.
        line = _build(bkr[:4])
        if len(line) > 63:
            line = _build("")
    if fate:
        cand = "%s|%s" % (line, fate)
        if len(cand) <= 63:              # fate only if it still fits; never truncates the tail
            line = cand
    return line[:63]


def _dbg_tail(blind, acts, act_label, xpd, held, watch_reason,
              action, symbol, pT, preason, full_reason, rshort):
    """The DBG/SCAN tail selector, extracted from run() so the priority chain is
    UNIT-TESTABLE. It regressed twice while inline (a mis-ordered branch silently
    shadowed a more informative one), so it now lives here with golden tests. First
    match wins, most-urgent first:
      bl.<why>   state_blind -- the book couldn't be read (own/act unreliable)
      act.<t>    a management action fired this cycle
      xpd.<d>    a stuck owned-pending order the bot can't time-expire
      hld.<...>  a position is held and quiet -- its live state
      no.<r>     a watch stand-down -- WHY the cycle placed nothing
      perr.<c>   the pending fetch genuinely FAILED (pT==0 + a reason)
      sig.<Ls>   an actionable decision whose open path gave NO reason -- name the
                 intended trade. GATED on full_reason == 'none' so a REAL open-path
                 reason (entry_already_pending, cooldown, sizing_failed, ...) is never
                 masked -- it wins via rshort below. This gate is the fix for the
                 v0.9.18 regression that shadowed entry_already_pending with sig.LMSTR.
      <rshort>   fallback: the raw open-path reason / decided outcome."""
    if blind:
        return blind
    if acts and act_label:
        return "act." + str(act_label)[:28]
    if xpd:
        return "xpd." + xpd
    if held:
        return held
    if watch_reason:
        return "no." + _watch_short(watch_reason)
    if str(pT) == "0" and preason:
        return "perr." + preason
    if action in ("long", "short") and str(full_reason) == "none":
        return "sig." + ("L" if action == "long" else "s") + str(symbol).replace("USDT", "")[:5]
    return rshort


def _safe_manage(cfg: dict, follow: bool) -> dict:
    """Management snapshot, or a BLIND fallback. v0.9.4 (audit S-1): only a
    manage_open_state that RAN TO COMPLETION may authorize opens. The old
    fallback ({"circuit": "ok"}) read as a flat book -- open_count 0, no
    state_blind -- so a crash anywhere in the management layer silently disabled
    the concurrency cap, the correlation budget, and the loss breaker while
    still placing new entries. That is the same fail-open the v0.6.5 interlock
    exists to stop, reintroduced one layer up. Now: management never ran, or
    raised => state_blind => open_if_allowed refuses to ADD this cycle."""
    if not follow:
        return {"circuit": "ok", "state_blind": True, "mgmt_skipped": "not_follow_trade"}
    try:
        return execution.manage_open_state(cfg)
    except Exception as exc:
        return {"circuit": "ok", "state_blind": True, "mgmt_error": type(exc).__name__}


def run() -> None:
    cfg = _cfg()
    try:
        follow = runtime.is_follow_trade()
    except Exception:
        follow = False
    try:
        exec_mode = str(runtime.execution_mode())
    except Exception:
        exec_mode = "?"

    mgmt = _safe_manage(cfg, follow)

    decision = build_decision(cfg, mgmt)
    decision["meta"]["follow_trade"] = follow

    # v0.1.7 diagnostic: keep the sanctioned emit_signal_or_follow trade path, but
    # capture (a) whether the runtime actually invoked our trade callback and
    # (b) the placement result, via a closure. Then emit a second, readable signal
    # encoding it -- the catalog exposes only action/symbol/confidence, so the
    # diagnosis is packed into those.
    captured = {"called": False}

    def _execute():
        captured["called"] = True
        try:
            res = execution.open_if_allowed(decision, cfg, mgmt) or {}
        except Exception as exc:
            res = {"placed": False, "reason": "exc_" + type(exc).__name__}
        captured.update(res)
        return res

    runtime.emit_signal_or_follow(
        action=decision["action"],
        symbol=decision["symbol"],
        confidence=decision["confidence"],
        metrics=_sanitize(decision["metrics"]),
        meta=_sanitize(decision["meta"]),
        execute_trade=_execute,
    )

    # v0.1.15 MANAGEMENT DIAGNOSTIC. Two stale-limit-prune fixes failed, so stop
    # guessing and surface every link of the management chain on a readable
    # (actionable-primary) channel: did manage_open_state run at all (f =
    # is_follow_trade), how many live pending orders it saw (pT) vs recognised as
    # ours by size (oP / own), what it actually did (act), and any error (e) --
    # plus the trade outcome (c/p/reason). Plain emit_signal on a sentinel symbol,
    # so it trades nothing; it only reports. Decode:
    #   f0...      -> management never ran (is_follow_trade gate) <- bug is the gate
    #   f1-own0-pT>0 -> ran but recognised none of our orders <- size-scoping
    #   f1-own>0-act0 with a stale order live -> chase-guard logic <- prune/extract
    called = bool(captured.get("called"))
    placed = captured.get("placed")
    full_reason = str(captured.get("reason", "")) or "none"
    # v0.5.0: a placed breakout market entry shows c{c}pB (vs p1 for a pullback
    # limit), so a momentum entry is unmistakable on the compact line.
    pcode = "1" if placed is True else ("0" if placed is False else "X")
    if placed is True and str(captured.get("entry_mode", "")) == "breakout":
        pcode = "B"
    emc = {"follow_trade": "F", "signal_only": "S"}.get(exec_mode, "?")
    own = mgmt.get("open_count", 0)
    pT = mgmt.get("pending_total", "?")
    oP = mgmt.get("owned_pending", "?")
    mgmt_actions = mgmt.get("actions", []) or []
    acts = len(mgmt_actions)
    # v0.3.1: each action is {"<type>": symbol, ...}; surface the first action's
    # type so a fired chase/expiry/circuit/TP shows WHAT happened in the tail,
    # not just a bare act{N} (the verdict ambiguity hit live twice on 0.3.0).
    act_label = ""
    if mgmt_actions and isinstance(mgmt_actions[0], dict) and mgmt_actions[0]:
        act_label = str(next(iter(mgmt_actions[0].keys())))
    merr = str(mgmt.get("mgmt_error") or mgmt.get("position_query_error") or "ok")[:16]
    rshort = full_reason.replace(" ", "_")[:32]
    pshape = str(mgmt.get("pending_shape", ""))[:30]
    preason = str(mgmt.get("pending_reason", ""))[:24]
    # v0.4.2: a stuck owned-pending order that should have time-expired but did not
    # (no parseable open-time, or the cancel API rejected). This is the silent
    # failure that left a limit resting 5h+ on Classic with act0 and no cancel.
    xpd = str(mgmt.get("expiry_diag", ""))[:26]
    # Tail priority (v0.3.1, extended v0.9.14 by the observability-audit). First
    # match wins; each names WHAT the cycle actually did, most-urgent first:
    #   1. bl.<why>        -- state_blind: the book could not be read, so own/act are
    #      unreliable and no new entry is allowed (dominates -- flying blind).
    #   2. act.<type>      -- a management action fired (stale_limit_cancel,
    #      limit_expiry_cancel, circuit_cancel, time_stop_close, auto_be, ...).
    #   3. xpd.<diag>      -- a stuck owned-pending order the bot cannot time-expire.
    #   4. hld.<sym...>    -- a position is held and quiet: its live state (move /
    #      breakeven / lock / scale / trail / time-stop age) -- the steady state.
    #   5. no.<reason>     -- a watch stand-down: WHY the cycle placed nothing (was
    #      invisible as 'none', since full_reason only fills on an actionable open).
    #   6. perr.<code:msg> -- the pending fetch genuinely FAILED (actionable error).
    #   7. <reason>        -- fallback: the raw open-path reason / decided outcome.
    # The full envelope shape is preserved in metrics (pending_shape) for deep debug.
    blind = _blind_token(mgmt) if follow else ""
    held = _held_token(mgmt, cfg)
    watch_reason = (str(decision.get("meta", {}).get("reason", ""))
                    if decision.get("action") == "watch" else "")
    tail = _dbg_tail(blind, acts, act_label, xpd, held, watch_reason,
                     decision.get("action"), decision.get("symbol", "?"),
                     pT, preason, full_reason, rshort)
    # v0.9.8: the breaker token rides between the trade block and the tail so the
    # headroom is visible in the compact line (the only surface the SITREP reads).
    # v0.9.14: -cx follows it iff the equity circuit claims active on a .state/ that
    # does not persist (its day_start_equity never round-trips -> it can never trip).
    bkr = _breaker_token(mgmt)
    cbx = _circuit_state_token(mgmt)
    dbg = ("DBG-f{f}{em}-own{own}-pT{pt}-oP{op}-act{a}-c{c}p{p}{bk}{cx}-{t}"
           .format(f=int(follow), em=emc, own=own, pt=pT, op=oP, a=acts,
                   c=int(called), p=pcode, bk=bkr, cx=cbx, t=tail))[:63]
    runtime.emit_signal(
        action="close", symbol=dbg, confidence=0.222,
        metrics={"dbg": dbg, "follow": follow, "exec_mode": exec_mode,
                 "open_count": own, "pending_total": pT, "owned_pending": oP,
                 "mgmt_actions": mgmt.get("actions", []), "mgmt_error": merr,
                 "pending_reason": mgmt.get("pending_reason", ""),
                 "pending_shape": mgmt.get("pending_shape", ""),
                 "expiry_diag": mgmt.get("expiry_diag", ""),
                 "pending_max_age_h": mgmt.get("pending_max_age_h"),
                 "position_diag": mgmt.get("position_diag", []),
                 "state_runs": mgmt.get("state_runs"),
                 # v0.9.14 observability: surface the blind read + its reason so the
                 # -cx / bl. tokens on the compact line are reconstructable in metrics.
                 "state_blind": mgmt.get("state_blind", False),
                 "blind_reason": mgmt.get("blind_reason") or mgmt.get("position_query_reason")
                 or mgmt.get("pending_reason"),
                 # v0.9.0 observability: emit each control's running state every cycle
                 # (not only when it fires) so an INERT control is visible -- the trail
                 # was dead a whole session because nothing surfaced its status.
                 "controls_active": mgmt.get("controls_active", {}),
                 "loss_breaker": mgmt.get("loss_breaker", False),
                 "realized_window_pnl": mgmt.get("realized_window_pnl"),
                 # v0.9.7 observability: the breaker's own arithmetic, emitted so
                 # the SITREP never re-derives it (threshold = frac*margin*lev;
                 # headroom = further realized loss to the trip point).
                 "loss_breaker_threshold": mgmt.get("loss_breaker_threshold"),
                 "loss_breaker_headroom": mgmt.get("loss_breaker_headroom"),
                 # v0.9.1 Phase-4 live journal: closed-trade realized records for
                 # live-vs-backtest reconciliation (accrues over cycles via metrics).
                 "fills_journal": mgmt.get("fills_journal", []),
                 "analysis_version": ANALYSIS_VERSION,
                 "thesis_source": THESIS_SOURCE,
                 "called": called, "placed": placed, "reason": full_reason[:120]},
        meta={"dbg": dbg, "mgmt": _sanitize(mgmt)},
    )

    # v0.9.8: a third, dedicated SCAN signal so the per-universe digest is visible
    # in the operator's feed (which reads symbol strings, not metrics). This is the
    # surface that answers 'why did the bot sit out a moving universe' -- gate
    # direction + best candidate + score + qualified/skipped, per universe.
    scan_digest = str(decision.get("meta", {}).get("scan_digest", "SCAN-none"))
    # v0.9.9: fold the board-leader's fate onto the SCAN line so a passed-over
    # leader (the 08:49 ETH->TAO case) names its own reason in the visible feed.
    fate = decision.get("metrics", {}).get("leader_fate")
    # v0.9.17: fold the DBG exec-state (own/pending, the action tail, breaker, -cx)
    # onto the SCAN line too -- the operator's tool surfaces only this (last) close-
    # signal, so the separate DBG emit was never seen. Reuses the exact tokens the
    # DBG line above computed (own/pT/bkr/cbx/tail).
    scan_line = _fold_exec_onto_scan(scan_digest, own, pT, bkr, cbx, tail,
                                     str(fate) if fate else None, follow=follow)
    try:
        runtime.emit_signal(
            action="close", symbol=scan_line, confidence=0.111,
            metrics={"scan_digest": scan_line,
                     "regime_by_universe": decision.get("metrics", {}).get("regime_by_universe"),
                     "best_score": decision.get("metrics", {}).get("best_score"),
                     "min_score": decision.get("metrics", {}).get("min_score"),
                     "leader_fate": fate,
                     "analysis_version": ANALYSIS_VERSION},
            meta={"scan_digest": scan_line},
        )
    except Exception:
        pass  # the SCAN line is diagnostic-only; never let it break the cycle


if __name__ == "__main__":
    run()
