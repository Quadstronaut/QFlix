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
- `seed-pick-fail` — *arr has zero movies/series to seed the probe
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
- `digest-stale` / `digest-missing` / `digest-malformed` / `digest-empty` — newsletter-digest-stale: the digest branch's `week_of` isn't fresh at Monday send time, the file/branch 404s or is absent, the JSON fails to parse (or `week_of` is missing/non-string), or `html` is blank. Only evaluated inside the Monday 14:15-24:00 UTC send window — outside it, and on a pure transport failure after 3 retries, the canary passes (`not-in-eval-window` / `digest-check-inconclusive`) rather than alerting.

## Exit codes

- 0 — pass; stdout has the `PASS: ...` line (or, for newsletter-digest-stale, `digest-fresh ...` / `not-in-eval-window ...` / `digest-check-inconclusive ...`) that Kuma stores as `msg=`
- non-zero — fail; stderr's `STAGE=... msg=...` line becomes Kuma `msg=`
