# 60 — decommission blue (operator checklist)

Only after green has been canonical for **at least 7 quiet days** (no
rollback candidates, Kuma green, members not reporting ghosts).

- [ ] Confirm DNS has fully cut over (dig from a network you don't control).
- [ ] Blue Plex has served zero streams for 72 h (Tautulli on blue).
- [ ] Export blue's final state for the archive:
      - `~/.opt/maint/` logs + notify.log
      - VictoriaLogs data dir snapshot (or accept 90-day loss — decide)
      - final `quota -s` + `migration-state.json` delta
- [ ] Torrent ratio obligations: blue's qBittorrent has been seeding since
      cutover; check private-tracker ratios are safe to abandon, or keep the
      slot one more billing cycle purely as a seed box.
- [ ] Remove blue's SSH host entries + tunnel daemon config on the
      workstation; retire `rclone-qflix` mount or re-point it at green.
- [ ] Update repo: secrets/seedbox.host + seedbox.ssh-host → green values,
      README FQDN references, `docs/internal-app-tunnels.md` port table.
- [ ] Cancel the slot in the Ultra.cc panel.
- [ ] Close the migration: move this checklist's completion date into
      `docs/transition-log.md`.
