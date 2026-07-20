#!/usr/bin/env python3
"""scripts/configure/90b-usenet-all-arrs.py — usenet everywhere + SAB hardening.

Why: 90-sabnzbd-usenet-install.sh (2026-06-22) wired SABnzbd + NZBgeek into
Sonarr only. Radarr/sonarr2/radarr2 have never had a usenet download client,
so during the SAB stuck-handling-parity build (spec
docs/superpowers/specs/2026-07-19-sab-stuck-parity-design.md, sections
C7/C8) any FDH/unstick remediation that lands in those three arrs has no
usenet leg to fall back to — same failure class the original buildout fixed
for Sonarr, just unfixed everywhere else. This script closes that gap and
applies the one SAB-side hardening the research sweep flagged as unsafe:
`history_limit` auto-pruning history rows out from under Sonarr/Radarr's
FailedDownloadService before it can reconcile a Failed row (GH-documented
race; see spec research section).

Per *arr in {radarr, sonarr2, radarr2}:
  1. SABnzbd download client — host 172.17.0.1 (docker0 bridge gateway, same
     pattern as Tautulli's pms_url / Seerr's hostname), category = arr slug,
     removeCompletedDownloads/removeFailedDownloads = True, priority fields
     matching the -100/-100 the original sonarr wiring used.
  2. NZBgeek Newznab indexer added DIRECT to the arr. Prowlarr's app-sync
     silently skips Usenet indexers that return no hits to its blank-term
     category probe (2026-06-22 lesson, still true) — so, same as sonarr,
     these three hold NZBgeek themselves rather than syncing it from
     Prowlarr.
  3. Delay profile: enableUsenet=True on every profile that doesn't already
     have it (2026-06-22 lesson: a fresh arr ships enableUsenet=False, which
     silently drops usenet grabs even with a client + indexer configured).
     preferredProtocol is left AS-IS unless the field is entirely absent —
     this script arms usenet as an option, it does not relitigate which
     protocol wins by default.
  4. FailedDownloadService: autoRedownloadFailed=True — reported either way,
     flipped only if off.

SAB side: ensure categories `radarr` / `sonarr2` / `radarr2` exist (dir
mirrors the category name, same layout the original sonarr category used),
then `history_limit` 10 -> 0. `fail_hopeless_jobs` / `fast_fail` /
`pause_on_post_processing` are REPORT-ONLY — the research sweep confirmed
all three correct as-is; this script never touches them.

Idempotent: every step reads current state first and no-ops when already
configured — safe to re-run. Modes: default = DRY-RUN (reads only, prints
the plan, mutates nothing). --execute arms every POST/PUT/set_config call.

Run on the seedbox (reads ~/secrets directly), same convention as
30-seerr-arrs.py / 57-no-4k-enforce.py. Every payload builder and
already-configured predicate below is a pure, importable function so it can
be unit-tested with transports mocked — see
tests/unit/test_usenet_all_arrs.py. Pipe over SSH like its siblings:
  sshm "python3 -" < scripts/configure/90b-usenet-all-arrs.py -- --execute
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from pathlib import Path

SECRETS = Path(os.path.expanduser("~/secrets"))

# arr slug -> media kind. Drives which *arr-side field names (tvCategory vs
# movieCategory) and which Newznab category block (5xxx vs 2xxx) apply.
ARRS = {
    "radarr":  "movie",
    "sonarr2": "tv",
    "radarr2": "movie",
}

# Newznab category ids per kind. TV block is copied verbatim from the
# original sonarr wiring (90-sabnzbd-usenet-install.sh); the movie block
# mirrors the same shape (base + Foreign/Other/SD/HD/UHD/BluRay/3D) using
# the standard Newznab 2xxx Movies ids.
CATEGORIES = {
    "tv":    [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5090],
    "movie": [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060],
}

# *arr download-client schema field names differ between Sonarr (tv) and
# Radarr (movie) for the category + priority fields.
FIELD_MAP = {
    "tv":    {"category": "tvCategory",    "recent": "recentTvPriority",    "older": "olderTvPriority"},
    "movie": {"category": "movieCategory", "recent": "recentMoviePriority", "older": "olderMoviePriority"},
}

# SAB misc flags the research sweep confirmed correct as-is (C8) — reported
# every run, never mutated by this script.
MISC_EXPECTED = {
    "fail_hopeless_jobs": True,
    "fast_fail": True,
    "pause_on_post_processing": True,
}


# ===========================================================================
# Secrets (matches 30-seerr-arrs.py / 57-no-4k-enforce.py convention: read
# straight off disk, no shared lib import, so the script stays a single
# self-contained file that can be piped over SSH or imported by tests).
# ===========================================================================

def secret(name: str) -> str:
    return (SECRETS / name).read_text(encoding="utf-8").strip()


def secret_or(name: str, fallback: str) -> str:
    try:
        return secret(name)
    except FileNotFoundError:
        return fallback


# ===========================================================================
# Pure helpers — payload builders + already-configured predicates. No I/O.
# These are the functions tests/unit/test_usenet_all_arrs.py exercises
# directly; every network call above them is a thin, untested wrapper.
# ===========================================================================

def find_sab_client(clients: list) -> dict:
    """Return the first Sabnzbd download client (any enable state) or None.
    Presence — not enabled-ness — is the idempotency key (council 2026-07-20,
    Defect 6): keying the skip on `enable` alone meant a present-but-DISABLED
    client failed the check, so every --execute re-POSTed a second "SABnzbd"
    client that never converged. We converge on presence and report a disabled
    one rather than silently re-adding or force-enabling an operator's
    deliberate choice."""
    return next((c for c in clients or [] if c.get("implementation") == "Sabnzbd"), None)


def has_enabled_sab_client(clients: list) -> bool:
    """True iff an already-enabled Sabnzbd client exists. Retained for the
    tests that assert the enabled-vs-disabled distinction; orchestration uses
    find_sab_client for the idempotency decision."""
    c = find_sab_client(clients)
    return bool(c and c.get("enable"))


def build_sab_downloadclient_setv(kind: str, slug: str, port: str, apikey: str) -> dict:
    """Field-name/value map for the SABnzbd download-client schema, keyed by
    media kind. `port` is coerced to int (the *arr schema expects a number;
    secrets are always stored as plain-text strings)."""
    fields = FIELD_MAP[kind]
    return {
        "host": "172.17.0.1",
        "port": int(port),
        "urlBase": "sabnzbd",
        "apiKey": apikey,
        fields["category"]: slug,
        "useSsl": False,
        fields["recent"]: -100,
        fields["older"]: -100,
        "removeCompletedDownloads": True,
        "removeFailedDownloads": True,
    }


def _apply_field_values(schema: dict, setv: dict) -> dict:
    """Deep-copy `schema` and set `fields[i]['value']` for every field name
    present in `setv`. Shared shape between the download-client and indexer
    builders — mirrors the `for f in sch["fields"]: ...` loop
    90-sabnzbd-usenet-install.sh uses against the same *arr schema APIs."""
    out = deepcopy(schema)
    for f in out.get("fields") or []:
        if f.get("name") in setv:
            f["value"] = setv[f["name"]]
    return out


def build_sab_downloadclient_payload(schema: dict, kind: str, slug: str, port: str, apikey: str) -> dict:
    """Full POST body for /downloadclient: patched schema + name/enable."""
    out = _apply_field_values(schema, build_sab_downloadclient_setv(kind, slug, port, apikey))
    out["name"] = "SABnzbd"
    out["enable"] = True
    return out


def has_nzbgeek_indexer(indexers: list) -> bool:
    return any(i.get("name") == "NZBgeek" for i in indexers or [])


def build_nzbgeek_payload(schema_template: dict, kind: str, url: str, key: str) -> dict:
    """Full POST body for /indexer: patched Newznab schema template, named
    NZBgeek, categories per media kind. Mirrors the sonarr Newznab payload
    in 90-sabnzbd-usenet-install.sh (priority=25, RSS/automatic/interactive
    search all on, id/infoLink/presets stripped since this is an ADD)."""
    setv = {
        "baseUrl": url or "https://api.nzbgeek.info",
        "apiPath": "/api",
        "apiKey": key,
        "categories": CATEGORIES[kind],
    }
    out = _apply_field_values(schema_template, setv)
    out["name"] = "NZBgeek"
    out["priority"] = 25
    out["enableRss"] = out["enableAutomaticSearch"] = out["enableInteractiveSearch"] = True
    for k in ("id", "infoLink", "presets"):
        out.pop(k, None)
    return out


def delay_profile_patch(profile: dict):
    """Return a patched copy of `profile` with usenet enabled, or None if
    it's already enabled (idempotency predicate + patch in one, since the
    caller needs both: skip-or-apply). preferredProtocol is left untouched
    unless the field is missing/empty — C7 explicitly does NOT re-litigate
    protocol preference, only makes usenet available."""
    if profile.get("enableUsenet"):
        return None
    out = deepcopy(profile)
    out["enableUsenet"] = True
    out["usenetDelay"] = 0
    if not out.get("preferredProtocol"):
        out["preferredProtocol"] = "usenet"
    return out


def config_downloadclient_patch(cfg: dict):
    """Return a patched copy of the /config/downloadclient resource with
    autoRedownloadFailed forced True, or None if already True."""
    if cfg.get("autoRedownloadFailed"):
        return None
    out = deepcopy(cfg)
    out["autoRedownloadFailed"] = True
    return out


def has_sab_category(cats: list, slug: str) -> bool:
    return any(c.get("name") == slug for c in cats or [])


def sab_category_params(slug: str) -> dict:
    """SAB `mode=set_config&section=categories` query params for one arr
    slug. `dir` mirrors the keyword, same layout the original sonarr
    category used (dir="sonarr" for keyword="sonarr")."""
    return {
        "mode": "set_config", "section": "categories",
        "keyword": slug, "dir": slug,
        "priority": "-100", "pp": "3", "script": "Default",
    }


def history_limit_needs_fix(current) -> bool:
    """True unless `current` is already 0 (any of int 0, "0", or falsy-but-
    present numeric-string forms). Anything unparseable is treated as
    needing a fix — we'd rather set it again than silently trust garbage."""
    try:
        return int(current) != 0
    except (TypeError, ValueError):
        return str(current).strip() != "0"


