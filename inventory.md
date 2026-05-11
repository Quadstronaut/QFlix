# Manitoba — Live Artifact Inventory

**Source of truth.** Re-verified live 2026-05-11 against the seedbox
(`quadstronaut@seedbox.example.com`) — systemd units, timers, crontab,
`~/.apps/`, `~/secrets/`, Kuma `/metrics`, Kuma SQLite, nginx `proxy.d/`,
repo manifest. Where the manifest, docs, and live seedbox disagree, the
live seedbox wins and this file records both.

**Counts (live as of 2026-05-11):**
- **28 apps in `manifest/apps.yaml`** (19 UCC, 3 systemd, 5 cron, 1 library); all 28 present on seedbox.
- **35 Kuma monitors** total: 32 manitoba (incl. 4 canary push monitors + 6 added 2026-05-11 for FlareSolverr / Unpackerr / Postgres / Tdarr Node / Kometa / PlexAPI) + 3 quadstronix external. **32/32 manitoba UP.** Every app in the manifest now actively reports to Kuma.
- All 32 manitoba monitors wired to both notification channels (`Mission Control - QFlix` Discord + `Manitoba auto-heal webhook`). No silent-failure drift.
- Notification channel: single Discord webhook + operator @ping (user-id read from `secrets/discord-operator.id`) on `error` / `critical` levels via `scripts/maint/lib/notify.py`.
- Last full smoke: **45 pass / 0 fail / 0 skip** (2026-05-11).

**Q2 coverage gap closed 2026-05-11** — the prior 6 apps that didn't push to Kuma (flaresolverr / unpackerr / postgres / tdarr-node / kometa / python-plexapi) now all have monitors. Required two `health.py` fixes: `os.path.expanduser` on the `venv_python` field (import_check), and a new optional `hostname` override on http_root/http_api (so FlareSolverr's Docker-bridge bind at `172.17.0.1:17011` is probable from host netns).

`Auto-heal?` = `manitoba-maint-pusher` covers the app (any manifest entry with a `kuma_monitor:` value), or a dedicated heartbeat script restarts it. `Notification on fail?` = N Kuma notification slots wired.

---

