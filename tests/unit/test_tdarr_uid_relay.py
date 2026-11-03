"""QFLX-97: tdarr-uid-relay forwards our uid and nobody else's.

Tdarr 2.17.01 has no working auth and this slot shares one network namespace
with ~150 other tenants, so the relay's uid decision IS Tdarr's access control.
Three layers are pinned here:

  * the policy (`decide`) and the netlink parsing, on crafted bytes, so a reply
    for the wrong socket, a listener, an error or a truncated message can never
    turn into a uid;
  * the relay end to end on real sockets (Linux only: it needs
    NETLINK_SOCK_DIAG), including the cases a live box cannot produce on demand:
    a foreign uid and a lookup that fails;
  * the deployment shape: unit, drop-ins, post step, heartbeat.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
import struct
import sys

import pytest

_REPO = os.path.join(os.path.dirname(__file__), "..", "..")
_RELAY = os.path.join(_REPO, "scripts", "ops", "tdarr-uid-relay.py")
_50D = os.path.join(_REPO, "scripts", "configure", "50d-tdarr-uid-relay.sh")
_INSTALL = os.path.join(_REPO, "scripts", "configure", "50-tdarr-install.sh")
_HEARTBEAT = os.path.join(_REPO, "scripts", "ops", "heartbeat-tdarr-server.sh")

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="NETLINK_SOCK_DIAG is Linux-only")


def _load():
    spec = importlib.util.spec_from_file_location("tdarr_uid_relay", _RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load()


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------ policy
@pytest.mark.parametrize("peer,allowed,ok", [
    (1120, 1120, True),
    (1121, 1120, False),
    (0, 1120, False),          # root is not us either
    (None, 1120, False),       # unknown peer
    ("1120", 1120, False),     # a string is not a uid
    (True, 1, False),          # bool is an int subclass; still refused
    (1120, -1, False),         # the refuse-own sentinel matches nobody
    (-1, -1, False),
])
def test_decide_is_exact_int_equality_only(peer, allowed, ok):
    assert R.decide(peer, allowed) is ok


# ------------------------------------------------------------------ netlink
SRC, SPORT, DST, DPORT, SEQ = "127.0.0.1", 50123, "127.0.0.1", 42018, 7


def _reply(*, uid=1120, state=1, family=socket.AF_INET, sport=SPORT, dport=DPORT,
           src=SRC, dst=DST, seq=SEQ, mtype=20, truncate=0):
    sockid = (struct.pack("!HH", sport, dport) + socket.inet_aton(src) + b"\0" * 12
              + socket.inet_aton(dst) + b"\0" * 12 + struct.pack("=I", 1)
              + struct.pack("=II", 5, 6))
    body = (struct.pack("=BBBB", family, state, 0, 0) + sockid
            + struct.pack("=III", 0, 0, 0) + struct.pack("=II", uid, 999))
    body = body[:len(body) - truncate] if truncate else body
    return struct.pack("=IHHII", 16 + len(body), mtype, 0, seq, 0) + body


def test_request_is_an_exact_lookup_not_a_dump():
    req = R.build_request(SRC, SPORT, DST, DPORT, SEQ)
    length, mtype, flags, seq, _ = struct.unpack_from("=IHHII", req, 0)
    assert length == len(req) == 16 + 8 + 48
    assert mtype == 20 and seq == SEQ
    assert flags == 0x1, "NLM_F_DUMP would return every socket on the box"
    fam, proto = struct.unpack_from("=BB", req, 16)
    assert (fam, proto) == (socket.AF_INET, socket.IPPROTO_TCP)
    sport, dport = struct.unpack_from("!HH", req, 24)
    assert (sport, dport) == (SPORT, DPORT)


def test_parse_returns_the_uid_of_the_socket_we_asked_about():
    assert R.parse_response(_reply(uid=4242), SRC, SPORT, DST, DPORT, SEQ) == 4242


@pytest.mark.parametrize("kw", [
    {"sport": SPORT + 1},
    {"dport": DPORT + 1},
    {"src": "127.0.0.2"},
    {"dst": "127.0.0.2"},
    {"state": 10},                 # a LISTEN socket on the peer's port, not the peer
    {"family": socket.AF_INET6},
    {"seq": SEQ + 1},
    {"mtype": 3},                  # NLMSG_DONE carries no socket
    {"truncate": 6},
])
def test_parse_refuses_anything_but_the_exact_peer(kw):
    with pytest.raises(R.LookupError_):
        R.parse_response(_reply(**kw), SRC, SPORT, DST, DPORT, SEQ)


def test_parse_turns_a_kernel_error_into_a_refusal():
    err = struct.pack("=IHHII", 36, 2, 0, SEQ, 0) + struct.pack("=i", -2) + b"\0" * 16
    with pytest.raises(R.LookupError_, match="error -2"):
        R.parse_response(err, SRC, SPORT, DST, DPORT, SEQ)


@pytest.mark.parametrize("data", [b"", b"\0" * 8, struct.pack("=IHHII", 999, 20, 0, SEQ, 0)])
def test_parse_refuses_short_or_lying_lengths(data):
    with pytest.raises(R.LookupError_):
        R.parse_response(data, SRC, SPORT, DST, DPORT, SEQ)


def test_lookup_fails_closed_on_any_socket_error(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise PermissionError("netlink denied")
    monkeypatch.setattr(R.socket, "socket", Boom)
    monkeypatch.setattr(R.socket, "AF_NETLINK", 16, raising=False)
    with pytest.raises(R.LookupError_):
        R.lookup_uid(SRC, SPORT, DST, DPORT)


# ------------------------------------------------------------------ end to end
async def _roundtrip(relay, tmp_path):
    up = str(tmp_path / "up.sock")
    seen = []

    async def echo(r, w):
        seen.append(1)
        w.write(await r.read(65536))
        await w.drain()
        w.close()

    upstream = await asyncio.start_unix_server(echo, path=up)
    relay.upstream = up
    lsock = R.listen_socket(0)
    srv = await asyncio.start_server(relay.handle, sock=lsock)
    r, w = await asyncio.open_connection("127.0.0.1", lsock.getsockname()[1])
    w.write(b"hello")
    await w.drain()
    try:
        got = await asyncio.wait_for(r.read(65536), 5)
    except ConnectionError:
        got = b""
    w.close()
    srv.close()
    await srv.wait_closed()
    upstream.close()
    await upstream.wait_closed()
    return got, len(seen)


def _quiet(_m):
    pass


@linux_only
def test_kernel_lookup_reports_our_uid():
    assert R.own_lookup_selftest() == "pass"


@linux_only
def test_own_uid_is_forwarded(tmp_path):
    relay = R.Relay("unset", log=_quiet)
    got, reached = asyncio.run(_roundtrip(relay, tmp_path))
    assert (got, reached) == (b"hello", 1)
    assert relay.stats["accepted"] == 1


@linux_only
def test_foreign_uid_is_refused_before_upstream(tmp_path):
    """No second account on the slot, so the foreign peer is a lookup that
    answers a uid that is not ours: the relay must never dial Tdarr."""
    relay = R.Relay("unset", lookup=lambda s: os.getuid() + 1, log=_quiet)
    got, reached = asyncio.run(_roundtrip(relay, tmp_path))
    assert (got, reached) == (b"", 0)
    assert relay.stats["refused_foreign"] == 1


@linux_only
def test_refuse_own_test_mode_refuses_our_real_uid(tmp_path):
    """The live refusal proof: the REAL kernel lookup, our real uid, judged
    as foreign."""
    relay = R.Relay("unset", refuse_own=True, log=_quiet)
    got, reached = asyncio.run(_roundtrip(relay, tmp_path))
    assert (got, reached) == (b"", 0)
    assert relay.stats["refused_foreign"] == 1


@linux_only
def test_lookup_failure_is_refused(tmp_path):
    def boom(_s):
        raise R.LookupError_("no answer")
    relay = R.Relay("unset", lookup=boom, log=_quiet)
    got, reached = asyncio.run(_roundtrip(relay, tmp_path))
    assert (got, reached) == (b"", 0)
    assert relay.stats["refused_lookup"] == 1


@linux_only
def test_dead_upstream_is_a_closed_connection_not_a_hang(tmp_path):
    relay = R.Relay(str(tmp_path / "nothing.sock"), log=_quiet)

    async def go():
        lsock = R.listen_socket(0)
        srv = await asyncio.start_server(relay.handle, sock=lsock)
        r, w = await asyncio.open_connection("127.0.0.1", lsock.getsockname()[1])
        try:
            got = await asyncio.wait_for(r.read(10), 10)
        except ConnectionError:
            got = b""
        w.close()
        srv.close()
        await srv.wait_closed()
        return got
    assert asyncio.run(go()) == b""
    assert relay.stats["upstream_fail"] == 1


@linux_only
def test_selftest_command_passes(capsys):
    assert R.selftest() == 0
    out = capsys.readouterr().out
    for name in ("kernel-reports-own-uid", "own-uid-forwarded", "foreign-uid-refused",
                 "lookup-failure-refused", "other-uid-refused"):
        assert f"PASS: {name}" in out


def test_refusals_are_rate_limited_in_the_log():
    lines = []
    relay = R.Relay("unset", log=lines.append)
    for _ in range(50):
        relay._note_refusal("peer uid 9999")
    assert len(lines) == 1, "a co-tenant port scan must not fill the log"


def test_relay_listens_on_loopback_only():
    assert R.LISTEN_HOST == "127.0.0.1"
    src = _read(_RELAY)
    assert "SO_REUSEPORT" not in src.split('"""', 2)[2].replace("not REUSEPORT", "")


