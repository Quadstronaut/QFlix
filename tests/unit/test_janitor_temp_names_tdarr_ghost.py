"""SOURCE half of the Tdarr ghost-record fix: janitor temp filenames.

WHY THIS FILE EXISTS
--------------------
Tdarr admits a file into FileJSONDB purely by `path.extname()` against the
library `containerFilter` (mkv,mp4,mov,m4v,mpg,mpeg,avi,flv,webm,wmv,m2ts,ts).
Deobfuscated from Tdarr_Server/srcug/fileScanner/fileScanner.js on 2026-08-23:

    c = function(r){ s = path.extname(r).split(".").join("");
                     for (t ... allowedContainers ...) }

There is NO hidden-file rule anywhere in that scanner, and `foldersToIgnore` is
"" on all five libraries. So a dot prefix buys nothing at all against Tdarr --
which is exactly the wrong mechanism the old comments in both janitors asserted.

Both janitors used to name their temp `<stem>.<tag>.tmp<ORIGINAL SUFFIX>`, e.g.
`.Interstellar (2014) Bluray-1080p Proper.dispfix.tmp.mkv`. extname of that is
`.mkv`, so Tdarr's folder watcher (30s poll, enabled on every library) indexed
it mid-write; `os.replace(tmp, path)` then moved the file out from under the
record. What is left is a GHOST: a FileJSONDB record whose `_id` is a path that
no longer exists, permanently stuck at `HealthCheck=Queued` (nothing to check,
so it can never complete) with a terminal `TranscodeDecisionMaker=Transcode
error` (Tdarr never retries that state). Exactly one such record existed in 465
and it held BOTH tdarr canaries red on a healthy pipeline.

THE FIX IS THE FILENAME, and it has a hard dependency: ffmpeg chooses its muxer
from the OUTPUT FILENAME, so dropping the media extension without adding an
explicit `-f` fails 100% of files. Proved on the box against the same
ffmpeg 7.1.5 the janitors call:

    ffmpeg -y -loglevel error -i qmp4src.mp4 -map 0 -c copy qout.tmp
      -> Unable to choose an output format for 'qout.tmp'   (rc=234)
    ffmpeg -y -loglevel error -i qmp4src.mp4 -map 0 -c copy -f mp4 qout.tmp
      -> MUX-OK, ffprobe: 2 streams ['video', 'audio']
    (same for -f matroska from a .mkv source; os.replace of a .tmp onto a .mkv
     is extension-agnostic and was re-confirmed too)

So the name change and the `-f` are ONE change, and this file pins both halves
plus the invariant that binds MUXER to VIDEO_EXTS.

WHAT IS ACTUALLY EXERCISED
Nothing greps the source. `fix_file` is driven for real with mocked ffmpeg /
ffprobe I/O -- the same technique test_audio_disposition_janitor.py already
uses -- and the temp name asserted is the one the janitor genuinely handed to
ffmpeg. `test_the_old_name_would_have_been_admitted` is the mutation proof: the
admission predicate is run against the pre-fix name and must say ADMITTED, so a
green result above means the predicate discriminates rather than being vacuous.
"""
from __future__ import annotations

import importlib.util
import os
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(stem: str, modname: str):
    spec = importlib.util.spec_from_file_location(
        modname, ROOT / "scripts" / "maint" / (stem + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


adj = _load("audio-disposition-janitor", "audio_disposition_janitor")
ucj = _load("unknown-codec-stream-janitor", "unknown_codec_stream_janitor")

# Verbatim from every one of the five Tdarr libraries (Movies oBWkbmn0a,
# TV UVm0ExqnQ, Anime XUPiWNYFJ, Welcome tL04oKm_y, Anime Movies y4F1xdHdY),
# read off the live LibrarySettingsJSONDB on 2026-08-23. Identical on all five.
TDARR_CONTAINER_FILTER = "mkv,mp4,mov,m4v,mpg,mpeg,avi,flv,webm,wmv,m2ts,ts"
_ALLOWED = set(TDARR_CONTAINER_FILTER.split(","))


def tdarr_admits(name: str) -> bool:
    """Tdarr fileScanner admission, transcribed from the deobfuscated source.

    `path.extname(r).split(".").join("")` is exactly os.path.splitext()[1] with
    the dot removed, including the Node behaviour that a leading-dot name with
    no further dot (".plexmatch") has an EMPTY extname and is therefore never
    admitted. There is no hidden-file branch to model, because there is none.
    """
    return os.path.splitext(name)[1].replace(".", "") in _ALLOWED


class _Proc:
    def __init__(self, rc=0, stderr=""):
        self.returncode = rc
        self.stderr = stderr


def _v(codec="hevc"):
    return {"codec_type": "video", "codec_name": codec,
            "disposition": {"default": 1}}


def _a(codec, channels, default):
    return {"codec_type": "audio", "codec_name": codec, "channels": channels,
            "disposition": {"default": default}}


# ---------------------------------------------------------------------------
# The admission predicate itself -- guard the guard before trusting it below.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Movie (2014) Bluray-1080p.mkv", True),
    ("Movie (2014).mp4", True),
    ("Movie.m2ts", True),
    (".Movie (2014) Bluray-1080p.dispfix.tmp.mkv", True),   # the OLD temp name
    ("Movie.unkcodecfix.tmp.mkv", True),                    # the OLD temp name
    (".Movie (2014) Bluray-1080p.dispfix.tmp", False),      # the NEW temp name
    (".Movie.unkcodecfix.tmp", False),                      # the NEW temp name
    (".plexmatch", False),                                  # extname is EMPTY
    ("poster.jpg", False),
])
def test_tdarr_admission_predicate(name, expected):
    assert tdarr_admits(name) is expected


