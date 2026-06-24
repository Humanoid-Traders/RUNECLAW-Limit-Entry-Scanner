# SECURITY AUDIT — RUNECLAW Limit-Entry Scanner

> **STATUS: PLACEHOLDER — pending real audit**
>
> This file is a structural stub committed so the repo layout matches
> `SUBMISSION_CHECKLIST.md`. Replace this content with the actual security
> audit report before submitting to Bitget.

## Required sections (fill in before submission)

1. **Scope** — what was audited, what was out of scope, dates.
2. **Threat model** — assets (API keys, capital), adversaries, attack surfaces.
3. **Findings** — one entry per finding with severity (Critical / High / Medium / Low / Info), reproduction steps, impact, and remediation status.
4. **Secrets handling** — verification that no keys/secrets are committed; env-var matrix; rotation policy.
5. **Network / API hardening** — IP allowlists, rate-limit handling, retry/back-off policy.
6. **Order-placement safety** — pre-trade checks, kill-switch, max position/notional caps, reconciliation cadence.
7. **Logging & PII** — what is logged, retention, redaction.
8. **Auditor signature** — name, date, signature/PGP fingerprint.

## Cross-references

- Code reviewed at commit: `<fill in commit SHA at audit time>`
- SHA-256 manifest: `audit/SHA256_MANIFEST.txt`
