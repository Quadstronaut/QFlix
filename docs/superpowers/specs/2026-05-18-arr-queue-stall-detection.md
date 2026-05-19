# Spec: extend `arr-housekeeping.py --unstick` for broader stall detection

**Status:** proposed — awaiting operator approval
**Date:** 2026-05-18
**Author:** Claude (Opus 4.7) in collaboration with Quadstronaut
**Scope:** single PR, single file edit + tests

---

## Background

The 2026-05-17 environment audit identified four Sonarr/Radarr queue items recurring across collector cycles despite the existing `qflix-collect.ps1` qBit-side janitor:

| Hash | Title | Status | Age stuck |
|---|---|---|---|
| `a8100391…` | Blue.Mountain.State.S03E08.1080p.WEB.x264-STRiFE | warning / stalled with no connections | 32h |
| `2cd6b14b…` | Blue.Mountain.State.S03E09.1080p.WEB.x264-STRiFE | warning / stalled with no connections | 32h |
| `88b3c232…` | Blue Mountain State S03E04 720p HDTV x264-ORENJI | warning / stalled with no connections | 12h |
| `45502c24…` | (BMS S03E04 metadata download) | queued / qBittorrent is downloading metadata | 41h |

A second sweep on 2026-05-18 added one Radarr metadata-stuck (`c519ab25…`) and an NYPD Blue cluster (`f3cce7c6…`) — one 47GB torrent cross-attached to 22 Sonarr queue rows with 87-day ETA.

All seven items were resolved manually by issuing `DELETE /api/v3/queue/{id}?removeFromClient=true&blocklist=true` against the appropriate *arr. The existing `qflix-collect.ps1` had already taken action at the qBit layer for some of them (`result: "deleted+blocklisted"` events visible in `qflix_recent_events`), but Sonarr/Radarr immediately re-grabbed the same release because no *arr-level blocklist entry existed.

**Root cause:** `qflix-collect.ps1` operates only on qBit. The release name remains valid in the *arr's view of the indexer, so the next RSS sync re-grabs the same torrent. This is functioning as designed at the qBit layer; the missing piece is *arr-layer permanent suppression.

## Existing implementation

`scripts/maint/arr-housekeeping.py --unstick` (Quadstronaut, pre-2026-05) already does the *arr-layer permanent blocklist:

- Hourly systemd timer.
- Hits each *arr's `GET /api/v3/queue?pageSize=500&includeUnknownSeriesItems=true`.
- Tracks first-seen-stuck time per `(slug, downloadId)` in `~/.opt/maint/stuck-queue-state.json`.
- After grace period (`ARR_STUCK_HOURS=6`), issues `DELETE /api/v3/queue/{id}?removeFromClient=true&blocklist=true`. Sonarr/Radarr auto-search a replacement.
- Discord notification per cycle summarising actions.

The gap is the detection predicate. `_is_stuck()` (lines 183-188) only matches **completed-but-not-imported** stalls:

```python
return (
    item.get("status") == "completed"
    and item.get("trackedDownloadState") in {"importPending", "importBlocked", "importFailed"}
)
```

None of the seven incidents this week matched that predicate, because they all stalled **pre-completion** (no peers, no metadata, or downloading-but-impossibly-slowly).

## Proposed change

Extend `_is_stuck()` into `_classify_stuck()` with four detection modes, per-mode thresholds, and bounded action caps. No new file; no new timer; reuse the existing state file, action path, and notification channel.

### Detection modes

| Mode | Predicate | Default threshold |
|---|---|---|
| `completed-not-imported` | existing — `status=completed` ∧ `trackedDownloadState ∈ {importPending, importBlocked, importFailed}` | `ARR_STUCK_HOURS_IMPORT=6` |
| `stalled-no-peers` | `status=warning` ∧ `errorMessage` matches `/stalled.*no connections/i` | `ARR_STUCK_HOURS_PEERS=4` |
| `metadata-stuck` | `status=queued` ∧ `errorMessage` matches `/downloading metadata/i` | `ARR_STUCK_HOURS_METADATA=6` |
| `slow-cluster` | `downloadId` appears in ≥3 distinct queue rows ∧ `estimatedCompletionTime > now + 30d` ∧ recorded `sizeleft` has not decreased over the last 7 days | `ARR_STUCK_DAYS_CLUSTER_ETA=30`, `ARR_STUCK_DAYS_CLUSTER_NOPROGRESS=7` |

### State schema (backward-compatible)

Each record gains two optional fields:

```json
{
  "sonarr:88b3c232578dbd271fea28549709065136eecd48": {
    "title": "Blue Mountain State S03E04 720p HDTV x264-ORENJI",
    "queue_id": 388034041,
    "first_seen_stuck": 1716000000.0,
    "slug": "sonarr",
    "mode": "stalled-no-peers",                            // NEW
    "sizeleft_history": [                                  // NEW (cluster mode only)
      {"ts": 1716000000.0, "sizeleft": 4159978713}
    ]
  }
}
```

Records written by the pre-extension version (no `mode` field) are treated as `completed-not-imported` for grace-period math. No migration script needed; state evolves naturally over one hourly cycle.

### Action

Single path for all modes:

```
DELETE /api/v3/queue/{id}?removeFromClient=true&blocklist=true
```

