# QFLX-15: UCC divorce (native installs + box-2 migration)

Status: design arbitrated 2026-10-09 (design panel + two judges + arbiter
rulings R1-R8). Awaiting operator review of the Operator decisions section.
Jira: epic QFLX-15, sub-tickets QFLX-16..QFLX-44.
Plan: `docs/superpowers/plans/2026-10-09-ucc-divorce-plan.md`.

## 1. Goal

Get every app off Ultra.cc's container manager (UCC, the `app-<slug>` verbs)
and onto installs the repo owns: one user unit per app, pinned versions, and
installers that run on any Linux user account. Then move the whole stack to a
second box using the same installers. All of this without:

- a member-visible outage longer than one app's swap (minutes, outside the
  Monday window);
- losing any app's identity (API keys, ports, history, library state) on the
  current box;
- weakening any existing detector, Kuma monitor, or the entitlement gate.

The work runs in two phases:

1. **Current box (Ultra).** Convert the 13 convertible UCC apps one at a time.
   Each native install takes over its container's port and data in place. The
   container stays installed but **dormant**, ready for rollback.
2. **Box 2.** Re-cut the stale `feature/migration` scripts on top of the
   native installers. Box 2 runs every app natively, Plex included with a new
   identity. Cutover is a health-gated hot swap with a rollback under one
   minute.

## 2. Arbitration summary

Three designs were produced blind and scored by two judges:

| Design | Judge "reality" | Judge "portability" | Verdict |
|---|---|---|---|
| A minimal-change | 44 | 46 | **Base (R1)** |
| B risk-first | 46 | 46 | Safety grafts (R2) |
| C portability-first | 38 | 40 | Portability grafts (R2) |

Both judges picked A: it makes the most accurate claims about the repo and has
the cheapest path. They found two load-bearing errors outside A:

- **C** claimed that changing an app's manifest class removes it from
  `app-upgrade-all.sh`. That is false. The sweep walks `~/.apps/<name>/`
  directories (`scripts/maint/app-upgrade-all.sh:6-8`) and never reads the
  manifest.
- **A's** default bind of `0.0.0.0` would publish the unauthenticated
  FlareSolverr Chromium proxy. Today FlareSolverr binds only on the Docker
  bridge (`manifest/apps.yaml` flaresolverr comment and health `hostname:
  "172.17.0.1"`; `inventory.md` flaresolverr row).

The design below is A with the mandatory grafts from both judges (R2).

## 3. Facts this design rests on (verified, with source)

| # | Fact | Source |
|---|---|---|
| F-1 | 14 apps are `class: ucc`: sonarr, sonarr2, radarr, radarr2, prowlarr, bazarr, qbittorrent, plex, seerr, tautulli, flaresolverr, sabnzbd, unpackerr, postgres | `manifest/apps.yaml` (parsed) |
| F-2 | Lifecycle routes by class: `ucc` runs `app-<ucc_slug> <verb>`, `systemd` runs `systemctl --user <verb> <unit>` | `scripts/maint/lib/lifecycle.py:113-131`, `:175-215` |
| F-3 | `_ucc_status` runs `app-<slug> status`, which is not a UCC subcommand (existing bug) | `scripts/maint/lib/lifecycle.py:118-121`; QFLX-14 inventory note |
| F-4 | Upgrade kinds already implemented: `pip_install`, `git_checkout`, `tarball_swap`, `zip_swap` (plus `ucc_update`) | `scripts/maint/lib/lifecycle.py:326-444` |
| F-5 | The UCC gate probe runs `app-<probe_app> start`. `probe_app` comes from secret `ucc.probe_app` and defaults to `sonarr` | `scripts/maint/lib/ucc.py:9-10,82,210-224` |
| F-6 | The gate probe timer fires every 5 min | `scripts/maint/systemd/manitoba-maint-ucc-detect.timer:10` |
| F-7 | The Monday sweep finds apps by walking `~/.apps/<name>/` + `app-<name> --help`. `DEFAULT_SKIP=(mariadb nginx tailscale openvpn wireguard)` | `scripts/maint/app-upgrade-all.sh:6-8,114` |
| F-8 | MCP dispatch picks its `how` string by class, so it needs no edit | `scripts/mcp/dispatch.py:246-248` |
| F-9 | Push suppression and the window lock push UP and skip probe and recovery | `scripts/maint/lib/pusher.py:140-200` |
| F-10 | The `app-ports free` filter is copy-pasted 4 times | `240-maintenance-install.sh:38`, `43-listmonk-install.sh:32`, `50-tdarr-install.sh:39`, `80-vlogs-install.sh:31` (all `scripts/configure/`) |
| F-11 | `app-nginx` is called by `60-www-images.sh` and `91-nginx-root-to-dash.sh` (plus template text) | `git grep app-nginx` |
| F-12 | Other direct `app-*` call sites: `FS_RESTART_CMD` default `app-flaresolverr restart`, `app-tautulli start` in gate-watch and in the pms-url fix | `scripts/maint/flaresolverr-canary.py:80`, `scripts/ops/tautulli-gate-watch.sh:134`, `scripts/configure/50-tautulli-pms-url-fix.sh:88` |
| F-13 | `scripts/maint/lib` and `scripts/mcp/lib` have no `__init__.py` (merged namespace packages) | `ls` on both |
| F-14 | The on-host check in ssh.sh is `hostname == manitoba` | `scripts/lib/ssh.sh:25-27` |
| F-15 | Health reads a raw `hostname` field, default `127.0.0.1` | `scripts/maint/lib/health.py:151,217` |
| F-16 | 31 tracked files contain the literal `172.17.0.1` at f2464f9 (`git grep -l`; one judge counted 47 with a wider scope). Budget the ratchet at L either way | `git grep` |
| F-17 | UCC listeners today bind loopback + public IP + `172.17.0.1` at once; FlareSolverr binds `172.17.0.1` only; Seerr reaches the arrs, and Prowlarr reaches the arrs and FlareSolverr, through `172.17.0.1` | `inventory.md:53,234-247` |
| F-18 | qbittorrent already runs as a host user unit (`qbittorrent.service` v5.0.3, no `~/.apps/qbittorrent`) while the manifest still says `class: ucc` | `inventory.md:44`, `manifest/apps.yaml` |
| F-19 | The bazarr2 native venv recipe already exists | `scripts/install/06-bazarr2.sh`, `scripts/maint/systemd/bazarr2.service` |
| F-20 | `STANDALONE_SELF_PUSH_MONITORS` is the registry for self-pushers | `scripts/maint/lib/kuma.py:169` |
| F-21 | The 240 installer is the single deploy path, including the canary loop list | `scripts/configure/240-maintenance-install.sh:572,1149` |
| F-22 | CI jobs `pytest`, `audit`, `pwsh`. Defect class C-10 requires every tracked test to run in a live job | `.github/workflows/tests.yml`, `manifest/audit-scope.yaml` |
| F-23 | The postgres upgrade child exists and passes the password in argv (a known residual) | `scripts/maint/ucc-postgres-upgrade.sh`, `scripts/maint/app-upgrade-all.sh:19-50` |
| F-24 | `parse_line` in the MCP log reader turns zone-less stamps into naive timestamps, which VictoriaLogs stored 2h early (QFLX-13 check) | `scripts/mcp/logs.py:246`; QFLX-13 journal |

