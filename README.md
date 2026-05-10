# Optimize-Manitoba

A reproducible, opinionated configuration of a Plex-primary media stack on a single Ultra.cc shared seedbox (`quadstronaut.seedbox.example.com`). Everything an operator needs to bootstrap, run, monitor, and self-heal the stack lives in this repo: install scripts, a single-source-of-truth manifest, a Python maintenance daemon, a Playwright-driven cp.ultra.cc upgrade clicker, a Kuma-integrated auto-recovery loop, and end-to-end canaries.

The stack is **production-grade for one operator**. State changes go through tested code paths; nothing important is left to "click around in the UI." When something does need a UI click, headless Firefox does it on a schedule.

---

## What's in the stack

**30 apps** (manifest/apps.yaml) + **4 end-to-end canaries**.

| Role | Apps | Status |
|---|---|---|
| Media servers | **Plex** (primary) | live |
| Media servers (trial) | Jellyfin | **slated for removal** — Plex-only is the direction |
| Requests + invites | Jellyseerr | migrating to **Overseerr/Seerr** (Plex-native) once Jellyfin is gone |
| Requests + invites | Ombi | parked (kept for legacy invite flows until Wizarr/alt lands) |
| TV / Movies | Sonarr, Sonarr2 (anime branch), Radarr, Radarr2 (anime) | live |
| Books / Audiobooks | Readarr | **RETIRED upstream 2026** — replacement under evaluation (Chaptarr / Bookshelf / LazyLibrarian) |
| Comics | Mylar3 | live |
| Subtitles | Bazarr | live |
| Indexer aggregator | Prowlarr (+ FlareSolverr for Cloudflare) | live |
| Torrent client | qBittorrent (single client; rTorrent / Deluge / Transmission decommissioned) | live |
| Stats + analytics | Tautulli, Jellystat | live (Jellystat will follow Jellyfin out) |
| Library / posters | Maintainerr, Recyclarr (TRaSH-Guides), Kometa | live |
| Transcoding | Tdarr (server + node, hard-pinned at 2.17.01 — GLIBC blocker) | live |
| Comms | Listmonk (mass email), Conjurr (recommendations), Newsletterr (weekly digest) | live |
| Comms | Notifiarr (Discord push) | **being purged** — replaced by direct Discord webhook |
| Reading | Calibre-Web (empty library bootstrapped at `~/media/CalibreLibrary`), Komga, Kavita, Audiobookshelf | book/audiobook/comic apps about to be parked pending Readarr replacement |
| Dashboard | Homarr (private + public boards, "Qflix" theme) | live |
| Monitoring | Uptime Kuma + manitoba-maint daemon | live |

**4 canaries** (`scripts/canaries/`) probe whole pipelines, not just liveness:
- **movie** (hourly) — Jellyseerr request → Radarr grab → qBit
- **anime** (hourly) — same, anime branch (Sonarr2)
- **deletion** (daily 04:30) — Maintainerr 60-day rule audit
- **mobile-ux** (every 15 min) — Homarr public board renders, < 512KB HTML, root domain redirects 302

---

## Decisions in flight

Living list of architectural changes the operator has decided on but not yet executed. These will land across upcoming sessions.

| # | Change | Why |
|---|---|---|
| 1 | **Purge Notifiarr → Discord webhook direct** | Notifiarr's passthrough integration was silently failing (HTTP 400 — `pass through integration disabled` on operator's account, possibly Patron-only). Direct Discord webhook removes a middleware layer with no functional loss. |
| 2 | **Replace Readarr** (Chaptarr / Bookshelf / LazyLibrarian) | Readarr was retired upstream 2026 — repo archived, metadata source dead. |
| 3 | **Investigate Decypharr + Real-Debrid pipeline** | Decypharr is a media gateway that lets *arrs use Real-Debrid as a near-instant download client (qBit-API-compatible). Could replace qBit for cloud-streaming workflow. |
| 4 | **Purge Jellyfin** | Plex is canonical; Jellyfin trial concluded. Jellystat follows it out. |
| 5 | **Migrate Jellyseerr → Overseerr/Seerr** | Without Jellyfin, Jellyseerr's Jellyfin-fork advantage disappears. Overseerr is the Plex-native original. |
| 6 | **Wire Recyclarr fully** (Kuma monitor + maintenance-window upgrade) | Currently installed as cron with `kuma_monitor: null`. Make it visible + auto-upgraded. |
| 7 | **Park book/comic/audiobook apps** until Readarr replacement is chosen | Readarr is the ingestion piece; reader apps (Calibre-Web, Komga, Kavita, Audiobookshelf, Mylar3) are useful only when there's a fresh ingestion pipeline. |
| 8 | **Implement #2 decision** | Once Readarr replacement is picked, install + uninstall Readarr. Affects manifest, Calibre-Web library wiring, and Phase 16 stop-list. |
| 9 | **Stremio-feeding-into-Plex** (Real-Debrid + Zurg/Decypharr + Plex symlinks) | Same architectural answer as #3 — Decypharr + RD makes Stremio's debrid catalog visible to Plex as local files. |
| 10 | **Confirm Maintainerr replaces Deleterr** | Yes — Maintainerr is the same author's evolution of Deleterr; rule-based deletion + handles Plex collections. No Deleterr install needed. |

