#!/usr/bin/env python3
"""scripts/maint/audio-disposition-janitor.py — sole-default audio enforcement.

Problem (found 2026-07-19): the Tdarr "QFlix Direct-Play Fix" flow's
ensure-AAC step adds an aac/en/2ch compatibility track, but ffmpeg copies the
default disposition from the source stream it encodes from — so the output
carries BOTH the original (e.g. EAC3 5.1) and the added AAC track flagged
`default`. Plex resolves the tie to the lower-index original and LIVE-
TRANSCODES audio on every browser play, ignoring the compatible AAC track
sitting right there. Confirmed by ffprobe on multiple files; pattern is
library-wide wherever the flow added an alternate track.

Policy this janitor converges on: **a file with Tdarr's dual-default pattern
gets exactly ONE default audio stream — the AAC compatibility track.** The
original higher-quality track is preserved and manually selectable; it just
stops being the auto-picked stream that forces a transcode. Files without the
dual-default pattern are never touched (narrow predicate: refuse anything we
don't positively recognize).

Fix mechanics per file: ffmpeg full stream-copy remux (`-map 0 -c copy`)
adjusting only `-disposition:a:N` flags — no re-encode, IO-bound only —
written to a HIDDEN dot-prefixed temp in the same directory (Plex and
Sonarr skip dotfiles; Tdarr's watcher does NOT — it queues '.plexmatch' —
so the vanish-retry in fix_file is the hard backstop after a visible temp
got renamed away by Tdarr mid-run on 2026-08-08), post-verified with ffprobe
(same stream count, exactly one default audio and it is the AAC target),
original mtime preserved, then atomically renamed over the original.
rename(2) over an open file is safe for an in-flight reader, but files in
active Plex sessions (via Tautulli) are skipped as politeness anyway.

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
VIDEO_EXTS = {".mkv", ".mp4"}
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


def classify_streams(streams: list):
    """Decide whether a file has the Tdarr dual-default pattern and, if so,
    return the fix plan. Pure function.

    Returns None (leave the file alone) unless ALL hold:
      - >= 2 audio streams are flagged default (the bug signature), AND
      - at least one of those defaults is an aac <=2ch compat track.
    Plan: {"target": audio-relative index to keep default (the LAST matching
    compat track — Tdarr appends its stream), "clear": audio-relative indices
    of every other default audio stream, "audio_count": N}.
    Anything not positively matching the known-bad pattern is refused — this
    janitor narrows to the bug it was built for, nothing else.
    """
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    defaults = [i for i, s in enumerate(audio)
                if (s.get("disposition") or {}).get("default")]
    if len(defaults) < 2:
        return None
    compat = [i for i in defaults if _is_compat_track(audio[i])]
    if not compat:
        return None
    target = compat[-1]                      # Tdarr appends: last compat wins
    clear = [i for i in defaults if i != target]
    return {"target": target, "clear": clear, "audio_count": len(audio)}


def build_ffmpeg_cmd(src: str, dst: str, plan: dict) -> list:
    """Disposition-only stream-copy remux command. Touches ONLY the audio
    default flags named in the plan; every stream is mapped and copied."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", src, "-map", "0", "-c", "copy"]
    for i in plan["clear"]:
        cmd += ["-disposition:a:" + str(i), "0"]
    cmd += ["-disposition:a:" + str(plan["target"]), "default", dst]
    return cmd


def verify_fixed(streams: list, expect_stream_count: int) -> bool:
    """Post-remux check: stream count preserved AND exactly one default
    audio, and that stream is the aac compat track."""
    if len(streams) != expect_stream_count:
        return False
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    defaults = [s for s in audio if (s.get("disposition") or {}).get("default")]
    return len(defaults) == 1 and _is_compat_track(defaults[0])


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

def scan_files(roots: list):
    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            warn("root missing, skipped: " + root)
            continue
        for p in sorted(rp.rglob("*")):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                yield p


class TmpVanishedError(RuntimeError):
    """The temp remux disappeared between ffmpeg closing it and verification.
    Seen 2026-08-08: Tdarr's library watcher queued a still-visible
    "<stem>.dispfix.tmp.mkv" mid-write and its replaceOriginalFile staging
    renamed it to "*.tmp" out from under the verify step. The source file is
    untouched in this scenario, so one fresh remux attempt is safe."""


