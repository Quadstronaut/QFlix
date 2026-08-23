# Incidents

Operator-facing incident log for the QFlix stack. Newest first.

User-facing summaries are posted as **Uptime Kuma status-page incidents**
(status page slug `public`, "QFlix Status Page") so subscribers see plain-language
updates; this file keeps the full technical record. Keep the two in sync: when an
incident opens or resolves here, post/update the matching Kuma incident.

Severity scale: **P1** = user-visible outage or data-loss risk · **P2** = degraded
/ single non-critical service · **P3** = cosmetic or internal-only.

---

## 2026-08-20 — Two Radarr import races, one SQLite lock, and an 11-day-old cron failure the mail spool re-reports forever

- **Severity:** P3 (all three conditions self-cleared; no user impact, no operator action outstanding on the box)
- **Status:** Investigated — the Radarr and Radarr2 conditions are **closed**; the cron failure has been **closed since 2026-08-09**; the *reporting* defect that keeps resurfacing it is **open**, and it lives in REA, not on the box.
- **Components:** Radarr · Radarr2 · `/var/spool/mail/quadstronaut` · `scripts/local-llm/qflix-rea.ps1:516` (the `cron_mail` collector) · `scripts/ops/heartbeat-*.sh`
- **User impact:** None. Every affected movie holds its file; every affected cron job is running.
- **Kuma status-page incident:** none — nothing user-visible.

All three arrived in one REA alert alongside the BR-DISK ISO. Only the ISO is an open
defect. These three are, in order, a race that resolved itself twice, a lock that lasted
seconds, and a failure that ended eleven days ago.

### 1. `(removed)` is Radarr's own log scrubber, and both imports succeeded 67 seconds later

The logged path — `/home/(removed)/downloads/qbittorrent/radarr/...` — is neither a
mangled line nor a real directory. Radarr's `CleanseLogMessage` redacts the username
segment of `/home/<user>/` before the line is written. Both halves check out on the box:

```
$ ls -la "/home/(removed)"
ls: cannot access '/home/(removed)': No such file or directory

$ ls -la $R/downloads/qbittorrent/radarr/
-rw-rw-r-- 2 ... 11326905709 Aug 20 07:11  1080p.Mortal.Kombat.1995.DKom.[BDRip...].mkv
-rw-rw-r-- 1 ... 15089819922 Aug 20 07:24  The.Unbearable.Weight.Of.Massive.Talent.2022.BDRip.1080p.pk.mkv
```

The path was fine. The **timing** was not. Radarr's entire retained log set holds exactly
two of these errors, and each is followed by a successful import of the same movie:

| Movie | `Import failed` (log, CEST) | = UTC | `downloadFolderImported` (history, UTC) | Delta |
|---|---|---|---|---|
| Mortal Kombat (1995) | 07:12:14 | 05:12:14Z | **05:13:21Z** | **67 s** |
| The Unbearable Weight of Massive Talent (2022) | 07:25:10 | 05:25:10Z | **05:26:17Z** | **67 s** |

**The identical 67-second delta is the diagnosis.** Radarr's completed-download handler
polled qBittorrent, saw the torrent at 100%, and reached for the final path while qBit
still held the payload as `<name>.mkv.!qB`. One poll later the rename had landed and the
import succeeded. Two `.!qB` files were still on disk mid-audit
(`Joker...mkv.!qB`, `The.Kid.Who.Would.Be.King...mkv.!qB`) — the same state caught in the act.

Both movies hold a file right now:

| Radarr id | Title | `hasFile` | Quality | Size |
|---|---|---|---|---|
| 416 | Mortal Kombat (1995) | **true** | Bluray-1080p | 11,326,905,709 |
| 430 | The Unbearable Weight of Massive Talent (2022) | **true** | Bluray-1080p | 15,200,758,254 |

**Nothing is wedged.** The queue holds 5 records, every one `trackedDownloadStatus=ok`,
`trackedDownloadState=downloading`, with zero `statusMessages`. The blocklist's three
newest rows are all `Joker.Folie.a.Deux...REPACK` rejections — unrelated, and the
blocklist doing its job.

**A timezone trap sits on this finding.** Radarr writes its **logs in box-local CEST**
and serves its **history API in UTC**. Compared raw, a 67-second race reads as a
two-hour gap, and the successful import looks like it belongs to some other event.

Both movies are collateral from yesterday's remux cap: `movieFileDeleted` (Remux-1080p)
at 04:48Z, `grabbed` at 04:50Z, imported at 05:13Z / 05:26Z. Grabbing 23 movies at once
widens the window in which a poll can beat a rename. That is load, not a defect.

### 2. Radarr2's two error strings are one exception, and it lasted seconds

Both carry the same payload:

```
[v6.3.0.10514] code = Busy (5), message = System.Data.SQLite.SQLiteException (0x87AF00AA): database is locked
```

Every `Error` line in Radarr2's entire retained log set — all from today, none older —
reduces to **two moments**:

| When (CEST) | = UTC | Logger | Message |
|---|---|---|---|
| 07:19:20 | 05:19:20Z | `DownloadDecisionMaker` | `Couldn't process release.` |
| 07:51:58 | 05:51:58Z | `DownloadDecisionMaker` **and** `RadarrErrorPipeline` | `Couldn't process release.` / `[GET /api/v3/system/status]` |

`[GET /api/v3/system/status]` is **not a second fault.** It is the same lock surfacing
through the API pipeline — the stack bottoms out at
`NzbDrone.Core/Datastore/Database.cs:line 52` in `get_Version()`, meaning a health probe
hit Radarr2 at 05:51:58Z and took a 500 out of `SQLiteConnection.Open()`. Radarr2 answers
`200` on `/system/status` now, and its only standing health warning is the long-known
`RemotePathMappingCheck` docker-path advisory, which is pre-existing and unrelated.

Both moments fall inside the regrab cascade's disk-saturation window. `Busy (5)` is
SQLite contention, not corruption.

**Suppress this one carefully, not bluntly.** A noise class keyed on
`Couldn't process release.` or on `[GET /api/v3/system/status]` would blind REA to the
one shape that matters — a *sustained* `database is locked` at the API layer is exactly
how a probe-visible Radarr2 outage begins. The shape to key on is the
`code = Busy (5)` + `database is locked` pair; the part that separates a two-second blip
from a lock storm is a **rate floor**, and `manifest/rea-noise-classes.yaml` **cannot
express that** — it is a flat per-line regex table, one `rx` per class, with no notion of
how many times a line fired. Its own `plex-client-profile-extra` entry says so
(`manifest/rea-noise-classes.yaml`, `why:` block, ~line 210): *"this table has no rate
dimension, so the rate signal is carried OUTSIDE this rule - by the collector's
`# collector-suppressed:` census line and by follow-up FU-1 (an on-box class-rate
canary)"* — and `prowlarr-indexer-retry-transient` (~line 620) records the same accepted
limit. So split it the way that precedent already splits it: the **regex class in the
yaml** carries the shape, and the **rate condition lives on the box** as a class-rate
canary. Do not attempt a yaml-only fix; there is no field for it.

