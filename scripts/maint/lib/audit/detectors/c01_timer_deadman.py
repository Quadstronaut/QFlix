"""C-01 timer-without-deadman.

A scheduled job that stops running and pages nobody is the cheapest possible
way to lose a system quietly. This detector enumerates EVERY tracked timer unit
and demands a written answer for each: which Kuma monitor goes red, or why none
is needed.

The class exists because of the enumeration-boundary bug in the Stage-0 spec:
manitoba-maint-flaresolverr-canary.timer is on disk but absent from
manifest/apps.yaml:canaries, so whether an audit "saw" it depended on which
file that run happened to open. The boundary here is the UNION of both sides,
so neither can hide an entry from the other.

2026-08-06 - THE LEDGER COULD NOT EXPRESS UNITS THE REPO DOES NOT OWN.
The `timer:` field had to resolve to a repo-tracked path, so an
installer-GENERATED unit (written by a configure-script heredoc, never a file
in git) was not merely unmonitored, it was INEXPRESSIBLE: declaring one raised
`orphan-job-entry`, so the ledger actively punished honesty. Seven such timers
were live on the box and invisible to this detector AND to timer-liveness.sh,
which derives its unit list from the same `timer:` paths - buildarr, kometa,
logrotate, recyclarr, tdarr-node-pause, tdarr-node-resume, upgradinatorr.
That is a vocabulary bug in the dead-man, not seven independent misses.

2026-08-07 - AND IT COULD NOT EXPRESS CRONTAB AT ALL.
The `unit:` fix above closed the installer-generated systemd plane but left a
THIRD one entirely outside the boundary: the user crontab, 10 lines of it,
including `kill_stream.sh --max 4` - member-facing enforcement of the per-member
concurrent-stream cap. A crontab line has no unit name AND no file in git, so
neither `timer:` nor `unit:` can name it. Every cron job on this box was
therefore unadjudicated and unadjudicatABLE.

THE THREE PLANES, all adjudicated through one helper:
  timer:  repo-tracked .timer path, cross-checked against `git ls-files`
  unit:   installer-GENERATED systemd timer (heredoc, no file in git)
  cron:   a crontab line, identified by a command substring
Exactly ONE per entry. Zero leaves nothing to adjudicate; two is ambiguous about
which mechanism runs it, and would let a cron entry borrow a timer's
tracked-path cross-check.

HONEST LIMIT: this detector is OFFLINE, so `unit:` and `cron:` entries are
enumerated from the declaration alone. Nothing here can see an installer unit or
a crontab line that exists on the box and was never declared. That reverse
direction is live-only: timer-liveness.sh owns it for systemd, cron-liveness.sh
for crontab - the same offline-audits-SOURCE / live-audits-RUNNING split the
rest of the regime uses.

HONEST LIMIT: this detector is OFFLINE, so `unit:` entries are enumerated from
the declaration alone. Nothing here can see an installer unit that exists on
the box and was never declared. That reverse direction is live-only and belongs
to timer-liveness.sh, which runs on the box - the same offline-audits-SOURCE /
live-audits-RUNNING split the rest of the regime already uses.
"""
from __future__ import annotations

import ast
import re
from typing import Dict, List, Set

from ..model import FINDING, OK, DetectorResult, Verdict

NAME = "c01_timer_deadman"
CLASS_ID = "C-01"
BOUNDARY = ("tracked scripts/*/systemd/*.timer UNION manifest/jobs.yaml:jobs "
            "(incl. installer-generated units via `unit:` and crontab lines via `cron:`)")

TIMER_GLOB = "scripts/*/systemd/*.timer"
MIN_REASON_CHARS = 40


