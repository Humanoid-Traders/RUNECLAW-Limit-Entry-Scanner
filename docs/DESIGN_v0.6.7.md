# RUNECLAW v0.6.7 — The snake_case ownership root cause (why the trail NEVER fired)

**Status:** implemented on `claude/peaceful-clarke-m91c79`. Proven against the operator's
exact AAVE position dump. This is the root cause behind the entire trail saga — and likely
part of the over-open episodes too.

## 1. Symptom chain

Live (v0.6.6, AAVE short, account 9147408786): the DBG read **`own2`** stably while **3**
commitments were live (AAVE position + WLD + SUI limits). `pT2`/`oP2` showed both limits
owned, so the missing one was the **AAVE position** — it was not in `owned`. The
`correlation_budget`/`entry_already_pending` tails ruled out a blind read (those would have
fired `state_blind` first). So the position was being **filtered out by `_runeclaw_sized`**.

## 2. Root cause — a one-field snake/camel gap

The operator dumped the raw AAVE position record. Its entry price is **`open_price_avg`**
(snake_case) and mark is **`mark_price`**. But:
- `_record_notional` read price via **camelCase-only** keys (`openPriceAvg` … `markPrice`).
- `_ENTRY_PRICE_KEYS` had `openPriceAvg` (camel) + `average_open_price`/`open_price`, but
  **not `open_price_avg`** (the snake of openPriceAvg).

So on every position record, **price read `None`**:
```
_record_notional(AAVE snake) = None       -> _runeclaw_sized = False  -> EXCLUDED
_record_notional(AAVE camel) = 599.60     -> _runeclaw_sized = True   -> OWNED
```
An excluded position is never placed in `owned_position_records`, so
`_best_effort_position_controls` (trail + auto-BE + time-stop) **never ran on it**.

**This is why the trail never fired all session.** Every "frozen SL" we were poised to call
`modify_err` was the trail *never executing* — the position wasn't owned, so `_trail_stop`
was never called. There was no `position_diag`, no ratchet attempt, nothing. The same gap
silently disabled the **position time-stop** and **auto-breakeven**.

The irony: the codebase *knows* records are snake_case — the v0.1.18 comment says it
"carries both cases, like every other key list," and `_OPEN_TIME_KEYS`, `_HOLD_SIDE_KEYS`,
`_UPNL_KEYS`, `_SIZE_KEYS` all do. Only the entry-price field missed its snake variant.

## 3. Likely second consequence — the over-open

If `current_position()` returned the records but `_runeclaw_sized` filtered them all out,
`open_count` -> 0 and `open_if_allowed` over-opened. v0.6.6's margin cross-check does **not**
catch this (it only fires when `records` is *empty*, but here records are present-then-
filtered). So the 12:33 ETH/PEPE over-open may have been this exclusion, not a read-lie.
v0.6.5/v0.6.6 remain valid for genuine read failures, but **v0.6.7 fixes the actual
mechanism**: once positions are owned, `open_count` is correct and the concurrency /
correlation gates work at the source.

## 4. Fix

- `_ENTRY_PRICE_KEYS`: add `open_price_avg` (+ `avg_price`, `open_avg_price`, `entry_price`).
- `_record_notional`: price now reads order-price keys (limit orders) → `_ENTRY_PRICE_KEYS`
  (positions, incl. snake) → `markPrice`/`mark_price` (last-resort proxy); qty gains
  `base_volume`.

Strictly additive — extra candidate keys only *add* coverage; camelCase still matches first
where present, so no regression. Verified on the real dump (snake → 599.60 → owned).

## 5. Validation

`tests/test_position_ownership.py` (5 tests, against the exact AAVE dump):
- snake_case notional reads ~599.6 (was `None`); snake_case AAVE owned;
- camelCase still owned (no regression); oversized (>$1500) still excluded;
- `manage_open_state` with a snake position → `own1`, AAVE in `filled_symbols`, not blind.

Full suite 27/27 green.

## 6. What this unblocks

After v0.6.7, positions are owned → `_best_effort_position_controls` runs → **the trail
actually executes for the first time.** Only now does the original `trail_diag` question
become testable: when a position runs past its trigger, the SL either ratchets (`set:`) or
fails (`modify_err:<text>` — infra vs hedge-param). Everything before this was a false test.
Deploy, and the next position that runs in favor gives the **first real** trail read.
