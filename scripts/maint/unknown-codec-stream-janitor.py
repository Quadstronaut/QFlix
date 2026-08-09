#!/usr/bin/env python3
"""scripts/maint/unknown-codec-stream-janitor.py — strip unmappable streams.

Problem (found 2026-08-06, root-caused 2026-08-08): The Marshals S01E01-E13
parked permanently in Tdarr with TranscodeDecisionMaker=Transcode error. Every
episode carries one placeholder subtitle stream ffprobe cannot identify at all
(codec_name comes back "unknown" from a direct ffprobe run; Tdarr's own
scanner stores that as a MISSING codec_name key rather than the literal
string, confirmed from a live Tdarr JobReport spawnArgs dump).

Tdarr's "QFlix Direct-Play Fix" flow builds its ffmpeg command with
`ffmpegCommandStart`, which maps EVERY stream and defaults to `-c:N copy`
(see ffmpegCommandExecute's shouldAddCopyCodec). The matroska muxer then
aborts the whole job instantly with "Subtitle codec 0 is not supported" the
moment it hits the one it can't identify — confirmed by hand-building and
running the exact ffmpeg command Tdarr spawns (both from a raw ffprobe read
and from a live JobReport). Tdarr does not auto-retry a Transcode error state,
so the file parks forever.

Why this janitor lives OUTSIDE the Tdarr flow (unlike a Flow-node fix): the
two Tdarr-native mechanisms that could plausibly filter a stream by codec
both turned out to be broken in this Tdarr build (2.17.01), confirmed by
direct testing, not assumption:
  - Community plugin "Remove Stream By Property" silently no-ops on any
    property that's undefined/null (by design) - and codec_name IS undefined
    for these streams (see above), so it can never select them.
  - Community plugin "Custom JS Function" (Tdarr's only mechanism for
    embedding logic directly in a Flow document, which persists durably
    unlike bundled plugin files) has its own upstream bug: it writes the
    user's script to a workDir-relative path and then `require()`s that same
    relative path, which Node.js resolves module-relative rather than
    cwd-relative, so it CANNOT find its own generated file. Verified live
    2026-08-08 (this bled onto 18 unrelated Vanderpump Rules episodes for
    ~7 minutes before rollback - see git history same day).
  - Patching ffmpegCommandStart's bundled JS directly (the same pattern this
    repo already uses for Tdarr_Node/worker1.js) does not stick: Tdarr_Node
    resets its local FlowPlugins/CommunityFlowPlugins tree back to pristine
    on every restart/reconnect, independent of what Tdarr_Server serves.

So: fix the FILE, not the flow. Exactly the audio-disposition-janitor
precedent (2026-07-19) - remux out only the unmappable stream(s), same
narrow-predicate / stream-copy / atomic-replace safety machinery, then nudge
Tdarr to retry via a live (non-restart) FileJSONDB cruddb update.

Deliberately STANDALONE (own module, own timer, own Kuma check, own durable
log dir) per the compartmentalization design law.

Modes: default = DRY-RUN (scan + plan, mutate nothing). --execute arms the
remux. --emit-json prints the run doc. Kuma heartbeat "QFlix Unknown Codec
Stream" pushes up on success (dry-run included, reaper convention) and down
on partial failures.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
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
    str(Path.home() / "media" / "Anime"),
]
VIDEO_EXTS = {".mkv", ".mp4"}
DEFAULT_MAX_ITEMS = 25
FREE_SPACE_FACTOR = 1.15       # temp remux needs ~file-size free on the fs

TDARR_PORT = int(os.environ.get("QFLIX_TDARR_PORT", "42018"))

KUMA_BASE = os.environ.get("KUMA_BASE", "http://127.0.0.1:42005")
KUMA_PUSH_KEY = "qflix-unknown-codec-stream"   # key in ~/secrets/kuma-push-tokens.json

EXIT_OK = 0
EXIT_PARTIAL = 1

# ===========================================================================
# Logging — journal + durable per-day logfile (reaper convention).
# ===========================================================================
_LOG_FH = None
_LOG_RETENTION_DAYS = 30


def _setup_file_log() -> None:
    global _LOG_FH
    try:
        log_dir = Path(os.environ.get(
            "QFLIX_UNKCODEC_LOG_DIR",
            str(Path.home() / ".opt" / "maint" / "unknown-codec-stream"),
        ))
        log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        _LOG_FH = open(log_dir / ("unkcodec-" + day + ".log"), "a", encoding="utf-8")
        cutoff = datetime.now(timezone.utc).timestamp() - _LOG_RETENTION_DAYS * 86400
        for old in log_dir.glob("unkcodec-*.log"):
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
        sys.stderr.write("unknown-codec-stream-janitor.py: durable log write failed "
                          "(best-effort, continuing): " + repr(_exc) + "\n")


def log(msg: str) -> None:
    line = "[unknown-codec-stream] " + msg
    print(line, flush=True)
    _file_log(line)


def warn(msg: str) -> None:
    line = "[unknown-codec-stream] WARNING: " + msg
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
    env = os.environ.get("QFLIX_UNKCODEC_KUMA_TOKEN")
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


def _is_unmappable(stream: dict) -> bool:
    """A stream ffmpeg cannot map/copy into ANY output container: ffprobe
    could not identify its codec at all. Raw ffprobe reports this as the
    literal string 'unknown'; some code paths (including Tdarr's own
    scanner) omit the key entirely instead - treat both the same way.
    Deliberately narrow: never true for a real codec name, so a legitimate
    audio/subtitle/video track is never a match."""
    name = stream.get("codec_name")
    return name is None or name == "" or name == "unknown"


def classify_streams(streams: list):
    """Return the sorted list of stream indices that are unmappable, or None
    if the file is clean. Pure function."""
    bad = [s.get("index") for s in streams if _is_unmappable(s)]
    bad = [i for i in bad if i is not None]
    if not bad:
        return None
    return sorted(bad)


def build_ffmpeg_cmd(src: str, dst: str, bad_indices: list) -> list:
    """Map everything, stream-copy, then explicitly exclude the unmappable
    indices. Negative maps must follow the wildcard `-map 0` per ffmpeg's own
    stream-map ordering rules."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", src, "-map", "0", "-c", "copy"]
    for idx in bad_indices:
        cmd += ["-map", "-0:" + str(idx)]
    cmd += [dst]
    return cmd


