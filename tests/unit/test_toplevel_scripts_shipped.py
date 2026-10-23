"""QFLX-72: every top-level scripts/*.sh resident on the box must be SHIPPED.

deploy-drift compares every *.sh under ~/scripts against origin/master. The
five top-level scripts were all hand-copied once and had no installer, so an
edit to any of them (QFLX-71 touched smoke-test.sh) turned deploy-drift red
until someone scp'd it by hand. 240-maintenance-install.sh now tars and copies
them; this pins that a new top-level script cannot land without joining both.
Offline: reads git's index and the installer text only.
"""
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTALLER = (REPO / "scripts" / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")


def _toplevel_scripts():
    out = subprocess.run(["git", "ls-files", "scripts/*.sh"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    return sorted(p.split("/", 1)[1] for p in out.split() if p.count("/") == 1)


def test_there_are_toplevel_scripts():
    assert "smoke-test.sh" in _toplevel_scripts()


def test_every_toplevel_script_is_in_the_tar():
    missing = [f for f in _toplevel_scripts() if f"    scripts/{f} " not in INSTALLER]
    assert missing == []


def test_every_toplevel_script_is_copied_into_place():
    loop = [l for l in INSTALLER.splitlines()
            if l.startswith("for f in ") and "smoke-test.sh" in l]
    assert len(loop) == 1
    listed = loop[0].split(" in ", 1)[1].rstrip("; do").split()
    assert sorted(listed) == _toplevel_scripts()
