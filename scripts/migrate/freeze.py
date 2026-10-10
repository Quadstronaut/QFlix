#!/usr/bin/env python3
"""freeze.py -- the cutover freeze and its exact mirror (50 step 1 / 55).

Runs ON blue, fed over ssh stdin, with blue's own ~/secrets (no credential
ever crosses argv):

  ssh blue "python3 - snapshot"        < scripts/migrate/freeze.py
  ssh blue "python3 - pause '<json>'"  < scripts/migrate/freeze.py
  ssh blue "python3 - resume '<json>'" < scripts/migrate/freeze.py

WHY A SNAPSHOT (the hashes=all gap): the stale scripts paused with
`hashes=all` and rolled back with `hashes=all`, so a torrent the operator had
paused on purpose before cutover came back ACTIVE after a rollback. Now:

  snapshot  -> {"qbit": [hashes that are active right now], "sab_was_paused": bool}
               50-cutover.sh stores it locally BEFORE pausing, once (a re-run
               must not re-snapshot an already-frozen box and record nothing).
  pause     <- that snapshot: pauses exactly those hashes + SAB, then re-polls
               (qBit/SAB APIs lie; memory: re-poll to verify).
  resume    <- the same snapshot: resumes exactly those hashes, and SAB only if
               it was running at the snapshot.

qBit >= 5.0 renamed pause/resume to stop/start (WebUI API v2.11) and the
paused* states to stopped*; both spellings are sent/accepted.
Exit 0 verified, 1 the box did not reach the wanted state, 2 bad input /
missing secret.
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, List

PAUSED_WORDS = ("paused", "stopped")


def is_paused(state: str) -> bool:
    s = (state or "").lower()
    return any(w in s for w in PAUSED_WORDS)


def active_hashes(info: Iterable[dict]) -> List[str]:
    return sorted(t["hash"] for t in info if not is_paused(t.get("state", "")))


def not_in_state(info: Iterable[dict], hashes: Iterable[str], want_paused: bool) -> List[str]:
    want = set(hashes)
    return sorted(t["hash"] for t in info
                  if t["hash"] in want and is_paused(t.get("state", "")) != want_paused)


def _secret(name: str) -> str:
    return (Path.home() / "secrets" / name).read_text(encoding="utf-8").strip()


class Qbit:
    def __init__(self):
        self.base = "http://127.0.0.1:%s" % _secret("qbittorrent.port")
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        body = urllib.parse.urlencode({"username": _secret("qbittorrent.user"),
                                       "password": _secret("qbittorrent.password")}).encode()
        with self.op.open(self.base + "/api/v2/auth/login", body, timeout=20) as r:
            if b"Ok" not in r.read():
                raise RuntimeError("qbit-auth-failed")

    def info(self) -> list:
        with self.op.open(self.base + "/api/v2/torrents/info", timeout=30) as r:
            return json.loads(r.read())

    def act(self, verbs, hashes: List[str]) -> None:
        for i in range(0, len(hashes), 200):   # keep each POST body small
            body = urllib.parse.urlencode({"hashes": "|".join(hashes[i:i + 200])}).encode()
            for verb in verbs:                 # new + old API name; the other 404s
                try:
                    self.op.open(self.base + "/api/v2/torrents/" + verb, body, timeout=30).read()
                except OSError:
                    pass


def sab(mode: str) -> dict:
    q = urllib.parse.urlencode({"mode": mode, "output": "json"})
    body = urllib.parse.urlencode({"apikey": _secret("sabnzbd.key")}).encode()
    url = "http://127.0.0.1:%s/api?%s" % (_secret("sabnzbd.port"), q)
    with urllib.request.urlopen(url, body, timeout=20) as r:
        raw = r.read()
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def sab_paused() -> bool:
    return bool((sab("queue").get("queue") or {}).get("paused"))


def main(argv: List[str]) -> int:
    cmd = argv[0] if argv else ""
    try:
        snap = json.loads(argv[1]) if cmd in ("pause", "resume") else None
        if snap is not None and not isinstance(snap.get("qbit"), list):
            raise ValueError("snapshot has no qbit list")
    except (IndexError, ValueError, AttributeError) as exc:
        sys.stderr.write("STAGE=usage msg=freeze-%s-needs-snapshot-json:%s\n" % (cmd, exc))
        return 2
    try:
        qb = Qbit()
        if cmd == "snapshot":
            print(json.dumps({"qbit": active_hashes(qb.info()), "sab_was_paused": sab_paused()}))
            return 0
        hashes = snap["qbit"]
        if cmd == "pause":
            qb.act(("stop", "pause"), hashes)
            sab("pause")
            want_paused, sab_want = True, True
        elif cmd == "resume":
            qb.act(("start", "resume"), hashes)
            if not snap.get("sab_was_paused"):
                sab("resume")
            want_paused, sab_want = False, bool(snap.get("sab_was_paused"))
        else:
            sys.stderr.write("STAGE=usage msg=freeze-action-must-be-snapshot|pause|resume\n")
            return 2
        bad: List[str] = []
        for _ in range(10):                    # re-poll: these APIs answer before acting
            time.sleep(2)
            bad = not_in_state(qb.info(), hashes, want_paused)
            if not bad and sab_paused() == sab_want:
                print(json.dumps({"ok": True, "action": cmd, "torrents": len(hashes)}))
                return 0
        print(json.dumps({"ok": False, "action": cmd, "torrents_wrong_state": len(bad),
                          "sab_paused": sab_paused()}))
        return 1
    except OSError as exc:
        sys.stderr.write("STAGE=freeze-%s msg=%s\n" % (cmd, str(exc).replace(" ", "-")[:200]))
        return 2 if isinstance(exc, FileNotFoundError) else 1
    except RuntimeError as exc:
        sys.stderr.write("STAGE=freeze-%s msg=%s\n" % (cmd, exc))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
