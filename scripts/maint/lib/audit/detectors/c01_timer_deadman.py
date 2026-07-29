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
"""
from __future__ import annotations

import ast
import re
from typing import Dict, List, Set

from ..model import FINDING, OK, DetectorResult, Verdict

NAME = "c01_timer_deadman"
CLASS_ID = "C-01"
BOUNDARY = "tracked scripts/*/systemd/*.timer UNION manifest/jobs.yaml:jobs"

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


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    timers = repo.tracked_matching([TIMER_GLOB])
    jobs: Dict[str, dict] = (ctx.ledgers.jobs.get("jobs") or {})
    monitors = _known_monitors(repo)

    by_timer_path = {}
    for job_id, job in sorted(jobs.items()):
        by_timer_path[(job or {}).get("timer")] = (job_id, job or {})

    verdicts: List[Verdict] = []
    monitored = adjudicated = open_gaps = 0

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
        monitor = job.get("kuma_monitor")
        reason = (job.get("no_monitor_reason") or "").strip()

        if monitor:
            if monitor not in monitors:
                verdicts.append(Verdict(
                    instance_id=iid, kind="unknown-monitor", status=FINDING,
                    path=path, lineno=0,
                    detail="kuma_monitor '" + monitor + "' resolves against neither apps.yaml nor "
                           "STANDALONE_SELF_PUSH_MONITORS",
                ))
                continue
            monitored += 1
            verdicts.append(Verdict(
                instance_id=iid, kind="monitored", status=OK, path=path,
                detail="dead-manned by " + monitor,
            ))
            continue

        if len(reason) < MIN_REASON_CHARS:
            verdicts.append(Verdict(
                instance_id=iid, kind="incomplete-adjudication", status=FINDING,
                path=path, lineno=0,
                detail="no kuma_monitor and no_monitor_reason is " + str(len(reason))
                       + " chars (need >= " + str(MIN_REASON_CHARS) + ")",
            ))
            continue
        if not job.get("adjudicated") or not job.get("owner"):
            verdicts.append(Verdict(
                instance_id=iid, kind="incomplete-adjudication", status=FINDING,
                path=path, lineno=0,
                detail="no_monitor_reason present but adjudicated date and/or owner missing",
            ))
            continue

        adjudicated += 1
        if job.get("open_gap"):
            open_gaps += 1
            # Reported EVERY run, on purpose. `open_gap: true` moves a gap from
            # unknown to known-dated-owned; it does not make it disappear.
            verdicts.append(Verdict(
                instance_id=iid, kind="open-gap", status=FINDING, path=path, lineno=0,
                detail="adjudicated as a KNOWN, UNCLOSED dead-man gap (owner "
                       + str(job.get("owner")) + ")",
            ))
        else:
            verdicts.append(Verdict(
                instance_id=iid, kind="no-monitor-accepted", status=OK, path=path,
                detail="adjudicated: no dead-man needed",
            ))

    # The other direction: a jobs.yaml entry whose timer does not exist means
    # the ledger is describing a system that is gone.
    timer_set = set(timers)
    for job_id, job in sorted(jobs.items()):
        tpath = (job or {}).get("timer")
        if tpath not in timer_set:
            verdicts.append(Verdict(
                instance_id="jobs.yaml:" + job_id, kind="orphan-job-entry", status=FINDING,
                path="manifest/jobs.yaml", lineno=0,
                detail="job '" + job_id + "' points at " + str(tpath) + " which is not a tracked timer",
            ))

    return DetectorResult(
        boundary_name=BOUNDARY,
        boundary_size=len(timers),
        verdicts=verdicts,
        metrics={
            "timers": len(timers),
            "jobs_declared": len(jobs),
            "monitored": monitored,
            "adjudicated_no_monitor": adjudicated,
            "open_gaps": open_gaps,
        },
    )
