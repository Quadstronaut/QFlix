"""Guards the DEPLOY-PATH fix for the 2026-07-29 dead-shell incident.

WHY THIS FILE EXISTS
--------------------
The 2026-07-29 change shipped two halves: a new `dash-asset-integrity` canary
(guarded by tests/unit/test_dash_asset_integrity.py) and a fix to the deploy
path that CAUSED the incident. The canary half was mutation-verified to death.
The fix half had ZERO test coverage, which was proven by mutation on
2026-07-29: reverting

    systemctl --user enable qflix-dash.service && systemctl --user restart ...
                                  -> systemctl --user enable --now qflix-dash.service

i.e. re-introducing the literal root cause, left the entire 1209-test suite
GREEN. So did deleting the installer's asset sweep, and so did neutering the
smoke test's new predicates back to a bare marker count.

That is the same defect class the canary exists to prevent, pointed at the fix
instead of the guard: the repo READS as though the deploy path is fixed, and
nothing would notice if it stopped being. RULE 3's "a guard that is committed
but not scheduled is worse than no guard" applies verbatim to "a fix that is
committed but not pinned".

WHAT IS COVERED, AND HOW STRONGLY
---------------------------------
Two tiers, deliberately:

  STRUCTURAL (fast, always runs) - pins the three textual properties whose
  removal the mutation run proved is invisible: `restart` not `enable --now`,
  the installer verify asserting the asset invariant, and the smoke landing-page
  gate consuming its new predicates. Structural assertions cannot prove the
  code WORKS, only that it has not been deleted. They are pinned anyway because
  deletion is exactly what happened in the mutation run.

  BEHAVIOURAL (spawns bash + curl + a loopback fixture) - EXTRACTS the
  installer's remote verify heredoc verbatim and RUNS it against a fake $HOME
  and a real HTTP server, including a server that reproduces the incident
  signature (a served shell referencing an asset the server 404s while the file
  sits on disk). This is what proves the verify would actually have failed the
  2026-07-29 deploy instead of reporting success.

The behavioural tier deliberately does NOT exercise the /healthz readiness
branch: that path spends a 60-second budget by design, and paying a minute of
suite time to observe a `sleep` is a bad trade. Its presence is pinned
structurally instead, and the comment says so rather than implying coverage
that is not there.
"""
from __future__ import annotations

import http.server
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASH_INSTALL = REPO_ROOT / "scripts" / "configure" / "90-qflix-dash-install.sh"
SMOKE = REPO_ROOT / "scripts" / "smoke-test.sh"

BASH = shutil.which("bash")
CURL = shutil.which("curl")

