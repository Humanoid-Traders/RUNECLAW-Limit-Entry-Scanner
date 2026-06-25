# RUNECLAW v0.2.0 — Kline & Funding Engines (Design)

**Status:** implemented on branch `claude/peaceful-clarke-v0.2.0`, for `signal_only` validation.
**Scope:** two new deterministic data engines (multi-timeframe klines, perpetual funding) feeding the scorer and risk layer. No LLM. No change to the live v0.1.20 instance.

---

## 1. Motivation

RUNECLAW today uses **2 of ~16** available futures data tools — only `ticker` and `taker_volume`. Its features are therefore 24h-snapshot scale, which (a) makes the 15-minute scan cadence largely redundant, (b) forces a crude `ATR = (high-low)/2.5` proxy (audit #10), and (c) leaves it blind to two cheap, robust edges: **higher-timeframe trend alignment** and **funding/crowding**.

v0.2.0 adds those two engines without disturbing the deterministic, auditable core.

---

## 2. Architecture — two-pass enrichment

Fetching klines + funding for all 66 symbols would be ~200 calls/cycle. Instead:

- **Pass 1 — universe scan (all 66, cheap):** `ticker` per symbol → existing 0–100 score on the five ticker dimensions → rank → `qualified = score ≥ min_score and not skipped`.
- **Pass 2 — enrich the top `enrich_top_n` (default 8):** fetch `kline` + `funding` per candidate, compute real ATR / higher-TF trend / funding state, apply the **trend-alignment adjustment** and the **funding skip + penalty**, re-rank, then pick `best`.

**Call budget:** ~66 (ticker) + ~`2 × enrich_top_n` (≈16) + 2 (gate) ≈ **84 calls/cycle** vs ~67 today — modest and bounded.

> **Refinement vs the original spec table:** because kline/funding only exist for the enriched top-N, the "Trend alignment" dimension and the funding filter are applied as a **second-stage adjustment** to the already-qualified candidates, not baked into the universe-wide pass-1 score. Net effect matches intent — the final pick is trend-aligned and funding-aware — while keeping `min_score` semantics on the existing 0–100 scale unchanged and the per-cycle call budget low.

---

## 3. Engine 1 — Kline (`features.py`)

```python
fetch_klines(symbol, interval="1h", limit=50, exchange="bitget") -> list[dict]
# data.crypto.futures.kline(symbol, interval=interval, limit=limit,
#                           exchange=exchange, data_type="ohlc"); normalized via _to_records
```

Derived (populated by `enrich()` on a `SymbolFeatures`):

| Field | Computation | Purpose |
|---|---|---|
| `atr` | **Wilder ATR** over `atr_period` (14) bars of `kline_interval` (1h). `TR = max(H−L, |H−prevC|, |L−prevC|)` | real ATR — retires the `(high-low)/2.5` proxy |
| `trend_dir` ∈ {long, short, neutral} | sign of `close − EMA(trend_lookback)` on `trend_interval` (4h) | trend-alignment adjustment |
| `trend_strength` ∈ [0,1] | `clamp(|close/EMA − 1| / trend_norm)` | scales the adjustment |
| `kline_ok` | ≥ `atr_period+1` bars parsed | graceful-degrade flag |

**Fallback:** `kline_ok=False` → keep the proxy ATR, `trend_dir="neutral"` (zero adjustment). Never crash on a data miss.

---

## 4. Engine 2 — Funding (`features.py`)

```python
fetch_funding(symbol, interval="1h", limit=8, exchange="bitget") -> (now, avg, ok)
# data.crypto.futures.funding_rate(symbol, interval=interval, limit=limit, exchange=exchange)
```

| Field | Meaning |
|---|---|
| `funding_now` | latest `fr_close` |
| `funding_avg` | trailing mean over the window (crowding baseline) |
| `funding_ok` | at least one rate parsed |

**Reading:** positive funding = longs pay shorts (crowded long); negative = crowded short. Leaning *into* the crowded side is the costly, mean-reversion-prone trade.

---

## 5. Scoring adjustment (`scoring.py: enrich_score`)

For each enriched candidate, given its `Scored` row + `SymbolFeatures`:

- **Trend alignment** (`trend_weight`, default 15):
  - `trend_dir == side` → `+ trend_weight × trend_strength`
  - `trend_dir` opposed → `− trend_weight × trend_strength`
  - neutral / `kline` missing → `0`
- **Funding skip** (hard, mirrors the `ask_wall`/`bid_wall` pattern): long with `funding_now > +funding_skip_bps`, or short with `funding_now < −funding_skip_bps` → drop the candidate (`skip=True`, reason `funding_crowded_long` / `_short`).
- **Funding penalty** (soft): adverse-but-not-extreme funding → `− funding_penalty_weight × adverse_magnitude_score`.

`adjusted = base_score + trend_adj − funding_penalty`. Re-rank the enriched set by `adjusted`; pick the best non-skipped. New dims surfaced for transparency: `trend_dir`, `trend_adj`, `funding_now`, `funding_skip`.

---

## 6. Risk integration (`risk.py`)

```python
atr = feats.atr if (feats.kline_ok and feats.atr) else max((high - low) / 2.5, 0.0)
```

Real ATR now drives **entry depth** (`atr_limit_mult × ATR`) and **trailing** (`trail_atr_mult × ATR`). The `atr14_est` meta key becomes a real ATR-14, not a proxy.

---

## 7. Config deltas (`manifest.yaml`, → **0.2.0**)

```yaml
kline_interval: "1h"          # 5m | 15m | 1h | 4h
atr_period: 14
trend_interval: "4h"
trend_lookback: 12
trend_norm: "0.05"            # 5% close/EMA gap == full strength
enrich_top_n: 8
funding_interval: "1h"
funding_window: 8
funding_skip_bps: "30"        # 0.03%/interval adverse extreme -> skip
funding_penalty_weight: "8.0" # max soft score penalty
trend_weight: "15.0"
```

`decision_mode` / `runtime_profile` stay **deterministic**. `backtest_support` stays **none**.

---

## 8. Files touched

| File | Change |
|---|---|
| `features.py` | `fetch_klines`, `fetch_funding`, `enrich()`, Wilder ATR + EMA trend + funding fields |
| `scoring.py` | `enrich_score()` — trend adjustment + funding skip/penalty |
| `risk.py` | real-ATR swap with proxy fallback |
| `main_live.py` | pass-2 enrichment of the qualified top-N; real `kline_ok`/`funding_ok`; new DBG metrics |
| `manifest.yaml` | new config keys, version 0.2.0 |
| `README.md` | document the two new engines |

---

## 9. Validation plan (no backtest exists — this is the gate)

1. Publish 0.2.0, enable **`signal_only`** on a test instance (NOT the live 0.1.20 follow_trade instance).
2. Watch N cycles:
   - ATR sanity vs the proxy (same order of magnitude, not absurd).
   - Trend adjustment moves rankings the expected way (aligned names rise).
   - Funding skip fires at known crowded extremes.
   - Trade-frequency drop from the trend filter is acceptable.
3. Confirm graceful degrade: if a kline/funding call fails, the candidate falls back to proxy ATR + neutral trend and the cycle still completes.
4. Only after a clean `signal_only` read → consider `follow_trade`.

**Do not** enable v0.2.0 on the live instance while it is in flight. One change at a time, `signal_only` first.
