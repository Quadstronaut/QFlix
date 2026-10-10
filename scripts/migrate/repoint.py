#!/usr/bin/env python3
"""repoint.py -- fix app-to-app links after appdata lands on green (spec 8, row 35).

The data copied from blue carries blue's view of the network: Prowlarr's
Applications point at the arrs, Seerr points at sonarr/radarr, every arr points
at qBittorrent/SABnzbd, Prowlarr's FlareSolverr proxy points at FlareSolverr.
On Ultra those links go through the docker gateway (spec F-17). Spec section 8
says re-point PUTs happen ONLY when needed, so this rewrites exactly two
things and leaves every other byte alone:

  * a host equal to one of OLD_HOSTS (blue's docker gateway, when green does
    not have the same one) becomes NEW_HOST (green's net.app_host);
  * a port listed in PORT_MAP (a port that collided on green and was
    re-claimed) becomes its new value.

An entity whose rewrite is a no-op is never PUT (idempotent, I-4).

Runs ON green, fed over ssh stdin; the plan is argv JSON with NO secrets in it
(API keys are read from green's own ~/secrets, never passed in argv where
/proc/<pid>/cmdline would show them):

  ssh green "python3 - '<plan-json>'" < scripts/migrate/repoint.py

  plan = {"old_hosts": [...], "new_host": "127.0.0.1", "port_map": {"8989": "9898"},
          "arrs": ["sonarr", ...], "prowlarr": ["prowlarr"], "seerr": ["seerr"]}

Bazarr keeps its arr links in config.yaml, not behind a JSON API; a stale host
there is REPORTED (exit 1) for the operator, not rewritten.
Exit 0 all links ok, 1 a PUT failed or a manual fix is needed, 2 bad plan.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import urlsplit, urlunsplit

HOST_KEYS = ("host", "hostname")
URL_KEYS = ("baseUrl", "prowlarrUrl", "url")
PORT_KEYS = ("port",)


def rewrite_url(url: str, old_hosts: List[str], new_host: str, port_map: Dict[str, str]) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.hostname:
        return url
    host, port = parts.hostname, parts.port
    new_h = new_host if host in old_hosts else host
    new_p = port_map.get(str(port), str(port)) if port is not None else None
    if new_h == host and (port is None or new_p == str(port)):
        return url
    netloc = new_h + (":" + new_p if new_p else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def rewrite_value(key: str, value, old_hosts, new_host, port_map):
    if key in PORT_KEYS and value is not None and str(value) in port_map:
        new = port_map[str(value)]
        return int(new) if isinstance(value, int) else new
    if isinstance(value, str):
        if key in HOST_KEYS:
            if value in old_hosts:
                return new_host
            if "://" in value:
                return rewrite_url(value, old_hosts, new_host, port_map)
        if key in URL_KEYS or "://" in value:
            return rewrite_url(value, old_hosts, new_host, port_map)
    return value


def rewrite_entity(entity: dict, old_hosts, new_host, port_map) -> Tuple[dict, bool]:
    """Rewrite top-level keys and *arr-style `fields: [{name, value}]`."""
    out = json.loads(json.dumps(entity))
    for k in list(out):
        if k != "fields":
            out[k] = rewrite_value(k, out[k], old_hosts, new_host, port_map)
    for f in out.get("fields") or []:
        if isinstance(f, dict) and "name" in f and "value" in f:
            f["value"] = rewrite_value(f["name"], f["value"], old_hosts, new_host, port_map)
    return out, out != entity


def _secret(name: str) -> str:
    try:
        return (Path.home() / "secrets" / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _call(method: str, url: str, key: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-Api-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def _base(slug: str) -> Tuple[str, str]:
    port, key, ub = _secret(slug + ".port"), _secret(slug + ".key"), _secret(slug + ".urlbase")
    ub = ("/" + ub.strip("/")) if ub.strip("/") else ""
    return "http://127.0.0.1:%s%s" % (port, ub), key


def _fix_collection(base: str, key: str, path: str, plan: dict, log: List[str]) -> int:
    fails = 0
    try:
        items = _call("GET", base + path, key) or []
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.append("FAIL GET %s: %s" % (path, exc))
        return 1
    for item in items if isinstance(items, list) else []:
        new, changed = rewrite_entity(item, plan["old_hosts"], plan["new_host"], plan["port_map"])
        if not changed:
            continue
        try:
            _call("PUT", "%s%s/%s" % (base, path, item.get("id")), key, new)
            log.append("PUT %s/%s (%s)" % (path, item.get("id"), item.get("name")))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.append("FAIL PUT %s/%s: %s" % (path, item.get("id"), exc))
            fails += 1
    return fails


def main(argv: List[str]) -> int:
    try:
        plan = json.loads(argv[0])
        plan.setdefault("old_hosts", [])
        plan.setdefault("port_map", {})
        plan["port_map"] = {str(k): str(v) for k, v in plan["port_map"].items()}
        new_host = plan["new_host"]
    except (IndexError, ValueError, KeyError, AttributeError) as exc:
        sys.stderr.write("STAGE=usage msg=bad-repoint-plan:%s\n" % exc)
        return 2
    if not plan["old_hosts"] and not plan["port_map"]:
        print("repoint: nothing to rewrite (same gateway, no port collisions)")
        return 0
    log: List[str] = []
    fails = 0
    for slug in plan.get("arrs") or []:
        base, key = _base(slug)
        fails += _fix_collection(base, key, "/api/v3/downloadclient", plan, log)
    for slug in plan.get("prowlarr") or []:
        base, key = _base(slug)
        fails += _fix_collection(base, key, "/api/v1/applications", plan, log)
        fails += _fix_collection(base, key, "/api/v1/indexerProxy", plan, log)
    for slug in plan.get("seerr") or []:
        base, key = _base(slug)
        for kind in ("sonarr", "radarr"):
            fails += _fix_collection(base, key, "/api/v1/settings/" + kind, plan, log)
    for slug in plan.get("bazarr") or []:
        for cfg in (Path.home() / ".apps" / slug).rglob("config.yaml"):
            text = cfg.read_text(encoding="utf-8", errors="replace")
            stale = [h for h in plan["old_hosts"] if h in text]
            if stale:
                log.append("MANUAL %s still names %s (fix in the Bazarr UI: Settings > Sonarr/Radarr)"
                           % (cfg, ",".join(stale)))
                fails += 1
    print("\n".join(log) or "repoint: no entity needed a change")
    print("repoint: %d failure(s)" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
