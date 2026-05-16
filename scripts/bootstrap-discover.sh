#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

log_info "Capturing existing API keys + ports from manitoba..."

# Ports come from ~/.apps/nginx/proxy.d/<app>.conf (the running nginx upstream).
# config.xml's <Port> is the in-container default (8989/7878/9696); not the host port.
# Pattern: `proxy_pass http://127.0.0.1:NNNNN[/<urlbase>]`. We grab the first match.
discover_port_via_nginx() {
  local app="$1"
  sshm "grep -hoE 'proxy_pass[[:space:]]+http://127\\.0\\.0\\.1:[0-9]+' ~/.apps/nginx/proxy.d/$app.conf 2>/dev/null | head -1 | grep -oE '[0-9]+$'" || true
}

# Sonarr / Radarr / Prowlarr / Sonarr2 / Radarr2 — config.xml gives ApiKey + UrlBase, nginx gives port
# (Readarr removed 2026-05-15 — purged 2026-05-11.)
for app in sonarr radarr prowlarr sonarr2 radarr2; do
  cfg="$(sshm "cat ~/.apps/$app/config.xml 2>/dev/null" || true)"
  if [ -z "$cfg" ]; then
    log_warn "$app: no config.xml (not installed)"
    continue
  fi
  key="$(printf '%s' "$cfg" | grep -oP '(?<=<ApiKey>)[^<]+')"
  base="$(printf '%s' "$cfg" | grep -oP '(?<=<UrlBase>)[^<]+' || true)"
  port="$(discover_port_via_nginx "$app")"
  [ -n "$key" ]  && secret_write "$app.key" "$key"
  [ -n "$base" ] && secret_write "$app.urlbase" "${base#/}"
  if [ -n "$port" ]; then
    secret_write "$app.port" "$port"
    log_info "  $app: key=${key:0:8}... port=$port base=${base:-(none)}"
  else
    log_warn "  $app: key captured but no nginx proxy → service may be installed but not exposed"
  fi
done

# Bazarr (config.yaml) — port via nginx
bazarr_port="$(discover_port_via_nginx bazarr)"
bazarr_cfg_path="$(sshm 'find ~/.apps/bazarr -name "config.yaml" 2>/dev/null | head -1' || true)"
if [ -n "$bazarr_cfg_path" ]; then
  bazarr_key="$(sshm "awk '/^[[:space:]]+apikey:/{print \$2; exit}' $bazarr_cfg_path 2>/dev/null" || true)"
  [ -n "$bazarr_key" ] && secret_write "bazarr.key" "$bazarr_key"
fi
[ -n "$bazarr_port" ] && secret_write "bazarr.port" "$bazarr_port"
secret_write "bazarr.urlbase" "bazarr"
log_info "  bazarr: port=${bazarr_port:-?} key=${bazarr_key:+captured}"

# Bazarr 2 (bare-Python install under ~/.apps/bazarr2/) — fixed port 17032,
# no nginx proxy (loopback-only). config.yaml lives one level deeper than
# bazarr-1's since --config is a data root, not a config file.
bazarr2_cfg_path="$(sshm 'test -f ~/.apps/bazarr2/data/config/config.yaml && echo ~/.apps/bazarr2/data/config/config.yaml' || true)"
if [ -n "$bazarr2_cfg_path" ]; then
  bazarr2_key="$(sshm "awk '/^auth:/,/^[a-z]/{if(\$1==\"apikey:\"){print \$2; exit}}' $bazarr2_cfg_path 2>/dev/null" || true)"
  [ -n "$bazarr2_key" ] && secret_write "bazarr2.key" "$bazarr2_key"
  secret_write "bazarr2.port" "17032"
  secret_write "bazarr2.urlbase" "bazarr2"
  log_info "  bazarr2: port=17032 key=${bazarr2_key:+captured}"
else
  log_info "  bazarr2: not installed (run scripts/install/06-bazarr2.sh)"
fi

# Tautulli (config.ini)
tautulli_key="$(sshm 'awk -F"=" "/^api_key/{gsub(/[[:space:]]/,\"\",\$2); print \$2; exit}" ~/.apps/tautulli/config.ini 2>/dev/null' || true)"
[ -n "$tautulli_key" ] && secret_write "tautulli.key" "$tautulli_key"
tautulli_port="$(discover_port_via_nginx tautulli)"
[ -n "$tautulli_port" ] && secret_write "tautulli.port" "$tautulli_port"

# Ombi capture removed 2026-05-15 (purged 2026-05-11). Seerr is the
# canonical requester now — captured below.

# Seerr / Maintainerr / etc — may not be installed; capture port if nginx has it
for app in seerr maintainerr flaresolverr unpackerr uptimekuma komga kavita calibre-web audiobookshelf homarr-upstream homarr; do
  port="$(discover_port_via_nginx "$app")"
  [ -n "$port" ] && secret_write "$app.port" "$port"
done

# qBittorrent — already known: bound on bond0.27 at 17041
secret_write "qbittorrent.port" "17041"
secret_write "qbittorrent.user" "quadstronaut"
log_warn "qbittorrent.password — capture manually from Ultra.cc panel into secrets/qbittorrent.password"

# Plex token — Plex runs in a Docker container at 172.17.x.x (NOT user-systemd); Preferences.xml not local.
# Operator must capture this from the Plex web UI: https://app.plex.tv → settings → account → X-Plex-Token
log_info "Plex token: capture manually from https://app.plex.tv (X-Plex-Token in URL after sign-in) into secrets/plex.token"

# Maintainerr API — generated in DB at first-run; capture from UI Settings → API
log_info "Maintainerr key: capture manually from UI (Settings > API) into secrets/maintainerr.key"

# Seerr API — config is inside the Docker container `seerr-quadstronaut`.
# Operator must capture from Seerr UI → Settings → General → API Key into secrets/seerr.key.
log_info "Seerr key: capture manually from UI (Settings > General > API Key) into secrets/seerr.key"

# Notifiarr — already in secrets/notifiarr.key

# Live-API check: confirm captured ports actually respond to API calls (loopback)
log_info ""
log_info "Live-API verification (only for apps with both port + key):"
for app in sonarr radarr prowlarr sonarr2 radarr2 bazarr bazarr2; do
  if secret_exists "$app.port" && secret_exists "$app.key"; then
    port="$(secret_read $app.port)"
    key="$(secret_read $app.key)"
    base="$(secret_read $app.urlbase 2>/dev/null || echo $app)"
    # API versions: bazarr/bazarr2=plain, prowlarr=v1, sonarr/radarr=v3
    if [ "$app" = "bazarr" ] || [ "$app" = "bazarr2" ]; then
      url="http://127.0.0.1:$port/$base/api/system/status"
      hdr="X-API-KEY"
    elif [ "$app" = "prowlarr" ]; then
      url="http://127.0.0.1:$port/$base/api/v1/system/status"
      hdr="X-Api-Key"
    else
      url="http://127.0.0.1:$port/$base/api/v3/system/status"
      hdr="X-Api-Key"
    fi
    code="$(sshm "curl -s -m 5 -o /dev/null -w '%{http_code}' -H '$hdr: $key' '$url'" || echo "000")"
    if [ "$code" = "200" ]; then
      log_info "  ✓ $app live ($url → 200)"
    else
      log_warn "  ✗ $app api → $code at $url"
    fi
  fi
done

log_info ""
log_info "Bootstrap discovery complete. Inventory:"
ls -la "$SECRETS_DIR"
