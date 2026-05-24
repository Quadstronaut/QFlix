# Manitoba — Live Artifact Inventory

**Source of truth.** Re-verified live 2026-05-11 against the seedbox
(`quadstronaut@seedbox.example.com`) — systemd units, timers, crontab,
`~/.apps/`, `~/secrets/`, Kuma `/metrics`, Kuma SQLite, nginx `proxy.d/`,
repo manifest. Where the manifest, docs, and live seedbox disagree, the
live seedbox wins and this file records both.

**Counts (live as of 2026-05-16 after post-release-0.0.1 verification):**
- **33 apps in `manifest/apps.yaml`** + 13 canaries (movie, anime, deletion, mobile-ux, vlogs-stall, qbit-stall, kometa-libraries, stale-log-watchdog, kometa-deploy-drift, prowlarr-indexer-health, hardlink-integrity, plex-transcoder, tautulli-plex-link).
- **51 Kuma monitors** total: **47 manitoba** (33 manifest-app monitors + 13 canary monitors + 1 `Manitoba Pusher` daemon-self-heartbeat) + 4 external (`Quadstronix`, `Quadstronix Node 1`, `Quadstronix Node 2`, `QFlix Collect (workstation)`). **47/47 manitoba UP.** Every app in the manifest plus all canaries reports continuously.
- All 47 manitoba monitors wired to both notification channels (`Mission Control - QFlix` Discord + `Manitoba auto-heal webhook`). No silent-failure drift.
- Notification channel: single Discord webhook + operator @ping (user-id read from `secrets/discord-operator.id`) on `error` / `critical` levels via `scripts/maint/lib/notify.py`.
- Last full smoke: **51 pass / 0 fail / 0 skip** (2026-05-20; prior `tdarr.server_port` secret gap closed — both `tdarr-up` and `tdarr-node-registered` now pass).

**Q2 coverage gap closed 2026-05-11** — the prior 6 apps that didn't push to Kuma (flaresolverr / unpackerr / postgres / tdarr-node / kometa / python-plexapi) now all have monitors. Required two `health.py` fixes: `os.path.expanduser` on the `venv_python` field (import_check), and a new optional `hostname` override on http_root/http_api (so FlareSolverr's Docker-bridge bind at `172.17.0.1:17011` is probable from host netns).

`Auto-heal?` = `manitoba-maint-pusher` covers the app (any manifest entry with a `kuma_monitor:` value), or a dedicated heartbeat script restarts it. `Notification on fail?` = N Kuma notification slots wired.

---

