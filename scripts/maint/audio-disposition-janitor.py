#!/usr/bin/env python3
"""scripts/maint/audio-disposition-janitor.py — sole-default audio enforcement.

Two independent classes converge on one operator policy: **English is the
default audio on all media, forever.** Each class is a narrow, positive
predicate — anything that doesn't exactly match either one is left alone.

CLASS 1 — "dual_default" (found 2026-07-19): the Tdarr "QFlix Direct-Play
Fix" flow's ensure-AAC step adds an aac/en/2ch compatibility track, but
ffmpeg copies the default disposition from the source stream it encodes
from — so the output carries BOTH the original (e.g. EAC3 5.1) and the added
AAC track flagged `default`. Plex resolves the tie to the lower-index
original and LIVE-TRANSCODES audio on every browser play, ignoring the
compatible AAC track sitting right there. Confirmed by ffprobe on multiple
files; pattern is library-wide wherever the flow added an alternate track.
Fix: exactly ONE default audio stream survives — the AAC compatibility
track. The original higher-quality track is preserved and manually
selectable; it just stops being the auto-picked stream that forces a
transcode.

CLASS 2 — "foreign_default" (found 2026-09-13, Futurama S1 incident): all
9 of 9 Season-1 files shipped from the upstream release with the German dub
(`ger`) flagged the sole default audio stream and English (`eng`) present
but silent at track index 1, alongside 8 more languages (spa spa fre hun
ita pol por tur) — not a Tdarr artifact, a foreign-language-default
upstream release. A same-day 25-file TV sample found 2 more files with the
same shape, one of them carrying 9 distinct audio languages. Fix: when the
current default audio is CONFIRMED non-English and an English track exists,
flag the English track default instead. "Confirmed" is load-bearing — every
currently-default stream must carry an explicit language tag, and if even
one default is untagged (no `language` key, or `und`) the whole file is
refused: we never overwrite a default we can't prove is wrong. This class
does not strip or remove any track (stripping is explicitly deferred by
operator ruling) — it only moves which one is flagged default.

Both classes exclude the ANIME libraries structurally — see
EXCLUDED_ROOTS — because those libraries are jpn-only-original and jpn
being the shipped default there is correct, not a bug; "English default" is
a policy for everything else, not anime.

SCOPE (operator-reviewed 2026-09-13, MAJOR finding from review round 2): a
read-only dry-run against the box's real DEFAULT_ROOTS (Movies + TV Shows,
489 video files) found 50 candidates across 24 titles combining both
classes — not just the 9 Futurama S1 files CLASS 2 was demoed against; an
independent re-measure the next day read 44 / 20 / 488, because the nightly
dual_default pass and the retention churn move the set every day. The
figure is a SIZING, not a reviewed list: every run re-derives it live.
Operator reviewed the sizing and APPROVED the full-library first live run;
English-default-everywhere is the ruled policy for all of Movies + TV Shows
(Anime and Anime Movies excluded structurally, as above), including
currently-airing titles the sample turned up (e.g. Squid Game S03,
Star Trek: Strange New Worlds S03).

Neither class ever chooses a commentary track as the new default (a stream
with disposition.comment truthy, or a title tag containing "commentary"
case-insensitive, is excluded from every candidate pool — see
_is_commentary), and CLASS 1's compat-track selection additionally refuses
a foreign-tagged candidate rather than ever installing a non-English
default (see _classify_dual_default) — both added 2026-09-13 review round 2
after adversarial review found the original CLASS 1 logic and CLASS 2's
target selection could each install the wrong track as sole default.

Fix mechanics per file (both classes, identical mechanism): ffmpeg full
stream-copy remux (`-map 0 -c copy`)
adjusting only `-disposition:a:N` flags — no re-encode, IO-bound only. Each
touched stream's FULL existing disposition bitmask is preserved (original,
comment, dub, hearing_impaired, etc.) with only "default" added or removed
(see _disposition_value) — a bare "0"/"default" literal, which is what
ffmpeg's -disposition option actually replaces the WHOLE flag set with, was
found 2026-09-13 review round 2 to silently strip every other flag on the
two touched streams. Remux output is
written to a temp in the same directory whose name ENDS IN ".tmp" and is
also dot-prefixed (see fix_file: the ".tmp" ending is what hides it from
Tdarr, the leading dot is what hides it from Plex and Sonarr — two different
scanners, two different rules, and the earlier claim that the leading dot
covered both was wrong), post-verified with ffprobe (same stream count,
exactly one default audio and it is the AAC target), original mtime
preserved, then atomically renamed over the original. rename(2) over an open
file is safe for an in-flight reader, but files in active Plex sessions (via
Tautulli) are skipped as politeness anyway.

Deliberately STANDALONE (own module, own timer, own Kuma check, own durable
log dir) per the compartmentalization design law — independently swappable /
tunable, and portable as-is to the upcoming qflix2 server (pure python3 +
ffmpeg/ffprobe, zero Tdarr coupling).

Modes: default = DRY-RUN (scan + plan, mutate nothing). --execute arms the
remux. --emit-json prints the run doc. Kuma heartbeat "QFlix Audio
Disposition" pushes up on success (dry-run included, reaper convention) and
down on partial failures.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path nudge (reaper convention) so `from lib.secrets import ...` resolves.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent                       # scripts/maint
for _p in (str(_HERE),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.secrets import read_secret  # noqa: E402

DEFAULT_ROOTS = [
    str(Path.home() / "media" / "Movies"),
    str(Path.home() / "media" / "TV Shows"),
]
# STRUCTURAL exclusion (2026-09-13, foreign_default rollout): anime libraries
# are jpn-only-original — jpn as the shipped default there is CORRECT, not
# the bug either class targets. "English default, forever" is a policy for
# the rest of the library, not anime. Enforced inside scan_files() itself
# (both at the root level AND per-file, see _is_excluded) so it holds no
# matter what --roots is passed — DEFAULT_ROOTS omitting these two dirs is
# not sufficient on its own, because it is trivially bypassed by a flag.
EXCLUDED_ROOTS = [
    str(Path.home() / "media" / "Anime"),
    str(Path.home() / "media" / "Anime Movies"),
]
VIDEO_EXTS = {".mkv", ".mp4"}
# Output muxer per source container. REQUIRED, not optional: the temp in
# fix_file no longer ends in a media extension, and ffmpeg picks its muxer from
# the output FILENAME — without an explicit -f it dies "Unable to choose an
# output format for '...tmp'" and every single file fails. Verified on the box
# 2026-08-23 against ffmpeg 7.1.5 for BOTH legs (-f matroska and -f mp4 mux
# cleanly to a .tmp; ffprobe and os.replace are extension-agnostic).
# Total over VIDEO_EXTS by construction — widening one without the other is a
# KeyError on every candidate, so keep these two constants in the same commit.
MUXER = {".mkv": "matroska", ".mp4": "mp4"}
DEFAULT_MAX_ITEMS = 25
FREE_SPACE_FACTOR = 1.15       # temp remux needs ~file-size free on the fs

KUMA_BASE = os.environ.get("KUMA_BASE", "http://127.0.0.1:42005")
KUMA_PUSH_KEY = "qflix-audio-disposition"   # key in ~/secrets/kuma-push-tokens.json

EXIT_OK = 0
EXIT_PARTIAL = 1

# ===========================================================================
# Logging — journal + durable per-day logfile (reaper convention: journald on
# this shared box is permission-restricted/rotation-prone, the logfile is the
# reliable record). Best-effort: file trouble degrades to journal-only.
# ===========================================================================
_LOG_FH = None
_LOG_RETENTION_DAYS = 30


def _setup_file_log() -> None:
    global _LOG_FH
    try:
        log_dir = Path(os.environ.get(
            "QFLIX_AUDIODISP_LOG_DIR",
            str(Path.home() / ".opt" / "maint" / "audio-disposition"),
        ))
        log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        _LOG_FH = open(log_dir / ("audiodisp-" + day + ".log"), "a", encoding="utf-8")
        cutoff = datetime.now(timezone.utc).timestamp() - _LOG_RETENTION_DAYS * 86400
        for old in log_dir.glob("audiodisp-*.log"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
    except Exception:
        _LOG_FH = None


def _file_log(line: str) -> None:
    if _LOG_FH is None:
        return
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        _LOG_FH.write(ts + " " + line + "\n")
        _LOG_FH.flush()
    except Exception as _exc:
        sys.stderr.write("audio-disposition-janitor.py: durable log write failed (best-effort, continuing): "
                         + repr(_exc) + "\n")


def log(msg: str) -> None:
    line = "[audio-disposition] " + msg
    print(line, flush=True)
    _file_log(line)


def warn(msg: str) -> None:
    line = "[audio-disposition] WARNING: " + msg
    print(line, file=sys.stderr, flush=True)
    _file_log(line)


# ===========================================================================
# Best-effort notify + Kuma (never raise into the main flow).
# ===========================================================================

def _notify(msg: str, level: str = "info") -> None:
    try:
        from lib.notify import notify
        notify(msg, level)
    except Exception as exc:
        warn("notify unavailable (non-fatal): " + str(exc))


def _read_kuma_token() -> str:
    env = os.environ.get("QFLIX_AUDIODISP_KUMA_TOKEN")
    if env:
        return env
    try:
        path = Path.home() / "secrets" / "kuma-push-tokens.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get(KUMA_PUSH_KEY, "") or ""
    except Exception:
        return ""


def _push_kuma(status: str, msg: str) -> None:
    token = _read_kuma_token()
    if not token:
        # Loud skip (lesson of the 2026-07-19 reaper red-loop: a silent
        # missing-token skip is indistinguishable from a dead job).
        warn("no Kuma push token under '" + KUMA_PUSH_KEY + "' — heartbeat NOT pushed")
        return
    qs = urllib.parse.urlencode({"status": status, "msg": msg[:200]})
    url = KUMA_BASE + "/api/push/" + token + "?" + qs
    try:
        urllib.request.urlopen(url, timeout=5).read()
    except Exception as exc:
        warn("Kuma push failed (non-fatal): " + str(exc))


# ===========================================================================
# ffprobe / classification — pure logic kept import-safe for unit tests.
# ===========================================================================

def ffprobe_streams(path: str) -> list:
    """Return the ffprobe streams array for `path`. Raises on probe failure."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("ffprobe exit " + str(proc.returncode) + ": "
                           + proc.stderr.strip()[:200])
    return json.loads(proc.stdout).get("streams") or []


