#!/usr/bin/env python3
"""One-off (2026-10-11, QFLX-26): retire FlareSolverr from Prowlarr and Kuma.

WHY. Operator decision 2026-10-11 ("get rid of flaresolverr for post-
migration"). FlareSolverr could not be converted to a native app (UCC divorce
decision D-7: its bundled Chromium fails the slot's 10 GB address-space cap)
and it carried nothing: 23 Prowlarr indexers, only 2 tagged `cloudflare`
(the tag that routes through the proxy), both already disabled, and 0
queries / 0 grabs through them in 30 days (Prowlarr history for those two
indexers: 0 records, ever).

TWO SUBCOMMANDS, run in this order (the 2026-07-13 restart-storm lesson: a probe
must never outlive the thing it probes, so the manifest entry is removed and
the pusher restarted by the 240 deploy BEFORE either of these runs):

  prowlarr   Runs ON THE BOX (Prowlarr listens on loopback there). For every
             indexer carrying a tag of the FlareSolverr proxy: REFUSES if the
             indexer is enabled (that would be live use - stop and look), else
             removes the proxy's tags from it and saves it STILL DISABLED
             (forceSave: a disabled walled indexer fails its own test). Then
             deletes the FlareSolverr indexer proxy. Dry-run unless --execute.
               ssh <box> python3 - prowlarr --execute < scripts/ops/decommission-flaresolverr-2026-10-11.py

  kuma       Runs on the WORKSTATION with a tunnel to Kuma (Kuma is
             Dockerized; no socket API on the box). Deletes the "FlareSolverr"
             monitor. Modeled on decommission-books-2026-08-16.py.
               ssh -fN -L 42005:127.0.0.1:42005 quadstronaut@<seedbox-ssh-host>
               tests/.venv/Scripts/python.exe scripts/ops/decommission-flaresolverr-2026-10-11.py kuma

Idempotent: a re-run on an already-clean Prowlarr / Kuma reports "absent (ok)"
and exits 0. Exit codes: 0 done/clean, 1 a step failed, 2 missing dependency,
3 login failed, 4 REFUSED (a tagged indexer is enabled).
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

KUMA = "http://127.0.0.1:42005"
KUMA_MONITOR = "FlareSolverr"
PROXY_IMPL = "FlareSolverr"


def _secrets_dir() -> Path:
    # Workstation: <repo>/secrets. Box (script piped over ssh, no __file__
    # path to resolve): ~/secrets.
    here = Path(__file__).resolve() if "__file__" in globals() else None
    cands = []
    if here is not None and here.name.endswith(".py"):
        cands.append(here.parents[2] / "secrets")
    cands.append(Path.home() / "secrets")
    for c in cands:
        if c.is_dir():
            return c
    raise SystemExit("no secrets dir among: " + ", ".join(str(c) for c in cands))


def secret(name: str) -> str:
    return (_secrets_dir() / name).read_text(encoding="utf-8").strip()


def _prowlarr_call(base: str, key: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"X-Api-Key": key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)


def cmd_prowlarr(execute: bool) -> int:
    key = secret("prowlarr.key")
    port = secret("prowlarr.port")
    try:
        urlbase = secret("prowlarr.urlbase")
    except OSError:
        urlbase = "prowlarr"
    base = "http://127.0.0.1:%s/%s/api/v1" % (port, urlbase)
    mode = "EXECUTE" if execute else "DRY-RUN"

    _, proxies = _prowlarr_call(base, key, "GET", "/indexerProxy")
    fs = [p for p in proxies if p.get("implementation") == PROXY_IMPL]
    _, indexers = _prowlarr_call(base, key, "GET", "/indexer")
    print("[%s] before: indexers=%d enabled=%d proxies=%d flaresolverr_proxies=%d"
          % (mode, len(indexers), sum(1 for i in indexers if i.get("enable")),
             len(proxies), len(fs)))
    if not fs:
        print("absent (ok): no FlareSolverr indexer proxy")
        return 0

    fs_tags = {t for p in fs for t in (p.get("tags") or [])}
    tagged = [i for i in indexers if fs_tags & set(i.get("tags") or [])]
    live = [i for i in tagged if i.get("enable")]
    if live:
        print("REFUSED: enabled indexer(s) route through FlareSolverr: "
              + ", ".join("%s (id=%s)" % (i["name"], i["id"]) for i in live))
        return 4

    failed = 0
    for idx in tagged:
        new_tags = [t for t in idx["tags"] if t not in fs_tags]
        print("untag %s (id=%s, enable=%s): tags %s -> %s"
              % (idx["name"], idx["id"], idx["enable"], idx["tags"], new_tags))
        if not execute:
            continue
        _, full = _prowlarr_call(base, key, "GET", "/indexer/%d" % idx["id"])
        full["tags"] = new_tags
        full["enable"] = False              # stays disabled, explicitly
        try:
            _prowlarr_call(base, key, "PUT",
                           "/indexer/%d?forceSave=true" % idx["id"], full)
        except Exception as exc:            # noqa: BLE001 - reported + counted
            print("  untag FAILED: %r" % (exc,))
            failed += 1
            continue
        _, back = _prowlarr_call(base, key, "GET", "/indexer/%d" % idx["id"])
        if fs_tags & set(back.get("tags") or []) or back.get("enable"):
            print("  read-back MISMATCH: tags=%s enable=%s"
                  % (back.get("tags"), back.get("enable")))
            failed += 1

    if failed:
        print("not deleting the proxy: %d untag step(s) failed" % failed)
        return 1
    for p in fs:
        print("delete indexer proxy %s (id=%s, tags=%s)"
              % (p["name"], p["id"], p.get("tags")))
        if execute:
            try:
                _prowlarr_call(base, key, "DELETE", "/indexerProxy/%d" % p["id"])
            except Exception as exc:        # noqa: BLE001 - reported + counted
                print("  delete FAILED: %r" % (exc,))
                failed += 1

    if execute:
        _, proxies = _prowlarr_call(base, key, "GET", "/indexerProxy")
        _, indexers = _prowlarr_call(base, key, "GET", "/indexer")
        left = [p for p in proxies if p.get("implementation") == PROXY_IMPL]
        print("[%s] after: indexers=%d enabled=%d proxies=%d flaresolverr_proxies=%d"
              % (mode, len(indexers), sum(1 for i in indexers if i.get("enable")),
                 len(proxies), len(left)))
        if left:
            failed += 1
    return 1 if failed else 0


def cmd_kuma() -> int:
    try:
        from uptime_kuma_api import UptimeKumaApi
    except ImportError:
        print("ERROR: uptime-kuma-api not installed in this venv", file=sys.stderr)
        return 2
    api = UptimeKumaApi(KUMA)
    logged_in = False
    for pw_name in ("htpasswd.password", "shared-admin.password"):
        try:
            api.login("quadstronaut", secret(pw_name))
            logged_in = True
            print("login ok via secrets/" + pw_name)
            break
        except Exception as exc:            # noqa: BLE001 - next credential
            sys.stderr.write("decommission-flaresolverr: login probe failed "
                             "(best-effort, continuing): " + repr(exc) + "\n")
    if not logged_in:
        print("FATAL: all logins failed", file=sys.stderr)
        api.disconnect()
        return 3
    try:
        monitors = {m["name"]: m for m in api.get_monitors()}
        print("monitors before: %d" % len(monitors))
        m = monitors.get(KUMA_MONITOR)
        if not m:
            print("absent (ok): " + KUMA_MONITOR)
            return 0
        try:
            api.delete_monitor(m["id"])
        except Exception as exc:            # noqa: BLE001 - reported
            print("delete FAILED %s: %s" % (KUMA_MONITOR, exc))
            return 1
        print("deleted monitor: %s (id=%s)" % (KUMA_MONITOR, m["id"]))
        print("monitors after: %d" % len(api.get_monitors()))
        return 0
    finally:
        api.disconnect()


def main(argv) -> int:
    if not argv or argv[0] not in ("prowlarr", "kuma"):
        print(__doc__)
        return 2
    if argv[0] == "prowlarr":
        return cmd_prowlarr("--execute" in argv[1:])
    return cmd_kuma()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
