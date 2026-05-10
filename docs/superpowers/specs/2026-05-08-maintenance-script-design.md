# Manitoba Maintenance System — Design Spec

**Date:** 2026-05-08
**Owner:** Quadstronaut (operator@example.com)
**Target:** `quadstronaut@seedbox.example.com` (Ultra.cc seedbox)
**Status:** Draft — pending operator review

---

## 1. Goals & non-goals

**Primary goals**

- One Python codebase (`manitoba-maint`) that owns three responsibilities for every managed app: **health probes**, **lifecycle commands** (start/stop/restart, plus class-aware upgrade/downgrade where possible), and **automated recovery** when something breaks unexpectedly.
- An interactive operator surface (CLI subcommands now, full-screen Textual TUI in a later phase) that calls the same code paths as the unattended automation, so behavior is identical between "operator runs `manitoba-maint restart sonarr`" and "Kuma webhook triggers a recovery".
- A weekly **maintenance window** Mon 04:00–08:00 AZ (= 11:00–15:00 UTC, 240 minutes) that holds a lockfile, drains a notifier-written upgrade queue, runs a health-only smoke test, and pings Notifiarr at open + close.
- A **Kuma webhook receiver** on `127.0.0.1:<port>` that turns up/down events into automated 3-attempt recovery (with backoff), then escalates to the operator if recovery fails or if local health passes but Kuma still reports down.
- One source-of-truth dispatch table (`manifest/apps.yaml`) for every app's class, unit name, kuma monitor name, health endpoint, and version pin.

**Explicit non-goals**

- No version-pin enforcement on Ultra.cc-managed apps (`app-<name>` always installs upstream-latest; "pin" only applies to apps we install ourselves).
- No rollback automation. Downgrade is supported only where the upgrade procedure is symmetric (binary swap with a previous-version cache).
- No retry-on-Notifiarr-failure persistent queue. Alerts that miss because Notifiarr is down are missed.
- No chaos testing. The system is small enough that manual integration tests against listmonk are sufficient ground-truth.
- No replacement of the existing `scripts/ops/heartbeat-*.sh` cron checks. They keep firing every 5 minutes; the maintenance system is additive.
- No public exposure of the webhook receiver (loopback only — Kuma is on the same host).
- No textual TUI in v1. CLI subcommands ship first; TUI is Phase 2.

**Constraints inherited from project memory**

- Manitoba is shared Ultra.cc infrastructure: no root, no sudo. Everything lives under `quadstronaut`'s uid.
- Three app binding modes (user-systemd loopback, Docker loopback, Docker docker0-gateway). Port discovery via `~/.apps/nginx/proxy.d/<app>.conf` for proxied apps; via `secrets/<app>.port` files populated by `bootstrap-discover.sh`.
- All third-party install pins go through `versions.env` (current state: 3 pins; this spec migrates 4 more from `secrets/<app>.version` and 1 from a script constant).
- 1080p ceiling is a media-quality rule, irrelevant to this spec, mentioned only because the apps.yaml schema must not invite "4K profile" knobs.

---

## 2. Decisions log

Locked-in answers to the brainstorming questions:

| # | Question | Decision |
|---|---|---|
| 1 | Pin handling for UCC apps | Observe-and-recover only. No rollback. Modified version notifier is the upgrade-driver (out of scope for this spec); maintenance script is the recovery brain. |
| 2 | Manifest source-of-truth | Single `manifest/apps.yaml` at repo root. |
| 3 | Interactive UI shape | CLI subcommands as foundation (`manitoba-maint <verb> [app]`); full-screen Textual TUI added in Phase 2 on the same code paths. |
| 4 | Webhook hosting | User-systemd Python service on `127.0.0.1:<port>`, NOT exposed via nginx. Kuma reaches it loopback. |
| 5 | Window behavior | Lockfile + drain queue + health-only smoke + 240-min hard ceiling + Notifiarr ping at open and close. |
| 6 | Window length | 4 hours (Mon 04:00–08:00 AZ = 11:00–15:00 UTC). |
| 7 | Heartbeat retrofit | Not needed — `systemctl restart` of an already-(re)starting service is harmless, and the 240-min window gives apps plenty of time to settle. |

---

## 3. Architecture

Three runtime entry-points into one Python codebase, plus an in-repo manifest.