### 3. The cron `Permission denied` ended on 2026-08-09 — and the crontab line does exist

Two premises carried into this audit were wrong, and each one inverts the conclusion.

**First: `heartbeat-maint-webhook.sh` *is* invoked.** It is the last line of `crontab -l`,
sitting without the comment header that would make it easy to spot — the
"restart manitoba-maint-webhook" comment several lines above it has drifted onto the
`arr-housekeeping.py --unstick` entry, which is why the line reads as absent:

```
*/5 * * * * /home/quadstronaut/scripts/ops/heartbeat-maint-webhook.sh
```

`manitoba-maint-webhook` is `active`. The job runs.

**Second: it was never a single-file permission slip.** All four `*/5` heartbeat scripts
failed together, in the same minute, and stopped together:

| Script | Mails | Dates seen |
|---|---|---|
| `heartbeat-maint-webhook.sh` | 107 | Aug 08 (9) + Aug 09 (98) |
| `heartbeat-listmonk.sh` | 107 | Aug 08 (9) + Aug 09 (98) |
| `heartbeat-tdarr-server.sh` | 107 | Aug 08 (9) + Aug 09 (98) |
| `heartbeat-tdarr-node.sh` | 107 | Aug 08 (9) + Aug 09 (98) |

**Zero occurrences after Aug 09.** The repair is dateable straight off the inode:

```
mtime=2026-08-09 08:38:36 +0200   ctime=2026-08-09 11:03:24 +0200   mode=-rwxr-xr-x
```

A `ctime` bump 2h25m *after* the last `mtime` bump, with the mode now carrying `+x`, is
the `chmod`: content rewritten at 08:38, exec bit restored at 11:03. Outage window
**2026-08-08 23:15 → 2026-08-09 11:03 CEST, ~11h45m**, ~107 missed firings per job.

Those four scripts are installed by **three different installers**
(`43-listmonk-install.sh`, `50-tdarr-install.sh`, `240-maintenance-install.sh`), each
doing its own `chmod +x`. Four files across three installers losing `+x` in one minute
points at the Aug-08 deploy transport rather than any single installer. It is already
repaired, so this is recorded for the pattern, not for action.

### The real defect: the spool is append-only, so any failure in it pages forever

`/var/spool/mail/quadstronaut` has never been rotated.

| Property | Value |
|---|---|
| Size | 439,713 bytes |
| Lines | 9,944 |
| Messages | 432 |
| Oldest entry | **2026-08-08 23:15:01 CEST** |
| Newest entry | **2026-08-18 09:25:01 CEST** |
| `Permission denied` bodies | **428** (4 scripts x 107, all Aug 08–09) |
| Everything else | **4** — `setlocale: LC_ALL ... cannot change locale`, Aug 18, harmless |

REA reads it at `scripts/local-llm/qflix-rea.ps1:516`:

```bash
collect cron_mail bash -c 'tailfresh 500 /var/spool/mail/quadstronaut'
```

**Two staleness filters miss this file, for different reasons; a third gate — the byte
cap — is what actually bounds the damage:**

1. `tailfresh` gates on **file mtime**, never on line date —
   `find "$f" -mtime -"$FRESH_DAYS"`, with `$Script:FreshDays = 3`. The spool's mtime is
   **Aug 18** and today is Aug 20, so it passes as "fresh" while its newest *relevant*
   content is eleven days old.
2. The `FRESH_CUTOFF` **line** filter is not applied to `cron_mail` at all — and could
   not work here if it were. It reads a date out of `substr($0,1,10)`, and the
   `Permission denied` body line carries no date; the `Date:` header sits roughly ten
   lines above it, in a different part of the message.
3. `collect()` then truncates every section with `head -c "$SECTION_CAP"`
   (`scripts/local-llm/qflix-rea.ps1:277-280`; `$Script:SectionByteCap = 3000` at line 43).
   `cron_mail` uses plain `collect`, so it gets the global 3000 — it declares no
   `collect_cap` override. **`head -c` keeps the OLDEST bytes**, i.e. the *front* of what
   `tail -500` emitted, not the newest lines.

The stale lines are squarely inside the read window, but the byte cap decides how many
survive. `tail -500` covers lines **9445–9944** and contains **18** `Permission denied`
lines; the last one is line **9843**. The 3000-byte cap then delivers only the first
**~67** lines of that tail, which today carry **3** `Permission denied` lines. Measured
read-only on the box 2026-08-20: `PD_total=428`, `PD_in_tail500=18`,
`head3000_lines=67`, `PD_in_head3000=3`.

That is the trap, and it renews itself: the moment the spool ages past `FRESH_DAYS`,
`tailfresh` would skip it — but **any** new cron mail, however harmless (the Aug 18
locale warnings did exactly this), resets mtime and re-arms whatever still falls inside
`tail -500`'s first 3000 bytes — **currently 3 of 428** — for another three days. Note
the asymmetry: this byte-cap exposure **decays** on its own as the spool grows, because
every appended message pushes older lines out of the tail window. The mtime gate does
not decay — it re-arms in full on every new mail, indefinitely. An append-only spool
behind an mtime-based freshness gate means a long-resolved incident can keep being
re-read for as long as the file keeps receiving unrelated mail.

### Verdict on the 11-issue alert

REA's 11 issues are **five distinct conditions across seven reported rows**, inflated by
two models reporting the
same lines. The raw alert text was not available to this investigation, so the *pairing*
below is inferred from the summary; the per-condition verdicts are not.

