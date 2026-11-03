"""tdarr-loopback-bind canary, executed against fixture state rather than grepped.

Since QFLX-97 the canary asserts the uid-relay model: Tdarr on a 0700 unix
socket, the TCP port held by tdarr-uid-relay and nothing else, no Tdarr-owned
TCP listener, both patch markers present, and a running relay that enforces
our uid (fresh heartbeat from the unit's MainPID, allow_uid == ours, its own
kernel-lookup selftest passing, the code on disk) plus the on-disk code's
--selftest.

The embedded python body is lifted out of the shell wrapper and EXECUTED with
canned `ss` output and fixture files (same technique as the transcode-stall
canary's tests), because grepping it would prove nothing about behaviour. POSIX
only: it reads uids and file modes.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts" / "canaries" / "tdarr-loopback-bind.sh"
PORT = 40999      # fixture port, not the live one
RELAY_PID = 4321
MANAGER_PID = 1777   # the systemd user manager holding tdarr-uid-relay.socket

posix = pytest.mark.skipif(os.name != "posix", reason="uids and file modes")


def _body() -> str:
    src = CANARY.read_text(encoding="utf-8")
    opener = "python3 - <<PYEOF\n"
    start = src.index(opener) + len(opener)
    body = src[start:src.index("\nPYEOF", start)]
    assert "tdarr-port-not-relay" in body and "QFLIX-UNIX-SOCKET" in body, "wrong block"
    return body


def test_body_survives_the_remote_quoting():
    """It runs inside sshm '...' and an UNQUOTED heredoc on the box: an
    apostrophe ends the sshm string, and $ or a backslash gets expanded."""
    body = _body()
    assert "'" not in body
    assert "$" not in body
    assert "\\" not in body


def _ss(addr, pid=RELAY_PID, comm="python3", manager=MANAGER_PID, extra=""):
    users = '("%s",pid=%d,fd=3)' % (comm, pid)
    if manager:
        users += ',("systemd",pid=%d,fd=41)' % manager
    return 'LISTEN 0      128        %s      0.0.0.0:*    users:(%s%s)' % (addr, users, extra)


RELAY_CODE = "# relay code fixture\n"


def _hb(**kw):
    hb = {"pid": RELAY_PID, "ts": int(time.time()), "allow_uid": os.getuid() if os.name == "posix" else 0,
          "refuse_own": False, "selftest": "pass", "listen_held_by": "systemd", "accepted": 3, "refused_foreign": 1,
          "refused_lookup": 0, "code_sha256": hashlib.sha256(RELAY_CODE.encode()).hexdigest()}
    hb.update(kw)
    return hb


def _run(tmp_path, *, ss_lines=None, ss_all=None, servers_js="x/*QFLIX-UNIX-SOCKET*/" * 2,
         mainapi_js="/*QFLIX-WEBUI-SHARED-IO*/", conf=None, hb=None, dir_mode=0o700,
         make_sock=True, relay="skip", relay_pid=RELAY_PID, socket_unit=None):
    conf_p = tmp_path / "conf.json"
    conf_p.write_text(json.dumps(conf if conf is not None else {"serverPort": PORT}),
                      encoding="utf-8")
    ss_lines = [_ss("127.0.0.1:%d" % PORT)] if ss_lines is None else ss_lines
    (tmp_path / "ss.txt").write_text("\n".join(ss_lines) + "\n", encoding="utf-8")
    (tmp_path / "ssall.txt").write_text("\n".join(ss_all if ss_all is not None else ss_lines) + "\n",
                                        encoding="utf-8")
    # Short path: pytest's tmp_path can pass the 108-byte AF_UNIX limit.
    short = Path(tempfile.mkdtemp(prefix="qflb-"))
    sock_dir = short / "qflix-tdarr"
    sock_dir.mkdir()
    if make_sock:
        import socket
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(sock_dir / "tdarr.sock"))
        s.close()
    sock_dir.chmod(dir_mode)
    (tmp_path / "sockunit.txt").write_text(
        socket_unit if socket_unit is not None
        else "ActiveState=active\nListen=127.0.0.1:%d (Stream)\n" % PORT, encoding="utf-8")
    state = tmp_path / "relay.json"
    state.write_text(hb if isinstance(hb, str) else json.dumps(_hb() if hb is None else hb),
                     encoding="utf-8")
    if servers_js is not None:
        (tmp_path / "servers.js").write_text(servers_js, encoding="utf-8")
    (tmp_path / "mainApi.js").write_text(mainapi_js, encoding="utf-8")
    env = {
        "QFLIX_CANARY_TDARR_LB_CONF": str(conf_p),
        "QFLIX_CANARY_TDARR_LB_SS": str(tmp_path / "ss.txt"),
        "QFLIX_CANARY_TDARR_LB_SS_ALL": str(tmp_path / "ssall.txt"),
        "QFLIX_CANARY_TDARR_LB_SERVERS_JS": str(tmp_path / "servers.js"),
        "QFLIX_CANARY_TDARR_LB_MAINAPI_JS": str(tmp_path / "mainApi.js"),
        "QFLIX_CANARY_TDARR_LB_SOCK_DIR": str(sock_dir),
        "QFLIX_CANARY_TDARR_LB_STATE": str(state),
        "QFLIX_CANARY_TDARR_LB_RELAY_PID": str(relay_pid),
        "QFLIX_CANARY_TDARR_LB_MANAGER_PID": str(MANAGER_PID),
        "QFLIX_CANARY_TDARR_LB_SOCKET_UNIT": (tmp_path / "sockunit.txt").read_text(encoding="utf-8"),
        "QFLIX_CANARY_TDARR_LB_RELAY": relay,
        "HOME": str(tmp_path),
        "PATH": "",
    }
    try:
        return subprocess.run([sys.executable, "-c", _body()], env=env,
                              capture_output=True, text=True, timeout=60)
    finally:
        sock_dir.chmod(0o700)
        shutil.rmtree(short, ignore_errors=True)


@posix
def test_relay_model_is_green(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "PASS: tdarr-loopback-bind" in r.stdout and "tdarr-uid-relay pid 4321" in r.stdout


@posix
def test_dead_relay_is_red_not_cannot_assert(tmp_path):
    """The relay is what this canary owns now: nothing on the port is a page."""
    r = _run(tmp_path, ss_lines=[])
    assert r.returncode == 1
    assert "STAGE=tdarr-relay-down" in r.stderr


@posix
@pytest.mark.parametrize("addr", ["0.0.0.0:%d" % PORT, "*:%d" % PORT, "[::]:%d" % PORT,
                                  "203.0.113.7:%d" % PORT])
def test_any_non_loopback_listener_is_red(tmp_path, addr):
    r = _run(tmp_path, ss_lines=[_ss("127.0.0.1:%d" % PORT), _ss(addr)])
    assert r.returncode == 1
    assert "STAGE=tdarr-bind-public" in r.stderr and addr in r.stderr


@posix
def test_tdarr_back_on_tcp_is_red(tmp_path):
    """A reverted servers.js + restart: Tdarr itself on 127.0.0.1:port."""
    line = _ss("127.0.0.1:%d" % PORT, pid=999, comm="Tdarr_Server", manager=0)
    r = _run(tmp_path, ss_lines=[line])
    assert r.returncode == 1
    assert "STAGE=tdarr-port-not-relay" in r.stderr


@posix
def test_squatter_with_invisible_pid_is_red(tmp_path):
    """Another tenant's listener shows no pid to us. The Node would hand it jobs."""
    r = _run(tmp_path, ss_lines=["LISTEN 0 128 127.0.0.1:%d 0.0.0.0:*" % PORT])
    assert r.returncode == 1
    assert "STAGE=tdarr-port-not-relay" in r.stderr


