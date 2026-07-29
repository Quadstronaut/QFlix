"""C-03 — every except handler gets a verdict, classified three ways.

The classification is what carries the class: `bare`, `broad`, `narrow`, and
the pass-only subset of the first two. All four counts are re-derived here by an
independent walk.
"""
from __future__ import annotations

import ast

from lib.audit.detectors import c03_error_swallowing as det
from lib.audit.model import FINDING, OK
from lib.audit.pysrc import source_files


def _reference(repo):
    rows = []
    for path in source_files(repo):
        try:
            tree = ast.parse(repo.read(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            t = node.type
            if t is None:
                kind = "bare"
            elif isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"):
                kind = "broad"
            elif isinstance(t, ast.Tuple) and any(
                    isinstance(e, ast.Name) and e.id in ("Exception", "BaseException")
                    for e in t.elts):
                kind = "broad"
            else:
                kind = "narrow"
            pass_only = all(isinstance(s, ast.Pass) for s in node.body)
            rows.append((path, node.lineno, kind, pass_only))
    return rows


def test_visits_every_handler_with_zero_omissions(ctx, repo):
    result = det.detect(ctx)
    ref = _reference(repo)
    assert ref, "reference walk found no handlers — the test is broken"
    assert result.boundary_size == len(ref)
    assert len(result.verdicts) == len(ref)
    assert sorted((v.path, v.lineno) for v in result.verdicts) == sorted(
        (p, ln) for p, ln, _k, _po in ref)


def test_classification_matches_an_independent_walk(ctx, repo):
    result = det.detect(ctx)
    ref = _reference(repo)
    m = result.metrics
    assert m["handlers"] == len(ref)
    assert m["handlers_bare"] == sum(1 for r in ref if r[2] == "bare")
    assert m["handlers_broad"] == sum(1 for r in ref if r[2] == "broad")
    assert m["handlers_narrow"] == sum(1 for r in ref if r[2] == "narrow")
    assert m["handlers_broad_pass_only"] == sum(
        1 for r in ref if r[2] in ("bare", "broad") and r[3])


def test_repo_still_has_zero_bare_excepts(ctx):
    """A property worth asserting on its own: `except:` also swallows
    KeyboardInterrupt and SystemExit, so a single one is categorically worse
    than a broad `except Exception`."""
    assert det.detect(ctx).metrics["handlers_bare"] == 0


def test_pass_only_handlers_are_the_findings(ctx, repo):
    result = det.detect(ctx)
    ref_pass_only = {(p, ln) for p, ln, k, po in _reference(repo)
                     if k in ("bare", "broad") and po}
    got = {(v.path, v.lineno) for v in result.verdicts if v.status == FINDING}
    assert got == ref_pass_only


def test_narrow_handlers_are_never_findings(ctx, repo):
    result = det.detect(ctx)
    narrow = {(p, ln) for p, ln, k, _po in _reference(repo) if k == "narrow"}
    for v in result.verdicts:
        if (v.path, v.lineno) in narrow:
            assert v.status == OK and v.kind == "narrow-handler"


def test_backlog_is_counted_not_hidden(ctx):
    """The 250-odd broad-but-not-empty handlers are the declared backlog. They
    must be COUNTED, because 'we did not look' and 'we looked and deferred' are
    different states and only one of them is honest."""
    m = det.detect(ctx).metrics
    assert m["backlog_unadjudicated"] == (
        m["handlers_broad"] + m["handlers_bare"] - m["handlers_broad_pass_only"])
    assert m["backlog_unadjudicated"] > 0
