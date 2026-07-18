# QFlix Quality Fallback — Design

**Date:** 2026-06-06
**Status:** Implemented + deployed 2026-06-06 (PR #66)
**Scope:** Movies only (radarr, radarr2) for v1. TV is alert-only; v2 decided from v1 data.

> **v2 RESOLVED (2026-07-18):** the TV-alert-only deferral is superseded by
> `docs/superpowers/specs/2026-07-18-tv-fallback-v2-design.md` — TV is now
> park-only (day-15 unmonitor; no loosen ramp, since Sonarr profiles are
> per-series and release-less specials grab nothing at any quality), and a
> standalone `specials_policy.py` janitor keeps Season 0 unmonitored.

## Problem

The daily MissingSearch sweep (`scripts/mcp/missing.py`, 07:00 UTC) re-searches the same
missing items forever at full quality standards. A real request (Brinton's) had no
acceptable hits at the standard profile but matched fine at a slightly lower quality.
Today nothing ever loosens, and unfindable items burn 4 indexer queries per item per day
indefinitely.

## Decision summary (operator interview)

| Question | Decision |
|---|---|
| Scope | Movies only in v1; Discord digest when TV episodes cross the threshold (no action) |
| Loosening ramp | Two-stage: day 5 → HDTV tier; day 10 → SD retail |
| Pre-retail qualities | CAM, TELESYNC, TELECINE, DVDSCR, WORKPRINT hard-banned everywhere. REGIONAL allowed in stage 2 only |
| After a fallback grab | Restore the original profile — file shows cutoff-unmet, existing upgradinatorr + RSS pipeline upgrades it later |
| Exhaustion (day 15) | Restore original profile, unmonitor, mark parked in state, Discord warning |
| Mechanism | Approach A: profile-swap orchestrator (Radarr's decision engine makes every grab call; we only widen the menu) |

Rejected alternatives: direct `/release` push (re-implements Radarr's selection logic —
maintenance trap); single wide profile (flattens the deliberate two-stage ramp).

## Component 1 — Fallback profiles (one-time configure)

> **Recon corrections (2026-06-06, deployed Radarr 6.1.1.10360):** the TRaSH
> `HD Bluray + WEB` profile (id 7 on both instances) allows only **Bluray-720p,
> WEB 1080p, Bluray-1080p** — no WEB-720p, contrary to the draft's assumption.
> Additionally, library movies are split across profile 7 and the stock profile 6
> `HD 720p/1080p` (which already allows HDTV and has `upgradeAllowed=false`).
> Per-movie `original_profile_id` restore handles the mix; profile-6 movies
> effectively loosen only at stage 2, and the restore-then-upgrade path only
> applies to movies whose original profile allows upgrades.

Profile bootstrap is a mode of the orchestrator itself
(`quality_fallback.py --bootstrap-profiles`, run on the seedbox where the API is
loopback-reachable), invoked by `scripts/configure/73-quality-fallback-install.sh`.
It clones each instance's `HD Bluray + WEB` profile (matched by name):

- **`QFlix Fallback HDTV`** — source profile's allowed set + HDTV-720p,
  HDTV-1080p, WEB 720p (WEBDL-720p + WEBRip-720p)
- **`QFlix Fallback SD`** — HDTV-tier set + SDTV, DVD, WEB 480p (WEBDL-480p +
  WEBRip-480p), Bluray-480p, REGIONAL

Invariants:

- CAM, TELESYNC, TELECINE, DVDSCR, WORKPRINT remain disallowed in both profiles.
- Cutoff is copied unchanged from the source profile, so any fallback grab is
  below-cutoff and therefore upgradable by the existing pipeline.
- Neither profile appears in `recyclarr.yml` — the weekly TRaSH sync never touches them.
- Custom-format items + scores are copied at clone time. They may drift from later TRaSH
  updates; this only affects movies temporarily in fallback and is accepted for v1.
- Re-running the script updates the fallback profiles in place (matched by name) rather
  than duplicating them.

## Component 2 — Daily orchestrator

`scripts/mcp/quality_fallback.py`, fired by systemd user units
`qflix-quality-fallback.{service,timer}` at **07:30 UTC** daily (30 min after the missing
sweep so day-of grabs settle). CLI matches `missing.py` conventions:
`--cron | --emit-json`, plus `--dry-run` (log intended actions, write no state, touch no
arr).

### State

`~/.apps/qflix-fallback/state.json` — per-item records keyed `slug:tmdbId` (movies) and
`slug:episodeId` (TV alert tracking):

```json
{
  "radarr:603": {
    "days": 7,
    "stage": 1,
    "original_profile_id": 6,
    "last_counted": "2026-06-06",
    "parked": false,
    "title": "The Matrix"
  }
}
```

`last_counted` dedupes by calendar date — a re-run on the same day never double-counts.

### Movie state machine (per radarr instance)

Eligible population: `wanted/missing`, `monitored == true`, `isAvailable == true`.
Unreleased movies never accrue days. A day only counts if the record's
`lastSearchTime` is within the past 48 h — proof the daily sweep actually
attempted a search ("5 whole continuous days despite 1 or more attempts").

Profile/monitoring writes use `PUT /api/v3/movie/editor` (`MovieEditorResource`),
verified against Radarr v6.1.1 source to null-skip absent fields — a swap sends
only `{movieIds, qualityProfileId, moveFiles: false}`, an unmonitor only
`{movieIds, monitored: false}`.

| Condition | Action |
|---|---|
| Eligible, `days` reaches **5**, stage 0 | Record `original_profile_id` → PUT `qualityProfileId` = Fallback HDTV → POST `MoviesSearch [id]` → Discord info |
| Still missing, `days` reaches **10**, stage 1 | PUT profile = Fallback SD → search → Discord info |
| Still missing, `days` reaches **15**, stage 2 | PUT profile = original → set `monitored = false` → `parked = true` → Discord warning ("unfindable — manual intervention needed") |
| Left the missing list while stage ≥ 1 (grab landed) | PUT profile = original → delete state record → Discord success ("grabbed at fallback quality; will auto-upgrade when a better release appears") |
| Left the missing list while stage 0 | Delete state record silently (normal grab) |
| Parked item is re-monitored by operator | Clear `parked`, reset `days`/`stage` to 0 — fresh cycle |
| Current profile ≠ the fallback profile we set | Operator override: delete state record, change nothing |
| Movie no longer exists in Radarr (deleted) | Delete state record; nothing to restore |

### TV (alert-only)

Same day-counter over sonarr/sonarr2 `wanted/missing` aired episodes. When an episode
crosses 5 days it joins a Discord digest ("TV fallback candidates — examine for v2"),
grouped by series, alerted **once per item** (an `alerted` flag in state prevents daily
spam). No profile or monitoring changes are ever made to TV in v1.

## Component 3 — Safety rails

- Both fallback profile IDs are resolved by name at the start of every run; if either is
  missing on an instance, all swaps for that instance abort and Discord gets an error.
- The orchestrator only ever writes two movie fields: `qualityProfileId` and `monitored`.
  It never creates, edits, or deletes quality profiles.
- Concurrency cap: at most **25** movies in fallback (stage ≥ 1) per instance. Overflow
  items keep counting days but wait for a slot; the cap event is logged.
- Failures notify Discord via `scripts/maint/lib/notify.py`; successful cron runs push a
  Kuma heartbeat via the maint-pusher pattern (same as `qflix-missing-search`).

## Component 4 — Library changes

`scripts/mcp/lib/arr_client.py` gains a `put()` method (currently get/post/delete only).
Same `(status_code, body)` contract as the others.

## Testing

`tests/unit/test_quality_fallback.py` with a fake ArrClient (no network). Table-driven
state-machine coverage:

- day-5 promotion, day-10 deepening, day-15 park (restore + unmonitor + parked flag)
- grab-and-restore at stage 1 and stage 2
- stage-0 grab clears state silently
- operator-override detection (profile mismatch → hands off)
- unreleased (`isAvailable == false`) accrues no days
- calendar-date dedup (same-day double run counts once)
- concurrency cap holds at 25; overflow promotes when a slot frees
- TV digest fires once per item, never repeats

## Interaction with existing work

- **Task #1** (perpetually-unfindable items): this design subsumes the movie half —
  day-15 parking ends the infinite re-search burn on radarr/radarr2. Sonarr's ~33
  perpetual episodes remain and surface in the TV digest for the v2 decision.
- **Upgradinatorr** (`scripts/post-import/upgradinatorr.sh`): untouched; it is the
  upgrade path that makes restore-after-grab self-healing.
- **Recyclarr**: untouched; fallback profiles live outside its managed set.
- **missing.py**: untouched; the orchestrator reads its effects (the wanted/missing
  list) rather than hooking into it.

## Rollout

1. Run configure script against radarr + radarr2; verify profiles in UI.
2. Deploy orchestrator + units; first run with `--dry-run` reviewed by operator.
3. Enable timer. State warms up; first possible promotion is 5 days after enablement.
