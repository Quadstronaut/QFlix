#!/usr/bin/env python3
"""QFLX-97: the only TCP door into Tdarr, and it opens for our uid alone.

WHY THIS EXISTS
---------------
Tdarr 2.17.01 has no working auth: a keyless POST /api/v2/cruddb answers 200,
and its plugin and ffmpeg-argv routes run code as our account. QFLX-96 took it
off the internet by binding 127.0.0.1, but this slot shares ONE network
namespace with every other tenant (measured 2026-11-02: 148 listening uids,
947 loopback listeners), so 127.0.0.1 is not private. `unshare` is EPERM, the
slot has no firewall access, and an auth proxy in front of a TCP port that
stays open fixes nothing.

So Tdarr no longer has a TCP port. 50a-tdarr-server-loopback.py makes it listen
on a unix socket in a 0700 directory under /run/user/<uid>, which no other uid
can traverse. This relay takes Tdarr's old address (127.0.0.1:<serverPort>) so
Tdarr Node, the pusher's health probe and every script that calls the API keep
working unchanged, and forwards a connection ONLY when the kernel says the
connecting socket belongs to our uid.

HOW THE UID IS KNOWN
--------------------
TCP has no SO_PEERCRED. Both ends of a loopback connection are local sockets,
though, and the kernel records the creating uid on each one. NETLINK_SOCK_DIAG
looks a socket up by its exact 4-tuple and returns that uid; it is what
`ss -e` reads, and it works unprivileged here (proved live 2026-11-03: our own
client socket -> our uid, another tenant's established socket -> that tenant's
uid, a tuple that does not exist -> ENOENT). We query the CLIENT end, i.e. the
socket whose local address is the peer we accepted, and accept the answer only
if the 4-tuple it returns is the one we asked for and it is not a listener.

FAIL CLOSED
-----------
Every path that is not "the kernel said our uid" is a refusal: lookup error,
timeout, mismatched or truncated reply, non-IPv4 peer, a foreign uid. Nothing
is read from the client before the decision, so a refused peer has spoken to
nothing but accept(2) and a netlink query about itself. Refusals are reset
(SO_LINGER 0) rather than closed politely.

HEARTBEAT
---------
Every HEARTBEAT_S the relay re-proves its own lookup (an ephemeral loopback
pair, looked up through the same code path) and writes relay.json beside the
socket. The tdarr-loopback-bind canary reds on a stale or failing heartbeat,
a pid that is not the unit's, or code on disk that differs from what is
running. A relay that cannot look up uids refuses everything, so "Tdarr" goes
red too.

Usage (box):
  tdarr-uid-relay.py                 run (systemd unit tdarr-uid-relay.service)
  tdarr-uid-relay.py --selftest      prove allow / refuse / fail-closed end to
                                     end on throwaway sockets, then exit 0 or 1
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import time

DEFAULT_CONF = "~/.apps/tdarr/configs/Tdarr_Server_Config.json"
LISTEN_HOST = "127.0.0.1"
SOCK_NAME = "tdarr.sock"
STATE_NAME = "relay.json"
HEARTBEAT_S = 60
BIND_WAIT_S = 90          # Tdarr may still hold the port during a cutover restart
UPSTREAM_TIMEOUT_S = 5
MAX_CONNS = 512           # own-uid sessions; Tdarr Node holds one, the UI a few
CHUNK = 65536

# linux/netlink.h, linux/sock_diag.h, linux/inet_diag.h
NETLINK_SOCK_DIAG = 4
SOCK_DIAG_BY_FAMILY = 20
NLM_F_REQUEST = 0x1
NLMSG_ERROR = 0x2
NLMSG_DONE = 0x3
INET_DIAG_NOCOOKIE = 0xFFFFFFFF
TCP_LISTEN = 10
_NLMSG_HDR = struct.Struct("=IHHII")          # len, type, flags, seq, pid
_REQ_HEAD = struct.Struct("=BBBBI")           # family, protocol, ext, pad, states
_MSG_HEAD = struct.Struct("=BBBB")            # family, state, timer, retrans
SOCKID_LEN = 48                               # sport dport src[16] dst[16] if cookie[2]
UID_OFFSET = _MSG_HEAD.size + SOCKID_LEN + 12  # after expires, rqueue, wqueue


class LookupError_(RuntimeError):
    """The kernel did not give us a trustworthy uid. Always a refusal."""


def runtime_dir(uid: int | None = None) -> str:
    # Same path 50a writes into servers.js. Not $XDG_RUNTIME_DIR: the unit and
    # the patched Tdarr must agree without sharing an environment.
    return f"/run/user/{os.getuid() if uid is None else uid}/qflix-tdarr"


def decide(peer_uid, allowed_uid) -> bool:
    """The whole policy. Anything but an exact int match is a refusal."""
    return (
        type(peer_uid) is int
        and type(allowed_uid) is int
        and allowed_uid >= 0
        and peer_uid == allowed_uid
    )


def _sockid(src: str, sport: int, dst: str, dport: int) -> bytes:
    return (
        struct.pack("!HH", sport, dport)
        + socket.inet_aton(src) + b"\0" * 12
        + socket.inet_aton(dst) + b"\0" * 12
        + struct.pack("=I", 0)
        + struct.pack("=II", INET_DIAG_NOCOOKIE, INET_DIAG_NOCOOKIE)
    )


def build_request(src: str, sport: int, dst: str, dport: int, seq: int = 1) -> bytes:
    """Exact (non-dump) lookup of the TCP socket bound to src:sport -> dst:dport."""
    body = _REQ_HEAD.pack(socket.AF_INET, socket.IPPROTO_TCP, 0, 0, 0xFFFFFFFF)
    body += _sockid(src, sport, dst, dport)
    return _NLMSG_HDR.pack(_NLMSG_HDR.size + len(body), SOCK_DIAG_BY_FAMILY,
                           NLM_F_REQUEST, seq, 0) + body


def parse_response(data: bytes, src: str, sport: int, dst: str, dport: int,
                   seq: int = 1) -> int:
    """Return the uid in a sock_diag reply, or raise. Trusts nothing it did not ask for."""
    if len(data) < _NLMSG_HDR.size:
        raise LookupError_("short netlink reply")
    length, mtype, _flags, rseq, _pid = _NLMSG_HDR.unpack_from(data, 0)
    if length > len(data) or length < _NLMSG_HDR.size:
        raise LookupError_("bad netlink length")
    if rseq != seq:
        raise LookupError_("netlink sequence mismatch")
    if mtype == NLMSG_ERROR:
        err = struct.unpack_from("=i", data, _NLMSG_HDR.size)[0] if length >= 20 else 0
        raise LookupError_(f"sock_diag error {err}")
    if mtype != SOCK_DIAG_BY_FAMILY:
        raise LookupError_(f"unexpected netlink type {mtype}")
    body = data[_NLMSG_HDR.size:length]
    if len(body) < UID_OFFSET + 4:
        raise LookupError_("truncated inet_diag_msg")
    family, state, _t, _r = _MSG_HEAD.unpack_from(body, 0)
    if family != socket.AF_INET:
        raise LookupError_("reply is not AF_INET")
    if state == TCP_LISTEN:
        raise LookupError_("reply is a listener, not the peer")
    rid = body[_MSG_HEAD.size:_MSG_HEAD.size + SOCKID_LEN]
    want = _sockid(src, sport, dst, dport)
    # Compare ports and both addresses; the interface and cookie fields are the
    # kernel's to fill in.
    if rid[:4] != want[:4] or rid[4:36] != want[4:36]:
        raise LookupError_("reply is for a different socket")
    return struct.unpack_from("=I", body, UID_OFFSET)[0]


def lookup_uid(src: str, sport: int, dst: str, dport: int, timeout: float = 2.0) -> int:
    """uid that owns the local TCP socket src:sport connected to dst:dport."""
    nl = None
    try:
        nl = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_SOCK_DIAG)
        nl.settimeout(timeout)
        seq = (os.getpid() ^ int(time.monotonic_ns())) & 0x7FFFFFFF or 1
        nl.sendto(build_request(src, sport, dst, dport, seq), (0, 0))
        return parse_response(nl.recv(8192), src, sport, dst, dport, seq)
    except LookupError_:
        raise
    except Exception as exc:  # timeout, EPERM, anything: fail closed
        raise LookupError_(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if nl is not None:
            nl.close()


def peer_uid(sock) -> int:
    """uid of whoever is on the other end of an ACCEPTED loopback TCP socket."""
    if sock.family != socket.AF_INET:
        raise LookupError_("peer is not IPv4")
    lip, lport = sock.getsockname()[:2]
    pip, pport = sock.getpeername()[:2]
    # The peer's socket has local = (pip, pport) and remote = (lip, lport).
    return lookup_uid(pip, pport, lip, lport)


def _reset(writer) -> None:
    sock = writer.get_extra_info("socket")
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except Exception:
        pass
    writer.close()


class Relay:
    def __init__(self, upstream: str, *, allowed_uid: int | None = None,
                 refuse_own: bool = False, lookup=peer_uid, log=None):
        self.upstream = upstream
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        # Test mode for the live refusal proof: compare against a uid nobody has.
        self.refuse_own = refuse_own
        self.lookup = lookup
        self.log = log or (lambda msg: print(msg, flush=True))
        self.stats = {"accepted": 0, "refused_foreign": 0, "refused_lookup": 0,
                      "upstream_fail": 0, "refused_busy": 0}
        self.active = 0
        self._last_refusal_log = 0.0
        self._quiet = 0

    def _note_refusal(self, why: str) -> None:
        # Rate-limited: a co-tenant port scan must not fill the journal.
        now = time.monotonic()
        if now - self._last_refusal_log >= 10:
            extra = f" (+{self._quiet} more since last line)" if self._quiet else ""
            self.log(f"tdarr-uid-relay: REFUSED {why}{extra}")
            self._last_refusal_log, self._quiet = now, 0
        else:
            self._quiet += 1

    async def handle(self, reader, writer) -> None:
        sock = writer.get_extra_info("socket")
        try:
            uid = self.lookup(sock)
        except Exception as exc:
            self.stats["refused_lookup"] += 1
            self._note_refusal(f"peer uid unknown ({exc})")
            _reset(writer)
            return
        allowed = -1 if self.refuse_own else self.allowed_uid
        if not decide(uid, allowed):
            self.stats["refused_foreign"] += 1
            self._note_refusal(f"peer uid {uid}")
            _reset(writer)
            return
        if self.active >= MAX_CONNS:
            self.stats["refused_busy"] += 1
            self._note_refusal("connection cap reached")
            _reset(writer)
            return
        try:
            ur, uw = await asyncio.wait_for(
                asyncio.open_unix_connection(self.upstream), UPSTREAM_TIMEOUT_S)
        except Exception as exc:
            self.stats["upstream_fail"] += 1
            self._note_refusal(f"upstream unavailable ({type(exc).__name__})")
            _reset(writer)
            return
        self.stats["accepted"] += 1
        self.active += 1
        try:
            await asyncio.gather(_pipe(reader, uw), _pipe(ur, writer))
        finally:
            self.active -= 1
            for w in (uw, writer):
                try:
                    w.close()
                except Exception:
                    pass


async def _pipe(reader, writer) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()   # half-close: HTTP clients may still be reading
    except Exception:
        writer.close()


def listen_socket(port: int, host: str = LISTEN_HOST, wait_s: float = 0) -> socket.socket:
    """Bind host:port, retrying while the old Tdarr listener lets go of it."""
    deadline = time.monotonic() + wait_s
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # TIME_WAIT only; not REUSEPORT
        try:
            s.bind((host, port))
            s.listen(128)
            s.setblocking(False)
            return s
        except OSError:
            s.close()
            if time.monotonic() >= deadline:
                raise
            # Short: while nothing holds the port a co-tenant could take it.
            time.sleep(0.2)


def own_lookup_selftest(lookup=peer_uid) -> str:
    """Prove, in THIS process, that the kernel still reports our uid for a peer."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cli = acc = None
    try:
        srv.bind((LISTEN_HOST, 0))
        srv.listen(1)
        cli = socket.create_connection(srv.getsockname(), timeout=2)
        acc, _ = srv.accept()
        uid = lookup(acc)
        return "pass" if uid == os.getuid() else f"fail: lookup gave {uid}"
    except Exception as exc:
        return f"fail: {type(exc).__name__}: {exc}"
    finally:
        for s in (acc, cli, srv):
            if s is not None:
                s.close()


