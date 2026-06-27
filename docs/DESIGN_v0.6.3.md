# RUNECLAW v0.6.3 — Trailing-Stop Exit (IMPLEMENTED, published; trial in signal_only)

**Status:** implemented + published (`version_id 4ad76c01…`). **Live instance stays on v0.6.2** until trialed in `signal_only` (or tiny-size follow_trade) — the live-exit rewrite is built safe-by-construction (below) but rests on approximate-sim evidence, so it earns a careful live read before normal size.

**Validation caught a real trap — the backstop width.** The first pass attached a tight tp2 (7%) backstop, which *capped the trail's winners* and erased the edge (lost to breakout-fixed in 2 of 3 samples). Re-validating live-faithfully across backstop widths showed the trail only pays with a WIDE backstop:

| sample (3-slot) | breakout-fixed | trail + 7% backstop | trail + **15% backstop** |
|---|---|---|---|
| MAJORS 21d | +1.4% | +11.3% | **+12.3%** |
| MOVERS 28d | +44.0% | +16.7% (capped!) | **+56.1%** |
| MIXED 35d | +36.4% | +28.0% | +32.5% |

So the shipped config is `trail_atr_mult 2.0`, `time_stop_hours 12`, **`tp2_pct 15.0`** (wide backstop). The trail beats pullback-only in all 3 samples and breakout-fixed in 2 of 3, and is *consistently* positive where breakout-fixed swings −13.6%..+44%.

---

## 1. Finding that motivates it (multi-position replay)

The single-position replay said "trailing stop is too noisy." The **3-slot multi-position replay** (`research/replay_mp.py`, honoring `max_concurrent` + the Rule-7 correlation budget like live) reversed that — the single-position view was a concurrency artifact. Validated across 3 independent samples, net of fees:

| sample | pullback-only (fixed) | breakout+fixed | **breakout+trail 2×ATR / ts12** |
|---|---|---|---|
| MAJORS 21d | +6.4% | +1.4% | **+12.3%** |
| MOVERS 28d | +34.9% | +43.2% | **+47.3%** |
| MIXED 35d | +14.0% | +36.4% | +30.7% |

And in a 30d window the contrast was starkest: breakout+**fixed** −13.6% vs breakout+**trail** **+26.6%**.

**Reads:**
- A trailing stop applied to **all** positions beats the current fixed-TP exit in every sample (no per-mode differentiation needed).
- Breakout-with-fixed-exit is wildly inconsistent (−13.6% to +43.2%); the momentum thesis only pays when winners can run.
- The win is the **breakout** leg under a trail; pullback-trail is slightly worse than pullback-fixed but the breakout gain dominates the portfolio.

Caveat: approximate sim — no order book, **bar-touch fills including the trail exit** (optimistic: assumes the trail fills exactly at its level). Magnitudes will not transfer; the **ranking** (trail > fixed) is the signal.

---

## 2. Design — a STATELESS trailing stop

Key insight: a trailing stop needs no `.state/` (which doesn't persist on this runtime anyway). The **exchange SL order is the trail state** — it only ratchets in the favorable direction, so each cycle we recompute the candidate trail and move the SL **only if** that's more protective than the current SL.

Per managed position, each scan cycle:
```
atr     = Wilder ATR (recomputed live for the symbol)
price   = current contract price
trail   = price - trail_atr_mult*atr        (long)   |  price + trail_atr_mult*atr (short)
cur_sl  = current SL plan-order trigger price
if (long  and trail > cur_sl) or (short and trail < cur_sl):
    modify_stop_loss(symbol, sl_order_id, align(trail))   # ratchet UP only
```
This subsumes auto-breakeven (the trail crosses entry as price runs) and lets winners ride.

**Entry change:** the tight TP1 attached at entry would cap the trail, so the entry should attach the **wider TP2** (or no TP) as a backstop and let the trail do the work. (`open_*_market/limit` take `tp_trigger_price` — pass tp2.)

**Config (validated):** `trail_atr_mult` 1.0 → **2.0**, `time_stop_hours` 4 → **12** (the trail needs room).

---

## 3. Implementation plan + the RISKS that gate it

`_best_effort_position_controls`: replace the auto-BE block with `_trail_stop(...)`, generalizing the existing `_move_stop_to_breakeven` (which already reads the SL plan order and calls `modify_stop_loss`).

**Unknowns to resolve BEFORE trusting it on live money:**
1. **Current-SL read.** Ratcheting safely requires reading the SL plan-order's trigger price. Its field shape on this account is unconfirmed — and a wrong read could move the SL the wrong way or onto a no-op that leaves the position unprotected. Must read it robustly (try multiple keys; coerce) and **fail-safe: if the current SL can't be read, do nothing** (never blind-set).
2. **ATR at manage-time.** Recompute via `features.fetch_klines` + `_wilder_atr` (≤3 positions → ≤3 extra calls/cycle). Fail-safe to no-op if klines/ATR unavailable.
3. **`modify_stop_loss` reliability** on this account is untested. Wrap in try/except; never leave a position without its existing SL (only ever ratchet, never cancel-then-replace).
4. **Idempotence / churn.** Only modify when the trail moves a meaningful tick (avoid hammering `modify_stop_loss` every cycle for sub-tick moves).

**Hard safety rule:** the trailing logic must be strictly additive — it can only move an existing SL in the protective direction. It must never cancel, widen, or remove a stop, and any parse/API failure is a silent no-op (the existing fixed SL stays in force).

---

## 4. Validation plan (do not skip)

1. Implement with the fail-safes above; unit-test the ratchet decision (up-only, both sides, no-op on unreadable SL).
2. Re-run `replay_mp.py` to confirm the live-faithful implementation reproduces the validated edge.
3. Publish v0.6.3; trial in **`signal_only`** (or a tiny-`max_loss` follow_trade) and watch the new `position_diag` to confirm the SL actually ratchets as intended on real fills.
4. Only after a clean read, run it at normal size.

Until then: **v0.6.2 stays live.** The fixed-TP exit is suboptimal per the sim but safe and understood; the trail is the upside, gated behind a careful build.

---

**Deferred siblings:** breakout-only trail (needs per-position entry_mode, which the stateless runtime can't carry — and the validated config trails all positions anyway, so not required); `tp1`/`tp2` re-tuning under the trail.
