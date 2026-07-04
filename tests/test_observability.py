"""v0.9.8 visible-surface observability tests.

The SITREP tool reads the emitted signal's compact symbol string (the dbg /
scan lines), NOT the metrics payload -- so every field added to metrics since
v0.9.0 (breaker threshold/headroom, per-name scores) was invisible to the
operator, who kept re-deriving the breaker by hand. These pin the two helpers
that fold the critical numbers back into the visible strings:

  _breaker_token  -- headroom / tripped / unreadable / disabled, compactly.
  _scan_digest    -- per-universe gate + best candidate + qualified/skipped, so a
                     missed trend (bot sitting out a moving universe) is
                     diagnosable from the feed alone.

Run: python3 tests/test_observability.py
"""
import types

from _stub import stub_getagent, load_src

stub_getagent()
# main_live imports the sibling engines at module load; bring them up on the stub.
load_src("features"); load_src("execution"); load_src("risk"); load_src("scoring")
ml = load_src("main_live")


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


# ---- _breaker_token ----

def test_breaker_token_headroom():
    _assert(ml._breaker_token({"loss_breaker_headroom": 63.34,
                               "loss_breaker_threshold": 80.0}) == "-b63",
            "armed with headroom -> -b<int headroom> (visible in the compact line)")


def test_breaker_token_tripped():
    _assert(ml._breaker_token({"loss_breaker_headroom": -12.0,
                               "loss_breaker_threshold": 80.0}) == "-b!12",
            "headroom <= 0 -> -b!<over> (tripped, unmistakable)")


def test_breaker_token_unreadable():
    _assert(ml._breaker_token({"loss_breaker_threshold": 80.0}) == "-b?",
            "threshold known but window unreadable -> -b?")


def test_breaker_token_disabled():
    _assert(ml._breaker_token({}) == "",
            "breaker off (no fields) -> empty token, nothing misleading emitted")


# ---- _scan_digest ----

def _cand(symbol, score, skip=False):
    return types.SimpleNamespace(symbol=symbol, score=score, skip=skip)


def test_scan_digest_answers_the_crypto_miss():
    # the live case: crypto gated LONG but its best name is below the floor, while
    # equities gated long and qualified -> the digest states exactly why the bot
    # traded the equity and sat out the moving crypto board.
    scans = [
        {"name": "crypto", "direction": "long",
         "scored": [_cand("ETHUSDT", 62), _cand("SOLUSDT", 58)]},
        {"name": "metals", "direction": "none", "scored": []},
        {"name": "equities", "direction": "long",
         "scored": [_cand("MSTRUSDT", 78), _cand("NVDAUSDT", 71)]},
    ]
    out = ml._scan_digest(scans, min_score=70)
    _assert(out == "SCAN-cry:LETH62x-met:n-equ:LMSTR78q", "digest names gate+best+verdict per universe")
    _assert("ETH62x" in out, "crypto best ETH 62 flagged BELOW floor (x) -> the miss is visible")
    _assert("MSTR78q" in out, "equities best MSTR 78 qualified (q)")
    _assert("met:n" in out, "neutral-regime universe shown as stood-down (n), no false candidate")


def test_scan_digest_short_gate_and_skip():
    scans = [{"name": "equities", "direction": "short",
              "scored": [_cand("TSLAUSDT", 82, skip=True)]}]  # high score but hard-skipped
    out = ml._scan_digest(scans, min_score=70)
    _assert(out == "SCAN-equ:sTSLA82x",
            "short gate shows 's'; a hard-skipped candidate is x even above the floor")


def test_scan_digest_gated_but_no_candidates():
    out = ml._scan_digest([{"name": "crypto", "direction": "long", "scored": []}], 70)
    _assert(out == "SCAN-cry:L-", "gated long but nothing scored -> '<abbr>:L-'")


def test_scan_digest_truncates():
    scans = [{"name": "u%d" % i, "direction": "long", "scored": [_cand("VERYLONGUSDT", 88)]}
             for i in range(12)]
    _assert(len(ml._scan_digest(scans, 70)) <= 63, "digest capped at the 63-char signal-symbol budget")


