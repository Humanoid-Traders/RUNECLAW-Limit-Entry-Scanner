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
    fate = ml._leader_fate("ETHUSDT", {"pre": 87, "post": 87, "skip": True,
                                       "reason": "funding_crowded"}, placed_sym="TAOUSDT")
    _assert(fate == "skip=funding_cr", "enrichment hard-skip named (reason, truncated)")


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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} observability tests passed.")
