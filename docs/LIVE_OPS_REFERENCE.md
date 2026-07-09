# RUNECLAW Live Operations Reference

Current as of **v0.9.42** (manifest.yaml). This is a living reference for
reading live SITREPs and the compact SCAN line without re-deriving mechanics
from source each time. **The repo is always the source of truth** — if this
doc and `manifest.yaml` / `src/*.py` ever disagree, trust the code and flag
the doc as stale.

---

## 1. The SCAN line, decoded

Format (`src/main_live.py`, `_fold_exec_onto_scan` + `_scan_digest`):

```
SCAN-<digest>|[nof-]o<own>p<pend><breaker><cx>-<tail>[|<fate>]
```

Worked example: `SCAN-cry:sETH97q-met:LXAG44x-equ:LMSTR70q|o1p0-hld.MSTR+0.t7h`

| Piece | Meaning |
|---|---|
| `cry:sETH97q` | crypto universe: regime **short-gated** (`s`), best candidate ETH, score 97, **qualified** (`q`) |
| `met:LXAG44x` | metals universe: regime **long-gated** (`L`), best candidate XAG, score 44, **not qualified** (`x`, below `min_score` or hard-skipped) |
| `equ:LMSTR70q` | equities universe: regime **long-gated**, MSTR, score 70, qualified |
| `o1p0` | 1 owned position, 0 pending orders (stateless size-based ownership) |
| `hld.MSTR+0.t7h` | tail: MSTR is the held/managed position, ~0% move, 7h old |

### ⚠️ The single most common misread: `L`/`s` is NOT a quality tier

`L` = that universe's regime leader is **long-gated**. `s` = **short-gated**.
`n` = neutral (universe stood down). This is the **direction of the regime
gate**, not "sub-threshold" or "signal-only." A `q`-suffixed short-gated name
(e.g. `sETH97q`) is a **fully qualified, strong short setup** — arguably a
stronger signal than a low-scoring long. Do not read the `s` prefix as "not
real." (`_scan_digest`, `src/main_live.py:552-584`)

### Per-universe digest token: `<abbr>:<L|s|n><SYM><score><q|x>`

- `q` = qualified: score ≥ `min_score` (70) **and** not hard-skipped.
- `x` = below the score floor, or hard-skipped (funding crowding, event
  blackout, etc.) at the ticker-scan (pass-1) stage.
- Bare `<abbr>:n` = leader regime neutral, universe stood down entirely.
- Bare `<abbr>:<L|s>-` = gated, but no candidate scored at all.

### Exec-state tail: `o<own>p<pend><breaker><cx>-<tail>`

