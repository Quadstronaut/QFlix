#!/usr/bin/env python3
"""One-shot functional audit. Hits a real feature endpoint on each app
(not just /ping) and reports a one-liner per app.

Designed to run ON the seedbox where loopback to all apps is direct.
Reads secrets from ~/secrets/. No CLI args. Output is a fixed-width table.
"""
from __future__ import annotations
import base64, json, os, re, ssl, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

SECRETS = Path.home() / "secrets"
HOST = (SECRETS / "seedbox.host").read_text().strip()
HTPW = (SECRETS / "htpasswd.password").read_text().strip()

# Disable cert verification — internal loopback URLs use the same nginx cert
# as public, but the internal call is by IP so SAN mismatch.
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _read(name: str) -> str:
    p = SECRETS / name
    return p.read_text().strip() if p.exists() else ""


def _get(url: str, headers: dict | None = None, timeout: int = 8,
         data: bytes | None = None, method: str = "GET") -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, method=method,
                                  headers=headers or {})
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return 0, str(e)


def row(name: str, code: int | str, detail: str):
    print(f"  {name:<26} HTTP={str(code):<4} {detail[:120]}")


def section(title: str):
    print()
    print(f"━━━━━━ {title} ━━━━━━")


def arr_app(name: str, secrets_prefix: str, api_version: str, item_endpoint: str, item_key: str):
    key = _read(f"{secrets_prefix}.key")
    port = _read(f"{secrets_prefix}.port")
    base = _read(f"{secrets_prefix}.urlbase")
    if not (key and port):
        row(name, "-", "secrets missing")
        return
    hdr = {"X-Api-Key": key}
    root = f"http://127.0.0.1:{port}/{base}/api/{api_version}"

    code, body = _get(f"{root}/{item_endpoint}", hdr)
    if code == 200:
        try:
            items = json.loads(body)
            count = len(items) if isinstance(items, list) else items.get("totalRecords", "?")
            row(f"{name} {item_endpoint}", code, f"count={count}")
        except Exception as e:
            row(f"{name} {item_endpoint}", code, f"parse-fail: {e}")
    else:
        row(f"{name} {item_endpoint}", code, body[:80])

    code, body = _get(f"{root}/queue?pageSize=1", hdr)
    if code == 200:
        try:
            d = json.loads(body)
            row(f"{name} queue", code, f"total={d.get('totalRecords', 0)} records-page={len(d.get('records', []))}")
        except Exception:
            row(f"{name} queue", code, "parse-fail")
    else:
        row(f"{name} queue", code, "")

    code, body = _get(f"{root}/health", hdr)
    if code == 200:
        try:
            issues = json.loads(body)
            n = len(issues) if isinstance(issues, list) else 0
            sample = ""
            if issues:
                sample = " sample=" + (issues[0].get("message", "")[:40])
            row(f"{name} health", code, f"issues={n}{sample}")
        except Exception:
            row(f"{name} health", code, "")
    else:
        row(f"{name} health", code, "")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # *arr stack
    section("RADARR (Cinema)")
    arr_app("radarr", "radarr", "v3", "movie", "id")

    section("RADARR2 (Anime Movies)")
    arr_app("radarr2", "radarr2", "v3", "movie", "id")

    section("SONARR (TV)")
    arr_app("sonarr", "sonarr", "v3", "series", "id")
    # wanted/missing
    p, b, k = _read("sonarr.port"), _read("sonarr.urlbase"), _read("sonarr.key")
    code, body = _get(f"http://127.0.0.1:{p}/{b}/api/v3/wanted/missing?pageSize=1", {"X-Api-Key": k})
    try:
        d = json.loads(body) if code == 200 else {}
        row("sonarr wanted-missing", code, f"total={d.get('totalRecords', 0)}")
    except Exception:
        row("sonarr wanted-missing", code, "parse-fail")

    section("SONARR2 (Anime)")
    arr_app("sonarr2", "sonarr2", "v3", "series", "id")

    section("PROWLARR")
    p, b, k = _read("prowlarr.port"), _read("prowlarr.urlbase"), _read("prowlarr.key")
    h = {"X-Api-Key": k}
    code, body = _get(f"http://127.0.0.1:{p}/{b}/api/v1/indexer", h)
    try:
        d = json.loads(body) if code == 200 else []
        en = sum(1 for x in d if x.get("enable"))
        row("prowlarr indexers", code, f"total={len(d)} enabled={en} disabled={len(d) - en}")
    except Exception:
        row("prowlarr indexers", code, body[:80])
    code, body = _get(f"http://127.0.0.1:{p}/{b}/api/v1/indexerstats", h)
    try:
        d = json.loads(body) if code == 200 else {}
        stats = d.get("indexers", [])
        good = sum(1 for x in stats if x.get("numberOfQueries", 0) > 0 and x.get("numberOfFailedQueries", 0) == 0)
        row("prowlarr indexer-stats", code, f"indexers-with-stats={len(stats)} clean={good}")
    except Exception:
        row("prowlarr indexer-stats", code, body[:80])
    code, body = _get(f"http://127.0.0.1:{p}/{b}/api/v1/health", h)
    try:
        d = json.loads(body) if code == 200 else []
        row("prowlarr health", code, f"issues={len(d)}")
    except Exception:
        row("prowlarr health", code, "")

    section("BAZARR + BAZARR2")
    for tag in ("bazarr", "bazarr2"):
        p, b, k = _read(f"{tag}.port"), _read(f"{tag}.urlbase"), _read(f"{tag}.key")
        h = {"X-Api-Key": k}
        code, body = _get(f"http://127.0.0.1:{p}/{b}/api/episodes/wanted?length=1", h)
        try:
            d = json.loads(body) if code == 200 else {}
            row(f"{tag} eps-wanted", code, f"total={d.get('total', 0)}")
        except Exception:
            row(f"{tag} eps-wanted", code, "parse-fail")
        code, body = _get(f"http://127.0.0.1:{p}/{b}/api/system/status", h)
        try:
            d = json.loads(body) if code == 200 else {}
            row(f"{tag} system/status", code, f"data-keys={list(d.get('data', {}).keys())[:4]}")
        except Exception:
            row(f"{tag} system/status", code, "")

    section("QBITTORRENT")
    p = _read("qbittorrent.port")
    u, pw = _read("qbittorrent.user"), _read("qbittorrent.password")
    # Login → grab SID cookie
    body = urllib.parse.urlencode({"username": u, "password": pw}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{p}/api/v2/auth/login",
                                  data=body, method="POST",
                                  headers={"Referer": f"http://127.0.0.1:{p}",
                                           "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            sid = ""
            for ck in r.getheader("Set-Cookie", "").split(","):
                if "SID=" in ck:
                    sid = ck.split("SID=")[1].split(";")[0].strip()
                    break
            row("qbit auth", r.status, f"sid-set={bool(sid)}")
    except Exception as e:
        row("qbit auth", 0, str(e))
        sid = ""

    if sid:
        h = {"Cookie": f"SID={sid}"}
        for label, path in [
            ("qbit version", "/api/v2/app/version"),
            ("qbit transfer/info", "/api/v2/transfer/info"),
            ("qbit torrents/info", "/api/v2/torrents/info"),
            ("qbit categories", "/api/v2/torrents/categories"),
            ("qbit maindata", "/api/v2/sync/maindata"),
        ]:
            code, body = _get(f"http://127.0.0.1:{p}{path}", h)
            if path.endswith("version"):
                row(label, code, body.strip()[:30])
            else:
                try:
                    d = json.loads(body)
                    if path.endswith("torrents/info"):
                        from collections import Counter
                        states = Counter(t["state"] for t in d)
                        row(label, code, f"total={len(d)} states={dict(states.most_common(5))}")
                    elif path.endswith("categories"):
                        row(label, code, f"categories={list(d.keys())}")
                    elif path.endswith("transfer/info"):
                        row(label, code, f"up={d['up_info_speed']//1024}KB dl={d['dl_info_speed']//1024}KB conn={d.get('connection_status')}")
                    elif path.endswith("maindata"):
                        row(label, code, f"server-state-keys={list(d.get('server_state', {}).keys())[:6]}")
                except Exception as e:
                    row(label, code, f"parse-fail {e}")

    section("PLEX")
    p, t = _read("plex.port"), _read("plex.token")
    for label, path in [
        ("plex identity", "/identity"),
        ("plex libraries", "/library/sections"),
        ("plex sessions", "/status/sessions"),
        ("plex transcode/sessions", "/transcode/sessions"),
        ("plex servers", "/servers"),
        ("plex /:/prefs", "/:/prefs"),
    ]:
        code, body = _get(f"http://127.0.0.1:{p}{path}?X-Plex-Token={t}")
        # Plex returns XML
        size = re.search(r'size="(\d+)"', body)
        version = re.search(r'version="([^"]+)"', body)
        info = []
        if size:
            info.append(f"size={size.group(1)}")
        if version and "identity" in path:
            info.append(f"v={version.group(1)}")
        if path == "/library/sections":
            titles = re.findall(r'title="([^"]+)"', body)[:4]
            info.append(f"titles={titles}")
        row(label, code, " ".join(info))

    section("MAINTAINERR")
    mport = _read("maintainerr.port")
    mkey = _read("maintainerr.key")
    basic = base64.b64encode(f"quadstronaut:{HTPW}".encode()).decode()
    userpart, domain = HOST.split(".", 1)
    mhost = f"https://maintainerr-{userpart}.{domain}"
    mh = {"X-Api-Key": mkey, "Authorization": f"Basic {basic}"}
    for label, path in [
        ("mt rules", "/api/rules"),
        ("mt collections", "/api/collections"),
        ("mt plex/libraries", "/api/plex/libraries"),
        ("mt settings", "/api/settings"),
    ]:
        code, body = _get(f"{mhost}{path}", mh)
        try:
            d = json.loads(body)
            if isinstance(d, list):
                row(label, code, f"count={len(d)}")
            else:
                row(label, code, f"keys={list(d.keys())[:6]}" if isinstance(d, dict) else "ok")
        except Exception:
            row(label, code, body[:80])

    section("PLEX-ADJACENT (Tautulli / Seerr / Audiobookshelf / Kavita / Komga / Calibre-Web / Homarr / FlareSolverr)")
    # Tautulli — api_v2 with api_key
    tp = _read("tautulli.port")
    if not tp:
        tp = "8181"
    # Tautulli secret file name: tautulli.api-key or similar
    tkey = ""
    for cand in ("tautulli.key", "tautulli.api-key", "tautulli.api_key"):
        if (SECRETS / cand).exists():
            tkey = _read(cand)
            break
    if tkey:
        code, body = _get(f"http://127.0.0.1:{tp}/api/v2?apikey={tkey}&cmd=get_activity")
        try:
            d = json.loads(body)
            r2 = d.get("response", {})
            data = r2.get("data", {})
            row("tautulli get_activity", code, f"streams={data.get('stream_count', '?')} bw={data.get('total_bandwidth', '?')}KB/s")
        except Exception as e:
            row("tautulli get_activity", code, f"parse-fail {e}")
        code, body = _get(f"http://127.0.0.1:{tp}/api/v2?apikey={tkey}&cmd=get_libraries")
        try:
            d = json.loads(body)
            libs = d.get("response", {}).get("data", [])
            row("tautulli get_libraries", code, f"libs={len(libs)}")
        except Exception:
            row("tautulli get_libraries", code, "")
    else:
        row("tautulli", "-", "no api key secret file found")

    # Seerr (Jellyseerr-compatible API)
    sp = _read("seerr.port")
    skey = _read("seerr.key")
    if sp and skey:
        sh = {"X-Api-Key": skey}
        code, body = _get(f"http://127.0.0.1:{sp}/api/v1/status", sh)
        try:
            d = json.loads(body) if code == 200 else {}
            row("seerr status", code, f"v={d.get('version', '?')}")
        except Exception:
            row("seerr status", code, body[:60])
        code, body = _get(f"http://127.0.0.1:{sp}/api/v1/request?take=1", sh)
        try:
            d = json.loads(body) if code == 200 else {}
            page = d.get("pageInfo", {})
            row("seerr requests", code, f"total={page.get('results', 0)}")
        except Exception:
            row("seerr requests", code, "")
        code, body = _get(f"http://127.0.0.1:{sp}/api/v1/user?take=1", sh)
        try:
            d = json.loads(body) if code == 200 else {}
            page = d.get("pageInfo", {})
            row("seerr users", code, f"total={page.get('results', 0)}")
        except Exception:
            row("seerr users", code, "")

    # Audiobookshelf
    ap = _read("audiobookshelf.port")
    akey = _read("audiobookshelf.key")
    if ap and akey:
        ah = {"Authorization": f"Bearer {akey}"}
        code, body = _get(f"http://127.0.0.1:{ap}/api/libraries", ah)
        try:
            d = json.loads(body) if code == 200 else {}
            libs = d.get("libraries", [])
            row("audiobookshelf libs", code, f"libs={len(libs)} titles={[l.get('name') for l in libs[:4]]}")
        except Exception:
            row("audiobookshelf libs", code, body[:60])

    # Kavita — bearer in /api/Auth/login first
    kvp = _read("kavita.port")
    if kvp:
        code, body = _get(f"http://127.0.0.1:{kvp}/api/health")
        row("kavita /api/health", code, body[:40])

    # Komga
    kgp = _read("komga.port")
    if kgp:
        code, body = _get(f"http://127.0.0.1:{kgp}/actuator/health")
        try:
            d = json.loads(body) if code == 200 else {}
            row("komga actuator", code, f"status={d.get('status', '?')}")
        except Exception:
            row("komga actuator", code, body[:40])

    # Calibre-Web — port-only check (no API)
    cwp = _read("calibre-web.port")
    if cwp:
        code, body = _get(f"http://127.0.0.1:{cwp}/")
        row("calibre-web /", code, "(login page)" if "login" in body.lower() else body[:40])

    # Homarr
    hp = _read("homarr.port")
    if hp:
        code, body = _get(f"http://127.0.0.1:{hp}/api/health")
        row("homarr api/health", code, body[:60])

    # FlareSolverr — hostname is 172.17.0.1 not 127.0.0.1
    fp = _read("flaresolverr.port")
    if fp:
        for host_try in ("172.17.0.1", "127.0.0.1"):
            code, body = _get(f"http://{host_try}:{fp}/")
            row(f"flaresolverr {host_try}", code, body[:60])
            if code == 200:
                # Try /v1 with a dummy request
                req_body = json.dumps({"cmd": "sessions.list"}).encode()
                code, body = _get(f"http://{host_try}:{fp}/v1",
                                  {"Content-Type": "application/json"},
                                  data=req_body, method="POST")
                try:
                    d = json.loads(body) if code == 200 else {}
                    row("flaresolverr v1", code, f"status={d.get('status', '?')} msg={d.get('message', '')[:40]}")
                except Exception:
                    row("flaresolverr v1", code, body[:60])
                break

    section("TDARR")
    tdp = _read("tdarr.server_port")
    if not tdp:
        # Try from versions.env
        try:
            for line in (Path.home() / "scripts" / "configure" / "55-kometa-install.sh").read_text().splitlines():
                pass
        except Exception:
            pass
    if tdp:
        code, body = _get(f"http://127.0.0.1:{tdp}/api/v2/cruddb",
                          {"Content-Type": "application/json"},
                          data=json.dumps({"data": {"collection": "FileJSONDB", "mode": "getAll"}}).encode(),
                          method="POST")
        try:
            d = json.loads(body) if code == 200 else []
            row("tdarr files-db", code, f"files-tracked={len(d) if isinstance(d, list) else '?'}")
        except Exception:
            row("tdarr files-db", code, body[:60])
        code, body = _get(f"http://127.0.0.1:{tdp}/api/v2/status")
        row("tdarr /api/v2/status", code, body[:60])

    section("UNPACKERR / POSTGRES / LISTMONK / RECYCLARR")
    # Unpackerr — listens on port for /api/v1/info if configured
    up_port = _read("unpackerr.port")
    if up_port:
        code, body = _get(f"http://127.0.0.1:{up_port}/api/v1/info")
        row("unpackerr api/v1/info", code, body[:60])

    # Postgres — only port check (no HTTP API)
    pgp = _read("postgres.port")
    if pgp:
        # Use psql via ports
        row("postgres", "-", f"port={pgp} (psql probe is separate)")

    # Listmonk
    lp = _read("listmonk.port")
    lat = _read("listmonk.api_token")
    if lp and lat:
        lu = _read("listmonk.api_user")
        basic_lm = base64.b64encode(f"{lu}:{lat}".encode()).decode()
        code, body = _get(f"http://127.0.0.1:{lp}/api/lists",
                          {"Authorization": f"Basic {basic_lm}"})
        try:
            d = json.loads(body) if code == 200 else {}
            data = d.get("data", {})
            results = data.get("results", [])
            row("listmonk lists", code, f"lists={len(results)} titles={[l.get('name') for l in results[:3]]}")
        except Exception:
            row("listmonk lists", code, body[:60])

    section("VICTORIALOGS / KUMA-PUSHER-SELF")
    # VLogs query — count log entries in last 5min
    vp = _read("vlogs.port") or _read("victorialogs.port")
    if vp:
        code, body = _get(f"http://127.0.0.1:{vp}/health")
        row("vlogs health", code, body.strip()[:30])
        # Last 5min logs
        q = urllib.parse.urlencode({"query": "* | stats count() as n", "start": "5m"})
        code, body = _get(f"http://127.0.0.1:{vp}/select/logsql/query?{q}")
        try:
            # Returns JSONL — first line is the count
            lines = body.strip().splitlines()
            d = json.loads(lines[0]) if lines else {}
            row("vlogs count(5m)", code, f"lines-last-5m={d.get('n', '?')}")
        except Exception:
            row("vlogs count(5m)", code, body[:60])
        # Per-app freshness
        for app in ("sonarr", "radarr", "prowlarr", "qbittorrent", "plex"):
            q = urllib.parse.urlencode({"query": f"app:{app} | stats count() as n", "start": "30m"})
            code, body = _get(f"http://127.0.0.1:{vp}/select/logsql/query?{q}")
            try:
                lines = body.strip().splitlines()
                d = json.loads(lines[0]) if lines else {}
                row(f"vlogs app:{app} 30m", code, f"lines={d.get('n', '?')}")
            except Exception:
                row(f"vlogs app:{app} 30m", code, "parse-fail")

    print()
    print("audit complete.")


if __name__ == "__main__":
    main()
