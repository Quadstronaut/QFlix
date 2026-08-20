"""VIDEO_EXTS taxonomy + the present-vs-resolved split (fix 2026-08-19).

INCIDENT. Kuma monitor #90 (hardlink-integrity) was the only red monitor on the
board, reding every 30 minutes from 2026-08-19T20:01Z with
STAGE=qbit-no-completed -- a stage whose documented meaning is "qBit data dir
nuked / mount evaporated / downloads tree moved". Nothing was wrong. The
completed pool held exactly one torrent, a BDMV disc rip whose only
video-bearing file is BDMV/STREAM/00000.m2ts (19,307,427,840 bytes; the rest of
the tree is .bdmv/.mpls/.clpi index metadata). VIDEO_EXTS did not list .m2ts,
so the multi-file walk found no target, the torrent was skipped, `resolved`
stayed 0, and `resolved == 0` was wired straight to a red.

TWO defects, and this module guards both, because fixing only the first leaves
the canary one unseen container format away from repeating the whole incident:

  1. TAXONOMY. .m2ts and .ts are now listed. VIDEO_EXTS is ONE constant with
     TWO consumers -- the library index (which needs .m2ts to ever find an
     inode twin for a disc-shaped import) and torrent target resolution -- so
     an omission does not skip a check, it silently shrinks the sample. The
     structural tests below pin both the membership and the single-constant
     coupling, so a future "tidy up the extension list" edit fails here rather
     than on the pager.

  2. PREDICATE. `present` (content path exists on disk) and `resolved`
     (present AND holds a VIDEO_EXTS file) are now separate counters. present
     == 0 is the storage signal and still reds; present > 0 with resolved == 0
     is an intact pool this run simply cannot classify, which falls through to
     the accumulated-ledger assertion instead of paging. Blindness stays
     covered by the vacuity clock (test_hardlink_vacuity_clock.py), which is
     the mechanism actually designed for "asserted nothing", and which pages
     on a streak rather than on a single unlucky snapshot.

TEST STRATEGY. Structural checks read the script text. Behavioural checks run
the embedded python as a subprocess, same technique as
test_hardlink_vacuity_clock.py and test_hardlink_observation_ledger.py -- but
those two deliberately drive everything through the empty-pool exit so they
never need a library layout. These tests DO need one (a skipped torrent and a
resolved torrent are indistinguishable from an empty pool), so the run helper
additionally rewrites the script's hardcoded /home/quadstronaut LIB_ROOTS and
DOWNLOADS onto a tmp_path tree, with the real inode relationships built by
os.link. Every rewrite target is asserted present first, so a rename in the
script raises here instead of silently testing nothing.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CANARY = REPO / "scripts" / "canaries" / "hardlink-integrity.sh"

DAY = 86400
MIB = 1024 * 1024


# ---------------------------------------------------------------------------
# extraction + harness
# ---------------------------------------------------------------------------

def _embedded_python() -> str:
    """Lift the python heredoc out of the shell wrapper. Pinned deliberately:
    if the delimiter or heredoc style changes, this raises instead of silently
    testing an empty string and passing."""
    src = CANARY.read_text(encoding="utf-8")
    start = src.index('python3 <<"PYEND"') + len('python3 <<"PYEND"')
    end = src.index("\nPYEND", start)
    body = src[start:end]
    assert "VIDEO_EXTS" in body, "extracted the wrong block"
    return body


@pytest.fixture(scope="module")
def code():
    return _embedded_python()


# The script hardcodes absolute seedbox paths. Rewrite each onto tmp_path so
# the behavioural tests can build a real library with real inodes. Keys are the
# QUOTED literals, which makes "Anime" vs "Anime Movies" unambiguous.
_PATH_LITERALS = {
    '"/home/quadstronaut/downloads"': "downloads",
    '"/home/quadstronaut/media/Movies"': "media/Movies",
    '"/home/quadstronaut/media/TV Shows"': "media/TV Shows",
    '"/home/quadstronaut/media/Anime"': "media/Anime",
    '"/home/quadstronaut/media/Anime Movies"': "media/Anime Movies",
}


def _rehome(body: str, tmp_path: Path) -> str:
    for literal, rel in _PATH_LITERALS.items():
        assert body.count(literal) == 1, (
            "path literal %s no longer appears exactly once in the canary; "
            "this test would silently stop exercising the library walk" % literal
        )
        body = body.replace(literal, json.dumps(str(tmp_path / rel)))
    return body


def _run(code, tmp_path, torrents, *, max_days="7", min_sample=None):
    """Run the embedded canary python against a fake HOME and a rehomed tree."""
    payload = tmp_path / "qfh-completed.json"
    payload.write_text(json.dumps(torrents), encoding="utf-8")

    body = _rehome(code, tmp_path)
    body = body.replace('"/tmp/qfh-completed.json"', json.dumps(str(payload)))

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)          # expanduser on Windows
    env["MAX_VACUOUS_DAYS"] = max_days
    if min_sample is not None:
        env["MIN_SAMPLE"] = str(min_sample)
    script = tmp_path / "canary_body.py"
    script.write_text(body, encoding="utf-8")
    return subprocess.run([sys.executable, str(script)], env=env,
                          capture_output=True, text=True)


def _write(path: Path, size: int) -> Path:
    """A file of an exact byte size -- st_size is load-bearing here (it is what
    separates a copy-mode import from a benign orphan)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)
    return path


