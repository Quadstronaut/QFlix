# TitleCardMaker Install Plan (Manitoba) — **PARKED 2026-05-08**

> **PARKED.** Pre-flight check failed: ImageMagick is not installed on the Ultra.cc seedbox (`convert: command not found`). The plan called this out as a hard prerequisite. Without sudo, user-space ImageMagick build is significant work (~30-60 min compile of MagickCore, MagickWand + delegate libs); file an Ultra.cc support ticket for the package install instead.
>
> Operator decision (2026-05-08): defer until ImageMagick is available. Phase 39 (operator-curated series.yml + per-show font/style picking) inherits the parked state.

> **For agentic workers:** Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax.

**Goal:** Install TitleCardMaker (TCM) v1.16.0 to generate per-episode title cards (1920×1080 images with episode title + season/episode label overlaid on a source frame) for the Manitoba Plex library. Cards display in Plex's TV episode pickers and make show browsing visually richer for the country-folk + boomer audience.

**Architecture:** Python CLI driven by YAML config, run as a `systemd --user` *timer* (daily 05:30, after Kometa). Stays on v1.16.0 (`stable` branch — last v1 release 2024-06-03) — **not** v2.x, which is sponsor-only WUI in beta. No web UI, no nginx, no port. Calls TMDb / TVDB / Plex APIs to gather metadata + source frames; uses ImageMagick to render cards; writes finished images into Kometa's `asset_directory` so Kometa picks them up on its next run (= one assets-pipeline, two tools).

**Why TitleCardMaker (not just Kometa):** Kometa overlays add badges to *posters*. TCM generates *episode title cards* — visually distinct, addresses a different surface (the episode picker, not the show poster). Together they're a complete cosmetic pass.

**Non-goals:**
- v2.x web UI (sponsor-only — skipped).
- Movies (TCM technically supports them, but Movies don't render title cards in Plex's UI; pointless).
- Anime — separate metadata strategy, separate library, defer.
- Per-language card generation (English-only audience).

---

## Probe findings (verified 2026-05-08, applicable to this plan)

| Fact | Value | Source |
|---|---|---|
| Public exposure | **None** — CLI only in v1.x. v2 has a Web UI but is sponsor-only. | TitleCardMaker docs |
| Python | System Python 3.10+ available; isolate in venv. v1.16.0 supports 3.9-3.12. | `python3 --version` |
| ImageMagick | **Required.** Pre-flight check: `convert --version` must succeed on the seedbox. Ultra.cc ships ImageMagick system-wide; verify before install. | manual probe |
| Plex token + URL | Already in `secrets/plex.token` + `secrets/plex.host` + `secrets/plex.port`. | existing |
| TMDb API key | Required. Reuses `secrets/tmdb.api_key` (created for Kometa). | shared |
| TVDB v4 key | Optional; skip unless operator wants TVDB-sourced metadata in addition to TMDb. | TCM docs |
| Disk headroom | ~20 GB worst-case (cards + source frame cache) on 9T free. Trivial. | TCM community + Tdarr probe |
| Asset directory | TCM writes into `~/.apps/kometa/config/assets/` so Kometa picks up the cards via `assets_for_all: true`. **Single shared directory eliminates duplicate disk usage.** | Kometa plan |
| Schedule | Daily 05:30 (after Kometa 03:30+jitter completes). | scheduling design |

## Conventions

- `SSHM` = `ssh -o BatchMode=yes quadstronaut@seedbox.example.com`. Defined in `scripts/lib/ssh.sh`.
- All installs idempotent.
- **Pin versions.** TitleCardMaker pinned to `v1.16.0` per `feedback_pin-app-versions.md`. Capture in `secrets/titlecardmaker.version`.
- **Reuse Kometa's TMDb key + asset directory.** Two tools, one assets pipeline.
- All commits include `Co-Authored-By: Claude Opus 4.7`.

---

## Phase 38 — TitleCardMaker install

### Task 38.1: Verify ImageMagick + pin version (pre-req)

