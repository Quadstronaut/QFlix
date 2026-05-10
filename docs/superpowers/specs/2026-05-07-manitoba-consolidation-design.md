# Manitoba Consolidation — Design Spec

**Date:** 2026-05-07
**Owner:** Quadstronaut (operator@example.com)
**Target:** `quadstronaut@seedbox.example.com` (Ultra.cc seedbox)
**Status:** Draft — pending operator review (Section 11)

---

## 1. Goals & non-goals

**Primary goals**

- Consolidate manitoba into a robust, production-grade media platform (TV, movies, anime, books, comics) that users actively watch on, with no long-running downtime during the rectification.
- Fix every currently-broken integration: Prowlarr (empty), *arrs (no indexers), FlareSolverr (missing), Unpackerr (broken config), Maintainerr → Jellyseerr deletion webhook (the original "re-request" bug-fix from the PlexEcosystem spec), Bazarr ↔ *arrs, Tautulli/Jellystat ↔ media servers, Notifiarr ↔ everything.
- Add the missing pieces: Jellyfin alongside Plex, Sonarr2/Radarr2 for anime, Readarr + Mylar3 + Komga + Kavita + Calibre-Web + Audiobookshelf for books/comics/manga/audiobooks.
- Land a friendly two-board Homarr dashboard at `https://quadstronaut.seedbox.example.com/` — a public board for friends/family, an admin board for the operator.
- Plan-first execution with one batched approval; reversible/idempotent where possible.

**Explicit non-goals**

- No usenet (no nzbget/nzbhydra2/sabnzbd installation or configuration).
- No music server (no navidrome / airsonic / Lidarr).
- No personal file storage (no Nextcloud / Pydio / Resilio).
- No second torrent client (qBittorrent only; Deluge/pyload/transmission stop).
- No replacement of Ultra.cc-managed infrastructure: ports, public TLS, primary nginx, system OS, app installation mechanism (`app-*` commands) are all host-managed and out of bounds.
- No data migration (no folder restructure to TRaSH layout — current layout is kept and extended). 1.8 TB of in-flight torrents stays untouched.
- No same-time decommission of the old/duplicate apps (Ombi, Jackett, Medusa). They get stopped during rectification; uninstall is operator-initiated post-verification.

**Constraints**

- No root, no sudo, no Docker exec on Ultra.cc. Apps managed via `app-<name> {install,start,stop,restart,uninstall}`. App configs live under `~/.apps/<app>/` for natively-installed apps; `/app/<app>/` for Ultra.cc-managed containerized apps.
- Single `quadstronaut` Linux user; all services run under that uid.
- 3 TB user disk quota. Current usage ~1.8 TB seeding + 623 GB media (much of media is hardlinked into seeding) → ~1.2 TB headroom. Disk budget tight; design must respect it.
- Public access to all apps is via Ultra.cc's nginx + HTTPS at `https://quadstronaut.seedbox.example.com/<app>/` with htpasswd basic auth gating from `~/www/.htpasswd`. No custom domain, no Caddy, no DNS-01 plumbing.
- Hardlinks work natively on the seedbox filesystem (verified — same inode, link count = 2 between `~/downloads/qbittorrent/<cat>/` and `~/media/<lib>/` paths).
- Spec PlexEcosystem's Fallback A (copy mode) is no longer needed. *arrs are configured to use hardlinks.

---

## 2. Architecture overview

### 2.1 The one logical filesystem

Everything runs on one Debian 11 host. All data lives under `$HOME` = `/home/quadstronaut` (symlink to `/home28/quadstronaut`). Hardlinks between `~/downloads/qbittorrent/<category>/` and `~/media/<library>/` are confirmed working — see Section 3.

### 2.2 Three rings of services