def history_limit_params() -> dict:
    return {"mode": "set_config", "section": "misc", "keyword": "history_limit", "value": "0"}


def misc_flags_report(misc_cfg: dict) -> list:
    """Report-only rows for the three SAB misc flags C8's research sweep
    confirmed correct as-is. Never used to decide a write — surfaced purely
    so a human reviewing --dry-run output can see them and catch drift."""
    rows = []
    for name, expected in MISC_EXPECTED.items():
        actual = misc_cfg.get(name)
        rows.append({"name": name, "expected": expected, "actual": actual,
                      "ok": bool(actual) == expected})
    return rows


# ===========================================================================
# Transports — thin urllib wrappers, deliberately dumb so the logic above
# stays pure and testable. Mirrors lib/arr_client.py's (status, body) return
# convention (0-status dict on transport failure) without importing it, to
# keep this script a single self-contained file (30-seerr-arrs.py style).
# ===========================================================================

def arr_ctx(slug: str) -> dict:
    return {
        "slug": slug,
        "key": secret(f"{slug}.key"),
        "port": secret(f"{slug}.port"),
        "base": secret_or(f"{slug}.urlbase", slug).strip("/"),
    }


def arr_call(ctx: dict, method: str, path: str, body=None, timeout: int = 30):
    url = "http://127.0.0.1:{}/{}/api/v3{}".format(ctx["port"], ctx["base"], path)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-Api-Key": ctx["key"]}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"raw": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


