#!/usr/bin/env python3
"""qflix-tcpfwd.py -- tiny loopback TCP forwarder (QFLX-33, spec 5.4 resolution 2).

WHY: the UCC container answered on three addresses (docker-proxy binds each
published IP). A native app that can bind only ONE address (SABnzbd: one
`--server host:port`) cannot reproduce that set. The spec's allowed resolution
is "a tiny loopback-to-172.17.0.1 forwarder": the app binds net.app_host (the
Docker bridge gateway the containerised arrs dial) and this process re-creates
the 127.0.0.1 listener that nginx, the canaries and the MCP tools dial.

It runs as a CHILD of the unit's main process (the app's ExecStart wrapper
starts it before `exec`-ing the app), so runtime-parity's port-owner check sees
a descendant of MainPID, and `systemctl stop` takes it down with the cgroup.
If it dies, the 127.0.0.1 health probe goes red and pusher recovery restarts
the whole unit: a dead forwarder is never silent.

Never binds a wildcard: --listen must be a concrete loopback address.
Stdlib only (runs under the app's venv python or the system python3).

usage: qflix-tcpfwd.py --listen 127.0.0.1:PORT --target HOST:PORT
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import signal
import sys

BUF = 64 * 1024


def _hostport(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit() or not (0 < int(port) < 65536):
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {text!r}")
    return host.strip("[]"), int(port)


def _loopback_only(host: str) -> None:
    try:
        ok = ipaddress.ip_address(host).is_loopback
    except ValueError:
        ok = False
    if not ok:
        raise SystemExit(f"qflix-tcpfwd: refusing to listen on non-loopback {host!r}")


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(BUF)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 - already gone
            pass


async def _serve(listen: tuple[str, int], target: tuple[str, int]) -> None:
    async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(*target), timeout=10)
        except (OSError, asyncio.TimeoutError):
            cw.close()            # upstream not up yet: the client sees a reset, retries
            return
        await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))

    server = await asyncio.start_server(handle, listen[0], listen[1], reuse_address=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):   # non-POSIX workstation
            pass
    async with server:
        await stop.wait()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="loopback TCP forwarder (QFLX-33)")
    ap.add_argument("--listen", type=_hostport, required=True)
    ap.add_argument("--target", type=_hostport, required=True)
    a = ap.parse_args(argv)
    _loopback_only(a.listen[0])
    if a.listen == a.target:
        raise SystemExit("qflix-tcpfwd: --listen and --target are the same address")
    try:
        asyncio.run(_serve(a.listen, a.target))
    except OSError as exc:
        print(f"qflix-tcpfwd: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