@posix
def test_an_extra_holder_beside_the_relay_is_red(tmp_path):
    line = _ss("127.0.0.1:%d" % PORT, extra=',("x",pid=5555,fd=9)')
    r = _run(tmp_path, ss_lines=[line])
    assert r.returncode == 1
    assert "STAGE=tdarr-port-not-relay" in r.stderr


@posix
def test_hidden_manager_is_still_green(tmp_path):
    """This slot hides the user manager's /proc entry, so ss shows the relay
    alone even though systemd holds the fd. The socket unit proves the hold."""
    r = _run(tmp_path, ss_lines=[_ss("127.0.0.1:%d" % PORT, manager=0)])
    assert r.returncode == 0, r.stderr


@posix
@pytest.mark.parametrize("unit,held_by", [
    ("ActiveState=inactive\nListen=127.0.0.1:%d (Stream)\n" % PORT, "systemd"),
    ("ActiveState=active\nListen=127.0.0.1:1 (Stream)\n", "systemd"),
    ("", "systemd"),
    ("ActiveState=active\nListen=127.0.0.1:%d (Stream)\n" % PORT, "self"),
])
def test_port_not_held_by_systemd_is_red(tmp_path, unit, held_by):
    """Without the socket unit a relay restart frees the port for a squatter."""
    r = _run(tmp_path, ss_lines=[_ss("127.0.0.1:%d" % PORT, manager=0)],
             hb=_hb(listen_held_by=held_by), socket_unit=unit)
    assert r.returncode == 1
    assert "STAGE=tdarr-relay-port-not-held" in r.stderr


@posix
def test_relay_unit_not_running_is_red(tmp_path):
    r = _run(tmp_path, relay_pid=0)
    assert r.returncode == 1
    assert "STAGE=tdarr-port-not-relay" in r.stderr


