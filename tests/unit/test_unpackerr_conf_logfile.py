"""QFLX-46: unpackerr.conf [[general]] TOML trap -- validator, template, render
gate, and the stale-log-watchdog unpackerr leg.

The leg is exercised for real: its bash block is extracted from the canary and
run against a fake HOME with a stub journalctl, so 'log behind journald' is
state the leg has to observe, not a string we grep for.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VALIDATOR = REPO / "scripts" / "maint" / "lib" / "unpackerr_conf.py"
TMPL = REPO / "scripts" / "data" / "unpackerr.conf.tmpl"
RENDER = REPO / "scripts" / "configure" / "31-unpackerr.sh"
CANARY = REPO / "scripts" / "canaries" / "stale-log-watchdog.sh"
BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")

_spec = importlib.util.spec_from_file_location("unpackerr_conf", VALIDATOR)
uc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uc)

GOOD = 'log_file = "/h/.apps/unpackerr/unpackerr.log"\ninterval = "2m"\n\n[[sonarr]]\nurl = "http://x"\n'


def _fill(text: str) -> str:
    return re.sub(r"\{\{[A-Z0-9_]+\}\}", "v", text)


# --- validator ---------------------------------------------------------------
def test_good_conf_passes():
    assert uc.check(GOOD) == []


@pytest.mark.parametrize("hdr", ["[[general]]", "[general]", "  [[ General ]]  # panel"])
def test_general_header_is_flagged(hdr):
    bad = hdr + '\nlog_file = "/x/unpackerr.log"\n\n[[sonarr]]\nurl = "u"\n'
    assert "general-header" in uc.check(bad)


def test_log_file_after_a_table_is_missing_at_top_level():
    bad = '[[sonarr]]\nurl = "u"\nlog_file = "/x/unpackerr.log"\n'
    assert "log-file-missing" in uc.check(bad)


def test_unresolved_placeholder_and_invalid_toml():
    assert "unresolved-placeholder" in uc.check(GOOD + 'api_key = "{{KEY}}"\n')
    if sys.version_info >= (3, 11):
        assert "toml-invalid" in uc.check(GOOD + "this is not toml\n")


def test_cli_exit_codes(tmp_path):
    good, bad = tmp_path / "g.conf", tmp_path / "b.conf"
    good.write_text(GOOD, newline="\n")
    bad.write_text("[[general]]\n" + GOOD, newline="\n")

    def run(f):
        return subprocess.run([sys.executable, str(VALIDATOR), "check", str(f)],
                              capture_output=True, text=True)

    assert run(good).returncode == 0
    r = run(bad)
    assert r.returncode == 1 and "general-header" in r.stdout


# --- template ----------------------------------------------------------------
def test_template_filled_is_valid_and_logs_to_file():
    t = TMPL.read_text(encoding="utf-8")
    assert uc.check(_fill(t)) == []
    assert re.search(r"^log_file = ", t, re.M)


def test_template_has_usenet_protocols_and_sab_paths_and_no_secrets():
    t = TMPL.read_text(encoding="utf-8")
    assert t.count('protocols = "torrent,usenet"') == 4
    assert t.count("/home/quadstronaut/downloads/sabnzbd/complete/") == 4
    for m in re.finditer(r'^api_key = "(.*)"', t, re.M):
        assert re.fullmatch(r"\{\{[A-Z0-9_]+\}\}", m.group(1))
    assert not re.search(r"[0-9a-f]{32}", t)


def test_template_general_keys_precede_first_table():
    t = TMPL.read_text(encoding="utf-8")
    code = [ln for ln in t.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    first_table = next(i for i, ln in enumerate(code) if ln.startswith("["))
    top = "\n".join(code[:first_table])
    for k in ("log_file", "log_files", "log_file_mb", "interval"):
        assert re.search(rf"^{k} = ", top, re.M)


# --- 31-unpackerr.sh gate --------------------------------------------------------
def _run_render(tmp_path, tmpl_text: str):
    """Run 31's real render + validation block with a swapped template."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for n in ("net.app_host", "sonarr.port", "sonarr.key", "sonarr2.port", "sonarr2.key",
              "radarr.port", "radarr.key", "radarr2.port", "radarr2.key"):
        (secrets / n).write_text("1\n")
    t = tmp_path / "t.tmpl"
    t.write_bytes(tmpl_text.encode())
    src = RENDER.read_text(encoding="utf-8")
    start = src.index("TMPL=")
    end = src.index('log_info "Backing up')
    block = src[start:end].replace('"$HERE/data/unpackerr.conf.tmpl"', f'"{t.as_posix()}"')
    script = (
        "set -euo pipefail\n"
        'die() { echo "$*" >&2; exit 1; }\n'
        f'HERE="{(REPO / "scripts").as_posix()}"\n'
        f'source "{(REPO / "scripts/lib/secrets.sh").as_posix()}"\n'
        + block + 'echo RENDER_OK\ncat "$OUT"\n')
    sh = tmp_path / "r.sh"
    sh.write_bytes(script.encode())
    env = dict(os.environ, SECRETS_DIR=secrets.as_posix())
    return subprocess.run([BASH, sh.as_posix()], env=env, capture_output=True, text=True, timeout=60)