| # | Condition | Real? | Verdict |
|---|---|---|---|
| 1 | BR-DISK ISO imported at 47.6 GB | **Yes — open** | Genuine defect. The grab passed on the release *name* (`Bluray-1080p`); Radarr re-graded the payload to `BR-DISK` at import and imported it anyway. No profile allows BR-DISK, so **a profile cannot stop this** — the import step does not re-check the quality profile. The `.iso` is gone; a 42.3 GB BR-DISK **`.mkv`** stands in its place as of 11:03 CEST. Full chain, root cause and fix in the appendix below. |
| 2 | Radarr `Import failed, path does not exist` | Real event, **closed** | Two 67-second qBittorrent rename races; both movies imported. Not stuck. Suppressible **only** when a later `downloadFolderImported` exists for the same download — which, like the rate floor in #3, is a **state** condition the flat regex table cannot express. Same split: regex class in `rea-noise-classes.yaml` for the line shape, the "was it imported afterwards?" check on the box. |
| 3 | Radarr2 `Couldn't process release.` | Real event, **closed** | Two moments, SQLite `Busy (5)`. |
| 4 | Radarr2 `[GET /api/v3/system/status]` | **Duplicate of #3** | Same exception, API surface. Not an independent fault. |
| 5 | bazarr2 `RuntimeError: can't start new thread` | **Yes — real** | The `ulimit -u 2000` process ceiling. Owned by the thread-ceiling canary; out of scope here. |
| 6 | bazarr2 signalr max-retry | Likely **downstream of #5** | Same 8.4 MB `bazarr2.err`, same window. |
| 7 | cron `Permission denied` | **Stale-log artifact** | Fixed 2026-08-09 11:03 CEST. Re-read out of a never-rotated spool on every run since. |

That is **one open defect** (the ISO), **one real but owned elsewhere** (the thread
ceiling), **two closed and self-resolved** (the import races, the SQLite lock), **two
duplicates** (#4 of #3, #6 of #5), and **one pure reporting artifact** (#7).

The one that should worry an operator is **#7**, because its *gate* is the only one here
that does not decay. #2 and #3 stop being reported once the logs rotate; #7 sits in an
append-only file whose mtime is re-armed in full every time an unrelated mail lands. What
limits it today is not the freshness logic but the 3000-byte section cap, which does
decay — 3 lines of 428 currently reach a model, and that number shrinks as the spool
grows. The cap is a budget knob, not a correctness one: raise it for any reason and #7
gets louder again.

### Appendix — #1 in full: how a remux cap ended in a 42 GB BR-DISK

The chain, each link verified against Radarr's history API and its own logs (history is
**UTC**, logs are **box-local CEST**; both are given where it matters):

1. **Cap Remux.** `scripts/configure/58-remux-cap-enforce.py` landed on the box
   `Aug 20 06:22 CEST` and removed Remux-1080p from the Radarr main profiles.
2. **Re-search 23.** Every movie holding a now-disallowed remux was re-searched. For
   this title: `movieFileDeleted` (Remux-1080p, 27,697,936,671 bytes) at **04:49:00Z**,
   `Searching indexers ... 5 active indexers` at 06:51:26 CEST.
3. **Grab on the name.** At **04:52:08Z** Radarr grabbed
   `In.the.Mouth.of.Madness.1994.1080p.Blu-ray.CE.4K.REMASTERED.DTS-HD.MA.5.1-NOGRP-Obfuscated`
   from NZBgeek, **parsed `Bluray-1080p`**, reported size **51,311,448,000 bytes**
   (48,935 MiB). The title carries no disc indicator — nothing in the name says
   "full disc".
4. **The payload was a full Blu-ray disc.**
5. **Radarr re-graded it at import and imported it anyway.** At **07:14:03Z**:
   `downloadFolderImported`, quality **BR-DISK**, 47,649,253,376 bytes
   (`Assigning file [In the Mouth of Madness (1995) BR-DISK.iso]`, 09:14:03 CEST).
   No profile on this box allows BR-DISK. **The import step does not re-check the
   quality profile** — re-grading is a labelling act, not a gate.
6. **After cleanup, a 42.3 GB BR-DISK `.mkv`.** As of 12:0x CEST the directory holds
   `In the Mouth of Madness (1995) BR-DISK.mkv`, **42,341,133,540 bytes** (39.4 GiB),
   mtime **11:03 CEST**, and the `.iso` is gone. Radarr did not do this: its `movieFile`
   row still names the **`.iso`** at 47,649,253,376, its history holds no second
   `downloadFolderImported`, and its 11:09:55 CEST re-search of this title reported
   `0 reports downloaded`. Disk and Radarr's DB disagree, and the surviving file is still
   graded BR-DISK at ~40 Mbps — precisely the bitrate the low-bandwidth client that
   started this whole effort cannot play. **A `.mkv` also defeats an extension check**;
   only the *arr-quality leg catches it.

**Root cause: there was no grab-time size signal at all.** `/api/v3/config/indexer` had
`maximumSize = 0` (unlimited) on **both** Radarr instances, which is why the debug log
carried, for every release, for years:

```
2026-08-20 09:32:11.5|Debug|MaximumSizeSpecification|Maximum size is not set.
```

`MaximumSizeSpecification` runs on every release *before* the download starts, against
the size the indexer reports — the only signal at grab time that separated a mislabelled
disc from a real 1080p rip. Set to 0, it abstained.

**Was the grab before the ceiling, or is the ceiling not applied? Before.** The debug log
pins the transition: the **last** `Maximum size is not set.` is **09:32:11 CEST**, and
the **first** `Checking if release meets maximum size requirements.` is **11:10:21 CEST**
(69 enforced checks since). The grab was **06:52 CEST**, ~2h20m ahead of the ceiling
landing. The ceiling **is** live and **is** applied — confirmed twice:
`config/indexer.maximumSize` now reads **25000** on radarr and **42000** on radarr2, and
the 11:09:55 CEST re-search of this exact title, run with the ceiling active, grabbed
nothing.

**Fix:** `scripts/configure/59-brdisk-block.py` — two levers, both re-read after writing.
Lever 1 sets `config/indexer.maximumSize` (25000 MiB radarr / 42000 MiB radarr2, sized
off the largest *still-policy-legal* grab per instance, not off raw grab history — the
raw history contains 7 larger releases, every one a Remux-1080p that 58 had just banned).
Lever 2 scores the existing `BR-DISK` custom format at **-10000** on the profiles that
lacked it (radarr p6, radarr2 p1-6, sonarr p6); with `minFormatScore 0` that is a hard
grab rejection, and -10000 is the value recyclarr itself writes, so it is safe on
recyclarr-managed profiles.

**Residual risk, stated plainly: neither lever is an import gate.** Import still does not
re-check the quality profile, so the only things between a mislabelled disc and the
library are a **size ceiling** and a **custom-format score** — both acting at **grab**
time, against **indexer-reported** metadata. A disc that under-reports its size, or one
arriving by a path that skips the grab decision (manual import, an already-downloaded
payload), still reaches the library with nothing left to stop it. The
library-container-sanity canary is the detection half; there is no prevention half at
import.

## 2026-08-19 → ongoing — REA audits complete, fail to deliver, and the canary stays green

