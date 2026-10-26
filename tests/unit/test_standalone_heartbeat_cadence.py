"""QFLX-81: a standalone self-pusher's dead-man must not be wider than its job.

STANDALONE_SELF_PUSH_HEARTBEATS exists so that a non-daily job is not born with
bootstrap's 25h daily window. It listed only the Entitlement Gate; the hourly
Collect and the 6-hourly Audit Live were missing, so either could be dead for a
day under a green monitor. This test derives the cadence from the timer the
job actually runs on (manifest/jobs.yaml -> timer -> OnCalendar) and fails if a
non-daily job has no entry, or if an entry is not tighter than a day.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from lib.kuma import STANDALONE_SELF_PUSH_HEARTBEATS, STANDALONE_SELF_PUSH_MONITORS

REPO = Path(__file__).resolve().parents[2]
_DAILY = re.compile(r"^\*-\*-\* \d{2}:\d{2}(:\d{2})? UTC$")


def _standalone_jobs():
    jobs = yaml.safe_load((REPO / "manifest" / "jobs.yaml").read_text(encoding="utf-8"))
    jobs = jobs.get("jobs", jobs)
    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        mon = job.get("kuma_monitor")
        timer = job.get("timer")
        if mon in STANDALONE_SELF_PUSH_MONITORS and timer:
            yield name, mon, REPO / timer


def _is_daily(timer: Path) -> bool:
    lines = [ln.strip() for ln in timer.read_text(encoding="utf-8").splitlines()]
    cal = [ln.split("=", 1)[1].strip() for ln in lines if ln.startswith("OnCalendar=")]
    relative = [ln for ln in lines if ln.startswith(("OnUnitActiveSec=", "OnUnitInactiveSec="))]
    return len(cal) == 1 and not relative and bool(_DAILY.match(cal[0]))


def test_the_derivation_sees_the_jobs_it_must():
    found = {mon for _, mon, _ in _standalone_jobs()}
    # The two monitors QFLX-81 was about, plus the one that was already right.
    assert {"QFlix Collect (seedbox)", "QFlix Audit Live", "QFlix Entitlement Gate"} <= found


def test_every_non_daily_standalone_job_has_a_tight_heartbeat():
    offenders = []
    for job, mon, timer in _standalone_jobs():
        if _is_daily(timer):
            continue
        hb = STANDALONE_SELF_PUSH_HEARTBEATS.get(mon)
        if hb is None or hb >= 86400:
            offenders.append(f"{job} -> {mon!r}: non-daily timer, heartbeat={hb}")
    assert not offenders, "\n".join(offenders)
