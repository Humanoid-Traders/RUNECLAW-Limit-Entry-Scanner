# ⚔️ RUNECLAW — Limit Entry Scanner

**Live on Bitget** | GetAgent playbook `0791942e` | Instance `ad079b69` | v0.1.21

> RUNECLAW is a two-sided perpetual futures scanner that gates entries behind a market-regime check, ranks a 66-symbol universe by a blended score, and places resting limit orders at pullback depth. It does not predict direction — it waits for price to come to it.

---

## 1. Idea — Why This Works

### The Thesis

Most trading agents fail in execution, not signal design. Common failure modes: opposite-side opens from bad `tradeSide` handling, P&L corruption from mis-aggregated partial fills, and unscoped position ownership that causes the agent to manage trades it didn't open. RUNECLAW was built to eliminate each of these.

### Strategy Logic

Before any candidate is evaluated, RUNECLAW reads the market leader's regime (BTC by default). If the leader is weak or mixed, the agent stands aside and opens nothing. Capital preservation is the first job.

When the regime is constructive, the scanner ranks the configured universe with a blended score built from:

- Relative strength vs. the leader
- Location against session VWAP
- Position within the session range
- Resting order-book balance (bid/ask wall ratios)
- Traded liquidity (min $10M USDT/24h)

Only names clearing `min_score: 70` become candidates. Entry is a resting limit order placed at `atr_limit_mult × ATR` below VWAP — waiting for a pullback, never chasing.

### Risk Controls (All Code-Enforced)

| Control | Value | Enforcement |
|---------|-------|-------------|
| Max loss per trade | $15 USDT | `max_loss_usdt` in config; drives position size from the stop |
| Max concurrent positions | 3 | `max_concurrent: 3` in config |
| Max correlated alts | 2 | `max_correlated_alts: 2` in config |
| TP/SL required | Yes | Tool-level gate; order rejected without plan |
| Time-stop | 4h | `time_stop_hours: 4`; closes position regardless of P&L |
| Limit expiry | 4h | `limit_expiry_hours: 4`; cancels unfilled resting orders |
| Circuit breaker (pause) | $30 USDT daily loss | `circuit_pause_usdt: 30` |
| Circuit breaker (stop) | $40 USDT daily loss | `circuit_stop_usdt: 40` |
| Size-scoping cap | $1,050 USDT notional | `size_scope_mult: 1.5 × margin_budget`; agent ignores positions above this cap |
| Isolated margin | Yes | Code-enforced by design; subaccount API keys, no withdrawal scope |

**Size-scoping:** Any position with notional above `size_scope_mult × margin_budget` ($1,050 at current config) is excluded from ownership. The agent carries `own=0` on those positions and never touches them. This prevents the agent from accidentally managing manually-placed or externally-sourced trades.

### Exit Structure

Exits are layered and risk-first: protective stop beyond the recent defended level, partial profit at TP1 (3.5%), TP2 (7.0%), trailing stop thereafter, stop lifted to breakeven once trade moves 2% in favor.

### What We Do Not Claim

- No directional edge claimed from LLM or signal scoring alone
- No parameter optimization from live data
- No look-ahead in backtest harness
- No ownership of positions we didn't open

---

## 2. Progress

### Key Engineering Challenges

#### Bitget API Routing (CCXT v4.x)

CCXT v4.x defaults to v2 routing. Flipping `uta=True` without converting `tradeSide` to `reduceOnly` opens opposing positions instead of closing them. Fix: explicit per-order parameter conversion + conformance tests on the close path.

#### Partial-Fill P&L Aggregation

Initial code trusted summary `profit` fields. Bitget's `/api/v2/mix/order/fills` returns per-fill `profit` and `feeDetail` — these must be summed explicitly across all fills for an order. Fix applied; P&L reconciliation now runs before any state transition.

#### Known Bugs Fixed in v0.1.18

| Bug | Root Cause | Fix | Proof |
|-----|-----------|-----|-------|
| Stale-limit expiry | Handler read cached order reference, not live `create_time` | Now reads live pending response | PENDING — first order aged >4h unfilled |
| Position time-stop | Shared key list mutated across iteration (latent break on multi-position) | Per-iteration copy | PENDING — requires filled position aged >4h |

Both fixes are in production code. Proofs are time-gated, not fabricated. Documented as PENDING in `audit/SECURITY_AUDIT_FINAL.md` with exact conditions required.

### Live Operation — 2026-06-24

