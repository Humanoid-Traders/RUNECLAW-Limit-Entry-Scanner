"""v0.9.4 documentation / evidence-integrity tests (audit fixes #1/#9).

The audit's #1 critical finding was an orphaned performance report citing a
nonexistent config under a placeholder hash, plus a frozen manifest that no
longer verified. These tests make that whole failure class permanent-red:

  1. audit/MANIFEST.sha256 must actually verify (a broken freeze is worse
     than no freeze).
  2. No placeholder hashes outside the quarantined legacy dir.
  3. Repo-path references in the LOAD-BEARING docs must exist on disk.
  4. The legacy quarantine invariants hold: legacy docs carry the banner,
     and no non-legacy doc references the removed backtest/ artifacts.

Run: python3 tests/test_doc_integrity.py
"""
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def test_manifest_verifies():
    manifest = ROOT / "audit" / "MANIFEST.sha256"
    _assert(manifest.exists(), "audit/MANIFEST.sha256 exists")
    n = 0
    for line in manifest.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        want, rel = line.split(None, 1)
        target = ROOT / rel.strip()
        _assert(target.exists(), f"manifest-listed file exists: {rel.strip()}")
        got = hashlib.sha256(target.read_bytes()).hexdigest()
        _assert(got == want, f"hash verifies: {rel.strip()}")
        n += 1
    _assert(n >= 3, f"manifest covers the frozen artifact set ({n} files)")


def test_no_placeholder_hashes_outside_legacy():
    bad = []
    for md in ROOT.rglob("*.md"):
        if "legacy" in md.parts or ".git" in md.parts:
            continue
        text = md.read_text()
        if "LEGACY ARTIFACT" in text[:600]:
            continue  # bannered historical records are exempt (quarantined in place)
        if re.search(r"abc123|deadbeef{2,}|sha-?256.{0,12}\.\.\.", text, re.I):
            bad.append(str(md.relative_to(ROOT)))
    _assert(not bad, "no placeholder hashes outside docs/legacy -> " + (", ".join(bad) or "clean"))


_PATH_RE = re.compile(
    r"\b((?:src|research|tests|docs|audit|logs)/[A-Za-z0-9_\-./]+?\.(?:py|md|yaml|csv|sha256|json))")
# load-bearing docs: fully checked. Banner-marked legacy docs are exempt.
_CHECKED = ["README.md", "audit/README.md"]


def test_loadbearing_doc_references_exist():
    for rel in _CHECKED:
        text = (ROOT / rel).read_text()
        refs = {m.group(1).rstrip(".") for m in _PATH_RE.finditer(text)}
        missing = sorted(r for r in refs if not (ROOT / re.sub(r":\d.*$", "", r)).exists())
        _assert(not missing, f"{rel}: all referenced paths exist"
                + ("" if not missing else " -- MISSING: " + ", ".join(missing)))


def test_legacy_quarantine_invariants():
    legacy = ROOT / "docs" / "legacy" / "BACKTEST_REPORT_v3.3.0.md"
    _assert(legacy.exists(), "quarantined v3.3.0 report lives under docs/legacy/")
    _assert("LEGACY ARTIFACT" in legacy.read_text()[:600], "quarantine banner present on the report")
    for rel in ("METHODOLOGY.md", "EXECUTIVE_MEMO.md", "SUBMISSION_CHECKLIST.md"):
        _assert("LEGACY ARTIFACT" in (ROOT / rel).read_text()[:600],
                f"{rel} carries the legacy-artifact banner")
    _assert(not (ROOT / "backtest").exists() or not any((ROOT / "backtest").iterdir()),
            "backtest/ no longer ships orphan artifacts")
    # no NON-banner doc may cite the removed backtest artifacts as if real
    offenders = []
    for md in ROOT.rglob("*.md"):
        if "legacy" in md.parts or ".git" in md.parts or "docs" == md.parts[-2:-1]:
            continue
        text = md.read_text()
        if "LEGACY ARTIFACT" in text[:600]:
            continue  # bannered docs are exempt historical records
        if re.search(r"backtest/(config\.json|runner\.py|BACKTEST_REPORT\.md)", text):
            offenders.append(str(md.relative_to(ROOT)))
    _assert(not offenders,
            "no un-bannered doc cites removed backtest artifacts -> " + (", ".join(offenders) or "clean"))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} doc-integrity tests passed.")
