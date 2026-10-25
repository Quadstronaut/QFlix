"""QFLX-75: every systemd timer runs on an explicit timezone, never box-local.

The box is Europe/Amsterdam. At the 2026-10-25 DST fall-back systemd ran the
repeated local 02:xx hour only once, so every bare `OnCalendar=*:N/15` canary
skipped an hour and Kuma paged ~40 monitors red with nothing broken.

Pinned here:
  1. Every OnCalendar this repo ships, as a unit file or an installer heredoc,
     ends in `UTC` or an IANA Area/City zone.
  2. scripts/maint/timer-utc-pin.sh (the box-side enforcer for timers the repo
     does not own) pins bare timers via a drop-in, is idempotent, retires its
     drop-in once the base unit carries a zone, and reloads at most once.
  3. The timer-liveness canary heals a bare timer and fails only if the heal
     does not hold.
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
SCRIPTS = REPO_ROOT / "scripts"
PIN = SCRIPTS / "maint" / "timer-utc-pin.sh"
CANARY = SCRIPTS / "canaries" / "timer-liveness.sh"

bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

_TZ = re.compile(r"(^|\s)(UTC|[A-Za-z]+/[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)?)$")


def _repo_oncalendars():
    """(file, spec) for every OnCalendar= line in a tracked-shape source:
    *.timer units anywhere under scripts/, plus heredoc'd units in *.sh/*.py."""
    out = []
    for p in SCRIPTS.rglob("*"):
        if p.suffix not in (".timer", ".sh", ".py") or not p.is_file():
            continue
        if "node_modules" in p.parts:
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("OnCalendar="):
                spec = line.split("=", 1)[1].strip()
                if spec:                                  # empty = list reset
                    out.append((p.relative_to(REPO_ROOT).as_posix(), spec))
    return out


def test_scan_found_the_fleet():
    # Guard the guard: if the walker breaks it must not pass vacuously.
    assert len(_repo_oncalendars()) >= 60


def test_every_repo_oncalendar_has_an_explicit_timezone():
    bare = [f"{f}: {s}" for f, s in _repo_oncalendars() if not _TZ.search(s)]
    assert not bare, (
        "OnCalendar without a timezone runs in the box's local zone and skips or "
        "repeats an hour at every DST change (QFLX-75). Append ' UTC':\n  "
        + "\n  ".join(bare))


# --- timer-utc-pin.sh ---------------------------------------------------------

def _pin_env(tmp_path: Path):
    units = tmp_path / "units"
    units.mkdir()
    stub = tmp_path / "bin"
    stub.mkdir()
    calls = tmp_path / "systemctl.calls"
    (stub / "systemctl").write_text(
        f'#!/usr/bin/env bash\necho "$*" >> "{calls.as_posix()}"\n', newline="\n")
    (stub / "systemctl").chmod(0o755)
    env = {**os.environ, "QFLIX_TIMER_UNIT_DIR": str(units),
           "PATH": str(stub) + os.pathsep + os.environ["PATH"]}
    return units, calls, env


def _pin(env, *args):
    return subprocess.run(["bash", str(PIN), *args], capture_output=True, text=True,
                          timeout=30, env=env)


def _unit(units: Path, name: str, *cal: str):
    body = "[Timer]\n" + "".join(f"OnCalendar={c}\n" for c in cal) + "Persistent=true\n"
    (units / name).write_text(body, newline="\n")


@bash
def test_pin_writes_utc_dropin_only_for_bare_timers(tmp_path):
    units, calls, env = _pin_env(tmp_path)
    _unit(units, "bare.timer", "*:5/15")
    _unit(units, "weekly.timer", "weekly")
    _unit(units, "utc.timer", "*-*-* 04:30:00 UTC")
    _unit(units, "phx.timer", "Mon 08:00:00 America/Phoenix")
    _unit(units, "mixed.timer", "*-*-* 02,08:15:00 UTC", "Sun 04:00")
    r = _pin(env)
    assert r.returncode == 0, r.stderr
    d = lambda n: units / f"{n}.d" / "50-qflix-utc.conf"
    assert "OnCalendar=\nOnCalendar=*:5/15 UTC\n" in d("bare.timer").read_text()
    assert "OnCalendar=weekly UTC" in d("weekly.timer").read_text()
    # A mixed timer must restate EVERY entry, or the reset drops the zoned one.
    mixed = d("mixed.timer").read_text()
    assert "OnCalendar=*-*-* 02,08:15:00 UTC\n" in mixed and "OnCalendar=Sun 04:00 UTC\n" in mixed
    assert not d("utc.timer").exists() and not d("phx.timer").exists()
    assert calls.read_text().strip() == "--user daemon-reload"


