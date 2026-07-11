# QFlix Random Error Audit (REA) — Design Spec

- **Date:** 2026-05-11
- **Status:** Approved (brainstorm complete; pending implementation plan)
- **Owner:** Quadstronaut
- **Branch:** `feat/qflix-rea` (off master; no merge until tested green)

## 1. Purpose

A workstation-side, single-file PowerShell audit that runs on every Windows logon. It pulls a fixed set of seedbox log surfaces over one SSH call, hands the consolidated blob to every code-capable Ollama model installed locally, and posts a consensus Discord message (with operator @ping) if any model finds errors.

The point: an extra independent set of eyes on the Manitoba stack that does NOT depend on Kuma's monitor definitions, threshold logic, or push-pull infrastructure. If Kuma silently breaks the way it did with `systemd_only` against `.timer` (2026-05-11), this audit can still surface the underlying failure because it reads the raw journal.

## 2. Non-Goals

- **Not in Kuma.** Zero monitors, zero push tokens, zero notification channels. Pure workstation-side.
- **Not a daemon.** Single-shot script triggered by Task Scheduler at-logon. Exits when done.
- **Not auto-heal.** Read-only audit. No `app-X restart`, no service writes, no SSH commands that mutate seedbox state.
- **Not a replacement for the maint-pusher pipeline.** It's a second opinion, not a primary signal.
- **Not multi-model concurrent.** Models run sequentially — one at a time — to avoid VRAM/RAM contention on the workstation.

## 3. File Layout

```
QFlix/
├─ scripts/
│  └─ local-llm/                          ← new subdir; future local-LLM helpers land here
│     └─ qflix-rea.ps1                    ← THIS script; gitignored (operator-specific FQDN + paths)
├─ docs/
│  └─ superpowers/specs/
│     └─ 2026-05-11-qflix-rea-design.md   ← THIS file (tracked)
└─ .gitignore                             ← add: scripts/local-llm/qflix-rea.ps1

%APPDATA%\qflix-rea\                      ← workstation state dir (created on first run)
├─ state.json                             ← {last_heartbeat_date, last_ollama_dead_ping}
├─ audit.log                              ← one line per run; rotated at 10MB → audit.log.1
├─ last-fetch.log                         ← raw JSON blob from latest SSH fetch (for spot-check)
└─ state.json.lock                        ← prevents concurrent runs from a rapid double-logon
```

The script is gitignored for the same reason `scripts/manitoba-tunnel.ps1` is: it hardcodes the real FQDN `quadstronaut@seedbox.example.com` and operator-specific filesystem paths.

## 4. Workflow (one pass, on logon)

```
┌─────────────────────────────────────────────────────────────────────┐
│  Phase 0 — Pre-flight gates                                          │
│  ─────────────────────────                                           │
│    a. Acquire state.json.lock (skip+log if already held)             │
│    b. Wait up to 120s for Test-Port 42014 = open                     │
│       (canonical tunnel probe; same port manitoba-tunnel.ps1 uses)   │
│    c. Check Ollama health: GET http://localhost:11434/api/tags       │
│       └─ unreachable → DEAD-MAN PATH (§8) → exit                     │
│    d. Discover models: ollama list | filter by allowlist regex       │
│       └─ zero matches → local-log + exit (no Discord)                │
│    e. Read secrets: discord-webhook.url + discord-operator.id        │
│       └─ either missing → local-log + exit (no Discord)              │
├─────────────────────────────────────────────────────────────────────┤
│  Phase 1 — Single SSH fetch                                          │
│  ──────────────────────────                                          │
│    Run one ssh.exe invocation with -o BatchMode=yes against          │
│    quadstronaut@seedbox.example.com. Heredoc'd bash assembles a JSON    │
│    blob from the 7 pre-approved sources (§5) and prints it to        │
│    stdout. Workstation captures stdout to:                           │
│        %APPDATA%\qflix-rea\last-fetch.log                            │
│    Hard timeout: 60s. Any non-zero exit → local-log + exit.          │
├─────────────────────────────────────────────────────────────────────┤
│  Phase 2 — Model loop (sequential)                                   │
│  ──────────────────────────────                                      │
│    foreach $model in $discoveredModels {                             │
│      POST /api/generate with:                                        │
│        model:  $model                                                │
│        system: <fixed system prompt, §6>                             │
│        prompt: <last-fetch.log contents + JSON-schema reminder>      │
│        options: { temperature: 0, num_predict: 2048 }                │
│        stream: false                                                 │
│      Hard per-model timeout: 240s (qwen3-coder:30b can be slow).     │
│      Parse: first valid JSON array in response.                      │
│      Unparseable / timeout → record model as "no opinion", continue. │
│    }                                                                 │
├─────────────────────────────────────────────────────────────────────┤
│  Phase 3 — Consensus + Discord                                       │
│  ────────────────────────────                                        │
│    Group findings by normalized signature (§7).                      │
│    If any group has severity ∈ {error, critical}:                    │
│       → ONE Discord webhook POST with @operator-id ping              │
│    Elif clean AND state.last_heartbeat_date ≠ today:                 │
│       → ONE small "✓ audit clean" Discord post (no ping)             │
│       → update state.last_heartbeat_date                             │
│    Else:                                                             │
│       → silent. Append outcome to audit.log.                         │
└─────────────────────────────────────────────────────────────────────┘
```

