# Tdarr Install Plan (Manitoba)

> **For agentic workers:** Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax.

**Goal:** Install Tdarr (server + local node) on the Manitoba seedbox to normalize the existing media library to Direct-Play-friendly H.264/AAC + HEVC for disk savings, then transition to "process new arrivals only" steady-state.

**Architecture:** Tdarr Server (orchestrator + Web UI) + Tdarr Node (FFmpeg/HandBrake worker), both running locally as user-systemd services. Path-based nginx exposure under `quadstronaut.seedbox.example.com/tdarr/` with htpasswd protection. CPU-only transcoding (no GPU on seedbox).

**Non-goals:**
- Streaming Tdarr to remote nodes (single-host install).
- GPU acceleration.
- Real-time transcoding for Plex playback (Plex handles that itself).

---

## Probe findings (verified 2026-05-08, applicable to this plan)

| Fact | Value | Source |
|---|---|---|
| Public exposure model | **path-based only for custom installs** — `https://quadstronaut.seedbox.example.com/tdarr/`. No subdomains for unofficial apps. | [Ultra.cc generic-software-installation docs](https://docs.ultra.cc/unofficial-application-installers/generic-software-installation) |
| Port allocation | Use ports from `app-ports free` only. Out-of-range = Fair Use violation. Pre-claimed for this plan: **42018 (Tdarr server UI), 42019 (Tdarr server-node API)**. | `app-ports show` / `app-ports free` |
| nginx reload | `systemctl --user reload nginx` (the documented `app-nginx restart` is not implemented — only backup/install/migrate/repair). | live probe |
| Public host | `quadstronaut.seedbox.example.com` → internal nginx :17040, server block at `~/.apps/nginx/sites-available/default`, `include proxy.d/*.conf`. | `~/.apps/nginx/sites-available/default` |
| Disk free | 11T / 20T (45% used). Plenty of room for transcode cache + temp files. | `df -h ~` |
| Architecture | linux_x64. | `uname -m` |
| Available system packages | `ffmpeg` ships with Tdarr (no install needed). `mkvtoolnix` and `handbrake-cli` would normally come from `apt-get install`, but **no sudo on Ultra.cc**. We'll use Tdarr's bundled FFmpeg and forgo mkvpropedit/HandBrake unless statically-linked binaries can be sourced. | `app-` installer list does not include them |
| Fair Use | "Our services run on shared resources... Ultra.cc may stop, without warning, any applications that are negatively affecting other clients." Operator's read: post-normalization steady-state is fine; first-run normalization burst will be throttled. | [Ultra.cc ToS](https://ultra.cc/policies/terms-of-service) |

## Conventions

- `SSHM` = `ssh -o BatchMode=yes quadstronaut@seedbox.example.com`. Defined in `scripts/lib/ssh.sh`.
- All installs idempotent. Re-running a phase must not double-install or break the running service.
- New ports allocated via `app-ports free`; default 42018+42019 for Tdarr server, dynamic re-claim if taken.
- **Pin versions.** Per `feedback_pin-app-versions` memory: pin Tdarr to a specific release. v2.45.01 (latest stable as of 2026-05-08) — adjust if a newer pin is preferred.
- All commits include `Co-Authored-By: Claude Opus 4.7`.

---

## Phase 27 — Tdarr Server install

### Task 27.1: Allocate ports + capture pin

**Files:**
- Create: `scripts/configure/50-tdarr-server-install.sh`

- [ ] **Step 1: Pin Tdarr version + claim ports**

```bash
TDARR_VER="2.45.01"  # pinned per feedback_pin-app-versions
TDARR_URL="https://f000.backblazeb2.com/file/tdarrs/versions/${TDARR_VER}/linux_x64/Tdarr_Updater.zip"

# Claim 2 ports from app-ports free (server UI + server-to-node API).
if ! secret_exists tdarr.webui_port; then
  PORT=$(sshm "app-ports free 2>/dev/null | grep -E '^[0-9]+$' | head -1")
  [ -n "$PORT" ] || die "no free port from app-ports"
  secret_write tdarr.webui_port "$PORT"
fi
if ! secret_exists tdarr.server_port; then
  USED=$(secret_read tdarr.webui_port)
  PORT=$(sshm "app-ports free 2>/dev/null | grep -E '^[0-9]+$'" | grep -vxF "$USED" | head -1)
  secret_write tdarr.server_port "$PORT"
fi
```

### Task 27.2: Download + install binaries

- [ ] **Step 1: Use `Tdarr_Updater` to fetch the pinned version**

Tdarr ships a self-updater that downloads the matched server + node binaries together. Easier than chasing each release asset.

```bash
sshm 'bash -s' <<'EOF'
set -euo pipefail
mkdir -p ~/.apps/tdarr/{server,node,configs,logs,transcode_cache}
cd ~/.apps/tdarr
if [ ! -x ./Tdarr_Updater ]; then
  curl -fsSL "https://f000.backblazeb2.com/file/tdarrs/versions/2.45.01/linux_x64/Tdarr_Updater.zip" -o updater.zip
  unzip -o updater.zip
  chmod +x Tdarr_Updater
  rm updater.zip
fi
./Tdarr_Updater  # downloads Server + Node binaries into ./Tdarr_Server/ and ./Tdarr_Node/
ls -la Tdarr_Server/Tdarr_Server Tdarr_Node/Tdarr_Node
EOF
```

If `unzip` is missing on the seedbox, fall back to direct asset download from the GitHub mirror or HomeAssistant Community archives.

### Task 27.3: Server config + auth + base URL

**Files:**
- Server config: `~/.apps/tdarr/configs/Tdarr_Server_Config.json`

- [ ] **Step 1: Generate API key + write config**

```bash
WEBUI_PORT=$(secret_read tdarr.webui_port)
SERVER_PORT=$(secret_read tdarr.server_port)
HTPW=$(secret_read htpasswd.password)

# Tdarr API tokens must start with 'tapi_' and be ≥14 chars.
if ! secret_exists tdarr.api_key; then
  secret_write tdarr.api_key "tapi_$(openssl rand -hex 16)"
fi
API_KEY=$(secret_read tdarr.api_key)

sshm "cat > ~/.apps/tdarr/configs/Tdarr_Server_Config.json" <<JSON
{
  "serverIP": "127.0.0.1",
  "serverPort": ${SERVER_PORT},
  "webUIPort": ${WEBUI_PORT},
  "openBrowser": false,
  "auth": true,
  "seededApiKey": "${API_KEY}",
  "handbrakePath": "",
  "ffmpegPath": "",
  "mkvpropeditPath": "",
  "ccextractorPath": "",
  "maxLogSizeMB": 10,
  "cronPluginUpdate": "0 4 * * 0"
}
JSON
```

- [ ] **Step 2: Web UI base path** — Tdarr supports `webUIBaseUrl` setting (set after first start in the UI's Options tab). For now, leave at default; add `/tdarr` once the nginx fragment is in place (Task 27.5).

### Task 27.4: User-systemd service + heartbeat

**Files:**
- Service: `~/.config/systemd/user/tdarr-server.service`
- Heartbeat: `scripts/ops/heartbeat-tdarr-server.sh`

- [ ] **Step 1: Server unit**

```bash
sshm "cat > ~/.config/systemd/user/tdarr-server.service" <<'UNIT'
[Unit]
Description=Tdarr Server (transcoding orchestrator)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.apps/tdarr
ExecStart=%h/.apps/tdarr/Tdarr_Server/Tdarr_Server
Restart=on-failure
RestartSec=10s
TimeoutStopSec=30
StandardOutput=append:%h/.apps/tdarr/logs/server.log
StandardError=append:%h/.apps/tdarr/logs/server.err

[Install]
WantedBy=default.target
UNIT
sshm 'systemctl --user daemon-reload && systemctl --user enable --now tdarr-server.service'
sleep 5
sshm 'systemctl --user is-active tdarr-server.service' | grep -q active || die "tdarr-server not active"
```

- [ ] **Step 2: Heartbeat cron**

```bash
cat > scripts/ops/heartbeat-tdarr-server.sh <<'EOF'
#!/usr/bin/env bash
PORT=$(grep -oP '"webUIPort":\s*\K[0-9]+' ~/.apps/tdarr/configs/Tdarr_Server_Config.json)
curl -sfm 5 "http://127.0.0.1:${PORT}/api/v2/status" >/dev/null && exit 0
systemctl --user is-active tdarr-server.service >/dev/null && exit 0
logger -t tdarr-server-heartbeat "tdarr-server unhealthy — restarting"
systemctl --user restart tdarr-server.service
EOF
scpm_to scripts/ops/heartbeat-tdarr-server.sh '~/scripts/ops/heartbeat-tdarr-server.sh'
sshm 'chmod +x ~/scripts/ops/heartbeat-tdarr-server.sh && (crontab -l | grep -v heartbeat-tdarr-server; echo "*/5 * * * * /home/quadstronaut/scripts/ops/heartbeat-tdarr-server.sh") | crontab -'
```

### Task 27.5: nginx path fragment (htpasswd-protected)

- [ ] **Step 1: Drop fragment**

```bash
PORT=$(secret_read tdarr.webui_port)
sshm "cat > ~/.apps/nginx/proxy.d/tdarr.conf" <<EOF
location /tdarr/ {
    # Inherit htpasswd from parent server block (admin-only). Tdarr's auth=true
    # provides defense in depth on the API itself.
    proxy_pass http://127.0.0.1:${PORT}/;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-Prefix /tdarr;

    # WebSockets — Tdarr Web UI streams job updates live
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 1d;
    proxy_send_timeout 1d;
    proxy_buffering off;
}
EOF
sshm '/usr/sbin/nginx -p ~/.apps/nginx/ -c nginx.conf -t && systemctl --user reload nginx'
```

- [ ] **Step 2: Set Web UI base path in Tdarr** — Web UI → Options → Web UI Base URL = `/tdarr`. Save. Server restarts automatically.

### Task 27.6: Verify

- [ ] **Step 1: Web UI reachable**

```bash
curl -sIk -u "quadstronaut:$(secret_read htpasswd.password)" "https://quadstronaut.seedbox.example.com/tdarr/"
# Expect HTTP 200
```

- [ ] **Step 2: API reachable with token**

```bash
curl -sk -H "x-api-key: $(secret_read tdarr.api_key)" \
     "https://quadstronaut.seedbox.example.com/tdarr/api/v2/status" | jq .
```

---

## Phase 28 — Tdarr Node install (local worker)

### Task 28.1: Node config

**Files:**
- Node config: `~/.apps/tdarr/configs/Tdarr_Node_Config.json`

- [ ] **Step 1: Write config**

```bash
SERVER_PORT=$(secret_read tdarr.server_port)
API_KEY=$(secret_read tdarr.api_key)

sshm "cat > ~/.apps/tdarr/configs/Tdarr_Node_Config.json" <<JSON
{
  "nodeName": "manitoba-local-node",
  "serverURL": "http://127.0.0.1:${SERVER_PORT}",
  "apiKey": "${API_KEY}",
  "nodeType": "mapped",
  "priority": -1,
  "pollInterval": 2000,
  "startPaused": true,
  "handbrakePath": "",
  "ffmpegPath": "",
  "mkvpropeditPath": "",
  "maxLogSizeMB": 10,
  "transcodecpuWorkers": 1,
  "transcodegpuWorkers": 0,
  "healthcheckcpuWorkers": 1,
  "healthcheckgpuWorkers": 0,
  "cronPluginUpdate": "0 4 * * 0"
}
JSON
```

**Critical**: `startPaused: true` and `transcodecpuWorkers: 1` — single worker, paused on start. The operator manually unpauses to begin processing. This prevents Tdarr from going feral on boot and burning all CPU at once.

### Task 28.2: User-systemd service

```bash
sshm "cat > ~/.config/systemd/user/tdarr-node.service" <<'UNIT'
[Unit]
Description=Tdarr Node (local worker)
After=tdarr-server.service network-online.target
Wants=tdarr-server.service network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.apps/tdarr
ExecStart=%h/.apps/tdarr/Tdarr_Node/Tdarr_Node
Restart=on-failure
RestartSec=10s
StandardOutput=append:%h/.apps/tdarr/logs/node.log
StandardError=append:%h/.apps/tdarr/logs/node.err

[Install]
WantedBy=default.target
UNIT
sshm 'systemctl --user daemon-reload && systemctl --user enable --now tdarr-node.service'
sleep 5
sshm 'systemctl --user is-active tdarr-node.service' | grep -q active || die "tdarr-node not active"
```

### Task 28.3: Verify node connects to server

- [ ] **Step 1: From the Tdarr Web UI, navigate to Nodes tab. The "manitoba-local-node" should appear with status "Paused" (since `startPaused: true`).**

If not appearing: check `~/.apps/tdarr/logs/node.err` for connection errors. Most common cause: API key mismatch.

---

## Phase 29 — Plugin / library / rule configuration (operator)

This phase is entirely Web UI work. No scripts. The plan documents what the operator should configure.

### Task 29.1: Add the Plex library as a Tdarr library

- [ ] In the Web UI: **Libraries → Add new library**.
  - **Name**: `Manitoba Plex Library`
  - **Source**: `/home/quadstronaut/media` (or whichever Plex roots you want covered — start with `Movies` and `TV Shows` only; skip `Anime` and `Music` for now).
  - **Cache**: `/home/quadstronaut/.apps/tdarr/transcode_cache` (avoid filling Plex storage with temp files).
  - **Folder watch**: ON (auto-process new files).
  - **Process priority**: Low.

### Task 29.2: Configure the rule stack

- [ ] **Step 1: Use the community "Standardize to H.264 / AAC for Direct Play" plugin stack:**
  - `Tdarr_Plugin_MC93_Migz1FFMPEG` — convert to H.264 if not already
  - `Tdarr_Plugin_MC93_Migz4FFMPEG` — convert audio to AAC stereo
  - `Tdarr_Plugin_MC93_Migz5FFMPEG` — clean subtitles
  - **Optional for disk savings**: `Tdarr_Plugin_henk_Reorder_Streams` then a HEVC-conversion plugin (only on files larger than 8GB).

- [ ] **Step 2: Set the bitrate target conservatively** to avoid quality complaints — H.264 CRF 22, AAC 192k. Adjust per library.

- [ ] **Step 3: Schedule regular library scans** — *Disabled by default*. Manually trigger via Web UI for the first run. This is the biggest single CPU event in the plan; do it under operator supervision.

---

## Phase 30 — Throttled first-run normalization (high-watch)

The library is ~11TB. First-run normalization will touch every file. Even at 1 worker, this could take days. Run this with eyes on it.

### Task 30.1: Off-peak start

- [ ] **Step 1: Pick a low-usage window** — typically 22:00 local (Sunday night) when seedbox neighbors are quiet.
- [ ] **Step 2: In Tdarr Web UI: Nodes → manitoba-local-node → Unpause.** Library scan begins; jobs queue.
- [ ] **Step 3: First 30 minutes — operator watches:**
  - `top` / `htop` — Tdarr_Node should be the only heavy CPU process.
  - `iotop` — disk write rate should not saturate the disk (look for >50 MB/s sustained writes).
  - Notifiarr / `#notifiarr` — Plex playback for any user should not stutter.
- [ ] **Step 4: If concerns arise, pause** in Web UI and reduce to `transcodecpuWorkers: 0` (effectively pause), then debug.

### Task 30.2: Monitor for Ultra.cc warnings

- [ ] **Step 1: Email/dashboard check daily for the first week** — Ultra.cc may quietly throttle CPU or send a fair-use email.
- [ ] **Step 2: If a warning lands**: pause Tdarr immediately, reply to support, scale down `transcodecpuWorkers` to 1 + add cron-driven pause windows (e.g. only run 02:00-06:00).

### Task 30.3: Walk away when first-run completes

- [ ] **Step 1: Web UI: Statistics → "Files transcoded" should equal "Library size" eventually** (could take 1-3 weeks at 1 CPU worker for 11TB).
- [ ] **Step 2: Verify spot-check** — pick 5 random pre/post files, confirm Plex still streams them, audio still works, subtitles still render.
- [ ] **Step 3: Note disk savings** — `df -h ~` before/after, expect 15-30% library size reduction.

---

## Phase 31 — Steady-state ongoing mode

Post-normalization: Tdarr is mostly idle, only processing newly-arrived files (a few per day, minutes each). This is what fits "fair use" cleanly per the operator's read.

### Task 31.1: Reduce footprint

- [ ] **Step 1: Web UI: Library → Folder watch interval = 60 seconds** (was default).
- [ ] **Step 2: Confirm `transcodecpuWorkers: 1` stays at 1.** Going to 2+ on a shared seedbox is asking for fair-use enforcement.
- [ ] **Step 3: Optional — add cron-driven nightly pause** to be safe:

```bash
sshm "(crontab -l 2>/dev/null | grep -v tdarr-pause; cat) | crontab -"
sshm "(crontab -l; echo '0 7 * * * systemctl --user stop tdarr-node.service'; echo '0 22 * * * systemctl --user start tdarr-node.service') | crontab -"
```

This pauses Tdarr during peak streaming hours (07:00-22:00) and only runs overnight.

### Task 31.2: Document for operator

- [ ] **Step 1: Add Phase 31 entry to `docs/operator-deferred.md`** — note the steady-state config, cron-pause window, and the "if Ultra.cc warns, pause first, ask questions later" rule.

---

## Smoke test additions

Add three new tests to `scripts/smoke-test.sh`:

```bash
# 18. Tdarr Server health
echo "18. Tdarr Server"
HTPW=$(secret_read htpasswd.password)
TS_HTTP=$(curl -sIk -u "quadstronaut:$HTPW" -o /dev/null -w '%{http_code}' "https://quadstronaut.seedbox.example.com/tdarr/")
case "$TS_HTTP" in 200|302) record "tdarr-server-up" pass "HTTP $TS_HTTP" ;; *) record "tdarr-server-up" fail "HTTP $TS_HTTP" ;; esac

# 19. Tdarr Node connected
echo "19. Tdarr Node"
NODE_COUNT=$(curl -sk -H "x-api-key: $(secret_read tdarr.api_key)" "https://quadstronaut.seedbox.example.com/tdarr/api/v2/status" 2>/dev/null | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len([n for n in d.get("nodes",{}).values() if n.get("connected")]))' 2>/dev/null)
if [ "${NODE_COUNT:-0}" -ge 1 ]; then
  record "tdarr-node-connected" pass "$NODE_COUNT node(s) online"
else
  record "tdarr-node-connected" fail "no nodes connected"
fi

# 20. Tdarr CPU footprint (sanity — no more than 1 worker active)
echo "20. Tdarr CPU"
WORKERS=$(sshm "ps -ef | grep -E '(Tdarr_Node|Tdarr_Server)' | grep -v grep | wc -l")
[ "${WORKERS:-0}" -ge 2 ] && record "tdarr-procs" pass "$WORKERS Tdarr processes" || record "tdarr-procs" fail "expected ≥2, got $WORKERS"
```

After Phase 27-31 complete + mass-comms phases, smoke is 26/26.

---

## Rollback per phase

| Phase | If broken |
|---|---|
| 27 (Server) | `systemctl --user disable --now tdarr-server.service && rm -rf ~/.apps/tdarr ~/.config/systemd/user/tdarr-server.service ~/.apps/nginx/proxy.d/tdarr.conf && systemctl --user reload nginx`. Crontab strip the heartbeat. |
| 28 (Node) | `systemctl --user disable --now tdarr-node.service && rm ~/.config/systemd/user/tdarr-node.service`. Server keeps running. |
| 29 (Rules) | Tdarr libraries / plugin config edited in Web UI — revert via UI. State in `~/.apps/tdarr/Tdarr_Server/Server/serverDB/` (LokiJS). Backup before bulk changes: `cp -r ~/.apps/tdarr/Tdarr_Server/Server/serverDB/ ~/.apps/backup/tdarr-rules-$(date +%F)/`. |
| 30 (First run) | **Pause via Web UI**, set `transcodecpuWorkers: 0` in node config, restart node. Files already transcoded stay transcoded — no auto-revert. If a transcode broke a file, restore from `~/.apps/backup/postgres-…sql` if Postgres-backed metadata or from the original (non-overwritten) source if Tdarr was set to "non-destructive" (recommended). |
| 31 (Steady-state) | Strip the cron pause lines if you want Tdarr always-on. |

**Never** rollback a Phase 30 transcode by force-deleting transcoded files — Tdarr's database tracks state. Use the Web UI's "Re-process" function instead.

---

## Cost summary

- **Tdarr (free for personal use)**: $0. Tdarr Plus exists (paid tier) but not needed.
- **Disk usage**: ~10-30% library size reduction post-normalization (HEVC). On 11T → save ~1-3 TB.
- **CPU cost**: hard to quantify. First-run is the big spike (1-3 weeks at 1 worker on 11T). Steady-state is minutes per new file, ~20-30 files/day expected = ~hours/day total CPU.
- **Operator effort**: ~30 min initial setup in Tdarr UI (libraries + rules) + first-week vigilance during the normalization run + ~5 min/week steady-state.

---

## What this plan does NOT do

- **No GPU / hardware acceleration.** Seedbox has no GPU. Pure-CPU transcoding is the only option.
- **No multi-node / cluster.** Single host, single worker.
- **No Plex hardware-transcode replacement.** Plex still does live transcoding when a client asks for a profile Tdarr didn't pre-bake. Tdarr's job is to make Direct Play work for the *common* profiles so that doesn't happen often.
- **No automated rollback of broken transcodes.** If a Tdarr rule produces a bad output, the operator manually restores from the original. Recommendation: keep Tdarr in "non-destructive mode" (it writes the new file, keeps the original until verified, then deletes original) — this is configurable in the rule stack.
- **No Sonarr/Radarr re-trigger on transcode complete.** Tdarr's "post-process" hooks could fire a `*arr` rescan if needed; left as a v2 enhancement.

---

## Total scope

- **3 new tracked files** (`scripts/configure/50-tdarr-server-install.sh` + `scripts/ops/heartbeat-tdarr-server.sh` + this plan doc)
- **2 binary downloads** (Tdarr Server + Node, both delivered by `Tdarr_Updater`)
- **2 user-systemd services** (`tdarr-server.service`, `tdarr-node.service`)
- **1 heartbeat cron** (server only — node failure surfaces in the server's "Nodes" tab and self-recovers via `Restart=on-failure`)
- **1 nginx path fragment** (`/tdarr/`) under existing `quadstronaut.seedbox.example.com` — no subdomain needed
- **2 ports claimed from `app-ports free`** (42018 + 42019 by default)
- **3 new smoke tests** (`tdarr-server-up`, `tdarr-node-connected`, `tdarr-procs`)
- **0 secrets committed.** API key, ports, htpasswd reuse — all in gitignored `secrets/`.

Estimated install time end-to-end: ~1 hour for the install + service setup. **First-run library normalization: 1-3 weeks of background processing**, operator-supervised, throttled.

---

## Open decisions (operator)

1. **Library scope** — start with Movies + TV Shows? Or include Anime / Anime Movies? Audiobooks + Music typically don't need transcoding.
2. **Bitrate target** — CRF 22 (recommended, transparent) vs. CRF 20 (slightly bigger files, more visually identical).
3. **HEVC for everything vs. only large files** — recommend "only files >8GB" rule to focus disk savings where they matter; small files stay H.264 for Direct Play compat with old Plex clients.
4. **Cron-driven pause window** — operator chose "fair use will be fine post-normalization" but a daytime pause is cheap insurance during the first-run period.

---

## Execution protocol

### Step 0 — Pre-execution check

- [ ] Operator pre-reqs from "Open decisions" answered.
- [ ] Library scope decided (Movies + TV Shows; skip Anime + Music recommended).
- [ ] Bitrate target chosen (CRF 22 default).
- [ ] Working tree clean.

### Step 1 — Credential pre-flight (consolidated, one-time)

| Cred | Used by (this plan) | Likely already exists? |
|---|---|---|
| `htpasswd.password` | Phase 27 nginx (admin-only path) | **Yes** |
| `tdarr.api_key` | Phase 27 server config | No — generated at install (`tapi_<random>`) |
| `tdarr.webui_port`, `tdarr.server_port` | Phase 27 service binding | No — claimed via `app-ports free` |
| `tdarr.version` | Phase 27 pin | Captured (`v2.45.01`) |

No hard blockers — all required creds auto-generate or already exist.

**Browser policy:** Phase 29 (libraries + plugin rules) is browser-only via Tdarr Web UI. No CLI alternative for the rule stack composer. Document the click sequence in `docs/operator-deferred.md`.

### Step 2 — Implementation phases

Continuous execution per `feedback_continuous-execution-preferred.md`: don't ask for per-phase approval. Pause only on genuine blockers.

Phases in this plan:
- **Phase 27** — Tdarr Server install + service + nginx + heartbeat (Tasks 27.1-27.6)
- **Phase 28** — Tdarr Node install + service (Tasks 28.1-28.3)
- **Phase 29** — Operator: library + plugin/rule configuration via Web UI (Tasks 29.1-29.2)
- **Phase 30** — Throttled first-run normalization with vigilance (Tasks 30.1-30.3)
- **Phase 31** — Steady-state ongoing mode (Tasks 31.1-31.2)

Each phase = one commit. Phase 30 is *operator-supervised* — multi-day; commit at start, log progress in `docs/operator-deferred.md`, commit at end.

### Step 3 — Self-check (after Phase 31)

1. Run `scripts/smoke-test.sh` — `tdarr-server-up`, `tdarr-node-connected`, `tdarr-procs` must pass.
2. `git status` — clean.
3. Re-run smoke twice.
4. Confirm `transcodecpuWorkers: 1` (single worker) is unchanged from default — never silently bumps to 2+.

### Step 4 — Log audit

1. `journalctl --user -u tdarr-server.service --since "today" -p err`
2. `journalctl --user -u tdarr-node.service --since "today" -p err`
3. `~/.apps/tdarr/logs/{server.log,server.err,node.log,node.err}` — `grep -E 'ERROR|FATAL|crash|killed'`
4. Tdarr Web UI → Statistics → Errors tab — for transcode failures by file
5. Ultra.cc support inbox (operator) — daily during Phase 30 first-run for fair-use warnings

Classify each error:
- **Cosmetic** (single-file transcode failure on a malformed source) — note, the file stays untouched.
- **Actionable** (FFmpeg path missing for a plugin, plugin update broke a rule) — fix, re-run.
- **Blocking** (Ultra.cc fair-use warning email, sustained CPU >50%) — pause node immediately, surface to operator.

### Step 5 — Final summary template

```
# Tdarr implementation
- Phases completed: 27, 28, 29, 30, 31
- Scripts added: 1 install + 1 heartbeat
- Configs: Tdarr_Server_Config.json, Tdarr_Node_Config.json, nginx fragment
- Smoke: N/N pass (was M/M before)

# First-run normalization stats (Phase 30)
- Files transcoded: N / total
- Disk savings observed: X GB → Y GB (Z%)
- Ultra.cc warnings received: yes/no

# Self-check results
- [details]

# Log audit
- Cosmetic: [list]
- Actionable (fixed): [list]
- Blockers requiring operator: [list, or "none"]

# Follow-up parking lot
- HEVC-for-large-files plugin tuning post-normalization
- Optional cron-pause window decision (operator)
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

- **Never default-unpause the node** — `startPaused: true` is the safety. Operator manually unpauses in Phase 30 under vigilance.
- **Never set `transcodecpuWorkers > 1`** on this shared seedbox without a specific operator decision — fair-use enforcement is the cost.
- **Never run destructive HEVC re-encode** without rule-stack review — Tdarr can permanently overwrite source files. Recommended: keep "non-destructive mode" enabled (writes new file, verifies, deletes original).
- **Never ignore an Ultra.cc fair-use warning** — pause Tdarr immediately, reply to support, scale down, then resume.
- **Never bulk-revert transcoded files by force-deleting** — Tdarr's database tracks state; use Web UI's "Re-process" function instead.
- Don't include Anime/Music libraries in v1 normalization — separate metadata + bitrate strategy needed.
- Don't commit secrets — gitignored.
