# QFlix Anime Library Janitor — Design Spec

- **Date:** 2026-07-24
- **Status:** Approved (brainstorm) — pending implementation plan
- **Owner:** Quadstronaut
- **Sibling of:** `qflix-reaper` (same safety envelope, same deploy shape)
- **Branch:** `feat/anime-janitor` (off master; ships dry-run, no `--execute` in the unit until trusted)

## 1. Purpose

Seerr routes anime requests to the dedicated anime *arr instances (Sonarr2 →
`~/media/Anime`, Radarr2 → `~/media/Anime Movies`) using keyword detection.
That detection misfires: **regular shows/movies get routed into the anime
instances**, so their hardlinks land in the Anime / Anime Movies libraries and
show up as anime in Plex.

This is a box-side, once-a-day janitor — **structurally identical to
`qflix-reaper`** — that scans the anime libraries for misclassified titles and
**re-homes** the confirmed non-anime ones to the main instances + libraries by
moving their hardlinks and moving the *arr record between instances. It also
**flags** (report-only) the reverse error: genuine anime that landed in the main
TV/Movies libraries.

The reaper deletes; this janitor **moves** — a strictly less destructive,
fully reversible operation — but it inherits the reaper's entire safety
envelope (dry-run default, caps, exclusions, durable ledger, Kuma, window-aware).

## 2. Non-Goals

- **Not a Seerr routing fix.** This corrects misroutes *after the fact*,
  reactively, exactly like the reaper. Fixing Seerr's anime keyword detection
  upstream is a separate concern (and may be impossible to make perfect).
- **Not a deleter.** It never deletes media. Worst case it moves a title to the
  wrong library, which the next run (or a manual exclude) corrects.
- **Not workstation-side.** Runs on manitoba via a systemd timer, on the box,
  daily, regardless of whether the workstation is on. (Explicit operator
  correction 2026-07-24: this is server-side, like the reaper.)
- **Not bidirectional auto-move.** Anime sitting in the *main* libraries is
  **flagged only**, never auto-moved (avoids yanking borderline Japanese titles
  out of the main libraries on a metadata hiccup).
- **Not a re-download.** Re-homing imports the *existing* moved files; it never
  re-grabs from indexers.

## 3. Topology (recap — canonical mapping from qflix-reaper)

| Plex library | *arr instance | slug | root folder | kind / id |
|---|---|---|---|---|
| `QFlix - TV` | Sonarr (main) | `sonarr` | `~/media/TV` | series / tvdb |
| `QFlix - Anime` | Sonarr2 | `sonarr2` | `~/media/Anime` | series / tvdb |
| `QFlix - Movies` | Radarr (main) | `radarr` | `~/media/Movies` | movie / tmdb |
| `QFlix - Anime Movies` | Radarr2 | `radarr2` | `~/media/Anime Movies` | movie / tmdb |

**Hardlink reality (this is what "move the links" means).** *arr import
hardlinks the download (`~/downloads/<cat>/<release>/…`) into the media library
folder. The library file and the download file are two directory entries on one
inode. Both anime and main roots live under `~/media` on **one filesystem**, so
moving a title's folder is a same-device `rename()` — instant, inode preserved,
qBit seeding untouched, no disk doubling. **The janitor MUST verify same
`st_dev` before any move and refuse (flag instead) if the target root is on a
different device** (that would silently double disk usage).

## 4. Classifier (genre + origin, confidence-tiered)

Source of truth is the *arr record itself (no Plex, no external DB). Sonarr v3
`series` and Radarr v3 `movie` objects both carry `genres: [str]` and
`originalLanguage: {id, name}`. Enumerate each library directly from its arr.

Config constant `ANIME_LANGS = {"Japanese"}` (default). Korean/Chinese
(`aeni`/`donghua`) are **out** by default — configurable via `--anime-lang`
(repeatable) if the operator wants them treated as anime.

For a title **in an anime library** (Sonarr2 / Radarr2):

| Genres | Origin lang | Verdict | Action |
|---|---|---|---|
| `Animation` **absent** | any | **live-action — not anime** | **AUTO re-home OUT** (high confidence) |
| `Animation` present | ∈ `ANIME_LANGS` | anime — correct | leave |
| `Animation` present | ∉ `ANIME_LANGS` | western/other cartoon | **FLAG only** (report) |
| `genres` empty/missing | any | **unknown** | **SKIP + FLAG** — never move |

For a title **in a main library** (Sonarr / Radarr) — reverse direction:

