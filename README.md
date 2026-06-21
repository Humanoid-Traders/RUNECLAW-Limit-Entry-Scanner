# RUNECLAW Limit Entry Scanner

A live, long-only scanner for Bitget USDT-margined perpetual futures. On every
scheduled pass it runs a configurable universe through a BTC regime gate, scores
each coin 0–100, and surfaces the single best pullback-limit-long setup as a
managed signal.

This Playbook is **live-only**. Its order-book dimension reads resting bid/ask
demand that has no historical feed to replay, so it declares
`backtest_support: none` and produces paper / live evidence rather than a
historical equity curve. Default `execution_mode` is `signal_only` (a platform
rule for live-only Playbooks); `follow_trade_supported` is `true`, so subscribers
who opt into follow-trade also get the order placed for them.

## 策略 / Strategy

RUNECLAW buys strength on a pullback. Nothing is considered until the market
leader (BTCUSDT, used as the regime gate only and never traded) is constructive:
BTC must be up on the day and trading above its session VWAP, with taker buying
as a bonus confirmation. With the gate open, each tradable name is scored across
five dimensions:

- **Momentum (0–25)** — relative strength of the coin's 24h change versus BTC,
  ranked across the scanned universe.
- **VWAP position (0–20)** — above VWAP scores full, at VWAP scores half, below
  VWAP scores zero.
- **Range position (0–20)** — upper third of the 24h range scores full, the
  middle third scores half, the lower third scores zero.
- **Order book (0–20)** — resting best bid/ask volume imbalance; bid-heavy books
  score full, balanced books warn, and an extreme ask wall hard-skips the name.
- **Volume (0–15)** — 24h quote volume ranked across peers; thin names are
  disqualified.

## 开仓 / Entry

When the gate is open and a coin clears the minimum score, the strategy places a
**resting limit long** below the day's average price, at
`VWAP − 0.5 × ATR14`, where `ATR14` is estimated as the 24h range divided by
2.5. The order waits for a normal retracement instead of paying up. If the BTC
gate is only partially constructive, position size is automatically reduced.

## 平仓 / Exit

- **Stop loss** sits just below the 24h low, with per-tier minimums enforced:
  BTC/ETH ≥ 1.5%, SOL/BNB ≥ 1.2%, other alts ≥ 2.5%. If the 24h low implies a
  tighter stop than the tier minimum, the minimum is used instead.
- **Take-profit ladder** — first target at +3.5% (50%), extended target at +7.0%
  (25%), and a final 25% runner trailed by 1 × ATR.
- **Breakeven** — once price is +2% in favor, the stop is lifted to entry.

In follow-trade mode the executed bracket is the limit entry plus the protective
stop and the first take-profit target; the staged second target, runner, and
trailing/breakeven management are surfaced in the signal metadata for the live
management layer and for manual subscribers.

## Position sizing

Size is solved **backward from risk**: `notional = max_loss_usdt / stop_%`, then
`margin = notional / leverage`, capped by `margin_budget`. With the defaults
(`max_loss_usdt = 15`), each trade risks at most about 15 USDT to its stop.

## Parameters

- **trading_symbols** — the scan universe (USDT perpetuals). The first
  constructive entry, BTCUSDT, is the regime gate and is never traded.
- **leverage** — amplifies gains and drawdowns equally; also feeds the
  margin-from-notional math.
- **margin_budget** — per-strategy capital cap and the denominator for the
  user-facing return percentage.
- **max_loss_usdt** — hard per-trade dollar risk; drives position size from the
  stop distance.
- **max_scan_symbols** — how many names are scanned each pass (lower it if runs
  approach the sandbox time limit).
- **min_score** — the 0–100 quality bar a coin must clear to be traded.

## How to read the signal

A `long` signal reports `limit_price`, `sl_price`, `sl_pct`, `tp1_price`,
`tp2_price`, `notional_usdt`, `margin_usdt`, `leverage`, the BTC `gate` breakdown,
and a ranked `board` of the top candidates with their per-dimension scores. A
`watch` signal means the gate was closed or no coin cleared the score bar; its
`meta.reason` explains why.

## 风险 / Risk

This is a momentum-continuation strategy and it underperforms when BTC is choppy
or falling, when market breadth is weak, or when thin names gap straight through
the stop. By design it can sit idle for long stretches, placing no orders when the
gate is closed. Order-book imbalance is a live, fast-moving feature; resting
demand can evaporate, and limit fills are not guaranteed. There is no historical
backtest for this Playbook, so judge it on live/paper evidence only. Past results
never guarantee future performance, and live trading pays fees and slippage that
erode edge — size every trade to a drawdown you can actually tolerate, and do not
run leverage you cannot afford to lose.
