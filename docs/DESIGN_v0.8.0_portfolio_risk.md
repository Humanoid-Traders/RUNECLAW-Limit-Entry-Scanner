# RUNECLAW v0.8.0 — Portfolio-risk value-tests (the left-tail layer)

**Status:** research-only (`research/replay_mp.py` + `tests/test_portfolio_risk.py`).
No `src/**` change, no redeploy, no live risk. This is the validation-loop applied
to the **portfolio layer** — the one part of RUNECLAW that had never been measured.
The 40-category audit flagged it as the weak flank: correlated-position cascade
risk (cat 34) and a drawdown control (cat 33) that turns out to be **dead**.

The point of this round, like the v0.7.0 loop, is to let data decide *what NOT to
ship*. It killed two plausible features and validated one — and the one it
validated also repairs an existing safety control that is silently non-functional.

## 1. What was tested

All three candidates were A/B'd in the 3-slot multi-position harness (the correct
one for concurrency/exit questions), at the live config (breakout on, trail
2×ATR, time-stop 12h, 0.04% per-side fee), across **three windows (20d / 28d /
35d)**. Crucially the harness now reports the **tail** — max drawdown of the
exit-ordered cumulative-return curve, worst single trade, and profit factor —
because a risk control is judged on the **left tail, not the mean**. Judging it on
expectancy alone is the exact honesty trap the loop exists to avoid.

| candidate | what it does | verdict |
|---|---|---|
| **(A) Correlation-weighted exposure budget** | replace the legacy count cap with Σ\|corr\| same-side exposure; tighten automatically when correlations spike | **REJECTED** |
| **(B) Concurrent aggregate-heat breaker** | pause new entries when the open book's combined *unrealized* % is below a threshold | **REJECTED (never fires at live concurrency)** |
| **(C) Realized rolling-loss breaker** | pause new entries when *realized* % over the trailing 24h is below a threshold | **VALIDATED @ 8% / 24-bar** |

## 2. (A) Correlation cap — REJECTED; the legacy count cap already dominates

The live Rule-7 cap is crude: treat every alt as BTC-correlated, count-cap at 2,
tighten to **1** whenever BTC/ETH is held. The hypothesis was that *measured*
correlation would beat it — let in genuine diversifiers, auto-tighten in a crash
when correlations →1. It does not.

| 20d (live config) | trades | net | **maxDD** | PF |
|---|---|---|---|---|
| **LEGACY count cap** | 43 | +32.3% | **−9.3%** | **1.91** |
| corr-budget 0.5 | 53 | +37.2% | −12.9% | 1.73 |
| corr-budget 1.0 | 68 | +54.0% | −16.3% | 1.87 |
| corr-budget 2.0 | 76 | +53.6% | −16.3% | 1.80 |

Every weighted budget is **looser** on the tail than legacy. The reason is
structural and worth keeping: legacy's blunt "BTC/ETH held → only 1 slot" rule
**is** a strong correlation prior — BTC/ETH *are* the market beta — and it is
tighter than any measured budget. The "smarter" model only relaxed the protection
that was already working, buying more trades and net return at the cost of the
tail and PF. **Keep the legacy cap. Do not ship measured correlation.**

## 3. (B) Concurrent-heat breaker — REJECTED; the tail is sequential, not concurrent

Pausing entries when the *open book's combined unrealized* is underwater sounds
like the cascade fix. But at the live tight cap the book holds ~1–2 concurrent
positions and the trail cuts losers fast, so the aggregate concurrent heat **never
reached even −6%** in any window (`heat-block: 0` at every threshold). The −29%
multi-day drawdown is **not** three positions underwater at one instant — it is a
*string of losing trades over days*. A concurrent-heat instrument cannot see that.

## 4. (C) Realized rolling-loss breaker — VALIDATED, robust across windows

Pause new entries when the **sum of realized returns from trades closed in the
trailing 24 bars** is ≤ −8%. This targets sequential bleed: after a fresh losing
streak, stand aside until the streak clears, instead of feeding a bad regime.

