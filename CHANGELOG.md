# Changelog

## 2026-07-19 (later) — Usenet monitoring parity: SAB stall canary + failure alerts

The stack went usenet-live 2026-06-22 but monitoring stayed torrent-shaped.
Proven the hard way tonight: **2 SAB jobs sat slot-Paused since 07-16**
(wedged SAB queue objects — resume API silently no-oped, app restart no
help) with every surface green. Cleared via the designed unstick path
(delete + blocklist + auto re-search) → replacements at 215 MB/s.

New: `sab-stall` canary (15th canary, every-15min, Kuma **"Canary SAB
Stall"** #103) with two predicates — queue speed ~0 ≥10 min with active
slots (dead provider/creds) and slot-Paused job pinned ≥24 h (tonight's
class) — plus `downloads.sab.failed_24h` in the heartbeat doc with a
"N Usenet download(s) failed (24h)" warn alert (reaches the phone with no
app rebuild). Counts synced: 15 canaries / 54 manitoba / 55 total.

## 2026-07-19 — Phantom stuck downloads + silent reaper heartbeat, both fixed

**Heartbeat app showed 5 stuck downloads vs 0 real.** Root cause: acted-on
unstick candidates whose torrents were long gone from qBit lingered in
`stale-state.json` forever — the delta-based prune needs 3 snapshot samples,
which a gone torrent never produces. Fixed in two layers: the collector
(`qflix-collect.py`) now prunes tracked hashes absent from the latest
snapshot's torrent list (guarded against a failed qBit collect mass-pruning
legitimate state), and `app_status.py`'s `build_stuck_list` skips candidates
not present in live qBit, keeping the doc honest between hourly collects.
Deployed + verified live: stale-state 5 ghosts → 0, phantom warn alert gone.

**"QFlix Reaper" Kuma monitor red despite clean reaper runs.** The
`qflix-reaper` key had **never** existed in `~/secrets/kuma-push-tokens.json`
(absent from every backup back to 2026-05-22), and `_push_kuma()`'s
missing-token early-return was silent — no journal or logfile trace — so the
monitor red-looped on Kuma's 25h watchdog (3rd recurrence: 07-13..15 were
"fixed" with un-persisted manual pushes). Fixed durably: token persisted into
the secrets file (from the monitor's own DB row), missing-token path now
`warn()`s into the durable logfile, monitor pushed green same day.

**Deploy parity swept** (box vs repo, 58 files): 1 drift closed
(`functional-audit.py` picked up the 07-13 Homarr-decommission edit), rest
match; `bazarr2-sync.py` confirmed in parity at its `~/.opt/maint/` home.

**Tdarr dual-default audio bug — FIXED via new audio-disposition janitor.**
Tdarr's ensure-AAC flow step leaves BOTH audio tracks flagged `default`
(original EAC3 + added AAC — ffmpeg copies the disposition from the source
stream), so Plex tie-breaks to the EAC3 track and live-transcodes audio
despite the compatible AAC track sitting right there. Dry-run showed
**318 of 424** library files affected. The installed
`ffmpegCommandEnsureAudioStream` 1.0.0 plugin has no disposition control, so
the fix is a new standalone janitor (compartmentalization law; portable
as-is to qflix2): `scripts/maint/audio-disposition-janitor.py` +
`manitoba-maint-audio-disposition.{service,timer}` (daily 04:30 UTC, Kuma
push monitor "QFlix Audio Disposition"). Narrow predicate — only the exact
dual-default-with-AAC-compat pattern — disposition-only stream-copy remux,
ffprobe post-verify, mtime preserved, atomic replace, Tautulli
active-session skip, 50/night cap (backlog converges in ~7 nights). First
supervised batch verified live: EAC3 `default=0`, AAC sole default. Video
HEVC→H264 transcodes on Plex Web remain by design (browsers can't decode
HEVC; the flow only targets VC-1/MPEG-2).

## 2026-07-18 — TV fallback v2: Season-0 specials janitor + TV park-only

**New: standalone specials-policy janitor** (`scripts/mcp/specials_policy.py` +
`qflix-specials-policy.{service,timer}`, daily 06:00 UTC, own Kuma monitor
"Qflix Specials Policy"). Enforces **"Season 0 is never monitored"** across
sonarr/sonarr2: unmonitors any monitored S0 episode and clears the Season-0
season flag (the flag clear is what makes it durable — a series refresh
otherwise re-monitors episodes to match the flag). Deliberately a separate unit,
not folded into quality-fallback, so it stays compartmentalized / independently
tunable as QFlix migrates to larger servers. Motivated by a 2026-07-18 stuck-TV
investigation: the `quality_fallback` TV digest was almost entirely Season-0
specials (Ted Lasso promo featurettes, Chainsaw Man recap/chibi shorts) with
**zero obtainable releases at any quality**, plus one queue stall (Graham Norton
S33E12, unstuck). Live remediation swept the specials the same day.

**TV fallback goes park-only** (`quality_fallback.py`): a real (non-specials)
aired+searched episode still missing at day 15 is unmonitored + Discord-warned
(day 5 stays an info heads-up), blast-capped 10/run. No quality-loosening ramp
for TV — Sonarr profiles are per-series, so loosening one stuck episode would
drop the whole series, and release-less items grab nothing at any quality.
Resolves the "TV alert-only, v2 decided from data" deferral from the 2026-06-06
design.

**Confidence: two-round Council (v2) adversarial review.** Round 1 routed back 3
major masked-live-write-failure defects (swallowed episode-fetch failure left S0
monitored under a cleared flag with Kuma green; park recorded before the
unmonitor was confirmed, no retry; exit code ignored TV-park failures) + 2
hardening items; all fixed TDD. Round 2 returned COMMIT (20/20 lens verdicts
pass, each with an executable artifact). The conditional D8 deploy gate was
cleared with a live `PUT /series` round-trip proving Sonarr 4.0.17 accepts the
full-object body with non-S0 flags preserved and no fields dropped. Suite 881
green. Ledger: `.claude/council-ledger.jsonl`.

**Manifest/docs reconciled:** the 3 stale `Quadstronix` externals (removed from
Kuma 2026-07-16 but left in `kuma_external_monitors`) dropped from the manifest,
fixing a `test_doc_counts` red since 07-16. Counts across README / inventory /
wiki / FAQ now match live: **35 apps · 52 manitoba · 53 total Kuma monitors ·
14 canaries**.

## 2026-07-16 — QFlix Heartbeat v2 phone app · reaper token restore · CI time-bomb

**New: QFlix Heartbeat v2** — personal read-only Android dashboard
(`apps/heartbeat-android/`, Kotlin/Compose) fetching one JSON doc from a new
seedbox aggregator (`scripts/mcp/app_status.py`) over a dedicated ed25519 key
locked to `command="python3 …/app_status.py",restrict` in authorized_keys — the
key can only emit health JSON. Sections: quota bars (disk GB+% / bandwidth
%-only — Ultra.cc hides GB from user accounts), Kuma up/down + reds, live
streams/users fraction, top-5 requests + watch time (30 d), downloads/stuck/
unsticks, derived alerts. Installer `scripts/configure/74-heartbeat-status-install.sh`
(idempotent; gate detects unrestricted key duplicates); one-shot
`apps/heartbeat-android/provision.ps1` moves the key to app-private storage and
deletes the box copy. 5 adversarial-review fixes applied (incl. authenticated
host-key pinning — no TOFU). Old `com.qflix.heartbeat.debug` uninstalled; its
source was never retained. Spec + plan under `docs/superpowers/`, both stamped
as-built.

**Reaper Kuma red (07-15 → 07-16) root-caused: missing push token, not a reaper
fault.** The `qflix-reaper` entry had vanished from `secrets/kuma-push-tokens.json`;
`_push_kuma()` silently no-ops on an empty token, so monitor #97 starved while
the daily 05:00 UTC `--execute` runs kept succeeding (07-16 run deleted
"The Pacific" correctly). Token restored from kuma.db (backup
`kuma-push-tokens.json.bak-2026-07-16`). Confidence work: new
`tests/unit/test_reaper_e2e.py` (11 tests driving the real `run()`/`main()` —
age boundary, cap ordering, exclusion rail, dry-run vs execute exact call
lists, manifest-before-delete, Kuma up/down/empty-token regression) + a live
box dry-run with independent before/after API counts across 9 surfaces proving
zero mutation. Known live posture: `--max-pct 100` drop-in disables the
%-tripwire (operator decision 2026-07-13, documented in the on-box drop-in);
exclude file has zero active rules.

**CI red since 07-15 was a fixture time-bomb** — `tests/fixtures/arr-queue/cluster.json`
hardcodes ETA `2026-08-13`; the slow-cluster predicate needs ETA > now+30 d,
which the calendar crossed on 07-15, breaking 2 `test_arr_housekeeping.py` tests
on every push. Tests now inject now-relative ETAs. Suite 862 green.

**Kuma pruned by operator:** `Quadstronix` + `Node 1` + `Node 2` externals
removed (stale DNS, both nodes resolved to one dead IP) — 55 → **52 monitors**.

**Heads-up:** disk quota crossed **80%** (2242/2794 GB) on 2026-07-16 — the new
app's amber warn fired for it.

**A single un-resolvable orphan used to page the reaper twice daily forever.** An
"orphan" is a Plex item aged past the 60-day threshold that resolves to no unique
*arr id (no backing *arr record, or missing external guids) — by design never
deleted. But it set the same `partial` flag as a transient operational failure,
so it fired an ERROR notify + Kuma-down on **every** run until a human cleared it.
Triggered by "Frieren: Beyond Journey's End" (anime series, files removed
out-of-band, `sonarr2` empty → UNRESOLVED) on 2026-07-14.

Fix (`scripts/maint/qflix-reaper.py`): split operational failures from orphans and
put orphans on a **24h time-grace**, tracked in a durable state file
(`~/.opt/maint/reaper/orphan-state.json`). A **fresh** orphan (first seen ≤24h)
still reds the run so you notice newly-stranded media; a **known** orphan (older)
goes **green** and is surfaced via `--json`, the durable log, and a throttled
**weekly WARN** reminder — no more forever-red. Operational failures
(DELETE/Seerr/Plex/arr) page exactly as before. The safety rail (an orphan is
NEVER deleted) is untouched. New flags `--orphan-grace-hours` (24),
`--orphan-remind-days` (7), `--orphan-state` are defaulted so the systemd units
need no edit. The reminder slot is consumed only at the guaranteed emit point, so
a cap-trip / lock-held abort can't silently swallow it. 19 new unit tests (52
total green). Spec:
[`docs/superpowers/specs/2026-07-14-reaper-orphan-grace-design.md`](docs/superpowers/specs/2026-07-14-reaper-orphan-grace-design.md).

