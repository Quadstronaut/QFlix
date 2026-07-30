"""Tests for scripts/canaries/dash-asset-integrity.sh.

There is NO shellcheck or shell-lint gate anywhere in this repo's CI
(.github/workflows/tests.yml runs pytest and nothing else), so a pytest test
that actually runs `bash <script>` is the ONLY real gate on this canary's
correctness. Everything below therefore drives the real artifact end to end:
real bash, real python3, real HTTP over loopback, real files on disk.

Five jobs:

  1. MUTATION VERIFICATION against the two blind checks that let the
     2026-07-29 incident run for 22 hours. Every failure fixture is
     constructed so that
       - `_marker_only_verdict()`  (what scripts/canaries/mobile-ux.sh:29 and
         scripts/smoke-test.sh:120 actually do -- grep the served HTML for
         "data-qflix-dash") returns GREEN, and
       - where relevant `_p1_only_verdict()` (a 404-sweep with no
         Content-Length predicate) also returns GREEN,
     while this canary returns RED. Those two helpers ARE the un-fixed
     behaviour; the assertions prove the new canary is strictly stronger
     rather than merely differently worded.

  2. THE CONTENT-ENCODING DECISION, proven rather than asserted. A fixture
     where the .br entry 404s while identity is fine is run twice: once with
     the shipped ENCODINGS=identity,br,gzip (RED) and once with
     ENCODINGS=identity (GREEN). The second run is the identity-only canary a
     bare curl would give you -- i.e. the smoke test's blind spot, reproduced
     and then closed.

  3. THE EXISTS-VS-ABSENT BRANCH, which decides whether a restart can possibly
     help: 404 + file present on disk -> restart; 404 + file absent -> broken
     deploy, alert and do NOT restart; build dir unreadable -> cannot
     arbitrate, do NOT restart. Each asserts on a restart-stub marker file, so
     "did not restart" is verified by absence of the side effect, not by
     reading the message.

  4. THE 24h BREAKER, including the two ways this exact pattern has already
     been gotten wrong in this repo (.claude/council-ledger.jsonl:122-123):
     a not-even-issued repair must NOT burn the 24h budget, and a heal must
     never be reported as successful without re-verification.

  5. THE PAUSE-WINDOW GUARD on the mutation only, with negative controls, plus
     a config-drift lock that pins the script's hour literals against
     manitoba-maint-window.timer / manitoba-maint-window-watchdog.timer -- the
     same technique as tests/unit/test_pause_window_chokepoint.py:102-117.

The fake dashboard is a loopback http.server driven by a route table. A lying
Content-Length can only be produced by a server that actually lies, so this is
not a place where mocking the transport would do: the whole predicate is about
what comes off the wire. Nothing egresses; the socket binds 127.0.0.1:0.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "canaries" / "dash-asset-integrity.sh"
WINDOW_TIMER = (REPO_ROOT / "scripts" / "maint" / "systemd"
                / "manitoba-maint-window.timer")
WATCHDOG_TIMER = (REPO_ROOT / "scripts" / "maint" / "systemd"
                  / "manitoba-maint-window-watchdog.timer")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="dash-asset-integrity.sh needs bash + python3 on PATH",
)

MARKER = "data-qflix-dash"

# The exact reference set the live dashboard served on 2026-07-29 (extracted
# read-only from the box). 6 of these 10 returned 404 while present on disk.
REAL_REFS = [
    "/_app/immutable/entry/start.D0YnI2ak.js",
    "/_app/immutable/entry/app.CGGeqWLt.js",
    "/_app/immutable/chunks/BDT80ewK.js",
    "/_app/immutable/chunks/DHu39jgv.js",
    "/_app/immutable/chunks/DYl5dUZ5.js",
    "/_app/immutable/chunks/xihTtKlq.js",
    "/_app/immutable/nodes/0.Bmegr9Z3.js",
    "/_app/immutable/nodes/2.BZsEM563.js",
    "/_app/immutable/assets/0.C1k00VOu.css",
    "/_app/immutable/assets/2.Cu3NURG7.css",
]
SMALL_REFS = REAL_REFS[:3]
ALL_ENC = ("identity", "br", "gzip")


# ---------------------------------------------------------------------------
# The two blind checks, implemented so they can be shown to pass
# ---------------------------------------------------------------------------


def _marker_only_verdict(html: str) -> bool:
    """What mobile-ux.sh:29 / smoke-test.sh:120 actually assert. GREEN means
    'this check would have said the dashboard was fine'."""
    return MARKER in html


def _p1_only_verdict(plan: dict, refs: list) -> bool:
    """A 404-sweep with no Content-Length predicate: every referenced URL
    resolves 200 in every encoding. GREEN means 'a canary that only looked for
    404s would have said the dashboard was fine'."""
    for ref in refs:
        per = plan.get(ref)
        if per is None:
            return False
        for enc in ALL_ENC:
            r = per.get(enc) or per.get("*")
            if r is None or r["status"] != 200:
                return False
    return True


# ---------------------------------------------------------------------------
# Fake dashboard
# ---------------------------------------------------------------------------


def _resp(status=200, body=b"", declared=None, stall_s=0.0,
          honest_after_first=False):
    """declared=None means 'tell the truth about Content-Length'. A non-None
    value larger than len(body) is the sirv stale-stat symptom: the server
    advertises a byte count it does not deliver, which is what a browser
    reports as net::ERR_CONTENT_LENGTH_MISMATCH.

    stall_s > 0: send the headers and a few bytes, then sleep. The client hits
    its socket timeout mid-body, which is the "200 whose body never arrives"
    fault - a wedged worker, and a distinct symptom from an over-declared
    length because `received` never becomes a number at all.

    honest_after_first: lie on the FIRST request for this (path, encoding) and
    tell the truth on every later one. That is what a single transient
    mid-transfer drop looks like, as opposed to a deterministic stale sirv stat
    tuple, which lies identically forever. The canary must distinguish them:
    only the deterministic one may trigger an unattended restart.
    """
    return {"status": status, "body": body, "declared": declared,
            "stall_s": stall_s, "honest_after_first": honest_after_first}


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 (the BaseHTTPRequestHandler default) closes the connection after
    # every response. That is load-bearing: it is what turns an over-declared
    # Content-Length into an http.client.IncompleteRead on the client instead
    # of a hang waiting for bytes that never come.
    protocol_version = "HTTP/1.0"

    def do_GET(self):  # noqa: N802 - stdlib naming
        fake = self.server.fake
        enc = self.headers.get("Accept-Encoding", "identity")
        path = self.path.split("?", 1)[0]
        fake.hits.append((path, enc))
        nth = fake.bump(path, enc)
        spec = fake.resolve(path, enc)
        body = spec["body"]
        declared = spec["declared"] if spec["declared"] is not None else len(body)
        if spec.get("honest_after_first") and nth > 1:
            declared = len(body)
        self.send_response(spec["status"])
        self.send_header("Content-Type", "text/html;charset=utf-8")
        if enc != "identity" and spec["status"] == 200:
            self.send_header("Content-Encoding", enc)
        self.send_header("Content-Length", str(declared))
        self.end_headers()
        try:
            if spec.get("stall_s"):
                # Headers + a token payload, then stop. The client's read()
                # raises a socket timeout with `received` still unset.
                self.wfile.write(body[:8] or b"0123")
                self.wfile.flush()
                time.sleep(spec["stall_s"])
                return
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *args):
        pass


