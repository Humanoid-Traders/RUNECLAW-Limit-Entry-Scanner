# Deploy runbook — shipping a RUNECLAW version to GetAgent

Copy-paste pipeline, verified across deploys 0.6.28 → 0.6.34. Every step has
failed at least once when skipped — do them in order.

**Preconditions (hard rules):**
- Deploy ONLY on the operator's explicit flatten+go ("all is closed and
  disabled"). Never enable the playbook yourself — the operator enables via
  their card.
- The repo state you package must be merged to `main` and CI-green.
- The engine's loss-breaker window is ACCOUNT-wide: a redeploy does **not**
  reset it (proven live 2026-07-07). Don't promise a fresh slate.
- `ACCESS_KEY` is held by the operator (Bitget/GetClaw-managed, non-rotatable).
  Export it in the shell; **never** commit it to the repo.

```bash
cd <repo-root>
export ACCESS_KEY=<operator-provided>
STRATEGY_ID=e977214c-86e5-405b-be0b-d5bad50b97c8
S=$(mktemp -d)/pkg    # any scratch dir OUTSIDE the repo

# 1. Full test suite green (any FAIL = stop)
for f in tests/test_*.py; do python3 "$f" >/dev/null || echo "FAIL: $f"; done

# 2. Stage: manifest + README + src ONLY (tests/docs/research must NOT ship)
mkdir -p "$S/src"
cp manifest.yaml README.md "$S/" && cp src/*.py "$S/src/"

# 3. Platform lint (must print "Validation PASSED"; the long_description
#    word-count WARN is known and acceptable)
python3 /root/.claude/skills/getagent/scripts/validate.py "$S"

# 4. Package (<10MB)
tar czf "$S/../pkg.tar.gz" -C "$S" --exclude='__pycache__' --exclude='*.pyc' \
    manifest.yaml README.md src

# 5. Upload -> returns draft_id + suggested_version (e.g. 0.6.35)
curl -sS -X POST "https://api.bitget.com/api/v1/playbook/upload" \
  -H "ACCESS-KEY: $ACCESS_KEY" \
  -F "package=@$S/../pkg.tar.gz" -F "strategy_id=$STRATEGY_ID"

# 6. Confirm (temporary_id = draft_id from step 5)
curl -sS -X POST "https://api.bitget.com/api/v1/playbook/confirm" \
  -H "ACCESS-KEY: $ACCESS_KEY" -H "Content-Type: application/json" \
  -d '{"temporary_id":"<draft_id>"}'

# 7. Publish
curl -sS -X POST "https://api.bitget.com/api/v1/playbook/publish" \
  -H "ACCESS-KEY: $ACCESS_KEY" -H "Content-Type: application/json" \
  -d '{"draft_id":"<draft_id>","bump_type":"patch"}'
```

**8. Hand off to the operator:** report the published version + playbook_id and
wait for the card-enable confirmation. Decode the first SCAN line when pasted
(`python3 research/decode.py "<line>"`). Expected first-cycle quirks, all
benign: `nof-` (non-follow eval cycle), `p?` (management didn't run), and the
breaker token reflecting the ACCOUNT's existing 24h window.

**Known failure modes:**
- `import time` anywhere in `src/` → platform lint rejects (use `datetime`).
- Packaging from inside the repo picks up junk — stage to a clean dir first.
- Version bump forgotten → bump `manifest.yaml:3` and `ANALYSIS_VERSION`
  in `src/main_live.py`, then `python3 scripts/refresh_hashes.py` and
  `python3 tests/test_doc_integrity.py` BEFORE staging.
