#!/usr/bin/env python3
"""QFLX-74: pin known-bad indexers to the LOWEST Prowlarr priority.

Prowlarr priority runs 1 (most preferred) .. 50 (least); the default is 25.
Prowlarr's app sync copies it onto each *arr's indexer, where it breaks ties
between otherwise-equal releases, so a demoted indexer only wins when nobody
else has the episode.

LimeTorrents supplied 5 of the 6 fake releases on 2026-10-22/23 (a 1.1 GB
Windows PE executable dressed as an .iso, then the same malware wrapped in a
.7z). The qBit excluded_file_names guard (59c) stops the payload; this stops
LimeTorrents winning ties. It is demoted rather than disabled so it can still
fill a gap no other indexer covers.

Keyed by definitionName (stable across a rename in the UI). Idempotent: only
PUTs an indexer whose priority differs. Runs ON the box (04g-...sh ships it).
Env: PROW_KEY, PROW_URL (http://127.0.0.1:<port>/<base>/api/v1).
"""
import json
import os
import sys
import urllib.request

LOWEST = 50
DEMOTE = {
    "limetorrents": LOWEST,
}


def plan(indexers):
    """[(indexer, wanted_priority)] for every indexer whose priority is wrong."""
    out = []
    for ind in indexers:
        want = DEMOTE.get(str(ind.get("definitionName", "")).lower())
        if want is not None and ind.get("priority") != want:
            out.append((ind, want))
    return out


def _req(method, path, body=None):
    r = urllib.request.Request(
        os.environ["PROW_URL"] + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": os.environ["PROW_KEY"], "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        txt = resp.read().decode()
        return json.loads(txt) if txt else None


def main():
    indexers = _req("GET", "/indexer")
    found = {str(i.get("definitionName", "")).lower() for i in indexers}
    for name in sorted(set(DEMOTE) - found):
        print(f"absent: {name} (nothing to demote)")
    todo = plan(indexers)
    for ind, want in todo:
        body = dict(ind, priority=want)
        # forceSave skips Prowlarr's pre-save connectivity test: a flaky site
        # must not block lowering its priority.
        _req("PUT", f"/indexer/{ind['id']}?forceSave=true", body)
        print(f"demoted: {ind['name']} {ind.get('priority')} -> {want}")
    after = {i["id"]: i.get("priority") for i in _req("GET", "/indexer")}
    bad = [ind["name"] for ind, want in todo if after.get(ind["id"]) != want]
    if bad:
        print(f"FAIL: priority did not stick for {bad}", file=sys.stderr)
        return 1
    if not todo:
        print("ok: already demoted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
