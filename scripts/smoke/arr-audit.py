#!/usr/bin/env python3
"""arr-audit — read-only audit of the *arr stack via public nginx.

Probes every *arr (Sonarr, Sonarr2, Radarr, Radarr2, Readarr, Prowlarr, Bazarr)
and Prowlarr's downstream-app sync, reporting:
 1. indexer count + origin (Prowlarr-managed vs manual entries that should
    be removed)
 2. Prowlarr -> *arr sync registration (which downstream apps Prowlarr knows
    about)
 3. download clients per *arr (should be qBit only, working credentials)
 4. quality profile inventory (preface to TRaSH compliance check)
 5. root folder presence + free space
 6. indexer test results (which return 429 / fail / time out)
 7. tags + tag mappings (anime -> sonarr2/radarr2)

Read-only — never mutates state. Reads creds from secrets/*.key + secrets/htpasswd.password.

Output goes to stdout as markdown — pipe to `> docs/arr-audit-2026-05-09.md`.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import urllib.request
import urllib.error
import base64

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRETS = REPO_ROOT / "secrets"
HOST = "https://quadstronaut.seedbox.example.com"

ARRS = [
    {"name": "sonarr",   "urlbase": "sonarr",   "apiver": "v3", "kind": "tv"},
    {"name": "sonarr2",  "urlbase": "sonarr2",  "apiver": "v3", "kind": "anime"},
    {"name": "radarr",   "urlbase": "radarr",   "apiver": "v3", "kind": "movie"},
    {"name": "radarr2",  "urlbase": "radarr2",  "apiver": "v3", "kind": "anime-movie"},
    {"name": "readarr",  "urlbase": "readarr",  "apiver": "v1", "kind": "book"},
]
PROWLARR = {"name": "prowlarr", "urlbase": "prowlarr", "apiver": "v1"}


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


HTPW = _read(SECRETS / "htpasswd.password")


def _basic_auth_header() -> str:
    raw = f"quadstronaut:{HTPW}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _api_key(name: str) -> str:
    return _read(SECRETS / f"{name}.key")


def _api_get(arr: dict, path: str, *, query: dict | None = None) -> dict | list | None:
    """GET /<urlbase>/api/<ver>/<path> with htpasswd basic + X-Api-Key. Returns
    parsed JSON, or None on error."""
    qs = ("?" + urlencode(query)) if query else ""
    url = f"{HOST}/{arr['urlbase']}/api/{arr['apiver']}/{path}{qs}"
    req = urllib.request.Request(url, headers={
        "X-Api-Key": _api_key(arr["name"]),
        "Authorization": _basic_auth_header(),
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        sys.stderr.write(f"  ! {arr['name']} {path}: HTTP {exc.code}\n")
    except Exception as exc:
        sys.stderr.write(f"  ! {arr['name']} {path}: {exc}\n")
    return None


def _api_post(arr: dict, path: str, body: dict) -> tuple[int, dict | None]:
    url = f"{HOST}/{arr['urlbase']}/api/{arr['apiver']}/{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "X-Api-Key": _api_key(arr["name"]),
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="ignore") if hasattr(exc, "read") else ""
        return exc.code, {"_err": body[:200]}
    except Exception as exc:
        return 0, {"_err": str(exc)[:200]}


def section(title: str) -> None:
    print(f"\n## {title}\n")


def subhead(title: str) -> None:
    print(f"\n### {title}\n")


# --- 1. Indexers per *arr -------------------------------------------------

def audit_indexers_per_arr() -> dict:
    """Each *arr should have ZERO manual indexers — Prowlarr syncs them in.
    Manual entries (where the indexer isn't tagged as Prowlarr-managed) are
    a smell.
    """
    section("1. Indexers per *arr")
    results = {}
    for arr in ARRS:
        idxs = _api_get(arr, "indexer") or []
        prowlarr_managed = []
        manual = []
        for idx in idxs:
            # Prowlarr-managed indexers in *arr have a tag with name like
            # "prowlarr-..." OR they store a Prowlarr URL in fields. Two
            # signals: name often starts with the Prowlarr indexer name,
            # and the implementation is usually "Newznab" or "Torznab".
            name = idx.get("name", "?")
            impl = idx.get("implementation", "?")
            tags = idx.get("tags", []) or []
            # Heuristic: if the BaseUrl points back at our prowlarr install,
            # it's Prowlarr-managed. Otherwise manual.
            base_url = ""
            for f in idx.get("fields", []) or []:
                if f.get("name") == "baseUrl":
                    base_url = (f.get("value") or "").lower()
                    break
            is_prowlarr = "prowlarr" in base_url or any(
                t == "prowlarr" for t in tags
            )
            entry = {"name": name, "impl": impl, "base_url": base_url}
            (prowlarr_managed if is_prowlarr else manual).append(entry)

        results[arr["name"]] = {
            "total": len(idxs),
            "prowlarr_managed": len(prowlarr_managed),
            "manual": len(manual),
            "manual_entries": manual,
        }
        print(f"- **{arr['name']}**: {len(idxs)} indexer(s) — {len(prowlarr_managed)} Prowlarr-managed, **{len(manual)} manual**")
        for m in manual:
            print(f"  - ⚠ manual: `{m['name']}` ({m['impl']}) -> {m['base_url']!r}")
    return results


# --- 2. Prowlarr -> *arr sync registration -------------------------------

def audit_prowlarr_apps() -> dict:
    section("2. Prowlarr -> *arr sync registration")
    apps = _api_get(PROWLARR, "applications") or []
    by_name = {a.get("name", "?"): a for a in apps}
    print(f"Prowlarr knows about **{len(apps)} downstream app(s)**:")
    for name, a in by_name.items():
        sync = a.get("syncLevel", "?")
        impl = a.get("implementation", "?")
        tags = a.get("tags", []) or []
        print(f"- `{name}` — impl={impl}, syncLevel={sync}, tags={tags}")
    expected = {"Sonarr", "Sonarr2", "Radarr", "Radarr2", "Readarr",
                "sonarr", "sonarr2", "radarr", "radarr2", "readarr"}
    missing = []
    for arr in ARRS:
        present = any(arr["name"].lower() in (n or "").lower() for n in by_name)
        if not present:
            missing.append(arr["name"])
    if missing:
        print(f"\n⚠ **Missing from Prowlarr Applications**: {', '.join(missing)}")
    else:
        print("\n✓ All 5 *arrs registered with Prowlarr")
    return {"total": len(apps), "missing": missing}


# --- 3. Download clients per *arr ------------------------------------------

def audit_download_clients() -> dict:
    section("3. Download clients per *arr")
    results = {}
    for arr in ARRS:
        dcs = _api_get(arr, "downloadclient") or []
        print(f"\n#### {arr['name']}")
        if not dcs:
            print("⚠ no download clients configured")
            continue
        for dc in dcs:
            name = dc.get("name", "?")
            impl = dc.get("implementation", "?")
            enabled = dc.get("enable", False)
            host = ""
            port = ""
            for f in dc.get("fields", []) or []:
                if f.get("name") == "host":
                    host = f.get("value") or ""
                if f.get("name") == "port":
                    port = f.get("value") or ""
            print(f"- `{name}` — impl={impl}, enabled={enabled}, target={host}:{port}")
        results[arr["name"]] = len(dcs)

        # Run testall — Sonarr/Radarr expose POST /api/v3/downloadclient/testall
        code, body = _api_post(arr, "downloadclient/testall", {})
        if code == 200:
            print(f"  ✓ testall HTTP 200")
        else:
            print(f"  ⚠ testall HTTP {code}: {body}")
    return results


# --- 4. Quality profiles + 5. Root folders --------------------------------

def audit_profiles_and_roots() -> dict:
    section("4. Quality profiles + 5. Root folders + free space")
    out = {}
    for arr in ARRS:
        profiles = _api_get(arr, "qualityprofile") or []
        roots = _api_get(arr, "rootfolder") or []
        print(f"\n#### {arr['name']}")
        print(f"- {len(profiles)} quality profile(s):")
        for p in profiles[:8]:
            cuts = p.get("cutoff", "?")
            cf_count = len(p.get("formatItems", []) or [])
            print(f"  - `{p.get('name', '?')}` (cutoff={cuts}, custom-format-items={cf_count})")
        if len(profiles) > 8:
            print(f"  - ... and {len(profiles)-8} more")
        print(f"- {len(roots)} root folder(s):")
        for r in roots:
            free_gb = (r.get("freeSpace") or 0) / 1024 / 1024 / 1024
            access = r.get("accessible", "?")
            print(f"  - `{r.get('path', '?')}` (free={free_gb:.0f} GB, accessible={access})")
        out[arr["name"]] = {"profiles": len(profiles), "roots": len(roots)}
    return out


# --- 6. Indexer health (test each, identify 429/failures) -----------------

def audit_indexer_health() -> dict:
    section("6. Indexer test pass-rate (Prowlarr)")
    idxs = _api_get(PROWLARR, "indexer") or []
    print(f"Prowlarr has **{len(idxs)} indexers** configured.\n")
    print("Note: live test endpoint hits each indexer's /api/v1/indexer/{id}/test —")
    print("can take 30+ sec for slow indexers. We sample test results from")
    print("Prowlarr's last-known status fields rather than re-testing live.\n")
    # Fields like `latestSearchStatus` / `failureReason` are in Prowlarr's
    # /api/v1/indexerstats endpoint.
    stats = _api_get(PROWLARR, "indexerstats") or {}
    by_id = {}
    for s in (stats.get("indexers") or []):
        by_id[s.get("indexerId")] = s
    failures = []
    healthy = []
    rate_limited = []
    for idx in idxs:
        iid = idx.get("id")
        name = idx.get("name", "?")
        proto = idx.get("protocol", "?")
        # `enable` plus `priority` plus any rolling failure
        s = by_id.get(iid, {})
        avg_resp = s.get("averageResponseTime", -1)
        nfailures = s.get("numberOfQueries", 0) - s.get("numberOfGrabs", 0)
        # 429-detection: look at indexer health objects
        # Prowlarr exposes /api/v1/health for cross-app health items.
        entry = {"id": iid, "name": name, "proto": proto,
                 "avg_resp_ms": avg_resp, "queries": s.get("numberOfQueries", 0),
                 "grabs": s.get("numberOfGrabs", 0)}
        if avg_resp >= 0 and avg_resp < 8000:
            healthy.append(entry)
        elif avg_resp >= 8000:
            rate_limited.append(entry)
        else:
            failures.append(entry)

    health = _api_get(PROWLARR, "health") or []
    print(f"### Prowlarr health items: {len(health)}")
    for h in health:
        print(f"- {h.get('type', '?').upper()}: {h.get('message', '?')}")

    subhead("Indexer status")
    print(f"- ✓ Responding fast (<8s avg): {len(healthy)}")
    print(f"- ⚠ Slow / suspected rate-limit (>=8s avg): {len(rate_limited)}")
    for r in rate_limited[:10]:
        print(f"  - `{r['name']}` avg {r['avg_resp_ms']}ms, queries={r['queries']}, grabs={r['grabs']}")
    print(f"- ✗ No data / never tested: {len(failures)}")
    for f in failures[:10]:
        print(f"  - `{f['name']}` (id={f['id']})")
    return {"healthy": len(healthy), "rate_limited": len(rate_limited),
            "failures": len(failures)}


# --- 7. Tags + anime routing ---------------------------------------------

def audit_tags() -> dict:
    section("7. Tags + anime routing")
    out = {}
    for arr in ARRS:
        tags = _api_get(arr, "tag") or []
        print(f"\n#### {arr['name']} — {len(tags)} tag(s)")
        for t in tags:
            print(f"- `{t.get('label', '?')}` (id={t.get('id')})")
        out[arr["name"]] = [t.get("label") for t in tags]

    # Prowlarr applications use tags to route specific indexers to specific
    # downstream apps. Audit which Prowlarr apps have which tags.
    apps = _api_get(PROWLARR, "applications") or []
    print("\n#### Prowlarr applications — tag routing")
    for a in apps:
        print(f"- `{a.get('name', '?')}` tags={a.get('tags', [])}")
    return out


# --- main -----------------------------------------------------------------

def main() -> int:
    print("# *arr stack audit — read-only")
    print(f"\nGenerated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"\nHost: `{HOST}`\n")

    audit_indexers_per_arr()
    audit_prowlarr_apps()
    audit_download_clients()
    audit_profiles_and_roots()
    audit_indexer_health()
    audit_tags()

    print("\n## Summary\n")
    print("See sections above for per-area findings. Action items extracted at:")
    print("`docs/arr-audit-actions-2026-05-09.md` (operator to triage).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
