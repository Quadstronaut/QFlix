"""qflix-audit: determinism, digest hygiene, suppression logging, exit codes.

These are the operator-facing properties. If the digest is not stable, the whole
regime collapses back into "run it again and see what you get" — which is the
defect it was built to remove.
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import re

import pytest

from lib.audit.engine import exit_code_for, run
from lib.audit.model import RegimeError, canonical_json, digest
from lib.audit.repo import Repo

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location(
        "qflix_audit_cli", os.path.join(REPO_ROOT, "scripts", "maint", "qflix-audit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def two_runs():
    """AC-1: two in-process runs of the same commit, from two distinct Repo
    objects so no cache is shared between them."""
    return run(Repo(REPO_ROOT)), run(Repo(REPO_ROOT))


# ---------------------------------------------------------------------------
# AC-1 determinism
# ---------------------------------------------------------------------------

def test_two_runs_produce_the_same_digest(two_runs):
    a, b = two_runs
    assert a["report_digest"] == b["report_digest"]
    assert re.fullmatch(r"[0-9a-f]{64}", a["report_digest"])


def test_two_runs_produce_identical_findings_waived_and_coverage(two_runs):
    a, b = two_runs
    for key in ("findings", "waived", "coverage", "summary", "audit_log"):
        assert json.dumps(a[key], sort_keys=True) == json.dumps(b[key], sort_keys=True), key


def test_only_meta_differs_between_runs(two_runs):
    a, b = two_runs
    assert {k: v for k, v in a.items() if k != "meta"} == {
        k: v for k, v in b.items() if k != "meta"}


def test_ordering_is_total_not_incidental(two_runs):
    """Every list the digest covers must be sorted by a key that cannot tie."""
    a, _ = two_runs
    keys = [(f["class"], f["path"], f["lineno"], f["instance_id"], f["kind"])
            for f in a["findings"]]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys), "two findings share an ordering key"


# ---------------------------------------------------------------------------
# AC-2 digest hygiene
# ---------------------------------------------------------------------------

def _digest_body(report):
    """Reconstruct exactly what run() digests, from the report."""
    return {
        "schema": report["meta"]["schema"],
        "classes": sorted(
            [{
                "id": c["class"], "status": c["status"], "boundary": c["boundary"],
                "boundary_size": c["boundary_size"], "tally": c["tally"],
                "metrics": c["metrics"],
                "findings": [
                    {k: f[k] for k in
                     ("instance_id", "path", "lineno", "kind", "severity", "detail")}
                    for f in report["findings"] if f["class"] == c["class"]],
                "waived": [{"instance_id": w["instance_id"], "waiver": w["waiver"]}
                           for w in report["waived"] if w["class"] == c["class"]],
            } for c in report["coverage"]],
            key=lambda c: c["id"]),
        "residual_classes": sorted(
            c["class"] for c in report["coverage"] if c["status"] == "residual"),
    }


def test_the_reconstructed_body_hashes_to_the_published_digest(two_runs):
    """Proves the digest really is over the published body — not over some
    private structure the operator cannot inspect."""
    a, _ = two_runs
    body = _digest_body(a)
    body["residual_classes"] = sorted(
        c.split()[0] for c in _residual_ids())
    assert digest(body) == a["report_digest"]


def _residual_ids():
    import yaml
    with open(os.path.join(REPO_ROOT, "manifest", "defect-classes.yaml"),
              encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return sorted(c["id"] for c in data["classes"] if c["status"] == "residual")


DIRTY_PATTERNS = {
    "windows-absolute-path": r"[A-Za-z]:[\\/]",
    "posix-absolute-path": r'(?:^|["\s(\[])/(?:home|root|usr|var|etc|tmp|opt|mnt|srv)/',
    "home-relative-path": r"~/",
    "iso-timestamp": r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}",
    "clock-time": r"\b\d{2}:\d{2}:\d{2}\b",
    "pid": r"(?i)\bpid[=: ]\s*\d+",
    "hostname": r"(?i)\b[a-z0-9][a-z0-9-]*\.(?:me|com|net|org|app|io|dev|local|lan|internal)\b",
    "ip-literal": r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
}


def test_the_digested_body_is_clean(two_runs):
    a, _ = two_runs
    body = _digest_body(a)
    body["residual_classes"] = _residual_ids()
    canonical = canonical_json(body)
    for name, pattern in DIRTY_PATTERNS.items():
        m = re.search(pattern, canonical)
        assert m is None, name + " leaked into the digest: " + repr(m.group(0))


def test_hygiene_is_enforced_in_production_not_only_in_this_test():
    """A detector that leaks a timestamp must break the AUDIT, not just this
    test file — otherwise the invariant only holds where somebody remembered
    to check."""
    with pytest.raises(RegimeError) as exc:
        digest({"detail": "collected at 2026-07-29T04:00:00Z"})
    assert "digest hygiene violated" in str(exc.value)
    with pytest.raises(RegimeError):
        digest({"detail": "/home/operator/secrets"})
    with pytest.raises(RegimeError):
        digest({"detail": "seedbox.example.com"})
    with pytest.raises(RegimeError):
        digest({"detail": r"C:\Users\thing"})
    with pytest.raises(RegimeError):
        digest({"detail": "pid=4321"})


def test_generated_at_exists_and_provably_does_not_affect_the_digest():
    a = run(Repo(REPO_ROOT), generated_at="2026-01-01T00:00:00Z")
    b = run(Repo(REPO_ROOT), generated_at="2099-12-31T23:59:59Z")
    assert a["meta"]["generated_at"] != b["meta"]["generated_at"]
    assert a["report_digest"] == b["report_digest"]


def test_meta_declares_what_the_digest_excludes(two_runs):
    a, _ = two_runs
    assert a["meta"]["digest_excludes"] == ["meta", "audit_log"]
    assert a["meta"]["generated_at"]
    assert a["meta"]["host"]


# ---------------------------------------------------------------------------
# AC-13 suppression is logged
# ---------------------------------------------------------------------------

def test_every_waived_instance_appears_in_both_the_array_and_the_log(two_runs):
    a, _ = two_runs
    assert a["waived"], (
        "no waivers are exercised — this guard would pass vacuously. Keep at "
        "least one real waiver in the ledger.")
    for w in a["waived"]:
        assert w["reason"] and len(w["reason"]) >= 40
        assert w["waiver"] and w["owner"] and w["date"]
        line = [ln for ln in a["audit_log"] if w["instance_id"] in ln]
        assert line, "waived instance missing from the audit log: " + w["instance_id"]
        assert "class=" + w["class"] in line[0]
        assert "waiver=" + w["waiver"] in line[0]


def test_a_waived_instance_is_never_merely_absent(two_runs):
    a, _ = two_runs
    waived_ids = {w["instance_id"] for w in a["waived"]}
    finding_ids = {f["instance_id"] for f in a["findings"]}
    assert waived_ids and not (waived_ids & finding_ids)
    for c in a["coverage"]:
        n = sum(1 for w in a["waived"] if w["class"] == c["class"])
        assert c["tally"]["waived"] == n


def test_waived_counts_toward_the_boundary_not_out_of_it(two_runs):
    """A suppression must not shrink the coverage denominator — that would let
    waiving things look like auditing more of them."""
    a, _ = two_runs
    for c in a["coverage"]:
        assert sum(c["tally"].values()) <= c["boundary_size"] or c["boundary_size"] == 0


# ---------------------------------------------------------------------------
# Exit codes + CLI surface
# ---------------------------------------------------------------------------

def test_exit_code_is_zero_with_no_enforced_findings(two_runs):
    a, _ = two_runs
    assert a["summary"]["findings_enforced"] == 0
    assert exit_code_for(a) == 0


def test_exit_code_is_one_when_an_enforced_finding_exists():
    fake = {"summary": {"findings_enforced": 1}}
    assert exit_code_for(fake) == 1


def test_cli_json_mode_round_trips(cli, capsys):
    code = cli.main(["--root", REPO_ROOT, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert re.fullmatch(r"[0-9a-f]{64}", out["report_digest"])
    for key in ("meta", "coverage", "findings", "waived", "audit_log", "summary"):
        assert key in out


def test_cli_digest_only_prints_one_line(cli, capsys):
    cli.main(["--root", REPO_ROOT, "--digest-only"])
    out = capsys.readouterr().out.strip().split("\n")
    assert len(out) == 1
    assert re.fullmatch(r"[0-9a-f]{64}", out[0])


def test_cli_human_mode_names_every_class(cli, capsys):
    cli.main(["--root", REPO_ROOT])
    out = capsys.readouterr().out
    for cid in ("C-01", "C-05", "C-10"):
        assert cid in out
    assert "docs/audit-residual-risk.md" in out


def test_cli_does_not_write_outside_its_own_log_dir(cli, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QFLIX_AUDIT_LOG_DIR", str(tmp_path / "auditlog"))
    cli.main(["--root", REPO_ROOT, "--digest-only"])
    capsys.readouterr()
    written = list((tmp_path / "auditlog").iterdir())
    assert [p.name for p in written] == ["qflix-audit.log"]
    body = written[0].read_text(encoding="utf-8")
    assert "digest=" in body
    assert "waived class=" in body


def test_missing_kuma_token_is_reported_not_swallowed(cli, tmp_path, monkeypatch, capsys):
    """The auditor must not be an instance of C-09."""
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path / "no-secrets-here"))
    monkeypatch.delenv("QFLIX_AUDIT_KUMA_TOKEN", raising=False)
    cli._push_kuma("up", "test")
    err = capsys.readouterr().err
    assert "no Kuma push token" in err
    assert "heartbeat NOT pushed" in err


def test_cadence_date_never_reaches_the_digest():
    """--today feeds the residual-cadence gate only. If it touched the digest,
    the digest would churn on the calendar."""
    a = run(Repo(REPO_ROOT), today=_dt.date(2026, 7, 29))
    b = run(Repo(REPO_ROOT), today=_dt.date(2026, 8, 15))
    assert a["report_digest"] == b["report_digest"]


def test_no_detector_imports_anything_that_could_reach_out():
    """Structural determinism guard, checked on IMPORTS via AST rather than on
    raw text — a docstring that explains why `requests` is avoided must not
    read as a violation of the very rule it documents.

    A detector that could open a socket, shell out, or read a secret could
    produce a different answer on a different day, which is the one property
    this regime cannot afford to lose.
    """
    import ast as _ast

    import lib.audit.detectors as pkg
    forbidden = {"requests", "urllib", "socket", "http", "subprocess",
                 "paramiko", "sqlite3", "random", "time", "datetime"}
    for name in pkg.available():
        mod = pkg.load(name)
        tree = _ast.parse(open(mod.__file__, encoding="utf-8").read())
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                mods = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, _ast.ImportFrom):
                mods = {(node.module or "").split(".")[0]}
            else:
                continue
            bad = mods & forbidden
            assert not bad, name + " imports " + ", ".join(sorted(bad))