def fix_file(path: Path, plan: dict) -> None:
    """Remux `path` in place per plan. Raises on any failure; never leaves a
    partial temp behind. A temp that vanishes before verify (external scanner
    interference, see TmpVanishedError) gets ONE retry with a fresh remux
    before counting as a real failure."""
    st = path.stat()
    free = shutil.disk_usage(str(path.parent)).free
    if free < st.st_size * FREE_SPACE_FACTOR:
        raise RuntimeError("insufficient free space ({} GB free)".format(
            round(free / 1024**3, 1)))
    # DOT-PREFIXED so Plex/Sonarr library scanners ignore the temp. Tdarr's
    # watcher does NOT skip dotfiles (it queues '.plexmatch'), so the vanish
    # retry below is the real backstop. A visible "<stem>.dispfix.tmp.mkv"
    # got renamed away by Tdarr mid-run on 2026-08-08 (1 FAILED of 56).
    tmp = path.with_name("." + path.stem + ".dispfix.tmp" + path.suffix)
    for attempt in (1, 2):
        try:
            _remux_once(path, tmp, plan, st)
            return
        except TmpVanishedError as exc:
            if attempt == 2:
                raise RuntimeError(str(exc) + " (persisted after retry)")
            warn("temp vanished before verify for " + str(path)
                 + " — retrying once with a fresh remux")


def _remux_once(path: Path, tmp: Path, plan: dict, st) -> None:
    """Single remux attempt: ffmpeg -> verify -> atomic replace. Raises
    TmpVanishedError when the temp is gone at verify/replace time (retryable
    by fix_file); any other failure raises straight through."""
    try:
        proc = subprocess.run(build_ffmpeg_cmd(str(path), str(tmp), plan),
                              capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg exit " + str(proc.returncode) + ": "
                               + proc.stderr.strip()[:200])
        src_count = len(ffprobe_streams(str(path)))
        try:
            tmp_streams = ffprobe_streams(str(tmp))
        except Exception:
            if not tmp.exists():        # probe failed because the temp is gone
                raise TmpVanishedError("temp remux vanished before verify: "
                                       + str(tmp))
            raise                       # real probe failure — not retryable
        if not verify_fixed(tmp_streams, src_count):
            raise RuntimeError("post-remux verification failed")
        try:
            os.utime(tmp, (st.st_atime, st.st_mtime))   # keep *arr/Plex mtime view
            os.replace(tmp, path)                       # atomic; safe for readers
        except FileNotFoundError:       # same race, later window
            raise TmpVanishedError("temp remux vanished before replace: "
                                   + str(tmp))
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
    candidates = []       # (path, plan)
    for p in scan_files(roots):
        scanned += 1
        try:
            plan = classify_streams(ffprobe_streams(str(p)))
        except Exception as exc:
            probe_failures.append({"file": str(p), "error": str(exc)[:160]})
            continue
        if plan:
            candidates.append((p, plan))

    fixed, skipped, failures = [], [], []
    if execute:
        for p, plan in candidates:
            if len(fixed) >= max_items:
                skipped.append({"file": str(p), "reason": "max-items cap"})
                continue
            if str(p) in playing:
                skipped.append({"file": str(p), "reason": "active Plex session"})
                continue
            try:
                fix_file(p, plan)
                fixed.append(str(p))
                log("FIXED " + str(p))
            except Exception as exc:
                failures.append({"file": str(p), "error": str(exc)[:200]})
                warn("fix failed for " + str(p) + ": " + str(exc)[:200])

    return {"scanned": scanned, "candidates": [str(p) for p, _ in candidates],
            "fixed": fixed, "skipped": skipped, "failures": failures,
            "probe_failures": probe_failures}


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
        "{} failure(s), {} probe-failure(s)".format(
            res["scanned"], len(res["candidates"]), len(res["fixed"]),
            len(res["skipped"]), len(res["failures"]), len(res["probe_failures"])))

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
