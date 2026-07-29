"""C-02 — every subprocess call site gets a verdict.

Counts are RE-DERIVED by a second, deliberately naive implementation and
compared to the detector's. Hardcoding "30" would only prove somebody typed 30;
a differential test proves two independent walks of the same tree agree.
"""
from __future__ import annotations

import ast

from lib.audit.detectors import c02_unchecked_subprocess as det
from lib.audit.model import FINDING, OK
from lib.audit.pysrc import source_files


def _reference_sites(repo):
    """Naive re-derivation: every subprocess.{run,call,Popen} Call node."""
    out = []
    for path in source_files(repo):
        try:
            tree = ast.parse(repo.read(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "subprocess" \
                    and node.func.attr in ("run", "call", "Popen"):
                has_check = any(k.arg == "check" for k in node.keywords)
                out.append((path, node.lineno, node.func.attr, has_check))
    return sorted(out)


def test_enumerates_every_site_with_zero_omissions(ctx, repo):
    result = det.detect(ctx)
    ref = _reference_sites(repo)
    assert ref, "reference walk found no subprocess sites — the test is broken"
    assert result.boundary_size == len(ref)
    assert len(result.verdicts) == len(ref)
    assert sorted((v.path, v.lineno) for v in result.verdicts) == sorted(
        (p, ln) for p, ln, _a, _c in ref)


def test_metrics_match_an_independent_count(ctx, repo):
    result = det.detect(ctx)
    ref = _reference_sites(repo)
    assert result.metrics["sites"] == len(ref)
    assert result.metrics["sites_run"] == sum(1 for r in ref if r[2] == "run")
    assert result.metrics["sites_call"] == sum(1 for r in ref if r[2] == "call")
    assert result.metrics["sites_popen"] == sum(1 for r in ref if r[2] == "Popen")
    assert result.metrics["sites_without_check"] == sum(1 for r in ref if not r[3])


def test_every_site_with_check_is_ok(ctx, repo):
    result = det.detect(ctx)
    checked = {(p, ln) for p, ln, _a, has in _reference_sites(repo) if has}
    for v in result.verdicts:
        if (v.path, v.lineno) in checked:
            assert v.status == OK and v.kind == "checked"


def test_findings_are_a_subset_of_the_no_check_population(ctx, repo):
    """A finding must never appear at a site that passes check= — that would
    mean the rule fires on code that already made the decision."""
    result = det.detect(ctx)
    no_check = {(p, ln) for p, ln, _a, has in _reference_sites(repo) if not has}
    for v in result.verdicts:
        if v.status == FINDING:
            assert (v.path, v.lineno) in no_check


def test_result_inspection_is_recognised(ctx, tmp_path, repo):
    """Synthetic proof of the three OK paths, so the rule is testable without
    depending on which real files happen to be written which way."""
    from lib.audit.repo import Repo
    src = (
        "import subprocess\n"
        "def a():\n"
        "    r = subprocess.run(['x'])\n"
        "    if r.returncode != 0:\n"
        "        raise SystemExit(1)\n"
        "def b():\n"
        "    subprocess.run(['x'], check=True)\n"
        "def c():\n"
        "    out = subprocess.run(['x']).stdout\n"
        "    return out\n"
        "def d():\n"
        "    subprocess.run(['x'])\n"
    )
    d = tmp_path / "scripts" / "maint"
    d.mkdir(parents=True)
    (d / "sample.py").write_text(src, encoding="utf-8")
    fake = Repo(tmp_path, tracked=["scripts/maint/sample.py"])

    class _C:
        pass
    c = _C()
    c.repo = fake
    c.ledgers = None
    result = det.detect(c)
    by_line = {v.lineno: v for v in result.verdicts}
    assert by_line[3].status == OK          # returncode read
    assert by_line[7].kind == "checked"     # explicit check=
    assert by_line[9].status == OK          # inline .stdout
    assert by_line[12].status == FINDING    # nothing at all
    assert by_line[12].kind == "no-check-result-ignored"
