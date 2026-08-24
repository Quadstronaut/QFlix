#!/usr/bin/env python3
"""Configure Tdarr libraries, worker caps, and webUIPort.

Closes operator-deferred items 29-31 (libraries, workflow, ops vigilance)
and the bookmarks-audit fix for the broken :8265 redirect.

Idempotent: skips libraries already present; only PUTs Settings if values
differ.

Run on the seedbox:  python3 50b-tdarr-config.py
Or pipe via SSH:    sshm "python3 -" < scripts/configure/50b-tdarr-config.py
                    (the qflix-direct-play-fix.json sidecar must already
                    live at ~/.apps/tdarr/configs/ — the runner shell
                    scripts/configure/50b-tdarr-config.sh handles the scp.)
"""
from __future__ import annotations

import glob
import json
import os
import string
import sys
import urllib.error
import urllib.request

TDARR_PORT = 42018
HOME = os.path.expanduser("~")

# Real, expected library names — anything else in LibrarySettingsJSONDB is
# an orphan from earlier UI-create attempts and gets purged. The 21→3 cleanup
# closes the Phase 29.2 "18 orphan library rows" item.
# "Anime Movies" added 2026-08-20: the 59-brdisk-block / containerFilter door
# was closed on Movies/TV/Anime only, leaving radarr2's second root unscanned
# by Tdarr. Welcome added the same day on the operator's "all libraries, all
# files" directive (one hand-placed clip today, but the policy is universal).
REAL_LIBRARY_NAMES = {"Movies", "TV", "Anime", "Anime Movies", "Welcome"}

LIBRARIES = [
    {"name": "Movies", "folder": f"{HOME}/media/Movies"},
    {"name": "TV",     "folder": f"{HOME}/media/TV Shows"},
    {"name": "Anime",  "folder": f"{HOME}/media/Anime"},
    {"name": "Anime Movies", "folder": f"{HOME}/media/Anime Movies"},
    {"name": "Welcome", "folder": f"{HOME}/media/Welcome"},
]

# Path to the Flow JSON (resolved relative to this script in repo, or
# alongside it when piped over SSH). The Flow doc gets inserted into
# FlowsJSONDB and then referenced by libraries via decisionMaker.settingsFlows
# + flowId. See docs/superpowers/plans/2026-05-08-tdarr-install.md.
# Look for the Flow JSON in a few well-known places. The runner shell
# scripts/configure/50b-tdarr-config.sh scp's it to the first path; the rest
# are fallbacks for in-repo invocations.
FLOW_FILE_CANDIDATES = [
    os.environ.get("TDARR_FLOW_JSON", ""),
    f"{HOME}/.apps/tdarr/configs/qflix-direct-play-fix.json",
    os.path.join(os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else ".",
                 "tdarr-flows", "qflix-direct-play-fix.json"),
    "tdarr-flows/qflix-direct-play-fix.json",
]

# Library decisionMaker block to flip the library into Flow mode. Mutually
# exclusive radio in the Tdarr UI — only one of the four settings* keys is
# allowed true at a time. settingsPlugin is the classic-plugin-stack default;
# settingsFlows is the modern Flow engine.
DECISION_MAKER_FLOWS = {
    "settingsPlugin": False,
    "settingsVideo": False,
    "settingsAudio": False,
    "settingsFlows": True,
}

