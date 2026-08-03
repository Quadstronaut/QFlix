# Prowlarr indexer remediation — operator runbook (2026-08-03)

Two independent faults, both **config**, both invisible to every monitor that
existed when they were found. Nothing in this runbook is applied by any script:
each step is a change in the Prowlarr UI that only the operator makes.

Each step names the detector that locks it, so a fix cannot silently revert:

| Step | Fault | Detector that locks it |
|------|-------|------------------------|
| 1 | Knaben / Tokyo Toshokan grabs fail on a 403 proxy download | `scripts/canaries/prowlarr-app-sync.sh` predicate **P2** → `STAGE=prowlarr-appsync-magnet-pref-off` |
| 2 | LimeTorrents never reaches Radarr | `scripts/canaries/prowlarr-app-sync.sh` predicate **P1** → `STAGE=prowlarr-appsync-missing` |
| 3 | Dead-weight indexers on every sync cycle | none — cosmetic, counted as `skip_nocat` |

> **The app-sync canary is RED until step 1 is applied.** That is deliberate.
> The fault is live, the fix is one checkbox per indexer, and a detector that
> was born green on a broken system would be worth nothing. Step 2 clears the
> `missing` half.

## STATUS — steps 1 and 2 APPLIED 2026-08-03 09:05–09:23 UTC

Applied live via the Prowlarr/Radarr APIs, before the Monday window opened.
The canary now exits **0** with `magnet.violations: []` and `missing: []`.
Step 3 was **not** applied (cosmetic, and the `skip_nocat` count is the
intended signal for it).

