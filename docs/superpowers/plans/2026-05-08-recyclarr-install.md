# Recyclarr Install Plan (Manitoba)

> **For agentic workers:** Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax.

**Goal:** Install Recyclarr to keep Sonarr / Radarr quality profiles + custom format scoring synchronized with the [TRaSH guides](https://trash-guides.info/), capped at **1080p** per the no-4k policy.

**Architecture:** Single self-contained .NET 8 binary (`recyclarr-linux-x64`), no runtime install needed. Driven by `~/.apps/recyclarr/config/recyclarr.yml`. Run as a `systemd --user` *timer* (weekly, low priority) — **not** a long-running daemon. No web UI, no nginx, no port. Talks to Sonarr/Radarr via existing API keys in `secrets/`.

**Why Recyclarr:** Custom formats are how you tell Sonarr/Radarr "prefer this release group", "downgrade these encodes", "skip these audio profiles". TRaSH guides curate these for the community. Maintaining them by hand is tedious + drifts. Recyclarr syncs them in seconds and pins to a guide commit so changes are reproducible.

**Non-goals:**
- Maintaining custom formats outside the TRaSH guide ecosystem (operator can layer additional CFs but the *baseline* is TRaSH).
- 4K profile sync (**explicitly excluded** — see `feedback_no-4k-profiles.md`).
- Lidarr/Readarr/Whisparr (Recyclarr supports Sonarr + Radarr only).
- Bypassing the operator's existing manual tweaks — Recyclarr only touches what's listed in the YAML; everything else stays.

---

## Probe findings (verified 2026-05-08, applicable to this plan)

| Fact | Value | Source |
|---|---|---|
| Sonarr instances | `sonarr` (TV) + `sonarr2` (Anime) — keys + ports + urlbase in `secrets/`. | existing audit |
| Radarr instances | `radarr` (Movies) + `radarr2` (Movies-4K? **verify**) — keys + ports + urlbase in `secrets/`. **Per no-4k policy, `radarr2` should NOT be a 4K instance.** Confirm before sync. | existing audit |
| Recyclarr release | v8.6.0 latest stable as of 2026-04-26; pin via `secrets/recyclarr.version`. | [Recyclarr GitHub releases](https://github.com/recyclarr/recyclarr/releases) |
| Linux asset | `recyclarr-linux-x64.tar.xz` self-contained (no .NET runtime needed). | release page |
| TRaSH guide pinning | Recyclarr 8.x pins by guide commit hash automatically — output reproducible. | Recyclarr docs |
| Public exposure | None — CLI tool only. No port allocation. No nginx. | Recyclarr docs |

## Conventions

- `SSHM` = `ssh -o BatchMode=yes quadstronaut@seedbox.example.com`. Defined in `scripts/lib/ssh.sh`.
- All installs idempotent.
- **Pin versions.** Recyclarr binary version pinned in `secrets/recyclarr.version` (default: latest at first run, captured into the secret).
- All commits include `Co-Authored-By: Claude Opus 4.7`.
- **No 4K.** Per `feedback_no-4k-profiles.md`: every quality_profiles block in `recyclarr.yml` caps at 1080p. Operator can per-case escalate later, but the default config NEVER includes UHD profiles.

---

## Phase 34 — Recyclarr install

### Task 34.1: Confirm radarr2 / sonarr2 are NOT 4K (operator verification)

Before drafting the YAML, confirm what each *arr instance is for. Per the no-4k policy, neither `radarr2` nor `sonarr2` should be a 4K instance. Most likely:

- `sonarr` → English TV
- `sonarr2` → Anime
- `radarr` → English Movies
- `radarr2` → ??? (Foreign? Documentaries? Music videos? **Verify with operator before sync.**)

- [ ] **Step 1: Operator reviews each *arr instance's quality profiles in the Web UI.** If any UHD/2160p profile is currently active, operator decides:
  - Demote it to 1080p (recommended per policy).
  - Or document the per-case escalation in `docs/operator-deferred.md` and exclude that instance from Recyclarr sync.

- [ ] **Step 2: Capture the resolved-purpose-per-instance into `secrets/`** (so future runs don't have to re-ask):

```bash
secret_write sonarr.purpose "TV-1080p"
secret_write sonarr2.purpose "Anime-1080p"
secret_write radarr.purpose "Movies-1080p"
secret_write radarr2.purpose "<operator: Movies-Foreign-1080p? skip-from-recyclarr?>"
```

### Task 34.2: Download + install binary

**Files:**
- Create: `scripts/configure/56-recyclarr-install.sh`

- [ ] **Step 1: Pin version (default: latest stable)**

```bash
if ! secret_exists recyclarr.version; then
  TAG=$(curl -fsSL https://api.github.com/repos/recyclarr/recyclarr/releases/latest | grep -oP '"tag_name":\s*"\K[^"]+')
  [ -n "$TAG" ] || die "could not resolve Recyclarr latest tag"
  secret_write recyclarr.version "$TAG"
fi
RVER=$(secret_read recyclarr.version)
RVER_NUM="${RVER#v}"
```

- [ ] **Step 2: Download + install**

```bash
sshm 'bash -s' <<EOF
set -euo pipefail
mkdir -p ~/.apps/recyclarr/{bin,config,logs,cache}
cd ~/.apps/recyclarr
TARBALL="recyclarr-linux-x64.tar.xz"
URL="https://github.com/recyclarr/recyclarr/releases/download/${RVER}/recyclarr-linux-x64.tar.xz"
if [ ! -x bin/recyclarr ] || ! ./bin/recyclarr --version 2>/dev/null | grep -q "${RVER_NUM}"; then
  curl -fsSL "\$URL" -o "\$TARBALL"
  tar -xf "\$TARBALL" -C bin --strip-components=0
  chmod +x bin/recyclarr
  rm "\$TARBALL"
fi
./bin/recyclarr --version
EOF
```

### Task 34.3: Generate `recyclarr.yml` (1080p-capped)

**Files:**
- Config: `~/.apps/recyclarr/config/recyclarr.yml`

- [ ] **Step 1: Render config**

```bash
SONARR_KEY=$(secret_read sonarr.key)
SONARR_PORT=$(secret_read sonarr.port)
SONARR_BASE=$(secret_read sonarr.urlbase)

SONARR2_KEY=$(secret_read sonarr2.key)
SONARR2_PORT=$(secret_read sonarr2.port)
SONARR2_BASE=$(secret_read sonarr2.urlbase)

RADARR_KEY=$(secret_read radarr.key)
RADARR_PORT=$(secret_read radarr.port)
RADARR_BASE=$(secret_read radarr.urlbase)

RADARR2_KEY=$(secret_read radarr2.key)
RADARR2_PORT=$(secret_read radarr2.port)
RADARR2_BASE=$(secret_read radarr2.urlbase)

sshm "cat > ~/.apps/recyclarr/config/recyclarr.yml" <<YAML
# Recyclarr config — Manitoba.
# 1080p cap enforced everywhere per feedback_no-4k-profiles.md.
# DO NOT add a UHD/2160p quality_profiles block here without operator approval.

sonarr:
  tv:
    base_url: http://127.0.0.1:${SONARR_PORT}/${SONARR_BASE}
    api_key: ${SONARR_KEY}
    quality_definition:
      type: series
    quality_profiles:
      - name: WEB-1080p
        reset_unmatched_scores:
          enabled: true
        upgrade:
          allowed: true
          until_quality: WEB-1080p
          until_score: 10000
        min_format_score: 0
        score_set: default
    include:
      - template: sonarr-quality-definition-series
      - template: sonarr-v4-quality-profile-web-1080p
      - template: sonarr-v4-custom-formats-web-1080p
    custom_formats:
      - trash_ids:
          # Unwanted
          - 32b367365e7add525b2dc2f3258a1adb  # MPEG2
          - 82d40da2bc6923f41e14394075dd4b03  # No-RlsGroup
          - e1a997ddb54e3ecbfe06341ad323c458  # Obfuscated
        assign_scores_to:
          - name: WEB-1080p

  anime:
    base_url: http://127.0.0.1:${SONARR2_PORT}/${SONARR2_BASE}
    api_key: ${SONARR2_KEY}
    quality_definition:
      type: anime
    quality_profiles:
      - name: Anime-1080p
        reset_unmatched_scores:
          enabled: true
        upgrade:
          allowed: true
          until_quality: WEB-1080p
          until_score: 10000
        min_format_score: 100
    include:
      - template: sonarr-quality-definition-anime
      - template: sonarr-v4-quality-profile-anime
      - template: sonarr-v4-custom-formats-anime

radarr:
  movies:
    base_url: http://127.0.0.1:${RADARR_PORT}/${RADARR_BASE}
    api_key: ${RADARR_KEY}
    quality_definition:
      type: movie
    quality_profiles:
      - name: HD-1080p
        reset_unmatched_scores:
          enabled: true
        upgrade:
          allowed: true
          until_quality: Bluray-1080p
          until_score: 10000
        min_format_score: 0
    include:
      - template: radarr-quality-definition-movie
      - template: radarr-quality-profile-hd-1080p
      - template: radarr-custom-formats-hd-1080p
    custom_formats:
      - trash_ids:
          - 9c11cd3f07101cdba90a2d81cf0e56b4  # LQ
          - e6886871085226c3da1830830146846c  # Generated Dynamic HDR
          - 90a6f9a8e1a45a78da9c1d9b3a1e5e89  # No-RlsGroup
        assign_scores_to:
          - name: HD-1080p

  # radarr2 — operator must confirm purpose. Default: COMMENTED OUT until reviewed.
  # Uncomment + set the right templates if radarr2 is e.g. Foreign movies.
  #
  # foreign:
  #   base_url: http://127.0.0.1:${RADARR2_PORT}/${RADARR2_BASE}
  #   api_key: ${RADARR2_KEY}
  #   quality_definition:
  #     type: movie
  #   quality_profiles:
  #     - name: HD-1080p-Foreign
  #       upgrade: { allowed: true, until_quality: Bluray-1080p, until_score: 10000 }
  #   include:
  #     - template: radarr-quality-definition-movie
  #     - template: radarr-quality-profile-hd-1080p
  #     - template: radarr-custom-formats-hd-1080p
YAML
chmod 600 ~/.apps/recyclarr/config/recyclarr.yml
```

The TRaSH IDs (e.g. `9c11cd3f07101cdba90a2d81cf0e56b4`) are stable identifiers from the TRaSH guide repo — Recyclarr looks them up at sync time. The list above is a minimal "block known-bad releases" baseline; expand per operator taste.

### Task 34.4: Dry-run sync (operator review before commit)

- [ ] **Step 1: `recyclarr sync --preview`**

```bash
sshm 'cd ~/.apps/recyclarr && ./bin/recyclarr sync --preview --config config/recyclarr.yml' | tee /tmp/recyclarr-preview.log
```

The `--preview` flag prints the planned changes without applying. Operator reviews:
- Quality profile names — do they match the profile names already in Sonarr/Radarr? If not, Recyclarr will *create* new profiles, not edit existing.
- Custom format scores — sane direction (positive for desired, negative for blocked)?
- Quality definition (size/MB) — operator's existing settings get overwritten if `quality_definition:` is present.

- [ ] **Step 2: Operator says yes → real sync**

```bash
sshm 'cd ~/.apps/recyclarr && ./bin/recyclarr sync --config config/recyclarr.yml' 2>&1 | tee ~/.apps/recyclarr/logs/first-sync.log
```

### Task 34.5: User-systemd timer (weekly Sunday 04:30)

**Files:**
- Service: `~/.config/systemd/user/recyclarr.service`
- Timer: `~/.config/systemd/user/recyclarr.timer`

- [ ] **Step 1: Service unit**

```bash
sshm "cat > ~/.config/systemd/user/recyclarr.service" <<'UNIT'
[Unit]
Description=Recyclarr TRaSH-guide sync
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/.apps/recyclarr
ExecStart=%h/.apps/recyclarr/bin/recyclarr sync --config %h/.apps/recyclarr/config/recyclarr.yml
Nice=15
StandardOutput=append:%h/.apps/recyclarr/logs/recyclarr.log
StandardError=append:%h/.apps/recyclarr/logs/recyclarr.err
UNIT
```

- [ ] **Step 2: Timer unit (weekly)**

```bash
sshm "cat > ~/.config/systemd/user/recyclarr.timer" <<'UNIT'
[Unit]
Description=Recyclarr weekly sync

[Timer]
OnCalendar=Sun *-*-* 04:30:00
RandomizedDelaySec=1800
Persistent=true
Unit=recyclarr.service

[Install]
WantedBy=timers.target
UNIT
sshm 'systemctl --user daemon-reload && systemctl --user enable --now recyclarr.timer'
```

Weekly is fine. TRaSH guide updates land on a multi-day cadence; daily is overkill. Sundays 04:30 + 30min jitter avoids overlap with Kometa (03:30 daily) and the *arr stack's own nightly maintenance.

- [ ] **Step 3: Verify**

```bash
sshm 'systemctl --user list-timers recyclarr.timer --no-pager' | grep -q recyclarr.timer || die "recyclarr.timer not scheduled"
```

---

## Smoke test additions

Add two new tests to `scripts/smoke-test.sh`:

```bash
# 23. Recyclarr timer scheduled
echo "23. Recyclarr timer"
RT=$(sshm "systemctl --user list-timers recyclarr.timer --no-pager 2>/dev/null | grep -c recyclarr.timer")
if [ "${RT:-0}" -ge 1 ]; then
  record "recyclarr-timer" pass "scheduled"
else
  record "recyclarr-timer" fail "timer not scheduled"
fi

# 24. Recyclarr last sync result + sanity (no UHD/2160p in synced profiles — enforces no-4k policy)
echo "24. Recyclarr no-4k policy"
UHD_COUNT=0
for app in sonarr sonarr2 radarr radarr2; do
  KEY=$(secret_read $app.key 2>/dev/null || echo "")
  PORT=$(secret_read $app.port 2>/dev/null || echo "")
  BASE=$(secret_read $app.urlbase 2>/dev/null || echo $app)
  [ -z "$KEY" ] && continue
  V=v3; [ "${app#sonarr}" != "$app" ] || V=v3
  N=$(sshm "curl -sf -m 10 -H 'X-Api-Key: $KEY' http://127.0.0.1:$PORT/$BASE/api/$V/qualityprofile 2>/dev/null | python3 -c 'import sys,json;print(sum(1 for p in json.load(sys.stdin) for i in p.get(\"items\",[]) if i.get(\"allowed\") and \"2160\" in i.get(\"quality\",{}).get(\"name\",\"\")))'")
  UHD_COUNT=$((UHD_COUNT + ${N:-0}))
done
if [ "$UHD_COUNT" = 0 ]; then
  record "recyclarr-no-4k" pass "no UHD profiles enabled (per policy)"
else
  record "recyclarr-no-4k" fail "$UHD_COUNT UHD entries found — policy violation"
fi
```

Test 24 is the **policy gate** — even if the operator manually flips on a UHD profile in Sonarr/Radarr, smoke catches it and forces a per-case escalation conversation.

---

## Rollback per phase

| Phase | If broken |
|---|---|
| 34 (Install) | `systemctl --user disable --now recyclarr.timer recyclarr.service && rm -rf ~/.apps/recyclarr ~/.config/systemd/user/recyclarr.{service,timer} && systemctl --user daemon-reload`. **Important:** custom formats / quality profiles already pushed to Sonarr/Radarr REMAIN. Recyclarr does not auto-revert. To remove its handiwork: in each *arr Web UI, delete the `Recyclarr` tag from custom formats and delete profiles named `WEB-1080p` / `HD-1080p` / `Anime-1080p` (or whichever Recyclarr created). |
| Bad sync | If Recyclarr sync put a *arr in a weird state: `cd ~/.apps/recyclarr && ./bin/recyclarr sync --debug --config config/recyclarr.yml 2>&1 | tee /tmp/debug.log`. The Recyclarr Discord (#recyclarr channel) is the authoritative help channel. |

---

## Cost summary

- **Recyclarr**: $0 (MIT-adjacent open source).
- **Disk**: <50 MB (binary + cache + logs).
- **CPU**: ~30 sec/week.
- **Operator effort**: 30 min one-time review of the dry-run output, then ~quarterly check that nothing's drifted unexpectedly.

---

## What this plan does NOT do

- **No 4K profile management.** This is the policy headline.
- **No Recyclarr-managed *arr settings outside quality + CFs** (no naming format sync, no media management sync — those stay manual).
- **No Lidarr / Readarr support.** Recyclarr only handles Sonarr + Radarr.
- **No automatic re-search on profile change.** When Recyclarr changes a custom format score, *arr won't automatically re-grab existing files. Use the *arr Web UI or `Upgradinatorr` (cherry-picked separately) to trigger.

---

## Total scope

- **1 install script** (`scripts/configure/56-recyclarr-install.sh`)
- **1 binary download** (Recyclarr linux-x64 self-contained)
- **1 user-systemd service** (`recyclarr.service`, oneshot)
- **1 user-systemd timer** (`recyclarr.timer`, weekly Sun 04:30)
- **0 nginx fragments** (no Web UI)
- **0 ports claimed**
- **2 new smoke tests** (`recyclarr-timer`, `recyclarr-no-4k`)
- **0 secrets committed.** *arr keys reused — all in gitignored `secrets/`.

Estimated install time: ~20 min including the dry-run review.

---

## Open decisions (operator)

1. **What is `radarr2`?** This is the gating decision — the plan template leaves the `radarr2` block commented out until operator clarifies (Foreign? Documentaries? 4K-but-promoted-to-1080p? Drop it?).
2. **Custom format additions beyond TRaSH baseline.** Operator can layer on top — e.g. release-group-specific scoring favoring/blocking specific groups.
3. **Quality definitions** — Recyclarr writes recommended size limits per quality. Operator can override in the YAML if Manitoba-specific seeding constraints make the defaults wrong.
4. **`reset_unmatched_scores: true`** — currently set, meaning Recyclarr-unknown CFs get score 0. Set `false` if operator wants to manually score CFs Recyclarr doesn't know about.

---

## Execution protocol

### Step 0 — Pre-execution check

- [ ] Operator confirmed `radarr2` purpose (or agreed to skip it from sync — block in YAML stays commented).
- [ ] All `*arr` instances verified to NOT have UHD/2160p profiles enabled (per no-4k policy). Demote or document escalation in `docs/operator-deferred.md`.
- [ ] Working tree clean.

### Step 1 — Credential pre-flight (consolidated, one-time)

| Cred | Used by (this plan) | Likely already exists? |
|---|---|---|
| `sonarr.key`, `sonarr.port`, `sonarr.urlbase` | Phase 34 sync | **Yes** |
| `sonarr2.key`, `sonarr2.port`, `sonarr2.urlbase` | Phase 34 sync | **Yes** |
| `radarr.key`, `radarr.port`, `radarr.urlbase` | Phase 34 sync | **Yes** |
| `radarr2.*` | Phase 34 sync (gated — see open decision) | Yes — but skipped until clarified |
| `recyclarr.version` | Phase 34 install | No — captured at first install (default v8.6.0) |

No hard blockers — all `*arr` keys already exist.

**Browser policy:** No browser steps. Phase 34 is 100% CLI.

### Step 2 — Implementation phases

Continuous execution per `feedback_continuous-execution-preferred.md`: don't ask for per-phase approval. Pause only on genuine blockers.

Phases in this plan:
- **Phase 34** — Recyclarr install + 1080p-capped sync + weekly Sun 04:30 timer (Tasks 34.1-34.5)

Single-phase plan; one commit at the end.

### Step 3 — Self-check (after Phase 34)

1. Run `scripts/smoke-test.sh` — `recyclarr-timer` must pass; **`recyclarr-no-4k` is the policy gate** — must report 0 UHD entries across all `*arr` instances.
2. `git status` — clean (1 new commit).
3. Re-run smoke twice. The no-4k policy gate must remain stable.
4. Manually inspect each `*arr`'s Quality Profiles tab in the Web UI — confirm no UHD entries are `allowed`.

### Step 4 — Log audit

1. `journalctl --user -u recyclarr.service --since "today" -p err`
2. `~/.apps/recyclarr/logs/{recyclarr.log,recyclarr.err,first-sync.log}` — `grep -E 'ERROR|FATAL|Traceback|fail|invalid'`
3. Each `*arr`'s System → Logs in the Web UI for unexpected profile/CF deletions caused by `reset_unmatched_scores: true`

Classify each error:
- **Cosmetic** (e.g. "Custom format X not in TRaSH guide — score reset to 0") — expected per `reset_unmatched_scores`, note only.
- **Actionable** (config typo, wrong template name) — fix, re-sync, re-audit.
- **Blocking** (any UHD entry detected by smoke) — stop, surface immediately.

### Step 5 — Final summary template

```
# Recyclarr implementation
- Phases completed: 34
- Scripts added: 1 install (56-recyclarr-install.sh)
- Configs: ~/.apps/recyclarr/config/recyclarr.yml (1080p-capped)
- Smoke: N/N pass (was M/M before)

# Self-check results
- recyclarr-no-4k policy gate: pass / fail
- All 4 *arr instances showing TRaSH-managed profiles: yes/no

# Log audit
- Cosmetic: [list]
- Actionable (fixed): [list]
- Blockers requiring operator: [list, or "none"]

# Follow-up parking lot
- radarr2 purpose clarification + uncomment block (operator)
- Operator-layered custom formats beyond TRaSH baseline
```

### Hard rules (non-negotiable)

- **No 4K** anywhere — Recyclarr profiles, Kometa overlays, transcoder hints, request quotas. 1080p ceiling. (per `feedback_no-4k-profiles.md`)
- **Pin every version** — never `latest`, never `main`. Surface pinned versions in `versions.env` at repo root for the future updater. (per `feedback_pin-app-versions.md`)
- **Plex-primary** in all media-server config; Jellyfin gets parity only where the feature explicitly serves trial users. (per `project_plex-primary-jellyfin-trial.md`)
- **Reuse `secrets/htpasswd.password`** for any new admin-facing self-hosted app. (per `feedback_shared-admin-password.md`)
- **Read ports from `~/.apps/nginx/proxy.d/<app>.conf`** at runtime, not config.xml. (per `project_manitoba-network-model.md`)
- **Continuous execution** — no per-phase approval. Pause only on missing creds, smoke failure, or blocking log errors. (per `feedback_continuous-execution-preferred.md`)
- **Browser is last resort** — CLI/API everywhere; defer manual browser steps to `docs/operator-deferred.md`; Playwright authorized only when no alternative exists.
- **Modern AI-augmented preferred** when there's a choice; willing to fork if upstream stalls. (per `feedback_modern-ai-augmented-apps.md`)

### Failure modes to avoid (this plan)

- **Never run `recyclarr sync` without `--preview` first** — `reset_unmatched_scores: true` will overwrite operator-tweaked CF scores irreversibly. Phase 34.4 Step 1 is the gate.
- **Never sync `radarr2` while its purpose is unclarified** — keep the YAML block commented. Wrong-purpose sync = polluted profiles + a manual restore from Radarr backup.
- **Never enable a UHD/2160p quality profile** anywhere — the `recyclarr-no-4k` smoke is the regression gate. Operator escalations go through `docs/operator-deferred.md` per-case, not policy-flips.
- **Never commit `recyclarr.yml`** — it embeds API keys. The plan generates it at install time on the seedbox; only the install script lives in git.
- Don't bypass dry-run review.
- Don't commit secrets — gitignored.
