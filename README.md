# Optimize-Manitoba

A reproducible, opinionated configuration of a Plex-primary media stack on a single Ultra.cc shared seedbox (`quadstronaut.seedbox.example.com`). Everything an operator needs to bootstrap, run, monitor, and self-heal the stack lives in this repo: install scripts, a single-source-of-truth manifest, a Python maintenance daemon, a Playwright-driven cp.ultra.cc upgrade clicker, a Kuma-integrated auto-recovery loop, and end-to-end canaries.

The stack is **production-grade for one operator**. State changes go through tested code paths; nothing important is left to "click around in the UI." When something does need a UI click, headless Firefox does it on a schedule.

---

## What's in the stack

**28 apps** (manifest/apps.yaml) + **4 end-to-end canaries**.

| Role | Apps | Status |
|---|---|---|
| Media servers | **Plex** (primary) | live |
| Requests + invites | Jellyseerr | live (migration to Seerr queued — Plex-native, post-Jellyfin) |
| Requests + invites | Ombi | parked (legacy invite flows pending Wizarr/alt) |
| TV / Movies | Sonarr, Sonarr2 (anime branch), Radarr, Radarr2 (anime) | live |
| Books / Audiobooks | Readarr | parked (upstream retired 2026; replacement under evaluation) |
| Comics | Mylar3 | live (parked alongside Readarr book stack) |
| Subtitles | Bazarr | live |
| Indexer aggregator | Prowlarr (+ FlareSolverr for Cloudflare) | live |
| Torrent client | qBittorrent (single client; rTorrent / Deluge / Transmission decommissioned) | live |
| Stats | Tautulli | live |
| Library / posters | Maintainerr, Recyclarr (TRaSH-Guides), Kometa | live |
| Declarative *arr config | **Buildarr** (cron-class, nightly 04:30) | live (operator fills in `~/.apps/buildarr/buildarr.yml`) |
| Transcoding | Tdarr (server + node, hard-pinned at 2.17.01 — GLIBC blocker) | live |
| Comms | Listmonk (mass email), **qflix-newsletter** (weekly digest, Mon 08:00, Tautulli + Gemini + Listmonk) | live |
| Reading | Calibre-Web, Komga, Kavita, Audiobookshelf | parked pending Readarr replacement |
| Dashboard | Homarr (private + public boards, "Qflix" theme) | live |
| Monitoring | Uptime Kuma + manitoba-maint daemon | live |
| Operator alerts | direct Discord webhook (`secrets/discord-webhook.url`) | live (replaced Notifiarr passthrough 2026-05-10) |

**4 canaries** (`scripts/canaries/`) probe whole pipelines, not just liveness:
- **movie** (hourly) — Jellyseerr request → Radarr grab → qBit
- **anime** (hourly) — same, anime branch (Sonarr2)
- **deletion** (daily 04:30) — Maintainerr 60-day rule audit
- **mobile-ux** (every 15 min) — Homarr public board renders, < 512KB HTML, root domain redirects 302

---

## Decisions made (recent) and in flight

Architectural changes recently landed and items still pending. Items struck-through are done.