- **Severity:** P2 (observability loss; no user impact)
- **Status:** Open — the *defect* is unfixed; the *condition* cleared on its own. Two runs failed to deliver (2026-08-19T02:09:55Z, 2026-08-20T01:06:39Z); the next run, 2026-08-20T02:09:39Z, posted normally (`findings=16 models=2/3 outcome=error_post`) and the box heartbeat now carries that line. Re-verified 2026-08-20T03:30Z.
- **Components:** `scripts/local-llm/qflix-rea.ps1` (workstation, gitignored) · `scripts/canaries/rea-liveness.sh` · Kuma "REA Liveness"
- **User impact:** None. Operator impact: 42 audit findings produced and thrown away with a green light.
- **Kuma status-page incident:** none — no user-visible effect, and the `incident` table in `kuma.db` is empty as of 2026-08-20.

### The audit's premise was inverted

The audit reported *"15 findings, outcome=error_post, failed to post."* `error_post` is
REA's **success** token. `scripts/local-llm/qflix-rea.ps1:1660` (the findings-post
path; line **1680** is the identical construction on the empty-findings heartbeat
path, where the success token is `heartbeat`):

```powershell
$outcome = if ($ok) { 'error_post' } else { 'discord_post_failed' }
```

`error_post` = "there were errors, and I posted them." The run the audit cited —
`2026-08-18T20:09:03-07:00` = **2026-08-19T03:09:03Z**, `findings=15
outcome=error_post` — was **delivered**. It is also the single most common outcome in
REA's history, which is why a misreading here mislabels most of the log as failure.
Counted over the whole workstation audit log:

| Outcome | Runs |
|---|---|
| `error_post` | **73** |
| `silent` | 40 |
| `heartbeat` | 26 |
| `deadman_post` | 10 |
| `dryrun_heartbeat` | 9 |
| `discord_post_failed` | **2** |

**RE-COUNTED 2026-08-23** (same log, `%APPDATA%\qflix-rea\audit.log`). The table
above is the count as of 2026-08-19 and is left as written, because the *point*
it makes — `error_post` is the success token and dominates the log — held then
and holds now. The `discord_post_failed` row did not hold:

| Outcome | 2026-08-19 | 2026-08-23 |
|---|---|---|
| `error_post` | 73 | **96** |
| `silent` | 40 | 40 |
| `heartbeat` | 26 | 26 |
| `deadman_post` | 10 | 10 |
| `dryrun_heartbeat` | 9 | 9 |
| `discord_post_failed` | 2 | **7** |

Five more egress failures landed after this entry was written, all inside a
57-hour band, four of them on one day:

| Audit-log stamp (workstation, -07:00) | Findings |
|---|---|
| 2026-08-20T05:05:13 | 20 |
| 2026-08-20T11:08:22 | 21 |
| 2026-08-20T12:07:23 | 18 |
| 2026-08-20T18:07:31 | 22 |
| 2026-08-20T20:07:55 | 21 |

So the "transient and self-clearing" reading below was right about the mechanism
and wrong about the scale: 7 runs and **145 findings** never reached Discord, not
2 runs and 42. Nothing has failed since 2026-08-20T20:07:55-07:00, which is what
"self-clearing" is doing all the work of describing — a fault that clears between
runs and returns is not the same as a fault that is over. Do not re-quote either
table as an all-time count without re-running the count.

### What actually failed

Two runs, both real, both undelivered (`%APPDATA%\qflix-rea\audit.log`):

| Audit-log stamp (workstation, -07:00) | UTC | Findings | Outcome |
|---|---|---|---|
| 2026-08-18T19:09:55 | **2026-08-19T02:09:55Z** | 22 | `discord_post_failed` |
| 2026-08-19T18:06:39 | **2026-08-20T01:06:39Z** | 20 | `discord_post_failed` |

Both sit **outside** the box's own Discord brownout (see the 2026-08-18 entry below,
which ended 07:20:29Z). REA posts from the **workstation**, not the box, so this is a
second, independent egress failure on a second rail. 42 findings lost.

They were the only two as of this entry — see the RE-COUNT above, which found five
more the day after — and they did not
persist: the very next run (`2026-08-19T19:09:39-07:00` = **2026-08-20T02:09:39Z**,
`findings=16 models=2/3 duration=797s outcome=error_post`) posted normally. So the
delivery fault is **transient and self-clearing**, which makes the canary defect below
worse, not better: an intermittent loss of a run's findings is exactly the shape that
never accumulates into anything an operator notices.

### Verdict on the canary: it is not a liar — it is mute

`scripts/canaries/rea-liveness.sh` **can** distinguish the two states, and does. The
`ok findings=` branch classifies `discord_post_failed` explicitly:

```bash
discord_post_failed|deadman_post_failed)
  skip "notify-failed:$O"
  vac_clear
  WARN="-WARN" ;;
```

It emits `PASS-WARN: rea-liveness — ... skips=1(notify-failed:discord_post_failed)`.
The information is on the wire, in the Kuma message. But the exit code is **0**, so
the monitor is **UP**, so **nothing pages**. The heartbeat writer is honest, the
canary's parser is honest, and the alert never fires.

Proved live, not inferred. Two consecutive rows of `kuma.db`, monitor
`Canary REA Liveness`, read with `sqlite3 -readonly` on 2026-08-20:

```
status=1 | 2026-08-20 02:03:21.160 | PASS-WARN: rea-liveness — reached=0h-ago verdict=0h-old cap=336h [ok findings=20 models=3/3 duration=739s outcome=discord_post_failed] skips=1(notify-…
status=1 | 2026-08-20 03:02:40.479 | PASS:      rea-liveness — reached=0h-ago verdict=0h-old cap=336h [ok findings=16 models=2/3 duration=797s outcome=error_post] skips=0
```

`status=1` is **UP** on both. The string `discord_post_failed` is *in the alert text
of a green monitor* — the failure is published and unactionable in the same breath —
and one hour later the evidence has scrolled off the only surface carrying it. Nothing
persists across those two rows: whatever an operator did not read at 02:03 is gone.

Corroborating defect 1 below: `~/.opt/maint/rea/` contains the `heartbeat` file and
**nothing else** (verified 2026-08-20). No `vacuity` file survives an undelivered run,
because the `discord_post_failed` arm calls `vac_clear` just like the clean arm does —
the two are indistinguishable to the vacuity clock by construction, which is the whole
of defect 1 and does not depend on which run happened to be last.

Two compounding defects, in order of severity:

1. **`vac_clear` on an undelivered audit.** The P5 vacuity clock exists to catch REA
   running without producing verdicts. An undelivered audit *has* a verdict, so the
   clock is reset — meaning an unbroken run of `discord_post_failed` keeps P1, P2, P3
   **and** P5 green indefinitely. No predicate degrades over time.