def sab_call(params: dict, timeout: int = 40):
    key, port = secret("sabnzbd.key"), secret("sabnzbd.port")
    url = "http://127.0.0.1:{}/sabnzbd/api".format(port)
    qs = urllib.parse.urlencode({**params, "apikey": key, "output": "json"})
    try:
        return json.load(urllib.request.urlopen(url + "?" + qs, timeout=timeout))
    except Exception as e:
        return {"error": str(e)}


# ===========================================================================
# Orchestration — one function per C7/C8 step, each: GET current state,
# decide via the pure helpers above, POST/PUT/set_config only when
# execute=True. Returns a human-readable line (or lines) for the run report.
# ===========================================================================

def ensure_download_client(ctx: dict, kind: str, execute: bool) -> str:
    code, clients = arr_call(ctx, "GET", "/downloadclient")
    if code != 200 or not isinstance(clients, list):
        return "[{}] GET /downloadclient failed: {} {}".format(ctx["slug"], code, clients)
    existing = find_sab_client(clients)
    if existing is not None:
        # Converge on PRESENCE (Defect 6): re-POSTing because a present client
        # is merely disabled would spawn duplicate "SABnzbd" clients every run.
        if existing.get("enable"):
            return "[{}] SABnzbd download client already present+enabled".format(ctx["slug"])
        return ("[{}] SABnzbd download client present but DISABLED "
                "(id={}) — leaving as-is; enable it in the arr UI if intended"
                .format(ctx["slug"], existing.get("id")))
    code, schema_list = arr_call(ctx, "GET", "/downloadclient/schema")
    if code != 200 or not isinstance(schema_list, list):
        return "[{}] GET /downloadclient/schema failed: {} {}".format(ctx["slug"], code, schema_list)
    schema = next((s for s in schema_list if s.get("implementation") == "Sabnzbd"), None)
    if schema is None:
        return "[{}] no Sabnzbd schema offered by this arr".format(ctx["slug"])
    if not execute:
        return "[{}] DRY-RUN would add SABnzbd download client (category={})".format(ctx["slug"], ctx["slug"])
    payload = build_sab_downloadclient_payload(
        schema, kind, ctx["slug"], secret("sabnzbd.port"), secret("sabnzbd.key"))
    code, resp = arr_call(ctx, "POST", "/downloadclient", body=payload)
    if code in (200, 201):
        return "[{}] added SABnzbd download client".format(ctx["slug"])
    return "[{}] FAILED add SABnzbd download client: {} {}".format(ctx["slug"], code, str(resp)[:200])