- `o<n>` = owned positions, `p<n>` = pending orders — both **stateless,
  size-scoped** ownership (`_runeclaw_sized`: anything within
  `margin_budget × leverage × size_scope_mult` of the bot's own sizing is
  counted as the bot's own; `size_scope_mult` = 1.5). A manual trade ~10x the
  bot's size is invisible to this count by design.
- `nof-` prefix = this cycle ran with `is_follow_trade() == False` (an
  eval/pre-window cycle, not live follow-trade management).
- Breaker token = realized-loss breaker state: `b<n>` armed with ~$n of
  further realized loss to the trip; `b!<n>` tripped, ~$n past the threshold;
  `b?<stage>` armed but **blind** this cycle, with the failing stage named —
  `r` fills read failed, `t` no row timestamp parsed, `k` no recognised
  profit field, `e` fills read **empty on a state-blind cycle** (v0.9.36 —
  "empty" is untrustworthy while sibling reads fail; healthy-cycle empty
  reads full headroom `b<threshold>`, not blind). Under the 63-char budget
  the token degrades gracefully: full → 4-char stage form → dropped.
- `d:<SYM><score>` trailing segment (v0.9.41) = the **shadow-discovery**
  forward test's top-scoring candidate this cycle. **Multi-class since v0.9.42** (operator "add all"): the scan covers every asset class Bitget lists — crypto (BTC-gated), tokenized stocks + ETFs (QQQ-gated), commodities (XAU-gated) — each routed to its correct regime leader. LOGGED only, NEVER traded (the full per-class list with class+leader tags is in `metrics.discovery`); the token shows only the single top-scored name. Lowest budget priority. Armed 2026-07-08.
  **CAVEAT (proven live 2026-07-09): this token is near-unobservable** — a scored
  3-universe digest is ~42 of the 63 chars, so `d:` is budget-dropped on every
  real board (10/10 consecutive live lines carried none). Do NOT read its
  absence as "discovery blind." Use the dedicated `DISC-` line below instead.
- **`DISC-<source>-<n>c[-<SYM><score>]`** dedicated marker (v0.9.44) = the
  discovery forward-test's health, emitted as its **own signal line** (the
  Recent-Signals view shows one signal per cycle; this claims that slot when
  due). It exists because the `d:` token above is budget-dead and
  `metrics.discovery` is unreachable through the operator's tools (config-level
  only). Read `source`: **`tickers`/`ticker` = bulk surface LIVE** (forward test
  collecting), **`no_bulk_surface` = BLIND** (SDK-native per-symbol fallback
  warranted), **`error:<Type>` = exception path**. `<n>c` = candidate count;
  optional `-<SYM><score>` = top scored candidate. Cadence: **LOUD every cycle
  while blind/errored** (a persistent blind read must not hide behind the board),
  a **quiet hourly (:00) heartbeat while healthy** so SCAN owns the other cycles.
  Decode with `python3 research/decode.py "DISC-..."`. Diagnostic-only, never trades.
- `cx` suffix = circuit/ops notes, when present (v0.9.39): `-cx` = the legacy
  equity circuit is non-functional (state never persists — historical); `-dw` =
  account-day realized past the Rule-10 warn line (warn only, entries flow);
  `-!clk` / `-!mgn` / `-!sl` = **invariant sentinel confession** — the engine
  found its own live book violating a contract (wrong hold clock / crossed
  margin under an isolated manifest / oversized stop risk). A `-!` token is
  never noise: decode it same-day.

### Tail priority chain (`_dbg_tail`, `src/main_live.py:620-648`) — first match wins

1. `bl.<why>` — **state_blind**: the book (own/pending) couldn't be read reliably.
2. `act.<label>` — a management action fired **this cycle** (`trail_stop`,
   `steplock`, `scale_out`, `time_stop_close`, `auto_be`, `limit_expiry`).
3. `xpd.<d>` — a stuck owned-pending order the bot can't time-expire.
4. `hld.<...>` — a position is held and quiet (see below). **This wins over
   everything below it** — including whatever the raw entry-decision path did
   for a *different* symbol that cycle. See §2 caveat.
5. `no.<reason>` — a watch stand-down: *why* the cycle placed nothing.
6. `perr.<code>` — the pending-order fetch genuinely **failed** (this is the
   real "data problem" token — distinct from `no.enrich0`, which is a
   deliberate decision, not a failure).
7. `sig.<L|s><SYM>` — an actionable decision whose open-path gave no reason;
   gated on `full_reason == "none"` so a real reason (below) is never masked.
8. Fallback: the raw open-path reason (`entry_already_pending`,
   `already_in_position`, `sizing_failed`, `cooldown`, ...).

Common `no.<reason>` short forms (`_WATCH_SHORT`):
`neutral` (all regimes neutral) · `lowscore` (nothing at/above `min_score`)
· `enrich0` (candidates qualified at ticker-stage but ALL were skipped in
second-pass enrichment — a decision, not a pipeline fault; see §2) ·
`sizefail` · `cbpause` / `cbtrip` (circuit breaker).

### Held-position token: `hld.<sym><±move%><flags>.t<age>h`

Built by `_held_token` (`src/main_live.py:478-510`) from the **oldest**
currently-managed position. Format pieces:

