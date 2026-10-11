"""scripts/data/seerr-listen.cjs (QFLX-36): the --require preload that makes Seerr
bind EXACTLY the recorded listen set (I-7) instead of upstream's one-host-or-
wildcard `server.listen(PORT[, HOST])`.

Runs real Node when it is on PATH (CI's ubuntu runner has it); the seerr-artifact
workflow additionally smoke-boots the real Seerr build through it.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PRELOAD = REPO / "scripts" / "data" / "seerr-listen.cjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="needs node")

# Upstream's call shape (server/index.ts): express app.listen(port[, host], cb).
APP = r'''
const http = require('http');
const port = Number(process.env.PORT);
const app = (req, res) => { res.setHeader('content-type', 'application/json');
  res.end(JSON.stringify({ version: '3.5.0', local: req.socket.localAddress })); };
const server = http.createServer(app);
const args = process.env.WITH_HOST ? [port, process.env.WITH_HOST] : [port];
server.on('error', (e) => { console.error('listen error ' + e.code); process.exit(1); });
server.listen(...args, () => console.log('ready'));
// An unrelated server on another port must pass through untouched.
if (process.env.OTHER_PORT) http.createServer(app).listen(Number(process.env.OTHER_PORT), '127.0.0.1');
setTimeout(() => process.exit(0), 20000);
'''


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _run(env: dict, tmp_path: Path, wait=True):
    app = tmp_path / "app.js"
    app.write_text(APP, newline="\n")
    e = dict(os.environ, **env)
    return subprocess.Popen([NODE, "--require", str(PRELOAD), str(app)], env=e,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _get(host: str, port: int) -> dict:
    with urllib.request.urlopen(f"http://{host}:{port}/", timeout=3) as r:
        return json.loads(r.read())


def _wait_get(host, port, deadline=10.0):
    end = time.time() + deadline
    while True:
        try:
            return _get(host, port)
        except OSError:
            if time.time() > end:
                raise
            time.sleep(0.2)


def _can_bind(addr: str) -> bool:
    s = socket.socket()
    try:
        s.bind((addr, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


@pytest.mark.parametrize("with_host", [None, "0.0.0.0"])
def test_binds_every_recorded_address_and_nothing_else(tmp_path, with_host):
    if not _can_bind("127.0.0.2"):
        pytest.skip("127.0.0.2 not bindable here")
    port, other = _free_port(), _free_port()
    env = {"PORT": str(port), "SEERR_LISTEN": f"127.0.0.1:{port} 127.0.0.2:{port}",
           "OTHER_PORT": str(other)}
    if with_host:
        env["WITH_HOST"] = with_host          # upstream HOST is overridden, never widened
    p = _run(env, tmp_path)
    try:
        assert _wait_get("127.0.0.1", port)["local"].endswith("127.0.0.1")
        assert _wait_get("127.0.0.2", port)["local"].endswith("127.0.0.2")
        assert _wait_get("127.0.0.1", other)["version"] == "3.5.0"      # pass-through
        with pytest.raises(OSError):
            _get("127.0.0.3", port)                                    # not a wildcard
    finally:
        p.kill()
        p.communicate()


@pytest.mark.parametrize("listen,msg", [
    ("", "SEERR_LISTEN is not set"),
    ("0.0.0.0:{p}", "wildcard"),
    ("[::]:{p}", "wildcard"),
    ("127.0.0.1:{q}", "is not on PORT"),
    ("localhost:{p}", "malformed"),
])
def test_refuses_to_start_without_an_exact_listen_set(tmp_path, listen, msg):
    port = _free_port()
    p = _run({"PORT": str(port), "SEERR_LISTEN": listen.format(p=port, q=port + 1)}, tmp_path)
    out, err = p.communicate(timeout=20)
    assert p.returncode != 0
    assert msg in err


def test_an_address_that_cannot_bind_kills_the_process(tmp_path):
    port = _free_port()
    # 192.0.2.1 (TEST-NET-1) is never a local address: EADDRNOTAVAIL -> exit 1.
    p = _run({"PORT": str(port), "SEERR_LISTEN": f"127.0.0.1:{port} 192.0.2.1:{port}"}, tmp_path)
    out, err = p.communicate(timeout=20)
    assert p.returncode == 1 and "listen error" in err