class FakeDash:
    """Route table -> HTTP. `fix_flag` lets an out-of-process restart stub flip
    the server from broken to healthy, which is how the post-repair
    re-verification path is exercised for real."""

    def __init__(self, table, fixed_table=None, fix_flag=None):
        self.table = table
        self.fixed_table = fixed_table
        self.fix_flag = fix_flag
        self.hits = []
        self.counts = {}
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.fake = self
        self.port = self.httpd.socket.getsockname()[1]
        self.base = "http://127.0.0.1:%d/" % self.port
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def bump(self, path, enc):
        """1-based request ordinal for this (path, encoding). Drives
        honest_after_first, i.e. transient-vs-deterministic."""
        with self._lock:
            k = (path, enc)
            self.counts[k] = self.counts.get(k, 0) + 1
            return self.counts[k]

    def resolve(self, path, enc):
        table = self.table
        if self.fixed_table is not None and self.fix_flag is not None:
            try:
                if self.fix_flag.exists():
                    table = self.fixed_table
            except OSError:
                pass
        per = table.get(path)
        if per is None:
            return _resp(404, b"not found")
        return per.get(enc) or per.get("*") or _resp(404, b"not found")

    def stop(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass


def _shell_html(refs):
    """A realistic SvelteKit app shell: carries the data-qflix-dash marker on
    <body> (so the marker-only check passes) and references every asset the
    way adapter-node actually emits them."""
    head = ['<!doctype html><html lang="en"><head><meta charset="utf-8">']
    for r in refs:
        if r.endswith(".css"):
            head.append('<link rel="stylesheet" href="%s">' % r)
        else:
            head.append('<link rel="modulepreload" href="%s">' % r)
    head.append("<script type=\"module\">")
    for r in refs:
        if not r.endswith(".css"):
            head.append('import("%s");' % r)
    head.append("</script></head>")
    head.append('<body %s><div id="app">QFlix</div></body></html>' % MARKER)
    return "".join(head)


def _base_plan(refs, html):
    """Everything healthy in all three encodings with honest lengths.

    The compressed variants are just distinct shorter byte strings, not real
    brotli/gzip. That is deliberate and sufficient: the canary never
    decompresses anything (it sends an explicit Accept-Encoding and compares
    the advertised Content-Length against the delivered wire bytes), so real
    compression would add a dependency and test nothing extra.
    """
    plan = {}
    hb = html.encode("utf-8")
    plan["/"] = {
        "identity": _resp(200, hb),
        "br": _resp(200, b"BR" + hb[:48]),
        "gzip": _resp(200, b"GZ" + hb[:72]),
    }
    for r in refs:
        payload = ("// " + r + "\n" + "x" * 64).encode("utf-8")
        plan[r] = {
            "identity": _resp(200, payload),
            "br": _resp(200, b"BR" + payload[:24]),
            "gzip": _resp(200, b"GZ" + payload[:36]),
        }
    return plan


@pytest.fixture
def dash(request):
    made = []

    def _make(table, fixed_table=None, fix_flag=None):
        s = FakeDash(table, fixed_table=fixed_table, fix_flag=fix_flag)
        made.append(s)
        return s

    yield _make
    for s in made:
        s.stop()


# ---------------------------------------------------------------------------
# Build tree + restart stub
# ---------------------------------------------------------------------------


def _build_tree(tmp_path, refs, omit=()):
    """A real on-disk build tree. `omit` reproduces a broken/partial deploy:
    the HTML references a file that is genuinely not there."""
    build = tmp_path / "build"
    (build / "prerendered").mkdir(parents=True, exist_ok=True)
    (build / "prerendered" / "index.html").write_text("<html></html>",
                                                      encoding="utf-8")
    for r in refs:
        if r in omit:
            continue
        p = build / "client" / r.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("// " + r + "\n", encoding="utf-8")
        # sirv indexes the precompressed siblings as separate manifest entries;
        # ship them so the fixture matches the real deployment shape.
        for ext in (".br", ".gz"):
            sib = Path(str(p) + ext)
            sib.write_bytes(b"COMPRESSED")
    return build


def _restart_stub(tmp_path, name="restart-stub", rc=0, marker=None,
                  fix_flag=None):
    """A stand-in for `systemctl --user restart qflix-dash`. Records that it
    ran (so 'did not restart' is verifiable by absence) and can optionally flip
    the fake server healthy, which is how a REAL heal is simulated."""
    p = tmp_path / (name + ".sh")
    lines = ["#!/usr/bin/env bash"]
    if marker is not None:
        lines.append('printf "restart %%s\\n" "$*" >> "%s"' % marker.as_posix())
    if fix_flag is not None:
        lines.append(': > "%s"' % fix_flag.as_posix())
    lines.append("exit %d" % rc)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return "bash " + p.as_posix()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _mkenv(tmp_path, srv=None, build=None, restart_cmd=None, **extra):
    """Hermetic env. Every knob that would otherwise reach the wall clock, the
    real box, or a real sleep is pinned. SELF_HEAL is deliberately NOT set, so
    tests exercise the PRODUCTION default (armed) and the restart is contained
    by pointing RESTART_CMD at a stub."""
    env = {
        "QFLIX_CANARY_DASH_STATE_DIR": str(tmp_path / "state"),
        "QFLIX_CANARY_DASH_BUILD_DIR": str(build if build is not None
                                           else tmp_path / "no-such-build"),
        "QFLIX_CANARY_DASH_RETRY_SLEEP_S": "0",
        "QFLIX_CANARY_DASH_TIMEOUT_S": "5",
        "QFLIX_CANARY_DASH_VERIFY_DELAY_S": "0",
        "QFLIX_CANARY_DASH_VERIFY_DEADLINE_S": "0",
        "QFLIX_CANARY_DASH_VERIFY_POLL_S": "0",
        "QFLIX_CANARY_DASH_RESTART_TIMEOUT_S": "20",
        # Force "outside the maintenance window" so the suite cannot go flaky
        # by happening to run on a Monday between 11:00 and 15:00 UTC.
        "QFLIX_CANARY_DASH_FORCE_WINDOW": "0",
        # Past the cold-start guard unless a test overrides it.
        "QFLIX_CANARY_DASH_UPTIME_S": "99999",
        # Leg 2 of the window check imports lib.suppression from here. Pointed
        # at a directory that does not exist so the import fails and the leg is
        # deterministically unavailable -- one test points it at the real repo
        # to prove the canonical predicate is genuinely consulted. (An EMPTY
        # value would not work: the bash preamble uses ${VAR:-default}, so empty
        # resolves back to the in-repo path.)
        "QFLIX_CANARY_DASH_MAINT_LIB": str(tmp_path / "no-such-maint-lib"),
    }
    if srv is not None:
        env["QFLIX_CANARY_DASH_URL"] = srv.base
    env["QFLIX_CANARY_DASH_RESTART_CMD"] = (
        restart_cmd if restart_cmd is not None
        else _restart_stub(tmp_path, "noop-stub"))
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return env


def _run(args=(), env=None, timeout=120):
    full = dict(os.environ)
    # Git Bash on Windows rewrites env values that look like POSIX paths when
    # it spawns a native python.exe, so "/_app/immutable/x.js" would arrive as
    # "C:/.../git/_app/immutable/x.js" and the __disk__ hook would always say
    # absent. Exclude only that one variable by name (excluding "*" would also
    # stop PATH being translated, and python then cannot find bash). This is a
    # Windows-workstation test-harness artifact only: on the seedbox and on the
    # CI ubuntu runner nothing is rewritten, and in production the URL paths are
    # extracted inside python from HTTP bytes and never pass through argv or
    # the environment at all. The variable is harmless on POSIX.
    full["MSYS2_ENV_CONV_EXCL"] = "QFLIX_CANARY_DASH_SELFTEST_ARG"
    if env:
        full.update({k: str(v) for k, v in env.items()})
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=full, capture_output=True, text=True, timeout=timeout,
    )


def _stage(result):
    m = re.search(r"STAGE=([a-z0-9-]+)", result.stderr or "")
    return m.group(1) if m else None


def _read_log(tmp_path):
    d = tmp_path / "state"
    if not d.is_dir():
        return ""
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(d.glob("dash-asset-integrity-*.log")))


def _read_events(tmp_path):
    d = tmp_path / "state" / "events"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


# ===========================================================================
# 1. HTML extraction (self-test hook __refs__)
# ===========================================================================


def test_refs_extracted_from_served_html_deduped_in_order(tmp_path):
    html = _shell_html(REAL_REFS)
    f = tmp_path / "shell.html"
    f.write_text(html, encoding="utf-8")
    r = _run(["__refs__", str(f)])
    assert r.returncode == 0, r.stderr
    got = [ln for ln in r.stdout.splitlines() if ln.strip()]
    # Every ref appears twice in the shell (modulepreload + import) but must be
    # probed once.
    assert got == REAL_REFS, got


def test_refs_extraction_stops_at_the_quote_and_ignores_prose(tmp_path):
    f = tmp_path / "shell.html"
    f.write_text(
        '<link href="/_app/immutable/entry/app.AAA.js">'
        "<p>see /_app/immutable/nodes/1.BBB.js.</p>"
        "<script>x=1</script>", encoding="utf-8")
    r = _run(["__refs__", str(f)])
    assert r.returncode == 0, r.stderr
    got = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert got == ["/_app/immutable/entry/app.AAA.js",
                   "/_app/immutable/nodes/1.BBB.js"], got


def test_refs_extraction_returns_nothing_for_a_shell_with_no_assets(tmp_path):
    f = tmp_path / "shell.html"
    f.write_text("<html><body %s>nothing</body></html>" % MARKER,
                 encoding="utf-8")
    r = _run(["__refs__", str(f)])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


# ===========================================================================
# 2. On-disk resolver (self-test hook __disk__)
# ===========================================================================


