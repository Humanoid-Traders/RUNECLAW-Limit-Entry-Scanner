# RUNECLAW v0.6.4 — Isolated-Margin Entry Path (opt-in, fail-closed, trial-gated)

**Status:** implemented on `claude/peaceful-clarke-m91c79`. Default behaviour **unchanged**
(`margin_mode: crossed` → the proven `open_*` wrapper path, byte-for-byte). The isolated
path is **opt-in** and **untested against the live account** — trial in `signal_only` /
tiny size before trusting at normal size.

---

## 1. The finding (live, surfaced by the first v0.6.3 fill)

The first real fill (NEAR long, 2026-06-27) showed `margin_mode: CROSSED`. Investigation
of the live code, not memory:

- `grep` of all of `src` for `isolated|crossed|margin_mode|set_margin` → **zero matches**.
  The playbook never sets margin mode anywhere.
- The open path (`execution.py`) opens via `trade.contract.open_long_limit` /
  `open_short_limit` / `open_long_market` / `open_short_market`. Per the SDK reference
  (`references/sdk/trade/contract.md`), **these composite wrappers take no `margin_mode`
  parameter** — they are `change_leverage` + `place_order`, and `place_order` defaults
  `margin_mode='crossed'`. So **every position this playbook has ever opened was
  cross-margined.** This is not a v0.6.3 regression; it predates v0.4.x.
- **The docs were wrong.** `README.md` and `audit/SECURITY_AUDIT_FINAL.md` both claimed
  *"Isolated margin: code-enforced by design (`margin_mode=isolated` in executor)."*
  There is no such code. `docs/DESIGN_v0.5.0.md:137` already flagged a *"pending
  isolated-margin fix (v0.4.4)"* — documented, never implemented. Both doc claims are
  corrected in this version.
- ("Rule 3" in `METHODOLOGY.md` is *"Verification Infrastructure is Durable"* — unrelated
  to margin. The isolated-margin intent lived only in the README/audit table.)