# Worker cap for the shared seedbox (operator spec §31).
# Two layers — both must be set:
#   1. SettingsGlobalJSONDB — server-wide defaults
#   2. NodeJSONDB.workerLimits — per-node override (THIS is what actually
#      gates work; if zero, the node runs nothing regardless of global)
# Operator picked "tune interactively, start at 2/2 and ramp" on 2026-05-09.
# Ramped DOWN to 1/1 on 2026-08-07: 2/2 means FOUR concurrent workers (the two
# pipelines are counted separately, not shared) and that was driving ~94% CPU on
# a SHARED slot. 128-core EPYC.
#
# 2026-08-20 -- 2 transcode + 1 health-check (THREE concurrent, not four) as
# the node went 24/7. Reached only after ffmpeg was thread-capped, and the
# order mattered: raising this first drove the slot into its `ulimit -u 2000`
# TASK ceiling within minutes (bash could not fork, which breaks cron and every
# canary, not just Tdarr).
#
# The constraint here is tasks, not CPU. Uncapped ffmpeg threads to core count
# on a 128-core box, so ONE job held 129-273 threads and a single worker
# already sat at 70.5% of the ceiling. Capped to 8 threads a job holds ~34, so
# the second worker costs ~25 tasks: 1 worker 1411 -> 1030, 2 workers 1055.
# Two capped workers are cheaper than one uncapped one.
#
# The cap is scripts/ops/ffmpeg-threadcap-shim.sh, installed as Tdarr ffmpeg
# binary by 50-tdarr-install.sh. If that shim is ever missing, put these back
# to 1 -- see manifest/apps.yaml tdarr-node.throttle for the full table. This is now the WHOLE fair-use mechanism: the
# 18:00-23:00 UTC quiet-hours pause is retired, so nothing else throttles this
# node at any hour. Read the numbers from manifest/apps.yaml
# tdarr-node.throttle -- that is the single source of truth, and
# tdarr-throttle-integrity.sh audits the live node against it hourly.
#
# The asymmetry is deliberate. Transcode is the work that has to converge (39
# hevc/av1 files were forcing Plex to transcode on the fly for every client);
# health-check is maintenance and gets one worker, not two, so the 2/2 ->
# four-worker 94% CPU episode cannot recur by arithmetic.
#
# LAYER 2 IS THE ONE THAT MATTERS, AND EDITING ONLY LAYER 1 LOOKS LIKE IT WORKED.
# On 2026-08-07 the global was set to 1/1 and the node kept running 4 workers,
# because SettingsGlobalJSONDB is only the seed default for a node that has no
# record yet -- an existing NodeJSONDB.workerLimits is never re-read from it.
# The global edit persisted cleanly (1/1 on disk), so every check short of
# counting live workers agreed the change had taken. Set both, always.
#
# AND THE NODE RECORD MUST BE WRITTEN WHILE THE SERVER IS STOPPED. The server
# rewrote this record two minutes after that edit, when the node reconnected,
# so a hand-patch applied to a running server is clobbered on the next connect.
# ensure_node_worker_limits() writes the file directly for the same reason
# cruddb is avoided elsewhere here; the caller is responsible for stopping
# tdarr-server first (see 50b's own deploy notes).
WORKER_LIMITS = {
    "transcodeWorkerLimit": 2,
    "healthcheckWorkerLimit": 1,
    "transcodeWorkerLimitGpu": 0,
    "healthcheckWorkerLimitGpu": 0,
}
NODE_WORKER_LIMITS = {
    "transcodecpu": 2,
    "transcodegpu": 0,
    "healthcheckcpu": 1,
    "healthcheckgpu": 0,
}

# Per-library defaults: scan on every server start + watch folder for new
# files. Persistence: direct file write (cruddb mode=set crashes server).
# Container scan filter. Tdarr ships a stock containerFilter of
#   mkv,mp4,mov,m4v,mpg,mpeg,avi,flv,webm,wmv,vob,evo,iso,m2ts,ts
# and add_library() copies it wholesale, so every library was scanning DISC
# IMAGE and DISC STRUCTURE containers: .iso (a whole Blu-ray/DVD filesystem),
# .vob (DVD VIDEO_TS payload) and .evo (HD-DVD payload).
#
# That fired for real on 2026-08-20. Capping Remux on the Radarr profiles made
# Radarr re-search 23 movies; one release parsed as Bluray-1080p from its NAME
# while the payload was a full disc, so a 47.6 GB .iso landed in the Movies
# library. Tdarr's folder watcher then picked the .iso up at 09:14:39, spent
# roughly two hours of node time on it, and wrote a 39.4 GiB .mkv at 11:03:14
# that Radarr does not know exists -- so the *arr side could not evict it and
# the file survived the disc cleanup by changing its own extension.
#
# A disc image is never a transcode SOURCE worth having here: it carries menus,
# multiple angles and playlist structure that ffmpeg cannot sensibly flatten,
# and the useful video inside it is exactly what the *arr should have grabbed
# instead. Dropping the three disc containers means an .iso that slips past the
# grab-time levers (scripts/configure/59-brdisk-block.py) sits inert until a
# human or the library-container-sanity canary deals with it, rather than being
# laundered into a library-shaped file.
#
# m2ts and ts are DELIBERATELY KEPT: both are legitimate standalone containers
# here (the 2026-08-19 hardlink-integrity false positive was a BDMV rip whose
# only video was 00000.m2ts, and that file is a real payload). The BDMV
# DIRECTORY structure is caught by the library-container-sanity canary instead.
CONTAINER_FILTER = "mkv,mp4,mov,m4v,mpg,mpeg,avi,flv,webm,wmv,m2ts,ts"

