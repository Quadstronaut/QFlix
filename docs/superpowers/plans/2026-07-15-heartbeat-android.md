# QFlix Heartbeat v2 Implementation Plan

> **For agentic workers:** executed via ultracode Workflow orchestration (operator-approved this session); each task below = one subagent with this plan + spec as context. Steps use checkbox syntax.

**Goal:** Read-only Android health dashboard fetching one JSON doc from the seedbox over a forced-command SSH key.

**Architecture:** `scripts/mcp/app_status.py` (seedbox aggregator, stdlib py3.9) ← forced-command SSH ← Kotlin/Compose app on Pixel 6. Spec: `docs/superpowers/specs/2026-07-15-heartbeat-android-design.md`.

**Tech stack:** Python 3.9 stdlib · sqlite3 CLI-free (python sqlite3 module, read-only URI) · Kotlin + Jetpack Compose (M3), sshj + BouncyCastle, kotlinx-serialization · Gradle wrapper 8.13 + AGP 8.9.x + JDK17 · minSdk 31, compile/target 36.

## Global constraints

- Box python is **3.9.2** — no 3.10+ syntax. `from __future__ import annotations`.
- Server file is `scripts/mcp/app_status.py` (underscore, importable by pytest — deviation from spec's `app-status.py`, deliberate).
- JSON out: `json.dump(result, sys.stdout, default=str)` + `\n`; diagnostics → stderr. Repo convention.
- Per-section failure isolation: every section carries `"ok": bool`, `"error": str|null`; one dead source never kills the doc.
- Secrets from `~/secrets/<slug>.<field>` via `scripts/maint/lib/secrets.py` `read_secret()`; sys.path bootstrap identical to `collect.py:30-35`.
- Target <5 s wall; sections fetched concurrently (ThreadPoolExecutor, like collect.py).
- Tests: `tests/unit/test_mcp_app_status.py`, pure functions only, run `bash tests/run.sh -q`. CI gates master.
- Installer: `scripts/configure/71-heartbeat-status-install.sh` (verify 71 free at write time), tar-over-ssh via `scripts/lib/ssh.sh` (`sshm`/`scpm_to`), idempotent, ends with pass/fail gate.
- Commit + push at every green checkpoint (standing rule). Branch: master.
- No secrets or private keys ever committed. `.gitignore` already covers `secrets/`.

## Verified data contracts (live recon 2026-07-15)

| Source | Access | Parse |
|---|---|---|
| Disk quota | `quota -s` subprocess | row `/dev/…`: used=2073G quota=2794G → pct |
| Bandwidth | `app-traffic info` subprocess | `Traffic available: 96.58%` + `Last/Next traffic reset:` lines. **No GB numbers exist for user accounts** — bar shows % used + reset dates |
| Kuma | python `sqlite3.connect("file:...kuma.db?mode=ro", uri=True)` | `SELECT m.name, h.status, h.msg, h.time FROM monitor m JOIN heartbeat h ON h.id=(SELECT id FROM heartbeat WHERE monitor_id=m.id ORDER BY time DESC LIMIT 1) WHERE m.active=1` → 55 total, status 0/1 (2=pending 3=maint possible) |
| Tautulli | `GET http://127.0.0.1:{tautulli.port}/tautulli/api/v2?apikey={tautulli.key}&cmd=get_activity` | `response.data.stream_count` (str!), distinct users = set of `sessions[].user_id` |
| Tautulli top | `...&cmd=get_home_stats&time_range=30&stats_type=duration&stat_id=top_users` | `response.data.rows[]`: `friendly_name`, `total_plays`, `total_duration` (sec) |
| Seerr | `GET http://127.0.0.1:{seerr.port}/api/v1/request?take=200&sort=added` header `X-Api-Key: {seerr.key}` | filter `createdAt` ≥ now−30d; group by `requestedBy.id`, label `displayName` (fallback `plexUsername`); paginate via `pageInfo` if `results` > take |
| qBittorrent | reuse `scripts/mcp/lib/qbit_client.py` `QbitClient.login()/list_torrents()` | count `state`: active=`downloading|forcedDL`, stalled_dl=`stalledDL`, errored=`error|missingFiles`, stopped_dl=`stoppedDL|pausedDL` |
| SABnzbd | `GET http://127.0.0.1:{sabnzbd.port}/api?mode=queue&output=json&apikey={sabnzbd.key}` | `queue.paused`, `queue.noofslots`, `queue.mbleft`/`mb`, `queue.kbpersec` |
| Stuck | `~/.opt/qflix-collect/stale-state.json` | `hashes{<hash>: {consecutive_zero_hours, rule_matched, candidate_for_unstick, acted_on_at}}`; join hash→torrent name via qbit list |
| Unstick history | `~/.opt/qflix-collect/events/<YYYY-MM-DD>.jsonl` (today + yesterday) | lines with `action=="unstick"`: ts, result, hash |
| Auto-heal | `~/.opt/maint/state.json` | `apps{slug: {event, final_health, updated_at}}`; alert if `event=="failed"` within 48 h |

## Output JSON contract (server produces, app consumes)

```json
{
  "meta":   {"generated_at": "2026-07-15T20:00:00Z", "elapsed_ms": 2100, "host": "manitoba", "version": 1},
  "quota":  {"ok": true, "error": null,
             "disk": {"used_gb": 2073, "total_gb": 2794, "pct": 74.2},
             "bandwidth": {"used_pct": 3.42, "available_pct": 96.58,
                           "last_reset": "2026-06-28T00:00:00", "next_reset": "2026-07-28T00:00:00"}},
  "kuma":   {"ok": true, "error": null, "total": 55, "up": 51, "down": 4,
             "red": [{"name": "QFlix Reaper", "msg": "No heartbeat in the time window", "since": "2026-07-15 18:02:11"}]},
  "streams":{"ok": true, "error": null, "streams": 3, "users": 2, "transcodes": 1, "wan_kbps": 12000},
  "top5":   {"ok": true, "error": null,
             "requests_30d": [{"user": "sarahvanpelt", "count": 12}],
             "watch_30d":    [{"user": "BAsylum", "hours": 78.2, "plays": 105}]},
  "downloads": {"ok": true, "error": null,
             "qbit": {"total": 12, "active": 1, "stalled_dl": 0, "errored": 0, "seeding": 10},
             "sab":  {"queued": 1, "paused": false, "mb_left": 4203.9, "mb_total": 4801.7, "kbps": 0},
             "stuck": [{"hash8": "f0a3658d", "name": "…", "hours": 3, "rule": "stalledDL", "acted": true}],
             "recent_unsticks": [{"ts": "2026-07-15T19:00:39Z", "hash8": "f0a3658d", "result": "qbit-orphan-removed"}]},
  "alerts": [{"level": "crit", "text": "Kuma down: QFlix Reaper — No heartbeat in the time window"}]
}
```

Alert derivation (ordered crit→warn): each Kuma red = crit (except the two `Quadstronix Node*` externals — those + their parent roll into one line); disk ≥90 % crit / ≥80 % warn (mirrors quota canary); bandwidth available <10 % crit / <20 % warn; maint `event=="failed"` <48 h = crit; stuck `candidate_for_unstick` count >0 = warn; SAB paused = warn. Empty array = all clear.

## Tasks

### S1 — server aggregator (TDD)
**Files:** create `scripts/mcp/app_status.py`, `tests/unit/test_mcp_app_status.py`.
**Produces:** `run(sections=None) -> dict` matching contract; `main()` with `--emit-json` default when invoked bare (forced-command has no args); pure parsers `parse_quota(text)`, `parse_traffic(text)`, `classify_qbit(torrents)`, `top5_requests(requests_json, now)`, `top5_watch(rows)`, `derive_alerts(doc)` — each unit-tested with fixtures lifted verbatim from the recon samples above.
- [ ] failing tests for every parser + alert derivation (incl. section-failure isolation: dead tautulli ⇒ streams.ok=false, doc still complete)
- [ ] implement; `bash tests/run.sh -q` green
- [ ] commit `feat(mcp): app_status.py aggregator for Heartbeat v2`

### S2 — installer + key mint
**Files:** create `scripts/configure/71-heartbeat-status-install.sh`.
**Behavior (idempotent, re-runnable):** deploy `scripts/mcp/` tar-over-ssh (same as 70); on box: `ssh-keygen -t ed25519 -N '' -C qflix-heartbeat-phone -f ~/.ssh/heartbeat_phone_ed25519` only if missing; ensure `authorized_keys` line `command="python3 /home/quadstronaut/scripts/mcp/app_status.py",restrict <pubkey>` via `grep -qF` guard; `chmod 600`; gate: run `ssh -i` self-test from workstation? (gate runs box-side: `python3 ~/scripts/mcp/app_status.py | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["meta"]["version"]==1'` + authorized_keys entry present). Also `rm -f ~/nul` (recon artifact cleanup).
- [ ] write installer, run it, gate green
- [ ] workstation verify forced channel: `ssh -i <tmp copy of key> quadstronaut@seedbox.example.com anything` → returns status JSON only (command override proof)
- [ ] capture live JSON → `tests/fixtures/app_status_live.json` (scrub nothing — no secrets in doc; usernames fine, personal app) for app-side tests
- [ ] commit `feat(configure): 71 heartbeat status installer + forced-command phone key`

### A1 — Android scaffold
**Files:** create `apps/heartbeat-android/` (settings.gradle.kts, gradle wrapper 8.13, app module, manifest `com.qflix.heartbeat`, Compose M3 dark theme, empty screen).
Deps: compose BOM, material3, lifecycle-viewmodel-compose, kotlinx-serialization-json, sshj 0.38.0 + bcprov-jdk18on (Security provider swap at app start), material pull-refresh (M3 `PullToRefreshBox`).
- [ ] `gradlew.bat assembleDebug` green; `adb install -r` on Pixel 6 launches
- [ ] commit `feat(app): heartbeat-android scaffold`

### A2 — model + view-state (TDD)
**Files:** create `model/StatusDoc.kt` (kotlinx-serialization mirror of contract, all sections nullable-tolerant), `model/ViewState.kt` (per-section render states, fraction text `"streams/users"`, hours formatting, alert ordering), `test/` JVM tests against `app_status_live.json` fixture + hand-built degraded docs.
- [ ] tests fail → implement → `gradlew.bat testDebugUnitTest` green
- [ ] commit `feat(app): status model + view-state mapping`

### A3 — SSH fetcher
**Files:** create `net/SshFetcher.kt` (sshj: load ed25519 key from `filesDir/keys/phone_key`, host key from `filesDir/keys/known_host` — verify against `OpenSSHKnownHosts`-style pin; exec empty command, read stdout, 15 s timeouts), `net/Provisioning.kt` (import bundle from `filesDir/provision/`), plus `provision.ps1` at app root: box-side key check → pull key+hostkey via scp to `$env:TEMP` → `adb push` → `adb shell run-as com.qflix.heartbeat cp` into filesDir → shred temp + **delete private key from box** (pubkey line in authorized_keys stays).
- [ ] unit test: fetcher parses stdout via A2 model (fake transport)
- [ ] commit `feat(app): ssh fetcher + provisioning`

### A4 — dashboard UI
**Files:** create `ui/Dashboard.kt` + section composables (AlertBanner, QuotaBars — disk `used/total GB` label, bandwidth `% used + resets <date>` label —, KumaCard, StreamsCard with big `streams/users` fraction, two Top5 lists, DownloadsCard), `MainActivity` + `StatusViewModel` (fetch on start, PullToRefreshBox, per-section error chips, data-age in top bar).
- [ ] `assembleDebug` + install + renders fixture via preview/fake repo
- [ ] commit `feat(app): dashboard UI`

### A5 — live E2E on phone
- [ ] run `provision.ps1` (mints nothing new; transfers key bundle; deletes box private key + temp copies)
- [ ] launch app on Pixel 6 → live fetch OK; pull-to-refresh OK
- [ ] `adb exec-out screencap -p > shot.png` → operator-visible verification of all 5 sections
- [ ] kill one source assumption test: airplane-mode toggle → clean error state (no crash)
- [ ] commit `chore(app): live E2E verified on Pixel 6`

### R — adversarial review
- [ ] workflow verify pass over S1/S2 (security: forced-command really constrains; key perms; no secret leak into repo/logcat) + app correctness; fix confirmed findings; final commit + push

## Self-review

Spec coverage: quota bars ✓ (bandwidth = % only, hard platform limit, spec amended by this plan) · kuma ✓ · streams fraction ✓ · top5 ×2 ✓ · downloads/stuck/unstick ✓ · alerts ✓ · read-only ✓ (forced command, key deleted from box after transfer) · pull-to-refresh ✓ · per-section chips ✓ · old app uninstalled ✓ (done this session). Types consistent: contract JSON is single source, fixture-driven both sides. No placeholders: contracts + exact commands above; implementation code authored by task agents under TDD gates.
