# ⚔️ RUNECLAW — Limit Entry Scanner

**Live on Bitget** | GetAgent playbook `0791942e` | Instance `ad079b69` | v0.1.20

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