---

## Architecture in one diagram

```
                         Operator workstation (Windows)
                                      │
                                      │ ssh / scp / Playwright
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
  │     ├─ window    (Mon 04:00–08:00 lock + Notifiarr open/close)      │
  │     └─ canary-*  (timers — fire scripts/canaries/*.sh)              │
  │                                                                     │
  │   ~/scripts/maint/cp_upgrade_clicker.py   (Mon 04:30 — Playwright   │
  │     Firefox → cp.ultra.cc → Upgrade & Repair on 12 UCC apps)        │
  │                                                                     │
  └─────────────────┬───────────────────────────────────────────────────┘
                    │ /metrics (Basic auth, push-token URLs)
                    ▼
        Uptime Kuma (isolated netns: 127.0.0.1:42005 INSIDE container)
        29 PUSH monitors + 1 webhook notification target
```

The "isolated netns" detail matters: Kuma cannot reach the host's loopback. Pusher pushes status TO Kuma. Auto-heal originally relied on Kuma webhook IN — but Kuma can't POST to host-loopback either, so recovery is now triggered directly from the pusher when its probe fails. See `scripts/maint/lib/pusher.py`.

---

## Repo layout

```
manifest/apps.yaml           # 30 apps + 4 canaries — single source of truth
versions.env                 # Pinned versions (Tdarr only — pin policy lifted 2026-05-09)
Tuesday.md                   # Design doc — extending Mon maintenance to systemd apps
                             # NB: bookmarks/EDGEbookmarks.html are gitignored — operator keeps a local copy outside the repo

docs/
  operator-deferred.md       # Manual steps that can't be scripted yet
  transition-log.md          # Reversible state-changes log (stop/start/uninstall)
  secrets-convention.md      # ~/secrets/ inventory + filename rules
  arr-audit-2026-05-09.md    # Most recent *arr stack audit
  arr-audit-actions-*.md     # Audit punch-list
  external/ultracc-reference.md  # Ultra.cc CLI / file-layout cheat-sheet
  superpowers/               # Plan + spec docs (longer-form designs)

scripts/
  configure/                 # 44 phased install/configure scripts (numbered 01..61)
                             # Run in numeric order on a fresh seedbox.
  install/                   # Lower-level installer libs (used by configure/)
  lib/                       # Shared bash helpers: ssh, log, secrets, pwgen
  data/                      # Static config: kuma-qflix*.css, prowlarr indexer JSON, unpackerr template
  ops/                       # Cron-friendly heartbeat scripts per long-running app
  smoke/                     # Read-only audits + one-shot fixes (arr-audit, arr-audit-fixes)
  plex/                      # Plex-specific utilities (kill_stream, stream_stats)
  post-import/               # Library-rescan callbacks invoked by *arrs after import
  canaries/                  # 4 end-to-end pipeline checks (bash)
  smoke-test.sh              # Production smoke (193+ checks across the whole stack)
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
      notify.py       # Operator-escalation ping (Discord webhook direct; Notifiarr passthrough deprecated)
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
  notifiarr_channel: "#notifiarr"
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
| `manitoba-maint-window.timer` | Mon 04:00 | window open: lock + Notifiarr ping |
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

Smoke target as of v2: **193+ checks pass**, covering Prowlarr indexers, *arr→qBit reachability, Notifiarr round-trip, Plex/Jellyfin libraries, Maintainerr rules, canary monitor presence, etc.

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
| `<app>.key` | `sonarr.key`, `prowlarr.key`, `notifiarr.key` |
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

- **No 4K profiles** — every Sonarr/Radarr/Recyclarr/Jellyseerr/Jellyfin transcoder caps at 1080p. 4K is per-case operator escalation. (`feedback_no-4k-profiles.md`)
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

- **v2 SHIPPED.** See `docs/operator-deferred.md` for the residual operator-only items (Newsletterr template UI, Listmonk cutover send, Phase H walkthrough).
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