| Genres | Origin lang | Verdict | Action |
|---|---|---|---|
| `Animation` present | ∈ `ANIME_LANGS` | likely misplaced anime | **FLAG only** (report) |
| otherwise | — | fine | ignore |

**The auto-move trigger is deliberately the narrowest, highest-precision
signal: a live-action title (no `Animation` genre at all) has no legitimate
reason to sit in an anime library.** Everything softer is flagged for the
operator, mirroring the reaper's high-confidence-auto / ambiguous-defer split.

**Missing metadata is never grounds to move.** Empty `genres` (metadata not yet
fetched, or a provider blip) → skip + flag. This is the failure mode that could
mass-mislabel a whole library, so it is guarded twice: here, and by the cap in §6.

## 5. Actions

### 5a. AUTO re-home (non-anime out of an anime library)

Moving a series between two Sonarr instances is not a native operation; it is
scripted as add-target → move-files → import → remove-source, tracked in a
durable ledger so a crash mid-move is resumable. For a Sonarr2 series → Sonarr
(Radarr2 → Radarr is the exact analogue with `movie`/tmdb/`~/media/Movies`):

1. **Ledger `planned`**: append `{id_key, id, title, from_slug, to_slug,
   from_path, to_path, step, ts}` to `~/.opt/maint/anime-janitor/inflight.json`.
2. **Same-device guard**: `os.stat(from_root).st_dev == os.stat(to_root).st_dev`
   — else abort this item, flag it, ledger `skipped-crossdev`.
3. **Add to target** (`POST sonarr /series`): same `tvdbId`, `rootFolderPath =
   ~/media/TV`, `seriesType = "standard"` (anime instances use `"anime"`),
   `qualityProfileId` = target's default profile, `monitored` preserved,
   `addOptions.searchForMissingEpisodes = false`. Idempotent: if already present
   (re-run after a crash), reuse it.
4. **Move files**: `rename(from_path, to_path)` (same-device). `to_path` =
   `~/media/TV/<series.folder>` — the folder Sonarr expects.
5. **Rescan target** (`POST sonarr /command {name: RescanSeries, seriesId}`):
   imports the moved files into the new record.
6. **Remove from source** (`DELETE sonarr2 /series/{id}?deleteFiles=false&
   addImportListExclusion=false`). `deleteFiles=false` is load-bearing — files
   already moved; we only drop the stale record.
7. **Plex refresh** both affected sections (`QFlix - Anime` + `QFlix - TV`).
8. **Ledger `done`**; on any step failure ledger the failed step and continue to
   the next item (exit 1, partial — re-run resumes from the ledger).

**Reversibility:** nothing is deleted; the ledger records `from`/`to` for every
move, so a wrong move is undone by replaying it backwards (a future `--undo
<id>` convenience; manual reversal is always possible from the ledger).

### 5b. FLAG only (anime in a main library, or the ambiguous anime-lib cases)

No mutation. Collect into the run report + Kuma message + durable log:
`{lib, title, id, reason}`. Reasons: `anime-in-main-lib`,
`animation-non-jp-in-anime-lib`, `missing-metadata`, `crossdev-skip`. These are
the operator's manual-review queue.

## 6. Safety Envelope (inherited from qflix-reaper)

- **DRY-RUN IS THE DEFAULT.** No flags → enumerate, classify, print the plan +
  totals, mutate nothing (no add/move/delete/Plex-refresh, no ledger writes).
  The systemd unit ships in this mode. **Arm with `--execute`** via an ExecStart
  drop-in once the dry-run plan is trusted — same ritual as the reaper.
- **`--max-moves N`** (default 10): per-run rate limit on auto re-homes. Overflow
  **defers** the excess to the next run (forward progress), does not abort.
- **`--max-pct P`** (default 25): per-library tripwire. If auto-move candidates
  exceed P% of an anime library's title count, **abort the whole run before any
  mutation** (exit 2, page operator). This is the mass-mislabel circuit breaker:
  a metadata provider dropping `genres` fleet-wide would make everything look
  live-action; the tripwire catches it before it moves the entire library.
- **`--force`** overrides both caps (logged WARN); does **not** imply `--execute`.
- **Exclusions** `--exclude-file` (default `scripts/maint/qflix-anime-janitor.exclude`):
  `tvdb:<id>`, `tmdb:<id>`, or bare `title` (case-insensitive), `#` comments.
  Protects deliberate operator placements (e.g., a title they *want* in Anime).
- **Same-device guard** (§5a step 2): never turn a move into a disk-doubling copy.
- **Missing-metadata guard** (§4): unknown → skip, never move.
- **Idempotent + crash-safe** via the inflight ledger; re-running self-heals.