## 2026-07-13 — Homarr fully decommissioned (killed a 31-alert restart-storm)

**Homarr is gone.** It was superseded as the public root by the qflix-dash
SvelteKit board on 2026-06-27 and left running only to serve a "QFlix has moved"
notice. On 2026-07-13 its container died and stayed down; the pusher's auto-heal
exhausted its 3 `app-homarr restart` attempts and then paged Discord on every
permanent-failure re-arm — **31 alerts** for an already-retired app.

Full decommission (Maintainerr-decom pattern): Kuma monitor **#39 "Homarr"**
deleted (off the public status page + no more down-notify); `homarr` removed from
`manifest/apps.yaml` and the deployed `~/.opt/maint/apps.yaml`, pusher + webhook
restarted (auto-heal loop stopped); both UCC slots uninstalled (`app-homarr` +
`app-homarr-upstream`, config backed up to `~/.apps/backup/`); `homarr.{host,port}`
secrets and the push token purged. Retired the Homarr-only configure scripts
(`34-nginx-root-to-homarr.sh`, `35-homarr-seed-boards.py`, `46-homarr-add-comms.py`,
`61-homarr-qflix-theme.py`) and `scripts/qflix-dash/homarr-moved-notice.py`. The
`mobile-ux` canary was **kept** — it was repointed to the dashboard on 2026-06-27
and now guards the live homepage. Counts: **34 apps / 51 manitoba Kuma monitors**
(was 35 / 52). The old `homarr-upstream-<host>` subdomain is now dead (expected —
uninstall tears down the outer-nginx route).

## 2026-07-09 — Newsletter digest routine fix + never-silent detection

**The weekly "Behind the scenes" cloud routine had silently stalled.** It fired
every Monday but stopped publishing after 2026-06-29 (the 07-06 run produced no
commit), so the newsletter quietly fell back to the deterministic commit recap
with no alert. Root cause (council-v2 diagnosis): the routine's pinned model
`claude-sonnet-4-6` — a prior generation — most likely went unavailable between
the last success and the failure; the session fired but couldn't do the work.

