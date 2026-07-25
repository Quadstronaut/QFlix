#Requires -Version 5.1
<#
.SYNOPSIS
    Ollama recovery — bring the workstation Ollama server back up on demand.

.DESCRIPTION
    Manually-triggered companion to qflix-rea.ps1. REA posts a Discord WARN
    ("Ollama appears down") when http://localhost:11434 doesn't answer at logon.
    This script performs the exact recovery an operator would do by hand:

      1. Ensure the model store exists and OLLAMA_MODELS points at it.
      2. (Re)start the "\Archangel\Ollama Serve" task.
      3. Wait for /api/tags to answer, then report model count.

    Root cause it was written for (2026-07-24): the store used to live at a
    symlink .ollama\models -> G:\AIModels; G:\AIModels was deleted, so
    `ollama serve` died on startup ("mkdir ... not traversable") on every boot.
    The store was relocated to B:\AIModels and pinned via the OLLAMA_MODELS
    env var, which removes the fragile dangling-symlink failure mode entirely.

    No secrets — safe to track in git (unlike qflix-rea.ps1).

.PARAMETER Install
    Register the manually-triggered "\Archangel\Ollama Recovery" task (no
    schedule trigger — runs only when started by hand or by the Documents
    launcher).

.PARAMETER Uninstall
    Remove that task.

.PARAMETER Quiet
    Minimal console output (used when the scheduled task runs hidden). The log
    file is written either way.
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Quiet
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# ---------- Configuration ----------
$Script:ModelsDir       = 'B:\AIModels'               # relocated store (was G:\AIModels)
$Script:OllamaBase      = 'http://localhost:11434'
$Script:ServeTaskName   = 'Ollama Serve'
$Script:ServeTaskPath   = '\Archangel\'
$Script:RecoverTaskName = 'Ollama Recovery'
$Script:RecoverTaskPath = '\Archangel\'
$Script:HealthRetries   = 20
$Script:HealthDelaySec  = 2

# Resolve the Ollama binary the same defensive way qflix-rea.ps1 does.
$Script:OllamaExe = $(
    $c = Get-Command ollama -ErrorAction SilentlyContinue
    if ($c -and $c.Source)                                        { $c.Source }
    elseif (Test-Path "$env:USERPROFILE\scoop\shims\ollama.exe")  { "$env:USERPROFILE\scoop\shims\ollama.exe" }
    else                                                          { "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
)

# ---------- Logging ----------
function Get-LogDir {
    $d = Join-Path $env:APPDATA 'ollama-recover'
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    return $d
}

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} {1,-5} {2}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz'), $Level, $Message
    Add-Content -LiteralPath (Join-Path (Get-LogDir) 'recover.log') -Value $line -Encoding UTF8
    if (-not $Quiet) {
        $color = switch ($Level) { 'ERROR' { 'Red' } 'WARN' { 'Yellow' } 'OK' { 'Green' } default { 'Gray' } }
        Write-Host $line -ForegroundColor $color
    }
}

# ---------- Recovery steps ----------
function Assert-ModelStore {
    # The store must exist and OLLAMA_MODELS must point at it, else serve either
    # writes to the wrong place or (with a dangling symlink) fails to start.
    $drive = (Split-Path -Qualifier $Script:ModelsDir)          # e.g. "B:"
    if (-not (Test-Path "$drive\")) {
        Write-Log "Store drive $drive is not mounted — cannot recover." 'ERROR'
        throw "drive $drive missing"
    }
    if (-not (Test-Path -LiteralPath $Script:ModelsDir)) {
        New-Item -ItemType Directory -Path $Script:ModelsDir -Force | Out-Null
        Write-Log "Created missing store dir $Script:ModelsDir" 'WARN'
    }
    $cur = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'User')
    if ($cur -ne $Script:ModelsDir) {
        [Environment]::SetEnvironmentVariable('OLLAMA_MODELS', $Script:ModelsDir, 'User')
        Write-Log "Set OLLAMA_MODELS (User) = $Script:ModelsDir (was '$cur')" 'WARN'
    }
    $env:OLLAMA_MODELS = $Script:ModelsDir                       # this session too
}