**ETH limit order (RUNECLAW-placed):**
- Order `1453542180542066689` — ETHUSDT buy/open — 0.42 ETH @ $1,650.71 — placed 05:03 UTC — unfilled as of 06:55 UTC — expiry 09:03 UTC

**Decision cycles (DBG-confirmed, 05:03–06:48 UTC):**
- `entry_already_pe` fired at 05:33 and 06:18 UTC: second ETH candidate correctly blocked while order was on book
- `correlation_budg` fired at 06:33 and 06:48 UTC: non-ETH candidate correctly blocked by correlation budget

**SOL position (correctly excluded):**
- SOLUSDT position in subaccount: 72.6 SOL, notional $4,998.51. RUNECLAW did not open it. Size exceeds $1,050 scope cap → agent carries `own=0`, never touches it. Verified in live DBG output.

**Full trading log:** [`logs/TRADING_LOG_2026.csv`](logs/TRADING_LOG_2026.csv) — contains ORDER, DBG_CYCLE, and FILL rows with real Bitget order IDs and timestamps.

### Completed

- Live GetAgent playbook running on Bitget (15-minute scan cadence)
- Regime gating (leader check before any entry)
- Blended score ranking across 66-symbol universe
- Correlation budget gating (DBG-confirmed live)
- Size-scoping exclusion (SOL position correctly excluded)
- TP/SL enforcement (tool-level gate)
- Two-bug fix in v0.1.18

### Pending

- ETH order fill or expiry proof (09:03 UTC today)
- Bug #1 and Bug #2 live proofs (time-gated)
- GPG signature on final commit (local machine, human-only)

### Next: v0.1.19

- Gate CLOSE cycle behind position existence check (~80% API call reduction when flat)
- Strip DBG codes from user-facing signal output (low severity, operational metadata only)

---

## 3. Technology Stack

| Component | Details |
|-----------|---------|
| **Execution environment** | GetAgent / MuleRun playbook system |
| **Exchange** | Bitget USDT perpetual futures (REST API v2) |
| **Signal scoring** | LLM (Claude) — blended regime + momentum + book score |
| **Order type** | Resting limit (entry), market (close) |
| **Scan cadence** | Every 15 minutes (`*/15 * * * *`) |
| **Universe** | 66 USDT perpetual pairs (configurable) |
| **Bitget tools used** | Bitget REST API, GetAgent execution environment |

---

## 4. Live Trading Record

**File:** [`logs/TRADING_LOG_2026.csv`](logs/TRADING_LOG_2026.csv)

**Schema:** `record_type, timestamp_utc, timestamp_ms, symbol, order_id, trade_id, side, trade_side, order_type, price, qty, notional_usdt, fee_usdt, pnl_usdt, status, dbg_string, notes`

**Record types:**
- `ORDER` — RUNECLAW-placed order (pending or filled)
- `DBG_CYCLE` — FSM state per scan cycle (own/pT/oP/act/correlation codes)
- `FILL` — Exchange fill rows (real Bitget data, verified via `tradesdk_contract_fills`)

**Current log contents (2026-06-24):**
- 1 ETH limit buy ORDER (pending, order ID `1453542180542066689`)
- 8 DBG_CYCLE rows (05:03–06:48 UTC, showing `entry_already_pe` and `correlation_budg` gates)
- 9 SOLUSDT FILL rows (real fills, correctly excluded — size-scoping cap)

**Artifact integrity:** SHA-256 hash of this file is in [`audit/MANIFEST.sha256`](audit/MANIFEST.sha256).

---

## 5. Audit Reports

| Report | Contents |
|--------|----------|
| [`audit/SECURITY_AUDIT_FINAL.md`](audit/SECURITY_AUDIT_FINAL.md) | API surface, execution controls (all 5 PASS), two bugs fixed, DBG exposure (low), size-scoping feature |
| [`audit/RUNECLAW_V3_API_AUDIT_PLAN.md`](audit/RUNECLAW_V3_API_AUDIT_PLAN.md) | Endpoints designed to use, real SOLUSDT fill verification, DBG error patterns, cycle overhead finding |
| [`audit/MANIFEST.sha256`](audit/MANIFEST.sha256) | SHA-256 hashes of all frozen submission files |

All claims in audit reports are marked with provenance: `code-enforced by design`, `DBG-confirmed`, or `PENDING`. No unverified observations presented as facts.

---

## 6. Manifest Config (Live Values)