```
┌─────────────────────────────────────────────────────────────┐
│  Public ring (user-facing)                                   │
│  Plex · Jellyfin · Jellyseerr · Komga · Kavita · Calibre-Web│
│  Audiobookshelf · Homarr public board                        │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────┴──────────────────────────────────┐
│  Automation ring (admin-facing, htpasswd-gated)              │
│  Sonarr · Sonarr2 (anime) · Radarr · Radarr2 (anime)         │
│  Readarr · Mylar3 · Prowlarr · FlareSolverr · Bazarr         │
│  Maintainerr · Tautulli · Jellystat · Uptime Kuma            │
│  autobrr · Unpackerr · Notifiarr (hosted) · Homarr admin    │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────┴──────────────────────────────────┐
│  Infrastructure ring                                         │
│  qBittorrent (port 17041, single download client)            │
│  Ultra.cc nginx (host-managed reverse proxy + HTTPS)         │
│  User nginx (per-user, ~/.apps/nginx — proxies app paths)    │
│  filebrowser (admin file mgmt) · Syncthing (idle)           │
└─────────────────────────────────────────────────────────────┘
```

### 2.3 Two-board landing page

URL: `https://quadstronaut.seedbox.example.com/`

User nginx is reconfigured to proxy `/` to Homarr (currently bound at its assigned subpath). Homarr serves two boards with different visibility:

- **Public board (htpasswd-only):** family-friendly, see Section 9.
- **Admin board (htpasswd + Homarr admin login):** operator dashboard, see Section 9.

### 2.4 Transitional discipline

Any app being replaced is stopped (not uninstalled) during rectification. The operator uninstalls these on a post-verification schedule (Section 8).

| Replaced | Replacement | Disposition |
|---|---|---|
| Ombi | Jellyseerr | Stop, leave installed |
| Jackett | Prowlarr + FlareSolverr | Stop after Prowlarr indexers verified |
| Medusa | Sonarr (+ Sonarr2 for anime) | Stop, leave installed |
| Doplarr | Notifiarr (hosted) | Already uninstalled by operator |
| MariaDB | (no replacement — was Nextcloud-only) | Stop, then uninstall after audit |
| Deluge / pyload / transmission | qBittorrent | Stop |

---

## 3. Folder layout & qBit categories

### 3.1 Filesystem (post-rectification)

```
~/downloads/qbittorrent/
├── radarr/         ← existing, 1.8 TB seeding, untouched
├── tv-sonarr/      ← existing
├── radarr-anime/   ← NEW (empty)
├── sonarr-anime/   ← NEW (empty)
├── readarr/        ← NEW (empty)
└── mylar/          ← NEW (empty)

~/media/
├── Movies/         ← existing 247 GB; Radarr root
├── TV Shows/       ← existing 377 GB; Sonarr root
├── Anime Movies/   ← NEW; Radarr2 root
├── Anime/          ← NEW; Sonarr2 root
├── Books/          ← NEW; Readarr root, Calibre-Web library
├── Audiobooks/     ← existing empty; Audiobookshelf, Readarr (audio)
├── Comics/         ← NEW; Mylar3 root, Komga, Kavita
├── Manga/          ← existing empty; Mylar3 root, Komga, Kavita
├── Podcasts/       ← existing empty; Audiobookshelf
└── Calibre Library, Playlists, Music, Books — existing empty,
   left in place; Music will not be populated (out of scope).
```

### 3.2 qBit categories (final)

| Category | Save path | Used by |
|---|---|---|
| `radarr` | `~/downloads/qbittorrent/radarr` | Radarr |
| `tv-sonarr` | `~/downloads/qbittorrent/tv-sonarr` | Sonarr |
| `radarr-anime` | `~/downloads/qbittorrent/radarr-anime` | Radarr2 |
| `sonarr-anime` | `~/downloads/qbittorrent/sonarr-anime` | Sonarr2 |
| `readarr` | `~/downloads/qbittorrent/readarr` | Readarr |
| `mylar` | `~/downloads/qbittorrent/mylar` | Mylar3 |

The first two categories already exist; the other four are added on freshly-created empty directories. No torrents are touched, no recheck is triggered.

---

## 4. App disposition

Definitive inventory. Status as of 2026-05-07.

### 4.1 Currently running — keep + reconfigure

