"""QFLX-24: the generic-host CI job exists, is registered in audit-scope, and is not required (D-8)."""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WF = yaml.safe_load((REPO / ".github/workflows/tests.yml").read_text())
SCOPE = yaml.safe_load((REPO / "manifest/audit-scope.yaml").read_text())


def _runs(job):
    return "\n".join(s.get("run", "") for s in WF["jobs"][job]["steps"])


def test_job_exists_and_runs_smoke_and_shellcheck():
    job = WF["jobs"]["generic-host"]
    assert job["runs-on"].startswith("ubuntu")
    runs = _runs("generic-host")
    assert "enable-linger" in runs
    assert "tests/ci/test_generic_host_smoke.py" in runs
    assert "shellcheck" in runs and "scripts/lib/" in runs


def test_registered_in_audit_scope():
    entry = {j["job"]: j for j in SCOPE["ci_execution"]["jobs"]}["generic-host"]
    assert entry["must_contain"] in _runs("generic-host")
    assert "tests/ci/**" in entry["executes"]


def test_smoke_file_exists_and_nothing_needs_the_job():
    assert (REPO / "tests/ci/test_generic_host_smoke.py").is_file()
    for name, job in WF["jobs"].items():
        assert "generic-host" not in (job.get("needs") or []), name
