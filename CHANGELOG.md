# Changelog

All notable changes to the RUNECLAW Limit Entry Scanner. Versions are the public
versions assigned by the GetAgent `publish` step; the deployed instance runs the
latest.

## v0.9.4 — audit hardening (fail-closed gates, scoped ownership, CI)
Full write-up: `docs/DESIGN_v0.9.4_audit_hardening.md`. Fixes from the 2026-07
repository audit, safety-first order:
- **S-1 fail-open closed:** a crashed (or never-run) `manage_open_state` now
  yields a `state_blind` management snapshot, so `open_if_allowed` refuses to
  place instead of reading a crash as a flat, gate-free book (`_safe_manage`).
- **S-3 fail-open closed:** a failed `pending_orders` read (raise or error
  envelope) now sets `state_blind` — an invisible resting-limit count can no
  longer let the concurrency/correlation caps overshoot.
- **S-2 scoped ownership:** destructive management (time-stop close, limit
  cancel, stop modify, circuit flatten) is restricted to symbols the scanner can
  actually open (universe candidates minus leaders); out-of-set records still
  count toward the caps. Absent universe config = unrestricted (v0.6.7 lesson).
- **S-4 scoped fills:** the loss breaker + live journal drop fills whose
  notional is readable and above the ownership envelope; unreadable-size fills
  are kept (fail-conservative — never blinds the breaker).
- **C-1 scoring:** missing `quote_volume` is now a hard skip
  (`no_volume_data`) — an unknown-liquidity name can no longer qualify.
- **M-3 manifest:** `user_config_schema.max_scan_symbols` default 66 violated
  its own `max: 28`; fixed, and `scripts/lint_manifest.py` now guards the class.
- **CI:** `.github/workflows/ci.yml` runs the full test suite + manifest lint +
  hash-manifest check on every push. New suites: mgmt-failopen, pending-blind,
  ownership-scope, and the first direct math tests for scoring + risk
  (91 assertions total across 15 suites).
- Fallback defaults aligned with the manifest (`time_stop_hours` 12,
  `trail_atr_mult` 2.0, `tp1/tp2` 5/15, `atr_limit_mult` 0.3); research fetchers
  use stdlib urllib instead of shelling out to curl; exchange order ids redacted
  from committed logs/audit artifacts; README synced to the armed loss breaker.

> Note: v0.2.0 – v0.9.3 were documented in `docs/DESIGN_v0.*.md` rather than
> here; this changelog resumes at v0.9.4.

## v0.1.18 — stale-limit expiry fix
- Add snake_case `create_time` (and `c_time` / `update_time` variants) to
  `_OPEN_TIME_KEYS`. The SDK serializes records to snake_case, so order age was
  resolving to `None`, the 4h `limit_expiry_hours` check silently no-op'd, and a
  real resting limit could sit for hours past expiry. The position **time-stop**
  reads the same key list, so it was latently broken too — both are now repaired.

## v0.1.17 — pre-placement staleness skip
- Decline an entry at the source when its limit would already sit more than
  `limit_chase_pct` from price, instead of placing then pruning a dead order.

## v0.1.16 — pending-orders parse fix
- Recursively locate the order-row list inside the nested unfiltered
  `pending_orders()` envelope (the prior flat parse reported zero pending orders).

## v0.1.15 — management-chain diagnostic
- Surface every link of the management chain (ownership, pending counts, actions,
  outcome) on a catalog-readable channel to pinpoint the prune bug.

## v0.1.14 — stateless, size-scoped ownership
- Recognize RUNECLAW's own orders/positions by notional size rather than `.state/`
  (which does not persist between scheduled runs), so manual trades are never
  touched.

## v0.1.13 — stale-limit handler
- Cancel a resting limit the market has left behind by more than `limit_chase_pct`
  in the un-fillable direction.

## v0.1.12 — concurrency accounting
- Count resting limits (not just filled positions) toward `max_concurrent`; quiet
  benign diagnostics.

## v0.1.11 — order precision
- Tick-align take-profit / stop-loss trigger prices and surface the real exchange
  validation message on rejects.

## v0.1.10 — entry-blocking fix
- Replace the position check that raised on flat hedge-mode slots and blocked
  100% of entries with a non-blocking count.

## v0.1.9 — placement observability
- Emit the placement reason on a catalog-readable channel so silent failures
  become operator-readable.

## v0.1.8 — extension guard
- Stop the pullback scorer from picking momentum runaways the limit can never fill
  (`max_vwap_ext_pct`).

## v0.1.7 — rejection visibility
- Surface the exchange rejection reason and a follow-trade diagnostic.

## v0.1.6 — execution path
- Execute via `emit_signal_or_follow` (fix zero-placement).

## v0.1.5 — scoped controls
- Scope all portfolio controls to RUNECLAW-owned positions only.

## v0.1.4 — feature fix
- Fix order-book object extraction (root cause of zero fills).

## v0.1.3 — metrics
- Promote diagnostics into top-level signal metrics.

## v0.1.2 — execution + diagnostics
- In-line execution with diagnostics embedded in the emitted signal.

## v0.1.1 — tunable
- Expose `atr_limit_mult` as a subscriber override.

## v0.1.0 — initial
- Two-sided pullback-limit scanner: regime gate, five-dimension blended score,
  side-aware limit / stop / take-profit ladder, risk-based sizing, and
  portfolio-level risk controls.
