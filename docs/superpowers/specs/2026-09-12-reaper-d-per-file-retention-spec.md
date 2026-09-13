# Spec — per-file retention (option D), the `permanent` flag, and cross-system status

**Status:** operator-confirmed 2026-09-12. Supersedes
`2026-09-12-reaper-retention-grading-spec.md` §6, which posed A/B/C; the operator
defined **D** instead and it is now the design.

**Subject:** `scripts/maint/qflix-reaper.py` (destructive) plus a new canary.

---

## 1. The promise, stated exactly

> A file is available to a member in Plex for **45 days from the moment it is
> imported into Plex**.

Time spent downloading before that does **not** count against the member. Total
residency on disk may therefore exceed 45 days. Whether a show is airing,
finished, popular or obscure is **not a consideration** and must not appear
anywhere in the grading logic.

The service is deliberately ephemeral: anything can be asked for, it shows up,
and it clears away so that everyone can keep asking for anything.

---

## 2. Retention is PER FILE

**R-1.** The unit of retention is the **episode file** (Sonarr) or **movie file**
(Radarr), never the series.

**R-2.** Grading clock = that file's **Plex leaf `addedAt`** (import time),
corroborated by the *arr's `episodeFile`/`movieFile` `dateAdded`. They agree on
83/83 items measured live. Where they differ by >24h take the **newer** and
record the disagreement.

**R-3.** `mtime` is REJECTED as the clock — Tdarr's 24/7 re-encodes rewrite it
and would reset the retention clock forever.

**R-4.** The Plex **container** `addedAt` is REJECTED as the clock. It is the
defect that deleted Futurama 70 minutes after it was requested.

**R-5.** Deleting a file MUST unmonitor that episode in the same operation.
A monitored episode with no file is re-grabbed immediately — an infinite
download loop on content we just expired. **This is a hard requirement, not an
optimisation.**

---

## 3. The `permanent` flag

**P-1. Signal = a Sonarr/Radarr tag named `permanent`.**
Rejected: `monitorNewItems`, which is already `all` on all 37 series and
therefore discriminates nothing. Existing tags are Seerr requester usernames
(`quadstronaut`, `jessirigby`, `brintonasylum`, `cupid_rays180`); a series may
hold several tags, so there is no collision.

**P-2. It exempts the SERIES RECORD, never the files.** Files always expire at
45 days. The record survives so future episodes keep arriving without anyone
re-requesting them.

**P-3. Set three ways, all programmatic:**
- by the operator in the Sonarr UI;
- by an operator command (`--set-permanent <title|tvdb:N>`);
- **automatically** for any series whose status is **not ended** (`ended=false`
  / `status=continuing`). An unfinished show is never removed, so it can
  surprise members with new episodes.

**P-4. Removal is ONE condition only:** the last file of an **ended** series has
expired → remove the series record from the *arr. Ended + empty. Nothing else
removes a record.

**P-5.** Named permanents to be tagged this session: **South Park, Family Guy,
American Dad** (plus Futurama, which qualifies automatically as `continuing`).

---

## 4. Cross-system status — the half the reaper never did

**Measured 2026-09-12 in Seerr's own DB:**

```
season.status   1→357   3→22   4→8   5→32   7→66
media.status    1→20    3→27   4→13  5→71   7→27
28 distinct shows carry status-7 seasons; Law & Order alone has 23.
763 season_request rows still reference status-7 seasons.
```

Status `7` = DELETED. Status `1`/absent = never requested, cleanly
re-requestable.

`reconcile_seerr()` exists to stop reaped titles being stuck un-re-requestable —
its own comment says so — but it pages `filter=available` only, so **rows that
land in DELETED are invisible to it and are never cleared.** The operator's
father could not request Law & Order seasons 3 and 4; an admin had to push them
through by hand. That is one of 28 affected shows, not an isolated complaint.

**S-1.** When a file is expired, every system must end in a state that permits a
member to re-request it: Seerr season/media row cleared to a requestable status,
*arr episode unmonitored, Plex entry gone.

**S-2.** Reconciliation must sweep **all** statuses, not `filter=available`.

**S-3.** Correctness here is **continuous, not a side effect of a reap.** It gets
its own canary, own timer, own Kuma check (operator design law). The reaper may
act; only the canary may *assert*.

---

## 5. Safety envelope — unchanged and non-negotiable

Dry-run default; mandatory unique *arr resolve; `--max-items` / `--max-pct`;
audit manifest; run lock. **REQ-CLAMP** from the superseded spec carries over
verbatim: any minimum-age floor is a module constant, the flag is **raise-only**
(`max(FLOOR, requested)`), a below-floor request is clamped and logged loudly,
and tests must exercise `-1`, `0`, `0.0`, `0.5` and above-floor. A test that
only pins `0.0` does not satisfy it — that is exactly how the last attempt
shipped a fail-open floor with a test that appeared to prove the opposite.

**Fail closed:** if the grading clock for a file cannot be determined, that file
is withheld, counted, and named. Withholding is never silent — `quota.sh` fires
the reaper autonomously at 90%, so silent withholding walks the box to the 98%
wall.

---

## 6. Acceptance — Futurama is the testbed

Series id 272, `ended=false`, `status=continuing`, S01 9/9 files (requested live
2026-09-12, Seerr request 3217).

1. Futurama acquires the `permanent` tag automatically (continuing).
2. An S01 file past 45 days is deleted **and its episode unmonitored**; Sonarr
   does **not** re-grab it. *(This is the one behaviour asserted from general
   Sonarr knowledge rather than measured — it MUST be proven live before ship.)*
3. Futurama's series record survives at zero files.
4. An **ended** series at zero files loses its record.
5. A reaped season returns to a requestable status in Seerr.
6. Regression: the exact Futurama manifest shape (`addedAt=1739683831`,
   `arrId=271`, `rk=9371`) is **withheld**, not deleted.
7. The 66 existing status-7 seasons across 28 shows are reconciled.

**Ships only after council review** of the cross-system interaction — the
operator's explicit requirement.
