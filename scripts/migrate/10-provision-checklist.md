# 10 — provision the green slot (operator checklist)

Everything below is panel/browser work Ultra.cc gives no API for. Do it once,
record the two values at the bottom, then hand control back to the scripts.

## Buy

- [ ] Ultra.cc → **Gold** (22 TB HDD / 55 TB monthly upload / 50 Gbps shared).
- [ ] Location: **NL** (has Platinum 28 TB above it — headroom without a
      transatlantic move later) or **Canada** (closer to NA viewers).
      Singapore only if the membership moves to APAC.
- [ ] During checkout, pick **Plex** as the included media server option
      (Jellyfin/Emby declined — Plex is canonical since 2026-05-10).

## Panel installs (the 18 UCC-class apps, from `manifest/apps.yaml`)

Install each from the Ultra.cc control panel so the `app-<slug>` wrappers,
ports, and nginx fragments exist. Order does not matter except **postgres
before listmonk's configure phase** runs later.

- [ ] plex
- [ ] sonarr
- [ ] sonarr2 *(second Sonarr instance — panel "install another instance")*
- [ ] radarr
- [ ] radarr2 *(second instance)*
- [ ] prowlarr
- [ ] bazarr *(bazarr2 is NOT a panel app — bare-python install, scripted later)*
- [ ] qbittorrent
- [ ] sabnzbd
- [ ] seerr
- [ ] tautulli
- [ ] audiobookshelf
- [ ] kavita
- [ ] komga
- [ ] calibre-web
- [ ] flaresolverr
- [ ] unpackerr
- [ ] postgres

Everything else (listmonk, tdarr, vlogs, dash, kuma, maint daemon, canaries,
cron-class apps) is installed by `20-install-stack.sh` from the repo.

## Access

- [ ] Add the workstation SSH public key in the panel (same key as blue:
      `~/.ssh/id_ed25519.pub`).
- [ ] Confirm `ssh <user>@<new-host> hostname` works from the workstation.
- [ ] Authorize BLUE's key on green too (media sync runs blue→green):
      `15-bootstrap-new.sh` prints the exact line if missing.

## Record (the scripts take it from here)

- [ ] `NEW_HOST=` user@host — passed to every `scripts/migrate/*.sh`.
- [ ] New public FQDN (panel shows it) — DNS work at cutover only.
