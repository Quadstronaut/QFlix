# WARN.md — log-audit warnings 2026-05-16

**Generated:** 2026-05-16 from 21-app log scrub (.scratch/log-audit-2026-05-16/).
**Scope:** every active log surface under `~/` on manitoba; 24h window for daily-cadence apps, 14d window for weekly oneshots.
**Health backstop:** `qflix_status` reports **0 Kuma red** at audit time; no zombies; latest snapshot 36 min stale (within tolerance).

CRITICAL findings (CRIT/FATAL/FAIL/ERROR) are reported separately in chat for operator action. This file is the WARN punch-list for later evaluation.

---

## sonarr — main TV *arr

- **[50×] HTTP 429 TooManyRequests — Prowlarr indexer rate-limit** (recurring through entire 48h window; first seen 2026-05-16T20:34:18Z).
  - `GET http://172.17.0.1:17024/prowlarr/28/api?t=tvsearch... → 429.TooManyRequests`
  - Cosmetic — Prowlarr correctly rate-limits and disables indexer until cooldown; sonarr handles gracefully. Root cause cascades from **prowlarr** CRITICAL (FlareSolverr 500s → indexer disable → 429 fan-out).
- **[1×] kickasstorrents.ws Prowlarr timeout** — 2026-05-16T21:39:19Z. Transient.

## sonarr2 — anime TV *arr

- **[14×] System.Net.Sockets stack-trace fragments** during indexer API calls — expected under throttle while indexers are disabled by Prowlarr.
- **[70×] HTML CSS link fragments from upstream Ultra.cc nginx error pages** captured as response bodies of 429 indexer replies. Cosmetic log noise — the "nginx configuration has been modified incorrectly" text the agent flagged is the OUTER Ultra.cc error page, not our user-nginx. Indexer body, not infra fault.

## radarr — main movies *arr

- **HTTP 429 TooManyRequests from Prowlarr** — `kickasstorrents.ws`, `Torrent[CORE]` cycles (multiple). Same FlareSolverr root cause as prowlarr CRITICAL.
- **Indexer coverage gaps in RSS sync** — `The Pirate Bay`, `Uindex`, `Torrent[CORE]` — 30 min to ~4 h gap windows. Self-healing.
- **[2×] "Query successful, but no results in the configured categories"** — `2026-05-16T14:23:23–25` and `T20:23:55–57`. Indexer category mismatch, no grabs lost.
- **[2×] Prowlarr HTTP request timeout (kickasstorrents.ws)** — transient.

## radarr2 — anime movies *arr

- **[8+×] nekoBT (Prowlarr) RSS sync coverage gaps** — chronic since 2026-05-12. Latest: 05/16 05:58:31 → 05/16 17:43:06 UTC.
- **[4×] "Invalid request / Validation failed / no results in configured categories"** — 2026-05-16 08:22–14:23. Category-config drift.
- **[1×] "Unable to retrieve queue and history items from qBittorrent"** — 2026-05-16 02:45. Transient.
- **[1×] API rate limit (Nyaa.si via Prowlarr)** — disabled 00:01:07. Acceptable.
- **[multiple] HTTP 502 BadGateway on `http://172.17.0.1:17024/qbittorrent/api/v2/...`** — 2026-05-16 19:50–20:23. Surfaced in CRITICAL — possible misconfig (port 17024 is Prowlarr, not qBit; URL path looks like cross-routing artifact).

## prowlarr — indexer aggregator

- **No-results-in-configured-categories notices** — `Prowlarr will not sync X Indexer to App` — 1+. Normal operational notice.
- **`Torrent[CORE]`, `kickasstorrents.ws` site-unavailable timeouts** — site connectivity, not actionable.
- **`kickass.ws`, `torrentcore.xyz` 403 HTML responses** — Cloudflare challenge; FlareSolverr handles. (See CRITICAL: FlareSolverr 500s are the upstream issue.)

## bazarr — main subtitle service

- **[4×] "Item not found in library — IMDB tt1344204 (Blue Mountain State)"** — fallback to full library rescan, 04:19:41–42.
- **[4×] "Failed Plex library update"** — related to the `Pirate TV Shows` library section CRITICAL.
- **Provider 404/403/connection-error throttles** (auto-recovery within 10 min):
  - podnapisi `ConnectionError` (3×)
  - tvsubtitles `HTTPError 404` (3×)
  - greeksubtitles `HTTPError 403` (3×)
  - subs4series `HTTPError 404` (1×)
