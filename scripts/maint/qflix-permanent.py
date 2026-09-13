#!/usr/bin/env python3
"""qflix-permanent.py — manage the `permanent` tag on Sonarr/Radarr series.

WHY THIS EXISTS
---------------
qflix-reaper deletes a whole SERIES RECORD once its content ages out. For a
show that is still running, that is wrong twice over: the operator loses the
Sonarr entry that was quietly collecting future episodes, and members lose a
show nobody had to re-request. On 2026-09-12 it deleted Futurama seventy
minutes after it was requested; Family Guy and American Dad are already gone
from Sonarr entirely, reaped while parked with zero files waiting for a new
season.

The `permanent` tag exempts the series RECORD. It never exempts the files --
every file still expires 45 days after it became available in Plex, because
that is what members are promised. A permanent show simply keeps its Sonarr
entry at zero files so `monitorNewItems` can keep pulling new episodes.

WHY A TAG AND NOT `monitorNewItems`
-----------------------------------
`monitorNewItems` is already `all` on all 37 series -- it is the default and
discriminates nothing. A tag is explicit operator intent, survives series
refreshes, is visible in the UI, and nothing else sets it. The existing tags on
this box are Seerr requester usernames (quadstronaut, jessirigby,
brintonasylum, cupid_rays180); a series may hold several tags, so `permanent`
coexists with them.

AUTO-RULE
---------
Any series that is NOT ended (`ended=false`) is auto-tagged. An unfinished show
can still surprise members with new episodes, so its record must survive. When
a show ends, the tag is NOT removed automatically -- ending is a fact about the
show, but un-protecting a record is an operator decision, and silently
withdrawing protection is exactly the class of surprise this tool exists to
stop. `--prune-ended` does it explicitly when asked.

READ-ONLY BY DEFAULT. Nothing is written without `--execute`.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

TAG_LABEL = "permanent"
SECRETS = Path.home() / "secrets"
INSTANCES = ("sonarr", "sonarr2", "radarr", "radarr2")
TV = ("sonarr", "sonarr2")


def _secret(name: str, default: str | None = None) -> str | None:
    p = SECRETS / name
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return default


def _base(inst: str):
    port = _secret(inst + ".port")
    key = _secret(inst + ".key")
    urlbase = _secret(inst + ".urlbase", inst)
    if not port or not key:
        return None, None
    return "http://127.0.0.1:%s/%s/api/v3" % (port, urlbase), key


def _req(url: str, key: str, method: str = "GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"X-Api-Key": key,
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as fh:
        body = fh.read().decode(errors="replace")
    return json.loads(body) if body.strip() else None


def ensure_tag(base: str, key: str, execute: bool) -> int | None:
    """Return the id of the `permanent` tag, creating it if needed."""
    for t in _req(base + "/tag", key) or []:
        if str(t.get("label", "")).lower() == TAG_LABEL:
            return t["id"]
    if not execute:
        return None
    created = _req(base + "/tag", key, "POST", {"label": TAG_LABEL})
    return (created or {}).get("id")


def collection(inst: str) -> str:
    return "series" if inst in TV else "movie"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true",
                    help="actually write. Without it nothing is modified.")
    ap.add_argument("--auto", action="store_true",
                    help="tag every series that is NOT ended.")
    ap.add_argument("--set", metavar="TITLE", action="append", default=[],
                    help="tag one title (case-insensitive substring). Repeatable.")
    ap.add_argument("--unset", metavar="TITLE", action="append", default=[],
                    help="remove the tag from one title. Repeatable.")
    ap.add_argument("--prune-ended", action="store_true",
                    help="remove the tag from series that have since ENDED. "
                         "Never automatic -- withdrawing protection is an "
                         "operator decision.")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    args = ap.parse_args()

    if not (args.auto or args.set or args.unset or args.prune_ended):
        args.auto = True  # default action is a read-only audit of the auto-rule

    report = {"tag": TAG_LABEL, "execute": bool(args.execute), "instances": {}}
    rc = 0

    for inst in INSTANCES:
        base, key = _base(inst)
        if not base:
            report["instances"][inst] = {"error": "no port/key secret"}
            rc = max(rc, 2)
            continue
        try:
            # Only create the tag where it can be used. ensure_tag ran
            # unconditionally across all four instances and left dead
            # `permanent` tags behind in radarr/radarr2.
            tag_id = ensure_tag(base, key, args.execute and inst in TV)
            rows = _req("%s/%s" % (base, collection(inst)), key) or []
        except (urllib.error.URLError, OSError, ValueError) as exc:
            report["instances"][inst] = {"error": "unreachable: %s" % exc}
            rc = max(rc, 2)
            continue

        info = {"total": len(rows), "tag_id": tag_id,
                "tagged": [], "would_tag": [], "untagged": [], "would_untag": [],
                "skipped_ended": 0}

        for s in rows:
            title = s.get("title", "")
            tags = list(s.get("tags") or [])
            ended = bool(s.get("ended")) if inst in TV else True
            has = tag_id is not None and tag_id in tags

            want = False
            if args.auto and inst in TV and not ended:
                want = True
            for pat in args.set:
                if pat.lower() in title.lower():
                    want = True
            drop = any(pat.lower() in title.lower() for pat in args.unset)
            if args.prune_ended and inst in TV and ended and has:
                drop = True

            if drop and has:
                # The removal branch MUST be gated on --execute exactly like the
                # add branch below it. It was not: `untagged` was appended
                # unconditionally, so a dry run reported a protection as REMOVED
                # while writing nothing. An operator following the dry-run-first
                # discipline would be told a de-protection succeeded when the
                # series was still tagged. Asymmetry between the two branches of
                # one tool is exactly how that slips through review.
                if args.execute and tag_id is not None:
                    _req("%s/%s/%s" % (base, collection(inst), s["id"]), key, "PUT",
                         dict(s, tags=[t for t in tags if t != tag_id]))
                    info["untagged"].append(title)
                else:
                    info["would_untag"].append(title)
            elif want and not has:
                if args.execute and tag_id is not None:
                    _req("%s/%s/%s" % (base, collection(inst), s["id"]), key, "PUT",
                         dict(s, tags=tags + [tag_id]))
                    info["tagged"].append(title)
                else:
                    info["would_tag"].append(title)
            elif inst in TV and ended:
                info["skipped_ended"] += 1

        report["instances"][inst] = info

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        mode = "EXECUTE" if args.execute else "DRY-RUN (nothing written)"
        print("qflix-permanent — tag '%s' — %s" % (TAG_LABEL, mode))
        for inst, info in report["instances"].items():
            if "error" in info:
                print("  %-9s ERROR %s" % (inst, info["error"]))
                continue
            print("  %-9s total=%-4s tag_id=%-5s tagged=%-3d would_tag=%-3d "
                  "untagged=%-3d would_untag=%-3d ended_skipped=%d"
                  % (inst, info["total"], info["tag_id"], len(info["tagged"]),
                     len(info["would_tag"]), len(info["untagged"]),
                     len(info["would_untag"]), info["skipped_ended"]))
            for t in info["tagged"]:
                print("      + %s" % t)
            for t in info["would_tag"]:
                print("      ~ would tag: %s" % t)
            for t in info["untagged"]:
                print("      - %s" % t)
            for t in info["would_untag"]:
                print("      ~ would untag: %s" % t)
    return rc


if __name__ == "__main__":
    sys.exit(main())
