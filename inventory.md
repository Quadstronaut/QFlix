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
- Notification channel: single Discord webhook + operator @ping (`<@REDACTED>`) on `error` / `critical` levels via `scripts/maint/lib/notify.py`.
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
| bazarr | ucc | yes | Subtitles | NO | Internal | tunnel `http://localhost:17031/bazarr/` | yes | yes (pusher) | 2 |  |
| qbittorrent | ucc | yes (qbittorrent.service v5.0.3) | Download client | NO | Internal (operator-public via /qbittorrent/) | tunnel `http://localhost:17041/` · public `https://…/qbittorrent/` (htpasswd + qBit auth) | yes | yes (pusher) | 2 | No `~/.apps/qbittorrent/` dir — config at system level; manifest probes via http_root. |
| plex | ucc | yes | Media server (canonical) | NO | Public | `https://quadstronaut.seedbox.example.com/web/` (Plex SSO) | yes | yes (pusher) | 2 |  |
| seerr | ucc | yes (Docker container `seerr-quadstronaut`, v3.2.0) | User request portal + issue tracking | NO | Public — `https://quadstronaut.seedbox.example.com/seerr/` AND canonical `https://seerr-quadstronaut.seedbox.example.com/` | port 42011 (Docker) · loopback `http://127.0.0.1:42011/` | yes (Mon 04-08) | yes (pusher; manifest `kuma_monitor: "Seerr"`, health probes `/api/v1/status` with `seerr.key`) | 2 | Installed 2026-05-11 via `app-seerr install` v3.2.0; Jellyseerr stopped + purged → `~/.purged-2026-05-11/jellyseerr-install/`. API key in `~/secrets/seerr.key`. 4 *arr servers configured via API (Sonarr Cinema default + Sonarr Anime non-default + Radarr Cinema default + Radarr Anime non-default; idempotent script `scripts/configure/30-seerr-arrs.py`). `trustProxy: true` on `/api/v1/settings/network` for the nginx reverse proxy. **Anime auto-routing caveat:** per [docs.seerr.dev](https://docs.seerr.dev/using-seerr/settings/services) only `isDefault`/`is4k` are documented routing axes — no cross-server anime field exists. Users pick "Sonarr Anime" / "Radarr Anime" from the per-request server dropdown in the Seerr UI. Restart confirmed clean 2026-05-11 (`app-seerr restart` → 8s warmup → HTTP 200 both loopback + via nginx). |
| tautulli | ucc | yes | Plex stats (read-only public) | NO | Public | `https://…/tautulli/` (auth_basic off confirmed) · tunnel `localhost:17014` | yes | yes (pusher) | 2 | Second channel wired 2026-05-11. |
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
| buildarr.timer | cron | scheduled (Mon 04:30) | Declarative *arr state converger | NO | n/a | no UI; log `~/.apps/buildarr/logs/buildarr.log` | yes | n/a (oneshot) | 2 | Notif wires added 2026-05-11. |
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
| nginx.service | UCC | yes (v2026.04.28) | User-level reverse proxy (40+ `proxy.d/` fragments) | NO | Public-facing | binds 80/443 of operator's slot | yes | systemd Restart=on-failure | **0** (no Kuma monitor) | Outer Ultra.cc root-nginx terminates TLS + applies htpasswd; this user-nginx maps paths to app ports. Only `listmonk.conf` and `tautulli.conf` set `auth_basic off;`. |
| uptimekuma | UCC | yes | Status monitoring + push receiver | NO | Internal admin + Public status page | admin tunnel `localhost:42005` · public `https://…/status/manitoba` | yes | n/a (self-monitoring) | n/a (it IS the monitor) | DB at `~/.apps/uptimekuma/kuma.db`. 29 monitors total (26 manitoba + 3 quadstronix external). |

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
| `P:\Documents\GIT\EDGEbookmarks.html` | html | n/a | Local browser bookmarks dashboard (gitignored) | NO | n/a | n/a | n/a | n/a | n/a | 860 lines, one dir above repo per convention. Cleaned 2026-05-11: ombi/mylar3/readarr tiles removed. |

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
- Whether `buildarr.timer` actually fires its weekly 04:30 UTC Monday run cleanly (next is 2026-05-11 04:30 UTC).
- End-to-end qflix-newsletter render to live subscribers — smoke #13b exercises `--dry-run`, not a real Listmonk POST. Next real send is Mon 08:00 UTC.
