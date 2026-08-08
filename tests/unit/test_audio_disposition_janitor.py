"""Tests for audio-disposition-janitor classify/build/verify (pure logic).

Bug (2026-07-19): Tdarr's ensure-AAC flow step leaves BOTH the original
(e.g. EAC3 5.1) and the added aac/2ch track flagged default; Plex tie-breaks
to the lower-index original and live-transcodes audio despite the compatible
track. The janitor narrows to exactly that dual-default pattern.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location(
    "audio_disposition_janitor",
    ROOT / "scripts" / "maint" / "audio-disposition-janitor.py",
)
adj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adj)


def _a(codec: str, channels: int, default: int) -> dict:
    return {"codec_type": "audio", "codec_name": codec, "channels": channels,
            "disposition": {"default": default}}


def _v() -> dict:
    return {"codec_type": "video", "codec_name": "hevc",
            "disposition": {"default": 1}}


# -- classify_streams -------------------------------------------------------

def test_tdarr_dual_default_pattern_matches():
    """The exact observed bug: EAC3 5.1 default + appended aac/2ch default."""
    streams = [_v(), _a("eac3", 6, 1), _a("aac", 2, 1)]
    plan = adj.classify_streams(streams)
    assert plan == {"target": 1, "clear": [0], "audio_count": 2}


def test_single_default_untouched():
    """Healthy file (one default) is never a candidate — even if that
    default is a non-aac track."""
    assert adj.classify_streams([_v(), _a("eac3", 6, 1), _a("aac", 2, 0)]) is None


def test_dual_default_without_compat_track_refused():
    """Two defaults but no aac<=2ch among them = not the Tdarr pattern —
    refuse rather than guess."""
    assert adj.classify_streams([_v(), _a("eac3", 6, 1), _a("dts", 6, 1)]) is None


def test_aac_51_is_not_a_compat_track():
    """aac but 6ch is not the added stereo compat track."""
    assert adj.classify_streams([_v(), _a("eac3", 6, 1), _a("aac", 6, 1)]) is None


def test_last_compat_track_wins():
    """Tdarr appends its stream last — with two default aac/2ch tracks the
    LAST keeps default, everything else clears."""
    streams = [_v(), _a("aac", 2, 1), _a("eac3", 6, 1), _a("aac", 2, 1)]
    plan = adj.classify_streams(streams)
    assert plan == {"target": 2, "clear": [0, 1], "audio_count": 3}


def test_no_audio_streams():
    assert adj.classify_streams([_v()]) is None


def test_missing_disposition_key_tolerated():
    streams = [_v(), {"codec_type": "audio", "codec_name": "aac", "channels": 2}]
    assert adj.classify_streams(streams) is None


# -- build_ffmpeg_cmd -------------------------------------------------------

def test_ffmpeg_cmd_shape():
    cmd = adj.build_ffmpeg_cmd("/in.mkv", "/tmp.mkv",
                                {"target": 1, "clear": [0], "audio_count": 2})
    assert cmd[:2] == ["ffmpeg", "-y"]
    assert ["-i", "/in.mkv"] == cmd[cmd.index("-i"):cmd.index("-i") + 2]
    assert ["-map", "0", "-c", "copy"] == cmd[cmd.index("-map"):cmd.index("-map") + 4]
    assert ["-disposition:a:0", "0"] == cmd[cmd.index("-disposition:a:0"):cmd.index("-disposition:a:0") + 2]
    assert ["-disposition:a:1", "default"] == cmd[cmd.index("-disposition:a:1"):cmd.index("-disposition:a:1") + 2]
    assert cmd[-1] == "/tmp.mkv"


# -- verify_fixed -----------------------------------------------------------

def test_verify_accepts_fixed_file():
    fixed = [_v(), _a("eac3", 6, 0), _a("aac", 2, 1)]
    assert adj.verify_fixed(fixed, expect_stream_count=3)


def test_verify_rejects_lost_stream():
    fixed = [_v(), _a("aac", 2, 1)]
    assert not adj.verify_fixed(fixed, expect_stream_count=3)


def test_verify_rejects_still_dual_default():
    fixed = [_v(), _a("eac3", 6, 1), _a("aac", 2, 1)]
    assert not adj.verify_fixed(fixed, expect_stream_count=3)


def test_verify_rejects_wrong_sole_default():
    fixed = [_v(), _a("eac3", 6, 1), _a("aac", 2, 0)]
    assert not adj.verify_fixed(fixed, expect_stream_count=3)


# ---------------------------------------------------------------------------
# Destructive path — fix_file / run (council 2026-07-20, Defect 5).
# The nightly-armed remux path had ZERO coverage; these exercise the
# free-space guard, atomic replace, post-verify rejection, tmp cleanup on
# failure, --max-items cap, and active-session skip, all with mocked I/O.
# ---------------------------------------------------------------------------
import os
import subprocess as _subprocess
import types


_PLAN = {"target": 1, "clear": [0], "audio_count": 2}
_BEFORE = [_v(), _a("eac3", 6, 1), _a("aac", 2, 1)]
_AFTER = [_v(), _a("eac3", 6, 0), _a("aac", 2, 1)]


class _Proc:
    def __init__(self, rc=0, stderr=""):
        self.returncode = rc
        self.stderr = stderr


def _mk(path: Path, size=1000):
    path.write_bytes(b"x" * size)
    return path


def test_fix_file_happy_path_atomic_replace(tmp_path, monkeypatch):
    src = _mk(tmp_path / "ep.mkv")
    orig_mtime = 1_600_000_000
    os.utime(src, (orig_mtime, orig_mtime))
    monkeypatch.setattr(adj.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=10**9))

    def _fake_run(cmd, **kw):
        # ffmpeg writes the tmp output; simulate by copying bytes.
        Path(cmd[-1]).write_bytes(b"fixed")
        return _Proc(0)
    monkeypatch.setattr(adj.subprocess, "run", _fake_run)
    # src probe -> BEFORE (3 streams); tmp probe -> AFTER (verifies clean)
    calls = {"n": 0}

    def _fake_probe(p):
        return _BEFORE if p == str(src) else _AFTER
    monkeypatch.setattr(adj, "ffprobe_streams", _fake_probe)

    adj.fix_file(src, _PLAN)
    assert src.read_bytes() == b"fixed"                 # replaced
    assert int(src.stat().st_mtime) == orig_mtime       # mtime preserved
    assert not (tmp_path / ".ep.dispfix.tmp.mkv").exists()  # tmp gone


def test_fix_file_insufficient_space_raises_and_no_tmp(tmp_path, monkeypatch):
    src = _mk(tmp_path / "big.mkv", size=2000)
    monkeypatch.setattr(adj.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=100))  # < size*factor
    monkeypatch.setattr(adj.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ffmpeg must not run")))
    import pytest
    with pytest.raises(RuntimeError):
        adj.fix_file(src, _PLAN)
    assert src.read_bytes() == b"x" * 2000              # untouched
    assert list(tmp_path.glob("*.tmp*")) == []


def test_fix_file_verify_failure_keeps_original(tmp_path, monkeypatch):
    src = _mk(tmp_path / "ep.mkv")
    monkeypatch.setattr(adj.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=10**9))
    monkeypatch.setattr(adj.subprocess, "run",
                        lambda cmd, **kw: (Path(cmd[-1]).write_bytes(b"bad"), _Proc(0))[1])
    # tmp still shows BOTH defaults -> verify_fixed False -> must raise, keep original
    monkeypatch.setattr(adj, "ffprobe_streams",
                        lambda p: _BEFORE if p == str(src) else _BEFORE)
    import pytest
    with pytest.raises(RuntimeError):
        adj.fix_file(src, _PLAN)
    assert src.read_bytes() == b"x" * 1000
    assert not (tmp_path / ".ep.dispfix.tmp.mkv").exists()  # finally-unlink ran


def test_fix_file_ffmpeg_nonzero_raises(tmp_path, monkeypatch):
    src = _mk(tmp_path / "ep.mkv")
    monkeypatch.setattr(adj.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=10**9))
    monkeypatch.setattr(adj.subprocess, "run",
                        lambda cmd, **kw: (Path(cmd[-1]).write_bytes(b"partial"), _Proc(1, "boom"))[1])
    monkeypatch.setattr(adj, "ffprobe_streams", lambda p: _BEFORE)
    import pytest
    with pytest.raises(RuntimeError):
        adj.fix_file(src, _PLAN)
    assert src.read_bytes() == b"x" * 1000
    assert list(tmp_path.glob("*.tmp*")) == []


# ---------------------------------------------------------------------------
# Hidden temp + vanish-retry (incident 2026-08-08): Tdarr's library watcher
# queued the visible "<stem>.dispfix.tmp.mkv" mid-write and renamed it to
# "*.tmp" before verify, failing the run (1 FAILED of 56). Hardening: the
# temp is now dot-prefixed (hides it from Plex/Sonarr; Tdarr's watcher does
# NOT skip dotfiles) and a temp that still vanishes at verify time gets
# exactly one fresh-remux retry — that retry is the hard backstop.
# ---------------------------------------------------------------------------

def test_fix_file_tmp_is_dot_prefixed_hidden(tmp_path, monkeypatch):
    """The ffmpeg destination must be a hidden dotfile so Plex/Sonarr
    scanners never see the in-flight temp (Tdarr's watcher may still —
    the vanish-retry tests below cover that case)."""
    src = _mk(tmp_path / "ep.mkv")
    monkeypatch.setattr(adj.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=10**9))
    seen = []

    def _fake_run(cmd, **kw):
        seen.append(cmd[-1])
        Path(cmd[-1]).write_bytes(b"fixed")
        return _Proc(0)
    monkeypatch.setattr(adj.subprocess, "run", _fake_run)
    monkeypatch.setattr(adj, "ffprobe_streams",
                        lambda p: _BEFORE if p == str(src) else _AFTER)
    adj.fix_file(src, _PLAN)
    tmp_name = Path(seen[0]).name
    assert tmp_name == ".ep.dispfix.tmp.mkv"
    assert tmp_name.startswith(".")                     # hidden from scanners


def test_fix_file_vanished_tmp_retried_once_then_succeeds(tmp_path, monkeypatch):
    """Temp renamed away by an external scanner between ffmpeg and verify =
    retryable: one fresh remux, then normal success."""
    src = _mk(tmp_path / "ep.mkv")
    monkeypatch.setattr(adj.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=10**9))
    runs = []

    def _fake_run(cmd, **kw):
        runs.append(cmd[-1])
        Path(cmd[-1]).write_bytes(b"fixed")
        return _Proc(0)
    monkeypatch.setattr(adj.subprocess, "run", _fake_run)
    probed = {"tmp": 0}

    def _fake_probe(p):
        if p == str(src):
            return _BEFORE
        probed["tmp"] += 1
        if probed["tmp"] == 1:                          # the renamer strikes
            Path(p).unlink()
            raise RuntimeError("ffprobe exit 1: No such file or directory")
        return _AFTER
    monkeypatch.setattr(adj, "ffprobe_streams", _fake_probe)

    adj.fix_file(src, _PLAN)                            # must NOT raise
    assert len(runs) == 2                               # exactly one retry
    assert src.read_bytes() == b"fixed"                 # second attempt landed


def test_fix_file_vanished_twice_is_hard_failure(tmp_path, monkeypatch):
    """A temp that vanishes on the retry too is a real failure — bounded at
    two attempts, no infinite remux loop."""
    src = _mk(tmp_path / "ep.mkv")
    monkeypatch.setattr(adj.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=10**9))
    runs = []

    def _fake_run(cmd, **kw):
        runs.append(cmd[-1])
        Path(cmd[-1]).write_bytes(b"fixed")
        return _Proc(0)
    monkeypatch.setattr(adj.subprocess, "run", _fake_run)

    def _fake_probe(p):
        if p == str(src):
            return _BEFORE
        Path(p).unlink()                                # vanishes every time
        raise RuntimeError("ffprobe exit 1: No such file or directory")
    monkeypatch.setattr(adj, "ffprobe_streams", _fake_probe)

    import pytest
    with pytest.raises(RuntimeError, match="persisted after retry"):
        adj.fix_file(src, _PLAN)
    assert len(runs) == 2                               # tried, retried, stopped
    assert src.read_bytes() == b"x" * 1000              # source untouched


def test_fix_file_real_probe_failure_not_retried(tmp_path, monkeypatch):
    """A probe failure while the temp still EXISTS is not the vanish race —
    it must raise immediately with no second remux."""
    src = _mk(tmp_path / "ep.mkv")
    monkeypatch.setattr(adj.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=10**9))
    runs = []

    def _fake_run(cmd, **kw):
        runs.append(cmd[-1])
        Path(cmd[-1]).write_bytes(b"fixed")
        return _Proc(0)
    monkeypatch.setattr(adj.subprocess, "run", _fake_run)

    def _fake_probe(p):
        if p == str(src):
            return _BEFORE
        raise RuntimeError("ffprobe exit 1: corrupt output")  # tmp still there
    monkeypatch.setattr(adj, "ffprobe_streams", _fake_probe)

    import pytest
    with pytest.raises(RuntimeError, match="corrupt output"):
        adj.fix_file(src, _PLAN)
    assert len(runs) == 1                               # no retry


def test_run_max_items_caps_fixed_count(tmp_path, monkeypatch):
    files = [_mk(tmp_path / f"e{i}.mkv") for i in range(5)]
    monkeypatch.setattr(adj, "scan_files", lambda roots: iter(files))
    monkeypatch.setattr(adj, "ffprobe_streams", lambda p: _BEFORE)  # all candidates
    monkeypatch.setattr(adj, "classify_streams", lambda streams: dict(_PLAN))
    monkeypatch.setattr(adj, "active_file_paths", lambda: set())
    fixed = []
    monkeypatch.setattr(adj, "fix_file", lambda p, plan: fixed.append(str(p)))
    res = adj.run(roots=["/x"], execute=True, max_items=2)
    assert len(res["fixed"]) == 2
    assert sum(1 for s in res["skipped"] if s["reason"] == "max-items cap") == 3


def test_run_skips_active_plex_session(tmp_path, monkeypatch):
    f = _mk(tmp_path / "e.mkv")
    monkeypatch.setattr(adj, "scan_files", lambda roots: iter([f]))
    monkeypatch.setattr(adj, "ffprobe_streams", lambda p: _BEFORE)
    monkeypatch.setattr(adj, "classify_streams", lambda streams: dict(_PLAN))
    monkeypatch.setattr(adj, "active_file_paths", lambda: {str(f)})
    monkeypatch.setattr(adj, "fix_file",
                        lambda p, plan: (_ for _ in ()).throw(AssertionError("must skip")))
    res = adj.run(roots=["/x"], execute=True, max_items=50)
    assert res["fixed"] == []
    assert res["skipped"][0]["reason"] == "active Plex session"


def test_run_dry_run_mutates_nothing(tmp_path, monkeypatch):
    f = _mk(tmp_path / "e.mkv")
    monkeypatch.setattr(adj, "scan_files", lambda roots: iter([f]))
    monkeypatch.setattr(adj, "ffprobe_streams", lambda p: _BEFORE)
    monkeypatch.setattr(adj, "classify_streams", lambda streams: dict(_PLAN))
    monkeypatch.setattr(adj, "fix_file",
                        lambda p, plan: (_ for _ in ()).throw(AssertionError("dry-run must not fix")))
    res = adj.run(roots=["/x"], execute=False, max_items=50)
    assert res["candidates"] == [str(f)]
    assert res["fixed"] == []
