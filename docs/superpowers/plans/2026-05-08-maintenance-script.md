# Manitoba Maintenance System — Implementation Plan (Phase 1)

> **For agentic workers:** Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax.

**Goal:** Ship the Phase-1 design from `docs/superpowers/specs/2026-05-08-maintenance-script-design.md` — a single Python codebase (`manitoba-maint`) on the seedbox that owns health probes, lifecycle commands, automated recovery via Kuma webhooks, and a weekly maintenance window. Phases 2 (TUI) and 3 (notifier rewrite) are out of scope per spec §8.4.

**Architecture (recap from spec):** One stdlib-only Python package with three runtime entrypoints (`webhook`, `window run`, CLI verbs), one source-of-truth manifest (`manifest/apps.yaml`), one user-systemd webhook service on `127.0.0.1`, one weekly window timer + watchdog. State lives under `~/.opt/maint/`. No nginx, no public exposure.

**Non-goals (this plan):** Textual TUI; notifier-rewrite; rollback automation; chaos testing; replacement of existing `heartbeat-*.sh` cron checks.

---

## Conventions

- `SSHM` = `ssh -o BatchMode=yes quadstronaut@seedbox.example.com`. Defined in `scripts/lib/ssh.sh`.
- All installs idempotent. Re-running a phase must not double-install or break the running service.
- **Pin versions** in `versions.env`. Phase 10 migrates `KOMETA_VERSION`, `RECYCLARR_VERSION`, `PYTHON_PLEXAPI_VERSION`, `TDARR_VERSION` from existing per-app `secrets/<app>.version` files / script constants.
- **Continuous execution** — no per-phase approval. Pause only on missing creds, smoke failure, or blocking log errors. (per `feedback_continuous-execution-preferred.md`)
- **No `git add -A`.** Stage explicitly. Never include the pre-existing dirty `scripts/configure/43-listmonk-install.sh`.
- All commits include `Co-Authored-By: Claude Opus 4.7 (1M context)`.
- Commit message format: `maint: phase N — <one-line summary> — smoke <PASS>/<TOTAL>`.

## Pre-flight (verified 2026-05-08)

| Cred / fact | Status |
|---|---|
| `secrets/uptimekuma.key` | **present** — captured 2026-05-08 |
| `secrets/uptimekuma.port` | present (Kuma's own port — NOT the maint webhook port; that's claimed at install) |
| `secrets/notifiarr.key` | present |
| Local Python | 3.14.4 (Windows controller — used to run unit tests) |
| Seedbox Python | available (used by 50+ existing scripts) |
| Existing dirty file | `scripts/configure/43-listmonk-install.sh` — DO NOT include in any commit |

---

## Repo file inventory (created across all phases)

```
manifest/apps.yaml                                              [P1]
scripts/maint/manitoba-maint                                    [P7]
scripts/maint/lib/__init__.py                                   [P1]
scripts/maint/lib/manifest.py                                   [P1]
scripts/maint/lib/state.py                                      [P2]
scripts/maint/lib/notify.py                                     [P2]
scripts/maint/lib/health.py                                     [P3]
scripts/maint/lib/lifecycle.py                                  [P4]
scripts/maint/lib/kuma.py                                       [P5,P8]
scripts/maint/lib/recovery.py                                   [P5]
scripts/maint/lib/window.py                                     [P6]
scripts/maint/systemd/manitoba-maint-webhook.service            [P9]
scripts/maint/systemd/manitoba-maint-window.service             [P9]
scripts/maint/systemd/manitoba-maint-window.timer               [P9]
scripts/maint/systemd/manitoba-maint-window-watchdog.service    [P9]
scripts/maint/systemd/manitoba-maint-window-watchdog.timer      [P9]
scripts/ops/heartbeat-maint-webhook.sh                          [P9]
scripts/configure/240-maintenance-install.sh                    [P10]
tests/__init__.py                                               [P1]
tests/conftest.py                                               [P1]
tests/run.sh                                                    [P1]
tests/unit/test_manifest.py                                     [P1]
tests/unit/test_state.py                                        [P2]
tests/unit/test_notify.py                                       [P2]
tests/unit/test_health.py                                       [P3]
tests/unit/test_lifecycle.py                                    [P4]
tests/unit/test_recovery.py                                     [P5]
tests/unit/test_kuma.py                                         [P5,P8]
tests/unit/test_window.py                                       [P6]
tests/fixtures/manifests/{valid,bad-class,duplicate-monitor,...}.yaml [P1]
tests/fixtures/kuma-payloads/{down,up,malformed}.json           [P5]
tests/fixtures/queue-samples/{empty,mixed-valid,all-invalid}.jsonl [P6]
versions.env                                                    [P10 — modified, +4 keys]
scripts/smoke-test.sh                                           [P11 — modified, +maint entries]
```

---

## Phase 1 — Manifest schema + loader (`lib/manifest.py`)

Phase deliverable: `manifest/apps.yaml` populated with every app from `secrets/<app>.port`, plus `lib/manifest.py` that loads + validates it. Pure data + parse code; no I/O against the seedbox.

### Task 1.1: Repo skeleton + test runner

- [ ] **Step 1: Create directories + empty package files**