- `<±move%>` = `int(round(move_pct))` — a **stateless, memoryless, freshly
  recomputed** percentage move, rounded to the nearest whole percent, **every
  single cycle**. There is no "tier," no hysteresis, no engine state machine
  behind this number. `+1` → `+0` across two cycles just means the live move
  rounded down — it does **not** mean the engine "de-tiered" anything, and it
  has **zero effect** on the trail, breakeven, or steplock logic (those use
  their own full-precision thresholds — see §3).
- flags: `a` = breakeven armed · `l` = a breakeven/steplock floor is
  currently setting the stop (not the raw ATR trail) · `s` = scale-out armed
  or trimmed · `r` = the trail set the stop this cycle · **`P`/`B` (v0.9.37)
  = which hold clock GOVERNS** (P = pullback 4h, B = breakout cap; neither =
  mode recovery returned unknown → the 12h global cap governs). A
  pullback-entered position whose `hld` token lacks `P` is running the wrong
  clock — that absence is exactly how the 2026-07-08 ETH-short incident
  (pullback held 12h09m past a 4h cap) would have been visible.
- `.t<age>h` = hours held, floor-rounded (e.g. age 7.9h renders `t7h`). This
  is a **plain age counter**, not a ceiling — it climbs every cycle. It has no
  relationship to a "2H/4H" rule; the only unconditional clocks are the
  per-mode time-stops (v0.9.22: pullback 4h / breakout+unknown 12h — §3),
  and they stay unconditional on P&L.

### ⚠️ Observability gap worth knowing

Because `hld` outranks `sig`/`no`/the raw reason in the tail priority chain,
**whenever any position is being quietly held, the compact line cannot tell
you what the entry pipeline decided for a *different* symbol that cycle** —
only the Open Positions / Pending Orders sections of the account are ground
truth for "did anything new get placed." A high ticker-score on a different
symbol (e.g. `sETH97q`) coexisting with `hld.MSTR...` does not by itself mean
that candidate was blocked *by* the MSTR hold — per-symbol dedupe only blocks
re-entry on the *same* symbol (§2). It most likely means that candidate was
demoted or skipped in second-pass enrichment, but the exact filter isn't
visible without that cycle's live funding/trend data.

---

## 2. Entry pipeline

1. Each universe's regime leader (BTC / QQQ / XAU) sets that universe's
   direction: `long`, `short`, or `neutral` (`scoring.regime`). Three votes —
   day-change sign, price-vs-VWAP, taker flow (crypto only) — 2+ votes gates a
   direction at full size, exactly 1 at half size. **Since v0.9.32 the
   day-change vote has a ±0.3% dead-zone** (`regime_chg_deadzone_pct`): a
   leader inside that band casts no direction vote, so hair-trigger tape
   yields reduced-size/none regimes instead of full-size L↔s flips (the
   whipsaw fix; swept 9/9). The VWAP vote deliberately keeps its razor edge —
   softening it was swept and KILLED (net −6..−14pt, worse tail). The taker
   vote is live-only (the harness has never simulated it); `regime_taker_vote:
   "0"` is the documented card toggle for exact live/replay parity.
2. Pass 1 (cheap ticker scan): every non-leader symbol in the universe is
   scored; `qualified` = score ≥ `min_score` and not skipped.
3. **All universes' `qualified` candidates are pooled and sorted globally by
   score** (`build_decision`, `src/main_live.py:181-184`) — this is a
   board-wide ranking, not a per-universe one.
4. Only the top `enrich_top_n` (8) of that pooled list get pass-2 enrichment:
   real kline ATR, higher-TF trend alignment, funding-crowding check. Scores
   are overwritten; some get `skip=True` here (this is what produces
   `no.enrich0` when *every* enriched candidate ends up skipped).
5. **The single highest-scoring survivor of pass 2 becomes `best` — one
   decision per cycle, board-wide.** This is the same single-slot design
   validated by the (killed) multi-slot A/B.