Fixed: the routine's model updated to `claude-sonnet-5`, and the `qflix-digest`
skill hardened with VERIFY-AFTER-PUSH (re-fetch + assert `week_of`==today; a curl
failure or mismatch is a run failure) + FAIL-LOUD (Gmail alert on any failure).

**New detection canary `newsletter-digest-stale`** — the real fix, so it's never
silent again: it checks the `newsletter-digest` branch's `week_of` against the
newsletter's own `_is_fresh` rule at Monday send time (fires 14:20/14:50/15:20
UTC) and pushes DOWN → Kuma + Discord when the blurb is stale/absent/malformed.
Enforcement is gated to the Monday send window so the rule's +4-day freshness
bound never false-alarms mid-week. Kuma monitor "Canary Newsletter Digest"
bootstrapped (14th canary; 52 manitoba monitors). Monitor counts reconciled
across README/inventory/wiki and the doc-counts test now counts the 3
auto-injected monitors (pusher self-heartbeat, fleet aggregate, QFlix Reaper).

## 2026-07-08 — Self-healing hardening, stream cap, docs reconciled to live

**qBittorrent WebUI auto-heal fixed (d8d82bf).** A host maintenance reboot left
qBit's WebUI unable to bind its port; auto-heal couldn't recover it because
recovery ran `start` (a no-op on a running-but-degraded app) instead of
`restart`, and the permanent-failure latch never re-armed. Recovery now
restarts, and the latch auto-re-arms after a cooldown so a cleared transient
self-heals. Added a boot-time TCP-listener snapshot (`boot-listeners-snapshot.sh`)
to identify a port squatter next reboot, and widened qBittorrent's recovery
backoff (5c5d4de).

**Stream cap raised to 4 per member (4865aba).** The `kill_stream` and
`stream_stats` every-minute crons are now reproducibly provisioned by
`scripts/configure/59a-plex-stream-crons-install.sh` (previously manual crontab
entries the repo couldn't rebuild).

**Documentation reconciled against the live environment** (council-v2 audit +
follow-up). Homarr → qflix-dash (public dashboard cutover is live), Maintainerr →
qflix-reaper, Gemini "AI Picks" retired (the "Behind the scenes" blurb is written
by a scheduled Claude cloud routine, with a deterministic commit-recap fallback;
a "This week's tune-ups" line reads `last-upgrade.json` on blurb-less weeks).
Counts corrected across wiki/FAQ/README/inventory (35 apps, 13 canaries, 51
manitoba Kuma monitors), the FAQ stream-cap prose fixed (per-member, not a global
total), and the deployed newsletter systemd unit's description de-Gemini'd.

## 2026-06-27 — Newsletter "Behind the scenes" + autonomous digest; Gemini retired; repo public

**Newsletter gains a "Behind the scenes" section.** The weekly digest
(`scripts/qflix-newsletter`) now renders a "🔧 Behind the scenes" block between
Coming Soon and Nerd Corner, summarizing what improved for members that week.
Two sources, override-then-fallback, both fail-safe (hide the section, the email
still sends): (a) a Claude-authored, non-technical blurb published to the new
**`newsletter-digest`** branch as `digest/latest.json`, freshness-guarded so a
stale blurb is never shown; (b) a deterministic recap built from the week's
public GitHub commits — grouped feat / fix·perf, scope-stripped, with an opt-in
`Newsletter:` commit-body trailer to override any subject with friendly copy.
New `changelog.py` + 18 unit tests (27 in the package, all green).

**Gemini / "AI Picks" retired.** Confirmed from the seedbox logs: every run since
2026-05-11 hit `HTTP 429 quota exceeded, limit:0` for `gemini-2.0-flash` (free
tier revoked on the deprecated endpoint), so the section never once rendered.
Deleted `ai.py`, the `google-generativeai` dependency, the config field, the
template block, the install copy line, the `gemini.api_key` secret (local +
seedbox), and the stale `secrets-convention` row (replaced with optional
`github.repo`).

**Autonomous weekly digest routine.** A scheduled cloud agent (the `/qflix-digest`
skill, committed at `.claude/skills/qflix-digest/`) runs in Anthropic's cloud
every **Mon 14:00 UTC** — one hour before the 15:00 UTC send — reads the week's
commits from its own checkout, writes the member blurb, and pushes it to the
`newsletter-digest` branch. It runs independently of any workstation; proven
end-to-end (a test-fire pushed `5b4aa20` in ~20 s). If a run is missed, the
deterministic commit recap covers it. Routine `trig_01ARibSXarcy5ddQdDXiV3Dp`.

**Repo made public.** `Quadstronaut/QFlix` flipped to public after verifying the
history is secret-clean (264 commits, root is the deliberate "sanitized public
release" squash, `secrets/` never tracked, no keys/tokens/private keys in any
diff). This removes the need for any GitHub token on the seedbox or in the cloud
routine — both read commits + the digest unauthenticated. Scrubbed the real SSH
host + a stale local path out of the two `buildarr-v4-patch-session-*` docs.

**Reusable single-recipient test send.** `python -m qflix_newsletter --test-to
EMAIL` renders the true production email and fires a single Listmonk test to one
address (recipient must be a subscriber) without mailing the list.

## 2026-06-26 — Maintainerr decom finished, SABnzbd manifested, qui removed

A council audit found the 2026-06-20 Maintainerr decommission left live
references behind — including a broken disk-safety path. A second council (arch
tier, unanimous 5-lens **commit**) produced the code fixes.

**Fixed — broken autonomous disk-reclaim (`scripts/canaries/quota.sh`).** The 90%
CRITICAL branch still read `~/secrets/maintainerr.key` and POSTed to the
decommissioned Maintainerr subdomain (502) — so autonomous space reclaim above
90% disk silently failed and would have marched to the 98% FAIL wall. Repointed
to the deployed replacement `python3 ~/scripts/maint/qflix-reaper.py --execute
--json`, relying on the reaper's built-in `--max-items`/`--max-pct` caps +
run-lock (no `--force`); preserves the `STAGE=quota-critical`/`quota-reclaim-fail`
labels and Kuma-DOWN exit. Verified live: canary runs `PASS` at 63% (reaper
branch correctly dormant).

**Orphan references purged.** `scripts/mcp/logs.py` (dead maintainerr glob route),
`scripts/smoke-test-plex.sh` E18 + `scripts/smoke-test.sh` #11 (maintainerr tests
that false-failed once the key was gone), and `scripts/ops/maintainerr-fix-watch.ps1`
(205-line dead watcher, deleted). Orphan `~/secrets/maintainerr.key`/`.port`
deleted on the seedbox (only stale `apps.yaml` backups still name them). Two
benign historical comments left intentionally.

