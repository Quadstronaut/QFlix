"""C-08 — decommissioned names, enumerated across every tracked file.

Baselines are REPRODUCED by the detector, never asserted as literals: the
Stage-0 measurement said maintainerr appeared in 19 files under scripts/ +
manifest/, and this test recomputes that number independently and compares it
to what the detector saw. A hardcoded 19 would go stale on the next commit and
teach everyone to edit the number instead of the code.
"""
from __future__ import annotations

import re

from lib.audit.detectors import c08_decommissioned_refs as det
from lib.audit.model import FINDING, OK

CODE_SURFACES = ("scripts/", "manifest/")


def _reference_files(repo, name):
    rx = re.compile(re.escape(name), re.I)
    return sorted(
        p for p in repo.tracked
        if p.startswith(CODE_SURFACES)
        and not p.lower().endswith(det.BINARY_SUFFIXES)
        and rx.search(repo.read(p))
    )


def test_every_component_is_enumerated(ctx, ledgers):
    result = det.detect(ctx)
    assert result.metrics["components"] == len(ledgers.decommissioned["components"])
    for comp in ledgers.decommissioned["components"]:
        assert "occurrences_" + comp["id"] in result.metrics


def test_detector_reproduces_the_measured_baselines(ctx, repo):
    """Not asserted as literals — recomputed here and compared."""
    result = det.detect(ctx)
    for comp_id in ("maintainerr", "homarr", "quadstronix"):
        ref = _reference_files(repo, comp_id)
        seen = {v.path for v in result.verdicts
                if v.instance_id.endswith(":" + comp_id)
                and v.path.startswith(CODE_SURFACES)}
        assert seen == set(ref), (
            comp_id + " file set mismatch: detector-only "
            + str(sorted(seen - set(ref))) + " reference-only "
            + str(sorted(set(ref) - seen)))


def test_boundary_size_equals_the_verdict_count(ctx):
    result = det.detect(ctx)
    assert result.boundary_size == len(result.verdicts)
    assert result.boundary_size == result.metrics["occurrences"]


def test_history_surfaces_are_never_findings(ctx, ledgers):
    """A changelog that stopped naming what it removed would be a worse
    artifact than the stale reference."""
    result = det.detect(ctx)
    history = {h["path"] for h in ledgers.decommissioned["allowed_context_paths"]}
    for v in result.verdicts:
        if v.path in history:
            assert v.status == OK


def test_every_allow_path_carries_a_reason(ledgers):
    for comp in ledgers.decommissioned["components"]:
        for allow in comp.get("allow_paths") or []:
            assert len((allow.get("reason") or "").strip()) >= 40, (
                comp["id"] + " allow_path " + str(allow.get("path")) + " has a thin reason")
    for ctxp in ledgers.decommissioned["allowed_context_paths"]:
        assert len((ctxp.get("reason") or "").strip()) >= 20


def test_a_live_reference_is_flagged(ctx, tmp_path):
    """Synthetic: a unit file that still ExecStarts the retired app."""
    from lib.audit.repo import Repo
    (tmp_path / "scripts" / "maint").mkdir(parents=True)
    (tmp_path / "scripts" / "maint" / "x.service").write_text(
        "[Service]\nExecStart=/usr/bin/app-homarr start\n", encoding="utf-8")

    class _C:
        repo = Repo(tmp_path, tracked=["scripts/maint/x.service"])
        ledgers = ctx.ledgers

    result = det.detect(_C())
    findings = [v for v in result.verdicts if v.status == FINDING]
    assert len(findings) == 1
    assert findings[0].kind == "live-reference"


def test_a_line_declaring_the_retirement_is_allowed(ctx, tmp_path):
    from lib.audit.repo import Repo
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts" / "y.sh").write_text(
        "# homarr was decommissioned 2026-07-13; qflix-dash replaced it\n",
        encoding="utf-8")

    class _C:
        repo = Repo(tmp_path, tracked=["scripts/y.sh"])
        ledgers = ctx.ledgers

    result = det.detect(_C())
    assert [v for v in result.verdicts if v.status == FINDING] == []
    assert result.verdicts[0].kind == "declared-retired"


def test_the_backlog_is_non_empty_and_that_is_the_honest_answer(ctx):
    """Advisory means enumerated-and-reported, not clean. Asserting the backlog
    exists stops anyone quietly allowlisting it to zero."""
    result = det.detect(ctx)
    assert [v for v in result.verdicts if v.status == FINDING], (
        "C-08 reports zero live references — either the repo really was cleaned, "
        "in which case flip the class to enforced, or the allowlist grew teeth "
        "it should not have")
