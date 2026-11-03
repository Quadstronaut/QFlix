"""An MP4 that reaches ffmpeg keeps its container; everything else still goes to MKV.

WHY THIS FILE EXISTS (QFLX-97)
------------------------------
Both ffmpeg paths in qflix-direct-play-fix ended in an unconditional
`Set Container mkv`, and ffmpegCommandStart maps every source stream with
`-c copy`. Matroska cannot hold `mov_text` (tx3g) subtitles, nor the MP4 chapter
text track that ffmpeg exposes as a `bin_data` data stream, so any MP4 carrying
either aborted the muxer:

    [matroska] Subtitle codec 94213 is not supported.
    Could not write header for output file #0

`Transcode error` is terminal in Tdarr, so the file parked and held
"Canary Tdarr Transcode Error" red for ~163h. The live case was
`Verity (2026) WEBDL-1080p.mp4`: h264 + EAC3 + 36 mov_text + a chapter track +
cover art. Its 12 sibling MP4s were h264 + AAC, never reached ffmpeg, and so
never showed the bug.

THE FIX the tests pin: a `checkFileExtension` gate ("mp4") on each path, between
the Ensure-AAC node and the Set-Container node. An MP4 keeps its own container,
so the only change is the added AAC track (plus the h264 re-encode on the video
path). Every other container keeps the old mkv path unchanged.

BEING IN AN MP4 IS NOT PROOF A STREAM CAN BE COPIED INTO A NEW MP4 (review
finding). Tdarr's ffmpeg 5.0.1 refuses `flac` and `truehd` in MP4 as experimental
(Execute never passes `-strict -2`) and has no MP4 tag for `pcm_*`, so those
exited 1 on the keep-container path where the old mkv path exited 0. A second
gate on each path (`qfxCheckNoMp4Copy*`, audio codec_name includes
flac/truehd/pcm_) sends them back to mkv. Measured with Tdarr's own plugins and
ffmpeg 5.0.1 on generated MP4s: aac/ac3/eac3/mp3/mp2/opus/vorbis/dts/alac copy
into MP4 cleanly; flac/truehd/pcm_s16le/pcm_s24le do not and now take mkv (exit
0, as before). An MP4 carrying a refused codec AND mov_text fits neither
container without dropping a stream and still fails, exactly as it did before.

HOW: a tiny interpreter walks the real flow JSON with the same branch rules the
installed plugins use (read from the Tdarr 2.17.01 plugin source on the box), so
a rewired edge or a swapped handle fails here rather than on a member's file.
The real-plugin proof (Tdarr's own JS + ffmpeg 5.0.1 on a clip cut from Verity)
is recorded in the CHANGELOG entry for QFLX-97.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FLOW = REPO / "scripts" / "configure" / "tdarr-flows" / "qflix-direct-play-fix.json"


@pytest.fixture(scope="module")
def flow():
    return json.loads(FLOW.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Branch rules, mirrored from the installed community plugins (handle 1 = match)
# ---------------------------------------------------------------------------

def _ext(path):
    return path.rsplit(".", 1)[-1].lower()


def _check(node, f):
    """Return the output handle a check plugin would take for file `f`."""
    p, i, streams = node["pluginName"], node.get("inputsDB", {}), f["streams"]
    if p == "checkVideoCodec":
        hit = any(s["codec_type"] == "video" and s["codec_name"] == i["codec"] for s in streams)
    elif p == "checkAudioCodec":
        hit = any(s["codec_type"] == "audio" and s["codec_name"] == i["codec"] for s in streams)
    elif p == "checkFileExtension":
        hit = _ext(f["path"]) in [e.strip().lower() for e in i["extensions"].split(",")]
    elif p == "checkStreamProperty":
        typed = [s for s in streams if s["codec_type"] == i["streamType"]]
        if not typed:
            return "2"  # hard-returns 2 when no stream of the type exists
        vals = [v.strip() for v in i["valuesToMatch"].split(",")]
        prop = i["propertyToCheck"]

        def match(s):
            have = str(s.get(prop, ""))
            if i["condition"] == "includes":
                return any(v in have for v in vals)
            return have in vals
        hit = any(match(s) for s in typed)
    else:
        raise AssertionError("no branch rule for " + p)
    return "1" if hit else "2"


def walk(flow, f):
    """Follow the flow from inputFile to replaceOriginalFile.

    Returns (trail of node ids, output container, every SetContainer node run)."""
    nodes = {n["id"]: n for n in flow["flowPlugins"]}
    edges = {(e["source"], e["sourceHandle"]): e["target"] for e in flow["flowEdges"]}
    node = next(n for n in flow["flowPlugins"] if n["pluginName"] == "inputFile")
    trail, container, set_containers = [], None, []
    for _ in range(100):  # a loop in the graph must fail, not hang
        trail.append(node["id"])
        p = node["pluginName"]
        if p == "replaceOriginalFile":
            return trail, container, set_containers
        handle = "1"
        if p.startswith("check"):
            handle = _check(node, f)
        elif p == "ffmpegCommandStart":
            container = _ext(f["path"])  # Start keeps the source container
        elif p == "ffmpegCommandSetContainer":
            set_containers.append(node)
            container = node["inputsDB"]["container"]
        node = nodes[edges[(node["id"], handle)]]
    raise AssertionError("flow did not reach replaceOriginalFile: " + " > ".join(trail))


# ---------------------------------------------------------------------------
# Fixtures: the live failure, its siblings, and the MKV control
# ---------------------------------------------------------------------------

def _v(codec="h264", pix="yuv420p", level=40):
    return {"codec_type": "video", "codec_name": codec, "pix_fmt": pix, "level": level}


def _a(codec):
    return {"codec_type": "audio", "codec_name": codec, "channels": 6}


SUBS = [{"codec_type": "subtitle", "codec_name": "mov_text"} for _ in range(36)]
CHAPTER_TRACK = {"codec_type": "data", "codec_name": "bin_data"}
COVER = {"codec_type": "video", "codec_name": "mjpeg", "pix_fmt": "yuvj420p", "level": -99}

VERITY = {"path": "/m/Movies/Verity (2026)/Verity (2026) WEBDL-1080p.mp4",
          "streams": [_v(), _a("eac3"), *SUBS, CHAPTER_TRACK, COVER]}


def test_mp4_with_eac3_and_mov_text_keeps_its_container(flow):
    """The live failure. It must take the audio path, pass the MP4 gate and
    reach Execute without ever running a Set Container node."""
    trail, container, set_containers = walk(flow, VERITY)
    assert "qfxStartAudio" in trail and "qfxEnsureAacAudio" in trail
    assert "qfxCheckMp4Audio" in trail
    assert "qfxSetMkvAudio" not in trail
    assert trail[-2:] == ["qfxExecuteAudio", "qfxReplace"]
    assert set_containers == []
    assert container == "mp4"


def test_mkv_with_eac3_still_takes_the_old_mkv_path(flow):
    f = {"path": "/m/TV/Show/S01E01.mkv",
         "streams": [_v(), _a("eac3"), {"codec_type": "subtitle", "codec_name": "subrip"}]}
    trail, container, _ = walk(flow, f)
    assert trail[-4:] == ["qfxCheckMp4Audio", "qfxSetMkvAudio", "qfxExecuteAudio", "qfxReplace"]
    assert container == "mkv"


@pytest.mark.parametrize("ext", ["avi", "ts", "m4v", "mov"])
def test_every_other_container_still_converts_to_mkv(flow, ext):
    """Only .mp4 is gated. m4v selects ffmpeg's stricter ipod muxer, so it is
    deliberately left on the mkv path rather than assumed safe."""
    f = {"path": "/m/Movies/X/x." + ext, "streams": [_v(), _a("ac3")]}
    trail, container, _ = walk(flow, f)
    assert "qfxSetMkvAudio" in trail
    assert container == "mkv"


def test_hevc_mp4_keeps_its_container_on_the_video_path(flow):
    """Same defect on the re-encode path: an hevc MP4 with mov_text would have
    died in the same muxer. It is re-encoded to h264 and stays MP4."""
    f = dict(VERITY, streams=[_v("hevc", "yuv420p10le", 120), _a("eac3"), *SUBS, CHAPTER_TRACK])
    trail, container, set_containers = walk(flow, f)
    assert "qfxSetH264" in trail and "qfxCheckMp4Video" in trail
    assert "qfxSetMkvVideo" not in trail
    assert trail[-2:] == ["qfxExecuteVideo", "qfxReplace"]
    assert set_containers == [] and container == "mp4"


def test_hevc_mkv_still_takes_the_old_video_path(flow):
    f = {"path": "/m/Anime/S/S01E01.mkv", "streams": [_v("hevc", "yuv420p10le", 120), _a("aac")]}
    trail, container, _ = walk(flow, f)
    assert trail[-4:] == ["qfxCheckMp4Video", "qfxSetMkvVideo", "qfxExecuteVideo", "qfxReplace"]
    assert container == "mkv"


def test_compliant_mp4_never_reaches_ffmpeg(flow):
    """The 12 sibling MP4s: h264 + AAC. The gate must not change their route,
    which goes straight from the AAC check to Replace (a no-op for them)."""
    f = {"path": "/m/Movies/Y/y.mp4", "streams": [_v(), _a("aac"), *SUBS, COVER]}
    trail, container, _ = walk(flow, f)
    assert trail[-2:] == ["qfxCheckAac", "qfxReplace"]
    assert container is None


def test_no_set_container_node_force_conforms(flow):
    """forceConform drops every stream the target container cannot hold. On
    Verity that is all 36 subtitle tracks, so it must never be the fix."""
    for n in flow["flowPlugins"]:
        if n["pluginName"] == "ffmpegCommandSetContainer":
            assert str(n.get("inputsDB", {}).get("forceConform", "false")).lower() != "true", n["id"]


def test_mp4_gates_are_exact(flow):
    for path in ("Audio", "Video"):
        mp4, nocopy = "qfxCheckMp4" + path, "qfxCheckNoMp4Copy" + path
        execute, setmkv = "qfxExecute" + path, "qfxSetMkv" + path
        n = next(n for n in flow["flowPlugins"] if n["id"] == mp4)
        assert n["pluginName"] == "checkFileExtension"
        assert n["inputsDB"] == {"extensions": "mp4"}
        out = {e["sourceHandle"]: e["target"] for e in flow["flowEdges"] if e["source"] == mp4}
        assert out == {"1": nocopy, "2": setmkv}
        g = next(n for n in flow["flowPlugins"] if n["id"] == nocopy)
        assert g["pluginName"] == "checkStreamProperty"
        assert g["inputsDB"] == {"streamType": "audio", "propertyToCheck": "codec_name",
                                 "condition": "includes", "valuesToMatch": "flac,truehd,pcm_"}
        out = {e["sourceHandle"]: e["target"] for e in flow["flowEdges"] if e["source"] == nocopy}
        # handle 1 = a refused codec is present -> the old mkv path
        assert out == {"1": setmkv, "2": execute}


# ---------------------------------------------------------------------------
# Codec cases measured with Tdarr's ffmpeg 5.0.1 (see module docstring)
# ---------------------------------------------------------------------------

MP4_REFUSED = ["flac", "truehd", "pcm_s16le", "pcm_s24le", "pcm_s16be"]
MP4_COPIES = ["ac3", "eac3", "mp3", "mp2", "opus", "vorbis", "dts", "alac"]


@pytest.mark.parametrize("codec", MP4_REFUSED)
def test_mp4_with_audio_the_mp4_muxer_refuses_takes_the_mkv_path(flow, codec):
    """The review regression: flac/truehd exit 1 as 'experimental' and pcm has
    no MP4 tag, so keeping MP4 parks the file. The old mkv path exits 0."""
    f = {"path": "/m/Movies/Z/z.mp4", "streams": [_v(), _a(codec)]}
    trail, container, _ = walk(flow, f)
    assert trail[-5:] == ["qfxCheckMp4Audio", "qfxCheckNoMp4CopyAudio", "qfxSetMkvAudio",
                          "qfxExecuteAudio", "qfxReplace"]
    assert container == "mkv"


@pytest.mark.parametrize("codec", MP4_COPIES)
def test_mp4_with_audio_the_mp4_muxer_copies_keeps_mp4(flow, codec):
    f = {"path": "/m/Movies/Z/z.mp4", "streams": [_v(), _a(codec), *SUBS[:2]]}
    trail, container, set_containers = walk(flow, f)
    assert trail[-4:] == ["qfxCheckMp4Audio", "qfxCheckNoMp4CopyAudio", "qfxExecuteAudio", "qfxReplace"]
    assert set_containers == [] and container == "mp4"


def test_a_refused_secondary_track_is_enough_to_leave_mp4(flow):
    """Every stream is copied, so a FLAC commentary track behind an EAC3 primary
    fails the MP4 muxer just the same. The gate is ANY-match for that reason."""
    f = {"path": "/m/Movies/Z/z.mp4", "streams": [_v(), _a("eac3"), _a("flac")]}
    _, container, _ = walk(flow, f)
    assert container == "mkv"


def test_hevc_mp4_with_flac_takes_the_mkv_video_path(flow):
    f = {"path": "/m/Movies/Z/z.mp4", "streams": [_v("hevc", "yuv420p10le", 120), _a("flac")]}
    trail, container, _ = walk(flow, f)
    assert trail[-5:] == ["qfxCheckMp4Video", "qfxCheckNoMp4CopyVideo", "qfxSetMkvVideo",
                          "qfxExecuteVideo", "qfxReplace"]
    assert container == "mkv"
