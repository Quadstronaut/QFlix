# Changelog

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
