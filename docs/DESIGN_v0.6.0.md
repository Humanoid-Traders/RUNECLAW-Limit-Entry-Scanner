# RUNECLAW v0.6.0 — Per-Universe Breakout + Tunable Extension (Design)

**Status:** design + implementation for live `follow_trade` (v0.5.x already runs breakout on crypto). Builds directly on `docs/DESIGN_v0.5.0.md`.
**Scope:** make the breakout path **per-universe** (not hard-wired to crypto) and let each universe tune the **extension threshold** that routes a name from pullback → breakout. Enable **equities** breakout. No engine-math change — the breakout entry, structure stop, and confirmation from v0.5.0 are reused verbatim.

---

## 1. Motivation — equities trend too, and can't catch it

v0.5.0 gated breakout to crypto only (`uni["name"] == "crypto"`), deferring equities/metals over session-gap and thin-book concerns. Live evidence since then says the gate now costs more than it protects:

- **MSTR chase-cancelled twice** on a clean −2.3% / −3% downtrend (sell-limit above market waiting for a bounce the downtrend never gave). A **breakout-short** would have taken it; equities can't breakout by design.
- Every equities setext is a pullback into a one-way move — the exact failure crypto's breakout path was built to fix, now observed one universe over.

Two gates block it: (a) breakout is crypto-only, and (b) the single global `max_vwap_ext_pct` (4%) is calibrated for volatile crypto — equities move less, so a tradeable equity breakout rarely reaches 4% above/below VWAP and never triggers.

---

## 2. Design — two small generalizations

### 2a. Per-universe breakout flag
Each `universes[]` entry gains an optional `breakout: bool`. `_scan_universe` computes:
```
allow_breakout = breakout_enabled  AND  uni.get("breakout", uni["name"] == "crypto")
```
Default preserves v0.5.x behavior (crypto-only) for any universe that doesn't set the flag. Enable equities with `breakout: true`.

### 2b. Per-universe config overrides (the extension lever)
`_scan_universe` already swaps `allow_short` per universe. Generalize that to a per-universe `overrides` map merged onto `cfg` for the **pass-1** scan:
```
ucfg = {**cfg, **per_universe_overrides}     # allow_short (existing) + overrides{}
```
Equities set `overrides: { max_vwap_ext_pct: "2.5" }` — so an equity extended past **2.5%** (not 4%) routes to breakout. This is the lever that lets MSTR-class moves qualify.

`max_vwap_ext_pct` does double duty (pull­back "too far to fill" skip **and** the breakout routing trigger), so lowering it per-universe correctly tightens both for that universe in one move: an equity past 2.5% is both un-fillable as a pullback *and* a breakout candidate. Coherent.

**Scope note:** pass-1 (`score_universe`) sees `ucfg`, so the extension override applies per-universe. Pass-2 (`enrich_score`) runs on the **merged** pool with global `cfg`, so `breakout_trend_min` / `breakout_extreme_band` stay **global** (the trend/extreme confirmation is the same quality bar for every universe — only the *routing* threshold is per-universe). This is intentional and keeps the merge simple.

---

## 3. What does NOT change

- **Entry mechanism** — still `open_long/short_market` (SDK has no stop-entry). Equities breakout enters at market the cycle it confirms, same as crypto.
- **Structure stop / sizing / targets** — `risk.build_plan` breakout branch is symbol-agnostic (24h high/low + ATR + per-symbol `sl_min`; MSTR/TSLA/NVDA fall in the 2.5% alt floor). No change.
- **Confirmation** — pass-2 still requires strong aligned higher-TF trend at the 24h extreme + non-crowded funding.
- **Caps / circuit / correlation** — unchanged, merged-pool-wide.

So v0.6.0 is almost entirely a **gating + threshold** change in `main_live._scan_universe` plus manifest config. `scoring.py`, `risk.py`, `execution.py` are untouched.

---

## 4. The thin-book risk (equities), and how it's bounded

The original deferral reason stands: equity perps gap on session boundaries and have thinner off-hours books, so a **market** breakout entry can slip more than on 24/7 crypto. v0.6.0 ships it anyway because the downside is bounded and the escape hatch is clean:

- **Per-trade loss is capped** by the structure stop + `max_loss_usdt` sizing (~$15) regardless of entry slippage — a bad fill costs a slightly worse entry, not an unbounded loss.
- **Liquidity floor** — `min_volume_usdt` already skips thin names; MSTR/TSLA/NVDA clear it.
- **Per-universe kill** — if equities breakout slips badly live, set `equities.breakout: false` and republish; crypto is unaffected.
- **24h-extreme staleness** — the `near_extreme` check uses the 24h high/low, which can be gappy for session-bound equities. Watched as a data-quality caveat, not gated in v0.6.0.

**Deferred (if live slippage is bad):** a slippage-capped *marketable limit* for non-crypto breakouts (a limit priced a small % through current, filling like a market but never worse than the cap) instead of pure market. Reintroduces partial-fill handling, so out of scope until the data warrants it.

---

## 5. Config (v0.6.0)

```yaml
universes:
  - name: crypto
    leader: BTCUSDT
    breakout: true                 # explicit (was the hard-wired default)
  - name: metals
    leader: XAUUSDT
    symbols: [XAGUSDT]             # breakout omitted -> stays pullback-only (1 thin symbol)
  - name: equities
    leader: QQQUSDT
    symbols: [TSLAUSDT, NVDAUSDT, MSTRUSDT]
    breakout: true                 # NEW: equities can now take trends
    overrides:
      max_vwap_ext_pct: "2.5"      # equities breakout triggers at a lower extension than crypto's 4%
```
`breakout_enabled` (global master switch) and the `breakout_*` tuning keys are unchanged from v0.5.1.

---

## 6. Code change surface

| File | Change |
|---|---|
| `main_live.py` | `_scan_universe`: generalize per-universe overrides (`allow_short` + `overrides{}` merged onto `cfg`); per-universe `breakout` gate replaces the hard-wired `name == "crypto"` |
| `manifest.yaml` | per-universe `breakout` flags + equities `overrides.max_vwap_ext_pct`; version → 0.6.0 |
| `scoring.py`, `risk.py`, `execution.py` | none (reused as-is) |

---

## 7. Validation

1. Publish 0.6.0; live `follow_trade` (breakout already trusted on crypto).
2. Watch for an **equities `pB`** — a TSLA/NVDA/MSTR market entry (`c{c}pB`, `metrics.entry_mode=breakout`, `metrics.universe=equities`). MSTR on a renewed down-leg past 2.5% below VWAP is the prime candidate.
3. Confirm the equity breakout gets a sane structure stop and fundable size, and that its market fill slippage is acceptable on the live book.
4. If equity slippage is bad → `equities.breakout: false`, republish; crypto unaffected.

**Deferred:** metals breakout (single thin symbol), per-universe pass-2 trend tuning, the marketable-limit slippage cap.
