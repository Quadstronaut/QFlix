"""THE META-CHECK — this is what audits the MONITOR.

Every assertion here is about the AUDITOR, not about the system it audits. A
failure means the auditor is broken, which is a categorically different event
from the auditor finding something: it exits 2, not 1, and the Kuma message
says so.

Every guard below is proved in BOTH directions. A meta-check that has never
been shown to fail is indistinguishable from a meta-check that cannot.
"""
from __future__ import annotations

import copy
import datetime as _dt
import os
import re

import pytest
import yaml

from lib.audit import detectors as _detectors
from lib.audit.engine import run
from lib.audit.ledger import (DefectClass, RESIDUAL_DOC, check_ci_execution,
                              check_class_detector_bijection,
                              check_residual_discipline, check_scope_partition,
                              check_waiver_discipline, load, run_meta_checks)
from lib.audit.model import RegimeError
from lib.audit.repo import Repo

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.fixture(scope="module")
def repo():
    return Repo(REPO_ROOT)


@pytest.fixture(scope="module")
def ledgers(repo):
    return load(repo)


# ---------------------------------------------------------------------------
# The whole regime, on the real checkout
# ---------------------------------------------------------------------------

def test_the_real_repo_passes_every_meta_check(repo, ledgers):
    problems = run_meta_checks(repo, ledgers, _detectors.available())
    assert problems == [], "\n  - " + "\n  - ".join(problems)


def test_audit_runs_clean_of_enforced_findings(repo):
    """CI gate. Advisory backlogs are allowed and expected; an ENFORCED finding
    means a class that was declared closed has reopened."""
    report = run(repo)
    enforced = [f for f in report["findings"] if f["severity"] == "enforced"]
    assert enforced == [], [f["class"] + " " + f["instance_id"] for f in enforced]


# ---------------------------------------------------------------------------
# Bijection: taxonomy <-> detector <-> test  (AC-3)
# ---------------------------------------------------------------------------

def _fake_class(**kw):
    base = dict(id="C-99", title="t", status="advisory", detector="c99_fake",
                test="tests/unit/audit/test_c99_fake.py", enforced_kinds=[],
                advisory_kinds=[], waivers=[], raw={})
    base.update(kw)
    return DefectClass(**base)


def test_every_class_names_a_detector_that_imports(ledgers):
    for cls in ledgers.offline_classes:
        mod = _detectors.load(cls.detector)
        assert mod.CLASS_ID == cls.id
        assert callable(mod.detect)
        assert mod.BOUNDARY


def test_every_detector_module_is_claimed_by_exactly_one_class(ledgers):
    declared = [c.detector for c in ledgers.offline_classes]
    assert sorted(declared) == sorted(set(declared)), "a detector is double-claimed"
    assert set(declared) == set(_detectors.available())


def test_every_class_names_a_tracked_collectable_test_module(repo, ledgers):
    tracked = set(repo.tracked)
    for cls in ledgers.offline_classes:
        assert cls.test in tracked, cls.id + " test module is not git-tracked"
        assert os.path.basename(cls.test).startswith("test_")


def test_class_with_a_nonexistent_detector_fails(repo):
    problems = check_class_detector_bijection(
        [_fake_class(detector="c99_does_not_exist")], _detectors.available(), repo.tracked)
    assert any("does not exist" in p for p in problems)


def test_detector_with_no_class_fails(repo, ledgers):
    problems = check_class_detector_bijection(
        ledgers.classes, list(_detectors.available()) + ["c98_orphan"], repo.tracked)
    assert any("c98_orphan" in p and "no class declares it" in p for p in problems)


def test_class_whose_test_is_untracked_fails(repo):
    problems = check_class_detector_bijection(
        [_fake_class(detector=_detectors.available()[0],
                     test="tests/unit/audit/test_never_committed.py")],
        _detectors.available(), repo.tracked)
    assert any("not git-tracked" in p for p in problems)


def test_residual_class_may_not_carry_a_detector(repo):
    problems = check_class_detector_bijection(
        [_fake_class(id="L-99", status="residual", detector="c01_timer_deadman",
                     raw={"why_not_offline": "x"})],
        _detectors.available(), repo.tracked)
    assert any("residual but names detector" in p for p in problems)


def test_meta_failure_raises_and_the_cli_exits_2(repo, monkeypatch):
    """AC-3's exit-code half: a broken bijection must exit 2, not 1, not 0."""
    monkeypatch.setattr(_detectors, "available", lambda: ["c98_ghost_detector"])
    with pytest.raises(RegimeError) as exc:
        run(repo)
    assert "REGIME INTEGRITY" in str(exc.value)

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "qflix_audit_cli", os.path.join(REPO_ROOT, "scripts", "maint", "qflix-audit.py"))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["--root", REPO_ROOT, "--json"]) == 2