6. Per-symbol duplicate guards (`execution.py:1280-1292`), checked only for
   the chosen `best.symbol`:
   - Already an **open position** on that symbol → `already_in_position`.
   - Already a **pending (resting limit) order** on that symbol → 
     `entry_already_pending`.
   - Neither guard can be triggered by a *different* symbol's state.
7. **`max_concurrent` (3) IS a hard-enforced gate**, not just advisory
   (`execution.py:1249-1251`: `if open_count >= max_concurrent: return
   {"placed": False, "reason": "max_concurrent_reached"}`).
8. `funding_universes` / `taker_universes` = `["crypto"]` only — funding-skip
   and taker-flow gating are crypto-only because the underlying data
   endpoints are crypto-native and mis-resolve on equities/metals leaders.
9. Macro event blackout: universes with `event_blackout: true` (equities
   only) suppress **new** entries within ±`event_blackout_hours` (2) of a
   high-importance US calendar event. Existing positions/limits are
   untouched. Fail-open if the calendar is unreadable.
10. **Earnings blackout (v0.9.30)**: the macro calendar doesn't know MSTR
    reports tonight. Universes with `earnings_blackout: true` (equities)
    withdraw a **symbol's own candidacy** around its report date (the report's
    UTC day ± `earnings_blackout_hours` (4) — day-granular because the
    calendar carries a date, not a timestamp). Stood-down symbols appear in
    `metrics.earnings_blackout_symbols`; the digest still shows their scores.
    Entries only; fail-open at every layer.
11. **Owned-symbol fall-through (v0.9.23)**: symbols with an open position or
    resting limit are withdrawn from pooled candidacy *before* step 5, so a
    blocked best falls through to the runner-up instead of wasting the cycle
    (live/replay parity — the harness always worked this way). Feed
    signature: `entry_already_pending` is rare; its appearance means the
    execution-level race backstop fired.
12. Score dimension weights (v0.9.33): the five pass-1 dims (momentum 25 /
    VWAP 20 / range 20 / order-book 20 / volume 15) are config keys
    (`score_w_*`) defaulting to the legacy split — built for **ablation**
    ("does this signal earn its weight?"), not tuning. Hard disqualifiers
    (walls, thin volume, no VWAP) are not weights and hold at any setting.
13. Swing structure + candle reads (v0.9.34): `features.swing_points` finds
    confirmed pivots (k=3 bar lag, no repainting) in the 1h bars already
    fetched; `SymbolFeatures` carries `swing_high/swing_low/structure_dir/
    candle_veto_*`. Four opt-in consumer gates exist but **all four were
    swept and killed** (§7) — the fields flow, nothing acts on them by
    default. They remain available as card-tunable ablation hooks.

---

## 3. Exit / risk-management stack

All in `src/execution.py`, `_best_effort_position_controls` +
`_trail_stop` + `_scale_out`. Runs every cycle against every currently owned
position, independent of what the entry pipeline decided that cycle.

### Time-stop — the one truly unconditional exit (per-mode since v0.9.22)
```
age_h >= cap_h  ->  close_position(), NO other condition
cap_h = 4h  if the position was opened as a PULLBACK   (pullback_time_stop_hours)
      = 12h if opened as a BREAKOUT, or mode unknown   (global time_stop_hours)
```
**There is no P&L check anywhere in this branch.** Green or red, the position
closes at its cap. (A `240h` outer bound exists only to reject a garbage/
unparseable open-time, not as a second threshold.) The only reason this can
silently *not* fire is if the open-time is unparseable (`ts_ok: False` in the
diag) — a genuinely blind time-stop, distinct from choosing to hold.