needs_shell = pytest.mark.skipif(
    not BASH or not CURL,
    reason="behavioural tier needs a real bash and curl on PATH",
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Extracting the remote verify block
# ---------------------------------------------------------------------------


def extract_verify_block() -> str:
    """Return the body of the installer's LAST `<<'REMOTE' ... REMOTE` heredoc.

    Located by CONTENT (the block that runs the asset sweep) rather than by
    line number or ordinal, so re-ordering the installer's steps does not
    silently make this test exercise the wrong heredoc - it fails loudly
    instead.
    """
    src = _read(DASH_INSTALL)
    blocks = re.findall(r"<<'REMOTE'\n(.*?)\nREMOTE\n", src, re.S)
    hits = [b for b in blocks if "_app/immutable" in b]
    assert len(hits) == 1, (
        "expected exactly one remote heredoc asserting the asset invariant in "
        "%s, found %d - the installer was restructured and this test is no "
        "longer exercising the verify step" % (DASH_INSTALL.name, len(hits))
    )
    return hits[0]


# ---------------------------------------------------------------------------
# STRUCTURAL TIER
# ---------------------------------------------------------------------------


def test_the_deploy_restarts_the_unit_it_just_shipped_a_build_over():
    """THE ROOT CAUSE. `enable --now` starts a STOPPED unit and is a no-op on a
    running one, so the installer scp'd a fresh build/ under a live node process
    and left it there. adapter-node's sirv snapshots its static-file manifest
    once at process start, so the new assets were invisible to the process that
    was serving the shell referencing them.

    Reverting this single word is the whole incident, and before this test the
    full suite stayed green when it was reverted.
    """
    src = _read(DASH_INSTALL)
    assert "enable --now qflix-dash.service" not in src, (
        "90-qflix-dash-install.sh is back to `enable --now qflix-dash.service`. "
        "That STARTS a stopped unit but does NOT restart a running one, so a "
        "re-run ships a new build/ under the old process and the dashboard "
        "serves a shell whose assets it will 404. This is the literal root "
        "cause of the 2026-07-29 22-hour dead-shell outage."
    )
    assert re.search(r"systemctl --user restart qflix-dash\.service", src), (
        "the deploy no longer restarts qflix-dash.service. `restart` also "
        "starts a stopped unit, so it is a strict superset of `--now` and "
        "equally idempotent - there is no reason to weaken it."
    )


def test_the_deploy_verify_asserts_the_asset_invariant_not_just_healthz():
    """/healthz and /api/status are answered happily by a STALE process - that
    is precisely how the 2026-07-29 deploy reported success over a dead shell.
    The verify must assert what the old one could not: that every asset the
    served shell references is servable by the process that served it."""
    block = extract_verify_block()
    assert "/_app/immutable" in block, (
        "the deploy verify no longer extracts /_app/immutable references from "
        "the served document - it is back to asking the stale process how it "
        "feels"
    )
    assert re.search(r"curl[^\n]*\$BASE\$ref", block), (
        "the verify extracts asset references but never fetches them; "
        "extracting without probing asserts nothing"
    )
    assert re.search(r"Content-Length", block, re.I), (
        "the verify dropped the Content-Length predicate - the "
        "ERR_CONTENT_LENGTH_MISMATCH leg of the incident. It is NOT implied by "
        "the 404 sweep: the prerendered document is always named index.html, so "
        "it never changes path and can only ever fail this way."
    )


def test_the_deploy_verify_distinguishes_stale_manifest_from_partial_deploy():
    """A 404 whose file EXISTS on disk is a stale in-process sirv manifest
    (fixable by a restart). A 404 whose file is ABSENT is a partial deploy (a
    restart cannot help). Collapsing them would send the operator to the wrong
    repair, which is the same mistake as not detecting the fault at all."""
    block = extract_verify_block()
    assert re.search(r"\[ -f \"\$BUILD\$ref\" \]", block), (
        "the verify no longer checks whether a 404'd asset exists under "
        "~/.apps/qflix-dash/build/client - it cannot tell a stale sirv manifest "
        "from a broken deploy"
    )
    assert "EXISTS on disk" in block and "ABSENT on disk" in block, (
        "the two 404 causes no longer carry distinct operator-facing diagnoses"
    )


def test_the_deploy_verify_still_gates_on_healthz_readiness():
    """Pinned STRUCTURALLY only. The readiness poll exists because
    qflix-dash.service is Type=exec, so `restart` returns at execve and not at
    listen() - a single-shot curl FATALs a perfectly good deploy. Exercising it
    behaviourally means paying its 60-second budget, which is not worth a minute
    of suite time; this asserts the poll has not been deleted, nothing more."""
    block = extract_verify_block()
    assert re.search(r"for .* in \$\(seq 1 \d+\); do", block), (
        "the /healthz readiness poll was replaced by a single-shot probe - a "
        "Type=exec unit is 'active' at execve, so this FATALs good deploys"
    )
    assert re.search(r'\[ "\$HZ" = "200" \] \|\| \{', block), (
        "/healthz is no longer a hard gate; it is the path the pusher's app "
        "probe uses, so an install that leaves it non-200 leaves the app "
        "monitor lying"
    )


@pytest.mark.parametrize("path", ["scripts/configure/90-qflix-dash-install.sh",
                                  "scripts/smoke-test.sh"])
def test_http_code_capture_cannot_concatenate_a_fallback(path):
    """`CODE=$(curl ... -w '%{http_code}' || echo 000)` CONCATENATES on a
    mid-transfer failure: curl prints the status it DID receive and the fallback
    appends, so a truncated 200 becomes "200000". Observed 2026-07-29 - the
    installer aborted with "HTTP 200000", the wrong diagnosis, and it aborted
    BEFORE the Content-Length predicate that exists to name that exact fault."""
    src = _read(REPO_ROOT / path)
    offenders = [
        line.strip() for line in src.split("\n")
        # Comment lines are excluded on purpose: both files DOCUMENT the trap
        # in prose, and the prose must not be what this test is reading.
        if not line.lstrip().startswith("#")
        and "%{http_code}" in line and re.search(r"\|\|\s*echo\s+0", line)
    ]
    assert not offenders, (
        "%s captures %%{http_code} with a `|| echo 000` fallback, which "
        "concatenates rather than replaces on a partial transfer: %s"
        % (path, offenders)
    )


def test_the_smoke_landing_page_gate_consumes_its_new_predicates():
    """Computing a predicate and not gating on it is indistinguishable from not
    computing it. The mutation run neutered exactly this line and the suite
    stayed green."""
    src = _read(SMOKE)
    m = re.search(r"echo \"7\. Landing page\"(.*?)\n# 8\.", src, re.S)
    assert m, "the smoke test's landing-page section could not be located"
    section = m.group(1)

    assert "_app/immutable" in section, (
        "the smoke landing-page check is back to a marker-only grep - the blind "
        "check that let the dead shell run for 22 hours"
    )
    gate = re.search(r"\nif \[ \"\$\{ROOT_CODE\}\".*?; then", section, re.S)
    assert gate, "the landing-page pass/fail condition could not be located"
    cond = gate.group(0)
    for var in ("LP_BAD", "LP_CL_OK", "LP_N"):
        assert var in cond, (
            "the landing-page gate computes %s but does not gate on it, so a "
            "dead shell still records `pass`" % var
        )


def test_the_smoke_failure_detail_names_the_repair():
    """The incident's cost was diagnosis time, not detection time. The fail
    string carries the exact command that fixes the restartable case."""
    src = _read(SMOKE)
    assert "systemctl --user restart qflix-dash" in src, (
        "the landing-page failure detail no longer names the repair command"
    )


# ---------------------------------------------------------------------------
# BEHAVIOURAL TIER
# ---------------------------------------------------------------------------

MARKER = "data-qflix-dash"
REFS = [
    "/_app/immutable/entry/app.CGGeqWLt.js",
    "/_app/immutable/nodes/2.BZsEM563.js",
]


def _shell(html_refs=REFS):
    body = "<html><body data-qflix-dash>"
    for r in html_refs:
        body += '<link rel="modulepreload" href="%s">' % r
    return (body + "</body></html>").encode()


class _Fixture(http.server.BaseHTTPRequestHandler):
    """Serves a dashboard shell. Class attributes are set per-test."""

    protocol_version = "HTTP/1.0"  # close-delimited: an over-declared
    # Content-Length becomes a real short read on the client, which is the
    # only way to reproduce the ERR_CONTENT_LENGTH_MISMATCH leg honestly.

    html = _shell()
    missing = ()          # url paths that 404
    overdeclare = 0       # extra bytes to add to the document Content-Length

    def log_message(self, *a):  # silence the default stderr spam
        pass

    def _send(self, code, body, declared=None):
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length",
                         str(len(body) if declared is None else declared))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self._send(200, b"ok")
        if path == "/api/status":
            return self._send(200, b"{}")
        if path in self.missing:
            return self._send(404, b"not found")
        if path == "/":
            declared = len(self.html) + self.overdeclare or None
            return self._send(200, self.html,
                              declared if self.overdeclare else None)
        return self._send(200, b"//js\n")