function Test-Ollama {
    try {
        $null = Invoke-WebRequest -Uri "$($Script:OllamaBase)/api/tags" -TimeoutSec 4 -UseBasicParsing
        return $true
    } catch { return $false }
}

function Restart-Serve {
    # Stop the supervising task + any stray server, then start it fresh so it
    # re-reads OLLAMA_MODELS.
    try { Stop-ScheduledTask -TaskName $Script:ServeTaskName -TaskPath $Script:ServeTaskPath -ErrorAction SilentlyContinue } catch {}
    Get-Process -Name 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    try {
        Start-ScheduledTask -TaskName $Script:ServeTaskName -TaskPath $Script:ServeTaskPath
        Write-Log "Started task $($Script:ServeTaskPath)$($Script:ServeTaskName)"
    } catch {
        # Fall back to launching serve directly if the task is missing.
        Write-Log "Serve task unavailable ($_); launching '$($Script:OllamaExe) serve' directly" 'WARN'
        Start-Process -FilePath $Script:OllamaExe -ArgumentList 'serve' -WindowStyle Hidden
    }
}

function Wait-Ollama {
    for ($i = 1; $i -le $Script:HealthRetries; $i++) {
        if (Test-Ollama) { return $true }
        Start-Sleep -Seconds $Script:HealthDelaySec
    }
    return $false
}

function Get-ModelCount {
    try {
        $r = Invoke-RestMethod -Uri "$($Script:OllamaBase)/api/tags" -TimeoutSec 6
        if ($r -and $r.models) { return @($r.models).Count }
        return 0
    } catch { return 0 }
}

function Invoke-Recovery {
    Write-Log "=== Ollama recovery start (store=$Script:ModelsDir) ==="
    Assert-ModelStore
    Restart-Serve
    if (Wait-Ollama) {
        $n = Get-ModelCount
        Write-Log "Ollama is UP on $Script:OllamaBase — $n model(s) present." 'OK'
        if ($n -eq 0) {
            Write-Log "Store is EMPTY. Re-pull with: ollama pull qwen3-coder:30b; qwen3:8b; qwen2.5-coder:7b; bge-m3; qwen3-vl:8b" 'WARN'
        }
        return 0
    }
    Write-Log "Ollama still not responding after ~$([int]($Script:HealthRetries*$Script:HealthDelaySec))s." 'ERROR'
    return 1
}

# ---------- Task install / uninstall (manual trigger => no -Trigger) ----------
function Install-Task {
    $ps = Join-Path $PSHOME 'powershell.exe'
    $arg = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Quiet"
    $action    = New-ScheduledTaskAction -Execute $ps -Argument $arg
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    if (Get-ScheduledTask -TaskName $Script:RecoverTaskName -TaskPath $Script:RecoverTaskPath -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Script:RecoverTaskName -TaskPath $Script:RecoverTaskPath -Confirm:$false
    }
    Register-ScheduledTask -TaskName $Script:RecoverTaskName -TaskPath $Script:RecoverTaskPath `
        -Action $action -Settings $settings -Principal $principal `
        -Description 'Manually-triggered: ensure the model store + OLLAMA_MODELS, then (re)start Ollama Serve and verify. No schedule trigger.' | Out-Null
    Write-Host "Installed manual task: $($Script:RecoverTaskPath)$($Script:RecoverTaskName)" -ForegroundColor Green
}

function Uninstall-Task {
    if (Get-ScheduledTask -TaskName $Script:RecoverTaskName -TaskPath $Script:RecoverTaskPath -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Script:RecoverTaskName -TaskPath $Script:RecoverTaskPath -Confirm:$false
    }
    Write-Host "Uninstalled task: $($Script:RecoverTaskPath)$($Script:RecoverTaskName)" -ForegroundColor Yellow
}

# ---------- Entry ----------
if ($Install)   { Install-Task;   exit 0 }
if ($Uninstall) { Uninstall-Task; exit 0 }
exit (Invoke-Recovery)
