"""The library-container-sanity canary, executed rather than grepped.

WHY THIS FILE EXISTS
The canary shipped at 686 lines with zero tests, and the single most delicate
thing in it - the vacuity clock - was the untested part. It was also wrong: the
first draft asked

    asserted = payloads > 0 or arr_graded > 0

and that one OR let the *arr leg satisfy the FILESYSTEM leg. Proved live on
2026-08-20 by pointing MEDIA_ROOT at a directory that does not exist while the
four *arrs were up:

    PASS: library-container-sanity ... scanned=0 payloads=0 roots=0/5
          arr_graded=432

A filesystem check reporting green with zero filesystem examined. This repo has
now shipped that shape three times (hardlink-integrity twice, this once), which
is why the fix is pinned by tests instead of by a comment.

HOW THESE TESTS RUN THE REAL THING
The canary is a shell wrapper around an embedded python heredoc. Grepping the
shell text asserts nothing about behaviour, so the heredoc is lifted out and
EXECUTED against fixture directories, the same technique
test_hardlink_vacuity_clock.py uses for its clock and
test_canary_sshm_quoting.py uses to parse the shipped remote body. The extractor
asserts on a known symbol so a renamed delimiter fails here instead of silently
testing an empty string - the guard-the-guard rule, which is the same vacuity
defect one level up.

The *arr leg is exercised for real in the vacuity test: a stub HTTP server on
loopback plus a fake ~/secrets drives secret() / arr_base() / arr_get() /
quality_name() end to end, so `arr_graded > 0` in that test is produced by the
canary talking to something, not by a monkeypatch.
"""
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts" / "canaries" / "library-container-sanity.sh"

GREEN = "every payload is a playable container"
KIB = 1024


def _embedded_python() -> str:
    """Lift the python heredoc out of the shell wrapper.

    Pinned on a known symbol: if the delimiter or the heredoc style changes this
    raises, rather than testing an empty string and passing.
    """
    src = CANARY.read_text(encoding="utf-8")
    opener = 'python3 <<"PYEOF"'
    start = src.index(opener) + len(opener)
    end = src.index("\nPYEOF", start)
    body = src[start:end]
    assert "fs_asserted" in body, "extracted the wrong block"
    return body


@pytest.fixture(scope="module")
def code():
    return _embedded_python()