## A. Manifest — UCC apps (managed by Ultra.cc `app-<slug>`)

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| sonarr | ucc | yes | TV main library *arr | NO | Internal | tunnel `http://localhost:17026/sonarr/` · public `https://quadstronaut.seedbox.example.com/sonarr/` (htpasswd) | yes (Mon 04-08) | yes (pusher) | 2 |  |
| sonarr2 | ucc | yes | TV anime *arr | NO | Internal | tunnel `http://localhost:17003/sonarr2/` · public same-base (htpasswd) | yes | yes (pusher) | 2 |  |
| radarr | ucc | yes | Movies main library *arr | NO | Internal | tunnel `http://localhost:17027/radarr/` · public (htpasswd) | yes | yes (pusher) | 2 |  |
| radarr2 | ucc | yes | Movies anime *arr | NO | Internal | tunnel `http://localhost:17008/radarr2/` · public (htpasswd) | yes | yes (pusher) | 2 |  |
| prowlarr | ucc | yes | Indexer aggregator | NO | Internal | tunnel `http://localhost:17024/prowlarr/` | yes | yes (pusher) | 2 |  |
| bazarr | ucc | yes | Subtitles (main TV + main movies) | NO | Internal | tunnel `http://localhost:17031/bazarr/` | yes | yes (pusher) | 2 |  |
| bazarr2 | systemd-user | yes (bazarr2.service, bare-Python under `~/.apps/bazarr2/`) | Subtitles (anime TV + anime movies) | NO | Internal | tunnel `http://localhost:17032/bazarr2/` (loopback only, no nginx) | yes (bazarr2-sync.timer pins to bazarr-1 version) | yes (pusher) | 2 | Ultra.cc has no `app-bazarr2` slot — bare-Python install on python3.11 venv. Paired with Sonarr2 + Radarr2 via loopback ports (no htpasswd path). `bazarr2-sync.service` (hourly oneshot) keeps the version aligned with bazarr-1 by fetching the matching upstream tag and re-applying the waitress `threads=100→4` patch. Closed deferral: `docs/anime-subs-deferred.md`. |
| qbittorrent | ucc | yes (qbittorrent.service v5.0.3) | Download client | NO | Internal (operator-public via /qbittorrent/) | tunnel `http://localhost:17041/` · public `https://…/qbittorrent/` (htpasswd + qBit auth) | yes | yes (pusher) | 2 | No `~/.apps/qbittorrent/` dir — config at system level; manifest probes via http_root. |
| plex | ucc | yes | Media server (canonical) | NO | Public | `https://quadstronaut.seedbox.example.com/web/` (Plex SSO) | yes | yes (pusher) | 2 |  |
| seerr | ucc | yes (Docker container `seerr-quadstronaut`, v3.2.0) | User request portal + issue tracking | NO | Public — `https://quadstronaut.seedbox.example.com/seerr/` AND canonical `https://seerr-quadstronaut.seedbox.example.com/` | port 42011 (Docker) · loopback `http://127.0.0.1:42011/` | yes (Mon 04-08) | yes (pusher; manifest `kuma_monitor: "Seerr"`, health probes `/api/v1/status` with `seerr.key`) | 2 | Installed 2026-05-11 via `app-seerr install` v3.2.0; Jellyseerr stopped + purged → `~/.purged-2026-05-11/jellyseerr-install/`. API key in `~/secrets/seerr.key`. 4 *arr servers configured via API (Sonarr Cinema default + Sonarr Anime non-default + Radarr Cinema default + Radarr Anime non-default; idempotent script `scripts/configure/30-seerr-arrs.py`). `trustProxy: true` on `/api/v1/settings/network` for the nginx reverse proxy. **Anime auto-routing caveat:** per [docs.seerr.dev](https://docs.seerr.dev/using-seerr/settings/services) only `isDefault`/`is4k` are documented routing axes — no cross-server anime field exists. Users pick "Sonarr Anime" / "Radarr Anime" from the per-request server dropdown in the Seerr UI. Restart confirmed clean 2026-05-11 (`app-seerr restart` → 8s warmup → HTTP 200 both loopback + via nginx). |
| tautulli | ucc | yes | Plex stats (read-only public) | NO | Public | `https://…/tautulli/` (auth_basic off confirmed) · tunnel `localhost:17014` | yes | yes (pusher) | 2 | Second channel wired 2026-05-11. `pms_url` pinned to `http://172.17.1.250:32400` + `pms_url_manual=1` (see `scripts/configure/50-tautulli-pms-url-fix.sh`): Tautulli's Docker container can't resolve `*.plex.direct`, which silently broke `get_metadata` API + WebSocket session enrichment for ~1 month. |
| audiobookshelf | ucc | yes | Audiobook server | NO | Internal | tunnel via port `secrets/audiobookshelf.port` | yes | yes (pusher) | 2 |  |
| kavita | ucc | yes | Manga / comics / ebook reader | NO | Internal | tunnel via port `secrets/kavita.port` | yes | yes (pusher) | 2 |  |
| komga | ucc | yes | Comics server | NO | Internal | tunnel `localhost:<komga.port>/komga/` | yes | yes (pusher) | 2 |  |
| calibre-web | ucc | yes | Ebook catalog | NO | Internal | tunnel via `secrets/calibre-web.port` | yes | yes (pusher) | 2 |  |
| homarr | ucc | yes | Public landing board | NO | Public | `https://quadstronaut.seedbox.example.com/` (root) + `/board/private` htpasswd | yes | yes (pusher) | 2 |  |
| flaresolverr | ucc | yes | Cloudflare-bypass for Prowlarr | NO | Internal | API-only `172.17.0.1:<flaresolverr.port>` | yes | yes (pusher) — no Kuma monitor by design | n/a | `kuma_monitor: null` in manifest. |
| maintainerr | ucc | yes | Library deletion rules (60-day) | NO | Internal | tunnel `http://localhost:42007/` · per-app subdomain `https://maintainerr-quadstronaut.seedbox.example.com/` (htpasswd) | yes | yes (pusher) | 2 | Prior session falsely claimed parked — corrected in commit 30b9e08. |
| unpackerr | ucc (Docker `/app/unpackerr -c …`) | yes | Auto-extract archives post-import for the *arr stack | NO | n/a (no HTTP surface) | n/a | yes (Mon 04-08) | yes (pusher; `process_pattern` probe matches `/app/unpackerr`) | n/a (`kuma_monitor: null` — no probe kind for raw-process supervision yet) | Smoke #8 verifies the process is running. Added to manifest 2026-05-11. |
| postgres | ucc | yes (PID supervised by UCC) | Database backend for Listmonk | NO | Internal | localhost:`secrets/postgres.port` | yes | implicit via Listmonk health (if Postgres dies, Listmonk `/health` goes red) | n/a (`kuma_monitor: null` — covered transitively) | Manifest `process_pattern: postgres: checkpointer`. Added to manifest 2026-05-11. |

## B. Manifest — systemd-class apps

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| listmonk.service | systemd | yes | Newsletter / mailing list manager | NO | Public archive + Internal admin | public `https://…/listmonk/campaign/<uuid>` · admin tunnel `localhost:42014` (canonical probe port) | yes | yes (heartbeat-listmonk cron + pusher) | 2 |  |
| tdarr-server.service | systemd | yes | Transcoding orchestrator | NO | Internal | tunnel `http://localhost:42018/` | yes | yes (heartbeat-tdarr-server cron + pusher) | 2 | Pinned to v2.17.01 (GLIBC). |
| tdarr-node.service | systemd | yes | Transcoding worker | NO | Internal | no UI | yes | yes (heartbeat-tdarr-node cron) | n/a (`kuma_monitor: null`) |  |

## C. Manifest — cron/timer-driven apps

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| recyclarr.timer | cron | scheduled (Sun 04:51) | TRaSH-guide quality profile sync | NO | n/a | no UI | yes | n/a (oneshot) | 2 | Notif wires added 2026-05-11. |
| qflix-newsletter.timer | cron | scheduled (Mon 08:00) | Weekly Plex digest → Listmonk | NO | n/a | no UI | yes | n/a (oneshot) | 2 | Notif wires added 2026-05-11. Replaces Conjurr+Newsletterr. |
| qflix-poster-cache-prune.timer | cron | scheduled (00:00 UTC daily) | Delete poster cache files older than 30 days | NO | n/a | no UI | yes | n/a (oneshot) | n/a (`kuma_monitor: null`) | User-systemd timer. Probed via `lib/health.py systemd_oneshot`. Paired with cache directory `~/www/images/newsletter/`. |
| buildarr.timer | cron | scheduled (Mon 04:30) | Declarative *arr state converger | NO | n/a | no UI; log `~/.apps/buildarr/logs/buildarr.log` | yes | n/a (oneshot) | 2 | Notif wires added 2026-05-11. Patched to manage Sonarr v4 + Radarr v6 — 7 in-venv edits at `scripts/patches/`, idempotently re-applied by `scripts/configure/60-buildarr-patches.sh`. End-to-end working (`Result=success`, all 4 instances clean). Retire when upstream catches up. |
| kometa.timer | cron | scheduled (Mon 03:37) | Plex metadata + collections | NO | n/a | no UI | yes | n/a (oneshot) | n/a (`kuma_monitor: null`) | Result=success last run. |
| upgradinatorr.timer | cron | scheduled (Sun 06:04) | Re-search stale grabs across Sonarr/Sonarr2/Radarr/Radarr2 | NO | n/a | no UI | yes (outside maint window — pre-Monday) | n/a (oneshot) | 2 | Kuma push monitor #65 added 2026-05-11. Manifest class:cron. |

## D. Manifest — library (no service)

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| python-plexapi | library | n/a | venv used by canaries + qflix-newsletter | NO | n/a | n/a | yes (pip-upgrade in window) | n/a | n/a |  |

## E. ~~DRIFT — installed but NOT in manifest~~ → RESOLVED 2026-05-11

All three prior drift entries (unpackerr, upgradinatorr, postgres) added to `manifest/apps.yaml` and re-located to Sections A and C above. Section retained as a marker so the resolution stays visible.

## F. Gateway / infrastructure

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| nginx.service | UCC | yes (v2026.04.28) | User-level reverse proxy (15 `proxy.d/` fragments after 2026-05-11 sweep — down from 45 + 2 disabled; 33 UCC-stock orphans for never-installed apps moved to `~/.purged-2026-05-11/nginx-proxy-d/`) | NO | Public-facing | binds 80/443 of operator's slot | yes | systemd Restart=on-failure | **0** (no Kuma monitor) | Outer Ultra.cc root-nginx terminates TLS + applies htpasswd; this user-nginx maps paths to app ports. Only `listmonk.conf` and `tautulli.conf` set `auth_basic off;`. |
| uptimekuma | UCC | yes | Status monitoring + push receiver | NO | Internal admin + Public status page | admin tunnel `localhost:42005` · public `https://…/status/manitoba` | yes | n/a (self-monitoring) | n/a (it IS the monitor) | DB at `~/.apps/uptimekuma/kuma.db`. 35 monitors total (32 manitoba + 3 quadstronix external). |

## G. Maintenance system (`manitoba-maint-*`)

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| manitoba-maint-pusher.service | systemd | yes | Kuma push-loop (host netns → /api/push) | NO | Internal | loopback | n/a | systemd Restart= | (none — failure surfaces as Kuma monitor going stale) |  |
| manitoba-maint-webhook.service | systemd | yes | Kuma webhook receiver → `~/.opt/maint/state.json` | NO | Internal | `127.0.0.1:<maintenance.port>/health` returns "ok" | n/a | heartbeat-maint-webhook.sh (cron every 5m) | (downstream — receives notifications, doesn't send) |  |
| manitoba-maint-window.timer | systemd | scheduled (Mon 13:00 CEST = 11:00 UTC) | Weekly maintenance window orchestrator | NO | n/a | no UI | n/a (it IS the window) | n/a | n/a |  |
| manitoba-maint-window-watchdog.timer | systemd | scheduled (Mon 17:00) | Clears stale lockfile | NO | n/a | no UI | n/a | n/a | n/a |  |
| manitoba-maint-cp-upgrade.timer + .service | systemd | not scheduled (manual) | cp.ultra.cc Upgrade & Repair sweep | NO | n/a | no UI | yes (when invoked) | n/a | n/a | Manual trigger. |
| `~/bin/manitoba-maint` CLI | UCC | n/a (CLI) | Manifest validation, kuma audit, app lifecycle | NO | n/a | n/a | n/a | n/a | n/a |  |

## H. Canaries

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| manitoba-maint-canary-movie.timer | systemd | scheduled (hourly) | Seerr → Radarr movie path canary | NO | n/a | n/a | n/a | n/a | **1** (Kuma) |  |
| manitoba-maint-canary-anime.timer | systemd | scheduled (hourly) | Seerr → Sonarr2 anime path canary | NO | n/a | n/a | n/a | n/a | **1** |  |
| manitoba-maint-canary-deletion.timer | systemd | scheduled (Mon 04:30) | Maintainerr 60-day deletion-rule audit | NO | n/a | n/a | n/a | n/a | **1** |  |
| manitoba-maint-canary-mobile-ux.timer | systemd | scheduled (every 15m) | Homarr public board reachability | NO | n/a | n/a | n/a | n/a | **1** |  |
| scripts/canaries/{movie,anime,deletion,mobile-ux}.sh | script | invoked by services above | (see services) | NO | n/a | n/a | n/a | n/a | (via Kuma push) |  |

## I. Operator scripts (cron-driven)

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| heartbeat-listmonk.sh | cron */5m | yes | Restart Listmonk if /health fails | NO | n/a | n/a | n/a | (it IS the auto-heal) | (silent on heal; logs only) |  |
| heartbeat-tdarr-server.sh | cron */5m | yes | Restart Tdarr Server on /api/v2/status fail | NO | n/a | n/a | n/a | (auto-heal) | (silent) |  |
| heartbeat-tdarr-node.sh | cron */5m | yes | Restart Tdarr Node if systemd inactive | NO | n/a | n/a | n/a | (auto-heal) | (silent) |  |
| heartbeat-maint-webhook.sh | cron */5m | yes | Restart maint-webhook on /health fail | NO | n/a | n/a | n/a | (auto-heal) | (silent) |  |
| listmonk-sync.py | cron 04:00 daily | scheduled | Plex friends + Seerr → Listmonk subscribers | NO | n/a | n/a | yes | n/a (idempotent) | (silent) | Logs to `~/.apps/listmonk/logs/sync.log`. |
| arr-housekeeping.py --missing | cron 04:00 mostdays | scheduled | Find/grab missing episodes | NO | n/a | n/a | yes | n/a | (silent) | Excludes Mondays (maint window). |
| arr-housekeeping.py --unstick | cron :15 hourly | scheduled | Bump stuck-on-import queue items | NO | n/a | n/a | n/a | n/a | (silent) |  |
| kill_stream.sh --max 2 | cron every-minute | yes | Cap concurrent Plex streams at 2 | NO | n/a | n/a | n/a | n/a | (silent) | Pairs with stream_stats.sh. |
| stream_stats.sh | cron every-minute | yes | Log Plex stream stats → JSON | NO | n/a | `~/.apps/stream-stats/state.json` | n/a | n/a | (silent) | Smoke #13h checks freshness <180s. |
| prune-text-libraries.sh | cron 04:00 daily | scheduled | Prune ebook/manga/audiobook >365d | NO | n/a | n/a | yes | n/a | references Notifiarr in comment (stale); actual action is delete+rescan |  |
| post-import/upgradinatorr.sh | systemd-triggered (oneshot) | invoked weekly | called by upgradinatorr.timer (Section E) | NO | n/a | n/a | yes | n/a | (silent) |  |
| post-import/library-rescan-*.sh | manual | invoked by *arr custom scripts | rescan Komga/Kavita/Calibre/AudioBS/Comics | NO | n/a | n/a | n/a | n/a | n/a |  |
| configure/46-homarr-add-comms.py | manual setup | one-shot completed | Adds Listmonk widget to Homarr | yes (one-shot, kept for replay) | n/a | n/a | n/a | n/a | n/a |  |

## J. ~~Failed / stale systemd units~~ → RESOLVED 2026-05-11

`systemctl --user list-units --state=failed` returns `0 loaded units listed` as of 2026-05-11. autobrr/filebrowser/qbt_pub orphan unit references gone from systemd's view. `logrotate.service` no longer in failed state (timer fires daily, health verified).

## K. ~~Orphan artifacts (Conjurr / Newsletterr partial purge)~~ → RESOLVED 2026-05-11

Verified live: `~/Conjurr/` gone; secrets/{conjurr,newsletterr,jellyfin,jellystat}.* moved to `~/.purged-2026-05-11/`. Kuma monitors #42 (Conjurr) + #43 (Newsletterr) deleted. Crontab cleaned of all three stale stubs.

## L. ~~Disabled crons + dead scripts (Notifiarr migration leftovers)~~ → RESOLVED 2026-05-11

Verified live: all four `~/scripts/Ultra-*/` dirs purged (Version-Notifier, App-Monitor, Quota-Checker, Traffic-Monitor). Their functionality is now covered by manitoba-maint-pusher (live health), Kuma push monitors (visibility), and smoke #6 disk-usage (quota).

## M. Workstation-side artifacts

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| `P:\Documents\GIT\QFlix\scripts\manitoba-tunnel.ps1` | powershell daemon | runs on operator workstation (Windows Task: `\Archangel\Manitoba SSH Tunnel`) | Permanent SSH tunnel for 10 INTERNAL admin ports | NO | n/a | n/a | n/a | self-healing (Test-Tunnel via 42014) | n/a | Gitignored (hardcodes real FQDN). Post-2026-05-11: readarr/mylar3/ombi forwards + 42015-reserved-for-Profilarr comment removed. |
| `P:\Documents\GIT\EDGEbookmarks.html` | html | n/a | Local browser bookmarks dashboard (gitignored) | NO | n/a | n/a | n/a | n/a | n/a | 860 lines, one dir above repo per convention. Cleaned 2026-05-11: ombi/mylar3/readarr tiles removed; FAQ tile added. |

## N. Documentation served by user-nginx

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| `~/www/qflix-faq/index.html` (74 KB) | static html | served by user-nginx | End-user + operator FAQ + tutorial · 17 sections, 50+ Q&amp;As; blue-themed self-contained page | NO (regenerate from `scripts/data/qflix-faq.html` in repo) | Public | `https://quadstronaut.seedbox.example.com/faq/` | n/a (static) | n/a | n/a (covered transitively by canary-mobile-ux: if nginx down, that canary goes red) | Deployed 2026-05-11. Nginx fragment `~/.apps/nginx/proxy.d/qflix-faq.conf` opts out of htpasswd via `auth_basic off`. Repo source: `scripts/data/qflix-faq.html` + `scripts/data/qflix-faq.conf`. |
| `~/www/images/Q.png` (4 KB) | static png | served by user-nginx | Brand asset for newsletter + future surfaces; Listmonk media uploader is restricted, so we self-host | NO (regenerate from repo `Q.png` via `scripts/configure/60-www-images.sh`) | Public | `https://quadstronaut.seedbox.example.com/images/Q.png` | n/a (static) | n/a | n/a (covered transitively by canary-mobile-ux: if nginx down, that canary goes red) | Deployed 2026-05-10. Nginx fragment `~/.apps/nginx/proxy.d/qflix-images.conf` allowlists image extensions only; `server_tokens off` set globally; `error_page 403 404 = /images/_blank.png` masks errors. GitHub raw `https://raw.githubusercontent.com/Quadstronaut/QFlix/master/Q.png` is the documented fallback. |
| `~/www/images/newsletter/` (cache directory) | dir | dynamically populated | Plex poster cache for weekly newsletter renders | NO (recreated daily by qflix-newsletter.timer + qflix-poster-cache-prune.timer) | Public (via `../` → `/images/`) | `https://quadstronaut.seedbox.example.com/images/newsletter/<sha>.<ext>` | n/a (dynamic) | n/a | n/a (covered transitively by canary-mobile-ux) | Mirrored from Plex `/photo/` endpoints at render time by `qflix_newsletter.posters.mirror_posters()`; individual files served via nginx allowlist (images only). Pruned daily by `qflix-poster-cache-prune.timer` (00:00 UTC) — deletes files ≥30 days old. |

---

## Drift summary — 2026-05-11 resolution status

**Resolved repo-side (committed):**

- ✓ Repo renamed from `Optimize-Manitoba/` → `QFlix/`; tunnel ps1 consolidated to `QFlix/scripts/`.
- ✓ `manifest/apps.yaml`: readarr/mylar3/ombi entries removed; unpackerr/upgradinatorr/postgres entries added with `kind: process_pattern` for the no-systemd cases.
- ✓ `docs/internal-app-tunnels.md`: readarr/mylar3/ombi/Profilarr/Janitorr/Suggestarr/Watcharr rows removed.
- ✓ `scripts/manitoba-tunnel.ps1`: 17042/17045/17046/42015 forwards + parked-app comments removed; gitignored (hardcodes real FQDN).
- ✓ `EDGEbookmarks.html`: ombi/mylar3/readarr tiles removed.
- ✓ Repo `secrets/`: 12 orphan files moved to `.purged-2026-05-10/` (reversible). conjurr/newsletterr/jellyfin/jellystat/readarr/mylar3/ombi entries gone.
- ✓ `secrets/discord-webhook.url` pushed to seedbox `~/secrets/`. Notification channel now functional.

**Resolved seedbox-side (2026-05-11, after operator-driven `finalize-purge` + agent-driven finishers):**

- ✓ `app-readarr / app-mylar3 / app-ombi` uninstalled (backups in `~/.apps/backup/`); dirs gone from `~/.apps/`.
- ✓ `~/Conjurr/`, `~/scripts/Ultra-*/` (4 dirs), 12 orphan `~/secrets/*` moved to `~/.purged-2026-05-11/`.
- ✓ Crontab cleaned: Notifiarr-migration commented stubs + Conjurr/Newsletterr empty heartbeat blocks removed.
- ✓ Kuma SQL: 5 monitors deleted (#27 Readarr, #38 Mylar3, #42 Conjurr, #43 Newsletterr, #62 Ombi). Notification wires added for Recyclarr (#61), Qflix Newsletter (#63), Buildarr (#64), Tautulli (#50), and the 4 canaries (#52–#55) on channel 1 (QFlix Discord) + channel 2 (auto-heal).
- ✓ Kuma + manitoba-maint-pusher restarted; all 4 canaries re-pushed via `manitoba-maint canary push`.
- ✓ New Upgradinatorr Kuma push monitor (#65) created via `bootstrap-kuma-monitors.py`; token in `secrets/kuma-push-tokens.json` (now 26 entries).
- ✓ `secrets/discord-operator.id` created with the operator's Discord user ID and synced to seedbox; `notify.py` extended to ping that ID via `content: <@id>` (with `allowed_mentions`) on error/critical levels — embeds alone don't trigger a Discord push.
- ✓ `secrets/seedbox.ssh-host` created — distinct from the public HTTPS `seedbox.host` because Ultra.cc shared seedboxes have a shared SSH FQDN (seedbox.example.com) vs operator-slot HTTPS FQDN (quadstronaut.seedbox.example.com).
- ✓ `scripts/lib/ssh.sh` reads from the new SSH-host secret; fixed the "Could not resolve seedbox.example.com" failure mode in smoke + canaries.
- ✓ Full smoke test: **45 pass / 0 fail / 1 skip** (skip is `readarr-qbit` — expected since readarr is purged).
- ✓ End-to-end Discord operator ping verified (`notify.notify(..., "error")` returned True).

**Known still-not-verified (require time to pass):**
- ~~Whether `buildarr.timer` actually fires its weekly 04:30 UTC Monday run cleanly (next is 2026-05-11 04:30 UTC).~~ → it didn't (see below).
- End-to-end qflix-newsletter render to live subscribers — smoke #13b exercises `--dry-run`, not a real Listmonk POST. Next real send is Mon 08:00 UTC.

---

## 2026-05-11 — audit sweep findings + fixes

Live scan turned up four real bugs the existing monitoring was hiding. All but the buildarr blocker are fixed.

**Resolved:**

- ✓ **Cron heartbeat scripts unable to reach user-systemd bus.** All four `~/scripts/ops/heartbeat-*.sh` invoked `systemctl --user` from a cron shell that had no `XDG_RUNTIME_DIR`, returning `Failed to connect to bus: No medium found` (exit 1) and emailing the operator. `heartbeat-tdarr-node.sh` had no HTTP-curl fast path so it failed *every* */5min — 571 cron emails accumulated in `/var/spool/mail/quadstronaut` over ~48h before the fix. The other three only "worked" because their `curl /health` short-circuited before the bus call; their recovery path (the actual restart) was equally broken. Each script now sets `: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"; export XDG_RUNTIME_DIR` at the top, restoring auto-heal capability under crash conditions.
- ✓ **`systemd_oneshot` health probe added (`scripts/maint/lib/health.py`).** The `systemd_only` probe checks `is-active` — for timer-driven oneshot services that's nearly useless: the `.timer` reports "active" even when its `.service` has Result=exit-code. Buildarr proved the gap on 2026-05-11 04:30 (silent failure with green Kuma monitor). The new probe runs `systemctl --user show <unit> -p Result -p ActiveState` and judges by the .service's last Result, with in-flight (activating/deactivating) and never-ran (Result=="") treated as ok to avoid spurious red.
- ✓ **Lifecycle.start now supports cron-class apps with a `unit:` field.** Calls `systemctl --user reset-failed` then `start --wait` so the recovery loop's subsequent health probe reads the *new* Result, not the prior invocation's. `recovery._is_recoverable` updated to admit cron-with-unit, so the existing 3-strike-then-recover-then-notify pipeline (60s push cycle → 180s threshold → 3 attempts × [10/30/60]s backoff → Discord ping with @operator) now covers oneshot timer failures the same way it covers HTTP services.
- ✓ **Manifest re-pointed 4 of 5 timer-class apps onto `systemd_oneshot`:** `recyclarr`, `kometa`, `qflix-newsletter`, `upgradinatorr`. Each now declares `unit: <name>.service` and `kind: systemd_oneshot`. Live Kuma confirms post-deploy: `Kometa msg=success`, `Recyclarr msg=success`, `Qflix Newsletter msg=success`, `Upgradinatorr msg=success`.
- ✓ **nginx `proxy.d/` swept.** Went from 45 active + 2 disabled to **15 active**. 33 UCC-stock fragments for never-installed apps (`airsonic`/`autobrr`/`btsync`/`calibre`/`couchpotato`/`deluge`/`emby`/`filebrowser`/`jackett`/`jdownloader2`/`lazylibrarian`/`lidarr`/`medusa`/`navidrome`/`nextcloud`/`nzbget`/`nzbhydra2`/`overseerr`/`pydio`/`pyload`/`qui`/`requestrr`/`resilio`/`rutorrent`/`sabnzbd`/`sickchill`/`syncthing`/`thelounge`/`transmission`/`ubooquity`/`znc` + the two disabled `ombi.conf.disabled.*` / `uptimekuma.conf.disabled.broken-subpath`) moved to `~/.purged-2026-05-11/nginx-proxy-d/`. Snapshot tarball preserved alongside. Stale `~/.apps/nginx/logs/ombi.{access,error}.log` moved to `~/.purged-2026-05-11/nginx-logs/`. Five public paths (`/airsonic/`, `/autobrr/`, `/emby/`, `/lidarr/`, `/couchpotato/`) which were returning 502s no longer route. Verified live: `/sonarr/`, `/tautulli/`, `/seerr/`, `/faq/` all still work; `/airsonic/` and `/overseerr/` now stop at outer htpasswd (401) with no upstream to forward to.
- ✓ **nginx config warnings cleaned.** Removed the obsolete `http2_push_preload on;` directive from `proxy.d/qbittorrent.conf:5`. The `proxy_headers_hash_bucket_size` warning vanished on its own (one of the swept fragments was the source). Only the cosmetic `/var/log/nginx/error.log` permission alert remains (nginx compile-time default; harmless since the user-nginx writes to its own relative logfile).
- ✓ **`buildarr-jellyseerr` plugin removed from `~/.apps/buildarr/.venv/`.** Loaded but unused; misleading after the Overseerr+Jellyseerr→Seerr merger (Seerr 3.2.0 lives at the original `seerr-team/seerr`; no `buildarr-seerr` plugin exists on PyPI; Seerr is config-managed by `scripts/configure/30-seerr-arrs.py`).
- ✓ **buildarr-sonarr 0.6.4 + buildarr-radarr 0.2.6 patched to manage Sonarr v4 + Radarr v6.** 7 surgical venv edits captured under `scripts/patches/` and re-applied idempotently by `scripts/configure/60-buildarr-patches.sh`: `buildarr/config/base.py` (missing field/value → fall back to pydantic default rather than raise), `buildarr_sonarr/config/import_lists.py` (languageProfileId guard + Trakt 4 required fields → Optional), `buildarr_sonarr/config/profiles/release.py` (preferred + IPWR remote-map optional — captured from prior session), `buildarr_sonarr/config/connect.py` (OnGrabField/OnImportField enums extended with v4 fields), `buildarr_radarr/config/settings/media_management.py` (ColonReplacement += smart), `buildarr_radarr/config/settings/notifications/discord.py` (OnGrabField + OnImportField + OnManualInteractionField extended with v6 Tags + custom-format values), `radarr/models/colon_replacement_format.py` (SMART — captured from prior session). Populated `~/.apps/buildarr/buildarr.yml` with all 4 instances (sonarr, sonarr2, radarr, radarr2) using API keys from `~/secrets/`. End-to-end run: `Result=success, ExecMainStatus=0`; each instance reports "Remote configuration is up to date" + "Remote configuration is clean". Manifest flipped from legacy `systemd_only`-against-`.timer` to `systemd_oneshot`-against-`.service` so real failures now reach Kuma/Discord. Kuma confirms `Buildarr msg=success`. **Retire path:** when upstream catches up, `pip install -U buildarr buildarr-sonarr buildarr-radarr` in the venv clears the patched files; re-running `60-buildarr-patches.sh` will report "hunks do not apply" — that's the signal to delete the 7 `.patch` files + the configure script.

**Mail spool:** 14,359 lines / 583 messages built up pre-fix — 571 from `heartbeat-tdarr-node.sh` alone. Once the fix is verified through one full cron cycle, safe to truncate with `> /var/spool/mail/quadstronaut`.