## A. Manifest — UCC apps (managed by Ultra.cc `app-<slug>`)

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| sonarr | ucc | yes | TV main library *arr | NO | Internal | tunnel `http://localhost:17026/sonarr/` · public `https://quadstronaut.seedbox.example.com/sonarr/` (htpasswd) | yes (Mon 11-15) | yes (pusher) | 2 |  |
| sonarr2 | ucc | yes | TV anime *arr | NO | Internal | tunnel `http://localhost:17003/sonarr2/` · public same-base (htpasswd) | yes | yes (pusher) | 2 |  |
| radarr | ucc | yes | Movies main library *arr | NO | Internal | tunnel `http://localhost:17027/radarr/` · public (htpasswd) | yes | yes (pusher) | 2 |  |
| radarr2 | ucc | yes | Movies anime *arr | NO | Internal | tunnel `http://localhost:17008/radarr2/` · public (htpasswd) | yes | yes (pusher) | 2 |  |
| prowlarr | ucc | yes | Indexer aggregator | NO | Internal | tunnel `http://localhost:17024/prowlarr/` | yes | yes (pusher) | 2 |  |
| bazarr | ucc | yes | Subtitles (main TV + main movies) | NO | Internal | tunnel `http://localhost:17031/bazarr/` | yes | yes (pusher) | 2 |  |
| bazarr2 | systemd-user | yes (bazarr2.service, bare-Python under `~/.apps/bazarr2/`) | Subtitles (anime TV + anime movies) | NO | Internal | tunnel `http://localhost:17032/bazarr2/` (loopback only, no nginx) | yes (bazarr2-sync.timer pins to bazarr-1 version) | yes (pusher) | 2 | Ultra.cc has no `app-bazarr2` slot — bare-Python install on python3.11 venv. Paired with Sonarr2 + Radarr2 via loopback ports (no htpasswd path). `bazarr2-sync.service` (hourly oneshot) keeps the version aligned with bazarr-1 by fetching the matching upstream tag and re-applying the waitress `threads=100→4` patch. Closed deferral: `docs/anime-subs-deferred.md`. |
| qbittorrent | ucc | yes (qbittorrent.service v5.0.3) | Download client | NO | Internal (operator-public via /qbittorrent/) | tunnel `http://localhost:17041/` · public `https://…/qbittorrent/` (htpasswd + qBit auth) | yes | yes (pusher) | 2 | No `~/.apps/qbittorrent/` dir — config at system level; manifest probes via http_root. |
| plex | ucc | yes | Media server (canonical) | NO | Public | `https://quadstronaut.seedbox.example.com/web/` (Plex SSO) | yes | yes (pusher) | 2 |  |
| seerr | ucc | yes (Docker container `seerr-quadstronaut`, v3.2.0) | User request portal + issue tracking | NO | Public — `https://quadstronaut.seedbox.example.com/seerr/` AND canonical `https://seerr-quadstronaut.seedbox.example.com/` | port 42011 (Docker) · loopback `http://127.0.0.1:42011/` | yes (Mon 11-15) | yes (pusher; manifest `kuma_monitor: "Seerr"`, health probes `/api/v1/status` with `seerr.key`) | 2 | Installed 2026-05-11 via `app-seerr install` v3.2.0; Jellyseerr stopped + purged → `~/.purged-2026-05-11/jellyseerr-install/`. API key in `~/secrets/seerr.key`. 4 *arr servers configured via API (Sonarr Cinema default + Sonarr Anime non-default + Radarr Cinema default + Radarr Anime non-default; idempotent script `scripts/configure/30-seerr-arrs.py`). `trustProxy: true` on `/api/v1/settings/network` for the nginx reverse proxy. **Anime auto-routing caveat:** per [docs.seerr.dev](https://docs.seerr.dev/using-seerr/settings/services) only `isDefault`/`is4k` are documented routing axes — no cross-server anime field exists. Users pick "Sonarr Anime" / "Radarr Anime" from the per-request server dropdown in the Seerr UI. Restart confirmed clean 2026-05-11 (`app-seerr restart` → 8s warmup → HTTP 200 both loopback + via nginx). |
| tautulli | ucc | yes | Plex stats (read-only public) | NO | Public | `https://…/tautulli/` (auth_basic off confirmed) · tunnel `localhost:17014` | yes | yes (pusher) | 2 | Second channel wired 2026-05-11. `pms_url` pinned to `http://172.17.1.250:32400` + `pms_url_manual=1` (see `scripts/configure/50-tautulli-pms-url-fix.sh`): Tautulli's Docker container can't resolve `*.plex.direct`, which silently broke `get_metadata` API + WebSocket session enrichment for ~1 month. |
| audiobookshelf | ucc | yes | Audiobook server | NO | Internal | tunnel via port `secrets/audiobookshelf.port` | yes | yes (pusher) | 2 |  |
| kavita | ucc | yes | Manga / comics / ebook reader | NO | Internal | tunnel via port `secrets/kavita.port` | yes | yes (pusher) | 2 |  |
| komga | ucc | yes | Comics server | NO | Internal | tunnel `localhost:<komga.port>/komga/` | yes | yes (pusher) | 2 |  |
| calibre-web | ucc | yes | Ebook catalog | NO | Internal | tunnel via `secrets/calibre-web.port` | yes | yes (pusher) | 2 |  |
| homarr | ucc | yes | Public landing board | NO | Public | `https://quadstronaut.seedbox.example.com/` (root) + `/board/private` htpasswd | yes | yes (pusher) | 2 |  |
| flaresolverr | ucc | yes | Cloudflare-bypass for Prowlarr | NO | Internal | API-only `172.17.0.1:<flaresolverr.port>` | yes | yes (pusher) | 2 | `kuma_monitor: "FlareSolverr"` — http_root probe with `hostname: 172.17.0.1` override (Docker-bridge bind). Monitor added 2026-05-11. |
| maintainerr | ucc | yes | Library deletion rules (60-day) | NO | Internal | tunnel `http://localhost:42007/` · per-app subdomain `https://maintainerr-quadstronaut.seedbox.example.com/` (htpasswd) | yes | yes (pusher) | 2 | Prior session falsely claimed parked — corrected in commit 30b9e08. |
| unpackerr | ucc (Docker `/app/unpackerr -c …`) | yes | Auto-extract archives post-import for the *arr stack | NO | n/a (no HTTP surface) | n/a | yes (Mon 11-15) | yes (pusher; `process_pattern` probe matches `/app/unpackerr`) | 2 | `kuma_monitor: "Unpackerr"` — process_pattern probe. Smoke #8 verifies the process is running. Added to manifest 2026-05-11. |
| postgres | ucc | yes (PID supervised by UCC) | Database backend for Listmonk | NO | Internal | localhost:`secrets/postgres.port` | yes | yes (pusher; `process_pattern` matches `postgres: checkpointer`) | 2 | `kuma_monitor: "Postgres"` — `process_pattern: postgres: checkpointer`. Added to manifest 2026-05-11. |

## B. Manifest — systemd-class apps

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| listmonk.service | systemd | yes | Newsletter / mailing list manager | NO | Public archive + Internal admin | public `https://…/listmonk/campaign/<uuid>` · admin tunnel `localhost:42014` (canonical probe port) | yes | yes (heartbeat-listmonk cron + pusher) | 2 |  |
| tdarr-server.service | systemd | yes | Transcoding orchestrator | NO | Internal | tunnel `http://localhost:42018/` | yes | yes (heartbeat-tdarr-server cron + pusher) | 2 | Pinned to v2.17.01 (GLIBC). |
| tdarr-node.service | systemd | yes | Transcoding worker | NO | Internal | no UI | yes | yes (heartbeat-tdarr-node cron + pusher) | 2 | `kuma_monitor: "Tdarr Node"` — systemd_only probe. Monitor added 2026-05-11. |

## C. Manifest — cron/timer-driven apps

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| recyclarr.timer | cron | scheduled (Sun 04:30) | TRaSH-guide quality profile sync | NO | n/a | no UI | yes | n/a (oneshot) | 2 | Notif wires added 2026-05-11. |
| qflix-newsletter.timer | cron | scheduled (Mon 08:00) | Weekly Plex digest → Listmonk | NO | n/a | no UI | yes | n/a (oneshot) | 2 | Notif wires added 2026-05-11. Replaces Conjurr+Newsletterr. |
| qflix-poster-cache-prune.timer | cron | scheduled (00:00 UTC daily) | Delete poster cache files older than 30 days | NO | n/a | no UI | yes | n/a (oneshot) | n/a (`kuma_monitor: null`) | User-systemd timer. Probed via `lib/health.py systemd_oneshot`. Paired with cache directory `~/www/images/newsletter/`. |
| buildarr.timer | cron | scheduled (nightly 04:30) | Declarative *arr state converger | NO | n/a | no UI; log `~/.apps/buildarr/logs/buildarr.log` | yes | n/a (oneshot) | 2 | Notif wires added 2026-05-11. Patched to manage Sonarr v4 + Radarr v6 — 7 in-venv edits at `scripts/patches/`, idempotently re-applied by `scripts/configure/60-buildarr-patches.sh`. End-to-end working (`Result=success`, all 4 instances clean). Retire when upstream catches up. |
| kometa.timer | cron | scheduled (daily 03:30) | Plex metadata + collections | NO | n/a | no UI | yes | n/a (oneshot) | 2 | `kuma_monitor: "Kometa"` — systemd_oneshot probe. Result=success last run. |
| upgradinatorr.timer | cron | scheduled (Sun 06:00 ±30m jitter) | Re-search stale grabs across Sonarr/Sonarr2/Radarr/Radarr2 | NO | n/a | no UI | yes (outside maint window — pre-Monday) | n/a (oneshot) | 2 | Kuma push monitor added 2026-05-11. Manifest class:cron. |

## D. Manifest — library (no service)

| Artifact | Type | Running? | Purpose | Safe to delete? | Public/Internal | URL | In Mon-window? | Auto-heal? | Notification on fail? | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| python-plexapi | library | n/a | venv used by canaries + qflix-newsletter | NO | n/a | n/a | yes (pip-upgrade in window) | n/a | 2 | `kuma_monitor: "PlexAPI"` — import_check probe on the venv. Monitor added 2026-05-11. |

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
| manitoba-maint-canary-movie.timer | systemd | scheduled (hourly) | Seerr → Radarr movie path canary | NO | n/a | n/a | n/a | n/a | **1** (Kuma) | 2026-05-11 evening: rewritten to drive a real Seerr `POST /api/v1/request` and poll `media.externalServiceId` — forces traversal of the Seerr-in-container → Radarr-in-container netns hop. Stage labels on failure (`seerr-up-fail` / `radarr-up-fail` / `seed-pick-fail` / `seerr-push-fail` / `arr-not-populated` / `verify-fail` / `cleanup-fail`) reach Kuma `msg=`. |
| manitoba-maint-canary-anime.timer | systemd | scheduled (hourly) | Seerr → Sonarr2 anime path canary | NO | n/a | n/a | n/a | n/a | **1** | Same rewrite as movie canary, `mediaType=tv` + `seasons:[1]`. Picks lowest-id Sonarr2 series as seed (tvdb→tmdb resolved via Seerr search if Sonarr2's record is missing tmdbId). |
| manitoba-maint-canary-deletion.timer | systemd | scheduled (daily 04:30) | Maintainerr 60-day deletion-rule audit | NO | n/a | n/a | n/a | n/a | **1** |  |
| manitoba-maint-canary-mobile-ux.timer | systemd | scheduled (every 15m) | Homarr public board reachability | NO | n/a | n/a | n/a | n/a | **1** |  |
| scripts/canaries/{movie,anime,deletion,mobile-ux}.sh | script | invoked by services above | (see services) | NO | n/a | n/a | n/a | n/a | (via Kuma push) | README at `scripts/canaries/README.md` documents stage labels + exit-code contract for orchestrator (`manitoba-maint canary push <name>`). |

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
| `P:\Documents\GIT\QFlix\scripts\local-llm\qflix-rea.ps1` | powershell orchestrator | triggered AtLogOn by Windows Task `\Archangel\QFlix-LLM\QFlix Random Error Audit` | **QFlix Random Error Audit (REA)** — workstation-side second-opinion log audit. One SSH call pulls 7 seedbox log surfaces (*arr, systemd journal, cron mail, maint pipeline, nginx errors, Plex errors, Kuma red-state), hands the blob to every code-capable local Ollama model sequentially (`qwen3-coder:30b` / `qwen2.5-coder:7b` / `qwen3:8b` — auto-discovered via regex), collapses verdicts by signature, posts one Discord message (operator @ping on error, daily "✓ clean" heartbeat otherwise) | NO (audit pipeline gone) | n/a (workstation-local) | n/a | n/a (workstation-side, doesn't observe maint window) | dead-man Discord alert if Ollama unreachable (24h dedupe) | 1 (Discord webhook) | Gitignored (hardcodes real FQDN). Spec: `docs/superpowers/specs/2026-05-11-qflix-rea-design.md`. Test suite at `tests/local-llm/test-qflix-rea.ps1` (90 unit cases). Merged to master 2026-05-11 (commit 0d2624e); the `archangel/qflix-rea-do-not-delete` branch was a transient design idea that proved load-bearing only in conversation. Install: `scripts/local-llm/qflix-rea.ps1 -Install`. |
| `P:\Documents\GIT\QFlix\scripts\local\qflix-collect.ps1` + `scripts\local\qflix-mcp\qflix_mcp.py` | powershell daemon + python MCP server (stdio) | hourly Windows Task `\Archangel\QFlix\Hourly Collect` (top-of-hour, while logged on) + stdio MCP registered with Claude Code | **QFlix MCP** — hourly farm-snapshot collector + 11-tool MCP server. Collector SSH-invokes `~/scripts/mcp/{collect,logs,unstick}.py` to write `B:\QFlix\data\snapshots\<date>\HH.json`, append per-app logs, update `stale-state.json`, and autonomously unstick torrents with 3 hourly snapshots of zero progress (DELETE+blocklist → *arr researches). The MCP server reads that local cache for 8 read tools (status/list_torrents/torrent_history/list_stale/get_logs/plex_libraries/recent_events/arr_queue) and SSH-proxies 3 write tools (unstick_torrent/trigger_missing_search/refresh_collect). | NO | n/a (workstation-local cache; SSH-driven on seedbox) | n/a | n/a (workstation; doesn't observe maint window) | self-healing tunnel restart in collector; Kuma push monitor "QFlix Collect (workstation)" red after 90 min of misses | 2 (Discord webhook on every run + Kuma dead-man) | 25-task TDD build, 372 unit tests passing. Spec: `docs/superpowers/specs/2026-05-12-qflix-mcp-design.md`. Plan: `docs/superpowers/plans/2026-05-12-qflix-mcp.md`. Install: `scripts/local/install-qflix-collect.ps1 -Install` (Task Scheduler + B:\QFlix\data\) and `scripts/local/qflix-mcp/install.ps1` (venv + Claude Code register). Seedbox deploy: `scripts/configure/70-mcp-install.sh` (rsync + systemd-user units) + `scripts/configure/71-mcp-manifest-update.py` (manifest entry) + `scripts/configure/72-mcp-workstation-kuma-monitor.py` (workstation Kuma monitor). Daily 00:00 Phoenix (07:00 UTC) MissingSearch sweep via `qflix-missing-search.timer`. Replaces `arr-housekeeping.py --missing` (now delegated). |
| `~/.apps/vlogs/bin/victoria-logs-prod` + `scripts/maint/qflix-vlogs-ingest.py` + `scripts/configure/80-vlogs-install.sh` | linux binary + python ingester + installer | server: systemd-user `victorialogs.service` (long-running) — ingester: `qflix-vlogs-ingest.timer` (every 5 min) — canary: `manitoba-maint-canary-vlogs-stall.timer` (every 15 min) | **QFlix Logging — seedbox VictoriaLogs.** Persistent searchable log index for every managed app. Single-binary VictoriaLogs v1.50.0 (~15 MB exe, <512 MB RAM) bound loopback-only on `127.0.0.1:<secrets/vlogs.port>`, embedded storage at `~/.apps/vlogs/data/` with 90 d retention. Ingester imports `scripts/mcp/logs.py` in-process every 5 min (no SSH hop — same host) and POSTs JSON-lines to `/insert/jsonline` with stream fields `(host, app)`. The MCP server's `qflix_get_logs` does live SSH pulls; `qflix_query_logs` queries the index via SSH-exec'd curl (no workstation tunnel needed). Canary detects server-down or zero-ingest stalls and pushes to Kuma "Canary VLogs Stall". | NO | Loopback only (UI/API reachable via `ssh -L $PORT:127.0.0.1:$PORT $SEEDBOX -N`) | UI `http://127.0.0.1:$PORT/select/vmui/` · API `/select/logsql/query` (after tunnel) | Yes — auto-heal applies via manitoba-maint pusher (3-strike restart, then Discord page) | Yes — `victorialogs.service` is `class: systemd` in manifest with `http_root` probe; ingest is `class: cron` with `systemd_oneshot` probe | 3 Kuma monitors (`VictoriaLogs`, `Qflix VLogs Ingest`, `Canary VLogs Stall`) + Discord on auto-heal failure | Migrated from workstation to seedbox 2026-05-14 (PR vlogs-to-seedbox). Reason: workstation-resident ingest left a blind spot whenever the operator's PC was off — violated the autonomy mandate. Install: `bash scripts/configure/80-vlogs-install.sh`. Uninstall: `systemctl --user disable --now victorialogs.service qflix-vlogs-ingest.timer manitoba-maint-canary-vlogs-stall.timer` + `rm -rf ~/.apps/vlogs/`. |

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

---

## 2026-05-11 evening — Docker netns audit + canary rewrites

Same-day follow-up to the two `127.0.0.1` → `172.17.0.1` incidents
(Tautulli `pms_url` and Seerr→*arr push). Full sweep of every
Dockerized UCC app's outbound config across all the apps under
`~/.apps/`, cross-referenced against the actual cgroup membership of
each process (since `docker ps` is socket-denied for the operator
user, classification was done by `/proc/<pid>/cgroup`).

**Audit result.** 18 apps verified — one real bug:

- ✓ **Maintainerr `seerr_url` was `http://172.17.0.1:17013` (port dead).** Stale from the pre-2026-05-11 Jellyseerr setup; never updated when Seerr installed at port 42011. Pre-fix probe: `curl http://172.17.0.1:17013 → HTTP 000`. Post-fix probe: `curl http://172.17.0.1:42011/api/v1/status → HTTP 200`. SQLite updated (`UPDATE settings SET seerr_url='http://172.17.0.1:42011' WHERE id=1`) + container restarted (PID change confirmed). End-to-end seerr-call from Maintainerr deferred to the next 12:00 UTC CollectionWorker cycle — the Maintainerr container's internal nginx requires htpasswd we don't hold from the host. Repo: NEW idempotent script `scripts/configure/35-maintainerr-seerr-url.py` (sqlite UPDATE + `app-maintainerr restart`; re-runnable, prints `OK: ...already...` when state matches). See [[maintainerr-seerr-url-fix]].
- ✓ **Seerr `settings.json` and `scripts/configure/30-seerr-arrs.py`** carried over from earlier today's prior session: 4 *arr `hostname` fields all `172.17.0.1` (was `127.0.0.1` at install). Verified live: `settings.json` shows Radarr Cinema 172.17.0.1:17027, Radarr Anime 172.17.0.1:17008, Sonarr Cinema 172.17.0.1:17026, Sonarr Anime 172.17.0.1:17003.

**Everything else was already correct** — documented here so the
next agent doesn't re-audit:

- All 4 *arr download clients (Sonarr/Sonarr2/Radarr/Radarr2 → qBittorrent) route via the **public FQDN** `https://quadstronaut.seedbox.example.com:443/qbittorrent` (htpasswd + qBit auth). Architecturally fragile (depends on outer Ultra.cc nginx), but bypasses the netns issue entirely. Not changed.
- All 4 *arr indexers (Prowlarr-fed) use `http://172.17.0.1:17024/prowlarr/<id>/`. Correct.
- Prowlarr `/api/v1/applications` for all 4 downstream *arrs use `http://172.17.0.1:170XX/<urlbase>`. Correct.
- Prowlarr's FlareSolverr indexer-proxy uses `http://172.17.0.1:17011/`. Correct.
- Sonarr + Radarr Plex notification has `server: http://172.17.1.208:32400` (stale Plex container IP) **but** the active connection uses `host: 172.17.1.250` (current Plex bridge IP) — POST `/api/v3/notification/<id>/test` returns 200, current `~/.apps/sonarr/logs/sonarr.txt` has zero `172.17.1.208` errors. The `server` field is a UI label from the OAuth flow, not the runtime connect target. Cosmetic, left alone.
- Maintainerr `tautulli_url = http://172.17.0.1:17014/tautulli` and `plex_hostname = 172.17.1.250 port 32400` are both reachable. The recurring `[WARN] [PlexApiService] Plex connection failed (manual mode active — skipping re-discovery)` log entry is the *plex.tv re-discovery* skip notice, not a runtime connect failure — Plex itself is fine at `172.17.1.250:32400` from inside Maintainerr's container.
- Homarr `integration.url` entries all on `172.17.0.1` or public FQDN. Correct.
- Bazarr (Docker) talks to Sonarr/Radarr via `quadstronaut.seedbox.example.com:443/<arrbase>` (public FQDN routing — same family as the *arr→qBit pattern).
- Bazarr2 (host-native bare-Python at `~/.apps/bazarr2/venv/`) talks to Sonarr2/Radarr2 at `127.0.0.1:170XX` — works because every UCC app's listener is bound on `127.0.0.1` AND `169.150.251.170` AND `172.17.0.1` simultaneously. The `plex` block has `apikey: ''` so the listed `ip: 127.0.0.1` is dormant (integration disabled).
- Kometa, Buildarr, Recyclarr, qflix-newsletter: all **host-native** (`%h/.apps/<app>/venv/bin/python ...` from systemd-user services); `127.0.0.1` references are correct.
- Listmonk (systemd host), nginx (host), tdarr-node/-server (host), qbittorrent (host) — all `127.0.0.1` references are LISTEN-side or host-loopback-correct.
- Postgres (Docker, runs as the `quadstronaut listmonk` connection from `172.17.0.1:7872` in `pg_stat_activity` — verified live); `pg_hba.conf 127.0.0.1/32` rule is for auth-from-host-loopback (Listmonk lives on host).

**Canary rewrite (`scripts/canaries/{movie,anime}.sh`).** The prior probes ran on the host netns (`curl 127.0.0.1:17027/...`) and stayed green for ~9h on 2026-05-11 while every Seerr→Radarr request was failing with `ECONNREFUSED 127.0.0.1:17027` inside Seerr's container — the netns blind spot the new probes close. New design: pick the lowest-id movie/series already in the *arr as seed → `POST /api/v1/request` against Seerr → poll `media.externalServiceId` for up to 30s → verify the id matches the *arr's record → `DELETE /api/v1/request/{id}` cleanup. 409 (already-requested) is handled by recovering the existing request id and skipping the cleanup step; the *arr movie/series is never touched. Stage-labelled failure messages (`seerr-up-fail` / `radarr-up-fail` / `sonarr2-up-fail` / `seed-pick-fail` / `seerr-push-fail` / `arr-not-populated` / `verify-fail` / `cleanup-fail`) flow through `manitoba-maint canary push <name>` into Kuma's `msg=` field. Live first runs (movie + anime, both via the orchestrator) returned exit 0; Kuma heartbeats show `1|PASS: movie canary — seerr→radarr push verified (tmdb=504827 req=23 rrMid=261 created=1)` and `1|PASS: anime canary — seerr→sonarr2 push verified (tmdb=209867 tvdb=424536 req=24 s2Sid=1 created=1)` as the latest entries. Red-path proven by a direct Kuma push of `status=down msg=STAGE=seerr-up-fail msg=test-red-path-verification` — heartbeat row recorded `0|STAGE=seerr-up-fail msg=test-red-path-verification` before the next green push restored it.