## 5. SSH Allowlist (the seven pre-approved sources)

The ps1 sends exactly one heredoc to the seedbox. The remote-side bash assembles the JSON blob by running the commands below — *no other commands are sent*. Each section is hard-capped at ~16 KB after collection so a runaway log can't blow model context.

| # | Source | Remote command (exact) | What it surfaces |
|---|---|---|---|
| 1 | *arr app logs | `for app in sonarr sonarr2 radarr radarr2 prowlarr bazarr bazarr2; do for f in ~/.apps/$app/logs/*.txt; do [ -f "$f" ] && tail -n 200 "$f"; done; done` | indexer auth fails, import errors, profile breakage |
| 2 | systemd --user journal | `journalctl --user -p err --since '24 hours ago' --no-pager` | service/timer/oneshot failures (buildarr, listmonk, tdarr, maint-*) |
| 3 | Cron mail spool | `tail -n 500 /var/spool/mail/quadstronaut 2>/dev/null` | the 571-email class of failure |
| 4 | Maint pipeline | `cat ~/.opt/maint/state.json 2>/dev/null && echo '---' && journalctl --user -u manitoba-maint-pusher --since '6h ago' -p warning --no-pager` | auto-heal misfires |
| 5 | nginx errors | `tail -n 200 ~/.apps/nginx/logs/error.log 2>/dev/null` | 502s, upstream failures |
| 6 | Plex errors | `grep -h '\[ERROR\]' ~/'.apps/plex/Library/Application Support/Plex Media Server/Logs'/*.log 2>/dev/null \| tail -n 100` | Plex-side breakage |
| 7 | Kuma red-state | `sqlite3 ~/.apps/uptimekuma/kuma.db "SELECT m.name FROM monitor m JOIN heartbeat h ON h.monitor_id=m.id WHERE h.time=(SELECT MAX(time) FROM heartbeat WHERE monitor_id=m.id) AND h.status=0;"` | pre-aggregated "what's down NOW" |

The remote bash wraps the output as:

```json
{
  "fetched_at": "<ISO-8601 UTC>",
  "host": "seedbox.example.com",
  "sources": {
    "arr_logs":        "<truncated string>",
    "journal_errors":  "<truncated string>",
    "cron_mail":       "<truncated string>",
    "maint_state":     "<truncated string>",
    "nginx_errors":    "<truncated string>",
    "plex_errors":     "<truncated string>",
    "kuma_red":        "<truncated string>"
  }
}
```

## 6. Model Discovery & Prompting

### Discovery regex

```
include: (?i)(coder|^qwen3(:|$))
exclude: (?i)(-base|-vl|^bge|embed)
```

Today's resolved set: `qwen3-coder:30b`, `qwen2.5-coder:7b`, `qwen3:8b`. Future `ollama pull qwen3-coder:14b` auto-joins. Future `ollama pull mistral:7b` is ignored.

### System prompt (fixed, identical for every model)

> You are auditing a self-hosted media-server stack ("Manitoba" / "QFlix") running on an Ultra.cc shared seedbox. You receive a JSON blob containing log excerpts from seven sources. Your job: find real errors a sysadmin should act on. Ignore noise (info-level chatter, expected periodic warnings, unrelated debug lines).
>
> Return ONLY a JSON array. No prose, no markdown fences. Empty array `[]` means clean.
>
> Each finding object MUST have these exact keys:
> - `time`: ISO-8601 string (best-effort from log line; fall back to fetched_at)
> - `app`: short slug (`sonarr`, `buildarr`, `nginx`, `plex`, `cron`, ...)
> - `file`: source identifier (e.g. `journal:buildarr.service`, `/var/spool/mail/quadstronaut`, `nginx/error.log`)
> - `severity`: one of `warning` | `error` | `critical`
> - `summary`: one-line human description
> - `excerpt`: ≤300 chars of the offending log line(s)
> - `signature`: short stable string for dedupe (e.g. `buildarr:pydantic-validation-error`)

