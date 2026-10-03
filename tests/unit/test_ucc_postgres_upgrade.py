"""Guards the "postgres never upgraded" root-gap fix (2026-10-02).

WHY THIS FILE EXISTS
--------------------
app-upgrade-all.sh had `postgres` in DEFAULT_SKIP, so UCC postgres never went
through the weekly in-window sweep. It aged until UCC gated `app-postgres
start/restart` behind "older build ... Upgrade & Repair" (exit 2); on
2026-10-01 postgres stopped, could not be restarted, and listmonk crash-looped
for two days.

The fix routes postgres through scripts/maint/ucc-postgres-upgrade.sh, which
must (a) upgrade WITH the existing password (`-p`) or UCC rotates it and
listmonk loses its DB, (b) fail closed AND LOUD when it cannot read that
password, (c) never let the password reach stdout, notify, last-upgrade.json
(feeds the PUBLIC newsletter) or logs, and (d) prove postgres + listmonk are
healthy after every attempt.

TWO TIERS
---------
STRUCTURAL (always runs): skip list, installer staging (tar list AND cp), the
header documentation of the argv residual.

BEHAVIOURAL (POSIX only): runs the real scripts against a fake $HOME with stub
`app-*`, `pgrep`, `curl`, `systemctl` on PATH, a real listening TCP socket for
the postgres port, and a fake lib/notify.py that captures every page.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UPGRADE_ALL = REPO / "scripts" / "maint" / "app-upgrade-all.sh"
PG_MODULE = REPO / "scripts" / "maint" / "ucc-postgres-upgrade.sh"
INSTALLER = REPO / "scripts" / "configure" / "240-maintenance-install.sh"
CHANGELOG = REPO / "CHANGELOG.md"

# Fixture-only values. Neither is a real credential.
FIXTURE_PW = "Fx7pQ2wLm9Zt"
GENERIC_PW = "S3cretGeneric!"

posix_only = pytest.mark.skipif(
    os.name != "posix" or not shutil.which("bash") or not shutil.which("timeout"),
    reason="behavioural tier needs POSIX bash + coreutils timeout (runs in CI)",
)


# ---------------------------------------------------------------------------
# Structural tier
# ---------------------------------------------------------------------------

def test_default_skip_no_longer_contains_postgres():
    text = UPGRADE_ALL.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.startswith("DEFAULT_SKIP=")]
    assert lines == ["DEFAULT_SKIP=(mariadb nginx tailscale openvpn wireguard)"]


def test_module_tracked_and_executable_in_git():
    if not shutil.which("git"):
        pytest.skip("git not on PATH")
    out = subprocess.run(
        ["git", "ls-files", "-s", "scripts/maint/ucc-postgres-upgrade.sh"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout
    if not out.strip():
        pytest.skip("module not yet in the git index (pre-commit run)")
    assert out.split()[0] == "100755", out


def test_installer_stages_and_copies_module():
    text = INSTALLER.read_text(encoding="utf-8")
    assert re.search(r"^\s*scripts/maint/ucc-postgres-upgrade\.sh \\$", text, re.M), \
        "missing from the staging tar list"
    assert 'cp -f "$STG"/scripts/maint/ucc-postgres-upgrade.sh' in text, \
        "staged but never copied out (the 2026-07-30 failure class)"
    assert "chmod +x ~/scripts/maint/ucc-postgres-upgrade.sh" in text


def test_module_header_documents_residual_and_restore():
    head = PG_MODULE.read_text(encoding="utf-8").split("\nset -u", 1)[0]
    assert "argv" in head and "/proc/<pid>/cmdline" in head
    assert "not" in head and "crash-atomic" in head
    assert "RESTORE" in head and "untar" in head
    assert "ucc-postgres-upgrade" in CHANGELOG.read_text(encoding="utf-8")


def test_module_never_passes_no_backup_or_xtrace():
    text = PG_MODULE.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "set -x" not in code
    assert re.search(r"app-postgres upgrade -p \"\$PW\"", code)
    assert not re.search(r"app-postgres upgrade[^\n]*(-n\b|--no-backup)", code)


# ---------------------------------------------------------------------------
# Behavioural tier — fake box
# ---------------------------------------------------------------------------

HELP = 'printf "Usage: x\\nSubcommands:\\n  upgrade   Upgrade the app\\n  start     Start\\n"'

PG_STUB = r"""#!/usr/bin/env bash
if [ "${1:-}" = "--help" ]; then %(help)s; exit 0; fi
printf '%%s\n' "$*" >> "$CALLS/app-postgres.argv"
echo postgres >> "$CALLS/order.log"
t=$(ls "$HOME"/.apps/backup/qflix-postgres-*.tar.gz 2>/dev/null | head -1)
if [ -n "$t" ]; then stat -c %%a "$t" > "$CALLS/tarball-at-call"; fi
case "$(cat "$CALLS/pg_mode")" in
  ok)    echo "upgrading postgres"
         printf '{"data":{"user":"u","password":"%%s","port":42009},"result":true}\n' "$3"
         exit 0 ;;
  older) printf '{"data": {"message": "This app is running an older build. Please run %%s from your UCP."}, "result": false}\n' "'Upgrade & Repair'"
         exit 2 ;;
  fail)  printf '{"data":{"password":"%%s"},"result":false}\n' "$3"
         echo "fatal: could not set password $3"
         exit 1 ;;
