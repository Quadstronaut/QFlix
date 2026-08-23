"""Tdarr flow: overall output arguments must not blow up on COVER-ART streams.

WHY THIS FILE EXISTS (2026-08-23)
The Force 8-bit node shipped `-profile:v high` as an OVERALL output argument.
`:v` is a stream specifier that matches EVERY output video stream, not only the
one being encoded. `ffmpegCommandStart` maps all source streams and
`ffmpegCommandExecute` stream-copies everything except the primary video, so a
file carrying an attached picture (an mjpeg or png poster) has a SECOND output
video stream under `-c:N copy`. ffmpeg still builds an AVCodecContext for a
copied stream, but with a NULL codec — so the libx264-private named constant
`high` cannot be resolved:

    [NULL @ 0x...] Undefined constant or missing '(' in 'high'
    Error setting up codec context options.
    Error initializing output stream 0:9 --

Exit 1, zero bytes written, before a single frame is encoded. Tdarr stamps the
file `TranscodeDecisionMaker: Transcode error`, which is TERMINAL — 50b's
requeue_noncompliant_video deliberately excludes that state so a flow bug cannot
become a retry loop — so the file parks forever and the 48h grace on "Canary
Tdarr Transcode Error" fires. Two movies were parked this way; 34 more files in
the library carry cover art and were latent, waiting on any hevc/av1 re-grab.

WHAT THIS PINS
The failure is not "cover art" — cover art is just today's instance of a second
video stream. The mechanism is: a CODEC-PRIVATE NAMED CONSTANT handed to a
stream-copied output stream, which has no codec to resolve it against. So the
rule tested here is the mechanism, not the symptom — every overall output
argument whose value is a symbolic constant must carry an explicit stream index.

`-pix_fmt:v` and `-level:v` are deliberately left unqualified and are ALLOWED by
this rule: their values are not symbolic, ffmpeg accepts and ignores them on a
copied stream (proven — scoping all three produced byte-identical output), and
the fix that removes the defect is the smallest one. If a future edit gives
either of them a symbolic value, this test starts failing, which is correct.

No ffmpeg here: the workstation has none, and the defect is fully visible in the
flow document. This asserts on the shipped JSON that 50b-tdarr-config.py writes
into FlowsJSONDB.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FLOW_DIR = REPO / "scripts" / "configure" / "tdarr-flows"
DIRECT_PLAY_FLOW = FLOW_DIR / "qflix-direct-play-fix.json"

# Plugins whose inputsDB text is pushed onto `overallOuputArguments` verbatim and
# therefore applies to EVERY output stream, encoded or copied. (Per-stream
# `stream.outputArgs` are a different, safe channel — those are index-substituted
# by ffmpegCommandExecute.)
OVERALL_ARG_PLUGINS = {"ffmpegCommandCustomArguments"}

# ffmpeg options whose value is resolved through the TARGET STREAM'S codec option
# table (AVOption named constants). On a `-c copy` stream the codec context is
# NULL, so the lookup fails and ffmpeg aborts at output-stream init.
CODEC_PRIVATE_OPTIONS = {"profile", "level", "preset", "tune", "rc", "cq"}

# `-opt`, `-opt:v`, `-opt:v:0`, `-opt:0` ...
_OPT = re.compile(r"^-(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?P<spec>:.*)?$")

# A stream specifier that pins ONE stream: a trailing numeric index.
# `:v:0` and `:0` qualify; `:v` and `:a` do not.
_PINNED = re.compile(r":\d+$")


def _is_symbolic(value: str) -> bool:
    """True if ffmpeg must resolve this value against a codec's constant table.

    Numbers (and dotted numbers like `4.1`) are parsed by the eval engine itself
    and never reach a codec lookup, so they survive a NULL codec context.
    """
    try:
        float(value)
    except ValueError:
        return True
    return False


def _flow(path: Path = DIRECT_PLAY_FLOW) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _overall_arg_nodes(flow: dict) -> list[dict]:
    return [p for p in flow.get("flowPlugins", []) if p.get("pluginName") in OVERALL_ARG_PLUGINS]


def _tokens(node: dict) -> list[str]:
    """Exactly how Tdarr splits it: `outputArguments.split(' ')`, blanks dropped."""
    raw = (node.get("inputsDB") or {}).get("outputArguments", "") or ""
    return [t for t in (tok.strip() for tok in raw.split(" ")) if t]


def _option_pairs(tokens: list[str]) -> list[tuple[str, str, str]]:
    """-> [(bare option name, stream specifier or '', value)] for every `-opt value`."""
    pairs: list[tuple[str, str, str]] = []
    for i, tok in enumerate(tokens):
        m = _OPT.match(tok)
        if not m:
            continue
        value = tokens[i + 1] if i + 1 < len(tokens) else ""
        if value.startswith("-"):  # a bare flag, no value
            value = ""
        pairs.append((m.group("name"), m.group("spec") or "", value))
    return pairs


def test_flow_json_is_parseable_and_has_the_node_this_file_guards():
    """Guard the guard: a rename must fail HERE, not silently test nothing."""
    flow = _flow()
    nodes = _overall_arg_nodes(flow)
    assert nodes, (
        "no ffmpegCommandCustomArguments node found in "
        f"{DIRECT_PLAY_FLOW.name} — this test would pass vacuously"
    )
    assert any(n.get("id") == "qfxForce8bitHigh41" for n in nodes)


def test_force_8bit_node_scopes_profile_to_output_video_stream_zero():
    """The exact 2026-08-23 regression, byte-level.

    `-profile:v high` aborts every file that carries an attached picture.
    `-profile:v:0 high` binds to the encoded video stream only.
    """
    node = next(n for n in _overall_arg_nodes(_flow()) if n["id"] == "qfxForce8bitHigh41")
    tokens = _tokens(node)

    assert "-profile:v" not in tokens, (
        "unqualified -profile:v is back. It matches EVERY output video stream, "
        "including cover art copied with -c:N copy, and ffmpeg aborts at "
        "'Error initializing output stream' with a NULL codec context. "
        "Use -profile:v:0."
    )
    assert "-profile:v:0" in tokens
    assert tokens[tokens.index("-profile:v:0") + 1] == "high"


@pytest.mark.parametrize("flow_path", sorted(FLOW_DIR.glob("*.json")))
def test_no_symbolic_overall_output_argument_is_left_unpinned(flow_path: Path):
    """The mechanism, not the symptom — applies to every flow in the dir.

    Any overall output option that ffmpeg resolves through a codec's constant
    table must name a single stream index, because the flow stream-copies foreign
    video streams and a copied stream has no codec to resolve against.
    """
    offenders = []
    for node in _overall_arg_nodes(_flow(flow_path)):
        for name, spec, value in _option_pairs(_tokens(node)):
            if name not in CODEC_PRIVATE_OPTIONS:
                continue
            if not _is_symbolic(value):
                continue  # e.g. -level:v 4.1 — parsed as a number, never hits a codec
            if not _PINNED.search(spec):
                offenders.append(f"{node.get('id')}: -{name}{spec} {value}")

    assert not offenders, (
        "codec-private named constant(s) applied to every output stream: "
        + "; ".join(offenders)
        + ". Pin the stream index (e.g. -profile:v:0) or ffmpeg aborts on any "
        "file with a second, stream-copied video stream (cover art)."
    )


def test_flow_still_stream_copies_foreign_video_streams():
    """Why the rule above is load-bearing rather than theoretical.

    Nothing in this flow removes non-primary video streams, so ffmpegCommandStart's
    map-everything + ffmpegCommandExecute's copy-everything-but-the-primary-video
    behaviour is still in force. If a future edit ever DID drop cover art, this
    test fails and the reasoning above gets re-read instead of quietly rotting.
    """
    flow = _flow()
    names = [p.get("pluginName") for p in flow.get("flowPlugins", [])]
    assert "ffmpegCommandStart" in names
    assert "ffmpegCommandExecute" in names

    removers = [
        p for p in flow.get("flowPlugins", [])
        if p.get("pluginName") == "ffmpegCommandRemoveStreamByProperty"
    ]
    # The two that exist are the unknown-codec backstops (documented in the flow
    # description as known-not-to-fire); neither targets cover art.
    for node in removers:
        assert (node.get("inputsDB") or {}).get("valuesToRemove") == "unknown", (
            f"{node.get('id')} now removes streams by something other than "
            "codec_name=unknown — re-check whether cover art still survives to "
            "the output, and whether -profile:v:0 is still needed."
        )


def test_description_quotes_the_arguments_the_node_actually_ships():
    """The description is the incident log operators read. It must not lie.

    It quoted the pre-fix argument string verbatim; that quote is now the fixed
    one, and this keeps the pair from drifting apart again.
    """
    flow = _flow()
    description = flow["description"]
    node = next(n for n in _overall_arg_nodes(flow) if n["id"] == "qfxForce8bitHigh41")

    shipped = node["inputsDB"]["outputArguments"]
    assert shipped in description, (
        "the description no longer quotes the argument string the node actually "
        f"ships ({shipped!r}) — one of the two was edited without the other"
    )
    assert "-pix_fmt:v yuv420p -profile:v high -level:v 4.1" not in description, (
        "the description still presents the pre-fix, unqualified argument string "
        "as what the node pins"
    )
    assert "COVER-ART NOTE 2026-08-23" in description, (
        "the dated operator note recording the cover-art cause is gone"
    )