### 3.1 In-repo layout

```
manifest/apps.yaml                                     # canonical dispatch table
scripts/maint/manitoba-maint                           # Python CLI entrypoint
scripts/maint/lib/{manifest,health,lifecycle,recovery,kuma,notify,window,state}.py
scripts/maint/systemd/manitoba-maint-webhook.service
scripts/maint/systemd/manitoba-maint-window.service
scripts/maint/systemd/manitoba-maint-window.timer
scripts/maint/systemd/manitoba-maint-window-watchdog.service
scripts/maint/systemd/manitoba-maint-window-watchdog.timer
scripts/configure/240-maintenance-install.sh           # deploy + enable
scripts/ops/heartbeat-maint-webhook.sh                 # mirrors existing heartbeat-*.sh pattern
tests/{unit,integration,fixtures}/                     # see §7
versions.env                                           # extended with 4 more pins
```

### 3.2 On-host layout

```
~/scripts/maint/manitoba-maint                                   # symlinked from ~/bin/manitoba-maint
~/scripts/maint/lib/*.py
~/.opt/maint/apps.yaml                                           # rendered from repo at install time
~/.opt/maint/lock                                                # present iff window is open
~/.opt/maint/queue.jsonl                                         # version notifier appends; window-orchestrator drains
~/.opt/maint/state.json                                          # last health/probe/fail per app
~/.opt/maint/window-events.jsonl                                 # Kuma events received during a window
~/.opt/maint/window-log/<YYYY-MM-DD>.log                         # per-window run log
~/.opt/maint/notify-fail.log                                     # alerts that didn't reach Notifiarr
~/.config/systemd/user/manitoba-maint-webhook.service            # always running
~/.config/systemd/user/manitoba-maint-window.{service,timer}     # oneshot + Mon 11:00 UTC timer
~/.config/systemd/user/manitoba-maint-window-watchdog.{service,timer}  # Mon 15:00 UTC stale-lock cleaner
```

### 3.3 Three entrypoints, one binary

1. `manitoba-maint webhook` — long-running HTTP listener on `127.0.0.1:<port>` (user-systemd service, `Restart=on-failure`).
2. `manitoba-maint window run` — fired once weekly by the timer; opens lockfile, drains queue, smoke-tests, closes lockfile, pings.
3. `manitoba-maint <verb> [app]` — synchronous CLI for the operator.

**Why one binary:** shared manifest loader + health + lifecycle + state. The webhook recovery flow and the operator's `manitoba-maint restart sonarr` go through identical code paths. What you test interactively is exactly what runs at 04:00 AM.

### 3.4 Timezone handling

systemd timer's `OnCalendar=Mon 11:00 UTC` is the canonical schedule. Arizona has no DST (UTC-7 year-round), so this is a fixed offset forever — no host TZ touched.

---

## 4. Components

### 4.1 Module-level responsibilities

```
manitoba-maint                 ← argparse dispatch (~150 LOC)
lib/manifest.py                ← load + validate apps.yaml; resolve port from secrets/nginx
lib/health.py                  ← probe kinds: http_api | http_root | systemd_only | port_listen
lib/lifecycle.py               ← per-class start/stop/restart/upgrade/downgrade
lib/recovery.py                ← 3x restart with backoff, post-restart re-probe, Kuma re-check, escalate
lib/window.py                  ← lockfile lifecycle, queue drain, smoke runner, open/close pings
lib/kuma.py                    ← stdlib http.server, parses Kuma webhook payload, dispatches to recovery
lib/notify.py                  ← thin Notifiarr client (existing notifiarr.key); Discord-bound
lib/state.py                   ← state.json read/write (atomic via tempfile + os.replace)
```

Pure stdlib + `requests` + `pyyaml` for runtime. Test-only deps: `pytest`, `pytest-cov`. No Flask, no FastAPI. `textual` and `rich` arrive in Phase 2.

The webhook service binds to `127.0.0.1:$(secret_read maintenance.port)` — port claimed via `app-ports free` at install time, mirroring the listmonk/conjurr/newsletterr port-claim pattern.

The `kuma_recheck_delay_s` and `lifecycle_timeout_s` defaults are overridable per-app under `apps.<app>.defaults_override`.

### 4.2 `apps.yaml` schema (load-bearing contract)

