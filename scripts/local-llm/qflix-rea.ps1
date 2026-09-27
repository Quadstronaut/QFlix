#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$DryRun,
    [switch]$Once,
    [string]$FixturePath
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# ---------- Configuration ----------
$Script:SshExe          = "$env:WINDIR\System32\OpenSSH\ssh.exe"
# SSH host resolves from <repo>/secrets/seedbox.ssh-host (the SHARED Ultra.cc
# hostname, not the operator-slot subdomain). Set via Resolve-SshHost on first
# use so that Get-RepoRoot is callable. Override with $env:QFLIX_SSH_HOST.
$Script:SshHost         = $null
$Script:OllamaBase      = 'http://localhost:11434'
# Resolve the Ollama binary at load time. This box runs scoop-managed Ollama
# (shim on PATH); the official-installer path is kept only as a last-resort
# fallback. A wrong path here silently disables Start-Ollama's self-start +
# Wait-ForOllama race mitigation (it short-circuits on Test-Path), so resolve
# defensively rather than hardcoding one install layout.
$Script:OllamaExe = $(
    $c = Get-Command ollama -ErrorAction SilentlyContinue
    if ($c -and $c.Source)                                        { $c.Source }
    elseif (Test-Path "$env:USERPROFILE\scoop\shims\ollama.exe")  { "$env:USERPROFILE\scoop\shims\ollama.exe" }
    else                                                          { "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
)
$Script:OllamaHealthRetries    = 12
$Script:OllamaHealthBackoffSec = 10
$Script:TunnelProbePort = 42014
$Script:TunnelWaitSec   = 120
$Script:SshTimeoutSec   = 60
# 240 -> 420 (2026-08-18): with num_ctx=24576 and a storm-fat blob, the two
# larger models need longer prefill; at 240s they timed out and runs graded
# models=1/3 (563s run, 2 timeouts + 1 pass, measured live). Worst case is
# 3 x 420s + fetch = ~22 min, comfortably inside the hourly cadence.
$Script:ModelTimeoutSec = 420
# Yield-to-operator gate (2026-09-13). The model phase pins every core (Ollama
# launches llama-server at AboveNormal) and 5-7 GB of the 8 GB GPU for up to
# ~20 min, which stuttered YouTube in Edge at the top of every hour. Scheduled
# runs skip while the operator touched keyboard/mouse within BusyInputMinutes,
# or while any non-plumbing app is AUDIBLY playing (catches a long video
# watched hands-off). -Once / -DryRun / -FixturePath bypass the gate.
$Script:BusyInputMinutes = 10
# Processes whose audio is plumbing, not media: Sonar mixes to the real device
# continuously, audiodg is the engine, pid 0 ("Idle") is the system-sounds
# session. Counting any of these would hold REA off forever.
$Script:BusyAudioIgnoreRx = '^(Idle|audiodg|SteelSeries.*)$'
$Script:ModelIncludeRx  = '(?i)(coder|^qwen3(:|$))'
$Script:ModelExcludeRx  = '(?i)(-base|-vl|^bge|embed)'
$Script:SectionByteCap  = 3000   # per-section cap. Lowered from 16384 when the
                                 # source count went 7 -> 14 (2026-07-25): the
                                 # DECODED blob must fit the models' 16K ctx AND
                                 # leave room to generate findings. 14x3000 decoded
                                 # ~= 9-10K tokens -> ~6K tokens of output headroom.
$Script:FreshDays      = 3      # SINGLE source of truth for "current vs stale".
                                 # Templated into the remote bash heredoc (FRESH_DAYS)
                                 # AND used by Test-IsStaleFinding below - the models
                                 # are ALSO told this via the system prompt, but that
                                 # is advisory only (see the enforcement comment on
                                 # Test-IsStaleFinding: models demonstrably ignore it).
$Script:PageCooldownHours = 24
                                 # Cross-RUN page suppression. Get-Consensus dedups
                                 # WITHIN one run and nothing dedupped ACROSS runs, so
                                 # a single log line paged once per hour for as long as
                                 # its file stayed inside the FreshDays window: up to
                                 # 72 identical Discord embeds from ONE fault. Measured
                                 # 2026-09-02: 45 pages in 24h from ~8 distinct causes,
                                 # e.g. one listmonk "connection refused" line that was
                                 # a single transient postgres blip at 12:25 and the
                                 # LAST line of a 52-line log, so it sat in the tail and
                                 # re-paged every hour afterwards.
                                 # 24h, not FreshDays: a fault still broken tomorrow
                                 # must be re-surfaced. Silence-forever is the opposite
                                 # failure to the one this fixes.
$Script:AuditLogMaxBytes = 10MB
# The LIVE task has sat at \QFlix-LLM\ (root-level) since it was created; this
# constant said \Archangel\QFlix-LLM\, so -Install would have registered a SECOND
# copy and left the original running — two hourly REA runs, neither aware of the
# other. Corrected to the live path 2026-09-27; Uninstall-Task sweeps the stale
# one so the drift cannot survive a reinstall.
$Script:TaskFolderPath      = '\QFlix-LLM\'
$Script:LegacyTaskFolderPath = '\Archangel\QFlix-LLM\'
$Script:TaskName            = 'QFlix Random Error Audit'

# ---------- Noise policy: LOADED FROM GIT, NOT AUTHORED HERE ----------
# The system prompt ASKS the models to skip known-benign classes, but a prompt is
# advisory and consensus has no floor: on 2026-07-28 qwen3-coder:30b reported the
# tdarr "reading 'includes'" TypeError that the prompt EXPLICITLY forbids, and one
# model out of three was enough to page the operator at 2am. The rule table is the
# ENFORCEMENT layer that a prompt alone can never be.
#
# THE TABLE NO LONGER LIVES IN THIS FILE. It did until 2026-08-19, as a literal
# hand-kept twin of manifest/rea-noise-classes.yaml, and the twin fell behind
# three times in three weeks: 2026-07-29 (three classes named in the prompt with
# no enforcing rule), 2026-08-06 (same shape again), and 2026-08-19 (yaml at 27
# classes, this file at 25 - audit detector C-07 red, report_digest suddenly
# host-dependent because this file exists only on the workstation, and BOTH new
# classes INERT, so REA kept paging the operator on lines the policy said to
# ignore). A copy a human has to remember to update is not a policy, it is a bug
# with a schedule.
#
# scripts/local-llm/rea-noise-classes.ps1 (TRACKED, and therefore CI-testable)
# now reads the yaml directly. A class added to manifest/rea-noise-classes.yaml
# alone is enforcing on the very next run of this script with no edit here at
# all. Every per-rule rationale that used to sit in comments in this file lives
# in that yaml's `why:` field, in git, where the offline suite can read it.
#
# Suppressions are still never silent: each one is written to the audit log
# (`suppressed n=... rules=...`).
. (Join-Path $PSScriptRoot 'rea-noise-classes.ps1')
$Script:NoiseFindingRules = Get-ReaNoiseRules

# The five early-return failure paths in Invoke-Main (fixed 2026-07-29: REA could
# go permanently dark - only the ollama_down branch used to page). Each reason
# gets its OWN 24h dedup key (state.dead_ping_<reason>) so a stuck tunnel does
# not suppress a later, unrelated model outage from ever paging. Mirrored into
# the same yaml so audit detector C-09 can enumerate them without this file.
$Script:DeadmanReasons = Get-ReaDeadmanReasons

# BEGIN GENERATED NOISE-TABLE MIRROR (regenerated by rea-noise-classes.ps1::Sync-ReaNoiseMirror - DO NOT EDIT)
<#
$Script:NoiseFindingRules = @(
    @{ id = 'plex-client-abort-stream-write'
       rx = '(?i)(caught exception trying to stream file|protocol is shutdown \(ssl|ssl[-: ]?protocol[-: ]?shutdown)' }
    @{ id = 'plex-nat-pmp-upnp'
       rx = '(?i)(nat-?pmp|upnp)\b.*(not supported|unsupported|no gateway|failed)' }
    @{ id = 'tdarr-express-undefined-includes'
       rx = '(?i)cannot read properties of undefined \(reading ''includes''\)' }
    @{ id = 'tdarr-worker-not-a-function'
       rx = '(?i)worker\d*\b.*is not a function' }
    @{ id = 'tdarr-wasm-oom'
       rx = '(?i)(webassembly\.instantiate\(\):\s*out of memory|wasm memory|wasm-memory-exhausted)' }
    @{ id = 'mediainfo-failure'
       rx = '(?i)error running mediainfo' }
    @{ id = 'tdarr-mediainfo-result-fragment'
       field = 'excerpt'
       rx = '(?im)tdarr_server - (stderr:\s*\{\s*$|\{ result: ''error'', error: \{\} \})' }
    @{ id = 'plex-post-reap-scan'
       rx = '(?i)failed to create parent iterator' }
    @{ id = 'external-indexer-5xx-html'
       rx = '(?im)(cloudflare.{0,80}(ray id|error 5\d\d)|ray id:\s*[0-9a-f]{8,}|<center>nginx(?:/[\d.]+)?</center>|^\s*error code:\s*52\d\b)' }
    @{ id = 'indexer-severity-field-echo'
       rx = '"severity"\s*:\s*"error"' }
    @{ id = 'bare-stack-continuation'
       field = 'excerpt'
       rx = '(?ims)\A(?!.*(?:^\s*[A-Za-z][\w.]*(?:Exception|Error)\b\s*:|Traceback \(most recent call last\)|\[ERROR\]|ERROR\s*[-:])).*(?:^\s*at\s+\S|end of inner exception stack trace)' }
    @{ id = 'plex-client-profile-extra'
       rx = '(?i)clientprofileextra:\s*missing or invalid type parameter' }
    @{ id = 'plex-metadata-agent-pseudo-identifier'
       rx = '(?i)unable to find metadata agent provider for identifier\s+\W?(?:library|iva)\b' }
    @{ id = 'arr-parsing-no-matching-title'
       rx = '(?i)\|Debug\|ParsingService\|No matching (?:series|movie)\b' }
    @{ id = 'arr-release-rejected-unknown-title'
       rx = '(?i)\|Debug\|DownloadDecisionMaker\|.*\[Permanent\] Unknown (?:series|movie)\b' }
    @{ id = 'arr-debug-only-excerpt'
       field = 'excerpt'
       rx = '(?ims)\A(?!.*(?:\|\s*(?:Error|Fatal|Warn(?:ing)?|Critical)\s*\||ERROR\s*[-:]|\[ERROR\]|Traceback \(most recent call last\))).*\|Debug\|' }
    @{ id = 'plex-credits-detection-chatter'
       rx = '(?i)\[creditsdetectionmanager(?:\]|/).{0,80}?(?:incomplete marker attributes|bufferinglinereader: failed to read line|credits detection for item \d+ has failed too many times)' }
    @{ id = 'plex-credits-job-video-missing'
       rx = '(?i)\[creditsdetectionmanager\]\s*job failed:\s*video does not exist' }
    @{ id = 'plex-unknown-metadata-type-folder'
       rx = '(?i)unknown metadata type:\s*folder\b' }
    @{ id = 'bazarr-github-release-check-ratelimit'
       field = 'excerpt'
       rx = '(?im)^(?=.*(?:trying to get releases from github|morpheus65535/bazarr/releases))(?=.*(?:rate.?limit|http error))' }
    @{ id = 'tdarr-handbrake-binary-test'
       field = 'excerpt'
       rx = '(?is)\A(?!.*binary test \d+:\s*(?!handbrakepath\b)\S+ not working\b).*binary test \d+:\s*handbrakepath not working\b' }
    @{ id = 'seerr-plex-scan-tvdbid-collision'
       field = 'excerpt'
       rx = '(?is)\A(?!.*unique constraint failed:\s*(?!media\.tvdbid\b)\S+).*unique constraint failed:\s*media\.tvdbid\b' }
    @{ id = 'buildarr-unsupported-plex-notification'
       rx = '(?i)unsupported remote notification connection\b.{0,60}implementation .?plexserver.?, ignoring' }
    @{ id = 'plex-network-service-shutdown'
       field = 'excerpt'
       rx = '(?im)^(?=.*network service)(?=.*advertis)(?=.*(?:operation canceled|abandoning))' }
    @{ id = 'bazarr-signalr-reconnect'
       rx = '(?i)bazarr signalr client for \w+ connection as been lost' }
    @{ id = 'arr-indexer-unavailable-backoff'
       rx = '(?i)(indexer is disabled till .{0,60}due to recent failures|indexer.s server is unavailable\. try again later)' }
    @{ id = 'prowlarr-cardigann-retry-5xx'
       field = 'excerpt'
       rx = '(?is)\A(?!.*(?:\|\s*(?:Error|Fatal|Critical)\s*\||\[ERROR\]|ERROR\s*[-:]|Traceback \(most recent call last\))).*\|Warn\|[^|\n]{0,40}\|Request for [^\n]{0,120}? failed with status (?:5\d\d|InternalServerError|BadGateway|ServiceUnavailable|GatewayTimeout)\. Retrying in' }
    @{ id = 'plex-orphaned-webhook-delivery'
       field = 'excerpt'
       rx = '(?is)\A(?!.*(?:\|\s*(?:Error|Fatal|Critical)\s*\||\[ERROR\]|ERROR\s*[-:]|Traceback \(most recent call last\)))(?!.*webhook: error delivering payload to (?!\S*/1177487639654441000/)\S).*webhook: error delivering payload to \S*/1177487639654441000/' }
    @{ id = 'arr-cloud-news-fetch-timeout'
       rx = '(?i)serversidenotificationservice\|?\s*failed to retrieve notifications' }
    @{ id = 'arr-discord-notify-post-failure'
       rx = '(?i)(discordproxy\|\s*unable to post payload|unable to post payload\s+nzbdrone\.core\.notifications\.discord|unable to send on[a-z]+ notification to:\s*discord)' }
    @{ id = 'bazarr-subsync-single-file'
       rx = '(?i)unable to sync subtitles' }
    @{ id = 'prowlarr-flaresolverr-validation-transient'
       rx = '(?i)flaresolverr\|?\s*proxy validation failed' }
    @{ id = 'arr-update-check-failure'
       rx = '(?i)error occurred while executing task (applicationcheckupdate|applicationupdatecheck|checkhealth)\b' }
    @{ id = 'seerr-plextv-watchlist-5xx'
       rx = '(?is)failed to retrieve watchlist items.{0,160}status code 5\d\d' }
    @{ id = 'reaper-success-line-misread'
       field = 'excerpt'
       rx = '(?is)\A(?!.*(?:\|\s*(?:Error|Fatal|Critical)\s*\||\[ERROR\]|ERROR\s*[-:]|Traceback \(most recent call last\))).*success\s*[-—]\s*\d+ deleted, [\d.]+ gb reclaimed' }
    @{ id = 'plex-post-reap-missing-input'
       field = 'excerpt'
       rx = '(?i)error opening input(?: files?)?[: ].*(?:404 not found|/library/parts/)' }
    @{ id = 'arr-rss-sync-missed-period'
       rx = '(?i)rss sync (?:didn.t|did not) cover the period between' }
    @{ id = 'tautulli-plex-websocket-refused'
       field = 'excerpt'
       rx = '(?i)tautulli websocket ::.*(?:errno 111|connection refused|connection is already closed|connection to remote host was lost)' }
    @{ id = 'plex-tuner-discover-ssl'
       field = 'excerpt'
       rx = '(?i)discover\.json.*(?:ssl|certificate).*(?:not ok|subject name|peer certificate)' }
    @{ id = 'maint-notify-echo'
       field = 'excerpt'
       rx = '(?i)lib\.notify:\s*alert sent:' }
    @{ id = 'plex-vanished-file-decision-failure'
       field = 'excerpt'
       rx = '(?i)(?:failed to get a decision for:|mde:\s*video has neither a video stream nor an audio stream|mde:\s*no compatible media decisions are available)' }
    @{ id = 'plex-download-container-html-for-vanished-item'
       field = 'excerpt'
       rx = '(?i)downloadcontainer:\s*expected mediacontainer element,\s*found html' }
    @{ id = 'plex-credits-job-no-thumbnails'
       field = 'excerpt'
       rx = '(?i)\[creditsdetectionmanager\]\s*job failed:\s*failed to generate any thumbnails' }
    @{ id = 'plex-eae-watchfolder-missing'
       field = 'excerpt'
       rx = '(?i)error iterating eae watchfolder directory:\s*no such file or directory' }
    @{ id = 'plex-credits-job-scanner-failed'
       field = 'excerpt'
       rx = '(?i)\[creditsdetectionmanager\]\s*job failed:\s*scanner job failed' }
    @{ id = 'sab-queue-finished-notification'
       field = 'excerpt'
       rx = '(?i)::info::\[notifier:\d+\]\s*sending notification:\s*sabnzbd - queue finished' }
    @{ id = 'plex-season-skip-null-value'
       field = 'excerpt'
       rx = '(?i)exception caught determining whether we could skip\s+''.+/season \d+''\s*~\s*null value not allowed for this type' }
)
#>
# END GENERATED NOISE-TABLE MIRROR
# The block between those two markers is TEXT, not code: it sits inside a
# PowerShell block comment and is never executed. It exists only so the audit's
# C-07 cross-check - which reads this file as BYTES on the workstation and cannot
# run it - can compare the enforcement table against the tracked policy without
# this file being in git. Invoke-Main re-renders it from the yaml on every run
# (Sync-ReaNoiseMirror), so it can be stale for at most one run, and never for a
# reason a human has to notice.

# ---------- State + paths ----------
function Get-RepoRoot {
    # Script lives at <repo>/scripts/local-llm/qflix-rea.ps1 — 3 dirs up.
    Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSCommandPath))
}

function Resolve-SshHost {
    # Resolve once, cache in $Script:SshHost. Precedence:
    #   1. $env:QFLIX_SSH_HOST   — operator override
    #   2. secrets/seedbox.ssh-host (shared Ultra.cc host, not the slot)
    #   3. secrets/seedbox.host  — legacy fallback (per scripts/lib/ssh.sh)
    #   4. throw — refuse to guess
    if ($Script:SshHost) { return $Script:SshHost }
    if ($env:QFLIX_SSH_HOST) {
        $Script:SshHost = $env:QFLIX_SSH_HOST
        return $Script:SshHost
    }
    $secretsDir = Join-Path (Get-RepoRoot) 'secrets'
    $candidates = @('seedbox.ssh-host', 'seedbox.host')
    foreach ($name in $candidates) {
        $p = Join-Path $secretsDir $name
        if (Test-Path -LiteralPath $p) {
            $fqdn = (Get-Content -Raw -LiteralPath $p).Trim()
            if ($fqdn) {
                $Script:SshHost = "quadstronaut@$fqdn"
                return $Script:SshHost
            }
        }
    }
    throw "Cannot resolve SSH host: no QFLIX_SSH_HOST env, no $secretsDir\seedbox.ssh-host, no $secretsDir\seedbox.host"
}

function Get-StateDir {
    $d = Join-Path $env:APPDATA 'qflix-rea'
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    return $d
}

function Read-State {
    # dead_ping_<reason> (one per $Script:DeadmanReasons entry) rides alongside
    # the original last_heartbeat_date / last_ollama_dead_ping fields - added
    # 2026-07-29 so each of the five non-ollama failure paths gets its own
    # independent 24h page-dedup key. Unknown/missing keys default to '', same
    # as the two original fields always have.
    $defaults = @{ last_heartbeat_date = ''; last_ollama_dead_ping = '' }
    foreach ($r in $Script:DeadmanReasons) { $defaults["dead_ping_$r"] = '' }
    $p = Join-Path (Get-StateDir) 'state.json'
    if (-not (Test-Path $p)) { return $defaults }
    try {
        $raw = Get-Content -Raw -LiteralPath $p
        $obj = $raw | ConvertFrom-Json
        $result = @{}
        foreach ($k in $defaults.Keys) {
            if ($obj.PSObject.Properties.Name -contains $k) { $result[$k] = [string]$obj.$k }
            else { $result[$k] = $defaults[$k] }
        }
        return $result
    } catch {
        return $defaults
    }
}

