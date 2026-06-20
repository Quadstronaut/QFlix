#requires -Version 5.1
<#
.SYNOPSIS
    Watch Maintainerr on the seedbox for the Plex external-ID resolution fix and
    ping Quadstronaut (Mission Control #temp) once it lands.

.DESCRIPTION
    Background: Maintainerr 3.15.0's Radarr/Sonarr action handler can't resolve
    Plex items' external IDs ("Couldn't resolve any supported external IDs"), so
    its 60-day autodelete silently does nothing on Plex. We replaced the delete
    engine with scripts/maint/qflix-reaper.py (armed, daily) but left Maintainerr
    installed and upgrading weekly, waiting for upstream to fix the bug. See
    docs/maintainerr-plex-id-resolution-bug.md.

    Detection (SSH probe of the seedbox, log-based, no false positives):
      * THE bug signal is the literal "Couldn't resolve any supported external
        IDs" line. Its presence in recent logs == still broken. Unambiguous.
      * Absence only counts as FIXED if Maintainerr actually had work to do:
        it logs "N queued for handling" each run; queued_total > 0 with ZERO
        resolve errors == it handled due media and resolved it == fixed.
      * If nothing was queued (collections empty / the reaper cleared items
        first) we report "unknown", never "fixed" -- so we don't false-ping.
    NOTE we deliberately do NOT trust collection_log "Removed ..." rows: an item
    leaves a Maintainerr collection when it disappears from Plex for ANY reason
    (the reaper deleting it included), so a removal row does not prove Maintainerr
    deleted anything.

    On FIXED it calls ~/Documents/ping-me.ps1 with a detailed message, ONCE (a
    state file guards re-pinging). If it later sees "broken" again the guard
    resets, so a regression-then-refix re-pings.

.PARAMETER DryRun
    Evaluate + print the verdict but never ping (and don't flip the pinged flag).

.PARAMETER Force
    Ping even if already pinged for this fix (for testing the ping path).

.NOTES
    Schedule it (the seedbox upgrades Maintainerr on Mondays):
      schtasks /Create /SC DAILY /ST 09:00 /TN "QFlix\MaintainerrFixWatch" /TR ^
        "powershell -NoProfile -ExecutionPolicy Bypass -File \"G:\Documents\GIT\Ultra.cc\QFlix\scripts\ops\maintainerr-fix-watch.ps1\""
    Needs key-based SSH to the seedbox (same as the rest of the repo tooling).
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# --- paths / config ---------------------------------------------------------
$RepoRoot   = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # ...\QFlix
$PingScript = Join-Path $env:USERPROFILE 'Documents\ping-me.ps1'
$StateFile  = Join-Path $env:USERPROFILE 'Documents\.maintainerr-fix-watch.state.json'

# SSH host: prefer the gitignored real FQDN, fall back to the known default.
$hostFile = Join-Path $RepoRoot 'secrets\seedbox.ssh-host'
if (Test-Path $hostFile) { $fqdn = (Get-Content $hostFile -Raw).Trim() } else { $fqdn = 'seedbox.example.com' }
$SshHost = "quadstronaut@$fqdn"

function Log([string]$m) { Write-Host ("[reaper-watch] " + $m) }

# --- the remote probe (stdlib python 3.9 on the box; emits one JSON line) ----
# Piped to `python3 -` over SSH so there is no cross-shell quoting to escape.
$Probe = @'
import json, os, glob, gzip, time, re, urllib.request

HOME = os.path.expanduser("~")
SEC = os.path.join(HOME, "secrets")
ERRSTR = "Couldn't resolve any supported external IDs"
LOGDIR = os.path.join(HOME, ".apps", "maintainerr", "logs")
WINDOW_DAYS = 10

def secret(n):
    with open(os.path.join(SEC, n)) as f:
        return f.read().strip()

out = {
    "version": None, "commitTag": None, "updateAvailable": None,
    "action_attempts": 0, "resolve_errors": 0, "queued_total": 0,
    "verdict": "unknown", "evidence": "", "error": None,
}

# version (also confirms Maintainerr is up). localhost:port is fronted by nginx
# basic-auth on this box, so send Basic auth + the app X-Api-Key.
try:
    import base64
    port = secret("maintainerr.port"); key = secret("maintainerr.key")
    hdrs = {"X-Api-Key": key}
    try:
        htpw = secret("htpasswd.password")
        hdrs["Authorization"] = "Basic " + base64.b64encode(("quadstronaut:" + htpw).encode()).decode()
    except Exception:
        pass
    req = urllib.request.Request("http://127.0.0.1:%s/api/app/status" % port, headers=hdrs)
    with urllib.request.urlopen(req, timeout=8) as r:
        d = json.load(r)
    out["version"] = d.get("version")
    out["commitTag"] = d.get("commitTag")
    out["updateAvailable"] = d.get("updateAvailable")
except Exception as e:
    out["error"] = "status: " + str(e)

# recent logs: bug = the resolve error; proof-of-work = "N queued for handling"
cutoff = time.time() - WINDOW_DAYS * 86400
try:
    for path in sorted(glob.glob(os.path.join(LOGDIR, "maintainerr-*.log*"))):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "ActionHandler]" in line:
                        out["action_attempts"] += 1
                        if ERRSTR in line:
                            out["resolve_errors"] += 1
                    elif "queued for handling" in line:
                        m = re.search(r"(\d+)\s+queued for handling", line)
                        if m:
                            out["queued_total"] += int(m.group(1))
        except Exception:
            continue
except Exception as e:
    out["error"] = (out["error"] or "") + " logs: " + str(e)

# verdict — resolve error == unambiguous bug; absence only "fixed" if it had work
if out["resolve_errors"] > 0:
    out["verdict"] = "broken"
    out["evidence"] = ("%d resolve-error line(s) in last %dd of logs -- bug still active"
                       % (out["resolve_errors"], WINDOW_DAYS))
elif out["queued_total"] > 0:
    out["verdict"] = "fixed"
    out["evidence"] = ("%d item(s) queued for handling in last %dd with ZERO resolve errors -- Maintainerr is resolving/deleting again"
                       % (out["queued_total"], WINDOW_DAYS))
elif out["version"] is None:
    out["verdict"] = "unknown-down"
    out["evidence"] = "Maintainerr unreachable and no resolve errors in recent logs; no signal this run"
else:
    out["verdict"] = "unknown"
    out["evidence"] = ("no due media handled in last %dd (collections empty / reaper clears them first); cannot tell yet"
                       % WINDOW_DAYS)

print(json.dumps(out))
'@

# --- run probe over SSH -----------------------------------------------------
Log "probing $SshHost ..."
$raw = $Probe | ssh -o BatchMode=yes -o ConnectTimeout=15 $SshHost "python3 -" 2>$null
if (-not $raw) {
    Log "ERROR: empty probe result (SSH down? key auth?). Exiting without action."
    exit 1
}
try {
    $p = $raw | Where-Object { $_.Trim().StartsWith('{') } | Select-Object -Last 1 | ConvertFrom-Json
} catch {
    Log "ERROR: could not parse probe JSON: $raw"
    exit 1
}

Log ("verdict={0} version={1} updateAvailable={2} attempts={3} resolveErrors={4} queued={5}" -f $p.verdict, $p.version, $p.updateAvailable, $p.action_attempts, $p.resolve_errors, $p.queued_total)
Log ("evidence: " + $p.evidence)
if ($p.error) { Log ("probe note: " + $p.error) }

# --- state ------------------------------------------------------------------
$state = @{ pinged = $false; last_version = $null; last_verdict = $null; last_check = $null }
if (Test-Path $StateFile) {
    try {
        $j = Get-Content $StateFile -Raw | ConvertFrom-Json
        foreach ($k in 'pinged','last_version','last_verdict','last_check') {
            if ($null -ne $j.$k) { $state[$k] = $j.$k }
        }
    } catch { }
}
$alreadyPinged = [bool]$state['pinged']

# --- decide -----------------------------------------------------------------
if ($p.verdict -eq 'fixed') {
    if ($alreadyPinged -and -not $Force) {
        Log "fixed, but already pinged for this fix -- no re-ping."
    } else {
        $msg = "Maintainerr Plex-delete bug appears FIXED. Maintainerr is now version $($p.version) ($($p.commitTag)) and its autodelete is resolving and deleting on Plex again (it was broken on 3.15.0 with the error: Couldnt resolve any supported external IDs, so the 60-day cleanup silently did nothing). Evidence: $($p.evidence). While it was broken, qflix-reaper (scripts/maint/qflix-reaper.py) has been doing the 60-day deletes, armed and daily around 05:00 UTC. DECISION NEEDED: keep qflix-reaper as the engine, or revert to Maintainerr? Revert means re-add the Maintainerr Kuma monitor (scripts/maint/bootstrap-kuma-monitors.py) plus its manifest entry, then disable the reaper (rm manitoba-maint-reaper.service.d/10-execute.conf). Keep the reaper means Maintainerr can be uninstalled. Refs: docs/maintainerr-plex-id-resolution-bug.md and memory maintainerr-autodelete-broken. Automated by scripts/ops/maintainerr-fix-watch.ps1."
        if ($DryRun) {
            Log ("DRY-RUN: would ping with message:`n" + $msg)
        } else {
            Log "FIXED detected -> pinging via ping-me.ps1"
            & $PingScript $msg
            $state['pinged'] = $true
        }
    }
} else {
    if ($alreadyPinged) { Log ("verdict regressed to '" + $p.verdict + "'; resetting pinged guard.") }
    $state['pinged'] = $false
}

# --- persist state ----------------------------------------------------------
$state['last_version'] = $p.version
$state['last_verdict'] = $p.verdict
$state['last_check']   = (Get-Date).ToString('o')
$state | ConvertTo-Json | Set-Content -Path $StateFile -Encoding UTF8
Log ("state saved -> " + $StateFile)