def test_disk_resolver_finds_an_asset_under_build_client(tmp_path):
    build = _build_tree(tmp_path, SMALL_REFS)
    r = _run(["__disk__", SMALL_REFS[0]],
             env={"QFLIX_CANARY_DASH_BUILD_DIR": str(build)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "present"


def test_disk_resolver_reports_absent_for_a_file_that_is_not_there(tmp_path):
    build = _build_tree(tmp_path, SMALL_REFS)
    r = _run(["__disk__", "/_app/immutable/entry/app.GONE0000.js"],
             env={"QFLIX_CANARY_DASH_BUILD_DIR": str(build)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "absent"


def test_disk_resolver_maps_root_to_the_prerendered_document(tmp_path):
    build = _build_tree(tmp_path, SMALL_REFS)
    r = _run(["__disk__", "/"],
             env={"QFLIX_CANARY_DASH_BUILD_DIR": str(build)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "present"


def test_disk_resolver_refuses_path_traversal(tmp_path):
    build = _build_tree(tmp_path, SMALL_REFS)
    r = _run(["__disk__", "/_app/immutable/../../../../../../etc/passwd"],
             env={"QFLIX_CANARY_DASH_BUILD_DIR": str(build)})
    assert r.returncode == 0, r.stderr
    # Refusing to resolve routes to "absent", i.e. alert-do-not-restart, which
    # is the safe direction.
    assert r.stdout.strip() == "absent"


# ===========================================================================
# 3. Healthy baseline
# ===========================================================================


def test_healthy_dashboard_passes(tmp_path, dash):
    html = _shell_html(SMALL_REFS)
    srv = dash(_base_plan(SMALL_REFS, html))
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS)))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.startswith("PASS: dash-asset-integrity"), r.stdout
    assert "all-200-and-length-consistent" in r.stdout
    # 1 root + 3 refs, each in 3 encodings.
    assert "probes=12" in r.stdout, r.stdout
    assert "refs=3" in r.stdout


def test_pass_emits_exactly_one_stdout_line_and_nothing_on_stderr(tmp_path, dash):
    html = _shell_html(SMALL_REFS)
    srv = dash(_base_plan(SMALL_REFS, html))
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS)))
    assert r.returncode == 0
    assert len([ln for ln in r.stdout.splitlines() if ln.strip()]) == 1
    assert r.stderr.strip() == "", r.stderr


# ===========================================================================
# 4. THE INCIDENT, and the mutation proof against the marker-only check
# ===========================================================================


def test_reproduces_the_2026_07_29_incident_that_every_monitor_missed(tmp_path, dash):
    """6 of the 10 referenced modules 404 while present on disk at mode 644 --
    the verified live signature. The served shell still carries
    data-qflix-dash, so the checks that were in place stay GREEN. This canary
    must go RED."""
    html = _shell_html(REAL_REFS)
    plan = _base_plan(REAL_REFS, html)
    stale = REAL_REFS[:6]
    for ref in stale:
        plan[ref] = {"*": _resp(404, b"Not Found")}

    # --- the un-fixed behaviour, shown to be green on this very fixture -----
    assert _marker_only_verdict(html) is True, (
        "fixture must be indistinguishable from healthy to the marker check "
        "(scripts/canaries/mobile-ux.sh:29, scripts/smoke-test.sh:120)")

    marker = tmp_path / "restarted"
    srv = dash(plan)
    r = _run(env=_mkenv(
        tmp_path, srv,
        build=_build_tree(tmp_path, REAL_REFS),
        restart_cmd=_restart_stub(tmp_path, "s", rc=0, marker=marker)))

    assert r.returncode != 0, r.stdout
    # Files are all present on disk, so this is the repairable signature and
    # the heal runs; the 404 detail is still carried in the message.
    assert _stage(r) in ("dash-healed", "dash-heal-unverified"), r.stderr
    assert "404=18" in r.stderr, r.stderr  # 6 refs x 3 encodings
    assert marker.exists(), "repairable signature must have issued a restart"


def test_a_single_stale_asset_is_enough_to_fail(tmp_path, dash):
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    plan[SMALL_REFS[1]] = {"*": _resp(404, b"")}
    srv = dash(plan)
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                        QFLIX_CANARY_DASH_SELF_HEAL="0"))
    assert r.returncode != 0
    assert _stage(r) == "dash-heal-disarmed"
    assert "404=3" in r.stderr


def test_fail_emits_exactly_one_stage_line_on_stderr_and_nothing_on_stdout(
        tmp_path, dash):
    """cli.py takes stderr verbatim as the Kuma msg= and truncates at 200
    chars (scripts/maint/lib/cli.py:605-610), so the contract is one line of
    exactly two whitespace-separated tokens."""
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    plan[SMALL_REFS[0]] = {"*": _resp(404, b"")}
    srv = dash(plan)
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                        QFLIX_CANARY_DASH_SELF_HEAL="0"))
    assert r.returncode != 0
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    tokens = lines[0].split()
    assert len(tokens) == 2, tokens
    assert tokens[0].startswith("STAGE=")
    assert tokens[1].startswith("msg=")
    assert r.stdout.strip() == "", r.stdout


# ===========================================================================
# 5. THE CONTENT-ENCODING DECISION, proven both ways
# ===========================================================================


def test_a_stale_br_entry_fails_even_though_identity_is_fine(tmp_path, dash):
    """The .br sibling is a SEPARATE sirv manifest entry. Every browser sends
    "gzip, deflate, br" and gets it; a bare curl sends nothing and gets
    identity. So a stale .br entry breaks 100% of real traffic while an
    identity-only probe stays green."""
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    for ref in SMALL_REFS:
        plan[ref]["br"] = _resp(404, b"")
    srv = dash(plan)

    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                        QFLIX_CANARY_DASH_SELF_HEAL="0"))
    assert r.returncode != 0, r.stdout
    assert _stage(r) == "dash-heal-disarmed"
    assert "404=3" in r.stderr
    assert "/br" in r.stderr, r.stderr


def test_identity_only_probing_is_the_blind_spot_this_canary_closes(tmp_path, dash):
    """MUTATION PROOF for the encoding choice: the SAME fixture as the test
    above, probed the way a bare curl probes, PASSES. That is exactly how the
    smoke test could stay green while browsers were broken. Delete br/gzip from
    the shipped ENCODINGS default and this is what you get back."""
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    for ref in SMALL_REFS:
        plan[ref]["br"] = _resp(404, b"")
    srv = dash(plan)
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                        QFLIX_CANARY_DASH_ENCODINGS="identity"))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.startswith("PASS:")
    assert "probes=4" in r.stdout


def test_a_stale_gzip_entry_also_fails(tmp_path, dash):
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    plan[SMALL_REFS[0]]["gzip"] = _resp(404, b"")
    srv = dash(plan)
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                        QFLIX_CANARY_DASH_SELF_HEAL="0"))
    assert r.returncode != 0
    assert "/gzip" in r.stderr, r.stderr


# ===========================================================================
# 6. PREDICATE P2 -- declared vs delivered, and why it is not implied by P1
# ===========================================================================


def test_root_document_lying_about_content_length_fails_with_zero_404s(
        tmp_path, dash):
    """The second symptom, isolated. The prerendered root document is named
    index.html and NEVER changes name across builds, so a rewrite in place
    leaves it 200 with a stale precomputed Content-Length. Nothing 404s. A
    404-only canary is green; this one must not be."""
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    body = plan["/"]["br"]["body"]
    plan["/"]["br"] = _resp(200, body, declared=len(body) + 4096)

    assert _marker_only_verdict(html) is True
    assert _p1_only_verdict(plan, SMALL_REFS) is True, (
        "fixture must contain ZERO 404s so a 404-sweep-only canary passes")

    srv = dash(plan)
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                        QFLIX_CANARY_DASH_SELF_HEAL="0"))
    assert r.returncode != 0, r.stdout
    assert _stage(r) == "dash-heal-disarmed"
    assert "len-mismatch=1" in r.stderr, r.stderr
    assert "404=" not in r.stderr, r.stderr


def test_an_asset_lying_about_content_length_fails(tmp_path, dash):
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    ref = SMALL_REFS[0]
    body = plan[ref]["identity"]["body"]
    plan[ref]["identity"] = _resp(200, body, declared=len(body) + 999)
    srv = dash(plan)
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                        QFLIX_CANARY_DASH_SELF_HEAL="0"))
    assert r.returncode != 0
    assert "len-mismatch=1" in r.stderr, r.stderr
    assert "declared-%d" % (len(body) + 999) in r.stderr, r.stderr


def test_a_content_length_mismatch_is_a_repairable_signature(tmp_path, dash):
    """A 200 with a lying length can only come from a stale in-process stat
    cache; the file demonstrably exists because we just read bytes out of it.
    So the exists-on-disk precondition is satisfied by construction and the
    heal is allowed to run."""
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    body = plan["/"]["identity"]["body"]
    plan["/"]["identity"] = _resp(200, body, declared=len(body) + 512)
    marker = tmp_path / "restarted"
    srv = dash(plan)
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                        restart_cmd=_restart_stub(tmp_path, "s", marker=marker)))
    assert r.returncode != 0
    assert marker.exists(), "length mismatch must be treated as repairable"


# ===========================================================================
# 7. PREDICATE P3 -- a shell that references nothing cannot hydrate
# ===========================================================================


def test_zero_asset_references_is_a_failure_not_a_pass(tmp_path, dash):
    """Guards the extractor itself. If a future SvelteKit release moves the
    asset prefix, the failure mode of a naive implementation is silence -- the
    canary finds nothing to check and reports success. That is the exact
    failure class being fixed, so it must be impossible here."""
    html = "<html><body %s>server-rendered only</body></html>" % MARKER
    plan = {"/": {"identity": _resp(200, html.encode()),
                  "br": _resp(200, b"BR"), "gzip": _resp(200, b"GZ")}}
    assert _marker_only_verdict(html) is True
    srv = dash(plan)
    marker = tmp_path / "restarted"
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                        restart_cmd=_restart_stub(tmp_path, "s", marker=marker)))
    assert r.returncode != 0
    assert _stage(r) == "dash-no-asset-refs"
    assert not marker.exists(), "a template regression is not restartable"


