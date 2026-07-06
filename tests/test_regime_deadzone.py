"""v0.9.31 regime dead-zone tests (signal-audit finding #2).

All three regime votes were razor-edged (change_pct vs 0, last vs vwap, taker
vs 1.0) -- a leader hovering at +0.05% flipped the whole universe long<->short
between cycles. These pin: dead-zones withdraw WEAK votes (hair-trigger tape ->
none/reduced) while strong tape is untouched; defaults are bit-exact legacy;
and regime_taker_vote="0" restores the 2-vote gate every replay ever validated
(live/harness parity, the v0.9.23 argument).

Run: python3 tests/test_regime_deadzone.py
"""
from _stub import stub_getagent, load_src

stub_getagent()
features = load_src("features")
scoring = load_src("scoring")
SF = features.SymbolFeatures


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def _leader(chg, last, vwap):
    return SF("BTCUSDT", True, last=last, vwap=vwap, high=0, low=0,
              change_pct=chg, quote_volume=1e9)


_DZ = {"allow_short": True, "regime_chg_deadzone_pct": "0.3",
       "regime_vwap_deadzone_pct": "0.1", "regime_taker_deadzone": "0.05"}


def test_defaults_are_bitexact_legacy():
    r = scoring.regime(_leader(0.05, 100.05, 100.0), 1.01, {"allow_short": True})
    _assert(r.direction == "long" and r.size_factor == 1.0,
            "defaults: hair-trigger tape still gates long full-size (legacy edges)")


def test_deadzones_withdraw_weak_votes():
    r = scoring.regime(_leader(0.05, 100.05, 100.0), 1.01, dict(_DZ))
    _assert(r.direction == "none" and r.size_factor == 0.0,
            "chg +0.05% / $0.05 over VWAP / taker 1.01 -> ALL votes inside bands -> none")


def test_deadzones_leave_strong_tape_alone():
    r = scoring.regime(_leader(2.0, 102.0, 100.0), 1.5, dict(_DZ))
    _assert(r.direction == "long" and r.size_factor == 1.0,
            "+2% / +2 over VWAP / taker 1.5 -> full-size long unchanged")
    r = scoring.regime(_leader(-2.0, 98.0, 100.0), 0.6, dict(_DZ))
    _assert(r.direction == "short" and r.size_factor == 1.0,
            "mirrored strong short unchanged")


def test_deadzone_yields_reduced_not_flipped():
    # one strong vote (chg +2%) with the other two inside bands -> half-size long,
    # never a flip to short.
    r = scoring.regime(_leader(2.0, 100.05, 100.0), 1.01, dict(_DZ))
    _assert(r.direction == "long" and r.size_factor == 0.5,
            "single surviving vote -> reduced size, not a whipsaw flip")


def test_taker_vote_toggle_restores_replay_parity():
    cfg = {"allow_short": True, "regime_taker_vote": "0"}
    r = scoring.regime(_leader(2.0, 102.0, 100.0), 1.5, cfg)
    _assert(r.direction == "long" and r.score == 2,
            "taker vote off -> the 2-vote gate every replay validated (gate score 2)")
    r2 = scoring.regime(_leader(2.0, 102.0, 100.0), 0.2, cfg)
    _assert(r2.direction == "long" and r2.score == 2,
            "with the vote off, an extreme taker reading cannot move the gate")


def test_malformed_keys_failsafe_to_legacy():
    cfg = {"allow_short": True, "regime_chg_deadzone_pct": "garbage",
           "regime_vwap_deadzone_pct": None, "regime_taker_deadzone": "-5"}
    r = scoring.regime(_leader(0.05, 100.05, 100.0), 1.01, cfg)
    _assert(r.direction == "long" and r.size_factor == 1.0,
            "garbage/negative keys -> zero bands -> exact legacy behaviour")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} regime-deadzone tests passed.")