def _is_compat_track(stream: dict) -> bool:
    """The Tdarr-added compatibility track: aac, stereo-or-mono."""
    return (stream.get("codec_name") == "aac"
            and int(stream.get("channels") or 0) <= 2)


def _lang(stream: dict):
    """Normalized language tag for a stream, or None if we don't actually
    know. ffmpeg/mkvmerge write "no tag at all" and the literal ISO 639-2
    "und" (undetermined) to mean the same thing — both come back None here
    on purpose, because foreign_default's whole safety property is refusing
    to touch a default it can't PROVE is non-English (see classify_streams).
    """
    tag = (stream.get("tags") or {}).get("language")
    if not tag:
        return None
    tag = tag.strip().lower()
    return None if tag in ("", "und") else tag


def _is_eng(lang) -> bool:
    """Spec explicitly accepts both the 3-letter ("eng") and 2-letter ("en")
    ISO forms — both appear in the wild depending on the muxer that wrote
    the file."""
    return lang in ("eng", "en")


def _is_commentary(stream: dict) -> bool:
    """Operator ruling (2026-09-13, review round 2): a commentary/descriptive
    track must never be chosen as the new default in EITHER class, even when
    it is otherwise English-tagged and/or aac<=2ch (compat-shaped). Two
    independent signals, either one disqualifies:
      - disposition.comment is the ffmpeg/mkvmerge flag muxers set for
        director/cast commentary tracks.
      - a title tag containing "commentary" (case-insensitive) catches
        releases that carry the intent in metadata text but never set the
        disposition bit (seen in the wild more often than the bit itself).
    Excluded from every candidate pool this module builds — never just
    filtered out of the *chosen* result, because an unfiltered pool one
    index away from being chosen is one refactor away from being chosen."""
    disp = stream.get("disposition") or {}
    if disp.get("comment"):
        return True
    title = (stream.get("tags") or {}).get("title") or ""
    return "commentary" in title.lower()


