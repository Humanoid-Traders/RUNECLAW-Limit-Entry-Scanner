# CLAUDE.md — session bootstrap for RUNECLAW

RUNECLAW is a **live-money** GetAgent/Bitget deterministic perp scanner
(crypto BTC-led 28 symbols; equities QQQ-led MSTR/TSLA/NVDA breakout-only;
metals XAU-led XAG) running follow_trade on the operator's GetClaw account.
There is no LLM in the decision path — every trade decision is deterministic
code in `src/`. Read `docs/LIVE_OPS_REFERENCE.md` before decoding any live
feed line; it is kept current and its §7 kill ledger is binding.

## Non-negotiable disciplines (learned the hard way — each rule has a scar)

1. **Validate on the LIVE 28-symbol set**, never replay's 14-symbol default —
   a replay-default validation once inverted a verdict (the vol-gate trap).
2. **OOS windows 21/35/42 days. Ship only net AND tail (PF/maxDD) winners in
   ALL windows.** A 2-of-3 winner is a kill. One interpolation probe is
   allowed per idea; no grids.
3. **Sweep combos jointly** before shipping stacked features (dead-zone ×
   loss-pause interaction shrank the summed edge by half).
4. **Record every verdict in `manifest.yaml` comments** next to the key it
   concerns, and in `docs/LIVE_OPS_REFERENCE.md` §7 — killed ideas stay dead.
   Do NOT re-litigate anything in the kill ledger without beating the recorded
   A/B on the same rule.
5. **Probe research-side first** (a `--set` knob in `research/replay_mp.py`),
   build `src/` code only for survivors. The anchored-VWAP and market-entry
   probes died without a line of engine code — that is the correct order.
6. **Score is a threshold, not a magnitude** (triple-confirmed). The 70–75
   band carries the profitable marginal trades; don't raise `min_score`,
   don't size by score.
7. **Uptime beats parameters.** Reconciliation (2026-07-07) showed the live
   week's underperformance was dark-board hours, not slippage or bad params.
   Avoid unnecessary flatten-for-deploy cutovers.

## Canonical replay baseline (joint live set)

```
python3 research/replay_mp.py --days {21|35|42} --breakout \
  --symbols BTCUSDT ETHUSDT SOLUSDT LABUSDT ZECUSDT XRPUSDT DOGEUSDT TAOUSDT \
  HYPEUSDT BNBUSDT SUIUSDT ADAUSDT LINKUSDT ENAUSDT ONDOUSDT BCHUSDT AVAXUSDT \
  NEARUSDT AAVEUSDT WLDUSDT XPLUSDT XLMUSDT TRUMPUSDT MUSDT INJUSDT SEIUSDT \
  PEPEUSDT SHIBUSDT \
  --exit-mode trail --trail 2.0 --time-stop 12 --be-lock 1.5 \
  --steplock 2:1.5,4:3,6:4.5 --scaleout 0.35 \
  --set tp2_pct=20,breakout_trend_min=0.7,max_vwap_ext_pct=5.0,pullback_time_stop_hours=4,pullback_tp2_pct=22,loss_pause_pct=3,regime_chg_deadzone_pct=0.3
```

- `--symbols` is space-separated (`nargs="*"`); a comma string silently
  becomes one bogus symbol. `--leader` is a flag, not a `--set` key.
- Research-only probe knobs already wired in replay_mp (all default-off):
  `vwap_anchor=day|swing`, `pullback_market_entry=1`, `score_size_floor=F`,
  plus missed-limit opportunity tracking in the report.
- Weekly maintenance: rerun the 3-window baseline and compare against the
  numbers recorded in manifest comments — drift beyond noise means the tape
  changed, not that killed ideas came back to life.
- New-listings watch: a daily CI job (`listings-watch.yml` →
  `research/listings_watch.py`) diffs Bitget's public USDT-M perp list against
  `research/listings_snapshot.json` and opens an issue on new names (classified
  + volume-ranked vs the $30M floor). Acting on it = extend the classifier
  allowlists / `discovery_watchlist`, then `listings_watch.py --update` in the
  same commit so the snapshot matches the code.
- Dead-man's switch: a 15-min CI job (`deadman.yml` → `research/deadman.py`,
  auth via the `GETAGENT_ACCESS_KEY` repo secret) opens an issue when the live
  instance is not `active` and auto-closes on recovery — rule 7's enforcement
  arm. No signal-history endpoint exists (403 wall); instance status is the
  observable ceiling.
