# QFLX-15: UCC divorce implementation plan

Spec: `docs/superpowers/specs/2026-10-09-ucc-divorce-design.md` (the section
numbers below refer to it).
Epic: QFLX-15. Every item is its own ticket, its own `QFLX-N-slug` branch and
its own PR. A PR merges only when `pytest`, `audit` and `pwsh` are green.
After each merge the box is deployed to the same commit, and the
`deploy-drift` canary must be green.

Rules for every ticket:

- Run `git add -A` before `pytest` and `scripts/maint/qflix-audit.py`. The
  audit boundary is the git index.
- Run pytest from the Bash tool.
- No box operations Mon 11:00-15:00 UTC.
- No member data in the repo. Tests assert counts only.
- App swaps run strictly one at a time. Swap N+1 starts only when swap N is
  green, and never within the Monday window. A soak blocks only native
  upgrades, not the next swap.

The "standard swap" and "standard rollback" are defined in spec section 5.9.

## Order and dependencies

```
F1 QFLX-16 ─┬─ F3 QFLX-18 ─┬─ F5 QFLX-20 ─┐
            │              └─ F8 QFLX-23  │
            ├─ F4 QFLX-19                 ├─ F6 QFLX-21 ─ F9 QFLX-24
F2 QFLX-17 ───────────────────────────────┤
F7 QFLX-22 ───────────────────────────────┤
                                          └─ A1 QFLX-25 (pilot, rollback drill)
A1 ─ A2 QFLX-26 (needs F8) ─ A3 QFLX-27 ─ A4 QFLX-28 ─ A5 QFLX-29 ─ A6 QFLX-30
   ─ A7 QFLX-31 ─ A8 QFLX-32 ─ A9 QFLX-33 ─ A10 QFLX-34 ─ A11 QFLX-35
   ─ A12 QFLX-36 ─ A13 QFLX-37
A14 QFLX-38 (Plex spike; needs F6; any time, outside the window)
M5 QFLX-43 (operator decisions; any time; blocks the tickets listed in spec section 11)
M6 QFLX-44 (vlogs timestamps; after QFLX-13)
A13 + all F ─ M1 QFLX-39 ─ M2 QFLX-40 ─ M4 QFLX-42
                         └ M3 QFLX-41 (needs D-2)
```

Gates before swap #1 (QFLX-25): QFLX-17, QFLX-18, QFLX-20 and QFLX-21 must be
merged and deployed, with the runtime-parity leg green on the box.

## Tickets

### Framework (ship first)

| # | Key | Title | Size | Depends |
|---|---|---|---|---|
| F1 | QFLX-16 | Fail-closed host.profile + flat hostpolicy modules + on-box marker | M | - |
| F2 | QFLX-17 | Monday-sweep + gate-probe safety (generated skip list, probe_app pinned to plex) | M | - |
| F3 | QFLX-18 | `scripts/lib/appctl` with dormant refusal + fix `lifecycle._ucc_status` | M | F1 |
| F4 | QFLX-19 | `lib/ports.py claim()` under flock replaces 4 app-ports filters | S | F1 |
| F5 | QFLX-20 | Runtime-parity leg + swap state + `require_unit_active` | M | F3 |
| F6 | QFLX-21 | `native.sh` installer lib, unit template, version parity + soak gate | M | F1, F5 |
| F7 | QFLX-22 | Per-app sanitize with fixture-sqlite tests | M | - |
| F8 | QFLX-23 | `net.app_host` + shrink-only audit ratchet (`app-<x>`, `172.17.0.1`) | L | F1, F3 |
| F9 | QFLX-24 | CI generic-host job (systemd --user + linger) | M | F6 |

**F1 QFLX-16 Host policy**

- **Scope:**
  - Add `scripts/maint/lib/hostpolicy.py`, `hostpolicy_ultra.py` and
    `hostpolicy_generic.py` as flat modules, with no `__init__.py`.
  - Add the `host.profile` secret. It is read lazily, a missing secret fails
    closed, and it is cross-checked against `detect()`.
  - `lib/window.py` asks the policy for the window.
  - `ssh.sh _sshm_on_host` uses the `~/.config/qflix/host.id` marker, with the
    hostname check as a fallback.