@bash
def test_pin_is_idempotent_and_self_retiring(tmp_path):
    units, calls, env = _pin_env(tmp_path)
    _unit(units, "bare.timer", "*:07")
    assert "pinned bare.timer" in _pin(env).stdout
    calls.unlink()
    r = _pin(env)
    assert "changed=0" in r.stdout and not calls.exists()     # no reload when nothing changed
    _unit(units, "bare.timer", "*:07 UTC")                      # base unit fixed at the source
    r = _pin(env, "--no-reload")
    assert "unpinned bare.timer" in r.stdout
    assert not (units / "bare.timer.d").exists() and not calls.exists()


@bash
def test_pin_leaves_foreign_dropins_alone(tmp_path):
    units, _, env = _pin_env(tmp_path)
    _unit(units, "t.timer", "*:07 UTC")
    (units / "t.timer.d").mkdir()
    (units / "t.timer.d" / "override.conf").write_text("[Timer]\nPersistent=false\n")
    (units / "t.timer.d" / "50-qflix-utc.conf").write_text("stale\n")
    _pin(env)
    assert (units / "t.timer.d" / "override.conf").exists()
    assert not (units / "t.timer.d" / "50-qflix-utc.conf").exists()


def test_installer_stages_and_runs_the_pin_before_the_reload():
    text = (SCRIPTS / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")
    assert "scripts/maint/timer-utc-pin.sh \\" in text
    pin = text.index("bash ~/scripts/maint/timer-utc-pin.sh --no-reload")
    assert pin < text.index("systemctl --user daemon-reload", pin)


# --- timer-liveness predicate 5 -------------------------------------------------

def _canary_tree(tmp_path: Path, heal_works: bool) -> tuple[Path, dict]:
    root = tmp_path / "root"
    (root / "scripts" / "canaries").mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "manifest").mkdir()
    shutil.copy(CANARY, root / "scripts" / "canaries" / "timer-liveness.sh")
    (root / "scripts" / "lib" / "ssh.sh").write_text('sshm() { bash -c "$*"; }\n', newline="\n")
    (root / "manifest" / "jobs.yaml").write_text(
        "jobs:\n  ingest:\n    timer: scripts/maint/systemd/ingest.timer\n", newline="\n")
    home = tmp_path / "home"
    (home / "scripts" / "maint").mkdir(parents=True)
    flag = (tmp_path / "pinned").as_posix()
    # Stub pin: marks the box as healed (or not) for the next TimersCalendar read.
    (home / "scripts" / "maint" / "timer-utc-pin.sh").write_text(
        f'{"touch " + flag if heal_works else ":"}\n', newline="\n")
    stub = root / "bin"
    stub.mkdir()
    (stub / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  *TimersCalendar*)\n"
        f'    if [ -f "{flag}" ]; then z=" UTC"; else z=""; fi\n'
        "    printf '%s\\n' 'Id=upgradinatorr.timer' "
        "\"TimersCalendar={ OnCalendar=*-*-* 06:00:00$z ; next_elapse=Sun 2026-10-25 07:00:00 CET }\" '' ;;\n"
        "  *ingest.timer*) printf '%s\\n' 'NextElapseUSecRealtime=Sun 2026-10-25' "
        "'NextElapseUSecMonotonic=0' 'Id=ingest.timer' 'LoadState=loaded' 'ActiveState=active' '' ;;\n"
        "  *ingest.service*) printf '%s\\n' 'Id=ingest.service' 'Result=success' 'ActiveState=inactive' ;;\n"
        "esac\n", newline="\n")
    (stub / "systemctl").chmod(0o755)
    (stub / "python3").write_text(f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n',
                                  newline="\n")
    (stub / "python3").chmod(0o755)
    env = {**os.environ, "HOME": str(home), "QFLIX_TL_RECHECK_S": "0",
           "PATH": str(stub) + os.pathsep + os.environ["PATH"]}
    return root, env


def _canary(root, env):
    return subprocess.run(["bash", str(root / "scripts" / "canaries" / "timer-liveness.sh")],
                          capture_output=True, text=True, timeout=60, env=env)


@bash
def test_canary_heals_a_bare_timer_and_warns(tmp_path):
    r = _canary(*_canary_tree(tmp_path, heal_works=True))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS-WARN" in r.stdout and "utc-pinned:upgradinatorr.timer" in r.stdout


@bash
def test_canary_fails_when_the_heal_does_not_hold(tmp_path):
    r = _canary(*_canary_tree(tmp_path, heal_works=False))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STAGE=timer-local-tz" in r.stderr and "upgradinatorr.timer" in r.stderr
