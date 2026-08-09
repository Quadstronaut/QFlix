"""lib/audit/engine.py — run the detectors, apply waivers, emit the report.

The engine's whole job is to be boring and total: sorted in, sorted out, no
clock in the compared body, no path outside the repo, and an exception rather
than a guess whenever the ledgers and the code disagree.
"""
from __future__ import annotations

import datetime as _dt
import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import detectors as _detectors
from .ledger import Ledgers, load as load_ledgers, run_meta_checks
from .model import (ADVISORY, ENFORCED, FINDING, OK, WAIVED, DetectorResult,
                    RegimeError, Verdict, digest)
from .repo import Repo, find_repo_root

SCHEMA = 1

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_REGIME = 2


@dataclass
class Ctx:
    """What a detector gets. Deliberately tiny: a repo and the ledgers.

    No network client, no clock, no secrets. A detector that cannot reach any
    of those cannot accidentally become non-deterministic.
    """
    repo: Repo
    ledgers: Ledgers


def head_commit(repo: Repo) -> str:
    """Short HEAD sha, or "unknown". Metadata only — never digested, so a
    detached/exported tree still produces a comparable digest."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo.root), capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return proc.stdout.decode("ascii", "replace").strip() or "unknown"


def _s2_presence(repo: Repo, ledgers: Ledgers) -> Dict[str, bool]:
    """{S2 member path -> is it on THIS machine}. Reported, never digested."""
    s2 = (ledgers.scope.get("surfaces") or {}).get("S2") or {}
    return {
        m["path"]: repo.exists(m["path"])
        for m in sorted(s2.get("members") or [], key=lambda m: m.get("path", ""))
        if m.get("path")
    }


def _waiver_matches(waiver: dict, cls_id: str, v: Verdict) -> bool:
    """A waiver selector is a conjunction over verdict fields.

    Empty selectors are rejected at ledger-validation time, so a waiver can
    never match everything by accident.
    """
    sel = waiver.get("match") or {}
    for key, want in sel.items():
        if key == "instance_id" and v.instance_id != want:
            return False
        if key == "path" and v.path != want:
            return False
        if key == "kind" and v.kind != want:
            return False
        if key == "lineno" and v.lineno != want:
            return False
        if key not in {"instance_id", "path", "kind", "lineno"}:
            raise RegimeError(
                "waiver " + str(waiver.get("id")) + " (class " + cls_id
                + ") selects on unknown field " + repr(key)
            )
    return True


def run(
    repo: Optional[Repo] = None,
    *,
    today: Optional[_dt.date] = None,
    generated_at: Optional[str] = None,
    skip_meta: bool = False,
) -> Dict[str, Any]:
    """Run the full audit. Raises RegimeError (=> exit 2) if the auditor is
    broken; otherwise returns the report dict."""
    repo = repo or Repo(find_repo_root())
    ledgers = load_ledgers(repo)
    available = _detectors.available()

    if not skip_meta:
        problems = run_meta_checks(repo, ledgers, available, today=today)
        if problems:
            raise RegimeError(
                "REGIME INTEGRITY: " + str(len(problems)) + " problem(s)\n  - "
                + "\n  - ".join(problems)
            )

    ctx = Ctx(repo=repo, ledgers=ledgers)

    coverage: List[dict] = []
    findings: List[dict] = []
    waived: List[dict] = []
    audit_log: List[str] = []
    digest_classes: List[dict] = []

    for cls in sorted(ledgers.offline_classes, key=lambda c: c.id):
        mod = _detectors.load(cls.detector)
        result = mod.detect(ctx)
        if not isinstance(result, DetectorResult):
            raise RegimeError("detector " + cls.detector + " did not return a DetectorResult")

        declared_kinds = set(cls.enforced_kinds) | set(cls.advisory_kinds)
        tally = {OK: 0, FINDING: 0, WAIVED: 0}
        cls_findings: List[dict] = []
        cls_waived: List[dict] = []

        for v in result.sorted_verdicts():
            if v.status == OK:
                tally[OK] += 1
                continue
            if v.status != FINDING:
                raise RegimeError(
                    "detector " + cls.detector + " emitted status " + repr(v.status)
                    + "; detectors emit ok/finding only — waiving is the engine's job"
                )
            if v.kind not in declared_kinds:
                raise RegimeError(
                    "detector " + cls.detector + " emitted finding kind " + repr(v.kind)
                    + " which class " + cls.id + " does not declare in enforced_kinds/"
                    "advisory_kinds — the ledger and the code disagree"
                )
            hit = next(
                (w for w in cls.waivers if _waiver_matches(w, cls.id, v)), None
            )
            if hit is not None:
                tally[WAIVED] += 1
                row = {
                    "class": cls.id,
                    "instance_id": v.instance_id,
                    "path": v.path,
                    "lineno": v.lineno,
                    "kind": v.kind,
                    "detail": v.detail,
                    "waiver": hit.get("id"),
                    "reason": (hit.get("reason") or "").strip(),
                    "date": str(hit.get("date")),
                    "owner": hit.get("owner"),
                }
                cls_waived.append(row)
                # Second design law: every suppression is WRITTEN, with its rule
                # id. A waived instance is never merely absent from the output.
                audit_log.append(
                    "waived class=" + cls.id + " waiver=" + str(hit.get("id"))
                    + " instance=" + v.instance_id + " reason=" + (hit.get("reason") or "").strip()
                )
                continue
            tally[FINDING] += 1
            severity = (
                ENFORCED if (cls.status == "enforced" and v.kind in set(cls.enforced_kinds))
                else ADVISORY
            )
            cls_findings.append({
                "class": cls.id,
                "instance_id": v.instance_id,
                "path": v.path,
                "lineno": v.lineno,
                "kind": v.kind,
                "severity": severity,
                "detail": v.detail,
            })

        cls_findings.sort(key=lambda f: (f["path"], f["lineno"], f["instance_id"], f["kind"]))
        cls_waived.sort(key=lambda f: (f["path"], f["lineno"], f["instance_id"], f["kind"]))
        findings.extend(cls_findings)
        waived.extend(cls_waived)

        cov = {
            "class": cls.id,
            "title": cls.title,
            "status": cls.status,
            "detector": cls.detector,
            "boundary": result.boundary_name,
            "boundary_size": result.boundary_size,
            "tally": dict(tally),
            "metrics": dict(sorted(result.metrics.items())),
        }
        coverage.append(cov)

        digest_classes.append({
            "id": cls.id,
            "status": cls.status,
            "boundary": result.boundary_name,
            "boundary_size": result.boundary_size,
            "tally": dict(tally),
            "metrics": dict(sorted(result.metrics.items())),
            "findings": [
                {k: f[k] for k in ("instance_id", "path", "lineno", "kind", "severity", "detail")}
                for f in cls_findings
            ],
            # Waiver free text is NOT digested — it is git-tracked ledger data
            # and a commit SHA already covers it. What IS digested is that this
            # exact instance was suppressed by this exact waiver id, so a
            # suppression can never be invisible to the one-hex-string check.
            "waived": [
                {"instance_id": w["instance_id"], "waiver": w["waiver"]} for w in cls_waived
            ],
        })

    # Residual classes are listed in the digest too: dropping L-03 from the
    # ledger MUST change the digest, or "we quietly stopped tracking the live
    # side" would be invisible.
    residual_ids = sorted(c.id for c in ledgers.classes if c.is_residual)

    digest_body = {
        "schema": SCHEMA,
        "classes": sorted(digest_classes, key=lambda c: c["id"]),
        "residual_classes": residual_ids,
    }
    report_digest = digest(digest_body)

    enforced_count = sum(1 for f in findings if f["severity"] == ENFORCED)
    advisory_count = len(findings) - enforced_count

    report = {
        "meta": {
            "schema": SCHEMA,
            # Everything in meta is EXCLUDED from report_digest. Changing any
            # of it must not change the hex string — that is what makes the
            # string comparable by eye across runs and across machines.
            "generated_at": generated_at or _dt.datetime.now(_dt.timezone.utc)
                                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": platform.node() or "unknown",
            "commit": head_commit(repo),
            "digest_excludes": ["meta", "audit_log"],
            "detectors_run": sorted(c.detector for c in ledgers.offline_classes),
            # WHICH S2 SUBJECTS THIS MACHINE CAN SEE. Environment, not code —
            # so it lives here, outside the digest. An absent subject means the
            # cross-check for it DID NOT RUN on this host (residual R4); the
            # skip is reported, never silent, and it does not perturb the one
            # hex string the operator compares.
            "s2_subjects": _s2_presence(repo, ledgers),
        },
        "coverage": sorted(coverage, key=lambda c: c["class"]),
        "findings": sorted(findings, key=lambda f: (f["class"], f["path"], f["lineno"],
                                                    f["instance_id"], f["kind"])),
        "waived": sorted(waived, key=lambda w: (w["class"], w["path"], w["lineno"],
                                                w["instance_id"], w["kind"])),
        "audit_log": sorted(audit_log),
        "summary": {
            "classes_enrolled": len(ledgers.classes),
            "classes_offline": len(ledgers.offline_classes),
            "classes_residual": len(residual_ids),
            "instances_enumerated": sum(c["boundary_size"] for c in coverage),
            "findings_enforced": enforced_count,
            "findings_advisory": advisory_count,
            "waived": len(waived),
        },
        "report_digest": report_digest,
    }
    return report


def exit_code_for(report: Dict[str, Any]) -> int:
    return EXIT_FINDINGS if report["summary"]["findings_enforced"] else EXIT_CLEAN
