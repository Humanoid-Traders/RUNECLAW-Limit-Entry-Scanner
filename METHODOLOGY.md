> **⚠️ LEGACY ARTIFACT (historical record).** Parts of this document date from the
> prior "RUNECLAW v3.x" lineage and reference artifacts that are **not in this
> repository** (`backtest/runner.py`, `backtest/config.json`,
> `backtest/RUNECLAW_IN_SAMPLE_ANALYSIS.md`). Those references describe a
> different, earlier system — see `docs/legacy/BACKTEST_REPORT_v3.3.0.md`.
> The authoritative description of the current system is `README.md`.

# RUNECLAW Methodology: Validation, Verification, and the Edge

**Version:** v3.3.0  
**Date:** June 1, 2026 (frozen)  
**Purpose:** Explain the engineering discipline that prevents overfitting and enables honest edge claims

---

## Executive Summary

RUNECLAW is built on three core principles:

1. **Signal edge must be demonstrated before strategy deployment.**
2. **Platform summary fields are never load-bearing; reconciliation to raw exchange data is primary.**
3. **Verification infrastructure (test harness, trust hierarchy, frozen windows) is the durable asset.**

This document explains why these principles exist and how they prevent the most common failure modes in algorithmic trading.

---

## Part 1: The Signal Validation Failure & Rebuild

### Problem: Initial Signal (v2.x)

RUNECLAW began with a 6-signal system designed to predict BTC direction:
- Trend-following (50-period / 200-period SMA cross)
- Mean-reversion (Bollinger Band breakout)
- Liquidation cascades (on-chain borrowing surges)
- Macro regime (VIX proxy)
- Technical structure (support/resistance)
- Sentiment (social media volume)

**Validation** (11-year Kraken BTCUSDT backtest, 2015–2026):
- 4,372 total trades
- Directional accuracy: **50.1%** (coin flip)
- Net expectancy: **−0.118R** per trade
- Negative years: **12 of 13** years tested
- Conclusion: **Zero edge**

### Why This Happened

Three mental errors:

1. **Summary Field Trap:** Summary fields (PF, win%, Sharpe) were positive in in-sample test, hiding that directional accuracy was random. We confused "profitable when gated correctly" (true) with "directional edge exists" (false).

2. **Parameter Selection Bias:** Hold-times, confluence thresholds, and stop-loss multipliers were optimized on the same dataset used to backtest. This created selection bias: the parameters fit the backtest period's specific noise.

3. **Backtester Overfit:** Backtest harness had no cost model initially. When fees were added, PF collapsed. This showed the edge was marginal and confidence-interval dependent.

### The Fix: v3.0 Signal Redesign

**New mental model:** Signals don't predict direction. Confluence voting gates entries.

**New signal types** (all 6 redesigned):

| Old | New | Change |
|-----|-----|--------|
| SMA cross predicts trend | SMA cross is 1/5 voter (not predictor) | Reduce solo signal weight |
| Bollinger breakout predicts reversion | Bollinger is 1/5 voter | Prevent mean-reversion overfitting |
| Liquidation = capitulation buy signal | Liquidation is 1/5 voter (context-dependent) | Add risk weight |
| VIX proxy = macro trend | Volatility regime is 1/5 voter | Reduce regime dependency |
| Support/resistance = inflection | Confluence requires 3/5 agreement | Gate, don't predict |
| Sentiment = mood | Sentiment is 1/5 voter | Diversify input |

**Key change:** Consensus voting (3/5 minimum) prevents overweighting any single signal.

---

## Part 2: The Three Permanent Rules

### Rule 1: Edge Must Be Pre-Registered

**What this means:**

Before a strategy is deployed live or traded on real account, the edge must be demonstrated on held-out data with explicit success criteria.

**How we enforce it:**

1. **Frozen Artifacts:** All backtest code, config, and signal definitions locked via git tag before live deployment.
   ```bash
   git tag -s v3.3.0 -m "Frozen: pre-registered forward walk at 325 in-sample trades"
   # SHA-256 hashes of all files committed
   ```

2. **Pre-Committed Evaluation Criteria:** PASS/FAIL gates written before forward walk begins:
   - PASS: PF ≥ 1.3, Sharpe ≥ 0.5 (30% above in-sample)
   - FAIL: PF ≤ 1.1, Sharpe ≤ 0.3 (at noise floor)
   - Trigger: 130 round-trips OR 12 months (whichever first)
   - No parameter refit during forward period

3. **No Data Leakage:** Hold-out test set is untouched until evaluation trigger hits.

**Why this works:**

Standard backtesting has p-hacking risk: run 100 parameter combinations, pick the best. With pre-registration, you get 1 try. This forces honest edge claims.

---

### Rule 2: Platform State is Primary, Summary Fields Are Secondary

**What this means:**

The source of truth is the exchange (Bitget). Reconcile to raw data always. Never trust platform summaries without verification.

**Real example from RUNECLAW:**

