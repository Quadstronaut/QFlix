"""tests/unit/test_ucc_skip.py — I-9 "nothing wakes a dormant app" (QFLX-17).

lib/ucc_skip.py derives, from the DEPLOYED manifest, which UCC slugs must never
be touched through `app-<slug>` again: every app whose class is no longer `ucc`
or that carries `ucc_dormant: true`. The Monday sweep (app-upgrade-all.sh), the
UCC gate probe (lib/ucc.py) and the unpackerr configure step all consume it, so
the skip list is generated, never hand-restated.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lib import ucc_skip

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "maint" / "lib" / "ucc_skip.py"

FIXTURE = """\
apps:
  sonarr:
    class: ucc
    ucc_slug: sonarr
  radarr:
    class: systemd
    unit: qflix-radarr.service
    ucc_slug: radarr
    ucc_dormant: true
  plex:
    class: ucc
    ucc_slug: plex
  bazarr2:
    class: systemd
    unit: bazarr2.service
  tautulli:
    class: ucc
    ucc_slug: tautulli
    ucc_dormant: true
  anime:
    class: ucc
    ucc_slug: sonarr2
  kometa:
    class: cron
  odd:
    class: ucc
    ucc_slug: odd
    ucc_dormant: "yes"
"""


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    p = tmp_path / "apps.yaml"
    p.write_text(FIXTURE, encoding="utf-8")
    return p


def test_skip_list_is_every_converted_or_dormant_slug(manifest):
    skip = ucc_skip.skip_slugs(manifest)
    # radarr: converted (class systemd) AND dormant; tautulli: still class ucc
    # but dormant; bazarr2/kometa: never ucc; odd: dormant flag set to a
    # non-boolean -> fail closed (treated as dormant).
    assert skip == ["bazarr2", "kometa", "odd", "radarr", "tautulli"]


def test_active_ucc_slugs_use_the_ucc_slug(manifest):
    active = ucc_skip.active_ucc_slugs(manifest)
    # manifest name "anime" maps to ucc_slug sonarr2: both names are covered.
    assert active == {"sonarr", "plex", "sonarr2", "anime"}


def test_a_slug_claimed_by_a_dormant_and_an_active_entry_is_skipped(tmp_path):
    p = tmp_path / "apps.yaml"
    p.write_text(
        "apps:\n"
        "  radarr:\n    class: ucc\n    ucc_slug: radarr\n"
        "  radarr-native:\n    class: systemd\n    ucc_slug: radarr\n    ucc_dormant: true\n",
        encoding="utf-8",
    )
    assert "radarr" in ucc_skip.skip_slugs(p)
    assert "radarr" not in ucc_skip.active_ucc_slugs(p)


def test_probe_allowed_only_for_active_ucc(manifest):
    assert ucc_skip.probe_allowed("plex", manifest) is True
    assert ucc_skip.probe_allowed("sonarr", manifest) is True
    for slug in ("radarr", "tautulli", "bazarr2", "kometa", "nosuchapp", "", "odd"):
        assert ucc_skip.probe_allowed(slug, manifest) is False, slug


def test_missing_manifest_raises(tmp_path):
    with pytest.raises(ucc_skip.SkipListError):
        ucc_skip.skip_slugs(tmp_path / "absent.yaml")


@pytest.mark.parametrize("body", ["", "apps: [1, 2]\n", "- just\n- a list\n", "apps:\n  x: 3\n"])
def test_malformed_manifest_raises(tmp_path, body):
    p = tmp_path / "apps.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ucc_skip.SkipListError):
        ucc_skip.skip_slugs(p)


def test_unsafe_slug_names_are_a_hard_error(tmp_path):
    # The list is consumed by bash; a name that is not a plain slug must stop
    # the run rather than flow into a shell comparison.
    p = tmp_path / "apps.yaml"
    p.write_text("apps:\n  'a b':\n    class: cron\n", encoding="utf-8")
    with pytest.raises(ucc_skip.SkipListError):
        ucc_skip.skip_slugs(p)


def test_default_manifest_path_is_the_deployed_copy(monkeypatch, tmp_path):
    monkeypatch.delenv("MANITOBA_MANIFEST", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert ucc_skip.default_manifest_path() == tmp_path / ".opt" / "maint" / "apps.yaml"
    monkeypatch.setenv("MANITOBA_MANIFEST", str(tmp_path / "x.yaml"))
    assert ucc_skip.default_manifest_path() == tmp_path / "x.yaml"


# --- CLI (the sweep and 31-unpackerr.sh call it as a plain script) ----------

def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=60)


def test_cli_list_prints_one_slug_per_line(manifest):
    cp = _cli("--manifest", str(manifest), "--list")
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.splitlines() == ["bazarr2", "kometa", "odd", "radarr", "tautulli"]


def test_cli_check_exit_codes(manifest):
    assert _cli("--manifest", str(manifest), "--check", "plex").returncode == 0
    assert _cli("--manifest", str(manifest), "--check", "radarr").returncode == 3
    assert _cli("--manifest", str(manifest), "--check", "nosuchapp").returncode == 3


def test_cli_fails_closed_on_unreadable_manifest(tmp_path):
    absent = str(tmp_path / "absent.yaml")
    cp = _cli("--manifest", absent, "--list")
    assert cp.returncode == 2 and cp.stdout == ""
    assert _cli("--manifest", absent, "--check", "plex").returncode == 2


def test_real_manifest_generates_cleanly():
    # The repo manifest must always generate; today plex is an active ucc slug
    # (never converted on Ultra), which the probe pin relies on.
    real = REPO / "manifest" / "apps.yaml"
    assert ucc_skip.probe_allowed("plex", real) is True
    skip = ucc_skip.skip_slugs(real)
    assert "plex" not in skip
    assert all(s for s in skip)