def test_the_old_name_would_have_been_admitted():
    """MUTATION PROOF. If the predicate said False for the pre-fix names, every
    assertion in this file would be vacuously green and the ghost class would be
    unpinned. The two names that actually minted ghosts must read ADMITTED."""
    assert tdarr_admits(".Interstellar (2014) Bluray-1080p Proper.dispfix.tmp.mkv")
    assert tdarr_admits("The Marshals S01E01.unkcodecfix.tmp.mkv")


def test_dot_prefix_alone_never_protected_against_tdarr():
    """The corrected prose, asserted. Both janitors used to claim the leading
    dot was the guard; it is not, and '.plexmatch' was never evidence that Tdarr
    queues dotfiles -- its extname is empty, so it could never be admitted."""
    assert tdarr_admits(".anything.mkv")        # hidden AND admitted
    assert not tdarr_admits(".plexmatch")       # the cited counter-example


# ---------------------------------------------------------------------------
# MUXER <-> VIDEO_EXTS: the constant pair that must move together.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod,name", [(adj, "audio-disposition"),
                                      (ucj, "unknown-codec-stream")])
def test_muxer_is_total_over_video_exts(mod, name):
    """A source extension the janitor will SCAN but cannot MUX is a KeyError on
    every candidate at execute time -- loud in the durable log, invisible to
    every canary (they watch Tdarr, not the janitor). The two constants sit
    ~200 lines apart and nothing else binds them, so this is the binding."""
    assert set(mod.MUXER) == set(mod.VIDEO_EXTS), name
    assert mod.MUXER[".mkv"] == "matroska"
    assert mod.MUXER[".mp4"] == "mp4"


# ---------------------------------------------------------------------------
# build_ffmpeg_cmd: -f present, derived from SRC, output still last.
# ---------------------------------------------------------------------------

def test_audio_cmd_carries_explicit_muxer_from_source_extension():
    plan = {"target": 1, "clear": [0], "audio_count": 2}
    mkv = adj.build_ffmpeg_cmd("/m/Movie.mkv", "/m/.Movie.dispfix.tmp", plan)
    assert mkv[-3:] == ["-f", "matroska", "/m/.Movie.dispfix.tmp"]
    mp4 = adj.build_ffmpeg_cmd("/m/Movie.mp4", "/m/.Movie.dispfix.tmp", plan)
    assert mp4[-3:] == ["-f", "mp4", "/m/.Movie.dispfix.tmp"]


def test_unknown_codec_cmd_carries_explicit_muxer_from_source_extension():
    mkv = ucj.build_ffmpeg_cmd("/m/Ep.mkv", "/m/.Ep.unkcodecfix.tmp", [3])
    assert mkv[-3:] == ["-f", "matroska", "/m/.Ep.unkcodecfix.tmp"]
    assert ["-map", "-0:3"] == mkv[mkv.index("-map", 1 + mkv.index("-map")):][:2]
    mp4 = ucj.build_ffmpeg_cmd("/m/Ep.mp4", "/m/.Ep.unkcodecfix.tmp", [3])
    assert mp4[-3:] == ["-f", "mp4", "/m/.Ep.unkcodecfix.tmp"]


@pytest.mark.parametrize("mod,args", [
    (adj, ("/m/Movie.MKV", "/m/.Movie.dispfix.tmp",
           {"target": 1, "clear": [0], "audio_count": 2})),
    (ucj, ("/m/Movie.MKV", "/m/.Movie.unkcodecfix.tmp", [3])),
])
def test_muxer_lookup_is_case_insensitive(mod, args):
    """scan_files matches on `p.suffix.lower()`, so a file named .MKV is a
    genuine candidate. Looking MUXER up on the raw suffix would KeyError on it."""
    assert mod.build_ffmpeg_cmd(*args)[-2] == "matroska"


