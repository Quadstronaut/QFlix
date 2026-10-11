"""scripts/maint/app-upgrade-all.sh -- the Monday UCC upgrade sweep.

QFLX-37 retired the postgres child (scripts/maint/ucc-postgres-upgrade.sh,
which drove `app-postgres upgrade -p <pw>` with the listmonk DB password in
argv, F-23): postgres runs native and is NEVER upgraded through app-postgres
again. A bare `app-postgres upgrade` rotates the password and takes listmonk
down, so the sweep REFUSES the slug before any probe, whatever the manifest,
--include or --only say.

This file also carries the fake box the sweep tests share (test_upgrade_all_
skip.py imports Box/posix_only from here) and the generic sweep behaviour that
used to live in the retired module's test file: redaction of failure text,
the skip list, the loud zero-target probe, and env hardening.

Behavioural tier: stub PATH (fake app-* whose --help lists `upgrade`) + fixture
manifest; POSIX only (runs in CI). The structural tier always runs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UPGRADE_ALL = REPO / "scripts" / "maint" / "app-upgrade-all.sh"
UCC_SKIP = REPO / "scripts" / "maint" / "lib" / "ucc_skip.py"
INSTALLER = REPO / "scripts" / "configure" / "240-maintenance-install.sh"
RETIRED = REPO / "scripts" / "maint" / "ucc-postgres-upgrade.sh"

# Fixture-only value. Not a real credential.
GENERIC_PW = "S3cretGeneric!"

posix_only = pytest.mark.skipif(
    os.name != "posix" or not shutil.which("bash") or not shutil.which("timeout"),
    reason="behavioural tier needs POSIX bash + coreutils timeout (runs in CI)",
)

# The REAL UCC app-manager format ("Sub-commands:", hyphenated, since ~2026-08-18).
HELP = 'printf "Usage: x\n\nSub-commands:\n    upgrade            Upgrade the app\n    start              Start\n"'

GENERIC_STUB = r"""#!/usr/bin/env bash
if [ "${1:-}" = "--help" ]; then %(help)s; exit 0; fi
echo %(name)s >> "$CALLS/order.log"
printf '%%s\n' "$*" >> "$CALLS/app-%(name)s.argv"
if [ -f "$CALLS/fail-%(name)s" ]; then
  echo "upgrade failed"
  printf '{"data":{"password":"%(gpw)s"},"result":false}\n'
  exit 1
fi
exit 0
"""

NOTIFY_PY = """import os
def notify(msg, level="info"):
    with open(os.environ["NOTIFY_CAPTURE"], "a", encoding="utf-8") as f:
        f.write(level + "\\t" + msg.replace("\\n", " ") + "\\n")