# ===========================================================================
# 8. EXISTS-VS-ABSENT -- the load-bearing branch
# ===========================================================================


def test_404_with_the_file_present_on_disk_restarts(tmp_path, dash):
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    plan[SMALL_REFS[0]] = {"*": _resp(404, b"")}
    marker = tmp_path / "restarted"
    srv = dash(plan)
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                        restart_cmd=_restart_stub(tmp_path, "s", marker=marker)))
    assert r.returncode != 0
    assert marker.exists()
    assert _stage(r) in ("dash-healed", "dash-heal-unverified")


def test_404_with_the_file_absent_on_disk_alerts_and_does_not_restart(
        tmp_path, dash):
    """A broken/partial deploy. A restart cannot conjure files that are not
    there, so restarting would be pure churn that also masks the real fault.
    This is scripts/maint/flaresolverr-canary.py:12-18 applied here."""
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    gone = SMALL_REFS[0]
    plan[gone] = {"*": _resp(404, b"")}
    marker = tmp_path / "restarted"
    srv = dash(plan)
    r = _run(env=_mkenv(
        tmp_path, srv,
        build=_build_tree(tmp_path, SMALL_REFS, omit=(gone,)),
        restart_cmd=_restart_stub(tmp_path, "s", marker=marker)))
    assert r.returncode != 0
    assert _stage(r) == "dash-assets-missing-on-disk", r.stderr
    assert "BROKEN-DEPLOY-not-restartable" in r.stderr
    assert not marker.exists(), "must NOT restart when the files are gone"
    assert not (tmp_path / "state" / "heal-latch.epoch").exists()


def test_mixed_present_and_absent_404s_refuses_to_restart(tmp_path, dash):
    """The absent file dominates: a restart fixes only the present ones and
    would leave the operator with a half-healed deploy and a green monitor."""
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    plan[SMALL_REFS[0]] = {"*": _resp(404, b"")}   # present on disk
    plan[SMALL_REFS[1]] = {"*": _resp(404, b"")}   # absent on disk
    marker = tmp_path / "restarted"
    srv = dash(plan)
    r = _run(env=_mkenv(
        tmp_path, srv,
        build=_build_tree(tmp_path, SMALL_REFS, omit=(SMALL_REFS[1],)),
        restart_cmd=_restart_stub(tmp_path, "s", marker=marker)))
    assert r.returncode != 0
    assert _stage(r) == "dash-assets-missing-on-disk", r.stderr
    assert not marker.exists()


def test_404_with_an_unreadable_build_dir_alerts_and_does_not_restart(
        tmp_path, dash):
    """The exists-vs-absent question is unanswerable, so the honest action is
    to alert. Restarting on the strength of a question you could not answer is
    how a canary becomes a churn generator."""
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    plan[SMALL_REFS[0]] = {"*": _resp(404, b"")}
    marker = tmp_path / "restarted"
    srv = dash(plan)
    r = _run(env=_mkenv(tmp_path, srv, build=tmp_path / "gone-build",
                        restart_cmd=_restart_stub(tmp_path, "s", marker=marker)))
    assert r.returncode != 0
    assert _stage(r) == "dash-build-dir-missing", r.stderr
    assert not marker.exists()


def test_a_non_200_non_404_status_is_not_the_repairable_signature(tmp_path, dash):
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    plan[SMALL_REFS[0]] = {"*": _resp(503, b"upstream")}
    marker = tmp_path / "restarted"
    srv = dash(plan)
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                        restart_cmd=_restart_stub(tmp_path, "s", marker=marker)))
    assert r.returncode != 0
    assert _stage(r) == "dash-asset-badstatus", r.stderr
    assert "http-503" in r.stderr
    assert not marker.exists()


# ===========================================================================
# 9. INCONCLUSIVE tiers -- correlated-noise avoidance
# ===========================================================================


def test_root_non_200_is_inconclusive_not_a_second_red(tmp_path, dash):
    """'The dashboard is down' is already owned by the QFlix Dashboard app
    monitor and the mobile-ux canary. Two reds for one cause is the correlated
    noise this repo keeps removing. Safe because the incident's signature was
    root 200 -- this branch cannot mask it."""
    srv = dash({})   # everything 404s, including the root
    r = _run(env=_mkenv(tmp_path, srv))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("dash-check-inconclusive"), r.stdout
    assert "root-http-404" in r.stdout


def test_running_out_of_budget_is_inconclusive_never_a_pass(tmp_path, dash):
    """Soundness of the PASS itself. If the sweep budget expires part way
    through, some referenced assets were never probed, so "everything resolves"
    was not established. Reporting PASS there would be the same class of lie as
    the marker check -- a green light that proves nothing."""
    html = _shell_html(SMALL_REFS)
    srv = dash(_base_plan(SMALL_REFS, html))
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                        QFLIX_CANARY_DASH_BUDGET_S="0"))
    assert r.returncode == 0
    assert not r.stdout.startswith("PASS:"), r.stdout
    assert "dash-check-inconclusive" in r.stdout
    assert "budget-exhausted" in r.stdout, r.stdout


def test_capping_the_reference_count_is_disclosed_in_the_pass_message(
        tmp_path, dash):
    html = _shell_html(REAL_REFS)
    srv = dash(_base_plan(REAL_REFS, html))
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, REAL_REFS),
                        QFLIX_CANARY_DASH_MAX_ASSETS="4"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("PASS:")
    assert "CAPPED-at-4-refs" in r.stdout, r.stdout


def test_unreachable_root_is_inconclusive(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()   # nothing listening -> connection refused
    r = _run(env=_mkenv(tmp_path, QFLIX_CANARY_DASH_URL="http://127.0.0.1:%d/"
                        % port, QFLIX_CANARY_DASH_TIMEOUT_S="2"))
    assert r.returncode == 0, r.stderr
    assert "dash-check-inconclusive" in r.stdout
    assert "root-transport-" in r.stdout


# ===========================================================================
# 10. THE 24h BREAKER
# ===========================================================================

LATCH = ("state", "heal-latch.epoch")


def _latch(tmp_path):
    return tmp_path / LATCH[0] / LATCH[1]


def _broken_env(tmp_path, dash_factory, marker, **extra):
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    plan[SMALL_REFS[0]] = {"*": _resp(404, b"")}
    srv = dash_factory(plan)
    return srv, _mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                       restart_cmd=_restart_stub(tmp_path, "s", marker=marker),
                       **extra)


def test_a_heal_stamps_the_durable_latch(tmp_path, dash):
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker)
    r = _run(env=env)
    assert r.returncode != 0
    assert marker.exists()
    latch = _latch(tmp_path)
    assert latch.exists(), "the breaker must be durable, not in-memory"
    stamped = int(latch.read_text(encoding="utf-8").strip())
    assert abs(stamped - int(time.time())) < 600


def test_a_second_heal_inside_the_cooldown_is_refused_loudly(tmp_path, dash):
    """Refused, NOT silently passed. flaresolverr-canary.py:306-317 pages
    'restart REFUSED ... operator intervention needed' rather than going
    quiet, because a crash-loop the canary hides is worse than one it names."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker)
    first = _run(env=env)
    assert first.returncode != 0
    assert marker.read_text(encoding="utf-8").count("restart") == 1

    second = _run(env=env)
    assert second.returncode != 0
    assert _stage(second) == "dash-heal-breaker-open", second.stderr
    assert "OPERATOR-INTERVENTION-NEEDED" in second.stderr
    assert marker.read_text(encoding="utf-8").count("restart") == 1, (
        "the breaker must prevent the SECOND restart, not just relabel it")


def test_a_latch_older_than_the_cooldown_lets_the_heal_fire_again(tmp_path, dash):
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker)
    latch = _latch(tmp_path)
    latch.parent.mkdir(parents=True, exist_ok=True)
    latch.write_text(str(int(time.time()) - 25 * 3600), encoding="utf-8")
    r = _run(env=env)
    assert r.returncode != 0
    assert _stage(r) != "dash-heal-breaker-open", r.stderr
    assert marker.exists()


def test_a_latch_just_inside_the_cooldown_still_blocks(tmp_path, dash):
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker)
    latch = _latch(tmp_path)
    latch.parent.mkdir(parents=True, exist_ok=True)
    latch.write_text(str(int(time.time()) - 23 * 3600), encoding="utf-8")
    r = _run(env=env)
    assert _stage(r) == "dash-heal-breaker-open", r.stderr
    assert not marker.exists()


def test_a_corrupt_latch_fails_open(tmp_path, dash):
    """qflix-collect.py:679-689 records the reasoning verbatim: 'the worst
    case there is one extra fire, not a permanently-stuck queue'. A garbage
    latch must never wedge the heal shut forever."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker)
    latch = _latch(tmp_path)
    latch.parent.mkdir(parents=True, exist_ok=True)
    latch.write_text("not-an-epoch\x00", encoding="utf-8")
    r = _run(env=env)
    assert _stage(r) != "dash-heal-breaker-open", r.stderr
    assert marker.exists()


