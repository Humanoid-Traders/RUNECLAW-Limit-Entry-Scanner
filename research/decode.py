#!/usr/bin/env python3
"""SCAN-line decoder (research-only, zero-dependency).

Decodes the compact live feed line the SITREP tool surfaces -- the exact
grammar of src/main_live.py's _scan_digest + _fold_exec_onto_scan + the
_breaker_token -- and prints a human-readable read with the KNOWN MISREAD
GUARDS built in (every guard below was a real operator misread, corrected
live, and is documented in CLAUDE.md / docs/LIVE_OPS_REFERENCE.md).

Usage:
  python3 research/decode.py "SCAN-cry:LM85x-met:sXAG64x-equ:sNVDA80x|o2p1-b24-act.trail_stop"
  python3 research/decode.py "cry:LSOL74q-met:sXAG70q-equ:sMSTR82q|o2p2-correlation_budget"
  python3 research/decode.py --window " -13.078 -13.371 " "…|o0p0-b4-no.lowscore"
      # --window: known realized fills still inside the 24h breaker window,
      # space-separated USD (negative = loss), to cross-check the b-token.

The decoder never guesses silently: anything it can't parse is printed
verbatim under 'unparsed' so a grammar drift is visible, not swallowed.
"""
import argparse
import re
import sys

# defaults mirror manifest.yaml; override via flags if the card was tuned
DEF_FRAC = 0.018
DEF_BUDGET = 100.0
DEF_LEV = 10

TAILS = {
    "bl": "state_blind -- the book (own/pending) couldn't be read; engine refuses NEW entries",
    "act": "a management action fired THIS cycle",
    "xpd": "a stuck owned-pending order the bot can't time-expire",
    "hld": "a position is held and quiet (its live state follows)",
    "no": "watch stand-down -- WHY the cycle placed nothing",
    "perr": "the pending-order fetch genuinely FAILED",
    "sig": "an actionable decision -- names the intended trade",
}
NO_REASONS = {
    "neutral": "all universe regimes neutral",
    "lowscore": "nothing at/above min_score 70 survived pooling",
    "enrich0": "candidates qualified at ticker stage but ALL were skipped in enrichment (a decision, not a fault)",
    "sizefail": "sizing failed",
    "cbpause": "circuit breaker pause", "cbtrip": "circuit breaker trip",
}
BLIND_STAGES = {
    "r": "fills read failed outright",
    "t": "fill rows present but no timestamp parsed on any row",
    "k": "in-window fills carry no recognised profit field",
    "e": "fills read EMPTY on a state-blind cycle -- 'empty' untrustworthy while sibling reads fail (v0.9.36)",
}


def decode_digest(tok):
    m = re.match(r"^(\w+):([Lsn])(?:([A-Z0-9]+?)(\d{2,3})([qx]))?-?$", tok)
    if not m:
        return f"  {tok}: UNPARSED universe token"
    uni, gate, sym, score, ql = m.groups()
    gates = {"L": "LONG-gated", "s": "SHORT-gated", "n": "NEUTRAL (stood down)"}
    out = f"  {uni}: regime {gates[gate]}"
    if sym:
        out += f"; best {sym} score {score} "
        if ql == "q":
            out += "QUALIFIED (can trade if pooled best + gates allow)"
        elif int(score) < 70:
            out += "not qualified (x): below the min_score 70 floor"
        else:
            out += ("DISQUALIFIED (x) despite clearing the floor -- failed a hard "
                    "gate (funding/book-wall/overextension/blackout). NOT "
                    "slot-blocked: an x-name cannot trade even if every slot is free")
    else:
        out += "; no candidate scored"
    return out


def decode_breaker(tok, frac, budget, lev, window):
    thr = frac * budget * lev
    if tok.startswith("b?"):
        st = tok[2:3]
        base = (f"  breaker: BLIND this cycle (stage '{st}': "
                f"{BLIND_STAGES.get(st, 'unknown stage -- grammar drift?')})")
        if "." in tok:
            base += f"; time-key probe suffix: {tok.split('.',1)[1]}"
        return base
    if tok.startswith("b!"):
        over = tok[2:]
        return (f"  breaker: TRIPPED, ~${over} past the ${thr:.0f} threshold. "
                f"No new entries until fills age out of the 24h window "
                f"(existing positions keep their exchange SL/TP)")
    m = re.match(r"^b(\d+)$", tok)
    if m:
        hr = int(m.group(1))
        line = (f"  breaker: ARMED, ~${hr} of further realized loss to the trip "
                f"(threshold ${thr:.0f} = frac {frac} x budget {budget:.0f} x lev {lev}"
                f" -- NEVER frac x equity)")
        if window:
            implied = thr - sum(-w for w in window if w < 0)
            line += (f"\n           cross-check: given window losses "
                     f"{[round(w,2) for w in window]}, expected token b{implied:.0f}"
                     f" -- {'CONSISTENT' if abs(implied - hr) <= 1.5 else 'MISMATCH: frac/budget likely card-tuned; solve frac = (headroom + losses) / 1000'}")
        return line
    return f"  breaker token UNPARSED: {tok}"


