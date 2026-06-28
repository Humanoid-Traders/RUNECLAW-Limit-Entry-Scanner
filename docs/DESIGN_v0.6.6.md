# RUNECLAW v0.6.6 — Blind-Spot Detector (the "read-lies-empty" variant)

**Status:** implemented on `claude/peaceful-clarke-m91c79`. Built against the **confirmed**
`total_value()` shape (operator dump), not a guessed field. Strictly additive, fail-open.

## 1. The incident (live, v0.6.5 trial — same account, confirmed)

The v0.6.5 deployment (account `9147408786`, Classic) opened an ETH short at **12:29**.
At the **12:33** cron the playbook read **`own0`** — blind to its own live ETH — so it scored
a new candidate and placed a limit, which filled into a PEPE long at **12:37**. **It opened a
second position because it could not see the first.** Both positions carried RUNECLAW's exact
SL/TP fingerprint (−2.5%/+15% and +1.67%/−15%), confirming playbook origin; the operator's
TradeSDK read the same account and saw both. So this was **one account**, the positions were
the playbook's own, and the read simply returned empty.

## 2. Why v0.6.5 didn't catch it

The v0.6.5 state-blind interlock triggers when `current_position()` **raises** or returns a
non-success **error envelope**. Here the call **succeeded and returned empty** — a flaky
trade-bridge "read lie" (the same bridge that threw `Failed_to_call` twice earlier). A
successful-but-wrong read is invisible to an error check. The result: `records=[]` ->
`open_count: 0` -> `open_if_allowed` believed the book was flat and added on top. The
correlation budget (which would cap ETH + 1 alt) was defeated for the same reason.

## 3. The detector — an account cross-check

A read claiming "no positions" can be falsified by the account's own margin ledger.
`trade.account.total_value()` returns per-asset `contract_assets[]` with:

| field | flat | position open | use |
|---|---|---|---|
| `crossedMargin` / `isolatedMargin` | `"0"` | non-zero | **reliable** — position margin, PnL-independent |
| `unrealizedPL` | `"0"` | non-zero | weak — ~0 at breakeven |
| `locked` | `"0"` | non-zero | dirty — a resting limit also locks funds |

So v0.6.6 uses **position margin** (`crossedMargin + isolatedMargin`):

```
if current_position() read EMPTY and not already state_blind:
    m = account position margin (crossed + isolated, summed across contract_assets)
    if m is not None and m > 0:        # positions exist but the read missed them
        state_blind = True;  blind_reason = "pos_margin_<m>_vs_empty"
```

`open_if_allowed` already refuses to open while `state_blind` (v0.6.5), so no change there.

**Safety properties:**
- **Fail-open / no false-positive.** Unreadable `total_value` (raise, missing field) -> `None`
  -> no block. A genuinely flat account (`margin 0`) is never blinded. The detector only fires
  on a *positive* discrepancy: margin locked **and** positions empty.
- **Strictly additive.** Only ever *blocks* a new open; never opens more, never touches
  existing positions (they keep their exchange SL/TP).
- **Cheap.** The cross-check only runs when the position read came back empty.
- **Observable.** `blind_reason` surfaces in status for the DBG.

## 4. Validation

`tests/test_blind_spot.py` (7 tests, against the real dump shape):
- `_account_position_margin` sums `crossedMargin` from the real shape (59.93); flat -> 0.0;
  raise -> `None`.
- `manage_open_state`: empty positions + locked margin -> `state_blind` + `blind_reason`;
  empty + 0 margin -> NOT blind (flat); empty + unreadable account -> NOT blind (fail-open);
  positions visible -> not blind, `own1`.

Full suite 22/22 green (blind-spot + state-blind + isolated-margin + trail_diag).

## 5. Residual limit

If the bridge is so degraded that **both** `current_position()` and `total_value()` lie in the
same cycle, the cross-check can't help (it relies on the margin read being honest when the
position read is not). The 12:33 evidence (TradeSDK read margin/equity fine while the position
read was empty) suggests they fail independently, so the cross-check should catch the common
case. The disambiguation remains: a `Failed_to_call`-class error is infra (re-test), a
persistent discrepancy is the bug.

## 6. Stacking with prior interlocks

- v0.6.5 `state_blind`: read **errors** (raise / error envelope).
- v0.6.6 `state_blind`: read **lies empty** (success + margin-says-otherwise).
Together they cover both failure modes; both route to the same `open_if_allowed` refusal.
The trail (`trail_diag`, v0.6.4) and isolated margin (v0.6.4) are unaffected.