**Files:**
- Create: `scripts/configure/58-titlecardmaker-install.sh`

- [ ] **Step 1: Pre-flight ImageMagick check**

```bash
sshm 'convert --version 2>&1 | head -1' || die "ImageMagick missing — TitleCardMaker cannot run"
```

If this fails, file an Ultra.cc support ticket asking for ImageMagick. Do NOT proceed with TCM install until `convert` works.

- [ ] **Step 2: Pin version**

```bash
if ! secret_exists titlecardmaker.version; then
  secret_write titlecardmaker.version "v1.16.0"  # last v1 stable; v2 is WUI sponsor-only
fi
```

- [ ] **Step 3: Verify Kometa is installed first**

TCM writes into Kometa's `asset_directory`. Kometa install must be complete (Phase 32) before TCM install starts:

```bash
sshm 'test -d ~/.apps/kometa/config/assets' || die "Kometa Phase 32 must be complete before TCM install"
```

### Task 38.2: Clone + venv + dependencies

- [ ] **Step 1: Install**

```bash
TVER=$(secret_read titlecardmaker.version)

sshm 'bash -s' <<EOF
set -euo pipefail
mkdir -p ~/.apps/titlecardmaker
cd ~/.apps/titlecardmaker
if [ ! -d titlecardmaker ]; then
  git clone --branch "$TVER" --depth 1 https://github.com/CollinHeist/TitleCardMaker.git titlecardmaker
else
  cd titlecardmaker
  git fetch --tags --depth 1 origin "$TVER:refs/tags/$TVER" || true
  git checkout "$TVER"
  cd ..
fi

if [ ! -d venv ]; then
  python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip wheel >/dev/null
./venv/bin/pip install --no-cache-dir -r titlecardmaker/requirements.txt >/dev/null

mkdir -p ~/.apps/titlecardmaker/{config,logs,source-cache}
EOF
```

### Task 38.3: Render preferences.yml

**Files:**
- Config: `~/.apps/titlecardmaker/config/preferences.yml`
- Series config: `~/.apps/titlecardmaker/config/series.yml`

- [ ] **Step 1: Render preferences.yml**

```bash
PLEX_TOKEN=$(secret_read plex.token)
PLEX_HOST=$(secret_read plex.host)
PLEX_PORT=$(secret_read plex.port)
TMDB_KEY=$(secret_read tmdb.api_key)

sshm "cat > ~/.apps/titlecardmaker/config/preferences.yml" <<YAML
# TitleCardMaker preferences — generated by scripts/configure/58-titlecardmaker-install.sh.
# Reference: https://github.com/CollinHeist/TitleCardMaker/wiki

options:
  source: tmdb
  series: titlecardmaker/config/series.yml
  card_type: standard
  card_dimensions: 1920x1080
  filename_format: "{name} ({year}) - S{season:02}E{episode:02}"
  card_extension: jpg
  validate_fonts: false
  zero_pad_seasons: false
  archive: true
  archive_directory: titlecardmaker/config/archive
  use_magick_prefix: false  # Ultra.cc uses unprefixed 'convert'

archive:
  archive_all_variations: false
  summary: false

style:
  watched: unique
  unwatched: unique

plex:
  url: http://${PLEX_HOST}:${PLEX_PORT}
  token: ${PLEX_TOKEN}
  verify_ssl: false
  integrate_with_pmm: true   # honor PMM/Kometa label conventions
  integrate_with_kometa: true

tmdb:
  api_key: ${TMDB_KEY}
  minimum_resolution: 1280x720
  skip_localized_images: true
  language_priority:
    - en

# Output cards into Kometa's asset directory so Kometa picks them up next run.
# Single shared assets pipeline.
output:
  base_directory: /home/quadstronaut/.apps/kometa/config/assets
  layout: kometa
YAML
chmod 600 ~/.apps/titlecardmaker/config/preferences.yml
```

- [ ] **Step 2: Drop empty series.yml stub**

```bash
sshm 'echo "series: []" > ~/.apps/titlecardmaker/config/series.yml'
```

