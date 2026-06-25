# RUNECLAW — Deep Code Audit

**Date:** 2026-06-25
**Scope:** Static audit of `src/` (execution, scoring, risk, features, main_live) + `manifest.yaml`, cross-referenced against the live SITREP/DBG signals and the existing `audit/SECURITY_AUDIT_FINAL.md`.
**Method:** Source-only. The `getagent` SDK is not vendored and there are no tests in the repo, so SDK-internal behavior (envelope shapes, helper semantics) is reasoned about, not executed. Findings are tagged **VERIFIED** (provable from source in this repo) or **SUSPECTED** (depends on runtime/SDK behavior I cannot execute here).

> This audit is deliberately adversarial. The existing `SECURITY_AUDIT_FINAL.md` marks most controls "PASS / code-enforced"; several of those claims do not survive a close read of the code. The point of this pass is to surface what is actually load-bearing vs. advertised.

---

## Severity summary

| # | Finding | Severity | Tag |
|---|---------|----------|-----|
| 1 | Circuit breaker is non-functional under the project's own stated `.state/` assumption | **HIGH** | VERIFIED |
| 2 | Management layer silently treats an error/unrecognized `pending_orders()` envelope as "0 orders" — the live `pT0` blindness; limit-expiry & circuit-cancel can never fire | **HIGH** | VERIFIED (code path) / SUSPECTED (root-cause shape) |
| 3 | Circuit breaker measures *whole-account* equity, contradicting the size-scoping ownership model | **MED-HIGH** | VERIFIED |
| 4 | Size-based ownership will adopt & force-close a user's manual trade under the notional cap | **MED-HIGH** | VERIFIED |
| 5 | `max_concurrent: 3` is unreachable — correlation budget caps real concurrency at 2 (1 with BTC/ETH) | **MEDIUM** | VERIFIED |
| 6 | Size-scope cap is mis-documented: code = $1,500, README = $1,050, README formula = $150 | **MEDIUM** | VERIFIED |
| 7 | `_extract_rows` row-detection is brittle: one row missing `"symbol"` discards the whole list | **MEDIUM** | VERIFIED |
| 8 | Doc drift across artifacts (`max_concurrent`/`min_score`/version) | **LOW** | VERIFIED |
| 9 | Tick-alignment of SL uses `ROUND_DOWN` regardless of side; realized risk can drift from the sized `max_loss` | **LOW** | VERIFIED |
| 10 | `atr14_est` is `(high-low)/2.5`, not a 14-period ATR — mislabeled | **LOW** | VERIFIED |

---

## 1. Circuit breaker is non-functional under the project's own `.state/` assumption — **HIGH**

The README and risk table advertise a code-enforced daily-loss circuit breaker (`circuit_pause_usdt: 30`, `circuit_stop_usdt: 40`). Its implementation depends entirely on persisting `day_start_equity` across scheduled runs:

```python
# src/execution.py:244-258
equity = _account_equity()
state = _read_state()
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
if equity is not None:
    if state.get("date") != today or "day_start_equity" not in state:
        state["date"] = today
        state["day_start_equity"] = equity        # <-- baseline = CURRENT equity
    today_pnl = equity - float(state.get("day_start_equity", equity))
    ...
    _write_state(state)
```

But the codebase repeatedly asserts that `.state/` does **not** survive between runs:

- `src/execution.py:192-194` — *"The runtime does not persist `.state/` between scheduled runs, so we cannot remember which orders we placed."*
- `src/execution.py:260-261` — *"the only source of truth; `.state/` does not persist between scheduled runs."*
- `CHANGELOG.md` **v0.1.14** — *"Recognize RUNECLAW's own orders/positions by notional size rather than `.state/` (which does not persist between scheduled runs)."*

These two assumptions are mutually exclusive:

