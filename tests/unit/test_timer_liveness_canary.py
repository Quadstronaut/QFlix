"""QFLX-55: timers that died after the 2026-10-16 reboot, and the canary that missed them.

Two regressions pinned here:

  1. scripts/canaries/timer-liveness.sh must call a timer STUCK when systemd
     reports NextElapseUSecMonotonic=infinity (its spelling of "never"). The real
     script runs end to end in a temp tree; only `sshm` (run locally) and
     `systemctl` (canned `show` output) are stubbed.
  2. No repo timer may use OnBootSec. A --user manager can start after a short
     OnBootSec has passed (boot+2m19s at load ~107), and a timer with only
     OnBootSec + OnUnitActiveSec then never fires. OnStartupSec counts from the
     manager's own start.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "canaries" / "timer-liveness.sh"
SYSTEMD_DIR = REPO_ROOT / "scripts" / "maint" / "systemd"

bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _tree(tmp_path: Path, next_mono: str, next_real: str = "", then_mono: str | None = None) -> Path:
    """then_mono: what the timer reports on the 2nd+ sample (default: unchanged)."""
    then_mono = next_mono if then_mono is None else then_mono
    root = tmp_path / "root"
    (root / "scripts" / "canaries").mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "manifest").mkdir()
    shutil.copy(SCRIPT, root / "scripts" / "canaries" / "timer-liveness.sh")
    (root / "scripts" / "lib" / "ssh.sh").write_text('sshm() { bash -c "$*"; }\n', newline="\n")
    (root / "manifest" / "jobs.yaml").write_text(
        "jobs:\n  ingest:\n    timer: scripts/maint/systemd/ingest.timer\n", newline="\n")
    stub = root / "bin"
    stub.mkdir()
    # Property order mirrors the box (Id mid-block); blank line ends the block.
    count = (root / "samples").as_posix()
    show = (
        f'n=$(cat "{count}" 2>/dev/null || echo 0)\n'
        "case \"$*\" in\n"
        f'  *ingest.timer*) echo $((n+1)) > "{count}"; mono={next_mono!r}; [ "$n" -gt 0 ] && mono={then_mono!r}\n'
        "    printf '%s\\n' "
        f"'NextElapseUSecRealtime={next_real}' \"NextElapseUSecMonotonic=$mono\" "
        "'Id=ingest.timer' 'LoadState=loaded' 'ActiveState=active' '' ;;\n"
        "  *ingest.service*) printf '%s\\n' 'Id=ingest.service' 'Result=success' ;;\n"
        "esac\n"
    )
    (stub / "systemctl").write_text("#!/usr/bin/env bash\n" + show, newline="\n")
    (stub / "systemctl").chmod(0o755)
    # The canary parses jobs.yaml with `python3 -c 'import yaml'`; point it at the
    # interpreter running pytest (CI's system python3 has no PyYAML).
    (stub / "python3").write_text(f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n',
                                  newline="\n")
    (stub / "python3").chmod(0o755)
    return root


def _run(root: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
           "QFLIX_TL_RECHECK_S": "0"}
    return subprocess.run(["bash", str(root / "scripts" / "canaries" / "timer-liveness.sh")],
                          capture_output=True, text=True, timeout=60, env=env)


@bash
def test_monotonic_infinity_is_stuck(tmp_path):
    r = _run(_tree(tmp_path, "infinity"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=timer-stuck" in r.stderr and "ingest.timer" in r.stderr


@bash
def test_monotonic_countdown_is_scheduled(tmp_path):
    r = _run(_tree(tmp_path, "3w 17h 22min 56.114690s"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


@bash
def test_transient_no_next_elapse_is_not_stuck(tmp_path):
    # A timer caught mid-run reports nothing scheduled once, then re-arms.
    root = _tree(tmp_path, "infinity", then_mono="4min 47s")
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / "samples").read_text().strip() == "2"       # it really re-sampled


def test_no_repo_timer_uses_onbootsec():
    offenders = [p.name for p in SYSTEMD_DIR.glob("*.timer")
                 if re.search(r"^OnBootSec=", p.read_text(encoding="utf-8"), re.M)]
    assert not offenders, f"use OnStartupSec in --user timers (QFLX-55): {offenders}"
