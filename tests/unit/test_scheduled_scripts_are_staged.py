"""Every script the box RUNS on a schedule must be staged by an installer.

WHY THIS FILE EXISTS
--------------------
Three times now a script has been running on the box from a timer or from cron
with no installer shipping it:

  2026-07-30  qflix-anime-janitor.py staged but never copied out; qflix-reaper.py
              (which DELETES media), audio-disposition-janitor.py and
              functional-audit.py not staged at all.
  2026-08-23  unknown-codec-stream-janitor.py -- a nightly janitor that REWRITES
              media files -- never staged, so the box ran whatever copy landed
              in ~/scripts by hand.
  2026-08-24  arr-housekeeping.py (hourly cron, the autonomous stuck-download
              repair) and flaresolverr-unsuppress-watch.sh.

Each time the fix was "add the missing line", and each time the class survived
to bite again, because nothing asserted the invariant.

WHAT THE DEPLOY-DRIFT CANARY DOES *NOT* COVER
`deploy-drift.sh` compares deployed bytes against origin/master, so an unstaged
script reads perfectly green for as long as nobody edits it. The failure only
appears AFTER someone pushes a fix -- at which point the canary reports drift
that no installer can resolve, and the fix silently does not apply. Drift
detection is the wrong half of the problem: this test is the other half.

WHAT COUNTS AS "SCHEDULED"
A systemd `.service` under scripts/maint/systemd/ whose ExecStart names a path
under `%h/scripts/`, or a crontab line in manifest/jobs.yaml naming one. Those
are the scripts that run unattended; a one-shot installer or an ops remediation
script run by hand is deliberately out of scope.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SYSTEMD = REPO / "scripts" / "maint" / "systemd"
JOBS = REPO / "manifest" / "jobs.yaml"

# Scripts a unit invokes that are NOT expected to be staged, each with the
# reason. An exemption is how a real gap hides, so every entry must name one.
EXEMPT: dict[str, str] = {}


def _installer_text() -> str:
    """Everything any installer could copy, concatenated."""
    out = []
    for d in ("scripts/configure", "scripts/install"):
        base = REPO / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file():
                try:
                    out.append(p.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
    return "\n".join(out)


def _scheduled_scripts() -> dict[str, str]:
    """{repo-relative script path: what schedules it}."""
    found: dict[str, str] = {}

    for svc in sorted(SYSTEMD.glob("*.service")):
        text = svc.read_text(encoding="utf-8")
        for m in re.finditer(r"%h/scripts/(\S+)", text):
            rel = "scripts/" + m.group(1)
            found.setdefault(rel, svc.name)

    if JOBS.exists():
        doc = yaml.safe_load(JOBS.read_text(encoding="utf-8")) or {}
        for section in doc.values():
            if not isinstance(section, dict):
                continue
            for name, entry in section.items():
                if not isinstance(entry, dict):
                    continue
                for value in entry.values():
                    if not isinstance(value, str):
                        continue
                    for m in re.finditer(r"scripts/[A-Za-z0-9_./-]+\.(?:py|sh)", value):
                        found.setdefault(m.group(0), "jobs.yaml:%s" % name)
    return found


def test_the_collector_actually_finds_scheduled_scripts():
    """Guard the guard. An extractor that silently returns nothing would make
    the assertion below vacuously green -- the same defect one level up."""
    s = _scheduled_scripts()
    assert len(s) >= 15, "scheduled-script collector found only %d (units moved?)" % len(s)
    # A couple of known members, so a regex that matches the wrong thing fails here.
    assert any(p.endswith("manitoba-maint") or "/maint/" in p for p in s)


@pytest.mark.parametrize("script,scheduler", sorted(_scheduled_scripts().items()))
def test_every_scheduled_script_is_staged_by_an_installer(script, scheduler):
    base = os.path.basename(script)
    if base in EXEMPT:
        pytest.skip("exempt: %s" % EXEMPT[base])
    assert base in _INSTALLERS, (
        "%s is run unattended by %s but no installer under scripts/configure or "
        "scripts/install copies it. It will read green in deploy-drift until "
        "someone pushes a fix to it, and then the fix will silently not apply. "
        "Add it to the staging list AND the copy block in "
        "scripts/configure/240-maintenance-install.sh (or exempt it here with a "
        "reason)." % (script, scheduler))


_INSTALLERS = _installer_text()


def test_scheduled_scripts_exist_in_the_repo():
    """A unit pointing at a script that is not in git is the other direction of
    the same failure: code running with no source of truth."""
    missing = [s for s in _scheduled_scripts()
               if not (REPO / s).exists()
               and os.path.basename(s) not in EXEMPT]
    assert not missing, "scheduled scripts absent from the repo: %s" % sorted(missing)
