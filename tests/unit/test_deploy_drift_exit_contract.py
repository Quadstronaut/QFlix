"""deploy-drift.sh: the exit-code contract and the unstaged-deployed-file stage.

WHY THIS FILE EXISTS
--------------------
Two distinct defects meet in this one script.

B4 — "the canary found drift" and "the canary could not run" both exited 1, and
lib/cli.py mapped every non-zero rc to 2, so systemd recorded the identical
`Result=exit-code` either way. REA's journal_errors collector keeps
`Failed to start <unit>` lines whose unit is still failed, so a BY-DESIGN drift
red paged as a critical service outage. The source now distinguishes: 20 =
FINDING (I ran, my assertion failed), anything else non-zero = HARNESS ERROR (I
could not run, or could not read the truth I grade against). Severity is not the
axis; "do I trust my own measurement" is.

B3 — byte equality is only half of "deployed". The other half is "can the
installer put it there at all". `60-www-images.sh` sat under
~/scripts/configure/ with no stager: green here for as long as nobody edited it,
and silently un-deployable the moment somebody did (PRs #32/#34/#36/#37).

HOW IT IS TESTED
The stages are executed, not asserted about. The script body is a single-quoted
string handed to `sshm`; this file extracts that body verbatim and runs it under
Git Bash against a synthetic $HOME with real files — so a stage that is present
but a no-op fails, which a text assertion could never catch.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts" / "canaries" / "deploy-drift.sh"
TEXT = CANARY.read_text(encoding="utf-8")


def _resolve_git_bash() -> str | None:
    """GIT BASH EXPLICITLY — a bare `bash` on this workstation is WSL, a
    different filesystem namespace that would ignore the $HOME handed to it and
    make every assertion vacuously green. Same resolver, same reason, as
    tests/unit/test_rea_collect_freshness.py."""
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


def _remote_body() -> str:
    """The single-quoted script `sshm` runs on the box, verbatim."""
    body = TEXT.split("RES=$(sshm '", 1)[1]
    body = body.rsplit("') || RC=$?", 1)[0]
    return body


# ---------------------------------------------------------------------------
# STRUCTURAL — the contract is written down and the exits say what it says
# ---------------------------------------------------------------------------

def test_the_exit_contract_is_documented_in_the_header():
    """AC-12. A contract that lives only in the code is a convention."""
    head = TEXT.split("set -uo pipefail", 1)[0]
    assert "EXIT-CODE CONTRACT" in head
    assert "20" in head and "FINDING" in head and "HARNESS ERROR" in head


@pytest.mark.parametrize("stage", [
    "deploy-drift", "deploy-mode-drift", "deploy-orphan", "unstaged-deployed-file",
])
def test_finding_stages_exit_20(stage):
    """MUTATION: change any of these back to `exit 1` → RED."""
    body = _remote_body()
    # split on a LINE that is exactly `fi` — "files=" also contains "fi".
    block = body.split(f"STAGE={stage} ", 1)[1].split("\nfi\n", 1)[0]
    assert re.search(r"\n\s*exit 20\b", block), f"{stage} must exit 20"


@pytest.mark.parametrize("stage", ["src-missing", "fetch-failed", "installer-unreadable"])
def test_harness_stages_exit_non_20(stage):
    """These mean "I could not read the truth". Grading them as findings would
    make a broken SSH hop look like real drift."""
    body = _remote_body()
    idx = body.index(f"STAGE={stage} ")
    tail = body[idx:idx + 400]
    assert "exit 1" in tail, stage
    assert "exit 20" not in tail.split("exit 1", 1)[0], stage


def test_the_body_carries_no_apostrophes():
    """The whole body rides inside a single-quoted `sshm '…'` string. One
    apostrophe in a comment terminates it and the canary becomes a syntax
    error at run time — on the box, where nothing would notice until the
    monitor went red for the wrong reason."""
    assert "'" not in _remote_body()


def test_the_script_parses():
    if BASH is None:
        pytest.skip("Git Bash not available")
    cp = subprocess.run([BASH, "-n", str(CANARY)], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr


# ---------------------------------------------------------------------------
# BEHAVIOURAL — the unstaged-deployed-file stage, executed
# ---------------------------------------------------------------------------

def _unstaged_stage_script() -> str:
    """Just the new stage, lifted out of the body, with DEPLOYED bound by the
    caller. Extracted rather than retyped so a change to the script changes
    what this test runs."""
    body = _remote_body()
    return body.split("# STAGE unstaged-deployed-file.", 1)[1].split("\nprintf \"PASS:", 1)[0]


def _run_stage(tmp_path: Path, *, configure_files: list[str], staged: list[str] | None,
               installer: bool = True):
    deployed = tmp_path / "scripts"
    (deployed / "configure").mkdir(parents=True)
    for name in configure_files:
        (deployed / "configure" / name).write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    if installer:
        arr = "\n".join(f"  {n}   # reason" for n in (staged or []))
        (deployed / "configure" / "240-maintenance-install.sh").write_text(
            "#!/usr/bin/env bash\n"
            "echo unrelated\n"
            f"STAGED_CONFIGURE=(\n{arr}\n)\n"
            "echo more\n", encoding="utf-8")
    script = (
        'set -uo pipefail\n'
        f'DEPLOYED="{deployed.as_posix()}"\n'
        '# stage under test:\n'
        + _unstaged_stage_script()
        + '\necho STAGE_PASSED\n'
    )
    cp = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
    return cp


@pytest.mark.skipif(BASH is None, reason="Git Bash not available")
def test_an_unstaged_configure_script_is_a_finding(tmp_path):
    """AC-11, the real 2026-09-17 shape: 60-www-images.sh deployed, absent from
    the installer's declared list.

    MUTATION: delete the stage → RED (exit becomes 0 with no finding)."""
    cp = _run_stage(
        tmp_path,
        configure_files=["60-www-images.sh", "55-kometa-install.sh"],
        staged=["55-kometa-install.sh", "240-maintenance-install.sh"])
    assert cp.returncode == 20, cp.stdout + cp.stderr
    assert "STAGE=unstaged-deployed-file" in cp.stderr
    assert "60-www-images.sh" in cp.stderr
    assert "55-kometa-install.sh" not in cp.stderr.split("files=", 1)[1]


@pytest.mark.skipif(BASH is None, reason="Git Bash not available")
def test_a_fully_staged_configure_dir_passes(tmp_path):
    cp = _run_stage(
        tmp_path,
        configure_files=["60-www-images.sh", "55-kometa-install.sh"],
        staged=["55-kometa-install.sh", "240-maintenance-install.sh",
                "60-www-images.sh"])
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "STAGE_PASSED" in cp.stdout
    assert "unstaged-deployed-file" not in cp.stderr


@pytest.mark.skipif(BASH is None, reason="Git Bash not available")
def test_an_unreadable_installer_is_a_harness_error_not_a_clean_bill(tmp_path):
    """"I could not read the list" must never render as "nothing is unstaged".
    Both failure shapes: no installer at all, and an array that parses empty."""
    cp = _run_stage(tmp_path, configure_files=["60-www-images.sh"],
                    staged=None, installer=False)
    assert cp.returncode == 1
    assert "STAGE=installer-unreadable" in cp.stderr

    cp = _run_stage(tmp_path / "b", configure_files=["60-www-images.sh"], staged=[])
    assert cp.returncode == 1
    assert "STAGE=installer-unreadable" in cp.stderr


@pytest.mark.skipif(BASH is None, reason="Git Bash not available")
def test_the_stage_parses_the_real_installers_array(tmp_path):
    """The box-side parser and the python-side parser must agree on the REAL
    file — the canary reads the deployed copy of 240-maintenance-install.sh, so
    a change to the array's formatting that only one parser survives is a live
    blind spot."""
    deployed = tmp_path / "scripts"
    (deployed / "configure").mkdir(parents=True)
    real = REPO / "scripts" / "configure" / "240-maintenance-install.sh"
    shutil.copy(real, deployed / "configure" / "240-maintenance-install.sh")
    for name in ("55-kometa-install.sh", "60-www-images.sh"):
        shutil.copy(REPO / "scripts" / "configure" / name,
                    deployed / "configure" / name)
    script = (
        'set -uo pipefail\n'
        f'DEPLOYED="{deployed.as_posix()}"\n'
        + _unstaged_stage_script()
        + '\necho STAGE_PASSED\n'
    )
    cp = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "STAGE_PASSED" in cp.stdout
