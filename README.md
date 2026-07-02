# ⚔️ RUNECLAW — Limit Entry Scanner

**Live on Bitget** · GetAgent playbook `runeclaw-limit-scanner` · codebase **v0.9.4** · `decision_mode: deterministic`

> A two-sided, **multi-asset** perpetual-futures scanner across **crypto, equities (RWA stock perps), and commodities (precious metals)** — each class gated by its own market-regime leader (BTC / Nasdaq-QQQ / gold), ranked by a blended deterministic score, and entered with resting limit orders at pullback depth. It does not predict direction — it waits for price to come to it, trades with the prevailing regime, and treats capital preservation as the first job.

---

## Table of Contents

1. [Overview](#1-overview)
2. [How It Works](#2-how-it-works)
3. [Risk & Safety Controls](#3-risk--safety-controls)
4. [Engine Architecture](#4-engine-architecture)
5. [Multi-Universe Scanning](#5-multi-universe-scanning)
6. [The Validation Loop](#6-the-validation-loop)
7. [Configuration Reference](#7-configuration-reference)
8. [Repository Structure](#8-repository-structure)
9. [Development & Testing](#9-development--testing)
10. [Honest Limitations](#10-honest-limitations)
11. [中文说明](#中文说明-plain-language-summary)

---

## 1. Overview

RUNECLAW is a **deterministic, rules-based** trading playbook for the GetAgent /
Bitget platform. There is no LLM in the decision path — every entry, exit, and
risk decision is computed from a fixed, auditable formula over public market data.

The design thesis is that most trading agents fail in **execution**, not signal
design — opposite-side opens from bad `tradeSide` handling, P&L corruption from
mis-aggregated partial fills, and unscoped position ownership that makes an agent
manage trades it never opened. RUNECLAW is built to eliminate each of these, and
its development is governed by a single rule: **measure before you ship.** Every
feature is validated in an offline research harness before it touches live money,
and the project's record includes as many ideas *rejected* by data as shipped.

**At a glance:**

| | |
|---|---|
| Platform | GetAgent / Bitget USDT perpetual futures |
| Decision mode | Deterministic (no LLM, no look-ahead) |
| Scan cadence | Every 15 minutes (`*/15 * * * *`) |
| Universe | 28 crypto + silver (XAG) + 3 RWA equity perps (TSLA/NVDA/MSTR) |
| Entry | Resting limit at pullback depth; optional momentum breakout (crypto/equities) |
| Exit | Structure stop · TP ladder · ratcheting ATR trailing stop · time-stop |
| Default leverage | 10× (configurable 1–25×) |

---

## 2. How It Works

**1. Regime gate (capital preservation first).** Before any candidate is judged,
each asset class reads its leader's regime from three independent signals — daily
change sign, side of VWAP, and taker buy/sell dominance — tallied 0–3. Score ≥ 2
trades that side at full size; == 1 at half size; otherwise the scanner reports
scores but **opens nothing**. This is what lets RUNECLAW stand aside in
directionless chop instead of forcing trades.

**2. Blended score (0–100, deterministic).** Each symbol is scored for the active
direction across five weighted dimensions — relative strength vs the leader, VWAP
position, range position, resting order-book balance, and traded liquidity — fully
mirrored for shorts. Three hard skips remove a name regardless of score:
overextension past `max_vwap_ext_pct`, an opposing order-book wall, and thin
volume (`< min_volume_usdt`). Only names clearing `min_score` (default 70) qualify.

**3. Entry — pullback limit, or breakout market.** The default entry is a resting
limit at `VWAP ∓ atr_limit_mult × ATR` — it waits for a normal retracement and
never chases. For crypto and equities, a name riding a strong higher-timeframe
trend at the session extreme (where a pullback limit structurally can't fill) is
instead entered at market via the **breakout** path. Metals are pullback-only (the
breakout path was value-tested for metals and rejected — see §6).

**4. Exit — layered and risk-first.** A protective stop sits beyond the recently
defended structural level (floored at a per-tier minimum). When the trailing stop
is active, a **wide `tp2` backstop** (15%) is attached so the ratcheting trail —
not a tight target — governs the upside; the trail moves the stop only in the
protective direction, only on a meaningful tick, and never loosens. An intraday
time-stop and stale-limit expiry close out the tail.

---

## 3. Risk & Safety Controls

Sizing is solved **backward from a fixed dollar risk**, never forward from a
notional: `notional = max_loss_usdt / sl_pct × size_factor`, `margin = notional /
leverage`, then capped by `margin_budget`. The per-trade loss at the stop is the
control variable; everything else is derived from it.

| Control | Live value | Mechanism |
|---|---|---|
| Max loss per trade | $15 | `max_loss_usdt` — drives position size from the stop distance |
| Max concurrent positions | 3 | `max_concurrent` |
| Correlation budget | 2 (→ 1 when BTC/ETH held) | `max_correlated_alts`; counts all owned names, position + resting limit |
| TP/SL on every entry | required | order rejected without a resolved plan |
| Stop floor (per tier) | BTC/ETH 1.5% · SOL/BNB 1.2% · alts 2.5% | minimum stop distance so noise can't place an absurdly tight stop |
| Trailing stop | 2×ATR ratchet | `trail_atr_mult`; protective-direction only, fail-safe no-op |
| Time-stop | 12h | `time_stop_hours` — closes a position regardless of P&L |
| Limit expiry | 4h | `limit_expiry_hours` — cancels unfilled resting orders (checked per scan cycle) |
| Chase-cancel | 3% | `limit_chase_pct` — drops a limit the market ran away from |
| Ownership scope | $1,500 notional | `margin_budget × leverage × size_scope_mult`; positions above this are never adopted |
| Realized-loss breaker | **armed** (0.08, since v0.9.3) | `loss_breaker_frac` — pauses new entries after a trailing-window realized-loss streak (§6); set 0 to disable |
| Margin mode | crossed (default) | `margin_mode`; isolated is an opt-in, trial-gated path (untested live) |

**Ownership is stateless and size-scoped.** `.state/` does not persist between
scheduled runs, so ownership is recomputed every cycle from live exchange state,
scoped to RUNECLAW's own envelope. Any position whose notional exceeds the scope
cap is excluded — the agent never adopts a larger, manually-placed trade. Since
v0.9.4, destructive management (time-stop close, limit cancel, stop modify) is
additionally restricted to symbols the scanner can actually open (universe
candidates minus leaders): a small manual trade in another name still counts
toward the caps but is never closed or re-stopped by the bot. The loss breaker
and live journal read only RUNECLAW-envelope-sized fills. And the open-gate is
fail-closed: if the management layer crashes, or the position *or* pending-order
read fails, the cycle is `state_blind` and no new entry is placed.

**Two honest notes on the safety stack:**

- **The `.state/`-backed equity circuit breaker (`circuit_pause_usdt` /
  `circuit_stop_usdt`) is non-functional in this runtime.** It depends on a
  day-start equity value persisting across runs, but `.state/` is ephemeral
  (`state_runs` stays at 1). Its working replacement is the **stateless
  realized-loss breaker** (v0.8.0), which sources trailing realized P&L from
  exchange fills — no local persistence required. **Armed by default since
  v0.9.3** (`loss_breaker_frac = 0.08`, the validated setting); set it to 0 to
  disable.
- **Margin mode is crossed by default.** Earlier documentation claimed isolated
  margin was enforced; it never was (verified). The opt-in isolated path exists
  (v0.6.4) but is untested live, so the proven default is crossed. Per-trade loss
  is bounded by the exchange SL + `max_loss` regardless of mode.

### What we do not claim

- No directional edge from any LLM — the strategy is deterministic
- No parameter optimization fit to live data
- No look-ahead in the research harness
- No ownership of positions we did not open

---

## 4. Engine Architecture

RUNECLAW is six cooperating engines. Each runs once per 15-minute cycle; the
orchestrator wires them together and emits one compact diagnostic (`DBG`) line.

**1. Feature engine — `features.py`.** One `ticker` call per symbol returns the
24h snapshot every downstream dimension consumes (`last`, `vwap`, high/low, 24h
change, quote volume, best-level bid/ask). Responses are normalized through the
sanctioned `data.to_records(...)` converter; any symbol missing core fields is
marked `ok=False` and scored 0 — never guessed or back-filled.

**2. Regime engine — `scoring.py: regime`.** The leader gate described in §2.

**3. Scoring engine — `scoring.py: score_universe`.** The 0–100 blend over five
dimensions (momentum, VWAP position, range position, order book, volume), mirrored
for shorts, with the three hard skips.

**4. Risk engine — `risk.py: build_plan`.** Backward-from-stop sizing, the
per-tier stop floors, the TP ladder (`tp1` 5% / `tp2` 15%), the ATR trailing
distance, and the breakeven trigger.

**5. Execution & management engine — `execution.py`.** The layer that owns the
live book: stateless size-scoped ownership, limit-lifecycle (expiry +
chase-cancel), position management (time-stop, breakeven lift, the trailing-stop
ratchet), the realized-loss breaker, and a stack of state-blindness interlocks
that refuse to open while the exchange read is unreliable.

**6. Orchestrator & diagnostics — `main_live.py`.** Wires the cycle and emits the
`DBG` line — `own / pT / oP / act / c / p` plus a tail carrying the fired action or
decision reason — and a metrics payload exposing each control's running state and
the live trade journal.

### The DBG line

`DBG-f{follow}{mode}-own{N}-pT{N}-oP{N}-act{N}-c{C}p{P}-{tail}` — `own` counts
owned commitments (positions **and** resting limits), `pT`/`oP` count pending
orders, and the tail surfaces a fired management action (`act.<type>`), a real
fetch error, or the decision reason (`correlation_budget`, `entry_already_pending`,
…). One readable line per cycle reporting what every engine did.

### Two-pass enrichment (kline & funding)

A cheap `ticker` pass ranks the universe; only the top `enrich_top_n` finalists are
enriched with intraday klines (Wilder ATR over `atr_period` of `kline_interval` =
1h; higher-TF trend on `trend_interval` = 4h) and funding (crowding skip + soft
penalty), keeping the per-cycle call budget bounded. Any data miss degrades
gracefully to the proxy ATR and neutral trend.

---

## 5. Multi-Universe Scanning

RUNECLAW runs **N universes, each with its own regime leader**, in one cycle, and
merges their qualified candidates into a single pool — so one cycle can short
metals while it longs crypto. The scoring and regime engines are leader-agnostic,
so this is orchestration, not an engine rewrite.

| Universe | Leader | Candidates | Breakout |
|---|---|---|---|
| `crypto` | `BTCUSDT` | the 28-symbol liquid `trading_symbols` list | enabled |
| `equities` | `QQQUSDT` (Nasdaq proxy) | `TSLAUSDT, NVDAUSDT, MSTRUSDT` | enabled |
| `metals` | `XAUUSDT` (gold) | `XAGUSDT` (silver) | pullback-only (§6) |

Every candidate must clear `min_volume_usdt` ($10M/24h) or it is `thin_volume`-
skipped — which is why the equity and metal legs carry only the few names that
clear the floor on Bitget. Energy is deferred (only WTI crude clears the floor; a
clean universe needs a second liquid energy name to gate against). Stock perps
track underlying equities that gap on session boundaries — off-hours liquidity is
thinner and an event filter is a known gap.

---

## 6. The Validation Loop

The project's defining discipline: a multi-position research harness
(`research/replay_mp.py`) that replays the **real engine modules** over public
klines, models fills and fees, and reports expectancy, profit factor, MAE/MFE, a
capture ratio, and **tail metrics** (max drawdown, worst trade) — because risk
controls are judged on the left tail, not the mean. Every change is A/B'd here
before it ships. Its most valuable output has repeatedly been telling us what
**not** to build:

| Idea | Axis | Verdict |
|---|---|---|
| Trailing stop (3-slot concurrency) | exit | ✅ **kept** — beats fixed under live concurrency; proven live |
| Realized rolling-loss breaker | risk | ✅ **shipped** (v0.8.0) — cuts tail ~22% in weak windows, harmless in healthy ones |
| Live trade journal | measurement | ✅ **shipped** (v0.9.1) — realized records from fills, live-vs-backtest loop |
| Correlation-weighted exposure cap | entry | ❌ rejected — the legacy count cap already dominates on the tail |
| Concurrent aggregate-heat breaker | entry | ❌ rejected — never fires; the tail is sequential, not concurrent |
| Leader efficiency-ratio chop gate | entry | ❌ rejected — anti-predictive; removes good trades |
| Fill-rate fix (tighter limits) | entry | ❌ rejected — forcing fills lowers expectancy |
| Metals breakout | entry | ❌ rejected — silver mean-reverts at extremes; adds only losers |

The through-line: RUNECLAW's edge lives in its existing selection; the productive
upgrades have been on the **risk / exit / observability** axis, and every
speculative entry-side idea was caught by the harness rather than by the account.
Full write-ups in [`docs/`](docs/) (`DESIGN_v0.7.0` … `DESIGN_v0.9.2`).

> **Note on backtest support.** The package declares `backtest_support: none` — the
> live score blends order-book imbalance that historical OHLCV cannot reconstruct,
> so the harness is a **research / ranking tool, not a published P&L claim.** Its
> rankings (which exit, which entry mode, what to *not* ship) are the signal.

---

## 7. Configuration Reference

Live values from [`manifest.yaml`](manifest.yaml) (the authoritative source):

```yaml
min_score: 70             # minimum blended score to enter
leverage: 10              # 1–25x configurable
margin_budget: "100"      # sizing anchor + return %% denominator
max_loss_usdt: "15"       # per-trade loss cap (drives size)
max_concurrent: 3         # max open commitments
max_correlated_alts: 2    # correlation budget (→1 when BTC/ETH held)
atr_limit_mult: "0.3"     # pullback-limit depth (× ATR from VWAP)
tp1_pct: "5.0"            # first target
tp2_pct: "15.0"           # wide backstop when the trail is active
trail_atr_mult: "2.0"     # trailing-stop distance (× ATR)
breakeven_pct: "2.0"      # move stop to breakeven after this favorable move
time_stop_hours: "12"     # force-close after this long
limit_expiry_hours: "4"   # cancel unfilled resting limits
limit_chase_pct: "3.0"    # chase-cancel threshold
size_scope_mult: "1.5"    # ownership cap = margin_budget × leverage × this = $1,500
loss_breaker_frac: "0.08" # realized-loss breaker (armed since v0.9.3; 0 = off)
journal_enabled: "true"   # live trade journal emission
margin_mode: "crossed"    # isolated is an opt-in, trial-gated path
kline_interval: "1h"      # ATR timeframe (Wilder, atr_period 14)
trend_interval: "4h"      # higher-TF trend
```

The GetAgent control plane computes its own published semver on publish, so the
live instance version label may differ from `manifest.version` (e.g. the v0.9.1
codebase was published as `0.6.8` on the deployment).

---

## 8. Repository Structure

```
.
├── manifest.yaml              # live playbook config (authoritative)
├── README.md                  # this file
├── METHODOLOGY.md             # engineering discipline + validation rules
├── EXECUTIVE_MEMO.md          # one-page summary
├── CHANGELOG.md               # version history
├── SUBMISSION_CHECKLIST.md
├── src/                       # playbook source (uploaded package)
│   ├── main_live.py           # orchestrator + DBG/metrics emission
│   ├── features.py            # ticker/kline/funding data layer
│   ├── scoring.py             # regime gate + 0–100 blended score
│   ├── risk.py                # backward-from-stop sizing + plan
│   └── execution.py           # ownership, lifecycle, trail, breaker, journal
├── research/                  # offline validation harness (NOT in the package)
│   ├── replay.py / replay_mp.py   # single- & multi-position backtest
│   ├── analytics.py           # expectancy / PF / MAE-MFE / capture / tail
│   └── live_journal.py        # reduce live journal records → live metrics
├── tests/                     # network-free unit tests (getagent stubbed)
├── scripts/                   # manifest lint + hash-manifest tooling (CI)
├── .github/workflows/ci.yml   # CI: full test suite + manifest lint on every push
├── docs/                      # DESIGN_v0.2.0 … DESIGN_v0.9.4 (design history)
│   └── legacy/                # quarantined prior-generation artifacts (v3.x —
│                              #   code not in this repo; claims unverifiable)
├── audit/                     # security + API audits, file hashes
└── logs/                      # trading log artifacts (order ids redacted)
```

---

## 9. Development & Testing

The full test suite is **network-free** (the `getagent` SDK is stubbed) and pins
every fix against real exchange-response shapes captured live:

```bash
# run the whole suite (CI runs the same loop on every push — see .github/workflows/ci.yml)
for f in tests/test_*.py; do python3 "$f"; done

# lint the manifest (schema bounds + live-value cross-check)
python3 scripts/lint_manifest.py manifest.yaml

# validate the package before upload (allowed paths only)
python3 <getagent-skill>/scripts/validate.py .

# run the multi-position research harness (the validation loop)
python3 research/replay_mp.py --days 35 --breakout --exit-mode trail \
    --trail 2.0 --time-stop 12 --ab-loss          # the validated drawdown breaker A/B
python3 research/replay_mp.py --days 60 --leader XAUUSDT --symbols XAGUSDT  # per-universe test
```

Coverage includes isolated-margin routing, the trailing-stop diagnostic,
state-blindness interlocks, the snake_case ownership fix (the bug that left the
trail inert for an entire session before v0.6.7), the realized-loss breaker, the
live journal, and the research analytics math. **No package is uploaded that fails
local validation; no feature is shipped that fails the harness.**

---

## 10. Honest Limitations

- **No order-book dimension in backtest** — historical OHLCV can't reconstruct
  bid/ask depth, so the harness degrades that dimension and non-crypto legs score
  ~2 points low offline. The harness is a ranking tool, not a P&L promise.
- **Live MAE/MFE is not reconstructable** — the stateless 15-minute runtime can't
  keep an intra-trade high-water track, so the live journal records realized P&L
  only; excursion metrics stay backtest-only.
- **Equity circuit breaker is non-functional** (`.state/` ephemeral) — superseded
  by the stateless realized-loss breaker (armed by default since v0.9.3).
- **Crossed margin by default** — isolated is an untested opt-in path.
- **No news/event filter** — RWA equity perps can gap on earnings/macro prints.
- **Strategy sits idle by design** — when leaders chop without direction it opens
  nothing, and the pullback-limit entry misses trending moves that never pull back.
- Past performance is not a guarantee; live trading pays fees and slippage. Size
  every position to a drawdown you can tolerate.

---

## 中文说明 (Plain-language summary)

**策略 (Strategy).** RUNECLAW 是一个**确定性规则驱动**的双向永续合约扫描策略（决策不依赖
大语言模型）。开仓前先判断各资产类别领头标的（加密用 BTC，股票用纳指 QQQ，金属用黄金）的趋势
状态：趋势偏弱或混乱时不开任何仓位，优先保住本金；趋势向好时做多，明显走弱时做空。在配置的
币种范围内（28 个加密 + 白银 + 3 个 RWA 股票永续），用相对强弱、VWAP 位置、日内区间位置、
订单簿买卖盘比例和成交流动性综合打分，只有分数达到 `min_score`（默认 70）的标的才会成为候选。

**开仓 (Opening).** 默认使用限价挂单，挂在 VWAP 下方/上方 `atr_limit_mult × ATR` 的位置，
等待价格回调成交，不追价；加密与股票类在强趋势冲到区间极值（限价无法成交）时改用突破市价入场，
金属类仅用回调限价（突破路径经回测验证对金属无效）。仓位按每笔最大亏损（`max_loss_usdt`，
默认 15 USDT）与止损距离反推。

**平仓 (Closing).** 退出分层、风险优先：结构性保护止损（设有每类最小止损距离）；当跟踪止损
启用时挂上较宽的 `tp2`（15%）作为后备，由逐步收紧的 ATR 跟踪止损主导上行（只朝有利方向移动，
绝不放松）；并设日内时间止损与挂单过期撤销。

**风险 (Risk).**
- 每笔最大亏损由 `max_loss_usdt` 控制，仓位由止损距离反推。
- 杠杆可调（默认 10x，最高 25x）；更高杠杆会同时放大盈利与回撤。
- **基于 `.state/` 的当日权益熔断在当前运行环境中不生效**（`.state/` 不持久化），其替代是
  **无状态的已实现亏损熔断**（v0.8.0；自 v0.9.3 起默认开启，`loss_breaker_frac=0.08`，设为 0 可关闭）。
- 限制最大并发持仓与相关性敞口；持仓所有权按规模范围界定，绝不接管更大的人工仓位。
- **默认全仓（crossed）保证金**；逐仓为未实测的可选路径。
- 在领头标的无方向震荡、或快速行情击穿止损时表现较差，按设计可能长时间空仓。
- 过往表现不代表未来收益；实盘有手续费与滑点，请按可承受回撤规模建仓。