- **Acceptance:** no behaviour change on Ultra.
- **Tests:**
  - a missing profile or a profile/detect mismatch exits 2;
  - with a frozen clock, Mon 11:00-14:59 UTC is refused and 15:00 is allowed;
  - ssh.sh marker subprocess test.
- **Box proof:**
  1. Deploy via 240.
  2. Write `host.profile=ultra`.
  3. Run the window dry-run plus one canary through `sshm`. All green.
- **Rollback:** revert the PR. The secret is inert to the old code.

**F2 QFLX-17 Sweep + probe safety**

- **Scope:**
  - `app-upgrade-all.sh` gets an explicit skip list, generated from the
    manifest at deploy time: slugs with `class != ucc` or `ucc_dormant`.
  - Pin the `ucc.probe_app` secret to `plex`. `ucc.py` refuses to probe a
    converted or dormant slug.
- **Acceptance:** a converted slug is skipped even when its `~/.apps` dir and
  `app-<slug>` exist.
- **Tests:**
  - a stub-PATH subprocess test of the sweep with a fixture manifest;
  - a `test_ucc` guard.
- **Box proof:**
  - `app-upgrade-all.sh --dry-run` shows the generated skip list;
  - the ucc-detect run shows the probe on plex;
  - the ucc-gate-stuck canary is green.
- **Rollback:** revert the PR and restore the old probe secret.

**F3 QFLX-18 appctl**

- **Scope:**
  - Verbs `start|stop|restart|status|version|upgrade|is-native|ports-free|proxy-reload`.
  - Dispatch by class.
  - The dormant refusal: every verb except `stop` exits 3.
  - `status` becomes version plus a port probe, which fixes F-3.
  - Re-route the call sites listed in spec F-11 and F-12, and the
    `recovery.py` hint text.
- **Acceptance:** no direct `app-<x>` call remains outside the F8 allowlist.
- **Tests:** `test_appctl` stub-PATH argv matrix, the dormant refusal, and that
  `status` never emits `app-x status`.
- **Box proof:** `appctl version` and `appctl status` for every UCC app match
  `app-<slug> version`.
- **Rollback:** revert the PR.

**F4 QFLX-19 ports.claim**

- **Scope:**
  - `ports.claim(name)` is idempotent and runs under flock, writing with
    mkstemp+rename.
  - Candidates come from the policy, minus `secrets/*.port`, minus the ports
    `ss -tlnH` shows bound.
  - Replace the filters in 240, 43, 50-tdarr and 80.
- **Tests:**
  - with fake `app-ports` and `ss` output, an existing secret is returned;
  - concurrent claims never collide.
- **Box proof:** re-run `80-vlogs-install.sh`. The port is unchanged.
- **Rollback:** revert the PR.

**F5 QFLX-20 Runtime parity**

- **Scope:**
  - `scripts/ops/qflix-listen-set.sh` captures the listen set and writes swap
    state.
  - Add a runtime-parity leg in `qflix-audit-live.py` that alarms on a woken
    dormant container, 2 PIDs, a port owner that is not the `MainPID`, or a
    listen set that differs from the recorded one.
  - `health.py` gets `require_unit_active`.
  - No new timer and no new monitor.
- **Tests:**
  - fixture predicates for each failure mode;
  - `test_health` for `require_unit_active`.
- **Box proof:**
  - audit-live is green;
  - capture the listen sets of all 14 UCC apps, record only. These seed
    spec D-4.
- **Rollback:** revert the PR.

**F6 QFLX-21 native.sh + soak**

- **Scope:**
  - `fetch_verify`, `install_versioned` and `render_unit`, with the thread
    caps from spec section 5.3.
  - The installer refuses to proceed unless the target version equals
    `app-<slug> version`.
  - Self-update is off.
  - `lifecycle` upgrade refuses while `now < soak_until`.
  - The first native upgrade writes `rollback_window: closed`.
- **Tests:**
  - golden units per family;
  - `bash -n`;
  - soak refusal and the window-close write.
- **Box proof:** install a throwaway unit, enabled but not started, and run
  `systemd-analyze --user verify`.
- **Rollback:** revert the PR.

**F7 QFLX-22 Sanitize**

- **Scope:**
  - `native_sanitize` covers the families arr, prowlarr, bazarr, seerr,
    tautulli and sab.
  - It works on copies only and refuses a live `~/.apps/<slug>` path.
