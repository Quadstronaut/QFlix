# 10 — provision box 2 (operator checklist)

Box 2 (green) is a **generic Linux user account**, not a panel slot: every app
is installed natively by `20-install-stack.sh` from the repo (spec section 1).
There are no panel installs to do. The decision on which host it is (another
slot or a generic host behind a front proxy) is D-6.

## Account

- [ ] A user account with `systemd --user` and **linger on**
      (`loginctl show-user $USER -p Linger` says `yes`). Without linger every
      user unit dies at logout.
- [ ] `python3` with PyYAML, `git`, `rsync`, `sqlite3`, `curl` on PATH.
- [ ] Postgres client tools (`psql`, `pg_restore`, `createdb`) for 35's
      dump/restore into the native postgres.
- [ ] Free space for the media library plus app data (00-preflight records
      blue's sizes; check `free_kb` in its green section).

## Access

- [ ] The workstation key can `ssh NEW_HOST true` non-interactively.
- [ ] **Blue's** key is authorized on green: media and appdata copies run
      blue -> green. `30-sync-media.sh` prints blue's public key if not.

## Record (gitignored, never committed)

- [ ] `NEW_HOST` (user@host or an ssh alias): passed to every
      `scripts/migrate/*.sh` as the first argument.
- [ ] `secrets/green/seedbox.host`: green's public hostname (the installers
      read `seedbox.host` to build URLs).
- [ ] Optional `secrets/green/net.app_host` if green's apps must reach each
      other on something other than `127.0.0.1`.
- [ ] Optional `secrets/migrate/front-door.url`: the public URL the cutover
      health gate must see answering after the flip.

Then: `00-preflight.sh NEW_HOST` -> `15-bootstrap-new.sh NEW_HOST --execute`.