Operator populates `series.yml` in Phase 39 with the show list to render cards for. Until then, TCM has nothing to do (graceful no-op).

### Task 38.4: Smoke run before scheduling

- [ ] **Step 1: TCM dry-run with `--no-render`**

```bash
sshm 'cd ~/.apps/titlecardmaker && ./venv/bin/python titlecardmaker/main.py --preferences config/preferences.yml --no-render 2>&1 | tee ~/.apps/titlecardmaker/logs/first-run.log | tail -30'
```

A successful dry-run prints "Detected X series" (where X is from series.yml — 0 acceptable for a fresh install) and exits 0. Common failures:
- ImageMagick missing → see Phase 38.1.
- TMDb 401 → check `tmdb.api_key`.
- Plex token expired → re-run audit Phase 5.

### Task 38.5: User-systemd timer (daily 05:30)

**Files:**
- Service: `~/.config/systemd/user/titlecardmaker.service`
- Timer: `~/.config/systemd/user/titlecardmaker.timer`

- [ ] **Step 1: Service unit (oneshot)**

```bash
sshm "cat > ~/.config/systemd/user/titlecardmaker.service" <<'UNIT'
[Unit]
Description=TitleCardMaker daily render
After=network-online.target kometa.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/.apps/titlecardmaker
ExecStart=%h/.apps/titlecardmaker/venv/bin/python %h/.apps/titlecardmaker/titlecardmaker/main.py --preferences %h/.apps/titlecardmaker/config/preferences.yml --runs 1
Nice=15
IOSchedulingClass=idle
StandardOutput=append:%h/.apps/titlecardmaker/logs/tcm.log
StandardError=append:%h/.apps/titlecardmaker/logs/tcm.err
UNIT
```

`Nice=15` + `IOSchedulingClass=idle` keep TCM polite. `--runs 1` is one render pass per timer tick (TCM has its own continuous-run mode but we prefer timer-driven).

- [ ] **Step 2: Timer unit**

```bash
sshm "cat > ~/.config/systemd/user/titlecardmaker.timer" <<'UNIT'
[Unit]
Description=TitleCardMaker daily run

[Timer]
OnCalendar=*-*-* 05:30:00
RandomizedDelaySec=900
Persistent=true
Unit=titlecardmaker.service

[Install]
WantedBy=timers.target
UNIT
sshm 'systemctl --user daemon-reload && systemctl --user enable --now titlecardmaker.timer'
```

05:30 + 15min jitter places TCM well after Kometa's 03:30+jitter window. Kometa pushes assets first; TCM adds episode cards on top; Plex sees both.

- [ ] **Step 3: Verify timer scheduled**

```bash
sshm 'systemctl --user list-timers titlecardmaker.timer --no-pager' | grep -q titlecardmaker.timer || die "titlecardmaker.timer not scheduled"
```

---

## Phase 39 — Operator: pick shows + first-run vigilance

This phase is operator-driven YAML editing of `series.yml` and watching the first full render finish.

### Task 39.1: Populate series.yml

- [ ] **Step 1: Decide which shows get cards**

Recommendation: **start small.** Pick 5-10 favorite shows for the first run. Verify cards render correctly + look good in Plex before expanding to the full TV library.

```yaml
# ~/.apps/titlecardmaker/config/series.yml
series:
  - "Breaking Bad (2008)":
      year: 2008
      style:
        watched: unique
        unwatched: unique
  - "The Bear (2022)":
      year: 2022
  - "Yellowstone (2018)":
      year: 2018
```

- [ ] **Step 2: Manual trigger first render**

```bash
sshm 'systemctl --user start titlecardmaker.service && journalctl --user -u titlecardmaker.service -f --no-pager' | head -200
```

First render of 5-10 shows takes ~15-30 min depending on episode count. Operator watches log for errors.

### Task 39.2: Visually verify in Plex