# Where a transcode worker builds its scratch directory. MUST be an explicit
# absolute path, and MUST NOT be left empty.
#
# Tdarr builds the worker scratch dir by string-concatenating the library's
# `cache` with "/tdarr-workDir-node-<id>-worker-<name>-ts-<ms>". Three of the
# five libraries carried cache="", so that concatenation produced
# "/tdarr-workDir-..." -- an absolute path at the FILESYSTEM ROOT. This slot is
# rootless, so every transcode worker died the instant it started:
#     [FATAL] Tdarr_Node - Error: EACCES: permission denied, mkdir
#     '/tdarr-workDir-node-YjouEnw6d-worker-lame-loris-ts-1787514583085'
#     Worker lame-loris exited with code 1 and signal null
# and the node then pruned the worker and never retried. Found 2026-08-23 after
# a tdarr-server + tdarr-node restart, with two files sitting at Queued and both
# transcode slots free.
#
# WHAT MAKES THIS THE DANGEROUS KIND OF BUG: nothing reported it. The node unit
# stays active, the node stays registered, health checks keep passing (they use
# a different code path), and a file whose worker died never reaches
# TranscodeDecisionMaker=Error -- it stays Queued. So the Tdarr monitor, the
# Tdarr Node monitor, tdarr-healthcheck, tdarr-transcode-error and
# tdarr-throttle-integrity were ALL green through a total transcode outage.
# The tdarr-transcode-stall canary exists because of this.
#
# An explicit absolute path cannot degrade this way whatever Tdarr does with the
# string. "." also happens to work (it resolves against the node's
# WorkingDirectory) and two libraries carried it, which is exactly why the
# breakage looked intermittent and library-dependent.
TRANSCODE_CACHE = f"{HOME}/.apps/tdarr/cache"

# Per-library defaults: scan on every server start + watch folder for new
# files. Persistence: direct file write (cruddb mode=set crashes server).
#
# `output` is deliberately NOT in here: empty means "write back to the source
# folder", which is the in-place replacement this library wants. `cache` is the
# staging dir and is a different thing entirely.
LIBRARY_DEFAULTS = {
    "scanOnStart": True,
    "folderWatcherEnabled": True,
    "scanFoundJobs": True,
    "containerFilter": CONTAINER_FILTER,
    "cache": TRANSCODE_CACHE,
}

# Health-check engine. Tdarr picks the health-check CLI from a mutually
# exclusive pair on the library record (Tdarr_Node/srcug/workers/worker1.js):
#   handbrakescan -> HandBrakeCLI -i <file> -o <cache> --scan
#   ffmpegscan    -> ffmpeg -i <file> -f null -max_muxing_queue_size 9999
# Tdarr's own libraryDefaults ships handbrakescan=true, and add_library() copies
# that dict wholesale — but HandBrakeCLI does not exist on this rootless Ultra.cc
# slot and cannot be installed (no root, no package, and Tdarr would wipe any
# hand-patched binary on upgrade). So every health check spawn-failed
# `Error: spawn HandBrakeCLI ENOENT` and the file was recorded HealthCheck=Error.
#
# That ran undetected from 2026-05-21 to 2026-07-28: 2,866 failures across 54
# days with a literal 0% success rate (healthCheckScore 0.000) and no alert,
# because the pipeline it blocks is orthogonal to transcoding — transcodes use
# the bundled ffmpeg-static and were succeeding the whole time, so every
# surface an operator would glance at looked healthy.
#
# ffmpeg-static is present, already carries every transcode, and full-decodes at
# ~20x realtime here (~2.5 min for a 50-min episode), bounded by the health-check
# worker cap (1 as of 2026-08-20; the quiet-hours pause that used to bound it too
# is retired and the node runs 24/7). Guarded by the
# tdarr-healthcheck canary, which reds if the error ratio ever goes pathological
# again. Do NOT "fix" this by reintroducing handbrakescan.
HEALTHCHECK_ENGINE = {
    "handbrakescan": False,
    "ffmpegscan": True,
}


def _short_id() -> str:
    """Tdarr's _id format is a 9-char alphanumeric. Match it for visual
    parity with UI-created records."""
    import secrets
    alpha = string.ascii_letters + string.digits
    return "".join(secrets.choice(alpha) for _ in range(9))