def _known_monitors(repo) -> Set[str]:
    """Every monitor name that can legitimately back a job.

    Read from the SAME two places the drift audit reads (manifest/apps.yaml and
    lib/kuma.py's STANDALONE_SELF_PUSH_MONITORS) so a typo in jobs.yaml cannot
    resolve against a monitor that does not exist. lib/kuma.py is parsed with
    ast rather than imported: importing it would drag `requests` into a
    detector that must stay pure-stdlib+pyyaml and offline.
    """
    import yaml
    names: Set[str] = {"Manitoba Pusher", "QFlix Fleet"}
    apps = yaml.safe_load(repo.read("manifest/apps.yaml")) or {}
    for section in ("apps", "canaries"):
        for entry in (apps.get(section) or {}).values():
            mon = (entry or {}).get("kuma_monitor")
            if mon:
                names.add(mon)
    for mon in apps.get("kuma_external_monitors") or []:
        names.add(mon)

    kuma_src = repo.read_optional("scripts/maint/lib/kuma.py")
    if kuma_src:
        tree = ast.parse(kuma_src)
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "STANDALONE_SELF_PUSH_MONITORS":
                    try:
                        names.update(ast.literal_eval(node.value).keys())
                    except (ValueError, SyntaxError):
                        pass
    return names