def code_sha256() -> str:
    with open(os.path.abspath(__file__), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def write_state(path: str, payload: dict) -> None:
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
    os.replace(tmp, path)


async def _heartbeat(relay: Relay, state_path: str, base: dict) -> None:
    while True:
        payload = dict(base, ts=int(time.time()), active=relay.active,
                       selftest=own_lookup_selftest(), **relay.stats)
        try:
            write_state(state_path, payload)
        except OSError as exc:
            relay.log(f"tdarr-uid-relay: heartbeat write failed: {exc}")
        await asyncio.sleep(HEARTBEAT_S)


def read_port(conf: str) -> int:
    with open(os.path.expanduser(conf), encoding="utf-8") as fh:
        return int(json.load(fh)["serverPort"])


async def serve(args) -> int:
    rdir = runtime_dir()
    upstream = args.upstream or os.path.join(rdir, SOCK_NAME)
    port = args.port or read_port(args.conf)
    lsock = listen_socket(port, wait_s=BIND_WAIT_S)
    relay = Relay(upstream, refuse_own=args.test_refuse_own)
    server = await asyncio.start_server(relay.handle, sock=lsock)
    relay.log(f"tdarr-uid-relay: {LISTEN_HOST}:{port} -> {upstream}, "
              f"allow uid {relay.allowed_uid}{' [TEST: refusing own uid]' if args.test_refuse_own else ''}")
    hb = None
    # A test instance must never overwrite the live relay's heartbeat.
    if not args.no_state and not (args.test_refuse_own and not args.state):
        state_path = args.state or os.path.join(rdir, STATE_NAME)
        base = {"pid": os.getpid(), "started": int(time.time()), "listen": f"{LISTEN_HOST}:{port}",
                "upstream": upstream, "allow_uid": relay.allowed_uid,
                "refuse_own": relay.refuse_own, "code_sha256": code_sha256()}
        hb = asyncio.ensure_future(_heartbeat(relay, state_path, base))
    async with server:
        await server.serve_forever()
    if hb:
        hb.cancel()
    return 0


# ---------------------------------------------------------------- selftest
async def _echo_upstream(path: str, seen: list):
    async def h(r, w):
        seen.append(1)
        data = await r.read(CHUNK)
        w.write(data)
        await w.drain()
        w.close()
    return await asyncio.start_unix_server(h, path=path)


async def _try(port: int) -> bytes:
    r, w = await asyncio.open_connection(LISTEN_HOST, port)
    try:
        w.write(b"qflix-ping")
        await w.drain()
        return await asyncio.wait_for(r.read(CHUNK), 5)
    except (ConnectionError, OSError):
        return b""
    finally:
        w.close()


async def _selftest() -> list:
    results = []
    with tempfile.TemporaryDirectory(prefix="qflix-relay-st-") as d:
        os.chmod(d, 0o700)
        up = os.path.join(d, "up.sock")
        seen: list = []
        upstream = await _echo_upstream(up, seen)

        async def case(name, relay, want_echo):
            lsock = listen_socket(0)
            port = lsock.getsockname()[1]
            srv = await asyncio.start_server(relay.handle, sock=lsock)
            before = len(seen)
            got = await _try(port)
            srv.close()
            await srv.wait_closed()
            reached = len(seen) > before
            ok = (got == b"qflix-ping" and reached) if want_echo else (got == b"" and not reached)
            results.append((name, ok, f"echo={got!r} upstream_reached={reached}"))

        quiet = lambda _m: None  # noqa: E731
        results.append(("kernel-reports-own-uid", own_lookup_selftest() == "pass", own_lookup_selftest()))
        await case("own-uid-forwarded", Relay(up, log=quiet), True)
        await case("foreign-uid-refused", Relay(up, refuse_own=True, log=quiet), False)

        def boom(_sock):
            raise LookupError_("injected lookup failure")
        await case("lookup-failure-refused", Relay(up, lookup=boom, log=quiet), False)
        await case("other-uid-refused", Relay(up, lookup=lambda s: os.getuid() + 1, log=quiet), False)
        upstream.close()
        await upstream.wait_closed()
    return results


def selftest() -> int:
    results = asyncio.run(_selftest())
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}: {name} ({detail})")
    return 0 if all(ok for _, ok, _ in results) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--conf", default=DEFAULT_CONF, help="Tdarr server config (serverPort)")
    ap.add_argument("--port", type=int, help="listen port (default: serverPort)")
    ap.add_argument("--upstream", help="Tdarr unix socket (default: runtime dir)")
    ap.add_argument("--state", help="heartbeat file (default: runtime dir)")
    ap.add_argument("--no-state", action="store_true", help="no heartbeat file (test instances)")
    ap.add_argument("--test-refuse-own", action="store_true",
                    help="treat our own uid as foreign: refuse everyone (live refusal proof)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    try:
        return asyncio.run(serve(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
