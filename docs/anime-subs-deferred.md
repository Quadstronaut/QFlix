# Anime subtitles — RESOLVED 2026-05-11

**Status:** Closed. Anime subtitle automation now ships via a second Bazarr instance (`bazarr2`).

**Background:** Bazarr supports only one Sonarr and one Radarr per install (upstream wontfix — see [issue #404](https://github.com/morpheus65535/bazarr/issues/404)). We have two Sonarrs (main TV + Sonarr2 anime) and two Radarrs (main movies + Radarr2 anime). Ultra.cc provides `app-sonarr2` and `app-radarr2` but no `app-bazarr2` slot.

**Resolution:** Bare-Python install at `~/.apps/bazarr2/` (python3.11 venv) wrapped by a user systemd unit. Paired with Sonarr2 + Radarr2 via loopback ports — no htpasswd, no SSL. A companion `bazarr2-sync.timer` (hourly oneshot) pins `bazarr2`'s version to `bazarr-1`'s running version by fetching the matching upstream tag and re-applying the host-specific `waitress threads=100→4` patch each time bazarr-1 moves.

**Artifacts:**
- Install: `scripts/install/06-bazarr2.sh` (idempotent)
- Source-of-truth systemd units: `scripts/maint/systemd/bazarr2.service`, `scripts/maint/systemd/bazarr2-sync.{service,timer}`
- Version sync logic: `scripts/maint/bazarr2-sync.py`
- Manifest entries: `bazarr2` (class: systemd) and `bazarr2-sync` (class: cron) in `manifest/apps.yaml`
- Inventory row: `inventory.md` (row between `bazarr` and `qbittorrent`)
- Live state: `bazarr2` on `127.0.0.1:17032/bazarr2/`, talks to `Sonarr2 127.0.0.1:17003/sonarr2/` and `Radarr2 127.0.0.1:17008/radarr2/`

**Host-specific patch:** Bazarr hardcodes `threads=100` for waitress in `bazarr/app/server.py`. The host kernel refuses a burst of 100 thread-creates from a freshly-imported Python child (the LSIO container that runs `bazarr-1` has a cleaner namespace where this works). The sync script rewrites every git checkout to `threads=4` (waitress default), which is plenty for subtitle-fetch traffic.

**Out-of-scope alternatives considered + rejected:**
1. Ask Ultra.cc to provision `app-bazarr2` — would have been blocking and uncertain.
2. Docker Compose under `~/` — user has no Docker daemon access on this seedbox.
3. Migrating anime *arrs into the general Sonarr/Radarr — defeats the anime-isolation spec.
