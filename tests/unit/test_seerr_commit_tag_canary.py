"""Tests for scripts/canaries/seerr-commit-tag.sh (QFLX-58).

QFLX-53 (the "Seerr Updated / Reload Seerr" modal every 60s) was reported by
a member and detected by nothing. The invariant: the commitTag literal baked
into the client bundle (compiled as `"<baked>"!==a.commitTag`) must equal the
server's /api/v1/status commitTag.

These drive the REAL script end to end -- real bash, real grep, real curl over
loopback to a fake status server, fixture chunk files on disk -- because there
is no shell-lint gate and only an executed script proves the verdict. Cases:
agree (local/local, commit/commit), mismatch in both directions, every
extraction shape, and every could-not-assert leg (exit 2, never green).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "canaries" / "seerr-commit-tag.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None
    or shutil.which("curl") is None,
    reason="seerr-commit-tag.sh needs bash + python3 + curl on PATH",
)

COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"


# ---------------------------------------------------------------------------
# Fake Seerr status endpoint
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):                                    # noqa: N802 - stdlib
        status, body = self.server.reply
        if self.path != "/api/v1/status":
            status, body = 404, b'{"error":"no route"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    httpd.reply = (200, b"{}")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def set_reply(obj=None, *, status=200, raw=None):
        httpd.reply = (status, raw if raw is not None else json.dumps(obj).encode())

    set_reply.port = httpd.socket.getsockname()[1]
    set_reply.url = "http://127.0.0.1:%d/api/v1/status" % set_reply.port
    yield set_reply
    httpd.shutdown()
    httpd.server_close()


def _status(tag):
    # Shape of the live answer: {"version":"3.5.0","commitTag":"local",...}
    return {"version": "3.5.0", "commitTag": tag, "updateAvailable": False,
            "commitsBehind": 0, "restartRequired": False}


def _app(tmp_path, chunks):
    """Lay out <app>/.next/static/chunks/<name> files; returns the app dir."""
    app = tmp_path / "app"
    d = app / ".next" / "static" / "chunks"
    d.mkdir(parents=True)
    for name, text in chunks.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return app


def _chunk_fwd(tag, ident="a"):
    # Minified StatusChecker, as compiled on the box today with tag "local".
    return ('(0,o.useEffect)(()=>{if(%s){"%s"!==%s.commitTag&&s(!0)}},[%s]);'
            % (ident, tag, ident, ident))


def _run(app, url=None, env=None):
    full = dict(os.environ)
    full["QFLIX_CANARY_SCT_APP_DIR"] = str(app)
    full["QFLIX_CANARY_SCT_TIMEOUT_S"] = "5"
    if url is not None:
        full["QFLIX_CANARY_SCT_URL"] = url
    if env:
        full.update(env)
    return subprocess.run(["bash", str(SCRIPT)], env=full, capture_output=True,
                          text=True, timeout=60)


def _stage(res):
    for line in res.stderr.splitlines():
        if line.startswith("STAGE="):
            return line.split()[0][len("STAGE="):]
    return None


# ---------------------------------------------------------------------------
# Agree -> PASS
# ---------------------------------------------------------------------------


def test_local_bundle_and_local_server_pass(tmp_path, server):
    """Today's live state: bundle baked "local", status says "local"."""
    server(_status("local"))
    res = _run(_app(tmp_path, {"pages/_app-1.js": _chunk_fwd("local")}), server.url)
    assert res.returncode == 0, res.stderr
    assert "PASS: seerr-commit-tag" in res.stdout
    assert "commitTag=local" in res.stdout


def test_commit_bundle_and_same_commit_server_pass(tmp_path, server):
    """A correctly built artifact (seerr-artifact.yml bakes the 40-hex)."""
    server(_status(COMMIT))
    res = _run(_app(tmp_path, {"c1.js": _chunk_fwd(COMMIT)}), server.url)
    assert res.returncode == 0, res.stderr
    assert COMMIT in res.stdout


def test_baked_commit_wins_over_a_handwritten_local_check(tmp_path, server):
    """A dev-build `"local"!==x.commitTag` elsewhere in the bundle is a
    hand-written literal, not the baked value; the commit literal wins."""
    server(_status(COMMIT))
    app = _app(tmp_path, {"c1.js": _chunk_fwd(COMMIT),
                          "c2.js": 'x=function(e){return"local"!==e.commitTag}'})
    res = _run(app, server.url)
    assert res.returncode == 0, res.stderr


def test_same_literal_in_many_chunks_is_not_ambiguous(tmp_path, server):
    server(_status(COMMIT))
    app = _app(tmp_path, {"a.js": _chunk_fwd(COMMIT), "b.js": _chunk_fwd(COMMIT, "t"),
                          "pages/settings.js": _chunk_fwd(COMMIT, "n")})
    assert _run(app, server.url).returncode == 0