| # | Change | Status |
|---|---|---|
| 1 | ~~Purge Notifiarr → Discord webhook direct~~ | **DONE 2026-05-10** — *arr Notifiarr Connections + Tautulli passthrough deleted, `notifiarr.key` purged, `lib/notify.py` legacy fallback removed. Operator must populate `secrets/discord-webhook.url` for alerts to land. |
| 2 | **Replace Readarr** (Chaptarr / Bookshelf / LazyLibrarian) | pending — upstream retired 2026, repo archived |
| 3 | **Investigate Decypharr + Real-Debrid pipeline** | declined 2026-05-09 (operator: staying on traditional indexer + qBit) |
| 4 | ~~Purge Jellyfin~~ | **DONE 2026-05-10** — Jellyfin + Jellystat uninstalled, manifest cleaned |
| 5 | **Migrate Jellyseerr → Overseerr/Seerr** | pending — queued post-Jellyfin |
| 6 | ~~Wire Recyclarr fully~~ | **DONE 2026-05-10** — Kuma monitor "Recyclarr" + auto-heal coverage |
| 7 | ~~Park book/comic/audiobook apps until Readarr replacement~~ | **DONE 2026-05-10** — Readarr/Mylar3/Calibre-Web/Komga/Kavita/Audiobookshelf marked parked |
| 8 | **Implement #2 decision** | blocked on #2 |
| 9 | **Stremio-feeding-into-Plex** | declined 2026-05-09 (same as #3) |
| 10 | ~~Confirm Maintainerr replaces Deleterr~~ | **DONE** — Maintainerr is the canonical retention engine |
| 11 | ~~Replace Conjurr + Newsletterr with qflix-newsletter (Path B)~~ | **DONE 2026-05-10** — single Python script (Tautulli + Gemini + Listmonk), Mon-08:00 systemd timer; old apps decom'd, ~626 MB freed (Newsletterr's Playwright Chromium) |
| 12 | ~~Install Buildarr~~ | **DONE 2026-05-10** — declarative *arr config, nightly 04:30 cron |
| 13 | **Phase 3 deferred installs** | Suggestarr, Profilarr, Janitorr blocked on Ultra.cc per-process WASM/JVM heap limit; Watcharr parked (no subpath, like Wizarr). See `project_seedbox-wasm-oom-blocker` memory. |
| 14 | **Jellyseerr public-vs-htpasswd** | open — current state: htpasswd inherits; kickoff calls for `auth_basic off`. Operator decision pending in Phase 5 walkthrough. |

---

## Architecture

### One-screen overview

```
                         Operator workstation (Windows)
                                      │
                                      │ ssh / scp / Playwright Firefox
                                      ▼
  ┌──────────────── Ultra.cc shared seedbox (host netns) ────────────────┐
  │                                                                     │
  │   ~/.apps/<name>/         user-systemd ports 17xxx (loopback)       │
  │   ~/secrets/<name>.{key,port,urlbase,host}                          │
  │   ~/.opt/maint/{state.json, apps.yaml, lock}                        │
  │                                                                     │
  │   manitoba-maint (Python, user-systemd)                             │
  │     ├─ webhook   (loopback :42017 — receives Kuma down events)      │
  │     ├─ pusher    (every 60s — health-probe + push status to Kuma    │
  │     │             AND fire recovery.run async on probe failure)     │
  │     ├─ window    (Mon 04:00–08:00 lock + Discord open/close ping)   │
  │     └─ canary-*  (timers — fire scripts/canaries/*.sh)              │
  │                                                                     │
  │   ~/scripts/maint/cp_upgrade_clicker.py   (Mon 04:30 — Playwright   │
  │     Firefox → cp.ultra.cc → Upgrade & Repair on 12 UCC apps)        │
  │                                                                     │
  │   ~/.apps/qflix-newsletter/   (Mon 08:00 — Tautulli + Gemini +      │
  │     Listmonk campaign API; replaces Conjurr + Newsletterr)          │
  │                                                                     │
  │   ~/.apps/buildarr/   (nightly 04:30 — declarative YAML reconcile   │
  │     to Sonarr/Radarr/Prowlarr/Jellyseerr)                           │
  │                                                                     │
  └─────────────────┬───────────────────────────────────────────────────┘
                    │ /metrics (Basic auth, push-token URLs)
                    ▼
        Uptime Kuma (isolated netns: 127.0.0.1:42005 INSIDE container)
        28 PUSH monitors + 1 Discord-webhook notification target
```

The "isolated netns" detail matters: Kuma cannot reach the host's loopback. Pusher pushes status TO Kuma. Auto-heal originally relied on Kuma webhook IN — but Kuma can't POST to host-loopback either, so recovery is now triggered directly from the pusher when its probe fails. See `scripts/maint/lib/pusher.py`.