def test_a_custom_cooldown_is_honored(tmp_path, dash):
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker,
                         QFLIX_CANARY_DASH_HEAL_COOLDOWN_H="1")
    latch = _latch(tmp_path)
    latch.parent.mkdir(parents=True, exist_ok=True)
    latch.write_text(str(int(time.time()) - 2 * 3600), encoding="utf-8")
    r = _run(env=env)
    assert _stage(r) != "dash-heal-breaker-open", r.stderr
    assert marker.exists()


def test_a_restart_that_was_never_issued_does_not_burn_the_breaker(tmp_path, dash):
    """Council defect D1 (.claude/council-ledger.jsonl:122, major, 2026-07-20):
    the SAB breaker stamped its latch unconditionally after the repair call,
    including on an `error:no-secrets` no-op -- spending the whole 24h budget
    on a restart that never happened and mis-signalling a fire. Same trap here
    when systemctl is absent (which is exactly what happens if this script is
    ever run off-box)."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(
        tmp_path, dash, marker,
        QFLIX_CANARY_DASH_RESTART_CMD=str(
            tmp_path / "definitely-not-a-real-binary-xyz"))
    r = _run(env=env)
    assert r.returncode != 0
    assert _stage(r) == "dash-heal-not-issued", r.stderr
    assert "latch-not-burned" in r.stderr
    assert not _latch(tmp_path).exists(), (
        "a repair that could not be attempted must NOT consume the 24h budget")
    assert not marker.exists()

    events = _read_events(tmp_path)
    assert any(e["outcome"].startswith("not-issued") for e in events), events


def test_a_restart_that_ran_and_failed_does_burn_the_breaker(tmp_path, dash):
    """The mutation WAS attempted against the box. Retrying it every 15 minutes
    against a unit that refuses to restart is precisely the churn the breaker
    exists to stop, so this leg is the opposite of the not-issued leg."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(
        tmp_path, dash, marker,
        QFLIX_CANARY_DASH_RESTART_CMD=_restart_stub(
            tmp_path, "failing", rc=3, marker=marker))
    r = _run(env=env)
    assert r.returncode != 0
    assert _stage(r) == "dash-heal-failed", r.stderr
    assert "rc=3" in r.stderr
    assert marker.exists()
    assert _latch(tmp_path).exists(), (
        "an issued-but-failed restart must consume the budget")


# ===========================================================================
# 11. POST-REPAIR RE-VERIFICATION
# ===========================================================================


def test_a_heal_that_works_is_reported_as_verified(tmp_path, dash):
    """The restart stub flips the fake server healthy, so the re-probe really
    does see a fixed dashboard. Note the exit code stays NON-ZERO on purpose:
    scripts/canaries/quota.sh:67-80 pushes DOWN even on a successful
    autonomous reclaim so the operator sees that something happened."""
    html = _shell_html(SMALL_REFS)
    broken = _base_plan(SMALL_REFS, html)
    broken[SMALL_REFS[0]] = {"*": _resp(404, b"")}
    healthy = _base_plan(SMALL_REFS, html)
    fix_flag = tmp_path / "fixed"
    marker = tmp_path / "restarted"
    srv = dash(broken, fixed_table=healthy, fix_flag=fix_flag)

    r = _run(env=_mkenv(
        tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
        restart_cmd=_restart_stub(tmp_path, "s", marker=marker,
                                  fix_flag=fix_flag)))
    assert r.returncode != 0, "a successful autonomous restart still pages once"
    assert _stage(r) == "dash-healed", r.stderr
    assert "RE-VERIFIED-healthy" in r.stderr
    assert fix_flag.exists()

    log = _read_log(tmp_path)
    assert "heal VERIFIED healthy" in log, log
    events = _read_events(tmp_path)
    assert any(e["outcome"] == "recovered" for e in events), events


def test_a_heal_that_does_not_work_is_never_claimed_as_success(tmp_path, dash):
    """Council defect D2's sibling: the restart succeeds at the command level
    but the fault persists. Reporting that as healed would be a lie the
    operator acts on."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker)
    r = _run(env=env)
    assert r.returncode != 0
    assert _stage(r) == "dash-heal-unverified", r.stderr
    assert "still-failing-after" in r.stderr
    assert "dash-healed" not in r.stderr
    events = _read_events(tmp_path)
    assert any(e["outcome"] == "issued-not-verified" for e in events), events


def test_re_verification_actually_re_fetches_the_dashboard(tmp_path, dash):
    """The analogue of tests/unit/test_qflix_collect_stale_state.py:349's
    `call_count == 2`: prove the verification really went back to the wire
    instead of reusing the pre-restart verdict."""
    marker = tmp_path / "restarted"
    srv, env = _broken_env(tmp_path, dash, marker)
    r = _run(env=env)
    assert r.returncode != 0
    # 1 root + 3 refs x 3 encodings = 12 probes per sweep; a re-verify sweep
    # doubles that.
    assert len(srv.hits) >= 24, len(srv.hits)


# ===========================================================================
# 12. PAUSE WINDOW -- restart suppressed, detection still reported
# ===========================================================================


def test_the_maintenance_window_suppresses_the_restart_but_still_reports(
        tmp_path, dash):
    """RULE 7 in full: no box operations during the Monday window, but
    detection and alerting may still report. So the fetch and the verdict run
    and the 404 detail reaches Kuma; only the mutation is withheld."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker,
                         QFLIX_CANARY_DASH_FORCE_WINDOW="1")
    r = _run(env=env)
    assert r.returncode != 0
    assert _stage(r) == "dash-heal-suppressed-window", r.stderr
    assert "404=3" in r.stderr, "detection must still be reported in-window"
    assert not marker.exists(), "NO box operations during the window"
    assert not _latch(tmp_path).exists(), (
        "a suppressed heal must not consume the 24h budget either")


@pytest.mark.parametrize("now_iso,expect_suppressed,label", [
    ("2026-07-27T11:00:00Z", True, "monday 11:00 - window opens"),
    ("2026-07-27T12:30:00Z", True, "monday mid-window"),
    ("2026-07-27T14:59:00Z", True, "monday 14:59 - last minute"),
    ("2026-07-27T10:59:00Z", False, "monday 10:59 - before the window"),
    ("2026-07-27T15:00:00Z", False, "monday 15:00 - watchdog clears it"),
    ("2026-07-28T12:30:00Z", False, "tuesday same hour"),
    ("2026-07-26T12:30:00Z", False, "sunday same hour"),
])
def test_wallclock_window_boundaries(tmp_path, dash, now_iso, expect_suppressed,
                                     label):
    """The wall-clock leg exists because the lockfile only exists if the window
    orchestrator actually opened it; if manitoba-maint-window.service failed,
    the calendar window is still live and box ops are still forbidden. Negative
    controls included, per the test_canary.py:403-432 convention."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker,
                         QFLIX_CANARY_DASH_FORCE_WINDOW=None,
                         QFLIX_CANARY_DASH_NOW=now_iso)
    r = _run(env=env)
    assert r.returncode != 0
    if expect_suppressed:
        assert _stage(r) == "dash-heal-suppressed-window", (label, r.stderr)
        assert "wallclock-mon-1100-1500-utc" in r.stderr
        assert not marker.exists(), label
    else:
        assert _stage(r) != "dash-heal-suppressed-window", (label, r.stderr)
        assert marker.exists(), label


def test_the_canonical_suppression_predicate_is_genuinely_consulted(
        tmp_path, dash):
    """Leg 2 imports lib.suppression and calls in_maintenance_window() -- the
    same function lib/cli.py:562 and lib/kuma.do_POST use. Asserting on the leg
    name proves the canonical predicate ran, not just a local reimplementation
    of it. (RULE 7 named suppression.in_pause_window, which reads
    app.pause_window; manifest.Canary has no such field and qflix-dash declares
    none, so that call would return False unconditionally -- inert. See the
    script header.)"""
    state = tmp_path / "manitoba-state"
    state.mkdir()
    (state / "lock").write_text("%d\n2026-07-27T11:00:00Z\n" % os.getpid(),
                                encoding="utf-8")
    marker = tmp_path / "restarted"
    _, env = _broken_env(
        tmp_path, dash, marker,
        QFLIX_CANARY_DASH_FORCE_WINDOW=None,
        # A Tuesday, so the wall-clock leg cannot be what fires.
        QFLIX_CANARY_DASH_NOW="2026-07-28T09:00:00Z",
        QFLIX_CANARY_DASH_MAINT_LIB=str(REPO_ROOT / "scripts" / "maint"),
        MANITOBA_STATE_DIR=str(state))
    r = _run(env=env)
    assert r.returncode != 0
    assert _stage(r) == "dash-heal-suppressed-window", r.stderr
    assert "suppression-in_maintenance_window" in r.stderr, r.stderr
    assert not marker.exists()


def test_a_live_pid_lockfile_suppresses_even_when_the_lib_is_unimportable(
        tmp_path, dash):
    """Leg 3: the guard must survive lib being unimportable mid-deploy (the base
    env already points MAINT_LIB at a directory that does not exist, so leg 2
    genuinely raises here), which is why the lockfile is also read directly."""
    state = tmp_path / "manitoba-state"
    state.mkdir()
    (state / "lock").write_text("%d\n" % os.getpid(), encoding="utf-8")
    marker = tmp_path / "restarted"
    _, env = _broken_env(
        tmp_path, dash, marker,
        QFLIX_CANARY_DASH_FORCE_WINDOW=None,
        QFLIX_CANARY_DASH_NOW="2026-07-28T09:00:00Z",
        MANITOBA_STATE_DIR=str(state))
    r = _run(env=env)
    assert _stage(r) == "dash-heal-suppressed-window", r.stderr
    assert "-lock-" in r.stderr, r.stderr
    assert not marker.exists()
    # The failed leg-2 import must be recorded, not swallowed.
    assert "window leg suppression-unavailable" in _read_log(tmp_path)


@pytest.mark.skipif(os.name != "posix",
                    reason="kill(0) liveness semantics are posix-only")
def test_a_leaked_lockfile_with_a_dead_pid_does_not_suppress(tmp_path, dash):
    """A leaked lock must not disable the heal forever -- matching
    qflix-torrent-janitor.py:224-231, which liveness-checks the PID rather
    than trusting the file's existence."""
    state = tmp_path / "manitoba-state"
    state.mkdir()
    (state / "lock").write_text("999999999\n", encoding="utf-8")
    marker = tmp_path / "restarted"
    _, env = _broken_env(
        tmp_path, dash, marker,
        QFLIX_CANARY_DASH_FORCE_WINDOW=None,
        QFLIX_CANARY_DASH_NOW="2026-07-28T09:00:00Z",
        QFLIX_CANARY_DASH_MAINT_LIB="",
        MANITOBA_STATE_DIR=str(state))
    r = _run(env=env)
    assert _stage(r) != "dash-heal-suppressed-window", r.stderr
    assert marker.exists()