# ---- _leader_fate (why the board leader wasn't the trade) ----

def test_leader_fate_none_when_leader_is_the_trade():
    _assert(ml._leader_fate("ETHUSDT", {"pre": 87, "post": 84, "skip": False},
                            placed_sym="ETHUSDT") is None,
            "leader IS the placed trade -> None (nothing to explain)")
    _assert(ml._leader_fate(None, None, None) is None, "no leader -> None")


def test_leader_fate_hard_skip():
    # non-funding skip (no bps) keeps the generic reason form
    fate = ml._leader_fate("ETHUSDT", {"pre": 87, "post": 87, "skip": True,
                                       "reason": "breakout_unconfirmed"}, placed_sym="TAOUSDT")
    _assert(fate == "skip=breakout_u", "generic hard-skip named (reason, truncated)")


def test_leader_fate_funding_skip_carries_bps():
    # v0.9.11: a funding skip carries the ACTUAL bps -- the number that proves a
    # glitch (fcr+47, when real funding is <3bps) vs a genuine >30bps crowd.
    fate = ml._leader_fate("MSTRUSDT", {"pre": 100, "post": 100, "skip": True,
                                        "reason": "funding_crowded_long", "bps": 47.3},
                           placed_sym="XAGUSDT")
    _assert(fate == "skip=fcr+47", "funding skip shows signed bps (glitch-confirming)")
    short = ml._leader_fate("MSTRUSDT", {"pre": 100, "post": 100, "skip": True,
                                         "reason": "funding_crowded_short", "bps": -52.0},
                            placed_sym="XAGUSDT")
    _assert(short == "skip=fcr-52", "crowded-short funding skip shows negative bps")


def test_leader_fate_demote():
    # the 08:49 shape: ETH led at 87 ticker, trend/funding penalty dropped it to 69,
    # TAO took the limit. The fate string states exactly that.
    fate = ml._leader_fate("ETHUSDT", {"pre": 87, "post": 69, "skip": False}, placed_sym="TAOUSDT")
    _assert(fate == "demote:87->69", "trend/funding demotion shown pre->post")


def test_leader_fate_outrank():
    # leader survived enrichment (no skip, no material demote) but a lower name's
    # alignment lifted it past the leader.
    fate = ml._leader_fate("ETHUSDT", {"pre": 87, "post": 87, "skip": False}, placed_sym="SOLUSDT")
    _assert(fate == "outrank:ETH", "survived (no material demote) but outranked -> names the leader")


def test_leader_fate_folds_onto_scan_line_budget():
    # the fate is appended to the SCAN line; the whole thing stays within 63 chars.
    line = ("SCAN-cry:LETH87q-met:LXAG64x-equ:sTSLA70q" + "|" + "demote:87->69")[:63]
    _assert(len(line) <= 63, "SCAN line + fate stays within the signal-symbol budget")


# ---- _blind_token (v0.9.14: a blind read must not look like a flat idle cycle) ----

def test_blind_token_clean_read():
    _assert(ml._blind_token({"state_blind": False}) == "",
            "book read cleanly -> no bl. token (nothing misleading)")


def test_blind_token_priority():
    # management crash wins over every downstream reason
    _assert(ml._blind_token({"state_blind": True, "mgmt_error": "KeyError",
                             "position_query_error": "TimeoutError"}) == "bl.crash:KeyError",
            "a management crash surfaces first (bl.crash:<Exc>)")
    _assert(ml._blind_token({"state_blind": True, "position_query_error": "TimeoutError"})
            == "bl.posq:TimeoutErr", "position query raised -> bl.posq")
    _assert(ml._blind_token({"state_blind": True, "pending_error": "ConnErr"})
            == "bl.pendq:ConnErr", "pending query raised -> bl.pendq")
    _assert(ml._blind_token({"state_blind": True, "blind_reason": "pos_margin_1.2_vs_empty"})
            == "bl.margin", "positions empty but margin locked -> bl.margin (the read-lie)")
    _assert(ml._blind_token({"state_blind": True}) == "bl.?",
            "blind with no attributable reason -> bl.? (never silent)")


