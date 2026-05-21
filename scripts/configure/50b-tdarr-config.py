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
REAL_LIBRARY_NAMES = {"Movies", "TV", "Anime"}

LIBRARIES = [
    {"name": "Movies", "folder": f"{HOME}/media/Movies"},
    {"name": "TV",     "folder": f"{HOME}/media/TV Shows"},
    {"name": "Anime",  "folder": f"{HOME}/media/Anime"},
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
# Bump these together when scaling up. 128-core EPYC; current load avg ~30.
WORKER_LIMITS = {
    "transcodeWorkerLimit": 2,
    "healthcheckWorkerLimit": 2,
    "transcodeWorkerLimitGpu": 0,
    "healthcheckWorkerLimitGpu": 0,
}
NODE_WORKER_LIMITS = {
    "transcodecpu": 2,
    "transcodegpu": 0,
    "healthcheckcpu": 2,
    "healthcheckgpu": 0,
}

# Per-library defaults: scan on every server start + watch folder for new
# files. Persistence: direct file write (cruddb mode=set crashes server).
LIBRARY_DEFAULTS = {
    "scanOnStart": True,
    "folderWatcherEnabled": True,
    "scanFoundJobs": True,
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
              f"scanOnStart=True folderWatcher=True")
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


def set_non_destructive_mode() -> int:
    """Phase 30 gate: keep libraries in 'watch new arrivals only' mode by
    forcing processLibrary=false on every real library. Existing files are
    catalogued by scanOnStart but NOT auto-queued for transcode. The 7-day
    clean-window observation requires this; flip to true in 50d (Phase 30
    first-run) only after the operator green-lights the library-wide pass."""
    db_dir = f"{HOME}/.apps/tdarr/server/Tdarr/DB2/LibrarySettingsJSONDB"
    changed = 0
    for path in glob.glob(f"{db_dir}/*.json"):
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("name") not in REAL_LIBRARY_NAMES:
            continue
        if doc.get("processLibrary") is False:
            continue
        doc["processLibrary"] = False
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(path + ".tmp", path)
        print(f"[lock] library '{doc.get('name')}' processLibrary=False "
              f"(new-arrivals-only — flip in Phase 30)")
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
    libs_locked = set_non_destructive_mode()
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
    print(f"Libraries locked to new-arrivals-only: {libs_locked}")
    if any([config_changed, workers_changed, node_changed, libs_patched,
            orphans_purged, flow_changed, libs_attached, libs_locked]):
        print("\nNote: restart tdarr-server.service + tdarr-node.service "
              "for changes to take effect:")
        print("  systemctl --user restart tdarr-server.service "
              "tdarr-node.service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
