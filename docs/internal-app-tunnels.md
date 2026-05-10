# Internal app tunnels

For apps marked INTERNAL in the public/internal audit (2026-05-10), admin
access is via SSH tunnel — not the public FQDN. The outer Ultra.cc nginx
gates them with `htpasswd`, but the canonical, no-CSS-quirks experience is
the tunnel: the SPA frontend sees itself at `/` and all routes work
without subpath rewriting (this is why we couldn't host Listmonk admin
publicly under `/listmonk/`; same logic applies to most modern admin UIs).

The recommended pattern is the operator's permanent tunnel daemon at
`~/Documents/GIT/QFlix/scripts/manitoba-tunnel.ps1` — it forwards every
INTERNAL admin port at startup. For ad-hoc single-app tunnels, use the
individual commands below.

## Public vs internal split (canonical, post-2026-05-10)

| Surface | Public FQDN | Internal admin (tunnel) |
|---------|-------------|-------------------------|
| Plex | `https://<fqdn>/web/` (Plex's own SSO) | direct, no admin tunnel |
| Jellyseerr (user requests) | `https://<fqdn>/jellyseerr/` ⚠ see note below | n/a — users self-auth |
| Homarr (public board) | `https://<fqdn>/` (root redirect) | `https://<fqdn>/board/private` (htpasswd) |
| Tautulli (read-only stats) | `https://<fqdn>/tautulli/` | n/a |
| Listmonk (campaign archive) | `https://<fqdn>/listmonk/campaign/<uuid>` | tunnel: `localhost:42014` |
| Kuma (public status page) | `https://<fqdn>/status/manitoba` | `localhost:42005` |

**⚠ Jellyseerr note:** the kickoff lists Jellyseerr as PUBLIC, but its
nginx fragment (`~/.apps/nginx/proxy.d/jellyseerr.conf`) does NOT set
`auth_basic off`, so the outer nginx's htpasswd is still in effect. If
the operator wants user-friends to reach `https://<fqdn>/jellyseerr/`
without sharing the htpasswd password, add `auth_basic off;` to the
location block. Currently undecided — may be intentional double-auth.

## Internal admin tunnels (one-off SSH commands)

Format: `ssh -L <local>:127.0.0.1:<server-port> quadstronaut@<seedbox FQDN>`

Then open `http://localhost:<local>/` in a browser.

| App | Server port | Tunnel command |
|-----|-------------|----------------|
| Listmonk admin | 42014 | `ssh -L 42014:127.0.0.1:42014 quadstronaut@<fqdn>` |
| Sonarr | 17026 | `ssh -L 17026:127.0.0.1:17026 quadstronaut@<fqdn>` |
| Sonarr (anime) | 17003 | `ssh -L 17003:127.0.0.1:17003 quadstronaut@<fqdn>` |
| Radarr | 17027 | `ssh -L 17027:127.0.0.1:17027 quadstronaut@<fqdn>` |
| Radarr (anime) | 17008 | `ssh -L 17008:127.0.0.1:17008 quadstronaut@<fqdn>` |
| Prowlarr | 17024 | `ssh -L 17024:127.0.0.1:17024 quadstronaut@<fqdn>` |
| Bazarr | 17031 | `ssh -L 17031:127.0.0.1:17031 quadstronaut@<fqdn>` |
| qBittorrent | 17041 | `ssh -L 17041:127.0.0.1:17041 quadstronaut@<fqdn>` |
| Maintainerr | 42007 | `ssh -L 42007:127.0.0.1:42007 quadstronaut@<fqdn>` (start service first: `app-maintainerr start`) |
| Tdarr | 42018 | `ssh -L 42018:127.0.0.1:42018 quadstronaut@<fqdn>` |
| Uptime Kuma admin | 42005 | `ssh -L 42005:127.0.0.1:42005 quadstronaut@<fqdn>` |
| Readarr (parked) | 17042 | `ssh -L 17042:127.0.0.1:17042 quadstronaut@<fqdn>` |
| Mylar3 (parked) | 17045 | `ssh -L 17045:127.0.0.1:17045 quadstronaut@<fqdn>` |
| Ombi (parked) | 17046 | `ssh -L 17046:127.0.0.1:17046 quadstronaut@<fqdn>` |
| Tautulli (also public) | 17014 | `ssh -L 17014:127.0.0.1:17014 quadstronaut@<fqdn>` |
| Profilarr (deferred install) | 42015 | n/a — install blocked by seedbox Vite OOM |
| Janitorr (deferred install) | — | n/a — no upstream jar |
| Suggestarr (deferred install) | — | n/a — same Vite OOM |
| Buildarr (new) | — | n/a — cron-class, no UI; check `~/.apps/buildarr/logs/buildarr.log` |
| Watcharr (parked) | — | n/a — no subpath, parked permanently |

Apps without an admin UI:
- **FlareSolverr** — Cloudflare-bypass headless proxy at `172.17.0.1:17011`; no UI, just an API consumed by Prowlarr.
- **Recyclarr / Kometa / Buildarr / qflix-newsletter** — cron-class, run on timers; observe via `journalctl --user -u <name>.service` or `~/.apps/<name>/logs/`.
- **python-plexapi** — library, no service.

## qBittorrent caveat

qBittorrent is exposed at `https://<fqdn>/qbittorrent/` for the operator's
casual remote-control use — it's behind htpasswd but its OWN auth is also
required (qBit user/password). The tunnel form (`localhost:17041`) is the
no-htpasswd path that the *arr stack uses internally for API calls.

## Maintainerr caveat

Maintainerr is currently **stopped** (`app-maintainerr stop`) per
operator policy — running it advertises a UCC subdomain. Restart with
`app-maintainerr start` before tunneling.

## How the permanent tunnel daemon picks ports

`manitoba-tunnel.ps1` mirrors local-port to server-port (e.g. `17026:17026`).
That way bookmarks read naturally: `http://localhost:17026/sonarr/` always
points to the right thing whether you're on the workstation tunnel or
SSH-forwarding ad-hoc. Adding a new INTERNAL app to the daemon is a
one-line edit to the `$Forwards` array.

## Outer-nginx auth model (background)

Ultra.cc's root-managed outer nginx terminates HTTPS and applies
`htpasswd` to the public FQDN. User-level proxy fragments (in
`~/.apps/nginx/proxy.d/<app>.conf`) can override with `auth_basic off`
to make a path public. Most INTERNAL apps inherit the outer htpasswd by
not specifying `auth_basic` at all; PUBLIC apps explicitly opt out via
`auth_basic off`. Tunnels skip the outer nginx entirely and hit the
user-level loopback port directly.