## 7. State, Logging, Kuma, Exit Codes (reaper-parity)

- **Durable log**: `~/.opt/maint/anime-janitor/anime-janitor-<date>.log`,
  best-effort (degrades to journal-only), 30-day retention. Journal on the
  shared box is unreliable — the logfile is the authority.
- **Ledger**: `~/.opt/maint/anime-janitor/inflight.json` (in-flight moves) +
  `moved.json` (completed history, for undo/audit).
- **Kuma**: `_push_kuma(status, msg)` → `http://127.0.0.1:42005`, key
  `qflix-anime-janitor` in `~/secrets/kuma-push-tokens.json`. `up` on a clean
  run (dry-run plan or execute with 0 failures), `down` on partial/cap/fatal.
  Best-effort, never raises. A **new** Kuma monitor `qflix-anime-janitor` is
  provisioned (distinct from the existing anime *routing* canary).
- **`--json`** structured summary for dashboards/newsletter.
- **Exit codes** (identical semantics to reaper): `0` clean · `1` partial
  (a per-item step failed; re-run resumes) · `2` cap trip (aborted, no mutation)
  · `3` fatal (can't reach an arr / read creds).

## 8. Deployment

- **Module**: `scripts/maint/qflix-anime-janitor.py` — stdlib-only, Python 3.9
  compatible (no f-string backslashes, no `match`), sibling-import `lib.secrets`
  (`scripts/maint/lib`) and `lib.arr_client` (`scripts/mcp/lib`) via the same
  `sys.path` nudge the reaper uses.
- **Units**: `manitoba-maint-anime-janitor.service` + `.timer`. Daily (offset off
  the reaper so they don't overlap Plex refreshes). Service ships **dry-run**
  (no `--execute`).
- **Window-aware**: honor the Monday maintenance window (`lib.window` /
  `suppression.in_pause_window`) — file moves + arr/Plex mutation are box ops and
  must not run 11:00–15:00 UTC Monday. A window hit → log + Kuma `up` "skipped
  (window)" + clean exit, like the rest of the Monday-aware cascade.
- **Install hook**: add to `scripts/configure/240-maintenance-install.sh`
  (or the equivalent installer) alongside the other maint units.

## 9. Implementation Phase 0 — Signal Validation (gate before writing move code)

Before any mutation logic is trusted, validate the classifier against **live**
data (this is cheap and de-risks the whole design):

1. `GET sonarr2/series` + `GET radarr2/movie`; confirm `genres` and
   `originalLanguage` are **populated** on real records.
2. Dump the classifier verdict for every current anime-library title in
   `--json` dry-run and **eyeball it with the operator** — confirm the
   auto-move set is exactly the live-action misroutes and nothing borderline.
3. Only after that review does `--execute` get armed on the box.

If `originalLanguage` proves unreliable/empty on the box's Sonarr version, fall
back to genre-only for the auto-move tier (live-action detection doesn't need
language — it keys on the *absence* of `Animation`), and language only gates the
softer flag tiers.

## 10. Failure Matrix

| Failure | Behavior | Reasoning |
|---|---|---|
| An arr unreachable | exit 3 fatal, Kuma down | can't classify safely |
| `genres` empty on a title | skip + flag, no move | never move on absent metadata |
| Auto-move candidates > `--max-pct` of a lib | abort before mutation, exit 2 | mass-mislabel circuit breaker |
| Target root on a different device | skip item + flag `crossdev` | don't double disk / risk seeding |
| Add-to-target succeeds, crash before remove-source | ledger resumes; next run removes source | crash-safe via inflight ledger |
| Move succeeds, Plex refresh fails | exit 1 partial; media is correct, Plex catches up next scan | Plex refresh is not load-bearing |
| Title in exclude file | never touched | operator override is absolute |
| Maintenance window active | skip whole run, Kuma up "window" | box-op suppression directive |

## 11. Open Questions (non-blocking; revisit post-merge)

- **CN/KR animation**: default JP-only. Flip via `--anime-lang Korean --anime-lang
  Chinese` if the operator wants donghua/aeni in the anime libraries.
- **Undo convenience**: `--undo <id>` replaying a `moved.json` entry backwards —
  ship only if a bad move actually happens; manual reversal is always available.
- **Seerr re-request reconciliation**: after a re-home the title's Seerr media
  record still points at the old server. Probably harmless (it's available
  either way); revisit if it causes duplicate-request weirdness.
- **Repeat-offender allowlist**: if Seerr keeps re-misrouting the same title,
  auto-add it to the exclude/known-non-anime set after N corrections so the
  janitor stops fighting the router. Deferred.
