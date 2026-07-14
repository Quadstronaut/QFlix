# Operator-deferred manual steps

**v2 production push 2026-05-09 — most deferrals resolved.** Remaining
items below are now narrow, well-bounded, or genuinely require human
judgment / one-time UI work that resists scripting.

Last reconciled 2026-05-16 against the 2026-05-15 → 2026-05-16 audit sweep.

---

## Still requires the operator

### Phase 16 — uninstall the 6 stopped apps (ready 2026-05-16)

All 7 apps stopped 2026-05-09 (see `docs/transition-log.md`). The 7-day
hold ended **today (2026-05-16)** — ready to execute. Ombi alone holds
out pending a Wizarr invite path replacement (it was the invite path too).

```bash
sshm 'app-jackett uninstall && app-medusa uninstall && \
  app-pyload uninstall && app-deluge uninstall && \
  app-transmission uninstall && app-mariadb uninstall'
```

Re-check `~/.purged-2026-05-11/` afterwards in case anything else
deserves a sweep (some apps' `app-<x> uninstall` doesn't clean up
nginx fragments / cron entries — the 2026-05-11 audit-sweep already
caught most of those).

### ~~Homarr `mediaReleases` widget — TRPCClientError~~ — MOOT 2026-07-13

Homarr was fully decommissioned 2026-07-13 (uninstalled; replaced by the
qflix-dash SvelteKit board at root). The decorative widget error no longer
exists because the app is gone.

### Bounced system mail — `root: usbx` unrouteable alias (human-in-the-loop)

Queued from the 2026-07-14 health audit. `/var/spool/mail/quadstronaut` held one
bounce (`Mail delivery failed: returning message to sender`).

**Scoped findings (2026-07-14):**
- **Not a QFlix delivery problem, and NOT customer-facing.** Member newsletters go
  out via the **Listmonk API/SMTP**, never the local MTA — unaffected.
- The bounce is host-internal: a one-off `sudo` **SECURITY** notice (`quadstronaut :
  a password is required ... COMMAND=/bin/cat /etc/seedbox/appmanager/app-manager.py`,
  **May 25 2026**) auto-mailed to `root`. The shared box aliases **`root: usbx`**
  (`/etc/aliases`), and `support@seedbox-provider.example.com` is **Unrouteable** → permanent
  bounce back into our spool.
- **Non-recurring:** exactly **1** message in the entire spool, ~7 weeks old. No
  `MAILTO` in our crontab (cron mails the local user, stays local). No QFlix
  script/unit uses `sendmail`/`mail`/`smtplib`.
- The `root → usbx` alias is **root-owned Ultra.cc host config** — we can't change
  it without root.

**Decision for the operator (why human-in-the-loop):**
1. **Do nothing** (recommended) — cosmetic, single stale message, no customer impact.
   Optionally clear the spool: `ssh manitoba 'cat /dev/null > /var/spool/mail/quadstronaut'`.
2. **Redirect our own user mail** to a real inbox if you want to *see* future
   system/cron notices: set `~/.forward` to your address (or `MAILTO=` in crontab).
   Note: that forwards *quadstronaut* mail; the *root* SECURITY bounce is separate.
3. **Ultra.cc support ticket** if root-addressed system mail matters to you — ask
   them to point the `root` alias at a routable address. Low value; the sudo event
   was a one-time manual `cat` of a root-owned file, not an ongoing signal.

### Notifiarr CLIENT daemon — Plex push notifications

Plex push events (play / pause / scrobble) require the Notifiarr
client daemon running on the seedbox + a Plex Webhook pointing at
`http://localhost:5454/plex?token=<plex-token>`. Without it, Plex
events still flow via Tautulli's Webhook agent (configured 2026-05-09)
— the client is a redundant secondary channel. Deferred unless
operator wants the duplicate path.

### UI smoke checklist (operator + agent loop)

After all CLI/API smoke turns green, run a one-link-at-a-time UI
walkthrough with the operator covering:
- QFlix Dashboard (qflix-dash) renders at root, all tiles clickable
- Seerr request flow end-to-end
- Plex streaming a recently-added title
- Calibre-Web admin login (rotated password)
- Tautulli notifications fire to Discord on Watched

Drive this from the agent prompt — see Task #19 in the session task list.

---

## Resolved 2026-05-11

- **Newsletterr (template + schedule) + Listmonk cutover campaign** —
  superseded entirely. Newsletterr was purged 2026-05-11; the
  replacement (`scripts/qflix-newsletter/` Python package) ships its
  own template at `qflix_newsletter/templates/weekly.html.j2`, renders
  every Monday 08:00 via `qflix-newsletter.timer`, and posts to
  Listmonk via the API. There is no UI tunnel anymore. Operator
  verifies via the Mon morning email + Kuma `Qflix Newsletter` monitor.
