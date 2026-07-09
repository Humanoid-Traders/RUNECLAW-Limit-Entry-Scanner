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
    "cbpause": "circuit breaker pause",
    "cbtrip": "circuit breaker trip -- since v0.9.39 also the account-day Rule-13 halt (realized fills since UTC midnight <= -circuit_stop_usdt)",
    "paused": "entries_paused safe mode (card key): NEW entries stopped, management engine still running",
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
    if line.startswith("DISC-"):
        # v0.9.44 discovery-source marker: DISC-<source>-<n>c[-<SYM><score>].
        # A dedicated line because the SCAN `d:` token is budget-dropped on every
        # scored 3-universe board -- this is what answers 'bulk live or blind?'.
        m = re.match(r"^DISC-(.+?)-(\d+)c(?:-([A-Z0-9]+?)(\d{1,3}))?$", line)
        print("=== discovery marker (v0.9.44) ===")
        if not m:
            print(f"  UNPARSED discovery marker: {line}")
            return
        src, n, sym, score = m.groups()
        # v0.9.48: source may carry an enumeration diagnostic "<src>;e=<diag>"
        enum_diag = ""
        if ";e=" in src:
            src, enum_diag = src.split(";e=", 1)
        if src in ("tickers", "ticker"):
            verdict = "bulk ticker surface LIVE -- forward test is collecting"
        elif src == "derivatives_tickers":
            verdict = ("REAL bulk enumeration LIVE (v0.9.46) -- derivatives_tickers "
                       "feed ranked the venue's perps by volume; catches UNKNOWN listings")
        elif src == "watchlist":
            verdict = ("per-symbol FALLBACK active (v0.9.45) -- bulk surface blind, "
                       "probing the named discovery_watchlist; cannot catch UNKNOWN listings")
        elif src == "no_bulk_surface":
            verdict = ("BLIND -- no bulk SDK surface found (fail-open); the "
                       "SDK-native per-symbol fallback is warranted")
        elif src.startswith("error:"):
            verdict = "EXCEPTION path -- discovery raised " + src.split(":", 1)[1]
        else:
            verdict = "source=" + src
        print(f"  source: {src} -> {verdict}")
        if enum_diag:
            if enum_diag == "nomethod":
                d = "crypto.derivatives_tickers() ABSENT on the SDK -- enumeration dead, watchlist is the path"
            elif enum_diag == "rows0":
                d = "derivatives_tickers() returned 0 rows (exists but empty)"
            elif enum_diag.startswith("err:"):
                d = "derivatives_tickers() raised " + enum_diag[4:]
            elif enum_diag.startswith("m0of"):
                d = ("rows returned but 0 matched the venue/perp/USDT/floor filter; "
                     "sample row -> " + enum_diag.split(":", 1)[1] + "  (market/symbol/volume format names the fix)")
            else:
                d = enum_diag
            print(f"  ENUM DIAG (why derivatives_tickers was empty): {d}")
        print(f"  candidates scored this cycle: {n}")
        print(f"  top candidate: {sym} score {score}" if sym
              else "  top candidate: none scored this cycle")
        print("  NOTE: LOUD every cycle while blind/errored; hourly (:00) heartbeat while healthy.")
        return
    if line.startswith("SCAN-"):
        line = line[5:]
    segs = line.split("|")
    digest, exec_seg = segs[0], (segs[1] if len(segs) > 1 else "")
    # v0.9.41: the discovery token `d:<SYM><score>` rides as a trailing segment
    # (lowest budget priority); pull it out wherever it landed, the rest is fate.
    trailing = segs[2:]
    disc_seg = next((t for t in trailing if t.startswith("d:")), "")
    fate = next((t for t in trailing if not t.startswith("d:")), "")

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
        if "-dw" in exec_seg:
            print("  -dw: account-day WARNING (v0.9.39) -- realized since UTC midnight past the")
            print("       Rule-10 soft line (circuit_pause_usdt). Warn only; entries keep flowing.")
        if "-!" in exec_seg:
            code = exec_seg.split("-!", 1)[1][:3]
            inv = {"clk": "per-mode clocks armed but mode recovery UNKNOWN -- position on the wrong (12h global) clock",
                   "mgn": "live position is CROSSED while the manifest says isolated",
                   "sl": "protective stop implies risk > max_loss x 1.3 -- oversized"}
            print(f"  -!{code}: INVARIANT SENTINEL confession (v0.9.39): "
                  f"{inv.get(code, 'unknown code -- grammar drift?')}")
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
    if disc_seg:
        body = disc_seg[2:]
        i = len(body)
        while i > 0 and body[i-1].isdigit():
            i -= 1
        sym, score = body[:i], body[i:]
        print("=== discovery (v0.9.42 multi-class forward test) ===")
        print(f"  {sym} scored {score} -- the top-scored candidate the SHADOW scan"
              f" surfaced (could be crypto, a tokenized stock/ETF, or a commodity --"
              f" each scored under its own regime leader). LOGGED only, never traded;"
              f" the full per-class list is in metrics.discovery.")
    if fate:
        print(f"=== fate === {fate}")


if __name__ == "__main__":
    main()
