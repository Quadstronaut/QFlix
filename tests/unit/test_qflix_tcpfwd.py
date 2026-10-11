"""scripts/lib/qflix-tcpfwd.py -- the loopback forwarder SABnzbd's unit runs (QFLX-33)."""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

FWD = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "qflix-tcpfwd.py"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _echo_server():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def run():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                data = c.recv(65536)
                c.sendall(b"echo:" + data)

    threading.Thread(target=run, daemon=True).start()
    return srv


def _connect(port: int, deadline: float = 10.0) -> socket.socket:
    end = time.monotonic() + deadline
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            if time.monotonic() > end:
                raise
            time.sleep(0.1)


def test_forwards_bytes_both_ways():
    srv = _echo_server()
    target = srv.getsockname()[1]
    listen = _free_port()
    p = subprocess.Popen([sys.executable, str(FWD), "--listen", f"127.0.0.1:{listen}",
                          "--target", f"127.0.0.1:{target}"])
    try:
        with _connect(listen) as c:
            c.sendall(b"GET /sabnzbd/ HTTP/1.0\r\n\r\n")
            got = b""
            while not got.endswith(b"\r\n\r\n"):
                chunk = c.recv(4096)
                if not chunk:
                    break
                got += chunk
        assert got == b"echo:GET /sabnzbd/ HTTP/1.0\r\n\r\n"
    finally:
        p.terminate()
        p.wait(timeout=10)
        srv.close()


def test_upstream_down_resets_but_keeps_listening():
    listen, dead = _free_port(), _free_port()
    p = subprocess.Popen([sys.executable, str(FWD), "--listen", f"127.0.0.1:{listen}",
                          "--target", f"127.0.0.1:{dead}"])
    try:
        for _ in range(2):
            with _connect(listen) as c:
                c.settimeout(15)
                assert c.recv(10) == b""          # closed, not hung
        assert p.poll() is None
    finally:
        p.terminate()
        p.wait(timeout=10)


@pytest.mark.parametrize("listen", ["0.0.0.0:17007", "172.17.0.1:17007", "[::]:17007",
                                    "localhost:17007"])
def test_refuses_a_non_loopback_listener(listen):
    r = subprocess.run([sys.executable, str(FWD), "--listen", listen, "--target", "172.17.0.1:17007"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "non-loopback" in (r.stderr + r.stdout)


def test_refuses_forwarding_to_itself():
    r = subprocess.run([sys.executable, str(FWD), "--listen", "127.0.0.1:17007",
                        "--target", "127.0.0.1:17007"], capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "same address" in r.stderr