def test_window_hour_literals_match_the_systemd_calendars(tmp_path):
    """CONFIG-DRIFT LOCK, the technique from
    tests/unit/test_pause_window_chokepoint.py:102-117. The Monday window's
    calendar is authoritative in the unit files, not in python
    (lib/window.py:44 only mentions it in a comment), so the canary's two hour
    literals are pinned against those units here. Change one without the other
    and this fails."""
    script = SCRIPT.read_text(encoding="utf-8")
    start = re.search(r"OnCalendar=Mon \*-\*-\* (\d{2}):00:00 UTC",
                      WINDOW_TIMER.read_text(encoding="utf-8"))
    end = re.search(r"OnCalendar=Mon \*-\*-\* (\d{2}):00:00 UTC",
                    WATCHDOG_TIMER.read_text(encoding="utf-8"))
    assert start and end, "window timer calendars moved -- update this guard"
    assert ("WINDOW_START_HOUR_UTC = %d" % int(start.group(1))) in script
    assert ("WINDOW_END_HOUR_UTC = %d" % int(end.group(1))) in script
    # And the guard must actually be wired into the restart decision.
    assert "restart_suppressed_by_window()" in script
    assert "in_maintenance_window" in script


def test_the_script_does_not_call_the_inert_pause_window_predicate(tmp_path):
    """suppression.in_pause_window(app) reads app.pause_window; manifest.Canary
    has no such field, so calling it on a canary returns False unconditionally.
    Writing it would satisfy the letter of the rule while providing zero
    protection -- the committed-but-inert failure this work exists to prevent.
    Locked so a future 'compliance' edit cannot quietly reintroduce it. Only
    the CALL FORM is locked, in the executable body -- the header and the
    window_active() docstring MUST discuss the predicate by name, to document
    why it is deliberately absent."""
    script = SCRIPT.read_text(encoding="utf-8")
    body = script.split("set -uo pipefail", 1)[1]
    assert "in_pause_window(" not in body
    # and the predicate that IS used must be the lockfile one
    assert "in_maintenance_window()" in body


# ===========================================================================
# 13. ARMING, COLD START, DISARM
# ===========================================================================


def test_self_heal_can_be_disarmed_by_env(tmp_path, dash):
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker,
                         QFLIX_CANARY_DASH_SELF_HEAL="0")
    r = _run(env=env)
    assert r.returncode != 0
    assert _stage(r) == "dash-heal-disarmed", r.stderr
    assert not marker.exists()
    assert not _latch(tmp_path).exists()


def test_dry_run_argv_disarms_the_restart(tmp_path, dash):
    """scripts/maint/flaresolverr-canary.py:319-321's --dry-run: probe and
    decide, but never mutate."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker)
    r = _run(["--dry-run"], env=env)
    assert r.returncode != 0
    assert _stage(r) == "dash-heal-disarmed", r.stderr
    assert not marker.exists()


def test_self_heal_is_armed_by_default(tmp_path, dash):
    """A self-heal that ships disarmed makes the repo read as if the concern is
    covered when it is not -- the same objection as a canary that ships
    unscheduled. The production default must be armed."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker)
    env.pop("QFLIX_CANARY_DASH_SELF_HEAL", None)
    r = _run(env=env)
    assert r.returncode != 0
    assert _stage(r) != "dash-heal-disarmed", r.stderr
    assert marker.exists()


def test_a_cold_started_unit_is_not_restarted_again(tmp_path, dash):
    """flaresolverr-canary.py:299-304. If the unit entered active seconds ago
    and the assets are STILL broken, another restart is churn -- and it is the
    backstop if the latch write ever fails."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker,
                         QFLIX_CANARY_DASH_UPTIME_S="5",
                         QFLIX_CANARY_DASH_MIN_UPTIME_S="120")
    r = _run(env=env)
    assert r.returncode != 0
    assert _stage(r) == "dash-heal-cold-start", r.stderr
    assert "OPERATOR-INTERVENTION-NEEDED" in r.stderr
    assert not marker.exists()
    assert not _latch(tmp_path).exists()


def test_a_warm_unit_is_restarted(tmp_path, dash):
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker,
                         QFLIX_CANARY_DASH_UPTIME_S="600",
                         QFLIX_CANARY_DASH_MIN_UPTIME_S="120")
    r = _run(env=env)
    assert _stage(r) != "dash-heal-cold-start", r.stderr
    assert marker.exists()


# ===========================================================================
# 14. LOUD FAILURE ON MISSING INPUT (never a silent green)
# ===========================================================================


def test_a_missing_host_secret_fails_loudly_instead_of_passing(tmp_path):
    """scripts/maint/lib/cli.py:628-630 skips the Kuma push SILENTLY when a
    token key is absent, so a canary that swallowed a missing prerequisite into
    exit 0 would be invisible twice over. Same loud shape as
    scripts/canaries/mobile-ux.sh:13-14."""
    fake_home = tmp_path / "empty-home"
    fake_home.mkdir()
    env = _mkenv(tmp_path)
    env.pop("QFLIX_CANARY_DASH_URL", None)
    env["HOME"] = str(fake_home)
    r = _run(env=env)
    assert r.returncode == 1
    assert "STAGE=dash-host-secret-missing" in r.stderr
    assert r.stdout.strip() == ""


def test_an_empty_host_secret_fails_loudly(tmp_path):
    fake_home = tmp_path / "blank-home"
    (fake_home / "secrets").mkdir(parents=True)
    (fake_home / "secrets" / "seedbox.host").write_text("\n", encoding="utf-8")
    env = _mkenv(tmp_path)
    env.pop("QFLIX_CANARY_DASH_URL", None)
    env["HOME"] = str(fake_home)
    r = _run(env=env)
    assert r.returncode == 1
    assert "seedbox.host-empty" in r.stderr


# ===========================================================================
# 15. DURABLE AUDIT TRAIL
# ===========================================================================


def test_every_heal_attempt_lands_in_the_durable_log_and_event_trail(
        tmp_path, dash):
    """journald on this shared seedbox is permission-restricted and
    rotation-prone ('No entries' while debugging the 2026-07-13 reaper
    failure), so the logfile is the trail that is actually trusted --
    scripts/maint/qflix-reaper.py:140-192."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker)
    r = _run(env=env)
    assert r.returncode != 0

    log = _read_log(tmp_path)
    assert "REPAIRABLE signature detected" in log, log
    assert "heal ISSUING restart" in log, log
    assert re.search(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z "
                     r"\[dash-asset-integrity\] ", log, re.M), log

    events = _read_events(tmp_path)
    assert events, "no structured event trail written"
    e = events[-1]
    assert e["action"] == "restart"
    assert set(("ts", "action", "trigger", "outcome")) <= set(e)