```
scripts/maint/lib/__init__.py
scripts/maint/lib/__init__.py — empty
tests/__init__.py — empty
tests/unit/__init__.py — empty
tests/conftest.py — adds scripts/maint to sys.path
```

- [ ] **Step 2: Create `tests/run.sh`** — bootstrap a venv if missing, install pytest+pyyaml+requests, run `python -m pytest tests/unit/ -v`. Idempotent. Exits non-zero on test failure.

```bash
#!/usr/bin/env bash
# tests/run.sh — run unit tests in a local venv. Idempotent.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
if [ ! -x "$VENV/bin/python" ] && [ ! -x "$VENV/Scripts/python.exe" ]; then
  python -m venv "$VENV"
fi
PY="$VENV/bin/python"
[ ! -x "$PY" ] && PY="$VENV/Scripts/python.exe"
"$PY" -m pip install -q pytest pyyaml requests 2>/dev/null
"$PY" -m pytest "$HERE/unit/" "$@"
```

### Task 1.2: TDD — write `tests/unit/test_manifest.py` first

Test cases (write these BEFORE implementing `manifest.py`):

- [ ] `test_load_valid` — `tests/fixtures/manifests/valid.yaml` parses; resolves to N apps with non-empty `class` and `kuma_monitor` (or explicit None).
- [ ] `test_invalid_class` — `bad-class.yaml` (class: `bogus`) raises `ManifestError("unknown class")`.
- [ ] `test_duplicate_kuma_monitor` — two apps with same `kuma_monitor: "Sonarr"` raises `ManifestError("duplicate kuma_monitor")`.
- [ ] `test_missing_required_field` — app with no `class` raises.
- [ ] `test_max_version_ceiling` — manifest with `version_pin.max: 2.17.01` round-trips; loader exposes `app.upgrade.max_version`.
- [ ] `test_resolve_kuma_monitor` — `manifest.resolve_kuma_monitor("Sonarr")` returns `"sonarr"`; unknown returns `None`.
- [ ] `test_defaults_inheritance` — top-level `defaults:` propagates to apps that don't override; `defaults_override` per-app wins.

### Task 1.3: Write `manifest/apps.yaml` covering every app from `secrets/`

Apps to include (deduplicated from `secrets/*.port`):

| app | class | kuma_monitor | health.kind | port_secret | api_key_secret | urlbase_secret |
|---|---|---|---|---|---|---|
| sonarr | ucc | "Sonarr" | http_api | sonarr.port | sonarr.key | sonarr.urlbase |
| sonarr2 | ucc | "Sonarr Anime" | http_api | sonarr2.port | sonarr2.key | sonarr2.urlbase |
| radarr | ucc | "Radarr" | http_api | radarr.port | radarr.key | radarr.urlbase |
| radarr2 | ucc | "Radarr 2" | http_api | radarr2.port | radarr2.key | radarr2.urlbase |
| readarr | ucc | "Readarr" | http_api | readarr.port | readarr.key | readarr.urlbase |
| prowlarr | ucc | "Prowlarr" | http_api | prowlarr.port | prowlarr.key | prowlarr.urlbase |
| bazarr | ucc | "Bazarr" | http_api | bazarr.port | bazarr.key | bazarr.urlbase |
| qbittorrent | ucc | "qBittorrent" | http_root | qbittorrent.port | — | — |
| jellyfin | ucc | "Jellyfin" | http_api | jellyfin.port | jellyfin.key | — |
| jellyseerr | ucc | "Jellyseerr" | http_api | jellyseerr.port | jellyseerr.key | — |
| jellystat | ucc | "Jellystat" | http_root | jellystat.port | — | — |
| audiobookshelf | ucc | "Audiobookshelf" | http_api | audiobookshelf.port | audiobookshelf.key | — |
| kavita | ucc | "Kavita" | http_api | kavita.port | kavita.key | — |
| komga | ucc | "Komga" | http_api | komga.port | komga.key | — |
| calibre-web | ucc | "Calibre-Web" | http_root | calibre-web.port | — | — |
| mylar3 | ucc | "Mylar3" | http_api | mylar3.port | mylar3.key | mylar3.urlbase |
| homarr | ucc | "Homarr" | http_root | homarr.port | — | — |
| ombi | ucc | null | http_root | ombi.port | — | — |
| flaresolverr | ucc | null | http_root | flaresolverr.port | — | — |
| maintainerr | ucc | "Maintainerr" | http_api | maintainerr.port | maintainerr.key | — |
| listmonk | systemd | "Listmonk" | http_root | listmonk.port | — | — |
| conjurr | systemd | "Conjurr" | http_root | (env_file PORT) | — | — |
| newsletterr | systemd | "Newsletterr" | http_root | (env_file PORT) | — | — |
| tdarr-server | systemd | "Tdarr" | http_api | tdarr.server_port | — | — |
| tdarr-node | systemd | null | systemd_only | — | — | — |
| recyclarr | cron | null | systemd_only | — | — | — |
| kometa | cron | null | systemd_only | — | — | — |
| python-plexapi | library | null | import_check | — | — | — |

**Note:** Kuma monitor names are best-guess. If a name doesn't match the live Kuma instance, the install-time smoke (Phase 11) will surface the mismatch — fix the manifest, re-deploy.