def _classify_dual_default(audio: list, defaults: list, refusals=None):
    """CLASS 1 (2026-07-19). See module docstring.

    Language + commentary safety (2026-09-13 review round 2, BLOCKER):
    the original version picked ANY aac<=2ch default track as the compat
    target, including one tagged for a foreign language or flagged/titled
    commentary — on a file with e.g. a French aac/2ch default alongside an
    English EAC3 default, the old code installed the French track as the
    SOLE default end-to-end. Now: a compat candidate is only eligible when
    its language tag is either absent/und (we don't know it's foreign — the
    original Tdarr-added track is usually untagged or eng) or explicitly
    eng/en, AND it is not a commentary track. If every aac<=2ch default is
    disqualified this way, refuse the whole file rather than guess — this
    is a deliberate, counted-and-named refusal (classify_streams still
    tries CLASS 2 next, which can rescue a file this refusal drops if an
    untouched English track exists elsewhere to promote instead)."""
    if len(defaults) < 2:
        return None
    compat_all = [i for i in defaults
                  if _is_compat_track(audio[i]) and not _is_commentary(audio[i])]
    if not compat_all:
        return None
    compat = [i for i in compat_all
              if _lang(audio[i]) is None or _is_eng(_lang(audio[i]))]
    if not compat:
        return _refuse(refusals, "dual_default:every-compat-default-is-foreign")
    target = compat[-1]                      # Tdarr appends: last compat wins
    clear = [i for i in defaults if i != target]
    return {"target": target, "clear": clear, "audio_count": len(audio),
            "kind": "dual_default"}