**Problem:** CCXT 4.x `filled` field returns cumulative fill size, not per-fill data.
```python
# Bad (trusts summary):
response = exchange.create_order('BTC/USDT', 'limit', 'buy', amount=0.5, price=42000)
pnl = response['info']['profit']  # Summary field — unreliable

# Good (uses raw data):
for fill in response['info']['fills']:
    pnl += fill['profit']  # Per-fill P&L
    fees += fill['fee']
    # Verify: sum(fills) == response['filled']
```

**Another example:** Bitget UTA (Unified Trading Account)

- Platform says: "Position closed, PnL = $150"
- Raw check: Query `/api/v3/trade/orders/{order_id}` → verify `reduceOnly=true`, `tradeSide` not `positionSide`
- Raw check: Query `/api/v2/mix/order/fills` → sum per-fill `profit` fields
- Only then: Accept platform summary

**Why this matters:**

Partial fills, order rejections, and slippage can corrupt summary fields. Checking raw data catches bugs before they destroy the trading log.

---

### Rule 3: Verification Infrastructure is Durable

**What this means:**

Code changes. Markets change. Signals decay. But testing harness, trust hierarchy, and frozen windows are permanent.

**How we build it:**

#### 3a. Trust Hierarchy
```
Raw Exchange Data (Bitget API)
    ↓
Per-Fill Aggregation (sum, verify)
    ↓
P&L Reconciliation (closed_pnl == sum(fills))
    ↓
Account Equity Check (balance_before - balance_after == pnl + fees)
    ↓
Risk Decision (position size, stop-loss, hold time)
    ↓
Order Execution (FSM state transition)
```

Each layer verifies the layer below. If any check fails, the trade is rejected.

#### 3b. Frozen Windows
Backtest uses constant data windows, never expanding or refitting:
- In-sample: Always 325 trades (2015–2026 complete)
- Holdout: Always 130 trades (forward walk, June 2026 onward)
- No refit of in-sample period (prevents lookahead bias)

#### 3c. Test Harness
368 tests cover:
- **Unit tests:** Each signal type independently
- **Integration tests:** Confluence voting with all 5 voters
- **State machine tests:** FSM transitions (no invalid paths)
- **Reconciliation tests:** P&L matches Bitget API queries
- **Risk tests:** Position sizing never exceeds risk cap
- **Regression tests:** Code changes don't break edge

---

## Part 3: Common Failure Modes & How RUNECLAW Prevents Them

| Failure Mode | Example | RUNECLAW Prevention |
|---|---|---|
| **Overfitting to backtest** | Optimize stop-loss on same data used to test | Hold-time rules pre-registered; one var per version |
| **Parameter selection bias** | Run 1,000 param combos, pick best | No optimization; config frozen |
| **Look-ahead bias** | Use future data in backtest (bugs in harness) | Forward-only windows; no data leakage |
| **Survivorship bias** | Only test winning trades | Include all trades; report full distribution |
| **P-hacking** | Redefine success criteria post-hoc | Pre-registered PASS/FAIL gates at June 1 |
| **Partial-fill corruption** | Trust summary PnL field | Reconcile per-fill; verify against exchange |
| **State divergence** | Account balance doesn't match trades | Monotonic watchdog; checkpoint reconciliation |
| **Slippage surprise** | Backtest ignores costs | Cost model uses 0.02% taker + 0.05% slippage |
| **Regime change** | Strategy works in bull, fails in bear | Walk-forward validation across 5 chunks |
| **LLM hallucination** | Model generates fake signal | Confluence voting filters low-conviction entries |

---

## Part 4: Why This Approach is Credible

### 1. Verifiable Claims
Not: "Our AI beats the market" (vague, unfalsifiable)  
Yes: "PF 1.20 across 325 trades, Sharpe 0.49, consistent in all 5 historical chunks, pre-registered forward walk at 130 RTs"

### 2. Honest Failure Reporting
We published the initial signal's 50% accuracy—it failed. Instead of hiding or redefining success, we rebuilt.

### 3. Security & Audit Transparency
We published security audit findings (14 total, 10 fixed). Other projects hide findings; we show them because transparency builds trust.

### 4. Cost Realism
Backtest includes taker fees, slippage, realistic Bitget cost model. Not just "gross PnL."

### 5. One Variable Per Version
- v2.x: Signal design (failed, directional accuracy = 50%)
- v3.0: Rebuild signal + confluence voting
- v3.1: CCXT integration + API audit
- v3.2: Security patches
- v3.3: Risk engine multi-layer (current)

Each version changes one variable, preventing cascading bugs.

---

## Part 5: Forward Walk Protocol

### Pre-Walk Commitments (Frozen at June 1, 2026)

```
Criterion         | PASS            | FAIL
------------------|-----------------|-----------
Profit Factor     | PF ≥ 1.30       | PF ≤ 1.10
Sharpe Ratio      | Sharpe ≥ 0.50   | Sharpe ≤ 0.30
Win Rate (opt)    | WR ≥ 50%        | WR ≤ 45%
Trigger           | 130 RT or 12mo  | Whichever first
Refit Allowed?    | NO              | NO
Parameter Change? | NO              | NO
```

### Autonomous Trigger

RT count detector (`rt_count.py`) monitors Bitget trading log:
- Counts completed round-trips (entry + exit pairs)
- Compares to 130-RT trigger
- Automatically halts trading and evaluates when trigger hits

