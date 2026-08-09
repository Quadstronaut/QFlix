"""C-02 unchecked-subprocess.

`subprocess.run(...)` with no `check=` returns a CompletedProcess nobody looks
at. The command fails, the function continues, and the caller is told the work
was done. Baseline at HEAD 06d4226: 30 call sites, 27 of them `subprocess.run`
without `check=`.

A site is OK when the failure is REACHABLE by the code, one of:
  - `check=` is passed explicitly (whatever its value — passing check=False is
    a decision, and a decision is what this class is asking for);
  - the result is bound to a name whose .returncode/.stdout/.stderr is read, or
    which is tested, in the same function;
  - the call is attribute-accessed inline (`subprocess.run(...).returncode`).

Everything else is a finding. Deliberately syntactic: whether the code then
does the RIGHT thing with the returncode is a semantic question this class does
not pretend to answer (residual R3).
"""
from __future__ import annotations

import ast
from typing import Dict, List, Optional, Set

from ..model import FINDING, OK, DetectorResult, Verdict
from ..pysrc import enclosing_function, parent_map, parse, source_files

NAME = "c02_unchecked_subprocess"
CLASS_ID = "C-02"
BOUNDARY = "every ast.Call on subprocess.{run,call,Popen} in tracked scripts/**/*.py (tests excluded)"

WATCHED = ("run", "call", "Popen")
RESULT_ATTRS = {"returncode", "stdout", "stderr", "args", "check_returncode"}


def _is_subprocess_call(node: ast.AST) -> Optional[str]:
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "subprocess":
        if f.attr in WATCHED:
            return f.attr
    return None


def _bound_name(node: ast.Call, parents: Dict[int, ast.AST]) -> Optional[str]:
    """The single name this call's result is bound to, if any."""
    parent = parents.get(id(node))
    if isinstance(parent, ast.Assign) and len(parent.targets) == 1 \
            and isinstance(parent.targets[0], ast.Name):
        return parent.targets[0].id
    if isinstance(parent, (ast.AnnAssign, ast.NamedExpr)) and isinstance(parent.target, ast.Name):
        return parent.target.id
    if isinstance(parent, ast.withitem) and isinstance(parent.optional_vars, ast.Name):
        return parent.optional_vars.id
    return None


def _inline_attribute(node: ast.Call, parents: Dict[int, ast.AST]) -> bool:
    parent = parents.get(id(node))
    return isinstance(parent, ast.Attribute) and parent.attr in RESULT_ATTRS


def _name_is_inspected(scope: ast.AST, name: str, call: ast.Call) -> bool:
    """Is `name` read in a way that could observe failure, in `scope`?"""
    for node in ast.walk(scope):
        if node is call:
            continue
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == name and node.attr in RESULT_ATTRS:
            return True
        # `if rc != 0`, `assert rc == 0`, `while rc:` — the classic
        # subprocess.call idiom, where the int IS the returncode.
        if isinstance(node, (ast.Compare, ast.If, ast.Assert, ast.While, ast.Return, ast.BoolOp)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == name:
                    return True
    return False


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    files = source_files(repo)
    verdicts: List[Verdict] = []
    unparsable: List[str] = []
    no_check = 0
    by_attr: Dict[str, int] = {a: 0 for a in WATCHED}

    for path in files:
        tree = parse(repo, path)
        if tree is None:
            unparsable.append(path)
            continue
        parents = parent_map(tree)
        for node in ast.walk(tree):
            attr = _is_subprocess_call(node)
            if attr is None:
                continue
            by_attr[attr] += 1
            kwargs: Set[str] = {kw.arg for kw in node.keywords if kw.arg}
            iid = path + ":" + str(node.lineno) + ":subprocess." + attr
            if "check" in kwargs:
                verdicts.append(Verdict(iid, "checked", OK, path, node.lineno,
                                        "subprocess." + attr + " passes check= explicitly"))
                continue
            no_check += 1
            if _inline_attribute(node, parents):
                verdicts.append(Verdict(iid, "result-inspected-inline", OK, path, node.lineno,
                                        "no check= but the result is attribute-accessed inline"))
                continue
            name = _bound_name(node, parents)
            scope = enclosing_function(node, parents) or tree
            if name and _name_is_inspected(scope, name, node):
                verdicts.append(Verdict(iid, "result-inspected", OK, path, node.lineno,
                                        "no check= but the bound result is read in the same scope"))
                continue
            if attr == "Popen":
                # Popen has no returncode until wait(); a bare Popen whose
                # handle is never touched is still a finding, but a named one
                # whose .wait/.communicate is called counts as inspected above.
                verdicts.append(Verdict(iid, "no-check-result-ignored", FINDING, path, node.lineno,
                                        "subprocess.Popen result is never inspected"))
                continue
            verdicts.append(Verdict(iid, "no-check-result-ignored", FINDING, path, node.lineno,
                                    "subprocess." + attr + " has no check= and its result is never read"))

    return DetectorResult(
        boundary_name=BOUNDARY,
        boundary_size=sum(by_attr.values()),
        verdicts=verdicts,
        metrics={
            "files_scanned": len(files),
            "files_unparsable": len(unparsable),
            "sites": sum(by_attr.values()),
            "sites_run": by_attr["run"],
            "sites_call": by_attr["call"],
            "sites_popen": by_attr["Popen"],
            "sites_without_check": no_check,
        },
    )
