# Transition log — apps stopped/started/decommissioned

Records every reversible state change to the seedbox app inventory. Each
entry captures: when, what, why, how to reverse. New entries on top.

---

## 2026-05-10 — Jellyfin + Jellystat purged (Plex-only direction)

**Action:** uninstalled both apps via `app-jellyfin uninstall` and
`app-jellystat uninstall`. Removed from `manifest/apps.yaml` (28 apps
remain), bookmarks.html, and Kuma (monitor IDs 31 + 33 deleted).

**Why:** the Jellyfin trial (per `project_plex-primary-jellyfin-trial.md`)
concluded — operator declared Plex-only is the production direction.
Jellystat depends on Jellyfin so it followed.

**Rollback:** `app-jellyfin install` recreates the app; capture the new
API key into `secrets/jellyfin.key`; re-add the manifest block; re-run
`bootstrap-kuma-monitors.py` for the monitor.

**Followups landing later:**
- Migrate Jellyseerr → **Seerr** (https://github.com/seerr-team/seerr —
  the next-gen Overseerr/Jellyseerr successor; v3.2.0 supports Plex
  natively without the Jellyfin half).

---

## 2026-05-10 — Recyclarr full wiring (Kuma monitor + auto-heal)

**Action:** set `kuma_monitor: "Recyclarr"` in `manifest/apps.yaml`
(was null). Bootstrap created a PUSH monitor; pusher's systemd_only
probe verifies `recyclarr.timer` is active each cycle. Health-check
gap fixed in `lib/health.py::_probe_systemd_only` to read `unit` from
`app.raw["unit"]` (the canonical app-level placement) in addition to
`health.raw["unit"]`. Tdarr-Node (same pattern, kuma_monitor=null)
benefits from the fix the moment it ever gets a monitor.

**Why:** Recyclarr was running but invisible in monitoring. Visibility
+ auto-heal coverage are now consistent with the rest of the stack.

**Out of scope (Tuesday-night work, see Tuesday.md):** extending the
Mon-04:30 cp-clicker upgrade sweep to systemd-installed apps including
Recyclarr's `tarball_swap` upgrade kind.

---

## 2026-05-09 — Phase 16 uninstall: 5 of 7 (operator waived 7-day grace)

**Action:** uninstalled 5 of the 7 stopped apps. Operator explicitly
waived the 7-day rollback grace; no regressions surfaced after stop.

| App          | `app-<x> version` post-uninstall                  |
|--------------|---------------------------------------------------|
| medusa       | "Unable to retrieve application version" (gone)   |
| pyload       | "Unable to retrieve application version" (gone)   |
| deluge       | "Unable to retrieve application version" (gone)   |
| transmission | "Unable to retrieve application version" (gone)   |
| mariadb      | "Unable to retrieve application version" (gone)   |

**Held back (still stopped, not uninstalled):**
- **ombi** — kept for Wizarr-alternative parking; restart if invites needed
- **jackett** — operator request; not yet decommissioned

**Rollback:** if needed, `app-<name> install` re-bootstraps from upstream;
configs are gone (pyload's queue, deluge's torrents, etc. are not preserved).
qBittorrent is the canonical replacement for all 3 torrent clients.

---

## 2026-05-09 — Phase 16 stop: 7 redundant apps

**Action:** stopped (not uninstalled) seven legacy apps that have been
fully replaced by their successors in v2:

| App           | Replaced by                                    |
|---------------|------------------------------------------------|
| ombi          | Jellyseerr (requests) + Listmonk (mass-comms)  |
| jackett       | Prowlarr (single indexer aggregator)           |
| medusa        | Sonarr / Sonarr2 (anime branch)                |
| pyload        | qBittorrent (single torrent client)            |
| deluge        | qBittorrent                                    |
| transmission  | qBittorrent                                    |
| mariadb       | sqlite per-app databases (no shared DB)        |

Verified `systemctl --user is-active <app>.service` returns `inactive`
for all seven.

**7-day grace:** per the v2 decom roadmap, do NOT `app-<x> uninstall`
for at least 7 days — keep configs/databases intact in case of rollback.
Earliest uninstall date: **2026-05-16**.

**Rollback:** `ssh quadstronaut@seedbox.example.com 'app-<name> start'`
brings any app back. Ombi's invite functionality, in particular, is
parked here pending a Wizarr alternative; restart Ombi if invites are
needed in the interim.

**Phase 26 note:** Ombi's mass-comms role is now Listmonk's. After at
least one full Newsletterr Sunday-09:00 cycle (next: 2026-05-10) and
the Listmonk cutover campaign delivery (Phase 25), Ombi can be fully
decommissioned.
