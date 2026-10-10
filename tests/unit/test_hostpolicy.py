"""QFLX-16 (F1): fail-closed host.profile + flat hostpolicy modules.

Spec: docs/superpowers/specs/2026-10-09-ucc-divorce-design.md 5.6, I-12, O-5.

What is pinned here:
  * the loader fails CLOSED (missing/empty/unknown profile, detect mismatch);
  * the wall-clock window is policy-backed: Mon 11:00-14:59 UTC in, 15:00 out;
  * an unresolvable profile still answers with the Ultra window (the window is
    a safety brake; deploying code before the secret must change nothing);
  * the generic policy carries no app-* / 172.17.0.1 knowledge;
  * every migrated caller consults the policy and no hard-coded Monday check
    is left behind;
  * ssh.sh _sshm_on_host honours the on-box marker (subprocess).
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "maint" / "lib"
UTC = dt.timezone.utc


def _load(name: str):
    spec = importlib.util.spec_from_file_location("t_" + name, str(LIB / (name + ".py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def hp():
    return _load("hostpolicy")


@pytest.fixture()
def secrets(tmp_path, monkeypatch):
    d = tmp_path / "hp-secrets"
    d.mkdir()
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(d))
    monkeypatch.setenv("MANITOBA_SECRETS", str(d))
    return d


def _which(monkeypatch, ultra: bool):
    real = shutil.which
    monkeypatch.setattr(
        shutil, "which",
        lambda cmd, *a, **k: ("/usr/bin/app-ports" if ultra else None)
        if cmd == "app-ports" else real(cmd, *a, **k))


def _t(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso).replace(tzinfo=UTC)


# --- structure ---------------------------------------------------------------

def test_no_init_py_in_lib_and_modules_are_flat():
    assert not (LIB / "__init__.py").exists(), "lib is a merged namespace package"
    for n in ("hostpolicy", "hostpolicy_ultra", "hostpolicy_generic"):
        assert (LIB / (n + ".py")).is_file()
    assert not (LIB / "hostpolicy").exists(), "no package dir"


def test_import_reads_no_secret(secrets, monkeypatch):
    """Lazy: importing must not touch the secrets dir."""
    calls = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text",
                        lambda self, *a, **k: calls.append(str(self)) or real(self, *a, **k))
    _load("hostpolicy")
    assert not [c for c in calls if "host.profile" in c]


# --- loader fails closed -----------------------------------------------------

def test_missing_profile_fails_closed(hp, secrets, monkeypatch):
    _which(monkeypatch, ultra=True)       # app-ports present must NOT imply ultra
    with pytest.raises(hp.HostProfileError):
        hp.load()


@pytest.mark.parametrize("body", ["", "   \n", "debian", "Ultra2"])
def test_empty_or_unknown_profile_fails_closed(hp, secrets, monkeypatch, body):
    _which(monkeypatch, ultra=True)
    (secrets / "host.profile").write_text(body)
    with pytest.raises(hp.HostProfileError):
        hp.load()


def test_ultra_profile_on_non_ultra_host_is_a_mismatch(hp, secrets, monkeypatch):
    _which(monkeypatch, ultra=False)
    (secrets / "host.profile").write_text("ultra\n")
    with pytest.raises(hp.HostProfileError):
        hp.load()


def test_generic_profile_on_ultra_host_is_a_mismatch(hp, secrets, monkeypatch):
    _which(monkeypatch, ultra=True)
    (secrets / "host.profile").write_text("generic")
    with pytest.raises(hp.HostProfileError):
        hp.load()


def test_matching_profiles_load(hp, secrets, monkeypatch):
    (secrets / "host.profile").write_text("ultra\n")
    _which(monkeypatch, ultra=True)
    assert hp.load().name == "ultra"
    (secrets / "host.profile").write_text("generic\n")
    _which(monkeypatch, ultra=False)
    assert hp.load().name == "generic"


def _cli(args, secrets_dir, extra_env=None):
    env = dict(os.environ)
    env["MANITOBA_SECRETS_DIR"] = str(secrets_dir)
    env["MANITOBA_SECRETS"] = str(secrets_dir)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(LIB / "hostpolicy.py")] + args,
                          env=env, capture_output=True, text=True, timeout=60)


def test_cli_preflight_exits_2_on_missing_profile(secrets):
    r = _cli(["preflight"], secrets)
    assert r.returncode == 2, r.stderr
    assert "host.profile" in r.stderr


def test_cli_preflight_exits_2_on_mismatch(secrets, tmp_path):
    # PATH without app-ports -> detect() false -> ultra declared is a mismatch.
    (secrets / "host.profile").write_text("ultra")
    empty = tmp_path / "emptybin"
    empty.mkdir()
    r = _cli(["preflight"], secrets, {"PATH": str(empty)})
    assert r.returncode == 2, r.stderr


# --- the window --------------------------------------------------------------

@pytest.mark.parametrize("iso,expect", [
    ("2026-07-27T10:59:00", False),    # Monday, one minute before
    ("2026-07-27T11:00:00", True),
    ("2026-07-27T12:30:00", True),
    ("2026-07-27T14:59:59", True),
    ("2026-07-27T15:00:00", False),    # watchdog clears it at 15:00
    ("2026-07-28T12:30:00", False),    # Tuesday same hour
    ("2026-07-26T12:30:00", False),    # Sunday same hour
])
def test_ultra_window_boundaries(hp, secrets, monkeypatch, iso, expect):
    (secrets / "host.profile").write_text("ultra")
    _which(monkeypatch, ultra=True)
    assert hp.in_maintenance_window(_t(iso)) is expect
    assert hp.load().may_operate(_t(iso)) is (not expect)


def test_window_with_no_profile_is_the_ultra_window(hp, secrets):
    """Deploy-before-secret must change nothing: restrictive, not permissive."""
    assert hp.in_maintenance_window(_t("2026-07-27T12:00:00")) is True
    assert hp.in_maintenance_window(_t("2026-07-27T15:00:00")) is False


def test_window_with_garbage_profile_is_the_ultra_window(hp, secrets):
    (secrets / "host.profile").write_text("nonsense")
    assert hp.in_maintenance_window(_t("2026-07-27T12:00:00")) is True


def test_naive_and_foreign_zone_datetimes_are_normalised(hp, secrets):
    naive = dt.datetime(2026, 7, 27, 12, 0)
    assert hp.in_maintenance_window(naive) is True
    est = dt.timezone(dt.timedelta(hours=-5))
    # 07:00 EST Monday == 12:00 UTC Monday
    assert hp.in_maintenance_window(dt.datetime(2026, 7, 27, 7, 0, tzinfo=est)) is True


def test_generic_window_defaults_to_none_and_is_configurable(hp, secrets, monkeypatch):
    (secrets / "host.profile").write_text("generic")
    _which(monkeypatch, ultra=False)
    assert hp.in_maintenance_window(_t("2026-07-27T12:00:00")) is False
    (secrets / "host.windows").write_text("# sunday night\n6 2-4\nbogus line\n")
    assert hp.in_maintenance_window(_t("2026-07-26T03:00:00")) is True
    assert hp.in_maintenance_window(_t("2026-07-26T04:00:00")) is False
    assert hp.in_maintenance_window(_t("2026-07-27T12:00:00")) is False


def test_cli_in_window_exit_codes(secrets):
    assert _cli(["in-window", "2026-07-27T12:00:00Z"], secrets).returncode == 0
    assert _cli(["in-window", "2026-07-27T15:00:00Z"], secrets).returncode == 1
    assert _cli(["in-window", "not-a-time"], secrets).returncode == 3


# --- policy content ----------------------------------------------------------

def test_ultra_policy_answers(secrets, monkeypatch):
    _load("hostpolicy")
    ultra = _load("hostpolicy_ultra").UltraPolicy()
    assert ultra.gate_probe() == "plex"
    assert ultra.task_ceiling() == 2000
    assert ultra.docker_gateway() == "172.17.0.1"
    assert ultra.windows() == [(0, 11, 15)]
    assert ultra.proxy_reload()
    assert "app-upgrade-all" in ultra.upgrade_sweeps()


def test_generic_policy_has_no_ultra_knowledge(secrets):
    src = (LIB / "hostpolicy_generic.py").read_text(encoding="utf-8")
    assert "172.17" not in src
    assert "app-" not in src, "no panel-tool knowledge in the generic policy"
    gen = _load("hostpolicy_generic").GenericPolicy()
    assert gen.docker_gateway() is None
    assert gen.gate_probe() is None
    assert gen.proxy_reload() is None
    assert gen.upgrade_sweeps() == ["native"]
    assert gen.windows() == []


def test_generic_port_range_from_config(secrets):
    gen = _load("hostpolicy_generic").GenericPolicy()
    assert list(gen.port_candidates()) == []
    (secrets / "host.port-range").write_text("20000-20003\n")
    assert list(gen.port_candidates()) == [20000, 20001, 20002, 20003]


# --- every caller is migrated -----------------------------------------------

CALLERS_PY = [
    "scripts/maint/qflix-anime-janitor.py",
    "scripts/maint/qflix-torrent-janitor.py",
    "scripts/maint/qflix-remux-regrab.py",
    "scripts/maint/qflix-entitlement.py",
]
CALLERS_SH = [
    "scripts/canaries/prowlarr-app-sync.sh",
    "scripts/canaries/dash-asset-integrity.sh",
    "scripts/canaries/plex-playback.sh",
    "scripts/canaries/library-container-sanity.sh",
]


@pytest.mark.parametrize("rel", CALLERS_PY + CALLERS_SH)
def test_callers_consult_the_policy_and_keep_no_monday_literal(rel):
    text = (REPO / rel).read_text(encoding="utf-8")
    assert "hostpolicy" in text, rel
    code = [l for l in text.splitlines()
            if not l.strip().startswith("#") and "window-ok:" not in l]
    joined = "\n".join(code)
    assert "weekday() == 0" not in joined, rel
    assert "+%u" not in joined, rel
    assert "11 <= now.hour < 15" not in joined, rel


@pytest.mark.parametrize("rel,modname", [
    ("scripts/maint/qflix-anime-janitor.py", "aj"),
    ("scripts/maint/qflix-torrent-janitor.py", "tj"),
    ("scripts/maint/qflix-remux-regrab.py", "rr"),
])
def test_python_callers_window_behaviour_unchanged(rel, modname, secrets, tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path / "nostate"))
    spec = importlib.util.spec_from_file_location("t_" + modname, str(REPO / rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.in_maintenance_window(_t("2026-07-27T11:00:00")) is True
    assert m.in_maintenance_window(_t("2026-07-27T14:59:00")) is True
    assert m.in_maintenance_window(_t("2026-07-27T15:00:00")) is False
    assert m.in_maintenance_window(_t("2026-07-28T12:00:00")) is False


def test_entitlement_window_behaviour_unchanged(secrets, tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path / "nostate"))
    sys.path.insert(0, str(REPO / "scripts" / "maint"))
    spec = importlib.util.spec_from_file_location(
        "t_ent", str(REPO / "scripts" / "maint" / "qflix-entitlement.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["t_ent"] = m           # @dataclass resolves annotations via sys.modules
    spec.loader.exec_module(m)
    assert m.in_maintenance_window(_t("2026-07-27T12:00:00")) is True
    assert m.in_maintenance_window(_t("2026-07-27T15:00:00")) is False


# --- shell canaries: the in_window clock leg goes through the policy CLI -----

@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("rel,force", [
    ("scripts/canaries/plex-playback.sh", "QP_FORCE_WINDOW"),
    ("scripts/canaries/library-container-sanity.sh", "LCS_FORCE_WINDOW"),
])
def test_bash_in_window_uses_policy_cli(rel, force):
    text = (REPO / rel).read_text(encoding="utf-8")
    assert "hostpolicy.py" in text and "in-window" in text


# --- ssh.sh on-box marker ----------------------------------------------------

def _sshm_on_host(tmp_path, marker_body, hostname_stub):
    home = tmp_path / "home"
    (home / ".config" / "qflix").mkdir(parents=True)
    if marker_body is not None:
        (home / ".config" / "qflix" / "host.id").write_text(marker_body)
    binp = tmp_path / "bin"
    binp.mkdir()
    hn = binp / "hostname"
    hn.write_text("#!/bin/sh\necho %s\n" % hostname_stub)
    hn.chmod(0o755)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = str(binp) + os.pathsep + env["PATH"]
    env.pop("QFLIX_HOST_ID_FILE", None)
    r = subprocess.run(
        ["bash", "-c", 'source "%s"; _sshm_on_host && echo YES || echo NO'
         % (REPO / "scripts" / "lib" / "ssh.sh").as_posix()],
        env=env, capture_output=True, text=True, timeout=30)
    return r.stdout.strip().splitlines()[-1]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_ssh_marker_means_on_host_even_with_other_hostname(tmp_path):
    assert _sshm_on_host(tmp_path, "slot-1\n", "some-other-name") == "YES"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_ssh_empty_marker_does_not_count(tmp_path):
    assert _sshm_on_host(tmp_path, "", "workstation") == "NO"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_ssh_no_marker_no_match_is_remote(tmp_path):
    assert _sshm_on_host(tmp_path, None, "workstation") == "NO"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_ssh_hostname_fallback_still_works(tmp_path):
    assert _sshm_on_host(tmp_path, None, "manitoba") == "YES"