function Write-State {
    param([hashtable]$State)
    $p   = Join-Path (Get-StateDir) 'state.json'
    $tmp = "$p.tmp"
    ($State | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -Force -LiteralPath $tmp -Destination $p
}

# ---------- Model discovery ----------
function Filter-OllamaListOutput {
    param([string]$RawText)
    $lines = $RawText -split "`r?`n"
    foreach ($line in $lines) {
        if (-not $line) { continue }
        if ($line -match '^\s*NAME\s') { continue }
        $first = ($line -split '\s+',2)[0]
        if (-not $first) { continue }
        if ($first -match $Script:ModelExcludeRx) { continue }
        if ($first -match $Script:ModelIncludeRx) { $first }   # emit to pipeline
    }
}

function Get-CodeModels {
    try {
        $out = & ollama list 2>$null | Out-String
        Filter-OllamaListOutput -RawText $out
    } catch { }
}

# ---------- Remote heredoc + SSH fetch ----------
function Get-RemoteHeredoc {
    # Build a single bash script that emits one JSON object on stdout.
    # Each section is captured to a tmpfile, truncated to SECTION_CAP bytes,
    # then base64-encoded. The PowerShell side decodes per-section.
    #
    # Single-quoted here-string keeps bash `$VAR` literal (no PS expansion);
    # the template holes are __SECTION_CAP__, __FRESH_DAYS__ and
    # __REA_HEARTBEAT_B64__ (the rea-liveness writer half), all filled below.
    $bash = @'
#!/usr/bin/env bash
set -u
SECTION_CAP=__SECTION_CAP__
TMP=$(mktemp -d -t qflix-rea.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

# collect_cap <name> <cap> <cmd...> - run a collector, cap it, base64 it.
#
# LINE-SAFE TRUNCATION (2026-08-25). The cap is a BYTE cut, and a byte cut lands
# wherever it lands - including in the middle of a "===== path =====" header.
# That is not cosmetic: on 2026-08-25 the tdarr section ended on the fragment
# `===== /home/.../Tdarr_Server_Log.txt (ERROR` - a header with ZERO lines under
# it, sitting immediately below an EACCES block that came from a DIFFERENT file -
# and a model copied that trailing header into the `file` field of the finding it
# paged the operator with. A cut that lands on a line boundary cannot orphan a
# header from its body, and cannot hand a model a headless path token.
#
# Read cap+1 bytes: if we got more than the cap, the stream WAS truncated and the
# last line of the capped text is (or may be) a fragment, so drop it. If we got
# cap or fewer, nothing was cut and every line is whole - drop nothing. Detecting
# truncation by byte count rather than always dropping the last line keeps short
# sections byte-identical to what they have always shipped.
collect_cap() {
  local name="$1"; local cap="$2"; shift 2
  local raw="$TMP/.raw.$name"
  ( "$@" 2>&1 || true ) | head -c "$((cap + 1))" > "$raw"
  if [ "$(wc -c < "$raw" 2>/dev/null || echo 0)" -gt "$cap" ]; then
    head -c "$cap" "$raw" | sed -e '$d' | base64 -w0 > "$TMP/$name"
  else
    base64 -w0 < "$raw" > "$TMP/$name"
  fi
  rm -f "$raw"
}

# collect() is collect_cap() at the global SECTION_CAP. Three sections declare
# their own cap because their content volume differs by 3 orders of magnitude
# (arr_logs raw = 4.45 MB, kuma_red = ~40 bytes).
collect() {
  local name="$1"; shift
  collect_cap "$name" "$SECTION_CAP" "$@"
}

# tailfresh <lines> <file...> - emit header+tail ONLY for files modified within
# FRESH_DAYS. A FROZEN log (an app decommissioned/reconfigured weeks ago, e.g.
# nginx 42006 Homarr-era 2026-06-27, or unpackerr Readarr 2026-05-25) must NOT
# keep re-alerting forever on its last stale error lines - REA is a CURRENT-state
# audit. Exported so the `bash -c` collect subshells inherit it.
FRESH_DAYS=__FRESH_DAYS__
tailfresh() {
  local n="$1"; shift
  local f
  for f in "$@"; do
    [ -f "$f" ] || continue
    if [ -n "$(find "$f" -mtime -"$FRESH_DAYS" -print -quit 2>/dev/null)" ]; then
      echo "===== $f ====="
      tail -n "$n" "$f"
    fi
  done
}
export FRESH_DAYS
export -f tailfresh

# tailnew <cap> <file...> - the WATERMARK freshness basis, for append-only
# streams that carry NO per-line date at all.
#
# WHY mtime IS NOT ENOUGH, measured. tailfresh gates the FILE; an undated
# append-only .err file defeats it completely because one fresh write makes the
# whole tail window look current. node.err: 694 lines, 0 of them dated, mtime
# 1.4 days old, and its tail-80 window held workDir ids spanning
# ts-1779348004454 (2026-05-21) to ts-1787514583226 (2026-08-23) - 94 days
# inside one window. On 2026-08-25 a model reported the 2026-05-21 EACCES line
# as a live fault. There is no line date for FRESH_CUTOFF to compare, so no
# line filter can ever help here: freshlines on a 0/694-dated file is a
# provable no-op. The same is true of server.err and kometa.err (0/138689).
#
# An undated append-only stream carries exactly one usable freshness signal:
# how many bytes are NEW. Persist the size per file and ship only the bytes
# appended since the previous run. Content age is then bounded by the RUN
# INTERVAL (~1h), not by however far back the tail happens to reach.
#
# THREE WAYS THIS SHIPS NOTHING, all deliberate:
#   1. FIRST SIGHT of a file: record the watermark, emit nothing. Bytes that
#      were already there when we started watching cannot be proven recent, and
#      the law is that unprovable bytes contribute NOTHING (a bootstrap run
#      that shipped `tail -c cap` would re-import exactly the stale history
#      this function exists to stop).
#   2. NOTHING APPENDED since last run: emit nothing. This is the steady state.
#   3. MTIME FLOOR: a file nothing has written inside FRESH_DAYS emits nothing
#      whatever the offsets file says - the tailfresh law, kept as the floor.
# Shrinking size = rotation/truncation; the whole file is then genuinely new.
#
# Per-line [path] prefix, never a header: byte truncation can orphan a line
# from a header, it can never orphan a line from its own prefix (the arr_logs
# and plex_errors law, applied here for the same reason).
REA_STATE_DIR=$HOME/.opt/maint/rea
REA_OFFSETS=$REA_STATE_DIR/offsets
tailnew() {
  local cap="$1"; shift
  local f size prev ptime now dsz new
  mkdir -p "$REA_STATE_DIR" 2>/dev/null || true
  for f in "$@"; do
    [ -f "$f" ] || continue
    size=$(wc -c < "$f" 2>/dev/null | tr -d " ")
    case "$size" in *[!0-9]*) size=0 ;; esac
    [ -n "$size" ] || size=0
    prev=$(awk -v p="$f" '$1 == p { v = $2 } END { print v }' "$REA_OFFSETS" 2>/dev/null)
    ptime=$(awk -v p="$f" '$1 == p { t = $3 } END { print t }' "$REA_OFFSETS" 2>/dev/null)
    # Advance the watermark FIRST and unconditionally: a run that decides to
    # ship nothing must still move the mark, or one stale burst re-ships for
    # ever. Losing one window of undated stderr to a failed run is the correct
    # trade - every fault class in these files has a dedicated canary.
    # THE MARK MUST ACTUALLY LAND. If the offsets file cannot be written -- the
    # directory replaced by an unwritable path, a full disk, a stray root-owned
    # file -- then `prev` is unreadable every run, every run is "first sight",
    # and this source goes SILENTLY AND PERMANENTLY DARK. That is unbounded
    # loss masquerading as the bounded one-window trade below, and it is worse
    # than the staleness this watermark exists to stop. Fail loud instead: emit
    # a collector-error line so the section is visibly broken rather than
    # quietly empty.
    # The type check is NOT redundant with the mv test below: `mv -f x d` when
    # d is a DIRECTORY succeeds by moving x INSIDE d, returning 0 while the
    # watermark is never actually recorded. That is precisely the silent-dark
    # path, and it passes a naive exit-status check.
    if [ -e "$REA_OFFSETS" ] && [ ! -f "$REA_OFFSETS" ]; then
      printf '# collector-error: watermark path %s is not a regular file; source WITHHELD\n' \
        "$REA_OFFSETS"
      continue
    fi
    if ! { awk -v p="$f" '$1 != p' "$REA_OFFSETS" 2>/dev/null; printf '%s %s %s\n' "$f" "$size" "$(date +%s)"; } \
         > "$REA_OFFSETS.$$" 2>/dev/null || ! mv -f "$REA_OFFSETS.$$" "$REA_OFFSETS" 2>/dev/null; then
      rm -f "$REA_OFFSETS.$$" 2>/dev/null
      printf '# collector-error: watermark unwritable at %s for %s; source WITHHELD\n' \
        "$REA_OFFSETS" "$f"
      continue
    fi
    [ -n "$prev" ] || continue                   # 1. first sight: watch only
    case "$prev" in *[!0-9]*) continue ;; esac   #    unparseable mark: same
    # 1b. THE MARK MUST HAVE ITS OWN AGE. A byte delta is not a time bound: it
    # says the bytes are NEW SINCE THE MARK, and says nothing about how old the
    # mark is. REA is logon/session triggered with StartWhenAvailable, and this
    # workstation is routinely off for 50-102h, so on the boot run the mark is
    # a gap old and the delta reaches back across the whole gap - undated, so
    # no line filter can touch it. That boot run is precisely when the operator
    # gets paged. The mtime floor cannot help: it grades the FILE, so one write
    # yesterday clears it while the delta still spans days.
    # A mark older than the freshness window is treated as first sight: emit
    # nothing, and the unconditional re-mark above has already re-armed it.
    now=$(date +%s)
    case "$ptime" in ''|*[!0-9]*) continue ;; esac
    [ "$((now - ptime))" -le "$((FRESH_DAYS * 86400))" ] || continue
    [ "$size" -lt "$prev" ] && prev=0            #    rotated: all of it is new
    [ "$size" -gt "$prev" ] || continue          # 2. nothing appended
    [ -n "$(find "$f" -mtime -"$FRESH_DAYS" -print -quit 2>/dev/null)" ] || continue  # 3.
    dsz=$((size - prev))
    if [ "$dsz" -gt "$cap" ]; then
      # Newest bytes win here, unlike head -c elsewhere: this is a delta, so
      # its NEW end is the interesting one. sed 1d drops the partial line the
      # byte window opens in.
      # sed 1d drops the partial first line the byte window opens mid-way
      # through. If the whole window is ONE line with no newline in it, that
      # deletes everything and the burst vanishes silently -- so fall back to
      # the raw window rather than ship nothing.
      new=$(tail -c "$cap" "$f" 2>/dev/null | sed -e '1d')
      [ -n "$new" ] || new=$(tail -c "$cap" "$f" 2>/dev/null)
    else
      new=$(tail -c "+$((prev + 1))" "$f" 2>/dev/null)
    fi
    [ -n "$new" ] || continue
    printf '%s\n' "$new" | sed -e "s#^#[$f] #"
  done
}
export REA_STATE_DIR
export REA_OFFSETS
export -f tailnew

# FRESH_CUTOFF - the oldest calendar date (YYYY-MM-DD, box clock) a log LINE may
# carry and still ship. tailfresh filters whole FROZEN files by mtime, and
# Test-IsStaleFinding drops findings the models correctly date - but the models
# demonstrably omit/mangle `time` (2026-08-13: a 5-day-old tdarr xhr burst and
# 4-day-old bazarr2 SignalR blips all paged straight through that fail-open
# hole). Append-only .err files are the worst case: fresh mtime, tail window
# spanning weeks. Collectors that tail such files compare each line's own
# leading date against this cutoff IN BASH - deterministic, model-free. Lines
# with no leading date pass through (fail open, same law as the ps1 side).
# BOUNDARY IS DELIBERATE: lines dated ON the cutoff day are KEPT (filters use
# d >= c). A cutoff-day line can be as young as ~FRESH_DAYS-1 days (dated
# 23:59, run 00:01), which both sibling layers (find -mtime, the ps1
# TotalDays check) grade FRESH - dropping it here would be fail-closed. The
# residual gray zone at date granularity belongs to the time-aware ps1
# backstop, exactly like undated lines do.
FRESH_CUTOFF=$(date -d "-$FRESH_DAYS days" +%F)
export FRESH_CUTOFF

# freshlines - the FRESH_CUTOFF line filter as a REUSABLE filter, for sources
# whose per-line awk would otherwise be copy-pasted. Added 2026-08-18 for the
# app_extra section, which had no line-date filter at all: those files are
# append-only and some are written WEEKLY, so `tail -n 120` reaches back months
# while `find -mtime` still calls the file fresh. Measured that day:
# qflix-newsletter.err is 843 lines covering ten weekly runs and its tail-120
# window spanned 2026-06-15 to 2026-08-17, which is how a 2026-06-22 Gemini 429
# - from an ai.py that no longer exists anywhere in the deployed tree - paged as
# a live fault. Test-IsStaleFinding's own docstring names this exact file as the
# case it exists to catch; it fails open whenever a model omits `time`, which is
# why the deterministic bash-side filter has to exist too.
#
# STATE MACHINE, not a per-line test: an undated line INHERITS the verdict of
# the last dated line above it, so a multi-line traceback under a stale header
# goes with its header instead of surviving alone. Same fail-open law as every
# other site - lines before any dated line, and files with no dates at all,
# pass through untouched. Accepts YYYY-MM-DD and YYYY/MM/DD (listmonk logs the
# latter); anything else is simply undated and passes.
freshlines() {
  awk -v c="$FRESH_CUTOFF" '
    BEGIN { keep = 1 }
    {
      d = substr($0, 1, 10); gsub("/", "-", d)
      if (d ~ /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/) keep = (d >= c)
      if (keep) print
    }'
}
export -f freshlines

# freshtail <file> <window> - freshlines' verdict, but emitted only for the
# last <window> lines of the file.
#
# Whole-file scanning is what makes the inheritance verdict CORRECT: a line is
# governed by the last dated line above IT, and an EOF-relative window cannot be
# guaranteed to contain that (2026-09-02: bazarr2.err shipped a fifteen-day-old
# traceback through exactly that hole). But scanning whole and EMITTING whole
# moves the fail-open frontier to the TOP OF THE FILE. freshlines passes undated
# lines that precede the first dated line - a deliberate law - and in a tail
# window those are recent, while in a whole-file read they are the oldest lines
# there are. Measured the same day: buildarr.err is 1,383 lines with only ~40
# dated ones, so `freshlines < file | tail -n 60` returned a long undated
# traceback from the head of the file, whose ~100-char lines then ate the entire
# 3000-byte section budget and starved the CURRENT dated warnings out of it -
# the same "one class eats the section" pathology the arr_logs rebuild exists to
# kill, reintroduced from the other end.
#
# Two passes over the same file: pass 1 counts, pass 2 carries the inheritance
# from line 1 but prints only inside the window. Correct verdict, bounded
# output, no ancient leak. Cheap - these files are KB-to-MB, and the whole point
# is that nothing extra is shipped.
freshtail() {
  awk -v c="$FRESH_CUTOFF" -v w="$2" '
    NR == FNR { n++; next }
    FNR == 1  { start = (n > w) ? n - w + 1 : 1; keep = 1 }
    {
      d = substr($0, 1, 10); gsub("/", "-", d)
      if (d ~ /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/) keep = (d >= c)
      if (keep && FNR >= start) print
    }' "$1" "$1"
}
export -f freshtail

# mboxfresh - freshlines for an mbox spool, keyed on the MESSAGE not the line.
# /var/spool/mail/quadstronaut is append-only and nothing rotates it: 439 KB
# holding 428 accumulated "Permission denied" lines as of 2026-08-20. tailfresh
# gates on file MTIME alone, so a single fresh delivery re-ships the whole tail
# window - measured that day `tail -n 500` spanned 2026-08-09 to 2026-08-18, and
# collect()'s `head -c SECTION_CAP` keeps the FIRST bytes of that window, which
# is its OLDEST end. That is how a heartbeat Permission-denied burst already
# fixed on 2026-08-08 (fcb756b restored the exec bit; every script in
# ~/scripts/ops has been 0755 since) paged on 2026-08-20 flagged 2/3 - the
# highest-confidence finding in a 13-issue run, and completely dead.
#
# freshlines cannot do this job: mbox leads with the RFC-2822 envelope
# "From <addr> Tue Aug 18 09:25:01 2026", not a leading ISO date, so every line
# of every message reads as undated and fails open. This filter keys on that
# envelope line, which is BOTH the message boundary and the message date - so
# the verdict is per MESSAGE and a stale body can never outlive its stale
# header. Cron mail is the only source where that distinction is free.
#
# Same fail-open law as freshlines: lines before the first envelope line (the
# partial message any tail window opens in) and envelope lines whose date does
# not parse both pass through. Field-shape checks rather than one long regex -
# awk interval expressions {n} are not portable across mawk/gawk.
mboxfresh() {
  awk -v c="$FRESH_CUTOFF" '
    BEGIN {
      keep = 1
      split("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec", mn, " ")
      for (i = 1; i <= 12; i++) m[mn[i]] = sprintf("%02d", i)
    }
    /^From [^ ]+ / {
      # ctime day is space-padded ("Aug  8"); default FS collapses that, so
      # $4=Mon $5=DD $7=YYYY hold for both one- and two-digit days.
      if (NF >= 7 && ($4 in m) && $7 ~ /^[0-9][0-9][0-9][0-9]$/ && $5 ~ /^[0-9]+$/)
        keep = (sprintf("%s-%s-%02d", $7, m[$4], $5) >= c)
      else
        keep = 1
    }
    { if (keep) print }'
}
export -f mboxfresh

