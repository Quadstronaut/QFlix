# QFlix migration — blue-green cutover to an Ultra.cc Gold slot

**Status:** spec approved-by-default (operator review pending) · **Date:** 2026-08-08
**Branch:** `feature/migration` · **Trigger phrase:** "migrate me"

## 0. Goal

Move QFlix from the current shared slot (`seedbox.example.com`,
2 794 G quota, **85.15 % full on 2026-08-08**) to a new **Ultra.cc Gold** slot
(22 TB HDD / 55 TB monthly upload / 50 Gbps shared), with:

- **Blue-green**: old box (blue) stays fully live until the operator flips DNS.
  Nothing on blue is destructively modified by any migration script.
- **One command day-of**: all discovery, installers, sync and validation are
  pre-built and dry-run in advance; cutover day is running a short, rehearsed
  script order, not writing code.
- **Accepted cost**: Plex on green is a NEW server identity. Members re-pin
  their favorite libraries once. (22 TB means most deleted content can simply
  be re-requested.)

## 1. Facts the design rests on (measured 2026-08-08)

| Fact | Value |
|---|---|
| Media to move | ~2.2 T (`TV Shows` 1.3 T, `Movies` 715 G, `Anime` 163 G, `Anime Movies` 45 G) |
| Quota pressure | 2 379 G / 2 794 G (85.15 %), ~40 GB/day ingest bursts |
| Stack | 35 manifest apps: 18 UCC-panel, 6 systemd, 10 cron, 1 library |
| Monitors | 78 Kuma push monitors, all born from `bootstrap-kuma-monitors.py` |
| Timers | 61 user-systemd timers live |
| Deploy model | `~/scripts` (runtime) must equal `origin/master` `scripts/` (deploy-drift canary); `~/.opt/qflix-src` is the git checkout |
| Provider | Same (Ultra.cc) — `app-<slug>` UCC wrappers, panel installs, nginx fragment model all remain valid on green |
| Gold availability | NL / Canada / Singapore; NL uniquely has a Platinum 28 TB tier above Gold (headroom without a transatlantic move) |

## 2. Decisions (defaults taken — veto any of these before saying "migrate me")

1. **Location** — operator's choice at purchase. Note: NL keeps a bigger tier
   above Gold; Canada is closer to NA viewers. Scripts don't care.
2. **Domain** — operator points `qflix.quadstronix.dev` (or a new domain) at
   green manually. Every script takes `NEW_HOST` (SSH) and reads/writes
   secrets; nothing hardcodes the new FQDN.