def _adjudicate(job: dict, iid: str, path: str, monitors: Set[str]) -> tuple:
    """The written-answer test, shared by repo-tracked timers and `unit:` entries.

    Returns (verdict, bucket) where bucket is one of "monitored",
    "adjudicated", "open_gap" or None (a finding). Extracted verbatim from the
    original inline block so `unit:` entries are held to the IDENTICAL standard
    - a second, laxer copy of this logic would be exactly the two-policy-surface
    defect this repo keeps getting bitten by.
    """
    monitor = job.get("kuma_monitor")
    reason = (job.get("no_monitor_reason") or "").strip()

    if monitor:
        if monitor not in monitors:
            return Verdict(
                instance_id=iid, kind="unknown-monitor", status=FINDING,
                path=path, lineno=0,
                detail="kuma_monitor '" + monitor + "' resolves against neither apps.yaml nor "
                       "STANDALONE_SELF_PUSH_MONITORS",
            ), None
        return Verdict(
            instance_id=iid, kind="monitored", status=OK, path=path,
            detail="dead-manned by " + monitor,
        ), "monitored"

    if len(reason) < MIN_REASON_CHARS:
        return Verdict(
            instance_id=iid, kind="incomplete-adjudication", status=FINDING,
            path=path, lineno=0,
            detail="no kuma_monitor and no_monitor_reason is " + str(len(reason))
                   + " chars (need >= " + str(MIN_REASON_CHARS) + ")",
        ), None
    if not job.get("adjudicated") or not job.get("owner"):
        return Verdict(
            instance_id=iid, kind="incomplete-adjudication", status=FINDING,
            path=path, lineno=0,
            detail="no_monitor_reason present but adjudicated date and/or owner missing",
        ), None

    if job.get("open_gap"):
        # Reported EVERY run, on purpose. `open_gap: true` moves a gap from
        # unknown to known-dated-owned; it does not make it disappear.
        return Verdict(
            instance_id=iid, kind="open-gap", status=FINDING, path=path, lineno=0,
            detail="adjudicated as a KNOWN, UNCLOSED dead-man gap (owner "
                   + str(job.get("owner")) + ")",
        ), "open_gap"
    return Verdict(
        instance_id=iid, kind="no-monitor-accepted", status=OK, path=path,
        detail="adjudicated: no dead-man needed",
    ), "adjudicated"


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    timers = repo.tracked_matching([TIMER_GLOB])
    jobs: Dict[str, dict] = (ctx.ledgers.jobs.get("jobs") or {})
    monitors = _known_monitors(repo)

    # Only `timer:` entries index by path. A `unit:` entry has no repo file by
    # definition, so folding it in here would make it look like a path clash.
    by_timer_path = {}
    for job_id, job in sorted(jobs.items()):
        tpath = (job or {}).get("timer")
        if tpath:
            by_timer_path[tpath] = (job_id, job or {})

    verdicts: List[Verdict] = []
    monitored = adjudicated = open_gaps = 0
    external_units = 0
    cron_jobs = 0

    for path in timers:
        unit = path.rsplit("/", 1)[-1][: -len(".timer")]
        iid = path + ":timer"
        entry = by_timer_path.get(path)
        # Fall back to matching by job key so a moved-but-renamed timer is
        # reported as a path mismatch rather than silently as "no entry".
        if entry is None and unit in jobs:
            verdicts.append(Verdict(
                instance_id=iid, kind="incomplete-adjudication", status=FINDING,
                path=path, lineno=0,
                detail="jobs.yaml entry '" + unit + "' exists but its timer: path does not point here",
            ))
            continue
        if entry is None:
            verdicts.append(Verdict(
                instance_id=iid, kind="unadjudicated-timer", status=FINDING,
                path=path, lineno=0,
                detail="no manifest/jobs.yaml entry: nothing declares what notices when this stops",
            ))
            continue

        job_id, job = entry
        verdict, bucket = _adjudicate(job, iid, path, monitors)
        verdicts.append(verdict)
        if bucket == "monitored":
            monitored += 1
        elif bucket == "adjudicated":
            adjudicated += 1
        elif bucket == "open_gap":
            adjudicated += 1
            open_gaps += 1

    # The two NON-repo scheduling planes, adjudicated through the SAME helper as
    # repo-tracked timers. One loop over both rather than a copy per plane: a
    # third near-identical block is how the standards drift apart, which is the
    # defect this file keeps re-learning.
    #
    #   unit:  an installer-GENERATED systemd timer (heredoc, no file in git)
    #   cron:  a CRONTAB line, which has no unit name at all
    #
    # Neither is cross-checked against the tracked-file set, because neither has
    # a file by construction.
    for plane in ("unit", "cron"):
        for job_id, job in sorted(jobs.items()):
            job = job or {}
            if not job.get(plane) or job.get("timer"):
                continue
            if plane == "unit":
                external_units += 1
            else:
                cron_jobs += 1
            iid = "jobs.yaml:" + job_id + ":" + plane
            verdict, bucket = _adjudicate(job, iid, "manifest/jobs.yaml", monitors)
            verdicts.append(verdict)
            if bucket == "monitored":
                monitored += 1
            elif bucket == "adjudicated":
                adjudicated += 1
            elif bucket == "open_gap":
                adjudicated += 1
                open_gaps += 1

    # The other direction: a jobs.yaml entry whose timer does not exist means
    # the ledger is describing a system that is gone. Scoped to entries that
    # actually claim a repo path - a `unit:` entry has no file by construction,
    # and orphaning it is what made installer units inexpressible.
    timer_set = set(timers)
    for job_id, job in sorted(jobs.items()):
        job = job or {}
        tpath = job.get("timer")
        declared = [p for p in ("timer", "unit", "cron") if job.get(p)]
        # EXACTLY ONE scheduling plane per entry. Two would be ambiguous about
        # which mechanism actually runs it (and would let a cron entry borrow a
        # timer's tracked-path cross-check); zero leaves nothing to adjudicate.
        if len(declared) > 1:
            verdicts.append(Verdict(
                instance_id="jobs.yaml:" + job_id, kind="incomplete-adjudication", status=FINDING,
                path="manifest/jobs.yaml", lineno=0,
                detail="job '" + job_id + "' declares " + " and ".join(declared)
                       + " - exactly one scheduling plane per entry",
            ))
            continue
        if not declared:
            verdicts.append(Verdict(
                instance_id="jobs.yaml:" + job_id, kind="incomplete-adjudication", status=FINDING,
                path="manifest/jobs.yaml", lineno=0,
                detail="job '" + job_id + "' declares no timer:, unit: or cron: - nothing to adjudicate",
            ))
            continue
        if tpath and tpath not in timer_set:
            verdicts.append(Verdict(
                instance_id="jobs.yaml:" + job_id, kind="orphan-job-entry", status=FINDING,
                path="manifest/jobs.yaml", lineno=0,
                detail="job '" + job_id + "' points at " + str(tpath) + " which is not a tracked timer",
            ))

    return DetectorResult(
        boundary_name=BOUNDARY,
        boundary_size=len(timers) + external_units + cron_jobs,
        verdicts=verdicts,
        metrics={
            "timers": len(timers),
            "external_units": external_units,
            "cron_jobs": cron_jobs,
            "jobs_declared": len(jobs),
            "monitored": monitored,
            "adjudicated_no_monitor": adjudicated,
            "open_gaps": open_gaps,
        },
    )
