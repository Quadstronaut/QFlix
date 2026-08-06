# Changelog

## 2026-08-06 — The anime Bazarr held zero series for twelve days, green the whole way

Anime and Anime Movies got no subtitles between 2026-07-25 and 2026-08-06.
bazarr2 was up, answering its web UI with a 200, pushing a healthy heartbeat,
and storing nothing at all.

**Mechanism.** One row went missing. `table_languages_profiles` was emptied on
2026-07-25 while `config.yaml` kept naming profile 1 as both
`serie_default_profile` and `movie_default_profile`. Every series Bazarr synced
from Sonarr2 was stamped `profileId=1`, that FK pointed into an empty table, and
the insert failed:

```
ERROR (series:227) - BAZARR cannot insert series /home/.../Anime/Cowboy Bebop
  because of (sqlite3.IntegrityError) FOREIGN KEY constraint failed
```

`table_shows` therefore stayed at **0** — and because `table_episodes` carries
its own FK to `table_shows`, every episode insert then failed against the empty
parent, at which point the sync job died on `'NoneType' object has no attribute
'episode_file_id'`. Three failures deep, one missing row. It ran hourly for
twelve days and logged **8,485** `FOREIGN KEY constraint failed` errors. The
same event also cleared every enabled language, so even a surviving profile
would have subtitled nothing.

**Why nothing caught it.** bazarr2's app monitor probes the web UI. The web UI
was genuinely fine — Flask was serving, the API answered, `/system/status`
returned a version. Nothing in the monitoring set asked the only question that
mattered: *does this subtitle daemon actually hold any content?* An HTTP 200
from a service that stores nothing is indistinguishable from health at the
transport layer, which is the entire blind spot.

What eventually surfaced it was the Random Error Audit flagging one FK line at
**1 of 3** model confidence — twelve days late, and by luck rather than by
design. That is not a monitoring system; that is a lottery ticket that happened
to win.

**Fix.** Profile 1 (`English`, mirroring bazarr-1) recreated through Bazarr's
own settings API rather than a raw SQLite INSERT — Bazarr caches the profile id
list in memory and a direct write would have diverged from the running process.
`en` re-enabled in the same call. A forced `update_series` + `update_movies`
brought in 3/3 series (75 episodes) and 3/3 movies-with-files, all carrying
`profileId=1`, with zero errors logged afterward.

**Guard.** `canary-bazarr-ingest`, hourly, over **both** instances. Three
predicates: a **dangling default profile**, **zero enabled languages**, and
**ingest stalled** (the *arr holds items, Bazarr holds none). The profile
predicate is written as a dangling *reference* rather than "the profiles table
is empty", because deleting and recreating a profile in the UI assigns a fresh
id and leaves the config pointing at the old one — a state an emptiness test
reads as perfectly healthy. Mutation-proved on arrival: wiping the profiles
table fires it, renumbering 1→7 fires it (at `profiles-in-db-1`, which is what
an emptiness check would have waved through), disabling all languages fires it,
emptying `table_shows` fires it, an unreadable DB exits 2 rather than passing
clean, and an unmutated scratch copy still passes.

Bazarr's `Error trying to get releases from Github` rate-limit line, flagged in
the same alert, was classified as noise across all three REA policy surfaces.
It is the unauthenticated GitHub API's 60-requests-per-hour **per-IP** quota
being spent by other tenants of this shared seedbox, and it is cosmetic by
proof rather than assertion: every Bazarr here runs `--no-update`, so the
release list is display-only. Authentication is not available as a fix —
`check_update.py` passes no headers and has no token setting, and
`bazarr2-sync.timer` would revert a patched call site within the hour. The rule
is anchored to the Bazarr repo URL, so a rate-limit against any other GitHub
endpoint still pages.

## 2026-07-29 — The dashboard served a dead app shell for 22 hours, green the whole way

The public board at `/` looked fine. It was not fine: for roughly 22 hours it
served an HTML shell that could not hydrate. No Support modal, no live refresh,
no client-side navigation. Some people saw a working dashboard and some saw
nothing, which is the tell — browsers with a warm cache kept running off the
stale-but-*consistent* June-28 shell, and only a cold load exposed the break.

**Mechanism.** `build/` on the box was rewritten at 01:33 and again at
03:31–03:32. The node process serving it — PID 15178 — had been running since
2026-07-08 21:14 and was never restarted. SvelteKit's adapter-node hands static
assets to **sirv, which snapshots its file manifest exactly once, at process
start**. Files created after that moment do not exist as far as the running
server is concerned. So 6 of the 10 `/_app/immutable/*` modules the freshly
rendered shell referenced returned **404** while sitting on disk at mode 644:

```
app.CGGeqWLt.js   2830 bytes, present on disk   ->  HTTP 404
```

sirv also precomputes `Content-Length`, `ETag` and `Last-Modified` at boot, so
the four assets it *had* indexed — and the HTML document itself — advertised
their old byte counts while streaming the new bytes. Chrome aborted the document
with `net::ERR_CONTENT_LENGTH_MISMATCH` and navigation never reached
`domcontentloaded` (60 s timeout). Two distinct symptoms, one cause.

**Why nothing caught it** — the part worth internalizing. Three independent
guards pointed at this exact surface and all three were structurally incapable
of seeing it:

1. **The app monitor probes `/healthz`.** `qflix-dash`'s manifest health kind is
   `http_root` with `path_override: /healthz`. The stale in-memory server
   answers that forever, so the pusher's probe never failed, `recovery.trigger_async`
   never ran, and "QFlix Dashboard" stayed green.
2. **The canary and the smoke grepped a marker that outlives hydration.**
   `scripts/canaries/mobile-ux.sh:29` and `scripts/smoke-test.sh:120` counted
   occurrences of `data-qflix-dash`. That string is emitted by the *server-side*
   render, so a dashboard with zero working JavaScript scores a perfect green.
3. **The installer shipped the build and then asked the old process how it was
   doing.** `scripts/configure/90-qflix-dash-install.sh` used
   `systemctl --user enable --now qflix-dash.service`. `enable --now` **starts a
   stopped unit but does not restart a running one** — so step 2's `scp` of the
   new `build/` landed under a process that would never read it. The verify step
   then probed `/healthz` and `/api/status`, both answered happily by that same
   stale process, and the installer reported success.

Every layer was honest about what it measured. None of them measured whether the
bytes the page asks for are bytes the server will hand over.

**Fixes:**

- **The deploy path now restarts, and its verify asserts the real invariant.**
  `enable --now` became `enable` + `restart` — `restart` also starts a stopped
  unit, so it is a strict superset and just as idempotent (the same reasoning
  `lib/recovery.py` already records for using restart over start: a process that
  is alive but degraded is a no-op under `start`). The verify step now fetches
  the loopback root, extracts **every** `/_app/immutable/*` reference from the
  served HTML, requires all of them to return 200, and compares the document's
  advertised `Content-Length` against the bytes delivered. A deploy that leaves a
  dead shell now **fails the install** instead of printing `healthz=ok`.
- **New `dash-asset-integrity` canary** (every 15 min, `Canary Dash Asset
  Integrity`) — the durable half, because the next stale-manifest cause will not
  necessarily be the installer. Two predicates, deliberately independent:
  *asset resolvability* (every referenced `/_app/immutable/*` must resolve 200
  from the process that served the shell) and *Content-Length agreement*. The
  second is **not** implied by the first: sirv *did* index the files it knew
  about at boot, so a file rewritten in place at the same path still returns 200
  — just under a stale length. The 404 sweep can be entirely clean while browsers
  still refuse to parse the document.
- **A narrow self-heal, and an explicit refusal.** A 404 whose file **exists**
  under `~/.apps/qflix-dash/build/client/` is diagnostic of a stale in-process
  sirv manifest and of nothing else, so the canary fires one
  `systemctl --user restart qflix-dash` — capped at one per 24 h by a durable
  epoch latch, suppressed inside the Monday 11:00–15:00 UTC maintenance window
  (lockfile *and* wall-clock legs, as in `qflix-torrent-janitor.py`), stamping the
  latch only when a restart was actually issued, and **re-fetching afterwards** so
  it never reports a heal it did not verify. A 404 whose file is **absent** is a
  partial deploy: it alerts and does not restart, because a restart cannot fix
  it. Same refuse-on-the-wrong-signature discipline as `flaresolverr-canary.py`.
- **Wired at all five points, not four.** Manifest entry with the full rationale,
  both systemd units, installer staging + unit install + `enable --now` on the
  timer, and — the line that makes "committed but inert" impossible — the name
  appended to the installer's canary-timer smoke loop, so a missing timer fails
  the install gate. Kuma provisioning needed no code: `bootstrap-kuma-monitors.py`
  iterates `manifest.canaries()`, derives the 1500 s heartbeat from the
  `every-15min` schedule, captures the push token as `canary-dash-asset-integrity`
  (the exact key `cli.py` reads), and `_ensure_notifications_attached()` runs last
  on every invocation, so the new monitor cannot be born mute.
- **Corrected two count sites that were already stale.** The operator-visibility
  mermaid diagram still said 54 push monitors (the 2026-07-29 pass fixed the
  high-level diagram and missed this one) and labelled the remainder `+ 9 more
  canaries` against a real remainder of 14. The timer count in the at-a-glance
  table was understated by one for the same reason.

> Diagnostic note: `enable --now` is correct and idempotent on a **timer** — a
> timer has no long-running in-memory state to go stale. It is only dangerous on
> a **service** whose process caches something at start. The distinction is what
> made this bug survive dozens of clean-looking installer runs.

Doc counts were not hand-chased: `tests/unit/test_doc_counts.py` derives them
from the manifest and named all six stale sites (README table + prose + repo
layout, inventory, FAQ deck-sub + count + canary table, `canaries/README`).
Canaries **19 → 20**, manitoba Kuma monitors **61 → 62**. Suite **1113 passed,
5 skipped** with the wiring in place; `kuma audit` will declare manifest 62 =
matched 62 once `bootstrap-kuma-monitors.py` has created the new monitor.

### Adversarial review of the above, same day — seven more defects, five of them in the fix itself

The change above was put through a hostile review before it shipped. It found
seven real defects, every one reproduced by running the shipped artifacts rather
than reading them. Two reviewer findings were duplicates of others, and two
proposed mutants turned out to be behaviourally equivalent to the code they were
attacking — those are called out below rather than papered over.

**1. The installer would have aborted before installing the new timer.** This is
the one that mattered most, because it would have shipped the guard *inert* —
the precise failure the five-point wiring exists to prevent. Step 7's unit-copy
loop iterates 60 unit filenames and `cp -f`s each out of `~/scripts/maint/systemd/`
under `set -euo pipefail`. Four of those names —
`manitoba-maint-canary-tdarr-{scanner,healthcheck}.{service,timer}` — were in
the loop, in the `enable` list and in the smoke gate, but had never been added to
the **tar staging list**, so they do not exist on the box. Confirmed read-only:
`~/scripts/maint/systemd/` holds 61 files and not one of them is a tdarr,
sab-stall, ucc-gate or dash-asset unit. Replaying the heredoc verbatim under a
fake `$HOME` pre-populated with exactly what the staging list names:

