# RESEARCH_BACKLOG — probe ideas not yet run

Ideas parked for a future research cycle. Nothing here is validated or shipped —
each entry is a **hypothesis** that must clear the standard bar before any `src/`
code is built (frozen-tape 21/35/42d OOS, net AND tail winner in ALL windows,
`research/ab_ci.py` bootstrap CI, kill recorded if it fails). Probe research-side
first (a `--set` knob in `research/replay_mp.py`); build the engine only for
survivors. See `METHODOLOGY.md` and CLAUDE.md.

---

## 1. `momentum_confluence` entry gate (2026-07-10)

**Provenance.** External signpost, not our data: a backtest of the sister strategy
`/home/user/RUNECLAW` (a different engine, richer signal taxonomy). Its edge
attribution over 33 trades showed a `momentum_confluence` signal type at **PF 4.76**
(11 tr, +$112.96, win 73%) — the sharpest bucket, vs `regime_trend` PF 2.15 (the
workhorse). Same run: edge concentrated in `TREND_DOWN` + `with-trend` + `swing`;
`neutral`/`EXPANSION` entries were small net losers.

**Hypothesis for us.** A **precision entry filter**: qualify a candidate only when
multiple momentum dimensions *agree* — e.g. relative-strength-vs-leader AND
higher-TF trend (`trend_strength`) AND range-position all pointing the same way as
the intended side — instead of relying on the blended score alone. Rationale: a
confluence gate should raise per-trade PF by taking only stacked-momentum setups.

**How to probe (research-side, no engine build until it survives).**
- Add a default-off `--set momentum_confluence=N` knob in `replay_mp.py`: require
  ≥N of {rel-strength sign, `trend_strength` ≥ floor, range-position on-side}
  aligned with the side, applied in the qualification filter (same place the other
  entry probes hook). N∈{2,3}; one interpolation probe only, no grid.
- Run the canonical joint-live 28-symbol baseline at 21/35/42d on a **frozen tape**
  (`--data-file`), `--dump-trades` each arm, `research/ab_ci.py` for the delta CI.
- Ship only if it's a net AND tail (PF/maxDD) winner in **all three** windows.

**Priors / traps to respect (why this may well die).**
- The source is **underpowered**: 33 trades, single window, no OOS, and **PENGU
  +$176.61 was ~45% of the trend-down profit** — one lucky symbol. By our bar this
  proves nothing; it's a hypothesis generator only.
- A confluence gate is a **precision filter → fewer trades**. That collides head-on
  with our triple-confirmed finding that *the marginal 70–75 trades carry the net*
  (min_score-75 catastrophe, score-weighted-sizing kill, `limit_requalify` kill).
  Any filter that thins the fill pool has lost this way repeatedly. Watch for it.
- We already encode much of this: the regime gate + `breakout_trend_min 0.7` +
  the score blend + dead-zones. The confluence gate must beat that *stack*, not a
  naive baseline.

**Status:** UNPROBED. Idea logged; do not port the sister backtest's conclusion —
re-derive on our tapes or not at all.

**Bonus corroboration (already ours, no action needed).** The same run
independently validated our design direction: `with-trend` (+$390) crushed
`neutral` (−$3.71), and the edge was regime-specific — exactly the thesis behind
our regime gate and the dead-zone kills (withdraw votes in the noise band). Nice
external agreement that the neutral/non-trending stand-aside is correct.