- **Listmonk cutover campaign** — also dead. The migration email was
  either sent during the 2026-05-09 push (see Listmonk admin →
  Campaigns → Archive) or superseded by the first regular Mon digest
  (Listmonk's subscriber list ported across; existing subscribers are
  on the new pipeline by default).

## Resolved 2026-05-09

| Phase | Was | Resolution |
|-------|-----|------------|
| 9.4 anime gap | Anime+Anime Movies in Jellyfin only | Added to Plex via 59-plex-anime-libraries.py; Maintainerr 60-day rules now cover all 4 Plex libs (27b-maintainerr-rules.py NAME_OVERRIDES routes anime libs to Sonarr2/Radarr2) |
| 12.2 Plex Webhook | Required Notifiarr client | Tautulli Webhook agent → Notifiarr passthrough wired (58-tautulli-notifiarr-webhook.py); covers play/scrobble events. Notifiarr client itself deferred (see above) |
| 13 Homarr boards | DONE prior session | — |
| 15 canaries | Manual UI walkthrough | Automated as scripts/canaries/{movie,anime,deletion,mobile-ux,qbit-stall,vlogs-stall}.sh; wired into smoke-test.sh §15 |
| 16 stop apps | DESTRUCTIVE | All 7 stopped via SSH (docs/transition-log.md). Uninstall hold ended 2026-05-16 — see top of file |
| 22 Conjurr config | UI tunnel | Conjurr+Newsletterr purged 2026-05-11; superseded by qflix-newsletter Python pkg |
| 23 Newsletterr config | UI tunnel | Same — superseded |
| 25 Listmonk cutover | UI campaign create | Superseded — see "Resolved 2026-05-11" above |
| 26 Ombi decom | PARKED | Ombi stopped (invites paused). Decom after Wizarr alternative |
| 29-31 Tdarr | UI library setup | 50b-tdarr-config.py adds 3 libraries (Movies/TV/Anime), worker cap 2/2, webUIPort fix |
| Tdarr Phase 30 | Library pass gated | Go-live 2026-05-30: `processLibrary=True` enforced by 50b's `ensure_library_processing()` (was `set_non_destructive_mode()` forcing False). Transcoding is live; re-running 50b now preserves it instead of halting it (PR #65) |
| 34 Recyclarr no-4k | Smoke gate red | 57-no-4k-enforce.py disabled 9 2160p entries across 3 factory profiles. Gate green |
| Tautulli Notifiarr | Removed native agent | Webhook agent wired (58-tautulli-notifiarr-webhook.py) |
| Calibre-Web pw | admin/admin123 | Rotated to shared admin password via direct SQLite UPDATE (PBKDF2-SHA256) |
| Maintainerr rules | 2 (Plex-only) | 4 active (Movies, TV, Anime, Anime Movies — anime branch routed to Sonarr2/Radarr2 via NAME_OVERRIDES) |
| Tdarr `:8265` ghost | Broken redirect | webUIPort baked into Tdarr_Server_Config.json (live + 50-tdarr-install.sh) |
| Tdarr nginx subpath | Disabled fragment | Re-enabled `~/.apps/nginx/proxy.d/tdarr.conf`; SIGHUP-reloaded |
| Listmonk `/public/` assets | Hardcoded paths | nginx sub_filter rewrites href/src to `/listmonk/public/...` (43-listmonk-install.sh) |
| Lifecycle upgrade/downgrade | Phase-1 stubs | Real impl for ucc / systemd / cron / library; rollback on health failure; recovery auto-downgrade after attempt-cap |

---

## Captured already (no operator action)

- ✓ Plex token, host, port → `secrets/plex.{token,host,port}` (port via Docker 172.17.1.250:32400)
- ✓ Seerr API key → `secrets/seerr.key`
- ✓ All active *arr API keys (Sonarr/Radarr/Sonarr2/Radarr2/Prowlarr) → `secrets/<arr>.key` (Readarr/Mylar3 secrets moved to `.purged-2026-05-11/` along with the apps)
- ✓ qBit credentials → `secrets/qbittorrent.{user,password}`
- ✓ Komga / Kavita / Audiobookshelf / Calibre-Web / Maintainerr / Bazarr / Bazarr 2 / Tautulli API keys → `secrets/<app>.key`
- ✓ htpasswd password → `secrets/htpasswd.password` (= shared admin password)
- ✓ Listmonk API user + token → `secrets/listmonk.{api_user,api_token}`
- ✓ Discord webhook + operator user-id → `secrets/discord-webhook.url`, `secrets/discord-operator.id`
- ✓ Seedbox public FQDN + SSH host → `secrets/seedbox.host`, `secrets/seedbox.ssh-host`

### 2026-06-06 — quality-fallback Kuma monitor

`qflix-quality-fallback` (daily 07:30 UTC) is in the manifest but its Kuma
push monitor "Qflix Quality Fallback" requires operator-held Kuma creds:
run `scripts/maint/bootstrap-kuma-monitors.py` once. Until then the pusher
logs a missing-token WARN for this app (harmless).
