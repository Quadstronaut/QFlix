"""tests/unit/test_provision_admin_key.py — guards for
scripts/configure/provision-admin-key.sh (C2, I1/I2 findings, 2026-08-03
QFlix Admin final fix wave).

Two things are exercised, both without a live box or network access:

  1. The BOX host-resolution preamble (top of the script) — extracted and run
     verbatim, the same way tests/unit/test_canary_sshm_quoting.py runs the
     shipped `sshm '...'` body rather than trusting `bash -n` on the wrapper.
  2. The REMOTE `<<'REMOTE' ... REMOTE` heredoc body — extracted and run
     against a throwaway $HOME/.ssh/authorized_keys, so the backup/append/
     invariant/restore logic gets real assertions instead of a read-through.
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


def _remote_body() -> str:
    """The exact text between <<'REMOTE' and the closing REMOTE line — what
    actually ships to the box, not the wrapper script around it."""
    text = SCRIPT.read_text(encoding="utf-8")
    start_marker = "<<'REMOTE'\n"
    start = text.index(start_marker) + len(start_marker)
    end = text.index("\nREMOTE\n", start)
    return text[start:end]


def test_script_parses() -> None:
    proc = subprocess.run(["bash", "-n", str(SCRIPT)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


def test_there_is_a_remote_body_to_check() -> None:
    """Guards the guard: if the heredoc delimiter ever changes, every test
    below would silently stop checking anything."""
    body = _remote_body()
    assert "authorized_keys" in body
    assert "restore_and_exit" in body


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
    # example.com is on the repo's PII-scanner allowlist (tests/unit/
    # test_no_pii_in_repo.py ALLOWED_DOMAINS) - any other user@host-shaped
    # placeholder here reads as a personal email address to that scanner.
    env["QFLIX_BOX"] = "nobody@example.com"
    proc = subprocess.run(["bash", str(SCRIPT), "--check"],
                          capture_output=True, text=True, timeout=30,
                          cwd=str(REPO_ROOT), env=env)
    assert "user@host" not in proc.stderr
    assert proc.returncode != 2 or "user@host" not in proc.stderr


# --- I1/I2: backup verified, newline normalized, invariant failure restores ---

def _fresh_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    return home


def _run_body(home: Path, *, opts: str, pub: str, prelude: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["OPTS"] = opts
    env["PUB"] = pub
    script_input = prelude + _remote_body()
    return subprocess.run(["bash", "-s"], input=script_input,
                          capture_output=True, text=True, timeout=30, env=env)


def _real_keypair(tmp_path: Path, name: str) -> str:
    """A real ed25519 pubkey line, so ssh-keygen -l has something genuine to
    count — a synthetic base64-looking string may not parse as a valid key
    and would make the "valid-key delta" invariant meaningless."""
    keyfile = tmp_path / name
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", name, "-f", str(keyfile)],
                  capture_output=True, text=True, timeout=30, check=True)
    return (keyfile.with_suffix(".pub")).read_text(encoding="utf-8").strip()


def test_happy_path_appends_exactly_one_line_and_keeps_the_backup(tmp_path) -> None:
    home = _fresh_home(tmp_path)
    old_pub = _real_keypair(tmp_path, "old")
    new_pub = _real_keypair(tmp_path, "new")
    ak = home / ".ssh" / "authorized_keys"
    ak.write_text(old_pub + "\n", encoding="utf-8", newline="\n")

    proc = _run_body(home, opts='command="x",restrict', pub=new_pub)
    assert proc.returncode == 0, proc.stderr

    lines = ak.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == old_pub, "the pre-existing key must survive byte-identical"
    backups = list((home / ".ssh").glob("authorized_keys.bak-preadmin-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == old_pub + "\n"


def test_missing_trailing_newline_gets_normalized_not_corrupted(tmp_path) -> None:
    """I1 reproduction: the pre-existing file's last key has NO trailing
    newline (a real, seen-in-the-wild authorized_keys shape). Before this
    fix, the append landed on the same line and the old key silently
    absorbed the new one into its comment field — ssh-keygen would then
    never see a second valid key. After the fix, a newline is inserted
    before the append, so the new key lands on its own line."""
    home = _fresh_home(tmp_path)
    old_pub = _real_keypair(tmp_path, "old")
    new_pub = _real_keypair(tmp_path, "new")
    ak = home / ".ssh" / "authorized_keys"
    ak.write_bytes(old_pub.encode("utf-8"))  # deliberately NO trailing \n

    proc = _run_body(home, opts='command="x",restrict', pub=new_pub)
    assert proc.returncode == 0, proc.stderr

    content = ak.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert len(lines) == 2, "the new key must land on its OWN line: %r" % content
    assert lines[0] == old_pub, "the old key must not absorb the new one into its comment"
    assert lines[1].endswith(new_pub)


def test_an_unparseable_new_key_trips_the_invariant_and_auto_restores(tmp_path) -> None:
    """I1: on invariant failure the script must RESTORE the live file from
    the verified backup, not just print an instruction and leave it mutated.
    Forcing a garbage PUB value makes ssh-keygen -l count zero new valid
    keys post-append (POST_KEYS - PRE_KEYS != 1) while invariants 1 and 2
    (line count, prior-lines hash) still pass — isolating the valid-key
    invariant and the restore path it triggers."""
    home = _fresh_home(tmp_path)
    old_pub = _real_keypair(tmp_path, "old")
    ak = home / ".ssh" / "authorized_keys"
    original = old_pub + "\n"
    ak.write_text(original, encoding="utf-8", newline="\n")

    proc = _run_body(home, opts='command="x",restrict', pub="not-a-real-public-key")
    assert proc.returncode == 1
    assert "RESTORED" in proc.stderr
    assert "valid-key delta" in proc.stderr
    assert ak.read_text(encoding="utf-8") == original, (
        "the live file must be restored to its pre-append state, not left mutated")


def test_a_backup_that_never_landed_aborts_before_any_append(tmp_path) -> None:
    """I1: if `cp -p` silently fails to produce a backup (e.g. ENOSPC), the
    append must never be attempted at all — trusting a backup that doesn't
    exist is worse than not having tried. Override `cp` with a shell
    function that no-ops, simulating exactly that failure mode."""
    home = _fresh_home(tmp_path)
    old_pub = _real_keypair(tmp_path, "old")
    ak = home / ".ssh" / "authorized_keys"
    original = old_pub + "\n"
    ak.write_text(original, encoding="utf-8", newline="\n")

    proc = _run_body(home, opts='command="x",restrict', pub="ssh-ed25519 AAAAunused test",
                     prelude="cp() { :; }\n")  # cp becomes a no-op
    assert proc.returncode == 1
    assert "backup" in proc.stderr.lower()
    assert "missing or empty" in proc.stderr
    assert ak.read_text(encoding="utf-8") == original, (
        "nothing should have been appended once the backup was known to be missing")