```
cp: cannot stat '.../manitoba-maint-canary-tdarr-scanner.service'
--- heredoc exit code: 1 ---
units installed: 49        dash-asset-integrity.timer MISSING
```

Entry #50 of 60 kills the remote shell, so `daemon-reload`, all 20 `enable --now`
lines and the smoke gate never run. Worse than "no coverage": Step 4.5 runs
*before* Step 7 and had already created the `Canary Dash Asset Integrity` monitor
with both notification channels attached, so a deploy would have left a live
monitor with a 1500 s heartbeat and nothing pushing to it — a permanent Discord
page, plus zero coverage. After the fix the same replay installs all 62 units and
exits 0.

While in there, `sab-stall` was wired too. It had shipped on 2026-07-19 with a
manifest entry, a script and both units and **zero** installer wiring — never
staged, never installed, never enabled, and the repo read as though usenet stalls
were covered.

**2. Every counting smoke gate was dead code.** `CT=$(sshm "... | grep -c ...")`
is a bare assignment, so it inherits the command substitution's exit status — and
`grep -c` exits **1** when it counts zero. Under the installer's `set -euo
pipefail` the exact condition each gate exists to catch (a timer that is *not*
scheduled) terminated the installer **at the assignment**, so the
`gate ... fail` branch below it was unreachable. With `2>/dev/null` also
swallowing ssh's stderr, the operator got a bare `rc=1` and no clue which check
failed. Demonstrated against the live box using the genuinely-undeployed timer as
the natural zero case:

```
BEFORE:  replay exit=1        (nothing printed at all)
AFTER:   GATE canary-timer-dash-asset-integrity  fail  timer not in list-timers
         GATE canary-timer-movie                 pass  scheduled
         LOOP COMPLETED
