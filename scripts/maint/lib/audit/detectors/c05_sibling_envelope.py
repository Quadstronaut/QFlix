"""C-05 sibling-inconsistent-envelope.

The ad8198e class, generalised so a THIRD sibling cannot appear.

qflix_status and qflix_arr_queue computed captured_at / snapshot_age_minutes /
stale_warning. Three sibling functions in the SAME FILE read the SAME snapshot
and returned it bare — serving a 19-day-old cache as live. Every prior fix in
this repo for this shape was per-instance; this one is per-module.

The rule, derived from the real instance rather than invented:
  1. A module has a freshness HELPER if some function mentions both
     `captured_at` and `stale_warning` AND is called by >= 2 other functions in
     the module. (The ">= 2 callers" clause is what stops a single annotating
     function being mistaken for the shared helper.)
  2. The helper's callers are the sibling set. The ACCESSORS are the call names
     that appear in EVERY sibling — for qflix_mcp.py that is `_cache` and
     `latest_snapshot`.
  3. Any other module-level function that calls ALL of those accessors, but not
     the helper, is reading the same snapshot without the envelope.

Requiring ALL accessors (not any) is what keeps write-side tools that merely
touch `_cache()` out of the results. Narrow on purpose: this class is worth
having only if its findings are believable.
"""
from __future__ import annotations

import ast
import builtins
from typing import Dict, List, Set

# Builtins are called by everything, so leaving them in the shared-accessor set
# makes the set describe "functions that use Python" instead of "functions that
# read the snapshot" — and a sibling that happens not to call dict() slips out.
_BUILTINS = set(dir(builtins))

from ..model import FINDING, OK, DetectorResult, Verdict
from ..pysrc import parse, source_files

NAME = "c05_sibling_envelope"
CLASS_ID = "C-05"
BOUNDARY = "module-level functions of tracked scripts/**/*.py modules that define a freshness helper"

ENVELOPE_TOKENS = ("captured_at", "stale_warning")
MIN_HELPER_CALLERS = 2


def _called_names(fn: ast.AST) -> Set[str]:
    """Every simple call name in a function: `f()` -> "f", `x.g()` -> "g"."""
    out: Set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    verdicts: List[Verdict] = []
    modules_with_helper = 0
    boundary = 0
    unparsable = 0

    for path in source_files(repo):
        text = repo.read(path)
        if not all(tok in text for tok in ENVELOPE_TOKENS):
            continue
        tree = parse(repo, path)
        if tree is None:
            unparsable += 1
            continue

        funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if len(funcs) < 3:
            continue
        calls: Dict[str, Set[str]] = {f.name: _called_names(f) for f in funcs}

        helper = None
        for f in funcs:
            src = ast.unparse(f) if hasattr(ast, "unparse") else ""
            mentions = all(tok in src for tok in ENVELOPE_TOKENS) if src else False
            if not mentions:
                continue
            callers = [n for n, cs in calls.items() if n != f.name and f.name in cs]
            if len(callers) >= MIN_HELPER_CALLERS:
                helper = f.name
                break
        if helper is None:
            continue

        siblings = sorted(n for n, cs in calls.items() if n != helper and helper in cs)
        accessors = set.intersection(*(calls[s] for s in siblings)) - {helper} - _BUILTINS
        if not accessors:
            continue
        modules_with_helper += 1

        for f in funcs:
            if f.name == helper:
                continue
            boundary += 1
            iid = path + ":" + str(f.lineno) + ":" + f.name
            if f.name in siblings:
                verdicts.append(Verdict(iid, "envelope-applied", OK, path, f.lineno,
                                        "calls the shared freshness helper " + helper + "()"))
                continue
            if accessors.issubset(calls[f.name]):
                verdicts.append(Verdict(
                    iid, "sibling-missing-envelope", FINDING, path, f.lineno,
                    "reads the same snapshot source (" + ", ".join(sorted(accessors))
                    + ") as its siblings but never calls " + helper + "()",
                ))
                continue
            verdicts.append(Verdict(iid, "not-a-snapshot-reader", OK, path, f.lineno,
                                    "does not read the shared snapshot source"))

    return DetectorResult(
        boundary_name=BOUNDARY,
        boundary_size=boundary,
        verdicts=verdicts,
        metrics={
            "modules_with_helper": modules_with_helper,
            "functions_examined": boundary,
            "files_unparsable": unparsable,
        },
    )
