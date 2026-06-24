# RUNECLAW Limit Entry Scanner

A live, **two-sided** scanner for Bitget USDT-margined perpetual futures, built
for the GetAgent runtime. On every scheduled pass it reads the market leader's
regime, ranks a configurable universe 0–100, and places the single best
**pullback limit** entry — long *or* short — behind portfolio-level risk
controls.

> **Live-only.** Its order-book dimension reads resting bid/ask demand that has
> no historical feed to replay, so it declares `backtest_support: none` and
> produces paper / live evidence rather than a historical equity curve.
> `follow_trade_supported: true` — subscribers who opt into follow-trade get the
> order placed for them; otherwise it emits a managed signal.

## Strategy / 策略

RUNECLAW trades **with the prevailing regime, not against it.** Nothing is
considered until the market leader (BTCUSDT — used as the regime gate only, never
traded) gives a clear read:

- **Leader constructive** (up on the day, above session VWAP) → look for **longs**.
- **Leader clearly weak** (down, below VWAP) → look for **shorts**.
- **Weak or mixed** → stand aside and open nothing — capital preservation first.

With a direction set, each tradable name is scored across five dimensions, every
one **mirrored for the side being sought**:

- **Momentum (0–25)** — relative strength versus the leader (for shorts, relative
  weakness), ranked across the universe.
- **VWAP position (0–20)** — for longs, above VWAP scores full; for shorts, below.
- **Range position (0–20)** — for longs, the upper third of the 24h range scores
  full; mirrored for shorts.
- **Order book (0–20)** — resting bid/ask imbalance on the traded side; an extreme
  wall against the trade hard-skips the name.
- **Volume (0–15)** — 24h quote volume ranked across peers; thin names are
  disqualified.

## Entry / 开仓

When the gate is open and a coin clears `min_score`, the strategy places a
**resting limit** on the favorable side of the day's average price:

- **Long:** `VWAP − atr_limit_mult × ATR14` (below market — waits for a pullback)
- **Short:** `VWAP + atr_limit_mult × ATR14` (above market — waits for a bounce)

where `ATR14` is estimated as the 24h range ÷ 2.5 and `atr_limit_mult` defaults
to 0.5. The order waits for a normal retracement instead of paying up. Two guards
keep it fillable: a **VWAP-extension cap** (`max_vwap_ext_pct`) skips momentum
runaways too far from VWAP, and a **pre-placement staleness skip** declines any
entry whose limit would already sit more than `limit_chase_pct` from price.

## Exit / 平仓

All exits are side-aware:

- **Stop loss** sits beyond the recently defended level (below the 24h low for
  longs, above the 24h high for shorts), with per-tier minimums: BTC/ETH ≥ 1.5%,
  SOL/BNB ≥ 1.2%, other alts ≥ 2.5%.
- **Take-profit ladder** — first target +3.5% (50%), extended +7.0% (25%), and a
  final 25% runner trailed by 1 × ATR.
- **Breakeven** — once price is +2% in favor, the stop lifts to entry.

In follow-trade mode the executed bracket is the limit entry plus the protective
stop and first take-profit; the staged second target, runner, and trail/breakeven
are surfaced in signal metadata for the management layer.

## Portfolio controls

- **Concurrency cap** (`max_concurrent`) — resting limits *and* filled positions
  both count toward the limit.
- **Correlation budget** — every open alt is treated as BTC-correlated; tightens
  to a single fresh slot when BTC or ETH is already held.
- **Circuit breaker** — pauses new entries on a soft daily loss, halts and
  flattens on a hard one.
- **Time-stop** — closes a position aged past `time_stop_hours`.
- **Limit expiry** — cancels a resting limit past `limit_expiry_hours`, or one
  the market has left behind by more than `limit_chase_pct`.
- **Stateless ownership** — the runtime does not persist state between scheduled
  runs, so RUNECLAW recognizes its own orders by notional size
  (≤ `margin_budget × leverage × size_scope_mult`) and never touches larger
  manual positions.

## Position sizing

Size is solved **backward from risk**: `notional = max_loss_usdt / stop_%`, then
`margin = notional / leverage`, capped by `margin_budget`. With the defaults
(`max_loss_usdt = 15`), each trade risks at most about 15 USDT to its stop.

## Configuration

| Parameter | Default | What it does |
|---|---|---|
| `trading_symbols` | 66 USDT perps | Scan universe; BTCUSDT is the regime gate, never traded |
| `leverage` | 10 | Amplifies gains/drawdowns; feeds the margin-from-notional math |
| `margin_budget` | 100 | Per-strategy capital cap and the return-% denominator |
| `max_loss_usdt` | 15 | Hard per-trade dollar risk; drives size from the stop |
| `max_scan_symbols` | 66 | Names scanned per pass (lower it if runs near the sandbox limit) |
| `min_score` | 70 | The 0–100 quality bar a coin must clear to be traded |
| `allow_short` | true | Enable short-side setups |
| `max_concurrent` | 3 | Max simultaneous commitments (resting + filled) |
| `atr_limit_mult` | 0.5 | Limit depth from VWAP (× ATR); lower fills sooner |
| `max_vwap_ext_pct` | 4.0 | Max distance from VWAP to enter (skips runaways) |

## Deployment

The Playbook runs on the GetAgent control plane and executes once per enabled
subscriber on a `*/15 * * * *` schedule (Asia/Shanghai). Lifecycle:
`upload → confirm → publish → enable`. For automated execution, enable with
`execution_mode: follow_trade`; per-subscriber `config_overrides` are stored on
the deployment instance. The package runs in a sandbox with only `getagent`,
`pandas`, and `numpy` available (no `pip install`); the entry point is
`python -m src.main`.

## Repository layout

```
manifest.yaml        # Playbook contract, schedule, config schema, universe
src/
  main.py            # entry point (scheduled run)
  main_live.py       # live decision + follow-trade execution + DBG diagnostics
  scoring.py         # regime gate + five-dimension blended score
  features.py        # per-symbol feature extraction (VWAP, range, order book)
  risk.py            # side-aware limit / stop / TP ladder / risk sizing
  execution.py       # ownership, portfolio controls, order placement & expiry
README.md
CHANGELOG.md
```

## Risk / 风险

This is a momentum-continuation strategy and it underperforms when the leader is
choppy or directionless, when market breadth is weak, or when fast moves slice
straight through stops. By design it can sit idle for long stretches, placing no
orders when the gate is closed. Order-book imbalance is a live, fast-moving
feature; resting demand can evaporate, and limit fills are not guaranteed. There
is no historical backtest for this Playbook, so judge it on live/paper evidence
only. Past results never guarantee future performance, and live trading pays fees
and slippage that erode edge — size every trade to a drawdown you can actually
tolerate, and do not run leverage you cannot afford to lose.

## License

Proprietary — see [LICENSE](LICENSE). Not licensed for redistribution or use
without permission.