### Per-model invocation

`POST http://localhost:11434/api/generate` with `stream: false`, `options.temperature: 0`, `options.num_predict: 2048`. 240s per-model hard timeout. Each model gets the SAME prompt and SAME blob, sequentially. PowerShell collects all responses into an array `$verdicts[]`.

### Output parsing

Robust extractor: scan response text for the first `[` through matched closing `]`, attempt `ConvertFrom-Json`. If it fails, that model is recorded as "no opinion" and contributes nothing to consensus. No retries.

## 7. Consensus

Group all findings across all models by `signature` (lowercased, trimmed). For each signature group:
- `time` = earliest among contributors
- `severity` = max(warning < error < critical) among contributors
- `app`, `file` = first non-empty
- `summary` = pick the longest among contributors (rough heuristic for most informative)
- `excerpt` = pick the longest
- `models_flagged` = `[model-name, ...]` (dedup'd)

The Discord payload lists groups in severity-desc, then time-asc order.

## 8. Discord Reporting

### Error message (any group with severity ≥ error)

```
content: "<@OPERATOR_ID>"
allowed_mentions: { parse: [], users: [OPERATOR_ID] }
embeds: [{
  title: "🚨 QFlix REA — N issues",
  color: 15158332,        // red
  timestamp: <ISO>,
  fields: [ one field per group, value formatted as:
    "**{app}** · `{file}` · {severity}\n{summary}\n_flagged by: {models_flagged} ({n}/{total})_\n```\n{excerpt}\n```"
  ],
  footer: { text: "models: {total}  ·  sources: 7  ·  duration: {sec}s" }
}]
```

### Clean heartbeat (clean run + first of calendar day)

```
content: ""    // no ping
embeds: [{
  title: "✓ QFlix REA clean",
  color: 3066993,        // green
  description: "{model_count} models · 7 sources · 0 findings",
  timestamp: <ISO>
}]
```

After successful POST, update `state.last_heartbeat_date` to today (workstation local date).

### Dead-man (Ollama down)

Triggered in Phase 0c. Posted regardless of dedupe IF `state.last_ollama_dead_ping` is empty OR > 24h old.

```
content: "<@OPERATOR_ID>"
embeds: [{
  title: "⚠️ QFlix REA — Ollama appears down",
  color: 16753920,       // orange
  description: "Workstation Ollama at http://localhost:11434/api/tags is not responding. Audit skipped.\n\nNext check: next Windows logon. Manual fix: `ollama serve` (or restart the Ollama service).",
  timestamp: <ISO>
}]
```

Updates `state.last_ollama_dead_ping` after successful POST. Failed POST → local-log only, no retry, no 24h timestamp update (so next logon will try again).

## 9. State & Locking

`%APPDATA%\qflix-rea\state.json`:

```json
{
  "last_heartbeat_date": "2026-05-11",
  "last_ollama_dead_ping": "2026-05-10T22:14:03-07:00"
}
```

`state.json.lock` is an empty file created with exclusive open. If locked → write one line to `audit.log` (`SKIPPED locked`) and exit cleanly. Removed on script exit (try/finally).

`audit.log` format (one line per run):

```
2026-05-11T14:22:01-07:00 ok findings=2 models=3 duration=47s outcome=error_post
2026-05-11T15:01:14-07:00 ok findings=0 models=3 duration=44s outcome=heartbeat
2026-05-11T18:30:02-07:00 ok findings=0 models=3 duration=39s outcome=silent
2026-05-11T20:10:55-07:00 fail reason=tunnel_timeout
2026-05-11T22:00:12-07:00 fail reason=ollama_down outcome=deadman_post
```

Rotated when >10MB: `audit.log` → `audit.log.1` (single backup, old `.1` overwritten).

## 10. Task Scheduler Integration

Script supports a `-Install` flag that creates the task and a `-Uninstall` flag that removes it.

```powershell
.\scripts\local-llm\qflix-rea.ps1 -Install
```

Task properties:
- **Path:** `\Archangel\QFlix-LLM\`
- **Name:** `QFlix Random Error Audit`
- **Trigger:** AtLogOn for current user
- **Action:** `powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "G:\Documents\GIT\Ultra.cc\QFlix\scripts\local-llm\qflix-rea.ps1"`
- **Settings:**
  - `StartWhenAvailable = $true` (catches missed logons)
  - `MultipleInstancesPolicy = IgnoreNew` (file lock is a belt; this is the suspenders)
  - `ExecutionTimeLimit = PT15M` (hard ceiling — qwen3-coder:30b is the long tent pole; 3 models × ~240s + overhead fits comfortably)
  - `AllowStartIfOnBatteries = $true` (operator runs solar)
  - `RunOnlyIfNetworkAvailable = $true`
- **User context:** current user, "Run only when user is logged on" (no stored password required).

The `\Archangel\QFlix-LLM\` folder is created if it doesn't exist (via the COM ScheduledTasks API). Companion to the existing `\Archangel\Manitoba SSH Tunnel` task. Future local-LLM tasks land in the same folder.

## 11. Robustness Matrix

| Failure mode | Discord behavior | Local behavior | Reasoning |
|---|---|---|---|
| Lock file held (concurrent run) | silent | `SKIPPED locked` in audit.log | Avoid duplicate Discord on rapid double-logon |
| Tunnel-port 42014 never opens within 120s | silent | `fail reason=tunnel_timeout` | Could just be a quick reboot; not actionable |
| Ollama API unreachable | **dead-man ping** (24h dedupe) | `fail reason=ollama_down` | Operator explicitly wants this — the one exception to "silent on own breakage" |
| Zero models match allowlist | silent | `fail reason=no_models` | Operator changed Ollama install; not seedbox concern |
| Either Discord secret missing | silent | `fail reason=no_secrets` | Can't post anyway |
| SSH fails (host unreachable / auth / BatchMode prompt) | silent | `fail reason=ssh_fail` | Workstation network issue; not seedbox concern |
| SSH succeeds but JSON unparseable | silent | `fail reason=blob_parse` + raw saved to last-fetch.log | Bug in remote heredoc — operator inspects manually |
| All models return unparseable output | silent | `fail reason=all_models_noop` | Models drifted; not a seedbox alert |
| Some models OK, some unparseable | normal consensus on the OK ones | `partial` flag in audit.log | Best-effort; consensus over reachable signal |
| Discord POST fails | n/a | `fail reason=discord_post` | Don't update heartbeat date so next run retries |

## 12. Implementation Notes (non-binding hints for the plan)

- Single file, no external modules. Stick to PowerShell 5.1 syntax (matches `manitoba-tunnel.ps1`).
- Use `Invoke-RestMethod` for Ollama + Discord. Use `Start-Process ssh.exe` with redirected stdout for the SSH call (avoids shell-quoting issues with the heredoc).
- The heredoc payload is built in PowerShell as a single-quoted here-string `@' ... '@` and piped to `ssh.exe ... bash -s`. No interpolation = no escaping headaches.
- JSON output from remote bash: prefer `jq` if available; otherwise emit JSON via printf-escape (heredoc must be defensive about literal `"` inside log lines — base64-encode each section's payload before assembly is the simplest correct path).
- Param block at top: `[switch]$Install`, `[switch]$Uninstall`, `[switch]$DryRun` (no Discord posts; print payload to console), `[switch]$Once` (skip the tunnel-wait gate; useful for manual debugging).
- Logging helper: write `audit.log` lines with `Add-Content -Encoding UTF8`. Lock file: `[System.IO.File]::Open($lockPath,'OpenOrCreate','ReadWrite','None')`.

## 13. Testing Strategy (high-level; details in plan)

1. **Unit-shaped:** `-DryRun` against a synthetic `last-fetch.log` fixture with both clean and dirty payloads. Verify consensus grouping, severity max, Discord payload shape (assert against a captured JSON template).
2. **Integration:** `-DryRun` against a live SSH fetch. Inspect `last-fetch.log` manually; verify all 7 sources populate.
3. **End-to-end clean:** Real run when seedbox is known-clean. Verify heartbeat posts once, second logon same day stays silent.
4. **End-to-end dirty:** Plant a synthetic error (e.g. write a fake error line to a log under `~/.tmp/`, point one source at it, restore after). Verify error post lands with correct ping + group structure.
5. **Dead-man:** Stop Ollama service. Run script. Verify dead-man Discord post + 24h dedupe.
6. **Failure modes:** Disconnect network, run; close OpenSSH, run; remove webhook secret, run. Each should append the expected `fail reason=...` line and stay silent on Discord (except dead-man).
7. **Task Scheduler:** `-Install`, log off and back on, verify task fires within 60s of logon.

Merge to master gated on items 1–7 passing.

## 14. Open Questions

None blocking. The following can be revisited post-merge if behavior is unsatisfying:

- **Per-error 24h cooldown** (in addition to consensus collapse) if the same error nags daily and you want it muted. Easy state.json extension.
- **Severity threshold for ping** — currently `error` and above. If `warning` proves quiet enough, could be lowered.
- **Model voting weight** — currently flat (every model equal). Could weight by size or by historical accuracy if false-positive rate skews by model.
