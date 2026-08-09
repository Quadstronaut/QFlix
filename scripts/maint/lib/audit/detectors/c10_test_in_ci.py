"""C-10 test-not-in-CI / subject-not-tracked.

The single check that would have caught the qflix-rea.ps1 gap, and the highest
value per line in the whole regime.

At HEAD 06d4226: tests/local-llm/test-qflix-rea.ps1 held 85 Test-Case blocks
against the ALERTING LAYER, dot-sourced a file that .gitignore:55 keeps out of
git, and was run by ZERO CI jobs. A test suite in no CI, testing a subject that
does not exist in CI, guarding the thing that pages the operator at 2am.

Two invariants, both enforced:
  1. every tracked test file is executed by >= 1 CI job — where "executed" is
     read from audit-scope.yaml:ci_execution AND verified against the real
     workflow file, so deleting the pwsh job makes this fail rather than
     quietly leaving a map that lies;
  2. every file a test imports or dot-sources is git-tracked, OR registered in
     audit-scope.yaml surface S2 with a residual reason and an owner.
"""
from __future__ import annotations

import re
from typing import Dict, List, Set

from ..model import FINDING, OK, DetectorResult, Verdict
from ..repo import glob_match

NAME = "c10_test_in_ci"
CLASS_ID = "C-10"
BOUNDARY = "tracked test files UNION their resolved subjects UNION declared CI jobs"

# `. $scriptPath` where $scriptPath came from Join-Path $repoRoot '<rel>'.
# Filtered to LOADABLE extensions: a PowerShell test also Join-Paths directories
# and scratch files it merely probes (`Join-Path $r 'secrets'`), and calling
# those "subjects" would bury the one that matters — the dot-sourced script —
# under noise.
PS_JOINPATH = re.compile(r"Join-Path\s+\$\w+\s+'([^']+)'")
LOADABLE_SUFFIXES = (".ps1", ".psm1", ".py", ".yaml", ".yml", ".json")
PY_LIB_IMPORT = re.compile(r"^\s*(?:from\s+(lib\.[\w.]+)\s+import|import\s+(lib\.[\w.]+))", re.M)


def _s2_members(scope: dict) -> Dict[str, dict]:
    s2 = (scope.get("surfaces") or {}).get("S2") or {}
    return {m["path"]: m for m in (s2.get("members") or []) if m.get("path")}


def _resolve_python_subjects(text: str, roots: List[str], repo) -> Set[str]:
    """`from lib.foo.bar import x` -> the first existing <root>/lib/foo/bar.py."""
    out: Set[str] = set()
    for m in PY_LIB_IMPORT.finditer(text):
        dotted = m.group(1) or m.group(2)
        rel = dotted.replace(".", "/")
        for root in roots:
            for cand in (root + "/" + rel + ".py", root + "/" + rel + "/__init__.py"):
                if repo.exists(cand):
                    out.add(cand)
                    break
            else:
                continue
            break
        else:
            out.add("<unresolved>" + dotted)
    return out


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    scope = ctx.ledgers.scope
    ci = scope.get("ci_execution") or {}
    patterns = scope.get("test_file_patterns") or []
    roots = scope.get("python_import_roots") or []
    s2 = _s2_members(scope)

    workflow_text = repo.read_optional(ci.get("workflow") or "")
    import yaml
    workflow = {}
    if workflow_text:
        try:
            workflow = yaml.safe_load(workflow_text) or {}
        except yaml.YAMLError:
            workflow = {}
    wf_jobs = workflow.get("jobs") or {}

    verdicts: List[Verdict] = []
    live_globs: List[str] = []

    # --- the CI map must describe the real workflow ------------------------
    for decl in ci.get("jobs") or []:
        job = decl.get("job")
        iid = (ci.get("workflow") or "workflow") + ":job:" + str(job)
        if job not in wf_jobs:
            verdicts.append(Verdict(iid, "ci-job-missing", FINDING, ci.get("workflow", ""), 0,
                                    "declared CI job '" + str(job) + "' is not in the workflow"))
            continue
        needle = decl.get("must_contain") or ""
        runs = "\n".join(str(s.get("run", "")) for s in (wf_jobs[job].get("steps") or []))
        if needle and needle not in runs:
            verdicts.append(Verdict(iid, "ci-command-missing", FINDING, ci.get("workflow", ""), 0,
                                    "CI job '" + str(job) + "' no longer runs " + repr(needle)))
            continue
        live_globs.extend(decl.get("executes") or [])
        verdicts.append(Verdict(iid, "ci-job-live", OK, ci.get("workflow", ""), 0,
                                "job exists and still runs " + repr(needle)))

    # --- every tracked test file is executed somewhere ---------------------
    test_files = [p for p in repo.tracked if any(glob_match(g, p) for g in patterns)]
    covered = 0
    for path in test_files:
        iid = path + ":ci"
        if any(glob_match(g, path) for g in live_globs):
            covered += 1
            verdicts.append(Verdict(iid, "test-in-ci", OK, path, 0, "executed by a live CI job"))
        else:
            verdicts.append(Verdict(
                iid, "test-not-in-ci", FINDING, path, 0,
                "no live CI job executes this test file — it can rot green forever",
            ))

    # --- every subject a test loads is tracked or S2-registered ------------
    subjects = 0
    unresolved = 0
    for path in test_files:
        text = repo.read(path)
        found: Set[str] = set()
        if path.endswith(".ps1"):
            found |= {
                m.group(1) for m in PS_JOINPATH.finditer(text)
                if m.group(1).lower().endswith(LOADABLE_SUFFIXES)
            }
        else:
            found |= _resolve_python_subjects(text, roots, repo)
        for subj in sorted(found):
            subjects += 1
            iid = path + "->" + subj
            if subj.startswith("<unresolved>"):
                unresolved += 1
                verdicts.append(Verdict(
                    iid, "subject-unresolvable", FINDING, path, 0,
                    "import " + subj[len("<unresolved>"):] + " resolves to no file under the "
                    "declared python_import_roots",
                ))
                continue
            if repo.is_tracked(subj):
                verdicts.append(Verdict(iid, "subject-tracked", OK, path, 0,
                                        "subject " + subj + " is in git"))
                continue
            member = s2.get(subj)
            if member and (member.get("residual") or "").strip() and (member.get("owner") or "").strip():
                verdicts.append(Verdict(
                    iid, "subject-s2-registered", OK, path, 0,
                    "subject " + subj + " is untracked but registered in audit-scope S2 "
                    "with a residual reason and an owner (" + str(member.get("owner")) + ")",
                ))
                continue
            verdicts.append(Verdict(
                iid, "subject-untracked", FINDING, path, 0,
                "subject " + subj + " is neither git-tracked nor registered in "
                "audit-scope.yaml surface S2 — this test's subject is invisible to CI",
            ))

    return DetectorResult(
        boundary_name=BOUNDARY,
        boundary_size=len(test_files) + subjects + len(ci.get("jobs") or []),
        verdicts=verdicts,
        metrics={
            "ci_jobs_declared": len(ci.get("jobs") or []),
            "test_files": len(test_files),
            "test_files_in_ci": covered,
            "subjects_resolved": subjects,
            "subjects_unresolved": unresolved,
        },
    )