def _post(path: str, body: dict) -> tuple[int, object]:
    url = f"http://127.0.0.1:{TDARR_PORT}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(data) if data else None
            except json.JSONDecodeError:
                return resp.status, data
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _cruddb(collection: str, mode: str, **extra) -> tuple[int, object]:
    return _post(
        "/api/v2/cruddb",
        {"data": {"collection": collection, "mode": mode, **extra}},
    )


def get_libraries() -> list:
    code, data = _cruddb("LibrarySettingsJSONDB", "getAll")
    if code != 200 or not isinstance(data, list):
        raise SystemExit(f"libraries getAll failed: HTTP {code}: {data!r}")
    return data


def _load_tdarr_library_defaults() -> dict:
    """Load Tdarr's own libraryDefaults dict from the sidecar JSON. Without
    these ~49 fields, Tdarr's file scanner crashes silently with
    `Cannot set properties of undefined (setting 'storeID')` and the library
    is unscannable. The JSON is dumped from the live Tdarr install via:
        cd ~/.apps/tdarr/Tdarr_Server && node -e \\
          'const d=require("./srcug/commonModules/jobs/libraryDefaults.js"); \\
           console.log(JSON.stringify(d.default,null,2))'
    Refresh when bumping Tdarr versions (currently pinned to 2.17.01)."""
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__))
                     if "__file__" in globals() else ".",
                     "tdarr-flows", "library-defaults.json"),
        f"{HOME}/.apps/tdarr/configs/library-defaults.json",
        "tdarr-flows/library-defaults.json",
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    raise SystemExit(
        "FATAL: tdarr library-defaults.json not found. Tried: "
        + ", ".join(candidates)
    )


def add_library(spec: dict) -> bool:
    """Insert a library record built from Tdarr's own libraryDefaults dict,
    overlaid with QFlix-specific customizations (name, folder, flow attach,
    processLibrary gate). The full-default approach replaces the earlier
    skeleton record that crashed Tdarr's file scanner — see commit history
    on this file for the 2026-05-21 incident notes."""
    if not os.path.isdir(spec["folder"]):
        print(f"[skip] '{spec['name']}': folder missing: {spec['folder']}",
              file=sys.stderr)
        return False
    record = dict(_load_tdarr_library_defaults())
    record["name"] = spec["name"]
    record["folder"] = spec["folder"]
    record["folderToProcess"] = spec["folder"]
    record["_id"] = _short_id()
    record["scanOnStart"] = True
    record["folderWatching"] = True
    record["useFsEvents"] = False
    # Overlay the health-check engine at birth, so a newly created library never
    # inherits Tdarr's handbrakescan default onto a box with no HandBrakeCLI.
    record.update(HEALTHCHECK_ENGINE)
    code, resp = _cruddb("LibrarySettingsJSONDB", "insert", obj=record)
    ok = code == 200
    if ok:
        print(f"[create] '{spec['name']}' → {spec['folder']} (id={record['_id']}, fields={len(record)})")
    else:
        print(f"[fail] '{spec['name']}': HTTP {code}: {str(resp)[:200]}",
              file=sys.stderr)
    return ok


def ensure_library_defaults() -> int:
    """Patch existing library records (created via insert above) to flip
    LIBRARY_DEFAULTS settings on. Direct file write — Tdarr's cruddb
    mode=set is unreliable for nested doc updates."""
    import glob
    # The cache dir has to exist before a worker tries to mkdir inside it;
    # Tdarr creates the leaf workDir, not its parent.
    os.makedirs(TRANSCODE_CACHE, exist_ok=True)
    db_dir = f"{HOME}/.apps/tdarr/server/Tdarr/DB2/LibrarySettingsJSONDB"
    files = glob.glob(f"{db_dir}/*.json")
    changed = 0
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        needs = any(doc.get(k) != v for k, v in LIBRARY_DEFAULTS.items())
        if not needs:
            continue
        doc.update(LIBRARY_DEFAULTS)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(path + ".tmp", path)
        print(f"[update] library '{doc.get('name')}' "
              f"scanOnStart=True folderWatcher=True "
              f"containerFilter={CONTAINER_FILTER} cache={TRANSCODE_CACHE}")
        changed += 1
    return changed