```yaml
defaults:
  health_timeout_s: 5
  recovery_attempts: 3
  recovery_backoff_s: [10, 30, 60]
  lifecycle_timeout_s: 60
  notifiarr_channel: "#notifiarr"
  kuma_recheck_delay_s: 90        # how long to wait after local-recovery before checking Kuma's view

apps:
  sonarr:
    class: ucc                                      # ucc | systemd | cron | library
    ucc_slug: sonarr                                # → app-sonarr {start|stop|restart}
    kuma_monitor: "Sonarr"                          # exact monitor name in Uptime Kuma
    health:
      kind: http_api                                # http_api | http_root | systemd_only | port_listen
      path_template: "/{urlbase}/api/v3/system/status"
      auth_header: "X-Api-Key"
      auth_secret: "sonarr.key"
      port_secret: "sonarr.port"
      urlbase_secret: "sonarr.urlbase"
      expect_status: 200

  conjurr:
    class: systemd
    unit: conjurr.service
    kuma_monitor: "Conjurr"
    health:
      kind: http_root
      port_source: "env_file:~/.apps/conjurr/repo/env/.env:PORT"
      expect_status: 200
    upgrade:
      kind: git_checkout
      repo_path: "~/.apps/conjurr/repo"
      version_pin: { source: versions.env, key: CONJURR_VERSION }
      post_steps:
        - "cd ~/.apps/conjurr/repo && .venv/bin/pip install -r requirements.txt"

  tdarr-server:
    class: systemd
    unit: tdarr-server.service
    kuma_monitor: "Tdarr"
    health:
      kind: http_api
      path_template: "/api/v2/status"
      port_source: "json_file:~/.apps/tdarr/configs/Tdarr_Server_Config.json:serverPort"
      expect_status: 200
    upgrade:
      kind: zip_swap
      url_template: "https://storage.tdarr.io/versions/{version}/linux_x64/Tdarr_Server.zip"
      target_dir: "~/.apps/tdarr/Tdarr_Server"
      version_pin:
        source: versions.env
        key: TDARR_VERSION
        max: "2.17.01"
        max_reason: "GLIBC 2.34 required, host has 2.31"

  recyclarr:
    class: cron
    unit: recyclarr.timer                            # we check the timer, not a service
    kuma_monitor: null                               # not monitored by Kuma (no port to probe)
    health:
      kind: systemd_only
      expect: active
    upgrade:
      kind: tarball_swap
      url_template: "https://github.com/recyclarr/recyclarr/releases/download/{version}/recyclarr-linux-x64.tar.xz"
      target_path: "~/.apps/recyclarr/bin/recyclarr"
      version_pin: { source: versions.env, key: RECYCLARR_VERSION }

  tdarr-node:
    class: systemd
    unit: tdarr-node.service
    kuma_monitor: null                               # piggybacks on tdarr-server's monitor
    health:
      kind: systemd_only
      expect: active
    upgrade:
      kind: zip_swap
      url_template: "https://storage.tdarr.io/versions/{version}/linux_x64/Tdarr_Node.zip"
      target_dir: "~/.apps/tdarr/Tdarr_Node"
      version_pin:
        source: versions.env
        key: TDARR_VERSION                           # shares pin with tdarr-server
        max: "2.17.01"
        max_reason: "GLIBC 2.34 required, host has 2.31"

  python-plexapi:
    class: library
    unit: null                                       # no service
    kuma_monitor: null                               # no port
    health:
      kind: import_check
      venv_python: "~/.apps/python-plexapi/venv/bin/python"
      module: "plexapi"
    upgrade:
      kind: pip_install
      venv_python: "~/.apps/python-plexapi/venv/bin/python"
      package: "plexapi"
      version_pin: { source: versions.env, key: PYTHON_PLEXAPI_VERSION }
```

### 4.3 Per-app upgrade procedures (real, not placeholders)