"""


def _exe(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8", newline="\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Box:
    """A fake $HOME with ~/.apps/<app> dirs and an app-<app> stub per app."""

    def __init__(self, tmp: Path, apps=("postgres",)):
        self.tmp = tmp
        self.home = tmp / "home"
        self.bin = tmp / "bin"
        self.calls = tmp / "calls"
        self.state = tmp / "state"
        self.secrets = tmp / "secrets"
        self.maint = tmp / "maint"
        for d in (self.home / ".apps", self.bin, self.calls, self.state,
                  self.secrets, self.maint / "lib"):
            d.mkdir(parents=True, exist_ok=True)
        (self.maint / "lib" / "notify.py").write_text(NOTIFY_PY, encoding="utf-8")
        shutil.copy(UCC_SKIP, self.maint / "lib" / "ucc_skip.py")
        self.manifest = self.home / ".opt" / "maint" / "apps.yaml"
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.write_manifest({a: {"class": "ucc", "ucc_slug": a} for a in apps})
        self.notify = self.calls / "notify.capture"
        for app in apps:
            (self.home / ".apps" / app).mkdir(exist_ok=True)
            _exe(self.bin / f"app-{app}",
                 GENERIC_STUB % {"help": HELP, "name": app, "gpw": GENERIC_PW})

    def write_manifest(self, apps: dict) -> None:
        lines = ["apps:"]
        for name, fields in apps.items():
            lines.append(f"  {name}:")
            lines += [f"    {k}: {str(v).lower() if isinstance(v, bool) else v}"
                      for k, v in fields.items()]
        self.manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MANITOBA_",))}
        env.update(
            HOME=str(self.home),
            PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            CALLS=str(self.calls),
            NOTIFY_CAPTURE=str(self.notify),
            MANITOBA_STATE_DIR=str(self.state),
            MANITOBA_SECRETS_DIR=str(self.secrets),
            MANITOBA_MAINT_DIR=str(self.maint),
            MANITOBA_UPGRADE_RESULTS=str(self.state / "last-upgrade.json"),
            MANITOBA_PYTHON=sys.executable,
        )
        env.update(extra)
        return env

    def sweep(self, *args, **extra):
        return subprocess.run(["bash", str(UPGRADE_ALL), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=120)

    def read(self, name: str) -> str:
        p = self.calls / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def pg_calls(self) -> list[str]:
        """Every argv app-postgres was called with (--help excluded)."""
        return [l for l in self.read("app-postgres.argv").splitlines() if l]

    def close(self):
        pass


@pytest.fixture
def multibox(tmp_path):
    b = Box(tmp_path, apps=("postgres", "bazarr", "sonarr", "mariadb", "nginx"))
    yield b
    b.close()


def _code(path: Path) -> str:
    return "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))


# ---- structural: the child is gone -----------------------------------------

def test_postgres_child_is_retired():
    assert not RETIRED.exists()
    code = _code(UPGRADE_ALL)
    for gone in ("ucc-postgres-upgrade", "PG_MODULE", "run_postgres_module",
                 "PG_TOKEN_RE", "pg_probe_after_timeout", "PW"):
        assert gone not in code, gone
    assert "NEVER_UCC_UPGRADE=(postgres)" in code


def test_installer_no_longer_ships_the_child():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "scripts/maint/ucc-postgres-upgrade.sh \\" not in text
    assert 'cp -f "$STG"/scripts/maint/ucc-postgres-upgrade.sh' not in text
    # a box that had it deployed loses the stale copy on the next 240 run
    assert "rm -f ~/scripts/maint/ucc-postgres-upgrade.sh" in text


def test_postgres_refusal_precedes_every_probe_and_include():
    code = _code(UPGRADE_ALL)
    loop = code[code.index('for name in "${INSTALLED[@]}"; do'):]
    assert loop.index("NEVER_UCC_UPGRADE") < loop.index('command -v "$cmd"')
    assert loop.index("NEVER_UCC_UPGRADE") < loop.index("has_upgrade_verb")
    include_block = code[code.index("SKIP=()"):code.index("in_list() {")]
    assert "NEVER_UCC_UPGRADE" not in include_block


def test_default_skip_unchanged():
    lines = [l for l in UPGRADE_ALL.read_text(encoding="utf-8").splitlines()
             if l.startswith("DEFAULT_SKIP=")]
    assert lines == ["DEFAULT_SKIP=(mariadb nginx tailscale openvpn wireguard)"]


# ---- behavioural ------------------------------------------------------------

@posix_only
def test_ucc_postgres_is_never_upgraded_even_when_included(multibox):
    # Even a manifest that still says class ucc (pre-flip) and an explicit
    # --include/--only postgres must not reach app-postgres.
    cp = multibox.sweep("--include", "postgres", "--only", "postgres")
    assert multibox.pg_calls() == []
    assert "skip: postgres: never upgraded through app-postgres" in cp.stdout


@posix_only
def test_sweep_upgrades_the_rest_in_order_and_honours_the_skip_list(multibox):
    cp = multibox.sweep()
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert multibox.read("order.log").splitlines() == ["bazarr", "sonarr"]
    assert "skip: mariadb: in skip list" in cp.stdout
    assert "skip: nginx: in skip list" in cp.stdout
    res = json.loads((multibox.state / "last-upgrade.json").read_text())
    assert "postgres" not in res["apps"]
    assert res["apps"] == {"bazarr": "upgraded", "sonarr": "upgraded"}


@posix_only
def test_generic_failure_last_line_is_redacted(multibox):
    (multibox.calls / "fail-sonarr").write_text("")
    cp = multibox.sweep()
    assert cp.returncode == 1
    assert "<redacted>" in cp.stdout
    assert "<redacted>" in multibox.read("notify.capture")
    assert GENERIC_PW not in cp.stdout + cp.stderr + multibox.read("notify.capture")
    for p in multibox.state.rglob("*"):
        if p.is_file():
            assert GENERIC_PW not in p.read_text(encoding="utf-8", errors="replace"), p


@posix_only
def test_dry_run_plans_without_touching_anything(multibox):
    cp = multibox.sweep("--dry-run")
    assert cp.returncode == 0, cp.stdout
    assert "[DRY] app-sonarr upgrade" in cp.stdout
    assert "app-postgres" not in cp.stdout.replace("through app-postgres", "")
    assert multibox.read("order.log") == ""


@posix_only
def test_zero_targets_live_is_loud(multibox):
    for app in ("bazarr", "sonarr"):
        (multibox.bin / f"app-{app}").write_text(
            '#!/usr/bin/env bash\nprintf "Usage: x\n"\n', encoding="utf-8")
    cp = multibox.sweep()
    assert cp.returncode == 1, cp.stdout
    rec = multibox.read("notify.capture")
    assert "warning" in rec and "0 upgradeable" in rec


@posix_only
def test_zero_targets_with_only_filter_stays_quiet(multibox):
    cp = multibox.sweep("--only", "nosuchapp")
    assert cp.returncode == 0, cp.stdout


BAD_VALUES = ['a[$(touch PWNED)]', "99999999999999999999", "1.5m", "-1", "abc", ""]


@posix_only
def test_bad_budget_env_never_aborts_or_executes(tmp_path):
    b = Box(tmp_path, apps=("postgres", "sonarr"))
    for val in BAD_VALUES:
        cp = subprocess.run(["bash", str(UPGRADE_ALL), "--dry-run"],
                            env=b.env(MANITOBA_UPGRADE_BUDGET_S=val),
                            capture_output=True, text=True, timeout=120, cwd=tmp_path)
        assert cp.returncode == 0, (val, cp.stdout, cp.stderr)
        assert "sonarr: would_upgrade" in cp.stdout
        assert "postgres: would_upgrade" not in cp.stdout
        assert '"schema_version":1' in (b.state / "last-upgrade.json").read_text()
    assert not (tmp_path / "PWNED").exists()


def test_summary_notify_is_unguarded():
    sweep = _code(UPGRADE_ALL)
    assert re.search(r"^notify \"\$level\" \"\$\{summary\}\"", sweep, re.M)
