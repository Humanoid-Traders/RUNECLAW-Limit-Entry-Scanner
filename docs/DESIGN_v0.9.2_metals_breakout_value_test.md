# RUNECLAW v0.9.2 — Metals-breakout value-test (REJECTED) + per-universe leader

**Status:** research-only (`research/replay_mp.py` gains a configurable `--leader`).
No `src/**` change, no deploy. Triggered by a live observation: on v0.6.8 the metals
leg kept placing silver pullback-short limits that never filled while silver trended
to new day lows — because the **metals universe has no breakout path** (only crypto
and equities set `breakout: true`). Hypothesis: enable breakout for metals so it can
market-enter silver breakdowns, the way crypto does. The loop tested it. **Rejected.**

## 1. Why the question is legitimate

Crypto's breakout leg is the *stronger* edge (validated: +0.90%/trade vs +0.40%
pullback). Silver at the day low, trending down, is a textbook breakout-short — but
the metals leg can only place a pullback-short *above* market, which never fills if
silver doesn't bounce. So the leg correctly identifies the short and correctly
refuses to chase, then **can't participate** in the breakdown. Plausibly a gap.

## 2. The harness can't score metals natively (first finding)

Running `replay_mp --leader XAUUSDT --symbols XAGUSDT` produces **0 entries** — XAG's
score caps at **68.0**, just under the **70** gate. Cause: the offline replay
**degrades the order-book dimension** to a fallback (historical OHLCV can't
reconstruct bid/ask depth), which knocks ~2 points off every score. Live XAG has the
real book and clears 70 (we watched it place shorts); offline it can't. So metals is
**not cleanly offline-evaluable** — a fidelity limit, not a verdict.

## 3. Compensated read: breakout only adds losers (second finding)

Lowering the gate to 64 to offset the missing order-book points lets XAG trade, then
A/B breakout off vs on (live exit config: trail 2×ATR, ts12, 0.04% fee):

| window | metals pullback-only | metals + breakout |
|---|---|---|
| 35d | **+5.4% / maxDD −4.6% / PF 1.54** | +0.0% / −8.4% / PF 1.0 |
| 60d | **+1.5% / −4.6% / PF 1.1** | −3.9% / −8.4% / PF 0.81 |

The breakout path generated **3 silver trades, all losers** (0% win, −1.81% avg,
−5.4% total), turning a profitable pullback-only leg into breakeven-to-negative and
**doubling the tail** (−4.6 → −8.4). Silver at session extremes **mean-reverts**
rather than continues — the opposite of crypto. And this is the *optimistic* case:
the harness models no slippage on thin silver books, so live would be worse.

## 4. Verdict

**Keep the metals leg pullback-only. Do not set `breakout: true` for metals.** The
silver pullback-misses observed live are the strategy correctly declining moves it
can't enter well — not a gap. Forcing participation adds losing trades, exactly like
the debunked crypto fill-rate fix. This is the fifth plausible idea the loop has
killed (corr cap, concurrent-heat, chop gate, fill-rate fix, metals-breakout); the
only validated additions remain risk/observability (loss breaker, journal).

## 5. Harness improvement kept

`replay_mp` now takes `--leader` (default `BTCUSDT`), so any universe can be tested
against its real regime leader (`XAUUSDT` metals, `QQQUSDT` equities), and the leader
is auto-added to the fetch set. This is a permanent capability — the next universe
question (e.g. equities behavior) is now one flag away. Caveat from §2 stands: the
degraded order-book dimension means non-crypto legs score ~2pts low offline, so a
lowered gate is required to generate their trades and results are directional only.

## 6. The honest path to actually settling metals-breakout

Offline can't do it at full fidelity (no order book, no silver slippage). The
rigorous test would be a **signal-only** instance with metals-breakout enabled,
observing whether silver breakout-shorts fire and how they would have resolved —
live forward evidence, not a backtest. Given the directional result here is clearly
negative, that live test is low priority: the burden of proof is on breakout to show
it helps, and the harness says it doesn't.