# ---------------------------------------------------------------------------
# Total scope cover  (AC-4)
# ---------------------------------------------------------------------------

def test_every_tracked_path_maps_to_exactly_one_surface(repo, ledgers):
    problems = check_scope_partition(repo.tracked, ledgers.scope)
    assert problems == [], "\n  - " + "\n  - ".join(problems)


def test_a_path_outside_every_area_fails(ledgers):
    """AC-4's negative: a new top-level directory must be ENROLLED before it can
    be audited. Silently inheriting a catch-all is how scope rots."""
    problems = check_scope_partition(["zzz-brand-new-dir/thing.py"], ledgers.scope)
    assert any("maps to NO scope area" in p for p in problems)


def test_a_path_claimed_twice_fails(ledgers):
    scope = copy.deepcopy(ledgers.scope)
    scope["repo_areas"].append(
        {"id": "A-overlap", "surface": "S1", "paths": ["scripts/maint/*"]})
    problems = check_scope_partition(["scripts/maint/qflix-audit.py"], scope)
    assert any("scope areas" in p and "ambiguous" in p for p in problems)


def test_out_of_scope_entries_carry_reasons(ledgers):
    for oos in ledgers.scope["out_of_scope"]:
        assert (oos.get("reason") or "").strip(), oos["id"] + " has no reason"


# ---------------------------------------------------------------------------
# Waiver + residual discipline  (AC-12)
# ---------------------------------------------------------------------------

def test_real_waivers_pass_discipline(ledgers):
    assert check_waiver_discipline(ledgers.classes) == []


@pytest.mark.parametrize("mutation,needle", [
    ({"reason": "too short"}, "chars; needs >="),
    ({"date": None}, "no valid date"),
    ({"owner": ""}, "no owner"),
    ({"match": {}}, "no match selector"),
    ({"id": None}, "waiver with no id"),
])
def test_a_defective_waiver_fails_the_meta_check(mutation, needle):
    waiver = {"id": "W-X", "match": {"path": "a"}, "owner": "operator",
              "date": "2026-07-29", "reason": "x" * 60}
    waiver.update(mutation)
    problems = check_waiver_discipline([_fake_class(waivers=[waiver])])
    assert any(needle in p for p in problems), problems


def test_residuals_are_fresh_today(repo, ledgers):
    problems = check_residual_discipline(
        ledgers.scope, ledgers.classes, repo.read(RESIDUAL_DOC), _dt.date.today())
    assert problems == [], "\n  - " + "\n  - ".join(problems)


def test_a_lapsed_residual_review_fails(repo, ledgers):
    """A residual register nobody re-reads is a list of excuses. The cadence is
    a dead-man on the operator, and it is SUPPOSED to go red on schedule."""
    far_future = _dt.date.today() + _dt.timedelta(days=4000)
    problems = check_residual_discipline(
        ledgers.scope, ledgers.classes, repo.read(RESIDUAL_DOC), far_future)
    assert any("review is DUE" in p for p in problems)


def test_a_residual_with_no_row_in_the_register_fails(ledgers):
    scope = copy.deepcopy(ledgers.scope)
    scope["residuals"].append({
        "id": "R-GHOST", "owner": "operator", "review_cadence_days": 30,
        "last_reviewed": _dt.date.today().isoformat(), "residual": "invented"})
    problems = check_residual_discipline(
        scope, ledgers.classes, "| R1 | x |", _dt.date.today())
    assert any("R-GHOST has no row" in p for p in problems)


def test_a_missing_register_fails(ledgers):
    problems = check_residual_discipline(
        ledgers.scope, ledgers.classes, None, _dt.date.today())
    assert problems and "is missing" in problems[0]


# ---------------------------------------------------------------------------
# Live classes are named, never silently dropped  (AC-16)
# ---------------------------------------------------------------------------

def test_live_classes_appear_in_both_ledgers(repo, ledgers):
    doc = repo.read(RESIDUAL_DOC)
    live = [c for c in ledgers.classes if c.is_residual]
    assert {c.id for c in live} >= {"L-01", "L-02", "L-03", "L-04", "L-05", "L-06"}
    for c in live:
        assert (c.raw.get("why_not_offline") or "").strip(), c.id
        assert c.raw.get("live_detector"), c.id + " names no live detector"
        assert ("| " + c.id + " |") in doc, c.id + " is missing a register row"