2. **No consecutive-failure clock for delivery.** `-WARN` is not a severity in this
   system; Kuma sees UP or DOWN. A state that loses ~20 findings per occurrence has no
   escalation path at all.

This is the same class the canary's own header warns about for rejected option (c): a
watchdog that is green through the dominant failure mode. It was designed against
`all_models_noop` and is blind to `discord_post_failed`.

### Fix

- **`scripts/canaries/rea-liveness.sh`** — treat notify failure as its own clocked
  predicate (P6), not as a clean verdict. Stop calling `vac_clear` in the
  `discord_post_failed|deadman_post_failed` arm; instead persist an `undelivered`
  streak file beside the heartbeat (same shape as `vac_check`) and `die
  rea-findings-undelivered ... 1` once the streak reaches the cap. Cap should be **1**
  — a single undelivered audit is already a total loss of that run's findings — with
  `QFLIX_CANARY_REA_MAX_UNDELIVERED` as the override. Add the STAGE label to the
  header table and a unit test.
- **`scripts/local-llm/qflix-rea.ps1`** (workstation, out of repo scope) — spool the
  rendered payload on post failure and replay it on the next run, so a transient
  workstation egress fault costs a delay rather than the findings.

### Follow-ups

- Nothing on the workstation watches the workstation's own outbound reachability;
  REA's robustness matrix classifies that as "not a seedbox concern" and stays silent.
  That classification is correct for `ssh_fail` and wrong for `discord_post_failed`,
  because the latter means work was done and discarded.

---

## 2026-08-18 — Cold-start storm after host reboot, with a 56-minute notification brownout

- **Severity:** P1 (whole-stack outage; 15 apps, 48 `operator needed` events)
- **Status:** Resolved — stack recovered; alerting gaps open
- **Components:** every UCC app · `scripts/maint/lib/notify.py` (Discord pusher) · Uptime Kuma
- **User impact:** Plex and the full request chain down for roughly 75 minutes.
- **Kuma status-page incident:** **none posted** — the `incident` table in `kuma.db` is empty as of 2026-08-20, and Kuma itself was down for the first 75 minutes of the outage, so there was no surface to post to while it mattered. This is a gap against this file's own "keep the two in sync" rule: a P1 with 75 minutes of user-visible downtime got no subscriber-facing note. Recorded rather than backfilled, because a status-page incident written two days late is worse than none.

### Timeline (UTC)

| When | Event |
|---|---|
| **04:39:32** | First `⚠ Fleet storm: 12/31 monitors down at once`. **Sent to Discord successfully.** |
| **04:43 – 06:24** | Auto-heal loops through sonarr, sonarr2, radarr, radarr2, prowlarr, bazarr, bazarr2, plex, seerr, tautulli, sabnzbd, unpackerr, postgres, listmonk, flaresolverr. All `✗ could not be started after 3 attempts — operator needed` pages **delivered** (18 sent in hour 04, 50 in hour 05, 0 failures). |
| **06:21:41** | **Host reboots.** (`uptime -s` = 2026-08-18 08:21:41 CEST.) |
| **06:24:33.860777** | First Discord send failure, ~3 min after boot: `failed: connection error: HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded`. |
| **06:24 – 07:20** | Partial egress brownout. Hour 06: 24 sent / 39 failed. Hour 07: 33 sent / 32 failed. Intermittent, never total. |
| **07:20:29.506510** | Last failed send. Egress recovers. |
| **07:36:25** | **Uptime Kuma starts** (pid 373741, still the running pid at time of writing — no restart since). 75 minutes after boot. |
| **08:00 onward** | Normal cadence resumes, 1 send/hour, zero failures. |

### Root cause, and the correction to the audit's framing

The audit's claim — *"every Discord page failed at the same moment"* — is **false**, and
the truth is both better and worse than that.

**Better:** the pages during the storm proper were delivered. On 2026-08-18 `notify.log`
holds 226 rows: **155 sent, 71 failed**. All 71 failures fall inside one 56-minute
window (06:24:33Z → 07:20:29Z), and 57 messages succeeded *inside that same window*.
The whole of the 04:39–06:21 storm sent cleanly.

**Which of the three candidate causes it was:**

- *Discord unreachable?* **No.** The failure is a client-side `ConnectionError`
  (`Max retries exceeded`), not an HTTP status from Discord. For contrast, genuine
  Discord-side faults do appear in this log and look different: 3× `429`, 1× `503`,
  1× `500` across all time. And 57 messages reached Discord during the window.
- *Pusher down?* **No.** The pusher is the process that wrote both the `sent` and the
  `failed` rows. It ran throughout.
- *Kuma down?* **Yes — and this is the buried finding.** Kuma did not start until
  **07:36:25Z**, 75 minutes after the reboot. The entire brownout sat inside Kuma's
  own downtime. For that window no canary verdict could be recorded by anything. The
  only reason any page went out at all is that the auto-heal loop posts to Discord
  **directly**, bypassing Kuma. Had alerting been routed *through* Kuma, the outage
  would have been completely silent.

Compounding this, per operator memory `kuma-restart-resets-push-timers`: Kuma's
07:36 start re-armed every push timeout from boot, so monitors that went overdue
across the outage **re-greened without ever paging**.

**Why the lost pages were not noticed:** `scripts/maint/lib/notify.py:107` is a single
`requests.post(webhook_url, json=payload, timeout=5)`. There is no retry, no spool, no
dead-letter replay — `notify()` records the failure at line 227 and returns. Those 71
pages are gone. Content was recovered only by accident: the auto-heal loop re-emits the
same `could not be started` conditions every few minutes, so later attempts carried the
same information.

### The real defect: one failure domain for subject, judge, and courier

The stack being monitored, the monitor (Kuma), and the notification path (the pusher's
egress) all live on the same host. Any host-level fault removes all three at once. On
2026-08-18 that is exactly what happened, and the only thing that saved the incident
was a bypass path nobody designed as a safety feature.

### What an out-of-band rail would have to look like

Five requirements, each falsified by the current design:

1. **Not on the box.** Anything sharing the host's kernel, power, or boot sequence
   dies with the subject. Rules out every current alerting component.
2. **Not through the box's egress.** The pusher's failure mode here was the box's own
   outbound network. A second webhook from the same host is the same rail.
3. **Not dependent on Kuma.** Kuma was absent for 75 minutes. A rail that needs Kuma to
   decide something is wrong cannot report Kuma being wrong.
