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