def test_the_pass_path_also_writes_a_durable_line(tmp_path, dash):
    html = _shell_html(SMALL_REFS)
    srv = dash(_base_plan(SMALL_REFS, html))
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS)))
    assert r.returncode == 0
    assert "PASS refs=3" in _read_log(tmp_path)


def test_old_logs_are_pruned_and_recent_ones_kept(tmp_path, dash):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    old = state / "dash-asset-integrity-20250101.log"
    old.write_text("ancient\n", encoding="utf-8")
    os.utime(old, (time.time() - 40 * 86400, time.time() - 40 * 86400))
    recent = state / "dash-asset-integrity-20250102.log"
    recent.write_text("recent\n", encoding="utf-8")

    html = _shell_html(SMALL_REFS)
    srv = dash(_base_plan(SMALL_REFS, html))
    r = _run(env=_mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS)))
    assert r.returncode == 0
    assert not old.exists(), "logs older than 30 days must be pruned"
    assert recent.exists()


# ===========================================================================
# 16. THE SCRIPT ITSELF -- shape and hygiene
# ===========================================================================


def test_script_is_ascii_and_lf_only():
    """.gitattributes:11 forces eol=lf repo-wide; a CRLF shell script shipped
    to the seedbox broke 4 cron heartbeats on 2026-05-11 ("env couldn't
    resolve 'bash\\r'")."""
    raw = SCRIPT.read_bytes()
    assert b"\r" not in raw, "CRLF found"
    bad = [(i, b) for i, b in enumerate(raw) if b > 127]
    assert not bad, "non-ASCII bytes at %r" % bad[:5]
    assert raw.startswith(b"#!/usr/bin/env bash\n")


def test_script_uses_the_canary_set_flags_not_dash_e():
    """`set -e` in a canary is a footgun: an assignment or check that exits
    non-zero aborts the script before the STAGE= line is emitted and Kuma sees
    a bare exit-1 with no label. Every failure-capable canary here uses
    `set -uo pipefail` (ucc-gate-stuck.sh:60, sab-stall.sh:25)."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "\nset -uo pipefail\n" in text
    assert "set -euo pipefail" not in text


def test_script_parses_under_bash():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stderr


def test_script_does_not_re_grep_the_blind_marker():
    """The whole point. data-qflix-dash lives in the server-rendered shell and
    survives a total hydration failure; re-checking it here would rebuild the
    blind spot inside the canary meant to close it."""
    text = SCRIPT.read_text(encoding="utf-8")
    body = text.split("set -uo pipefail", 1)[1]
    assert "data-qflix-dash" not in body, (
        "the marker may be discussed in the header, never asserted on")


def test_script_never_pushes_to_kuma_itself():
    """cli.py owns the push, using token key canary-<name> from
    ~/secrets/kuma-push-tokens.json (scripts/maint/lib/cli.py:626-644). A
    bespoke self-push here would double-count in the drift audit and mint an
    orphan token key."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "api/push" not in text
    assert "kuma-push-tokens" not in text


def test_script_reads_state_from_opt_maint_not_from_secrets():
    """Push tokens live in ~/secrets/; state and logs live under ~/.opt/maint/
    (the reaper precedent, ~/.opt/maint/reaper/). Mixing them is how the
    2026-07-28 token gap happened."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert '".opt" / "maint" / "dash-asset-integrity"' in text
    assert 'secrets' in text  # only for the seedbox.host read
    assert "secrets/kuma" not in text


# ===========================================================================
# 15. REGRESSION GUARDS FOR THE 2026-07-29 ADVERSARIAL REVIEW
# ===========================================================================
# Four defects were found by running THIS script against a loopback harness.
# Each test below reproduces one and pins the fix. Every one of them FAILS
# against the pre-fix script (verified by mutation), which is what makes them
# worth having rather than decoration.


# --- 15a. the breaker must fail CLOSED on an unwritable latch -------------
# Reviewed defect: stamp_latch() swallowed its write error, returned False, and
# the caller only LOGGED the result. heal_cooldown_active() then failed OPEN on
# the absent latch, so a repairable fault produced an unattended
# `systemctl --user restart qflix-dash` on EVERY 15-minute tick - 96/day - with
# no durable record, because the narrative log and events/*.jsonl die on the
# same ENOSPC/EPERM. Two-arm proof: latch unwritable -> 3 ticks/3 restarts;
# writable -> 3 ticks/1 restart, with only that one variable changed.


def _block_latch(tmp_path):
    """Make the latch FILE unwritable while leaving the state dir writable, so
    ONLY the latch write fails. A directory at the latch path does that on both
    POSIX and Windows, which matters because CI is ubuntu and the workstation is
    Windows."""
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    (d / LATCH[1]).mkdir()


def test_an_unwritable_latch_refuses_the_restart_instead_of_looping(tmp_path,
                                                                   dash):
    marker = tmp_path / "restarted"
    _block_latch(tmp_path)
    _, env = _broken_env(tmp_path, dash, marker)
    stages = []
    for _ in range(3):
        r = _run(env=env)
        assert r.returncode != 0, r.stdout
        stages.append(_stage(r))
    assert stages == ["dash-heal-latch-unwritable"] * 3, stages
    assert not marker.exists(), (
        "NO restart may be issued without a durable 1-per-24h breaker; "
        "issuing one anyway is an unbounded restart loop on a full disk")


def test_the_unwritable_latch_refusal_still_pages_and_names_the_cause(tmp_path,
                                                                     dash):
    marker = tmp_path / "restarted"
    _block_latch(tmp_path)
    _, env = _broken_env(tmp_path, dash, marker)
    r = _run(env=env)
    assert r.returncode != 0
    assert r.stdout == "", "a refused heal is a FAIL, never a pass line"
    assert "OPERATOR-INTERVENTION-NEEDED" in r.stderr, r.stderr
    assert "no-durable-breaker" in r.stderr, r.stderr


def test_the_latch_is_reserved_BEFORE_the_restart_is_issued(tmp_path, dash):
    """Reserve-before-act is the whole fix: a stamp written AFTER the mutation
    cannot prevent the mutation. Proven by side effect - the restart stub records
    whether the latch already existed at the moment it ran."""
    marker = tmp_path / "restarted"
    witness = tmp_path / "latch-seen-by-restart.txt"
    latch = _latch(tmp_path)
    stub = tmp_path / "witness-stub.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ -f "{latch}" ]; then echo LATCH-PRESENT > "{w}"; '
        'else echo LATCH-ABSENT > "{w}"; fi\n'
        'printf "restart\\n" >> "{m}"\n'
        "exit 0\n".format(latch=latch.as_posix(), w=witness.as_posix(),
                          m=marker.as_posix()),
        encoding="utf-8", newline="\n")
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    plan[SMALL_REFS[0]] = {"*": _resp(404, b"")}
    srv = dash(plan)
    env = _mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                 restart_cmd="bash " + stub.as_posix())
    r = _run(env=env)
    assert marker.exists(), r.stderr
    assert witness.read_text(encoding="utf-8").strip() == "LATCH-PRESENT", (
        "the breaker latch must already be on disk when the restart runs")


def test_a_reservation_is_RELEASED_when_the_mutation_never_happened(tmp_path,
                                                                   dash):
    """Council defect D1 (.claude/council-ledger.jsonl:122): the SAB breaker
    spent its whole 24h budget on a no-op. Reserving first must not resurrect
    that - a command that could not be spawned releases the latch, so the next
    tick can still heal."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(
        tmp_path, dash, marker,
        QFLIX_CANARY_DASH_RESTART_CMD=str(tmp_path / "does-not-exist-at-all"))
    r = _run(env=env)
    assert _stage(r) == "dash-heal-not-issued", r.stderr
    assert not marker.exists()
    assert not _latch(tmp_path).exists(), (
        "a heal that was never issued must not burn the 24h budget")
    ev = [e for e in _read_events(tmp_path) if e["action"] == "restart"]
    assert ev and ev[-1]["latch_released"] is True, ev


def test_the_latch_is_written_atomically_with_no_tmp_residue(tmp_path, dash):
    """tmp + fsync + os.replace, so a kill mid-write cannot leave a truncated
    latch - heal_cooldown_active() fails OPEN on an unparseable one, so a
    half-written latch is indistinguishable from no breaker at all.

    The crash-atomicity property itself cannot be exercised from in-process (it
    needs the process to die between write and flush), so it is pinned
    STRUCTURALLY as well as behaviourally. A structural assertion is the honest
    form here: without it, replacing os.replace with a plain write_text passes
    every behavioural test while silently removing the guarantee.
    """
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker)
    _run(env=env)
    assert _latch(tmp_path).exists()
    leftovers = sorted(p.name for p in (tmp_path / "state").glob("*.tmp.*"))
    assert leftovers == [], leftovers
    src = SCRIPT.read_text(encoding="utf-8")
    assert "os.replace(str(tmp), str(latch_path()))" in src, (
        "the latch write must be atomic (tmp + os.replace), not a direct write")
    assert "os.fsync(fh.fileno())" in src


