"""Bitget funding-rate history probe (research-only, zero-dependency).

Why this exists: the live funding gate (src/scoring.py:291-299) skips a candidate
when its adverse per-interval funding exceeds `funding_skip_bps` (30 bps) and soft-
penalizes milder adverse funding. Across live operation we repeatedly hypothesized
that funding crowding was behind the `no.enrich0` stand-downs -- but we had only the
live instantaneous reading, never the history, so we could never check how often
funding is anywhere NEAR the 30 bps gate. This pulls the public funding-rate history
for the live symbols and reports it in the SAME units the gate uses (decimal * 10000
= bps per interval), so the "is funding actually the cause?" question is answerable
from data instead of guessed.

Deliberately stdlib-only (urllib), matching research/replay.py's v0.9.4 zero-dep
fetch -- NOT ccxt: this is a single-exchange (Bitget) single-purpose fetch, so ccxt's
one real value (cross-exchange abstraction) buys nothing and would re-add a dependency
the harness intentionally removed. Local research tooling only; never shipped in the
getagent package (src/ + manifest + README only).

Run:
  python3 research/funding.py                 # the 28 live crypto symbols, last 30 intervals
  python3 research/funding.py --limit 100      # deeper history
  python3 research/funding.py --symbols BTCUSDT ETHUSDT PEPEUSDT
  python3 research/funding.py --skip-bps 30    # override the gate threshold to compare
"""
import argparse
import json
import urllib.parse
import urllib.request

# Public Bitget v2 USDT-perp funding-rate history. Same host/no-auth surface as
# research/replay.py's candles fetch; funding is a public market endpoint.
FUND_HIST = "https://api.bitget.com/api/v2/mix/market/history-fund-rate"

# The live crypto universe (manifest.yaml strategy_config.trading_symbols, v0.9.21).
LIVE_CRYPTO = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "LABUSDT", "ZECUSDT", "XRPUSDT", "DOGEUSDT",
    "TAOUSDT", "HYPEUSDT", "BNBUSDT", "SUIUSDT", "ADAUSDT", "LINKUSDT", "ENAUSDT",
    "ONDOUSDT", "BCHUSDT", "AVAXUSDT", "NEARUSDT", "AAVEUSDT", "WLDUSDT", "XPLUSDT",
    "XLMUSDT", "TRUMPUSDT", "MUSDT", "INJUSDT", "SEIUSDT", "PEPEUSDT", "SHIBUSDT",
]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_funding_history(symbol, limit=30, product_type="usdt-futures"):
    """Return [(funding_time_ms:int, rate_decimal:float), ...], newest first.

    Fail-soft: any network/parse error yields [] so one bad symbol never aborts the
    sweep (same contract as replay.fetch_klines). `rate_decimal` is the raw per-
    interval funding rate; multiply by 10000 for the bps the live gate compares.
    """
    q = urllib.parse.urlencode({
        "symbol": symbol, "productType": product_type,
        "pageSize": str(min(max(limit, 1), 100)),
    })
    try:
        with urllib.request.urlopen(FUND_HIST + "?" + q, timeout=30) as resp:
            doc = json.load(resp)
    except Exception as exc:  # noqa: BLE001 -- research probe, fail-soft per symbol
        print("  ! %-11s fetch error: %s" % (symbol, repr(exc)[:60]))
        return []
    if str(doc.get("code")) != "00000":
        print("  ! %-11s api code %s: %s" % (symbol, doc.get("code"), str(doc.get("msg"))[:40]))
        return []
    out = []
    for row in (doc.get("data") or []):
        t = _f(row.get("fundingTime"))
        r = _f(row.get("fundingRate"))
        if t is not None and r is not None:
            out.append((int(t), r))
    return out


def summarize(symbol, hist, skip_bps):
    """Reduce a symbol's history to the gate-relevant stats, in bps/interval."""
    if not hist:
        return None
    bps = [r * 10000.0 for _, r in hist]                 # live: funding_now * 10000
    latest = bps[0]
    mean = sum(bps) / len(bps)
    # The gate skips on ADVERSE funding of either sign (positive hurts longs, negative
    # hurts shorts), so |bps| is the crowding magnitude the 30-bps gate actually sees.
    worst = max(bps, key=abs)
    near = sum(1 for b in bps if abs(b) >= 0.5 * skip_bps)   # within half the gate
    over = sum(1 for b in bps if abs(b) >= skip_bps)         # would trip the gate
    return {"latest": latest, "mean": mean, "worst": worst,
            "near": near, "over": over, "n": len(bps)}


def main():
    ap = argparse.ArgumentParser(description="Bitget funding-rate history probe (research).")
    ap.add_argument("--symbols", nargs="*", default=LIVE_CRYPTO,
                    help="symbols to probe (default: the 28 live crypto names)")
    ap.add_argument("--limit", type=int, default=30, help="funding intervals back (max 100)")
    ap.add_argument("--skip-bps", type=float, default=30.0,
                    help="the live funding_skip_bps gate to compare against (default 30)")
    a = ap.parse_args()

    print("Bitget funding-rate history -- %d symbols x last %d intervals" % (len(a.symbols), a.limit))
    print("gate: funding_skip_bps = %.0f bps/interval (adverse). bps = rate * 10000.\n" % a.skip_bps)
    print("  %-11s %9s %9s %9s %6s %6s" % ("symbol", "latest", "mean", "worst|", "near", "OVER"))
    print("  " + "-" * 56)

    rows = []
    for sym in a.symbols:
        s = summarize(sym, fetch_funding_history(sym, a.limit), a.skip_bps)
        if s is None:
            continue
        rows.append((sym, s))
        print("  %-11s %8.2f  %8.2f  %8.2f  %5d  %5d"
              % (sym, s["latest"], s["mean"], s["worst"], s["near"], s["over"]))

    if not rows:
        print("\nno funding data returned.")
        return
    total_int = sum(s["n"] for _, s in rows)
    total_near = sum(s["near"] for _, s in rows)
    total_over = sum(s["over"] for _, s in rows)
    peak_sym, peak = max(rows, key=lambda kv: abs(kv[1]["worst"]))
    print("\n  %d symbol-intervals sampled." % total_int)
    print("  within half the %.0f-bps gate (>=%.0f): %d (%.1f%%)"
          % (a.skip_bps, 0.5 * a.skip_bps, total_near, 100.0 * total_near / total_int))
    print("  would TRIP the %.0f-bps skip:            %d (%.2f%%)"
          % (a.skip_bps, total_over, 100.0 * total_over / total_int))
    print("  peak adverse funding seen: %.2f bps on %s" % (peak["worst"], peak_sym))
    if total_over == 0:
        print("\n  => funding never reached the skip gate in this window: the funding")
        print("     hard-skip is NOT what produced the enrich0 stand-downs here. Look")
        print("     to trend-misalignment / VWAP-extension demotions instead.")


if __name__ == "__main__":
    main()