- **Media QA observation:** Avatar WEBRip stored as `.iso` at 09:34:52 — not a valid Bazarr video extension. Belongs in library validation, not Bazarr.

## bazarr2 — anime subtitle service

- No warns. Silent operation in window — hourly `bazarr2-sync.service` PID rotations only (expected version-pinning).

## qbittorrent — download client (v5.0.3)

- **[1×] WebAPI login failure (invalid credentials, empty username, ::ffff:127.0.0.1)** — 2026-05-16 09:28:10. Auto-recovered within ~1 min (successful login 09:29:21). Likely scripted probe with stale credentials. Monitor for pattern.
- **[6×] "max outstanding piece requests" on single torrent ([www.Speed.Cd] S03E04)** — 09:02:10–20. Benign network congestion / peer oversubscription.

## nginx — user-level reverse proxy

- No warns in 48h window. Silent — clean operation. (Documented cosmetic permission alert on `/var/log/nginx/error.log` per `inventory.md` continues to be benign.)

## seerr — request portal (v3.2.0)

- **[5,006+×] "Media already exists, marking request as COMPLETED" + "Media became available before request was approved"** — benign canary-driven request-state transitions. Expected from the new movie/anime canaries that POST real Seerr requests and let them auto-complete. Per `inventory.md` 2026-05-11 evening canary rewrite. **Not actionable.** Volume is high but harmless; consider muting at log level if it crowds VictoriaLogs storage.

## tautulli — Plex stats

- **[219×] "Failed to get library metadata images"** — fallback to defaults; UI degraded but functional.
- **[28×] "Failed to get user avatars"** — fallback to generic avatars.
- **[48×] "Unable to parse XML / retrieve metadata details"** — caused by upstream Plex API timeouts during the 2026-05-11 pms_url spike; recovered.
- (Last 24h: **zero** warns/errors. Issue closed post-2026-05-11 fix.)

## buildarr — declarative *arr converger

- No warns. 2026-05-11/12/16 weekly oneshots all clean (`Result=success`).

## kometa — Plex metadata + collections

- **[11×] "Asset folder not found / created (pre-collection setup)"**.
- **[38+×] "No poster/background in asset directory"** — cosmetic missing-custom-artwork notices.
- Run 2026-05-16 04:41 UTC processed 38 movies, zero auth/config/write failures.

## recyclarr — TRaSH-guide profile sync

- No WARN-level entries. (CRITICAL: 12h of include-template fatal errors on the `movies` instance — see report.)

## maintainerr — library deletion rules

- No warns. INFO-only activity in 48h.

## listmonk — newsletter / mailing manager

- Silent. No warns. Health green per `qflix_status`.

## tdarr-server — transcoding orchestrator

- Silent. No warns. (Tdarr only logs on transcode events; no jobs in window.)

## tdarr-node — transcoding worker

- Silent. No warns. XDG_RUNTIME_DIR fix from 2026-05-11 holding — no cron-mail regression.

## maint-pusher — Kuma push-loop

- Silent. No warns. (Pusher only logs on /api/push failures or recovery events; ongoing green Kuma means zero log output, by design.)

## maint-webhook — Kuma webhook receiver

- No warns. 160+ consecutive 200 OK health checks in 48h.

## maint-window — weekly maintenance window

- Silent in 14d window. (Last full smoke 2026-05-16: **49 pass / 0 fail / 2 skip** per `inventory.md`. Timer is firing — the maint scripts just don't emit journal lines on success.)

---

## Cross-cutting observation

The top WARN signal across the *arr stack — Prowlarr 429 indexer-disabled cascade — has a single upstream root cause: **FlareSolverr 500 InternalServerError on kickasstorrents.ws + Torrent[CORE]** (60s Cloudflare CAPTCHA timeouts). See the prowlarr CRITICAL report for details. Fixing that one issue will quiet ~50+ sonarr WARNs, ~6 radarr WARNs, ~9 radarr2 WARNs, and the 140-line sonarr2 indexer-disable noise.
