# QFlix migration — implementation plan

Spec: [`../specs/2026-08-08-qflix-migration-blue-green-design.md`](../specs/2026-08-08-qflix-migration-blue-green-design.md)
Branch: `feature/migration`. Nothing here touches master or the live box
mutatively; dry-runs against blue are read-only.

## Conventions every script follows

- Bash, `set -uo pipefail`, sourcing `scripts/lib/ssh.sh` where blue SSH is
  needed; green SSH always through an explicit `NEW_HOST` argument
  (`user@host` or ssh-config alias), never a hardcoded FQDN.
- Mutating scripts are **inert by default**: print the full action plan, do
  nothing without `--execute` (I-3). Read-only scripts (00, 40) run live.
- `STAGE=<token> msg=<detail>` on stderr for every failure, distinct exit
  codes: `0` ok, `1` finding/failure, `2` could-not-assert (house style).
- Idempotent: every step checks current state before acting (I-4).
- Blue is sacred: the only blue writes in the whole tree are 50's freeze
  (pause qBit/SAB, disable import lists) and its 55 mirror (I-2).

## Build order + acceptance

| Step | Deliverable | Acceptance (what "done" means today, pre-green) |
|---|---|---|
| 1 | `00-preflight.sh` | REAL run against blue succeeds → `migration-state.json` written locally (ports, versions, du, timers, monitors). **Gitignored, never committed** — it embeds the live port map and this repo is public. |
| 2 | `10-provision-checklist.md` | UCC app list generated from `manifest/apps.yaml` (18 entries), panel steps, SSH-key step, "record NEW_HOST" step |
| 3 | `15-bootstrap-new.sh` | Dry-run prints exact plan; `--execute` path reviewed; identity-vs-slot-specific secret split matches spec §2.3 |
| 4 | `20-install-stack.sh` | Enumerates the `configure/` phases it will run in order, with the Kuma-channels-OFF guarantee visible in the plan output |
| 5 | `30-sync-media.sh` | `--dry-run` against blue computes source manifest (count+bytes) via rsync -n; `--delta` flag present; `-aH --partial` pinned |
| 6 | `35-sync-appdata.sh` | Per-app stop→copy→start table in dry-run output; Seerr re-point pass listed with the exact API PUTs it will make |
| 7 | `40-validate-green.sh` | Runs the manifest health probes + kuma audit + `--arm-check` and emits a PASS/FAIL checklist; degrades to could-not-assert (2) when green absent |
| 8 | `45-plex-invites.py` | REAL `--dry-run` against plex.tv lists blue's friends + the invite plan; zero writes without `--execute` |
| 9 | `50-cutover.sh` | Orchestrates 30→35→40→channel-attach→gate-swap→DNS instructions; stops on first failure naming completed steps; every mutating leg operator-confirmed |
| 10 | `55-rollback.sh` | Mirror of 50's freeze + channel moves; runnable in <1 min |
| 11 | `60-decommission-old.md` | Checklist only |

## Dry-run matrix (what can be proven before the slot exists)

| Provable today | Needs green |
|---|---|
| 00 full run (read-only on blue) | 15/20/35 `--execute` paths |
| 30 source manifest via rsync -n | 30 actual transfer |
| 45 friend enumeration + invite plan | 45 `--execute` invites |
| 40's could-not-assert path (no NEW_HOST) | 40 PASS run |
| 50/55 plan printouts (inert default) | cutover rehearsal |

## Out of scope (spec §7)

qBit state, Plex metadata, DNS automation, panel automation, dual-write.