- **Tests:**
  - fixture sqlite and ini files per family; after sanitize, enabled download
    clients, indexers, apps-sync and notifications are all 0;
  - the live-path refusal.
- **Box proof:** sanitize a VACUUM INTO copy of `prowlarr.db` under
  `~/.apps/.prove/`, check that the counts are 0, then delete the copy.
- **Rollback:** revert the PR.

**F8 QFLX-23 net.app_host + ratchet**

- **Scope:**
  - Replace the `172.17.0.1` literals (31 tracked files at f2464f9) with
    `secret_read net.app_host` or `hostname_ref`.
  - Add an audit detector with a shrink-only allowlist for raw
    `app-<x>`/`app-ports`/`app-nginx` and `172.17.0.1`.
  - REA fingerprint strings are allowlisted as text.
- **Tests:**
  - positive and negative detector fixtures;
  - growing the allowlist fails.
- **Box proof:** dry runs of `30-seerr-arrs.py` and `03-prowlarr-flaresolverr.sh`
  show no diff.
- **Rollback:** revert the PR.

**F9 QFLX-24 CI generic host**

- **Scope:**
  - Add a job to `.github/workflows/tests.yml`: ubuntu with linger and
    `systemd --user`, `host.profile=generic`.
  - It installs and health-checks unpackerr, flaresolverr and an empty-data
    prowlarr.
  - Register the job in `manifest/audit-scope.yaml`.
- **Acceptance:** the job is green, and the generic render contains no
  `172.17.0.1` and no `app-` calls.
- **Tests:** the job itself, plus a pytest that checks the audit-scope
  registration.
- **Box proof:** n/a, since this is a CI-only proof. Confirm the box
  installers are unchanged.
- **Rollback:** remove the job.

### App conversions (sequential, R5)

Each one is a standard swap plus the app-specific items below. Every app
ticket has the same four parts:

- **Tests:** a golden unit, the sanitize fixture for its family, and the
  manifest test (a systemd app with native metadata has an installer, a pin
  and an upgrade block).
- **Box proof:** the proof copy, then the swap, then runtime-parity plus the
  listed canaries green.
- **Rollback:** the standard rollback.
- **Acceptance:** green for 24h after the swap, with no runtime-parity alarm.

| # | Key | App | Size | Depends | App-specific scope | Behavioural canaries |
|---|---|---|---|---|---|---|
| A1 | QFLX-25 | unpackerr (pilot) | S | F2, F3, F5, F6 | Inert fixture-folder proof; **rollback-to-UCC drilled once, timed** | Kuma Unpackerr; one import end to end |
| A2 | QFLX-26 | flaresolverr | M | A1, F8 | Bind `172.17.0.1` only; ldd + task-delta gate; `FS_RESTART_CMD` via appctl; D-7 fallback | prowlarr-proxy-link-fatal, prowlarr-indexer-health |
| A3 | QFLX-27 | bazarr | S | A2, F7 | Reuse the `06-bazarr2.sh` recipe; config path audit | bazarr-ingest |
| A4 | QFLX-28 | prowlarr | S | A3 | First .NET; `SyncLevel` disabled in the proof | prowlarr-app-sync, prowlarr-indexer-health |
| A5 | QFLX-29 | radarr2 | S | A4 | Shared arr installer; queue idle | anime, hardlink-integrity, library-container-sanity |
| A6 | QFLX-30 | sonarr2 | S | A5 | `renameEpisodes` true; buildarr oneshot afterwards | anime, arr-plex-parity |
| A7 | QFLX-31 | radarr | S | A6 | Low-request hour | movie |
| A8 | QFLX-32 | sonarr | S | A7 | Pre-swap assert `probe_app != sonarr` | arr-plex-parity, seerr-arr-parity, ucc-gate-stuck |
| A9 | QFLX-33 | sabnzbd | M | A8 | par2/unrar/7zz; ini path audit; pause/re-poll | sab-stall + one real download |
| A10 | QFLX-34 | tautulli | S | A9 | `pms_url` unchanged; privacy | tautulli-plex-link |
| A11 | QFLX-35 | qbittorrent (adopt) | M | A10 | Static nox; panel unit disabled; WebUI bind race | qbit-stall, arr download-client test |
| A12 | QFLX-36 | seerr | L | A11 | CI bullseye build; gate paused via the manual rail; vhost live test; UCC seerr dormant forever | movie, anime, seerr-arr-parity, entitlement-service |
| A13 | QFLX-37 | postgres | M | A12 | dump/restore; retire `ucc-postgres-upgrade.sh` + its sweep child; never within 24h of the newsletter | Listmonk health, subscriber count parity |
| A14 | QFLX-38 | Plex spike | S | F6 | Private-port bind, claim exposure, task delta; written verdict for D-1 | n/a (5-min scratch boot) |