Step 2 did **not** go the way this runbook predicted — see
[Step 2 · what was actually wrong](#2a-superseded--the-definition-is-not-buggy).
Rollback values for every mutation are in
[Rollback](#rollback--exact-prior-values).

---

## Step 1 — tick **Prefer Magnet URL** on Knaben and Tokyo Toshokan

**Highest value, two checkboxes, removes the whole failure chain.**

Prowlarr → **Indexers** → *Knaben* → **Show Advanced** → tick
**Prefer Magnet URL** → Save. Repeat for *Tokyo Toshokan*.

(Field name: `torrentBaseSettings.preferMagnetUrl`. Prowlarr indexer ids 21 and
38 at time of writing — match on the **name**, ids are not stable across a
re-add.)

### Why

Knaben results carry both a magnet **and** a Prowlarr proxy `downloadUrl` that
wraps `knaben.org/live/dl/rutracker/?...`. With `preferMagnetUrl = false` the
*arr takes the proxy URL. Prowlarr fetches it and gets **403 Forbidden** — a
26 KB HTML login wall, not a torrent. From there:

```
403 upstream
  → Prowlarr:  Error|Knaben|Downloading for release failed
  → Prowlarr:  Error|NewznabController|ReleaseDownloadException: Download failed
  → *arr gets: <error code="500" description="Download failed" />
  → repeated 500s trip PROWLARR's own ~15-minute failure backoff
  → *arr retry: <error code="429" description="Indexer is disabled till ... due to recent failures." />
  → Radarr logs: "API Grab Limit reached for <url>"
  → Radarr logs: "Couldn't add release '...' from Indexer Knaben (Prowlarr)"
```

**"API Grab Limit reached" is not a quota.** It is Radarr's hardcoded label for
*any* HTTP 429 on a download URL. `baseSettings.grabLimit` is `None` on all 14
enabled indexers, `limitsUnit` is `0`, and `/api/v1/indexerstatus` is `[]`.
There is no limit to raise, and nothing to ask the tracker about.

Measured on the live indexer: **100 % of Knaben results carry a magnet**
(83/83, 37/37, 74/74 across three queries), while only ~43 % carry the proxy
`downloadUrl` at all. Preferring the magnet removes the broken path entirely
without losing a single result.

### Do NOT

- **Do not remove Knaben.** `api.knaben.org` answers 200 / ~199 KB and a live
  cat-2000 search returns 73–83 results. Only its non-magnet download path is
  broken.
- **Do not chase the 504/522s.** Knaben also throws intermittent gateway errors
  (5× 504, 4× 522, 1× 500 in 24 h). Those are *search* flakiness on a healthy
  indexer and are **not** what breaks grabs — the 403 on the download proxy is.
- **Do not tick this on all twelve enabled torrent indexers.** All of them
  currently have it false and only these two have a diagnosed broken proxy
  path. The canary's `MAGNET_REQUIRED` list is deliberately named, not blanket.

### Verify (read-only)

```
GET /api/v1/indexer/21   → torrentBaseSettings.preferMagnetUrl == true
GET /api/v1/indexer/38   → torrentBaseSettings.preferMagnetUrl == true
```
or just run the canary: `bash scripts/canaries/prowlarr-app-sync.sh --json`

---

## Step 2 — LimeTorrents is absent from Radarr

Radarr holds 8 indexers and LimeTorrents is not one of them, while Sonarr has
it and it works there. Prowlarr retries the add every ~6 h
(`ApplicationIndexerSync`: 10:13 / 16:13 / 22:14 / 04:14) and fails every time,
2 warnings per cycle, 8/day. The same warning appears in the logs on
**2026-05-22**, so this has been running for at least ten weeks.

Not a category-config mismatch, not a disabled indexer, not a dead site:

- Prowlarr's own tag+category check **passes** (LimeTorrents declares 2000 and
  shares tag `general` with the Radarr app), so it never logs the benign
  `Skipping add ... due to no app Sync Categories` line.
- `https://www.limetorrents.fun/latest100` answers **200 / 76,320 bytes** and
  parses into 150 rows.
- Those 150 rows map to **8000 Other (116) + 5000 TV (34) and ZERO in 2000
  Movies**, although the titles are plainly movies.
- Radarr's add-time validation issues exactly that empty-term RSS query, sees
  nothing in its configured categories, and returns
  `400 "Query successful, but no results in the configured categories were
  returned from your indexer."` Prowlarr retries once with `?forceSave=true`,
  gets 400 again, and gives up.
- Keyword search on the same indexer is **fine**: `query=matrix` returns 40
  results, 33 of them in cat 2000.

So the defect is the bundled `limetorrents` Cardigann definition's `/latest100`
row → category mapping. Prowlarr is `2.5.2.5491` (branch `master`).

### 2a SUPERSEDED — the definition is not buggy

**The "refresh the definitions, it is probably fixed upstream" theory is
wrong.** Read on the live box 2026-08-03: `Definitions/limetorrents.yml` is
dated 2026-08-02 (Prowlarr re-syncs that folder; it was already current) and it
carries this setting, which is upstream *documenting the behaviour as intended*:

```yaml
  - name: info_category_8000
    label: About LimeTorrents Categories
    default: LimeTorrents only returns category <b>Other</b> in its
      <i>Keywordless</i> search results page. To pass your apps' indexer TEST
      you will need to include the 8000(Other) category.
```

Confirmed against the site, not just the YAML: `/latest100` and
`/browse-torrents/Movies/` both render the second `<td>` as a bare relative date
(`2 minutes ago`) with **no `in <Category>` text**, so the definition's
`category` selector regex `" in (.+?)[.]?$"` cannot match and every row falls to
the `default:` — `TV shows` if the title has `SxxEyy`, else `Other`. That is an
honest mapping of a site that genuinely does not categorise its keywordless
listing. There is no upstream fix to wait for.

Reproduced exactly as Radarr's add-time validation issues it:

```
GET /2/api?t=movie&cat=2000,…,2090          → 0 items   (Radarr 400s)
GET /2/api?t=movie&cat=2000,…,2090,8000     → 118 items (Radarr accepts)
GET /2/api?t=movie&q=matrix&cat=2000        → 33 items  (the actual value)
```

### 2a′ — what was applied: scope the 8000 exception to LimeTorrents alone

The runbook's **Do NOT widen Radarr's `syncCategories` to include 8000** still
holds and was honoured — widening application id 2 would have written cat 8000
into **all eight** of Radarr's Prowlarr indexers. The measurement behind that
DO NOT is now quantified: of the 121 items the keywordless feed returns,
**20 (16.5 %) are plainly XXX**, 2 are `SxxEyy` TV, 87 carry a year.

So the exception was scoped instead of broadened, using the same dedicated-tag
lever §2b already endorses:

1. New Prowlarr tag **`radarr-allow-other`** (id 4).
2. New Prowlarr application **“Radarr (LimeTorrents / Other)”** (id 6) —
   same Radarr target as app 2, `syncLevel: **addOnly**`, `tags: [4]`,
   `syncCategories` = app 2's eleven movie cats **+ 8000**.
   `addOnly` is load-bearing: a second `fullSync` application pointed at the
   same Radarr is the one shape that could reap the other eight indexers.
3. LimeTorrents tagged `[3, 4]` — tag 3 (`general`) **kept**, so the working
   Sonarr entry is never at risk. This is precisely the failure §2b warns about
   and it is why the tag was *added*, not swapped.
4. `ApplicationIndexerSync` → LimeTorrents added to Radarr, cats `[2000, 8000]`.
5. `enableRss = false` on **Radarr's LimeTorrents entry only**. The 8000 category
   exists solely to get past add-time validation; the indexer's real value to
   Radarr is its keyword search (33/40 in cat 2000), which runs on
   `enableAutomaticSearch` / `enableInteractiveSearch`. This keeps the 16.5 %-XXX
   feed out of the movie pipeline entirely. Stable because app 6 is `addOnly` —
   Prowlarr adds the entry and never rewrites it.

Net: only LimeTorrents' own Radarr entry ever sees cat 8000. Applications 1–4,
the other eight Radarr indexers, and Sonarr are byte-for-byte unchanged.

### Residuals — accepted, not fixed

- **Application 2 still retries and still fails**, 2 × `Warn|RadarrV3Proxy|No
  Results in configured categories` per 6 h cycle, unchanged from before. It
  still holds tag 3 and still asks for cat 2000 only. Closing this needs the
  §2b tag surgery on the **Sonarr application**, which risks the working Sonarr
  entry for 8 log lines a day — the runbook already rates that trade as not
  worth it, and it was not taken.
- **The canary's `orphan` count moves 8 → 15.** Application 6 intends exactly
  one indexer, so Radarr's other seven Prowlarr entries read as orphans *for
  that application*. Orphans are counted and reported but never fail, by design.

Verify (read-only):

```
GET /api/v1/search?query=&indexerIds=2&categories=2000&type=search   → still 0; expected
GET Radarr /api/v3/indexer   → must contain "LimeTorrents (Prowlarr)"
bash scripts/canaries/prowlarr-app-sync.sh --json                    → exit 0
```

### 2b — CORRECTION to the "just untag it" idea

The obvious fallback — *untag LimeTorrents from `general` so Prowlarr stops
retrying* — **is not safe as stated, and would break the working half.**

`general` is tag id **3**, and **both** the Radarr app *and* the Sonarr app
carry tag 3. LimeTorrents' only tag is 3. Both applications are
`syncLevel: fullSync`, which means Prowlarr manages **removals** as well as
adds. Dropping tag 3 from LimeTorrents therefore stops the Radarr attempt *and*
puts the working Sonarr entry at risk of being reaped on the next sync — trading
a Debug-level log line for a real loss of TV indexer coverage.

If the 6-hourly warning must be silenced without waiting on 2a, the safe lever
is a **dedicated tag**: create e.g. `tv-only`, tag LimeTorrents with it instead
of `general`, and add that tag to the **Sonarr** application only. Then verify
LimeTorrents is still present in Sonarr *after* the next sync before walking
away.

Doing nothing is also a legitimate choice: the cost today is 8 Debug/Warn lines
a day and no movie-side coverage from one public indexer.

### Do NOT

- **Do not widen Radarr's `syncCategories` to include 8000 (Other).** It would
  clear the log line by importing the 116 mis-mapped rows plus genuine junk —
  the `/latest100` sample includes XXX content — straight into the movie
  pipeline.
- **Do not delete LimeTorrents.** Its keyword search returns 33/40 results in
  cat 2000 and it is working in Sonarr today.

---

## Step 3 — housekeeping (zero risk, entirely optional)

**MagnetDownload** and **TorrentsCSV** are both tagged `general` but publish
only cat 8000 (Other). Both *arrs correctly and silently skip them
(`Skipping add ... due to no app Sync Categories supported by the indexer`,
Debug level) and they contribute nothing to either. Untag or disable them if a
clean sync log is worth the click.

The app-sync canary counts them as `skip_nocat` rather than ignoring them, so
if one of them ever *starts* publishing a wanted category the number moves and
the change is visible.

---

## Rollback — exact prior values

Every mutation of 2026-08-03, with the value to restore. Full pre-change JSON
bodies are on the box at `~/.opt/maint/prowlarr-remediation-2026-08-03/`
(`indexer-list.before.json`, `indexer-21.before.json`, `indexer-38.before.json`,
`applications.before.json`, `radarr-indexers.before.json`).

| # | Where | Object | Prior value | Restore |
|---|-------|--------|-------------|---------|
| 1 | Prowlarr | indexer **Knaben** (id 21) `torrentBaseSettings.preferMagnetUrl` | `false` | PUT the field back to `false` |
| 2 | Prowlarr | indexer **Tokyo Toshokan** (id 38) `torrentBaseSettings.preferMagnetUrl` | `false` | PUT the field back to `false` |
| 3 | Prowlarr | tag list | `[anime:1, cloudflare:2, general:3]` — **no tag 4** | `DELETE /api/v1/tag/4` |
| 4 | Prowlarr | application list | four apps: `1 Sonarr`, `2 Radarr`, `3 Sonarr2 (Anime)`, `4 Radarr2 (Anime)` — **no app 6** | `DELETE /api/v1/applications/6` |
| 5 | Prowlarr | indexer **LimeTorrents** (id 2) `tags` | `[3]` | PUT `tags: [3]` |
| 6 | Radarr | indexer list | 8 entries, ids `23,24,27,28,29,30,32,33` — **no id 34** | `DELETE /api/v3/indexer/34` |
| 7 | Radarr | indexer id 34 `enableRss` | `true` (as Prowlarr created it) | PUT `enableRss: true` |

Order to unwind cleanly: 7 → 6 → 5 → 4 → 3 → 2 → 1. Application 2 was **not**
modified — its `syncCategories` is still the original eleven movie categories
with no 8000, and its `syncLevel` is still `fullSync`.

> Undoing 4/5/6 restores the exact fault this runbook documents: LimeTorrents
> absent from Radarr and the canary back to `prowlarr-appsync-missing`.

---

## What the detector does and does not claim

`scripts/canaries/prowlarr-app-sync.sh` (hourly, Kuma *Canary Prowlarr App
Sync*) reads Prowlarr's `/api/v1/indexer` and `/api/v1/applications`, computes
the set of indexers Prowlarr **intends** to sync to each application
(enabled ∧ tag-intersect ∧ category-intersect, categories flattened through
`subCategories`), and diffs it against each *arr's own `/api/v{3,1}/indexer`.

- Every intentional exclusion is **counted and named** — `skip_disabled`,
  `skip_notag`, `skip_nocat` — so a regression shows up as a *move* between
  buckets rather than as an absence.
- Indexers present in an *arr but no longer intended (today: 8 disabled-legacy
  Torznab leftovers) are counted as `orphan` and reported, but do **not** fail.
  A stale entry is untidy, not broken, and paging on it would park a red.
- Exit **0** = queried everything, nothing missing. Exit **1** = a real finding.
  Exit **2** = could not establish anything (Prowlarr down, zero applications,
  zero enabled indexers, an *arr unreachable, an app implementation with no
  known API version). *Empty because clean* and *empty because broken* are
  different exit codes on purpose.

It does **not** cover the 429 burst itself — that is
`scripts/canaries/prowlarr-indexer-health.sh` Probe 1, which was retuned in the
same commit (window 10m → 20m so it no longer has a ≥5-minute blind gap against
its own 15-minute timer, threshold reconciled to the documented 25, and a note
that it counts log **lines**, roughly ten per grab failure, not events).
