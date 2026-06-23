#!/usr/bin/env bash
# Phase: SABnzbd + Usenet path for the main Sonarr (NZBgeek indexer, Frugal provider).
#
# Why: the stack was public-torrent-only (qBittorrent + 22 public trackers). Old
# back-catalog (e.g. Vanderpump Rules S1, a 2013 Bravo show) only exists on those
# trackers as dead-swarm SD with no seeders — unsourceable. Usenet retains old
# content reliably. Built 2026-06-22. See docs/secrets-convention.md and the
# `usenet-buildout` memory for the full story.
#
# As-built reference: re-runnable (each step is check-then-add / idempotent), but
# the SABnzbd install + port capture is best-effort against Ultra.cc's app CLI.
#
# Prereqs (operator-supplied, gitignored secrets):
#   usenet.host/.port/.user/.pass[/.ssl/.connections]  — provider (block account)
#   nzbgeek.key                                         — indexer API key
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

for s in usenet.host usenet.port usenet.user usenet.pass nzbgeek.key sonarr.key prowlarr.key; do
  secret_exists "$s" || die "missing secret: $s"
done

# --- 1. Install SABnzbd if absent (reuses the shared htpasswd password) -------
if sshm 'test -d ~/.apps/sabnzbd' ; then
  log_info "SABnzbd already installed."
else
  log_info "Installing SABnzbd via Ultra.cc app CLI..."
  # app-sabnzbd install emits JSON: {"data":{"port":17007,"version":...},...}
  sshm 'P=$(cat ~/secrets/htpasswd.password); app-sabnzbd install -p "$P"' | tail -3
fi

# --- 2. Capture SABnzbd api key + loopback port into secrets ------------------
# NOTE: sabnzbd.ini shows the *internal* bind (8080 = behind a local Squid); the
# real loopback API port the *arr use is the Ultra.cc-assigned one (17007 here),
# reachable at http://127.0.0.1:<port>/sabnzbd/api and, from the Docker-ised
# *arr containers, at http://172.17.0.1:<port>/sabnzbd/api (bridge gateway).
SAB_KEY="$(sshm "grep -E '^api_key' ~/.apps/sabnzbd/sabnzbd.ini | head -1 | awk '{print \$3}'")"
# Port: prefer an already-recorded secret; else discover the listening port.
if secret_exists sabnzbd.port ; then
  SAB_PORT="$(secret_read sabnzbd.port)"
else
  SAB_PORT="$(sshm "ss -tlnp 2>/dev/null | grep -oE ':1700[0-9]' | tr -d ':' | sort -u | head -1")"
  SAB_PORT="${SAB_PORT:-17007}"
fi
[ -n "$SAB_KEY" ] || die "could not read SABnzbd api_key from sabnzbd.ini"
secret_write sabnzbd.key  "$SAB_KEY"
secret_write sabnzbd.port "$SAB_PORT"
sshm "printf '%s\n' '$SAB_KEY' > ~/secrets/sabnzbd.key; printf '%s\n' '$SAB_PORT' > ~/secrets/sabnzbd.port; chmod 600 ~/secrets/sabnzbd.key"
log_info "SABnzbd: key captured, loopback port $SAB_PORT"

# --- 3. Configure everything via the box-loopback APIs (idempotent) -----------
# Runs ON the box so it can reach 127.0.0.1 services + read ~/secrets directly.
sshm 'python3 -' <<'PY'
import json, urllib.request, urllib.parse, urllib.error, pathlib
sec = pathlib.Path.home()/"secrets"
rd  = lambda n: (sec/n).read_text().strip()

# ---- SABnzbd: Frugal server + sonarr category + docker-bridge whitelist ----
SK, SP = rd("sabnzbd.key"), rd("sabnzbd.port")
sab = f"http://127.0.0.1:{SP}/sabnzbd/api"
def sabcall(p): return json.load(urllib.request.urlopen(sab+"?"+urllib.parse.urlencode({**p,"apikey":SK,"output":"json"}), timeout=40))

servers = sabcall({"mode":"get_config","section":"servers"})["config"]["servers"]
if not any(s.get("host")==rd("usenet.host") for s in servers):
    sabcall({"mode":"set_config","section":"servers","keyword":"Frugal","displayname":"Frugal",
             "host":rd("usenet.host"),"port":rd("usenet.port"),"username":rd("usenet.user"),
             "password":rd("usenet.pass"),"connections":(sec/"usenet.connections").read_text().strip() if (sec/"usenet.connections").exists() else "20",
             "ssl":"1","enable":"1","priority":"0","timeout":"60"})
    print("SABnzbd: added Frugal server")
