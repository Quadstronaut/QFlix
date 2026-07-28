#!/usr/bin/env python3
"""Emit stats.json for the qflix.starhold.dev invite page, then ship it.

Runs hourly on the seedbox. The seedbox always initiates: it pushes stats.json
up to the public VPS and pulls signups.jsonl back down in the same run. The
public box therefore never needs a credential that can reach anything here.

Only stdlib — deliberately. This has to keep working on the box's Python 3.9
without a venv, because the one job it has is to not silently stop.

Usage:
    qflix-stats.py                 # collect, print JSON, change nothing
    qflix-stats.py --write         # also write LOCAL_OUT
    qflix-stats.py --push          # also scp to the VPS and pull signups back
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
SECRETS = os.path.join(HOME, "secrets")
LOCAL_OUT = os.path.join(HOME, ".opt", "maint", "qflix-stats.json")
SIGNUPS_LOCAL = os.path.join(HOME, ".opt", "maint", "qflix-signups.jsonl")

VPS_HOST = "15.204.116.242"
VPS_USER = "qflix"
VPS_STATS = "/home/qflix/data/stats.json"
VPS_SIGNUPS = "/home/qflix/data/signups.jsonl"
SSH_KEY = os.path.join(HOME, ".ssh", "qflix_stats_ed25519")

# Resolved by title, never by section id — Plex renumbers sections when a
# library is removed and re-added, which has already bitten this stack once.
MOVIE_LIBS = ("QFlix - Movies", "QFlix - Anime Movies")
SHOW_LIBS = ("QFlix - TV", "QFlix - Anime")

TIMEOUT = 25


def secret(name, default=""):
    path = os.path.join(SECRETS, name)
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def plex_get(path, params):
    host = secret("plex.host", "127.0.0.1")
    port = secret("plex.port", "32400")
    params = dict(params)
    params["X-Plex-Token"] = secret("plex.token")
    url = "http://%s:%s%s?%s" % (host, port, path, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def section_map():
    """title -> (key, type) for every library on the server."""
    mc = plex_get("/library/sections", {})["MediaContainer"]
    return {d["title"]: (d["key"], d["type"]) for d in mc.get("Directory", [])}


def total_size(key, extra=None):
    """Container totalSize without fetching any items (Size=0)."""
    params = {"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "0"}
    if extra:
        params.update(extra)
    mc = plex_get("/library/sections/%s/all" % key, params)["MediaContainer"]
    return int(mc.get("totalSize", 0))


_QUOTA_UNITS = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def quota_bytes():
    """Bytes used by this account, from `quota -s`.

    Deliberately not `df`: this is a shared Ultra.cc box, so df reports the
    whole filesystem and would overstate the library by an order of magnitude.
    """
    try:
        out = subprocess.run(
            ["quota", "-s"], capture_output=True, text=True, timeout=20
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("/dev/"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        m = re.match(r"^([0-9.]+)([KMGT])?$", parts[1])
        if not m:
            continue
        return int(float(m.group(1)) * _QUOTA_UNITS.get(m.group(2) or "", 1))
    return None


def collect():
    libs = section_map()
    libraries, films, series, episodes = [], 0, 0, 0

    for title in MOVIE_LIBS:
        if title not in libs:
            continue
        n = total_size(libs[title][0])
        films += n
        libraries.append({"name": title, "items": n})

    for title in SHOW_LIBS:
        if title not in libs:
            continue
        key = libs[title][0]
        n = total_size(key)
        series += n
        episodes += total_size(key, {"type": "4"})
        libraries.append({"name": title, "items": n})

    stats = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "libraries": libraries,
        "films": films,
        "series": series,
        "episodes": episodes,
    }

    disk = quota_bytes()
    if disk:
        stats["disk_bytes"] = disk
    return stats


def _scp(src, dst):
    cmd = [
        "scp", "-q",
        "-i", SSH_KEY,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=20",
        src, dst,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def push(path):
    """Push stats up, pull signups down. Never fatal — next run retries."""
    ok = True
    up = _scp(path, "%s@%s:%s" % (VPS_USER, VPS_HOST, VPS_STATS))
    if up.returncode != 0:
        print("push failed: %s" % up.stderr.strip(), file=sys.stderr)
        ok = False

    down = _scp("%s@%s:%s" % (VPS_USER, VPS_HOST, VPS_SIGNUPS), SIGNUPS_LOCAL)
    if down.returncode != 0:
        print("signup pull failed: %s" % down.stderr.strip(), file=sys.stderr)
        ok = False
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="write %s" % LOCAL_OUT)
    ap.add_argument("--push", action="store_true", help="scp to the VPS (implies --write)")
    args = ap.parse_args()

    try:
        stats = collect()
    except Exception as exc:  # noqa: BLE001 — a cron job must not stack-trace
        print("collect failed: %s" % exc, file=sys.stderr)
        return 1

    payload = json.dumps(stats, indent=2)
    print(payload)

    if args.write or args.push:
        os.makedirs(os.path.dirname(LOCAL_OUT), exist_ok=True)
        with open(LOCAL_OUT, "w") as fh:
            fh.write(payload + "\n")

    if args.push and not push(LOCAL_OUT):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