def _classify_foreign_default(audio: list, defaults: list, refusals=None):
    """CLASS 2 (2026-09-13, Futurama S1 incident). See module docstring.

    Fires iff ALL hold:
      - >= 1 audio stream is tagged eng/en, AND
      - there is at least one current default audio stream, AND
      - every current default carries an explicit (non-und) language tag
        — one untagged default and we refuse the WHOLE file, no partial
        credit, because "untagged" means we cannot prove it isn't already
        English or something we shouldn't touch, AND
      - not EVERY tagged default is eng/en (all-English defaults means
        nothing foreign to fix). Mixed defaults (some eng, some foreign, all
        tagged) ARE claimed: the foreign defaults are cleared and an English
        default survives — see the MIXED block below (Akira, 2026-09-15).
    Target = an eng track that is aac<=2ch (compat) if one exists, else the
    first eng track by index. Clear = every current default (all proven
    non-eng by the checks above).

    Commentary safety (2026-09-13 review round 2, MAJOR): eng_indices — the
    ENTIRE candidate pool, both for the compat-preference rule and the
    index-order fallback — excludes commentary tracks (see _is_commentary).
    Without this an eng-tagged director's-commentary track could outrank
    the real English dialogue track, especially under the "prefer compat"
    rule if the commentary track happens to be aac<=2ch shaped.
    """
    if not defaults:
        return None
    eng_indices = [i for i, s in enumerate(audio)
                   if _is_eng(_lang(s)) and not _is_commentary(s)]
    if not eng_indices:
        if any(_is_eng(_lang(s)) for s in audio):
            # English exists but ONLY as commentary: a named refusal, not a
            # healthy no-op (telemetry must tell the two apart).
            return _refuse(refusals, "foreign_default:only-english-is-commentary")
        return None
    default_langs = []
    for i in defaults:
        lang = _lang(audio[i])
        if lang is None:
            return _refuse(refusals, "foreign_default:untagged-default")
        default_langs.append(lang)
    if all(_is_eng(lang) for lang in default_langs):
        return None                # every default is already English — no-op
    if any(_is_eng(lang) for lang in default_langs):
        # MIXED defaults (2026-09-15, Akira e2e test: opus/2ch jpn default=1 AND
        # opus/2ch eng default=1, neither aac so CLASS 1 cannot claim it).
        # Plex breaks a default tie by the LOWER index, so the member gets the
        # foreign track even though an English default is flagged right
        # beside it. Policy is "English is the default": keep an English
        # default, clear the foreign ones. Every default is tagged (checked
        # above); the survivor must be a non-commentary eng track, preferring
        # one that is already default, then aac<=2ch, then lowest index.
        eng_defaults = [i for i in defaults if i in eng_indices]
        if not eng_defaults:
            return _refuse(refusals, "foreign_default:mixed-defaults-only-commentary-english")
        compat_eng = [i for i in eng_defaults if _is_compat_track(audio[i])]
        target = compat_eng[0] if compat_eng else eng_defaults[0]
        clear = [i for i in defaults if i != target]
        return {"target": target, "clear": clear, "audio_count": len(audio),
                "kind": "foreign_default"}
    compat_eng = [i for i in eng_indices if _is_compat_track(audio[i])]
    target = compat_eng[0] if compat_eng else eng_indices[0]
    return {"target": target, "clear": list(defaults), "audio_count": len(audio),
            "kind": "foreign_default"}


