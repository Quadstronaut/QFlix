# QFlix MCP — Design Spec

**Date:** 2026-05-12
**Status:** Draft — pending operator approval
**Author:** brainstorm with Claude (Opus 4.7, 1M context)

## 1. Goal

Replace ad-hoc SSH-based farm inspection with a structured tool surface
that Claude (and the operator) can call via MCP. The same scripts that
serve the MCP also run as cronjobs on the seedbox, so analysis,
diagnosis, and self-healing are driven by one code path with one set
of behaviors.

The MCP is **token-cheap by design**: a Windows scheduled task collects
an hourly time-series snapshot to local disk (`B:\QFlix\data\`); MCP
read tools serve from that local cache, never re-querying the farm.
Only write tools (unstick, force-collect, missing-search) round-trip to
the seedbox.

The system is **autonomously reactive**: if a torrent shows zero
progress across 3 consecutive hourly snapshots and matches a stale rule,
the collector unsticks it immediately (DELETE from *arr queue with
removeFromClient=true + blocklist=true; *arr auto-researches). Actions
are logged + posted to Discord without @-pinging the operator.

## 2. Non-goals

- Cross-machine / multi-user MCP exposure. Single-workstation only.
- Replacing `arr-housekeeping.py --unstick` (the hourly `:15` cron that
  handles >6h stuck-import). That stays. The MCP's autonomous unstick
  catches the 3-hour-zero-movement case; `--unstick` catches the
  stuck-import case. Different signals, different windows, both kept.
- Generic log aggregation. We tail named app logs, not all syslog.

## 3. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│ Windows workstation (B:\QFlix\data\ is source of truth)        │
│                                                                │
│  Task Scheduler ─ \Archangel\QFlix\Hourly Collect              │
│    └── qflix-collect.ps1                                       │
│          ├── ensures \Archangel\Manitoba SSH Tunnel running    │
│          ├── ssh ... python3 ~/scripts/mcp/collect.py          │
│          ├── ssh ... python3 ~/scripts/mcp/logs.py             │
│          ├── walk last 3 snapshots → stale-state.json          │
│          ├── ssh ... python3 ~/scripts/mcp/unstick.py (auto)   │
│          ├── append events, prune retention                    │
│          ├── push Kuma "QFlix Collect (workstation)"           │
│          └── post Discord summary (no @ping)                   │
│                                                                │
│  Claude Code MCP ─ qflix-mcp (stdio)                           │
│    ├── 8 read tools  → read B:\QFlix\data\                     │
│    └── 3 write tools → ssh ... python3 ~/scripts/mcp/<x>.py    │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│ Seedbox (seedbox.example.com, quadstronaut@)                      │
│                                                                │
│  ~/scripts/mcp/                                                │
│    ├── collect.py     read-only snapshot                       │
│    ├── unstick.py     DELETE+blocklist for one queue item      │
│    ├── logs.py        tail app logs                            │
│    ├── plex.py        Plex library + sessions                  │
│    ├── missing.py     fire MissingSearch on all *arrs          │
│    └── lib/           shared (imports scripts/maint/lib/* too) │
│                                                                │
│  systemd-user                                                  │
│    └── qflix-missing-search.timer @ 07:00 UTC (=00:00 Phoenix) │
└────────────────────────────────────────────────────────────────┘
```

### Invariants

- The seedbox scripts are the **only** code that talks to farm APIs.
  Workstation MCP server and workstation hourly PS1 both invoke them
  via SSH.
- Each seedbox script is callable in two modes: `--emit-json` (stdout
  JSON, no Discord chatter — for MCP/PS1 callers) and `--cron`
  (logs-only, Discord on failure — for systemd-timer callers).
- Workstation `B:\QFlix\data\` is the source of truth for time-series
  analysis. Claude reads it instead of re-querying.

## 4. Data layout

```
B:\QFlix\data\
├── snapshots\
│   └── 2026-05-12\
│       ├── 00.json            ← one file per UTC hour
│       ├── 01.json
│       └── ...                  (~50KB-200KB each)
├── logs\
│   └── 2026-05-12\
│       ├── sonarr.log         ← appended hourly (last 1h tail)
│       ├── sonarr2.log
│       └── ...                  (rotates daily)
├── stale-state.json           ← per-hash zero-movement ledger
├── events\
│   └── 2026-05-12.jsonl       ← one line per write action
├── runs\
│   └── 2026-05-12\
│       └── 07-00-13.log       ← PS1 transcript (7d retention)
├── last-collect.json          ← heartbeat
└── .collect.lock              ← single-instance lock (with PID)
```

### Snapshot schema (`snapshots/<date>/HH.json`)

```jsonc
{
  "captured_at": "2026-05-12T07:00:13Z",
  "captured_at_az": "2026-05-12T00:00:13-07:00",
  "qbit": {
    "torrents": [
      {
        "hash": "abc123...",
        "name": "Some.Release.Group.1080p.mkv",
        "added_on": 1715472000,
        "size_bytes": 12345678901,
        "downloaded_bytes": 8000000000,
        "progress": 0.648,
        "dl_speed_bytes_s": 0,
        "up_speed_bytes_s": 1234,
        "state": "stalledDL",
        "category": "sonarr",
        "tags": ["tv-sonarr"],
        "ratio": 0.21,
        "eta_seconds": null,
        "seeds": 0,
        "leeches": 0,
        "last_activity": 1715472000,
        "arr": {
          "slug": "sonarr",
          "queue_id": 4218,
          "title": "Show Name - S01E03",
          "tracked_state": "downloading",
          "status_messages": [],
          "cf_score": 25
        },
        "seerr_request": {
          "id": 87,
          "requested_by": "operator@example.com",
          "requested_at": "2026-05-08T13:42:00Z"
        }
      }
    ],
    "totals": { "count": 47, "dl_mbps": 12.4, "up_mbps": 3.1 }
  },
  "arrs": {
    "sonarr": {
      "queue": [/* full queue items */],
      "missing_count": 12,
      "system_status": { "version": "...", "isProduction": true }
    },
    "sonarr2": {/* ... */},
    "radarr": {/* ... */},
    "radarr2": {/* ... */}
  },
  "plex": {
    "libraries": [
      { "key": "1", "title": "Movies", "type": "movie",
        "count": 4218, "recently_added_24h": 7,
        "unanalyzed_count": 0 }
    ],
    "active_sessions": 2,
    "last_scan": "2026-05-12T03:15:00Z"
  },
  "health": {
    "kuma_red": []
  }
}
```

### `stale-state.json` (mutable ledger)

```jsonc
{
  "hashes": {
    "abc123...": {
      "first_zero_movement_at": "2026-05-12T04:00:00Z",
      "consecutive_zero_hours": 3,
      "last_progress": 0.648,
      "rule_matched": "stalledDL>24h",
      "candidate_for_unstick": true,
      "acted_on_at": null
    }
  },
  "updated_at": "2026-05-12T07:00:13Z"
}
```

### `events\<date>.jsonl` (audit log)

```jsonl
{"ts":"2026-05-12T07:00:14Z","action":"unstick","slug":"sonarr","queue_id":4218,"hash":"abc...","title":"Show - S01E03","reason":"3h-zero-movement+stalledDL","result":"deleted+blocklisted","post_action":"sonarr-research-queued"}
```

### Retention

- `snapshots/` — 30 days
- `logs/` — 7 days
- `runs/` — 7 days
- `events/` — 365 days
- Pruning runs at the end of every hourly collect

## 5. Detection rules

All four signals required to flag a torrent as "stale" or "bad grab":

1. **qBit stalled or dead-slow >24h** — `state == "stalledDL"` for >24h,
   OR `state == "downloading"` with avg DL <10 kB/s and no progress
   change for >24h.
2. **qBit zombie / orphan** — Torrent in qBit with *arr category but
   no matching queue entry in that *arr (orphan), OR *arr queue item
   whose hash isn't present in qBit (zombie).
3. **Suspicious size / negative CF score** — Single video <100MB for
   a movie or <50MB for an episode (sample/scam), AND/OR *arr Custom
   Format score for the release is negative (TRaSH-guide says bad).
4. **Stuck-import + Plex post-import sanity** — Queue item in
   importPending/Blocked/Failed >6h (separate from `arr-housekeeping
   --unstick`; collector only flags + reports, doesn't act in this
   case to avoid double-action), AND post-import Plex item that's
   0-duration / unknown codec / never-analyzed.

### Action trigger: 3-hour zero-movement rule

A torrent becomes a `candidate_for_unstick` only when:

- The same hash appears in 3 consecutive hourly snapshots, AND
- `downloaded_bytes` delta == 0 across all three, AND
- At least one of rules 1–3 applies (rule 4 flags only; never acts —
  it's already handled by `arr-housekeeping --unstick`).

Once a candidate, the collector immediately invokes `unstick.py` on the
seedbox unless `--max-actions-per-day` (default 10) has been reached
in today's `events/<date>.jsonl`. The cap is enforced both
workstation-side (collector pre-flight check) and seedbox-side
(`unstick.py` re-check at action time).

## 6. Seedbox scripts (`~/scripts/mcp/`)

Five scripts, all under `scripts/mcp/` in the repo, deployed to seedbox
`~/scripts/mcp/` by `scripts/configure/70-mcp-install.sh`.

### `collect.py`

- **Args:** `--emit-json` (MCP/PS1 mode) | `--cron` (systemd mode).
  Optional `--include logs,plex` toggles.
- **Does:** in one pass, in this order — qBit `GET /api/v2/torrents/info`;
  each *arr `GET /api/v3/queue?pageSize=500`, `/wanted/missing?pageSize=1`,
  `/system/status`; build hash→queue-item index; Seerr `/api/v1/request`
  build externalServiceId index; CF score batch lookup; Plex via
  python-plexapi venv enumerate libraries + recently-added + sessions.
  Emit single JSON.
- **Parallelism:** qBit + 4 *arrs concurrent via `ThreadPoolExecutor`;
  Plex serial after.
- **Budget:** ~3-8s total.
- **Imports:** `scripts/maint/lib/manifest.py` for config + secrets;
  new `scripts/mcp/lib/qbit_client.py` extracted from existing
  `scripts/maint/lib/qbit.py`; `scripts/maint/lib/notify.py` for
  Discord on failure.

### `unstick.py`

- **Args:** `--slug <name> --queue-id <n> --reason <s>` OR
  `--hash <h> --reason <s>` (looks up queue-id). `--dry-run` supported.
  `--emit-json` for MCP.
- **Does:** `DELETE /api/v3/queue/{id}?removeFromClient=true&blocklist=true`.
  Idempotent — already-removed returns `{"status":"already-removed"}`,
  exit 0.
- **Safety:**
  - Refuses if the relevant *arr's Kuma monitor is red. Read from
    `~/.opt/maint/state.json` (populated by the existing
    `manitoba-maint-webhook.service`). If state file is unreadable,
    fail closed (refuse to act).
  - Refuses if `events/<date>.jsonl` already has `--max-actions-per-day`
    (default 10) entries for today.
- **Output:** JSON with `{pre_state, action, post_state}`; appends one
  line to seedbox `~/scripts/mcp/events/<date>.jsonl`.

### `logs.py`

- **Args:** `--app <slug>|all --since <duration> --tail <n>`. Default
  24h / 5000 lines.
- **Does:** routes per app class:
  - UCC apps with `~/.apps/<slug>/logs/` → tail canonical log file.
  - systemd apps → `journalctl --user -u <unit> --since '<duration> ago'
    -n <tail> --output=short-iso`.
  - Docker UCC apps → `~/.apps/<slug>/logs/` if present, else
    `app-<slug> logs --tail <tail>`.
  - nginx → `~/.apps/nginx/logs/{access,error}.log`.
  - Maint pipeline → `journalctl --user -u manitoba-maint-pusher`,
    same for `-webhook` / `-window`.
- **Output:** JSON array of `{ts, level, message, source_file}`.
  Unparseable lines kept as raw `message` with `level: "unknown"`.

### `plex.py`

- **Args:** `--include libraries,sessions,recent`. `--recent-hours 24`.
  `--recent-max-per-library 20` (caps the recently-added list per
  library to keep enumeration cheap; 4218-movie library with a 24h
  filter never hits the cap in normal operation). `--emit-json` for
  MCP.
- **Does:** uses python-plexapi venv at `~/.apps/python-plexapi/venv/`.
  Enumerates libraries (count, type via `Library.totalSize`,
  recently_added via `library.recentlyAdded(maxresults=20)` filtered
  in-process to `--recent-hours`), active sessions, per-library
  unanalyzed-count (count of items where `media[0].videoCodec is None`,
  sampled via `library.search(unwatched=False, sort='addedAt:desc',
  limit=200)` — bounded so a million-item library doesn't time us out).

### `missing.py`

- **Args:** `--cron` (no args, default behavior) | `--slug <name>`
  (single *arr). `--emit-json`.
- **Does:** `POST /api/v3/command {"name": "MissingEpisodeSearch"}`
  (or `MissingMoviesSearch` for radarrs). Re-uses logic from
  `arr-housekeeping.py --missing`; the existing `arr-housekeeping`
  script is refactored to `import scripts/mcp/missing.py` and
  `--missing` becomes a thin wrapper so we don't duplicate code.

## 7. Workstation components (`scripts/local/`)

### `qflix-collect.ps1`

Installed by `scripts/local/install-qflix-collect.ps1 -Install` creating
Task Scheduler entry `\Archangel\QFlix\Hourly Collect`:

- **Trigger:** at logon + repeat every 1 hour for 24h indefinitely.
- **Run only if user logged on.**
- **Stop if running >5 min.**

**Execution order:**

1. **Tunnel check:** `Test-NetConnection 127.0.0.1 -Port 42014`. If
   false, `Start-ScheduledTask \Archangel\Manitoba SSH Tunnel`. Wait
   30s. If still down → log, post Discord error, exit 2.
2. **Single-instance lock:** PID file at `B:\QFlix\data\.collect.lock`.
   If prior run still alive, exit 1.
3. **Collect:** `ssh ... python3 ~/scripts/mcp/collect.py --emit-json
   --include logs,plex` → atomic write to `snapshots\<date>\<HH>.json`.
4. **Per-app logs:** `ssh ... python3 ~/scripts/mcp/logs.py --app all
   --since 1h --tail 2000` → append to `logs\<date>\<app>.log`.
5. **Stale ledger update:** read previous 2 hours' snapshots locally
   (no SSH); for each hash present in current + 2 priors, compute
   `downloaded_bytes` delta; update `stale-state.json`.
6. **Autonomous actions:** for each `candidate_for_unstick=true` not
   yet acted on, `ssh ... python3 ~/scripts/mcp/unstick.py --hash <h>
   --reason <r> --emit-json`. Append result to `events\<date>.jsonl`.
   Workstation-side cap `--max-actions-per-day=10` (independent of
   the seedbox cap of the same value; both enforced, defense in depth).
   Workstation reads today's `events\<date>.jsonl` to count.
7. **Discord post:** one summary message, no @ping, counts of
   torrents/stale-candidates/unstick-actions. Skipped if 3 consecutive
   identical no-change runs (collapses to 1 per 6h).
8. **Retention prune.**
9. **Kuma push:** push UP to monitor "QFlix Collect (workstation)"
   with `msg=<counts summary>`.
10. **Heartbeat:** atomic-write `last-collect.json` with
    `{ts, exit_code, duration_s, snapshot_path}`.

### `qflix-mcp.py`

Native Python MCP server, stdio transport. Registered via
`claude mcp add qflix-mcp -- python <path>\qflix-mcp.py`.

**Read tools (operate on `B:\QFlix\data\`, zero seedbox traffic):**

| Tool | Source |
|---|---|
| `qflix_status` | `last-collect.json` + latest snapshot |
| `qflix_list_torrents` | latest snapshot |
| `qflix_torrent_history` | walk snapshots dir for one hash (default last 24h, max 720h) |
| `qflix_list_stale` | `stale-state.json` |
| `qflix_get_logs` | `logs/` dir |
| `qflix_plex_libraries` | latest snapshot |
| `qflix_recent_events` | `events/` jsonl |
| `qflix_arr_queue` | latest snapshot |

**Write tools (proxy SSH to seedbox):**

| Tool | Action |
|---|---|
| `qflix_unstick_torrent` | SSH → `unstick.py` |
| `qflix_trigger_missing_search` | SSH → `missing.py` |
| `qflix_refresh_collect` | SSH → `collect.py` + write snapshot |

Each tool's docstring includes a "use when…" hint; all responses are
typed JSON, all complete in <5s.

### `qflix-mcp/lib/`

- `cache.py` — read latest / nth-most-recent snapshot, range queries,
  atomic write helper.
- `ssh.py` — `ssh_call(cmd, timeout=30) -> CompletedProcess`. Reads
  SSH-host from `secrets/seedbox.ssh-host`, sandboxed PATH, no shell
  interpolation.
- `discord.py` — webhook poster, no @ping (matches `qflix-rea.ps1`'s
  notifier shape; we do not duplicate `notify.py` logic).

## 8. Scheduling

| Task | Where | When (TZ) | UTC |
|---|---|---|---|
| `qflix-collect.ps1` | Windows Task Scheduler | Hourly on the hour, while user logged on | hourly |
| `qflix-missing-search.timer` | Seedbox systemd-user | `00:00 America/Phoenix` daily (incl. Mon) | `07:00 UTC` daily |
| `arr-housekeeping.py --unstick` | Seedbox crontab | unchanged: `:15 hourly` | hourly |
| `manitoba-maint-window.timer` | Seedbox systemd-user | unchanged: Mon 13:00 CEST | Mon 11:00 UTC |

Phoenix is UTC-7 year-round (no DST), so 07:00 UTC is stable.
Missing-search at 07:00 UTC runs 4h before the 11:00 UTC maintenance
window — no collision risk.

## 9. Secrets

**Workstation:** no new secrets. SSH key, `seedbox.ssh-host`, and
`discord-webhook.url` already used by the tunnel daemon and REA. Path
resolution matches `qflix-rea.ps1`.

**Seedbox:** no new secrets. Per-app `.key/.port/.urlbase` already
present in `~/secrets/`.

**Repo `secrets/`:**

- `kuma-push-tokens.json` gains one entry: `"QFlix Collect (workstation)"`.
  Token generated by `scripts/maint/bootstrap-kuma-monitors.py` at
  install time (existing pattern).

## 10. Failure modes

| Failure | Behavior | Detection |
|---|---|---|
| SSH tunnel down + restart fails | exit 2, Discord error, next hour retries | `last-collect.json.exit_code == 2`; Kuma stays red |
| SSH OK but `collect.py` non-zero | Partial snapshot written for stages that completed; Discord error names failing stage | `last-collect.json.exit_code != 0` |
| Seedbox API auth fails (rotated key) | Per-app `{error: "auth_failed"}`; other apps still collected | Snapshot inspection |
| Workstation off >3h | Gap in snapshots; 3-hour rule pauses (only acts on 3 consecutive hours of data); resumes after 3 fresh | Visible via `qflix_torrent_history` |
| `unstick.py` *arr-success but qBit doesn't release | Event line `result: "deleted+blocklisted+qbit-orphan"`; next collect's orphan rule picks it up | Self-healing |
| `--max-actions-per-run` hit | Remaining candidates carried forward; Discord summary mentions cap hit | event-line count |
| `B:\` full | Atomic-write fails → exit 3, Discord error, no partial corruption | manual intervention needed |
| Plex token expired | `plex.py` returns `{error: "plex-auth-failed"}`; rest of snapshot continues | Plex section missing |

### Dead-man monitor

Kuma push monitor "QFlix Collect (workstation)" pushed at end of every
successful run. Threshold 90 min (one miss tolerated; two consecutive
= red). Wired to QFlix Discord channel + auto-heal channel (matches
all other manitoba monitors).

## 11. Definition of Done

**Done = 3 consecutive hourly collect runs with zero errors, end to
end.** The implementation is complete only when **all** of the
following pass:

| # | Criterion |
|---|---|
| 1 | `B:\QFlix\data\snapshots\<date>\HH.json` written 3 hours in a row, valid JSON, `captured_at` within 60s of top-of-hour |
| 2 | All 4 *arrs + qBit + Seerr + Plex sections present and non-empty in each of the 3 snapshots (no `error:` keys) |
| 3 | Per-app log files in `B:\QFlix\data\logs\<date>\` updated each of 3 hours, non-zero size |
| 4 | `stale-state.json` accumulates state correctly across the 3 runs — at least one hash tracked across all three samples, regardless of whether it triggers action |
| 5 | If a 3-hour-zero-movement candidate exists in the test window, autonomous unstick fires, appends to `events/<date>.jsonl`, posts to Discord (no @ping), respects `--max-actions-per-run` |
| 6 | Each of 3 runs posts a Discord summary, no @ping, accurate counts |
| 7 | Kuma push monitor "QFlix Collect (workstation)" goes down → up after first run, stays green across all 3 |
| 8 | All 11 MCP tools invokable from a fresh Claude Code session, each returns typed JSON under 5s |
| 9 | `qflix-missing-search.timer` shows `Result=success` on its first 07:00 UTC fire |
| 10 | Tunnel-down test: stop `manitoba-tunnel.ps1`, trigger collect.ps1, confirm auto-restart of tunnel + successful completion |
| 11 | SSH-broken test: temporarily break SSH key path, trigger collect, confirm graceful exit + Discord error + Kuma red until restored |
| 12 | `inventory.md` Section M gets a new row for the MCP system; new manifest entry for `qflix-missing-search` cron-class app; new Kuma monitor row reflected in counts |

No phases. No follow-ups. Implementation finishes when all 12 pass
across 3 real consecutive hours.

## 12. Deployment

### One-time install order

1. `scripts/configure/70-mcp-install.sh` — rsync `scripts/mcp/` to
   seedbox `~/scripts/mcp/`, set perms, install
   `qflix-missing-search.service` + `.timer` units.
2. `scripts/maint/bootstrap-kuma-monitors.py` — adds Kuma push monitor
   "QFlix Collect (workstation)", writes token to
   `secrets/kuma-push-tokens.json` (now 27 entries).
3. `scripts/configure/71-mcp-manifest-update.py` — adds
   `qflix-missing-search` entry to `manifest/apps.yaml` with
   `class: cron`, `unit: qflix-missing-search.service`,
   `kind: systemd_oneshot`, `kuma_monitor: "Qflix Missing Search"`.
4. On workstation: `scripts/local/install-qflix-collect.ps1 -Install`
   — creates `B:\QFlix\data\` tree, registers
   `\Archangel\QFlix\Hourly Collect` task.
5. On workstation: `claude mcp add qflix-mcp -- python
   <path>\scripts\local\qflix-mcp\qflix-mcp.py`.

### Acceptance run

Wait 3 consecutive top-of-hours. Run each of the 12 criteria checks
documented in §11. If any criterion fails, debug, fix, restart the
3-hour clock.
