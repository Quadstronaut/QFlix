# Canaries

Automated probes that exercise the full request → *arr push chain for
each major content type. Each canary makes a request via the real
user-facing API (Seerr), then polls Seerr until the request's
`media.externalServiceId` is populated (= Seerr successfully reached
the *arr inside its container netns) and confirms that id matches the
*arr's record.

This forces traversal of the same Docker boundary that the
[reference_ucc-docker-host-loopback] bug class lives on. Earlier
host-side probes (`curl 127.0.0.1:17027/...` from the seedbox shell)
stayed green for ~9h on 2026-05-11 while every Seerr→Radarr request
was failing with `ECONNREFUSED 127.0.0.1:17027` inside Seerr's
container — that's the blind spot this rewrite closes.

The probe is non-destructive: the seed is the lowest-id movie/series
already in the *arr, so Seerr ack's via existing record. The Seerr
request itself is deleted in cleanup; the *arr movie/series is
untouched. On 409 (already-requested), the probe re-uses the existing
request id and skips the cleanup step.

## Files

- `movie.sh`              — Seerr → Radarr push test (creates+deletes a request, verifies externalServiceId)
- `anime.sh`              — Seerr → Sonarr2 push test (same pattern, tv mediaType, seasons:[1])
- `mobile-ux.sh`          — render-time check on the QFlix Dashboard public root (200 + `data-qflix-dash` marker, HTML <512KB); repointed off the retired Homarr board 2026-06-27
- `prowlarr-indexer-health.sh` — detects *arr→Prowlarr 429 cascades + chronically-unavailable indexers (detect-and-notify)
- `quota.sh`              — Ultra.cc per-user disk quota thresholds (80% warn / 90% critical+reclaim / 98% fail)
- `vlogs-stall.sh`        — VictoriaLogs reachable + non-zero ingest in last 15 min
- `sab-stall.sh`          — SABnzbd flowing: queue speed 0 while active jobs wait >10 min (dead provider/creds), OR any slot-level Paused job pinned >24 h (the 2026-07-19 wedged-queue-object class). Both invisible to the app monitor, which only sees SAB's web UI answering.
- `thread-ceiling.sh`     — the user's task count (processes **and** threads) against the slot's `ulimit -u` 2000 ceiling: 70% warn / 85% fail. RLIMIT_NPROC counts every task, so a Go app defaulting GOMAXPROCS to the host's 128 cores can exhaust it and crash-loop on `pthread_create` EAGAIN — invisible until something dies (the 2026-06-26 VictoriaLogs class).
- `tdarr-scanner.sh`      — Tdarr's four startup scanner self-tests. Reds only on a **regression** in the load-bearing pair (FFprobe / Exiftool — without them Tdarr cannot read a file at all); the known-dead Mediainfo (WebAssembly OOM vs the slot's `ulimit -v` 10GB, unfixable) and CCExtractor (missing `libtesseract.so.4`) stay green-with-WARN instead of parking a permanent red on an accepted condition.
- `tdarr-healthcheck.sh`  — Tdarr's health-check pipeline actually produces successes. Three predicates: **engine sanity** (each library's configured scan engine must have a binary that exists — reds on tick one), **error ratio** of completed checks (20% warn / 50% fail, after a 20-check minimum), and **progress/stall** (queue non-empty + node running + no new completed check in 8 h = wedged). Added after the pipeline ran 100% dead and silent for 68 days: libraries defaulted to `handbrakescan` and HandBrakeCLI does not exist on this slot, so every check spawn-failed ENOENT while transcodes kept succeeding and masked it.
- `ucc-gate-stuck.sh`     — the UCC maintenance-gate **detector**, not the apps it gates. `lib/ucc.py`'s probe-error branch holds the prior `active` flag, and `lib/suppression.py` reads that flag on every webhook down-event — so a probe that merely can't reach the host silently disables auto-recovery fleet-wide. Found stuck at `consecutive_error=128` (~10.6 h); the 2026-07-29 fix caps the hold at 3 cycles and fails **open**. This is the independent second leg: `consecutive_error > 12` (~1 h — the cap stops the suppression damage, this catches detection itself going dark) or `active` continuously true ≥ 6 h (defense in depth against a *different* bug freezing the gate on). Reads `ucc-window.json` directly rather than importing `lib.ucc`, so it still fires if that module breaks. A missing state file passes — timer liveness is a separate concern.
- `dash-asset-integrity.sh` — whether the SERVED dashboard shell can actually **hydrate**, which `mobile-ux.sh` cannot see. Two independent predicates: every `/_app/immutable/*` reference extracted from the served HTML must resolve **200** from the process that served it, and the document's advertised `Content-Length` must match the bytes actually delivered. Added after the public board served a dead shell for ~22 h on 2026-07-29 with every monitor green: `build/` was rewritten (01:33, 03:31) over a node process up since 2026-07-08, and adapter-node hands static files to **sirv, which snapshots its file manifest ONCE at process start** — so 6 of the 10 referenced modules 404'd although every file was on disk at mode 644, and files rewritten in place kept the `Content-Length`/`ETag` sirv computed at boot, so browsers aborted with `ERR_CONTENT_LENGTH_MISMATCH` before `domcontentloaded`. Nothing caught it: qflix-dash's app probe is `/healthz` (answered by the stale process), and both `mobile-ux.sh` and `smoke-test.sh` only counted the server-rendered `data-qflix-dash` marker, which survives a total hydration failure. A third predicate covers the case neither of those catches: a 200 whose body **never arrives** (headers, then a stall) satisfies neither predicate — `received` is never a number — and would otherwise be counted as a healthy probe. The `Content-Length` leg is **corroborated by an immediate re-probe** before it is believed, because a stale sirv stat tuple lies identically forever while one cycling nginx worker produces a single truncated response, and restarting a healthy dashboard over wire noise would spend the whole 24 h self-heal budget on nothing. **Self-heal is narrow:** a 404 whose file *exists* under `~/.apps/qflix-dash/build/client/` is a stale in-process sirv manifest and gets one `systemctl --user restart qflix-dash` — breaker 1 per 24 h, held in a durable epoch latch that is **reserved before the restart is issued** (written atomically, read back) and released again on every path that turns out not to have issued it; if the latch will not persist the restart is *refused*, because a breaker that is not durable is not a breaker. Also suppressed inside the Monday 11:00-15:00 UTC window, refused if the unit itself entered active less than two timer ticks ago, re-verified by re-fetch afterwards, every attempt logged durably. A 404 whose file is **absent** is a partial deploy, and a stalled body is a wedged worker: both alert and do *not* restart, because a restart cannot fix either.
- `qbit-stall.sh`         — qBittorrent has had a state change in the last N hours
- `kometa-libraries.sh`   — every kometa-configured Plex library still exists in Plex (semantic config-drift guard)
- `kometa-deploy-drift.sh` — deployed kometa config.yml `libraries:` set matches what scripts/configure/55-kometa-install.sh would render (textual drift guard)
- `stale-log-watchdog.sh` — timer-driven app logs (kometa daily, recyclarr weekly, buildarr daily) are still being written on schedule
- `hardlink-integrity.sh` — cross-references qBit completed torrents against the media library by inode: hardlinked = a library file shares the torrent's inode; a **copy-mode regression** = no inode twin but a byte-identical library file exists at a *different* inode (storage genuinely doubled). Benign orphan seeds (superseded/different-release grabs qBit holds for ratio — no same-size library file) are excluded. Fails only when copy-mode imports exceed both thresholds **and** the orphan-excluded sample reaches `QFLIX_CANARY_HARDLINK_MIN_SAMPLE` (default 5); below that it passes as inconclusive. Protects against silent storage-doubling if *arr flips from hardlink to copy mode.
- `plex-transcoder.sh`   — Plex `/transcode/sessions` + `/:/prefs` respond <10s with 2xx (catches transcoder daemon stall while main `/identity` still says 200)
- `tautulli-plex-link.sh` — Tautulli's CONFIGURED `pms_ip:port` is a live Plex `/identity` (catches "Tautulli web up but pinned to a dead/old Plex address" — the 2026-05-20 re-IP class the app monitor stayed green through)
- `newsletter-digest-stale.sh` — the weekly "Behind the scenes" digest (`digest/latest.json` on the `newsletter-digest` branch) is fresh per the newsletter's own `_is_fresh()` rule, checked ONLY inside the Monday 14:15-24:00 UTC send window (fires 3x: 14:20/14:50/15:20 UTC). Detects the silent override→fallback degradation the newsletter itself never surfaces. Test overrides: `QFLIX_DIGEST_CANARY_NOW` / `_URL` / `_FORCE_WINDOW` (see the script header for the full env var table).

