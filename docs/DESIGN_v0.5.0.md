# RUNECLAW v0.5.0 — Momentum / Breakout Entry Path (Design)

**Status:** design draft for `signal_only` validation. Default **off** (`breakout_enabled: false`) until proven on the board.
**Scope:** add a **second entry mode** — a market-on-confirmation breakout entry — for names trending too hard for the pullback limit to ever fill. No change to the pullback path, regime gate, caps, or circuit breaker when the mode is off.

---

## 1. Motivation — the structural miss, observed live

RUNECLAW has exactly one entry: a **pullback limit** (long rests at `VWAP − atr_mult·ATR`; short at `VWAP + atr_mult·ATR`). It waits for a retracement. In a one-way trend the retracement never comes, so the limit sits, the **chase guard cancels it** at `limit_chase_pct` (3%), and the next cycle re-picks the same name and repeats.

Observed live on v0.4.3, three cycles in a row (2026-06-26): ETH long pullback @ ~1540 while spot ground 1576→1582; chase-cancel; re-pick ETH; chase-cancel; re-pick ETH. The strategy **never participated** in a clean +4% trend and sat flat by design.

The code already names the problem. `scoring.py:128-130`:

> *"Names extended beyond the cap on the entry side are momentum breakouts the limit model structurally cannot catch — score them for the board but skip them as candidates."*

`max_vwap_ext_pct` (4%) hard-skips exactly the names a momentum path should trade (`overextended_above_vwap` / `overextended_below_vwap`). **The breakout path is: stop discarding those names; route them to a market entry with a structure stop.**

---

## 2. Core idea — dual entry mode, selected per candidate

Each candidate carries an `entry_mode ∈ {pullback, breakout}`:

- **pullback** (existing) — price is within normal range of VWAP; rest a limit and wait for the dip. Mean-reversion-to-trend.
- **breakout** (new) — price is extended past `max_vwap_ext_pct` on the entry side **and** the higher-TF trend is strong **and** price is at/near the session extreme. The pullback can't fill, so enter **with** the move at market and protect with a tight structure stop.

The two are mutually exclusive per name and decided by where price sits relative to VWAP plus the kline-engine trend, so they never both fire on the same symbol.

---

## 3. Routing — where the branch lives

Trend strength (`trend_strength`, `trend_dir`) is only computed in **pass 2** (`features.enrich` / kline engine), so breakout *qualification* must happen there. Pass 1 only *tags eligibility*.

**Pass 1 — `scoring.score_universe`** (at the existing ext check, `scoring.py:134-138`):
```
if side == "long"  and ext >  max_ext_pct:   # was: skip "overextended_above_vwap"
    if breakout_enabled: mark entry_mode="breakout", skip=False (compete on score)
    else:                skip, reason = True, "overextended_above_vwap"   # unchanged
# symmetric for short with ext < -max_ext_pct
```
Breakout-eligible names are **no longer hard-skipped** (so they keep their high momentum score and reach the top-`enrich_top_n` for enrichment). They carry `entry_mode="breakout"` provisionally.

**Pass 2 — `scoring.enrich_score`** (after trend + funding are known):
```
if scored.entry_mode == "breakout":
    aligned = (feats.trend_dir == side) and feats.trend_strength >= breakout_trend_min
    near_extreme = long: last >= high24h*(1-breakout_extreme_band)   # at the top of range
                   short: last <= low24h *(1+breakout_extreme_band)
    if not (aligned and near_extreme and not funding_crowded):
        skip, reason = True, "breakout_unconfirmed"   # demote back to no-trade
```
So a name is traded as a breakout **only** when it is extended (pass 1) *and* riding a strong aligned higher-TF trend at the session extreme with non-crowded funding (pass 2). Everything else that was overextended still skips, exactly as today.

`Scored` gains one field: `entry_mode: str = "pullback"` (carried through the merge alongside `universe` / `size_factor`).

---

## 4. Entry mechanism — market on confirmation (SDK-forced)

The trade SDK exposes `open_long_limit/open_short_limit`, `open_long_market/open_short_market`, and `place_order` — **but no native stop/trigger entry** (buy-stop above market). A breakout therefore cannot rest as a stop order; it enters **at market the cycle it is confirmed**, via `open_long_market` / `open_short_market` (which still attach `tp_trigger_price` / `sl_trigger_price`).

