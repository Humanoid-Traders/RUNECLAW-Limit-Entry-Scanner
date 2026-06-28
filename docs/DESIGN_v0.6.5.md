# RUNECLAW v0.6.5 — State-Blind Interlock (safety; prevents over-open on an unreadable book)

**Status:** implemented on `claude/peaceful-clarke-m91c79`. Strictly additive, fail-safe:
it can only *block* a new open, never cause one. No change to the proven open path when
the book reads cleanly.

## 1. The incident (live, v0.6.4 trial)

During the v0.6.4 follow_trade trial the playbook held two live positions (WLD short,
NEAR long). At **06:32 UTC** a signal logged `exchange_reject:?:Failed_to_call`, and for
**three consecutive cron cycles (~45 min)** the DBG read `own0-pT0-oP0` while both positions
were live on the exchange. The platform raised its "non-strategy positions / manual
intervention detected" banner. Both positions stayed protected by their exchange SL/TP; the
operator disabled and went flat.

**Root cause = infra, not strategy logic.** `Failed_to_call` hit both the position *read*
and a *placement* in the same window — reads and writes failing together means the runtime's
trade-call bridge to the subaccount was down, not a bug in RUNECLAW. (Re-enabling fresh
re-establishes the bridge.)

## 2. The real code gap the incident exposed

`manage_open_state` read positions like this:
```python
try:
    positions = trade.contract.current_position()
    records = trade.helpers.contract_position_records(positions) or []
except Exception:
    records = []          # <-- a FAILED read becomes "no positions"
```
A failed read was indistinguishable from a flat book: `records=[]` -> `own0` ->
`open_count: 0`. And `open_if_allowed` gated only on `open_count` / correlation / circuit —
**it never checked whether the read had actually succeeded.** So while blind, the playbook
believed it was flat and **tried to place a new entry on top of two untracked positions**
(the 06:32 `Failed_to_call` was that very placement — stopped only because the same infra
outage failed the write too). Relying on the write also failing is luck, not design. Worst
case without that luck: a stack of untracked positions, blowing past `max_concurrent` and the
correlated-alt cap.

## 3. The interlock

Principle: **a playbook that cannot see its own book must not open new trades.**

- `manage_open_state`: the position read now also probes `is_success` (mirroring the pending
  path), so a non-raising error envelope is caught too. On a raised exception *or* a failed
  envelope, set `status["state_blind"] = True` (and record `position_query_error` /
  `position_query_reason`).
- `open_if_allowed`: if `mgmt["state_blind"]`, return `{"placed": False, "reason":
  "state_blind"}` before any concurrency/correlation/open logic — no order is attempted.

**Safety properties:**
- **Strictly additive.** Only adds a refusal-to-open condition; it can never open *more*.
- **Existing positions untouched.** Their exchange SL/TP stay in force; the playbook simply
  does not ADD while blind.
- **Self-healing.** The moment a position read succeeds again, `state_blind` clears and normal
  placement resumes — consistent with the stateless ownership design (no persisted state to
  reset).
- **Observable.** `state_blind` / `position_query_reason` surface in status for the DBG.

## 4. Validation

`tests/test_state_blind.py` (5 tests):
- `manage_open_state` sets `state_blind` when `current_position` **raises**.
- ...and when it returns a **non-success error envelope** (does not raise).
- a **clean empty** read is NOT blind (genuinely flat still opens).
- `open_if_allowed` **refuses** to open when `state_blind` — and makes **no** open/place call.
- not-blind + room still opens via the proven wrapper (no regression).

## 5. Priority note

This is a **safety interlock** and ranks above the v0.6.3 trailing-stop work: it removes a
live-witnessed over-leverage hole during infra outages, whereas the trail is an upside
optimization that is still awaiting a clean live diagnostic (the v0.6.4 trial never produced
one — WLD never sat <= ~$0.4358 at a cron sampling instant). The trail diagnostic
(`trail_diag`, v0.6.4) stays armed for the next clean run; this interlock ships first.