## Stage labels (failure messages on stderr → Kuma `msg=`)

- `seerr-up-fail` / `radarr-up-fail` / `sonarr2-up-fail` — the named API didn't return 200
- `seed-pick-fail` — a seed exists but its tmdb id couldn't be resolved. NOTE: an **empty but reachable** library (0 movies/series — e.g. the reaper aged out the last title) is treated as an **inconclusive SKIP → PASS** (`PASS: … SKIP: … up but 0 …`), not a failure — it's a content state, not a Seerr→*arr path fault. A genuine *arr outage still trips `*-up-fail` earlier.
- `seerr-push-fail` — POST /api/v1/request returned non-2xx/409 (or 409 with no recoverable id)
- `arr-not-populated` — externalServiceId stayed null after 30s of polling
- `verify-fail` — externalServiceId did not match the *arr's id for the seed
- `cleanup-fail` — DELETE request returned non-2xx (warned to stderr, probe still passes)
- `kometa-config-missing` / `kometa-config-parse-fail` — kometa-libraries / kometa-deploy-drift: can't read/parse config.yml
- `plex-up-fail` / `plex-libraries-fetch-fail` — kometa-libraries: Plex API unreachable or no library list
- `library-drift` — kometa-libraries: at least one kometa-configured library doesn't exist in Plex
- `install-script-parse-fail` — kometa-deploy-drift: can't read the install script's library list
- `deploy-drift` — kometa-deploy-drift: deployed config's library set differs from what scripts/configure/55-kometa-install.sh would render
- `log-stale` / `log-missing-<app>` — stale-log-watchdog: a timer-driven app's log file mtime exceeds its expected cadence, or the log is missing entirely
- `library-empty` / `qbit-no-completed` / `hardlink-regression` — hardlink-integrity: no scanable videos under the media roots; qBit reports completed torrents but none resolve to on-disk files (data dir nuked / mount gone); or copy-mode imports (`detached`) exceed `MAX_DETACHED`n **and** `MAX_DETACHED_PCT`% over the orphan-excluded sample
- `plex-up-fail` / `transcode-api-fail` / `prefs-api-fail` — plex-transcoder: Plex /identity non-200, or transcode/prefs endpoint hung or non-200
- `dash-*` — dash-asset-integrity: the served shell references an asset the running server will not serve, or the document's advertised `Content-Length` disagrees with the bytes delivered. The two faults carry **distinct** labels because they need different responses: a 404 whose file is present on disk is a stale in-process sirv manifest (restartable), a 404 whose file is absent is a partial deploy (not restartable), a 200 whose body never arrives is a stalled worker (not restartable), and the self-heal has its own labels for suppressed-by-the-Monday-window, refused-by-the-24h-breaker, refused-because-the-breaker-latch-will-not-persist, refused-because-the-unit-just-started, and issued-but-re-verification-still-failing. The script header carries the authoritative label table and the `QFLIX_CANARY_DASH_*` env overrides — read it there rather than duplicating the strings here, since this canary is the one that *acts*.
- `digest-stale` / `digest-missing` / `digest-malformed` / `digest-empty` — newsletter-digest-stale: the digest branch's `week_of` isn't fresh at Monday send time, the file/branch 404s or is absent, the JSON fails to parse (or `week_of` is missing/non-string), or `html` is blank. Only evaluated inside the Monday 14:15-24:00 UTC send window — outside it, and on a pure transport failure after 3 retries, the canary passes (`not-in-eval-window` / `digest-check-inconclusive`) rather than alerting.

## Exit codes

- 0 — pass; stdout has the `PASS: ...` line (or, for newsletter-digest-stale, `digest-fresh ...` / `not-in-eval-window ...` / `digest-check-inconclusive ...`) that Kuma stores as `msg=`
- non-zero — fail; stderr's `STAGE=... msg=...` line becomes Kuma `msg=`
