#!/usr/bin/env python3
"""Emit library stats for the qflix.starhold.dev invite page.

Only stdlib — deliberately. This has to keep working on the box's Python 3.9
without a venv, because the one job it has is to not silently stop.

Usage:
    qflix-stats.py                 # collect, print JSON, change nothing
    qflix-stats.py --write         # also write LOCAL_OUT

Delivery is a PULL, not a push: the VPS firewalls SSH to its tailnet and this
box is not on it. The VPS invokes this script hourly over a forced-command key
(see qflix-stats-serve.sh) and reads stdout.
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

# Shallow checkout of the public repo, refreshed on every run. The manifest and
# the test suite are the source of truth for "how many apps" and "how many
# tests", and neither is otherwise present on this box — without this the page
# would be quoting numbers somebody had to remember to update by hand.
SRC_DIR = os.path.join(HOME, ".opt", "qflix-src")
SRC_URL = "https://github.com/Quadstronaut/QFlix.git"


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
        parts = line.split()
        if len(parts) < 2:
            continue
        # The trailing "*" appears once you are over the soft limit — the exact
        # moment this figure matters most. A "$"-anchored pattern without it
        # made the disk tile vanish precisely when the box was filling up.
        #
        # A bare number means 1K blocks, not bytes; defaulting to bytes
        # under-reported by 1024x and rendered "0 GB".
        m = re.match(r"^([0-9.]+)([KMGT])?\*?$", parts[1])
        if not m:
            continue
        return int(float(m.group(1)) * _QUOTA_UNITS.get(m.group(2) or "K", 1024))
    return None


def canary_count():
    """How many end-to-end canaries are installed.

    Counted from disk rather than hardcoded on the page, so the figure cannot
    drift out of date as canaries get added.
    """
    d = os.path.join(HOME, "scripts", "canaries")
    try:
        return len([f for f in os.listdir(d) if f.endswith(".sh")])
    except OSError:
        return None


def _git(*args, **kw):
    return subprocess.run(
        ["git"] + list(args), capture_output=True, text=True, timeout=kw.get("timeout", 90)
    )


def refresh_src():
    """Clone or update the shallow source checkout. Never fatal.

    A failure here just means the counts fall back to whatever the last good
    checkout says, which is far better than the page losing them entirely.
    """
    try:
        if not os.path.isdir(os.path.join(SRC_DIR, ".git")):
            os.makedirs(os.path.dirname(SRC_DIR), exist_ok=True)
            r = _git("clone", "--depth", "1", SRC_URL, SRC_DIR, timeout=180)
            return r.returncode == 0
        if _git("-C", SRC_DIR, "fetch", "--depth", "1", "origin", "master").returncode != 0:
            return False
        return _git("-C", SRC_DIR, "reset", "--hard", "origin/master").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_TEST_DEF = re.compile(r"^\s*def (test_\w+)", re.M)


def test_count():
    """Automated tests in the suite, counted from the checkout."""
    root = os.path.join(SRC_DIR, "tests")
    if not os.path.isdir(root):
        return None
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                    total += len(_TEST_DEF.findall(fh.read()))
            except OSError:
                continue
    return total or None


def app_count():
    """Apps declared in manifest/apps.yaml, excluding the canaries block.

    Deliberately not a count of ~/.apps: that directory holds only the
    UCC-installed subset and would undercount the systemd, cron and library
    entries that the manifest treats as first-class apps.
    """
    path = os.path.join(SRC_DIR, "manifest", "apps.yaml")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            body = fh.read().split("\ncanaries:")[0]
    except OSError:
        return None
    return len(re.findall(r"^  ([a-z0-9][\w.-]*):\s*$", body, re.M)) or None


def collect():
    libs = section_map()

    # Fail loudly rather than emitting a valid, fresh, all-zero payload. A
    # renamed Plex library would otherwise blank the proof wall while this
    # script still exited 0 — the page would look fine and say nothing.
    if not [t for t in MOVIE_LIBS + SHOW_LIBS if t in libs]:
        raise RuntimeError("no configured libraries found; server has: %s" % sorted(libs))

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

    canaries = canary_count()
    if canaries:
        stats["canaries"] = canaries

    refresh_src()
    tests = test_count()
    if tests:
        stats["tests"] = tests
    apps = app_count()
    if apps:
        stats["apps"] = apps
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="write %s" % LOCAL_OUT)
    args = ap.parse_args()

    try:
        stats = collect()
    except Exception as exc:  # noqa: BLE001 — a cron job must not stack-trace
        print("collect failed: %s" % exc, file=sys.stderr)
        return 1

    payload = json.dumps(stats, indent=2)
    print(payload)

    if args.write:
        os.makedirs(os.path.dirname(LOCAL_OUT), exist_ok=True)
        with open(LOCAL_OUT, "w") as fh:
            fh.write(payload + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