- [ ] **Step 1:** Open Plex Web UI → one of the shows TCM rendered → episode picker. Cards should display the new title cards (clearly distinct from Plex's default which is just a thumbnail with no title text).
- [ ] **Step 2:** If cards don't show, check:
  - Kometa's `assets_for_all: true` is set (Phase 32).
  - Trigger a Kometa run (`systemctl --user start kometa.service`) — Kometa is what actually applies the assets to Plex; TCM only generates them.
  - Confirm Plex token has write access (re-audit Phase 5).
- [ ] **Step 3: Iterate** — if a show's cards look bad (wrong style, wrong source frames, ugly fonts), tweak that show's `series.yml` block (override style, change card_type to `roman` or `cutout`, etc.). TCM is heavily themable.

### Task 39.3: Expand to full TV library

Once 5-10 shows are dialed in, expand `series.yml` to the full TV library. Two paths:

- **Manual** — list every show by name + year (tedious for a large library).
- **Sync from Plex** — TCM has a `--sync-libraries-from-plex` flag that auto-populates series.yml from a Plex library:

```bash
sshm 'cd ~/.apps/titlecardmaker && ./venv/bin/python titlecardmaker/main.py --preferences config/preferences.yml --sync-libraries-from-plex "TV Shows"'
```

This appends every show in the Plex "TV Shows" library to series.yml. Operator can then prune shows they don't want cards for (e.g. anime, kids' content).

### Task 39.4: First-full-library-run vigilance

- [ ] **Step 1: Off-peak start** — kick off the full-library render at ~22:00 local Sunday. Estimated time at 1 core: ~1-2 hours for 100 shows × 10-20 episodes/show.
- [ ] **Step 2: Watch CPU + IO** — `top` / `iotop` should show ImageMagick spikes but never sustained 100% CPU (we set `Nice=15`). If sustained, pause via `systemctl --user stop titlecardmaker.service` and lower the queue (operator can split series.yml in half + run two days).
- [ ] **Step 3: Watch TMDb rate limits** — TMDb's free tier is 40 requests / 10 sec. TCM is well-behaved but a full library run can graze the limit. Errors in `~/.apps/titlecardmaker/logs/tcm.err` will mention 429 if so.
- [ ] **Step 4: Ultra.cc fair-use** — same as Tdarr Phase 30 — daily check of support inbox for the first week post-full-library-run. Steady-state is minimal CPU.

### Task 39.5: Post-first-run cleanup

- [ ] **Step 1:** Verify card count in Kometa's asset dir matches expected:

```bash
sshm 'find ~/.apps/kometa/config/assets/TV\ Shows -name "*.jpg" -o -name "*.png" | wc -l'
```

- [ ] **Step 2:** Spot-check 5 random shows in Plex Web UI — episode pickers should show TCM-generated cards.
- [ ] **Step 3:** Note disk usage post-run:

```bash
sshm 'du -sh ~/.apps/kometa/config/assets ~/.apps/titlecardmaker/source-cache'
```

Expect total combined: 2-4 GB across both directories.

---

## Smoke test additions

Add two new tests to `scripts/smoke-test.sh`:

```bash
# 28. TCM timer scheduled
echo "28. TitleCardMaker timer"
TT=$(sshm "systemctl --user list-timers titlecardmaker.timer --no-pager 2>/dev/null | grep -c titlecardmaker.timer")
if [ "${TT:-0}" -ge 1 ]; then
  record "tcm-timer" pass "scheduled"
else
  record "tcm-timer" fail "timer not scheduled"
fi

# 29. TCM has rendered at least N cards (sanity — first run produced output)
echo "29. TitleCardMaker rendered cards"
CARDS=$(sshm "find ~/.apps/kometa/config/assets/TV\\ Shows -type f \\( -name '*.jpg' -o -name '*.png' \\) 2>/dev/null | wc -l")
if [ "${CARDS:-0}" -ge 10 ]; then
  record "tcm-cards-rendered" pass "$CARDS cards"
elif [ "${CARDS:-0}" -ge 1 ]; then
  record "tcm-cards-rendered" pass "$CARDS cards (low — first run not yet complete)"
else
  record "tcm-cards-rendered" skip "no cards yet (first run pending)"
fi
```

