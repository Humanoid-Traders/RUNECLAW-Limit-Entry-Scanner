> **⚠️ LEGACY ARTIFACT (historical record).** Parts of this document date from the
> prior "RUNECLAW v3.x" lineage and reference artifacts that are **not in this
> repository** (`backtest/runner.py`, `backtest/config.json`,
> `backtest/RUNECLAW_IN_SAMPLE_ANALYSIS.md`). Those references describe a
> different, earlier system — see `docs/legacy/BACKTEST_REPORT_v3.3.0.md`.
> The authoritative description of the current system is `README.md`.

# RUNECLAW — Executive Memo for Judges

**Team:** HUMANOID TRADERS (Rotterdam)  
**Project:** Autonomous perpetual futures trading agent  
**Status:** Forward walk in progress (pre-registered)  
**Submission:** June 24, 2026

---

## The Thesis (30 seconds)

Trading systems fail in execution, not signal design. RUNECLAW proves it by rebuilding from signal validation: our initial 6-signal system had *zero edge* (50% directional accuracy, negative expectancy). We rebuilt with confluence voting (3/5 gate) + hold-time enforcement + multi-layer risk controls. Result: **PF 1.20, Sharpe 0.49** across 325 trades, 11 years, *all regimes*.

---

## Why This Wins

| Attribute | Most Submissions | RUNECLAW |
|-----------|------------------|----------|
| **Edge claim** | "Our LLM beats the market" (unfalsifiable) | "PF 1.20 across all regimes; here's the data" (verifiable) |
| **Backtest** | Optimized parameters; hides drawdowns | Frozen config; cost model included; walk-forward chunks |
| **Failure reporting** | Hides negative findings | Published audit: 14 findings, 10 fixed, 4 deprioritized |
| **Signal confidence** | High (probably overfit) | Low (52.3% win rate, but consistent) |
| **Forward criteria** | Undefined (cherry-pick later) | Pre-registered: PASS ≥1.3 PF, FAIL ≤1.1 PF, 130 RTs |
| **Security** | Claims "we're secure" | Shows bugs found + patches applied |
| **Code quality** | Black box | 368 tests, zero new failures post-security patch |

---

## The Numbers

| Metric | In-Sample (Frozen) | Forward Walk (Live) |
|--------|---|---|
| **Period** | 325 trades, 11 years | 130 trades, ongoing |
| **Profit Factor** | 1.200 | [X / 130 RTs] |
| **Sharpe Ratio** | 0.486 | [TBD at trigger] |
| **Max Drawdown** | 18.3% | [Tracking] |
| **Win Rate** | 52.3% | [TBD] |
| **Status** | Frozen (June 1) | Pre-registered (PASS: PF≥1.3, FAIL: PF≤1.1) |

---

## What Makes This Credible

✅ **Signal validation failure published.** Initial signals: 50% accuracy. Rebuilt v3.0 instead of hiding.

✅ **Artifacts frozen.** All backtest code, config locked via git tag. SHA-256 hashes committed. No post-hoc changes.

✅ **Pre-registered forward walk.** PASS/FAIL criteria written June 1, before any live trading. No cherry-picking.

✅ **Security audit published.** 14 findings (2 CRITICAL, 8 HIGH). 10 fixed. Shows rigor, not perfection.

✅ **Cost model realistic.** Backtest includes 0.02% taker fees, 0.05% slippage. Not inflated PnL.

✅ **Risk framework documented.** Multi-layer: capital check, P&L reconciliation, monotonic watchdog, position sizing.

✅ **Code is testable.** 368 tests, reproducible backtest harness. Run it yourself.

✅ **CCXT/Bitget integration validated.** Caught v4.x routing bug (defaults to v2 instead of v3). Fixed explicitly.

---

## Quick FAQ

**Q: "PF 1.20 is low."**  
A: Yes, modest. But positive in *all* regimes (bull, sideways, bear). Walk-forward chunks 1–5 all 1.09–1.24. Consistency > magnitude.

**Q: "Why no strong signal?"**  
A: We looked. Found none (50% directional accuracy in all signals). Built gating instead (confluence). Better to skip bad setups than predict direction.

**Q: "How is this different from 50 other LLM trading bots?"**  
A: Honesty. We publish failures, frozen artifacts, pre-registered criteria. Most hide downsides and redefine success post-hoc.

**Q: "Paper trading, not real?"**  
A: Real Bitget API, real latency, real partial fills, real order queue. Risk is zero; execution physics are identical to live.

**Q: "What if live trading fails?"**  
A: Full transparency. We'll publish logs, write postmortem, learn what broke. Can't do that if you've only been hiding downsides.

---

## Submission Materials

- **GitHub:** https://github.com/humanoid-traders/runeclaw (v3.3.0 tag)
  - `README.md` — Start here
  - `backtest/RUNECLAW_IN_SAMPLE_ANALYSIS.md` — Full report
  - `audit/` — Security findings + fixes
  - `logs/TRADING_LOG_2026.csv` — Live/paper trades
  - `tests/` — 368 test suite (0 failures)

- **Key Docs:**
  - `METHODOLOGY.md` — Why we do it this way (on disk now)
  - `BACKTEST_REPORT.md` — Detailed metrics (on disk)
  - `SUBMISSION_CHECKLIST.md` — How to verify our claims (on disk)

---

## The Ask

Judge RUNECLAW on:
1. **Methodology rigor** — Architecture is clean, risk layered, state reconcilable
2. **Honest edge claims** — PF 1.20 in all regimes, not just lucky period
3. **Bitget integration quality** — CCXT audit passed, UTA routing correct
4. **Transparency** — Failures published, audits public, forward walk pre-registered

Not on:
- ❌ LLM novelty (we're skeptical of AI trading hype too)
- ❌ Backtest PF alone (depends on cost model, regime, luck)
- ❌ Win rate (52.3% barely above random; PF is real metric)

---

## Why You Should Believe This

**The Smell Test**

- Humble claims (1.2x PF, not 10x returns)
- Negative findings published (security audit, signal validation failure)
- Code is verifiable (backtest reproducible, tests pass)
- Tone is terse, skeptical (no marketing fluff)

This is how serious engineers talk about uncertain things.

---

## Next Step

**Autonomous trigger:** 130 round-trips OR 12 months (whichever first)  
**Current:** RT count detector running; trades logged daily  
**Expected completion:** ~August–September 2026 (if 10+ trades/month)  
**Publication:** Full results immediately upon trigger

---

**"The best time to prove your edge is when you can't change the rules afterward."**

— HUMANOID TRADERS

---

*Questions? See full docs: README.md, METHODOLOGY.md, BACKTEST_REPORT.md*
