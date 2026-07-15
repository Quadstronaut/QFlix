# QFlix Heartbeat v2 — Android health dashboard (design)

**Date:** 2026-07-15 · **Status:** approved (operator, this session)
**Replaces:** `com.qflix.heartbeat.debug` (old app, source not retained; uninstalled from phone)

## Purpose

Personal (non-distributed) Android app giving the operator an at-a-glance health
overview of the QFlix stack on the manitoba seedbox. Read-only. Fetch on open +
pull-to-refresh. No background work, no widget, no notifications (Discord already
pages on error).

## Architecture

```
[Android app] --SSH (dedicated ed25519 key, forced command)--> [seedbox]
                                                                 app-status.py --> one JSON doc
```

- **Transport:** SSH direct to `seedbox.example.com` as `quadstronaut`. Dedicated
  keypair minted for the phone. `authorized_keys` entry pins
  `command="~/scripts/mcp/app-status.py",no-pty,no-X11-forwarding,no-agent-forwarding,no-port-forwarding,restrict`
  — the key can only emit the health JSON. Read-only by construction.
- **Key storage:** private key in app-private storage (EncryptedSharedPreferences /
  filesDir), provisioned once via adb. Not baked into APK assets.

## Server side — `scripts/mcp/app-status.py`

Single aggregator, stdlib-only Python, target <5 s wall, emits one JSON doc:

| Section | Source | Content |
|---|---|---|
| `quota` | Ultra.cc disk quota (same source smoke #6 uses) + Ultra traffic counter | disk: used/total GB + %; bandwidth: used/total GB + % |
| `kuma` | `~/.apps/uptimekuma/kuma.db` (SQLite, read-only) | up/down/total counts, red monitor names + last msg + since |
| `streams` | Tautulli `get_activity` | stream_count, user_count → fraction `streams/users` |
| `top5.requests` | Seerr API, requests last 30 d grouped by user | top 5 users by request count |
| `top5.watch` | Tautulli `get_home_stats` (30 d, top_users, duration) | top 5 users by watch time |
| `downloads` | qBittorrent API + SABnzbd API + collector `stale-state.json` | active/stalled/errored counts, queue sizes, stuck list, recent auto-unstick history |
| `alerts` | derived: kuma reds + quota>90 % + stuck>0 + recent auto-heal events (maint `state.json`) | one flat prioritized list |
| `meta` | — | generated_at, elapsed_ms, per-section `ok`/`error` |

Per-section failure isolation: a dead source yields `{"ok": false, "error": "..."}`
for that section only; the rest of the doc still renders.

Deployed by an idempotent `scripts/configure/` installer per repo convention
(rsync script + mint key + patch authorized_keys, re-runnable).

## Android app — `QFlix/apps/heartbeat-android/`

- Kotlin + Jetpack Compose, Material 3 dark theme, single activity/screen.
- sshj client (ed25519). Fetch = one exec channel call, parse JSON.
- Layout top→bottom:
  1. **Alert banner** — green "all clear" or red list from `alerts`
  2. **Quota bars** — disk + bandwidth, % and GB numbers on each bar
  3. **Kuma** — `N/M up`, red monitors listed with msg
  4. **Streams + top-5s** — fraction `streams/users` (fractional >1 ⇒ multi-stream
     users), two columns: requests 30 d, watch time 30 d
  5. **Downloads** — qBit/SAB counts, stuck items, recent unsticks
- Per-section error chips; data-age timestamp in header; pull-to-refresh
  (Compose `PullToRefreshBox`).
- No secrets in repo: key + host config live on-device; `local.properties`-style
  provisioning documented in app README.

## Testing

- Server: unit tests for parsers/aggregation (repo `tests/` convention), live
  smoke on box (`app-status.py | jq .meta`).
- App: JUnit tests for JSON parsing/view-state mapping; end-to-end = adb install
  on the phone, live fetch against the box, screenshot verification.

## Out of scope (explicit)

Write actions (unstick etc.), widgets, background polling, alert push, multi-server
support, Play distribution.
