# Internal app tunnels

For apps marked INTERNAL in the public/internal audit (2026-05-10), admin
access is via SSH tunnel — not the public FQDN. Public routes are protected
only by each app's own login (see "Public route auth model" below), so an app
with no working auth must have no route at all. The tunnel is also the
canonical, no-CSS-quirks experience: the SPA frontend sees itself at `/` and
all routes work without subpath rewriting (this is why we couldn't host Listmonk
admin publicly under `/listmonk/`; same logic applies to most modern admin UIs).

The recommended pattern is the operator's permanent tunnel daemon at
`~/Documents/GIT/Ultra.cc/QFlix/scripts/manitoba-tunnel.ps1` — it forwards every
INTERNAL admin port at startup. For ad-hoc single-app tunnels, use the
individual commands below.

## Public vs internal split (canonical, post-2026-05-10)

| Surface | Public FQDN | Internal admin (tunnel) |
|---------|-------------|-------------------------|
| Plex | `https://<fqdn>/web/` (Plex's own SSO) | direct, no admin tunnel |
| Seerr (user requests) | `https://<fqdn>/seerr/` (path) or `https://seerr-<user>.<fqdn>/` (subdomain) | n/a — users self-auth |
| qflix-dash (public board) | `https://<fqdn>/` (root redirect) | `https://<fqdn>/board/private` (htpasswd) |
| Tautulli (read-only stats) | `https://<fqdn>/tautulli/` | n/a |
| Listmonk (campaign archive) | `https://<fqdn>/listmonk/campaign/<uuid>` | tunnel: `localhost:42014` |
| Kuma (public status page) | `https://<fqdn>/status/manitoba` | `localhost:42005` |

**Seerr note:** the kickoff lists Seerr as PUBLIC. Post-2026-05-11 install,
Seerr runs as a Docker container (`seerr-quadstronaut`) on port 42011 with
the nginx fragment `~/.apps/nginx/proxy.d/seerr.conf` proxying both the
path `/seerr/` and the canonical per-app subdomain
`https://seerr-quadstronaut.<fqdn>/`. Users authenticate via Plex SSO.

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
| Bazarr 2 (anime; internal-only — no nginx proxy) | 17032 | `ssh -L 17032:127.0.0.1:17032 quadstronaut@<fqdn>` — bare-Python install under `~/.apps/bazarr2/` |
| qBittorrent | 17041 | `ssh -L 17041:127.0.0.1:17041 quadstronaut@<fqdn>` |
| SABnzbd (Usenet DL client) | 17007 | `ssh -L 17007:127.0.0.1:17007 quadstronaut@<fqdn>` — then `http://localhost:17007/sabnzbd/`. The *arr reach it at `172.17.0.1:17007` (Docker bridge), not loopback. |
| Tdarr (tunnel-only, no public route; see caveat below) | 42018 | `ssh -L 42018:127.0.0.1:42018 quadstronaut@<fqdn>` |
| Uptime Kuma admin | 42005 | `ssh -L 42005:127.0.0.1:42005 quadstronaut@<fqdn>` |
| Tautulli (also public) | 17014 | `ssh -L 17014:127.0.0.1:17014 quadstronaut@<fqdn>` |
| Buildarr (new) | — | n/a — cron-class, no UI; check `~/.apps/buildarr/logs/buildarr.log` |

Apps without an admin UI:
- ~~**FlareSolverr** — Cloudflare-bypass headless proxy at `172.17.0.1:17011`; no UI, just an API consumed by Prowlarr.~~ Retired 2026-10-11 (QFLX-26).
- **Recyclarr / Kometa / Buildarr / qflix-newsletter** — cron-class, run on timers; observe via `journalctl --user -u <name>.service` or `~/.apps/<name>/logs/`.
- **python-plexapi** — library, no service.

## qBittorrent caveat

qBittorrent is exposed at `https://<fqdn>/qbittorrent/` for the operator's
casual remote-control use — it's protected by qBit's OWN auth
(user/password); no htpasswd layer sits in front (see below). The tunnel form
(`localhost:17041`) is the loopback path that the *arr stack uses internally for API calls.

## Tdarr caveat

Tdarr 2.17.01 has no working auth, so the tunnel is the only way in and the
listener is the only access control. The server is patched to bind `127.0.0.1`
(`scripts/configure/50a-tdarr-server-loopback.py`); there is no nginx route.

- **Never add a `/tdarr/` route.** It would publish an unauthenticated admin API.
- **After a re-unzip or upgrade,** re-run `50-tdarr-install.sh`. The vendor
  files come back with a wildcard bind, a redirect that loops on any `Host`
  carrying the port (a tunnel always sends one), and no `/webui` socket on the
  one port. A `manitoba-maint upgrade`/`downgrade` re-applies 50a itself (the
  tdarr-server `zip_swap` post step) before it restarts the unit. Smoke
  `tdarr-loopback-bound` and `tdarr-webui-namespace` catch a miss.
- **The UI's live panels** (Nodes, OS Mem, Libraries, Flows) read from the
  `/webui` socket.io namespace. Stock 2.17.01 registers it on a second listener
  for `webUIPort`; with `webUIPort == serverPort` that listener dies on a
  swallowed `EADDRINUSE` at every start, so the browser got `Invalid namespace`
  and the panels stayed empty while REST answered. 50a's fourth edit puts
  `/webui` on the one listener. Live 2026-11-03: the handshake answers
  `40/webui`, and a browser over the tunnel shows the node, OS Mem, all five
  libraries and the flow. The first load reads ~9.8k DB records over the
  socket, which took about two minutes on this box.

## Maintainerr caveat (retired)

Maintainerr was **uninstalled 2026-06-25** (its subdomain now 502s) and is
no longer running, tunneled, or UCC-managed on port 42007. Retention
duties passed to `scripts/maint/qflix-reaper.py`, a daily-timer script with
no admin UI — nothing to tunnel to. The row and `app-maintainerr start`
instruction that used to live here are gone; don't follow them.

## How the permanent tunnel daemon picks ports

`manitoba-tunnel.ps1` mirrors local-port to server-port (e.g. `17026:17026`).
That way bookmarks read naturally: `http://localhost:17026/sonarr/` always
points to the right thing whether you're on the workstation tunnel or
SSH-forwarding ad-hoc. Adding a new INTERNAL app to the daemon is a
one-line edit to the `$Forwards` array.

## Public route auth model (background)

Ultra.cc's root-managed outer nginx terminates HTTPS and forwards to the
user-level nginx, whose fragments live in `~/.apps/nginx/proxy.d/<app>.conf`.
**Neither layer applies `htpasswd` to those routes.** This page used to say the
outer nginx did and that `auth_basic off` opted a path out. Measured 2026-11-02
(QFLX-96), that is false: `auth_basic` exists only on `/webdav` and PHP, the
`auth_basic off` lines change nothing, and anonymous requests reach the app.
`/sonarr/` redirects to Sonarr's own login page, not a 401 from nginx, and
`/tdarr/` served the whole admin UI until its route was retired.

So a route is exactly as private as the app behind it. Before adding one, check
the app's API refuses a clean request. Tunnels skip nginx entirely and hit the
user-level loopback port directly.