**SABnzbd manifested (33→34 apps).** The 2026-06-22 usenet buildout left SABnzbd
unmanaged. Added a `sabnzbd` UCC entry (`kuma_monitor: SABnzbd`, http_root
`/sabnzbd/` expect 200 — SAB's form-login redirect resolves to a final 200) and
created the live **SABnzbd** Kuma PUSH monitor + token via
`bootstrap-kuma-monitors.py`. Pusher now reports **34/34 ok**, sabnzbd pushes
`up`, health 200. README/inventory counts reconciled: apps 33→34, manitoba
monitors 47→48, total 51→52.

**qui removed.** Orphaned autobrr qBittorrent web-UI (`app-qui`, port 42010,
installed 2026-05-25, zero references, not in manifest/inventory) uninstalled via
`app-qui uninstall` — service/unit/binary/data gone, port freed.

Repo 715 tests pass / 5 skip; 0 failed systemd units on the box. Council ledger
in `.claude/council-ledger.jsonl`.

## 2026-06-26 — VictoriaLogs crash-loop fixed (thread-cap exhaustion)

**Alert:** `✗ victorialogs could not be started after 3 attempts — operator needed`
— fired 06-23 (×2), 06-24, 06-26 per `~/.opt/maint/notify.log`; `lib/recovery.py`'s
3-attempt loop exhausted every time and marked the app permanently-failed.

**Root cause — `pthread_create: Resource temporarily unavailable` (EAGAIN) → SIGABRT.**
Storage opens cleanly, then the process aborts while spinning up worker/flusher
threads. The shared Ultra.cc seedbox exposes all **128 host cores**, so the Go
runtime defaulted `GOMAXPROCS=128` and burst a ~thread-per-core pool at startup;
combined with the per-partition flusher fan-out over **86 daily partitions** (90d
retention), that pushed the *user's* total OS-thread count past the `ulimit -u` /
`RLIMIT_NPROC` = **2000** cap (the rest of the QFlix stack already holds ~1000
threads — python3 alone 239, Plex 162). `pthread_create` then returns EAGAIN and
the process SIGABRTs in a 10s `Restart=on-failure` loop, never binding
`127.0.0.1:42015`.

**Why now:** hard failures begin **06-23**, the day after the **06-22 usenet
buildout** (SABnzbd/Frugal) raised the baseline thread count over the edge. The
06-11 `recovered after 3 attempt(s)` entries were the early warning.

**Fix:** `Environment=GOMAXPROCS=4` in `scripts/maint/systemd/victorialogs.service`
(deployed to `~/.config/systemd/user/`). vlogs is I/O-bound, not CPU-bound (3s CPU
over a 48s boot), so capping the scheduler bounds the thread high-water mark at no
perf cost.

**Verified:** 2 clean restarts, **0** `pthread_create` aborts, thread count 11–28
(was bursting past 2000); `is-active=active`, **health 200**, ingest cycle **25178
lines / 0 failures**, LogsQL count query returns data. Steady-state clean boot
**47.6 s** (the first post-crash boot took 156 s clearing unclean-shutdown debris —
a one-time cost). `manitoba-maint-pusher` re-probes healthy and clears the
permanent-failure mark.

**Recovery-window hardening (implemented):** the 48 s clean boot fits recovery's
`[10,30,60]` s probe window (3rd probe ≈100 s), but an unclean-shutdown debris boot
(>150 s) would trip a false alert. `lib/recovery.py` now reads `recovery_attempts`
/ `recovery_backoff_s` / `kuma_recheck_delay_s` from the per-app manifest entry
(`App.raw`) before falling back to the global `defaults`; the `victorialogs` entry
sets `recovery_backoff_s: [30, 90, 180]` (probes at ≈30/120/300 s — catches both the
48 s clean boot and a >150 s debris boot). Covered by 3 new `test_recovery.py` cases;
full suite 715 pass / 5 skip.

## 2026-06-24 — Full-stack audit (host · apps · canaries · scripts · 72h logs)

End-to-end audit against the live seedbox. Headline: **33/33 apps UP, 13/13
canaries green, 711 pytest pass, smoke 51/56** (4 fails were non-faults). No
host/perf problems — disk 1.77 TB / 2.79 TB quota (63%), load is shared-box
noise, glibc 2.31 (the documented Tdarr pin reason).

**Fixed this audit:**
- **Prowlarr `prowlarr-indexer-health` canary was red** (true-positive): the dead
  public indexer **`TorrentDownload`** (id=5) had been failing >6h, tripping
  Prowlarr's health warning and the *arr "Indexers unavailable" warnings.
  Disabled it + triggered `CheckHealth` → 0 issues, canary green.
- **`smoke-test.sh` + `scripts/canaries/README.md`** still referenced the retired
  `deletion` canary (its Kuma monitor was deleted 2026-06-20, so smoke false-
  failed every run). Swapped the spot-check to `quota`; README now lists the real
  13 canaries (added `prowlarr-indexer-health` + `quota`, dropped `deletion`).
- **Doc reconciliation** — README/inventory/FAQ still presented **Maintainerr as a
  load-bearing app** and an off-by-one **34→33** app count. Completed the
  2026-06-20 Maintainerr→`qflix-reaper` decommission across all three: required-
  apps table, library-hygiene + monitoring diagrams, timeline (appended 06-20 /
  06-22 / 06-24), FAQ canary table (14→13, swapped `deletion`/`maintainerr-rule-
  sanity` for `tautulli-plex-link`), and the smoke buckets.

**Remediated in the follow-up pass (2026-06-25):**
- **#2 Sonarr↔SABnzbd "Connection refused" → FULLY FIXED.** SAB was never down. The
  SAB download-client was recreated fresh (**id 6 at `172.17.0.1`**) with the real
  API key. The live Sonarr **debug log** shows it polling `http://172.17.0.1:17007/
  sabnzbd/api` (mode=queue/history) every minute and succeeding — **zero `127.0.0.1`
  across 28+ min of debug log**; the last real SAB event in `sonarr.txt` is a Jun-23
  *success*. Correction: `~/.apps/sonarr/sonarr.db` IS host-readable and is **clean
  of `127.0.0.1`** — the earlier "container-private, no lever" claim was wrong (the
  DB was never the source).
- **Plex library-update connection → FULLY FIXED on all four *arr.** They were pinned
  to the dead **`172.17.1.250:32400`** — the pre-re-IP Plex address that only Tautulli
  had been migrated off after the 2026-05-20 Ultra.cc kernel migration (per
  `50-tautulli-pms-url-fix.sh`). Repointed live to the stable bridge gateway
  **`172.17.0.1:<plex.port>`** (all four test 200; verified `172.17.0.1:17025` reaches
  Plex `/identity` 200 and the dead `172.17.1.250:32400` does not). Source fixed:
  `09-phase5-arr-connects-and-sync.sh` `PLEX_HOST` → gateway, not `plex.host`
  (=127.0.0.1). Native Plex refresh-on-import works again (last real failure Jun-23).
- **Observability fix — `scripts/mcp/logs.py` mis-timestamped continuation lines.**
  Multi-line stack-trace lines (no leading timestamp — `---> ...Connection refused
  (127.0.0.1:17007)`, ` -- : Test was aborted`) were assigned the *ingest* time, so
  old pre-fix exception lines resurfaced in VictoriaLogs as phantom "recent" errors.
  This made the SAB/Plex fixes *look* unapplied and fed the council a corrupted error
  signal. Fixed `collect_for()` to carry each timestamped line's ts forward to its
  continuation lines; the real Sonarr logs were clean all along.
- **#3 Maintainerr → fully UNINSTALLED.** `app-maintainerr uninstall` — `~/.apps/
  maintainerr` removed, no process, no UCC auto-restart, subdomain now 502.
- **#4 Orphan cleanup done.** `git rm` of `scripts/canaries/{deletion,maintainerr-
  rule-sanity}.sh` + their 4 systemd units; removed the matching refs in
  `240-maintenance-install.sh`; removed the dead unit files + deployed scripts from
  the box. `functional-audit.py`: dropped the MAINTAINERR section and fixed the
  Tautulli urlbase + Plex `<MediaContainer version>` probe bugs.
- **#5 `QFlix Reaper` registered.** Added to `audit_monitors()`'s expected set
  (alongside `Manitoba Pusher` / `QFlix Fleet`) — `kuma audit` now reports **no
  drift** (49/49 matched), no orphan.

**Deferred:**
- **#1 Sonarr/Sonarr2 4.0.17.2952 → 4.0.18.2971** and **qBittorrent 5.0.3 → 5.2.x**
  — left for the Monday `cp-upgrade` weekly sweep (Radarr pair already latest).

**72h log scan:** 7.40 M lines ingested, **97,454 ERROR-level**; journald `-p err`
clean (0). 97% of ERRORs are benign Plex `Unknown metadata type: folder` spam
(94,930). See the audit report for the full per-app error table.

## 2026-06-22 — Usenet path: SABnzbd + NZBgeek + Frugal (fix dead-swarm back-catalog)

A member flagged Vanderpump Rules S2 present but S1 missing. S1 (2013 Bravo
reality) exists on the stack's public trackers only as **dead-swarm SD** — 8/10
episodes had 0 real seeders (indexers reported fake "100+ seeders"; qBittorrent
showed 0 connected, while an unrelated download pulled fine). The stack was
**public-torrent-only** (qBittorrent + 22 public trackers, no Usenet/private),
so old back-catalog was unsourceable. Built a Usenet path; S1 now live in Plex
as 10/10 1080p.

- **`scripts/configure/90-sabnzbd-usenet-install.sh`** — installs SABnzbd
  (`app-sabnzbd`), adds the Frugal provider + `sonarr` category + docker-bridge
  host_whitelist, wires it into Sonarr as a download client via the bridge
  gateway `172.17.0.1:<port>` (the *arr run in linuxserver Docker — their
  `127.0.0.1` is not the host's), adds **NZBgeek** as a Newznab indexer, and
  flips the Sonarr delay profile to **enableUsenet + prefer usenet**.
- **Delay-profile fix (library-wide):** a fresh Sonarr ships
  `enableUsenet=False`/`preferredProtocol=torrent`, so automatic and
  failed-redownload grabs picked dead torrents over reliable NZBs ("Usenet is
  not enabled for this series" rejections). Now Usenet-preferred everywhere.
- **Prowlarr caveat:** its app-sync skips Usenet indexers that return nothing to
  the empty-term category probe ("No Results in configured categories"), so
  NZBgeek is held directly in Sonarr rather than synced.
- New gitignored secrets: `usenet.{host,port,user,pass,ssl,connections}`,
  `sabnzbd.{key,port}`, `nzbgeek.{key,url}` (see docs/secrets-convention.md).
- Not yet in `manifest/apps.yaml` (no Kuma monitor / lifecycle) — follow-up.

## 2026-06-06 — quality fallback: two-stage loosening for stuck missing movies (PR #66)

A movie missing for 5 continuous attempted days (proof via `lastSearchTime`)
gets swapped to `QFlix Fallback HDTV` (+HDTV-720p/1080p, +WEB 720p); day 10 →
`QFlix Fallback SD` (+SDTV/DVD/WEB 480p/Bluray-480p/REGIONAL); day 15 →
original profile restored, unmonitored, Discord alert. A grab at any fallback
stage restores the original profile so upgradinatorr/RSS can upgrade later.
CAM/TELESYNC/TELECINE/DVDSCR/WORKPRINT hard-banned everywhere; fallback
profiles live outside recyclarr's managed set. TV is alert-only (once-per-
episode Discord digest) — v2 decided from v1 data.

- `scripts/mcp/quality_fallback.py` — pure planners + null-skipping
  `PUT /movie/editor` writes (only `qualityProfileId` + `monitored`, ever);
  25-per-instance blast-radius cap; `--bootstrap-profiles|--cron|--emit-json|--dry-run`.
- `qflix-quality-fallback.timer` daily 07:30 UTC (30 min after missing sweep);
  manifest cron entry (34th app); Kuma monitor pending operator bootstrap
  (docs/operator-deferred.md).
- Verified live post-deploy: profiles on both radarr instances ban all
  pre-retail; dry-run clean; timer armed.
- Plan double-reviewed by 2 independent Opus panels (unanimous approve);
  all API payloads verified against deployed Radarr 6.1.1.10360 + source at
  tag (RTFM section in the plan doc).

## 2026-05-30 — Tdarr Phase 30 go-live: keep transcoding live (PR #65)

The live seedbox had `processLibrary=True` on all 3 libraries (Movies/TV/Anime)
— Tdarr actively transcoding via the `qflix-direct-play-fix` flow (484 files
catalogued, 637 health-checks, 20 transcodes). But the repo disagreed: the only
code touching `processLibrary` was `50b-tdarr-config.py`'s
`set_non_destructive_mode()`, which **forced it to False** ("Phase 30 gate, flip
in 50d") — and no 50d ever existed. **Hazard:** re-running the idempotent 50b
config would have silently flipped every library back to False and halted all
transcoding.

- **`set_non_destructive_mode()` → `ensure_library_processing()`.** Now enforces
  `processLibrary=True` (Phase 30 go-live, operator green-lit 2026-05-30).
  Idempotent: only writes a library that has drifted to False, so it also
  self-heals any library paused out-of-band. Re-running 50b now *preserves* live
  transcoding instead of killing it.
- **Verified live:** re-ran 50b over SSH against the box — reported
  `Libraries enabled for live transcoding: 0` (already True) with no `[lock]`
  output; box re-confirmed `processLibrary=True` on all three after the run.
- Docs: `inventory.md` (tdarr-server row) + `operator-deferred.md` (new Phase 30
  row) record that transcoding is live.

## 2026-05-30 — tdarr-node heartbeat honors fair-use quiet hours (PR #64)

Two auto-heal mechanisms were fighting each other. `50c-tdarr-quiet-hours.sh`
intentionally **stops** `tdarr-node` 18:00–23:00 UTC so its worker threads don't
compete with streamers during peak watch hours. But `heartbeat-tdarr-node.sh`
(cron `*/5`) restarts the node whenever the server reports **0 registered
nodes** — which is exactly what a paused node looks like. The watchdog revived
the node on the next tick, collapsing the 5-hour pause to ~2.5 minutes.

- **Observed 2026-05-30:** node stopped `18:00:01 UTC`, back up `18:02:27 UTC`
  instead of staying down until 23:00. Net effect: the node ran ~24/7 and never
  backed off at peak.
- **Fix:** `heartbeat-tdarr-node.sh` now early-exits during the 18:00–23:00 UTC
  pause window (UTC-hour guard, `10#` base-10 to avoid octal parsing of `08`),
  before any restart path. The watchdog still covers genuine failures outside
  the window. Window kept in sync with `50c`'s `OnCalendar` values.
- **Verified live (in-window):** deployed to seedbox, EOL-normalized SHA-256
  parity confirmed; with the node stopped, the heartbeat exits 0 and leaves it
  inactive (pre-fix it would have revived it). Today's pause restored manually.

## 2026-05-26 — flaresolverr-canary honors push-suppress + alert audit trail (PR #62)

FlareSolverr went into a crash-loop (HTTP listener connection-refused) pending
an Ultra.cc ticket (s6 `cap_setuid`). Its Kuma monitor was correctly muted via
the push-suppress registry (PR #61) — but the operator kept getting paged at
2 AM anyway. Root cause: a **second, independent alert path** that the registry
didn't cover.

- **`flaresolverr-canary.py` now honors the push-suppress registry.** The pusher
  mutes the `FlareSolverr` Kuma monitor and skips recovery when the app is in
  `push-suppress.json`, but the standalone restart-bot canary runs on its **own**
  5-min timer and pages Discord **directly** — it never consulted the registry,
  so it kept firing `restart REFUSED — 3 restarts in last hour ≥ cap 3 …
  crash-loop; operator intervention needed` every cycle. `run()` now checks
  `lib.suppression.push_suppressed(FS_SUPPRESS_KEY)` **first** and short-circuits
  to a clean no-op (no probe, no restart churn, no page). Fail-open: any registry
  read error falls through to normal alerting. The self-destructing unsuppress
  watcher already lifts the entry once FlareSolverr is live, restoring both the
  monitor and this canary in one move. **Lesson:** any standalone alert path
  (not just the pusher) must consult the suppress registry.
- **`lib/notify.py` now writes a full send-audit trail to `notify.log`.**
  Previously only *failed* sends were recorded (`notify-fail.log`); a delivered
  page left no trace. Every attempt — sent or failed — is now appended to a
  capped `notify.log` (token redacted), plus `logging.info`/`warning`. The file
  audit is caller-independent (canary uses `print()`+journal; pusher uses
  `logging`). `notify-fail.log` semantics are unchanged.
- **Deployed + verified live:** seedbox runtime files are byte-identical
  (EOL-normalized) to merged master; the canary journal logs `SUPPRESSED …`
  instead of paging. The suppress entry auto-restores when FlareSolverr is live
  (post-ticket); it can also be removed manually.

## 2026-05-22 — 7-gap triage closure + Kuma self-heal + hardlink-canary rewrite (PR #42)

Sweep across the seedbox that started as a triage of seven known gaps
and ended with two structural improvements: external-monitor push
tokens now self-heal, and the hardlink-integrity canary stopped
firing 20/20 false-positives. Final Kuma state: **50 UP / 2 dormant /
0 DOWN** (52 monitors total).

### Triage closures

- **`tdarr.server_port` + `tdarr.api_key` secrets deployed.** Both
  installer-bootstrapped via `scripts/configure/50-tdarr-install.sh`
  but had never run on the current host. Wrote them manually
  (`42018` and `tapi_…`), 0600 perms; `~/.config/tdarr` now serves
  `/api/v2/status` cleanly. Functional-audit goes green.
- **qBittorrent orphan categories purged.** `mylar` + `readarr` left
  behind from the 2026-05-11 purge sweep. Removed via
  `POST /api/v2/torrents/removeCategories` with a real-newline-
  separated body (URL-encoded `%0A` is treated as one category name
  &mdash; only `--data-binary @-` with a literal newline works). qBit
  category list now reads `[radarr, radarr-anime, sonarr-anime,
  tv-sonarr]`.
- **5 chronically-failing Prowlarr indexers disabled.** BTdirectory,
  0Magnet, TorrentProject2, EZTV, Torrent Downloads &mdash; all sat
  in long-term-failure for &gt; 2 weeks. GET/PUT roundtrip via the
  loopback API (URL bases require the <code>/prowlarr/</code> prefix
  or the proxy returns HTTP 307). Prowlarr health array clears to
  &ldquo;new update available&rdquo; only.
- **Radarr FNAF3 stale stub deleted.** TMDB id 1692507, Radarr id
  366, never had files and pointed at a non-existent TMDB record.
  Radarr health array now empty.
- **Plex log surface wired into VictoriaLogs.** Plex was the only
  managed app without a vlogs ingest route. Added route + non-ISO
  &ldquo;Mon DD, YYYY HH:MM:SS.fff&rdquo; timestamp parser (with
  month-name &rarr; numeric table) to
  <code>scripts/mcp/logs.py</code>; the ingest service auto-
  discovers from <code>_FILE_LOGS</code>, so a 24-hour vlogs query
  for <code>app:plex</code> immediately returned data. Self-test
  coverage 18 &rarr; 21.

### Structural improvements

- **`bootstrap-kuma-monitors.py` no longer wipes operator-placed
  tokens.** Previously the script built its tokens dict from scratch
  &mdash; only entries it re-synced on that run survived. Any
  manually-bootstrapped key (e.g. for an external PUSH monitor)
  vanished on the next run. The fix seeds the dict from the existing
  <code>secrets/kuma-push-tokens.json</code> before merging the
  fresh app/canary/pusher tokens.
- **External PUSH monitor tokens now self-sync.** Added a pass over
  <code>manifest.kuma_external_monitors</code>: for each entry whose
  Kuma type is PUSH (today: just &ldquo;QFlix Collect (workstation)&rdquo;),
  capture its <code>pushToken</code> and write it under the display-
  name key &mdash; that's what <code>Push-Kuma</code> in
  <code>qflix-collect.ps1</code> looks up. HTTP-type externals
  (Quadstronix nodes) are correctly skipped. <em>Net effect:</em>
  regenerating an external monitor in the Kuma UI no longer
  silently breaks its consumer; the next bootstrap sweep re-syncs
  the rotated token automatically. Closes the
  hourly-Discord-WARN failure mode that triggered this session.
- **`hardlink-integrity` canary rewritten qBit-side.** The old
  design sampled the 20 most-recently-modified library video files
  and failed if &gt; 50% had linkcount=1 &mdash; but qBit's
  share-ratio cleanup removes seeds faster than that sample window
  refreshes, so recent library files are almost always
  linkcount=1 not because *arr skipped the hardlink but because qBit
  deleted the source afterward. Fired 20/20 DOWN this morning while
  *arr was at 100% hardlink coverage (verified by inode cross-check:
  60/60 qBit-completed torrents shared an inode with a library
  path). The new design enumerates qBit completed-state torrents,
  stats each <code>content_path</code>'s (dev, inode), and looks up
  the media library for a sibling outside <code>~/downloads</code>.
  Two thresholds (both must trip): <code>MAX_DETACHED</code> count
  and <code>MAX_DETACHED_PCT</code> percent. Live run reports
  <code>qbit_completed=60 hardlinked=60 detached=0</code> in 214 ms.
  Old script preserved at
  <code>~/scripts/canaries/hardlink-integrity.sh.old-pre-rewrite-20260522</code>.
- **`docs/secrets-convention.md` documents two-copy layout.** The
  <code>kuma-push-tokens.json</code> entry now spells out that the
  seedbox copy (read by <code>manitoba-maint-pusher.service</code>
  and the canary push pipeline) and the workstation copy (read by
  <code>scripts/local/qflix-collect.ps1</code>) are independent,
  with the bootstrap script syncing both when run from their
  respective hosts.

### Verified

- Bootstrap deployed + re-run on seedbox: 48 &rarr; 49 keys,
  workstation token captured (<code>15fk3z95Dn</code>),
  0 monitors created (idempotent).
- Hardlink canary deployed + service fired:
  <code>Active: inactive (dead) since &hellip; status=0/SUCCESS</code>
  &mdash; first PASS after several hours of 2/INVALIDARGUMENT
  exits.
- Kuma audit final: **50 UP / 2 ? / 0 DOWN**. The two ? (Canary
  Deletion + Canary Kometa Deploy-Drift) are heartbeat-retention
  artifacts; they re-prime on their daily 04:30 schedule.

## 2026-05-20 — random-audit findings + Kuma-push silent-failure guard (PR #29)

The 2026-05-19 random audit (REA + qflix-mcp + manual log scrub) surfaced
a 5-day-silent regression: the workstation collector's
`"QFlix Collect (workstation)"` push-token went missing from
`secrets/kuma-push-tokens.json` after the 2026-05-14 tokens-file regen.
Every hourly `qflix-collect.ps1` run pushed nothing for five days; the
seedbox-side Kuma monitor stayed red while the workstation-side
`qflix_status` MCP (which reads the local snapshot, not seedbox Kuma)
reported all-green. The two views disagreed and nobody noticed.

- **`Push-Kuma` now fails loudly on missing tokens.** `scripts/local/qflix-collect.ps1`
  used to `return $false` silently when `$tokens.$monitor` lookup missed.
  Now writes a WARN line to the transcript and posts a yellow Discord embed
  (`"Missing Kuma push token for monitor: <name>"`). Any future regression
  surfaces within one collect cycle instead of "until somebody runs an
  audit and reads the raw fetch."
- **Token restored manually.** Local-only secret (`secrets/` is gitignored).
- **Side observation, not actionable.** A single `manitoba-maint-canary-vlogs-stall.service`
  failed-start at `2026-05-19T19:31:09Z` turned out to be a transient — the
  next eight consecutive 15-min fires (`00:46 → 02:16 UTC`) all completed
  cleanly with status=0/SUCCESS.

`WARN.md` (this audit's punch-list) committed at repo root as historical
record.

## post-release-0.0.1 (2026-05-16) — live-verification fixes

End-to-end verification on the live seedbox surfaced five deployment-time
bugs that unit tests couldn't catch. All landed via PR #22.

- **`bootstrap-kuma-monitors.py` manifest resolution.** Hard-coded
  `REPO_ROOT/manifest/apps.yaml` was unreachable on the seedbox (no repo
  checkout there). Now resolves `MANITOBA_MANIFEST` env var → deployed
  `~/.opt/maint/apps.yaml` → repo path, in that order.
- **Pusher pushToken capture.** `get_monitors()` raced behind the create
  and returned the new monitor without its pushToken, so
  `kuma-push-tokens.json` was missing the `manitoba-pusher` entry. Now
  captures the token from `_add_push_monitor`'s return as fallback.
- **Smoke-test K_EXCLUDE manifest path.** Python helper opened
  `manifest/apps.yaml` from cwd, silently dropping the external-monitor
  filter whenever the operator ran the script from `~`. Now tries both
  the local repo and the deployed manifest path.
- **`Manitoba Pusher` auto-included in drift audit.** The daemon's
  self-heartbeat monitor exists outside the manifest's app/canary sets,
  so `manitoba-maint kuma audit` flagged it as orphan drift. Now
  injected into the expected manifest set so it's always matched when
  present and reported as `manifest_only` (bootstrap needed) when absent.
- **Workstation collector declared external.** Added `QFlix Collect
  (workstation)` to `kuma_external_monitors` — it's a Windows scheduled
  task that the seedbox manifest cannot self-heal.

Live smoke-test on seedbox: **51/0/2** (2 skips = pre-existing
`tdarr.server_port` secret gap documented in `todo-after-claude.md`).
Test count 440 → 446 (+5 unit, +1 regression guard for the pusher-
drift case).

## release-0.0.1 (2026-05-16) — audit sweep

First tagged release. Closes ~90 findings from the 2026-05-15 top-to-
bottom audit. Built on top of `pre-release-0.0.1` (tagged at 7861768,
the merge of PR #20 — qbit-stall canary). See the
`release/0.0.1-audit-sweep` branch for the per-commit detail.

### Highest-leverage fixes

- **MCP→SSH shell injection closed.** `qflix_mcp.qflix_get_logs`,
  `qflix_unstick_torrent`, `qflix_diagnose_unstick`,
  `qflix_trigger_missing_search` now `shlex.quote` every caller-supplied
  argument. 5 regression tests against `;`, `|`, `&`, `$(...)`, backtick
  payloads.
- **PGPASSWORD off the command line.** `configure/43-listmonk-install.sh`
  pipes the postgres password via env-on-stdin so it no longer appears
  in `ps -ef` for the lifetime of every psql call during install.
- **Recovery loop unified.** Kuma webhook + pusher now share
  `recovery._RECOVERY_SEMAPHORE` + `_in_flight`. Permanent-failure mark
  prevents the pusher from re-firing `trigger_async` every 60s for the
  duration of an outage that has already exhausted the 3-attempt loop.
  Pusher's Kuma `msg=` annotates strike state for at-a-glance triage.
- **Health probes fail loud.** A missing `auth_secret` /
  `basic_auth_secret` returns `ok=False` instead of silently issuing
  an unauthed request that may 200-without-auth on the wrong target.
  `health.kind` typos now caught at manifest load, not deep in the
  pusher loop. `lib/secrets.py` unifies the prior 6 copy-pasted
  `_secrets_dir()` functions that disagreed on which env var to read.
- **Atomic writes for the stale-state DB.** `qflix-collect.ps1`'s
  `Update-StaleState` + `Stamp-ActedOn` both hit `stale-state.json` in
  the same PS run; the prior non-atomic `WriteAllText` had a crash
  window where the `acted_on_at` stamp could be lost (causing
  double-blocklist on the next hour).
- **Newsletter resilience.** A single arr failure during
  `fetch_all_calendars` no longer drops the entire Coming Soon section
  + everything downstream — degrades that section by 25% instead.
  Listmonk 4xx/5xx + TMDB 429 are now logged with the actual response,
  not just `HTTPError`.

### Stale-reference purge

The seedbox was cleaned 2026-05-11 (Readarr / Mylar3 / Jellyfin /
Jellyseerr / Jellystat / Conjurr / Newsletterr / Ombi all purged),
but the repo still carried install scripts + configure-time references
that would either fail re-install or inject dead artifacts. Resolved
by deleting 5 orphan install scripts + pruning references in 7 more.

### Observability + ergonomics

- **`Manitoba Pusher` self-heartbeat monitor.** Pusher pushes status=up
  to a dedicated Kuma monitor each cycle; a pusher crashloop now
  surfaces as a single dead-man instead of "every app went down at
  once". Bootstrap script provisions the monitor + token.
- **MCP staleness signal.** `qflix_status` returns
  `snapshot_age_minutes` + `stale_warning` so callers detect a
  suspended/offline collector without parsing `captured_at` by hand.
- **`qflix_list_torrents` gains `state` / `category` / `stale_only`
  filters.** Unfiltered call on a 200-torrent farm dumps ~50KB JSON
  into context; filters reduce that to the diagnostic-relevant subset.
- **Smoke + canary correctness.** `smoke-test-plex.sh` no longer
  permafail on the purged Newsletterr/Conjurr probes. New
  `smoke-test.sh:13m` check covers vlogs-ingest timer + last Result.
  `qbit-stall` canary no longer requires `UP_SPEED=0` (partial libtorrent
  wedges seed but don't download). `vlogs-stall` canary now fails on any
  single stale app, not only when all four *arr are stale.

### Doc reconciliation

- README badges + at-a-glance + both Mermaid diagrams updated to the
  real numbers (33 apps, 34 Kuma monitors, 6 canaries).
- `operator-deferred.md` purged of the dead Newsletterr UI section and
  the superseded Listmonk cutover entry. Phase 16 uninstall block
  updated to reflect the 7-day hold ending today (2026-05-16).
- `Tuesday.md` marked SUPERSEDED (the cross-class upgrade sweep was
  replaced by `app-upgrade-all.sh` 2026-05-13).
- `secrets-convention.md` rewritten — documents the actual active
  secret inventory + the purged set.

### Test count

414 → 440 (+26). New regression coverage for shell injection, recovery
permanent-failure mark, health probe fail-loud paths, manifest-load
validation, MCP filters + staleness, fetch_all_calendars per-arr
resilience, and the previously-untested `qflix_newsletter/sync.py`
Listmonk template uploader.

### Known follow-ups (not blocking this release)

- `test_kuma.py` has 6 `time.sleep(0.05–0.1)` waits that are flaky on
  loaded CI runners. Replace with `threading.Event`/`Queue` deterministic
  awaits when the test re-architecture lands.
- Hardlink-integrity canary + Plex transcoder canary not yet
  implemented — flagged in the audit as missing coverage.
- `plex.py` self-test mode (per audit) deferred.
- REA all-models-timeout dead-man alert (PowerShell, requires live
  Ollama integration to validate) deferred.