---

## Rollback per phase

| Phase | If broken |
|---|---|
| 38 (Install) | `systemctl --user disable --now titlecardmaker.timer titlecardmaker.service && rm -rf ~/.apps/titlecardmaker ~/.config/systemd/user/titlecardmaker.{service,timer} && systemctl --user daemon-reload`. Generated cards in Kometa's asset dir REMAIN — Plex still uses them until Kometa next run + you've also removed the assets dir entries. To fully revert: `find ~/.apps/kometa/config/assets/TV\ Shows -name '*.jpg' -newer ~/.apps/titlecardmaker/.installed_marker -delete` (operator inspects before running). |
| 39 (Render) | Bad cards: edit the offending show's `series.yml` block, restart `titlecardmaker.service`. To revert all cards for a show: `rm -rf ~/.apps/kometa/config/assets/TV\ Shows/"<Show Name>/"`; Kometa next run + Plex refresh restore the default (Plex thumbnail). |

**Never** delete from `source-cache/` — that's TCM's internal scratch space and won't break anything if left in place.

---

## Cost summary

- **TitleCardMaker**: $0 (GPL-3.0).
- **TMDb**: $0 (free tier; reuse existing key).
- **Disk**: 2-4 GB final + transient cache.
- **CPU**: ~1-2 hours one-time first-render burst, then minutes/day for new episodes.
- **Operator effort**: ~30 min Phase 38 install + ~30 min Phase 39 series.yml + visual verification iteration loop.

---

## What this plan does NOT do

- **No v2 web UI.** v2.x is sponsor-only; v1.16.0 YAML is what we run.
- **No Movie cards.** Plex doesn't surface movie title cards anywhere visible to users.
- **No anime card customization** by default — anime users typically want different fonts + Japanese title overlays. Defer.
- **No Jellyfin integration.** Jellyfin already has decent built-in episode card support; redundant.
- **No font customization** in the baseline. Operator can add custom fonts to `~/.apps/titlecardmaker/config/fonts/` and reference them per-series in series.yml.

---

## Total scope

- **1 install script** (`scripts/configure/58-titlecardmaker-install.sh`)
- **1 git clone** (CollinHeist/TitleCardMaker, pinned tag)
- **1 Python venv** at `~/.apps/titlecardmaker/venv/`
- **1 user-systemd service** (`titlecardmaker.service`, oneshot)
- **1 user-systemd timer** (`titlecardmaker.timer`, daily 05:30 + 15min jitter)
- **0 nginx fragments** (no Web UI on v1.x)
- **0 ports claimed**
- **2 new smoke tests** (`tcm-timer`, `tcm-cards-rendered`)
- **0 secrets committed.** TMDb key + Plex token reused — all in gitignored `secrets/`.

Estimated install time: ~30 min Phase 38 install + 1-2 hours unattended first full render in Phase 39.

---

## Open decisions (operator)

1. **Card type / aesthetic** — TCM ships several styles (`standard`, `cutout`, `frame`, `landscape`, `logo`, `roman`, `4x3`). Default `standard` is safest. Operator preview by manually rendering one show with each style before committing library-wide.
2. **Per-show overrides** — some shows have weird title formatting (e.g. "Breaking Bad" vs. "Breaking_Bad"). May need per-series font/template tweaks.
3. **Source frame strategy** — `source: tmdb` (default) vs. `source: plex` (use frames Plex already has). TMDb is cleaner; Plex is faster but requires Plex to have already extracted frames.
4. **Anime library** — include or skip? Skip recommended unless operator wants to curate anime card style separately.
5. **Schedule frequency** — daily is overkill for a steady-state library. Weekly is fine. Adjust `OnCalendar=` to `Sun *-*-* 05:30:00` if operator prefers.

---

## Execution protocol

### Step 0 — Pre-execution check