| App | Action | Why |
|---|---|---|
| qBittorrent 5.0.3 (port 17041) | Add anime + readarr + mylar categories | Single source of torrent truth |
| Plex Media Server 1.43 | Configure scan webhooks from all *arrs; verify libraries |Currently authoritative for users |
| Sonarr | Reconfigure: Prowlarr indexers (sync), qBit client (cat=`tv-sonarr`), Plex/Jellyfin/Bazarr/Notifiarr Connects, root `~/media/TV Shows` | Currently broken — no indexers |
| Radarr | Same as Sonarr; cat=`radarr`, root `~/media/Movies` | Currently broken — no indexers |
| Prowlarr | Repopulate with 14 Jackett indexers + 6 free natives + FlareSolverr proxy + sync to all 6 *arrs | Currently empty |
| Bazarr | Connect to all 4 *arrs (general + anime); language profiles English |Currently disconnected |
| Tautulli | Fresh Plex token, Notifiarr webhook, Maintainerr feed | Plex stats data feeds Maintainerr rules |
| Maintainerr | Fresh Plex token, Jellyseerr deletion webhook, rules per Section 7 | Webhook is THE bug-fix from PlexEcosystem spec §5.2 |
| Jellyseerr | Plex SSO, Jellyfin connection, Sonarr/Sonarr2/Radarr/Radarr2 routing rules | Already running but unconnected |
| autobrr | Verify filters target qBit not rtorrent; add anime-aware filters | Bonus utility — keep |
| qbt_pub | Leave alone | Ultra.cc-managed qBit API endpoint |
| filebrowser | Leave alone | Useful admin tool |
| homarr-upstream | Reconfigure as the public-facing landing page (two-board) | Section 9 |
| user nginx (~/.apps/nginx) | Add `/` → homarr proxy | Routes the landing page |
| Syncthing | Leave running, idle | Not in spec but harmless |

### 4.2 Currently running — stop, leave installed (operator-uninstalls later)

