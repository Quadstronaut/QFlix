"""QFLX-48: a 240 deploy must not wedge the user manager with a daemon-reload storm.

Root cause (2026-10-11, proven on the box): every `systemctl --user enable` and
`disable` implies a full user-manager daemon-reload unless given --no-reload.
Step 7 of 240-maintenance-install.sh enabled ~40 units one at a time, so a
deploy ran 39 back-to-back reloads at 4-8s each (02:06:28 -> 02:12:18, 5m50s).
While a reload runs, the manager answers no `systemctl show`/`is-active`, so
the pusher's runtime-parity and health probes blocked 5s apiece, its cycle grew
from ~6s to 106-119s, and 33 Kuma push monitors (90s window) went DOWN with
"No heartbeat in the time window" on every deploy.

These tests REPLAY the Step 7 heredoc under bash with a recording `systemctl`
stub and count the calls that make the real manager reload. The bound is the
fix itself: one reload after the unit files land, one after the enables.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "scripts" / "configure" / "240-maintenance-install.sh"

# Max reloads per deploy. Each costs 4-8s of a manager that answers nothing;
# the pusher's slack inside Kuma's 90s push window is ~25s, so two reloads
# separated by the enable phase stay well inside it. Forty did not.
MAX_RELOADS = 2

SYSTEMCTL_STUB = r'''#!/usr/bin/env bash
# Records every invocation, one line, args space-joined.
printf '%s\n' "$*" >> "$QFLIX_TEST_CALLS"
exit 0
'''
NOOP_STUB = "#!/usr/bin/env bash\nexit 0\n"


def _unitscript() -> str:
    src = INSTALLER.read_text(encoding="utf-8")
    m = re.search(r"sshm 'bash -s' <<'UNITSCRIPT'\n(.*?)\nUNITSCRIPT\n", src, re.S)
    assert m, "Step 7 UNITSCRIPT heredoc not found in 240-maintenance-install.sh"
    return m.group(1)


def _strip_shim(body: str) -> str:
    """The pre-fix heredoc: same calls, no --no-reload shim."""
    out = re.sub(r"\nsystemctl\(\) \{\n.*?\n\}\n", "\n", body, count=1, flags=re.S)
    assert out != body, "could not locate the systemctl() shim to strip"
    return out


def _replay(tmp_path: Path, body: str) -> list[str]:
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "systemctl").write_text(SYSTEMCTL_STUB, newline="\n")
    # cp: unit sources are not on disk here; sleep: the trailing settle.
    (stub / "cp").write_text(NOOP_STUB, newline="\n")
    (stub / "sleep").write_text(NOOP_STUB, newline="\n")
    home = tmp_path / "home"
    home.mkdir()
    # QFLX-75: Step 7 runs the real UTC pin against a bare timer, so the reload
    # bound below also proves the pin adds no reload of its own (--no-reload).
    (home / "scripts" / "maint").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "maint" / "timer-utc-pin.sh",
                home / "scripts" / "maint" / "timer-utc-pin.sh")
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    (home / ".config" / "systemd" / "user" / "bare.timer").write_text(
        "[Timer]\nOnCalendar=*:5/15\n", newline="\n")
    calls = tmp_path / "calls.log"
    calls.write_text("", newline="\n")
    script = tmp_path / "unitscript.sh"
    prelude = (
        f'chmod +x "{stub.as_posix()}"/*\n'
        # `pwd` yields the shell-native path: a raw "C:/..." would split PATH
        # at its drive colon under Git Bash and the stubs would never resolve.
        f'export PATH="$(cd "{stub.as_posix()}" && pwd):$PATH"\n'
        f'export HOME="{home.as_posix()}"\n'
        f'export QFLIX_TEST_CALLS="{calls.as_posix()}"\n'
    )
    script.write_text(prelude + body + "\n", newline="\n")
    r = subprocess.run(["bash", script.as_posix()], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return [ln for ln in calls.read_text().splitlines() if ln.strip()]


def _reloads(calls: list[str]) -> list[str]:
    """Calls that make a real systemd manager run a daemon-reload."""
    out = []
    for c in calls:
        argv = c.split()
        if "daemon-reload" in argv:
            out.append(c)
        elif {"enable", "disable"} & set(argv) and "--no-reload" not in argv:
            out.append(c)
    return out


def test_deploy_runs_at_most_two_daemon_reloads(tmp_path):
    calls = _replay(tmp_path, _unitscript())
    enables = [c for c in calls if c.split()[:2] == ["--user", "enable"]]
    assert len(enables) >= 30, f"replay did not reach the enable phase: {calls[:5]}"
    reloads = _reloads(calls)
    assert len(reloads) <= MAX_RELOADS, (
        f"{len(reloads)} daemon-reloads per deploy (max {MAX_RELOADS}); each wedges "
        f"the user manager 4-8s and starves the pusher -> fleet-wide Kuma DOWN "
        f"(QFLX-48). Offenders: {reloads[:6]}")


def test_every_enable_and_disable_skips_the_implicit_reload(tmp_path):
    calls = _replay(tmp_path, _unitscript())
    bad = [c for c in calls
           if {"enable", "disable"} & set(c.split()) and "--no-reload" not in c.split()]
    assert not bad, f"enable/disable without --no-reload: {bad[:6]}"


def test_reload_lands_after_the_last_enable_and_before_the_restarts(tmp_path):
    """--no-reload defers the .wants/ symlinks; a reload must publish them, and
    it must land before the pusher/webhook restarts so they restart into a
    manager that matches disk."""
    calls = _replay(tmp_path, _unitscript())
    idx_last_enable = max(i for i, c in enumerate(calls) if " enable " in f" {c} ")
    idx_first_restart = min(i for i, c in enumerate(calls) if c.split()[:2] == ["--user", "restart"])
    between = [c for c in calls[idx_last_enable:idx_first_restart] if "daemon-reload" in c.split()]
    assert between, "no daemon-reload between the last enable and the restarts"
    # And one before the first enable/start, so the copied unit files are loaded.
    idx_first_enable = min(i for i, c in enumerate(calls) if " enable " in f" {c} ")
    assert any("daemon-reload" in c.split() for c in calls[:idx_first_enable]), \
        "no daemon-reload before the first enable: copied unit files not loaded"


def test_counter_catches_the_pre_fix_storm(tmp_path):
    """Mutation proof: without the shim the same heredoc is the 40-reload storm
    observed on 2026-10-11, so the bound above is not vacuous."""
    calls = _replay(tmp_path, _strip_shim(_unitscript()))
    assert len(_reloads(calls)) > 30
