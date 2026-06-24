# Changelog

All notable changes to the RUNECLAW Limit Entry Scanner. Versions are the public
versions assigned by the GetAgent `publish` step; the deployed instance runs the
latest.

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
