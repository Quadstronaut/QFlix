#requires -Version 5.1
<#
.SYNOPSIS
    Pushes the Fix page's routine credentials (QFLX-70) into QFlix Admin.

.DESCRIPTION
    The Fix page POSTs the operator's prompt to a Claude Code routine's /fire
    endpoint. The routine's bearer token can start a fully-autonomous cloud
    session with box access, so it never goes in the APK or the repo: it
    lives in a gitignored-by-location file on the workstation
    (~/.qflix-cloud-key/routine.json by default) and in the app's private
    storage on the phone, nowhere else.

    routine.json shape (matches net/RoutineFire.kt RoutineConfig):
      {"fireUrl":"https://api.anthropic.com/v1/claude_code/routines/trig_.../fire",
       "token":"sk-ant-oat01-..."}

    Independent of provision.ps1 (SSH bundle): the Fix page works without it.
    Idempotent - re-run after rotating the routine token.

.EXAMPLE
    .\provision-fixer.ps1
    .\provision-fixer.ps1 -RoutineJson D:\keys\routine.json
#>
[CmdletBinding()]
param(
    [string]$RoutineJson = (Join-Path $HOME '.qflix-cloud-key\routine.json'),
    [string]$AppId = 'com.qflix.admin'
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    Write-Host "FAILED: $Message" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $RoutineJson)) { Fail "No routine config at $RoutineJson." }
# Validate shape before it reaches the phone: a typo here only shows up as
# "Not sent: ..." on the Fix page, far from the cause.
$cfg = Get-Content $RoutineJson -Raw | ConvertFrom-Json
if (-not $cfg.fireUrl -or $cfg.fireUrl -notmatch '^https://api\.anthropic\.com/v1/claude_code/routines/trig_[A-Za-z0-9]+/fire$') {
    Fail "fireUrl missing or not a routines /fire URL."
}
if (-not $cfg.token) { Fail 'token missing.' }

$devices = (& adb devices) | Select-Object -Skip 1 | Where-Object { $_ -match '\sdevice$' }
if (-not $devices) { Fail 'No adb device. Plug in, unlock, allow USB debugging.' }

# Stage via /data/local/tmp, then copy into app-private storage with run-as
# (debug build) - same path provision.ps1 uses for the SSH bundle.
# adb push reports success on stderr; merge inside cmd.exe so PS 5.1 does
# not treat it as a terminating error.
cmd /c "adb push `"$RoutineJson`" /data/local/tmp/qflix-routine.json 2>&1" | Out-Null
if ($LASTEXITCODE -ne 0) { Fail 'adb push failed.' }
try {
    & adb shell "run-as $AppId sh -c 'mkdir -p files/provision && cp /data/local/tmp/qflix-routine.json files/provision/routine.json && chmod 600 files/provision/routine.json'"
    if ($LASTEXITCODE -ne 0) { Fail "run-as copy failed. Is the $AppId debug build installed?" }
}
finally {
    & adb shell rm -f /data/local/tmp/qflix-routine.json | Out-Null
}

& adb shell run-as $AppId test -s files/provision/routine.json
if ($LASTEXITCODE -ne 0) { Fail 'routine.json not present in app storage after copy.' }
Write-Host 'Fix page provisioned. Relaunch QFlix Admin -> drawer -> Fix.' -ForegroundColor Green