@pytest.mark.parametrize("text", [
    'if(t.commitTag!=="%s")r(!0)',                                # reversed operands
    'if("%s"!=t.commitTag)r(!0)',                                 # loose !=
    'if("%s"!==(null==t?void 0:t.commitTag))r(!0)',               # SWC ?. lowering
    'if("%s"!==$e.commitTag)r(!0)',                               # $ in identifier
], ids=["reversed", "loose", "optional-chain", "dollar-ident"])
def test_every_comparison_shape_is_extracted(tmp_path, server, text):
    server(_status(COMMIT))
    res = _run(_app(tmp_path, {"c.js": text % COMMIT}), server.url)
    assert res.returncode == 0, res.stderr
    # ...and the same shape DISAGREEING is caught, proving the literal was read.
    server(_status("local"))
    res = _run(tmp_path / "app", server.url)
    assert res.returncode == 1 and _stage(res) == "seerr-commit-tag-mismatch"


def test_port_file_path_is_used_when_no_url_override(tmp_path, server):
    server(_status("local"))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "seerr.port").write_text("%d\n" % server.port, encoding="utf-8")
    res = _run(_app(tmp_path, {"c.js": _chunk_fwd("local")}),
               env={"MANITOBA_SECRETS": str(secrets)})
    assert res.returncode == 0, res.stderr


# ---------------------------------------------------------------------------
# Mismatch -> exit 1
# ---------------------------------------------------------------------------


def test_local_bundle_vs_commit_server_is_the_modal_loop(tmp_path, server):
    """QFLX-53's shape: client says "local", server reports a commit."""
    server(_status(COMMIT))
    res = _run(_app(tmp_path, {"c.js": _chunk_fwd("local")}), server.url)
    assert res.returncode == 1
    assert _stage(res) == "seerr-commit-tag-mismatch"
    assert "client-baked=local" in res.stderr
    assert "server-status=" + COMMIT in res.stderr


def test_commit_bundle_vs_local_server_mismatches(tmp_path, server):
    server(_status("local"))
    res = _run(_app(tmp_path, {"c.js": _chunk_fwd(COMMIT)}), server.url)
    assert res.returncode == 1
    assert _stage(res) == "seerr-commit-tag-mismatch"


def test_commit_bundle_vs_other_commit_server_mismatches(tmp_path, server):
    """Artifact assembled from two builds: both real commits, different."""
    server(_status(OTHER))
    res = _run(_app(tmp_path, {"c.js": _chunk_fwd(COMMIT)}), server.url)
    assert res.returncode == 1
    assert _stage(res) == "seerr-commit-tag-mismatch"


# ---------------------------------------------------------------------------
# Could not assert -> exit 2, never green
# ---------------------------------------------------------------------------


def test_missing_static_dir_is_bundle_unreadable(tmp_path, server):
    server(_status("local"))
    res = _run(tmp_path / "nope", server.url)
    assert res.returncode == 2
    assert _stage(res) == "seerr-commit-tag-bundle-unreadable"


def test_no_comparison_in_bundle_is_bundle_unreadable(tmp_path, server):
    """An `===` (e.g. an About page showing "development build") is not the
    StatusChecker comparison; with nothing else the canary must not guess."""
    server(_status("local"))
    app = _app(tmp_path, {"c.js": 'x="local"===e.commitTag?"dev":e.version'})
    res = _run(app, server.url)
    assert res.returncode == 2
    assert _stage(res) == "seerr-commit-tag-bundle-unreadable"


def test_two_different_commits_in_bundle_is_ambiguous(tmp_path, server):
    server(_status(COMMIT))
    app = _app(tmp_path, {"a.js": _chunk_fwd(COMMIT), "b.js": _chunk_fwd(OTHER)})
    res = _run(app, server.url)
    assert res.returncode == 2
    assert _stage(res) == "seerr-commit-tag-bundle-ambiguous"


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.parametrize("case", [
    "missing-port-file", "garbage-port-file", "refused", "http-500",
    "not-json", "no-commitTag", "non-string-commitTag", "insane-commitTag",
])
def test_unreadable_status_is_exit_2(tmp_path, server, case):
    app = _app(tmp_path, {"c.js": _chunk_fwd("local")})
    env, url = {}, server.url
    if case == "missing-port-file":
        env["MANITOBA_SECRETS"] = str(tmp_path / "no-secrets")
        url = None
    elif case == "garbage-port-file":
        sec = tmp_path / "secrets"
        sec.mkdir()
        (sec / "seerr.port").write_text("seerr\n", encoding="utf-8")
        env["MANITOBA_SECRETS"] = str(sec)
        url = None
    elif case == "refused":
        url = "http://127.0.0.1:%d/api/v1/status" % _closed_port()
    elif case == "http-500":
        server(_status("local"), status=500)
    elif case == "not-json":
        server(raw=b"<html>Bad Gateway</html>")
    elif case == "no-commitTag":
        server({"version": "3.5.0"})
    elif case == "non-string-commitTag":
        server({"version": "3.5.0", "commitTag": None})
    elif case == "insane-commitTag":
        server(_status("local; rm -rf ~"))
    res = _run(app, url, env)
    assert res.returncode == 2, (case, res.stdout, res.stderr)
    assert _stage(res) == "seerr-commit-tag-status-unreadable"
    assert "PASS" not in res.stdout
