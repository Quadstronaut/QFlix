"""tdarr-transcode-stall canary, executed against fixture state rather than grepped.

WHY THIS CANARY EXISTS, AND THEREFORE WHAT THESE TESTS HAVE TO PROVE
--------------------------------------------------------------------
On 2026-08-23 every Tdarr transcode worker died the moment it was handed a job:

    [FATAL] Tdarr_Node - Error: EACCES: permission denied, mkdir
    '/tdarr-workDir-node-YjouEnw6d-worker-lame-loris-ts-1787514583085'
    Worker lame-loris exited with code 1 and signal null
    Worker lame-loris disconnected. Pruning.

Three libraries carried cache:"", which Tdarr concatenated into an absolute path
at the filesystem root; a rootless slot cannot mkdir there. The node pruned the
worker and never retried. Transcoding was 100% dead with a backlog in front of
it, and the fleet sat at 76/76 green -- because a file whose worker DIES never
reaches TranscodeDecisionMaker=Error, it stays Queued, and every other Tdarr
surface (unit state, node registration, health checks, worker cap) was
genuinely fine.

So the two tests that carry this file are:

  test_backlog_with_idle_workers_and_no_progress_reds
      the incident itself -- backlog, capacity, nothing running, nothing
      finishing. Must exit 1.

  test_busy_worker_keeps_it_green_however_long_the_job
      the negative control, and the reason this canary is safe to arm at all.
      A 5 GB feature can hold a worker for hours while `completed` does not
      move. That is healthy. If a busy worker did not keep it green, this
      canary would red on every long transcode and get muted inside a week --
      at which point it protects nothing.

Everything else here guards the could-not-assert paths, because
empty-because-broken must never read as empty-because-clean.

The canary is a shell wrapper around an embedded python heredoc that runs on
the box; grepping the shell text asserts nothing about behaviour, so the heredoc
is lifted and EXECUTED against fixture state -- same technique as
test_tdarr_canary_ghost_records.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TS = REPO / "scripts" / "canaries" / "tdarr-transcode-stall.sh"

NOW = 1787500000          # fixed clock
HOUR = 3600


# ---------------------------------------------------------------------------
# Extractor (pinned on a symbol, so a renamed delimiter fails here rather than
# silently making every test below vacuous)
# ---------------------------------------------------------------------------

def _ts_body() -> str:
    src = TS.read_text(encoding="utf-8")
    opener = "python3 - <<PYEOF\n"
    start = src.index(opener) + len(opener)
    body = src[start:src.index("\nPYEOF", start)]
    assert "tdarr-transcode-stalled" in body and "is_ghost" in body, \
        "extracted the wrong block"
    return body


@pytest.fixture(scope="module")
def ts_code():
    return _ts_body()


def test_the_embedded_body_is_found(ts_code):
    assert "WORKERS-ARE-NOT-PICKING-UP-WORK" in ts_code
    assert len(ts_code.splitlines()) > 80


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _nodes(*, transcodecpu=2, busy_transcode=0, paused=False,
           busy_healthcheck=0) -> str:
    workers = {}
    for i in range(busy_transcode):
        workers["t%d" % i] = {"workerType": "transcodecpu", "percentage": 3.2,
                              "file": "/home/quadstronaut/media/Movies/X.mkv"}
    for i in range(busy_healthcheck):
        workers["h%d" % i] = {"workerType": "healthcheckcpu", "percentage": 50}
    return json.dumps({"nodeXYZ": {
        "nodeName": "manitoba-local",
        "nodePaused": paused,
        "workerLimits": {"transcodecpu": transcodecpu, "transcodegpu": 0,
                         "healthcheckcpu": 1, "healthcheckgpu": 0},
        "workers": workers,
    }})


def _ghost(media: Path, name: str) -> str:
    """A path inside a REAL, readable directory that does not contain the file.

    POSIX separators because the canary takes basenames with rsplit("/", 1);
    a real directory because a ghost must be PROVEN absent, not merely
    unlookable (see is_ghost)."""
    return str(media).replace(os.sep, "/") + "/" + name


def _unreachable(tmp_path: Path, name: str) -> str:
    """A path whose PARENT does not exist -- the unmounted media tree."""
    return str(tmp_path / "gone-media").replace(os.sep, "/") + "/" + name


def _db(tmp_path: Path, *, queued=0, completed=0, ghosts=0, unreachable=0):
    db = tmp_path / "DB2"
    (db / "FileJSONDB").mkdir(parents=True, exist_ok=True)
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)

    def write(rid, doc):
        (db / "FileJSONDB" / (rid + ".json")).write_text(
            json.dumps(doc), encoding="utf-8")

    for i in range(completed):
        f = media / ("done%02d.mkv" % i)
        f.write_bytes(b"x")
        write("done%02d" % i, {"_id": str(f), "file": str(f),
                               "TranscodeDecisionMaker": "Transcode success"})
    for i in range(queued):
        f = media / ("pending%02d.mkv" % i)
        f.write_bytes(b"x")
        write("pending%02d" % i, {"_id": str(f), "file": str(f),
                                  "TranscodeDecisionMaker": "Queued"})
    for i in range(ghosts):
        g = _ghost(media, ".Gone %d.dispfix.tmp" % i)
        write("ghost%d" % i, {"_id": g, "file": g,
                              "TranscodeDecisionMaker": "Queued"})
    for i in range(unreachable):
        u = _unreachable(tmp_path, "Pending %d.mkv" % i)
        write("unreach%d" % i, {"_id": u, "file": u,
                                "TranscodeDecisionMaker": "Queued"})
    return db


def _state(tmp_path: Path, *, completed: int, age_h: float) -> Path:
    p = tmp_path / "ts-state.json"
    p.write_text(json.dumps({"completed": completed,
                             "last_progress_ts": NOW - age_h * HOUR}),
                 encoding="utf-8")
    return p


def _run(code: str, tmp_path: Path, *, db: Path, nodes: str, state: Path,
         stall_hours="3", port="42018"):
    script = tmp_path / "ts_body.py"
    script.write_text(code, encoding="utf-8")
    env = dict(os.environ)
    env.update({"TS_NOW": str(NOW), "TS_STALL_HOURS": stall_hours,
                "TS_DB": str(db), "TS_STATE": str(state),
                "TS_NODES": nodes, "TS_PORT": port})
    return subprocess.run([sys.executable, str(script)], env=env,
                          capture_output=True, text=True, timeout=120)


def _stage(result) -> str:
    for line in result.stderr.splitlines():
        if line.startswith("STAGE="):
            return line
    raise AssertionError("no STAGE= line in stderr:\n" + result.stderr)


# ---------------------------------------------------------------------------
# The incident, and the control that makes it safe to arm
# ---------------------------------------------------------------------------

def test_backlog_with_idle_workers_and_no_progress_reds(ts_code, tmp_path):
    """THE INCIDENT. Work queued, capacity available, nothing running, nothing
    finishing for longer than the window. Every other Tdarr surface read green
    through exactly this state."""
    db = _db(tmp_path, queued=2, completed=79)
    r = _run(ts_code, tmp_path, db=db, nodes=_nodes(busy_transcode=0),
             state=_state(tmp_path, completed=79, age_h=6))
    assert r.returncode == 1, r.stdout + r.stderr
    stage = _stage(r)
    assert "STAGE=tdarr-transcode-stalled" in stage, stage
    assert "backlog=2" in stage, stage
    assert "2-free-worker-slot(s)" in stage, stage
    assert "WORKERS-ARE-NOT-PICKING-UP-WORK" in stage, stage


def test_busy_worker_keeps_it_green_however_long_the_job(ts_code, tmp_path):
    """THE NEGATIVE CONTROL. Identical fixture, identical stale clock -- but a
    transcode worker is in flight. A feature-length encode legitimately holds a
    worker for hours without moving `completed`, and a canary that reds on that
    would be muted within a week and protect nothing. In-flight work is proof
    of life."""
    db = _db(tmp_path, queued=2, completed=79)
    r = _run(ts_code, tmp_path, db=db, nodes=_nodes(busy_transcode=1),
             state=_state(tmp_path, completed=79, age_h=6))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "tdarr-transcode-stall-working" in r.stdout, r.stdout
    assert "busy=1/2" in r.stdout, r.stdout


def test_progress_since_last_run_clears_the_clock(ts_code, tmp_path):
    """A completion between runs resets the stall clock, so a slow-but-moving
    pipeline never accumulates its way into a red."""
    db = _db(tmp_path, queued=2, completed=80)
    r = _run(ts_code, tmp_path, db=db, nodes=_nodes(busy_transcode=0),
             state=_state(tmp_path, completed=79, age_h=6))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "idle=0.0h" in r.stdout, r.stdout


def test_idle_with_empty_backlog_is_healthy(ts_code, tmp_path):
    """Nothing to do is not a stall. This is the steady state most of the day
    and must never page."""
    db = _db(tmp_path, queued=0, completed=79)
    r = _run(ts_code, tmp_path, db=db, nodes=_nodes(busy_transcode=0),
             state=_state(tmp_path, completed=79, age_h=99))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "backlog-empty" in r.stdout, r.stdout


def test_inside_the_window_is_not_yet_a_stall(ts_code, tmp_path):
    """The server stages on its own scan cycle, so a short idle-with-backlog
    gap is a scheduling artifact, not a fault."""
    db = _db(tmp_path, queued=2, completed=79)
    r = _run(ts_code, tmp_path, db=db, nodes=_nodes(busy_transcode=0),
             state=_state(tmp_path, completed=79, age_h=1))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "awaiting-pickup" in r.stdout, r.stdout


# ---------------------------------------------------------------------------
# Ghost handling -- same law as tdarr-healthcheck: suppression must be EARNED
# ---------------------------------------------------------------------------

def test_ghost_backlog_is_not_a_stall_and_is_named(ts_code, tmp_path):
    """A Queued record whose file is gone can never be worked by anyone. It
    must not hold the backlog above zero forever -- but it must still be named,
    or the operator loses the only signal a janitor is minting ghosts."""
    db = _db(tmp_path, queued=0, completed=79, ghosts=1)
    r = _run(ts_code, tmp_path, db=db, nodes=_nodes(busy_transcode=0),
             state=_state(tmp_path, completed=79, age_h=6))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "backlog-empty" in r.stdout, r.stdout
    assert "-ghosts=1-first=.Gone 0.dispfix.tmp" in r.stdout, r.stdout


def test_unreachable_media_tree_still_reds(ts_code, tmp_path):
    """THE SECOND NEGATIVE CONTROL. Unmount the media tree while the pipeline is
    genuinely stalled and every queued record answers "absent". A suppression
    keyed on absence alone would empty the backlog and turn a dead pipeline
    green -- strictly worse than no canary. A ghost requires a READABLE
    directory that does not contain the file; anything unlookable stays
    counted, and the red says which."""
    db = _db(tmp_path, queued=0, completed=79, unreachable=30)
    r = _run(ts_code, tmp_path, db=db, nodes=_nodes(busy_transcode=0),
             state=_state(tmp_path, completed=79, age_h=6))
    assert r.returncode == 1, r.stdout + r.stderr
    stage = _stage(r)
    assert "backlog=30" in stage, stage
    assert "-ghosts=" not in stage, stage
    assert "-unreachable=30-MEDIA-TREE-NOT-READABLE" in stage, stage


# ---------------------------------------------------------------------------
# Could-not-assert paths: empty-because-broken must never read as clean
# ---------------------------------------------------------------------------

def test_paused_node_is_an_operator_choice_not_a_fault(ts_code, tmp_path):
    db = _db(tmp_path, queued=2, completed=79)
    r = _run(ts_code, tmp_path, db=db, nodes=_nodes(paused=True),
             state=_state(tmp_path, completed=79, age_h=6))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all-nodes-paused-operator-choice" in r.stdout, r.stdout


def test_zero_transcode_capacity_cannot_be_judged(ts_code, tmp_path):
    """A cap of 0 means nobody asked for transcoding. Reporting a stall against
    a throttle the operator set would be the canary arguing with policy."""
    db = _db(tmp_path, queued=2, completed=79)
    r = _run(ts_code, tmp_path, db=db, nodes=_nodes(transcodecpu=0),
             state=_state(tmp_path, completed=79, age_h=6))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "zero-transcode-worker-capacity" in r.stdout, r.stdout


def test_server_unreachable_is_exit_2(ts_code, tmp_path):
    db = _db(tmp_path, queued=2, completed=79)
    r = _run(ts_code, tmp_path, db=db, nodes="",
             state=_state(tmp_path, completed=79, age_h=6))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "STAGE=tdarr-ts-server-unreachable" in _stage(r)


def test_zero_registered_nodes_is_exit_2(ts_code, tmp_path):
    db = _db(tmp_path, queued=2, completed=79)
    r = _run(ts_code, tmp_path, db=db, nodes="{}",
             state=_state(tmp_path, completed=79, age_h=6))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "STAGE=tdarr-ts-no-nodes" in _stage(r)


def test_empty_filedb_is_exit_2_not_a_clean_pass(ts_code, tmp_path):
    """This Tdarr holds ~465 records. An empty scan means the DB moved or the
    glob broke, and that must never read as 'nothing queued, all good'."""
    db = tmp_path / "DB2"
    (db / "FileJSONDB").mkdir(parents=True)
    r = _run(ts_code, tmp_path, db=db, nodes=_nodes(),
             state=_state(tmp_path, completed=0, age_h=6))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "STAGE=tdarr-ts-filedb-empty" in _stage(r)


def test_state_file_is_written_so_the_clock_survives_restarts(ts_code, tmp_path):
    """The stall is measured from the last real completion, not from boot --
    otherwise a restart loop hides an indefinite stall."""
    db = _db(tmp_path, queued=1, completed=5)
    state = tmp_path / "fresh-state.json"
    r = _run(ts_code, tmp_path, db=db, nodes=_nodes(), state=state)
    assert r.returncode == 0, r.stdout + r.stderr
    written = json.loads(state.read_text(encoding="utf-8"))
    assert written["completed"] == 5
    assert written["queued"] == 1
    assert written["last_progress_ts"] == NOW
