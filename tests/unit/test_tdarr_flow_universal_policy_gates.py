"""The universal-playability policy is enforced by TWO surfaces, and they must agree.

WHY THIS FILE EXISTS
--------------------
The operator directive (2026-08-20) is that every file must direct-play on every
TV, phone and tablet. The implementation gated on the video CODEC alone
(vc1/mpeg2video/hevc/av1 -> h264), which quietly answered a narrower question:
"is this file the wrong codec?" rather than "is this file playable?".

A file that ARRIVES as h264 skipped every gate. The Force 8-bit node -- the node
that actually pins `high`, `yuv420p` and level 4.1 -- only ever runs on files
that entered the codec branch, so an h264 file was stamped `Not required`
whatever its bit depth or level. A full audit of the live library on 2026-08-24
found 28 such files:

    12  High 10 / yuv420p10le   (Mob Psycho 100 S01)
    15  level 4.2               (The Graham Norton Show S33)
     1  level 5.0               (Colony 2026)

The twelve matter most. Samsung Tizen and most smart-TV decoders cannot decode
10-bit H.264 **at all** -- it is precisely the failure mode the directive was
written about, arriving through a door the directive's own implementation left
open.

TWO SURFACES, ONE POLICY
The flow decides what to re-encode. `50b-tdarr-config.py` decides what to
re-queue. Tdarr caches a per-file verdict and never revisits it on a flow
change, so BOTH must move together: a widened flow leaves the 28 existing files
untouched forever, and a widened requeue against an un-widened flow just returns
each file to `Not required`. These tests pin them to the same predicates so the
next person to widen one is told about the other.

WHAT IS DELIBERATELY *NOT* TESTED HERE
Bitrate. A 35 Mbps h264 file is perfectly *compatible*; it is merely too big for
a slow link, and that is Plex's transcoder's job, not Tdarr's -- no codec policy
can make 1080p direct-play at 1.9 Mbps. Conflating "incompatible" with "large"
would re-encode 62 healthy files for nothing.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FLOW = REPO / "scripts" / "configure" / "tdarr-flows" / "qflix-direct-play-fix.json"
CONFIG = REPO / "scripts" / "configure" / "50b-tdarr-config.py"


@pytest.fixture(scope="module")
def flow():
    return json.loads(FLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cfg():
    spec = importlib.util.spec_from_file_location("tdarr_cfg_50b", CONFIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _node(flow, node_id):
    for n in flow["flowPlugins"]:
        if n["id"] == node_id:
            return n
    raise AssertionError("flow node %s is gone (renamed? the gate is the point)" % node_id)


def _edges(flow, src):
    return {e["sourceHandle"]: e["target"]
            for e in flow["flowEdges"] if e["source"] == src}


# ---------------------------------------------------------------------------
# The gates exist and route the right way
# ---------------------------------------------------------------------------

def test_bit_depth_gate_catches_more_than_8_bits(flow):
    n = _node(flow, "qfxCheckHighBitDepth")
    i = n["inputsDB"]
    assert n["pluginName"] == "checkStreamProperty"
    assert i["streamType"] == "video"
    assert i["propertyToCheck"] == "pix_fmt"
    assert i["condition"] == "includes"
    vals = {v.strip() for v in i["valuesToMatch"].split(",")}
    # 10-bit is the hard incompatibility; 12-bit is included because it is the
    # same class and costs nothing to name.
    assert {"10le", "10be", "12le", "12be"} <= vals


def test_level_gate_enumerates_every_h264_level_above_41(flow):
    """The enumeration must be COMPLETE, or a future level slips through as
    compliant. H.264 defines no level beyond 6.2, so this set is closed."""
    n = _node(flow, "qfxCheckLevelAbove41")
    i = n["inputsDB"]
    assert n["pluginName"] == "checkStreamProperty"
    assert i["streamType"] == "video"
    assert i["propertyToCheck"] == "level"
    assert i["condition"] == "equals"
    vals = {v.strip() for v in i["valuesToMatch"].split(",")}
    assert vals == {"42", "50", "51", "52", "60", "61", "62"}


def test_both_gates_route_violations_into_the_reencode_branch(flow):
    """Handle 1 is the MATCH handle on checkStreamProperty, so handle 1 must be
    the violation path. Wire it backwards and the flow re-encodes every
    compliant file and passes every broken one."""
    bits = _edges(flow, "qfxCheckHighBitDepth")
    lvl = _edges(flow, "qfxCheckLevelAbove41")
    assert bits["1"] == "qfxStartVideo"
    assert lvl["1"] == "qfxStartVideo"


def test_the_compliant_path_is_handle_2_so_an_unreadable_file_is_left_alone(flow):
    """checkStreamProperty hard-returns output 2 when the file has no stream of
    the requested type. Handle 2 therefore carries BOTH "compliant" and "I could
    not look", and it must lead onward rather than into the encoder -- otherwise
    a file with missing ffProbeData gets re-encoded on no evidence at all."""
    assert _edges(flow, "qfxCheckHighBitDepth")["2"] == "qfxCheckLevelAbove41"
    assert _edges(flow, "qfxCheckLevelAbove41")["2"] == "qfxCheckAac"


def test_the_gates_sit_after_the_codec_chain_and_before_the_audio_check(flow):
    """Position matters: an hevc file must still be caught by Check hevc (which
    knows it needs a full re-encode) rather than by the bit-depth gate."""
    assert _edges(flow, "qfxCheckAv1")["2"] == "qfxCheckHighBitDepth"


def test_flow_graph_is_still_whole(flow):
    """Every edge lands on a real node, every check wires both handles, and the
    input node is the only unreachable one."""
    ids = {n["id"] for n in flow["flowPlugins"]}
    handles, targets = {}, set()
    for e in flow["flowEdges"]:
        assert e["source"] in ids, "dangling source " + e["source"]
        assert e["target"] in ids, "dangling target " + e["target"]
        handles.setdefault(e["source"], set()).add(e["sourceHandle"])
        targets.add(e["target"])
    for n in flow["flowPlugins"]:
        if n["pluginName"] in ("checkVideoCodec", "checkAudioCodec",
                               "checkStreamProperty"):
            assert handles.get(n["id"]) == {"1", "2"}, \
                "%s does not wire both handles" % n["id"]
    assert len({e["id"] for e in flow["flowEdges"]}) == len(flow["flowEdges"]), \
        "duplicate edge id"
    assert sorted(ids - targets) == ["qfxInputFile"]


# ---------------------------------------------------------------------------
# 50b agrees with the flow
# ---------------------------------------------------------------------------

def test_requeue_predicates_match_the_flow_gates(flow, cfg):
    """The two surfaces must name the same values. Widening one alone is the
    default failure: a requeue whose flow still says 'Not required' just puts
    the file back where it started."""
    bits = {v.strip() for v in
            _node(flow, "qfxCheckHighBitDepth")["inputsDB"]["valuesToMatch"].split(",")}
    lvls = {int(v) for v in
            _node(flow, "qfxCheckLevelAbove41")["inputsDB"]["valuesToMatch"].split(",")}
    assert set(cfg.DISALLOWED_PIX_FMT_MARKERS) == bits
    assert cfg.DISALLOWED_H264_LEVELS == lvls


def _rec(codec, pix, level, *, cover_art=False):
    streams = []
    if cover_art:
        streams.append({"codec_type": "video", "codec_name": "mjpeg",
                        "pix_fmt": "yuvj420p", "level": -99,
                        "disposition": {"attached_pic": 1}})
    streams.append({"codec_type": "video", "codec_name": codec,
                    "pix_fmt": pix, "level": level,
                    "disposition": {"attached_pic": 0}})
    return {"video_codec_name": codec, "ffProbeData": {"streams": streams}}


def test_compliant_file_is_not_requeued(cfg):
    assert cfg.video_policy_violation(_rec("h264", "yuv420p", 41)) == ""
    assert cfg.video_policy_violation(_rec("h264", "yuv420p", 40)) == ""
    assert cfg.video_policy_violation(_rec("h264", "yuv420p", 31)) == ""


def test_ten_bit_h264_is_requeued(cfg):
    """The live case: 12 Mob Psycho episodes, already h264, undecodable on the
    TVs this policy exists to serve."""
    r = cfg.video_policy_violation(_rec("h264", "yuv420p10le", 40))
    assert "pix_fmt" in r and "8-bit" in r


def test_level_above_41_is_requeued(cfg):
    assert "level=42" in cfg.video_policy_violation(_rec("h264", "yuv420p", 42))
    assert "level=50" in cfg.video_policy_violation(_rec("h264", "yuv420p", 50))


def test_wrong_codec_still_requeued_and_says_so(cfg):
    r = cfg.video_policy_violation(_rec("hevc", "yuv420p10le", 120))
    assert r == "codec=hevc", "codec must be reported before bit depth"


def test_cover_art_is_never_read_as_the_primary_video(cfg):
    """A poster is a video stream with pix_fmt yuvj420p and level -99. Reading
    it would answer the policy question about the wrong stream -- and 36 of the
    live records carry one."""
    assert cfg.video_policy_violation(_rec("h264", "yuv420p", 40, cover_art=True)) == ""
    r = cfg.video_policy_violation(_rec("h264", "yuv420p10le", 40, cover_art=True))
    assert "pix_fmt" in r


def test_missing_probe_data_is_not_a_violation(cfg):
    """Absence of evidence is not evidence. A record Tdarr has not probed must
    not be re-encoded on a guess."""
    assert cfg.video_policy_violation({"video_codec_name": "h264"}) == ""
    assert cfg.video_policy_violation({}) == ""
    assert cfg.video_policy_violation(
        {"video_codec_name": "h264", "ffProbeData": {"streams": []}}) == ""


def test_non_integer_level_is_ignored(cfg):
    """ffprobe can report level as a string or omit it; neither is a violation."""
    assert cfg.video_policy_violation(_rec("h264", "yuv420p", None)) == ""
    assert cfg.video_policy_violation(_rec("h264", "yuv420p", "42")) == ""
