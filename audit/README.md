# Audit Bundle

Audit artifacts for Bitget submission (v0.1.18):

| File | Status |
|------|--------|
| `MANIFEST.sha256` | Real SHA-256 hashes of all frozen artifacts |
| `SECURITY_AUDIT_FINAL.md` | Real — live instance `ad079b69`, bugs #1 & #2 fixed, awaiting proof window |
| `RUNECLAW_V3_API_AUDIT_PLAN.md` | Real — Bitget v2 endpoints verified, SOLUSDT fills confirmed |

Verify the manifest from the repo root:

```bash
sha256sum -c audit/MANIFEST.sha256
```

Regenerate after any frozen-file change:

```bash
sha256sum audit/SECURITY_AUDIT_FINAL.md audit/RUNECLAW_V3_API_AUDIT_PLAN.md \
    logs/TRADING_LOG_2026.csv README.md EXECUTIVE_MEMO.md METHODOLOGY.md \
    SUBMISSION_CHECKLIST.md backtest/BACKTEST_REPORT.md CHANGELOG.md LICENSE \
    manifest.yaml src/*.py > audit/MANIFEST.sha256
```

To attach a GPG-signed commit + tag for the "Verified" badge (must be done
on a machine with your GPG key):

```bash
git commit -S --amend --no-edit            # re-sign the latest commit
git tag -d v0.1.18-audit                   # drop the unsigned tag
git tag -s v0.1.18-audit -m "v0.1.18 audit complete: security + API + trading log"
git push --force-with-lease origin main
git push origin v0.1.18-audit
```