### Box 2 and closing items

| # | Key | Title | Size | Depends |
|---|---|---|---|---|
| M1 | QFLX-39 | Re-cut `scripts/migrate/` on master, generated from the manifest, hot-swap cutover | L | A13, all F |
| M2 | QFLX-40 | 45-plex-invites mirrors blue's share set; green gate disarmed (I-5) | M | M1 |
| M3 | QFLX-41 | Box-2 native Kuma + our own reverse proxy/TLS | M | M1, D-2 |
| M4 | QFLX-42 | Listmonk re-pin campaign template (live Listmonk on operator go) | S | M2 |
| M5 | QFLX-43 | Operator decision checklist (spec section 11) | S | - |
| M6 | QFLX-44 | VictoriaLogs timestamp integrity for zone-less stamps | S | QFLX-13 |

**M1 QFLX-39**

- **Scope:** copy files only from `origin/feature/migration`. Script by script
  treatment is in spec section 8. Per-app lists are generated from the
  manifest, and I-1..I-5 are enforced in code.
- **Tests:**
  - stub-PATH dry-run plan containing every step;
  - I-5 ordering;
  - an idempotent re-run gives no diff;
  - the generated app list equals the manifest.
- **Box proof:**
  1. 00-preflight, read-only, on the current box.
  2. A full dry run against box 2.
  3. A 55-rollback drill before the real cutover.
- **Rollback:** `55-rollback.sh`.

**M2 QFLX-40**

- **Scope:**
  - Mirror the share SET by library name.
  - Tagalongs get Plex only.
  - Re-check invites via `/api/invites/requested`.
  - Green entitlement is `armed:false` with no execute drop-in, and 40 asserts
    it.
- **Tests:** count-only fixtures for set equality, tagalong exclusion and the
  `armed:false` assertion.
- **Box proof:** a dry run, where planned invites equal the blue share count.
- **Rollback:** revoke the green invites.

**M3 QFLX-41**

- **Scope:**
  - Kuma on box 2 as a user unit, with a copy of `kuma.db` so the push tokens
    stay valid.
  - `lib/kuma.py` reads the tunnel and db path from secrets.
  - The proxy (D-2) renders the same fragments, with seerr as a vhost.
  - Ultra Kuma and nginx are unchanged (R4).
- **Tests:**
  - proxy render golden plus a config test;
  - kuma path-from-secret.
- **Box proof (box 2):** the Kuma audit matches, and HTTPS reaches every app.
- **Rollback:** discard. Box 2 is not live before M1.

**M4 QFLX-42**

- **Scope:** a dry-run-default push script for the re-pin campaign. "Seerr"
  wording.
- **Tests:** the script requires `--execute`.
- **Box proof:** an operator preview, and a test send to the operator only.
- **Rollback:** delete the draft.

**M5 QFLX-43**

- **Scope:** record D-1..D-8 in spec section 11.
- **Tests and box proof:** n/a.

**M6 QFLX-44**

- **Scope:**
  - `parse_line` applies a declared per-source zone (default UTC), never
    box-local.
  - Owned emitters log `Z`.
  - A check that ingested `_time` is close to the file mtime.
- **Tests:** `parse_line` fixtures with zone-less, `Z` and `+0200` stamps;
  exactly-once ingest unchanged.
- **Box proof:** run listmonk-sync and the ingest; `_time` equals `date -u`.
- **Rollback:** revert the PR and re-ingest the window.

## Sizes

| Size | Count | Tickets |
|---|---|---|
| S | 13 | F4, A1, A3-A8, A10, A14, M4, M5, M6 |
| M | 13 | F1, F2, F3, F5, F6, F7, F9, A2, A9, A11, A13, M2, M3 |
| L | 3 | F8, A12, M1 |

Total: 29 sub-tickets under epic QFLX-15.
