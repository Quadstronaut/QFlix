"""tdarr-loopback-bind canary, executed against fixture state rather than grepped.

Tdarr 2.17.01 has no working auth, so the 50a loopback patch is its only access
control. Every scheduled Tdarr probe hits 127.0.0.1, which a WILDCARD listener
also answers, so before this canary a reverted servers.js put the keyless admin
API back on the box's public address with every monitor green.

The embedded python body is lifted out of the shell wrapper and EXECUTED with
canned `ss` output and fixture files (same technique as the transcode-stall
canary's tests), because grepping it would prove nothing about behaviour.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts" / "canaries" / "tdarr-loopback-bind.sh"
PORT = 40999  # fixture port, not the live one


def _body() -> str:
    src = CANARY.read_text(encoding="utf-8")
    opener = "python3 - <<PYEOF\n"
    start = src.index(opener) + len(opener)
    body = src[start:src.index("\nPYEOF", start)]
    assert "tdarr-bind-public" in body and "QFLIX-LOOPBACK-BIND" in body, "wrong block"
    return body


def test_body_survives_the_remote_quoting():
    """It runs inside sshm '...' and an UNQUOTED heredoc on the box: an
    apostrophe ends the sshm string, and $ or a backslash gets expanded."""
    body = _body()
    assert "'" not in body
    assert "$" not in body
    assert "\\" not in body


def _run(tmp_path, *, ss_lines, servers_js="x" + "/*QFLIX-LOOPBACK-BIND*/" * 2,
         conf=None):
    conf_p = tmp_path / "conf.json"
    conf_p.write_text(json.dumps(conf if conf is not None else {"serverPort": PORT}),
                      encoding="utf-8")
    ss_p = tmp_path / "ss.txt"
    ss_p.write_text("\n".join(ss_lines) + ("\n" if ss_lines else ""), encoding="utf-8")
    env = {
        "QFLIX_CANARY_TDARR_LB_CONF": str(conf_p),
        "QFLIX_CANARY_TDARR_LB_SS": str(ss_p),
        "QFLIX_CANARY_TDARR_LB_SERVERS_JS": str(tmp_path / "servers.js"),
        "PATH": "",
    }
    if servers_js is not None:
        (tmp_path / "servers.js").write_text(servers_js, encoding="utf-8")
    return subprocess.run([sys.executable, "-c", _body()], env=env,
                          capture_output=True, text=True, timeout=30)


def _ss(addr):
    return "LISTEN 0      511        %s      0.0.0.0:*" % addr


def test_loopback_only_and_patched_is_green(tmp_path):
    r = _run(tmp_path, ss_lines=[_ss("127.0.0.1:%d" % PORT)])
    assert r.returncode == 0, r.stderr
    assert "PASS: tdarr-loopback-bind" in r.stdout


@pytest.mark.parametrize("addr", ["0.0.0.0:%d" % PORT, "*:%d" % PORT, "[::]:%d" % PORT,
                                  "203.0.113.7:%d" % PORT])
def test_any_non_loopback_listener_is_red(tmp_path, addr):
    """The incident this exists for: the process took a vendor servers.js."""
    r = _run(tmp_path, ss_lines=[_ss("127.0.0.1:%d" % PORT), _ss(addr)])
    assert r.returncode == 1
    assert "STAGE=tdarr-bind-public" in r.stderr and addr in r.stderr


def test_reverted_file_is_red_even_while_the_bind_still_holds(tmp_path):
    """The pusher auto-heals Tdarr with restarts, so an unpatched file under a
    loopback process is one restart from public: red now, not then."""
    r = _run(tmp_path, ss_lines=[_ss("127.0.0.1:%d" % PORT)],
             servers_js="vendor servers.js, no marker")
    assert r.returncode == 1
    assert "STAGE=tdarr-loopback-patch-missing" in r.stderr


def test_nothing_listening_cannot_assert(tmp_path):
    """Down is the "Tdarr" monitor's fact; it must never read as a clean pass."""
    r = _run(tmp_path, ss_lines=[])
    assert r.returncode == 2
    assert "STAGE=tdarr-lb-not-listening" in r.stderr


def test_unreadable_config_cannot_assert(tmp_path):
    r = _run(tmp_path, ss_lines=[_ss("127.0.0.1:%d" % PORT)], conf={"webUIPort": PORT})
    assert r.returncode == 2
    assert "STAGE=tdarr-lb-config-unreadable" in r.stderr


def test_unreadable_servers_js_cannot_assert(tmp_path):
    r = _run(tmp_path, ss_lines=[_ss("127.0.0.1:%d" % PORT)], servers_js=None)
    assert r.returncode == 2
    assert "STAGE=tdarr-lb-servers-js-unreadable" in r.stderr


def test_the_marker_is_the_one_50a_writes():
    src = (REPO / "scripts" / "configure" / "50a-tdarr-server-loopback.py").read_text(
        encoding="utf-8")
    assert "/*QFLIX-LOOPBACK-BIND*/" in src
