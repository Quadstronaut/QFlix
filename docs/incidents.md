# Incidents

Operator-facing incident log for the QFlix stack. Newest first.

User-facing summaries are posted as **Uptime Kuma status-page incidents**
(status page slug `public`, "QFlix Status Page") so subscribers see plain-language
updates; this file keeps the full technical record. Keep the two in sync: when an
incident opens or resolves here, post/update the matching Kuma incident.

Severity scale: **P1** = user-visible outage or data-loss risk · **P2** = degraded
/ single non-critical service · **P3** = cosmetic or internal-only.

---

## 2026-05-20 → ongoing — Tautulli outage during Ultra.cc kernel maintenance

- **Severity:** P1 (single-service outage; **streaming unaffected**)
- **Status:** Mitigated, awaiting provider — Tautulli down, config fixed, auto-recovery watcher armed
- **Components:** Tautulli (down) · UCC app-lifecycle CLI (gated platform-wide) · Plex (healthy, re-IP'd)
- **User impact:** Watch-history/stats unavailable; newsletter "recently added / most watched" data stale. Plex streaming, libraries, and playback **unaffected**.
- **Kuma status-page incident:** id 1, style `warning`, posted 2026-05-24 03:45 UTC.

### Timeline (UTC)

| When | Event |
|---|---|
| **2026-05-20** | Ultra.cc begins batched Linux-kernel-upgrade maintenance (daily 09:00–21:00 UTC windows). Plex container re-IP'd from `172.17.1.250:32400` → docker bridge gateway `172.17.0.1:17025`. Tautulli, pinned to the old IP, begins storming `[Errno 111] Connection refused` against Plex. **141,688** errors this day (0 the day prior). |
| **2026-05-21** | ~**194,741** connection-refused errors. |
| **2026-05-22** | ~**367,843** connection-refused errors (peak). |
| **2026-05-23 ~15:50** | Outage discovered during environment audit. Diagnosed Plex re-IP + Tautulli stale pin. Kuma did **not** alert — the Tautulli monitor probes only its own web port, which stayed up (→ follow-up #2). |
| **2026-05-23 16:28:58** | Tautulli stopped to apply the config fix. `app-tautulli start` then refused: `{"result": false, … "no longer available due to maintenance"}` — Ultra.cc had gated app-lifecycle commands. Tautulli now fully down and unrestartable operator-side. |
| **2026-05-24 ~03:42** | Gate confirmed still up; Plex confirmed healthy at the gateway; config already re-pinned. One-shot watcher armed (`scripts/ops/tautulli-gate-watch.sh`, runs on box) to auto-start Tautulli the instant the gate lifts, verify the Plex link, and ping Discord. Support ticket filed with Ultra.cc. |

### Root cause

Two compounding provider-side changes during maintenance:

1. **Plex container re-IP** — Plex moved from `172.17.1.250:32400` to the docker
   bridge gateway `172.17.0.1:17025`. Tautulli's pinned `pms_url` broke; it could no
   longer reach Plex. (The `50-tautulli-pms-url-fix.sh` configure step re-pins
   whatever IP is already in config, so it did **not** self-correct this drift.)
2. **UCC lifecycle CLI gated** — `app-<slug> start|stop|restart` return the
   maintenance refusal while read ops (`version`) still work. With no operator-side
   way to start a stopped UCC app (docker socket permission-denied, `app-manager.py`
   sudo-only), the stopped Tautulli could not be restarted **and platform auto-heal
   became a no-op** for the duration.

Compounding operator error: Tautulli was *stopped* to apply the fix during an active
maintenance window — see operator memory `ultracc-may2026-migration` ("don't stop a
UCC app mid-maintenance; edit config in place and let it pick up on the next
sanctioned restart").

### Remediation

- Tautulli `config.ini` re-pinned to the gateway: `pms_ip=172.17.0.1`,
  `pms_port=17025`, `pms_url=http://172.17.0.1:17025`, `pms_ssl=0`,
  `pms_url_manual=1`. Backup: `~/.apps/tautulli/config.ini.bak.1779553737`.
- Watcher `scripts/ops/tautulli-gate-watch.sh` armed on box (one-shot, deletable):
  polls every 5 min, auto-starts Tautulli on gate-lift, verifies web + Plex, pings
  Discord. Stop with `pkill -f tautulli-gate-watch.sh`; log at
  `~/.opt/maint/tautulli-watch.log`.
- Support ticket open with Ultra.cc (restore lifecycle CLI; confirm Plex address
  stability).

### Follow-ups

- **#2** Tautulli monitor must probe Plex connectivity, not just its own web port
  (the reason this outage never alerted).
- **#5** After maintenance lifts: confirm Tautulli started + storm gone (watcher
  handles the start; verify the error count drops to ~0).
- Operator memory: `ultracc-may2026-migration`, `qbit-max-ratio-backlog-hazard`.

### Related (same maintenance window, separate issue)

**qBittorrent runaway upload.** Noticed 2026-05-23: 22.17 TB uploaded vs 4.20 TB
down (ratio 5.28) against a 24 TB monthly cap — pure seeding, one torrent at ratio
771 (~7.55 TB). Global share-ratio limit set to **2.0 / pause** on 2026-05-24.
Enabling the limit against the over-ratio backlog mass-removed ~25 torrents and
deleted their download copies; **library intact via hardlinks** (verified). No user
impact. See `qbit-max-ratio-backlog-hazard`.