- **If `.state/` does not persist** (the stated assumption), then every run `_read_state()` returns `{}`, so `"day_start_equity" not in state` is always true, the baseline is reset to *current* equity, and `today_pnl ≡ equity − equity = 0`. The breaker **can never pause or trip.** The advertised $30/$40 control is dead code.
- **If `.state/` does persist**, the breaker works — but then the entire v0.1.14 "stateless size-scoped ownership" rewrite was unnecessary, and ownership could be done reliably by tagging placed orders in `.state/` instead of guessing by size (see #4).

The project cannot have it both ways. One of the two headline mechanisms is built on a false premise. **This needs a definitive answer to "does `/workspace/.state/` persist across scheduled runs?"** — it determines which of the two mechanisms is actually real.

**Recommended fix:** Establish the ground truth empirically (write a sentinel value, read it next scheduled run, emit it on the DBG channel). Then either (a) if state persists → tag owned orders in state and drop the size heuristic; or (b) if it does not → the equity-delta breaker must be replaced with a stateless measure (e.g. derive realized day-P&L from the exchange's own account-bills/fills endpoint for the current UTC day, not a remembered baseline).

---

## 2. `pending_orders()` envelope blindness — the live `pT0` (**HIGH**)

This is the defect behind the live SITREP. The management read path is hand-rolled:

```python
# src/execution.py:268-274
try:
    pending_raw = trade.contract.pending_orders()
except Exception as exc:
    pending_raw = None
    status["pending_error"] = type(exc).__name__
pending_records = _extract_rows(pending_raw) if pending_raw is not None else []
status["pending_shape"] = _shape(pending_raw)[:40] if pending_raw is not None else "none"
```

Two structural problems:

1. **No success check.** `pending_raw` is never tested with `trade.is_success(...)` or a `code` check. If the SDK returns a *non-raising error envelope* (an error that comes back as data rather than an exception), `_extract_rows` simply finds no rows and the code reports `pending_total = 0` — indistinguishable from "genuinely no open orders." Every downstream control that needs the order list (`_best_effort_limit_expiry`, the circuit `_flatten_owned` cancel loop) then silently no-ops.

2. **The observed shape is a raw envelope, not a parsed list.** The live DBG tail is `shp.code;message;data;trace_id>(…)` (recorded both in this session's SITREP and in `SECURITY_AUDIT_FINAL.md:55`, the latter alongside `c1p1` — i.e. an order *was* placed successfully yet the manager still saw `pT0`). Keys `code / message / data / trace_id` are an API/gateway wrapper. The fact that a successful placement (`p1`) coexists with `pT0` is the proof: **a real resting order existed and the manager could not see it.** Because the manager is blind, the 4h `limit_expiry_hours` cancel can never fire (consistent with "expiry never fired all night"), and a circuit hard-stop would fail to cancel the resting order.

**Why `_extract_rows` yields `[]` (SUSPECTED, needs SDK):** the most likely causes, in order:
   - `data` is an error payload (non-success `code`) → no order rows exist to find.
   - the order rows are nested under a key not in the search list, or each row keys its symbol as something other than the literal `"symbol"`, so the `all("symbol" in r for r in recs)` gate (line 154) rejects them.

**Recommended fix (high value, low risk):**
   - Gate on success first: `if not trade.is_success(pending_raw): status["pending_error"] = _result_reason(pending_raw)` and surface it — never collapse an error into `pT0`.
   - Stop hand-rolling the parse. The write path already trusts an SDK helper — `trade.helpers.select_contract_order(trade.contract.pending_orders(symbol=...))` (line 497) — and positions use `trade.helpers.contract_position_records(...)` (line 264). Use the SDK's own pending-order records helper in the manager too, so read and write paths parse identically. The bespoke recursive `_extract_rows` is the root cause of every "parse miss" in the changelog (v0.1.4, v0.1.16) and will keep regressing.

---

## 3. Circuit breaker watches the whole account, not RUNECLAW — **MED-HIGH**

Even if #1 is resolved so the breaker runs, it measures `_account_equity()` — total account equity — against a remembered baseline. But the account demonstrably holds positions RUNECLAW disclaims: `SECURITY_AUDIT_FINAL.md:70-74` documents a 72.6 SOL / ~$4,998 position that RUNECLAW explicitly does **not** own (above the size-scope cap).

Consequence: equity swings from that ~$5k SOL position (and any other manual trade) flow straight into `today_pnl` and can pause/trip RUNECLAW's breaker — halting the agent on losses it did not cause — or, worse, *mask* RUNECLAW's own losses with unrelated account gains so the breaker fails to trip when it should. This directly contradicts the size-scoping ownership model that the rest of the file is built around (manage only what we placed).

**Recommended fix:** Compute day-P&L from RUNECLAW-owned realized fills only (size-scoped, consistent with `_runeclaw_sized`), not from account equity.

---

## 4. Size-based ownership adopts (and can force-close) a user's manual trade — **MED-HIGH**

Ownership is inferred purely by notional:

```python
# src/execution.py:187-202
def _runeclaw_sized(record, cfg) -> bool:
    notional = _record_notional(record)
    ...
    return notional <= budget * leverage * mult       # 100 * 10 * 1.5 = $1,500
```

Any account position **or resting order** with notional ≤ $1,500 is treated as RUNECLAW's. On a circuit hard-stop, `_flatten_owned` will `cancel_order` / `close_position` every such record (lines 307-329). So a user's own small manual trade (a $1,000 hedge, a $500 scalp) is silently adopted and can be cancelled/closed by the agent. The size heuristic only protects *large* manual trades; it gives no protection at all below the cap, and the agent acts on real money it never placed.

The comment (lines 188-195) acknowledges the heuristic but frames it as safe because "the user's manual trades have been ~10x larger" — an empirical assumption about one user's habits, not an invariant. This is a direct consequence of the `.state/`-doesn't-persist branch of #1; if state persists, replace the heuristic with order tagging and this risk disappears.

---

## 5. `max_concurrent: 3` is unreachable — real cap is 2 — **MEDIUM**

```python
# src/execution.py:462-472
max_concurrent = int(cfg.get("max_concurrent", 3))
if open_count >= max_concurrent:
    return ... "max_concurrent_reached"
max_corr = int(cfg.get("max_correlated_alts", 2))
if any(s in ("BTCUSDT", "ETHUSDT") for s in open_symbols):
    max_corr = min(max_corr, 1)
if symbol.upper() not in open_symbols and len(open_symbols) >= max_corr:
    return ... "correlation_budget"
```

The correlation budget treats **every** open symbol as one correlated group (by design — comment lines 466-467). With `max_correlated_alts: 2`, once 2 positions are open the budget blocks all new distinct symbols, so the `max_concurrent: 3` gate is never the binding constraint. Effective concurrency is **2** (or **1** when BTC/ETH is held). This is conservative/safe, but the README advertises "Max concurrent positions | 3", which is misleading — the third slot can only ever be filled by a symbol already open (which the duplicate guards then reject anyway).

**Recommended fix:** either raise `max_correlated_alts` to make `max_concurrent` reachable, or document the real cap as 2.

---

## 6. Size-scope cap is mis-documented — **MEDIUM**

Three different numbers for the same control:

| Source | Value | Formula implied |
|--------|-------|-----------------|
| Code (`execution.py:202`) | **$1,500** | `margin_budget × leverage × size_scope_mult` = 100 × 10 × 1.5 |
| README risk table | $1,050 | "`size_scope_mult: 1.5 × margin_budget`" |
| README size-scoping paragraph | $1,050 | "`size_scope_mult × margin_budget`" = 1.5 × 100 = $150 |

The README formula omits the **leverage** factor that the code actually applies, and the stated "$1,050" matches neither the code ($1,500) nor its own formula ($150). The number a subscriber reads as their ownership boundary is wrong. (Note the SOL exclusion example is consistent with the *code's* $1,500, confirming $1,500 is the live value.)

**Recommended fix:** README should state the cap as `margin_budget × leverage × size_scope_mult = $1,500` at default config.

---

## 7. `_extract_rows` row-detection is brittle — **MEDIUM**

```python
# src/execution.py:152-154
recs = [m for m in (_to_mapping(x) for x in value) if m]
if recs and all("symbol" in r for r in recs):
    return recs
```

The `all(...)` predicate means a **single** row lacking a literal `"symbol"` key (e.g. a TP/SL plan order, a conditional order, or any row using a different symbol key) causes the entire list to be rejected, after which the function recurses and most likely returns `[]`. Pending-order endpoints commonly interleave entry orders with plan/TP-SL rows. This makes the parser fragile exactly where Bitget responses are heterogeneous, and is a plausible contributor to #2. Prefer `any(...)` + per-row filtering, or (better) the SDK helper.

---

## 8. Documentation drift across artifacts — **LOW**

- `SECURITY_AUDIT_FINAL.md:30-31` lists `max_concurrent=6` and `min_score=65`. The manifest and README say **3** and **70**. The security audit's "PASS — code-enforced" rows cite values that aren't in the code.
- `manifest.yaml` `version: "0.1.0"` and `execution_mode: signal_only` while the live instance is **v0.1.19 / follow_trade**. The manifest is a frozen submission artifact; it does not describe the running build. Anyone reading the manifest to understand live behavior will be misled (e.g. `signal_only` implies no order placement at all).

These are documentation-integrity issues, not code bugs, but in a trading system the audit trail *is* part of the control surface.

---

## 9. SL tick-alignment uses `ROUND_DOWN` for both sides — **LOW**

`_align` (lines 555-568) always rounds **down** to the tick. Sizing (`risk.build_plan`) solves qty from the *un-aligned* `sl_pct`, but the order is placed with the *aligned* SL. For a long, rounding the stop down widens the stop slightly, so realized loss at stop can marginally exceed the sized `max_loss_usdt`. The error is sub-tick × qty and negligible for liquid symbols, but it means `max_loss` is "≈$15", not "≤$15". Consider rounding the stop in the conservative (risk-reducing) direction per side, or re-deriving qty from the aligned stop.

---

## 10. `atr14_est` is not an ATR — **LOW**

`risk.build_plan:50` sets `atr = (high - low) / 2.5` and `main_live.py:182` surfaces it as `atr14_est`. This is today's range/2.5, not a 14-period ATR. It's a defensible proxy for sizing, but the `atr14_est` label overstates what's computed. Rename to `range_proxy` or compute a real ATR from klines.

---

## What the existing audit got right

For balance: the side-aware plan construction (`risk.py`), the regime gate (`scoring.regime`), the extension/overextension skip (`scoring.py:124-136`), the pre-placement staleness skip (`main_live.py:150-163`), and the rejection-reason surfacing (`_result_reason`, `_exc_brief`) are sound and well-commented. The duplicate-entry guards in `open_if_allowed` correctly fail *open* (proceed on parse error) after the v0.1.10 lesson, which is the right call for an entry path. The diagnostics-on-a-readable-channel pattern is genuinely good operational engineering.

---

## Priority order for fixes

1. **Resolve #1** — determine empirically whether `/workspace/.state/` persists. Everything else (circuit breaker reality, ownership model) hinges on this one fact.
2. **Fix #2** — add a success check to `pending_orders()` and switch the manager to the SDK's own order-records helper. This is what unblocks limit-expiry and the live `pT0`.
3. **#3 / #4** — scope the circuit breaker and ownership to RUNECLAW's own fills, not the whole account / arbitrary sub-cap records.
4. **#5 / #6 / #8** — reconcile docs with code (concurrency cap, size-scope number, manifest vs. live).
5. **#7 / #9 / #10** — hardening and labeling cleanups.

---

*Static audit; no code was executed (SDK not vendored, no test suite). VERIFIED findings are provable from the source in this repository; SUSPECTED findings name the exact runtime fact needed to confirm them.*
