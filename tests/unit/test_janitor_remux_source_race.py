"""QFLX-126: the in-place remux janitors must not replace a file that changed
under them.

Both janitors stat the file, run ffmpeg for up to an hour, then os.replace()
the result over the path. Nothing checked that the path still held the bytes
that were read. Two real neighbours change files in that window: the reaper
(deletes expired episode files; its timer starts while a long janitor run is
still going) and Tdarr / an *arr upgrade (atomically swap a new file in at the
same path).

The real `fix_file` and `run` are driven here. Only ffmpeg and ffprobe are
faked, and the fake "ffmpeg" is where the other actor strikes, i.e. exactly
while the remux is running. Every test fails on the pre-fix code: the deleted
file came back, and the newer file was overwritten with a remux of the old
bytes, both logged as FIXED.
"""
from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from tests.unit.test_janitor_temp_names_tdarr_ghost import _Proc, _a, _v, adj, ucj

OLD = b"old-bytes" * 100
NEW = b"NEWER-ENCODE" * 100

ADJ_PLAN = {"target": 1, "clear": [0], "audio_count": 2}
JANITORS = [
    pytest.param(adj, lambda m, p: m.fix_file(p, ADJ_PLAN), ADJ_PLAN, id="audio-disposition"),
    pytest.param(ucj, lambda m, p: m.fix_file(p, [2]), [2], id="unknown-codec"),
]


def _wire(mod, monkeypatch, src: Path, during):
    """Fake ffmpeg: writes the temp from the OLD bytes, then lets `during` act
    on the source the way another process would mid-remux."""
    monkeypatch.setattr(mod.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=10**12))

    def fake_run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"remux-of:" + src.read_bytes()[:9])
        during()
        return _Proc(0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    if mod is adj:
        before = [_v(), _a("eac3", 6, 1), _a("aac", 2, 1)]
        after = [_v(), _a("eac3", 6, 0), _a("aac", 2, 1)]
    else:
        before = [_v(), _a("aac", 2, 1), {"codec_type": "subtitle", "index": 2}]
        after = [_v(), _a("aac", 2, 1)]
    monkeypatch.setattr(mod, "ffprobe_streams",
                        lambda p: before if p == str(src) else after)


def _no_temps(d: Path):
    return [p.name for p in d.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.parametrize("mod,call,plan", JANITORS)
def test_a_file_deleted_mid_remux_is_not_recreated(mod, call, plan, tmp_path, monkeypatch):
    """The reaper case."""
    src = tmp_path / "Show S01E01.mkv"
    src.write_bytes(OLD)
    _wire(mod, monkeypatch, src, during=lambda: src.unlink())
    with pytest.raises(mod.SourceChangedError):
        call(mod, src)
    assert not src.exists(), "a reaped file was resurrected from its old bytes"
    assert _no_temps(tmp_path)


@pytest.mark.parametrize("mod,call,plan", JANITORS)
def test_a_file_swapped_mid_remux_is_not_overwritten(mod, call, plan, tmp_path, monkeypatch):
    """The Tdarr / upgrade case: a NEW inode lands at the same path."""
    src = tmp_path / "Movie (2014).mkv"
    src.write_bytes(OLD)

    def swap():
        staging = tmp_path / "staging.part"
        staging.write_bytes(NEW)
        os.replace(staging, src)

    _wire(mod, monkeypatch, src, during=swap)
    with pytest.raises(mod.SourceChangedError):
        call(mod, src)
    assert src.read_bytes() == NEW, "the newer file was overwritten by a remux of the old one"
    assert _no_temps(tmp_path)


@pytest.mark.parametrize("mod,call,plan", JANITORS)
def test_a_same_size_same_mtime_swap_is_still_caught_by_the_inode(
        mod, call, plan, tmp_path, monkeypatch):
    src = tmp_path / "Movie.mkv"
    src.write_bytes(OLD)
    st0 = src.stat()

    def swap():
        staging = tmp_path / "staging.part"
        staging.write_bytes(b"X" * len(OLD))                 # same size
        os.utime(staging, ns=(st0.st_atime_ns, st0.st_mtime_ns))   # same mtime
        os.replace(staging, src)

    _wire(mod, monkeypatch, src, during=swap)
    with pytest.raises(mod.SourceChangedError):
        call(mod, src)
    assert src.read_bytes() == b"X" * len(OLD)


@pytest.mark.parametrize("mod,call,plan", JANITORS)
def test_an_untouched_file_is_still_remuxed(mod, call, plan, tmp_path, monkeypatch):
    src = tmp_path / "Quiet.mkv"
    src.write_bytes(OLD)
    _wire(mod, monkeypatch, src, during=lambda: None)
    call(mod, src)
    assert src.read_bytes().startswith(b"remux-of:")
    assert _no_temps(tmp_path)


@pytest.mark.parametrize("mod,call,plan", JANITORS)
def test_run_counts_a_changed_source_as_skipped_not_fixed_and_not_failed(
        mod, call, plan, tmp_path, monkeypatch):
    """Before the fix both janitors logged FIXED for these. A changed source is
    also not a failure: it must not turn the nightly monitor red."""
    src = tmp_path / "Show S01E02.mkv"
    src.write_bytes(OLD)
    _wire(mod, monkeypatch, src, during=lambda: src.unlink())
    monkeypatch.setattr(mod, "active_file_paths", lambda: set())
    monkeypatch.setattr(mod, "scan_files", lambda roots: iter([src]))
    if mod is adj:
        monkeypatch.setattr(mod, "classify_streams", lambda streams, refusals=None: plan)
    else:
        monkeypatch.setattr(mod, "classify_streams", lambda streams: plan)
        monkeypatch.setattr(mod, "requeue_in_tdarr", lambda p: False)
    res = mod.run(roots=[str(tmp_path)], execute=True, max_items=10)
    assert res["fixed"] == []
    assert res["failures"] == []
    assert res["skipped"] == [{"file": str(src), "reason": "source changed during remux"}]
    assert not src.exists()
