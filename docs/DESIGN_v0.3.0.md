# RUNECLAW v0.3.0 — Multi-Universe / Metals (Design)

**Status:** implemented on branch `claude/peaceful-clarke-v0.3.0`, for `signal_only` validation.
**Scope:** generalize the single BTC-gated universe into **N universes, each with its own regime leader**, and ship a **metals** universe (gold/silver/platinum/palladium/copper) alongside crypto. No engine rewrite — the scoring/regime functions are already leader-agnostic.

---

## 1. Motivation

The scanner hard-codes `_GATE = "BTCUSDT"` and scans one universe. Gating gold on BTC's daily move is noise — metals follow their own macro driver (USD, real yields, risk-on/off), led by **gold**. Because the platform allows **one active instance per account**, trading both crypto *and* metals means running both **in one playbook**, each with its correct leader.

Confirmed Bitget USDT-FUTURES metal/commodity contracts: `XAUUSDT` (gold), `XAGUSDT` (silver), `XPTUSDT` (platinum), `XPDUSDT` (palladium), `COPPERUSDT` (copper).

---

## 2. Architecture — list of universes, each with a leader

`strategy_config.universes` is a list; each entry is `{name, leader, symbols, allow_short?}`. The cycle scans **each universe independently** with its own leader-resolved regime, then merges candidates into one global pool that the existing pass-2 enrichment, caps, and pick operate on.

```yaml
universes:
  - name: "crypto"
    leader: "BTCUSDT"
    symbols: [ ...66 crypto... ]
  - name: "metals"
    leader: "XAUUSDT"
    symbols: ["XAUUSDT", "XAGUSDT", "XPTUSDT", "XPDUSDT", "COPPERUSDT"]
```

**Backward compatibility:** if `universes` is absent, the cycle synthesizes a single universe `{name:"crypto", leader:"BTCUSDT", symbols: trading_symbols}` — identical to today's behavior. Existing deployments are unaffected.

### Per-cycle flow
1. **For each universe:** fetch its `leader` ticker + taker → `scoring.regime(...)`. If the regime is `long`/`short`, scan that universe's symbols → `scoring.score_universe(feats, leader_feats, cfg, direction)` → `qualified_u` (`score ≥ min_score`, not skipped). Tag each candidate with its universe's `direction`, `size_factor`, and `name`. A `none` regime contributes scores to the board but no candidates.
2. **Merge** all `qualified_u` into one global pool. Sides are mixed (crypto may be long while metals is short — each candidate carries its own side).
3. **Pass 2 (v0.2.0):** enrich the global top-`enrich_top_n` with kline + funding, apply trend/funding adjustments, re-rank.
4. **Pick** the best non-skipped candidate; build its plan with **its universe's `size_factor`** and **its own side**; run the chase guard; emit.

### Engine reuse (no change to math)
- `scoring.regime(leader_feats, taker, cfg)` already takes any leader.
- `scoring.score_universe(feats, leader_feats, cfg, direction)` computes relative strength vs the **passed** leader — pass gold for the metals pass.
- `risk.build_plan(feats, cfg, size_factor, side)` is already side- and size-factor-parameterized.

Only `Scored` gains two carry fields, and `main_live` gains the universe loop + merge.

---

## 3. Code changes

| File | Change |
|---|---|
| `scoring.py` | `Scored` gains `size_factor: float = 1.0` and `universe: str = ""` (carry the per-universe regime context through the merge) |
| `main_live.py` | `_universes(cfg)` (config or legacy fallback); `_scan_universe(uni, cfg)` → `(regime, [Scored])`; `build_decision` loops universes, merges, then runs the existing enrich → plan → emit on the pool; per-universe regime summaries in meta |
| `manifest.yaml` | `universes` (crypto + metals) in `strategy_config`; `trading_symbols` retained for fallback; version → 0.3.0 |
| `README.md` | document multi-universe + the metals universe |

---

## 4. Risk & correctness notes

- **Correlation cap** matters more across a class: metals are tightly intercorrelated (gold/silver/platinum/palladium). The existing `max_correlated_alts` / correlation-budget logic applies to the merged pool; metals should be treated as one correlation cluster (follow-up: per-cluster tagging — out of scope for v0.3.0, flagged).
- **Leader as candidate:** the metals leader (gold) is *also* a tradable metal. It may appear both as the regime leader and a candidate; that is fine (BTC is excluded from the crypto scan today, but gold can be a metals candidate — its own regime simply gates the class).
- **Liquidity:** metal perps are thinner than majors; the existing `min_volume_usdt` skip applies. Verify metals clear it in `signal_only` before any `follow_trade`.
- **Sessions/gaps:** metals (esp. industrial copper) track underlying markets with weekday/session character; the 24h VWAP/range math is less clean than for 24/7 crypto. Watch the `signal_only` board for absurd ATR/VWAP on metals before promoting.

---

## 5. Validation plan

1. Implement, **publish 0.3.0**, enable **`signal_only`** (the v0.2.0 signal_only test instance must be disabled first — one instance per account).
2. Watch N cycles' **metrics/board**:
   - both universes resolve a regime (crypto via BTC, metals via gold);
   - metals candidates appear with sane scores, ATR (kline engine), funding, and clear `min_volume_usdt`;
   - the merged pick is coherent (right side per universe).
3. Confirm graceful degrade: a metal symbol with missing data is skipped, not fatal.
4. Clean `signal_only` read → consider `follow_trade`.

**Deferred (bigger project):** stock perps — needs per-class regime leaders (`SPXUSDT`/`QQQUSDT` exist), market-hours/session handling, and gap-aware features. The multi-universe scaffold here is the prerequisite; stocks become another `universes` entry once session handling lands.
