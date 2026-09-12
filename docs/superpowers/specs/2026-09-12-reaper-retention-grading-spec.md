# Stage-0 spec (corrected) — qflix-reaper retention grading

**Status:** ready to implement. Supersedes the arch-tier council spec of
2026-09-12, which routed back to Stage 0 after 3 of 4 candidates shipped the
same blocker. This document exists so the next implementation pass does not
re-discover, at ~2M tokens, what that run already paid for.

**Subject:** `scripts/maint/qflix-reaper.py` — destructive automation. It deletes
member-visible media via Sonarr/Radarr.

**Baseline:** HEAD `ac0deb1` + PR #21 (the Futurama pin). Reaper tests `79
passed`; `qflix-audit.py` → 0 enforced.

---

## 1. The defect, stated only as far as it is established

The reaper grades retention on the **Plex container `addedAt`** — the `show`
object for TV, the `movie` object for film — and that clock is **not the arrival
time of the current files**.

On 2026-09-12T05:17:57Z (UTC) it deleted Futurama 70 minutes after the operator
requested it, grading `addedAt=1739683831` (2025-02-16) as 572 days against a
45-day threshold, for files 67 minutes old.

### What was REFUTED — do not rebuild on it

- **"The Plex item survived the earlier reap."** False. `rk 7884`/`arrId 169`
  (09-05) and `rk 9371`/`arrId 271` (09-12) are two distinct objects. The clock
  never moved backwards within one row.
- **"`addedAt` derives from file mtime."** False. Vanderpump Rules' oldest file
  mtime is 2016-01-16, Debris 2021-04-15 — all with 2026 `addedAt`.
- **"Same tmdbId across libraries identifies a duplicate."** False, and relevant
  here only as a warning: TMDB namespaces ids by medium.

### What is ESTABLISHED

- The new container row was created ~04:09Z **already carrying a 2025
  timestamp**, during a degraded agent match (`Match request for 'Futurama'
  failed`, 04:09:19Z UTC).
- Plex demonstrably writes provider-supplied back-dated `added_at`: row
  `id=9374`, `created_at` 2026-09-12T04:09:31Z, `added_at` 2016-06-08.
- Container and leaf clocks disagreed by 573 days on the same item.

### What is UNPROVEN

That the back-dating came from the metadata agent. The row is gone.
**The design must not depend on the mechanism.** It must be correct whatever
writes a bad container clock.

---

## 2. Authoritative clock

| Rank | Source | Role |
|---|---|---|
| 1 | `*arr` `episodeFile`/`movieFile` `dateAdded`, max over files | **Authoritative** — the *arr placed the file and is already the delete authority the reaper resolves against |
| 2 | Plex **leaf** `addedAt` (max over `/allLeaves`; the item itself for movies) | **Corroborating** — independent path, agrees 83/83 live, already fetched for sizing |
| 3 | `max(st_mtime, st_ctime)` over `Media.Part.file` | **Floor only** — `ctime` cannot be back-dated via any API |
| ✗ | Container `addedAt` | **Rejected as the grading input** |

`mtime` alone is rejected outright: Tdarr's 24/7 re-encodes rewrite it, which
would reset the retention clock forever and nothing would age out.

---

## 3. THE BLOCKER THE LAST RUN SHIPPED — the requirement it was missing

Three of four candidates declared `--min-file-age-days` with no clamp while
their own `--help` claimed the floor "cannot be switched off". `0` or negative
graded a file touched 1.2 seconds ago as deletable. **Two shipped a regression
test that passed only because it pinned the floor to exactly `0.0`** — the one
value where strict `>` still rejects.

**REQ-CLAMP (normative).**
- `MIN_FILE_AGE_FLOOR_DAYS` is a module constant, not a default.
- The effective value is `max(MIN_FILE_AGE_FLOOR_DAYS, requested)`. The flag is
  **raise-only**; it can never lower the floor.
- A request below the floor is accepted, clamped, and **logged loudly** at WARN
  naming both values. It is never silently honoured and never fatal.
- `--help` must describe the actual clamped behaviour.
- Tests must assert the clamp at `-1`, `0`, `0.0`, `0.5` and a value above the
  floor. **A test that only exercises `0.0` does not satisfy this.**

---

## 4. Normative requirements

**REQ-GRADE.** Grade on rank-1 with rank-2 corroboration. Where they disagree by
more than 24h, take the **newer** and record the disagreement in the manifest.

**REQ-FAIL-CLOSED.** If the grading clock cannot be determined for an item, that
item is **withheld from deletion**, counted, and named. Withholding is never
silent.

**REQ-LOUD.** `scripts/canaries/quota.sh` fires `qflix-reaper.py --execute` at
90% autonomously. Any run that withholds candidates must say so in its exit
message and its Kuma push, or the emergency reclaim silently no-ops toward the
98% operator-intervention wall.

**REQ-PIN-REMOVABLE.** The fix must make the PR #21 Futurama pin unnecessary.
Removing it is part of the change, not a follow-up.

**REQ-NO-WEAKENING.** The existing envelope is untouched: dry-run default,
mandatory unique *arr resolve, `--max-items`/`--max-pct`, audit manifest, run
lock.

**REQ-DRY-RUN-IS-A-WRITE.** Dry-run writes the durable log and
`orphan-state.json`. Any on-box dry-run during development must redirect both
via `QFLIX_REAPER_LOG_DIR` and `QFLIX_REAPER_ORPHAN_STATE`.

---

## 5. Decisions the last run left UNRESOLVED — settled here

**D-1. Prefilter.** Use `OR(container-aged, leaf-aged)` as the cheap prefilter,
then grade survivors on the authoritative clock. Leaf-only silently drops the
Futurama shape before it can reach the withheld bucket.

**D-2. Manifest contract.** `candidates[].addedAt` keeps its current meaning
(the container clock, for comparability with the 50 existing manifests). Add
`gradedAt`, `gradeSource`, and `clockDisagreementSec` as new keys. Do not
redefine an existing key in a forensic artifact.

**D-3. Unstat-able sibling.** A sibling file that cannot be stat'ed makes the
**item** ungradeable → withheld under REQ-FAIL-CLOSED. "I could not look" is
never "it is old."

**D-4. Absence-assertion tests.** REQ that "every new test fails first" is
unsatisfiable for guards asserting a thing is absent. For those, the required
evidence is a **mutation test**: break the guard, show the test reds.

**D-5. `orphan-state.json` unlocked read-modify-write.** Pre-existing at HEAD,
out of scope here, and **must be filed as its own task** rather than folded in.

---

## 6. THE TRADE — needs an operator ruling before implementation

Newest-file grading means **an actively-airing series never ages out while it
airs.** Measured container-age → newest-file-age today:

| Title | container | newest file |
|---|---|---|
| Law & Order | 34.6d | **0.6d** |
| Star Trek: Strange New Worlds | 34.4d | **2.4d** |
| Ted Lasso | 34.5d | **3.6d** |
| Vanderpump Rules | 36.0d | 34.5d |

Vanderpump Rules was **425 GB** at its previous reap. Options:

- **(A) Pure newest-file.** Honest against "45 days" as members understand it.
  Retention pressure moves onto the 90% quota path.
- **(B) Newest-file with a per-item cap** — e.g. never retain beyond N days from
  the container clock regardless.
- **(C) Per-season grading.** Finished seasons age out while the airing one does
  not. Most correct, most work.

**The operator promises members 45 days. Whatever is chosen must not delete
anything before 45 real days of residency.** That constraint is the point of
this change; the storage consequence is the cost of keeping the promise.

---

## 7. Gates

```
tests/.venv/Scripts/python.exe -m pytest tests/unit/test_qflix_reaper.py \
    tests/unit/test_reaper_e2e.py tests/unit/test_reaper_series_sizing.py -q
PYTHONPATH=scripts/maint tests/.venv/Scripts/python.exe scripts/maint/qflix-audit.py
```

pytest from the **Bash tool only**. Run every gate **after `git add`**. Required
regression test: the exact Futurama manifest shape
(`addedAt=1739683831`, `arrId=271`, `rk=9371`) must be **withheld**, not deleted.
