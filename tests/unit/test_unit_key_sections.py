"""QFLX-57: every systemd key we emit must sit in the section systemd reads it from.

Root cause: native_render_unit (scripts/lib/native.sh) put StartLimitIntervalSec
and StartLimitBurst under [Service]. They are [Unit] keys (systemd >= 230), so
on every daemon-reload the box logged "Unknown key StartLimitIntervalSec in
section [Service], ignoring" for all 12 native units -- and restart rate
limiting was silently OFF. systemd never fails a unit for a misplaced key; it
warns and drops it, so nothing went red. This test is the detector.

Scope: every unit-shaped file under scripts/ and tests/fixtures/ (.service,
.timer, .path, .socket, .tmpl, drop-in .conf), unit heredocs inside the
installer .sh files, and the live renderer output for every native family.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "scripts" / "lib" / "native.sh"

# Keys that are ONLY valid in one section. Not exhaustive: the keys we use, plus
# the ones whose section people get wrong (StartLimit* moved [Service] -> [Unit]
# in systemd 230; OnFailure/Wants/After read like service behaviour but are
# dependency keys).
UNIT_ONLY = frozenset({"StartLimitIntervalSec", "StartLimitBurst", "StartLimitAction",
                       "OnFailure", "Wants", "After", "Requires"})
SERVICE_ONLY = frozenset({"ExecStart", "Restart", "RestartSec", "Environment",
                          "EnvironmentFile", "WorkingDirectory", "KillMode"})
# Only these headers open a systemd section. Anything else in brackets (an INI
# or TOML block in a heredoc next door) ends the unit context.
SECTIONS = frozenset({"Unit", "Service", "Install", "Timer", "Path", "Socket"})

_HEADER = re.compile(r"\[(\w+)\]")
_KEY = re.compile(r"([A-Za-z]+)=")


def misplaced(text: str) -> list[str]:
    """Return 'line N: Key in [Section]' for each wrong-section key in TEXT."""
    out, sec = [], None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        m = _HEADER.fullmatch(line)
        if m:
            sec = m.group(1) if m.group(1) in SECTIONS else None
            continue
        m = _KEY.match(line)
        if sec is None or not m:
            continue
        key = m.group(1)
        if key in UNIT_ONLY and sec != "Unit":
            out.append(f"line {n}: {key} in [{sec}] (belongs in [Unit])")
        elif key in SERVICE_ONLY and sec != "Service":
            out.append(f"line {n}: {key} in [{sec}] (belongs in [Service])")
    return out


def _unit_files() -> list[Path]:
    roots = [REPO / "scripts", REPO / "tests" / "fixtures"]
    found = []
    for root in roots:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix in {".service", ".timer", ".path", ".socket"} or \
               p.name.endswith((".service.tmpl", ".timer.tmpl")) or \
               (p.suffix == ".conf" and p.parent.name.endswith((".service.d", ".timer.d"))):
                found.append(p)
    return sorted(found)


def _installer_scripts() -> list[Path]:
    # Installers write units through heredocs (e.g. 50-tdarr-install.sh). The
    # parser only tracks a section after a real [Unit]/[Service]/... header, so
    # ordinary shell lines are never read as keys.
    return sorted(p for p in (REPO / "scripts").rglob("*.sh") if "[Service]" in
                  p.read_text(encoding="utf-8", errors="replace"))


# --- the parser itself (a detector that cannot fail is worse than none) --------

def test_parser_flags_startlimit_under_service():
    bad = "[Unit]\nDescription=x\n\n[Service]\nExecStart=/x\nStartLimitBurst=5\n"
    assert misplaced(bad) == ["line 6: StartLimitBurst in [Service] (belongs in [Unit])"]


def test_parser_flags_service_key_under_unit_and_install():
    bad = "[Unit]\nRestart=always\n[Install]\nEnvironment=A=1\n"
    assert len(misplaced(bad)) == 2


def test_parser_accepts_the_fixed_shape():
    good = ("[Unit]\nAfter=network-online.target\nStartLimitIntervalSec=600\n"
            "StartLimitBurst=5\n\n[Service]\nExecStart=/x\nRestart=on-failure\n")
    assert misplaced(good) == []


def test_parser_ignores_non_systemd_brackets():
    # A TOML/INI block in a neighbouring heredoc must not inherit [Service].
    txt = "[Service]\nExecStart=/x\n[[general]]\n[folders]\nAfter=1\n"
    assert misplaced(txt) == []


# --- the repo --------------------------------------------------------------------

def test_discovery_is_not_vacuous():
    files = _unit_files()
    names = {p.name for p in files}
    # The 12 native units the bug hit, plus the golden fixture, must be in scope.
    for app in ("bazarr", "postgres", "prowlarr", "qbittorrent", "radarr", "radarr2",
                "sabnzbd", "seerr", "sonarr", "sonarr2", "tautulli", "unpackerr"):
        assert f"qflix-{app}.service" in names
    assert len(files) > 100
    assert any(p.name == "50-tdarr-install.sh" for p in _installer_scripts())


@pytest.mark.parametrize("path", _unit_files(), ids=lambda p: p.relative_to(REPO).as_posix())
def test_committed_unit_keys_in_right_section(path):
    assert misplaced(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", _installer_scripts(), ids=lambda p: p.relative_to(REPO).as_posix())
def test_installer_heredoc_keys_in_right_section(path):
    assert misplaced(path.read_text(encoding="utf-8")) == []


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("fam,slug", [("dotnet", "sonarr"), ("go", "unpackerr"),
                                      ("node", "seerr"), ("python", "tautulli"),
                                      ("db", "postgres"), ("static", "qbittorrent")])
def test_rendered_unit_keys_in_right_section(tmp_path, fam, slug):
    script = f'set -u\nsource "{NATIVE.as_posix()}"\nnative_render_unit {slug} {fam} exe "--x"\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=dict(os.environ, HOME=str(tmp_path)), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert misplaced(r.stdout) == []
    # Positive check: the rate limit is present AND in [Unit], not just absent
    # from [Service].
    unit_block = r.stdout.split("[Service]", 1)[0]
    assert "StartLimitIntervalSec=600\n" in unit_block
    assert "StartLimitBurst=5\n" in unit_block
