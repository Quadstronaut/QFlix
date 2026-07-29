"""C-03 error-swallowing-handler.

Baseline at HEAD 06d4226: 566 except handlers under scripts/**/*.py, 0 bare
`except:`, 288 broad `except Exception`, and 35 of those with a pass-only body.
The pass-only 35 are the enforceable subset — a handler that catches everything
and does literally nothing cannot be defended by context.

The remaining 253 broad handlers are NOT findings. They are counted, reported
in `metrics`, and left as the class's declared backlog. Calling them findings
today would drown the 35 that matter, and calling them fine would be a lie; the
honest state is "enumerated, unadjudicated".
"""
from __future__ import annotations

import ast
from typing import List

from ..model import FINDING, OK, DetectorResult, Verdict
from ..pysrc import parse, source_files

NAME = "c03_error_swallowing"
CLASS_ID = "C-03"
BOUNDARY = "every ast.ExceptHandler in tracked scripts/**/*.py (tests excluded)"

BROAD_NAMES = {"Exception", "BaseException"}


def _handler_kind(node: ast.ExceptHandler) -> str:
    t = node.type
    if t is None:
        return "bare"
    if isinstance(t, ast.Name) and t.id in BROAD_NAMES:
        return "broad"
    if isinstance(t, ast.Tuple) and any(
            isinstance(e, ast.Name) and e.id in BROAD_NAMES for e in t.elts):
        return "broad"
    return "narrow"


def _is_pass_only(node: ast.ExceptHandler) -> bool:
    """Only `pass` (and/or a docstring/ellipsis) in the body."""
    for stmt in node.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
                and stmt.value.value is Ellipsis:
            continue
        return False
    return True


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    files = source_files(repo)
    verdicts: List[Verdict] = []
    counts = {"bare": 0, "broad": 0, "narrow": 0}
    broad_pass_only = 0
    unparsable = 0

    for path in files:
        tree = parse(repo, path)
        if tree is None:
            unparsable += 1
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            kind = _handler_kind(node)
            counts[kind] += 1
            iid = path + ":" + str(node.lineno) + ":except"
            if kind == "narrow":
                verdicts.append(Verdict(iid, "narrow-handler", OK, path, node.lineno,
                                        "handler names specific exception types"))
                continue
            if _is_pass_only(node):
                broad_pass_only += 1
                verdicts.append(Verdict(
                    iid, "broad-pass-only", FINDING, path, node.lineno,
                    "catches " + kind + " and the body is pass-only: the fault leaves no trace",
                ))
                continue
            # Broad but does SOMETHING. Enumerated and counted, not adjudicated.
            verdicts.append(Verdict(
                iid, "broad-unadjudicated", OK, path, node.lineno,
                "broad handler with a non-empty body — declared backlog, not yet reviewed",
            ))

    return DetectorResult(
        boundary_name=BOUNDARY,
        boundary_size=sum(counts.values()),
        verdicts=verdicts,
        metrics={
            "files_scanned": len(files),
            "files_unparsable": unparsable,
            "handlers": sum(counts.values()),
            "handlers_bare": counts["bare"],
            "handlers_broad": counts["broad"],
            "handlers_narrow": counts["narrow"],
            "handlers_broad_pass_only": broad_pass_only,
            "backlog_unadjudicated": counts["broad"] + counts["bare"] - broad_pass_only,
        },
    )
