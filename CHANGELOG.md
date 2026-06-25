# Changelog

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
- **#2 Sonarr↔SABnzbd "Connection refused" → config FIXED; benign residual.** SAB
  was never down (process up 2d+, 200 on loopback + bridge). The SAB download-client
  config is corrected + verified: deleted the old client and recreated it fresh as
  **id 6 at `172.17.0.1`** with the real API key — live test 200, downloads work.
  The residual ~3/5min `127.0.0.1` log-error is Sonarr's `DownloadedEpisodesScan`
  (Completed Download Handling) re-testing the client from a **tracked failed-SAB-
  grab record** that stores the grab-time address. That state lives in Sonarr's
  **container-private `/config/sonarr.db`** and survived config-correction, client
  delete+recreate, AND three restarts. No host/API/UI lever reaches it (this UCC
  box exposes no `docker exec`; the Sonarr UI has no clear-queue control) — it's
  cosmetic log-noise that will age out.
- **Plex library-update connection fixed on all four *arr.** They were pinned to the
  dead **`172.17.1.250:32400`** — the pre-re-IP Plex address that only Tautulli had
  been migrated off after the 2026-05-20 Ultra.cc kernel migration (per
  `50-tautulli-pms-url-fix.sh`). Repointed live to the stable bridge gateway
  **`172.17.0.1:<plex.port>`** (all four test 200), restoring native Plex refresh-
  on-import (silently broken for ~5 weeks). Source fixed: `09-phase5-arr-connects-
  and-sync.sh` `PLEX_HOST` → gateway, not `plex.host` (=127.0.0.1). Same container-
  private-DB residual as SAB drains the stale 172.17.1.250 queued updates.
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
