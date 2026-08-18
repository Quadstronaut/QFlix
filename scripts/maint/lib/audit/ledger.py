"""lib/audit/ledger.py — load + validate the ledgers, and the META-CHECK.

This is the module that audits the AUDITOR. Everything here raises RegimeError
(exit 2), never a finding (exit 1), because "the auditor is broken" must never
look like "the auditor found nothing".

Pure functions take their inputs as arguments so every meta-check can be
negative-tested without mutating the real repo — which is the only way to prove
a guard actually fails when it should.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import yaml

from .model import RegimeError
from .repo import Repo, glob_match

MIN_REASON_CHARS = 40

LEDGER_PATHS = {
    "scope": "manifest/audit-scope.yaml",
    "classes": "manifest/defect-classes.yaml",
    "decommissioned": "manifest/decommissioned.yaml",
    "jobs": "manifest/jobs.yaml",
    "rea": "manifest/rea-noise-classes.yaml",
}

RESIDUAL_DOC = "docs/audit-residual-risk.md"


def _load_yaml(repo: Repo, rel: str) -> dict:
    text = repo.read_optional(rel)
    if text is None:
        raise RegimeError("ledger missing: " + rel)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegimeError("ledger " + rel + " is not valid YAML: " + str(exc)) from exc
    if not isinstance(data, dict):
        raise RegimeError("ledger " + rel + " must be a mapping at the top level")
    return data


@dataclass
class DefectClass:
    id: str
    title: str
    status: str
    detector: Optional[str]
    test: Optional[str]
    enforced_kinds: List[str] = field(default_factory=list)
    advisory_kinds: List[str] = field(default_factory=list)
    waivers: List[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def is_residual(self) -> bool:
        return self.status == "residual"


@dataclass
class Ledgers:
    scope: dict
    classes: List[DefectClass]
    decommissioned: dict
    jobs: dict
    rea: dict

    def by_id(self, cls_id: str) -> DefectClass:
        for c in self.classes:
            if c.id == cls_id:
                return c
        raise RegimeError("no such defect class: " + cls_id)

    @property
    def offline_classes(self) -> List[DefectClass]:
        return [c for c in self.classes if not c.is_residual]


VALID_STATUS = {"enforced", "advisory", "residual"}


def load(repo: Repo) -> Ledgers:
    scope = _load_yaml(repo, LEDGER_PATHS["scope"])
    raw_classes = _load_yaml(repo, LEDGER_PATHS["classes"])
    decom = _load_yaml(repo, LEDGER_PATHS["decommissioned"])
    jobs = _load_yaml(repo, LEDGER_PATHS["jobs"])
    rea = _load_yaml(repo, LEDGER_PATHS["rea"])

    classes: List[DefectClass] = []
    seen = set()
    for entry in raw_classes.get("classes") or []:
        cid = entry.get("id")
        if not cid:
            raise RegimeError("defect-classes.yaml: an entry has no id")
        if cid in seen:
            raise RegimeError("defect-classes.yaml: duplicate class id " + cid)
        seen.add(cid)
        status = entry.get("status")
        if status not in VALID_STATUS:
            raise RegimeError(
                "defect-classes.yaml: class " + cid + " has status " + repr(status)
                + "; must be one of " + ", ".join(sorted(VALID_STATUS))
            )
        classes.append(DefectClass(
            id=cid,
            title=entry.get("title", ""),
            status=status,
            detector=entry.get("detector"),
            test=entry.get("test"),
            enforced_kinds=list(entry.get("enforced_kinds") or []),
            advisory_kinds=list(entry.get("advisory_kinds") or []),
            waivers=list(entry.get("waivers") or []),
            raw=entry,
        ))
    return Ledgers(scope=scope, classes=classes, decommissioned=decom, jobs=jobs, rea=rea)


# ---------------------------------------------------------------------------
# META-CHECKS. Each returns a list of human-readable problems; empty == pass.
# ---------------------------------------------------------------------------

def check_class_detector_bijection(
    classes: Sequence[DefectClass],
    available_detectors: Sequence[str],
    tracked: Sequence[str],
) -> List[str]:
    """Every non-residual class names a detector module that EXISTS, and every
    detector module is named by exactly one class.

    This is the check that makes "I added a class" and "I added a detector"
    into the same act. Half of either is a broken auditor, not a clean run.
    """
    problems: List[str] = []
    declared: Dict[str, List[str]] = {}
    tracked_set = set(tracked)

    for c in classes:
        if c.is_residual:
            if c.detector:
                problems.append(
                    "class " + c.id + " is residual but names detector " + c.detector
                    + " — residual means NOT decidable offline; drop the detector or change the status"
                )
            if not (c.raw.get("why_not_offline") or "").strip():
                problems.append("residual class " + c.id + " has no why_not_offline")
            continue
        if not c.detector:
            problems.append("class " + c.id + " has status " + c.status + " but no detector")
            continue
        declared.setdefault(c.detector, []).append(c.id)
        if c.detector not in available_detectors:
            problems.append(
                "class " + c.id + " names detector " + repr(c.detector)
                + " which does not exist (available: " + ", ".join(sorted(available_detectors)) + ")"
            )
        if not c.test:
            problems.append("class " + c.id + " has no test module declared")
        elif c.test not in tracked_set:
            problems.append(
                "class " + c.id + " declares test " + c.test + " which is not git-tracked"
            )
        elif not re.search(r"(^|/)test_[^/]*\.py$", c.test):
            problems.append(
                "class " + c.id + " declares test " + c.test
                + " which pytest will not collect (needs a test_*.py basename)"
            )

    for det, owners in sorted(declared.items()):
        if len(owners) > 1:
            problems.append("detector " + det + " is claimed by " + str(len(owners))
                            + " classes: " + ", ".join(sorted(owners)))
    for det in sorted(available_detectors):
        if det not in declared:
            problems.append(
                "detector module " + det + " exists but no class declares it — "
                "an unowned detector runs nothing and proves nothing"
            )
    return problems


def check_scope_partition(tracked: Sequence[str], scope: dict) -> List[str]:
    """Every tracked path matches EXACTLY ONE area or out_of_scope entry.

    Zero matches => a path nobody has decided about (the 2026-07-27 shape).
    Two matches  => an ambiguous boundary (the section-1(g) shape).
    """
    problems: List[str] = []
    claims: List[tuple] = []
    for area in scope.get("repo_areas") or []:
        for pat in area.get("paths") or []:
            claims.append((area["id"], pat))
    for oos in scope.get("out_of_scope") or []:
        for pat in oos.get("paths") or []:
            claims.append((oos["id"], pat))
        if not (oos.get("reason") or "").strip():
            problems.append("out_of_scope entry " + str(oos.get("id")) + " has no reason")

    for path in tracked:
        owners = sorted({cid for cid, pat in claims if glob_match(pat, path)})
        if not owners:
            problems.append(
                "tracked path " + path + " maps to NO scope area — enrol it in "
                "manifest/audit-scope.yaml:repo_areas or out_of_scope"
            )
        elif len(owners) > 1:
            problems.append(
                "tracked path " + path + " maps to " + str(len(owners))
                + " scope areas (" + ", ".join(owners) + ") — the boundary is ambiguous"
            )
    return problems


_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _as_date(value: Any) -> Optional[_dt.date]:
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str) and _DATE_RX.match(value.strip()):
        try:
            return _dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def check_waiver_discipline(classes: Sequence[DefectClass]) -> List[str]:
    """A waiver is a DECISION, so it must carry who decided, when, and why.

    A waiver with a thin reason is worse than no waiver: it silences a finding
    while recording nothing anybody can review later.
    """
    problems: List[str] = []
    seen_ids = set()
    for c in classes:
        for w in c.waivers:
            wid = w.get("id")
            if not wid:
                problems.append("class " + c.id + " has a waiver with no id")
                continue
            if wid in seen_ids:
                problems.append("duplicate waiver id " + wid)
            seen_ids.add(wid)
            reason = (w.get("reason") or "").strip()
            if len(reason) < MIN_REASON_CHARS:
                problems.append(
                    "waiver " + wid + " (class " + c.id + ") reason is "
                    + str(len(reason)) + " chars; needs >= " + str(MIN_REASON_CHARS)
                )
            if _as_date(w.get("date")) is None:
                problems.append("waiver " + wid + " (class " + c.id + ") has no valid date (YYYY-MM-DD)")
            if not (w.get("owner") or "").strip():
                problems.append("waiver " + wid + " (class " + c.id + ") has no owner")
            match = w.get("match")
            if not isinstance(match, dict) or not match:
                problems.append("waiver " + wid + " (class " + c.id + ") has no match selector")
    return problems


def check_residual_discipline(
    scope: dict,
    classes: Sequence[DefectClass],
    residual_doc: Optional[str],
    today: _dt.date,
) -> List[str]:
    """Residual register discipline, in both directions.

    (a) every `residuals:` entry has an owner, a cadence, and a last_reviewed
        inside that cadence;
    (b) every residual entry has a ROW in docs/audit-residual-risk.md;
    (c) every residual-status defect class (L-01..L-07) also has a row, so a
        live class can never be silently dropped from the ledger pair.
    """
    problems: List[str] = []
    if residual_doc is None:
        return ["residual register " + RESIDUAL_DOC + " is missing"]

    for entry in scope.get("residuals") or []:
        rid = entry.get("id")
        if not rid:
            problems.append("audit-scope.yaml: a residual entry has no id")
            continue
        if not (entry.get("owner") or "").strip():
            problems.append("residual " + rid + " has no owner")
        cadence = entry.get("review_cadence_days")
        if not isinstance(cadence, int) or cadence <= 0:
            problems.append("residual " + rid + " has no positive review_cadence_days")
            cadence = None
        reviewed = _as_date(entry.get("last_reviewed"))
        if reviewed is None:
            problems.append("residual " + rid + " has no valid last_reviewed date")
        elif cadence is not None:
            age = (today - reviewed).days
            if age > cadence:
                problems.append(
                    "residual " + rid + " was last reviewed " + str(age)
                    + " days ago, cadence is " + str(cadence) + " days — the review is DUE"
                )
        if not (entry.get("residual") or "").strip():
            problems.append("residual " + rid + " has no description")
        if not _doc_has_row(residual_doc, rid):
            problems.append("residual " + rid + " has no row in " + RESIDUAL_DOC)

    # S2 members are residual by construction; hold them to the same bar.
    for member in ((scope.get("surfaces") or {}).get("S2") or {}).get("members") or []:
        path = member.get("path", "<unnamed>")
        if not (member.get("residual") or "").strip():
            problems.append("S2 member " + path + " has no residual reason")
        if not (member.get("owner") or "").strip():
            problems.append("S2 member " + path + " has no owner")
        reviewed = _as_date(member.get("last_reviewed"))
        cadence = member.get("review_cadence_days")
        if reviewed is None:
            problems.append("S2 member " + path + " has no valid last_reviewed date")
        elif isinstance(cadence, int) and cadence > 0 and (today - reviewed).days > cadence:
            problems.append(
                "S2 member " + path + " review is DUE ("
                + str((today - reviewed).days) + " days > " + str(cadence) + ")"
            )

    for c in classes:
        if not c.is_residual:
            continue
        row = c.raw.get("residual_row") or c.id
        if not _doc_has_row(residual_doc, str(row)):
            problems.append(
                "live class " + c.id + " has no row in " + RESIDUAL_DOC
                + " — a live class may be undecidable, never invisible"
            )
    return problems


def _doc_has_row(doc: str, row_id: str) -> bool:
    """A markdown table row whose first cell is exactly `row_id`."""
    return re.search(r"^\|\s*`?" + re.escape(row_id) + r"`?\s*\|", doc, re.M) is not None


def check_ci_execution(scope: dict, workflow_text: Optional[str]) -> List[str]:
    """The declared CI map must describe the REAL workflow file.

    Without this the map is just a wish: someone deletes the pwsh job, the map
    still claims tests/local-llm is executed, and C-10 passes while the
    alerting layer's 85 test cases run nowhere.
    """
    problems: List[str] = []
    ci = scope.get("ci_execution") or {}
    if workflow_text is None:
        return ["CI workflow " + str(ci.get("workflow")) + " is missing"]
    try:
        wf = yaml.safe_load(workflow_text) or {}
    except yaml.YAMLError as exc:
        return ["CI workflow is not valid YAML: " + str(exc)]
    jobs = wf.get("jobs") or {}
    for decl in ci.get("jobs") or []:
        name = decl.get("job")
        if name not in jobs:
            problems.append("declared CI job " + repr(name) + " is not in the workflow file")
            continue
        needle = decl.get("must_contain") or ""
        runs = " \n".join(
            str(step.get("run", "")) for step in (jobs[name].get("steps") or [])
        )
        if needle and needle not in runs:
            problems.append(
                "CI job " + str(name) + " no longer runs " + repr(needle)
                + " — the execution map and the workflow disagree"
            )
    return problems


def run_meta_checks(
    repo: Repo,
    ledgers: Ledgers,
    available_detectors: Sequence[str],
    today: Optional[_dt.date] = None,
) -> List[str]:
    """Every meta-check, in one call. Empty list == the auditor is sound."""
    today = today or _dt.date.today()
    problems: List[str] = []
    problems += check_class_detector_bijection(ledgers.classes, available_detectors, repo.tracked)
    problems += check_scope_partition(repo.tracked, ledgers.scope)
    problems += check_waiver_discipline(ledgers.classes)
    problems += check_residual_discipline(
        ledgers.scope, ledgers.classes, repo.read_optional(RESIDUAL_DOC), today
    )
    problems += check_ci_execution(
        ledgers.scope, repo.read_optional((ledgers.scope.get("ci_execution") or {}).get("workflow", ""))
    )
    return problems
