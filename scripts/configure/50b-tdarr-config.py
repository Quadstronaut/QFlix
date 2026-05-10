#!/usr/bin/env python3
"""Configure Tdarr libraries, worker caps, and webUIPort.

Closes operator-deferred items 29-31 (libraries, workflow, ops vigilance)
and the bookmarks-audit fix for the broken :8265 redirect.

Idempotent: skips libraries already present; only PUTs Settings if values
differ.

Run on the seedbox:  python3 50b-tdarr-config.py
Or pipe via SSH:    sshm "python3 -" < scripts/configure/50b-tdarr-config.py
"""
from __future__ import annotations

import json
import os
import string
import sys
import urllib.error
import urllib.request

TDARR_PORT = 42018
HOME = os.path.expanduser("~")

LIBRARIES = [
    {"name": "Movies", "folder": f"{HOME}/media/Movies"},
    {"name": "TV",     "folder": f"{HOME}/media/TV Shows"},
    {"name": "Anime",  "folder": f"{HOME}/media/Anime"},
]

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


def add_library(spec: dict) -> bool:
    """Insert a minimal library record via mode=insert + obj. Tdarr
    generates the _id server-side and fills in defaults on read."""
    if not os.path.isdir(spec["folder"]):
        print(f"[skip] '{spec['name']}': folder missing: {spec['folder']}",
              file=sys.stderr)
        return False
    record = {
        "name": spec["name"],
        "folder": spec["folder"],
        "folderToProcess": spec["folder"],
        "useFolderToProcess": False,
        "scheduleEnabled": False,
        "deleteFromArr": False,
        "expanded": True,
        "copyMode": False,
        "priority": 0,
        **LIBRARY_DEFAULTS,
    }
    code, resp = _cruddb("LibrarySettingsJSONDB", "insert", obj=record)
    ok = code == 200
    if ok:
        print(f"[create] '{spec['name']}' → {spec['folder']}")
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
    print("=== Tdarr config (libraries + workers + webUIPort) ===\n")
    libs_added = ensure_libraries()
    libs_patched = ensure_library_defaults()
    workers_changed = ensure_worker_limits()
    node_changed = ensure_node_worker_limits()
    config_changed = patch_server_config()
    print()
    print(f"Libraries added: {libs_added}")
    print(f"Library defaults patched: {libs_patched}")
    print(f"Global worker limits changed: {workers_changed}")
    print(f"Node worker limits changed: {node_changed}")
    print(f"webUIPort changed: {config_changed}")
    if config_changed or workers_changed or node_changed or libs_patched:
        print("\nNote: restart tdarr-server.service + tdarr-node.service "
              "for changes to take effect:")
        print("  systemctl --user restart tdarr-server.service "
              "tdarr-node.service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