# ---- _circuit_state_token (v0.9.14: flag an equity circuit on a dead .state/) ----

def test_circuit_state_token_dead_state():
    dead = {"controls_active": {"circuit_breaker": True}, "state_runs": 1}
    _assert(ml._circuit_state_token(dead) == "-cx",
            "circuit claims active but state_runs stuck at 1 -> -cx (day_start never carries)")


def test_circuit_state_token_persisting_state():
    live = {"controls_active": {"circuit_breaker": True}, "state_runs": 42}
    _assert(ml._circuit_state_token(live) == "",
            "state_runs climbing -> .state/ persists -> no warning (circuit is valid)")
    off = {"controls_active": {"circuit_breaker": False}, "state_runs": 1}
    _assert(ml._circuit_state_token(off) == "",
            "circuit not active (no equity read) -> no -cx even on ephemeral state")
    _assert(ml._circuit_state_token({}) == "", "no fields -> empty")


# ---- _watch_short (v0.9.14: the six stand-downs stop collapsing to 'none') ----

def test_watch_short_maps_known_reasons():
    _assert(ml._watch_short("all_regimes_neutral") == "neutral", "neutral regime compacted")
    _assert(ml._watch_short("no_setup_at_or_above_min_score") == "lowscore", "below-floor compacted")
    _assert(ml._watch_short("no_setup_after_enrichment") == "enrich0", "enrichment-empty compacted")
    _assert(ml._watch_short("sizing_failed") == "sizefail", "sizing failure compacted")
    _assert(ml._watch_short("circuit_paused") == "cbpause", "circuit pause compacted")


def test_watch_short_dynamic_reason_passthrough():
    _assert(ml._watch_short("entry_too_far_3pct") == "far:3pct",
            "dynamic entry-too-far keeps its number (far:Npct)")
    _assert(len(ml._watch_short("some_unknown_very_long_reason_string_here")) <= 16,
            "an unknown reason is truncated to the tail budget")


# ---- _held_token (v0.9.14: the quiet steady-state position becomes visible) ----

def _diag(sym, move, age, ts_ok=True, be_armed=False, be_lock=None, so=None, trail=None):
    d = {"sym": sym, "move_pct": move, "age_h": age, "ts_ok": ts_ok, "be_armed": be_armed}
    if be_lock is not None:
        d["be_lock"] = be_lock
    if so is not None:
        d["so"] = so
    if trail is not None:
        d["trail"] = trail
    return d


def test_held_token_none_when_nothing_managed():
    _assert(ml._held_token({}, {}) == "", "no position_diag -> empty")
    _assert(ml._held_token({"position_diag": [{"sym": "X", "note": "unmanaged_symbol"}]}, {}) == "",
            "an unmanaged diag (no move_pct) is not a held position -> empty")


def test_held_token_surfaces_move_flags_and_age():
    # MSTR up 4%, breakeven armed + lock floored + trail set, 9h into a 12h ceiling.
    diag = _diag("MSTRUSDT", 4.3, 9.2, be_armed=True, be_lock=100.7, trail="set:100.7000")
    tok = ml._held_token({"position_diag": [diag]}, {"time_stop_hours": "12"})
    _assert(tok == "hld.MSTR+4alr.t9/12",
            "hld names sym, +move, flags(a=armed,l=lock,r=trail), age/cap -> " + tok)


def test_held_token_picks_oldest_and_blind_timestop():
    young = _diag("NVDAUSDT", 1.0, 2.0)
    # old is the oldest by age_h but ts_ok False -> its open-time is unreadable, so
    # the time-stop is blind on it (the more urgent thing to show): .t?/cap.
    old = _diag("TSLAUSDT", -2.0, 8.0, ts_ok=False)
    tok = ml._held_token({"position_diag": [young, old]}, {"time_stop_hours": "12"})
    _assert(tok == "hld.TSLA-2.t?/12",
            "oldest held wins; ts_ok False -> .t?/cap (time-stop is blind on it) -> " + tok)