def classify_streams(streams: list, refusals=None):
    """Decide whether a file matches either recognized bad-default pattern
    and, if so, return the fix plan. Pure function; tries dual_default
    (CLASS 1) first, then foreign_default (CLASS 2) — the two predicates are
    disjoint in practice (dual_default requires >=2 defaults with a compat
    track among them; foreign_default requires all defaults to be tagged
    non-eng, which a Tdarr-added aac/en compat default already contradicts),
    but the order is fixed here so behaviour is deterministic either way.

    Plan shape (both kinds): {"target": audio-relative index to make the
    sole default, "clear": audio-relative indices of every default to
    clear, "audio_count": N, "kind": "dual_default" | "foreign_default"}.
    Anything not positively matching a known pattern is refused — this
    janitor narrows to bugs it was built for, nothing else.
    """
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    defaults = [i for i, s in enumerate(audio)
                if (s.get("disposition") or {}).get("default")]
    plan = _classify_dual_default(audio, defaults, refusals)
    if plan is not None:
        return plan
    return _classify_foreign_default(audio, defaults, refusals)


def _refuse(refusals, reason: str):
    """Record WHY a classifier declined, when the caller asked. A refusal
    is a named safety decision; without this it read identically to a
    healthy file in the JSON and the durable log (round-2 review)."""
    if refusals is not None:
        refusals.append(reason)
    return None


# ffmpeg's -disposition:a:N option REPLACES the entire flag set on that
# stream — it is not additive. Naively passing the bare literal "0" or
# "default" (as this module did before 2026-09-13 review round 2, MAJOR)
# silently wipes every OTHER flag the source stream carried: original,
# comment, dub, hearing_impaired, visual_impaired, karaoke, forced, lyrics,
# descriptions, etc. Confirmed on a real remux: a source with
# default+original on track 0 came back default:0,original:0 — "original"
# vanished even though nothing about this janitor's job description ever
# said to touch it. _disposition_value rebuilds the FULL flag string from
# the stream's own (already-probed) disposition dict, adding/removing only
# "default" — ffmpeg accepts a "+"-joined flag list (verified against
# ffmpeg 8.1.1: "-disposition:a:1 comment+default" round-trips both bits).
def _disposition_value(stream: dict, want_default: bool) -> str:
    """Build the full -disposition:a:N argument for `stream`, preserving
    every flag ffprobe reported truthy except overriding "default" per
    `want_default`. ffprobe's disposition JSON keys and ffmpeg's
    -disposition flag names are the same strings (both walk the same
    libavformat enum), so any truthy key here round-trips as a flag name
    ffmpeg understands. Falls back to a bare "0" only when the result would
    otherwise be empty — ffmpeg rejects an empty -disposition value."""
    disp = dict(stream.get("disposition") or {})
    disp["default"] = 1 if want_default else 0
    flags = [name for name, val in disp.items() if val]
    return "+".join(flags) if flags else "0"


