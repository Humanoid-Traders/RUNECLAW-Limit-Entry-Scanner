# RUNECLAW v0.7.0 — The validation loop (Phase 1 of the roadmap)

**Status:** implemented in `research/` (offline tooling — NOT in the uploaded package, no
redeploy, no live risk). This is Phase 1 of a 4-phase roadmap:
1. **Validation loop** (this) — backtest + trade analytics + MAE/MFE + journal.
2. Fill-rate fix (entry execution) — A/B'd here before it ships.
3. Signal enrichment — scored and validated here.
4. Live trade-journal emission — reconstruct live MAE/MFE, compare live-vs-backtest.

## 1. Why this is first

The session shipped v0.6.1→v0.6.7 and the flagship feature — the trailing stop — was
**inert the entire time** (positions were never owned; only found via forensics). We were
adding features faster than we could validate them. This loop closes that gap: it turns
"deploy and hope" into "measure, then deploy."

## 2. What it adds

- `research/replay.py`: each simulated trade now records its full lifecycle —
  `entry_px`, `exit_px`, **`mae_pct` / `mfe_pct`** (max adverse / favorable excursion),
  `bars`, `regime`, `score` — in addition to `ret_pct`/`mode`/`reason`.
- `research/analytics.py`: aggregates the trades into the metrics that drive iteration —
  expectancy, profit factor, win rate, avg win/loss, **MAE/MFE distributions**, a
  **capture ratio** (how much of the favorable move winners keep), per-mode / per-reason /
  per-symbol breakdowns, and a one-line **edge verdict + the lever to pull next**. `--ab`
  runs fixed-vs-trail head-to-head; `--journal` dumps a per-trade JSON.
- `tests/test_analytics.py`: 5 network-free tests pinning the metric math (incl. the
  loser-MFE "trail opportunity" signal).

The point of MAE/MFE: **losers' average MFE** quantifies the trailing-stop / breakeven
opportunity as a NUMBER (how much winners-turned-losers gave back) — the exact thing we
could never get from the months-long live trail wait.

## 3. First findings (20d, breakout on, 0.06% round-turn fee, single-position replay)

| metric | FIXED exit (current) | TRAIL exit (v0.6.3) |
|---|---|---|
| trades | 35 | 33 |
| win rate | 54.3% | 51.5% |
| **expectancy / trade** | **+0.688%** | +0.516% |
| total return | **+24.07%** | +17.04% |
| profit factor | 1.68 | **1.74** |
| avg win / avg loss | +3.13% / −2.21% | +2.36% / **−1.44%** |

**Reads:**
- **There is edge.** Both exits are net-of-fee positive (PF 1.68/1.74, expectancy > 0). First
  time we've measured it rather than guessed.
- **The trail does NOT beat fixed on expectancy in this window** (+0.52% vs +0.69%; +17% vs
  +24% total). It cuts losers (avg loss −1.44% vs −2.21%, higher PF) but **caps winners** —
  fixed `tp1` exits are +3.70%/trade at 100% win, while the trail's big winners only resolve
  at `time_stop`. The trail's capture ratio is 0.46 (gives back from the peak). So the
  feature we spent the whole session trying to test live is, in this sample, **a wash-to-
  slightly-negative on expectancy** — exactly the call the loop exists to make.
  *Caveat:* one 20d window, single-position, approximate fills. The 3-slot `replay_mp.py`
  favored the trail in some windows — so the honest verdict is **regime-dependent; needs
  multi-window confirmation**, which is now a one-command sweep instead of a live wait.
- **Breakout > pullback** (expectancy +0.90% vs +0.40%) — the breakout leg is the stronger
  edge; lean into it.
- **The fill problem is real and quantified: ~43% chase-cancel** (80/188 signals never
  filled). That's Phase 2's target, and the single biggest reason we have so little live data.

## 3a. CORRECTION — multi-position reverses the trail verdict; fill-rate fix debunked

Two follow-up runs corrected the §3 single-position reads:

**Trail (use `replay_mp.py` — single-position is the WRONG harness for an exit question):**
live runs **3 concurrent slots**, so the realistic A/B is multi-position:

| window (3-slot, breakout on) | FIXED | TRAIL (2xATR, ts12, tp2 15%) |
|---|---|---|
| 20d | +19.2% (+0.43/trade) | **+26.9% (+0.63/trade)** |
| 35d | +13.3% (+0.16/trade) | **+36.7% (+0.48/trade)** |

**Under live's 3-slot concurrency the trail BEATS fixed in both windows (35d ~3x).** A
slot-starved single position banks small fixed-TP wins fast; with slots to spare the trail
lets winners run while other slots keep working, and the wide tp2 captures moves a fixed 5%
tp1 caps. This vindicates the original v0.6.3 design ("the single-position view is a
concurrency artifact") and **reverses §3**. Verdict: **keep the trail — it is the right exit
at the live config**, and the live trail diagnostic (once a fill runs) is worth capturing.
Lesson: use `replay_mp.py`, not `replay.py`, for any exit/concurrency decision.

**Fill-rate fix — debunked.** Sweeping `atr_limit_mult` (limit depth) showed tightening the
limit to fill more REDUCES expectancy (0.1 -> +0.42 vs 0.5 -> +0.69, single-position 20d).
The ~43% chase-cancel is mostly the strategy correctly skipping entries where price ran away;
forcing them in adds worse trades. So **Phase 2 (fill-rate) is NOT built** — the data says
it's net-negative. (`atr_limit_mult` ~0.5 is the sweet spot, marginally > live's 0.3, but
window-dependent -> not a confident live change.)

## 4. How this reshapes the roadmap

- **Phase 2 (fill-rate): cancelled.** The data shows forcing fills lowers edge. The chase-cancel
  rate is the strategy working, not a bug to fix.
- **The trail: validated, keep it.** Confirmed under live's 3-slot concurrency. Not faith now —
  a number (multi-position A/B above).
- **Edge confirmed + robust** across single- and multi-position, multiple windows, net of fees.
- Remaining edge-adder is **Phase 3 (signal enrichment)** — speculative, validate in the harness
  (with `replay_mp` for anything exit/concurrency-touching) before shipping. No more shipping
  unvalidated features.

## 5. Honest limits (unchanged from replay.py)

No order-book dimension (degraded fallback), bar-touch fills (no intrabar path / partials),
single-position (use `replay_mp.py` for concurrency), 1h-reconstructed trend vs live 4h
(under-fires breakouts). This is a **ranking/decision tool, not a P&L promise** — but the
rankings (fixed vs trail, breakout vs pullback, the chase-cancel rate) are the signal.