**Severity in context:** moderate, not acute. Per-trade loss is bounded by the **exchange
SL + `max_loss_usdt` sizing** regardless of margin mode (NEAR carried a live full-size SL
at −2.56%). The incremental risk of crossed is *gap-through-stop*: if price gaps past the
stop trigger, crossed draws the overflow loss from whole-account equity (and other open
positions' margin) instead of capping it at the position's own ~$60 margin. With
`max_concurrent 3` + correlated alts, that is a real tail risk worth closing — but the
SL is the first-line control and it is in force.

## 2. Why the fix is constrained (and therefore opt-in)

The **only** SDK route to isolated margin is the lower-level
`place_order(..., margin_mode='isolated', ...)`. There is no `set_margin_mode` method in
the contract API (confirmed against the full method list). Dropping from the wrappers to
`place_order` means owning `side` / `order_type` / `pos_side` / `trade_side` directly.

Two facts force the trial-gate:
1. **`getagent` is runner-managed and cannot be imported offline**, so the exact literal
   values and the live account's position mode cannot be unit-verified here.
2. The account exhibits **hedge-mode** behaviour (the flat-slot `TypeError` note in
   `open_if_allowed`, from the v0.1.9 diagnostic), so `pos_side`/`trade_side` matter.

A wrong mapping that *broke entries* would be far worse than crossed margin (this codebase
has hit a 100%-order-block bug before — the v0.1.9 except-branch). So the path is built
**fail-closed**, not best-guess-live.

## 3. Design

`_open_isolated(side, entry_mode, symbol, qty, entry_price, leverage, tpsl)`:
```
order_side = "sell" if short else "buy"      # direction PINNED here
pos_side   = "short" if short else "long"    # hedge-mode fields
order_type = "market" if breakout else "limit"
price      = ""      if breakout else entry_price
change_leverage(symbol, leverage)            # best-effort, mirrors the wrapper
place_order(symbol, side=order_side, order_type=order_type, qty=qty, price=price,
            margin_mode="isolated", pos_side=pos_side, trade_side="open",
            tp_trigger_price=tpsl.tp, sl_trigger_price=tpsl.sl)
```

Dispatch in `open_if_allowed` (unchanged for the default):
```
margin_mode = cfg.get("margin_mode", "crossed").lower()
if   margin_mode == "isolated": result = _open_isolated(...)
elif entry_mode  == "breakout": result = open_*_market(...)   # proven, unchanged
else:                           result = open_*_limit(...)     # proven, unchanged
```

**Safety properties:**
- **Default unchanged.** `crossed` → the exact pre-v0.6.4 wrapper calls. Zero behavioural
  change unless an operator sets `margin_mode: isolated`.
- **Fail-closed.** Direction is fixed by long→buy / short→sell. A wrong `pos_side`/
  `trade_side` (e.g. account is actually one-way) makes the exchange **reject** the order →
  `placed: False` → one skipped entry, surfaced as `exchange_reject:…`. It can never open a
  wrong-direction trade.
- **Observable.** The open result carries `margin_mode` so the trial is readable in metrics.
- **No live-position risk.** Only affects *new* opens; existing positions are untouched.

## 4. Validation plan (do not skip)

1. **Offline (done):** unit-test the dispatch + direction mapping with a stubbed
   `getagent` (`tests/test_isolated_margin.py`) — crossed routes to the wrappers, isolated
   routes to `place_order` with `margin_mode='isolated'` and the correct buy/sell + hedge
   fields; the qty/price/tp/sl are threaded through.
2. **Live trial:** set `margin_mode: isolated` on a `signal_only` or tiny-`max_loss`
   `follow_trade` instance. Watch the first open:
   - **Success** → confirm the position shows `margin_mode: isolated` on the exchange.
   - **Reject** (`exchange_reject:…`) → the hedge-field mapping is wrong for this account;
     read the rejection text and adjust `pos_side`/`trade_side` (likely empty them if the
     account is one-way). No wrong trade was placed.
3. Only after a clean isolated open at tiny size → enable at normal size.

Until then: **`margin_mode` stays `crossed`** (the proven path). The default carries the
same loss bound (SL + `max_loss`); isolated is defence-in-depth against gap-through-stop,
earned only after a live read — same discipline as the v0.6.3 trail.

---

## 5. Trail-not-firing investigation + `trail_diag` (also v0.6.4)

**Symptom (live):** the v0.6.3 trailing stop has not ratcheted on either real fill.
NEAR re-entry: entry $1.8331, mark $1.8907 (+3.14%), SL stuck at the *original* $1.7776
(`uTime === cTime` on the SL plan order — never modified). The trail should have moved it.

**Proof it should fire** (computed from public Bitget 1h klines via the research harness's
`fetch_klines` + the real `features._wilder_atr`):

| quantity | value |
|---|---|
| NEAR 1h Wilder ATR(14) | 1.81% of mark |
| `2×ATR` (the trail band) | 3.61% |
| cushion mark→SL | 5.98% |
| trail candidate = mark − 2×ATR | **$1.8224 > SL $1.7776** → should ratchet |

**Static analysis eliminated the obvious suspects** (against the live SL plan-order shape):
- `triggerPrice: "1.7776"` is the FIRST key in `_TRIGGER_KEYS` → the SL read returns 1.7776.
  Not the bug. (My first hypothesis — the missing attribute fallback — was wrong; hardened
  anyway, see below.)
- `openPriceAvg` is the first `_ENTRY_PRICE_KEYS` entry → the position entry reads fine.
- ATR math says fire. So entry ✓ / current ✓ / SL-trigger ✓ / geometry ✓.

What's left are the two **silent** failure points inside `_trail_stop`: the live ATR
fetch returning empty, or **`modify_stop_loss` raising** — the latter very plausible given
the SL confirms `posMode: hedge_mode`. Both were swallowed by bare `return False` / `except`,
indistinguishable from a working trail with nothing to do.

**Fix = make it speak, then fix precisely.** `_trail_stop` now records a one-token reason
into `diag["trail"]` at every exit — `off` / `atr_err:…` / `no_atr` / `sl_err:…` /
`no_sl_order` / `no_sl_trigger` / `hold:<trail><=<cur>` / `tick` / `modify_err:…` /
`set:<price>`. This is the position analogue of the `xpd` diagnostic that turned the silent
4H limit-expiry into a one-line fix. On the next deploy the cycle log names exactly where
the trail dies (almost certainly `modify_err:…` if it's the hedge-mode modify call), and
*that* error text dictates the real fix (e.g. pass `hold_side`/`pos_side` to the modify).
Also added a defensive attribute fallback for the `cur_sl` read (mirrors the `order_id`
read) — harmless robustness, not the root cause.

**Strictly additive / fail-safe preserved:** behaviour is unchanged except for the recorded
string; the trail still only ratchets protectively and no-ops on any failure. Validated in
`tests/test_trail_diag.py` (5 tests) — notably `modify_err` is now visible, and the
attribute-only SL still ratchets.

**Cannot deploy mid-position** (a swap needs a flat book and NEAR is open), so this can't
diagnose the *current* NEAR — it instruments the *next* fill after a redeploy. Interim
narrowing without a deploy: pull the live `position_diag` for NEAR — if it carries
`move_pct`/`be_armed` but no `acted`, the trail is reached and dying inside `_trail_stop`
(ATR or modify); if it carries `note: no_entry_price`/`no_current_price` or is absent, the
position isn't reaching the trail at all.