def decode_tail(tail):
    parts = tail.split(".", 1)
    kind, det = parts[0], (parts[1] if len(parts) > 1 else "")
    if kind in TAILS:
        out = f"  tail '{kind}': {TAILS[kind]}"
        if kind == "no" and det:
            out += f" -- {NO_REASONS.get(det, det)}"
        elif kind == "hld" and det:
            m = re.match(r"^([A-Z0-9]+?)([+-]\d+)([a-zPB]*)\.t(\d+|\?)h$", det)
            if m:
                sym, mv, flags, age = m.groups()
                out += (f" -- {sym} {mv}% (stateless whole-%% rounding, NOT a tier), "
                        f"flags '{flags}' (a=BE armed, l=lock floor sets stop, "
                        f"s=scale-out, r=trail set stop; P=pullback 4h clock, "
                        f"B=breakout clock, NEITHER = mode unknown -> 12h global "
                        f"cap governs, v0.9.37), held {age}h "
                        f"(plain counter; the only hard clocks are the 4h pullback / "
                        f"12h breakout time-stops)")
            else:
                out += f" -- {det}"
        elif det:
            out += f" -- {det}"
        return out
    return (f"  tail '{tail}': raw open-path reason (entry_already_pending / "
            f"already_in_position / correlation_budget / cooldown / ...)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("line")
    ap.add_argument("--frac", type=float, default=DEF_FRAC,
                    help="loss_breaker_frac (card value if tuned; default manifest 0.018)")
    ap.add_argument("--budget", type=float, default=DEF_BUDGET)
    ap.add_argument("--lev", type=int, default=DEF_LEV)
    ap.add_argument("--window", default="",
                    help="space-separated realized USD fills still in the 24h window")
    a = ap.parse_args()
    window = [float(x) for x in a.window.split()] if a.window.strip() else []

    line = a.line.strip()
    if line.startswith("SCAN-"):
        line = line[5:]
    segs = line.split("|")
    digest, exec_seg = segs[0], (segs[1] if len(segs) > 1 else "")
    fate = segs[2] if len(segs) > 2 else ""

    print("=== digest ===")
    for tok in digest.split("-"):
        if ":" in tok:
            print(decode_digest(tok))
        elif tok:
            # score token split across '-'? re-join heuristic failed -> show raw
            print(f"  (raw) {tok}")
    print("  NOTE: L/s = regime GATE DIRECTION, not a quality tier; a q'd short is a full setup.")

    if exec_seg:
        print("=== exec state ===")
        rest = exec_seg
        if rest.startswith("nof-"):
            print("  nof-: NON-FOLLOW cycle (eval/pre-window) -- the engine scanned but "
                  "CANNOT trade this cycle. Not 'no fills'.")
            rest = rest[4:]
        m = re.match(r"^o(\d+|\?)p(\d+|\?|T\d+)(.*)$", rest)
        if m:
            own, pend, tail_part = m.groups()
            print(f"  o{own}: {own} owned SYMBOL(s) (positions+pendings, size-scoped);"
                  f" p{pend}: {'pending count unknown (mgmt did not run / read blind)' if pend=='?' else pend + ' pending order(s)'}")
            tp = tail_part.lstrip("-")
            btok = ""
            mb = re.match(r"^(b[!?]?[\w.]*?)-(.*)$", tp)
            if mb and mb.group(1).startswith("b"):
                btok, tp = mb.group(1), mb.group(2)
            elif tp.startswith("b") and "-" not in tp:
                btok, tp = tp, ""
            if btok:
                print(decode_breaker(btok, a.frac, a.budget, a.lev, window))
            else:
                print("  (no breaker token -- dropped under the 63-char budget, or breaker disabled)")
            if tp:
                print(decode_tail(tp))
        else:
            print(f"  UNPARSED exec segment: {rest}")
    if fate:
        print(f"=== fate === {fate}")


if __name__ == "__main__":
    main()