def test_a_latch_that_cannot_be_read_back_is_treated_as_unwritable(tmp_path,
                                                                  dash):
    """A write that appears to succeed but does not persist is the ENOSPC case:
    write() returns, flush fails, the file is empty. Read-back is what turns
    that into a refusal instead of a phantom breaker."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert 'if latch_path().read_text(encoding="utf-8").strip() != stamp:' in src
    assert "latch-read-back-MISMATCH" in src


# --- 15b. the cold-start guard must be able to fire ----------------------
# Reviewed defect: MIN_UPTIME_S defaulted to 120s against an OnCalendar=*:0/15
# timer, so measured uptime at the next tick was always ~900s and the guard the
# header advertised as the breaker's backstop was unreachable code.


def test_min_uptime_default_exceeds_one_timer_tick():
    text = SCRIPT.read_text(encoding="utf-8")
    tick = re.search(r'QFLIX_CANARY_DASH_TICK_S"?,\s*(\d+)\)', text)
    assert tick, "TICK_S default not found (was it renamed?)"
    assert int(tick.group(1)) == 900
    assert 'QFLIX_CANARY_DASH_MIN_UPTIME_S", TICK_S * 2' in text, (
        "MIN_UPTIME_S must be derived from the tick and exceed it, or the "
        "cold-start guard can never fire")


def test_tick_literal_matches_the_timer_oncalendar():
    """The schedule is authoritative in the unit, not in python."""
    unit = (REPO_ROOT / "scripts" / "maint" / "systemd"
            / "manitoba-maint-canary-dash-asset-integrity.timer")
    body = unit.read_text(encoding="utf-8")
    m = re.search(r"OnCalendar=\*:0/(\d+)", body)
    assert m, body
    minutes = int(m.group(1))
    text = SCRIPT.read_text(encoding="utf-8")
    tick = int(re.search(r'QFLIX_CANARY_DASH_TICK_S"?,\s*(\d+)\)',
                         text).group(1))
    assert tick == minutes * 60, (
        "TICK_S=%d but the timer fires every %d min" % (tick, minutes))


def test_a_unit_restarted_one_tick_ago_and_still_broken_is_not_restarted_again(
        tmp_path, dash):
    """900s is exactly the uptime one tick after a heal. Under the old 120s
    default this sailed past the guard; it must now be refused."""
    marker = tmp_path / "restarted"
    _, env = _broken_env(tmp_path, dash, marker,
                         QFLIX_CANARY_DASH_UPTIME_S="900")
    r = _run(env=env)
    assert _stage(r) == "dash-heal-cold-start", r.stderr
    assert not marker.exists()


# --- 15c. a length mismatch must be CORROBORATED before it heals ---------
# Reviewed defect: record() promoted any single IncompleteRead straight to the
# repairable bucket, so ONE mid-body drop restarted a healthy dashboard,
# reported "restarted-and-RE-VERIFIED-healthy", and burned the 24h latch - so a
# REAL incident inside the next 24h would have needed a human.


def _one_lie_env(tmp_path, dash_factory, marker, **extra):
    """Everything healthy and ZERO 404s, except the FIRST identity request for
    one asset over-declares Content-Length. Every later request tells the truth,
    which is what a transient wire drop looks like."""
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    payload = ("// " + SMALL_REFS[1] + "\n" + "x" * 64).encode("utf-8")
    plan[SMALL_REFS[1]] = dict(plan[SMALL_REFS[1]])
    plan[SMALL_REFS[1]]["identity"] = _resp(
        200, payload, declared=len(payload) + 40, honest_after_first=True)
    srv = dash_factory(plan)
    return srv, _mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                       restart_cmd=_restart_stub(tmp_path, "s", marker=marker),
                       QFLIX_CANARY_DASH_ENCODINGS="identity", **extra)


def test_a_single_transient_truncation_never_restarts_the_dashboard(tmp_path,
                                                                   dash):
    marker = tmp_path / "restarted"
    _, env = _one_lie_env(tmp_path, dash, marker)
    r = _run(env=env)
    assert not marker.exists(), (
        "one dropped response must not restart a customer-facing app")
    assert r.returncode == 0, r.stderr
    assert "dash-check-inconclusive" in r.stdout, r.stdout
    assert "not-reproduced" in r.stdout, r.stdout
    assert not _latch(tmp_path).exists(), (
        "wire noise must not spend the 24h self-heal budget")


def test_a_transient_truncation_is_reported_not_silently_discarded(tmp_path,
                                                                  dash):
    marker = tmp_path / "restarted"
    _, env = _one_lie_env(tmp_path, dash, marker)
    r = _run(env=env)
    assert "transport-errors" in r.stdout, r.stdout
    assert "dash-asset-integrity" in _read_log(tmp_path)


def test_a_deterministic_length_lie_is_still_a_single_cycle_heal(tmp_path,
                                                                dash):
    """The corroboration must NOT cost detection latency for the real fault: a
    stale sirv stat tuple lies identically on every request, so the re-probe
    confirms it and the heal still fires on the first cycle."""
    marker = tmp_path / "restarted"
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    payload = ("// " + SMALL_REFS[1] + "\n" + "x" * 64).encode("utf-8")
    plan[SMALL_REFS[1]] = dict(plan[SMALL_REFS[1]])
    plan[SMALL_REFS[1]]["identity"] = _resp(200, payload,
                                            declared=len(payload) + 40)
    srv = dash(plan)
    env = _mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                 restart_cmd=_restart_stub(tmp_path, "s", marker=marker),
                 QFLIX_CANARY_DASH_ENCODINGS="identity")
    r = _run(env=env)
    assert r.returncode != 0, r.stdout
    assert "len-mismatch=1" in r.stderr, r.stderr
    assert marker.exists(), "a reproducible stale length IS the repairable fault"


# --- 15d. a 200 whose body never arrives is not a PASS -------------------
# Reviewed defect: when the mid-body read raised anything other than
# IncompleteRead, `received` stayed None and record() filed the probe in NO
# bucket at all - it counted as a healthy probe and the canary printed
# "PASS ... all-200-and-length-consistent" for an asset the browser can never
# execute. The exact false-green class this canary exists to eliminate.


def _stall_env(tmp_path, dash_factory, marker, path_index=1, **extra):
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    plan[SMALL_REFS[path_index]] = {
        "*": _resp(200, b"z" * 5000, declared=5000, stall_s=6)}
    srv = dash_factory(plan)
    return srv, _mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                       restart_cmd=_restart_stub(tmp_path, "s", marker=marker),
                       QFLIX_CANARY_DASH_ENCODINGS="identity",
                       QFLIX_CANARY_DASH_TIMEOUT_S="2", **extra)


def test_a_200_whose_body_never_arrives_is_never_a_pass(tmp_path, dash):
    marker = tmp_path / "restarted"
    _, env = _stall_env(tmp_path, dash, marker)
    r = _run(env=env)
    assert r.stdout == "", (
        "a stalled asset must not produce a PASS line: %r" % r.stdout)
    assert r.returncode != 0
    assert _stage(r) == "dash-asset-unread", r.stderr


def test_a_stalled_body_alerts_but_never_restarts(tmp_path, dash):
    """headers-then-stall is a wedged worker, not stale in-process state, so it
    is deliberately outside the narrow repairable signature."""
    marker = tmp_path / "restarted"
    _, env = _stall_env(tmp_path, dash, marker)
    r = _run(env=env)
    assert _stage(r) == "dash-asset-unread"
    assert not marker.exists()
    assert not _latch(tmp_path).exists()


def test_a_stalled_ROOT_document_is_not_filed_as_inconclusive(tmp_path, dash):
    """The document-level form of the same fault. Labelling it inconclusive
    would push Kuma UP for a dashboard that cannot load at all."""
    marker = tmp_path / "restarted"
    html = _shell_html(SMALL_REFS)
    plan = _base_plan(SMALL_REFS, html)
    plan["/"] = {"*": _resp(200, html.encode("utf-8"),
                            declared=len(html) + 500, stall_s=6)}
    srv = dash(plan)
    env = _mkenv(tmp_path, srv, build=_build_tree(tmp_path, SMALL_REFS),
                 restart_cmd=_restart_stub(tmp_path, "s", marker=marker),
                 QFLIX_CANARY_DASH_ENCODINGS="identity",
                 QFLIX_CANARY_DASH_TIMEOUT_S="2")
    r = _run(env=env)
    assert r.returncode != 0, r.stdout
    assert "dash-check-inconclusive" not in r.stdout, r.stdout
    assert _stage(r) == "dash-asset-unread", r.stderr


def test_a_stalled_probe_is_retried_before_it_is_believed(tmp_path, dash):
    """`unread` is transport-level, so probe_with_retry must retry it. A 404 is
    immutable for the process lifetime and is deliberately NOT retried, but body
    delivery is not covered by that argument."""
    marker = tmp_path / "restarted"
    srv, env = _stall_env(tmp_path, dash, marker)
    _run(env=env)
    hits = [h for h in srv.hits if h[0] == SMALL_REFS[1]]
    assert len(hits) >= 2, (
        "a stalled 200 must be re-probed at least once, saw %d" % len(hits))
