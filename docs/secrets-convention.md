# Secrets

This directory is **gitignored**. Nothing here is tracked.

Each file holds one secret value, one per line. Scripts read these via path, never via env var leakage.

## Inventory

| File | Purpose | Source |
|---|---|---|
| `notifiarr.key` | Notifiarr hosted API key | https://notifiarr.com profile → API Keys |
| `prowlarr.key` (TBD) | Prowlarr API key | `~/.apps/prowlarr/config.xml` ApiKey field on manitoba |
| `sonarr.key` (TBD) | Sonarr (general TV) API key | `~/.apps/sonarr/config.xml` |
| `sonarr2.key` (TBD) | Sonarr2 (anime TV) API key | After install: `~/.apps/sonarr2/config.xml` |
| `radarr.key` (TBD) | Radarr (general movies) API key | `~/.apps/radarr/config.xml` |
| `radarr2.key` (TBD) | Radarr2 (anime movies) API key | After install |
| `bazarr.key` (TBD) | Bazarr API key | Bazarr UI → System → Security |
| `tautulli.key` (TBD) | Tautulli API key | Tautulli UI → Settings → Web Interface |
| `jellyseerr.key` (TBD) | Jellyseerr API key | Jellyseerr UI → Settings → API |
| `maintainerr.key` (TBD) | Maintainerr API key | Maintainerr UI → Settings |
| `plex.token` (TBD) | Plex authentication token | https://www.plex.tv/claim or extract from a Plex Web Tools session |
| `jellyfin.key` (TBD) | Jellyfin API key | After install: Dashboard → API Keys |
| `omdb.key` (TBD, optional) | OMDB metadata API key | https://www.omdbapi.com/apikey.aspx (you have one in Jackett: 708253b1) |
| `flaresolverr.key` (TBD, optional) | FlareSolverr health-check key | If FlareSolverr is configured with auth |

## Filling these in

Most keys come into existence only after the corresponding app is installed/configured. Capture them as we go; do not commit them.

`scripts/secrets-pull.sh` (TBD) is a helper that SSHes into manitoba, scrapes the available config files, and populates the local secrets/ folder. Read-only on the seedbox; idempotent.
