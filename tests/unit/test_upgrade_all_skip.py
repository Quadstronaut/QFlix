"""tests/unit/test_upgrade_all_skip.py — QFLX-17: the Monday sweep never wakes a
converted or dormant UCC app (spec I-9, section 5.7).

app-upgrade-all.sh discovers apps by walking ~/.apps/<name>/ and probing
`app-<name> --help` (F-7). A converted app keeps both its ~/.apps dir (it is the
native app's data dir, I-8) and its `app-<slug>` wrapper, so discovery alone
would `app-<slug> upgrade` the dormant container. The sweep therefore joins a
skip list GENERATED from the deployed manifest (lib/ucc_skip.py) to
DEFAULT_SKIP, and fails closed when it cannot generate one.

Behavioural tier: stub PATH (fake app-* whose --help lists `upgrade`) + fixture
manifest; POSIX only (runs in CI). The structural tier always runs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.unit.test_ucc_postgres_upgrade import Box, posix_only

REPO = Path(__file__).resolve().parents[2]
UPGRADE_ALL = REPO / "scripts" / "maint" / "app-upgrade-all.sh"
INSTALLER = REPO / "scripts" / "configure" / "240-maintenance-install.sh"
UNPACKERR = REPO / "scripts" / "configure" / "31-unpackerr.sh"


def _code(path: Path) -> str:
    return "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))


@pytest.fixture
def convbox(tmp_path):
    b = Box(tmp_path, apps=("postgres", "sonarr", "radarr", "bazarr", "tautulli"))
    b.write_manifest({
        "postgres": {"class": "ucc", "ucc_slug": "postgres"},
        "sonarr": {"class": "ucc", "ucc_slug": "sonarr"},
        # converted: class flipped, dormant set, ucc_slug kept (spec 5.1)
        "radarr": {"class": "systemd", "unit": "qflix-radarr.service",
                   "ucc_slug": "radarr", "ucc_dormant": True},
        # dormant while still class ucc (mid-swap / pending-swap state)
        "tautulli": {"class": "ucc", "ucc_slug": "tautulli", "ucc_dormant": True},
        "bazarr": {"class": "ucc", "ucc_slug": "bazarr"},
    })
    yield b
    b.close()


def _upgraded(box: Box) -> list[str]:
    # GENERIC_STUB appends its app name to order.log on every non --help call.
    return [l for l in box.read("order.log").splitlines() if l]


# ---- behavioural -----------------------------------------------------------

@posix_only
def test_converted_slug_is_skipped_even_with_dir_and_wrapper(convbox):
    # The acceptance criterion: ~/.apps/radarr AND app-radarr both exist.
    assert (convbox.home / ".apps" / "radarr").is_dir()
    assert (convbox.bin / "app-radarr").exists()
    cp = convbox.sweep()
    assert cp.returncode == 0, cp.stdout + cp.stderr
    # order.log also carries the postgres child's own lines; only the generic
    # app-* stubs matter here.
    upgraded = set(_upgraded(convbox))
    assert {"sonarr", "bazarr"} <= upgraded
    assert not upgraded & {"radarr", "tautulli"}
    assert re.search(r"skip: radarr: converted/dormant", cp.stdout)
    assert re.search(r"skip: tautulli: converted/dormant", cp.stdout)
    res = json.loads((convbox.state / "last-upgrade.json").read_text())
    assert "radarr" not in res["apps"] and "tautulli" not in res["apps"]


@posix_only
def test_dry_run_shows_the_generated_skip_list(convbox):
    cp = convbox.sweep("--dry-run")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "generated_skip=radarr tautulli" in cp.stdout
    assert f"manifest={convbox.manifest}" in cp.stdout
    assert "[DRY] app-radarr" not in cp.stdout
    assert "[DRY] app-sonarr upgrade" in cp.stdout


@posix_only
def test_include_and_only_cannot_unskip_a_generated_slug(convbox):
    cp = convbox.sweep("--include", "radarr", "--only", "radarr")
    assert "radarr" not in _upgraded(convbox)
    assert "skip: radarr: converted/dormant" in cp.stdout


@posix_only
def test_converted_postgres_never_reaches_the_child(convbox):
    convbox.write_manifest({
        "postgres": {"class": "systemd", "unit": "qflix-postgres.service",
                     "ucc_slug": "postgres", "ucc_dormant": True},
        "sonarr": {"class": "ucc", "ucc_slug": "sonarr"},
    })
    cp = convbox.sweep()
    assert convbox.pg_calls() == []
    assert "skip: postgres: converted/dormant" in cp.stdout
    assert "via ucc-postgres-upgrade.sh" not in cp.stdout


@posix_only
@pytest.mark.parametrize("breakage", ["missing", "garbage", "no_generator"])
def test_sweep_fails_closed_when_the_skip_list_cannot_be_generated(convbox, breakage):
    if breakage == "missing":
        convbox.manifest.unlink()
    elif breakage == "garbage":
        convbox.manifest.write_text("apps: [not, a, mapping]\n", encoding="utf-8")
    else:
        (convbox.maint / "lib" / "ucc_skip.py").unlink()
    cp = convbox.sweep()
    assert cp.returncode == 2, cp.stdout + cp.stderr
    assert _upgraded(convbox) == []
    assert convbox.pg_calls() == []
    assert "skip list" in cp.stderr
    assert "warning" in convbox.read("notify.capture")


@posix_only
def test_manifest_env_override_is_honoured(convbox, tmp_path):
    alt = tmp_path / "alt.yaml"
    alt.write_text("apps:\n  sonarr:\n    class: systemd\n    ucc_dormant: true\n",
                   encoding="utf-8")
    cp = convbox.sweep("--dry-run", MANITOBA_MANIFEST=str(alt))
    assert "generated_skip=sonarr" in cp.stdout
    assert "[DRY] app-sonarr" not in cp.stdout


# ---- structural (always runs) ----------------------------------------------

def test_generated_skip_is_checked_before_any_app_probe():
    code = _code(UPGRADE_ALL)
    loop = code[code.index('for name in "${INSTALLED[@]}"; do'):]
    assert loop.index("GEN_SKIP") < loop.index('command -v "$cmd"')
    assert loop.index("GEN_SKIP") < loop.index("has_upgrade_verb")


def test_generated_skip_is_not_subject_to_include():
    code = _code(UPGRADE_ALL)
    include_block = code[code.index("SKIP=()"):code.index("in_list() {")]
    assert "GEN_SKIP" not in include_block


def test_installer_stages_the_generator_and_prints_the_list():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "scripts/maint/lib/ucc_skip.py \\" in text
    assert "ucc_skip.py --list" in _code(INSTALLER)


def test_unpackerr_configure_step_checks_the_manifest_before_waking():
    # QFLX-18 moved the guard into ~/bin/appctl, which reads the deployed
    # manifest and refuses (exit 3) to wake a dormant ucc slug; the dispatch
    # itself is pinned by tests/unit/test_appctl.py.
    code = _code(UNPACKERR)
    assert "~/bin/appctl restart unpackerr" in code
    # no direct (unguarded) app-unpackerr call anywhere
    assert code.count("app-unpackerr") == 0
