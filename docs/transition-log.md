# Transition log — apps stopped/started/decommissioned

Records every reversible state change to the seedbox app inventory. Each
entry captures: when, what, why, how to reverse. New entries on top.

---

## 2026-05-10 — Phase 3 app sweep: 1 installed, 4 deferred/parked

**Action:** evaluated 5 new apps for install (Suggestarr, Buildarr,
Profilarr, Janitorr, Watcharr). Only **Buildarr** installed cleanly —
pure-Python pip install with a nightly 04:30 systemd timer running
`buildarr run`. Manifest entry added (cron-class, kuma_monitor
"Buildarr").

**Deferrals & parks** (see `project_phase3-app-installs-2026-05-10`
memory + `project_seedbox-wasm-oom-blocker`):

| App         | Status   | Reason |
|-------------|----------|--------|
| Suggestarr  | DEFER    | Vite frontend build OOMs on seedbox (Ultra.cc per-process heap cap) |
| Buildarr    | INSTALL  | Pure Python — works |
| Profilarr   | DEFER    | Same Vite OOM blocker |
| Janitorr    | DEFER    | No upstream jar; source build risks same OOM; Maintainerr already covers role |
| Watcharr    | PARK     | No subpath + no subdomain — same Wizarr pattern |

**Why:** Operator approved the 5-app install autonomously. Reality
intervened: 3 of 5 require JS-runtime build steps that fail on this
seedbox (WebAssembly heap reservation OOM despite 250GB free RAM —
appears to be a per-process address-space limit). 1 has no published
binary. 1 has the same Wizarr-style subpath impossibility.

**Rollback:** `bash scripts/configure/50-buildarr-install.sh` is
idempotent; to undo, `systemctl --user disable --now buildarr.timer
buildarr.service && rm -rf ~/.apps/buildarr ~/.config/systemd/user/buildarr.*`.

**Followups for operator:**
- For Profilarr/Suggestarr: build the React/Vite frontends on
  operator's workstation, rsync `frontend/dist/` into the seedbox
  install — this cleanly sidesteps the seedbox memory blocker.
- For Janitorr: extract jar from `ghcr.io/schaka/janitorr:jvm-stable`
  using rootless `crane` or `skopeo`; if that succeeds, finish the
  systemd install with Eclipse Temurin 17.
- For Watcharr: blocked until upstream lands subpath support
  (issue #312) or operator obtains a subdomain.

---

## 2026-05-10 — Conjurr + Newsletterr retired → qflix-newsletter (Path B)

**Action:** introduced `scripts/qflix-newsletter/` (standalone Python
package, Jinja2 + Tautulli + arr-calendar + Gemini + Listmonk campaign
API). Deployed via `scripts/configure/49-qflix-newsletter-install.sh`
to `~/.apps/qflix-newsletter/` with a Mon-08:00 systemd timer
(`scripts/maint/systemd/qflix-newsletter.{service,timer}`).
Decommissioned Conjurr + Newsletterr in `49b-conjurr-newsletterr-decom.sh`:
stop+disable+remove `conjurr.service`/`newsletterr.service`,
`rm -rf ~/.apps/{conjurr,newsletterr}`, drop heartbeat-conjurr/newsletterr
crons + scripts, drop `listmonk-to-newsletterr-sync.py` (no longer
needed). Manifest now has a single `qflix-newsletter` entry (cron-class,
Kuma monitor "Qflix Newsletter") in place of two.

**Why:** Both apps' missions collapse into one (weekly digest with
optional AI picks). Single Python script removes ~150 MB of Playwright
Chromium (Newsletterr) plus the duplicate Flask/HTTP surface, and lets
us own the email layout end-to-end (Pick of Week → New Movies → New TV
→ Anime → Coming Soon → AI Picks at bottom → Nerd Corner). See
`project_qflix-newsletter-replaces-conjurr-newsletterr.md`.

**Rollback:** restore from git: `git revert <this-commit>`, re-run
`scripts/configure/47-conjurr-install.sh` and `48-newsletterr-install.sh`,
re-add manifest entries, re-run `bootstrap-kuma-monitors.py`. Cutover
email is the smoke test — until that send happens, Listmonk + the
script remain the only validated pieces.

**Followups landing later:**
- The first real-world send (the cutover email itself) doubles as the
  smoke test for the new pipeline. If subscribers don't render correctly,
  fix forward — don't roll back.
- Custom Listmonk admin/public CSS pasted into Settings → Appearance is
  imperfect (sidebar+title selectors didn't fully apply). Tracked in
  `project_listmonk-css-imperfect-2026-05-10.md`; revisit when convenient.

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
