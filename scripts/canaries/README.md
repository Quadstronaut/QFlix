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

- `movie.sh`       — Seerr → Radarr push test (creates+deletes a request, verifies externalServiceId)
- `anime.sh`       — Seerr → Sonarr2 push test (same pattern, tv mediaType, seasons:[1])
- `deletion.sh`    — verify the 4 Maintainerr 60-day rules exist and are active
- `mobile-ux.sh`   — render-time check on the Homarr public board

## Stage labels (failure messages on stderr → Kuma `msg=`)

- `seerr-up-fail` / `radarr-up-fail` / `sonarr2-up-fail` — the named API didn't return 200
- `seed-pick-fail` — *arr has zero movies/series to seed the probe
- `seerr-push-fail` — POST /api/v1/request returned non-2xx/409 (or 409 with no recoverable id)
- `arr-not-populated` — externalServiceId stayed null after 30s of polling
- `verify-fail` — externalServiceId did not match the *arr's id for the seed
- `cleanup-fail` — DELETE request returned non-2xx (warned to stderr, probe still passes)

## Exit codes

- 0 — pass; stdout has the `PASS: ...` line that Kuma stores as `msg=`
- non-zero — fail; stderr's `STAGE=... msg=...` line becomes Kuma `msg=`