@needs_bash
def test_render_accepts_the_repo_template(tmp_path):
    r = _run_render(tmp_path, TMPL.read_text(encoding="utf-8"))
    assert r.returncode == 0 and "RENDER_OK" in r.stdout, r.stderr
    assert not [ln for ln in r.stdout.splitlines() if "{{" in ln and not ln.startswith("#")]
    assert 'protocols = "torrent,usenet"' in r.stdout


@needs_bash
def test_render_refuses_a_general_header_template(tmp_path):
    bad = "[[general]]\n" + TMPL.read_text(encoding="utf-8")
    r = _run_render(tmp_path, bad)
    assert r.returncode == 1 and "RENDER_OK" not in r.stdout
    assert "general-header" in r.stderr


def test_validation_precedes_the_push():
    src = RENDER.read_text(encoding="utf-8")
    assert src.index("unpackerr_conf.py") < src.index("scpm_to")


# --- canary leg -------------------------------------------------------------------
def _leg_block() -> str:
    t = CANARY.read_text(encoding="utf-8")
    return t.split("# BEGIN unpackerr-leg", 1)[1].split("# END unpackerr-leg", 1)[0]


def _run_leg(tmp_path, *, journal_epoch=None, log_mtime=None, conf=GOOD):
    home = tmp_path / "home"
    d = home / ".apps" / "unpackerr"
    d.mkdir(parents=True)
    (d / "unpackerr.conf").write_text(conf, newline="\n")
    if log_mtime is not None:
        log = d / "unpackerr.log"
        log.write_text("x\n")
        os.utime(log, (log_mtime, log_mtime))
    stub = tmp_path / "bin"
    stub.mkdir()
    jc = stub / "journalctl"
    out = f"{journal_epoch}.123456 manitoba unpackerr[1]: queue" if journal_epoch else ""
    jc.write_bytes(f"#!/usr/bin/env bash\n[ -n '{out}' ] && echo '{out}'\nexit 0\n".encode())
    jc.chmod(0o755)
    script = (f'FAILED=(); PASSED=()\n{_leg_block()}\nunpackerr_leg\n'
              'echo "FAILED=${FAILED[*]:-}"; echo "PASSED=${PASSED[*]:-}"\n')
    sh = tmp_path / "leg.sh"
    sh.write_bytes(script.encode())
    env = dict(os.environ, HOME=home.as_posix(),
               PATH=f"{stub.as_posix()}{os.pathsep}{os.environ['PATH']}")
    r = subprocess.run([BASH, sh.as_posix()], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)


NOW = 1_800_000_000


@needs_bash
def test_leg_reds_when_log_is_over_2h_behind_journal(tmp_path):
    r = _run_leg(tmp_path, journal_epoch=NOW, log_mtime=NOW - 5 * 86400)
    assert "log-behind-journal" in r["FAILED"]


@needs_bash
def test_leg_green_when_log_in_step(tmp_path):
    r = _run_leg(tmp_path, journal_epoch=NOW, log_mtime=NOW - 600)
    assert r["FAILED"] == "" and "log-in-step" in r["PASSED"]


@needs_bash
def test_leg_boundary_exactly_2h_is_green_just_over_is_red(tmp_path):
    assert _run_leg(tmp_path, journal_epoch=NOW, log_mtime=NOW - 7200)["FAILED"] == ""
    (tmp_path / "b").mkdir()
    assert "log-behind" in _run_leg(tmp_path / "b", journal_epoch=NOW, log_mtime=NOW - 7201)["FAILED"]


@needs_bash
def test_leg_no_journal_is_pass_not_red(tmp_path):
    r = _run_leg(tmp_path, journal_epoch=None, log_mtime=NOW - 99999)
    assert r["FAILED"] == "" and "no-journal" in r["PASSED"]


@needs_bash
def test_leg_missing_log_with_live_journal_reds(tmp_path):
    r = _run_leg(tmp_path, journal_epoch=NOW, log_mtime=None)
    assert "log-missing-unpackerr" in r["FAILED"]


@needs_bash
def test_leg_reds_on_general_header_even_when_log_fresh(tmp_path):
    r = _run_leg(tmp_path, journal_epoch=NOW, log_mtime=NOW,
                 conf="# Auto-generated by app-unpackerr\n[[general]]\n" + GOOD)
    assert "conf-general-header" in r["FAILED"]


def test_canary_calls_the_leg_before_the_verdict():
    t = CANARY.read_text(encoding="utf-8")
    assert t.index("\nunpackerr_leg\n") < t.index("if [ ${#FAILED[@]} -gt 0 ]")
    assert "'" not in _leg_block()          # lives inside sshm '...'


def test_check_flags_empty_rendered_secrets():
    import sys
    sys.path.insert(0, str(TMPL.parents[2] / "scripts" / "maint" / "lib"))
    import unpackerr_conf
    bad = "log_file = '/x.log'\n[[sonarr]]\nurl = 'http://:/'\napi_key = ''\n"
    bad = bad.replace("'http://:/'", '"http://:/"').replace("api_key = ''", 'api_key = ""')
    assert "empty-secret" in unpackerr_conf.check(bad)
