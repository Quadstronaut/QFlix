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

### 2a — preferred: refresh the indexer definitions

Prowlarr → **System → Tasks → Application Update** (definition refresh), or
upgrade Prowlarr past `2.5.2.5491`, then re-test. The search path already works,
which is what makes a definition bug the likely story — and a likely
already-fixed-upstream one.

Verify with the exact read-only probe used in diagnosis:

```
GET /api/v1/search?query=&indexerIds=2&categories=2000&type=search   → must be > 0
```

Then hit **Sync App** on the Radarr row and confirm LimeTorrents appears in
Radarr's indexer list.

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