def _write(path: Path, size: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        if size:
            fh.write(b"\0" * size)
    return path


def _run(code, tmp_path, *, media_root=None, roots="Movies", skip_arr="1",
         min_payload="1024", max_named="", state_dir=None):
    """Execute the embedded canary python against a fixture tree."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)          # expanduser on Windows
    env["LCS_MEDIA_ROOT"] = str(media_root if media_root is not None
                                else tmp_path / "media")
    env["LCS_ROOTS"] = roots
    env["LCS_MIN_PAYLOAD_BYTES"] = min_payload
    env["LCS_DISC_QUALITIES"] = ""
    env["LCS_MAX_VACUOUS_DAYS"] = "7"
    env["LCS_STATE_DIR"] = str(state_dir or (tmp_path / "state"))
    env["LCS_MAX_NAMED"] = max_named
    env["LCS_ARR_TIMEOUT"] = "5"
    env["LCS_SKIP_ARR"] = skip_arr
    env["LCS_FORCE_WINDOW"] = "0"
    script = tmp_path / "canary_body.py"
    script.write_text(code, encoding="utf-8")
    return subprocess.run([sys.executable, str(script)], env=env,
                          capture_output=True, text=True, timeout=120)


def _stage_line(result) -> str:
    for line in result.stderr.splitlines():
        if line.startswith("STAGE="):
            return line
    raise AssertionError("no STAGE= line in stderr:\n" + result.stderr)


# --- the extractor itself ----------------------------------------------------

def test_the_embedded_body_is_found(code):
    """Guards the guard. An extractor that silently returns nothing would make
    every test below vacuously green - the defect this file exists to pin."""
    assert "os.walk" in code and "vacuous_exit" in code
    assert len(code.splitlines()) > 100


# --- BLOCKER: the vacuity clock is per leg -----------------------------------

class _ArrStub(BaseHTTPRequestHandler):
    """Minimal Radarr /api/v3/movie. One movie, one file, a clean quality."""

    def do_GET(self):                                    # noqa: N802
        payload = [{
            "title": "Fixture Movie",
            "movieFile": {
                "path": "/nowhere/Fixture Movie (1999).mkv",
                "quality": {"quality": {"name": "Bluray-1080p"}},
            },
        }] if self.path.endswith("/movie") else []
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                           # silence the test log
        pass


@pytest.fixture
def arr_stub():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ArrStub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def test_missing_roots_are_not_green_even_when_the_arr_leg_asserts(
        code, tmp_path, arr_stub):
    """THE BLOCKER, pinned.

    All five roots absent, the *arr leg answering normally. Under the original
    `payloads > 0 or arr_graded > 0` this printed a green PASS. A filesystem
    check MUST NOT be satisfied by a filesystem it never touched.
    """
    home = tmp_path / "home"
    _write(home / "secrets" / "radarr.key").write_text("k", encoding="utf-8")
    (home / "secrets" / "radarr.port").write_text(str(arr_stub),
                                                  encoding="utf-8")

    r = _run(code, tmp_path, media_root=tmp_path / "does-not-exist",
             roots="Movies|TV Shows|Anime|Anime Movies|Welcome", skip_arr="")

    assert GREEN not in r.stdout, (
        "green with zero filesystem examined - the leg-2-satisfies-leg-1 "
        "vacuity bug is back:\n" + r.stdout)
    assert "inconclusive" in r.stdout, r.stdout + r.stderr
    assert "no-library-roots-readable" in r.stdout
    # The *arr leg really did assert; that is the whole point of the case.
    assert "arr_graded=1" in r.stdout, (
        "the stub arr was not reached, so this test would pass for the wrong "
        "reason:\n" + r.stdout + r.stderr)


def test_one_missing_root_among_present_ones_is_not_green(code, tmp_path):
    """A root that is not there is UNEXAMINED, not empty. Half a library walked
    and reported green is how "the library moved" reads as "the library is
    clean"."""
    media = tmp_path / "media"
    _write(media / "Movies" / "T (1999)" / "T (1999).mkv", 4 * KIB)
    r = _run(code, tmp_path, roots="Movies|Anime")
    assert GREEN not in r.stdout, r.stdout
    assert "inconclusive" in r.stdout
    assert "library-roots-missing:Anime" in r.stdout


def test_a_walked_root_with_content_is_green(code, tmp_path):
    """The other side of the same clock: a real walk over a real root with the
    arr leg deliberately skipped is a verified pass, not an inconclusive."""
    media = tmp_path / "media"
    _write(media / "Movies" / "T (1999)" / "T (1999).mkv", 4 * KIB)
    r = _run(code, tmp_path)
    assert r.returncode == 0, r.stderr
    assert GREEN in r.stdout, r.stdout + r.stderr
    assert "payloads=1" in r.stdout and "roots=1/1" in r.stdout


# --- leg 1 classification ----------------------------------------------------

def test_a_disc_image_fires_at_any_size(code, tmp_path):
    """4 KB of .ifo is proof a DVD rip landed. The size gate is for UNKNOWN
    extensions only; a disc index is a finding on sight."""
    media = tmp_path / "media"
    _write(media / "Movies" / "T (1999)" / "T (1999).mkv", 4 * KIB)
    _write(media / "Movies" / "T (1999)" / "VIDEO_TS.IFO", 12)
    r = _run(code, tmp_path)
    assert r.returncode == 2
    assert _stage_line(r).startswith("STAGE=container-disc-image")


def test_a_bdmv_directory_fires_once_and_is_pruned(code, tmp_path):
    """The directory is the finding. Walking INTO it yields dozens of .clpi /
    .mpls index files that would flood the unknown bucket with noise about a
    fault already reported - and its .m2ts would be counted as a healthy
    payload, which is worse."""
    media = tmp_path / "media"
    disc = media / "Movies" / "T (1999)" / "BDMV"
    _write(disc / "STREAM" / "00001.m2ts", 8 * KIB)
    _write(disc / "CLIPINF" / "00001.clpi", 64)
    r = _run(code, tmp_path)
    assert r.returncode == 2
    line = _stage_line(r)
    assert line.startswith("STAGE=container-disc-dir")
    assert "findings=1" in line, "the pruned tree leaked findings: " + line
    assert "m2ts" not in line and "clpi" not in line, line
    assert "payloads=0" in line, "a disc payload was counted as healthy: " + line


def test_known_sidecars_and_dotfiles_are_ignored(code, tmp_path):
    """A healthy library is FULL of non-video files. A canary that reds on
    .plexmatch gets muted in a day. splitext reports NO extension for a
    leading-dot name, so the dotfile arm is the only thing that classifies the
    38 of them measured live."""
    media = tmp_path / "media"
    d = media / "Movies" / "T (1999)"
    _write(d / "T (1999).mkv", 4 * KIB)
    _write(d / "T (1999).srt", 200)
    _write(d / "T (1999).nfo", 200)
    _write(d / ".plexmatch", 40)
    r = _run(code, tmp_path)
    assert r.returncode == 0, r.stderr
    assert GREEN in r.stdout
    assert "payloads=1 sidecars=3" in r.stdout, r.stdout


def test_a_staging_tmp_beside_a_container_does_not_fire(code, tmp_path):
    """audio-disposition-janitor.py remuxes in place and Tdarr replaceOriginal
    File staging renames its temp to a *.tmp name mid-write (its own
    TmpVanishedError docstring, 2026-08-08). Both jobs share the 04:30 UTC slot
    with this walk, so firing on it would page nightly for healthy work."""
    media = tmp_path / "media"
    d = media / "Movies" / "T (1999)"
    _write(d / "T (1999).mkv", 4 * KIB)
    _write(d / "T (1999).mkv.tmp", 4 * KIB)
    r = _run(code, tmp_path)
    assert r.returncode == 0, r.stderr
    assert GREEN in r.stdout
    # NAMED, not swallowed: a staging file still there tomorrow is visible on a
    # green line.
    assert "staging=.mkv.tmp:1" in r.stdout, r.stdout


def test_a_bare_tmp_payload_still_fires(code, tmp_path):
    """Nothing here writes one, and payload-sized bytes with no evidence of
    their container are the anti-enumeration-gap case, not staging."""
    media = tmp_path / "media"
    _write(media / "Movies" / "T (1999)" / "T (1999).mkv", 4 * KIB)
    _write(media / "Movies" / "T (1999)" / "payload.tmp", 4 * KIB)
    r = _run(code, tmp_path)
    assert r.returncode == 2
    assert _stage_line(r).startswith("STAGE=container-unknown-payload")


def test_a_small_unknown_is_named_not_fired(code, tmp_path):
    """Under the payload floor it cannot be a feature or an episode. Named so
    the whitelist gets extended deliberately rather than by a red at 04:30."""
    media = tmp_path / "media"
    _write(media / "Movies" / "T (1999)" / "T (1999).mkv", 4 * KIB)
    _write(media / "Movies" / "T (1999)" / "T (1999).sfv", 60)
    r = _run(code, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "unlisted_sidecars=.sfv:1" in r.stdout


# --- the 200-char message budget ---------------------------------------------

def test_the_summary_survives_the_cli_200_char_cut(code, tmp_path):
    """lib/cli.py stores stderr[:200] and Kuma shows that. It is a hard cut, so
    counts must be on the surviving side of it: a truncated path list still
    names the first offender, a truncated summary names nothing."""
    media = tmp_path / "media"
    d = media / "Movies" / "A Very Long Movie Title For Budget Testing (1999)"
    _write(d / "A Very Long Movie Title For Budget Testing (1999).mkv", 4 * KIB)
    for n in range(3):
        _write(d / ("A Very Long Movie Title For Budget Testing part%d.iso" % n),
               4 * KIB)
    line = _stage_line(_run(code, tmp_path))
    assert line.index("scanned=") < line.index("paths="), line
    assert "scanned=" in line[:200], (
        "the summary fell off the end of the Kuma message: " + line[:200])


def test_max_named_defaults_to_two_and_paths_are_cut(code, tmp_path):
    """Three findings, two names. The third could never be displayed inside the
    budget, and pretending otherwise is how MAX_NAMED=3 shipped."""
    media = tmp_path / "media"
    d = media / "Movies" / "A Very Long Movie Title For Budget Testing (1999)"
    _write(d / "A Very Long Movie Title For Budget Testing (1999).mkv", 4 * KIB)
    for n in range(3):
        _write(d / ("A Very Long Movie Title For Budget Testing part%d.iso" % n),
               4 * KIB)
    line = _stage_line(_run(code, tmp_path))
    assert "findings=3" in line
    paths = line.split("paths=", 1)[1]
    assert paths.count(";") == 1, "expected 2 named paths, got: " + paths
    for p in paths.split(";"):
        assert len(p) <= 46, "path not cut to 45 chars: " + p
    assert "~" in paths, "a cut path must be marked, else it reads as real"


# --- the blind timer still trips ---------------------------------------------

def test_the_blind_streak_trips_after_the_budget(code, tmp_path):
    """The clock is only worth splitting if it still fires. An old `since`
    stamp with nothing to examine must red as container-blind."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "vacuity.json").write_text(
        json.dumps({"since": 1, "reason": "empty-library"}), encoding="utf-8")
    r = _run(code, tmp_path, media_root=tmp_path / "gone", state_dir=state)
    assert r.returncode == 2, r.stdout + r.stderr
    assert _stage_line(r).startswith("STAGE=container-blind")


def test_findings_beat_vacuity(code, tmp_path):
    """A real ISO in a readable root is a red even when a sibling root could not
    be walked at all. A missing root does not make a real disc image less
    real."""
    media = tmp_path / "media"
    _write(media / "Movies" / "T (1999)" / "T (1999).iso", 4 * KIB)
    r = _run(code, tmp_path, roots="Movies|Anime")
    assert r.returncode == 2
    assert _stage_line(r).startswith("STAGE=container-disc-image")


# --- the shipped defaults ----------------------------------------------------

def test_the_shipped_default_for_max_named_is_two(code):
    """Read from the source, because the env override above would mask a
    regression in the default the box actually runs with."""
    assert re.search(r'env\("LCS_MAX_NAMED",\s*"2"\)', code), (
        "MAX_NAMED default is not 2; a one-finding STAGE line already measured "
        "233 chars against a 200-char cut")


def test_the_timer_pins_utc():
    """Box TZ is Europe/Amsterdam. A bare OnCalendar fires at 02:30 UTC in CEST
    while three documents claim 04:30 UTC."""
    unit = (REPO / "scripts" / "maint" / "systemd"
            / "manitoba-maint-canary-library-container-sanity.timer")
    text = unit.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 04:30:00 UTC" in text, text


def test_the_installer_gates_the_first_enable():
    """A monitor enabled into a known red gets muted within the week. The
    installer must run it once by hand first - and must NOT be able to disable
    an already-armed timer just because today is red."""
    text = (REPO / "scripts" / "configure"
            / "240-maintenance-install.sh").read_text(encoding="utf-8")
    gate = text.split("FIRST-RUN GATE", 1)
    assert len(gate) == 2, "the first-run gate is gone from the installer"
    block = gate[1].split("# quota:", 1)[0]
    assert "is-enabled" in block, "the gate can disarm a live monitor"
    assert "bash ~/scripts/canaries/library-container-sanity.sh" in block
    assert "timer left DISABLED" in block


# --- 2026-08-27: the ACTUAL janitor temp shape (council re-entry L-55) -------

def test_the_dispfix_janitor_temp_does_not_fire(code, tmp_path):
    """audio-disposition-janitor.py stages as '.<stem>.dispfix.tmp' — leading
    dot, NO media extension, deliberately (the 2026-08-24 tdarr ghost fix).
    The playable-stem *.tmp exemption above never matched it, so a
    payload-sized in-flight remux redded this canary — and both units share
    the 04:30 UTC slot, so the collision is nightly-shaped. Triple-reproduced
    by the 2026-08-27 council re-entry."""
    media = tmp_path / "media"
    d = media / "Movies" / "T (1999)"
    _write(d / "T (1999).mkv", 4 * KIB)
    _write(d / ".T (1999).dispfix.tmp", 128 * 1024 * KIB)
    r = _run(code, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert GREEN in r.stdout
    assert "staging=.dispfix.tmp:1" in r.stdout, r.stdout


def test_a_non_hidden_dispfix_tmp_still_fires(code, tmp_path):
    """The exemption is the PRODUCER SIGNATURE, not the suffix: the janitor
    always writes a leading dot. A visible payload-sized dispfix.tmp is not
    its work and stays a finding."""
    media = tmp_path / "media"
    _write(media / "Movies" / "T (1999)" / "T (1999).mkv", 4 * KIB)
    _write(media / "Movies" / "T (1999)" / "payload.dispfix.tmp", 128 * 1024 * KIB)
    r = _run(code, tmp_path)
    assert r.returncode == 2
    assert _stage_line(r).startswith("STAGE=container-unknown-payload")