**Why per-mode (v0.9.22):** 21/35/42d replay on the live 28-symbol set —
pullbacks decay when held (win% 62→38 as the cap grows; they bounce fast or
they're dead — the July-5 8h MSTR grind was this class) while breakouts are
the runners (win% *rises* 70→80 with hold). **How the mode is known with no
local state:** the attached TP backstop width is the marker —
`pullback_tp2_pct: 22` vs the breakout-inherited `tp2_pct: 20` — read back
from the exchange's TP plan order by `execution._position_entry_mode`
(`tmode` in the position diag). An ambiguous/foreign width (legacy position,
manually edited TP) refuses to classify and keeps the 12h global cap.
Equities additionally run **breakout-only** (`pullback: false` — their
pullback class was net-negative at every hold cap in replay), and a
session-hours gate exists (`session_hours_utc`) but ships **unarmed**: it
*hurt* on top of pullback-off (off-session equity breakouts are profitable).

### Trailing stop — one-way ratchet off *current* price, not the high-water mark
Every cycle: `trail = current_price ∓ trail_atr_mult(2.0) × ATR(14, 1h)`.
It **only moves the stop if strictly more protective** than the live SL, and
only if the move exceeds 0.1% of current price (else logged as `tick`, a
no-op). It **never loosens**. Because it's recomputed from *current* price
each cycle (not a stored peak), a pullback does not "tighten" the stop — the
stop is simply static while price sits below whatever level last cleared the
tick threshold. A trail that hasn't moved in hours on a low-vol name with a
narrow trading range (well under `2×ATR`) is expected behavior, not a stall.

### Breakeven arm
Arms (`be_armed = True`) when move ≥ `breakeven_pct` (2.0%) **or**
unrealized PnL ≥ `breakeven_trigger_usdt` ($20) — whichever comes first.
Once armed, `breakeven_lock_pct` (1.5%) becomes an available floor (see
steplock below), applied only when inside the market (never an above-market
self-trigger).

### Step-lock ladder
`steplock: "2:1.5,4:3,6:4.5"` → at ≥2% move, floor the stop at entry+1.5%; at
≥4%, floor at entry+3%; at ≥6%, floor at entry+4.5%. Highest armed rung wins.
Tighten-only, inside-market guard, stateless (the exchange SL order **is**
the memory — a reversal keeps the best floor reached). When a lock (not the
raw ATR trail) sets the stop, the action is labeled `act.steplock`, not
`act.trail_stop` — the label tells you which mechanism actually moved it.

**None of breakeven/steplock has any dependency on the `hld.<sym>+N` display
digit.** They use the same full-precision `move_pct` the display digit is
rounded from, but the digit itself gates nothing.

### Scale-out
`scaleout_frac` (**0.35** since v0.9.24; was 0.5) of the position closes at
market once move ≥ `scaleout_trigger_pct` (3.5%); the remainder keeps riding
the trail/ladder. Runs *before* the trail each cycle so the trail then covers
the trimmed size. The 0.35 was validated **jointly** with the v0.9.24
loss-breaker recalibration (9/9 windows) — retune them together, not alone.

### Margin mode
`margin_mode: "isolated"` is the default **since v0.9.29** (operator
request — caps a gap-through-stop to the position's own margin instead of
whole-account equity; per-trade loss is bounded by the exchange SL +
`max_loss_usdt` in either mode). Opens route through
`place_order(margin_mode='isolated')`, **fail-closed**: a wrong hedge
mapping REJECTS the order rather than placing a wrong-direction trade —
so the failure signature of this path is *missed entries*, never bad
fills. Instant revert: the key is card-tunable; setting `"crossed"`
restores the pre-v0.9.29 proven wrapper path with no redeploy. History:
`crossed` was the intentional default through v0.9.28 because the SDK's
`open_*` wrappers cannot set `margin_mode` (v0.6.4 note) — older docs
claiming isolated were wrong, and every pre-v0.9.29 position was crossed.

---

## 4. Sizing / risk model (`src/risk.py`)

```
notional = (max_loss_usdt / sl_pct) × size_factor          # risk.py:117-119
margin   = notional / leverage                              # then capped by margin_budget
```

- **`size_factor` (1.0 full / 0.5 reduced / 0.0 blocked) is a *regime*
  multiplier carried from the universe's leader regime
  (`reg.size_factor`), NOT a function of the candidate's own ticker
  score.** A position size doubling between two entries at the *same*
  displayed conviction score is a regime-strength signal, not a
  conviction signal — read it as "the environment now supports full
  risk," independent of the traded symbol's own `q`-score.
- Stop-loss floor by symbol class (`_sl_min_fraction`, `risk.py:38-43`):
  BTC/ETH → `sl_min_btc_eth_pct` (1.5%) · SOL/BNB → `sl_min_sol_bnb_pct`
  (1.2%) · **everything else, including MSTR/TSLA/NVDA/XAG → the generic
  `sl_min_alt_pct` (2.5%)** — there is no dedicated equity/metals floor;
  they fall into the alt bucket by default.
- Breakout entries use a structure-based stop (24h high/low ± buffer,
  widened to at least `breakout_stop_atr_mult × ATR`), floored at the same
  `sl_min`. Pullback entries enter at `vwap ∓ atr_limit_mult × ATR`.

---

## 5. Current live parameter reference (v0.9.42)

| Parameter | Value | manifest.yaml |
|---|---|---|
| `leverage` | 10 | :71 |
| `margin_budget` | $100 | :72 |
| `max_loss_usdt` | $15/trade | :73 |
| `margin_mode` | **isolated** (v0.9.29; card-revertable to crossed) | margin block |
| `max_scan_symbols` | 28 | :85 |
| `min_score` | 70 | :86 |
| `max_vwap_ext_pct` | 5.0% (2.5% equities override) | :96, :313 |
| `vol_floor` / `vol_ceiling` | 0 / 0 — **gate OFF** | :108-109 |
| `tp1_pct` / `tp2_pct` | 5.0% / 20.0% | :111, :132 |
| `trail_atr_mult` | 2.0× ATR(14) | :133 |
| `breakeven_pct` / `_trigger_usdt` | 2.0% / $20 | :134, :229 |
| `breakeven_lock_pct` | 1.5% | :151 |
| `steplock` | 2:1.5, 4:3, 6:4.5 | :161 |
| `scaleout_frac` / `_trigger_pct` | **0.35** (v0.9.24) / 3.5% | scaleout block |
| `sl_min_btc_eth / sol_bnb / alt` | 1.5% / 1.2% / 2.5% | :177-179 |
| `allow_short` | true | :183 |
| `max_concurrent` | 3 (hard-enforced) | :184 |
| `max_correlated_alts` | 2 | :185 |
| `loss_breaker_frac` / window | **0.018** (v0.9.24, ≈ -$18/24h; was 0.08) / 24h | breaker block |
| `event_blackout_hours` | 2 (equities only) | :217-224 |
| `earnings_blackout_hours` | **4** (equities, per-symbol, v0.9.30) | earnings block |
| `regime_chg_deadzone_pct` | **0.3** (v0.9.32, swept 9/9; vwap dz KILLED) | regime block |
| `regime_taker_vote` | 1 (parity toggle; "0" = replay-validated 2-vote gate) | regime block |
| `score_w_*` (5 dims) | legacy 25/20/20/20/15 (ablation keys, v0.9.33) | scoring block |
| `swing_k` | 3 (pivot confirmation bars, v0.9.34) | v0.9.34 block |
| structure/candle gates (4) | **all "0" — swept and KILLED** (v0.9.34, see §7) | v0.9.34 block |
| `time_stop_hours` | **12** (unconditional; breakout + unknown-mode cap) | :225 |
| `pullback_time_stop_hours` | **4** (unconditional; v0.9.22 per-mode cap) | v0.9.22 block |
| `pullback_tp2_pct` | 22 (mode marker; replay-proven inert) | v0.9.22 block |
| equities `pullback` | **false** (breakout-only universe, v0.9.22) | equities block |
| `limit_expiry_hours` | 4 | :226 |
| `limit_chase_pct` | 3.0% | :227 |
| `size_scope_mult` | 1.5 | :228 |
| `atr_period` / `kline_interval` | 14 / 1h | :232, :231 |
| `enrich_top_n` | 8 | :237 |
| `funding_skip_bps` | 30bps (crypto-only) | :241, :250 |
| `breakout_trend_min` | 0.7 | :270 |
| `breakout_extreme_band` | 1.5% | :271 |

---

## 6. Universe reference

| Universe | Leader | Symbols | Breakout | Event blackout | Overrides |
|---|---|---|---|---|---|
| `crypto` | BTCUSDT | 28 (BTC...SHIB, see `trading_symbols`) | ✅ | — | — |
| `metals` | XAUUSDT | XAGUSDT only (Pt/Pd/Cu permanently thin-skipped, <$10M volume) | — | — | — |
| `equities` | QQQUSDT | TSLAUSDT, NVDAUSDT, MSTRUSDT | ✅ | ✅ ±2h | `max_vwap_ext_pct: 2.5` |

---

## 7. Validated / killed feature ledger

Don't re-litigate these — they were settled with replay data, not vibes.

| Feature | Outcome | Why |
|---|---|---|
| Multi-slot concurrency | **KILLED** | Fixed -19.1% maxDD everywhere; net worse in 4/6 windows. Single-slot "trade only best" is protective. |
| v0.9.19 breakout filters (`breakout_trend_min` 0.6→0.7, `max_vwap_ext_pct` 4.0→5.0) | **SHIPPED, kept** | OOS-verified on 21/35/42d windows; re-verified on the live 28-symbol universe: wins 12/12 cells vs baseline (more entries, higher net, shallower DD, higher PF). |
| v0.9.20/21 vol-regime gate (`vol_ceiling`) | **SHIPPED then disabled by default** | Validated on replay's 14-symbol default (capped DD, net up) — but on the live 28-symbol universe it **halves net every window** for ~6pt DD relief. The high-vol alts it refuses (PEPE/SHIB/LAB class) are the *winners* live, not the drawdown drivers. Classic cross-universe calibration trap: don't trust a validation run on a smaller/different symbol set than what's actually live. Mechanism retained, opt-in via `vol_ceiling > 0`. |
| `sl_min_alt` widening | **KILLED** | Overfit — failed the 21d out-of-sample window. |
| `trend_norm` retune | **Held back** | +16pt effect was implausibly large for the mechanism; not shipped pending further scrutiny. |
| `pullback_structure_stop` (v0.9.34) | **KILLED** | Pivot-anchored stops lose −2.7/−2.7/−2.5pt across 21/35/42d, PF down in all three. Tighter "real structure" stops tag out pullbacks the rolling 24h anchor survives — same lesson as the stop buffer, opposite direction: the 24h extreme is fine. |
| `breakout_structure_confirm` (v0.9.34) | **KILLED** | Inert at 21d, −11.8/−11.7pt at 35/42d. The trades it filters (rolling-extreme break before the last pivot breaks) were net winners — early is where the breakout money is. |
| `structure_trend_veto` (v0.9.34) | **KILLED decisively** | −11.6/−16.0/−17.9pt, PF 2.55→1.61 at 21d, DD worse. Counter-structure entry *is* the strategy — a pullback limit buys against falling 1h structure by construction. |
| equities universe (v0.9.37 OOS validation) | **DEFENDED** | QQQ-led breakout-only: +13.6/+21.5/+30.9, PF 2.08/2.20/2.73, maxDD ≤ −4.6 across 21/35/42d. Earns its slot. |
| metals universe (v0.9.37) | **No evidence — unvalidatable offline** | Zero replay entries in 42d: the harness's degraded order-book fallback caps XAG at ~68 (live prints 72q with a real book). Rides on live results only. |
| funding-as-signal (v0.9.37) | **Not built** | funding.py history: this universe never approaches the 30bps gate — no dynamic range, nothing to trade on. Adjudicated without code. |
| score-weighted sizing (v0.9.37 probe) | **KILLED** | Floor 0.7 at score 70 → full at 90: net −4.3/−18.5/−21.5pt for ~4pt DD relief. Third confirmation that score is a threshold, not a magnitude — the 70–75 floor-grazers carry the net. Do not size by score. |
| stochastic OB/OS entry filter (v0.9.43 probe) | **PROVEN HARM** (CI clear of zero, all windows) | net +25.7/+56.7/+78.4 → −5.8/−5.8/−10.5, PF 2.6 → 0.24; 90% CIs [−64,−2]/[−101,−27]/[−138,−43], P(better) ≤ 0.04. Vetoes ~99% of entries; the survivors catch falling knives. A mean-reversion veto strangles a trend-follower. (KER trend filter = the killed er_floor chop gate; vol-regime band = the killed vol-gate — the other two components of the same proposal were already dead.) |
| loss cooldown after stop-out (v0.9.42 probe) | **KILLED** | −5.1pt in every window. The "revenge trade" is the re-load, and it pays — the symbol that just stopped us out is often at the extreme the strategy fades again. Every filter on fills loses (4th confirmation). |
| feature timescale (v0.9.42 probe) | **24h DEFENDED** | 48h catastrophic (win% −15pts, PF halves); 12h untestable AND unshippable (the 24h window is the exchange ticker API's definition). The system's clock is optimal and structurally fixed. |
| half-size (1-vote) regime path (v0.9.42) | **Empty, not killed** | Zero 1-vote qualified candidates in any window — dead-zone era regimes are 2-vote or neutral. Machinery stays as fail-safe. |
| resting-limit condition management (v0.9.41 probes) | **All three KILLED** | Regime-flip cancel −11/−17/−17pt (the dip that fills and the regime flip are the same local extreme — cancelling surrenders the reversion); re-qualification decisive kill (a second min_score raise in disguise); VWAP re-price inert at 0.5 ATR and marginally worse at 0.2. **The exit trio (fill / 3% chase / 4h expiry) is complete** — expiry + fresh re-placement already re-anchors everything. |
| pullback market entry (v0.9.37 probe) | **KILLED decisively** | Fill-rate audit: ~85% of pullback limits never fill, misses 2:1 missed-winners. But taking the same signals at market guts the edge: per-trade +0.46%→+0.08%, win% 62→47, PF 2.55/3.44/2.90 → 1.46/1.96/1.78. The dip-to-fill requirement IS the filter — patience defended with numbers. |
| anchored VWAP, swing pivot (v0.9.35 probe) | **KILLED decisively** | +6.0/+47.5/+40.8 net vs +25.9/+84.7/+83.2, PF 1.13–1.61, maxDD −25.7. Pivot-anchored VWAP whipsaws on every confirmation and poisons all four VWAP consumers at once. Research-side probe only, never shipped. |
| anchored VWAP, UTC day (v0.9.35 probe) | **KILLED** (net rule) — noted as best defensive profile | −1.5/−9.8/−0.7pt net, PF down — but maxDD nearly halves (−15.4 → −7.9/−7.9/−9.1): the day anchor hugs price, doubling pullback fills at half the per-trade edge. First thing to re-examine if risk appetite ever drops. |
| `candle_veto` (v0.9.34) | **KILLED** (all-windows rule) | −7.4/−2.6/−7.3pt net. Noted, not armed: the breakout side *improves* in every window (win% 70/76/75 → 78/83/80; 35d PF 3.81) — counter-candles hurt continuation but are exactly the bar a pullback limit wants. A breakout-scoped variant projects flat at 21d, so it doesn't clear the bar. |

**Lesson driving both the vol-gate reversal and this doc existing:** replay's
*default* symbol set (14 crypto majors) is materially different from what's
actually live (28 crypto + separate equities/metals universes). Any
validation run must be re-checked against the live universe before being
trusted — a result that holds on 14 symbols is not guaranteed to hold on 28.