else:
    print("SABnzbd: Frugal server already present")

cats = sabcall({"mode":"get_config","section":"categories"})["config"]["categories"]
if not any(c.get("name")=="sonarr" for c in cats):
    sabcall({"mode":"set_config","section":"categories","keyword":"sonarr","dir":"sonarr","priority":"-100","pp":"3","script":"Default"})
    print("SABnzbd: added sonarr category")

wl = sabcall({"mode":"get_config","section":"misc","keyword":"host_whitelist"})["config"]["misc"]["host_whitelist"]
wl = [x.strip() for x in wl.split(",") if x.strip()] if isinstance(wl,str) else list(wl)
changed=False
for h in ("172.17.0.1","127.0.0.1","localhost"):
    if h not in wl: wl.append(h); changed=True
if changed:
    sabcall({"mode":"set_config","section":"misc","keyword":"host_whitelist","value":", ".join(wl)})
    print("SABnzbd: extended host_whitelist for docker bridge")

# ---- Sonarr: download client (via 172.17.0.1 bridge) + NZBgeek + delay profile ----
SONK, SONP, SONB = rd("sonarr.key"), rd("sonarr.port"), rd("sonarr.urlbase")
base=f"http://127.0.0.1:{SONP}/{SONB}/api/v3"
def son(path, method="GET", body=None):
    data=json.dumps(body).encode() if body is not None else None
    h={"X-Api-Key":SONK}
    if data: h["Content-Type"]="application/json"
    with urllib.request.urlopen(urllib.request.Request(base+path, data=data, method=method, headers=h), timeout=60) as r:
        raw=r.read().decode(); return (json.loads(raw) if raw else None)

if not any(d["implementation"]=="Sabnzbd" for d in son("/downloadclient")):
    sch=[s for s in son("/downloadclient/schema") if s["implementation"]=="Sabnzbd"][0]
    setv={"host":"172.17.0.1","port":int(SP),"urlBase":"sabnzbd","apiKey":SK,
          "tvCategory":"sonarr","useSsl":False,"recentTvPriority":-100,"olderTvPriority":-100}
    for f in sch["fields"]:
        if f["name"] in setv: f["value"]=setv[f["name"]]
    sch["name"]="SABnzbd"; sch["enable"]=True
    son("/downloadclient","POST",sch); print("Sonarr: added SABnzbd download client")
else:
    print("Sonarr: SABnzbd client already present")

# NZBgeek indexer. NB: Prowlarr's app-sync skips Usenet indexers that return no
# results to its empty-term category probe ("No Results in configured categories"),
# so we add NZBgeek DIRECTLY to Sonarr as Newznab — the path that actually works.
if not any(i["name"]=="NZBgeek" for i in son("/indexer")):
    tmpl=json.loads(json.dumps([s for s in son("/indexer/schema") if s["implementation"]=="Newznab"][0]))
    setv={"baseUrl":rd("nzbgeek.url") if (sec/"nzbgeek.url").exists() else "https://api.nzbgeek.info",
          "apiPath":"/api","apiKey":rd("nzbgeek.key"),
          "categories":[5000,5010,5020,5030,5040,5045,5050,5090]}
    for f in tmpl["fields"]:
        if f["name"] in setv: f["value"]=setv[f["name"]]
    tmpl["name"]="NZBgeek"; tmpl["priority"]=25
    tmpl["enableRss"]=tmpl["enableAutomaticSearch"]=tmpl["enableInteractiveSearch"]=True
    for kk in ("id","infoLink","presets"): tmpl.pop(kk, None)
    son("/indexer","POST",tmpl); print("Sonarr: added NZBgeek (Newznab) indexer")
else:
    print("Sonarr: NZBgeek already present")

# Delay profile: a fresh Sonarr ships enableUsenet=False / preferredProtocol=torrent,
# which makes automatic + failed-redownload grabs pick (dead) torrents over reliable
# NZBs. Enable Usenet and prefer it library-wide.
for d in son("/delayprofile"):
    if not d.get("enableUsenet"):
        d["enableUsenet"]=True; d["preferredProtocol"]="usenet"; d["usenetDelay"]=0
        son(f"/delayprofile/{d['id']}","PUT",d)
        print(f"Sonarr: delay profile {d['id']} -> enableUsenet=True, prefer usenet")
PY

log_info "Done. Adding NZBgeek to Prowlarr (for management/movies) is optional — the"
log_info "Prowlarr->Sonarr Usenet sync is unreliable, so Sonarr holds NZBgeek directly."