def ensure_indexer(ctx: dict, kind: str, execute: bool) -> str:
    code, indexers = arr_call(ctx, "GET", "/indexer")
    if code != 200 or not isinstance(indexers, list):
        return "[{}] GET /indexer failed: {} {}".format(ctx["slug"], code, indexers)
    if has_nzbgeek_indexer(indexers):
        return "[{}] NZBgeek indexer already present".format(ctx["slug"])
    code, schema_list = arr_call(ctx, "GET", "/indexer/schema")
    if code != 200 or not isinstance(schema_list, list):
        return "[{}] GET /indexer/schema failed: {} {}".format(ctx["slug"], code, schema_list)
    tmpl = next((s for s in schema_list if s.get("implementation") == "Newznab"), None)
    if tmpl is None:
        return "[{}] no Newznab schema offered by this arr".format(ctx["slug"])
    if not execute:
        return "[{}] DRY-RUN would add NZBgeek indexer (categories={})".format(ctx["slug"], CATEGORIES[kind])
    url = secret_or("nzbgeek.url", "https://api.nzbgeek.info")
    payload = build_nzbgeek_payload(tmpl, kind, url, secret("nzbgeek.key"))
    code, resp = arr_call(ctx, "POST", "/indexer", body=payload)
    if code in (200, 201):
        return "[{}] added NZBgeek (Newznab) indexer".format(ctx["slug"])
    return "[{}] FAILED add NZBgeek indexer: {} {}".format(ctx["slug"], code, str(resp)[:200])


def ensure_delay_profiles(ctx: dict, execute: bool) -> str:
    code, profiles = arr_call(ctx, "GET", "/delayprofile")
    if code != 200 or not isinstance(profiles, list):
        return "[{}] GET /delayprofile failed: {} {}".format(ctx["slug"], code, profiles)
    if not profiles:
        return "[{}] no delay profiles found".format(ctx["slug"])
    lines = []
    for p in profiles:
        patched = delay_profile_patch(p)
        if patched is None:
            lines.append("[{}] delay profile {} already enableUsenet=True".format(ctx["slug"], p.get("id")))
            continue
        if not execute:
            lines.append("[{}] DRY-RUN would enable usenet on delay profile {}".format(ctx["slug"], p.get("id")))
            continue
        code, resp = arr_call(ctx, "PUT", "/delayprofile/{}".format(patched["id"]), body=patched)
        if code in (200, 202):
            lines.append("[{}] delay profile {} -> enableUsenet=True".format(ctx["slug"], patched["id"]))
        else:
            lines.append("[{}] FAILED PUT delay profile {}: {} {}".format(
                ctx["slug"], patched["id"], code, str(resp)[:200]))
    return "\n".join(lines)