Consequences and mitigations:
- **We pay the spread and enter already-extended.** Mitigation: the strict pass-2 trend + near-extreme + funding filter, and a tight structure stop so a fakeout costs little.
- **No chase guard applies** — a market fill is immediate, there is no resting limit to leave behind. The breakout branch is exempt from `_best_effort_limit_expiry`'s chase + expiry (those operate on resting limits only; a filled position is governed by the position controls instead).
- **Optional micro-confirmation** (deferred): require `last` to exceed the prior cycle's high before firing, to avoid buying a stalling tape. At 15-min cadence a single confirm bar is coarse; start without it and add if fakeout rate is high.

---

## 5. Risk model — a breakout needs its own stop

`risk.build_plan` today puts the long stop **below the 24h low** (short: above the 24h high). For a breakout entering **near** the 24h high, that stop is enormous → `sl_pct` huge → position size tiny (or margin-capped to a stop so wide it never protects). That risk geometry is wrong for momentum.

**Breakout stop = structure stop below the broken level, widened to ≥ 1 ATR** so a clean breakout isn't wicked out, floored at the per-symbol `sl_min` (implemented):
```
long:  struct = high*(1 - buffer)          # just below the broken 24h high
       vol    = entry - stop_atr_mult*ATR
       raw    = min(struct, vol)            # the WIDER (lower) of the two
       sl_pct = max( (entry-raw)/entry, sl_min )
short: struct = low*(1 + buffer); vol = entry + stop_atr_mult*ATR
       raw    = max(struct, vol)            # the wider (higher) of the two
       sl_pct = max( (raw-entry)/entry, sl_min )
```
`entry` = the market fill (≈ `last`). `min`/`max` take the safer (wider) of the structure and volatility stops, so the stop neither sits a hair under the level (wicked instantly) nor floats in mid-air. Size still solves backward from `max_loss_usdt / sl_pct`, then margin-capped — `sl_pct` lands a sane ~1–2.5% instead of the 4–6% the pullback geometry would force near the high, so the position is fundable. *(Verified: a SOL breakout @210 over a 210.5 high → SL 206, 1.9%, ~$787 notional.)*

**Breakout-failed early exit — DEFERRED.** Detecting "price re-entered the range within N cycles" needs per-position state (the broken level), but management is stateless (it reads live positions fresh each cycle). The structure stop already caps the loss; the early exit is an optimization deferred until a state mechanism exists.

**Targets / trail — let winners run** (breakouts pay for the lower hit-rate with size of the win):
- `tp1` partial at a modest multiple (reuse `tp1_pct`, or a breakout-specific `breakout_tp1_pct`),
- trail the remainder with `trail_atr_mult·ATR` (existing machinery),
- auto-BE to entry once `breakeven_pct` in favor (existing),
- **breakout-failed exit**: if price closes back **below** `breakout_level` (long) within the first N cycles, exit immediately rather than waiting for the full stop — a clean breakout shouldn't re-enter the range. New small check in `_best_effort_position_controls`.

`TradePlan` gains `entry_mode` so execution and management know which geometry produced it.

---

## 6. Scoring weight — don't double-penalize extension

