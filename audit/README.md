# Audit Bundle

Audit artifacts (originally frozen for the v0.1.18 Bitget submission; manifest
maintained since):

| File | Status |
|------|--------|
| `MANIFEST.sha256` | SHA-256 hashes of the frozen audit artifacts (verified by `tests/test_doc_integrity.py`) |
| `SECURITY_AUDIT_FINAL.md` | Real — live-instance audit; **amended post-freeze** to correct the false "isolated margin enforced" claim (see `docs/DESIGN_v0.6.4.md`); manifest regenerated 2026-07-02 |
| `RUNECLAW_V3_API_AUDIT_PLAN.md` | Real — Bitget v2 endpoints verified, SOLUSDT fills confirmed |
| `DEEP_AUDIT.md` | Deep code audit (state persistence, envelope blindness, ownership) |

Verify the manifest from the repo root (also enforced by the test suite):

```bash
sha256sum -c audit/MANIFEST.sha256
```

Regenerate after any deliberate change to a frozen artifact (record the reason
in the commit message — a hash change without an explanation defeats the
purpose of the freeze):

```bash
sha256sum audit/SECURITY_AUDIT_FINAL.md audit/RUNECLAW_V3_API_AUDIT_PLAN.md \
    logs/TRADING_LOG_2026.csv > audit/MANIFEST.sha256
```

> Note: the legacy v3.3.0 backtest report was quarantined to
> `docs/legacy/BACKTEST_REPORT_v3.3.0.md` (2026-07-02) — it describes a
> prior-generation system whose code is not in this repository, so it is no
> longer part of the frozen evidence set.

To attach a GPG-signed commit + tag for the "Verified" badge (must be done
on a machine with your GPG key):

```bash
git commit -S --amend --no-edit            # re-sign the latest commit
git tag -d v0.1.18-audit                   # drop the unsigned tag
git tag -s v0.1.18-audit -m "v0.1.18 audit complete: security + API + trading log"
git push --force-with-lease origin main
git push origin v0.1.18-audit
```