- [ ] Operator pre-reqs from "Open decisions" answered (or defaults accepted).
- [ ] Required credentials in `secrets/` (see Step 1 below).
- [ ] **Kometa Phase 32 complete** — TCM writes into Kometa's asset directory; that dir must exist first.
- [ ] **ImageMagick verified** — `sshm 'convert --version'` succeeds.
- [ ] Working tree clean: `git status` shows no unrelated changes.

### Step 1 — Credential pre-flight (consolidated, one-time)

| Cred | Used by (this plan) | Likely already exists? |
|---|---|---|
| `plex.token`, `plex.host`, `plex.port` | Phase 38 config | **Yes** — captured during Phase 5 audit |
| `tmdb.api_key` | Phase 38 config | **Yes if Kometa installed first**; otherwise operator must obtain from themoviedb.org |
| `titlecardmaker.version` | Phase 38 install | No — captured at first install (default v1.16.0) |

Hard blocker: `tmdb.api_key` missing → install fails. ImageMagick missing → file an Ultra.cc support ticket BEFORE proceeding (no `apt` available on the seedbox).

**Browser policy:** TMDb key requires a browser sign-up at themoviedb.org — document in `docs/operator-deferred.md` if not already done for Kometa. All other Phase 38-39 work is CLI/API.

### Step 2 — Implementation phases

Continuous execution per `feedback_continuous-execution-preferred.md`: don't ask for per-phase approval. Pause only on genuine blockers.

Phases in this plan:
- **Phase 38** — TCM install + venv + smoke run + daily 05:30 timer (Tasks 38.1-38.5)
- **Phase 39** — Operator-driven series.yml + first-full-library-run vigilance (Tasks 39.1-39.5)

Each phase = one commit (`tcm: phase 38 install — smoke +2`, etc).

### Step 3 — Self-check (after Phase 39)

1. Run `scripts/smoke-test.sh` — `tcm-timer` + `tcm-cards-rendered` must pass.
2. `git status` — clean (2 new commits ahead of `origin/main`).
3. Re-run smoke twice; the `tcm-cards-rendered` count should be stable across back-to-back runs.

### Step 4 — Log audit

1. `journalctl --user -u titlecardmaker.service --since "today" -p err`
2. `~/.apps/titlecardmaker/logs/{tcm.log,tcm.err}` — `grep -E 'ERROR|FATAL|Traceback|429|401'`
3. Kometa's next-run log — confirm new TCM-generated assets are picked up (`assets_for_all` triggers).

Classify each error:
- **Cosmetic** (e.g. "no source frame for episode SxxEyy" on episodes TMDb doesn't have) — note, don't act.
- **Actionable** (e.g. ImageMagick convert error on a malformed font) — fix, re-run.
- **Blocking** (e.g. Plex 401 token expired) — stop, surface in summary.

### Step 5 — Final summary template

```
# TitleCardMaker implementation
- Phases completed: 38, 39
- Scripts added: 1 install (58-titlecardmaker-install.sh)
- Configs added: ~/.apps/titlecardmaker/config/{preferences,series}.yml
- Smoke: N/N pass (was M/M before)

# Self-check results
- [details]

# Log audit
- Cosmetic: [list]
- Actionable (fixed): [list]
- Blockers requiring operator: [list, or "none"]

# Follow-up parking lot
- Per-show font/template overrides for shows with weird title formatting
- Anime library opt-in (if operator decides)
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

- **Don't bypass the ImageMagick pre-flight** — TCM silently produces 0-byte files without `convert`. Phase 38.1 Step 1 is the gate.
- **Don't run TCM full-library before Kometa's first run completes** — race condition: TCM writes into a directory Kometa hasn't initialized.
- **Don't enable anime in v1 baseline** — anime cards need different fonts (Japanese title overlay) and template choices; defer to a separate operator-driven phase.
- **Don't ignore TMDb 429 rate-limits** — TCM doesn't auto-back-off; observed 429s in logs = pause first, retry tomorrow at lower concurrency.
- **Don't `--sync-libraries-from-plex` before pruning anime/kids content from Plex's TV library** — saves a manual cleanup pass later.
- Don't commit secrets — gitignored.
