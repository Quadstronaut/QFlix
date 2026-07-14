# QFlix Reaper — Orphan Grace Window — Design Spec

- **Date:** 2026-07-14
- **Status:** Approved (brainstorm complete; pending implementation plan)
- **Owner:** Quadstronaut
- **Branch:** `master` (direct commits, this repo's convention; small labelled commits)

## 1. Purpose

Stop a single un-resolvable orphan from red-alerting `qflix-reaper` twice daily
forever.

An **orphan** is a Plex item that has aged past the 60-day threshold but does not
resolve to exactly one `*arr` id (either no backing `*arr` record, or missing
external guids). By design the reaper NEVER deletes what it can't positively map
to one `*arr` id — that safety rail is correct and stays. The bug is only in the
*alerting*: an orphan sets the same `partial` flag as a transient operational
failure, so it pages ERROR + Kuma-down on **every** run (dry-run 00:45 UTC +
execute 05:00 UTC) until a human clears it.

Triggering incident: 2026-07-14, "Frieren: Beyond Journey's End" (anime series,
files removed out-of-band, `sonarr2` had 0 series → UNRESOLVED). Diagnosed +
cleared manually; this spec prevents the recurrence class.

## 2. Non-Goals

- **Not weakening the safety rail.** An orphan is still never deleted. Resolution
  is still mandatory before any DELETE.
- **Not auto-clearing orphans.** The reaper never touches un-resolvable media on
  disk or in Plex. Clearing an orphan remains a human/operator action.
- **Not a new monitor / Kuma definition.** Reuses the existing `qflix-reaper`
  push token and notify channel.
- **Not a systemd unit change.** New behavior ships with defaults such that the
  existing `.service`/`.timer` files need no edit.
- **Not flock-guarded state.** Orphan-state read-modify-write is best-effort
  (see §9).

## 3. Core Idea

Separate two failure classes that today both set `partial`:

1. **Operational** — DELETE failed, Seerr delete failed, Plex refresh/emptyTrash
   failed, arr-client build failed, could-not-list-items. Transient, actionable
   → still page ERROR / Kuma-down (unchanged).
2. **Orphan UNRESOLVED** — persistent state → put on a **grace clock**:
   - **fresh** (first observed ≤ 24h ago) → reds exactly like today, so the
     operator learns about newly-stranded media.
   - **known** (first observed > 24h ago) → run goes **green**; the orphan is
     surfaced via `--json`, the durable log, and the Kuma "up" message, plus a
     **weekly WARN reminder** so it isn't silently forgotten on disk.

## 4. Components

### 4.1 Orphan identity — `_orphan_key(item) -> str`

Stable key preferring external ids so it survives Plex ratingKey churn:

- series → `"tvdb:<id>"` if `tvdbId` present
- movie  → `"tmdb:<id>"` if `tmdbId` present
- fallback → `"plex:<ratingKey>"` (covers items with missing external guids — a
  distinct UNRESOLVED cause that must still be tracked)

Each tracked orphan also carries `title` + `library` for human-readable messages.

### 4.2 State store

`~/.opt/maint/reaper/orphan-state.json` (beside the durable per-day logs).
Override via env `QFLIX_REAPER_ORPHAN_STATE` or `--orphan-state <path>` (tests
point this at a temp file).

```json
{
  "version": 1,
  "orphans": {
    "tvdb:424536": {
      "title": "Frieren: Beyond Journey's End",
      "library": "QFlix - Anime",
      "first_seen": "2026-07-14T00:45:07Z",
      "last_seen":  "2026-07-14T05:00:08Z",
      "last_warned": "2026-07-14T05:00:08Z"
    }
  }
}
```

Read/write is **best-effort**, same philosophy as `_setup_file_log`: any error
degrades to in-memory-empty and never breaks the delete job. A corrupt or
unreadable file is treated as **empty** — so every current orphan looks *new* and
reds. Fail **toward** alerting, never toward silence.

### 4.3 Reconciliation — `reconcile_orphans(current, now, grace_hours, remind_days, state_path)`

`current` = list of `{key, title, library}` observed this run. Returns
`(fresh, known, warn_due)` lists.

1. Load prior state (best-effort → `{}` on any failure).
2. For each current orphan key: set `first_seen`=`last_seen`=`now` if new; else
   keep `first_seen`, refresh `last_seen`. Update `title`/`library`.
3. **Drop** any prior key not in `current` (orphan resolved → forgotten; a later
   re-appearance restarts the grace + alert cycle).
4. Classify each current orphan by `age = now - first_seen`:
   - `age <= grace_hours` → **fresh**
   - `age >  grace_hours` → **known**
5. For each **known** orphan: if `last_warned` missing or
   `now - last_warned >= remind_days` → add to **warn_due** and set
   `last_warned = now`.
6. Persist updated state (best-effort). Return `(fresh, known, warn_due)`.

Runs in **both** dry-run and execute paths (both enumerate + observe orphans).
Time-based grace is idempotent across the twice-daily cadence and ad-hoc manual
runs — `first_seen` is stamped once and never moved.

### 4.4 Run color — `classify_run(operational_partial, fresh, known, warn_due) -> (rc, notify_level, message)`

Small pure helper so the decision is unit-testable without driving a whole run.

| condition | rc | notify level | Kuma |
|---|---|---|---|
| `operational_partial` **or** `fresh` non-empty | `EXIT_PARTIAL` (1) | `error` | down |
| `known` non-empty, `warn_due` non-empty | `EXIT_OK` (0) | `warning` | up |
| `known` non-empty, `warn_due` empty | `EXIT_OK` (0) | none | up |
| all empty | `EXIT_OK` (0) | `info` (existing success) | up |

- ERROR message enumerates operational issues **and** fresh orphans (distinct,
  precise wording — not the old catch-all).
- WARN message lists the stranded known orphans (title + library + age).
- No new exit code: known-orphan-only is a green run (`EXIT_OK`), so the oneshot
  unit + Kuma go green. The distinction lives in notify level, Kuma message text,
  the durable log, and `--json`.

### 4.5 Wiring into `run()`

- Enumeration loop UNRESOLVED branch (currently `qflix-reaper.py:907-911`): stop
  setting `partial`; instead `orphans_seen.append({key, title, library})`.
  Missing-guid items hit the same branch and key as `plex:<ratingKey>`.
- Operational failures keep setting `partial` (unchanged).
- **Dry-run path** (`:986-993`) and **execute summary** (`:1054-1067`) both call
  `reconcile_orphans` → `classify_run`, then notify / push Kuma / choose exit.
  Dry-run intent preserved: a **fresh** orphan still returns `EXIT_PARTIAL`; a
  **known** orphan lets dry-run go green.

### 4.6 `--json` additions

Add to both the dry-run plan JSON and the execute result JSON:

```json
"orphans": [
  {"key":"tvdb:424536","title":"...","library":"QFlix - Anime",
   "first_seen":"...","age_hours":123.4,"state":"known"}
],
"orphan_counts": {"fresh":0,"known":1}
```

Feeds the dashboard / ops so stranded media is visible without reading logs.

### 4.7 CLI flags (all defaulted — no unit edits required)

- `--orphan-grace-hours` (float, default `24`)
- `--orphan-remind-days` (float, default `7`)
- `--orphan-state <path>` (default env `QFLIX_REAPER_ORPHAN_STATE` →
  `~/.opt/maint/reaper/orphan-state.json`)

## 5. Data Flow

```
enumerate libraries
  └─ item aged > threshold, not excluded, resolve → None
        └─ orphans_seen.append({key,title,library})     (no partial set)
  └─ operational failure (delete/seerr/plex/arr)
        └─ partial = True                                 (unchanged)
after all libraries:
  reconcile_orphans(orphans_seen, now, ...) → (fresh, known, warn_due)   (state persisted)
  classify_run(partial, fresh, known, warn_due) → (rc, level, msg)
  notify(msg, level?) ; push_kuma(up/down, msg) ; return rc
```

## 6. Testing (TDD)

Extend `tests/unit/test_qflix_reaper.py` (importlib-loaded module, FakeArr +
monkeypatched Plex boundaries, temp `--orphan-state`):

- `_orphan_key`: series→tvdb, movie→tmdb, missing-guid→plex fallback.
- `reconcile_orphans`:
  - new orphan → `fresh`; state written with `first_seen==last_seen==now`.
  - `first_seen` 25h ago → `known`, not `fresh`.
  - prior key absent from `current` → dropped from persisted state.
  - `last_warned` 8d ago (known) → in `warn_due`, `last_warned` advanced.
  - `last_warned` 1d ago (known) → not in `warn_due`.
  - corrupt/missing state file → all current orphans `fresh` (fail-toward-alert).
- `classify_run` truth table: the four rows of §4.4.
- **Regression:** existing "UNRESOLVED → exit 1" test stays green — with an empty
  temp state, the orphan is `fresh` ⇒ `EXIT_PARTIAL`.
- **Regression:** known-orphan-only execute run → `EXIT_OK`, delete fns never
  called, Kuma "up".

## 7. Documentation

- `README.md` reaper section — describe the grace window + weekly reminder.
- `docs/` reaper references / `inventory.md` — note the new state file + flags.
- Memory `reaper-orphan-unresolved-redloop` / `reaper-maxpct-cap-disabled` —
  cross-link the new behavior.

## 8. Backward Compatibility

- Existing systemd `.service`/`.timer` unchanged (defaults cover it).
- Exit-code contract preserved: `1` partial, `2` cap, `3` fatal; known-orphan-only
  is `0` (new green case, not a new code).
- First run after deploy: empty state ⇒ any existing orphan is `fresh` ⇒ reds once
  more, then ages into `known` and greens within 24h. Expected, self-healing.

## 9. Accepted Limitations

- Orphan-state RMW is not flock-guarded. Reaper runs are serialized in practice
  (timers 4h apart; manual runs rare) and this is low-stakes observational state;
  last-writer-wins is acceptable. Mirrors the file-log's best-effort stance.
- A manual dry-run can consume that week's WARN slot or advance `last_seen`.
  Harmless (operator is present) and idempotent.
