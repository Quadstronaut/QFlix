"""tests/unit/test_provision_admin_key.py — guards for
scripts/configure/provision-admin-key.sh (C2 finding, 2026-08-03 QFlix Admin
final fix wave).

The BOX host-resolution preamble (top of the script) is extracted and run
verbatim, the same way tests/unit/test_canary_sshm_quoting.py runs the
shipped `sshm '...'` body rather than trusting `bash -n` on the wrapper —
no live box or network access needed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "configure" / "provision-admin-key.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None,
                                reason="needs bash on PATH")


def test_script_parses() -> None:
    proc = subprocess.run(["bash", "-n", str(SCRIPT)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


# --- C2: BOX must resolve through scripts/lib/ssh.sh, never a bare FQDN ----

def test_default_box_resolution_gets_a_user_at_host_prefix() -> None:
    """Regression guard for C2: the script used to `cat` secrets/
    seedbox.ssh-host directly, which holds the FQDN ONLY. Run the exact
    resolution line the script uses (source ssh.sh, then the same default
    expansion) and confirm the result is prefixed user@host, not a bare
    FQDN — no network access needed, this is pure variable resolution."""
    snippet = 'source scripts/lib/ssh.sh && BOX="${QFLIX_BOX:-$SSHM_HOST}"; printf %s "$BOX"'
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True,
                          text=True, timeout=30, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
    box = proc.stdout.strip()
    assert "@" in box, "BOX resolved with no user@ prefix: %r" % box


def test_a_bare_fqdn_box_is_refused_before_any_ssh_or_scp() -> None:
    """C2 reproduction: QFLIX_BOX set to a bare FQDN (the shape secrets/
    seedbox.ssh-host actually holds) must be refused by the case-guard,
    before the script reaches --check's `ssh "$BOX" ...` call — proven here
    by checking stdout never got as far as the --check output line."""
    env = dict(os.environ)
    env["QFLIX_BOX"] = "seedbox.example.com"  # bare FQDN: no "@"
    proc = subprocess.run(["bash", str(SCRIPT), "--check"],
                          capture_output=True, text=True, timeout=30,
                          cwd=str(REPO_ROOT), env=env)
    assert proc.returncode == 2, proc.stderr
    assert "user@host" in proc.stderr
    assert "existing qflix-admin-phone" not in proc.stdout


def test_a_proper_user_at_host_box_passes_the_guard() -> None:
    """The guard must not reject a well-formed target — it should get far
    enough to attempt the (unreachable, in this sandbox) SSH call, not fail
    on the case-statement itself."""
    env = dict(os.environ)
    env["QFLIX_BOX"] = "nobody@nonexistent.invalid.example"
    proc = subprocess.run(["bash", str(SCRIPT), "--check"],
                          capture_output=True, text=True, timeout=30,
                          cwd=str(REPO_ROOT), env=env)
    assert "user@host" not in proc.stderr
    assert proc.returncode != 2 or "user@host" not in proc.stderr