def ensure_fdh(ctx: dict, execute: bool) -> str:
    code, cfg = arr_call(ctx, "GET", "/config/downloadclient")
    if code != 200 or not isinstance(cfg, dict):
        return "[{}] GET /config/downloadclient failed: {} {}".format(ctx["slug"], code, cfg)
    patched = config_downloadclient_patch(cfg)
    if patched is None:
        return "[{}] autoRedownloadFailed already True".format(ctx["slug"])
    if not execute:
        return "[{}] DRY-RUN would set autoRedownloadFailed=True".format(ctx["slug"])
    code, resp = arr_call(ctx, "PUT", "/config/downloadclient/{}".format(cfg.get("id", 1)), body=patched)
    if code in (200, 202):
        return "[{}] autoRedownloadFailed -> True".format(ctx["slug"])
    return "[{}] FAILED PUT config/downloadclient: {} {}".format(ctx["slug"], code, str(resp)[:200])


def ensure_sab_categories(execute: bool) -> list:
    resp = sab_call({"mode": "get_config", "section": "categories"})
    if not isinstance(resp, dict) or "error" in resp:
        return ["[sab] GET categories failed: {}".format(resp)]
    existing = (resp.get("config") or {}).get("categories") or []
    lines = []
    for slug in ARRS:
        if has_sab_category(existing, slug):
            lines.append("[sab] category '{}' already present".format(slug))
        elif not execute:
            lines.append("[sab] DRY-RUN would add category '{}'".format(slug))
        else:
            out = sab_call(sab_category_params(slug))
            lines.append("[sab] added category '{}': {}".format(slug, out))
    return lines


def ensure_history_limit(execute: bool) -> tuple:
    """Returns (report_lines, misc_cfg) — misc_cfg is reused by the
    report-only misc-flags check so we only fetch section=misc once."""
    resp = sab_call({"mode": "get_config", "section": "misc"})
    if not isinstance(resp, dict) or "error" in resp:
        return (["[sab] GET misc config failed: {}".format(resp)], {})
    misc_cfg = (resp.get("config") or {}).get("misc") or {}
    current = misc_cfg.get("history_limit")
    if not history_limit_needs_fix(current):
        return (["[sab] history_limit already 0"], misc_cfg)
    if not execute:
        return (["[sab] DRY-RUN would set history_limit {} -> 0".format(current)], misc_cfg)
    sab_call(history_limit_params())
    return (["[sab] history_limit {} -> 0".format(current)], misc_cfg)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true",
                     help="apply changes; default is a read-only dry-run plan")
    args = ap.parse_args()
    execute = args.execute
    mode = "EXECUTE" if execute else "DRY-RUN"

    print("=== 90b-usenet-all-arrs ({}) ===".format(mode))

    print()
    print("--- SAB categories ---")
    for line in ensure_sab_categories(execute):
        print(line)

    print()
    print("--- SAB history_limit ---")
    hist_lines, misc_cfg = ensure_history_limit(execute)
    for line in hist_lines:
        print(line)

    print()
    print("--- SAB misc flags (report only, never mutated) ---")
    for row in misc_flags_report(misc_cfg):
        flag = "OK" if row["ok"] else "MISMATCH"
        print("[sab] {} = {} (expected {}) [{}]".format(row["name"], row["actual"], row["expected"], flag))

    for slug, kind in ARRS.items():
        print()
        print("--- {} ({}) ---".format(slug, kind))
        try:
            ctx = arr_ctx(slug)
        except FileNotFoundError as exc:
            print("[{}] skipped: missing secret {}".format(slug, exc))
            continue
        print(ensure_download_client(ctx, kind, execute))
        print(ensure_indexer(ctx, kind, execute))
        print(ensure_delay_profiles(ctx, execute))
        print(ensure_fdh(ctx, execute))

    return 0


if __name__ == "__main__":
    sys.exit(main())
