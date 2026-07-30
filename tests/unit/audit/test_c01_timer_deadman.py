"""C-01 — every timer gets a verdict, and the ledger is honest about the gaps.

The load-bearing assertion is TOTALITY: the number of verdicts equals the
number of timers on disk, re-globbed here rather than copied from a spec. A
detector that skips one timer is exactly the failure this class exists to
prevent, one level up.
"""
from __future__ import annotations

import yaml

from lib.audit.detectors import c01_timer_deadman as det
from lib.audit.model import FINDING, OK


def _timers(repo):
    """Independent re-derivation of the boundary. Deliberately not the
    detector's own helper — a differential test proves agreement, a shared
    helper only proves the helper runs twice."""
    return sorted(
        p for p in repo.tracked
        if p.endswith(".timer") and p.startswith("scripts/")
        and p.count("/") == 3 and "/systemd/" in p
    )


def test_enumerates_every_timer_with_zero_omissions(ctx, repo):
    result = det.detect(ctx)
    timers = _timers(repo)
    assert timers, "no timers found — the boundary re-derivation is broken"
    assert result.boundary_size == len(timers)
    timer_verdicts = [v for v in result.verdicts if v.path.endswith(".timer")]
    assert len(timer_verdicts) == len(timers), (
        "detector emitted " + str(len(timer_verdicts)) + " timer verdicts for "
        + str(len(timers)) + " timers — an omission"
    )
    assert sorted(v.path for v in timer_verdicts) == timers


def test_every_timer_is_adjudicated_one_way_or_the_other(ctx):
    """The enforced part of the class: no timer may be silent about its
    dead-man. `open-gap` findings are advisory and expected."""
    result = det.detect(ctx)
    hard = [v for v in result.verdicts
            if v.status == FINDING and v.kind != "open-gap"]
    assert hard == [], "unadjudicated timers: " + str([v.instance_id for v in hard])


def test_declared_monitors_all_resolve(ctx, repo):
    jobs = yaml.safe_load(repo.read("manifest/jobs.yaml"))["jobs"]
    known = det._known_monitors(repo)
    for job_id, job in jobs.items():
        mon = job.get("kuma_monitor")
        if mon:
            assert mon in known, "job " + job_id + " names unknown monitor " + repr(mon)


def test_the_spec_named_candidates_are_all_adjudicated(ctx):
    """The six candidates the Stage-0 spec named must each be resolved — either
    monitored, or carrying a written reason. Their VERDICT is asserted, not
    their answer: the point is that a human decided, not which way."""
    result = det.detect(ctx)
    by_path = {v.path: v for v in result.verdicts}
    for unit in ("manitoba-maint-arr-audit", "manitoba-maint-backup-prune",
                 "manitoba-maint-ucc-detect", "manitoba-maint-window",
                 "manitoba-maint-window-watchdog", "qflix-poster-cache-prune"):
        path = "scripts/maint/systemd/" + unit + ".timer"
        assert path in by_path, unit + " was not enumerated"
        assert by_path[path].kind in {"monitored", "no-monitor-accepted", "open-gap"}


def test_flaresolverr_canary_is_the_boundary_bug_and_is_now_visible(ctx):
    """manitoba-maint-flaresolverr-canary.timer is on disk but absent from
    manifest/apps.yaml:canaries — the exact section-1(g) enumeration-boundary
    bug. Whether an audit 'saw' it used to depend on which file it opened.

    What this guards is ENUMERATION, not brokenness. It originally also asserted
    kind == "open-gap" so the gap could not be quietly erased, which was right
    while it was open. It was CLOSED on 2026-07-30 by the timer-liveness canary,
    a generic dead-man over every job in the ledger — a real closure, not an
    erasure, so demanding the finding persist would now be demanding the defect
    persist. The boundary property is what must never regress: this timer has to
    stay visible to the detector even though nothing in apps.yaml:canaries
    mentions it.
    """
    result = det.detect(ctx)
    hit = [v for v in result.verdicts
           if v.path.endswith("manitoba-maint-flaresolverr-canary.timer")]
    assert len(hit) == 1, "the boundary bug is back: this timer is unenumerated"
    assert hit[0].kind != "unknown-timer", (
        "enumerated but unadjudicated — the ledger no longer describes it")


def test_orphan_job_entry_is_a_finding(ctx, repo):
    """A ledger entry pointing at a timer that does not exist means the ledger
    describes a system that is gone."""
    fake = dict(ctx.ledgers.jobs)
    fake["jobs"] = dict(fake["jobs"])
    fake["jobs"]["ghost-job"] = {"timer": "scripts/maint/systemd/ghost.timer",
                                 "kuma_monitor": "Canary Movie"}

    class _L:
        jobs = fake
        decommissioned = ctx.ledgers.decommissioned
        scope = ctx.ledgers.scope
        rea = ctx.ledgers.rea
        classes = ctx.ledgers.classes

    class _C:
        repo = ctx.repo
        ledgers = _L()

    result = det.detect(_C())
    kinds = {v.kind for v in result.verdicts if v.status == FINDING}
    assert "orphan-job-entry" in kinds


def test_unknown_monitor_name_is_a_finding(ctx):
    fake = {"jobs": {"movie": {
        "timer": "scripts/maint/systemd/manitoba-maint-canary-movie.timer",
        "kuma_monitor": "Canary Moovie",  # typo
    }}}

    class _L:
        jobs = fake
        decommissioned = ctx.ledgers.decommissioned
        scope = ctx.ledgers.scope
        rea = ctx.ledgers.rea
        classes = ctx.ledgers.classes

    class _C:
        repo = ctx.repo
        ledgers = _L()

    result = det.detect(_C())
    typo = [v for v in result.verdicts
            if v.path.endswith("canary-movie.timer") and v.status == FINDING]
    assert typo and typo[0].kind == "unknown-monitor"


def test_ok_verdicts_carry_the_monitor_name(ctx):
    result = det.detect(ctx)
    monitored = [v for v in result.verdicts if v.kind == "monitored"]
    assert monitored
    assert all(v.status == OK and "dead-manned by" in v.detail for v in monitored)
