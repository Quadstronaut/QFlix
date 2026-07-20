# SAB stuck-handling parity — design (2026-07-19)

Usenet (SABnzbd) becomes a first-class citizen of the stuck-download pipeline:
same detection, same autonomous remediation, same observability as torrents —
plus the escalation layer torrents never needed, and usenet wired into every
*arr. Nothing deferred.

## Research grounding (4-agent sweep, source-cited)

- **SAB has no stall detection and never will get one from us waiting**: no
  built-in watchdog for Fetching/Verifying/Repairing/Extracting/Propagating
  hangs (GH issues open 2017–2025, SAB FAQ prescribes manual process-kill).
  No established third-party "SAB unsticker" exists. Detection must be ours.
- **SAB-side remediation APIs are unreliable**: `mode=resume` returns
  `{"status":true}` while no-oping on wedged queue objects (GH #802, #1104,
  #3106; reproduced live on this box 2026-07-19). SAB does NOT log API calls
  even with api_logging=1 — state must be verified by re-polling, never by
  return codes.
- **Root cause of the incident, found in SAB source** (`nzb/object.py`
  ~1230-46): when a file's `finish_import()` fails after restart, SAB sets the
  WHOLE job `Status.PAUSED` with no auto-resume path. Reproduced in box logs
  2026-07-16 04:30 (SIGTERM mid-decode → re-import fail → force-pause).
  Always follows a process restart; rare (2 dates in 12 days of logs).
- **Sonarr provably never self-heals a paused SAB job**:
  `FailedDownloadService.Check()` fires only on `IsEncrypted` or
  `Status==Failed`; `Paused` maps to neither (Sonarr source). No Stall
  health-check class exists in the *arr codebase at all.
- **The ecosystem-standard remediation is *arr-side**: queue
  `DELETE ?removeFromClient=true&blocklist=true` → SAB
  `mode=queue&name=delete&del_files=1` + blocklist row
  (title+pubdate+size keyed for usenet, no GUID) + auto re-search
  (`AutoRedownloadFailed=true` confirmed on box). Our `unstick.py` core
  DELETE flow is already protocol-agnostic — proven live 2026-07-19
  (`--queue-id` on SAB-backed Sonarr items → instant healthy re-grab at
  215 MB/s).
- **Last-resort hammer exists**: `mode=restart_repair` (restart + queue
  rebuild from disk, preserves downloaded data, may reorder queue). The only
  documented remedy for wedged queue objects and hung par2/unrar.
- **Config landmine on box**: SAB `history_limit=10` auto-prunes history rows;
  documented as unsafe with Sonarr/Radarr FDH (can delete the Failed row
  before the *arr reconciles it). Must be disabled.
- **FDH blind spot**: SAB failure message "Unpacking failed, write error or
  disk is full?" maps to *arr Warning, NOT Failed — FDH silently skips it.
  Our detector must catch it independently.
- **Radarr has no SAB client wired at all** (qBit only); sonarr2/radarr2
  likewise. Usenet-live currently means Sonarr-only.

## Decisions (operator-confirmed)

1. **Fold into the collect loop** — stuck-downloads is ONE concern,
   client-agnostic: snapshot → stale-state → unstick, hourly, shared 10/day
   cap, shared events log, existing Kuma check. (Compartmentalization law
   reading: same concern, second client — not a new module.)
2. **Armed from day 1** — same trust as the torrent loop.
3. **Nothing out of scope**: automated restart_repair circuit-breaker IN;
   Radarr + sonarr2 + radarr2 usenet wiring IN; history_limit fix IN.

## Component contracts (pinned — parallel implementation builds against these)

### C1. `scripts/mcp/lib/sab_client.py` (NEW)

Mirrors `qbit_client.py` conventions. No login (apikey query param), secrets
`sabnzbd.port` / `sabnzbd.key`, base `http://127.0.0.1:{port}/api`.

```python
class SabClient:
    def list_slots(self) -> list[dict]      # mode=queue  -> queue.slots raw dicts
    def queue_meta(self) -> dict            # mode=queue  -> {"paused": bool, "kbpersec": float, "status": str}
    def list_history(self, limit=60) -> list[dict]  # mode=history -> history.slots raw
    def delete_slot(self, nzo_id, del_files=True) -> bool   # mode=queue&name=delete[&del_files=1]
    def restart_repair(self) -> bool        # mode=restart_repair
```

All methods raise on transport error; callers wrap. Timeouts 15s
(restart_repair 30s, and its caller must tolerate the connection dropping —
SAB restarts mid-response; treat timeout/connection-reset after issuing as
success-pending, verify by re-poll).

### C2. Snapshot `sab` section (collect.py)

`--include` gains `sab` (in the default set). Normalized slot shape (the
stale loop consumes exactly these fields; mirrors qbit vocabulary):

```json
"sab": {
  "slots": [{
    "id": "SABnzbd_nzo_abc123",       // nzo_id
    "name": "...", "cat": "sonarr",
    "state": "Downloading",           // SAB Status string verbatim
    "size_bytes": 0, "downloaded_bytes": 0,   // (mb - mbleft) * 1MB, int
    "progress": 0.42,                 // 1 - mbleft/mb (0 when mb==0)
    "dl_speed_bytes_s": 0             // queue-level kbpersec*1024 if this slot is the active Downloading one, else 0
  }],
  "queue": {"paused": false, "status": "Idle", "kbpersec": 0.0},
  "totals": {"count": 2}
}
```

Failure shape parity with qbit: `{"error": "...", "slots": [], ...}`.
`classify`-style helper `matches_stale_sab_rule(slot, queue_paused) -> rule|None`:

| rule | predicate |
|---|---|
| `sab-paused-pinned` | `state == "Paused"` and queue not paused (object.py wedge) |
| `sab-zero-movement` | `state in {Downloading, Queued, Grabbing, Fetching, Propagating}` — byte-delta handled by the stale loop, this helper only vets state eligibility |
| `sab-pp-hung` | `state in {Verifying, Repairing, Extracting, Moving, Running, QuickCheck, Checking}` |

### C3. Stale-state loop (qflix-collect.py)

- Samples walk BOTH `snap.qbit.torrents` (key `hash`) and `snap.sab.slots`
  (key `id`) into the same `samples` dict; each sample records
  `{downloaded, state, progress, dlspeed, kind: "qbit"|"sab"}`.
- Every `hashes[k]` entry gains `kind`. Missing `kind` in a loaded legacy
  entry ⇒ `"qbit"`.
- Rule dispatch per kind. SAB branch (3-snapshot zero-delta required, same as
  torrents):
  - zero-delta + `sab-paused-pinned` state ⇒ candidate (rule sab-paused-pinned)
  - zero-delta + downloadish state ⇒ candidate (rule sab-zero-movement)
  - zero-delta + PP state ⇒ tracked with rule `sab-pp-hung`,
    `candidate_for_unstick: False` (unstick can't fix a hung unrar) — feeds
    the stuck list + the escalation path instead.
- **Ghost prune**: `live = {qbit hashes} ∪ {sab slot ids}`; skip prune if
  EITHER section has `error`.
- qBit-only rules (meta-stuck, bad-grab) unchanged, still qbit-scoped.

### C4. Escalation circuit-breaker (qflix-collect.py)

After `act_on_candidates`, `escalate_sab_if_pinned(...)`:

- **Strike condition** (any): (a) a sab-kind entry has `acted_on_at` set AND
  its id still appears in the latest snapshot's sab slots AND still matches a
  stale rule (unstick was dispatched ≥1h ago and the slot survived — delete
  no-oped, wedged beyond *arr reach); (b) a `sab-pp-hung` entry has
  `consecutive_zero_hours >= PP_HUNG_ESCALATE_HOURS` (default 4).
- **Action**: `SabClient.restart_repair()` — SAB restart + queue rebuild
  (the documented fix for both wedge classes; kills hung par2/unrar).
- **Rails**: latch file `DATA_ROOT/sab-repair-latch.epoch`, max 1 fire per
  `SAB_REPAIR_COOLDOWN_H` (default 24h); Discord WARN + event-log line
  `{"action": "sab-restart-repair", "trigger": ..., "ids": [...]}`; verify by
  re-polling `queue_meta()` after 60s (log outcome, never raise).
- Post-repair, the normal loop re-evaluates next hour: repaired jobs resume
  or fail → FDH / unstick handles from there. Closed loop.

### C5. Unstick (unstick.py)

- Core `_resolve_queue_item` / `_execute_delete` / `run()` DELETE flow:
  **zero changes** (proven protocol-agnostic).
- Dispatch by id shape: `_id_kind(s)` → `"sab"` if `s.startswith("SABnzbd_nzo")`,
  `"qbit"` if 40-char hex, else `"unknown"` (probe qBit then SAB).
- `_auto_detect_slug`: for sab ids, look up slot via `SabClient.list_slots()`,
  return `cat` iff known *arr slug.
- `_try_sab_orphan_cleanup(nzo_id, dry_run)`: twin of the qBit fallback —
  `delete_slot(nzo_id, del_files=True)`. Statuses: `sab-orphan-removed`,
  `already-fully-removed`, `sab-unreachable`, `sab-delete-failed`,
  `dry-run-sab-orphan`. `sab-orphan-removed` joins `_EFFECTIVE_STATUSES`
  (consumes a daily-cap slot) and the collector's `_EFFECTIVE_RESULTS` /
  `_TERMINAL_STATUSES`.
- Orphan fallback call sites route by `_id_kind`.

### C6. Heartbeat doc (app_status.py)

- Per-slot SAB fetch (via existing inline collectors or SabClient) feeds
  `build_stuck_list` a second name-join source: union names map
  `{qbit hash → name} ∪ {sab id → filename}`; ghost-guard now checks the
  right liveness source per entry kind.
- Label: `hash8` for SAB ids = LAST 8 chars (literal `SABnzbd_` prefix would
  collide); entries gain `"kind": "torrent"|"usenet"`. Android app: no
  rebuild needed (`ignoreUnknownKeys`); a later cosmetic release may render
  kind icons.
- New alert: SAB history rows whose fail message matches the FDH blind spot
  ("Unpacking failed, write error or disk is full?") ⇒ crit
  "SAB unpack failed (disk full?) — FDH blind spot" (disk-full is operator
  territory).
- `downloads.sab` gains `"slots_stuck"` passthrough count for the app.

### C7. Usenet everywhere (`scripts/configure/90b-usenet-all-arrs.py`, NEW)

Idempotent, `--dry-run` default / `--execute`; per *arr in
{radarr, sonarr2, radarr2}:
1. SAB download client (implementation Sabnzbd, host `172.17.0.1`, port
   secret `sabnzbd.port`, apikey secret `sabnzbd.key`, category = arr slug,
   `removeCompletedDownloads=true, removeFailedDownloads=true`, priority
   matching sonarr's).
2. NZBgeek indexer added DIRECT (Prowlarr won't sync usenet — 2026-06-22
   lesson), Newznab implementation, url secret `nzbgeek.url`, key secret
   `nzbgeek.key`, categories per media kind.
3. Delay profile: usenet ENABLED, preferred protocol unchanged unless absent
   (2026-06-22 lesson: sonarr shipped with usenet OFF).
4. Verify FDH: `autoRedownloadFailed=true` on each arr (report, set if off).
SAB side: ensure categories `radarr`, `sonarr2`, `radarr2` exist
(`mode=set_config` section=categories, dirs mirroring the sonarr category
layout under `complete_dir`).

### C8. SAB config hardening (same script, `--execute`)

- `history_limit` 10 → 0 (unsafe-with-FDH per research).
- Report (no change): `fail_hopeless_jobs=1`, `fast_fail=1`,
  `pause_on_post_processing=True` — all correct as-is.

### C9. Tests

- `test_mcp_collect.py`: normalize_sab_slot; matches_stale_sab_rule per
  state class; error-shape.
- `test_qflix_collect_stale_state.py`: SAB sample tracking; kind field;
  legacy-entry kind default; SAB candidate rules; pp-hung non-candidate;
  ghost-prune union (live SAB id survives; gone SAB id pruned; qbit-error
  alone doesn't mass-prune sab and vice versa); escalation strike (a) and
  (b), latch cooldown, event line (urllib monkeypatched).
- `test_mcp_unstick.py`: `_id_kind`; sab auto-detect slug; sab orphan
  statuses incl. effective-status accounting; resolve-by-nzo_id documented.
- `test_mcp_app_status.py`: mixed stuck list (no collision, both names
  resolve, kind field); unpack-blind-spot alert; last-8 label.
- `test_usenet_all_arrs.py` (new): pure helpers of 90b (payload builders,
  idempotency predicates) with mocked transports.

## Rollout order

1. Implement + full suite green locally.
2. Deploy code to box (collect.py, qflix-collect.py, unstick.py,
   app_status.py, lib/sab_client.py).
3. Run 90b `--dry-run`, review, `--execute` (arr wiring + SAB categories +
   history_limit=0).
4. Force one collect run; verify snapshot has `sab` section, stale-state
   empty (queue currently healthy), doc renders.
5. Docs (CHANGELOG, inventory collector row, FAQ), commit, push, memory.

## Invariants

- A wedged SAB job is detected ≤3h after last byte moved, unstuck ≤4h
  (shared cap permitting), escalated to restart_repair ≤1 day if the unstick
  no-ops; every action lands in the events log and Discord.
- No SAB entry can be ghost-pruned while its slot is live; no torrent
  behavior changes.
- All *arrs can grab usenet; FDH is armed everywhere; SAB history can no
  longer race FDH.