For a breakout candidate the VWAP-location dimension (`scoring.py:115-124`) currently rewards being *just* on the favorable side and the ext cap punishes being *far* — contradictory for momentum. When `entry_mode=="breakout"`:
- keep `momentum` and the pass-2 `trend_adj` (these are the breakout's edge),
- **do not** apply the `overextended_*` skip (handled in §3),
- leave VWAP/range/orderbook/volume as-is (they still inform ranking; a breakout into an opposing wall via `ask_wall`/`bid_wall` should still skip — a wall is a real liquidity barrier even in a trend).

No reweighting is strictly required for v0.5.0; the routing change alone makes breakouts tradable. A breakout-specific score blend is a deferred refinement.

---

## 7. Config (all new keys; mode off by default)

```yaml
strategy_config:
  breakout_enabled: false          # master switch; false = today's behavior exactly
  breakout_trend_min: "0.6"        # min kline trend_strength [0,1] to qualify (strict)
  breakout_extreme_band: "0.015"   # must be within 1.5% of the 24h extreme to fire
  breakout_stop_atr_mult: "1.0"    # structure-stop distance in ATRs
  breakout_level_buffer_pct: "0.2" # stop sits this % below the broken level
  breakout_tp1_pct: "4.0"          # wider first target than the pullback tp1
  breakout_fail_cycles: 2          # exit if price re-enters the range within N cycles
  # max_concurrent, correlation budget, circuit breaker, sizing all unchanged
```
Off by default means a published v0.5.0 is byte-for-byte behavior-identical to v0.4.3 until a subscriber (or the operator) flips `breakout_enabled: true`.

---

## 8. Failure modes & guards

| Risk | Guard |
|---|---|
| **Fakeout / buy the top** | strict pass-2 filter (trend_strength ≥ 0.6 **and** at the extreme **and** funding not crowded); tight structure stop caps the loss; breakout-failed early exit |
| **Whipsaw in chop** | breakout only fires when price is *extended past 4%* AND trend is strong — chop won't satisfy both; in ranges the pullback path stays in control |
| **Thin off-hours (metals/equities)** | market entry slips more on thin books; gate breakout to names clearing `min_volume_usdt`; consider disabling breakout for the equities/metals universes initially |
| **Crowded-long blow-off** | funding crowding skip already demotes `breakout` in pass 2 (`funding_crowded_long/short`) |
| **Correlated stack** | unchanged `max_concurrent` + Rule 7 correlation budget apply to the merged pool regardless of entry_mode |
| **Margin mode** | breakout uses `open_*_market`; fold in the pending isolated-margin fix (v0.4.4) so both entry paths place isolated |

---

## 9. Code change surface

| File | Change |
|---|---|
| `scoring.py` | `Scored.entry_mode` field; pass-1 routes `overextended_*` → `entry_mode="breakout"` (no skip) when enabled; pass-2 `enrich_score` confirms trend+extreme+funding or demotes to `breakout_unconfirmed` |
| `risk.py` | `build_plan` branches on `entry_mode`: breakout uses the tight structure stop (§5) and breakout targets; `TradePlan.entry_mode` carried |
| `execution.py` | `open_if_allowed` branches on `entry_mode`: `breakout` → `open_long_market/open_short_market`; breakout positions exempt from chase/limit-expiry; `_best_effort_position_controls` gains the breakout-failed early exit |
| `main_live.py` | carry `entry_mode` from pick → plan → emit; DBG surfaces the mode (e.g. tail `brk.<symbol>` or a metrics field) so the board shows which engine fired |
| `manifest.yaml` | new `breakout_*` config keys (mode off); version → 0.5.0 |
| `README.md` | document the dual entry mode + the breakout engine |

---

## 10. Validation plan

1. Implement, publish **0.5.0**, run **`signal_only`** with `breakout_enabled: true` (one instance per account; disable the live follow_trade first or use a separate signal_only read).
2. Watch the board for: breakout candidates appearing with `entry_mode=breakout`, sane structure stops (1–2.5%, fundable size), and the trend/extreme/funding filter actually gating (no breakout in chop).
3. Confirm graceful behavior when the kline engine degrades (no trend → no breakout qualification → falls back to skip, never a wrong market entry).
4. Only after a clean signal_only read, consider `follow_trade` with `breakout_enabled: true` and a small `max_loss_usdt`.

---

## 11. Decisions (locked 2026-06-26)

1. **Entry aggressiveness** — ✅ **market-on-confirm** (pure). No one-cycle micro-confirm in v0.5.0; revisit if the live fakeout rate is high.
2. **Universes** — ✅ **crypto only** (`uni["name"] == "crypto"`). Equities/metals stay pullback-only until their session/liquidity behavior under a market entry is understood.
3. **Stop model** — ✅ **structure stop** (§5), the safer of {below-broken-level, entry−ATR}, floored at `sl_min`.
4. **Default** — ✅ ship **off** (`breakout_enabled: false`); flip per-instance. v0.5.0 is byte-identical to v0.4.3 until enabled.

---

**Deferred:** breakout-specific score blend (momentum-weighted), measured-move targets, multi-cycle confirmation, and per-universe breakout tuning. The routing + market-entry + structure-stop core here is the prerequisite; those are refinements once the path is proven on the board.