| App | Replaced by | Action |
|---|---|---|
| Ombi | Jellyseerr | `app-ombi stop` |
| Jackett | Prowlarr + FlareSolverr | `app-jackett stop` after Prowlarr verified |
| Medusa | Sonarr / Sonarr2 | `app-medusa stop` |
| pyload | (nothing) | `app-pyload stop` |
| Deluge daemon + web | qBittorrent | `app-deluge stop` |
| MariaDB | (nothing — was Nextcloud's; nextcloud not running) | `app-mariadb stop` |

### 4.3 Currently NOT running — install fresh

| App | Why | Notes |
|---|---|---|
| Jellyfin (`app-jellyfin install`) | Parallel media server per spec | Same `~/media/*` libraries |
| Sonarr2 (`app-sonarr2 install`) | Anime TV automation | Root `~/media/Anime`, qBit cat `sonarr-anime` |
| Radarr2 (`app-radarr2 install`) | Anime movie automation | Root `~/media/Anime Movies`, qBit cat `radarr-anime` |
| FlareSolverr (`app-flaresolverr install`) | Cloudflare bypass for indexers | Wire into Prowlarr Indexer Proxies |
| Unpackerr (config fix + `app-unpackerr start`) | Auto-extract password-protected RARs | Fix TOML, point at qBit dirs (Section 6.6) |
| Notifiarr (hosted, no install) | Notification aggregator | API key in `secrets/notifiarr.key` |
| Jellystat (`app-jellystat install`) | Jellyfin equivalent of Tautulli | |
| Uptime Kuma (`app-uptimekuma install`) | Service health monitoring | |
| Readarr (`app-readarr install`) | Book/audiobook automation | Roots: `~/media/Books`, `~/media/Audiobooks` |
| Mylar3 (`app-mylar3 install`) | Comic automation | Roots: `~/media/Comics`, `~/media/Manga` |
| Komga (`app-komga install`) | Comics/manga server (Tachiyomi-canonical) | Both `~/media/Comics` and `~/media/Manga` |
| Kavita (`app-kavita install`) | Comics/manga server (alt UI) | Same paths as Komga; runs in parallel |
| Calibre-Web (`app-calibre-web install`) | Ebook web reader | `~/media/Books` |
| Audiobookshelf (`app-audiobookshelf install`) | Audiobook + podcast server | `~/media/Audiobooks`, `~/media/Podcasts` |

### 4.4 Doplarr

Already uninstalled by the operator. No action.

### 4.5 Available `app-*` but skipped (off-scope)

`airsonic`, `airsonic-advanced`, `couchpotato`, `emby`, `filebot`, `lidarr`, `lazylibrarian`, `mylar` (use mylar3), `navidrome`, `nextcloud`, `nzbget`, `nzbhydra2`, `sabnzbd`, `overseerr` (Jellyseerr supersets), `plexrequests`, `pydio`, `pydiocells`, `qui`, `rapidleech`, `pyloadng`, `requestrr`, `resilio`, `rtorrent`, `rutorrent`, `seerr`, `sickbeard`, `sickchill`, `sickrage`, `thelounge`, `transmission`, `ubooquity`, `znc`.

---

## 5. Integration wiring

**Placeholder convention:** `<sonarr-port>`, `<jellyfin-port>`, etc. are filled at implementation time. Ultra.cc assigns ports per-app on install; discover via `app-ports show` after each app is installed. Where a path-prefixed URL is used (e.g., `http://127.0.0.1:<port>/sonarr`), the prefix matches the Ultra.cc `BasePathOverride` for that app.

### 5.1 Indexer flow (Prowlarr-centric)

```
Prowlarr (with FlareSolverr indexer-proxy attached)
   │ pushes via "Apps" tab to:
   ├──► Sonarr   (general TV — excludes anime-only indexers)
   ├──► Sonarr2  (anime — only anime-tagged indexers)
   ├──► Radarr   (general movies — excludes anime-only)
   ├──► Radarr2  (anime movies — only anime-tagged)
   ├──► Readarr  (books)
   └──► Mylar3   (comics)
```

### 5.2 Indexers to configure in Prowlarr (full list)

Migrated from Jackett (all free public, all native Prowlarr built-ins):

**General:** `1337x` (NEW), `BitSearch` (NEW), `EZTV`, `Glodls` (NEW), `Internet Archive`, `IsoHunt2`, `KickassTorrents-WS`, `LimeTorrents`, `Solid Torrents` (NEW), `ShowRSS`, `The Pirate Bay`, `TheRARBG`, `TorrentDownload`, `TorrentDownloads`, `TorrentGalaxy` (NEW), `YTS`

**Anime:** `Nyaa.si`, `AniDex` (NEW), `Tokyo Toshokan`, `ShanaProject`, `subsplease`

Disabled in Jackett, skipped: `ehentai`.

Tagging in Prowlarr's "Indexer" tab:
- Anime indexers tagged `anime` → synced only to Sonarr2/Radarr2
- All other indexers untagged or tagged `general` → synced only to Sonarr/Radarr/Readarr/Mylar3

### 5.3 FlareSolverr wiring

After `app-flaresolverr install`:
- Discover the bound port via `app-ports show` (host-assigned).
- Prowlarr Settings → Indexers → Indexer Proxies → Add → FlareSolverr → URL `http://127.0.0.1:<port>`.
- Tag with `cloudflare`. Indexers known to need it (TorrentGalaxy, sometimes 1337x) get the same tag.

### 5.4 Download client flow

All 6 *arrs configure qBittorrent at:
- Host: `127.0.0.1`
- Port: `17041`
- Username: `quadstronaut`
- Password: existing
- Use SSL: no (loopback)
- Category: per Section 3.2
- "Use hashed name" / sequential / first-and-last: defaults
- Recent priority / older priority: defaults

### 5.5 Import / library scan webhooks (per *arr)

Webhook fan-out depends on whether the *arr feeds video libraries (which Plex/Jellyfin index) or text/comic libraries (which Komga/Kavita/Calibre-Web/Audiobookshelf index). Each *arr's Settings → Connect entries:

**Video *arrs (Sonarr, Sonarr2, Radarr, Radarr2):**

| Connection | Triggers | Settings |
|---|---|---|
| Plex | On Import / On Upgrade / On Rename | Server `127.0.0.1:32400`, library mappings to `~/media/{Movies,TV Shows,Anime,Anime Movies}` |
| Jellyfin | On Import / On Upgrade / On Rename | Server `127.0.0.1:<jellyfin port>`, API key, library refresh |
| Bazarr | On Import / On Upgrade | API URL, API key |
| Notifiarr | On Grab / On Import / On Upgrade / On Health Issue | Notifiarr passthru webhook URL + API key |

**Readarr (books / audiobooks):**

| Connection | Triggers | Settings |
|---|---|---|
| Calibre-Web | On Import | API URL, API key — triggers library rescan |
| Audiobookshelf | On Import | API URL, API key — triggers library rescan (audiobooks only) |
| Notifiarr | On Grab / On Import / On Health Issue | Notifiarr passthru |

**Mylar3 (comics / manga):**

| Connection | Triggers | Settings |
|---|---|---|
| Komga | On Import | API URL, API key — triggers library rescan |
| Kavita | On Import | API URL, API key — triggers library rescan |
| Notifiarr | On Grab / On Import / On Health Issue | Notifiarr passthru |

Bazarr is video-only and does not connect to Readarr or Mylar3 (subtitles aren't a thing for books/comics). Plex/Jellyfin do not connect to Readarr or Mylar3.

### 5.6 Bazarr ↔ *arrs

Bazarr Settings → Sonarr / Radarr (one entry per *arr — both general and anime instances):
- URL: `http://127.0.0.1:<sonarr-port>/sonarr` (and `/sonarr2` for anime)
- API key from each *arr config.xml
- Language profile: English forced + English regular (default starting point)

### 5.7 Maintainerr ↔ Plex/Jellyseerr/*arrs (the bug-fix)

Maintainerr Settings:
- Plex: URL `http://127.0.0.1:32400`, token from `secrets/plex.token`
- Jellyseerr (Overseerr-compatible): URL `http://127.0.0.1:<jellyseerr-port>`, API key from `secrets/jellyseerr.key`
- *arrs: Sonarr/Sonarr2/Radarr/Radarr2 endpoint + API key — used for delete-API
- Notifiarr: webhook URL + API key

Deletion flow:
```
Maintainerr ── eligible-for-deletion warning (day 46 / 351) ──► Notifiarr ──► Discord #downloads
Maintainerr ── deletion fires (day 60 / 365) ──► *arr.delete() ──► Plex/Jellyfin scan
                                              └─► Jellyseerr deletion webhook (built-in)
                                                  └─► request marked "available for re-request"
```

### 5.8 Tautulli ↔ Plex / Jellystat ↔ Jellyfin

- Tautulli: URL `http://127.0.0.1:32400`, token from `secrets/plex.token`. Notifiarr agent enabled. History sync.
- Jellystat: URL `http://127.0.0.1:<jellyfin port>`, API key from `secrets/jellyfin.key`.

### 5.9 autobrr verification

- Filters: each filter's "Action" must target `qBittorrent` client (not rtorrent which is gone).
- Add anime-tagged filters that drop captures into `sonarr-anime` / `radarr-anime` qBit categories.

### 5.10 Unpackerr config fix

Replace `~/.apps/unpackerr/unpackerr.conf` with valid TOML:

```toml
[[sonarr]]
url = "http://127.0.0.1:<sonarr-port>/sonarr"
api_key = "<from secrets/sonarr.key>"
paths = ["/home/quadstronaut/downloads/qbittorrent/tv-sonarr"]
protocols = "torrent"

[[sonarr]]
url = "http://127.0.0.1:<sonarr2-port>/sonarr2"
api_key = "<from secrets/sonarr2.key>"
paths = ["/home/quadstronaut/downloads/qbittorrent/sonarr-anime"]
protocols = "torrent"

[[radarr]]
url = "http://127.0.0.1:<radarr-port>/radarr"
api_key = "<from secrets/radarr.key>"
paths = ["/home/quadstronaut/downloads/qbittorrent/radarr"]
protocols = "torrent"

[[radarr]]
url = "http://127.0.0.1:<radarr2-port>/radarr2"
api_key = "<from secrets/radarr2.key>"
paths = ["/home/quadstronaut/downloads/qbittorrent/radarr-anime"]
protocols = "torrent"

[[readarr]]
url = "http://127.0.0.1:<readarr-port>/readarr"
api_key = "<from secrets/readarr.key>"
paths = ["/home/quadstronaut/downloads/qbittorrent/readarr"]
protocols = "torrent"
```

Lidarr block: empty / removed (not in scope).
Folder watch block: optional, skip in v1.

### 5.11 Jellyseerr routing rules

- Media type = anime → routes to Sonarr2 (root `~/media/Anime`) / Radarr2 (root `~/media/Anime Movies`)
- Media type = TV (non-anime) → Sonarr (root `~/media/TV Shows`)
- Media type = movie (non-anime) → Radarr (root `~/media/Movies`)

Anime detection: Jellyseerr tag `anime` (TVDB genre or manual override).

Auto-approve: friends/family requests auto-approve up to N requests/week (configurable later); admin requests always auto-approve.

---

## 6. Notifications matrix (Notifiarr free tier, hosted)

Notifiarr is the single notification hub. API key stored at `secrets/notifiarr.key`.

| Source | Event | Channel | Notes |
|---|---|---|---|
| Sonarr / Sonarr2 / Radarr / Radarr2 | Import success | `#downloads` (user-visible) | "🎬 Title is now available" |
| Sonarr / Sonarr2 / Radarr / Radarr2 | Download failed (3 retries) | `#ops` (admin only) | |
| Bazarr | Subs missing >7 days | `#ops` | |
| Plex / Jellyfin | New media added | `#downloads` | Notifiarr dedupes against *arr Import |
| Tautulli | Recently-added digest (weekly) | `#downloads` | |
| Maintainerr | Items queued for deletion (T-14d warning) | `#ops` | "X items will be removed in 14 days" |
| Maintainerr | Items deleted | `#downloads` | "X is no longer in the library, request again any time" |
| Readarr | Book added | `#downloads` | |
| Mylar3 | Issue/series added | `#downloads` | |
| Komga / Kavita | Library rescan complete | `#ops` (optional) | |
| autobrr | Filter caught release | `#ops` | Useful for tuning |
| Uptime Kuma | Service down | `#ops` | Routes through Notifiarr |
| Cert renewal failure (Ultra.cc-managed) | n/a | n/a | Out of operator scope |

---

## 7. Retention rules

Two distinct mechanisms because Maintainerr only sees Plex/Jellyfin libraries — it cannot govern Komga, Kavita, Calibre-Web, or Audiobookshelf.

### 7.1 Lifetime caps

| Library | Cap | Warning lead | Engine |
|---|---|---|---|
| Movies | 60 days from add | 14 days (warns at day 46) | Maintainerr |
| TV Shows | 60 days from add | 14 days | Maintainerr |
| Anime Movies | 60 days from add | 14 days | Maintainerr |
| Anime | 60 days from add | 14 days | Maintainerr |
| Books | 365 days from add | 14 days (warns at day 351) | `prune-text-libraries.sh` cron |
| Audiobooks | 365 days from add | 14 days | `prune-text-libraries.sh` cron |
| Comics | 365 days from add | 14 days | `prune-text-libraries.sh` cron |
| Manga | 365 days from add | 14 days | `prune-text-libraries.sh` cron |
| Podcasts | 365 days from add | 14 days | `prune-text-libraries.sh` cron |

Reasoning: video files are large and cycle through "watched / not watched" quickly; books/comics/audio are tiny and tend to be revisited less predictably, so they get the longer leash.

### 7.2 Maintainerr-governed libraries (video)

One Maintainerr collection per library. Configuration:
- Filter: `Plex.addedAt is older than 60 days` (or Jellyfin `DateCreated` for Jellyfin-only items).
- Schedule: daily evaluation.
- Action: delete file via *arr API + post Jellyseerr deletion webhook + Notifiarr "deleted" message to `#downloads`.
- Pre-delete warning: items entering the 14-day window post a digest to Notifiarr `#ops` once per day ("X items will be removed in 14 days").

### 7.3 Custom-cron–governed libraries (text/audio)

`scripts/prune-text-libraries.sh` is a small bash script run daily via the user's crontab on manitoba. Behavior:

- For each path in `~/media/{Books,Audiobooks,Comics,Manga,Podcasts}`:
  - Find files where `mtime > 365 days`. Delete them.
  - Find files where `mtime > 351 days` (i.e., entering the 14-day window). Emit a daily Notifiarr digest to `#ops`.
  - Trigger a library rescan via Komga/Kavita/Calibre-Web/Audiobookshelf API as appropriate after deletions.
- Idempotent (safe to re-run); stateless (no DB).
- A pre-delete warning state file lives at `~/.cache/prune-text/state.json` so we don't re-warn the same file every day.

### 7.4 Exceptions (both engines)

- Anything tagged "keep" / "favorite" in Plex/Jellyfin (Maintainerr) or present in `~/media/<lib>/.keep-forever` directories (cron) bypasses deletion.
- Currently-watching items (Maintainerr: Plex sessions in the last 7 days, not finished) are skipped.
- New items in their first 14 days are not eligible regardless (so a warning never fires before day 14).

---

## 8. Verification plan & acceptance gates

### 8.1 Automated smoke test (`scripts/smoke-test.sh`)

Single script, idempotent, exits non-zero on any check fail. Reports per-check pass/fail:

1. Indexer reachability — Prowlarr `/api/v1/indexer/{id}/test` per indexer; expect 200 + IsValid=true.
2. Indexer search — Prowlarr search for a known free-leech title; expect ≥1 result.
3. *arr → qBit reachability — `/api/v3/downloadclient/test` per *arr.
4. *arr → Plex/Jellyfin reachability — `/api/v3/notification/test` per Connect.
5. Bazarr → *arr — `/api/system/status` per connected *arr.
6. Maintainerr → Plex/Jellyseerr — settings test endpoints.
7. Notifiarr round-trip — POST a test notification; expect Discord delivery.
8. Hardlink sanity — `stat -c '%h'` ≥ 2 on at least 5 sample files in Movies/TV Shows.
9. Disk quota — `app-stats show` parsed; usage <90% of 3 TB; warn ≥80%.
10. Service health — Uptime Kuma reports all monitored services up.
11. Unpackerr extract sanity — drop a small password-protected RAR into a watched path; expect extracted file to appear within `interval + retry_delay` and Unpackerr log shows success.
12. Landing page reachability — `curl -k -u <htpasswd> https://quadstronaut.seedbox.example.com/` returns Homarr's HTML (200, `<title>` contains Homarr).
13. Prune-text-libraries dry-run — run `scripts/prune-text-libraries.sh --dry-run` against a synthetic test file aged 366 days; expect deletion candidate listed in dry-run output.

### 8.2 Manual canary checklist

- Plex claim refresh (only if needed during reconfig).
- Jellyfin first-run wizard; libraries point at all `~/media/*` dirs; Plex SSO plugin enabled.
- Plex SSO end-to-end via Jellyseerr.
- **Movie canary:** request a small public-domain title via Jellyseerr → Radarr → qBit → import → Plex/Jellyfin scan → Notifiarr delivery.
- **Anime canary:** request a small anime title via Jellyseerr → Sonarr2 → qBit → import → Plex/Jellyfin scan → Notifiarr delivery.
- **Maintainerr canary:** mark a junk title for deletion; verify warning at 14d, deletion at 60d, Jellyseerr re-request flag set, Notifiarr `#downloads` "deleted" message.
- Mobile UX preview: friends/family walkthrough on phone against the Homarr public board.

### 8.3 Acceptance gates (must all pass before user switchover)

- Automated smoke test green for **3 consecutive runs over 48 hours**.
- Manual canary lifecycle complete on **at least one Movie and one Anime**.
- Disk usage trend over **7 days** shows growth bounded by ingest rate (no runaway leak).
- A synthetic delete-canary at the 7-day mark: artificially mark a junk title as 60-day-aged in Maintainerr's eligibility filter and confirm deletion fires + Jellyseerr re-request flag is set + Notifiarr `#downloads` posts.

---

## 9. Landing page (Homarr two-board)

### 9.1 Public board

URL: `https://quadstronaut.seedbox.example.com/` (after htpasswd).

Tiles (final):
1. Big "Watch on Plex / Watch on Jellyfin / Request" trio
2. Recently added carousel (Plex/Jellyfin/Sonarr/Radarr aggregate)
3. Currently streaming ("Mom is watching Bridgerton")
4. Search bar (Jellyseerr-backed)
5. "How to request" mini-guide
6. Books/comics shortcuts (Komga, Kavita, Calibre-Web, Audiobookshelf tiles)
7. Server status banner (Uptime Kuma summary)
8. **#11** Bandwidth/quality chart (last 24 h)
9. **#12** Maintainerr "going away" list (titles within their warning window)
10. **#18** Donation/cost reminder (soft "servers cost money")
11. **#19** "Surprise me" button
12. **#21** Plex "On Deck" continue-watching row
13. **#23** Re-request banner for previously-deleted items
14. **#28** First-time onboarding popup
15. **#30** Custom announcement banner (admin-editable)
16. **#31** Episode countdown for currently-airing shows on user's list
17. **#33** "Subtitles 101" link (boomer-parent FAQ)
18. **#49** QR codes ("Add Plex to your phone", "Open Jellyseerr")

### 9.2 Admin board

Adds to public:
- Service health matrix (Uptime Kuma all services)
- Disk usage gauge with quota traffic-light (green <70%, yellow 70-90%, red >90%)
- Active downloads (qBit queue + ETA + speed)
- Pending requests queue (Jellyseerr)
- Upcoming releases calendar (Sonarr/Radarr)
- Bazarr missing-subs panel
- **#22** Active stream device matrix (transcoded vs direct)
- **#36** Aggregated red flags (failed downloads, missing subs >7d, autobrr errors)
- **#38** Storage forecast ("at current ingest, quota fills in N days")
- **#42** One-click "scan now" buttons per app
- **#43** Upcoming Maintainerr deletions
- **#44** Indexer health (Prowlarr success rate per indexer)

### 9.3 Cross-cutting

- **#45** Mobile-first responsive layout
- **#46** PWA enabled — "Add to Home Screen"
- **#48** No-login family experience (htpasswd is the only auth gate for the public board)
- **#50** Theme toggle (light/dark/auto)
- **#51** Search bar unifies Plex (play now) + Jellyseerr (request)

### 9.4 Nginx mapping

User nginx (`~/.apps/nginx/sites-enabled/<file>`) gains a `location = /` proxy to Homarr's bound port. The default `~/www/index.html` autoindex behavior is replaced. Existing `proxy.d/*.conf` per-app entries are untouched.

---

## 10. Out-of-scope decom roadmap (post-verification)

Strictly operator-initiated, after acceptance gates pass and users have switched.

| Day | Action |
|---|---|
| 0 (gates pass) | Notify users of new URLs |
| 0–7 | Watch logs and Notifiarr stream; address any user-reported regressions |
| 7+ | `app-jackett uninstall` (Prowlarr fully feeding *arrs) |
| 7+ | `app-ombi uninstall` (Jellyseerr serving requests) |
| 7+ | `app-medusa uninstall` (Sonarr handling load) |
| 7+ | `app-pyload uninstall`, `app-deluge uninstall`, `app-transmission uninstall` |
| 7+ | `app-mariadb uninstall` (after `~/.apps/mariadb/databases/nextcloud` confirmed empty/unneeded) |
| 14+ | Delete Jackett indexer `.bak` files |

---

## 11. Operator review checklist

Before this spec is locked and we transition to writing-plans:

- [ ] Goals & non-goals match operator intent
- [ ] App disposition table is complete and accurate
- [ ] Folder layout is acceptable (no migration of existing 1.8 TB seeding torrents)
- [ ] Maintainerr rules (60 d video / 365 d text+audio, 14 d warning) match operator preference
- [ ] Notification matrix routing matches operator preference
- [ ] Landing page tile selection matches operator preference
- [ ] Decom roadmap is acceptable as post-rectification operator activity
- [ ] No secrets are committed in this spec

---

## 12. References

- Ultra.cc docs: https://docs.ultra.cc/
- Ultra.cc SSH guide: https://docs.ultra.cc/connection-details/ssh/your-ultra-shell-a-beginners-guide
- TRaSH Guides — quality profiles, naming: https://trash-guides.info/
- Maintainerr docs: https://github.com/jorenn92/Maintainerr
- Notifiarr API: https://notifiarr.wiki/
- PlexEcosystem spec (sister project, pre-pivot): `P:/Documents/git/PlexEcosystem/docs/superpowers/specs/2026-05-07-plex-ecosystem-design.md`