### Data flows (for both beginners and the "show me the wires" crowd)

The stack has four live data flows. Each one starts with a real human action and ends with an artifact. Beginners can follow the arrows; everyone else can drop into the named files for the gory details.

#### 1. Media ingestion — "I want to watch X"

```
  User taps Jellyseerr ──► Sonarr/Radarr ──► Prowlarr ──► newznab/torznab indexers
  (or "I just added it"      (`/api/v3/         (`/api/v1/         (or Cloudflare-walled,
   manually in Sonarr)       command")          search`)            via FlareSolverr at
                                │                                   172.17.0.1:17011)
                                │
                                ▼
                              qBittorrent (17041) ── grabs torrent ──► ~/data/torrents/...
                                │
                                │ on completion (qBit "Run external program")
                                ▼
                              Sonarr/Radarr import ── hardlink ──► ~/data/media/Movies/<...>
                                                                   ~/data/media/TV Shows/<...>
                                │
                                │ Bazarr watches *arrs, fetches subs
                                ▼
                              ~/data/media/.../<title>.<lang>.srt
                                │
                                │ Plex scans ~/data/media/* (auto on completion via
                                │ scripts/post-import/library-rescan-plex.sh)
                                ▼
                              Plex (172.17.1.250:32400) ── available to user
```

Hardlinks are sacred: the *arrs hardlink (not copy) from `torrents/` into `media/`. That keeps qBit seeding the same physical bytes Plex is streaming. `scripts/smoke-test.sh` step 5 spot-checks linkcount.

#### 2. Library hygiene — "I want my disk back"

```
  Maintainerr (42007) ── nightly rule pass
    │
    ├── "watched 60 days ago + nobody else watched" ──► tag for deletion
    └── delete from Plex collection + delete file
                                                  │
  Recyclarr (cron) ── pulls TRaSH-Guides ──► writes Sonarr/Radarr quality profiles
                                                  │
  Kometa (cron)    ── pulls Plex-meta-manager   ──► writes Plex collections/posters
                                                  │
  Buildarr (cron 04:30) ── reads buildarr.yml   ──► reconciles *arr settings
                                                       (declarative — drift becomes nightly fix)
```

#### 3. Operator visibility — "is anything on fire?"

```
  manitoba-maint-pusher.service (every 60s)
    │
    ├── for each app: probe health (HTTP, systemd state, port listen)
    ├── push status to Kuma /metrics (Push monitor per app)
    └── on 3 consecutive probe failures: recovery.trigger_async(app)
                                              │
                                              ▼
                                            lifecycle.start(app) up to 3 times
                                              │ on still-failing
                                              ▼
                                            notify.py ──► Discord webhook
                                                          (was Notifiarr, retired
                                                          2026-05-10)
  Canaries (timers, hourly / 15min / daily):
    movie / anime    ── Jellyseerr request → Sonarr/Radarr grab → qBit ── ✓
    deletion         ── Maintainerr 60-day rule audit                    ── ✓
    mobile-ux        ── Homarr public board renders + redirects          ── ✓
                          (each pushes Pass/Fail to its own Kuma monitor)

  Operator's permanent SSH tunnel (manitoba-tunnel.ps1) forwards every
  INTERNAL admin port to localhost:<same-port> so SPA admin UIs (Listmonk,
  *arrs, Tdarr, Maintainerr) work at root from the workstation's perspective.
```

#### 4. Subscriber comms — "Mon morning email"

```
  qflix-newsletter.timer (Mon 08:00, post-maintenance window)
    │
    ▼
  ~/.apps/qflix-newsletter/.venv/bin/python -m qflix_newsletter
    │
    ├── Tautulli  ── recently_added (50 items) ──┐
    ├── Sonarr    ── /calendar (14d window)     ──┤
    ├── Sonarr2   ── /calendar (14d, anime)     ──┤── normalize → RecentItem / CalendarItem
    ├── Radarr    ── /calendar (14d)            ──┤
    ├── Radarr2   ── /calendar (14d, anime)     ──┤
    ├── TMDB      ── ratings for "Pick of Week" ──┘
    │
    ├── Gemini    ── 3 "if you liked X try Y" picks (small, bottom-of-email)
    │
    ├── Jinja2    ── render scripts/qflix-newsletter/.../templates/weekly.html.j2
    │                (Pick of Week → New Movies → New TV → Anime → Coming Soon
    │                 → AI Picks → Nerd Corner)
    │
    └── Listmonk  ── POST /api/campaigns + PUT .../status=running
                     │
                     ▼
                   Listmonk fans out via SMTP → subscribers
                     │
                     ▼
                   "View in browser" link → https://<fqdn>/listmonk/campaign/<uuid>
                                            (server-rendered; nginx subpath
                                             rewrites handle CSS/asset paths)
```

The newsletter is one Python invocation. Failure modes (Tautulli down, Gemini rate-limited, Listmonk down) are caught and logged; the script still ships an email with whatever data it could gather, with the AI section silently empty if Gemini fails. See `scripts/qflix-newsletter/qflix_newsletter/main.py`.

---

## Repo layout

```
manifest/apps.yaml           # 28 apps + 4 canaries — single source of truth
versions.env                 # Pinned versions (Tdarr only — pin policy lifted 2026-05-09)
Tuesday.md                   # Design doc — extending Mon maintenance to systemd apps
                             # NB: bookmarks/EDGEbookmarks.html are gitignored — operator keeps a local copy outside the repo

docs/
  operator-deferred.md       # Manual steps that can't be scripted yet
  transition-log.md          # Reversible state-changes log (stop/start/uninstall)
  internal-app-tunnels.md    # Public/internal split + ssh -L command per INTERNAL app
  secrets-convention.md      # ~/secrets/ inventory + filename rules
  arr-audit-2026-05-09.md    # Most recent *arr stack audit
  arr-audit-actions-*.md     # Audit punch-list
  external/ultracc-reference.md  # Ultra.cc CLI / file-layout cheat-sheet
  superpowers/               # Plan + spec docs (longer-form designs)

scripts/
  configure/                 # phased install/configure scripts (numbered 01..61)
                             # Run in numeric order on a fresh seedbox.
  install/                   # Lower-level installer libs (used by configure/)
  lib/                       # Shared bash helpers: ssh, log, secrets, pwgen
  data/                      # Static config: kuma-qflix*.css, prowlarr indexer JSON, unpackerr template
  ops/                       # Cron-friendly heartbeat scripts per long-running app
  smoke/                     # Read-only audits + one-shot fixes (arr-audit, arr-audit-fixes)
  plex/                      # Plex-specific utilities (kill_stream, stream_stats)
  post-import/               # Library-rescan callbacks invoked by *arrs after import
  canaries/                  # 4 end-to-end pipeline checks (bash)
  qflix-newsletter/          # Mon-08:00 weekly digest (replaces Conjurr+Newsletterr 2026-05-10)
                             # Python package: qflix_newsletter/ + tests fixtures + Jinja2 template
  smoke-test.sh              # Production smoke (190+ checks across the whole stack)
  smoke-test-plex.sh         # Plex-ecosystem-only smoke

  maint/                     # The maintenance daemon
    manitoba-maint           # CLI entrypoint (Python shim)
    bootstrap-kuma-monitors.py  # One-shot: create push monitors + webhook + tokens
    cp_upgrade_clicker.py    # Playwright/Firefox Mon-morning Upgrade & Repair sweep
    arr-housekeeping.py      # daily Find-Missing + hourly stuck-queue unstick (cron)
    lib/                     # Python codebase
      manifest.py     # Load + validate manifest/apps.yaml
      health.py       # http_root / http_api / systemd_only / port_listen probes
      lifecycle.py    # start/stop/restart + upgrade/downgrade per class (ucc/systemd/cron/library)
      recovery.py     # 3-attempt restart with backoff + auto-downgrade + operator-escalation ping
      kuma.py         # /metrics client + webhook receiver (canary auto-heal handler)
      pusher.py       # 60s push loop — pushes health to Kuma; auto-heal fires only after 3 CONSECUTIVE failed probes (~180s sustained downtime)
      window.py       # Maintenance-window lockfile + drain logic
      notify.py       # Operator-escalation ping (Discord webhook only — Notifiarr removed 2026-05-10)
      state.py        # JSON state file at ~/.opt/maint/state.json
      cli.py          # All argparse subparsers + verb dispatch
      qbit.py         # qBit pw rotation + *arr cascade ('manitoba-maint qbit rotate-pw')
    systemd/          # 8 services + 8 timers, deployed to ~/.config/systemd/user/

secrets/                     # *gitignored* — per-secret one-line files (see docs/secrets-convention.md)
tests/                       # 202 pytest tests (unit/) — pure-Python, no SSH
  conftest.py
  unit/test_*.py
  fixtures/
```

---

## Manifest — the source of truth

`manifest/apps.yaml` is the only place where "what apps exist" is recorded. Everything else (health probes, systemd units, Kuma monitors, recovery, upgrade) reads from here. Schema:

```yaml
defaults:
  health_timeout_s: 5
  recovery_attempts: 3
  recovery_backoff_s: [10, 30, 60]
  lifecycle_timeout_s: 60
  kuma_recheck_delay_s: 90

apps:
  sonarr:
    class: ucc                           # ucc | systemd | cron | library
    ucc_slug: sonarr
    kuma_monitor: "Sonarr"               # null = no Kuma monitor
    parked: false                        # true = pusher skips auto-heal (e.g. ombi)
    health:
      kind: http_api                     # http_api | http_root | systemd_only | port_listen | import_check
      path_template: "/{urlbase}/api/v3/system/status"
      auth_header: "X-Api-Key"
      auth_secret: sonarr.key
      port_secret: sonarr.port
      urlbase_secret: sonarr.urlbase
      expect_status: 200
    upgrade:
      kind: tarball_swap                 # tarball_swap | zip_swap | git_checkout | pip_install | (UCC: app-<slug> update)
      url_template: "..."
      target_path: "..."
      version_pin:                       # OPTIONAL — most apps don't pin (policy lifted)
        source: versions.env
        key: TDARR_VERSION
        max: "2.17.01"                   # ceiling, with reason:
        max_reason: "GLIBC 2.34 required, host has 2.31"

canaries:
  movie:
    kuma_monitor: "Canary Movie"
    script: "scripts/canaries/movie.sh"
    schedule: "hourly"                   # hourly | every-15min | daily-0430
```

**Validation:** `from lib.manifest import load; load("manifest/apps.yaml")` raises `ManifestError` on duplicate keys, unknown classes, missing required fields.

---

## Maintenance daemon — `manitoba-maint`

Single Python CLI; same code paths whether an operator runs `manitoba-maint restart sonarr` or a Kuma push event triggers recovery.

### Verbs

```
manitoba-maint status [APP|--all]                 # health + state table
manitoba-maint start  APP                         # class-aware start
manitoba-maint stop   APP                         # class-aware stop
manitoba-maint restart APP
manitoba-maint upgrade APP [--to VERSION]         # honors version_pin or latest
manitoba-maint downgrade APP --to VERSION
manitoba-maint recover APP                        # 3-attempt restart loop
manitoba-maint webhook                            # daemon: HTTP receiver on 127.0.0.1:42017
manitoba-maint pusher                             # daemon: every-60s health-push to Kuma
manitoba-maint window {open|close|status|watchdog}
manitoba-maint manifest {validate|render-status-config}
manitoba-maint kuma audit                         # drift report: manifest vs live Kuma monitors
manitoba-maint canary push {movie|anime|deletion|mobile-ux}
manitoba-maint qbit rotate-pw --new <pw> [--dry-run]
```

### Daemons (user-systemd timers + services)

| Unit | Schedule | Purpose |
|---|---|---|
| `manitoba-maint-pusher.service` | running | health probe + push to Kuma every 60s; auto-heals on probe failure |
| `manitoba-maint-webhook.service` | running | receives Kuma down/up events on 127.0.0.1:42017 |
| `manitoba-maint-window.timer` | Mon 04:00 | window open: lock + Discord webhook ping |
| `manitoba-maint-window.service` | (oneshot) | actual window logic |
| `manitoba-maint-window-watchdog.timer` | Mon 15:00 UTC | hard-close + drain queued events |
| `manitoba-maint-cp-upgrade.timer` | Mon 04:30 | Playwright/Firefox cp.ultra.cc Upgrade & Repair sweep (12 UCC apps) |
| `manitoba-maint-canary-movie.timer` | hourly | run scripts/canaries/movie.sh, push status |
| `manitoba-maint-canary-anime.timer` | hourly | (same) |
| `manitoba-maint-canary-deletion.timer` | daily 04:30 | (same) |
| `manitoba-maint-canary-mobile-ux.timer` | every 15min | (same) |

Plus operator cron entries (installed by `scripts/configure/240-maintenance-install.sh`):

| Cron | Schedule | Purpose |
|---|---|---|
| `arr-housekeeping --missing` | `0 4 * * 0,2-6` (Tue–Sun 04:00) | trigger MissingEpisodeSearch / MissingMoviesSearch / MissingBookSearch on each *arr |
| `arr-housekeeping --unstick` | `15 * * * *` (hourly) | DELETE+blocklist queue items stuck >=6h in importPending/importBlocked/importFailed |
| `heartbeat-maint-webhook.sh` | `*/5 * * * *` | smoke the webhook receiver |

### Auto-recovery flow

1. Pusher probes app every 60s.
2. On `health.probe(app).ok == False`:
   - **Increment per-app strike counter.** Only invoke `recovery.trigger_async` when strikes reach `MANITOBA_AUTOHEAL_STRIKES` (default **3**). Single transient blips do NOT trigger recovery — must be 3 consecutive failures (~180s of sustained downtime) before any restart fires.
   - First success after a strike streak resets the counter to 0.
3. When threshold is hit, `recovery.trigger_async(app)` — fire-and-forget thread (non-blocking)
   - Skips if `app.parked == True` (e.g. Ombi)
   - Skips if app already has a recovery in flight (per-app lock)
4. `recovery.run` — up to 3 attempts of `lifecycle.start(app)` with backoff `[10, 30, 60]s`
5. After each restart: probe locally → if ok, wait 90s → query Kuma → if Kuma still says down, escalate via operator ping (Discord webhook)
6. After 3 failed restart attempts: try one auto-downgrade to `state.previous_version` (skipped for UCC class), then operator-escalate

**Total time from first failure to operator ping** for a genuinely-down app: ~180s strike-accumulation + ~100s of restart attempts ≈ **~5 min before your phone buzzes**.

The maintenance-window lockfile pauses recovery — events are queued to `~/.opt/maint/window-events.jsonl` and drained at window close.

---

## Quick start (fresh seedbox)

```bash
# 0. Discover what's already there + populate ~/secrets locally
./scripts/bootstrap-discover.sh