# 1. *arr app logs. REBUILT 2026-08-03. The old block globbed ~/.apps/$app/logs/*.txt
#    and tailed 200 lines from every fresh file: 317 matching files, 195 fresh,
#    4,447,280 bytes of which SECTION_CAP delivered the FIRST 3,000 (0.067%) -
#    measured 26/26 |Debug| lines, ZERO Error. The two genuine Error lines in the
#    entire *arr corpus were structurally unreachable, and the Debug spam that DID
#    arrive produced a false page reported at severity=error. Three fixes:
#     a. *.debug.*/*.trace.* EXCLUDED. Matched against the FULL path, not an
#        extglob - `!(*.debug.*).txt` misses prowlarr.debug.txt, which has no
#        rotation number. 250-255 files drop, 4.45 MB -> ~163 KB. This is not a
#        level filter on a mixed file: those files contain NOTHING but Debug/Trace,
#        and every Error/Fatal line also lands in the non-debug sink.
#     b. SEVERITY-ORDERED EMISSION. Error/Fatal verbatim FIRST, Bazarr second,
#        collapsed Warn LAST. collect_cap truncates with `head -c` (oldest bytes
#        win), so ordering - not a new cap - is what makes an error un-amputatable.
#        Warn is date-collapsed + uniq -c'd (same technique as the tdarr collector
#        below) so a warning repeating 27x costs one line.
#     c. PER-LINE [path] PREFIX, not a header. Byte truncation can orphan a line
#        from a header; it can never orphan a line from its own prefix.
#    norm() redacts URLs and base64 blobs BEFORE budgeting: the three radarr
#    "API Grab Limit reached" Error lines each carry ~3 KB of base64 `link=` and
#    would alone have exceeded the old 3,000-byte cap.
#    Bazarr still rides along here (its ~/.apps/bazarr/log/bazarr.log singular dir
#    and bazarr2's .log/.err match no *.txt glob, fixed 2026-07-25) but now gets a
#    real share of the section instead of being crowded out by Debug.
collect_cap arr_logs 5000 bash -c '
  norm() { sed -E "s#https?://[^ )]+#<url>#g; s#[A-Za-z0-9+/]{40,}={0,2}#<blob>#g"; }
  ARRLOGS=""; nskip=0
  for app in sonarr sonarr2 radarr radarr2 prowlarr; do
    for f in ~/.apps/$app/logs/*.txt; do
      [ -f "$f" ] || continue
      case "$f" in *.debug.*|*.trace.*) nskip=$((nskip+1)); continue ;; esac
      [ -n "$(find "$f" -mtime -"$FRESH_DAYS" -print -quit 2>/dev/null)" ] || continue
      ARRLOGS="$ARRLOGS $f"
    done
  done
  nf=0; for f in $ARRLOGS; do nf=$((nf+1)); done
  # Error/Fatal is counted over the WHOLE fresh file, Warn/Info over the tail
  # window that actually feeds the Warn block. Mixing the two scopes is what
  # made the first version of this census lie - see the note above the emission
  # loop. The two scopes are named in the census text so the numbers are
  # readable without reading this script.
  ne=0; nw=0; nq=0
  for f in $ARRLOGS; do
    ne=$((ne + $(grep -acE "\|(Error|Fatal)\|" "$f" || true)))
    t=$(tail -n 400 "$f")
    nw=$((nw + $(printf "%s\n" "$t" | grep -acE "\|Warn\|" || true)))
    nq=$((nq + $(printf "%s\n" "$t" | grep -acE "\|(Info|Debug|Trace)\|" || true)))
  done
  { echo "===== arr_logs census: fresh_files=$nf debug_trace_files_excluded=$nskip error_fatal=$ne (whole-file) warn=$nw info_debug_trace_lines_dropped=$nq (last-400-lines) ====="
  echo "===== arr_logs: Error/Fatal VERBATIM first, then Bazarr, then Warn (date-collapsed + counted). EVERY line below is prefixed [<path>] - copy THAT into the file field, never infer a path. ====="
  # Error/Fatal: scan the WHOLE fresh file, keep the newest 8. The first version
  # of this block prefiltered with `tail -n 400`, which moved the truncation
  # boundary from bytes to LINES rather than removing it: measured on the box
  # 2026-08-03, 9 fresh Error/Fatal lines existed and only 3 were reachable,
  # while the census - computed over the same 400-line window - printed
  # error_fatal=3, i.e. asserted a total it had not measured. The 6 lost
  # included all four prowlarr ReleaseDownloadException errors that are the
  # paired CAUSE of the three radarr "API Grab Limit reached" errors this
  # section DID deliver: symptom shipped, cause hidden. These lines are rare
  # (9 across 7 files / 3.5 MB), so a full grep is cheap.
  for f in $ARRLOGS; do
    # FRESH_CUTOFF here too (2026-08-15: a July "Indexer is disabled till
    # 07/31" line paged from a sparse-error file - whole-file grep + tail -8
    # ships arbitrarily old lines when a file has few errors). *arr NLog
    # lines lead with "YYYY-MM-DD "; plain fail-open, continuations never
    # match the Error/Fatal grep.
    grep -aE "\|(Error|Fatal)\|" "$f" | awk -v c="$FRESH_CUTOFF" "{ d=substr(\$0,1,10); if (d ~ /^[0-9]{4}-/ && d < c) next; print }" | tail -n 8 | norm | cut -c1-240 | sed -e "s#^#[$f] #"
  done
  # Bazarr, per-file so every line carries its own [path] prefix, and collapsed
  # with uniq -c like the Warn block below. Both fixes are load-bearing:
  #  - the previous version piped tailfresh through `tail -n 20`, which threw
  #    away the "===== path =====" headers that tailfresh emits (74 matched
  #    lines, 2 headers,
  #    0 surviving), so 20 of the section 33 lines had NO provenance while the
  #    banner above asserted that every line does. That is worse than shipping
  #    nothing: real *arr paths sit two lines up for a model to copy.
  #  - uncollapsed, Bazarr took 2,660 of the 5,000-byte budget (53%) for a
  #    handful of repeated classes and amputated the Warn block - the same
  #    "one class eats the section" pathology this rebuild exists to kill.
  for f in ~/.apps/bazarr/log/bazarr.log ~/.apps/bazarr2/logs/bazarr2.log ~/.apps/bazarr2/logs/bazarr2.err; do
    [ -f "$f" ] || continue
    [ -n "$(find "$f" -mtime -"$FRESH_DAYS" -print -quit 2>/dev/null)" ] || continue
    # Drop Bazarr INFO lines before ranking. Bazarr logs provider throttling at
    # INFO with the words "because of: ConnectionError" / "403 Client Error" in
    # the text, so the substring grep matches them; being count-1 like everything
    # else, they then displaced the real ERROR lines from the per-file head -n 4.
    # Line-level freshness: bazarr2.err is append-only, so its last 120 lines
    # span WEEKS while its mtime stays fresh - the 2026-08-10 SignalR blips
    # paged on 2026-08-13 through exactly this window. The two DATED bazarr
    # formats (bazarr.log "YYYY-MM-DD HH:MM:SS|", bazarr2.err
    # "YYYY-MM-DD HH:MM:SS,mmm - ") lead with the date; bazarr2.log is
    # supervisor echoes with NO dates at all and rides the fail-open path
    # whole, by design. Undated lines INHERIT the verdict of the last dated
    # line above them (keep starts 1): a stale Python traceback sheds its
    # dated header AND its undated body together, instead of the body lines
    # surviving as orphaned "(Background on this error at: <url>)" fragments
    # that uniq -c then ranks TOP (measured live 2026-08-14: count 7, the
    # first line models saw for the file). A fully undated file keeps=1
    # throughout - fail open preserved.
    # WHOLE FILE, then trim to the 120-line budget AFTER. This used to be a 3x
    # pre-window (tail -n 360), and the "a header >360 lines up can still leak
    # its tail" residual it documented as ACCEPTED was not theoretical. Measured
    # 2026-09-02: bazarr2.err was 36,400 lines, a SignalR connection-refused
    # traceback sat at line 36,111, and the dated header governing it was at
    # 35,960 - only 151 lines above the traceback, but 81 lines above the START
    # of the tail -360 window. awk therefore opened at keep=1 (fail open), never
    # saw a date, and shipped a 2026-08-18 traceback as current every hour for
    # FIFTEEN DAYS. The pre-window was measured from EOF, but what governs a line
    # is the last dated line above IT, and no EOF-relative window can guarantee
    # to contain that. Reading the file whole is the only sizing that is always
    # right, and it is cheap: 8 MB of awk, with the 120-line trim after, so not
    # one extra byte is shipped. freshtail is freshlines with the emit bounded
    # to the last 120 lines - see its definition for why scanning whole but
    # EMITTING whole moves the fail-open frontier to the top of the file.
    # Delegating to the shared helper also collapses a
    # verbatim copy of that awk (the freshlines date test is the stricter of
    # the two, so it fails open slightly more often - the safe direction).
    freshtail "$f" 120 \
      | grep -aiE "error|exception|fail|traceback" \
      | grep -avE "\|INFO +\|" | norm \
      | cut -c1-220 | sort | uniq -c | sort -rn | head -n 4 | sed -e "s#^#[$f] #"
  done
  for f in $ARRLOGS; do
    tail -n 400 "$f" | grep -aE "\|Warn\|" | norm \
      | sed -E "s/^([0-9]{4}-[0-9]{2}-[0-9]{2}) [0-9:.]+\|/\1|/" \
      | cut -c1-200 | sort | uniq -c | sort -rn | head -n 3 | sed -e "s#^#[$f] #"
  done
  # Line-safe truncation. collect_cap ends in `head -c 5000`, which cuts
  # mid-line: observed endings included a bare "[/home/.../.apps/rad" and a
  # line with a correct-looking path and real timestamp amputated mid-word,
  # which a model cannot distinguish from a complete line. plex_errors got a
  # guard for this and arr_logs - the only section that actually overruns its
  # cap - did not.
  # Budget in awk rather than `head -c ... | sed $d`: sed deletes the last line
  # UNCONDITIONALLY, so on any run that did NOT overrun the cap it would throw
  # away a perfectly good line. This stops at the last line that fits and emits
  # whole lines only, so the outer head -c can never fire. `length` and `print`
  # with no argument are the whole record, so the program needs no $ and stays
  # safe inside the double quotes.
  } | awk "{ n += length + 1; if (n > 4900) exit; print }"
  true
'

# 2. systemd --user journal errors (24h). RESOLVED-FAILURE filter 2026-08-16:
#    a "Failed to start <unit>" line persists in the 24h window long after the
#    unit recovered - the books-stack decommission left two such lines for a
#    canary that was green again within the hour, and models paged on the echo
#    (every maintenance action leaves this same 24h wake). A start-failure is
#    CURRENT only while systemd still holds the unit failed, so ask systemd:
#    drop failed-to-start lines whose unit is not in failed state at fetch
#    time, COUNTED via the census line. Fail open everywhere else: lines that
#    do not carry the failed-to-start shape pass untouched, and a unit that IS
#    still failed keeps every one of its lines.
collect journal_errors bash -c '
  RAW=$(journalctl --user -p err --since "24 hours ago" --no-pager 2>&1)
  KEPT=""; ND=0
  while IFS= read -r line; do
    u=$(printf "%s\n" "$line" | grep -oE "Failed to start [A-Za-z0-9@._-]+\.(service|timer|socket|mount|path)" | sed -E "s/^Failed to start //")
    if [ -n "$u" ] && [ "$(systemctl --user is-failed "$u" 2>/dev/null)" != "failed" ]; then
      ND=$((ND+1)); continue
    fi
    KEPT="$KEPT$line
"
  done <<JEOF
$RAW
JEOF
  [ "$ND" -gt 0 ] && echo "# collector-suppressed: section=journal_errors n=$ND resolved failed-to-start lines (unit not in failed state at fetch time)"
  printf "%s" "$KEPT"
'

# 3. Cron mail spool (last 500 lines; skipped when the spool is stale/frozen).
#    Message-level freshness on top of the mtime gate - see mboxfresh for the
#    2026-08-20 false page this closes. FILTER FIRST, TAIL SECOND: tailing first
#    leaves the oldest survivors at the TOP of the window, which is precisely
#    where collect()'s `head -c SECTION_CAP` lands, so the two orders are not
#    equivalent. Suppression is COUNTED, never silent - same law as the
#    journal_errors and plex_errors collectors below.
collect cron_mail bash -c '
  f=/var/spool/mail/quadstronaut
  [ -f "$f" ] || exit 0
  [ -n "$(find "$f" -mtime -"$FRESH_DAYS" -print -quit 2>/dev/null)" ] || exit 0
  ALL=$(grep -ac "^From [^ ]* " "$f" 2>/dev/null)
  KEPT=$(mboxfresh < "$f" | grep -ac "^From [^ ]* " 2>/dev/null)
  echo "===== $f ====="
  if [ "${ALL:-0}" -gt "${KEPT:-0}" ]; then
    echo "# collector-suppressed: section=cron_mail n=$((ALL-KEPT)) of $ALL messages dated before $FRESH_CUTOFF (append-only spool, nothing rotates it)"
  fi
  mboxfresh < "$f" | tail -n 500
'

# 4. Maint pipeline (state + 6h warnings)
collect maint_state bash -c '
  [ -f ~/.opt/maint/state.json ] && cat ~/.opt/maint/state.json
  echo "----- pusher warnings 6h -----"
  journalctl --user -u manitoba-maint-pusher --since "6h ago" -p warning --no-pager 2>&1 || true
'

# 5. nginx errors (last 200 lines; skipped when the error.log is frozen/stale).
#    freshlines added 2026-08-25: nginx DOES date every line ("2026/08/20
#    04:31:00 [error] ...", which freshlines normalises via its gsub) and this
#    was one of only two dated sources still riding on the mtime gate alone.
#    Declared basis: line (see src_basis).
collect nginx_errors bash -c 'tailfresh 200 ~/.apps/nginx/logs/error.log | freshlines'

# 6. Plex errors. Plex runs as a LinuxServer docker container whose /config maps
#    to ~/.config/plex (NOT ~/.apps/plex, which never existed) - the old path
#    silently captured nothing (fixed 2026-07-25; matches scripts/mcp/logs.py).
#    PMS logs as "<ts> [<thread-id>] ERROR - <msg>" - the brackets hold the THREAD
#    ID, not the level, so grep '\[ERROR\]' matched ZERO lines (fixed 2026-07-28).
#    PROVENANCE FIX 2026-08-03: this collector used `grep -h`, which strips the
#    filename, and emitted no "===== path =====" header - the section contained
#    ZERO path tokens, so the models INVENTED the `file` field and reported both
#    Plex findings against a plex log path that has never existed on this box.
#    Provenance is now COLLECTOR-supplied per line: -h -> -a plus a short
#    [<basename>] tag (full paths measured 116 B/line and cut the payload from
#    22 lines to 12).
#    BUDGET FIX 2026-08-03: 20 of the 22 delivered lines were ONE class
#    (ClientProfileExtra); on Aug 03 it was 45 of 73 total ERRORs. Two benign
#    classes are stripped here so the section's bytes are available to anything
#    else. Each has a TWIN rule in manifest/rea-noise-classes.yaml - the yaml rule
#    is the counted OUTPUT suppressor, this grep is the INPUT budget reclaimer;
#    neither substitutes for the other, and only the yaml half is CI-visible.
#    The suppression is COUNTED, never silent (the 2026-07-25 three-class grep -v
#    has been dropping lines uncounted ever since; that is closed here too).
#    NOTE: this ERE is POSIX (grep -E), the yaml rx is .NET/Python. Two engines,
#    two surfaces, on purpose - C-07 only compiles the yaml one.
collect_cap plex_errors 2200 bash -c '
  PD="$HOME/.config/plex/Library/Application Support/Plex Media Server/Logs"
  echo "===== plex_errors: dir=$PD - EVERY line below is prefixed [<logfile>]; copy THAT into the file field, never infer a path. ====="
  # Line-level freshness (2026-08-16: a 3.5-day-old Aug-12 EAE-timeout burst
  # paged on Aug 15 - PMS rotates WEEKLY at 13:05 Sunday, so mtime-fresh
  # "Plex Media Server.log" holds up to 7 days of lines, and the model-side
  # Test-IsStaleFinding backstop fails open when a model omits/mangles `time`,
  # which is exactly what happened). PMS leads with "%b %d, %Y" (month name,
  # ZERO-PADDED day - verified against every rotated log on the box: "Aug 01,
  # 2026", never "Aug 1"), which awk cannot compare lexically, so the fresh
  # window is enumerated as an alternation of FRESH_DAYS+1 literal dates
  # (same keep-the-boundary-day semantics as the d >= c sites above). Lines
  # not matching the date SHAPE pass through - fail open, same law as every
  # other FRESH_CUTOFF site. Runs before the [logfile] sed prefix, so the
  # date sits at column 1.
  PLEX_FRESH=$(for i in $(seq 0 "$FRESH_DAYS"); do date -d "-$i days" "+%b %d, %Y"; done | paste -sd"|" -)
  # Fail-OPEN guard (adversarial review 2026-08-16): if the date/seq/paste
  # pipeline ever yields an empty alternation, rx would become "^() " which
  # matches NO real PMS line - every dated ERROR would be silently dropped
  # while the census still printed plausible numbers (the exact
  # empty-because-broken failure the banner of this section exists to
  # prevent). An EMPTY awk dynamic regex matches every string, so an empty
  # PLEX_RX disables the drop branch entirely and ships everything.
  # NO APOSTROPHES anywhere in this bash -c body, comments included - the
  # body is a single-quoted shell argument and one apostrophe ends it
  # (broke live 2026-08-16; the bash -n test now pins this).
  PLEX_RX=""; [ -n "$PLEX_FRESH" ] && PLEX_RX="^($PLEX_FRESH) "
  RAW=$(for f in "$PD"/*.log; do
          [ -f "$f" ] || continue
          [ -n "$(find "$f" -mtime -"$FRESH_DAYS" -print -quit 2>/dev/null)" ] || continue
          grep -a " ERROR - " "$f" | awk -v rx="$PLEX_RX" "{ if (\$0 ~ /^[A-Z][a-z]{2} [0-9]{2}, [0-9]{4} /) { if (\$0 !~ rx) next } print }" | sed -e "s#^#[${f##*/}] #"
        done 2>/dev/null)
  # Each alternative below now has a real twin rule in
  # manifest/rea-noise-classes.yaml. Two of them did NOT until 2026-08-03 -
  # CreditsDetectionManager and "Unknown metadata type: folder", which were 897
  # of 1051 fresh lines, i.e. 86% of this section suppression was uncounted,
  # unwritten and invisible to CI while this very comment claimed otherwise.
  # The CreditsDetectionManager alternative is also no longer a bare subsystem
  # name: it matched five distinct message shapes including "Job failed:
  # Scanner job failed", which is a plausible real fault. It now enumerates
  # only the three benign high-volume shapes, matching the yaml rule exactly.
  E1="Caught exception trying to stream file.*protocol is shutdown"
  E2="\[CreditsDetectionManager(\]|/).*(incomplete marker attributes|BufferingLineReader: failed to read line|[Cc]redits detection for item [0-9]+ has failed too many times)"
  E3="Unknown metadata type: folder"
  E4="ClientProfileExtra: missing or invalid type parameter"
  E5="Unable to find metadata agent provider for identifier .(library|iva)"
  EXC="$E1|$E2|$E3|$E4|$E5"
  KEPT=$(printf "%s\n" "$RAW" | grep -avE "$EXC" || true)
  NR=$(printf "%s\n" "$RAW"  | grep -ac . || true)
  NK=$(printf "%s\n" "$KEPT" | grep -ac . || true)
  # PER-CLASS counts, not one aggregate. plex-client-profile-extra is muted on
  # the explicit condition that its ONSET signal is carried by this census, and
  # a single total cannot carry a per-class rate: the folder class alone is ~66%
  # of the volume, so a 3x ClientProfileExtra onset - larger than the event that
  # motivated muting it - moved the old aggregate by ~21% and was
  # indistinguishable from ordinary variance in a different class.
  c1=$(printf "%s\n" "$RAW" | grep -acE "$E1" || true)
  c2=$(printf "%s\n" "$RAW" | grep -acE "$E2" || true)
  c3=$(printf "%s\n" "$RAW" | grep -acE "$E3" || true)
  c4=$(printf "%s\n" "$RAW" | grep -acE "$E4" || true)
  c5=$(printf "%s\n" "$RAW" | grep -acE "$E5" || true)
  echo "# collector-suppressed: section=plex_errors n=$((NR-NK)) of $NR benign ERROR lines (stream-abort=$c1 credits-chatter=$c2 metadata-type-folder=$c3 client-profile-extra=$c4 pseudo-agent=$c5; all classes twinned in manifest/rea-noise-classes.yaml)"
  printf "%s\n" "$KEPT" | tail -n 100 | tail -c 1800 | grep -a "^\[" || true
'
# The banner + census line are emitted UNCONDITIONALLY, including when forwarded=0.
# Deliberate: this source was silently DEAD twice (wrong path until 2026-07-25,
# wrong grep pattern until 2026-07-28) and empty-because-clean must be
# distinguishable from empty-because-broken. The trailing `grep -a "^\["` discards
# the partial line `tail -c` leaves at the head of its window - without it the
# model is handed a fragment with no timestamp and no tag. `tail -c` is still
# needed because collect_cap caps with `head -c` (OLDEST bytes win) and a rotated
# PMS log holds a week of lines.

# 7. Kuma red-state
collect kuma_red bash -c '
  sqlite3 -batch ~/.apps/uptimekuma/kuma.db "
    SELECT m.name FROM monitor m
    JOIN heartbeat h ON h.monitor_id = m.id
    WHERE h.time = (SELECT MAX(time) FROM heartbeat WHERE monitor_id = m.id)
      AND h.status = 0;
  " 2>/dev/null || true
'

# --- Sources added 2026-07-25 (REA live-state completeness audit) ---
# These apps log to FILES, never to the systemd --user journal, so source #2
# could never see them. Paths verified live on the box.

# 8. SABnzbd (usenet): download/postproc/unpack failures, dead providers, stalls.
#    freshlines added 2026-08-25 (same audit as nginx above): sabnzbd.log leads
#    every line with "YYYY-MM-DD HH:MM:SS,mmm::LEVEL::", so it was always
#    line-datable and was never filtered - measured 2026-08-25 its tail-150
#    window spanned 2.7 days. Declared basis: line (see src_basis).
collect sabnzbd bash -c 'tailfresh 150 ~/.apps/sabnzbd/logs/sabnzbd.log | freshlines'

# 9. Tdarr node+server: transcode/ffmpeg crashes, node disconnects.
#    2026-07-28: the .err files are ~90% express stack-trace CONTINUATION lines
#    ("    at Layer.handle ..."), which the system prompt already tells the models
#    to ignore - tailing them raw spent the entire section on noise and starved
#    the real faults. Strip continuations, and ALSO read the timestamped app logs,
#    which is where the actionable errors actually live (the WASM/MediaInfo OOM
#    below repeated 858x over 5 days and REA had never once surfaced it). ERROR
#    lines are timestamp-collapsed to a date and uniq -c'd so a fault that repeats
#    hundreds of times costs one line instead of drowning the cap.
#    2026-08-25, TWO fixes, one page. The .err half used to run FIRST and it
#    measured 2934 bytes against a 3000-byte cap, so the dated, FRESH_CUTOFF-
#    filtered .txt half below got 66 bytes and shipped a truncated header and
#    ZERO log lines - deterministically, every run. 100% of this sections
#    budget went to the two files that carry no dates at all. List order IS the
#    budget priority (collect_cap caps with head -c, oldest bytes win), so the
#    DATED half now runs first and the undated half takes what is left.
#    And the undated half is now on the WATERMARK basis (tailnew) instead of
#    the mtime gate, which is what makes a three-month-old EACCES line
#    structurally unable to ship. See tailnew.
collect tdarr bash -c '
  for f in ~/.apps/tdarr/logs/Tdarr_Server_Log.txt ~/.apps/tdarr/logs/Tdarr_Node_Log.txt; do
    [ -f "$f" ] || continue
    [ -n "$(find "$f" -mtime -"$FRESH_DAYS" -print -quit 2>/dev/null)" ] || continue
    echo "===== $f (ERROR lines, date-collapsed + counted) ====="
    # Line-level freshness (see FRESH_CUTOFF above): these app logs restart
    # in-place daily, so "the whole file" reaches back a month - a 2026-08-08
    # xhr-poll burst paged on 2026-08-13 out of this very grep. Filter BEFORE
    # tail so stale lines cannot eat the 400-line window either. Tdarr lines
    # lead with "[YYYY-MM-DDT..." (substr from 2 skips the bracket). NO
    # verdict inheritance here, unlike the bazarr site: this awk runs AFTER
    # the [ERROR] grep, so adjacent stream lines can be weeks apart, and the
    # only undated lines that reach it are interleave-CORRUPTED real error
    # lines (double-bracket / mid-line splices - live in this log), not
    # traceback continuations. Inheriting a temporally distant stale verdict
    # would silently drop a FRESH corrupted fault (fail-closed); plain
    # pass-through keeps them (fail open).
    grep -a "\[ERROR\]" "$f" \
      | awk -v c="$FRESH_CUTOFF" "{ if (match(\$0, /\[[0-9]{4}-[0-9]{2}-[0-9]{2}T/)) { d=substr(\$0, RSTART+1, 10); if (d < c) next } print }" \
      | tail -n 400 \
      | sed -E "s/^\[([0-9]{4}-[0-9]{2}-[0-9]{2})T[0-9:.]+\]/[\1]/" \
      | sort | uniq -c | sort -rn | head -n 15
  done
  # Undated append-only stderr: watermark basis, newly-appended bytes only.
  # The continuation strip now has to clear tailnews own [path] prefix, so it
  # anchors on "] " followed by the lines original indentation.
  tailnew 900 ~/.apps/tdarr/logs/server.err ~/.apps/tdarr/logs/node.err \
    | grep -vE "^\[[^]]+\][[:space:]][[:space:]]+at " | uniq