esac
""" % {"help": HELP}

GENERIC_STUB = r"""#!/usr/bin/env bash
if [ "${1:-}" = "--help" ]; then %(help)s; exit 0; fi
echo %(name)s >> "$CALLS/order.log"
if [ -f "$CALLS/fail-%(name)s" ]; then
  echo "upgrade failed"
  printf '{"data":{"password":"%(gpw)s"},"result":false}\n'
  exit 1
fi
exit 0
"""

PGREP_STUB = """#!/usr/bin/env bash
echo "pgrep $*" >> "$CALLS/health.log"
[ -f "$CALLS/checkpointer" ]
"""

CURL_STUB = """#!/usr/bin/env bash
echo "curl $*" >> "$CALLS/health.log"
mode=$(cat "$CALLS/lm_mode")
if [ "$mode" = 200 ] || { [ "$mode" = restart_fix ] && [ -f "$CALLS/restarted" ]; }; then
  printf 200
else
  printf 500
fi
"""

SYSTEMCTL_STUB = """#!/usr/bin/env bash
echo "$*" >> "$CALLS/systemctl.log"
touch "$CALLS/restarted"
"""

NOTIFY_PY = """import os
def notify(msg, level="info"):
    with open(os.environ["NOTIFY_CAPTURE"], "a", encoding="utf-8") as f:
        f.write(level + "\\t" + msg.replace("\\n", " ") + "\\n")