3. **Data that MIGRATES** (identity or history the members would miss):
   - **Seerr** — sqlite DB + settings (users, request history). Re-pointed at
     green's *arr ports and the new Plex server after restore.
   - **Tautulli** — DB (watch history feeds the newsletter's top-requesters).
   - **Listmonk** — Postgres dump (subscriber list + campaign archive).
   - ***arr × 5** — native backup zips, restored on green, then download
     clients / Prowlarr sync / ports re-pointed via API (ports WILL differ).
   - **Roster + gate state** — `members.yaml`, `state.json`,
     `declared-payers.json` copied; gate ships **disarmed** on green (see §5).
   - **Secrets** — regenerated on green by `bootstrap-discover.sh` where
     slot-specific (ports, urlbases); copied where identity (Discord webhook,
     TMDB, NZBgeek, github PAT, plex token, entitlement key/url).
4. **Data that goes FRESH**:
   - **Plex** — new server, new machineIdentifier. Libraries rebuilt from
     synced media. Members re-invited (scripted) and re-pin.
   - **qBittorrent session** — no fastresume migration. Blue keeps seeding
     until its term ends; green starts clean. (Private-tracker ratio lives
     with blue's client; nothing to preserve on green day one.)
   - **Kuma** — fresh instance; `bootstrap-kuma-monitors.py` recreates all 78
     from the manifest. **Discord notification channels stay OFF on green
     until cutover** — exactly one side may page the operator at any time.
5. **Media sync** — direct box→box `rsync -aH --partial` over SSH (blue has
   outbound SSH), multi-pass: bulk passes while blue is live, one small
   `--delta` pass during the cutover freeze. Hardlinks preserved (`-H`).
6. **Old box afterlife** — read-only grace ≥ DNS TTL after flip; operator
   cancels the slot after N green days. `60-decommission-old.md` is a
   checklist, never a script.

## 3. What blue-green means here (invariants)

- **I-1** Exactly one side sends Discord alerts, newsletters, or Seerr emails
  at any moment. Enforced by: green's Kuma channels detached + green's
  `qflix-newsletter` / `listmonk-sync` timers not enabled until cutover.
- **I-2** No migration script writes to blue except: qBit/SAB pause+resume
  (the cutover freeze and its rollback mirror), the *arr backup-trigger POST
  (one new zip under blue's own Backups folder), newsletter/listmonk timer
  disable+enable (park blue / rollback's re-enable), and the entitlement-gate
  `--execute` drop-in removal/reinstall (§5) — everything else touching blue
  is a read. Nothing on blue is deleted, ever, by these scripts.
- **I-3** Every mutating script defaults to dry-run and requires `--execute`
  (house convention: the reaper, the gate, the janitors all ship inert).
- **I-4** Every script is idempotent and resumable; a mid-run failure is
  re-run, not hand-repaired.
- **I-5** The entitlement gate is armed on at most ONE side, ever (§5).

## 4. Script inventory — `scripts/migrate/`

| # | Script | Runs against | Purpose |
|---|---|---|---|
| 00 | `00-preflight.sh` | blue | Snapshot inventory: app versions, port map, secrets list, media du, timer list, Kuma monitor list → `migration-state.json`. Read-only; runnable today. |
| 10 | `10-provision-checklist.md` | operator | Buy slot, pick location, panel-install the 18 UCC apps (list auto-generated from `manifest/apps.yaml`), add SSH key, note new host. |
| 15 | `15-bootstrap-new.sh NEW_HOST` | green | Verify SSH + panel apps present, clone repo to `~/.opt/qflix-src`, seed `~/scripts` from master, run `bootstrap-discover.sh` to build green's `secrets/`, copy identity secrets from local. |
| 20 | `20-install-stack.sh NEW_HOST` | green | Run the numbered `configure/` phases: systemd units + timers, maint daemon, vlogs, dash, nginx fragments, Kuma (channels OFF). Post-condition: green pusher pushing, zero Discord. |
| 30 | `30-sync-media.sh NEW_HOST [--delta]` | blue→green | `rsync -aH --partial --info=progress2` of `~/media` (and `~/www/images/newsletter` poster cache). Multi-pass; `--delta` variant excludes nothing and is expected to be small. |
| 35 | `35-sync-appdata.sh NEW_HOST` | blue→green | Stop target app on green → copy Seerr/Tautulli DBs, restore *arr backup zips, `pg_dump | pg_restore` Listmonk, copy roster/gate state → start apps → API re-point pass (Seerr↔*arr hostnames/ports, Prowlarr app sync, download client defs). |
| 40 | `40-validate-green.sh NEW_HOST` | green | Green smoke: every manifest app UP via its health probe, `manitoba-maint kuma audit` (78/78), canary one-shots in report mode, `qflix-entitlement.py --arm-check` (report-only), hardlink spot-check on synced media. Emits a PASS/FAIL checklist. |
| 45 | `45-plex-invites.py NEW_HOST [--execute]` | plex.tv | Enumerate blue-server friends via python-plexapi → invite the same accounts to green's server (all libraries except Welcome per roster state). Dry-run prints the exact invite list; runnable today with `--dry-run`. |
| 50 | `50-cutover.sh NEW_HOST` | both | The "migrate me" script. Order: freeze blue (pause qBit+SAB, disable *arr import lists) → `30 --delta` → `35` re-sync of DBs → `40` must PASS → attach green Kuma Discord channels + enable newsletter timers on green → disarm blue gate, arm green gate (operator confirms each) → print DNS-flip instructions + post-flip verification commands → park blue's cron/newsletter timers (Plex on blue keeps serving through DNS TTL). |
| 55 | `55-rollback.sh` | both | Mirror of 50: unfreeze blue, re-attach blue channels, mute green. DNS reverts manually. |
| 60 | `60-decommission-old.md` | operator | After N green days: export blue's final logs, cancel slot. |

## 5. The entitlement gate across the boundary

The gate mutates **Plex shares** (account-level API) and Seerr users. Shares
are per-server objects, so blue's gate governs blue's server and green's gate
green's — but two armed gates reading one roster is still two writers to one
member's experience. Rule: **green's gate ships disarmed** (the roster copy
keeps `armed: true` but green gets no `--execute` drop-in until cutover step
"disarm blue → arm green", operator-confirmed, in that order). The
`--arm-check` rehearsal is part of `40-validate-green.sh`.

## 6. Failure handling

- Any `50-cutover.sh` step failing **stops the script** with the completed
  steps named; `55-rollback.sh` returns to blue in under a minute (unfreeze +
  channels). DNS not having propagated yet is the normal case, not an error.
- rsync interruptions: `--partial` + re-run (I-4).
- Green validation failing on cutover day = no cutover; blue was never
  degraded (I-2).

## 7. Explicitly NOT built (YAGNI)

- No qBittorrent fastresume/cross-seed migration.
- No Plex watch-state/metadata migration (Tautulli keeps history for stats).
- No automated DNS (operator does the flip; script prints and verifies).
- No dual-write or proxy period between the boxes.
- No automation of the Ultra.cc panel (it has no API; `10-` is a checklist).

## 8. Timeline expectation

Bulk media (~2.2 T) at a realistic sustained 40–80 MB/s box→box: **8–16 h**
per full pass; run twice while blue is live. Cutover-day freeze (delta sync +
appdata + validate): well under 1 h. Member-visible gap: zero on blue; green
becomes canonical when DNS lands.
