"""qflix-listen-set.sh, appctl pending-swap, audit-live L-08 (QFLX-20)."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit import test_appctl as ta

REPO = Path(__file__).resolve().parents[2]
LISTEN_SH = REPO / "scripts" / "ops" / "qflix-listen-set.sh"
LIB = REPO / "scripts" / "maint" / "lib"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

SS_ROWS = ("LISTEN 0 4096 127.0.0.1:42050 0.0.0.0:*\n"
           "LISTEN 0 4096 172.17.0.1:42050 0.0.0.0:*\n"
           "LISTEN 0 4096 127.0.0.1:9999 0.0.0.0:*\n")


# --- qflix-listen-set.sh ------------------------------------------------------

def _listen_env(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    ss = stub / "ss"
    ss.write_text(f"#!/bin/sh\ncat <<'EOF'\n{SS_ROWS}EOF\n", newline="\n")
    appctl = stub / "appctl"
    appctl.write_text('#!/bin/sh\necho \'{"data": {"version": "4.0.20"}, "result": true}\'\n', newline="\n")
    for p in (ss, appctl):
        p.chmod(0o755)
    sec = tmp_path / "sec"
    sec.mkdir()
    (sec / "sonarr.port").write_text("42050\n")
    man = tmp_path / "apps.yaml"
    man.write_text("apps:\n  sonarr:\n    class: ucc\n    health: {kind: http_api, port_secret: sonarr.port}\n"
                   "  unpackerr:\n    class: ucc\n    health: {kind: process_pattern}\n"
                   "  done:\n    class: systemd\n    ucc_dormant: true\n")
    env = dict(os.environ)
    env.update({"QFLIX_LIB": LIB.as_posix(), "QFLIX_PYTHON": Path(sys.executable).as_posix(),
                "QFLIX_APPCTL": appctl.as_posix(), "QFLIX_SS": ss.as_posix(),
                "QFLIX_SWAP_DIR": (tmp_path / "swap").as_posix(),
                "MANITOBA_MANIFEST": man.as_posix(), "MANITOBA_SECRETS_DIR": sec.as_posix()})
    return env


@needs_bash
def test_capture_records_listen_set_and_ucc_version_only(tmp_path):
    env = _listen_env(tmp_path)
    r = subprocess.run(["bash", LISTEN_SH.as_posix(), "capture", "sonarr"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    base = tmp_path / "swap" / "sonarr"
    assert (base / "listen-set.before").read_text().split() == [
        "127.0.0.1:42050", "172.17.0.1:42050"]
    st = json.loads((base / "state.json").read_text())
    assert st["ucc_version"] == "4.0.20" and st["rollback_window"] == "open"
    assert st["swap_date"] is None                    # record only


@needs_bash
def test_capture_all_covers_active_ucc_apps_and_skips_portless_and_converted(tmp_path):
    env = _listen_env(tmp_path)
    r = subprocess.run(["bash", LISTEN_SH.as_posix(), "capture-all"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in (tmp_path / "swap").iterdir()) == ["sonarr"]
    assert "SKIP unpackerr" in r.stderr


@needs_bash
def test_a_failing_ss_never_records_an_empty_set(tmp_path):
    env = _listen_env(tmp_path)
    bad = tmp_path / "stub" / "ss"
    bad.write_text("#!/bin/sh\nexit 1\n", newline="\n")
    r = subprocess.run(["bash", LISTEN_SH.as_posix(), "capture", "sonarr"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert not (tmp_path / "swap" / "sonarr" / "listen-set.before").exists()


@needs_bash
def test_usage_error_is_64(tmp_path):
    r = subprocess.run(["bash", LISTEN_SH.as_posix(), "bogus"], env=_listen_env(tmp_path),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 64


def test_listen_set_script_is_valid_bash():
    if shutil.which("bash"):
        assert subprocess.run(["bash", "-n", LISTEN_SH.as_posix()]).returncode == 0


# --- appctl pending-swap --------------------------------------------------------

PENDING = ta.MANIFEST + """\
  pend:
    class: systemd
    ucc_slug: sonarr
    unit: qflix-pend.service
    ucc_dormant: true
    swap_state: pending-swap
    health:
      kind: http_api
      port_secret: sonarr.port