From [`manifest.yaml`](manifest.yaml) — these are the actual values the live instance runs with:

```yaml
min_score: 70           # Minimum blended score to enter
max_loss_usdt: "15"     # Per-trade loss cap (drives position size)
max_concurrent: 3       # Max open positions
max_correlated_alts: 2  # Correlated position limit
time_stop_hours: "4"    # Force-close after 4h regardless
limit_expiry_hours: "4" # Cancel unfilled limits after 4h
size_scope_mult: "1.5"  # Ownership cap = 1.5 × margin_budget
circuit_pause_usdt: "30"
circuit_stop_usdt: "40"
leverage: 10
margin_budget: "100"
```

---

## 7. Artifact Integrity

```
audit/MANIFEST.sha256 — regenerated at commit cd7f411
```

To verify locally after clone:
```bash
sha256sum audit/SECURITY_AUDIT_FINAL.md \
          audit/RUNECLAW_V3_API_AUDIT_PLAN.md \
          logs/TRADING_LOG_2026.csv \
| diff - <(head -3 audit/MANIFEST.sha256)
```

---

## 8. Repository Structure

```
.
├── manifest.yaml                    # Live playbook config (authoritative)
├── README.md                        # This file
├── METHODOLOGY.md                   # Engineering discipline + validation rules
├── EXECUTIVE_MEMO.md                # One-page submission summary
├── CHANGELOG.md                     # Version history
├── audit/
│   ├── SECURITY_AUDIT_FINAL.md      # Security + execution control audit
│   ├── RUNECLAW_V3_API_AUDIT_PLAN.md # API endpoint audit + fill verification
│   └── MANIFEST.sha256              # Frozen file hashes
├── backtest/
│   └── BACKTEST_REPORT.md           # In-sample analysis (PF 1.20, Sharpe 0.49)
├── logs/
│   └── TRADING_LOG_2026.csv         # Live trading log (ORDER + DBG + FILL rows)
└── src/
    └── (playbook source)
```

---

## 9. Pending Items (Time-Gated)

| Item | Condition | ETA |
|------|-----------|-----|
| Bug #1 proof (stale-limit expiry) | ETH limit order aged >4h unfilled → handler emits `act1+limit_expiry_cancel` | 09:03 UTC 2026-06-24 |
| Bug #2 proof (position time-stop) | Filled position aged >4h → time-stop fires without iterator break | Next filled trade + 4h |
| GPG signature | `git commit -S --amend --no-edit` + re-tag on local machine | Human action — cannot be automated |

When Bug #1 proof lands: add the `limit_expiry_cancel` row to `logs/TRADING_LOG_2026.csv`, update `PENDING → VERIFIED` in `audit/SECURITY_AUDIT_FINAL.md`, regenerate manifest, push.

---