- [ ] **Step 1:** Author `manifest/apps.yaml` matching schema in spec §4.2. Use `defaults:` block with `health_timeout_s: 5`, `recovery_attempts: 3`, `recovery_backoff_s: [10, 30, 60]`, `lifecycle_timeout_s: 60`, `kuma_recheck_delay_s: 90`.

- [ ] **Step 2:** Add 5 fixture YAMLs under `tests/fixtures/manifests/`:
  - `valid.yaml` — minimal 3-app fixture (one of each class).
  - `bad-class.yaml` — class: `bogus`.
  - `duplicate-monitor.yaml` — two apps with same `kuma_monitor`.
  - `missing-class.yaml` — app entry without `class` field.
  - `max-version.yaml` — app with `version_pin.max: 2.17.01`.

### Task 1.4: Implement `lib/manifest.py`

- [ ] Make all tests in 1.2 pass. ~150 LOC. Pure stdlib + pyyaml. Public API: `load(path) -> Manifest`, `Manifest.app(name)`, `Manifest.resolve_kuma_monitor(name)`, `ManifestError` exception class.

### Task 1.5: Phase-1 smoke

```bash
bash tests/run.sh -v -k "manifest"
# Expect: all manifest tests pass, manifest/apps.yaml validates against itself
```

Add a final test `test_real_manifest.py::test_repo_apps_yaml_loads` that loads `manifest/apps.yaml` from repo root — proves the live manifest is valid.

**Smoke gate:** `bash tests/run.sh -v -k "manifest or test_repo_apps_yaml_loads"` exits 0 with all tests passing.

**Files to commit:**
- `manifest/apps.yaml`
- `scripts/maint/lib/__init__.py`
- `scripts/maint/lib/manifest.py`
- `tests/__init__.py`
- `tests/conftest.py`
- `tests/run.sh`
- `tests/unit/__init__.py`
- `tests/unit/test_manifest.py`
- `tests/unit/test_real_manifest.py`
- `tests/fixtures/manifests/{valid,bad-class,duplicate-monitor,missing-class,max-version}.yaml`

Add `tests/.venv/` to `.gitignore` if not already covered.

---

## Phase 2 — `lib/state.py` + `lib/notify.py`

### Task 2.1: TDD — `tests/unit/test_state.py`

- [ ] `test_state_read_empty_file_returns_default` — missing file → `{}`.
- [ ] `test_state_write_creates_atomic_replace` — write goes through tempfile + os.replace; partial-write left of crash never visible.
- [ ] `test_state_record_app_event` — `state.record(app, event="recovered", attempts=1)` appends; subsequent read shows the entry.
- [ ] `test_state_corrupt_file_treated_as_empty` — file with garbage JSON → reader logs + returns `{}`, doesn't crash.

### Task 2.2: TDD — `tests/unit/test_notify.py`

- [ ] `test_notify_posts_to_notifiarr_url` — mock `requests.post`, assert URL + payload shape.
- [ ] `test_notify_failure_appends_to_notify_fail_log` — Notifiarr 500 → log line written, no exception raised.
- [ ] `test_notify_uses_secret_read_for_key` — mocks `secret_read("notifiarr.key")`.

### Task 2.3: Implement both modules

- [ ] `lib/state.py` — `read(path)`, `write(path, dict)` atomic, `record(path, app, **kwargs)` convenience.
- [ ] `lib/notify.py` — `notify(message, channel="notifiarr", level="info")`. Reads `secrets/notifiarr.key`. Failure-log path: `~/.opt/maint/notify-fail.log`.

### Phase-2 smoke

```bash
bash tests/run.sh -v -k "state or notify"
```

**Files to commit:**
- `scripts/maint/lib/state.py`
- `scripts/maint/lib/notify.py`
- `tests/unit/test_state.py`
- `tests/unit/test_notify.py`

---

## Phase 3 — `lib/health.py`

### Task 3.1: TDD — `tests/unit/test_health.py`

- [ ] `test_http_api_200_with_apikey` — mocks `requests.get` returning 200; asserts the X-Api-Key header is set, urlbase substituted.
- [ ] `test_http_api_timeout` — `requests.Timeout` → returns `HealthResult(ok=False, reason="timeout")`.
- [ ] `test_http_api_non_200` — 502 → ok=False, reason="http 502".
- [ ] `test_http_root_no_auth` — qbittorrent-style probe, no api key, expects 200/302/401 as ok (Web UI is allowed to challenge auth).
- [ ] `test_systemd_only_active` — mocks `subprocess.run("systemctl --user is-active …")` → "active\n" → ok=True.
- [ ] `test_systemd_only_inactive` — exit 3, "inactive\n" → ok=False.
- [ ] `test_port_listen` — mocks `socket.create_connection` success → ok=True; `ConnectionRefusedError` → ok=False.
- [ ] `test_import_check` — mocks subprocess running `<venv>/bin/python -c "import plexapi"` → exit 0 ok=True.

### Task 3.2: Implement `lib/health.py`

- [ ] Public API: `probe(app_record) -> HealthResult(ok: bool, latency_ms: int|None, reason: str)`. Dispatches by `app_record.health.kind`. Honors `health_timeout_s`. Resolves `port_secret`/`api_key_secret`/`urlbase_secret` via `secret_read()`.