"""


@needs_bash
@pytest.mark.parametrize("verb", ["start", "restart", "version"])
def test_pending_swap_dispatches_to_the_ucc_app_it_still_is(tmp_path, verb):
    r, calls = ta._run(tmp_path, verb, "pend", manifest=PENDING)
    assert r.returncode == 0, r.stderr
    assert calls == ["app-sonarr " + verb]            # not systemctl, not refused


@needs_bash
def test_pending_swap_is_not_native_yet(tmp_path):
    r, _ = ta._run(tmp_path, "is-native", "pend", manifest=PENDING)
    assert (r.returncode, r.stdout.strip()) == (1, "ucc")


@needs_bash
def test_without_pending_swap_the_flipped_app_is_still_native_and_dormant_guarded(tmp_path):
    r, calls = ta._run(tmp_path, "is-native", "flipped")
    assert (r.returncode, r.stdout.strip()) == (0, "native")
    assert calls == []


# --- audit-live L-08 ---------------------------------------------------------------

@pytest.fixture(scope="module")
def live():
    spec = importlib.util.spec_from_file_location(
        "qflix_audit_live_l08", REPO / "scripts" / "maint" / "qflix-audit-live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_listen_set_findings_only_for_changed_slugs(live):
    def differ(slug, _ss):
        return {"a": {"added": [], "removed": []},
                "b": {"added": ["0.0.0.0:1"], "removed": ["127.0.0.1:1"]}}[slug]
    f = live.listen_set_findings("", ["a", "b"], differ)
    assert [(x["class_id"], x["instance_id"]) for x in f] == [("L-08", "b")]
    assert "+0.0.0.0:1 -127.0.0.1:1" in f[0]["detail"]


def test_missing_baseline_is_a_finding_not_a_pass(live):
    def differ(slug, _ss):
        raise RuntimeError("no recorded listen set")
    f = live.listen_set_findings("", ["a"], differ)
    assert f and f[0]["detail"].startswith("listen-set-baseline-missing")


def test_l08_is_checked_and_vacuous_before_any_swap(live, tmp_path, monkeypatch):
    monkeypatch.setenv("QFLIX_SWAP_DIR", str(tmp_path / "none"))
    findings, coverage = [], {}
    live._listen_set_leg(findings, coverage)
    assert coverage["L-08"] == "checked" and findings == []


def test_l08_flags_a_changed_listen_set_end_to_end(live, tmp_path, monkeypatch):
    from lib import swapstate
    monkeypatch.setenv("QFLIX_SWAP_DIR", str(tmp_path / "swap"))
    swapstate.capture("sonarr", "LISTEN 0 1 127.0.0.1:42050 0.0.0.0:*\n", 42050)
    swapstate.update_state("sonarr", swap_date="2026-10-20")

    class P:
        returncode = 0
        stdout = "LISTEN 0 1 0.0.0.0:42050 0.0.0.0:*\n"
    monkeypatch.setattr(live.subprocess, "run", lambda *a, **k: P())
    findings, coverage = [], {}
    live._listen_set_leg(findings, coverage)
    assert coverage["L-08"] == "checked"
    assert findings and findings[0]["instance_id"] == "sonarr"


def test_l08_unavailable_when_ss_fails(live, tmp_path, monkeypatch):
    from lib import swapstate
    monkeypatch.setenv("QFLIX_SWAP_DIR", str(tmp_path / "swap"))
    swapstate.capture("sonarr", "LISTEN 0 1 127.0.0.1:42050 0.0.0.0:*\n", 42050)
    swapstate.update_state("sonarr", swap_date="2026-10-20")

    class P:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(live.subprocess, "run", lambda *a, **k: P())
    findings, coverage = [], {}
    live._listen_set_leg(findings, coverage)
    assert coverage["L-08"] == "unavailable" and findings == []