**Tag:** `v0.1.18-audit` | **Repo:** [Humanoid-Traders/RUNECLAW-Limit-Entry-Scanner](https://github.com/Humanoid-Traders/RUNECLAW-Limit-Entry-Scanner) | **Commit SHA:** see `git log` (volatile across re-sign)


---

## Engine Architecture

RUNECLAW is built as six cooperating engines. Each runs once per 15-minute cycle; the orchestrator wires them together and emits one diagnostic line at the end.

### 1. Feature Engine — `features.py`
The data layer. One `getagent.data.crypto.futures.ticker` call per symbol returns the 24h snapshot every downstream dimension consumes: `last`, `vwap`, `high`, `low`, 24h `change_percent`, `quote_volume`, and best-level `bid_volume` / `ask_volume`. A second `taker_volume` call on the gate asset supplies the optional taker buy/sell ratio. Responses arrive as SDK `OBBject`s and are normalized through the sanctioned `data.to_records(...)` converter; any symbol missing core price fields is marked `ok=False` and scored 0 — never guessed or back-filled.

### 2. Regime Engine — the BTC gate — `scoring.py: regime`
Capital preservation runs first. Before any coin is judged, the leader (BTC by default) resolves a **regime** from three independent signals, each worth one point: daily change sign (up/down), side of VWAP (above/below), and taker dominance (buy- vs sell-initiated flow). `long_score` and `short_score` are tallied 0–3:

- **≥ 2 → trade that side at full size** (`size_factor 1.0`)
- **== 1 → reduced size** (`size_factor 0.5`)
- **otherwise `none`** — the scanner still reports scores but **opens nothing**

Shorts are only taken when `allow_short` is enabled. This is the gate that makes RUNECLAW stand aside in directionless chop instead of forcing trades.

### 3. Scoring Engine — `scoring.py: score_universe`
Every symbol is scored **0–100 for the active direction**, fully mirrored for shorts, across five weighted dimensions:

| Dimension | Weight | Long rewards… | Computed from |
|---|---|---|---|
| Momentum | 0–25 | relative strength vs BTC (cross-sectional min-max) | `change_pct − btc_change` |
| VWAP position | 0–20 | trading above VWAP (±0.1% deadband) | `last` vs `vwap` |
| Range position | 0–20 | upper third of the 24h range | `(last−low)/(high−low)` |
| Order book | 0–20 | bid-heavy resting book (tiered) | `bid_volume / ask_volume` |
| Volume | 0–15 | deeper traded liquidity (cross-sectional) | `quote_volume` |

Three **hard skips** remove a name from candidacy regardless of score: **overextension** beyond `max_vwap_ext_pct` from VWAP on the entry side (a pullback limit structurally cannot fill a runaway breakout), an **opposing wall** (`bidask_wall_ratio`), and **thin volume** (`< min_volume_usdt`). Only names clearing `min_score` become candidates.

### 4. Risk Engine — `risk.py: build_plan`
Sizing is solved **backward from a fixed dollar risk**, never forward from a notional:

- **Entry** — a pullback limit at `VWAP ∓ atr_limit_mult × ATR` (ATR proxied as `(high − low) / 2.5`).
- **Stop** — beyond the 24h low (long) / high (short), floored at a per-tier minimum (`BTC/ETH 1.5%`, `SOL/BNB 1.2%`, alts `2.5%`) so market noise cannot place an absurdly tight stop.
- **Targets** — a two-stage take-profit ladder (`tp1 3.5%`, `tp2 7%`), an ATR trailing distance, and a breakeven trigger (`2%`).
- **Size** — `notional = max_loss_usdt / sl_pct × size_factor`; `margin = notional / leverage`; then **capped by `margin_budget`**. The per-trade loss at the stop is the control variable; everything else is derived from it.

### 5. Execution & Management Engine — `execution.py`
The stateful layer that owns the live book:

- **Ownership** — stateless and size-scoped: it manages only records whose notional fits RUNECLAW's own envelope (`margin_budget × leverage × size_scope_mult`), so it never adopts a larger manual trade.
- **Circuit breaker** — tracks day-start account equity in `.state/`; pauses new entries at `circuit_pause_usdt`, halts at `circuit_stop_usdt`.
- **Limit lifecycle** — expires resting orders past `limit_expiry_hours`, and chase-cancels a limit the market has run more than `limit_chase_pct` past (a stale limit that will never fill).
- **Position management** — intraday `time_stop_hours`, breakeven lift once a trade moves enough in favor, and the staged take-profit ladder.

### 6. Orchestrator & Diagnostics — `main_live.py`
Wires the cycle: features → regime gate → score → plan → `open_if_allowed`, then `manage_open_state`, then emits the compact **DBG** line — `own / pT / oP / act / c / p` plus a tail that is either a real fetch error (`perr.<code:msg>`) or the decision reason (`none`, `correlation_budget`, `entry_already_pending`, …), untruncated as of v0.1.20. It is the single readable line that reports what every engine did this cycle.

---

## v0.2.0 Engines — Kline & Funding

v0.2.0 adds two deterministic data engines via a **two-pass** scan: the cheap `ticker` pass ranks all 66 symbols (engines 1–3 above), then only the top `enrich_top_n` finalists are enriched with intraday klines and funding before the final pick — keeping the per-cycle call budget bounded (~2 calls × `enrich_top_n`).

### 7. Kline Engine — `features.py: fetch_klines` / `enrich`
Pulls `data.crypto.futures.kline` (5m/15m/1h/4h) for each finalist and computes:
- **Wilder ATR** over `atr_period` bars of `kline_interval` — a real ATR that replaces the `(high-low)/2.5` proxy and now drives entry depth and the trailing stop.
- **Higher-timeframe trend** (`trend_interval`, default 4h): close vs `EMA(trend_lookback)` → `trend_dir` + `trend_strength`.

A **trend-alignment** adjustment (`scoring.enrich_score`) adds up to `trend_weight` points when a finalist agrees with the higher-TF trend and subtracts when it opposes. Any data miss degrades gracefully to the proxy ATR + neutral trend.

### 8. Funding Engine — `features.py: fetch_funding`
Pulls `data.crypto.futures.funding_rate` and reads the latest rate plus a trailing mean:
- **Crowding skip:** a long into funding above `+funding_skip_bps`, or a short into funding below `−funding_skip_bps`, is dropped — leaning into the crowded, mean-reversion-prone side.
- **Soft penalty:** milder adverse funding subtracts up to `funding_penalty_weight` points.

Both engines are deterministic and bounded. Full spec: [`docs/DESIGN_v0.2.0.md`](docs/DESIGN_v0.2.0.md).

---

## v0.3.0 — Multi-Universe (Metals)

v0.3.0 generalizes the single BTC-gated scan into **N universes, each with its own regime leader**, run in one cycle and merged into one candidate pool (one instance per account → both asset classes live in one playbook). The scoring/regime engines are leader-agnostic, so this is an orchestration change, not an engine rewrite.

Default `strategy_config.universes`:

| Universe | Leader | Symbols |
|---|---|---|
| `crypto` | `BTCUSDT` | the 66-symbol `trading_symbols` list (inherited) |
| `metals` | `XAUUSDT` (gold) | `XAGUSDT` (silver) — *v0.3.1: Pt/Pd/Cu pruned (always below `min_volume_usdt`)* |

> **v0.3.1 notes:** the metals universe is pruned to **silver-only** — gold leads/gates, silver (~$257M/24h) is the one liquid metal candidate; platinum/palladium/copper sit permanently below the $10M volume floor so they were dead weight. Also, the DBG **tail now surfaces a fired management action** as `act.<type>` (e.g. `act.stale_limit_cancel`, `act.limit_expiry_cancel`) instead of a bare `act{N}` with a `none` tail — so chase/expiry/circuit/TP verdicts are unambiguous on the DBG line itself.

Each universe resolves its **own** regime from its leader (gold gates the metals class; BTC gates crypto), scores its symbols for that direction, and contributes qualified candidates to a merged pool. Pass-2 enrichment, the caps, and the final pick operate on the pool — so one cycle can short metals while it longs crypto. A universe with no `symbols` inherits `trading_symbols`; with no `universes` config at all, the scanner falls back to the legacy single BTC universe (fully backward-compatible).

Spec: [`docs/DESIGN_v0.3.0.md`](docs/DESIGN_v0.3.0.md). **Deferred:** stock perps (`SPXUSDT`/`QQQUSDT` as index leaders + market-hours/session handling) become another `universes` entry once session logic lands.

---

## 中文说明 (Plain-language summary)

### 策略 (Strategy)
RUNECLAW 是一个双向永续合约扫描策略。开仓前先判断市场领头币（默认 BTC）的趋势状态：当趋势偏弱或混乱时不开任何仓位，优先保住本金；趋势向好时做多，明显走弱时做空。在配置的币种范围内，用相对强弱、VWAP 位置、日内区间位置、订单簿买卖盘比例和成交流动性综合打分，只有分数达到 min_score（默认 70）的标的才会成为候选。

### 开仓 (Opening)
入场使用限价挂单，挂在 VWAP 下方/上方 atr_limit_mult × ATR 的位置，等待价格回调成交，不追价。每笔订单按照每笔最大亏损（max_loss_usdt，默认 15 USDT）与止损距离反推仓位大小，并受最大并发持仓数（max_concurrent）和相关性预算限制。

### 平仓 (Closing)
退出分层、风险优先：在市场近期防守位之外设置保护性止损；分批止盈（tp1/tp2）；剩余仓位用 ATR 跟踪止损；价格朝有利方向运行足够后将止损移至保本。组合层面还会过期撤销长时间未成交的挂单（limit_expiry_hours）、执行日内时间止损（time_stop_hours），并在当日累计亏损触发熔断时暂停开新仓。

### 风险 (Risk)
- 每笔交易最大亏损由 max_loss_usdt 控制（默认 15 USDT），仓位由止损距离反推。
- 杠杆可调（默认 10x，最高 25x）；更高杠杆会同时放大盈利与回撤。
- 当日亏损达到 circuit_pause_usdt 暂停、circuit_stop_usdt 停止开新仓（熔断）。
- 限制最大并发持仓与相关性敞口。
- 在领头币无方向震荡、市场宽度弱或快速行情击穿止损时表现较差，按设计可能长时间空仓。
- 过往表现不代表未来收益；实盘有手续费与滑点，请按可承受回撤规模建仓。