# 1. Run configure scripts in numeric order. Each is idempotent.
for s in scripts/configure/0*.sh scripts/configure/[1-9]*.sh; do
  bash "$s" || break
done
# Or run a single phase:
bash scripts/configure/240-maintenance-install.sh

# 2. Bootstrap Kuma push monitors (creates the 29 monitors + the auto-heal webhook notification + tokens).
PYTHONPATH=scripts/maint tests/.venv/Scripts/python.exe scripts/maint/bootstrap-kuma-monitors.py

# 3. Smoke
bash scripts/smoke-test.sh
```

Smoke target as of v2: **190+ checks pass**, covering Prowlarr indexers, *arr→qBit reachability, Plex library, Maintainerr rules, qflix-newsletter render + timer, Buildarr timer, canary monitor presence, etc.

---

## Operational runbook

| Task | Command |
|---|---|
| Status of everything | `manitoba-maint status --all` |
| Restart a flaky app | `manitoba-maint restart bazarr` |
| Force a recovery cycle | `manitoba-maint recover bazarr` |
| Audit Kuma drift | `manitoba-maint kuma audit` |
| Rotate qBit password | `manitoba-maint qbit rotate-pw --new <pw>` |
| Run *arr audit | `python3 scripts/smoke/arr-audit.py > docs/arr-audit-$(date +%F).md` |
| Apply *arr audit fixes | `python3 scripts/smoke/arr-audit-fixes.py [--dry-run]` |
| Manual canary fire | `systemctl --user start manitoba-maint-canary-movie.service` |
| Skip Monday window once | Touch `~/.opt/maint/window-skip-next` (window service exits early if present) |
| Tail recovery activity | `journalctl --user -u manitoba-maint-pusher.service -f` |
| Read state | `cat ~/.opt/maint/state.json` |

---

## Conventions

### Secrets

`secrets/` is gitignored. Each secret is one file with one line. Names follow `<app>.<purpose>`:

| Pattern | Example |
|---|---|
| `<app>.key` | `sonarr.key`, `prowlarr.key`, `tautulli.key` |
| `<app>.port` | `sonarr.port` (loopback port 17xxx) |
| `<app>.urlbase` | `sonarr.urlbase` (e.g. `sonarr` → `/sonarr/...`) |
| `<app>.host` | `plex.host` (Docker bridge IP if not loopback) |
| `<app>.password` | `htpasswd.password`, `qbittorrent.password` |
| Special | `kuma-push-tokens.json` (29 push tokens), `ultracc.user` (cp.ultra.cc login email) |

Full inventory in `docs/secrets-convention.md`.

### Version pinning (current policy)

**Pin policy lifted 2026-05-09.** Most apps track upstream latest; only **Tdarr** stays pinned (GLIBC 2.34 ceiling). Standing exceptions live in `versions.env` with a comment explaining *why*. The future automated-updater + rollback tool is deferred — keeping pins synced was friction without a payoff. Don't re-add pins to new installs; if you do, document the constraint.

See `Tuesday.md` for the design that extends Monday's automated upgrade sweep to all systemd-installed apps.

### Quality / ARR config

- **No 4K profiles** — every Sonarr/Radarr/Recyclarr/Jellyseerr/Plex transcoder caps at 1080p. 4K is per-case operator escalation. (`feedback_no-4k-profiles.md`)
- **TRaSH-Guides custom formats** — applied via Recyclarr to 36 Sonarr / 39 Radarr items per profile. Anime branch (Sonarr2/Radarr2) uses 55 items/profile.
- **All indexers come from Prowlarr** — manual indexers are an audit smell. `scripts/smoke/arr-audit.py` flags them.

### Networking

- Apps live in either user-systemd (`127.0.0.1:17xxx`) or Docker-bridged (`172.17.x.x:NNNN`).
- Read ports from `~/.apps/nginx/proxy.d/<app>.conf`, NOT from `config.xml` (which has the in-container default).
- Kuma runs in an isolated netns and cannot reach host loopback — that's why every monitor is **PUSH** type, fed by `manitoba-maint-pusher`.

### SSH

`scripts/lib/ssh.sh` provides `sshm` / `scpm_to` / `scpm_from` wrappers. The connection target is read from `$SSHM_HOST` (or per-script default). Important: `sshm()` detects on-host (`hostname == manitoba`) and runs commands locally with `bash -c` instead — necessary for the canary scripts which the systemd timers fire from inside the seedbox itself (no SSH-loopback authentication configured).

---

## Testing

```bash
PYTHONPATH=scripts/maint tests/.venv/Scripts/python.exe -m pytest tests/ -q
```

**202 tests** across 14 modules, all pure-Python (no live SSH, no live Kuma). Test fixtures: `tests/fixtures/manifests/` for manifest-loader edge cases, `conftest.py` for the venv path.

CI is the operator's local pytest run before deploy. Production smoke (`scripts/smoke-test.sh`) is the real gate.

---

## Ultra.cc-specific gotchas

These will bite anyone porting this to a different host:

- **Chromium SIGTRAPs** under Ultra.cc's seccomp filter — Playwright must use Firefox. (`project_cp-ultra-cc-automation.md`)
- **cp.ultra.cc has no global Apps tab** — it's per-service. Find via `a[href*="userservice"]`. The "Apps" tab itself doesn't change the URL after click; rows live at `tr[ng-repeat-start*="vma.applications"]`. Login form is AngularJS `type="email"` so pass an email, not a username.
- **Modern *arr password hashing is PBKDF2-HMAC-SHA512**, not the old SHA256. Direct DB inserts must use that or auth fails. Legacy raw-SHA256 entries auto-upgrade on first successful login.
- **`app-ports free` over-reports** — cross-check against `secrets/*.port` AND `ss -tln` before claiming a port.
- **plexapi `uuid.getnode()` triggers Plex sign-in spam** in seedbox containers — pin a fixed identifier in `~/.config/plexapi/config.ini`.
- **rotating qBit pw breaks every *arr's torrent adding silently** — every *arr's `DownloadClients.Settings` table stores the qBit password independently. Use `manitoba-maint qbit rotate-pw` which cascades.

---

## Status

- **v2 SHIPPED.** Post-v2 sweep on 2026-05-10 retired Conjurr/Newsletterr/Jellyfin/Jellystat/Notifiarr; landed qflix-newsletter (Path B Mon-08:00 weekly digest) + Buildarr (declarative *arr config nightly). The cutover newsletter send is the qflix-newsletter end-to-end smoke. See `docs/internal-app-tunnels.md` for the public/internal admin split and `docs/operator-deferred.md` for residual manual steps.
- **30 apps + 4 canaries** monitored, 29 of which have Kuma push monitors (Tdarr-Node has no kuma_monitor by design — health = systemd_only).
- **Auto-heal proven end-to-end** via deliberate `app-bazarr stop` test (recovered after 2 attempts).
- **Monday 04:00–08:00 maintenance window** automated end-to-end for the 12 UCC apps; `Tuesday.md` plans the extension to systemd apps.

---

## Pointers

| For | Read |
|---|---|
| Manual operator steps still pending | `docs/operator-deferred.md` |
| Audit the *arr stack | `python3 scripts/smoke/arr-audit.py` |
| Extending the maintenance window to systemd apps | `Tuesday.md` |
| Why a thing is the way it is | `docs/superpowers/specs/` and `docs/superpowers/plans/` |
| Recent reversible state changes | `docs/transition-log.md` |
| Ultra.cc CLI / file layout | `docs/external/ultracc-reference.md` |

---

## Non-goals

- Multi-host / k8s / cluster orchestration. This is one box.
- Multi-tenant request attribution. Jellyseerr handles per-user requests; legacy Ombi-era tags (`billy-j44`, etc.) are preserved as historical labels but not extended.
- Re-implementing Ultra.cc's UI features. The Mon-morning clicker IS the integration with cp.ultra.cc; we don't reverse-engineer the API.
- 4K media pipeline. Every quality knob is 1080p.

If this README contradicts what's in the code, the code wins. File an issue (or just edit it).
