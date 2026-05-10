# Canaries

Automated probes that exercise the full request → download → import →
notify chain for each major content type. Each canary makes a request
via the *real* user-facing API (Jellyseerr), then polls downstream
state to confirm the request propagates correctly.

These do NOT actually download a movie/TV file — Jellyseerr requests
are submitted but immediately marked declined to keep the *arr queue
clean. The point is to verify the *control plane*, not to seed real
content.

## Files

- `movie.sh`       — request a movie, expect Radarr to receive it
- `anime.sh`       — request anime, expect Sonarr2 to receive it
- `deletion.sh`    — verify Maintainerr deletion rule existence
- `mobile-ux.sh`   — render-time check on the Homarr public board

## Exit codes

- 0 — pass
- non-zero — fail; stderr explains
