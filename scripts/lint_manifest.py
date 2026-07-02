#!/usr/bin/env python3
"""Manifest sanity lint (v0.9.4, audit M-3/M-4).

Validates manifest.yaml before upload / in CI:

  1. user_config_schema self-consistency: every `default` respects its own
     min/max, pattern, options, and (for arrays) min_items/max_items. This is
     the check that would have caught max_scan_symbols `default: 66, max: 28`.
  2. strategy_config cross-check: where a key also exists in the schema, the
     live value must satisfy the same bounds (numeric strings are coerced).
  3. Required top-level keys are present.

Usage: python3 scripts/lint_manifest.py [manifest.yaml]
Exit codes: 0 clean, 1 findings.
"""
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("lint_manifest: PyYAML required (pip install pyyaml)")
    sys.exit(2)

REQUIRED_TOP = ("name", "version", "description", "market_type",
                "strategy_config", "user_config_schema")


def _num(value):
    """Coerce int/float/'numeric string' to float; None if not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _check_bounds(key, value, spec, where, problems):
    lo, hi = spec.get("min"), spec.get("max")
    n = _num(value)
    if (lo is not None or hi is not None) and n is None and not isinstance(value, list):
        problems.append(f"{where}.{key}: value {value!r} is not numeric but min/max declared")
        return
    if n is not None:
        if lo is not None and n < float(lo):
            problems.append(f"{where}.{key}: {value!r} < min {lo}")
        if hi is not None and n > float(hi):
            problems.append(f"{where}.{key}: {value!r} > max {hi}")
    pattern = spec.get("pattern")
    if pattern and isinstance(value, str) and re.fullmatch(pattern, value) is None:
        problems.append(f"{where}.{key}: {value!r} does not match pattern {pattern!r}")
    options = spec.get("options")
    if options:
        vals = value if isinstance(value, list) else [value]
        bad = [v for v in vals if v not in options]
        if bad:
            problems.append(f"{where}.{key}: value(s) {bad} not in declared options")
    if isinstance(value, list):
        mi, ma = spec.get("min_items"), spec.get("max_items")
        if mi is not None and len(value) < int(mi):
            problems.append(f"{where}.{key}: {len(value)} items < min_items {mi}")
        if ma is not None and len(value) > int(ma):
            problems.append(f"{where}.{key}: {len(value)} items > max_items {ma}")


def lint(path: Path) -> list:
    problems = []
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        return [f"{path}: not a mapping"]
    for key in REQUIRED_TOP:
        if key not in doc:
            problems.append(f"missing top-level key: {key}")
    schema = doc.get("user_config_schema") or {}
    strategy = doc.get("strategy_config") or {}
    for key, spec in schema.items():
        if not isinstance(spec, dict):
            problems.append(f"user_config_schema.{key}: spec is not a mapping")
            continue
        if "default" in spec:
            _check_bounds(key, spec["default"], spec, "user_config_schema", problems)
        if key in strategy:
            _check_bounds(key, strategy[key], spec, "strategy_config", problems)
    return problems


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("manifest.yaml")
    problems = lint(path)
    if problems:
        print(f"lint_manifest: {len(problems)} problem(s) in {path}:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print(f"lint_manifest: {path} clean")


if __name__ == "__main__":
    main()