`skipRedownload` is omitted (= default `false`), so the *arr auto-searches a replacement release after the blocklist add. For `slow-cluster`, the cascade behaviour observed on 2026-05-18 holds: deleting one queue row removes the underlying qBit torrent, and the remaining cluster rows fall off the *arr queue automatically on its next poll. Confirmed in practice on the NYPD Blue cluster (1 DELETE → 22 queue rows resolved).

### Caps (new)

Two bounded counters per cycle prevent runaway when something systemic breaks (indexer down, qBit hung, etc.):

| Variable | Default | Behaviour |
|---|---|---|
| `ARR_MAX_ACTIONS_PER_RUN` | 10 | Across all *arrs combined. Excess matches logged but not acted on. |
| `ARR_MAX_ACTIONS_PER_SLUG` | 5 | Per *arr instance. Excess matches logged but not acted on. |

When either cap is hit, the Discord notification escalates to `error` level (signals systemic problem, not isolated stuck torrent).

### Idempotency contract

| Re-run condition | Outcome |
|---|---|
| Same data, no wall-clock change | Zero new actions; state file unchanged. |
| Same hash, still stuck, threshold not yet elapsed | State record carried forward; no action. |
| Same hash, still stuck, threshold elapsed | DELETE issued; state record dropped (hash will be re-tracked under a new `downloadId` if the *arr's re-search finds another stalled torrent). |
| Different release post-blocklist that stalls | Caught fresh under its own hash. |
| Queue item recovers (peers come back, metadata resolves) | State record dropped on next cycle when predicate no longer matches. |
| Cold start with empty state file | First run records baseline; first deletions land on the second run. Effective first-deletion latency = max(threshold, 1h). |

### Notification

Existing Discord path via `lib.notify`. Action summary tagged with mode:

```
arr-unstick swept:
sonarr: Blue Mountain State S03E04 (4.2h, stalled-no-peers) → blocklisted+research
sonarr: NYPD.Blue.S01… cluster-of-22 (slow-cluster) → blocklisted+research
radarr: c519ab25… (6.1h, metadata-stuck) → blocklisted+research
```

Level escalation: `info` (zero actions) → `warning` (1+ actions) → `error` (cap hit or any DELETE returns non-2xx).

### Tests

Three fixture queues + one unit test per mode against `_classify_stuck`:

- `tests/fixtures/arr-queue/peers.json` — single item, `status=warning`, peer-stalled message
- `tests/fixtures/arr-queue/metadata.json` — single item, `status=queued`, metadata message
- `tests/fixtures/arr-queue/cluster.json` — 3 items same `downloadId`, ETA 87d, sizeleft-history showing no decrease

Test names follow the existing `tests/unit/test_lifecycle.py` pattern.

Optional but recommended: one end-to-end test against a fake httpserver that returns a stuck fixture, asserts the DELETE was issued with the right query string, and asserts the state file was updated correctly.

## Out of scope

- Modifying `qflix-collect.ps1`. It remains the qBit-layer janitor; this is the *arr-layer escalation. Both can run in parallel without conflict (qBit-side removal cascades to *arr queue, and *arr-side DELETE cascades to qBit).
- Kuma push events. Existing Discord channel is the alarm path. Adding a Kuma monitor would require a new push token and dashboard tile; not justified by audit findings.
- New secrets. Reuses existing `~/secrets/{sonarr,sonarr2,radarr,radarr2}.{key,port,urlbase}` + `htpasswd.password`.
- Indexer-level blocklisting. We blocklist at the *arr layer per-release; the indexer itself remains untouched, so a different release group's encoding of the same content can still be grabbed. This is the desired behaviour.
- FlareSolverr / Cloudflare cascade root cause. Tracked separately under the same audit; that work fixes the upstream that produces some of the indexer-level 429 noise.

## Open questions

1. **Slow-cluster mode opinionation.** The NYPD Blue case (87-day ETA, 22 episode rows, single torrent) is unambiguously stuck. But occasionally an operator may intentionally grab a slow-seeded archival pack. With `ARR_STUCK_DAYS_CLUSTER_ETA=30` and `ARR_STUCK_DAYS_CLUSTER_NOPROGRESS=7`, a legitimate but slow torrent that's actually making progress (sizeleft decreasing) will not match. The risk window is "ETA >30d AND sizeleft truly stable for 7d" — likely rare enough to accept. **Operator decision:** ship with these defaults; tighten/relax via env var without redeploy.
2. **Logging volume.** With four modes, log line count per cycle will roughly 2× compared to current. VictoriaLogs ingest cost is negligible; not a concern unless a separate budget surfaces.

## Migration / rollout

1. Land PR.
2. Buildarr/deploy pulls the new `arr-housekeeping.py` to manitoba on next sync.
3. First hourly run after deploy records state baseline for newly-visible modes (no deletions).
4. Subsequent runs trigger deletions for items that have been stuck ≥ threshold from baseline.
5. Discord notification confirms behaviour in production within 2 hours of deploy.

## Reversal plan

The change is a pure additive predicate. To revert: drop the new modes from `_classify_stuck()` and the original behaviour is restored. State file is forward-compatible; new optional fields are simply ignored by the old code.