4. **Dead-man / pull, not push.** Push-only alerting is silent on total failure by
   construction. The external side must alert on **absence of a heartbeat**, so
   silence is the signal. Note the trap already recorded in
   `kuma-restart-resets-push-timers`: the timer must be anchored to the *last observed
   beat*, not re-armed from the observer's own boot, or a restart erases the evidence.
5. **Spooled, not fire-and-forget.** A 56-minute egress brownout must cost latency, not
   the message. The pusher needs a durable queue with replay.

Practical shape: a heartbeat the box emits to an **external** endpoint on a fixed
cadence, with a dead-man timer *there* that pages by a route the box does not touch.
Starhold (`starhold-dedi` / `starhold-vps`) already satisfies (1)–(3) and is reachable
from QFlix. Whatever is chosen must be verified by the only honest test: **cut the
box's egress and confirm a page still arrives.**

### Fix

- **`scripts/maint/lib/notify.py`** — spool on `ConnectionError`/`Timeout` to a durable
  queue under `~/.opt/maint/notify-spool/`, and drain it at the head of the next
  `notify()`. Cap the spool and count drops. Today a failed page is silently terminal.
- **New canary** — assert Kuma's own process start is not newer than the box's boot by
  more than a bounded margin; a Kuma that lags boot by 75 minutes is a blind window
  that nothing currently reports.
- **External dead-man rail** — per the five requirements above.

### Follow-ups

- Quantify the recurrence: the same `connection error` signature appears once before
  this incident, 2026-07-08T19:14:57Z, also on a fleet-storm message. 72 occurrences
  all-time, 71 of them on 2026-08-18.

---

## 2026-08-14 — Deprecated *arr endpoints: no QFlix caller, one vendored package, one ad-hoc curl

- **Severity:** P3 (latent upgrade hazard, internal only)
- **Status:** Investigated — Sonarr side actionable, Radarr side is a non-issue
- **Components:** `~/.apps/buildarr/.venv` (vendored, not in git) · Sonarr, Sonarr2, Radarr

### Verdict: the callers are not QFlix python tooling

The audit attributed ~8 deprecated `languageprofile` calls/day to QFlix tooling. The
user-agent says otherwise. From the Sonarr logs:

```
2026-08-19 04:30:24.8|Warn|LanguageProfileSchemaController|API call made to deprecated endpoint from python-requests/2.33.1
2026-08-19 04:30:24.8|Warn|LanguageProfileController|API call made to deprecated endpoint from python-requests/2.33.1
```

`python-requests/2.33.1` is **buildarr**, and the timestamp is `buildarr.timer`'s daily
04:30 slot (`systemctl --user list-timers`: next `Thu 2026-08-20 04:30:00 CEST`, last
`Wed 2026-08-19 04:30:01 CEST`). Warning counts confirm the cadence exactly, and confirm
it is **one caller hitting both instances**, not two:

| Instance | Deprecated-endpoint warnings/day | Controller |
|---|---|---|
| sonarr  | **8**/day, 2026-08-10 → 2026-08-18 (16 on 08-19, two runs) | `LanguageProfileController`, `LanguageProfileSchemaController` |
| sonarr2 | **8**/day, 2026-08-15 → 2026-08-19 | same |
| radarr  | 4 total, all-time, all 2026-08-14 | `ImportListExclusionController` |
| radarr2 | **0** all-time | — |

The audit's "~8x/day on BOTH sonarr instances" is correct as a count and wrong as an
attribution: 8+8 is **one** nightly buildarr run walking two instances.

**The user-agent is the whole proof, and the version digit carries it.** Across all four
*arr logs, all-time, three agent strings appear on deprecated endpoints — the third
being an empty one, corrected here after the first draft claimed "exactly two":

| Agent | sonarr | sonarr2 | radarr | radarr2 |
|---|---|---|---|---|
| `python-requests/2.33.1` | 744 | 736 | 0 | 0 |
| `curl/7.74.0` | 0 | 0 | 4 | 0 |
| *(empty — no `User-Agent` header)* | **5** | 0 | 0 | 0 |

The 5 blank-agent rows are all `LanguageProfileController` on sonarr inside a
3.5-minute window on **2026-08-08** (12:27:09 → 12:30:57), nothing before or since.
Same shape as the Radarr `curl` rows below — a one-off interactive session, not a
scheduled caller — and they change none of the conclusions, but "exactly two agents"
was an exhaustiveness claim and it was wrong.

`requests` version pins the caller unambiguously, because the box has two of them:

```
system python3   -> requests 2.32.3
~/.apps/buildarr/.venv/bin/python -> requests 2.33.1   <- the logged agent
```