| app | class | source-of-truth | upgrade procedure | restart |
|---|---|---|---|---|
| **listmonk** | systemd | `versions.env:LISTMONK_VERSION` | dl tarball → replace `bin/listmonk` → run `--install --idempotent --yes` (schema migration) | `systemctl --user restart listmonk.service` |
| **conjurr** | systemd | `versions.env:CONJURR_VERSION` | `git fetch && git checkout <tag>` → `.venv/bin/pip install -r requirements.txt` | `systemctl --user restart conjurr.service` |
| **newsletterr** | systemd | `versions.env:NEWSLETTERR_VERSION` | same as conjurr + `playwright install chromium` if cache empty | `systemctl --user restart newsletterr.service` |
| **tdarr-server** | systemd | `versions.env:TDARR_VERSION` (NEW pin) | dl `Tdarr_Server.zip` from storage.tdarr.io → unzip in place. **HARD CEILING 2.17.01** (`max_version` in manifest, GLIBC reason). | `systemctl --user restart tdarr-server.service` |
| **tdarr-node** | systemd | inherits `TDARR_VERSION` | dl `Tdarr_Node.zip` → unzip in place | `systemctl --user restart tdarr-node.service` |
| **kometa** | cron | `versions.env:KOMETA_VERSION` (migrated from `secrets/kometa.version`) | `git fetch --tags && git checkout <tag>` → pip install requirements | none — next timer fire uses new code |
| **recyclarr** | cron | `versions.env:RECYCLARR_VERSION` (migrated from `secrets/recyclarr.version`) | dl tarball → replace `bin/recyclarr` | none — next timer fire uses new binary |
| **python-plexapi** | library | `versions.env:PYTHON_PLEXAPI_VERSION` (migrated from `secrets/python-plexapi.version`) | `~/.apps/python-plexapi/venv/bin/pip install plexapi==X` | none — consumers pick up at next invocation |

### 4.4 Three app classes drive lifecycle dispatch

| class | start/stop/restart | upgrade | health |
|---|---|---|---|
| **ucc** | `app-<slug> {start,stop,restart}` | not applicable (driven by external version notifier) | http probe per manifest |
| **systemd** | `systemctl --user {start,stop,restart} <unit>` | per-app upgrade-fn from manifest | http probe or systemd-active |
| **cron** | n/a (timer-driven) | per-app upgrade-fn from manifest | `systemctl --user is-active <timer>` |
| **library** | n/a | `pip install pkg==X` in shared venv | n/a |

### 4.5 Version-source consolidation (one-time migration)

Three pin-conventions live in the repo today; the maintenance system needs one. Migration done in `240-maintenance-install.sh`:

