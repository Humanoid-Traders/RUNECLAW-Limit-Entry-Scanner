#!/usr/bin/env python3
"""Regenerate + verify audit/MANIFEST.sha256 (v0.9.4, audit hygiene).

The hash manifest existed but covered only 3 files and had no tooling -- an
integrity mechanism that can silently rot. This script hashes the package
source, the live manifest, and the audit/log artifacts.

Usage:
  python3 scripts/refresh_hashes.py            # rewrite audit/MANIFEST.sha256
  python3 scripts/refresh_hashes.py --check    # verify, exit 1 on mismatch
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "audit" / "MANIFEST.sha256"

PATTERNS = ("manifest.yaml", "src/*.py", "audit/*.md", "logs/*.csv",
            "METHODOLOGY.md", "README.md")


def _targets():
    files = []
    for pat in PATTERNS:
        files.extend(sorted(ROOT.glob(pat)))
    return [f for f in files if f.is_file()]


def _line(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"{digest}  {path.relative_to(ROOT).as_posix()}"


def main():
    lines = [_line(f) for f in _targets()]
    body = "\n".join(lines) + "\n"
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != body:
            print("refresh_hashes: MANIFEST.sha256 is STALE -- run scripts/refresh_hashes.py")
            sys.exit(1)
        print(f"refresh_hashes: {len(lines)} hashes verified")
        return
    OUT.write_text(body, encoding="utf-8")
    print(f"refresh_hashes: wrote {len(lines)} hashes to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