@pytest.fixture
def server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Fixture)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    yield srv
    srv.shutdown()
    srv.server_close()
    # Reset class state so one test cannot leak into the next.
    _Fixture.html = _shell()
    _Fixture.missing = ()
    _Fixture.overdeclare = 0


def _run_verify(tmp_path, port, on_disk=REFS):
    """Run the EXTRACTED installer verify heredoc against a fake $HOME."""
    home = tmp_path / "home"
    (home / "secrets").mkdir(parents=True)
    (home / "secrets" / "qflix-dash.port").write_text(str(port), encoding="utf-8")
    build = home / ".apps" / "qflix-dash" / "build" / "client"
    for ref in on_disk:
        f = build / ref.lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("//js\n", encoding="utf-8")

    script = tmp_path / "verify.sh"
    script.write_text(extract_verify_block() + "\n", encoding="utf-8",
                      newline="\n")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("MSYS2_ARG_CONV_EXCL", None)
    return subprocess.run([BASH, str(script)], env=env, timeout=180,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@needs_shell
def test_verify_passes_a_healthy_deploy(tmp_path, server):
    r = _run_verify(tmp_path, server.server_port)
    out = (r.stdout + r.stderr).decode()
    assert r.returncode == 0, "healthy deploy rejected:\n" + out
    assert "assets=2/2 resolve 200" in out, out


@needs_shell
def test_verify_FAILS_the_incident_signature_404_while_present_on_disk(
        tmp_path, server):
    """THE 2026-07-29 DEPLOY. The shell renders, the marker is present,
    /healthz is 200 - and a referenced module 404s while the file sits on disk.
    The old verify reported success here."""
    _Fixture.missing = (REFS[0],)
    r = _run_verify(tmp_path, server.server_port, on_disk=REFS)
    out = (r.stdout + r.stderr).decode()

    # The un-fixed behaviour, reproduced: /healthz and /api/status are both
    # fine and the marker is in the body, so every pre-incident check is green.
    assert "healthz=ok" in out, out
    assert MARKER in _Fixture.html.decode()

    assert r.returncode != 0, "the verify PASSED the incident signature:\n" + out
    assert "EXISTS on disk" in out, out
    assert "stale in-process sirv manifest" in out, out


@needs_shell
def test_verify_reports_a_partial_deploy_differently(tmp_path, server):
    """Same 404, file genuinely absent. Must still fail, but must NOT tell the
    operator a restart will fix it."""
    _Fixture.missing = (REFS[0],)
    r = _run_verify(tmp_path, server.server_port, on_disk=REFS[1:])
    out = (r.stdout + r.stderr).decode()
    assert r.returncode != 0, out
    assert "ABSENT on disk" in out, out
    assert "stale in-process sirv manifest" not in out, (
        "a partial deploy was diagnosed as a stale manifest:\n" + out)


@needs_shell
def test_verify_FAILS_an_overdeclared_content_length(tmp_path, server):
    """The ERR_CONTENT_LENGTH_MISMATCH leg, with ZERO 404s - so a verify that
    only swept for 404s would pass this fixture."""
    _Fixture.overdeclare = 40
    r = _run_verify(tmp_path, server.server_port)
    out = (r.stdout + r.stderr).decode()
    assert _Fixture.missing == (), "this fixture must contain no 404s"
    assert r.returncode != 0, "an over-declared Content-Length passed:\n" + out
    assert "bytes delivered" in out, out


@needs_shell
def test_verify_FAILS_a_shell_with_no_asset_references(tmp_path, server):
    """A 200 that references zero modules is not a SvelteKit shell. Passing it
    would turn the verify into another check that proves nothing."""
    _Fixture.html = b"<html><body data-qflix-dash>nothing here</body></html>"
    r = _run_verify(tmp_path, server.server_port, on_disk=())
    out = (r.stdout + r.stderr).decode()
    assert r.returncode != 0, "a shell with no asset references passed:\n" + out
    assert "references 0 /_app/immutable" in out, out