def requeue_errored_healthchecks() -> int:
    """Reset HealthCheck=Error file records back to Queued so they get re-checked.

    Only called when the engine actually changed — a stale Error verdict recorded
    under the broken HandBrake engine is not evidence about the file, it's
    evidence about the missing binary, and Tdarr will never retry it on its own.
    Idempotent by construction: after a successful pass there are no Error rows
    left to reset, and a genuine Error found by the working ffmpeg engine is a
    real corrupt-file signal we must NOT keep clearing."""
    db_dir = f"{HOME}/.apps/tdarr/server/Tdarr/DB2/FileJSONDB"
    requeued = 0
    for path in glob.glob(f"{db_dir}/*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if doc.get("HealthCheck") != "Error":
            continue
        doc["HealthCheck"] = "Queued"
        doc["lastHealthCheckDate"] = 0
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(path + ".tmp", path)
        requeued += 1
    return requeued


# Video codecs the flow treats as universally direct-playable. Anything else is
# re-encoded to h264 8-bit High@4.1 by qflix-direct-play-fix (operator
# directive 2026-08-20: every file must play on every TV/phone/tablet).
DIRECT_PLAY_VIDEO_CODECS = {"h264"}

# The rest of "universally playable", which the codec set alone cannot express.
#
# The flow used to gate on CODEC only, so a file that ARRIVED as h264 was
# stamped "Not required" whatever its bit depth or level -- and the Force 8-bit
# node, the thing that actually enforces the policy, only runs on files that
# entered the codec branch. An audit of all 465 records on 2026-08-24 found 28
# files already h264 and already out of policy: 12 at High 10 / yuv420p10le and
# 16 above level 4.1.
#
# The 10-bit dozen is the one that matters. Samsung Tizen and most smart-TV
# decoders cannot decode 10-bit H.264 AT ALL -- it is exactly the failure the
# 2026-08-20 directive was written about, arriving through a door the directive's
# own implementation did not cover.
#
# Level: the complete set of H.264 levels above 4.1. The spec defines no level
# beyond 6.2, so this enumeration cannot go stale.
DISALLOWED_PIX_FMT_MARKERS = ("10le", "10be", "12le", "12be")
DISALLOWED_H264_LEVELS = {42, 50, 51, 52, 60, 61, 62}


def _video_stream(doc: dict) -> dict:
    """The primary video stream of a Tdarr FileJSONDB record, or {}.

    Attached cover art is a video stream too (mjpeg/png poster) and must never
    be mistaken for the primary one -- its pix_fmt is yuvj420p and its level is
    -99, so reading it would answer the policy question about the wrong stream.
    """
    for s in ((doc.get("ffProbeData") or {}).get("streams") or []):
        if s.get("codec_type") != "video":
            continue
        if (s.get("disposition") or {}).get("attached_pic") == 1:
            continue
        return s
    return {}


def video_policy_violation(doc: dict) -> str:
    """Why this file is not universally playable, or "" if it is.

    Returns a short reason string so the requeue log says WHICH rule fired --
    "h264 but High 10" and "not h264 at all" are different operator stories and
    a bare "requeued" hides both.
    """
    codec = doc.get("video_codec_name")
    if not codec:
        return ""                       # unknown is not evidence of a problem
    if codec not in DIRECT_PLAY_VIDEO_CODECS:
        return "codec=%s" % codec
    s = _video_stream(doc)
    if not s:
        return ""                       # cannot see the stream -> do not act
    pix = str(s.get("pix_fmt") or "")
    if any(m in pix for m in DISALLOWED_PIX_FMT_MARKERS):
        return "pix_fmt=%s(>8-bit)" % pix
    level = s.get("level")
    if isinstance(level, int) and level in DISALLOWED_H264_LEVELS:
        return "level=%s(>4.1)" % level
    return ""


def requeue_noncompliant_video() -> int:
    """Re-queue files Tdarr already stamped 'Not required' / 'Transcode
    success' that violate the universal-playability policy -- wrong codec, more
    than 8 bits per component, or an H.264 level above 4.1.

    Widened from codec-only on 2026-08-24. Gating on the codec set alone meant a
    file that ARRIVED as h264 was never revisited whatever its profile, bit depth
    or level, and 28 files sat out of policy -- 12 of them High 10, which Samsung
    Tizen and most smart-TV decoders cannot decode at all. See
    video_policy_violation() and the matching gates in the flow JSON; the flow
    must agree, because a requeue whose flow still answers "Not required" simply
    returns the file to where it started.

    Tdarr caches the flow's verdict per file and never revisits it when the
    flow changes, so widening the flow (hevc/av1 -> h264, 2026-08-20) would
    have left the 39 pre-existing hevc/av1 files untouched forever. Idempotent:
    a file that has been re-encoded reads back as h264 and no longer matches;
    a file whose transcode ERRORED is left alone (its verdict is not 'Not
    required'), so a flow bug cannot become a retry loop."""
    db_dir = f"{HOME}/.apps/tdarr/server/Tdarr/DB2/FileJSONDB"
    requeued = 0
    for path in glob.glob(f"{db_dir}/*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        # 'Transcode success' too: a file the OLD flow processed for audio only
        # still carries its hevc/av1 video and would otherwise never be revisited.
        # 'Transcode error' is deliberately NOT here (no retry loop on a flow bug).
        if doc.get("TranscodeDecisionMaker") not in ("Not required", "Transcode success"):
            continue
        reason = video_policy_violation(doc)
        if not reason:
            continue
        doc["TranscodeDecisionMaker"] = "Queued"
        doc["lastTranscodeDate"] = 0
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(path + ".tmp", path)
        print(f"[requeue] {reason} -> h264 8-bit High@4.1: "
              f"{os.path.basename(doc.get('file', path))[:80]}")
        requeued += 1
    return requeued


def ensure_healthcheck_engine() -> int:
    """Force every real library onto the ffmpeg health-check engine.

    See HEALTHCHECK_ENGINE above for why HandBrake is not an option on this box.
    Direct file write, like the other library patchers — Tdarr re-reads the JSON
    DB on restart. Stop the server before running if you want to be certain the
    in-memory copy can't race the write."""
    db_dir = f"{HOME}/.apps/tdarr/server/Tdarr/DB2/LibrarySettingsJSONDB"
    changed = 0
    for path in glob.glob(f"{db_dir}/*.json"):
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("name") not in REAL_LIBRARY_NAMES:
            continue
        if all(doc.get(k) == v for k, v in HEALTHCHECK_ENGINE.items()):
            continue
        was = {k: doc.get(k) for k in HEALTHCHECK_ENGINE}
        doc.update(HEALTHCHECK_ENGINE)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(path + ".tmp", path)
        print(f"[healthcheck] library '{doc.get('name')}' engine {was} "
              f"-> ffmpeg (HandBrakeCLI absent on this slot)")
        changed += 1
    return changed


def ensure_node_worker_limits() -> bool:
    """Patch every NodeJSONDB record's workerLimits. This is the layer
    that actually gates work — global SettingsGlobalJSONDB is just the
    seed default."""
    import glob
    db_dir = f"{HOME}/.apps/tdarr/server/Tdarr/DB2/NodeJSONDB"
    files = glob.glob(f"{db_dir}/*.json")
    if not files:
        print(f"[skip] no NodeJSONDB files at {db_dir}", file=sys.stderr)
        return False
    changed = False
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        cur = doc.get("workerLimits") or {}
        if all(cur.get(k) == v for k, v in NODE_WORKER_LIMITS.items()):
            continue
        doc["workerLimits"] = NODE_WORKER_LIMITS
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(path + ".tmp", path)
        print(f"[update] node '{doc.get('nodeName', '?')}' workerLimits "
              f"→ transcode={NODE_WORKER_LIMITS['transcodecpu']} "
              f"healthcheck={NODE_WORKER_LIMITS['healthcheckcpu']}")
        changed = True
    return changed


def heal_skeleton_libraries() -> int:
    """Detect any LibrarySettingsJSONDB records created via the pre-2026-05-21
    skeleton-insert path (fewer than 30 fields — the scanner needs ~49). For
    each: delete the file from disk, then re-create through add_library().
    This is a one-shot upgrade that idempotently no-ops on healthy installs."""
    import glob
    db_dir = f"{HOME}/.apps/tdarr/server/Tdarr/DB2/LibrarySettingsJSONDB"
    healed = 0
    for path in glob.glob(f"{db_dir}/*.json"):
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        name = doc.get("name", "")
        if name not in REAL_LIBRARY_NAMES:
            continue
        if len(doc) >= 30:
            continue
        os.remove(path)
        print(f"[heal] '{name}' skeleton record ({len(doc)} fields) removed; will recreate")
        spec = next((s for s in LIBRARIES if s["name"] == name), None)
        if spec and add_library(spec):
            healed += 1
    return healed


def ensure_libraries() -> int:
    existing = {lib.get("name") for lib in get_libraries()}
    created = 0
    for spec in LIBRARIES:
        if spec["name"] in existing:
            print(f"[skip] '{spec['name']}' already present")
            continue
        if add_library(spec):
            created += 1
    return created


def ensure_worker_limits() -> bool:
    """Tdarr's `mode=set` is fragile (server-side crashes on wrong shape).
    Patch the JSON-DB file directly — Tdarr re-reads it on restart."""
    import glob
    db_dir = f"{HOME}/.apps/tdarr/server/Tdarr/DB2/SettingsGlobalJSONDB"
    files = glob.glob(f"{db_dir}/*.json")
    if not files:
        print(f"[skip] no SettingsGlobalJSONDB file at {db_dir}",
              file=sys.stderr)
        return False
    path = files[0]
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    needs_update = any(doc.get(k) != v for k, v in WORKER_LIMITS.items())
    if not needs_update:
        print("[skip] worker limits already at desired values")
        return False
    doc.update(WORKER_LIMITS)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, path)
    print(f"[update] worker limits → "
          f"transcode={WORKER_LIMITS['transcodeWorkerLimit']} "
          f"healthcheck={WORKER_LIMITS['healthcheckWorkerLimit']} "
          f"(direct file write — restart for effect)")
    return True


def _load_flow_doc() -> dict:
    """Read the qflix-direct-play-fix.json from the first existing candidate.
    Fail loud — if no candidate found, we can't proceed."""
    for path in FLOW_FILE_CANDIDATES:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    raise SystemExit(
        "FATAL: qflix-direct-play-fix.json not found. Tried: "
        + ", ".join(p for p in FLOW_FILE_CANDIDATES if p)
    )


def purge_orphan_libraries() -> int:
    """Delete LibrarySettingsJSONDB records that were created during failed
    UI experiments — name == 'Library Name' (default placeholder) OR folder
    is empty. Real libraries (Movies/TV/Anime) are preserved by both checks.

    Direct file delete — cruddb mode=remove crashes the server on absent _id."""
    db_dir = f"{HOME}/.apps/tdarr/server/Tdarr/DB2/LibrarySettingsJSONDB"
    removed = 0
    for path in glob.glob(f"{db_dir}/*.json"):
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        name = doc.get("name", "")
        folder = doc.get("folder", "")
        is_real = name in REAL_LIBRARY_NAMES and folder
        if is_real:
            continue
        os.remove(path)
        print(f"[purge] orphan library '{name}' folder='{folder}' ({os.path.basename(path)[:8]})")
        removed += 1
    return removed


def ensure_flow() -> bool:
    """Insert the QFlix Direct-Play Fix Flow into FlowsJSONDB if missing.
    Update-in-place if a record with the same _id exists but content differs."""
    flow = _load_flow_doc()
    flow_id = flow["_id"]
    db_dir = f"{HOME}/.apps/tdarr/server/Tdarr/DB2/FlowsJSONDB"
    os.makedirs(db_dir, exist_ok=True)
    target = f"{db_dir}/{flow_id}.json"
    if os.path.isfile(target):
        with open(target, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if existing == flow:
            print(f"[skip] flow '{flow_id}' already at desired state")
            return False
        with open(target + ".tmp", "w", encoding="utf-8") as f:
            json.dump(flow, f, indent=2)
        os.replace(target + ".tmp", target)
        print(f"[update] flow '{flow_id}' patched to match repo JSON")
        return True
    with open(target + ".tmp", "w", encoding="utf-8") as f:
        json.dump(flow, f, indent=2)
    os.replace(target + ".tmp", target)
    print(f"[create] flow '{flow_id}' written to {target}")
    return True


def attach_flow_to_libraries() -> int:
    """For each real library, set flowId + decisionMaker.settingsFlows so the
    library uses our Flow instead of the default classic plugin stack.

    Library record fields modified:
      - flowId: '<flow_id>'
      - decisionMaker.{settingsPlugin, settingsVideo, settingsAudio,
        settingsFlows} = DECISION_MAKER_FLOWS

    Re-attaching is idempotent — only writes when current values differ."""
    flow = _load_flow_doc()
    flow_id = flow["_id"]
    db_dir = f"{HOME}/.apps/tdarr/server/Tdarr/DB2/LibrarySettingsJSONDB"
    changed = 0
    for path in glob.glob(f"{db_dir}/*.json"):
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("name") not in REAL_LIBRARY_NAMES:
            continue
        cur_decision = doc.get("decisionMaker") or {}
        needs_flow_id = doc.get("flowId") != flow_id
        needs_decision = any(
            cur_decision.get(k) != v for k, v in DECISION_MAKER_FLOWS.items()
        )
        if not (needs_flow_id or needs_decision):
            continue
        doc["flowId"] = flow_id
        new_decision = dict(cur_decision)
        new_decision.update(DECISION_MAKER_FLOWS)
        doc["decisionMaker"] = new_decision
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(path + ".tmp", path)
        print(f"[attach] library '{doc.get('name')}' -> flow '{flow_id}'")
        changed += 1
    return changed


def ensure_library_processing() -> int:
    """Phase 30 go-live: keep transcoding LIVE by forcing processLibrary=true on
    every real library, so the qflix-direct-play-fix Flow actively processes the
    library (not just catalogues it). Operator green-lit the library-wide pass on
    2026-05-30; this replaces the earlier non-destructive 'watch new arrivals only'
    lock. Idempotent — only writes when a library has drifted to False, which also
    self-heals any library that gets paused out-of-band."""
    db_dir = f"{HOME}/.apps/tdarr/server/Tdarr/DB2/LibrarySettingsJSONDB"
    changed = 0
    for path in glob.glob(f"{db_dir}/*.json"):
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("name") not in REAL_LIBRARY_NAMES:
            continue
        if doc.get("processLibrary") is True:
            continue
        doc["processLibrary"] = True
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(path + ".tmp", path)
        print(f"[enable] library '{doc.get('name')}' processLibrary=True "
              f"(transcoding live — Phase 30 go-live)")
        changed += 1
    return changed


def patch_server_config() -> bool:
    """Add webUIPort to Tdarr_Server_Config.json so its self-redirect uses
    the right port (instead of defaulting to :8265)."""
    config_path = f"{HOME}/.apps/tdarr/configs/Tdarr_Server_Config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print(f"[skip] {config_path} not found", file=sys.stderr)
        return False
    if cfg.get("webUIPort") == TDARR_PORT:
        print(f"[skip] webUIPort already {TDARR_PORT}")
        return False
    cfg["webUIPort"] = TDARR_PORT
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, config_path)
    print(f"[update] webUIPort = {TDARR_PORT} in {config_path}")
    return True


def main() -> int:
    print("=== Tdarr config (libraries + workers + webUIPort + flow) ===\n")
    orphans_purged = purge_orphan_libraries()
    libs_healed = heal_skeleton_libraries()
    libs_added = ensure_libraries()
    libs_patched = ensure_library_defaults()
    workers_changed = ensure_worker_limits()
    node_changed = ensure_node_worker_limits()
    config_changed = patch_server_config()
    flow_changed = ensure_flow()
    libs_attached = attach_flow_to_libraries()
    libs_enabled = ensure_library_processing()
    hc_engine_changed = ensure_healthcheck_engine()
    # Only re-queue when we just switched engines: stale Error rows written by the
    # broken HandBrake engine are meaningless, but a real ffmpeg-found Error must
    # be allowed to stick so it surfaces as an actual corrupt file.
    hc_requeued = requeue_errored_healthchecks() if hc_engine_changed else 0
    video_requeued = requeue_noncompliant_video()
    print()
    print(f"Orphan libraries purged: {orphans_purged}")
    print(f"Skeleton libraries healed (re-created with full defaults): {libs_healed}")
    print(f"Libraries added: {libs_added}")
    print(f"Library defaults patched: {libs_patched}")
    print(f"Global worker limits changed: {workers_changed}")
    print(f"Node worker limits changed: {node_changed}")
    print(f"webUIPort changed: {config_changed}")
    print(f"Flow created/updated: {flow_changed}")
    print(f"Libraries attached to flow: {libs_attached}")
    print(f"Libraries enabled for live transcoding: {libs_enabled}")
    print(f"Libraries switched to ffmpeg health-check engine: {hc_engine_changed}")
    print(f"Stale HandBrake-era health-check errors re-queued: {hc_requeued}")
    print(f"Non-h264 files re-queued for the direct-play flow: {video_requeued}")
    if any([config_changed, workers_changed, node_changed, libs_patched,
            orphans_purged, flow_changed, libs_attached, libs_enabled,
            hc_engine_changed, video_requeued]):
        print("\nNote: restart tdarr-server.service + tdarr-node.service "
              "for changes to take effect:")
        print("  systemctl --user restart tdarr-server.service "
              "tdarr-node.service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
