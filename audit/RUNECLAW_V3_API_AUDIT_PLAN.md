# RUNECLAW v0.1.18 — API Audit

**Date:** 2026-06-24  
**Instance:** `ad079b69`  
**Auditor:** GetClaw (AI Agent)

**Provenance key:**  
- `designed to use` = declared in playbook manifest or executor source  
- `DBG-confirmed` = pattern observed in live signal output this session  
- `verified` = real rows returned by `tradesdk_contract_fills` this session  
- `PENDING` = fix applied; live proof not yet available

---

## Endpoints — Designed To Use

| Endpoint | Purpose | Rate Limit Risk | Basis |
|----------|---------|-----------------|-------|
| `GET /api/v2/mix/order/pending` | Pending order scan per cycle | Low | Designed to use — executor scans pending orders each tick |
| `GET /api/v2/mix/position/all-position` | Position state check | Low | Designed to use — executor reads positions each tick |
| `POST /api/v2/mix/order/cancel-order` | Limit expiry cancel / time-stop close | Low | Designed to use — fires on `act1` path |
| `POST /api/v2/mix/order/place-order` | Entry limit placement | Low | Designed to use — fires on entry signal |
| `GET /api/v2/mix/order/fills` | Fill verification post-order | Low | Designed to use — post-check step |
| `GET /api/v2/mix/plan/currentPlan` | TP/SL plan reads | Low | Designed to use — TP/SL state check |

All endpoints within Bitget v2 rate limits at a 15-minute scan cadence.

---

## Fill Data — Verified

### SOLUSDT (verified — real fills from `tradesdk_contract_fills`, pulled 2026-06-24T06:55 UTC)

**Entry:** order `…8961` (id redacted) — buy/open — 72.6 SOL @ $68.85 — maker — notional $4,998.51  
**Close:** order `…4944` (id redacted) — buy/close — 8 partial fills @ $69.145 — taker

| Fill # | Qty (SOL) | Notional | Fee | PnL |
|--------|-----------|----------|-----|-----|
| 1 | 4.9 | $338.81 | −$0.2033 | +$1.4455 |
| 2 | 20.0 | $1,382.90 | −$0.8297 | +$5.9000 |
| 3 | 11.3 | $781.34 | −$0.4688 | +$3.3335 |
| 4 | 0.2 | $13.83 | −$0.0083 | +$0.0590 |
| 5 | 9.0 | $622.31 | −$0.3734 | +$2.6550 |
| 6 | 7.4 | $511.67 | −$0.3070 | +$2.1830 |
| 7 | 10.6 | $732.94 | −$0.4398 | +$3.1270 |
| 8 | 9.2 | $636.13 | −$0.3817 | +$2.7140 |
| **Total** | **72.6** | **$5,019.89** | **−$3.642** | **+$21.417** |

**Net PnL (after fees):** +$17.775 USDT  
**Size note:** Position notional $4,998.51 exceeds $1,050 size-scoping cap — correctly excluded from RUNECLAW ownership; agent carried `own=0` throughout

### ETHUSDT (no verified fill data)

One pending limit buy: order `…6689` (id redacted, v0.9.4 audit), 0.42 ETH @ $1,650.71, notional $693.30, unfilled as of 2026-06-24T06:55 UTC. Expiry 09:03 UTC.  
No ETH fill rows exist in account history. No ETH PnL to report. This section will be updated if/when the order fills or expires.

---

## Error Patterns — DBG-Confirmed

| Pattern | Cycles Observed | Severity | Cause |
|---------|----------------|----------|-------|
| `shp.code;message;data;trace_id>(` | 05:03 UTC | Cosmetic / non-blocking | Response parser hits empty plan/position array on CLOSE cycle when flat |
| `entry_already_pe` | 05:33, 06:18 UTC | Expected behavior | ETH order already on book; second ETH candidate correctly gated |
| `correlation_budg` | 06:33, 06:48 UTC | Expected behavior | Non-ETH candidate correctly blocked while ETH slot occupied |

---

## Cycle Overhead Finding

**Observation:** CLOSE cycle runs every tick regardless of position state.  
**When flat** (`own0` / no open position): CLOSE cycle is a no-op that still consumes API calls.  
**Recommendation:** Gate CLOSE cycle behind position existence check — reduces API calls ~80% during flat periods.

```python
# Proposed gate
positions = api.get_positions()
if len(positions) > 0:
    close_cycle()
```

---

## Bug Fix Verification Status

| Fix | Code Change | Live Proof |
|-----|-------------|------------|
| Stale-limit expiry handler reads `create_time` from live pending response | v0.1.18 | PENDING — proof window 09:03 UTC 2026-06-24 |
| Position time-stop reads per-iteration key copy | v0.1.18 | PENDING — no aged position to test yet |

Both fixes are in production. Proof is time-gated.