'

# 10. Kometa: metadata/collection run failures.
#     WATERMARK basis 2026-08-25: kometa.err is 138,689 lines with ZERO dated
#     lines (measured), so no line filter can bound it and the mtime gate never
#     could - see tailnew. Only bytes appended since the previous run ship.
collect kometa bash -c 'tailnew 1200 ~/.apps/kometa/logs/kometa.err'

# 11. Declarative *arr config sync (buildarr + recyclarr) + bazarr2 drift-sync.
#     Line-level freshness added 2026-08-16: buildarr.err is APPEND-ONLY with a
#     fresh mtime every run, so plain tailfresh shipped a 60-line window
#     spanning 14 days - the 2026-08-02 "Unsupported remote notification"
#     warnings paged on 2026-08-16. Same class as bazarr2.err, same cure:
#     bazarr-style inheritance awk (undated traceback lines shed with their
#     dated header) over a 3x pre-window, trimmed to the 60-line budget AFTER.
#     buildarr.err and bazarr2-sync.err lead with "YYYY-MM-DD "; anything
#     undated rides the fail-open path whole.
collect config_sync bash -c '
  for f in ~/.apps/buildarr/logs/buildarr.err ~/.apps/recyclarr/logs/recyclarr.err ~/.opt/maint/bazarr2-sync.err; do
    [ -f "$f" ] || continue
    [ -n "$(find "$f" -mtime -"$FRESH_DAYS" -print -quit 2>/dev/null)" ] || continue
    echo "===== $f ====="
    # Whole-file scan, 60-line emit, for the reason spelled out on the bazarr
    # leg above: an EOF-relative pre-window cannot guarantee it contains the
    # dated line that governs the lines inside it, and 180 was the narrowest
    # window in the script. buildarr.err is THE file that proved the emit must
    # stay bounded too - see freshtail.
    freshtail "$f" 60
  done
'

