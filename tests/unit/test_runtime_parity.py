"""lib/runtime_parity.py - per-minute parity predicates (QFLX-20, spec 5.8).

Fixture /proc trees and canned systemctl/pgrep/ss output; no real processes.
Each failure mode must go red, and the healthy native app must stay green.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lib import runtime_parity as rp
from lib.manifest import App, HealthConfig

UID = 1004
UNIT = "qflix-sonarr.service"
BIN = "/home/u/.apps/sonarr/bin/Sonarr"


def _app(**raw_over):
    raw = {"class": "systemd", "ucc_slug": "sonarr", "unit": UNIT, "ucc_dormant": True}
    raw.update(raw_over)
    return App(name="sonarr", class_="systemd", kuma_monitor="Sonarr",
               health=HealthConfig(kind="http_api", raw={"port_secret": "sonarr.port"}),
               defaults={}, raw=raw)


class FakeHost(rp.Host):
    """A /proc fixture on disk plus canned command output."""

    def __init__(self, root: Path, show: str, pgrep: tuple[int, str] = (1, ""),
                 ss: tuple[int, str] = (0, "")):
        super().__init__(root)
        self.show, self.pgrep_out, self.ss_out = show, pgrep, ss
        self.calls = []

    def uid(self):
        return UID

    def run(self, cmd):
        self.calls.append(cmd)
        if cmd[0] == "systemctl":
            return 0, self.show
        if cmd[0] == "pgrep":
            return self.pgrep_out
        if cmd[0] == "ss":
            return self.ss_out
        raise AssertionError(cmd)


def _proc(root: Path, pid: int, *, uid=UID, cgroup="0::/user.slice/x.service",
          cmdline="", ppid=1):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "status").write_text(f"Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (d / "cgroup").write_text(cgroup + "\n")
    (d / "cmdline").write_text(cmdline.replace(" ", "\0") + "\0")
    (d / "stat").write_text(f"{pid} (my app) S {ppid} 1 1 0 -1 0\n")


def _show(main=500):
    return (f"MainPID={main}\n"
            f"ExecStart={{ path={BIN} ; argv[]={BIN} -nobrowser ; ignore_errors=no }}\n")


PORT = 42050


def _ss(pid, port=PORT):
    return 0, f'LISTEN 0 4096 127.0.0.1:{port} 0.0.0.0:* users:(("Sonarr",pid={pid},fd=9))\n'


@pytest.fixture
def healthy(tmp_path):
    root = tmp_path / "proc"
    _proc(root, 500, cgroup=f"0::/user.slice/{UNIT}", cmdline=f"{BIN} -nobrowser")
    return FakeHost(root, _show(500), pgrep=(0, "500\n"), ss=_ss(500))


def test_healthy_native_app_has_no_violation(healthy):
    assert rp.check(_app(), healthy, port=PORT) == []


def test_nothing_applies_to_unconverted_apps(healthy):
    ucc = App(name="x", class_="ucc", kuma_monitor=None,
              health=HealthConfig(kind="http_api", raw={}), defaults={},
              raw={"class": "ucc", "ucc_slug": "x"})
    assert rp.check(ucc, healthy, port=PORT) == []
    assert healthy.calls == []


def test_pending_swap_is_exempt(healthy):
    assert rp.applies(_app(swap_state="pending-swap")) is False
    assert rp.check(_app(swap_state="pending-swap"), healthy, port=PORT) == []


def test_woken_dormant_container_is_detected(healthy):
    _proc(healthy.proc_root, 777, cgroup="0::/user.slice/docker-abc123.scope",
          cmdline="/app/Sonarr -data=/config")
    v = rp.check(_app(), healthy, port=PORT)
    assert len(v) == 1 and "dormant container woken" in v[0] and "777" in v[0]


def test_container_cgroup_of_another_app_or_uid_is_ignored(healthy):
    _proc(healthy.proc_root, 778, cgroup="0::/system.slice/docker-1.scope",
          cmdline="/app/Radarr")
    _proc(healthy.proc_root, 779, uid=9999, cgroup="0::/system.slice/docker-2.scope",
          cmdline="/app/Sonarr")
    assert rp.check(_app(), healthy, port=PORT) == []


def _mountinfo(root: Path, pid: int, src_root: str):
    (root / str(pid) / "mountinfo").write_text(
        f"1234 1200 0:52 {src_root} /config rw,relatime - fuse.mergerfs x rw\n"
        "1235 1200 0:53 / /proc rw - proc proc rw\n")


def test_sibling_container_with_the_same_cmdline_is_not_our_container(healthy):
    """sonarr2's container runs the IDENTICAL cmdline (and s6 svc-sonarr): its
    /config bind mount names ~/.apps/sonarr2, so it is not sonarr woken."""
    root = healthy.proc_root
    _proc(root, 117021, cgroup="0::/user.slice/docker-s2.scope", cmdline="s6-supervise svc-sonarr")
    _proc(root, 117134, cgroup="0::/user.slice/docker-s2.scope",
          cmdline="/app/sonarr/bin/Sonarr -nobrowser -data=/config")
    for pid in (117021, 117134):
        _mountinfo(root, pid, "/quadstronaut/.apps/sonarr2")
    assert rp.check(_app(), healthy, port=PORT) == []


def test_our_own_container_mount_is_still_detected(healthy):
    root = healthy.proc_root
    _proc(root, 781, cgroup="0::/user.slice/docker-s1.scope",
          cmdline="/app/sonarr/bin/Sonarr -nobrowser -data=/config")
    _mountinfo(root, 781, "/quadstronaut/.apps/sonarr")
    v = rp.check(_app(), healthy, port=PORT)
    assert len(v) == 1 and "781" in v[0]


def test_unreadable_mountinfo_keeps_the_hit(healthy):
    """Fail toward red: no mountinfo = cannot prove it is the sibling."""
    _proc(healthy.proc_root, 782, cgroup="0::/user.slice/docker-x.scope",
          cmdline="/app/sonarr/bin/Sonarr -data=/config")
    v = rp.check(_app(), healthy, port=PORT)
    assert len(v) == 1 and "782" in v[0]


def test_native_unit_cgroup_never_counts_as_container(healthy):
    # a cgroup path that contains BOTH the unit name and a marker word
    _proc(healthy.proc_root, 780, cgroup=f"0::/user.slice/docker.slice/{UNIT}",
          cmdline=f"{BIN}")
    assert rp.check(_app(), healthy, port=PORT) == []


def test_two_process_trees_are_detected(healthy):
    _proc(healthy.proc_root, 501, cmdline=f"{BIN}", ppid=1)       # second root
    healthy.pgrep_out = (0, "500\n501\n")
    v = rp.check(_app(), healthy, port=PORT)
    assert any("2 process trees" in x for x in v)


def test_a_parent_child_pair_is_one_tree(healthy):
    _proc(healthy.proc_root, 501, cmdline=f"{BIN}", ppid=500)     # worker of 500
    healthy.pgrep_out = (0, "500\n501\n")
    assert rp.check(_app(), healthy, port=PORT) == []


def test_pgrep_is_uid_scoped_and_matches_execstart_not_a_bare_pattern(healthy):
    rp.check(_app(), healthy, port=PORT)
    cmd = next(c for c in healthy.calls if c[0] == "pgrep")
    assert cmd[:4] == ["pgrep", "-u", str(UID), "-f"]
    assert BIN.replace(".", r"\.") in cmd[-1] or BIN in cmd[-1]


def test_port_owned_by_a_stranger_is_detected(healthy):
    _proc(healthy.proc_root, 900, cgroup="0::/user.slice/docker-1.scope",
          cmdline="/app/other", ppid=1)
    healthy.ss_out = _ss(900)
    v = rp.check(_app(), healthy, port=PORT)
    assert any("not unit MainPID 500" in x for x in v)


def test_port_owned_by_a_child_of_mainpid_is_fine(healthy):
    _proc(healthy.proc_root, 501, cmdline="x", ppid=500)
    healthy.ss_out = _ss(501)
    assert rp.check(_app(), healthy, port=PORT) == []


FWD = "qflix-sonarr-fwd.service"


def _ss_two(app_pid, fwd_pid, port=PORT):
    return 0, (f'LISTEN 0 512 172.17.0.1:{port} 0.0.0.0:* users:(("Sonarr",pid={app_pid},fd=9))\n'
               f'LISTEN 0 4096 127.0.0.1:{port} 0.0.0.0:* '
               f'users:(("systemd-socket-",pid={fwd_pid},fd=3))\n')


def test_loopback_owned_by_the_apps_own_forwarder_is_fine(healthy, tmp_path):
    # QFLX-28 box 2026-10-10: the app binds 172.17.0.1, qflix-x-fwd.socket owns
    # 127.0.0.1; the proxyd in the -fwd.service cgroup is not a stranger.
    _proc(tmp_path / "proc", 600, ppid=4942,
          cgroup=f"0::/user.slice/user-{UID}.slice/user@{UID}.service/app.slice/{FWD}")
    healthy.ss_out = _ss_two(500, 600)
    assert rp.check(_app(), healthy, port=PORT) == []


def test_loopback_held_by_the_user_manager_before_activation_is_fine(healthy, tmp_path):
    _proc(tmp_path / "proc", 4942,
          cgroup=f"0::/user.slice/user-{UID}.slice/user@{UID}.service/init.scope")
    healthy.ss_out = _ss_two(500, 4942)
    assert rp.check(_app(), healthy, port=PORT) == []


def test_another_apps_forwarder_is_still_a_stranger(healthy, tmp_path):
    _proc(tmp_path / "proc", 601, ppid=4942,
          cgroup=f"0::/user.slice/user-{UID}.slice/user@{UID}.service/app.slice/qflix-radarr-fwd.service")
    healthy.ss_out = _ss_two(500, 601)
    v = rp.check(_app(), healthy, port=PORT)
    assert any("owned by pid 601" in x for x in v)


def test_fwd_unit_name():
    assert rp.fwd_unit("qflix-prowlarr.service") == "qflix-prowlarr-fwd.service"
    assert rp.fwd_unit("qflix-prowlarr-fwd.service") is None
    assert rp.fwd_unit("x.socket") is None


def test_listener_with_no_visible_owner_is_a_violation(healthy):
    healthy.ss_out = (0, "LISTEN 0 4096 127.0.0.1:42050 0.0.0.0:*\n")
    v = rp.check(_app(), healthy, port=PORT)
    assert any("not owned by our uid" in x for x in v)


def test_not_listening_is_the_probes_finding_not_parity(healthy):
    healthy.ss_out = (0, "")
    assert rp.check(_app(), healthy, port=PORT) == []


def test_a_broken_detector_fails_open(tmp_path):
    host = FakeHost(tmp_path / "no-proc", "")

    def boom(cmd):
        raise OSError("systemctl gone")
    host.run = boom
    assert rp.check(_app(), host, port=PORT) == []


def test_parse_ppid_survives_parens_and_spaces_in_comm():
    assert rp.parse_ppid("12 (a) b) S 77 1 1") == 77
