# UCC uninstall runbook (QFLX-60, for QFLX-56)

Operator ruling 2026-10-21: once a migrated app's native unit is verified
**functionally the same** as the UCC container it replaced, uninstall the
dormant UCC app. Order: **Unpackerr pilot (after 2026-10-25)**, then the arrs,
then SAB/qBit/Tautulli/Postgres, **Seerr last** (blocked, see below).

Tool: `~/scripts/ops/ucc-uninstall.sh <app>` (dry-run) then `--execute`.
Never during the Monday 11:00-15:00 UTC window (the script refuses).

## What the script guarantees

- Resolves the app via `~/bin/appctl lookup` (same resolver as every lifecycle call).
- Refuses (exit 2) unless `class: systemd`, `ucc_dormant: true`, no
  `swap_state: pending-swap`, native unit `active`, key data files present.
- Backs up `~/.apps/<slug>` to `~/.opt/maint/ucc-uninstall/<slug>-<ts>.tgz`
  (bin/logs/caches excluded) and verifies the tarball lists the key files.
- Snapshots `~/.apps/nginx/proxy.d/<slug>.conf`.
- Runs **only** `app-<slug> uninstall --keep-config`. Plain `uninstall` deletes
  the config files, and `~/.apps/<slug>` IS the live native data dir.
- Post-checks: unit still active (MainPID logged), key files present, proxy
  conf present (restored + `appctl proxy-reload` if it vanished, still exit 1),
  no container processes (`runtime_parity.woken_container`). Any failure =
  exit 1, **no automatic reinstall**.
- Log: `~/.opt/maint/ucc-uninstall.log`.

## Parity checklist -- do ALL before `--execute`

Generic (every app):

- [ ] Native unit active >= 7 days, `NRestarts` 0 or explained.
- [ ] Kuma monitor for the app green over the soak; `runtime-parity` canary green.
- [ ] Every canary that touches the app green (`scripts/canaries/README.md`).
- [ ] `deploy-drift` green (box == master).
- [ ] Dry-run passes every gate and prints the expected key files.

### Unpackerr (pilot)

- [ ] `qflix-unpackerr.service` active; `pgrep -f /unpackerr` shows only the native pid.
- [ ] An archived release was extracted by the NATIVE process (its log, not the container's).
- [ ] Sonarr/Radarr imported that extracted release.
- [ ] Kuma "Unpackerr" green; `unpackerr.conf` has no `/config` `/data` `/downloads` paths.

### Arrs (Sonarr/Sonarr2/Radarr/Radarr2/Prowlarr/Bazarr)

- [ ] API answers: `/api/v3/system/status` (Prowlarr `/api/v1`, Bazarr `/api/system/status`) with the expected version.
- [ ] Queue moves: a grab -> download -> import completed under the native runtime.
- [ ] Scheduled tasks ran (RSS sync, refresh) since the swap (`/api/v3/system/task`).
- [ ] Prowlarr: indexer test + app sync reach every arr. Bazarr: a subtitle was fetched.
- [ ] Proxy route (`proxy.d/<slug>.conf`) answers through nginx.

### SABnzbd / qBittorrent

- [ ] API answers (SAB `mode=version`, qBit `/api/v2/app/version`).
- [ ] A download completed and was handed to an arr since the swap.
- [ ] qBit: WebUI bound (no boot-bind squatter); seeding/ratio actions intact.
- [ ] qBit config lives at `~/.config/qBittorrent/qBittorrent.conf` -- confirm
      `--keep-config` keeps it (the script checks after; back it up by hand first).

### Seerr -- BLOCKED

The member portal vhost `seerr-<slot>` is owned by the UCC seerr app;
uninstalling it removes the route members use. The script refuses `seerr`
outright. Needs a replacement route first (QFLX-56).

## If a post-check fails

1. Read the log line(s) `POST-CHECK FAILED:`.
2. Data file gone: stop the native unit, restore from the tarball
   (`tar -xzf <tgz> -C ~/.apps`), start the unit, re-verify.
3. Proxy conf: the script restored it; confirm the route answers.
4. Container process left: `app-<slug>` is gone, so kill the stray pid and
   check `runtime-parity`.
5. Do not reinstall the UCC app unless rolling back the whole migration.
