# Anime subtitles — deferred

**Status:** Out of scope for v1.

**Why:** Bazarr supports only one Sonarr instance and one Radarr instance per Bazarr deployment. We have *two* Sonarrs (general TV + Sonarr2 anime) and two Radarrs (general + Radarr2 anime). Ultra.cc only offers `app-bazarr` once.

**Mitigation in v1:** Anime acquired through Sonarr2/Radarr2 ships with hardsubs in the vast majority of cases (community standard for fan-released anime). Subtitle automation for the rare softsubs case is a manual step.

**Future options if subtitle automation for anime becomes needed:**
1. Run a second Bazarr instance via Docker compose under `~/` (out of `app-*` scope; would require a custom systemd-user service file).
2. Self-host the community fork that supports multiple instances (out of Ultra.cc scope).
3. Migrate anime *arrs into the general Sonarr/Radarr (defeats the spec's anime-isolation rationale).

**Verification of current Bazarr state (2026-05-07):**
- Bazarr 1.5.5 running on `127.0.0.1:17031` (URL base `/bazarr`)
- Configured to talk to Sonarr (`quadstronaut.seedbox.example.com:443/sonarr`, ssl) and Radarr (same pattern, `/radarr`)
- API keys match current Sonarr/Radarr config (verified by bootstrap-discover.sh)
- Synced state: 45 series, 27 movies tracked
- Sonarr2 / Radarr2: not configured in Bazarr; spec accepts this gap.