def verify_fixed(streams: list, expect_stream_count: int) -> bool:
    """Post-remux check: exactly the expected number of streams remain, and
    none of them are still unmappable."""
    if len(streams) != expect_stream_count:
        return False
    return not any(_is_unmappable(s) for s in streams)


# ===========================================================================
# Tautulli active-session guard (politeness skip; degrades to empty set).
# Same approach as audio-disposition-janitor - see its 2026-07-27 urlbase note.
# ===========================================================================

def active_file_paths() -> set:
    try:
        port = read_secret("tautulli.port")
        key = read_secret("tautulli.key")
        url = ("http://127.0.0.1:" + port + "/tautulli/api/v2?"
               + urllib.parse.urlencode({"apikey": key, "cmd": "get_activity"}))
        payload = json.loads(urllib.request.urlopen(url, timeout=10).read())
        sessions = ((payload.get("response") or {}).get("data") or {}).get("sessions") or []
        return {s.get("file") for s in sessions if s.get("file")}
    except Exception as exc:
        warn("Tautulli activity unavailable — active-session skip disabled: " + str(exc))
        return set()


# ===========================================================================
# Tdarr nudge — live (non-restart) FileJSONDB update via cruddb, so the file
# gets picked up on Tdarr's normal cadence without needing a service restart.
# Best-effort: if Tdarr's API is unreachable, the fixed file just waits for
# Tdarr's own folder-watcher / next scan to notice the on-disk change.
# ===========================================================================

