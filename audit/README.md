# Audit Bundle

Required artifacts before Bitget submission:

| File | Status |
|------|--------|
| `SHA256_MANIFEST.txt` | Generated (real hashes of frozen files) |
| `SECURITY_AUDIT_FINAL.md` | **Placeholder — replace with real audit** |
| `RUNECLAW_V3_API_AUDIT_PLAN.md` | **Placeholder — replace with real audit** |

After dropping in the real audit reports, regenerate the manifest:

```bash
sha256sum manifest.yaml src/*.py backtest/*.md logs/*.csv \
    README.md EXECUTIVE_MEMO.md METHODOLOGY.md CHANGELOG.md LICENSE \
    audit/SECURITY_AUDIT_FINAL.md audit/RUNECLAW_V3_API_AUDIT_PLAN.md \
    > audit/SHA256_MANIFEST.txt
```

Then tag the frozen snapshot:

```bash
git add audit/ && git commit -S -m "audit: frozen reports + manifest"
git tag -s v3.3.0 -m "RUNECLAW v3.3.0 frozen"
git push origin main --tags
```