- Order-book tape: a 30-min CI job (`book-tape.yml` → `research/book_snap.py`)
  records public merge-depth for XAG/XAU + thin equity perps to the
  **`book-tapes` branch** (reader: `research/book_tape.py`). Started 2026-07-13
  because historical books can't be bought — after ~3 weeks of tape, replay can
  consume real XAG books and metals stops being unvalidatable offline.
- Trade dumps carry `sl_pct0`/`fill_i` (since 2026-07-13): judge wide-stop
  probes in **R-multiples** (`ret_pct / (sl_pct0*100)`), not raw return-units —
  backward sizing keeps live dollar risk constant, so return-units overstate
  wide-stop drawdowns.
- **Sweep on frozen tapes**: add `--data-file tape.json` so every arm runs on
  the identical dataset (re-fetching between arms compares different tapes —
  the window-drift trap, hit twice on 2026-07-08). Add `--dump-trades f.json`
  per arm and run `python3 research/ab_ci.py a.json b.json` for a bootstrap
  CI on the delta. Calibration fact: a −5pt single-window delta on ~60 trades
  has a 90% CI of ±60pt — **single windows prove nothing; the all-windows
  rule is what carries the inference**. Small-delta kills mean "no evidence
  of benefit, don't ship complexity," not "proven harm"; only large
  consistent deltas (structure veto, requalify, fw48 class) are proven harms.

## Build/test/ship mechanics

- Tests are standalone scripts: `for f in tests/test_*.py; do python3 $f; done`.
  New tests go BEFORE the `if __name__` runner (a test appended after it
  silently never runs — happened once).
- `manifest.yaml` + `src/*.py` are hash-frozen: after any edit run
  `python3 scripts/refresh_hashes.py` (or manually re-freeze
  `audit/MANIFEST.sha256`) and confirm `python3 tests/test_doc_integrity.py`.
- Version bumps: `manifest.yaml:3` AND `src/main_live.py` `ANALYSIS_VERSION`.
- Deploy package = `manifest.yaml` + `README.md` + `src/**.py` ONLY (<10MB,
  `import time` is banned by platform lint; `datetime` is fine). Full deploy
  sequence: `docs/DEPLOY_RUNBOOK.md`.
- **Deploy only on the operator's explicit flatten+go** ("all is closed and
  disabled" = flatten done, ship the next version). The operator enables via
  their card; the engine's fills window is ACCOUNT-wide, so a redeploy does
  NOT reset the loss breaker (proven live 2026-07-07).
- Git: branch per session instructions, commit with the session trailer, push
  with retry, open a DRAFT PR via GitHub MCP; the operator merges fast.
  If the PR merged while commits were pending, rebase onto main and open a
  new PR — never stack on merged history (PR #67/#68 race).

## Live-feed decode (the operator pastes SITREPs — decoding them is a duty)

Use `python3 research/decode.py "<SCAN line>"` for any pasted line; it embeds
the misread guards. The recurring operator misread classes, all previously
corrected in-session — catch them proactively:

- **Breaker math**: threshold = `loss_breaker_frac × margin_budget × leverage`
  (frac × $1000 at defaults), NEVER frac × equity. When hand math and the
  `b`-token disagree, **the token wins** (`b24` = ~$24 headroom remaining).
- **`x` vs `q`**: `85x` is a DISQUALIFIED candidate (hard gate at enrichment),
  not "conviction blocked by a slot". Only `q` names can trade.
- **`nof-`** = non-follow (eval/pre-window) cycle marker — NOT "no fills".
- **`L`/`s` digest prefix** = regime gate direction, not a quality tier.
- **Flatten attribution**: closes at a version cutover are the operator's own
  flatten, not engine exits.
- **Position clocks**: pullback 4h / breakout+unknown 12h unconditional
  time-stops; 4h is also the limit-expiry — there is no "4H position gate".
- The TP plan order is the tp2 **backstop and mode marker** (pullback ×1.22 /
  breakout ×1.20 of entry); real exits come from trail/steplock/scale-out.
- Operator rulebook lines to respect in advice: Rule 3 $15 max/trade,
  Rule 10 −$30 warning, Rule 13 −$40 hard stop (account-day frame).

## Account/platform facts

- GetAgent strategy_id: `e977214c-86e5-405b-be0b-d5bad50b97c8`.
- Deploy API: `api.bitget.com/api/v1/playbook/{upload,confirm,publish}`,
  bare `ACCESS-KEY` header — the operator holds the key (Bitget/GetClaw-
  managed, non-rotatable; never commit it to the repo).
- Metals is UNVALIDATABLE offline (replay's degraded order-book fallback caps
  XAG ~4pt below its live score) — judge it on live results only.
