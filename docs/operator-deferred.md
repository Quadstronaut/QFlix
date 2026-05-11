# Operator-deferred manual steps

**v2 production push 2026-05-09 — most deferrals resolved.** Remaining
items below are now narrow, well-bounded, or genuinely require human
judgment / one-time UI work that resists scripting.

---

## Still requires the operator

### Newsletterr — template + schedule (UI drag-and-drop)

Settings ARE configured (plex_url + token, tautulli, conjurr, smtp
creds — all present in the `settings` table). The remaining gap is
the visual template and the weekly schedule, which Newsletterr
expects via its drag-and-drop builder. ~5 minutes via SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:42016 quadstronaut@seedbox.example.com
# in browser: http://localhost:8080/
```

1. Templates → New → drag a "Recently Added" snap-in + a "Personalized
   recommendations" snap-in. Save.
2. Schedule → frequency=weekly, send_time=09:00, start_date=next
   Sunday, list="Manitoba (auto)".
3. Send Now your test template to `operator@example.com` to verify.

### Listmonk cutover campaign — confirm draft + send

`scripts/configure/60-listmonk-cutover.py` was run live: the template
+ campaign exist in DRAFT state. Once the Newsletterr first weekly
digest is also configured (above), open the Listmonk admin UI, review
the cutover body, and either click "Send Now" in the UI or re-invoke
the script with `--send`:

```bash
sshm "python3 -" < scripts/configure/60-listmonk-cutover.py --send
```

13 subscribers, single email each. Body diverges from the original
plan: the `/alerts/` paragraph (#2) was removed because ntfy/alerts
was dropped on 2026-05-08 (Ultra.cc constraints).

### Phase 16 — uninstall the 7 stopped apps (after 2026-05-16)

All 7 apps stopped 2026-05-09 (see `docs/transition-log.md`). Per the
v2 decom roadmap, hold ≥7 days before `app-<x> uninstall` to allow
rollback if a regression surfaces. Earliest uninstall date:
**2026-05-16**.

```bash
ssh quadstronaut@seedbox.example.com 'app-ombi uninstall && \
  app-jackett uninstall && app-medusa uninstall && \
  app-pyload uninstall && app-deluge uninstall && \
  app-transmission uninstall && app-mariadb uninstall'
```

Ombi specifically: keep stopped (not uninstalled) until a Wizarr
alternative is in place, since Ombi was the invite path too.

### Homarr `mediaReleases` widget — TRPCClientError (decorative)

The Plex Recently-Added widget renders a TRPCClientError visible to
users. Likely cause: widget options shape mismatch with Homarr v1's
zod schema or a missing required field on the `integrationSecret`
row. Inspect:

- `homarr-labs/homarr` repo, `packages/widgets/src/media-releases/`
- `journalctl --user-unit homarr-upstream.service -f` while the
  board loads
- DB: `item` (kind=mediaReleases), `integration` (kind=plex),
  `integrationSecret` (kind=apiKey)

Decorative — does not block any user-facing flow.

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
- Homarr public board renders, all tiles clickable
- Seerr request flow end-to-end
- Plex streaming a recently-added title
- Calibre-Web admin login (rotated password)
- Tautulli notifications fire to Discord on Watched

Drive this from the agent prompt — see Task #19 in the session task list.

---

## Resolved 2026-05-09

| Phase | Was | Resolution |
|-------|-----|------------|
| 9.4 anime gap | Anime+Anime Movies in Jellyfin only | Added to Plex via 59-plex-anime-libraries.py; Maintainerr 60-day rules now cover all 4 Plex libs (27b-maintainerr-rules.py NAME_OVERRIDES routes anime libs to Sonarr2/Radarr2) |
| 12.2 Plex Webhook | Required Notifiarr client | Tautulli Webhook agent → Notifiarr passthrough wired (58-tautulli-notifiarr-webhook.py); covers play/scrobble events. Notifiarr client itself deferred (see above) |
| 13 Homarr boards | DONE prior session | — |
| 15 canaries | Manual UI walkthrough | Automated as scripts/canaries/{movie,anime,deletion,mobile-ux}.sh; wired into smoke-test.sh §15 |
| 16 stop apps | DESTRUCTIVE | All 7 stopped via SSH (docs/transition-log.md). Uninstall held 7 days |
| 22 Conjurr config | UI tunnel | Verified — env file fully populated (TAUTULLI_URL+KEY, GOOGLE_API_KEY, OVERSEERR_URL+KEY) |
| 23 Newsletterr config | UI tunnel | Settings populated in DB (plex, tautulli, conjurr, smtp). Template+schedule still operator UI |
| 25 Listmonk cutover | UI campaign create | 60-listmonk-cutover.py creates draft via API; --send fires it |
| 26 Ombi decom | PARKED | Ombi stopped (invites paused). Decom after Wizarr alternative |
| 29-31 Tdarr | UI library setup | 50b-tdarr-config.py adds 3 libraries (Movies/TV/Anime), worker cap 2/2, webUIPort fix |
| 34 Recyclarr no-4k | Smoke gate red | 57-no-4k-enforce.py disabled 9 2160p entries across 3 factory profiles. Gate green |
| Tautulli Notifiarr | Removed native agent | Webhook agent wired (58-tautulli-notifiarr-webhook.py) |
| Calibre-Web pw | admin/admin123 | Rotated to shared admin password via direct SQLite UPDATE (PBKDF2-SHA256) |
| Maintainerr rules | 2 (Plex-only) | 4 active (Movies, TV, Anime, Anime Movies — anime branch routed to Sonarr2/Radarr2 via NAME_OVERRIDES) |
| Tdarr `:8265` ghost | Broken redirect | webUIPort baked into Tdarr_Server_Config.json (live + 50-tdarr-install.sh) |
| Tdarr nginx subpath | Disabled fragment | Re-enabled `~/.apps/nginx/proxy.d/tdarr.conf`; SIGHUP-reloaded |
| Mylar3 `/mylar3/` | 404 | Re-enabled `~/.apps/nginx/proxy.d/mylar3.conf`; both `/mylar/` (301) and `/mylar3/` (302) resolve |
| Listmonk `/public/` assets | Hardcoded paths | nginx sub_filter rewrites href/src to `/listmonk/public/...` (43-listmonk-install.sh) |
| Lifecycle upgrade/downgrade | Phase-1 stubs | Real impl for ucc / systemd / cron / library; rollback on health failure; recovery auto-downgrade after attempt-cap |

---

## Captured already (no operator action)

- ✓ Plex token, host, port → `secrets/plex.{token,host,port}` (port via Docker 172.17.1.250:32400)
- ✓ Jellyfin API key → `secrets/jellyfin.key`
- ✓ Jellyseerr API key → `secrets/jellyseerr.key`
- ✓ All *arr API keys (Sonarr/Radarr/Sonarr2/Radarr2/Readarr/Mylar3/Prowlarr) → `secrets/<arr>.key`
- ✓ qBit credentials → `secrets/qbittorrent.{user,password}`
- ✓ Notifiarr API key → `secrets/notifiarr.key`
- ✓ Komga / Kavita / Audiobookshelf / Calibre-Web / Maintainerr / Bazarr / Tautulli API keys → `secrets/<app>.key`
- ✓ htpasswd password → `secrets/htpasswd.password` (= shared admin password)
- ✓ Listmonk API user + token → `secrets/listmonk.{api_user,api_token}`