```

Seven sites shared the pattern; all now route through a `remote_count` helper
that always yields an integer.

**3. The 24-hour breaker deleted itself whenever the state dir was unwritable.**
`stamp_latch()` swallowed its write error and returned `False`; the caller only
*logged* that. `heal_cooldown_active()` then failed **open** on the absent latch,
so a repairable fault produced an unattended `systemctl --user restart qflix-dash`
on every 15-minute tick — 96 a day — and left no record, because the narrative log
and `events/*.jsonl` die on the same `ENOSPC`. Not hypothetical on this slot:
`quota.sh` exists because the disk reaches 90 %. Two-arm proof, one variable
changed:

```
latch unwritable:  3 ticks -> 3 restarts
latch writable:    3 ticks -> 1 restart
```

The stated backstop did not exist either: `MIN_UPTIME_S` was 120 s against an
`OnCalendar=*:0/15` timer, so measured uptime at the next tick is always ~900 s
and the cold-start guard could never fire. Both fixed. The latch is now
**reserved before the mutation** — written atomically (tmp + fsync + `os.replace`)
and read back — and the restart is *refused* with `dash-heal-latch-unwritable` if
it will not persist, because a breaker that is not durable is not a breaker.
Every path that turns out not to have issued the restart **releases** the
reservation, so council defect D1 (a breaker spending its whole budget on a
no-op) stays fixed. `MIN_UPTIME_S` is now derived as two timer ticks, and a test
pins the tick literal against the unit's own `OnCalendar`.

**4. One dropped response restarted a healthy dashboard.** `record()` promoted
any single `IncompleteRead` straight to the repairable bucket with no
corroboration. A fixture that over-declared `Content-Length` on exactly the first
request produced `STAGE=dash-healed ... restarted-and-RE-VERIFIED-healthy` — it
restarted a fine dashboard, claimed to have repaired a fault that never existed,
and burned the 24 h latch, so a *real* incident in the next 24 h would have needed
a human. The 404 leg's licence for single-cycle escalation is sirv's manifest
being immutable for the process lifetime; that argument does not extend to body
delivery, which one cycling nginx worker can break transiently. A mismatch is now
**re-probed once** before it is believed. A genuine stale sirv stat tuple lies
identically forever, so the re-probe confirms it and the real fault still heals on
the first cycle; noise resolves to inconclusive and never touches the breaker.

**5. A 200 whose body never arrived was reported as `PASS`.** When the mid-body
read raised anything other than `IncompleteRead`, `received` stayed `None`, so the
probe matched neither the transport bucket (status was 200, not 0) nor the length
predicate (which needs a number) — it fell through **every** bucket and counted as
healthy. Against a fixture answering `200`, `Content-Length: 5000`, ten bytes and
then a stall, the shipped canary printed:

```
PASS: dash-asset-integrity refs=3-probes=4-enc=identity-all-200-and-length-consistent
```

for an asset no browser can execute — the exact false-green class this canary was
built to eliminate, inside the canary. It now has its own bucket and stage
(`dash-asset-unread`), is retried once because body delivery *is* transient, and
alerts without restarting: headers-then-stall is a wedged worker, not stale
in-process state.

**6. The hardened verify raced node's readiness.** `qflix-dash.service` is
`Type=exec`, so `systemctl --user restart` returns at `execve`, not at `listen()`
— confirmed on the box, where `ExecMainStartTimestamp` and `ActiveEnterTimestamp`
are the same instant while "Listening on 127.0.0.1:*" lands in `app.log` later.
The new gate curled `/healthz` immediately afterwards with no settle and no retry,
so a deploy that had in fact succeeded would `FATAL` on connection-refused. Every
sibling installer settles first; this one now polls, which costs nothing on a
healthy deploy:

```
BEFORE:  FATAL: /healthz returned HTTP 000        exit=1  elapsed=1s
AFTER:   healthz=ok ... assets=3/3 resolve 200    exit=0  elapsed=6s
```

**7. Found while testing the fix, not reported by the review.**
`CODE=$(curl ... -w '%{http_code}' || echo 000)` **concatenates** on a
mid-transfer failure: curl prints the status it did receive *and* the fallback
appends, so a truncated 200 came out as `200000`. The root gate then aborted with
`loopback root returned HTTP 200000` — the wrong diagnosis, and it aborted
*before* the Content-Length predicate that exists specifically to name that fault.
Now:

```
FATAL: document Content-Length=328 but 288 bytes delivered (curl rc=18)
       - stale sirv metadata; the running process predates this build
```

The same pattern was fixed in the smoke test's landing-page gate, where a 200
with no `Content-Length` and a broken transfer had been a free pass.

**8. Three contradictory unit counts, all stamped with the same date.** The
at-a-glance table said 45 timers while the repo-layout comment nine lines of prose
later and the public FAQ both said 43. Git history shows those figures have moved
in lockstep through every commit that touched them (36/30 → 55/43), so they are
one measurement, not three populations — and the derivation offered for the bump
does not survive checking. Measured read-only instead:
`~/.config/systemd/user/` holds **56 services + 44 timers** today (57 + 45 once
this canary deploys). All three sites now say 44, the re-count instruction names
the exact command, and a new `test_doc_counts` anchor asserts the three agree —
the mechanism RULE 4 exists for, applied to a number the manifest cannot derive.

**The durable fix for the class, not just the instance.** `tests/unit/test_canary_wiring.py`
derives the required wiring from `manifest/apps.yaml` and asserts, for **every**
canary: both units exist, the service unit's `ExecStart` names the canary by its
manifest key, the script is tar-staged, both units are tar-staged, the unit is in
the cp loop, the timer is enabled, and the name is in the smoke gate — plus the
closure property that actually caused the abort, *every unit the installer copies
or enables must be tar-staged*. Seven mutants restoring each historical omission
are all caught. That is what makes the three-time-repeated "committed but not
scheduled" failure a test failure from now on instead of a reading exercise.

**Two reviewer claims rejected, with evidence.** A proposed mutant that removed
the sweep's `status == 200 and body is None` branch survived the suite — because
it is behaviourally equivalent: `main()` checks the `unread` bucket *before*
`inconclusive`, so that branch was dead code. It was deleted rather than tested
around, and replaced with a comment pinning the ordering that makes the deletion
safe. Separately, two pairs of findings (installer abort; breaker fail-open) were
the same root cause reported twice from different angles, and are fixed once.

Suite **1180 → 1209 passed, 6 skipped**. 10/10 canary mutants and 7/7 wiring
mutants caught. Nothing was written to the box: `qflix-dash` is still
`MainPID=237679`, `NRestarts=0`.

### Completeness pass, same day — the *fix* was the unguarded half

Everything above was verified by re-deriving it from the working tree. The five
wiring points hold for all 20 canaries, the units are byte-identical to the
`ucc-gate-stuck` precedent, the self-heal genuinely gates on the window before
the breaker and reserves the latch before mutating, the live box confirms the
documented **56 services + 44 timers**, and the canary passes against the healthy
dashboard. One thing did not hold.

**The deploy-path fix — the root cause, the half that actually stops this
recurring — had zero test coverage.** Proven by mutation against the full suite:

```
revert `restart` -> `enable --now`      (the literal root cause)  1209 passed
delete the installer's asset sweep                                1209 passed
neuter the smoke landing-page predicates back to a marker count   1209 passed
```

Three separate reversions of the incident fix, and the suite never blinked. That
is the same defect the canary exists to prevent, aimed at the repair instead of
the guard: the repo *reads* as though the deploy path is fixed, and nothing would
notice if it stopped being. RULE 3's "committed but not scheduled is worse than
no guard" applies verbatim to "committed but not pinned" — the canary half was
mutation-verified to death while the half that removes the fault was held in
place by nothing but the diff.

`tests/unit/test_deploy_path.py` closes it in two tiers. **Structural** pins the
properties whose deletion was invisible: `restart` not `enable --now`, the verify
extracting *and probing* `/_app/immutable` references, the Content-Length
predicate, the exists-vs-absent discrimination, the readiness poll, and the smoke
gate actually *consuming* the predicates it computes (computing one and not
gating on it is indistinguishable from not computing it). **Behavioural**
extracts the installer's remote verify heredoc verbatim — located by content, so
restructuring the installer fails loudly rather than silently exercising the
wrong block — and runs it under a fake `$HOME` against a real loopback server
that reproduces the incident signature: shell renders, marker present,
`/healthz` 200, and a referenced module 404s while the file sits on disk. The old
verify reported success on exactly that fixture. 9/9 mutants caught.

**One live defect found by the new guard on its first run.** The readiness poll
added in finding 6 above still carried the `|| echo 000` concatenation that
finding 7 claims to have eliminated:

```
HZ=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$BASE/healthz" || echo 000)
  -> HZ=[000000] on a refused connection, verified by running it
```

The poll still behaves (neither value equals `200`), but the `FATAL` below it
then reports a nonsense status and sends the operator after the wrong fault —
the same wrong-diagnosis class, in the fix for that class. Normalised the same
way as its two siblings.

Suite **1209 → 1222 passed, 6 skipped**. The box remains untouched:
`MainPID=237679`, `NRestarts=0`, public root 200, and no
`~/.opt/maint/dash-asset-integrity/` or canary timer exists there yet — this
change is **not deployed**, and the guard is inert until it is.

## 2026-07-28 — 32 of 60 Kuma monitors could go red in total silence

Found while answering a simple question: *can I close this window?* Verifying
that the new `tdarr-healthcheck` canary could actually reach the operator —
rather than assuming it — turned up two more silent-failure layers stacked
underneath it. Neither would have announced itself.

**1. The canary pushed nowhere, and exited 0 doing it.** `manitoba-maint canary
push` reads `~/secrets/kuma-push-tokens.json`; the token had been deployed to
`~/.opt/maint/kuma-push-tokens.json`. When a token is missing the CLI returns
the canary's own exit code and simply doesn't push — no warning, no non-zero.
Monitor 108 had **zero heartbeats** across three successful-looking `exit=0`
invocations. Same shape as the 2026-07-19 reaper token gap. Token deployed to
the path the CLI actually reads; verified a real beat now lands carrying the
canary's message.

**2. `_ensure_autoheal_webhook` only ever attached itself once.** It creates the
webhook with `applyExisting=True`, but every subsequent run hits `[skip]` — so
every monitor created *after* that first run was born with **no notifications at
all**. Not just no Discord: no auto-heal webhook either, which is precisely what
that function's own docstring warns about (*"Kuma down events never reach
manitoba-maint-webhook, so lib.recovery never fires"*).

By today that was **32 of 60 active monitors** — 15 of 18 canaries, the
`QFlix Fleet` storm aggregate, the `Manitoba Pusher` self-heartbeat, all four
self-pushing janitors, plus real apps (SABnzbd, VictoriaLogs, QFlix Dashboard,
Bazarr 2, Upgradinatorr). Every one of them could have gone red without pinging
anyone or triggering recovery.

**Fix:** new `_ensure_notifications_attached()` reconciles the default channel
set onto every active monitor and runs **last on every invocation**, after all
monitors exist — so a monitor added later can never again be born mute. The
channel set is derived from Kuma's `isDefault` flags rather than hardcoded.
Applied via the socket.io API, not SQL, because Kuma 2.x caches monitors in
memory and direct DB writes diverge from that cache.

Repaired 32/32; re-run reports 0 (idempotent); DB confirms **0 of 60 active
monitors without notifications**.

> Diagnostic note: reading `kuma.db` with `mode=ro` does **not** see the
> write-ahead log, so recent heartbeats are invisible and every monitor looks
> stale. Copy `kuma.db` + `-wal` + `-shm` and read the copy.

## 2026-07-28 — Tdarr health checks: 100% dead and silent for 68 days

A full Tdarr audit. Transcoding was healthy; the **health-check pipeline had
never once succeeded** since the libraries were built on 2026-05-21 — **2,866
failures across 54 days, 0 successes, `healthCheckScore` 0.000**, 242 files
parked at `HealthCheck=Error`, and not one alert in 68 days.

**Root cause.** The libraries carried Tdarr's stock `handbrakescan=true`, so
every check spawned `HandBrakeCLI -i <file> --scan`. HandBrakeCLI does not exist
on this rootless Ultra.cc slot, and never did:

```
Subworker:a.Error executing binary: HandBrakeCLI Error: spawn HandBrakeCLI ENOENT
```

`add_library()` copies Tdarr's own `libraryDefaults` dict wholesale, so all three
libraries inherited a HandBrake health check onto a box with no HandBrake.

**Why it stayed invisible for 68 days** — the part worth internalizing. The
failure is *orthogonal* to transcoding: transcodes use the bundled
`ffmpeg-static` and were succeeding the entire time (1,168 successes over the
same window). So every surface an operator would glance at read healthy — unit
states, the dashboard, the Kuma app monitors, even the `tdarr-scanner` canary
(which watches the startup FFprobe/Exiftool probes, and those genuinely were
fine). Nothing in the fleet was watching the one number that was wrong. A
subsystem can be 100% dead while every adjacent signal is green.

**Fixes:**

- **Engine switched to ffmpeg** on all three libraries (`ffmpegscan=true`).
  Verified against Tdarr's own `worker1.js`: `handbrakescan` / `ffmpegscan` are
  the two mutually exclusive health-check engine branches. `ffmpeg-static` is
  present, already carries every transcode, and full-decodes at ~20x realtime
  here (~2.5 min for a 50-min episode), bounded by the 2 health-check workers.
  HandBrake was never an option — no root, no package, and Tdarr would wipe any
  hand-patched binary on upgrade.
- **242 stale `Error` verdicts re-queued.** A verdict written by an engine that
  could not spawn is evidence about the missing binary, not about the file, and
  Tdarr never retries on its own. All 459 files now queued; backfill clears in
  about a day of node uptime. First successes confirmed live
  (`verdict: healthcheckSuccess`) — 0% error rate.
- **Codified in `50b-tdarr-config.py`** so it cannot regress: `HEALTHCHECK_ENGINE`
  + `ensure_healthcheck_engine()` (idempotent), plus an overlay in `add_library()`
  so a freshly created library never inherits the HandBrake default again.
  Re-queue fires *only* when the engine actually changes — a genuine
  ffmpeg-found error must stick and surface as a real corrupt file.
- **New `tdarr-healthcheck` canary** (hourly, `Canary Tdarr Healthcheck`) — the
  durable half. Two predicates, because a ratio alone lags:
  **engine sanity** resolves each library's configured engine binary and FAILs on
  tick one if it's missing (the exact 2026-05-21 bug, caught before a single file
  is mis-verdicted); **error ratio** of *completed* checks at 20% WARN / 50% FAIL,
  judged only after 20 completed so a fresh library can't trip on noise. Stays UP
  when tdarr-server is down — `tdarr-scanner` owns that red, and two reds for one
  cause is the correlated noise this repo keeps removing. All six branches
  verified against fixtures, including a reproduction of the original bug.
- **CCExtractor was misdiagnosed in our own canary.** `tdarr-scanner.sh` reported
  it as `ccextractor-wasm-oom`; it is not WASM at all —
  `libtesseract.so.4: cannot open shared object file`, a dynamic-link failure
  needing root we don't have. Corrected in the message and the header. Mediainfo
  remains genuine WASM OOM and genuinely unfixable (unchanged).

Suite **1089 passed, 5 skipped** (was 1086 + 3 doc-count failures from the
17→18 canary bump; README / inventory / FAQ all updated, `kuma audit` no drift
at manifest 59 = matched 59).

## 2026-07-28 — REA noise enforcement + the Plex source that was still dead

The 2026-07-28 REA alert paged the operator with two findings. **Both were
noise**, and one of them was a class the system prompt *already* forbade —
`qwen3-coder:30b` reported it anyway at 1/3 consensus, which is enough to page.
Evaluated against live logs, then fixed at the layer that can actually enforce it
(`scripts/local-llm/qflix-rea.ps1`, gitignored — tests here carry the contract).

**The two alerted findings, adjudicated:**

- `plex:ssl-protocol-shutdown` — **benign**. `Caught exception trying to stream
  file …: write: protocol is shutdown (SSL routines)` is a viewer closing or
  seeking mid-transcode. The identical error fires on PhotoTranscoder *poster
  JPEGs*, which have no transcode pipeline at all — proof it is the client
  leaving, not a media fault.
- `tdarr:undefined-includes-error` — **benign**, and already on the prompt's
  never-report list. Unhandled express-route error from
  `Tdarr_Server/srcug/api/servers.js`; server was at `NRestarts=0` with 20 days'
  uptime while these fired. The prompt's *stated reason* was wrong, though (it
  blamed the node's quiet-hours shutdown; the node was up) — corrected.

**Fixes:**

- **Deterministic noise suppression** (`$Script:NoiseFindingRules` +
  `Test-IsNoiseFinding`). Prompt text is advisory and consensus has no floor, so
  one over-eager model could page alone. Four narrow rules now drop known-benign
  classes *before* consensus. Rules match exact phrasing, never a subsystem, and
  every suppression is written to the audit log (`suppressed n=… rules=…`) —
  never silent.
- **`plex_errors` was still a dead source.** The 2026-07-25 audit fixed its
  *path* but not its *pattern*: PMS logs `<ts> [<thread-id>] ERROR - <msg>`, so
  the brackets hold the thread id and `grep '\[ERROR\]'` matched **zero** lines
  in every Plex log (measured: 0 hits vs 2021/3351/1328/… for `" ERROR - "`).
  The finding in this very alert reached REA through VictoriaLogs, not through
  the Plex source. Pattern fixed, plus three high-volume benign classes dropped
  so real faults fit the cap — **1204 fresh ERROR lines → 22**.
- **Byte cap kept the wrong end.** `collect()` caps with `head -c`, which keeps
  the *oldest* bytes; a rotated PMS log holds a week, so the section filled with
  day-7 lines that the models' own 3-day staleness rule then discarded — a
  section that looks full but is 100% ignorable. `plex_errors` now caps from the
  tail; verified it ends on today's date instead of seven days back.
- **`tdarr` source rebuilt.** The `.err` files are ~90% express stack-trace
  continuations the prompt already ignores, so the section spent its whole budget
  on noise. Continuations stripped and deduped; the *timestamped* app logs added
  with ERROR lines date-collapsed and `uniq -c`'d, so a fault repeating hundreds
  of times costs one line instead of drowning the cap.

**What that last fix immediately exposed** — a real, ongoing fault REA had never
once surfaced: `WebAssembly.instantiate(): Out of memory: wasm memory` →
`Error running MediaInfo 3`, **858 hits across 2026-07-24…28**. This is the known
`node-ultracc-wasm-fix` class (slot `ulimit -v` 10 GB vs WASM's ~8 GB trap-guard
reservation) — `tdarr-server.service` carries no `NODE_OPTIONS`, and the process
was confirmed running at `Max address space 10240000000`. The prompt now names
WASM/MediaInfo OOM and quota/`EDQUOT` write failures as explicitly **reportable**,
so this suppression layer can never grow over them.

REA test suite **90 → 117 assertions**, all green; both rebuilt sources verified
by running the regenerated heredoc live against the box.

**Tdarr Mediainfo: diagnosed, unfixable at our layer, now monitored.** Two fix
attempts were tried and both falsified — recorded here so nobody burns the
afternoon again:

- `NODE_OPTIONS=--disable-wasm-trap-handler` (the `node-ultracc-wasm-fix` that
  `qflix-dash` carries) is **rejected outright** by Tdarr's bundled Node v18
  runtime — `"--disable-wasm-trap-handler is not allowed in NODE_OPTIONS"`. It's
  a CLI-only flag and the launcher spawns the runtime itself. Deploying it
  crash-looped `tdarr-server` (exit 9, 2 restarts); rolled back within the
  minute and the server confirmed healthy again at `NRestarts=0`.
- `--max-old-space-size=512` *is* accepted but doesn't help. Capping the heap
  frees nothing: on this box even `new WebAssembly.Memory({initial:1})` — one
  64 KB page — fails, because Node reserves a ~8 GB trap-guard region per wasm
  memory and the slot's `ulimit -v` is 10 GB. `LimitAS` can't be raised
  (soft == hard). `/usr/bin/mediainfo` v24.12 exists natively, but Tdarr's
  scanner is obfuscated (`srcug/`) with no path knob, and any patch dies at the
  next Tdarr update.

Impact is bounded — FFprobe and Exiftool carry the pipeline and scans complete
normally — so the answer is monitoring, not a fix. New **`tdarr-scanner` canary**
(hourly, `Canary Tdarr Scanner`) reads Tdarr's own four startup self-tests and
reds **only on regression**: FFprobe or Exiftool going down (pipeline-blocking)
or the server not running. The known-dead pair stays green-with-WARN rather than
parking a permanent red over an accepted condition — the same disease this entry
started by curing. If Mediainfo ever recovers the canary says so and asks to be
dropped from the baseline, so a later relapse reds properly; an indeterminate
read (log rotated away) stays UP rather than false-redding. All five branches
exercised live against fixtures.

`bootstrap-kuma-monitors.py` did **not** return a `pushToken` for the freshly
created monitor — the `reaper-kuma-token-gap` failure mode, which would have left
the canary silently never pushing — so the token was read from `kuma.db` and
persisted to both stores. Verified end to end: real heartbeat `status=1`,
`kuma audit` reports no drift (manifest 58 = matched 58). Counts moved
**16 → 17 canaries**, **57 → 58** manitoba monitors; unit suite **1089 green**.

## 2026-07-25 — Anime-library janitor (ships dry-run) + REA completeness overhaul

**Anime-library janitor** (`scripts/maint/qflix-anime-janitor.py`, spec
`docs/superpowers/specs/2026-07-24-anime-library-janitor-design.md`) — a daily
box-side corrector, reaper-parity. It classifies titles in the Anime / Anime
Movies libraries (Sonarr2 / Radarr2) via a 4-quadrant genre + origin tier and,
when armed, re-homes confirmed non-anime OUT to the main Sonarr/Radarr; it flags
the reverse (real anime sitting in the main libraries). Ships **DRY-RUN** (timer
`manitoba-maint-anime-janitor`, 03:00 UTC) — `--execute` stays disarmed pending
Phase-0 live validation + a re-council of the execute path.

- **Council (arch tier)** route_back'd the first cut *and* its four generated
  fixes over a shared path-traversal blocker. Every confirmed finding is
  implemented + covered by **30 unit tests**: a containment invariant under
  `to_root` before any `os.rename`; import verification before the source
  delete (no async-rescan orphan); created-vs-adopted rollback (never delete an
  adopted record); valid-id guard (rejects the 0/None sentinel); empty-stub
  reclaim; `fcntl` run-lock; `EXIT_FATAL` on an arr enumeration failure;
  Plex-optional (Plex not load-bearing); id-match adoption guard.
- **Phase-0 live validation** caught real misroutes immediately: the live-action
  *Cowboy Bebop (2021)* in the Anime TV library (auto-move OUT), and *Chainsaw
  Man* + three anime movies sitting in the main libraries (flagged).
  `originalLanguage` confirmed populated on live records.

**REA completeness overhaul** (`scripts/local-llm/qflix-rea.ps1`, gitignored) —
a live-state audit found REA had **never actually worked**: it fed the models
the base64-encoded blob instead of the decoded logs, so every model no-op'd.
Fixed the decode; fixed two silently-dead sources (bazarr's `log/` dir + Plex's
docker `~/.config/plex` path); added seven sources (SABnzbd, Tdarr, Kometa,
config-sync, dash/newsletter/unpackerr, reaper daily log, and a VictoriaLogs
aggregate) — **7 → 14 sources**; per-section cap 16384 → 3000 for output
headroom; 90 REA unit tests green. Re-fired: **4 real findings** posted to
Discord.

## 2026-07-20 — SAB stuck-handling FULL parity: detection, autonomous unstick, restart_repair breaker, usenet on every *arr

Usenet is now a first-class citizen of the stuck-download pipeline
(spec: `docs/superpowers/specs/2026-07-19-sab-stuck-parity-design.md`;
operator scope: "absolutely nothing left behind"). Delivered + deployed:

- **Detection**: hourly snapshots now carry a `sab` section (new
  `lib/sab_client.py`); the stale-state loop tracks SAB slots with the same
  3-snapshot zero-movement engine, `kind`-tagged entries, SAB rules
  `sab-paused-pinned` (SAB's `object.py` force-pause wedge — research proved
  the *arrs NEVER self-heal it), `sab-zero-movement`, `sab-pp-hung`
  (tracked, never auto-unstuck). Ghost prune is union-aware per kind.
- **Autonomous unstick**: `unstick.py` dispatches by id shape
  (`SABnzbd_nzo…` vs 40-hex), auto-detects the *arr from the SAB slot
  category, and gains a SAB orphan-cleanup twin (`del_files=1`). Core
  DELETE+blocklist+re-search flow unchanged — proven live 2026-07-19.
  Shared 10/day cap, shared events log, armed from day 1.
- **Circuit-breaker**: if an unstick no-ops (wedged queue object survives
  ≥1h) or post-processing hangs ≥4h, the collector fires SAB
  `restart_repair` (restart + queue rebuild — the only documented remedy),
  latched to 1/24h, Discord-warned, event-logged, verified by re-poll.
- **Usenet everywhere** (`90b-usenet-all-arrs.py`, executed live): radarr,
  sonarr2, radarr2 each got the SABnzbd download client (own category),
  NZBgeek wired DIRECT, delay-profile usenet enabled, FDH confirmed on.
  SAB `history_limit` 10→0 (documented unsafe with *arr FDH — could prune
  a Failed row before the *arr reconciled it).
- **Heartbeat doc**: stuck rows carry `kind` (torrent|usenet) with
  collision-free labels; SAB stuck rows join the name-map (no longer
  ghost-dropped); new crit alert for SAB's FDH blind spot ("Unpacking
  failed, write error or disk is full" → *arr Warning, FDH skips it).
  Phone app needs no rebuild.
- Built by a 5-agent parallel fleet + suite reconciler + adversarial
  reviewer (2 crits caught pre-deploy: prod collector wasn't requesting
  the sab section; unconditional sab key would have mass-pruned). Suite:
  1003 green, 139 new tests.

## 2026-07-19 (later) — Usenet monitoring parity: SAB stall canary + failure alerts

The stack went usenet-live 2026-06-22 but monitoring stayed torrent-shaped.
Proven the hard way tonight: **2 SAB jobs sat slot-Paused since 07-16**
(wedged SAB queue objects — resume API silently no-oped, app restart no
help) with every surface green. Cleared via the designed unstick path
(delete + blocklist + auto re-search) → replacements at 215 MB/s.

New: `sab-stall` canary (15th canary, every-15min, Kuma **"Canary SAB
Stall"** #103) with two predicates — queue speed ~0 ≥10 min with active
slots (dead provider/creds) and slot-Paused job pinned ≥24 h (tonight's
class) — plus `downloads.sab.failed_24h` in the heartbeat doc with a
"N Usenet download(s) failed (24h)" warn alert (reaches the phone with no
app rebuild). Counts synced: 15 canaries / 54 manitoba / 55 total.

## 2026-07-19 — Phantom stuck downloads + silent reaper heartbeat, both fixed

**Heartbeat app showed 5 stuck downloads vs 0 real.** Root cause: acted-on
unstick candidates whose torrents were long gone from qBit lingered in
`stale-state.json` forever — the delta-based prune needs 3 snapshot samples,
which a gone torrent never produces. Fixed in two layers: the collector
(`qflix-collect.py`) now prunes tracked hashes absent from the latest
snapshot's torrent list (guarded against a failed qBit collect mass-pruning
legitimate state), and `app_status.py`'s `build_stuck_list` skips candidates
not present in live qBit, keeping the doc honest between hourly collects.
Deployed + verified live: stale-state 5 ghosts → 0, phantom warn alert gone.

**"QFlix Reaper" Kuma monitor red despite clean reaper runs.** The
`qflix-reaper` key had **never** existed in `~/secrets/kuma-push-tokens.json`
(absent from every backup back to 2026-05-22), and `_push_kuma()`'s
missing-token early-return was silent — no journal or logfile trace — so the
monitor red-looped on Kuma's 25h watchdog (3rd recurrence: 07-13..15 were
"fixed" with un-persisted manual pushes). Fixed durably: token persisted into
the secrets file (from the monitor's own DB row), missing-token path now
`warn()`s into the durable logfile, monitor pushed green same day.

**Deploy parity swept** (box vs repo, 58 files): 1 drift closed
(`functional-audit.py` picked up the 07-13 Homarr-decommission edit), rest
match; `bazarr2-sync.py` confirmed in parity at its `~/.opt/maint/` home.

**Tdarr dual-default audio bug — FIXED via new audio-disposition janitor.**
Tdarr's ensure-AAC flow step leaves BOTH audio tracks flagged `default`
(original EAC3 + added AAC — ffmpeg copies the disposition from the source
stream), so Plex tie-breaks to the EAC3 track and live-transcodes audio
despite the compatible AAC track sitting right there. Dry-run showed
**318 of 424** library files affected. The installed
`ffmpegCommandEnsureAudioStream` 1.0.0 plugin has no disposition control, so
the fix is a new standalone janitor (compartmentalization law; portable
as-is to qflix2): `scripts/maint/audio-disposition-janitor.py` +
`manitoba-maint-audio-disposition.{service,timer}` (daily 04:30 UTC, Kuma
push monitor "QFlix Audio Disposition"). Narrow predicate — only the exact
dual-default-with-AAC-compat pattern — disposition-only stream-copy remux,
ffprobe post-verify, mtime preserved, atomic replace, Tautulli
active-session skip, 50/night cap (backlog converges in ~7 nights). First
supervised batch verified live: EAC3 `default=0`, AAC sole default. Video
HEVC→H264 transcodes on Plex Web remain by design (browsers can't decode
HEVC; the flow only targets VC-1/MPEG-2).

## 2026-07-18 — TV fallback v2: Season-0 specials janitor + TV park-only

**New: standalone specials-policy janitor** (`scripts/mcp/specials_policy.py` +
`qflix-specials-policy.{service,timer}`, daily 06:00 UTC, own Kuma monitor
"Qflix Specials Policy"). Enforces **"Season 0 is never monitored"** across
sonarr/sonarr2: unmonitors any monitored S0 episode and clears the Season-0
season flag (the flag clear is what makes it durable — a series refresh
otherwise re-monitors episodes to match the flag). Deliberately a separate unit,
not folded into quality-fallback, so it stays compartmentalized / independently
tunable as QFlix migrates to larger servers. Motivated by a 2026-07-18 stuck-TV
investigation: the `quality_fallback` TV digest was almost entirely Season-0
specials (Ted Lasso promo featurettes, Chainsaw Man recap/chibi shorts) with
**zero obtainable releases at any quality**, plus one queue stall (Graham Norton
S33E12, unstuck). Live remediation swept the specials the same day.

**TV fallback goes park-only** (`quality_fallback.py`): a real (non-specials)
aired+searched episode still missing at day 15 is unmonitored + Discord-warned
(day 5 stays an info heads-up), blast-capped 10/run. No quality-loosening ramp
for TV — Sonarr profiles are per-series, so loosening one stuck episode would
drop the whole series, and release-less items grab nothing at any quality.
Resolves the "TV alert-only, v2 decided from data" deferral from the 2026-06-06
design.

**Confidence: two-round Council (v2) adversarial review.** Round 1 routed back 3
major masked-live-write-failure defects (swallowed episode-fetch failure left S0
monitored under a cleared flag with Kuma green; park recorded before the
unmonitor was confirmed, no retry; exit code ignored TV-park failures) + 2
hardening items; all fixed TDD. Round 2 returned COMMIT (20/20 lens verdicts
pass, each with an executable artifact). The conditional D8 deploy gate was
cleared with a live `PUT /series` round-trip proving Sonarr 4.0.17 accepts the
full-object body with non-S0 flags preserved and no fields dropped. Suite 881
green. Ledger: `.claude/council-ledger.jsonl`.

**Manifest/docs reconciled:** the 3 stale `Quadstronix` externals (removed from
Kuma 2026-07-16 but left in `kuma_external_monitors`) dropped from the manifest,
fixing a `test_doc_counts` red since 07-16. Counts across README / inventory /
wiki / FAQ now match live: **35 apps · 52 manitoba · 53 total Kuma monitors ·
14 canaries**.

## 2026-07-16 — QFlix Heartbeat v2 phone app · reaper token restore · CI time-bomb

**New: QFlix Heartbeat v2** — personal read-only Android dashboard
(`apps/heartbeat-android/`, Kotlin/Compose) fetching one JSON doc from a new
seedbox aggregator (`scripts/mcp/app_status.py`) over a dedicated ed25519 key
locked to `command="python3 …/app_status.py",restrict` in authorized_keys — the
key can only emit health JSON. Sections: quota bars (disk GB+% / bandwidth
%-only — Ultra.cc hides GB from user accounts), Kuma up/down + reds, live
streams/users fraction, top-5 requests + watch time (30 d), downloads/stuck/
unsticks, derived alerts. Installer `scripts/configure/74-heartbeat-status-install.sh`
(idempotent; gate detects unrestricted key duplicates); one-shot
`apps/heartbeat-android/provision.ps1` moves the key to app-private storage and
deletes the box copy. 5 adversarial-review fixes applied (incl. authenticated
host-key pinning — no TOFU). Old `com.qflix.heartbeat.debug` uninstalled; its
source was never retained. Spec + plan under `docs/superpowers/`, both stamped
as-built.

**Reaper Kuma red (07-15 → 07-16) root-caused: missing push token, not a reaper
fault.** The `qflix-reaper` entry had vanished from `secrets/kuma-push-tokens.json`;
`_push_kuma()` silently no-ops on an empty token, so monitor #97 starved while
the daily 05:00 UTC `--execute` runs kept succeeding (07-16 run deleted
"The Pacific" correctly). Token restored from kuma.db (backup
`kuma-push-tokens.json.bak-2026-07-16`). Confidence work: new
`tests/unit/test_reaper_e2e.py` (11 tests driving the real `run()`/`main()` —
age boundary, cap ordering, exclusion rail, dry-run vs execute exact call
lists, manifest-before-delete, Kuma up/down/empty-token regression) + a live
box dry-run with independent before/after API counts across 9 surfaces proving
zero mutation. Known live posture: `--max-pct 100` drop-in disables the
%-tripwire (operator decision 2026-07-13, documented in the on-box drop-in);
exclude file has zero active rules.

**CI red since 07-15 was a fixture time-bomb** — `tests/fixtures/arr-queue/cluster.json`
hardcodes ETA `2026-08-13`; the slow-cluster predicate needs ETA > now+30 d,
which the calendar crossed on 07-15, breaking 2 `test_arr_housekeeping.py` tests
on every push. Tests now inject now-relative ETAs. Suite 862 green.

**Kuma pruned by operator:** `Quadstronix` + `Node 1` + `Node 2` externals
removed (stale DNS, both nodes resolved to one dead IP) — 55 → **52 monitors**.

**Heads-up:** disk quota crossed **80%** (2242/2794 GB) on 2026-07-16 — the new
app's amber warn fired for it.

**A single un-resolvable orphan used to page the reaper twice daily forever.** An
"orphan" is a Plex item aged past the 60-day threshold that resolves to no unique
*arr id (no backing *arr record, or missing external guids) — by design never
deleted. But it set the same `partial` flag as a transient operational failure,
so it fired an ERROR notify + Kuma-down on **every** run until a human cleared it.
Triggered by "Frieren: Beyond Journey's End" (anime series, files removed
out-of-band, `sonarr2` empty → UNRESOLVED) on 2026-07-14.

Fix (`scripts/maint/qflix-reaper.py`): split operational failures from orphans and
put orphans on a **24h time-grace**, tracked in a durable state file
(`~/.opt/maint/reaper/orphan-state.json`). A **fresh** orphan (first seen ≤24h)
still reds the run so you notice newly-stranded media; a **known** orphan (older)
goes **green** and is surfaced via `--json`, the durable log, and a throttled
**weekly WARN** reminder — no more forever-red. Operational failures
(DELETE/Seerr/Plex/arr) page exactly as before. The safety rail (an orphan is
NEVER deleted) is untouched. New flags `--orphan-grace-hours` (24),
`--orphan-remind-days` (7), `--orphan-state` are defaulted so the systemd units
need no edit. The reminder slot is consumed only at the guaranteed emit point, so
a cap-trip / lock-held abort can't silently swallow it. 19 new unit tests (52
total green). Spec:
[`docs/superpowers/specs/2026-07-14-reaper-orphan-grace-design.md`](docs/superpowers/specs/2026-07-14-reaper-orphan-grace-design.md).

## 2026-07-13 — Homarr fully decommissioned (killed a 31-alert restart-storm)

**Homarr is gone.** It was superseded as the public root by the qflix-dash
SvelteKit board on 2026-06-27 and left running only to serve a "QFlix has moved"
notice. On 2026-07-13 its container died and stayed down; the pusher's auto-heal
exhausted its 3 `app-homarr restart` attempts and then paged Discord on every
permanent-failure re-arm — **31 alerts** for an already-retired app.

Full decommission (Maintainerr-decom pattern): Kuma monitor **#39 "Homarr"**
deleted (off the public status page + no more down-notify); `homarr` removed from
`manifest/apps.yaml` and the deployed `~/.opt/maint/apps.yaml`, pusher + webhook
restarted (auto-heal loop stopped); both UCC slots uninstalled (`app-homarr` +
`app-homarr-upstream`, config backed up to `~/.apps/backup/`); `homarr.{host,port}`
secrets and the push token purged. Retired the Homarr-only configure scripts
(`34-nginx-root-to-homarr.sh`, `35-homarr-seed-boards.py`, `46-homarr-add-comms.py`,
`61-homarr-qflix-theme.py`) and `scripts/qflix-dash/homarr-moved-notice.py`. The
`mobile-ux` canary was **kept** — it was repointed to the dashboard on 2026-06-27
and now guards the live homepage. Counts: **34 apps / 51 manitoba Kuma monitors**
(was 35 / 52). The old `homarr-upstream-<host>` subdomain is now dead (expected —
uninstall tears down the outer-nginx route).

## 2026-07-09 — Newsletter digest routine fix + never-silent detection

**The weekly "Behind the scenes" cloud routine had silently stalled.** It fired
every Monday but stopped publishing after 2026-06-29 (the 07-06 run produced no
commit), so the newsletter quietly fell back to the deterministic commit recap
with no alert. Root cause (council-v2 diagnosis): the routine's pinned model
`claude-sonnet-4-6` — a prior generation — most likely went unavailable between
the last success and the failure; the session fired but couldn't do the work.

Fixed: the routine's model updated to `claude-sonnet-5`, and the `qflix-digest`
skill hardened with VERIFY-AFTER-PUSH (re-fetch + assert `week_of`==today; a curl
failure or mismatch is a run failure) + FAIL-LOUD (Gmail alert on any failure).

**New detection canary `newsletter-digest-stale`** — the real fix, so it's never
silent again: it checks the `newsletter-digest` branch's `week_of` against the
newsletter's own `_is_fresh` rule at Monday send time (fires 14:20/14:50/15:20
UTC) and pushes DOWN → Kuma + Discord when the blurb is stale/absent/malformed.
Enforcement is gated to the Monday send window so the rule's +4-day freshness
bound never false-alarms mid-week. Kuma monitor "Canary Newsletter Digest"
bootstrapped (14th canary; 52 manitoba monitors). Monitor counts reconciled
across README/inventory/wiki and the doc-counts test now counts the 3
auto-injected monitors (pusher self-heartbeat, fleet aggregate, QFlix Reaper).

## 2026-07-08 — Self-healing hardening, stream cap, docs reconciled to live

**qBittorrent WebUI auto-heal fixed (d8d82bf).** A host maintenance reboot left
qBit's WebUI unable to bind its port; auto-heal couldn't recover it because
recovery ran `start` (a no-op on a running-but-degraded app) instead of
`restart`, and the permanent-failure latch never re-armed. Recovery now
restarts, and the latch auto-re-arms after a cooldown so a cleared transient
self-heals. Added a boot-time TCP-listener snapshot (`boot-listeners-snapshot.sh`)
to identify a port squatter next reboot, and widened qBittorrent's recovery
backoff (5c5d4de).

**Stream cap raised to 4 per member (4865aba).** The `kill_stream` and
`stream_stats` every-minute crons are now reproducibly provisioned by
`scripts/configure/59a-plex-stream-crons-install.sh` (previously manual crontab
entries the repo couldn't rebuild).

**Documentation reconciled against the live environment** (council-v2 audit +
follow-up). Homarr → qflix-dash (public dashboard cutover is live), Maintainerr →
qflix-reaper, Gemini "AI Picks" retired (the "Behind the scenes" blurb is written
by a scheduled Claude cloud routine, with a deterministic commit-recap fallback;
a "This week's tune-ups" line reads `last-upgrade.json` on blurb-less weeks).
Counts corrected across wiki/FAQ/README/inventory (35 apps, 13 canaries, 51
manitoba Kuma monitors), the FAQ stream-cap prose fixed (per-member, not a global
total), and the deployed newsletter systemd unit's description de-Gemini'd.

## 2026-06-27 — Newsletter "Behind the scenes" + autonomous digest; Gemini retired; repo public

**Newsletter gains a "Behind the scenes" section.** The weekly digest
(`scripts/qflix-newsletter`) now renders a "🔧 Behind the scenes" block between
Coming Soon and Nerd Corner, summarizing what improved for members that week.
Two sources, override-then-fallback, both fail-safe (hide the section, the email
still sends): (a) a Claude-authored, non-technical blurb published to the new
**`newsletter-digest`** branch as `digest/latest.json`, freshness-guarded so a
stale blurb is never shown; (b) a deterministic recap built from the week's
public GitHub commits — grouped feat / fix·perf, scope-stripped, with an opt-in
`Newsletter:` commit-body trailer to override any subject with friendly copy.
New `changelog.py` + 18 unit tests (27 in the package, all green).

**Gemini / "AI Picks" retired.** Confirmed from the seedbox logs: every run since
2026-05-11 hit `HTTP 429 quota exceeded, limit:0` for `gemini-2.0-flash` (free
tier revoked on the deprecated endpoint), so the section never once rendered.
Deleted `ai.py`, the `google-generativeai` dependency, the config field, the
template block, the install copy line, the `gemini.api_key` secret (local +
seedbox), and the stale `secrets-convention` row (replaced with optional
`github.repo`).

**Autonomous weekly digest routine.** A scheduled cloud agent (the `/qflix-digest`
skill, committed at `.claude/skills/qflix-digest/`) runs in Anthropic's cloud
every **Mon 14:00 UTC** — one hour before the 15:00 UTC send — reads the week's
commits from its own checkout, writes the member blurb, and pushes it to the
`newsletter-digest` branch. It runs independently of any workstation; proven
end-to-end (a test-fire pushed `5b4aa20` in ~20 s). If a run is missed, the
deterministic commit recap covers it. Routine `trig_01ARibSXarcy5ddQdDXiV3Dp`.

**Repo made public.** `Quadstronaut/QFlix` flipped to public after verifying the
history is secret-clean (264 commits, root is the deliberate "sanitized public
release" squash, `secrets/` never tracked, no keys/tokens/private keys in any
diff). This removes the need for any GitHub token on the seedbox or in the cloud
routine — both read commits + the digest unauthenticated. Scrubbed the real SSH
host + a stale local path out of the two `buildarr-v4-patch-session-*` docs.

**Reusable single-recipient test send.** `python -m qflix_newsletter --test-to
EMAIL` renders the true production email and fires a single Listmonk test to one
address (recipient must be a subscriber) without mailing the list.

## 2026-06-26 — Maintainerr decom finished, SABnzbd manifested, qui removed

A council audit found the 2026-06-20 Maintainerr decommission left live
references behind — including a broken disk-safety path. A second council (arch
tier, unanimous 5-lens **commit**) produced the code fixes.

**Fixed — broken autonomous disk-reclaim (`scripts/canaries/quota.sh`).** The 90%
CRITICAL branch still read `~/secrets/maintainerr.key` and POSTed to the
decommissioned Maintainerr subdomain (502) — so autonomous space reclaim above
90% disk silently failed and would have marched to the 98% FAIL wall. Repointed
to the deployed replacement `python3 ~/scripts/maint/qflix-reaper.py --execute
--json`, relying on the reaper's built-in `--max-items`/`--max-pct` caps +
run-lock (no `--force`); preserves the `STAGE=quota-critical`/`quota-reclaim-fail`
labels and Kuma-DOWN exit. Verified live: canary runs `PASS` at 63% (reaper
branch correctly dormant).

**Orphan references purged.** `scripts/mcp/logs.py` (dead maintainerr glob route),
`scripts/smoke-test-plex.sh` E18 + `scripts/smoke-test.sh` #11 (maintainerr tests
that false-failed once the key was gone), and `scripts/ops/maintainerr-fix-watch.ps1`
(205-line dead watcher, deleted). Orphan `~/secrets/maintainerr.key`/`.port`
deleted on the seedbox (only stale `apps.yaml` backups still name them). Two
benign historical comments left intentionally.

**SABnzbd manifested (33→34 apps).** The 2026-06-22 usenet buildout left SABnzbd
unmanaged. Added a `sabnzbd` UCC entry (`kuma_monitor: SABnzbd`, http_root
`/sabnzbd/` expect 200 — SAB's form-login redirect resolves to a final 200) and
created the live **SABnzbd** Kuma PUSH monitor + token via
`bootstrap-kuma-monitors.py`. Pusher now reports **34/34 ok**, sabnzbd pushes
`up`, health 200. README/inventory counts reconciled: apps 33→34, manitoba
monitors 47→48, total 51→52.

**qui removed.** Orphaned autobrr qBittorrent web-UI (`app-qui`, port 42010,
installed 2026-05-25, zero references, not in manifest/inventory) uninstalled via
`app-qui uninstall` — service/unit/binary/data gone, port freed.

Repo 715 tests pass / 5 skip; 0 failed systemd units on the box. Council ledger
in `.claude/council-ledger.jsonl`.

## 2026-06-26 — VictoriaLogs crash-loop fixed (thread-cap exhaustion)

**Alert:** `✗ victorialogs could not be started after 3 attempts — operator needed`
— fired 06-23 (×2), 06-24, 06-26 per `~/.opt/maint/notify.log`; `lib/recovery.py`'s
3-attempt loop exhausted every time and marked the app permanently-failed.

**Root cause — `pthread_create: Resource temporarily unavailable` (EAGAIN) → SIGABRT.**
Storage opens cleanly, then the process aborts while spinning up worker/flusher
threads. The shared Ultra.cc seedbox exposes all **128 host cores**, so the Go
runtime defaulted `GOMAXPROCS=128` and burst a ~thread-per-core pool at startup;
combined with the per-partition flusher fan-out over **86 daily partitions** (90d
retention), that pushed the *user's* total OS-thread count past the `ulimit -u` /
`RLIMIT_NPROC` = **2000** cap (the rest of the QFlix stack already holds ~1000
threads — python3 alone 239, Plex 162). `pthread_create` then returns EAGAIN and
the process SIGABRTs in a 10s `Restart=on-failure` loop, never binding
`127.0.0.1:42015`.

**Why now:** hard failures begin **06-23**, the day after the **06-22 usenet
buildout** (SABnzbd/Frugal) raised the baseline thread count over the edge. The
06-11 `recovered after 3 attempt(s)` entries were the early warning.

**Fix:** `Environment=GOMAXPROCS=4` in `scripts/maint/systemd/victorialogs.service`
(deployed to `~/.config/systemd/user/`). vlogs is I/O-bound, not CPU-bound (3s CPU
over a 48s boot), so capping the scheduler bounds the thread high-water mark at no
perf cost.

**Verified:** 2 clean restarts, **0** `pthread_create` aborts, thread count 11–28
(was bursting past 2000); `is-active=active`, **health 200**, ingest cycle **25178
lines / 0 failures**, LogsQL count query returns data. Steady-state clean boot
**47.6 s** (the first post-crash boot took 156 s clearing unclean-shutdown debris —
a one-time cost). `manitoba-maint-pusher` re-probes healthy and clears the
permanent-failure mark.

**Recovery-window hardening (implemented):** the 48 s clean boot fits recovery's
`[10,30,60]` s probe window (3rd probe ≈100 s), but an unclean-shutdown debris boot
(>150 s) would trip a false alert. `lib/recovery.py` now reads `recovery_attempts`
/ `recovery_backoff_s` / `kuma_recheck_delay_s` from the per-app manifest entry
(`App.raw`) before falling back to the global `defaults`; the `victorialogs` entry
sets `recovery_backoff_s: [30, 90, 180]` (probes at ≈30/120/300 s — catches both the
48 s clean boot and a >150 s debris boot). Covered by 3 new `test_recovery.py` cases;
full suite 715 pass / 5 skip.

## 2026-06-24 — Full-stack audit (host · apps · canaries · scripts · 72h logs)

End-to-end audit against the live seedbox. Headline: **33/33 apps UP, 13/13
canaries green, 711 pytest pass, smoke 51/56** (4 fails were non-faults). No
host/perf problems — disk 1.77 TB / 2.79 TB quota (63%), load is shared-box
noise, glibc 2.31 (the documented Tdarr pin reason).

**Fixed this audit:**
- **Prowlarr `prowlarr-indexer-health` canary was red** (true-positive): the dead
  public indexer **`TorrentDownload`** (id=5) had been failing >6h, tripping
  Prowlarr's health warning and the *arr "Indexers unavailable" warnings.
  Disabled it + triggered `CheckHealth` → 0 issues, canary green.
- **`smoke-test.sh` + `scripts/canaries/README.md`** still referenced the retired
  `deletion` canary (its Kuma monitor was deleted 2026-06-20, so smoke false-
  failed every run). Swapped the spot-check to `quota`; README now lists the real
  13 canaries (added `prowlarr-indexer-health` + `quota`, dropped `deletion`).
- **Doc reconciliation** — README/inventory/FAQ still presented **Maintainerr as a
  load-bearing app** and an off-by-one **34→33** app count. Completed the
  2026-06-20 Maintainerr→`qflix-reaper` decommission across all three: required-
  apps table, library-hygiene + monitoring diagrams, timeline (appended 06-20 /
  06-22 / 06-24), FAQ canary table (14→13, swapped `deletion`/`maintainerr-rule-
  sanity` for `tautulli-plex-link`), and the smoke buckets.

**Remediated in the follow-up pass (2026-06-25):**
- **#2 Sonarr↔SABnzbd "Connection refused" → FULLY FIXED.** SAB was never down. The
  SAB download-client was recreated fresh (**id 6 at `172.17.0.1`**) with the real
  API key. The live Sonarr **debug log** shows it polling `http://172.17.0.1:17007/
  sabnzbd/api` (mode=queue/history) every minute and succeeding — **zero `127.0.0.1`
  across 28+ min of debug log**; the last real SAB event in `sonarr.txt` is a Jun-23
  *success*. Correction: `~/.apps/sonarr/sonarr.db` IS host-readable and is **clean
  of `127.0.0.1`** — the earlier "container-private, no lever" claim was wrong (the
  DB was never the source).
- **Plex library-update connection → FULLY FIXED on all four *arr.** They were pinned
  to the dead **`172.17.1.250:32400`** — the pre-re-IP Plex address that only Tautulli
  had been migrated off after the 2026-05-20 Ultra.cc kernel migration (per
  `50-tautulli-pms-url-fix.sh`). Repointed live to the stable bridge gateway
  **`172.17.0.1:<plex.port>`** (all four test 200; verified `172.17.0.1:17025` reaches
  Plex `/identity` 200 and the dead `172.17.1.250:32400` does not). Source fixed:
  `09-phase5-arr-connects-and-sync.sh` `PLEX_HOST` → gateway, not `plex.host`
  (=127.0.0.1). Native Plex refresh-on-import works again (last real failure Jun-23).
- **Observability fix — `scripts/mcp/logs.py` mis-timestamped continuation lines.**
  Multi-line stack-trace lines (no leading timestamp — `---> ...Connection refused
  (127.0.0.1:17007)`, ` -- : Test was aborted`) were assigned the *ingest* time, so
  old pre-fix exception lines resurfaced in VictoriaLogs as phantom "recent" errors.
  This made the SAB/Plex fixes *look* unapplied and fed the council a corrupted error
  signal. Fixed `collect_for()` to carry each timestamped line's ts forward to its
  continuation lines; the real Sonarr logs were clean all along.
- **#3 Maintainerr → fully UNINSTALLED.** `app-maintainerr uninstall` — `~/.apps/
  maintainerr` removed, no process, no UCC auto-restart, subdomain now 502.
- **#4 Orphan cleanup done.** `git rm` of `scripts/canaries/{deletion,maintainerr-
  rule-sanity}.sh` + their 4 systemd units; removed the matching refs in
  `240-maintenance-install.sh`; removed the dead unit files + deployed scripts from
  the box. `functional-audit.py`: dropped the MAINTAINERR section and fixed the
  Tautulli urlbase + Plex `<MediaContainer version>` probe bugs.
- **#5 `QFlix Reaper` registered.** Added to `audit_monitors()`'s expected set
  (alongside `Manitoba Pusher` / `QFlix Fleet`) — `kuma audit` now reports **no
  drift** (49/49 matched), no orphan.

**Deferred:**
- **#1 Sonarr/Sonarr2 4.0.17.2952 → 4.0.18.2971** and **qBittorrent 5.0.3 → 5.2.x**
  — left for the Monday `cp-upgrade` weekly sweep (Radarr pair already latest).

**72h log scan:** 7.40 M lines ingested, **97,454 ERROR-level**; journald `-p err`
clean (0). 97% of ERRORs are benign Plex `Unknown metadata type: folder` spam
(94,930). See the audit report for the full per-app error table.

## 2026-06-22 — Usenet path: SABnzbd + NZBgeek + Frugal (fix dead-swarm back-catalog)

A member flagged Vanderpump Rules S2 present but S1 missing. S1 (2013 Bravo
reality) exists on the stack's public trackers only as **dead-swarm SD** — 8/10
episodes had 0 real seeders (indexers reported fake "100+ seeders"; qBittorrent
showed 0 connected, while an unrelated download pulled fine). The stack was
**public-torrent-only** (qBittorrent + 22 public trackers, no Usenet/private),
so old back-catalog was unsourceable. Built a Usenet path; S1 now live in Plex
as 10/10 1080p.

- **`scripts/configure/90-sabnzbd-usenet-install.sh`** — installs SABnzbd
  (`app-sabnzbd`), adds the Frugal provider + `sonarr` category + docker-bridge
  host_whitelist, wires it into Sonarr as a download client via the bridge
  gateway `172.17.0.1:<port>` (the *arr run in linuxserver Docker — their
  `127.0.0.1` is not the host's), adds **NZBgeek** as a Newznab indexer, and
  flips the Sonarr delay profile to **enableUsenet + prefer usenet**.
- **Delay-profile fix (library-wide):** a fresh Sonarr ships
  `enableUsenet=False`/`preferredProtocol=torrent`, so automatic and
  failed-redownload grabs picked dead torrents over reliable NZBs ("Usenet is
  not enabled for this series" rejections). Now Usenet-preferred everywhere.
- **Prowlarr caveat:** its app-sync skips Usenet indexers that return nothing to
  the empty-term category probe ("No Results in configured categories"), so
  NZBgeek is held directly in Sonarr rather than synced.
- New gitignored secrets: `usenet.{host,port,user,pass,ssl,connections}`,
  `sabnzbd.{key,port}`, `nzbgeek.{key,url}` (see docs/secrets-convention.md).
- Not yet in `manifest/apps.yaml` (no Kuma monitor / lifecycle) — follow-up.

## 2026-06-06 — quality fallback: two-stage loosening for stuck missing movies (PR #66)

A movie missing for 5 continuous attempted days (proof via `lastSearchTime`)
gets swapped to `QFlix Fallback HDTV` (+HDTV-720p/1080p, +WEB 720p); day 10 →
`QFlix Fallback SD` (+SDTV/DVD/WEB 480p/Bluray-480p/REGIONAL); day 15 →
original profile restored, unmonitored, Discord alert. A grab at any fallback
stage restores the original profile so upgradinatorr/RSS can upgrade later.
CAM/TELESYNC/TELECINE/DVDSCR/WORKPRINT hard-banned everywhere; fallback
profiles live outside recyclarr's managed set. TV is alert-only (once-per-
episode Discord digest) — v2 decided from v1 data.

- `scripts/mcp/quality_fallback.py` — pure planners + null-skipping
  `PUT /movie/editor` writes (only `qualityProfileId` + `monitored`, ever);
  25-per-instance blast-radius cap; `--bootstrap-profiles|--cron|--emit-json|--dry-run`.
- `qflix-quality-fallback.timer` daily 07:30 UTC (30 min after missing sweep);
  manifest cron entry (34th app); Kuma monitor pending operator bootstrap
  (docs/operator-deferred.md).
- Verified live post-deploy: profiles on both radarr instances ban all
  pre-retail; dry-run clean; timer armed.
- Plan double-reviewed by 2 independent Opus panels (unanimous approve);
  all API payloads verified against deployed Radarr 6.1.1.10360 + source at
  tag (RTFM section in the plan doc).

## 2026-05-30 — Tdarr Phase 30 go-live: keep transcoding live (PR #65)

The live seedbox had `processLibrary=True` on all 3 libraries (Movies/TV/Anime)
— Tdarr actively transcoding via the `qflix-direct-play-fix` flow (484 files
catalogued, 637 health-checks, 20 transcodes). But the repo disagreed: the only
code touching `processLibrary` was `50b-tdarr-config.py`'s
`set_non_destructive_mode()`, which **forced it to False** ("Phase 30 gate, flip
in 50d") — and no 50d ever existed. **Hazard:** re-running the idempotent 50b
config would have silently flipped every library back to False and halted all
transcoding.

- **`set_non_destructive_mode()` → `ensure_library_processing()`.** Now enforces
  `processLibrary=True` (Phase 30 go-live, operator green-lit 2026-05-30).
  Idempotent: only writes a library that has drifted to False, so it also
  self-heals any library paused out-of-band. Re-running 50b now *preserves* live
  transcoding instead of killing it.
- **Verified live:** re-ran 50b over SSH against the box — reported
  `Libraries enabled for live transcoding: 0` (already True) with no `[lock]`
  output; box re-confirmed `processLibrary=True` on all three after the run.
- Docs: `inventory.md` (tdarr-server row) + `operator-deferred.md` (new Phase 30
  row) record that transcoding is live.

## 2026-05-30 — tdarr-node heartbeat honors fair-use quiet hours (PR #64)

Two auto-heal mechanisms were fighting each other. `50c-tdarr-quiet-hours.sh`
intentionally **stops** `tdarr-node` 18:00–23:00 UTC so its worker threads don't
compete with streamers during peak watch hours. But `heartbeat-tdarr-node.sh`
(cron `*/5`) restarts the node whenever the server reports **0 registered
nodes** — which is exactly what a paused node looks like. The watchdog revived
the node on the next tick, collapsing the 5-hour pause to ~2.5 minutes.

- **Observed 2026-05-30:** node stopped `18:00:01 UTC`, back up `18:02:27 UTC`
  instead of staying down until 23:00. Net effect: the node ran ~24/7 and never
  backed off at peak.
- **Fix:** `heartbeat-tdarr-node.sh` now early-exits during the 18:00–23:00 UTC
  pause window (UTC-hour guard, `10#` base-10 to avoid octal parsing of `08`),
  before any restart path. The watchdog still covers genuine failures outside
  the window. Window kept in sync with `50c`'s `OnCalendar` values.