Stale-branch inputs: `origin/feature/migration` holds
`docs/superpowers/specs/2026-08-08-qflix-migration-blue-green-design.md` and
`scripts/migrate/{00,10,15,20,30,35,40,45,50,55,60}-*`. Files are **copied,
never merged** (R6).

## 4. Invariants

Carried verbatim from the stale blue-green spec (section 3):

- **I-1** Exactly one side sends Discord alerts, newsletters, or Seerr emails
  at any moment. Enforced by: green's Kuma channels detached + green's
  `qflix-newsletter` / `listmonk-sync` timers not enabled until cutover.
- **I-2** No migration script writes to blue except: qBit/SAB pause+resume
  (the cutover freeze and its rollback mirror), the *arr backup-trigger POST
  (one new zip under blue's own Backups folder), newsletter/listmonk timer
  disable+enable (park blue / rollback's re-enable), and the entitlement-gate
  `--execute` drop-in removal/reinstall. Everything else touching blue is a
  read. Nothing on blue is deleted, ever, by these scripts.
- **I-3** Every mutating script defaults to dry-run and requires `--execute`.
- **I-4** Every script is idempotent and resumable. A mid-run failure is
  re-run, not hand-repaired.
- **I-5** The entitlement gate is armed on at most ONE side, ever. This design
  goes further: green ships **disarmed**, and arming it later is a separate
  operator act.

New invariants (from R2):

- **I-6 One runtime per app.** At any instant an app is served by exactly one
  process tree. A dormant UCC container must never run while its native unit
  is active. The runtime-parity leg (section 5.8) detects a violation and goes
  red.
- **I-7 Listen-set parity.** Before a swap, record `ss -tlnH sport = :P`. The
  native app reproduces that exact set of addresses. Never `0.0.0.0`.
  FlareSolverr stays on `172.17.0.1` only. A forwarder, or dropping one
  address, is allowed only as an operator-approved exception recorded in swap
  state.
- **I-8 Dormant, never uninstalled.** On Ultra, converted UCC apps stay
  installed and stopped. They are rollback targets, and Seerr's
  `seerr-<slot>` vhost is owned by the UCC seerr app.
- **I-9 Nothing wakes a dormant app.**
  - `appctl` refuses every verb except `stop` on a dormant slug.
  - `ucc.probe_app` is pinned once to `plex`, which is never converted on
    Ultra, and the `_DEFAULT_PROBE_APP` constant in `ucc.py` changes from
    `sonarr` to `plex`, because any secret read error falls back to the
    default and would otherwise run `app-sonarr start` every 5 minutes.
  - Every other call site that can start a container is re-routed through
    `appctl` or guarded: `31-unpackerr.sh` (`app-unpackerr restart || start`),
    `05-sonarr2.sh` / `07-radarr2.sh` (`app_install` runs `app-$app install`),
    and the lifecycle `ucc_update` kind (`app-<slug> stop` + `update`).
  - The `app-upgrade-all` skip list is generated from the manifest.
- **I-10 Version parity + soak.**
  - The native version equals `app-<slug> version` at swap time.
  - Built-in self-updaters are disabled.
  - Native upgrades are refused for 14 days after a swap.
  - The first native upgrade closes the rollback-to-UCC window, and that is
    recorded in swap state.
- **I-11 Inert proofs.** A proof copy boots only after `sanitize` has driven
  enabled download clients, indexers, apps-sync and notifications to 0. A
  fixture-sqlite test asserts this.
- **I-12 Fail-closed host identity.** The `host.profile` secret is explicit. A
  missing secret, or a mismatch with `detect()`, stops the run. It never
  defaults to `ultra` just because `app-ports` exists.
- **I-13 Gate safety around Seerr.** The `rail: manual` attribute is a
  per-household billing attribute, not a pause switch (it routes households to
  the frozen unknown-payer or floor branches). The Seerr swap pauses the gate
  with its real switches instead: remove the `--execute` `execute.conf`
  drop-in (or set `armed:false`), confirm `execute=False` in the next run's
  log line, and reinstall the drop-in afterwards (I-2). Never unset fields.
  The gate stays disarmed on green.

## 5. Framework

### 5.1 Layout

- **Repo:**
  - `scripts/configure/3NN-native-<slug>-install.sh`, one per app, numbered
    in swap order (300 unpackerr ... 312 postgres). Each mirrors the
    `80-vlogs-install.sh` / `43-listmonk-install.sh` shape: source `ssh.sh`,
    `log.sh` and `secrets.sh`, keep the existing port secret, pin the exact
    version, render the unit, and `enable` but do not start unless `--swap`.
  - Shared helper `scripts/lib/native.sh`.
  - Flat Python modules in `scripts/maint/lib/`: `hostpolicy.py`,
    `hostpolicy_ultra.py`, `hostpolicy_generic.py`, `ports.py`, and
    `native_sanitize.py` (which may live one level up as a CLI). **Never an
    `__init__.py` in lib** (F-13).
- **Host:**
  - Data stays in `~/.apps/<slug>`, the directory the container mounted as
    its config.
  - Binaries go in `~/.apps/<slug>/bin/<ver>` with a `current` symlink.
  - Env files live at `~/.config/qflix/<slug>.env` (0600, rendered from
    secrets).
  - Units go in `~/.config/systemd/user/qflix-<slug>.service` (manifest
    `unit:` set to match), next to bazarr2, listmonk and tdarr. The `qflix-`
    prefix avoids a collision with panel-managed units: qbittorrent already
    runs as the panel's `qbittorrent.service` (F-18), and the panel's Upgrade
    & Repair could rewrite a same-named file. The panel unit file is backed up
    before A11 and stays as the rollback target.
  - Proof copies go in `~/.apps/.prove/<slug>/`, quota-checked first and
    deleted after the verdict.
  - Swap state goes in `~/.opt/maint/swap/<slug>/`: `listen-set.before`,
    `ucc-version`, `swapped_at`, `soak_until`, `rollback_window`
    (open|closed), `exceptions`.
  - Exception: postgres uses `~/.apps/pg-native/{bin,data}` because its data
    moves by dump/restore.
- **Manifest:** a swap changes `class: ucc` to `class: systemd`, adds `unit:`
  and an `upgrade:` block, and sets `ucc_dormant: true`. The app name and
  `kuma_monitor` string never change, so Kuma, push tokens, the pusher and
  recovery need no change.

### 5.2 appctl (`scripts/lib/appctl`)

`appctl` is a bash script deployed to `~/bin/appctl` by 240.

**Verbs:** `start|stop|restart|status|version|upgrade <slug>`,
`is-native <slug>`, `ports-free`, `proxy-reload`.

**Dispatch** (the manifest is read with a short `python3 -c` over the deployed
copy at `~/.opt/maint/apps.yaml`; the repo path `manifest/apps.yaml` does not
exist on the box):

1. `class: systemd` runs `systemctl --user <verb> <unit>`. `version` reads
   `bin/current`.
2. `class: ucc` with `app-<slug>` present runs `app-<slug> <verb>`. `status`
   maps to `version` plus a port probe, never `app-<slug> status` (F-3).
3. Anything else exits 2 with `no lifecycle for <slug>`.
4. **Dormant refusal:** if the slug has `ucc_dormant: true` and the verb would
   reach UCC, every verb except `stop` exits 3 (I-9).

**Host-specific verbs:**

- `ports-free` delegates to `lib/ports.py` (section 5.6).
- `proxy-reload`: Ultra keeps today's command (`app-nginx restart`, F-11).
  Generic runs `nginx -t` (or `caddy validate`), then
  `systemctl --user reload`.

**Call sites to change:** F-11, F-12, `lifecycle._ucc_status` (F-3), the
`recovery.py` hint text, `31-unpackerr.sh:34`, `05-sonarr2.sh` /
`07-radarr2.sh` (`app_install`), and the lifecycle `ucc_update` kind. Every
re-routed call site uses the absolute `%h/bin/appctl` (`~/bin/appctl`),
including the `FS_RESTART_CMD` default, because a bare `appctl` fails with
ENOENT under the systemd `--user` default PATH.

**Unchanged:** `lifecycle.py` verbs, `recovery.py` and `mcp/dispatch.py`. The
class flip routes them (F-2, F-8).

**No PATH shadowing of `app-<slug>`.** Shadowing is fragile under systemd
PATH and hides which path ran.

### 5.3 Unit template (rendered by `native.sh render_unit`, golden-tested)

```
[Unit]
Description=QFlix <slug> (native)
After=network-online.target
[Service]
Type=simple
WorkingDirectory=%h/.apps/<slug>
Environment=PATH=%h/.apps/<slug>/bin/current:%h/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=%h/.config/qflix/<slug>.env
ExecStart=%h/.apps/<slug>/bin/current/<exe> <data-dir flag> <bind/port flags>
Restart=on-failure
RestartSec=15
StartLimitIntervalSec=600
StartLimitBurst=5
TimeoutStopSec=60        # 120 for DB apps
Nice=5
UMask=0002
[Install]
WantedBy=default.target
```

`render_unit` always emits the `Environment=PATH=` line (a golden-unit assertion
checks it) and has a per-family extra-env hook. First user: native bazarr-1
needs `BAZARR_VERSION` (bazarr reports its version from that variable;
`bazarr2-sync` reads bazarr-1's version from `/api/system/status`), written by
the installer and by the bazarr2-sync equivalent for bazarr-1. Without it the
bazarr2 pin and I-10 version parity both break. `TasksMax=` is left out unless
F6's box proof shows pids delegation under the user manager.

Thread caps go in the env file, because `ulimit -u 2000` is shared by every
process on the slot:

| Runtime | Apps | Env caps |
|---|---|---|
| .NET | arrs, prowlarr | `DOTNET_PROCESSOR_COUNT=4`, `DOTNET_gcServer=0` |
| Go | unpackerr | `GOMAXPROCS=4` |
| Node | seerr, box-2 kuma | `UV_THREADPOOL_SIZE=4`, and `--disable-wasm-trap-handler` on the CLI, never in `NODE_OPTIONS` |
| all | all | `MALLOC_ARENA_MAX=2` |

Self-update is disabled everywhere (I-10):

- arrs: `UpdateMechanism=External`
- bazarr: no-update flag
- plex (box 2): auto-update off

Logging: apps keep their own durable log files under `~/.apps/<slug>/logs`, so
vlogs-ingest and the canaries keep reading the same paths. stdout and stderr
go to the journal, which is not trusted as the only record.

### 5.4 Listen-set parity (I-7)

`scripts/ops/qflix-listen-set.sh <slug>` records the container's listen set.
It runs `ss -tlnH sport = :P` while the container is live and writes
`listen-set.before`. The native unit is rendered with exactly those addresses.

Most UCC listeners bind three addresses (F-17). Most apps accept one bind
address or `*`. Allowed resolutions, in order:

1. The app accepts several binds: reproduce them all.
2. **Exception (operator-approved, recorded):**
   - Drop the public-IP listener when all ingress goes through nginx. The
     loopback and `172.17.0.1` listeners stay.
   - Or add a tiny loopback-to-`172.17.0.1` forwarder.
3. Otherwise the app stays on UCC until box 2.

`0.0.0.0` is never an option. The runtime-parity leg compares the live set
with the recorded set after every swap and on every audit-live run.

### 5.5 Secrets and discovery contract

Today's files are kept: `secrets/<slug>.{port,key,urlbase}`, synced by 240.

**In-place swap (current box):** the installer READS the port, ApiKey and
UrlBase from the live app config (`config.xml`, `config.ini`, `config.yaml`,
`settings.json`, `sabnzbd.ini`) and asserts they equal the secrets. Any drift
aborts the swap.

**Fresh install (box 2 or a proof copy):**

1. Take the port from `ports.claim`.
2. Generate the key with `openssl rand -hex 16`.
3. Template it into the app config before the first start.
4. Write the secrets.

New host-level secrets:

- `host.profile`: `ultra` or `generic`. Required, fail-closed (I-12).
- `host.id`: also written to `~/.config/qflix/host.id` as the on-box marker
  that replaces the hostname check in `ssh.sh` (F-14). The old check stays as
  a fallback.
- `net.app_host`: `172.17.0.1` on Ultra, `127.0.0.1` on generic hosts. It
  replaces the literals (F-16). The manifest `hostname` becomes
  `hostname_ref: net.app_host` (F-15).
- `<slug>.native_version`

### 5.6 Host policy

**`hostpolicy.py`** owns:

- the loader: reads `host.profile` lazily (never at import), fails closed, and
  cross-checks `detect()`;
- the protocol: `name`, `detect()`, `windows()`, `may_operate(now)`,
  `gate_probe()`, `port_candidates()`, `proxy_reload()`, `quota()`,
  `task_ceiling()`, `docker_gateway()`, `upgrade_sweeps()`.

**`hostpolicy_ultra.py`** owns everything Ultra-specific:

- the Mon 11:00-15:00 UTC window. `window.py` is a lock orchestrator with no
  clock, so the window is NOT defined there today. It is set by timer
  `OnCalendar` lines (`manitoba-maint-window.timer`, `window-watchdog.timer`)
  and by hard-coded Monday checks in at least 9 files
  (`qflix-entitlement.py` x2, `qflix-anime-janitor.py`,
  `qflix-torrent-janitor.py`, `qflix-remux-regrab.py`, and the canaries
  `prowlarr-app-sync.sh`, `dash-asset-integrity.sh`, `plex-playback.sh`,
  `library-container-sanity.sh`). F1 adds one policy-backed
  `in_maintenance_window(now)` (or renders the timer `OnCalendar` from the
  policy), migrates every caller, and adds an audit check against new
  `weekday() == 0` literals, so window awareness stays active on Ultra (R7)
  and the generic profile's "window may be none" can actually take effect;
- the UCC gate probe, pinned to `plex` (I-9);
- `quota -p` via `scripts/canaries/quota.sh`;
- `task_ceiling` 2000;
- the docker gateway `172.17.0.1`;
- `app-ports` as the port source;
- the `proxy.d` + `app-nginx` proxy;
- `app-upgrade-all` with the generated skip list, which shrinks as apps
  convert.

**`hostpolicy_generic.py`** has:

- a window set by the operator in config (may be none);
- statvfs quota;
- `ulimit -u` or cgroup `pids.max` as the task ceiling;
- no docker gateway;
- a port range taken from config;
- only the native upgrade sweep.

Neither canaries, the entitlement code nor the reaper gains an `if ultra`
branch.

**Ports.** `lib/ports.py claim(name)` is idempotent:

- If the secret already exists, return it.
- Otherwise take the policy's candidates, minus `secrets/*.port`, minus the
  ports `ss -tlnH` shows bound.
- Write the new secret under `flock`: skip on contention, mkstemp+rename,
  never a partial file.

This replaces the four copies (F-10).

### 5.7 Upgrades

- **Native apps** get a manifest `upgrade:` block that uses the existing kinds
  (F-4):

  | Kind | Apps |
  |---|---|
  | `tarball_swap` | arrs, prowlarr, unpackerr, flaresolverr, qbittorrent static nox, seerr CI artifact, plex (box 2) |
  | `zip_swap` | bazarr |
  | `git_checkout` + pip post steps | tautulli, sabnzbd |
  | minor versions only; a major is a manual runbook | postgres |

- **No new upgrade kind and no new timer.** `app-upgrade-all.sh` skips native
  and dormant slugs and calls `manitoba-maint upgrade <slug>` for native apps
  inside the same window sweep. It writes the same `last-upgrade.json` that
  the newsletter changelog reads.
- **The skip list is generated** from the manifest at 240 deploy time
  (`class != ucc` or `ucc_dormant: true`) and joined with `DEFAULT_SKIP`. It
  is never hand-restated. This is mandatory because the sweep walks
  `~/.apps` (F-7).
- **Soak gate:** a native upgrade is refused while `now < soak_until`, which
  is 14 days and includes at least one Monday window. The refusal is recorded
  as `skipped: soak`. The first native upgrade writes
  `rollback_window: closed`. After that, rollback is the `tarball_swap`
  rollback plus a snapshot restore, never a return to UCC.

### 5.8 Runtime-parity detector (ships before swap #1)

The audit-live timer fires only at 02,08,14,20:15 UTC, so a woken container
could run up to 6h, and its red would land on the shared "QFlix Audit Live"
monitor mixed with unrelated legs. The detector is therefore split:

- **Per-minute path (the pusher, no new timer or monitor):**
  - `require_unit_active: true` in `health.py` (a revived container that
    answers 200 with stale data shows red, not green);
  - a cgroup-scoped woken-container check: any PID whose `/proc/<pid>/cgroup`
    belongs to the dormant container and whose cmdline matches the app;
  - the two-process-trees predicate, using `pgrep -u "$(id -u)"` and matching
    on the unit's `ExecStart`, never the bare `pgrep -f <pattern>` of
    `health.py`, which is not scoped to our uid and can count other tenants'
    `postgres: checkpointer` or a `tail` of the unpackerr log;
  - the port owner is not the unit's `MainPID`.
- **Audit-live leg (`scripts/maint/qflix-audit-live.py`, timer
  `manitoba-maint-audit-live`):** only the listen-set diff against
  `listen-set.before` (minus recorded exceptions).

This adds no new timer and no new Kuma monitor, so jobs.yaml C-01 and the five
240 lists are untouched.

**deploy-drift scope.** The `deploy-drift` canary compares only `*.py` and
`*.sh` under `~/scripts`. The manifest that drives the flip is deployed flat to
`~/.opt/maint/apps.yaml`, and `~/bin/appctl`, units and
`~/.config/qflix/*.env` are outside that scope, so "deploy-drift green" would
hold even if a flip was never deployed. F5 extends deploy-drift (or adds a
sibling leg) to hash `~/.opt/maint/{apps,jobs}.yaml`, `~/bin/appctl` and the
rendered units against the commit's renders.

If this ever needs its own timer, it is promoted the full way: jobs.yaml, the
five 240 lists, Kuma channels 1 and 2 (born-mute rule), and
`STANDALONE_SELF_PUSH_MONITORS` if it self-pushes.

### 5.9 Standard swap procedure

It runs on the current box, outside Mon 11:00-15:00 UTC, one app per ticket,
branch and PR. Each step is checked before the next:

1. **Pin.** Set `version = app-<slug> version`. Install, enable, do not start.
2. **Prove.**
   1. VACUUM INTO / copy the data to `~/.apps/.prove/<slug>`.
   2. Run `sanitize` and assert the zero counts.
   3. Boot on a `ports.claim` port bound to `127.0.0.1` only, with no nginx
      fragment and no Kuma monitor.
   4. Run the status and version checks, measure the proof copy's task count,
      then destroy the copy. Refuse the swap if the current
      thread-ceiling count plus the delta would reach 70% of `task_ceiling()`.
      The `Canary Thread Ceiling` canary is a named behavioural canary for
      every A-ticket.
3. **Capture** the listen set. Path-audit the config for container paths
   (`/config`, `/data`, `/downloads`). Assert and record in swap state that
   local-address auth bypass is off: arr `AuthenticationRequired=Enabled` (not
   `DisabledForLocalAddresses`), qBit `bypass_local_auth=false`, SAB `api_key`
   plus username required. (Connections reach the container today from the
   docker gateway `172.17.0.1` and will reach the native app from
   `127.0.0.1`; both are private, so the behaviour matches only if this is
   set explicitly.)
4. **Suppress** the app's Kuma monitor AND its listed behavioural canaries
   (the plan's A-table list) via `push-suppress.json`, which is keyed per app
   or canary name. The pusher pushes UP and skips recovery (F-9). Before A2,
   confirm the self-destructing `flaresolverr-unsuppress` watcher units are
   gone, or it lifts suppression as soon as port 17011 answers, mid-swap.
5. **Snapshot** the data (tar or VACUUM INTO, excluding cache and logs).
6. `app-<slug> stop`. Stop is asynchronous Docker behaviour on a box where
   `docker.sock` is denied, so before enabling the unit, poll with a timeout
   and abort the swap on failure until all three hold: no PID whose
   `/proc/<pid>/cgroup` belongs to the container and whose cmdline matches the
   app; an empty `ss -tlnH sport = :P`; and `fuser` on the db and its `-wal`
   file returns nothing. No WAL checkpoint step is needed (the native app
   replays `-wal` itself). Then
   `systemctl --user enable --now qflix-<slug>.service` on the recorded listen
   set.
7. **Manifest flip:** class systemd, `ucc_dormant: true`, `upgrade:` block.
   Ordering: branch protection means the flip PR merges first, and the swap
   runs immediately after its 240 deploy, with the manifest in a
   `pending-swap` state that `appctl` and `health` understand (no
   `class:systemd` + `require_unit_active` red while UCC still serves). Deploy
   via 240 so the generated skip list updates in the same deploy.
8. **Verify:** runtime-parity green, the app's behavioural canaries green, and
   the extended `deploy-drift` check green (it now covers the deployed
   manifest, `appctl` and units; see section 5.8).
9. **Unsuppress** the app and its canaries together and start the 14-day
   soak.

**Rollback** (any time inside the soak):

0. Re-suppress the app and its canaries via `push-suppress.json`, and run
   `systemctl --user mask qflix-<slug>.service`. Without this, the
   unit-inactive probe goes red and pusher recovery (`lifecycle.restart` ->
   `systemctl --user restart`) starts even a disabled unit, giving two
   runtimes on one SQLite file (I-6).
1. `systemctl --user disable --now qflix-<slug>.service`.
2. Revert the deployed manifest (PR + 240 deploy) BEFORE the next step.
3. `app-<slug> start`.
4. Unmask only at the next forward swap.
5. Restore the snapshot only if a migration ran. That cannot happen while the
   versions match.

The pilot (unpackerr) drills this rollback once, for real. A1 depends on the F8 ratchet (or on a test asserting zero `app-<converted>` starts), because re-running the config renderer after the pilot swap would otherwise wake UCC unpackerr.

## 6. Per-app table

Order follows R5. "Status" is the state at spec time.

| # | App | Install method | Data strategy | Proving | Swap + rollback notes | Risk | Ticket | Status |
|---|---|---|---|---|---|---|---|---|
| 1 | unpackerr (pilot) | Static Go binary from the upstream release, exact version. Config rendered from `scripts/data/unpackerr.conf.tmpl` | Stateless. Config in place, keeping the `[[general]]` TOML shape | Inert config with only a fixture `[[folder]]`, no arr sections. Never side by side against live queues | No port. `process_pattern '/unpackerr'` still matches. Rollback drilled once | low | QFLX-25 | planned |
| 2 | flaresolverr | Upstream linux x64 tarball (PyInstaller + Chromium). `HEADLESS=true`, session cap | Stateless | `ldd` on Chromium, fresh loopback port, `POST /v1` solve, task delta | Bind stays `172.17.0.1` only. `FS_RESTART_CMD` becomes `appctl`. If Chromium fails or the delta is above ~80 tasks: stays UCC on Ultra (operator) | medium | QFLX-26 | planned |
| 3 | bazarr | Release zip + venv, the same recipe as `06-bazarr2.sh` | In place, `--config ~/.apps/bazarr` | VACUUM INTO + sanitize (providers `[]`, arr sync off) | bazarr2-sync keeps pinning to bazarr-1's version | medium | QFLX-27 | planned |
| 4 | prowlarr | .NET linux-core-x64 tarball, `-nobrowser -data=` | In place (`config.xml` + db) | Sanitize Applications `SyncLevel` disabled, notifications removed. One manual search | Listen set reproduced; arrs reach it via `172.17.0.1` (F-17) | medium | QFLX-28 | planned |
| 5 | radarr2 | Shared .NET arr installer | In place | Sanitize download clients, indexers, import lists, notifications. Count parity | Queue idle first | medium | QFLX-29 | planned |
| 6 | sonarr2 | Shared .NET arr installer (Sonarr v4) | In place | As radarr2. Check `renameEpisodes=true` | Run the buildarr oneshot afterwards | medium | QFLX-30 | planned |
| 7 | radarr | Shared .NET arr installer | In place | Data-only proof | Seerr (still UCC) reaches it via `172.17.0.1`. Canary movie is the acceptance test | medium | QFLX-31 | planned |
| 8 | sonarr | Shared .NET arr installer | In place | Data-only proof | Hard prerequisite: probe pinned to plex (F-5, F-6) | medium | QFLX-32 | planned |
| 9 | sabnzbd | Source tarball + venv (sabctools). Static par2cmdline-turbo, unrar, 7zz | In place, `-f sabnzbd.ini`. Path audit of the dirs | Servers `enable=0`, fixture par2/unrar repair, and `command -v par2 unrar 7zz` under `systemd-run --user --pipe` | Pause the queue via API and re-poll. `pause_on_post_processing` stays 0. Single provider | medium | QFLX-33 | planned |
| 10 | tautulli | git tag + venv, `--datadir --nolaunch` | In place | Notifiers and newsletters off. Plex link reachable | `pms_url` keeps pointing at UCC Plex. No member activity leaves the box | low | QFLX-34 | planned |
| 11 | qbittorrent | Adopt: static userdocs qbittorrent-nox at the running version, tracked unit | Profile unchanged (system-level config, F-18) | Scratch `--profile` on a loopback WebUI port. Never two engines on one profile | Back up the panel `qbittorrent.service` file, pause all, stop and disable the panel unit, start the tracked `qflix-qbittorrent.service`, wait out the WebUI bind race | medium | QFLX-35 | planned |
| 12 | seerr | Built from source at the exact tag in a CI `debian:bullseye` job (glibc 2.31), plus Node 22 portable | In place, `CONFIG_DIRECTORY=~/.apps/seerr` | Sanitize notifications, arr servers and plex sync. Login only, never requests | Gate paused by removing the `execute.conf` drop-in (I-13), reinstalled afterwards. UCC seerr stays dormant forever (owns the vhost). Live vhost test first | high | QFLX-36 | planned |
| 13 | postgres | bullseye-pgdg debs `dpkg-deb -x`, same major | **Copy** (A13 sequence below): `pg_dumpall --globals-only` + `pg_dump -Fc`, restore into a fresh C.UTF-8 cluster | Restore on a free port, per-table row counts | Never within 24h of the newsletter. Retire `ucc-postgres-upgrade.sh` (F-23) | high | QFLX-37 | planned |
| 14 | plex | Default: **stays UCC on Ultra**, native on box 2 (deb-extracted, new identity). Spike can flip it | Box 2: fresh claim and libraries. Ultra (spike-pass only): in place | Spike: private-port bind + claim exposure | Ultra conversion only on operator go after the spike | high | QFLX-38 (spike) | decision pending |
| - | nginx | **Stays panel-managed on Ultra** (already a user unit, already in `DEFAULT_SKIP`). Box 2: our own proxy (Caddy or nginx, operator decision) | Fragments re-rendered per host | `nginx -t` / `caddy validate` + an HTTPS ingress probe | `appctl proxy-reload` | low | QFLX-41 (box 2) | Ultra: no change |
| - | uptimekuma | **Stays panel-managed on Ultra** (the observer must not move mid-migration). Box 2: native user unit | Box 2: `kuma.db` copy so push tokens stay valid | Kuma audit (manifest vs `kuma.db`) on box 2 | Never dual-posting (I-1). Push base URL flipped in one step | medium | QFLX-41 (box 2) | Ultra: no change |

**A13 postgres sequence** (replaces the standard step 6, because `pg_dump` needs
the UCC postgres running and listmonk writes continuously):

1. Stop `listmonk.service` and the `listmonk-sync` timer (the writers: the
   service, the timer and the public subscribe form).
2. From the running UCC postgres take `pg_dumpall --globals-only` (roles and
   passwords; `-Fc` does not carry them) plus `pg_dump -Fc`, and record
   per-table row counts.
3. Stop UCC postgres.
4. Restore roles and data into pg-native on `postgres.port`. The listmonk role
   and password must match `~/.apps/listmonk/etc/config.toml`.
5. Compare row counts and sequence values, then start listmonk.

Rollback restores no data: nothing writes between the dump and the cutover.

## 7. Plex variants (R3, operator decides)

- **Variant P1 (default):** Plex stays UCC on the current box, with the gate
  probe pinned to it. At box-2 cutover Plex is installed natively with a new
  identity and libraries:
  - libraries come from the existing `59-*` scripts plus Kometa;
  - 45-plex-invites mirrors blue's actual share set;
  - members re-pin through a Listmonk campaign (QFLX-42).

  The UCC gate detector stays live on Ultra until the box is retired.
- **Variant P2 (spike-backed):** only if QFLX-38 shows that PMS can bind a
  private port on the shared host and that an unclaimed instance cannot be
  claimed by a stranger. Plex then converts last, in place, with its identity
  preserved:
  1. Snapshot `Preferences.xml` and the DBs.
  2. Re-point the Tautulli, arr and Kometa Plex URLs.
  3. Move the probe to a never-converted UCC app or retire the detector.

## 8. Second-box cutover and rollback (R6)

Re-cut `scripts/migrate/` on current master by copying files from
`origin/feature/migration` (QFLX-39):

| Script | Treatment |
|---|---|
| 00-preflight | Keep, read-only. Add `host.profile`, quota, glibc and task-budget checks |
| 10-provision-checklist.md | Keep (operator checklist) |
| 15-bootstrap-new / 20-install-stack | Rewrite: run 240 + the proven `3NN-native-*` installers with `host.profile=generic`. No panel installs |
| 30-sync-media | Keep: `rsync -aH --partial`, bulk passes, then one `--delta` pass in the freeze |
| 35-sync-appdata | Slim: VACUUM INTO / `pg_dump`; re-point PUTs only on a port collision |
| 40-validate-green | Add runtime-parity, every canary in muted mode, and an assertion of gate `armed:false` |
| 45-plex-invites | Rewrite to mirror blue's per-member share SET by library name (QFLX-40) |
| 50-cutover | Health-gated flip, 8 confirmed steps, `--execute` inert by default |
| 55-rollback | Under 1 min. Mute green before anything is re-enabled on blue |
| 60-decommission-old.md | Checklist, never a script |

Every per-app list in these scripts is **generated from
`manifest/apps.yaml`**, never pasted.

Cutover steps:

1. Freeze blue (pause qBit and SAB via API).
2. Final media `--delta`.
3. Final appdata sync.
4. Validate green: every canary green while muted, gate `armed:false`.
5. Green goes loud and blue is muted (I-1). The push base URL flips in one
   step.
6. Disarm blue's gate. Green stays **disarmed** (I-5, I-13).
7. Flip the front door. This is an upstream flip if the operator un-parks a
   front proxy, otherwise members get new URLs in the re-pin campaign.
8. Park blue's newsletter and timers.

**Rollback** (55): mute green, unfreeze blue, re-enable blue's timers. Blue's
gate is re-armed only after confirming that green is disarmed. Blue stays
intact through a 14-day decommission hold (I-2).

The Listmonk re-pin template is pushed to **live Listmonk only on operator
go** (QFLX-42).

## 9. Preserved (R7)

These stay as they are:

- **Canaries** and their log paths. Data stays in `~/.apps/<slug>`.
- **Kuma monitor names and push tokens.** Every swap keeps the app name and
  `kuma_monitor`.
- **STANDALONE_SELF_PUSH_MONITORS** (F-20). No new self-pusher is planned.
- **`manifest/jobs.yaml` C-01 and the five lists in
  `scripts/configure/240-maintenance-install.sh`.** No new timer is planned.
  Any later promotion follows section 5.8.
- **One concern per module, timer and Kuma check:**
  - each app has its own installer and unit;
  - appctl only dispatches;
  - hostpolicy only answers policy questions;
  - runtime-parity is one leg with one concern.
- **`lib/window.py` Ultra window awareness**, which stays active on Ultra via
  the host policy.
- **The entitlement gate**, DISARMED on green.

## 10. Testing strategy (R8)

Every ticket carries tests and a box-proof step.

- **pytest** (run from the Bash tool, after `git add`, because the audit
  boundary is the git index). Each suite and what it asserts:

  | Suite | Asserts |
  |---|---|
  | `test_appctl` | Stub PATH that records argv: the class x verb matrix, the dormant refusal, and that `status` never emits `app-x status` |
  | `test_app_upgrade_all` | A converted slug is skipped even though its `~/.apps` dir and `app-<slug>` exist |
  | `test_ucc` | The probe never names a converted slug |
  | `test_hostpolicy` | Fails closed when the profile is missing; a profile/detect mismatch fails; the window boundaries hold with a frozen clock |
  | `test_ports` | Concurrent claims under flock never collide |
  | runtime-parity | Predicates against fixture `ss` and `systemctl` output |
  | `test_health` | `require_unit_active` |
  | `test_native_sanitize` | Fixture sqlite per app family: zero enabled clients, indexers, sync and notifications |
  | render | Golden unit files per app family |
  | `test_lifecycle` | Soak refusal |
  | audit ratchet | One positive and one negative fixture |
  | migrate | Dry-run plan contents, I-5 ordering, idempotence, generated app list equals the manifest |

- **Shell recipes:** `bash -n`, pytest subprocess tests with stub PATH, and the
  CI generic-host job (QFLX-24). That job runs ubuntu with linger and
  `systemd --user`, really installs the stateless recipes, health-checks
  them, and is registered in `manifest/audit-scope.yaml` (C-10).
- **pwsh:** only if REA fingerprint text changes. Run under both 7 and 5.1.
- **Box proof:**
  - the per-app proof copy, the swap, and the behavioural canaries after the
    swap;
  - runtime-parity green, and `deploy-drift` green, meaning box = master =
    GitHub;
  - all of it outside the Monday window.

## 11. Operator decisions (QFLX-43)

| # | Decision | Default proposed | Blocks |
|---|---|---|---|
| D-1 | Plex variant (section 7) | P1: UCC on Ultra, native on box 2 | QFLX-38 outcome, QFLX-39 |
| D-2 | Box-2 reverse proxy + TLS: user-space Caddy (ACME) vs nginx | none (operator) | QFLX-41 |
| D-3 | Soak length before the first native upgrade closes rollback-to-UCC | 14 days incl. one Monday window | QFLX-21 |
| D-4 | Listen-set exceptions per app (drop the public-IP listener vs forwarder vs stay UCC) | Drop public-IP only when ingress is nginx-only | each app ticket |
| D-5 | When, and on what evidence, the gate is armed on green | Never automatically; after N green days | post-cutover |
| D-6 | Box-2 front door (another Ultra slot vs a generic host behind a front proxy) | operator | QFLX-39, QFLX-42 |
| D-7 | FlareSolverr fallback if Chromium fails on the host | Stay UCC on Ultra, native on box 2 | QFLX-26 |
| D-8 | Required-check status of the CI generic-host job | Advisory first, required after 2 green weeks | QFLX-24 |

## 12. Risks

| Risk | Mitigation |
|---|---|
| Monday sweep or gate probe wakes a dormant container that grabs the port | Generated skip list, probe pinned to plex, appctl dormant refusal, runtime-parity leg. All ship before swap #1 (QFLX-17, -18, -20) |
| Ultra "Upgrade & Repair" or the panel restarts a stopped container | runtime-parity + `require_unit_active` go red. Dormant apps are never uninstalled, so data is consistent |
| An app can bind only one address while UCC bound three | D-4 exception, recorded in swap state, else stay UCC |
| A proof copy grabs, syncs or notifies | Sanitize + fixture tests (I-11). Loopback-only bind, no Kuma monitor |
| A schema migration breaks rollback | Exact version parity, self-update off, soak gate (I-10) |
| glibc 2.31: Seerr's Node addon, FlareSolverr's Chromium, postgres debs | Bullseye CI build for Seerr; ldd gate; pgdg bullseye debs; `version_pin.max` |
| Container-internal paths inside SAB ini or arr remote path mappings | Path audit before stop; rewrite only together with the mappings |
| Thread budget (`ulimit -u 2000`) with 13 native processes | Env caps; the swap refuses when current count + proof delta reaches 70% of `task_ceiling()` (5.9 step 2); `Canary Thread Ceiling` is a named canary for every A-ticket |
| The Seerr vhost targets the container IP, not the host port | Brief live test with the native unit before committing the swap |
| Postgres writes made after the final dump are lost on rollback | A13 sequence stops all writers before the dump, so rollback restores nothing; never near the newsletter |
| Planned swap outage pages through dependent canaries | Suppress the app plus its listed behavioural canaries together (5.9 step 4) |
| Rollback or panel restart runs UCC and native at once | Rollback step 0: re-suppress + `mask`; revert the deployed manifest before `app-<slug> start` |
| `172.17.0.1` literal sprawl (F-16) | `net.app_host` + a shrink-only audit ratchet (QFLX-23) |
| Log timestamps shift on ingest when apps move to our units | QFLX-44: per-source zone, never box-local; `_time` vs mtime check |
| Plex claim exposure on a shared host | Default P1; the spike checks it before any P2 |

Unverified items, to be probed live before relying on them:

- whether Ultra auto-restarts stopped containers;
- the Seerr vhost target;
- container paths inside the arr and SAB configs;
- the PMS listen port on a shared host;
- whether Ultra isolates tenants by network namespace or `hidepid` (decides
  how exposed a loopback or `172.17.0.1` listener is to other tenants).

## 13. Review ledger

Cross-vendor review folded in (QFLX-15). One line per finding.

| Id | Verdict | What changed |
|---|---|---|
| G-1 | partial | No unix sockets (arrs and SAB cannot use them; exposure already exists via docker-proxy, F-17). 5.9 step 3 now asserts and records local-auth-bypass off (arr, qBit, SAB); section 12 "Unverified" gains the netns/hidepid check |
| G-2 | partial | 5.9 step 2.4 refuses a swap when thread-ceiling count + proof task delta reaches 70% of `task_ceiling()`; `Canary Thread Ceiling` is a named canary for every A-ticket; `TasksMax=` left out until F6 proves pids delegation |
| G-3 | accept | `render_unit` emits `Environment=PATH=...`; all re-routed call sites use absolute `%h/bin/appctl` incl. `FS_RESTART_CMD`; golden PATH assertion; SAB `command -v par2 unrar 7zz` proof step |
| G-4 | partial | No WAL checkpoint step; 5.9 step 6 adds a polled, abort-on-failure stop check (no container PID by cgroup, empty `ss`, `fuser` on db + `-wal`) before enabling the unit |
| G-5 | reject | Linger already in effect (listmonk, tdarr, qflix-dash user units run today; F9 provisions it in CI); at most an optional `loginctl show-user -p Linger` line in box-2 preflight |
| G-6 | reject | No ownership mismatch evidence (31-unpackerr.sh already edits `~/.apps/unpackerr`); if ever added, use `find -H "$(cd ~/.apps/<slug> && pwd -P)" ! -user` and assert entries > 0 |
| G-7 | partial | New A13 sequence: stop listmonk + sync timer, `pg_dumpall --globals-only` + `pg_dump -Fc` from running UCC postgres, row counts, stop, restore roles + data, compare rows and sequences; rollback restores no data |
| G-8 | partial | Process note only, no spec change from G-8 itself; the own-findings below cover the uncovered areas (second Gemini pass optional) |
| O-1 | accept | I-13, the A12 row and plan A12 no longer say "manual rail"; use the `execute.conf` drop-in removal / `armed:false`, confirm `execute=False`, reinstall |
| O-2 | accept | Rollback gains step 0 (re-suppress + `mask`) and reverts the deployed manifest before `app-<slug> start` (I-6) |
| O-3 | accept | I-9 and 5.2 add `31-unpackerr.sh`, `05-sonarr2.sh`/`07-radarr2.sh`, lifecycle `ucc_update`; `_DEFAULT_PROBE_APP` becomes `plex`; A1 depends on F8 |
| O-4 | accept | 5.8 moves woken-container and unit-active predicates into the per-minute pusher path with cgroup-scoped, uid-scoped `pgrep` on `ExecStart`; audit-live keeps only the listen-set diff |
| O-5 | accept | 5.6 states the window lives in timer `OnCalendar` and 9+ hard-coded files; F1 adds `in_maintenance_window(now)`, migrates callers, adds an audit check against new `weekday() == 0` literals |
| O-6 | accept | 5.8 extends deploy-drift to the deployed manifest, `appctl` and units; 5.2 uses `~/.opt/maint/apps.yaml`; 5.9 step 7 fixes merge-then-deploy ordering with a `pending-swap` manifest state |
| O-7 | accept | Tracked units are `qflix-<slug>.service` (manifest `unit:` matches); panel qbittorrent unit backed up before A11; `render_unit` per-family extra-env hook (`BAZARR_VERSION`) |
| O-8 | accept | 5.9 step 4 suppresses the app plus its listed canaries, unsuppressed together at step 9; confirm the flaresolverr unsuppress watcher is gone before A2 |

The operator-decision list (section 11, D-1..D-8) is unchanged.