def test_test_mode_never_overwrites_the_live_heartbeat():
    src = _read(_RELAY)
    assert "not (args.test_refuse_own and not args.state)" in src


def test_socket_path_matches_the_one_50a_patches_in():
    spec = importlib.util.spec_from_file_location(
        "p50a", os.path.join(_REPO, "scripts", "configure", "50a-tdarr-server-loopback.py"))
    p = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p)
    assert p.SOCK_DIR_JS == "'/run/user/'+process.getuid()+'/qflix-tdarr'"
    assert "f\"/run/user/{os.getuid() if uid is None else uid}/qflix-tdarr\"" in _read(_RELAY)
    assert R.SOCK_NAME == "tdarr.sock" and "'tdarr.sock'" in p._sock_expr("x", "tdarr.sock")


# ------------------------------------------------------------------ deployment
def test_systemd_holds_the_port_not_the_relay():
    """A relay restart must queue connections, never free the port: whoever
    holds it is "the server" to our Node."""
    src = _read(_50D)
    assert "ListenStream=127.0.0.1:${PORT}" in src
    assert "ReusePort=yes" not in src and "Accept=yes" not in src
    assert "Requires=tdarr-uid-relay.socket" in src
    assert "WantedBy=sockets.target" in src
    # The socket is restarted ONLY when its own unit changed.
    i = src.index("systemctl --user restart tdarr-uid-relay.socket")
    assert 'if [ "$SOCK_CHANGED" = 1 ]' in src[i - 300:i]