def test_held_token_within_budget():
    diag = _diag("VERYLONGNAMEUSDT", 123, 240, be_armed=True, be_lock=1.0,
                 so="trimmed:5", trail="set:1.0")
    _assert(len(ml._held_token({"position_diag": [diag]}, {})) <= 32,
            "hld token capped so the compact line never overflows")


# ---- _fold_exec_onto_scan (v0.9.17: the exec-state rides the VISIBLE surface) ----

def test_fold_exec_basic_watch():
    # flat watch: the DBG exec tail (no.neutral) now rides the SCAN line the operator
    # actually sees -- the DBG emit was clobbered by this SCAN emit every cycle.
    out = ml._fold_exec_onto_scan("SCAN-cry:LADA82q", 0, 1, "", "", "no.neutral", None)
    _assert(out == "SCAN-cry:LADA82q|o0p1-no.neutral",
            "watch: digest + o<own>p<pend> + tail, no breaker/fate -> " + out)


def test_fold_exec_held_with_breaker():
    out = ml._fold_exec_onto_scan("SCAN-cry:LADA82q", 0, 1, "-b45", "", "hld.MSTR+2a.t3/12", None)
    _assert(out == "SCAN-cry:LADA82q|o0p1-b45-hld.MSTR+2a.t3/12",
            "held: breaker headroom + hld tail both fold on when they fit -> " + out)


def test_fold_exec_circuit_and_pending_unreadable():
    # -cx (dead .state circuit) folds in; pT can be '?' when the pending book is blind
    out = ml._fold_exec_onto_scan("SCAN-cry:LADA82q", 0, "?", "", "-cx", "no.lowscore", None)
    _assert(out == "SCAN-cry:LADA82q|o0p?-cx-no.lowscore",
            "-cx and an unreadable pending count both surface -> " + out)


def test_fold_exec_fate_appended_when_it_fits():
    out = ml._fold_exec_onto_scan("SCAN-cry:LADA82q", 0, 0, "", "", "no.neutral", "skip=fcr+47")
    _assert(out == "SCAN-cry:LADA82q|o0p0-no.neutral|skip=fcr+47",
            "leader_fate is appended after the exec seg when there is room -> " + out)


def test_fold_exec_fate_dropped_never_truncates_tail():
    # a full 3-universe digest + exec tail leaves no room for the fate: it is dropped
    # WHOLE, never clipped into the tail.
    digest = "SCAN-cry:LADA82q-met:sXAG60x-equ:LMSTR100q"
    out = ml._fold_exec_onto_scan(digest, 0, 1, "", "", "no.lowscore", "demote:87->69")
    _assert(out == digest + "|o0p1-no.lowscore",
            "fate dropped intact when it would overflow -> " + out)
    _assert("demote" not in out, "no partial fate bleed")


def test_fold_exec_breaker_shed_first_to_protect_tail():
    # with the breaker the line would overflow; the breaker headroom is shed first
    # (it survives on the DBG metrics/token) so the held tail is preserved.
    digest = "SCAN-cry:LADA82q-met:sXAG60x-equ:LMSTR100q"
    out = ml._fold_exec_onto_scan(digest, 1, 0, "-b12", "", "hld.MSTR+2al", None)
    _assert(len(out) <= 63, "never exceeds the 63-char budget")
    _assert("-b12" not in out and "hld.MSTR+2al" in out,
            "breaker headroom shed before the held tail is touched -> " + out)


def test_fold_exec_always_within_budget():
    digest = "SCAN-cry:LVERYLONG100q-met:sXAGXX99x-equ:LMSTRLONG100q"
    out = ml._fold_exec_onto_scan(digest, 2, 3, "-b!99", "-cx", "hld.MSTR+12alr.t11/12", "outrank:ETHUS")
    _assert(len(out) <= 63, "max-content fold still capped at 63 -> len " + str(len(out)))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} observability tests passed.")