### Phase-3 smoke

```bash
bash tests/run.sh -v -k "health"
```

**Files to commit:**
- `scripts/maint/lib/health.py`
- `tests/unit/test_health.py`

---

## Phase 4 — `lib/lifecycle.py`

### Task 4.1: TDD — `tests/unit/test_lifecycle.py`

- [ ] `test_ucc_start` — mocks `subprocess.run("app-sonarr start")` → exit 0.
- [ ] `test_ucc_restart_timeout` — process hangs > `lifecycle_timeout_s` → SIGTERM, returns failure.
- [ ] `test_systemd_start` — mocks `systemctl --user start conjurr.service`.
- [ ] `test_cron_class_has_no_start` — calling `start()` on a cron-class app raises `LifecycleError("cron class has no start")`.
- [ ] `test_library_class_has_no_lifecycle` — `start()` on a library raises.
- [ ] `test_status_reports_systemd_state` — mocks `systemctl is-active <unit>`.

### Task 4.2: Implement `lib/lifecycle.py`

- [ ] `start(app)`, `stop(app)`, `restart(app)`, `status(app)`. Dispatch on `app.class`. Wrap every subprocess call in a timeout. `upgrade()` and `downgrade()` are stubbed in this phase (return `LifecycleError("not implemented")`); concrete upgrade implementations are not part of Phase-1 of the spec — recovery only restarts.

### Phase-4 smoke

```bash
bash tests/run.sh -v -k "lifecycle"
```

**Files to commit:**
- `scripts/maint/lib/lifecycle.py`
- `tests/unit/test_lifecycle.py`

---

## Phase 5 — `lib/kuma.py` (client) + `lib/recovery.py`

### Task 5.1: TDD — `tests/unit/test_kuma.py` (client portion)

- [ ] `test_kuma_monitor_status_up` — mocks GET to Kuma API with bearer `secrets/uptimekuma.key` → `{"monitor": "Sonarr", "status": "up"}` → returns "up".
- [ ] `test_kuma_monitor_status_down` — returns "down".
- [ ] `test_kuma_unknown_monitor_returns_none` — 404 → returns None.
- [ ] `test_kuma_api_unreachable` — connection refused → returns "unknown" (best-effort, never block recovery).

### Task 5.2: TDD — `tests/unit/test_recovery.py`

- [ ] `test_recovery_succeeds_on_first_attempt` — lifecycle.start ok, health.probe ok, kuma "up" → state=`recovered`, attempts=1, notify called once.
- [ ] `test_recovery_three_failures_escalate` — all 3 attempts fail probe → state=`failed`, notify called with operator-needed message.
- [ ] `test_recovery_healthy_locally_kuma_still_down` — probe ok but kuma still "down" after `kuma_recheck_delay_s` → state=`healthy_locally_kuma_down`, notify with routing-or-auth hint.
- [ ] `test_recovery_backoff_applied` — fakes `time.sleep`; assert called with [10, 30, 60].
- [ ] `test_recovery_per_app_lock_prevents_double_recovery` — second concurrent invocation for same app blocks ≤ 120s, then drops.

### Task 5.3: Implement `lib/kuma.py` client + `lib/recovery.py`

- [ ] `lib/kuma.py` — `monitor_status(name) -> Literal["up","down","unknown",None]`. (HTTP server portion deferred to Phase 8.)
- [ ] `lib/recovery.py` — `run(app_name)`. Per-app `threading.Lock` registry.

### Task 5.4: Kuma payload fixtures