# 12. App extras: the customer-facing dashboard, newsletter, unpackerr - error lines only.
#     WIDENED 2026-08-16 (REA source audit): listmonk/upgradinatorr/stream-stats
#     log to FILES, never to the systemd --user journal, and none are ingested
#     into VictoriaLogs (stats-by-app measured live) - so their errors were
#     reachable by NO source at all. The newsletter delivery half + two maint
#     tools. Original three files stay FIRST: collect caps with head -c (oldest
#     bytes win), so list order IS the budget priority, and the proven-signal
#     files must not be starved by the new tail. New files tail -20 (vs 40)
#     each: the files share one 3000-byte section.
#     NARROWED same day: kavita/komga/calibre-web were in the new list for a few
#     hours until all four books-stack apps were decommissioned 2026-08-16. Their
#     log directories are gone, so the entries were removed rather than left as
#     permanently-skipped [ -f ] misses.
collect app_extra bash -c '
  # cut -c1-200 on BOTH loops (adversarial review 2026-08-16): these files
  # share ONE 3000-byte head-c budget, and a single 4KB JSON/traceback line
  # from any of them would otherwise consume the whole section mid-line and
  # starve every file after it - the exact pathology the arr_logs rebuild
  # documents. Same per-line cap family as arr_logs (240) and bazarr (220).
  for f in ~/.apps/qflix-dash/logs/app.log ~/.apps/qflix-newsletter/logs/qflix-newsletter.err; do
    [ -f "$f" ] || continue
    [ -n "$(find "$f" -mtime -"$FRESH_DAYS" -print -quit 2>/dev/null)" ] || continue
    echo "===== $f ====="; tail -n 120 "$f" | freshlines | grep -aiE "error|exception|fail|traceback" | cut -c1-200 | tail -n 40 || true
  done
  # unpackerr SEPARATELY, level-anchored. The word-match grep above reads its
  # per-minute INFO stats line ("Queue: ... 0 failed, 0 deleted") as an error
  # hit - the log was dead 2026-05-22..2026-08-26 so this entry was inert, and
  # the day the log came back alive the models started paging on healthy
  # queue chatter (unpackerr-empty-queue, 2026-08-26). unpackerr levels its
  # lines "[ERROR]"/"[WARN]", so anchor on the bracketed level token; the
  # stats line is [INFO] and can never match.
  f=~/.apps/unpackerr/unpackerr.log
  if [ -f "$f" ] && [ -n "$(find "$f" -mtime -"$FRESH_DAYS" -print -quit 2>/dev/null)" ]; then
    echo "===== $f ====="
    tail -n 400 "$f" | freshlines | grep -aE "\[(ERROR|WARN)\]" | cut -c1-200 | tail -n 20 || true
  fi
  for f in ~/.apps/listmonk/logs/listmonk.log ~/.apps/listmonk/logs/sync.log \
           ~/.apps/upgradinatorr/logs/*.log ~/.apps/stream-stats/logs/kill_stream.log; do
    [ -f "$f" ] || continue
    [ -n "$(find "$f" -mtime -"$FRESH_DAYS" -print -quit 2>/dev/null)" ] || continue
    echo "===== $f ====="
    T=$(tail -n 120 "$f" | freshlines | grep -aiE "error|exception|fail|traceback" || true)
    printf "%s\n" "$T" | cut -c1-200 | tail -n 20 || true
  done
'

# 13. Reaper daily detail log (richer than the state.json summary in source #4).
collect reaper_log bash -c 'tailfresh 120 ~/.opt/maint/reaper/reaper-$(date -u +%Y%m%d).log'

# 14. VictoriaLogs aggregate: normalized cross-app error feed. Drift-proof - it
#     queries a DB, not hardcoded paths, so path drift (like #2 above) can't
#     silently blind it. Covers sonarr/radarr/prowlarr/bazarr/kometa/buildarr/
#     qbit/plex/tautulli/seerr/pusher/webhook/tdarr and more.
# FIX 2026-07-29: this query used to carry global term exclusions "-PMP -includes",
# which LogsQL applies server-side across ALL 13+ aggregated apps - so a real error
# whose text merely contained the word "includes" (e.g. a sonarr schema-validation
# message) was dropped before any model ever saw it, with no audit-log trace
# (second design law violation: a suppression must be logged, never silent).
# CHOSE TO WIDEN rather than scope the exclusion server-side in LogsQL: both
# offending classes (the tdarr "reading 'includes'" express-route bug and Plex
# NAT-PMP/UPnP chatter) already have narrow, unit-tested rules in
# $Script:NoiseFindingRules ('tdarr-express-undefined-includes',
# 'plex-nat-pmp-upnp') that match the exact phrasing rather than a bare token, and
# every drop they make IS counted + logged (`suppressed n=... rules=...`). A
# server-side LogsQL negation would be unverifiable from here (no way to test
# against the live VictoriaLogs instance without touching the box) and, if
# malformed, would silently degrade this whole 13-app source with no
# audit-log signal at all. Removing the blanket exclusion and trusting the
# already-proven code-side enforcement layer is the safer fix.
# LEVEL FIX 2026-08-03: this source is the designated drift-proof *arr error path
# and it was LEVEL-BLIND. scripts/mcp/logs.py captures `msg` as everything after
# the third pipe, so qflix-vlogs-ingest.py posts `_msg` with the NLog level token
# already stripped and `level` as a separate field - and this query neither
# filtered on `level` nor projected it. Measured: 5 of 8 *arr Error-level messages
# (62.5%), including all three "API Grab Limit reached" lines, were unreachable by
# the old term list. Adding level:ERROR to the filter and `level` to `| fields`
# fixes both halves. The inner `head -c 6000` has been dead code
# since SectionByteCap dropped to 3000 on 2026-07-25; collect_cap owns the budget.
# LEVEL FIX, PART 2 (same day). The first version of that fix REGRESSED this
# section and had to be corrected before shipping. Three measured problems:
#  a. `level:ERROR` inflated the candidate pool 8,196 -> 23,500 rows, almost all
#     of it Plex (2,228 -> 16,888, i.e. 72% of the pool), against an unchanged
#     40-row budget. Six consecutive runs returned 37-39 Plex rows out of 40 and
#     as few as ONE *arr row - from the source whose entire purpose is being the
#     drift-proof *arr error path.
#  b. The flooding Plex classes were CreditsDetectionManager and "Unknown
#     metadata type: folder" - exactly what the plex_errors collector 100 lines
#     above spends bytes stripping. Section 14 was re-importing section 6's
#     noise. They are excluded here now, and both have twin rules in the
#     manifest as of this commit, so the suppression is written down twice on
#     purpose rather than being a bare grep.
#  c. `| limit` with no `sort` returns an arbitrary shard: consecutive runs
#     returned different rows, and 40 rows measured ~6.5 KB against a 3,500-byte
#     cap, so `head -c` silently discarded ~45% of what was fetched. Now sorted
#     newest-first and cut to 25 rows = ~2.7 KB, which fits with headroom and is
#     byte-identical across runs.
# `level:CRITICAL` and `level:FATAL` are dropped: scripts/mcp/logs.py normalizes
# CRITICAL -> FATAL before ingest, and a `stats by (level)` over 24h returns only
# DEBUG/INFO/WARN/ERROR/unknown - neither token can ever match. Note that ~50% of
# rows carry level="unknown", so the bare term list still does the work for them.
collect_cap vlogs 3500 bash -c 'VP=$(cat ~/secrets/vlogs.port 2>/dev/null); [ -n "$VP" ] && curl -s -m 8 "http://127.0.0.1:$VP/select/logsql/query" --data-urlencode "query=_time:24h (level:ERROR OR error OR fatal OR exception OR panic OR traceback) -_msg:\"CreditsDetectionManager\" -_msg:\"Unknown metadata type\" | sort by (_time) desc | fields _time,app,level,_msg | limit 25" 2>/dev/null || true'

# --- REA liveness heartbeat -------------------------------------------------
# The WRITER half of scripts/canaries/rea-liveness.sh. Read that header before
# changing anything here; the file below is a CONTRACT, not a log.
#
#   ~/.opt/maint/rea/heartbeat
#   mtime   = written here, by the BOX's own clock, so no workstation clock skew
#             and no second SSH connection. Answers "did REA reach me".
#   content = ONE line, the PREVIOUS run's terminal audit line. Answers "did REA
#             finish". REA's single SSH hop happens BEFORE the model phase, so at
#             this instant THIS run has no verdict yet - the one-run lag is by
#             construction and is what lets the heartbeat report ssh_fail and
#             tunnel_timeout at all, which an end-of-run write never could
#             (by definition its SSH is dead).
#
# WHY THE ARGUMENT ORDER MATTERS: this sits at the END of collection, not the
# top. Reaching here means REA reached the box AND the collection ran to
# completion. Every path that dies before the fetch - tunnel_timeout,
# ollama_down, no_models, no_secrets, SKIPPED-on-lock - never executes this line
# at all, so a failing REA cannot feed its own watchdog: mtime freezes and P1
# eventually fires. Its `fail reason=` line is then carried by the NEXT run and
# reds P3 immediately. A watchdog the failing process keeps feeding is worthless.
#
# The line is base64'd on the workstation side and NEVER interpolated raw: the
# ssh_fail path records the verbatim SSH error as msg=..., which can carry
# quotes, $ and backticks. base64's alphabet contains no shell metacharacter -
# the same reason the section captures above are safe to interpolate.
#
# Empty hole = the workstation had no terminal verdict to report. Write NOTHING.
# An absent heartbeat is exit 2 rea-heartbeat-absent; a fabricated or stale one
# would hold both age predicates green forever, which is the tdarr-healthcheck
# class of failure this canary exists to avoid.
#
# tmp+rename so a truncated write can never be read as a verdict, and the whole
# block is muted: one stray byte on stdout would corrupt the JSON blob below.
REA_HB_B64='__REA_HEARTBEAT_B64__'
if [ -n "$REA_HB_B64" ]; then
  REA_HB_DIR=$HOME/.opt/maint/rea
  {
    mkdir -p "$REA_HB_DIR" &&
    printf '%s' "$REA_HB_B64" | base64 -d > "$REA_HB_DIR/.heartbeat.$$" &&
    printf '\n' >> "$REA_HB_DIR/.heartbeat.$$" &&
    mv -f "$REA_HB_DIR/.heartbeat.$$" "$REA_HB_DIR/heartbeat"
  } >/dev/null 2>&1 || rm -f "$REA_HB_DIR/.heartbeat.$$" >/dev/null 2>&1
fi

# --- src_basis: EVERY section declares HOW its bytes are proven recent -------
#
# Freshness used to be three uncoordinated layers, each independently failing
# open (find -mtime, the FRESH_CUTOFF awks, Test-IsStaleFinding). A source that
# defeated all three - undated, append-only, fresh mtime - shipped unbounded
# history and nothing noticed. tdarr/.err and kometa.err were exactly that, for
# months, and 5 of the 27 noise classes exist only to mop up their stack-trace
# vocabulary. Enumerating a models OUTPUT can never converge; restricting the
# collectors INPUT can, because the input set is finite and ours.
#
# Three legal bases, and a section MUST declare one:
#   line       every shipped line carries its own date, compared to FRESH_CUTOFF
#              (freshlines / mboxfresh / the per-collector awks)
#   watermark  undated append-only stream: only bytes appended since the
#              previous run ship, floored by the file mtime gate (tailnew)
#   query      the collector asks for a bounded window at query time -
#              journalctl --since, LogsQL _time:24h, a live DB/state read, or a
#              path built from todays date
#
# `mtime` alone is NOT legal, on purpose. File mtime bounds the FILE, not its
# CONTENT: node.err had a 1.4-day-old mtime over a tail window spanning 94 days.
# Any section that returns nothing here is a NAMED CONFIGURATION ERROR - it
# ships a `# collector-error:` marker and ZERO content, never a silent pass. A
# 15th source added without a declaration therefore cannot quietly inherit
# "unbounded"; it withholds itself until someone says how it is bounded.
src_basis() {
  case "$1" in
    arr_logs|cron_mail|nginx_errors|plex_errors|sabnzbd|config_sync|app_extra) echo line ;;
    tdarr)   echo line+watermark ;;
    kometa)  echo watermark ;;
    journal_errors|maint_state|kuma_red|reaper_log|vlogs) echo query ;;
    *)       echo "" ;;
  esac
}

# Assemble JSON. base64 -w0 output is safe to interpolate (no quotes, no newlines).
printf '{'
printf '"fetched_at":"%s",' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '"host":"%s",' "$(hostname 2>/dev/null || echo unknown)"
printf '"sources":{'
sep=''
for k in arr_logs journal_errors cron_mail maint_state nginx_errors plex_errors kuma_red sabnzbd tdarr kometa config_sync app_extra reaper_log vlogs; do
  case "$(src_basis "$k")" in
    line|watermark|query|line+watermark)
      v=$(cat "$TMP/$k" 2>/dev/null || echo '') ;;
    *)
      v=$(printf '# collector-error: section=%s declares no freshness basis in src_basis; content WITHHELD\n' "$k" | base64 -w0) ;;
  esac
  printf '%s"%s":"%s"' "$sep" "$k" "$v"
  sep=','
done
printf '}}'
printf '\n'
'@
    $bash = $bash -replace '__SECTION_CAP__', [string]$Script:SectionByteCap
    $bash = $bash -replace '__FRESH_DAYS__', [string]$Script:FreshDays

    # rea-liveness heartbeat: the previous run's terminal audit line, base64'd.
    # See the block it fills, near the end of the script above.
    # try/catch, not bare: a heartbeat we cannot COMPOSE must never break the
    # audit itself. Get-RemoteHeredoc is called inside Invoke-RemoteFetch's try,
    # so an exception here would be caught and misreported as `fail reason=
    # ssh_fail` - a workstation bookkeeping bug wearing a network fault's name.
    # Empty on any failure, and empty means the box writes nothing.
    $hbB64 = ''
    try {
        $hbLine = Get-LastTerminalAuditLine
        if ($hbLine) { $hbB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($hbLine)) }
    } catch { $hbB64 = '' }
    # .Replace(), not -replace: the replacement side of -replace treats $ as a
    # capture reference. Base64 never contains one, but the next person editing
    # this hole should not have to know that to stay safe.
    $bash = $bash.Replace('__REA_HEARTBEAT_B64__', $hbB64)
    # Normalize CRLF -> LF (bash hates CRLF)
    return ($bash -replace "`r", '')
}

function Invoke-RemoteFetch {
    param([string]$OutPath, [int]$TimeoutSec = $Script:SshTimeoutSec)
    $sshHost = Resolve-SshHost
    $heredoc = Get-RemoteHeredoc

    # Use System.Diagnostics.Process directly: Start-Process -PassThru
    # does not reliably expose .ExitCode when stdout/stderr are redirected.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName  = $Script:SshExe
    $psi.UseShellExecute        = $false
    $psi.RedirectStandardInput  = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow         = $true
    # PS 5.1 lacks ArgumentList; use Arguments. None of our args contain spaces.
    $psi.Arguments = (@(
        '-o','BatchMode=yes',
        '-o','ServerAliveInterval=15',
        '-o','ServerAliveCountMax=2',
        '-o','StrictHostKeyChecking=accept-new',
        $sshHost,
        'bash','-s'
    ) -join ' ')

    $p = [System.Diagnostics.Process]::Start($psi)
    try {
        # Write heredoc to stdin and close, so ssh sees EOF. EXPLICIT BOM-LESS
        # BYTES, not StreamWriter.Write: under a UTF-8 console (chcp 65001)
        # the default writer for redirected stdin emits an EF BB BF preamble
        # on first write, and remote bash then tries to run a line-1
        # "<BOM>#!/usr/bin/env" command that does not exist - hit live
        # 2026-08-16 running -Once from a UTF-8 shell. The logon Task
        # Scheduler context uses the OEM codepage and never tripped it. The
        # heredoc is pure ASCII, so UTF8-no-BOM bytes are identical to what
        # the OEM path always sent.
        $hdBytes = [System.Text.UTF8Encoding]::new($false).GetBytes($heredoc)
        $p.StandardInput.BaseStream.Write($hdBytes, 0, $hdBytes.Length)
        $p.StandardInput.BaseStream.Flush()
        $p.StandardInput.Close()

        $stdoutTask = $p.StandardOutput.ReadToEndAsync()
        $stderrTask = $p.StandardError.ReadToEndAsync()

        if (-not $p.WaitForExit([int]($TimeoutSec * 1000))) {
            try { $p.Kill() } catch {}
            throw "ssh fetch exceeded $TimeoutSec sec"
        }
        $stdoutTask.Wait()
        $stderrTask.Wait()

        if ($p.ExitCode -ne 0) {
            throw "ssh fetch exit $($p.ExitCode): $($stderrTask.Result)"
        }
        $blob = $stdoutTask.Result
        if ($OutPath) { Set-Content -LiteralPath $OutPath -Value $blob -Encoding UTF8 -NoNewline }
        return $blob
    } finally {
        try { $p.Dispose() } catch {}
    }
}

# ---------- JSON extractor + blob decoder ----------
function Extract-JsonArray {
    # REWRITTEN 2026-08-18 (council, reviewer-reproduced live). The original
    # anchored on IndexOf('[') - the FIRST bracket in the text - so a model
    # whose prose prefix contained a bracket ("[INFO] ..."), or that emitted a
    # decorative empty [] before the real array, had its actual findings
    # silently discarded or replaced with []. And the truncation salvage cut
    # at LastIndexOf('}'), which is not quote-aware: a '}' inside a string
    # value produced an unparseable candidate and the whole batch was lost.
    #
    # Now: EVERY '[' is a candidate start. Each gets the same quote-aware
    # balanced scan, which also records the end of the last COMPLETE
    # top-level element (a '}' at bracket depth 1, outside strings) - that
    # position is the quote-aware salvage cut. Preference order:
    #   1. first CLOSED candidate parsing to a non-empty array of objects
    #   2. first UNCLOSED candidate salvaging to a non-empty array of objects
    #   3. first CLOSED candidate that parses at all (a clean run's bare [])
    #   4. $null
    # Salvage may only ever widen what parses, never invent structure; when it
    # is used, $Script:LastExtractSalvaged is set so the call site can COUNT
    # the truncation - suppression is audited, and truncation loss must be
    # too, not silent.
    param([string]$Text)
    $Script:LastExtractSalvaged = $false
    if (-not $Text) { return $null }

    $firstClosedAny = $null
    $pos = $Text.IndexOf('[')
    while ($pos -ge 0) {
        $depth = 0; $inStr = $false; $esc = $false
        $lastElemEnd = -1
        $closedEnd   = -1
        for ($j = $pos; $j -lt $Text.Length; $j++) {
            $ch = $Text[$j]
            if ($inStr) {
                if ($esc)        { $esc = $false; continue }
                if ($ch -eq '\') { $esc = $true;  continue }
                if ($ch -eq '"') { $inStr = $false }
                continue
            }
            if ($ch -eq '"') { $inStr = $true; continue }
            if ($ch -eq '[') { $depth++ }
            elseif ($ch -eq '}') {
                if ($depth -eq 1) { $lastElemEnd = $j }
            }
            elseif ($ch -eq ']') {
                $depth--
                if ($depth -eq 0) { $closedEnd = $j; break }
            }
        }
        if ($closedEnd -ge 0) {
            $candidate = $Text.Substring($pos, $closedEnd - $pos + 1)
            try {
                $parsed = $candidate | ConvertFrom-Json
                # ConvertFrom-Json flattens [] to $null in PS 5.1; preserve as empty array.
                if ($null -eq $parsed) { $arr = @() }
                elseif ($parsed -is [array] -or $parsed -is [System.Object[]]) { $arr = $parsed }
                else { $arr = @($parsed) }
                $objs = @($arr | Where-Object { $_ -is [pscustomobject] -or $_ -is [hashtable] })
                if ($objs.Count -gt 0) { return ,$arr }          # preference 1
                if ($null -eq $firstClosedAny) { $firstClosedAny = ,$arr }
            } catch { }
        } elseif ($lastElemEnd -gt $pos) {
            # Unclosed with at least one complete element: quote-aware salvage.
            $candidate = $Text.Substring($pos, $lastElemEnd - $pos + 1) + ']'
            try {
                $parsed = $candidate | ConvertFrom-Json
                if ($null -ne $parsed) {
                    $arr = if ($parsed -is [array] -or $parsed -is [System.Object[]]) { $parsed } else { @($parsed) }
                    $objs = @($arr | Where-Object { $_ -is [pscustomobject] -or $_ -is [hashtable] })
                    if ($objs.Count -gt 0) {
                        $Script:LastExtractSalvaged = $true       # preference 2
                        return ,$arr
                    }
                }
            } catch { }
        }
        $pos = $Text.IndexOf('[', $pos + 1)
    }
    if ($null -ne $firstClosedAny) { return $firstClosedAny }     # preference 3
    return $null
}

function ConvertFrom-FetchedBlob {
    param([string]$Json)
    $obj = $Json | ConvertFrom-Json
    $decoded = @{}
    foreach ($prop in $obj.sources.PSObject.Properties) {
        if (-not $prop.Value) { $decoded[$prop.Name] = ''; continue }
        try {
            $bytes = [Convert]::FromBase64String($prop.Value)
            $decoded[$prop.Name] = [System.Text.Encoding]::UTF8.GetString($bytes)
        } catch {
            $decoded[$prop.Name] = ''
        }
    }
    return [pscustomobject]@{
        fetched_at = $obj.fetched_at
        host       = $obj.host
        sources    = [pscustomobject]$decoded
    }
}

# ---------- Consensus ----------
function Get-Consensus {
    param([array]$Findings)
    $sevRank = @{ 'warning' = 1; 'error' = 2; 'critical' = 3 }
    $rankSev = @{ 1 = 'warning'; 2 = 'error'; 3 = 'critical' }
    $groups  = @{}
    foreach ($f in $Findings) {
        if (-not $f) { continue }
        $sig = ([string]$f.signature).Trim().ToLowerInvariant()
        if (-not $sig) { continue }
        $sevKey = ([string]$f.severity).ToLowerInvariant()
        $sevR   = if ($sevRank.ContainsKey($sevKey)) { [int]$sevRank[$sevKey] } else { 1 }
        if (-not $groups.ContainsKey($sig)) {
            $groups[$sig] = @{
                signature      = $sig
                time           = [string]$f.time
                app            = [string]$f.app
                file           = [string]$f.file
                severity_rank  = $sevR
                summary        = [string]$f.summary
                excerpt        = [string]$f.excerpt
                models_flagged = @([string]$f._model)
            }
        } else {
            $g = $groups[$sig]
            if ($sevR -gt $g.severity_rank) { $g.severity_rank = $sevR }
            $newSummary = [string]$f.summary
            if ($newSummary.Length -gt $g.summary.Length) { $g.summary = $newSummary }
            $newExcerpt = [string]$f.excerpt
            if ($newExcerpt.Length -gt $g.excerpt.Length) { $g.excerpt = $newExcerpt }
            $newTime = [string]$f.time
            if ($newTime -and ($newTime -lt $g.time)) { $g.time = $newTime }
            $newModel = [string]$f._model
            if ($newModel -and ($g.models_flagged -notcontains $newModel)) {
                $g.models_flagged += $newModel
            }
        }
    }
    # Second-level merge: one underlying LOG LINE, many invented signatures.
    # Signatures are model-authored strings, so grouping on them alone cannot
    # dedup - on 2026-08-25 one sonarr Discord line paged as FOUR fields
    # (discord-proxy-post-failure / discord-notification-failure /
    # discord-proxy-failure / ...-failure-2). The excerpt is collector-anchored,
    # so it is the honest key: strip digits (kills timestamps), collapse
    # non-alphanumerics, lowercase. A key that CONTAINS another group's key is
    # the same line with more context kept (models trim excerpts differently),
    # so containment merges too - guarded at >=40 chars so two genuinely
    # different findings cannot collide on a short shared phrase.
    $merged = @()
    $sorted = @($groups.Values | Sort-Object @{Expression={ ([string]$_.excerpt).Length }; Descending=$true})
    foreach ($g in $sorted) {
        $key = (([string]$g.excerpt).ToLowerInvariant() -replace '[0-9]', '' -replace '[^a-z]+', ' ').Trim()
        $host_ = $null
        if ($key.Length -ge 40) {
            foreach ($m in $merged) {
                if ($m.key.Length -ge 40 -and $m.key.Contains($key)) { $host_ = $m; break }
            }
        }
        if ($host_) {
            $h = $host_.g
            if ($g.severity_rank -gt $h.severity_rank) { $h.severity_rank = $g.severity_rank }
            if (([string]$g.summary).Length -gt ([string]$h.summary).Length) { $h.summary = $g.summary }
            if ($g.time -and (-not $h.time -or $g.time -lt $h.time)) { $h.time = $g.time }
            foreach ($mm in @($g.models_flagged)) {
                if ($h.models_flagged -notcontains $mm) { $h.models_flagged += $mm }
            }
        } else {
            $merged += @{ key = $key; g = $g }
        }
    }
    $out = @()
    foreach ($entry in $merged) {
        $g = $entry.g
        $g['severity'] = $rankSev[$g.severity_rank]
        $g.Remove('severity_rank') | Out-Null
        # Carry the collector-anchored merge key out of this function: it is the
        # ONLY stable identity a finding has across runs. `signature` is a
        # model-authored string that is re-invented every hour (one bazarr line
        # has been seen as bazarr-connection-refused / bazarr:connection-refused
        # / bazarr:sonarr-api-connection-refused / bazarr-local-service-
        # connection-failed), so keying cross-run suppression on it would never
        # match and would suppress nothing. The excerpt key is digit-stripped,
        # so the same line with a different timestamp is the SAME key - which is
        # exactly the identity a repeat-page ledger needs.
        $g['page_key'] = $entry.key
        $out += [pscustomobject]$g
    }
    # Emit via pipeline; caller's @(...) wrap collects 0..N items reliably.
    $out | Sort-Object @{Expression={ $sevRank[$_.severity] }; Descending=$true}, time
}

# ---------- Ownership gate: does a MONITOR already own this? ----------
# REA reads LOGS, which are history. Kuma and the 35 canaries measure LIVE
# STATE. A log line saying "connection refused at 14:55" is a true statement
# about 14:55 and tells you nothing about now - so paging on it is reporting,
# not alerting.
#
# Measured over 2026-09-01T17Z..2026-09-02T17Z: 45 pages, of which 2 were the
# real fault (Plex was down) and 43 were not. The single largest family, 14 of
# them, was one app logging that it could not reach another app:
#
#   bazarr2  -> 127.0.0.1:17003   (sonarr2)
#   tautulli -> 172.17.0.1:17025  (plex)
#   stream-stats -> 127.0.0.1:17025 (plex)
#   listmonk -> 127.0.0.1:42009   (postgres)
#
# Every one of those targets is a managed stack app with its own Kuma monitor.
# So the finding is ALWAYS a duplicate, in both directions:
#   - target monitor GREEN -> the blip already resolved; there is nothing to do.
#   - target monitor RED   -> the monitor already paged; saying it twice is worse
#                             than saying it once, because now two alerts have to
#                             be correlated by a human at 3am.
# Either way the operator learns nothing from the second message. REA's actual
# job is the RESIDUE: faults that no monitor and no canary can see. This gate
# keeps it to that job.
#
# THE PORT IS THE EVIDENCE, not the app name. Model-authored `app` fields are
# unreliable and app names appear in prose; a port number in a connection error
# is emitted by the failing client library and cannot be hallucinated into a
# different service. secrets/*.port IS the stack's port registry - if a port is
# declared there, that app is managed, pushed to Kuma by the pusher, and owned.
#
# ACCEPTED RESIDUAL, stated out loud: a broken LINK between two apps that are
# both individually green (the bazarr2 -> sonarr2 SignalR negotiate path is the
# real example) is held by this gate and will not page. That is deliberate. Per
# the compartmentalize law a link that matters gets its OWN canary with its own
# timer and its own Kuma check; it does not get an LLM re-reading a traceback
# every hour. Held findings are written to the audit log by rule id and counted
# on the daily heartbeat, so a persistent one is visible in review the next day.
#
# FAILS OPEN at every step: no repo root, no secrets dir, no port in the
# excerpt, an unrecognised port, or any exception at all -> the finding pages.

$Script:StackPortMap = $null

function Get-StackPortMap {
    # { "17025" -> "plex"; "42009" -> "postgres"; ... } from secrets/*.port.
    # Cached per process - this is read once per finding otherwise.
    if ($null -ne $Script:StackPortMap) { return $Script:StackPortMap }
    $map = @{}
    try {
        $dir = Join-Path (Get-RepoRoot) 'secrets'
        if (Test-Path -LiteralPath $dir) {
            foreach ($f in Get-ChildItem -LiteralPath $dir -Filter '*.port' -File -ErrorAction SilentlyContinue) {
                $v = (Get-Content -Raw -LiteralPath $f.FullName -ErrorAction SilentlyContinue)
                if ($null -eq $v) { continue }
                $v = $v.Trim()
                if ($v -match '^\d{2,5}$') { $map[$v] = $f.BaseName }
            }
        }
    } catch { }
    $Script:StackPortMap = $map
    return $map
}

# A connection-level failure, as the client libraries in this stack actually
# word it. Deliberately NOT "error" or "failed" - this must match the transport
# giving up, never an application-level error that merely mentions a port.
$Script:ConnFailureRx =
    '(?i)(connection refused|econnrefused|failed to establish a new connection|' +
    'max retries exceeded|newconnectionerror|connectionerror:|' +
    'connect: connection refused|no route to host|connection timed out)'

function Get-ConnectionTargetPort {
    <#
      Pull the TARGET port out of a connection-failure excerpt. Recognises the
      three shapes this stack emits:
        urllib3 / requests : host='127.0.0.1', port=17003
        Go / node          : dial tcp 127.0.0.1:42009 / ECONNREFUSED 172.17.0.1:17025
        bare URL           : http://172.17.0.1:17025/status/sessions
      Returns the port as a string, or '' when there is no unambiguous one.
    #>
    param([string]$Text)
    if (-not $Text) { return '' }
    $m = [regex]::Match($Text, '(?i)\bport\s*=\s*(\d{2,5})')
    if ($m.Success) { return $m.Groups[1].Value }
    $m = [regex]::Match($Text, '(?i)\b(?:\d{1,3}\.){3}\d{1,3}:(\d{2,5})\b')
    if ($m.Success) { return $m.Groups[1].Value }
    return ''
}

function Test-IsOwnedByAMonitor {
    <#
      Returns a rule id when this finding is a connection failure against a
      MANAGED stack app - meaning a Kuma monitor already owns that target and
      the page would be a duplicate either way. Returns $null to page.

      Fails OPEN: anything it cannot establish means it pages.
    #>
    param($Finding)
    try {
        if (-not $Finding) { return $null }
        $excerpt = Get-FieldOrEmpty $Finding 'excerpt'
        # EXCERPT-SCOPED on purpose. The excerpt is collector-anchored (it has
        # to survive Resolve-FindingFile's provenance check); summary and
        # signature are model prose, and a speculative summary must never be
        # able to mute a finding whose real evidence is something else. Same
        # law as the 2026-08-16 excerpt-scoped rules.
        if (-not $excerpt) { return $null }
        if ($excerpt -notmatch $Script:ConnFailureRx) { return $null }
        $port = Get-ConnectionTargetPort $excerpt
        if (-not $port) { return $null }
        $map = Get-StackPortMap
        if ($map.Count -eq 0) { return $null }   # no registry -> cannot judge -> page
        $app = $map[$port]
        if (-not $app) { return $null }          # unmanaged target -> REA's actual job
        return "monitor-owns-target:$app"
    } catch {
        return $null
    }
}

# ---------- Cross-run page ledger ----------
# Get-Consensus dedups within ONE run. Nothing dedupped across runs, so the
# hourly cadence multiplied every finding by however many hours its evidence
# stayed inside the FreshDays window. This is that missing half.
#
# Its OWN file, not state.json: Read-State projects onto a fixed key set and
# silently drops anything else, so a map parked there would be erased by the
# next Write-State. Keeping it separate also means a corrupt ledger cannot take
# the deadman state with it.

function Get-PageLedgerPath { Join-Path (Get-StateDir) 'page-ledger.json' }

function Read-PageLedger {
    # { page_key -> unix seconds of last page }. Any failure reads as EMPTY,
    # which pages. See Select-DuePageGroups for why that direction is the safe
    # one to fail in.
    $p = Get-PageLedgerPath
    if (-not (Test-Path -LiteralPath $p)) { return @{} }
    try {
        $obj = (Get-Content -Raw -LiteralPath $p) | ConvertFrom-Json
        $h = @{}
        foreach ($prop in $obj.PSObject.Properties) {
            $v = 0.0
            if ([double]::TryParse([string]$prop.Value, [ref]$v)) { $h[$prop.Name] = $v }
        }
        return $h
    } catch { return @{} }
}

function Write-PageLedger {
    param([hashtable]$Ledger)
    $p   = Get-PageLedgerPath
    $tmp = "$p.tmp"
    ($Ledger | ConvertTo-Json -Depth 3) | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -Force -LiteralPath $tmp -Destination $p
}

function Select-DuePageGroups {
    <#
      Split consensus groups into the ones the operator should actually be
      pinged about now, and the ones already paged inside the cooldown.

      Returns a hashtable: @{ Due = @(...); Muted = @(...) }.

      FAILS OPEN, deliberately. If the ledger is unreadable, unparseable, or
      unwritable, every group comes back Due and the page goes out. A bug in a
      noise suppressor must never be able to eat "Plex is down"; the worst case
      of failing open is the storm that already existed, and the worst case of
      failing closed is a silence nobody notices until a member complains.

      Stamps the ledger for everything it returns Due, and prunes entries older
      than 2x the cooldown so the file stays bounded without a separate job.

      -Stamp:$false reads the ledger without writing it. The caller passes that
      under -DryRun: a dry run posts nothing, so if it stamped, it would mute
      the next REAL page for 24h and the operator would lose an alert to a
      diagnostic command they ran. Muting must only ever be paid for by a ping
      that actually went out.
    #>
    param([array]$Groups, [switch]$Stamp = $true)
    $due = @(); $muted = @()
    try {
        $now      = [int][double]::Parse(((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds)
        $cooldown = $Script:PageCooldownHours * 3600
        $ledger   = Read-PageLedger
        foreach ($g in $Groups) {
            $key = ''
            if ($g.PSObject.Properties.Name -contains 'page_key') { $key = [string]$g.page_key }
            # A group with no usable key has no identity to dedup on, so it
            # pages. Never the other way round.
            if (-not $key) { $due += $g; continue }
            # 2026-09-14: CONTAINMENT across runs, not just exact match. Models
            # trim excerpts differently run to run ("error req ab downloadcontainer
            # expected ..." one hour, "downloadcontainer expected ..." the next), so
            # an exact-key ledger let ONE 89-line Plex burst page three times in
            # nine hours under three signatures. Get-Consensus already merges by
            # containment within a run at the same >=40-char guard; the ledger
            # now applies the same identity across runs. Short keys still need an
            # exact match so two different findings cannot collide on a phrase,
            # AND the shorter key must be at least 60% of the longer one: a
            # trim differs by a prefix ("error req ab ", 13 of 73 chars), while
            # a different fault that QUOTES an earlier one as context is mostly
            # new text (adversarial review 2026-09-14 built a 57-char outage
            # line inside a 133-char prowlarr finding, 43%, and it muted).
            $hit = $null
            if ($ledger.ContainsKey($key)) { $hit = $key }
            elseif ($key.Length -ge 40) {
                foreach ($lk in @($ledger.Keys)) {
                    $lks = [string]$lk
                    if ($lks.Length -lt 40) { continue }
                    if (-not ($lks.Contains($key) -or $key.Contains($lks))) { continue }
                    $shorter = [Math]::Min($lks.Length, $key.Length)
                    $longer  = [Math]::Max($lks.Length, $key.Length)
                    if ($shorter -ge (0.6 * $longer)) { $hit = $lks; break }
                }
            }
            $last = $null
            if ($hit) { $last = $ledger[$hit] }
            if ($null -ne $last -and ($now - $last) -lt $cooldown -and ($now - $last) -ge 0) {
                $muted += $g
            } else {
                $ledger[$key] = $now
                $due += $g
            }
        }
        # Prune: an entry twice the cooldown old can no longer mute anything.
        foreach ($k in @($ledger.Keys)) {
            if (($now - $ledger[$k]) -gt (2 * $cooldown)) { $ledger.Remove($k) }
        }
        if ($Stamp) { Write-PageLedger $ledger }
    } catch {
        Write-AuditLog "page-ledger-failed-open err=$($_.Exception.Message -replace '\s+',' ')"
        return @{ Due = @($Groups); Muted = @() }
    }
    return @{ Due = $due; Muted = $muted }
}

# ---------- Discord payload builders ----------
function New-DiscordErrorPayload {
    param([array]$Groups, [string]$OperatorId, [int]$ModelCount, [int]$DurationSec)
    $fields = @()
    foreach ($g in $Groups) {
        $modelList = ($g.models_flagged -join ', ')
        $fraction  = "$($g.models_flagged.Count)/$ModelCount"
        $excerpt   = [string]$g.excerpt
        if ($excerpt.Length -gt 300) { $excerpt = $excerpt.Substring(0,297) + '...' }
        $value = "**$($g.app)** | ``$($g.file)`` | $($g.severity)`n$($g.summary)`n_flagged by: $modelList ($fraction)_`n``````$excerpt```````n"
        if ($value.Length -gt 1024) { $value = $value.Substring(0,1021) + '...' }
        $nameRaw = "$($g.app) - $($g.signature)"
        if ($nameRaw.Length -gt 256) { $nameRaw = $nameRaw.Substring(0,253) + '...' }
        $fields += @{ name = $nameRaw; value = $value; inline = $false }
    }
    return @{
        content          = "<@$OperatorId>"
        allowed_mentions = @{ parse = @(); users = @($OperatorId) }
        embeds = @(@{
            title     = "[ALERT] QFlix REA - $($Groups.Count) issue$(if($Groups.Count -ne 1){'s'})"
            color     = 15158332
            timestamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            fields    = $fields
            footer    = @{ text = "models: $ModelCount | sources: 14 | duration: ${DurationSec}s" }
        })
    }
}

function New-DiscordHeartbeatPayload {
    param([int]$ModelCount, [int]$SoloCount = 0, [int]$MutedCount = 0)
    # SoloCount surfaces the consensus-floor remainder (2026-08-26): findings
    # only one model flagged never ping, but their existence shows here so a
    # persistent real fault is visible in the daily review without a page.
    $solo = if ($SoloCount -gt 0) { " | $SoloCount single-model finding(s) held (see audit log)" } else { "" }
    # MutedCount is the SAME honesty obligation for the cross-run page ledger
    # (2026-09-02). A run whose only findings were already-paged repeats reaches
    # this builder, and titling that "clean" would be a lie of exactly the kind
    # the quorum_degraded fix existed to stop: still-broken is not clean, it is
    # already-reported. Naming the count keeps the daily embed truthful without
    # re-pinging.
    $muted = if ($MutedCount -gt 0) { " | $MutedCount repeat finding(s) muted (already paged within ${Script:PageCooldownHours}h)" } else { "" }
    $title = if ($MutedCount -gt 0) { '[OK] QFlix REA - no new findings' } else { '[OK] QFlix REA clean' }
    return @{
        content = ''
        embeds  = @(@{
            title       = $title
            color       = 3066993
            description = "$ModelCount models | 14 sources | 0 new findings$solo$muted"
            timestamp   = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        })
    }
}

function New-DiscordDeadmanPayload {
    # Generic "audit could not run" alert. Title/Description default to the
    # original Ollama-down wording so every existing caller (and the test that
    # pins that exact shape) is unaffected; the five other early-return failure
    # paths in Invoke-Main (fix 2026-07-29: REA could go permanently dark - see
    # Send-DeadmanAlert below) pass their own reason-specific Title/Description
    # through the same builder instead of duplicating the embed shape.
    param(
        [string]$OperatorId,
        [string]$Title       = '[WARN] QFlix REA - Ollama appears down',
        [string]$Description = "Workstation Ollama at $($Script:OllamaBase)/api/tags is not responding. Audit skipped.`n`nNext check: next Windows logon. Manual fix: ``ollama serve`` (or restart the Ollama service)."
    )
    return @{
        content          = "<@$OperatorId>"
        allowed_mentions = @{ parse = @(); users = @($OperatorId) }
        embeds = @(@{
            title       = $Title
            color       = 16753920
            description = $Description
            timestamp   = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        })
    }
}

# ---------- Ollama generate + Discord POST ----------
function Invoke-Model {
    # CPU priority is NOT handled here. Ollama spawns llama-server at a
    # hard-coded AboveNormal, and REA's Interactive/Limited token gets Access
    # Denied demoting a runner owned by the S4U "Ollama Serve" task (tested
    # 2026-09-13). Demotion lives in the elevated \Archangel\Ollama Runner
    # Priority watcher instead.
    param([string]$Model, [string]$Prompt, [string]$SystemPrompt, [int]$TimeoutSec = $Script:ModelTimeoutSec)
    $body = @{
        model   = $Model
        system  = $SystemPrompt
        prompt  = $Prompt
        stream  = $false
        # num_ctx EXPLICIT (2026-08-18). Without it Ollama uses its server
        # default window, and when system+blob outgrow it the PROMPT is
        # silently truncated FROM THE TOP - which amputates the JSON-format
        # instructions first, so every model answers in prose and the run
        # grades all_models_noop. That is exactly what 22 consecutive hourly
        # runs did after the 2026-08-18 host-reboot storm maxed every blob
        # section cap (~45KB blob vs ~15KB on a quiet day). 24576 holds
        # worst-case blob (~10k tok) + system prompt (~3k) + output (~3k)
        # with slack, and is VRAM-safe for the 8b/7b dense models; the 30b
        # is MoE (3B active) and takes it too.
        # num_predict 2048 -> 3072: a storm blob makes models WANT to emit
        # dozens of findings; the cap used to cut the array mid-element and
        # the parser (correctly) refused it. Belt: bigger budget. Suspenders:
        # the prompt now caps findings at 10. Braces: Extract-JsonArray
        # salvages a truncated tail.
        options = @{ temperature = 0; num_predict = 3072; num_ctx = 24576 }
        # Unload as soon as this answer is returned (2026-09-13). Each model is
        # used exactly once per run, so Ollama's default keep-alive only held
        # 5-7 GB of VRAM idle for half an hour after the run, and a still-
        # resident predecessor pushed the next model partly onto the CPU.
        keep_alive = 0
    } | ConvertTo-Json -Depth 10 -Compress
    try {
        $resp = Invoke-RestMethod -Uri "$Script:OllamaBase/api/generate" `
                  -Method Post -Body $body -ContentType 'application/json' `
                  -TimeoutSec $TimeoutSec -ErrorAction Stop
        if ($resp.PSObject.Properties.Name -contains 'response') {
            return [string]$resp.response
        }
        return $null
    } catch { return $null }
}

function Send-Discord {
    param([string]$WebhookUrl, [hashtable]$Payload)
    $json = $Payload | ConvertTo-Json -Depth 12 -Compress
    try {
        Invoke-RestMethod -Uri $WebhookUrl -Method Post -Body $json `
            -ContentType 'application/json' -TimeoutSec 15 -ErrorAction Stop | Out-Null
        return $true
    } catch { return $false }
}

# ---------- Tunnel + Ollama gates ----------
function Wait-ForTunnel {
    param(
        [int]$Port    = $Script:TunnelProbePort,
        [int]$MaxSec  = $Script:TunnelWaitSec,
        [int]$PollSec = 5
    )
    $deadline = (Get-Date).AddSeconds($MaxSec)
    while ((Get-Date) -lt $deadline) {
        $c = New-Object Net.Sockets.TcpClient
        try {
            $t = $c.ConnectAsync('127.0.0.1', $Port)
            if ($t.Wait(1000) -and $c.Connected) { return $true }
        } catch {} finally { $c.Close() }
        Start-Sleep -Seconds $PollSec
    }
    return $false
}

function Test-OllamaHealth {
    try {
        Invoke-RestMethod -Uri "$Script:OllamaBase/api/tags" -TimeoutSec 5 -ErrorAction Stop | Out-Null
        return $true
    } catch { return $false }
}

function Wait-ForOllama {
    param(
        [int]$Retries    = $Script:OllamaHealthRetries,
        [int]$BackoffSec = $Script:OllamaHealthBackoffSec
    )
    for ($i = 0; $i -le $Retries; $i++) {
        if (Test-OllamaHealth) { return $true }
        if ($i -lt $Retries) { Start-Sleep -Seconds $BackoffSec }
    }
    return $false
}

function Start-Ollama {
    if (Test-OllamaHealth) { return $true }
    if (-not (Test-Path $Script:OllamaExe)) { return $false }
    try {
        Start-Process -FilePath $Script:OllamaExe -ArgumentList 'serve' -WindowStyle Hidden | Out-Null
    } catch { return $false }
    return Wait-ForOllama
}

# ---------- Yield-to-operator gate ----------
# Native probes: GetLastInputInfo for keyboard/mouse recency, and the Core Audio
# session API for "is anything audibly playing". Compiled once per process.
$Script:BusyProbeSrc = @'
using System; using System.Collections.Generic; using System.Runtime.InteropServices; using System.Threading;
namespace QflixRea {
  [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorCo {}
  [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDeviceEnumerator { [PreserveSig] int EnumAudioEndpoints(int flow, int stateMask, out IMMDeviceCollection c); }
  [Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDeviceCollection { [PreserveSig] int GetCount(out int n); [PreserveSig] int Item(int i, out IMMDevice d); }
  [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDevice { [PreserveSig] int Activate(ref Guid iid, int ctx, IntPtr p, [MarshalAs(UnmanagedType.IUnknown)] out object o); }
  [Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioSessionManager2 { int Unused1(); int Unused2(); [PreserveSig] int GetSessionEnumerator(out IAudioSessionEnumerator e); }
  [Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioSessionEnumerator { [PreserveSig] int GetCount(out int n); [PreserveSig] int GetSession(int i, out IAudioSessionControl2 s); }
  // IAudioSessionControl (9 methods) then IAudioSessionControl2; only GetState
  // and GetProcessId are called, the slots between are vtable padding.
  [Guid("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioSessionControl2 { [PreserveSig] int GetState(out int st);
    int P1(); int P2(); int P3(); int P4(); int P5(); int P6(); int P7(); int P8(); int P9(); int P10();
    [PreserveSig] int GetProcessId(out uint pid); }
  [Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioMeterInformation { [PreserveSig] int GetPeakValue(out float p); }

  public static class BusyProbe {
    [StructLayout(LayoutKind.Sequential)] struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
    [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO lii);

    public static double InputIdleMinutes() {
      var lii = new LASTINPUTINFO(); lii.cbSize = (uint)Marshal.SizeOf(lii);
      if (!GetLastInputInfo(ref lii)) throw new InvalidOperationException("GetLastInputInfo failed");
      uint idleMs = unchecked((uint)Environment.TickCount - lii.dwTime);  // wraps safely at 49.7 days
      return idleMs / 60000.0;
    }

    // Process names owning an ACTIVE render session whose peak meter rose above
    // threshold during the sampling window. ACTIVE alone is not enough: many
    // apps hold a silent stream open indefinitely.
    public static string[] AudibleProcesses(int samples, int intervalMs, float threshold) {
      var en = (IMMDeviceEnumerator)(new MMDeviceEnumeratorCo());
      IMMDeviceCollection col; Marshal.ThrowExceptionForHR(en.EnumAudioEndpoints(0, 1, out col));  // eRender, ACTIVE
      int nDev; col.GetCount(out nDev);
      var meters = new List<IAudioMeterInformation>(); var pids = new List<uint>();
      for (int d = 0; d < nDev; d++) {
        IMMDevice dev; if (col.Item(d, out dev) != 0) continue;
        Guid g = typeof(IAudioSessionManager2).GUID; object o;
        if (dev.Activate(ref g, 23, IntPtr.Zero, out o) != 0) continue;  // CLSCTX_ALL
        IAudioSessionEnumerator se; if (((IAudioSessionManager2)o).GetSessionEnumerator(out se) != 0) continue;
        int nSess; se.GetCount(out nSess);
        for (int i = 0; i < nSess; i++) {
          IAudioSessionControl2 s; if (se.GetSession(i, out s) != 0) continue;
          int st; s.GetState(out st); if (st != 1) continue;  // AudioSessionStateActive
          uint pid; s.GetProcessId(out pid);
          meters.Add((IAudioMeterInformation)s); pids.Add(pid);
        }
      }
      var peak = new float[meters.Count];
      for (int t = 0; t < samples; t++) {
        for (int i = 0; i < meters.Count; i++) { float p; if (meters[i].GetPeakValue(out p) == 0 && p > peak[i]) peak[i] = p; }
        Thread.Sleep(intervalMs);
      }
      var names = new List<string>();
      for (int i = 0; i < meters.Count; i++) {
        if (peak[i] <= threshold) continue;
        string n; try { n = System.Diagnostics.Process.GetProcessById((int)pids[i]).ProcessName; } catch { n = "pid" + pids[i]; }
        if (!names.Contains(n)) names.Add(n);
      }
      return names.ToArray();
    }
  }
}
'@

function Initialize-BusyProbe {
    if (-not ('QflixRea.BusyProbe' -as [type])) { Add-Type -TypeDefinition $Script:BusyProbeSrc }
}

function Get-InputIdleMinutes {
    Initialize-BusyProbe
    return [QflixRea.BusyProbe]::InputIdleMinutes()
}

function Get-AudibleApps {
    # 3 s window: a 1.5 s one was measured missing a playing video during a
    # quiet passage (1 miss in 5 live samples, 2026-09-13).
    param([int]$Samples = 30, [int]$IntervalMs = 100, [double]$Threshold = 0.001)
    Initialize-BusyProbe
    # Comma-wrap so a single-element result stays an array through the pipeline.
    return ,@([QflixRea.BusyProbe]::AudibleProcesses($Samples, $IntervalMs, [float]$Threshold))
}

function Get-BusyVerdict {
    # PURE: probe readings in, one space-free audit token out ('' = not busy).
    param([double]$InputIdleMinutes, [string[]]$AudibleApps)
    if ($InputIdleMinutes -lt $Script:BusyInputMinutes) {
        return "reason=input,idle_min=$([math]::Floor($InputIdleMinutes))<$($Script:BusyInputMinutes)"
    }
    $media = @($AudibleApps | Where-Object { $_ -and $_ -notmatch $Script:BusyAudioIgnoreRx })
    if ($media.Count -gt 0) { return "reason=audio,apps=$($media -join '+')" }
    return ''
}

function Get-UserBusyReason {
    # Fails OPEN: a probe that cannot answer reads as "not busy" and is logged.
    # A broken gate must never be the reason REA goes dark (2026-07-29 class).
    try {
        $idle = Get-InputIdleMinutes
        # Skip the ~1.5 s audio sample when input already decides the verdict.
        if ($idle -lt $Script:BusyInputMinutes) { return (Get-BusyVerdict -InputIdleMinutes $idle -AudibleApps @()) }
        return (Get-BusyVerdict -InputIdleMinutes $idle -AudibleApps (Get-AudibleApps))
    } catch {
        Write-AuditLog "busy-probe FAILED err=$(($_.Exception.Message -replace '[\r\n]+',' '))"
        return ''
    }
}

# ---------- Lock + audit log ----------
function Acquire-Lock {
    $p = Join-Path (Get-StateDir) 'state.json.lock'
    try {
        $fs = [System.IO.File]::Open($p, 'OpenOrCreate', 'ReadWrite', 'None')
        return $fs
    } catch { return $null }
}

function Write-AuditLog {
    param([string]$Line)
    $p = Join-Path (Get-StateDir) 'audit.log'
    if ((Test-Path $p) -and ((Get-Item -LiteralPath $p).Length -ge $Script:AuditLogMaxBytes)) {
        $rot = "$p.1"
        if (Test-Path $rot) { Remove-Item -Force -LiteralPath $rot }
        Move-Item -Force -LiteralPath $p -Destination $rot
    }
    $stamp = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
    Add-Content -LiteralPath $p -Value "$stamp $Line" -Encoding UTF8
}

function Get-LastTerminalAuditLine {
    # WRITER HALF of scripts/canaries/rea-liveness.sh (the box-side dead-man for
    # this script). Returns the most recent TERMINAL verdict line from audit.log,
    # verbatim and unparsed - that string IS the canary's contract content. All
    # judgement of it happens on the box, in versioned code with tests; REA's job
    # is to report a FACT ("here is my last terminal line"), never a verdict
    # ("I am healthy"). That separation is why the alarm does not depend on REA
    # being well enough to diagnose itself.
    #
    # "Terminal" excludes the `suppressed n=` line, which Write-AuditLog emits
    # mid-run immediately BEFORE the run's real verdict. A naive `tail -1` is a
    # KNOWN-WRONG implementation of this contract - the canary has a dedicated
    # branch for receiving that line, labelled
    # "writer-wrote-suppression-line-not-terminal-line". Terminal shapes are
    # `ok findings=...`, `fail reason=...` and `SKIPPED ...`; the filter stays
    # minimal (one known non-terminal shape) precisely so new vocabulary reaches
    # the canary's closed table rather than being silently dropped here.
    #
    # Returns '' when there is nothing honest to report: no log yet, unreadable,
    # or a log holding only a suppression line. '' means WRITE NOTHING. A missing
    # heartbeat is exit 2 rea-heartbeat-absent - the truth, stated loudly -
    # whereas emitting a line REA never produced would be a fabricated verdict,
    # and a stale-but-present one would keep the age predicates green forever.
    $p = Join-Path (Get-StateDir) 'audit.log'
    if (-not (Test-Path -LiteralPath $p)) { return '' }
    try { $lines = @(Get-Content -LiteralPath $p -Encoding UTF8 -ErrorAction Stop) }
    catch { return '' }
    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        # Add-Content -Encoding UTF8 emits CRLF, and a BOM on file creation.
        # The canary strips CR itself, but it quotes this line back into a Kuma
        # message, so hand it something clean rather than something tolerated.
        $l = $lines[$i].TrimStart([char]0xFEFF).Trim()
        if (-not $l) { continue }
        if ((($l -split ' ', 2)[-1]) -like 'suppressed n=*') { continue }
        return $l
    }
    return ''
}

function Send-DeadmanAlert {
    # Fixed 2026-07-29: five early-return failure paths in Invoke-Main
    # (tunnel_timeout, no_models, ssh_fail, blob_parse, all_models_noop) used to
    # write only an audit-log line and return - REA runs on the workstation and
    # nothing on the seedbox can see its state, so those paths were a silent,
    # total blackout. Only ollama_down paged, via its own inline dedup. This is
    # the same deadman shape + same 24h-dedup-via-state approach, generalized so
    # each reason gets an INDEPENDENT dedup key (state.dead_ping_<reason>) - a
    # stuck tunnel paging today must not swallow a model outage paging tomorrow.
    #
    # $DryRun is the script's own [switch] param, read here the same way
    # Invoke-Main already reads it for the ollama_down branch (both are defined
    # in this same script's scope, so the unqualified reference resolves there).
    param(
        [string]$Reason,
        [string]$Title,
        [string]$Description,
        [string]$Webhook,
        [string]$OpId,
        [string]$Detail = ''   # optional free-text appended to the audit line (e.g. msg=... for ssh_fail)
    )
    $suffix = if ($Detail) { " $Detail" } else { '' }
    $state      = Read-State
    $key        = "dead_ping_$Reason"
    $shouldPing = $true
    if ($state[$key]) {
        try {
            $last = [datetime]::Parse($state[$key])
            if (((Get-Date) - $last).TotalHours -lt 24) { $shouldPing = $false }
        } catch {}
    }
    if (-not ($shouldPing -and $Webhook -and $OpId)) {
        Write-AuditLog "fail reason=$Reason outcome=silent$suffix"
        return
    }
    $payload = New-DiscordDeadmanPayload -OperatorId $OpId -Title $Title -Description $Description
    if ($DryRun) {
        ($payload | ConvertTo-Json -Depth 12) | Write-Host
        Write-AuditLog "fail reason=$Reason outcome=dryrun_deadman$suffix"
        return
    }
    if (Send-Discord -WebhookUrl $Webhook -Payload $payload) {
        $state[$key] = (Get-Date).ToString('o')
        Write-State $state
        Write-AuditLog "fail reason=$Reason outcome=deadman_post$suffix"
    } else {
        Write-AuditLog "fail reason=$Reason outcome=deadman_post_failed$suffix"
    }
}

# ---------- Prompt + secret helpers ----------
function Get-SystemPrompt {
@'
You are auditing a self-hosted media-server stack ("Manitoba" / "QFlix") running on an Ultra.cc shared seedbox. You receive a JSON blob containing log excerpts from fourteen sources. Find real errors a sysadmin should act on. Ignore noise (info-level chatter, expected periodic warnings, unrelated debug lines). IGNORE STALE HISTORY: the blob's `fetched_at` is NOW; any log line whose own timestamp is more than 3 days older than `fetched_at` is a resolved/historical incident (e.g. a decommissioned app's last error), NOT a current fault - do not report it. Lines beginning "# collector-suppressed:" or "===== <section> census:" are collector bookkeeping, not log content - never report them; they exist so you can see the section was filtered. Every log line is prefixed with its own [path] or [logfile], or preceded by a "===== path =====" header: COPY THAT VERBATIM into the `file` field. If a section supplies none, emit source:<section-name>. NEVER infer or invent a filesystem path. NON-ACTIONABLE NOISE you must NEVER report (external, cosmetic, or expected): Plex NAT-PMP / UPnP / port-mapping "not supported by gateway" messages; any "unexpected networking error", Cloudflare 5xx (52x), or nginx error-page HTML returned to an *arr from an EXTERNAL indexer/proxy (transient upstream, not our stack); indexer responses that merely echo a `"severity": "error"` JSON field; bare stack-trace CONTINUATION fragments ("End of inner exception stack trace", lines starting "at ") with no accompanying root error message; tdarr unhandled express-route errors ("worker2 ... is not a function", "Cannot read properties of undefined (reading 'includes')" from Tdarr_Server/srcug/api/servers.js), an upstream Tdarr bug that neither restarts nor degrades the server; and Plex client-abort stream writes ("Caught exception trying to stream file ...: write: protocol is shutdown (SSL routines)"), which mean a viewer closed or seeked mid-stream - the identical error fires on poster JPEGs, so it is never a media fault; the Tdarr Mediainfo probe's WASM out-of-memory failure ("WebAssembly.instantiate(): Out of memory: wasm memory", "Error running MediaInfo") is a KNOWN, PERMANENT fault on this memory-capped slot (ruled unfixable 2026-07-28: Tdarr's bundled Node rejects the only viable fix and crash-loops if forced) tracked by a dedicated canary, not by this audit - do NOT report it; and Plex "Failed to create parent iterator" lines are the expected trailing edge of qflix-reaper's 45-day retention deleting a whole series directory out from under a library scan; Plex "ClientProfileExtra: missing or invalid type parameter" lines, which PMS logs at ERROR level once or more per transcode-decision request and which never accompany a failed transcode - the stream still plays, so do NOT report them; Plex "Unable to find metadata agent provider for identifier" naming the PSEUDO-identifiers 'library' or 'iva', which are Plex resolving its own PlayQueue and Discover scopes and NOT a missing agent - the SAME message naming a real provider (com.plexapp.agents.* or tv.plex.agents.*) IS a genuine config break and you MUST still report that one; Sonarr/Radarr ParsingService "No matching series"/"No matching movie" Debug lines and the paired DownloadDecisionMaker "[Permanent] Unknown Series/Unknown Movie" rejections, which are BY DESIGN - the *arr pulls an indexer's full recent RSS feed and filters locally, so every release title outside this library logs one, and they are never severity=error (an IMPORT failure mentioning "no matching series folder on disk" or "no matching series files" is a different, real fault and IS reportable); any finding whose ONLY evidence is *arr Debug-level lines - an excerpt carrying a "|Debug|" NLog level token and no error-level token anywhere in it - because *arr Debug is per-release trace chatter and a real fault always carries an error-level token somewhere in the same excerpt; Plex CreditsDetectionManager marker-scan chatter, specifically "incomplete marker attributes", "BufferingLineReader: failed to read line" and "credits detection for item N has failed too many times", which are the intro/credits marker scanner retrying its own work and never affect playback - a CreditsDetectionManager "Job failed" or "Mis-matching media items" line is NOT covered by this and you SHOULD still report it; Plex "Unknown metadata type: folder" replies under a [Req#...] tag, which are a client asking the HTTP API about a folder-type item Plex has no metadata class for, i.e. a normal browse response rather than a fault; Bazarr "Error trying to get releases from Github" GitHub API rate-limit failures, which are the unauthenticated 60-requests-per-hour GitHub limit being consumed by the other tenants sharing this seedbox's IP - every Bazarr here runs with --no-update so the release list is display-only, the poll succeeds again as soon as the shared quota rolls over, and Bazarr's updater passes no auth header at all so there is nothing to configure (a 403 against any OTHER GitHub endpoint is NOT covered by this and you MUST still report it); Tdarr node "Binary test N: handbrakePath not working" lines, because HandBrakeCLI is absent and uninstallable on this slot (ruled 2026-07-28) and every Tdarr flow and health check runs ffmpeg only, so the daily binary self-test failure is permanent and cosmetic (a "Binary test N: ffmpegPath not working" line is a REAL fault and you MUST still report that one); Seerr/Jellyseerr Plex-scan "UNIQUE constraint failed: media.tvdbId" lines, because a Plex anthology item can carry multiple tmdb guids that resolve to one tvdbId - the media row already exists and the title stays available, an upstream Jellyseerr quirk with no local fix (any OTHER Seerr SQLITE_CONSTRAINT naming a different column or table is NOT covered and you MUST still report it); buildarr "Unsupported remote notification connection" warnings naming implementation PlexServer, which buildarr logs on every daily run because its radarr plugin cannot manage that connection type and skips it - radarr's own Plex notification keeps working untouched (the same message naming any OTHER implementation is a real sync gap and you MUST still report it); Plex "Network Service: Error in advertiser handle read: 125 (Operation canceled)" and the paired "Abandoning advertise socket" lines, which are PMS tearing down its announce socket as it SHUTS DOWN - exactly one appears in each rotated log, always as the last error and always immediately before "Killing process: Plex EAE Service" - so it marks a restart rather than a running fault (an advertiser error with a DIFFERENT cause, such as address-already-in-use or a refused connection, carries no cancellation token and you MUST still report it); Bazarr "SignalR client for Sonarr/Radarr connection as been lost. Trying to reconnect" lines, which are Bazarr losing its event subscription when the *arr it watches restarts - every observed loss is followed by a matching "is connected" within 20-35 seconds and they cluster inside the Monday maintenance window, so the reconnect is automatic. This suppression deliberately accepts blindness to a permanent loss: Bazarr falls back to its scheduled sync either way, and a genuinely wedged Bazarr shows OTHER errors which you MUST still report; external indexer unavailability relayed by Prowlarr - both its "Indexer's server is unavailable. Try again later" warning naming a third-party indexer and the 429 it then hands Sonarr/Radarr carrying "Indexer is disabled till <time> due to recent failures" - because the origin is someone else's server and the backoff clears itself within the hour (a bare 429.TooManyRequests with no such description is NOT covered and you MUST still report it); Prowlarr Cardigann "Request for <indexer> failed with status 5xx. Retrying in Ns" retry warnings, including the named-status equivalents (InternalServerError, BadGateway, ServiceUnavailable, GatewayTimeout), which are the per-attempt retry of a transient third-party 5xx and are followed by a successful query - Tokyo Toshokan logged 24-38 of these a day while still serving ~92 successful queries a day, so the retry line says nothing about indexer health on its own (the TERMINAL "Indexer's server is unavailable" line is a different string, already covered above, and only a SUSTAINED rise in THAT one is actionable); Plex "Webhook: Error delivering payload" failures against the orphaned Discord webhook, a hook created in Nov 2023 that is referenced nowhere in QFlix, has returned 400 on every notification and has NEVER returned 2xx in any rotated PMS log, because Plex posts a multipart payload field where Discord requires payload_json - it cannot be fixed from the box and is pending deletion at plex.tv (a delivery failure against any OTHER webhook target is NOT covered and you MUST still report it); *arr ServerSideNotificationService "Failed to retrieve notifications" cloud-news poll timeouts; *arr Discord notifier "Unable to post payload" delivery failures; Bazarr "unable to sync subtitles" single-file subsync failures; Prowlarr FlareSolverr "Proxy validation failed" one-off validation errors; Seerr "Failed to retrieve watchlist items" plex.tv 5xx responses; qflix-reaper "SUCCESS - N deleted, N GB reclaimed" summary lines, including N=0; *arr "Error occurred while executing task ApplicationCheckUpdate/ApplicationUpdateCheck/CheckHealth" scheduled-task failures; Plex CreditsDetectionManager "Job failed: Video does not exist" lines; Plex transcode "Error opening input(s): Server returned 404 Not Found" for a part the reaper already deleted; Sonarr/Radarr Torznab "rss sync didn't cover the period between X and Y" warnings; Tautulli "Tautulli WebSocket :: [Errno 111] Connection refused" PMS websocket drops; Plex "GET http://<gateway>/discover.json ... SSL peer certificate ... was not OK" tuner-discovery probes; manitoba-maint "lib.notify: alert sent:" lines, which are the alerting system CONFIRMING it already told the operator; Plex "Failed to get a decision for: <path>" errors and the paired "MDE: video has neither a video stream nor an audio stream" / "MDE: no compatible media decisions are available" lines; Plex "downloadContainer: expected MediaContainer element, found html" errors; Plex CreditsDetectionManager "Job failed: Failed to generate any thumbnails" lines; Plex "Error iterating EAE watchfolder directory: No such file or directory" transcoder lines; Plex CreditsDetectionManager "Job failed: Scanner job failed" lines; SABnzbd INFO "Sending notification: SABnzbd - Queue finished" lines; Plex "Exception caught determining whether we could skip '<Show>/Season N' ~ Null value not allowed for this type" scanner lines. CONVERSELY, disk-quota and write failures ("Unknown system error -122", EDQUOT, ENOSPC, "Disk quota exceeded") ARE real and you MUST still report them if present. Only report an error that shows a service is genuinely DOWN or crash-looping, a scheduled job that cannot run at all, actual data loss, an auth/credential failure, or a config break a human must act on.

Report AT MOST 10 findings: if more exist, keep the 10 most severe and drop the rest - a truncated answer helps nobody. Return ONLY a JSON array of findings. No prose, no markdown fences. Empty array [] means clean.

Each finding MUST have exact keys:
- time: ISO-8601 COPIED FROM THE LOG LINE ITSELF. If the line carries no timestamp of its own, emit the empty string "" - NEVER substitute fetched_at and never guess a time. A guessed time reads as fresh evidence and there is no way to tell it from a real one.
- app: short slug (sonarr, buildarr, nginx, plex, cron, ...)
- file: the [path] prefix or "===== path =====" header GOVERNING the line you quoted, copied verbatim; emit "" if you cannot see one. This field is checked against what the collector actually shipped and is replaced when it does not match, so guessing a path gains you nothing.
- severity: warning | error | critical
- summary: one-line human description
- excerpt: <=300 chars of the offending log
- signature: short stable string for dedupe (e.g. "buildarr:pydantic-validation-error")
'@
}

function Build-UserPrompt {
    param([string]$BlobJson)
    return "LOG BLOB:`n$BlobJson`n`nReturn a JSON array of findings now. Output nothing else."
}

function Read-Secret {
    param([string]$RepoRoot, [string]$Name)
    $p = Join-Path $RepoRoot "secrets/$Name"
    if (-not (Test-Path $p)) { return $null }
    return (Get-Content -Raw -LiteralPath $p).Trim()
}

function Get-FieldOrEmpty {
    # Safe property access for model-emitted pscustomobjects under StrictMode 2.
    # ARRAY VALUES JOIN WITH NEWLINE, not the [string] cast's implicit space:
    # a model emitting "excerpt" as a JSON array of lines would otherwise be
    # flattened to ONE newline-free line, silently defeating every per-line
    # ((?m)^ without (?s)) rule constraint (proven 2026-08-14: an array-shaped
    # excerpt let a benign updater element borrow a rate-limit token from a
    # real 429 element and suppressed the finding).
    param($Obj, [string]$Name)
    if ($null -eq $Obj) { return '' }
    $v = $null
    if ($Obj -is [hashtable]) {
        if (-not $Obj.ContainsKey($Name)) { return '' }
        $v = $Obj[$Name]
    } elseif ($Obj.PSObject.Properties.Name -contains $Name) {
        $v = $Obj.$Name
    } else {
        return ''
    }
    if ($v -is [System.Collections.IEnumerable] -and $v -isnot [string]) {
        return (@($v) | ForEach-Object { [string]$_ }) -join "`n"
    }
    return [string]$v
}

function Get-CollectorPathIndex {
    <#
      .SYNOPSIS
      Every file token the COLLECTOR emitted, plus the owning token of every
      line it shipped.
      .DESCRIPTION
      Before 2026-08-25 `file` was 100% model-authored: Invoke-Main copied the
      model's string straight into the Discord embed and nothing in between ever
      compared it to a collector source. On 2026-08-25 that shipped a page whose
      excerpt came from node.err and whose `file` said Tdarr_Server_Log.txt -
      the last header-shaped token in the window, and a TRUNCATED one at that.
      The model was not hallucinating; it was doing a positional
      header-scope-tracking job that the collector should never have delegated.

      This index is the collector's own answer to that question. `tokens` is the
      allowlist of path strings the collector actually emitted; `byBase` maps a
      basename back to the full path; `lines` records, for every shipped line,
      which token governs it (its own [path] prefix if it has one, else the
      nearest preceding "===== path =====" header, else source:<section>).

      Only path-SHAPED bracket tokens are admitted, because log lines are full
      of brackets that are not provenance ("[2026-08-23T...]", "[ERROR]",
      "[Req#123]"). A token qualifies if it is absolute or ends .log/.txt/.err -
      which covers both emitted forms, arr_logs' full path and plex_errors'
      bare basename.
    #>
    param($DecodedBlob)
    $idx = @{ tokens = @{}; byBase = @{}; lines = @(); sections = @() }
    if ($null -eq $DecodedBlob) { return $idx }
    if (-not ($DecodedBlob.PSObject.Properties.Name -contains 'sources')) { return $idx }
    if ($null -eq $DecodedBlob.sources) { return $idx }
    foreach ($p in $DecodedBlob.sources.PSObject.Properties) {
        $name = [string]$p.Name
        $idx.sections += $name
        $idx.tokens["source:$name"] = $true
        $current = "source:$name"
        foreach ($ln in ([string]$p.Value -split "`r?`n")) {
            if (-not $ln) { continue }
            if ($ln -match '^=====\s+(\S+)') {
                $t = $Matches[1]
                # The plex_errors banner opens "===== plex_errors: dir=..." -
                # a section label, not a path. Path-shaped only.
                if ($t -match '/') {
                    $current = $t
                    $idx.tokens[$t] = $true
                    $b = $t.Substring($t.LastIndexOf('/') + 1)
                    if ($b) { $idx.byBase[$b] = $t }
                }
                continue
            }
            $owner = $current
            if ($ln -match '^\[([^\]]+)\]\s') {
                $t = $Matches[1]
                if ($t.StartsWith('/') -or $t -match '\.(log|txt|err)$') {
                    $owner = $t
                    $idx.tokens[$t] = $true
                    $b = $t.Substring($t.LastIndexOf('/') + 1)
                    if ($b) { $idx.byBase[$b] = $t }
                }
            }
            $idx.lines += @{ text = $ln; owner = $owner }
        }
    }
    return $idx
}

function Resolve-FindingFile {
    <#
      .SYNOPSIS
      The path rendered in a finding, taken from COLLECTOR metadata rather than
      from the model. Never returns a string the collector did not emit.
      .DESCRIPTION
      Three steps, strongest evidence first:
        1. ANCHOR ON THE EXCERPT. If the quoted text is a line the collector
           shipped, that line's own owner is the answer - provenance, not
           inference. This is the step that fixes the 2026-08-25 page: the
           EACCES excerpt was a real node.err line, so it resolves to node.err
           however many other path tokens sat next to it in the window.
        2. RATIFY THE MODEL'S STRING, only if the collector emitted it (exact,
           then by basename). A hint that survives the allowlist is still a
           collector token.
        3. 'unattributed'. An excerpt matching NO shipped line was paraphrased,
           mangled or invented; saying so is honest and is counted in the audit
           log. The finding is KEPT - this resolver fixes labels, it does not
           get to eat findings.
    #>
    param([string]$ModelFile, [string]$Excerpt, $Index)
    if ($null -eq $Index) { return 'unattributed' }

    # 1. Excerpt anchor. Probes are excerpt lines with any leading [..] prefix
    #    stripped (models re-quote the prefix inconsistently) and capped at 60
    #    chars, so a model that truncated the tail of a long line still matches
    #    its head. 24 chars is the floor: shorter fragments collide.
    foreach ($e in ([string]$Excerpt -split "`r?`n")) {
        $probe = $e.Trim()
        if ($probe -match '^\[[^\]]+\]\s+(.*)$') { $probe = $Matches[1].Trim() }
        if ($probe.Length -lt 24) { continue }
        if ($probe.Length -gt 60) { $probe = $probe.Substring(0, 60) }
        foreach ($l in $Index.lines) {
            if (([string]$l.text).IndexOf($probe, [StringComparison]::Ordinal) -ge 0) {
                return [string]$l.owner
            }
        }
    }

    # 2. Ratify the model's own string against the allowlist.
    $mf = ([string]$ModelFile).Trim()
    if ($mf) {
        if ($Index.tokens.ContainsKey($mf)) { return $mf }
        $b = $mf.Substring([Math]::Max($mf.LastIndexOf('/'), $mf.LastIndexOf('\')) + 1)
        if ($b -and $Index.byBase.ContainsKey($b)) { return [string]$Index.byBase[$b] }
    }

    # 3. Not shipped by any collector.
    return 'unattributed'
}

function Test-IsNoiseFinding {
    # Returns the matching rule id when a model-emitted finding is a known-benign
    # class, else $null. Matches against signature + summary + excerpt together:
    # models put the tell-tale phrasing in different fields (the 2026-07-28 Plex
    # false positive carried "ssl-protocol-shutdown" in the signature and the real
    # log text only in the excerpt), so any single field would miss.
    #
    # A rule may set an optional `field` key (added 2026-07-29 for
    # 'bare-stack-continuation') to test ONLY that one field instead of the
    # combined haystack - needed when a model's own prose summary would
    # otherwise poison a negative-lookahead check (summary almost always
    # contains the word "error"/"exception" regardless of what the raw excerpt
    # actually shows). Rules without `field` keep matching the combined hay,
    # unchanged from before.
    param($Finding)
    $sigVal = Get-FieldOrEmpty $Finding 'signature'
    $sumVal = Get-FieldOrEmpty $Finding 'summary'
    $excVal = Get-FieldOrEmpty $Finding 'excerpt'
    $hay = @($sigVal, $sumVal, $excVal) -join ' '
    if (-not $hay.Trim()) { return $null }
    foreach ($rule in $Script:NoiseFindingRules) {
        $target = $hay
        if ($rule.ContainsKey('field') -and $rule.field) {
            $target = switch ($rule.field) {
                'signature' { $sigVal }
                'summary'   { $sumVal }
                'excerpt'   { $excVal }
                default     { $hay }
            }
        }
        if ($target -match $rule.rx) {
            # 2026-09-14 (adversarial review of PR #23): an `excerpt` rule is
            # a claim about ONE log line, but models often quote the whole
            # surrounding block. A bundle of [noise line + a real fault] used
            # to be dropped wholesale because the noise phrase was somewhere
            # in the blob. Now a multi-line excerpt is noise only if EVERY
            # line is claimed by some rule; one unclaimed line and the finding
            # pages. Fail open, by construction.
            if ($rule.ContainsKey('field') -and $rule.field -eq 'excerpt') {
                $lines = @(($excVal -split "`r?`n") | Where-Object { $_.Trim() })
                if ($lines.Count -gt 1) {
                    foreach ($ln in $lines) {
                        $claimed = $false
                        foreach ($r2 in $Script:NoiseFindingRules) {
                            if ($ln -match $r2.rx) { $claimed = $true; break }
                        }
                        if (-not $claimed) { return $null }
                    }
                }
            }
            return [string]$rule.id
        }
    }
    return $null
}

function Test-IsStaleFinding {
    # Enforces line-level staleness as CODE, not prompt text. The system prompt
    # ASKS models to drop any log line more than FRESH_DAYS older than the blob's
    # `fetched_at`, but this is advisory only and models demonstrably do not
    # comply (e.g. the 2026-07-19 "Failed to create parent iterator" Fargo-reap
    # chatter still rode along on 2026-07-29, ten days past the model's own
    # 3-day rule). File-level freshness (tailfresh in the heredoc) filters whole
    # FROZEN files, but a file can be fresh (touched today) while still
    # containing old lines inside it (e.g. the newsletter .err file, touched
    # every Monday, carrying 5-week-old Gemini lines) - this is the per-LINE
    # backstop that catches those.
    #
    # Fails OPEN: a finding whose `time` is missing or unparseable is KEPT, not
    # dropped - this suppressor must never get to eat a finding it cannot
    # confidently date. A `time` in the FUTURE relative to FetchedAt is also
    # kept (clock skew between the model and this box), not treated as fresh
    # evidence of staleness.
    param($Finding, [string]$FetchedAt)
    $timeStr = (Get-FieldOrEmpty $Finding 'time').Trim()
    if (-not $timeStr)  { return $false }
    if (-not $FetchedAt) { return $false }
    $styles = [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor `
              [System.Globalization.DateTimeStyles]::AssumeUniversal
    try {
        $t   = [datetime]::Parse($timeStr,  [System.Globalization.CultureInfo]::InvariantCulture, $styles)
        $ref = [datetime]::Parse($FetchedAt, [System.Globalization.CultureInfo]::InvariantCulture, $styles)
    } catch {
        return $false
    }
    if ($t -gt $ref) { return $false }
    return (($ref - $t).TotalDays -gt $Script:FreshDays)
}

# ---------- Main orchestrator ----------
function Invoke-Main {
    $startUtc = Get-Date

    # Phase 0a: yield to the operator (see $Script:BusyInputMinutes). Runs
    # before anything heavy. The SKIPPED line is a known shape to the box-side
    # rea-liveness canary: vacuous, cleared by the next real verdict, and only
    # pages if no real verdict lands for MAX_VACUOUS_H (7 d) - overnight runs
    # clear it long before that.
    $yieldToUser = -not ($Once -or $DryRun -or $FixturePath)
    if ($yieldToUser) {
        $busy = Get-UserBusyReason
        if ($busy) { Write-AuditLog "SKIPPED user_active $busy"; return 0 }
    }

    # Keep the two DERIVED policy surfaces in this file following the tracked
    # yaml, with no human in the loop. Repairs the generated table mirror and
    # appends any never-report clause the prompt is missing. NON-FATAL by
    # design: a sync failure must never be the reason REA goes dark - that is
    # the 2026-07-29 defect class this whole subsystem exists to prevent.
    try {
        $sync = Sync-ReaNoiseMirror
        if ($sync.changed) {
            Write-AuditLog "policy-sync changed=1 table=$([int]$sync.table_synced) clauses=$($sync.clauses_added -join ',')"
        } elseif ($sync.reason -ne 'ok') {
            Write-AuditLog "policy-sync changed=0 reason=$($sync.reason)"
        }
    } catch {
        Write-AuditLog "policy-sync FAILED err=$($_.Exception.Message)"
    }
    $stateDir = Get-StateDir
    $lock = Acquire-Lock
    if (-not $lock) { Write-AuditLog 'SKIPPED locked'; return 0 }

    try {
        $repoRoot = Get-RepoRoot
        $webhook  = Read-Secret -RepoRoot $repoRoot -Name 'discord-webhook.url'
        $opId     = Read-Secret -RepoRoot $repoRoot -Name 'discord-operator.id'

        # Phase 0b: tunnel readiness
        if (-not $Once) {
            if (-not (Wait-ForTunnel)) {
                Send-DeadmanAlert -Reason 'tunnel_timeout' -Webhook $webhook -OpId $opId `
                    -Title '[WARN] QFlix REA - SSH tunnel never came up' `
                    -Description "The reverse SSH tunnel to the seedbox did not open port $($Script:TunnelProbePort) within $($Script:TunnelWaitSec)s. Audit skipped.`n`nNext check: next Windows logon. Manual fix: verify the tunnel service is running and SSH connectivity to the seedbox."
                return 0
            }
        }

        # Phase 0c: Ollama health (self-start + dead-man) — launch serve if absent, then retry
        if (-not (Start-Ollama)) {
            $state = Read-State
            $shouldPing = $true
            if ($state.last_ollama_dead_ping) {
                try {
                    $last = [datetime]::Parse($state.last_ollama_dead_ping)
                    if (((Get-Date) - $last).TotalHours -lt 24) { $shouldPing = $false }
                } catch {}
            }
            if ($shouldPing -and $webhook -and $opId) {
                $payload = New-DiscordDeadmanPayload -OperatorId $opId
                if ($DryRun) {
                    ($payload | ConvertTo-Json -Depth 12) | Write-Host
                    Write-AuditLog 'fail reason=ollama_down outcome=dryrun_deadman'
                } else {
                    if (Send-Discord -WebhookUrl $webhook -Payload $payload) {
                        $state.last_ollama_dead_ping = (Get-Date).ToString('o')
                        Write-State $state
                        Write-AuditLog 'fail reason=ollama_down outcome=deadman_post'
                    } else {
                        Write-AuditLog 'fail reason=ollama_down outcome=deadman_post_failed'
                    }
                }
            } else {
                Write-AuditLog 'fail reason=ollama_down outcome=silent'
            }
            return 0
        }

        # Phase 0d: model discovery
        $models = @(Get-CodeModels)
        if ($models.Count -eq 0) {
            Send-DeadmanAlert -Reason 'no_models' -Webhook $webhook -OpId $opId `
                -Title '[WARN] QFlix REA - no code-capable Ollama models found' `
                -Description "``ollama list`` returned no model matching the include/exclude filters (coder / qwen3, minus -base/-vl/embed). Audit skipped.`n`nNext check: next Windows logon. Manual fix: ``ollama pull`` a coder model, or check the Ollama install."
            return 0
        }

        if (-not $webhook -or -not $opId) {
            Write-AuditLog 'fail reason=no_secrets'
            return 0
        }

        # Phase 1: fetch
        $blobPath = Join-Path $stateDir 'last-fetch.log'
        try {
            if ($FixturePath) {
                Copy-Item -LiteralPath $FixturePath -Destination $blobPath -Force
                $blobJson = Get-Content -Raw -LiteralPath $blobPath
            } else {
                $blobJson = Invoke-RemoteFetch -OutPath $blobPath
            }
        } catch {
            $msg = ($_.Exception.Message -replace '[\r\n]+',' ')
            if ($msg.Length -gt 200) { $msg = $msg.Substring(0,200) }
            Send-DeadmanAlert -Reason 'ssh_fail' -Webhook $webhook -OpId $opId -Detail "msg=$msg" `
                -Title '[WARN] QFlix REA - SSH fetch from the seedbox failed' `
                -Description "The remote log fetch over SSH failed: ``$msg``. Audit skipped.`n`nNext check: next Windows logon. Manual fix: check SSH connectivity / seedbox load."
            return 0
        }

        # Decode the base64 sections into readable logs. This ALSO validates the
        # blob parses (fail reason=blob_parse on error). CRITICAL: the decoded
        # object is what the models must see - passing the raw base64 $blobJson
        # (as this did before 2026-07-25) fed every model gibberish, so they
        # no-op'd; the larger 14-source blob then filled the whole 16K context
        # and guaranteed all_models_noop. Decode once, here, and feed that.
        try {
            $decodedBlob = ConvertFrom-FetchedBlob -Json $blobJson
        } catch {
            Send-DeadmanAlert -Reason 'blob_parse' -Webhook $webhook -OpId $opId `
                -Title '[WARN] QFlix REA - could not parse the seedbox log blob' `
                -Description "The JSON blob fetched from the seedbox failed to parse. Audit skipped.`n`nNext check: next Windows logon. Manual fix: run with -FixturePath or -DryRun to inspect the raw fetch."
            return 0
        }

        # A section that declared no freshness basis withholds its content and
        # says so. NAMED, never silent - the whole point of the declaration.
        foreach ($sp in $decodedBlob.sources.PSObject.Properties) {
            if (([string]$sp.Value) -match '(?m)^# collector-error:\s*(.+)$') {
                Write-AuditLog "collector-error section=$($sp.Name) msg=$($Matches[1])"
            }
        }

        # Phase 2: model loop
        $sys  = Get-SystemPrompt
        $user = Build-UserPrompt -BlobJson ($decodedBlob | ConvertTo-Json -Depth 6 -Compress)
        # Built ONCE per run, from the bytes the models are about to be handed:
        # this is the collector's own record of which file every shipped line
        # came from, and it is the only thing allowed to name a path in a
        # rendered finding.
        $pathIndex     = Get-CollectorPathIndex -DecodedBlob $decodedBlob
        $unprovenanced = 0
        $allFindings = @()
        $suppressed  = @()
        $okModels    = 0
        $modelIdx    = 0
        foreach ($m in $models) {
            # Re-check between models: the operator coming back mid-run abandons
            # the WHOLE run rather than grading a partial one - a truncated model
            # set would silently change the >=2 consensus floor's meaning.
            if ($yieldToUser -and $modelIdx -gt 0) {
                $busy = Get-UserBusyReason
                if ($busy) { Write-AuditLog "SKIPPED user_active phase=models done=$modelIdx/$($models.Count) $busy"; return 0 }
            }
            $modelIdx++
            $resp = Invoke-Model -Model $m -Prompt $user -SystemPrompt $sys
            if (-not $resp) { continue }
            $arr = Extract-JsonArray $resp
            if ($null -eq $arr) { continue }
            $okModels++
            # COUNTED, never silent (council 2026-08-18): suppression has an
            # audit line, so the two lossy paths on this side get one too.
            if ($Script:LastExtractSalvaged) {
                Write-AuditLog "salvaged model=$m kept=$(@($arr).Count) (truncated output, complete elements recovered)"
            }
            # The 10-finding cap was PROMPT TEXT ONLY - enforced nowhere. A
            # model that ignores it re-creates the num_predict truncation the
            # cap exists to prevent. Enforce here and say what was dropped.
            if (@($arr).Count -gt 10) {
                Write-AuditLog "overflow model=$m findings=$(@($arr).Count) kept=10"
                $arr = @($arr | Select-Object -First 10)
            }
            foreach ($f in @($arr)) {
                if (-not $f) { continue }
                $h = @{
                    time      = Get-FieldOrEmpty $f 'time'
                    app       = Get-FieldOrEmpty $f 'app'
                    file      = Get-FieldOrEmpty $f 'file'
                    severity  = (Get-FieldOrEmpty $f 'severity').ToLowerInvariant()
                    summary   = Get-FieldOrEmpty $f 'summary'
                    excerpt   = Get-FieldOrEmpty $f 'excerpt'
                    signature = Get-FieldOrEmpty $f 'signature'
                    _model    = $m
                }
                # PROVENANCE, not trust. The path that reaches Discord comes
                # from the collector index or reads 'unattributed'; the model's
                # string is at most a hint that has to survive the allowlist.
                $h.file = Resolve-FindingFile -ModelFile $h.file -Excerpt $h.excerpt -Index $pathIndex
                if ($h.file -eq 'unattributed') { $unprovenanced++ }
                # Enforcement, not advice: drop known-benign classes here so a
                # single over-eager model cannot page the operator on its own.
                $noiseId = Test-IsNoiseFinding $h
                if ($noiseId) { $suppressed += "$noiseId"; continue }
                # Second enforcement pass: drop findings whose OWN line timestamp
                # is stale, regardless of the (advisory-only) prompt instruction.
                if (Test-IsStaleFinding -Finding $h -FetchedAt $decodedBlob.fetched_at) {
                    $suppressed += 'stale-line'; continue
                }
                # Third enforcement pass: a connection failure against a managed
                # stack app is owned by that app's monitor, green or red alike.
                # See the ownership gate above for why both directions are a
                # duplicate and what residual that deliberately accepts.
                $ownedBy = Test-IsOwnedByAMonitor $h
                if ($ownedBy) { $suppressed += $ownedBy; continue }
                $allFindings += $h
            }
        }
        if ($okModels -eq 0) {
            Send-DeadmanAlert -Reason 'all_models_noop' -Webhook $webhook -OpId $opId -Detail "models=$($models.Count)" `
                -Title '[WARN] QFlix REA - every model no-op''d' `
                -Description "All $($models.Count) model(s) either timed out, errored, or returned unparseable output. Audit skipped this run.`n`nNext check: next Windows logon. Manual fix: check Ollama load / model health."
            return 0
        }
        if ($suppressed.Count -gt 0) {
            # Observable, never silent - a rule that starts eating real findings
            # shows up here before anyone has to guess why REA went quiet.
            $ruleList = (($suppressed | Sort-Object -Unique) -join ',')
            Write-AuditLog "suppressed n=$($suppressed.Count) rules=$ruleList"
        }
        if ($unprovenanced -gt 0) {
            # A finding whose excerpt matches no line the collector shipped.
            # Counted, not dropped (yet): this is the measurement that says
            # whether promoting it to a drop would eat real findings.
            Write-AuditLog "unprovenanced n=$unprovenanced (excerpt matched no collector-shipped line)"
        }

        # Phase 3: consensus + report
        $groups      = @(Get-Consensus -Findings $allFindings)
        $errorGroups = @($groups | Where-Object { $_.severity -in @('error','critical') })
        # CONSENSUS FLOOR (operator directive 2026-08-26): a finding only ONE
        # model flagged does not ping. Every hallucinated page in the 2026-08
        # storm — the reaper SUCCESS line read as a failure, the 4-for-1
        # Discord multiplication, each stale one-off — was flagged by exactly
        # one model; nothing real in the recorded history was 1/3-only except
        # the 2026-07-28 find, and the cost asymmetry has flipped: hundreds of
        # false pings render the channel muted, which is worse than a solo
        # find waiting in the audit log. Solo groups are logged (never silent)
        # and counted on the daily heartbeat, so a persistent real fault shows
        # up in review even if no second model ever agrees.
        $soloGroups  = @($errorGroups | Where-Object { @($_.models_flagged).Count -lt 2 })
        $errorGroups = @($errorGroups | Where-Object { @($_.models_flagged).Count -ge 2 })
        if ($soloGroups.Count -gt 0) {
            $soloSigs = (($soloGroups | ForEach-Object { $_.signature }) -join ',')
            Write-AuditLog "solo-unpaged n=$($soloGroups.Count) sigs=$soloSigs"
        }
        $duration    = [int]((Get-Date) - $startUtc).TotalSeconds

        # CROSS-RUN PAGE FLOOR. Everything above this line decides what is TRUE;
        # this decides what is NEWS. A finding that already paged within
        # $Script:PageCooldownHours is still real and still logged - it just
        # does not ping the operator a second time. Muted groups are written to
        # the audit log by key, never silently dropped, so "why did REA go
        # quiet about X" is answerable from the same file that answers "what
        # pinged me at 16:09". Ledger writes happen here, AFTER the consensus
        # floor, so a solo finding that never had the votes to page cannot
        # burn its key and mute the run where a second model finally agrees.
        $pageSplit   = Select-DuePageGroups -Groups $errorGroups -Stamp:(-not $DryRun)
        $mutedGroups = @($pageSplit.Muted)
        $errorGroups = @($pageSplit.Due)
        if ($mutedGroups.Count -gt 0) {
            $mutedKeys = (($mutedGroups | ForEach-Object {
                $k = [string]$_.page_key
                if ($k.Length -gt 48) { $k = $k.Substring(0,48) }
                "$($_.signature)~$k" }) -join ',')
            Write-AuditLog "repeat-muted n=$($mutedGroups.Count) cooldown=${Script:PageCooldownHours}h keys=$mutedKeys"
        }

        if ($errorGroups.Count -gt 0) {
            # Durable record of WHAT paged, not just that something did. Until
            # 2026-08-26 the audit log held only findings=N for an error_post,
            # so "what pinged me at 16:09?" was unanswerable once the Discord
            # embed scrolled away — the operator asks exactly that question.
            $pagedSigs = (($errorGroups | ForEach-Object {
                "$($_.signature)[$(@($_.models_flagged).Count)m]" }) -join ',')
            Write-AuditLog "paged n=$($errorGroups.Count) sigs=$pagedSigs"
            $payload = New-DiscordErrorPayload -Groups $errorGroups -OperatorId $opId -ModelCount $models.Count -DurationSec $duration
            if ($DryRun) {
                ($payload | ConvertTo-Json -Depth 12) | Write-Host
                Write-AuditLog "ok findings=$($errorGroups.Count) models=$okModels/$($models.Count) duration=${duration}s outcome=dryrun_error"
            } else {
                $ok = Send-Discord -WebhookUrl $webhook -Payload $payload
                $outcome = if ($ok) { 'error_post' } else { 'discord_post_failed' }
                Write-AuditLog "ok findings=$($errorGroups.Count) models=$okModels/$($models.Count) duration=${duration}s outcome=$outcome"
            }
            return 0
        }

        # Quorum floor (council finding, arbiter-verified 2026-08-26): okModels
        # was compared to 0 exactly once (the all_models_noop deadman) and never
        # to 2 — so a ONE-model run, where the >=2 consensus floor structurally
        # holds every finding, exited as outcome=silent and read as a clean
        # audit. It is not a clean audit; it is a blind one. Record it as its
        # own outcome token so the box-side rea-liveness canary (which fails
        # CLOSED on unknown vocabulary — its branch ships in the same commit)
        # files it with the vacuous family: transient degradation is a WARN,
        # a PERSISTENT one accrues the no-verdict streak and pages via the
        # existing P5 blind-streak clock. No heartbeat is posted — a green
        # "clean" embed from a one-model run would be the lie this fixes.
        if ($okModels -lt 2) {
            Write-AuditLog "ok findings=0 models=$okModels/$($models.Count) duration=${duration}s outcome=quorum_degraded"
            return 0
        }

        # Clean run
        $state = Read-State
        $today = (Get-Date).ToString('yyyy-MM-dd')
        if ($state.last_heartbeat_date -ne $today) {
            $payload = New-DiscordHeartbeatPayload -ModelCount $okModels -SoloCount $soloGroups.Count -MutedCount $mutedGroups.Count
            if ($DryRun) {
                ($payload | ConvertTo-Json -Depth 12) | Write-Host
                Write-AuditLog "ok findings=0 models=$okModels/$($models.Count) duration=${duration}s outcome=dryrun_heartbeat"
            } else {
                $ok = Send-Discord -WebhookUrl $webhook -Payload $payload
                if ($ok) {
                    $state.last_heartbeat_date = $today
                    Write-State $state
                }
                $outcome = if ($ok) { 'heartbeat' } else { 'discord_post_failed' }
                Write-AuditLog "ok findings=0 models=$okModels/$($models.Count) duration=${duration}s outcome=$outcome"
            }
        } else {
            Write-AuditLog "ok findings=0 models=$okModels/$($models.Count) duration=${duration}s outcome=silent"
        }
        return 0
    } finally {
        try { $lock.Dispose() } catch {}
        $lockPath = Join-Path (Get-StateDir) 'state.json.lock'
        if (Test-Path $lockPath) { Remove-Item -Force -LiteralPath $lockPath -ErrorAction SilentlyContinue }
    }
}

# ---------- Task Scheduler integration ----------
function Get-HeadlessPsAction {
    <#
      .SYNOPSIS
      The command line for a scheduled task that runs a PowerShell script with
      NO console window, ever. Pure -- returns data, touches nothing.

      .DESCRIPTION
      An Interactive ("run only when the user is logged on") task that sets
      -Execute to powershell.exe gets a console window allocated by the OS
      BEFORE the process starts parsing -WindowStyle Hidden. The window is
      drawn, it steals keyboard focus, and only then is it hidden: a flash
      every single run, which lands mid-game / mid-stream / mid-recording.

      conhost.exe --headless attaches the child to a console that is never
      drawn, so there is nothing to flash and nothing to steal focus.

      The cost, and it is real: Task Scheduler's "Last Run Result" afterwards
      reports CONHOST's exit code (always 0), not the script's. This script's
      own audit log and the rea-liveness canary are the truth about whether a
      run succeeded -- never the Last Run Result column.
    #>
    param([Parameter(Mandatory)][string]$Arguments)
    # $env:WINDIR is empty off-Windows (the hosted CI runner), so fall back to a
    # literal rather than emitting a path that starts with a bare separator.
    $win = if ($env:WINDIR) { $env:WINDIR } else { 'C:\WINDOWS' }
    # Concatenated, NOT Join-Path: pwsh on the Linux CI runner throws "A drive
    # with the name 'C' does not exist" when Join-Path is handed a path with a
    # Windows drive qualifier. These are always Windows paths, so build them as
    # strings and let this stay a pure function everywhere.
    $ps  = "$win\System32\WindowsPowerShell\v1.0\powershell.exe"
    [pscustomobject]@{
        Execute  = "$win\System32\conhost.exe"
        Argument = ('--headless "{0}" {1}' -f $ps, $Arguments)
    }
}

function Get-ReaTaskDefinition {
    <#
      .SYNOPSIS
      The task's identity, command line and triggers as DATA, so a test can
      assert them without registering anything.

      .DESCRIPTION
      The three triggers are not decoration -- each one is load-bearing, and the
      previous installer registered only the first, so any -Install silently
      downgraded a system that audits hourly into one that audits at logon:

        Logon          -- catch up on whatever broke while the box was off.
        SessionLock    -- StateChange 7. The operator just walked away, which is
                          exactly when the yield-to-operator gate ($BusyInput /
                          audible-media) will let a GPU-pinning run proceed.
        Daily + PT1H   -- the actual cadence. Anchored 01:00, repeats hourly for
                          P1D, so it re-arms every day.
    #>
    param([Parameter(Mandatory)][string]$ScriptPath)
    $cmd = Get-HeadlessPsAction -Arguments ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $ScriptPath)
    @{
        TaskPath = $Script:TaskFolderPath
        TaskName = $Script:TaskName
        Execute  = $cmd.Execute
        Argument = $cmd.Argument
        # 2026-09-14: a 15m limit killed ~22m runs (3 models sequential, measured).
        ExecutionTimeLimitMinutes = 30
        RepeatMinutes = 60
        DailyAt = '01:00'
        SessionStateChange = 7   # TASK_SESSION_LOCK
    }
}

function Install-Task {
    $d = Get-ReaTaskDefinition -ScriptPath $PSCommandPath

    $action = New-ScheduledTaskAction -Execute $d.Execute -Argument $d.Argument

    $daily = New-ScheduledTaskTrigger -Daily -At $d.DailyAt
    $daily.Repetition = (New-ScheduledTaskTrigger -Once -At $d.DailyAt `
        -RepetitionInterval (New-TimeSpan -Minutes $d.RepeatMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
    $logon = New-ScheduledTaskTrigger -AtLogOn
    # No New-ScheduledTaskTrigger switch reaches a session-state-change trigger;
    # it only exists as a CIM class. -ClientOnly builds it without a server.
    $lock = New-CimInstance -ClassName MSFT_TaskSessionStateChangeTrigger `
        -Namespace 'Root/Microsoft/Windows/TaskScheduler' -ClientOnly `
        -Property @{
            Enabled     = $true
            StateChange = [uint32]$d.SessionStateChange
            UserId      = "$env:USERDOMAIN\$env:USERNAME"
        }

    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                    -LogonType Interactive -RunLevel Limited
    $settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
                    -RunOnlyIfNetworkAvailable `
                    -ExecutionTimeLimit (New-TimeSpan -Minutes $d.ExecutionTimeLimitMinutes)

    Remove-ReaTask -Folder $Script:LegacyTaskFolderPath   # sweep the drifted copy
    Register-ScheduledTask -TaskPath $d.TaskPath -TaskName $d.TaskName -Action $action `
        -Trigger @($logon, $lock, $daily) -Principal $principal -Settings $settings -Force | Out-Null

    Write-Host "Installed task: $($d.TaskPath)$($d.TaskName)" -ForegroundColor Green
}

function Remove-ReaTask {
    param([Parameter(Mandatory)][string]$Folder)
    $existing = Get-ScheduledTask -TaskPath $Folder -TaskName $Script:TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskPath $Folder -TaskName $Script:TaskName -Confirm:$false
        return $true
    }
    return $false
}

function Uninstall-Task {
    foreach ($folder in @($Script:TaskFolderPath, $Script:LegacyTaskFolderPath)) {
        if (Remove-ReaTask -Folder $folder) {
            Write-Host "Uninstalled task: $folder$($Script:TaskName)" -ForegroundColor Yellow
        }
    }
}

# ---------- Main entry guard ----------
if (-not (Get-Variable -Name 'DotSourceMode' -Scope Script -ErrorAction SilentlyContinue)) {
    if ($Install)   { Install-Task;   exit 0 }
    if ($Uninstall) { Uninstall-Task; exit 0 }
    exit (Invoke-Main)
}