| window | NO breaker (net / maxDD / PF) | **loss-pause 8% / 24b** |
|---|---|---|
| **20d** (healthy) | +32.3% / −9.3% / 1.91 | **+32.3% / −9.3% / 1.91** (byte-identical — never fires) |
| **28d** (weak tail) | +15.0% / −29.4% / 1.21 | **+21.5% / −22.9% / 1.32** |
| **35d** (weak tail) | +22.8% / −29.4% / 1.24 | **+29.3% / −22.9% / 1.33** |

This is exactly how a circuit breaker should behave: **invisible when the system
is healthy, decisive when it isn't.** At 8%/24b it cuts the multi-day tail ~22%
(−29.4 → −22.9) in *both* weak windows *while raising* net return and profit
factor, and is identical to no-breaker in the healthy window. Tuning bands:

- **4% is too tight** — over-brakes, gutts return everywhere (35d +8.1%, 20d +18.1%).
- **10% fires too late** — barely helps the tail, costs return (35d +16.6%).
- **6–8% is the sweet spot;** 8% is the most surgical (35d: blocked only 8 entries).

## 5. The bonus: this repairs a DEAD control

`execution.manage_open_state` already has an equity circuit breaker
(`circuit_pause_usdt` / `circuit_stop_usdt`) that pauses/flattens on day P&L. But
it reads `day_start_equity` from `.state/`, and this session established `.state/`
is **ephemeral** in the live runtime (`state_runs` stuck at 1 — see the code
comment at `execution.py:384`). So the live drawdown control **never trips** — the
account has been running without the brake everyone assumes is on. The realized
rolling-loss breaker is the **stateless** replacement: it needs no local
persistence, only the trailing realized P&L, which the **exchange** persists.

## 6. The live port — IMPLEMENTED (stateless, fills-sourced, opt-in)

The validated decision is now wired into `src/execution.py`. The stateless data
source question is resolved: **`trade.contract.fills()`** returns recent contract
fills carrying realized `profit` and a `cTime` timestamp, both **exchange-persisted**
— so the trailing-window realized P&L needs no `.state/` (the reason the equity
breaker is dead).

- **`_trailing_realized_pnl(window_hours)`** — calls `trade.contract.fills(limit=100)`,
  sums `profit` over fills whose timestamp is inside the window. Key reads are
  snake/camel tolerant (`_REALIZED_PNL_KEYS`, `_OPEN_TIME_KEYS`) — the v0.6.7 lesson.
  **Fail-open:** an unreadable/empty fills response returns `None` ⇒ no pause, so a
  read glitch never blocks trading. This brake only ever *adds* caution.
- **`manage_open_state`** computes it when `loss_breaker_frac > 0`, sets
  `status["loss_breaker"]` + `realized_window_pnl`, and emits a `loss_breaker_pause`
  action (visible in the DBG tail). Threshold = `frac × margin_budget × leverage`
  (one slot's notional), so `frac = 0.08` ⇒ −$80 at the live 100×10 config — the
  faithful translation of the backtest's −8%-summed-return setting.
- **`open_if_allowed`** returns `{placed: False, reason: "loss_breaker"}` while it is
  active. **Pause only** — it never flattens; open positions keep their exchange SL/TP.
- **Opt-in, default OFF** (`loss_breaker_frac: "0"`), mirroring the v0.6.4 margin_mode
  precedent: the proven default is a no-op, and the validated value (0.08 / 24h) is
  one toggle away in `user_config_schema`. Coverage: `tests/test_loss_breaker.py`
  (10 tests — sum, window cutoff, snake-case, fail-open, trip/no-trip, the pause).

**Deploy note:** the package change is inert until the operator sets
`loss_breaker_frac = 0.08` on the instance. Recommended rollout: deploy v0.8.0, then
enable the breaker (it is harmless in healthy regimes by construction — it never
fires unless a real 24h realized-loss streak crosses the threshold).

## 7. Honest limits

Same as `replay_mp.py`: bar-touch fills, no intrabar path/partials, 1h-recon trend
vs live 4h, approximate fees. The tail numbers are **ranking signal, not a P&L
promise** — but the ranking is unambiguous and consistent across three windows:
realized-loss breaker in, correlation cap out, concurrent-heat out. As before, the
loop's value is that it told us what not to build before we shipped it live.
