#!/usr/bin/env python3
"""arr-audit — read-only audit of the *arr stack.

Probes every *arr (Sonarr, Sonarr2, Radarr, Radarr2, Prowlarr, Bazarr) and
Prowlarr's downstream-app sync, reporting:
 1. indexer count + origin (Prowlarr-managed vs manual entries that should
    be removed)
 2. Prowlarr -> *arr sync registration (which downstream apps Prowlarr knows
    about)
 3. download clients per *arr (should be qBit only, working credentials)
 4. quality profile inventory (preface to TRaSH compliance check)
 5. root folder presence + free space
 6. indexer test results (which return 429 / fail / time out)
 7. tags + tag mappings (anime -> sonarr2/radarr2)
 8. category drift — *arr indexer categories vs Prowlarr indexer capabilities

Read-only — never mutates state. Output goes to stdout as markdown.

Two transport modes:

  public (default): reads `secrets/seedbox.host` + `secrets/htpasswd.password`
    and hits the public URL through nginx. Works from the workstation.

  loopback: set `QFLIX_ARR_AUDIT_LOOPBACK=1` to hit each *arr directly on
    127.0.0.1:{port} using `secrets/{slug}.port`. No htpasswd needed; faster
    and more reliable when running on the seedbox itself (the weekly
    manitoba-maint-arr-audit timer uses this mode).

Readarr removed 2026-05-16 — app purged 2026-05-11 per arr-housekeeping.py
comment.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRETS = Path(os.environ.get("MANITOBA_SECRETS", str(REPO_ROOT / "secrets")))
LOOPBACK = os.environ.get("QFLIX_ARR_AUDIT_LOOPBACK", "0") == "1"


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


def _resolve_public_host() -> str:
    env = os.environ.get("ARR_HOST")
    if env:
        return env
    fqdn = _read(SECRETS / "seedbox.host")
    return f"https://{fqdn}" if fqdn else ""


PUBLIC_HOST = _resolve_public_host()

ARRS = [
    {"name": "sonarr",   "apiver": "v3", "kind": "tv"},
    {"name": "sonarr2",  "apiver": "v3", "kind": "anime"},
    {"name": "radarr",   "apiver": "v3", "kind": "movie"},
    {"name": "radarr2",  "apiver": "v3", "kind": "anime-movie"},
]
PROWLARR = {"name": "prowlarr", "apiver": "v1"}


def _urlbase(slug: str) -> str:
    return _read(SECRETS / f"{slug}.urlbase") or slug


def _port(slug: str) -> str:
    return _read(SECRETS / f"{slug}.port")


def _api_key(slug: str) -> str:
    return _read(SECRETS / f"{slug}.key")


def _basic_auth_header() -> str:
    pw = _read(SECRETS / "htpasswd.password")
    raw = f"quadstronaut:{pw}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _build_url(arr: dict, path: str, *, query: dict | None = None) -> str:
    urlbase = _urlbase(arr["name"])
    qs = ("?" + urlencode(query)) if query else ""
    if LOOPBACK:
        port = _port(arr["name"])
        return f"http://127.0.0.1:{port}/{urlbase}/api/{arr['apiver']}/{path}{qs}"
    return f"{PUBLIC_HOST}/{urlbase}/api/{arr['apiver']}/{path}{qs}"


def _headers(arr: dict, *, json_body: bool = False) -> dict:
    h = {
        "X-Api-Key": _api_key(arr["name"]),
        "Accept": "application/json",
    }
    if not LOOPBACK:
        h["Authorization"] = _basic_auth_header()
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _api_get(arr: dict, path: str, *, query: dict | None = None) -> dict | list | None:
    url = _build_url(arr, path, query=query)
    req = urllib.request.Request(url, headers=_headers(arr))
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        sys.stderr.write(f"  ! {arr['name']} {path}: HTTP {exc.code}\n")
    except Exception as exc:
        sys.stderr.write(f"  ! {arr['name']} {path}: {exc}\n")
    return None


def _api_post(arr: dict, path: str, body: dict) -> tuple[int, dict | None]:
    url = _build_url(arr, path)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers=_headers(arr, json_body=True),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="ignore") if hasattr(exc, "read") else ""
        return exc.code, {"_err": body_text[:200]}
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
            name = idx.get("name", "?")
            impl = idx.get("implementation", "?")
            tags = idx.get("tags", []) or []
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
    missing = []
    for arr in ARRS:
        present = any(arr["name"].lower() in (n or "").lower() for n in by_name)
        if not present:
            missing.append(arr["name"])
    if missing:
        print(f"\n⚠ **Missing from Prowlarr Applications**: {', '.join(missing)}")
    else:
        print(f"\n✓ All {len(ARRS)} *arrs registered with Prowlarr")
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
    print("Note: sampling Prowlarr's last-known stats — no live re-testing.\n")
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
        s = by_id.get(iid, {})
        avg_resp = s.get("averageResponseTime", -1)
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

    apps = _api_get(PROWLARR, "applications") or []
    print("\n#### Prowlarr applications — tag routing")
    for a in apps:
        print(f"- `{a.get('name', '?')}` tags={a.get('tags', [])}")
    return out


# --- 8. Category drift ----------------------------------------------------
#
# Each *arr declares which Prowlarr-synced category IDs it consumes (the
# `categories` field on each indexer config). Prowlarr's indexer
# `capabilities.categories` reports what each *indexer* announces. A *arr
# asking for cat=2010 from an indexer that only announces {2000,5000} is
# the "Query successful, but no results in the configured categories"
# runtime warning surfaced in WARN.md (2026-05-16) — radarr 2×, radarr2 4×.
# This audit flags those mismatches up-front so the operator can adjust
# either side instead of waiting for the runtime log to surface it.

def _indexer_caps_by_name() -> dict[str, set[int]]:
    """Return {indexer_name: {category_ids_it_announces}} from Prowlarr."""
    idxs = _api_get(PROWLARR, "indexer") or []
    out: dict[str, set[int]] = {}
    for idx in idxs:
        name = idx.get("name", "?")
        cats: set[int] = set()
        caps = (idx.get("capabilities") or {}).get("categories") or []
        for c in caps:
            cid = c.get("id")
            if isinstance(cid, int):
                cats.add(cid)
            for sub in c.get("subCategories") or []:
                sid = sub.get("id")
                if isinstance(sid, int):
                    cats.add(sid)
        out[name] = cats
    return out


def audit_category_drift() -> dict:
    section("8. Category drift — *arr requested vs indexer announced")
    caps = _indexer_caps_by_name()
    print(f"Prowlarr indexer capability dump: {len(caps)} indexer(s).\n")

    drift_count = 0
    out: dict = {}
    for arr in ARRS:
        idxs = _api_get(arr, "indexer") or []
        per_arr = []
        for idx in idxs:
            name = idx.get("name", "?")
            requested: set[int] = set()
            for f in idx.get("fields", []) or []:
                if f.get("name") in ("categories", "animeCategories"):
                    val = f.get("value") or []
                    if isinstance(val, list):
                        for c in val:
                            if isinstance(c, int):
                                requested.add(c)
            if not requested:
                continue
            announced = caps.get(name, set())
            if not announced:
                # We don't know what this indexer announces — likely manual
                # entry that isn't Prowlarr-managed, or Prowlarr lookup
                # failed mid-audit. Skip rather than false-flag.
                continue
            missing = requested - announced
            if missing:
                drift_count += 1
                per_arr.append({"indexer": name, "requested_missing": sorted(missing)})
                print(f"- ⚠ `{arr['name']} -> {name}`: requested categories not announced: {sorted(missing)}")
        out[arr["name"]] = per_arr

    if drift_count == 0:
        print("✓ No category drift detected.")
    else:
        print(f"\n**Total drift entries: {drift_count}**")
        print("Operator action: adjust *arr indexer category selection or "
              "update Prowlarr indexer capability sync.")
    return {"drift_count": drift_count, "per_arr": out}


# --- main -----------------------------------------------------------------

def main() -> int:
    print("# *arr stack audit — read-only")
    print(f"\nGenerated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"\nTransport: `{'loopback' if LOOPBACK else 'public'}`")
    if not LOOPBACK:
        print(f"Host: `{PUBLIC_HOST or '(secrets/seedbox.host missing!)'}`")
    print()

    if not LOOPBACK and not PUBLIC_HOST:
        sys.stderr.write(
            "ERROR: secrets/seedbox.host missing and QFLIX_ARR_AUDIT_LOOPBACK "
            "not set — cannot resolve a transport target.\n"
        )
        return 2

    audit_indexers_per_arr()
    audit_prowlarr_apps()
    audit_download_clients()
    audit_profiles_and_roots()
    audit_indexer_health()
    audit_tags()
    audit_category_drift()

    print("\n## Summary\n")
    print("See sections above for per-area findings. Operator action items "
          "from earlier audits are tracked at `docs/arr-audit-actions-2026-05-09.md`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