- [ ] `tests/fixtures/kuma-payloads/down.json`, `up.json`, `degraded.json`, `malformed.json` — captured from real Kuma webhook payload format ([Kuma webhook docs](https://github.com/louislam/uptime-kuma/wiki/Notification-Settings#webhook)).

### Phase-5 smoke

```bash
bash tests/run.sh -v -k "kuma or recovery"
```

**Files to commit:**
- `scripts/maint/lib/kuma.py`
- `scripts/maint/lib/recovery.py`
- `tests/unit/test_kuma.py`
- `tests/unit/test_recovery.py`
- `tests/fixtures/kuma-payloads/{down,up,degraded,malformed}.json`

---

## Phase 6 — `lib/window.py`

### Task 6.1: TDD — `tests/unit/test_window.py`

- [ ] `test_open_creates_lockfile_with_pid_and_iso` — `open(state_dir)` writes `<pid>\n<utc-iso>\n` to `lock`.
- [ ] `test_open_refuses_when_lock_present_with_live_pid` — second `open()` raises `WindowAlreadyOpen`.
- [ ] `test_open_force_overrides_when_pid_dead` — `kill(0, dead_pid)` raises ProcessLookupError → lock taken over.
- [ ] `test_drain_queue_drops_unknown_app` — `queue.jsonl` line for app not in manifest → dropped + counted.
- [ ] `test_drain_queue_blocks_max_version` — entry exceeds `version_pin.max` → dropped, counted as `blocked`.
- [ ] `test_drain_queue_defers_active_cron` — cron-class app whose `<unit>.service` is currently `active` → deferred.
- [ ] `test_smoke_runs_health_for_all_apps` — fakes `health.probe`; result is per-app dict.
- [ ] `test_close_removes_lock_writes_summary` — `close()` `os.unlink`s the lock + appends `summary` to `~/.opt/maint/window-log/<date>.log`.
- [ ] `test_watchdog_clears_stale_lock` — `lock` exists with PID that fails `kill -0` → watchdog removes lock + notifies.

### Task 6.2: Queue fixture samples

- [ ] `tests/fixtures/queue-samples/{empty,mixed-valid,all-invalid}.jsonl`.

### Task 6.3: Implement `lib/window.py`

- [ ] `WindowOrchestrator` class: `open()`, `drain_queue()`, `smoke()`, `close()`. Reads/writes via `state.py` for atomicity. Lockfile content = `f"{os.getpid()}\n{datetime.utcnow().isoformat()}Z\n"`.
- [ ] Watchdog as separate function `watchdog_clear_stale_lock(state_dir)` callable from CLI.

### Phase-6 smoke

```bash
bash tests/run.sh -v -k "window"
```

**Files to commit:**
- `scripts/maint/lib/window.py`
- `tests/unit/test_window.py`
- `tests/fixtures/queue-samples/{empty,mixed-valid,all-invalid}.jsonl`

---

## Phase 7 — `manitoba-maint` CLI dispatch

### Task 7.1: Implement `scripts/maint/manitoba-maint`

`#!/usr/bin/env python3` script, ~150 LOC argparse dispatch. Shebanged + chmod +x. Subcommands per spec §5.4:

```
manitoba-maint status [--all | <app>]
manitoba-maint start <app>
manitoba-maint stop <app>
manitoba-maint restart <app>
manitoba-maint upgrade <app> [--to <version>]      # raises NotImplementedError(phase 1: no upgrades)
manitoba-maint downgrade <app> --to <version>      # same
manitoba-maint recover <app>                       # calls recovery.run(app)
manitoba-maint window run [--dry-run] [--force]
manitoba-maint window status
manitoba-maint window watchdog                     # one-shot stale-lock check
manitoba-maint webhook                             # foreground; called by systemd unit (Phase 8)
manitoba-maint manifest validate
```

`status` renders the rich-style table from spec §5.3 using plain `print()` (rich/textual deferred to Phase 2 of design).

### Task 7.2: TDD where the dispatch logic actually lives

Most CLI behavior is wiring; high-value tests:

- [ ] `test_cli_status_calls_health_probe_for_each_app` — fakes manifest + health.
- [ ] `test_cli_recover_calls_recovery_run` — fakes recovery.
- [ ] `test_cli_manifest_validate_exits_nonzero_on_bad_manifest`.
- [ ] `test_cli_upgrade_raises_not_implemented` — Phase-1 stub.

### Task 7.3: Phase-7 smoke

```bash
bash tests/run.sh -v -k "cli"
# Plus: PYTHONPATH=scripts/maint scripts/maint/manitoba-maint manifest validate
# (uses the real manifest; should exit 0)
```

**Files to commit:**
- `scripts/maint/manitoba-maint`
- `tests/unit/test_cli.py`

---

## Phase 8 — Kuma webhook HTTP server

### Task 8.1: TDD — `tests/unit/test_kuma.py` (server portion, additive)

- [ ] `test_webhook_post_down_dispatches_recovery` — POSTs `down.json` fixture; mock recovery.run is called with `app="sonarr"`.
- [ ] `test_webhook_post_up_records_state_no_recovery` — POSTs `up.json`; recovery.run NOT called; state.record called with event=`up`.
- [ ] `test_webhook_lock_present_appends_window_event` — fakes lock present; recovery NOT called; `~/.opt/maint/window-events.jsonl` appended.
- [ ] `test_webhook_unknown_monitor_400` — POST with monitor name not in manifest → HTTP 400 + counted in daily-summary state.
- [ ] `test_webhook_malformed_json_400` — bad JSON → 400, no crash.
- [ ] `test_webhook_health_endpoint_returns_200` — GET `/health` → 200 with body `ok\n`.
- [ ] `test_webhook_runs_recovery_in_background_thread` — POST returns 200 fast (≤ 100ms); recovery thread is daemon.

### Task 8.2: Implement webhook server in `lib/kuma.py`

- [ ] `class KumaWebhookHandler(http.server.BaseHTTPRequestHandler)` + `serve(port)` function. Per-app `threading.Lock` registry shared with `lib/recovery.py`. Background thread per recovery (max in-flight cap = 5).

### Task 8.3: Integration — `manitoba-maint webhook` runs the server

- [ ] Wire `webhook` CLI verb to `lib.kuma.serve(port=int(secret_read("maintenance.port")))`.

### Phase-8 smoke

```bash
bash tests/run.sh -v -k "webhook"
```

**Files to commit:**
- `scripts/maint/lib/kuma.py` (modified — adds server portion)
- `tests/unit/test_kuma.py` (modified — adds webhook tests)

---

## Phase 9 — systemd units + heartbeat

### Task 9.1: Author 5 unit files under `scripts/maint/systemd/`

- [ ] **`manitoba-maint-webhook.service`** — `Type=simple`, `Restart=on-failure`, `RestartSec=10s`, `ExecStart=%h/scripts/maint/manitoba-maint webhook`, `WantedBy=default.target`.
- [ ] **`manitoba-maint-window.service`** — `Type=oneshot`, `ExecStart=%h/scripts/maint/manitoba-maint window run`, `TimeoutStartSec=4h+10min`.
- [ ] **`manitoba-maint-window.timer`** — `OnCalendar=Mon 11:00 UTC`, `Persistent=true`.
- [ ] **`manitoba-maint-window-watchdog.service`** — `Type=oneshot`, `ExecStart=%h/scripts/maint/manitoba-maint window watchdog`.
- [ ] **`manitoba-maint-window-watchdog.timer`** — `OnCalendar=Mon 15:00 UTC`.

### Task 9.2: Heartbeat — `scripts/ops/heartbeat-maint-webhook.sh`

Mirror `heartbeat-listmonk.sh` shape. Reads `~/.opt/maint/maintenance.port` (rendered at install) → curls `http://127.0.0.1:<port>/health` → restart service on failure → `logger -t maint-webhook-heartbeat`.

```bash
#!/usr/bin/env bash
set -uo pipefail
PORT_FILE="$HOME/.opt/maint/maintenance.port"
[ -f "$PORT_FILE" ] || { logger -t maint-webhook-heartbeat "no port file"; exit 0; }
PORT=$(tr -d '[:space:]' < "$PORT_FILE")
curl -sfm 5 "http://127.0.0.1:${PORT}/health" >/dev/null && exit 0
systemctl --user is-active manitoba-maint-webhook.service >/dev/null && exit 0
logger -t maint-webhook-heartbeat "webhook unhealthy — restarting"
systemctl --user restart manitoba-maint-webhook.service
```

### Task 9.3: Phase-9 smoke (local syntax check only — actual deploy in Phase 11)

```bash
# Validate systemd unit syntax with systemd-analyze if available locally; else just lint with grep-based checks
grep -q "^ExecStart=" scripts/maint/systemd/manitoba-maint-webhook.service
grep -q "^OnCalendar=Mon 11:00 UTC" scripts/maint/systemd/manitoba-maint-window.timer
test -x scripts/ops/heartbeat-maint-webhook.sh || die "heartbeat script not executable"
bash -n scripts/ops/heartbeat-maint-webhook.sh
```

**Files to commit:**
- `scripts/maint/systemd/manitoba-maint-webhook.service`
- `scripts/maint/systemd/manitoba-maint-window.service`
- `scripts/maint/systemd/manitoba-maint-window.timer`
- `scripts/maint/systemd/manitoba-maint-window-watchdog.service`
- `scripts/maint/systemd/manitoba-maint-window-watchdog.timer`
- `scripts/ops/heartbeat-maint-webhook.sh` (chmod +x)

---

## Phase 10 — Install script + `versions.env` migration

### Task 10.1: `scripts/configure/240-maintenance-install.sh`

Idempotent deploy script. Sources `lib/{ssh,log,secrets}.sh`. Phases:

1. **Pre-flight:** verify `secrets/uptimekuma.key`, `secrets/notifiarr.key` exist.
2. **Claim port:** if `secrets/maintenance.port` missing → `app-ports free | head -1` → `secret_write maintenance.port`.
3. **Migrate `versions.env`:**
   - `KOMETA_VERSION` ← `secrets/kometa.version`
   - `RECYCLARR_VERSION` ← `secrets/recyclarr.version`
   - `PYTHON_PLEXAPI_VERSION` ← `secrets/python-plexapi.version`
   - `TDARR_VERSION=2.17.01` (hardcoded ceiling per spec §4.3 GLIBC reason)
   - Append-only; never reorder existing keys.
4. **Sync code:** `rsync -a scripts/maint/ quadstronaut@seedbox:~/scripts/maint/`. `rsync -a manifest/apps.yaml quadstronaut@seedbox:~/.opt/maint/apps.yaml`.
5. **Render port file:** `secret_read maintenance.port > ~/.opt/maint/maintenance.port` on host.
6. **Install systemd units:** copy `*.service`/`*.timer` to `~/.config/systemd/user/`, `systemctl --user daemon-reload`.
7. **Install heartbeat:** copy `heartbeat-maint-webhook.sh` to `~/.opt/heartbeat/`. Add cron entry `*/5 * * * *` if not present.
8. **Symlink:** `ln -sf ~/scripts/maint/manitoba-maint ~/bin/manitoba-maint`.
9. **Enable + start:** `systemctl --user enable --now manitoba-maint-webhook.service manitoba-maint-window.timer manitoba-maint-window-watchdog.timer`.
10. **Install-time smoke:** see Task 10.2.

### Task 10.2: Install-time smoke (the "production gate")

Embedded at the bottom of `240-maintenance-install.sh`:

```
- webhook /health returns 200 within 5s of service-start
- manitoba-maint manifest validate exits 0
- systemctl --user is-active manitoba-maint-webhook.service → active
- systemctl --user list-timers manitoba-maint-window.timer → next-fire ≤ 7 days
- synthetic POST: curl -X POST -H 'Content-Type: application/json' \
    --data '{"monitor":"NonExistentForRoundTrip","status":"up","time":"…"}' \
    http://127.0.0.1:<port>/kuma → expect 400 (unknown monitor) AND
    state.json shows the unknown_monitor counter incremented
```

Non-zero exit on any failure. Same shape as `scripts/smoke-test.sh`.

### Task 10.3: Phase-10 smoke (locally — script syntax)

```bash
bash -n scripts/configure/240-maintenance-install.sh
test -x scripts/configure/240-maintenance-install.sh
grep -q "TDARR_VERSION=2.17.01" scripts/configure/240-maintenance-install.sh
```

**Files to commit:**
- `scripts/configure/240-maintenance-install.sh` (chmod +x)
- `versions.env` (modified — +4 keys)

---

## Phase 11 — Deploy + production smoke

### Task 11.1: Run install script against manitoba

```bash
bash scripts/configure/240-maintenance-install.sh 2>&1 | tee /tmp/maint-install.log
echo "exit=$?"
```

Expect exit 0. If the install-time smoke fails at any step, fix the underlying issue (likely a Kuma monitor-name mismatch in `manifest/apps.yaml`) and re-run; idempotency of the install script means re-running is safe.

### Task 11.2: Synthetic webhook end-to-end

```bash
PORT=$(secret_read maintenance.port)
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
sshm "curl -sf -X POST -H 'Content-Type: application/json' \
  --data '{\"monitor\":\"Listmonk\",\"status\":\"up\",\"time\":\"${NOW}\",\"msg\":\"smoke-test\"}' \
  http://127.0.0.1:${PORT}/kuma"
sleep 2
sshm "cat ~/.opt/maint/state.json | python3 -m json.tool" | grep -q '"event"'
```

**Round-trip gate:** must show a state.json entry mentioning "Listmonk" — listed because Listmonk is the only systemd-class app guaranteed to be in the operator's Kuma monitor list and a real systemd unit on the host (works as both an `up` event sanity-check and as a recovery-target exercise if needed).

### Task 11.3: Add maint smoke entries to `scripts/smoke-test.sh`

Append a new section (before the final summary):

```bash
# 14. Maintenance system
echo "14. Maintenance system"
M_PORT=$(sshm "cat ~/.opt/maint/maintenance.port 2>/dev/null" 2>/dev/null)
if [ -n "$M_PORT" ]; then
  M_HTTP=$(sshm "curl -sk -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:${M_PORT}/health" 2>/dev/null)
  case "$M_HTTP" in
    200) record "maint-webhook-up" pass "HTTP 200 on 127.0.0.1:$M_PORT" ;;
    *)   record "maint-webhook-up" fail "HTTP $M_HTTP" ;;
  esac
  M_TIMER=$(sshm "systemctl --user list-timers manitoba-maint-window.timer --no-pager 2>/dev/null | grep -c manitoba-maint-window.timer" 2>/dev/null)
  if [ "${M_TIMER:-0}" -ge 1 ]; then
    record "maint-window-timer" pass "scheduled"
  else
    record "maint-window-timer" fail "timer not scheduled"
  fi
  M_VAL=$(sshm "manitoba-maint manifest validate 2>&1; echo exit=\$?" 2>/dev/null)
  if echo "$M_VAL" | grep -q "exit=0"; then
    record "maint-manifest-valid" pass
  else
    record "maint-manifest-valid" fail "$(echo "$M_VAL" | tail -3)"
  fi
else
  record "maint-webhook-up" skip "no maintenance.port"
fi
```

### Task 11.4: Final smoke run

```bash
bash scripts/smoke-test.sh 2>&1 | tee /tmp/smoke-final.log | tail -30
```

Record PASS/TOTAL into the commit message.

**Files to commit:**
- `scripts/smoke-test.sh` (modified — +3 maint entries)

---

## Execution protocol

### Step 0 — Pre-execution check (each iteration)

- [ ] Re-read spec at `docs/superpowers/specs/2026-05-08-maintenance-script-design.md`.
- [ ] Re-read this plan.
- [ ] `git status --short` and `git log --oneline -10`.
- [ ] Find the next unchecked phase. Resume there.

### Step 1 — Per-phase loop

1. Mark the phase task in_progress.
2. Use `superpowers:test-driven-development` for new Python — tests first.
3. Implement until tests pass.
4. Use `superpowers:verification-before-completion` — run the smoke gate, capture output.
5. Stage ONLY this phase's files. Never `git add -A`. Never include `scripts/configure/43-listmonk-install.sh`.
6. Commit `maint: phase N — <summary> — smoke <PASS>/<TOTAL>`.
7. Update this plan: mark the phase done with the commit SHA.
8. Commit the plan update separately (`plan: phase N marked done — <sha>`).
9. Loop to next phase.

### Step 2 — Stop conditions

- All 11 phases marked done AND install-time smoke passes against manitoba AND synthetic Kuma POST round-trips into `~/.opt/maint/state.json`. → Final summary commit, terminate.
- 3 consecutive iterations on the same phase with no progress. → `WIP:` commit, blocker note, stop.
- Genuine blocker (missing secret beyond spec §8.3, Ultra.cc API down, manifest contradiction). → `WIP:` commit, blocker note, stop.

### Hard rules

- **Continuous execution** — no per-phase approval requests.
- **Pin every version** — `versions.env` only.
- **Webhook is loopback-only.** Never expose via nginx.
- **Never `git add -A`.** Stage explicitly.
- **Never commit secrets/\* contents.**
- **No 4K** — irrelevant to this plan but mentioned for completeness.

---

## Phase progress log

- [x] Phase 1 — manifest schema + lib/manifest.py — `acf5257`
- [x] Phase 2 — lib/state.py + lib/notify.py — `e4a8f69`
- [x] Phase 3 — lib/health.py — `c23559b`
- [x] Phase 4 — lib/lifecycle.py — `b6663ec`
- [x] Phase 5 — lib/kuma.py client + lib/recovery.py — `5f52548`
- [x] Phase 6 — lib/window.py — `0054838`
- [x] Phase 7 — manitoba-maint CLI dispatch — `f4cb7f2`
- [x] Phase 8 — Kuma webhook HTTP server — `ef2cc2f`
- [x] Phase 9 — systemd units + heartbeat — `f0a3536`
- [x] Phase 10 — install script + versions.env migration — `e548c84`
- [x] Phase 11 — deploy + production smoke — `833b112`
- [x] Phase 12 — kuma push-loop service (`pusher.py` + `manitoba-maint pusher` + systemd unit) — `4dd8bc8`
- [x] Phase 13 — kuma push monitors live, all 22 UP (bootstrap + manifest fixes for audiobookshelf/komga/maintainerr; basic-auth + path-override added to `lib/health.py`; install deploys all secrets) — `9d5aa25` (Plex added in `3487132`, `afb86cf`)
- [x] Phase 14 — `manitoba-maint kuma audit` (read-only manifest↔Kuma drift detection) — `2084aac`
- [x] Phase 15 — wire kuma drift audit into install + production smoke — `e6c10e7`
- [x] Post-15 — restart long-running services on re-deploy + `maint-kuma-all-up` smoke gate — `78e9977`
- [x] Post-15 — `tdarr-up` smoke probes loopback (server is loopback-only by design) — `8e13d94`
- [x] Phase 16 — lifecycle.upgrade/downgrade real for all 4 classes (TDD; 14 test cases incl. version_pin.max enforcement, rollback) — `559a0ae`
- [x] Phase 16 — recovery auto-downgrade after attempt-cap, gated on UCC class (4 new tests) — `08f5340`
- [x] Phase 16 — `manitoba-maint upgrade <app> [--to V]` + `downgrade <app> --to V` CLI verbs; previous_version auto-resolved from state.json — `559a0ae`
- [x] Phase 16 — Phase-15 canaries (`scripts/canaries/{movie,anime,deletion,mobile-ux}.sh`) wired into `scripts/smoke-test.sh §15`; all 4 PASS live — `f4a2522`
- [x] Phase 17 — canary Kuma Push monitors wired: `manifest/apps.yaml` gains `canaries:` section; `lib/manifest.py` exposes `Manifest.canaries()` + `Manifest.canary(name)`; `lib/kuma.py audit_monitors` includes canary monitors; `lib/cli.py` adds `canary push <name>` verb (reuses `pusher.py` token/push pattern, not duplicated); 8 new systemd units (4 `.service` + 4 `.timer`) for movie/anime/deletion/mobile-ux at hourly/hourly/04:30/every-15min schedules; `240-maintenance-install.sh` deploys + enables canary timers (idempotent); `smoke-test.sh §15b` verifies each canary's Kuma Push monitor; `tests/unit/test_canary.py` 17 new unit tests (manifest validation, push success/fail/missing-script/missing-token, kuma audit coverage) — 193 pass, 5 skip

> Phase 1-11 progress checkboxes within each phase body were rolled up into the per-phase commits and not individually toggled. The shipped artifacts are the source of truth: `scripts/maint/lib/{manifest,state,notify,health,lifecycle,kuma,recovery,window,pusher,cli}.py`, `scripts/maint/manitoba-maint`, `scripts/maint/systemd/manitoba-maint-{webhook,window,window-watchdog,pusher}.{service,timer}`, `scripts/configure/240-maintenance-install.sh`, and `scripts/smoke-test.sh` §14.

## Live state — what shipped beyond the original Phase-1 plan

**Architecture pivot:** The original spec assumed Kuma → maint webhook (Kuma POSTs alerts inbound). Phase 12-13 inverted this because Kuma 2.x runs in its own net namespace and can't reach host loopback where most apps bind. Now `manitoba-maint pusher` polls every monitored app via `lib/health.py` and POSTs status to Kuma `/api/push/<token>`. The webhook server still runs (loopback-only) for any future Kuma → maint signaling, but is not the load-bearing mechanism.

**Live monitors:** 23 (22 from manifest + Plex). Last audit: zero drift between manifest and Kuma.

**Phase 17 canary monitors:** 4 new Push-type Kuma monitors (`Canary Movie`, `Canary Anime`, `Canary Deletion`, `Canary Mobile-UX`). Token keys in `kuma-push-tokens.json` use the `canary-<name>` convention. Canary timers are in a separate notification group from app-health alerts per operator requirement.

**Production smoke as of latest deploy:** 37/38 pass. One residual fail — `recyclarr-no-4k` — is operator-deferred (3 pre-existing factory-default UHD profiles in sonarr2/radarr2; see `docs/operator-deferred.md` Phase 34).
