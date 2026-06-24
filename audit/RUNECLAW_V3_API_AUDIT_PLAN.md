# RUNECLAW v3 — API AUDIT PLAN

> **STATUS: PLACEHOLDER — pending real API audit**
>
> This file is a structural stub committed so the repo layout matches
> `SUBMISSION_CHECKLIST.md`. Replace this content with the actual API audit
> plan and findings before submitting to Bitget.

## Required sections (fill in before submission)

1. **Exchange endpoints in use** — exhaustive list of REST + WS endpoints with method, path, rate limits.
2. **Authentication path** — HMAC signing scheme, header set, clock-skew tolerance, replay protection.
3. **Idempotency & client order IDs** — generation scheme, collision avoidance, retry semantics.
4. **Error-class taxonomy** — exchange error codes mapped to local categories (retryable / fatal / kill-switch).
5. **State reconciliation** — when and how local state is reconciled against `fetch_positions`, `fetch_open_orders`, `fetch_my_trades`. Document the `productType=USDT-FUTURES` requirement for Bitget UTA mode.
6. **Failure-mode matrix** — for each endpoint, what happens on 4xx / 5xx / timeout / partial-fill / phantom-order.
7. **Test harness** — list of recorded fixtures + mock-exchange tests proving each failure mode is handled.
8. **Open issues** — known gaps and mitigations.

## Cross-references

- Code reviewed at commit: `<fill in commit SHA at audit time>`
- SHA-256 manifest: `audit/SHA256_MANIFEST.txt`
