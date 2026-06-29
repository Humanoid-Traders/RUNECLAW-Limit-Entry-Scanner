# RUNECLAW v0.9.0 — Regime framework, value-tested (entry-gating does NOT validate)

**Status:** research-only (`research/replay_mp.py`). No `src/**` change, no deploy.
This applies the validation loop to the **Part-3 regime framework** — the proposed
"classify the market, gate entries by regime" upgrade — before building it live.

The headline: **the regime *entry-gating* does not validate. Do not ship it.** This
is the loop doing its job a fourth time. Across the whole v0.8–v0.9 line, every
*entry-side* portfolio idea has been rejected by data, and the only validated win is
a *risk-side* control:

| candidate | axis | verdict |
|---|---|---|
| Correlation-weighted exposure cap | entry gate | ❌ rejected (legacy count cap dominates the tail) |
| Concurrent aggregate-heat breaker | entry gate | ❌ rejected (never fires; tail is sequential) |
| **Realized rolling-loss breaker** | **risk control** | ✅ **validated, shipped (v0.8.0)** |
| Leader efficiency-ratio chop/range gate | entry gate | ❌ rejected (removes edge; anti-predictive) |

**The pattern is the finding:** RUNECLAW's edge lives in its existing per-symbol
selection (scoring + regime direction). Blunt regime/portfolio *entry* filters
discard good trades faster than bad ones. The productive upgrade axis is **risk and
exit control**, not adding entry gates.

## 1. The chop/range no-trade gate — REJECTED

Part 3 proposes standing aside in "range" and "high-vol chop." Implemented as a
leader-level **Kaufman efficiency ratio** gate (`|net move| / Σ|bar move|` over the
trailing window; ~1 = clean trend, ~0 = directionless) — one number that captures
both no-trade regimes. A/B at the live config (breakout, trail 2×ATR, ts12), two
windows, floors 0.15–0.45:

| 20d | net | maxDD | PF |
|---|---|---|---|
| **NO gate** | **+33.4%** | −9.3% | **1.94** |
| er-floor 0.25 | +13.4% | −11.1% | 1.48 |
| er-floor 0.45 | +2.5% | −8.6% | 1.12 |

| 35d | net | maxDD | PF |
|---|---|---|---|
| **NO gate** | **+24.0%** | −29.4% | 1.25 |
| er-floor 0.15 | **−20.4%** | **−40.7%** | **0.77** |
| er-floor 0.35 | +2.8% | −17.8% | 1.06 |

Two damning reads:
- **20d: every floor is worse than no-gate on both return and PF.** The gate only
  removes good trades — there is no setting that helps.
- **35d at a tight floor is catastrophic:** er 0.15 flips +24.0% → **−20.4%** *and*
  worsens maxDD (−29 → −41). The trades the gate *keeps* (high leader-ER) are worse
  than the ones it removes — the leader-ER signal is **anti-predictive** here.

Why: individual alts break out while the leader (BTC) is consolidating; a
leader-level directionality filter throws those away. The existing `scoring.regime`
+ per-symbol score already handle direction better than a crude aggregate gate. Only
a very aggressive floor (0.45) improves the *tail*, and even then net return stays
below no-gate. **Not built.**

## 2. Per-symbol edge audit — directional only, no confident prune

`--by-symbol` (35d, majors) shows edge concentrating but on thin samples:

| symbol | net | n | avg | note |
|---|---|---|---|---|
| INJUSDT | +17.1% | 12 | +1.43% | carries edge |
| WLDUSDT | +9.3% | 18 | +0.52% | carries edge (by volume) |
| ETHUSDT | −1.2% | 24 | −0.05% | ~flat despite most trades |
| NEARUSDT | −2.7% | 14 | −0.19% | mild bleeder |

One window, most names n < 5 → **not a prune list.** The honest move is to bank
LIVE per-trade data (the journal) and re-audit on real fills + multiple windows,
not to cut names on a single thin backtest window. Premature pruning is its own
overfitting.

## 3. What this means for the Part-3 framework

The regime *concept* is sound; the regime-as-**entry-filter** *implementation* is
not (the data rejects it). The parts of Part 3 that survive contact with data:

- **No-trade on low volume** — already effectively enforced by `min_volume_usdt`.
- **No-trade on event risk (RWA earnings/macro)** — still worth a calendar blackout
  (untested here because it's a data-feed gap, not an entry-logic question; it's a
  *risk* guard, not an edge filter — the axis that validates).
- **Risk-% / leverage *coupling* to regime** — a risk-side idea (cap leverage in
  weak/uncertain regimes), distinct from entry-gating. Plausibly additive, but
  **untested** — it must be A/B'd in the harness before shipping, exactly like the
  loss breaker. Not built on faith.

What is explicitly **NOT** recommended: gating *which entries fire* by an aggregate
regime classifier. Four independent tests now say RUNECLAW's selection should be
left alone; the gains are on the risk/exit/observability axis.

## 4. The validated roadmap (data-driven, not aspirational)

1. **Risk control — done:** realized-loss breaker (v0.8.0). Enable it live.
2. **Risk posture — operator:** rotate keys; trial isolated margin (signal-only /
   tiny) before defaulting it for RWA — isolated is still UNTESTED live.
3. **Observability:** surface control-fired state (trail ratchet, breaker, blind
   interlocks) so an inert feature can never hide a whole session again (the trail
   was dead pre-v0.6.7). Safe, additive.
4. **Live journal (Phase 4):** the prerequisite for an honest per-symbol audit,
   live-vs-backtest reconciliation, and any future regime-*risk* coupling test.
5. **Event blackout for RWA:** a calendar feed → no-trade window (risk guard).

Net: "do all in best order" tested the whole regime-entry-gating roadmap and the
data said don't ship it — which is the point of having the loop. The order that
remains is risk-and-observability first, and every new idea earns its way in through
`replay_mp` before it touches live money.