def build_ffmpeg_cmd(src: str, dst: str, plan: dict, streams: list | None = None) -> list:
    """Disposition-only stream-copy remux command. Touches ONLY the audio
    default flags named in the plan; every stream is mapped and copied.

    `streams` (optional, the SOURCE ffprobe streams array) lets each touched
    track's non-default disposition flags survive the edit — see
    _disposition_value. Omitting it (back-compat for the handful of
    existing direct callers/tests that only care about the "default" bit)
    degrades to the pre-2026-09-13 bare "0"/"default" behaviour, since there
    is no source flag data to preserve.

    `-f` is derived from the SOURCE extension, never from `dst`: dst ends in
    ".tmp" so ffmpeg cannot infer the muxer at all (see MUXER)."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", src, "-map", "0", "-c", "copy"]
    audio = [s for s in (streams or []) if s.get("codec_type") == "audio"]
    for i in plan["clear"]:
        stream = audio[i] if i < len(audio) else {}
        cmd += ["-disposition:a:" + str(i), _disposition_value(stream, want_default=False)]
    target = plan["target"]
    target_stream = audio[target] if target < len(audio) else {}
    cmd += ["-disposition:a:" + str(target), _disposition_value(target_stream, want_default=True),
            "-f", MUXER[Path(src).suffix.lower()], dst]
    return cmd


def verify_fixed(streams: list, expect_stream_count: int,
                  kind: str = "dual_default") -> bool:
    """Post-remux check: stream count preserved AND exactly one default
    audio stream, AND that stream matches what `kind` promised:
      - dual_default:    the sole default is the aac<=2ch compat track AND
                         (2026-09-13 review round 2, BLOCKER) it is not
                         provably foreign — its language tag, if present and
                         not und, must be eng/en. An untagged compat track
                         still passes (matches the original Tdarr use case,
                         which carries no language tags at all); a track
                         explicitly tagged e.g. "fre" never does, even
                         though it is aac<=2ch-shaped. This closes the same
                         hole as _classify_dual_default's compat filter —
                         belt AND suspenders, since verify_fixed is the last
                         gate before an atomic replace lands on disk.
      - foreign_default: the sole default is tagged eng/en.
    `kind` defaults to "dual_default" for backward compatibility with
    existing callers/tests that predate the foreign_default class."""
    if len(streams) != expect_stream_count:
        return False
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    defaults = [s for s in audio if (s.get("disposition") or {}).get("default")]
    if len(defaults) != 1:
        return False
    sole = defaults[0]
    if kind == "foreign_default":
        return _is_eng(_lang(sole))
    if not _is_compat_track(sole):
        return False
    lang = _lang(sole)
    return lang is None or _is_eng(lang)


# ===========================================================================
# Tautulli active-session guard (politeness skip; degrades to empty set).
# ===========================================================================

def active_file_paths() -> set:
    try:
        port = read_secret("tautulli.port")
        key = read_secret("tautulli.key")
        # Tautulli is served under the /tautulli urlbase (there is no
        # tautulli.urlbase secret) — the bare /api/v2 path 404s, which silently
        # disabled this active-session guard on EVERY run (audit 2026-07-27):
        # active_file_paths() caught the 404 and returned an empty set, so files
        # were remuxed even while a viewer was streaming them. Every other
        # Tautulli caller in the repo (app_status/functional-audit/playback-audit
        # /newsletter) already uses this /tautulli base.
        url = ("http://127.0.0.1:" + port + "/tautulli/api/v2?"
               + urllib.parse.urlencode({"apikey": key, "cmd": "get_activity"}))
        payload = json.loads(urllib.request.urlopen(url, timeout=10).read())
        sessions = ((payload.get("response") or {}).get("data") or {}).get("sessions") or []
        return {s.get("file") for s in sessions if s.get("file")}
    except Exception as exc:
        warn("Tautulli activity unavailable — active-session skip disabled: " + str(exc))
        return set()


# ===========================================================================
# Scan + fix
# ===========================================================================

def _is_excluded(path: Path) -> bool:
    """True if `path` sits under any EXCLUDED_ROOTS entry. Checked at BOTH
    the root level and the per-file level in scan_files — root-level alone
    is not enough because a caller could pass an ANCESTOR of an excluded
    directory (e.g. the whole media/ parent) and the anime subtree would
    still get walked and yielded without a second check here."""
    for ex in EXCLUDED_ROOTS:
        try:
            path.resolve().relative_to(Path(ex).resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def scan_files(roots: list):
    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            warn("root missing, skipped: " + root)
            continue
        if _is_excluded(rp):
            warn("root is an excluded anime library (jpn-only-original, "
                 "not in scope), skipped: " + root)
            continue
        for p in sorted(rp.rglob("*")):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not _is_excluded(p):
                yield p


class TmpVanishedError(RuntimeError):
    """The temp remux disappeared between ffmpeg closing it and verification.
    Seen 2026-08-08: Tdarr's library watcher queued a still-visible
    "<stem>.dispfix.tmp.mkv" mid-write and its replaceOriginalFile staging
    renamed it to "*.tmp" out from under the verify step. The source file is
    untouched in this scenario, so one fresh remux attempt is safe.

    KEPT ON PURPOSE but expected to be DEAD for Tdarr since 2026-08-23: the
    temp no longer carries a media extension, so Tdarr cannot admit it at all.
    It stays as a cheap backstop against any OTHER scanner, which also means a
    clean run is no longer evidence that this retry still works."""


def fix_file(path: Path, plan: dict) -> bool:
    """Remux `path` in place per plan. Raises on any failure; never leaves a
    partial temp behind. A temp that vanishes before verify (external scanner
    interference, see TmpVanishedError) gets ONE retry with a fresh remux
    before counting as a real failure."""
    st = path.stat()
    free = shutil.disk_usage(str(path.parent)).free
    if free < st.st_size * FREE_SPACE_FACTOR:
        raise RuntimeError("insufficient free space ({} GB free)".format(
            round(free / 1024**3, 1)))
    # Temp name has TWO independent guards, because two different scanners use
    # two different rules:
    #   leading "."  -> Plex and Sonarr skip dotfiles.
    #   ends ".tmp"  -> Tdarr admits a file to FileJSONDB purely by
    #                   path.extname() against the library containerFilter
    #                   (mkv,mp4,mov,m4v,mpg,mpeg,avi,flv,webm,wmv,m2ts,ts).
    #                   ".tmp" is in no containerFilter, so it is never indexed.
    # CORRECTION (2026-08-23): the old comment here claimed the leading dot was
    # the guard and Tdarr merely ignored it. Tdarr has NO hidden-file rule at
    # all — it was the trailing ".mkv" that got the temp admitted. The old name
    # ".<stem>.dispfix.tmp.mkv" kept the media suffix, Tdarr's folder watcher
    # (30s poll) indexed it mid-write, and os.replace then moved the file out
    # from under the record: a GHOST stuck at HealthCheck=Queued forever with a
    # terminal TranscodeDecisionMaker=Transcode error, which pinned both tdarr
    # canaries permanently red. Exactly one such ghost existed in 465 records.
    # Dropping path.suffix is what actually closes it. Requires the explicit
    # -f in build_ffmpeg_cmd — ffmpeg cannot guess a muxer from ".tmp".
    tmp = path.with_name("." + path.stem + ".dispfix.tmp")
    for attempt in (1, 2):
        try:
            return _remux_once(path, tmp, plan, st)   # True = a hardlink was detached
        except TmpVanishedError as exc:
            if attempt == 2:
                raise RuntimeError(str(exc) + " (persisted after retry)")
            warn("temp vanished before verify for " + str(path)
                 + " — retrying once with a fresh remux")


def _remux_once(path: Path, tmp: Path, plan: dict, st) -> bool:
    """Single remux attempt: ffmpeg -> verify -> atomic replace. Raises
    TmpVanishedError when the temp is gone at verify/replace time (retryable
    by fix_file); any other failure raises straight through.

    Probes the SOURCE before invoking ffmpeg (reordered 2026-09-13, MAJOR
    fix) so build_ffmpeg_cmd can read each touched stream's existing
    disposition flags and preserve them (see _disposition_value) — this is
    a pure remux, the source is never mutated until the final os.replace,
    so probing it before vs. after ffmpeg runs observes the same bytes
    either way; probing first is what lets the command carry the flags."""
    try:
        src_streams = ffprobe_streams(str(path))
        src_count = len(src_streams)
        proc = subprocess.run(build_ffmpeg_cmd(str(path), str(tmp), plan, src_streams),
                              capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg exit " + str(proc.returncode) + ": "
                               + proc.stderr.strip()[:200])
        try:
            tmp_streams = ffprobe_streams(str(tmp))
        except Exception:
            if not tmp.exists():        # probe failed because the temp is gone
                raise TmpVanishedError("temp remux vanished before verify: "
                                       + str(tmp))
            raise                       # real probe failure — not retryable
        if not verify_fixed(tmp_streams, src_count, plan.get("kind", "dual_default")):
            raise RuntimeError("post-remux verification failed")
        try:
            os.utime(tmp, (st.st_atime, st.st_mtime))   # keep *arr/Plex mtime view
            os.replace(tmp, path)                       # atomic; safe for readers
        except FileNotFoundError:       # same race, later window
            raise TmpVanishedError("temp remux vanished before replace: "
                                   + str(tmp))
        # The remux is a NEW inode. If the library file was a hardlink to a
        # qBit seed copy (the *arr import path), that link is now detached:
        # the torrent keeps its own bytes until torrent-janitor / ratio
        # removes it, so disk transiently doubles for this one file. This is
        # the same effect every Tdarr re-encode has had on every file since
        # 2026-08-20 (universal h264 policy) and hardlink-integrity only
        # grades imports, so nothing pages — but it must be VISIBLE, not
        # silent (round-2 remux-safety review, 2026-09-14): returned to run()
        # and counted in the durable log and JSON.
        return st.st_nlink > 1
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def run(*, roots: list, execute: bool, max_items: int) -> dict:
    playing = active_file_paths() if execute else set()
    scanned = 0
    probe_failures = []
    refused = []          # {file, reason}: a deliberate safety refusal is
                          # not "nothing to fix" — telemetry must tell them
                          # apart (round-2 review, 2026-09-14)
    candidates = []       # (path, plan)
    for p in scan_files(roots):
        scanned += 1
        reasons = []
        try:
            plan = classify_streams(ffprobe_streams(str(p)), refusals=reasons)
        except Exception as exc:
            probe_failures.append({"file": str(p), "error": str(exc)[:160]})
            continue
        if plan:
            candidates.append((p, plan))
        elif reasons:
            refused.append({"file": str(p), "reason": ";".join(reasons)})

    fixed, skipped, failures, hardlink_detached = [], [], [], []
    if execute:
        for p, plan in candidates:
            if len(fixed) >= max_items:
                skipped.append({"file": str(p), "reason": "max-items cap"})
                continue
            if str(p) in playing:
                skipped.append({"file": str(p), "reason": "active Plex session"})
                continue
            try:
                if fix_file(p, plan):
                    hardlink_detached.append(str(p))
                    log("hardlink detached by remux (seed copy keeps its own bytes): " + str(p))
                fixed.append(str(p))
                log("FIXED " + str(p))
            except Exception as exc:
                failures.append({"file": str(p), "error": str(exc)[:200]})
                warn("fix failed for " + str(p) + ": " + str(exc)[:200])

    return {"scanned": scanned, "candidates": [str(p) for p, _ in candidates],
            "fixed": fixed, "skipped": skipped, "failures": failures,
            "probe_failures": probe_failures, "refused": refused,
            "hardlink_detached": hardlink_detached}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true",
                    help="arm the remux; default is a read-only dry-run plan")
    ap.add_argument("--emit-json", action="store_true")
    ap.add_argument("--roots", nargs="*", default=DEFAULT_ROOTS)
    ap.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    args = ap.parse_args()

    _setup_file_log()
    mode = "EXECUTE" if args.execute else "DRY-RUN"
    log("--- audio-disposition-janitor ({}) roots={} max-items={} ---".format(
        mode, args.roots, args.max_items))

    res = run(roots=args.roots, execute=args.execute, max_items=args.max_items)
    log("scanned {} file(s): {} candidate(s), {} fixed, {} skipped, "
        "{} failure(s), {} probe-failure(s), {} refused, {} hardlink-detached".format(
            res["scanned"], len(res["candidates"]), len(res["fixed"]),
            len(res["skipped"]), len(res["failures"]), len(res["probe_failures"]),
            len(res["refused"]), len(res["hardlink_detached"])))
    for r in res["refused"]:
        log("REFUSED {} ({})".format(r["file"], r["reason"]))

    if args.emit_json:
        json.dump(res, sys.stdout, default=str)
        sys.stdout.write("\n")

    hard_failures = res["failures"] or res["probe_failures"]
    if not args.execute:
        _push_kuma("up", "dry-run: {} candidate(s) of {} scanned".format(
            len(res["candidates"]), res["scanned"]))
        return EXIT_OK
    if hard_failures:
        msg = "{} fixed, {} FAILED of {} candidate(s)".format(
            len(res["fixed"]), len(res["failures"]) + len(res["probe_failures"]),
            len(res["candidates"]))
        _notify("audio-disposition: " + msg, "error")
        _push_kuma("down", msg)
        return EXIT_PARTIAL
    if res["fixed"]:
        _notify("audio-disposition: fixed sole-default audio on {} file(s)".format(
            len(res["fixed"])), "info")
    _push_kuma("up", "{} fixed, {} candidate(s), {} scanned".format(
        len(res["fixed"]), len(res["candidates"]), res["scanned"]))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
