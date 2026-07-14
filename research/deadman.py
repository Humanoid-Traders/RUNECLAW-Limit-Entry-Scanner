#!/usr/bin/env python3
"""Dead-man's switch for the live RUNECLAW deployment (CI-side, zero-dep).

THE SCAR IT GUARDS (2026-07-07 reconciliation): the live week's
underperformance was DARK-BOARD HOURS -- the engine not running -- not
slippage or parameters. "Uptime beats parameters" is ledger rule 7, and
until now the only dark-board detector was the operator noticing the card
went quiet.

WHAT IT CAN AND CANNOT SEE (adjudicated 2026-07-14): the control plane has
no signal-history endpoint (all plausible paths 403 at the gateway -- same
wall class as metrics.discovery). The authenticated surface that DOES work
is GET /api/v1/playbook/my-playbooks, which returns each deployment's live
`status`. That covers the dominant real failure mode: the instance sitting
"disabled"/absent while the operator believes it is running (the
flatten -> deploy -> forget-to-re-enable gap, and the accidental card-click
class -- both have happened). A scheduler stall while status=active remains
undetectable from outside; that is a platform wall, not a gap in this
script.

Behavior (report-only philosophy, like listings-watch):
  * healthy  -> prints OK, writes nothing
  * dark     -> writes research/.deadman_alert.json for the workflow's
                issue step (create/comment)
  * recovery -> the workflow closes the open dead-man issue

An alert during an INTENTIONAL flatten window is a feature, not noise: it
doubles as the re-enable reminder.

Auth: ACCESS-KEY read from the GETAGENT_ACCESS_KEY environment variable
(GitHub Actions secret). Never passed on the command line, never printed.

  GETAGENT_ACCESS_KEY=... python3 research/deadman.py
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

URL = "https://api.bitget.com/api/v1/playbook/my-playbooks"
STRATEGY_ID = "e977214c-86e5-405b-be0b-d5bad50b97c8"
HERE = Path(__file__).resolve().parent
ALERT = HERE / ".deadman_alert.json"


def main():
    key = os.environ.get("GETAGENT_ACCESS_KEY", "").strip()
    if not key:
        print("ABORT: GETAGENT_ACCESS_KEY not set (repo secret missing?)")
        _alert("no_credential", "GETAGENT_ACCESS_KEY env var is empty -- "
               "the workflow secret is missing or masked out")
        return

    try:
        req = urllib.request.Request(URL, headers={"ACCESS-KEY": key,
                                                   "User-Agent": "runeclaw-deadman"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
    except Exception as exc:
        print(f"API unreachable: {type(exc).__name__}")
        _alert("api_error", f"my-playbooks call failed: {type(exc).__name__} "
               "(control plane down, key revoked, or network) -- the board "
               "state is UNKNOWN, verify manually")
        return

    items = (body.get("data") or {}).get("items") or []
    mine = [i for i in items if i.get("strategy_id") == STRATEGY_ID]
    if not mine:
        print("DARK: strategy not present in my-playbooks -- no enabled instance")
        _alert("not_deployed", "No enabled instance of the RUNECLAW strategy "
               "exists -- the card is OFF (post-flatten forget-to-re-enable, "
               "accidental disable, or platform removed it)")
        return

    inst = mine[0]
    status = str(inst.get("status", "")).lower()
    ver = inst.get("version", "?")
    if status == "active":
        print(f"OK: instance active (v{ver}, mode={inst.get('execution_mode')})")
        ALERT.unlink(missing_ok=True)
        return
    print(f"DARK: instance status={status!r} (v{ver})")
    _alert("not_active", f"Instance exists but status is '{status}' "
           f"(version v{ver}) -- the engine is NOT scanning. If this is an "
           "intentional flatten window, treat this as the re-enable reminder.")


def _alert(kind, detail):
    ALERT.write_text(json.dumps({"kind": kind, "detail": detail}, indent=1))


if __name__ == "__main__":
    main()