- Add to `versions.env`: `KOMETA_VERSION`, `RECYCLARR_VERSION`, `PYTHON_PLEXAPI_VERSION` (read from existing `secrets/<app>.version` files), `TDARR_VERSION=2.17.01` (read from `50-tdarr-install.sh` constant).
- Leave `secrets/<app>.version` files in place (don't break the existing install scripts), but `versions.env` becomes authoritative for the maintenance system. A future cleanup can remove the duplicates after install scripts migrate to read `versions.env`.

---

## 5. Data flow

### 5.1 Webhook recovery (the self-healing path)

```
Uptime Kuma (on manitoba)
  └─ monitor "Sonarr" goes DOWN
       └─ POST http://127.0.0.1:<webhook-port>/kuma
              {monitor: "Sonarr", status: "down", msg, time}
                 │
                 ▼
manitoba-maint webhook (always-running user-systemd service)
  ├─ load apps.yaml → resolve kuma_monitor "Sonarr" → app entry "sonarr"
  ├─ check ~/.opt/maint/lock
  │    │
  │    ├─ LOCK PRESENT ──► append to ~/.opt/maint/window-events.jsonl
  │    │                   RETURN 200 (no recovery — apps may be mid-upgrade)
  │    │
  │    └─ LOCK ABSENT ──► spawn recovery worker, RETURN 200 immediately
  ▼
recovery_worker(app="sonarr"):
  for attempt in 1..3:
    lifecycle.start(app)               # class-aware
    sleep(backoff_s[attempt])          # [10, 30, 60]
    if health.probe(app) == OK:
       sleep(kuma_recheck_delay_s)     # 90s — let Kuma re-probe
       if kuma.monitor_status("Sonarr") == "up":
          state.record(app, recovered, attempts=attempt)
          notify("✓ {app} recovered after {attempt} attempt(s)")
       else:
          state.record(app, healthy_locally_kuma_down, attempts=attempt)
          notify("⚠️ {app} healthy locally but Kuma still reports down — likely routing/auth")
       return
  state.record(app, failed, attempts=3)
  notify("✗ {app} could not be started after 3 attempts — operator needed")
```

### 5.2 Maintenance window (Mon 11:00–15:00 UTC)

```
manitoba-maint-window.timer fires at Mon 11:00 UTC
  ▼
manitoba-maint window run                   # systemd oneshot, owns the window
  ├─ touch ~/.opt/maint/lock with content "<pid>\n<utc-start-iso>\n"
  ├─ notify("🔧 maintenance window opened (closes by 15:00 UTC)")
  ├─ drain ~/.opt/maint/queue.jsonl:
  │     for each {app, target_version, enqueued_at}:
  │       if app not in apps.yaml      → drop, log "unknown app"
  │       if app cron-class & .service active → defer to next window
  │       if target_version > apps.yaml max_version → drop, log "max-version block"
  │       else → lifecycle.upgrade(app, target_version), log result
  ├─ run health-only smoke probe across every app in apps.yaml
  ├─ build summary: {N upgrades attempted, M succeeded, K still down, D dropped}
  ├─ rm ~/.opt/maint/lock
  └─ notify("✅ window closed: N↑ M✓ K✗ D⊘ — see ~/.opt/maint/window-log/<date>.log")

manitoba-maint-window-watchdog.timer fires at Mon 15:00 UTC
  └─ if lock present + start-timestamp > 4h ago + kill -0 <pid> fails:
       rm lock; notify("⚠️ window watchdog cleared stale lockfile (PID {pid} unresponsive)")
```

### 5.3 Operator interactive (`manitoba-maint status`)

```
$ manitoba-maint status [--all | <app>]
  ├─ load apps.yaml + secrets/* → resolved app records
  ├─ for each app (parallel via threadpool):
  │     health.probe(app) → {status, latency_ms, detail}
  │     state.read(app)   → {last_recovered_at, fail_count}
  └─ render table (rich-style, but pure print() in v1):
        APP            CLASS    STATUS         LATENCY  LAST RECOVERY
        sonarr         ucc      ✓ healthy      42ms     —
        conjurr        systemd  ✓ healthy      8ms      —
        listmonk       systemd  ✗ http 502     —        2026-05-06 11:14 (auto-recovered)
        recyclarr      cron     ✓ timer-active —        —
        tdarr-server   systemd  ✗ unit-dead    —        3 attempts at 14:21, FAILED
```

### 5.4 CLI verb surface (v1)

```
manitoba-maint status [--all | <app>]                     # health snapshot
manitoba-maint start <app>                                # class-aware lifecycle
manitoba-maint stop <app>
manitoba-maint restart <app>
manitoba-maint upgrade <app> [--to <version>]             # systemd|cron|library only; UCC refuses
manitoba-maint downgrade <app> --to <version>             # binary-swap classes only
manitoba-maint recover <app>                              # manual invocation of the 3x flow
manitoba-maint window run [--dry-run] [--force]
manitoba-maint window status                              # lock present? since when?
manitoba-maint webhook                                    # foreground; called by systemd unit
manitoba-maint manifest validate                          # config-lint
```

### 5.5 File layout — single writer per file

```
~/.opt/maint/
├── lock                          # window orchestrator: touch on open, rm on close
├── queue.jsonl                   # version notifier appends; drainer reads + truncates
├── state.json                    # webhook-recovery + status-cmd write; everyone reads
├── window-events.jsonl           # webhook appends Kuma events received during a window
├── notify-fail.log               # alerts that didn't reach Notifiarr
└── window-log/
    └── 2026-05-11.log            # one per window run
```

**Concurrency:**
- Per-app `threading.Lock` in webhook server prevents double-restart on near-simultaneous Kuma events for the same app.
- `state.json` writes go through `tempfile + os.replace()` (atomic on POSIX).
- Lockfile content includes PID + UTC start time; readers do `kill -0 <pid>` staleness check.

---

## 6. Error handling

Failure-mode-driven, not exception-soup. Each failure has a named guardrail.

| # | Failure mode | Guardrail |
|---|---|---|
| 1 | Webhook receiver dies | `heartbeat-maint-webhook.sh` cron `*/5`, restarts the service. Notifiarr alert on second consecutive failure (silence transients). |
| 2 | Lockfile leak (orchestrator crash mid-window) | Watchdog timer at Mon 15:00 UTC + stale-PID check (`kill -0`) on every read. Removes lock + alerts on cleanup. |
| 3 | apps.yaml validation failure on startup | Webhook service refuses to start (`Restart=on-failure` with backoff). Operator sees precise error in journal. |
| 4 | Recovery succeeds locally but Kuma still down | Wait `kuma_recheck_delay_s` (90s default), re-check Kuma's API, route to `healthy_locally_kuma_down` notify branch. Requires `secrets/uptimekuma.key`. |
| 5 | `app-<name> start` or `systemctl restart` hangs | Wrap every lifecycle call in `lifecycle_timeout_s` (60s default). On timeout: SIGTERM, count as failed attempt. |
| 6 | Notifiarr unreachable | Best-effort, never blocks recovery. Failures append to `~/.opt/maint/notify-fail.log`. |
| 7 | Kuma sends malformed JSON / unknown monitor | HTTP 400 + log + drop. Unknown monitor names aggregated to one daily Notifiarr ping (prevents spam from misconfigured Kuma). |
| 8 | state.json corruption | Atomic writes prevent it. On read failure: log, treat as empty, continue. |
| 9 | Two recovery threads race for same app | Per-app `threading.Lock`, second arrival blocks (120s timeout, then drops). |
| 10 | Operator runs `window run` during active window | Refuses with `window already in progress (PID=X started=Y); use --force to override`. |
| 11 | ~~Heartbeat retrofit race~~ | ~~Dropped — `systemctl restart` of an already-(re)starting service is harmless.~~ |
| 12 | GLIBC / max-version ceiling violation | `lifecycle.upgrade()` checks `version_pin.max` first. Refuses with reason. tdarr blocked at 2.17.01. |
| 13 | Cron-class app currently mid-run when upgrade fires | Upgrade fn checks `systemctl --user is-active <unit>.service` (the oneshot, not timer). If active: defer to next window. |
| 14 | Disk full mid-upgrade | Each upgrade-fn checks `df ~/.apps` for ≥ 500 MB free before starting. Fails fast. |
| 15 | Notifier enqueues unknown app | Drainer validates each entry against apps.yaml. Dropped + logged + counted in window summary. |

**One explicit non-goal:** No retry-on-Notifiarr-failure persistent queue. If Notifiarr is down, alerts are missed — Notifiarr-down is itself something Kuma will catch separately.

---

## 7. Testing

Three layers, scaled to the cost of getting each one wrong.

### 7.1 Unit tests (pytest, run on controller, no SSH)

- `test_manifest.py` — load every fixture in `tests/fixtures/manifests/` (valid, missing-secret, bad-class, duplicate-kuma-monitor, max-version-ceiling); assert validator catches each.
- `test_health.py` — mock `requests` + `subprocess.run`; assert each probe kind interprets responses correctly. Includes timeout, non-200, connection-refused.
- `test_recovery.py` — mock `lifecycle.start` + `health.probe` + `notify`; assert the 3-attempt-with-backoff loop fires exactly 3x on persistent failure, exits early on success, and routes to the `healthy_locally_kuma_down` branch.
- `test_window.py` — fixture queue.jsonl with mixed entries (1 unknown, 1 max-blocked, 3 valid); assert orchestrator drops the bad two, attempts the three good, writes correct summary.
- `test_kuma_webhook.py` — POST fixtures of real Kuma payloads (down, up, degraded, malformed); assert dispatch + HTTP responses.
- Coverage gate: `≥ 80%` on `lib/`, enforced by `pytest --cov`.

### 7.2 Integration tests (gated on `MAINT_INTEGRATION=1`)

- `test_recover_listmonk.sh` — SSH stops listmonk, fires synthetic Kuma webhook at the live receiver, asserts state.json shows `recovered`, asserts listmonk is `is-active`, asserts Notifiarr was called (mock endpoint via env var).
- `test_window_dryrun.sh` — runs `manitoba-maint window run --dry-run`: walks the queue + smoke-test paths but skips `lifecycle.upgrade`. Verifies orchestration without touching apps. Safe any day.
- `test_max_version_block.sh` — enqueues a fake `tdarr → 2.71.01`, runs window run --dry-run, asserts blocked + logged + not attempted.

### 7.3 Production smoke (every install)

`240-maintenance-install.sh` ends with: webhook `/health` returns 200 + window timer `next-fire ≤ 7 days` + manifest validates + a synthetic webhook hits the receiver and gets logged. Non-zero exit on any failure — same shape as `scripts/smoke-test.sh`.

### 7.4 Test fixtures

```
tests/
├── fixtures/
│   ├── manifests/         # YAML: valid + 6 invalid variants
│   ├── kuma-payloads/     # JSON: down / up / degraded / malformed
│   └── queue-samples/     # JSONL: empty / mixed-valid / all-invalid
├── unit/
└── integration/
```

### 7.5 Explicit non-goals

- No chaos-style "kill random apps and assert recovery" — too noisy for 27 apps, false positives erode trust.
- No mocked Kuma server. Synthetic payloads + one real integration test on install.
- No load test. Webhook handles ~50 events/week; stdlib `http.server` is fine.

---

## 8. Deployment artifacts

### 8.1 New repo files

- `manifest/apps.yaml` — initial dispatch table covering every app currently in `secrets/<app>.port`.
- `scripts/maint/manitoba-maint` (Python entrypoint).
- `scripts/maint/lib/{manifest,health,lifecycle,recovery,kuma,notify,window,state}.py`.
- `scripts/maint/systemd/*.{service,timer}` (5 unit templates).
- `scripts/configure/240-maintenance-install.sh` — deploys all of the above + creates `~/.opt/maint/` + enables systemd units + runs install-time smoke.
- `scripts/ops/heartbeat-maint-webhook.sh` — mirrors existing heartbeat-*.sh pattern.
- `tests/{unit,integration,fixtures}/` — see §7.

### 8.2 Modified repo files

- `versions.env` — add `KOMETA_VERSION`, `RECYCLARR_VERSION`, `PYTHON_PLEXAPI_VERSION`, `TDARR_VERSION` (migrated from existing sources).

### 8.3 Secrets needed

- Existing: `notifiarr.key`, `htpasswd.password` (no new secrets needed for those).
- **`secrets/uptimekuma.key`** — read-only API token for Kuma. **Already captured 2026-05-08** (operator-provided). Install script just verifies presence and reachability; no manual prompt needed.
- **New: `secrets/maintenance.port`** — claimed via `app-ports free` at install time, like listmonk/conjurr/newsletterr.

### 8.4 Phased implementation (drives the writing-plans output)

- **Phase 1 (this spec):** manifest + lib/ + CLI subcommands + webhook receiver + window timer + heartbeat-maint-webhook + version migration. Shippable maintenance system end-to-end.
- **Phase 2 (out of this spec):** Textual full-screen TUI as a thin layer on the Phase-1 CLI.
- **Phase 3 (out of this spec):** Modified Application Version Notifier — rewrites Ultra.cc's `Ultra-Version-Notifier/main.sh` to enqueue upgrades to `~/.opt/maint/queue.jsonl` instead of pinging Discord.

---

## 9. Open questions / future work

1. **Uptime Kuma API key capture** — operator must capture from Kuma UI on first install. The install script will prompt; we don't auto-generate.
2. **Notifier rewrite (Phase 3)** — needs its own design pass; may want richer queue entries (severity, requested-window, etc.).
3. **TUI (Phase 2)** — Textual layout and keybindings deferred to a Phase-2 design; the Phase-1 CLI is the API surface that TUI binds to.
4. **Backup of `~/.opt/maint/`** — state.json + window-log have audit value. Add to the deferred backup-tooling spec when that lands (per project memory: backup tooling is its own deferred session).
5. **Downgrade UX** — `manitoba-maint downgrade <app> --to X` works for binary-swap classes (listmonk, tdarr binaries, recyclarr binary). For git-checkout classes (conjurr, newsletterr, kometa), it works iff the older tag is fetchable. UCC apps cannot be downgraded; CLI refuses with a precise error.

---

## 10. Operator review

Operator: please confirm or flag changes to:

- Section 2 decisions log (these are locked; raising them again means spec rewrite).
- Section 4.3 per-app upgrade table — particularly the `tdarr 2.17.01` ceiling and the `versions.env` consolidation list.
- Section 8.3 new secrets — `secrets/uptimekuma.key` is the only operator-touch dependency for install.
- Section 8.4 phased split — Phase 1 is what this spec ships; Phases 2/3 are explicitly later.

Once approved, this spec drives `writing-plans` for the implementation plan.
