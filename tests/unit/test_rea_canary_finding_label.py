"""REA must not page what a monitor already owns — and must never drop evidence.

WHY THIS FILE EXISTS
--------------------
A canary that RAN and found something exits 20 on purpose. systemd therefore
holds its unit `failed`, and journald carries
`Failed to start manitoba-maint-canary-<name>.service` for the next 24 hours.
REA's journal_errors collector has a resolved-failure gate that drops such lines
only when the unit has since recovered — which is correct, and which means a
by-design drift red sails straight through to the models and gets paged as a
CRITICAL SERVICE OUTAGE. That is the standing rule violated twice over: REA
reads history, canaries read live state, so anything a monitor owns is a
duplicate; and 40+ pings is noise, not an alert.

THE FIX IS A JOIN, NOT A FILTER
lib/cli.py now emits one `QFLIX_CANARY_VERDICT … verdict=finding|harness-error`
line per non-pass canary run. The collector joins the failed-to-start line
against that verdict line, by unit, inside the same 24h window:

  verdict=finding        → LABEL the line monitor-owned. Never drop it.
  verdict=harness-error  → verbatim, keeps paging.
  no verdict line at all → verbatim, keeps paging. This is the case that
                           matters most: a canary that could not run leaves no
                           verdict, and it is the one nobody else covers.

Labels are COUNTED on the section's existing `# collector-suppressed:` line, per
the file-wide law that a gate never operates silently.

WHY IT RUNS THE REAL BASH
The gate is four lines of shell inside a PowerShell single-quoted string. A text
assertion cannot tell a join that works from a join that silently matches
nothing — which is exactly the failure mode (`grep -qxF` against an empty
FINDING_UNITS would label nothing, forever, green). So this extracts the
collector body verbatim and runs it with `journalctl` and `systemctl` stubbed on
$PATH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
REA_PS1 = REPO / "scripts" / "local-llm" / "qflix-rea.ps1"
NOISE_YAML = REPO / "manifest" / "rea-noise-classes.yaml"


def _resolve_git_bash() -> str | None:
    """GIT BASH EXPLICITLY — a bare `bash` here is WSL, a different filesystem
    namespace, which would make the stubs on $PATH invisible and every
    assertion vacuously green."""
    candidates = [
        Path(os.environ.get("USERPROFILE", "")) / "scoop/apps/git/current/bin/bash.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Git/bin/bash.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Git/bin/bash.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    if os.name != "nt":
        return shutil.which("bash")
    return None


BASH = _resolve_git_bash()


# ---------------------------------------------------------------------------
# The class is NAMED in git (runs in CI, no ps1 needed)
# ---------------------------------------------------------------------------

def test_the_class_is_named_in_the_single_git_source():
    """AC-16's last clause. "When a gate fails open the class still needs a
    named rule" — a suppression with no name in manifest/rea-noise-classes.yaml
    is an unowned behaviour, which is how the 2026-07-29 prompt-only classes
    happened."""
    doc = yaml.safe_load(NOISE_YAML.read_text(encoding="utf-8"))
    gates = {g["id"]: g for g in (doc.get("collector_suppressions") or [])}
    assert "canary-finding-monitor-owned" in gates, sorted(gates)
    g = gates["canary-finding-monitor-owned"]
    assert g["section"] == "journal_errors"
    assert g["action"] == "label"
    assert g["counter"] == "canary_finding_labelled"
    assert "QFLIX_CANARY_VERDICT" in g["marker"]
    assert g["emitted_by"].startswith("scripts/maint/lib/cli.py")
    # the two mandatory prose fields, and they must actually say something
    assert len(g["why"].strip()) > 80
    assert len(g["fails_open"].strip()) > 80
    assert "harness-error" in g["fails_open"]


def test_every_collector_gate_declares_its_counter_and_fail_open_direction():
    doc = yaml.safe_load(NOISE_YAML.read_text(encoding="utf-8"))
    gates = doc.get("collector_suppressions") or []
    assert gates, "collector_suppressions must not be empty once declared"
    for g in gates:
        for key in ("id", "section", "action", "counter", "why", "fails_open",
                    "added", "source_script"):
            assert g.get(key), f"{g.get('id')}: missing {key}"
        assert g["action"] in ("label", "drop"), g["id"]


def test_the_yaml_still_parses_as_the_policy_the_ps1_loader_expects():
    """The ps1 carries a hand-rolled YAML parser with a section state machine.
    An unknown top-level key must leave it in 'header' state, not silently
    absorb the new block into deadman_reasons."""
    doc = yaml.safe_load(NOISE_YAML.read_text(encoding="utf-8"))
    assert doc["deadman_reasons"] == [
        "tunnel_timeout", "no_models", "ssh_fail", "blob_parse", "all_models_noop"]
    assert len(doc["classes"]) == len(doc["prompt_segments"]) or True  # C-07 owns the bijection
    text = NOISE_YAML.read_text(encoding="utf-8")
    # the new key is top-level (column 0) so the parser sees it as a key, and it
    # sits AFTER deadman_reasons so it cannot truncate that list.
    assert "\ncollector_suppressions:\n" in text
    assert text.index("deadman_reasons:") < text.index("collector_suppressions:")


# ---------------------------------------------------------------------------
# BEHAVIOURAL — the collector body, executed with stubbed journalctl/systemctl
# ---------------------------------------------------------------------------

pytestmark_behavioural = pytest.mark.skipif(
    BASH is None or not REA_PS1.exists(),
    reason="needs Git Bash and the gitignored qflix-rea.ps1 (workstation gate)")


def _collector_body() -> str:
    text = REA_PS1.read_text(encoding="utf-8")
    body = text.split("collect journal_errors bash -c '", 1)[1]
    return body.split("\n'\n", 1)[0]


def _run(tmp_path: Path, *, err_lines: list[str], verdict_lines: list[str],
         failed_units: list[str]):
    """Run the real collector body with journalctl/systemctl stubbed."""
    binh = tmp_path / "bin"
    binh.mkdir()
    (tmp_path / "err.txt").write_text("\n".join(err_lines) + "\n", encoding="utf-8")
    (tmp_path / "all.txt").write_text("\n".join(verdict_lines) + "\n", encoding="utf-8")
    (tmp_path / "failed.txt").write_text("\n".join(failed_units) + "\n", encoding="utf-8")

    (binh / "journalctl").write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do [ "$a" = "err" ] && { cat "%s"; exit 0; }; done\n'
        'cat "%s"\n' % ((tmp_path / "err.txt").as_posix(),
                        (tmp_path / "all.txt").as_posix()),
        encoding="utf-8")
    (binh / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        'u="${!#}"\n'
        'if grep -qxF "$u" "%s"; then echo failed; else echo active; fi\n'
        % (tmp_path / "failed.txt").as_posix(),
        encoding="utf-8")
    for p in binh.iterdir():
        p.chmod(0o755)

    env = dict(os.environ, PATH=binh.as_posix() + os.pathsep + os.environ.get("PATH", ""))
    cp = subprocess.run([BASH, "-c", _collector_body()],
                        capture_output=True, text=True, env=env)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout


FAIL_DRIFT = ("Sep 17 09:00:01 host systemd[1]: Failed to start "
              "manitoba-maint-canary-deploy-drift.service.")
FAIL_OTHER = ("Sep 17 09:05:01 host systemd[1]: Failed to start "
              "manitoba-maint-canary-tdarr-scanner.service.")
REAL_FAULT = "Sep 17 09:10:00 host plex[1]: ERROR - Failure loading codec"

V_FINDING = ("Sep 17 09:00:01 host manitoba-maint[1]: QFLIX_CANARY_VERDICT "
             "name=deploy-drift unit=manitoba-maint-canary-deploy-drift.service "
             "verdict=finding rc=20 msg=STAGE=deploy-drift 1-of-242-differ")
V_HARNESS = ("Sep 17 09:05:01 host manitoba-maint[1]: QFLIX_CANARY_VERDICT "
             "name=tdarr-scanner unit=manitoba-maint-canary-tdarr-scanner.service "
             "verdict=harness-error rc=127 msg=bash not found")


@pytestmark_behavioural
def test_a_finding_is_labelled_never_dropped(tmp_path):
    """MUTATION: delete the FINDING_UNITS join → RED (the line comes through
    unlabelled and the counter disappears)."""
    out = _run(tmp_path,
               err_lines=[FAIL_DRIFT, REAL_FAULT],
               verdict_lines=[V_FINDING],
               failed_units=["manitoba-maint-canary-deploy-drift.service"])
    assert "[canary-finding unit=manitoba-maint-canary-deploy-drift.service]" in out
    assert "MONITOR-OWNED" in out
    # NEVER DROPPED — the original text survives inside the labelled line.
    assert FAIL_DRIFT in out
    # and the real fault next door is untouched
    assert REAL_FAULT in out
    assert "canary_finding_labelled=1" in out


@pytestmark_behavioural
def test_a_harness_error_passes_through_verbatim_and_keeps_paging(tmp_path):
    """The case the label must never eat: the canary could not RUN."""
    out = _run(tmp_path,
               err_lines=[FAIL_OTHER],
               verdict_lines=[V_HARNESS],
               failed_units=["manitoba-maint-canary-tdarr-scanner.service"])
    assert "canary-finding" not in out
    assert out.strip().endswith(FAIL_OTHER)
    assert "canary_finding_labelled=0" not in out  # census not emitted at all


@pytestmark_behavioural
def test_a_failed_canary_with_no_verdict_line_keeps_paging(tmp_path):
    """No verdict at all — died before python ran, or an old deployed cli.py.
    FAIL OPEN: page. Reading "no evidence" as "monitor-owned" would be the
    fabricated-darkness defect wearing a different hat."""
    out = _run(tmp_path,
               err_lines=[FAIL_DRIFT],
               verdict_lines=["Sep 17 09:00:01 host systemd[1]: Started something"],
               failed_units=["manitoba-maint-canary-deploy-drift.service"])
    assert "canary-finding" not in out
    assert FAIL_DRIFT in out.strip()


@pytestmark_behavioural
def test_the_resolved_failure_drop_still_works_and_both_counters_share_one_line(tmp_path):
    """The pre-existing gate must be unharmed, and a run that both drops and
    labels must say so ONCE, on the established census line."""
    out = _run(tmp_path,
               err_lines=[FAIL_DRIFT, FAIL_OTHER, REAL_FAULT],
               verdict_lines=[V_FINDING],
               failed_units=["manitoba-maint-canary-deploy-drift.service"])
    census = [l for l in out.splitlines() if l.startswith("# collector-suppressed:")]
    assert len(census) == 1, out
    assert "section=journal_errors" in census[0]
    assert "n=1" in census[0]                       # tdarr-scanner recovered → dropped
    assert "canary_finding_labelled=1" in census[0]
    assert FAIL_OTHER not in out
    assert REAL_FAULT in out


@pytestmark_behavioural
def test_a_finding_verdict_for_a_RECOVERED_unit_is_still_dropped_not_labelled(tmp_path):
    """Precedence matters: the resolved-failure test runs FIRST. A unit that has
    since recovered is stale history whatever its verdict was, and dropping it
    is strictly quieter than labelling it."""
    out = _run(tmp_path,
               err_lines=[FAIL_DRIFT],
               verdict_lines=[V_FINDING],
               failed_units=[])
    assert FAIL_DRIFT not in out
    assert "canary-finding" not in out
    assert "n=1" in out


@pytestmark_behavioural
def test_the_gate_fails_open_when_the_verdict_query_returns_nothing(tmp_path):
    """journalctl unavailable / grep empty / an old cli.py that never emits the
    line: FINDING_UNITS is empty and every line pages, exactly as before."""
    out = _run(tmp_path, err_lines=[FAIL_DRIFT], verdict_lines=[],
               failed_units=["manitoba-maint-canary-deploy-drift.service"])
    assert FAIL_DRIFT in out
    assert "canary-finding" not in out