def test_dropping_a_live_class_from_the_register_fails(repo, ledgers):
    doc = repo.read(RESIDUAL_DOC).replace("| L-03 |", "| L-XX |")
    problems = check_residual_discipline(
        ledgers.scope, ledgers.classes, doc, _dt.date.today())
    assert any("L-03 has no row" in p for p in problems)


# ---------------------------------------------------------------------------
# CI map vs the real workflow  (AC-5)
# ---------------------------------------------------------------------------

def test_ci_map_matches_the_workflow(repo, ledgers):
    wf = repo.read(ledgers.scope["ci_execution"]["workflow"])
    assert check_ci_execution(ledgers.scope, wf) == []


def test_the_workflow_has_a_pwsh_job_running_the_rea_suite(repo):
    wf = yaml.safe_load(repo.read(".github/workflows/tests.yml"))
    assert "pwsh" in wf["jobs"], "the alerting layer's suite is out of CI again"
    runs = "\n".join(str(s.get("run", "")) for s in wf["jobs"]["pwsh"]["steps"])
    assert "tests/local-llm/test-qflix-rea.ps1" in runs
    assert "tests/local-llm/test-rea-noise-classes.ps1" in runs


def test_deleting_the_pwsh_job_fails_the_ci_check(repo, ledgers):
    wf = yaml.safe_load(repo.read(".github/workflows/tests.yml"))
    del wf["jobs"]["pwsh"]
    problems = check_ci_execution(ledgers.scope, yaml.safe_dump(wf))
    assert any("'pwsh'" in p and "not in the workflow" in p for p in problems)


def test_a_job_that_stops_running_its_command_fails(repo, ledgers):
    wf = yaml.safe_load(repo.read(".github/workflows/tests.yml"))
    wf["jobs"]["pytest"]["steps"][-1]["run"] = "echo skipped"
    problems = check_ci_execution(ledgers.scope, yaml.safe_dump(wf))
    assert any("no longer runs" in p for p in problems)


# ---------------------------------------------------------------------------
# Compartmentalisation  (AC-15)
# ---------------------------------------------------------------------------

def test_the_regime_ships_as_its_own_module_timer_and_monitor(repo):
    tracked = set(repo.tracked)
    assert "scripts/maint/qflix-audit.py" in tracked
    assert "scripts/maint/systemd/manitoba-maint-audit.timer" in tracked
    assert "scripts/maint/systemd/manitoba-maint-audit.service" in tracked
    from lib.kuma import STANDALONE_SELF_PUSH_MONITORS
    assert "QFlix Audit Regime" in STANDALONE_SELF_PUSH_MONITORS


def test_nothing_was_folded_into_an_existing_job(repo):
    """The operator design law: every maintenance concern gets its OWN module,
    timer and check so it stays independently swappable across a migration."""
    for host in ("scripts/maint/functional-audit.py", "scripts/maint/qflix-collect.py"):
        text = repo.read(host)
        assert "lib.audit" not in text
        assert "qflix-audit" not in text


def test_the_audit_timer_is_itself_dead_manned(ledgers):
    """The auditor must be watched by something that is not itself."""
    job = ledgers.jobs["jobs"]["manitoba-maint-audit"]
    assert job["kuma_monitor"] == "QFlix Audit Regime"


# ---------------------------------------------------------------------------
# The honest statement  (AC-17)
# ---------------------------------------------------------------------------

def _flat(text):
    """Markdown reflows; the SENTENCE has to survive a line break."""
    return re.sub(r"[\s>*_`]+", " ", text.lower())


def test_the_docs_say_plainly_that_no_new_findings_ever_is_not_delivered(repo):
    regime = _flat(repo.read("docs/audit-regime.md"))
    residual = _flat(repo.read(RESIDUAL_DOC))
    assert "does not deliver" in regime or "not deliver" in regime
    assert "no new findings ever again" in regime
    assert "no new findings ever again" in residual
    residual = repo.read(RESIDUAL_DOC)
    for rid in ("R1", "R2", "R3", "R4", "R5", "R6"):
        assert ("| " + rid + " |") in residual, rid + " is missing from the register"
    # R5 is the last turtle and must be stated, not implied.
    assert "unwatched" in residual.lower()


def test_advisory_classes_say_why_they_are_not_enforced_yet(ledgers):
    for cls in ledgers.offline_classes:
        if cls.status == "advisory":
            note = (cls.raw.get("backlog_note") or "").strip()
            assert note or cls.advisory_kinds, (
                cls.id + " is advisory with no written reason and no advisory kinds")
