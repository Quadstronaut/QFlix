# Incidents

Operator-facing incident log for the QFlix stack. Newest first.

User-facing summaries are posted as **Uptime Kuma status-page incidents**
(status page slug `public`, "QFlix Status Page") so subscribers see plain-language
updates; this file keeps the full technical record. Keep the two in sync: when an
incident opens or resolves here, post/update the matching Kuma incident.

Severity scale: **P1** = user-visible outage or data-loss risk · **P2** = degraded
/ single non-critical service · **P3** = cosmetic or internal-only.

---

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

### What actually failed

Two runs, both real, both undelivered (`%APPDATA%\qflix-rea\audit.log`):

| Audit-log stamp (workstation, -07:00) | UTC | Findings | Outcome |
|---|---|---|---|
| 2026-08-18T19:09:55 | **2026-08-19T02:09:55Z** | 22 | `discord_post_failed` |
| 2026-08-19T18:06:39 | **2026-08-20T01:06:39Z** | 20 | `discord_post_failed` |

Both sit **outside** the box's own Discord brownout (see the 2026-08-18 entry below,
which ended 07:20:29Z). REA posts from the **workstation**, not the box, so this is a
second, independent egress failure on a second rail. 42 findings lost.

They are also the only two, ever — see the outcome table above — and they did not
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