@posix
def test_any_tdarr_tcp_listener_is_red(tmp_path):
    """Tdarr listening on some OTHER TCP port is just as reachable."""
    extra = _ss("127.0.0.1:45555", pid=999, comm="Tdarr_Server", manager=0)
    r = _run(tmp_path, ss_all=[_ss("127.0.0.1:%d" % PORT), extra])
    assert r.returncode == 1
    assert "STAGE=tdarr-own-tcp-listener" in r.stderr and "45555" in r.stderr


@posix
@pytest.mark.parametrize("mode", [0o755, 0o711, 0o701, 0o770])
def test_socket_dir_must_be_0700(tmp_path, mode):
    r = _run(tmp_path, dir_mode=mode)
    assert r.returncode == 1
    assert "STAGE=tdarr-socket-dir-mode" in r.stderr


@posix
def test_missing_socket_is_red(tmp_path):
    r = _run(tmp_path, make_sock=False)
    assert r.returncode == 1
    assert "STAGE=tdarr-socket-missing" in r.stderr


@posix
@pytest.mark.parametrize("kw", [{"servers_js": "vendor servers.js"},
                                {"servers_js": "x/*QFLIX-UNIX-SOCKET*/"},  # half patched
                                {"mainapi_js": "vendor mainApi.js"}])
def test_reverted_file_is_red_even_while_the_process_holds(tmp_path, kw):
    """The pusher auto-heals Tdarr with restarts, so a reverted file under a
    good process is one restart from TCP: red now, not then."""
    r = _run(tmp_path, **kw)
    assert r.returncode == 1
    assert "STAGE=tdarr-unix-patch-missing" in r.stderr


@posix
@pytest.mark.parametrize("hb,stage", [
    ({"ts": 0}, "tdarr-relay-heartbeat-stale"),
    ({"pid": 1}, "tdarr-relay-heartbeat-foreign"),
    ({"allow_uid": 0}, "tdarr-relay-not-enforcing"),
    ({"refuse_own": True}, "tdarr-relay-not-enforcing"),
    ({"refuse_own": None}, "tdarr-relay-not-enforcing"),
    ({"selftest": "fail: LookupError_: sock_diag error -1"}, "tdarr-relay-lookup-broken"),
])
def test_relay_must_be_enforcing(tmp_path, hb, stage):
    r = _run(tmp_path, hb=_hb(**hb))
    assert r.returncode == 1
    assert "STAGE=" + stage in r.stderr


@posix
@pytest.mark.parametrize("raw", ["{not json", '"a string"', "[]"])
def test_missing_heartbeat_is_red(tmp_path, raw):
    r = _run(tmp_path, hb=raw)
    assert r.returncode == 1
    assert "STAGE=tdarr-relay-heartbeat-missing" in r.stderr


@posix
def test_stale_code_is_red(tmp_path):
    """Code on disk changed but the relay was not restarted: what passed the
    selftest is not what is running."""
    code = tmp_path / "relay.py"
    code.write_text("print('PASS: x')\n", encoding="utf-8")
    r = _run(tmp_path, relay=str(code))
    assert r.returncode == 1
    assert "STAGE=tdarr-relay-stale-code" in r.stderr


@posix
def test_on_disk_selftest_failure_is_red(tmp_path):
    code = tmp_path / "relay.py"
    code.write_text("print('FAIL: foreign-uid-refused (echo)'); raise SystemExit(1)\n",
                    encoding="utf-8")
    sha = hashlib.sha256(code.read_bytes()).hexdigest()
    r = _run(tmp_path, relay=str(code), hb=_hb(code_sha256=sha))
    assert r.returncode == 1
    assert "STAGE=tdarr-relay-selftest-failed" in r.stderr and "foreign-uid-refused" in r.stderr


@posix
def test_on_disk_selftest_pass_is_green(tmp_path):
    code = tmp_path / "relay.py"
    code.write_text("print('PASS: all')\n", encoding="utf-8")
    sha = hashlib.sha256(code.read_bytes()).hexdigest()
    r = _run(tmp_path, relay=str(code), hb=_hb(code_sha256=sha))
    assert r.returncode == 0, r.stderr


@posix
def test_unreadable_config_cannot_assert(tmp_path):
    r = _run(tmp_path, conf={"webUIPort": PORT})
    assert r.returncode == 2
    assert "STAGE=tdarr-lb-config-unreadable" in r.stderr


@posix
def test_unreadable_servers_js_cannot_assert(tmp_path):
    r = _run(tmp_path, servers_js=None)
    assert r.returncode == 2
    assert "STAGE=tdarr-lb-patch-unreadable" in r.stderr


def test_the_markers_are_the_ones_50a_writes():
    src = (REPO / "scripts" / "configure" / "50a-tdarr-server-loopback.py").read_text(
        encoding="utf-8")
    assert 'UNIX_MARK = "QFLIX-UNIX-SOCKET"' in src
    assert 'WEBUI_MARK = "QFLIX-WEBUI-SHARED-IO"' in src
