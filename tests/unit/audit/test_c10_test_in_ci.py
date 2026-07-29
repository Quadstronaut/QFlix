"""C-10 — the check that would have caught the qflix-rea.ps1 gap.

At HEAD 06d4226: 85 Test-Case blocks against the ALERTING LAYER, dot-sourcing a
file .gitignore keeps out of git, executed by ZERO CI jobs. Both halves of that
sentence are now assertions.
"""
from __future__ import annotations

import copy

import yaml

from lib.audit.detectors import c10_test_in_ci as det
from lib.audit.model import FINDING, OK


def _ctx_with_scope(ctx, scope):
    class _L:
        pass
    L = _L()
    for attr in ("scope", "classes", "decommissioned", "jobs", "rea"):
        setattr(L, attr, getattr(ctx.ledgers, attr))
    L.scope = scope

    class _C:
        pass
    c = _C()
    c.repo = ctx.repo
    c.ledgers = L
    return c


def test_every_tracked_test_file_is_executed_by_ci(ctx):
    result = det.detect(ctx)
    orphans = [v for v in result.verdicts if v.kind == "test-not-in-ci"]
    assert orphans == [], "tests in no CI job: " + str([v.path for v in orphans])
    assert result.metrics["test_files"] == result.metrics["test_files_in_ci"]
    assert result.metrics["test_files"] > 50


def test_the_powershell_suite_is_enumerated_and_covered(ctx):
    result = det.detect(ctx)
    hit = [v for v in result.verdicts
           if v.instance_id == "tests/local-llm/test-qflix-rea.ps1:ci"]
    assert len(hit) == 1
    assert hit[0].status == OK, "the alerting layer's test suite is out of CI again"


def test_removing_the_pwsh_job_makes_the_check_fail(ctx):
    """AC-5's negative. Delete the pwsh job from the execution map and the
    PowerShell suite becomes an orphan — the map cannot lie on its own."""
    scope = copy.deepcopy(ctx.ledgers.scope)
    scope["ci_execution"]["jobs"] = [
        j for j in scope["ci_execution"]["jobs"] if j["job"] != "pwsh"
    ]
    result = det.detect(_ctx_with_scope(ctx, scope))
    orphans = {v.path for v in result.verdicts if v.kind == "test-not-in-ci"}
    assert "tests/local-llm/test-qflix-rea.ps1" in orphans
    assert "tests/local-llm/test-rea-noise-classes.ps1" in orphans


def test_a_declared_job_that_is_not_in_the_workflow_is_a_finding(ctx):
    scope = copy.deepcopy(ctx.ledgers.scope)
    scope["ci_execution"]["jobs"].append(
        {"job": "imaginary", "must_contain": "nope", "executes": ["**"]})
    result = det.detect(_ctx_with_scope(ctx, scope))
    assert any(v.kind == "ci-job-missing" for v in result.verdicts)


def test_a_job_that_stopped_running_its_command_is_a_finding(ctx):
    """The map must describe the REAL workflow. Renaming the command inside a
    job would otherwise leave a map that claims coverage it lost."""
    scope = copy.deepcopy(ctx.ledgers.scope)
    for j in scope["ci_execution"]["jobs"]:
        if j["job"] == "pytest":
            j["must_contain"] = "bash tests/run-something-else.sh"
    result = det.detect(_ctx_with_scope(ctx, scope))
    assert any(v.kind == "ci-command-missing" for v in result.verdicts)


def test_untracked_subject_must_be_registered_in_s2(ctx):
    result = det.detect(ctx)
    hit = [v for v in result.verdicts
           if v.instance_id.endswith("->scripts/local-llm/qflix-rea.ps1")]
    assert len(hit) == 1
    # Either it is tracked (it is not, by design) or S2-registered with an owner.
    assert hit[0].kind in ("subject-tracked", "subject-s2-registered")
    assert hit[0].status == OK


def test_deleting_the_s2_entry_makes_the_check_fail(ctx):
    """AC-6's negative. Without the S2 registration the alerting layer's
    subject is invisible to CI and nothing says so."""
    scope = copy.deepcopy(ctx.ledgers.scope)
    scope["surfaces"]["S2"]["members"] = [
        m for m in scope["surfaces"]["S2"]["members"]
        if m["path"] != "scripts/local-llm/qflix-rea.ps1"
    ]
    result = det.detect(_ctx_with_scope(ctx, scope))
    bad = [v for v in result.verdicts if v.kind == "subject-untracked"]
    assert any("qflix-rea.ps1" in v.instance_id for v in bad)


def test_s2_entry_without_a_reason_does_not_count(ctx):
    """An S2 registration is an adjudication. A bare path with no reason and no
    owner is a hole with a label on it."""
    scope = copy.deepcopy(ctx.ledgers.scope)
    for m in scope["surfaces"]["S2"]["members"]:
        if m["path"] == "scripts/local-llm/qflix-rea.ps1":
            m["residual"] = ""
    result = det.detect(_ctx_with_scope(ctx, scope))
    assert any(v.kind == "subject-untracked" and "qflix-rea" in v.instance_id
               for v in result.verdicts)


def test_python_subjects_resolve_to_tracked_files(ctx, ledgers):
    """Every subject is tracked, S2-registered, or covered by a WAIVER that
    names it. The detector reports raw; waiving is the engine's job, so the
    residue is checked against the ledger here rather than assumed away."""
    result = det.detect(ctx)
    waived_ids = {
        w["match"].get("instance_id")
        for cls in ledgers.classes if cls.id == "C-10"
        for w in cls.waivers
    }
    untracked = [v for v in result.verdicts
                 if v.kind == "subject-untracked" and v.instance_id not in waived_ids]
    assert untracked == [], [v.instance_id for v in untracked]
    assert result.metrics["subjects_resolved"] > 20
    assert result.metrics["subjects_unresolved"] == 0


def test_the_only_waived_subject_is_a_deliberate_negative_fixture(ledgers):
    """Guard against the waiver list quietly growing. Every C-10 waiver has to
    be re-read by a human, and there should be exactly one."""
    c10 = next(c for c in ledgers.classes if c.id == "C-10")
    assert len(c10.waivers) == 1
    w = c10.waivers[0]
    assert "does-not-exist" in w["match"]["instance_id"]
    assert "NEGATIVE fixture" in w["reason"]


def test_workflow_file_really_contains_a_pwsh_job(repo):
    """Belt and braces: read the workflow directly, not through the map."""
    wf = yaml.safe_load(repo.read(".github/workflows/tests.yml"))
    assert "pwsh" in wf["jobs"]
    runs = "\n".join(str(s.get("run", "")) for s in wf["jobs"]["pwsh"]["steps"])
    assert "tests/local-llm/test-qflix-rea.ps1" in runs
