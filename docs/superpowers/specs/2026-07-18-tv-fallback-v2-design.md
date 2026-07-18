# QFlix TV Fallback v2 — Specials Policy + Park-Only — Design

**Date:** 2026-07-18
**Status:** Designed (pending implementation)
**Supersedes:** the "TV is alert-only; v2 decided from v1 data" deferral in
`docs/superpowers/specs/2026-06-06-quality-fallback-design.md`.
**Scope:** Two **independent, compartmentalized** deliverables —
(A) a standalone Season-0 specials janitor with its own module, timer, and Kuma
check; and (B) park-only auto-unmonitor for genuinely-unfindable regular
episodes, added to the existing `quality_fallback` orchestrator.

## Background / decision trail

Two live notifications kicked this off:

- `[radarr] fallback stage 1 (HDTV): My Butt Has a Fever — day 5, searching` —
  the movie orchestrator working as designed; interactive search returns **0
  releases** at any indexer, so it will ride to day-15 auto-park. Not a bug.
- `[WARNING] TV fallback candidates (alert-only, v2 decision data): Ted Lasso
  S00E01–06,E10 …` — the v1 alert-only TV digest, doing exactly its job:
  surfacing decision data for v2.

Investigation of the full live stuck-TV set found it is almost entirely **Season
0 specials** (Ted Lasso Apple TV+ promo featurettes; Chainsaw Man "Omnibus"
recaps and "Chainsaw Days" chibi shorts) plus one queue stall (Graham Norton
S33E12, since unstuck). Interactive searches confirmed these specials have **zero
obtainable releases at any quality** — the results are always *other* episodes,
all rejected. One item (Chainsaw Man "Reze Arc") is a real film already present
in the library at WEBDL-1080p via a Radarr movie; its Sonarr S00 entry was a
phantom duplicate.

Key constraint that shapes the mechanism: **Radarr quality profiles are
per-movie; Sonarr quality profiles are per-series.** There is no per-episode
profile in Sonarr, so the movie trick (swap *this item's* profile to a fallback,
search, restore on grab) has no clean per-episode TV equivalent — loosening one
stuck episode means loosening the whole series (blast radius). Combined with the
evidence that loosening grabs nothing for release-less specials, quality
loosening on TV is rejected.

Operator decisions (2026-07-18):

- **Mechanism:** park-only. The loosen ramp is rejected (series-level profiles +
  provably zero yield for release-less items).
- **Specials:** systematically ignored. The janitor is a **STANDALONE** module
  with its **own timer** — deliberately *not* folded into `quality_fallback` — so
  it stays compartmentalized and independently swappable/tunable as QFlix
  migrates to larger servers. Cadence will be tuned from usage metrics, decoupled
  from the fallback cadence.
- Live remediation already applied manually on 2026-07-18: the stuck specials
  were unmonitored and both affected series' Season-0 flags cleared; Graham
  Norton was unstuck (blocklist + research).

## Component A — Specials-policy janitor (standalone)

**Principle:** Season 0 (specials) is never monitored on QFlix. Specials rarely
have standalone releases and otherwise sit perpetually-missing — burning indexer
queries and generating false-red / alert noise.

**Module:** `scripts/mcp/specials_policy.py`. Stateless, convergent enforcement
(no day-counting, no state file — every run re-asserts the invariant).

**Modes:** `--cron | --emit-json | --dry-run`, matching `missing.py` /
`quality_fallback.py` conventions. `--slug <name>` limits to one instance.

**Targets:** `sonarr`, `sonarr2`.

**Logic (per instance, idempotent):**

1. `GET /series`.
2. For each series whose Season 0 is `monitored == true` **or** has any monitored
   S00 episode:
   - Unmonitor all monitored S00 episodes: `PUT /episode/monitor
     {episodeIds, monitored: false}`.
   - Clear the season flag: `PUT /series/{id}` with
     `seasons[seasonNumber == 0].monitored = false`.
     *(Durability: a series refresh re-monitors episodes to match the season
     flag, so episode-only unmonitoring silently regresses; clearing the flag is
     what actually prevents recurrence.)*
3. Efficiency: only `GET /episode?seriesId=` for series whose Season 0 has
   `statistics.totalEpisodeCount > 0` or `season0.monitored == true`.

