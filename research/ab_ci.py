#!/usr/bin/env python3
"""Bootstrap significance for replay A/Bs (research-only, zero-dependency).

The kill-ledger rule adjudicates on point estimates ("net AND tail winner in
ALL windows") and the phrase "beyond noise" has always been a judgment call.
This turns it into a number: given two --dump-trades JSON files from
replay_mp runs ON THE SAME FROZEN DATASET (--data-file -- comparing runs on
different fetches re-introduces window drift), it bootstraps the trade lists
(resample with replacement, default 10k iterations) and reports the
confidence interval of the NET and PF deltas plus the fraction of resamples
where B beats A.

Usage:
    python3 research/replay_mp.py ... --data-file tape.json --dump-trades a.json
    python3 research/replay_mp.py ... --data-file tape.json --set X=1 --dump-trades b.json
    python3 research/ab_ci.py a.json b.json --label-a baseline --label-b X=1

Reading the output:
  - If the 90% CI of the net delta includes 0, the arms are NOT distinguishable
    on this window -- a "kill" or "win" from point estimates alone is noise.
  - P(B>A) near 0.5 = coin flip; near 0 or 1 = a real ordering.
Honesty caveat: trades within a window are not fully independent (shared
regime days, slot competition), so the bootstrap is anti-conservative --
treat "not significant" as decisive, "significant" as merely supportive.
"""
import argparse
import json
import random


def _net(trades):
    return sum(t["ret_pct"] for t in trades)


def _pf(trades):
    wins = sum(t["ret_pct"] for t in trades if t["ret_pct"] > 0)
    losses = -sum(t["ret_pct"] for t in trades if t["ret_pct"] < 0)
    return (wins / losses) if losses > 0 else float("inf")


def _boot(trades, iters, rng):
    n = len(trades)
    outs = []
    for _ in range(iters):
        sample = [trades[rng.randrange(n)] for _ in range(n)]
        outs.append((_net(sample), _pf(sample)))
    return outs


def _ci(vals, lo=5.0, hi=95.0):
    v = sorted(vals)
    n = len(v)
    return v[int(n * lo / 100)], v[min(int(n * hi / 100), n - 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a_trades")
    ap.add_argument("b_trades")
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    ap.add_argument("--iters", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=1337,
                    help="fixed seed: the CI itself is reproducible")
    args = ap.parse_args()

    a = json.load(open(args.a_trades))
    b = json.load(open(args.b_trades))
    if not a or not b:
        print("empty trade list -- nothing to compare")
        return
    rng = random.Random(args.seed)
    ba = _boot(a, args.iters, rng)
    bb = _boot(b, args.iters, rng)
    d_net = [y[0] - x[0] for x, y in zip(ba, bb)]
    d_pf = [y[1] - x[1] for x, y in zip(ba, bb)
            if x[1] != float("inf") and y[1] != float("inf")]

    print(f"=== {args.label_a}: n={len(a)}  net={_net(a):+.1f}  PF={_pf(a):.2f}")
    print(f"=== {args.label_b}: n={len(b)}  net={_net(b):+.1f}  PF={_pf(b):.2f}")
    lo, hi = _ci(d_net)
    p_b = sum(1 for d in d_net if d > 0) / len(d_net)
    sig = "SIGNIFICANT" if (lo > 0 or hi < 0) else "NOT distinguishable from noise"
    print(f"\nnet delta ({args.label_b} - {args.label_a}): "
          f"{_net(b) - _net(a):+.1f}   90% CI [{lo:+.1f}, {hi:+.1f}]   -> {sig}")
    if d_pf:
        plo, phi = _ci(d_pf)
        print(f"PF  delta: {_pf(b) - _pf(a):+.2f}   90% CI [{plo:+.2f}, {phi:+.2f}]")
    print(f"P({args.label_b} > {args.label_a}) = {p_b:.3f}"
          f"   ({args.iters} bootstrap resamples, seed {args.seed})")
    print("\nCaveat: within-window trades share regime days/slots -> bootstrap is"
          "\nanti-conservative. Treat 'not significant' as decisive; 'significant'"
          "\nas supportive, still requiring the all-windows rule.")


if __name__ == "__main__":
    main()