- **Verified live (in-window):** deployed to seedbox, EOL-normalized SHA-256
  parity confirmed; with the node stopped, the heartbeat exits 0 and leaves it
  inactive (pre-fix it would have revived it). Today's pause restored manually.

## 2026-05-26 — flaresolverr-canary honors push-suppress + alert audit trail (PR #62)

FlareSolverr went into a crash-loop (HTTP listener connection-refused) pending
an Ultra.cc ticket (s6 `cap_setuid`). Its Kuma monitor was correctly muted via
the push-suppress registry (PR #61) — but the operator kept getting paged at
2 AM anyway. Root cause: a **second, independent alert path** that the registry
didn't cover.

- **`flaresolverr-canary.py` now honors the push-suppress registry.** The pusher
  mutes the `FlareSolverr` Kuma monitor and skips recovery when the app is in
  `push-suppress.json`, but the standalone restart-bot canary runs on its **own**
  5-min timer and pages Discord **directly** — it never consulted the registry,
  so it kept firing `restart REFUSED — 3 restarts in last hour ≥ cap 3 …
  crash-loop; operator intervention needed` every cycle. `run()` now checks
  `lib.suppression.push_suppressed(FS_SUPPRESS_KEY)` **first** and short-circuits
  to a clean no-op (no probe, no restart churn, no page). Fail-open: any registry
  read error falls through to normal alerting. The self-destructing unsuppress
  watcher already lifts the entry once FlareSolverr is live, restoring both the
  monitor and this canary in one move. **Lesson:** any standalone alert path
  (not just the pusher) must consult the suppress registry.
- **`lib/notify.py` now writes a full send-audit trail to `notify.log`.**
  Previously only *failed* sends were recorded (`notify-fail.log`); a delivered
  page left no trace. Every attempt — sent or failed — is now appended to a
  capped `notify.log` (token redacted), plus `logging.info`/`warning`. The file
  audit is caller-independent (canary uses `print()`+journal; pusher uses
  `logging`). `notify-fail.log` semantics are unchanged.
- **Deployed + verified live:** seedbox runtime files are byte-identical
  (EOL-normalized) to merged master; the canary journal logs `SUPPRESSED …`
  instead of paging. The suppress entry auto-restores when FlareSolverr is live
  (post-ticket); it can also be removed manually.

## 2026-05-22 — 7-gap triage closure + Kuma self-heal + hardlink-canary rewrite (PR #42)

Sweep across the seedbox that started as a triage of seven known gaps
and ended with two structural improvements: external-monitor push
tokens now self-heal, and the hardlink-integrity canary stopped
firing 20/20 false-positives. Final Kuma state: **50 UP / 2 dormant /
0 DOWN** (52 monitors total).

### Triage closures

- **`tdarr.server_port` + `tdarr.api_key` secrets deployed.** Both
  installer-bootstrapped via `scripts/configure/50-tdarr-install.sh`
  but had never run on the current host. Wrote them manually
  (`42018` and `tapi_…`), 0600 perms; `~/.config/tdarr` now serves
  `/api/v2/status` cleanly. Functional-audit goes green.
- **qBittorrent orphan categories purged.** `mylar` + `readarr` left
  behind from the 2026-05-11 purge sweep. Removed via
  `POST /api/v2/torrents/removeCategories` with a real-newline-
  separated body (URL-encoded `%0A` is treated as one category name
  &mdash; only `--data-binary @-` with a literal newline works). qBit
  category list now reads `[radarr, radarr-anime, sonarr-anime,
  tv-sonarr]`.
- **5 chronically-failing Prowlarr indexers disabled.** BTdirectory,
  0Magnet, TorrentProject2, EZTV, Torrent Downloads &mdash; all sat
  in long-term-failure for &gt; 2 weeks. GET/PUT roundtrip via the
  loopback API (URL bases require the <code>/prowlarr/</code> prefix
  or the proxy returns HTTP 307). Prowlarr health array clears to
  &ldquo;new update available&rdquo; only.
- **Radarr FNAF3 stale stub deleted.** TMDB id 1692507, Radarr id
  366, never had files and pointed at a non-existent TMDB record.
  Radarr health array now empty.
- **Plex log surface wired into VictoriaLogs.** Plex was the only
  managed app without a vlogs ingest route. Added route + non-ISO
  &ldquo;Mon DD, YYYY HH:MM:SS.fff&rdquo; timestamp parser (with
  month-name &rarr; numeric table) to
  <code>scripts/mcp/logs.py</code>; the ingest service auto-
  discovers from <code>_FILE_LOGS</code>, so a 24-hour vlogs query
  for <code>app:plex</code> immediately returned data. Self-test
  coverage 18 &rarr; 21.

### Structural improvements

- **`bootstrap-kuma-monitors.py` no longer wipes operator-placed
  tokens.** Previously the script built its tokens dict from scratch
  &mdash; only entries it re-synced on that run survived. Any
  manually-bootstrapped key (e.g. for an external PUSH monitor)
  vanished on the next run. The fix seeds the dict from the existing
  <code>secrets/kuma-push-tokens.json</code> before merging the
  fresh app/canary/pusher tokens.
- **External PUSH monitor tokens now self-sync.** Added a pass over
  <code>manifest.kuma_external_monitors</code>: for each entry whose
  Kuma type is PUSH (today: just &ldquo;QFlix Collect (workstation)&rdquo;),
  capture its <code>pushToken</code> and write it under the display-
  name key &mdash; that's what <code>Push-Kuma</code> in
  <code>qflix-collect.ps1</code> looks up. HTTP-type externals
  (Quadstronix nodes) are correctly skipped. <em>Net effect:</em>
  regenerating an external monitor in the Kuma UI no longer
  silently breaks its consumer; the next bootstrap sweep re-syncs
  the rotated token automatically. Closes the
  hourly-Discord-WARN failure mode that triggered this session.
- **`hardlink-integrity` canary rewritten qBit-side.** The old
  design sampled the 20 most-recently-modified library video files
  and failed if &gt; 50% had linkcount=1 &mdash; but qBit's
  share-ratio cleanup removes seeds faster than that sample window
  refreshes, so recent library files are almost always
  linkcount=1 not because *arr skipped the hardlink but because qBit
  deleted the source afterward. Fired 20/20 DOWN this morning while
  *arr was at 100% hardlink coverage (verified by inode cross-check:
  60/60 qBit-completed torrents shared an inode with a library
  path). The new design enumerates qBit completed-state torrents,
  stats each <code>content_path</code>'s (dev, inode), and looks up
  the media library for a sibling outside <code>~/downloads</code>.
  Two thresholds (both must trip): <code>MAX_DETACHED</code> count
  and <code>MAX_DETACHED_PCT</code> percent. Live run reports
  <code>qbit_completed=60 hardlinked=60 detached=0</code> in 214 ms.
  Old script preserved at
  <code>~/scripts/canaries/hardlink-integrity.sh.old-pre-rewrite-20260522</code>.
- **`docs/secrets-convention.md` documents two-copy layout.** The
  <code>kuma-push-tokens.json</code> entry now spells out that the
  seedbox copy (read by <code>manitoba-maint-pusher.service</code>
  and the canary push pipeline) and the workstation copy (read by
  <code>scripts/local/qflix-collect.ps1</code>) are independent,
  with the bootstrap script syncing both when run from their
  respective hosts.

### Verified

- Bootstrap deployed + re-run on seedbox: 48 &rarr; 49 keys,
  workstation token captured (<code>15fk3z95Dn</code>),
  0 monitors created (idempotent).
- Hardlink canary deployed + service fired:
  <code>Active: inactive (dead) since &hellip; status=0/SUCCESS</code>
  &mdash; first PASS after several hours of 2/INVALIDARGUMENT
  exits.
- Kuma audit final: **50 UP / 2 ? / 0 DOWN**. The two ? (Canary
  Deletion + Canary Kometa Deploy-Drift) are heartbeat-retention
  artifacts; they re-prime on their daily 04:30 schedule.

## 2026-05-20 — random-audit findings + Kuma-push silent-failure guard (PR #29)

The 2026-05-19 random audit (REA + qflix-mcp + manual log scrub) surfaced
a 5-day-silent regression: the workstation collector's
`"QFlix Collect (workstation)"` push-token went missing from
`secrets/kuma-push-tokens.json` after the 2026-05-14 tokens-file regen.
Every hourly `qflix-collect.ps1` run pushed nothing for five days; the
seedbox-side Kuma monitor stayed red while the workstation-side
`qflix_status` MCP (which reads the local snapshot, not seedbox Kuma)
reported all-green. The two views disagreed and nobody noticed.

- **`Push-Kuma` now fails loudly on missing tokens.** `scripts/local/qflix-collect.ps1`
  used to `return $false` silently when `$tokens.$monitor` lookup missed.
  Now writes a WARN line to the transcript and posts a yellow Discord embed
  (`"Missing Kuma push token for monitor: <name>"`). Any future regression
  surfaces within one collect cycle instead of "until somebody runs an
  audit and reads the raw fetch."
- **Token restored manually.** Local-only secret (`secrets/` is gitignored).
- **Side observation, not actionable.** A single `manitoba-maint-canary-vlogs-stall.service`
  failed-start at `2026-05-19T19:31:09Z` turned out to be a transient — the
  next eight consecutive 15-min fires (`00:46 → 02:16 UTC`) all completed
  cleanly with status=0/SUCCESS.

`WARN.md` (this audit's punch-list) committed at repo root as historical
record.

## post-release-0.0.1 (2026-05-16) — live-verification fixes

End-to-end verification on the live seedbox surfaced five deployment-time
bugs that unit tests couldn't catch. All landed via PR #22.

- **`bootstrap-kuma-monitors.py` manifest resolution.** Hard-coded
  `REPO_ROOT/manifest/apps.yaml` was unreachable on the seedbox (no repo
  checkout there). Now resolves `MANITOBA_MANIFEST` env var → deployed
  `~/.opt/maint/apps.yaml` → repo path, in that order.
- **Pusher pushToken capture.** `get_monitors()` raced behind the create
  and returned the new monitor without its pushToken, so
  `kuma-push-tokens.json` was missing the `manitoba-pusher` entry. Now
  captures the token from `_add_push_monitor`'s return as fallback.
- **Smoke-test K_EXCLUDE manifest path.** Python helper opened
  `manifest/apps.yaml` from cwd, silently dropping the external-monitor
  filter whenever the operator ran the script from `~`. Now tries both
  the local repo and the deployed manifest path.
- **`Manitoba Pusher` auto-included in drift audit.** The daemon's
  self-heartbeat monitor exists outside the manifest's app/canary sets,
  so `manitoba-maint kuma audit` flagged it as orphan drift. Now
  injected into the expected manifest set so it's always matched when
  present and reported as `manifest_only` (bootstrap needed) when absent.
- **Workstation collector declared external.** Added `QFlix Collect
  (workstation)` to `kuma_external_monitors` — it's a Windows scheduled
  task that the seedbox manifest cannot self-heal.

Live smoke-test on seedbox: **51/0/2** (2 skips = pre-existing
`tdarr.server_port` secret gap documented in `todo-after-claude.md`).
Test count 440 → 446 (+5 unit, +1 regression guard for the pusher-
drift case).

## release-0.0.1 (2026-05-16) — audit sweep

First tagged release. Closes ~90 findings from the 2026-05-15 top-to-
bottom audit. Built on top of `pre-release-0.0.1` (tagged at 7861768,
the merge of PR #20 — qbit-stall canary). See the
`release/0.0.1-audit-sweep` branch for the per-commit detail.

### Highest-leverage fixes

- **MCP→SSH shell injection closed.** `qflix_mcp.qflix_get_logs`,
  `qflix_unstick_torrent`, `qflix_diagnose_unstick`,
  `qflix_trigger_missing_search` now `shlex.quote` every caller-supplied
  argument. 5 regression tests against `;`, `|`, `&`, `$(...)`, backtick
  payloads.
- **PGPASSWORD off the command line.** `configure/43-listmonk-install.sh`
  pipes the postgres password via env-on-stdin so it no longer appears
  in `ps -ef` for the lifetime of every psql call during install.
- **Recovery loop unified.** Kuma webhook + pusher now share
  `recovery._RECOVERY_SEMAPHORE` + `_in_flight`. Permanent-failure mark
  prevents the pusher from re-firing `trigger_async` every 60s for the
  duration of an outage that has already exhausted the 3-attempt loop.
  Pusher's Kuma `msg=` annotates strike state for at-a-glance triage.
- **Health probes fail loud.** A missing `auth_secret` /
  `basic_auth_secret` returns `ok=False` instead of silently issuing
  an unauthed request that may 200-without-auth on the wrong target.
  `health.kind` typos now caught at manifest load, not deep in the
  pusher loop. `lib/secrets.py` unifies the prior 6 copy-pasted
  `_secrets_dir()` functions that disagreed on which env var to read.
- **Atomic writes for the stale-state DB.** `qflix-collect.ps1`'s
  `Update-StaleState` + `Stamp-ActedOn` both hit `stale-state.json` in
  the same PS run; the prior non-atomic `WriteAllText` had a crash
  window where the `acted_on_at` stamp could be lost (causing
  double-blocklist on the next hour).
- **Newsletter resilience.** A single arr failure during
  `fetch_all_calendars` no longer drops the entire Coming Soon section
  + everything downstream — degrades that section by 25% instead.
  Listmonk 4xx/5xx + TMDB 429 are now logged with the actual response,
  not just `HTTPError`.

### Stale-reference purge

The seedbox was cleaned 2026-05-11 (Readarr / Mylar3 / Jellyfin /
Jellyseerr / Jellystat / Conjurr / Newsletterr / Ombi all purged),
but the repo still carried install scripts + configure-time references
that would either fail re-install or inject dead artifacts. Resolved
by deleting 5 orphan install scripts + pruning references in 7 more.

### Observability + ergonomics

- **`Manitoba Pusher` self-heartbeat monitor.** Pusher pushes status=up
  to a dedicated Kuma monitor each cycle; a pusher crashloop now
  surfaces as a single dead-man instead of "every app went down at
  once". Bootstrap script provisions the monitor + token.
- **MCP staleness signal.** `qflix_status` returns
  `snapshot_age_minutes` + `stale_warning` so callers detect a
  suspended/offline collector without parsing `captured_at` by hand.
- **`qflix_list_torrents` gains `state` / `category` / `stale_only`
  filters.** Unfiltered call on a 200-torrent farm dumps ~50KB JSON
  into context; filters reduce that to the diagnostic-relevant subset.
- **Smoke + canary correctness.** `smoke-test-plex.sh` no longer
  permafail on the purged Newsletterr/Conjurr probes. New
  `smoke-test.sh:13m` check covers vlogs-ingest timer + last Result.
  `qbit-stall` canary no longer requires `UP_SPEED=0` (partial libtorrent
  wedges seed but don't download). `vlogs-stall` canary now fails on any
  single stale app, not only when all four *arr are stale.

### Doc reconciliation

- README badges + at-a-glance + both Mermaid diagrams updated to the
  real numbers (33 apps, 34 Kuma monitors, 6 canaries).
- `operator-deferred.md` purged of the dead Newsletterr UI section and
  the superseded Listmonk cutover entry. Phase 16 uninstall block
  updated to reflect the 7-day hold ending today (2026-05-16).
- `Tuesday.md` marked SUPERSEDED (the cross-class upgrade sweep was
  replaced by `app-upgrade-all.sh` 2026-05-13).
- `secrets-convention.md` rewritten — documents the actual active
  secret inventory + the purged set.

### Test count

414 → 440 (+26). New regression coverage for shell injection, recovery
permanent-failure mark, health probe fail-loud paths, manifest-load
validation, MCP filters + staleness, fetch_all_calendars per-arr
resilience, and the previously-untested `qflix_newsletter/sync.py`
Listmonk template uploader.

### Known follow-ups (not blocking this release)

- `test_kuma.py` has 6 `time.sleep(0.05–0.1)` waits that are flaky on
  loaded CI runners. Replace with `threading.Event`/`Queue` deterministic
  awaits when the test re-architecture lands.
- Hardlink-integrity canary + Plex transcoder canary not yet
  implemented — flagged in the audit as missing coverage.
- `plex.py` self-test mode (per audit) deferred.
- REA all-models-timeout dead-man alert (PowerShell, requires live
  Ollama integration to validate) deferred.
