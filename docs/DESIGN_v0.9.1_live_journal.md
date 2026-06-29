# RUNECLAW v0.9.1 — Phase 4: the live trade journal (live-vs-backtest loop)

**Status:** live (src) + research reducer. The audit's biggest *process* gap was #30:
every performance number is from the approximate backtest, and nothing surfaces
**live edge decay**. This closes that loop — the playbook now emits live closed-trade
results, and a reducer turns them into metrics directly comparable with `replay_mp`.

It is the prerequisite the v0.9.0 audit named: an honest per-symbol audit and any
future regime-*risk* test both need real live data, not a thin backtest window.

## 1. Why this was the right next build (and the others weren't)

Across v0.8–v0.9 the loop rejected every *entry-side* idea (correlation cap,
concurrent-heat, chop gate) and validated one *risk-side* control (the loss breaker).
The journal is neither an entry nor a risk gate — it's **measurement**, the thing that
lets every future change be judged on live evidence. With no live journal we were
back to "deploy and hope" for anything the backtest can't model. So before adding any
more logic, we add the instrument that tells us whether the logic is working live.

## 2. How it works (stateless, one fills read)

- **Source:** `trade.contract.fills` — realized `profit` + `cTime`, exchange-persisted,
  so it needs no `.state/` (the same read the loss breaker uses). `_read_fills()` is
  shared, so the journal adds **zero extra API cost when the breaker is on**.
- **Emit:** each cycle, `_fills_journal(rows, window)` builds compact records
  `{id, sym, side, profit, ts}` (most-recent-first, capped at 50) and
  `manage_open_state` puts them in `status["fills_journal"]`; `main_live` emits them
  in the DBG metrics. Read-only, fail-open (a bad fills read → no journal, never
  blocks trading). Default **on** (`journal_enabled: "true"`).
- **Reduce:** snapshots OVERLAP (a fill recurs every cycle until it ages out of the
  window), so `research/live_journal.py` **dedups by fill id** first, then computes
  realized total, win rate, profit factor, avg win/loss, and by-symbol / by-side.
  Point it at a dump of the DBG metrics and compare to `research/analytics.py`.

## 3. The honest limit: realized PnL only, no live MAE/MFE

The backtest journal records MAE/MFE (max adverse/favorable excursion). The live
journal **cannot** — excursions need an intra-trade high-water track, and the runtime
is stateless (a fresh process every 15 min, `.state/` ephemeral). Fills give the
*outcome* (realized PnL, side, symbol, time), not the *path*. So:

- **Live ↔ backtest comparison is on realized metrics** — expectancy, PF, win rate,
  by-symbol — which is exactly what edge-decay detection needs.
- **MAE/MFE stays a backtest-only metric.** Documented, not silent. Reconstructing it
  live would require a stateful high-water store the platform doesn't persist.

## 4. What it unblocks

1. **Live edge-decay detection** — live PF/expectancy vs the backtest's 1.2–1.9.
2. **Honest per-symbol audit** — the v0.9.0 audit flagged the backtest sample as too
   thin to prune; live `by_symbol` accrues real fills to settle it.
3. **Any future regime-risk test** — leverage/risk-% coupling can finally be judged on
   live outcomes, not assumed.

## 5. Tests

- `tests/test_live_journal.py` (6) — journal build (fields, window, cap, sort,
  snake-case) + the `manage_open_state` wiring (emitted by default; off when disabled,
  and then fills is never read).
- `tests/test_journal_reduce.py` (5) — the reducer: dedup of overlapping snapshots,
  realized aggregation, by-symbol/side, missing-profit handling, empty.

Suite **59/59 green**. The journal is read-only and fail-open: no trade-path change,
no live risk — it only ever *observes*.