"""


def _exe(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8", newline="\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _config(port: int, pw_line: str | None = f'password = "{FIXTURE_PW}"') -> str:
    lines = ['[app]', 'address = "127.0.0.1:9000"', '', '[db]',
             'host = "127.0.0.1"', f'port = {port}', 'user = "u"']
    if pw_line is not None:
        lines.append(pw_line)
    lines += ['database = "listmonk"', '', '[privacy]', 'x = 1']
    return "\n".join(lines) + "\n"


class Box:
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
        self.notify = self.calls / "notify.capture"
        self.backup = self.home / ".apps" / "backup"

        # A real listener for the postgres [db] port.
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]

        pgdir = self.home / ".apps" / "postgres" / "data"
        pgdir.mkdir(parents=True)
        (pgdir / "PG_VERSION").write_text("17\n")
        lmetc = self.home / ".apps" / "listmonk" / "etc"
        lmetc.mkdir(parents=True)
        self.config = lmetc / "config.toml"
        self.config.write_text(_config(self.port), encoding="utf-8")
        (self.secrets / "listmonk.port").write_text("19999\n")

        for app in apps:
            if app != "postgres":
                (self.home / ".apps" / app).mkdir(exist_ok=True)
                _exe(self.bin / f"app-{app}",
                     GENERIC_STUB % {"help": HELP, "name": app, "gpw": GENERIC_PW})
        if "postgres" in apps:
            _exe(self.bin / "app-postgres", PG_STUB)
        _exe(self.bin / "pgrep", PGREP_STUB)
        _exe(self.bin / "curl", CURL_STUB)
        _exe(self.bin / "systemctl", SYSTEMCTL_STUB)
        self.set(pg_mode="ok", lm_mode="200", checkpointer=True)

    def set(self, *, pg_mode=None, lm_mode=None, checkpointer=None):
        if pg_mode is not None:
            (self.calls / "pg_mode").write_text(pg_mode)
        if lm_mode is not None:
            (self.calls / "lm_mode").write_text(lm_mode)
        if checkpointer is True:
            (self.calls / "checkpointer").write_text("")
        elif checkpointer is False:
            (self.calls / "checkpointer").unlink(missing_ok=True)

    def env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MANITOBA_", "PG_", "LISTMONK_"))}
        env.pop("PW", None)
        env.update(
            HOME=str(self.home),
            PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            CALLS=str(self.calls),
            NOTIFY_CAPTURE=str(self.notify),
            MANITOBA_STATE_DIR=str(self.state),
            MANITOBA_SECRETS_DIR=str(self.secrets),
            MANITOBA_MAINT_DIR=str(self.maint),
            MANITOBA_UPGRADE_RESULTS=str(self.state / "last-upgrade.json"),
            PG_UPGRADE_TIMEOUT="20s",
            PG_HEALTH_TIMEOUT_S="2",
            LISTMONK_HEALTH_TIMEOUT_S="2",
            HEALTH_POLL_INTERVAL_S="1",
        )
        env.update(extra)
        return env

    def sweep(self, *args, **extra):
        return subprocess.run(["bash", str(UPGRADE_ALL), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=120)

    def module(self, *args, **extra):
        return subprocess.run(["bash", str(PG_MODULE), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=120)

    def read(self, name: str) -> str:
        p = self.calls / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def pg_calls(self) -> list[str]:
        return [l for l in self.read("app-postgres.argv").splitlines() if l]

    def tarballs(self) -> list[Path]:
        return sorted(self.backup.glob("qflix-postgres-*.tar.gz")) if self.backup.exists() else []

    def close(self):
        self.sock.close()


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    yield b
    b.close()


@pytest.fixture
def multibox(tmp_path):
    b = Box(tmp_path, apps=("postgres", "bazarr", "sonarr", "mariadb", "nginx"))
    yield b
    b.close()


def _result(cp) -> str:
    return cp.stdout.rstrip("\n").splitlines()[-1]


def _assert_no_secret(box: Box, cp, secret: str) -> None:
    assert secret not in cp.stdout
    assert secret not in cp.stderr
    assert secret not in box.read("notify.capture")
    for p in box.state.rglob("*"):
        if p.is_file():
            assert secret not in p.read_text(encoding="utf-8", errors="replace"), p


# ---- AC-3: password parsing ------------------------------------------------

@posix_only
@pytest.mark.parametrize("line", [
    'password = "abcdefgh12"',
    "password = 'abcdefgh12'  # c",
    'password="abcdefgh12"   ',
])
def test_parse_accepts_basic_and_literal(box, line):
    box.config.write_text(_config(box.port, line), encoding="utf-8")
    cp = box.module("--dry-run")
    assert cp.returncode == 0, cp.stdout
    assert _result(cp) == "RESULT=would_upgrade"
    assert "abcdefgh12" not in cp.stdout + cp.stderr
    assert "<redacted>" in cp.stdout


FAIL_CASES = {
    "missing_file":   (None, "no_config"),
    "no_db_table":    ('[app]\naddress = "127.0.0.1:9000"\n', "no_password"),
    "no_key":         ("NOKEY", "no_password"),
    "unquoted":       ("password = abcdefgh12", "unparseable_password"),
    "backslash":      ('password = "abcd\\efgh12"', "unparseable_password"),
    "duplicate":      ('password = "abcdefgh12"\npassword = "abcdefgh34"', "unparseable_password"),
    "multiline":      ('password = """abcdefgh12"""', "unparseable_password"),
    "other_table":    ("OTHER", "unparseable_password"),
    "short":          ('password = "abcdefg"', "short_password"),
}


def _write_fail_case(box: Box, case: str) -> str:
    spec, reason = FAIL_CASES[case]
    if spec is None:
        box.config.unlink()
    elif spec == "NOKEY":
        box.config.write_text(_config(box.port, None), encoding="utf-8")
    elif spec == "OTHER":
        box.config.write_text(
            '[app]\npassword = "abcdefgh12"\n' + _config(box.port, None).replace("[app]\n", "[x]\n"),
            encoding="utf-8")
    elif spec.startswith("[app]"):
        box.config.write_text(spec, encoding="utf-8")
    else:
        box.config.write_text(_config(box.port, spec), encoding="utf-8")
    return reason


@posix_only
@pytest.mark.parametrize("case", sorted(FAIL_CASES))
def test_parse_fails_closed_with_exact_reason(box, case):
    reason = _write_fail_case(box, case)
    cp = box.module("--dry-run")
    assert _result(cp) == f"RESULT=skipped:{reason}"
    assert cp.returncode == 3
    assert "abcdefg" not in cp.stdout + cp.stderr


# ---- AC-4: fail-closed is loud through the sweep ---------------------------

@posix_only
@pytest.mark.parametrize("case", sorted(FAIL_CASES))
def test_fail_closed_skip_is_loud_and_mutates_nothing(box, case):
    reason = _write_fail_case(box, case)
    cp = box.sweep()
    assert box.pg_calls() == []
    assert box.tarballs() == []
    assert f"postgres: skipped: fail-closed {reason}" in cp.stdout
    assert cp.returncode == 1
    levels = [l.split("\t", 1)[0] for l in box.read("notify.capture").splitlines()]
    assert "warning" in levels
    assert '"postgres":"skipped"' in (box.state / "last-upgrade.json").read_text()


# ---- AC-5/6/8/10: the happy path -------------------------------------------

@posix_only
@pytest.mark.parametrize("flags", [(), ("--no-backup",)])
def test_success_argv_backup_and_redaction(box, flags):
    cp = box.sweep(*flags)
    assert cp.returncode == 0, cp.stdout
    assert box.pg_calls() == [f"upgrade -p {FIXTURE_PW}"]   # never -n
    assert box.read("tarball-at-call").strip() == "600"     # backup BEFORE upgrade
    assert len(box.tarballs()) == 1
    assert stat.S_IMODE(box.tarballs()[0].stat().st_mode) == 0o600
    assert "postgres: upgraded" in cp.stdout
    assert box.read("systemctl.log") == ""                  # healthy: no restart
    log = box.state / "postgres-upgrade.log"
    assert log.exists() and stat.S_IMODE(log.stat().st_mode) == 0o600
    assert "<redacted>" in log.read_text()
    assert '"postgres":"upgraded"' in (box.state / "last-upgrade.json").read_text()
    _assert_no_secret(box, cp, FIXTURE_PW)


@posix_only
def test_failed_upgrade_output_is_redacted_everywhere(box):
    box.set(pg_mode="fail")
    cp = box.sweep()
    assert cp.returncode == 1
    assert "postgres: error: error:upgrade_rc1" in cp.stdout
    assert box.read("health.log") != ""
    _assert_no_secret(box, cp, FIXTURE_PW)
    assert "<redacted>" in (box.state / "postgres-upgrade.log").read_text()


# ---- AC-7: retention -------------------------------------------------------

@posix_only
def test_retention_keeps_new_plus_newest_old_and_never_touches_zips(box):
    box.backup.mkdir(parents=True)
    now = time.time()
    olds = []
    for i, age in enumerate((300, 200, 100)):
        p = box.backup / f"qflix-postgres-2026-09-0{i + 1}_00-00-00.tar.gz"
        p.write_bytes(b"x")
        os.utime(p, (now - age, now - age))
        olds.append(p)
    zips = [box.backup / "postgres-2026-09-01_00-00_1.zip",
            box.backup / "listmonk-2026-09-01_00-00_2.zip"]
    for z in zips:
        z.write_bytes(b"z")
        os.utime(z, (now - 1000, now - 1000))
    cp = box.module(PG_BACKUP_KEEP="2")
    assert cp.returncode == 0, cp.stdout
    remaining = box.tarballs()
    assert len(remaining) == 2
    assert olds[2] in remaining              # newest old one survives
    assert all(z.exists() for z in zips)


@posix_only
def test_tar_failure_skips_upgrade_and_leaves_no_partial(box):
    cp = box.module(PG_APP_DIR=str(box.home / ".apps" / "does-not-exist"))
    assert _result(cp) == "RESULT=skipped:backup_failed"
    assert cp.returncode == 3
    assert box.pg_calls() == []
    assert box.tarballs() == []


# ---- AC-10/11: health after every attempt ----------------------------------

@posix_only
def test_listmonk_restarted_once_then_healthy(box):
    box.set(lm_mode="restart_fix")
    cp = box.module()
    assert _result(cp) == "RESULT=upgraded" and cp.returncode == 0
    assert box.read("systemctl.log").splitlines() == ["--user restart listmonk.service"]


@posix_only
def test_listmonk_stays_down_is_error(box):
    box.set(lm_mode="500")
    cp = box.module()
    assert _result(cp) == "RESULT=error:listmonk_unhealthy" and cp.returncode == 1
    assert len(box.read("systemctl.log").splitlines()) == 1
    assert box.read("notify.capture").startswith("error\t")


@posix_only
def test_no_checkpointer_is_postgres_unhealthy_without_listmonk_restart(box):
    box.set(checkpointer=False)
    cp = box.module()
    assert _result(cp) == "RESULT=error:postgres_unhealthy" and cp.returncode == 1
    assert box.read("systemctl.log") == ""
    assert box.read("notify.capture").startswith("error\t")


@posix_only
def test_unreadable_listmonk_port_is_never_green(box):
    (box.secrets / "listmonk.port").unlink()
    cp = box.module()
    assert _result(cp) == "RESULT=error:listmonk_unhealthy" and cp.returncode == 1


@posix_only
def test_older_build_tell_classified_and_health_still_runs(box):
    box.set(pg_mode="older")
    cp = box.module()
    assert _result(cp) == "RESULT=error:upgrade_rc2:older_build"
    assert cp.returncode == 1
    assert "pgrep" in box.read("health.log") and "curl" in box.read("health.log")


# ---- AC-9: generic-path redaction ------------------------------------------

@posix_only
def test_generic_failure_last_line_is_redacted(multibox):
    (multibox.calls / "fail-sonarr").write_text("")
    cp = multibox.sweep()
    assert cp.returncode == 1
    assert "<redacted>" in cp.stdout
    assert "<redacted>" in multibox.read("notify.capture")
    _assert_no_secret(multibox, cp, GENERIC_PW)


# ---- AC-12: dry-run --------------------------------------------------------

@posix_only
def test_dry_run_plans_postgres_without_touching_anything(box):
    cp = box.sweep("--dry-run")
    assert cp.returncode == 0, cp.stdout
    assert "postgres: would_upgrade" in cp.stdout
    assert "<redacted>" in cp.stdout
    assert box.pg_calls() == [] and box.tarballs() == []
    assert box.read("systemctl.log") == "" and box.read("notify.capture") == ""
    _assert_no_secret(box, cp, FIXTURE_PW)


# ---- AC-13/14: ordering and the remaining skip list ------------------------

@posix_only
def test_postgres_first_and_skip_list_honoured(multibox):
    cp = multibox.sweep()
    assert cp.returncode == 0, cp.stdout
    targets = next(l for l in cp.stdout.splitlines() if l.strip().startswith("targets:"))
    assert targets.split()[1] == "postgres"
    assert multibox.read("order.log").splitlines() == ["postgres", "bazarr", "sonarr"]
    assert "skip: mariadb: in skip list" in cp.stdout
    assert "skip: nginx: in skip list" in cp.stdout


@posix_only
def test_missing_module_is_loud(tmp_path, box):
    # Copy the sweep alone (no sibling module) to prove the missing-child path.
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy(UPGRADE_ALL, lone / "app-upgrade-all.sh")
    cp = subprocess.run(["bash", str(lone / "app-upgrade-all.sh")], env=box.env(),
                        capture_output=True, text=True, timeout=60)
    assert "postgres: error: postgres_module_missing" in cp.stdout
    assert cp.returncode == 1
