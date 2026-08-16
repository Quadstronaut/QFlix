# Secrets

This directory is **gitignored**. Nothing here is tracked.

Each file holds one secret value, one per line. Scripts read these via
path, never via env var leakage. The maint daemon's secrets-dir resolution
lives in `scripts/maint/lib/secrets.py` (`MANITOBA_SECRETS_DIR` env var
takes precedence; the legacy `MANITOBA_SECRETS` name is still honored for
back-compat).

## Public identity

| File | Purpose |
|---|---|
| `seedbox.host` | Operator's public HTTPS FQDN (e.g. `quadstronaut.<seedbox-domain>`). Used by every script that builds a public URL — `secret_read seedbox.host` dies loudly if missing rather than baking the sanitized placeholder into config. |
| `seedbox.ssh-host` | SSH FQDN. On Ultra.cc shared seedboxes the SSH FQDN differs from the HTTPS FQDN; on single-domain setups, omit and lib/ssh.sh falls back to seedbox.host. |
| `discord-webhook.url` | Discord notification webhook (single channel). |
| `discord-operator.id` | Operator's Discord user-id. notify.py adds `<@id>` on error/critical levels so the operator gets a push, not just an embed. |

## App credentials (active)

| File | Purpose | Source |
|---|---|---|
| `htpasswd.password` | Shared admin password (outer Ultra.cc htpasswd + Listmonk rotated to match) | Ultra.cc panel (outer htpasswd), operator-captured; consumed by 91-nginx-root-to-dash.sh + 43-listmonk-install.sh |
| `prowlarr.key` / `.port` / `.urlbase` | Prowlarr API | `~/.apps/prowlarr/config.xml` ApiKey + bootstrap-discover.sh |
| `sonarr.key` / `sonarr2.key` / `radarr.key` / `radarr2.key` (+ `.port` + `.urlbase` each) | *arr API + listener | bootstrap-discover.sh scrapes config.xml + nginx upstream port |
| `bazarr.key` / `bazarr2.key` (+ `.port`, `.urlbase`) | Subtitles API | UI / 06-bazarr2.sh |
| `qbittorrent.user` / `.password` / `.port` | qBit WebUI | Ultra.cc panel + bootstrap |
| `usenet.host` / `.port` / `.user` / `.pass` / `.ssl` / `.connections` | Usenet provider (Frugal block account) — SABnzbd downloads from this | operator signup; consumed by 90-sabnzbd-usenet-install.sh |
| `sabnzbd.key` / `.port` | SABnzbd API + loopback port (17007; *arr reach it at `172.17.0.1:17007` over the Docker bridge) | 90-sabnzbd-usenet-install.sh scrapes sabnzbd.ini |
| `nzbgeek.key` / `.url` | NZBgeek Newznab indexer (Usenet search), added directly to Sonarr | operator signup (https://nzbgeek.info); url `https://api.nzbgeek.info` |
| `seerr.key` / `.port` / `.urlbase` | Seerr API | UI → Settings → General |
| `plex.token` / `.host` / `.port` | Plex API | https://www.plex.tv/claim or X-Plex-Token from a web session |
| `plex.direct_host` (optional) | Direct-access Plex hostname for the dashboard's Plex tile (Ultra.cc shared boxes route a dedicated direct-IP endpoint). Falls back to `<seedbox.host>/web/` if absent. |
| `tautulli.key` / `.port` | Tautulli API | UI → Settings → Web Interface |
| `tmdb.read_token` | TMDB API v4 read token | https://themoviedb.org → API |
| `github.repo` (optional) | `owner/name` of the public repo whose weekly commits drive the newsletter's "Behind the scenes" recap. Defaults to `Quadstronaut/QFlix` if absent. | n/a (public repo; no token) |
| `listmonk.api_user` / `.api_token` / `.port` / `.list_id` / `.from_email` / `.smtp_user` / `.smtp_password` | Listmonk admin + SMTP creds | 43-listmonk-install.sh provisions the API user; SMTP/from_email come from the operator |
| `postgres.port` | Listmonk's Postgres backend | bootstrap |
| `ucc.probe_app` | App name the UCC maintenance-gate detector probes with `app-<name> start` every 5 min (`scripts/maint/lib/ucc.py`). Currently `sonarr`; was an implicit `kavita` default until the books stack was decommissioned 2026-08-16. Must always name an INSTALLED app — an uninstalled one makes the gate detector permanently `probe-error`. | operator-written; falls back to `_DEFAULT_PROBE_APP` when absent |
| ~~`komga.*` / `kavita.*` / `audiobookshelf.*` / `calibre-web.*`~~ | **RETIRED 2026-08-16** — books stack decommissioned; purged from `~/secrets/` and `~/.opt/secrets/` | n/a |
| `tdarr.server_port` / `.api_key` | Tdarr Server | 50-tdarr-install.sh |
| `uptimekuma.key` / `.port` (+ `.host` for off-box) | Kuma API (metrics scrape + push-token bootstrap) | UI → Settings → API |
| `kuma-push-tokens.json` | Per-monitor push tokens. **Two consumers, two copies:** (a) the seedbox copy at `~/secrets/kuma-push-tokens.json` is read by `manitoba-maint-pusher.service` and the canary push pipeline; (b) the local-repo copy at `secrets/kuma-push-tokens.json` is read by `scripts/local/qflix-collect.ps1` (Windows scheduled task) for the workstation-side push. Both are written by `scripts/maint/bootstrap-kuma-monitors.py` when run from that host. Keys for apps and canaries are slug-style (`sonarr`, `canary-quota`). Keys for entries in `manifest.kuma_external_monitors` that are PUSH-typed (e.g. `QFlix Collect (workstation)`) use the **Kuma display name verbatim** — that's what `Push-Kuma` in `qflix-collect.ps1` looks up. The bootstrap seeds from any existing token file before writing, so operator-placed keys survive across runs. |
| `vlogs.port` | VictoriaLogs loopback port | 80-vlogs-install.sh |
| `flaresolverr.port` | FlareSolverr Docker-bridge port | bootstrap |
| `maintenance.port` | manitoba-maint webhook port | 240-maintenance-install.sh |
| `github.pat` | GitHub fine-grained PAT with **ZERO permissions**, public-repo read only, no expiry. Purely a rate-limit credential: GitHub's anonymous API is 60 req/h **per IP**, and on a shared Ultra.cc box that quota is spent by other tenants — measured 2026-08-07, anonymous 60 vs authenticated 5000. Authentication alone lifts it, so no permission is granted and a leaked copy allows nothing anonymous access did not. Read by `scripts/lib/github.sh` (`gh_curl` / `gh_latest_tag`), which **fails open**: if the file is absent the call still goes out unauthenticated, because an optional rate-limit credential must never break an install. Consumers today are the three release-tag lookups in `55-kometa-install.sh`, `56-recyclarr-install.sh`, `59-python-plexapi-venv.sh`; `tests/unit/test_github_auth_wiring.py` prevents a fourth from bypassing the helper. **Not usable for Bazarr** — its updater passes no headers and has no token setting, and `bazarr2-sync.timer` reverts a patched call site hourly. | github.com → Settings → Developer Settings → fine-grained tokens |
| `notifiarr.key` | Notifiarr hosted API key — **only used by `prune-text-libraries.sh` for the daily prune digest**. Not part of the main maintenance notification path (that uses `discord-webhook.url`). | https://notifiarr.com profile → API Keys |

## Purged 2026-05-11 (kept only as `.purged-2026-05-11/<name>`)

Jellyfin, Jellyseerr, Jellystat, Conjurr, Newsletterr, Ombi, Readarr,
Mylar3 — secrets moved to `~/.purged-2026-05-11/` on the seedbox and
removed from the repo. If you need to re-install any of these, restore
the secret from the purge directory.

## Filling these in

Most keys come into existence only after the corresponding app is
installed/configured. Capture them as we go; do not commit them.
`scripts/bootstrap-discover.sh` SSHes into the seedbox, scrapes the
available config files, and populates the local `secrets/` folder.
Read-only on the seedbox; idempotent.
