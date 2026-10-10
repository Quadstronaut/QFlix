#!/usr/bin/env python3
"""green_facts.py -- read-only facts about one box, as one JSON object.

Runs ON the box, fed over ssh stdin (40-validate-green.sh and the 50-cutover
health gate):

  ssh green "python3 - '<args-json>'" < scripts/migrate/green_facts.py

args = {"dropin": ".config/systemd/user/<gate>.d/execute.conf",
        "members": "secrets/members.yaml", "webhook": "discord-webhook.url",
        "media_root": "media", "libraries": ["Movies", ...]}

Facts (migrate_manifest.evaluate_green turns them into verdicts):
  gate_dropin        bool, the I-5 --execute drop-in exists
  members_armed      the roster's `armed` value (None if unreadable)
  parity_violations  runtime-parity strings in the pusher's state.json
                     (None if state.json is unreadable: could-not-assert)
  webhook_parked     True parked (.held only) / False live / None neither
  ffmpeg_shim        Tdarr's ffmpeg is the threadcap shim (ffmpeg.real exists)
  media_files        video files found (sampling stops at 20)
  timer_count        systemd --user timers, all states
  notes              informational only (Seerr applicationUrl: the Seerr vhost
                     fact; Tautulli notifier count: an I-1 residue to know about)
Mutates nothing. Exit 0 always when it could print JSON, 2 on bad args.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

VIDEO = (".mkv", ".mp4", ".m4v", ".avi")


def facts(args: dict, home: Path) -> dict:
    out: dict = {"notes": {}}
    out["gate_dropin"] = (home / args["dropin"]).exists()
    try:
        import yaml
        out["members_armed"] = (yaml.safe_load((home / args["members"]).read_text()) or {}).get("armed")
    except Exception as exc:  # unreadable roster: report None, the verdict fails it
        out["members_armed"] = None
        out["notes"]["members_error"] = type(exc).__name__
    try:
        state = (home / ".opt" / "maint" / "state.json").read_text(encoding="utf-8")
        out["parity_violations"] = sorted(set(re.findall(r'runtime-parity:[^"\\]*', state)))
    except OSError:
        out["parity_violations"] = None
    url = home / "secrets" / args["webhook"]
    held = home / "secrets" / (args["webhook"] + ".held")
    out["webhook_parked"] = (True if held.exists() and not url.exists()
                             else False if url.exists() else None)
    out["ffmpeg_shim"] = any((home / ".apps" / "tdarr").rglob("ffmpeg.real"))
    n = 0
    for lib in args.get("libraries") or []:
        for root, _dirs, files in os.walk(home / args["media_root"] / lib):
            n += sum(1 for f in files if f.lower().endswith(VIDEO))
            if n >= 20:
                break
        if n >= 20:
            break
    out["media_files"] = n
    try:
        cp = subprocess.run(["systemctl", "--user", "list-timers", "--all", "--no-legend"],
                            capture_output=True, text=True, timeout=20, check=False)
        out["timer_count"] = sum(1 for line in cp.stdout.splitlines() if line.strip())
    except (OSError, subprocess.SubprocessError):
        out["timer_count"] = None
    try:
        s = json.loads((home / ".apps" / "seerr" / "settings.json").read_text(encoding="utf-8"))
        out["notes"]["seerr_applicationUrl"] = (s.get("main") or {}).get("applicationUrl")
    except (OSError, ValueError):
        out["notes"]["seerr_applicationUrl"] = None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % (home / ".apps" / "tautulli" / "tautulli.db"), uri=True)
        out["notes"]["tautulli_notifiers"] = con.execute("select count(*) from notifiers").fetchone()[0]
        con.close()
    except sqlite3.Error:
        out["notes"]["tautulli_notifiers"] = None
    return out


def main(argv) -> int:
    try:
        args = json.loads(argv[0])
    except (IndexError, ValueError):
        sys.stderr.write("STAGE=usage msg=green_facts-needs-args-json\n")
        return 2
    print(json.dumps(facts(args, Path.home())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