def test_50d_never_starts_the_socket_while_tdarr_holds_the_port():
    src = _read(_50D)
    guard = src.index("grep -q '\"Tdarr_'")
    assert guard < src.index("systemctl --user start tdarr-uid-relay.socket")
    assert "exit 0" in src[guard:guard + 400]


TAKE_FD = """
import importlib.util, os, socket, sys
spec = importlib.util.spec_from_file_location("r", sys.argv[1])
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1); port = s.getsockname()[1]
os.dup2(s.fileno(), 3)
env = {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "1"}
assert r.systemd_listen_socket(port, {"LISTEN_PID": "1", "LISTEN_FDS": "1"}) is None
got = r.systemd_listen_socket(port, env)
assert got.getsockname()[1] == port
try:
    r.systemd_listen_socket(port + 1, env)
    print("ACCEPTED-WRONG")
except SystemExit:
    print("OK")
"""


@linux_only
def test_takes_the_listening_fd_from_systemd():
    """Run in a child so fd 3 can be a real listening socket, as systemd
    passes it. The wrong address is refused, not silently served."""
    import subprocess
    out = subprocess.run([sys.executable, "-c", TAKE_FD, _RELAY],
                         capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "OK", out.stderr


def test_unit_restarts_forever_and_runs_the_shipped_file():
    src = _read(_50D)
    assert "Restart=always" in src
    assert "StartLimitIntervalSec=0" in src
    assert "ExecStart=/usr/bin/python3 %h/scripts/ops/tdarr-uid-relay.py" in src
    assert "WantedBy=default.target" in src


def test_relay_never_stops_with_tdarr():
    """Wants=, never BindsTo=/PartOf=/Requires=: a Tdarr restart must not take
    the port's gatekeeper down with it (a squatter could take the port)."""
    src = _read(_50D)
    for bad in ("BindsTo=", "PartOf=", "Requires=tdarr-server", "Requires=tdarr-node"):
        assert bad not in src
    assert src.count("Wants=tdarr-uid-relay.socket tdarr-uid-relay.service") == 2


def test_50d_refuses_code_whose_selftest_fails_before_installing_it():
    src = _read(_50D)
    assert src.index("--selftest") < src.index('mv -f "$OPS/tdarr-uid-relay.py.qflix-new"')
    assert "set -euo pipefail" in src.split("RELAY", 2)[1]


def test_50d_backs_up_what_it_replaces():
    src = _read(_50D)
    assert 'qflix-bak-$STAMP' in src


def test_installer_runs_the_relay_before_the_restart_that_needs_it():
    code = "\n".join(ln for ln in _read(_INSTALL).splitlines() if not ln.lstrip().startswith("#"))
    assert code.index("50d-tdarr-uid-relay.sh") < code.index(
        "systemctl --user start tdarr-server.service")


def test_installer_hands_the_port_over_with_no_gap():
    """Stop Tdarr (it may still hold the port on TCP), let the socket unit take
    the port, THEN start the patched Tdarr on its unix socket."""
    code = "\n".join(ln for ln in _read(_INSTALL).splitlines() if not ln.lstrip().startswith("#"))
    stop = code.index("systemctl --user stop tdarr-server.service")
    take = code.index("systemctl --user start tdarr-uid-relay.socket tdarr-uid-relay.service")
    start = code.index("systemctl --user start tdarr-server.service")
    assert stop < take < start
    assert "systemctl --user restart tdarr-server.service" not in code


def test_zip_swap_post_step_starts_the_relay():
    sys.path.insert(0, os.path.join(_REPO, "scripts", "maint"))
    from lib import manifest  # noqa: E402
    steps = manifest.load(os.path.join(_REPO, "manifest", "apps.yaml")).app(
        "tdarr-server").upgrade.raw["post_steps"]
    assert steps == ["python3 ~/scripts/configure/50a-tdarr-server-loopback.py",
                     "systemctl --user start tdarr-uid-relay.socket tdarr-uid-relay.service"]


def test_heartbeat_judges_tdarr_on_its_socket_and_heals_the_relay_separately():
    src = _read(_HEARTBEAT)
    assert "--unix-socket" in src
    assert "systemctl --user restart tdarr-uid-relay.service" in src
    assert "systemctl --user start tdarr-uid-relay.socket" in src
    assert "systemctl --user restart tdarr-uid-relay.socket" not in src
    # The relay branch must exit before the Tdarr restart path is reachable.
    relay = src.index("systemctl --user restart tdarr-uid-relay.service")
    assert src.index("exit 0", relay) < src.index("systemctl --user restart tdarr-server.service")
