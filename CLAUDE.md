# QFlix — operator runbook for Claude

Self-healing Plex stack on one Ultra.cc shared seedbox: `manifest/apps.yaml` (single source of truth), `manitoba-maint` Python daemon, 35 canaries, Kuma push monitors, weekly newsletter. README + `inventory.md` describe the system; this file is what must not go wrong.

## Access & safety
- SSH is **`quadstronaut@seedbox.example.com` only** (wrapper `scripts/lib/ssh.sh`). Wrong-user guesses trip Ultra.cc fail2ban and kill the tunnel; ban bypass = `ssh -J starhold quadstronaut@seedbox.example.com`.
- **Monday maintenance window (11:00–15:00 UTC): no box operations.** Deploy before or after, never during. Local repo work is fine.
- **Repo is PUBLIC.** Never commit member data, activity, hostnames beyond the sanitized `seedbox.example.com`, or secrets (`secrets/` is untracked). Member privacy is absolute — no per-member activity in any surface.
- Never `git reset --hard` or destroy uncommitted WIP without explicit authorization; "clean up" is not authorization.
- Always push. Session-end invariant: repo, GitHub, and the running box are on the **same commit** (`deploy-drift` canary checks it).

## What's live vs. gone (check before asserting)
| Live | Retired — do not restore |
|---|---|
| Plex (primary), Seerr, Prowlarr, Sonarr/Sonarr2, Radarr/Radarr2, Bazarr/Bazarr2, qBittorrent, SABnzbd, Tdarr, Tautulli, **Kometa** (daily 03:30 timer), Listmonk, qflix-dash, qflix-reaper, 35 canaries | Jellyfin/Jellystat (2026-05-10) · Notifiarr (2026-05-10; Discord webhook is the only channel) · Maintainerr → qflix-reaper (2026-06-20) · Homarr → qflix-dash (2026-07-13) · books stack kavita/komga/calibre-web/audiobookshelf (2026-08-16) |

When the operator says something was purged, **verify on the box** (`~/.apps/`, `systemctl --user list-timers`) before agreeing or acting — the manifest is the truth, memory is a lead.

## How work ships
- Every maintenance concern is its **own module / timer / Kuma check** (compartmentalize for the coming migration); don't fold jobs together because cadences overlap.
- New self-pusher → register in `lib/kuma.py STANDALONE_SELF_PUSH_MONITORS` (audit + bootstrap read it). Kuma audit reads `kuma.db`, not `/metrics`.
- Improvements you notice → `TaskCreate`, not a passing mention. Surface known issues proactively.
- Trust durable logs (`~/.opt/maint/*`) over journald; SAB/qBit APIs lie — re-poll to verify.
- Migration to the Gold slot lives on `feature/migration` (worktree `../QFlix-migration`); trigger phrase is "migrate me".

## Memory
Project memory (`~/.claude/projects/G--Documents-GIT-Ultra-cc-QFlix/memory/`) holds ~100 dated incident notes; the memory-router hook surfaces matches per prompt. Dated facts there can be stale — this file and the manifest win.