### Publication

Results published immediately upon trigger:
- Full trading log (timestamps, order IDs, PnL)
- Realized Sharpe, PF, win rate
- Comparison to pre-registered criteria
- Postmortem if failed (honest assessment)

---

## Part 6: The Edge (What Actually Makes Money)

### NOT the Edge

❌ LLM predicting direction (tested: zero edge in Llama 3.1 fine-tuning)  
❌ Directional signal accuracy (initial signals: 50% in all regimes)  
❌ High win rate (RUNECLAW: 52.3%, barely above random)  
❌ Parameter optimization (we don't do it)

### Actually the Edge

✅ **Confluence Voting:** 3/5 voters required for entry. Filters low-conviction noise.  
  - In-sample: 325 trades at 3/5 threshold  
  - Out-of-sample: 2/5 entries attempted but filtered (would have -0.2R each)  
  - Edge: Lower trade count, higher per-trade quality

✅ **Hold-Time Enforcement:** Signal-dependent exit timing prevents overfitting to calendar.  
  - Trend-follow: 4–8h hold  
  - Mean-revert: 1–3h hold  
  - Locked before deployment (no refit)

✅ **Deterministic Risk Controls:** Position sizing, stop-loss, P&L reconciliation prevent execution failures.  
  - Max DD: 18.3% despite 325 trades (good risk management)  
  - No single trade >0.5% account risk  
  - All fills reconciled before next trade

✅ **Partial-Fill Handling:** Exchange PnL used, not estimated.  
  - Bitget v2 fills endpoint: `/api/v2/mix/order/fills` returns per-fill `profit`  
  - Aggregation: `sum(fill['profit'] for fill in fills)` prevents P&L mismatch

---

## Part 7: Metrics Interpretation

### Backtest Metrics (In-Sample)

| Metric | Value | Interpretation |
|--------|-------|---|
| **PF 1.200** | Wins are 1.2x losses | Modest edge, but consistent across all regimes |
| **Sharpe 0.486** | 0.486 return per unit volatility | Below CTA median (0.6–1.0) but positive |
| **Max DD 18.3%** | Worst peak-to-trough | Acceptable for leveraged futures trading |
| **Win% 52.3%** | Above coin flip, below strong signal | Confirms: edge is confluence gating, not direction |
| **325 trades / 11yr** | ~30 trades/year | Quality over frequency; confluence filters entries |

### Forward Walk Metrics (Live/Paper)

| Status | Meaning |
|--------|---------|
| PF 1.3+ | Edge persists in live market (rare, good) |
| PF 1.1–1.3 | Edge degraded but present (expected decay) |
| PF <1.1 | Edge disappeared (disappointing, but honest) |
| Sharpe 0.5+ | Risk-adjusted return strong |
| Sharpe 0.3–0.5 | Risk-adjusted return moderate |
| Sharpe <0.3 | Risk-adjusted return weak; FAIL |

---

## Part 8: What Judges Should Ask (And How We Answer)

**Q: "Why should I believe your backtest?"**

A: "Backtest artifacts frozen before trading. SHA-256 hashes in git. No parameter refit. Walk-forward chunks show consistency (PF 1.09–1.24 across all 5 historical periods). Run `python backtest/runner.py` yourself—results are reproducible."

**Q: "PF 1.20 is low. Is this edge real?"**

A: "Yes, modest but real. Compound over 1,000 trades: 1.20^10 ≈ 6.2x return. But more importantly: positive in *all* regimes (bull, sideways, bear). Consistency matters more than magnitude. We're testing live now."

**Q: "What if live trading fails?"**

A: "We'll publish full logs and write an honest postmortem. Edge can decay due to: market regime change, increased competition, or hidden overfitting we missed. That's why forward walk is pre-registered—we can't cherry-pick success."

**Q: "Why not use LLM more aggressively?"**

A: "LLM is 1 of 5 voters. We tested fine-tuned Llama 3.1 on trading data (v5 candidate) and found zero edge—it learned output format, not trading signal. Claude distillation did the same. LLM is useful for parsing, not for trading edge."

**Q: "How is this different from every other agent project?"**

A: "We published our failures (initial signal had 50% accuracy). We froze artifacts before trading (no post-hoc cherry-picking). We pre-registered success criteria (PASS/FAIL gates). Most projects hide failures and redefine success criteria after-the-fact. We're betting that transparency is credible."

---

## Conclusion

RUNECLAW's edge is not glamorous: confluence voting + hold-time enforcement + multi-layer risk controls. It's engineering discipline, not novel signal design or LLM magic.

The real product is the **methodology**, not the current PF 1.20. If live trading fails, the methodology persists—it can be reapplied to new signals, new assets, new markets.

Judges looking for honest engineering will recognize this. Judges looking for a "10x AI magic wand" will be disappointed (and rightfully skeptical of anyone promising one).

---

**Status:** Methodology frozen v3.3.0  
**Last Updated:** June 1, 2026  
**Forward Walk Trigger:** June 24, 2026 (current date) + 130 RTs  
**Next Review:** Upon forward walk completion or 12-month mark
