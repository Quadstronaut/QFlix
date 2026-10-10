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
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UPGRADE_ALL = REPO / "scripts" / "maint" / "app-upgrade-all.sh"
PG_MODULE = REPO / "scripts" / "maint" / "ucc-postgres-upgrade.sh"
UCC_SKIP = REPO / "scripts" / "maint" / "lib" / "ucc_skip.py"
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

# The REAL UCC app-manager format ("Sub-commands:", hyphenated, since ~2026-08-18).
# The stub used to print "Subcommands:", which is why every test stayed green
# while the live sweep matched nothing for six weeks.
HELP = 'printf "Usage: x\n\nSub-commands:\n    upgrade            Upgrade the app\n    start              Start\n"'

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
  slow)  sleep 3
         echo "upgrading postgres"
         exit 0 ;;
  slowkill) echo $$ > "$CALLS/stub.pid"
         sleep 31.4159
         echo late > "$CALLS/marker"
         exit 0 ;;
  rf)    echo '{"result": false}'
         exit 0 ;;
  leak)  pw="$3"; bs='\'
         e=${pw//"$bs"/"$bs$bs"}; e=${e//\"/"$bs\""}; f=${e//\//"$bs/"}
         printf '{"data":{"password":"%%s","PASSWORD":"%%s"},"result":true}\n' "$e" "$e"
         echo "raw=$pw esc=$e slash=$f"
         echo "dsn postgres://u:$pw@h/db postgres://u:$f@h/db"
         env > "$CALLS/env.dump"
         exit 0 ;;
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
        # QFLX-17: the sweep generates its skip list from the deployed manifest
        # via lib/ucc_skip.py and fails closed without one. The default fixture
        # manifest lists every stubbed app as an active ucc app (empty
        # generated list), so these tests see the pre-QFLX-17 behaviour.
        shutil.copy(UCC_SKIP, self.maint / "lib" / "ucc_skip.py")
        self.manifest = self.home / ".opt" / "maint" / "apps.yaml"
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.write_manifest({a: {"class": "ucc", "ucc_slug": a} for a in apps})
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

    def write_manifest(self, apps: dict) -> None:
        lines = ["apps:"]
        for name, fields in apps.items():
            lines.append(f"  {name}:")
            lines += [f"    {k}: {str(v).lower() if isinstance(v, bool) else v}"
                      for k, v in fields.items()]
        self.manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

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
            MANITOBA_PYTHON=sys.executable,
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
    assert "postgres: error: upgrade_rc1" in cp.stdout
    assert "error: error:" not in cp.stdout
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
    assert box.read("systemctl.log") == "" and box.read("health.log") == ""
    # master behaviour: exactly ONE record, the parent summary (never the child's)
    recs = [l for l in box.read("notify.capture").splitlines() if l]
    assert len(recs) == 1, recs
    assert "ucc-postgres-upgrade" not in recs[0] and FIXTURE_PW not in recs[0]
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
def test_zero_targets_live_is_loud(multibox):
    # Every wrapper losing its upgrade verb at once = a broken probe, not a
    # quiet week. 2026-08-24..10-03 the sweep upgraded nothing and exited 0.
    for app in ("postgres", "bazarr", "sonarr"):
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


# ===========================================================================
# ROUND 2 — lock, budgets, kill semantics, env hardening, RESULT contract
# ===========================================================================

def _code(path: Path) -> str:
    return "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))


# ---- structural (always runs) ----------------------------------------------

def test_lock_taken_before_password_is_read():
    code = _code(PG_MODULE)
    assert code.index("flock -n 9") < code.index("read_listmonk_db_password ||")
    assert code.index("flock -n 9") < code.index("read_listmonk_db_password()")
    assert 'exec 9>>"$LOCK_FILE"' in code


def test_every_checkpointer_pgrep_is_uid_scoped_and_lockfile_never_deleted():
    for path in (PG_MODULE, UPGRADE_ALL):
        code = _code(path)
        for line in code.splitlines():
            if "pgrep" in line and "checkpointer" in line:
                assert 'pgrep -u "$(id -u)"' in line, line
        assert not re.search(r"\brm\b[^\n]*(LOCK_FILE|\.lock)", code)


def test_pw_never_exported_and_unset_first():
    for path in (PG_MODULE, UPGRADE_ALL):
        code = _code(path)
        assert not re.search(r"\b(export|declare\s+-x|typeset\s+-x)\s+PW\b", code)
        assert not re.search(r"\benv\b[^\n]*\bPW=", code)
        assert "set -x" not in code
        lines = [l for l in code.splitlines() if l.strip()]
        assert lines[lines.index("set -u") + 1] == "unset PW"


def test_budget_lines_and_unguarded_summary_notify():
    code = _code(PG_MODULE)
    assert re.search(r"timeout -k 10 \"\$PG_BACKUP_TIMEOUT\" tar ", code)
    assert re.search(r"timeout -k 30 \"\$PG_UPGRADE_TIMEOUT\" app-postgres upgrade -p \"\$PW\"", code)
    sweep = _code(UPGRADE_ALL)
    assert re.search(r"^notify \"\$level\" \"\$\{summary\}\"", sweep, re.M)
    assert "PG_BACKUP_TIMEOUT + PG_UPGRADE_TIMEOUT + PG_HEALTH_TIMEOUT_S + LISTMONK_HEALTH_TIMEOUT_S + 60" in sweep
    assert "timeout -k \"$PG_OUTER_KILL_GRACE_S\"" in sweep


def test_header_documents_round2_contracts():
    text = PG_MODULE.read_text(encoding="utf-8")
    head = text.split("\nset -u", 1)[0]
    for needle in ("flock", "PG_BACKUP_TIMEOUT", "PG_OUTER_TIMEOUT_S", "KILL SEMANTICS",
                   "BOUNDED RESIDUAL", "argv", "crash-atomic", "RESTORE"):
        assert needle in head, needle
    log = CHANGELOG.read_text(encoding="utf-8")
    for needle in ("ucc-postgres-upgrade.lock", "PG_BACKUP_TIMEOUT", "PG_OUTER_TIMEOUT_S",
                   "Bounded residual", "crash-atomic"):
        assert needle in log, needle


# ---- behavioural (POSIX) ---------------------------------------------------

ARCHIVE_RE = re.compile(
    r"^qflix-postgres-[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]+-[A-Za-z0-9]{6}\.tar\.gz$")


def _alive_matching(needle: str) -> list[str]:
    hits = []
    for d in Path("/proc").glob("[0-9]*"):
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if needle in cmd and str(os.getpid()) != d.name:
            hits.append(cmd)
    return hits


@posix_only
def test_lock_held_elsewhere_is_skipped_locked(box):
    import fcntl
    lock = box.state / "ucc-postgres-upgrade.lock"
    fd = os.open(lock, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cp = box.module()
        assert _result(cp) == "RESULT=skipped:locked" and cp.returncode == 3
        assert box.pg_calls() == [] and box.tarballs() == []
        sw = box.sweep()
        assert "postgres: skipped: fail-closed locked" in sw.stdout
        assert sw.returncode == 1 and box.pg_calls() == []
        assert "warning" in [l.split("\t")[0] for l in box.read("notify.capture").splitlines()]
    finally:
        os.close(fd)


@posix_only
def test_two_concurrent_runs_one_upgrade(box):
    box.set(pg_mode="slow")
    ps = [subprocess.Popen(["bash", str(PG_MODULE)], env=box.env(), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True) for _ in range(2)]
    outs = [p.communicate(timeout=60)[0] for p in ps]
    results = sorted(o.rstrip("\n").splitlines()[-1] for o in outs)
    assert results == ["RESULT=skipped:locked", "RESULT=upgraded"], outs
    assert len(box.pg_calls()) == 1
    assert len(box.tarballs()) == 1
    assert sorted(p.returncode for p in ps) == [0, 3]


@posix_only
def test_lockfile_is_0600_after_any_run(box):
    box.module("--dry-run")
    lock = box.state / "ucc-postgres-upgrade.lock"
    assert lock.exists() and stat.S_IMODE(lock.stat().st_mode) == 0o600


@posix_only
def test_same_second_runs_leave_distinct_0600_archives(box):
    _exe(box.bin / "date", '#!/usr/bin/env bash\n'
         'if [ "$*" = "-u +%Y-%m-%d_%H-%M-%S" ]; then echo 2026-01-01_00-00-00; '
         'else exec /bin/date "$@"; fi\n')
    for _ in range(2):
        cp = box.module(PG_BACKUP_KEEP="5")
        assert _result(cp) == "RESULT=upgraded", cp.stdout
    names = [p.name for p in box.tarballs()]
    assert len(names) == 2 and len(set(names)) == 2
    assert all(ARCHIVE_RE.match(n) for n in names), names
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in box.tarballs())


@posix_only
def test_retention_spares_future_mtime_and_zips(box):
    box.backup.mkdir(parents=True)
    now = time.time()
    fut = box.backup / "qflix-postgres-2099-01-01_00-00-00-1-AAAAAA.tar.gz"
    fut.write_bytes(b"x")
    os.utime(fut, (now + 120, now + 120))
    olds = []
    for i, age in enumerate((500, 400, 300)):
        p = box.backup / f"qflix-postgres-2026-09-0{i + 1}_00-00-00-1-BBBBB{i}.tar.gz"
        p.write_bytes(b"x")
        os.utime(p, (now - age, now - age))
        olds.append(p)
    z = box.backup / "postgres-2026-09-01_00-00_1.zip"
    z.write_bytes(b"z")
    os.utime(z, (now - 900, now - 900))
    cp = box.module(PG_BACKUP_KEEP="1")
    assert _result(cp) == "RESULT=upgraded", cp.stdout
    assert fut.exists() and z.exists()
    assert not any(p.exists() for p in olds)


@posix_only
def test_keep_08_parses_as_eight(box):
    box.backup.mkdir(parents=True)
    now = time.time()
    for i in range(10):
        p = box.backup / f"qflix-postgres-2026-08-{i + 10:02d}_00-00-00-1-CCCCC{i}.tar.gz"
        p.write_bytes(b"x")
        os.utime(p, (now - 1000 + i, now - 1000 + i))
    cp = box.module(PG_BACKUP_KEEP="08")
    assert _result(cp) == "RESULT=upgraded", cp.stdout
    assert len(box.tarballs()) == 8


@posix_only
def test_tar_timeout_is_backup_failed_and_cleans_up(box):
    _exe(box.bin / "tar", "#!/usr/bin/env bash\nsleep 60\n")
    t0 = time.time()
    cp = box.module(PG_BACKUP_TIMEOUT="2")
    assert time.time() - t0 < 20
    assert _result(cp) == "RESULT=skipped:backup_failed" and cp.returncode == 3
    assert box.pg_calls() == [] and box.tarballs() == []


@posix_only
def test_zero_duration_falls_back_to_default(box):
    _exe(box.bin / "timeout", '#!/usr/bin/env bash\necho "$*" >> "$CALLS/timeout.argv"\n'
         'exec /usr/bin/timeout "$@"\n')
    cp = box.module(PG_UPGRADE_TIMEOUT="0")
    assert _result(cp) == "RESULT=upgraded", cp.stdout
    assert any(l.startswith("-k 30 480s app-postgres") for l in box.read("timeout.argv").splitlines())


BAD_VALUES = ['a[$(touch PWNED)]', "99999999999999999999", "1.5m", "-1", "abc", ""]
CHILD_VARS = ["PG_UPGRADE_TIMEOUT", "PG_BACKUP_TIMEOUT", "PG_HEALTH_TIMEOUT_S",
              "LISTMONK_HEALTH_TIMEOUT_S", "HEALTH_POLL_INTERVAL_S"]
PARENT_VARS = CHILD_VARS + ["PG_OUTER_TIMEOUT_S", "PG_OUTER_KILL_GRACE_S", "MANITOBA_UPGRADE_BUDGET_S"]


@posix_only
@pytest.mark.parametrize("var", CHILD_VARS + ["PG_BACKUP_KEEP"])
def test_child_bad_env_still_prints_would_upgrade(box, var):
    values = BAD_VALUES + (["0", "abc", "9999999999999"] if var == "PG_BACKUP_KEEP" else [])
    for val in values:
        cp = subprocess.run(["bash", str(PG_MODULE), "--dry-run"], env=box.env(**{var: val}),
                            capture_output=True, text=True, timeout=120, cwd=box.tmp)
        assert _result(cp) == "RESULT=would_upgrade" and cp.returncode == 0, (var, val, cp.stdout)
        assert "FATAL" not in cp.stdout + cp.stderr
    assert not (box.tmp / "PWNED").exists()


@posix_only
@pytest.mark.parametrize("var", PARENT_VARS)
def test_parent_bad_env_never_aborts_or_executes(tmp_path, var):
    b = Box(tmp_path, apps=("postgres", "sonarr"))
    try:
        for val in BAD_VALUES:
            cp = subprocess.run(["bash", str(UPGRADE_ALL), "--dry-run"], env=b.env(**{var: val}),
                                capture_output=True, text=True, timeout=120, cwd=tmp_path)
            assert cp.returncode == 0, (var, val, cp.stdout, cp.stderr)
            assert "postgres: would_upgrade" in cp.stdout and "sonarr: would_upgrade" in cp.stdout
            assert '"schema_version":1' in (b.state / "last-upgrade.json").read_text()
        assert not (tmp_path / "PWNED").exists()
    finally:
        b.close()


@posix_only
def test_bad_usage_and_internal_error_always_print_result(box):
    cp = box.module("--bogus")
    assert _result(cp) == "RESULT=error:bad_usage" and cp.returncode == 1
    # A `set -u` violation: BASH_ENV unsets HOME before the script runs (bash would
    # otherwise re-derive HOME from passwd). The EXIT trap must still emit RESULT.
    pre = box.tmp / "unset_home.sh"
    pre.write_text("unset HOME LISTMONK_CONFIG PG_APP_DIR PG_BACKUP_DIR\n", encoding="utf-8")
    env = box.env(BASH_ENV=str(pre))
    cp = subprocess.run(["bash", str(PG_MODULE)], env=env, capture_output=True, text=True, timeout=60)
    assert _result(cp) == "RESULT=error:internal" and cp.returncode == 1


def _sweep_with_fake_child(box: Box, tmp_path: Path, child_body: str, **extra):
    copy = tmp_path / "copy"
    copy.mkdir(exist_ok=True)
    shutil.copy(UPGRADE_ALL, copy / "app-upgrade-all.sh")
    _exe(copy / "ucc-postgres-upgrade.sh", "#!/usr/bin/env bash\n" + child_body)
    return subprocess.run(["bash", str(copy / "app-upgrade-all.sh")], env=box.env(**extra),
                          capture_output=True, text=True, timeout=120)


@posix_only
@pytest.mark.parametrize("body", [
    "echo 'RESULT=upgraded; x'\n",
    "echo 'RESULT=error:$(id)'\n",
    "echo hello\n",
])
def test_off_whitelist_or_missing_result_is_no_result(tmp_path, body):
    b = Box(tmp_path / "b", apps=("postgres", "sonarr"))
    try:
        cp = _sweep_with_fake_child(b, tmp_path, body)
        assert "postgres: error: postgres_no_result" in cp.stdout
        assert cp.returncode == 1
    finally:
        b.close()


@posix_only
def test_rc0_result_false_and_older_build_single_prefix(box):
    box.set(pg_mode="rf")
    cp = box.module()
    assert _result(cp) == "RESULT=error:upgrade_rc0"
    assert "postgres: error: upgrade_rc0" in box.sweep().stdout
    box.set(pg_mode="older")
    sw = box.sweep()
    assert "postgres: error: upgrade_rc2:older_build" in sw.stdout
    assert "error: error:" not in sw.stdout


@posix_only
def test_child_interrupt_mid_upgrade(box):
    box.set(pg_mode="slowkill")
    p = subprocess.Popen(["bash", str(PG_MODULE)], env=box.env(PG_UPGRADE_TIMEOUT="120"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 20
    while not (box.calls / "stub.pid").exists() and time.time() < deadline:
        time.sleep(0.1)
    assert (box.calls / "stub.pid").exists()
    health_before = box.read("health.log")
    p.send_signal(15)
    out = p.communicate(timeout=45)[0]
    assert out.rstrip("\n").splitlines()[-1] == "RESULT=error:interrupted" and p.returncode == 1
    assert len(box.read("health.log")) > len(health_before)        # probed after the signal
    assert any(l.startswith("error\t") and "postgres" in l
               for l in box.read("notify.capture").splitlines())
    assert _alive_matching("sleep 31.4159") == []
    assert not (box.calls / "marker").exists()


@posix_only
def test_parent_outer_timeout_cooperative_child(tmp_path):
    b = Box(tmp_path, apps=("postgres", "sonarr"))
    try:
        b.set(pg_mode="slowkill")
        cp = b.sweep(PG_OUTER_TIMEOUT_S="3", PG_OUTER_KILL_GRACE_S="60", PG_UPGRADE_TIMEOUT="120")
        assert "postgres probe after outer timeout: ok" in cp.stdout
        assert "postgres: error: interrupted" in cp.stdout
        assert '"postgres":"error"' in (b.state / "last-upgrade.json").read_text()
        assert _alive_matching("sleep 31.4159") == []
        assert "sonarr: upgraded" in cp.stdout          # the sweep carried on
        assert cp.returncode == 1
    finally:
        b.close()


@posix_only
@pytest.mark.parametrize("checkpointer,probe,result,level", [
    (False, "down", "error: postgres_unhealthy_after_timeout", "error"),
    (True, "ok", "timeout", "warning"),
])
def test_parent_outer_timeout_uncooperative_child(tmp_path, checkpointer, probe, result, level):
    b = Box(tmp_path / "b", apps=("postgres", "sonarr"))
    try:
        b.set(checkpointer=checkpointer)
        cp = _sweep_with_fake_child(b, tmp_path, "trap '' TERM\nsleep 60\n",
                                    PG_OUTER_TIMEOUT_S="2", PG_OUTER_KILL_GRACE_S="2")
        assert f"postgres probe after outer timeout: {probe}" in cp.stdout
        assert f"postgres: {result}" in cp.stdout
        assert "error: error:" not in cp.stdout
        assert "sonarr: upgraded" in cp.stdout and cp.returncode == 1
        levels = [l.split("\t", 1)[0] for l in b.read("notify.capture").splitlines()]
        assert levels[-1] == level
    finally:
        b.close()


LEAK_PWS = ["Zx9kQ2mLpw", 'ab\\\\cd"ef/12', "a.b*c[d]e&f$g/h|i"]


@posix_only
@pytest.mark.parametrize("pw", LEAK_PWS)
@pytest.mark.parametrize("exported", [None, "same", "decoy"])
def test_password_never_in_env_or_outputs(box, pw, exported):
    box.config.write_text(_config(box.port, f"password = '{pw}'"), encoding="utf-8")
    box.set(pg_mode="leak")
    extra = {}
    if exported:
        extra["PW"] = pw if exported == "same" else "DECOYdecoy99"
    cp = subprocess.run(["bash", str(UPGRADE_ALL)], env=box.env(**extra),
                        capture_output=True, text=True, timeout=120)
    esc = pw.replace("\\", "\\\\").replace('"', '\\"')
    forms = {pw, esc, esc.replace("/", "\\/")}
    blobs = [cp.stdout, cp.stderr, box.read("notify.capture")]
    blobs += [p.read_text(errors="replace") for p in box.state.rglob("*") if p.is_file()]
    for form in forms:
        for blob in blobs:
            assert form not in blob, form
    assert not any(l.startswith("PW=") for l in box.read("env.dump").splitlines())
    assert box.pg_calls() == [f"upgrade -p {pw}"]


@posix_only
def test_every_recorded_pgrep_is_uid_scoped(box):
    box.sweep()
    lines = [l for l in box.read("health.log").splitlines() if l.startswith("pgrep")]
    assert lines and all(f"-u {os.getuid()}" in l for l in lines)


@posix_only
def test_child_dry_run_skip_sends_no_notify(box):
    box.config.unlink()
    cp = box.module("--dry-run")
    assert _result(cp) == "RESULT=skipped:no_config" and cp.returncode == 3
    assert box.read("notify.capture") == ""
