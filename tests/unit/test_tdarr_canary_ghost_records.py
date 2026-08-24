"""CANARY half of the Tdarr ghost-record fix, executed rather than grepped.

WHY THIS FILE EXISTS
--------------------
A FileJSONDB record whose file no longer exists on disk is a STALE RECORD, not
a live judgement input. Both tdarr canaries treated one as live, and that is
the load-bearing defect -- it turns any future janitor ghost into a PERMANENT
false red, regardless of which janitor mints it:

  tdarr-healthcheck.sh  -- a ghost sits at HealthCheck=Queued. Nothing can open
    it, so it can never complete. `completed` never rises and `queued > 0` stays
    true, so predicate 3 (`queued > 0 and node == active and stalled > 6h`)
    reports PIPELINE-WEDGED forever on a pipeline that is fine.
  tdarr-transcode-error.sh -- the same record carries the terminal
    TranscodeDecisionMaker=Transcode error, which Tdarr never retries and never
    rewrites, so it inflates the parked population forever. Live on 2026-08-23
    it said 3 parked when only 2 were real.

The source half (why a janitor temp got indexed at all) is pinned in
tests/unit/test_janitor_temp_names_tdarr_ghost.py. This file pins the durable
half: the canaries must EXCLUDE a vanished-file record from the verdict and
NAME it in the message.

HOW THESE TESTS RUN THE REAL THING
Both canaries are shell wrappers around an embedded python heredoc that runs on
the box. Grepping the shell text asserts nothing about behaviour, so the heredoc
is lifted out and EXECUTED against fixture DBs -- the technique
test_library_container_sanity.py and test_hardlink_vacuity_clock.py already use.
Each extractor is pinned on a known symbol, so a renamed delimiter fails here
instead of silently testing an empty string.

THE TESTS THAT MATTER MOST ARE THE NEGATIVE ONES.
Suppressing a record is only correct if it suppresses EXACTLY the vanished ones.
`test_hc_present_file_still_reports_a_wedge` re-points the identical fixture at
a file that DOES exist and demands the red come back, and
`test_hc_ghost_named_on_the_fail_path_too` proves the suppression stays visible
when the canary is red. Without those two, this patch would be indistinguishable
from having simply disabled the wedge detector.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HC = REPO / "scripts" / "canaries" / "tdarr-healthcheck.sh"
TE = REPO / "scripts" / "canaries" / "tdarr-transcode-error.sh"

NOW = 1787500000          # fixed clock, 2026-08-23-ish
HOUR = 3600

# A ghost path is POSIX-separated on purpose: both canaries take the basename
# with `rsplit("/", 1)`, which is correct on the seedbox and wrong for a native
# Windows path -- a backslash fixture would exercise a basename extractor that
# never runs in production.
#
# The DIRECTORY, however, must really exist. Both canaries only classify a
# record as a ghost once they have READ its directory and not found the file in
# it, because "the file is absent" and "I could not look" are the same
# os.path.exists() answer and demand opposite verdicts. A fixture rooted at a
# directory that does not exist would silently exercise the unreachable branch
# while claiming to test the ghost branch -- and would then pass no matter what
# the ghost branch did.
def _ghost(media: Path, name: str) -> str:
    return str(media).replace(os.sep, "/") + "/" + name


# A path whose PARENT does not exist either. This is the unmounted /
# path-remapped / permission-lost media tree, and it must never be suppressed.
def _unreachable(tmp_path: Path, name: str) -> str:
    return str(tmp_path / "gone-media").replace(os.sep, "/") + "/" + name


def test_ghost_fixture_shape_is_what_the_canaries_classify(tmp_path):
    """Guards the fixture on both sides: the directory is readable (so the
    canary reaches the ghost branch) and the file is not in it (so there is a
    ghost to find). Break either half and every ghost test below quietly
    becomes a test of something else."""
    media = tmp_path / "media"
    media.mkdir()
    g = _ghost(media, "anything.mkv")
    assert os.path.isdir(os.path.dirname(g))
    assert os.access(os.path.dirname(g), os.R_OK | os.X_OK)
    assert not os.path.exists(g)
    u = _unreachable(tmp_path, "anything.mkv")
    assert not os.path.isdir(os.path.dirname(u))


# ---------------------------------------------------------------------------
# Extractors (guard the guard: pinned on a symbol, not just on a delimiter)
# ---------------------------------------------------------------------------

def _hc_body() -> str:
    src = HC.read_text(encoding="utf-8")
    opener = "python3 - <<PYEOF\n"
    start = src.index(opener) + len(opener)
    body = src[start:src.index("\nPYEOF", start)]
    assert "tdarr-hc-stalled" in body and "ghosts" in body, "extracted the wrong block"
    return body


def _te_body() -> str:
    src = TE.read_text(encoding="utf-8")
    opener = "python3 - <<PY\n"
    start = src.index(opener) + len(opener)
    body = src[start:src.index("\nPY'", start)]
    assert "tdarr-transcode-error-parked" in body and "ghosts" in body, \
        "extracted the wrong block"
    return body


@pytest.fixture(scope="module")
def hc_code():
    return _hc_body()


@pytest.fixture(scope="module")
def te_code():
    return _te_body()


def test_the_embedded_bodies_are_found(hc_code, te_code):
    """An extractor that silently returns nothing makes every test below
    vacuously green -- the same vacuity defect, one level up."""
    assert "PIPELINE-WEDGED" in hc_code and len(hc_code.splitlines()) > 100
    assert "0 parked beyond" in te_code and len(te_code.splitlines()) > 30


# ---------------------------------------------------------------------------
# Fixture DB builders
# ---------------------------------------------------------------------------

def _fake_home(tmp_path: Path) -> Path:
    """A HOME with the node ffmpeg present, so the healthcheck canary's
    predicate-1 engine-sanity check resolves instead of failing first."""
    home = tmp_path / "home"
    ff = home / ".apps" / "tdarr" / "Tdarr_Node" / "node_modules" / "ffmpeg-static"
    ff.mkdir(parents=True, exist_ok=True)
    binary = ff / "ffmpeg"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(binary, 0o755)
    return home


def _db(tmp_path: Path, home: Path) -> Path:
    """The DB2 tree under the fake HOME. tdarr-transcode-error.sh hardcodes
    ~/.apps/tdarr/server/Tdarr/DB2/FileJSONDB (no env override), so the fixture
    must live exactly there."""
    db = home / ".apps" / "tdarr" / "server" / "Tdarr" / "DB2"
    (db / "FileJSONDB").mkdir(parents=True, exist_ok=True)
    (db / "LibrarySettingsJSONDB").mkdir(parents=True, exist_ok=True)
    (db / "LibrarySettingsJSONDB" / "movies.json").write_text(json.dumps({
        "name": "Movies", "processHealthChecks": True,
        "ffmpegscan": True, "handbrakescan": False,
    }), encoding="utf-8")
    return db


def _record(db: Path, rid: str, src: str, *, health=None, transcode=None,
            age_h: float = 0.0, hc_age_h=None) -> Path:
    doc = {"_id": src, "file": src}
    if hc_age_h is not None:
        # lastHealthCheckDate is ms-epoch and is the healthcheck canary's
        # progress clock -- see the PROGRESS block in tdarr-healthcheck.sh.
        doc["lastHealthCheckDate"] = int((NOW - hc_age_h * HOUR) * 1000)
    if health:
        doc["HealthCheck"] = health
    if transcode:
        doc["TranscodeDecisionMaker"] = transcode
    p = db / "FileJSONDB" / (rid + ".json")
    p.write_text(json.dumps(doc), encoding="utf-8")
    mt = NOW - age_h * HOUR
    os.utime(p, (mt, mt))
    return p


def _run(code: str, home: Path, env: dict):
    script = home.parent / "canary_body.py"
    script.write_text(code, encoding="utf-8")
    full = dict(os.environ)
    full["HOME"] = str(home)
    full["USERPROFILE"] = str(home)          # expanduser on Windows
    full.update(env)
    return subprocess.run([sys.executable, str(script)], env=full,
                          capture_output=True, text=True, timeout=120)


def _stage(result) -> str:
    for line in result.stderr.splitlines():
        if line.startswith("STAGE="):
            return line
    raise AssertionError("no STAGE= line in stderr:\n" + result.stderr)


# ===========================================================================
# tdarr-healthcheck.sh
# ===========================================================================

def _hc_fixture(tmp_path, *, queued_file_exists: bool, extra_ghosts: int = 0):
    """25 completed Success records plus ONE Queued record. The Queued record
    either points at a real file (a genuine backlog -> wedge) or at a path that
    does not exist (a ghost -> must be excluded). Progress state is stale by
    12h, well past the 6h STALL_HOURS, so the stall predicate is armed."""
    home = _fake_home(tmp_path)
    db = _db(tmp_path, home)
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)

    for i in range(25):
        real = media / ("ok%02d.mkv" % i)
        real.write_bytes(b"x")
        _record(db, "ok%02d" % i, str(real), health="Success",
                transcode="Not required")

    if queued_file_exists:
        real = media / "Pending (2024).mkv"
        real.write_bytes(b"x")
        target = str(real)
    else:
        target = _ghost(media, ".Interstellar (2014) Bluray-1080p Proper.dispfix.tmp")
    _record(db, "queued", target, health="Queued",
            transcode="Transcode error")

    for g in range(extra_ghosts):
        _record(db, "ghost%d" % g, _ghost(media, ".Gone %d.dispfix.tmp" % g),
                health="Queued")

    state = tmp_path / "hc-state.json"
    state.write_text(json.dumps({"completed": 25,
                                 "last_progress_ts": NOW - 12 * HOUR}),
                     encoding="utf-8")
    env = {"HC_WARN": "20", "HC_FAIL": "50", "HC_MIN": "20",
           "HC_DB": str(db), "HC_STALL_HOURS": "6", "HC_STATE": str(state),
           "HC_SERVER": "active", "HC_NODE": "active", "HC_NOW": str(NOW)}
    return home, env


def test_hc_ghost_queued_record_is_not_a_wedge(hc_code, tmp_path):
    """THE BUG. One Queued record for a vanished janitor temp used to hold
    `queued > 0` true forever, so the stall predicate reported a wedged
    pipeline permanently. It must now pass, with queued counted as zero."""
    home, env = _hc_fixture(tmp_path, queued_file_exists=False)
    r = _run(hc_code, home, env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("PASS:"), r.stdout
    assert "queued=0" in r.stdout, r.stdout


def test_hc_ghost_is_named_not_silently_dropped(hc_code, tmp_path):
    """Suppressed from the verdict, visible in the message -- otherwise the
    operator loses the only signal that a janitor is minting ghosts."""
    home, env = _hc_fixture(tmp_path, queued_file_exists=False)
    r = _run(hc_code, home, env)
    assert "-ghosts=1-first=.Interstellar (2014) Bluray-1080p Proper.dispfix.tmp" \
        in r.stdout, r.stdout


def test_hc_present_file_still_reports_a_wedge(hc_code, tmp_path):
    """THE NEGATIVE CONTROL, and the most important test here. Identical
    fixture, but the Queued record points at a file that EXISTS. The red must
    come back -- otherwise the patch did not narrow the predicate, it disabled
    the wedge detector."""
    home, env = _hc_fixture(tmp_path, queued_file_exists=True)
    r = _run(hc_code, home, env)
    assert r.returncode == 1, r.stdout + r.stderr
    stage = _stage(r)
    assert "STAGE=tdarr-hc-stalled" in stage
    assert "PIPELINE-WEDGED" in stage
    assert "queued=1" in stage
    assert "-ghosts=" not in stage          # nothing was suppressed


def test_hc_recent_completion_clears_the_stall_even_when_the_count_FELL(hc_code, tmp_path):
    """THE 2026-08-24 FALSE RED, pinned.

    `completed` counts records not in Queued, which is a POPULATION number, not
    a progress one. Re-encoding a file sends its HealthCheck back to Queued, so
    during a re-encode wave the count goes DOWN -- and the old predicate only
    refreshed its clock on an INCREASE. Requeueing 28 files for the
    universal-playability widening drove it 464 -> 461 -> 459 and this canary
    reported PIPELINE-WEDGED at 24.9h while the newest health check on disk was
    thirty-six seconds old.

    Here the state file claims 500 completed 12h ago (so the count has FALLEN
    and the old clock is long stale) but a record carries a fresh
    lastHealthCheckDate. Checks are finishing; there is no wedge."""
    home, env = _hc_fixture(tmp_path, queued_file_exists=True)
    db = Path(env["HC_DB"])
    for f in (db / "FileJSONDB").glob("ok*.json"):
        doc = json.loads(f.read_text(encoding="utf-8"))
        doc["lastHealthCheckDate"] = int((NOW - 0.1 * HOUR) * 1000)
        f.write_text(json.dumps(doc), encoding="utf-8")
    state = Path(env["HC_STATE"])
    state.write_text(json.dumps({"completed": 500,
                                 "last_progress_ts": NOW - 12 * HOUR}),
                     encoding="utf-8")
    r = _run(hc_code, home, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "idle=0.1h" in r.stdout, r.stdout


def test_hc_stale_completion_stamps_still_red(hc_code, tmp_path):
    """NEGATIVE CONTROL. Same fixture, but every completion stamp is old. A real
    wedge must still be caught -- the new clock must not be a way of never
    reding."""
    home, env = _hc_fixture(tmp_path, queued_file_exists=True)
    db = Path(env["HC_DB"])
    for f in (db / "FileJSONDB").glob("ok*.json"):
        doc = json.loads(f.read_text(encoding="utf-8"))
        doc["lastHealthCheckDate"] = int((NOW - 30 * HOUR) * 1000)
        f.write_text(json.dumps(doc), encoding="utf-8")
    r = _run(hc_code, home, env)
    assert r.returncode == 1, r.stdout + r.stderr
    stage = _stage(r)
    assert "STAGE=tdarr-hc-stalled" in stage, stage
    assert "COMPLETED-in-30.0h" in stage, stage


def test_hc_falls_back_to_the_count_clock_when_no_stamp_exists(hc_code, tmp_path):
    """A fresh install, or a Tdarr that stops writing lastHealthCheckDate, must
    degrade to the old predicate rather than lose the wedge detector -- and must
    SAY it is doing so, because a clock that silently changes meaning is worse
    than either clock."""
    home, env = _hc_fixture(tmp_path, queued_file_exists=True)
    r = _run(hc_code, home, env)          # fixture writes no stamps at all
    assert r.returncode == 1, r.stdout + r.stderr
    stage = _stage(r)
    assert "clock=count-fallback-no-lastHealthCheckDate" in stage, stage


def test_hc_future_stamp_cannot_manufacture_negative_idle(hc_code, tmp_path):
    """Clock skew between the box and whatever wrote the record must not produce
    a negative age (which would format absurdly and could underflow a
    comparison). Clamped to now."""
    home, env = _hc_fixture(tmp_path, queued_file_exists=True)
    db = Path(env["HC_DB"])
    f = next((db / "FileJSONDB").glob("ok*.json"))
    doc = json.loads(f.read_text(encoding="utf-8"))
    doc["lastHealthCheckDate"] = int((NOW + 5 * HOUR) * 1000)
    f.write_text(json.dumps(doc), encoding="utf-8")
    r = _run(hc_code, home, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "idle=0.0h" in r.stdout, r.stdout
    assert "idle=-" not in r.stdout, r.stdout


def test_hc_unreachable_media_tree_is_not_a_ghost(hc_code, tmp_path):
    """THE REFUTER'S SCENARIO, and the reason the suppression is not keyed on
    os.path.exists alone. Unmount the media tree -- or lose +x on a parent, or
    migrate the slot so the stored absolute paths stop resolving -- while the
    pipeline is genuinely wedged. Every Queued record then answers "absent", so
    an absence-only rule would collapse `queued` to 0 and hold this canary
    permanently GREEN on a dead pipeline: strictly worse than the false red it
    replaced. A ghost has to be PROVEN by reading the directory, so an
    unreadable tree stays counted and the wedge still reds."""
    home, env = _hc_fixture(tmp_path, queued_file_exists=True)
    db = Path(env["HC_DB"])
    (db / "FileJSONDB" / "queued.json").unlink()
    for i in range(30):
        _record(db, "unreach%d" % i,
                _unreachable(tmp_path, "Pending %d.mkv" % i), health="Queued")
    r = _run(hc_code, home, env)
    assert r.returncode == 1, r.stdout + r.stderr
    stage = _stage(r)
    assert "STAGE=tdarr-hc-stalled" in stage, stage
    assert "queued=30" in stage, stage
    assert "-ghosts=" not in stage, stage
    assert "-unreachable=30-MEDIA-TREE-NOT-READABLE" in stage, stage


def test_hc_ghost_named_on_the_fail_path_too(hc_code, tmp_path):
    """A real wedge AND a ghost at the same time: the wedge still reds, and the
    ghost is still named. libstr is interpolated into all four exit lines, so
    this is the assertion that the append reaches the failure branch."""
    home, env = _hc_fixture(tmp_path, queued_file_exists=True, extra_ghosts=1)
    r = _run(hc_code, home, env)
    assert r.returncode == 1, r.stdout + r.stderr
    stage = _stage(r)
    assert "STAGE=tdarr-hc-stalled" in stage
    assert "queued=1" in stage              # the ghost is NOT in the count
    assert "-ghosts=1-first=.Gone 0.dispfix.tmp" in stage, stage


def test_hc_clean_db_has_no_ghost_segment(hc_code, tmp_path):
    """No ghosts -> no `-ghosts=` noise on the message at all."""
    home, env = _hc_fixture(tmp_path, queued_file_exists=True)
    # Drop the queued record entirely: 25 completed, nothing pending.
    (Path(env["HC_DB"]) / "FileJSONDB" / "queued.json").unlink()
    r = _run(hc_code, home, env)
    assert r.returncode == 0, r.stderr
    assert "-ghosts=" not in r.stdout, r.stdout
    assert "queued=0" in r.stdout


def test_hc_records_with_no_healthcheck_key_are_untouched(hc_code, tmp_path):
    """The rewritten loop swapped `if state:` for an early `continue`; a record
    with no HealthCheck field must still be ignored rather than counted or
    ghost-classified."""
    home, env = _hc_fixture(tmp_path, queued_file_exists=True)
    db = Path(env["HC_DB"])
    gone = _ghost(tmp_path / "media", "nowhere.mkv")
    (db / "FileJSONDB" / "nohc.json").write_text(
        json.dumps({"_id": gone, "file": gone}), encoding="utf-8")
    r = _run(hc_code, home, env)
    assert "-ghosts=" not in (r.stdout + r.stderr)
    assert "completed=25" in _stage(r)


# ===========================================================================
# tdarr-transcode-error.sh
# ===========================================================================

def _te_fixture(tmp_path, *, real_parked: int, ghosts: int, fresh: int = 0):
    home = _fake_home(tmp_path)
    db = _db(tmp_path, home)
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)

    # A clean record so `total` is never zero (exit 2 is empty-because-broken).
    clean = media / "Clean.mkv"
    clean.write_bytes(b"x")
    _record(db, "clean", str(clean), transcode="Not required", age_h=1)

    for i in range(real_parked):
        real = media / ("Parked %d.mkv" % i)
        real.write_bytes(b"x")
        _record(db, "parked%d" % i, str(real), transcode="Transcode error",
                age_h=67.5 + i)
    for i in range(fresh):
        real = media / ("Fresh %d.mkv" % i)
        real.write_bytes(b"x")
        _record(db, "fresh%d" % i, str(real), transcode="Transcode error",
                age_h=2)
    for i in range(ghosts):
        _record(db, "ghost%d" % i, _ghost(media, ".Ghost %d.dispfix.tmp" % i),
                transcode="Transcode error", age_h=61.5)

    return home, {"GRACE_H": "48"}


def test_te_ghost_is_excluded_from_the_parked_count(te_code, tmp_path):
    """THE BUG, live on 2026-08-23: 3 records in the terminal state, 2 real.
    A vanished file cannot be un-parked by anyone, so it must not be counted."""
    home, env = _te_fixture(tmp_path, real_parked=2, ghosts=1)
    r = _run(te_code, home, env)
    assert r.returncode == 1, r.stdout + r.stderr
    stage = _stage(r)
    assert "msg=2-file(s)-terminal" in stage, stage
    assert "-ghosts=1-first=.Ghost 0.dispfix.tmp" in stage, stage


def test_te_ghost_only_population_is_green_and_named(te_code, tmp_path):
    """Every terminal record is a ghost -> nothing is actually parked. Green,
    but the ghost is still named on the PASS line."""
    home, env = _te_fixture(tmp_path, real_parked=0, ghosts=1)
    r = _run(te_code, home, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("PASS: tdarr-transcode-error"), r.stdout
    assert "-ghosts=1-first=.Ghost 0.dispfix.tmp" in r.stdout, r.stdout


def test_te_real_parked_file_still_reds(te_code, tmp_path):
    """NEGATIVE CONTROL. A parked file that EXISTS and is past grace must still
    red -- the patch must not have blunted the detector."""
    home, env = _te_fixture(tmp_path, real_parked=1, ghosts=0)
    r = _run(te_code, home, env)
    assert r.returncode == 1
    stage = _stage(r)
    assert "msg=1-file(s)-terminal" in stage
    assert "-ghosts=" not in stage


def test_te_unreachable_media_tree_is_not_a_ghost(te_code, tmp_path):
    """Same refuter scenario on the parked-population canary. If absence alone
    earned the suppression, an unreadable media tree would empty the terminal
    population and report 0 parked while a real backlog sat there."""
    home, env = _te_fixture(tmp_path, real_parked=0, ghosts=0)
    db = home / ".apps" / "tdarr" / "server" / "Tdarr" / "DB2"
    for i in range(30):
        _record(db, "unreach%d" % i,
                _unreachable(tmp_path, "Parked %d.mkv" % i),
                transcode="Transcode error", age_h=67.5)
    r = _run(te_code, home, env)
    assert r.returncode == 1, r.stdout + r.stderr
    stage = _stage(r)
    assert "msg=30-file(s)-terminal" in stage, stage
    assert "-ghosts=" not in stage, stage


def test_te_fresh_errors_still_within_grace_stay_green(te_code, tmp_path):
    """Unchanged behaviour: an error inside the 48h grace is the janitor's to
    fix and is counted as fresh, not parked."""
    home, env = _te_fixture(tmp_path, real_parked=0, ghosts=0, fresh=2)
    r = _run(te_code, home, env)
    assert r.returncode == 0, r.stderr
    assert "2 fresh error(s)" in r.stdout, r.stdout
    assert "-ghosts=" not in r.stdout


def test_te_empty_db_is_still_could_not_assert(te_code, tmp_path):
    """Exit 2 must survive the patch: empty-because-broken never reads as
    empty-because-clean."""
    home = _fake_home(tmp_path)
    _db(tmp_path, home)
    r = _run(te_code, home, {"GRACE_H": "48"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "STAGE=tdarr-filedb-empty" in _stage(r)