def _seed_library(tmp_path: Path) -> Path:
    """At least one library video, or the run reds as library-empty long before
    reaching the code under test."""
    return _write(tmp_path / "media" / "Movies" / "Filler (1999)" /
                  "Filler (1999).mkv", 7 * MIB)


def _torrent(content_path: Path, h: str, name="t", category="radarr") -> dict:
    return {"hash": h, "name": name, "category": category,
            "content_path": str(content_path)}


def _ledger(tmp_path: Path) -> Path:
    return tmp_path / ".opt" / "maint" / "hardlink-integrity" / "observations.json"


def _vacuity(tmp_path: Path) -> Path:
    return tmp_path / ".opt" / "maint" / "hardlink-integrity" / "vacuity.json"


def _hardlink(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(str(src), str(dst))
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip("filesystem cannot hardlink (%s)" % exc)


# ---------------------------------------------------------------------------
# 1. taxonomy -- structural
# ---------------------------------------------------------------------------

def _video_exts(code: str) -> tuple:
    m = re.search(r"VIDEO_EXTS = \(([^)]*)\)", code)
    assert m, "VIDEO_EXTS is no longer a tuple literal"
    return tuple(re.findall(r'"([^"]+)"', m.group(1)))


def test_disc_and_transport_stream_extensions_are_listed(code):
    """THE 2026-08-19 INCIDENT. A BDMV rip carries its video as .m2ts and
    nothing else; omitting it made the canary blind to the only torrent in the
    pool and then paged about the blindness as if it were lost storage."""
    exts = _video_exts(code)
    assert ".m2ts" in exts, "the extension that caused the six-hour red is back out"
    assert ".ts" in exts, "same disc/broadcast family, same trap"


def test_the_original_extensions_survive(code):
    """Guards the other direction: this list is only ever appended to. mkv/mp4
    are what essentially the whole library is stored as -- dropping one would
    empty the library index and red as library-empty."""
    exts = _video_exts(code)
    for ext in (".mkv", ".mp4", ".m4v", ".avi", ".mov"):
        assert ext in exts, "%s was removed from VIDEO_EXTS" % ext


def test_one_constant_feeds_both_consumers(code):
    """The coupling is the whole reason a missing extension is dangerous rather
    than merely incomplete: the SAME list decides what the library index can
    see and what a torrent can resolve to. If someone ever forks it into two
    lists, the library could stop indexing a format the torrent walk still
    resolves -- every such import would then read as detached (storage
    doubled), which is a FALSE REGRESSION, the loudest failure this canary
    has."""
    assert len(re.findall(r"^\s*VIDEO_EXTS = \(", code, re.M)) == 1, \
        "VIDEO_EXTS is assigned more than once"
    consumers = re.findall(r"endswith\(VIDEO_EXTS\)", code)
    assert len(consumers) == 2, (
        "expected exactly two consumers (library index + torrent target walk), "
        "found %d" % len(consumers)
    )


def test_the_constant_carries_its_do_not_trim_warning():
    """Repo convention: the WHY lives next to the code, dated and evidenced.
    The list looks arbitrary and grep-tidyable; without the incident named
    inline, trimming it is a reasonable-looking edit."""
    src = CANARY.read_text(encoding="utf-8")
    block = src[src.index("VIDEO_EXTS = (") - 1200:src.index("VIDEO_EXTS = (")]
    assert "DO NOT TRIM" in block
    assert "2026-08-19" in block
    assert "m2ts" in block


# ---------------------------------------------------------------------------
# 2. present vs resolved -- behavioural
# ---------------------------------------------------------------------------

def test_a_bdmv_torrent_now_resolves_and_is_classified(code, tmp_path):
    """END-TO-END REGRESSION TEST for the incident. Same shape as the live
    pool on 2026-08-19: one completed torrent, a BDMV directory whose only
    video is STREAM/00000.m2ts, hardlinked into the library. Before the fix
    this scored resolved=0 and exited 1."""
    lib = _seed_library(tmp_path)
    assert lib.exists()
    stream = _write(tmp_path / "downloads" / "qbittorrent" / "radarr" /
                    "Disc.Rip.1988" / "BDMV" / "STREAM" / "00000.m2ts", 9 * MIB)
    # The metadata siblings a real BDMV carries -- none of them a video.
    _write(stream.parent.parent / "index.bdmv", 120)
    _write(stream.parent.parent / "PLAYLIST" / "00000.mpls", 626)
    _write(stream.parent.parent / "CLIPINF" / "00000.clpi", 67908)
    _hardlink(stream, tmp_path / "media" / "Movies" / "Disc Rip (1988)" /
              "Disc Rip (1988).m2ts")

    r = _run(code, tmp_path, [_torrent(stream.parent.parent.parent, "a" * 40)])

    assert "STAGE=qbit-no-completed" not in r.stderr, (
        "the incident reproduced: a healthy BDMV pool still reds as lost storage"
    )
    assert r.returncode == 0, r.stderr
    obs = json.loads(_ledger(tmp_path).read_text(encoding="utf-8"))["observations"]
    assert obs["a" * 40]["verdict"] == "hardlinked", (
        "the torrent was skipped rather than classified: " + r.stdout + r.stderr
    )


def test_a_transport_stream_torrent_resolves_too(code, tmp_path):
    """.ts is the broadcast-capture sibling of .m2ts. Same trap, so it gets the
    same proof rather than only a membership assertion."""
    _seed_library(tmp_path)
    cap = _write(tmp_path / "downloads" / "cap" / "Show.S01E01" /
                 "Show.S01E01.ts", 5 * MIB)
    _hardlink(cap, tmp_path / "media" / "TV Shows" / "Show" / "Season 01" /
              "Show - S01E01.ts")

    r = _run(code, tmp_path, [_torrent(cap.parent, "b" * 40)])

    assert r.returncode == 0, r.stderr
    obs = json.loads(_ledger(tmp_path).read_text(encoding="utf-8"))["observations"]
    assert obs["b" * 40]["verdict"] == "hardlinked"


def test_an_unclassifiable_pool_is_inconclusive_not_red(code, tmp_path):
    """THE PREDICATE FIX. Content paths present, nothing inside them a video
    (ISO/RAR/NFO -- or tomorrow's container this list has not learned yet).
    That is an intact pool with nothing to say, which is the vacuity clock's
    job, not a storage alarm."""
    _seed_library(tmp_path)
    d = tmp_path / "downloads" / "misc" / "Some.Release"
    _write(d / "disc.iso", 3 * MIB)
    _write(d / "archive.rar", 2 * MIB)
    _write(d / "info.nfo", 400)

    r = _run(code, tmp_path, [_torrent(d, "c" * 40)])

    assert r.returncode == 0, r.stderr
    assert "STAGE=qbit-no-completed" not in r.stderr
    assert "none holding a VIDEO_EXTS file" in r.stdout, (
        "the operator gets no hint why the sample did not grow: " + r.stdout
    )
    assert "this_run=present:1/resolved:0" in r.stdout, (
        "present and resolved are not reported separately: " + r.stdout
    )
    st = json.loads(_vacuity(tmp_path).read_text(encoding="utf-8"))
    assert st["reason"] == "no-classifiable-torrents", (
        "an unclassifiable pool is indistinguishable from a merely small one; "
        "the remedies differ (extend VIDEO_EXTS vs wait for imports)"
    )


def test_vanished_content_paths_still_red(code, tmp_path):
    """The split must not cost the signal the stage is NAMED for. qBit reports
    completed torrents, not one content path is on disk: data dir nuked, mount
    gone, downloads tree moved."""
    _seed_library(tmp_path)
    gone = tmp_path / "downloads" / "evaporated" / "Nothing.Here"

    r = _run(code, tmp_path, [_torrent(gone, "d" * 40),
                              _torrent(gone.parent / "AlsoGone", "e" * 40)])

    assert r.returncode == 1, "lost storage now passes silently: " + r.stdout
    assert "STAGE=qbit-no-completed" in r.stderr, r.stderr
    assert "torrents=2" in r.stderr, (
        "the red says nothing about how much vanished: " + r.stderr
    )


def test_an_unclassifiable_pool_still_evaluates_the_accumulated_ledger(code, tmp_path):
    """WHY THE OLD BEHAVIOUR WAS BACKWARDS, stated as a test. On 2026-08-19 the
    ledger already held 7 hardlinked observations -- more than MIN_SAMPLE -- so
    the canary had ample evidence to assert on. The resolved==0 exit fired
    BEFORE that evaluation and threw all of it away to red on a single
    unrecognised torrent. Now the run falls through and the real assertion
    runs."""
    _seed_library(tmp_path)
    now = int(time.time())
    led = _ledger(tmp_path)
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text(json.dumps({"observations": {
        ("%02d" % i) * 20: {"verdict": "hardlinked",
                            "first_seen": now - 5 * DAY, "last_seen": now - DAY}
        for i in range(5)
    }}), encoding="utf-8")

    d = tmp_path / "downloads" / "misc" / "Some.Release"
    _write(d / "disc.iso", 3 * MIB)

    r = _run(code, tmp_path, [_torrent(d, "f" * 40)])

    assert r.returncode == 0, r.stderr
    assert "PASS: hardlink-integrity" in r.stdout, r.stdout
    assert "inconclusive" not in r.stdout, (
        "the accumulated evidence was discarded instead of asserted on: "
        + r.stdout
    )
    assert "detached_pct=0.0%" in r.stdout
    assert not _vacuity(tmp_path).exists(), (
        "the assertion ran, so the blind streak must be cleared"
    )


def test_a_real_copy_mode_regression_still_reds(code, tmp_path):
    """The taxonomy change must not soften the assertion it exists to make. A
    .m2ts sitting in the library at a DIFFERENT inode but an IDENTICAL byte
    size is storage genuinely doubled -- and it is only visible at all because
    .m2ts is now indexed on BOTH sides."""
    _seed_library(tmp_path)
    now = int(time.time())
    led = _ledger(tmp_path)
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text(json.dumps({"observations": {
        ("%02d" % i) * 20: {"verdict": "detached",
                            "first_seen": now - 5 * DAY, "last_seen": now - DAY}
        for i in range(5)
    }}), encoding="utf-8")

    stream = _write(tmp_path / "downloads" / "qbittorrent" / "radarr" /
                    "Copied.1988" / "BDMV" / "STREAM" / "00000.m2ts", 4 * MIB)
    # Same size, separate inode == copied, not hardlinked.
    _write(tmp_path / "media" / "Movies" / "Copied (1988)" /
           "Copied (1988).m2ts", 4 * MIB)

    r = _run(code, tmp_path,
             [_torrent(stream.parent.parent.parent, "f" * 40, name="Copied.1988")])

    assert r.returncode == 1, "copy-mode storage doubling passed: " + r.stdout
    assert "STAGE=hardlink-regression" in r.stderr, r.stderr
    assert "detached=6/6" in r.stderr, r.stderr