# ---------------------------------------------------------------------------
# The real fix_file paths: the name ffmpeg is actually handed.
# ---------------------------------------------------------------------------

def _mk(path: Path, size: int = 1000) -> Path:
    path.write_bytes(b"x" * size)
    return path


def test_audio_janitor_temp_is_invisible_to_tdarr(tmp_path, monkeypatch):
    """Drives the real fix_file and asserts on the destination it handed to
    ffmpeg: dot-prefixed (Plex/Sonarr) AND ending .tmp (Tdarr)."""
    src = _mk(tmp_path / "Movie (2014) Bluray-1080p.mkv")
    monkeypatch.setattr(adj.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=10**9))
    seen = []

    def _fake_run(cmd, **kw):
        seen.append(cmd)
        Path(cmd[-1]).write_bytes(b"fixed")
        return _Proc(0)

    monkeypatch.setattr(adj.subprocess, "run", _fake_run)
    before = [_v(), _a("eac3", 6, 1), _a("aac", 2, 1)]
    after = [_v(), _a("eac3", 6, 0), _a("aac", 2, 1)]
    monkeypatch.setattr(adj, "ffprobe_streams",
                        lambda p: before if p == str(src) else after)

    adj.fix_file(src, {"target": 1, "clear": [0], "audio_count": 2})

    dst = Path(seen[0][-1])
    assert dst.name == ".Movie (2014) Bluray-1080p.dispfix.tmp"
    assert dst.name.startswith(".")             # Plex / Sonarr skip dotfiles
    assert not tdarr_admits(dst.name)           # Tdarr never indexes it
    assert seen[0][-3:-1] == ["-f", "matroska"]
    assert src.read_bytes() == b"fixed"         # the replace still landed


def test_unknown_codec_janitor_temp_is_invisible_to_tdarr(tmp_path, monkeypatch):
    """Same assertion for the sibling janitor, whose old temp was STRICTLY
    worse: no leading dot either, so Plex, Sonarr and Radarr saw it too."""
    src = _mk(tmp_path / "The Marshals S01E01.mkv")
    monkeypatch.setattr(ucj.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=10**9))
    seen = []

    def _fake_run(cmd, **kw):
        seen.append(cmd)
        Path(cmd[-1]).write_bytes(b"fixed")
        return _Proc(0)

    monkeypatch.setattr(ucj.subprocess, "run", _fake_run)
    before = [_v(), _a("aac", 2, 1), {"codec_type": "subtitle", "index": 2}]
    after = [_v(), _a("aac", 2, 1)]
    monkeypatch.setattr(ucj, "ffprobe_streams",
                        lambda p: before if p == str(src) else after)

    ucj.fix_file(src, [2])

    dst = Path(seen[0][-1])
    assert dst.name == ".The Marshals S01E01.unkcodecfix.tmp"
    assert dst.name.startswith(".")             # was MISSING before the fix
    assert not tdarr_admits(dst.name)
    assert seen[0][-3:-1] == ["-f", "matroska"]
    assert src.read_bytes() == b"fixed"


@pytest.mark.parametrize("mod,tag,call", [
    (adj, "dispfix", lambda m, p: m.fix_file(
        p, {"target": 1, "clear": [0], "audio_count": 2})),
    (ucj, "unkcodecfix", lambda m, p: m.fix_file(p, [2])),
])
def test_no_janitor_temp_survives_on_disk(mod, tag, call, tmp_path, monkeypatch):
    """The temp is unlinked either way; a leftover would be a permanent
    scanner-visible artefact regardless of its name."""
    src = _mk(tmp_path / "Ep.mkv")
    monkeypatch.setattr(mod.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=10**9))
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: (Path(cmd[-1]).write_bytes(b"f"),
                                           _Proc(0))[1])
    before = [_v(), _a("eac3", 6, 1), _a("aac", 2, 1)]
    after = [_v(), _a("eac3", 6, 0), _a("aac", 2, 1)]
    if mod is ucj:
        before = [_v(), _a("aac", 2, 1), {"codec_type": "subtitle", "index": 2}]
        after = [_v(), _a("aac", 2, 1)]
    monkeypatch.setattr(mod, "ffprobe_streams",
                        lambda p: before if p == str(src) else after)

    call(mod, src)
    leftovers = [p.name for p in tmp_path.iterdir() if tag in p.name]
    assert leftovers == []
