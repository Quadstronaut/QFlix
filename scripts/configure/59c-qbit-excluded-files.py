#!/usr/bin/env python3
"""Never let qBittorrent download disc images or executables. Idempotent -
safe to re-run. Sibling of 59-brdisk-block.py (the grab-time disc guard on the
arrs); this is the DOWNLOAD-time guard on the client, which no release name
can talk its way past.

WHY THIS EXISTS - the concrete failure it prevents (QFLX-71)
------------------------------------------------------------
2026-10-22 Sonarr grabbed, from LimeTorrents via Prowlarr, three "episodes":

    Avatar Seven Havens S01E07 What Lies Beneath 1080p WEB H264 EZTV
    Marshals S02E04 Blast Radius 1080p WEB H264 EZTV
    South Park S29E04 1080p WEB H264 FLUX

Each torrent held ONE file named "<title>.iso". All three were byte-identical
(1,105,495,820 B, same sha1) and `file` called them "PE32+ executable for MS
Windows" - malware bait wearing an episode's name. The same three episodes had
already been faked twice before (Knaben 10-19, LimeTorrents "...7z" 10-22), so
blocklisting one release does not end it; the next fake is one search away.

The release NAMES carry no tell (no "iso", no "exe"), so no Sonarr release
profile could have rejected them. Only the file list inside the torrent says
what it is, and only the download client sees that.

Then it cost ~5 hours of pages. Unpackerr v0.16.1 tried to extract the fake
.iso and hit a nil-pointer panic in golift.io/xtractr v0.6.0 iso.go:45
(progressTracker.done on a non-ISO input) -> exit 2 -> systemd StartLimit ->
pusher DOWN -> auto-heal restart -> panic again, every ~8 minutes.

WHAT IT SETS
------------
qBittorrent `excluded_file_names_enabled=true` and `excluded_file_names` =
the operator's existing patterns UNION the required set below. A matching file
is set to "do not download"; a torrent made only of such files completes empty,
Sonarr finds nothing to import, and arr-unstick blocklists it and re-searches.

Disc images are not wanted ANYWHERE on this stack: BR-DISK is blocked at grab
(59-brdisk-block.py) and Tdarr's containerFilter excludes iso (50b). So
excluding *.iso at the client contradicts no policy.

Existing patterns are kept, never overwritten - the merge only ever adds.

Run on the seedbox (reads ~/secrets/qbittorrent.{port,user,password}). Prints
what changed; a re-run on an armed client prints "unchanged" and exits 0.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# qBit matches these as case-insensitive wildcards against each file NAME.
# Disc images (the 2026-10-22 vector) + Windows executables/scripts/shortcuts
# (what fake releases actually ship). None of these is ever a library file.
#
# *.7z (QFLX-72, 2026-10-23): the SAME three episodes came back as single-file
# "<title>.7z" torrents from LimeTorrents. Each held one Windows PE executable
# named after a DIFFERENT show (Abbott Elementary, Georgie and Mandy), and
# unpackerr extracted it onto disk before Sonarr refused the .7z. Scene and
# p2p video releases ship rar (or nothing); a single 7z is a wrapper for a fake.
REQUIRED_PATTERNS = (
    "*.iso",
    "*.7z",
    "*.img",
    "*.exe",
    "*.scr",
    "*.msi",
    "*.bat",
    "*.cmd",
    "*.lnk",
    "*.vbs",
    "*.ps1",
)

TIMEOUT = 15


def split_patterns(raw) -> list:
    """qBit stores the list newline-separated; tolerate CRLF and blanks."""
    return [p.strip() for p in str(raw or "").replace("\r", "\n").split("\n")
            if p.strip()]


def merge_patterns(existing_raw, required=REQUIRED_PATTERNS) -> tuple:
    """Return (merged_list, added_list). Existing order is preserved and the
    comparison is case-insensitive (qBit matches case-insensitively, so
    "*.ISO" already covers "*.iso" and must not be duplicated)."""
    merged = split_patterns(existing_raw)
    have = {p.lower() for p in merged}
    added = []
    for pat in required:
        if pat.lower() not in have:
            merged.append(pat)
            added.append(pat)
            have.add(pat.lower())
    return merged, added


def plan(prefs: dict, required=REQUIRED_PATTERNS):
    """Return the setPreferences payload needed, or None when already armed."""
    merged, added = merge_patterns(prefs.get("excluded_file_names"), required)
    enabled = bool(prefs.get("excluded_file_names_enabled"))
    if enabled and not added:
        return None
    return {
        "excluded_file_names_enabled": True,
        "excluded_file_names": "\n".join(merged),
    }


def missing(prefs: dict, required=REQUIRED_PATTERNS) -> list:
    """What the guard is NOT enforcing right now ([] = armed). Used by the
    smoke gate so the check and the fix read one policy."""
    out = []
    if not prefs.get("excluded_file_names_enabled"):
        out.append("excluded_file_names_enabled=false")
    _, added = merge_patterns(prefs.get("excluded_file_names"), required)
    out.extend(added)
    return out


def _secret(name: str, default: str = "") -> str:
    try:
        with open(os.path.expanduser("~/secrets/" + name)) as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


class _Qbit:
    def __init__(self, port: str):
        self.base = "http://127.0.0.1:" + port
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

    def _post(self, path: str, fields: dict) -> str:
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Referer": self.base})
        return self.opener.open(req, timeout=TIMEOUT).read().decode().strip()

    def login(self, user: str, password: str) -> bool:
        return self._post("/api/v2/auth/login",
                          {"username": user, "password": password}) == "Ok."

    def prefs(self) -> dict:
        req = urllib.request.Request(self.base + "/api/v2/app/preferences",
                                     headers={"Referer": self.base})
        return json.loads(self.opener.open(req, timeout=TIMEOUT).read())

    def set_prefs(self, payload: dict) -> None:
        self._post("/api/v2/app/setPreferences", {"json": json.dumps(payload)})


def main() -> int:
    port = _secret("qbittorrent.port")
    if not port:
        print("[qbittorrent] skipped: ~/secrets/qbittorrent.port missing")
        return 1
    q = _Qbit(port)
    try:
        if not q.login(_secret("qbittorrent.user", "quadstronaut"),
                       _secret("qbittorrent.password")):
            print("[qbittorrent] login refused")
            return 1
        payload = plan(q.prefs())
        if payload is None:
            print("[qbittorrent] excluded file names unchanged (armed)")
            return 0
        q.set_prefs(payload)
        # Re-read: qBit answers 200 to setPreferences even for keys it ignores.
        left = missing(q.prefs())
    except (urllib.error.URLError, ValueError) as exc:
        print("[qbittorrent] error: " + str(exc))
        return 1
    if left:
        print("[qbittorrent] set but NOT enforced after re-read: " + ", ".join(left))
        return 1
    print("[qbittorrent] excluded file names armed: "
          + payload["excluded_file_names"].replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