def requeue_in_tdarr(path: str) -> bool:
    body = json.dumps({"data": {
        "collection": "FileJSONDB", "mode": "update", "docID": path,
        "obj": {"TranscodeDecisionMaker": "Queued", "lastTranscodeDate": 0},
    }}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:" + str(TDARR_PORT) + "/api/v2/cruddb",
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError) as exc:
        warn("Tdarr requeue nudge failed for " + path + " (non-fatal, Tdarr's own "
             "scan will pick up the fixed file eventually): " + str(exc))
        return False


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


def fix_file(path: Path, bad_indices: list) -> None:
    """Remux `path` in place, dropping bad_indices. Raises on any failure;
    never leaves a partial temp behind."""
    st = path.stat()
    free = shutil.disk_usage(str(path.parent)).free
    if free < st.st_size * FREE_SPACE_FACTOR:
        raise RuntimeError("insufficient free space ({} GB free)".format(
            round(free / 1024**3, 1)))
    tmp = path.with_name(path.stem + ".unkcodecfix.tmp" + path.suffix)
    try:
        src_count = len(ffprobe_streams(str(path)))
        proc = subprocess.run(build_ffmpeg_cmd(str(path), str(tmp), bad_indices),
                              capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg exit " + str(proc.returncode) + ": "
                               + proc.stderr.strip()[:200])
        expect_count = src_count - len(bad_indices)
        if not verify_fixed(ffprobe_streams(str(tmp)), expect_count):
            raise RuntimeError("post-remux verification failed")
        os.utime(tmp, (st.st_atime, st.st_mtime))   # keep *arr/Plex mtime view
        os.replace(tmp, path)                       # atomic; safe for readers
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
    candidates = []       # (path, bad_indices)
    for p in scan_files(roots):
        scanned += 1
        try:
            bad = classify_streams(ffprobe_streams(str(p)))
        except Exception as exc:
            probe_failures.append({"file": str(p), "error": str(exc)[:160]})
            continue
        if bad:
            candidates.append((p, bad))

    fixed, skipped, failures, requeued = [], [], [], []
    if execute:
        for p, bad in candidates:
            if len(fixed) >= max_items:
                skipped.append({"file": str(p), "reason": "max-items cap"})
                continue
            if str(p) in playing:
                skipped.append({"file": str(p), "reason": "active Plex session"})
                continue
            try:
                fix_file(p, bad)
                fixed.append(str(p))
                log("FIXED " + str(p) + " (dropped stream index(es) " + str(bad) + ")")
                if requeue_in_tdarr(str(p)):
                    requeued.append(str(p))
            except Exception as exc:
                failures.append({"file": str(p), "error": str(exc)[:200]})
                warn("fix failed for " + str(p) + ": " + str(exc)[:200])

    return {"scanned": scanned,
            "candidates": [{"file": str(p), "bad_indices": bad} for p, bad in candidates],
            "fixed": fixed, "requeued": requeued, "skipped": skipped,
            "failures": failures, "probe_failures": probe_failures}


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
    log("--- unknown-codec-stream-janitor ({}) roots={} max-items={} ---".format(
        mode, args.roots, args.max_items))

    res = run(roots=args.roots, execute=args.execute, max_items=args.max_items)
    log("scanned {} file(s): {} candidate(s), {} fixed, {} requeued-in-tdarr, "
        "{} skipped, {} failure(s), {} probe-failure(s)".format(
            res["scanned"], len(res["candidates"]), len(res["fixed"]),
            len(res["requeued"]), len(res["skipped"]), len(res["failures"]),
            len(res["probe_failures"])))

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
        _notify("unknown-codec-stream: " + msg, "error")
        _push_kuma("down", msg)
        return EXIT_PARTIAL
    if res["fixed"]:
        _notify("unknown-codec-stream: dropped an unmappable stream and requeued "
                 "{} file(s) in Tdarr".format(len(res["fixed"])), "info")
    _push_kuma("up", "{} fixed, {} candidate(s), {} scanned".format(
        len(res["fixed"]), len(res["candidates"]), res["scanned"]))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
