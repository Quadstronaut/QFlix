<#
.SYNOPSIS
  Install workstation-local VictoriaLogs for QFlix log aggregation.

.DESCRIPTION
  Downloads the VictoriaLogs Windows binary, sets up B:\Tools\victorialogs\
  and B:\QFlix\logs\, and registers two Scheduled Tasks:
    1. \Archangel\QFlix-Logging\VictoriaLogs (AtLogOn) - the long-running server
    2. \Archangel\QFlix-Logging\Ship Logs    (every 5 min) - SSH-pulls from seedbox

  UI: http://127.0.0.1:9428/select/vmui/
  API: http://127.0.0.1:9428/select/logsql/query

.PARAMETER Install
  Create everything (idempotent).

.PARAMETER Uninstall
  Remove both Scheduled Tasks. Leaves binary + B:\QFlix\logs in place.

.PARAMETER Version
  VictoriaLogs release tag. Default: v1.50.0.
#>

[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [string]$Version = 'v1.50.0'
)

$ErrorActionPreference = "Stop"
$ToolsRoot   = "B:\Tools\victorialogs"
$DataRoot    = "B:\QFlix\logs"
$Port        = 9428
$Retention   = "90d"
$TaskFolder  = "\Archangel\QFlix-Logging"
$ServerTask  = "VictoriaLogs"
$ShipTask    = "Ship Logs"
$RepoRoot    = (Resolve-Path "$PSScriptRoot\..\..").Path
$ShipPS1     = Join-Path $RepoRoot "scripts\local\qflix-vlogs-ship.ps1"

function Get-VLogsExe {
    $cand = Get-ChildItem $ToolsRoot -Filter 'victoria-logs-prod*.exe' -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($cand) { return $cand.FullName }
    return $null
}

function Install-Binary {
    New-Item -ItemType Directory -Force -Path $ToolsRoot, $DataRoot | Out-Null
    $existing = Get-VLogsExe
    if ($existing) {
        Write-Host "Binary already present: $existing (skipping download)"
        return
    }
    $asset = "victoria-logs-windows-amd64-$Version.zip"
    $url   = "https://github.com/VictoriaMetrics/VictoriaLogs/releases/download/$Version/$asset"
    $zip   = Join-Path $env:TEMP $asset
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $ToolsRoot -Force
    Remove-Item $zip
    $exe = Get-VLogsExe
    if (-not $exe) { throw "Extraction completed but no victoria-logs-prod*.exe found in $ToolsRoot" }
    Write-Host "Installed binary: $exe"
}

function Register-ServerTask {
    $exe = Get-VLogsExe
    if (-not $exe) { throw "VictoriaLogs binary missing in $ToolsRoot" }
    $argLine = "-storageDataPath=$DataRoot -httpListenAddr=127.0.0.1:$Port -retentionPeriod=$Retention -loggerOutput=stderr"
    $action  = New-ScheduledTaskAction -Execute $exe -Argument $argLine
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 5 `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
    Register-ScheduledTask -TaskName $ServerTask -TaskPath $TaskFolder `
        -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Registered: $TaskFolder\$ServerTask (AtLogOn)"
}

function Register-ShipTask {
    if (-not (Test-Path $ShipPS1)) {
        throw "Shipper script not found at $ShipPS1"
    }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ShipPS1`""
    $now = Get-Date
    $first = $now.AddMinutes(2)
    $trigger = New-ScheduledTaskTrigger -Once -At $first `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 9125)
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
    Register-ScheduledTask -TaskName $ShipTask -TaskPath $TaskFolder `
        -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Registered: $TaskFolder\$ShipTask (every 5 min from $first)"
}

function Unregister-Task {
    param([string]$name)
    Get-ScheduledTask -TaskPath "$TaskFolder\" -TaskName $name -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false
    Write-Host "Removed: $TaskFolder\$name"
}

if ($Install) {
    Install-Binary
    Register-ServerTask
    Register-ShipTask
    Write-Host ""
    Write-Host "Done."
    Write-Host "  UI:  http://127.0.0.1:$Port/select/vmui/"
    Write-Host "  Start server now: Start-ScheduledTask -TaskPath '$TaskFolder\' -TaskName '$ServerTask'"
    Write-Host "  Trigger ship now: Start-ScheduledTask -TaskPath '$TaskFolder\' -TaskName '$ShipTask'"
} elseif ($Uninstall) {
    Unregister-Task -name $ShipTask
    Unregister-Task -name $ServerTask
} else {
    Write-Host "Usage: $($MyInvocation.MyCommand.Name) -Install | -Uninstall"
}