**Notifications:** info-level summary **only when it changed something** (series +
episode counts); silent when already clean; error-level on API failure. A
successful `--cron` run pushes a Kuma heartbeat (maint-pusher pattern) backing its
**own Kuma monitor**.

**Timer:** `qflix-specials-policy.{service,timer}`, **daily 06:00 UTC** — ahead of
the 07:00 missing sweep and 07:30 `quality_fallback`, so specials are unmonitored
before any search wastes queries on them. Cadence independently tunable.

**Install:** `scripts/configure/73b-specials-policy-install.sh` (mirrors
`73-quality-fallback-install.sh`): deploy module + unit + timer, enable the timer,
register the Kuma monitor via `bootstrap-kuma-monitors.py`.

**Compartmentalization:** fully independent of `quality_fallback` — own module,
timer, (no) state, tests, and Kuma monitor. Can be disabled, retuned, or moved
between instances without touching the fallback orchestrator.

## Component B — TV park-only (in `quality_fallback.py`)

TV goes from *zero* sonarr writes to exactly one write type: **unmonitor**
(park). No quality loosening, no Sonarr fallback profiles, no series-profile
swaps.

**Eligible population** (unchanged day-counter plus one new filter): `wanted/
missing`, `monitored == true`, **`seasonNumber != 0`** (defensive — the janitor
already removes S00, but the filter *decouples* the two components so the park is
correct even if the janitor timer is lagging or disabled), aired
(`airDateUtc <= now`), fresh-searched (`lastSearchTime` within
`SEARCH_FRESH_HOURS` = 48).

**State machine (`plan_tv`), per episode:**

| Condition | Action |
|---|---|
| Eligible, `days` reaches 5 (`PROMOTE_DAYS`), not yet alerted | Join the day-5 Discord digest (info); set `alerted`. Reworded from "v2 decision data" → "still missing, auto-parks at day 15 if unfound" |
| Eligible, `days` reaches 15 (`PARK_DAYS`), not yet parked | Unmonitor the episode (`PUT /episode/monitor`); set `parked`; Discord **warning** ("UNFINDABLE after 15d — unmonitored, manual intervention needed") — subject to the per-run blast cap |
| Left the missing list (grabbed or unmonitored) | Prune the state record (existing logic) |
| Operator re-monitors a parked episode | Re-enters missing → fresh day-0 record via prune-and-recreate |

**Blast cap:** `MAX_TV_PARKS_PER_RUN` (default 10). Overflow episodes keep their
day count and park on a later run — guards against a bug mass-unmonitoring a show.

**State schema:** TV record `{days, last_counted, alerted}` →
`{days, last_counted, alerted, parked}`.

**Apply layer:** new `_apply_tv_park(client, episode_ids)` →
`PUT /episode/monitor {episodeIds, monitored: false}`. `run()` applies parks and
emits per-episode warnings, mirroring the movie apply loop. `--dry-run` writes
nothing.

**No new timer:** rides the existing `qflix-quality-fallback.timer` (07:30 UTC).

## Testing

**Component A — `tests/unit/test_specials_policy.py`** (fake ArrClient, no network):

- unmonitors monitored S00 episodes and clears the Season-0 season flag
- idempotent no-op when already clean
- `--dry-run` performs no writes
- empty instance (e.g. sonarr2 with 0 series) handled without error
- only S00 is touched; S01+ monitoring is untouched

**Component B — extend `tests/unit/test_quality_fallback.py`:**

- day-5 digest fires once per episode
- day-15 park: unmonitor emitted + `parked` set + warning notified
- park fires once, never repeats
- re-monitor → fresh cycle (day-0)
- `seasonNumber == 0` excluded from the park flow
- blast cap holds; overflow defers to a later run

## Docs

- Update the `quality_fallback.py` module docstring (TV is no longer alert-only;
  park-only added).
- Note in `docs/superpowers/specs/2026-06-06-quality-fallback-design.md` that the
  TV-alert-only deferral is resolved here.
- Update `inventory.md` and relevant surface docs with the new module, timer, and
  Kuma monitor.

## Rollout

1. **Component A:** deploy module + unit + timer via the configure script;
   `--dry-run` reviewed; enable timer; register Kuma monitor. (The live sweep was
   already run manually on 2026-07-18, so the first cron is a no-op.)
2. **Component B:** deploy the updated `quality_fallback.py`; `--dry-run` reviewed
   by operator; state warms up; first possible park is 15 days after enablement.
3. Both deliverables are independently revertable.
