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


def _plane(repo, name):
    """Entries declared on one NON-repo scheduling plane, re-derived
    independently of the detector. `unit:` = installer-generated systemd timer,
    `cron:` = crontab line. Neither has a file in git."""
    jobs = yaml.safe_load(repo.read("manifest/jobs.yaml"))["jobs"]
    return sorted(k for k, v in jobs.items()
                  if (v or {}).get(name) and not (v or {}).get("timer"))


def _external_units(repo):
    return _plane(repo, "unit")


def _cron_jobs(repo):
    return _plane(repo, "cron")


def test_enumerates_every_timer_with_zero_omissions(ctx, repo):
    result = det.detect(ctx)
    timers = _timers(repo)
    externals = _external_units(repo)
    crons = _cron_jobs(repo)
    assert timers, "no timers found — the boundary re-derivation is broken"
    # The boundary is the UNION of all THREE scheduling planes, asserted as a sum
    # of three INDEPENDENTLY re-derived counts so no plane can silently shrink.
    # Each was added only after it was found missing: before 2026-08-06 seven
    # installer-generated timers sat outside every enumeration, and before
    # 2026-08-07 the entire crontab did - ten lines including the member-facing
    # stream-cap enforcement.
    assert result.boundary_size == len(timers) + len(externals) + len(crons)
    assert result.metrics["external_units"] == len(externals)
    assert result.metrics["cron_jobs"] == len(crons)
    assert crons, "no cron entries — the crontab plane vanished from the ledger"
    timer_verdicts = [v for v in result.verdicts if v.path.endswith(".timer")]
    assert len(timer_verdicts) == len(timers), (
        "detector emitted " + str(len(timer_verdicts)) + " timer verdicts for "
        + str(len(timers)) + " timers — an omission"
    )
    assert sorted(v.path for v in timer_verdicts) == timers
    # Every external unit gets its own verdict too — same totality guarantee.
    ext_verdicts = [v for v in result.verdicts if v.instance_id.endswith(":unit")]
    assert len(ext_verdicts) == len(externals), (
        "detector emitted " + str(len(ext_verdicts)) + " unit verdicts for "
        + str(len(externals)) + " declared installer units — an omission"
    )
    cron_verdicts = [v for v in result.verdicts if v.instance_id.endswith(":cron")]
    assert len(cron_verdicts) == len(crons), (
        "detector emitted " + str(len(cron_verdicts)) + " cron verdicts for "
        + str(len(crons)) + " declared crontab jobs — an omission"
    )


def test_a_unit_entry_is_never_orphaned(ctx, repo):
    """The regression that made installer units inexpressible: a `unit:` entry
    has no file in git by construction, so cross-checking it against the
    tracked-timer set reported it as an orphan and the ledger punished honesty."""
    result = det.detect(ctx)
    externals = set(_external_units(repo))
    orphans = [v.instance_id for v in result.verdicts
               if v.kind == "orphan-job-entry"
               and v.instance_id.split(":")[-1] in externals]
    assert orphans == [], (
        "installer-generated units reported as orphan-job-entry: " + str(orphans))


def test_exactly_one_scheduling_plane_per_entry(ctx, repo):
    """Declaring two planes is ambiguous about which mechanism actually runs the
    job, and would let a cron entry borrow a timer's tracked-path cross-check.
    Declaring none leaves nothing to adjudicate. Guarded so the vocabulary that
    made these planes expressible cannot be used to evade the check."""
    jobs = yaml.safe_load(repo.read("manifest/jobs.yaml"))["jobs"]
    PLANES = ("timer", "unit", "cron")
    bad = {}
    for k, v in jobs.items():
        declared = [p for p in PLANES if (v or {}).get(p)]
        if len(declared) != 1:
            bad[k] = declared
    assert bad == {}, (
        "every job must declare exactly one of timer:/unit:/cron: — offenders "
        "(job -> planes declared): " + str(bad))


def test_every_cron_entry_carries_a_command_substring(repo):
    """`cron:` is matched against `crontab -l` by cron-liveness.sh, so an empty
    or whitespace value would match every line (or none) and the live check
    would silently assert nothing."""
    jobs = yaml.safe_load(repo.read("manifest/jobs.yaml"))["jobs"]
    weak = {k: v.get("cron") for k, v in jobs.items()
            if (v or {}).get("cron") and len(str(v.get("cron")).strip()) < 8}
    assert weak == {}, "cron: values too short to identify a crontab line: " + str(weak)


def test_cron_substrings_are_unique(repo):
    """Two entries matching the same crontab line would let one job's presence
    vouch for another's. The live canary matches by substring, so uniqueness is
    what makes that matching sound."""
    jobs = yaml.safe_load(repo.read("manifest/jobs.yaml"))["jobs"]
    crons = {k: str(v["cron"]) for k, v in jobs.items() if (v or {}).get("cron")}
    collisions = []
    for a, sa in crons.items():
        for b, sb in crons.items():
            if a < b and (sa in sb or sb in sa):
                collisions.append((a, b))
    assert collisions == [], (
        "cron: substrings where one contains the other — a single crontab line "
        "could satisfy both: " + str(collisions))


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