QFlix's own python **does** use `requests` (`scripts/maint/lib/{notify,pusher,kuma,
health,listmonk,cli}.py`), so "it's requests, therefore not us" would have been an
invalid inference. It is `2.33.1`, therefore it is the **buildarr venv** — QFlix maint
running under system python3 would log `2.32.3`, and that string appears **zero** times.
Independently, every `scripts/configure/*.py` — including `30-seerr-arrs.py`, the sole
in-repo `/languageprofile` caller — uses stdlib `urllib`, and `Python-urllib` also
appears **zero** times. Two independent agent-based exclusions, same conclusion.

**Exact callers**, all in a pinned third-party package that git does not track and
`deploy-drift.sh` cannot see:

| File (under `~/.apps/buildarr/.venv/lib/python3.11/site-packages/`) | Lines |
|---|---|
| `buildarr_sonarr/config/profiles/language.py` | 275, 301, 308, 334, 339, 353, 357, 396 |
| `buildarr_sonarr/config/import_lists.py` | 1233, 1274 |

**The repo's only `languageprofile` reference is already safe.**
`scripts/configure/30-seerr-arrs.py:57` calls `get("/languageprofile")` but wraps it:

```python
try:
    langs = get("/languageprofile")
except Exception:
    langs = []
```

and consumes it at line 100 with a default (`lang_id = ... if ... else 1`). It is a
**configure-time** script on no timer, so it contributes **zero** of the 8/day, and it
degrades cleanly when the endpoint is removed.

**Modern replacement (Sonarr):** there isn't one. Sonarr v4 **removed language profiles
entirely** and merged language into quality profiles — see
`docs/buildarr-v4-patch-session-prompt.md:26`. Language now lives as a field on
`/api/v3/qualityprofile` and as `originalLanguage` on the series resource. The
migration is *deletion*, not substitution.

### The Radarr `curl` caller is not a caller

`ImportListExclusionController` fired **four times, ever**, all on one day inside a
4.5-minute window:

```
2026-08-14 09:48:19.9|Warn|ImportListExclusionController|API call made to deprecated endpoint from curl/7.74.0
2026-08-14 09:52:19.6|Warn|ImportListExclusionController|...
2026-08-14 09:52:46.6|Warn|ImportListExclusionController|...
2026-08-14 09:52:47.8|Warn|ImportListExclusionController|...
```

Radarr2: **zero**. Nothing before, nothing since. `curl/7.74.0` is the box's system
curl, and grep across the repo *and* the deployed `~/scripts` finds no script issuing
it. 2026-08-14 is the date of the reaper orphan re-match session (operator memory
`reaper-orphan-unresolved-redloop`, the Obsession wrong-Plex-match). This was an
**interactive one-off**, not a deployed job. There is nothing to fix.

**Do not confuse it with the query parameter.** `addImportListExclusion=false` on
`DELETE /api/v3/series` and `/api/v3/movie` — `scripts/maint/qflix-anime-janitor.py:764`
and `:849`, `scripts/maint/qflix-reaper.py:793` — is a **parameter on a live endpoint**,
not the deprecated `/importlistexclusion` **resource**. Those calls are safe and must
not be "fixed". (`tests/unit/test_qflix_reaper.py:186` pins the parameter.)

**Modern replacement (Radarr), for reference only:** `/api/v3/importlistexclusion` →
`/api/v3/exclusions`.

### Fix

- **`scripts/patches/` + `scripts/configure/60-buildarr-patches.sh`** — add an eighth
  patch neutering `buildarr_sonarr/config/profiles/language.py`, the same way
  `buildarr-sonarr-import_lists.patch` already guards `languageProfileId`. Cheaper
  alternative with the same effect: drop `language_profiles` from
  `~/.apps/buildarr/buildarr.yml` so the module never executes.
- **Detection** — a Sonarr/Radarr upgrade that removes these endpoints will surface as
  a buildarr run failure, which *is* already monitored (Kuma "Buildarr",
  `systemd_oneshot`). The hazard is bounded; the value of patching first is avoiding a
  broken nightly config-sync at an unchosen moment.

---

## 2026-05-25 → ongoing — Unpackerr has written no log for 87 days and nothing noticed

- **Severity:** P2 (total observability loss on one service)
- **Status:** Open — detection fix is in-repo and unblocked; logging fix needs a deploy-time experiment
- **Components:** unpackerr (container, uid 1120) · `scripts/canaries/stale-log-watchdog.sh` · `scripts/configure/31-unpackerr.sh`

### Verdict: NOT structural. The bind-mounted log path is reachable and writable.

The audit concluded the docker socket being permission-denied makes unpackerr's output
unreachable, so only bind-mounted file logs are auditable. The first half is true; the
conclusion that file logging is therefore unavailable is **wrong**. Four independent
reads disprove it.

1. **The home directory is bind-mounted into the container.**
   `/proc/373846/mountinfo`:

   ```
   8649 8641 65:161 /quadstronaut/.apps/unpackerr /config            rw,... ext4 /dev/sdaa1
   8650 8641 65:161 /quadstronaut                 /home/quadstronaut rw,... ext4 /dev/sdaa1
   ```

   The configured `log_file` path resolves **inside** the container via mount 8650, and
   the same directory is *also* mounted at `/config` via 8649.

2. **The log file is visible from inside the container's mount namespace**, along with
   the rotation set unpackerr itself created back when it was writing.
   `ls -la /proc/373846/root/home/quadstronaut/.apps/unpackerr/`:

   ```
   -rw-r----- 1 quadstronaut quadstronaut    1676 Aug 17 13:10 unpackerr.conf
   -rw------- 1 quadstronaut quadstronaut 3264974 May 25 14:15 unpackerr.log
   -rw------- 1 quadstronaut quadstronaut 1599369 May 22 02:43 unpackerr.log.1
   ```

3. **The uid matches.** `/proc/373846/status` → `Uid: 1120 1120 1120 1120`, and `id -u`
   for `quadstronaut` on the host is **1120**. The container is not uid-remapped; the
   process runs as the file's owner. `unpackerr.log` at mode `600` is therefore
   writable by it, and the `log_files = 10` / `log_file_mb = 10` rotation it is
   configured for is exactly what produced `unpackerr.log.1`. Nothing about
   permissions, ownership, or path resolution blocks this write.

4. **The config is demonstrably being read.** The container's TCP peers
   (`/proc/373846/net/tcp`) are `172.17.0.1:17003`, `172.17.0.1:17008`,
   `172.17.0.1:17026` — precisely the sonarr2 / radarr2 / sonarr URLs written into
   `~/.apps/unpackerr/unpackerr.conf`. The file is loaded; only its `log_file`
   directive is being ignored.

And unpackerr itself confirmed it honoured that directive, right up to the moment it
stopped. The final two lines of `~/.apps/unpackerr/unpackerr.log`:

```
[INFO] 2026/05/25 14:15:03  => Log File: /home/quadstronaut/.apps/unpackerr/unpackerr.log (10 @ 10Mb, mode: 600)
=====> Exiting! Caught Signal: terminated
```

Nothing after that. The current process instead has `fd 1 -> pipe:[1886592]` and
`fd 2 -> pipe:[1886593]` — stdout/stderr into the s6 logger — and **no fd on the log
file at all**.

### Root cause

The container's environment carries `LSIO_NON_ROOT_USER=1` and `HOME=/`; the cmdline is
a bare `/unpackerr` with no `-c` and no `UN_*` overrides. This is a linuxserver.io
image, and those images route logging to the console by convention so `docker logs` is
the intended interface. That interface is exactly the one the permission-denied docker
socket removes. The container was recreated at some point on or after 2026-05-25
(`Caught Signal: terminated`) and came back with console-only logging.

So the blind spot is genuine **today**, but its cause is a container-image logging
convention — not an unreachable path. `~/.apps/unpackerr/unpackerr.conf` was rewritten
2026-08-17 13:10 and the container restarted 2026-08-18 07:36; the log mtime is still
2026-05-25 14:15. That is the decisive current test: a fresh config plus a fresh start
still produces nothing.

**Honest limit:** confirming *which* mechanism suppresses the file sink requires
changing the config and restarting — a write, and therefore a deploy action, not a
read. Everything above is read-only evidence.

### The unambiguous bug is the detection gap

`scripts/canaries/stale-log-watchdog.sh` exists precisely for this failure shape. Its
`WATCHED` table carries kometa, recyclarr, buildarr, upgradinatorr, listmonk-sync — and
**not unpackerr**. 87 days of silence, zero alerts. This is the same class as the
tdarr-healthcheck incident: a dead component behind a green light.

Worse, the configure script actively manufactures false confidence.
`scripts/configure/31-unpackerr.sh` ends with:

```bash
log_info "Tail of log:"
sshm 'tail -20 ~/.apps/unpackerr/unpackerr.log 2>/dev/null'
```

Since 2026-05-25 that has printed three-month-old lines on every run. An operator
reading the configure output sees plausible unpackerr activity and concludes success.

### Fix

- **`scripts/configure/31-unpackerr.sh`** — capture the log mtime *before* the restart
  and assert it advanced *after*; fail loudly if not. Replace the unconditional `tail`
  with the assertion. This turns a silent liar into the thing that would have caught
  this on day one.
- **`scripts/canaries/stale-log-watchdog.sh`** — add
  `"unpackerr|$HOME/.apps/unpackerr/unpackerr.log|10800"` to `WATCHED` (unpackerr's
  `interval = "2m"` and `log_queues = "1m"` mean it writes continuously; 3h is
  generous). **Ordering constraint:** land this only *after* the log is confirmed
  writing, or the canary reds forever on a known-unfixed condition — the same
  "enable the timer only after the writer lands" rule `rea-liveness.sh` follows.
- **`scripts/data/unpackerr.conf.tmpl`** — if the LSIO image honours env-var config,
  the durable fix is `UN_LOG_FILE` in the unit environment rather than the TOML
  `log_file` at line 7, since env overrides config in unpackerr. Must be verified
  empirically before being committed as the answer.

### Follow-ups

- Only 3 of 4 *arr instances appear in the container's peer list at sample time
  (`172.17.0.1:17027` / radarr absent). Possibly benign poll timing, possibly a real
  connection failure — and with no log there is no way to tell. A second symptom of
  the same blindness.
- `manifest/apps.yaml:257` monitors unpackerr by **process pattern** (`/unpackerr`).
  That proves the binary is running; it proves nothing about whether it is extracting
  anything. Process-liveness is not function-liveness.

---

## 2026-05-20 → ongoing — Tautulli outage during Ultra.cc kernel maintenance

- **Severity:** P1 (single-service outage; **streaming unaffected**)
- **Status:** Mitigated, awaiting provider — Tautulli down, config fixed, auto-recovery watcher armed
- **Components:** Tautulli (down) · UCC app-lifecycle CLI (gated platform-wide) · Plex (healthy, re-IP'd)
- **User impact:** Watch-history/stats unavailable; newsletter "recently added / most watched" data stale. Plex streaming, libraries, and playback **unaffected**.
- **Kuma status-page incident:** id 1, style `warning`, posted 2026-05-24 03:45 UTC.

### Timeline (UTC)

| When | Event |
|---|---|
| **2026-05-20** | Ultra.cc begins batched Linux-kernel-upgrade maintenance (daily 09:00–21:00 UTC windows). Plex container re-IP'd from `172.17.1.250:32400` → docker bridge gateway `172.17.0.1:17025`. Tautulli, pinned to the old IP, begins storming `[Errno 111] Connection refused` against Plex. **141,688** errors this day (0 the day prior). |
| **2026-05-21** | ~**194,741** connection-refused errors. |
| **2026-05-22** | ~**367,843** connection-refused errors (peak). |
| **2026-05-23 ~15:50** | Outage discovered during environment audit. Diagnosed Plex re-IP + Tautulli stale pin. Kuma did **not** alert — the Tautulli monitor probes only its own web port, which stayed up (→ follow-up #2). |
| **2026-05-23 16:28:58** | Tautulli stopped to apply the config fix. `app-tautulli start` then refused: `{"result": false, … "no longer available due to maintenance"}` — Ultra.cc had gated app-lifecycle commands. Tautulli now fully down and unrestartable operator-side. |
| **2026-05-24 ~03:42** | Gate confirmed still up; Plex confirmed healthy at the gateway; config already re-pinned. One-shot watcher armed (`scripts/ops/tautulli-gate-watch.sh`, runs on box) to auto-start Tautulli the instant the gate lifts, verify the Plex link, and ping Discord. Support ticket filed with Ultra.cc. |

### Root cause

Two compounding provider-side changes during maintenance:

1. **Plex container re-IP** — Plex moved from `172.17.1.250:32400` to the docker
   bridge gateway `172.17.0.1:17025`. Tautulli's pinned `pms_url` broke; it could no
   longer reach Plex. (The `50-tautulli-pms-url-fix.sh` configure step re-pins
   whatever IP is already in config, so it did **not** self-correct this drift.)
2. **UCC lifecycle CLI gated** — `app-<slug> start|stop|restart` return the
   maintenance refusal while read ops (`version`) still work. With no operator-side
   way to start a stopped UCC app (docker socket permission-denied, `app-manager.py`
   sudo-only), the stopped Tautulli could not be restarted **and platform auto-heal
   became a no-op** for the duration.

Compounding operator error: Tautulli was *stopped* to apply the fix during an active
maintenance window — see operator memory `ultracc-may2026-migration` ("don't stop a
UCC app mid-maintenance; edit config in place and let it pick up on the next
sanctioned restart").

### Remediation

- Tautulli `config.ini` re-pinned to the gateway: `pms_ip=172.17.0.1`,
  `pms_port=17025`, `pms_url=http://172.17.0.1:17025`, `pms_ssl=0`,
  `pms_url_manual=1`. Backup: `~/.apps/tautulli/config.ini.bak.1779553737`.
- Watcher `scripts/ops/tautulli-gate-watch.sh` armed on box (one-shot, deletable):
  polls every 5 min, auto-starts Tautulli on gate-lift, verifies web + Plex, pings
  Discord. Stop with `pkill -f tautulli-gate-watch.sh`; log at
  `~/.opt/maint/tautulli-watch.log`.
- Support ticket open with Ultra.cc (restore lifecycle CLI; confirm Plex address
  stability).

### Follow-ups

- **#2** Tautulli monitor must probe Plex connectivity, not just its own web port
  (the reason this outage never alerted).
- **#5** After maintenance lifts: confirm Tautulli started + storm gone (watcher
  handles the start; verify the error count drops to ~0).
- Operator memory: `ultracc-may2026-migration`, `qbit-max-ratio-backlog-hazard`.

### Related (same maintenance window, separate issue)

**qBittorrent runaway upload.** Noticed 2026-05-23: 22.17 TB uploaded vs 4.20 TB
down (ratio 5.28) against a 24 TB monthly cap — pure seeding, one torrent at ratio
771 (~7.55 TB). Global share-ratio limit set to **2.0 / pause** on 2026-05-24.
Enabling the limit against the over-ratio backlog mass-removed ~25 torrents and
deleted their download copies; **library intact via hardlinks** (verified). No user
impact. See `qbit-max-ratio-backlog-hazard`.
