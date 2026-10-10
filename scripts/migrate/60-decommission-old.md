# 60 — decommission blue (operator checklist, never a script)

Blue stays intact through a **14-day decommission hold** after cutover (spec
section 8, invariant I-2: nothing on blue is deleted by the migration). Start
this list only when green has been canonical for 14 days with no rollback
candidate: every Kuma monitor green on green, no member reports, `deploy-drift`
green on green.

## Prove green owns everything

- [ ] `40-validate-green.sh NEW_HOST --post` exits 0 (every manifest app and
      canary healthy on green, green loud).
- [ ] Exactly one side pages and sends (I-1): blue's Kuma human channels are
      detached and `~/secrets/discord-webhook.url` is parked as `.held` on blue;
      blue's newsletter timer is disabled and its listmonk-sync crontab line
      carries the `#qflix-migrate-held#` prefix.
- [ ] The entitlement gate is armed on at most one side (I-5): blue has no
      `execute.conf` drop-in. Arm green only as its own decision (D-5):
      `50-cutover.sh NEW_HOST --arm-green-gate --execute`, then `armed: true`
      in green's roster.
- [ ] The front door resolves to green from a network you do not control.
- [ ] Blue's Plex served zero streams for 72 h (Tautulli on blue). Members
      re-pinned to green's new Plex identity (QFLX-42 campaign, P1).

## Archive blue's evidence (to the workstation, outside the repo)

- [ ] `~/.opt/maint/` logs, `notify.log`, `notify-fail.log`, swap state.
- [ ] VictoriaLogs: green already holds the 90-day data copied by 35. Decide
      whether blue's tail since the last 35 run is worth keeping.
- [ ] Final `quota -p` and `secrets/migrate/migration-state.json`.

## Torrents

- [ ] Private-tracker ratios: blue's qBittorrent was frozen at cutover and the
      profile moved to green. Confirm green seeds the same set before letting
      blue's slot lapse; or keep blue one more billing cycle as a seed box.

## Workstation and repo

- [ ] Re-point `secrets/seedbox.ssh-host` / `seedbox.host` at green, and every
      tunnel or `~/.ssh/config` alias that names blue.
- [ ] Remove `secrets/migrate/` evidence once archived (it holds ports and the
      freeze snapshot).
- [ ] Update `inventory.md` and `docs/transition-log.md` with the cutover date.

## Cancel

- [ ] Cancel the Ultra.cc slot in the panel. This is the only irreversible
      step: do it last, after everything above is ticked.
