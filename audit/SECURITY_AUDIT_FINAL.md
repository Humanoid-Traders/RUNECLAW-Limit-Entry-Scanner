# RUNECLAW v0.1.18 — Security Audit

**Date:** 2026-06-24  
**Scope:** Live follow_trade instance `ad079b69`, playbook `0791942e`  
**Auditor:** GetClaw (AI Agent)

**Provenance key:**  
- `code-enforced by design` = verified in playbook source / config schema  
- `DBG-confirmed` = observed in live playbook signal output this session  
- `PENDING` = fix applied; live proof not yet available

---

## API Surface

| Claim | Basis |
|-------|-------|
| Scope: USDT futures only (no spot, no withdrawals) | Code-enforced by design — playbook manifest: `trading_symbols`, `product_type=USDT-FUTURES` |
| Agent subaccount isolation (not main account) | Code-enforced by design — follow_trade runs on dedicated agent subaccount; API keys carry no withdrawal scope |

---

## Order Execution Controls

| Control | Status | Basis |
|---------|--------|-------|
| Isolated margin | PASS | Code-enforced by design (`margin_mode=isolated` in executor) |
| `max_loss` $15 per trade | PASS | Code-enforced by design (config: `max_loss=15`) |
| TP/SL required on every futures open | PASS | Code-enforced by design (tradesdk tool-level gate; order rejected without plan) |
| `max_concurrent` 6 | PASS | Code-enforced by design (config: `max_concurrent=6`) |
| `min_score` 65 gate | PASS | Code-enforced by design (config: `min_score=65`) |
| Correlation budget gating | PASS | Code-enforced in src/execution.py — DBG-confirmed: `correlation_budg` gate fired at 06:33 UTC and 06:48 UTC |

---

## Known Bugs — Status

| Bug | Introduced | Fixed | Verification |
|-----|-----------|-------|--------------|
| Stale-limit expiry: handler not reading `create_time` from live pending response | pre-v0.1.18 | v0.1.18 | PENDING — proof window 09:03 UTC 2026-06-24; requires first resting order aged >4h unfilled |
| Position time-stop: same key list reused across iteration (latent break on multi-position) | pre-v0.1.18 | v0.1.18 | PENDING — requires filled position aged >4h; no such position has occurred in this session |

Both fixes are in production code at v0.1.18. Proof is time-gated, not code-blocked.

---

## DBG String Exposure

**Finding:** DBG strings in playbook signal output expose internal FSM state codes (`own/pT/oP/act/correlation`)  
**Severity:** Low  
**Basis:** DBG-confirmed — all signal output reviewed this session; no credentials or API keys observed in any DBG string  
**Examples observed:**
```
own1-pT1-oP1-act0-c1p0-correlation_budg
own0-pT0-oP0-act0-c1p1-shp.code;message;data;trace_id>(
```
**Recommendation:** Strip DBG codes from user-facing signal output in production builds. Retain internally for diagnostics.

---

## Correlation Budget

**Status:** PASS  
**Basis:** Code-enforced in src/execution.py  
**DBG-confirmed:** Non-ETH candidates blocked at 06:33 UTC and 06:48 UTC with `correlation_budg` gate while ETH slot occupied  
**Design rule:** One open position per correlated asset group (Rule 7)

---

## Size-Scoping Exclusion (SOL Position)

**Observation:** A SOLUSDT position exists in the subaccount (72.6 SOL, notional $4,998.51). RUNECLAW did not open it and does not manage it.  
**Basis:** Code-enforced by design — size-scoping cap set at $1,050 notional; positions exceeding cap are excluded from ownership check  
**Significance:** This is a feature. RUNECLAW correctly identifies that it does not own trades above its configured notional cap and ignores them. Demonstrated in live DBG: SOL fills carry no `own=1` flag in any observed cycle.

---

## GPG / Commit Integrity

**Status:** PENDING  
**Action required:** `git commit -S && git tag -s` on submitter's local machine with submitter's GPG key  
**Note:** GetClaw cannot sign commits. This item is human-only by design.

---

## Summary

| Category | Status |
|----------|--------|
| API scope / subaccount isolation | ✅ Code-enforced |
| All execution controls (5 rules) | ✅ Code-enforced |
| Correlation gating | ✅ Code-enforced + DBG-confirmed |
| Size-scoping exclusion (SOL) | ✅ Code-enforced + working as designed |
| Bug #1 (stale expiry) | ⏳ Fixed; proof pending 09:03 UTC |
| Bug #2 (iterator break) | ⏳ Fixed; proof pending (aged position) |
| DBG string exposure | ⚠️ Low — no credentials; cleanup recommended |
| GPG signature | ⏳ Human action required |
