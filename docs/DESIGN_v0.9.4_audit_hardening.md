# DESIGN v0.9.4 — Audit Hardening: Fail-Closed Gates, Scoped Ownership, CI

**Status: SHIPPED** · Source: 2026-07 full repository audit (architecture /
security / correctness). This release ships no strategy change — every entry,
exit, and score is byte-identical on a healthy cycle. It changes what happens
on an *unhealthy* one, and who the management layer is allowed to touch.

---

## 1. The findings

The audit's thesis matched the project's own: RUNECLAW's execution layer is
incident-hardened, but three **fail-open paths** sat around the safety stack,
each able to disable controls exactly when they were needed.

### S-1 — a management crash disabled every portfolio gate

`run()`'s fallback for a crashed `manage_open_state` was `{"circuit": "ok"}`:
no `state_blind`, `open_count` 0, `open_symbols` empty. `open_if_allowed` read
that as a healthy flat book — concurrency cap passed, correlation budget
passed, loss breaker unset — and placed a new order on top of an unknown book.
This is the same failure class the v0.6.5 interlock ("never open on an
unreadable book") was built to stop, reintroduced one layer up.

**Fix:** `_safe_manage(cfg, follow)` — only a `manage_open_state` that ran to
completion may authorize opens. Never ran (not follow-trade) or raised ⇒
`state_blind` ⇒ no new entries this cycle. Existing positions keep their
exchange SL/TP; the playbook resumes when the layer recovers. Pinned in
`tests/test_mgmt_failopen.py`.

### S-3 — a failed pending read under-counted the book

A raised or non-success `pending_orders()` read became `pending_records = []`
with only a diagnostic (`pending_error` / `pending_reason`). Resting limits
vanished from `open_count`, so the caps could overshoot during a bridge
outage. The position read already blinded the gate; the pending read now
follows the same rule. Pinned in `tests/test_pending_blind.py`.

### S-2 — size-scoped ownership could act on manual trades

Ownership-by-size adopts ANY account record under the ~$1,500 envelope. A
small manual position could be **time-stop-closed**, its stop **trail-moved**,
or its resting limit **expiry-cancelled** by the bot. The heuristic only ever
protected the user's *larger* trades.

**Fix:** destructive management is now restricted to `_managed_symbols(cfg)` —
the union of universe candidate lists minus each universe's leader, i.e. the
symbols the scanner can actually open. Deliberate asymmetry: out-of-set
records still **count toward the caps** (conservative — fewer new entries),
they are just never acted on. Fail-safe guard: an empty symbol config means
*no restriction*, because a missing cfg must never silently disable the
trail/time-stop (the v0.6.7 lesson). Pinned in `tests/test_ownership_scope.py`.

The full fix remains client-order-id tagging at placement; that needs SDK
confirmation and is deferred (see §4).

### S-4 — the loss breaker read the whole account's fills

`_read_fills` fed the breaker and journal unscoped: a manual whale WIN inside
the window masked bot losses (breaker fails to trip), a manual LOSS tripped it
spuriously, and the live-vs-backtest journal was contaminated. **Fix:**
`_scope_fills` drops fills whose notional is *readable* and above the
ownership envelope; an unreadable-size fill is KEPT, so a serialization change
can only ever err toward more caution, never blind the breaker (the v0.6.7
lesson applied to fills).

### Correctness & config

- **C-1:** `quote_volume=None` escaped the `min_volume_usdt` floor with a
  neutral score — an unknown-liquidity name could qualify. Missing volume is
  now a hard skip (`no_volume_data`), matching the features-layer rule that
  missing data is never guessed. Pinned in `tests/test_scoring_math.py`.
- **M-3:** `user_config_schema.max_scan_symbols` declared `default: 66` under
  `max: 28`. Fixed, and `scripts/lint_manifest.py` (run in CI) now checks every
  schema default against its own bounds and every `strategy_config` value
  against the schema.
- **Default drift:** fallback defaults disagreed with the manifest
  (`time_stop_hours` "4" vs 12, `tp1/tp2` 3.5/7 vs 5/15, `atr_limit_mult` 0.5
  vs 0.3, `trail_atr_mult` 1.0 vs 2.0). Aligned — a key missing from a future
  manifest no longer reverts to a *different* regime per module.

## 2. What was validated

`for f in tests/test_*.py; do python3 "$f"; done` — 15 suites, 91 assertions,
green. New suites: `test_mgmt_failopen` (4), `test_pending_blind` (4),
`test_ownership_scope` (8), and the first direct math tests for the scoring
and risk engines: `test_scoring_math` (9 — regime gate tallies, all three hard
skips, breakout routing, the 100-point weight ceiling) and `test_risk_plan`
(7 — the loss-at-stop ≤ `max_loss_usdt` invariant uncapped/capped/short/
breakout, stop floors, size_factor scaling). No research-harness A/B was run:
this release does not change what a healthy cycle decides, only what an
unhealthy one is allowed to do.

## 3. Infrastructure shipped alongside

- **CI** (`.github/workflows/ci.yml`): the full suite + manifest lint +
  hash-manifest freshness on every push — the validation discipline no longer
  depends on developer memory.
- `scripts/refresh_hashes.py`: regenerates/verifies `audit/MANIFEST.sha256`
  (now covering `src/`, the manifest, audit docs, and logs).
- `tests/_stub.py`: shared getagent stub for new suites. Pre-v0.9.4 suites
  keep their inline copies on purpose — they are frozen incident pins.
- Research fetchers use stdlib `urllib` (curl was an undeclared system
  dependency); `replay.py` documents the B-1 fidelity gap (offline gate is
  2-of-2 vs live 2-of-3 — the harness is *stricter* than live); exchange order
  ids are redacted from committed logs/audit artifacts.

## 4. Deferred (documented, not forgotten)

- **clientOid tagging** — the complete S-2 fix; needs SDK parameter
  confirmation, then trial in signal_only.
- **Dead equity circuit breaker removal** — kept as the `.state/` persistence
  probe (`state_runs`), per DEEP_AUDIT #1; revisit when the runner question is
  settled.
- **Isolated-margin live trial** (DESIGN_v0.6.4) — unchanged, still trial-gated.
- **DBG sentinel channel** — still emitted as an `action="close"` signal;
  migrate when the platform offers a diagnostics channel.
